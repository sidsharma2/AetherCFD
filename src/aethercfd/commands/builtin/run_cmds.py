"""run.start / run.control: OpenFOAM runs as journalled, approvable commands."""

from __future__ import annotations

import threading
from pathlib import Path

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
from aethercfd.runs.targets import TargetError, find_profile, parse_target

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
    target: str = "local"  # local (WSL) | cluster:<profile name> (ADR 0007)
    # cluster runs only: override the profile's job defaults
    walltime: str | None = None
    nodes: int | None = None
    tasks_per_node: int | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    mem: str | None = None


PRESETS = {"auto": "Automatic (what this case needs)", "solve": "Solve", "check": "Check mesh",
           "mesh-check-solve": "Mesh + check + solve", "mesh": "Mesh + check",
           "allrun": "Tutorial script (./Allrun)"}


def _has_mesh(case) -> bool:
    regs = getattr(case, "regions", None) or []
    if any((r.mesh_dir / "owner").exists() or (r.mesh_dir / "owner.gz").exists() for r in regs):
        return True
    root = Path(case.root)
    return (root / "constant" / "polyMesh" / "owner").exists() or any(
        root.glob("processor*/constant/polyMesh/owner*")) or any(  # processorN/ and collated processorsN/
        root.glob("processor*/constant/*/polyMesh/owner*"))


def suggest_preset(case) -> tuple[str, str]:
    """(preset, why) for a beginner pressing Start: a tutorial that prepares itself with Allrun gets its
    script, a case without a mesh gets mesh + check + solve, a meshed case is solved."""
    root = Path(case.root)
    meshed = _has_mesh(case)
    if (root / "Allrun").is_file() and not meshed:
        return "allrun", "no mesh yet and the case has an Allrun script (the tutorial's own preparation steps)"
    if not meshed:
        if (root / "system" / "blockMeshDict").is_file():
            return "mesh-check-solve", "no mesh yet: blockMesh, checkMesh, then the solver"
        return "allrun" if (root / "Allrun").is_file() else "mesh-check-solve", "no mesh yet"
    return "solve", "the mesh exists"


def _steps(case, p: StartParams) -> list[RunStep]:
    if p.steps:
        return [RunStep(s.app, tuple(s.args), s.kind, s.parallel) for s in p.steps]
    preset = p.preset or "solve"
    if preset == "auto":
        preset = suggest_preset(case)[0]
    if preset == "allrun":
        if not (Path(case.root) / "Allrun").is_file():
            raise CommandError("this case has no Allrun script")
        # bash runs it even when the copy lost its executable bit (Windows drives)
        return [RunStep("bash", ("./Allrun",), StepKind.SCRIPT, log_name="log.Allrun")]
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
        try:
            kind, cluster_name = parse_target(params.target)
        except TargetError as exc:
            plan.preconditions.append(CheckResult(False, str(exc)))
            return plan
        if kind == "cluster":
            return _plan_cluster(ctx, params, plan, case, mgr, cluster_name or "")
        fork_id = params.fork
        if fork_id is None and ctx.project is not None and (cfg := ctx.project.case_config(case.root)):
            fork_id = cfg.fork
        fork = next((f for f in forks if f.id == fork_id), None) if fork_id else case.fork_guess.pick(forks)
        if fork is None:
            plan.preconditions.append(CheckResult(False, f"no installed OpenFOAM fork matches "
                                                         f"{fork_id or case.fork_guess.family}"))
            return plan
        if params.preset == "auto" and not params.steps:
            # decide NOW and bind it into the approved plan (its hash): what runs is what was approved,
            # even if the case changes between planning and approval
            chosen, why_auto = suggest_preset(case)
            params = params.model_copy(update={"preset": chosen})
            plan.params["preset"] = chosen
            plan.consequences.append(f"chosen automatically: {PRESETS[chosen]} ({why_auto})")
        try:
            steps = _steps(case, params)
        except CommandError as exc:
            plan.preconditions.append(CheckResult(False, str(exc)))
            return plan
        active = mgr.active_for_case(case.root)
        if active:
            plan.preconditions.append(CheckResult(False, f"run {active[0].id} is still {active[0].state} on this case"))
        place, why = resolve_placement(case, params.placement, heavy_io=params.heavy_io)
        if any(s.kind is StepKind.SCRIPT for s in steps):
            # a tutorial script builds the mesh, 0/ and processor folders itself: it needs the whole case, so
            # it runs in place (a stage copy would miss 0.orig/ and geometry, and not bring the mesh back)
            if not case.path_safety.safe:
                plan.preconditions.append(CheckResult(False, "the tutorial script must run in the case folder, "
                                                             "but OpenFOAM cannot use this path ("
                                                             + ", ".join(case.path_safety.problems)
                                                             + "): copy the case to a folder without spaces"))
            place, why = Placement.IN_PLACE, "a tutorial script runs in the case folder itself"
            plan.params["placement"] = Placement.IN_PLACE.value
        plan.summary = f"run {' → '.join(s.app for s in steps)} on {case.name} ({fork.label})"
        results = sorted({t for _, t in (case.times + case.processor_times) if t != "0"}, key=float)
        if results and any(s.kind is StepKind.SCRIPT for s in steps):
            plan.risk = Risk.DESTRUCTIVE
            plan.consequences.append(f"the tutorial script usually re-meshes and resets fields; the {len(results)} "
                                     "existing result time folder(s) may be replaced")
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
        if parse_target(p.target)[0] == "cluster":
            return _apply_cluster(ctx, plan, p, case)
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


