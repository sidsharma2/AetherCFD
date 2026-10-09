"""CAD intake through FreeCAD (headless FreeCADCmd): STEP / IGES / BREP / FCStd -> a meshing-ready STL.

Every named solid (FCStd objects, STEP product names) becomes one region of an ASCII STL, so snappyHexMesh
turns it into its own patch; ``regions="faces"`` gives one region per CAD face instead (to tag inlets and
outlets afterwards). FreeCAD works in millimetres; ``scale`` (default 0.001) converts to metres.
Tessellation: linear deflection relative to the part size (default 1/1000 of the bounding-box diagonal)
and an angular deflection, so small radii keep enough triangles for curvature-based refinement.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CAD_SUFFIXES = (".step", ".stp", ".iges", ".igs", ".brep", ".brp", ".fcstd")

_SCRIPT = r'''
import json, os, sys
job = json.load(open(os.environ["AETHER_CAD_JOB"], encoding="utf-8"))
import FreeCAD, Part, Mesh, MeshPart
src = job["source"]
shapes = []
if src.lower().endswith(".fcstd"):
    doc = FreeCAD.openDocument(src)
    objs = [o for o in doc.Objects if hasattr(o, "Shape") and o.Shape.Solids and not o.InList]
    shapes = [(o.Label, o.Shape) for o in objs]
else:
    import Import
    doc = FreeCAD.newDocument("aether")
    try:
        Import.insert(src, doc.Name)
        objs = [o for o in doc.Objects if hasattr(o, "Shape") and o.Shape.Solids and not o.InList]
        shapes = [(o.Label, o.Shape) for o in objs]
    except Exception:
        shapes = []
    if not shapes:
        s = Part.Shape(); s.read(src)
        shapes = [("body", s)]
whole = shapes[0][1] if len(shapes) == 1 else Part.makeCompound([s for _, s in shapes])
bb = whole.BoundBox
diag = bb.DiagonalLength
lin = job.get("linear_deflection") or diag / 1000.0
ang = job.get("angular_deflection", 0.5)
regions = []
if job.get("regions") == "faces":
    for i, f in enumerate(whole.Faces):
        regions.append(("face_%d" % i, f))
else:
    prods = job.get("step_products") or []
    if len(shapes) == 1 and prods and len(shapes[0][1].Solids) == len(prods):
        # the headless STEP import merged the parts into one compound: name its solids by the STEP products
        shapes = [(n, sol) for n, sol in zip(prods, shapes[0][1].Solids)]
    seen = {}
    for label, s in shapes:
        for k, solid in enumerate(s.Solids):
            base = "".join(c if (c.isascii() and c.isalnum()) or c == "_" else "_" for c in label) or "body"
            name = base if len(s.Solids) == 1 else "%s_%d" % (base, k)
            n = seen.get(name, 0); seen[name] = n + 1
            regions.append((name if n == 0 else "%s_%d" % (name, n), solid))
scale = job.get("scale", 0.001)
report = {"bbox_mm": [bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax], "linear_deflection_mm": lin,
          "regions": []}
with open(job["out"], "w", encoding="ascii") as fh:
    for name, shp in regions:
        m = MeshPart.meshFromShape(Shape=shp, LinearDeflection=lin, AngularDeflection=ang, Relative=False)
        fh.write("solid %s\n" % name)
        for f in m.Facets:
            nx, ny, nz = f.Normal
            fh.write("  facet normal %g %g %g\n    outer loop\n" % (nx, ny, nz))
            for p in f.Points:
                fh.write("      vertex %.9g %.9g %.9g\n" % (p[0] * scale, p[1] * scale, p[2] * scale))
            fh.write("    endloop\n  endfacet\n")
        fh.write("endsolid %s\n" % name)
        vol = getattr(shp, "Volume", 0.0)
        report["regions"].append({"name": name, "triangles": m.CountFacets, "closed": bool(m.isSolid()),
                                  "volume_m3": vol * scale ** 3, "area_m2": shp.Area * scale ** 2})
json.dump(report, open(job["report"], "w", encoding="utf-8"), indent=1)
'''


@dataclass
class CadResult:
    ok: bool
    stl: Path | None
    regions: list[dict] = field(default_factory=list)
    bbox_m: list[float] = field(default_factory=list)
    log: str = ""
    notes: list[str] = field(default_factory=list)


def find_freecad(setting: str | None = None) -> Path | None:
    """FreeCADCmd.exe: the setting, FREECAD_CMD, or the newest Program Files\\FreeCAD* install."""
    for c in (setting, os.environ.get("FREECAD_CMD")):
        if c and Path(c).is_file():
            return Path(c)
    roots = [Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
             Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"]
    found = []
    for r in roots:
        if r.is_dir():
            found += [p / "bin" / "FreeCADCmd.exe" for p in r.glob("FreeCAD*") if (p / "bin" / "FreeCADCmd.exe").is_file()]
    return sorted(found)[-1] if found else None


def _step_products(src: Path) -> list[str]:
    """Leaf part names of a STEP file (PRODUCT entries minus the assembly roots), in file order."""
    import re

    if src.suffix.lower() not in (".step", ".stp"):
        return []
    try:
        text = src.read_text(encoding="latin-1", errors="replace")
    except OSError:
        return []
    names = re.findall(r"PRODUCT\('([^']*)'", text)
    # assemblies are the 'relating' side of NEXT_ASSEMBLY_USAGE_OCCURRENCE; with one root it is the first product
    n_assemblies = text.count("NEXT_ASSEMBLY_USAGE_OCCURRENCE") and 1
    leaves = names[n_assemblies:] if len(names) > 1 else names
    return [n for n in leaves if n]


def cad_to_stl(source: str | Path, out_stl: str | Path, *, regions: str = "solids", scale: float = 0.001,
               linear_deflection: float | None = None, angular_deflection: float = 0.5,
               freecad: str | Path | None = None, timeout_s: float = 600) -> CadResult:
    src, out = Path(source), Path(out_stl)
    if src.suffix.lower() not in CAD_SUFFIXES:
        return CadResult(False, None, notes=[f"{src.suffix} is not a CAD format FreeCAD imports here "
                                             f"({', '.join(CAD_SUFFIXES)})"])
    if not src.is_file():
        return CadResult(False, None, notes=[f"no such file: {src}"])
    exe = Path(freecad) if freecad else find_freecad()
    if exe is None:
        return CadResult(False, None, notes=["FreeCAD (FreeCADCmd.exe) was not found; install FreeCAD or set its "
                                             "path in settings (freecad_cmd)"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aether-cad-") as td:
        script, job_f, rep = Path(td) / "cad2stl.py", Path(td) / "job.json", Path(td) / "report.json"
        script.write_text(_SCRIPT, encoding="utf-8")
        job_f.write_text(json.dumps({"source": str(src), "out": str(out), "report": str(rep), "regions": regions,
                                     "step_products": _step_products(src),
                                     "scale": scale, "linear_deflection": linear_deflection,
                                     "angular_deflection": angular_deflection}), encoding="utf-8")
        env = {**os.environ, "AETHER_CAD_JOB": str(job_f)}
        try:
            r = subprocess.run([str(exe), str(script)], capture_output=True, text=True, timeout=timeout_s, env=env,
                               creationflags=0x08000000)
        except subprocess.TimeoutExpired:
            return CadResult(False, None, notes=[f"FreeCAD did not finish within {timeout_s:.0f} s"])
        log = (r.stdout or "") + (r.stderr or "")
        if not rep.is_file() or not out.is_file():
            return CadResult(False, None, log=log[-4000:], notes=["FreeCAD could not convert the file; see the log"])
        report = json.loads(rep.read_text(encoding="utf-8"))
    bb = [v * scale for v in report["bbox_mm"]]
    res = CadResult(True, out, report["regions"], bb, log[-2000:])
    if regions == "faces":  # single faces are open by nature: judge the whole surface
        from aethercfd.mesh.geometry import analyse

        if not analyse(out).watertight:
            res.notes.append("the surface is not closed (gaps between CAD faces); snappy needs a watertight "
                             "surface for internal flow")
        res.notes.append("one region per CAD face: rename the inlet/outlet faces (or group them) before meshing")
    else:
        open_ = [g["name"] for g in report["regions"] if not g["closed"]]
        if open_:
            res.notes.append(f"not closed: {', '.join(open_[:8])} (snappy needs watertight surfaces for internal "
                             "flow; check the CAD for gaps)")
    return res
