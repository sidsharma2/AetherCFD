"""Workflow: the guided, staged workspace (Geometry → ... → ML / Data Assimilation).

Left: the twelve stages with their completion state; the selected stage below with its next action, its
dependencies, its correctness gates (why, how to fix, one-click fixes) and three modes:

* Guided   -- physical questions ("How fast is the flow?", "Which fluid?"); answers become ONE dict.batch
              plan (every diff shown for approval, journalled, revertable);
* Advanced -- the existing expert tools for the stage (AetherMesh, numerics lab, Combustion, ...);
* Raw      -- the exact files, opened in the editor.

"Mark done" is refused while a gate fails. The state comes from aethercfd.case.guided (no parallel model of
the case); the copilot drives the same path through its workflow tools.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

_STATUS = {  # status -> (icon, colour, label)
    "done": ("msc.pass-filled", T.ok, "done"),
    "ready": ("msc.circle-large-outline", T.info, "ready to mark done"),
    "attention": ("msc.warning", T.warn, "review warnings"),
    "error": ("msc.error", T.error, "needs a fix"),
    "blocked": ("msc.lock", T.text_faint, "waiting for an earlier stage"),
    "changed": ("msc.history", T.warn, "changed since marked done"),
    "todo": ("msc.circle-large-outline", T.text_faint, "to do"),
    "na": ("msc.circle-slash", T.text_faint, "not needed"),
}
_GATE = {"error": ("msc.error", T.error), "warn": ("msc.warning", T.warn), "info": ("msc.info", T.info)}
_RUN_PRESET = {"run.mesh": "mesh", "run.check": "check", "run.start": "auto"}


class WorkflowPanel(Panel):
    STATE_VERSION = 2

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.stages: list = []
        self.current: str | None = None
        self._editors: dict[str, QWidget] = {}
        self._busy = False
        self._last_agent: dict | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        self.progress = QLabel("")
        self.progress.setStyleSheet(f"color: {T.text_dim};")
        bar.addWidget(self.progress, 1)
        self.refresh_btn = QToolButton(icon=icon("msc.refresh"), toolTip="Re-check every stage")
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_btn)
        lay.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnCount(2)
        self.tree.currentItemChanged.connect(self._on_select)
        split.addWidget(self.tree)

        self.page = QWidget()
        pl = QVBoxLayout(self.page)
        pl.setContentsMargins(8, 6, 8, 6)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 600; font-size: 11pt;")
        self.status = QLabel("")
        self.next = QLabel("")
        self.next.setWordWrap(True)
        self.next.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {T.text_dim};")
        self.deps = QLabel("")
        self.deps.setWordWrap(True)
        self.deps.setStyleSheet(f"color: {T.text_dim};")
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.status)
        pl.addLayout(head)
        pl.addWidget(self.next)
        pl.addWidget(self.summary)
        pl.addWidget(self.deps)
        self.gates = QTreeWidget()
        self.gates.setHeaderHidden(True)
        self.gates.setWordWrap(True)
        self.gates.setMinimumHeight(90)
        self.gates.itemDoubleClicked.connect(self._gate_clicked)
        pl.addWidget(QLabel("Correctness gates"))
        pl.addWidget(self.gates, 1)
        self.fix_btn = QPushButton("Apply the selected gate's fix…")
        self.fix_btn.clicked.connect(self.apply_fix)
        self.fix_btn.setEnabled(False)
        self.gates.currentItemChanged.connect(lambda *_: self.fix_btn.setEnabled(self._selected_gate_fix() is not None))
        pl.addWidget(self.fix_btn)

        self.tabs = QTabWidget()
        # --- Guided
        g = QWidget()
        gl = QVBoxLayout(g)
        gl.setContentsMargins(4, 4, 4, 4)
        self.form_host = QWidget()
        self.form = QGridLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self.form_host)
        self.apply_btn = QPushButton("Preview and apply answers…")
        self.apply_btn.setToolTip("Shows every file change as one plan to approve; journalled and revertable")
        self.apply_btn.clicked.connect(self.apply_answers)
        gl.addWidget(self.apply_btn)
        self.actions_host = QVBoxLayout()
        gl.addLayout(self.actions_host)
        self.agent_out = QTextBrowser()
        self.agent_out.setOpenExternalLinks(False)
        self.agent_out.setMaximumHeight(160)
        self.agent_out.hide()
        gl.addWidget(self.agent_out)
        self.suggest_btn = QPushButton("Apply the agent's suggestion…")
        self.suggest_btn.clicked.connect(self.apply_agent_suggestion)
        self.suggest_btn.hide()
        gl.addWidget(self.suggest_btn)
        gl.addStretch(1)
        self.tabs.addTab(g, "Guided")
        # --- Advanced
        a = QWidget()
        self.adv_lay = QVBoxLayout(a)
        self.adv_lay.setContentsMargins(4, 4, 4, 4)
        self.tabs.addTab(a, "Advanced")
        # --- Raw
        r = QWidget()
        rl = QVBoxLayout(r)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.addWidget(QLabel("The exact files of this stage (double-click: open in the editor, raw text)"))
        self.raw = QListWidget()
        self.raw.itemDoubleClicked.connect(lambda it: self.open_raw(it.data(Qt.ItemDataRole.UserRole)))
        rl.addWidget(self.raw, 1)
        self.tabs.addTab(r, "Raw")
        pl.addWidget(self.tabs, 2)

        marks = QHBoxLayout()
        self.mark_btn = QPushButton("Mark stage done")
        self.mark_btn.clicked.connect(lambda: self.mark_done(True))
        self.unmark_btn = QPushButton("Unmark")
        self.unmark_btn.clicked.connect(lambda: self.mark_done(False))
        marks.addWidget(self.mark_btn, 1)
        marks.addWidget(self.unmark_btn)
        pl.addLayout(marks)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.page)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        lay.addWidget(split, 1)

        self._timer = QTimer(self, singleShot=True, interval=900, timeout=self.refresh)
        self.sv.case_changed.connect(lambda _s: self.refresh())
        self.sv.files_changed.connect(lambda _p: self._timer.start())
        self.refresh()

    # ------------------------------------------------------------------ state
    def _session(self):
        return self.sv.session

    def refresh(self) -> None:
        s = self._session()
        if s is None:
            self.stages = []
            self.tree.clear()
            self.tree.addTopLevelItem(QTreeWidgetItem(["Open a case to see its workflow"]))
            self.progress.setText("")
            self._show_stage(None)
            return
        if self._busy:
            self._timer.start()
            return
        self._busy = True
        root = s.root

        def work():
            from aethercfd.case.guided import assess

            return assess(root)

        self.sv.run_bg(work, self._assessed)

    def refresh_now(self) -> None:
        """Synchronous refresh (tests, automation)."""
        from aethercfd.case.guided import assess

        s = self._session()
        if s is not None:
            self._assessed((assess(s.root), None))

    def _assessed(self, res) -> None:
        self._busy = False
        stages, err = res if isinstance(res, tuple) else (res, None)
        keep = self.current
        self.tree.blockSignals(True)  # repopulating must not "select" the first row
        try:
            self._fill(stages, err)
        finally:
            self.tree.blockSignals(False)
        if not self.stages:
            return
        cur = next((s for s in self.stages if s.status not in ("done", "na") and not s.optional), None)
        self.select_stage(keep or (cur.id if cur else self.stages[0].id))

    def _fill(self, stages, err) -> None:
        self.tree.clear()
        if err is not None or stages is None:
            self.stages = []
            self.tree.addTopLevelItem(QTreeWidgetItem([f"Could not assess the case: {err}"]))
            return
        self.stages = stages
        for st in stages:
            ic, col, label = _STATUS.get(st.status, _STATUS["todo"])
            it = QTreeWidgetItem([f"{st.index:>2}  {st.title}", label])
            it.setIcon(0, icon(ic, col))
            it.setForeground(1, QColor(col))
            it.setToolTip(0, st.next_action)
            it.setToolTip(1, st.next_action)
            it.setData(0, Qt.ItemDataRole.UserRole, st.id)
            self.tree.addTopLevelItem(it)
        self.tree.resizeColumnToContents(0)
        done = sum(1 for s in stages if s.status in ("done", "na"))
        cur = next((s for s in stages if s.status not in ("done", "na") and not s.optional), None)
        self.progress.setText(f"{done}/{len(stages)} stages done" + (f" · next: {cur.title}" if cur else " · complete"))

    def select_stage(self, sid: str) -> None:
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == sid:
                self.tree.blockSignals(True)
                self.tree.setCurrentItem(it)
                self.tree.blockSignals(False)
                self._show_stage(self.stage(sid))
                return

    def stage(self, sid: str | None):
        return next((s for s in self.stages if s.id == sid), None)

    def _on_select(self, cur, _prev) -> None:
        if cur is None:
            return
        sid = cur.data(0, Qt.ItemDataRole.UserRole)
        if sid and sid != self.current:
            self._show_stage(self.stage(sid))

    # ------------------------------------------------------------------ the stage page
    def _show_stage(self, st) -> None:
        # a refresh (file watcher) re-renders the page: keep what the user has typed but not applied yet
        pending = self.answers() if st is not None and self.current == st.id and self._editors else {}
        self.current = st.id if st else None
        for w in (self.gates, self.raw):
            w.clear()
        _clear(self.form)
        _clear(self.actions_host)
        _clear(self.adv_lay)
        self._editors = {}
        self.agent_out.hide()
        self.suggest_btn.hide()
        if st is None:
            self.title.setText("")
            self.status.setText("")
            self.next.setText("")
            self.summary.setText("")
            self.deps.setText("")
            self.mark_btn.setEnabled(False)
            self.unmark_btn.setEnabled(False)
            return
        _ic, col, label = _STATUS.get(st.status, _STATUS["todo"])
        self.title.setText(f"{st.index}. {st.title}")
        self.status.setText(label)
        self.status.setStyleSheet(f"color: {col};")
        self.next.setText(f"<b>Next:</b> {_esc(st.next_action)}")
        self.summary.setText(st.summary)
        dep_txt = []
        for d in st.depends_on:
            ds = self.stage(d)
            if ds is not None:
                mark = "✓" if ds.status in ("done", "na") else ("✗" if ds.blocking else "…")
                dep_txt.append(f"{ds.title} {mark}")
        self.deps.setText(("Depends on: " + ", ".join(dep_txt)) if dep_txt else "No dependencies")
        # gates
        for gt in st.gates:
            if gt.ok:
                it = QTreeWidgetItem([f"✓ {gt.title}"])
                it.setIcon(0, icon("msc.pass", T.ok))
                it.setForeground(0, QColor(T.text_dim))
            else:
                ic2, c2 = _GATE.get(gt.severity, _GATE["info"])
                it = QTreeWidgetItem([gt.title + ("   [fix available]" if gt.edits else "")])
                it.setIcon(0, icon(ic2, c2))
                it.setForeground(0, QColor(c2))
            it.setData(0, Qt.ItemDataRole.UserRole, gt.id)
            for label2, text in (("Why", gt.why), ("How to fix", gt.how)):
                if text and not gt.ok:
                    ch = QTreeWidgetItem([f"{label2}: {text}"])
                    ch.setToolTip(0, text)
                    it.addChild(ch)
            for ev in gt.evidence[:8]:
                if ev:
                    ch = QTreeWidgetItem([ev])
                    ch.setForeground(0, QColor(T.text_dim))
                    it.addChild(ch)
            self.gates.addTopLevelItem(it)
            if not gt.ok and gt.severity in ("error", "warn"):
                it.setExpanded(True)
        self.fix_btn.setEnabled(False)
        # guided questions
        for row, q in enumerate(st.questions):
            lab = QLabel(q.text)
            lab.setWordWrap(True)
            lab.setToolTip(q.help)
            ed = self._editor(q)
            self._editors[q.id] = ed
            unit = QLabel(q.unit)
            unit.setStyleSheet(f"color: {T.text_dim};")
            self.form.addWidget(lab, row * 2, 0)
            self.form.addWidget(ed, row * 2, 1)
            self.form.addWidget(unit, row * 2, 2)
            where = ("writes " + ", ".join(w for w in q.writes if w)) if any(q.writes) else \
                "kept in the workflow state"
            info = " · ".join(x for x in (q.derived, where) if x)
            hint = QLabel(info)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {T.text_faint}; font-size: 8pt;")
            self.form.addWidget(hint, row * 2 + 1, 0, 1, 3)
        for qid, v in pending.items():
            if qid in self._editors:
                with contextlib.suppress(ValueError):
                    self.set_answer(qid, v)
        self.form.setColumnStretch(0, 3)
        self.form.setColumnStretch(1, 2)
        self.apply_btn.setVisible(bool(st.questions))
        # actions (runs, agents, panels)
        for act in st.actions:
            b = QPushButton(act.label)
            b.setToolTip(act.help)
            b.setProperty("action", act.id)
            b.clicked.connect(lambda _=False, aid=act.id: self.run_action(aid))
            self.actions_host.addWidget(b)
        # advanced
        if st.advanced:
            self.adv_lay.addWidget(QLabel("All OpenFOAM objects and settings of this stage, in the expert tools:"))
        for pid, label3 in st.advanced:
            b = QPushButton(label3)
            b.clicked.connect(lambda _=False, p=pid: self._open_panel(p))
            self.adv_lay.addWidget(b)
        self.adv_lay.addStretch(1)
        # raw
        s = self._session()
        for f in self._raw_files(st):
            rel = str(Path(f).relative_to(s.root)) if s and Path(f).is_relative_to(s.root) else str(f)
            it2 = QListWidgetItem(icon("msc.file"), rel)
            it2.setData(Qt.ItemDataRole.UserRole, str(f))
            self.raw.addItem(it2)
        self.mark_btn.setEnabled(st.can_mark_done and st.status != "done")
        tip = "" if st.can_mark_done else ("; ".join(g.title for g in st.blocking) or
                                           "an earlier stage has errors" if st.status != "na" else "not needed")
        self.mark_btn.setToolTip(tip or ("Accept the stage (warnings are recorded as accepted)" if st.warnings
                                         else "All gates pass"))
        self.unmark_btn.setEnabled(st.marked_done)

    def _raw_files(self, st) -> list[Path]:
        out = []
        for f in st.files:
            p = Path(f)
            if p.is_file():
                out.append(p)
            elif p.is_dir() and p.name != "polyMesh":
                out += sorted(x for x in p.iterdir() if x.is_file())[:60]
            elif p.is_dir():
                out += [p / "boundary"] if (p / "boundary").is_file() else []
        return out

    def _editor(self, q) -> QWidget:
        if q.kind == "choice":
            w = QComboBox()
            for v, lab in q.choices:
                w.addItem(lab, v)
            i = w.findData(q.value)
            w.setCurrentIndex(max(i, 0))
            w.setProperty("initial", w.currentData())
        elif q.kind == "bool":
            w = QComboBox()
            w.addItem("Yes", True)
            w.addItem("No", False)
            w.setCurrentIndex(0 if q.value else 1)
            w.setProperty("initial", bool(q.value))
        else:
            w = QLineEdit("" if q.value is None else f"{q.value:g}" if isinstance(q.value, float) else str(q.value))
            w.setPlaceholderText("value")
            w.setProperty("initial", w.text())
        w.setToolTip(q.help)
        return w

    def set_answer(self, qid: str, value) -> None:
        """Automation/tests: put a value into a question's editor."""
        w = self._editors[qid]
        if isinstance(w, QComboBox):
            i = w.findData(value)
            if i < 0:
                raise ValueError(f"{value!r} is not a choice of {qid}")
            w.setCurrentIndex(i)
        else:
            w.setText(f"{value:g}" if isinstance(value, float) else str(value))

    def answers(self, *, changed_only: bool = True) -> dict:
        out = {}
        for qid, w in self._editors.items():
            if isinstance(w, QComboBox):
                v = w.currentData()
                if changed_only and v == w.property("initial"):
                    continue
                out[qid] = v
            else:
                t = w.text().strip()
                if not t or (changed_only and t == w.property("initial")):
                    continue
                out[qid] = t
        return out

    # ------------------------------------------------------------------ changes
    def _approve(self, plan, who: str = "AetherCFD guided workflow") -> bool:
        from aethercfd.studio.widgets.approval import ApprovalDialog

        return ApprovalDialog(plan, self, who=who).exec() == QDialog.DialogCode.Accepted

    def _batch(self, params: dict, who: str = "AetherCFD guided workflow") -> bool:
        from aethercfd.commands.base import USER

        s = self._session()
        if s is None or not self.sv.ensure_project():
            return False
        try:
            plan = s.executor.plan("dict.batch", params, USER)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot plan the change", str(exc))
            return False
        if plan.blocked:
            QMessageBox.warning(self, "Cannot apply", "\n".join(c.message for c in plan.blocked))
            return False
        if not plan.file_changes:
            self.sv.notify("Nothing to change")
            return True
        if not self._approve(plan, who):
            return False
        res = s.executor.execute("dict.batch", params, USER, plan=plan, approved_hash=plan.plan_hash)
        self.sv.notify("Applied (see the Command Journal)" if res.ok else f"Not applied: {res.message}",
                       "info" if res.ok else "error")
        self.refresh_now()
        return res.ok

    def apply_answers(self) -> bool:
        from aethercfd.case.guided import load_state, plan_answers

        s = self._session()
        if s is None or self.current is None:
            return False
        if not self.sv.ensure_project():
            return False
        ans = self.answers()
        if not ans:
            self.sv.notify("No answer changed")
            return False
        state = load_state(s.root)
        try:
            gp = plan_answers(s.root, self.current, ans, state)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot use these answers", str(exc))
            return False
        if gp.errors:
            QMessageBox.information(self, "Some answers were not applied", "\n".join(gp.errors))
        if gp.empty:
            return False
        return self._batch(gp.batch_params(s.root, state))

    def _selected_gate_fix(self):
        it = self.gates.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        st = self.stage(self.current)
        if it is None or st is None:
            return None
        g = next((g for g in st.gates if g.id == it.data(0, Qt.ItemDataRole.UserRole)), None)
        return g if g is not None and not g.ok and g.edits else None

    def _gate_clicked(self, item, _col) -> None:
        if self._selected_gate_fix() is not None:
            self.apply_fix()

    def apply_fix(self, gate_id: str | None = None) -> bool:
        st = self.stage(self.current)
        s = self._session()
        if st is None or s is None:
            return False
        g = next((g for g in st.gates if g.id == gate_id), None) if gate_id else self._selected_gate_fix()
        if g is None or not g.edits:
            return False
        edits = [{"file": str(s.root / e.file), "key": e.key, "value": e.value,
                  "insert_if_missing": e.insert_if_missing} for e in g.edits]
        return self._batch({"edits": edits, "summary": f"{g.fix_label} ({g.title})"})

    def mark_done(self, done: bool = True) -> bool:
        from aethercfd.case.guided import mark_params

        s = self._session()
        if s is None or self.current is None or not self.sv.ensure_project():
            return False
        params, refusal = mark_params(s.root, self.current, done, by="user")
        if refusal:
            QMessageBox.information(self, "Not yet", refusal)
            return False
        from aethercfd.commands.base import USER

        res = s.executor.execute("dict.batch", params, USER)
        if not res.ok:
            self.sv.notify(f"Could not mark the stage: {res.message}", "error")
            return False
        cur = self.current
        self.refresh_now()
        if done:  # move on to the next unfinished stage
            nxt = next((x for x in self.stages if x.index > (self.stage(cur).index if self.stage(cur) else 0)
                        and x.status not in ("done", "na")), None)
            if nxt is not None:
                self.select_stage(nxt.id)
        return True

    # ------------------------------------------------------------------ actions
    def run_action(self, aid: str) -> None:
        if aid in _RUN_PRESET:
            self.start_run(_RUN_PRESET[aid])
        elif aid.startswith("agent."):
            self.run_agent(aid.split(".", 1)[1])
        elif aid.startswith("open."):
            self._open_panel(aid.split(".", 1)[1])

    def start_run(self, preset: str) -> bool:
        from aethercfd.commands.base import USER
        from aethercfd.studio.panels.run.panels import PlanDialog

        s = self._session()
        if s is None or not self.sv.ensure_project():
            return False
        params = {"case": str(s.root), "preset": preset}
        try:
            plan = s.executor.plan("run.start", params, USER)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot plan run", str(exc))
            return False
        if plan.blocked:
            QMessageBox.warning(self, "Cannot run yet", "\n".join(c.message for c in plan.blocked))
            return False
        if PlanDialog(plan, self).exec() != QDialog.DialogCode.Accepted:
            return False
        res = s.executor.execute("run.start", params, USER, plan=plan, approved_hash=plan.plan_hash)
        self.sv.notify(res.message, "info" if res.ok else "error")
        if res.ok:
            self._open_panel("aether.run_status")
        return res.ok

    def run_agent(self, agent: str, **kw) -> None:
        """Run a cfd_agent in the background; the result appears under the actions."""
        s = self._session()
        if s is None:
            return
        root = s.root
        self.agent_out.show()
        self.agent_out.setHtml(f"<i>Asking {agent.capitalize()} (cfd_agents, out of process)…</i>")
        self.suggest_btn.hide()

        def work():
            from aethercfd.case import gates as G
            from aethercfd.case.model import load_case
            from aethercfd.services.cfd_agents import CfdAgents

            a = CfdAgents.from_settings()
            case = load_case(root)
            if agent == "murphy":
                log = G.latest_solver_log(root)
                if log is None:
                    from aethercfd.services.cfd_agents import AgentResult

                    return AgentResult("murphy", False, explanation="No solver log yet: run the case first.")
                return a.murphy(log)
            if agent == "connie":
                return a.connie(root)
            if agent == "reggie":
                _g, no = G.mesh_quality_gate(case)
                dt = G._dict_value(case, "system/controlDict", "deltaT")
                from aethercfd.services.cfd_agents import AgentResult

                if no is None or not dt:
                    return AgentResult("reggie", False, explanation="Reggie needs the mesh non-orthogonality (run "
                                                                    "checkMesh) and deltaT in controlDict.")
                return a.reggie(no, float(dt), kw.get("target_courant_max", 0.5), case_root=root)
            if agent == "darwin":
                from aethercfd.services.cfd_agents import AgentResult

                if "diameter_m" not in kw:
                    return AgentResult("darwin", False, explanation=(
                        "Darwin plans shear-layer refinement for jets from a converged RANS result on an all-tet "
                        "mesh; it needs the jet diameter and velocity jump. Ask the copilot (cfd_agent_run "
                        "darwin) with those numbers."))
                return a.darwin(root, kw.get("time") or case.latest_result_time, **kw)
            raise ValueError(agent)

        self.sv.run_bg(work, self._agent_done)

    def _agent_done(self, res) -> None:
        r, err = res if isinstance(res, tuple) else (res, None)
        if err is not None or r is None:
            self.agent_out.setHtml(f"<span style='color:{T.error}'>Agent failed: {_esc(str(err))}</span>")
            return
        d = r.as_dict()
        self._last_agent = d
        col = T.ok if r.ok else T.warn
        html = f"<p style='color:{col}'>{_esc(r.explanation or r.error)}</p>"
        for sg in r.suggestions[:6]:
            html += f"<p>• {_esc(sg.get('text', ''))}</p>"
        html += f"<p style='color:{T.text_faint}'>Advice from cfd_agents: nothing was changed.</p>"
        self.agent_out.setHtml(html)
        self.suggest_btn.setVisible(any(sg.get("edits") for sg in r.suggestions))

    def apply_agent_suggestion(self) -> bool:
        s = self._session()
        d = self._last_agent
        if s is None or not d:
            return False
        sg = next((x for x in d.get("suggestions", []) if x.get("edits")), None)
        if sg is None:
            return False
        edits = [{**e, "file": str(s.root / e["file"])} for e in sg["edits"]]
        return self._batch({"edits": edits, "summary": f"{d['agent'].capitalize()}: {sg['text']}"},
                           who=f"cfd_agents {d['agent'].capitalize()} (suggestion)")

    def _open_panel(self, pid: str) -> None:
        spec = self.sv.panels.get(pid)
        if spec is not None and self.sv.host is not None:
            self.sv.host.open_panel(spec)

    def open_raw(self, path: str) -> None:
        spec = self.sv.panels.get("aether.editor")
        if spec is None or self.sv.host is None:
            return
        d = self.sv.host.open_panel(spec, args={"path": path})
        panel = self.sv.host.panel(d.objectName())
        if panel is not None and hasattr(panel, "mode_group"):
            panel.mode_group.button(0).setChecked(True)  # raw text
            panel._set_mode(0)

    # ------------------------------------------------------------------ persistence
    def save_state(self) -> dict:
        return {"stage": self.current, "tab": self.tabs.currentIndex()}

    def restore_state(self, state: dict) -> None:
        if state.get("stage"):
            self.current = state["stage"]
            self.select_stage(self.current)
        self.tabs.setCurrentIndex(int(state.get("tab", 0) or 0))


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PANELS = [PanelSpec("aether.workflow", "Workflow", "msc.list-ordered", "Case", WorkflowPanel, default_area=Area.LEFT,
                    tab_group="nav", description="Guided, staged setup: 12 stages with gates, questions, next actions")]
