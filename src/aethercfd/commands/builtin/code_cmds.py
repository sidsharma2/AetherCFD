"""code.create / code.build: your own OpenFOAM physics as journalled, approvable commands."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from aethercfd.commands.base import (
    Actor,
    CheckResult,
    Command,
    CommandPlan,
    CommandResult,
    CommandSpec,
    FileChange,
    Risk,
    ShellStep,
)
from aethercfd.commands.executor import Context
from aethercfd.core.paths import win_to_wsl
from aethercfd.core.textio import sha256_bytes


class CreateParams(BaseModel):
    template: str
    name: str
    dest: str  # folder for the sources (e.g. <case>/aether-code/<name>)


class CodeCreate(Command):
    spec = CommandSpec("code.create", CreateParams, Risk.EDIT, "Create OpenFOAM code from a template")

    def plan(self, ctx: Context, params: CreateParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        from aethercfd.codegen.templates import render, template

        plan = CommandPlan(self.spec.name, params.model_dump(), actor, self.spec.risk,
                           f"create {params.template} as {params.name}")
        t = template(params.template)
        if t is None:
            plan.preconditions.append(CheckResult(False, f"unknown template {params.template!r}"))
            return plan
        if not params.name.isidentifier():
            plan.preconditions.append(CheckResult(False, "the name must be a C++ identifier (letters, digits, _)"))
            return plan
        dest = Path(params.dest)
        for rel, text in render(t, params.name).items():
            p = dest / rel
            before = p.read_bytes() if p.exists() else None
            data = text.encode("utf-8")
            plan.file_changes.append(FileChange(p, sha256_bytes(before) if before is not None else None, data,
                                                f"+++ {rel} ({len(text.splitlines())} lines)\n", before))
        plan.consequences.append(t.usage)
        if t.kind == "library":
            plan.consequences.append("next: build it (wmake inside WSL with the case's OpenFOAM)")
        return plan


class BuildParams(BaseModel):
    dir: str
    fork: str | None = None
    target: str = "libso"
    clean: bool = False


class CodeBuild(Command):
    spec = CommandSpec("code.build", BuildParams, Risk.EXECUTE, "Compile OpenFOAM code with wmake in WSL")

    def plan(self, ctx: Context, params: BuildParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        plan = CommandPlan(self.spec.name, params.model_dump(), actor, self.spec.risk, f"wmake {params.target} in "
                           f"{Path(params.dir).name}")
        forks = getattr(ctx, "forks", []) or []
        fork = next((f for f in forks if f.id == params.fork), None) if params.fork else (forks[0] if forks else None)
        if fork is None or ctx.backend is None:
            plan.preconditions.append(CheckResult(False, "no WSL backend / OpenFOAM fork available"))
            return plan
        if not (Path(params.dir) / "Make" / "files").exists():
            plan.preconditions.append(CheckResult(False, f"{params.dir} has no Make/files"))
        plan.shell_steps.append(ShellStep(fork.id, win_to_wsl(params.dir), ("wmake", params.target)))
        plan.consequences.append("writes the library/executable into $FOAM_USER_LIBBIN / $FOAM_USER_APPBIN in WSL "
                                 "(your user area, not the OpenFOAM installation)")
        plan.params["_fork"] = fork.id
        return plan

    def apply(self, ctx: Context, plan: CommandPlan) -> CommandResult:
        from aethercfd.codegen.build import build

        fork = next(f for f in ctx.forks if f.id == plan.params["_fork"])
        res = build(ctx.backend, fork, Path(plan.params["dir"]), plan.params.get("target", "libso"),
                    clean=bool(plan.params.get("clean")))
        msg = f"built {res.library}" if res.ok else (f"{sum(d.severity == 'error' for d in res.diagnostics)} "
                                                    f"compiler error(s)" if res.diagnostics else "build failed")
        return CommandResult(res.ok, plan, msg, data={"library": res.library, "output": res.output[-20000:],
                                                     "diagnostics": [d.__dict__ for d in res.diagnostics]})


COMMANDS = [CodeCreate(), CodeBuild()]
