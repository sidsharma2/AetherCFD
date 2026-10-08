"""Bridge between the copilot's MCP server process and the AetherCFD process that owns the case.

Claude Code spawns ``aethercfd.copilot.mcp_server`` (stdio MCP). That process has no state of its own:
it relays ``tools/list`` and ``tools/call`` here over a loopback TCP socket (newline-delimited JSON, a
random per-session token, 127.0.0.1 only). Tool calls run on their own threads so a long run or a
pending approval does not hold up a quick read; replies carry the request id.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import socketserver
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class BridgeServer:
    def __init__(self, toolset: Callable[[], Any]):
        """``toolset()`` returns the current ToolSet (it follows the open case)."""
        self.toolset = toolset
        self.token = secrets.token_hex(16)
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.bridge = self
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, name="copilot-bridge", daemon=True)
        self.on_call: Callable[[str, dict, bool | None, str | None], None] | None = None  # activity feed

    @property
    def endpoint(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self) -> BridgeServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def handle(self, msg: dict) -> dict:
        if not secrets.compare_digest(str(msg.get("token", "")), self.token):
            return {"error": "bad token"}
        op = msg.get("op")
        ts = self.toolset()
        if ts is None:
            return {"error": "no case is open in AetherCFD"}
        if op == "list":
            return {"tools": ts.describe()}
        if op == "call":
            name, args = str(msg.get("name")), msg.get("arguments") or {}
            if self.on_call:
                self.on_call(name, args, None, None)
            ok, text = ts.call(name, args)
            if self.on_call:
                self.on_call(name, args, ok, text)
            return {"ok": ok, "text": text}
        return {"error": f"unknown op {op!r}"}


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    bridge: BridgeServer


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        lock = threading.Lock()

        def answer(msg: dict) -> None:
            try:
                reply = self.server.bridge.handle(msg)  # type: ignore[attr-defined]
            except Exception as exc:
                log.exception("copilot bridge request failed")
                reply = {"error": f"{type(exc).__name__}: {exc}"}
            reply["id"] = msg.get("id")
            data = (json.dumps(reply) + "\n").encode("utf-8")
            with lock:
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    pass

        try:
            for raw in self.rfile:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                threading.Thread(target=answer, args=(msg,), name="copilot-tool", daemon=True).start()
        except OSError:  # the MCP server process went away (Claude exited)
            pass


class BridgeClient:
    """Used by the MCP server process."""

    def __init__(self, endpoint: str, token: str, *, timeout: float = 10.0):
        host, port = endpoint.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(None)
        self.token = token
        self._r = self.sock.makefile("rb")
        self._wlock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._next = 0
        self._plock = threading.Lock()
        threading.Thread(target=self._read, name="bridge-reader", daemon=True).start()

    def _read(self) -> None:
        for raw in self._r:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            with self._plock:
                slot = self._pending.pop(msg.get("id"), None)
            if slot:
                slot[1].append(msg)
                slot[0].set()
        with self._plock:  # connection closed: fail everything still waiting
            for ev, box in self._pending.values():
                box.append({"error": "AetherCFD closed the connection"})
                ev.set()
            self._pending.clear()

    def request(self, op: str, **kw) -> dict:
        ev, box = threading.Event(), []
        with self._plock:
            self._next += 1
            rid = self._next
            self._pending[rid] = (ev, box)
        data = (json.dumps({"id": rid, "token": self.token, "op": op, **kw}) + "\n").encode("utf-8")
        with self._wlock:
            self.sock.sendall(data)
        ev.wait()
        return box[0]
