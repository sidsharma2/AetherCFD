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
# -L: tutorials share dictionaries through symlinks (v2606 multiRegionHeater: system/decomposeParDict ->
# decomposeParDict.4); a WSL symlink on a Windows drive cannot be opened from Windows (WinError 1920)
cp -rL "$src" "$2"
cd "$2" || exit 5
# what the tutorial's Allrun does first (restore0Dir): 0.orig -> 0, and system/*.orig where missing
if [ -d 0.orig ] && [ ! -e 0 ]; then cp -r 0.orig 0; fi
for f in system/*.orig; do [ -e "$f" ] && [ ! -e "${f%.orig}" ] && cp "$f" "${f%.orig}"; done
ls
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


def write_interval_for_end(control_text: str) -> tuple[str, str] | None:
    """When endTime is not a multiple of writeInterval, the last iterations are never written (a beginner sets
    120 iterations, the tutorial writes every 100: results stop at 100). Returns (new writeInterval, why) so
    the end time is written, or None when it already is."""
    import math

    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    v = DictView(parse(control_text))

    def num(key):
        try:
            return float(v.get_text(key) or "nan")
        except ValueError:
            return float("nan")

    ctrl = (v.get_text("writeControl") or "timeStep").strip()
    end, wi, dt = num("endTime"), num("writeInterval"), num("deltaT")
    if not all(math.isfinite(x) and x > 0 for x in (end, wi)):
        return None
    if ctrl == "timeStep":
        steps = round(end / dt) if math.isfinite(dt) and dt > 0 else round(end)
        every = max(1, round(wi))
        if steps <= every or steps % every == 0:
            return None if steps % every == 0 else (str(steps), f"writeInterval {every} -> {steps}: the end "
                                                                 f"({steps} steps) comes before the first write")
        new = max(d for d in range(1, every + 1) if steps % d == 0)
        if new < every / 2:  # e.g. 2003 steps: only 1 divides it -> thousands of time folders; never do that
            return None, (f"the last {steps % every} of the {steps} steps will not be written (writeInterval "
                          f"{every}); choose an end that is a multiple of {every} to keep them")
        return f"{new}", f"writeInterval {every} -> {new} so the last of the {steps} steps is written"
    if ctrl in ("runTime", "adjustableRunTime"):
        ratio = end / wi
        if abs(ratio - round(ratio)) < 1e-9 * max(1.0, ratio):
            return None
        new = end / math.ceil(ratio)  # at most 1 more write than before, and the end is a write time
        return f"{new!r}", f"writeInterval {wi:g} -> {new:.6g} so the end time {end:g} is written"
    return None


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
