"""The copilot's tools: what Claude can see and do inside AetherCFD (no Qt here).

Read tools answer from the case on disk. Anything that changes a file or runs OpenFOAM goes through
the command executor as an AGENT actor: it must carry a rationale and a confidence, the user approves
the exact plan (diff, commands, consequences) in the approval dialog, and it is journalled like any
other change (revertable). The same ToolSet serves the GUI copilot panel and the headless
``aethercfd-cli copilot``; GUI-only tools (show a field, open a file) come from optional UI hooks.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from aethercfd.commands.base import Actor, ActorKind, CommandResult
from aethercfd.core.textio import decode_bytes

AGENT = Actor(ActorKind.AGENT, "claude")
MAX_CHARS = 48_000  # per tool result, so one call cannot flood the model's context
_SKIP_DIRS = {".aethercfd", "__pycache__", ".git"}


class ToolError(RuntimeError):
    pass


class UiHooks(Protocol):
    """Implemented by the GUI; absent in headless mode."""

    def show_field(self, field: str, time: str | None, slice_axis: str | None) -> str: ...

    def open_file(self, path: Path, line: int | None) -> str: ...


# ============================================================================ parameter models
class Justified(BaseModel):
    rationale: str = Field(description="Why this is the right change, in one or two sentences. Shown to the user, "
                                       "who approves or declines it.")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1: how sure you are that this change is right")


class NoParams(BaseModel):
    pass


class ListFilesParams(BaseModel):
    subdir: str = Field("", description="Folder relative to the case root ('' = case root)")
    depth: int = Field(2, ge=1, le=6)


class ReadFileParams(BaseModel):
    path: str = Field(description="File path relative to the case root (or absolute inside it)")
    start_line: int = Field(1, ge=1)
    max_lines: int = Field(400, ge=1, le=3000)


class DictGetParams(BaseModel):
    file: str = Field(description="Dictionary file relative to the case root, e.g. system/fvSolution")
    key: str = Field("", description="Keyword path with '/' separators, e.g. 'SIMPLE/nNonOrthogonalCorrectors'; "
                                     "'' lists the top-level keywords")


class DictSetParams(Justified):
    file: str
    key: str = Field(description="Keyword path, e.g. 'relaxationFactors/fields/p'")
    value: str = Field(description="Raw OpenFOAM text for the value, e.g. '0.1', 'uniform (1 0 0)', "
                                   "'bounded Gauss upwind'")
    insert_if_missing: bool = False


class DictInsertParams(Justified):
    file: str
    dict_path: str = Field(description="Sub-dictionary to add the keyword to ('' = top level)")
    key: str
    value: str


class DictRemoveParams(Justified):
    file: str
    key: str


class WriteFileParams(Justified):
    path: str = Field(description="Path relative to the case root; created if missing")
    content: str = Field(description="The complete new file content")


class StepParam(BaseModel):
    app: str = Field(description="OpenFOAM application or utility, e.g. blockMesh, checkMesh, rhoSimpleFoam")
    args: list[str] = Field(default_factory=list, description="Arguments, e.g. ['-allTopology'] (no shell syntax)")


class RunParams(Justified):
    steps: list[StepParam] = Field(default_factory=list, description="Applications to run in order in the case "
                                   "folder (leave empty to use preset)")
    preset: str | None = Field(None, description="Instead of steps: auto (what the case needs: the tutorial's "
                                                 "./Allrun, or blockMesh + checkMesh + solver when there is no mesh "
                                                 "yet, else the solver) | solve | mesh-check-solve | mesh | check | "
                                                 "allrun")
    nprocs: int = Field(1, ge=1, le=64, description="MPI processes for parallel solver steps")
    wait: bool = Field(True, description="Wait for the run to end (up to timeout_min) and return the outcome")
    timeout_min: float = Field(60.0, gt=0, le=24 * 60)


class RunStatusParams(BaseModel):
    run_id: str | None = Field(None, description="Run id; default: the most recent run of this case")


class RunControlParams(Justified):
    run_id: str
    action: str = Field(description="write_now | stop_write | pause | resume | terminate | kill")


class LogParams(BaseModel):
    log: str = Field("latest", description="Log file relative to the case root, or 'latest' (newest solver log)")
    lines: int = Field(80, ge=1, le=2000)
    grep: str | None = Field(None, description="Only lines matching this regular expression")


class ProgressParams(BaseModel):
    log: str = Field("latest", description="Solver log relative to the case root, or 'latest'")


class MeshQualityParams(BaseModel):
    log: str = Field("latest", description="checkMesh log relative to the case root, or 'latest'")


class JournalParams(BaseModel):
    last: int = Field(10, ge=1, le=100)


class ShowFieldParams(BaseModel):
    field: str
    time: str | None = Field(None, description="Time folder name; default latest")
    slice_axis: str | None = Field(None, description="x | y | z for a mid-plane slice, or omit")


class OpenFileParams(BaseModel):
    path: str
    line: int | None = None


# ============================================================================ tool registry
@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    fn: Callable[[ToolContext, Any], Any]
    mutates: bool = False
    gui_only: bool = False

    def input_schema(self) -> dict:
        return self.params.model_json_schema()


class ToolContext:
    """What a tool call can reach: the session (case, project, executor, runs) and UI hooks."""

    def __init__(self, session, *, executor=None, ui: UiHooks | None = None):
        """``executor``: the copilot's own Executor (its broker is the user's approval UI); default: the
        session's."""
        self.session = session
        self.executor = executor or session.executor
        self.ui = ui

    @property
    def root(self) -> Path:
        return Path(self.session.root)

    def path(self, rel: str, *, must_exist: bool = True) -> Path:
        p = Path(rel)
        p = (p if p.is_absolute() else self.root / p).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ToolError(f"{rel} is outside the case folder {self.root}") from None
        if must_exist and not p.exists():
            raise ToolError(f"{rel} does not exist")
        return p

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def execute(self, command: str, params: dict, j: Justified) -> dict:
        res: CommandResult = self.executor.execute(command, params, AGENT, rationale=j.rationale, confidence=j.confidence)
        out: dict[str, Any] = {"ok": res.ok, "message": res.message, "summary": res.plan.summary,
                               "risk": res.plan.risk.value}
        if res.journal_id:
            out["journal_id"] = res.journal_id
        if res.plan.file_changes:
            out["diff"] = _clip("".join(fc.diff for fc in res.plan.file_changes), 6000)
        if res.plan.consequences:
            out["consequences"] = res.plan.consequences
        if res.data is not None:
            out["data"] = res.data
        if not res.ok and res.message.startswith("not approved"):
            out["note"] = "The user declined this plan. Ask what they want instead of retrying the same change."
        return out

    def latest_log(self, *, kind: str = "solver") -> Path | None:
        logs = [p for p in self.root.glob("log.*") if p.is_file()]
        logs_dir = self.root / "logs"
        if logs_dir.is_dir():
            logs += [p for p in logs_dir.glob("log.*") if p.is_file()]
        runs = self.root / ".aethercfd" / "local" / "runs"
        if runs.is_dir():
            logs += [p for p in runs.glob("*/log.*") if p.is_file()]
        if kind == "checkMesh":
            pool = [p for p in logs if p.name.startswith("log.checkMesh")]
        else:
            pool = [p for p in logs if not p.name.startswith(("log.blockMesh", "log.checkMesh", "log.decompose",
                                                              "log.reconstruct", "log.foamDictionary",
                                                              "log.snappyHexMesh", "log.surfaceFeature",
                                                              "log.splitMeshRegions", "log.topoSet"))]
        return max(pool, key=lambda p: p.stat().st_mtime) if pool else None

    def log_path(self, spec: str, *, kind: str = "solver") -> Path:
        if spec in ("", "latest"):
            p = self.latest_log(kind=kind)
            if p is None:
                raise ToolError(f"no {kind} log found in the case")
            return p
        return self.path(spec)


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-(limit - head):]


