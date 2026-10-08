from __future__ import annotations

import struct

from hypothesis import given, settings
from hypothesis import strategies as st

from aethercfd.core.textio import decode_bytes
from aethercfd.foamdict.cst import Atom, DictNode, DimNode, Directive, Entry, ListNode, MacroEntry
from aethercfd.foamdict.parser import parse
from aethercfd.foamdict.tokens import K, tokenize

HEADER = """/*--------------------------------*- C++ -*----------------------------------*\\
  =========                 |
  \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
\\*---------------------------------------------------------------------------*/
FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      fvSchemes;
}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //
"""


def tiles(src: str) -> bool:
    return "".join(src[t.start:t.end] for t in tokenize(src)) == src


def entry(doc, *path) -> Entry:
    d = doc.root
    e = None
    for name in path:
        e = next(m for m in d.members if isinstance(m, Entry) and m.key.name == name)
        d = e.dict
    return e


def test_words_with_parentheses_and_regex_keys():
    src = HEADER + """
divSchemes
{
    default         none;
    div(phi,U)      bounded Gauss linearUpwind grad(U);
    "div\\(phi,(k|omega)\\)" bounded Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}
"""
    doc = parse(src)
    assert doc.ok, doc.diagnostics
    ds = entry(doc, "divSchemes").dict
    keys = [m.key for m in ds.members]
    assert [k.name for k in keys] == ["default", "div(phi,U)", "div\\(phi,(k|omega)\\)",
                                      "div((nuEff*dev2(T(grad(U)))))"]
    assert keys[2].is_pattern and not keys[1].is_pattern
    v = ds.members[1].value
    assert [a.text for a in v.atoms] == ["bounded", "Gauss", "linearUpwind", "grad(U)"]


def test_values_lists_dims_and_counts():
    src = """dimensions [0 1 -1 0 0 0 0];
internalField uniform (10 0 0);
value nonuniform List<scalar> 3(1 2 3);
faces 4(0 1 2 3);
empty;
"""
    doc = parse(src)
    assert doc.ok, doc.diagnostics
    dims = entry(doc, "dimensions").value.items[0]
    assert isinstance(dims, DimNode) and [a.text for a in dims.items] == ["0", "1", "-1", "0", "0", "0", "0"]
    lst = entry(doc, "internalField").value.items[1]
    assert isinstance(lst, ListNode) and lst.count is None and [a.text for a in lst.items] == ["10", "0", "0"]
    nl = entry(doc, "value").value.items[-1]
    assert isinstance(nl, ListNode) and nl.count == 3
    faces = entry(doc, "faces")
    assert faces.value.start == src.index("4(")  # span includes the count prefix
    assert entry(doc, "empty").value.items == []


def test_macros_directives_and_code():
    src = """#include "initialConditions"
#includeEtc "caseDicts/setConstraintTypes"
#includeFunc streamlines(funcName=sl, U)
inlet { $massFlowInlet; value $internalField; }
U0 #calc "$Uinf*2";
code #codeStream
{
    code
    #{
        os << "uniform 1";
    #};
};
"""
    doc = parse(src)
    assert doc.ok, doc.diagnostics
    dirs = [m for m in doc.root.members if isinstance(m, Directive)]
    assert [d.name for d in dirs] == ["#include", "#includeEtc", "#includeFunc"]
    assert dirs[2].args_text == "streamlines(funcName=sl, U)"
    inlet = entry(doc, "inlet").dict
    assert isinstance(inlet.members[0], MacroEntry) and inlet.members[0].macro == "$massFlowInlet"
    assert entry(doc, "inlet", "value").value.has_kind(K.MACRO)
    assert entry(doc, "U0").value.has_kind(K.DIRECTIVE)
    code = entry(doc, "code").value
    assert any(isinstance(i, DictNode) for i in code.items)


def test_list_of_dictionaries_and_toplevel_list():
    src = """8
(
    inlet { type patch; nFaces 10; startFace 100; }
    frontAndBack { type empty; inGroups 1(empty); nFaces 4; startFace 1; }
)
"""
    doc = parse(src)
    assert doc.ok, doc.diagnostics  # root-level "N ( ... )" needs no ';'
    top = doc.root.members[0]
    assert top.key.text == "8"  # a root-level "N ( ... )" list is an entry keyed by its count
    lst = top.value.items[0]
    assert isinstance(lst, ListNode)
    names = [i.text for i in lst.items if isinstance(i, Atom)]
    assert names == ["inlet", "frontAndBack"]
    assert sum(isinstance(i, DictNode) for i in lst.items) == 2


