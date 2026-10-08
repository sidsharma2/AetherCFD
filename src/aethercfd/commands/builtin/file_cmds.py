"""file.save / file.revert / file.trash."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from aethercfd.commands.base import (
    Actor,
    CheckResult,
    Command,
    CommandPlan,
    CommandSpec,
    FileChange,
    Risk,
    unified_diff,
)
from aethercfd.commands.executor import Context
from aethercfd.core.textio import decode_bytes, encode_text, sha256_bytes


class SaveParams(BaseModel):
    path: str
    text: str
    expected_sha: str | None = None  # the disk sha the editor buffer was loaded from
    encoding: str | None = None  # default: keep the file's current encoding


class FileSave(Command):
    spec = CommandSpec("file.save", SaveParams, Risk.EDIT, "Save a file (keeps its encoding and line endings)")

    def plan(self, ctx: Context, params: SaveParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        path = Path(params.path)
        before = path.read_bytes() if path.exists() else None
        before_sha = sha256_bytes(before) if before is not None else None
        enc = params.encoding
        old_text = ""
        if before is not None:
            old_text, detected = decode_bytes(before)
            enc = enc or detected
        after = encode_text(params.text, enc or "utf-8")
        checks = []
        if params.expected_sha is not None and params.expected_sha != before_sha:
            checks.append(CheckResult(False, f"{path.name} changed on disk since it was opened; merge first"))
        plan = CommandPlan(self.spec.name, params.model_dump(exclude={"text"}), actor, self.spec.risk,
                           f"save {path.name}", preconditions=checks)
        if before == after:
            plan.consequences.append("no change")
            return plan
        plan.file_changes.append(FileChange(path, before_sha, after, unified_diff(path, old_text, params.text), before))
        return plan


class WriteFilesParams(BaseModel):
    root: str  # every path below is relative to this folder and must stay inside it
    files: dict[str, str] = {}  # relative path -> text (UTF-8, LF)
    copies: dict[str, str] = {}  # relative destination -> source file (copied byte for byte, e.g. an STL)
    keep_existing: list[str] = []  # relative paths that are written only if they do not exist yet


class FilesWrite(Command):
    spec = CommandSpec("files.write", WriteFilesParams, Risk.EDIT,
                       "Write a set of files (e.g. a generated mesh setup) as one plan")

    def plan(self, ctx: Context, params: WriteFilesParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        root = Path(params.root).resolve()
        n = len(params.files) + len(params.copies)
        plan = CommandPlan(self.spec.name, params.model_dump(exclude={"files"}) | {"files": sorted(params.files)},
                           actor, self.spec.risk, f"write {n} file(s) in {root.name}")
        items = [(rel, text.encode("utf-8"), text) for rel, text in params.files.items()]
        for rel, src in params.copies.items():
            sp = Path(src)
            if not sp.is_file():
                plan.preconditions.append(CheckResult(False, f"{src} does not exist"))
                return plan
            items.append((rel, sp.read_bytes(), None))
        for rel, data, text in items:
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                plan.preconditions.append(CheckResult(False, f"{rel} is outside {root}"))
                return plan
            before = path.read_bytes() if path.exists() else None
            if before is not None and rel in params.keep_existing:
                plan.consequences.append(f"kept existing {rel}")
                continue
            if before == data:
                continue
            if text is not None:
                diff = unified_diff(path, decode_bytes(before)[0] if before is not None else "", text)
            else:
                diff = f"copy {len(data):,} bytes -> {rel}\n"
            plan.file_changes.append(FileChange(path, sha256_bytes(before) if before is not None else None, data,
                                                diff, before))
        if not plan.file_changes:
            plan.consequences.append("no change")
        return plan


class RevertParams(BaseModel):
    journal_id: str


class FileRevert(Command):
    spec = CommandSpec("file.revert", RevertParams, Risk.EDIT, "Undo the file changes of a journalled command")

    def plan(self, ctx: Context, params: RevertParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        plan = CommandPlan(self.spec.name, params.model_dump(), actor, self.spec.risk,
                           f"revert {params.journal_id}")
        proj = ctx.project
        if proj is None:
            plan.preconditions.append(CheckResult(False, "no project journal"))
            return plan
        entry = proj.journal.find(params.journal_id)
        if entry is None:
            plan.preconditions.append(CheckResult(False, f"journal entry {params.journal_id} not found"))
            return plan
        plan.summary = f"revert: {entry.get('summary', entry.get('command'))}"
        for f in entry.get("files", []):
            path = Path(f["path"])
            cur = ctx.current_sha(path)
            if cur != f.get("after"):
                plan.preconditions.append(CheckResult(
                    False, f"{path.name} changed after that command; revert needs a merge"))
                continue
            before_sha = f.get("before")
            if before_sha is None:  # the command created the file -> revert moves it to the trash
                plan.file_changes.append(FileChange(path, cur, None, f"trash {path.name}"))
                continue
            data = proj.objects.get(before_sha)
            if data is None:
                plan.preconditions.append(CheckResult(False, f"original content of {path.name} is not stored"))
                continue
            now = path.read_bytes() if path.exists() else b""
            diff = unified_diff(path, decode_bytes(now)[0], decode_bytes(data)[0])
            plan.file_changes.append(FileChange(path, cur, data, diff, now))
        return plan


class TrashParams(BaseModel):
    path: str


class FileTrash(Command):
    spec = CommandSpec("file.trash", TrashParams, Risk.DESTRUCTIVE,
                       "Move a file into the project trash (never deleted)")

    def plan(self, ctx: Context, params: TrashParams, actor: Actor) -> CommandPlan:  # type: ignore[override]
        path = Path(params.path)
        plan = CommandPlan(self.spec.name, params.model_dump(), actor, self.spec.risk, f"move {path.name} to trash")
        if not path.exists():
            plan.preconditions.append(CheckResult(False, f"{path} does not exist"))
            return plan
        data = path.read_bytes() if path.is_file() else None
        plan.file_changes.append(FileChange(path, ctx.current_sha(path) if path.is_file() else None, None,
                                            f"trash {path.name}", data))
        plan.consequences.append("recoverable from .aethercfd/local/trash")
        return plan


COMMANDS = [FileSave(), FileRevert(), FileTrash(), FilesWrite()]
