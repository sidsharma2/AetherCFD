"""stdio MCP server that Claude Code launches for the AetherCFD copilot.

    python -m aethercfd.copilot.mcp_server        (AetherCFD.exe --run-module ... when packaged)

Environment: AETHER_BRIDGE=127.0.0.1:<port>, AETHER_TOKEN=<token> (set by CopilotSession). Implements the
MCP subset Claude Code uses for tools: initialize, tools/list, tools/call, ping. Every call is relayed
to the AetherCFD process (bridge.py), which owns the case, the approvals and the journal.
"""

from __future__ import annotations

import json
import os
import sys
import threading

from aethercfd.copilot.bridge import BridgeClient

SERVER_INFO = {"name": "aethercfd", "version": "0.2"}
INSTRUCTIONS = ("Tools for the OpenFOAM case open in AetherCFD Studio. Start with case_overview. Changes and runs are "
                "approved by the user and journalled; give each a short rationale and an honest confidence.")


def main() -> int:
    endpoint, token = os.environ.get("AETHER_BRIDGE"), os.environ.get("AETHER_TOKEN")
    if not endpoint or not token:
        print("AETHER_BRIDGE / AETHER_TOKEN not set", file=sys.stderr)
        return 2
    bridge = BridgeClient(endpoint, token)
    out = sys.stdout.buffer
    wlock = threading.Lock()

    def send(obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with wlock:
            out.write(data)
            out.flush()

    def reply(rid, result=None, error=None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        send(msg)

    def call_tool(rid, params: dict) -> None:
        r = bridge.request("call", name=params.get("name"), arguments=params.get("arguments") or {})
        if "error" in r:
            reply(rid, {"content": [{"type": "text", "text": r["error"]}], "isError": True})
        else:
            reply(rid, {"content": [{"type": "text", "text": r.get("text", "")}], "isError": not r.get("ok", False)})

    calls: list[threading.Thread] = []
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            continue  # notifications (initialized, cancelled, ...)
        params = msg.get("params") or {}
        if method == "initialize":
            reply(rid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                        "instructions": INSTRUCTIONS})
        elif method == "ping":
            reply(rid, {})
        elif method == "tools/list":
            r = bridge.request("list")
            if "error" in r:
                reply(rid, error={"code": -32000, "message": r["error"]})
            else:
                reply(rid, {"tools": r["tools"]})
        elif method == "tools/call":
            th = threading.Thread(target=call_tool, args=(rid, params), daemon=True)
            th.start()
            calls.append(th)
            calls = [c for c in calls if c.is_alive()]
        elif method in ("resources/list", "prompts/list"):
            reply(rid, {method.split("/")[0]: []})
        else:
            reply(rid, error={"code": -32601, "message": f"method not found: {method}"})
    for th in calls:  # stdin closed: answer what is still in flight before exiting
        th.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
