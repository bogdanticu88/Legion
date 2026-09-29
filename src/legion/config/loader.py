from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from legion.access.base import AccessProvider, ApiKeyAccess, NoAuth, check_endpoint
from legion.access.secrets import EnvResolver, SecretRef
from legion.artifacts import FileArtifactStore
from legion.authority.policy import Rule, RuleTablePolicy, Verdict, default_rules
from legion.canonical import digest
from legion.domain.agent import AgentSpec
from legion.domain.capability import Capability
from legion.domain.errors import ConfigError
from legion.events.sqlite_store import SqliteEventStore
from legion.kernel.credentials import CredentialMapping
from legion.kernel.locks import FileRunLocks
from legion.kernel.runtime import Legion
from legion.kernel.services import RetryPolicy
from legion.models.anthropic import AnthropicProvider
from legion.models.base import ModelProvider
from legion.models.openai_compat import OpenAICompatProvider
from legion.models.resolver import ModelBinding, ModelResolver
from legion.models.scripted import ScriptedProvider
from legion.ports.credentials import Assurance, CredentialAuthority
from legion.tools.mcp import (
    Connection,
    McpServerConfig,
    capability,
    check_servers,
    discover,
    local_name,
    opener_for,
)
from legion.tools.registry import ToolRegistry, load_tool_module


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AccessConfig(_Strict):
    kind: Literal["none", "api_key"] = "none"
    secret: str | None = None

    @model_validator(mode="after")
    def _secret(self) -> AccessConfig:
        if self.kind == "api_key" and self.secret is None:
            raise ValueError("api_key access needs `secret: env:NAME`")
        if self.kind == "none" and self.secret is not None:
            raise ValueError("`secret` is set but access kind is none")
        if self.secret is not None:
            SecretRef.parse(self.secret)
        return self


class ProviderConfig(_Strict):
    kind: Literal["scripted", "openai_compat", "anthropic"]
    base_url: str | None = None
    access: AccessConfig = AccessConfig()
    script: str | None = None
    timeout_s: float = Field(default=120.0, gt=0, le=3600)
    output_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

    @model_validator(mode="after")
    def _fields(self) -> ProviderConfig:
        if self.kind == "scripted" and not self.script:
            raise ValueError("scripted provider needs `script`")
        if self.kind == "openai_compat" and not self.base_url:
            raise ValueError("openai_compat provider needs `base_url`")
        if self.base_url is not None:
            check_endpoint(self.base_url, carries_credentials=self.access.kind != "none")
        return self


class AuthorityConfig(_Strict):
    grantable: list[str] = Field(default_factory=list)
    max_delegation_depth: int = Field(default=2, ge=0, le=8)
    max_tasks: int = Field(default=16, ge=1, le=256)


class PolicyConfig(_Strict):
    default: Verdict = Verdict.ALLOW
    rules: list[Rule] = Field(default_factory=default_rules)


class RetryConfig(_Strict):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay: float = Field(default=1.0, ge=0, le=3600)
    max_delay: float = Field(default=30.0, ge=0, le=3600)


class ApprovalConfig(_Strict):
    ttl_seconds: int = Field(default=3600, gt=0, le=7 * 24 * 3600)


class CredentialAuthorityConfig(_Strict):
    # a Python file defining AUTHORITY; trusted code, like a tool module
    module: str
    # only a trusted authority's evidence can make a credential verified or bound
    trusted: bool = False


class CredentialPolicy(_Strict):
    # the least assurance any credential may have; a mapping can ask for more
    minimum: Assurance = Assurance.UNVERIFIED
    timeout_s: float = Field(default=10.0, gt=0, le=120)


class LegionConfig(_Strict):
    version: Literal[1]
    store: str = ".legion/legion.db"
    artifacts: str = ".legion/artifacts"
    tool_modules: list[str] = Field(default_factory=list)
    # agents that can be delegated to, one YAML file each
    agents_dir: str | None = "agents"
    tool_settings: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig]
    models: list[ModelBinding]
    authority: AuthorityConfig = AuthorityConfig()
    policy: PolicyConfig = PolicyConfig()
    # name -> env:NAME (static, unverified) or a mapping onto a credential authority
    credentials: dict[str, str | CredentialMapping] = Field(default_factory=dict)
    credential_authorities: dict[str, CredentialAuthorityConfig] = Field(default_factory=dict)
    credential_policy: CredentialPolicy = CredentialPolicy()
    retry: RetryConfig = RetryConfig()
    approvals: ApprovalConfig = ApprovalConfig()
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @field_validator("store", "artifacts")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("must be a usable path")
        return value

    @field_validator("credentials")
    @classmethod
    def _refs(cls, value: dict[str, str | CredentialMapping]) -> dict[str, str | CredentialMapping]:
        for ref in value.values():
            if isinstance(ref, str):
                SecretRef.parse(ref)
        return value

    @model_validator(mode="after")
    def _authorities(self) -> LegionConfig:
        for name, cred in self.credentials.items():
            if (
                isinstance(cred, CredentialMapping)
                and cred.authority not in self.credential_authorities
            ):
                raise ValueError(
                    f"credential {name} uses authority {cred.authority!r}, not configured"
                )
        return self