def _read_text(p: Path, max_bytes: int = 8 * 2**20) -> str:
    with open(p, "rb") as fh:
        data = fh.read(max_bytes)
    return decode_bytes(data)[0]


def _tail_text(p: Path, max_bytes: int = 2 * 2**20) -> str:
    size = p.stat().st_size
    with open(p, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        data = fh.read()
    text = decode_bytes(data)[0]
    return text if size <= max_bytes else text.split("\n", 1)[-1]


# ============================================================================ read tools
def case_overview(ctx: ToolContext, _p: NoParams) -> dict:
    c = ctx.session.case()
    out: dict[str, Any] = {
        "case": str(c.root), "wsl_path": c.wsl_path, "solver": c.solver_label,
        "fork_guess": {"family": c.fork_guess.family, "evidence": list(getattr(c.fork_guess, "evidence", []))[:6]},
        "path_safety": c.path_safety.value if hasattr(c.path_safety, "value") else str(c.path_safety),
        "times": [t for _, t in c.times][-12:], "n_time_dirs": len(c.times),
        "decomposed_processors": c.n_processors, "generated_by": c.generated_by,
        "processor_times": [t for _, t in c.processor_times][-12:], "latest_result_time": c.latest_result_time,
        "regions": [{"name": r.name or "(single region)", "cells": r.n_cells, "is_2d": r.is_2d,
                     "axisymmetric": r.is_axisymmetric,
                     "patches": [{"name": p.name, "type": p.type, "faces": p.n_faces} for p in r.patches]}
                    for r in c.regions],
    }
    zero = c.root / "0"
    if zero.is_dir():
        out["initial_fields"] = sorted(f.name for f in zero.iterdir() if f.is_file())
    logs = sorted((p for p in c.root.glob("log.*") if p.is_file()), key=lambda p: p.stat().st_mtime)
    out["logs"] = [{"file": p.name, "bytes": p.stat().st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))} for p in logs[-10:]]
    runs = getattr(ctx.session, "runs", None)
    if runs is not None:
        recs = [r for r in runs.runs() if Path(r.case_root).resolve() == c.root.resolve()][-5:]
        out["runs"] = [_run_summary(r) for r in recs]
    return out


