"""Python console: the open case as numpy, matplotlib inline, and the journalled commands.

Enter runs the cell when it is complete (Shift+Enter: new line; Ctrl+Enter: run now); Up/Down walk the
history when the cursor is on the first/last line. Code runs on a worker thread, so the window stays live.
"""

from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QFont, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T


class _Relay(QObject):
    done = Signal(object, object)  # (source, RunResult | Exception)


class _Input(QPlainTextEdit):
    def __init__(self, panel: PythonConsolePanel):
        super().__init__()
        self.panel = panel
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(10)
        self.setFont(f)
        self.setPlaceholderText("Python here: data.fields(), data.residuals(), np, plt, show() …   "
                                "(Enter runs, Shift+Enter new line)")

    def keyPressEvent(self, e: QKeyEvent) -> None:
        enter = e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        mods = e.modifiers()
        if enter and mods & Qt.KeyboardModifier.ControlModifier:
            self.panel.run_input(force=True)
            return
        if enter and not mods & Qt.KeyboardModifier.ShiftModifier and self.panel.run_input(force=False):
            return
        cur = self.textCursor()
        if e.key() == Qt.Key.Key_Up and cur.blockNumber() == 0:
            self.panel.history_step(-1)
            return
        if e.key() == Qt.Key.Key_Down and cur.blockNumber() == self.document().blockCount() - 1:
            self.panel.history_step(+1)
            return
        super().keyPressEvent(e)


class PythonConsolePanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.console = None
        self.history: list[str] = []
        self._hpos = 0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aether-console")
        self._relay = _Relay()
        self._relay.done.connect(self._on_done)
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        self.examples = QComboBox()
        self.examples.addItem("Insert an example…")
        from aethercfd.scripting.console import EXAMPLES

        for k in EXAMPLES:
            self.examples.addItem(k)
        self.examples.activated.connect(self._example)
        clear = QPushButton(icon("msc.clear-all"), "Clear")
        clear.clicked.connect(lambda: self.out.clear())
        restart = QPushButton(icon("msc.debug-restart"), "Restart")
        restart.setToolTip("Fresh namespace for the open case")
        restart.clicked.connect(self.restart)
        self.state = QLabel()
        self.state.setStyleSheet(f"color: {T.text_faint};")
        for w in (self.examples, clear, restart):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.state)
        lay.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.out = QTextBrowser()
        self.out.setOpenExternalLinks(False)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.out.setFont(f)
        self.input = _Input(self)
        split.addWidget(self.out)
        split.addWidget(self.input)
        split.setSizes([400, 120])
        lay.addWidget(split, 1)
        self.sv.case_changed.connect(lambda _s: self.restart())
        self.restart()

    # ------------------------------------------------------------------ namespace
    def restart(self) -> None:
        from aethercfd.scripting.console import Console

        s = self.sv.session
        figs = (Path(s.root) / ".aethercfd" / "local" / "figures") if s is not None and s.project else None
        try:
            self.console = Console(s, figures_dir=figs)
        except Exception as exc:  # e.g. the case cannot be read
            self.console = Console(None)
            self._print(f"(case not loaded into the console: {exc})", T.warn)
        names = "case, data, session, do, np, plt, show" if s is not None else "np, plt, show (open a case for more)"
        self.state.setText(f"Python · {Path(s.root).name if s else 'no case'}")
        self._print(f"Python console · names: {names}", T.text_faint)

    # ------------------------------------------------------------------ running
    def run_input(self, *, force: bool) -> bool:
        """Run the input box; False when it needs more lines (the Enter key then inserts a newline)."""
        src = self.input.toPlainText()
        if not src.strip():
            return True
        return self.run_code(src, force=force)

    def run_code(self, src: str, *, force: bool = True) -> bool:
        if self._busy:
            self.sv.notify("The console is still running the previous cell", "warn")
            return True
        if not force:
            from codeop import compile_command

            try:
                if compile_command(src, "<console>", "single") is None:
                    return False
            except (SyntaxError, OverflowError, ValueError):
                pass
        self.input.clear()
        self.history.append(src)
        self._hpos = len(self.history)
        self._print(">>> " + src.replace("\n", "\n... "), T.accent)
        self._busy = True
        self.state.setText("running…")
        console = self.console
        fut = self._pool.submit(console.run, src, force=True)
        fut.add_done_callback(lambda f: self._relay.done.emit(src, f.exception() or f.result()))
        return True

    @Slot(object, object)
    def _on_done(self, _src, res) -> None:
        self._busy = False
        s = self.sv.session
        self.state.setText(f"Python · {Path(s.root).name if s else 'no case'}")
        if isinstance(res, BaseException):
            self._print(f"{type(res).__name__}: {res}", T.error)
            return
        out = res.output.rstrip("\n")
        if out.strip():
            last = out.splitlines()[-1].split(":")[0]
            err = "Traceback (most recent call last)" in out or last.endswith(("Error", "Exception"))
            self._print(out, T.error if err else T.text)
        for img in res.images:
            self.out.append(f'<img src="{Path(img).as_uri()}" width="520">')
        self.out.moveCursor(QTextCursor.MoveOperation.End)

    def _print(self, text: str, color: str) -> None:
        self.out.append(f'<pre style="color:{color}; margin:0">{html.escape(text)}</pre>')

    # ------------------------------------------------------------------ helpers
    def history_step(self, d: int) -> None:
        if not self.history:
            return
        self._hpos = max(0, min(len(self.history), self._hpos + d))
        self.input.setPlainText(self.history[self._hpos] if self._hpos < len(self.history) else "")
        self.input.moveCursor(QTextCursor.MoveOperation.End)

    def _example(self, i: int) -> None:
        from aethercfd.scripting.console import EXAMPLES

        if i > 0:
            self.insert(EXAMPLES[self.examples.itemText(i)])
        self.examples.setCurrentIndex(0)

    def insert(self, code: str) -> str:
        """Put code in the input box (not run): "Open as Python" and the copilot use this."""
        self.input.setPlainText(code)
        self.input.setFocus()
        self.input.moveCursor(QTextCursor.MoveOperation.End)
        return "code placed in the Python console (Enter runs it)"

    def dispose(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


PANELS = [PanelSpec("aether.python", "Python Console", "mdi6.language-python", "Tools", PythonConsolePanel,
                    default_area=Area.BOTTOM,
                    description="Python on the open case: fields as numpy, plots inline, journalled commands")]
