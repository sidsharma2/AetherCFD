"""The guided, step-by-step workflow: twelve stages, each with a completion state, the next action,
dependencies, correctness gates, and physical questions that turn into exact dictionary edits.

Geometry → Domain → Materials → Physics → Mesh → Models → Boundary/Initial conditions → Numerics → Run →
Validation → Post-processing → ML/Data assimilation.

* The case on disk is the truth. A stage's status is computed from its files and gates every time; the
  only stored state is the user's "marked done" (with a fingerprint of the stage's files, so a later
  change shows the stage as changed) and the answers to the reference-quantity questions (velocity,
  length, fluid, ...). Both live in the project sidecar (``.aethercfd/local/workflow.json``), never
  in the OpenFOAM case.
* A stage cannot be marked done while one of its gates fails with severity ``error``; each gate says
  why and how to fix it (``aethercfd.case.gates``).
* Answers become a ``dict.batch`` plan: one approval with every diff, one journal entry, one revert.
  Guided, Advanced (the existing forms, AetherMesh, numerics lab) and Raw (the editor on the exact
  files) all change the same files, so they stay in sync by construction.

No Qt here: the Workflow panel and the copilot's workflow tools are both thin views of this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aethercfd.case import gates as G
from aethercfd.case import qoi as Q
from aethercfd.case.checks import TURB_FIELDS, CaseFacts, facts
from aethercfd.case.model import Case, load_case

STATE_FORMAT = 1
CMU = 0.09

# (id, title, depends on, optional)
STAGE_DEFS: list[tuple[str, str, tuple[str, ...], bool]] = [
    ("geometry", "Geometry", (), False),
    ("domain", "Domain", ("geometry",), False),
    ("materials", "Materials", (), False),
    ("physics", "Physics", ("materials",), False),
    ("mesh", "Mesh", ("domain",), False),
    ("models", "Models", ("physics",), False),
    ("bc", "Boundary & Initial Conditions", ("models", "mesh"), False),
    ("numerics", "Numerics", ("models",), False),
    ("run", "Run", ("mesh", "bc", "numerics"), False),
    ("validation", "Validation", ("run",), False),
    ("post", "Post-processing", ("run",), False),
    ("ml", "ML / Data Assimilation", ("run",), True),
]
TITLES = {s[0]: s[1] for s in STAGE_DEFS}
ORDER = [s[0] for s in STAGE_DEFS]

# What "Advanced" opens per stage: (panel id, label)
ADVANCED: dict[str, list[tuple[str, str]]] = {
    "geometry": [("aether.aethermesh", "AetherMesh: CAD import / STL intake"), ("aether.viewport", "3D Viewport")],
    "domain": [("aether.aethermesh", "AetherMesh: blockMesh grading"), ("aether.case_tree", "Case Files")],
    "materials": [("aether.combustion", "Combustion (Cantera properties)"), ("aether.inspector", "Properties")],
    "physics": [("aether.checks", "Checks: solver facts"), ("aether.code", "Physics Code: your own models")],
    "mesh": [("aether.aethermesh", "AetherMesh: quality maps, y+, snappy"), ("aether.mesh_quality", "Mesh Quality")],
    "models": [("aether.combustion", "Combustion model advisor"), ("aether.checks", "Checks")],
    "bc": [("aether.case_tree", "Case Files (0/)"), ("aether.checks", "Checks")],
    "numerics": [("aether.checks", "Numerics lab (Checks)")],
    "run": [("aether.run_status", "Run"), ("aether.residuals", "Residuals"), ("aether.solver_log", "Solver Log")],
    "validation": [("aether.residuals", "Residuals"), ("aether.measurements", "Measurements (experiment vs CFD)"),
                   ("aether.diff", "Diff vs another case")],
    "post": [("aether.viewport", "3D Viewport (Showcase)"), ("aether.vis_controls", "Visualization pipeline"),
             ("aether.python", "Python Console")],
    "ml": [("aether.analysis", "Analysis: POD/DMD, GP surrogate, PINN"), ("aether.measurements", "Measurements: CFDMI")],
}
# Which cfd_agents fit where (aethercfd.services.cfd_agents)
AGENTS: dict[str, list[str]] = {"mesh": ["darwin"], "numerics": ["reggie"], "run": ["murphy"],
                                "validation": ["connie", "murphy"]}


# ============================================================================ model
@dataclass
class Question:
    id: str
    text: str
    kind: str  # choice | number | bool
    value: Any = None
    unit: str = ""
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    help: str = ""
    derived: str = ""  # what the current answer implies (Ma, Re, first-cell height, ...)
    writes: list[str] = field(default_factory=list)  # case files an answer changes ('' = sidecar only)

    def as_dict(self) -> dict:
        d = {"id": self.id, "question": self.text, "kind": self.kind, "value": self.value}
        for k in ("unit", "help", "derived"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.choices:
            d["choices"] = [{"value": v, "label": lab} for v, lab in self.choices]
        d["writes"] = self.writes or ["(reference value kept in the workflow state, not in the case)"]
        return d


@dataclass
class Action:
    id: str  # run.mesh | run.check | run.start | agent.<name> | open.<panel>
    label: str
    help: str = ""


@dataclass
class StageState:
    id: str
    title: str
    index: int
    status: str  # done | ready | attention | error | blocked | changed | todo | na
    summary: str
    next_action: str
    depends_on: list[str]
    blocked_by: list[str]
    gates: list[G.Gate]
    files: list[Path]
    questions: list[Question] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    advanced: list[tuple[str, str]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    marked_done: bool = False
    optional: bool = False
    fingerprint: str = ""

    @property
    def blocking(self) -> list[G.Gate]:
        return [g for g in self.gates if g.blocking]

    @property
    def warnings(self) -> list[G.Gate]:
        return [g for g in self.gates if not g.ok and g.severity == "warn"]

    @property
    def can_mark_done(self) -> bool:
        return not self.blocking and not self.blocked_by and self.status != "na"

    def as_dict(self, root: Path | None = None, *, detail: bool = True) -> dict:
        def rel(p: Path) -> str:
            try:
                return Path(p).relative_to(root).as_posix() if root else str(p)
            except ValueError:
                return str(p)

        d: dict[str, Any] = {"id": self.id, "index": self.index, "title": self.title, "status": self.status,
                             "summary": self.summary, "next_action": self.next_action,
                             "marked_done": self.marked_done, "can_mark_done": self.can_mark_done}
        if self.blocked_by:
            d["blocked_by"] = self.blocked_by
        d["failing_gates"] = [g.title for g in self.gates if not g.ok and g.severity in ("error", "warn")]
        if detail:
            d["depends_on"] = self.depends_on
            d["gates"] = [g.as_dict() for g in self.gates]
            d["files"] = [rel(f) for f in self.files]
            d["questions"] = [q.as_dict() for q in self.questions]
            d["actions"] = [{"id": a.id, "label": a.label, "help": a.help} for a in self.actions]
            d["agents"] = self.agents
        return d


# ============================================================================ sidecar state
def state_path(case_root: Path) -> Path | None:
    from aethercfd.project.project import Project

    proj = Project.find(case_root)
    return proj.local / "workflow.json" if proj is not None else None


def load_state(case_root: Path) -> dict:
    p = state_path(case_root)
    if p is None or not p.is_file():
        return {"format": STATE_FORMAT, "answers": {}, "stages": {}}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"format": STATE_FORMAT, "answers": {}, "stages": {}}
    st.setdefault("answers", {})
    st.setdefault("stages", {})
    return st


def dump_state(state: dict) -> str:
    return json.dumps(state, indent=2, sort_keys=True, default=str) + "\n"


def fingerprint(files: list[Path]) -> str:
    """Content hash of a stage's files (large files and directories by name/size/mtime)."""
    h = hashlib.sha256()
    items: list[Path] = []
    for f in files:
        f = Path(f)
        if f.is_dir():
            items += sorted(p for p in f.rglob("*") if p.is_file())
        elif f.is_file():
            items.append(f)
    for p in sorted(set(items)):
        h.update(p.name.encode())
        try:
            st = p.stat()
            if st.st_size <= 2 * 2**20:
                h.update(hashlib.sha256(p.read_bytes()).digest())
            else:
                h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:16]


# ============================================================================ assessment
@dataclass
class _Ctx:
    case: Case
    facts: CaseFacts
    fields: dict[str, G.FieldFile]
    ref: G.Reference
    answers: dict
    non_ortho: float | None = None
    findings: list = field(default_factory=list)  # aethercfd.case.checks findings, computed once
    _run: tuple | None = None
    _qoi: Q.QoIPlan | None = None

    def run(self) -> tuple[G.Gate, G.RunSummary | None]:
        if self._run is None:
            self._run = G.run_gate(self.case)
        return self._run

    def qoi_plan(self) -> Q.QoIPlan:
        if self._qoi is None:
            self._qoi = Q.plan(self.case, self.answers, self.facts, self.fields)
        return self._qoi


def _rel_existing(root: Path, *rels: str) -> list[Path]:
    return [root / r for r in rels if (root / r).exists()]


