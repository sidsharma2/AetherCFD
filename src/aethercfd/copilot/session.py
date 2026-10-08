"""CopilotSession: one Claude Code conversation about one case (no Qt here).

Claude Code runs headless (``claude -p``) with stream-json in both directions, so the conversation stays
open and every token, tool call and result streams to the UI as an event. It may only use the AetherCFD
MCP tools (changes and runs go through the user's approval and the journal) plus its read-only
Read/Grep/Glob tools; Bash/Edit/Write are disabled. The Claude session id is kept per case so a
conversation can be resumed after restarting AetherCFD.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from aethercfd.copilot.bridge import BridgeServer
from aethercfd.copilot.claude_cli import NO_WINDOW, ClaudeCli
from aethercfd.core.runtime import module_argv
from aethercfd.core.settings import cache_dir

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the simulation copilot inside AetherCFD Studio, a desktop workbench that drives OpenFOAM without \
hiding it. The user has this case open: {case} (Windows path). OpenFOAM runs in the WSL distro {distro} with \
the case's own OpenFOAM version; the case's Linux path is {wsl_path}.

How to work:
- Use the `aether` tools. Start with case_overview. Read with read_file / dict_get / log_tail / \
solver_progress / mesh_quality, or your Read/Grep/Glob tools (files outside the case are readable that way too).
- Every change (dict_set, dict_insert, dict_remove, write_file) and every OpenFOAM execution (run_openfoam, \
run_control) is shown to the user as an exact plan (diff, commands, consequences) and needs their approval. \
Give each a one- or two-sentence rationale and an honest confidence. If the user declines, ask what they want \
instead of retrying the same thing.
- Nothing can be deleted (files only ever move to the project trash). Prefer the smallest change that fixes \
the problem; keep comments and layout (dict_set does minimal edits).
- Diagnose from evidence: quote log lines, residual trends and checkMesh numbers. Separate facts from hypotheses.
- After any mesh change run `checkMesh -allGeometry -allTopology` and compare with the previous mesh before \
running a solver. Say how long a run is expected to take before starting it; use wait=false for long solves \
and follow them with run_status / solver_progress.
- Be concise; give units.{extra}"""


class CopilotSession:
    def __init__(self, cli: ClaudeCli, case_root: Path, bridge: BridgeServer, *,
                 on_event: Callable[[dict], None], on_exit: Callable[[int], None] | None = None,
                 model: str | None = None, resume: str | None = None, allow_web: bool = False,
                 distro: str = "LMAE-OpenFOAM", wsl_path: str = "", extra_prompt: str = ""):
        self.cli = cli
        self.case_root = Path(case_root)
        self.bridge = bridge
        self.on_event = on_event
        self.on_exit = on_exit
        self.model = model
        self.resume = resume
        self.allow_web = allow_web
        self.distro = distro
        self.wsl_path = wsl_path
        self.extra_prompt = extra_prompt
        self.session_id: str | None = resume
        self.proc: subprocess.Popen | None = None
        self.cost_usd = 0.0
        self._wlock = threading.Lock()
        self._req = 0

    # ------------------------------------------------------------------ lifecycle
    def mcp_config_path(self) -> Path:
        argv = module_argv("aethercfd.copilot.mcp_server")
        cfg = {"mcpServers": {"aether": {"type": "stdio", "command": argv[0], "args": argv[1:],
                                         "env": {"AETHER_BRIDGE": self.bridge.endpoint,
                                                 "AETHER_TOKEN": self.bridge.token}}}}
        d = cache_dir() / "copilot"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"mcp-{os.getpid()}-{self.bridge.port}.json"
        p.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        return p

    def argv(self) -> list[str]:
        allowed = ["mcp__aether", "Read", "Grep", "Glob"] + (["WebSearch", "WebFetch"] if self.allow_web else [])
        denied = ["Bash", "Edit", "Write", "NotebookEdit"] + ([] if self.allow_web else ["WebSearch", "WebFetch"])
        extra = f"\n- {self.extra_prompt}" if self.extra_prompt else ""
        prompt = SYSTEM_PROMPT.format(case=self.case_root, distro=self.distro, wsl_path=self.wsl_path or "?",
                                      extra=extra)
        a = [str(self.cli.exe), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--mcp-config", str(self.mcp_config_path()), "--strict-mcp-config",
             "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied),
             "--permission-prompts", "none", "--append-system-prompt", prompt]
        if self.model:
            a += ["--model", self.model]
        if self.resume:
            a += ["--resume", self.resume]
        return a

    def start(self) -> None:
        env = dict(os.environ)
        env["MCP_TOOL_TIMEOUT"] = str(4 * 3600 * 1000)  # approvals and runs can take a while
        env["MCP_TIMEOUT"] = "60000"
        errlog = cache_dir() / "copilot" / "claude-stderr.log"
        self._err = open(errlog, "ab")  # noqa: SIM115 -- closed in _pump
        self.proc = subprocess.Popen(self.argv(), cwd=str(self.case_root), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._err, env=env, creationflags=NO_WINDOW)
        threading.Thread(target=self._pump, name="copilot-claude", daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            if ev.get("type") == "result" and isinstance(ev.get("total_cost_usd"), (int, float)):
                self.cost_usd = float(ev["total_cost_usd"])
            try:
                self.on_event(ev)
            except Exception:
                log.exception("copilot event handler failed")
        code = self.proc.wait()
        self._err.close()
        if self.on_exit:
            self.on_exit(code)

    # ------------------------------------------------------------------ talking
    def _write(self, obj: dict) -> None:
        if not self.alive:
            raise RuntimeError("the Claude process is not running")
        assert self.proc is not None and self.proc.stdin is not None
        with self._wlock:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def send(self, text: str) -> None:
        self._write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def interrupt(self) -> None:
        self._req += 1
        self._write({"type": "control_request", "request_id": f"aether-{self._req}",
                     "request": {"subtype": "interrupt"}})

    def stop(self, timeout: float = 5.0) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.terminate()


def text_of(ev: dict) -> str:
    """Plain text of an assistant message event."""
    msg = ev.get("message") or {}
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
