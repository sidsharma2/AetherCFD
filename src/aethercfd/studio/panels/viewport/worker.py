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


def _smooth(poly: vtk.vtkPolyData, name: str) -> vtk.vtkPolyData:
    """Studio look: the shown cell array also as point data (averaged from the neighbouring cells) plus
    point normals, for smooth gradients and lighting. Cell data stay on the polydata: probing and the
    Classic look still use the true cell values."""
    keep = vtk.vtkPassSelectedArrays()
    keep.SetInputData(poly)
    keep.GetCellDataArraySelection().EnableArray(name)
    keep.GetPointDataArraySelection().DisableAllArrays()
    c2p = vtk.vtkCellDataToPointData()
    c2p.SetInputConnection(keep.GetOutputPort())
    c2p.PassCellDataOff()
    c2p.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(poly)
    pa = c2p.GetOutput().GetPointData().GetArray(name)
    if pa is not None:
        out.GetPointData().AddArray(pa)
    if out.GetNumberOfPolys():
        n = vtk.vtkPolyDataNormals()
        n.SetInputData(out)
        n.SplittingOff()  # keep the point arrays aligned with the points
        n.ComputeCellNormalsOff()
        n.ConsistencyOn()
        n.Update()
        nn = n.GetOutput().GetPointData().GetNormals()
        if nn is not None and nn.GetNumberOfTuples() == out.GetNumberOfPoints():
            out.GetPointData().SetNormals(nn)
    return out


def _outline(polys: list[vtk.vtkPolyData], angle: float = 40.0) -> vtk.vtkPolyData | None:
    """Sharp and boundary edges of the shown surfaces: the crisp silhouette of the Studio look."""
    app = vtk.vtkAppendPolyData()
    n = 0
    for p in polys:
        if p is None or not p.GetNumberOfPolys():
            continue
        # a clean outline grows like the perimeter (~sqrt of the faces); far more lines than that is
        # mesh texture (fine curved walls, AMI seams) and reads as noise: then sharp edges only, or none
        budget = 14 * p.GetNumberOfPolys() ** 0.5 + 400
        best = None
        for boundary, ang in ((True, angle), (False, 60.0)):
            fe = vtk.vtkFeatureEdges()
            fe.SetInputData(p)
            fe.SetBoundaryEdges(boundary)
            fe.FeatureEdgesOn()
            fe.SetFeatureAngle(ang)
            fe.NonManifoldEdgesOff()
            fe.ManifoldEdgesOff()
            fe.ColoringOff()
            fe.Update()
            if fe.GetOutput().GetNumberOfLines() <= budget:
                best = fe.GetOutput()
                break
        if best is None or not best.GetNumberOfLines():
            continue
        app.AddInputData(best)
        n += 1
    if not n:
        return None
    app.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(app.GetOutput())
    return out


