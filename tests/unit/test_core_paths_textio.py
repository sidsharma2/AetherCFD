from __future__ import annotations

import pytest

from aethercfd.core.paths import (
    PathTranslationError,
    is_wsl_unc,
    openfoam_path_safety,
    win_to_wsl,
    wsl_to_win,
)
from aethercfd.core.textio import (
    atomic_write_bytes,
    decode_bytes,
    detect_newline,
    encode_text,
    read_snapshot,
    write_generated_text,
)

RPL = r"D:\OpenFOAM\RPL Apollo Lunar Module Ascent Engine"


@pytest.mark.parametrize(
    ("win", "wsl"),
    [
        (r"D:\OpenFOAM\backwardFacingStep2D", "/mnt/d/OpenFOAM/backwardFacingStep2D"),
        ("D:\\", "/mnt/d"),
        (r"c:\Users\x\a b", "/mnt/c/Users/x/a b"),
        (RPL, "/mnt/d/OpenFOAM/RPL Apollo Lunar Module Ascent Engine"),
        (r"D:\OpenFOAM\SandiaFlameD\postProcessing\fieldMinMax(T,CH4,O2)",
         "/mnt/d/OpenFOAM/SandiaFlameD/postProcessing/fieldMinMax(T,CH4,O2)"),
        (r"\\wsl.localhost\LMAE-OpenFOAM\root\aethercfd\stage\p\c", "/root/aethercfd/stage/p/c"),
        (r"\\wsl$\LMAE-OpenFOAM\root", "/root"),
        (r"\\wsl.localhost\LMAE-OpenFOAM", "/"),
    ],
)
def test_win_to_wsl(win, wsl):
    assert win_to_wsl(win) == wsl


def test_unc_distro_must_match():
    with pytest.raises(PathTranslationError):
        win_to_wsl(r"\\wsl.localhost\Ubuntu-22.04\root", distro="LMAE-OpenFOAM")
    assert win_to_wsl(r"\\wsl.localhost\lmae-openfoam\root", distro="LMAE-OpenFOAM") == "/root"


@pytest.mark.parametrize("bad", ["relative\\path", "/already/posix", r"\\server\share\x"])
def test_win_to_wsl_rejects_non_absolute(bad):
    with pytest.raises(PathTranslationError):
        win_to_wsl(bad)


def test_round_trips():
    for wsl in ["/mnt/d/OpenFOAM/x y", "/root/aethercfd/stage/a", "/"]:
        win = wsl_to_win(wsl, distro="LMAE-OpenFOAM")
        assert win_to_wsl(win) == wsl
    assert wsl_to_win("/mnt/d/OpenFOAM", distro="X") == r"D:\OpenFOAM"
    assert is_wsl_unc(wsl_to_win("/root/x", distro="LMAE-OpenFOAM"))


def test_openfoam_path_safety():
    assert openfoam_path_safety("/mnt/d/OpenFOAM/backwardFacingStep2D").safe
    assert openfoam_path_safety("/mnt/d/OpenFOAM/SandiaFlameD/postProcessing/fieldMinMax(T,CH4)").safe
    s = openfoam_path_safety(win_to_wsl(RPL))
    assert not s.safe and "contains a space" in s.problems
    assert not openfoam_path_safety("/root/it's").safe


def test_decode_roundtrip_is_byte_exact(tmp_path):
    samples = [
        b"FoamFile\r\n{\r\n    version 2.0;\r\n}\r\n",  # CRLF kept
        b"\xef\xbb\xbfa 1;\n",  # BOM kept
        b"note \x97 cp1252 dash;\n",  # legacy cp1252
        b"mixed\r\nendings\n",
        b"",
    ]
    for data in samples:
        text, enc = decode_bytes(data)
        assert encode_text(text, enc) == data, (data, enc)
    p = tmp_path / "controlDict"
    p.write_bytes(samples[0])
    snap = read_snapshot(p)
    assert snap.newline == "\r\n" and snap.encode() == samples[0]


def test_detect_newline():
    assert detect_newline("a\nb\n") == "\n"
    assert detect_newline("a\r\nb\r\n") == "\r\n"
    assert detect_newline("") == "\n"


def test_atomic_write_and_generated_text(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_bytes(p, b"one")
    atomic_write_bytes(p, b"two")
    assert p.read_bytes() == b"two"
    assert not [x for x in tmp_path.iterdir() if x.name.endswith(".aether-tmp")]
    write_generated_text(tmp_path / "run.sh", "#!/bin/bash\necho hi\n")
    assert b"\r" not in (tmp_path / "run.sh").read_bytes()
    with pytest.raises(ValueError):
        write_generated_text(tmp_path / "bad.sh", "echo hi\r\n")


def test_save_user_file_reports_a_read_only_target_and_keeps_it(tmp_path):
    import os
    import stat

    from aethercfd.core.textio import save_user_file

    f = tmp_path / "probe é.csv"
    assert save_user_file(f, "a,b\n") is None and f.read_text(encoding="utf-8") == "a,b\n"
    os.chmod(f, stat.S_IREAD)
    try:
        err = save_user_file(f, "c,d\n")
        assert err and "read-only or open in another program" in err
        assert f.read_text(encoding="utf-8") == "a,b\n"
    finally:
        os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".aether-tmp")]
    err = save_user_file(tmp_path / "nope" / "x.csv", "x")
    assert err and "does not exist" in err
