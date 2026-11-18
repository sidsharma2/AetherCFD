"""Settings shared by the live tests, and one fork per family when a distro has several versions installed.

AETHERCFD_TEST_DISTRO names the WSL distro with OpenFOAM; AETHERCFD_TEST_SCRATCH is a Windows folder the
tests may create cases in (never your real cases). A test that needs one of them skips when it is unset.

AETHERCFD_LIVE_ESI / AETHERCFD_LIVE_FOUNDATION pick a version (e.g. ``v2606``); the default is the oldest
installed, the one these tests were written against (ESI v2306, Foundation 14)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.case.fork_detect import version_key

DISTRO = os.environ.get("AETHERCFD_TEST_DISTRO", "")
_SCRATCH = os.environ.get("AETHERCFD_TEST_SCRATCH", "")
# a placeholder when unset: every test that writes there carries needs_scratch and is skipped
SCRATCH_ROOT = Path(_SCRATCH) if _SCRATCH else Path("AETHERCFD_TEST_SCRATCH-not-set")
_CASES = os.environ.get("AETHERCFD_TEST_CASES", "")
CASES = Path(_CASES) if _CASES else None
needs_distro = pytest.mark.skipif(not DISTRO, reason="set AETHERCFD_TEST_DISTRO to the WSL distro with OpenFOAM")
needs_scratch = pytest.mark.skipif(not _SCRATCH, reason="set AETHERCFD_TEST_SCRATCH to a Windows folder the "
                                                        "live tests may write cases into")


def by_family(forks: list[ForkProfile]) -> dict[Family, ForkProfile]:
    out: dict[Family, ForkProfile] = {}
    for fam in Family:
        same = sorted((f for f in forks if f.family is fam), key=lambda f: version_key(f.version))
        if not same:
            continue
        want = os.environ.get(f"AETHERCFD_LIVE_{fam.name}")
        if not want:
            out[fam] = same[0]
            continue
        hit = next((f for f in same if f.version == want), None)
        if hit is None:
            raise LookupError(f"AETHERCFD_LIVE_{fam.name}={want} is not installed: {[f.version for f in same]}")
        out[fam] = hit
    return out


def expected_version(fam: Family) -> str:
    return os.environ.get(f"AETHERCFD_LIVE_{fam.name}") or {Family.ESI: "v2306", Family.FOUNDATION: "14"}[fam]
