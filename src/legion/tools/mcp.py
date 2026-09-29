# MCP tools behind Legion's normal tool interface. An MCP server is untrusted: it supplies a
# transport and some code to run, never authority. What a tool may do, what it needs and what
# its effect is all come from the operator's manifest; what the server says about itself is
# pinned by hash, shown to the model as untrusted text, and never believed.

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from legion.access.base import check_endpoint
from legion.access.secrets import CredentialResolver, SecretRef
from legion.canonical import digest
from legion.domain.action import EffectClass
from legion.domain.errors import ActionInDoubt, ConfigError, ToolFailed, ToolRetryable
from legion.tools.base import TOOL_NAME, ToolContext, ToolResult, ToolSpec, resource_from_argument

_SERVER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
_UNSAFE_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]"
)
DESCRIPTION_LIMIT = 1_000


class McpToolConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # the tool's name on the server, if it differs from the key Legion uses
    remote: str | None = None
    # Not taken from the server's annotations: a server saying "read-only" proves nothing.
    effect: EffectClass = EffectClass.EXTERNAL_IRREVERSIBLE
    capabilities: tuple[str, ...] = ()
    resource_arg: str | None = None
    # replaces the server's description, which is otherwise shown to the model as-is
    description: str | None = None
    pin: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    timeout_s: float = Field(default=30.0, gt=0, le=3600)
    max_attempts: int = Field(default=1, ge=1, le=5)


class McpServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["stdio", "http"]
    command: tuple[str, ...] = ()
    cwd: str | None = None
    url: str | None = None
    # values are secret references (env:NAME); names are part of the server's identity
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    # what the server's own credentials can do, as the operator understands it. Legion can't
    # check this; it's recorded so nobody mistakes Legion's grant for the downstream authority.
    credential_scope: str = ""
    pin_check: Literal["every_call", "discovery"] = "every_call"
    # connecting and listing tools, at startup and before each call
    discovery_timeout_s: float = Field(default=10.0, gt=0, le=300)
    max_response_bytes: int = Field(default=65_536, gt=0, le=10_000_000)
    max_depth: int = Field(default=32, gt=0, le=256)
    max_items: int = Field(default=1_000, gt=0, le=100_000)
    tools: dict[str, McpToolConfig] = Field(default_factory=dict)

    @field_validator("env", "headers")
    @classmethod
    def _refs(cls, value: dict[str, str]) -> dict[str, str]:
        for ref in value.values():
            SecretRef.parse(ref)
        return value

    def check(self, server_id: str) -> None:
        if not _SERVER_ID.match(server_id):
            raise ConfigError(f"MCP server id must match {_SERVER_ID.pattern}: {server_id!r}")
        if self.transport == "stdio":
            if not self.command or not self.command[0]:
                raise ConfigError(f"MCP server {server_id} uses stdio but has no command")
            if self.url or self.headers:
                raise ConfigError(f"MCP server {server_id} uses stdio; url and headers don't apply")
        if self.transport == "http":
            if not self.url:
                raise ConfigError(f"MCP server {server_id} uses http but has no url")
            if self.command or self.cwd or self.env:
                raise ConfigError(
                    f"MCP server {server_id} uses http; command, cwd and env don't apply"
                )
            # Always treated as carrying credentials: plain http only to this machine.
            try:
                check_endpoint(self.url, carries_credentials=True)
            except ValueError as exc:
                raise ConfigError(f"MCP server {server_id}: {exc}") from exc
        for key, tool in self.tools.items():
            if not TOOL_NAME.match(local_name(server_id, key)):
                raise ConfigError(f"MCP tool key {key!r} on {server_id} isn't a usable name")
            # The pin check before a call is part of the call's time. If it could use all of it,
            # a stalled check would look like a write that timed out, which it isn't.
            if tool.timeout_s <= self.discovery_timeout_s:
                raise ConfigError(
                    f"MCP tool {key} on {server_id}: timeout_s must be longer than the "
                    f"server's discovery_timeout_s ({self.discovery_timeout_s})"
                )


_RUNNERS = ("npx", "bunx", "pnpx", "uvx", "pipx")


