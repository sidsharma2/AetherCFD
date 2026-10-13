"""Regression tests for the robustness audit (review findings + functional sweep, 2026-10-08)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from aethercfd.assim.measurements import assimilate, compare, load_measurements
from aethercfd.commands.base import unified_diff
from aethercfd.results.modes import pod
from aethercfd.scripting.console import Console


# ---------------------------------------------------------------- measurements
def test_measurements_header_spaces_semicolons_and_bad_rows(tmp_path):
    f = tmp_path / "m.csv"
    f.write_text("x, y, z, T\n0,0,0,300\n,1,0,310\n1,1,0,320\n")
    m = load_measurements(f)
    assert list(m.values) == ["T"] and len(m.points) == 2 and "skipped" in m.notes[0]
    g = tmp_path / "s.csv"
    g.write_text("x;y;T\n0;0;300\n1;0;310\n")
    assert len(load_measurements(g).points) == 2
    h = tmp_path / "bad.csv"
    h.write_text("x,y,T\n0,0,hot\n")
    with pytest.raises(ValueError, match="row 2"):
        load_measurements(h)
    e = tmp_path / "e.csv"
    e.write_text("")
    with pytest.raises(ValueError, match="no data rows"):
        load_measurements(e)


def _grid(n=10):
    g = np.linspace(0, 1, n)
    return np.array([[x, y, 0.0] for x in g for y in g])


def test_points_outside_the_mesh_are_left_out(tmp_path):
    c = _grid()
    v = c[:, 0] * 10
    f = tmp_path / "m.csv"
    f.write_text("x,y,z,T\n0.5,0.5,0,5\n1000,1000,0,99\n")  # second point: metres read as mm, say
    m = load_measurements(f)
    rep = compare(m, {"T": v}, c)
    assert rep["outside"] == 1 and rep["fields"]["T"]["n"] == 1 and "outside the mesh" in rep["notes"][0]
    a = assimilate(c, v, m.points, m.values["T"], 0.1, sigma_b=1.0, length=0.2)
    assert any("outside the mesh" in n for n in a.notes) and len(a.innovation) == 1
    with pytest.raises(ValueError, match="inside the mesh"):
        assimilate(c, v, np.array([[50.0, 50, 0]]), np.array([1.0]), 0.1, sigma_b=1.0, length=0.2)


def test_compare_with_no_valid_value_and_singular_covariance():
    c = _grid()
    v = np.ones(len(c))
    from aethercfd.assim.measurements import Measurements

    m = Measurements(np.array([[0.5, 0.5, 0.0]]), {"T": np.array([np.nan])})
    assert compare(m, {"T": v}, c)["fields"]["T"]["n"] == 0  # the panel used to KeyError on 'bias'
    pts = np.array([[0.5, 0.5, 0.0], [0.5, 0.5, 0.0]])  # same cell, zero uncertainty: S singular
    a = assimilate(c, v, pts, np.array([2.0, 2.0]), 0.0, sigma_b=1.0, length=0.2)
    assert np.isfinite(a.analysis).all() and any("singular" in n for n in a.notes)


def test_assimilation_refuses_a_huge_neighbourhood():
    c = np.random.default_rng(0).random((50_000, 3))
    pts = c[:2000]  # every cell near every point: 1e8 pairs
    with pytest.raises(ValueError, match="reduce the correlation length"):
        assimilate(c, np.zeros(len(c)), pts, np.ones(len(pts)), 0.1, sigma_b=1.0, length=10.0)


# ---------------------------------------------------------------- console
def test_console_runs_one_line_compound_statements():
    con = Console(None)
    out = con.run("for i in range(3): print(i)", force=True).output
    assert out.split() == ["0", "1", "2"]  # used to be silently dropped (waiting for a blank line)
    assert con.run("if True:", force=True).output.strip().endswith("Error: expected an indented block "
                                                                  "after 'if' statement on line 1") or \
        "SyntaxError" in con.run("if True:", force=True).output


# ---------------------------------------------------------------- diffs / modes
def test_large_file_diff_is_summarised_fast():
    a = "\n".join(["0"] * 200_000)
    b = "\n".join(["0"] * 199_000 + ["1"] * 1000)
    t0 = time.perf_counter()
    d = unified_diff(Path("TPinnErr"), a, b)
    assert time.perf_counter() - t0 < 1.0 and "line-by-line diff skipped" in d


def test_pod_of_identical_snapshots():
    r = pod(np.ones((5, 50)), n_modes=3)
    assert r.modes_for(0.9) == 0 and any("identical" in n for n in r.notes)


# ---------------------------------------------------------------- reader
def test_fields_on_disk_reads_headers_only(tmp_path, monkeypatch):
    import gzip

    from aethercfd.results.foam_reader import FoamResults

    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "controlDict").write_text("FoamFile{}\n")
    t = tmp_path / "100"
    t.mkdir()
    head = b'FoamFile\n{\n    class       volScalarField;\n    object T;\n}\n'
    (t / "T").write_bytes(head + b"0" * 5_000_000)
    with gzip.open(t / "k.gz", "wb") as fh:
        fh.write(head)
    (t / "phi").write_bytes(b'FoamFile\n{\n    class       surfaceScalarField;\n}\n')
    r = FoamResults(tmp_path)
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    assert sorted(r.fields_on_disk()) == ["T", "k"]
    assert not reads  # never the whole file
    assert sorted(r.fields_on_disk()) == ["T", "k"]  # cached by folder mtime


# ---------------------------------------------------------------- PINN worker lifecycle
def test_pinn_dead_worker_is_reported(tmp_path):
    from aethercfd.ml import pinn

    p = subprocess.Popen([sys.executable, "-c", "import sys; print('ModuleNotFoundError: torch'); sys.exit(1)"],
                         stdout=open(tmp_path / "worker.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115
    p.wait()
    (tmp_path / "worker.pid").write_text(f"{p.pid} 0")
    assert pinn.progress(tmp_path)["done"]  # used to poll forever
    res = pinn.load(tmp_path)
    assert not res["ok"] and "torch" in res["error"]


def test_pinn_refuses_a_second_run_in_a_busy_folder(tmp_path):
    from aethercfd.ml import pinn

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "worker.pid").write_text(f"{p.pid} {pinn._create_time(p.pid)}")
        assert pinn._worker_alive(tmp_path) and not pinn.progress(tmp_path)["done"]
        job = pinn.PinnJob("data", np.zeros((3, 2)), np.zeros((3, 1)), np.zeros((3, 2)), np.zeros((3, 2)), {})
        with pytest.raises(RuntimeError, match="still training"):
            pinn.start(job, ml_python=sys.executable, job_dir=tmp_path)
    finally:
        p.kill()
        p.wait()


def test_pinn_region_parsing():
    from aethercfd.ml.pinn import parse_region

    assert parse_region("") is None
    assert parse_region("0 0 0 1 1 1") == ([0, 0, 0], [1, 1, 1])
    for bad in ("0 0 0 1", "0 0 0 1 1 1 1", "1 0 0 0 1 1", "a b c d e f"):
        with pytest.raises(ValueError, match="region"):
            parse_region(bad)


def test_pinn_result_written_atomically():
    src = Path(__file__).parents[2] / "src" / "aethercfd" / "ml" / "pinn_worker.py"
    text = src.read_text(encoding="utf-8")
    assert '"result.json").write_text' not in text and "tmp.replace" in text
    json.dumps({})  # (keeps the json import honest for linters)


def test_region_names_from_the_vtk_reader():
    from aethercfd.results.foam_reader import ResultsMeta, region_of

    # VTK 9.7 prefixes region blocks with "/"; older versions do not
    assert region_of("/fluid/internalMesh") == "fluid"
    assert region_of("/solid_1_collar/patch/wall") == "solid_1_collar"
    assert region_of("fluid/internalMesh") == "fluid"
    assert region_of("fluid/patch/inlet") == "fluid"
    for default in ("internalMesh", "patch/inlet", "group/wall"):
        assert region_of(default) == ""
    m = ResultsMeta(patches=["internalMesh", "patch/inlet", "/fluid/internalMesh", "/solid/internalMesh"],
                    regions=["fluid", "solid"])
    # the top-level mesh of a CHT case (often the unsplit original, no fields) must not overlap the regions
    assert m.internal_meshes == ["/fluid/internalMesh", "/solid/internalMesh"]
    assert ResultsMeta(patches=["internalMesh", "patch/inlet"]).internal_meshes == ["internalMesh"]


def test_reads_never_share_dataset_objects_with_the_reader():
    """The viewport draws one read's datasets on the GUI thread while its worker reads again with the same
    reader; sharing the reader's own leaf objects (a plain multiblock ShallowCopy) deadlocked the app."""
    pytest.importorskip("vtk")
    from aethercfd.results.foam_reader import FoamResults, iter_leaves

    case = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"
    if not (case / "constant" / "polyMesh").is_dir():
        pytest.skip("fixture without a mesh")
    r = FoamResults(case)
    first = dict(iter_leaves(r.read(None)))
    mine = {id(ds) for ds in first.values()}
    theirs = [ds for _, ds in iter_leaves(r.reader.GetOutput())]
    assert first and not (mine & {id(ds) for ds in theirs})

    def arrays(datasets):  # the points and field arrays (the reader reuses its cached mesh arrays)
        out = set()
        for ds in datasets:
            if ds.GetPoints() is not None:
                out.add(ds.GetPoints().GetData().__this__)  # the C++ address (Python wrapper ids get reused)
            cd = ds.GetCellData()
            out |= {cd.GetAbstractArray(i).__this__ for i in range(cd.GetNumberOfArrays())}
        return out

    assert not (arrays(first.values()) & arrays(theirs))  # a shallow copy of each leaf still shares these
    cells = {k: ds.GetNumberOfCells() for k, ds in first.items()}
    second = dict(iter_leaves(r.read(None, cell_to_point=True)))
    assert not ({id(ds) for ds in second.values()} & mine)
    assert {k: ds.GetNumberOfCells() for k, ds in first.items()} == cells  # the first read is left alone


