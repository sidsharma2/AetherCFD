from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.case.discovery import discover_cases
from aethercfd.case.model import FileKind, classify_file, load_case
from aethercfd.project.journal import Journal, ObjectStore
from aethercfd.project.project import SIDECAR, CaseConfig, Mode, Placement, Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
REAL = {
    "bfs": Path(r"D:\OpenFOAM\backwardFacingStep2D"),
    "sandia": Path(r"D:\OpenFOAM\SandiaFlameD"),
    "rpl04": Path(r"D:\OpenFOAM\RPL Apollo Lunar Module Ascent Engine\cases\04_fans_internal\openfoam"),
    "cht": Path(r"D:\OpenFOAM\AutoFOAM_Agents\cases\probeStudy_station4\analysis\C1d_time1_annulus_probe_20260914"),
}


def tree_state(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            st = os.stat(os.path.join(dp, f))
            out[os.path.join(dp, f)] = (st.st_size, st.st_mtime_ns)
    return out


def test_fixture_forks_are_detected():
    esi = load_case(FIX / "esi_cavity")
    of = load_case(FIX / "of14_cavity")
    assert esi.fork_guess.family is Family.ESI and esi.application == "icoFoam"
    assert of.fork_guess.family is Family.FOUNDATION and of.solver_module == "incompressibleFluid"
    assert of.solver_label == "foamRun -solver incompressibleFluid"
    assert esi.regions[0].is_2d and esi.n_cells == 400


def test_fork_guess_picks_installed_profile():
    forks = [ForkProfile("esi-v2306", Family.ESI, "v2306", "D", "/a", "/a"),
             ForkProfile("foundation-14", Family.FOUNDATION, "14", "D", "/b", "/b")]
    assert load_case(FIX / "of14_cavity").fork_guess.pick(forks).id == "foundation-14"
    assert load_case(FIX / "esi_cavity").fork_guess.pick(forks).id == "esi-v2306"


def test_fork_guess_with_two_esi_versions_installed():
    from aethercfd.case.fork_detect import ForkGuess

    forks = [ForkProfile("esi-v2606", Family.ESI, "v2606", "D", "/c", "/c"),
             ForkProfile("esi-v2306", Family.ESI, "v2306", "D", "/a", "/a"),
             ForkProfile("foundation-9", Family.FOUNDATION, "9", "D", "/d", "/d"),
             ForkProfile("foundation-14", Family.FOUNDATION, "14", "D", "/b", "/b")]

    def pick(fam, hint):
        return ForkGuess(fam, hint, 1.0).pick(forks).id

    assert pick(Family.ESI, "v2306") == "esi-v2306"  # exact
    assert pick(Family.ESI, "v2606") == "esi-v2606"
    assert pick(Family.ESI, "v2412") == "esi-v2306"  # newest not newer than the case
    assert pick(Family.ESI, "v2612") == "esi-v2606"
    assert pick(Family.ESI, "v2012") == "esi-v2306"  # older than everything installed: the closest
    assert pick(Family.ESI, None) == "esi-v2306"  # no hint: existing cases stay on the oldest install
    assert pick(Family.FOUNDATION, None) == "foundation-9"
    assert pick(Family.FOUNDATION, "12") == "foundation-9"  # numeric, not string, order
    assert pick(Family.FOUNDATION, "14") == "foundation-14"
    assert pick(Family.FOUNDATION, "dev") == "foundation-14"


def test_classify_file():
    root = FIX / "esi_cavity"
    assert classify_file(root, root / "system" / "controlDict") is FileKind.DICT
    assert classify_file(root, root / "0" / "U") is FileKind.FIELD
    assert classify_file(root, root / "constant" / "polyMesh" / "points") is FileKind.MESH
    assert classify_file(root, root / "log.icoFoam") is FileKind.LOG
    assert classify_file(root, root / "Allrun") is FileKind.SCRIPT


@pytest.mark.skipif(not REAL["bfs"].exists(), reason="corpus not available")
def test_real_cases_facts_and_read_only_invariant():
    before = {k: tree_state(p) for k, p in REAL.items() if p.exists()}
    bfs = load_case(REAL["bfs"])
    sandia = load_case(REAL["sandia"])
    rpl = load_case(REAL["rpl04"]) if REAL["rpl04"].exists() else None
    cht = load_case(REAL["cht"]) if REAL["cht"].exists() else None
    assert bfs.regions[0].is_2d and bfs.n_cells == 20540 and bfs.application == "simpleFoam"
    assert sandia.regions[0].is_axisymmetric and len(sandia.times) >= 30
    if rpl:
        assert not rpl.path_safety.safe and rpl.generated_by and "generate_case04" in rpl.generated_by
    if cht:
        assert cht.multi_region and "fluid" in [r.name for r in cht.regions] and cht.n_cells == 1430400
    after = {k: tree_state(p) for k, p in REAL.items() if p.exists()}
    assert before == after, "loading a case must never write into it"


def test_discover_cases(tmp_path):
    shutil.copytree(FIX / "esi_cavity", tmp_path / "a" / "esi")
    shutil.copytree(FIX / "of14_cavity", tmp_path / "b" / "c" / "of14")
    found = discover_cases(tmp_path)
    assert sorted(p.name for p in found) == ["esi", "of14"]


def test_project_sidecar_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "work" / "cavity"
    shutil.copytree(FIX / "esi_cavity", case)
    subprocess.run(["git", "init", "-q", str(tmp_path / "work")], check=True)
    assert Project.find(case) is None
    Project.create(case, cases=[CaseConfig(".", fork="esi-v2306", placement=Placement.IN_PLACE)])
    assert (case / SIDECAR / "project.toml").exists()
    assert (case / SIDECAR / "local" / ".gitignore").read_text().strip().endswith("*")
    exclude = (tmp_path / "work" / ".git" / "info" / "exclude").read_text()
    assert "/cavity/.aethercfd/" in exclude
    again = Project.find(case)
    assert again is not None and again.cases[0].fork == "esi-v2306" and again.cases[0].placement is Placement.IN_PLACE
    with pytest.raises(FileExistsError):
        Project.create(case)


def test_project_external_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "ro_case"
    shutil.copytree(FIX / "of14_cavity", case)
    before = tree_state(case)
    proj = Project.create(case, mode=Mode.EXTERNAL)
    assert tree_state(case) == before  # nothing written next to the case
    assert Project.find(case).id == proj.id
    proj.ensure_case(case)
    assert Project.find(case).cases[0].path == "."


def test_journal_and_objects(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    sha = store.put(b"endTime 2000;\n")
    assert store.put(b"endTime 2000;\n") == sha and store.get(sha) == b"endTime 2000;\n"
    j = Journal(tmp_path / "journal")
    e1 = j.append({"command": "dict.set", "params": {"path": "endTime"}})
    e2 = j.append({"command": "file.save"})
    assert [e["id"] for e in j.entries()] == [e1["id"], e2["id"]]
    assert next(j.entries(newest_first=True))["id"] == e2["id"]
    with open(next(iter(sorted((tmp_path / "journal").glob("*.jsonl")))), "a", encoding="utf-8") as fh:
        fh.write('{"torn": ')  # crash mid-write
    assert len(list(j.entries())) == 2
    assert j.find(e1["id"])["command"] == "dict.set"


def test_load_case_accepts_a_relative_path(tmp_path, monkeypatch):
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity", tmp_path / "cav")
    monkeypatch.chdir(tmp_path)
    c = load_case("cav")  # raised PathTranslationError ("not an absolute drive path")
    assert c.root == tmp_path / "cav" and c.root.is_absolute()


def test_a_damaged_project_file_says_how_to_recover(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "cav"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity", case)
    proj = Project.create(case)
    pt = proj.meta_dir / "project.toml"
    pt.write_text(pt.read_text(encoding="utf-8") + "<<<<<<< HEAD\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"is damaged.*fix it by hand.*rename the .aethercfd folder"):
        Project.find(case)


def test_a_damaged_external_project_does_not_hide_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    a, b = tmp_path / "a", tmp_path / "b"
    for c in (a, b):
        shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity", c)
    pa = Project.create(a, mode=Mode.EXTERNAL)
    Project.create(b, mode=Mode.EXTERNAL)
    (pa.meta_dir / "project.toml").write_text("[project\n", encoding="utf-8")
    pa.meta_dir.rename(pa.meta_dir.parent / "000-first")  # listed before b's folder
    found = Project.find(b)
    assert found is not None and found.root == b.resolve()