def unpinned_package(config: McpServerConfig) -> str | None:
    """The package a runner like npx will fetch at its latest version, if it looks unversioned."""
    if config.transport != "stdio" or not config.command:
        return None
    if Path(config.command[0]).name not in _RUNNERS:
        return None
    args = [a for a in config.command[1:] if not a.startswith("-") and a != "run"]
    if not args:
        return None
    package = args[0]
    # npm style name@1.2.3 (a leading @ is a scope), or python style name==1.2.3
    versioned = "@" in package.lstrip("@") or "==" in package
    return None if versioned else package


def local_name(server_id: str, key: str) -> str:
    return f"mcp_{server_id}_{key}"


def capability(server_id: str, key: str) -> str:
    return f"mcp.{server_id}.{key}"


def server_fingerprint(server_id: str, config: McpServerConfig) -> str:
    # What makes "the same server": our id for it, how we reach it, and which credentials we
    # hand it. Change any of these and every pin on that server stops matching.
    return digest(
        {
            "id": server_id,
            "transport": config.transport,
            "command": list(config.command),
            "cwd": config.cwd,
            "url": config.url,
            "env": dict(sorted(config.env.items())),
            "headers": dict(sorted(config.headers.items())),
        }
    )


def tool_pin(fingerprint: str, remote: Any) -> str:
    # Everything the server tells us about a tool, description included: a description that
    # changes after review is how a trusted tool turns into a poisoned one.
    annotations = remote.annotations.model_dump(mode="json") if remote.annotations else None
    return "sha256:" + digest(
        {
            "server": fingerprint,
            "name": remote.name,
            "description": remote.description,
            "input_schema": remote.input_schema,
            "output_schema": remote.output_schema,
            "annotations": annotations,
        }
    )


