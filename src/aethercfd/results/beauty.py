"""Ray-traced "beauty" renders through ParaView's pvbatch (OptiX path tracer on NVIDIA RTX, OSPRay path
tracer otherwise) -- a separate process, so nothing in AetherCFD links against ParaView.

The viewport exports what is on screen (polydata with its arrays, colour map, range, opacity, camera) and
pvbatch reproduces it with path tracing, soft shadows and denoising at the requested resolution.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PRESETS = {"turbo": "Turbo", "viridis": "Viridis (matplotlib)", "inferno": "Inferno (matplotlib)",
           "plasma": "Plasma (matplotlib)", "coolwarm": "Cool to Warm", "RdBu_r": "Cool to Warm", "jet": "Jet",
           "gray": "Grayscale"}

SCRIPT = r'''
import json, sys
from paraview.simple import *
paraview.simple._DisableFirstRenderCameraReset()  # keep the camera exported from AetherCFD
sc = json.load(open(sys.argv[1], encoding="utf-8"))
w, h = sc["size"]
view = CreateView("RenderView")
view.ViewSize = [w, h]
view.OrientationAxesVisibility = 0
bg = sc["background"]
for prop, val in (("UseColorPaletteForBackground", 0), ("Background", bg[0])):
    try:
        setattr(view, prop, val)
    except Exception:
        pass
if len(bg) > 1:
    for prop, val in (("BackgroundColorMode", "Gradient"), ("Background2", bg[1])):
        try:
            setattr(view, prop, val)
        except Exception:
            pass
# ray-traced views take their background from the "environment" (same colours)
for prop, val in (("UseGradientEnvironmentalBG", 1 if len(bg) > 1 else 0), ("EnvironmentalBG", bg[0]),
                  ("EnvironmentalBG2", bg[-1]), ("BackgroundMode", "Gradient" if len(bg) > 1 else "Single Color")):
    try:
        setattr(view, prop, val)
    except Exception:
        pass
backend_used = "rasterised"
if sc["raytrace"]:
    for be in sc["backends"]:
        try:
            view.EnableRayTracing = 1
            view.BackEnd = be
            view.SamplesPerPixel = sc["spp"]
            view.Denoise = 1
            view.Shadows = 1
            backend_used = be
            break
        except Exception as exc:
            print("backend", be, "unavailable:", exc)
for b in sc["blocks"]:
    r = XMLPolyDataReader(FileName=[b["file"]])
    d = Show(r, view)
    if b.get("outline"):
        # path tracers draw lines as thick tubes: show the walls as faint glass instead
        d.Representation = "Surface"
        d.Opacity = 0.14
        d.Specular = 0.8
    else:
        d.Representation = "Wireframe" if b["wireframe"] else "Surface"
        d.Opacity = b["opacity"]
    d.Specular = b.get("specular", 0.3)
    d.Interpolation = "Gouraud" if b.get("smooth") else "Flat"
    if b["array"]:
        ColorBy(d, ("POINTS" if b["assoc"] == "point" else "CELLS", b["array"]))
        lut = GetColorTransferFunction(b["array"])
        lut.ApplyPreset(b["preset"], True)
        lut.RescaleTransferFunction(b["range"][0], b["range"][1])
        if b.get("bar"):
            d.SetScalarBarVisibility(view, True)
            bar = GetScalarBar(lut, view)
            bar.Title = b.get("title", b["array"])
            bar.ComponentTitle = ""
            bar.TitleFontSize = 22
            bar.LabelFontSize = 18
            bar.Orientation = "Horizontal"
            bar.WindowLocation = "Lower Center"
    else:
        d.ColorArrayName = ["POINTS", ""]  # solid colour (ColorBy(d, None) is rejected by ParaView 6.1)
        d.AmbientColor = b["color"]
        d.DiffuseColor = b["color"]
cam = view.GetActiveCamera()
c = sc["camera"]
cam.SetPosition(*c["position"])
cam.SetFocalPoint(*c["focal_point"])
cam.SetViewUp(*c["view_up"])
cam.SetViewAngle(c["view_angle"])
view.CameraParallelProjection = 1 if c["parallel"] else 0
if c["parallel"]:
    view.CameraParallelScale = c["parallel_scale"]
Render(view)
SaveScreenshot(sc["output"], view, ImageResolution=[w, h], TransparentBackground=0)
print("AETHER_BEAUTY_BACKEND=" + backend_used)
'''


@dataclass
class Block:
    poly: object  # vtkPolyData
    array: str | None
    assoc: str  # point | cell
    range: tuple[float, float] | None
    cmap: str
    opacity: float = 1.0
    wireframe: bool = False
    smooth: bool = False
    color: tuple[float, float, float] = (0.75, 0.77, 0.80)
    bar: bool = False
    outline: bool = False
    title: str = ""


@dataclass
class Scene:
    blocks: list[Block]
    camera: dict
    size: tuple[int, int] = (3840, 2160)
    background: list[tuple[float, float, float]] = field(default_factory=lambda: [(0.03, 0.035, 0.05),
                                                                                 (0.20, 0.22, 0.27)])
    raytrace: bool = True
    spp: int = 32


def find_pvbatch(paraview_exe: str | None = None) -> Path | None:
    cands = []
    if paraview_exe:
        cands.append(Path(paraview_exe).with_name("pvbatch.exe"))
    cands += sorted(Path("C:/Program Files").glob("ParaView */bin/pvbatch.exe"), reverse=True)
    cands += sorted(Path("D:/Research-Software/tools/paraview").glob("*/bin/pvbatch.exe"), reverse=True)
    return next((c for c in cands if c.exists()), None)


def render(scene: Scene, output: str | Path, *, pvbatch: Path, timeout_s: float = 900,
           backends: tuple[str, ...] = ("OptiX pathtracer", "OSPRay pathtracer")) -> dict:
    import vtk

    tmp = Path(tempfile.mkdtemp(prefix="aethercfd-beauty-"))
    blocks = []
    for i, b in enumerate(scene.blocks):
        f = tmp / f"block{i}.vtp"
        w = vtk.vtkXMLPolyDataWriter()
        w.SetFileName(str(f))
        w.SetInputData(b.poly)
        w.SetDataModeToAppended()
        w.SetCompressorTypeToZLib()
        w.Write()
        blocks.append({"file": str(f), "array": b.array, "assoc": b.assoc,
                       "range": list(b.range) if b.range else None, "preset": PRESETS.get(b.cmap, "Turbo"),
                       "opacity": b.opacity, "wireframe": b.wireframe, "smooth": b.smooth, "color": list(b.color),
                       "bar": b.bar, "title": b.title, "outline": b.outline, "specular": 0.4 if b.smooth else 0.1})
    spec = {"size": list(scene.size), "background": [list(c) for c in scene.background], "raytrace": scene.raytrace,
            "spp": scene.spp, "backends": list(backends), "blocks": blocks, "camera": scene.camera,
            "output": str(output)}
    (tmp / "scene.json").write_text(json.dumps(spec), encoding="utf-8")
    (tmp / "render.py").write_text(SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["SHIM_MCCOMPAT"] = "0x800000001"  # render on the discrete NVIDIA GPU (Optimus laptops)
    r = subprocess.run([str(pvbatch), str(tmp / "render.py"), str(tmp / "scene.json")], capture_output=True,
                       text=True, timeout=timeout_s, env=env, creationflags=0x08000000 if os.name == "nt" else 0)
    backend = next((ln.split("=", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("AETHER_BEAUTY_BACKEND=")),
                   None)
    ok = r.returncode == 0 and Path(output).exists()
    return {"ok": ok, "backend": backend, "output": str(output), "log": (r.stdout + r.stderr)[-4000:],
            "workdir": str(tmp)}
