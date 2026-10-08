"""run.start / run.control: OpenFOAM runs as journalled, approvable commands."""

from __future__ import annotations

import threading

from pydantic import BaseModel, Field

from aethercfd.case.model import load_case
from aethercfd.commands.base import (
    Actor,
    CheckResult,
    Command,
    CommandError,
    CommandPlan,
    CommandResult,
    CommandSpec,
    Risk,
    ShellStep,
)
from aethercfd.commands.executor import Context
from aethercfd.project.project import Placement
from aethercfd.runs.manager import RunState, default_steps
from aethercfd.runs.placement import resolve_placement
from aethercfd.runs.spec import RunStep, StepKind

_MESHERS = {"blockMesh", "snappyHexMesh", "gmshToFoam", "fluentMeshToFoam", "extrudeMesh", "cfMesh",
            "cartesianMesh"}


class StepModel(BaseModel):
    app: str
    args: list[str] = Field(default_factory=list)
    kind: StepKind = StepKind.UTILITY
    parallel: bool = False


class StartParams(BaseModel):
    case: str
    fork: str | None = None  # ForkProfile.id; default: the case's confirmed fork, else the best guess
    preset: str | None = "solve"  # solve | check | mesh-check-solve | mesh (ignored if steps given)
    steps: list[StepModel] | None = None
    nprocs: int = 1
    placement: Placement = Placement.AUTO
    enable_signals: bool = True
    heavy_io: bool = False


def _steps(case, p: StartParams) -> list[RunStep]:
    if p.steps:
        return [RunStep(s.app, tuple(s.args), s.kind, s.parallel) for s in p.steps]
    preset = p.preset or "solve"
    flags = {"solve": (False, False, True), "check": (False, True, False),
             "mesh-check-solve": (True, True, True), "mesh": (True, True, False)}.get(preset)
    if flags is None:
        raise CommandError(f"unknown preset {preset!r}")
    return default_steps(case, mesh=flags[0], check=flags[1], solve=flags[2], nprocs=p.nprocs)


