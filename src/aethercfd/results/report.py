"""The run report: what the run computed, whether it can be trusted, and the evidence, in one folder.

    from aethercfd.results.report import build
    rep = build(r"D:\\OpenFOAM\\pitzDaily")     # -> Report(dir, markdown, data, figures, notes)

    <reports>/<case>-<YYYYmmdd-HHMMSS>/
        report.md       tables, verdicts and figures (Markdown)
        report.json     the same as data (QoI values and verdicts, gates, provenance) for scripts and agents
        qoi_*.pdf/.png  QoI histories, the window the convergence gate judged shaded (your plot_style)
        residuals.*     initial residuals
        field_*.png     p, |U|, T at the latest time, rendered by ParaView (pvbatch) on the real mesh

``<reports>`` is the project's ``local/reports`` folder, beside the case and never inside it (without a
project: Documents/AetherCFD/reports). The verdicts are the guided workflow's own gates (aethercfd.case.gates,
aethercfd.case.qoi, aethercfd.case.gridstudy): the report adds no judgement of its own, so the Workflow panel,
the copilot and the report always agree. Matplotlib and ParaView run in subprocesses. No Qt here; nothing in
the case is changed.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aethercfd.case import gates as G
from aethercfd.case import guided
from aethercfd.case import qoi as Q
from aethercfd.case.checks import facts
from aethercfd.case.model import Case, load_case

FORMAT = 1
MAX_POINTS = 4000
_MARK = {"error": "FAIL", "warn": "WARN"}


@dataclass
class Report:
    dir: Path
    markdown: Path
    data: dict
    figures: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"dir": str(self.dir), "markdown": str(self.markdown), "verdict": self.data.get("verdict"),
                "figures": [p.name for p in self.figures], "notes": self.notes}


def reports_root(case_root: Path) -> Path:
    from aethercfd.project.project import Project

    proj = Project.find(case_root)
    if proj is not None:
        return proj.local / "reports"
    return Path.home() / "Documents" / "AetherCFD" / "reports"


def latest(case_root: str | Path) -> Path | None:
    """The newest report.md of this case, or None."""
    root = reports_root(Path(case_root))
    name = Path(case_root).resolve().name
    found = sorted(root.glob(f"{name}-*/report.md")) if root.is_dir() else []
    return found[-1] if found else None


def mark(g: G.Gate) -> str:
    """PASS, FAIL (error), WARN, or INFO: a gate that passes but explains something (not audited, no telemetry)."""
    if not g.ok:
        return _MARK.get(g.severity, "INFO")
    return "INFO" if g.why or g.how else "PASS"


# ============================================================================ data
def residual_series(case: Case) -> dict[str, Q.Series]:
    """Initial residuals per field: function-object output (survives restarts), else the latest solver log."""
    from aethercfd.runs.datsources import find_residual_dat

    dat = find_residual_dat(case.root)
    if dat is not None and dat.series:
        return dat.series
    log = G.latest_solver_log(case.root)
    if log is None:
        return {}
    from aethercfd.core.textio import decode_bytes
    from aethercfd.runs.logparse import parse_log_text

    _p, steps = parse_log_text(decode_bytes(log.read_bytes()[-64 * 2**20:])[0])
    out: dict[str, Q.Series] = {}
    for st in steps:
        for k, r in st.residuals.items():
            out.setdefault(k, []).append((st.time, r.initial))
    return out


def provenance(case: Case) -> tuple[dict | None, Path | None]:
    """The newest provenance.json of a run of this case (written by the run manager)."""
    from aethercfd.project.project import Project

    proj = Project.find(case.root)
    if proj is None or not proj.runs_dir.is_dir():
        return None, None
    best: tuple[float, Path, dict] | None = None
    for p in proj.runs_dir.glob("*/provenance.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            same = Path(d.get("case", {}).get("root", "")).resolve() == case.root.resolve()
        except (OSError, ValueError):
            continue
        if same and (best is None or p.stat().st_mtime > best[0]):
            best = (p.stat().st_mtime, p, d)
    return (best[2], best[1]) if best else (None, None)


def _prov_summary(d: dict | None) -> dict:
    if not d:
        return {}
    fork = d.get("fork") or {}
    git = (d.get("case") or {}).get("git") or {}
    return {"run": (d.get("run") or {}).get("id"), "captured_at": d.get("captured_at"),
            "aethercfd": d.get("aethercfd"), "fork": fork.get("id"), "fork_version": fork.get("version"),
            "mesh_sha256": ((d.get("inputs") or {}).get("mesh_sha256") or "")[:16],
            "inputs_hashed": len((d.get("inputs") or {}).get("files") or {}),
            "git_head": git.get("head") if isinstance(git, dict) else None,
            "apps": [s.get("app") for s in (d.get("run") or {}).get("steps", [])]}


def verdict(gates: list[G.Gate]) -> tuple[str, list[str]]:
    """'ready' | 'not ready' | 'failed', and the reasons: the run's own gates, nothing else."""
    run = next((g for g in gates if g.id == "run-done"), None)
    if run is not None and run.blocking:
        return "failed", [f"{run.title}: {run.why}"]
    errs = [g for g in gates if g.blocking]
    warns = [g for g in gates if not g.ok and g.severity == "warn"]
    if errs:
        return "not ready", [f"{g.title}: {g.why}" for g in errs] + [g.title for g in warns]
    if warns:
        return "not ready", [g.title for g in warns]
    return "ready", []


