"""Follow one solver log file for the GUI: parse the existing content in the background, then tail
new lines every second. Works the same for a live run and for a finished case's old log."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from aethercfd.runs.logparse import LogParser
from aethercfd.runs.logtail import LogTail

INITIAL_TAIL_LINES = 20000  # the log view only shows the tail; the plot gets every step


class LogFollower(QObject):
    reset = Signal(str)  # path
    steps = Signal(list)  # [StepRecord]
    lines = Signal(list)  # [str]
    header = Signal(dict)
    events = Signal(list)  # [LogEvent] (fatal/warning/end)

    def __init__(self, services, parent: QObject | None = None):
        super().__init__(parent)
        self.services = services
        self.path: Path | None = None
        self.parser: LogParser | None = None
        self.tail: LogTail | None = None
        self.all_steps: list = []
        self.tail_lines: list[str] = []  # recent lines for panels that open later
        self._gen = 0
        self._timer = QTimer(self, interval=1000, timeout=self._poll)

    def follow(self, path: str | Path | None) -> None:
        self._timer.stop()
        self._gen += 1
        gen = self._gen
        self.path = Path(path) if path else None
        self.all_steps = []
        self.tail_lines = []
        self.reset.emit(str(self.path) if self.path else "")
        if self.path is None:
            return
        p = self.path

        def initial():
            tail = LogTail(p, max_bytes=1 << 62)
            text_lines = tail.read_new()
            parser = LogParser()
            steps = parser.feed(text_lines)
            return parser, tail, steps, text_lines[-INITIAL_TAIL_LINES:]

        def done(res):
            value, err = res
            if gen != self._gen:
                return  # a newer follow() superseded this one
            if err is not None:
                self.services.notify(f"Could not read {p.name}: {err}", "warn")
                return
            self.parser, self.tail, steps, tail_lines = value
            self.all_steps = list(steps)
            self.tail_lines = list(tail_lines)
            self.header.emit(dict(self.parser.header))
            if steps:
                self.steps.emit(steps)
            if tail_lines:
                self.lines.emit(tail_lines)
            if self.parser.events:
                self.events.emit(list(self.parser.events))
            self._timer.start()

        self.services.run_bg(initial, done)

    def _poll(self) -> None:
        if self.tail is None or self.parser is None:
            return
        new = self.tail.read_new()
        if not new:
            return
        n_events = len(self.parser.events)
        steps = self.parser.feed(new)
        self.tail_lines.extend(new)
        if len(self.tail_lines) > 2 * INITIAL_TAIL_LINES:
            del self.tail_lines[:-INITIAL_TAIL_LINES]
        self.lines.emit(new)
        if steps:
            self.all_steps.extend(steps)
            self.steps.emit(steps)
        if len(self.parser.events) > n_events:
            self.events.emit(self.parser.events[n_events:])

    def stop(self) -> None:
        self._timer.stop()
