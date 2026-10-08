from __future__ import annotations

import re
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.case.model import load_case
from aethercfd.project.project import Placement
from aethercfd.runs.placement import Stage, resolve_placement
from aethercfd.runs.scripts import control_sh, guard_sh, launch_sh, runner_sh
from aethercfd.runs.spec import RunSpec, RunStep, StepKind

ESI = ForkProfile("esi-v2306", Family.ESI, "v2306", "LMAE-OpenFOAM", "/usr/lib/openfoam/openfoam2306/etc/bashrc",
                  "/usr/lib/openfoam/openfoam2306", True)
OF = ForkProfile("foundation-14", Family.FOUNDATION, "14", "LMAE-OpenFOAM", "/opt/openfoam14/etc/bashrc",
                 "/opt/openfoam14", True)
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"


def spec(fork=ESI, **kw) -> RunSpec:
    base = dict(case_wsl="/mnt/d/OpenFOAM/x", fork=fork,
                steps=(RunStep("blockMesh"), RunStep("simpleFoam", kind=StepKind.SOLVER)),
                run_dir_wsl="/mnt/d/My Projects/case/.aethercfd/local/runs/1", linux_run_dir="/root/aethercfd/runs/1",
                run_id="1")
    base.update(kw)
    return RunSpec(**base)


def test_runner_script_basics():
    s = runner_sh(spec())
    assert "\r" not in s
    assert "RUN_DIR='/mnt/d/My Projects/case/.aethercfd/local/runs/1'" in s  # quoted, spaces survive
    assert "-opt-switch writeNowSignal=10 -opt-switch stopAtWriteNowSignal=12" in s  # ESI signals on solver
    assert s.count("-opt-switch writeNowSignal") == 1  # ...and not on blockMesh
    assert "WM_PROJECT_SITE" not in s
    assert "exit 97" in s and "exit 98" in s  # fork prelude
    assert "trap '' USR1 USR2" in s and "exit 143' TERM" in s
    assert re.search(r"ulimit -f \d+", s)


def test_foundation_uses_private_site_dir_without_spaces():
    s = runner_sh(spec(OF))
    assert "-opt-switch" not in s
    assert 'export WM_PROJECT_SITE="$LRUN/site"' in s and "LRUN=/root/aethercfd/runs/1" in s
    assert "writeNowSignal 10; stopAtWriteNowSignal 12;" in s


def test_signals_disabled_means_no_switches():
    assert "-opt-switch" not in runner_sh(spec(enable_signals=False))
    assert "WM_PROJECT_SITE" not in runner_sh(spec(OF, enable_signals=False))


def test_parallel_step_uses_mpirun_as_root():
    s = runner_sh(spec(steps=(RunStep("icoFoam", kind=StepKind.SOLVER, parallel=True),), nprocs=4))
    assert "mpirun -np 4 --bind-to none --allow-run-as-root icoFoam -opt-switch" in s and "-parallel" in s


def test_validation():
    assert spec(case_wsl="/mnt/d/a b").validate()  # OpenFOAM cannot run in a path with spaces
    assert spec(steps=(RunStep("rm -rf /"),)).validate()
    assert spec(steps=(RunStep("icoFoam", parallel=True),), nprocs=1).validate()
    assert not spec().validate()


def test_usr_signals_never_target_the_process_group():
    for action in ("write_now", "stop_write"):
        s = control_sh(action, "/mnt/d/r", 123)
        assert "kill -USR" in s and "-USR1 -" not in s and "-USR2 -" not in s and '"$SPID"' in s
    assert "kill -STOP -\"$PGID\"" in control_sh("pause", "/mnt/d/r", 123)
    k = control_sh("kill", "/mnt/d/r", 123)
    assert "kill -TERM" in k and "kill -KILL" in k and "stop_requested" in k


def test_launch_and_guard_scripts():
    sp = spec()
    launch = launch_sh(sp)
    assert "setsid -f bash" in launch and "runner.pid" in launch and "\r" not in launch
    g = guard_sh(sp, log_cap_gb=1)
    assert f"CAP={1024**3}" in g and "stats.jsonl" in g and "guard_stop.json" in g and "\r" not in g


def test_placement_resolution(tmp_path):
    safe = load_case(FIX / "esi_cavity")
    assert resolve_placement(safe, Placement.AUTO)[0] is Placement.IN_PLACE
    assert resolve_placement(safe, Placement.STAGED)[0] is Placement.STAGED
    assert resolve_placement(safe, Placement.AUTO, heavy_io=True)[0] is Placement.STAGED
    import shutil
    spaced = tmp_path / "with space"
    shutil.copytree(FIX / "esi_cavity", spaced)
    p, why = resolve_placement(load_case(spaced), Placement.IN_PLACE)
    assert p is Placement.STAGED and "space" in why


def test_stage_scripts_never_sync_inputs_back():
    st = Stage("/mnt/d/a b/case", "/root/aethercfd/stage/p/case")
    back = st.sync_back_sh(skip_newest=True)
    assert "system" not in back and "constant" not in back and "\r" not in back
    stage_in = st.stage_in_sh("0")
    assert "--include=/system/***" in stage_in and "--include=/0/***" in stage_in and "never synced back" in stage_in


@pytest.mark.parametrize("app", ["simpleFoam", "./Allrun"])
def test_step_log_names(app):
    assert RunStep(app).log in ("log.simpleFoam", "log.Allrun")


def test_existing_step_log_is_kept_not_overwritten(tmp_path):
    """The step() prologue moves an earlier log aside (continuation runs re-use the solver's log name)."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        import pytest

        pytest.skip("no bash")
    log = tmp_path / "log.rhoSimpleFoam"
    log.write_text("stage A\n")
    line = '  if [ -s "$log" ]; then mv -n "$log" "$log.$(date -r "$log" +%Y%m%d-%H%M%S)"; fi'
    assert line in runner_sh(spec())
    r = subprocess.run([bash, "-c", f'log={log.as_posix()!r}\n{line}\necho "stage B" > "$log"'], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    kept = [p for p in tmp_path.iterdir() if p.name.startswith("log.rhoSimpleFoam.")]
    assert len(kept) == 1 and kept[0].read_text() == "stage A\n" and log.read_text() == "stage B\n"