# ============================================================================ figures
def _decimate(s: Q.Series) -> Q.Series:
    if len(s) <= MAX_POINTS:
        return s
    step = math.ceil(len(s) / MAX_POINTS)
    return s[::step] + ([s[-1]] if (len(s) - 1) % step else [])


def figure_specs(qp: Q.QoIPlan, series: dict[str, Q.Series], stats: list[Q.QoIStat], residuals: dict[str, Q.Series],
                 steady: bool) -> tuple[list[dict], dict[str, list]]:
    """(figure specs for report_plots, arrays to save in the npz)."""
    info = qp.by_id()
    shown = {st.id: st for st in stats if not Q._negligible(st)}
    arrays: dict[str, list] = {}
    figs: list[dict] = []
    x_label = "Iteration" if steady else "Time (s)"

    def window(ids: list[str]) -> float | None:
        starts = []
        for k in ids:
            st, s = shown.get(k), series.get(k, [])
            if st and st.window and len(s) >= st.window:
                starts.append(s[-(st.window if steady else min(len(s), 2 * st.window))][0])
        return min(starts) if starts else None

    def add(name: str, ids: list[tuple[str, str]], y_label: str, *, logy: bool = False, judged: bool = True,
            src: dict[str, Q.Series] | None = None) -> None:
        src = series if src is None else src
        items = [(k, lab) for k, lab in ids if src.get(k)]
        if not items:
            return
        spec = {"name": name, "x_label": x_label, "y_label": y_label, "logy": logy, "series": [],
                "window": window([k for k, _ in items]) if judged else None}
        for k, lab in items:
            key = f"s{len(arrays) // 2}"
            s = _decimate(src[k])
            arrays[f"{key}_t"] = [t for t, _ in s]
            arrays[f"{key}_v"] = [v for _, v in s]
            spec["series"].append({"key": key, "label": lab})
        figs.append(spec)

    def unit(ids: list[str]) -> str:
        u = next((info[k].unit for k in ids if k in info and info[k].unit), "")
        return f" ({u})" if u else ""

    forces = [k for k in ("force:Fx", "force:Fy", "force:Fz") if k in shown]
    add("qoi_forces", [(k, k.split(":")[1]) for k in forces], "Force on the walls" + unit(forces))
    flows = sorted(k for k in shown if k.startswith("flow:"))
    add("qoi_flows", [(k, k.split(":", 1)[1]) for k in flows], "Flow rate, out +" + unit(flows))
    pk = sorted(k for k in shown if k.startswith(("mean:p:", "mean:p_rgh:"))) + (["dp"] if "dp" in shown else [])
    add("qoi_pressure", [(k, "Δp" if k == "dp" else f"mean {k.split(':')[1]}, {k.split(':')[2]}") for k in pk],
        "Pressure" + unit(pk))
    tk = sorted(k for k in shown if k.startswith("mean:T:"))
    add("qoi_temperature", [(k, f"mean T, {k.split(':')[2]}") for k in tk], "Temperature (K)")
    hk = sorted(k for k in shown if k.startswith("heat:"))
    add("qoi_heat", [(k, k.split(":", 1)[1]) for k in hk], "Heat flow into the fluid (W)")
    other: dict[str, list[str]] = {}
    for k in shown:
        if "/" in k and ":" in k:
            other.setdefault(k.rsplit(":", 1)[0], []).append(k)
    for i, (obj, ks) in enumerate(sorted(other.items())[:6]):
        add(f"qoi_other_{i + 1}", [(k, k.rsplit(":", 1)[1]) for k in sorted(ks)[:6]], obj)
    add("residuals", [(k, k) for k in sorted(residuals)], "Initial residual", logy=True, judged=False,
        src=residuals)
    return figs, arrays


