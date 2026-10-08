"""VtkWorker: reading and filtering on ONE dedicated thread (ADR 0005). The GUI thread only receives
ready-to-render vtkPolyData (shallow copies) and never runs reader/filter code. VTK releases the GIL
while it works, so the UI stays responsive even for million-cell meshes."""

from __future__ import annotations

import logging
import time

import numpy as np
import vtk
from PySide6.QtCore import QObject, QThread, Signal, Slot
from vtk.util import numpy_support as nps

from aethercfd.results.foam_reader import FoamResults, iter_leaves

log = logging.getLogger(__name__)


def _geometry(ds) -> vtk.vtkPolyData:
    """Outer surface of a volume mesh (cell data preserved), or the polydata itself."""
    if ds.IsA("vtkPolyData"):
        return ds
    f = vtk.vtkGeometryFilter()
    f.SetInputData(ds)
    f.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(f.GetOutput())
    return out


def _slice(ds, origin, normal) -> vtk.vtkPolyData:
    """Planar cut through real cells: every cut polygon carries its parent cell's value."""
    plane = vtk.vtkPlane()
    plane.SetOrigin(*origin)
    plane.SetNormal(*normal)
    c = vtk.vtkPlaneCutter()
    c.SetInputData(ds)
    c.SetPlane(plane)
    c.InterpolateAttributesOn()
    c.Update()
    out = c.GetOutput()
    if out.IsA("vtkMultiBlockDataSet") or out.IsA("vtkPartitionedDataSetCollection") or out.IsA(
            "vtkPartitionedDataSet"):
        app = vtk.vtkAppendPolyData()
        it = out.NewIterator()
        it.InitTraversal()
        while not it.IsDoneWithTraversal():
            d = it.GetCurrentDataObject()
            if d is not None and d.IsA("vtkPolyData"):
                app.AddInputData(d)
            it.GoToNextItem()
        app.Update()
        res = vtk.vtkPolyData()
        res.ShallowCopy(app.GetOutput())
        return res
    res = vtk.vtkPolyData()
    res.ShallowCopy(out)
    return res


def _add_component(poly: vtk.vtkPolyData, field: str, comp: str) -> str | None:
    """Make the requested scalar (magnitude / x / y / z) available as a cell array; return its name."""
    arr = poly.GetCellData().GetArray(field)
    if arr is None:
        return None
    if arr.GetNumberOfComponents() == 1:
        return field
    a = nps.vtk_to_numpy(arr)
    if comp == "magnitude":
        v, name = np.linalg.norm(a, axis=1), f"{field}_mag"
    else:
        i = "xyz".index(comp)
        v, name = a[:, i], f"{field}_{comp}"
    out = nps.numpy_to_vtk(np.ascontiguousarray(v, dtype=np.float64), deep=True)
    out.SetName(name)
    poly.GetCellData().AddArray(out)
    return name


class VtkWorker(QObject):
    meta_ready = Signal(str, int, object)  # viewport id, generation, dict
    data_ready = Signal(str, int, object)  # viewport id, generation, dict
    failed = Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self._readers: dict[tuple[str, bool], FoamResults] = {}

    def _reader(self, case: str, decomposed: bool) -> FoamResults:
        key = (case, decomposed)
        r = self._readers.get(key)
        if r is None:
            r = FoamResults(case, decomposed=decomposed)
            self._readers[key] = r
        return r

    @Slot(str, int, dict)
    def meta(self, vid: str, gen: int, req: dict) -> None:
        try:
            r = self._reader(req["case"], req.get("decomposed", False))
            m = r.refresh()
            self.meta_ready.emit(vid, gen, {
                "times": m.times, "patches": m.patches, "cell_arrays": m.cell_arrays, "regions": m.regions})
        except Exception as exc:
            log.exception("viewport meta failed")
            self.failed.emit(vid, gen, f"{type(exc).__name__}: {exc}")

    @Slot(str, int, dict)
    def load(self, vid: str, gen: int, req: dict) -> None:
        t0 = time.perf_counter()
        try:
            r = self._reader(req["case"], req.get("decomposed", False))
            field = req.get("field")
            mb = r.read(req.get("time"), fields=[field] if field else [], patches=req.get("patches"))
            blocks: dict[str, vtk.vtkPolyData] = {}
            internal = []
            for path, ds in iter_leaves(mb):
                if path.endswith("internalMesh"):
                    internal.append(ds)
                    if not req.get("slice"):
                        blocks[path] = _geometry(ds)
                else:
                    blocks[path] = _geometry(ds)
            sl = req.get("slice")
            if sl and internal:
                for i, ds in enumerate(internal):
                    blocks[f"slice{i}"] = _slice(ds, sl["origin"], sl["normal"])
            scalar = None
            n_comp = 0
            lo, hi = np.inf, -np.inf
            for poly in blocks.values():
                raw = poly.GetCellData().GetArray(field) if field else None
                if raw is not None:
                    n_comp = raw.GetNumberOfComponents()
                name = _add_component(poly, field, req.get("component", "magnitude")) if field else None
                if name:
                    scalar = name
                    a = poly.GetCellData().GetArray(name)
                    if a is not None and a.GetNumberOfTuples():
                        rlo, rhi = a.GetRange()
                        lo, hi = min(lo, rlo), max(hi, rhi)
            bounds = [np.inf, -np.inf] * 3
            n_cells = 0
            for ds in internal:
                b = ds.GetBounds()
                for k in range(3):
                    bounds[2 * k] = min(bounds[2 * k], b[2 * k])
                    bounds[2 * k + 1] = max(bounds[2 * k + 1], b[2 * k + 1])
                n_cells += ds.GetNumberOfCells()
            self.data_ready.emit(vid, gen, {
                "blocks": blocks, "scalar": scalar, "range": (lo, hi) if lo <= hi else None,
                "time": req.get("time"), "bounds": bounds if n_cells else None, "n_cells": n_cells,
                "components": n_comp,
                "seconds": time.perf_counter() - t0})
        except Exception as exc:
            log.exception("viewport load failed")
            self.failed.emit(vid, gen, f"{type(exc).__name__}: {exc}")


_SHARED: tuple[QThread, VtkWorker] | None = None


def shared_worker() -> VtkWorker:
    """One worker thread for all viewports (requests are tagged with viewport id + generation)."""
    global _SHARED
    if _SHARED is None:
        th = QThread()
        th.setObjectName("vtk-worker")
        w = VtkWorker()
        w.moveToThread(th)
        th.start()
        _SHARED = (th, w)
    return _SHARED[1]


def stop_shared_worker() -> None:
    global _SHARED
    if _SHARED is not None:
        th, _w = _SHARED
        th.quit()
        th.wait(5000)
        _SHARED = None