def list_files(ctx: ToolContext, p: ListFilesParams) -> dict:
    base = ctx.path(p.subdir or ".")
    lines: list[str] = []

    def walk(d: Path, depth: int, indent: str) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name))
        except OSError:
            return
        dirs = [e for e in entries if e.is_dir() and e.name not in _SKIP_DIRS]
        files = [e for e in entries if e.is_file()]
        if len(dirs) > 40:  # e.g. hundreds of time folders
            lines.append(f"{indent}[{len(dirs)} folders: {', '.join(e.name for e in dirs[:5])} ... "
                         f"{', '.join(e.name for e in dirs[-5:])}]")
            dirs = dirs[:3] + dirs[-3:]
        for e in dirs:
            lines.append(f"{indent}{e.name}/")
            if depth > 1:
                walk(e, depth - 1, indent + "  ")
        for e in files[:200]:
            lines.append(f"{indent}{e.name}  ({e.stat().st_size:,} B)")
        if len(files) > 200:
            lines.append(f"{indent}... {len(files) - 200} more files")

    walk(base, p.depth, "")
    return {"folder": ctx.rel(base) or ".", "tree": _clip("\n".join(lines), 20000)}


def read_file(ctx: ToolContext, p: ReadFileParams) -> dict:
    f = ctx.path(p.path)
    if f.is_dir():
        raise ToolError(f"{p.path} is a folder; use list_files")
    from aethercfd.foamdict.tokens import FileFormat, sniff_format

    with open(f, "rb") as fh:
        head = fh.read(4096)
    if sniff_format(decode_bytes(head)[0]) is FileFormat.BINARY and f.stat().st_size > 64_000:
        return {"path": ctx.rel(f), "note": f"binary-format OpenFOAM file ({f.stat().st_size:,} bytes); header only",
                "header": decode_bytes(head)[0][:1500]}
    text = _read_text(f)
    lines = text.split("\n")
    start = p.start_line - 1
    chunk = lines[start:start + p.max_lines]
    body = "\n".join(f"{start + i + 1:6d}| {ln}" for i, ln in enumerate(chunk))
    return {"path": ctx.rel(f), "total_lines": len(lines), "shown": [p.start_line, start + len(chunk)],
            "text": _clip(body)}


def dict_get(ctx: ToolContext, p: DictGetParams) -> dict:
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    f = ctx.path(p.file)
    doc = parse(_read_text(f))
    view = DictView(doc)
    if not p.key:
        return {"file": ctx.rel(f), "keys": view.keys(), "syntax_errors": [d.message for d in doc.diagnostics][:5]}
    lk = view.lookup(p.key)
    if lk is None:
        parent = "/".join(p.key.split("/")[:-1])
        return {"file": ctx.rel(f), "key": p.key, "found": False,
                "siblings": view.keys(parent) if parent and view.dict_at(parent) is not None else view.keys()}
    out: dict[str, Any] = {"file": ctx.rel(f), "key": p.key, "found": True, "provenance": lk.provenance.value,
                           "sources": list(lk.sources)}
    if lk.entry is not None:
        out["line"] = doc.line_col(lk.entry.start)[0]
        if lk.entry.dict is not None:
            out["text"] = _clip(doc.text(lk.entry.start, lk.entry.end), 12000)
            out["keys"] = view.keys(p.key)
        elif lk.entry.value is not None:
            out["value"] = doc.text(lk.entry.value.start, lk.entry.value.end)
    return out


def _run_summary(r) -> dict:
    return {"run_id": r.id, "state": r.state, "steps": [s.get("label") or s.get("app") for s in r.steps],
            "exit_code": r.exit_code, "stop_reason": r.stop_reason, "guard_stop": r.guard_stop,
            "placement": r.placement, "created": r.created, "finished": r.finished_at}


def run_status(ctx: ToolContext, p: RunStatusParams) -> dict:
    runs = getattr(ctx.session, "runs", None)
    if runs is None:
        raise ToolError("no run manager (the WSL backend is not available or the case has no project yet)")
    if p.run_id:
        rec = runs.get(p.run_id)
        if rec is None:
            raise ToolError(f"unknown run {p.run_id}")
    else:
        recs = [r for r in runs.runs() if Path(r.case_root).resolve() == ctx.root.resolve()]
        if not recs:
            return {"runs": []}
        rec = recs[-1]
    out = _run_summary(rec)
    out["logs"] = _step_logs(ctx, rec, lines=25)
    return out


def _step_logs(ctx: ToolContext, rec, *, lines: int) -> dict:
    out = {}
    base = Path(rec.case_root)
    for s in rec.steps:
        name = s.get("log")
        if not name:
            continue
        for cand in (base / name, Path(rec.run_dir) / name):
            if cand.is_file():
                text = _tail_text(cand, 512 * 1024)
                out[name] = "\n".join(text.rstrip("\n").split("\n")[-lines:])
                if note := log_note(name, text):
                    out[name + " (note)"] = note
                break
    return out


