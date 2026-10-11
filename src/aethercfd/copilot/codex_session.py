"""CodexSession: one OpenAI Codex conversation about one case (no Qt here).

``codex exec --json`` answers one prompt and exits, so every message is its own process: the first starts a
thread, later ones continue it with ``codex exec resume <thread id>``. The prompt goes in on stdin. Codex
gets the AetherCFD MCP bridge as the ``aether`` server (its tools pre-approved inside Codex, because every
change or run they make still opens AetherCFD's own approval dialog), the same system prompt as Claude (as
``developer_instructions``), a read-only sandbox with approvals off, and its shell, sub-agents, plugins,
browser and computer-use features disabled; ``--ignore-user-config`` keeps the user's own Codex MCP servers
and settings out. Codex's JSONL events are mapped onto the provider-neutral events of
:mod:`aethercfd.copilot.provider` by :class:`CodexEventMapper`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from aethercfd.copilot.provider import (
    MCP_SERVER,
    NO_WINDOW,
    CliInfo,
    CopilotSessionBase,
    result_event,
)
from aethercfd.copilot.session import system_prompt
from aethercfd.core.runtime import module_argv
from aethercfd.core.settings import cache_dir

log = logging.getLogger(__name__)

# Codex features that would let it act outside AetherCFD's approvals (or wander off); only those the installed
# build lists are disabled (an unknown --disable is an error). code_mode_host stays: newer builds call MCP tools
# through it.
DISABLED_FEATURES = ("shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "apps", "plugins", "remote_plugin",
                     "browser_use", "browser_use_external", "in_app_browser", "computer_use", "image_generation",
                     "memories", "hooks", "goals", "sleep_tool", "tool_suggest", "skill_mcp_dependency_install",
                     "workspace_dependencies", "realtime_conversation")
FILE_TOOLS = ("; you have no shell and no file-writing tools of your own, so read and change files only through "
              "these tools (view_image shows a PNG, such as one render_image wrote)")


def _toml_str(s: str) -> str:
    """A TOML string for a -c override: a literal '...' string keeps Windows backslashes as they are."""
    return f"'{s}'" if "'" not in s and "\n" not in s else json.dumps(s)


class CodexEventMapper:
    """Codex ``exec --json`` events -> provider-neutral events. One mapper lives as long as the conversation."""

    def __init__(self, model: str | None = None):
        self.model = model
        self.thread_id: str | None = None
        self.init_sent = False
        self.result_sent = False
        self.tool_calls = 0
        self._announced: set[str] = set()

    def begin_turn(self) -> None:
        self.result_sent = False
        self.tool_calls = 0
        self._announced.clear()

    def _call(self, item: dict, name: str, inp: dict) -> list[dict]:
        iid = str(item.get("id", ""))
        if iid in self._announced:
            return []
        self._announced.add(iid)
        self.tool_calls += 1
        return [{"type": "tool_call", "id": iid, "name": name, "input": inp}]

    def _item(self, kind: str, item: dict) -> list[dict]:
        t, iid, status = item.get("type"), str(item.get("id", "")), item.get("status")
        done = kind == "item.completed"
        if t == "agent_message":
            return [{"type": "message", "text": str(item.get("text", ""))}] if done else []
        if t == "mcp_tool_call":
            server, tool = item.get("server", ""), str(item.get("tool", ""))
            name = tool if server == MCP_SERVER else f"{server}.{tool}"
            out = self._call(item, name, item.get("arguments") or {})
            if done:
                out.append({"type": "tool_result", "id": iid,
                            "is_error": status != "completed" or bool(item.get("error"))})
            return out
        if t == "command_execution":
            out = self._call(item, "shell", {"command": item.get("command", "")})
            if done:
                code = item.get("exit_code")
                out.append({"type": "tool_result", "id": iid,
                            "is_error": status in ("failed", "declined") or (code not in (0, None))})
            return out
        if t == "file_change":
            if not done:
                return []
            paths = [c.get("path", "") for c in item.get("changes", []) if isinstance(c, dict)]
            return [*self._call(item, "file_change", {"path": ", ".join(paths)}),
                    {"type": "tool_result", "id": iid, "is_error": status != "completed"}]
        if t == "web_search":
            out = self._call(item, "web_search", {"query": item.get("query", "")})
            if done:
                out.append({"type": "tool_result", "id": iid, "is_error": False})
            return out
        if t == "error" and done:
            return [{"type": "error", "message": str(item.get("message", ""))}]
        return []  # reasoning, todo_list, collaboration, ...

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        if t == "thread.started":
            self.thread_id = ev.get("thread_id") or self.thread_id
            if not self.init_sent:
                self.init_sent = True
                return [{"type": "init", "model": self.model or "default", "session_id": self.thread_id,
                         "tools_failed": []}]
            return []
        if t in ("item.started", "item.updated", "item.completed"):
            return self._item(t, ev.get("item") or {})
        if t == "turn.completed":
            self.result_sent = True
            u = ev.get("usage") or {}
            return [result_event(session_id=self.thread_id, num_turns=self.tool_calls + 1,
                                 usage={"input_tokens": int(u.get("input_tokens", 0) or 0),
                                        "output_tokens": int(u.get("output_tokens", 0) or 0)} if u else None)]
        if t == "turn.failed":
            self.result_sent = True
            err = ev.get("error") or {}
            return [result_event(session_id=self.thread_id, num_turns=self.tool_calls + 1, is_error=True,
                                 message=str(err.get("message") if isinstance(err, dict) else err or "turn failed"))]
        if t == "error":
            return [{"type": "error", "message": str(ev.get("message", ""))}]
        return []


class CodexSession(CopilotSessionBase):
    def __init__(self, cli: CliInfo, case_root: Path, bridge, *, on_event: Callable[[dict], None],
                 on_exit: Callable[[int], None] | None = None, model: str | None = None, resume: str | None = None,
                 allow_web: bool = False, distro: str = "", wsl_path: str = "", extra_prompt: str = ""):
        self.cli = cli
        self.case_root = Path(case_root)
        self.bridge = bridge
        self.on_event = on_event
        self.on_exit = on_exit
        self.model = model
        self.allow_web = allow_web
        self.distro = distro
        self.wsl_path = wsl_path
        self.extra_prompt = extra_prompt
        self.session_id: str | None = resume
        self.cost_usd = 0.0  # Codex reports tokens, not money (a ChatGPT plan, or an API key billed by OpenAI)
        self.proc: subprocess.Popen | None = None
        self.mapper = CodexEventMapper(model)
        self.mapper.thread_id = resume
        self._started = False
        self._stopped = False
        self._interrupted = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ argv
    def _flags(self) -> set[str]:
        return set(self.cli.extra.get("exec_flags") or [])

    def config_overrides(self) -> list[str]:
        argv = module_argv("aethercfd.copilot.mcp_server")
        env = {"AETHER_BRIDGE": self.bridge.endpoint, "AETHER_TOKEN": self.bridge.token}
        if os.environ.get("PYTHONPATH"):  # source checkouts run with src on PYTHONPATH; Codex filters the env
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        env_toml = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items()) + "}"
        srv = f"mcp_servers.{MCP_SERVER}"
        prompt = system_prompt(self.case_root, self.distro, self.wsl_path, self.extra_prompt, file_tools=FILE_TOOLS)
        kv = [f"{srv}.command={_toml_str(argv[0])}",
              f"{srv}.args={json.dumps(argv[1:])}",
              f"{srv}.env={env_toml}",
              f"{srv}.startup_timeout_sec=60",
              f"{srv}.tool_timeout_sec={4 * 3600}",  # approvals and runs can take a while
              f'{srv}.default_tools_approval_mode="approve"',  # AetherCFD's own dialog approves edits and runs
              'sandbox_mode="read-only"',
              'approval_policy="never"',  # with the read-only sandbox, its apply_patch tool is refused
              f'web_search="{"live" if self.allow_web else "disabled"}"',
              f"developer_instructions={json.dumps(prompt)}"]
        out: list[str] = []
        for x in kv:
            out += ["-c", x]
        return out

    def argv(self, resume_id: str | None = None) -> list[str]:
        flags = self._flags()
        a = [str(self.cli.exe), "exec"] + (["resume"] if resume_id else [])
        a += ["--json", "--skip-git-repo-check"]
        a += [f for f in ("--ignore-user-config", "--ignore-rules") if f in flags]
        known = set(self.cli.extra.get("features") or [])
        for feat in DISABLED_FEATURES:
            if feat in known:
                a += ["--disable", feat]
        if self.model:
            a += ["-m", self.model]
        a += self.config_overrides()
        if resume_id:
            a.append(resume_id)
        a.append("-")  # the prompt comes on stdin
        return a

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not Path(self.cli.exe).is_file():
            raise FileNotFoundError(f"Codex CLI not found at {self.cli.exe}")
        self._started = True

    @property
    def alive(self) -> bool:
        return self._started and not self._stopped

    @property
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, text: str) -> None:
        if not self.alive:
            raise RuntimeError("the Codex conversation is closed")
        with self._lock:
            if self.busy:
                raise RuntimeError("Codex is still answering")
            self.mapper.begin_turn()
            self._interrupted = False
            errlog = cache_dir() / "copilot" / "codex-stderr.log"
            errlog.parent.mkdir(parents=True, exist_ok=True)
            err = open(errlog, "ab")  # noqa: SIM115 -- closed in _pump
            start_at = err.tell()
            self.proc = subprocess.Popen(self.argv(self.session_id), cwd=str(self.case_root), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=err, creationflags=NO_WINDOW)
            proc = self.proc
        try:
            assert proc.stdin is not None
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass  # it exited at once; _pump reports why
        threading.Thread(target=self._pump, args=(proc, err, errlog, start_at), name="copilot-codex",
                         daemon=True).start()

    def _emit(self, ev: dict) -> None:
        try:
            self.on_event(ev)
        except Exception:
            log.exception("copilot event handler failed")

    def _pump(self, proc: subprocess.Popen, err, errlog: Path, start_at: int) -> None:
        assert proc.stdout is not None
        finished = False  # this turn's own flag: the next turn may already have started when this thread ends
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:  # not JSON (or not UTF-8): skip the line, never lose the turn
                continue
            if not isinstance(ev, dict):
                continue
            for out in self.mapper.feed(ev):
                finished = finished or out["type"] == "result"
                self._emit(out)
            if self.mapper.thread_id:
                self.session_id = self.mapper.thread_id
        code = proc.wait()
        err.close()
        if not finished:
            if self._interrupted:
                msg = "stopped"
            else:
                try:
                    with open(errlog, "rb") as f:
                        f.seek(start_at)
                        tail = f.read().decode("utf-8", errors="replace").strip().splitlines()[-6:]
                except OSError:
                    tail = []
                msg = f"Codex exited (code {code}) before finishing" + (": " + " | ".join(tail) if tail else "")
            self._emit(result_event(session_id=self.session_id, is_error=True, message=msg))

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":  # the whole tree: codex.exe, its MCP server child, its code-mode host
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)
        else:
            proc.kill()

    def interrupt(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._interrupted = True
            self._kill(proc)

    def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True
        self.interrupt()
        proc = self.proc
        if proc is not None:
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
