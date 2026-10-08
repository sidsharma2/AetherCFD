"""AetherCFD project: app metadata kept *beside* OpenFOAM cases, never inside them.

Sidecar mode (default): ``<root>/.aethercfd/`` holding ``project.toml`` (shareable settings) and
``local/`` (journal, objects, runs, cache, trash; never committed). When ``<root>`` is in a git repo,
``.aethercfd/`` is added to ``.git/info/exclude`` unless the user chooses to share project settings.

External mode: the same layout under ``%APPDATA%\\AetherCFD\\projects\\<id>\\`` for folders the user
does not want touched at all.

Opening/browsing never writes: a project is only created by an explicit ``Project.create``.
"""

from __future__ import annotations

import subprocess
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import tomli_w

from aethercfd.core.settings import config_dir
from aethercfd.core.textio import atomic_write_bytes
from aethercfd.project.journal import Journal, ObjectStore

SIDECAR = ".aethercfd"


class Mode(StrEnum):
    SIDECAR = "sidecar"
    EXTERNAL = "external"


class Placement(StrEnum):
    AUTO = "auto"
    IN_PLACE = "in-place"
    STAGED = "staged"


@dataclass
class CaseConfig:
    path: str  # relative to the project root, POSIX separators ("." for the root itself)
    fork: str | None = None  # ForkProfile.id confirmed by the user
    placement: Placement = Placement.AUTO

    def to_toml(self) -> dict:
        d: dict = {"path": self.path, "placement": self.placement.value}
        if self.fork:
            d["fork"] = self.fork
        return d


@dataclass
class Project:
    root: Path
    meta_dir: Path  # where project.toml + local/ live
    mode: Mode
    id: str
    name: str
    created: str
    cases: list[CaseConfig] = field(default_factory=list)

    # ------------------------------------------------------------------ layout
    @property
    def local(self) -> Path:
        return self.meta_dir / "local"

    @property
    def journal(self) -> Journal:
        return Journal(self.local / "journal")

    @property
    def objects(self) -> ObjectStore:
        return ObjectStore(self.local / "objects")

    @property
    def runs_dir(self) -> Path:
        return self.local / "runs"

    @property
    def trash_dir(self) -> Path:
        return self.local / "trash"

    # ------------------------------------------------------------------ create / open
    @staticmethod
    def sidecar_dir(root: Path) -> Path:
        return root / SIDECAR

    @staticmethod
    def external_index() -> Path:
        return config_dir() / "projects"

    @classmethod
    def find(cls, root: str | Path) -> Project | None:
        """Open an existing project for ``root`` (sidecar first, then external). Never writes."""
        root = Path(root).resolve()
        side = cls.sidecar_dir(root) / "project.toml"
        if side.exists():
            return cls._load(root, side.parent, Mode.SIDECAR)
        idx = cls.external_index()
        if idx.exists():
            for d in idx.iterdir():
                pt = d / "project.toml"
                if pt.exists():
                    with open(pt, "rb") as f:
                        data = tomllib.load(f)
                    if Path(data.get("project", {}).get("root", "")).resolve() == root:
                        return cls._load(root, d, Mode.EXTERNAL)
        return None

    @classmethod
    def create(cls, root: str | Path, *, mode: Mode = Mode.SIDECAR, name: str | None = None,
               share_settings: bool = False, cases: list[CaseConfig] | None = None) -> Project:
        root = Path(root).resolve()
        if cls.find(root) is not None:
            raise FileExistsError(f"an AetherCFD project already exists for {root}")
        pid = uuid.uuid4().hex[:12]
        meta = cls.sidecar_dir(root) if mode is Mode.SIDECAR else cls.external_index() / pid
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "local").mkdir(exist_ok=True)
        atomic_write_bytes(meta / "local" / ".gitignore", b"# AetherCFD machine-local state: never commit\n*\n")
        proj = cls(root=root, meta_dir=meta, mode=mode, id=pid, name=name or root.name,
                   created=datetime.now(UTC).isoformat(timespec="seconds"), cases=cases or [])
        proj.save()
        if mode is Mode.SIDECAR and not share_settings:
            _git_exclude(root, f"/{SIDECAR}/")
        return proj

    @classmethod
    def _load(cls, root: Path, meta: Path, mode: Mode) -> Project:
        with open(meta / "project.toml", "rb") as f:
            data = tomllib.load(f)
        p = data.get("project", {})
        cases = [CaseConfig(path=c["path"], fork=c.get("fork"), placement=Placement(c.get("placement", "auto")))
                 for c in data.get("cases", [])]
        return cls(root=root, meta_dir=meta, mode=mode, id=p.get("id", "?"), name=p.get("name", root.name),
                   created=p.get("created", ""), cases=cases)

    def save(self) -> None:
        data = {"project": {"id": self.id, "name": self.name, "created": self.created, "format": 1}}
        if self.mode is Mode.EXTERNAL:
            data["project"]["root"] = str(self.root)
        data["cases"] = [c.to_toml() for c in self.cases]
        atomic_write_bytes(self.meta_dir / "project.toml", tomli_w.dumps(data).encode("utf-8"))

    # ------------------------------------------------------------------ cases
    def case_config(self, case_root: Path) -> CaseConfig | None:
        rel = _rel(self.root, case_root)
        return next((c for c in self.cases if c.path == rel), None)

    def ensure_case(self, case_root: Path) -> CaseConfig:
        cfg = self.case_config(case_root)
        if cfg is None:
            cfg = CaseConfig(path=_rel(self.root, case_root))
            self.cases.append(cfg)
            self.save()
        return cfg


def _rel(root: Path, p: Path) -> str:
    r = Path(p).resolve().relative_to(root)
    return r.as_posix() if str(r) != "." else "."


def _git_exclude(root: Path, pattern: str) -> None:
    """Add ``pattern`` to the enclosing repo's .git/info/exclude (local-only ignore) if not present."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired):
        return
    if top.returncode != 0:
        return
    git_dir = Path(top.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    # the pattern is relative to the repo root, which may be above `root`
    try:
        repo_root = Path(subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                                        text=True, timeout=10, creationflags=0x08000000).stdout.strip()).resolve()
        rel = root.resolve().relative_to(repo_root).as_posix()
        if rel and rel != ".":
            pattern = f"/{rel}{pattern}"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    ex = git_dir / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    current = ex.read_text(encoding="utf-8") if ex.exists() else ""
    if pattern not in current.splitlines():
        sep = "" if current.endswith("\n") or not current else "\n"
        atomic_write_bytes(ex, (current + sep + f"{pattern}  # AetherCFD project metadata\n").encode("utf-8"))