def test_error_recovery_keeps_structure():
    src = """a 1
b { c 2;
d (1 2;
e [0 1;
}
f 3;
}
"""
    doc = parse(src)
    msgs = [d.message for d in doc.diagnostics]
    assert any("missing ';' after 'a'" in m for m in msgs)
    assert any("missing ')'" in m for m in msgs)
    assert any("unexpected '}'" in m for m in msgs)
    assert entry(doc, "f").value.atoms[0].text == "3"  # parsing resumed after the broken block
    assert tiles(src)


def test_unterminated_things_still_tile():
    for src in ['a "unterminated', "a /* never closed", "a #{ code", "b { c 1;", "#", "$", "))", ""]:
        doc = parse(src)
        assert "".join(src[t.start:t.end] for t in doc.tokens) == src


def test_binary_field_payload_is_skipped_exactly():
    vals = [1.5, -2.25, 3.0]
    payload = b"".join(struct.pack("<d", v) for v in vals)
    # payload bytes deliberately include ')' and '{' / '}' / ';' values to prove the skip is by length
    payload += struct.pack("<d", struct.unpack("<d", b")};({;\n)")[0])
    raw = (b"FoamFile\n{\n    version 2.0;\n    format binary;\n    arch \"LSB;label=32;scalar=64\";\n"
           b"    class volScalarField;\n    object T;\n}\n"
           b"dimensions [0 0 0 1 0 0 0];\n"
           b"internalField nonuniform List<scalar> 4(" + payload + b");\n"
           b"boundaryField\n{\n    wall { type zeroGradient; }\n}\n")
    text, enc = decode_bytes(raw)
    assert enc == "latin-1"
    doc = parse(text)
    assert doc.ok, doc.diagnostics
    lst = entry(doc, "internalField").value.items[-1]
    assert lst.blob and lst.count == 4
    assert entry(doc, "boundaryField", "wall").dict.members[0].key.name == "type"
    assert text.encode("latin-1") == raw


def test_large_ascii_list_becomes_blob():
    body = "\n".join(f"({i} {i} 0)" for i in range(500))
    src = f"internalField nonuniform List<vector> 500\n(\n{body}\n)\n;\nboundaryField {{ }}\n"
    toks = tokenize(src)
    assert sum(t.kind is K.BLOB for t in toks) == 1
    doc = parse(src)
    assert doc.ok, doc.diagnostics
    assert entry(doc, "internalField").value.items[-1].blob


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.sampled_from(list("ab1 ;{}()[]\"#$/*\n\\.-<>,")), max_size=200))
def test_fuzz_tiling_and_no_crash(src):
    doc = parse(src)
    assert "".join(src[t.start:t.end] for t in doc.tokens) == src


def test_uniform_lists():
    doc = parse("flipMap List<bool> 17740{0};\nU uniformList 2{(1 0 0)};\n")
    assert doc.ok, doc.diagnostics
    fl = entry(doc, "flipMap").value.items[-1]
    assert isinstance(fl, ListNode) and fl.uniform and fl.count == 17740
    root_list = parse("FoamFile { class labelList; }\n3{-1}\n")
    assert root_list.ok and root_list.root.members[-1].value.items[0].uniform
    # a count separated from '{' by whitespace is NOT a uniform list
    assert not parse("a 5 {x 1;}").ok


def test_eval_brace_expression_is_verbatim():
    doc = parse("rIn 1;\nxInC    #eval{ $rIn  * $cos15 };\nnested #eval{ max({1, 2}) };\nb 2;\n")
    assert doc.ok, doc.diagnostics
    items = entry(doc, "xInC").value.items
    assert [i.kind for i in items] == [K.DIRECTIVE, K.VERBATIM]
    assert items[1].text == "{ $rIn  * $cos15 }"
    assert entry(doc, "nested").value.items[1].text == "{ max({1, 2}) }"
    assert entry(doc, "b").value.items[0].text == "2"
    assert not parse("x #eval{ 1 + 2 ;\n").ok  # unterminated is still reported


def test_stream_numbers_at_root_need_no_semicolon():
    # *.extendedFeatureEdgeMesh: bare numbers between keyless lists
    src = "FoamFile { class extendedFeatureEdgeMesh; }\n2\n(\n(0 0 0)\n(1 0 0)\n)\n" \
          "// concaveStart mixedStart nonFeatureStart\n0 0 4\n// internalStart flatStart\n0 0 0 488\n1(2)\n"
    assert parse(src).ok, parse(src).diagnostics
    # a word in such a run is still a genuine missing ';'
    assert not parse("0 0 endTime 5\n").ok
