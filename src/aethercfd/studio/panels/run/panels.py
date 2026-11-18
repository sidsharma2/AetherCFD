"""Run, Residuals and Solver Log panels.

Run starts jobs through the run.start command: the full plan (commands, placement, consequences,
risk) is shown before anything happens. Residuals and Solver Log follow the *current log* (a live
run's solver log, or the newest log of an opened case), so finished cases plot immediately too.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
import time
from functools import partial
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aethercfd.commands.base import USER, Risk
from aethercfd.core.runtime import module_argv
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon, mono_font, plot_defaults
from aethercfd.studio.theme.tokens import T

plot_defaults()
_STATE_TOKEN = {"running": "ok", "pending": "info", "paused": "warn", "stopping": "warn", "staging": "info", "launching": "info",
                "syncing": "info", "finished": "text_dim", "failed": "error", "killed": "warn", "lost": "error",
                "guard_stopped": "error", "queued": "text_dim"}


log = logging.getLogger(__name__)


def _state_color(state: str) -> str:
    return getattr(T, _STATE_TOKEN.get(state, "text"))


def _color(name: str) -> str:
    """A fixed, distinguishable colour per field (stable across sessions; the theme's series palette)."""
    return T.series[sum(map(ord, name)) % len(T.series)]


# ============================================================================ Run panel
class PlanDialog(QDialog):
    """Shows exactly what a command will do before it runs (dry run)."""

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run plan")
        self.resize(720, 460)
        lay = QVBoxLayout(self)
        risk = plan.risk
        head = QLabel(plan.summary)
        head.setStyleSheet(f"font-size: 11pt; color: {T.text};")
        head.setWordWrap(True)
        lay.addWidget(head)
        if risk is Risk.DESTRUCTIVE:
            warn = QLabel("This plan is DESTRUCTIVE — read the consequences below.")
            warn.setStyleSheet(f"color: {T.error}; font-weight: 600;")
            lay.addWidget(warn)
        text = QPlainTextEdit(readOnly=True)
        text.setFont(mono_font(9))
        lines = []
        for c in plan.preconditions:
            lines.append(f"{'ok     ' if c.ok else 'BLOCKED'}  {c.message}")
        if plan.shell_steps:
            lines.append("")
            where = ("in the cluster job" if plan.params.get("_resolved", {}).get("placement") == "cluster"
                     else "inside WSL")
            lines.append(f"Commands (run {where} with the case's OpenFOAM version):")
            for s in plan.shell_steps:
                lines.append(f"  $ {' '.join(s.argv)}" + (f"   > {s.log_name}" if s.log_name else ""))
        if plan.consequences:
            lines.append("")
            lines.append("Consequences:")
            lines += [f"  • {c}" for c in plan.consequences]
        text.setPlainText("\n".join(lines))
        lay.addWidget(text, 1)
        bb = QDialogButtonBox()
        self.go = bb.addButton("Run" if not plan.blocked else "Blocked", QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setEnabled(not plan.blocked)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class RunPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        sv = ctx.services
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        lay = QVBoxLayout(body)  # scrolls instead of squashing when the dock is short
        lay.setContentsMargins(8, 8, 8, 8)
        # --- start form
        form = QFormLayout()
        from aethercfd.commands.builtin.run_cmds import PRESETS

        self.preset = QComboBox(toolTip="Automatic picks what this case needs: the tutorial's ./Allrun, mesh + "
                                        "check + solve when there is no mesh yet, else solve")
        for key, label in PRESETS.items():
            self.preset.addItem(label, key)
        self.nprocs = QSpinBox(minimum=1, maximum=64, value=1)
        self.placement = QComboBox()
        for label, key in (("Automatic", "auto"), ("In place (/mnt/…)", "in-place"), ("Staged (ext4 copy)", "staged")):
            self.placement.addItem(label, key)
        self.signals = QCheckBox("Enable write-now / stop-and-write controls")
        self.signals.setChecked(True)
        # where it runs: this PC (WSL) or a cluster profile (ADR 0007)
        self.target = QComboBox(toolTip="This PC (WSL), or a Slurm job on a cluster profile")
        self.clusters_btn = QPushButton("Clusters…", toolTip="Add, test or sign in to cluster profiles")
        self.clusters_btn.clicked.connect(self._edit_clusters)
        trow = QHBoxLayout()
        trow.addWidget(self.target, 1)
        trow.addWidget(self.clusters_btn)
        self.walltime = QLineEdit(toolTip="Job time limit (HH:MM:SS or D-HH:MM:SS); empty: the profile's default")
        self.target.currentIndexChanged.connect(lambda *_: self._on_target())
        form.addRow("Run", self.preset)
        self.preset_hint = QLabel()
        self.preset_hint.setWordWrap(True)
        self.preset_hint.setStyleSheet(f"color: {T.text_faint};")
        form.addRow("", self.preset_hint)
        self.preset.currentIndexChanged.connect(lambda *_: self._show_preset_hint())
        form.addRow("Where", trow)
        form.addRow("MPI processes", self.nprocs)
        form.addRow("Placement", self.placement)
        form.addRow("Walltime", self.walltime)
        form.addRow("", self.signals)
        lay.addLayout(form)
        self._form = form
        self._fill_targets()
        self.start_btn = QPushButton(icon("msc.play", T.ok), "Start…")
        self.start_btn.clicked.connect(self.start)
        lay.addWidget(self.start_btn)
        # --- live status
        self.state = QLabel("No run")
        self.state.setStyleSheet(f"font-size: 11pt; color: {T.text_dim}; padding-top: 8px;")
        lay.addWidget(self.state)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 1000)
        lay.addWidget(self.progress)
        grid = QGridLayout()
        self.vals = {}
        for i, (k, label) in enumerate((("time", "Sim time"), ("eta", "ETA"), ("co", "Courant max"),
                                        ("dt", "Δt"), ("cont", "Continuity"), ("wall", "Wall time"))):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {T.text_faint};")
            val = QLabel("–")
            val.setFont(mono_font(9))
            grid.addWidget(lbl, i // 2, (i % 2) * 2)
            grid.addWidget(val, i // 2, (i % 2) * 2 + 1)
            self.vals[k] = val
        lay.addLayout(grid)
        ctl = QGridLayout()
        ctl.setSpacing(4)
        self.btns = {}
        for key, label, ic, col in (("write_now", "Write now", "msc.save", T.text_dim),
                                    ("stop_write", "Stop & write", "msc.debug-stop", T.warn),
                                    ("pause", "Pause", "msc.debug-pause", T.text_dim),
                                    ("resume", "Resume", "msc.debug-continue", T.text_dim),
                                    ("kill", "Kill", "msc.close", T.error)):
            b = QPushButton(icon(ic, col), label)
            b.setToolTip(label)
            b.clicked.connect(lambda _=False, a=key: self.control(a))
            i = len(self.btns)
            ctl.addWidget(b, i // 2, i % 2)
            self.btns[key] = b
        lay.addLayout(ctl)
        # a run the divergence guard stopped: one plan to restart from its last good time (roadmap Q6)
        rrow = QHBoxLayout()
        self.restart_btn = QPushButton(icon("msc.debug-restart", T.ok), "Restart from last good time…")
        self.restart_btn.setToolTip("Roll back to the newest written time before the divergence and restart with "
                                    "gentler numerics (relaxation times 0.7 or the time step times 0.5). Shown as a plan first.")
        self.restart_btn.clicked.connect(self.plan_restart)
        self.upwind = QCheckBox("first-order convection")
        self.upwind.setToolTip("Also switch convection to first-order upwind (more robust, less accurate: switch "
                               "back once the run is stable)")
        rrow.addWidget(self.restart_btn, 1)
        rrow.addWidget(self.upwind)
        lay.addLayout(rrow)
        self.restart_btn.hide()
        self.upwind.hide()
        self.guards = QListWidget()
        self.guards.setMaximumHeight(90)
        lay.addWidget(QLabel("Guard & run messages"))
        lay.addWidget(self.guards)
        lay.addWidget(QLabel("Runs of this case"))
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(["Run", "State", "Exit", "Steps"])
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.verticalHeader().hide()
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.setMinimumHeight(120)
        lay.addWidget(self.history, 1)
        self.run_id: str | None = None
        self.end_time: float | None = None
        self._t0 = None
        self._last_step = None
        sv.bridge.event.connect(self._on_event)
        sv.follower.steps.connect(self._on_steps)
        sv.case_changed.connect(self._on_session)  # bound slots: disconnected when the panel closes
        sv.backend_ready.connect(self._on_session)
        sv.files_changed.connect(self._on_files)
        self._timer = QTimer(self, interval=2000, timeout=self._tick)
        self._timer.start()
        self._refresh()

    # ------------------------------------------------------------------ helpers
    def _session(self):
        return self.ctx.services.session

    def _refresh(self) -> None:
        s = self._session()
        ready = s is not None and s.runner is not None and bool(s.forks)
        self.start_btn.setEnabled(ready and not getattr(self, "_starting", False))
        self.start_btn.setToolTip("" if ready else "Open a case; the OpenFOAM backend starts in the background")
        self._read_end_time()
        self._fill_history()
        self._update_buttons()
        self._show_preset_hint()

    def _on_session(self, _s=None) -> None:
        self._fill_targets()
        self._refresh()

    # ------------------------------------------------------------------ targets (local WSL / clusters)
    def _fill_targets(self) -> None:
        keep = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        self.target.addItem("This PC (WSL)", "local")
        for c in self.ctx.services.settings.clusters:
            self.target.addItem(f"Cluster: {c.name} ({c.destination})", c.target)
        i = self.target.findData(keep) if keep else 0
        self.target.setCurrentIndex(max(0, i))
        self.target.blockSignals(False)
        self._on_target()

    def _on_target(self) -> None:
        target = self.target.currentData() or "local"
        cluster = target != "local"
        self._form.setRowVisible(self.placement, not cluster)  # a cluster run always uploads to its work dir
        self._form.setRowVisible(self.walltime, cluster)
        self.nprocs.setMaximum(4096 if cluster else 64)
        if cluster:
            prof = next((c for c in self.ctx.services.settings.clusters if c.target == target), None)
            self.walltime.setPlaceholderText(prof.walltime if prof else "")

    def _edit_clusters(self) -> None:
        from aethercfd.studio.dialogs.clusters import ClusterProfilesDialog

        if ClusterProfilesDialog(self.ctx.services, self).exec() == QDialog.DialogCode.Accepted:
            self._fill_targets()

    def _on_files(self, _paths) -> None:
        # a run (or the user) meshed the case: what Start will do next may have changed
        if self.preset.currentData() == "auto":
            self._show_preset_hint()

    def _show_preset_hint(self) -> None:
        s = self._session()
        if s is None or self.preset.currentData() != "auto":
            self.preset_hint.setText("")
            return
        try:
            from aethercfd.commands.builtin.run_cmds import PRESETS, suggest_preset

            key, why = suggest_preset(s.case())
            self.preset_hint.setText(f"Start will run: {PRESETS[key]} ({why})")
        except Exception as exc:  # an unreadable case: Start's plan explains it
            self.preset_hint.setText(f"(cannot read the case: {exc})")

    def _read_end_time(self) -> None:
        s = self._session()
        self.end_time = None
        if s is None:
            return
        try:
            from aethercfd.foamdict.parser import parse
            from aethercfd.foamdict.semantic import DictView

            doc = self.ctx.services.documents.open(s.root / "system" / "controlDict")
            v = DictView(parse(doc.text)).get_text("endTime")
            self.end_time = float(v) if v else None
        except (OSError, ValueError):
            self.end_time = None

    def _fill_history(self) -> None:
        s = self._session()
        self.history.setRowCount(0)
        if s is None or s.runs is None:
            return
        recs = [r for r in s.runs.runs() if r.case_root == str(s.root)]
        for r in reversed(recs[-30:]):
            row = self.history.rowCount()
            self.history.insertRow(row)
            for col, val in enumerate((r.id[-13:], r.state, "" if r.exit_code is None else str(r.exit_code),
                                       " → ".join(st["app"] for st in r.steps))):
                it = QTableWidgetItem(val)
                if col == 1:
                    it.setForeground(QColor(_state_color(r.state)))
                self.history.setItem(row, col, it)
            if self.run_id is None and r.state in ("pending", "running", "paused", "staging", "stopping", "syncing"):
                self.run_id = r.id

    def _update_buttons(self) -> None:
        s = self._session()
        rec = s.runs.get(self.run_id) if (s and s.runs and self.run_id) else None
        active = rec is not None and rec.state in ("pending", "running", "paused", "stopping")
        for _k, b in self.btns.items():
            b.setEnabled(active)
        if rec is not None:
            cluster = rec.is_cluster
            self.btns["pause"].setVisible(rec.state != "paused" and not cluster)  # no suspend on clusters
            self.btns["resume"].setVisible(rec.state == "paused" and not cluster)
            sig = rec.signals_enabled and rec.state != "pending"  # a queued job has nothing to write yet
            self.btns["write_now"].setEnabled(active and sig)
            self.btns["stop_write"].setEnabled(active and sig)
            col = _state_color(rec.state)
            extra = f"  ·  {rec.stop_reason}" if rec.stop_reason else ""
            where = rec.placement
            if cluster:
                where = f"{rec.target.split(':', 1)[1]} · job {rec.job_id or '…'}" + (
                    f" · {rec.scheduler_state}" if rec.scheduler_state else "")
            self.state.setText(f"<span style='color:{col}'>● {rec.state}</span>  "
                               f"<span style='color:{T.text_dim}'>{where}{extra}</span>")
        else:
            self.btns["resume"].setVisible(False)
        diverged = rec is not None and bool(rec.divergence) and not active
        self.restart_btn.setVisible(diverged)
        self.upwind.setVisible(diverged)
        self.restart_btn.setEnabled(diverged and not getattr(self, "_restarting", False))

    # ------------------------------------------------------------------ restart after divergence (Q6)
    def plan_restart(self) -> None:
        s = self._session()
        rec = s.runs.get(self.run_id) if (s and s.runs and self.run_id) else None
        if rec is None or not rec.divergence or not self.ctx.services.ensure_project():
            return
        root, div, upwind = s.root, dict(rec.divergence), self.upwind.isChecked()
        log_path = s.runs.exec_case_win(rec) / div["log"] if div.get("log") else None
        self._restarting = True
        self.restart_btn.setText("Planning the restart…")
        self._update_buttons()

        def work():
            # reading every field of the candidate times and asking Murphy can take a while: off the GUI thread
            from aethercfd.case.rollback import plan_restart

            plan = plan_restart(root, div, first_order=upwind)
            murphy = None
            if log_path is not None and log_path.is_file():
                try:
                    from aethercfd.services.cfd_agents import CfdAgents

                    r = CfdAgents.from_settings().murphy(log_path, success=False, converged=False)
                    murphy = r.explanation if r.explanation else None
                except Exception:
                    log.exception("Murphy could not read %s", log_path)
            return plan, murphy

        self.ctx.services.run_bg(work, partial(self._restart_planned, div))

    def _restart_planned(self, div: dict, result) -> None:
        self._restarting = False
        self.restart_btn.setText("Restart from last good time…")
        self._update_buttons()
        out, err = result
        s = self._session()
        if err is not None or s is None:
            self.ctx.services.notify(f"Cannot plan the restart: {err}", "error")
            return
        plan, murphy = out
        if plan.errors:
            QMessageBox.warning(self, "No restart possible", "\n".join(plan.errors))
            return
        from aethercfd.studio.widgets.approval import ApprovalDialog

        what = div.get("message") or "the run diverged"
        why = [f"The run diverged: {what}." if div.get("kind") == "crash" else
               f"The divergence watchdog stopped the run: {what}.",
               f"Last good time: {div.get('last_ok_text') or 'before the first step'}. "
               f"Restart from the newest clean write at or before it: {plan.start.name}.",
               *plan.notes]
        if murphy:
            why.append(murphy)
        params = plan.batch_params()
        try:
            cmd_plan = s.executor.plan("dict.batch", params, USER, rationale="\n".join(why))
        except Exception as exc:
            self.ctx.services.notify(f"Cannot plan the restart: {exc}", "error")
            return
        if ApprovalDialog(cmd_plan, self, who="Divergence watchdog").exec() != QDialog.DialogCode.Accepted:
            return
        res = s.executor.execute("dict.batch", params, USER, plan=cmd_plan, approved_hash=cmd_plan.plan_hash)
        if res.ok:
            self.ctx.services.notify(f"Restart prepared from time {plan.start.name}: press Start to run it "
                                     "(undo it in the Command Journal)")
        else:
            self.ctx.services.notify(f"The restart plan was not applied: {res.message}", "error")
        self._refresh()

    # ------------------------------------------------------------------ actions
    def start(self) -> None:
        sv = self.ctx.services
        s = self._session()
        if s is None or not sv.ensure_project():
            return
        params = {"case": str(s.root), "preset": self.preset.currentData(), "nprocs": self.nprocs.value(),
                  "placement": self.placement.currentData(), "enable_signals": self.signals.isChecked()}
        target = self.target.currentData() or "local"
        if target != "local":
            params["target"] = target
            if self.walltime.text().strip():
                params["walltime"] = self.walltime.text().strip()
        try:
            plan = s.executor.plan("run.start", params, USER)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot plan run", str(exc))
            return
        dlg = PlanDialog(plan, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # a staged run copies the case to the Linux disk and the launch goes through wsl.exe: seconds to
        # minutes for a big case, so never on the GUI thread
        self._starting = True
        self.start_btn.setEnabled(False)
        self.start_btn.setText("Starting…")

        def work():
            return s.executor.execute("run.start", params, USER, plan=plan, approved_hash=plan.plan_hash)

        sv.run_bg(work, self._started)

    def _started(self, result) -> None:
        res, err = result
        self._starting = False
        self.start_btn.setText("Start…")
        if err is not None or not res.ok:
            self._refresh()
            QMessageBox.warning(self, "Run did not start", str(err) if err is not None else res.message)
            return
        self.run_id = res.data
        self._t0 = time.monotonic()
        self.guards.clear()
        self.ctx.services.notify(res.message)
        self._refresh()

    def control(self, action: str) -> None:
        s = self._session()
        if s is None or self.run_id is None:
            return
        if action == "kill" and QMessageBox.question(
                self, "Kill run", "Kill the run now? Progress since the last written time is lost.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            plan = s.executor.plan("run.control", {"run_id": self.run_id, "action": action}, USER)
        except Exception as exc:
            log.exception("planning %s failed", action)
            self.ctx.services.notify(f"{action}: {exc}", "warn")
            return
        if plan.blocked:
            self.ctx.services.notify(f"{action}: {'; '.join(c.message for c in plan.blocked)}", "warn")
            return
        for b in self.btns.values():  # one control at a time; the reply re-enables them
            b.setEnabled(False)

        def work():
            # a control script goes through wsl.exe, a cluster's through ssh (up to its connect timeout):
            # never on the GUI thread
            return s.executor.execute("run.control", {}, USER, plan=plan, approved_hash=plan.plan_hash)

        self.ctx.services.run_bg(work, partial(self._control_done, action))

    def _control_done(self, action: str, result) -> None:
        res, err = result
        if err is not None:
            self.ctx.services.notify(f"{action}: {err}", "warn")
        elif not res.ok:
            self.ctx.services.notify(f"{action}: {res.message}", "warn")
        self._update_buttons()

    # ------------------------------------------------------------------ live updates
    def _on_event(self, topic: str, p: dict) -> None:
        if not topic.startswith("run."):
            return
        if self.run_id is None and topic == "run.state":
            self.run_id = p["run_id"]
        if p.get("run_id") != self.run_id:
            return
        s = self._session()
        if topic == "run.step_state" and p.get("state") == "running" and p.get("log") and s and s.runs:
            rec = s.runs.get(self.run_id)
            if rec is not None:
                self.ctx.services.set_current_log(s.runs.exec_case_win(rec) / p["log"])
        elif topic == "run.guard":
            col = T.error if p.get("level") == "STOP" else T.warn if p.get("level") == "WARN" else T.info
            self.guards.insertItem(0, f"[{p.get('level')}] {p.get('message')}")
            self.guards.item(0).setForeground(QColor(col))
        elif topic == "run.state":
            self.guards.insertItem(0, f"state → {p['state']}")
            self._fill_history()
        self._update_buttons()

    def _on_steps(self, steps: list) -> None:
        if not steps:
            return
        st = steps[-1]
        self._last_step = st
        self.vals["time"].setText(st.time_text)
        self.vals["co"].setText("–" if st.co_max is None else f"{st.co_max:.3g}")
        self.vals["dt"].setText("–" if st.delta_t is None else f"{st.delta_t:.3g}")
        self.vals["cont"].setText("–" if not st.continuity else f"{st.continuity[0]:.2e}")
        self.vals["wall"].setText("–" if st.clock_time is None else f"{st.clock_time:.0f} s")
        if self.end_time:
            frac = max(0.0, min(1.0, st.time / self.end_time))
            self.progress.setValue(int(frac * 1000))
            self.progress.setFormat(f"{frac:.1%} of endTime {self.end_time:g}")
            if frac >= 0.9995:
                self.vals["eta"].setText("done")
            elif st.clock_time and frac > 0.002:
                remaining = max(0.0, st.clock_time * (1 - frac) / frac)
                self.vals["eta"].setText(f"~{remaining / 60:.0f} min" if remaining > 90 else f"~{remaining:.0f} s")

    def _tick(self) -> None:
        self._update_buttons()


# ============================================================================ Residuals
class ResidualsPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        sv = ctx.services
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        self.source = QLabel("No log")
        self.source.setStyleSheet(f"color: {T.text_dim};")
        bar.addWidget(self.source, 1)
        self.xmode = QComboBox()
        self.xmode.addItems(["Iteration", "Time"])
        self.xmode.currentIndexChanged.connect(self._redraw)
        bar.addWidget(self.xmode)
        pick = QPushButton(icon("msc.folder-opened"), "Log…")
        pick.clicked.connect(self._pick_log)
        bar.addWidget(pick)
        exp = QPushButton(icon("msc.export"), "Export")
        exp.setToolTip("Publication export (your plot_style.py: STIX serif, PDF + PNG)")
        exp.clicked.connect(self.export)
        bar.addWidget(exp)
        lay.addLayout(bar)
        self.plot = pg.PlotWidget()
        self.plot.setLogMode(y=True)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("left", "initial residual")
        self.plot.addLegend(offset=(-10, 10), labelTextColor=T.text_dim)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        lay.addWidget(self.plot, 1)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.data: dict[str, list[float]] = {}
        self.x_iter: list[float] = []
        self.x_time: list[float] = []
        self._dirty = False
        sv.follower.reset.connect(self._reset)
        sv.follower.steps.connect(self._on_steps)
        self._timer = QTimer(self, interval=100, timeout=self._maybe_redraw)  # 10 Hz
        self._timer.start()
        if sv.follower.all_steps:
            self._reset(sv.current_log)
            self._on_steps(sv.follower.all_steps)

    def _reset(self, path: str) -> None:
        self.plot.clear()
        self.plot.addLegend(offset=(-10, 10), labelTextColor=T.text_dim)
        self.curves.clear()
        self.data.clear()
        self.x_iter, self.x_time = [], []
        self.source.setText(Path(path).name if path else "No log")
        self.source.setToolTip(path)

    def _on_steps(self, steps: list) -> None:
        for st in steps:
            n = len(self.x_iter)
            self.x_iter.append(float(st.index))
            self.x_time.append(st.time)
            for name, r in st.residuals.items():
                if name not in self.data:
                    self.data[name] = [math.nan] * n
                self.data[name].append(r.initial if r.initial > 0 else math.nan)
            for arr in self.data.values():
                if len(arr) < n + 1:
                    arr.append(math.nan)
        self._dirty = True

    def _maybe_redraw(self) -> None:
        if self._dirty:
            self._dirty = False
            self._redraw()

    def _redraw(self) -> None:
        x = np.asarray(self.x_time if self.xmode.currentIndex() == 1 else self.x_iter)
        self.plot.setLabel("bottom", "time" if self.xmode.currentIndex() == 1 else "iteration")
        for name, ys in self.data.items():
            y = np.asarray(ys, dtype=float)
            if np.all(np.isnan(y)) or np.nanmax(y) == 0:
                continue  # e.g. rho with 0 residual (diagonal solver)
            c = self.curves.get(name)
            if c is None:
                c = self.plot.plot(name=name, pen=pg.mkPen(_color(name), width=1.4), connect="finite",
                                   skipFiniteCheck=True)
                self.curves[name] = c
            c.setData(x[: len(y)], y)

    def on_theme_changed(self) -> None:
        for name, c in self.curves.items():  # series colours are per theme
            c.setPen(pg.mkPen(_color(name), width=1.4))

    def _pick_log(self) -> None:
        s = self.ctx.services.session
        start = str(s.root) if s else ""
        f, _ = QFileDialog.getOpenFileName(self, "Follow solver log", start, "Logs (log.* *.log);;All files (*)")
        if f:
            self.ctx.services.set_current_log(f)

    def export(self) -> None:
        if not self.data:
            return
        s = self.ctx.services.session
        default = str((s.root if s else Path.home()) / "residuals")
        out, _ = QFileDialog.getSaveFileName(self, "Export residual plot (PDF + PNG)", default, "PDF (*.pdf)")
        if not out:
            return
        out = str(Path(out).with_suffix(""))
        tmp = Path(tempfile.mkdtemp(prefix="aethercfd-plot-"))
        x = np.asarray(self.x_time if self.xmode.currentIndex() == 1 else self.x_iter)
        arrays = {"x": x}
        for k, v in self.data.items():
            y = np.asarray(v, dtype=float)
            if not (np.all(np.isnan(y)) or np.nanmax(y) == 0):
                arrays[k] = y
        np.savez(tmp / "data.npz", **arrays)
        spec = {"npz": str(tmp / "data.npz"), "out": out, "style": self.ctx.services.settings.plot_style,
                "x_label": "Time" if self.xmode.currentIndex() == 1 else "Iteration",
                "title": None, "decimate": 4000}
        (tmp / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        self.ctx.services.notify(f"Exporting the residual plot to {out}.pdf …")
        # matplotlib in a subprocess takes seconds (a first font scan longer): never on the GUI thread
        self.ctx.services.run_bg(partial(_export_residuals, tmp), partial(self._export_done, out))

    def _export_done(self, out: str, result) -> None:
        err, error = result
        if error is not None:
            err = str(error)
        if err is None:
            self.ctx.services.notify(f"Exported {out}.pdf and .png")
        else:
            last = err.strip().splitlines()[-1] if err.strip() else err
            self.ctx.services.notify(f"Residual plot export failed: {last}", "error")
            QMessageBox.warning(self, "Export failed", err[-2000:])

    def save_state(self) -> dict:
        return {"x": self.xmode.currentIndex()}

    def restore_state(self, state: dict) -> None:
        self.xmode.setCurrentIndex(int(state.get("x", 0)))



def _export_residuals(tmp: Path) -> str | None:
    """Draw the exported residual plot in a subprocess (worker thread); None on success, else the error text.
    The temporary spec folder is removed either way."""
    import shutil

    try:
        r = subprocess.run(module_argv("aethercfd.studio.export_plot", str(tmp / "spec.json")),
                           capture_output=True, text=True, timeout=120, creationflags=0x08000000)
        if r.returncode == 0:
            return None
        return (r.stderr or r.stdout or f"exit code {r.returncode}").strip()
    except subprocess.TimeoutExpired:
        return "matplotlib did not finish within 120 s"
    except OSError as exc:
        return f"could not start Python for the export: {exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================ Solver log
class SolverLogPanel(Panel):
    MAX_LINES = 20000

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        sv = ctx.services
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        self.source = QLabel("No log")
        self.source.setStyleSheet(f"color: {T.text_dim};")
        bar.addWidget(self.source, 1)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        bar.addWidget(self.follow)
        fatal = QPushButton(icon("msc.error", T.error), "First error")
        fatal.clicked.connect(self._jump_fatal)
        bar.addWidget(fatal)
        lay.addLayout(bar)
        self.view = QPlainTextEdit(readOnly=True)
        self.view.setFont(mono_font(9))
        self.view.setMaximumBlockCount(self.MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.view, 1)
        self._fmt: dict[str, QTextCharFormat] = {}
        self.on_theme_changed()
        self._pending: list[str] = []
        sv.follower.reset.connect(self._reset)
        sv.follower.lines.connect(self._on_lines)  # (not self._pending.extend: _flush swaps the list)
        self._timer = QTimer(self, interval=50, timeout=self._flush)  # <= 20 Hz
        self._timer.start()
        if sv.current_log:
            self._reset(sv.current_log)
            self._pending.extend(sv.follower.tail_lines)

    def on_theme_changed(self) -> None:
        """Formats for the lines still to come (the shown ones were re-tinted by the theme manager)."""
        for k, c in (("fatal", T.error), ("warn", T.warn), ("time", T.accent), ("plain", T.text)):
            f = QTextCharFormat()
            f.setForeground(QColor(c))
            if k == "time":
                f.setFontWeight(600)
            self._fmt[k] = f

    def _on_lines(self, lines: list) -> None:
        self._pending.extend(lines)

    def _reset(self, path: str) -> None:
        self.view.clear()
        self._pending.clear()
        self.source.setText(Path(path).name if path else "No log")
        self.source.setToolTip(path)

    def _flush(self) -> None:
        if not self._pending:
            return
        lines, self._pending = self._pending[-self.MAX_LINES:], []
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        c = QTextCursor(self.view.document())
        c.movePosition(QTextCursor.MoveOperation.End)
        c.beginEditBlock()
        for ln in lines:
            if self.view.document().characterCount() > 1:
                c.insertBlock()
            kind = ("fatal" if "FOAM FATAL" in ln or ln.startswith(("Floating point exception", "#0  Foam"))
                    else "warn" if "Warning" in ln or ln.startswith("bounding ")
                    else "time" if ln.startswith("Time = ") else "plain")
            c.insertText(ln, self._fmt[kind])
        c.endEditBlock()
        if self.follow.isChecked() and at_bottom:
            sb.setValue(sb.maximum())

    def _jump_fatal(self) -> None:
        doc = self.view.document()
        c = doc.find("FOAM FATAL")
        if c.isNull():
            c = doc.find("Floating point exception")
        if not c.isNull():
            self.follow.setChecked(False)
            self.view.setTextCursor(c)
            self.view.centerCursor()


PANELS = [
    PanelSpec("aether.run_status", "Run", "msc.play-circle", "Run", RunPanel, default_area=Area.RIGHT,
              shortcut="Ctrl+Shift+R", description="Start and control OpenFOAM runs; status, ETA, guards"),
    PanelSpec("aether.residuals", "Residuals", "mdi6.chart-bell-curve-cumulative", "Run", ResidualsPanel,
              default_area=Area.CENTER, description="Live residual plot (log scale) with publication export"),
    PanelSpec("aether.solver_log", "Solver Log", "msc.output", "Run", SolverLogPanel, default_area=Area.BOTTOM,
              description="Live solver log with error/warning highlighting"),
]
