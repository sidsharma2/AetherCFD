"""PINN plumbing without torch: a fake worker speaks the worker's file protocol, so starting, following,
stopping, killing, failure messages, held-out errors and the saved record are tested on any computer (CI has
no PyTorch). The real training tests are in test_pinn.py and run only with a PyTorch environment."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import pytest

from aethercfd.ml import envsetup, pinn

FAKE = Path(__file__).with_name("fake_pinn_worker.py")
HEATER = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_multiRegionHeater"
APP_HAS_TORCH = importlib.util.find_spec("torch") is not None


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(pinn, "WORKER", FAKE)
    return sys.executable


def _job(mode: str, k: int = 1, **kw) -> pinn.PinnJob:
    rng = np.random.default_rng(0)
    return pinn.PinnJob("data", rng.random((10, 2)), rng.random((10, k)), rng.random((20, 2)), rng.random((30, 2)),
                        {"_fake": mode}, names=[f"f{j}" for j in range(k)], **kw)


def _gone(jd: Path) -> bool:
    pid = pinn._pid(jd)
    return pid is None or not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


# ------------------------------------------------------------------------------------------ no environment
def test_no_environment_says_how_to_get_one(monkeypatch, tmp_path):
    monkeypatch.setenv("AETHERCFD_ML_PYTHON", str(tmp_path / "nope" / "python.exe"))
    monkeypatch.setattr(pinn, "default_env_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(pinn, "__file__", str(tmp_path / "a" / "b" / "c" / "pinn.py"))  # no <repo>/.ml-env
    assert pinn.find_ml_python() is None
    st = pinn.ml_status()
    assert not st["available"] and "setup_ml_env.py" in st["hint"] and "AETHERCFD_ML_PYTHON" in st["hint"]
    st = pinn.ml_status(str(tmp_path / "typo.exe"))
    assert "typo.exe" in st["note"]
    with pytest.raises(RuntimeError, match=r"setup_ml_env\.py"):
        pinn.start(_job("ok"), job_dir=tmp_path / "j")


def test_env_folder_or_python_both_work(tmp_path):
    py = envsetup.env_python(tmp_path / "env")
    py.parent.mkdir(parents=True)
    py.write_text("")
    assert pinn.find_ml_python(str(tmp_path / "env")) == py
    assert pinn.find_ml_python(f'"{py}"') == py  # pasted with quotes


@pytest.mark.skipif(APP_HAS_TORCH, reason="the app's own Python has torch here")
def test_python_without_torch_is_reported_plainly():
    st = pinn.ml_status(sys.executable)
    assert not st["available"] and "PyTorch is not installed" in st["note"] and "setup_ml_env.py" in st["hint"]


@pytest.mark.skipif(APP_HAS_TORCH, reason="the app's own Python has torch here")
def test_real_worker_without_torch_writes_a_plain_result(tmp_path):
    jd = pinn.start(_job("ok"), ml_python=sys.executable, job_dir=tmp_path / "j")
    res = pinn.wait(jd, timeout_s=60)
    assert not res["ok"] and res["reason"] == "no_torch"
    assert "PyTorch is not installed" in res["error"] and "setup_ml_env.py" in res["error"]
    assert pinn.summary(res) == [res["error"]]


# ------------------------------------------------------------------------------------------ results
def test_result_reports_heldout_error_and_provenance(fake, tmp_path):
    job = pinn.build_job(HEATER, physics="data", field="T", region="topAir", n_data=30, n_colloc=200, seed=7,
                         n_holdout=50)
    assert job.params["seed"] == 7 and "50 random CFD cells" in job.params["val_source"]
    assert len(job.x_val) == 50 and not {tuple(r) for r in job.x_val} & {tuple(r) for r in job.x_data}
    jd = pinn.start(job, ml_python=fake, job_dir=tmp_path / "j")
    res = pinn.wait(jd, timeout_s=60)
    assert res["ok"] and res["kind"] == pinn.KIND
    # the fake is exact except +5 on the training cells: those are left out of the error vs the CFD
    assert res["rmse_over_std_vs_cfd"] == [0.0] and res["n_vs_cfd"] == len(job.eval_cells) - 30
    assert res["heldout"]["n"] == 50 and res["heldout"]["rmse_over_std"] == [0.1]
    text = " ".join(pinn.summary(res))
    assert "not a CFD solution" in text and "did not train on" in text and "held-out" in text and "seed 7" in text
    rec = json.loads((jd / "record.json").read_text())
    assert rec["seed"] == 7 and rec["torch"] == "fake" and rec["hyper"]["lr"] == 1e-3
    assert rec["job"]["region"] == "topAir" and rec["heldout"]["n"] == 50 and "parent_pid" not in rec["job"]
    (path, body), = pinn.record_file(res, "T", "0.4", "topAir").items()
    assert path == ".aethercfd/pinn/topAir_TPinn_0.4.json" and json.loads(body)["rmse_over_std_vs_cfd"] == [0.0]


def test_measurement_points_keep_some_back(tmp_path):
    job = pinn.build_job(HEATER, physics="data", field="T", region="topAir", n_colloc=50,
                         data_points=pinn.build_job(HEATER, physics="data", field="T", region="topAir", n_data=20,
                                                    n_colloc=5).x_data, data_values=np.full(20, 300.0))
    assert len(job.x_data) == 16 and len(job.x_val) == 4 and "measurement points" in job.params["val_source"]


def test_no_heldout_points_is_said(fake, tmp_path):
    jd = pinn.start(_job("ok"), ml_python=fake, job_dir=tmp_path / "j")
    res = pinn.wait(jd, timeout_s=60)
    assert res["ok"] and "accuracy away from the data is unknown" in " ".join(pinn.summary(res))


# ------------------------------------------------------------------------------------------ failures
def test_crash_without_result(fake, tmp_path):
    jd = pinn.start(_job("crash"), ml_python=fake, job_dir=tmp_path / "j")
    res = pinn.wait(jd, timeout_s=60)
    assert not res["ok"] and res["reason"] == "crash" and "boom in the fake worker" in res["error"]


def test_out_of_memory_in_the_log_is_named(fake, tmp_path):
    jd = pinn.start(_job("oom"), ml_python=fake, job_dir=tmp_path / "j")
    res = pinn.wait(jd, timeout_s=60)
    assert not res["ok"] and "ran out of memory" in res["error"]


def test_diverged_run_is_plain(fake, tmp_path):
    jd = pinn.start(_job("nan"), ml_python=fake, job_dir=tmp_path / "j")
    res = pinn.wait(jd, timeout_s=60)
    assert not res["ok"] and res["reason"] == "diverged" and "NaN" in pinn.summary(res)[0]


def test_cancel_a_cooperative_worker(fake, tmp_path):
    jd = pinn.start(_job("slow"), ml_python=fake, job_dir=tmp_path / "j")
    deadline = time.time() + 30
    while not pinn.progress(jd)["history"] and time.time() < deadline:
        time.sleep(0.05)
    assert not pinn.progress(jd)["done"]
    t0 = time.time()
    assert pinn.cancel(jd)
    assert time.time() - t0 < 5 and _gone(jd) and pinn.progress(jd)["done"]
    res = pinn.load(jd)
    assert not res["ok"] and res["reason"] == "cancelled" and res["history"]
    assert not pinn.cancel(jd)  # nothing left to stop


def test_cancel_kills_a_stuck_worker(fake, tmp_path):
    jd = pinn.start(_job("hang"), ml_python=fake, job_dir=tmp_path / "j")
    assert pinn.cancel(jd, grace_s=0.5)
    assert _gone(jd)
    res = pinn.load(jd)
    assert not res["ok"] and res["reason"] == "cancelled"


def test_cancel_from_another_process_view(fake, tmp_path):
    """The app restarted (no Popen handle): the worker is found by pid and start time and killed."""
    jd = pinn.start(_job("hang"), ml_python=fake, job_dir=tmp_path / "j")
    proc = pinn._PROCS.pop(str(jd))
    try:
        assert pinn._worker_alive(jd)
        assert pinn.cancel(jd, grace_s=0.2)
        assert proc.wait(10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_wait_timeout_stops_the_worker(fake, tmp_path):
    jd = pinn.start(_job("hang"), ml_python=fake, job_dir=tmp_path / "j")
    with pytest.raises(TimeoutError, match="was stopped"):
        pinn.wait(jd, timeout_s=0.5)
    assert _gone(jd)


def test_worker_killed_from_outside(fake, tmp_path):
    jd = pinn.start(_job("hang"), ml_python=fake, job_dir=tmp_path / "j")
    psutil.Process(pinn._pid(jd)).kill()
    res = pinn.wait(jd, timeout_s=30)
    assert not res["ok"] and "stopped without a result" in res["error"]


def test_a_new_run_clears_the_old_files(fake, tmp_path):
    jd = pinn.start(_job("slow"), ml_python=fake, job_dir=tmp_path / "j")
    pinn.cancel(jd)
    jd = pinn.start(_job("ok"), ml_python=fake, job_dir=tmp_path / "j")  # the cancel file must not stop it
    assert pinn.wait(jd, timeout_s=60)["ok"]


# ------------------------------------------------------------------------------------------ copilot tool
def _ctx(case: Path, ml_python):
    from aethercfd.commands.executor import AutoApprove, Executor
    from aethercfd.commands.registry import default_registry
    from aethercfd.copilot.tools import ToolContext
    from aethercfd.scripting.api import Session

    s = Session(case, create_project=True, with_backend=False)
    s.settings.ml_python = ml_python
    return ToolContext(s, executor=Executor(default_registry(), s.context, AutoApprove()))


def _heater(tmp_path) -> Path:
    case = tmp_path / "heater"
    shutil.copytree(HEATER, case)
    return case


def test_copilot_reply_without_environment(tmp_path, monkeypatch):
    from aethercfd.copilot.analysis_tools import PinnParams, pinn_train
    from aethercfd.copilot.tools import ToolError

    monkeypatch.setattr(pinn, "find_ml_python", lambda setting=None: None)
    ctx = _ctx(_heater(tmp_path), None)
    with pytest.raises(ToolError, match=r"No PyTorch environment found.*setup_ml_env\.py"):
        pinn_train(ctx, PinnParams(rationale="r", confidence=0.5, physics="data", field="T", region="topAir"))


def test_copilot_reply_is_honest_and_writes_a_record(fake, tmp_path):
    from aethercfd.copilot.analysis_tools import PinnParams, pinn_train

    case = _heater(tmp_path)
    ctx = _ctx(case, fake)
    out = pinn_train(ctx, PinnParams(rationale="r", confidence=0.5, physics="data", field="T", region="topAir",
                                     n_data=20, seed=3, write=True))
    assert out["kind"] == pinn.KIND and out["rmse_over_std_vs_cfd"] == [0.0] and out["heldout"]["n"] > 0
    assert out["seed"] == 3 and "not a CFD solution" in out["summary"] and out["write"]["ok"]
    assert (case / "0.4" / "topAir" / "TPinn").is_file()
    rec = json.loads((case / ".aethercfd" / "pinn" / "topAir_TPinn_0.4.json").read_text())
    assert rec["seed"] == 3 and rec["heldout"]["n"] == out["heldout"]["n"]


def test_copilot_failure_and_timeout(fake, tmp_path):
    from aethercfd.copilot.analysis_tools import PinnParams, pinn_train

    ctx = _ctx(_heater(tmp_path), fake)
    base = {"rationale": "r", "confidence": 0.5, "physics": "data", "field": "T", "region": "topAir", "n_data": 5}
    orig = pinn.build_job

    def with_mode(mode):
        def build(*a, **kw):
            job = orig(*a, **kw)
            job.params["_fake"] = mode
            return job
        return build

    import aethercfd.ml.pinn as mod

    mod.build_job = with_mode("crash")
    try:
        out = pinn_train(ctx, PinnParams(**base))
        assert not out["ok"] and out["reason"] == "crash" and "boom" in out["error"] and "log" in out
        mod.build_job = with_mode("hang")
        out = pinn_train(ctx, PinnParams(**base, wait_min=0.01))
        assert not out["ok"] and "was stopped" in out["error"] and "wait_min" in out["error"]
        assert _gone(Path(out["job_dir"]))
    finally:
        mod.build_job = orig


# ------------------------------------------------------------------------------------------ setup script
class Run:
    """Records commands instead of running them (no download); ``fail`` makes the n-th one fail."""

    def __init__(self, fail: int | None = None, smi: str = ""):
        self.cmds, self.fail, self.smi = [], fail, smi

    def __call__(self, cmd, **kw):
        self.cmds.append([str(c) for c in cmd])
        code = 1 if self.fail is not None and len(self.cmds) - 1 == self.fail else 0
        return type("R", (), {"returncode": code, "stdout": self.smi, "stderr": ""})()


GOOD = {"available": True, "python": "py", "torch": "2.14.1+cu126", "numpy": "2.5", "cuda": True, "gpu": "RTX",
        "cuda_version": "12.6", "python_version": "3.12"}


def test_setup_picks_the_build(monkeypatch):
    monkeypatch.setattr(envsetup.sys, "platform", "win32")
    smi = "| NVIDIA-SMI 581.42   Driver Version: 581.42   CUDA Version: 13.0 |"
    assert envsetup.driver_cuda(Run(smi=smi), lambda n: "nvidia-smi") == (13, 0)
    assert envsetup.driver_cuda(Run(), lambda n: None) is None
    assert envsetup.driver_cuda(Run(fail=0, smi=smi), lambda n: "nvidia-smi") is None
    assert "cu126" in envsetup.choose_build(False, None, (13, 0))[0]
    assert envsetup.choose_build(False, None, None)[0].endswith("/cpu")
    idx, why = envsetup.choose_build(False, None, (11, 8))
    assert idx.endswith("/cpu") and "update the driver" in why
    assert envsetup.choose_build(True, None, (13, 0))[0].endswith("/cpu")
    assert envsetup.choose_build(False, "cu128", None)[0].endswith("/cu128")


def test_setup_creates_checks_and_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(envsetup.sys, "platform", "win32")
    run, said, saved = Run(smi="CUDA Version: 12.8"), [], []
    code = envsetup.main(["--dir", str(tmp_path / "env")], run=run,
                         which=lambda n: {"uv": "uv", "nvidia-smi": "nvidia-smi"}.get(n),
                         check=lambda py: GOOD | {"python": str(py)}, save=lambda py: saved.append(py) or "saved",
                         out=said.append)
    assert code == 0, said
    venv, install = run.cmds[1], run.cmds[2]  # [0] is nvidia-smi
    assert venv[:2] == ["uv", "venv"] and "--python" in venv
    assert install[:3] == ["uv", "pip", "install"] and "torch" in install and install[-1].endswith("/cu126")
    assert saved == [envsetup.env_python((tmp_path / "env").resolve())]
    assert any("Check passed" in s and "RTX" in s for s in said)


def test_setup_without_uv_or_gpu_uses_venv_and_the_cpu_build(tmp_path, monkeypatch):
    monkeypatch.setattr(envsetup.sys, "platform", "linux")
    run = Run()
    code = envsetup.main(["--dir", str(tmp_path / "env"), "--no-settings"], run=run, which=lambda n: None,
                         check=lambda py: GOOD | {"cuda": False, "gpu": None}, save=pytest.fail, out=lambda s: None)
    assert code == 0
    assert run.cmds[0][1:3] == ["-m", "venv"] and run.cmds[1][1:4] == ["-m", "pip", "install"]
    assert run.cmds[1][-1].endswith("/cpu")


def test_setup_stops_on_a_failed_step(tmp_path):
    said = []
    code = envsetup.main(["--dir", str(tmp_path / "env"), "--cpu"], run=Run(fail=1), which=lambda n: None,
                         check=pytest.fail, save=pytest.fail, out=said.append)
    assert code == 1 and "Nothing was registered" in said[-1]


def test_setup_dry_run_and_check_only(tmp_path):
    run, said = Run(), []
    assert envsetup.main(["--dir", str(tmp_path / "env"), "--cpu", "--dry-run"], run=run, which=lambda n: None,
                         check=pytest.fail, save=pytest.fail, out=said.append) == 0
    assert run.cmds == [] and any("venv" in s for s in said)
    assert envsetup.main(["--dir", str(tmp_path / "env"), "--check"], out=said.append) == 1
    assert "No environment" in said[-1]


def test_setup_leaves_a_broken_env_alone_without_force(tmp_path):
    py = envsetup.env_python(tmp_path / "env")
    py.parent.mkdir(parents=True)
    py.write_text("")
    run, said = Run(), []
    code = envsetup.main(["--dir", str(tmp_path / "env")], run=run, which=lambda n: None,
                         check=lambda p: {"available": False, "note": "PyTorch does not load"}, save=pytest.fail,
                         out=said.append)
    assert code == 1 and run.cmds == [] and "--force" in said[-1]
    code = envsetup.main(["--dir", str(tmp_path / "env"), "--force", "--cpu", "--no-settings"], run=run,
                         which=lambda n: None, check=lambda p: GOOD, save=pytest.fail, out=said.append)
    assert code == 0 and len(run.cmds) == 1 and run.cmds[0][1:4] == ["-m", "pip", "install"]  # no new venv


def test_setup_script_runs_from_a_plain_python(tmp_path):
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "setup_ml_env.py"
    r = subprocess.run([sys.executable, "-I", str(script), "--dir", str(tmp_path / "env"), "--cpu", "--dry-run"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Installing PyTorch" in r.stdout, r.stderr
