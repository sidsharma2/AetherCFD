"""Regression test for a real bug: an existing case artifact
(vera_pre_solve_check.json in the real converged round-jet case) contains
a raw cp1252 em-dash byte that isn't valid UTF-8 — found by actually
running `cfd mesh check` against it, not by inspection.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from aethercfd._vendor.text_io import read_text_robust


class TestReadTextRobust(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="cfd_agents_test_textio_"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_reads_valid_utf8_normally(self) -> None:
        path = self.tmp_dir / "utf8.txt"
        path.write_text("hello — world", encoding="utf-8")
        self.assertEqual(read_text_robust(path), "hello — world")

    def test_falls_back_to_cp1252_for_the_exact_bug_found(self) -> None:
        # Reproduces the exact byte sequence found in the real
        # vera_pre_solve_check.json: raw byte 0x97 (cp1252's em-dash)
        # inside otherwise-ASCII text, which is not valid UTF-8. Written
        # as raw bytes directly (not via str.encode("cp1252")) since 0x97
        # is U+2014 EM DASH in cp1252, not the Python string "\x97"
        # (U+0097, a C1 control character with no cp1252 mapping at all).
        path = self.tmp_dir / "cp1252.txt"
        path.write_bytes(b"threshold \x97 acceptable")
        text = read_text_robust(path)
        self.assertIn("threshold", text)
        self.assertIn("acceptable", text)

    def test_json_with_cp1252_em_dash_parses(self) -> None:
        import json

        path = self.tmp_dir / "bad_encoding.json"
        payload = b'{"mesh_notes": ["close to the 70\x97degree threshold"]}'
        path.write_bytes(payload)
        data = json.loads(read_text_robust(path))
        self.assertIn("mesh_notes", data)


if __name__ == "__main__":
    unittest.main()
