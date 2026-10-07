"""Spike B — load real OpenFOAM cases with vtkOpenFOAMReader on a worker QThread while the GUI stays
responsive; render on the GUI thread on the *real* FV mesh (cell data, no resampling).

Run:  uv run python spikes/spike_b_reader_worker.py <case_dir> [<case_dir> ...] [--steps 5]
Never writes into the case: the reader is pointed at <case>/system/controlDict (no .foam stub).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
ap = argparse.ArgumentParser()
ap.add_argument("cases", nargs="+")
ap.add_argument("--steps", type=int, default=5, help="how many latest time steps to step through")
ap.add_argument("--field", default=None)
args = ap.parse_args()

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal, Slot  # noqa: E402
from PySide6.QtGui import QSurfaceFormat  # noqa: E402
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface  # noqa: E402
from pyvistaqt.rwi import _default_format  # noqa: E402

QSurfaceFormat.setDefaultFormat(_default_format())
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

app = QApplication(sys.argv)
import psutil  # noqa: E402
import pyvista as pv  # noqa: E402
import vtk  # noqa: E402
from pyvistaqt import QtInteractor  # noqa: E402

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
proc = psutil.Process()


def snapshot_tree(case: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) for every file — used to prove the reader wrote nothing into the case."""
    out = {}
    for root, _dirs, files in os.walk(case):
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


class VtkWorker(QObject):
    """Owns readers; runs on its own QThread. Results are shallow copies handed to the GUI thread."""

    meta_ready = Signal(int, dict)
    data_ready = Signal(int, object, float)  # gen, vtkMultiBlockDataSet, seconds
    failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.readers: dict[str, vtk.vtkPOpenFOAMReader] = {}

    def _reader(self, case: str) -> vtk.vtkPOpenFOAMReader:
        r = self.readers.get(case)
        if r is None:
            r = vtk.vtkPOpenFOAMReader()
            r.SetFileName(str(Path(case) / "system" / "controlDict"))
            r.SetCaseType(1)  # 1 = reconstructed, 0 = decomposed
            r.CreateCellToPointOff()  # truthful default: cell data only
            r.CacheMeshOn()
            r.SkipZeroTimeOff()
            r.ListTimeStepsByControlDictOff()
            r.ReadZonesOff()
            self.readers[case] = r
        return r

    @Slot(int, str)
    def load_meta(self, gen: int, case: str):
        try:
            t0 = time.perf_counter()
            r = self._reader(case)
            r.UpdateInformation()
            tv = r.GetTimeValues()
            times = [tv.GetValue(i) for i in range(tv.GetNumberOfTuples())] if tv else []
            patches = [r.GetPatchArrayName(i) for i in range(r.GetNumberOfPatchArrays())]
            cells = [r.GetCellArrayName(i) for i in range(r.GetNumberOfCellArrays())]
            self.meta_ready.emit(gen, {"case": case, "times": times, "patches": patches, "cell_arrays": cells,
                                       "meta_s": round(time.perf_counter() - t0, 3)})
        except Exception as exc:  # report, never kill the worker
            self.failed.emit(gen, f"meta: {exc!r}")

    @Slot(int, str, float, list)
    def load_step(self, gen: int, case: str, t: float, fields: list):
        try:
            t0 = time.perf_counter()
            r = self._reader(case)
            for i in range(r.GetNumberOfCellArrays()):
                name = r.GetCellArrayName(i)
                r.SetCellArrayStatus(name, 1 if name in fields else 0)
            # internal mesh only for the spike; patches are toggled per block in the real viewport
            for i in range(r.GetNumberOfPatchArrays()):
                name = r.GetPatchArrayName(i)
                r.SetPatchArrayStatus(name, 1 if name.endswith("internalMesh") else 0)
            r.UpdateTimeStep(t)
            r.Update()
            src = r.GetOutput()
            out = src.NewInstance()
            out.ShallowCopy(src)
            self.data_ready.emit(gen, out, time.perf_counter() - t0)
        except Exception as exc:
            self.failed.emit(gen, f"step {t}: {exc!r}")


