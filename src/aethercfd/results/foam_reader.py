"""Headless access to OpenFOAM results through VTK's OpenFOAM reader (no Qt here).

The reader is pointed at ``<case>/system/controlDict`` so nothing (no ``.foam`` stub) is ever written
into the case. Data stays on the real finite-volume mesh: cell data by default, never resampled.
Thread use: one FoamResults per worker thread; hand results to other threads as shallow copies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import vtk

# VTK replaces ':' '<' '>' in OpenFOAM field names with private-use code points (ADR 0005)
_PUA = {"": ":", "": "<", "": ">"}


def display_name(vtk_name: str) -> str:
    for k, v in _PUA.items():
        vtk_name = vtk_name.replace(k, v)
    return vtk_name


@dataclass
class ResultsMeta:
    times: list[float] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)  # reader patch-array names
    cell_arrays: list[str] = field(default_factory=list)
    point_arrays: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)

    @property
    def internal_meshes(self) -> list[str]:
        return [p for p in self.patches if p.endswith("internalMesh")]


class FoamResults:
    def __init__(self, case_root: str | Path, *, decomposed: bool = False, cache_mesh: bool = True):
        self.case_root = Path(case_root)
        r = vtk.vtkPOpenFOAMReader()
        r.SetFileName(str(self.case_root / "system" / "controlDict"))
        r.SetCaseType(0 if decomposed else 1)
        r.CreateCellToPointOff()
        r.SetCacheMesh(1 if cache_mesh else 0)
        r.SkipZeroTimeOff()
        r.ListTimeStepsByControlDictOff()
        r.ReadZonesOff()
        # keep polyhedra whole: cell i of the internal mesh is OpenFOAM cell i (quality maps and the GPU
        # isosurface engine index cell data by OpenFOAM cell id)
        if hasattr(r, "SetDecomposePolyhedra"):
            r.SetDecomposePolyhedra(0)
        self.reader = r
        self.meta = ResultsMeta()

    def refresh(self) -> ResultsMeta:
        """(Re)read time steps and available arrays (call after new time dirs appear)."""
        r = self.reader
        r.SetRefresh()
        r.UpdateInformation()
        tv = r.GetTimeValues()
        times = [tv.GetValue(i) for i in range(tv.GetNumberOfTuples())] if tv else []
        patches = [r.GetPatchArrayName(i) for i in range(r.GetNumberOfPatchArrays())]
        regions = sorted({p.split("/")[0] for p in patches if p.count("/") >= 1 and p.endswith("internalMesh")})
        cells = [r.GetCellArrayName(i) for i in range(r.GetNumberOfCellArrays())]
        # the reader lists only the fields of ONE time folder; a field written at other times (a derived field
        # such as TPinn, or one switched on mid-run) would be invisible: add every volume field on disk
        cells += [n for n in self._fields_on_disk() if n not in cells]
        self.meta = ResultsMeta(times, patches, cells,
                                [r.GetPointArrayName(i) for i in range(r.GetNumberOfPointArrays())], regions)
        return self.meta

    def _fields_on_disk(self) -> list[str]:
        from aethercfd.case.model import is_time_dir

        base = self.case_root / "processor0" if self.reader.GetCaseType() == 0 else self.case_root
        names: dict[str, None] = {}
        try:
            dirs = [d for d in base.iterdir() if d.is_dir() and is_time_dir(d.name)]
        except OSError:
            return []
        for d in dirs:
            for f in d.iterdir():
                if f.is_file() and f.name not in names and not f.name.startswith("."):
                    try:
                        head = f.read_bytes()[:600]
                    except OSError:
                        continue
                    if b"class" in head and b"vol" in head.split(b"class", 1)[1][:40]:
                        names[f.name] = None
        return list(names)

    def read(self, time: float | None, *, fields: list[str] | None = None, patches: list[str] | None = None,
             cell_to_point: bool = False) -> vtk.vtkMultiBlockDataSet:
        """Read one time step. ``fields`` None = all; ``patches`` None = internal meshes only."""
        r = self.reader
        if not self.meta.patches:
            self.refresh()
        want_fields = set(fields) if fields is not None else None
        if time is None and self.meta.times:
            time = self.meta.times[-1]
        listed = {r.GetCellArrayName(i) for i in range(r.GetNumberOfCellArrays())}
        if want_fields and time is not None and not want_fields <= listed:
            r.UpdateTimeStep(time)  # list the fields of THIS time folder (the reader lists one folder's fields)
            r.UpdateInformation()
        for i in range(r.GetNumberOfCellArrays()):
            n = r.GetCellArrayName(i)
            r.SetCellArrayStatus(n, 1 if want_fields is None or n in want_fields else 0)
        want_patches = set(patches) if patches is not None else set(self.meta.internal_meshes)
        for i in range(r.GetNumberOfPatchArrays()):
            n = r.GetPatchArrayName(i)
            r.SetPatchArrayStatus(n, 1 if n in want_patches else 0)
        r.SetCreateCellToPoint(1 if cell_to_point else 0)
        if time is None and self.meta.times:
            time = self.meta.times[-1]
        if time is not None:
            r.UpdateTimeStep(time)
        r.Update()
        out = r.GetOutput().NewInstance()
        out.ShallowCopy(r.GetOutput())
        return out


def iter_leaves(mb: vtk.vtkDataObject):
    """(path, dataset) for every non-empty leaf of a multiblock tree."""
    if mb is None:
        return
    if not mb.IsA("vtkMultiBlockDataSet"):
        if mb.GetNumberOfCells() > 0:
            yield "", mb
        return
    for i in range(mb.GetNumberOfBlocks()):
        name = mb.GetMetaData(i).Get(vtk.vtkCompositeDataSet.NAME()) if mb.HasMetaData(i) else str(i)
        for sub, ds in iter_leaves(mb.GetBlock(i)):
            yield (f"{name}/{sub}" if sub else name), ds
