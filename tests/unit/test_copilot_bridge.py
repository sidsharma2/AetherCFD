"""Copilot plumbing without Claude: MCP server process <-> bridge <-> tools <-> executor/journal."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from aethercfd.commands.executor import AutoApprove, DenyAll, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.copilot.bridge import BridgeServer
from aethercfd.copilot.tools import ToolContext, ToolSet
from aethercfd.core.runtime import module_argv
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def _session(tmp_path: Path) -> Session:
    case = tmp_path / "cavity"
    shutil.copytree(FIX, case)
    return Session(case, create_project=True, with_backend=False)


def test_tools_read_and_approved_edit(tmp_path):
    s = _session(tmp_path)
    ts = ToolSet(ToolContext(s, executor=Executor(default_registry(), s.context, AutoApprove())))
    names = {t["name"] for t in ts.describe()}
    assert {"case_overview", "dict_get", "dict_set", "run_openfoam"} <= names
    assert "show_field" not in names  # GUI-only tools are hidden without a GUI
    ok, text = ts.call("case_overview", {})
    assert ok and json.loads(text)["solver"] == "icoFoam"
    ok, text = ts.call("dict_get", {"file": "system/controlDict", "key": "endTime"})
    assert ok and json.loads(text)["value"].strip() == "0.5"
    # agents must justify changes
    ok, text = ts.call("dict_set", {"file": "system/controlDict", "key": "endTime", "value": "1"})
    assert not ok and "rationale" in text
    ok, text = ts.call("dict_set", {"file": "system/controlDict", "key": "endTime", "value": "1",
                                    "rationale": "longer run", "confidence": 0.9})
    assert ok, text
    assert "endTime         1;" in (s.root / "system" / "controlDict").read_text()
    entry = next(s.project.journal.entries(newest_first=True))
    assert entry["actor"]["kind"] == "agent" and entry["rationale"] == "longer run"
    # paths outside the case are refused
    ok, text = ts.call("read_file", {"path": str(tmp_path)})
    assert not ok and "outside" in text


def test_declined_plan_tells_the_model(tmp_path):
    s = _session(tmp_path)
    ts = ToolSet(ToolContext(s, executor=Executor(default_registry(), s.context, DenyAll())))
    before = (s.root / "system" / "controlDict").read_bytes()
    ok, text = ts.call("dict_set", {"file": "system/controlDict", "key": "endTime", "value": "9",
                                    "rationale": "x", "confidence": 0.5})
    assert not ok and "declined" in text
    assert (s.root / "system" / "controlDict").read_bytes() == before


def test_mcp_server_process_roundtrip(tmp_path):
    s = _session(tmp_path)
    ts = ToolSet(ToolContext(s, executor=Executor(default_registry(), s.context, AutoApprove())))
    bridge = BridgeServer(lambda: ts).start()
    try:
        env = {**os.environ, "AETHER_BRIDGE": bridge.endpoint, "AETHER_TOKEN": bridge.token}
        p = subprocess.Popen(module_argv("aethercfd.copilot.mcp_server"), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, env=env)
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "dict_get", "arguments": {"file": "system/fvSolution", "key": "PISO/nCorrectors"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}]
        out, _ = p.communicate("".join(json.dumps(r) + "\n" for r in reqs).encode(), timeout=60)
        replies = {m["id"]: m for m in map(json.loads, out.decode().splitlines())}
        assert replies[1]["result"]["serverInfo"]["name"] == "aethercfd"
        assert any(t["name"] == "dict_set" for t in replies[2]["result"]["tools"])
        r3 = replies[3]["result"]
        assert not r3["isError"] and json.loads(r3["content"][0]["text"])["value"].strip() == "2"
        assert replies[4]["result"]["isError"]
    finally:
        bridge.stop()


def test_bridge_rejects_bad_token(tmp_path):
    s = _session(tmp_path)
    bridge = BridgeServer(lambda: ToolSet(ToolContext(s))).start()
    try:
        from aethercfd.copilot.bridge import BridgeClient

        c = BridgeClient(bridge.endpoint, "wrong")
        assert c.request("list") == {"error": "bad token", "id": 1}
    finally:
        bridge.stop()
