"""Live template creation (marker wsl): copy real tutorials out of both forks into scratch, read and
edit their parameters through the journalled command path. Scratch only; nothing is run."""

from __future__ import annotations

import shutil
import uuid

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
from tests.wsl._forks import DISTRO, SCRATCH_ROOT, by_family, needs_distro, needs_scratch

pytestmark = [pytest.mark.wsl, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL"),
              needs_distro, needs_scratch]
SCRATCH = SCRATCH_ROOT / "templates"


@pytest.fixture(scope="module")
def forks():
    r = WslRunner(DISTRO)
    return r, by_family(probe_forks(r))


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


def test_every_template_source_and_param_file_exists(forks):
    runner, fk = forks
    checks = []
    for t in TEMPLATES:
        for fam, rel in t.sources.items():
            f = fk[Family.ESI if fam == "esi" else Family.FOUNDATION]
            root = f"{f.project_dir}/tutorials/{rel}"
            checks.append(f'test -d "{root}" || echo "MISSING {t.id} {fam} {root}"')
            for p in t.params:
                file = p.where[fam][0]
                orig = file.replace("0/", "0.orig/", 1) if file.startswith("0/") else f"{file}.orig"
                checks.append(f'test -f "{root}/{file}" || test -f "{root}/{orig}" || '
                              f'echo "NOFILE {t.id} {fam} {file}"')
    r = runner.run_script("\n".join(checks) + "\n", [], timeout_s=120)
    assert r.ok and not r.stdout.strip(), r.stdout


def test_esi_zero_orig_is_restored(forks):
    runner, fk = forks
    dest = SCRATCH / "parcelInBox_esi"
    if dest.exists():
        shutil.rmtree(dest)  # this test's own scratch copy
    copy_tutorial(runner, fk[Family.ESI], template("parcelInBox"), dest)
    assert (dest / "0.orig").is_dir() and (dest / "0" / "U").is_file()
    assert "endTime" in current_values(dest, template("parcelInBox"), "esi")


def test_tutorial_symlinks_become_files_windows_can_read(forks):
    runner, fk = forks
    dest = SCRATCH / "multiRegionHeater_esi"
    if dest.exists():
        shutil.rmtree(dest)  # this test's own scratch copy
    copy_tutorial(runner, fk[Family.ESI], template("multiRegionHeater"), dest)
    # region dictionaries are symlinks in the tutorial (and v2606 links system/decomposeParDict too)
    for f in [dest / "system" / "decomposeParDict", dest / "system" / "topAir" / "decomposeParDict"]:
        assert not f.is_symlink() and "numberOfSubdomains" in f.read_text(encoding="utf-8")
