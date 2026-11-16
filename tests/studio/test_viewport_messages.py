"""The viewport tells a damaged field file (a run stopped mid-write) from a field that is not written."""

from __future__ import annotations

import pytest

pytest.importorskip("pyvistaqt")

from aethercfd.studio.panels.viewport.panel import field_file_present


def test_field_file_present_tells_damaged_from_absent(tmp_path):
    (tmp_path / "0.5").mkdir()
    (tmp_path / "0.5" / "p").write_text("FoamFile { class volScal")  # half-written
    (tmp_path / "0.6").mkdir()  # empty time folder
    (tmp_path / "processor0" / "1").mkdir(parents=True)
    (tmp_path / "processor0" / "1" / "U.gz").write_bytes(b"")
    (tmp_path / "2").mkdir()
    (tmp_path / "2" / "heater").mkdir()
    (tmp_path / "2" / "heater" / "T").write_text("x")
    assert field_file_present(tmp_path, "p", 0.5)
    assert not field_file_present(tmp_path, "p", 0.6)
    assert not field_file_present(tmp_path, "U", 0.5)
    assert field_file_present(tmp_path, "U", 1.0)
    assert field_file_present(tmp_path, "T", 2.0)  # a region of a multi-region case
    assert not field_file_present(None, "p", 0.5) and not field_file_present(tmp_path, "p", None)
    assert not field_file_present(tmp_path / "gone", "p", 0.5)
