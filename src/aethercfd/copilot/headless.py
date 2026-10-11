"""Headless copilot: one prompt, streamed to the console (``aethercfd-cli copilot <case> "<prompt>"
[--provider claude|codex]``).

Approvals are a policy instead of a dialog -- every plan is printed in full either way:
  none   only reads (every change/run is declined, Claude is told so)
  edits  approve file edits, decline OpenFOAM runs
  all    approve edits and runs; destructive plans (re-meshing a case with results, kill) only with
         --allow-destructive
Meant for scratch copies of cases; the journal records every approval as "policy:<level>".
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from aethercfd.commands.base import CommandPlan, Risk
from aethercfd.commands.executor import Approval, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.copilot.bridge import BridgeServer
from aethercfd.copilot.providers import PROVIDERS, get_provider
from aethercfd.copilot.tools import ToolContext, ToolSet


class PolicyApprover:
    def __init__(self, level: str, *, allow_destructive: bool = False, out=sys.stdout):
        self.level = level
        self.allow_destructive = allow_destructive
        self.out = out

    def request(self, plan: CommandPlan) -> Approval:
        ok = {"none": False, "edits": plan.risk is Risk.EDIT,
              "all": plan.risk is not Risk.DESTRUCTIVE or self.allow_destructive}.get(self.level, False)
        print("\n" + "=" * 78 + f"\nPLAN ({'APPROVED' if ok else 'DECLINED'} by policy '{self.level}')\n"
              + plan.describe(), file=self.out)
        for fc in plan.file_changes:
            if fc.diff:
                print(fc.diff[:8000], file=self.out)
        print("=" * 78, file=self.out, flush=True)
        if ok:
            return Approval(True, f"policy:{self.level}")
        return Approval(False, f"policy:{self.level}", f"headless policy '{self.level}' does not approve "
                                                      f"{plan.risk.value} plans")


def run(case: str, prompt: str, *, approve: str = "none", allow_destructive: bool = False, model: str | None = None,
        timeout_min: float = 120.0, resume: str | None = None, provider: str | None = None, out=sys.stdout) -> int:
    from aethercfd.scripting.api import Session

    s = Session(case, create_project=True)
    prov = get_provider(provider or getattr(s.settings, "copilot_provider", None))
    cli = prov.find(prov.configured_exe(s.settings))
    if cli is None:
        print(prov.install_hint, file=out)
        return 3
    st = prov.auth_status(cli)
    if not st.logged_in:
        print(st.detail if st.method == "config-error" else
              f"{prov.product} ({cli.exe}) is not signed in. Run:\n  \"" + "\" \"".join(prov.login_argv(cli)) + "\"",
              file=out)
        return 4
    executor = Executor(default_registry(), s.context, PolicyApprover(approve, allow_destructive=allow_destructive,
                                                                      out=out))
    tools = ToolSet(ToolContext(s, executor=executor))
    bridge = BridgeServer(lambda: tools).start()

    def on_call(name, args, ok, text):
        if ok is None:
            short = {k: (v if len(str(v)) < 120 else str(v)[:117] + "...") for k, v in args.items()
                     if k not in ("content",)}
            print(f"\n  -> {name} {short}", file=out, flush=True)
        else:
            print(f"  <- {name}: {'ok' if ok else 'ERROR'} ({len(text or '')} chars)", file=out, flush=True)

    bridge.on_call = on_call
    done = threading.Event()
    state = {"result": None, "streaming": False}

    def on_event(ev: dict) -> None:  # provider-neutral events (aethercfd.copilot.provider)
        t = ev.get("type")
        if t == "text_delta":
            print(ev.get("text", ""), end="", file=out, flush=True)
            state["streaming"] = True
        elif t == "message" and not state["streaming"]:
            if ev.get("text"):
                print(ev["text"], file=out, flush=True)
        elif t == "message":
            state["streaming"] = False
            print("", file=out, flush=True)
        elif t == "error":
            print(f"[{prov.label}] {ev.get('message')}", file=out, flush=True)
        elif t == "init":
            print(f"[copilot] {prov.product} {cli.version}, model {ev.get('model')}, session {ev.get('session_id')}"
                  + (f", tools NOT connected: {ev['tools_failed']}" if ev.get("tools_failed") else ""), file=out,
                  flush=True)
        elif t == "result":
            state["result"] = ev
            done.set()

    sess = prov.session(cli, s.root, bridge, on_event=on_event, on_exit=lambda code: done.set(), model=model,
                        resume=resume, distro=s.settings.wsl.default_distro, wsl_path=s.case().wsl_path)
    sess.start()
    sess.send(prompt)
    t0 = time.monotonic()
    finished = done.wait(timeout_min * 60)
    res = state["result"] or {}
    spend = (f"cost ${res['cost_usd']:.2f}" if res.get("cost_usd") is not None else
             f"tokens {res['usage']}" if res.get("usage") else "")
    print(f"\n[copilot] {'finished' if finished else 'TIMED OUT'} in {(time.monotonic() - t0) / 60:.1f} min; "
          f"turns {res.get('num_turns')}, {spend}, session {sess.session_id}"
          + (f"\n[copilot] stopped: {res.get('message')}" if res.get("is_error") else ""), file=out, flush=True)
    sess.stop()
    bridge.stop()
    if s.runs is not None:
        s.runs.shutdown()
    return 0 if finished and not res.get("is_error") else 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="aethercfd-cli copilot")
    ap.add_argument("case")
    ap.add_argument("prompt", help="what to ask; '-' reads it from stdin; '@file' reads it from a file")
    ap.add_argument("--approve", choices=["none", "edits", "all"], default="none")
    ap.add_argument("--allow-destructive", action="store_true")
    ap.add_argument("--provider", choices=list(PROVIDERS), help="AI provider (default: the copilot_provider setting)")
    ap.add_argument("--model")
    ap.add_argument("--resume")
    ap.add_argument("--timeout-min", type=float, default=120.0)
    a = ap.parse_args(argv)
    prompt = sys.stdin.read() if a.prompt == "-" else (Path(a.prompt[1:]).read_text(encoding="utf-8")
                                                        if a.prompt.startswith("@") else a.prompt)
    return run(a.case, prompt, approve=a.approve, allow_destructive=a.allow_destructive, model=a.model,
               timeout_min=a.timeout_min, resume=a.resume, provider=a.provider)


if __name__ == "__main__":
    sys.exit(main())
