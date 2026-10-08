"""Concrete syntax tree for OpenFOAM dictionaries.

The tree is a *view* over the source text: every node records character spans into it, and nothing
is ever printed from the tree. Edits are computed as text replacements on those spans (see
``edits.py``), which keeps unedited bytes untouched by construction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from aethercfd.foamdict.tokens import K, Token


@dataclass(slots=True)
class Diagnostic:
    start: int
    end: int
    message: str
    severity: str = "error"  # error | warning


@dataclass(slots=True)
class Atom:
    """A single value token: word/number, string, macro, directive, verbatim code, blob."""

    kind: K
    start: int
    end: int
    text: str

    @property
    def is_number(self) -> bool:
        return self.kind is K.WORD and _is_number(self.text)


@dataclass(slots=True)
class ListNode:
    """( ... ) — items may be atoms, nested lists, dimension sets, or dictionaries."""

    start: int  # at the "N" count prefix if present, else at '('
    end: int  # after ')'
    items: list[Item] = field(default_factory=list)
    count: int | None = None  # the "N" prefix of an "N(...)" list, if any
    lparen: int = -1  # offset of '(' (or '{' for a uniform list)
    uniform: bool = False  # "N{value}": N copies of one value
    blob: bool = False  # payload skipped as one BLOB token (large/binary list)


@dataclass(slots=True)
class DimNode:
    """[ ... ] — dimension set, e.g. [0 1 -1 0 0 0 0]."""

    start: int
    end: int
    items: list[Atom] = field(default_factory=list)


type Item = Atom | ListNode | DimNode | DictNode  # lazy alias: DictNode is defined below


@dataclass(slots=True)
class Value:
    """Everything between a keyword and its ';' (exclusive), excluding surrounding trivia."""

    start: int
    end: int
    items: list[Item] = field(default_factory=list)

    @property
    def atoms(self) -> list[Atom]:
        return [i for i in self.items if isinstance(i, Atom)]

    def has_kind(self, *kinds: K) -> bool:
        return any(isinstance(i, Atom) and i.kind in kinds for i in self._walk())

    def _walk(self) -> Iterator[Item]:
        stack: list[Item] = list(self.items)
        while stack:
            it = stack.pop()
            yield it
            if isinstance(it, ListNode | DimNode):
                stack.extend(it.items)


@dataclass(slots=True)
class Key:
    kind: K  # WORD, STRING (quoted/regex key) or MACRO
    start: int
    end: int
    text: str  # raw source text (quotes included for STRING)

    @property
    def name(self) -> str:
        """Key with surrounding quotes removed."""
        if self.kind is K.STRING and len(self.text) >= 2:
            return self.text[1:-1]
        return self.text

    @property
    def is_pattern(self) -> bool:
        return self.kind is K.STRING


@dataclass(slots=True)
class Entry:
    """``key value;`` or ``key { ... }`` (then ``dict`` is set and ``value`` is None)."""

    start: int  # key start
    end: int  # after ';' or '}' (or last token on error)
    key: Key
    value: Value | None = None
    dict: DictNode | None = None
    semi: int | None = None  # index of ';' (None if missing)


@dataclass(slots=True)
class Directive:
    """Top-level or in-dictionary directive: #include "x", #includeFunc f(a=1), #remove k ..."""

    start: int
    end: int
    name: str  # e.g. "#include"
    args: list[Item] = field(default_factory=list)
    args_text: str = ""


@dataclass(slots=True)
class MacroEntry:
    """A bare ``$name;`` line inside a dictionary — merges another dictionary's entries."""

    start: int
    end: int
    macro: str


type Member = Entry | Directive | MacroEntry


@dataclass(slots=True)
class DictNode:
    start: int  # at '{' (or 0 for the file root)
    end: int  # after '}' (or len(src) for the root)
    members: list[Member] = field(default_factory=list)
    open_brace: int | None = None
    close_brace: int | None = None  # None if unterminated (or root)

    def entries(self) -> list[Entry]:
        return [m for m in self.members if isinstance(m, Entry)]


@dataclass
class FoamDocument:
    """Parse result: root dictionary + diagnostics, over an exact source string."""

    source: str
    tokens: list[Token]
    root: DictNode
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def text(self, start: int, end: int) -> str:
        return self.source[start:end]

    def line_col(self, offset: int) -> tuple[int, int]:
        """1-based (line, column) for a character offset."""
        line = self.source.count("\n", 0, offset) + 1
        col = offset - (self.source.rfind("\n", 0, offset) + 1) + 1
        return line, col


def _is_number(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return s.lower() not in ("nan", "inf", "-inf", "+inf", "infinity", "-infinity")
