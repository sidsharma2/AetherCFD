from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from aethercfd.commands.base import USER
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.documents.document import DiskState, Document
from aethercfd.documents.merge import merge3
from aethercfd.documents.store import DocumentStore
from aethercfd.documents.watcher import CaseWatcher
from aethercfd.foamdict.edits import TextEdit
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"


# ---------------------------------------------------------------- merge3
def test_merge_non_overlapping_changes_merge_cleanly():
    base = "a 1;\nb 2;\nc 3;\nd 4;\ne 5;\n"
    ours = base.replace("a 1;", "a 10;")
    theirs = base.replace("e 5;", "e 50;")
    r = merge3(base, ours, theirs)
    assert r.clean and r.text == "a 10;\nb 2;\nc 3;\nd 4;\ne 50;\n"


def test_merge_conflict_and_identical_changes():
    base = "a 1;\nb 2;\nc 3;\n"
    r = merge3(base, base.replace("b 2", "b 20"), base.replace("b 2", "b 21"))
    assert not r.clean and r.conflicts[0].ours == ["b 20;\n"] and r.conflicts[0].theirs == ["b 21;\n"]
    same = base.replace("b 2", "b 9")
    assert merge3(base, same, same).text == same
    assert merge3(base, base, same).text == same and merge3(base, same, base).text == same


def test_merge_insertions_on_both_sides():
    base = "x;\ny;\nz;\n"
    ours = "x;\nnew1;\ny;\nz;\n"
    theirs = "x;\ny;\nz;\nnew2;\n"
    r = merge3(base, ours, theirs)
    assert r.clean and r.text == "x;\nnew1;\ny;\nz;\nnew2;\n"


# ---------------------------------------------------------------- document editing
def make_doc(tmp_path, name="controlDict") -> Document:
    p = tmp_path / name
    shutil.copy(FIX / "esi_cavity" / "system" / "controlDict", p)
    return Document.open(p)


def test_form_and_raw_edits_share_one_undo_history(tmp_path):
    doc = make_doc(tmp_path)
    original = doc.text
    doc.form_set("endTime", "1")  # form edit
    pos = doc.text.index("deltaT")
    doc.apply([TextEdit(pos, pos, "// tweaked\n")], origin="raw")  # raw edit
    assert doc.dirty and "endTime         1;" in doc.text and "// tweaked" in doc.text
    assert doc.undo() and "// tweaked" not in doc.text and "endTime         1;" in doc.text
    assert doc.undo() and doc.text == original and not doc.dirty
    assert doc.redo() and doc.redo() and "// tweaked" in doc.text


def test_typing_coalesces_into_one_undo_step(tmp_path):
    doc = make_doc(tmp_path)
    original = doc.text
    p = doc.text.index("endTime")
    for i, ch in enumerate("abc"):
        doc.apply([TextEdit(p + i, p + i, ch)], origin="raw", coalesce=True)
    doc.apply([TextEdit(p + 2, p + 3, "")], origin="raw", coalesce=True)  # backspace
    doc.apply([TextEdit(p + 1, p + 2, "")], origin="raw", coalesce=True)  # backspace
    assert doc.text[p:p + 1] == "a"
    doc.undo()  # undoes the two backspaces
    assert doc.text[p:p + 3] == "abc"
    doc.undo()  # undoes the typed run
    assert doc.text == original


def test_external_change_reloads_clean_buffer(tmp_path):
    doc = make_doc(tmp_path)
    events = []
    doc.subscribe(lambda d, kind, info: events.append(kind))
    raw = doc.path.read_bytes().replace(b"endTime         0.5;", b"endTime         2;")
    time.sleep(0.01)
    doc.path.write_bytes(raw)
    assert doc.check_disk() is DiskState.RELOADED
    assert "endTime         2;" in doc.text and not doc.dirty and "reloaded" in events


def test_external_change_on_dirty_buffer_is_a_conflict_with_merge(tmp_path):
    doc = make_doc(tmp_path)
    doc.form_set("deltaT", "0.001")  # ours
    raw = doc.path.read_bytes().replace(b"writeInterval   20;", b"writeInterval   40;")  # theirs
    time.sleep(0.01)
    doc.path.write_bytes(raw)
    assert doc.check_disk() is DiskState.CONFLICT and doc.in_conflict
    proposal = doc.merge_proposal()
    assert proposal.clean and "deltaT          0.001;" in proposal.text and "writeInterval   40;" in proposal.text
    doc.resolve_conflict("merged")
    assert not doc.in_conflict and doc.dirty and "writeInterval   40;" in doc.text


def test_own_save_is_not_an_external_change(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    proj = Project.create(case)
    ex = Executor(default_registry(), Context(proj), AutoApprove())
    store = DocumentStore()
    doc = store.open(case / "system" / "controlDict")
    doc.form_set("endTime", "3")
    res = ex.execute("file.save", {"path": str(doc.path), "text": doc.text, "expected_sha": doc.snapshot.sha256}, USER)
    assert res.ok, res.message
    doc.mark_saved()
    assert not doc.dirty
    assert store.check()[str(doc.path)] is DiskState.UNCHANGED
    assert store.get(case / "system" / ".." / "system" / "controlDict") is doc  # one buffer per file


def test_watcher_reports_changes(tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    got: list[set[Path]] = []
    ev = threading.Event()

    def cb(paths):
        got.append(paths)
        ev.set()

    w = CaseWatcher(case, cb, debounce=0.2).start()
    try:
        time.sleep(0.3)
        (case / "system" / "controlDict").write_bytes((case / "system" / "controlDict").read_bytes() + b"\n")
        (case / ".aethercfd").mkdir()
        (case / ".aethercfd" / "x").write_text("ignored")
        assert ev.wait(3), "no change reported within 3 s"
        names = {p.name for batch in got for p in batch}
        assert "controlDict" in names and "x" not in names
    finally:
        w.stop()


def test_polling_mode_is_used_for_wsl_paths():
    w = CaseWatcher(r"\\wsl.localhost\LMAE-OpenFOAM\root", lambda p: None)
    assert w.polling
    w2 = CaseWatcher(os.getcwd(), lambda p: None)
    assert not w2.polling