@dataclass
class Loaded:
    path: Path
    config: LegionConfig
    config_hash: str
    providers: dict[str, ModelProvider] = field(default_factory=dict)
    connections: list[Connection] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.path.parent

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def store(self) -> SqliteEventStore:
        return SqliteEventStore(self.resolve_path(self.config.store))

    def locks(self) -> FileRunLocks:
        return FileRunLocks(self.resolve_path(self.config.store).parent / "locks")

    def check_state_dir(self) -> None:
        # The event log holds approvals, and the config and tool modules decide what runs. If a
        # tool setting points at a directory containing any of them, an agent could approve its
        # own actions or change its own code, so refuse. Paths that don't exist yet count too;
        # the first run would create them. It can't see paths tools pick themselves.
        protected = {
            "the state directory": self.resolve_path(self.config.store).parent,
            "legion.yaml": self.path,
            **{f"tool module {m}": self.resolve_path(m) for m in self.config.tool_modules},
            **{
                f"credential authority module {a.module}": self.resolve_path(a.module)
                for a in self.config.credential_authorities.values()
            },
        }
        if self.config.agents_dir is not None:
            protected["the agents directory"] = self.resolve_path(self.config.agents_dir)
        for key, value in self.config.tool_settings.items():
            candidate = self.resolve_path(value).resolve()
            for what, path in protected.items():
                if path.resolve().is_relative_to(candidate):
                    raise ConfigError(
                        f"tool setting {key}={value!r} lets tools write {what}; point it "
                        f"somewhere that holds only what the tools should touch"
                    )

    async def build(self, store: SqliteEventStore | None = None) -> Legion:
        self.check_state_dir()
        env = EnvResolver()
        self.providers = {name: self._provider(p, env) for name, p in self.config.providers.items()}
        tools = ToolRegistry()
        for module in self.config.tool_modules:
            for tool in load_tool_module(self.resolve_path(module)):
                tools.register(tool)
        for server_id, server in self.config.mcp_servers.items():
            await self._register_mcp(server_id, server, env, tools)
        legion = Legion(
            resolver=ModelResolver(self.config.models, self.providers),
            tools=tools,
            store=store or self.store(),
            policy=RuleTablePolicy(self.config.policy.rules, self.config.policy.default),
            grantable=[Capability.parse(c) for c in self.config.authority.grantable],
            credentials=env,
            credential_bindings={
                k: SecretRef.parse(v)
                for k, v in self.config.credentials.items()
                if isinstance(v, str)
            },
            credential_mappings={
                k: v for k, v in self.config.credentials.items() if isinstance(v, CredentialMapping)
            },
            credential_authorities={
                name: load_authority_module(self.resolve_path(cfg.module), name)
                for name, cfg in self.config.credential_authorities.items()
            },
            trusted_authorities=[
                name for name, cfg in self.config.credential_authorities.items() if cfg.trusted
            ],
            credential_minimum=self.config.credential_policy.minimum,
            credential_timeout_s=self.config.credential_policy.timeout_s,
            secret_refs=[
                SecretRef.parse(p.access.secret)
                for p in self.config.providers.values()
                if p.access.secret is not None
            ],
            artifacts=FileArtifactStore(self.resolve_path(self.config.artifacts)),
            settings={**self.config.tool_settings, "config_dir": str(self.root)},
            retry=RetryPolicy(**self.config.retry.model_dump()),
            config_hash=self.config_hash,
            locks=self.locks(),
            approval_ttl=timedelta(seconds=self.config.approvals.ttl_seconds),
            agents=self.agents(),
            max_delegation_depth=self.config.authority.max_delegation_depth,
            max_tasks=self.config.authority.max_tasks,
        )
        self._check_rules(legion.tools)
        return legion

    def _check_rules(self, tools: ToolRegistry) -> None:
        # A rule that matches no tool or capability reads like a control and does nothing, e.g.
        # `tool: publish-summary` when the tool is publish_summary. Refuse it.
        names = [*tools.names(), *tools.blocked]
        caps = {c for n in tools.names() if (t := tools.get(n)) for c in t.spec.capabilities}
        for server_id, server in self.config.mcp_servers.items():
            for key, cfg in server.tools.items():
                caps.update((capability(server_id, key), *cfg.capabilities))
        for i, rule in enumerate(self.config.policy.rules):
            if rule.tool is not None and not any(fnmatchcase(n, rule.tool) for n in names):
                raise ConfigError(f"policy rule {i + 1}: no tool matches {rule.tool!r}")
            if rule.capability is not None and not any(
                fnmatchcase(c, rule.capability) for c in caps
            ):
                raise ConfigError(
                    f"policy rule {i + 1}: no tool needs a capability matching {rule.capability!r}"
                )

    def agents(self) -> dict[str, AgentSpec]:
        if self.config.agents_dir is None:
            return {}
        directory = self.resolve_path(self.config.agents_dir)
        if not directory.is_dir():
            return {}
        catalog: dict[str, AgentSpec] = {}
        for path in sorted(directory.glob("*.yaml")):
            spec = load_agent(path)
            if spec.name in catalog:
                raise ConfigError(f"two agents are called {spec.name!r} in {directory}")
            catalog[spec.name] = spec
        return catalog

    def connection(self, server_id: str, env: EnvResolver | None = None) -> Connection:
        server = self.config.mcp_servers[server_id]
        logs = self.resolve_path(self.config.store).parent / "mcp"
        logs.mkdir(parents=True, exist_ok=True)
        opener = opener_for(
            server, env or EnvResolver(), errlog=logs / f"{server_id}.stderr.log", base=self.root
        )
        conn = Connection(server_id, server, opener)
        self.connections.append(conn)
        return conn

    async def _register_mcp(
        self, server_id: str, server: McpServerConfig, env: EnvResolver, tools: ToolRegistry
    ) -> None:
        # A server that can't be reached, or whose tools don't match their pins, just doesn't
        # contribute tools; an agent that needs them then refuses to start and says why.
        conn = self.connection(server_id, env)
        try:
            found = await discover(conn)
        except Exception as exc:
            for key in server.tools:
                tools.blocked[local_name(server_id, key)] = (
                    f"MCP server {server_id} unreachable ({type(exc).__name__})"
                )
            await conn.reset()
            return
        for tool in found.tools:
            tools.register(tool)
        for key, reason in found.blocked.items():
            tools.blocked[local_name(server_id, key)] = reason

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
        for conn in self.connections:
            await conn.aclose()

    def _provider(self, cfg: ProviderConfig, env: EnvResolver) -> ModelProvider:
        access: AccessProvider = NoAuth()
        if cfg.access.kind == "api_key" and cfg.access.secret:
            access = ApiKeyAccess(SecretRef.parse(cfg.access.secret), env)
        if cfg.kind == "scripted":
            assert cfg.script is not None
            return ScriptedProvider.from_yaml(self.resolve_path(cfg.script))
        if cfg.kind == "openai_compat":
            assert cfg.base_url is not None
            return OpenAICompatProvider(
                base_url=cfg.base_url,
                access=access,
                timeout=cfg.timeout_s,
                output_limit_field=cfg.output_limit_field,
            )
        return AnthropicProvider(
            access=access,
            base_url=cfg.base_url or "https://api.anthropic.com",
            timeout=cfg.timeout_s,
        )


