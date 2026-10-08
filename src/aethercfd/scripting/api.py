"""Scripting facade: one object that wires the headless core together.

    from aethercfd.scripting.api import Session
    s = Session(r"D:\\OpenFOAM\\backwardFacingStep2D", create_project=True)
    s.set("system/controlDict", "endTime", "500")          # journalled, minimal edit
    run_id = s.run(preset="solve")                          # detached WSL run
    s.runs.wait(run_id)

The GUI, the CLI and the future MCP server (copilot) all build on this same object.
"""

from __future__ import annotations

import json
from pathlib import Path

from aethercfd.backend.wsl.forks import ForkProfile, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.case.discovery import discover_cases
from aethercfd.case.model import Case, load_case
from aethercfd.commands.base import Actor, ActorKind, CommandResult
from aethercfd.commands.executor import ApprovalBroker, AutoApprove, Context, DenyAll, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.core.events import BUS, EventBus
from aethercfd.core.settings import Settings, cache_dir
from aethercfd.documents.store import DocumentStore
from aethercfd.project.project import Mode, Project
from aethercfd.runs.manager import RunManager

SCRIPT = Actor(ActorKind.SCRIPT, "api")


def load_forks(runner: WslRunner, *, refresh: bool = False) -> list[ForkProfile]:
    cache = cache_dir() / "forks" / f"{runner.distro}.json"
    if cache.exists() and not refresh:
        return [ForkProfile.from_json(d) for d in json.loads(cache.read_text(encoding="utf-8"))]
    forks = probe_forks(runner)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([f.to_json() for f in forks], indent=2), encoding="utf-8")
    return forks


class Session:
    def __init__(self, root: str | Path, *, create_project: bool = False, mode: Mode = Mode.SIDECAR,
                 broker: ApprovalBroker | None = None, actor: Actor = SCRIPT, bus: EventBus | None = None,
                 with_backend: bool = True):
        self.root = Path(root).resolve()
        self.settings = Settings.load()
        self.bus = bus or BUS
        self.actor = actor
        self.project = Project.find(self.root)
        if self.project is None and create_project:
            self.project = Project.create(self.root, mode=mode)
        self.runner: WslRunner | None = None
        self.forks: list[ForkProfile] = []
        self.runs: RunManager | None = None
        if with_backend:
            w = self.settings.wsl
            self.runner = WslRunner(w.default_distro, allowed=w.allowed_distros)
            self.forks = load_forks(self.runner)
            if self.project is not None:
                self.runs = RunManager(self.project, self.runner, settings=self.settings, bus=self.bus)
        self.context = Context(self.project, backend=self.runner, settings=self.settings, run_manager=self.runs,
                               forks=self.forks)
        self.executor = Executor(default_registry(), self.context, broker or DenyAll())
        self.documents = DocumentStore()

    # ------------------------------------------------------------------ cases
    def cases(self) -> list[Path]:
        return discover_cases(self.root)

    def case(self, path: str | Path | None = None) -> Case:
        return load_case(Path(path) if path else self.root)

    # ------------------------------------------------------------------ commands
    def do(self, name: str, **params) -> CommandResult:
        return self.executor.execute(name, params, self.actor)

    def set(self, file: str, key: str, value: str, *, case: str | Path | None = None,
            insert_if_missing: bool = False) -> CommandResult:
        base = Path(case) if case else self.root
        return self.do("dict.set", file=str(base / file), key=key, value=value, insert_if_missing=insert_if_missing)

    def run(self, *, case: str | Path | None = None, **params) -> str:
        res = self.do("run.start", case=str(Path(case) if case else self.root), **params)
        if not res.ok:
            raise RuntimeError(res.message)
        return res.data


__all__ = ["AutoApprove", "DenyAll", "Session", "load_forks"]
