"""Lossless tokenizer for OpenFOAM dictionaries and field files.

Invariant (tested on every dictionary under D:\\OpenFOAM): the tokens tile the source exactly —
``"".join(src[t.start:t.end] for t in tokens) == src`` — for *any* input, valid or not.

Text is expected to come from :func:`aethercfd.core.textio.decode_bytes` (binary field files decode
as latin-1, so one character == one byte and binary payload lengths can be skipped exactly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum


class K(IntEnum):
    WS = 0
    LINE_COMMENT = 1
    BLOCK_COMMENT = 2
    STRING = 3
    VERBATIM = 4  # #{ ... #}
    DIRECTIVE = 5  # #include, #calc, #codeStream ...
    MACRO = 6  # $var, $:a.b, ${...}
    WORD = 7  # keywords, numbers, div(phi,U), List<scalar>, uniform ...
    LBRACE = 8
    RBRACE = 9
    LPAREN = 10
    RPAREN = 11
    LBRACKET = 12
    RBRACKET = 13
    SEMI = 14
    BLOB = 15  # skipped payload of a large/binary list (between its parentheses)
    ERROR = 16  # a stray/unterminated fragment; still covers its bytes

    @property
    def trivia(self) -> bool:
        return self in (K.WS, K.LINE_COMMENT, K.BLOCK_COMMENT)


@dataclass(frozen=True, slots=True)
class Token:
    kind: K
    start: int
    end: int
    error: str | None = None


_PUNCT = {"{": K.LBRACE, "}": K.RBRACE, "(": K.LPAREN, ")": K.RPAREN,
          "[": K.LBRACKET, "]": K.RBRACKET, ";": K.SEMI}
_WS = re.compile(r"[ \t\r\n\f\v]+")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_DIRECTIVE = re.compile(r"#[A-Za-z_][A-Za-z0-9_]*")
_MACRO = re.compile(r"\$\{[^}\n]*\}|\$[^\s;{}()\[\]\"]+")
_WORD_RUN = re.compile(r"[^\s\";{}\[\]()/]+")
_INT = re.compile(r"[0-9]+$")
_LIST_TYPE = re.compile(r"List<(\w+)>")
_ARCH_LABEL = re.compile(r"label\s*=\s*(\d+)")
_ARCH_SCALAR = re.compile(r"scalar\s*=\s*(\d+)")

# components per element for binary payload sizes
_NCOMP = {"scalar": 1, "vector": 3, "sphericalTensor": 1, "symmTensor": 6, "tensor": 9,
          "vector2D": 2, "complex": 2}
# header "class" -> element type, for files whose list carries no List<T> keyword (polyMesh, *Field)
_CLASS_TYPE = {"vectorField": "vector", "scalarField": "scalar", "labelList": "label",
               "faceCompactList": "label", "cellCompactList": "label",
               "symmTensorField": "symmTensor", "tensorField": "tensor"}

#: lists with at least this many elements are collapsed into one BLOB token in ASCII files
BLOB_THRESHOLD = 64


@dataclass(frozen=True)
class FileFormat:
    binary: bool = False
    label_bytes: int = 4
    scalar_bytes: int = 8
    header_class: str | None = None


_FORMAT_RE = re.compile(r"\bformat\s+(ascii|binary)\s*;")
_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z0-9_<>]+)\s*;")
_ARCH_RE = re.compile(r'\barch\s+"([^"]*)"')


def sniff_format(text: str) -> FileFormat:
    """Read format/arch/class from the FoamFile header (first few KB only)."""
    head = text[:4096]
    hb = head.find("FoamFile")
    if hb < 0:
        return FileFormat()
    he = head.find("}", hb)
    hdr = head[hb: he if he > 0 else len(head)]
    m = _FORMAT_RE.search(hdr)
    binary = bool(m and m.group(1) == "binary")
    lb, sb = 4, 8
    a = _ARCH_RE.search(hdr)
    if a:
        if (ml := _ARCH_LABEL.search(a.group(1))) is not None:
            lb = int(ml.group(1)) // 8
        if (ms := _ARCH_SCALAR.search(a.group(1))) is not None:
            sb = int(ms.group(1)) // 8
    c = _CLASS_RE.search(hdr)
    return FileFormat(binary=binary, label_bytes=lb, scalar_bytes=sb, header_class=c.group(1) if c else None)


def _element_bytes(type_name: str | None, fmt: FileFormat) -> int | None:
    if type_name is None:
        return None
    if type_name in ("label", "bool"):
        return fmt.label_bytes if type_name == "label" else 1
    n = _NCOMP.get(type_name)
    return n * fmt.scalar_bytes if n else None


def _scan_word(src: str, i: int, n: int) -> int:
    """OpenFOAM word: runs of plain chars; '(' ')' allowed inside with nesting (div(phi,U)), an
    unmatched ')' ends the word; '/' allowed unless it starts a comment."""
    depth = 0
    start = i
    while i < n:
        m = _WORD_RUN.match(src, i)
        if m:
            i = m.end()
            if i >= n:
                break
        c = src[i]
        if c == "(":
            if depth == 0 and src[start:i].isdigit():
                break  # "5170(" / "4(0 1 2 3)": a list count, not part of a word
            depth += 1
            i += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
            i += 1
        elif c == "/":
            if i + 1 < n and src[i + 1] in "/*":
                break
            i += 1
        else:  # whitespace, quote, ; { } [ ]
            break
    return i


def _skip_ascii_list(src: str, i: int, n: int) -> int:
    """Return the index of the ')' that closes a list whose '(' ended just before ``i``."""
    depth = 0
    pos = i
    find = src.find
    while True:
        rp = find(")", pos)
        if rp < 0:
            return -1
        lp = find("(", pos, rp)
        if lp < 0:
            if depth == 0:
                return rp
            depth -= 1
            pos = rp + 1
        else:
            depth += 1
            pos = lp + 1


def tokenize(src: str, fmt: FileFormat | None = None) -> list[Token]:
    if fmt is None:
        fmt = sniff_format(src)
    toks: list[Token] = []
    append = toks.append
    n = len(src)
    i = 0
    last_word: str | None = None  # previous significant WORD text
    list_type: str | None = _CLASS_TYPE.get(fmt.header_class or "")
    while i < n:
        c = src[i]
        if c in " \t\r\n\f\v":
            m = _WS.match(src, i)
            append(Token(K.WS, i, m.end()))  # type: ignore[union-attr]
            i = m.end()  # type: ignore[union-attr]
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            m = _LINE_COMMENT.match(src, i)
            append(Token(K.LINE_COMMENT, i, m.end()))  # type: ignore[union-attr]
            i = m.end()  # type: ignore[union-attr]
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            e = src.find("*/", i + 2)
            if e < 0:
                append(Token(K.BLOCK_COMMENT, i, n, "unterminated /* comment"))
                break
            append(Token(K.BLOCK_COMMENT, i, e + 2))
            i = e + 2
            continue
        if c == '"':
            m = _STRING.match(src, i)
            if m is None:
                append(Token(K.STRING, i, n, "unterminated string"))
                break
            append(Token(K.STRING, i, m.end()))
            i = m.end()
            continue
        if c == "#":
            if i + 1 < n and src[i + 1] == "{":
                e = src.find("#}", i + 2)
                if e < 0:
                    append(Token(K.VERBATIM, i, n, "unterminated #{ code block"))
                    break
                append(Token(K.VERBATIM, i, e + 2))
                i = e + 2
                continue
            m = _DIRECTIVE.match(src, i)
            if m:
                append(Token(K.DIRECTIVE, i, m.end()))
                i = m.end()
                last_word = None
                continue
            append(Token(K.ERROR, i, i + 1, "stray '#'"))
            i += 1
            continue
        if c == "$":
            m = _MACRO.match(src, i)
            if m:
                append(Token(K.MACRO, i, m.end()))
                i = m.end()
                last_word = None
                continue
            append(Token(K.ERROR, i, i + 1, "stray '$'"))
            i += 1
            continue
        kind = _PUNCT.get(c)
        if kind is not None:
            append(Token(kind, i, i + 1))
            i += 1
            if kind is K.LPAREN and last_word is not None and _INT.match(last_word):
                count = int(last_word)
                j = _try_skip_list(src, i, n, count, fmt, list_type)
                if j is not None and j > i:
                    append(Token(K.BLOB, i, j))
                    i = j
            last_word = None
            continue
        # word (also numbers)
        e = _scan_word(src, i, n)
        if e == i:  # cannot happen for valid chars, but never loop forever
            append(Token(K.ERROR, i, i + 1, f"unexpected character {c!r}"))
            i += 1
            continue
        append(Token(K.WORD, i, e))
        last_word = src[i:e]
        lm = _LIST_TYPE.match(last_word)
        if lm:
            list_type = lm.group(1)
        i = e
    return toks


def _try_skip_list(src: str, i: int, n: int, count: int, fmt: FileFormat, list_type: str | None) -> int | None:
    """After 'N(' : return the index of the closing ')' if the payload should be one BLOB token."""
    if fmt.binary:
        eb = _element_bytes(list_type, fmt)
        if eb is not None:
            j = i + count * eb
            if j < n and src[j] == ")":
                return j
        # Unknown element type: never guess. Binary-format files also contain lists that are always
        # written structurally (e.g. the patch list in polyMesh/boundary, class polyBoundaryMesh),
        # and guessing an end would swallow them as one opaque blob.
        return None
    if count < BLOB_THRESHOLD:
        return None
    rp = _skip_ascii_list(src, i, n)
    return rp if rp > 0 else None
