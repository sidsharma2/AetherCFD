"""The copilot providers AetherCFD knows: Claude Code (the default) and OpenAI Codex (no Qt here)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from aethercfd.copilot.provider import AuthStatus, CliInfo, CopilotSessionBase, Provider

DEFAULT_PROVIDER = "claude"


class ClaudeProvider(Provider):
    id = "claude"
    label = "Claude"
    product = "Claude Code"
    exe_setting = "claude_exe"
    install_hint = ("Claude Code was not found. Install it (claude.com/code: the Windows installer, or "
                    "`npm install -g @anthropic-ai/claude-code`), or open the Claude desktop app once; then press "
                    "Check again.")
    login_hint = "claude auth login"
    default_models: ClassVar[list[tuple[str, str | None]]] = [("Default model", None), ("Opus", "opus"),
                                                              ("Sonnet", "sonnet"), ("Haiku", "haiku")]

    def find(self, configured: str | None = None) -> CliInfo | None:
        from aethercfd.copilot.claude_cli import find_claude

        return find_claude(configured)

    def auth_status(self, cli: CliInfo, timeout: float = 30) -> AuthStatus:
        from aethercfd.copilot.claude_cli import auth_status

        return auth_status(cli, timeout)

    def login_argv(self, cli: CliInfo) -> list[str]:
        from aethercfd.copilot.claude_cli import login_argv

        return login_argv(cli)

    def session(self, cli: CliInfo, case_root: Path, bridge, *, on_event: Callable[[dict], None],
                on_exit: Callable[[int], None] | None = None, model: str | None = None,
                resume: str | None = None, allow_web: bool = False, distro: str = "", wsl_path: str = "",
                extra_prompt: str = "") -> CopilotSessionBase:
        from aethercfd.copilot.session import ClaudeSession

        return ClaudeSession(cli, case_root, bridge, on_event=on_event, on_exit=on_exit, model=model, resume=resume,
                             allow_web=allow_web, distro=distro, wsl_path=wsl_path, extra_prompt=extra_prompt)


class CodexProvider(Provider):
    id = "codex"
    label = "Codex"
    product = "OpenAI Codex CLI"
    exe_setting = "codex_exe"
    install_hint = ("The OpenAI Codex CLI was not found. Install it with `npm install -g @openai/codex` (needs "
                    "Node.js) or install the Codex desktop app; then press Check again.")
    login_hint = "codex login"
    default_models: ClassVar[list[tuple[str, str | None]]] = [("Default model", None)]

    def find(self, configured: str | None = None) -> CliInfo | None:
        from aethercfd.copilot.codex_cli import find_codex

        return find_codex(configured)

    def auth_status(self, cli: CliInfo, timeout: float = 30) -> AuthStatus:
        from aethercfd.copilot.codex_cli import auth_status

        return auth_status(cli, timeout)

    def login_argv(self, cli: CliInfo) -> list[str]:
        from aethercfd.copilot.codex_cli import login_argv

        return login_argv(cli)

    def models(self, cli: CliInfo | None) -> list[tuple[str, str | None]]:
        from aethercfd.copilot.codex_cli import list_models

        listed = list_models(cli) if cli is not None else []
        return [("Default model", None), *listed]

    def session(self, cli: CliInfo, case_root: Path, bridge, *, on_event: Callable[[dict], None],
                on_exit: Callable[[int], None] | None = None, model: str | None = None,
                resume: str | None = None, allow_web: bool = False, distro: str = "", wsl_path: str = "",
                extra_prompt: str = "") -> CopilotSessionBase:
        from aethercfd.copilot.codex_session import CodexSession

        return CodexSession(cli, case_root, bridge, on_event=on_event, on_exit=on_exit, model=model, resume=resume,
                            allow_web=allow_web, distro=distro, wsl_path=wsl_path, extra_prompt=extra_prompt)


PROVIDERS: dict[str, Provider] = {p.id: p for p in (ClaudeProvider(), CodexProvider())}


def get_provider(pid: str | None) -> Provider:
    """The provider with this id; Claude Code for anything unknown."""
    return PROVIDERS.get(pid or DEFAULT_PROVIDER) or PROVIDERS[DEFAULT_PROVIDER]