def _cluster_resources(p: StartParams, profile) -> dict:
    """The job's resources: the run's own values over the profile's defaults (what the plan shows is what runs)."""
    own = {k: getattr(p, k) for k in ("walltime", "nodes", "tasks_per_node", "partition", "account", "qos", "mem")}
    return {k: (v if v not in (None, "") else getattr(profile, k)) for k, v in own.items()}


def _plan_cluster(ctx: Context, params: StartParams, plan: CommandPlan, case, mgr, name: str) -> CommandPlan:
    from aethercfd.core.clusters import check_walltime
    from aethercfd.runs.cluster.scheduler import SchedulerError, get_scheduler
    from aethercfd.runs.cluster.target import cluster_fork

    settings = getattr(ctx, "settings", None)
    try:
        profile = find_profile(getattr(settings, "clusters", []) or [], name)
        sched = get_scheduler(profile.scheduler)
    except (TargetError, SchedulerError) as exc:
        plan.preconditions.append(CheckResult(False, str(exc)))
        return plan
    fork = cluster_fork(profile)
    if not profile.openfoam_version:
        plan.preconditions.append(CheckResult(False, f"cluster profile {name}: set openfoam_version (e.g. v2306 or "
                                                     "11) so the right write-now switches are used"))
    guess = case.fork_guess
    if guess.confidence >= 0.5 and guess.family is not fork.family:
        plan.preconditions.append(CheckResult(False, f"the case looks like {guess.family} OpenFOAM; cluster profile "
                                                     f"{name} loads {fork.label}"))
    if params.preset == "auto" and not params.steps:
        chosen, why_auto = suggest_preset(case)
        params = params.model_copy(update={"preset": chosen})
        plan.params["preset"] = chosen
        plan.consequences.append(f"chosen automatically: {PRESETS[chosen]} ({why_auto})")
    try:
        steps = _steps(case, params)
    except CommandError as exc:
        plan.preconditions.append(CheckResult(False, str(exc)))
        return plan
    if any(s.kind is StepKind.SCRIPT for s in steps):
        plan.preconditions.append(CheckResult(False, "tutorial scripts (./Allrun) do not run on clusters yet: mesh "
                                                     "the case locally first, or use Mesh + check + solve"))
    active = mgr.active_for_case(case.root)
    if active:
        plan.preconditions.append(CheckResult(False, f"run {active[0].id} is still {active[0].state} on this case"))
    if params.walltime:
        try:
            check_walltime(params.walltime)
        except ValueError as exc:
            plan.preconditions.append(CheckResult(False, str(exc)))
    res = _cluster_resources(params, profile)
    plan.summary = (f"submit {' → '.join(s.app for s in steps)} on {case.name} to cluster {profile.name} "
                    f"({fork.label}, {sched.name})")
    job = [f"{params.nprocs} task(s)"]
    job += [f"{res['nodes']} node(s)"] if res["nodes"] else []
    job += [f"walltime {res['walltime']}"]
    job += [f"{k} {res[k]}" for k in ("partition", "account", "qos", "mem") if res[k]]
    plan.consequences.append(f"cluster {profile.name}: {profile.destination}, work dir {profile.work_dir}")
    plan.consequences.append("job: " + ", ".join(job))
    plan.consequences.append("uploads system/, constant/ and the start time (log files are not sent)"
                             + ("; decomposePar runs inside the job" if params.nprocs > 1 else ""))
    plan.consequences.append("results (time folders, postProcessing, logs) download into this folder while the job "
                             "runs and when it ends; system/ and constant/ are never overwritten")
    if params.enable_signals and any(s.kind is StepKind.SOLVER for s in steps):
        lead = profile.write_before_walltime_s
        plan.consequences.append("write-now / stop-and-write through the scheduler (scancel --batch --signal=USR1/"
                                 "USR2)" + (f"; the solver writes and stops {lead} s before the walltime" if lead
                                            else ""))
    results = sorted({t for _, t in (case.times + case.processor_times) if t != "0"}, key=float)
    if results and any(s.app in _MESHERS for s in steps):
        plan.risk = Risk.DESTRUCTIVE
        plan.consequences.append(f"re-meshing replaces constant/polyMesh; the {len(results)} existing result "
                                 "time folder(s) will no longer match the mesh")
    if case.generated_by:
        plan.consequences.append(f"note: case files say '{case.generated_by[:80]}'")
    for s in steps:
        argv = (s.app, *s.args)
        if s.parallel:
            launcher = profile.mpi_launcher
            argv = (launcher, "-n" if launcher == "srun" else "-np", str(params.nprocs), *argv, "-parallel")
        plan.shell_steps.append(ShellStep(fork.id, f"{profile.name}:{profile.work_dir}", argv, s.log))
    plan.params["_resolved"] = {"fork": fork.id, "placement": "cluster", "target": profile.target,
                                "steps": [s.label for s in steps], "resources": res}
    return plan


