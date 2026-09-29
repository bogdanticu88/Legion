from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from pathlib import Path

from legion.domain.errors import ConfigError
from legion.models.base import ToolDefinition
from legion.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ConfigError(f"tool {name} is registered twice")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self, names: Iterable[str]) -> tuple[ToolDefinition, ...]:
        out = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                raise ConfigError(f"unknown tool {name}")
            out.append(
                ToolDefinition(
                    name=name,
                    description=tool.spec.description,
                    input_schema=tool.spec.input_schema,
                )
            )
        return tuple(out)


def load_tool_module(path: Path) -> list[Tool]:
    # Runs the file. Tool modules are trusted like the rest of the operator config.
    if not path.is_file():
        raise ConfigError(f"tool module not found: {path}")
    spec = importlib.util.spec_from_file_location(f"legion_tools_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot import tool module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = getattr(module, "TOOLS", None)
    if not isinstance(tools, list):
        raise ConfigError(f"{path} must define TOOLS as a list")
    return tools
