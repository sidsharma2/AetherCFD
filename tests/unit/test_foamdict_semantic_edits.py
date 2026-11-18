from __future__ import annotations

import os
from pathlib import Path

import pytest

from aethercfd.core.textio import read_snapshot
from aethercfd.foamdict.edits import (
    EditError,
    NotFound,
    apply_edits,
    insert_entry,
    remove_entry,
    set_or_insert,
    set_value,
    to_foam,
)
from aethercfd.foamdict.parser import parse
from aethercfd.foamdict.semantic import DictView, Provenance

# Local OpenFOAM cases for the read-only real-case tests: set AETHERCFD_TEST_CASES to their folder; the
# tests skip without it.
_CASES = os.environ.get("AETHERCFD_TEST_CASES")
CASES = Path(_CASES) if _CASES else None
NO_CASES = "set AETHERCFD_TEST_CASES to a folder of local OpenFOAM cases"
# MACRO_CASE: a case whose controlDict has a massFlowOutlet function object that inherits
# "operation sum" from $massFlowInlet and sets its own "name outlet"
BFS = CASES / "backwardFacingStep2D" if CASES else None
MACRO_CASE = CASES / "macro_merge" if CASES else None

FVSOL = """FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers
{
    p
    {
        solver          GAMG;   // pressure
        tolerance       1e-06;
        relTol          0.1;
    }
    "(U|k|omega)"
    {
        solver          smoothSolver;
        tolerance       1e-05;
    }
}
PIMPLE
{
    nOuterCorrectors 2;
    nCorrectors      1;
}
inlet { type fixedValue; value uniform (1 0 0); }
Uinf 10;
U0 $Uinf;
U1 #calc "$Uinf*2";
"""


def doc_of(src: str):
    return parse(src)


def test_lookup_rules_patterns_and_last_wins():
    v = DictView(doc_of(FVSOL + "Uinf 12;\n"))
    assert v.get_text("Uinf") == "12"  # last duplicate wins
    lk = v.lookup("solvers/k/solver")
    assert lk is not None and lk.entry is not None
    assert v.get_text("solvers/k/tolerance") == "1e-05"
    assert v.lookup("solvers/k").matched_pattern
    assert not v.lookup("solvers/p").matched_pattern
    assert v.lookup("solvers/T") is None
    assert v.keys("PIMPLE") == ["nOuterCorrectors", "nCorrectors"]


def test_provenance():
    v = DictView(doc_of(FVSOL))
    assert v.lookup("Uinf").provenance is Provenance.LITERAL
    assert v.lookup("U0").provenance is Provenance.INHERITED
    assert v.lookup("U1").provenance is Provenance.COMPUTED
    assert v.lookup("PIMPLE").provenance is Provenance.DICT
    assert v.resolve_macro("$Uinf").key.name == "Uinf"


def test_set_value_changes_exactly_the_value():
    src = FVSOL
    d = doc_of(src)
    out = apply_edits(src, set_value(d, "PIMPLE/nOuterCorrectors", "3"))
    assert out == src.replace("nOuterCorrectors 2;", "nOuterCorrectors 3;")
    out = apply_edits(src, set_value(d, "solvers/p/tolerance", to_foam(1e-7)))
    assert "tolerance       1e-07;" in out and "// pressure" in out
    out = apply_edits(src, set_value(d, "inlet/value", "uniform (2 0 0)"))
    assert "inlet { type fixedValue; value uniform (2 0 0); }" in out


def test_set_value_refusals():
    d = doc_of(FVSOL)
    with pytest.raises(EditError, match="inherited"):
        set_value(d, "U0", "3")
    with pytest.raises(EditError, match="computed"):
        set_value(d, "U1", "3")
    with pytest.raises(EditError, match="pattern"):
        set_value(d, "solvers/k/tolerance", "1e-6")
    with pytest.raises(EditError, match="sub-dictionary"):
        set_value(d, "PIMPLE", "3")
    with pytest.raises(NotFound):
        set_value(d, "PIMPLE/momentumPredictor", "yes")
    assert "1e-6" in apply_edits(FVSOL, set_value(d, "U0", "1e-6", allow_nonliteral=True))


def test_insert_follows_alignment_and_packing():
    d = doc_of(FVSOL)
    out = apply_edits(FVSOL, insert_entry(d, "solvers/p", "maxIter", "50"))
    assert "        relTol          0.1;\n        maxIter         50;\n    }" in out
    out = apply_edits(FVSOL, insert_entry(d, "inlet", "inletValue", "uniform (0 0 0)"))
    assert "inlet { type fixedValue; value uniform (1 0 0); inletValue uniform (0 0 0); }" in out
    out = apply_edits(FVSOL, set_or_insert(d, "PIMPLE/momentumPredictor", "yes"))
    assert "    nCorrectors      1;\n    momentumPredictor yes;\n}" in out
    out = apply_edits(FVSOL, insert_entry(d, "", "endTime", "100"))
    assert out.endswith('U1 #calc "$Uinf*2";\nendTime 100;\n')
    reparsed = parse(out)
    assert reparsed.ok and DictView(reparsed).get_text("endTime") == "100"
    with pytest.raises(EditError):
        insert_entry(d, "PIMPLE", "nCorrectors", "2")


def test_remove_entry():
    d = doc_of(FVSOL)
    out = apply_edits(FVSOL, remove_entry(d, "PIMPLE/nCorrectors"))
    assert "nCorrectors" not in out and "nOuterCorrectors 2;\n}" in out
    out = apply_edits(FVSOL, remove_entry(d, "inlet/type"))
    assert "inlet { value uniform (1 0 0); }" in out


@pytest.mark.skipif(BFS is None or not BFS.exists(), reason=NO_CASES)
def test_real_controldict_set_endtime_changes_one_token():
    snap = read_snapshot(BFS / "system" / "controlDict")
    d = parse(snap.text)
    edits = set_value(d, "endTime", "3000")
    assert len(edits) == 1
    out = apply_edits(snap.text, edits)
    a, b = snap.text.split("\n"), out.split("\n")
    changed = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
    assert changed == [("endTime         2000;", "endTime         3000;")]
    assert snap.encode(out) != snap.encode()  # same encoding/newlines, one token different


@pytest.mark.skipif(MACRO_CASE is None or not MACRO_CASE.exists(), reason=NO_CASES)
def test_real_macro_merge_inheritance():
    snap = read_snapshot(MACRO_CASE / "system" / "controlDict")
    v = DictView(parse(snap.text))
    lk = v.lookup("functions/massFlowOutlet/operation")
    assert lk is not None and lk.provenance is Provenance.INHERITED and lk.sources == ("$massFlowInlet",)
    assert v.doc.text(lk.entry.value.start, lk.entry.value.end) == "sum"
    assert not lk.editable_here  # the form must send the user to massFlowInlet
    own = v.lookup("functions/massFlowOutlet/name")
    assert own.provenance is Provenance.LITERAL and v.get_text("functions/massFlowOutlet/name") == "outlet"