class VtkWorker(QObject):
    meta_ready = Signal(str, int, object)  # viewport id, generation, dict
    data_ready = Signal(str, int, object)  # viewport id, generation, dict
    probe_ready = Signal(str, int, object)  # viewport id, generation, dict (all fields at one cell)
    failed = Signal(str, int, str)

    def _cached(self, key):
        """Geometry that does not change between time steps (glass shell, outline), per worker."""
        return self.__dict__.setdefault("_geo_cache", {}).get(key)

    def _remember(self, key, value):
        cache = self.__dict__.setdefault("_geo_cache", {})
        cache[key] = value
        while len(cache) > 12:  # a few cases / slices; oldest first out
            cache.pop(next(iter(cache)))
        return value

    @Slot(str, int, dict)
    def probe(self, vid: str, gen: int, req: dict) -> None:
        """Every cell field at the cell containing req['point'] (internal meshes, req['time'])."""
        try:
            r = self._reader(req["case"], req.get("decomposed", False))
            mb = r.read(req.get("time"), fields=None, patches=None)
            for path, ds in iter_leaves(mb):
                loc = vtk.vtkStaticCellLocator()
                loc.SetDataSet(ds)
                loc.BuildLocator()
                cid = loc.FindCell(req["point"], 1e-9, vtk.vtkGenericCell(), [0, 0, 0], [0.0] * 8)
                if cid < 0:
                    continue
                values = {}
                cd = ds.GetCellData()
                for i in range(cd.GetNumberOfArrays()):
                    a = cd.GetArray(i)
                    if a is None or a.GetName() in ("vtkOriginalCellIds",):
                        continue
                    values[a.GetName()] = list(a.GetTuple(cid))
                self.probe_ready.emit(vid, gen, {"region": path, "cell": cid, "values": values,
                                                 "point": list(req["point"]), "time": req.get("time"),
                                                 "cell_type": ds.GetCellType(cid)})
                return
            self.probe_ready.emit(vid, gen, {"point": list(req["point"]), "cell": None, "values": {},
                                             "time": req.get("time")})
        except Exception as exc:
            log.exception("viewport probe failed")
            self.failed.emit(vid, gen, f"probe: {type(exc).__name__}: {exc}")

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
            from pathlib import Path

            from aethercfd.results import pipeline as pl

            r = self._reader(req["case"], req.get("decomposed", False))
            field = req.get("field")
            stages = [s for s in req.get("stages", []) if s.get("visible", True)]
            available = set(r.meta.cell_arrays or r.refresh().cell_arrays)
            want = ({field} if field else set()) | (pl.fields_needed(stages) & available)
            patches = req.get("patches")
            show_internal = req.get("show_internal", True)
            if stages and patches is not None:
                patches = list(dict.fromkeys([*patches, *r.meta.internal_meshes]))
            mb = r.read(req.get("time"), fields=sorted(want), patches=patches)
            blocks: dict[str, vtk.vtkPolyData] = {}
            internal, internal_paths = [], []
            for path, ds in iter_leaves(mb):
                if path.endswith("internalMesh"):
                    internal.append(ds)
                    internal_paths.append(path)
                    if not req.get("slice") and show_internal:
                        blocks[path] = _geometry(ds)
                else:
                    blocks[path] = _geometry(ds)
            sl = req.get("slice")
            if sl and internal:
                for i, ds in enumerate(internal):
                    blocks[f"slice{i}"] = _slice(ds, sl["origin"], sl["normal"])
                    if req.get("shell"):  # Studio: the domain as a faint glass shell around the slice
                        key = (req["case"], i, ds.GetNumberOfPoints(), ds.GetNumberOfCells())
                        shell = self._cached(("shell", key))
                        if shell is None:
                            shell = self._remember(("shell", key), _geometry(ds))
                        blocks[f"shell:{i}"] = shell
            stage_info: dict[str, dict] = {}
            if stages and internal:
                case = Path(req["case"])
                mesh_dirs = [case / "constant" / (p.rsplit("/", 1)[0] if "/" in p else "") / "polyMesh"
                             for p in internal_paths]
                for s in stages:
                    out = pl.run_stage(s, internal, field, req.get("component", "magnitude"), mesh_dirs=mesh_dirs,
                                       decomposed=bool(req.get("decomposed")))
                    key = f"stage:{s.get('id')}"
                    blocks[key] = out.poly
                    stage_info[key] = {"scalar": out.scalar, "association": out.association, "range": out.range,
                                       "cmap": out.cmap, "interpolated": out.interpolated, "label": out.label,
                                       "notes": out.notes, "cells": out.poly.GetNumberOfCells(),
                                       "opacity": float(s.get("opacity", 1.0))}
            scalar = None
            n_comp = 0
            lo, hi = np.inf, -np.inf
            for bname, poly in blocks.items():
                if bname in stage_info or bname.startswith("shell:"):
                    continue  # stages carry their own colouring; the glass shell has none
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
                        if req.get("look") == "studio":
                            blocks[bname] = _smooth(poly, name)
            view = req.get("view") or {}
            if view.get("mirror"):
                ax = view["mirror"]
                for name in list(blocks):
                    blocks[f"{name}~mirror"] = pl.mirror(blocks[name], ax, float(view.get("mirror_origin", 0.0)))
                    if name in stage_info:
                        stage_info[f"{name}~mirror"] = stage_info[name]
            if view.get("revolve"):
                rv = view["revolve"]
                for name in list(blocks):
                    for k, poly in enumerate(pl.revolve(blocks[name], rv.get("axis", "z"), float(rv.get("angle", 5)),
                                                        int(rv.get("copies", 72)),
                                                        tuple(rv.get("origin", (0.0, 0.0, 0.0))))):
                        blocks[f"{name}~rev{k}"] = poly
                        if name in stage_info:
                            stage_info[f"{name}~rev{k}"] = stage_info[name]
            overlays = {}
            if req.get("look") == "studio":
                base = [(k, p) for k, p in blocks.items() if k not in stage_info and "~rev" not in k]
                key = ("edges", req["case"], tuple((k, p.GetNumberOfPoints(), p.GetNumberOfPolys()) for k, p in base),
                       repr(req.get("slice")))
                edges = self._cached(key)
                if edges is None:
                    edges = self._remember(key, _outline([p for _k, p in base]))
                if edges is not None:
                    overlays["edges"] = edges
            bounds = [np.inf, -np.inf] * 3
            n_cells = 0
            # the domain's extent: the internal meshes, else (patches only, e.g. a big case) the shown surfaces
            extent = internal or [p for k, p in blocks.items() if k not in stage_info and "~" not in k]
            for ds in extent:
                if ds.GetNumberOfPoints() == 0:
                    continue
                b = ds.GetBounds()
                for k in range(3):
                    bounds[2 * k] = min(bounds[2 * k], b[2 * k])
                    bounds[2 * k + 1] = max(bounds[2 * k + 1], b[2 * k + 1])
            for ds in internal:
                n_cells += ds.GetNumberOfCells()
            self.data_ready.emit(vid, gen, {
                "blocks": blocks, "scalar": scalar, "range": (lo, hi) if lo <= hi else None,
                "time": req.get("time"), "bounds": bounds if bounds[0] <= bounds[1] else None, "n_cells": n_cells,
                "components": n_comp, "stage_info": stage_info, "overlays": overlays,
                "smoothed": req.get("look") == "studio" and scalar is not None,
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