class _StrictLoader(yaml.SafeLoader):
    # A repeated key would quietly replace the first one, so a file could read one way and load
    # another (`decision: require_approval` followed by `decision: allow`). Aliases are refused
    # too: config doesn't need them, and a few nested ones expand into gigabytes.

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            mark = self.peek_event().start_mark  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(None, None, "aliases aren't allowed", mark)
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                continue  # unhashable key; the base class reports it
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        # PyYAML's own message quotes the offending line, which may hold a pasted secret
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "syntax error"
        raise ConfigError(f"{path} is not valid YAML{where}: {problem}") from exc
    except ValueError as exc:
        # e.g. an integer with thousands of digits
        raise ConfigError(f"{path} has a value that can't be read: {exc}") from exc


def _explain(path: Path, exc: ValidationError) -> ConfigError:
    lines = [
        f"  {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in exc.errors()[:10]
    ]
    return ConfigError(f"{path} is invalid:\n" + "\n".join(lines))


def load_config(path: Path) -> Loaded:
    raw = _read_yaml(path)
    try:
        config = LegionConfig.model_validate(raw)
    except ValidationError as exc:
        raise _explain(path, exc) from exc
    for cap in config.authority.grantable:
        try:
            Capability.parse(cap)
        except ValueError as exc:
            raise ConfigError(f"{path}: invalid grantable capability {cap!r}") from exc
    check_servers(config.mcp_servers)
    try:
        config_hash = digest(raw)
    except ValueError as exc:
        # infinity or NaN somewhere pydantic doesn't look, like model options
        raise ConfigError(f"{path} has a value that can't be recorded: {exc}") from exc
    return Loaded(path=path.resolve(), config=config, config_hash=config_hash)


def load_agent(path: Path) -> AgentSpec:
    raw = _read_yaml(path)
    try:
        return AgentSpec.model_validate(raw)
    except ValidationError as exc:
        raise _explain(path, exc) from exc


def load_authority_module(path: Path, name: str) -> CredentialAuthority:
    # Runs the file, like a tool module: it's operator code.
    if not path.is_file():
        raise ConfigError(f"credential authority module not found: {path}")
    spec = importlib.util.spec_from_file_location(f"legion_authority_{name}", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot import credential authority module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    authority = getattr(module, "AUTHORITY", None)
    if authority is None or not callable(getattr(authority, "issue", None)):
        raise ConfigError(f"{path} must define AUTHORITY with an issue() method")
    if getattr(authority, "name", None) != name:
        raise ConfigError(
            f"{path} defines authority {getattr(authority, 'name', None)!r}, not {name!r}"
        )
    return authority  # type: ignore[no-any-return]
