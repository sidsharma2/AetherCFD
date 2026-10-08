from __future__ import annotations

import pytest

from aethercfd.backend.wsl.distro import parse_list_verbose
from aethercfd.backend.wsl.forks import (
    EXIT_ALREADY_SOURCED,
    EXIT_FORK_MISMATCH,
    Family,
    ForkProfile,
    family_of_version,
    prelude,
)
from aethercfd.backend.wsl.runner import (
    DistroNotAllowed,
    WslError,
    WslRunner,
    check_wsl_options,
    decode_output,
)

ESI = ForkProfile("esi-v2306", Family.ESI, "v2306", "LMAE-OpenFOAM", "/usr/lib/openfoam/openfoam2306/etc/bashrc",
                  "/usr/lib/openfoam/openfoam2306", True)


def test_argv_is_a_list_with_isolated_bash_and_timeout():
    r = WslRunner("LMAE-OpenFOAM")
    argv = r.script_argv(["/mnt/d/OpenFOAM/RPL Apollo Lunar Module Ascent Engine"], timeout_s=60)
    assert argv[:5] == ["wsl.exe", "-d", "LMAE-OpenFOAM", "--cd", "/"]
    assert argv[5] == "-e" and argv[6:10] == ["timeout", "-k", "5", "60"]
    assert "--noprofile" in argv and "--norc" in argv
    assert argv[-1] == "/mnt/d/OpenFOAM/RPL Apollo Lunar Module Ascent Engine"  # one argv element


@pytest.mark.parametrize("opt", ["--shutdown", "-t", "--terminate", "--unregister", "--export", "--import=x"])
def test_denylist(opt):
    with pytest.raises(WslError):
        check_wsl_options(["-d", "x", opt])


def test_distro_allowlist_and_agora_ban():
    with pytest.raises(DistroNotAllowed):
        WslRunner("Ubuntu-22.04", allowed=["LMAE-OpenFOAM"])
    with pytest.raises(DistroNotAllowed):
        WslRunner("Agora", allowed=["Agora"])


def test_crlf_scripts_are_rejected():
    with pytest.raises(WslError):
        WslRunner("LMAE-OpenFOAM").run_script("echo hi\r\n")


def test_decode_output_handles_utf16_and_utf8():
    assert decode_output("héllo".encode()) == "héllo"
    assert decode_output(b"\xff\xfe" + "NAME".encode("utf-16-le")) == "NAME"  # BOM form
    assert decode_output("NAME  STATE".encode("utf-16-le")) == "NAME  STATE"  # bare UTF-16LE


def test_parse_wsl_list_verbose():
    text = ("  NAME             STATE           VERSION\n"
            "* Ubuntu-22.04     Stopped         2\n"
            "  LMAE-OpenFOAM    Running         2\n"
            "  Agora            Running         2\n")
    ds = parse_list_verbose(text)
    assert [(d.name, d.state, d.version, d.default) for d in ds] == [
        ("Ubuntu-22.04", "Stopped", 2, True), ("LMAE-OpenFOAM", "Running", 2, False), ("Agora", "Running", 2, False)]
    assert ds[1].running and not ds[0].running


def test_prelude_text():
    p = prelude(ESI)
    assert "\r" not in p
    assert f"exit {EXIT_ALREADY_SOURCED}" in p and f"exit {EXIT_FORK_MISMATCH}" in p
    assert ". /usr/lib/openfoam/openfoam2306/etc/bashrc" in p
    assert p.startswith("export PATH=/usr/local/sbin")


def test_family_and_roundtrip():
    assert family_of_version("v2306") is Family.ESI
    assert family_of_version("14") is Family.FOUNDATION
    assert family_of_version("dev") is Family.FOUNDATION
    assert ForkProfile.from_json(ESI.to_json()) == ESI
    assert ESI.short_tag == "v2306"
