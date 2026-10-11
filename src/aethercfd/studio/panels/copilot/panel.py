"""Copilot panel: talk to an AI agent about the open case.

The agent is a provider's CLI run headless with the AetherCFD tools (aethercfd.copilot): Claude Code by
default, or OpenAI Codex (provider and model picker in the top bar, kept in the settings). Its words stream
into the transcript; every tool call shows up as a line; anything that changes the case or runs OpenFOAM
opens the approval dialog with the exact plan. The conversation is per case and resumes after a restart.
"""

from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)
CREATE_NEW_CONSOLE = 0x00000010
QUICK = [("Diagnose", "Look at this case and tell me what state it is in: setup, mesh quality, the latest "
          "run and anything that looks wrong. Evidence first, then a ranked list of problems."),
         ("Mesh check", "Assess the mesh quality of this case from the latest checkMesh (run it if there is "
          "none). Where are the worst cells and what would you change in the mesh generation?"),
         ("Why did it fail?", "Read the latest solver log. Why did the run stop or diverge? Point to the "
          "first sign of trouble and propose the smallest fix.")]


# ============================================================================ tiny markdown -> HTML
def md_to_html(text: str) -> str:
    """Enough Markdown for Claude's answers: code fences, headings, lists, tables, bold/italic/code."""
    out: list[str] = []
    lines = text.replace("\r", "").split("\n")
    i, in_list = 0, None

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", lambda m: f"<code style='background:{T.alt};color:{T.text}'>{m.group(1)}</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", s)
        return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", rf"<a style='color:{T.accent}' href='\2'>\1</a>", s)

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            close_list()
            j = i + 1
            block = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                block.append(lines[j])
                j += 1
            out.append(f"<pre style='background:{T.base};color:{T.text};padding:6px'>"
                       f"{html.escape(chr(10).join(block))}</pre>")
            i = j + 1
            continue
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            close_list()
            rows = [ln]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(lines[j])
                j += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            t = ["<table cellspacing=0 cellpadding=3 style='border-collapse:collapse'><tr>"
                 + "".join(f"<th style='border-bottom:1px solid {T.border}' align=left>{inline(c)}</th>"
                           for c in cells[0]) + "</tr>"]
            for r in cells[1:]:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("".join(t) + "</table>")
            i = j
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            close_list()
            size = {1: "12pt", 2: "11pt", 3: "10pt", 4: "9.5pt"}[len(m.group(1))]
            out.append(f"<p style='font-size:{size};font-weight:600;margin-top:6px'>{inline(m.group(2))}</p>")
        elif re.match(r"^\s*[-*]\s+", ln):
            if in_list != "ul":
                close_list()
                out.append("<ul style='margin-top:0'>")
                in_list = "ul"
            out.append(f"<li>{inline(re.sub(r'^\s*[-*]\s+', '', ln))}</li>")
        elif re.match(r"^\s*\d+[.)]\s+", ln):
            if in_list != "ol":
                close_list()
                out.append("<ol style='margin-top:0'>")
                in_list = "ol"
            out.append(f"<li>{inline(re.sub(r'^\s*\d+[.)]\s+', '', ln))}</li>")
        elif not ln.strip():
            close_list()
        else:
            close_list()
            out.append(f"<p style='margin:2px 0'>{inline(ln)}</p>")
        i += 1
    close_list()
    return "".join(out)


