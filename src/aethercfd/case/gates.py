"""Correctness gates for the guided workflow: the checks a stage must pass before it can be marked done.

A gate is a finding with a verdict: ``ok`` or not, a severity (``error`` blocks the stage, ``warn`` is
shown but does not block), WHY it matters physically/numerically, HOW to fix it, the evidence it was
read from, and, where a fix is safe and local, the exact dictionary edits (applied as one journalled
``dict.batch`` plan). Everything is read from the case on disk; nothing here writes. No Qt here.

The reference quantities (velocity, length, viscosity, temperature) behind Mach, Reynolds and y+ come
from the case files where they can be read (inlet values, transport properties) and otherwise from the
answers the user gave in the guided questions (kept in the project sidecar, never in the case).
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.case.checks import STEADY_APPS, TURB_FIELDS, CaseFacts, Edit, facts
from aethercfd.case.model import Case
from aethercfd.core.textio import decode_bytes

# ============================================================================ model


@dataclass
class Gate:
    id: str
    ok: bool
    severity: str  # error (blocks "done") | warn | info
    title: str
    why: str = ""
    how: str = ""  # how to fix it
    evidence: list[str] = field(default_factory=list)
    fix_label: str = ""
    edits: list[Edit] = field(default_factory=list)  # case-relative files

    @property
    def blocking(self) -> bool:
        return not self.ok and self.severity == "error"

    def as_dict(self) -> dict:
        d = {"id": self.id, "ok": self.ok, "severity": self.severity, "title": self.title}
        for k in ("why", "how", "fix_label"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.evidence:
            d["evidence"] = self.evidence[:12]
        if self.edits:
            d["fix_edits"] = [{"file": e.file, "key": e.key, "value": e.value} for e in self.edits]
        return d


def passed(id_: str, title: str, evidence: list[str] | None = None) -> Gate:
    return Gate(id_, True, "info", title, evidence=evidence or [])


# ============================================================================ reading helpers
_MAX = 20 * 2**20


def _read(p: Path, limit: int = _MAX) -> str | None:
    try:
        if not p.is_file() or p.stat().st_size > limit:
            return None
        return decode_bytes(p.read_bytes())[0]
    except OSError:
        return None


class FieldFile:
    """An initial field (0/U, 0/p, ...): dimensions, internal value and per-patch types/values."""

    def __init__(self, path: Path, text: str):
        from aethercfd.foamdict.parser import parse
        from aethercfd.foamdict.semantic import DictView

        self.path = path
        self.name = path.name
        self.doc = parse(text)
        self.view = DictView(self.doc)
        m = re.search(r"\bdimensions\s*\[([^\]]*)\]", text[:20000])
        self.dims_text = m.group(1).strip() if m else None
        self.dims: list[float] | None = None
        if m:
            try:
                nums = [float(x) for x in m.group(1).split()]
                self.dims = (nums + [0.0] * 7)[:7]
            except ValueError:
                self.dims = None  # named dimensions ([velocity]): not checked

    def get(self, key: str) -> str | None:
        t = self.view.get_text(key)
        return t.strip() if t is not None else None

    def patch_entry(self, patch, key: str) -> str | None:
        """Value of boundaryField/<patch>/<key>, following regex keys and patch groups."""
        v = self.get(f"boundaryField/{patch.name}/{key}")
        if v is None:
            for g in getattr(patch, "in_groups", ()):
                v = self.get(f"boundaryField/{g}/{key}")
                if v is not None:
                    break
        return v


def initial_fields(case: Case) -> dict[str, FieldFile]:
    from aethercfd.case.workflow import initial_dir

    d0 = initial_dir(case)
    if d0 is None:
        return {}
    region = case.regions[0].name if case.regions else ""
    fdir = d0 / region if region else d0
    out = {}
    if not fdir.is_dir():
        return out
    for p in sorted(fdir.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix in (".orig", ".bak", ".gz"):
            continue
        text = _read(p, 4 * 2**20)
        if text is None or "FoamFile" not in text[:4000] or "boundaryField" not in text:
            continue
        out[p.name] = FieldFile(p, text)
    return out


def _uniform_scalar(text: str | None) -> float | None:
    if not text:
        return None
    m = re.fullmatch(r"(?:uniform\s+)?([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", text.strip())
    return float(m.group(1)) if m else None


def _uniform_vector(text: str | None) -> tuple[float, float, float] | None:
    if not text:
        return None
    m = re.fullmatch(r"(?:uniform\s+)?\(\s*(\S+)\s+(\S+)\s+(\S+)\s*\)", text.strip())
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    except ValueError:
        return None


def _dict_value(case: Case, rel: str, key: str) -> str | None:
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    text = _read(case.root / rel)
    if text is None:
        return None
    v = DictView(parse(text)).get_text(key)
    return v.strip() if v is not None else None


def patches(case: Case) -> list:
    """The mesh's patches; before the mesh exists, the patch names of 0/U (types guessed from the
    boundary conditions), so boundary conditions can be reviewed before meshing."""
    pts = [p for r in case.regions[:1] for p in r.patches]
    if pts:
        return pts
    cached = getattr(case, "_pseudo_patches", None)
    if cached is not None:
        return cached
    from aethercfd.case.model import Patch

    out = []
    U = initial_fields(case).get("U")
    if U is not None:
        from aethercfd.foamdict.cst import Entry

        bf = U.view.dict_at("boundaryField")
        for m in (bf.members if bf is not None else []):
            if not isinstance(m, Entry) or m.dict is None or m.key.is_pattern:
                continue
            t = U.get(f"boundaryField/{m.key.name}/type") or ""
            kind = t if t in ("empty", "wedge", "symmetryPlane", "symmetry", "cyclic") else (
                "wall" if t in ("noSlip", "slip", "movingWallVelocity", "partialSlip") or "wall" in
                m.key.name.lower() else "patch")
            out.append(Patch(m.key.name, kind, 1))
    with contextlib.suppress(AttributeError):
        case._pseudo_patches = out  # type: ignore[attr-defined]
    return out


def _wall_patches(case: Case) -> list:
    return [p for p in patches(case) if p.type == "wall" and (p.n_faces or 0) > 0]


def _flow_patches(case: Case) -> list:
    """Patches with a boundary condition to set (not empty/wedge/symmetry/cyclic/processor)."""
    from aethercfd.case.workflow import CONSTRAINT_TYPES

    return [p for p in patches(case) if p.type not in CONSTRAINT_TYPES and (p.n_faces or 0) > 0]


# ============================================================================ reference quantities
FLUIDS = {  # name -> (nu [m2/s] at ~20 C, density [kg/m3], gamma, R [J/kg/K]) (gamma/R only for gases)
    "air": (1.516e-5, 1.204, 1.4, 287.05),
    "water": (1.004e-6, 998.2, None, None),
    "nitrogen": (1.54e-5, 1.165, 1.4, 296.8),
    "helium": (1.17e-4, 0.166, 1.667, 2077.0),
    "oil (SAE 30)": (3.0e-4, 875.0, None, None),
}


@dataclass
class Reference:
    U: float | None = None  # m/s
    L: float | None = None  # m
    nu: float | None = None  # m2/s
    T: float | None = None  # K
    gamma: float | None = None
    R: float | None = None
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def mach(self) -> float | None:
        if self.U is None or self.T is None or not self.gamma or not self.R:
            return None
        return self.U / math.sqrt(self.gamma * self.R * self.T)

    @property
    def reynolds(self) -> float | None:
        if self.U is None or self.L is None or not self.nu:
            return None
        return self.U * self.L / self.nu

    def as_dict(self) -> dict:
        out = {k: (float(f"{v:.6g}") if isinstance(v, float) else v)
               for k, v in (("U", self.U), ("L", self.L), ("nu", self.nu), ("T", self.T))}
        if self.mach is not None:
            out["Mach"] = float(f"{self.mach:.3g}")
        if self.reynolds is not None:
            out["Re"] = float(f"{self.reynolds:.4g}")
        out["sources"] = self.sources
        return out


def inlet_speed(case: Case, fields: dict[str, FieldFile] | None = None) -> tuple[float | None, str]:
    """Largest uniform fixedValue velocity magnitude on any patch (the inlet), else the internal field."""
    fields = fields if fields is not None else initial_fields(case)
    U = fields.get("U")
    if U is None:
        return None, ""
    best, where = None, ""
    for p in _flow_patches(case):
        t = U.patch_entry(p, "type") or ""
        if t in ("fixedValue", "uniformFixedValue", "inletOutlet", "pressureInletOutletVelocity",
                 "flowRateInletVelocity", "freestream", "freestreamVelocity", "movingWallVelocity"):
            vec = _uniform_vector(U.patch_entry(p, "value") or U.patch_entry(p, "uniformValue")
                                  or U.patch_entry(p, "freestreamValue"))
            if vec is not None:
                mag = math.sqrt(sum(c * c for c in vec))
                if mag > 0 and (best is None or mag > best):
                    best, where = mag, f"0/U {p.name} value"
    if best is None:
        vec = _uniform_vector(U.get("internalField"))
        if vec is not None and any(vec):
            best, where = math.sqrt(sum(c * c for c in vec)), "0/U internalField"
    return best, where


_MESH_CACHE: dict[tuple, object] = {}


def _mesh_key(mesh_dir: Path, what: str) -> tuple | None:
    try:
        st = [(mesh_dir / n).stat() for n in ("points", "owner", "faces")]
    except OSError:
        return None
    return (str(mesh_dir), what, *[(s.st_size, s.st_mtime_ns) for s in st])


def _cached(mesh_dir: Path, what: str, fn):
    key = _mesh_key(mesh_dir, what)
    if key is None:
        return None
    if key not in _MESH_CACHE:
        if len(_MESH_CACHE) > 16:
            _MESH_CACHE.clear()
        try:
            _MESH_CACHE[key] = fn()
        except Exception:
            _MESH_CACHE[key] = None
    return _MESH_CACHE[key]


def bounding_box(case: Case) -> tuple[list[float], list[float]] | None:
    reg = case.regions[0] if case.regions else None
    if reg is None or not (reg.mesh_dir / "points").exists():
        return None
    if (reg.mesh_dir / "points").stat().st_size > 400 * 2**20:
        return None

    def compute():
        from aethercfd.mesh.polymesh import _read_list

        pts, _ = _read_list(reg.mesh_dir / "points", "points")
        pts = pts.reshape(-1, 3)
        return pts.min(0).tolist(), pts.max(0).tolist()

    return _cached(reg.mesh_dir, "bbox", compute)


def reference(case: Case, answers: dict | None = None, fields: dict[str, FieldFile] | None = None) -> Reference:
    a = answers or {}
    fields = fields if fields is not None else initial_fields(case)
    ref = Reference()
    U, where = inlet_speed(case, fields)
    if a.get("U_ref"):
        ref.U, ref.sources["U"] = float(a["U_ref"]), "your answer"
    elif U is not None:
        ref.U, ref.sources["U"] = U, where
    if a.get("L_ref"):
        ref.L, ref.sources["L"] = float(a["L_ref"]), "your answer"
    else:
        bb = bounding_box(case)
        if bb is not None:
            ext = sorted(e for e in (hi - lo for lo, hi in zip(*bb, strict=True)) if e > 1e-12)
            two_d = bool(case.regions) and case.regions[0].is_2d
            if ext:
                # the smallest resolved extent (channel height, pipe diameter): a conservative guess
                ref.L = ext[1] if two_d and len(ext) > 1 else ext[0]
                ref.sources["L"] = "smallest domain extent (an estimate: give the real length in Domain)"
    T = fields.get("T")
    t_val = _uniform_scalar(T.get("internalField")) if T is not None else None
    if a.get("T_ref"):
        ref.T, ref.sources["T"] = float(a["T_ref"]), "your answer"
    elif t_val is not None:
        ref.T, ref.sources["T"] = t_val, "0/T internalField"
    else:
        ref.T, ref.sources["T"] = 293.15, "assumed 20 °C"
    fluid = a.get("fluid")
    nu = None
    for rel in ("constant/transportProperties", "constant/physicalProperties"):
        v = _dict_value(case, rel, "nu")
        if v:
            nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", v.split("]")[-1])
            if nums:
                nu = float(nums[-1])
                ref.sources["nu"] = f"{rel} nu"
                break
    if nu is None and fluid in FLUIDS:
        nu, ref.sources["nu"] = FLUIDS[fluid][0], f"{fluid} at ~20 °C"
    if nu is None and a.get("nu"):
        nu, ref.sources["nu"] = float(a["nu"]), "your answer"
    if nu is None and _first_existing(case, "constant/thermophysicalProperties", "constant/physicalProperties"):
        from aethercfd.mesh.wall import ideal_gas_rho, sutherland_mu

        p = fields.get("p")
        pv = _uniform_scalar(p.get("internalField")) if p is not None else None
        rho = ideal_gas_rho(pv if pv and pv > 1000 else 1e5, ref.T or 293.15)
        nu, ref.sources["nu"] = sutherland_mu(ref.T or 293.15) / rho, "air (Sutherland) at the case's p and T"
    ref.nu = nu
    gas = FLUIDS.get(fluid or "air", FLUIDS["air"])
    ref.gamma, ref.R = gas[2], gas[3]
    if fluid and gas[2] is None:  # a liquid: no Mach number
        ref.gamma = ref.R = None
    return ref


def _first_existing(case: Case, *rels: str) -> Path | None:
    for r in rels:
        if (case.root / r).is_file():
            return case.root / r
    return None


# ============================================================================ gates: syntax
def syntax_gate(case: Case, rels: list[str]) -> Gate:
    from aethercfd.foamdict.parser import parse

    bad = []
    for rel in rels:
        text = _read(case.root / rel)
        if text is None:
            continue
        doc = parse(text)
        if not doc.ok:
            d = next(x for x in doc.diagnostics if x.severity == "error")
            bad.append(f"{rel}: line {doc.line_col(d.start)[0]}: {d.message}")
    if bad:
        return Gate("syntax", False, "error", "Dictionary syntax errors",
                    "OpenFOAM stops at the first unreadable file, often with a confusing message.",
                    "Open the file in Raw mode at that line and fix the brace/semicolon.", bad)
    return passed("syntax", "Dictionaries parse", [r for r in rels if (case.root / r).is_file()])


# ============================================================================ gates: dimensions
_DIMS = {  # field -> (kg m s K mol A cd) for incompressible (kinematic) and compressible
    "U": ([0, 1, -1, 0, 0, 0, 0], [0, 1, -1, 0, 0, 0, 0]),
    "k": ([0, 2, -2, 0, 0, 0, 0], [0, 2, -2, 0, 0, 0, 0]),
    "epsilon": ([0, 2, -3, 0, 0, 0, 0], [0, 2, -3, 0, 0, 0, 0]),
    "omega": ([0, 0, -1, 0, 0, 0, 0], [0, 0, -1, 0, 0, 0, 0]),
    "nut": ([0, 2, -1, 0, 0, 0, 0], [0, 2, -1, 0, 0, 0, 0]),
    "nuTilda": ([0, 2, -1, 0, 0, 0, 0], [0, 2, -1, 0, 0, 0, 0]),
    "T": ([0, 0, 0, 1, 0, 0, 0], [0, 0, 0, 1, 0, 0, 0]),
    "p": ([0, 2, -2, 0, 0, 0, 0], [1, -1, -2, 0, 0, 0, 0]),
    "p_rgh": ([0, 2, -2, 0, 0, 0, 0], [1, -1, -2, 0, 0, 0, 0]),
    "alphat": ([0, 2, -1, 0, 0, 0, 0], [1, -1, -1, 0, 0, 0, 0]),
}
_DIM_NAMES = ("kg", "m", "s", "K", "mol", "A", "cd")


def _fmt_dims(d) -> str:
    return "[" + " ".join(f"{x:g}" for x in d) + "]"


def _unit(d) -> str:
    parts = [f"{n}^{x:g}" if x != 1 else n for n, x in zip(_DIM_NAMES, d, strict=True) if x]
    return " ".join(parts) or "dimensionless"


def dimensions_gate(case: Case, f: CaseFacts, fields: dict[str, FieldFile]) -> Gate:
    bad, edits, seen = [], [], []
    for name, (inc, comp) in _DIMS.items():
        ff = fields.get(name)
        if ff is None or ff.dims is None:
            continue
        want = comp if f.compressible else inc
        if name == "p" and case.application in ("buoyantBoussinesqSimpleFoam", "buoyantBoussinesqPimpleFoam"):
            want = inc
        seen.append(name)
        if [float(x) for x in want] != ff.dims:
            rel = ff.path.relative_to(case.root).as_posix()
            bad.append(f"{rel}: dimensions {_fmt_dims(ff.dims)} ({_unit(ff.dims)}), expected {_fmt_dims(want)} "
                       f"({_unit(want)}) for a {'compressible' if f.compressible else 'incompressible'} solver")
            edits.append(Edit(rel, "dimensions", _fmt_dims(want)))
    if bad:
        return Gate("dimensions", False, "error", "Field dimensions do not match the solver",
                    "OpenFOAM checks dimensions in every operation and aborts ('incompatible dimensions'); a "
                    "wrong pressure dimension usually means a field file copied from a different solver type "
                    "(kinematic p/rho for incompressible, Pa for compressible).",
                    "Correct the dimension set (and convert the values: p_absolute = rho × p_kinematic + p_ref).",
                    bad, "Set the expected dimension sets (values still need checking)", edits)
    return passed("dimensions", "Field dimensions match the solver", [", ".join(seen)] if seen else [])


# ============================================================================ gates: BCs
_P_FIXING = {"fixedValue", "totalPressure", "fixedMean", "fixedMeanOutletInlet", "prghPressure", "prghTotalPressure",
             "uniformFixedValue", "waveTransmissive", "outletInlet", "uniformTotalPressure", "fanPressure",
             "timeVaryingTotalPressure", "fixedFluxExtrapolatedPressure", "prghTotalHydrostaticPressure",
             "totalTemperature", "advective", "plenumPressure", "outletMappedUniformInlet",
             "entrainmentPressure"}
_U_FIXING = {"fixedValue", "noSlip", "flowRateInletVelocity", "movingWallVelocity", "uniformFixedValue", "slip",
             "rotatingWallVelocity", "surfaceNormalFixedValue", "translatingWallVelocity", "partialSlip",
             "fixedMean", "cylindricalInletVelocity", "swirlFlowRateInletVelocity", "timeVaryingMappedFixedValue"}
_U_FREE = {"zeroGradient", "inletOutlet", "pressureInletOutletVelocity", "outletInlet", "advective",
           "fluxCorrectedVelocity", "pressureInletVelocity", "noSlip_never"}


def pressure_reference_gate(case: Case, f: CaseFacts, fields: dict[str, FieldFile]) -> Gate:
    pname = "p_rgh" if "p_rgh" in fields else "p"
    p = fields.get(pname)
    if p is None or (f.compressible and pname == "p"):
        return passed("p-reference", "Pressure level")
    fixed = [pt.name for pt in _flow_patches(case) if (p.patch_entry(pt, "type") or "") in _P_FIXING]
    if fixed:
        return passed("p-reference", "Pressure level is set by a boundary", [f"0/{pname}: {', '.join(fixed)}"])
    sol = _dict_value(case, "system/fvSolution", f"{f.algorithm}/pRefCell") or \
        _dict_value(case, "system/fvSolution", f"{f.algorithm}/pRefPoint")
    if sol is not None:
        return passed("p-reference", "Pressure level set by pRefCell/pRefPoint", [f"fvSolution {f.algorithm}"])
    return Gate("p-reference", False, "error", "Pressure level is undefined",
                f"No patch fixes {pname} and fvSolution has no pRefCell/pRefPoint: in an incompressible flow "
                "only pressure differences matter, so the pressure equation is singular and drifts or fails.",
                f"Give the outlet a fixedValue {pname} (e.g. 0), or for a closed domain set {f.algorithm} "
                "{ pRefCell 0; pRefValue 0; }.", [f"0/{pname}: no fixedValue-type patch"],
                "Add pRefCell 0 and pRefValue 0",
                [Edit("system/fvSolution", f"{f.algorithm}/pRefCell", "0", insert_if_missing=True),
                 Edit("system/fvSolution", f"{f.algorithm}/pRefValue", "0", insert_if_missing=True)])


def up_pairing_gate(case: Case, fields: dict[str, FieldFile]) -> Gate:
    """A well-posed open boundary fixes U or p, not both and not neither (classic beginner mistake)."""
    U, p = fields.get("U"), fields.get("p_rgh") or fields.get("p")
    if U is None or p is None:
        return passed("u-p-pairing", "Velocity/pressure boundary pairing")
    both, neither = [], []
    for pt in _flow_patches(case):
        if pt.type == "wall":
            continue
        tu, tp = U.patch_entry(pt, "type") or "", p.patch_entry(pt, "type") or ""
        if not tu or not tp:
            continue
        if tu in ("fixedValue", "flowRateInletVelocity", "uniformFixedValue") and tp in (
                "fixedValue", "uniformFixedValue"):
            both.append(f"{pt.name}: U {tu} and {p.name} {tp}")
        if tu == "zeroGradient" and tp == "zeroGradient":
            neither.append(f"{pt.name}: U and {p.name} both zeroGradient")
    if both or neither:
        return Gate("u-p-pairing", False, "warn", "Over- or under-constrained open boundary",
                    "At an inlet fix the velocity and let pressure float (zeroGradient); at an outlet fix the "
                    "pressure and let velocity float. Fixing both over-constrains the boundary (non-physical "
                    "reflections, poor convergence); fixing neither leaves it undetermined.",
                    "Use the Boundary conditions questions, or set the types in Raw mode.", both + neither)
    return passed("u-p-pairing", "Velocity/pressure boundary pairing is well posed")


def bc_gate(case: Case) -> Gate:
    from aethercfd.case.workflow import bc_consistency

    issues = bc_consistency(case)
    if issues:
        return Gate("bc-coverage", False, "error", "Patches without a boundary condition",
                    "OpenFOAM refuses to start ('Cannot find patchField entry for ...') when any field lacks an "
                    "entry for a mesh patch.", "Add the patch to the field's boundaryField (Raw mode), or use a "
                    "patch group / regex entry.", issues[:20])
    return passed("bc-coverage", "Every patch has a boundary condition in every field")


def wall_function_gate(case: Case, f: CaseFacts, fields: dict[str, FieldFile]) -> Gate:
    """RANS walls: k/epsilon/omega/nut must use wall functions consistent with each other."""
    if f.turbulence in (None, "laminar"):
        return passed("wall-functions", "Wall treatment (laminar)")
    walls = _wall_patches(case)
    nut = fields.get("nut")
    bad = []
    for w in walls:
        tn = nut.patch_entry(w, "type") if nut else None
        for name, want in (("epsilon", "epsilonWallFunction"), ("omega", "omegaWallFunction")):
            ff = fields.get(name)
            if ff is None or name not in TURB_FIELDS.get(f.turbulence or "", []):
                continue
            t = ff.patch_entry(w, "type") or ""
            if t in ("zeroGradient",) and tn and "WallFunction" in tn:
                bad.append(f"0/{name} {w.name}: {t} with nut {tn} (needs {want})")
        if tn in ("calculated", "zeroGradient"):
            bad.append(f"0/nut {w.name}: {tn} on a wall (no wall model: use nutkWallFunction, or "
                       "nutLowReWallFunction / fixedValue 0 for a wall-resolved mesh)")
    if bad:
        return Gate("wall-functions", False, "warn", "Inconsistent near-wall treatment",
                    "The wall functions of nut, k and epsilon/omega work as a set; mixing them gives the wrong "
                    "wall shear stress and heat transfer.", "Use matching wall functions on every wall patch.", bad)
    return passed("wall-functions", "Near-wall treatment is consistent")


# ============================================================================ gates: solver/scheme coverage
def transported_fields(case: Case, f: CaseFacts, fields: dict[str, FieldFile]) -> list[str]:
    out = ["U"]
    out += [x for x in TURB_FIELDS.get(f.turbulence or "", []) if x not in ("nut",)]
    if f.compressible:
        out += [x for x in ("h", "e") if x in fields] or []
    return out


def coverage_gate(case: Case, f: CaseFacts, fields: dict[str, FieldFile]) -> Gate:
    """Every solved field needs a linear solver in fvSolution and a convection scheme in fvSchemes."""
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    sol_t, sch_t = _read(case.root / "system/fvSolution"), _read(case.root / "system/fvSchemes")
    if sol_t is None or sch_t is None:
        return Gate("coverage", False, "error", "fvSolution / fvSchemes missing",
                    "Every OpenFOAM case needs both.", "Copy them from a tutorial of the same solver.")
    sol, sch = DictView(parse(sol_t)), DictView(parse(sch_t))
    p_name = "p_rgh" if "p_rgh" in fields else "p"
    missing_solver, missing_scheme, edits = [], [], []
    for fld in [p_name, *transported_fields(case, f, fields)]:
        if sol.lookup(f"solvers/{fld}") is None and sol.lookup(f"solvers/{fld}Final") is None:
            missing_solver.append(fld)
    default_div = (sch.get_text("divSchemes/default") or "").strip()
    for fld in transported_fields(case, f, fields):
        if fld == "U" and f.compressible:
            continue
        key = f"div(phi,{fld})"
        if sch.lookup(f"divSchemes/{key}") is None and (not default_div or default_div == "none"):
            missing_scheme.append(key)
            edits.append(Edit("system/fvSchemes", f"divSchemes/{key}",
                              ("bounded " if f.steady else "") + "Gauss upwind" if fld != "U" else
                              ("bounded " if f.steady else "") + "Gauss linearUpwind grad(U)", insert_if_missing=True))
    for fld in missing_solver:
        if fld != p_name:
            edits.append(Edit("system/fvSolution", f"solvers/{fld}",
                              "{ solver smoothSolver; smoother symGaussSeidel; tolerance 1e-06; relTol 0.1; }",
                              insert_if_missing=True))
    if missing_solver or missing_scheme:
        ev = [f"fvSolution: no solver for {', '.join(missing_solver)}"] if missing_solver else []
        ev += [f"fvSchemes: no {', '.join(missing_scheme)} (and divSchemes default none)"] if missing_scheme else []
        return Gate("coverage", False, "error", "A solved field has no linear solver or scheme",
                    "OpenFOAM aborts at the first equation it cannot discretise or solve ('keyword div(phi,omega) is "
                    "undefined', 'cannot find solver entry'); typical after switching turbulence model.",
                    "Add the entries (or widen the existing regex, e.g. \"(U|k|epsilon|omega)\").", ev,
                    "Add upwind schemes / smoothSolver entries", edits)
    return passed("coverage", "Every solved field has a solver and a scheme")


# ============================================================================ gates: mesh
def mesh_exists_gate(case: Case) -> Gate:
    if any((r.mesh_dir / "owner").exists() or (r.mesh_dir / "owner.gz").exists() for r in case.regions):
        return passed("mesh-exists", "Mesh generated", [f"{case.n_cells:,} cells" if case.n_cells else ""])
    how = "Run the mesher (Mesh stage: 'Generate mesh' runs blockMesh + checkMesh through the run manager)"
    if (case.root / "system" / "snappyHexMeshDict").is_file():
        how = "Run blockMesh then snappyHexMesh -overwrite (or the case's Allrun) from the Run panel"
    return Gate("mesh-exists", False, "error", "No mesh yet", "Nothing can be solved or checked without a mesh.",
                how)


def checkmesh_log(case: Case) -> Path | None:
    logs = list(case.root.glob("log.checkMesh*"))
    for sub in ("logs",):
        if (case.root / sub).is_dir():
            logs += list((case.root / sub).glob("log.checkMesh*"))
    runs = case.root / ".aethercfd" / "local" / "runs"
    if runs.is_dir():
        logs += list(runs.glob("*/log.checkMesh*"))
    logs = [p for p in logs if p.is_file()]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def mesh_quality_gate(case: Case) -> tuple[Gate, float | None]:
    from aethercfd.case.checkmesh import parse_checkmesh

    reg = case.regions[0] if case.regions else None
    owner = reg.mesh_dir / "owner" if reg else None
    log = checkmesh_log(case)
    if owner is None or not owner.exists():
        return Gate("mesh-quality", False, "error", "Mesh quality unknown", "There is no mesh to check.",
                    "Generate the mesh first."), None
    if log is None:
        return Gate("mesh-quality", False, "error", "checkMesh has not been run",
                    "Bad cells (non-orthogonal, skewed, inverted) are the most common cause of divergence; "
                    "checkMesh finds them before you waste a run.",
                    "Run checkMesh (Mesh stage: 'Check mesh'), or see AetherMesh for the quality maps."), None
    if log.stat().st_mtime + 1 < owner.stat().st_mtime:
        return Gate("mesh-quality", False, "error", "checkMesh is older than the mesh",
                    "The mesh changed after the last checkMesh; its verdict no longer applies.",
                    "Run checkMesh again.", [f"{log.name} older than constant/polyMesh/owner"]), None
    rep = parse_checkmesh(decode_bytes(log.read_bytes()[: 8 * 2**20])[0])
    ev = [f"{log.relative_to(case.root).as_posix()}"]
    if rep.non_ortho_max is not None:
        ev.append(f"max non-orthogonality {rep.non_ortho_max:.1f}°")
    if rep.skewness_max is not None:
        ev.append(f"max skewness {rep.skewness_max:.2f}")
    if rep.aspect_max is not None:
        ev.append(f"max aspect ratio {rep.aspect_max:.1f}")
    fails = [c for c in rep.checks if c.severity == "fail"]
    if not rep.finished and not rep.checks:
        return Gate("mesh-quality", False, "error", "checkMesh did not finish", "The log is incomplete.",
                    "Run checkMesh again and look at the log for errors.", ev), rep.non_ortho_max
    if rep.failed and not fails:
        return Gate("mesh-quality", False, "error", f"checkMesh: failed {rep.failed} mesh check(s)",
                    "checkMesh reports failed checks (see its log for the lines starting with ***).",
                    "Open the checkMesh log (Raw) and AetherMesh's quality maps to locate the bad cells.", ev), \
            rep.non_ortho_max
    if fails:
        return Gate("mesh-quality", False, "error", f"checkMesh failed {len(fails)} check(s)",
                    "; ".join(f"{c.name}: {c.message}" for c in fails)[:600],
                    " ".join(dict.fromkeys(c.advice for c in fails if c.advice))[:900] or
                    "Improve the mesh near the reported sets (AetherMesh shows where they are).", ev), rep.non_ortho_max
    warns = [c for c in rep.checks if c.severity == "warn"]
    if warns:
        return Gate("mesh-quality", False, "warn", "checkMesh passed with warnings",
                    "; ".join(f"{c.name}: {c.message}" for c in warns)[:600],
                    " ".join(dict.fromkeys(c.advice for c in warns if c.advice))[:900], ev), rep.non_ortho_max
    return passed("mesh-quality", "checkMesh: mesh OK", ev), rep.non_ortho_max


def wall_distance(case: Case) -> dict[str, tuple[float, float, float]] | None:
    """Per wall patch (min, mean, max) distance of the first cell centre from the wall [m]."""
    reg = case.regions[0] if case.regions else None
    if reg is None or not (reg.mesh_dir / "owner").exists() or (case.n_cells or 0) > 3_000_000:
        return None
    return _cached(reg.mesh_dir, "walldist", lambda: _wall_distance(reg.mesh_dir))


def _wall_distance(mesh_dir: Path) -> dict[str, tuple[float, float, float]]:
    import numpy as np

    from aethercfd.mesh.polymesh import read_polymesh
    from aethercfd.mesh.quality import geometry

    mesh = read_polymesh(mesh_dir)
    geo = geometry(mesh)
    out = {}
    for pt in mesh.patches:
        if pt.get("type") != "wall" or int(pt.get("nFaces", 0)) == 0:
            continue
        s, n = int(pt["startFace"]), int(pt["nFaces"])
        fc, sf = geo.face_centres[s:s + n], geo.face_areas[s:s + n]
        nh = sf / (np.linalg.norm(sf, axis=1, keepdims=True) + 1e-300)
        cc = geo.cell_centres[mesh.owner[s:s + n]]
        d = np.abs(np.einsum("ij,ij->i", fc - cc, nh))
        out[pt["name"]] = (float(d.min()), float(d.mean()), float(d.max()))
    return out


_LOWRE_NUT = {"nutLowReWallFunction", "fixedValue", "nutUSpaldingWallFunction"}  # Spalding: valid for all y+
_HIGHRE_NUT = {"nutkWallFunction", "nutUWallFunction", "nutkRoughWallFunction", "nutURoughWallFunction",
               "nutWallFunction"}


def yplus_from_results(case: Case) -> dict[str, tuple[float, float, float]] | None:
    """(min, mean, max) y+ per wall patch from a yPlus field written by the yPlus function object."""
    t = case.latest_result_time
    if not t:
        return None
    region = case.regions[0].name if case.regions else ""
    f = case.root / t / region / "yPlus" if region else case.root / t / "yPlus"
    text = _read(f, 64 * 2**20)
    if text is None:
        return None
    ff = FieldFile(f, text)
    out = {}
    for pt in _wall_patches(case):
        v = ff.patch_entry(pt, "value")
        if not v:
            continue
        u = _uniform_scalar(v)
        if u is not None:
            out[pt.name] = (u, u, u)
            continue
        m = re.search(r"\(\s*([^()]*)\)\s*$", v, re.S)
        if m:
            vals = [float(x) for x in m.group(1).split()]
            if vals:
                out[pt.name] = (min(vals), sum(vals) / len(vals), max(vals))
    return out or None


def yplus_gate(case: Case, f: CaseFacts, fields: dict[str, FieldFile], ref: Reference) -> Gate:
    if f.turbulence in (None, "laminar") or not _wall_patches(case):
        return passed("yplus", "Near-wall resolution (laminar or no walls)")
    nut = fields.get("nut")
    kinds = {w.name: (nut.patch_entry(w, "type") if nut else None) or "?" for w in _wall_patches(case)}
    measured = yplus_from_results(case)
    src = "yPlus field of the latest result"
    yp: dict[str, tuple[float, float, float]] = {}
    if measured:
        yp = measured
    else:
        dist = wall_distance(case)
        if not dist or not ref.U or not ref.L or not ref.nu:
            return Gate("yplus", False, "warn", "y+ not estimated",
                        "y+ decides whether the wall functions are valid; it needs the mesh, a reference velocity, "
                        "length and viscosity.", "Answer the Domain/Materials questions (U, L, fluid), or add the "
                        "yPlus function object (Run stage) and look again after a run.")
        from aethercfd.mesh.wall import skin_friction

        re_l = ref.U * ref.L / ref.nu
        u_tau = ref.U * math.sqrt(skin_friction(max(re_l, 1.0), "pipe" if case.regions and not _external(case)
                                               else "plate") / 2.0)
        yp = {w: (d[0] * u_tau / ref.nu, d[1] * u_tau / ref.nu, d[2] * u_tau / ref.nu) for w, d in dist.items()}
        src = (f"a-priori estimate: first-cell distance × u_tau/nu, u_tau from Cf(Re={re_l:.3g}); ±50 %, check "
               "after the run with the yPlus function object")
    ev = [src] + [f"{w}: y+ min {a:.3g} / mean {m:.3g} / max {b:.3g}  (nut {kinds.get(w, '?')})"
                  for w, (a, m, b) in yp.items()]
    errors, warns = [], []
    for w, (_a, mean, mx) in yp.items():
        k = kinds.get(w, "?")
        if k in _LOWRE_NUT and k != "nutUSpaldingWallFunction" and mean > 5:
            errors.append(f"{w}: wall-resolved treatment ({k}) but mean y+ {mean:.3g} > 5: the viscous sublayer "
                          "is not resolved, wall shear and separation will be wrong")
        elif k in _HIGHRE_NUT and mean > 500:
            errors.append(f"{w}: mean y+ {mean:.3g} > 500 is beyond the log law the wall function assumes")
        elif k in _HIGHRE_NUT and mx > 300:
            warns.append(f"{w}: max y+ {mx:.3g} > 300 in places")
        elif k in _HIGHRE_NUT and 5 < mean < 30 and f.turbulence in ("kEpsilon", "realizableKE", "RNGkEpsilon"):
            warns.append(f"{w}: mean y+ {mean:.3g} sits in the buffer layer (5-30), where standard wall "
                         "functions are least accurate")
    if errors:
        return Gate("yplus", False, "error", "Mesh y+ does not suit the wall treatment", "; ".join(errors),
                    "Either refine the wall-normal first cell (AetherMesh ▸ Wall spacing gives the height for a "
                    "target y+) or switch nut to a wall function (nutkWallFunction / nutUSpaldingWallFunction).", ev)
    if warns:
        return Gate("yplus", False, "warn", "y+ outside the ideal range in places", "; ".join(warns),
                    "Target y+ 30-100 for wall functions or y+ ≈ 1 for wall-resolved (low-Re) meshes.", ev)
    return passed("yplus", "y+ suits the wall treatment", ev)


def _external(case: Case) -> bool:
    names = " ".join(p.name.lower() for r in case.regions[:1] for p in r.patches)
    return any(w in names for w in ("farfield", "freestream", "far", "atmosphere"))


# ============================================================================ gates: physics
INCOMPRESSIBLE_APPS = {"simpleFoam", "pimpleFoam", "icoFoam", "pisoFoam", "SRFSimpleFoam", "porousSimpleFoam",
                       "nonNewtonianIcoFoam", "shallowWaterFoam", "adjointShapeOptimizationFoam"}


def mach_gate(case: Case, f: CaseFacts, ref: Reference) -> Gate:
    ma = ref.mach
    if ma is None:
        return passed("mach", "Compressibility (liquid or unknown speed)")
    ev = [f"Ma = U/a = {ref.U:.4g} / {ref.U / ma:.4g} = {ma:.3g}  (U from {ref.sources.get('U', '?')}, T "
          f"{ref.T:.4g} K from {ref.sources.get('T', '?')})"]
    incompressible = case.application in INCOMPRESSIBLE_APPS or case.solver_module == "incompressibleFluid"
    if incompressible and ma > 0.3:
        return Gate("mach", False, "error", f"Ma ≈ {ma:.2f}: compressibility matters",
                    "Above Ma ≈ 0.3 density changes by more than ~5 %; an incompressible solver ignores that "
                    "(wrong pressure, no shocks).", "Use a compressible solver (rhoSimpleFoam / rhoPimpleFoam, "
                    "Foundation: fluid) — start from a compressible template in New Case.", ev)
    if f.compressible and ma < 0.05 and case.application not in ("buoyantSimpleFoam", "buoyantPimpleFoam",
                                                                  "reactingFoam", "chtMultiRegionFoam"):
        return Gate("mach", False, "info", f"Ma ≈ {ma:.3f}: an incompressible solver would do",
                    "Density-based/compressible solvers converge slowly at very low Mach number.", "", ev)
    return passed("mach", f"Ma ≈ {ma:.3g}: solver type fits", ev)


def steady_gate(case: Case, f: CaseFacts, answers: dict) -> Gate:
    want = answers.get("time_dependence")
    if not want:
        return passed("steady", "Steady/transient (not stated)")
    is_steady = f.steady
    if (want == "steady") != is_steady:
        app = case.application or case.solver_module or "?"
        return Gate("steady", False, "error", f"You said {want}, the case is {'steady' if is_steady else 'transient'}",
                    f"{app} with {f.algorithm} is {'a steady (pseudo-time)' if is_steady else 'a transient'} "
                    "algorithm; the time steps and the result mean something different.",
                    "Pick a template with the right algorithm (New Case), or change your answer if the case is "
                    "right.", [f"application {app}, algorithm {f.algorithm}"])
    return passed("steady", f"{want.capitalize()} as intended")


def species_sum_gate(case: Case, fields: dict[str, FieldFile]) -> Gate:
    """Multicomponent cases: the initial and inlet mass fractions must sum to 1."""
    th = _read(case.root / "constant/thermophysicalProperties") or _read(case.root / "constant/physicalProperties")
    if th is None or not re.search(r"(multi[Cc]omponent|reacting)Mixture|coefficientMultiComponent|"
                                   r"multicomponentMixture|valueMultiComponentMixture", th):
        return passed("species-sum", "Species mass fractions (single component)")
    species: list[str] = []
    for p in sorted((case.root / "constant").glob("*")):
        t = _read(p, 8 * 2**20) if p.is_file() else None
        m = re.search(r"\bspecies\s*\d*\s*\(([^)]*)\)", t or "")
        if m:
            species = m.group(1).split()
            break
    inert = re.search(r"\b(inertSpecie|defaultSpecie)\s+(\w+)\s*;", th)
    ydef = fields.get("Ydefault")
    ydef_v = _uniform_scalar(ydef.get("internalField")) if ydef else 0.0
    names = species or [n for n, ff in fields.items() if ff.dims == [0.0] * 7 and n not in ("Ydefault",)
                        and not n.startswith(("alpha", "Xi", "b", "ft", "f", "Su"))]
    if not names:
        return passed("species-sum", "Species mass fractions (no species fields found)")

    def total(get) -> tuple[float | None, list[str]]:
        s, missing = 0.0, []
        for n in names:
            ff = fields.get(n)
            v = get(ff) if ff is not None else ydef_v
            if v is None:
                return None, []
            if ff is None:
                missing.append(n)
            s += v
        return s, missing

    bad, ev = [], []
    s, _ = total(lambda ff: _uniform_scalar(ff.get("internalField")))
    if s is not None:
        ev.append(f"internalField: sum Y = {s:.6g} over {len(names)} species")
        if abs(s - 1.0) > 1e-3:
            bad.append(f"initial mass fractions sum to {s:.6g}, not 1")
    for pt in _flow_patches(case):
        if pt.type == "wall":
            continue

        def patch_val(ff, pt=pt):
            t = ff.patch_entry(pt, "type") or ""
            return _uniform_scalar(ff.patch_entry(pt, "value")) if t == "fixedValue" else None
        if all((fields.get(n) is not None and (fields[n].patch_entry(pt, "type") or "") == "fixedValue")
               for n in names if n in fields) and any(n in fields for n in names):
            s, _ = total(patch_val)
            if s is not None:
                ev.append(f"{pt.name}: sum Y = {s:.6g}")
                if abs(s - 1.0) > 1e-3:
                    bad.append(f"patch {pt.name}: fixed mass fractions sum to {s:.6g}, not 1")
    if bad:
        why = ("Mass fractions must sum to one. OpenFOAM silently puts the remainder into the inert species "
               f"({inert.group(2)}), " if inert else "Mass fractions must sum to one; OpenFOAM normalises silently, ")
        return Gate("species-sum", False, "error", "Species mass fractions do not sum to 1",
                    why + "so a typo changes the mixture (or makes the inert fraction negative).",
                    "Fix the uniform values in 0/<species> so that ΣY = 1 (Combustion panel computes "
                    "stoichiometric mixtures).", bad + ev)
    return passed("species-sum", "Species mass fractions sum to 1", ev)


def checks_gates(findings: list, ids: set[str] | None = None, exclude: set[str] | None = None) -> list[Gate]:
    """The numerics-lab / pre-run findings (aethercfd.case.checks.run_checks) as gates (errors block)."""
    out = []
    for fd in findings:
        if ids is not None and fd.id not in ids:
            continue
        if exclude and fd.id in exclude:
            continue
        out.append(Gate(f"check:{fd.id}", False, fd.severity, fd.title, fd.why,
                        fd.fix_label or "See Checks (Advanced) for the detail.", fd.evidence, fd.fix_label, fd.edits))
    return out


# ============================================================================ gates: run / validation
_NOT_SOLVER = ("log.blockMesh", "log.checkMesh", "log.decompose", "log.reconstruct", "log.foamDictionary",
               "log.snappyHexMesh", "log.surfaceFeature", "log.splitMeshRegions", "log.topoSet", "log.setFields",
               "log.potentialFoam", "log.Allrun", "log.createPatch", "log.extrudeMesh", "log.mapFields",
               "log.foamToVTK", "log.postProcess", "log.renumberMesh", "log.gmshToFoam", "log.changeDictionary")


def latest_solver_log(case_root: Path) -> Path | None:
    logs = [p for p in case_root.glob("log.*") if p.is_file()]
    if (case_root / "logs").is_dir():
        logs += [p for p in (case_root / "logs").glob("log.*") if p.is_file()]
    runs = case_root / ".aethercfd" / "local" / "runs"
    if runs.is_dir():
        logs += [p for p in runs.glob("*/log.*") if p.is_file()]
    pool = [p for p in logs if not p.name.startswith(_NOT_SOLVER)]
    return max(pool, key=lambda p: p.stat().st_mtime) if pool else None


@dataclass
class RunSummary:
    log: Path
    finished: bool
    fatal: list[str]
    steps: int
    converged_msg: str | None
    residuals: dict[str, tuple[float, float]]  # field -> (first initial, last initial)
    continuity: float | None
    bounding_late: int
    co_max: float | None


def run_summary(log: Path) -> RunSummary:
    from aethercfd.runs.logparse import parse_log_text

    text = decode_bytes(log.read_bytes()[-64 * 2**20:])[0]
    parser, steps = parse_log_text(text)
    res: dict[str, tuple[float, float]] = {}
    for st in steps:
        for k, r in st.residuals.items():
            first = res.get(k, (r.initial, r.initial))[0]
            res[k] = (first, r.initial)
    m = re.search(r"^(SIMPLE|PIMPLE).*converged in.*$", text, re.M)
    cont = next((st.continuity[1] for st in reversed(steps) if st.continuity), None)
    late = 0
    if steps:
        cut = steps[int(len(steps) * 0.9)].time if len(steps) > 10 else steps[0].time
        late = sum(1 for e in parser.events if e.kind == "bounding" and e.line_no >= _line_of_time(text, cut))
    co = max((st.co_max for st in steps if st.co_max is not None), default=None)
    return RunSummary(log, parser.finished, [e.text for e in parser.fatal][:5], len(steps), m.group(0) if m else None,
                      res, cont, late, co)


def _line_of_time(text: str, t: float) -> int:
    m = re.search(rf"^Time = {re.escape(f'{t:g}')}\s*$", text, re.M)
    return text.count("\n", 0, m.start()) + 1 if m else 0


def run_gate(case: Case) -> tuple[Gate, RunSummary | None]:
    log = latest_solver_log(case.root)
    results = [t for _, t in case.times if t != "0"] + [t for _, t in case.processor_times if t != "0"]
    if log is None:
        if results:
            return passed("run-done", "Results exist (no solver log found)", [f"{len(results)} time folder(s)"]), None
        return Gate("run-done", False, "error", "Not run yet", "There are no results to validate.",
                    "Run the case (Run stage: Start runs what the case needs: mesh, check, solve)."), None
    s = run_summary(log)
    ev = [f"{log.relative_to(case.root).as_posix() if log.is_relative_to(case.root) else log}: {s.steps} steps"]
    if s.fatal:
        return Gate("run-done", False, "error", "The run failed", "; ".join(s.fatal)[:500],
                    "Ask Murphy (Run stage) or the copilot to diagnose the log; the Solver Log panel jumps to the "
                    "error.", ev), s
    if not s.finished:
        return Gate("run-done", False, "warn", "The run has not finished (or was stopped)",
                    "The solver log has no 'End' line yet.", "Wait for it, or check the Run panel.", ev), s
    return passed("run-done", "The run finished", ev + ([s.converged_msg] if s.converged_msg else [])), s


def convergence_gate(case: Case, f: CaseFacts, s: RunSummary | None) -> Gate:
    if s is None or not s.residuals:
        return Gate("convergence", False, "warn", "Convergence not assessed", "No residual history found.",
                    "Run the case through AetherCFD so its log is kept.")
    ev, poor = [], []
    for k, (first, last) in sorted(s.residuals.items()):
        drop = math.log10(first / last) if first > 0 and last > 0 else float("nan")
        ev.append(f"{k}: initial residual {first:.2g} → {last:.2g} ({drop:.1f} orders)")
        if f.steady and not s.converged_msg and last > 1e-3 and k.split("/")[-1] in ("p", "U", "Ux", "Uy", "p_rgh"):
            poor.append(k)
    if s.continuity is not None:
        ev.append(f"global continuity error {s.continuity:.2g}")
    if s.bounding_late:
        ev.append(f"{s.bounding_late} 'bounding' warnings in the last 10 % of the run")
    if poor:
        return Gate("convergence", False, "warn", "Residuals still high at the end",
                    f"{', '.join(poor)} did not drop below 1e-3 and no residualControl convergence was reported: "
                    "the steady solution may not be converged.", "Run more iterations, improve the mesh or use "
                    "residualControl (Run stage); compare an integral quantity between the last writes.", ev)
    if s.bounding_late:
        return Gate("convergence", False, "warn", "Turbulence fields are still being bounded",
                    "'bounding k/epsilon/omega' late in a run means negative values are being clipped: the "
                    "solution is not clean.", "Use bounded/upwind convection for turbulence and check the mesh "
                    "near the reported cells.", ev)
    return passed("convergence", "Converged" if s.converged_msg or not f.steady else "Residuals dropped", ev)


def health_gate(case: Case) -> Gate:
    """Connie-style health of fieldMinMax/solverInfo telemetry, computed natively when present."""
    pp = case.root / "postProcessing"
    dats = list(pp.glob("*/*/fieldMinMax.dat")) if pp.is_dir() else []
    if not dats:
        return Gate("health", True, "info", "No fieldMinMax telemetry",
                    how="Add the monitoring function objects (Run stage) to get min/max of every field in time.")
    bad = []
    for d in dats:
        try:
            txt = decode_bytes(d.read_bytes()[-4 * 2**20:])[0]
        except OSError:
            continue
        if re.search(r"\b(nan|inf)\b", txt, re.I):
            bad.append(f"{d.relative_to(case.root).as_posix()}: NaN/Inf values")
        for line in txt.splitlines()[-200:]:
            parts = line.split()
            if not parts or line.startswith("#"):
                continue
            if len(parts) > 3 and parts[1] in ("k", "epsilon", "omega", "nut"):
                try:
                    if float(parts[2]) < 0:
                        bad.append(f"{parts[1]} min {parts[2]} < 0 at t={parts[0]}")
                        break
                except ValueError:
                    pass
    if bad:
        return Gate("health", False, "error", "Unphysical values in the run telemetry",
                    "NaN/Inf or negative turbulence quantities mean the solution is broken there.",
                    "Diagnose the log (Murphy / copilot) and check the mesh and schemes.", bad[:10])
    return passed("health", "Run telemetry is physical", [d.relative_to(case.root).as_posix() for d in dats][:4])


__all__ = ["FLUIDS", "STEADY_APPS", "FieldFile", "Gate", "Reference", "facts", "initial_fields", "reference"]
