"""Regenerate the README screenshots from the running app.

    PYTHONPATH=src python scripts/make_screenshots.py            # every image
    PYTHONPATH=src python scripts/make_screenshots.py --only 01,05
    PYTHONPATH=src python scripts/make_screenshots.py --pinn     # also train one short PINN (about 1 min on a GPU)
    PYTHONPATH=src python scripts/make_screenshots.py --pinn cpu # the same on the CPU (when the GPU is busy)

The script starts AetherCFD Studio in this process (the same QApplication, AppServices and MainWindow as
``python -m aethercfd.studio``), opens copies of a few small solved OpenFOAM cases, drives the app to each
state through its own panels and automation calls, and saves PNGs into docs/screenshots/.

Isolation:
- AETHERCFD_CONFIG_DIR and AETHERCFD_CACHE_DIR point into build/screens/, with a settings.toml written
  here, so the owner's settings, recent cases and layouts are never read or written.
- The cases are copied into build/screens/cases/ first. In the copies, the original folder in the logs and
  run records is replaced by the copy's folder and the log header's host name by "workstation", so no
  image shows a path outside build/.
- Nothing runs OpenFOAM and nothing is sent to an AI service. The copilot's sign-in status is not asked
  for: the status line says so.

Sources of the cases (read only): AETHER_SCREEN_CASES, default build/toys/cases (where
scripts/toy_campaign.py writes them; SOURCES names the solved folders of one campaign, so edit it to
match yours). AETHER_SCREEN_DISTRO names the WSL distro written into the isolated settings.

Known problem (2026-11-15): on the CPU the PINN worker is killed after about 30 to 45 s and the tab reports
"stopped without a result", so the committed 10-analysis-pinn.png shows the tab before training. On the GPU a
1500-step run finishes in about 20 s. A finished image is a run with --pinn and a free GPU.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
WORK = ROOT / "build" / "screens"
CASES = WORK / "cases"
TOYS = Path(os.environ.get("AETHER_SCREEN_CASES") or ROOT / "build" / "toys" / "cases")
SOURCES = {  # short name -> solved toy case
    "pitzDaily": "esi_pitzDaily_1010_072737",
    "forwardStep": "esi_forwardStep_1010_072835",
    "multiRegionHeater": "esi_multiRegionHeater_1010_082823",
    "counterFlowFlame2D": "esi_counterFlowFlame2D_1010_073109",
    "buoyantCavity": "esi_buoyantCavity_1010_072932",
}
WIDTH, HEIGHT = 1600, 1000
MAX_BYTES = 500_000
PINN_EPOCHS = {"gpu": 1500, "cpu": 600}  # one short training: about a minute on a laptop GPU, a few on a CPU
TEXT_SUFFIXES = {"", ".json", ".jsonl", ".sh", ".out", ".txt", ".toml", ".log"}


# ============================================================================ preparation (no Qt yet)
def _isolate() -> None:
    """Own config and cache folders under build/screens, with a minimal settings.toml."""
    cfg, cache = WORK / "config", WORK / "cache"
    for d in (cfg, cache):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    os.environ["AETHERCFD_CONFIG_DIR"] = str(cfg)
    os.environ["AETHERCFD_CACHE_DIR"] = str(cache)
    distro = os.environ.get("AETHER_SCREEN_DISTRO", "Ubuntu-22.04")
    ml = os.environ.get("AETHERCFD_ML_PYTHON", "")
    lines = [
        'theme = "dark"',
        'viewport_look = "studio"',
        "setup_done = true",
        "use_extra_screens = false",
        f"case_roots = [{json.dumps(str(CASES))}]",
    ]
    if ml:
        lines.append(f"ml_python = {json.dumps(ml)}")
    lines += ["", "[wsl]", f"allowed_distros = [{json.dumps(distro)}]", f"default_distro = {json.dumps(distro)}"]
    (cfg / "settings.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # the per-distro OpenFOAM fork list, so the app does not probe WSL again (a copy; only version names)
    from platformdirs import user_cache_dir

    real_forks = Path(user_cache_dir("AetherCFD", appauthor=False)) / "forks" / f"{distro}.json"
    if real_forks.is_file():
        (cache / "forks").mkdir(parents=True, exist_ok=True)
        shutil.copy2(real_forks, cache / "forks" / real_forks.name)


def _wsl(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    return f"/mnt/{s[0].lower()}{s[2:]}" if re.match(r"^[A-Za-z]:", s) else s


def _lx_link_target(path: Path) -> str | None:
    """Target of a WSL symlink (reparse tag IO_REPARSE_TAG_LX_SYMLINK), which Windows cannot open. OpenFOAM
    tutorials copied inside WSL keep them (e.g. multiRegionHeater's ``../heater/radiationProperties``)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    open_reparse_point, backup_semantics, fsctl_get_reparse_point = 0x00200000, 0x02000000, 0x000900A8
    h = k32.CreateFileW(str(path), 0, 7, None, 3, open_reparse_point | backup_semantics, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        buf = ctypes.create_string_buffer(16 * 1024)
        n = wintypes.DWORD()
        if not k32.DeviceIoControl(h, fsctl_get_reparse_point, None, 0, buf, len(buf), ctypes.byref(n), None):
            return None
        raw = buf.raw[:n.value]
        tag, length = int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:6], "little")
        if tag != 0xA000001D:
            return None
        return raw[8 + 4:8 + length].decode("utf-8")  # 4-byte version, then the UTF-8 target
    finally:
        k32.CloseHandle(h)


