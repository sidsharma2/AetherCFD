"""Built-in panels. Each registers a PanelSpec; plugins register theirs the same way."""

from __future__ import annotations

from aethercfd.studio.shell.panel_base import Area, PanelSpec
from aethercfd.studio.shell.panel_registry import PanelBundle, PanelRegistry


def register_builtin_panels(reg: PanelRegistry) -> None:
    from aethercfd.studio.panels.welcome.panel import WelcomePanel

    reg.register(PanelSpec("aether.welcome", "Start", "msc.home", "Case", WelcomePanel,
                           default_area=Area.CENTER, description="Open cases, recent cases, environment"))
    _register_optional(reg)
    reg.register_bundle(PanelBundle("run-monitor", "Run Monitor (Run + Residuals + Log)",
                                    ("aether.run_status", "aether.residuals", "aether.solver_log")))


def _register_optional(reg: PanelRegistry) -> None:
    """Panels that are added step by step; import failures must not break the shell."""
    import importlib
    import logging

    for mod in ("aethercfd.studio.panels.case_tree.panel", "aethercfd.studio.panels.editor.panel",
                "aethercfd.studio.panels.viewport.panel", "aethercfd.studio.panels.run.panels",
                "aethercfd.studio.panels.terminal.panel", "aethercfd.studio.panels.journal.panel",
                "aethercfd.studio.panels.mesh_quality.panel", "aethercfd.studio.panels.workflow.panel",
                "aethercfd.studio.panels.copilot.panel", "aethercfd.studio.panels.vis.panel",
                "aethercfd.studio.panels.inspector.panel", "aethercfd.studio.panels.checks.panel",
                "aethercfd.studio.panels.combustion.panel",
                "aethercfd.studio.panels.code.panel", "aethercfd.studio.panels.aethermesh.panel",
                "aethercfd.studio.panels.diff.panel", "aethercfd.studio.panels.python_console.panel",
                "aethercfd.studio.panels.measurements.panel", "aethercfd.studio.panels.analysis.panel"):
        try:
            m = importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("aethercfd.studio.panels"):
                continue  # not built yet
            logging.getLogger(__name__).exception("panel module %s failed to import", mod)
            continue
        except Exception:
            logging.getLogger(__name__).exception("panel module %s failed to import", mod)
            continue
        for spec in getattr(m, "PANELS", []):
            reg.register(spec)