def test_pod_and_dmd_agree_on_cpu_and_gpu():
    from aethercfd.results.modes import GPU_POD_MIN_VALUES, _linalg_backend, dmd, pod

    if _linalg_backend("cuda")[1] != "cuda":
        pytest.skip("no CUDA linear algebra on this machine")
    rng = np.random.default_rng(1)
    t = np.arange(12)[:, None]
    X = np.sin(0.4 * t) * rng.random(5000) + 0.3 * np.cos(1.1 * t) * rng.random(5000) + 0.01 * rng.random((12, 5000))
    a, b = pod(X, n_modes=3, backend="cpu"), pod(X, n_modes=3, backend="cuda")
    assert b.backend == "cuda" and np.allclose(a.energy, b.energy, rtol=1e-8)
    da, db = dmd(X, dt=1.0, rank=4, backend="cpu"), dmd(X, dt=1.0, rank=4, backend="cuda")
    assert np.allclose(np.sort(np.abs(da.eigenvalues)), np.sort(np.abs(db.eigenvalues)), rtol=1e-6)
    # auto: small fields and DMD stay on the CPU (measured faster there); large POD goes to the GPU
    assert pod(X, n_modes=2).backend == "cpu" and dmd(X, dt=1.0, rank=2).backend == "cpu"
    assert GPU_POD_MIN_VALUES >= 1_000_000
