"""--smoke: open the studio, cycle every perspective, screenshot every top-level window, quit.

Fails (exit 1) on Python exceptions or on Qt warnings about OpenGL / RHI / WebEngine.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from aethercfd.studio.layout.recipes import PERSPECTIVES

_BAD = ("OpenGL", "QRhi", "WebEngine", "GL_", "QOpenGLWidget")


def _pump(app: QApplication, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)


def run_smoke(app: QApplication, win, out: Path, qt_warnings: list[str]) -> int:
    import faulthandler
    import os as _os0

    if _os0.environ.get("AETHER_SMOKE_WATCHDOG"):  # dump every thread's stack and exit if the run hangs
        out.mkdir(parents=True, exist_ok=True)
        _wd = open(out / "watchdog_stacks.txt", "w", encoding="utf-8")  # noqa: SIM115 -- open until exit
        faulthandler.dump_traceback_later(float(_os0.environ["AETHER_SMOKE_WATCHDOG"]), exit=True, file=_wd)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    old_hook = sys.excepthook

    def hook(t, v, tb):
        errors.append("".join(traceback.format_exception(t, v, tb)))
        old_hook(t, v, tb)

    sys.excepthook = hook
    report = {"perspectives": {}}
    try:
        _pump(app, 2.0)
        for p in PERSPECTIVES:
            how = win.layouts.restore(p.id)
            _pump(app, 1.2)
            shots = []
            win.grab().save(str(out / f"{p.id}_main.png"))
            shots.append(f"{p.id}_main.png")
            for i, fw in enumerate(win.host.floating_containers()):
                if fw.isVisible():
                    fw.grab().save(str(out / f"{p.id}_float{i}.png"))
                    shots.append(f"{p.id}_float{i}.png")
            open_docks = [n for n, d in win.host.docks().items() if not d.isClosed()]
            report["perspectives"][p.id] = {"how": how, "open": open_docks, "shots": shots}
        sv = win.services
        import os as _os2

        if sv.session is not None and _os2.environ.get("AETHER_SMOKE_RUN"):
            run_live_scenario(app, win, out, report)
        elif sv.session is not None:
            _pump(app, 1.0)
            win.layouts.restore("setup")
            tree = sv.panels.get("aether.case_tree")
            ed = sv.panels.get("aether.editor")
            if tree is not None:
                win.host.open_panel(tree)
            if ed is not None:
                d = win.host.open_panel(ed, args={"path": str(sv.session.root / "system" / "controlDict")})
                _pump(app, 0.5)
                panel = win.host.panel(d.objectName())
                if panel is not None:
                    panel.mode_group.button(2).setChecked(True)
                    panel._set_mode(2)
            _pump(app, 3.0)
            win.grab().save(str(out / "case_editor.png"))
            wf = sv.panels.get("aether.workflow")
            if wf is not None:
                win.host.open_panel(wf)
                _pump(app, 1.5)
                win.grab().save(str(out / "case_workflow.png"))
            pal = getattr(win, "palette", None)
            if pal is not None:
                pal.open()
                pal.edit.setText("@deltaT")
                _pump(app, 0.8)
                kw = [pal.list.item(i).text() for i in range(min(5, pal.list.count()))]
                pal.edit.setText("#controlDict")
                _pump(app, 0.5)
                files = [pal.list.item(i).text() for i in range(min(3, pal.list.count()))]
                pal.edit.setText("residuals")
                _pump(app, 0.3)
                acts = [pal.list.item(i).text() for i in range(min(3, pal.list.count()))]
                win.grab().save(str(out / "case_palette.png"))
                pal.hide()
                report["palette"] = {"keywords": kw, "files": files, "actions": acts}
            win.layouts.restore("run")
            _pump(app, 4.0)
            win.grab().save(str(out / "case_run.png"))
            ai = sv.panels.get("aether.copilot")
            if ai is not None:  # panel + Claude discovery only; never sends a prompt (that costs usage)
                d = win.host.open_panel(ai)
                _pump(app, 4.0)
                cp = win.host.build_panel(d.objectName())
                if cp is not None:
                    report["copilot"] = {"cli": str(cp.cli.exe) if cp.cli else None,
                                         "signed_in": bool(cp.auth and cp.auth.logged_in),
                                         "status": cp.status.text(), "banner": cp.banner.isVisible()}
                win.grab().save(str(out / "case_copilot.png"))
            import os as _os

            skip = set(_os.environ.get("AETHER_SMOKE_SKIP", "").split(","))
            term_spec = None if "terminal" in skip else sv.panels.get("aether.terminal")
            if term_spec is not None:
                d = win.host.open_panel(term_spec)
                _pump(app, 6.0)
                term = win.host.panel(d.objectName())
                if term is not None:
                    term.write("echo AETHER_CHECK $WM_PROJECT_VERSION $(pwd)\r")
                    _pump(app, 3.0)
                    got = {}
                    term.view.page().runJavaScript(
                        "(()=>{let b=term.buffer.active,o=[];for(let i=0;i<b.length;i++){let l=b.getLine(i);"
                        "if(l)o.push(l.translateToString(true));}return o.join('@@');})()", 0,
                        lambda v: got.setdefault("text", v))
                    _pump(app, 1.5)
                    text = got.get("text") or ""
                    line = next((ln for ln in text.split("@@") if ln.startswith("AETHER_CHECK")), None)
                    report["terminal"] = {"check_line": line, "tail": [x for x in text.split("@@") if x.strip()][-4:]}
                    win.grab().save(str(out / "case_terminal.png"))
            if "viewport" not in skip:
                win.layouts.restore("post")
            _pump(app, 6.0)
            win.grab().save(str(out / "case_viewport.png"))
            visible = [v for v in win.host.panels_of("aether.viewport") if v.isVisible()]
            vp = visible[0] if visible else None
            if vp is not None:
                report["viewport"] = {"actors": len(vp.actors), "scalar": vp.scalar, "range": vp.range,
                                      "gpu": win.services.gl_renderer, "info": vp.info.text()}
            vis = sv.panels.get("aether.vis_controls")
            if vp is not None and vis is not None and "viewport" not in skip:
                visp = win.host.build_panel(win.host.open_panel(vis).objectName())
                sv.set_active_viewport(vp)
                custom = _os.environ.get("AETHER_SMOKE_STAGES")  # JSON list of stage dicts
                if custom:
                    vp.set_stages([{"id": f"x{i}", "visible": True, **s} for i, s in enumerate(json.loads(custom))])
                    visp._retarget(vp)
                else:
                    visp.add("slice")
                    visp.add("quality")
                t_wait = time.perf_counter()
                while time.perf_counter() - t_wait < float(_os.environ.get("AETHER_SMOKE_STAGE_WAIT", "60")):
                    _pump(app, 1.0)
                    if vp.stages and all(f"stage:{s['id']}" in vp.stage_info for s in vp.stages):
                        break
                _pump(app, 1.5)
                render = _os.environ.get("AETHER_SMOKE_RENDER")
                if render:
                    if _os.environ.get("AETHER_SMOKE_VIEW") == "iso":
                        vp.plotter.view_isometric()
                        vp.plotter.reset_camera()
                    if _os.environ.get("AETHER_SMOKE_AO", "1") == "1":
                        vp.btn_ao.setChecked(True)
                    _pump(app, 1.0)
                    report["render_started"] = True
                    (out / "smoke.partial.json").write_text(json.dumps(report, indent=1, default=str),
                                                            encoding="utf-8")
                    t_r = time.perf_counter()
                    vp.render_image(render, 3840, 2160, background="gradient",
                                    base=_os.environ.get("AETHER_SMOKE_BASE", "outline"), frame_stages=True)
                    report["render"] = {"path": render, "seconds": round(time.perf_counter() - t_r, 2)}
                report["vis"] = {"stages": [s["type"] for s in vp.stages],
                                 "info": {k: {"cells": v["cells"], "notes": v["notes"], "label": v["label"]}
                                          for k, v in vp.stage_info.items()}}
                win.grab().save(str(out / "case_vis.png"))
            if _os.environ.get("AETHER_SMOKE_COMBUSTION") == "1" and sv.panels.get("aether.combustion"):
                cb = win.host.build_panel(win.host.open_panel(sv.panels.get("aether.combustion")).objectName())
                cb.preset.setCurrentText("Sandia flame D (CH4/air 1:3 vol.)")
                cb.compute_mixture()
                t_c = time.perf_counter()
                while "mixture" not in cb.report and time.perf_counter() - t_c < 60:
                    _pump(app, 0.5)
                _pump(app, 1.0)
                win.grab().save(str(out / "case_combustion.png"))
                report["combustion"] = {"Z_st": cb.report.get("mixture", {}).get("stoich", {}).get("Z_st")}
            from aethercfd.studio.app.automation import AppAutomation

            auto = AppAutomation(sv)
            report["automation"] = {
                "open": auto.open_panel("checks"), "fuzzy": auto.open_panel("visualisation pannel"),
                "actions": auto.actions("new case templ")[:2],
                "point": auto.highlight("checks", "The Checks panel lists setup problems with one-click fixes.", 3),
                "select": auto.select_file("system/controlDict"),
                "overview_panels": len(auto.overview()["open_panels"])}
            _pump(app, 1.0)
            win.grab().save(str(out / "case_callout.png"))
            insp = sv.panels.get("aether.inspector")
            if insp is not None:
                ip = win.host.build_panel(win.host.open_panel(insp).objectName())
                sv.select({"kind": "file", "path": str(sv.session.root / "0" / "U")})
                _pump(app, 0.8)
                report["inspector"] = {"title": ip.title.text(), "rows": ip.tree.topLevelItemCount()}
                if vp is not None and vp.bounds is not None:
                    b = vp.bounds
                    vp._req_probe.emit(vp.vid, vp.gen, {"case": str(vp.case_root), "decomposed": False,
                                                        "point": [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2,
                                                                  (b[4] + b[5]) / 2],
                                                        "time": vp.meta["times"][vp.time.currentIndex()]})
                    _pump(app, 3.0)
                    sel = sv.selection or {}
                    report["probe"] = {"kind": sel.get("kind"), "cell": sel.get("cell"),
                                       "fields": sorted(sel.get("values", {}))}
                win.grab().save(str(out / "case_inspector.png"))
            report["case"] = {"root": str(sv.session.root), "backend": bool(sv.session.forks)}
    except Exception:
        errors.append(traceback.format_exc())
    bad = [w for w in qt_warnings if any(k in w for k in _BAD)]
    report.update(errors=errors, qt_warnings=qt_warnings, bad_warnings=bad, ok=not errors and not bad)
    (out / "smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "errors", "bad_warnings")}, indent=1))
    win.close()
    _pump(app, 0.5)
    return 0 if report["ok"] else 1


def run_live_scenario(app: QApplication, win, out: Path, report: dict, *, seconds: float = 25.0) -> None:
    """GUI-driven live run (AETHER_SMOKE_RUN=1, scratch cases only): start a solve from the Run panel,
    watch Residuals/Solver Log update live, stop-and-write from the panel, check the final state."""
    from aethercfd.commands.base import USER

    sv = win.services
    s = sv.session
    assert s is not None and "_aethercfd_scratch" in str(s.root), "live scenario only runs on scratch copies"
    t0 = time.perf_counter()
    while (s.runner is None or not s.forks) and time.perf_counter() - t0 < 60:
        _pump(app, 0.5)
    sv.ensure_project()
    win.layouts.restore("run")
    _pump(app, 1.0)
    run_panel = next((win.host.panel(n) for n in win.host.docks() if n.startswith("aether.run_status")), None)
    res_panel = next((win.host.panel(n) for n in win.host.docks() if n.startswith("aether.residuals")), None)
    log_panel = next((win.host.panel(n) for n in win.host.docks() if n.startswith("aether.solver_log")), None)
    params = {"case": str(s.root), "preset": "solve", "nprocs": 1, "placement": "auto", "enable_signals": True}
    plan = s.executor.plan("run.start", params, USER)
    res = s.executor.execute("run.start", params, USER, plan=plan, approved_hash=plan.plan_hash)
    live: dict = {"start_ok": res.ok, "message": res.message}
    if res.ok and run_panel is not None:
        run_panel.run_id = res.data
        run_panel._refresh()
        _pump(app, seconds)
        live["iterations_plotted"] = len(res_panel.x_iter) if res_panel else None
        live["curves"] = sorted(res_panel.curves) if res_panel else None
        live["log_lines"] = log_panel.view.document().blockCount() if log_panel else None
        live["status_time"] = run_panel.vals["time"].text()
        win.grab().save(str(out / "live_run.png"))
        run_panel.control("stop_write")
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 90:
            rec = s.runs.get(res.data)
            if rec is not None and rec.state in ("finished", "failed", "killed", "guard_stopped", "lost"):
                break
            _pump(app, 0.5)
        _pump(app, 2.0)
        rec = s.runs.get(res.data)
        live["final_state"] = rec.state if rec else None
        live["exit_code"] = rec.exit_code if rec else None
        live["history_rows"] = run_panel.history.rowCount()
        win.grab().save(str(out / "live_run_done.png"))
    report["live_run"] = live
