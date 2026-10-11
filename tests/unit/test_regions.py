"""Multi-region (CHT) cases: CaseData per region, region-aware field writing, copilot tools' region parameter.

The fixture is the cavity mesh twice: region "a" (a solid: T and p) and region "b" (the fluid: U and p),
named in system/regionProperties. p is uniform, 10 x the time in "a" and the time in "b", so every reading
shows which region it came from.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from aethercfd.results.casedata import CaseData, pick_region

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"
TIMES = (("0", 0.0), ("0.1", 1.0), ("0.2", 2.0), ("0.3", 4.0))


def two_region_case(tmp_path: Path) -> Path:
    case = tmp_path / "cht"
    shutil.copytree(FIX, case)
    for r in ("a", "b"):
        shutil.copytree(FIX / "constant" / "polyMesh", case / "constant" / r / "polyMesh")
    p_text = (FIX / "0" / "p").read_text()
    t_text = re.sub(r"dimensions\s+\[[^\]]*\];", "dimensions      [0 0 0 1 0 0 0];", p_text.replace("object      p;", "object      T;"))
    for t, v in TIMES:
        for r, scale, names in (("a", 10.0, ("T", "p")), ("b", 1.0, ("U", "p"))):
            d = case / t / r
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                text = {"p": p_text, "T": t_text, "U": (FIX / "0" / "U").read_text()}[n]
                if n != "U":
                    text = re.sub(r"internalField\s+uniform\s+[^;]+;", f"internalField uniform {v * scale};", text)
                (d / n).write_text(text)
    for n in ("U", "p"):
        (case / "0" / n).unlink()  # the top-level mesh has no fields (like an unsplit CHT original)
    (case / "system" / "regionProperties").write_text(
        "FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n    object regionProperties;\n}\n\n"
        "regions\n(\n    solid (a)  // the collar\n    fluid (b)\n);\n")
    return case


def test_vtk_lists_the_region_meshes(tmp_path):
    from aethercfd.results.foam_reader import FoamResults

    meta = FoamResults(two_region_case(tmp_path)).refresh()
    assert "/a/internalMesh" in meta.patches and "/b/internalMesh" in meta.patches
    assert meta.regions == ["a", "b"] and meta.internal_meshes == ["/a/internalMesh", "/b/internalMesh"]


def test_casedata_per_region(tmp_path):
    case = two_region_case(tmp_path)
    data = CaseData(case)
    assert data.regions() == ["a", "b"]
    assert data.default_region() == "b"  # the fluid of regionProperties, not the first name
    a, b = data.for_region("a"), CaseData(case, "b")
    assert sorted(a.field_names()) == ["T", "p"] and sorted(b.field_names()) == ["U", "p"]
    assert np.allclose(a.fields(names=["p"])["p"], 40.0) and np.allclose(b.fields(names=["p"])["p"], 4.0)
    assert "U" not in a.fields(0.3) and b.fields(0.3)["U"].shape == (400, 3)
    assert a.centres().shape == (400, 3)
    ts, snap = b.snapshots("p")
    assert ts == [0.1, 0.2, 0.3] and np.allclose(snap[:, 0], [1, 2, 4])
    assert a.mesh().n_cells == 400
    assert CaseData(FIX).regions() == [] and CaseData(FIX).default_region() == ""


def test_multi_region_without_a_region_says_choose(tmp_path):
    case = two_region_case(tmp_path)
    with pytest.raises(ValueError, match=r"multi-region case \(regions: a, b\): choose a region.*'b'"):
        CaseData(case).fields()
    with pytest.raises(ValueError, match="no region 'zz'"):
        CaseData(case, "zz").fields()


def test_pick_region_fallbacks(tmp_path):
    assert pick_region(tmp_path, []) == ""
    assert pick_region(tmp_path, ["solid_1", "fluid_main"]) == "fluid_main"  # by name
    (tmp_path / "1" / "s2").mkdir(parents=True)
    (tmp_path / "1" / "s2" / "U").write_text("")
    assert pick_region(tmp_path, ["s1", "s2"]) == "s2"  # the one with a velocity
    assert pick_region(tmp_path / "nope", ["s1", "s3"]) == "s1"


def test_field_files_into_a_region(tmp_path):
    from aethercfd.results.write_fields import field_files

    case = two_region_case(tmp_path)
    files = field_files(case, "0.3", {"pX": np.arange(400.0)}, like="p", region="a")
    assert list(files) == [str(Path("0.3") / "a" / "pX")]
    text = files[str(Path("0.3") / "a" / "pX")]
    assert 'location    "0.3/a";' in text and "[0 2 -2 0 0 0 0]" in text  # dimensions of a's p
    for rel, body in files.items():
        (case / rel).write_text(body)
    a = CaseData(case, "a")
    assert "pX" in a.field_names() and "pX" not in CaseData(case, "b").field_names()
    assert np.allclose(a.fields(0.3, ["pX"])["pX"], np.arange(400.0))


def test_pinn_job_on_a_region(tmp_path):
    from aethercfd.ml.pinn import build_job

    case = two_region_case(tmp_path)
    job = build_job(case, physics="data", field="T", region="a", n_data=10, n_colloc=50)
    assert job.params["region"] == "a" and np.allclose(job._truth, 40.0)
    with pytest.raises(ValueError, match="choose a region"):
        build_job(case, physics="data", field="p", n_data=10)
    # the old keyword: region=(lo, hi) was the box
    one = build_job(FIX, physics="data", field="p", time=0, region=([0, 0, 0], [0.05, 1, 1]),
                    n_data=5, n_colloc=20)
    assert len(one.x_eval) == 200 and one.params["region"] == ""


# ------------------------------------------------------------------ copilot tools
def _ctx(case: Path):
    from aethercfd.commands.executor import AutoApprove, Executor
    from aethercfd.commands.registry import default_registry
    from aethercfd.copilot.tools import ToolContext
    from aethercfd.scripting.api import Session

    s = Session(case, create_project=True, with_backend=False)
    return ToolContext(s, executor=Executor(default_registry(), s.context, AutoApprove()))


def test_copilot_tools_take_a_region(tmp_path):
    from aethercfd.copilot.analysis_tools import (
        AssimParams,
        MeasParams,
        ModesParams,
        PinnParams,
        assimilate_measurements,
        compare_measurements,
        modal_analysis,
        pinn_train,
    )
    from aethercfd.copilot.tools import ToolError

    case = two_region_case(tmp_path)
    ctx = _ctx(case)
    for call in (lambda: modal_analysis(ctx, ModesParams(field="p")),
                 lambda: compare_measurements(ctx, MeasParams(csv="m.csv", field="p")),
                 lambda: pinn_train(ctx, PinnParams(rationale="r", confidence=0.5, physics="data", field="p"))):
        with open(case / "m.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["x", "y", "z", "p", "T"])
            w.writerows([[0.01 + 0.08 * i / 9, 0.05, 0.005, 5.0, 300.0] for i in range(10)])
        with pytest.raises(ToolError, match=r"multi-region case: pass region, one of a, b \(the fluid region is 'b'\)"):
            call()
    with pytest.raises(ToolError, match=r"no region 'zz'.*a, b"):
        modal_analysis(ctx, ModesParams(field="p", region="zz"))

    r = modal_analysis(ctx, ModesParams(field="p", region="a"))
    assert r["method"] == "POD" and r["times"] == [0.1, 0.2, 0.3] and r["energy_percent"][0] > 99
    rep = compare_measurements(ctx, MeasParams(csv="m.csv", field="p", region="a"))
    assert rep["region"] == "a" and rep["fields"]["p"]["bias"] == pytest.approx(35.0)  # 40 simulated vs 5
    rep = compare_measurements(ctx, MeasParams(csv="m.csv", field="p", region="b"))
    assert rep["fields"]["p"]["bias"] == pytest.approx(-1.0)  # 4 vs 5
    with pytest.raises(ToolError, match=r"no field 'T' at t = 0.3 in region b"):
        compare_measurements(ctx, MeasParams(csv="m.csv", field="T", region="b"))
    out = assimilate_measurements(ctx, AssimParams(csv="m.csv", field="p", region="b", sigma_b=2.0, length=0.02,
                                                   write=True, rationale="r", confidence=0.5))
    assert out["region"] == "b" and out["write"]["ok"]
    assert (case / "0.3" / "b" / "pAssim").is_file() and not (case / "0.3" / "pAssim").exists()
    assert "pAssim" in CaseData(case, "b").field_names()


def test_pinn_params_keep_the_old_box_keyword():
    from aethercfd.copilot.analysis_tools import PinnParams

    p = PinnParams(rationale="r", confidence=0.5, region=[0, 0, 0, 1, 1, 1])
    assert p.box == [0, 0, 0, 1, 1, 1] and p.region == ""
    p = PinnParams(rationale="r", confidence=0.5, region="fluid", box=[0, 0, 0, 1, 1, 1])
    assert p.region == "fluid" and len(p.box) == 6
    assert "region" in json.dumps(PinnParams.model_json_schema())


# ------------------------------------------------------------------ console
def test_console_data_is_the_fluid_region(tmp_path):
    from aethercfd.scripting.api import Session
    from aethercfd.scripting.console import EXAMPLES, Console

    c = Console(Session(two_region_case(tmp_path), create_project=True, with_backend=False),
                figures_dir=tmp_path / "figs")
    assert "region 'b'" in c.data_note and "data.for_region('a')" in c.data_note
    assert c.run("print(data.region, float(data.fields(names=['p'])['p'].max()))").output.strip() == "b 4.0"
    out = c.run(EXAMPLES["regions"], force=True).output
    assert "a " in out and "T 40.0 .. 40.0" in out and "Traceback" not in out
    single = tmp_path / "single"
    shutil.copytree(FIX, single)
    assert Console(Session(single, create_project=True, with_backend=False)).data_note == ""