def _apply_cluster(ctx: Context, plan: CommandPlan, p: StartParams, case) -> CommandResult:
    from aethercfd.runs.cluster.target import cluster_fork

    _, name = parse_target(p.target)
    profile = find_profile(ctx.settings.clusters, name or "")  # type: ignore[attr-defined]
    steps = _steps(case, p)
    mgr = ctx.run_manager  # type: ignore[attr-defined]
    rec, _ = mgr.prepare(case, cluster_fork(profile), steps, nprocs=p.nprocs, enable_signals=p.enable_signals,
                         target=profile.target, resources=plan.params["_resolved"]["resources"])
    start_time = case.latest_time if not any(s.app in _MESHERS for s in steps) else (case.times[0][1]
                                                                                   if case.times else None)
    # upload + submit talk to the cluster (seconds to minutes): never on the caller's (GUI) thread.
    # RunManager.wait() waits for this launch too, so scripts can still `s.runs.wait(run_id)`.
    mgr.launch_in_background(rec, None, start_time=start_time)
    if ctx.project is not None:
        ctx.project.ensure_case(case.root)
    return CommandResult(True, plan, f"run {rec.id}: uploading to cluster {profile.name} and submitting", data=rec.id)


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
        if rec.is_cluster and params.action in ("pause", "resume"):
            plan.preconditions.append(CheckResult(False, "pause/resume is not available for cluster jobs"))
        if rec.is_cluster and params.action in ("terminate", "kill"):
            plan.consequences.append(f"cancels scheduler job {rec.job_id}")
        if params.action in ("terminate", "kill"):
            plan.consequences.append("progress since the last written time step is lost")
        return plan

    def apply(self, ctx: Context, plan: CommandPlan) -> CommandResult:
        ok, msg = ctx.run_manager.control(plan.params["run_id"], plan.params["action"])  # type: ignore[attr-defined]
        return CommandResult(ok, plan, msg or plan.params["action"])


COMMANDS = [RunStart(), RunControl()]

