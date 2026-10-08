"""Debounced file-system watching for case folders.

Local NTFS paths use watchdog's native observer (ReadDirectoryChangesW); ``\\\\wsl.localhost``
paths cannot deliver change notifications reliably, so they are polled.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from aethercfd.core.paths import is_wsl_unc

# noise we never report (sidecar churn, editor temp files, our own atomic-write temp files)
_IGNORE_PARTS = {".aethercfd", ".git", "__pycache__"}
_IGNORE_SUFFIXES = (".aether-tmp", ".swp", "~", ".tmp")


class _Handler(FileSystemEventHandler):
    def __init__(self, sink: CaseWatcher):
        self.sink = sink

    def on_any_event(self, event: FileSystemEvent) -> None:
        for p in (event.src_path, getattr(event, "dest_path", None)):
            if p:
                self.sink._push(Path(str(p)))


class CaseWatcher:
    """Calls ``callback(set_of_paths)`` at most every ``debounce`` seconds after changes stop."""

    def __init__(self, root: str | Path, callback: Callable[[set[Path]], None], *, debounce: float = 0.3,
                 polling: bool | None = None, poll_interval: float = 2.0):
        self.root = Path(root)
        self.callback = callback
        self.debounce = debounce
        use_polling = is_wsl_unc(str(self.root)) if polling is None else polling
        self.polling = use_polling
        self._observer = PollingObserver(timeout=poll_interval) if use_polling else Observer()
        self._pending: set[Path] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def start(self) -> CaseWatcher:
        self._observer.schedule(_Handler(self), str(self.root), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        return self

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)
        with self._lock:
            if self._timer:
                self._timer.cancel()

    def _push(self, p: Path) -> None:
        if any(part in _IGNORE_PARTS for part in p.parts) or p.name.endswith(_IGNORE_SUFFIXES) \
                or (p.name.startswith(".") and p.name.endswith(".aether-tmp")):
            return
        with self._lock:
            self._pending.add(p)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, set()
            self._timer = None
        if batch:
            self.callback(batch)