_MAPPED_LOOKUP = re.compile(r"failed lookup of (\w+) \(objectRegistry")


def log_note(name: str, text: str) -> str | None:
    """Known OpenFOAM log endings that look like failures but are not what they seem."""
    if "checkMesh" in name and "Checking geometry" in text and (m := _MAPPED_LOOKUP.search(text)):
        failed = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("***"))
        return (f"checkMesh ran all its checks ({failed} '***' failed-check line(s) above). The closing FATAL "
                f"'failed lookup of {m.group(1)}' is OpenFOAM v2306 looking up the neighbour region of a mapped "
                "(multi-region) patch while checking one region of a split mesh: not a mesh defect. Judge the mesh "
                "by the report; checkMesh on the assembled single-region case does not hit it.")
    return None


def log_tail(ctx: ToolContext, p: LogParams) -> dict:
    f = ctx.log_path(p.log)
    text = _tail_text(f, 16 * 2**20 if p.grep else 2 * 2**20)
    lines = text.rstrip("\n").split("\n")
    if p.grep:
        rx = re.compile(p.grep)
        lines = [ln for ln in lines if rx.search(ln)]
    return {"log": ctx.rel(f), "bytes": f.stat().st_size, "lines": _clip("\n".join(lines[-p.lines:]))}


def solver_progress(ctx: ToolContext, p: ProgressParams) -> dict:
    from aethercfd.runs.logparse import parse_log_text

    f = ctx.log_path(p.log)
    parser, steps = parse_log_text(_read_text(f, 64 * 2**20))
    out: dict[str, Any] = {"log": ctx.rel(f), "steps": len(steps)}
    if steps:
        first, last = steps[0], steps[-1]
        out["time"] = {"first": first.time, "last": last.time}
        res = {}
        for name in last.residuals:
            series = [s.residuals[name].initial for s in steps if name in s.residuals]
            res[name] = {"first": series[0], "min": min(series), "last": series[-1],
                         "last_10_trend": "falling" if len(series) > 10 and series[-1] < series[-10] else
                         "rising" if len(series) > 10 and series[-1] > series[-10] * 1.5 else "flat"}
        out["initial_residuals"] = res
        if last.co_max is not None:
            out["courant"] = {"mean": last.co_mean, "max": last.co_max}
        if last.continuity:
            out["continuity_last"] = {"local": last.continuity[0], "global": last.continuity[1]}
        if last.delta_t is not None:
            out["deltaT"] = last.delta_t
        if last.minmax:
            out["min_max_last"] = {k: list(v) for k, v in list(last.minmax.items())[:12]}
    fatal = parser.fatal() if hasattr(parser, "fatal") else []
    if fatal:
        out["fatal"] = [{"line": e.line_no, "text": e.text[:600]} for e in fatal[:3]]
    events = getattr(parser, "events", [])
    bounding = [e for e in events if getattr(e, "kind", "") == "bounding"]
    if bounding:
        out["bounding_warnings"] = {"count": len(bounding), "last": bounding[-1].text[:200]}
    return out


def mesh_quality(ctx: ToolContext, p: MeshQualityParams) -> dict:
    from aethercfd.case.checkmesh import parse_checkmesh

    f = ctx.log_path(p.log, kind="checkMesh")
    r = parse_checkmesh(_read_text(f))
    return {"log": ctx.rel(f), "mesh_ok": r.mesh_ok, "failed_checks": r.failed, "stats": r.stats,
            "cell_types": r.cell_types, "bounding_box": r.bounding_box, "non_ortho_max": r.non_ortho_max,
            "non_ortho_avg": r.non_ortho_avg, "skewness_max": r.skewness_max, "aspect_max": r.aspect_max,
            "min_volume": r.min_volume,
            "checks": [{"name": c.name, "severity": c.severity, "value": c.value, "message": c.message,
                        "advice": c.advice} for c in r.checks],
            "sets_written": [{"kind": s.kind, "name": s.name, "count": s.count, "reason": s.reason} for s in r.sets]}


def journal(ctx: ToolContext, p: JournalParams) -> dict:
    proj = getattr(ctx.session, "project", None)
    if proj is None:
        return {"entries": []}
    out = []
    for e in proj.journal.entries(newest_first=True):
        out.append({k: e.get(k) for k in ("id", "time", "actor", "command", "summary", "ok", "message",
                                         "rationale")})
        if len(out) >= p.last:
            break
    return {"entries": out}


# ============================================================================ change tools (approval)
def dict_set(ctx: ToolContext, p: DictSetParams) -> dict:
    return ctx.execute("dict.set", {"file": str(ctx.path(p.file)), "key": p.key, "value": p.value,
                                    "insert_if_missing": p.insert_if_missing}, p)