def _copy_tree(src: Path, dst: Path) -> list[str]:
    """Copy a case folder with plain files; a WSL symlink is replaced by a copy of what it points to."""
    unresolved = []
    for dirpath, _dirs, files in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            s, d = Path(dirpath) / f, dst / rel / f
            try:
                shutil.copyfile(s, d)
                continue
            except OSError:
                pass
            target = _lx_link_target(s)
            real = (s.parent / target) if target else None
            try:
                if real is None:
                    raise OSError("not a WSL symlink")
                shutil.copyfile(real, d)
            except OSError:
                unresolved.append(str(rel / f))
    return unresolved


def _copy_cases() -> dict[str, Path]:
    """Fresh copies of the toy cases, relocated: paths of the original folder become the copy's."""
    CASES.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, src_name in SOURCES.items():
        src, dst = TOYS / src_name, CASES / name
        if not src.is_dir():
            print(f"missing case {src}: its images are skipped", flush=True)
            continue
        if dst.exists():
            shutil.rmtree(dst)
        missed = _copy_tree(src, dst)
        if missed:
            print(f"{name}: could not copy {', '.join(missed)}", flush=True)
        old = [(_wsl(src), _wsl(dst)), (str(src), str(dst)), (str(src).replace("\\", "/"), str(dst).replace("\\", "/")),
               (str(src).replace("\\", "\\\\"), str(dst).replace("\\", "\\\\"))]
        for f in dst.rglob("*"):
            if not f.is_file() or (f.suffix not in TEXT_SUFFIXES and not f.name.startswith("log.")):
                continue
            if "objects" in f.parts or f.stat().st_size > 20_000_000:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            new = text
            for a, b in old:
                new = new.replace(a, b)
            new = re.sub(r"^(Host\s*:\s*)\S+", r"\1workstation", new, flags=re.M)
            new = new.replace(src_name, name)
            if new != text:
                f.write_text(new, encoding="utf-8", newline="")
        out[name] = dst
    return out


