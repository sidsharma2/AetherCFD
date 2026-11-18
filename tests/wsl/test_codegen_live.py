"""Live: generate k-epsilon-Pope and compile it with wmake in WSL through the code.build command (wsl_slow;
about 2 minutes). Scratch only."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.commands.base import USER
from aethercfd.scripting.api import Session
from tests.wsl._forks import DISTRO, SCRATCH_ROOT, by_family, needs_distro, needs_scratch

pytestmark = [pytest.mark.wsl_slow, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL"),
              needs_distro, needs_scratch]
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"
SCRATCH = SCRATCH_ROOT / "codegen"


@pytest.mark.timeout(900)
def test_pope_builds_with_wmake():
    case = SCRATCH / "cavity"
    if case.exists():
        shutil.rmtree(case)  # this test's own scratch copy
    shutil.copytree(FIX, case)
    runner = WslRunner(DISTRO)
    s = Session(case, create_project=True, with_backend=False)
    s.context.backend = runner
    s.context.forks = [by_family(probe_forks(runner))[Family.ESI]]
    dest = case / "aether-code" / "kEpsilonPopeT"
    assert s.executor.execute("code.create", {"template": "kEpsilonPope", "name": "kEpsilonPopeT",
                                              "dest": str(dest)}, USER).ok
    res = s.executor.execute("code.build", {"dir": str(dest)}, USER)
    assert res.ok, (res.message, (res.data or {}).get("output", "")[-3000:])
    assert res.data["library"].endswith("libaetherkEpsilonPopeT.so")
