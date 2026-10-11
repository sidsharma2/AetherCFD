"""--sweep: a functional robustness pass over the whole studio on one case.

Opens every registered panel and drives its main actions (non-destructive: nothing is run in WSL, edits go
only to files the sweep creates), waits for the asynchronous results, and records per step: ok / error /
seconds / detail, plus every exception that reached sys.excepthook while the step ran and every
notification the app showed. Writes <out>/sweep.json and a screenshot per stage; exit code 1 if any step
failed.

    AetherCFD.exe <case> --sweep --smoke-out build/sweep
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication

from aethercfd.studio.smoke import _pump


def _fail(msg: str):
    raise AssertionError(msg[-400:] or "returned nothing")


def sx_axis(vp) -> str:
    """The axis a cut-away of this domain should be normal to: the thinnest extent shows the most inside."""
    from aethercfd.results.sections import thinnest_axis

    return thinnest_axis(vp.bounds) if vp.bounds is not None else "x"


class Sweep:
    def __init__(self, app, win, out: Path):
        self.app, self.win, self.sv, self.out = app, win, win.services, out
        self.steps: list[dict] = []
        self._errors: list[str] = []
        self._notes: list[str] = []
        prev = sys.excepthook

        def hook(t, v, tb):
            self._errors.append("".join(traceback.format_exception(t, v, tb))[-3000:])
            prev(t, v, tb)

        sys.excepthook = hook
        self.sv.notification.connect(lambda msg, level: self._notes.append(f"[{level}] {msg}"))

    def wait(self, cond, timeout: float = 60.0) -> bool:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            _pump(self.app, 0.2)
            try:
                if cond():
                    return True
            except Exception:
                pass
        return False

    def step(self, name: str, fn, *, timeout: float = 60.0, until=None, detail=None) -> object:
        e0, n0 = len(self._errors), len(self._notes)
        t0 = time.perf_counter()
        rec = {"step": name}
        result = None
        try:
            result = fn()
            if until is not None and not self.wait(until, timeout):
                rec["error"] = f"timed out after {timeout:.0f} s"
            _pump(self.app, 0.3)
            if detail is not None:
                result = str(detail())
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["traceback"] = traceback.format_exc()[-2500:]
        rec["seconds"] = round(time.perf_counter() - t0, 2)
        if len(self._errors) > e0:
            rec.setdefault("error", "exception in a Qt slot / callback")
            rec["slot_exceptions"] = self._errors[e0:]
        notes = self._notes[n0:]
        if notes:
            rec["notifications"] = notes[-6:]
            bad = [n for n in notes if n.startswith("[error]")]
            if bad and "error" not in rec:
                rec["error"] = bad[0]
        if isinstance(result, str):
            rec["detail"] = result[-600:]
        rec["ok"] = "error" not in rec
        self.steps.append(rec)
        print(("OK  " if rec["ok"] else "FAIL") + f" {name} ({rec['seconds']} s) {rec.get('error', '')}", flush=True)
        return result

    def panel(self, type_id: str, instance: str | None = None):
        spec = self.sv.panels.get(type_id)
        d = self.sv.host.open_panel(spec, instance=instance)
        p = self.sv.host.build_panel(d.objectName())
        _pump(self.app, 0.3)
        return p

    def shot(self, name: str) -> None:
        with contextlib.suppress(Exception):
            self.win.grab().save(str(self.out / f"sweep_{name}.png"))


def run_sweep(app: QApplication, win, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    sw = Sweep(app, win, out)
    sv = sw.sv
    _pump(app, 2.0)
    s = sv.session
    has_case = s is not None
    root = Path(s.root) if has_case else None
    from aethercfd.case.model import load_case

    case = load_case(root) if has_case else None
    times = [t for _, t in (case.times + case.processor_times)] if case else []
    has_results = any(float(t) > 0 for t in times)
    latest = max(times, key=float) if times else None

    # ---------------------------------------------------------------- the restored layout is not an empty window
    def placed():
        names = win.layouts.open_panel_names()
        lost = [n for n in names if sv.host.dock(n) is not None and sv.host.dock(n).dockAreaWidget() is None]
        if not names or lost:
            raise AssertionError(f"panels open but in no dock area (empty window): {lost or 'none open'}")
        return f"{len(names)} panels placed"

    sw.step("restored layout shows its panels", placed)

    # ---------------------------------------------------------------- every panel opens and builds
    for spec in list(sv.panels.specs()):
        sw.step(f"open panel {spec.type_id}", lambda spec=spec: (sw.panel(spec.type_id), "built")[1])
    sw.shot("all_panels")

    # ---------------------------------------------------------------- perspectives
    from aethercfd.studio.layout.recipes import PERSPECTIVES

    for p in PERSPECTIVES:
        sw.step(f"perspective {p.id}", lambda p=p: win.layouts.switch(p.id) or p.id)
        sw.shot(f"persp_{p.id}")
    win.layouts.switch("setup")

    # ---------------------------------------------------------------- start page, palette, case files
    w = sw.panel("aether.welcome")
    sw.step("start page shows the open case", lambda: (None if not has_case or not w.card.isHidden()
                                                       else (_ for _ in ()).throw(AssertionError("no case card")),
                                                       w.card_info.text() if has_case else "no case")[1])
    from aethercfd.studio.palette.command_palette import (
        CommandPalette,  # noqa: F401 -- import check
    )

    sw.step("palette search with typos", lambda: f"{len(win.palette.rank('chek mesh', win.palette._collect()))} hits")
    if has_case:
        ct = sw.panel("aether.case_tree")
        sw.step("case files refresh", lambda: ct.refresh() or f"{ct.model.rowCount()} rows")

        # ------------------------------------------------------------ editor + inspector
        ed = sw.panel("aether.editor")
        sw.step("editor opens controlDict", lambda: ed.open(root / "system" / "controlDict") or "opened")
        ins = sw.panel("aether.inspector")
        sw.step("inspector shows a file", lambda: ins.show({"kind": "file", "path": str(root / "system" / "fvSolution")})
                or "shown")

        # ------------------------------------------------------------ checks
        ch = sw.panel("aether.checks")
        sw.step("pre-run checks", lambda: ch.run() or "started", timeout=180,
                until=lambda: ch.summary.text() not in ("Checking…", ""), detail=lambda: ch.summary.text())

        # ------------------------------------------------------------ journal, diff
        jn = sw.panel("aether.journal")
        sw.step("journal reload", lambda: jn.reload() or "reloaded")
        df = sw.panel("aether.diff")
        sw.step("diff two dictionaries", lambda: df.compare_paths(root / "system" / "controlDict",
                                                                  root / "system" / "fvSolution"))
        sw.step("diff before last change", lambda: df.before_last_change(root / "system" / "controlDict"))

        # ------------------------------------------------------------ python console
        pc = sw.panel("aether.python")
        n0 = len(pc.out.toPlainText())
        sw.step("python console: times + a field", lambda: pc.run_code(
            "t = data.times(); print('times', t[-3:]); f = data.fields(names=['p'] if 'p' in data.field_names() else None); "
            "print('fields', sorted(f)[:6])") and "submitted", timeout=300,
            until=lambda: len(pc.out.toPlainText()) > n0 + 20 and not pc._busy,
            detail=lambda: pc.out.toPlainText()[n0:])
        sw.step("python console: error stays in the console", lambda: pc.run_code("1/0") and "submitted", timeout=30,
                until=lambda: "ZeroDivisionError" in pc.out.toPlainText() and not pc._busy,
                detail=lambda: pc.out.toPlainText()[-200:])

        # ------------------------------------------------------------ AetherMesh
        am = sw.panel("aether.aethermesh")
        sw.step("aethermesh grading design", lambda: am.design() or am.g_out.toPlainText()[:120])
        sw.step("aethermesh wall spacing", lambda: am.wall() or am.w_out.toPlainText()[:120])
        sw.step("aethermesh hole blocking", lambda: am.holes() or am.h_out.toPlainText()[:120])
        sw.step("aethermesh mesh quality (checkMesh-exact)", lambda: am.analyse_quality() or "started", timeout=600,
                until=lambda: am.q_tree.topLevelItemCount() > 0 or "failed" in am.status.text().lower(),
                detail=lambda: f"{am.q_tree.topLevelItemCount()} rows; {am.status.text()}")

        # ------------------------------------------------------------ combustion, code
        cb = sw.panel("aether.combustion")
        sw.step("combustion mixture (Cantera)", lambda: (cb.preset.setCurrentText("Sandia flame D (CH4/air 1:3 vol.)"),
                                                          cb.compute_mixture(), "started")[2], timeout=120,
                until=lambda: "mixture" in cb.report, detail=lambda: str(cb.report.get("mixture"))[:300])
        sw.panel("aether.code")

        # ------------------------------------------------------------ viewport + visualization
        vp = sw.panel("aether.viewport")
        if has_results:
            sw.step("viewport loads results", lambda: "loading", timeout=300,
                    until=lambda: vp.field.count() > 0 and vp.range is not None,
                    detail=lambda: f"{vp.field.count()} fields; {vp.info.text()}")
            fld = next((f for f in ("T", "p", "U") if f in [vp.field.itemText(i) for i in range(vp.field.count())]),
                       vp.field.itemText(0))
            sw.step(f"viewport shows {fld} with a slice", lambda: vp.show_field(fld, latest, "z"), timeout=300,
                    until=lambda: vp.scalar is not None, detail=lambda: f"range {vp.range}; {vp.info.text()}")
            vis = sw.panel("aether.vis_controls")
            sw.step("vis: add slice + Q-criterion", lambda: (vis.add("slice"), vis.add("qcriterion"), "added")[2],
                    timeout=600, until=lambda: all(f"stage:{st['id']}" in vp.stage_info for st in vp.stages),
                    detail=lambda: str(vp.stage_info))
            sw.shot("viewport")
            from aethercfd.studio.app.automation import AppAutomation

            fields = [vp.field.itemText(i) for i in range(vp.field.count())]
            pinn = "TPinn" in fields and "TPinnErr" in fields
            args = ("T", "TPinn", "TPinnErr", "600") if pinn else (fld, fld, "", latest)
            sw.step(f"compare view {args[:3]}", lambda: AppAutomation(sv).compare_fields(*args), timeout=400,
                    until=lambda: all(sv.host.build_panel(f"aether.viewport#cmp{x}").lock.isChecked()
                                      for x in ("AB" + ("C" if args[2] else ""))))
            sw.shot("compare")

            # -------------------------------------------------------- post-processing looks (AI path: AppAutomation)
            auto = AppAutomation(sv)
            sv.set_active_viewport(vp)  # the tools act on the viewport the user looks at: make it the main one
            vp.slice_on.setChecked(False)
            for lname in ("classic", "studio"):
                sw.step(f"viewport look {lname}", lambda n=lname: auto.viewport_look(n), timeout=300,
                        until=lambda: getattr(vp, "drawn_gen", -1) == vp.gen,
                        detail=lambda: f"{vp.look}; bar '{vp.scalar_bar.GetTitle()}'; {vp.info.text()[-120:]}")
                sw.shot(f"look_{lname}")
            sw.step("viewport showcase", auto.showcase, timeout=400,
                    until=lambda: getattr(vp, "drawn_gen", -1) == vp.gen and not getattr(vp, "_showcase_pending", 0),
                    detail=lambda: f"slice={vp.slice_on.isChecked()} axis={vp.slice_axis.currentText()}; "
                                   f"{vp.info.text()[-100:]}")
            sw.shot("showcase")

            # -------------------------------------------------------- Smooth / Cells, sections, views, flow paths
            def drawn():
                return getattr(vp, "drawn_gen", -1) == vp.gen

            def stage_ok(prefix: str):
                hits = {k: i for k, i in vp.stage_info.items() if k.startswith(prefix) and "@" not in k}
                if not hits or not all(i.get("cells") for i in hits.values()):
                    _fail(f"{prefix}: {hits or vp.stage_info}")
                return "; ".join(f"{i['label']} ({i['cells']:,} cells) {' '.join(i.get('notes', []))[:120]}"
                                 for i in hits.values())

            sw.step("sections: three orthogonal planes with contours", lambda: auto.section(
                "ortho3", show={"contours": True}), timeout=400, until=drawn, detail=lambda: stage_ok("stage:sec"))
            sw.shot("sections_smooth")
            sw.step("smoothing: cells with mesh edges", lambda: auto.viewport_smoothing("cells", True), timeout=400,
                    until=drawn, detail=lambda: f"{vp.smoothing}; bar '{vp.scalar_bar.GetTitle()}'; "
                                                f"{stage_ok('stage:sec')}")
            sw.shot("sections_cells")
            sw.step("smoothing: back to the look's default", lambda: auto.viewport_smoothing("auto"), timeout=400,
                    until=drawn, detail=lambda: vp.smoothing if vp.smoothing == "smooth" else _fail(vp.smoothing))
            sw.step("sections: slice stack", lambda: auto.section("stack", n=4), timeout=400, until=drawn,
                    detail=lambda: stage_ok("stage:sec"))
            sw.step("sections: a stack is not dragged", lambda: vp.drag_section(True) if vp.drag_section(
                True).startswith("a slice stack") else _fail("a stack got drag handles"))
            sw.step("sections: plane by point + normal", lambda: auto.section(
                "plane", origin=[(vp.bounds[0] + vp.bounds[1]) / 2, (vp.bounds[2] + vp.bounds[3]) / 2,
                                 (vp.bounds[4] + vp.bounds[5]) / 2], normal=[1, 1, 0], show={"arrows": True}),
                    timeout=400, until=drawn, detail=lambda: stage_ok("stage:sec"))
            sw.step("sections: drag handles", lambda: vp.drag_section(True), timeout=30,
                    detail=lambda: "handles on" if vp._plane_widget is not None else _fail("no plane widget"))
            vp.drag_section(False)
            sw.step("sections: open in 2D", vp.open_section_2d, timeout=400,
                    until=lambda: "stage:sec2d" in getattr(sv.host.build_panel("aether.viewport#sec2d"), "stage_info",
                                                           {}),
                    detail=lambda: str(sv.host.build_panel("aether.viewport#sec2d").stage_info.get("stage:sec2d")))
            sv.host.close_panel("aether.viewport#sec2d")
            sv.set_active_viewport(vp)
            b = vp.bounds
            sw.step("line probe through the domain", lambda: auto.line_probe(
                [b[0], (b[2] + b[3]) / 2, (b[4] + b[5]) / 2], [b[1], (b[2] + b[3]) / 2, (b[4] + b[5]) / 2], None, 200),
                timeout=300, until=lambda: vp.last_line is not None,
                detail=lambda: (f"{vp.last_line['inside']} of {vp.last_line['n']} samples inside, "
                                f"{vp.last_line['association']}") if vp.last_line.get("inside") else
                _fail(str(vp.last_line)))
            with contextlib.suppress(Exception):
                vp._line_dialog.close()
            sw.step("cut-away in a glass shell", lambda: auto.cutaway(sx_axis(vp)), timeout=400, until=drawn,
                    detail=lambda: stage_ok("stage:cut"))
            sw.shot("cutaway")
            sw.step("remove sections", lambda: auto.section("mid-z", clear=True), timeout=400, until=drawn)
            inlets = vp.inlet_paths()
            sw.step(f"flow paths {'from the inlets' if inlets else 'from a plane'}", lambda: auto.flow_paths(
                "inlets" if inlets else "plane", color_by="residence_time"), timeout=600, until=drawn,
                detail=lambda: stage_ok("stage:flow") + f"; bar '{vp.scalar_bar.GetTitle()}'")
            sw.shot("flow_paths")
            sw.step("pathlines through the saved times", lambda: vp.set_pathlines(True), timeout=900, until=drawn,
                    detail=lambda: stage_ok("stage:flow"))
            sw.step("remove flow paths", lambda: auto.flow_paths(clear=True), timeout=400, until=drawn)
            for view in ("front", "top", "right", "iso"):
                sw.step(f"standard view {view}", lambda v=view: auto.camera(v))
            sw.step("save a view", lambda: auto.bookmark("save", "sweep view"))
            sw.step("restore a saved view", lambda: auto.camera("sweep view"))
            sw.step("render every saved view", lambda: auto.bookmark("render", "") if isinstance(
                auto.bookmark("render", ""), list) else _fail("no renders"), timeout=300)
            sw.step("delete a saved view", lambda: auto.bookmark("delete", "sweep view"))
            sw.step("quad view on", lambda: auto.quad(True), detail=lambda: f"{len(vp._quad or [])} extra views")
            sw.shot("quad_view")
            sw.step("quad view off", lambda: auto.quad(False))
            gif = sw.out / "sweep_turntable.gif"
            sw.step("turntable animation (GIF)", lambda: vp.record_animation("turntable", str(gif), frames=12, fps=12)
                    if Path(vp.record_animation("turntable", str(gif), frames=12, fps=12)).is_file() else
                    _fail("no GIF written"), timeout=120)
            rv = sw.panel("aether.run_status")
            sw.step("run panel says what Start will do", lambda: (rv.preset.setCurrentIndex(0), rv._show_preset_hint(),
                                                                   rv.preset_hint.text() or _fail("no hint"))[2])
            sw.step("compare reports a missing field", lambda: AppAutomation(sv).compare_fields(fld, "noSuchField")
                    if AppAutomation(sv).compare_fields(fld, "noSuchField").startswith("not written") else
                    (_ for _ in ()).throw(AssertionError("missing field not reported")))

            # -------------------------------------------------------- analysis: modes, surrogate, PINN status
            from aethercfd.results.casedata import CaseData

            an = sw.panel("aether.analysis")
            regions = CaseData(root).regions()
            region = CaseData(root).default_region()  # multi-region (CHT): the fluid region; "" otherwise
            if regions:
                def picker(p):
                    got = [p.region.combo.itemText(i) for i in range(p.region.combo.count())]
                    if p.region.isHidden() or got != regions or p.region.region() != region:
                        _fail(f"region picker: hidden={p.region.isHidden()} items={got} current={p.region.region()!r}"
                              f" (expected {regions}, {region!r})")
                    return f"regions {got}; on {region!r}"

                sw.step("analysis: region picker opens on the fluid region",
                        lambda: picker(an) + f"; fields {[an.m_field.itemText(i) for i in range(an.m_field.count())]}")
            if any(an.m_field.itemText(i) == "p" for i in range(an.m_field.count())):
                an.m_field.setCurrentText("p")

            def pod_done():
                text = an.m_out.toPlainText()
                if not text.startswith("POD of") or (region and f"(region {region})" not in text):
                    _fail(text)
                return text[:300]

            sw.step("analysis: POD of saved times", lambda: an.analyse() or _fail(an.m_out.toPlainText()), timeout=900,
                    until=lambda: not an.m_busy, detail=pod_done)
            an.m_method.setCurrentText("DMD")
            sw.step("analysis: DMD of saved times", lambda: an.analyse() or _fail(an.m_out.toPlainText()), timeout=900,
                    until=lambda: not an.m_busy,
                    detail=lambda: an.m_out.toPlainText()[:300])
            sw.step("analysis: GP surrogate", lambda: (an.fit_surrogate() or _fail(an.s_out.toPlainText())) and an.s_out.toPlainText()[:300])
            sw.step("analysis: PINN environment found", lambda: "checking" if "checking" in an.p_status.text() else
                    an.p_status.text(), timeout=120, until=lambda: "checking" not in an.p_status.text(),
                    detail=lambda: an.p_status.text())

            # -------------------------------------------------------- measurements (synthetic probe file)
            ms = sw.panel("aether.measurements")
            if regions:
                sw.step("measurements: region picker opens on the fluid region", lambda: picker(ms))

            def make_csv():
                import numpy as np

                d = CaseData(root, region)
                f0 = "T" if "T" in d.field_names() else "p"
                t = float(latest)
                c, v = d.centres(t), d.fields(t, [f0])[f0]
                v = v if v.ndim == 1 else np.linalg.norm(v, axis=1)
                idx = np.linspace(0, len(c) - 1, 12).astype(int)
                csv = sw.out / "sweep_measurements.csv"
                csv.write_text("x,y,z," + f0 + "\n" + "\n".join(f"{c[i,0]},{c[i,1]},{c[i,2]},{v[i] * 1.02}" for i in idx))
                ms.csv.setText(str(csv))
                ms.load()
                return f0

            f0 = sw.step("measurements: load CSV", make_csv, timeout=300) or "p"

            def on_region(text: str) -> str:
                # a multi-region case is read on the chosen (fluid) region and says so
                return text[:300] if not region or f"(region {region})" in text else _fail(text)

            def meas_compare():
                rep = ms.compare()
                if rep is None or not rep["fields"].get(f0, {}).get("n"):
                    _fail(ms.out.toPlainText())
                return on_region(ms.out.toPlainText())

            sw.step("measurements: compare", meas_compare, timeout=300)
            sw.step("measurements: assimilate", lambda: (ms.assimilate() or _fail(ms.out.toPlainText()))
                    and on_region(ms.out.toPlainText()), timeout=300)

            # -------------------------------------------------------- PINN: a tiny training run (nothing written)
            if "available" not in an.p_status.text() and "PyTorch " in an.p_status.text():
                an._pinn_fields()
                an.p_field.setCurrentText("T" if "T" in [an.p_field.itemText(i) for i in range(an.p_field.count())] else "p")
                an.p_phys.setCurrentIndex(max(0, an.p_phys.findData("data")))
                an.p_epochs.setValue(200)
                an.p_lbfgs.setValue(0)
                an.p_ndata.setValue(60)
                sw.step("analysis: tiny PINN (data only)", an.train_pinn, timeout=600,
                        until=lambda: an.p_go.isEnabled() and an.p_timer.isActive() is False
                        and any(k in an.p_out.toPlainText() for k in ("done on", "rror")),
                        detail=lambda: an.p_out.toPlainText()[:300])
                job = an.p_job[0] if an.p_job else None
                if "done on" not in an.p_out.toPlainText() or job is None or job.params.get("region", "") != region:
                    sw.steps[-1].update(ok=False, error=f"{an.p_out.toPlainText()[:400]} (job region "
                                                        f"{job.params.get('region') if job else None!r}, "
                                                        f"expected {region!r})")
                elif region:
                    sw.steps[-1]["detail"] = f"region {region}: {sw.steps[-1].get('detail', '')}"[:600]

            # -------------------------------------------------------- edge cases: bad input must give a message
            def surrogate(table):
                an.s_table.setPlainText(table)
                an.fit_surrogate()
                return an.s_out.toPlainText()[:200]

            for label, table in [("duplicate rows", "x, y\n1, 2\n1, 2\n1, 2"), ("constant output", "1, 5\n2, 5\n3, 5"),
                                 ("NaN in table", "1, nan\n2, 3\n3, 4"), ("one column", "1\n2\n3"),
                                 ("text only", "a, b\nc, d"), ("two identical inputs, different outputs",
                                                               "1, 2\n1, 3")]:
                sw.step(f"edge: surrogate with {label}", lambda t=table: surrogate(t))
            for label, body in [("missing file", None), ("no coordinate columns", "a,b\n1,2\n"),
                                ("points outside the mesh", f"x,y,z,{f0}\n1e6,1e6,1e6,300\n-1e6,-1e6,-1e6,310\n"),
                                ("empty file", ""), ("semicolon separated", "x;y;z;T\n0;0;0;300\n")]:
                def bad_csv(body=body, label=label):
                    path = sw.out / f"sweep_bad_{label.replace(' ', '_')}.csv"
                    if body is not None:
                        path.write_text(body)
                    ms.csv.setText(str(path))
                    ms.load()
                    if ms.meas is not None and ms.field.count():
                        ms.compare()
                        ms.assimilate()
                    return ms.out.toPlainText()[:200]

                ms.meas = None
                sw.step(f"edge: measurements with {label}", bad_csv)
            sw.step("edge: compare view at a time that does not exist",
                    lambda: AppAutomation(sv).compare_fields(fld, fld, "", "99999"))
            sw.step("edge: compare view at a non-numeric time", lambda: AppAutomation(sv).compare_fields(fld, fld, "", "abc"))
            sw.step("edge: viewport asked for a field that does not exist", lambda: vp.show_field("noSuchField", latest, "z")
                    or vp.info.text()[:200])
            sw.step("edge: diff a missing file", lambda: df.compare_paths(root / "nope.txt", root / "system" / "controlDict"))
            sw.step("edge: diff a binary file", lambda: df.compare_paths(Path(sys.executable), root / "system" / "controlDict"))
            sw.step("edge: editor opens a missing file", lambda: ed.open(root / "system" / "nope") or "handled")
            sw.step("edge: python console floods output", lambda: pc.run_code("for i in range(200000): print(i)") and "ok",
                    timeout=120, until=lambda: not pc._busy, detail=lambda: f"{len(pc.out.toPlainText()):,} chars kept")
            sw.step("edge: hole blocking with impossible radii", lambda: (am.h_r.setValue(5.0), am.h_half.setValue(1.0),
                                                                        am.holes(), am.h_out.toPlainText()[:200])[3])
            sw.step("edge: hole blocking with garbage level lines", lambda: (am.h_r.setValue(0.25), am.h_half.setValue(0.75),
                                                                           am.h_levels.setPlainText("cap\nR0 abc\n\n???"),
                                                                           am.holes(), am.h_out.toPlainText()[:200])[4])
            sw.step("edge: grading with zero sizes", lambda: (am.g_first.setValue(0), am.g_last.setValue(0), am.g_max.setValue(0),
                                                            am.design(), am.g_out.toPlainText()[:200])[4])
            am.status.setText("")
            sw.step("edge: CAD import of a missing file", lambda: am.import_cad(str(root / "nope.step")), timeout=120,
                    until=lambda: am.status.text() not in ("", "Converting with FreeCAD…"), detail=lambda: am.status.text())
        else:
            sw.step("viewport with no results", lambda: "no results: " + (vp.info.text()[:160] or "empty"), timeout=60,
                    until=lambda: True)
            an = sw.panel("aether.analysis")
            sw.step("no results: POD says why", an.analyse, timeout=120, until=lambda: not an.m_busy,
                    detail=lambda: an.m_out.toPlainText()[:200])
            if "PyTorch " in an.p_status.text() or sw.wait(lambda: "checking" not in an.p_status.text(), 60):
                an.p_field.addItem("p")  # the field list is empty without results: force a choice
                an.p_field.setCurrentText("p")
                sw.step("no results: PINN says why", an.train_pinn, timeout=120,
                        until=lambda: an.p_go.isEnabled() or "PyTorch environment not found" in an.p_status.text(),
                        detail=lambda: an.p_out.toPlainText()[:200])
            ms = sw.panel("aether.measurements")

            def meas_no_results():
                f = sw.out / "sweep_noresults.csv"
                f.write_text("x,y,z,p\n0.05,0.05,0.005,1\n")
                ms.csv.setText(str(f))
                ms.load()
                ms.compare()
                ms.assimilate()
                return ms.out.toPlainText()[:200]

            sw.step("no results: measurements say why", meas_no_results)
            from aethercfd.studio.app.automation import AppAutomation

            sw.step("no results: compare view says why", lambda: AppAutomation(sv).compare_fields("p", "U"))

        # ------------------------------------------------------------ run monitor panels (no runs started)
        for tid in ("aether.run_status", "aether.residuals", "aether.solver_log", "aether.mesh_quality"):
            sw.panel(tid)
        mq = sw.panel("aether.mesh_quality")
        sw.step("mesh quality reads the checkMesh log", lambda: mq.load_latest() or "loaded")

    # ---------------------------------------------------------------- copilot (no message is sent)
    cp = sw.panel("aether.copilot")
    sw.step("copilot finds Claude Code + sign-in", lambda: "looking", timeout=90,
            until=lambda: "Looking" not in cp.status.text(), detail=lambda: cp.status.text())
    sw.shot("final")

    failed = [st for st in sw.steps if not st["ok"]]
    report = {"case": str(root) if root else None, "has_results": has_results, "steps": sw.steps,
              "failed": len(failed), "total": len(sw.steps)}
    (out / "sweep.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"sweep: {len(sw.steps) - len(failed)}/{len(sw.steps)} steps ok -> {out / 'sweep.json'}", flush=True)
    return 1 if failed else 0