def _turb_dict_rel(case: Case) -> str | None:
    for r in ("constant/momentumTransport", "constant/turbulenceProperties"):
        if (case.root / r).is_file():
            return r
    return None


def _material_rel(case: Case) -> str | None:
    for r in ("constant/transportProperties", "constant/physicalProperties", "constant/thermophysicalProperties"):
        if (case.root / r).is_file():
            return r
    return None


def assess(case: Case | str | Path, state: dict | None = None) -> list[StageState]:
    case = case if isinstance(case, Case) else load_case(case)
    state = state if state is not None else load_state(case.root)
    answers = state.get("answers", {})
    fields = G.initial_fields(case)
    mesh_gate, non_ortho = G.mesh_quality_gate(case)
    f = facts(case, non_ortho)
    from aethercfd.case.checks import run_checks

    try:
        findings = run_checks(case, mesh_non_ortho=non_ortho)[1]
    except Exception:
        findings = []
    ctx = _Ctx(case, f, fields, G.reference(case, answers, fields), answers, non_ortho, findings)
    builders = {"geometry": _geometry, "domain": _domain, "materials": _materials, "physics": _physics,
                "mesh": lambda c: _mesh(c, mesh_gate), "models": _models, "bc": _bc, "numerics": _numerics,
                "run": _run, "validation": _validation, "post": _post, "ml": _ml}
    out: list[StageState] = []
    by_id: dict[str, StageState] = {}
    for i, (sid, title, deps, optional) in enumerate(STAGE_DEFS, start=1):
        try:
            summary, files, gates, questions, actions, na = builders[sid](ctx)
        except Exception as exc:  # one broken reader must not hide the other stages
            summary, files, gates, questions, actions, na = (
                f"could not assess: {type(exc).__name__}: {exc}", [], [G.Gate(
                    "assess", False, "warn", "Could not assess this stage", str(exc))], [], [], False)
        st = StageState(sid, title, i, "todo", summary, "", list(deps), [], gates, files, questions, actions,
                        ADVANCED.get(sid, []), AGENTS.get(sid, []), optional=optional)
        st.fingerprint = _stage_fingerprint(sid, ctx, files)
        mark = state.get("stages", {}).get(sid, {})
        st.blocked_by = [d for d in deps if by_id[d].blocking or by_id[d].status == "blocked"]
        st.status = _status(st, mark, na)
        st.marked_done = bool(mark.get("done"))
        out.append(st)
        by_id[sid] = st
    for st in out:
        st.next_action = _next_action(st, out)
    return out


_RESIDUAL_CONTROL = re.compile(r"\bresidualControl\s*\{[^{}]*\}", re.S)


def _stage_fingerprint(sid: str, c: _Ctx, files: list[Path]) -> str:
    """What a "done" mark is bound to. Stages share files (controlDict, fvSolution): a stage is bound only to
    what it decides, so a later stage's edit (e.g. Run adding function objects or residualControl) does not
    invalidate an earlier stage."""
    if sid == "physics":
        f = c.facts
        data = f"{c.case.application}|{c.case.solver_module}|{f.algorithm}|{f.steady}|{f.compressible}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    if sid == "numerics":
        h = hashlib.sha256()
        for rel in ("system/fvSchemes", "system/fvSolution"):
            text = G._read(c.case.root / rel) or ""
            h.update(_RESIDUAL_CONTROL.sub("", text).encode())  # residualControl is a Run-stage decision
        return h.hexdigest()[:16]
    return fingerprint(files)


def _status(st: StageState, mark: dict, na: bool) -> str:
    if na:
        return "na"
    if st.blocking:
        return "error"
    if st.blocked_by:
        return "blocked"
    if mark.get("done"):
        return "done" if mark.get("fingerprint") == st.fingerprint else "changed"
    if st.warnings:
        return "attention"
    return "ready"


def _next_action(st: StageState, stages: list[StageState]) -> str:
    if st.status == "na":
        return "Not needed for this case."
    if st.status == "blocked":
        by = {s.id: s for s in stages}
        why = []
        for b in st.blocked_by:
            dep = by[b]
            why.append(f"{dep.title} ({dep.blocking[0].title})" if dep.blocking else f"{dep.title} (itself waiting)")
        return f"First finish {', '.join(why)}."
    if st.blocking:
        g = st.blocking[0]
        fix = f" One-click fix: {g.fix_label}." if g.edits else ""
        return f"Fix: {g.title}. {g.how}{fix}".strip()
    if st.status == "changed":
        return "Files changed since you marked this stage done: review the gates and mark it done again."
    if st.status == "done":
        nxt = next((s for s in stages[st.index:] if s.status not in ("done", "na") and not s.optional), None)
        return f"Done. Next: {nxt.title}." if nxt else "Done. Every stage is complete."
    if st.warnings:
        return f"Review: {st.warnings[0].title}. {st.warnings[0].how}".strip() + " Then mark the stage done."
    open_q = [q for q in st.questions if q.value in (None, "")]
    if open_q:
        return f"Answer: {open_q[0].text}"
    return "Checks pass: review the summary and mark the stage done."


# ============================================================================ per-stage builders
def _geometry(c: _Ctx):
    r = c.case.root
    surf = _rel_existing(r, "constant/triSurface", "constant/geometry")
    shm = r / "system" / "snappyHexMeshDict"
    gates, files = [], list(surf) + ([shm] if shm.is_file() else [])
    if shm.is_file():
        text = G._read(shm) or ""
        refs = re.findall(r"([\w.\-]+\.(?:stl|obj|vtk|stlb|ftr|STL|OBJ))", text)
        missing = [n for n in dict.fromkeys(refs)
                   if not any((d / n).exists() for d in (r / "constant/triSurface", r / "constant/geometry"))]
        if missing:
            gates.append(G.Gate("surfaces", False, "error", "snappyHexMeshDict refers to missing surface files",
                                "snappyHexMesh aborts when a geometry file cannot be read.",
                                "Put the files in constant/triSurface (AetherMesh ▸ STL/CAD import does this), or the "
                                "tutorial's Allrun copies them from its resources.", [f"missing: {', '.join(missing)}"]))
        else:
            gates.append(G.passed("surfaces", "Every referenced surface exists", refs[:8]))
    na = not surf and not shm.is_file()
    if na:
        summary = "no CAD surfaces: the domain is block-structured (blockMesh) or an imported mesh"
    else:
        names = sorted(p.name for d in surf for p in d.iterdir() if p.is_file()) if surf else []
        summary = f"{len(names)} surface file(s): {', '.join(names[:6])}" if names else "snappyHexMesh setup"
    q = [Question("geometry_source", "Where does the geometry come from?", "choice",
                  c.answers.get("geometry_source") or ("blocks" if na else "surfaces"),
                  choices=[("blocks", "Simple shape built from blocks (blockMesh)"),
                           ("surfaces", "CAD / STL surfaces (snappyHexMesh)"),
                           ("imported", "A mesh made elsewhere (Fluent, Gmsh, ...)")],
                  help="Decides which meshing tools the Mesh stage offers.")]
    acts = [Action("open.aether.aethermesh", "Import CAD / STL (AetherMesh)", "STEP/IGES/STL with named parts")]
    return summary, files, gates, q, acts, na


def _domain(c: _Ctx):
    r = c.case.root
    dom = _rel_existing(r, "system/blockMeshDict", "system/snappyHexMeshDict", "system/extrudeMeshDict",
                        "system/meshDict", "constant/polyMesh/blockMeshDict")
    gates = [G.syntax_gate(c.case, [p.relative_to(r).as_posix() for p in dom if p.is_file()])]
    bb = G.bounding_box(c.case)
    reg = c.case.regions[0] if c.case.regions else None
    parts = []
    if bb:
        ext = [hi - lo for lo, hi in zip(*bb, strict=True)]
        parts.append("box " + " × ".join(f"{e:.4g}" for e in ext) + " m")
    if reg is not None:
        parts.append("2D" if reg.is_2d else "axisymmetric" if reg.is_axisymmetric else "3D")
        patches = [p for p in reg.patches if (p.n_faces or 0) > 0]
        parts.append(f"{len(patches)} patches")
        suspicious = [p.name for p in patches if p.type == "patch" and "wall" in p.name.lower()]
        if suspicious:
            gates.append(G.Gate("wall-type", False, "warn", "Wall-named patches are of type 'patch'",
                                "Wall functions and wall distance only act on patches of type wall; a 'patch' "
                                "named wall gets no near-wall treatment.",
                                "Set type wall for them in the mesh definition (blockMeshDict boundary) and re-mesh.",
                                [", ".join(suspicious)]))
    elif not dom:
        gates.append(G.Gate("domain", False, "error", "No domain definition and no mesh",
                            "The case has neither a mesh generator dictionary nor a mesh.",
                            "Start from a template (New Case), or import a mesh."))
    L = c.answers.get("L_ref") or (round(c.ref.L, 6) if c.ref.L else None)
    q = [Question("L_ref", "Characteristic length of the flow (step height, chord, pipe diameter)", "number",
                  L, "m", help="Used for the Reynolds number, y+ estimates and turbulence length scales.",
                  derived=f"default from the {c.ref.sources['L']}" if L and not c.answers.get("L_ref") else ""),
         Question("flow_kind", "Is the flow internal (duct, pipe, channel) or external (around a body)?", "choice",
                  c.answers.get("flow_kind") or ("external" if G._external(c.case) else "internal"),
                  choices=[("internal", "Internal (duct, pipe, channel)"), ("external", "External (around a body)")],
                  help="Selects the skin-friction correlation for y+ (pipe vs flat plate).")]
    return ", ".join(parts) or "domain defined by an imported mesh", dom, gates, q, [], False


