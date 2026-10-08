"""CopilotSession.start on a fresh install: its cache folder may not exist yet (the first-session bug)."""

from __future__ import annotations

import sys
from pathlib import Path

from aethercfd.copilot.claude_cli import ClaudeCli
from aethercfd.copilot.session import CopilotSession


class _Bridge:
    endpoint, token, port = "127.0.0.1:1", "t", 1


def test_start_creates_its_cache_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CACHE_DIR", str(tmp_path / "cache"))
    # a stand-in "claude" that exits at once: start() must get as far as spawning it
    cli = ClaudeCli(Path(sys.executable), "0", "test")
    s = CopilotSession(cli, tmp_path, _Bridge(), on_event=lambda e: None, on_exit=lambda c: None)
    s.start()
    s.proc.wait(timeout=30)
    assert (tmp_path / "cache" / "copilot" / "claude-stderr.log").is_file()
