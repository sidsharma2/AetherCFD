"""Command registry. Exports JSON schemas so the same commands can become MCP tools later."""

from __future__ import annotations

from aethercfd.commands.base import Command


class Registry:
    def __init__(self) -> None:
        self._cmds: dict[str, Command] = {}

    def register(self, cmd: Command) -> Command:
        name = cmd.spec.name
        if name in self._cmds:
            raise ValueError(f"command {name!r} registered twice")
        self._cmds[name] = cmd
        return cmd

    def get(self, name: str) -> Command:
        try:
            return self._cmds[name]
        except KeyError:
            raise KeyError(f"unknown command {name!r}") from None

    def names(self) -> list[str]:
        return sorted(self._cmds)

    def export_json_schema(self) -> list[dict]:
        out = []
        for name in self.names():
            spec = self._cmds[name].spec
            out.append({"name": name, "description": spec.summary, "risk": spec.risk.value,
                        "input_schema": spec.params.model_json_schema()})
        return out


def default_registry() -> Registry:
    from aethercfd.commands.builtin import code_cmds, dict_cmds, file_cmds, run_cmds

    reg = Registry()
    for mod in (file_cmds, dict_cmds, run_cmds, code_cmds):
        for cmd in mod.COMMANDS:
            reg.register(cmd)
    return reg
