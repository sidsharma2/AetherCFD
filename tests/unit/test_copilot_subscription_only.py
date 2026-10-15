"""The Claude copilot only runs on a Claude subscription sign-in, never an API key (docs/copilot-accounts.md)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from aethercfd.copilot import claude_cli
from aethercfd.copilot.provider import AuthStatus, CliInfo
from aethercfd.copilot.providers import PROVIDERS

CLI = CliInfo(Path("claude.exe"), "2.1.295", "test")
ON, OFF = SimpleNamespace(copilot_subscription_only=True), SimpleNamespace(copilot_subscription_only=False)
BASE = {"PATH": "x", "ANTHROPIC_API_KEY": "sk-ant-test", "anthropic_auth_token": "t", "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_MODEL": "opus"}


def test_cli_env_strips_api_billing_unless_the_guard_is_off():
    env = claude_cli.cli_env(ON, BASE)
    assert env == {"PATH": "x", "ANTHROPIC_MODEL": "opus"}  # only the billing switches go
    assert claude_cli.cli_env(OFF, BASE) == BASE


def test_auth_status_reads_the_plan_and_checks_with_the_stripped_env(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "email": "me@lab", "subscriptionType": "max"}), "")
    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    monkeypatch.setattr(claude_cli, "subscription_only", lambda settings=None: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    a = claude_cli.auth_status(CLI)
    assert (a.logged_in, a.method, a.detail, a.plan) == (True, "claude.ai", "me@lab", "max")
    assert seen["argv"][1:] == ["auth", "status", "--json"]
    assert "ANTHROPIC_API_KEY" not in {k.upper() for k in seen["env"]}


def test_sign_in_is_the_subscription_flow_and_switch_signs_out():
    p = PROVIDERS["claude"]
    assert p.login_argv(CLI) == [str(CLI.exe), "auth", "login", "--claudeai"]
    assert p.logout_argv(CLI) == [str(CLI.exe), "auth", "logout"]
    assert PROVIDERS["codex"].logout_argv(CLI) is None  # no switch offered where it is not wired


def test_only_a_subscription_sign_in_may_run_the_copilot():
    p = PROVIDERS["claude"]
    assert p.billing_problem(AuthStatus(True, "claude.ai", "me@lab", "max"), ON) is None
    for method in ("console", "api_key", "apiKeyHelper"):
        msg = p.billing_problem(AuthStatus(True, method, "me@lab"), ON)
        assert msg and "Switch account" in msg and method in msg
    assert p.billing_problem(AuthStatus(True, "console", "me@lab"), OFF) is None  # deliberate API setup
    assert p.billing_problem(AuthStatus(False, "none"), ON) is None  # not signed in: the sign-in banner covers it


def test_the_reported_cost_on_a_subscription_is_labelled_as_not_billed():
    p = PROVIDERS["claude"]
    assert p.spend_text(1.134, AuthStatus(True, "claude.ai", "me", "max")) == "$1.13 API-equiv. (max plan, not billed)"
    assert p.spend_text(1.134, AuthStatus(True, "console", "me")) == "est. $1.13"


def test_the_session_starts_claude_without_an_api_key(monkeypatch, tmp_path):
    from aethercfd.copilot import session as sess_mod

    started = {}

    class FakeProc:
        stdin = stdout = None

        def poll(self):
            return 0

    def popen(argv, **kw):
        started["env"] = kw["env"]
        return FakeProc()
    monkeypatch.setattr(sess_mod.subprocess, "Popen", popen)
    monkeypatch.setattr(sess_mod.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(sess_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(claude_cli, "subscription_only", lambda settings=None: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = sess_mod.ClaudeSession(CLI, tmp_path, SimpleNamespace(endpoint="tcp://x", port=1), on_event=lambda e: None)
    monkeypatch.setattr(s, "argv", lambda: ["claude.exe"])
    s.start()
    s._err.close()
    assert "ANTHROPIC_API_KEY" not in {k.upper() for k in started["env"]}
    assert started["env"]["MCP_TIMEOUT"] == "60000"
