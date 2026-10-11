"""What every copilot provider (an agent CLI: Claude Code, OpenAI Codex, ...) offers AetherCFD (no Qt here).

A provider finds its CLI, reports whether the user is signed in, gives the argv of its own interactive
sign-in (AetherCFD never handles credentials), lists models and starts a :class:`CopilotSessionBase` for
one case. Every provider gets the same AetherCFD MCP bridge and the same system prompt, and must keep
its own shell/file-writing tools off: changes and runs only go through the approvable, journalled
``aether`` tools.

Sessions report a provider-neutral event stream (plain dicts) that the Copilot panel and the headless
CLI render:

    {"type": "init", "model": str, "session_id": str | None, "tools_failed": [server names]}
    {"type": "turn_start"}                          a new assistant message begins (streaming)
    {"type": "text_delta", "text": str}             more words of the current message
    {"type": "message", "text": str}                the complete text of the current message (it
                                                    replaces the streamed text, or is a new message)
    {"type": "tool_call", "id": str, "name": str, "input": dict}     names without the mcp prefix
    {"type": "tool_result", "id": str, "is_error": bool}
    {"type": "error", "message": str}               a problem worth showing; the turn may go on
    {"type": "result", "session_id": str | None, "num_turns": int | None, "cost_usd": float | None,
     "usage": {"input_tokens": int, "output_tokens": int} | None, "is_error": bool, "message": str}
                                                    the request is finished (always sent once)
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MCP_SERVER = "aether"  # the name AetherCFD's MCP server has in every provider


@dataclass
class CliInfo:
    exe: Path
    version: str
    source: str  # setting | path | native | npm | desktop-app | ...
    extra: dict = field(default_factory=dict)  # provider-specific capabilities found while probing


@dataclass
class AuthStatus:
    logged_in: bool
    method: str = "none"
    detail: str = ""


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def result_event(*, session_id: str | None = None, num_turns: int | None = None, cost_usd: float | None = None,
                 usage: dict | None = None, is_error: bool = False, message: str = "") -> dict:
    return {"type": "result", "session_id": session_id, "num_turns": num_turns, "cost_usd": cost_usd,
            "usage": usage, "is_error": is_error, "message": message}


class CopilotSessionBase(ABC):
    """One conversation about one case. ``on_event`` gets the neutral events above (from a worker thread)."""

    session_id: str | None
    cost_usd: float

    @property
    @abstractmethod
    def alive(self) -> bool:
        """True while the conversation can take another message."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def interrupt(self) -> None: ...

    @abstractmethod
    def stop(self, timeout: float = 5.0) -> None: ...


class Provider(ABC):
    id: str
    label: str  # shown in the picker and the transcript ("Claude", "Codex")
    product: str  # the CLI's name ("Claude Code", "OpenAI Codex CLI")
    exe_setting: str  # Settings field with an explicit path to the CLI
    install_hint: str
    login_hint: str
    default_models: list[tuple[str, str | None]]

    @abstractmethod
    def find(self, configured: str | None = None) -> CliInfo | None: ...

    @abstractmethod
    def auth_status(self, cli: CliInfo, timeout: float = 30) -> AuthStatus: ...

    @abstractmethod
    def login_argv(self, cli: CliInfo) -> list[str]:
        """The CLI's own interactive sign-in; run it in a visible console (the browser opens)."""

    def models(self, cli: CliInfo | None) -> list[tuple[str, str | None]]:
        """(label, model id or None for the CLI's default). May ask the CLI; call it off the GUI thread."""
        return list(self.default_models)

    @abstractmethod
    def session(self, cli: CliInfo, case_root: Path, bridge, *, on_event: Callable[[dict], None],
                on_exit: Callable[[int], None] | None = None, model: str | None = None,
                resume: str | None = None, allow_web: bool = False, distro: str = "", wsl_path: str = "",
                extra_prompt: str = "") -> CopilotSessionBase: ...

    def configured_exe(self, settings) -> str | None:
        return getattr(settings, self.exe_setting, None) if settings is not None else None
