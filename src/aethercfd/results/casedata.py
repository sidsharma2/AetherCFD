"""CaseData: the open case's results as numpy, for the Python console, scripts and analysis tools (no Qt).

    data = CaseData("path/to/case")
    data.times()                      # result times (reconstructed, or in processor*/ for a decomposed run)
    f = data.fields(names=["U", "p"])  # latest time; cell values in OpenFOAM's cell order
    c = data.centres()                # cell centres (n, 3), same order
    data.residuals()                  # residual history parsed from the newest solver log
    data.mesh(), data.quality()       # polyMesh and checkMesh-exact quality

Decomposed runs are read from the processor folders and put back into the global OpenFOAM cell order with
cellProcAddressing, so arrays from any time line up with ``mesh()`` and with each other.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class CaseData:
    def __init__(self, root: str | Path, region: str = ""):
        self.root = Path(root)
        self.region = region
        self._reader = None
        self._decomposed = None

    # ------------------------------------------------------------------ reading
    def _results(self):
        if self._reader is None:
            from aethercfd.case.model import load_case
            from aethercfd.results.foam_reader import FoamResults

            c = load_case(self.root)
            self._decomposed = bool(c.processor_times) and not c.times[1:]
            self._reader = FoamResults(self.root, decomposed=self._decomposed)
            self._reader.refresh()
        return self._reader

    def times(self) -> list[float]:
        return list(self._results().refresh().times)

    def field_names(self) -> list[str]:
        return list(self._results().meta.cell_arrays)

    def _internal(self, time, names):
        from aethercfd.results.foam_reader import iter_leaves

        r = self._results()
        meshes = [m for m in r.meta.internal_meshes if (m.split("/")[0] == self.region if self.region else
                                                        m.count("/") == 0 or m == "internalMesh")]
        ds = r.read(time, fields=names, patches=meshes or None)
        leaves = [d for _p, d in iter_leaves(ds)]
        if not leaves:
            raise ValueError(f"no internal mesh data at time {time}")
        if len(leaves) == 1:
            return leaves[0]
        import vtk

        app = vtk.vtkAppendFilter()
        for d in leaves:
            app.AddInputData(d)
        app.Update()
        return app.GetOutput()

    def _perm(self, n: int):
        if not self._decomposed:
            return None
        from aethercfd.results.pipeline import global_cell_order

        return global_cell_order(self.root, n)

    def fields(self, time: float | None = None, names: list[str] | None = None) -> dict[str, np.ndarray]:
        """Cell values of the internal mesh at ``time`` (default: latest), in OpenFOAM cell order."""
        from vtk.util import numpy_support as nps

        ds = self._internal(time, names)
        perm = self._perm(ds.GetNumberOfCells())
        out = {}
        cd = ds.GetCellData()
        for i in range(cd.GetNumberOfArrays()):
            name = cd.GetArrayName(i)
            if names is not None and name not in names:
                continue
            a = nps.vtk_to_numpy(cd.GetArray(i)).astype(np.float64)
            if perm is not None:
                g = np.empty_like(a)
                g[perm] = a
                a = g
            out[name] = a
        return out

    def centres(self, time: float | None = None) -> np.ndarray:
        import vtk
        from vtk.util import numpy_support as nps

        ds = self._internal(time, [])
        cc = vtk.vtkCellCenters()
        cc.SetInputData(ds)
        cc.Update()
        c = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData()).astype(np.float64)
        perm = self._perm(len(c))
        if perm is not None:
            g = np.empty_like(c)
            g[perm] = c
            c = g
        return c

    def snapshots(self, name: str, times: list[float] | None = None) -> tuple[list[float], np.ndarray]:
        """(times, array (n_times, n_cells[, n_comp])) of one field over time -- the input of modal analysis."""
        ts = times if times is not None else [t for t in self.times() if t > 0]
        arr = [self.fields(t, [name])[name] for t in ts]
        return ts, np.stack(arr)

    # ------------------------------------------------------------------ other views
    def mesh(self):
        from aethercfd.mesh.polymesh import read_polymesh

        sub = Path("constant") / self.region / "polyMesh" if self.region else Path("constant") / "polyMesh"
        return read_polymesh(self.root / sub)

    def quality(self):
        from aethercfd.mesh.quality import quality

        return quality(self.mesh())

    def residuals(self, log: str | Path | None = None) -> dict[str, np.ndarray]:
        """Initial residual per field over the newest solver log (or ``log``): {"time": ..., "Ux": ..., ...}."""
        from aethercfd.core.textio import decode_bytes
        from aethercfd.runs.logparse import parse_log_text

        if log is None:
            logs = sorted((p for p in self.root.glob("log.*Foam*") if p.is_file()), key=lambda p: p.stat().st_mtime)
            if not logs:
                raise FileNotFoundError("no solver log (log.*Foam*) in the case")
            log = logs[-1]
        _p, steps = parse_log_text(decode_bytes(Path(log).read_bytes())[0])
        out = {"time": np.array([s.time for s in steps])}
        names = sorted({k for s in steps for k in s.residuals})
        for k in names:
            out[k] = np.array([s.residuals[k].initial if k in s.residuals else np.nan for s in steps])
        return out
