"""Kinetic mechanisms: find them (Cantera built-ins, CRECK CHEMKIN sets, YAML files in the user's research
folders), convert CHEMKIN to Cantera YAML once (cached by content hash), and summarise them."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from aethercfd.core.settings import cache_dir

DEFAULT_ROOTS: list[str] = []  # your own mechanism folders go in the mechanism_roots setting
BUILTIN = [("gri30.yaml", "GRI-Mech 3.0 (natural gas, NOx; 53 species)"),
           ("h2o2.yaml", "Hydrogen/oxygen (10 species)"),
           ("air.yaml", "Air dissociation (no carbon)")]


@dataclass
class Mechanism:
    name: str
    path: str  # Cantera-loadable YAML (built-in name or file)
    source: str  # builtin | yaml | chemkin
    note: str = ""
    chemkin: tuple[str, str, str | None] | None = None  # kinetics, thermo, transport for CHEMKIN sets


def discover(roots: list[str] | None = None, max_files: int = 400) -> list[Mechanism]:
    out = [Mechanism(n, n, "builtin", d) for n, d in BUILTIN]
    for r in roots or DEFAULT_ROOTS:
        root = Path(r)
        if not root.exists():
            continue
        n = 0
        for f in root.rglob("*"):
            if n > max_files or ".git" in f.parts:
                continue
            low = f.name.lower()
            if f.suffix.lower() == ".yaml" and f.is_file():
                out.append(Mechanism(f"{f.parent.name}/{f.stem}", str(f), "yaml", str(f.parent)))
                n += 1
            elif low in ("kinetics.chemkin.cki", "chem.dat", "chem.inp") and f.is_file():
                thermo = next((p for p in (f.parent / "thermo.CHEMKIN.CKT", f.parent / "therm.dat",
                                           f.parent / "thermo.dat") if p.exists()), None)
                tran = next((p for p in (f.parent / "TOT2003.TRAN", f.parent / "tran.dat", f.parent / "transport.dat")
                             if p.exists()), None)
                if thermo is not None:
                    out.append(Mechanism(f.parent.name, "", "chemkin", f"CRECK CHEMKIN set in {f.parent}",
                                         (str(f), str(thermo), str(tran) if tran else None)))
                    n += 1
    return out


def ensure_yaml(m: Mechanism) -> str:
    """A Cantera YAML path for the mechanism (CHEMKIN sets are converted once with ck2yaml)."""
    if m.source != "chemkin":
        return m.path
    kin, thermo, tran = m.chemkin  # type: ignore[misc]
    h = hashlib.sha256()
    for p in (kin, thermo, tran):
        if p:
            h.update(Path(p).read_bytes())
    out = cache_dir() / "mechanisms" / f"{m.name}_{h.hexdigest()[:12]}.yaml"
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    from cantera import ck2yaml

    args = [f"--input={kin}", f"--thermo={thermo}", f"--output={out}", "--permissive", "--quiet"]
    if tran:
        args.append(f"--transport={tran}")
    ck2yaml.main(args)
    (out.with_suffix(".json")).write_text(json.dumps({"from": [kin, thermo, tran]}, indent=1), encoding="utf-8")
    return str(out)
