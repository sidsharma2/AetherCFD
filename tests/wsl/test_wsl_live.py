"""Live checks against the distro named by $AETHERCFD_TEST_DISTRO (marker: wsl). They boot the distro if it is stopped,
run only read-only commands, and never shut anything down."""

from __future__ import annotations

import shutil

import pytest

from aethercfd.backend.wsl.forks import EXIT_ALREADY_SOURCED, Family, prelude, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from tests.wsl._forks import DISTRO, by_family, expected_version, needs_distro

pytestmark = [pytest.mark.wsl, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL"), needs_distro]


@pytest.fixture(scope="module")
def runner():
    return WslRunner(DISTRO)


@pytest.fixture(scope="module")
def forks(runner):
    return by_family(probe_forks(runner))


def test_both_forks_are_found(forks):
    assert forks[Family.ESI].version == expected_version(Family.ESI)
    assert forks[Family.FOUNDATION].version == expected_version(Family.FOUNDATION)


def test_each_prelude_loads_its_fork(runner, forks):
    for fork in forks.values():
        res = runner.run_script(prelude(fork) + 'echo "$WM_PROJECT_VERSION|$WM_PROJECT_DIR"\n')
        assert res.ok, res.stderr
        assert res.stdout.strip() == f"{fork.version}|{fork.project_dir}"


def test_mixed_forks_are_refused(runner, forks):
    res = runner.run_script(prelude(forks[Family.ESI]) + prelude(forks[Family.FOUNDATION]) + "echo NO\n")
    assert res.returncode == EXIT_ALREADY_SOURCED and "NO" not in res.stdout


def test_argv_with_spaces_and_quotes_survives(runner):
    args = ["/mnt/c/cases/a case with spaces", 'say "hi"', "it's", ""]
    res = runner.run_script('for a in "$@"; do printf "[%s]\\n" "$a"; done\n', args)
    assert res.stdout.splitlines() == [f"[{a}]" for a in args]


def test_stdin_is_detached(runner):
    res = runner.run_script("read -t 1 x && echo LEAK || echo detached\n")
    assert res.stdout.strip() == "detached"


def test_linux_side_timeout(runner):
    res = runner.run_script("sleep 30\n", timeout_s=2)
    assert res.timed_out
