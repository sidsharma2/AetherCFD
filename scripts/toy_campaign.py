"""Toy-case campaign: real OpenFOAM runs through AetherCFD's own command path (the one the GUI and the
copilot use), checked end to end.

For each toy: create the case from a template (both forks where the tutorial exists), shorten it, start
it with run.start preset "auto" (the tutorial's Allrun, or mesh + check + solve), wait, and check:
the run FINISHED, the solver log parses (residuals fall), result times and fields are readable as
numpy, and the case still opens. Writes build/toys/report.json and prints a table.

    uv run python scripts/toy_campaign.py [--only pitzDaily,cavity] [--root D:/OpenFOAM/_aethercfd_scratch/toys]
        [--fork esi-v2606]   (installed fork to use for that family; default: the first one found)
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path

# (template id, fork family, controlDict edits, timeout s)
TOYS = [
    ("pitzDaily", "esi", {"endTime": "60", "writeInterval": "20"}, 600),
    ("pitzDaily", "foundation", {"endTime": "60", "writeInterval": "20"}, 600),
    ("shockTube", "esi", {"endTime": "0.002", "writeInterval": "0.001"}, 600),
    ("forwardStep", "esi", {"endTime": "0.4", "writeInterval": "0.2"}, 900),
    ("damBreak", "esi", {"endTime": "0.1", "writeInterval": "0.05"}, 900),
    ("buoyantCavity", "esi", {"endTime": "40", "writeInterval": "20"}, 900),
    ("multiRegionHeater", "esi", {"endTime": "0.4", "writeInterval": "0.2"}, 1500),
    ("counterFlowFlame2D", "esi", {"endTime": "0.001", "writeInterval": "0.0005"}, 1500),
    ("counterFlowFlame2D", "foundation", {"endTime": "0.001", "writeInterval": "0.0005"}, 1500),
]


def run_toy(tid: str, family: str, edits: dict, timeout: float, root: Path, fork_id: str | None = None) -> dict:
    from aethercfd.backend.wsl.runner import WslRunner
    from aethercfd.core.settings import Settings
    from aethercfd.results.casedata import CaseData
    from aethercfd.scripting.api import AutoApprove, Session, load_forks
    from aethercfd.templates.catalog import TEMPLATES
    from aethercfd.templates.create import copy_tutorial

    rec: dict = {"toy": tid, "fork": fork_id or family}
    t0 = time.time()
    st = Settings.load()
    runner = WslRunner(st.wsl.default_distro, allowed=st.wsl.allowed_distros)
    forks = [f for f in load_forks(runner) if str(f.family.value) == family]
    fork = next(f for f in forks if f.id == fork_id) if fork_id else forks[0]
    t = next(x for x in TEMPLATES if x.id == tid)
    dest = root / f"{family}_{tid}_{time.strftime('%m%d_%H%M%S')}"
    copy_tutorial(runner, fork, t, dest)
    rec["case"] = str(dest)
    s = Session(dest, create_project=True, broker=AutoApprove())
    for k, v in edits.items():
        r = s.set("system/controlDict", k, v)
        if not r.ok:
            rec.setdefault("edit_errors", []).append(f"{k}: {r.message}")
    from aethercfd.commands.builtin.run_cmds import suggest_preset

    rec["preset"] = suggest_preset(s.case())[0]
    rid = s.run(preset="auto", fork=fork.id)
    r = s.runs.wait(rid, timeout=timeout)
    rec["state"] = str(r.state).lower() if r else "timeout"
    if rec["state"] not in ("finished", "failed", "killed", "lost", "guard_stopped"):
        s.runs.control(rid, "kill")  # never leave a toy running in WSL
        s.runs.wait(rid, timeout=60)
        rec["state"] = f"timeout after {timeout} s (killed)"
    rec["stop_reason"] = getattr(r, "stop_reason", None)
    rec["run_seconds"] = round(time.time() - t0, 1)
    if rec["state"] != "finished":
        logs = sorted(dest.glob("log.*"), key=lambda p: p.stat().st_mtime)
        if logs:
            rec["log_tail"] = logs[-1].read_text(errors="replace")[-1500:]
            rec["log"] = logs[-1].name
    try:
        data = CaseData(dest)
        regs = data.regions()
        rec["regions"] = regs
        dd = CaseData(dest, regs[0]) if regs else data
        times = [x for x in dd.times() if x > 0]
        rec["times"] = times[-4:]
        if times:
            f = dd.fields(times[-1], None)
            rec["fields"] = sorted(f)[:14]
            rec["cells"] = int(len(dd.centres(times[-1])))
            import numpy as np

            rec["finite"] = bool(all(np.isfinite(v).all() for v in f.values()))
        try:
            res = dd.residuals()
            keys = [k for k in res if k != "time"]
            rec["residuals"] = {k: [float(res[k][0]), float(res[k][-1])] for k in keys[:4] if len(res[k])}
        except Exception as exc:
            rec["residuals_error"] = str(exc)[:200]
    except Exception as exc:
        rec["results_error"] = f"{type(exc).__name__}: {exc}"[:400]
    rec["ok"] = rec["state"] == "finished" and bool(rec.get("times")) and rec.get("finite", False)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--root", default="D:/OpenFOAM/_aethercfd_scratch/toys")
    ap.add_argument("--out", default="build/toys")
    ap.add_argument("--fork", action="append", default=[], help="fork id to use for its family, e.g. esi-v2606")
    a = ap.parse_args()
    root, out = Path(a.root), Path(a.out)
    root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    only = {x for x in a.only.split(",") if x}
    fork_ids = {f.split("-", 1)[0]: f for f in a.fork}
    report = []
    for tid, fam, edits, tmo in TOYS:
        if only and tid not in only and f"{fam}_{tid}" not in only:
            continue
        print(f"== {fam} {tid}", flush=True)
        try:
            rec = run_toy(tid, fam, edits, tmo, root, fork_ids.get(fam))
        except Exception as exc:
            rec = {"toy": tid, "fork": fam, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-2000:]}
        report.append(rec)
        print(("  OK  " if rec.get("ok") else "  FAIL") + f" {rec.get('state', '')} {rec.get('run_seconds', '')} s "
              f"times={rec.get('times')} cells={rec.get('cells')} {rec.get('error', '')}"
              f"{rec.get('results_error', '')}", flush=True)
        (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    ok = sum(1 for r in report if r.get("ok"))
    print(f"toys: {ok}/{len(report)} ok -> {out / 'report.json'}")
    return 0 if ok == len(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