def draw(figs: list[dict], arrays: dict[str, list], out_dir: Path, style: str | None) -> tuple[list[Path], list[str]]:
    import numpy as np

    from aethercfd.core.runtime import module_argv

    if not figs:
        return [], []
    np.savez(out_dir / "series.npz", **{k: np.asarray(v, dtype=float) for k, v in arrays.items()})
    spec = {"npz": str(out_dir / "series.npz"), "out_dir": str(out_dir), "style": style or "", "figures": figs}
    (out_dir / "plots.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    try:
        r = subprocess.run(module_argv("aethercfd.results.report_plots", str(out_dir / "plots.json")),
                           capture_output=True, text=True, timeout=300, creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        return [], ["plots skipped: matplotlib took longer than 300 s"]
    finally:
        (out_dir / "series.npz").unlink(missing_ok=True)
        (out_dir / "plots.json").unlink(missing_ok=True)
    if r.returncode != 0:
        return [], [f"plots failed: {(r.stderr or r.stdout).strip()[-600:]}"]
    try:
        made = json.loads(r.stdout.strip().splitlines()[-1])["figures"]
    except (ValueError, IndexError, KeyError):
        made = []
    return [out_dir / f"{n}.png" for n in made if (out_dir / f"{n}.png").is_file()], []


# ============================================================================ the report
def build(case_root: str | Path, *, settings=None, out_dir: str | Path | None = None, renders: bool = True,
          plots: bool = True) -> Report:
    from aethercfd import __version__
    from aethercfd.case import gridstudy

    case = load_case(Path(case_root))
    if settings is None:
        from aethercfd.core.settings import Settings

        settings = Settings.load()
    state = guided.load_state(case.root)
    answers = state.get("answers", {})
    stages = guided.assess(case, state)
    by_id = {s.id: s for s in stages}
    f = facts(case)
    qp = Q.plan(case, answers, f)
    series = Q.read_series(case.root, qp)
    tol = float(answers.get("qoi_tolerance") or Q.DEFAULT_TOL_PCT)
    _qg, stats = Q.convergence_gate(case, f, qp, series, tol)
    residuals = residual_series(case)
    prov, prov_path = provenance(case)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir) if out_dir else reports_root(case.root) / f"{case.root.name}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    notes = list(qp.notes)
    figures: list[Path] = []
    if plots:
        figs, arrays = figure_specs(qp, series, stats, residuals, f.steady)
        made, n = draw(figs, arrays, out, getattr(settings, "plot_style", ""))
        figures += made
        notes += n
    pictures: list[Path] = []
    if renders and case.latest_result_time:
        from aethercfd.results import report_render as RR

        names = [n for n in ("p", "p_rgh", "U", "T") if n in G.initial_fields(case)]
        if "p" in names and "p_rgh" in names:
            names.remove("p_rgh")
        names += [n for n in G.initial_fields(case) if n.startswith("alpha.")][:1]
        pictures, n = RR.render_fields(case.root, out, RR.field_specs(names, f.compressible),
                                       decomposed=not case.times[1:] and bool(case.processor_times),
                                       two_d=bool(case.regions) and case.regions[0].is_2d, settings=settings)
        figures += pictures
        notes += n
    val = by_id["validation"].gates
    v, reasons = verdict(val)
    data = {
        "format": FORMAT, "created": datetime.now().isoformat(timespec="seconds"), "aethercfd": __version__,
        "case": str(case.root), "solver": case.solver_label, "steady": f.steady, "compressible": f.compressible,
        "turbulence": f.turbulence, "cells": case.n_cells, "latest_time": case.latest_time,
        "reference": G.reference(case, answers).as_dict(), "qoi_tolerance_pct": tol,
        "verdict": v, "verdict_reasons": reasons,
        "qois": [{**(qp.by_id()[st.id].as_dict() if st.id in qp.by_id() else {"id": st.id}), **st.as_dict()}
                 for st in stats if not Q._negligible(st)],
        "stages": [s.as_dict(case.root, detail=False) for s in stages],
        "validation_gates": [g.as_dict() for g in val],
        "grid_study": gridstudy.load_record(case.root),
        "provenance": {**_prov_summary(prov), "path": str(prov_path) if prov_path else None},
        "figures": [p.name for p in figures], "notes": notes,
    }
    (out / "report.json").write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    md = out / "report.md"
    md.write_text(markdown(case, data, by_id, val, stats, qp, figures, pictures), encoding="utf-8")
    return Report(out, md, data, figures, notes)


def _fmt(x: float | None, digits: int = 5) -> str:
    if x is None:
        return "–"
    return f"{x:.{digits}g}"


def markdown(case: Case, data: dict, by_id: dict, val: list[G.Gate], stats: list[Q.QoIStat], qp: Q.QoIPlan,
             figures: list[Path], pictures: list[Path]) -> str:
    ref = data["reference"]
    lines = [f"# Run report: {case.root.name}", "",
             f"{data['created'].replace('T', ' ')} · AetherCFD {data['aethercfd']}"
             + (f" · {data['provenance'].get('fork')}" if data["provenance"].get("fork") else ""), ""]
    word = {"ready": "**Ready to report.** Every validation gate passes.",
            "not ready": "**Not ready to report yet.**", "failed": "**The run failed.**"}[data["verdict"]]
    lines += ["## Verdict", "", word, ""]
    lines += [f"- {r}" for r in data["verdict_reasons"]] + ([""] if data["verdict_reasons"] else [])
    lines += ["| Check | Result | Evidence |", "|---|---|---|"]
    for g in val:
        ev = "; ".join(g.evidence[:2]).replace("|", "/")
        lines.append(f"| {g.title} | {mark(g)} | {ev} |")
    lines += ["", "## Case", "", "| | |", "|---|---|", f"| Folder | `{case.root}` |",
              f"| Solver | {case.solver_label} ({'steady' if data['steady'] else 'transient'}, "
              f"{'compressible' if data['compressible'] else 'incompressible'}, {data['turbulence'] or 'laminar'}) |",
              f"| Mesh | {data['cells']:,} cells |" if data["cells"] else "| Mesh | – |",
              f"| Latest time | {data['latest_time']} |"]
    refs = ", ".join(f"{k} = {_fmt(v, 4)}" for k, v in ref.items() if k != "sources" and v is not None)
    if refs:
        lines.append(f"| Reference | {refs} |")
    lines += ["", "## Quantities of interest", ""]
    shown = [st for st in stats if not Q._negligible(st)]
    if shown:
        what = ("band over the last 10 % of the iterations" if data["steady"] else
                "drift of the mean between the last two quarters")
        lines += [f"Converged when the {what} is at most {data['qoi_tolerance_pct']:g} % of the quantity's scale "
                  "(the largest value of its kind).", "",
                  "| Quantity | Final value | Unit | Change at the end | Converged |", "|---|---|---|---|---|"]
        for st in sorted(shown, key=lambda s: s.id):
            ch = f"{st.change_pct:.2g} %" if st.change_pct is not None else f"{st.samples} samples"
            ok = {True: "yes", False: "**no**", None: "too short"}[st.ok]
            lines.append(f"| {st.title or st.id} | {_fmt(st.final)} | {st.unit} | {ch} | {ok} |")
        lines.append("")
    else:
        lines += ["No integral quantities were monitored. Add them in the guided Run stage ('Add integral monitors') "
                  "and run again.", ""]
    for p in figures:
        if p.name.startswith("qoi_"):
            lines += [f"![{p.stem}]({p.name})", f"[PDF]({p.stem}.pdf)", ""]
    cons = [g for g in val if g.id in ("mass-balance", "energy-balance")]
    if cons:
        lines += ["## Conservation", ""]
        for g in cons:
            lines += [f"**{g.title}** ({mark(g)})", ""] + [f"- {e}" for e in g.evidence[:8]] + [""]
    lines += ["## Residuals", ""]
    rg = next((g for g in val if g.id == "convergence"), None)
    if rg is not None:
        lines += [f"- {e}" for e in rg.evidence[:10]] + [""]
    res_fig = next((p for p in figures if p.stem == "residuals"), None)
    if res_fig:
        lines += [f"![residuals]({res_fig.name})", "[PDF](residuals.pdf)", ""]
    if pictures:
        lines += [f"## Fields at t = {data['latest_time']}", "",
                  "Rendered by ParaView on the solver's own mesh (point values interpolated from the cells).", ""]
        lines += [f"![{p.stem}]({p.name})" for p in pictures] + [""]
    grid = data.get("grid_study")
    if grid and grid.get("results"):
        lines += ["## Grid study", "", "| Quantity | Behaviour | Order p | GCI fine |", "|---|---|---|---|"]
        for r in grid["results"][:12]:
            gci = f"{100 * r['gci_fine']:.3g} %" if r.get("gci_fine") is not None else "–"
            lines.append(f"| {r['quantity']} | {r['convergence']} | {_fmt(r.get('p'), 3)} | {gci} |")
        lines.append("")
    lines += ["## Workflow stages", "", "| Stage | Status | Open issue |", "|---|---|---|"]
    for s in data["stages"]:
        issue = ", ".join(s.get("failing_gates", [])[:2]) or ("" if s["status"] in ("done", "ready", "na") else
                                                              s.get("next_action", "")[:90])
        lines.append(f"| {s['title']} | {s['status']} | {issue} |")
    pv = data["provenance"]
    lines += ["", "## Provenance", ""]
    if pv.get("run"):
        lines += [f"- Run `{pv['run']}`, captured {pv.get('captured_at')}, AetherCFD {pv.get('aethercfd')}",
                  f"- OpenFOAM {pv.get('fork')} ({', '.join(a for a in pv.get('apps') or [] if a)})",
                  (f"- Mesh sha256 `{pv['mesh_sha256']}…`" if pv.get("mesh_sha256") else
                   "- Mesh not hashed (it was generated during the run)")
                  + f", {pv.get('inputs_hashed')} input files hashed"
                  + (f", git `{pv['git_head']}`" if pv.get("git_head") else ""),
                  f"- Full record: `{pv.get('path')}`", ""]
    else:
        lines += ["No provenance record: the case was not run through AetherCFD's run manager.", ""]
    notes = data["notes"] + ["This report checks the numerics (convergence, conservation, mesh, the grid study when "
                             "there is one). It does not compare with measurements; use the Measurements panel for "
                             "that."]
    lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines)


__all__ = ["Report", "build", "latest", "reports_root", "verdict"]
