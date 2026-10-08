"""One editable buffer per file, shared by every view of it (raw editor, forms, diff).

- The disk snapshot is the source of truth; ``dirty`` means the buffer differs from it.
- All changes (typing, form edits, programmatic) go through :meth:`Document.apply`, so a single
  undo/redo history covers them all and works without Qt (scripts, MCP).
- :meth:`check_disk` notices external edits: a clean buffer reloads silently; a dirty one becomes a
  conflict with a three-way merge proposal. Our own saves are recognised by hash and ignored.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from aethercfd.core.textio import TextSnapshot, read_snapshot, sha256_bytes
from aethercfd.documents.merge import MergeResult, merge3
from aethercfd.foamdict.cst import FoamDocument
from aethercfd.foamdict.edits import TextEdit, insert_entry, remove_entry, set_or_insert, set_value
from aethercfd.foamdict.parser import parse

COALESCE_SECONDS = 1.0


def apply_with_inverse(text: str, edits: Sequence[TextEdit]) -> tuple[str, list[TextEdit]]:
    """Apply non-overlapping edits; return (new_text, inverse_edits in new-text coordinates)."""
    es = sorted(edits, key=lambda e: (e.start, e.end))
    parts: list[str] = []
    inverse: list[TextEdit] = []
    pos = 0
    delta = 0
    for e in es:
        if e.start < pos:
            raise ValueError("overlapping edits")
        parts.append(text[pos:e.start])
        parts.append(e.text)
        inverse.append(TextEdit(e.start + delta, e.start + delta + len(e.text), text[e.start:e.end]))
        delta += len(e.text) - (e.end - e.start)
        pos = e.end
    parts.append(text[pos:])
    return "".join(parts), inverse


@dataclass
class EditRecord:
    inverse: list[TextEdit]
    origin: str
    t: float
    # for coalescing typed characters: span of the last insertion/deletion in current coordinates
    caret: int | None = None
    kind: str = "other"  # "insert" | "delete" | "other"


class DiskState(StrEnum):
    UNCHANGED = "unchanged"
    RELOADED = "reloaded"  # buffer was clean: took the new disk content
    CONFLICT = "conflict"  # buffer dirty and disk changed: needs a decision
    DELETED = "deleted"


Listener = Callable[["Document", str, dict], None]


@dataclass
class Document:
    path: Path
    snapshot: TextSnapshot
    text: str
    _undo: list[EditRecord] = field(default_factory=list)
    _redo: list[EditRecord] = field(default_factory=list)
    _listeners: list[Listener] = field(default_factory=list)
    _parsed: tuple[int, FoamDocument] | None = None
    pending_disk: TextSnapshot | None = None  # the newer disk content while in conflict
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @classmethod
    def open(cls, path: str | Path) -> Document:
        snap = read_snapshot(path)
        return cls(Path(path), snap, snap.text)

    # ------------------------------------------------------------------ state
    @property
    def dirty(self) -> bool:
        return self.text != self.snapshot.text

    @property
    def newline(self) -> str:
        return self.snapshot.newline

    @property
    def in_conflict(self) -> bool:
        return self.pending_disk is not None

    def parsed(self) -> FoamDocument:
        h = hash(self.text)
        if self._parsed is None or self._parsed[0] != h:
            self._parsed = (h, parse(self.text))
        return self._parsed[1]

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, kind: str, info: dict) -> None:
        for fn in list(self._listeners):
            fn(self, kind, info)

    # ------------------------------------------------------------------ editing
    def apply(self, edits: Sequence[TextEdit], *, origin: str = "api", coalesce: bool = False) -> None:
        """Apply edits to the buffer (not to disk). ``coalesce`` merges consecutive typing."""
        if not edits:
            return
        with self._lock:
            new, inverse = apply_with_inverse(self.text, edits)
            now = time.monotonic()
            kind, caret = "other", None
            if len(edits) == 1:
                e = edits[0]
                if e.start == e.end and e.text and "\n" not in e.text:
                    kind, caret = "insert", e.start + len(e.text)
                elif not e.text and e.end - e.start == 1:
                    kind, caret = "delete", e.start
            last = self._undo[-1] if self._undo else None
            if (coalesce and last is not None and last.origin == origin and now - last.t < COALESCE_SECONDS
                    and kind == last.kind and kind != "other" and len(last.inverse) == 1):
                li = last.inverse[0]
                e = edits[0]
                if kind == "insert" and last.caret == e.start:  # keep typing at the caret
                    last.inverse = [TextEdit(li.start, li.end + len(e.text), li.text)]
                    last.caret, last.t = caret, now
                    self._commit(new, origin, edits)
                    return
                if kind == "delete" and e.end == li.start and li.start == li.end:  # backspacing
                    last.inverse = [TextEdit(e.start, e.start, self.text[e.start:e.end] + li.text)]
                    last.caret, last.t = caret, now
                    self._commit(new, origin, edits)
                    return
            self._undo.append(EditRecord(inverse, origin, now, caret, kind))
            self._redo.clear()
            self._commit(new, origin, edits)

    def _commit(self, new: str, origin: str, edits: Sequence[TextEdit]) -> None:
        was_dirty = self.dirty
        self.text = new
        self._emit("edited", {"origin": origin, "edits": list(edits)})
        if was_dirty != self.dirty:
            self._emit("dirty", {"dirty": self.dirty})

    def set_text(self, text: str, *, origin: str = "api") -> None:
        if text != self.text:
            self.apply([TextEdit(0, len(self.text), text)], origin=origin)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        with self._lock:
            if not self._undo:
                return False
            rec = self._undo.pop()
            new, inv = apply_with_inverse(self.text, rec.inverse)
            self._redo.append(EditRecord(inv, rec.origin, time.monotonic()))
            self._commit(new, "undo", rec.inverse)
            return True

    def redo(self) -> bool:
        with self._lock:
            if not self._redo:
                return False
            rec = self._redo.pop()
            new, inv = apply_with_inverse(self.text, rec.inverse)
            self._undo.append(EditRecord(inv, rec.origin, time.monotonic()))
            self._commit(new, "redo", rec.inverse)
            return True

    # ------------------------------------------------------------------ structured (form) edits
    def form_set(self, key: str, value: str, *, insert_if_missing: bool = False) -> None:
        doc = self.parsed()
        edits = (set_or_insert(doc, key, value, newline=self.newline) if insert_if_missing
                 else set_value(doc, key, value))
        self.apply(edits, origin="form")

    def form_insert(self, dict_path: str, key: str, value: str) -> None:
        self.apply(insert_entry(self.parsed(), dict_path, key, value, newline=self.newline), origin="form")

    def form_remove(self, key: str) -> None:
        self.apply(remove_entry(self.parsed(), key), origin="form")

    # ------------------------------------------------------------------ disk
    def check_disk(self) -> DiskState:
        """Compare with the file on disk and react (see module docstring)."""
        with self._lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                self._emit("deleted", {})
                return DiskState.DELETED
            if st.st_mtime_ns == self.snapshot.mtime_ns and st.st_size == self.snapshot.size:
                return DiskState.UNCHANGED
            new = read_snapshot(self.path)
            if new.sha256 == self.snapshot.sha256:  # touched or our own write echoing back
                self.snapshot = new
                return DiskState.UNCHANGED
            if self.pending_disk is not None and new.sha256 == self.pending_disk.sha256:
                return DiskState.CONFLICT
            if not self.dirty:
                old = self.text
                self.snapshot = new
                self._undo.clear()
                self._redo.clear()
                self.text = new.text
                self._emit("reloaded", {"previous": old})
                return DiskState.RELOADED
            self.pending_disk = new
            self._emit("conflict", {"merge": self.merge_proposal()})
            return DiskState.CONFLICT

    def merge_proposal(self) -> MergeResult | None:
        if self.pending_disk is None:
            return None
        return merge3(self.snapshot.text, self.text, self.pending_disk.text)

    def resolve_conflict(self, choice: str, merged_text: str | None = None) -> None:
        """'mine' keeps the buffer (it will overwrite the disk change on save), 'theirs' takes the disk
        version, 'merged' takes ``merged_text`` (default: the automatic merge)."""
        with self._lock:
            disk = self.pending_disk
            if disk is None:
                return
            self.pending_disk = None
            if choice == "theirs":
                self.snapshot = disk
                self.set_text(disk.text, origin="conflict")
            elif choice == "merged":
                text = merged_text if merged_text is not None else merge3(self.snapshot.text, self.text, disk.text).text
                self.snapshot = disk
                self.set_text(text, origin="conflict")
            else:  # mine
                self.snapshot = disk
            self._emit("dirty", {"dirty": self.dirty})

    def mark_saved(self) -> None:
        """Call after the buffer was written (via the file.save command)."""
        with self._lock:
            self.snapshot = read_snapshot(self.path)
            self.pending_disk = None
            self._emit("saved", {"sha": self.snapshot.sha256})
            self._emit("dirty", {"dirty": self.dirty})

    def encoded(self) -> bytes:
        return self.snapshot.encode(self.text)

    def disk_sha(self) -> str:
        return sha256_bytes(self.path.read_bytes())
