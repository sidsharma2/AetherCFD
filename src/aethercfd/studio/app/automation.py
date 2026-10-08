"""App automation: what the copilot (or a script/tutorial) can do with the GUI, in front of the user.

Everything here runs on the GUI thread (the copilot's tool threads marshal calls through
panels.copilot._UiHooks). Names are resolved fuzzily so "vis", "visualisation" or "Visualization panel" all
find the same panel. Nothing here changes case files -- edits and runs stay behind the approval dialog.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path

from rapidfuzz import fuzz, process

PERSPECTIVE_ALIASES = {"setup": "setup", "mesh": "meshing", "meshing": "meshing", "run": "run", "monitor": "run",
                       "post": "post", "postprocessing": "post", "post-processing": "post", "visualization": "post",
                       "code": "code"}
_IDS = itertools.count(1)


class AppAutomation:
    def __init__(self, services):
        self.sv = services

    @property
    def win(self):
        return self.sv.window

    # ------------------------------------------------------------------ helpers
    def _spec(self, name: str):
        specs = self.sv.panels.specs()
        keys = {s.type_id: s for s in specs} | {s.title.lower(): s for s in specs}
        if name in keys:
            return keys[name]
        hit = process.extractOne(name.lower(), list(keys), scorer=fuzz.WRatio, score_cutoff=55)
        return keys[hit[0]] if hit else None

    def _viewport(self, open_if_missing: bool = True):
        vp = self.sv.active_viewport
        if vp is None and self.sv.host is not None:
            live = self.sv.host.panels_of("aether.viewport")
            vp = live[0] if live else None
        if vp is None and open_if_missing:
            spec = self.sv.panels.get("aether.viewport")
            d = self.sv.host.open_panel(spec)
            vp = self.sv.host.build_panel(d.objectName())
        return vp

    def _dock_of(self, panel_name: str):
        spec = self._spec(panel_name)
        if spec is None:
            return None
        for n, d in self.sv.host.docks().items():
            if n.split("#")[0] == spec.type_id and not d.isClosed():
                return d
        return None

    # ------------------------------------------------------------------ state
    def overview(self) -> dict:
        host = self.sv.host
        open_panels = [{"panel": n.split("#")[0], "title": d.windowTitle()} for n, d in host.docks().items()
                       if not d.isClosed() and not n.startswith("aether.workbench")]
        out = {"perspective": getattr(self.win.layouts, "current", None), "open_panels": open_panels,
               "case": str(self.sv.session.root) if self.sv.session else None,
               "available_panels": [{"panel": s.type_id, "title": s.title, "what": s.description}
                                    for s in self.sv.panels.specs()],
               "selection": {k: v for k, v in (self.sv.selection or {}).items() if k not in ("values", "text")}}
        vp = self._viewport(open_if_missing=False)
        if vp is not None:
            out["viewport"] = {"field": vp.field.currentText(), "time": vp.time.currentText(),
                               "fields": vp.field_names(), "times": [vp.time.itemText(i) for i in
                                                                    range(vp.time.count())][-10:],
                               "stages": [{**s, "result": {k: v for k, v in vp.stage_info.get(f"stage:{s['id']}",
                                                                                              {}).items()
                                                           if k in ("cells", "notes", "label", "range")}}
                                          for s in vp.stages],
                               "gpu": self.sv.gl_renderer}
        return out

    # ------------------------------------------------------------------ panels and perspectives
    def open_panel(self, name: str) -> str:
        spec = self._spec(name)
        if spec is None:
            return f"no panel like {name!r}; available: {', '.join(s.title for s in self.sv.panels.specs())}"
        d = self.sv.host.open_panel(spec)
        self.sv.host.build_panel(d.objectName())
        return f"opened {spec.title}"

    def _panel(self, type_id: str):
        d = self.sv.host.open_panel(self.sv.panels.get(type_id))
        return self.sv.host.build_panel(d.objectName())

    def show_diff(self, left: str, right: str = "", other_case: str = "", before_last_change: bool = False) -> str:
        """Open the Diff panel on two case files, a file vs the same file in another case, or a file vs its
        content before its last journalled change."""
        s = self.sv.session
        if s is None:
            return "no case is open"
        root = Path(s.root)
        lp = (root / left) if not Path(left).is_absolute() else Path(left)
        p = self._panel("aether.diff")
        if before_last_change:
            return p.before_last_change(lp)
        if other_case:
            return p.compare_with_other_case(lp, other_case)
        rp = (root / right) if right and not Path(right).is_absolute() else Path(right)
        return p.compare_paths(lp, rp)

    def python(self, code: str) -> str:
        """Put code into the Python console for the user to read and run (not run automatically)."""
        return self._panel("aether.python").insert(code)

    def open_case(self, path: str, *, delay_ms: int = 2500) -> str:
        """Open another OpenFOAM case (any folder with system/controlDict). Deferred a little: the copilot
        works per case, and opening a case ends its current conversation, so its reply gets out first."""
        from PySide6.QtCore import QTimer

        p = Path(path.strip().strip('"')).expanduser()
        if not (p / "system" / "controlDict").is_file():
            return f"{p} is not an OpenFOAM case (no system/controlDict)"
        s = self.sv.session
        if s is not None and Path(s.root).resolve() == p.resolve():
            return f"{p.name} is already the open case"
        QTimer.singleShot(delay_ms, lambda: self.sv.open_case(str(p)))
        return (f"opening {p} in {delay_ms / 1000:g} s; this copilot conversation then closes (it belongs to the "
                "current case) and the user can ask again in the new case")

    def perspective(self, name: str) -> str:
        pid = PERSPECTIVE_ALIASES.get(name.lower().strip(), name.lower().strip())
        from aethercfd.studio.layout.recipes import PERSPECTIVES

        if pid not in {p.id for p in PERSPECTIVES}:
            return f"unknown perspective {name!r}; use setup, meshing, run, post or code"
        self.win.layouts.switch(pid)
        return f"switched to the {pid} perspective"

    def select_file(self, path: str) -> str:
        s = self.sv.session
        if s is None:
            return "no case is open"
        p = Path(path)
        p = p if p.is_absolute() else s.root / p
        if not p.exists():
            return f"{path} does not exist"
        self.sv.select({"kind": "file", "path": str(p)})
        tree = next(iter(self.sv.host.panels_of("aether.case_tree")), None)
        if tree is not None and hasattr(tree, "reveal"):
            tree.reveal(p)
        return f"selected {p.name} (details in the Properties panel)"

    # ------------------------------------------------------------------ visualization
    def add_stage(self, kind: str, params: dict) -> str:
        from aethercfd.results.pipeline import STAGE_TYPES

        if kind not in STAGE_TYPES:
            return f"unknown stage {kind!r}; one of {', '.join(STAGE_TYPES)}"
        vp = self._viewport()
        if vp is None:
            return "no viewport"
        sid = f"c{next(_IDS)}"
        vp.set_stages([*vp.stages, {"id": sid, "type": kind, "visible": True, **params}])
        vis = next(iter(self.sv.host.panels_of("aether.vis_controls")), None)
        if vis is not None:
            vis._retarget(vp)
        return sid

    def stage_status(self, sid: str) -> dict | None:
        vp = self._viewport(open_if_missing=False)
        return None if vp is None else vp.stage_info.get(f"stage:{sid}")

    def remove_stage(self, sid: str | None) -> str:
        vp = self._viewport(open_if_missing=False)
        if vp is None:
            return "no viewport"
        keep = [] if sid in (None, "", "all") else [s for s in vp.stages if s.get("id") != sid]
        vp.set_stages(keep)
        vis = next(iter(self.sv.host.panels_of("aether.vis_controls")), None)
        if vis is not None:
            vis._retarget(vp)
        return f"{len(keep)} stage(s) left"

    def camera(self, view: str, stage_id: str | None = None) -> str:
        vp = self._viewport(open_if_missing=False)
        if vp is None:
            return "no viewport"
        v = view.lower()
        if v in ("iso", "isometric"):
            vp.plotter.view_isometric()
            vp.plotter.reset_camera()
        elif v in ("x", "y", "z"):
            vp._view({"x": "yz", "y": "xz", "z": "xy"}[v])
        elif v in ("fit", "reset"):
            vp._fit()
        elif v in ("frame", "frame_stage", "zoom"):
            if not vp.frame_stage(stage_id):
                return "nothing to frame"
        else:
            return "view must be iso, x, y, z, fit or frame"
        vp.plotter.render()
        return f"camera: {view}"

    def render(self, width: int, height: int, background: str, base: str, frame: bool, name: str | None) -> str:
        vp = self._viewport(open_if_missing=False)
        if vp is None or self.sv.session is None:
            return "no viewport/case"
        out = Path(self.sv.session.root) / ".aethercfd" / "local" / "renders"
        out.mkdir(parents=True, exist_ok=True)
        f = out / (name or f"render_{time.strftime('%Y%m%d_%H%M%S')}.png")
        vp.render_image(str(f), width, height, background=background, base=base, frame_stages=frame)
        return str(f)

    # ------------------------------------------------------------------ pointing and actions
    def highlight(self, target: str, message: str, seconds: float = 6.0) -> str:
        from aethercfd.studio.widgets.callout import point_at

        d = self._dock_of(target)
        widget = d if d is not None else None
        if widget is None:
            if target.lower() in ("window", "app", "main"):
                widget = self.win
            elif target.lower() in ("status", "statusbar", "status bar"):
                widget = self.win.statusBar()
            elif target.lower() in ("menu", "menubar", "menu bar"):
                widget = self.win.menuBar()
        if widget is None:
            return f"nothing visible like {target!r} (open the panel first)"
        if d is not None:
            d.toggleView(True)
            d.setAsCurrentTab()
        point_at(widget, message, seconds)
        return f"pointing at {target}"

    def actions(self, query: str = "") -> list[str]:
        acts = self._all_actions()
        names = list(acts)
        if not query:
            return names[:80]
        return [h[0] for h in process.extract(query, names, scorer=fuzz.WRatio, limit=15, score_cutoff=50)]

    def trigger(self, label: str) -> str:
        from PySide6.QtCore import QTimer

        acts = self._all_actions()
        act = acts.get(label)
        if act is None:
            hit = process.extractOne(label, list(acts), scorer=fuzz.WRatio, score_cutoff=70)
            act = acts.get(hit[0]) if hit else None
            label = hit[0] if hit else label
        if act is None:
            return f"no action like {label!r}"
        if "Exit" in label:
            return "the copilot may not close the application"
        QTimer.singleShot(0, act.trigger)  # dialogs open without blocking the copilot
        return f"triggered {label}"

    def _all_actions(self) -> dict:
        out = {}

        def walk(menu, path):
            for a in menu.actions():
                if a.menu():
                    walk(a.menu(), [*path, a.text().replace("&", "")])
                elif a.text() and not a.isSeparator() and a.isEnabled():
                    out[" › ".join([*path, a.text().replace("&", "")])] = a

        for a in self.win.menuBar().actions():
            if a.menu():
                walk(a.menu(), [a.text().replace("&", "")])
        return out
