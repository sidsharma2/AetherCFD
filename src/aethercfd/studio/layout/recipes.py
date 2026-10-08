"""Built-in perspectives as declarative recipes for 1, 2 and 3 screens.

A recipe is a list of placements applied through DockHost; panel types that are not registered yet
are skipped, so recipes never break when a panel is missing. "Reset perspective" re-applies one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aethercfd.studio.shell.panel_base import Area


@dataclass(frozen=True)
class Place:
    panel: str  # panel type id
    area: Area
    screen: int | None = None  # float onto screen N (0-based, left-to-right) if that many screens exist
    maximize: bool = False
    group: str | None = None  # floating panels with the same group share one window


@dataclass(frozen=True)
class Perspective:
    id: str
    title: str
    shortcut: str
    places: dict[int, list[Place]] = field(default_factory=dict)  # by screen count (1, 2, 3)

    def recipe(self, n_screens: int) -> list[Place]:
        for n in range(min(n_screens, 3), 0, -1):
            if n in self.places:
                return self.places[n]
        return []


V = "aether.viewport"
TREE = "aether.case_tree"
FLOW = "aether.workflow"
ED = "aether.editor"
RUN = "aether.run_status"
RES = "aether.residuals"
LOG = "aether.solver_log"
TERM = "aether.terminal"
MESHQ = "aether.mesh_quality"
VIS = "aether.vis_controls"
PROPS = "aether.inspector"
JOUR = "aether.journal"
START = "aether.welcome"
AI = "aether.copilot"
CHECKS = "aether.checks"
AMESH = "aether.aethermesh"

_setup1 = [Place(FLOW, Area.LEFT), Place(TREE, Area.LEFT), Place(START, Area.CENTER), Place(CHECKS, Area.RIGHT),
           Place(PROPS, Area.AUTOHIDE_RIGHT), Place(AI, Area.RIGHT), Place(TERM, Area.AUTOHIDE_BOTTOM), Place(JOUR, Area.AUTOHIDE_BOTTOM)]
_run1 = [Place(TREE, Area.LEFT), Place(RES, Area.CENTER), Place(RUN, Area.RIGHT), Place(AI, Area.AUTOHIDE_RIGHT),
         Place(LOG, Area.BOTTOM)]
_mesh1 = [Place(TREE, Area.LEFT), Place(V, Area.CENTER), Place(AMESH, Area.RIGHT), Place(AI, Area.AUTOHIDE_RIGHT),
          Place(LOG, Area.BOTTOM),
          Place(TERM, Area.AUTOHIDE_BOTTOM)]
_post1 = [Place(TREE, Area.LEFT), Place(V, Area.CENTER), Place(VIS, Area.RIGHT), Place(PROPS, Area.RIGHT)]
_code1 = [Place(TREE, Area.LEFT), Place(START, Area.CENTER), Place(TERM, Area.BOTTOM)]

PERSPECTIVES: list[Perspective] = [
    Perspective("setup", "Setup", "Ctrl+Alt+1", {1: _setup1, 3: [*_setup1, Place(V, Area.FLOATING, 1, True)]}),
    Perspective("meshing", "Meshing", "Ctrl+Alt+2", {
        1: _mesh1,
        2: [Place(TREE, Area.LEFT), Place(MESHQ, Area.RIGHT), Place(LOG, Area.BOTTOM), Place(V, Area.FLOATING, 1, True)],
        3: [Place(TREE, Area.LEFT), Place(MESHQ, Area.RIGHT), Place(V, Area.FLOATING, 1, True),
            Place(LOG, Area.FLOATING, 2, True, "mon")]}),
    Perspective("run", "Run Monitor", "Ctrl+Alt+3", {
        1: _run1,
        2: [Place(TREE, Area.LEFT), Place(RUN, Area.RIGHT), Place(RES, Area.FLOATING, 1, True, "mon"),
            Place(LOG, Area.FLOATING, 1, True, "mon")],
        3: [Place(FLOW, Area.LEFT), Place(TREE, Area.LEFT), Place(V, Area.FLOATING, 1, True),
            Place(RES, Area.FLOATING, 2, True, "mon"), Place(RUN, Area.FLOATING, 2, True, "mon"),
            Place(LOG, Area.FLOATING, 2, True, "mon")]}),
    Perspective("post", "Post-processing", "Ctrl+Alt+4", {1: _post1, 2: [Place(TREE, Area.LEFT), Place(VIS, Area.LEFT),
                                                                        Place(V, Area.FLOATING, 1, True)]}),
    Perspective("code", "Code", "Ctrl+Alt+5", {1: _code1}),
]


def perspective(pid: str) -> Perspective | None:
    return next((p for p in PERSPECTIVES if p.id == pid), None)
