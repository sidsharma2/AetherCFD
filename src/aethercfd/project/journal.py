"""Append-only command journal + content-addressed object store.

Every applied command is one JSON line in ``journal/YYYY-MM.jsonl``. File contents before and after
each change are stored once in ``objects/<sha[:2]>/<sha>``, so any journalled change can be inspected
or reverted later, independent of git.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from aethercfd.core.textio import atomic_write_bytes, sha256_bytes

OBJECT_MAX_BYTES = 5 * 2**20  # larger files are journalled by hash only


class ObjectStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        if len(data) <= OBJECT_MAX_BYTES:
            p = self._path(sha)
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(p, data)
        return sha

    def get(self, sha: str) -> bytes | None:
        p = self._path(sha)
        return p.read_bytes() if p.exists() else None

    def has(self, sha: str) -> bool:
        return self._path(sha).exists()


def new_id() -> str:
    """Time-ordered unique id (sortable): 13-digit ms timestamp + random suffix."""
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


class Journal:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, entry: dict) -> dict:
        entry = {"id": new_id(), "ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **entry}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        f = self.root / f"{datetime.now(UTC):%Y-%m}.jsonl"
        with open(f, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def entries(self, *, newest_first: bool = False) -> Iterator[dict]:
        files = sorted(self.root.glob("*.jsonl"), reverse=newest_first)
        for f in files:
            lines = f.read_text(encoding="utf-8").splitlines()
            if newest_first:
                lines.reverse()
            for ln in lines:
                if ln.strip():
                    try:
                        yield json.loads(ln)
                    except json.JSONDecodeError:
                        continue  # a torn last line after a crash must not break the history

    def find(self, entry_id: str) -> dict | None:
        return next((e for e in self.entries() if e.get("id") == entry_id), None)
