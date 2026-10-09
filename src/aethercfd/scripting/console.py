"""Python console engine (no Qt): an interpreter whose namespace is the open case.

Names available in the console:
    case      the Case (files, regions, times, fork)          data   CaseData: fields/centres/residuals as numpy
    session   the AetherCFD Session                           do     do("dict.set", file=..., key=..., value=...)
    np, plt   numpy and matplotlib (figures appear inline with show())          Path

``do`` runs the same journalled commands as the GUI (actor "script"): edits land in the Command Journal and
can be reverted there.
"""

from __future__ import annotations

import code
import contextlib
import io
import itertools
import os
from dataclasses import dataclass, field
from pathlib import Path

EXAMPLES = {
    "fields": ("f = data.fields()            # latest time, cell values (OpenFOAM cell order)\n"
               "print(sorted(f), data.times()[-3:])\n"
               "U = f['U']; p = f['p']\n"
               "print('|U| max', np.linalg.norm(U, axis=1).max(), ' p range', p.min(), p.max())"),
    "residuals": ("r = data.residuals()\n"
                  "for k in ('p', 'Ux', 'h'):\n"
                  "    if k in r: plt.semilogy(r['time'], r[k], label=k)\n"
                  "plt.xlabel('iteration'); plt.ylabel('initial residual'); plt.legend(); show()"),
    "profile": ("c = data.centres(); f = data.fields(names=['U'])\n"
                "on_axis = np.hypot(c[:, 1] - c[:, 1].mean(), c[:, 2] - c[:, 2].mean()) < 2e-4\n"
                "order = np.argsort(c[on_axis, 0])\n"
                "plt.plot(c[on_axis, 0][order] * 1e3, np.linalg.norm(f['U'][on_axis], axis=1)[order])\n"
                "plt.xlabel('x [mm]'); plt.ylabel('|U| [m/s]'); show()"),
    "edit": ("# a journalled edit (revert it in the Command Journal):\n"
             "# do('dict.set', file=str(case.root / 'system/controlDict'), key='endTime', value='1200')"),
}


class _Interpreter(code.InteractiveInterpreter):
    """Tracebacks go to the cell's output, never to sys.excepthook (the app's error reporter)."""

    def _print_exc(self, skip_frames: int) -> None:
        import sys
        import traceback

        t, v, tb = sys.exc_info()
        for _ in range(skip_frames):
            tb = tb.tb_next if tb is not None else None
        sys.stderr.write("".join(traceback.format_exception(t, v, tb)))

    def showtraceback(self) -> None:
        self._print_exc(1)

    def showsyntaxerror(self, filename=None, **kwargs) -> None:
        self._print_exc(0)


@dataclass
class RunResult:
    output: str
    images: list[str] = field(default_factory=list)
    more: bool = False  # the source is incomplete (e.g. an open block): keep reading lines


class Console:
    def __init__(self, session=None, *, figures_dir: Path | None = None):
        self.session = session
        self.figures_dir = Path(figures_dir) if figures_dir else None
        self._images: list[str] = []
        self._count = itertools.count(1)
        self.ns: dict = {"__name__": "__console__", "Path": Path}
        self._setup()
        self.interp = _Interpreter(self.ns)

    def _setup(self) -> None:
        import numpy as np

        self.ns["np"] = np
        os.environ.setdefault("MPLBACKEND", "Agg")
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        self.ns["plt"] = plt
        self.ns["show"] = self._show
        s = self.session
        if s is not None:
            from aethercfd.results.casedata import CaseData

            self.ns["session"] = s
            self.ns["case"] = s.case()
            self.ns["data"] = CaseData(s.root)
            self.ns["do"] = lambda name, **params: s.do(name, **params)

    def _show(self, fig=None) -> str:
        """Save the current (or given) matplotlib figure and show it inline below the cell."""
        plt = self.ns["plt"]
        fig = fig or plt.gcf()
        d = self.figures_dir or Path(os.environ.get("TEMP", ".")) / "aethercfd-console"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"figure_{os.getpid()}_{next(self._count)}.png"
        fig.savefig(p, dpi=110, bbox_inches="tight")
        plt.close(fig)
        self._images.append(str(p))
        return str(p)

    def run(self, source: str, *, force: bool = False) -> RunResult:
        """Execute one console cell. A single expression echoes its value, like the Python prompt. Without
        ``force``, an unfinished cell (open block, unclosed bracket) is not run: ``more`` asks for more lines."""
        buf = io.StringIO()
        self._images = []
        if not force:
            try:
                compiled = code.compile_command(source, "<console>", "single")
            except (SyntaxError, OverflowError, ValueError):
                compiled = False  # run it: the traceback shows the error
            if compiled is None:
                return RunResult("", more=True)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if "\n" in source.strip():  # multi-line cell: run as a block, echo nothing
                try:
                    exec(compile(source, "<console>", "exec"), self.ns)
                except SystemExit:
                    print("(exit() is ignored in the console)")
                except BaseException:
                    self.interp.showtraceback()
            elif self.interp.runsource(source, "<console>", "single") and force:
                # a one-line compound statement ("for i in x: print(i)") needs a blank line to end it at the
                # interactive prompt; an explicit run executes it instead of silently waiting for more input
                try:
                    exec(compile(source, "<console>", "exec"), self.ns)
                except SystemExit:
                    print("(exit() is ignored in the console)")
                except BaseException:
                    self.interp.showtraceback()
        return RunResult(buf.getvalue(), list(self._images))
