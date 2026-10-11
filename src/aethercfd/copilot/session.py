"""ClaudeSession: one Claude Code conversation about one case (no Qt here).

Claude Code runs headless (``claude -p``) with stream-json in both directions, so the conversation stays
open and every token, tool call and result streams to the UI as an event. It may only use the AetherCFD
MCP tools (changes and runs go through the user's approval and the journal) plus its read-only
Read/Grep/Glob tools; Bash/Edit/Write are disabled. The Claude session id is kept per case so a
conversation can be resumed after restarting AetherCFD. Claude's stream-json events are translated into the
provider-neutral events of :mod:`aethercfd.copilot.provider` (:func:`normalize_claude`).
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
from aethercfd.copilot.provider import MCP_SERVER, CopilotSessionBase, result_event
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
- The user is watching the AetherCFD window and learning from you. Drive the app visibly when it helps: \
app_overview to see what they see, app_open_panel / app_perspective / app_select_file / open_file to bring \
things up, add_view_stage + viewport_camera to show results (Q-criterion vortices, slices, streamlines, \
mesh-quality maps), and point_at with a one-sentence bubble when you refer to a panel. Do one visible step at \
a time and say what you are doing; never use point_at as decoration.
- For "show me the result / a nice view", start with show_field then viewport_showcase (it picks the slice \
through the most of the flow, in a glass shell, in the Studio look). To check what the user sees, render_image \
at about 1280x720 and Read the PNG; if the picture is uninformative (a uniform colour, a tiny colour range), \
move or change the slice before describing it. viewport_animation makes a turntable or time sweep; \
open_in_paraview hands the case to ParaView. To run a case the user just created, run_openfoam with \
preset auto (it runs the tutorial's Allrun, or mesh + check + solve) instead of guessing the steps.
- Multi-region (CHT) cases (case_overview lists the regions): the viewport draws every region at once, and the \
fluid hides the solids. For "the temperature in the solids" call show_field(field='T', regions='solids', \
slice_axis=...) or viewport_showcase(regions='solids') in one go: only those regions are drawn, sliced through \
their own middle and framed. regions takes 'solids', 'fluid', 'all', a region name or a list; \
viewport_regions lists the regions (fluid/solid) and what is shown. add_view_stage works on the shown regions \
only (or pass regions). A solid region has no U or p_rgh: show T there.
- Use case_checks for setup problems, mesh_hotspots/grading_design/wall_spacing for mesh work.
- Be concise; give units.{extra}"""


# the sentence about Claude's own read-only file tools, replaced for providers that have none
CLAUDE_FILE_TOOLS = ", or your Read/Grep/Glob tools (files outside the case are readable that way too)"


def system_prompt(case: Path | str, distro: str, wsl_path: str, extra_prompt: str = "", *,
                  file_tools: str | None = None) -> str:
    """The copilot's instructions, the same for every provider. ``file_tools`` replaces the Claude-specific
    mention of Read/Grep/Glob (e.g. "" for a provider whose only file access is the aether tools)."""
    extra = f"\n- {extra_prompt}" if extra_prompt else ""
    text = SYSTEM_PROMPT.format(case=case, distro=distro or "?", wsl_path=wsl_path or "?", extra=extra)
    return text if file_tools is None else text.replace(CLAUDE_FILE_TOOLS, file_tools)


def normalize_claude(ev: dict) -> list[dict]:
    """One Claude Code stream-json event -> the provider-neutral events (see aethercfd.copilot.provider)."""
    t = ev.get("type")
    if t == "stream_event":
        e = ev.get("event") or {}
        if e.get("type") == "message_start":
            return [{"type": "turn_start"}]
        delta = e.get("delta") or {}
        if e.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return [{"type": "text_delta", "text": delta.get("text", "")}]
        return []
    if t == "assistant":
        blocks = (ev.get("message") or {}).get("content") or []
        out = [{"type": "message", "text": "".join(b.get("text", "") for b in blocks if b.get("type") == "text")}]
        for b in blocks:
            if b.get("type") == "tool_use":
                out.append({"type": "tool_call", "id": b.get("id", ""),
                            "name": b.get("name", "").removeprefix(f"mcp__{MCP_SERVER}__"),
                            "input": b.get("input") or {}})
        return out
    if t == "user":
        content = (ev.get("message") or {}).get("content")
        return [{"type": "tool_result", "id": b.get("tool_use_id", ""), "is_error": bool(b.get("is_error"))}
                for b in (content if isinstance(content, list) else []) if b.get("type") == "tool_result"]
    if t == "result":
        failed = bool(ev.get("is_error")) or ev.get("subtype") not in ("success", None)
        cost = ev.get("total_cost_usd")
        usage = ev.get("usage") or None
        return [result_event(session_id=ev.get("session_id"), num_turns=ev.get("num_turns"),
                             cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                             usage={"input_tokens": usage.get("input_tokens", 0),
                                    "output_tokens": usage.get("output_tokens", 0)} if usage else None,
                             is_error=failed, message=str(ev.get("result") or ev.get("subtype") or ""))]
    if t == "system" and ev.get("subtype") == "init":
        return [{"type": "init", "model": ev.get("model", ""), "session_id": ev.get("session_id"),
                 "tools_failed": [m.get("name") for m in ev.get("mcp_servers", []) if m.get("status") != "connected"]}]
    return []


class ClaudeSession(CopilotSessionBase):
    def __init__(self, cli: ClaudeCli, case_root: Path, bridge: BridgeServer, *,
                 on_event: Callable[[dict], None], on_exit: Callable[[int], None] | None = None,
                 model: str | None = None, resume: str | None = None, allow_web: bool = False,
                 distro: str = "", wsl_path: str = "", extra_prompt: str = ""):
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
        cfg = {"mcpServers": {MCP_SERVER: {"type": "stdio", "command": argv[0], "args": argv[1:],
                                         "env": {"AETHER_BRIDGE": self.bridge.endpoint,
                                                 "AETHER_TOKEN": self.bridge.token}}}}
        d = cache_dir() / "copilot"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"mcp-{os.getpid()}-{self.bridge.port}.json"
        p.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        return p

    def argv(self) -> list[str]:
        allowed = [f"mcp__{MCP_SERVER}", "Read", "Grep", "Glob"] + (["WebSearch", "WebFetch"] if self.allow_web else [])
        denied = ["Bash", "Edit", "Write", "NotebookEdit"] + ([] if self.allow_web else ["WebSearch", "WebFetch"])
        prompt = system_prompt(self.case_root, self.distro, self.wsl_path, self.extra_prompt)
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
        errlog.parent.mkdir(parents=True, exist_ok=True)  # first session of a fresh install
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
            for out in normalize_claude(ev):
                try:
                    self.on_event(out)
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


CopilotSession = ClaudeSession  # the older name


def text_of(ev: dict) -> str:
    """Plain text of an assistant message event."""
    msg = ev.get("message") or {}
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
