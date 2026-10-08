"""Command palette (Ctrl+K / Ctrl+Shift+P): one search box for actions, panels, perspectives,
monitors, case files (#) and dictionary keywords (@). Fuzzy-ranked with rapidfuzz."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout
from rapidfuzz import fuzz

from aethercfd.case.model import is_time_dir
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T


@dataclass
class PaletteItem:
    label: str
    category: str
    run: Callable[[], None]
    icon: str = "msc.symbol-event"
    detail: str = ""
    keywords: str = ""  # synonyms people type for this item (searched, not shown)


# What people call things (searched alongside labels and descriptions; misspellings are handled by scoring)
KEYWORDS = {
    "aether.residuals": "convergence residual plot initial residuals monitor graph",
    "aether.solver_log": "log output console stdout errors fatal",
    "aether.run_status": "run start stop solve simulation launch kill pause write now",
    "aether.mesh_quality": "checkmesh check mesh quality non-orthogonality skewness aspect ratio bad cells",
    "aether.viewport": "3d view render results field contour plot visualise visualize",
    "aether.vis_controls": "pipeline slice clip contour isosurface streamlines q criterion vortices schlieren glyphs "
                           "mirror revolve presentation smoothing",
    "aether.inspector": "properties inspector details probe cell value info",
    "aether.checks": "numerics lab pre-run checks schemes relaxation boundedness diagnose problems errors fix",
    "aether.copilot": "claude ai assistant chat help copilot agent ask",
    "aether.terminal": "shell bash wsl command line console",
    "aether.journal": "history undo revert changes log journal",
    "aether.case_tree": "files explorer tree folders browse",
    "aether.editor": "edit dictionary text file code",
    "aether.workflow": "stages steps guide workflow progress",
    "aether.welcome": "start home recent cases",
    "aether.aethermesh": "mesh meshing quality hotspots grading blockmesh y+ yplus first cell layers stl geometry snappy cfmesh strategy wedge extrude feature angle",
    "aether.code": "code write own model solver turbulence model pope round jet c++ wmake compile library boundary condition source fvoption coded",
    "aether.combustion": "combustion cantera flame speed adiabatic temperature ignition delay chemistry mechanism creck gri flamelet regime borghi peters edc pasr soot reacting",
}
STAGE_ITEMS = [("qcriterion", "Q-criterion vortices (GPU)", "vortex vortices q criterion lambda2 swirl coherent "
                "structures cuda gpu isosurface", "mdi6.weather-tornado"),
               ("isosurface", "Isosurface (GPU)", "iso surface contour level flame front cuda gpu", "mdi6.layers-outline"),
               ("slice", "Slice", "cut plane section", "mdi6.box-cutter"),
               ("streamlines", "Streamlines", "stream lines path flow lines particle traces", "mdi6.waves"),
               ("schlieren", "Schlieren", "density gradient shadowgraph shock", "mdi6.blur"),
               ("quality", "Mesh quality map", "bad cells non-orthogonality skewness aspect ratio", "mdi6.grid"),
               ("glyphs", "Vector glyphs", "arrows vectors quiver", "mdi6.arrow-top-right"),
               ("threshold", "Threshold", "filter range cells above below", "mdi6.filter-outline")]


def score(query: str, item: PaletteItem) -> float:
    """Typo-tolerant relevance: best of whole-label, substring-with-typos, word-set and keyword matches."""
    q = query.lower()
    lab = item.label.lower()
    text = f"{lab} {item.detail.lower()} {item.keywords}"
    s = max(fuzz.WRatio(q, lab), 0.95 * fuzz.partial_ratio(q, lab), 0.9 * fuzz.token_set_ratio(q, text),
            0.88 * fuzz.partial_ratio(q, text) if len(q) >= 4 else 0.0)
    if lab.startswith(q) or any(w.startswith(q) for w in lab.split()):
        s += 8
    return s


class CommandPalette(QDialog):
    MAX = 60

    def __init__(self, window):
        super().__init__(window, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.window = window
        self.setStyleSheet(f"QDialog {{ background: {T.raised}; border: 1px solid {T.border}; border-radius: 6px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.edit = QLineEdit(placeholderText="Search actions, panels, files (#), keywords (@)…")
        self.edit.textChanged.connect(self._filter)
        self.edit.returnPressed.connect(self._run_current)
        self.list = QListWidget()
        self.list.itemActivated.connect(lambda it: self._run(it))
        lay.addWidget(self.edit)
        lay.addWidget(self.list)
        self.items: list[PaletteItem] = []
        self.resize(640, 420)
        QShortcut(QKeySequence("Down"), self.edit, lambda: self._move(1))
        QShortcut(QKeySequence("Up"), self.edit, lambda: self._move(-1))

    # ------------------------------------------------------------------ providers
    def _collect(self) -> list[PaletteItem]:
        w = self.window
        sv = w.services
        out: list[PaletteItem] = []

        def walk(menu, path):
            for a in menu.actions():
                if a.menu():
                    walk(a.menu(), [*path, a.text().replace("&", "")])
                elif a.text() and not a.isSeparator() and a.isEnabled():
                    out.append(PaletteItem(" › ".join([*path, a.text().replace("&", "")]), "action",
                                           a.trigger, "msc.symbol-event", a.shortcut().toString()))

        for a in w.menuBar().actions():
            if a.menu():
                walk(a.menu(), [a.text().replace("&", "")])
        for spec in sv.panels.specs():
            out.append(PaletteItem(f"Open {spec.title}", "panel", lambda s=spec: sv.host.open_panel(s), spec.icon,
                                   spec.description, KEYWORDS.get(spec.type_id, "")))
        from aethercfd.studio.app.automation import AppAutomation

        auto = AppAutomation(sv)
        for kind, title, kw, ic in STAGE_ITEMS:
            out.append(PaletteItem(f"Add to 3D view: {title}", "visualization",
                                   lambda k=kind: (auto.add_stage(k, {}), auto.open_panel("Visualization")), ic,
                                   "Visualization pipeline", kw))
        out.append(PaletteItem("Render image (up to 8K)…", "visualization",
                               lambda: auto._viewport() and auto._viewport().render_dialog(), "mdi6.image-filter-hdr",
                               "publication / presentation render", "screenshot picture export png 4k 8k render"))
        from aethercfd.templates.catalog import TEMPLATES

        for t in TEMPLATES:
            out.append(PaletteItem(f"New case: {t.title}", "template", w.new_case_dialog, "msc.new-folder",
                                   t.summary, " ".join(sorted(t.tags)) + " template tutorial new case"))
        from aethercfd.studio.layout.recipes import PERSPECTIVES
        from aethercfd.studio.layout.screens import current_screens

        for p in PERSPECTIVES:
            out.append(PaletteItem(f"Perspective: {p.title}", "perspective",
                                   lambda pid=p.id: w.layouts.switch(pid), "msc.layout", p.shortcut))
        focused = sv.host.manager.focusedDockWidget()
        if focused is not None:
            name = focused.objectName()
            for s in current_screens():
                out.append(PaletteItem(f"Move {focused.windowTitle()} to {s.label}", "monitor",
                                       lambda i=s.index, n=name: w.layouts.move_to_monitor(n, i),
                                       "mdi6.monitor-arrow-down"))
        return out

    def _case_files(self) -> list[PaletteItem]:
        sv = self.window.services
        s = sv.session
        if s is None:
            return []
        out = []
        root = s.root
        times = sorted((float(p.name), p.name) for p in root.iterdir() if p.is_dir() and is_time_dir(p.name))
        keep_times = {times[0][1], times[-1][1]} if times else set()
        for dirpath, dirnames, files in os.walk(root):
            rel = Path(dirpath).relative_to(root)
            top = rel.parts[0] if rel.parts else ""
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "processor")) and d != "postProcessing"
                           and not (not rel.parts and is_time_dir(d) and d not in keep_times) and d != "polyMesh"]
            for f in files:
                p = Path(dirpath) / f
                out.append(PaletteItem(str(p.relative_to(root)), "file",
                                       lambda pp=p: self._open_file(pp), "msc.file", top))
            if len(out) > 3000:
                break
        return out

    def _keywords(self) -> list[PaletteItem]:
        sv = self.window.services
        s = sv.session
        if s is None:
            return []
        from aethercfd.foamdict.cst import Entry
        from aethercfd.foamdict.parser import parse

        out = []
        for f in sorted((s.root / "system").glob("*")) + sorted((s.root / "constant").glob("*")):
            if not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            try:
                doc = parse(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue

            def walk(d, path, f=f, doc=doc):
                for m in d.members:
                    if isinstance(m, Entry) and m.key.name != "FoamFile":
                        p = [*path, m.key.name]
                        val = "" if m.value is None else doc.text(m.value.start, m.value.end)[:40]
                        line = doc.line_col(m.start)[0]
                        out.append(PaletteItem(f"{f.name} › {' › '.join(p)}", "keyword",
                                               lambda ff=f, ln=line: self._open_file(ff, ln), "msc.symbol-key", val))
                        if m.dict is not None and len(p) < 4:
                            walk(m.dict, p)

            walk(doc.root, [])
        return out

    def _open_file(self, p: Path, line: int | None = None) -> None:
        sv = self.window.services
        spec = sv.panels.get("aether.editor")
        if spec is None:
            return
        d = sv.host.open_panel(spec, args={"path": str(p)})
        if line:
            panel = sv.host.panel(d.objectName())
            if panel is not None:
                panel.editor.goto_line(line)

    # ------------------------------------------------------------------ ui
    def open(self, text: str = "") -> None:
        self.items = self._collect()
        self.edit.setText(text)
        self._filter(text)
        g = self.window.geometry()
        self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 80)
        self.show()
        self.edit.setFocus()

    def _filter(self, text: str) -> None:
        text = text.strip()
        pool = self.items
        if text.startswith("#"):
            pool, text = self._case_files(), text[1:].strip()
        elif text.startswith("@"):
            pool, text = self._keywords(), text[1:].strip()
        elif text.startswith(">"):
            pool, text = [i for i in self.items if i.category == "action"], text[1:].strip()
        self.list.clear()
        if not text:
            ranked = pool[: self.MAX]
        else:
            ranked = self.rank(text, pool)
            if len(text) >= 4 and pool is self.items:
                ranked.append(PaletteItem(f"Ask the copilot: “{text}”", "copilot", lambda q=text: self._ask(q),
                                          "mdi6.robot-outline", "Claude answers with the case in view"))
        for it in ranked:
            li = QListWidgetItem(icon(it.icon), f"{it.label}" + (f"    {it.detail}" if it.detail else ""))
            li.setData(Qt.ItemDataRole.UserRole, it)
            self.list.addItem(li)
        if self.list.count():
            self.list.setCurrentRow(0)

    def rank(self, text: str, pool: list[PaletteItem], cutoff: float = 52.0) -> list[PaletteItem]:
        scored = sorted(((score(text, it), i, it) for i, it in enumerate(pool)), key=lambda x: (-x[0], x[1]))
        return [it for s, _i, it in scored[: self.MAX] if s >= cutoff]

    def _ask(self, question: str) -> None:
        sv = self.window.services
        spec = sv.panels.get("aether.copilot")
        if spec is None:
            return
        d = sv.host.open_panel(spec)
        panel = sv.host.build_panel(d.objectName())
        if panel is not None:
            panel.input.setPlainText(question)
            if panel.auth and panel.auth.logged_in and sv.session is not None:
                panel._send_input()

    def _move(self, d: int) -> None:
        self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + d)))

    def _run_current(self) -> None:
        it = self.list.currentItem()
        if it is not None:
            self._run(it)

    def _run(self, li: QListWidgetItem) -> None:
        item: PaletteItem = li.data(Qt.ItemDataRole.UserRole)
        self.hide()
        item.run()


def install(window) -> CommandPalette:
    pal = CommandPalette(window)
    act = QAction("Command Palette…", window)
    act.setShortcuts([QKeySequence("Ctrl+K"), QKeySequence("Ctrl+Shift+P")])
    act.triggered.connect(pal.open)
    window.addAction(act)
    window.menuBar().actions()[1].menu().insertAction(window.menuBar().actions()[1].menu().actions()[0], act)
    return pal
