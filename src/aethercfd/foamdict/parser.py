"""Error-recovering recursive-descent parser: tokens -> CST (see cst.py).

It never raises on bad input. Problems become ``Diagnostic`` entries and parsing resumes at the
next sensible boundary, because the raw editor hands us half-typed text all the time.
"""

from __future__ import annotations

from aethercfd.foamdict.cst import (
    Atom,
    Diagnostic,
    DictNode,
    DimNode,
    Directive,
    Entry,
    FoamDocument,
    Item,
    Key,
    ListNode,
    MacroEntry,
    Member,
    Value,
)
from aethercfd.foamdict.tokens import FileFormat, K, Token, tokenize

# directives whose arguments run to the end of the line (OpenFOAM reads the rest of the line)
_LINE_DIRECTIVES = {"#includeFunc", "#includeFunction", "#includeModel", "#includeFvConstraint",
                    "#includeFvModel"}
# directives that take no argument
_NOARG_DIRECTIVES = {"#else", "#endif"}
# directives whose "{ ... }" argument is an expression read verbatim, not a dictionary
# ("xInC #eval{ $rIn * $cos15 };")
_EXPR_BRACE_DIRECTIVES = {"#eval"}
_ATOM_KINDS = (K.WORD, K.STRING, K.MACRO, K.DIRECTIVE, K.VERBATIM, K.BLOB, K.ERROR)
#: deeper nesting than this is reported and the rest of the input is kept as flat atoms (real
#: dictionaries nest < 10 deep; this only guards pathological or misdetected input)
MAX_DEPTH = 200
_MAX_FUNC_ARGS = 20000  # characters an #includeFunc argument list may span


def _line_directive_end(src: str, pos: int) -> int:
    """End of an #includeFunc-style directive: the end of its line, or, when its argument list is still open
    there or starts on the next line, the matching ')'. Foundation tutorials write
    ``#includeFunc streamlinesLine\\n(\\n    name=streamlines,\\n    ...\\n)`` (pitzDailySteady/system/functions)."""
    eol = src.find("\n", pos)
    eol = len(src) if eol < 0 else eol
    line = src[pos:eol].split("//", 1)[0]
    depth = line.count("(") - line.count(")")
    if depth <= 0:
        if "(" in line:
            return eol
        k = eol
        while k < len(src) and src[k] in " \t\r\n":
            k += 1
        if k >= len(src) or src[k] != "(":
            return eol
        scan, depth = k, 0
    else:
        scan = eol
    i, in_str, limit = scan, False, min(len(src), scan + _MAX_FUNC_ARGS)
    while i < limit:
        ch = src[i]
        if ch == '"':
            in_str = not in_str
        elif not in_str and ch == "(":
            depth += 1
        elif not in_str and ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return eol


def parse(source: str, fmt: FileFormat | None = None) -> FoamDocument:
    tokens = tokenize(source, fmt)
    p = _Parser(source, tokens)
    root = p.parse_root()
    for t in tokens:
        if t.error:
            p.diags.append(Diagnostic(t.start, t.end, t.error))
    p.diags.sort(key=lambda d: d.start)
    return FoamDocument(source=source, tokens=tokens, root=root, diagnostics=p.diags)