# ============================================================================ GUI hooks for tools
class _UiHooks(QObject):
    _run = Signal(object, object)

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.sv = services
        self._run.connect(self._exec, Qt.ConnectionType.QueuedConnection)

    @Slot(object, object)
    def _exec(self, fn, slot):
        ev, box = slot
        try:
            box.append(fn())
        except Exception as exc:
            box.append(f"failed: {exc}")
        ev.set()

    def _call(self, fn) -> str:
        ev, box = threading.Event(), []
        self._run.emit(fn, (ev, box))
        ev.wait(30)
        return str(box[0]) if box else "the GUI did not answer"

    def show_field(self, field: str, time: str | None, slice_axis: str | None, regions=None,
                   smoothing: str | None = None) -> str:
        def go():
            host, spec = self.sv.host, self.sv.panels.get("aether.viewport")
            # the viewport the other viewport tools (regions, stages, showcase) act on: the active one
            panel = getattr(self.sv, "active_viewport", None) or next(iter(host.panels_of("aether.viewport")), None)
            if panel is None:
                d = host.open_panel(spec)
                panel = host.build_panel(d.objectName())
            if panel is None or not hasattr(panel, "show_field"):
                return "no viewport available"
            return panel.show_field(field, time, slice_axis, regions, smoothing)
        return self._call(go)

    def open_file(self, path: Path, line: int | None) -> str:
        def go():
            spec = self.sv.panels.get("aether.editor")
            d = self.sv.host.open_panel(spec, args={"path": str(path)})
            p = self.sv.host.build_panel(d.objectName())
            if line and p is not None:
                p.editor.goto_line(line)
            return f"opened {path.name}" + (f" at line {line}" if line else "")
        return self._call(go)

    # app automation (aethercfd.studio.app.automation), always on the GUI thread
    def _auto(self, method: str, *args):
        from aethercfd.studio.app.automation import AppAutomation

        ev, box = threading.Event(), []

        def go():
            return getattr(AppAutomation(self.sv), method)(*args)
        self._run.emit(go, (ev, box))
        ev.wait(120)
        return box[0] if box else "the GUI did not answer"

    def overview(self) -> dict:
        return self._auto("overview")

    def open_panel(self, name: str) -> str:
        return self._auto("open_panel", name)

    def open_case(self, path: str) -> str:
        out = self._auto("open_case", path)
        if isinstance(out, str) and out.startswith("opening "):
            # the copilot panel carries the chat over to the new case and continues the request there
            self.sv.copilot_handoff = {"path": path.strip().strip('"'), "t": time.monotonic(),
                                       "request": getattr(self.sv, "copilot_last_request", "")}
            return (out + "; this conversation then ends and the chat continues by itself in a new conversation for "
                    "that case with the user's request handed over: do not ask the user to repeat it")
        return out

    def show_diff(self, left: str, right: str, other_case: str, before_last_change: bool) -> str:
        return self._auto("show_diff", left, right, other_case, before_last_change)

    def python(self, code: str) -> str:
        return self._auto("python", code)

    def compare_fields(self, left: str, right: str, diff: str, time: str | None, slice_axis: str | None) -> str:
        return self._auto("compare_fields", left, right, diff, time, slice_axis)

    def viewport_look(self, look: str) -> str:
        return self._auto("viewport_look", look)

    def showcase(self, regions=None) -> str:
        return self._auto("showcase", regions)

    def viewport_regions(self, regions=None) -> dict:
        return self._auto("viewport_regions", regions)

    def animation(self, kind: str, frames: int, fps: int, fmt: str) -> str:
        return self._auto("animation", kind, frames, fps, fmt)

    def open_paraview(self) -> str:
        return self._auto("open_paraview")

    def perspective(self, name: str) -> str:
        return self._auto("perspective", name)

    def select_file(self, path: str) -> str:
        return self._auto("select_file", path)

    def add_stage(self, kind: str, params: dict, regions=None, smoothing: str | None = None) -> str:
        return self._auto("add_stage", kind, params, regions, smoothing)

    def viewport_smoothing(self, mode: str, edges: bool | None) -> str:
        return self._auto("viewport_smoothing", mode, edges)

    def section(self, *args) -> str:
        return self._auto("section", *args)

    def cutaway(self, axis: str) -> str:
        return self._auto("cutaway", axis)

    def bookmark(self, action: str, name: str):
        return self._auto("bookmark", action, name)

    def quad(self, on: bool) -> str:
        return self._auto("quad", on)

    def flow_paths(self, *args) -> str:
        return self._auto("flow_paths", *args)

    def line_probe(self, p1, p2, field: str | None, n: int) -> str:
        return self._auto("line_probe", p1, p2, field, n)

    def line_status(self):
        return self._auto("line_status")

    def stage_status(self, sid: str):
        return self._auto("stage_status", sid)

    def remove_stage(self, sid: str | None) -> str:
        return self._auto("remove_stage", sid)

    def camera(self, view: str, stage_id: str | None) -> str:
        return self._auto("camera", view, stage_id)

    def render(self, width: int, height: int, background: str, base: str, frame: bool, name: str | None) -> str:
        return self._auto("render", width, height, background, base, frame, name)

    def highlight(self, target: str, message: str, seconds: float) -> str:
        return self._auto("highlight", target, message, seconds)

    def actions(self, query: str) -> list:
        return self._auto("actions", query)

    def trigger(self, label: str) -> str:
        return self._auto("trigger", label)


