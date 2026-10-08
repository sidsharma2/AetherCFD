"""Create a case from a template: copy the tutorial out of the fork's installation (inside WSL), then
apply parameter changes as journalled dict.set commands in the new case (no Qt here)."""

from __future__ import annotations

from pathlib import Path

from aethercfd.backend.wsl.forks import ForkProfile, prelude
from aethercfd.core.paths import openfoam_path_safety, win_to_wsl
from aethercfd.templates.catalog import (
    Template,
    read_scalar,
    read_vector,
    set_scalar_text,
    set_vector_text,
)

_COPY = """\
src="$FOAM_TUTORIALS/$1"
if [ ! -d "$src" ]; then echo "AETHERCFD: no tutorial $1 in $WM_PROJECT_DIR" >&2; exit 3; fi
if [ -e "$2" ]; then echo "AETHERCFD: $2 already exists" >&2; exit 4; fi
mkdir -p "$(dirname "$2")"
cp -r "$src" "$2"
cd "$2" && ls
"""


def family_of(fork: ForkProfile) -> str:
    return str(fork.family.value)  # "esi" | "foundation"


def available(runner, fork: ForkProfile, templates: list[Template]) -> dict[str, bool]:
    """Which templates exist in this fork's tutorials (one WSL call)."""
    fam = family_of(fork)
    paths = [t.sources[fam] for t in templates if fam in t.sources]
    body = prelude(fork) + 'for p in "$@"; do [ -d "$FOAM_TUTORIALS/$p" ] && echo "$p"; done\n'
    r = runner.run_script(body, paths, timeout_s=60)
    have = set(r.stdout.split()) if r.ok else set()
    return {t.id: fam in t.sources and t.sources[fam] in have for t in templates}


def copy_tutorial(runner, fork: ForkProfile, t: Template, dest: Path) -> list[str]:
    fam = family_of(fork)
    if fam not in t.sources:
        raise ValueError(f"{t.title} is not available for {fork.label}")
    wsl_dest = win_to_wsl(str(dest))
    safety = openfoam_path_safety(wsl_dest)
    if not safety.safe:
        raise ValueError(f"OpenFOAM cannot use this folder ({', '.join(safety.problems)}); choose a path without "
                         "spaces or quotes")
    r = runner.run_script(prelude(fork) + _COPY, [t.sources[fam], wsl_dest], timeout_s=300)
    if not r.ok:
        raise RuntimeError((r.stderr or r.stdout).strip()[-800:] or f"copy failed ({r.returncode})")
    return r.stdout.split()


def current_values(case: Path, t: Template, family: str) -> dict[str, tuple[str, str, str]]:
    """param id -> (file, key, current value text) as found in the copied case."""
    from aethercfd.core.textio import decode_bytes
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    out = {}
    for p in t.params:
        if family not in p.where:
            continue
        rel, key = p.where[family]
        f = case / rel
        if not f.exists():
            continue
        doc = parse(decode_bytes(f.read_bytes())[0])
        lk = DictView(doc).lookup(key)
        if lk is not None and lk.entry is not None and lk.entry.value is not None:
            out[p.id] = (rel, key, doc.text(lk.entry.value.start, lk.entry.value.end).strip())
    return out


def new_value_text(kind: str, current: str, value) -> str:
    if kind == "scalar":
        return set_scalar_text(current, value)
    if kind == "vector_x":
        return set_vector_text(current, [float(value), 0.0, 0.0], only_x=True)
    if kind == "vector":
        vec = value if isinstance(value, (list, tuple)) else [float(x) for x in str(value).strip("() ").split()]
        return set_vector_text(current, vec)
    return str(value)


def display_value(kind: str, current: str):
    if kind == "scalar":
        return read_scalar(current)
    if kind in ("vector", "vector_x"):
        v = read_vector(current)
        return (v[0] if kind == "vector_x" else v) if v else None
    return current
