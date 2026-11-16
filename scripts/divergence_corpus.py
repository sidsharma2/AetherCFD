"""Run the divergence guard over a folder of real OpenFOAM solver logs (read-only) and report what it would do.

    python scripts/divergence_corpus.py path/to/cases [--json out.json]

For each solver log: steps parsed, whether the log ends with End or a fatal error, the guard's STOP (if any,
with the time and the last good time) and its warnings. Healthy logs must never STOP (roadmap Q6). Files
are only read, never changed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from aethercfd.runs.guards import DivergenceGuard, Level
from aethercfd.runs.logparse import LogParser

UTILITIES = re.compile(r"^log\.(blockMesh|checkMesh|snappyHexMesh|decomposePar|reconstructPar\w*|foamToVTK|setFields"
                       r"|potentialFoam|surfaceFeature\w*|extrudeMesh|createPatch|topoSet|renumberMesh|mapFields"
                       r"|foamDictionary|postProcess|transformPoints|mirrorMesh|refineMesh|splitMeshRegions"
                       r"|changeDictionary|createBaffles|mergeMeshes|cfMesh|cartesianMesh|gmshToFoam"
                       r"|fluentMeshToFoam|ideasUnvToFoam|foamFormatConvert|subsetMesh|extrudeToRegionMesh)")


def scan(path: Path, guard: DivergenceGuard | None = None) -> dict:
    g = guard or DivergenceGuard()
    p = LogParser()
    stop = None
    warns: list[str] = []
    steps = 0

    def take(recs):
        nonlocal stop, steps
        for r in recs:
            steps += 1
            v = g.feed(r)
            if v.level is Level.STOP and stop is None:
                stop = {"message": v.message, "time": v.detail.get("time"), "last_ok": v.detail.get("last_ok_text")}
            elif v.level is Level.WARN and len(warns) < 20:
                warns.append(v.message)

    with open(path, encoding="utf-8", errors="replace") as f:
        buf: list[str] = []
        for line in f:
            buf.append(line.rstrip("\n"))
            if len(buf) >= 5000:
                take(p.feed(buf))
                buf = []
        take(p.feed(buf))
        last = p.flush()
        if last is not None:
            take([last])
    return {"log": str(path), "app": p.header.get("Exec", ""), "steps": steps, "finished": p.finished,
            "fatal": [e.text for e in p.fatal][:3], "stop": stop, "warnings": warns}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--min-kb", type=int, default=20)
    a = ap.parse_args(argv)
    out = []
    logs = sorted(p for p in a.root.rglob("log.*") if p.is_file() and not UTILITIES.match(p.name)
                  and p.stat().st_size >= a.min_kb * 1024)
    for p in logs:
        try:
            r = scan(p)
        except OSError as exc:
            print(f"skip {p}: {exc}", file=sys.stderr)
            continue
        if r["steps"] == 0:
            continue
        out.append(r)
        tag = "STOP" if r["stop"] else ("warn" if r["warnings"] else "ok  ")
        end = "End" if r["finished"] else ("FATAL" if r["fatal"] else "open")
        print(f"{tag} {end:5} {r['steps']:7d} {p}", flush=True)
        if r["stop"]:
            print(f"      {r['stop']['message']} (last good {r['stop']['last_ok']})", flush=True)
        for w in r["warnings"][:3]:
            print(f"      {w}", flush=True)
    n_stop = sum(1 for r in out if r["stop"])
    print(f"\n{len(out)} solver logs, {n_stop} would stop, {sum(1 for r in out if r['warnings'])} warn")
    if a.json:
        a.json.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
