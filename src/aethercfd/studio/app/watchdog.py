"""Hang forensics: a GUI-thread heartbeat and a watcher thread that dumps every thread's stack when it stops.

A QTimer on the GUI thread stamps the time every ``interval_ms``. A daemon thread checks the stamp; when the GUI
thread has not run its event loop for ``threshold`` seconds it writes ``hang-<time>.txt`` (all thread stacks via
faulthandler, the stuck time and a context label such as the running sweep step) into the crash folder, once per
hang. A blocked viewport, a deadlock or a long synchronous call on the GUI thread then leaves a stack trace
instead of a frozen window and nothing else.

Modal dialogs run a nested event loop, so the heartbeat keeps beating during ``exec()``; ``paused()`` exists for
the rare legitimate blocking call.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QTimer

log = logging.getLogger("aethercfd.studio.watchdog")

DEFAULT_THRESHOLD_S = 10.0


class HangWatchdog:
    def __init__(self, out_dir: Path, *, threshold: float = DEFAULT_THRESHOLD_S, interval_ms: int = 500,
                 keep: int = 20):
        self.out_dir = Path(out_dir)
        self.threshold = float(threshold)
        self.interval_ms = int(interval_ms)
        self.keep = int(keep)
        self.context = ""  # what the app is doing (e.g. the sweep step); written into each dump
        self.dumps: list[Path] = []
        self._stamp = time.monotonic()
        self._paused = 0
        self._dumped = False  # one dump per hang: re-armed when the GUI thread beats again
        self._stop = threading.Event()
        self._timer: QTimer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, out_dir: Path) -> HangWatchdog | None:
        """The app's watchdog: AETHER_HANG_SECONDS sets the threshold, 0 turns it off."""
        try:
            secs = float(os.environ.get("AETHER_HANG_SECONDS", DEFAULT_THRESHOLD_S))
        except ValueError:
            secs = DEFAULT_THRESHOLD_S
        return cls(out_dir, threshold=secs) if secs > 0 else None

    # ------------------------------------------------------------------ lifecycle (GUI thread)
    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._stamp = time.monotonic()
        self._timer = QTimer()
        self._timer.setInterval(self.interval_ms)
        self._timer.timeout.connect(self._beat)
        self._timer.start()
        self._thread = threading.Thread(target=self._watch, name="aether-hang-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @contextlib.contextmanager
    def paused(self):
        """No dump while a legitimate blocking call runs on the GUI thread."""
        self._paused += 1
        try:
            yield
        finally:
            self._paused -= 1
            self._stamp = time.monotonic()

    def _beat(self) -> None:
        self._stamp = time.monotonic()

    # ------------------------------------------------------------------ watcher thread
    def _watch(self) -> None:
        poll = min(1.0, max(0.05, self.threshold / 4))
        while not self._stop.wait(poll):
            stuck = time.monotonic() - self._stamp
            if self._paused or stuck < self.threshold:
                self._dumped = False
                continue
            if not self._dumped:
                self._dumped = True
                try:
                    self._dump(stuck)
                except Exception:  # forensics must never take the app down
                    log.exception("hang dump failed")

    def _dump(self, stuck: float) -> Path:
        path = self.out_dir / f"hang-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.txt"
        gui = threading.main_thread().ident or 0
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"AetherCFD hang: the GUI thread has not run its event loop for {stuck:.1f} s\n"
                    f"context: {self.context or '(none)'}\n"
                    f"GUI (main) thread id: 0x{gui:08x}  (its stack shows what blocks it)\n"
                    f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}  pid: {os.getpid()}\n\n")
            f.flush()
            faulthandler.dump_traceback(f, all_threads=True)
        self.dumps.append(path)
        log.warning("GUI thread stuck for %.0f s (%s): stacks in %s", stuck, self.context or "no context", path)
        self._prune()
        return path

    def _prune(self) -> None:
        old = sorted(self.out_dir.glob("hang-*.txt"), key=lambda p: p.stat().st_mtime)[:-self.keep]
        for p in old:  # our own diagnostic files, newest kept
            with contextlib.suppress(OSError):
                p.unlink()
