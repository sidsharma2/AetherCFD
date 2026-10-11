"""Grid-convergence study: systematically refined meshes, Richardson extrapolation and the Grid Convergence
Index (GCI), so a result is reported with its discretisation uncertainty.

The procedure follows Celik et al., "Procedure for estimation and reporting of uncertainty due to
discretization in CFD applications", J. Fluids Eng. 130 (2008) 078001 (the ASME V&V 20 / JFE policy):

* three meshes with a refinement ratio r = h_coarse/h_fine > 1.3, h = (V/N)^(1/d);
* the observed order p from the three solutions (fixed-point iteration when the ratios differ);
* the extrapolated value φ_ext, the relative errors e_a, e_ext and GCI_fine = 1.25 e_a / (r^p − 1);
* the convergence type (monotonic 0 < R < 1, oscillatory R < 0, divergent R > 1) and the asymptotic-range
  check GCI_32 / (r^p GCI_21) ≈ 1.

The levels are NEW cases inside the study folder ``<case>/grid_study/<level>/`` (visible in Case Files,
journalled like any created file): the case's inputs copied, with blockMeshDict's cell counts scaled by the
refinement ratio (grading ratios are total expansions, so they carry over unchanged). The open case itself is
the medium level. Nothing here writes or runs anything: it plans (files.write / run.start parameters) and
evaluates finished results. No Qt here.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aethercfd.case.model import Case, is_time_dir, load_case

STUDY_DIR = "grid_study"
RECORD = "grid_study.json"
DEFAULT_RATIO = 1.5
SAFETY = 1.25  # Fs for three-grid studies (Roache)
_SKIP_TOP = {STUDY_DIR, ".aethercfd", "postProcessing", "logs", "VTK", "dynamicCode", "__pycache__", ".git"}
_NOT_QOI = {"solverInfo", "residuals", "fieldMinMax", "yPlus", "continuityError", "Co", "CourantNo"}
_HEX = re.compile(r"(hex\s*\([\d\s]+\)\s*(?:[A-Za-z_]\w*\s*)?)\(\s*([^()]*?)\s*\)", re.S)


class GridStudyError(ValueError):
    pass


# ============================================================================ refined meshes
def scale_block_mesh(text: str, factor: float, *, two_d: bool) -> tuple[str, list[tuple[tuple, tuple]]]:
    """blockMeshDict text with every block's cell counts multiplied by ``factor`` (rounded, ≥ 1); in a 2D
    case the single cell of the empty direction stays 1. Returns (text, [(old counts, new counts)])."""
    changes: list[tuple[tuple, tuple]] = []

    def repl(m: re.Match) -> str:
        parts = m.group(2).split()
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise GridStudyError(f"block cell counts '({m.group(2).strip()})' are not three integers (macros or "
                                 "#calc): scale them by hand, or refine with refineMesh")
        old = tuple(int(p) for p in parts)
        new = tuple(n if (n == 1 and two_d) else max(1, round(n * factor)) for n in old)
        changes.append((old, new))
        return f"{m.group(1)}({' '.join(str(n) for n in new)})"

    blocks = re.search(r"\bblocks\s*\(", text)
    if blocks is None:
        raise GridStudyError("blockMeshDict has no blocks list")
    head, tail = text[:blocks.end()], text[blocks.end():]
    new_tail, n = _HEX.subn(repl, tail)
    if n == 0:
        raise GridStudyError("no 'hex (...) (nx ny nz)' blocks found in blockMeshDict")
    return head + new_tail, changes


def _is_2d(case: Case, bmd_text: str) -> bool:
    if case.regions and any(p.type == "empty" for p in case.regions[0].patches):
        return True
    return bool(re.search(r"\btype\s+empty\s*;", bmd_text))


def input_files(case_root: Path) -> list[str]:
    """Case-relative files a level needs: system/, constant/ (without the mesh), the initial time folder,
    scripts; never results, logs, processor folders, the sidecar or another study."""
    root = Path(case_root)
    out: list[str] = []
    times = sorted((float(p.name), p.name) for p in root.iterdir() if p.is_dir() and is_time_dir(p.name))
    first = times[0][1] if times else None
    for p in sorted(root.iterdir()):
        name = p.name
        if name in _SKIP_TOP or name.startswith(("processor", "log.", ".")):
            continue
        if p.is_dir() and is_time_dir(name) and name != first:
            continue  # results
        if p.is_file():
            if name.endswith((".foam", ".OpenFOAM")):
                continue
            out.append(name)
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            if "/polyMesh/" in f"/{rel}" and not rel.endswith("polyMesh/blockMeshDict"):
                continue  # the level makes its own mesh
            if "/uniform/" in f"/{rel}/" and name == first and first != "0":
                continue
            out.append(rel)
    return out


@dataclass
class LevelPlan:
    name: str
    factor: float  # cell-count factor per direction relative to the base case
    cells_estimate: int
    blocks: list[tuple[tuple, tuple]]


def plan_levels(case: Case | str | Path, ratio: float = DEFAULT_RATIO) -> tuple[dict, list[LevelPlan], list[str]]:
    """(files.write params, level plans, notes) for the coarse (1/r) and fine (r) levels of a study around
    the open case (the medium level)."""
    case = case if isinstance(case, Case) else load_case(case)
    if not 1.1 <= ratio <= 3.0:
        raise GridStudyError("refinement ratio should be between 1.1 and 3 (Celik et al. recommend > 1.3)")
    bmd = case.root / "system" / "blockMeshDict"
    if not bmd.is_file():
        raise GridStudyError("the study refines blockMesh meshes (system/blockMeshDict); this case has none. For "
                             "imported meshes run refineMesh in copies by hand (Raw mode) for now")
    if (case.root / STUDY_DIR).exists() and any((case.root / STUDY_DIR).iterdir()):
        raise GridStudyError(f"{STUDY_DIR}/ already exists: evaluate it, or move it away for a new study")
    text = bmd.read_text(encoding="utf-8", errors="replace")
    two_d = _is_2d(case, text)
    base_cells = case.n_cells
    rels = input_files(case.root)
    from aethercfd.case.checks import facts

    steady = facts(case).steady
    fine_iters = ratio ** (2 if two_d else 3)
    extra_note = ""
    params: dict = {"root": str(case.root / STUDY_DIR), "files": {}, "copies": {}}
    plans: list[LevelPlan] = []
    for name, factor in (("coarse", 1.0 / ratio), ("fine", ratio)):
        scaled, changes = scale_block_mesh(text, factor, two_d=two_d)
        cells = sum(a * b * c for _o, (a, b, c) in changes)
        plans.append(LevelPlan(name, factor, cells, changes))
        for rel in rels:
            if rel == "system/blockMeshDict":
                continue
            params["copies"][f"{name}/{rel}"] = str(case.root / rel)
        params["files"][f"{name}/system/blockMeshDict"] = scaled
        if name == "fine" and steady:
            cd = (case.root / "system" / "controlDict").read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^(\s*endTime\s+)(\d+(?:\.\d*)?)(\s*;)", cd, re.M)
            if m:
                more = math.ceil(float(m.group(2)) * fine_iters)
                params["files"][f"{name}/system/controlDict"] = cd[:m.start(2)] + str(more) + cd[m.end(2):]
                params["copies"].pop(f"{name}/system/controlDict", None)
                extra_note = (f"fine level: endTime {m.group(2)} → {more} iterations (a finer mesh needs more "
                              "SIMPLE iterations to converge; residualControl still stops it early)")
    notes = [f"base (medium) mesh: {base_cells:,} cells" if base_cells else "base case has no mesh yet: run it first",
             f"{'2D' if two_d else '3D'}: h = (V/N)^(1/{2 if two_d else 3}); target ratio r = {ratio:g}"]
    if extra_note:
        notes.append(extra_note)
    if (case.root / "system" / "snappyHexMeshDict").is_file():
        notes.append("snappyHexMesh case: the background mesh is scaled, so the refinement levels follow; check "
                     "that layer thicknesses are relative (relativeSizes true), else the near-wall cells do not "
                     "refine with the rest")
    return params, plans, notes


def levels(case_root: Path) -> list[tuple[str, Path]]:
    """(name, root) fine → coarse: fine, medium (the case itself), coarse, for the levels that exist."""
    root = Path(case_root)
    out = []
    if (root / STUDY_DIR / "fine" / "system").is_dir():
        out.append(("fine", root / STUDY_DIR / "fine"))
    out.append(("medium", root))
    if (root / STUDY_DIR / "coarse" / "system").is_dir():
        out.append(("coarse", root / STUDY_DIR / "coarse"))
    return out


# ============================================================================ quantities of interest
def patch_means(case_root: Path, fields: tuple[str, ...] = ("p", "p_rgh", "T")) -> dict[str, float]:
    """Area-weighted mean of scalar fields on every open (non-wall, non-constraint) patch, latest time."""
    import vtk
    from vtk.util import numpy_support as nps

    from aethercfd.case.workflow import CONSTRAINT_TYPES
    from aethercfd.results.foam_reader import FoamResults, iter_leaves

    case = load_case(case_root)
    if not case.latest_result_time or not case.regions:
        return {}
    open_patches = [p.name for p in case.regions[0].patches
                    if p.type not in CONSTRAINT_TYPES and p.type != "wall" and (p.n_faces or 0) > 0]
    fr = FoamResults(case_root)
    meta = fr.refresh()
    names = {n.rsplit("/", 1)[-1]: n for n in meta.patches}
    want = [names[p] for p in open_patches if p in names]
    if not want:
        return {}
    present = [f for f in fields if f in meta.cell_arrays]
    ds = fr.read(float(case.latest_result_time), fields=present, patches=want)
    out: dict[str, float] = {}
    for path, leaf in iter_leaves(ds):
        patch = path.rsplit("/", 1)[-1]
        if patch not in open_patches:
            continue
        sz = vtk.vtkCellSizeFilter()
        sz.SetInputData(leaf)
        sz.SetComputeArea(True)
        sz.SetComputeVolume(False)
        sz.SetComputeLength(False)
        sz.SetComputeVertexCount(False)
        sz.Update()
        area = nps.vtk_to_numpy(sz.GetOutput().GetCellData().GetArray("Area"))
        if area.sum() <= 0:
            continue
        cd = leaf.GetCellData()
        for f in present:
            arr = cd.GetArray(f)
            if arr is None or arr.GetNumberOfComponents() != 1:
                continue
            v = nps.vtk_to_numpy(arr).astype(float)
            out[f"mean {f} on {patch}"] = float((v * area).sum() / area.sum())
    return out


def dat_values(case_root: Path) -> dict[str, float]:
    """Last value of every scalar column of the function-object output (forces, forceCoeffs,
    surfaceFieldValue, ...): postProcessing/<object>/<time>/<file>.dat, newest time folder."""
    pp = Path(case_root) / "postProcessing"
    out: dict[str, float] = {}
    if not pp.is_dir():
        return out
    for obj in sorted(p for p in pp.iterdir() if p.is_dir()):
        tdirs = sorted((float(t.name), t) for t in obj.iterdir() if t.is_dir() and is_time_dir(t.name))
        if not tdirs:
            continue
        for dat in sorted(tdirs[-1][1].glob("*.dat")):
            # not solution quantities: residuals, field extremes (mesh-dependent by nature), y+ (a mesh property)
            if dat.stem in _NOT_QOI or dat.stat().st_size > 64 * 2**20:
                continue
            header, last = None, None
            with dat.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith("#"):
                        header = s.lstrip("#").split()
                    else:
                        last = s
            if last is None:
                continue
            vals = last.replace("(", " ").replace(")", " ").split()
            cols = header if header and len(header) == len(vals) else [f"col{i}" for i in range(len(vals))]
            for c, v in list(zip(cols, vals, strict=False))[1:]:  # first column is the time
                try:
                    x = float(v)
                except ValueError:
                    continue
                if math.isfinite(x):
                    out[f"{obj.name}/{dat.stem}:{c}"] = x
    return out


def quantities(case_root: Path) -> dict[str, float]:
    q = patch_means(case_root)
    q.update(dat_values(case_root))
    return q


# ============================================================================ Richardson / GCI
@dataclass
class GciResult:
    quantity: str
    phi: tuple[float, float, float]  # fine, medium, coarse
    cells: tuple[int, int, int]
    r21: float
    r32: float
    convergence: str  # monotonic | oscillatory | divergent | converged
    p: float | None = None
    phi_ext: float | None = None
    e_a21: float | None = None
    e_ext21: float | None = None
    gci_fine: float | None = None  # relative (0.02 = 2 %)
    gci_coarse: float | None = None
    asymptotic: float | None = None  # ≈ 1 in the asymptotic range
    note: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: (float(f"{v:.6g}") if isinstance(v, float) else v) for k, v in d.items()}


def gci(quantity: str, phi: tuple[float, float, float], cells: tuple[int, int, int], dim: int,
        volume: float = 1.0) -> GciResult:
    """Celik et al. (2008) three-grid procedure. phi/cells are ordered fine, medium, coarse."""
    f1, f2, f3 = phi
    n1, n2, n3 = cells
    h1, h2, h3 = ((volume / n) ** (1.0 / dim) for n in (n1, n2, n3))
    r21, r32 = h2 / h1, h3 / h2
    res = GciResult(quantity, phi, cells, r21, r32, "converged")
    e21, e32 = f2 - f1, f3 - f2
    scale = max(abs(f1), abs(f2), abs(f3), 1e-300)
    if abs(e21) <= 1e-12 * scale and abs(e32) <= 1e-12 * scale:
        res.p, res.phi_ext, res.e_a21, res.e_ext21, res.gci_fine = None, f1, 0.0, 0.0, 0.0
        res.note = "identical on all three meshes"
        return res
    if abs(e21) <= 1e-12 * scale or abs(e32) <= 1e-12 * scale:
        res.convergence = "oscillatory"
        res.note = "two meshes give the same value: the observed order is undefined"
        res.gci_fine = SAFETY * abs(f3 - f1) / max(abs(f1), 1e-300)
        return res
    R = e21 / e32
    res.convergence = "monotonic" if 0 < R < 1 else "oscillatory" if R < 0 else "divergent"
    s = 1.0 if e32 / e21 > 0 else -1.0
    p = abs(math.log(abs(e32 / e21))) / math.log(r21)
    for _ in range(100):
        try:
            q = math.log((r21**p - s) / (r32**p - s))
        except (ValueError, ZeroDivisionError):
            break
        p_new = abs(math.log(abs(e32 / e21)) + q) / math.log(r21)
        if abs(p_new - p) < 1e-10:
            p = p_new
            break
        p = p_new
    res.p = p
    if res.convergence == "divergent":
        res.note = "the change grows with refinement: not in the asymptotic range (or not iteratively converged)"
    if res.convergence == "oscillatory":
        # Richardson extrapolation does not apply; bound the uncertainty by the spread (Roache's advice)
        res.gci_fine = SAFETY * abs(max(phi) - min(phi)) / max(abs(f1), 1e-300) / 2.0
        res.note = "oscillatory convergence: uncertainty estimated from the spread of the three solutions"
        return res
    rp = r21**p
    if rp - 1.0 <= 1e-12:
        res.note = "observed order ≈ 0"
        return res
    res.phi_ext = (rp * f1 - f2) / (rp - 1.0)
    if abs(f1) > 1e-300:
        res.e_a21 = abs((f1 - f2) / f1)
        res.gci_fine = SAFETY * res.e_a21 / (rp - 1.0)
    if abs(res.phi_ext) > 1e-300:
        res.e_ext21 = abs((res.phi_ext - f1) / res.phi_ext)
    if abs(f2) > 1e-300 and r32**p - 1.0 > 1e-12:
        res.gci_coarse = SAFETY * abs((f2 - f3) / f2) / (r32**p - 1.0)
        if res.gci_fine:
            res.asymptotic = res.gci_coarse / (rp * res.gci_fine)
    return res


# ============================================================================ evaluation + record
def record_path(case_root: Path) -> Path | None:
    from aethercfd.project.project import Project

    proj = Project.find(case_root)
    return proj.local / RECORD if proj is not None else None


def load_record(case_root: Path) -> dict | None:
    p = record_path(case_root)
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class LevelState:
    name: str
    root: str
    cells: int | None
    latest_time: str | None
    finished: bool
    converged: bool | None
    note: str = ""


def level_states(case_root: Path) -> list[LevelState]:
    from aethercfd.case.checks import facts
    from aethercfd.case.gates import latest_solver_log, run_summary

    out = []
    for name, root in levels(case_root):
        c = load_case(root)
        log = latest_solver_log(root)
        finished, converged, note = False, None, ""
        if log is not None:
            s = run_summary(log)
            finished = s.finished and not s.fatal
            if s.fatal:
                note = "failed: " + s.fatal[0][:120]
            if facts(c).steady:
                converged = bool(s.converged_msg) or (bool(s.residuals) and all(
                    last < 1e-3 for _f, last in s.residuals.values()))
        elif name != "medium":
            note = "not run yet"
        out.append(LevelState(name, str(root), c.n_cells, c.latest_result_time, finished, converged, note))
    return out


def _volume(case_root: Path) -> float:
    try:
        from aethercfd.mesh.polymesh import read_polymesh
        from aethercfd.mesh.quality import geometry

        return float(geometry(read_polymesh(Path(case_root) / "constant" / "polyMesh")).cell_volumes.sum())
    except Exception:
        return 1.0


def evaluate(case_root: Path) -> dict:
    """Read the three levels' results and compute the GCI for every quantity they share."""
    case_root = Path(case_root)
    states = level_states(case_root)
    by = {s.name: s for s in states}
    missing = [n for n in ("fine", "medium", "coarse") if n not in by]
    if missing:
        raise GridStudyError(f"levels missing: {', '.join(missing)} (create the study first)")
    not_run = [s.name for s in states if not s.finished or not s.cells or not s.latest_time]
    if not_run:
        raise GridStudyError(f"levels without a finished run: {', '.join(not_run)}")
    q = {s.name: quantities(Path(s.root)) for s in states}
    shared = [k for k in q["medium"] if k in q["fine"] and k in q["coarse"]
              and max(abs(q[n][k]) for n in ("fine", "medium", "coarse")) > 1e-10]  # identically zero: nothing to say
    if not shared:
        raise GridStudyError("no quantity of interest found on all three levels (patch means of p/T, or "
                             "postProcessing .dat values)")
    case = load_case(case_root)
    dim = 2 if case.regions and case.regions[0].is_2d else 3
    vol = _volume(case_root)
    cells = (by["fine"].cells, by["medium"].cells, by["coarse"].cells)
    results = [gci(k, (q["fine"][k], q["medium"][k], q["coarse"][k]), cells, dim, vol) for k in shared]
    from aethercfd.case.gates import yplus_from_results
    from aethercfd.case.guided import fingerprint

    lv = [asdict(s) for s in states]
    for d in lv:  # wall y+ per level: with wall functions it must stay in the log layer on every mesh
        yp = yplus_from_results(load_case(d["root"]))
        d["yplus_mean"] = (sum(m for _a, m, _b in yp.values()) / len(yp)) if yp else None
    nut = _nut_wall_type(case)
    return {"format": 1, "evaluated_at": datetime.now(UTC).isoformat(timespec="seconds"), "dim": dim,
            "levels": lv, "nut_wall": nut,
            "medium_mesh": fingerprint([case_root / "constant" / "polyMesh"]),
            "results": [r.as_dict() for r in results]}