def _materials(c: _Ctx):
    r = c.case.root
    mat = _rel_existing(r, "constant/thermophysicalProperties", "constant/transportProperties",
                        "constant/physicalProperties", "constant/thermophysicalTransport")
    if (r / "constant").is_dir():
        mat += list((r / "constant").glob("*/thermophysicalProperties"))
    gates = [G.syntax_gate(c.case, [p.relative_to(r).as_posix() for p in mat])]
    if not mat:
        gates.append(G.Gate("materials", False, "error", "No material properties",
                            "Every solver reads the fluid's properties (constant/transportProperties, "
                            "physicalProperties or thermophysicalProperties).", "Start from a template, or copy "
                            "the file from a tutorial of the same solver."))
    nu = c.ref.nu
    if nu is not None and not (1e-7 <= nu <= 1e-2):
        gates.append(G.Gate("nu-range", False, "warn", f"Unusual kinematic viscosity {nu:.3g} m²/s",
                            "Gases are ~1e-5 m²/s, water ~1e-6 m²/s; a value far outside 1e-7…1e-2 is often a "
                            "dynamic viscosity (Pa s) or a unit slip.", "Check nu and its units.",
                            [f"nu from {c.ref.sources.get('nu')}"]))
    gates += G.checks_gates(c.findings, ids={"thermo-range"})
    incompressible_nu = any(p.name in ("transportProperties",) for p in mat) or (
        (r / "constant/physicalProperties").is_file() and G._dict_value(c.case, "constant/physicalProperties", "nu"))
    q = [Question("fluid", "Which fluid is it?", "choice", c.answers.get("fluid") or _guess_fluid(nu),
                  choices=[(k, k) for k in G.FLUIDS] + [("custom", "Something else (give nu)")],
                  help="Sets a typical viscosity, and the gas constants used for the Mach number.",
                  writes=[_material_rel(c.case) or ""] if incompressible_nu else []),
         Question("T_ref", "Typical temperature", "number", c.answers.get("T_ref") or round(c.ref.T or 293.15, 2),
                  "K", help="For the speed of sound (Mach number) and gas viscosity.")]
    if incompressible_nu:
        q.append(Question("nu", "Kinematic viscosity ν = μ/ρ", "number", nu, "m²/s",
                          help="Air ≈ 1.5e-5, water ≈ 1.0e-6 m²/s at 20 °C.", writes=[_material_rel(c.case) or ""]))
    summary = ", ".join(sorted({p.name for p in mat})) or "no material properties found"
    if nu:
        summary += f"; ν = {nu:.4g} m²/s"
    return summary, mat, gates, q, [], False


def _guess_fluid(nu: float | None) -> str | None:
    if nu is None:
        return None
    best = min(G.FLUIDS, key=lambda k: abs(math.log10(G.FLUIDS[k][0]) - math.log10(nu)))
    return best if abs(math.log10(G.FLUIDS[best][0]) - math.log10(nu)) < 0.25 else "custom"


def _physics(c: _Ctx):
    case, f = c.case, c.facts
    gates = []
    if not (case.application or case.solver_module):
        gates.append(G.Gate("solver", False, "error", "No solver named in controlDict",
                            "controlDict must name the application (ESI) or the solver module (Foundation foamRun).",
                            "Start from a template; or set 'application' in system/controlDict."))
    gates.append(G.mach_gate(case, f, c.ref))
    gates.append(G.steady_gate(case, f, c.answers))
    ma, re_ = c.ref.mach, c.ref.reynolds
    derived = []
    if ma is not None:
        derived.append(f"Ma ≈ {ma:.3g} ({'incompressible is fine' if ma < 0.3 else 'compressible'})")
    if re_ is not None:
        derived.append(f"Re ≈ {re_:.3g} ({_regime(re_, c.answers.get('flow_kind') or 'internal')})")
    q = [Question("U_ref", "Typical flow speed (e.g. inlet velocity)", "number",
                  c.answers.get("U_ref") or (round(c.ref.U, 6) if c.ref.U else None), "m/s",
                  help="With the temperature this gives the Mach number, with the length and ν the Reynolds number.",
                  derived="; ".join(derived)),
         Question("time_dependence", "Do you want a steady (time-averaged) or a transient (time-resolved) answer?",
                  "choice", c.answers.get("time_dependence") or ("steady" if f.steady else "transient"),
                  choices=[("steady", "Steady: the converged mean flow"), ("transient", "Transient: how it evolves")],
                  help="Must match the solver algorithm (SIMPLE = steady, PIMPLE/PISO = transient)."),
         Question("compressible", "Is the flow compressible?", "choice",
                  "yes" if f.compressible else "no", choices=[("no", "No (Ma < 0.3, small heating)"),
                                                             ("yes", "Yes (Ma > 0.3, strong heating, combustion)")],
                  help="Read from the solver; the Mach estimate above says whether that is right.",
                  derived=derived[0] if derived else "")]
    summary = f"{case.solver_label}: {'steady' if f.steady else 'transient'}, " \
              f"{'compressible' if f.compressible else 'incompressible'}, {f.algorithm}"
    return summary, _rel_existing(case.root, "system/controlDict"), gates, q, [], False


def _regime(re_: float, kind: str) -> str:
    lim = 2300 if kind == "internal" else 5e5
    return "laminar" if re_ < lim else "transitional" if re_ < 2 * lim else "turbulent"


def _mesh(c: _Ctx, mesh_gate: G.Gate):
    case = c.case
    gates = [G.mesh_exists_gate(case), mesh_gate]
    files = [reg.mesh_dir for reg in case.regions if reg.mesh_dir.exists()]
    log = G.checkmesh_log(case)
    if log is not None:
        files.append(log)
    summary = f"{case.n_cells:,} cells" if case.n_cells else "no mesh yet"
    if c.non_ortho is not None:
        summary += f", max non-orthogonality {c.non_ortho:.1f}°"
    target = c.answers.get("target_yplus") or ("wall-functions" if c.facts.turbulence not in (None, "laminar")
                                               else "resolved")
    derived = ""
    if c.ref.U and c.ref.L and c.ref.nu:
        from aethercfd.mesh.wall import first_cell_height

        yp = 1.0 if target == "resolved" else 50.0
        ws = first_cell_height(yp, c.ref.U, c.ref.L, nu=c.ref.nu,
                               kind="plate" if (c.answers.get("flow_kind") == "external") else "pipe")
        derived = f"for y+ ≈ {yp:g}: first cell height ≈ {ws.first_cell:.3g} m (Re {ws.reynolds:.3g}, ±30 %)"
        dist = G.wall_distance(case)
        if dist:
            first = min(d[1] for d in dist.values()) * 2.0
            derived += f"; this mesh: ≈ {first:.3g} m (mean, smallest wall patch)"
    q = [Question("target_yplus", "How will the walls be treated?", "choice", target,
                  choices=[("wall-functions", "Wall functions (y+ 30-100: coarser, faster)"),
                           ("resolved", "Resolve the boundary layer (y+ ≈ 1: fine, accurate heat transfer/"
                                        "separation)")],
                  help="AetherMesh ▸ Wall spacing designs the layers for this target.", derived=derived)]
    acts = [Action("run.mesh", "Generate mesh (blockMesh + checkMesh)",
                   "Runs through the run manager: plan shown first; re-meshing a case with results is flagged."),
            Action("run.check", "Check mesh (checkMesh)", "Writes log.checkMesh; quality gates read it."),
            Action("agent.darwin", "Darwin: RANS-informed refinement plan",
                   "cfd_agents; needs a converged RANS result on an all-tet mesh. Plan only, never refines.")]
    return summary, files, gates, q, acts, False


TURB_CHOICES = [("laminar", "Laminar (low Re, no turbulence model)"), ("kEpsilon", "k-ε (robust, free shear flows)"),
                ("realizableKE", "realizable k-ε (better for separation/jets)"),
                ("kOmegaSST", "k-ω SST (walls, separation: the usual default)"),
                ("SpalartAllmaras", "Spalart-Allmaras (external aero, one equation)")]


