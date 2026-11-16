"""Restart from the last good time (roadmap Q6): the right time folder, gentler numerics, one journalled plan."""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from aethercfd.case import rollback as rb
from aethercfd.case.model import load_case
from aethercfd.commands.base import USER
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.foamdict.parser import parse
from aethercfd.foamdict.semantic import DictView
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"


def _case(tmp_path, name="esi_pitzDaily") -> Path:
    c = tmp_path / name
    shutil.copytree(FIX / name, c)
    Project.create(c)
    return c


def _write_time(root: Path, name: str, *, nan: bool = False) -> None:
    d = root / name
    shutil.copytree(root / "0", d)
    if nan:
        p = d / "p"
        p.write_text(p.read_text().replace("uniform 0;", "nonuniform List<scalar> 3(0 -nan 1);", 1))


def _value(root: Path, rel: str, key: str) -> str | None:
    view = DictView(parse((root / rel).read_text()))
    lk = view.lookup(key)
    return None if lk is None or lk.entry is None else view.doc.text(lk.entry.value.start, lk.entry.value.end).strip()


def test_restart_time_skips_times_after_the_last_good_one_and_times_with_nan(tmp_path):
    root = _case(tmp_path)
    for name, nan in (("100", False), ("200", True), ("300", False), ("347", True)):
        _write_time(root, name, nan=nan)
    start, later = rb.restart_time(root, 320.0)
    assert start.name == "300" and [t.name for t in later] == ["347"]
    start, later = rb.restart_time(root, 250.0)
    assert start.name == "100" and [t.name for t in later] == ["200", "300", "347"]
    start, _ = rb.restart_time(root, None)  # diverged before the first write: only the initial time
    assert start.name == "0"


def test_field_scan_ignores_the_header_and_reads_gzip_but_not_binary(tmp_path):
    p = tmp_path / "U"
    p.write_text('FoamFile { version 2.0; format ascii; class volVectorField; note "info"; }\ninternalField uniform (1 0 0);')
    assert rb.field_is_bad(p) is False
    g = tmp_path / "k.gz"
    g.write_bytes(gzip.compress(b"FoamFile { format ascii; }\ninternalField nonuniform List<scalar> 2(1 inf);"))
    assert rb.field_is_bad(g) is True
    b = tmp_path / "p"
    b.write_bytes(b"FoamFile { format binary; }\ninternalField nonuniform List<scalar> 2(\x00\x01nan)")
    assert rb.field_is_bad(b) is None


def test_steady_simplec_plan_lowers_the_equation_relaxation_and_applies_as_one_change(tmp_path):
    root = _case(tmp_path)
    _write_time(root, "100")
    _write_time(root, "180", nan=True)
    plan = rb.plan_restart(root, {"last_ok_time": 150.0, "time": 180.0}, first_order=True)
    assert not plan.errors and plan.start.name == "100" and plan.later == ["180"]
    keys = {(Path(e["file"]).name, e["key"]): e["value"] for e in plan.edits}
    assert keys[("controlDict", "startFrom")] == "startTime" and keys[("controlDict", "startTime")] == "100"
    assert keys[("fvSolution", "relaxationFactors/equations/U")] == "0.63"
    assert keys[("fvSolution", "relaxationFactors/equations/.*")] == "0.63"
    assert keys[("fvSchemes", "divSchemes/div(phi,U)")] == "bounded Gauss upwind"
    assert ("fvSchemes", "divSchemes/div(phi,k)") not in keys  # a $macro value is never rewritten
    res = Executor(default_registry(), Context(Project.find(root)), AutoApprove()).execute(
        "dict.batch", plan.batch_params(), USER)
    assert res.ok, res.message
    assert _value(root, "system/controlDict", "startTime") == "100"
    assert _value(root, "system/fvSolution", "relaxationFactors/equations/U") == "0.63"
    assert (root / "180").is_dir()  # nothing is deleted


def test_transient_plan_halves_the_time_step(tmp_path):
    root = _case(tmp_path, "esi_cavity")
    _write_time(root, "0.1")
    case = load_case(root)
    plan = rb.plan_restart(case, {"last_ok_time": 0.12})
    keys = {e["key"]: e["value"] for e in plan.edits}
    assert plan.start.name == "0.1" and keys["deltaT"] == "0.0025"
    assert not any("relaxation" in k for k in keys)


def test_no_good_time_is_an_error(tmp_path):
    root = _case(tmp_path)
    (root / "0" / "p").write_text((root / "0" / "p").read_text().replace("uniform 0;", "uniform nan;", 1))
    plan = rb.plan_restart(root, {"last_ok_time": None})
    assert plan.start is None and plan.errors


def test_plain_simple_relaxation_is_capped_at_the_textbook_values(tmp_path):
    """On real OpenFOAM 14, p 0.9 x 0.7 = 0.63 with plain SIMPLE ran 2000 iterations stalled near 1e-3."""
    root = _case(tmp_path)
    fvs = root / "system" / "fvSolution"
    text = fvs.read_text().replace("consistent      yes;", "consistent      no;")
    text = text.replace("    equations\n", "    fields { p 0.9; }\n    equations\n", 1)
    fvs.write_text(text)
    _write_time(root, "10")
    keys = {e["key"]: e["value"] for e in rb.plan_restart(root, {"last_ok_time": 10.0}).edits}
    assert keys["relaxationFactors/fields/p"] == "0.3"
    assert keys["relaxationFactors/equations/U"] == "0.63"  # 0.9 x 0.7 is under the 0.7 cap


def test_a_run_that_diverged_early_restarts_from_where_it_started(tmp_path):
    root = _case(tmp_path)
    for name in ("5", "10", "15"):
        _write_time(root, name)
    plan = rb.plan_restart(root, {"last_ok_time": 10.0, "step": 14, "first_time": 1.0})
    assert plan.start.name == "0" and any("first 14 steps" in n for n in plan.notes)
    plan = rb.plan_restart(root, {"last_ok_time": 10.0, "step": 14, "first_time": 6.0})  # continued from 5
    assert plan.start.name == "5"
    plan = rb.plan_restart(root, {"last_ok_time": 10.0, "step": 400, "first_time": 1.0})
    assert plan.start.name == "10"
