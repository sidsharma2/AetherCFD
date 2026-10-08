"""Live template creation (marker wsl): copy real tutorials out of both forks into scratch, read and
edit their parameters through the journalled command path. Scratch only; nothing is run."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.commands.base import USER
from aethercfd.scripting.api import Session
from aethercfd.templates.catalog import TEMPLATES, template
from aethercfd.templates.create import (
    available,
    copy_tutorial,
    current_values,
    family_of,
    new_value_text,
)

pytestmark = [pytest.mark.wsl, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL")]
SCRATCH = Path(r"D:\OpenFOAM\_aethercfd_scratch\tests\templates")


@pytest.fixture(scope="module")
def forks():
    r = WslRunner("LMAE-OpenFOAM")
    return r, {f.family: f for f in probe_forks(r)}


@pytest.mark.parametrize("family", [Family.ESI, Family.FOUNDATION])
def test_create_pitzdaily_and_edit_inlet(forks, family):
    runner, fk = forks
    fork = fk[family]
    have = available(runner, fork, TEMPLATES)
    assert have["pitzDaily"] and have["forwardStep"]
    t = template("pitzDaily")
    dest = SCRATCH / f"pitz_{family.value}_{uuid.uuid4().hex[:6]}"
    files = copy_tutorial(runner, fork, t, dest)
    assert "system" in files and (dest / "system" / "controlDict").exists()
    with pytest.raises(RuntimeError, match="already exists"):
        copy_tutorial(runner, fork, t, dest)
    vals = current_values(dest, t, family_of(fork))
    assert vals["Uin"][2].startswith("uniform (10") and "nu" in vals
    s = Session(dest, create_project=True, with_backend=False)
    rel, key, cur = vals["Uin"]
    res = s.executor.execute("dict.set", {"file": str(dest / rel), "key": key,
                                          "value": new_value_text("vector_x", cur, 12.5)}, USER)
    assert res.ok, res.message
    assert "uniform (12.5 0 0)" in (dest / "0" / "U").read_text()
    rel, key, cur = vals["nu"]
    res = s.executor.execute("dict.set", {"file": str(dest / rel), "key": key,
                                          "value": new_value_text("scalar", cur, 2e-05)}, USER)
    assert res.ok, res.message
    assert "2e-05" in (dest / rel).read_text()


def test_unsafe_destination_is_refused(forks):
    runner, fk = forks
    with pytest.raises(ValueError, match="space"):
        copy_tutorial(runner, fk[Family.ESI], template("pitzDaily"), SCRATCH / "with space" / "x")
