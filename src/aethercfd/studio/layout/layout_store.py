"""Per-perspective layout files with one variant per monitor setup (fingerprint).

%APPDATA%\\AetherCFD\\layouts\\<perspective>.json  (atomic writes, last 5 versions kept as backups)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from aethercfd.core.settings import config_dir
from aethercfd.core.textio import atomic_write_bytes

SCHEMA = 1


def layouts_dir() -> Path:
    d = config_dir() / "layouts"
    d.mkdir(parents=True, exist_ok=True)
    return d


class LayoutStore:
    def __init__(self, root: Path | None = None):
        self.root = root or layouts_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, perspective: str) -> Path:
        return self.root / f"{perspective}.json"

    def load_file(self, perspective: str) -> dict:
        p = self.path(perspective)
        if not p.exists():
            return {"schema": SCHEMA, "perspective": perspective, "variants": {}}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("schema") != SCHEMA:
                raise ValueError("schema mismatch")
            return data
        except (ValueError, OSError):
            # corrupt or old: keep it aside, start fresh (the recipe will be used)
            shutil.copy2(p, p.with_suffix(".corrupt.json"))
            return {"schema": SCHEMA, "perspective": perspective, "variants": {}}

    def save_variant(self, perspective: str, fp: str, variant: dict) -> None:
        data = self.load_file(perspective)
        data["variants"][fp] = variant
        data["last_used_variant"] = fp
        p = self.path(perspective)
        if p.exists():
            self._rotate_backups(p)
        atomic_write_bytes(p, json.dumps(data, indent=1).encode("utf-8"))

    def _rotate_backups(self, p: Path, keep: int = 5) -> None:
        for i in range(keep - 1, 0, -1):
            src = p.with_suffix(f".bak{i}.json")
            if src.exists():
                src.replace(p.with_suffix(f".bak{i + 1}.json"))
        shutil.copy2(p, p.with_suffix(".bak1.json"))

    def find_variant(self, perspective: str, fp: str, arrangement: str, n_screens: int) -> tuple[str, dict] | None:
        """Exact fingerprint first (any arrangement), else a variant with the same number of screens."""
        variants = self.load_file(perspective)["variants"]
        if fp in variants:
            return "exact", variants[fp]
        same_count = [v for v in variants.values() if len(v.get("screens", [])) == n_screens]
        if same_count:
            return "similar", sorted(same_count, key=lambda v: v.get("saved_at", ""))[-1]
        return None

    def perspectives(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if "." not in p.stem)
