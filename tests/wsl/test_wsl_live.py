"""Live checks against the LMAE-OpenFOAM distro (marker: wsl). They boot the distro if it is stopped,
run only read-only commands, and never shut anything down."""

from __future__ import annotations

import shutil

import pytest

from aethercfd.backend.wsl.forks import EXIT_ALREADY_SOURCED, Family, prelude, probe_forks
from aethercfd.backend.wsl.runner import WslRunner

pytestmark = [pytest.mark.wsl, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL")]
DISTRO = "LMAE-OpenFOAM"


@pytest.fixture(scope="module")
def runner():
    return WslRunner(DISTRO)


@pytest.fixture(scope="module")
def forks(runner):
    return {f.family: f for f in probe_forks(runner)}


def test_both_forks_are_found(forks):
    assert forks[Family.ESI].version == "v2306"
    assert forks[Family.FOUNDATION].version == "14"


def test_each_prelude_loads_its_fork(runner, forks):
    for fork in forks.values():
        res = runner.run_script(prelude(fork) + 'echo "$WM_PROJECT_VERSION|$WM_PROJECT_DIR"\n')
        assert res.ok, res.stderr
        assert res.stdout.strip() == f"{fork.version}|{fork.project_dir}"


def test_mixed_forks_are_refused(runner, forks):
    res = runner.run_script(prelude(forks[Family.ESI]) + prelude(forks[Family.FOUNDATION]) + "echo NO\n")
    assert res.returncode == EXIT_ALREADY_SOURCED and "NO" not in res.stdout


def test_argv_with_spaces_and_quotes_survives(runner):
    args = ["/mnt/d/OpenFOAM/RPL Apollo Lunar Module Ascent Engine", 'say "hi"', "it's", ""]
    res = runner.run_script('for a in "$@"; do printf "[%s]\\n" "$a"; done\n', args)
    assert res.stdout.splitlines() == [f"[{a}]" for a in args]


def test_stdin_is_detached(runner):
    res = runner.run_script("read -t 1 x && echo LEAK || echo detached\n")
    assert res.stdout.strip() == "detached"


def test_linux_side_timeout(runner):
    res = runner.run_script("sleep 30\n", timeout_s=2)
    assert res.timed_out
