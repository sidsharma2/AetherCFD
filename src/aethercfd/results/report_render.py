"""Field pictures for the run report, rendered by ParaView's pvbatch on the real mesh (never re-gridded).

A 2-D case (one extent far smaller than the others, or an empty/wedge front-and-back) is seen along its thin
axis; a 3-D case is cut by a plane through the centre, normal to its smallest extent. One PNG per field at the
latest time, white background, a horizontal colour bar with the field's unit. The script is written to the
report folder and run out of process; nothing is written into the case.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

MARK = "@@AETHER_RENDER "

RENDER_SCRIPT = r'''# AetherCFD report: field pictures (generated; safe to delete)
import json
import sys

from paraview.simple import *  # noqa: F403

spec = json.load(open(sys.argv[1], encoding="utf-8"))
r = OpenFOAMReader(FileName=spec["control"])
r.CaseType = "Decomposed Case" if spec["decomposed"] else "Reconstructed Case"
r.UpdatePipelineInformation()
avail = list(r.GetProperty("MeshRegions").Available) if hasattr(r.GetProperty("MeshRegions"), "Available") else []
r.MeshRegions = [m for m in avail if m.endswith("internalMesh")][:1] or ["internalMesh"]
r.UpdatePipelineInformation()
times = list(r.TimestepValues) if r.TimestepValues else [0.0]
t = float(spec["time"]) if spec.get("time") is not None else times[-1]
merged = MergeBlocks(Input=r)
merged.UpdatePipeline(t)
b = merged.GetDataInformation().GetBounds()
ext = [b[1] - b[0], b[3] - b[2], b[5] - b[4]]
c = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
axis = ext.index(min(ext))
two_d = spec["two_d"] or min(ext) < 1e-3 * max(ext)
if two_d:
    src = merged
else:
    src = Slice(Input=merged)
    src.SliceType = "Plane"
    src.SliceType.Origin = c
    src.SliceType.Normal = [1.0 if i == axis else 0.0 for i in range(3)]
    src.UpdatePipeline(t)
view = CreateView("RenderView")
view.ViewSize = spec["size"]
try:
    view.UseColorPaletteForBackground = 0
except Exception:
    pass
view.Background = [1.0, 1.0, 1.0]
view.OrientationAxesVisibility = 0
view.ViewTime = t
n = [1.0 if i == axis else 0.0 for i in range(3)]
g_up = spec.get("up")  # against gravity, when the case has one in the picture plane
if g_up and abs(g_up[axis]) < 0.5 and max(abs(x) for x in g_up) > 0:
    k = max(range(3), key=lambda i: abs(g_up[i]) if i != axis else -1)
    up = [0.0, 0.0, 0.0]
    up[k] = 1.0 if g_up[k] > 0 else -1.0
else:
    up = [0.0, 1.0, 0.0] if axis == 2 else [0.0, 0.0, 1.0]
k_up = max(range(3), key=lambda i: abs(up[i]))
k_right = 3 - axis - k_up
if spec.get("fit_height"):  # a long, flat domain gets a short picture rather than white space
    w = spec["size"][0]
    spec["size"] = [w, int(min(1200, max(520, w * ext[k_up] / max(ext[k_right], 1e-30) * 1.1 / 0.76)))]
    view.ViewSize = spec["size"]
aspect = spec["size"][0] / spec["size"][1]
# parallel projection fitted to the geometry, kept above the colour bar (the lower ~22 % of the picture)
scale = 1.04 * max(ext[k_up] / (2 * 0.76), 1.06 * ext[k_right] / (2 * aspect))
focal = [c[i] - up[i] * 0.22 * scale for i in range(3)]
points = [a.Name for a in src.PointData]
cells = [a.Name for a in src.CellData]
done, missing = [], []
for f in spec["fields"]:
    name = f["name"]
    assoc = "POINTS" if name in points else ("CELLS" if name in cells else None)
    if assoc is None:
        missing.append(name)
        continue
    d = Show(src, view)
    if f.get("magnitude"):
        ColorBy(d, (assoc, name, "Magnitude"))
    else:
        ColorBy(d, (assoc, name))
    lut = GetColorTransferFunction(name)
    for preset in f.get("presets", ["Viridis", "Viridis (matplotlib)"]):  # ParaView 6 renamed the matplotlib maps
        try:
            lut.ApplyPreset(preset, True)
            break
        except RuntimeError:
            continue
    d.RescaleTransferFunctionToDataRange(False, True)
    bar = GetScalarBar(lut, view)
    bar.Title = f["title"]
    bar.ComponentTitle = ""
    bar.TitleColor = [0.0, 0.0, 0.0]
    bar.LabelColor = [0.0, 0.0, 0.0]
    bar.Orientation = "Horizontal"
    bar.WindowLocation = "Lower Center"
    bar.ScalarBarLength = 0.5
    bar.TitleFontSize = 18
    bar.LabelFontSize = 16
    try:
        bar.RangeLabelFormat = "%-#.4g"
    except Exception:
        pass
    d.SetScalarBarVisibility(view, True)
    Render(view)  # the first render after Show resets the camera: set ours after it
    dist = 4.0 * max(ext)
    view.CameraFocalPoint = focal
    view.CameraPosition = [focal[i] + dist * n[i] for i in range(3)]
    view.CameraViewUp = up
    view.CameraParallelProjection = 1
    view.CameraParallelScale = scale
    Render(view)
    out = spec["out_dir"] + "/field_" + f["file"] + ".png"
    SaveScreenshot(out, view, ImageResolution=spec["size"], TransparentBackground=0)
    Hide(src, view)
    d.SetScalarBarVisibility(view, False)
    done.append({"field": name, "png": out})
print("''' + MARK + r'''" + json.dumps({"time": t, "two_d": bool(two_d), "axis": "xyz"[axis], "done": done,
                                       "missing": missing, "arrays": sorted(set(points) | set(cells))}))
'''


def pvbatch_exe(settings=None) -> Path | None:
    from aethercfd.firstrun.detect import find_paraview

    pv = find_paraview(getattr(settings, "paraview_exe", None))
    if pv is None:
        return None
    for name in ("pvbatch.exe", "pvpython.exe"):
        exe = pv.parent / name
        if exe.is_file():
            return exe
    return None


def field_specs(names: list[str], compressible: bool) -> list[dict]:
    """What to draw: pressure (diverging map), |U|, T, a phase fraction, with units in the colour-bar title
    (plain text: ParaView's bar font has no Greek letters and turns [ ] into ( ))."""
    out = []
    for n in names:
        if n in ("p", "p_rgh"):
            title = f"{n} (Pa)" if compressible else f"{n}/rho (m²/s²)"
            out.append({"name": n, "title": title, "file": n, "presets": ["Cool to Warm"]})
        elif n == "U":
            out.append({"name": "U", "title": "|U| (m/s)", "file": "U", "magnitude": True})
        elif n == "T":
            out.append({"name": "T", "title": "T (K)", "file": "T", "presets": ["Inferno", "Inferno (matplotlib)"]})
        elif n.startswith("alpha."):
            out.append({"name": n, "title": f"{n} (-)", "file": n.replace(".", "_"),
                        "presets": ["Blue Orange (divergent)", "Cool to Warm"]})
    return out


def gravity_up(case_root: Path) -> list[float] | None:
    """-g from constant/g (so buoyant flows are drawn upright), or None."""
    from aethercfd.case.gates import _read

    text = _read(Path(case_root) / "constant" / "g") or ""
    m = re.search(r"\bvalue\s+\(\s*(\S+)\s+(\S+)\s+(\S+)\s*\)", text)
    if not m:
        return None
    try:
        g = [float(x) for x in m.groups()]
    except ValueError:
        return None
    return [-x for x in g] if any(g) else None


def render_fields(case_root: Path, out_dir: Path, fields: list[dict], *, time: str | None = None,
                  decomposed: bool = False, two_d: bool = False, settings=None, timeout: float = 600.0,
                  size: tuple[int, int] = (1600, 900), fit_height: bool = True) -> tuple[list[Path], list[str]]:
    """(PNG paths, notes). Notes say why a picture is missing (no ParaView, a field not in the result, ...)."""
    exe = pvbatch_exe(settings)
    if exe is None:
        return [], ["field pictures skipped: ParaView (pvbatch) was not found; install it or set paraview_exe"]
    if not fields:
        return [], ["field pictures skipped: no p, U, T or phase-fraction field in this case"]
    out_dir.mkdir(parents=True, exist_ok=True)
    script = out_dir / "render_fields.py"
    script.write_text(RENDER_SCRIPT, encoding="utf-8")
    spec = {"control": str(Path(case_root) / "system" / "controlDict"), "decomposed": decomposed, "time": time,
            "two_d": two_d, "fields": fields, "out_dir": out_dir.as_posix(), "size": list(size),
            "up": gravity_up(case_root), "fit_height": fit_height}
    spec_path = out_dir / "render_fields.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    try:
        r = subprocess.run([str(exe), str(script), str(spec_path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(out_dir),
                           creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        return [], [f"field pictures skipped: pvbatch took longer than {timeout:g} s"]
    except OSError as exc:
        return [], [f"field pictures skipped: could not start {exe.name}: {exc}"]
    line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith(MARK)), None)
    if line is None:
        tail = (r.stderr or r.stdout).strip()[-600:]
        return [], [f"field pictures failed (pvbatch exit {r.returncode}): {tail}"]
    res = json.loads(line[len(MARK):])
    notes = [f"not in the result at t = {res['time']:g}, not drawn: {', '.join(res['missing'])} (fields there: "
             f"{', '.join(res.get('arrays', [])) or 'none'})"] if res.get("missing") else []
    return [Path(d["png"]) for d in res["done"]], notes
