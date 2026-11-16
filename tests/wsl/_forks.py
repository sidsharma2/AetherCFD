"""One fork per family for the live tests, when a distro has several versions of a family installed.

AETHERCFD_LIVE_ESI / AETHERCFD_LIVE_FOUNDATION pick a version (e.g. ``v2606``); the default is the oldest
installed, the one these tests were written against (ESI v2306, Foundation 14)."""

from __future__ import annotations

import os

from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.case.fork_detect import version_key


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