# ============================================================================ the app
class Shooter:
    def __init__(self, cases: dict[str, Path], only: set[str] | None, pinn: str):
        self.cases, self.only, self.pinn = cases, only, pinn
        self.done: list[dict] = []
        self.errors: list[str] = []
        self._start_app()

    # ------------------------------------------------------------------ start
    def _start_app(self) -> None:
        from aethercfd.copilot import claude_cli, codex_cli
        from aethercfd.copilot.provider import AuthStatus

        def not_asked(cli, timeout: float = 30) -> AuthStatus:  # never shows an account in an image
            return AuthStatus(True, claude_cli.SUBSCRIPTION_METHOD, "sign-in not checked for screenshots")

        claude_cli.auth_status = not_asked
        codex_cli.auth_status = not_asked

        from aethercfd.studio.__main__ import _install_excepthook, _setup_logging

        _setup_logging()
        from aethercfd.studio.app.bootstrap import bootstrap

        bootstrap()
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
        from PySide6.QtWidgets import QApplication

        self.qt_warnings: list[str] = []

        def on_qt(mode, ctx, msg):
            if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                self.qt_warnings.append(msg)

        qInstallMessageHandler(on_qt)
        self.app = QApplication(sys.argv[:1])
        self.app.setApplicationName("AetherCFD Studio")
        self.app.setOrganizationName("AetherCFD")
        from aethercfd.studio.app.services import AppServices
        from aethercfd.studio.panels import register_builtin_panels
        from aethercfd.studio.shell.main_window import MainWindow
        from aethercfd.studio.theme import style

        style.apply(self.app)
        sv = AppServices()
        sv.theme.set_mode(sv.settings.theme)
        _install_excepthook(sv)
        prev = sys.excepthook

        def hook(t, v, tb):
            self.errors.append("".join(traceback.format_exception(t, v, tb))[-3000:])
            prev(t, v, tb)

        sys.excepthook = hook
        self.notes: list[str] = []
        sv.notification.connect(lambda msg, level: self.notes.append(f"[{level}] {msg}"))
        register_builtin_panels(sv.panels)
        self.sv = sv
        self.win = MainWindow(sv)
        self.win.resize(WIDTH, HEIGHT)
        self.win.show()
        self.win.move(20, 20)
        self.win.layouts.restore("setup")
        self.pump(2.0)

    # ------------------------------------------------------------------ helpers
    def pump(self, seconds: float) -> None:
        from aethercfd.studio.smoke import _pump

        _pump(self.app, seconds)

    def wait(self, cond, timeout: float = 120.0, what: str = "") -> bool:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            self.pump(0.2)
            try:
                if cond():
                    return True
            except Exception:
                pass
        print(f"  timed out after {timeout:.0f} s waiting for {what or cond}", flush=True)
        return False

    @staticmethod
    def latest(vp) -> str | None:
        """The latest saved time the viewport offers."""
        times = [vp.time.itemText(i) for i in range(vp.time.count())]
        return max(times, key=float) if times else None

    def open_case(self, name: str) -> Path:
        root = self.cases[name]
        s = self.sv.session
        if s is None or Path(s.root).resolve() != root.resolve():
            self.sv.open_case(str(root))
            self.wait(lambda: self.sv.session is not None and Path(self.sv.session.root).resolve() == root.resolve(),
                      120, "the case to open")
            self.pump(2.0)
        return root

    def panel(self, type_id: str, instance: str | None = None):
        spec = self.sv.panels.get(type_id)
        d = self.sv.host.open_panel(spec, instance=instance)
        p = self.sv.host.build_panel(d.objectName())
        self.front(p)
        return p

    def front(self, panel) -> None:
        """Bring a panel's dock tab to the front."""
        with contextlib.suppress(Exception):
            dock = next((d for n, d in self.sv.host.docks().items() if self.sv.host.panel(n) is panel), None)
            if dock is not None:
                dock.toggleView(True)
                dock.setAsCurrentTab()
                dock.raise_()
        self.pump(0.4)

    def viewport(self):
        vp = next(iter(v for v in self.sv.host.panels_of("aether.viewport") if v.isVisible()), None)
        if vp is None:
            vp = self.panel("aether.viewport")
        self.sv.set_active_viewport(vp)
        self.front(vp)
        self.wait(lambda: vp.field.count() > 0 and vp.range is not None, 300, "viewport results")
        return vp

    def drawn(self, vp, timeout: float = 400) -> bool:
        ok = self.wait(lambda: getattr(vp, "drawn_gen", -1) == vp.gen and not getattr(vp, "_showcase_pending", 0),
                       timeout, "the viewport to draw")
        self.pump(1.0)
        vp.plotter.render()
        self.pump(0.3)
        return ok

    def fit(self, vp) -> None:
        """Camera fitted to the current viewport size (a camera kept from a wider or narrower viewport can
        leave the domain under the colour bar)."""
        self.auto().camera("fit")
        self.drawn(vp)

    def arrange(self, *places: tuple[str, str], widths: tuple[float, float] = (0.2, 0.27),
                persp: str = "post") -> dict:
        """A clean window for one image: the perspective ``persp`` is selected (its tab is highlighted), every
        panel is closed, then ``places`` are opened in order. ``where`` is left / right / bottom (a new column
        or row), center, tab (a tab beside the previous one) or below (split under the previous one).
        Returns {type id: panel}."""
        import PySide6QtAds as ads

        from aethercfd.studio.shell.panel_base import Area

        self.win.layouts.restore(persp)
        self.pump(0.5)
        host, m = self.sv.host, self.sv.host.manager
        for name in list(host.docks()):
            d = host.dock(name)
            if d is None or name == "aether.workbench#main":
                continue
            if d.isAutoHide():
                d.setAutoHide(False)
            if not d.isClosed():
                d.toggleView(False)
        self.pump(0.3)
        sides = {"left": ads.LeftDockWidgetArea, "right": ads.RightDockWidgetArea, "bottom": ads.BottomDockWidgetArea}
        prev, built = None, {}
        for type_id, where in places:
            spec = self.sv.panels.get(type_id)
            d = host.open_panel(spec, area=Area.CENTER if where == "center" else Area.RIGHT, focus=False,
                                instance="main")
            if d.isAutoHide():
                d.setAutoHide(False)
            if where == "center":
                m.addDockWidgetTabToArea(d, host._central.dockAreaWidget())
            elif where in sides:
                m.addDockWidget(sides[where], d)
            elif where == "tab":
                m.addDockWidgetTabToArea(d, prev.dockAreaWidget())
            elif where == "below":
                m.addDockWidget(ads.BottomDockWidgetArea, d, prev.dockAreaWidget())
            d.toggleView(True)
            prev = d
            built[type_id] = host.build_panel(d.objectName())
        self.pump(0.5)
        host.fix_central_current()
        for _ in range(2):  # once more after QtAds has laid the areas out
            host.balance(*widths)
            self.pump(0.8)
        return built

    def split_height(self, upper, lower, frac: float) -> None:
        """Give the upper of two stacked panels ``frac`` of their height."""
        with contextlib.suppress(Exception):
            a = self._dock_of(upper).dockAreaWidget()
            sp = a.parentSplitter()
            tot = sum(sp.sizes())
            sp.setSizes([int(tot * frac), tot - int(tot * frac)])
        self.pump(0.5)

    def _dock_of(self, panel):
        return next(d for n, d in self.sv.host.docks().items() if self.sv.host.panel(n) is panel)

    def paste(self, base, top, at: tuple[int, int] | None = None):
        """``top`` (a dialog grab) drawn over ``base`` (a window grab), centred unless ``at`` is given, with a
        thin border: how the dialog looks over the window."""
        from PySide6.QtGui import QColor, QPainter, QPen

        img = base.copy()
        img.setDevicePixelRatio(1.0)
        top = top.copy()
        top.setDevicePixelRatio(1.0)
        x, y = at or ((img.width() - top.width()) // 2, (img.height() - top.height()) // 2)
        p = QPainter(img)
        p.fillRect(x + 6, y + 6, top.width(), top.height(), QColor(0, 0, 0, 90))
        p.drawImage(x, y, top)
        p.setPen(QPen(QColor(120, 120, 120), 1))
        p.drawRect(x, y, top.width() - 1, top.height() - 1)
        p.end()
        return img

    # ------------------------------------------------------------------ images
    def grab(self, widget=None):
        """The window (or a dialog) as a QImage, with every visible 3D viewport drawn from its own render:
        a plain widget grab can miss OpenGL content."""
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QImage, QPainter

        widget = widget or self.win
        self.pump(0.3)
        img = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
        dpr = img.devicePixelRatio()
        if widget is self.win:
            painter = QPainter(img)
            for vp in self.sv.host.panels_of("aether.viewport"):
                pl = getattr(vp, "plotter", None)
                if pl is None or not pl.isVisible() or pl.window() is not self.win:
                    continue
                try:
                    import numpy as np

                    rw = pl.render_window
                    if os.environ.get("AETHER_SCREEN_DEBUG"):
                        print(f"  GL: current={rw.IsCurrent()} mapped={rw.GetMapped()} hwnd={int(pl.winId())} "
                              f"vtk_hwnd={rw.GetGenericWindowId()} size={rw.GetSize()}", flush=True)
                    rw.MakeCurrent()  # another GL widget may hold the current context
                    pl.render()
                    arr = pl.screenshot(return_img=True)
                    h, w = arr.shape[:2]
                    arr = np.ascontiguousarray(arr[:, :, :3])
                    if float(arr.std()) < 2.0:  # one flat colour: the render did not reach the image
                        raise RuntimeError(f"the viewport render is blank ({w}x{h})")
                    shot = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
                    top = pl.mapTo(self.win, QPoint(0, 0))
                    target = QRect(round(top.x() * dpr), round(top.y() * dpr), round(pl.width() * dpr),
                                   round(pl.height() * dpr))
                    img.setDevicePixelRatio(1.0)
                    painter.drawImage(target, shot)
                except Exception as exc:  # the image is still saved; the run is marked failed
                    self.errors.append(f"viewport image: {type(exc).__name__}: {exc}")
                    print(f"  viewport image failed: {type(exc).__name__}: {exc}", flush=True)
            painter.end()
        return img

    def save(self, name: str, img) -> Path:
        """PNG at most WIDTH wide and MAX_BYTES big (palette-quantized when needed)."""
        from io import BytesIO

        from PIL import Image
        from PySide6.QtCore import QBuffer, QIODevice

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        im = Image.open(BytesIO(bytes(buf.data()))).convert("RGB")
        if im.width > WIDTH:
            im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.Resampling.LANCZOS)
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{name}.png"
        for colors in (None, 256, 192, 128):
            data = BytesIO()
            (im if colors is None else im.quantize(colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
             ).save(data, "PNG", optimize=True)
            if data.tell() <= MAX_BYTES or colors == 128:
                path.write_bytes(data.getvalue())
                break
        print(f"  saved {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB, {im.width}x{im.height})", flush=True)
        return path

    # ------------------------------------------------------------------ run every shot
    def run(self) -> int:
        shots = [(n, getattr(self, n)) for n in sorted(dir(self)) if re.match(r"^s\d\d_", n)]
        for name, fn in shots:
            key = name[1:3]
            if self.only and key not in self.only:
                continue
            print(f"[{key}] {fn.__doc__.splitlines()[0] if fn.__doc__ else name}", flush=True)
            e0, n0 = len(self.errors), len(self.notes)
            t0 = time.perf_counter()
            rec = {"shot": name}
            try:
                rec["files"] = [str(p.name) for p in (fn() or [])]
            except Exception:
                rec["error"] = traceback.format_exc()[-2500:]
                print(rec["error"], flush=True)
            rec["seconds"] = round(time.perf_counter() - t0, 1)
            if len(self.errors) > e0:
                rec["slot_exceptions"] = self.errors[e0:]
            if len(self.notes) > n0:
                rec["notifications"] = self.notes[n0:]
            self.done.append(rec)
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "screens.json").write_text(json.dumps({"shots": self.done, "qt_warnings": self.qt_warnings[-50:]},
                                                      indent=1), encoding="utf-8")
        bad = [r for r in self.done if "error" in r or "slot_exceptions" in r]
        print(f"{len(self.done) - len(bad)}/{len(self.done)} shots clean; report in "
              f"{(WORK / 'screens.json').relative_to(ROOT)}", flush=True)
        return 1 if bad else 0

    # ============================================================================ the shots
    def auto(self):
        from aethercfd.studio.app.automation import AppAutomation

        return AppAutomation(self.sv)

    def show_stage(self, vis, vp) -> None:
        """The Visualization panel shows the viewport's first stage and its settings."""
        vis._retarget(vp)
        if vis.list.count():
            vis.list.setCurrentRow(0)
        self.pump(0.8)

    def s01_main_window(self):
        """Main window on pitzDaily: case tree, guided workflow, Studio viewport with |U|."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), ("aether.workflow", "right"),
                         widths=(0.15, 0.25), persp="setup")
        wf = p["aether.workflow"]
        wf.refresh_now()
        vp = self.viewport()
        vp.set_look("studio")
        vp.show_field("U", "60")
        self.drawn(vp)
        self.fit(vp)
        self.wait(lambda: wf.stages, 120, "the workflow stages")
        self.pump(1.0)
        return [self.save("01-main-window", self.grab())]

    def s02_showcase(self):
        """Showcase on the five-region heater: mid-plane slice in a glass shell."""
        self.open_case("multiRegionHeater")
        self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), widths=(0.17, 0.0))
        vp = self.viewport()
        vp.show_field("T", self.latest(vp))
        self.drawn(vp)
        print("  " + self.auto().showcase())
        self.drawn(vp)
        return [self.save("02-showcase-heater", self.grab())]

    def s03_regions(self):
        """Multi-region: temperature in the three solid regions only."""
        self.open_case("multiRegionHeater")
        self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), widths=(0.17, 0.0))
        vp = self.viewport()
        vp.show_field("T", self.latest(vp))
        self.drawn(vp)
        # Showcase("solids") slices the solids' combined box at mid-height, which on this case cuts only the
        # heater (the two plates are one cell thick, above that height): show the solid surfaces instead
        print("  " + str(self.auto().viewport_regions("solids")["result"]))
        self.wait(lambda: vp.region_sel is not None, 60, "the solids to be selected")
        vp.slice_on.setChecked(False)
        self.drawn(vp)
        self.auto().camera("front")
        self.drawn(vp)
        return [self.save("03-regions-solids", self.grab())]

    def s04_flow_paths(self):
        """Flow paths from the inlet of pitzDaily over the velocity field."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), ("aether.vis_controls", "right"),
                         widths=(0.15, 0.22))
        vp = self.viewport()
        vp.set_look("studio")
        vp.show_field("U", "60")
        self.drawn(vp)
        self.fit(vp)
        print("  " + str(self.auto().flow_paths("inlets" if vp.inlet_paths() else "plane")))
        self.drawn(vp, 600)
        self.show_stage(p["aether.vis_controls"], vp)
        out = [self.save("04-flow-paths", self.grab())]
        self.auto().flow_paths(clear=True)
        self.drawn(vp)
        return out

    def s05_schlieren(self):
        """Schlieren (log scale, 99th percentile) on the supersonic forward step."""
        self.open_case("forwardStep")
        p = self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), ("aether.vis_controls", "right"),
                         widths=(0.15, 0.22))
        vis = p["aether.vis_controls"]
        vp = self.viewport()
        vp.set_look("studio")
        vp.show_field("rho", "0.4")
        self.drawn(vp)
        sid = self.auto().add_stage("schlieren", {"source": "rho", "scale": "log", "percentile": 99, "k": 12.0,
                                                  "axis": "z", "pos": 0.5})
        self.wait(lambda: f"stage:{sid}" in vp.stage_info, 400, "the schlieren stage")
        self.drawn(vp)
        print("  " + str(vp.stage_info.get(f"stage:{sid}", {}).get("label")))
        self.show_stage(vis, vp)
        vis.base.setCurrentText("hidden")  # the stage alone
        self.pump(0.5)
        self.drawn(vp)
        self.auto().camera("fit")  # hiding the base mesh leaves the camera zoomed past the domain's ends
        self.drawn(vp)
        out = [self.save("05-schlieren-forward-step", self.grab())]
        vis.base.setCurrentText("Auto")
        self.pump(0.6)  # the panel pushes view options after 180 ms
        vp.set_view_options({})
        self.auto().remove_stage(sid)
        self.drawn(vp)
        return out

    def s06_run_monitor(self):
        """Run Monitor: run status and history, residuals and the solver log of pitzDaily."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.case_tree", "left"), ("aether.residuals", "center"), ("aether.run_status", "right"),
                         ("aether.solver_log", "bottom"), widths=(0.14, 0.3), persp="run")
        res, log = p["aether.residuals"], p["aether.solver_log"]
        self.sv.set_current_log(None)
        self.sv.set_current_log(self.sv.newest_log(self.sv.session.root))
        self.wait(lambda: len(res.x_iter) > 10 and log.view.document().blockCount() > 50, 120, "residuals and log")
        p["aether.run_status"]._refresh()
        self.split_height(res, log, 0.7)
        self.pump(1.5)
        return [self.save("06-run-monitor", self.grab())]

    def s07_workflow(self):
        """Guided workflow: stage list with status, a stage's gates and its guided questions."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.workflow", "left"), ("aether.viewport", "center"), widths=(0.42, 0.0),
                         persp="setup")
        wf = p["aether.workflow"]
        wf.refresh_now()
        self.wait(lambda: wf.stages, 120, "the workflow stages")
        print("  " + ", ".join(f"{s.id}:{s.status}" for s in wf.stages))
        pick = next((s for s in wf.stages if s.status == "attention"), wf.stages[0])
        wf.select_stage(pick.id)
        self.pump(1.0)
        items = [wf.gates.topLevelItem(i) for i in range(wf.gates.topLevelItemCount())]
        warn = next((it for it in items if not it.text(0).lstrip().startswith("\N{CHECK MARK}")), None)
        if warn is not None:
            wf.gates.setCurrentItem(warn)
        with contextlib.suppress(Exception):  # show all twelve stages above the stage page
            sp = wf.tree.parentWidget()
            tot = sum(sp.sizes())
            sp.setSizes([300, tot - 300])
        vp = self.viewport()
        vp.show_field("p", "60")
        self.drawn(vp)
        self.fit(vp)
        return [self.save("07-guided-workflow", self.grab())]

    def s08_checks(self):
        """Checks: a finding on buoyantCavity and the plan its one-click fix would apply."""
        self.open_case("buoyantCavity")
        p = self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), ("aether.checks", "right"),
                         widths=(0.14, 0.3), persp="setup")
        ch = p["aether.checks"]
        ch.run()
        self.wait(lambda: ch.summary.text() not in ("Checking…", "", "No case"), 180, "the checks")
        fix = next((i for i, f in enumerate(ch.findings) if f.edits), None)
        if fix is not None:
            ch.list.setCurrentRow(fix)
        vp = self.viewport()
        vp.show_field("T", self.latest(vp))
        self.drawn(vp)
        self.auto().showcase()
        self.drawn(vp)
        win_img = self.grab()
        out = []
        if fix is not None:  # the dialog Apply fix opens (built the same way as ChecksPanel._fix), then declined
            from aethercfd.commands.base import USER
            from aethercfd.studio.widgets.approval import ApprovalDialog

            f, s = ch.findings[fix], self.sv.session
            params = {"edits": [{"file": str(s.root / e.file), "key": e.key, "value": e.value,
                                 "insert_if_missing": e.insert_if_missing} for e in f.edits],
                      "summary": f"{f.fix_label} ({f.title})"}
            plan = s.executor.plan("dict.batch", params, USER, rationale=f.why)
            dlg = ApprovalDialog(plan, ch, who="AetherCFD checks")
            dlg.resize(800, 400)
            dlg.show()
            self.pump(1.0)
            out.append(self.save("08-checks-fix", self.paste(win_img, self.grab(dlg).copy(), at=(
                215, win_img.height() - 400 - 70))))
            dlg.reject()
            self.pump(0.3)
        else:
            out.append(self.save("08-checks-fix", win_img))
        return out

    def _analysis(self):
        """The Analysis panel beside a viewport on pitzDaily."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.analysis", "center"), ("aether.viewport", "right"), widths=(0.0, 0.5))
        an = p["aether.analysis"]
        self.wait(lambda: an.m_field.count() > 0, 120, "the analysis fields")
        an.m_field.setCurrentText("U")
        an.m_method.setCurrentText("POD")
        an.analyse()
        self.wait(lambda: not an.m_busy and an.m_out.toPlainText().startswith("POD"), 600, "POD")
        vp = self.viewport()
        vp.show_field("U", "60")
        self.drawn(vp)
        self.fit(vp)
        self.front(an)
        an.tabs.setCurrentIndex(0)
        self.pump(1.0)
        return an

    def s09_pod(self):
        """Analysis: POD modes of the saved pitzDaily times."""
        self._analysis()
        return [self.save("09-analysis-pod", self.grab())]

    def s10_pinn(self):
        """Analysis: the PINN tab; with --pinn, after one short Navier-Stokes PINN training on pitzDaily."""
        an = self._analysis()
        an.tabs.setCurrentIndex(next(i for i in range(an.tabs.count()) if "PINN" in an.tabs.tabText(i)))
        self.wait(lambda: "checking" not in an.p_status.text(), 120, "the PyTorch check")
        if self.pinn and an.p_go.isEnabled():
            if self.pinn == "cpu":  # the worker inherits this: torch then sees no CUDA device
                os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # "" would unset it on Windows
            an._pinn_fields()
            an.p_field.setCurrentText("U")
            an.p_phys.setCurrentIndex(max(0, an.p_phys.findData("ns")))
            an.p_coef.setValue(1e-5)
            an.p_ndata.setValue(400)
            an.p_epochs.setValue(PINN_EPOCHS["cpu" if self.pinn == "cpu" else "gpu"])
            an.p_lbfgs.setValue(0)
            t0 = time.perf_counter()
            an.train_pinn()
            self.wait(lambda: an.p_job is not None and not an.p_timer.isActive(), 110 if self.pinn == "gpu" else 360, "the PINN")
            if an.p_timer.isActive():
                an.stop_pinn()
                self.wait(lambda: not an.p_timer.isActive(), 30, "the PINN to stop")
            print(f"  PINN done in {time.perf_counter() - t0:.0f} s: {an.p_out.toPlainText()[:300]}", flush=True)
        self.pump(1.0)
        return [self.save("10-analysis-pinn", self.grab())]

    def s11_copilot(self):
        """Copilot panel and the approval dialog for a proposed dictionary edit (no message is sent)."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), ("aether.copilot", "right"),
                         widths=(0.14, 0.28), persp="setup")
        cp = p["aether.copilot"]
        self.wait(lambda: "Looking" not in cp.status.text(), 90, "the copilot CLI lookup")
        vp = self.viewport()
        vp.show_field("U", "60")
        self.drawn(vp)
        self.fit(vp)
        win_img = self.grab()
        # a prepared plan, as the copilot's dict.set tool would propose it (Claude is not asked)
        from aethercfd.commands.base import Actor, ActorKind
        from aethercfd.studio.widgets.approval import ApprovalDialog

        s = self.sv.session
        claude = Actor(ActorKind.AGENT, "claude")
        params = {"file": str(s.root / "system" / "fvSolution"), "key": "relaxationFactors/equations/U",
                  "value": "0.7"}
        plan = s.executor.plan("dict.set", params, claude, confidence=0.6, rationale=(
            "Uy and p level off near 2e-2 after iteration 40 while k and epsilon keep falling. Less "
            "under-relaxation on U (0.9 to 0.7) is the usual first step."))
        dlg = ApprovalDialog(plan, self.win, who="Claude")
        dlg.resize(720, 370)
        dlg.show()
        self.pump(1.0)
        out = [self.save("11-copilot-approval", self.paste(win_img, self.grab(dlg).copy(), at=(250, 110)))]
        dlg.reject()
        self.pump(0.3)
        return out

    def s12_themes(self):
        """Light and dark theme of the same window: pitzDaily pressure with the guided workflow."""
        self.open_case("pitzDaily")
        p = self.arrange(("aether.case_tree", "left"), ("aether.viewport", "center"), ("aether.workflow", "right"),
                         widths=(0.15, 0.25), persp="setup")
        p["aether.workflow"].refresh_now()
        vp = self.viewport()
        vp.show_field("p", "60")
        self.drawn(vp)
        self.fit(vp)
        self.wait(lambda: p["aether.workflow"].stages, 120, "the workflow stages")
        out = []
        for mode in ("light", "dark"):  # the theme changes live; the layout and camera stay
            self.sv.set_theme(mode)
            self.pump(1.5)
            self.drawn(vp)
            out.append(self.save(f"12-theme-{mode}", self.grab()))
        return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="comma-separated shot numbers, e.g. 01,05")
    ap.add_argument("--pinn", nargs="?", const="gpu", default="", choices=["gpu", "cpu"],
                    help="train one short PINN for the Analysis image (cpu: keep the GPU free)")
    ap.add_argument("--keep-cases", action="store_true", help="reuse the case copies from the last run")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT / "src"))
    _isolate()
    if args.keep_cases and CASES.is_dir():
        cases = {n: CASES / n for n in SOURCES if (CASES / n).is_dir()}
    else:
        cases = _copy_cases()
    only = {x.strip().zfill(2) for x in args.only.split(",") if x.strip()} or None
    shooter = Shooter(cases, only, args.pinn)
    code = shooter.run()
    shooter.win.close()
    shooter.pump(0.5)
    return code


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        # as aethercfd.studio.__main__.run does: os._exit still runs DLL detach, where QtWebEngine's Chromium
        # can crash or hang (seen here: an access violation after every image was saved)
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateProcess(k32.GetCurrentProcess(), code)
    os._exit(code)
