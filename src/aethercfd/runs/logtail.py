"""Tail a growing log file by byte offset (never reads the whole file again).

Handles partial last lines, truncation/replacement (offset reset), and CRLF. Safe on huge logs:
each call reads at most ``max_bytes`` (the rest is picked up by later calls), and ``start_at_end``
lets a monitor attach to a 778 GB runaway log without reading it.
"""

from __future__ import annotations

import os
from pathlib import Path


class LogTail:
    def __init__(self, path: str | Path, *, start_at_end: bool = False, max_bytes: int = 4 * 2**20):
        self.path = Path(path)
        self.offset = 0
        self.max_bytes = max_bytes
        self._partial = b""
        self.resets = 0
        if start_at_end and self.path.exists():
            self.offset = self.path.stat().st_size

    def size(self) -> int:
        try:
            return os.stat(self.path).st_size
        except FileNotFoundError:
            return 0

    def read_new(self) -> list[str]:
        """Complete new lines since the last call (a trailing partial line is held back)."""
        size = self.size()
        if size < self.offset:  # truncated or replaced (e.g. a restart overwrote the log)
            self.offset = 0
            self._partial = b""
            self.resets += 1
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(min(self.max_bytes, size - self.offset))
        self.offset += len(data)
        data = self._partial + data
        cut = data.rfind(b"\n")
        if cut < 0:
            self._partial = data
            return []
        self._partial = data[cut + 1:]
        return data[:cut].decode("utf-8", errors="replace").replace("\r", "").split("\n")

    @property
    def behind(self) -> int:
        """Bytes not yet read (non-zero when a log grows faster than we drain it)."""
        return max(0, self.size() - self.offset)
