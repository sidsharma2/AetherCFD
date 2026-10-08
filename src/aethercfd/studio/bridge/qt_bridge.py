"""Core EventBus -> Qt signals on the GUI thread.

Core code publishes from any thread (run monitors, watchers, workers). This bridge queues events and
delivers them on the GUI thread in batches (log lines at most ~20x/s), so a chatty solver can never
flood the event loop.
"""

from __future__ import annotations

import threading
from collections import defaultdict

from PySide6.QtCore import QObject, QTimer, Signal

from aethercfd.core.events import EventBus


class CoreEventBridge(QObject):
    event = Signal(str, dict)  # (topic, payload) for every non-batched event
    log_batch = Signal(str, str, list)  # (run_id, log name, lines)
    progress_batch = Signal(str, list)  # (run_id, [StepRecord, ...])

    def __init__(self, bus: EventBus, parent: QObject | None = None, *, flush_ms: int = 50):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict]] = []
        self._logs: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._progress: dict[str, list] = defaultdict(list)
        self._unsub = bus.subscribe("*", self._on_core_event)
        self._timer = QTimer(self)
        self._timer.setInterval(flush_ms)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def _on_core_event(self, topic: str, payload: dict) -> None:  # any thread
        with self._lock:
            if topic == "run.log":
                self._logs[(payload["run_id"], payload.get("log") or "")].extend(payload["lines"])
            elif topic == "run.progress":
                self._progress[payload["run_id"]].append(payload["step"])
            else:
                self._events.append((topic, payload))

    def _flush(self) -> None:  # GUI thread
        with self._lock:
            events, self._events = self._events, []
            logs, self._logs = self._logs, defaultdict(list)
            progress, self._progress = self._progress, defaultdict(list)
        for (run_id, log), lines in logs.items():
            self.log_batch.emit(run_id, log, lines)
        for run_id, steps in progress.items():
            self.progress_batch.emit(run_id, steps)
        for topic, payload in events:
            self.event.emit(topic, payload)

    def close(self) -> None:
        self._timer.stop()
        self._unsub()