class _Parser:
    def __init__(self, src: str, tokens: list[Token]):
        self.src = src
        self.toks = [t for t in tokens if not t.kind.trivia]
        self.n = len(self.toks)
        self.i = 0
        self.depth = 0
        self.diags: list[Diagnostic] = []

    def _too_deep(self, t: Token) -> bool:
        if self.depth < MAX_DEPTH:
            return False
        self.diag(t.start, t.end, f"nesting deeper than {MAX_DEPTH} levels; rest of file not structured")
        return True

    def _flatten_rest(self) -> list[Item]:
        rest = [Atom(t.kind, t.start, t.end, self.txt(t)) for t in self.toks[self.i:]]
        self.i = self.n
        return rest

    # ------------------------------------------------------------------ helpers
    def peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.toks[j] if j < self.n else None

    def take(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def txt(self, t: Token) -> str:
        return self.src[t.start:t.end]

    def diag(self, start: int, end: int, msg: str, severity: str = "error") -> None:
        self.diags.append(Diagnostic(start, end, msg, severity))

    def atom(self, t: Token) -> Atom:
        return Atom(t.kind, t.start, t.end, self.txt(t))

    # ------------------------------------------------------------------ dictionaries
    def parse_root(self) -> DictNode:
        members = self.parse_members(closing=False, root=True)
        return DictNode(start=0, end=len(self.src), members=members)

    def parse_dict(self) -> DictNode:
        lb = self.take()  # '{'
        if self._too_deep(lb):
            rest = self._flatten_rest()
            return DictNode(start=lb.start, end=rest[-1].end if rest else lb.end, open_brace=lb.start)
        self.depth += 1
        try:
            members = self.parse_members(closing=True)
        finally:
            self.depth -= 1
        t = self.peek()
        if t is not None and t.kind is K.RBRACE:
            self.take()
            return DictNode(start=lb.start, end=t.end, members=members, open_brace=lb.start, close_brace=t.start)
        self.diag(lb.start, lb.end, "unterminated '{' (missing '}')")
        end = self.toks[self.i - 1].end if self.i > 0 else lb.end
        return DictNode(start=lb.start, end=end, members=members, open_brace=lb.start, close_brace=None)

    def parse_members(self, *, closing: bool, root: bool = False) -> list[Member]:
        members: list[Member] = []
        while (t := self.peek()) is not None:
            k = t.kind
            if k is K.RBRACE:
                if closing:
                    return members
                self.diag(t.start, t.end, "unexpected '}'")
                self.i += 1
            elif k is K.SEMI:
                self.i += 1  # stray ';' is harmless in OpenFOAM
            elif k is K.DIRECTIVE:
                members.append(self.parse_directive())
            elif k is K.MACRO and (nt := self.peek(1)) is not None and nt.kind is K.SEMI:
                self.take()
                semi = self.take()
                members.append(MacroEntry(t.start, semi.end, self.txt(t)))
            elif k in (K.WORD, K.STRING, K.MACRO):
                members.append(self.parse_entry(root=root))
            else:
                self.diag(t.start, t.end, f"unexpected {self.txt(t)[:20]!r} where a keyword was expected")
                self.i += 1
        return members

    def parse_entry(self, *, root: bool) -> Entry:
        kt = self.take()
        key = Key(kt.kind, kt.start, kt.end, self.txt(kt))
        t = self.peek()
        if t is None:
            self.diag(kt.start, kt.end, f"keyword {key.name!r} has no value")
            return Entry(start=kt.start, end=kt.end, key=key)
        if t.kind is K.LBRACE and root and kt.kind is K.WORD and key.text.isdigit() and t.start == kt.end:
            lst = self.parse_uniform(Atom(kt.kind, kt.start, kt.end, key.text))
            return Entry(start=kt.start, end=lst.end, key=key, value=Value(lst.start, lst.end, [lst]))
        if t.kind is K.LBRACE:
            d = self.parse_dict()
            return Entry(start=kt.start, end=d.end, key=key, dict=d)
        if root and kt.kind is K.WORD and key.text.isdigit() and t.kind is K.LPAREN:
            # keyless top-level "N ( ... )" list; files like *.eMesh hold several in a row
            lst = self.parse_list(None)
            return Entry(start=kt.start, end=lst.end, key=key, value=Value(lst.start, lst.end, [lst]))
        items, stop = self.parse_items(until_semi=True)
        value = Value(items[0].start if items else t.start, items[-1].end if items else t.start, items)
        if stop is not None and stop.kind is K.SEMI:
            self.take()
            return Entry(start=kt.start, end=stop.end, key=key, value=value, semi=stop.start)
        # no ';' -- allowed only for root-level stream data: "N ( ... )" lists (polyMesh/boundary,
        # points, ...) and the bare numbers + lists of *.extendedFeatureEdgeMesh files
        stream = root and Atom(K.WORD, 0, 0, key.text).is_number and all(
            isinstance(x, ListNode) or (isinstance(x, Atom) and x.is_number) for x in items)
        if not stream:
            where = value.end if items else kt.end
            self.diag(kt.start, where, f"missing ';' after {key.name!r}")
        return Entry(start=kt.start, end=value.end if items else kt.end, key=key, value=value)

    def parse_directive(self) -> Directive:
        dt = self.take()
        name = self.txt(dt)
        args: list[Item] = []
        if name in _NOARG_DIRECTIVES:
            return Directive(dt.start, dt.end, name)
        if name in _LINE_DIRECTIVES:
            eol = _line_directive_end(self.src, dt.end)
            # OpenFOAM reads the rest of the line verbatim ("streamlines(funcName=sl, U)" contains a
            # space inside the parentheses), so take raw tokens without structural interpretation
            while (t := self.peek()) is not None and t.start < eol and t.kind is not K.RBRACE:
                self.i += 1
                args.append(self.atom(t))
        else:
            t = self.peek()
            if t is not None and t.kind not in (K.RBRACE, K.SEMI, K.DIRECTIVE):
                args.extend(self.parse_one_item(in_list=False, prev=[self.atom(dt)]))
        end = args[-1].end if args else dt.end
        return Directive(dt.start, end, name, args, self.src[dt.end:end].strip())

    # ------------------------------------------------------------------ values
    def parse_items(self, *, until_semi: bool) -> tuple[list[Item], Token | None]:
        """Items up to (not including) ';' or a closing '}' / EOF. Returns (items, stop_token)."""
        items: list[Item] = []
        while (t := self.peek()) is not None:
            if t.kind is K.SEMI or t.kind is K.RBRACE:
                return items, t
            if t.kind is K.RPAREN or t.kind is K.RBRACKET:
                self.diag(t.start, t.end, f"unexpected {self.txt(t)!r}")
                self.i += 1
                continue
            after_directive = bool(items) and isinstance(items[-1], Atom) and items[-1].kind is K.DIRECTIVE
            if t.kind is K.LBRACE and self._uniform_count(items, t) is not None:
                items.append(self.parse_uniform(items.pop()))  # type: ignore[arg-type]
                continue
            if items and not after_directive:
                # "a 1 <newline> b { ... }": a keyword followed by '{' starts a new sub-dictionary, so
                # the current entry is just missing its ';'. A bare '{' is only a value after a
                # directive such as #codeStream.
                nt = self.peek(1)
                if t.kind in (K.WORD, K.STRING) and nt is not None and nt.kind is K.LBRACE \
                        and not (t.kind is K.WORD and nt.start == t.end and self.txt(t).isdigit()):
                    return items, t
                if t.kind is K.LBRACE:
                    return items, t
            items.extend(self.parse_one_item(in_list=False, prev=items))
        return items, None

    def parse_one_item(self, *, in_list: bool, prev: list[Item] | None = None) -> list[Item]:
        t = self.peek()
        assert t is not None
        k = t.kind
        if k is K.LPAREN:
            count_atom = None
            if prev and isinstance(prev[-1], Atom) and prev[-1].kind is K.WORD and prev[-1].text.isdigit():
                count_atom = prev.pop()
            return [self.parse_list(count_atom)]
        if k is K.LBRACKET:
            return [self.parse_dim()]
        if k is K.LBRACE:
            if prev is not None and self._uniform_count(prev, t) is not None:
                return [self.parse_uniform(prev.pop())]  # type: ignore[arg-type]
            if prev and isinstance(prev[-1], Atom) and prev[-1].kind is K.DIRECTIVE \
                    and prev[-1].text in _EXPR_BRACE_DIRECTIVES:
                return [self.parse_expr_block()]
            return [self.parse_dict()]
        if k in _ATOM_KINDS:
            self.i += 1
            return [self.atom(t)]
        self.diag(t.start, t.end, f"unexpected {self.txt(t)!r}")
        self.i += 1
        return []

    def parse_expr_block(self) -> Atom:
        """'{ ... }' after #eval: kept as one verbatim atom up to the matching '}'."""
        lb = self.take()
        depth = 1
        while (t := self.peek()) is not None:
            self.i += 1
            if t.kind is K.LBRACE:
                depth += 1
            elif t.kind is K.RBRACE:
                depth -= 1
                if depth == 0:
                    return Atom(K.VERBATIM, lb.start, t.end, self.src[lb.start:t.end])
        self.diag(lb.start, lb.end, "unterminated '{' (missing '}')")
        end = self.toks[self.i - 1].end
        return Atom(K.VERBATIM, lb.start, end, self.src[lb.start:end])

    def parse_list(self, count_atom: Atom | None) -> ListNode:
        lp = self.take()
        if self._too_deep(lp):
            rest = self._flatten_rest()
            return ListNode(start=lp.start, end=rest[-1].end if rest else lp.end, items=rest, lparen=lp.start)
        self.depth += 1
        try:
            return self._parse_list_body(lp, count_atom)
        finally:
            self.depth -= 1

    def _parse_list_body(self, lp: Token, count_atom: Atom | None) -> ListNode:
        # the span includes an "N" count prefix, so replacing the list never leaves the count behind
        node = ListNode(start=count_atom.start if count_atom else lp.start, end=lp.end,
                        count=int(count_atom.text) if count_atom else None, lparen=lp.start)
        while (t := self.peek()) is not None:
            if t.kind is K.RPAREN:
                self.take()
                node.end = t.end
                return node
            if t.kind in (K.SEMI, K.RBRACE):
                self.diag(lp.start, lp.end, "unterminated '(' (missing ')')")
                node.end = self.toks[self.i - 1].end
                return node
            if t.kind is K.RBRACKET:
                self.diag(t.start, t.end, "unexpected ']'")
                self.i += 1
                continue
            if t.kind is K.BLOB:
                node.blob = True
            node.items.extend(self.parse_one_item(in_list=True, prev=node.items))
        self.diag(lp.start, lp.end, "unterminated '(' (missing ')')")
        node.end = self.toks[-1].end if self.toks else lp.end
        return node

    @staticmethod
    def _uniform_count(prev: list[Item], lbrace: Token) -> int | None:
        """An integer atom *directly* followed by '{' ("17740{0}") starts a uniform list."""
        if prev and isinstance(prev[-1], Atom) and prev[-1].kind is K.WORD and prev[-1].text.isdigit() \
                and prev[-1].end == lbrace.start:
            return int(prev[-1].text)
        return None

    def parse_uniform(self, count_atom: Atom) -> ListNode:
        lb = self.take()  # '{'
        node = ListNode(start=count_atom.start, end=lb.end, count=int(count_atom.text), lparen=lb.start,
                        uniform=True)
        while (t := self.peek()) is not None:
            if t.kind is K.RBRACE:
                self.take()
                node.end = t.end
                return node
            if t.kind is K.SEMI:
                break
            node.items.extend(self.parse_one_item(in_list=True, prev=node.items))
        self.diag(lb.start, lb.end, "unterminated uniform list '{' (missing '}')")
        node.end = self.toks[self.i - 1].end
        return node

    def parse_dim(self) -> DimNode:
        lb = self.take()
        node = DimNode(start=lb.start, end=lb.end)
        while (t := self.peek()) is not None:
            if t.kind is K.RBRACKET:
                self.take()
                node.end = t.end
                return node
            if t.kind in (K.SEMI, K.RBRACE, K.LBRACE, K.LPAREN, K.RPAREN):
                break
            self.i += 1
            node.items.append(self.atom(t))
        self.diag(lb.start, lb.end, "unterminated '[' (missing ']')")
        node.end = self.toks[self.i - 1].end
        return node