class Main(QMainWindow):
    req_meta = Signal(int, str)
    req_step = Signal(int, str, float, list)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AetherCFD — Spike B (reader on worker thread)")
        self.resize(1300, 800)
        self.plotter = QtInteractor(self, auto_update=False)
        self.plotter.set_background("#1b1c1f")
        self.setCentralWidget(self.plotter)
        self.thread = QThread()
        self.worker = VtkWorker()
        self.worker.moveToThread(self.thread)
        self.req_meta.connect(self.worker.load_meta, Qt.ConnectionType.QueuedConnection)
        self.req_step.connect(self.worker.load_step, Qt.ConnectionType.QueuedConnection)
        self.worker.meta_ready.connect(self.on_meta)
        self.worker.data_ready.connect(self.on_data)
        self.worker.failed.connect(self.on_fail)
        self.thread.start()
        # jitter probe: a 10 ms timer; the largest gap between ticks = worst GUI stall
        self.last_tick = time.perf_counter()
        self.max_gap = 0.0
        self.probe = QTimer(self)
        self.probe.setInterval(10)
        self.probe.timeout.connect(self._tick)
        self.probe.start()
        self.gen = 0
        self.queue = [str(Path(c).resolve()) for c in args.cases]
        self.report: dict = {"cases": []}
        self.cur: dict = {}
        self.before_tree: dict = {}

    loading = False
    max_gap_loading = 0.0

    def _tick(self):
        now = time.perf_counter()
        gap = now - self.last_tick
        self.max_gap = max(self.max_gap, gap)
        if self.loading:
            self.max_gap_loading = max(self.max_gap_loading, gap)
        self.last_tick = now

    def next_case(self):
        if not self.queue:
            return self.finish()
        case = self.queue.pop(0)
        self.before_tree = snapshot_tree(Path(case))
        self.cur = {"case": case, "steps": [], "rss_before_mb": round(proc.memory_info().rss / 2**20)}
        self.max_gap = 0.0
        self.max_gap_loading = 0.0
        self.gen += 1
        self.req_meta.emit(self.gen, case)

    @Slot(int, dict)
    def on_meta(self, gen, meta):
        self.cur.update({k: meta[k] for k in ("meta_s",)})
        self.cur["n_times"] = len(meta["times"])
        self.cur["n_patches"] = len(meta["patches"])
        self.cur["cell_arrays"] = meta["cell_arrays"]
        fields = meta["cell_arrays"]
        pick = args.field if args.field in fields else next((f for f in ("T", "U", "p") if f in fields), fields[0] if fields else None)
        self.cur["field"] = pick
        self.pending = list(meta["times"][-args.steps:]) if meta["times"] else []
        print(f"\n{Path(meta['case']).name}: {len(meta['times'])} times, {len(meta['patches'])} patches, "
              f"arrays={meta['cell_arrays']} meta={meta['meta_s']}s -> stepping {self.pending} field={pick}")
        self._next_step()

    def _next_step(self):
        if not self.pending:
            after = snapshot_tree(Path(self.cur["case"]))
            changed = [p for p in set(self.before_tree) | set(after) if self.before_tree.get(p) != after.get(p)]
            self.cur["case_tree_unchanged"] = not changed
            self.cur["changed_files"] = changed[:10]
            self.cur["max_gui_stall_ms"] = round(self.max_gap * 1000, 1)
            self.cur["max_stall_while_loading_ms"] = round(self.max_gap_loading * 1000, 1)
            self.cur["rss_after_mb"] = round(proc.memory_info().rss / 2**20)
            self.report["cases"].append(self.cur)
            self.grab().save(str(OUT / f"spike_b_{Path(self.cur['case']).name}.png"))
            return QTimer.singleShot(200, self.next_case)
        t = self.pending.pop(0)
        self.gen += 1
        self.t_req = time.perf_counter()
        self.loading = True
        self.req_step.emit(self.gen, self.cur["case"], float(t), [self.cur["field"]] if self.cur["field"] else [])

    @Slot(int, object, float)
    def on_data(self, gen, mb, load_s):
        self.loading = False
        t_gui = time.perf_counter()
        ds = pv.wrap(mb)

        def first_mesh(d):
            if isinstance(d, pv.MultiBlock):
                for i in range(d.n_blocks):
                    m = first_mesh(d[i]) if d[i] is not None else None
                    if m is not None:
                        return m
                return None
            return d if d is not None and d.n_cells > 0 else None

        block = first_mesh(ds)
        if block is None:
            self.on_fail(gen, "no non-empty internal mesh block in reader output")
            return
        field = self.cur["field"]
        self.plotter.clear()
        if field and field in block.cell_data:
            arr = block.cell_data[field]
            scalars = field
            self.plotter.add_mesh(block, scalars=scalars, preference="cell", show_scalar_bar=True,
                                  cmap="viridis", show_edges=False)
            rng = (float(arr.min()), float(arr.max())) if arr.ndim == 1 else None
        else:
            self.plotter.add_mesh(block, color="#9aa0a6")
            rng = None
        if len(self.cur["steps"]) == 0:
            b = block.bounds
            thin = sorted([b[1] - b[0], b[3] - b[2], b[5] - b[4]])
            self.cur["n_cells"] = block.n_cells
            self.cur["is_2d_like"] = thin[0] < 1e-3 * thin[2]
            if self.cur["is_2d_like"]:
                self.plotter.view_xy()
                self.plotter.enable_parallel_projection()
            else:
                self.plotter.reset_camera()
        self.plotter.render()
        self.cur["steps"].append({"load_s": round(load_s, 3), "gui_s": round(time.perf_counter() - t_gui, 3),
                                  "range": rng})
        print(f"   step loaded in {load_s:.3f}s (worker), GUI handoff+render {time.perf_counter() - t_gui:.3f}s, "
              f"cells={block.n_cells} range={rng}")
        QTimer.singleShot(50, self._next_step)

    @Slot(int, str)
    def on_fail(self, gen, msg):
        print("   FAILED:", msg)
        self.cur.setdefault("errors", []).append(msg)
        QTimer.singleShot(0, self._next_step)

    def finish(self):
        gl = self.plotter.ren_win.ReportCapabilities()
        self.report["gl_renderer"] = next((ln.split(":", 1)[1].strip() for ln in gl.splitlines() if "renderer string" in ln), "?")
        (OUT / "spike_b.json").write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        ok = all(c.get("case_tree_unchanged") and c.get("max_stall_while_loading_ms", 1e9) < 50 and not c.get("errors")
                 for c in self.report["cases"])
        for c in self.report["cases"]:
            print(f"{Path(c['case']).name}: cells={c.get('n_cells')} 2D={c.get('is_2d_like')} max_stall={c.get('max_gui_stall_ms')}ms (while loading {c.get('max_stall_while_loading_ms')}ms) "
                  f"tree_unchanged={c.get('case_tree_unchanged')} rss {c['rss_before_mb']}->{c.get('rss_after_mb')} MB "
                  f"errors={c.get('errors')}")
        print("ALL OK" if ok else "SOME CHECKS FAILED")
        self.exit_code = 0 if ok else 1
        self.thread.quit()
        self.thread.wait(5000)
        self.plotter.close()
        app.quit()


w = Main()
w.show()
QTimer.singleShot(300, w.next_case)
app.exec()
sys.exit(getattr(w, "exit_code", 1))
