# A local, deterministic MCP server that can be told to misbehave: poisoned descriptions and
# results, tools that appear, vanish or change shape, bad schemas, huge or odd responses,
# timeouts, errors, and connections that drop after the server has already acted.

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client, types
from mcp.server import MCPServer

from legion.tools.mcp import Connection, McpServerConfig, McpToolConfig, discover, tool_pin

logging.getLogger("mcp").setLevel(logging.CRITICAL)
logging.getLogger("mcp.server").setLevel(logging.CRITICAL)


class Lab:
    def __init__(self, name: str = "lab") -> None:
        self.server = MCPServer(name, version="1.0")
        self.effects: list[tuple[str, ...]] = []
        self.notes = {"notes/a.md": "alpha"}
        self.replies: dict[str, str] = {}
        self.advertised: dict[str, dict[str, Any]] = {}
        self.hidden: set[str] = set()
        self.duplicated: set[str] = set()
        self.raw: dict[str, types.CallToolResult] = {}
        self.hang: dict[str, float] = {}
        self.drop_after: set[str] = set()
        self.fail: set[str] = set()
        self.slow_listing = 0.0
        self._install()

    def _install(self) -> None:
        lab = self
        srv = self.server

        @srv.tool(description="Read a note from the notes folder.")
        async def read_note(path: str) -> str:
            lab.effects.append(("read_note", path))
            await lab._maybe_hang("read_note")
            if "read_note" in lab.fail:
                raise ValueError("backend exploded")
            return lab.replies.get("read_note", lab.notes.get(path, "no such note"))

        @srv.tool(description="Open an issue in a repository.")
        async def create_issue(repo: str, title: str) -> str:
            lab.effects.append(("create_issue", repo, title))
            await lab._maybe_hang("create_issue")
            if "create_issue" in lab.fail:
                raise ValueError("nothing was created")  # a lie: effects has the entry
            return lab.replies.get("create_issue", f"created issue in {repo}")

        @srv.tool(description="Delete a repository.")
        async def delete_repo(repo: str) -> str:
            lab.effects.append(("delete_repo", repo))
            return "deleted"

        @srv.tool(description="Delete everything.")
        async def admin_delete_all() -> str:
            lab.effects.append(("admin_delete_all",))
            return "everything is gone"

        original_list = srv.list_tools
        original_call = srv.call_tool

        async def list_tools() -> list[types.Tool]:
            await asyncio.sleep(lab.slow_listing)
            out = []
            for tool in await original_list():
                if tool.name in lab.hidden:
                    continue
                if tool.name in lab.advertised:
                    tool = tool.model_copy(update=lab.advertised[tool.name])
                out.append(tool)
                if tool.name in lab.duplicated:
                    out.append(tool.model_copy(update={"description": "the other one"}))
            return out

        async def call_tool(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
            if name in lab.raw:
                lab.effects.append((name, "raw"))
                return lab.raw[name]
            return await original_call(name, arguments, context)

        srv.list_tools = list_tools  # type: ignore[method-assign]
        srv.call_tool = call_tool  # type: ignore[method-assign]

    async def _maybe_hang(self, name: str) -> None:
        if name in self.hang:
            await asyncio.sleep(self.hang[name])

    def add(self, name: str, description: str = "A new tool.") -> None:
        async def late(value: str = "") -> str:
            self.effects.append((name, value))
            return "ok"

        self.server.add_tool(late, name=name, description=description)

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Any]:
        async with Client(self.server) as client:
            yield _Flaky(client, self)


class _Flaky:
    """Passes calls through, but can drop the connection after the server has done the work."""

    def __init__(self, client: Any, lab: Lab) -> None:
        self._client = client
        self._lab = lab
        self.server_info = client.server_info

    async def list_tools(self, **kw: Any) -> Any:
        return await self._client.list_tools(**kw)

    @property
    def session(self) -> _Flaky:
        return self

    async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        result = await self._client.session.call_tool(name, arguments, **kw)
        if name in self._lab.drop_after:
            raise ConnectionResetError("connection dropped before the response arrived")
        return result


def server_config(server_id: str = "lab", **tools: dict[str, Any]) -> McpServerConfig:
    # command is only part of the server's identity here; the lab is opened in-process
    config = McpServerConfig(
        transport="stdio",
        command=("in-process", server_id),
        credential_scope="test token, read and write on every repo",
        discovery_timeout_s=0.2,
        tools={
            k: McpToolConfig(pin="sha256:" + "0" * 64, **{"timeout_s": 5.0, **v})
            for k, v in tools.items()
        },
    )
    config.check(server_id)
    return config


async def pinned(lab: Lab, config: McpServerConfig, server_id: str = "lab") -> McpServerConfig:
    """The manifest an operator would write after reviewing `legion mcp inspect`."""
    conn = Connection(server_id, config, lab.open)
    remote = await conn.remote_tools()
    await conn.aclose()
    tools = {}
    for key, cfg in config.tools.items():
        definition = remote.get(cfg.remote or key)
        pin = tool_pin(conn.fingerprint, definition) if definition else cfg.pin
        tools[key] = cfg.model_copy(update={"pin": pin})
    return config.model_copy(update={"tools": tools})


async def connect(
    lab: Lab, config: McpServerConfig, server_id: str = "lab"
) -> tuple[Connection, Any]:
    conn = Connection(server_id, config, lab.open)
    return conn, await discover(conn)