# ============================================================================ panel
class _ChatInput(QPlainTextEdit):
    """Enter sends, Shift+Enter starts a new line (Ctrl+Enter also sends), like every chat box."""

    def __init__(self, on_send, **kw):
        super().__init__(**kw)
        self._on_send = on_send

    def keyPressEvent(self, e) -> None:  # Qt override
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._on_send()
            return
        super().keyPressEvent(e)


class CopilotPanel(Panel):
    _event = Signal(object)  # (session generation, event)
    _exited = Signal(object)  # (session generation, exit code)
    _tool_activity = Signal(str, object, object, object)
    _cli_ready = Signal(object)  # (provider id, cli, auth, models)

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        from aethercfd.copilot.providers import PROVIDERS, get_provider

        self.sv = ctx.services
        self.provider = get_provider(getattr(self.sv.settings, "copilot_provider", None))
        self.cli = None
        self.auth = None
        self.copilot = None  # a CopilotSessionBase of self.provider
        self.bridge = None
        self.toolset = None
        self.items: list[dict] = []
        self._cur: int | None = None  # streaming assistant item
        self._tool_items: dict[str, int] = {}
        self._busy = False
        self._t_start = 0.0
        self._gen = 0  # events of a stopped conversation (e.g. the case changed) are ignored
        self._conv_state: Path | None = None  # copilot.json of the case the running conversation belongs to
        self._last_user_text = ""
        self._conv_case: str | None = None
        self._conv_provider = self.provider  # the provider of the running conversation
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.status = QLabel("Copilot")
        self.status.setStyleSheet(f"color: {T.text_dim};")
        self.status.setMinimumWidth(40)
        self.provider_box = QComboBox(toolTip="AI provider of the copilot (each signs in with its own CLI)")
        for p in PROVIDERS.values():
            self.provider_box.addItem(p.label, p.id)
        self.provider_box.setCurrentIndex(max(0, self.provider_box.findData(self.provider.id)))
        self.model = QComboBox(toolTip="Model for new conversations (type any model id the provider accepts)")
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumContentsLength(13)
        self._fill_models(self.provider.default_models, getattr(self.sv.settings, "copilot_model", None))
        self.btn_new = QToolButton(icon=icon("msc.add"), toolTip="New conversation", autoRaise=True)
        self.btn_stop = QToolButton(icon=icon("msc.debug-stop"), toolTip="Stop the current answer",
                                    autoRaise=True)
        self.btn_stop.setEnabled(False)
        top.addWidget(self.status, 1)
        top.addWidget(self.provider_box)
        top.addWidget(self.model)
        top.addWidget(self.btn_new)
        top.addWidget(self.btn_stop)
        lay.addLayout(top)
        self.banner = QWidget()
        bl = QVBoxLayout(self.banner)  # stacked: the copilot usually lives in a narrow side dock
        bl.setContentsMargins(8, 6, 8, 6)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        self.btn_login = QPushButton(f"Sign in to {self.provider.label}")
        self.btn_recheck = QPushButton("Check again")
        bl.addWidget(self.banner_text)
        brow = QHBoxLayout()
        brow.addWidget(self.btn_login)
        brow.addWidget(self.btn_recheck)
        brow.addStretch(1)
        bl.addLayout(brow)
        self.banner.setStyleSheet(f"background: {T.alt}; border-radius: 4px;")
        self.banner.hide()
        lay.addWidget(self.banner)
        self.view = QTextBrowser(openExternalLinks=True)
        self.view.setStyleSheet(f"QTextBrowser {{ background: {T.panel}; border: none; }}")
        lay.addWidget(self.view, 1)
        quick = QHBoxLayout()
        quick.setSpacing(2)
        for label, prompt in QUICK:
            b = QToolButton(text=label, toolTip=prompt, autoRaise=True)
            b.setStyleSheet(f"QToolButton {{ color: {T.accent}; padding: 1px 4px; }}")
            b.clicked.connect(lambda _=False, p=prompt: self.send(p))
            quick.addWidget(b)
        quick.addStretch(1)
        lay.addLayout(quick)
        row = QHBoxLayout()
        self.input = _ChatInput(self._send_input,
                                placeholderText="Ask about this case… (Enter sends, Shift+Enter for a new line)")
        self.input.setMaximumHeight(96)
        self.btn_send = QPushButton(icon("msc.send"), "")
        self.btn_send.setToolTip("Send (Enter)")
        row.addWidget(self.input, 1)
        row.addWidget(self.btn_send)
        lay.addLayout(row)
        QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self._send_input)
        self.btn_send.clicked.connect(self._send_input)
        self.btn_new.clicked.connect(self.new_conversation)
        self.btn_stop.clicked.connect(self._interrupt)
        self.btn_login.clicked.connect(self._login)
        self.btn_recheck.clicked.connect(self._find_cli)
        self.provider_box.currentIndexChanged.connect(self._provider_picked)
        self.model.activated.connect(lambda _i: self._model_picked())
        self.model.lineEdit().editingFinished.connect(self._model_picked)
        self._event.connect(self._on_event)
        self._exited.connect(self._on_exit)
        self._tool_activity.connect(self._on_tool_activity)
        self._cli_ready.connect(self._on_cli)
        self._render_timer = QTimer(self, singleShot=True, interval=80, timeout=self._render)
        self._hooks = _UiHooks(self.sv, self)
        from aethercfd.studio.widgets.approval import GuiApprovalBroker

        self.broker = GuiApprovalBroker(lambda: self.window(), self)
        self.broker.on_decision = self._on_decision
        self.sv.case_changed.connect(lambda _s: self._case_changed())
        self._find_cli()
        self._render()

    # ------------------------------------------------------------------ provider CLI
    def _find_cli(self) -> None:
        prov = self.provider
        self.status.setText(f"Looking for {prov.product}…")

        def work():
            cli = prov.find(prov.configured_exe(self.sv.settings))
            auth = prov.auth_status(cli) if cli else None
            return prov.id, cli, auth, prov.models(cli)

        self.sv.run_bg(work, lambda res: self._cli_ready.emit(res[0] or (prov.id, None, None, prov.default_models)))

    @Slot(object)
    def _on_cli(self, found) -> None:
        pid, cli, auth, models = found
        if pid != self.provider.id:
            return  # the user switched provider while this one was being looked up
        self.cli, self.auth = cli, auth
        if models:
            self._fill_models(models, self._model_id())
        self._show_auth()

    def _show_auth(self) -> None:
        prov, cli, auth = self.provider, self.cli, self.auth
        self.btn_login.setText(f"Sign in to {prov.label}")
        if cli is None:
            self.banner_text.setText(prov.install_hint)
            self.btn_login.hide()
            self.banner.show()
            self.status.setText(f"Copilot unavailable ({prov.product} not found)")
            return
        if not auth or not auth.logged_in:
            if auth is not None and auth.method == "config-error":
                self.banner_text.setText(auth.detail)
            else:
                self.banner_text.setText(f"{prov.product} {cli.version} is not signed in. Sign in once (your browser "
                                         "opens); then press Check again.")
            self.btn_login.show()
            self.banner.show()
            self.status.setText("Copilot: not signed in")
            return
        self.banner.hide()
        self.status.setText(f"{prov.product} {cli.version} · {auth.detail or auth.method}")

    def _login(self) -> None:
        if self.cli is None:
            return
        subprocess.Popen(self.provider.login_argv(self.cli), creationflags=CREATE_NEW_CONSOLE)  # user sees prompts
        self.sv.notify(f"Complete the {self.provider.label} sign-in in your browser, then press Check again")

    # ------------------------------------------------------------------ provider / model picker
    def _fill_models(self, models, current: str | None) -> None:
        self.model.blockSignals(True)
        self.model.clear()
        for label, val in models:
            self.model.addItem(label, val)
        i = self.model.findData(current) if current else 0
        if i >= 0:
            self.model.setCurrentIndex(i)
        else:  # a model id typed earlier that the list does not have
            self.model.setEditText(current or "")
        self.model.blockSignals(False)

    def _model_id(self) -> str | None:
        """The model for new conversations: the picked entry, or whatever id was typed (None = default)."""
        text = self.model.currentText().strip()
        i = self.model.findText(text)
        if i >= 0:
            return self.model.itemData(i)
        return text or None

    def _save_choice(self) -> None:
        st = self.sv.settings
        st.copilot_provider, st.copilot_model = self.provider.id, self._model_id()
        try:
            st.save()
        except Exception:
            log.warning("could not save the copilot provider/model", exc_info=True)

    def _model_picked(self) -> None:
        if self._model_id() != getattr(self.sv.settings, "copilot_model", None):
            self._save_choice()

    def _provider_picked(self, _index: int = 0) -> None:
        from aethercfd.copilot.providers import get_provider

        prov = get_provider(self.provider_box.currentData())
        if prov.id == self.provider.id:
            return
        had_conversation = self.copilot is not None
        self._stop_session()
        self.provider, self.cli, self.auth = prov, None, None
        self._fill_models(prov.default_models, None)
        self._save_choice()
        if had_conversation or self.items:
            self._add("note", f"Switched to {prov.label}. Your next message starts a new conversation with it.")
        self._find_cli()

    def reload_provider(self) -> None:
        """The settings changed elsewhere (the setup wizard): follow their provider."""
        pid = getattr(self.sv.settings, "copilot_provider", None)
        i = self.provider_box.findData(pid)
        if i >= 0 and pid != self.provider.id:
            self.provider_box.setCurrentIndex(i)  # -> _provider_picked
        else:
            self._find_cli()

    # ------------------------------------------------------------------ session
    def _state_file(self) -> Path | None:
        s = self.sv.session
        if s is None or s.project is None:
            return None
        return Path(s.project.meta_dir) / "local" / "copilot.json"

    def _saved_session_id(self) -> str | None:
        """The conversation saved for THIS case with THIS provider (one for another case or provider is ignored;
        files from before providers existed are Claude's)."""
        f = self._state_file()
        s = self.sv.session
        try:
            d = json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}
        except (OSError, json.JSONDecodeError):
            return None
        same = s is not None and d.get("case") and Path(d["case"]).resolve() == Path(s.root).resolve()
        return d.get("session_id") if same and d.get("provider", "claude") == self.provider.id else None

    def _save_session_id(self, sid: str | None, state: Path | None = None, case: str | None = None,
                         provider: str | None = None) -> None:
        """Into the copilot.json of the case the conversation belongs to (``state``), never into a case that was
        opened while the reply was still arriving."""
        f = state or self._state_file()
        if f is None:
            return
        try:
            prev = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except (OSError, json.JSONDecodeError):
            prev = {}
        owner = case or prev.get("case") or (str(self.sv.session.root) if self.sv.session else None)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"session_id": sid, "case": owner, "provider": provider or self.provider.id,
                                 "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1), encoding="utf-8")

    def _ensure_session(self, *, fresh: bool = False) -> bool:
        if self.copilot is not None and self.copilot.alive:
            return True
        s = self.sv.session
        if s is None:
            self.sv.notify("Open a case first", "warn")
            return False
        prov = self.provider
        if self.cli is not None and not (self.auth and self.auth.logged_in):
            # signed in after the app started? the check at startup is stale: ask again now (about a second)
            self.auth = prov.auth_status(self.cli, timeout=8)  # on the GUI thread: keep it short
            self._show_auth()
        if self.cli is None or not (self.auth and self.auth.logged_in):
            self._find_cli()
            what = "was not found" if self.cli is None else "is not signed in yet"
            self._add("note", f"{prov.product} {what}: see the note above (or run `{prov.login_hint}`), then send "
                              "again. Your message is still in the box.")
            return False
        if not self.sv.ensure_project():
            return False
        from aethercfd.commands.executor import Executor
        from aethercfd.commands.registry import default_registry
        from aethercfd.copilot.bridge import BridgeServer
        from aethercfd.copilot.tools import ToolContext, ToolSet

        executor = Executor(default_registry(), s.context, self.broker)
        self.toolset = ToolSet(ToolContext(s, executor=executor, ui=self._hooks))
        if self.bridge is None:
            self.bridge = BridgeServer(lambda: self.toolset).start()
            self.bridge.on_call = lambda n, a, ok, t: self._tool_activity.emit(n, a, ok, t)
        resume = None if fresh else self._saved_session_id()
        self._gen += 1
        gen = self._gen
        self._conv_state = self._state_file()
        self._conv_case = str(s.root)
        self._conv_provider = prov
        self.copilot = prov.session(self.cli, s.root, self.bridge, on_event=lambda ev: self._event.emit((gen, ev)),
                                    on_exit=lambda code: self._exited.emit((gen, code)),
                                    model=self._model_id(), resume=resume,
                                    distro=s.settings.wsl.default_distro, wsl_path=s.case().wsl_path)
        self.copilot.start()
        if resume:
            self._add("note", "Resumed the previous conversation for this case.")
        return True

    def new_conversation(self) -> None:
        self._stop_session()
        self._save_session_id(None, case=str(self.sv.session.root) if self.sv.session else None)
        self.status.setText(self._idle_status())
        self.items.clear()
        self._cur = None
        self._render()

    def _stop_session(self) -> None:
        self._gen += 1  # whatever the stopped conversation still sends is dropped
        if self.copilot is not None:
            self.copilot.stop()
            self.copilot = None
        self._set_busy(False)

    def _idle_status(self) -> str:
        a, c = self.auth, self.cli
        if a is not None and a.logged_in and c is not None:
            return f"{self.provider.product} {getattr(c, 'version', '')} · {a.detail or a.method}"
        return self.status.text()

    def _case_changed(self) -> None:
        handoff = getattr(self.sv, "copilot_handoff", None)
        self.sv.copilot_handoff = None
        s = self.sv.session
        self._stop_session()
        self._cur = None
        self._tool_items.clear()
        self.status.setText(self._idle_status())
        if handoff and s is not None and time.monotonic() - handoff["t"] < 60 \
                and Path(handoff["path"]).resolve() == Path(s.root).resolve():
            # the copilot opened this case itself: keep the chat on screen and carry on here (a conversation
            # belongs to one case, so a fresh one starts for this case with the request handed over)
            self._add("note", f"Opened {Path(s.root).name}. Continuing in a new conversation for this case.")
            request, name = handoff.get("request") or "", Path(s.root).name
            QTimer.singleShot(300, lambda: self._continue_after_open(request, name))
        else:
            self.items.clear()
        self._render()

    def _continue_after_open(self, request: str, case_name: str) -> None:
        msg = (f"(Hand-over from the previous case.) At my request you opened the case {case_name}; it is now the "
               f"open case and your tools act on it. My request was: '{request}'. Do whatever part of it is not "
               "done yet. If opening the case was all I asked, reply in two or three sentences with what this case "
               "is (solver, regions, cells, saved times) and suggest what I could ask next.")
        self.send(msg, show_as="note", show_text="Handing your request over to the new conversation…",
                  fresh=True)

    # ------------------------------------------------------------------ talking
    def _send_input(self) -> None:
        text = self.input.toPlainText().strip()
        if text and self.send(text):
            self.input.clear()  # only once Claude has it: a failed start keeps your message in the box

    def send(self, text: str, *, show_as: str = "user", show_text: str | None = None, fresh: bool = False) -> bool:
        if self._busy:
            self.sv.notify(f"{self._conv_provider.label} is still answering; wait or press Stop", "warn")
            return False
        try:
            ready = self._ensure_session(fresh=fresh)
        except Exception as exc:
            log.exception("copilot session failed to start")
            self.copilot = None
            self._add("note", f"{self.provider.label} could not start ({type(exc).__name__}: {exc}). Your message is "
                              "still in the box; details are in studio.log.")
            return False
        if not ready:
            return False
        try:
            self.copilot.send(text)
        except Exception as exc:
            log.exception("copilot send failed")
            self._add("note", f"{self.provider.label} did not take the message ({exc}). It is still in the box.")
            return False
        self._add(show_as, show_text if show_text is not None else text)
        if show_as == "user":
            self._last_user_text = text
            self.sv.copilot_last_request = text  # the hand-over after app_open_case passes it on
        self._set_busy(True)
        return True

    def _interrupt(self) -> None:
        if self.copilot is not None and self.copilot.alive:
            try:
                self.copilot.interrupt()
            except Exception:
                self.copilot.stop()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_stop.setEnabled(busy)
        self.btn_send.setEnabled(not busy)
        if busy:
            self._t_start = time.monotonic()

    # ------------------------------------------------------------------ events
    def _add(self, role: str, text: str, **kw) -> int:
        self.items.append({"role": role, "text": text, **kw})
        self._render_timer.start()
        return len(self.items) - 1

    @Slot(object)
    def _on_event(self, item) -> None:
        gen, ev = item
        if gen != self._gen:
            return  # from a conversation that was stopped (new conversation, or the case changed)
        who = self._conv_provider.label
        t = ev.get("type")  # provider-neutral events (aethercfd.copilot.provider)
        if t == "turn_start":
            self._cur = self._add("assistant", "", who=who)
        elif t == "text_delta":
            if self._cur is None:
                self._cur = self._add("assistant", "", who=who)
            self.items[self._cur]["text"] += ev.get("text", "")
            self._render_timer.start()
        elif t == "message":
            text = ev.get("text", "")
            if self._cur is None:
                self._cur = self._add("assistant", text, who=who)
            elif text:
                self.items[self._cur]["text"] = text
                self._render_timer.start()
            self._cur = None
        elif t == "tool_call":
            self._cur = None
            self._tool_items[ev.get("id", "")] = self._add("tool", _tool_summary(ev.get("name", ""),
                                                                                 ev.get("input") or {}), status="…")
        elif t == "tool_result":
            idx = self._tool_items.pop(ev.get("id", ""), None)
            if idx is not None:
                self.items[idx]["status"] = "error" if ev.get("is_error") else "ok"
                self._render_timer.start()
        elif t == "error":
            if ev.get("message"):
                self._add("note", f"{who}: {ev['message']}")
        elif t == "result":
            self._set_busy(False)
            self._cur = None
            if ev.get("session_id"):
                self._save_session_id(ev["session_id"], self._conv_state, self._conv_case, self._conv_provider.id)
            secs = time.monotonic() - self._t_start
            bits = [who, f"{ev.get('num_turns') or '?'} turns", f"{secs:.0f} s"]
            if ev.get("cost_usd") is not None:
                bits.append(f"est. ${ev['cost_usd']:.2f}")
            elif ev.get("usage"):
                u = ev["usage"]
                bits.append(f"{_tokens(u.get('input_tokens', 0))} in / {_tokens(u.get('output_tokens', 0))} out tokens")
            self.status.setText(" · ".join(bits))
            if ev.get("is_error"):
                self._add("note", f"{who} stopped: {ev.get('message') or 'error'}")
        elif t == "init":
            self.status.setText(f"{who} · {ev.get('model', '')}")
            bad = ev.get("tools_failed") or []
            if bad:
                self._add("note", f"AetherCFD tools did not connect ({', '.join(bad)}); see the copilot log.")

    @Slot(object)
    def _on_exit(self, item) -> None:
        gen, code = item
        if gen != self._gen:
            return
        self._set_busy(False)
        if code not in (0, None):
            self._add("note", f"{self._conv_provider.label} exited (code {code}). Your next message starts it again.")
        self.copilot = None

    @Slot(str, object, object, object)
    def _on_tool_activity(self, name, args, ok, text) -> None:
        if ok is False and text and text.startswith("not approved"):
            pass  # shown by the approval note

    def _on_decision(self, plan, approval) -> None:
        verdict = "approved" if approval.approved else "declined"
        note = f" (note: {approval.note})" if approval.note else ""
        self._add("approval", f"You {verdict}: {plan.summary}{note}", approved=approval.approved)

    # ------------------------------------------------------------------ render
    def _render(self) -> None:
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        parts = [f"<div style='color:{T.text};font-size:9.5pt'>"]
        if not self.items:
            parts.append(f"<p style='color:{T.text_faint}'>Ask {self.provider.label} about the open case. It can "
                         "read every file "
                         "and log, check the mesh, and propose changes or runs. Each change or run is shown to "
                         "you as an exact plan to approve, and lands in the Command Journal.</p>")
        for it in self.items:
            role = it["role"]
            if role == "user":
                parts.append(f"<p style='margin-top:10px;color:{T.accent};font-weight:600'>You</p>"
                             f"<div style='margin-left:8px'>{md_to_html(it['text'])}</div>")
            elif role == "assistant":
                if it["text"].strip():
                    parts.append(f"<p style='margin-top:8px;color:{T.ok};font-weight:600'>"
                                 f"{html.escape(it.get('who', 'Claude'))}</p>"
                                 f"<div style='margin-left:8px'>{md_to_html(it['text'])}</div>")
            elif role == "tool":
                st = it.get("status")
                color = {"ok": T.text_faint, "error": T.warn}.get(st, T.info)
                mark = {"ok": "done", "error": "failed"}.get(st, "running…")
                parts.append(f"<p style='margin:1px 0 1px 8px;color:{color};font-family:Consolas;font-size:8.5pt'>"
                             f"→ {html.escape(it['text'])} · {mark}</p>")
            elif role == "approval":
                color = T.ok if it.get("approved") else T.warn
                parts.append(f"<p style='margin:2px 0 2px 8px;color:{color}'>{html.escape(it['text'])}</p>")
            else:
                parts.append(f"<p style='color:{T.text_dim};font-style:italic'>{html.escape(it['text'])}</p>")
        parts.append("</div>")
        self.view.setHtml("".join(parts))
        if at_bottom:
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------------ panel API
    def save_state(self) -> dict:
        return {}  # provider and model live in the settings (copilot_provider / copilot_model)

    def restore_state(self, state: dict) -> None:
        pass  # layouts saved before providers existed carry a model index: the settings win

    def dispose(self) -> None:
        self._stop_session()
        if self.bridge is not None:
            self.bridge.stop()


def _tool_summary(name: str, args: dict) -> str:
    keys = ("file", "path", "key", "value", "log", "field", "run_id", "action")
    bits = [f"{k}={str(args[k])[:60]}" for k in keys if k in args]
    if name == "run_openfoam":
        bits = [" → ".join(" ".join([s.get("app", ""), *s.get("args", [])]) for s in args.get("steps", []))]
    return f"{name}({', '.join(bits)})"


def _tokens(n) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


PANELS = [PanelSpec("aether.copilot", "Copilot", "mdi6.robot-outline", "Assist", CopilotPanel,
                    default_area=Area.RIGHT, tab_group="assist",
                    description="Claude or Codex, working on the open case with your approval")]
