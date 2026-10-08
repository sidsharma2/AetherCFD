"""Numerics lab / pre-run checks: rules fire on a deliberately bad case and their fixes apply cleanly."""

from __future__ import annotations

import shutil
from pathlib import Path

from aethercfd.case.checks import run_checks
from aethercfd.case.model import load_case
from aethercfd.commands.base import USER
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"
HDR = 'FoamFile {{ version 2.0; format ascii; class {cls}; object {obj}; }}\n'


def _bad_case(tmp_path: Path) -> Path:
    c = tmp_path / "bad"
    shutil.copytree(FIX, c)
    cd = (c / "system" / "controlDict").read_text().replace("icoFoam", "simpleFoam")
    (c / "system" / "controlDict").write_text(cd)
    (c / "system" / "fvSchemes").write_text(HDR.format(cls="dictionary", obj="fvSchemes") + """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes
{
    default         none;
    div(phi,U)      Gauss linear;
    div(phi,k)      Gauss linear;
    div(phi,omega)  Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
""")
    (c / "system" / "fvSolution").write_text(HDR.format(cls="dictionary", obj="fvSolution") + """
solvers { p { solver GAMG; tolerance 1e-6; relTol 0.1; smoother GaussSeidel; } }
SIMPLE
{
    nNonOrthogonalCorrectors 0;
}
""")
    (c / "constant" / "turbulenceProperties").write_text(
        HDR.format(cls="dictionary", obj="turbulenceProperties") + "simulationType RAS;\nRAS { model kOmegaSST; }\n")
    (c / "log.checkMesh").write_text("    Mesh non-orthogonality Max: 75.2 average: 10.1\n")
    return c


def test_rules_fire_and_fixes_apply(tmp_path):
    c = _bad_case(tmp_path)
    facts, found = run_checks(load_case(c))
    assert facts.steady and facts.turbulence == "kOmegaSST" and facts.non_ortho_max == 75.2
    ids = {f.id for f in found}
    assert {"steady-bounded", "central-U", "central-turb", "laplacian-limit", "nonortho-correctors",
            "relaxation-missing", "turb-fields"} <= ids, ids
    turb = next(f for f in found if f.id == "turb-fields")
    assert "omega" in turb.evidence[0] and "k" in turb.evidence[0]
    edited = [(e.file, e.key) for f in found for e in f.edits]
    assert len(edited) == len(set(edited)), "two findings must never edit the same keyword"
    s = Session(c, create_project=True, with_backend=False)
    for f in found:
        for e in f.edits:
            res = s.executor.execute("dict.set", {"file": str(c / e.file), "key": e.key, "value": e.value,
                                                  "insert_if_missing": e.insert_if_missing}, USER)
            assert res.ok, (f.id, e, res.message)
    _, again = run_checks(load_case(c))
    left = {f.id for f in again}
    assert not ({"central-U", "central-turb", "laplacian-limit", "nonortho-correctors", "relaxation-missing",
                 "steady-bounded"} & left), left
    from aethercfd.foamdict.parser import parse

    assert parse((c / "system" / "fvSolution").read_text()).ok
    assert "limited corrected 0.33" in (c / "system" / "fvSchemes").read_text()


def test_clean_cavity_has_no_warnings():
    _, found = run_checks(load_case(FIX))
    assert not [f for f in found if f.severity in ("error", "warn")], found
