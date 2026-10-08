"""Write derived cell fields (assimilated, corrections, POD modes...) as OpenFOAM volScalarField files.

The files are plain ASCII with ``calculated`` boundary values (the patch value is the field's mean): they
are for looking at the derived field next to the solution in the viewport, ParaView or a solver's
functionObjects -- not boundary conditions. For a decomposed run the global field is split into every
processor folder with cellProcAddressing, so the viewport reads it like any other field.

``field_files`` returns {path relative to the case: text}; write them with the ``files.write`` command so
the change is journalled and can be reverted.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

_KEEP_TYPES = {"empty", "wedge", "cyclic", "cyclicAMI", "symmetry", "symmetryPlane", "processor",
               "processorCyclic"}


def _patches(mesh_dir: Path) -> list[tuple[str, str]]:
    from aethercfd.case.model import read_patches

    return [(p.name, p.type) for p in read_patches(mesh_dir / "boundary")]


def _dimensions(case: Path, time: str, like: str | None) -> str:
    if like:
        for base in (case / time, case / "processor0" / time):
            f = base / like
            if f.is_file():
                m = re.search(rb"dimensions\s*(\[[^\]]*\])", f.read_bytes()[:4000])
                if m:
                    return m.group(1).decode()
    return "[0 0 0 0 0 0 0]"


def _text(name: str, values: np.ndarray, patches: list[tuple[str, str]], dims: str, location: str) -> str:
    vec = values.ndim == 2
    if vec:
        m = values.mean(axis=0) if len(values) else np.zeros(3)
        mean_txt = f"({m[0]:.9g} {m[1]:.9g} {m[2]:.9g})"
        body = "\n".join(f"({a:.9g} {b:.9g} {c:.9g})" for a, b, c in values)
    else:
        mean_txt = f"{float(np.mean(values)) if values.size else 0.0:.9g}"
        body = "\n".join(f"{v:.9g}" for v in values)
    cls, lst = ("volVectorField", "List<vector>") if vec else ("volScalarField", "List<scalar>")
    head = (f"FoamFile\n{{\n    version     2.0;\n    format      ascii;\n    class       {cls};\n"
            f"    location    \"{location}\";\n    object      {name};\n}}\n"
            "// written by AetherCFD Studio (derived field, not a boundary condition)\n\n")
    bf = []
    for pname, ptype in patches:
        t = ptype if ptype in _KEEP_TYPES else "calculated"
        val = "" if t in ("empty", "wedge", "symmetry", "symmetryPlane", "cyclic", "cyclicAMI") \
            else f" value uniform {mean_txt};"
        bf.append(f"    {pname}\n    {{\n        type {t};{val}\n    }}")
    return (head + f"dimensions      {dims};\n\ninternalField   nonuniform {lst}\n{len(values)}\n(\n{body}\n)\n;\n\n"
            "boundaryField\n{\n" + "\n".join(bf) + "\n}\n")


def field_files(case: str | Path, time: str, fields: dict[str, np.ndarray], *, like: str | None = None,
                region: str = "") -> dict[str, str]:
    """{relative path: file text} for each named global cell array (OpenFOAM cell order)."""
    case = Path(case)
    reg = Path(region) if region else Path()
    procs = sorted((p for p in case.glob("processor*") if p.name[9:].isdigit()), key=lambda p: int(p.name[9:]))
    reconstructed = (case / time).is_dir()
    out: dict[str, str] = {}
    for name, vals in fields.items():
        vals = np.asarray(vals, dtype=np.float64)
        vals = vals.reshape(len(vals), 3) if vals.ndim == 2 else vals.ravel()  # (n, 3) -> volVectorField
        dims = _dimensions(case, time, like)
        if reconstructed or not procs:
            mesh = case / "constant" / reg / "polyMesh"
            out[str(Path(time) / reg / name)] = _text(name, vals, _patches(mesh), dims, f"{time}")
            continue
        from aethercfd.mesh.polymesh import _read_list

        for p in procs:
            pm = p / "constant" / reg / "polyMesh"
            addr = _read_list(pm / "cellProcAddressing", "labels")[0]
            out[str(Path(p.name) / time / reg / name)] = _text(name, vals[addr], _patches(pm), dims, f"{time}")
    return out