def _models(c: _Ctx):
    case, f = c.case, c.facts
    r = case.root
    models = _rel_existing(r, "constant/turbulenceProperties", "constant/momentumTransport",
                           "constant/combustionProperties", "constant/chemistryProperties",
                           "constant/radiationProperties", "constant/fvOptions", "system/fvOptions",
                           "constant/fvModels", "system/fvModels", "constant/fvConstraints", "system/fvConstraints")
    gates = [G.syntax_gate(case, [p.relative_to(r).as_posix() for p in models])]
    gates += G.checks_gates(c.findings, ids={"turb-fields", "alphat"})
    gates.append(G.wall_function_gate(case, f, c.fields))
    gates.append(G.yplus_gate(case, f, c.fields, c.ref))
    gates.append(G.species_sum_gate(case, c.fields))
    re_ = c.ref.reynolds
    rec = ""
    if re_ is not None:
        regime = _regime(re_, c.answers.get("flow_kind") or "internal")
        rec = f"Re ≈ {re_:.3g}: {regime}" + ("; a RANS model (k-ω SST) is the usual choice" if regime != "laminar"
                                              else "; laminar is appropriate")
        if regime == "laminar" and f.turbulence not in (None, "laminar"):
            gates.append(G.Gate("regime", False, "warn", f"A turbulence model at Re ≈ {re_:.3g}",
                                "At this Reynolds number the flow is laminar; a RANS model adds spurious viscosity.",
                                "Choose laminar in the Models question (if the reference values are right).",
                                [f"Re from U {c.ref.U:.3g} m/s, L {c.ref.L:.3g} m, ν {c.ref.nu:.3g} m²/s"]))
        if regime == "turbulent" and f.turbulence in (None, "laminar") and not f.compressible:
            gates.append(G.Gate("regime", False, "warn", f"Laminar at Re ≈ {re_:.3g}",
                                "At this Reynolds number the flow is turbulent; a laminar steady solution usually "
                                "does not converge or is unphysical (unless this is DNS/LES with a fine mesh).",
                                "Choose a turbulence model in the Models question.", []))
    tm = f.turbulence or "laminar"
    tq = Question("turbulence", "How should turbulence be modelled?", "choice", tm, choices=TURB_CHOICES,
                  help="Switching writes the model and creates any missing initial fields (k, ω, ε, ν̃, nut) "
                       "from the inlet turbulence intensity.", derived=rec,
                  writes=[_turb_dict_rel(case) or "constant/momentumTransport", "0/*"])
    q = [tq] if _turb_dict_rel(case) else []
    if tm not in ("laminar",) and G._wall_patches(case):
        cur = c.fields.get("nut")
        wall0 = G._wall_patches(case)[0]
        nut_t = cur.patch_entry(wall0, "type") if cur else None
        q.append(Question("wall_treatment", "Wall treatment for nut", "choice",
                          "resolved" if nut_t in ("nutLowReWallFunction", "fixedValue") else
                          "spalding" if nut_t == "nutUSpaldingWallFunction" else "wall-functions",
                          choices=[("wall-functions", "Standard wall functions (nutkWallFunction, y+ > 30)"),
                                   ("spalding", "Spalding (nutUSpaldingWallFunction: any y+)"),
                                   ("resolved", "Wall-resolved (nutLowReWallFunction, y+ ≈ 1)")],
                          help="Must match the mesh y+ (see the y+ gate).", writes=["0/nut"]))
    summary = (f"{f.turbulence or 'laminar'}" + (f" ({', '.join(p.name for p in models[1:4])})"
                                                 if len(models) > 1 else ""))
    return summary, models, gates, q, [], False


def _inlets(c: _Ctx) -> list:
    U = c.fields.get("U")
    if U is None:
        return []
    out = []
    for p in G._flow_patches(c.case):
        if p.type == "wall":
            continue
        t = U.patch_entry(p, "type") or ""
        vec = G._uniform_vector(U.patch_entry(p, "value"))
        if t == "fixedValue" and vec and any(vec):
            out.append((p, vec))
    return out


def _outlets(c: _Ctx) -> list:
    p_ = c.fields.get("p_rgh") or c.fields.get("p")
    if p_ is None:
        return []
    return [(pt, G._uniform_scalar(p_.patch_entry(pt, "value"))) for pt in G._flow_patches(c.case)
            if pt.type != "wall" and (p_.patch_entry(pt, "type") or "") == "fixedValue"]


def turbulence_inlet_defaults(c: _Ctx) -> tuple[float | None, float | None]:
    """(intensity %, length scale m) that reproduce the current k and epsilon/omega inlet values."""
    k = c.fields.get("k")
    inl = _inlets(c)
    if k is None or not inl:
        return None, None
    p, vec = inl[0]
    U = math.sqrt(sum(x * x for x in vec))
    kv = G._uniform_scalar(k.patch_entry(p, "value")) or G._uniform_scalar(k.get("internalField"))
    if not kv or not U:
        return None, None
    intensity = math.sqrt(2.0 * kv / 3.0) / U * 100.0
    eps = c.fields.get("epsilon")
    om = c.fields.get("omega")
    ev = G._uniform_scalar(eps.patch_entry(p, "value")) if eps else None
    ov = G._uniform_scalar(om.patch_entry(p, "value")) or (G._uniform_scalar(om.get("internalField")) if om else
                                                            None) if om else None
    if ev:
        length = CMU**0.75 * kv**1.5 / ev
    elif ov:
        length = math.sqrt(kv) / (CMU**0.25 * ov)
    else:
        length = None
    return round(intensity, 3), (float(f"{length:.4g}") if length else None)


def _bc(c: _Ctx):
    case, f = c.case, c.facts
    from aethercfd.case.workflow import initial_dir

    d0 = initial_dir(case)
    gates = [G.bc_gate(case), G.dimensions_gate(case, f, c.fields), G.pressure_reference_gate(case, f, c.fields),
             G.up_pairing_gate(case, c.fields)]
    gates += G.checks_gates(c.findings, ids={"no-initial", "decomposed-only"})
    q = []
    for p, vec in _inlets(c):
        mag = math.sqrt(sum(x * x for x in vec))
        q.append(Question(f"inlet_U:{p.name}", f"Inlet speed at '{p.name}'", "number", round(mag, 6), "m/s",
                          help=f"Direction kept: ({' '.join(f'{x / mag:.3g}' for x in vec)})", writes=["0/U"]))
    p_kin = not f.compressible
    for p, val in _outlets(c):
        q.append(Question(f"outlet_p:{p.name}", f"Pressure at '{p.name}'", "number", val,
                          "m²/s² (kinematic p/ρ; 0 = reference)" if p_kin else "Pa (absolute)",
                          help="Incompressible solvers use p/ρ relative to a reference; compressible ones absolute Pa.",
                          writes=[f"0/{'p_rgh' if 'p_rgh' in c.fields else 'p'}"]))
    turb = TURB_FIELDS.get(f.turbulence or "", [])
    if turb and _inlets(c):
        ti, tl = turbulence_inlet_defaults(c)
        L = c.ref.L
        q.append(Question("turb_intensity", "Inlet turbulence intensity", "number", ti if ti is not None else 5.0,
                          "%", help="1 % quiet wind tunnel, 5 % typical duct, 10-20 % behind fans/grids.",
                          writes=[f"0/{x}" for x in turb if x != "nut"]))
        q.append(Question("turb_length", "Inlet turbulent length scale", "number",
                          tl if tl is not None else (round(0.07 * L, 6) if L else None), "m",
                          help="≈ 0.07 × hydraulic diameter for duct flow; sets ε = Cμ^¾ k^{3/2}/ℓ and "
                               "ω = √k/(Cμ^¼ ℓ).", writes=[f"0/{x}" for x in turb if x != "nut"],
                          derived=_turb_derived(c, ti, tl)))
    n = len([p for p in d0.rglob("*") if p.is_file()]) if d0 else 0
    summary = f"{d0.name}/ with {n} field files" if d0 else "no initial conditions"
    return summary, ([d0] if d0 else []), gates, q, [], False


def _turb_derived(c: _Ctx, ti: float | None, tl: float | None) -> str:
    inl = _inlets(c)
    if not inl or ti is None or tl is None:
        return ""
    U = math.sqrt(sum(x * x for x in inl[0][1]))
    k, eps, om = turbulence_values(U, ti, tl)
    return f"k = {k:.4g} m²/s², ε = {eps:.4g} m²/s³, ω = {om:.4g} 1/s"


def turbulence_values(U: float, intensity_pct: float, length: float) -> tuple[float, float, float]:
    k = 1.5 * (U * intensity_pct / 100.0) ** 2
    eps = CMU**0.75 * k**1.5 / length
    om = math.sqrt(k) / (CMU**0.25 * length)
    return k, eps, om


SCHEME_PROFILES = [("keep", "Keep the current schemes"),
                   ("robust", "Robust: first-order upwind everywhere (first run, poor mesh; diffusive)"),
                   ("balanced", "Balanced: second-order, bounded (recommended)")]


