"""Numerics lab and pre-run checks: what an experienced CFD engineer looks at before pressing Run.

Each rule reads the case's dictionaries (lossless parser, provenance-aware: values that come from
macros or #include are reported, never "fixed") and, where it knows the mesh quality (from a
checkMesh log or the AetherMesh quality engine), ties scheme choices to the mesh. A finding has a
severity, why it matters, the evidence (file, keyword, value) and, where a fix is safe and local, the
exact dict edits to apply (shown as a plan and journalled like any other change). No Qt here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.case.model import Case
from aethercfd.core.textio import decode_bytes

STEADY_APPS = {"simpleFoam", "rhoSimpleFoam", "buoyantSimpleFoam", "chtMultiRegionSimpleFoam", "porousSimpleFoam",
               "SRFSimpleFoam", "potentialFoam"}
COMPRESSIBLE_APPS = {"rhoSimpleFoam", "rhoPimpleFoam", "rhoCentralFoam", "sonicFoam", "buoyantSimpleFoam",
                     "buoyantPimpleFoam", "chtMultiRegionFoam", "chtMultiRegionSimpleFoam", "reactingFoam",
                     "rhoReactingFoam", "XiFoam", "fireFoam"}
COMPRESSIBLE_MODULES = {"fluid", "multicomponentFluid", "shockFluid", "XiFluid", "compressibleVoF"}
TURB_FIELDS = {"kEpsilon": ["k", "epsilon", "nut"], "realizableKE": ["k", "epsilon", "nut"],
               "RNGkEpsilon": ["k", "epsilon", "nut"], "kOmega": ["k", "omega", "nut"],
               "kOmegaSST": ["k", "omega", "nut"], "SpalartAllmaras": ["nuTilda", "nut"],
               "kOmegaSSTLM": ["k", "omega", "nut", "ReThetat", "gammaInt"], "LaunderSharmaKE": ["k", "epsilon", "nut"]}
CONVECTION = re.compile(r"^div\(phi,(U|k|epsilon|omega|nuTilda|h|e|K|Ekp|T|Yi|R)\)$")


@dataclass
class Edit:
    file: str  # relative to the case
    key: str
    value: str
    insert_if_missing: bool = False


@dataclass
class Finding:
    id: str
    severity: str  # error | warn | info
    title: str
    why: str
    evidence: list[str] = field(default_factory=list)
    fix_label: str = ""
    edits: list[Edit] = field(default_factory=list)


@dataclass
class _Dict:
    path: Path
    rel: str
    doc: object
    view: object

    def get(self, key: str) -> tuple[str | None, str]:
        """(literal value text or None, provenance)."""
        lk = self.view.lookup(key)
        if lk is None:
            return None, "missing"
        if lk.entry is None or lk.entry.value is None:
            return None, lk.provenance.value
        return self.doc.text(lk.entry.value.start, lk.entry.value.end).strip(), lk.provenance.value

    def keys(self, path: str = "") -> list[str]:
        return self.view.keys(path) if (not path or self.view.dict_at(path) is not None) else []

    def line(self, key: str) -> int | None:
        lk = self.view.lookup(key)
        return self.doc.line_col(lk.entry.start)[0] if lk is not None and lk.entry is not None else None


def _load(case: Case, rel: str) -> _Dict | None:
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    p = case.root / rel
    if not p.is_file() or p.stat().st_size > 20 * 2**20:
        return None
    doc = parse(decode_bytes(p.read_bytes())[0])
    return _Dict(p, rel, doc, DictView(doc))


def _first(case: Case, *rels: str) -> _Dict | None:
    for r in rels:
        d = _load(case, r)
        if d is not None:
            return d
    return None


@dataclass
class CaseFacts:
    steady: bool
    compressible: bool
    algorithm: str  # SIMPLE | PIMPLE | PISO | ?
    turbulence: str | None
    non_ortho_max: float | None = None
    cells: int | None = None


def facts(case: Case, mesh_non_ortho: float | None = None) -> CaseFacts:
    app = case.application or ""
    module = case.solver_module or ""
    fvs = _load(case, "system/fvSolution")
    algo = next((a for a in ("SIMPLE", "PIMPLE", "PISO") if fvs and fvs.view.dict_at(a) is not None), "?")
    steady = app in STEADY_APPS or algo == "SIMPLE"
    if module and fvs is not None and algo == "PIMPLE":
        sch = _load(case, "system/fvSchemes")
        ddt = sch.get("ddtSchemes/default")[0] if sch else None
        steady = bool(ddt and "steadyState" in ddt)
    compressible = app in COMPRESSIBLE_APPS or module in COMPRESSIBLE_MODULES
    turb = None
    tp = _first(case, "constant/momentumTransport", "constant/turbulenceProperties")
    if tp is not None:
        sim = tp.get("simulationType")[0]
        if sim in ("RAS", "LES"):
            turb = tp.get(f"{sim}/model")[0] or tp.get(f"{sim}/{sim}Model")[0]
        elif sim == "laminar":
            turb = "laminar"
    return CaseFacts(steady, compressible, algo, turb, mesh_non_ortho, case.n_cells)


def mesh_non_ortho_from_logs(case: Case) -> float | None:
    from aethercfd.case.checkmesh import parse_checkmesh

    logs = sorted(case.root.glob("log.checkMesh*"), key=lambda p: p.stat().st_mtime)
    logs += sorted((case.root / "logs").glob("log.checkMesh*"), key=lambda p: p.stat().st_mtime) if (
        case.root / "logs").is_dir() else []
    for p in reversed(logs):
        r = parse_checkmesh(decode_bytes(p.read_bytes()[: 8 * 2**20])[0])
        if r.non_ortho_max is not None:
            return r.non_ortho_max
    return None


# ============================================================================ rules
def run_checks(case: Case, *, mesh_non_ortho: float | None = None) -> tuple[CaseFacts, list[Finding]]:
    if mesh_non_ortho is None:
        mesh_non_ortho = mesh_non_ortho_from_logs(case)
    f = facts(case, mesh_non_ortho)
    out: list[Finding] = []
    sch = _load(case, "system/fvSchemes")
    sol = _load(case, "system/fvSolution")
    if sch is not None:
        out += _schemes(f, sch)
    if sol is not None:
        out += _solution(f, sol, sch)
    out += _fields(case, f)
    out += _thermo(case, f)
    out += _io(case, f)
    from aethercfd.case.workflow import bc_consistency

    for msg in bc_consistency(case)[:20]:
        out.append(Finding("bc-missing", "error", "Boundary condition missing or inconsistent", msg))
    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda x: order.get(x.severity, 3))
    return f, out


def _schemes(f: CaseFacts, sch: _Dict) -> list[Finding]:
    out = []
    keys = sch.keys("divSchemes")
    conv = [k for k in keys if CONVECTION.match(k)]

    def central(k: str, v: str) -> bool:  # fixed by the central-scheme rules below (one edit per keyword)
        core = v.removeprefix("bounded").strip()
        return bool(re.fullmatch(r"Gauss\s+linear", core)) and (
            (k == "div(phi,U)" and f.turbulence not in (None, "laminar"))
            or k in ("div(phi,k)", "div(phi,epsilon)", "div(phi,omega)", "div(phi,nuTilda)"))

    if f.steady:
        unbounded = []
        for k in conv:
            v, prov = sch.get(f"divSchemes/{k}")
            if v and prov == "literal" and not v.startswith("bounded") and not v.startswith("none") \
                    and not central(k, v):
                unbounded.append((k, v))
        if unbounded:
            out.append(Finding(
                "steady-bounded", "warn", "Steady solver with unbounded convection schemes",
                "For steady (SIMPLE) runs, 'bounded Gauss …' removes the ∇·(φ)ψ term that is zero only at "
                "convergence; without it, intermediate continuity errors feed straight into the transported "
                "fields and can make k/ω/T go negative or the run diverge.",
                [f"{sch.rel}:{sch.line('divSchemes/' + k)}  {k}  {v}" for k, v in unbounded],
                "Prefix 'bounded' to these schemes",
                [Edit(sch.rel, f"divSchemes/{k}", f"bounded {v}") for k, v in unbounded]))
    for k in conv:
        v, prov = sch.get(f"divSchemes/{k}")
        if not v or prov != "literal":
            continue
        core = v.removeprefix("bounded").strip()
        if k == "div(phi,U)" and f.turbulence not in (None, "laminar") and re.fullmatch(r"Gauss\s+linear", core):
            out.append(Finding("central-U", "warn", "Central differencing for momentum in a RANS run",
                               "Pure 'Gauss linear' convection is unbounded and oscillates on all but very fine "
                               "meshes; second-order upwind-biased schemes are the robust default.",
                               [f"{sch.rel}:{sch.line('divSchemes/' + k)}  {k}  {v}"],
                               "Use linearUpwind grad(U)",
                               [Edit(sch.rel, f"divSchemes/{k}", ("bounded " if f.steady else "")
                                     + "Gauss linearUpwind grad(U)")]))
        if k in ("div(phi,k)", "div(phi,epsilon)", "div(phi,omega)", "div(phi,nuTilda)") and \
                re.fullmatch(r"Gauss\s+linear", core):
            out.append(Finding("central-turb", "warn", f"Central differencing for {k[8:-1]}",
                               "Turbulence quantities must stay positive; central schemes create undershoots. Use "
                               "upwind or limitedLinear 1.", [f"{sch.rel}:{sch.line('divSchemes/' + k)}  {k}  {v}"],
                               "Use upwind", [Edit(sch.rel, f"divSchemes/{k}", ("bounded " if f.steady else "")
                                                   + "Gauss upwind")]))
    lap, prov = sch.get("laplacianSchemes/default")
    sng, sprov = sch.get("snGradSchemes/default")
    no = f.non_ortho_max
    if no is not None and lap and prov == "literal":
        if no > 70 and re.search(r"\bcorrected\b", lap) and "limited" not in lap and "uncorrected" not in lap:
            edits = [Edit(sch.rel, "laplacianSchemes/default", "Gauss linear limited corrected 0.33")]
            if sng and sprov == "literal" and "limited" not in sng:
                edits.append(Edit(sch.rel, "snGradSchemes/default", "limited corrected 0.33"))
            out.append(Finding("laplacian-limit", "warn", f"Mesh non-orthogonality {no:.1f}° with full correction",
                               "Above ~70° the explicit non-orthogonal correction can exceed the orthogonal part and "
                               "destabilise the pressure equation; limit it (0.33 ≈ correction bounded by a third).",
                               [f"{sch.rel}:{sch.line('laplacianSchemes/default')}  laplacianSchemes default  {lap}",
                                f"max non-orthogonality {no:.2f}°"], "Use 'limited corrected 0.33'", edits))
        if no < 40 and re.search(r"limited\s+(corrected\s+)?0?\.3", lap):
            out.append(Finding("laplacian-overlimited", "info", "Laplacian limiter stronger than the mesh needs",
                               f"With max non-orthogonality {no:.1f}° the full 'corrected' scheme is accurate and "
                               "stable; the limiter discards part of the correction.",
                               [f"{sch.rel}  laplacianSchemes default  {lap}"]))
    return out


def _solution(f: CaseFacts, sol: _Dict, sch: _Dict | None) -> list[Finding]:
    out = []
    algo = f.algorithm
    if algo in ("SIMPLE", "PIMPLE", "PISO"):
        n, prov = sol.get(f"{algo}/nNonOrthogonalCorrectors")
        no = f.non_ortho_max
        try:
            nval = int(float(n)) if n is not None else 0
        except ValueError:
            nval = None
        if no is not None and nval is not None and prov in ("literal", "missing"):
            want = 0 if no < 60 else 1 if no < 70 else 2 if no < 80 else 3
            if nval < want:
                out.append(Finding("nonortho-correctors", "warn" if no >= 70 else "info",
                                   f"{nval} non-orthogonal correctors for a {no:.1f}° mesh",
                                   "Each corrector re-solves pressure with the updated non-orthogonal flux; "
                                   "meshes above ~60–70° usually need 1–3.",
                                   [f"{sol.rel}  {algo}/nNonOrthogonalCorrectors = {n or 'not set (0)'}"],
                                   f"Set nNonOrthogonalCorrectors {want}",
                                   [Edit(sol.rel, f"{algo}/nNonOrthogonalCorrectors", str(want),
                                         insert_if_missing=True)]))
    if f.steady and sol.view.dict_at("relaxationFactors") is None and algo == "SIMPLE":
        consistent = (sol.get("SIMPLE/consistent")[0] or "").strip() in ("yes", "true", "on")
        fields = "U 0.9; \".*\" 0.9;" if consistent else "U 0.7; \".*\" 0.7;"
        out.append(Finding("relaxation-missing", "warn", "SIMPLE without relaxationFactors",
                           "Steady SIMPLE needs under-relaxation (pressure ~0.3, momentum/turbulence ~0.7; "
                           "SIMPLEC: momentum ~0.9, pressure unrelaxed) or it diverges.",
                           [f"{sol.rel}: no relaxationFactors dictionary"], "Add typical relaxation factors",
                           [Edit(sol.rel, "relaxationFactors",
                                 "{ " + ("" if consistent else "fields { p 0.3; } ") + f"equations {{ {fields} }} }}",
                                 insert_if_missing=True)]))
    if f.steady and algo == "SIMPLE" and (sol.get("SIMPLE/consistent")[0] or "").strip() in ("yes", "true", "on"):
        for key in ("relaxationFactors/fields/p", "relaxationFactors/p", "relaxationFactors/fields/p_rgh"):
            val, prov = sol.get(key)
            try:
                relax = float(val) if val is not None and prov == "literal" else None
            except ValueError:
                relax = None
            if relax is not None and relax < 1:
                out.append(Finding("simplec-p-relaxed", "warn", f"SIMPLEC with the pressure relaxed ({relax:g})",
                                   "SIMPLEC (consistent yes) is built to run with the pressure unrelaxed and the "
                                   "momentum near 0.9; relaxing p as well only slows convergence. Plain SIMPLE is "
                                   "the one that needs p ~0.3.",
                                   [f"{sol.rel}:{sol.line(key)}  {key} {val}", f"{sol.rel}  SIMPLE/consistent yes"],
                                   "Set the pressure relaxation to 1", [Edit(sol.rel, key, "1")]))
                break
    if f.compressible and f.steady and algo == "SIMPLE":
        has = any(sol.get(f"SIMPLE/{k}")[0] for k in ("pMinFactor", "pMaxFactor", "pMin", "pMax"))
        if not has and not any(sol.get(f"SIMPLE/{k}")[0] for k in ("rhoMin", "rhoMax")):
            out.append(Finding("pressure-limits", "info", "No pressure limits for a compressible SIMPLE run",
                               "pMinFactor/pMaxFactor (or rhoMin/rhoMax) clip pressure excursions in early "
                               "iterations, which otherwise produce negative density and a crash.",
                               [f"{sol.rel}: SIMPLE has no pMin/pMax/pMinFactor/pMaxFactor"],
                               "Add pMinFactor 0.5, pMaxFactor 1.5",
                               [Edit(sol.rel, "SIMPLE/pMinFactor", "0.5", insert_if_missing=True),
                                Edit(sol.rel, "SIMPLE/pMaxFactor", "1.5", insert_if_missing=True)]))
    if algo == "PIMPLE" and not f.steady:
        nout, _ = sol.get("PIMPLE/nOuterCorrectors")
        if nout is not None and nout.strip() == "1":
            out.append(Finding("piso-mode", "info", "PIMPLE with one outer corrector (PISO mode)",
                               "With nOuterCorrectors 1 the time step must keep Co ≲ 1 for accuracy and stability; "
                               "large time steps need outer correctors (and residualControl).",
                               [f"{sol.rel}  PIMPLE/nOuterCorrectors 1"]))
    return out


def _fields(case: Case, f: CaseFacts) -> list[Finding]:
    from aethercfd.case.workflow import initial_dir

    out = []
    d0 = initial_dir(case)
    if d0 is None:
        procs = sorted(case.root.glob("processor0/*/U")) + sorted(case.root.glob("processor0/*/*/U"))
        if procs:
            return [Finding("decomposed-only", "info", "Fields exist only in processor directories",
                            "This decomposed case has no reconstructed 0/; field checks are skipped. "
                            "reconstructPar (or a serial copy) makes them reviewable.",
                            [f"e.g. {procs[0].relative_to(case.root).as_posix()}"])]
        return [Finding("no-initial", "error", "No initial conditions folder", "The case has no 0/ (or 0.orig/).")]
    region = case.regions[0].name if case.regions else ""
    fdir = d0 / region if region else d0
    have = {p.name.removesuffix(".gz") for p in fdir.iterdir() if p.is_file()} if fdir.is_dir() else set()
    need = TURB_FIELDS.get(f.turbulence or "", [])
    missing = [x for x in need if x not in have]
    if missing:
        out.append(Finding("turb-fields", "error", f"{f.turbulence} needs initial fields that are missing",
                           f"The {f.turbulence} model reads {', '.join(need)} at start-up.",
                           [f"{fdir.relative_to(case.root).as_posix() or '0'}: missing {', '.join(missing)}"]))
    if f.compressible and f.turbulence not in (None, "laminar") and "alphat" not in have:
        out.append(Finding("alphat", "warn", "Compressible turbulent case without alphat",
                           "Compressible RANS needs the turbulent thermal diffusivity field alphat (with "
                           "compressible::alphatWallFunction on walls).", [f"{fdir.name}: no alphat"]))
    p = fdir / "p"
    if p.is_file():
        m = re.search(r"dimensions\s*\[([^\]]*)\]", decode_bytes(p.read_bytes()[:4096])[0])
        if m:
            dims = m.group(1).split()
            kinematic = dims[:3] == ["0", "2", "-2"]
            absolute = dims[:3] == ["1", "-1", "-2"]
            if f.compressible and kinematic:
                out.append(Finding("p-dims", "error", "Kinematic pressure in a compressible case",
                                   "Compressible solvers solve absolute pressure in Pa ([1 -1 -2 ...]); [0 2 -2] is "
                                   "p/rho from an incompressible setup.", [f"0/p dimensions [{m.group(1)}]"]))
            if not f.compressible and absolute and case.application not in ("buoyantBoussinesqSimpleFoam",):
                out.append(Finding("p-dims", "error", "Absolute pressure in an incompressible case",
                                   "Incompressible solvers use kinematic pressure p/rho ([0 2 -2 ...]).",
                                   [f"0/p dimensions [{m.group(1)}]"]))
    return out


def _thermo(case: Case, f: CaseFacts) -> list[Finding]:
    th = _first(case, "constant/thermophysicalProperties", "constant/physicalProperties")
    if th is None or not f.compressible:
        return []
    out = []
    lows, highs = [], []
    for key in th.keys(""):
        sub = th.view.dict_at(key)
        if sub is None:
            continue
        lo, _ = th.get(f"{key}/thermodynamics/Tlow")
        hi, _ = th.get(f"{key}/thermodynamics/Thigh")
        if lo and hi:
            try:
                lows.append(float(lo))
                highs.append(float(hi))
            except ValueError:
                pass
    if not lows:
        return out
    tlo, thi = max(lows), min(highs)
    T = case.root / "0" / (case.regions[0].name if case.regions and case.regions[0].name else "") / "T"
    if T.is_file():
        txt = decode_bytes(T.read_bytes()[: 2**20])[0]
        temps = [float(x) for x in re.findall(r"uniform\s+([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)", txt)]
        bad = [t for t in temps if t < tlo or t > thi]
        if bad:
            out.append(Finding("thermo-range", "warn", "Temperatures outside the JANAF/thermo validity range",
                               f"The thermo polynomials are valid for {tlo:g}–{thi:g} K; outside they extrapolate "
                               "(cp, h wrong; OpenFOAM may abort with 'Maximum number of iterations exceeded').",
                               [f"0/T uniform values {sorted(set(bad))} outside [{tlo:g}, {thi:g}] K"]))
    return out


def _io(case: Case, f: CaseFacts) -> list[Finding]:
    cd = _load(case, "system/controlDict")
    if cd is None:
        return []
    out = []
    try:
        end = float(cd.get("endTime")[0] or "nan") - float(cd.get("startTime")[0] or 0)
        wi = float(cd.get("writeInterval")[0] or "nan")
        purge = int(float(cd.get("purgeWrite")[0] or 0))
        control = (cd.get("writeControl")[0] or "timeStep").strip()
        if control == "timeStep":  # writeInterval counts time steps: convert to simulated time
            wi *= float(cd.get("deltaT")[0] or 1.0)
        elif control not in ("runTime", "adjustable", "adjustableRunTime"):
            return out  # clockTime / cpuTime: no projection
    except ValueError:
        return out
    if wi > 0 and end == end and wi > end:
        out.append(Finding("write-after-end", "info", "writeInterval is longer than the run",
                           "Only the final time is written (if at all).",
                           [f"controlDict endTime {end:g}, writeInterval {wi:g}"]))
    if f.cells and wi > 0 and end == end:
        writes = end / wi if purge == 0 else purge
        n_fields = 8 if f.compressible else 5
        gb = f.cells * n_fields * 3 * 8 * 1.6 * writes / 1e9  # ascii ~1.6x binary, vectors x3
        if gb > 20:
            out.append(Finding("disk-projection", "warn", f"About {gb:,.0f} GB of result files",
                               f"{writes:,.0f} writes × {f.cells:,} cells × ~{n_fields} fields. Use purgeWrite, "
                               "a larger writeInterval or binary format.",
                               [f"controlDict writeInterval {wi:g}, endTime {end:g}, purgeWrite {purge}"],
                               "Keep the last 3 writes (purgeWrite 3)",
                               [Edit(cd.rel, "purgeWrite", "3", insert_if_missing=True)]))
    return out
