"""script.run: run a Python script that lives in the case folder (e.g. a mesh generator) as an approvable,
journalled command. The script runs with AetherCFD's own Python on Windows, in its folder."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from aethercfd.commands.base import (
    Actor,
    CheckResult,
    Command,
    CommandPlan,
    CommandResult,
    CommandSpec,
    Risk,
    ShellStep,
)
from aethercfd.commands.executor import Context
from aethercfd.core.runtime import script_argv


class ScriptParams(BaseModel):
    path: str
    args: list[str] = Field(default_factory=list)
    timeout_s: float = 900.0
    case: str | None = None  # the case folder the script must live in (confinement)


class ScriptRun(Command):
    spec = CommandSpec("script.run", ScriptParams, Risk.EXECUTE, "Run a Python script from the case folder")

    def plan(self, ctx: Context, params: ScriptParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        p = Path(params.path)
        plan = CommandPlan(self.spec.name, params.model_dump(), actor, self.spec.risk, f"run {p.name}")
        if p.suffix.lower() != ".py" or not p.is_file():
            plan.preconditions.append(CheckResult(False, f"{p} is not an existing .py file"))
            return plan
        if params.case:
            try:
                p.resolve().relative_to(Path(params.case).resolve())
            except ValueError:
                plan.preconditions.append(CheckResult(False, f"{p} is outside the case folder"))
                return plan
        head = p.read_text(encoding="utf-8", errors="replace").splitlines()[:25]
        plan.shell_steps.append(ShellStep(None, str(p.parent), ("python", p.name, *params.args)))
        plan.consequences += [f"runs with AetherCFD's Python on Windows in {p.parent} (may create or change files "
                              "there; those changes are not journalled individually)",
                              "first lines of the script:\n" + "\n".join("    " + x for x in head)]
        return plan

    def apply(self, ctx: Context, plan: CommandPlan) -> CommandResult:
        p = Path(plan.params["path"])
        try:
            r = subprocess.run(script_argv(str(p), *plan.params.get("args", [])), cwd=str(p.parent),
                               capture_output=True, text=True, timeout=float(plan.params.get("timeout_s", 900)),
                               creationflags=0x08000000)
        except subprocess.TimeoutExpired:
            return CommandResult(False, plan, "timed out")
        return CommandResult(r.returncode == 0, plan, f"exit {r.returncode}",
                             data={"returncode": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-8000:]})


COMMANDS = [ScriptRun()]