def _numerics(c: _Ctx):
    case, f = c.case, c.facts
    num = _rel_existing(case.root, "system/fvSchemes", "system/fvSolution")
    gates = [G.syntax_gate(case, ["system/fvSchemes", "system/fvSolution"]),
             G.coverage_gate(case, f, c.fields)]
    gates += G.checks_gates(c.findings, ids={"steady-bounded", "central-U", "central-turb", "laplacian-limit",
                                       "laplacian-overlimited", "nonortho-correctors", "relaxation-missing",
                                       "pressure-limits", "piso-mode"})
    q = [Question("schemes", "Convection schemes: robustness or accuracy?", "choice", "keep", choices=SCHEME_PROFILES,
                  help="Start robust if a run diverges; finish with second-order for the answer you report.",
                  writes=["system/fvSchemes"])]
    if not f.steady:
        cd = G._dict_value(case, "system/controlDict", "maxCo")
        q.append(Question("max_co", "Largest Courant number to allow (adaptive time step)", "number",
                          float(cd) if cd and _is_num(cd) else 1.0, "",
                          help="Co ≲ 1 for PISO-like PIMPLE (nOuterCorrectors 1); larger needs outer correctors.",
                          writes=["system/controlDict"]))
    summary = "fvSchemes + fvSolution" if len(num) == 2 else "fvSchemes/fvSolution missing"
    summary += f", {f.algorithm}"
    acts = [Action("open.aether.checks", "Open the numerics lab", "Every finding with its evidence and fixes")]
    if not f.steady:
        acts.append(Action("agent.reggie", "Reggie: transient numerics design",
                           "cfd_agents; PIMPLE/time-step settings from the mesh non-orthogonality"))
    return summary, num, gates, q, acts, False


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _run(c: _Ctx):
    case, f = c.case, c.facts
    gates = [G.syntax_gate(case, ["system/controlDict"])]
    gates += G.checks_gates(c.findings, ids={"write-after-end", "disk-projection"})
    rg, s = c.run()
    gates.append(rg)
    end = G._dict_value(case, "system/controlDict", "endTime")
    wi = G._dict_value(case, "system/controlDict", "writeInterval")
    q = [Question("end_time", "How many iterations (steady) / until what time (transient)?", "number",
                  float(end) if end and _is_num(end) else None, "iterations" if f.steady else "s",
                  writes=["system/controlDict"]),
         Question("write_interval", "Write results every", "number", float(wi) if wi and _is_num(wi) else None,
                  "steps/s (see writeControl)", writes=["system/controlDict"])]
    if f.steady and f.algorithm == "SIMPLE":
        rc = G._dict_value(case, "system/fvSolution", "SIMPLE/residualControl/p")
        q.append(Question("residual_target", "Stop when the residuals fall below", "number",
                          float(rc) if rc and _is_num(rc) else None, "",
                          help="residualControl for p, U and the turbulence fields; 1e-4 is a common target "
                               "(the tutorial's 1e-2/1e-3 is loose).", writes=["system/fvSolution"]))
    has_mon = _has_monitors(case)
    q.append(Question("monitoring", "Add monitoring function objects (min/max of fields, residuals, y+)?", "bool",
                      has_mon, help="fieldMinMax + solverInfo (ESI) / volFieldValue min, max + residuals (Foundation) "
                                    "+ yPlus: lets the "
                                    "Validation stage (and Connie) check the run's health.",
                      writes=[_functions_rel(case)]))
    qp = c.qoi_plan()
    if qp.objects:
        what = []
        if Q.FORCES in qp.objects:
            what.append(f"force on the walls ({', '.join(qp.walls[:4])}{' …' if len(qp.walls) > 4 else ''})")
        if qp.open_patches:
            what.append(f"flow rate and mean pressure on {', '.join(qp.open_patches[:4])}")
        if Q.HEAT in qp.objects:
            what.append("heat flow through each wall")
        q.append(Question("qoi_monitors", "Add integral monitors (the quantities you will report)?", "bool",
                          Q.has_monitors(case), help="; ".join(what) + ". Every iteration/time step, so the Validation "
                          "stage can judge convergence on them and audit the mass (and heat) balance.",
                          writes=[_functions_rel(case)]))
    from aethercfd.commands.builtin.run_cmds import PRESETS, suggest_preset

    preset, why = suggest_preset(case)
    acts = [Action("run.start", f"Start: {PRESETS[preset]}", f"Automatic choice: {why}. The exact plan is shown "
                   "first; runs go through the run manager (guards, journal, provenance)."),
            Action("agent.murphy", "Murphy: diagnose the latest solver log", "cfd_agents failure diagnosis")]
    summary = (f"{s.steps} steps logged, {'finished' if s.finished else 'not finished'}" if s
               else "not run yet")
    results = [t for _, t in case.times if t != "0"]
    if results:
        summary += f"; {len(results)} result time(s), latest {case.latest_time}"
    logs = sorted(case.root.glob("log.*"))[-6:]
    return summary, [p for p in _rel_existing(case.root, "system/controlDict")] + logs, gates, q, acts, False


def _has_monitors(case: Case) -> bool:
    texts = [G._read(case.root / "system/controlDict") or "", G._read(case.root / "system/functions") or ""]
    return any("fieldMinMax" in t or "aetherMin" in t for t in texts)


def _functions_rel(case: Case) -> str:
    """Where function objects go: the Foundation reads system/functions (and then IGNORES controlDict/functions,
    with only a warning), ESI reads controlDict/functions."""
    return "system/functions" if (case.root / "system/functions").is_file() and not Q.fork_is_esi(case) else \
        "system/controlDict"


def _validation(c: _Ctx):
    case, f = c.case, c.facts
    rg, s = c.run()
    gates = [rg, G.convergence_gate(case, f, s), G.health_gate(case)]
    if case.latest_result_time and f.turbulence not in (None, "laminar") and G.yplus_from_results(case):
        gates.append(G.yplus_gate(case, f, c.fields, c.ref))
    from aethercfd.case import gridstudy

    qp = c.qoi_plan()
    series = Q.read_series(case.root, qp)
    tol = float(c.answers.get("qoi_tolerance") or Q.DEFAULT_TOL_PCT)
    gates.append(Q.convergence_gate(case, f, qp, series, tol)[0])
    gates += Q.conservation_gates(case, f, qp, series)
    verdict = gridstudy.study_gate(case, c.answers)
    gates.append(verdict.gate)
    files = sorted((case.root / "postProcessing").glob("*")) if (case.root / "postProcessing").is_dir() else []
    if (case.root / gridstudy.STUDY_DIR).is_dir():
        files.append(case.root / gridstudy.STUDY_DIR)
    lv = {n for n, _ in gridstudy.levels(case.root)}
    acts = []
    if not {"fine", "coarse"} <= lv:
        acts.append(Action("grid.create", f"Grid study: create coarse and fine meshes (r = {gridstudy.DEFAULT_RATIO:g})",
                           "Copies the case into grid_study/coarse and grid_study/fine with blockMeshDict cell counts "
                           "scaled by 1/r and r; this case is the medium mesh. Shown as one plan to approve."))
    else:
        acts.append(Action("grid.run", "Grid study: run (or continue) the coarse and fine levels",
                           "mesh + check + solve for each level that has not run yet; a level that ran without "
                           "converging is continued from its latest time (startFrom latestTime, approved edit)"))
        acts.append(Action("grid.evaluate", "Grid study: evaluate (Richardson extrapolation, GCI)",
                           "Reads the three levels' results; the record is journalled in the project"))
    acts += [Action("agent.connie", "Connie: run-health review of the telemetry",
                    "cfd_agents; reads postProcessing/*/fieldMinMax.dat, solverInfo.dat"),
             Action("agent.murphy", "Murphy: diagnose the solver log", "cfd_agents failure diagnosis")]
    q = [Question("qoi_tolerance", "How much may a reported quantity still change at the end of the run?", "number",
                  tol, "%", help="Steady: the band of each integral quantity over the last 10 % of the iterations; "
                                 "transient: the drift of its mean between the last two quarters. Measured against the "
                                 "largest value of its kind (|F|, the largest flow rate, ...). 1 % is a common target; "
                                 "keep it well below the GCI you accept."),
         Question("gci_tolerance", "Acceptable discretisation uncertainty (GCI) of the quantity you report", "number",
                  c.answers.get("gci_tolerance") or 5.0, "%",
                  help="The grid study gate fails when GCI_fine exceeds this. 1-5 % is typical for engineering "
                       "quantities; validation against experiments needs it well below the measurement error.")]
    if verdict.choices:
        q.append(Question("grid_qoi", "Which quantity matters for your conclusion?", "choice",
                          c.answers.get("grid_qoi") or verdict.choices[0],
                          choices=[(n, n) for n in verdict.choices],
                          help="The grid study gate judges this quantity (all are in the study record)."))
    summary = rg.title if s is None else f"{len(s.residuals)} residual histories, " + (
        s.converged_msg or ("finished" if s.finished else "not finished"))
    return summary, files, gates, q, acts, False


