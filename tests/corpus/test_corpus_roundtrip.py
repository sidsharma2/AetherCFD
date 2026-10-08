"""Release gate (ADR 0002): every OpenFOAM file under D:\\OpenFOAM decodes byte-exactly, tokenizes
into tiles that reproduce it exactly, and parses without crashing. Read-only.

Parse *diagnostics* on real files are reported (build/corpus_report.json), not failed: some real
files are genuinely malformed and OpenFOAM would reject them too.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aethercfd.core.textio import decode_bytes, encode_text
from aethercfd.foamdict.parser import parse

from .corpus_files import CORPUS_ROOT, iter_foam_files

pytestmark = pytest.mark.corpus


@pytest.mark.skipif(not CORPUS_ROOT.exists(), reason="no corpus on this machine")
@pytest.mark.timeout(1800)  # ~22 GB / 12k files: 9 min warm, longer on a cold disk cache
def test_corpus_lossless_roundtrip():
    t0 = time.perf_counter()
    report: dict = {"files": 0, "bytes": 0, "inexact": [], "untiled": [], "crashed": [], "with_errors": [],
                    "slowest": []}
    timings = []
    for p in iter_foam_files():
        data = p.read_bytes()
        report["files"] += 1
        report["bytes"] += len(data)
        text, enc = decode_bytes(data)
        if encode_text(text, enc) != data:
            report["inexact"].append(str(p))
            continue
        t = time.perf_counter()
        try:
            doc = parse(text)
        except Exception as exc:  # the parser must never raise
            report["crashed"].append([str(p), repr(exc)[:200]])
            continue
        timings.append((time.perf_counter() - t, str(p), len(data)))
        if "".join(text[k.start:k.end] for k in doc.tokens) != text:
            report["untiled"].append(str(p))
        errs = [d for d in doc.diagnostics if d.severity == "error"]
        if errs:
            d = errs[0]
            line, col = doc.line_col(d.start)
            report["with_errors"].append({"file": str(p), "line": line, "col": col, "message": d.message,
                                          "n_errors": len(errs)})
    timings.sort(reverse=True)
    report["slowest"] = [[round(s, 3), f, b] for s, f, b in timings[:10]]
    report["seconds"] = round(time.perf_counter() - t0, 1)
    out = Path("build/corpus_report.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert report["files"] > 0
    assert not report["inexact"], report["inexact"][:5]
    assert not report["untiled"], report["untiled"][:5]
    assert not report["crashed"], report["crashed"][:5]
