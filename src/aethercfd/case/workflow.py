"""The staged workflow as a *view of real files*: each stage lists the files that define it, a status,
and concrete issues. Nothing here is a parallel model of the case — it is read from disk."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.case.model import Case
from aethercfd.core.textio import decode_bytes
from aethercfd.foamdict.cst import Directive, Entry
from aethercfd.foamdict.parser import parse

CONSTRAINT_TYPES = {"empty", "wedge", "symmetryPlane", "symmetry", "cyclic", "cyclicAMI", "cyclicACMI",
                    "processor", "processorCyclic", "cyclicSlip", "nonConformalCyclic"}


@dataclass
class Stage:
    id: str
    title: str
    status: str  # ok | warn | todo | na | info
    summary: str
    files: list[Path] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _existing(root: Path, *rels: str) -> list[Path]:
    return [root / r for r in rels if (root / r).exists()]


def initial_dir(case: Case) -> Path | None:
    for name in ("0", "0.orig"):
        if (case.root / name).is_dir():
            return case.root / name
    return case.root / case.times[0][1] if case.times else None


def bc_consistency(case: Case) -> list[str]:
    """Every non-constraint mesh patch must have a boundary condition in every initial field."""
    issues: list[str] = []
    d0 = initial_dir(case)
    if d0 is None or not case.regions:
        return issues
    for region in case.regions:
        fdir = d0 / region.name if region.name else d0
        if not fdir.is_dir() or not region.patches:
            continue
        for f in sorted(p for p in fdir.iterdir() if p.is_file() and not p.name.startswith(".")):
            try:
                text, _ = decode_bytes(f.read_bytes())
            except OSError:
                continue
            if "boundaryField" not in text:
                continue
            doc = parse(text)
            if not doc.ok:
                d = next(x for x in doc.diagnostics if x.severity == "error")
                where = f"{region.name}/" if region.name else ""
                issues.append(f"{where}{f.name}: syntax error at line {doc.line_col(d.start)[0]} ({d.message}); "
                              "OpenFOAM will not read this file")
                continue
            bf = next((m for m in doc.root.members if isinstance(m, Entry) and m.key.name == "boundaryField"), None)
            if bf is None or bf.dict is None:
                continue
            exact, patterns, constraints_included = set(), [], False
            for m in bf.dict.members:
                if isinstance(m, Entry):
                    (patterns.append(m.key.name) if m.key.is_pattern else exact.add(m.key.name))
                elif isinstance(m, Directive) and "setConstraintTypes" in m.args_text:
                    constraints_included = True
            missing = []
            for p in region.patches:
                if p.name in exact or any(_full(rx, p.name) for rx in patterns):
                    continue
                if p.type == "empty" or p.n_faces == 0:
                    continue  # OpenFOAM creates empty / zero-size patch fields itself
                if p.type in CONSTRAINT_TYPES and constraints_included:
                    continue
                if any(g in exact for g in p.in_groups):
                    continue
                missing.append(p.name)
            if missing:
                where = f"{region.name}/" if region.name else ""
                issues.append(f"{where}{f.name}: no boundary condition for patch(es) {', '.join(missing)}")
    return issues


def _full(rx: str, name: str) -> bool:
    try:
        return re.fullmatch(rx, name) is not None
    except re.error:
        return False


def assess(case: Case) -> list[Stage]:
    r = case.root
    stages: list[Stage] = []
    geo = _existing(r, "constant/triSurface", "constant/geometry")
    stages.append(Stage("geometry", "Geometry", "ok" if geo else "na",
                        "surface files present" if geo else "no CAD surfaces (block-structured or imported mesh)", geo))
    dom = _existing(r, "system/blockMeshDict", "system/snappyHexMeshDict", "system/extrudeMeshDict",
                    "system/meshDict", "constant/polyMesh/blockMeshDict")
    stages.append(Stage("domain", "Domain", "ok" if dom else "info",
                        ", ".join(p.name for p in dom) or "domain defined by an imported mesh", dom))
    mat = _existing(r, "constant/thermophysicalProperties", "constant/transportProperties",
                    "constant/physicalProperties", "constant/thermophysicalTransport")
    mat += [p for p in (r / "constant").glob("*/thermophysicalProperties")] if (r / "constant").is_dir() else []
    stages.append(Stage("materials", "Materials", "ok" if mat else "todo",
                        ", ".join(sorted({p.name for p in mat})) or "no material properties found", mat))
    stages.append(Stage("physics", "Physics", "ok" if (case.application or case.solver_module) else "todo",
                        case.solver_label, _existing(r, "system/controlDict")))
    mesh_ok = any((reg.mesh_dir / "owner").exists() for reg in case.regions)
    issues = []
    if not mesh_ok:
        issues.append("mesh not generated yet (run blockMesh / snappyHexMesh, or it lives in a staged copy)")
    stages.append(Stage("mesh", "Mesh", "ok" if mesh_ok else "todo",
                        f"{case.n_cells:,} cells" if case.n_cells else "no mesh",
                        [reg.mesh_dir for reg in case.regions if reg.mesh_dir.exists()], issues))
    models = _existing(r, "constant/turbulenceProperties", "constant/momentumTransport",
                       "constant/combustionProperties", "constant/chemistryProperties", "constant/radiationProperties",
                       "constant/fvOptions", "system/fvOptions", "constant/fvModels", "system/fvModels",
                       "constant/fvConstraints", "system/fvConstraints")
    stages.append(Stage("models", "Models", "ok" if models else "info",
                        ", ".join(p.name for p in models) or "laminar / no extra models", models))
    d0 = initial_dir(case)
    bc_issues = bc_consistency(case)
    stages.append(Stage("bc", "Boundary & Initial Conditions",
                        "warn" if bc_issues else ("ok" if d0 else "todo"),
                        f"{d0.name}/ with {len([p for p in d0.rglob('*') if p.is_file()])} field files" if d0 else
                        "no initial conditions", [d0] if d0 else [], bc_issues))
    num = _existing(r, "system/fvSchemes", "system/fvSolution")
    stages.append(Stage("numerics", "Numerics", "ok" if len(num) == 2 else "todo",
                        "fvSchemes + fvSolution" if len(num) == 2 else "fvSchemes/fvSolution missing", num))
    logs = sorted(r.glob("log.*"))
    results = [t for _, t in case.times if t not in ("0",)]
    stages.append(Stage("run", "Run", "ok" if results else ("info" if logs else "todo"),
                        f"{len(results)} result time(s), latest {case.latest_time}" if results else "not run yet",
                        logs[-6:]))
    stages.append(Stage("validation", "Validation", "info",
                        "pre-run checks: BC consistency above; checkMesh in Mesh Quality (more in M2)"))
    post = _existing(r, "postProcessing")
    stages.append(Stage("post", "Post-processing", "ok" if post else "info",
                        "function-object output present" if post else "no postProcessing output", post))
    stages.append(Stage("ml", "ML / Data Assimilation", "na", "planned (M5): surrogates, PINNs, CFDMI"))
    return stages
