"""Discover OpenFOAM dictionary/field files under D:\\OpenFOAM for read-only corpus tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

CORPUS_ROOT = Path(os.environ.get("AETHERCFD_CORPUS", r"D:\OpenFOAM"))
MAX_BYTES = 40 * 2**20  # skip giant fields (still covered by smaller siblings)
_SKIP_DIRS = {"postProcessing", "VTK", ".git", "__pycache__", "node_modules", ".conda-env", "logs",
              "figures", "reports", "_aethercfd_scratch", "dynamicCode", ".aethercfd"}


def iter_foam_files(root: Path = CORPUS_ROOT) -> Iterator[Path]:
    """Every file whose header contains ``FoamFile`` (skipping processor dirs and output folders)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("processor")
                       and not d.startswith(".")]
        for f in filenames:
            p = Path(dirpath) / f
            if p.suffix in (".py", ".png", ".pdf", ".gz", ".tar", ".zip", ".vtk", ".vtu", ".csv", ".dat",
                            ".json", ".md", ".tex", ".npz", ".npy", ".h5", ".yaml", ".yml", ".txt", ".log"):
                continue
            try:
                st = p.stat()
                if st.st_size == 0 or st.st_size > MAX_BYTES:
                    continue
                with open(p, "rb") as fh:
                    head = fh.read(2048)
            except OSError:
                continue
            if b"FoamFile" in head:
                yield p
