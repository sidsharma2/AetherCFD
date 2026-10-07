# ADR 0004 — One QtAds manager; pyvistaqt viewport as-is; xterm.js terminal

Status: accepted (2026-10-07) — evidence: `spikes/spike_a_gl_coexistence.py`, `spikes/out/spike_a_*.json`

## Context
The studio needs dockable/floating panels across up to three monitors, a VTK viewport
(QOpenGLWidget-based pyvistaqt 0.13.1) and a terminal. Known risks: QOpenGLWidget + QWebEngineView in
one window on Intel drivers, GL context loss when a GL widget is re-parented into a floating window,
QtAds/PySide6 version lag, DPI scaling.

## Spike A results (laptop, Intel UHD, Qt 6.11.2, QtAds 5.1.1, VTK 9.7.1)
With the bootstrap below, all checks pass:
- xterm.js renders inside QWebEngineView, and survives the viewport soak and being floated itself.
- 150 programmatic float→dock cycles of the viewport: renders every frame; the GL context is created
  exactly once (initializeGL=1, context cleanups=0) — re-parenting never recreates it.
- saveState/restoreState round trip keeps the viewport rendering. Panels can be tabbed into the
  central dock area. No GL/RHI/WebEngine warnings from Qt. Lazy QtWebEngine import after
  QApplication works.
- At `QT_SCALE_FACTOR=1.5` the VTK framebuffer is exactly widget×1.5 (600×764 for 400×509).
- Default renderer is **Intel UHD Graphics, GL 3.2 core** (Optimus) → the GPU banner/flow is needed.
- The process image is the *base* uv interpreter
  (`%APPDATA%\uv\python\cpython-3.12-windows-x86_64-none\python.exe`), not the venv launcher — a
  per-app GPU preference must target that path (or the packaged exe).

Harness lessons (not app bugs, but they shape tests and DockHost):
- Without a running event loop, `deleteLater()` never runs, so every programmatic float leaks a
  `CFloatingDockContainer` + area + menu (one native window per cycle; ~85 such GL-composited windows
  eventually blanked the viewport). Tests that drive docking without `exec()` must flush
  `QEvent.DeferredDelete`. With deferred deletes processed there is no leak.
- When the soak ran on a live desktop (user input/focus changes) the viewport dock was intermittently
  left hidden at ~120×37 px; the GL context was untouched. DockHost must, after every programmatic
  move, ensure the dock is open (`toggleView(True)`), current (`setAsCurrentTab()`) and visible, and
  the soak test must run input-isolated.

## Decision
1. Bootstrap before QApplication: `QSurfaceFormat.setDefaultFormat(pyvistaqt.rwi._default_format())`,
   `AA_ShareOpenGLContexts`, `QQuickWindow.setGraphicsApi(OpenGL)`.
2. One `CDockManager`; QtAds floating containers are the per-monitor workspaces.
3. Viewport: pyvistaqt `QtInteractor(auto_update=False)` wrapped in our `RenderSurface`; keep the
   `SceneModel` rebuild path only as a defensive fallback.
4. Terminal: pywinpty ConPTY + xterm.js 6.0 in a lazily created QWebEngineView. The pyte fallback is
   not needed.
5. Pin PySide6 6.11.2 ↔ PySide6-QtAds 5.1.1 as an exact pair (doctor checks it).

## Open
NVIDIA (dGPU) run not yet done — it needs a per-user GPU preference written for the interpreter above,
which requires the user's explicit consent.
