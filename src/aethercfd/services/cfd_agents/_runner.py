"""Runs ONE cfd_agents capability in the cfd_agents' own interpreter and prints its result as JSON.

Executed out of process by aethercfd.services.cfd_agents (``python -I -B _runner.py``); it must not
import aethercfd (a different interpreter and environment). Only read-only, deterministic agent
calls are wired here; nothing writes into an OpenFOAM case:

* murphy  -- failure diagnosis of a solver log (diagnosis_adapter.diagnose; no LLM)
* connie  -- run-health review of postProcessing fieldMinMax/solverInfo telemetry (Connie.watch_health)
* reggie  -- transient numerics design from the mesh non-orthogonality (Reggie.design_transient_numerics)
* darwin  -- RANS-informed mesh refinement plan (build_rans_plan; writes only to a NEW out_dir that
             the adapter places outside the case)
* probe   -- which of the above can be imported here, and why not

Request: JSON on stdin {"op": ..., "root": <dir that contains the cfd_agents package>, "args": {...}}.
Reply: one line ``@@AETHER_RESULT <json>`` on stdout (agents may print other lines before it).
"""

import importlib
import importlib.util
import json
import os
import sys
import traceback
import types

MARK = "@@AETHER_RESULT "


def _import_agent(modname):
    """Import cfd_agents.agents.<modname>; if the package __init__ fails (it imports every agent, Murphy
    and Larry pull in the 'nooa' LLM framework), load the one module under a stub package instead."""
    full = f"cfd_agents.agents.{modname}"
    try:
        return importlib.import_module(full), ""
    except ImportError as exc:
        first = f"{type(exc).__name__}: {exc}"
    for k in [k for k in sys.modules if k == "cfd_agents.agents" or k.startswith("cfd_agents.agents.")]:
        del sys.modules[k]
    import cfd_agents

    pkg_dir = os.path.join(os.path.dirname(cfd_agents.__file__), "agents")
    stub = types.ModuleType("cfd_agents.agents")
    stub.__path__ = [pkg_dir]
    sys.modules["cfd_agents.agents"] = stub
    spec = importlib.util.spec_from_file_location(full, os.path.join(pkg_dir, f"{modname}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod, f"loaded without cfd_agents.agents/__init__ ({first})"


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__") and not isinstance(obj, dict):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def op_probe(args):
    out = {"python": sys.version.split()[0], "executable": sys.executable}
    try:
        import cfd_agents

        out["package"] = os.path.dirname(cfd_agents.__file__)
    except Exception as exc:
        out["package_error"] = f"{type(exc).__name__}: {exc}"
        return out
    checks = {
        "murphy": lambda: (importlib.import_module("cfd_agents.autofoam_adapter.diagnosis_adapter"),
                           importlib.import_module("cfd_agents.contracts")),
        "connie": lambda: (_import_agent("connie"), importlib.import_module("cfd_agents.config.schema")),
        "reggie": lambda: _import_agent("reggie"),
        "darwin": lambda: importlib.import_module("cfd_agents.analysis.rans_mesh_plan"),
    }
    agents = {}
    for name, fn in checks.items():
        try:
            res = fn()
            note = ""
            for r in res if isinstance(res, tuple) else (res,):
                if isinstance(r, tuple) and r[1]:
                    note = r[1]
            agents[name] = {"ok": True, "note": note}
        except Exception as exc:
            agents[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["agents"] = agents
    return out


def op_murphy(args):
    from cfd_agents.autofoam_adapter import diagnosis_adapter
    from cfd_agents.contracts import AutoFOAMRunResult

    with open(args["log"], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if len(text) > 20_000_000:
        text = text[-20_000_000:]
    rr = AutoFOAMRunResult(success=bool(args.get("success", False)), converged=bool(args.get("converged", False)),
                           runtime=0.0, log=text)
    return _dump(diagnosis_adapter.diagnose(rr))


def op_connie(args):
    mod, note = _import_agent("connie")
    from cfd_agents.config.schema import PoliciesConfig

    policies = PoliciesConfig()
    try:
        from cfd_agents.config import load_config

        policies = load_config().policies
    except Exception:
        pass
    rep = _dump(mod.Connie().watch_health(args["postprocessing"], policies=policies))
    if note:
        rep["_loader_note"] = note
    return rep


def op_reggie(args):
    mod, note = _import_agent("reggie")
    rep = _dump(mod.Reggie().design_transient_numerics(
        max_non_orthogonality=float(args["max_non_orthogonality"]), initial_delta_t=float(args["initial_delta_t"]),
        target_courant_max=float(args.get("target_courant_max", 0.5)),
        relax_if_unstable=bool(args.get("relax_if_unstable", False))))
    if note:
        rep["_loader_note"] = note
    return rep


def op_darwin(args):
    from cfd_agents.analysis.rans_mesh_plan import RansMeshPolicy, build_rans_plan

    pol = {k: v for k, v in args.get("policy", {}).items() if v is not None}
    if "axis" in pol:
        pol["axis"] = tuple(pol["axis"])
    policy = RansMeshPolicy(**pol)
    return _dump(build_rans_plan(args["case"], args["time"], args["out_dir"], policy,
                                 export_gmsh_size_field=bool(args.get("gmsh_size_field", False))))


OPS = {"probe": op_probe, "murphy": op_murphy, "connie": op_connie, "reggie": op_reggie, "darwin": op_darwin}


def main():
    req = json.loads(sys.stdin.read() or "{}")
    root = req.get("root")
    if root and root not in sys.path:
        sys.path.insert(0, root)
    op = OPS.get(req.get("op"))
    if op is None:
        res = {"ok": False, "error": f"unknown op {req.get('op')!r}"}
    else:
        try:
            res = {"ok": True, "result": op(req.get("args") or {})}
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-4000:]}
    sys.stdout.write("\n" + MARK + json.dumps(res, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
