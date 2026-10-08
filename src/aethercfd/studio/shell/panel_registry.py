"""Registry of panel types (built-in + plugins) and panel bundles."""

from __future__ import annotations

from dataclasses import dataclass

from aethercfd.studio.shell.panel_base import PanelSpec


@dataclass(frozen=True)
class PanelBundle:
    id: str
    title: str
    members: tuple[str, ...]  # panel type ids
    description: str = ""


class PanelRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, PanelSpec] = {}
        self._bundles: dict[str, PanelBundle] = {}

    def register(self, spec: PanelSpec) -> None:
        if spec.type_id in self._specs:
            raise ValueError(f"panel type {spec.type_id!r} registered twice")
        self._specs[spec.type_id] = spec

    def register_bundle(self, bundle: PanelBundle) -> None:
        self._bundles[bundle.id] = bundle

    def get(self, type_id: str) -> PanelSpec | None:
        return self._specs.get(type_id)

    def specs(self) -> list[PanelSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.category, s.title))

    def categories(self) -> dict[str, list[PanelSpec]]:
        out: dict[str, list[PanelSpec]] = {}
        for s in self.specs():
            out.setdefault(s.category, []).append(s)
        return out

    def bundles(self) -> list[PanelBundle]:
        return list(self._bundles.values())
