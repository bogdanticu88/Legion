from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from legion.access.base import AccessProvider, ApiKeyAccess, NoAuth
from legion.access.secrets import EnvResolver, SecretRef
from legion.artifacts import FileArtifactStore
from legion.authority.policy import Rule, RuleTablePolicy, Verdict, default_rules
from legion.canonical import digest
from legion.domain.agent import AgentSpec
from legion.domain.capability import Capability
from legion.domain.errors import ConfigError
from legion.events.sqlite_store import SqliteEventStore
from legion.kernel.locks import FileRunLocks
from legion.kernel.runtime import Legion
from legion.kernel.services import RetryPolicy
from legion.models.anthropic import AnthropicProvider
from legion.models.base import ModelProvider
from legion.models.openai_compat import OpenAICompatProvider
from legion.models.resolver import ModelBinding, ModelResolver
from legion.models.scripted import ScriptedProvider
from legion.tools.registry import ToolRegistry, load_tool_module


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccessConfig(_Strict):
    kind: Literal["none", "api_key"] = "none"
    secret: str | None = None

    @model_validator(mode="after")
    def _secret(self) -> AccessConfig:
        if self.kind == "api_key":
            if self.secret is None:
                raise ValueError("api_key access needs `secret: env:NAME`")
            SecretRef.parse(self.secret)
        return self


class ProviderConfig(_Strict):
    kind: Literal["scripted", "openai_compat", "anthropic"]
    base_url: str | None = None
    access: AccessConfig = AccessConfig()
    script: str | None = None
    timeout_s: float = Field(default=120.0, gt=0)
    output_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

    @model_validator(mode="after")
    def _fields(self) -> ProviderConfig:
        if self.kind == "scripted" and not self.script:
            raise ValueError("scripted provider needs `script`")
        if self.kind == "openai_compat" and not self.base_url:
            raise ValueError("openai_compat provider needs `base_url`")
        return self


class AuthorityConfig(_Strict):
    grantable: list[str] = Field(default_factory=list)


class PolicyConfig(_Strict):
    default: Verdict = Verdict.ALLOW
    rules: list[Rule] = Field(default_factory=default_rules)


class RetryConfig(_Strict):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=30.0, ge=0)


class ApprovalConfig(_Strict):
    ttl_seconds: int = Field(default=3600, gt=0, le=7 * 24 * 3600)


class LegionConfig(_Strict):
    version: Literal[1]
    store: str = ".legion/legion.db"
    artifacts: str = ".legion/artifacts"
    tool_modules: list[str] = Field(default_factory=list)
    tool_settings: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig]
    models: list[ModelBinding]
    authority: AuthorityConfig = AuthorityConfig()
    policy: PolicyConfig = PolicyConfig()
    credentials: dict[str, str] = Field(default_factory=dict)
    retry: RetryConfig = RetryConfig()
    approvals: ApprovalConfig = ApprovalConfig()


@dataclass
class Loaded:
    path: Path
    config: LegionConfig
    config_hash: str
    providers: dict[str, ModelProvider] = field(default_factory=dict)

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

    def build(self, store: SqliteEventStore | None = None) -> Legion:
        env = EnvResolver()
        self.providers = {name: self._provider(p, env) for name, p in self.config.providers.items()}
        tools = ToolRegistry()
        for module in self.config.tool_modules:
            for tool in load_tool_module(self.resolve_path(module)):
                tools.register(tool)
        return Legion(
            resolver=ModelResolver(self.config.models, self.providers),
            tools=tools,
            store=store or self.store(),
            policy=RuleTablePolicy(self.config.policy.rules, self.config.policy.default),
            grantable=[Capability.parse(c) for c in self.config.authority.grantable],
            credentials=env,
            credential_bindings={k: SecretRef.parse(v) for k, v in self.config.credentials.items()},
            artifacts=FileArtifactStore(self.resolve_path(self.config.artifacts)),
            settings={**self.config.tool_settings, "config_dir": str(self.root)},
            retry=RetryPolicy(**self.config.retry.model_dump()),
            config_hash=self.config_hash,
            locks=self.locks(),
            approval_ttl=timedelta(seconds=self.config.approvals.ttl_seconds),
        )

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()

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


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc


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
    return Loaded(path=path.resolve(), config=config, config_hash=digest(raw))


def load_agent(path: Path) -> AgentSpec:
    raw = _read_yaml(path)
    try:
        return AgentSpec.model_validate(raw)
    except ValidationError as exc:
        raise _explain(path, exc) from exc
