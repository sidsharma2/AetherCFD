"""Executes commands: validate -> plan -> policy -> approval -> apply (with conflict check) -> journal."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from aethercfd.commands.base import (
    Actor,
    ActorKind,
    CommandError,
    CommandPlan,
    CommandResult,
    Conflict,
    Risk,
)
from aethercfd.commands.registry import Registry
from aethercfd.core.textio import atomic_write_bytes, sha256_bytes
from aethercfd.project.journal import new_id
from aethercfd.project.project import Project


@dataclass(frozen=True)
class Approval:
    approved: bool
    by: str
    note: str = ""


class ApprovalBroker(Protocol):
    def request(self, plan: CommandPlan) -> Approval: ...


class AutoApprove:
    """Approves everything (tests, trusted scripts)."""

    def request(self, plan: CommandPlan) -> Approval:
        return Approval(True, "auto")


class DenyAll:
    def request(self, plan: CommandPlan) -> Approval:
        return Approval(False, "deny-all", "no interactive approver available")


def needs_approval(actor: Actor, risk: Risk) -> bool:
    """Default policy. A user's own click/save is the approval for edits and runs they started;
    destructive actions always ask; agents must ask for anything that is not read-only."""
    if risk is Risk.READ:
        return False
    if actor.kind is ActorKind.USER:
        return risk is Risk.DESTRUCTIVE
    if actor.kind is ActorKind.SCRIPT:
        return risk is Risk.DESTRUCTIVE
    return True  # agent (and anything unknown)


class Context:
    """What commands may touch: the project (journal, objects, trash) and helpers to change files."""

    def __init__(self, project: Project | None, *, backend: Any = None, settings: Any = None):
        self.project = project
        self.backend = backend
        self.settings = settings
        self._listeners: list[Callable[[str, dict], None]] = []

    def on_event(self, fn: Callable[[str, dict], None]) -> None:
        self._listeners.append(fn)

    def emit(self, kind: str, payload: dict) -> None:
        for fn in list(self._listeners):
            fn(kind, payload)

    # ------------------------------------------------------------------ files
    @staticmethod
    def current_sha(path: Path) -> str | None:
        return sha256_bytes(path.read_bytes()) if path.exists() else None

    def apply_file_changes(self, plan: CommandPlan) -> None:
        # check every file first so a conflict leaves nothing half-applied
        for fc in plan.file_changes:
            cur = self.current_sha(fc.path)
            if cur != fc.before_sha:
                raise Conflict(f"{fc.path} changed since the plan was made (expected "
                               f"{(fc.before_sha or 'absent')[:12]}, found {(cur or 'absent')[:12]})")
        for fc in plan.file_changes:
            if fc.after is None:
                self.trash(fc.path)
            else:
                fc.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(fc.path, fc.after)
            self.emit("file.changed", {"path": str(fc.path), "kind": fc.kind})

    def trash(self, path: Path) -> Path:
        """Move a file/dir into the project trash (AetherCFD never hard-deletes user data)."""
        if self.project is None:
            raise CommandError("moving files to the trash needs an AetherCFD project")
        dest = self.project.trash_dir / f"{new_id()}_{path.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return dest


class Executor:
    def __init__(self, registry: Registry, ctx: Context, broker: ApprovalBroker | None = None):
        self.registry = registry
        self.ctx = ctx
        self.broker = broker or DenyAll()

    def plan(self, name: str, params: dict | Any, actor: Actor, *, rationale: str | None = None,
             confidence: float | None = None) -> CommandPlan:
        cmd = self.registry.get(name)
        try:
            p = params if isinstance(params, cmd.spec.params) else cmd.spec.params.model_validate(params)
        except ValidationError as exc:
            raise CommandError(f"invalid parameters for {name}: {exc}") from exc
        plan = cmd.plan(self.ctx, p, actor)
        plan.rationale = rationale if rationale is not None else plan.rationale
        plan.confidence = confidence if confidence is not None else plan.confidence
        return plan

    def execute(self, name: str, params: dict | Any, actor: Actor, *, rationale: str | None = None,
                confidence: float | None = None, plan: CommandPlan | None = None,
                approved_hash: str | None = None) -> CommandResult:
        cmd = self.registry.get(name)
        plan = plan or self.plan(name, params, actor, rationale=rationale, confidence=confidence)
        if plan.blocked:
            return CommandResult(False, plan, "; ".join(c.message for c in plan.blocked))
        if actor.kind is ActorKind.AGENT and plan.risk is not Risk.READ and (
                not plan.rationale or plan.confidence is None):
            return CommandResult(False, plan, "agent plans must state a rationale and a confidence")
        if actor.kind is ActorKind.EXTERNAL:
            return CommandResult(False, plan, "external actors cannot execute commands")
        approval = Approval(True, "policy")
        if approved_hash is not None:
            if approved_hash != plan.plan_hash:
                return CommandResult(False, plan, "the approved plan no longer matches what would happen")
            approval = Approval(True, "pre-approved")
        elif needs_approval(actor, plan.risk):
            approval = self.broker.request(plan)
            if not approval.approved:
                return CommandResult(False, plan, f"not approved: {approval.note or approval.by}")
        if plan.risk is not Risk.READ and plan.file_changes and self.ctx.project is None:
            return CommandResult(False, plan, "changing files needs an AetherCFD project (for the journal)")
        t0 = time.perf_counter()
        try:
            result = cmd.apply(self.ctx, plan)
        except Conflict as exc:
            return CommandResult(False, plan, str(exc))
        result.journal_id = self._journal(plan, approval, result, time.perf_counter() - t0)
        self.ctx.emit("command.applied", {"command": name, "ok": result.ok, "journal_id": result.journal_id})
        return result

    def _journal(self, plan: CommandPlan, approval: Approval, result: CommandResult, seconds: float) -> str | None:
        if self.ctx.project is None or plan.risk is Risk.READ:
            return None
        store = self.ctx.project.objects
        files = []
        for fc in plan.file_changes:
            files.append({"path": str(fc.path), "kind": fc.kind, "before": fc.before_sha,
                          "after": store.put(fc.after) if fc.after is not None else None})
            if fc.before is not None:
                store.put(fc.before)
        entry = self.ctx.project.journal.append({
            "actor": {"kind": plan.actor.kind.value, "id": plan.actor.id},
            "command": plan.command, "params": plan.params, "risk": plan.risk.value, "summary": plan.summary,
            "plan_hash": plan.plan_hash, "approval": {"by": approval.by, "note": approval.note},
            "rationale": plan.rationale, "confidence": plan.confidence,
            "ok": result.ok, "message": result.message, "files": files,
            "shell": [" ".join(s.argv) for s in plan.shell_steps], "seconds": round(seconds, 3)})
        return entry["id"]
