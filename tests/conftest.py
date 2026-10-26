"""Shared pytest setup."""

from __future__ import annotations

from pathlib import Path


def pytest_configure(config):
    # addopts sets --basetemp=build/pytest-tmp; pytest creates only the last folder, and build/ is not in git,
    # so a fresh clone (and CI) would fail every test that uses tmp_path
    basetemp = config.option.basetemp
    if basetemp:
        Path(config.rootpath, basetemp).parent.mkdir(parents=True, exist_ok=True)
