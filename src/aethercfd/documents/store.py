"""Open documents, one per file path, plus disk polling."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from aethercfd.documents.document import DiskState, Document


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class DocumentStore:
    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._lock = threading.Lock()

    def open(self, path: str | Path) -> Document:
        k = _key(path)
        with self._lock:
            doc = self._docs.get(k)
            if doc is None:
                doc = Document.open(path)
                self._docs[k] = doc
            return doc

    def get(self, path: str | Path) -> Document | None:
        return self._docs.get(_key(path))

    def close(self, path: str | Path) -> None:
        with self._lock:
            self._docs.pop(_key(path), None)

    def documents(self) -> list[Document]:
        return list(self._docs.values())

    def dirty(self) -> list[Document]:
        return [d for d in self._docs.values() if d.dirty]

    def check(self, paths: list[str | Path] | None = None) -> dict[str, DiskState]:
        """Check the given (or all) open documents against disk."""
        docs = [self.get(p) for p in paths] if paths is not None else self.documents()
        out: dict[str, DiskState] = {}
        for d in docs:
            if d is not None:
                out[str(d.path)] = d.check_disk()
        return out
