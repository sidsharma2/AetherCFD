"""The SSH/rsync scripts AetherCFD runs inside the WSL distro for cluster runs (ADR 0007), checked as text.

Nothing here starts ssh: a capturing runner stands in for WslRunner.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from aethercfd.backend.wsl.runner import ScriptResult
from aethercfd.core.clusters import ClusterProfile
from aethercfd.runs.cluster.transport import (
    NO_SSH,
    RUN_FILES,
    UNREACHABLE,
    WslSshTransport,
    check_connection,
    check_script,
    parse_check,
    parse_work_dir,
    resolve_work_dir_script,
    unreachable,
)

PROFILE = ClusterProfile(name="hpc", host="login.example.org", user="sid", port=2222,
                         identity_file="~/.ssh/id_ed25519", jump_host="sid@gw.example.org",
                         openfoam_version="v2306", env_setup="module load openfoam/v2306",
                         work_dir="$SCRATCH/aethercfd")


class CapturingRunner:
    distro = "LMAE-OpenFOAM"

    def __init__(self, result: ScriptResult | None = None):
        self.bodies: list[str] = []
        self.result = result or ScriptResult(0, "", "")

    def run_script(self, body: str, args=(), *, timeout_s: float = 120) -> ScriptResult:
        assert "\r" not in body
        self.bodies.append(body)
        return self.result


def _tr(profile: ClusterProfile = PROFILE, result: ScriptResult | None = None):
    r = CapturingRunner(result)
    return WslSshTransport(profile, r), r  # type: ignore[arg-type]


def test_ssh_is_non_interactive_and_multiplexed():
    tr, _ = _tr()
    o = tr.ssh_options()
    assert o[:2] == ["-o", "BatchMode=yes"]
    s = " ".join(o)
    assert "-p 2222" in s and "-i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes" in s and "-J sid@gw.example.org" in s
    assert "ControlMaster=auto" in s and "ControlPersist=8h" in s and "ControlPath=~/.ssh/aethercfd-hpc-%C" in s
    plain, _ = _tr(ClusterProfile(name="x", host="h", use_control_master=False))
    assert "Control" not in " ".join(plain.ssh_options()) and "-p" not in plain.ssh_options()


def test_remote_script_travels_base64_into_a_login_shell():
    tr, r = _tr()
    remote = "echo 'it''s'; module load x && printf '%s\\n' \"$HOME\"\n"
    tr.run(remote)
    body = r.bodies[-1]
    b64 = re.search(r"printf '%s' '?([A-Za-z0-9+/=]+)'? \| base64 -d \| rssh \"\$DEST\"", body).group(1)
    assert base64.b64decode(b64).decode() == remote
    assert "bash -l -c" in body and 'script=$(cat); exec </dev/null; eval "$script"' in body
    assert "DEST=sid@login.example.org" in body
    assert f"exit {NO_SSH}" in body  # ssh/rsync missing in the distro is reported, never installed
    tr2, r2 = _tr(PROFILE.model_copy(update={"login_shell": False}))
    tr2.run("true\n")
    assert "bash --noprofile --norc -c" in r2.bodies[-1]


def test_upload_sends_inputs_only():
    tr, r = _tr()
    tr.upload_case(Path(r"D:\OpenFOAM\cavity"), "/scratch/sid/aethercfd/p/cavity/case", start_time="0.5")
    b = r.bodies[-1]
    assert "SRC=/mnt/d/OpenFOAM/cavity" in b
    assert "--include=/system/***" in b and "--include=/constant/***" in b and "--include=/0.5/***" in b
    assert "--exclude='/log.*'" in b and "--exclude='/*/'" in b
    assert "sid@login.example.org:/scratch/sid/aethercfd/p/cavity/case/" in b
    assert "rm -f /scratch/sid/aethercfd/p/cavity/case/.aethercfd_synced" in b


def test_live_pull_appends_run_files_and_logs():
    tr, r = _tr()
    tr.pull_live("/s/runs/r1", "/s/case", Path(r"D:\P\.aethercfd\local\runs\r1"))
    b = r.bodies[-1]
    assert b.count("rsync -a --append") == 2
    assert all(f"--include={f}" in b for f in RUN_FILES) and "--include='log.*'" in b
    assert 'LRUN=/mnt/d/P/.aethercfd/local/runs/r1' in b and '"$LRUN/case/"' in b
    assert f"exit {UNREACHABLE}" in b


@pytest.mark.parametrize("final", [False, True])
def test_results_download(final):
    tr, r = _tr()
    tr.download_results("/s/case", Path(r"D:\OpenFOAM\cavity"), skip_newest=not final, processor_dirs=True)
    b = r.bodies[-1]
    assert f"if {'false' if final else 'true'} && [ \"$t\" = \"$newest\" ]" in b
    assert "postProcessing" in b and "--include='log.*'" in b and "processor0" in b
    assert ("touch /s/case/.aethercfd_synced" in b) == final
    assert "system" not in b and "constant" not in b  # never brought back
    tr2, r2 = _tr()
    tr2.download_results("/s/case", Path(r"D:\x"), skip_newest=True, processor_dirs=False)
    assert "processor" not in r2.bodies[-1]


def test_interactive_master_console():
    tr, _ = _tr()
    argv = tr.master_console_argv()
    script = argv[-1]
    assert argv[:4] == ["bash", "--noprofile", "--norc", "-c"]
    assert "BatchMode" not in script and "ControlMaster=yes" in script and "-fN" in script
    assert "sid@login.example.org" in script and "never sees" in script


def test_connection_check_report():
    out = ("banner\nAETHER_CHECK\nHOST=login1\nHAVE=sbatch\nHAVE=squeue\nMISSING=sacct\nHAVE=scancel\nHAVE=rsync\n"
           "WORK=/scratch/sid/aethercfd\nWMV=v2306\nWMD=/apps/openfoam/v2306\nAETHER_END\n")
    lines, work = parse_check(PROFILE, ScriptResult(0, out, ""))
    assert work == "/scratch/sid/aethercfd"
    text = "\n".join(f"{ln.ok} {ln.text}" for ln in lines)
    assert "True connected to login1" in text and "False not found on the login node: sacct" in text
    assert "True OpenFOAM v2306" in text
    bad, work = parse_check(PROFILE.model_copy(update={"openfoam_version": "v2312"}),
                            ScriptResult(0, out.replace("WORK=", "WORK_BAD="), ""))
    assert work is None and any("not writable" in ln.text for ln in bad)
    assert any("expects v2312" in ln.text for ln in bad)


@pytest.mark.parametrize(("stderr", "hint"), [
    ("Host key verification failed.\n", "Connect (interactive)"),
    ("sid@login.example.org: Permission denied (publickey,keyboard-interactive).\n", "identity_file"),
])
def test_connection_failures_explain_themselves(stderr, hint):
    lines, work = parse_check(PROFILE, ScriptResult(255, "", stderr))
    assert work is None and lines[0].ok is False and hint in lines[0].text


def test_check_connection_through_a_transport():
    tr, r = _tr(result=ScriptResult(NO_SSH, "", "AETHERCFD: rsync is not installed in the WSL distro\n"))
    lines, work = check_connection(PROFILE, transport=tr)
    assert work is None and "rsync is not installed" in lines[0].text
    remote = base64.b64decode(re.search(r"printf '%s' '?([A-Za-z0-9+/=]+)", r.bodies[-1]).group(1)).decode()
    assert remote == check_script(PROFILE) and "WD=$SCRATCH/aethercfd" in remote
    assert "for c in sbatch squeue sacct scancel rsync" in remote and "module load openfoam/v2306" in remote


def test_work_dir_resolution():
    assert "WD=$SCRATCH/aethercfd" in resolve_work_dir_script(PROFILE)
    assert parse_work_dir("motd\nAETHER_WORK=/scratch/sid/aethercfd/\n") == "/scratch/sid/aethercfd"
    assert parse_work_dir("AETHER_WORK=relative/dir\n") is None
    assert parse_work_dir("AETHER_WORK=/my dir\n") is None
    assert unreachable(ScriptResult(255, "", "")) and not unreachable(ScriptResult(1, "", ""))