class Connection:
    """One MCP server, connected lazily and reconnected after a failure."""

    def __init__(
        self,
        server_id: str,
        config: McpServerConfig,
        opener: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        self.server_id = server_id
        self.config = config
        self.fingerprint = server_fingerprint(server_id, config)
        self._opener = opener
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self.server_info: dict[str, Any] | None = None

    async def client(self) -> Any:
        if self._client is None:
            stack = AsyncExitStack()
            try:
                async with asyncio.timeout(self.config.discovery_timeout_s):
                    self._client = await stack.enter_async_context(self._opener())
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack
            info = getattr(self._client, "server_info", None)
            # what the server calls itself; recorded, not trusted
            self.server_info = info.model_dump(mode="json") if info is not None else None
        return self._client

    async def remote_tools(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        cursor = None
        async with asyncio.timeout(self.config.discovery_timeout_s):
            client = await self.client()
            pages = [await client.list_tools(cursor=cursor, cache_mode="bypass")]
            while pages[-1].next_cursor is not None and len(pages) < 50:
                cursor = pages[-1].next_cursor
                pages.append(await client.list_tools(cursor=cursor, cache_mode="bypass"))
        for page in pages:
            for tool in page.tools:
                if tool.name in found:
                    # two definitions under one name: nothing about it can be trusted
                    found[tool.name] = None
                else:
                    found[tool.name] = tool
        return found

    async def call(self, name: str, arguments: dict[str, Any], timeout: float) -> Any:
        client = await self.client()
        # Client.call_tool tries to answer input_required through callbacks Legion doesn't set,
        # then raises, which would look like a lost connection. This returns the result as is.
        return await client.session.call_tool(
            name, arguments, read_timeout_seconds=timeout, allow_input_required=True
        )

    async def reset(self) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            # the connection is being thrown away after a failure; closing it may fail too
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def aclose(self) -> None:
        await self.reset()


def opener_for(
    config: McpServerConfig,
    resolver: CredentialResolver,
    errlog: Path | None = None,
    base: Path | None = None,
) -> Callable[[], AbstractAsyncContextManager[Any]]:
    # `base` is the config directory: a relative cwd means relative to legion.yaml, like every
    # other path there. The pin uses cwd as written, so it doesn't depend on where that is.
    cwd = config.cwd
    if base is not None:
        cwd = str(base / cwd) if cwd is not None else str(base)

    import httpx2
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async def resolve_all(refs: Mapping[str, str]) -> dict[str, str]:
        return {k: (await resolver.resolve(SecretRef.parse(v))).reveal() for k, v in refs.items()}

    @asynccontextmanager
    async def open_stdio() -> AsyncIterator[Any]:
        env = await resolve_all(config.env)
        params = StdioServerParameters(
            command=config.command[0],
            args=list(config.command[1:]),
            env=env or None,
            cwd=cwd,
        )
        # A server's stderr would otherwise land in the operator's terminal, where it could pass
        # for Legion's own output. It goes to a file instead.
        # The server's stderr isn't scrubbed and may hold its own token, so only this user reads it.
        target = errlog or Path(os.devnull)
        with open(os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as log:
            # no response cache: every listing has to come from the server as it is now
            async with Client(stdio_client(params, errlog=log), cache=None) as client:
                yield client

    @asynccontextmanager
    async def open_http() -> AsyncIterator[Any]:
        assert config.url is not None
        headers = await resolve_all(config.headers)
        # No redirects: the credentials in these headers go to the configured endpoint only,
        # and a server shouldn't be able to point us anywhere else.
        http = httpx2.AsyncClient(headers=headers, follow_redirects=False)
        transport = streamable_http_client(config.url, http_client=http)
        async with http, Client(transport, cache=None) as client:
            yield client

    return open_stdio if config.transport == "stdio" else open_http


@dataclass
class Discovery:
    tools: list[McpTool] = field(default_factory=list)
    # tool key -> why it wasn't registered
    blocked: dict[str, str] = field(default_factory=dict)
    # what the server offers that the manifest doesn't mention; never registered
    unlisted: list[str] = field(default_factory=list)
    pins: dict[str, str] = field(default_factory=dict)


async def discover(connection: Connection) -> Discovery:
    """Match what the server offers against the manifest. Only exact, pinned matches register."""
    result = Discovery()
    config = connection.config
    remote = await connection.remote_tools()
    wanted = {cfg.remote or key: key for key, cfg in config.tools.items()}
    result.unlisted = sorted(name for name in remote if name not in wanted)
    for key, cfg in config.tools.items():
        remote_name = cfg.remote or key
        definition = remote.get(remote_name, "missing")
        if definition == "missing":
            result.blocked[key] = f"the server doesn't offer {remote_name!r}"
            continue
        if definition is None:
            result.blocked[key] = f"the server offers {remote_name!r} more than once"
            continue
        pin = tool_pin(connection.fingerprint, definition)
        result.pins[key] = pin
        if pin != cfg.pin:
            result.blocked[key] = (
                f"{remote_name!r} doesn't match its pin (it or the server changed)"
            )
            continue
        try:
            result.tools.append(McpTool(connection, key, cfg, definition, pin))
        except ValueError as exc:
            result.blocked[key] = f"{remote_name!r} can't be used: {exc}"
    return result


class McpTool:
    def __init__(
        self, connection: Connection, key: str, config: McpToolConfig, remote: Any, pin: str
    ) -> None:
        self.connection = connection
        self.key = key
        self.config = config
        self.remote_name = config.remote or key
        self.pin = pin
        self.blocked: str | None = None
        server_id = connection.server_id
        description = config.description or _clean(remote.description or "") or self.remote_name
        self._spec = ToolSpec(
            name=local_name(server_id, key),
            description=description[:DESCRIPTION_LIMIT],
            input_schema=remote.input_schema,
            output_schema=remote.output_schema,
            effect=config.effect,
            capabilities=(capability(server_id, key), *config.capabilities),
            resource_arg=config.resource_arg,
            timeout_s=config.timeout_s,
            max_attempts=config.max_attempts,
            origin={
                "kind": "mcp",
                "server": server_id,
                "server_fingerprint": connection.fingerprint,
                "remote_tool": self.remote_name,
                "pin": pin,
                "credential_scope": connection.config.credential_scope,
            },
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def secret_refs(self) -> list[SecretRef]:
        config = self.connection.config
        return [SecretRef.parse(v) for v in (*config.env.values(), *config.headers.values())]

    def resource_of(self, arguments: dict[str, Any]) -> str | None:
        return resource_from_argument(self._spec, arguments)

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if self.blocked:
            raise ToolFailed(self.blocked)
        connection = self.connection
        # Anything that fails before the request goes out is a clean failure: nothing was sent.
        if connection.config.pin_check == "every_call":
            try:
                remote = await connection.remote_tools()
            except Exception as exc:
                # includes the check timing out; nothing has been sent yet
                await connection.reset()
                raise ToolFailed(
                    f"MCP server {connection.server_id} unreachable: {type(exc).__name__}"
                ) from exc
            current = remote.get(self.remote_name)
            if current is None or tool_pin(connection.fingerprint, current) != self.pin:
                # stop using it for the rest of this process; re-pinning is an operator decision
                self.blocked = (
                    f"{self.remote_name} on {connection.server_id} changed since it was pinned"
                )
                raise ToolFailed(self.blocked)
        else:
            try:
                await connection.client()
            except Exception as exc:
                await connection.reset()
                raise ToolFailed(
                    f"MCP server {connection.server_id} unreachable: {type(exc).__name__}"
                ) from exc

        try:
            result = await connection.call(self.remote_name, arguments, self.config.timeout_s)
        except Exception as exc:
            # The request may have reached the server. A read can be tried again; for anything
            # else nobody knows whether it happened.
            await connection.reset()
            reason = (
                f"MCP call to {connection.server_id} failed after sending: {type(exc).__name__}"
            )
            if self._spec.effect.safe_to_repeat:
                raise ToolRetryable(reason) from exc
            raise ActionInDoubt(reason) from exc
        return convert(result, self._spec, connection.config)


def convert(result: Any, spec: ToolSpec, limits: McpServerConfig) -> ToolResult:
    """Turn an MCP result into what the model sees, within limits. It ran either way."""
    if getattr(result, "result_type", "complete") != "complete":
        return ToolResult(
            content=f"{spec.name} asked for more input, which Legion doesn't provide.",
            is_error=True,
        )
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(str(block.text))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} omitted: {getattr(block, 'mime_type', '?')}]")
        elif kind == "resource_link":
            # a link is shown, never followed
            parts.append(f"[resource link, not fetched: {getattr(block, 'uri', '?')}]")
        elif kind == "resource":
            text = getattr(getattr(block, "resource", None), "text", None)
            parts.append(str(text) if text is not None else "[embedded binary resource omitted]")
        else:
            parts.append(f"[{kind} content omitted]")
    text = "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    size = len(text.encode("utf-8")) + len(json.dumps(structured, default=str).encode("utf-8"))
    problem = None
    if size > limits.max_response_bytes:
        problem = f"returned {size} bytes, over the {limits.max_response_bytes} byte limit"
    elif structured is not None and _too_big(structured, limits.max_depth, limits.max_items):
        problem = "returned structured data that is too deep or too large"
    if problem:
        return ToolResult(
            content=f"{spec.name} ran, but its response was withheld: it {problem}.", is_error=True
        )
    return ToolResult(
        content=text, data=structured, is_error=bool(getattr(result, "is_error", False))
    )


def _too_big(value: Any, max_depth: int, max_items: int) -> bool:
    stack = [(value, 1)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            return True
        if isinstance(item, dict):
            children = list(item.values())
        elif isinstance(item, list):
            children = item
        else:
            continue
        seen += len(children)
        if seen > max_items:
            return True
        stack.extend((c, depth + 1) for c in children)
    return False


def _clean(text: str) -> str:
    return _UNSAFE_CHARS.sub("?", text)


def check_servers(servers: Mapping[str, McpServerConfig]) -> None:
    if servers and importlib.util.find_spec("mcp") is None:
        # not "pip install legion[mcp]": the legion package on PyPI is someone else's project, and
        # legion-runtime isn't published
        raise ConfigError("mcp_servers needs the MCP SDK: run `uv sync --extra mcp`")
    for server_id, config in servers.items():
        config.check(server_id)
