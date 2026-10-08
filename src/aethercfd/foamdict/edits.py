"""Minimal, style-preserving edits to OpenFOAM dictionaries.

Every operation returns a list of ``TextEdit`` (character spans + replacement text) against the
document's exact source. Nothing outside those spans changes, so comments, alignment, blank lines,
one-line packing and macros elsewhere in the file survive any edit.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from aethercfd.foamdict.cst import DictNode, Entry, FoamDocument
from aethercfd.foamdict.semantic import DictView, Provenance, split_path


class EditError(ValueError):
    pass


class NotFound(EditError):
    pass


@dataclass(frozen=True, slots=True)
class TextEdit:
    start: int
    end: int
    text: str


def apply_edits(src: str, edits: Iterable[TextEdit]) -> str:
    es = sorted(edits, key=lambda e: (e.start, e.end))
    for a, b in itertools.pairwise(es):
        if b.start < a.end:
            raise EditError(f"overlapping edits at {a.start}-{a.end} and {b.start}-{b.end}")
    out = src
    for e in reversed(es):
        out = out[: e.start] + e.text + out[e.end:]
    return out


def to_foam(value: object) -> str:
    """Python value -> OpenFOAM text. Strings are taken as raw OpenFOAM text (not quoted)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise EditError(f"cannot write {value!r} to an OpenFOAM dictionary")
        s = f"{value:.12g}"
        return s
    if isinstance(value, list | tuple):
        return "(" + " ".join(to_foam(v) for v in value) + ")"
    if isinstance(value, str):
        return value
    raise EditError(f"unsupported value type {type(value).__name__}")


def _line_start(src: str, pos: int) -> int:
    return src.rfind("\n", 0, pos) + 1


def _indent_of_line(src: str, pos: int) -> str:
    """Leading whitespace of the line containing pos."""
    ls = _line_start(src, pos)
    i = ls
    while i < len(src) and src[i] in " 	":
        i += 1
    return src[ls:i]


def _line_end(src: str, pos: int) -> int:
    """Index of the '\\n' ending the line containing pos (or len(src))."""
    e = src.find("\n", pos)
    return len(src) if e < 0 else e


def set_value(doc: FoamDocument, path: str | Sequence[str], text: str, *,
              allow_nonliteral: bool = False) -> list[TextEdit]:
    """Replace the value of an existing keyword. Raises NotFound / EditError with a reason a form can
    show (non-literal values, pattern matches and sub-dictionaries are refused)."""
    view = DictView(doc)
    lk = view.lookup(path)
    p = "/".join(split_path(path))
    if lk is None:
        raise NotFound(f"{p!r} is not set in this file")
    if lk.entry is None:
        raise NotFound(f"{p!r} is not set in this file (it may come from {', '.join(lk.sources)})")
    e = lk.entry
    if lk.matched_pattern:
        raise EditError(f"{p!r} is only matched by the pattern key {e.key.text}; editing it would change "
                        "every keyword that pattern matches. Insert an explicit entry instead.")
    if e.dict is not None:
        raise EditError(f"{p!r} is a sub-dictionary, not a value")
    if not allow_nonliteral and lk.provenance is not Provenance.LITERAL:
        raise EditError(f"{p!r} is {lk.provenance.value} ({' '.join(lk.sources) or 'computed'}); edit it at its source")
    if e.value is None or not e.value.items:
        if e.semi is not None:
            return [TextEdit(e.key.end, e.semi, " " + text)]
        return [TextEdit(e.key.end, e.key.end, " " + text + ";")]
    if e.semi is None:  # repair a missing ';' while we are here
        return [TextEdit(e.value.start, e.value.end, text + ";")]
    return [TextEdit(e.value.start, e.value.end, text)]