def dict_insert(ctx: ToolContext, p: DictInsertParams) -> dict:
    return ctx.execute("dict.insert", {"file": str(ctx.path(p.file)), "dict_path": p.dict_path, "key": p.key,
                                       "value": p.value}, p)


def dict_remove(ctx: ToolContext, p: DictRemoveParams) -> dict:
    return ctx.execute("dict.remove", {"file": str(ctx.path(p.file)), "key": p.key}, p)


def write_file(ctx: ToolContext, p: WriteFileParams) -> dict:
    f = ctx.path(p.path, must_exist=False)
    if f.is_dir():
        raise ToolError(f"{p.path} is a folder")
    return ctx.execute("file.save", {"path": str(f), "text": p.content}, p)


def run_openfoam(ctx: ToolContext, p: RunParams) -> dict:
    if not p.steps and not p.preset:
        raise ToolError("give steps, or preset (e.g. auto)")

    def solver(app: str) -> bool:  # every OpenFOAM solver is <name>Foam (or foamRun / foamMultiRun)
        return app.endswith("Foam") or app in ("foamRun", "foamMultiRun")

    params = {"case": str(ctx.root), "nprocs": p.nprocs}
    if p.steps:
        params["steps"] = [{"app": s.app, "args": s.args, "kind": "solver" if solver(s.app) else "utility",
                            "parallel": p.nprocs > 1 and solver(s.app)} for s in p.steps]
    else:
        params["preset"] = p.preset
    out = ctx.execute("run.start", params, p)
    run_id = out.get("data")
    if not out["ok"] or not run_id or not p.wait:
        return out
    runs = ctx.session.runs
    deadline = time.monotonic() + p.timeout_min * 60
    rec = runs.get(run_id)
    while rec is not None and time.monotonic() < deadline:
        from aethercfd.runs.manager import RunState

        if not RunState(rec.state).active and rec.state != RunState.QUEUED.value:
            break
        runs.wait(run_id, timeout=min(10.0, max(0.1, deadline - time.monotonic())))
        rec = runs.get(run_id)
    if rec is None:
        return {**out, "note": "run record disappeared"}
    out["run"] = _run_summary(rec)
    out["logs"] = _step_logs(ctx, rec, lines=30)
    if rec.state in ("running", "paused", "staging", "launching", "stopping", "syncing"):
        out["note"] = (f"still {rec.state} after {p.timeout_min:g} min; use run_status / solver_progress to follow it, "
                       "or run_control to stop it")
    return out


def run_control(ctx: ToolContext, p: RunControlParams) -> dict:
    return ctx.execute("run.control", {"run_id": p.run_id, "action": p.action}, p)


# ============================================================================ GUI tools
def show_field(ctx: ToolContext, p: ShowFieldParams) -> dict:
    if ctx.ui is None:
        raise ToolError("no GUI attached")
    return {"result": ctx.ui.show_field(p.field, p.time, p.slice_axis)}


def open_file(ctx: ToolContext, p: OpenFileParams) -> dict:
    if ctx.ui is None:
        raise ToolError("no GUI attached")
    return {"result": ctx.ui.open_file(ctx.path(p.path), p.line)}


# ============================================================================ app control (in front of the user)
class AppPanelParams(BaseModel):
    panel: str = Field(description="Panel name or id, fuzzy: e.g. 'Visualization', 'checks', 'residuals', "
                                   "'3D Viewport', 'Mesh Quality', 'Properties', 'Terminal', 'Command Journal'")


class OpenCaseParams(BaseModel):
    path: str = Field(description="Absolute Windows path of an OpenFOAM case folder (contains system/controlDict), "
                                  "e.g. C:/Users/me/OpenFOAM/cavity")


class ShowDiffParams(BaseModel):
    left: str = Field(description="File relative to the case")
    right: str = Field("", description="Second file relative to the case (or leave empty with other_case / "
                                       "before_last_change)")
    other_case: str = Field("", description="Absolute path of another case: compare the same relative file")
    before_last_change: bool = Field(False, description="Compare the file with its content before its last "
                                                        "journalled change")


class PythonParams(BaseModel):
    code: str = Field(description="Python for the console; names: case, data (fields/centres/residuals as numpy), "
                                  "session, do(...), np, plt, show()")


class CompareParams(BaseModel):
    left: str = Field(description="Field for the left view, e.g. T")
    right: str = Field(description="Field for the right view, e.g. TPinn")
    diff: str = Field("", description="Optional third view with a difference field, e.g. TPinnErr")
    time: str | None = Field(None, description="Result time (default latest)")
    slice_axis: str | None = Field(None, description="x, y or z for a mid-plane slice")


class PerspectiveParams(BaseModel):
    name: str = Field(description="setup | meshing | run | post | code")


class SelectFileParams(BaseModel):
    path: str = Field(description="Case file to select in Case Files (details appear in Properties)")