class RunStart(Command):
    spec = CommandSpec("run.start", StartParams, Risk.EXECUTE, "Run OpenFOAM applications on a case")

    def plan(self, ctx: Context, params: StartParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        case = load_case(params.case)
        plan = CommandPlan(self.spec.name, params.model_dump(mode="json"), actor, self.spec.risk,
                           f"run on {case.name}")
        mgr = getattr(ctx, "run_manager", None)
        forks = getattr(ctx, "forks", []) or []
        if mgr is None:
            plan.preconditions.append(CheckResult(False, "no run manager (WSL backend) available"))
            return plan
        fork_id = params.fork
        if fork_id is None and ctx.project is not None and (cfg := ctx.project.case_config(case.root)):
            fork_id = cfg.fork
        fork = next((f for f in forks if f.id == fork_id), None) if fork_id else case.fork_guess.pick(forks)
        if fork is None:
            plan.preconditions.append(CheckResult(False, f"no installed OpenFOAM fork matches "
                                                         f"{fork_id or case.fork_guess.family}"))
            return plan
        try:
            steps = _steps(case, params)
        except CommandError as exc:
            plan.preconditions.append(CheckResult(False, str(exc)))
            return plan
        active = mgr.active_for_case(case.root)
        if active:
            plan.preconditions.append(CheckResult(False, f"run {active[0].id} is still {active[0].state} on this case"))
        place, why = resolve_placement(case, params.placement, heavy_io=params.heavy_io)
        plan.summary = f"run {' → '.join(s.app for s in steps)} on {case.name} ({fork.label})"
        results = [t for _, t in case.times if t != "0"]
        if results and any(s.app in _MESHERS for s in steps):
            plan.risk = Risk.DESTRUCTIVE
            plan.consequences.append(f"re-meshing replaces constant/polyMesh; the {len(results)} existing result "
                                     "time folder(s) will no longer match the mesh")
        plan.consequences.append(f"placement: {place.value} ({why})")
        if place is Placement.STAGED:
            plan.consequences.append("inputs are copied to an ext4 stage; results sync back to this folder "
                                     "(system/ and constant/ are never overwritten)")
        if params.nprocs > 1:
            plan.consequences.append(f"{params.nprocs} MPI processes (shares the WSL VM's memory with other runs)")
        if params.enable_signals and any(s.kind is StepKind.SOLVER for s in steps):
            plan.consequences.append("write-now / stop-and-write controls enabled for the solver")
        if case.generated_by:
            plan.consequences.append(f"note: case files say '{case.generated_by[:80]}'")
        for s in steps:
            plan.shell_steps.append(ShellStep(fork.id, case.wsl_path, (s.app, *s.args), s.log))
        plan.params["_resolved"] = {"fork": fork.id, "placement": place.value, "steps": [s.label for s in steps]}
        return plan

    def apply(self, ctx: Context, plan: CommandPlan) -> CommandResult:
        p = StartParams.model_validate({k: v for k, v in plan.params.items() if not k.startswith("_")})
        case = load_case(p.case)
        forks = getattr(ctx, "forks", []) or []
        fork = next(f for f in forks if f.id == plan.params["_resolved"]["fork"])
        steps = _steps(case, p)
        mgr = ctx.run_manager  # type: ignore[attr-defined]
        rec, spec = mgr.prepare(case, fork, steps, placement=Placement(plan.params["_resolved"]["placement"]),
                                nprocs=p.nprocs, enable_signals=p.enable_signals, heavy_io=p.heavy_io)
        start_time = case.latest_time if not any(s.app in _MESHERS for s in steps) else (case.times[0][1]
                                                                                       if case.times else None)
        mgr.launch(rec, spec, start_time=start_time)
        if RunState(rec.state) is RunState.FAILED:
            return CommandResult(False, plan, rec.stop_reason or "launch failed", data=rec.id)

        def _prov() -> None:
            try:
                from aethercfd.runs.provenance import capture

                capture(rec, mgr.runner, start_time=start_time)
            except Exception:  # provenance must never break a run
                import logging

                logging.getLogger(__name__).exception("provenance capture failed for %s", rec.id)

        threading.Thread(target=_prov, name=f"prov-{rec.id}", daemon=True).start()
        if ctx.project is not None:
            ctx.project.ensure_case(case.root)
        return CommandResult(True, plan, f"run {rec.id} started ({rec.placement})", data=rec.id)


class ControlParams(BaseModel):
    run_id: str
    action: str  # write_now | stop_write | pause | resume | terminate | kill


_ACTION_RISK = {"write_now": Risk.EXECUTE, "stop_write": Risk.EXECUTE, "pause": Risk.EXECUTE,
                "resume": Risk.EXECUTE, "terminate": Risk.DESTRUCTIVE, "kill": Risk.DESTRUCTIVE}


class RunControl(Command):
    spec = CommandSpec("run.control", ControlParams, Risk.EXECUTE,
                       "Control a run: write_now, stop_write, pause, resume, terminate, kill")

    def plan(self, ctx: Context, params: ControlParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        risk = _ACTION_RISK.get(params.action)
        plan = CommandPlan(self.spec.name, params.model_dump(), actor, risk or Risk.EXECUTE,
                           f"{params.action} run {params.run_id}")
        mgr = getattr(ctx, "run_manager", None)
        if risk is None:
            plan.preconditions.append(CheckResult(False, f"unknown action {params.action!r}"))
            return plan
        rec = mgr.get(params.run_id) if mgr else None
        if rec is None:
            plan.preconditions.append(CheckResult(False, f"unknown run {params.run_id}"))
            return plan
        if not RunState(rec.state).active:
            plan.preconditions.append(CheckResult(False, f"run is {rec.state}"))
        if params.action in ("write_now", "stop_write") and not rec.signals_enabled:
            plan.preconditions.append(CheckResult(False, "this run was started without write-now signals"))
        if params.action in ("terminate", "kill"):
            plan.consequences.append("progress since the last written time step is lost")
        return plan

    def apply(self, ctx: Context, plan: CommandPlan) -> CommandResult:
        ok, msg = ctx.run_manager.control(plan.params["run_id"], plan.params["action"])  # type: ignore[attr-defined]
        return CommandResult(ok, plan, msg or plan.params["action"])


COMMANDS = [RunStart(), RunControl()]