def _value_column(doc: FoamDocument, d: DictNode) -> int | None:
    """The column most sibling values start at, if siblings visibly align their values."""
    src = doc.source
    cols = []
    for m in d.entries():
        if m.value is None or not m.value.items or m.dict is not None:
            continue
        if "\n" in src[m.key.end:m.value.start]:
            continue
        ls = _line_start(src, m.key.start)
        if src[ls:m.key.start].strip():
            continue
        cols.append(m.value.start - ls)
    if len(cols) < 2:
        return None
    col, n = Counter(cols).most_common(1)[0]
    return col if n >= max(2, len(cols) // 2) else None


def insert_entry(doc: FoamDocument, dict_path: str | Sequence[str], key: str, text: str, *,
                 newline: str = "\n") -> list[TextEdit]:
    """Append ``key text;`` to the dictionary at ``dict_path`` (root if empty), following the
    surrounding indentation, value alignment and one-line packing."""
    view = DictView(doc)
    parts = split_path(dict_path)
    d = view.dict_at(parts) if parts else doc.root
    if d is None:
        raise NotFound(f"dictionary {'/'.join(parts)!r} does not exist")
    existing, pat = view.find_in(d, key)
    if existing is not None and not pat:
        raise EditError(f"{key!r} already exists in {'/'.join(parts) or 'the top level'}; use set_value")
    src = doc.source
    members = d.members
    is_root = d is doc.root
    # one-line dictionary:  name { type wall; value uniform 0; }
    if not is_root and d.close_brace is not None and d.open_brace is not None \
            and "\n" not in src[d.open_brace:d.close_brace]:
        pos = d.close_brace
        lead = "" if src[pos - 1:pos] == " " else " "
        return [TextEdit(pos, pos, f"{lead}{key} {text}; ")]
    if members:
        last = members[-1]
        ls = _line_start(src, last.start)
        indent = src[ls:last.start] if not src[ls:last.start].strip() else ""
        if not indent and not is_root:
            indent = _indent_of_line(src, d.open_brace or 0) + "    "
    else:
        indent = "" if is_root else _indent_of_line(src, d.open_brace or 0) + "    "
    col = _value_column(doc, d)
    pad = " "
    if col is not None:
        pad = " " * max(1, col - len(indent) - len(key))
    line = f"{indent}{key}{pad}{text};"
    if members:
        last = members[-1]
        le = _line_end(src, last.end)
        if not is_root and d.close_brace is not None and d.close_brace < le:
            # "... last 1; }" -- the closing brace shares the last member's line
            return [TextEdit(last.end, last.end, f"{newline}{line}")]
        if le >= len(src):
            return [TextEdit(len(src), len(src), f"{newline}{line}{newline}")]
        return [TextEdit(le + 1, le + 1, f"{line}{newline}")]
    # empty dictionary
    if is_root:
        lead = "" if (not src or src.endswith("\n")) else newline
        return [TextEdit(len(src), len(src), lead + line + newline)]
    ob = d.open_brace or 0
    le = _line_end(src, ob)
    if d.close_brace is not None and d.close_brace < le:  # "name {}" on one line
        return [TextEdit(ob + 1, ob + 1, f" {key} {text}; ")]
    return [TextEdit(le + 1, le + 1, f"{line}{newline}")]


def set_or_insert(doc: FoamDocument, path: str | Sequence[str], text: str, *,
                  newline: str = "\n") -> list[TextEdit]:
    parts = split_path(path)
    try:
        return set_value(doc, parts, text)
    except NotFound:
        return insert_entry(doc, parts[:-1], parts[-1], text, newline=newline)


def remove_entry(doc: FoamDocument, path: str | Sequence[str]) -> list[TextEdit]:
    """Remove a keyword (and its whole line when it sits alone on it, trailing comment included)."""
    view = DictView(doc)
    lk = view.lookup(path)
    if lk is None or lk.entry is None:
        raise NotFound(f"{'/'.join(split_path(path))!r} is not set in this file")
    if lk.matched_pattern:
        raise EditError("refusing to remove a pattern entry through a keyword it happens to match")
    e: Entry = lk.entry
    src = doc.source
    ls = _line_start(src, e.start)
    le = _line_end(src, e.end)
    before = src[ls:e.start]
    after = src[e.end:le]
    after_code = after.split("//", 1)[0]
    if not before.strip() and not after_code.strip():
        return [TextEdit(ls, min(le + 1, len(src)), "")]
    end = e.end
    while end < len(src) and src[end] == " ":
        end += 1
    return [TextEdit(e.start, end, "")]
