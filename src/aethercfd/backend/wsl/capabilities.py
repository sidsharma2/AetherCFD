"""What an installed OpenFOAM fork can do: applications, boundary-condition types, function objects,
fvOptions/fvModels/fvConstraints, turbulence models, solver modules. Probed once per fork build and
cached (forms use it for dropdowns; the validator uses it to flag unknown types)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from aethercfd.backend.wsl.forks import Family, ForkProfile, prelude
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.core.settings import cache_dir


@dataclass
class Capabilities:
    fork_id: str
    build: str = ""
    probed_at: str = ""
    applications: list[str] = field(default_factory=list)
    tables: dict[str, list[str]] = field(default_factory=dict)  # e.g. "scalarBCs", "RAS", "functionObjects"

    def get(self, name: str) -> list[str]:
        return self.tables.get(name, [])

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)


_ESI_LISTS = ["listScalarBCs", "listVectorBCs", "listFunctionObjects", "listFvOptions", "listTurbulenceModels"]
_SECTION_ESI = re.compile(r"^@@(\w+)$")
_FOAM_LIST = re.compile(r"^(\d+)\s*$")
_INLINE_LIST = re.compile(r"^(\d+)\((.*)\)\s*$")
_FUSED_LIST = re.compile(r"^([A-Za-z_]+?)(\d+)\((.*)\)\s*$")  # ESI: "functionObjects7(a b c)"
_TABLE_ALL = re.compile(r"^(\S+) \((\S*)\s*$")  # foamToC -all: "fvPatch (patch " then N ( entries ))
_TABLE_ORG = re.compile(r"^Contents of table (\S+), base type (\S+):\s*$")


def _parse_esi_lists(text: str) -> dict[str, list[str]]:
    """Parse '<title>\\nN\\n(\\n a\\n b\\n)' and '<title>\\nN(a b)' blocks (OpenFOAM list output)."""
    out: dict[str, list[str]] = {}
    lines = text.splitlines()
    i = 0
    title = None
    while i < len(lines):
        ln = lines[i].strip()
        if m := _FUSED_LIST.match(ln):
            out[m.group(1)] = m.group(3).split()
            i += 1
            continue
        if m := _INLINE_LIST.match(ln):
            if title:
                out[title] = m.group(2).split()
            i += 1
            continue
        if _FOAM_LIST.match(ln) and i + 1 < len(lines) and lines[i + 1].strip() == "(":
            items = []
            i += 2
            while i < len(lines) and lines[i].strip() != ")":
                if lines[i].strip():
                    items.append(lines[i].strip())
                i += 1
            if title:
                out[title] = items
            i += 1
            continue
        if ln and not ln.startswith(("//", "\\", "|", "/*", "*", "Build", "Arch", "Exec", "Date", "Time", "Host",
                                     "PID", "I/O", "Case", "nProcs", "trapFpe", "fileModification", "allowSystem",
                                     "sigFpe", "-->")):
            title = ln.rstrip(":")
        i += 1
    return out


def _parse_foamtoc(text: str) -> dict[str, list[str]]:
    """Parse ``foamToC -all`` ("Name (base
N
(
entry lib
...
))") and the single-table format."""
    out: dict[str, list[str]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _TABLE_ALL.match(lines[i])
        if m and i + 2 < len(lines) and lines[i + 1].strip().isdigit() and lines[i + 2].strip() == "(":
            name = m.group(1)
            items = []
            i += 3
            while i < len(lines) and not lines[i].strip().startswith(")"):
                if lines[i].strip():
                    items.append(lines[i].split()[0])
                i += 1
            out[name] = items
        i += 1
    if out:
        return out
    cur = None
    for ln in lines:
        m = _TABLE_ORG.match(ln.strip())
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
            continue
        if cur and ln.startswith("    ") and ln.strip():
            out[cur].append(ln.split()[0])
        elif not ln.strip():
            cur = None
    return out


def probe_capabilities(runner: WslRunner, fork: ForkProfile) -> Capabilities:
    pre = prelude(fork)
    if fork.family is Family.ESI:
        body = pre + 'echo "@@BUILD $WM_PROJECT_VERSION"\nls "$FOAM_APPBIN" | sed "s/^/@@APP /"\n'
        # ESI only lists objects from libraries already loaded; load the standard function-object libs
        fo_libs = "'(fieldFunctionObjects utilityFunctionObjects forces solverFunctionObjects lagrangianFunctionObjects)'"
        for opt in _ESI_LISTS:
            extra = f" -lib {fo_libs}" if opt == "listFunctionObjects" else ""
            body += f'echo "@@{opt}"\nsimpleFoam -{opt}{extra} 2>/dev/null\n'
    else:
        body = pre + ('echo "@@BUILD $WM_PROJECT_VERSION"\nls "$FOAM_APPBIN" | sed "s/^/@@APP /"\n'
                      "foamToC -all 2>/dev/null\n")
    res = runner.run_script(body, timeout_s=300)
    text = res.stdout
    caps = Capabilities(fork.id, probed_at=datetime.now(UTC).isoformat(timespec="seconds"))
    caps.applications = sorted(ln[6:].strip() for ln in text.splitlines() if ln.startswith("@@APP "))
    b = next((ln for ln in text.splitlines() if ln.startswith("@@BUILD ")), "")
    caps.build = b[8:].strip()
    if fork.family is Family.ESI:
        chunks = re.split(r"^@@(list\w+)\s*$", text, flags=re.MULTILINE)
        for name, body_txt in zip(chunks[1::2], chunks[2::2], strict=False):
            parsed = _parse_esi_lists(body_txt)
            if name == "listTurbulenceModels":
                for k, v in parsed.items():
                    caps.tables[k.replace(" models", "")] = v
            else:
                key = {"listScalarBCs": "scalarBCs", "listVectorBCs": "vectorBCs",
                       "listFunctionObjects": "functionObjects", "listFvOptions": "fvOptions"}[name]
                caps.tables[key] = next(iter(parsed.values()), [])
    else:
        tables = _parse_foamtoc(text)
        alias = {"fvPatchScalarField": "scalarBCs", "fvPatchVectorField": "vectorBCs",
                 "scalarFvPatchField": "scalarBCs", "vectorFvPatchField": "vectorBCs",
                 "functionObject": "functionObjects", "fvModel": "fvModels", "fvConstraint": "fvConstraints",
                 "solver": "solvers"}
        for k, v in tables.items():
            caps.tables[alias.get(k, k)] = sorted(set(v))
        for k in list(caps.tables):  # e.g. RASincompressibleMomentumTransportModel -> RAS
            for prefix in ("RAS", "LES"):
                if k.startswith(prefix) and "MomentumTransportModel" in k:
                    caps.tables.setdefault(prefix, [])
                    caps.tables[prefix] = sorted(set(caps.tables[prefix]) | set(caps.tables[k]))
    return caps


def load_capabilities(runner: WslRunner, fork: ForkProfile, *, refresh: bool = False) -> Capabilities:
    path = cache_dir() / "forks" / f"{fork.distro}--{fork.id}.capabilities.json"
    if path.exists() and not refresh:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Capabilities(**d)
    caps = probe_capabilities(runner, fork)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(caps.to_json(), encoding="utf-8")
    return caps