def _post(c: _Ctx):
    case = c.case
    post = _rel_existing(case.root, "postProcessing")
    results = [t for _, t in case.times if t != "0"] + [t for _, t in case.processor_times if t != "0"]
    gates = [G.passed("results", "Results to look at", [f"latest {case.latest_time}"]) if results else
             G.Gate("results", False, "error", "No results yet", "Nothing to post-process.", "Run the case first.")]
    from aethercfd.results.report import latest

    rep = latest(case.root)
    if rep is not None:
        gates.append(G.passed("report", "Run report written", [str(rep)]))
    acts = [Action("report.build", "Write the run report (Markdown, plots, field pictures)",
                   "Verdict from the Validation gates, every integral quantity with its convergence, the mass/heat "
                   "balance, residuals, ParaView pictures of p, |U|, T and the run's provenance, in the project's "
                   "local/reports folder (nothing is written into the case)."),
            Action("open.aether.viewport", "Show the result (3D Viewport, Showcase)", ""),
            Action("open.aether.residuals", "Residuals", ""),
            Action("open.aether.python", "Python Console (the case as numpy)", "")]
    qp = c.qoi_plan()
    summary = ("function-object output present; " if post else "") + (f"{len(results)} result time(s)" if results
                                                                        else "no results")
    if qp.qois and Q.has_monitors(case):
        summary += f"; {len(qp.qois)} integral quantities monitored"
    return summary, post, gates, [], acts, False


def _ml(c: _Ctx):
    acts = [Action("open.aether.analysis", "Analysis: POD/DMD modes, GP surrogate, PINN", ""),
            Action("open.aether.measurements", "Measurements: compare with and assimilate experiments", "")]
    return ("optional: surrogates, reduced-order models, PINNs, data assimilation (Analysis / Measurements "
            "panels)"), [], [], [], acts, False


# ============================================================================ answers -> edits
@dataclass
class GuidedPlan:
    stage: str
    edits: list[dict] = field(default_factory=list)  # dict.batch BatchEdit dicts (absolute files)
    creates: dict[str, str] = field(default_factory=dict)
    answers: dict = field(default_factory=dict)  # stored in the sidecar
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def batch_params(self, case_root: Path, state: dict | None = None) -> dict:
        """dict.batch parameters: the case edits + the updated workflow sidecar (if answers changed)."""
        params: dict[str, Any] = {"edits": self.edits, "creates": self.creates,
                                  "summary": f"Guided {TITLES.get(self.stage, self.stage)}: "
                                             + (", ".join(self.notes[:3]) or "update")}
        if self.answers:
            sp = state_path(case_root)
            if sp is not None:
                st = state if state is not None else load_state(case_root)
                st = json.loads(json.dumps(st))
                st["format"] = STATE_FORMAT
                st.setdefault("answers", {}).update(self.answers)
                params["replace"] = {str(sp): dump_state(st)}
        return params

    @property
    def empty(self) -> bool:
        return not self.edits and not self.creates and not self.answers


def _edit(case: Case, rel: str, key: str, value: str | None, *, insert: bool = False, nonliteral: bool = False):
    return {"file": str(case.root / rel), "key": key, "value": value, "insert_if_missing": insert,
            "allow_nonliteral": nonliteral}


def plan_answers(case: Case | str | Path, stage: str, answers: dict, state: dict | None = None) -> GuidedPlan:
    """Turn answers to a stage's questions into exact edits. Unknown/unchanged answers are ignored."""
    case = case if isinstance(case, Case) else load_case(case)
    state = state if state is not None else load_state(case.root)
    stages = {s.id: s for s in assess(case, state)}
    if stage not in stages:
        raise ValueError(f"unknown stage {stage!r}; stages: {', '.join(ORDER)}")
    st = stages[stage]
    qs = {q.id: q for q in st.questions}
    gp = GuidedPlan(stage)
    fields = G.initial_fields(case)
    f = facts(case)
    turb: dict[str, float] = {}
    for qid, val in answers.items():
        q = qs.get(qid)
        if q is None:
            gp.errors.append(f"{qid}: not a question of the {st.title} stage (questions: {', '.join(qs)})")
            continue
        if q.kind == "number":
            try:
                val = float(val)
            except (TypeError, ValueError):
                gp.errors.append(f"{qid}: {val!r} is not a number")
                continue
            if not math.isfinite(val):
                gp.errors.append(f"{qid}: must be finite")
                continue
        if q.kind == "choice" and q.choices and val not in [v for v, _ in q.choices]:
            gp.errors.append(f"{qid}: {val!r} is not one of {', '.join(v for v, _ in q.choices)}")
            continue
        if q.kind == "bool":
            val = val in (True, "true", "yes", "1", 1)
        if qid in ("turb_intensity", "turb_length"):
            turb[qid] = val
            continue
        if qid == "fluid" and "nu" in answers:
            val = (val, False)  # an explicit nu wins over the fluid's typical value
        _apply_answer(case, f, fields, stage, q, val, gp, state.get("answers", {}))
    new_speed = {qid.split(":", 1)[1]: float(v) for qid, v in answers.items()
                 if qid.startswith("inlet_U:") and qid in qs and _is_num(str(v))}
    if turb or (new_speed and any(q.startswith("turb_") for q in qs)):
        # inlet turbulence follows the inlet speed: a new speed rescales k, epsilon, omega at the same I and l
        _turb_inlet(case, f, fields, turb, gp, state.get("answers", {}), new_speed)
    return gp


_SIDE_ONLY = {"geometry_source", "L_ref", "flow_kind", "T_ref", "U_ref", "time_dependence", "target_yplus",
              "gci_tolerance", "grid_qoi", "qoi_tolerance"}


def _apply_answer(case: Case, f: CaseFacts, fields, stage: str, q: Question, val, gp: GuidedPlan, stored: dict):
    qid = q.id
    if qid in _SIDE_ONLY:
        if stored.get(qid) != val:
            gp.answers[qid] = val
            gp.notes.append(f"{qid} = {val}")
        return
    if qid == "compressible":
        want = val == "yes"
        if want != f.compressible:
            gp.errors.append("Changing compressibility means a different solver family (and p in Pa, a thermo "
                             "model, T and alphat fields): start from a compressible/incompressible template in "
                             "New Case rather than editing this case.")
        return
    if qid == "fluid":
        val, set_nu = val if isinstance(val, tuple) else (val, True)
        if stored.get("fluid") != val:
            gp.answers["fluid"] = val
            gp.notes.append(f"fluid {val}")
        rel = _material_rel(case)
        if set_nu and val in G.FLUIDS and rel and rel != "constant/thermophysicalProperties":
            _set_nu(case, rel, G.FLUIDS[val][0], gp)
        return
    if qid == "nu":
        rel = _material_rel(case)
        if rel and rel != "constant/thermophysicalProperties":
            _set_nu(case, rel, val, gp)
        return
    if qid.startswith("inlet_U:"):
        patch = qid.split(":", 1)[1]
        U = fields.get("U")
        cur = U.get(f"boundaryField/{patch}/value") if U else None
        vec = G._uniform_vector(cur)
        if not vec:
            gp.errors.append(f"0/U {patch}: value is not a uniform vector; edit it in Raw mode")
            return
        mag = math.sqrt(sum(x * x for x in vec))
        new = tuple(x / mag * val for x in vec)
        if abs(mag - val) > 1e-12 * max(1.0, val):
            from aethercfd.templates.catalog import set_vector_text

            gp.edits.append(_edit(case, _field_rel(case, "U"), f"boundaryField/{patch}/value",
                                  set_vector_text(cur, [f"{x:.6g}" for x in new])))
            gp.notes.append(f"{patch} |U| {mag:g} → {val:g} m/s")
        return
    if qid.startswith("outlet_p:"):
        patch = qid.split(":", 1)[1]
        name = "p_rgh" if "p_rgh" in fields else "p"
        cur = fields[name].get(f"boundaryField/{patch}/value") if name in fields else None
        if cur is None or G._uniform_scalar(cur) is None:
            gp.errors.append(f"0/{name} {patch}: value is not uniform; edit it in Raw mode")
            return
        if G._uniform_scalar(cur) != val:
            from aethercfd.templates.catalog import set_scalar_text

            gp.edits.append(_edit(case, _field_rel(case, name), f"boundaryField/{patch}/value",
                                  set_scalar_text(cur, f"{val:g}")))
            gp.notes.append(f"{patch} {name} = {val:g}")
        return
    if qid == "turbulence":
        _set_turbulence(case, f, fields, val, gp)
        return
    if qid == "wall_treatment":
        want = {"wall-functions": "nutkWallFunction", "spalding": "nutUSpaldingWallFunction",
                "resolved": "nutLowReWallFunction"}[val]
        nut = fields.get("nut")
        if nut is None:
            gp.errors.append("0/nut does not exist")
            return
        for w in G._wall_patches(case):
            t = nut.get(f"boundaryField/{w.name}/type")
            if t is None:
                continue
            if t != want:
                gp.edits.append(_edit(case, _field_rel(case, "nut"), f"boundaryField/{w.name}/type", want))
        if any(e["file"].endswith("nut") for e in gp.edits):
            gp.notes.append(f"nut walls → {want}")
        return
    if qid == "schemes":
        _set_schemes(case, f, fields, val, gp)
        return
    if qid == "max_co":
        gp.edits.append(_edit(case, "system/controlDict", "adjustTimeStep", "yes", insert=True))
        gp.edits.append(_edit(case, "system/controlDict", "maxCo", f"{val:g}", insert=True))
        gp.notes.append(f"adaptive Δt, maxCo {val:g}")
        return
    if qid in ("end_time", "write_interval"):
        key = "endTime" if qid == "end_time" else "writeInterval"
        cur = G._dict_value(case, "system/controlDict", key)
        if cur is None or not _is_num(cur) or float(cur) != val:
            gp.edits.append(_edit(case, "system/controlDict", key, f"{val:g}", insert=True))
            gp.notes.append(f"{key} {val:g}")
        return
    if qid == "residual_target":
        rc = "SIMPLE/residualControl"
        if G._dict_value(case, "system/fvSolution", rc) is None:
            turb = "|".join(x for x in TURB_FIELDS.get(f.turbulence or "", []) if x != "nut")
            body = f"{{ p {val:g}; U {val:g};" + (f' "({turb})" {val:g};' if turb else "") + " }"
            gp.edits.append(_edit(case, "system/fvSolution", rc, body, insert=True))
        else:
            from aethercfd.foamdict.parser import parse
            from aethercfd.foamdict.semantic import DictView

            view = DictView(parse(G._read(case.root / "system/fvSolution") or ""))
            for k in view.keys(rc):
                gp.edits.append(_edit(case, "system/fvSolution", f"{rc}/{k}", f"{val:g}"))
        gp.notes.append(f"residualControl {val:g}")
        return
    if qid == "monitoring":
        if val and not _has_monitors(case):
            _add_monitors(case, f, fields, gp)
        elif not val and _has_monitors(case):
            gp.notes.append("monitoring function objects kept (remove them in Raw mode)")
        return
    if qid == "qoi_monitors":
        if val and not Q.has_monitors(case):
            qp = Q.plan(case, stored, f, fields)
            _function_edits(case, qp.objects, gp)
            gp.notes.append(f"integral monitors: {len(qp.qois)} quantities")
        elif not val and Q.has_monitors(case):
            gp.notes.append("integral monitors kept (remove them in Raw mode)")
        return
    gp.errors.append(f"{qid}: no writer")


