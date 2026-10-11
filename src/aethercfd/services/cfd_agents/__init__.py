"""The research group's cfd_agents (Betty, Bill, Bob, Connie, Larry, Murphy, Reggie, Darwin), called OUT
OF PROCESS.

cfd_agents is not imported: it lives in its own environment (Python 3.13, pydantic/numpy/pyyaml, the
AutoFOAM schemas and, for some agents, the 'nooa' LLM framework), patches sys.path when it loads, reads a
gitignored local config and is actively edited. Instead each call runs ``_runner.py`` in the agents'
interpreter with a JSON request and parses one JSON reply. Results are DATA (findings and suggestions);
nothing here writes into a case — a suggestion that would change a dictionary is turned into a normal
``dict.batch`` plan by the caller and goes through the user's approval and the journal.

Which agents are wired, and why the others are not (see CATALOG):

* Murphy  (diagnosis of a solver log)           -- read-only, deterministic: Run / Validation stages
* Connie  (run health from fieldMinMax/solverInfo) -- read-only, deterministic: Validation stage
* Reggie  (transient numerics design)          -- pure function of the mesh quality and Δt: Numerics
* Darwin  (RANS-informed refinement plan)      -- reads the case, writes only to a NEW folder that we
                                                  place outside the case: Mesh stage
* Betty/Bob write case files and run gmshToFoam; Bill always runs blockMesh and deletes time folders;
  Larry needs a local Ollama LLM: not called (AetherCFD's own run manager and editors do those jobs).

No Qt here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(r"D:\Research-Software\projects\repository-capability-lab\agent-architecture")
DEFAULT_PYTHON = Path(r"D:\Research-Software\envs\nooa-pilot-2026-09-01\Scripts\python.exe")
RUNNER = Path(__file__).with_name("_runner.py")
MARK = "@@AETHER_RESULT "
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PROBE_TTL = 600.0
_PROBES: dict[tuple[str, str], tuple[float, Availability]] = {}


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    role: str
    integrated: bool
    stages: tuple[str, ...] = ()
    reason: str = ""  # why it is (not) called from AetherCFD


CATALOG: dict[str, AgentInfo] = {a.id: a for a in [
    AgentInfo("murphy", "Murphy", "Failure diagnosis of a solver log (failure type, category, hints)", True,
              ("run", "validation"), "deterministic diagnosis; its LLM repair proposal is not used"),
    AgentInfo("connie", "Connie", "Run health from fieldMinMax/solverInfo telemetry (healthy/watch/unstable/failed)",
              True, ("validation",), "deterministic growth-ratio and sign checks on postProcessing/*.dat"),
    AgentInfo("reggie", "Reggie", "Transient numerics design: PIMPLE, ddt scheme, relaxation, Δt from the mesh",
              True, ("numerics",), "pure function of mesh non-orthogonality and Δt"),
    AgentInfo("darwin", "Darwin", "RANS-informed shear-layer refinement plan with cost scenarios", True,
              ("mesh",), "reads a converged RANS result (all-tet, ASCII mesh); writes only to a new folder outside "
                         "the case; never refines"),
    AgentInfo("betty", "Betty", "Writes OpenFOAM case dictionaries from AutoFOAM parameters", False, (),
              "writes 0/, constant/, system/ directly: AetherCFD's templates and guided edits do this through "
              "approvable, journalled plans instead"),
    AgentInfo("bill", "Bill", "Runs blockMesh, checkMesh and the solver in WSL", False, (),
              "always runs blockMesh and deletes every time folder except 0 before solving: AetherCFD's run "
              "manager (guards, staging, journal, provenance) runs cases instead"),
    AgentInfo("bob", "Bob", "Generates a gmsh mesh and converts it with gmshToFoam", False, (),
              "writes constant/polyMesh into the case outside the approval path; AetherMesh covers meshing"),
    AgentInfo("larry", "Larry", "Turns a natural-language request into a case specification", False, (),
              "needs a local Ollama LLM (127.0.0.1:11434); the copilot covers natural-language setup"),
]}


@dataclass
class AgentResult:
    agent: str
    ok: bool
    data: Any = None
    error: str = ""
    explanation: str = ""  # human-readable: what it found, or why it could not run
    suggestions: list[dict] = field(default_factory=list)  # {"text", "edits": [{file,key,value}]} (case-relative)
    seconds: float = 0.0
    argv: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {"agent": self.agent, "ok": self.ok, "explanation": self.explanation, "seconds": round(self.seconds, 2)}
        if self.error:
            d["error"] = self.error
        if self.data is not None:
            d["data"] = self.data
        if self.suggestions:
            d["suggestions"] = self.suggestions
        d["note"] = "Suggestions only: any change goes through a dict.batch plan the user approves."
        return d


@dataclass
class Availability:
    ok: bool
    python: str
    root: str
    agents: dict[str, dict] = field(default_factory=dict)  # id -> {"ok", "error"/"note"}
    problems: list[str] = field(default_factory=list)
    python_version: str = ""

    def available(self, agent: str) -> bool:
        return self.ok and bool(self.agents.get(agent, {}).get("ok"))

    def why_not(self, agent: str) -> str:
        if not self.ok:
            return "; ".join(self.problems) or "cfd_agents not available"
        a = self.agents.get(agent)
        if a is None:
            info = CATALOG.get(agent)
            return info.reason if info and not info.integrated else f"unknown agent {agent!r}"
        return explain_import_error(agent, a.get("error", ""))

    def as_dict(self) -> dict:
        return {"ok": self.ok, "python": self.python, "python_version": self.python_version, "root": self.root,
                "problems": self.problems,
                "agents": {aid: {"name": info.name, "role": info.role, "integrated": info.integrated,
                                 "available": self.available(aid) if info.integrated else False,
                                 "why": (info.reason if not info.integrated or self.available(aid)
                                         else self.why_not(aid)),
                                 "stages": list(info.stages)}
                           for aid, info in CATALOG.items()}}


def explain_import_error(agent: str, error: str) -> str:
    if not error:
        return ""
    e = error.lower()
    if "nooa" in e:
        return (f"{CATALOG[agent].name} needs the 'nooa' agent framework in the cfd_agents environment "
                f"({error})")
    if "openfoam_agent" in e or "autofoam" in e:
        return ("cfd_agents.contracts imports AutoFOAM's openfoam_agent: set autofoam_root in "
                f"cfd_agents/config/local.yaml to the AutoFOAM checkout ({error})")
    if "pydantic" in e or "numpy" in e or "yaml" in e:
        return f"a dependency is missing in the cfd_agents environment: {error}"
    if "file_digest" in e:
        return "Darwin needs Python ≥ 3.11 (hashlib.file_digest) in the cfd_agents environment"
    return error


# ============================================================================ the adapter
class CfdAgents:
    def __init__(self, python: str | Path | None = None, root: str | Path | None = None, *, timeout: float = 180.0,
                 work_dir: str | Path | None = None):
        self.python = Path(python or os.environ.get("AETHERCFD_CFD_AGENTS_PYTHON") or DEFAULT_PYTHON)
        self.root = Path(root or os.environ.get("AETHERCFD_CFD_AGENTS_ROOT") or DEFAULT_ROOT)
        self.timeout = timeout
        if work_dir is None:
            from aethercfd.core.settings import cache_dir

            work_dir = cache_dir() / "cfd_agents"
        self.work_dir = Path(work_dir)
        self._probe: Availability | None = None

    @classmethod
    def from_settings(cls, settings=None, **kw) -> CfdAgents:
        if settings is None:
            from aethercfd.core.settings import Settings

            settings = Settings.load()
        return cls(getattr(settings, "cfd_agents_python", None), getattr(settings, "cfd_agents_root", None), **kw)

    # ------------------------------------------------------------------ discovery
    def problems(self) -> list[str]:
        out = []
        if not self.python.is_file():
            out.append(f"the cfd_agents Python interpreter was not found at {self.python} (set 'cfd_agents_python' "
                       "in AetherCFD's settings, or AETHERCFD_CFD_AGENTS_PYTHON)")
        if not (self.root / "cfd_agents" / "__init__.py").is_file():
            out.append(f"no cfd_agents package under {self.root} (set 'cfd_agents_root' to the folder that "
                       "contains cfd_agents/)")
        return out

    def probe(self, *, refresh: bool = False) -> Availability:
        """What can run here. Cached per (interpreter, root) for PROBE_TTL s: the agents' environment takes
        tens of seconds to import, and every panel/copilot call makes a new CfdAgents."""
        key = (str(self.python), str(self.root))
        hit = _PROBES.get(key)
        if not refresh and hit is not None and time.monotonic() - hit[0] < PROBE_TTL:
            self._probe = hit[1]
        if self._probe is not None and not refresh:
            return self._probe
        probs = self.problems()
        av = Availability(not probs, str(self.python), str(self.root), problems=probs)
        if not probs:
            ok, res, err, _s, _argv = self._call("probe", {}, timeout=max(self.timeout, 240))
            if not ok:
                av.ok = False
                av.problems.append(f"the cfd_agents environment could not start: {err}")
            else:
                av.python_version = res.get("python", "")
                if res.get("package_error"):
                    av.ok = False
                    av.problems.append(f"cfd_agents does not import: {res['package_error']}")
                av.agents = res.get("agents", {})
        self._probe = av
        if av.ok or not any("timed out" in p for p in av.problems):  # a slow machine is not a verdict
            _PROBES[key] = (time.monotonic(), av)
        return av

    def ollama_running(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------ process
    def _call(self, op: str, args: dict, *, timeout: float | None = None) -> tuple[bool, Any, str, float, list[str]]:
        argv = [str(self.python), "-I", "-B", str(RUNNER)]
        req = json.dumps({"op": op, "root": str(self.root), "args": args})
        self.work_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "QT_"))}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(argv, input=req, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout or self.timeout, cwd=str(self.work_dir), env=env,
                                  creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            return False, None, f"{self.python} cannot be started", time.perf_counter() - t0, argv
        except subprocess.TimeoutExpired:
            return False, None, f"timed out after {timeout or self.timeout:g} s", time.perf_counter() - t0, argv
        dt = time.perf_counter() - t0
        line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(MARK)), None)
        if line is None:
            tail = (proc.stderr or proc.stdout).strip()[-1500:]
            return False, None, f"no result (exit code {proc.returncode}): {tail}", dt, argv
        try:
            reply = json.loads(line[len(MARK):])
        except ValueError as exc:
            return False, None, f"unreadable result: {exc}", dt, argv
        if not reply.get("ok"):
            return False, None, reply.get("error", "failed"), dt, argv
        return True, reply.get("result"), "", dt, argv

    def _run(self, agent: str, op: str, args: dict) -> AgentResult:
        av = self.probe()
        if not av.available(agent):
            return AgentResult(agent, False, error="unavailable",
                               explanation=f"{CATALOG[agent].name} is not available: {av.why_not(agent)}")
        ok, data, err, dt, argv = self._call(op, args)
        if not ok:
            return AgentResult(agent, False, error=err, explanation=f"{CATALOG[agent].name} could not run: "
                               f"{explain_import_error(agent, err)}", seconds=dt, argv=argv)
        return AgentResult(agent, True, data, seconds=dt, argv=argv)

    # ------------------------------------------------------------------ agents
    def murphy(self, log: str | Path, *, success: bool | None = None, converged: bool | None = None) -> AgentResult:
        """Murphy's verdict depends on whether the run succeeded/converged: by default both are read from
        the log with AetherCFD's own parser (End line, fatal errors, 'SIMPLE solution converged')."""
        log = Path(log)
        if not log.is_file():
            return AgentResult("murphy", False, error="no log", explanation=f"{log} does not exist")
        if success is None or converged is None:
            from aethercfd.case.gates import run_summary

            s = run_summary(log)
            ok_run = s.finished and not s.fatal
            success = ok_run if success is None else success
            converged = (ok_run and (bool(s.converged_msg) or _residuals_low(s))) if converged is None else converged
        r = self._run("murphy", "murphy", {"log": str(log), "success": success, "converged": converged})
        if r.ok and isinstance(r.data, dict):
            d = r.data
            ft, cat = d.get("failure_type", "?"), d.get("category", "?")
            raw = d.get("hints") or []
            hints = ([k.replace("_", " ") if v is True else f"{k.replace('_', ' ')}: {v}" for k, v in raw.items()
                      if v not in (False, None)] if isinstance(raw, dict) else
                     [str(h) for h in raw] if isinstance(raw, list) else [str(raw)])
            r.explanation = (f"Murphy: {ft} ({cat})" + (f": {d.get('message')}" if d.get("message") else "")
                             + ("" if not hints else " — " + "; ".join(str(h) for h in hints[:4])))
            r.suggestions = [{"text": str(h), "edits": []} for h in hints[:6]]
            if success and converged and ft in ("unknown", "none", None):
                r.explanation = ("Murphy found no failure signature: the run finished and converged by AetherCFD's "
                                 f"log parser (Murphy is meant for failed runs; its note: {d.get('message', '')})")
                r.suggestions = []
        return r

    def connie(self, case_root: str | Path) -> AgentResult:
        pp = Path(case_root) / "postProcessing"
        if not any(pp.glob("*/*/fieldMinMax.dat")) and not any(pp.glob("*/*/solverInfo.dat")):
            return AgentResult("connie", False, error="no telemetry",
                               explanation="Connie reads postProcessing/<name>/<time>/fieldMinMax.dat and "
                                           "solverInfo.dat: add the monitoring function objects (Run stage) and "
                                           "run the case first.")
        r = self._run("connie", "connie", {"postprocessing": str(pp)})
        if r.ok and isinstance(r.data, dict):
            q = r.data.get("quantities") or []
            bad = [x for x in q if x.get("status") not in ("healthy", None)]
            r.explanation = (f"Connie: run health {r.data.get('status', '?')} over {r.data.get('samples_evaluated', '?')} "
                             "samples" + ("" if not bad else ": " + "; ".join(
                                 f"{x.get('quantity')} {x.get('status')} ({x.get('reason', '')})" for x in bad[:5])))
            if bad and _steady(case_root):
                r.explanation += (". Note: Connie's growth-ratio thresholds are tuned for transient runs; in a steady "
                                  "(SIMPLE) run the start-up transient alone gives large ratios, so weigh this "
                                  "against the residuals and the fieldMinMax values")
        return r

    def reggie(self, max_non_orthogonality: float, initial_delta_t: float, target_courant_max: float = 0.5,
               *, case_root: str | Path | None = None) -> AgentResult:
        r = self._run("reggie", "reggie", {"max_non_orthogonality": max_non_orthogonality,
                                           "initial_delta_t": initial_delta_t,
                                           "target_courant_max": target_courant_max})
        if r.ok and isinstance(r.data, dict):
            d = r.data
            r.explanation = (f"Reggie: {d.get('solver_name', '?')} with ddt {d.get('ddt_scheme', '?')}, "
                             f"Δt₀ {d.get('initial_delta_t', '?')}, Co ≤ {d.get('target_courant_max', '?')}. "
                             + " ".join(str(d.get(k, "")) for k in ("ddt_scheme_rationale", "pimple_rationale"))[:600])
            r.suggestions = reggie_suggestions(d)
        return r

    def darwin(self, case_root: str | Path, time_name: str, *, diameter_m: float, velocity_jump_m_s: float,
               **policy) -> AgentResult:
        case_root = Path(case_root).resolve()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = (self.work_dir / "darwin" / f"{case_root.name}-{time_name}-{stamp}").resolve()
        if out_dir.is_relative_to(case_root):
            return AgentResult("darwin", False, error="out_dir inside case",
                               explanation="Darwin's output folder must be outside the case")
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        pol = {"diameter_m": diameter_m, "velocity_jump_m_s": velocity_jump_m_s, **policy}
        r = self._run("darwin", "darwin", {"case": str(case_root), "time": time_name, "out_dir": str(out_dir),
                                           "policy": pol})
        if r.ok and isinstance(r.data, dict):
            d = r.data
            sc = d.get("scenarios") or []
            r.explanation = (f"Darwin: {d.get('active_shear_cells', '?')} shear-layer cells of {d.get('n_cells', '?')}; "
                             + "; ".join(f"level {s.get('level_cap')}: {s.get('equivalent_cells')} cells, "
                                         f"~{s.get('estimated_wall_hours')} h" for s in sc[:3])
                             + f". Plan files in {out_dir} (nothing was refined).")
            r.data = {**d, "_out_dir": str(out_dir)}
        elif not r.ok and r.error not in ("unavailable",):
            e = r.error.lower()
            if "tet" in e or "topolog" in e or "ascii" in e or "binary" in e:
                r.explanation = ("Darwin only plans for all-tetrahedral meshes with ASCII fields from a converged "
                                 f"RANS run: {r.error}")
        return r


