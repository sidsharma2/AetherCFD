"""Command layer: every change AetherCFD makes is a Command.

``plan()`` is pure and *is* the dry run: it returns the exact file changes (with diffs), shell steps,
consequences and risk. The executor then asks the approval broker (policy depends on who is acting
and how risky it is), re-checks that files did not change underneath (TOCTOU), applies, and journals.
The GUI, the scripting console and (later) the MCP server for the copilot all go through this path.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from aethercfd.commands.executor import Context


class Risk(StrEnum):
    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"


class ActorKind(StrEnum):
    USER = "user"  # a person clicking in the GUI
    SCRIPT = "script"  # the scripting console / CLI
    AGENT = "agent"  # the copilot (must justify itself)
    EXTERNAL = "external"  # changes noticed on disk (VS Code, git) -- journalled only


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str = ""


USER = Actor(ActorKind.USER, "gui")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


@dataclass
class FileChange:
    path: Path
    before_sha: str | None  # None = file must not exist yet
    after: bytes | None  # None = move the file to the project trash (never hard-deleted)
    diff: str = ""
    before: bytes | None = None  # kept for journalling / revert

    @property
    def kind(self) -> str:
        if self.after is None:
            return "trash"
        return "create" if self.before_sha is None else "modify"


@dataclass(frozen=True)
class ShellStep:
    fork_id: str | None
    cwd: str  # WSL path
    argv: tuple[str, ...]
    log_name: str | None = None
    creates: tuple[str, ...] = ()


@dataclass
class CommandPlan:
    command: str
    params: dict[str, Any]
    actor: Actor
    risk: Risk
    summary: str
    preconditions: list[CheckResult] = field(default_factory=list)
    file_changes: list[FileChange] = field(default_factory=list)
    shell_steps: list[ShellStep] = field(default_factory=list)
    consequences: list[str] = field(default_factory=list)
    rationale: str | None = None
    confidence: float | None = None

    @property
    def blocked(self) -> list[CheckResult]:
        return [c for c in self.preconditions if not c.ok]

    @property
    def plan_hash(self) -> str:
        """Identity of exactly what will happen; an approval is bound to this hash."""
        h = hashlib.sha256()
        h.update(json.dumps({"c": self.command, "p": self.params, "r": self.risk.value},
                            sort_keys=True, default=str).encode())
        for fc in self.file_changes:
            h.update(str(fc.path).encode())
            h.update((fc.before_sha or "-").encode())
            h.update(hashlib.sha256(fc.after).digest() if fc.after is not None else b"trash")
        for s in self.shell_steps:
            h.update(json.dumps(asdict(s), sort_keys=True).encode())
        return h.hexdigest()

    def describe(self) -> str:
        lines = [f"{self.command}: {self.summary}  [{self.risk.value}]"]
        if self.rationale:
            lines.append(f"rationale: {self.rationale} (confidence {self.confidence})")
        for c in self.preconditions:
            lines.append(f"  {'ok ' if c.ok else 'BLOCKED'} {c.message}")
        for fc in self.file_changes:
            lines.append(f"  {fc.kind:6s} {fc.path}")
        for s in self.shell_steps:
            lines.append(f"  run    {' '.join(s.argv)}  (in {s.cwd})")
        lines += [f"  -> {c}" for c in self.consequences]
        return "\n".join(lines)


@dataclass
class CommandResult:
    ok: bool
    plan: CommandPlan
    message: str = ""
    journal_id: str | None = None
    data: Any = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    params: type[BaseModel]
    risk: Risk
    summary: str


class CommandError(RuntimeError):
    pass


class Conflict(CommandError):
    """A file changed between planning and applying."""


class Command(ABC):
    spec: ClassVar[CommandSpec]

    @abstractmethod
    def plan(self, ctx: Context, params: BaseModel, actor: Actor) -> CommandPlan: ...

    def apply(self, ctx: Context, plan: CommandPlan) -> CommandResult:
        """Default: apply the plan's file changes (subclasses add shell steps etc.)."""
        ctx.apply_file_changes(plan)
        return CommandResult(True, plan, "applied")


def unified_diff(path: Path, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{path.name}", tofile=f"b/{path.name}", n=2))