def _nut_wall_type(case: Case) -> str | None:
    from aethercfd.case.gates import _wall_patches, initial_fields

    nut = initial_fields(case).get("nut")
    walls = _wall_patches(case)
    return nut.patch_entry(walls[0], "type") if nut is not None and walls else None


def record_params(case_root: Path, record: dict) -> dict:
    """dict.batch parameters that store an evaluation in the project sidecar (journalled)."""
    p = record_path(case_root)
    if p is None:
        raise GridStudyError("storing the study needs an AetherCFD project for this case")
    return {"replace": {str(p): json.dumps(record, indent=2, default=str) + "\n"},
            "summary": f"Grid study evaluated: {len(record.get('results', []))} quantities"}


def run_params(case_root: Path) -> list[dict]:
    """run.start parameters for each study level that has not run yet (mesh + check + solve)."""
    out = []
    for s in level_states(case_root):
        if s.name == "medium" or s.finished:
            continue
        out.append({"case": s.root, "preset": "mesh-check-solve"})
    return out


def continue_params(case_root: Path, *, extra: int | None = None) -> list[tuple[dict, dict]]:
    """For every study level (not the medium case) that finished without converging iteratively:
    (dict.batch params that make it continue from its latest time for ``extra`` more iterations, default its
    own endTime again, with startFrom latestTime; run.start params that solve it). A plain re-run would
    start from startTime again and throw the iterations away."""
    from aethercfd.case.gates import _dict_value

    out = []
    for s in level_states(case_root):
        if s.name == "medium" or not s.finished or s.converged is not False or not s.latest_time:
            continue
        level = load_case(s.root)
        end = _dict_value(level, "system/controlDict", "endTime")
        try:
            n = int(extra or float(end or 1000))
        except ValueError:
            n = 1000
        new_end = float(s.latest_time) + n
        cd = str(Path(s.root) / "system" / "controlDict")
        edits = {"edits": [{"file": cd, "key": "startFrom", "value": "latestTime"},
                           {"file": cd, "key": "endTime", "value": f"{new_end:g}"}],
                 "summary": f"Grid study: continue {s.name} from {s.latest_time} to {new_end:g} (not converged)"}
        out.append((edits, {"case": s.root, "preset": "solve"}))
    return out