def _steady(case_root) -> bool:
    try:
        from aethercfd.case.checks import facts
        from aethercfd.case.model import load_case

        return facts(load_case(case_root)).steady
    except Exception:
        return False


def _residuals_low(s) -> bool:
    return bool(s.residuals) and all(last < 1e-3 for _first, last in s.residuals.values())


def reggie_suggestions(d: dict) -> list[dict]:
    """Reggie's design as case-relative dictionary edits (applied only through an approved plan)."""
    edits = []
    if d.get("ddt_scheme"):
        edits.append({"file": "system/fvSchemes", "key": "ddtSchemes/default", "value": str(d["ddt_scheme"])})
    control = {"adjustTimeStep", "maxCo", "maxDeltaT", "maxAlphaCo"}  # controlDict keywords, not PIMPLE ones
    for k, v in (d.get("pimple") or {}).items():
        if isinstance(v, (int, float, str, bool)) and not isinstance(v, dict):
            val = ("yes" if v else "no") if isinstance(v, bool) else str(v)
            edits.append({"file": "system/controlDict" if k in control else "system/fvSolution",
                          "key": k if k in control else f"PIMPLE/{k}", "value": val, "insert_if_missing": True})
    rf = d.get("relaxation_factors") or {}
    flat = {k: v for k, v in rf.items() if not isinstance(v, dict)}
    groups = {g: rf[g] for g in ("fields", "equations") if isinstance(rf.get(g), dict)}
    for k, v in flat.items():  # Reggie returns {p: 0.3, U: 0.7, ...}: pressure-like fields vs equations
        groups.setdefault("fields" if k in ("p", "p_rgh", "rho") else "equations", {})[k] = v
    for group, items in groups.items():
        for k, v in items.items():
            key = f'"{k}"' if any(ch in k for ch in "|.*()") else k
            edits.append({"file": "system/fvSolution", "key": f"relaxationFactors/{group}/{key}", "value": str(v),
                          "insert_if_missing": True})
    if d.get("initial_delta_t"):
        edits.append({"file": "system/controlDict", "key": "deltaT", "value": str(d["initial_delta_t"])})
    if d.get("target_courant_max") and not any(e["key"] == "maxCo" for e in edits):
        edits.append({"file": "system/controlDict", "key": "maxCo", "value": str(d["target_courant_max"]),
                      "insert_if_missing": True})
    return [{"text": "Apply Reggie's transient numerics (PIMPLE, ddt, relaxation, Δt, maxCo)", "edits": edits}] if \
        edits else []


__all__ = ["CATALOG", "AgentInfo", "AgentResult", "Availability", "CfdAgents"]