class AddStageParams(BaseModel):
    type: str = Field(description="qcriterion | isosurface | slice | clip | threshold | contour | streamlines | glyphs | "
                                  "schlieren | quality")
    params: dict = Field(default_factory=dict, description=(
        "Stage parameters, e.g. slice {'axis':'z','pos':0.5}; isosurface {'field':'T','value':1500,'color_by':'U'}; "
        "qcriterion {'percentile':97,'color_by':'U','smooth':20,'subdivide':1}; streamlines {'field':'U','seed':"
        "'line','axis':'x','pos':0.05,'n':60}; threshold {'field':'T','lo':1500}; quality {'metric':"
        "'nonOrthogonality','min':60}; schlieren {'source':'rho','axis':'z'}. smooth/subdivide are presentation-only."))
    wait_s: float = Field(60.0, ge=0, le=600, description="Wait for the stage to be computed and report its result")


class RemoveStageParams(BaseModel):
    stage_id: str | None = Field(None, description="Stage id from add_view_stage; omit to remove all stages")


class CameraParams(BaseModel):
    view: str = Field(description="iso | x | y | z | fit | frame (zoom onto a stage)")
    stage_id: str | None = None


class RenderParams(BaseModel):
    width: int = Field(3840, ge=320, le=7680)
    height: int = Field(2160, ge=240, le=4320)
    background: str = Field("gradient", description="gradient | dark | black | white")
    base: str = Field("outline", description="Mesh surfaces: as shown | outline | translucent | hidden")
    frame: bool = Field(True, description="Zoom onto the visualization stages")
    filename: str | None = None


class HighlightParams(BaseModel):
    target: str = Field(description="Panel to point at (fuzzy name), or 'menu', 'status bar', 'window'")
    message: str = Field(description="Short explanation shown in a bubble next to it (1-2 sentences)")
    seconds: float = Field(7.0, ge=1, le=30)


class ActionsParams(BaseModel):
    query: str = Field("", description="Fuzzy search over menu actions; '' lists them")


class TriggerParams(BaseModel):
    action: str = Field(description="Menu action label (fuzzy), e.g. 'New Case from Template', 'Reset Current "
                                    "Perspective', 'Identify Monitors'")


def _ui(ctx: ToolContext):
    if ctx.ui is None:
        raise ToolError("no GUI attached")
    return ctx.ui


def app_overview(ctx: ToolContext, _p: NoParams) -> dict:
    return _ui(ctx).overview()


def app_open_panel(ctx: ToolContext, p: AppPanelParams) -> dict:
    return {"result": _ui(ctx).open_panel(p.panel)}


def app_open_case(ctx: ToolContext, p: OpenCaseParams) -> dict:
    return {"result": _ui(ctx).open_case(p.path)}


def app_show_diff(ctx: ToolContext, p: ShowDiffParams) -> dict:
    return {"result": _ui(ctx).show_diff(p.left, p.right, p.other_case, p.before_last_change)}


def app_python(ctx: ToolContext, p: PythonParams) -> dict:
    return {"result": _ui(ctx).python(p.code)}


def app_compare_fields(ctx: ToolContext, p: CompareParams) -> dict:
    return {"result": _ui(ctx).compare_fields(p.left, p.right, p.diff, p.time, p.slice_axis)}


def app_perspective(ctx: ToolContext, p: PerspectiveParams) -> dict:
    return {"result": _ui(ctx).perspective(p.name)}


def app_select_file(ctx: ToolContext, p: SelectFileParams) -> dict:
    return {"result": _ui(ctx).select_file(p.path)}


def app_add_stage(ctx: ToolContext, p: AddStageParams) -> dict:
    ui = _ui(ctx)
    sid = ui.add_stage(p.type, p.params)
    if not str(sid).startswith("c"):
        raise ToolError(str(sid))
    deadline = time.monotonic() + p.wait_s
    info = None
    while time.monotonic() < deadline:
        info = ui.stage_status(sid)
        if info:
            break
        time.sleep(0.5)
    out = {"stage_id": sid, "computed": bool(info)}
    if info:
        out.update({k: info.get(k) for k in ("label", "cells", "range", "notes", "interpolated")})
    return out


def app_remove_stage(ctx: ToolContext, p: RemoveStageParams) -> dict:
    return {"result": _ui(ctx).remove_stage(p.stage_id)}


def app_camera(ctx: ToolContext, p: CameraParams) -> dict:
    return {"result": _ui(ctx).camera(p.view, p.stage_id)}


def app_render(ctx: ToolContext, p: RenderParams) -> dict:
    return {"image": _ui(ctx).render(p.width, p.height, p.background, p.base, p.frame, p.filename)}


class LookParams(BaseModel):
    look: str = Field("studio", description="studio (AetherCFD's look: smooth shading, outlines, units, depth) | "
                                            "classic (ParaView-style true cell colours)")


class AnimationParams(BaseModel):
    kind: str = Field("turntable", description="turntable (one camera orbit) | time (every saved time)")
    frames: int = Field(72, ge=12, le=360, description="turntable frames")
    fps: int = Field(24, ge=2, le=60)
    format: str = Field("gif", description="gif | webp")