# ============================================================================ the gate
@dataclass
class StudyVerdict:
    gate: object
    result: dict | None = None
    choices: list[str] = field(default_factory=list)


def g_over(r: dict, tol: float) -> bool:
    return r.get("gci_fine") is not None and r["gci_fine"] > tol


def study_gate(case: Case, answers: dict) -> StudyVerdict:
    from aethercfd.case.gates import Gate

    root = case.root
    rec = load_record(root)
    lv = {n for n, _ in levels(root)}
    tol = float(answers.get("gci_tolerance") or 5.0) / 100.0
    how_create = ("Grid study: create the coarse and fine levels (Validation actions), run them, then evaluate. "
                  "The GCI says how far the answer still depends on the mesh.")
    if rec is None:
        if {"fine", "coarse"} <= lv:
            return StudyVerdict(Gate("grid-study", False, "warn", "Grid study created but not evaluated",
                                     "The coarse and fine levels exist; run them and evaluate the study.",
                                     "Validation actions: run the grid levels, then evaluate."))
        return StudyVerdict(Gate("grid-study", False, "warn", "Mesh independence not shown",
                                 "A result from a single mesh carries an unknown discretisation error; reporting it "
                                 "without a grid study is the most common CFD validation mistake.", how_create))
    from aethercfd.case.guided import fingerprint

    results = rec.get("results", [])
    names = [r["quantity"] for r in results]
    if rec.get("medium_mesh") != fingerprint([root / "constant" / "polyMesh"]):
        return StudyVerdict(Gate("grid-study", False, "warn", "The mesh changed after the grid study",
                                 "The study was made around a different medium mesh.", "Repeat the study."),
                            None, names)
    want = answers.get("grid_qoi") or (names[0] if names else None)
    r = next((x for x in results if x["quantity"] == want), results[0] if results else None)
    if r is None:
        return StudyVerdict(Gate("grid-study", False, "warn", "Grid study has no quantities", "", how_create), None,
                            names)
    ev = [f"{r['quantity']}: fine/medium/coarse = {r['phi'][0]:.6g} / {r['phi'][1]:.6g} / {r['phi'][2]:.6g} "
          f"({r['cells'][0]:,} / {r['cells'][1]:,} / {r['cells'][2]:,} cells, r21 {r['r21']:.3g}, r32 {r['r32']:.3g})"]
    if r.get("p") is not None:
        ev.append(f"observed order p = {r['p']:.3g}" + (f", φ_ext = {r['phi_ext']:.6g}" if r.get("phi_ext") is not
                                                         None else ""))
    if r.get("gci_fine") is not None:
        ev.append(f"GCI_fine = {100 * r['gci_fine']:.3g} %" + (
            f", GCI_coarse = {100 * r['gci_coarse']:.3g} %" if r.get("gci_coarse") is not None else "") + (
            f", asymptotic-range ratio {r['asymptotic']:.3g} (≈ 1 expected)" if r.get("asymptotic") else ""))
    unconv = [lvl["name"] for lvl in rec.get("levels", []) if lvl.get("converged") is False]
    if unconv:
        ev.append(f"not iteratively converged: {', '.join(unconv)} (iterative error contaminates the GCI)")
    yp = {lvl["name"]: lvl.get("yplus_mean") for lvl in rec.get("levels", []) if lvl.get("yplus_mean")}
    wall_fn = (rec.get("nut_wall") or "") in ("nutkWallFunction", "nutUWallFunction", "nutWallFunction")
    # wall functions whose first cell slides out of the log layer as the mesh refines
    yplus_drift = (wall_fn and {"fine", "coarse"} <= set(yp) and yp["fine"] < 30
                   and yp["coarse"] / max(yp["fine"], 1e-9) > 1.5)
    if yp:
        ev.append("mean wall y+ coarse/medium/fine = " + " / ".join(
            f"{yp[n]:.3g}" for n in ("coarse", "medium", "fine") if n in yp) + (f" ({rec.get('nut_wall')})"
                                                                               if rec.get("nut_wall") else ""))
    y_how = (" With wall functions the first cell must stay in the log layer (y+ > 30) on EVERY mesh: here y+ "
             "falls into the buffer layer as the mesh refines, so the wall model changes regime and the solution "
             "cannot converge with the mesh. Refine away from the wall only (keep the first cell height), or use a "
             "y+-insensitive treatment (nutUSpaldingWallFunction, or k-ω SST resolved to y+ ≈ 1)."
             if yplus_drift else "")
    if unconv and (r["convergence"] != "monotonic" or g_over(r, tol)):
        return StudyVerdict(Gate("grid-study", False, "error", "Grid study inconclusive: "
                                 f"{', '.join(unconv)} not iteratively converged",
                                 "A level's iterative error is as large as the mesh effect, so the three solutions "
                                 "cannot be compared.", f"Run {', '.join(unconv)} longer (raise endTime, or tighten "
                                 "residualControl) and evaluate again." + y_how, ev), r, names)
    if r["convergence"] == "divergent":
        return StudyVerdict(Gate("grid-study", False, "error", "Grid study diverges",
                                 f"{r['quantity']} changes MORE between the finer meshes than between the coarser "
                                 "ones: the meshes are not in the asymptotic range.",
                                 "Refine further (a finer medium mesh)." + y_how, ev), r, names)
    g = r.get("gci_fine")
    if g is not None and g > tol:
        return StudyVerdict(Gate("grid-study", False, "error", f"Discretisation uncertainty {100 * g:.3g} % > "
                                 f"{100 * tol:g} %",
                                 f"The fine-mesh answer for {r['quantity']} is uncertain by ±{100 * g:.3g} % because "
                                 "of the mesh alone.", "Refine the mesh (and repeat the study), or accept a larger "
                                                       "tolerance if this quantity does not need it." + y_how, ev),
                            r, names)
    p = r.get("p")
    if r["convergence"] == "oscillatory" or unconv or (p is not None and not 0.5 <= p <= 4.0):
        why = ("oscillatory convergence" if r["convergence"] == "oscillatory" else
               "a level is not iteratively converged" if unconv else f"observed order p = {p:.3g} is far from "
               "the scheme's formal order (~1-2)")
        return StudyVerdict(Gate("grid-study", False, "warn", "Grid study within tolerance, but " + why,
                                 "The GCI estimate is less reliable outside monotonic, asymptotic convergence.",
                                 "Converge the levels further, or use a larger refinement ratio.", ev), r, names)
    return StudyVerdict(Gate("grid-study", True, "info", f"Mesh-independent within {100 * (g or 0):.3g} % "
                             f"({r['quantity']})", evidence=ev), r, names)
