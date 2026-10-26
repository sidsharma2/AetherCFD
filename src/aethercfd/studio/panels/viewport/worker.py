"""VtkWorker: reading and filtering on ONE dedicated thread (ADR 0005). The GUI thread only receives
ready-to-render vtkPolyData and never runs reader/filter code. VTK releases the GIL while it works, so the
UI stays responsive even for million-cell meshes.

Thread rules (ADR 0008): the reader's output arrives as a deep copy (FoamResults.read), so nothing handed to
the GUI is ever rewritten by the reader; everything else crosses as the frozen output of a filter this worker
created for one call and never runs again. Objects in the geometry cache (_cached/_remember: glass shells,
outlines) are reused across loads after the GUI already holds them, so they are never modified once
remembered: the colouring loop skips shell blocks and the outline is passed through as is."""

from __future__ import annotations

import logging
import time

import numpy as np
import vtk
from PySide6.QtCore import QObject, QThread, Signal, Slot
from vtk.util import numpy_support as nps

from aethercfd.results.foam_reader import FoamResults, iter_leaves, region_of
from aethercfd.results.pipeline import surface

log = logging.getLogger(__name__)


def _bounds(datasets) -> list[float]:
    b = [np.inf, -np.inf] * 3
    for ds in datasets:
        if ds.GetNumberOfPoints() == 0:
            continue
        db = ds.GetBounds()
        for k in range(3):
            b[2 * k] = min(b[2 * k], db[2 * k])
            b[2 * k + 1] = max(b[2 * k + 1], db[2 * k + 1])
    return b if b[0] <= b[1] else [0.0, 0.0] * 3


def _geometry(ds) -> vtk.vtkPolyData:
    """Outer surface of a volume mesh (cell data preserved, every face wound outward), or the polydata itself."""
    return surface(ds)


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


def _smooth(poly: vtk.vtkPolyData, name: str, field: str | None = None, comp: str = "magnitude") \
        -> vtk.vtkPolyData:
    """Smooth colours: the shown array as point data -- the reader's interpolated values (which use the
    patch values on the boundary) when it made them, else averaged from the neighbouring cells -- plus
    point normals for smooth lighting. Cell data stay on the polydata: probing and Cells mode still use the
    true cell values."""
    out = vtk.vtkPolyData()
    out.ShallowCopy(poly)
    src = out.GetPointData().GetArray(field) if field else None
    if src is not None and out.GetPointData().GetArray(name) is None:
        a = nps.vtk_to_numpy(src)
        if a.ndim > 1 and a.shape[1] > 1:
            v = np.linalg.norm(a, axis=1) if comp == "magnitude" else a[:, "xyz".index(comp)]
            arr = nps.numpy_to_vtk(np.ascontiguousarray(v, dtype=np.float64), deep=True)
            arr.SetName(name)
            out.GetPointData().AddArray(arr)
    if out.GetPointData().GetArray(name) is None:
        keep = vtk.vtkPassSelectedArrays()
        keep.SetInputData(poly)
        keep.GetCellDataArraySelection().EnableArray(name)
        keep.GetPointDataArraySelection().DisableAllArrays()
        c2p = vtk.vtkCellDataToPointData()
        c2p.SetInputConnection(keep.GetOutputPort())
        c2p.PassCellDataOff()
        c2p.Update()
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


