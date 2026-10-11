"""Semantic view over a parsed dictionary: keyword lookup with OpenFOAM's rules, macro resolution
within the file, and *provenance* of every value.

OpenFOAM lookup rules modelled here:
- a later duplicate keyword overrides an earlier one ("last wins");
- an exact (unquoted) keyword beats a quoted regex keyword; regex keywords are tried last-first;
- ``$name`` searches the current dictionary and then its parents; ``$:a.b`` is absolute from the top;
  ``$..name`` starts one level up; ``$a.b`` descends with '.' as the scope separator.

Evaluation of #calc/#eval/#codeStream and #include contents is *never* attempted (ADR 0002): such
values are reported as non-literal so forms can show them read-only with "edit at source".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from aethercfd.foamdict.cst import (
    Atom,
    DictNode,
    Directive,
    Entry,
    FoamDocument,
    Key,
    ListNode,
    MacroEntry,
)
from aethercfd.foamdict.tokens import K


class Provenance(StrEnum):
    LITERAL = "literal"  # plain value written right here
    INHERITED = "inherited"  # value is (or contains) a $macro reference
    COMPUTED = "computed"  # #calc, #eval, #codeStream, #{ code #}
    INCLUDE = "include"  # not found locally, but an #include / $merge could supply it
    DICT = "dict"  # the entry is a sub-dictionary


@dataclass(frozen=True)
class Lookup:
    entry: Entry | None  # the effective entry (None for INCLUDE)
    parent: DictNode
    provenance: Provenance
    matched_pattern: bool = False
    sources: tuple[str, ...] = ()  # macro names / include args responsible for the value

    @property
    def editable_here(self) -> bool:
        """Can a form safely rewrite this value in place?"""
        return self.entry is not None and self.provenance in (Provenance.LITERAL, Provenance.DICT)


def split_path(path: str | Sequence[str]) -> list[str]:
    if isinstance(path, str):
        return [p for p in path.split("/") if p]
    return list(path)


class DictView:
    """Read-only semantic helpers over a FoamDocument."""

    def __init__(self, doc: FoamDocument):
        self.doc = doc

    # ------------------------------------------------------------------ basic lookup
    def find_in(self, d: DictNode, key: str) -> tuple[Entry | None, bool]:
        """Effective entry for ``key`` directly inside ``d`` -> (entry, matched_by_pattern)."""
        exact = None
        patterns: list[Entry] = []
        for m in d.members:
            if isinstance(m, Entry):
                if m.key.is_pattern:
                    patterns.append(m)
                elif m.key.name == key:
                    exact = m  # last wins
        if exact is not None:
            return exact, False
        for e in reversed(patterns):  # the pattern entry itself, addressed by its own text: "(k|epsilon)"
            if e.key.name == key:
                return e, False
        for e in reversed(patterns):
            try:
                if re.fullmatch(e.key.name, key):
                    return e, True
            except re.error:
                continue
        if d is self.doc.root:
            hit = self.stream_entries().get(key)
            if hit is not None:
                return hit, False
        return None, False

    def stream_entries(self) -> dict[str, Entry]:
        """Named dictionaries of a root-level stream list -- ``N ( name { ... } name { ... } )`` as in
        constant/polyMesh/boundary -- as entries addressable by name (``"inlet/type"``). The returned
        Entry spans the name and its dictionary, so edits inside it work like in any sub-dictionary."""
        cached = getattr(self, "_streams", None)
        if cached is not None:
            return cached
        out: dict[str, Entry] = {}
        for m in self.doc.root.members:
            if not (isinstance(m, Entry) and m.value is not None and Atom(K.WORD, 0, 0, m.key.name).is_number):
                continue
            lists = [i for i in m.value.items if isinstance(i, ListNode)]
            if len(lists) != 1:
                continue
            items = lists[0].items
            for a, b in pairwise(items):
                if isinstance(a, Atom) and a.kind is K.WORD and isinstance(b, DictNode):
                    out[a.text] = Entry(a.start, b.end, Key(K.WORD, a.start, a.end, a.text), dict=b)
        self._streams = out
        return out

    def lookup(self, path: str | Sequence[str]) -> Lookup | None:
        parts = split_path(path)
        if not parts:
            return None
        d = self.doc.root
        via_pattern = False  # did any step of the path match a regex key?
        for i, name in enumerate(parts):
            e, pat = self.find_in(d, name)
            last = i == len(parts) - 1
            if e is None:
                # "$other;" merges inside this file: follow them (last merge wins)
                for m in reversed(d.members):
                    if isinstance(m, MacroEntry):
                        src_e = self.resolve_macro(m.macro, from_path=parts[:i])
                        if src_e is not None and src_e.dict is not None:
                            hit, hpat = self.find_in(src_e.dict, name)
                            if hit is not None:
                                if last:
                                    return Lookup(hit, src_e.dict, Provenance.INHERITED,
                                                  matched_pattern=hpat or via_pattern, sources=(m.macro,))
                                if hit.dict is None:
                                    return None
                                e, pat = hit, hpat
                                via_pattern = via_pattern or hpat
                                break
                if e is None:
                    srcs = self._merge_sources(d)
                    if srcs:
                        return Lookup(None, d, Provenance.INCLUDE, sources=tuple(srcs))
                    return None
                d = e.dict  # type: ignore[assignment]
                continue
            via_pattern = via_pattern or pat
            if last:
                return Lookup(e, d, self.provenance_of(e), matched_pattern=via_pattern,
                              sources=self._macro_names(e))
            if e.dict is None:
                return None  # path goes through a non-dictionary entry
            d = e.dict
        return None

    def get_text(self, path: str | Sequence[str]) -> str | None:
        lk = self.lookup(path)
        if lk is None or lk.entry is None:
            return None
        e = lk.entry
        if e.dict is not None:
            return self.doc.text(e.dict.start, e.dict.end)
        if e.value is None:
            return ""
        return self.doc.text(e.value.start, e.value.end)

    def keys(self, path: str | Sequence[str] = ()) -> list[str]:
        d = self.dict_at(path)
        if d is None:
            return []
        seen: dict[str, None] = {}
        for m in d.members:
            if isinstance(m, Entry):
                seen[m.key.name] = None
        return list(seen)

    def dict_at(self, path: str | Sequence[str]) -> DictNode | None:
        d = self.doc.root
        for name in split_path(path):
            e, _ = self.find_in(d, name)
            if e is None or e.dict is None:
                return None
            d = e.dict
        return d

    # ------------------------------------------------------------------ provenance
    def provenance_of(self, e: Entry) -> Provenance:
        if e.dict is not None:
            return Provenance.DICT
        if e.value is None:
            return Provenance.LITERAL
        for it in e.value._walk():
            if isinstance(it, Atom) and it.kind in (K.DIRECTIVE, K.VERBATIM):
                return Provenance.COMPUTED
        for it in e.value._walk():
            if isinstance(it, Atom) and it.kind is K.MACRO:
                return Provenance.INHERITED
        return Provenance.LITERAL

    def _macro_names(self, e: Entry) -> tuple[str, ...]:
        if e.value is None:
            return ()
        return tuple(it.text for it in e.value._walk() if isinstance(it, Atom) and it.kind is K.MACRO)

    def _merge_sources(self, d: DictNode) -> list[str]:
        out = []
        for m in d.members:
            if isinstance(m, Directive) and m.name.startswith("#include"):
                out.append(f"{m.name} {m.args_text}".strip())
            elif isinstance(m, MacroEntry):
                out.append(m.macro)
        return out

    # ------------------------------------------------------------------ macros
    def resolve_macro(self, macro: str, *, from_path: str | Sequence[str] = ()) -> Entry | None:
        """Find the entry a ``$macro`` refers to (within this file), or None.

        ``from_path`` is the dictionary path where the macro appears (for relative scoping).
        """
        name = macro[1:]
        if name.startswith("{") and name.endswith("}"):
            name = name[1:-1]
        scope = split_path(from_path)
        chain = [self.doc.root]
        d = self.doc.root
        for part in scope:
            e, _ = self.find_in(d, part)
            if e is None or e.dict is None:
                break
            d = e.dict
            chain.append(d)
        if name.startswith(":"):
            return self._descend(self.doc.root, name[1:].split("."))
        ups = 0
        while name.startswith("."):
            ups += 1
            name = name[1:]
        if ups:
            idx = max(0, len(chain) - ups)
            return self._descend(chain[idx - 1] if idx > 0 else chain[0], name.split("."))
        parts = name.replace("/", ".").split(".")
        for dd in reversed(chain):  # current dict, then parents
            hit = self._descend(dd, parts)
            if hit is not None:
                return hit
        return None

    def _descend(self, d: DictNode, parts: list[str]) -> Entry | None:
        e = None
        for i, p in enumerate(parts):
            e, _ = self.find_in(d, p)
            if e is None:
                return None
            if i < len(parts) - 1:
                if e.dict is None:
                    return None
                d = e.dict
        return e

    # ------------------------------------------------------------------ header
    def header(self) -> dict[str, str]:
        e, _ = self.find_in(self.doc.root, "FoamFile")
        if e is None or e.dict is None:
            return {}
        out = {}
        for m in e.dict.entries():
            if m.value is not None:
                out[m.key.name] = self.doc.text(m.value.start, m.value.end).strip().strip('"')
        return out


def path_at(doc, offset: int) -> list[str]:
    """Keyword path of the innermost entry whose span contains ``offset`` (e.g. the editor cursor):
    ['PIMPLE', 'nOuterCorrectors'] -- [] outside any entry."""
    from aethercfd.foamdict.cst import Entry

    path: list[str] = []
    d = doc.root
    while d is not None:
        hit = None
        for m in d.members:
            if isinstance(m, Entry) and m.start <= offset <= m.end:
                hit = m
                break
        if hit is None:
            break
        path.append(hit.key.name)
        d = hit.dict
    return path