def app_look(ctx: ToolContext, p: LookParams) -> dict:
    return {"result": _ui(ctx).viewport_look(p.look)}


def app_showcase(ctx: ToolContext, p: BaseModel) -> dict:
    return {"result": _ui(ctx).showcase()}


def app_animation(ctx: ToolContext, p: AnimationParams) -> dict:
    return {"result": _ui(ctx).animation(p.kind, p.frames, p.fps, p.format)}


def app_paraview(ctx: ToolContext, p: BaseModel) -> dict:
    return {"result": _ui(ctx).open_paraview()}


def app_highlight(ctx: ToolContext, p: HighlightParams) -> dict:
    return {"result": _ui(ctx).highlight(p.target, p.message, p.seconds)}


def app_actions(ctx: ToolContext, p: ActionsParams) -> dict:
    return {"actions": _ui(ctx).actions(p.query)}


def app_trigger(ctx: ToolContext, p: TriggerParams) -> dict:
    return {"result": _ui(ctx).trigger(p.action)}


# ============================================================================ the set
BASE_TOOLS: list[Tool] = [
    Tool("case_overview", "Summary of the open OpenFOAM case: solver, fork, regions, patches with types and face "
         "counts, cell counts, time folders, initial fields, recent logs and runs. Start here.", NoParams,
         case_overview),
    Tool("list_files", "Folder tree of the case (time folders are summarised).", ListFilesParams, list_files),
    Tool("read_file", "Read a text file in the case with line numbers (dictionaries, fields, logs, scripts). "
         "Binary-format fields return only their header.", ReadFileParams, read_file),
    Tool("dict_get", "Look up a keyword in an OpenFOAM dictionary: its value, line and provenance (literal, "
         "$macro-inherited, #include, #calc/#eval computed). key='' lists the top-level keywords.", DictGetParams,
         dict_get),
    Tool("log_tail", "Last lines of a log (optionally only lines matching a regex).", LogParams, log_tail),
    Tool("solver_progress", "Parsed solver log: iterations, initial residuals per field (first/min/last/trend), "
         "Courant number, continuity errors, bounding warnings, fatal errors.", ProgressParams, solver_progress),
    Tool("mesh_quality", "Parsed checkMesh log: cell counts/types, non-orthogonality, skewness, aspect ratio, "
         "failed checks with advice, cell/face sets written. Run checkMesh with run_openfoam first if needed.",
         MeshQualityParams, mesh_quality),
    Tool("run_status", "State of a run (or the latest run of this case) with the tail of each step's log.",
         RunStatusParams, run_status),
    Tool("journal", "Recent changes made in AetherCFD (who, what, why).", JournalParams, journal),
    Tool("dict_set", "Change one keyword's value in a dictionary with a minimal, comment-preserving edit. "
         "Needs the user's approval.", DictSetParams, dict_set, mutates=True),
    Tool("dict_insert", "Add a keyword to a (sub-)dictionary. Needs the user's approval.", DictInsertParams,
         dict_insert, mutates=True),
    Tool("dict_remove", "Remove a keyword from a dictionary. Needs the user's approval.", DictRemoveParams,
         dict_remove, mutates=True),
    Tool("write_file", "Create a file or replace a file's whole content (the user sees the diff and approves). "
         "Prefer dict_set for single values.", WriteFileParams, write_file, mutates=True),
    Tool("run_openfoam", "Run OpenFOAM applications in the case in WSL with the case's OpenFOAM version (e.g. "
         "blockMesh, checkMesh -allGeometry -allTopology, topoSet, snappyHexMesh -overwrite, rhoSimpleFoam). Needs "
         "approval; re-meshing a case that has results is flagged as destructive. With wait=true returns the outcome "
         "and log tails.", RunParams, run_openfoam, mutates=True),
    Tool("run_control", "Control a running job: write_now, stop_write (write and stop), pause, resume, terminate, "
         "kill. Needs approval.", RunControlParams, run_control, mutates=True),
    Tool("show_field", "Show a field in the AetherCFD 3D viewport for the user (optionally a mid-plane slice).",
         ShowFieldParams, show_field, gui_only=True),
    Tool("open_file", "Open a case file in the AetherCFD editor at a line, for the user to look at.", OpenFileParams,
         open_file, gui_only=True),
    Tool("app_overview", "What the user sees in AetherCFD: perspective, open panels, all available panels, the "
         "viewport's field/time/fields/stages, the current selection.", NoParams, app_overview, gui_only=True),
    Tool("app_open_panel", "Open (or bring forward) a panel by fuzzy name.", AppPanelParams, app_open_panel,
         gui_only=True),
    Tool("app_perspective", "Switch the window layout: setup, meshing, run, post or code.", PerspectiveParams,
         app_perspective, gui_only=True),
    Tool("app_show_diff", "Show the user a side-by-side diff in the Diff panel: two case files, a file vs the same "
         "file in another case, or a file vs its content before its last journalled change.", ShowDiffParams,
         app_show_diff, gui_only=True),
    Tool("app_python", "Put Python code into the user's Python console (shown, not run): teach them how to get "
         "fields, profiles or residuals as numpy themselves.", PythonParams, app_python, gui_only=True),
    Tool("app_compare_fields", "Show fields side by side in linked 3D viewports with one colour range (e.g. CFD T, "
         "PINN TPinn and the difference TPinnErr; or T at two times).", CompareParams, app_compare_fields,
         gui_only=True),
    Tool("app_open_case", "Open another OpenFOAM case in the app when the user asks for one (e.g. a case from the "
         "Start page list or a path they give). Call it last in your turn. The chat then continues by itself in a "
         "new conversation for the opened case, with the user's request handed over, so do NOT tell the user to "
         "ask again or start a new conversation; just say which case you are opening.", OpenCaseParams, app_open_case, gui_only=True),
    Tool("app_select_file", "Select a case file in the Case Files tree; its details show in Properties.",
         SelectFileParams, app_select_file, gui_only=True),
    Tool("add_view_stage", "Add a visualization stage to the 3D viewport the user is looking at (Q-criterion vortices "
         "and isosurfaces on the GPU, slices, clips, thresholds, streamlines, glyphs, schlieren, mesh-quality maps) and "
         "wait for its result.", AddStageParams, app_add_stage, gui_only=True),
    Tool("remove_view_stage", "Remove one visualization stage (or all).", RemoveStageParams, app_remove_stage,
         gui_only=True),
    Tool("viewport_camera", "Move the 3D camera: iso, x, y, z, fit, or frame a stage.", CameraParams, app_camera,
         gui_only=True),
    Tool("render_image", "Render the current 3D view offscreen at up to 8K with anti-aliasing into the case's "
         ".aethercfd/local/renders folder; returns the image path. To SEE what the user sees, render at e.g. "
         "1280x720 and open the returned PNG with the Read tool.", RenderParams, app_render, gui_only=True),
    Tool("viewport_look", "Switch the 3D viewport's look: studio (AetherCFD post-processing: smooth shading, "
         "outline edges, units on the colour bar, depth) or classic (ParaView-style, true cell colours).",
         LookParams, app_look, gui_only=True),
    Tool("viewport_showcase", "One-click best first view of the shown field: Studio look; for 3D cases a mid-plane "
         "slice in a glass shell of the domain with an isometric camera. Use it when the user wants to 'see the "
         "result' or a nice picture.", NoParams, app_showcase, gui_only=True),
    Tool("viewport_animation", "Record a turntable (camera orbit) or time sweep (every saved time) of the 3D view "
         "as an animated GIF/WebP in .aethercfd/local/animations; returns the file (a time sweep finishes later).",
         AnimationParams, app_animation, gui_only=True),
    Tool("open_in_paraview", "Open the case in ParaView (the expert/backup post-processor) on the viewport's field "
         "and time.", NoParams, app_paraview, gui_only=True),
    Tool("point_at", "Point at a panel with a glowing outline and a short bubble message, so the user sees what you "
         "are talking about. Use it when explaining the app or a result.", HighlightParams, app_highlight,
         gui_only=True),
    Tool("app_actions", "List or fuzzy-search the application's menu actions.", ActionsParams, app_actions,
         gui_only=True),
    Tool("app_trigger", "Trigger a menu action by (fuzzy) label, e.g. open the New Case wizard. Dialogs open for the "
         "user; nothing destructive happens without their approval.", TriggerParams, app_trigger, gui_only=True),
]