def _field_rel(case: Case, name: str) -> str:
    from aethercfd.case.workflow import initial_dir

    d0 = initial_dir(case)
    region = case.regions[0].name if case.regions else ""
    base = d0.relative_to(case.root).as_posix() if d0 else "0"
    return f"{base}/{region}/{name}" if region else f"{base}/{name}"


def _set_nu(case: Case, rel: str, nu: float, gp: GuidedPlan) -> None:
    from aethercfd.templates.catalog import set_scalar_text

    cur = G._dict_value(case, rel, "nu")
    if cur is None:
        gp.edits.append(_edit(case, rel, "nu", f"{nu:g}", insert=True))
    else:
        nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", cur.split("]")[-1])
        if nums and math.isclose(float(nums[-1]), nu, rel_tol=1e-9):
            return
        gp.edits.append(_edit(case, rel, "nu", set_scalar_text(cur, f"{nu:g}")))
    gp.notes.append(f"ν = {nu:g} m²/s")


def _turb_inlet(case: Case, f: CaseFacts, fields, given: dict[str, float], gp: GuidedPlan, stored: dict,
                new_speed: dict[str, float] | None = None) -> None:
    """Intensity/length scale -> k, epsilon, omega, nuTilda at the inlets (and as the internal field)."""
    new_speed = new_speed or {}
    c = _Ctx(case, f, fields, G.reference(case, stored, fields), stored)
    ti, tl = turbulence_inlet_defaults(c)
    if ti is None or tl is None:
        ti = ti or 5.0
        tl = tl or (0.07 * c.ref.L if c.ref.L else None)
    ti = given.get("turb_intensity", ti)
    tl = given.get("turb_length", tl)
    if ti is None or tl is None or tl <= 0 or ti <= 0:
        gp.errors.append("give both a positive turbulence intensity and length scale")
        return
    from aethercfd.templates.catalog import set_scalar_text

    done_internal: set[tuple[str, str]] = set()
    for p, vec in _inlets(c):
        U = new_speed.get(p.name, math.sqrt(sum(x * x for x in vec)))
        k, eps, om = turbulence_values(U, ti, tl)
        vals = {"k": k, "epsilon": eps, "omega": om, "nuTilda": math.sqrt(1.5) * U * ti / 100.0 * tl}
        for name, v in vals.items():
            ff = fields.get(name)
            if ff is None or name not in TURB_FIELDS.get(f.turbulence or "", []):
                continue
            rel = _field_rel(case, name)
            cur = ff.get(f"boundaryField/{p.name}/value")
            if cur is not None and not cur.startswith("$"):  # "$internalField" follows the internal value
                old = G._uniform_scalar(cur)
                if old is None or not math.isclose(old, v, rel_tol=1e-3):
                    new = set_scalar_text(cur, f"{v:.5g}") if old is not None else f"uniform {v:.5g}"
                    gp.edits.append(_edit(case, rel, f"boundaryField/{p.name}/value", new))
            internal = ff.get("internalField")
            key = (rel, "internalField")
            old = G._uniform_scalar(internal) if internal is not None else None
            if old is not None and key not in done_internal:
                done_internal.add(key)
                if not math.isclose(old, v, rel_tol=1e-3):
                    gp.edits.append(_edit(case, rel, "internalField", set_scalar_text(internal, f"{v:.5g}")))
    inl = _inlets(c)
    k, eps, om = turbulence_values(new_speed.get(inl[0][0].name, math.sqrt(sum(x * x for x in inl[0][1]))), ti,
                                   tl) if inl else (0, 0, 0)
    if any(e["file"].replace("\\", "/").rsplit("/", 1)[-1] in ("k", "epsilon", "omega", "nuTilda")
           for e in gp.edits):
        gp.notes.append(f"inlet turbulence I={ti:g} %, ℓ={tl:g} m (k={k:.4g}, ε={eps:.4g}, ω={om:.4g})")


_WALL_FN = {"k": "kqRWallFunction", "epsilon": "epsilonWallFunction", "omega": "omegaWallFunction",
            "nut": "nutkWallFunction", "nuTilda": "fixedValue"}
_DIMS_TXT = {"k": "[0 2 -2 0 0 0 0]", "epsilon": "[0 2 -3 0 0 0 0]", "omega": "[0 0 -1 0 0 0 0]",
             "nut": "[0 2 -1 0 0 0 0]", "nuTilda": "[0 2 -1 0 0 0 0]"}


def make_turbulence_field(case: Case, name: str, values: dict[str, float], fields) -> str | None:
    """Text for a missing turbulence field, built from the case's 0/U patch layout: inlets fixedValue,
    walls the matching wall function, everything else zeroGradient (constraint patches keep their type)."""
    U = fields.get("U")
    if U is None:
        return None
    from aethercfd.case.workflow import CONSTRAINT_TYPES

    v = values.get(name, 0.0)
    lines = []
    region_patches = G.patches(case)
    inlet_names = {p.name for p, _ in _inlets(_Ctx(case, facts(case), fields, G.Reference(), {}))}
    for p in region_patches:
        if p.type in CONSTRAINT_TYPES:
            body = f"type {p.type};"
        elif p.type == "wall":
            if name == "nuTilda":
                body = "type fixedValue; value uniform 0;"
            elif name == "nut":
                body = "type nutkWallFunction; value uniform 0;"
            else:
                body = f"type {_WALL_FN[name]}; value uniform {v:.5g};"
        elif name == "nut":
            body = "type calculated; value uniform 0;"
        elif p.name in inlet_names:
            body = f"type fixedValue; value uniform {v:.5g};"
        else:
            body = f"type inletOutlet; inletValue uniform {v:.5g}; value uniform {v:.5g};"
        lines.append(f"    {p.name}\n    {{\n        " + body.replace("; ", ";\n        ") + "\n    }")
    internal = 0.0 if name == "nut" else v
    loc = _field_rel(case, name).rsplit("/", 1)[0]
    return ("/*--------------------------------*- C++ -*----------------------------------*\\\n"
            "  Created by AetherCFD (guided Models stage)\n"
            "\\*---------------------------------------------------------------------------*/\n"
            "FoamFile\n{\n    version     2.0;\n    format      ascii;\n    class       volScalarField;\n"
            f"    location    \"{loc}\";\n    object      {name};\n}}\n\n"
            f"dimensions      {_DIMS_TXT[name]};\n\ninternalField   uniform {internal:.5g};\n\n"
            "boundaryField\n{\n" + "\n".join(lines) + "\n}\n")


