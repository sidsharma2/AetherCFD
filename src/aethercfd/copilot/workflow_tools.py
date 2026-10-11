"""Copilot tools for the guided workflow and the cfd_agents: the AI drives the same staged path as the user.

The copilot reads the workflow state (every stage's status, gates, next action and questions), answers a
stage's physical questions (which become ONE dict.batch plan the user approves, journalled like any
change), applies a gate's one-click fix, marks a stage done (refused while its gates fail, exactly as in
the GUI), and asks the group's cfd_agents (Murphy, Connie, Reggie, Darwin) for advice. Agent results are
suggestions; turning one into a change goes through ``dict_batch`` and the user's approval.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from aethercfd.copilot.tools import Justified, NoParams, Tool, ToolContext, ToolError

_STAGES = ("geometry | domain | materials | physics | mesh | models | bc | numerics | run | validation | post | ml")


class StageParams(BaseModel):
    stage: str = Field(description=f"Stage id: {_STAGES}")


class AnswerParams(Justified):
    stage: str = Field(description=f"Stage id: {_STAGES}")
    answers: dict[str, Any] = Field(description="Question id -> answer, using the ids and choice values that "
                                                "workflow_stage lists (e.g. {'turbulence': 'kOmegaSST'}, "
                                                "{'inlet_U:inlet': 12.5, 'turb_intensity': 5})")


class FixParams(Justified):
    stage: str
    gate: str = Field(description="Gate id from workflow_stage (e.g. 'p-reference', 'coverage', 'check:central-U')")


class MarkParams(Justified):
    stage: str
    done: bool = True
    note: str = Field("", description="Optional note stored with the mark (e.g. which warnings were accepted)")


class BatchEditParam(BaseModel):
    file: str = Field(description="Dictionary relative to the case, e.g. system/fvSolution")
    key: str = Field(description="Keyword path, e.g. 'PIMPLE/nOuterCorrectors' or 'boundaryField/inlet/value'")
    value: str | None = Field(None, description="Raw OpenFOAM text; null removes the keyword")
    insert_if_missing: bool = True


class DictBatchParams(Justified):
    edits: list[BatchEditParam] = Field(description="Edits applied as ONE plan (one approval, one journal entry)")
    summary: str = ""


class AgentParams(BaseModel):
    agent: str = Field(description="murphy (diagnose a solver log) | connie (run health from fieldMinMax/solverInfo) "
                                   "| reggie (transient numerics design) | darwin (RANS-informed mesh refinement plan)")
    log: str = Field("latest", description="murphy: solver log relative to the case, or 'latest'")
    max_non_orthogonality: float | None = Field(None, description="reggie: default from the latest checkMesh log")
    initial_delta_t: float | None = Field(None, description="reggie: default the controlDict deltaT")
    target_courant_max: float = Field(0.5, description="reggie")
    time: str | None = Field(None, description="darwin: converged RANS time folder (default latest)")
    diameter_m: float | None = Field(None, description="darwin: jet/nozzle diameter [m]")
    velocity_jump_m_s: float | None = Field(None, description="darwin: shear-layer velocity difference [m/s]")
    max_cells: int | None = Field(None, description="darwin: cell budget")


def _case(ctx: ToolContext):
    return ctx.session.case()


def workflow_state(ctx: ToolContext, _p: NoParams) -> dict:
    from aethercfd.case.guided import overview

    out = overview(_case(ctx))
    out["how"] = ("Work stage by stage: workflow_stage shows the gates, questions and actions; workflow_answer "
                  "writes answers; workflow_fix applies a gate's fix; workflow_mark_done needs every error gate "
                  "to pass. Run a stage's run.* actions with run_openfoam (preset mesh / check / auto).")
    return out


def workflow_stage(ctx: ToolContext, p: StageParams) -> dict:
    from aethercfd.case.guided import assess

    case = _case(ctx)
    st = next((s for s in assess(case) if s.id == p.stage), None)
    if st is None:
        raise ToolError(f"unknown stage {p.stage!r}: {_STAGES}")
    d = st.as_dict(case.root)
    d["advanced_panels"] = [{"panel": pid, "label": lab} for pid, lab in st.advanced]
    return d


def _execute_batch(ctx: ToolContext, params: dict, j: Justified) -> dict:
    return ctx.execute("dict.batch", params, j)


def workflow_answer(ctx: ToolContext, p: AnswerParams) -> dict:
    from aethercfd.case.guided import load_state, plan_answers

    case = _case(ctx)
    state = load_state(case.root)
    try:
        gp = plan_answers(case, p.stage, p.answers, state)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if gp.empty:
        out: dict = {"ok": not gp.errors, "message": "nothing to change"}
        if gp.errors:
            out["errors"] = gp.errors
        return out
    out = _execute_batch(ctx, gp.batch_params(case.root, state), p)
    if gp.errors:
        out["not_applied"] = gp.errors
    out["changes"] = gp.notes
    if out.get("ok"):
        out["stage_now"] = workflow_stage(ctx, StageParams(stage=p.stage))["status"]
    return out


def workflow_fix(ctx: ToolContext, p: FixParams) -> dict:
    from aethercfd.case.guided import assess

    case = _case(ctx)
    st = next((s for s in assess(case) if s.id == p.stage), None)
    if st is None:
        raise ToolError(f"unknown stage {p.stage!r}")
    g = next((g for g in st.gates if g.id == p.gate), None)
    if g is None:
        raise ToolError(f"no gate {p.gate!r} in {st.title}; gates: {', '.join(x.id for x in st.gates)}")
    if g.ok:
        return {"ok": True, "message": f"{g.title}: already passes"}
    if not g.edits:
        raise ToolError(f"{g.title} has no automatic fix: {g.how}")
    edits = [{"file": str(case.root / e.file), "key": e.key, "value": e.value,
              "insert_if_missing": e.insert_if_missing} for e in g.edits]
    return _execute_batch(ctx, {"edits": edits, "summary": f"{g.fix_label} ({g.title})"}, p)


def workflow_mark_done(ctx: ToolContext, p: MarkParams) -> dict:
    from aethercfd.case.guided import mark_params

    params, refusal = mark_params(ctx.root, p.stage, p.done, by="copilot", note=p.note)
    if refusal:
        raise ToolError(refusal)
    return _execute_batch(ctx, params, p)


def dict_batch(ctx: ToolContext, p: DictBatchParams) -> dict:
    edits = [{"file": str(ctx.path(e.file)), "key": e.key, "value": e.value,
              "insert_if_missing": e.insert_if_missing} for e in p.edits]
    return _execute_batch(ctx, {"edits": edits, "summary": p.summary}, p)


# ============================================================================ cfd_agents
def _agents():
    from aethercfd.services.cfd_agents import CfdAgents

    return CfdAgents.from_settings()


def cfd_agents_status(ctx: ToolContext, _p: NoParams) -> dict:
    a = _agents()
    out = a.probe().as_dict()
    out["ollama_running"] = a.ollama_running()
    return out


def cfd_agent_run(ctx: ToolContext, p: AgentParams) -> dict:
    a = _agents()
    case = _case(ctx)
    name = p.agent.lower().strip()
    if name == "murphy":
        log = ctx.log_path(p.log)
        return a.murphy(log).as_dict()
    if name == "connie":
        return a.connie(case.root).as_dict()
    if name == "reggie":
        from aethercfd.case import gates as G

        no = p.max_non_orthogonality
        if no is None:
            _g, no = G.mesh_quality_gate(case)
        dt = p.initial_delta_t
        if dt is None:
            v = G._dict_value(case, "system/controlDict", "deltaT")
            dt = float(v) if v else None
        if no is None or dt is None:
            raise ToolError("reggie needs max_non_orthogonality (run checkMesh) and initial_delta_t")
        return a.reggie(no, dt, p.target_courant_max, case_root=case.root).as_dict()
    if name == "darwin":
        if p.diameter_m is None or p.velocity_jump_m_s is None:
            raise ToolError("darwin needs diameter_m and velocity_jump_m_s (a jet/shear-layer case)")
        t = p.time or case.latest_result_time
        if not t:
            raise ToolError("darwin needs a converged RANS result time folder")
        extra = {"max_cells": p.max_cells} if p.max_cells else {}
        return a.darwin(case.root, t, diameter_m=p.diameter_m, velocity_jump_m_s=p.velocity_jump_m_s,
                        **extra).as_dict()
    from aethercfd.services.cfd_agents import CATALOG

    info = CATALOG.get(name)
    if info is not None:
        raise ToolError(f"{info.name} is not called from AetherCFD: {info.reason}")
    raise ToolError(f"unknown agent {p.agent!r}")


WORKFLOW_TOOLS: list[Tool] = [
    Tool("workflow_state", "The guided workflow of the open case: the 12 stages (Geometry → ... → ML) with status "
         "(done/ready/attention/error/blocked/changed/na), the next action for each, failing gates, the reference "
         "quantities (U, L, ν, T, Mach, Re) and the current stage. Use it to guide a beginner step by step.",
         NoParams, workflow_state),
    Tool("workflow_stage", "One stage in detail: its correctness gates (why, how to fix, evidence, fix edits), its "
         "physical questions (ids, current values, choices, what they write), actions and Advanced panels.",
         StageParams, workflow_stage),
    Tool("workflow_answer", "Answer a stage's physical questions; the answers become exact dictionary edits shown to "
         "the user as one plan to approve (and journalled). Reference values (U, L, fluid, T) go to the workflow "
         "state, not the case.", AnswerParams, workflow_answer, mutates=True),
    Tool("workflow_fix", "Apply a failing gate's one-click fix (one plan, approved by the user).", FixParams,
         workflow_fix, mutates=True),
    Tool("workflow_mark_done", "Mark a stage done (or not). Refused while any of its error gates fails or a "
         "dependency has errors; the reason says what to fix.", MarkParams, workflow_mark_done, mutates=True),
    Tool("dict_batch", "Several dictionary edits across files as ONE plan (one approval, one journal entry, one "
         "revert); missing parent sub-dictionaries are created. Use it to apply an agent's suggestion.",
         DictBatchParams, dict_batch, mutates=True),
    Tool("cfd_agents_status", "Which of the research group's cfd_agents can run here (Murphy, Connie, Reggie, Darwin "
         "are wired; Betty, Bill, Bob, Larry are not, with the reason), the interpreter and any setup problem.",
         NoParams, cfd_agents_status),
    Tool("cfd_agent_run", "Run one cfd_agent out of process on this case and return its findings: murphy (failure "
         "diagnosis of a solver log), connie (run health from fieldMinMax/solverInfo), reggie (transient PIMPLE/"
         "ddt/Δt design, with suggested edits), darwin (RANS shear-layer refinement plan; all-tet meshes). "
         "Suggestions only: apply them with dict_batch after explaining them.", AgentParams, cfd_agent_run),
]