class ToolSet:
    def __init__(self, ctx: ToolContext, extra: list[Tool] | None = None):
        self.ctx = ctx
        from aethercfd.copilot.analysis_tools import ANALYSIS_TOOLS
        from aethercfd.copilot.mesh_tools import MESH_TOOLS

        tools = [t for t in BASE_TOOLS + MESH_TOOLS + ANALYSIS_TOOLS if not (t.gui_only and ctx.ui is None)]             + list(extra or [])
        self.tools = {t.name: t for t in tools}

    def describe(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema()}
                for t in self.tools.values()]

    def call(self, name: str, arguments: dict | None) -> tuple[bool, str]:
        """(ok, text) -- text is JSON for the model; errors are explanations it can act on."""
        tool = self.tools.get(name)
        if tool is None:
            return False, f"unknown tool {name!r}"
        try:
            params = tool.params.model_validate(arguments or {})
            result = tool.fn(self.ctx, params)
        except ValidationError as exc:
            return False, f"invalid arguments for {name}: {exc}"
        except ToolError as exc:
            return False, str(exc)
        except Exception as exc:  # report, never crash the bridge
            import logging

            logging.getLogger(__name__).exception("copilot tool %s failed", name)
            return False, f"{name} failed: {type(exc).__name__}: {exc}"
        text = result if isinstance(result, str) else json.dumps(result, indent=1, default=str)
        ok = not (isinstance(result, dict) and result.get("ok") is False)
        return ok, _clip(text)
