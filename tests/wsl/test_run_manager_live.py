"""Live run-manager tests (marker wsl_slow). Everything runs on copies under
D:\\OpenFOAM\\_aethercfd_scratch\\tests; nothing touches real cases; no distro is ever shut down."""

from __future__ import annotations

import collections
import shutil
import time
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.case.model import load_case
from aethercfd.core.events import EventBus
from aethercfd.documents.document import Document
from aethercfd.project.project import Placement, Project
from aethercfd.runs.manager import RunManager, RunState
from aethercfd.runs.spec import RunStep, StepKind
from tests.wsl._forks import by_family

pytestmark = [pytest.mark.wsl_slow, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL")]

SCRATCH = Path(r"D:\OpenFOAM\_aethercfd_scratch\tests")
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
BFS = Path(r"D:\OpenFOAM\backwardFacingStep2D")


@pytest.fixture(scope="module")
def runner():
    r = WslRunner("LMAE-OpenFOAM")
    busy = r.run_script("pgrep -f 'Foam|foamRun|mpirun' | grep -v pgrep | head -1\n").stdout.strip()
    if busy:
        pytest.skip("an OpenFOAM process is already running in the distro; not competing with it")
    return r


@pytest.fixture(scope="module")
def forks(runner):
    return by_family(probe_forks(runner))


_LAUNCHED: list[tuple[RunManager, str]] = []
_orig_launch = RunManager.launch


def _tracking_launch(self, rec, spec, **kw):
    _LAUNCHED.append((self, rec.id))
    return _orig_launch(self, rec, spec, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated settings + guaranteed cleanup: a failing test must never leave a solver running."""
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(RunManager, "launch", _tracking_launch)
    yield
    for mgr, run_id in _LAUNCHED:
        rec = mgr.get(run_id)
        if rec is not None and RunState(rec.state).active:
            mgr.control(run_id, "kill")
        mgr.shutdown()
    _LAUNCHED.clear()


def fresh(name: str, src: Path, *, subdirs=("system", "constant", "0")) -> Path:
    dst = SCRATCH / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for d in subdirs:
        shutil.copytree(src / d, dst / d)
    return dst


class Recorder:
    def __init__(self):
        self.bus = EventBus()
        self.events = collections.defaultdict(list)
        self.bus.subscribe("*", lambda t, p: self.events[t].append(p))

    def last_time(self):
        return self.events["run.progress"][-1]["step"].time if self.events["run.progress"] else None


def set_key(path: Path, key: str, value: str) -> None:
    d = Document.open(path)
    d.form_set(key, value)
    path.write_bytes(d.encoded())


def wait_until(pred, timeout=60, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(every)
    return False


def time_dirs(case: Path) -> set[str]:
    return {p.name for p in case.iterdir() if p.is_dir() and p.name.replace(".", "", 1).isdigit()}


@pytest.mark.skipif(not BFS.exists(), reason="corpus not available")
def test_reattach_pause_resume_and_pid_reuse_guard(runner, forks, env):
    case_dir = fresh("reattach_bfs", BFS)
    proj = Project.create(case_dir)
    rec_a = Recorder()
    mgr = RunManager(proj, runner, bus=rec_a.bus)
    rec, spec = mgr.prepare(load_case(case_dir), forks[Family.ESI], [RunStep("simpleFoam", kind=StepKind.SOLVER)])
    mgr.launch(rec, spec)
    assert wait_until(lambda: rec_a.last_time() and rec_a.last_time() > 20, 60)
    mgr.shutdown()  # the "GUI" goes away; the solver must keep running

    rec_b = Recorder()
    mgr2 = RunManager(proj, runner, bus=rec_b.bus)
    resumed = mgr2.reattach()
    assert [r.id for r in resumed] == [rec.id]
    assert wait_until(lambda: rec_b.events["run.progress"], 30)

    ok, _ = mgr2.control(rec.id, "pause")
    assert ok and mgr2.get(rec.id).state == RunState.PAUSED
    time.sleep(2.5)  # let the monitor drain log lines written before the pause
    n = len(rec_b.events["run.progress"])
    time.sleep(4)
    assert len(rec_b.events["run.progress"]) == n  # frozen: no new iterations while paused
    ok, _ = mgr2.control(rec.id, "resume")
    assert ok and wait_until(lambda: len(rec_b.events["run.progress"]) > n + 5, 30)

    tampered = mgr2.get(rec.id)
    tampered.start_jiffies = "1"
    assert mgr2.is_alive(tampered) is False  # same pgid, different process start time -> not ours

    ok, _ = mgr2.control(rec.id, "kill")
    final = mgr2.wait(rec.id, 120)
    assert final.state in (RunState.KILLED.value, RunState.FAILED.value) and final.exit_code not in (None, 0)
    assert runner.run_script("pgrep -x simpleFoam || echo none\n").stdout.strip() == "none"


def test_foundation_signals_via_site_dir(runner, forks, env):
    case_dir = fresh("of14_signals", FIX / "of14_cavity")
    set_key(case_dir / "system" / "controlDict", "endTime", "1000")
    proj = Project.create(case_dir)
    r = Recorder()
    mgr = RunManager(proj, runner, bus=r.bus)
    rec, spec = mgr.prepare(load_case(case_dir), forks[Family.FOUNDATION],
                            [RunStep("blockMesh"), RunStep("foamRun", kind=StepKind.SOLVER)])
    mgr.launch(rec, spec)
    assert wait_until(lambda: r.last_time() and r.last_time() > 0.05, 90)
    before = time_dirs(case_dir)
    ok, msg = mgr.control(rec.id, "write_now")
    assert ok, msg
    assert wait_until(lambda: len(time_dirs(case_dir) - before) >= 1, 30)
    ok, msg = mgr.control(rec.id, "stop_write")
    assert ok, msg
    final = mgr.wait(rec.id, 120)
    assert final.state == RunState.FINISHED.value and final.exit_code == 0
    assert len(time_dirs(case_dir) - before) >= 2  # write-now + stop-write


def test_staged_run_for_path_with_spaces(runner, forks, env):
    case_dir = fresh("staged case with spaces", FIX / "esi_cavity")
    owner_before = (case_dir / "constant" / "polyMesh" / "owner").read_bytes()
    proj = Project.create(case_dir)
    case = load_case(case_dir)
    assert not case.path_safety.safe
    r = Recorder()
    mgr = RunManager(proj, runner, bus=r.bus)
    rec, spec = mgr.prepare(case, forks[Family.ESI],
                            [RunStep("blockMesh"), RunStep("icoFoam", kind=StepKind.SOLVER)])
    assert rec.placement == Placement.STAGED.value and " " not in rec.exec_case_wsl
    mgr.launch(rec, spec, start_time="0")
    final = mgr.wait(rec.id, 300)
    assert final.state == RunState.FINISHED.value, final.stop_reason
    assert {"0.1", "0.5"} <= time_dirs(case_dir)  # results synced back to the Windows copy
    assert (case_dir / "log.icoFoam").exists()
    # blockMesh re-meshed inside the stage; sync-back never writes system/ or constant/
    assert (case_dir / "constant" / "polyMesh" / "owner").read_bytes() == owner_before
    assert r.events["run.synced"]


def test_parallel_run_signals_reach_ranks(runner, forks, env):
    case_dir = fresh("parallel_cavity", FIX / "esi_cavity")
    set_key(case_dir / "system" / "controlDict", "endTime", "1000")
    set_key(case_dir / "system" / "controlDict", "writeInterval", "100000")
    proj = Project.create(case_dir)
    r = Recorder()
    mgr = RunManager(proj, runner, bus=r.bus)
    steps = [RunStep("blockMesh"), RunStep("decomposePar", ("-force",)),
             RunStep("icoFoam", kind=StepKind.SOLVER, parallel=True)]
    rec, spec = mgr.prepare(load_case(case_dir), forks[Family.ESI], steps, nprocs=4)
    mgr.launch(rec, spec)
    assert wait_until(lambda: r.last_time() and r.last_time() > 0.2, 120)
    assert r.events["run.progress"][-1]["step"].residuals  # parsing works on the parallel log
    ok, msg = mgr.control(rec.id, "stop_write")
    assert ok, msg
    final = mgr.wait(rec.id, 120)
    assert final.state == RunState.FINISHED.value and final.exit_code == 0
    written = [{p.name for p in (case_dir / f"processor{i}").iterdir() if p.name not in ("0", "constant")}
               for i in range(4)]
    assert all(w and w == written[0] for w in written), written  # every rank wrote the same final time
    assert runner.run_script("pgrep -x icoFoam || echo none\n").stdout.strip() == "none"