def _set_turbulence(case: Case, f: CaseFacts, fields, model: str, gp: GuidedPlan) -> None:
    rel = _turb_dict_rel(case)
    if rel is None:
        gp.errors.append("no constant/momentumTransport or turbulenceProperties to write the model into")
        return
    cur = f.turbulence or "laminar"
    if model == cur:
        return
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    view = DictView(parse(G._read(case.root / rel) or ""))
    if model == "laminar":
        gp.edits.append(_edit(case, rel, "simulationType", "laminar"))
        gp.notes.append("laminar")
        return
    gp.edits.append(_edit(case, rel, "simulationType", "RAS"))
    if view.dict_at("RAS") is None:
        key = "RASModel" if rel.endswith("turbulenceProperties") else "model"
        gp.edits.append(_edit(case, rel, "RAS", f"{{ {key} {model}; turbulence on; printCoeffs on; }}", insert=True))
    else:
        key = "RAS/model" if view.lookup("RAS/model") is not None else (
            "RAS/RASModel" if view.lookup("RAS/RASModel") is not None else
            "RAS/" + ("RASModel" if rel.endswith("turbulenceProperties") else "model"))
        gp.edits.append(_edit(case, rel, key, model, insert=True))
        if view.lookup("RAS/turbulence") is None:
            gp.edits.append(_edit(case, rel, "RAS/turbulence", "on", insert=True))
    gp.notes.append(f"turbulence {cur} → {model}")
    # missing fields: build them from the inlet turbulence (intensity/length of the current files, or 5 %, 0.07 L)
    c = _Ctx(case, f, fields, G.reference(case, load_state(case.root).get("answers", {}), fields), {})
    ti, tl = turbulence_inlet_defaults(c)
    inl = _inlets(c)
    U = math.sqrt(sum(x * x for x in inl[0][1])) if inl else (c.ref.U or 1.0)
    ti = ti or 5.0
    tl = tl or (0.07 * c.ref.L if c.ref.L else 0.01)
    k, eps, om = turbulence_values(U, ti, tl)
    vals = {"k": k, "epsilon": eps, "omega": om, "nut": 0.0, "nuTilda": math.sqrt(1.5) * U * ti / 100.0 * tl}
    for name in TURB_FIELDS.get(model, []):
        if name in fields or name not in _DIMS_TXT:
            continue
        text = make_turbulence_field(case, name, vals, fields)
        if text:
            gp.creates[str(case.root / _field_rel(case, name))] = text
            gp.notes.append(f"create 0/{name}")
    # solver + scheme coverage for the new model (same gate the Numerics stage enforces)
    probe = facts(case)
    probe.turbulence = model
    cov = G.coverage_gate(case, probe, {**fields, **{n: None for n in TURB_FIELDS.get(model, [])}})
    for e in cov.edits:
        gp.edits.append(_edit(case, e.file, e.key, e.value, insert=True))
    if cov.edits:
        gp.notes.append("add solver/scheme entries for the new fields")


def _set_schemes(case: Case, f: CaseFacts, fields, profile: str, gp: GuidedPlan) -> None:
    if profile == "keep":
        return
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    view = DictView(parse(G._read(case.root / "system/fvSchemes") or ""))
    b = "bounded " if f.steady else ""
    turb = [x for x in TURB_FIELDS.get(f.turbulence or "", []) if x != "nut"]
    want = {"div(phi,U)": f"{b}Gauss upwind" if profile == "robust" else f"{b}Gauss linearUpwind grad(U)"}
    for t in turb:
        want[f"div(phi,{t})"] = f"{b}Gauss upwind" if profile == "robust" else f"{b}Gauss limitedLinear 1"
    if f.compressible:
        for e in ("h", "e", "K"):
            if view.lookup(f"divSchemes/div(phi,{e})") is not None:
                want[f"div(phi,{e})"] = f"{b}Gauss upwind" if profile == "robust" else f"{b}Gauss limitedLinear 1"
    for k, v in want.items():
        lk = view.lookup(f"divSchemes/{k}")
        if lk is not None and lk.entry is not None and lk.entry.value is not None and \
                view.doc.text(lk.entry.value.start, lk.entry.value.end).strip() == v:
            continue
        gp.edits.append(_edit(case, "system/fvSchemes", f"divSchemes/{k}", v, insert=True, nonliteral=True))
    if profile == "robust" and (view.get_text("gradSchemes/default") or "").strip() == "Gauss linear":
        gp.edits.append(_edit(case, "system/fvSchemes", "gradSchemes/default", "cellLimited Gauss linear 1"))
    gp.notes.append(f"{profile} convection schemes")


def _add_monitors(case: Case, f: CaseFacts, fields, gp: GuidedPlan) -> None:
    # only fields the solver actually creates: a 0/omega left in a k-epsilon case is never read, and a function
    # object asking for it prints "Field omega not found" at every write
    model = set(TURB_FIELDS.get(f.turbulence or "", []))
    names = [n for n in ("U", "p", "p_rgh", "T", "k", "epsilon", "omega", "nut", "nuTilda")
             if n in fields and (n in model or n in ("U", "p", "p_rgh") or (n == "T" and f.compressible))]
    esi = Q.fork_is_esi(case)
    fl = f"({' '.join(names)})"
    solved = "(" + " ".join(n for n in names if n != "nut") + ")"
    fields_lib, utility_lib = Q.libs_text(esi, "fieldFunctionObjects"), Q.libs_text(esi, "utilityFunctionObjects")
    if esi:
        objs = {"aetherFieldMinMax": f"{{ type fieldMinMax; libs {fields_lib}; fields {fl}; mode magnitude; "
                                     "writeControl timeStep; writeInterval 1; log false; }",
                "aetherResiduals": f"{{ type solverInfo; libs {utility_lib}; fields {solved}; writeResidualFields no; }}"}
    else:  # Foundation 14 has no fieldMinMax ("Unknown function type"): volFieldValue min/max over all cells
        vfv = f"type volFieldValue; libs {fields_lib}; cellZone all; writeFields false; writeControl timeStep; " \
              "writeInterval 1; log false;"
        objs = {"aetherMin": f"{{ {vfv} operation min; fields {fl}; }}",
                "aetherMax": f"{{ {vfv} operation max; fields {fl}; }}",
                "aetherResiduals": f"{{ type residuals; libs {utility_lib}; fields {solved}; }}"}
    if f.turbulence not in (None, "laminar") and G._wall_patches(case):
        objs["aetherYPlus"] = f"{{ type yPlus; libs {fields_lib}; writeControl writeTime; }}"
    _function_edits(case, objs, gp)
    gp.notes.append("monitoring: " + ", ".join(objs))


def _function_edits(case: Case, objs: dict[str, str], gp: GuidedPlan) -> None:
    """Function objects into system/functions (Foundation, when it exists) or controlDict/functions."""
    if _functions_rel(case) == "system/functions":
        for k, v in objs.items():
            gp.edits.append(_edit(case, "system/functions", k, v, insert=True))
    elif G._dict_value(case, "system/controlDict", "functions") is not None or any(
            e["file"].endswith("controlDict") and e["key"] == "functions" for e in gp.edits):
        for k, v in objs.items():
            gp.edits.append(_edit(case, "system/controlDict", f"functions/{k}", v, insert=True))
    else:
        body = " ".join(f"{k} {v}" for k, v in objs.items())
        gp.edits.append(_edit(case, "system/controlDict", "functions", f"{{ {body} }}", insert=True))


# ============================================================================ marking done
def mark_params(case_root: Path, stage: str, done: bool, *, by: str = "user", note: str = "",
                state: dict | None = None) -> tuple[dict, str | None]:
    """dict.batch params that write the sidecar with the stage marked (or unmarked); refusal reason."""
    case = load_case(case_root)
    state = state if state is not None else load_state(case.root)
    stages = {s.id: s for s in assess(case, state)}
    st = stages.get(stage)
    if st is None:
        return {}, f"unknown stage {stage!r}"
    if done and not st.can_mark_done:
        if st.status == "na":
            return {}, f"{st.title} does not apply to this case"
        why = [f"{g.title}: {g.how or g.why}" for g in st.blocking] or [
            f"{TITLES[b]} has errors" for b in st.blocked_by]
        return {}, f"{st.title} cannot be marked done: " + "; ".join(why)
    sp = state_path(case.root)
    if sp is None:
        return {}, "marking stages needs an AetherCFD project (sidecar) for this case"
    new = json.loads(json.dumps(state))
    new["format"] = STATE_FORMAT
    entry = {"done": done, "at": datetime.now(UTC).isoformat(timespec="seconds"), "by": by,
             "fingerprint": st.fingerprint}
    if note:
        entry["note"] = note
    if st.warnings and done:
        entry["accepted_warnings"] = [g.title for g in st.warnings]
    new.setdefault("stages", {})[stage] = entry
    return {"edits": [], "replace": {str(sp): dump_state(new)},
            "summary": f"Workflow: {st.title} marked {'done' if done else 'not done'}"}, None


def overview(case: Case | str | Path, state: dict | None = None) -> dict:
    """Compact state for the copilot: every stage's status and next action, and the first thing to do."""
    case = case if isinstance(case, Case) else load_case(case)
    stages = assess(case, state)
    current = next((s for s in stages if s.status not in ("done", "na") and not s.optional), None)
    st = state if state is not None else load_state(case.root)
    return {"case": str(case.root), "solver": case.solver_label,
            "reference": G.reference(case, st.get("answers", {})).as_dict(),
            "answers": st.get("answers", {}),
            "current_stage": current.id if current else None,
            "next_action": f"{current.title}: {current.next_action}" if current else "All stages done.",
            "stages": [s.as_dict(case.root, detail=False) for s in stages]}