def _patch_key(name: str) -> tuple[str, str]:
    """(region, patch) of a reader patch name ('fluid/patch/inlet', 'patch/inlet') or of a read block's path
    ('fluid/boundary/inlet', 'defaultRegion/boundary/inlet', 'boundary/inlet'): the two spell it differently."""
    parts = name.strip("/").split("/")
    if "boundary" in parts[:-1]:
        region = "/".join(parts[:parts.index("boundary")])
        return ("" if region == "defaultRegion" else region), parts[-1]
    return region_of(name), parts[-1]


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
    line_ready = Signal(str, int, object)  # viewport id, request id, dict (a field along a line)
    failed = Signal(str, int, str)

    @Slot(str, int, dict)
    def probe_line(self, vid: str, rid: int, req: dict) -> None:
        """The field along the straight line req['p1'] -> req['p2'] through the volume meshes: interpolated
        (smooth) or each sample taking its cell's value (cells, the steps show)."""
        from aethercfd.results import sections as sx

        try:
            r = self._reader(req["case"], req.get("decomposed", False))
            smooth = req.get("smoothing", "smooth") == "smooth"
            mb = r.read(req.get("time"), fields=[req["field"]], patches=None, cell_to_point=smooth)
            internal = [ds for p, ds in iter_leaves(mb) if p.endswith("internalMesh")]
            res = sx.probe_line(internal, req["p1"], req["p2"], req["field"], n=int(req.get("n", 300)),
                                comp=req.get("component", "magnitude"), smooth=smooth)
            res.update({"p1": list(req["p1"]), "p2": list(req["p2"]), "time": req.get("time")})
            self.line_ready.emit(vid, rid, res)
        except Exception as exc:
            log.exception("line probe failed")
            self.line_ready.emit(vid, rid, {"error": f"{type(exc).__name__}: {exc}", "p1": req.get("p1"),
                                            "p2": req.get("p2")})

    def _pathlines(self, r, req: dict, spec: dict, internal: list, patches: dict):
        """Particle paths through the saved times of a transient run; streamlines (said so) when there is only
        one saved time or the run is steady (its saved times are iterations, not physical time)."""
        from aethercfd.results import pipeline as pl
        from aethercfd.results import sections as sx

        vec = spec.get("field") or "U"
        times = [t for t in (r.meta.times or []) if req.get("time") is None or t <= req["time"] + 1e-12]
        reason = None
        if len(times) < 2:
            reason = "only one saved time: pathlines need a transient run with several saved times"
        elif req.get("steady"):
            reason = "a steady run: its saved times are iterations, not physical time"
        if reason:
            out = pl.stage_streamlines({**spec, "type": "streamlines"}, internal, None, "magnitude",
                                       patches=patches)
            out.notes.insert(0, f"{reason}; showing streamlines (the paths of the steady flow) instead")
            out.label += " (streamlines, not pathlines)"
            return out
        if times[0] == 0.0 and len(times) > 2:
            times = times[1:]  # the initial field is not a flow yet
        step = max(1, -(-len(times) // int(spec.get("max_times", 40))))
        picked = times[::step]
        if picked[-1] != times[-1]:
            picked.append(times[-1])
        seeds_alg = pl.flow_seeds(spec, pl.bounds_of(internal), patches)
        seeds_alg.Update()
        seed_poly = seeds_alg.GetOutput()
        if not seed_poly.GetNumberOfPoints():
            return pl.StageOutput(vtk.vtkPolyData(), label="pathlines", notes=["no seeds"])
        seeds = nps.vtk_to_numpy(seed_poly.GetPoints().GetData())
        frames = []
        for t in picked:
            mb = r.read(t, fields=[vec], patches=None, cell_to_point=True)
            dss = []
            for p, ds in iter_leaves(mb):
                if p.endswith("internalMesh") and (not r.meta.regions or region_of(p)):
                    c = ds.NewInstance()
                    c.ShallowCopy(ds)
                    dss.append(c)
            frames.append((t, dss))
        poly = sx.pathlines(seeds, frames, vec, substeps=int(spec.get("substeps", 8)))
        b = pl.bounds_of(internal)
        diag = ((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) ** 0.5
        color = "time" if spec.get("color_by") in ("residence_time", "time") else f"{vec}_mag"
        rng = pl._range(poly, color, "point")
        if spec.get("tubes") and poly.GetNumberOfLines():
            poly = sx.tubes(poly, float(spec.get("tube_radius", 0.0025)) * diag)
        return pl.StageOutput(poly, color, "point", rng, interpolated=True,
                              title="time since release [s]" if color == "time" else None,
                              label=f"pathlines {vec} ({len(picked)} saved times, {picked[0]:g}..{picked[-1]:g} s)",
                              notes=[f"released at t = {picked[0]:g} s; velocity interpolated in space and linearly "
                                     "in time between saved times"])

    def _cached(self, key):
        """Geometry that does not change between time steps (glass shell, outline), per worker."""
        return self.__dict__.setdefault("_geo_cache", {}).get(key)

    def _remember(self, key, value):
        """Cache ``value``. The GUI may already hold it, so nothing modifies it from here on (ADR 0008 rule 3)."""
        cache = self.__dict__.setdefault("_geo_cache", {})
        cache[key] = value
        while len(cache) > 16:  # a few cases / slices / region shells; oldest first out
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
        r = self._readers.pop(key, None)
        if r is None:
            r = FoamResults(case, decomposed=decomposed)
            # a reader caches its whole mesh: keep the two most recent, so opening case after case (a CHT
            # case, then a decomposed one) does not hold every mesh in memory at once
            while len(self._readers) >= 2:
                old_case, _d = next(iter(self._readers))
                self._readers.pop(next(iter(self._readers)))
                cache = self.__dict__.get("_geo_cache", {})
                for k in [k for k in cache if old_case in repr(k)]:
                    cache.pop(k, None)
        self._readers[key] = r  # most recently used last
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
            smooth = req.get("smoothing", "smooth" if req.get("look") == "studio" else "cells") == "smooth"
            stages = [s for s in req.get("stages", []) if s.get("visible", True)]
            available = set(r.meta.cell_arrays or r.refresh().cell_arrays)
            want = ({field} if field else set()) | (pl.fields_needed(stages) & available)
            patches = req.get("patches")
            show_internal = req.get("show_internal", True)
            shown_regions = req.get("regions")  # multi-region case: the regions shown (None = all)
            if shown_regions is not None and patches is not None:
                patches = [p for p in patches if region_of(p) in shown_regions]
            shown = set(patches or [])
            extra = pl.patches_needed(stages)
            if extra:  # inlet seeds, a plane through a patch: read those patches too (never drawn for it)
                listed = r.meta.patches or r.refresh().patches
                extra = {p for p in listed if p in extra or p.split("/")[-1] in extra}
            if stages and patches is not None:  # stages run on the shown regions' internal meshes
                patches = list(dict.fromkeys([*patches, *(p for p in r.meta.internal_meshes
                                                          if shown_regions is None or region_of(p) in shown_regions),
                                              *sorted(extra)]))
            extra_keys = {_patch_key(p): p for p in extra}
            shown_keys = {_patch_key(p) for p in shown}
            # smooth + something cut through the volume: the reader interpolates the fields to the mesh points
            # (using the patch values on the boundary), so sections and contours are continuous in 3D; plain
            # surfaces alone are smoothed from their own faces (_smooth), much cheaper on a big mesh
            vol_points = smooth and bool(req.get("slice") or stages)
            mb = r.read(req.get("time"), fields=sorted(want), patches=patches, cell_to_point=vol_points)
            blocks: dict[str, vtk.vtkPolyData] = {}
            block_regions: dict[str, str] = {}  # block -> its region (per-region depth offsets in the view)
            internal, internal_paths = [], []
            patch_polys: dict[str, vtk.vtkPolyData] = {}  # reader patch name -> surface (seeds, planes)
            for path, ds in iter_leaves(mb):
                if path.endswith("internalMesh"):
                    # our own array lists: pathlines read other times with the same reader (its output is
                    # already a deep copy since 2d8564e, ADR 0008; this copy only keeps the lists apart)
                    own = ds.NewInstance()
                    own.ShallowCopy(ds)
                    ds = own
                    internal.append(ds)
                    internal_paths.append(path)
                    if not req.get("slice") and show_internal:
                        blocks[path] = _geometry(ds)
                else:
                    key = _patch_key(path)
                    if key in extra_keys:
                        patch_polys[extra_keys[key]] = _geometry(ds)
                    if key not in extra_keys or key in shown_keys:
                        blocks[path] = _geometry(ds)
                block_regions[path] = region_of(path)
            sl = req.get("slice")
            want_shell = req.get("look") == "studio" and any(s.get("section") or s.get("cutaway") for s in stages)
            if internal and want_shell:  # Studio: the domain as a faint glass shell around sections / a cut-away
                for i, (path, ds) in enumerate(zip(internal_paths, internal, strict=True)):
                    key = (req["case"], path, ds.GetNumberOfPoints(), ds.GetNumberOfCells())
                    shell = self._cached(("shell", key))
                    if shell is None:
                        shell = self._remember(("shell", key), _geometry(ds))
                    blocks[f"shell:{i}"] = shell
                    block_regions[f"shell:{i}"] = region_of(path)
            if sl and internal:
                origin = sl.get("origin")
                if origin is None:  # a fraction along the axis of the shown regions' extent (never stale)
                    b = _bounds(internal)
                    ax, frac = int(sl.get("axis", 2)), float(sl.get("frac", 0.5))
                    origin = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
                    origin[ax] = b[2 * ax] + frac * (b[2 * ax + 1] - b[2 * ax])
                for i, (path, ds) in enumerate(zip(internal_paths, internal, strict=True)):
                    blocks[f"slice{i}"] = _slice(ds, origin, sl["normal"])
                    block_regions[f"slice{i}"] = region_of(path)
                    if req.get("shell"):  # Studio: the domain as a faint glass shell around the slice
                        key = (req["case"], path, ds.GetNumberOfPoints(), ds.GetNumberOfCells())
                        shell = self._cached(("shell", key))
                        if shell is None:
                            shell = self._remember(("shell", key), _geometry(ds))
                        blocks[f"shell:{i}"] = shell
                        block_regions[f"shell:{i}"] = region_of(path)
            stage_info: dict[str, dict] = {}
            if stages and internal:
                case = Path(req["case"])
                mesh_dirs = [case / "constant" / (p.rsplit("/", 1)[0] if "/" in p else "") / "polyMesh"
                             for p in internal_paths]
                for s in stages:
                    if s.get("type") == "pathlines":
                        out = self._pathlines(r, req, s, internal, patch_polys)
                    else:
                        out = pl.run_stage(s, internal, field, req.get("component", "magnitude"),
                                           mesh_dirs=mesh_dirs, decomposed=bool(req.get("decomposed")),
                                           smooth=smooth, patches=patch_polys)
                    key = f"stage:{s.get('id')}"
                    blocks[key] = out.poly
                    stage_info[key] = {"scalar": out.scalar, "association": out.association, "range": out.range,
                                       "cmap": out.cmap, "interpolated": out.interpolated, "label": out.label,
                                       "notes": out.notes, "cells": out.poly.GetNumberOfCells(),
                                       "opacity": float(s.get("opacity", 1.0)), "type": s.get("type"),
                                       "edges": out.edges, "title": out.title}
                    for kind, ov in out.overlays.items():  # contour lines / arrows on a section
                        if ov is not None and ov.GetNumberOfCells():
                            blocks[f"{key}@{kind}"] = ov
                            stage_info[f"{key}@{kind}"] = {"overlay": kind, "label": f"{out.label} {kind}",
                                                           "cells": ov.GetNumberOfCells(), "notes": [],
                                                           "scalar": None, "range": None, "type": s.get("type")}
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
                        if smooth:
                            blocks[bname] = _smooth(poly, name, field, req.get("component", "magnitude"))
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
                "block_regions": block_regions,
                "smoothed": smooth and scalar is not None, "smoothing": "smooth" if smooth else "cells",
                "seconds": time.perf_counter() - t0})
        except Exception as exc:
            log.exception("viewport load failed")
            self.failed.emit(vid, gen, f"{type(exc).__name__}: {exc}")


_SHARED: tuple[QThread, VtkWorker] | None = None
_FINISHING: list[tuple[QThread, VtkWorker]] = []  # stopped while still busy: kept alive until they end


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
        th, w = _SHARED
        th.quit()
        if not th.wait(5000):
            # still inside a long read/filter: destroying a running QThread aborts the process (qFatal), so
            # keep it referenced; it quits as soon as the current request returns
            _FINISHING.append((th, w))
        _FINISHING[:] = [(t, x) for t, x in _FINISHING if t.isRunning()]
        _SHARED = None

