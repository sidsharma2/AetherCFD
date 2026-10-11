"""Copilot providers: Claude's and Codex's events mapped onto the neutral stream, argv, CLI discovery/sign-in."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aethercfd.copilot import codex_cli
from aethercfd.copilot.codex_session import CodexEventMapper, CodexSession, _toml_str
from aethercfd.copilot.provider import CliInfo
from aethercfd.copilot.providers import PROVIDERS, get_provider
from aethercfd.copilot.session import CLAUDE_FILE_TOOLS, ClaudeSession, normalize_claude

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "copilot"
RECORDED = FIX / "codex_exec_0.159.2.jsonl"  # a real `codex exec --json` run against the AetherCFD MCP bridge


class _Bridge:
    endpoint, token, port = "127.0.0.1:5555", "tok", 5555


def _codex_cli(**extra) -> CliInfo:
    return CliInfo(Path(sys.executable), "0.159.2", "test",
                   {"exec_flags": ["--ignore-user-config", "--json", "--skip-git-repo-check"],
                    "features": ["shell_tool", "unified_exec", "code_mode_host", "multi_agent"], **extra})


# ------------------------------------------------------------------------------------------------ Claude
def test_claude_events_normalised():
    assert normalize_claude({"type": "stream_event", "event": {"type": "message_start"}}) == [{"type": "turn_start"}]
    assert normalize_claude({"type": "stream_event", "event": {"type": "content_block_delta",
                                                               "delta": {"type": "text_delta", "text": "Hi"}}}) == \
        [{"type": "text_delta", "text": "Hi"}]
    assert normalize_claude({"type": "stream_event", "event": {"type": "content_block_stop"}}) == []
    out = normalize_claude({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tu1", "name": "mcp__aether__dict_get", "input": {"file": "system/controlDict"}}]}})
    assert out == [{"type": "message", "text": "Checking."},
                   {"type": "tool_call", "id": "tu1", "name": "dict_get", "input": {"file": "system/controlDict"}}]
    assert normalize_claude({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu1", "is_error": True}]}}) == \
        [{"type": "tool_result", "id": "tu1", "is_error": True}]
    assert normalize_claude({"type": "user", "message": {"content": "plain text"}}) == []
    ok = normalize_claude({"type": "result", "subtype": "success", "session_id": "s1", "num_turns": 3,
                           "total_cost_usd": 0.12, "result": "done", "usage": {"input_tokens": 10, "output_tokens": 5}})
    assert ok == [{"type": "result", "session_id": "s1", "num_turns": 3, "cost_usd": 0.12,
                   "usage": {"input_tokens": 10, "output_tokens": 5}, "is_error": False, "message": "done"}]
    bad = normalize_claude({"type": "result", "subtype": "error_max_turns", "session_id": "s1"})[0]
    assert bad["is_error"] and bad["message"] == "error_max_turns" and bad["cost_usd"] is None
    init = normalize_claude({"type": "system", "subtype": "init", "model": "opus", "session_id": "s1",
                             "mcp_servers": [{"name": "aether", "status": "failed"}]})
    assert init == [{"type": "init", "model": "opus", "session_id": "s1", "tools_failed": ["aether"]}]


def test_claude_keeps_its_tool_lockdown(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CACHE_DIR", str(tmp_path / "cache"))
    s = ClaudeSession(CliInfo(Path("claude.exe"), "2", "t"), tmp_path, _Bridge(), on_event=lambda e: None)
    a = s.argv()
    denied = a[a.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(denied)
    assert a[a.index("--allowedTools") + 1].startswith("mcp__aether,Read,Grep,Glob")
    assert CLAUDE_FILE_TOOLS in a[a.index("--append-system-prompt") + 1]


# ------------------------------------------------------------------------------------------------ Codex events
def _replay(lines: list[dict]) -> list[dict]:
    m = CodexEventMapper("gpt-test")
    m.begin_turn()
    out = []
    for ev in lines:
        out += m.feed(ev)
    return out


def test_codex_recorded_run_maps_onto_neutral_events():
    lines = [json.loads(x) for x in RECORDED.read_text(encoding="utf-8").splitlines() if x.strip()]
    out = _replay(lines)
    kinds = [e["type"] for e in out]
    assert kinds == ["init", "message", "message", "tool_call", "tool_result", "tool_call", "tool_result", "message",
                     "result"]
    assert out[0]["session_id"] == "01a12b9f-f5b1-7eb0-ba64-acc5abd39325" and out[0]["model"] == "gpt-test"
    calls = [e for e in out if e["type"] == "tool_call"]
    assert [c["name"] for c in calls] == ["case_overview", "dict_get"]  # no mcp__aether__ prefix
    assert calls[1]["input"] == {"file": "system/fvSolution", "key": "SIMPLE/x"}
    results = [e for e in out if e["type"] == "tool_result"]
    assert [r["is_error"] for r in results] == [False, True]  # dict_get answered isError -> status failed
    res = out[-1]
    assert res["is_error"] is False and res["num_turns"] == 3 and res["cost_usd"] is None
    assert res["usage"] == {"input_tokens": 105149, "output_tokens": 422}
    assert res["session_id"] == out[0]["session_id"]


def test_codex_failures_and_other_items():
    out = _replay([
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"id": "e0", "type": "error", "message": "MCP client failed to start"}},
        # only a completed item (no started): still announced once
        {"type": "item.completed", "item": {"id": "i1", "type": "mcp_tool_call", "server": "aether",
                                            "tool": "run_openfoam", "arguments": {}, "result": None,
                                            "error": {"message": "requires approval"}, "status": "failed"}},
        {"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": "ls",
                                          "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "ls",
                                            "exit_code": 1, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "i3", "type": "mcp_tool_call", "server": "other", "tool": "x",
                                            "arguments": {}, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "thinking"}},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "turn.failed", "error": {"message": "stream disconnected"}},
    ])
    assert out[0]["type"] == "init"
    assert out[1] == {"type": "error", "message": "MCP client failed to start"}
    assert out[2] == {"type": "tool_call", "id": "i1", "name": "run_openfoam", "input": {}}
    assert out[3] == {"type": "tool_result", "id": "i1", "is_error": True}
    assert out[4]["name"] == "shell" and out[5] == {"type": "tool_result", "id": "i2", "is_error": True}
    assert out[6]["name"] == "other.x" and out[7]["is_error"] is False
    assert out[8] == {"type": "error", "message": "Reconnecting... 1/5"}
    assert out[9]["type"] == "result" and out[9]["is_error"] and out[9]["message"] == "stream disconnected"


def test_codex_init_once_per_conversation():
    m = CodexEventMapper()
    assert m.feed({"type": "thread.started", "thread_id": "t1"})[0]["type"] == "init"
    m.begin_turn()
    assert m.feed({"type": "thread.started", "thread_id": "t1"}) == []


# ------------------------------------------------------------------------------------------------ Codex argv
def test_codex_argv_locks_codex_down(tmp_path):
    s = CodexSession(_codex_cli(), tmp_path, _Bridge(), on_event=lambda e: None, model="gpt-x", distro="Ubuntu",
                     wsl_path="/mnt/c/case")
    a = s.argv()
    assert a[1:4] == ["exec", "--json", "--skip-git-repo-check"] and a[-1] == "-"
    assert "--ignore-user-config" in a and "--ignore-rules" not in a  # only flags this build has
    disabled = [a[i + 1] for i, x in enumerate(a) if x == "--disable"]
    assert {"shell_tool", "unified_exec", "multi_agent"} <= set(disabled)
    assert "code_mode_host" not in disabled  # newer builds call MCP tools through it
    assert "apps" not in disabled  # not listed by this build: an unknown --disable would be an error
    kv = [a[i + 1] for i, x in enumerate(a) if x == "-c"]
    assert 'sandbox_mode="read-only"' in kv and 'approval_policy="never"' in kv
    assert 'mcp_servers.aether.default_tools_approval_mode="approve"' in kv
    assert 'web_search="disabled"' in kv
    env = next(x for x in kv if x.startswith("mcp_servers.aether.env="))
    assert '"127.0.0.1:5555"' in env and '"tok"' in env
    assert next(x for x in kv if x.startswith("mcp_servers.aether.command=")) == \
        f"mcp_servers.aether.command='{sys.executable}'"
    instr = json.loads(next(x for x in kv if x.startswith("developer_instructions="))
                       .removeprefix("developer_instructions="))
    assert str(tmp_path) in instr and "Ubuntu" in instr and "/mnt/c/case" in instr
    assert "Read/Grep/Glob" not in instr and "no shell" in instr
    assert a[a.index("-m") + 1] == "gpt-x"
    r = s.argv("thread-9")
    assert r[1:3] == ["exec", "resume"] and r[-2:] == ["thread-9", "-"]


def test_toml_strings_keep_windows_paths():
    assert _toml_str(r"C:\Program Files\x\python.exe") == r"'C:\Program Files\x\python.exe'"
    assert _toml_str("it's") == '"it\'s"'


# ------------------------------------------------------------------------------------------------ Codex session
def _fake_session(tmp_path, events: Path, *extra: str, on_event=None) -> tuple[CodexSession, Path, list]:
    got: list = []
    log = tmp_path / "argv.jsonl"
    s = CodexSession(_codex_cli(), tmp_path, _Bridge(), on_event=on_event or got.append)
    real = s.argv
    s.argv = lambda rid=None: [sys.executable, str(FIX / "fake_codex.py"), str(events), str(log), *extra,
                               *real(rid)[1:]]
    return s, log, got


def _wait_result(got: list, n: int = 1, timeout: float = 30) -> None:
    import time

    t0 = time.monotonic()
    while sum(e["type"] == "result" for e in got) < n:
        assert time.monotonic() - t0 < timeout, got
        time.sleep(0.05)


def test_codex_session_turns_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CACHE_DIR", str(tmp_path / "cache"))
    s, log, got = _fake_session(tmp_path, RECORDED)
    s.start()
    assert s.alive
    s.send("first question")
    _wait_result(got)
    assert s.session_id == "01a12b9f-f5b1-7eb0-ba64-acc5abd39325"
    s.proc.wait(10)
    s.send("second question")
    _wait_result(got, 2)
    runs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert runs[0]["stdin"] == "first question" and "resume" not in runs[0]["argv"]
    assert runs[1]["argv"][:2] == ["exec", "resume"] and s.session_id in runs[1]["argv"]
    assert [e["type"] for e in got].count("init") == 1
    s.stop()
    assert not s.alive


def test_codex_session_reports_a_crash_and_an_interrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CACHE_DIR", str(tmp_path / "cache"))
    empty = tmp_path / "none.jsonl"
    empty.write_text("", encoding="utf-8")
    s, _log, got = _fake_session(tmp_path, empty, "--exit", "3")
    s.start()
    s.send("hello")
    _wait_result(got)
    res = got[-1]
    assert res["is_error"] and "code 3" in res["message"] and "boom" in res["message"]

    done = threading.Event()
    got2: list = []
    s2, _log2, _ = _fake_session(tmp_path, RECORDED, "--sleep", "30",
                                 on_event=lambda e: (got2.append(e), e["type"] == "result" and done.set()))
    s2.start()
    s2.send("long one")
    import time

    time.sleep(1.0)
    with pytest.raises(RuntimeError):
        s2.send("too early")  # a second message while one runs is refused, not interleaved
    s2.interrupt()
    assert done.wait(20)
    assert got2[-1]["is_error"] and got2[-1]["message"] == "stopped"


# ------------------------------------------------------------------------------------------------ Codex CLI
def _cp(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


def test_codex_auth_status(monkeypatch):
    cli = _codex_cli()
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(0, "Logged in using ChatGPT\n"))
    a = codex_cli.auth_status(cli)
    assert a.logged_in and a.method == "chatgpt" and a.detail == "ChatGPT"
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(0, "Logged in using an API key - sk-***\n"))
    assert codex_cli.auth_status(cli).method == "api-key"
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(1, "Not logged in\n"))
    a = codex_cli.auth_status(cli)
    assert not a.logged_in and a.detail == "Not logged in"
    # an older CLI that cannot parse a config.toml written by a newer Codex app (seen on the dev PC)
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(
        1, "", "Error loading configuration: C:\\Users\\x\\.codex\\config.toml:3:16: unknown variant `default`"))
    a = codex_cli.auth_status(cli)
    assert not a.logged_in and a.method == "config-error" and "npm install -g @openai/codex@latest" in a.detail
    assert codex_cli.login_argv(cli) == [str(cli.exe), "login"]


def test_find_codex_prefers_setting_then_newest(monkeypatch, tmp_path):
    exes = {}
    for name, ver in (("old.exe", "0.114.0"), ("new.exe", "0.159.2"), ("mine.exe", "0.100.0")):
        p = tmp_path / name
        p.write_bytes(b"")
        exes[str(p)] = ver
    monkeypatch.setattr(codex_cli, "candidates", lambda configured=None: (
        ([(Path(configured), "setting")] if configured else [])
        + [(tmp_path / "old.exe", "npm"), (tmp_path / "missing.exe", "native"), (tmp_path / "new.exe", "desktop-app")]))
    monkeypatch.setattr(codex_cli, "probe", lambda exe, src, timeout=30: CliInfo(exe, exes[str(exe)], src))
    best = codex_cli.find_codex(with_capabilities=False)
    assert best is not None and best.version == "0.159.2" and best.source == "desktop-app"
    mine = codex_cli.find_codex(str(tmp_path / "mine.exe"), with_capabilities=False)
    assert mine is not None and mine.source == "setting"


def test_codex_candidates_prefer_the_native_exe_over_the_npm_shim(monkeypatch, tmp_path):
    npm = tmp_path / "npm"
    native = npm / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/codex"
    native.mkdir(parents=True)
    (native / "codex.exe").write_bytes(b"")
    (npm / "codex.cmd").write_text("@echo off", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: str(npm / "codex.cmd"))
    c = codex_cli.candidates()
    assert c[0] == (native / "codex.exe", "npm")
    assert c[-1] == (npm / "codex.cmd", "npm-shim")


def test_codex_probe_parses_version(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(0, "codex-cli 0.159.2\n"))
    assert codex_cli.probe(tmp_path / "codex.exe", "path").version == "0.159.2"
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(0, "something else 1.0\n"))
    assert codex_cli.probe(tmp_path / "codex.exe", "path") is None


def test_codex_model_catalog(monkeypatch):
    catalog = {"models": [{"slug": "b", "display_name": "B", "visibility": "list", "priority": 2},
                          {"slug": "hidden", "display_name": "H", "visibility": "hide", "priority": 0},
                          {"slug": "a", "display_name": "A", "visibility": "list", "priority": 1}]}
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(0, json.dumps(catalog)))
    assert codex_cli.list_models(_codex_cli()) == [("A", "a"), ("B", "b")]
    assert PROVIDERS["codex"].models(_codex_cli())[0] == ("Default model", None)
    monkeypatch.setattr(codex_cli, "_run", lambda argv, timeout: _cp(2, "error: unrecognized subcommand"))
    assert codex_cli.list_models(_codex_cli()) == []


def test_provider_registry():
    assert get_provider(None).id == "claude" and get_provider("nope").id == "claude"
    assert get_provider("codex").label == "Codex"
    for p in PROVIDERS.values():
        assert p.install_hint and p.login_hint and p.default_models[0][1] is None
