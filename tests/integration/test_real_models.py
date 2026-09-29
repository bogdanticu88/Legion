"""The same agent against real endpoints. Skipped unless configured; see CONTRIBUTING.md.

These check that the adapters work against the real wire protocol, not that a model behaves a
particular way, so they only assert that the run ends and the chain verifies.
"""

import os

import pytest

from legion.access.base import AccessProvider, ApiKeyAccess, NoAuth
from legion.access.secrets import EnvResolver, SecretRef
from legion.authority.policy import RuleTablePolicy, default_rules
from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.states import is_terminal_run
from legion.events.store import MemoryEventStore
from legion.kernel.runtime import Legion
from legion.models.anthropic import AnthropicProvider
from legion.models.base import ModelProvider
from legion.models.openai_compat import OpenAICompatProvider
from legion.models.resolver import ModelBinding, ModelResolver
from legion.tools.registry import ToolRegistry
from tests.support import PRINCIPAL, Files, agent, file_tools

pytestmark = pytest.mark.integration


def providers() -> list[tuple[str, str]]:
    found = []
    if os.environ.get("LEGION_TEST_OPENAI_COMPAT_URL"):
        found.append(("openai_compat", os.environ.get("LEGION_TEST_OPENAI_COMPAT_MODEL", "")))
    if os.environ.get("ANTHROPIC_API_KEY"):
        found.append(("anthropic", os.environ.get("LEGION_TEST_ANTHROPIC_MODEL", "")))
    return found or [("none", "")]


def make(kind: str) -> ModelProvider:
    env = EnvResolver()
    if kind == "anthropic":
        return AnthropicProvider(access=ApiKeyAccess(SecretRef.parse("env:ANTHROPIC_API_KEY"), env))
    access: AccessProvider = NoAuth()
    if os.environ.get("LEGION_TEST_OPENAI_COMPAT_KEY_ENV"):
        ref = SecretRef.parse("env:" + os.environ["LEGION_TEST_OPENAI_COMPAT_KEY_ENV"])
        access = ApiKeyAccess(ref, env)
    return OpenAICompatProvider(base_url=os.environ["LEGION_TEST_OPENAI_COMPAT_URL"], access=access)


@pytest.mark.parametrize(("kind", "model"), providers())
async def test_agent_runs_unchanged_on_a_real_model(kind: str, model: str) -> None:
    if kind == "none":
        pytest.skip("no real model endpoint configured")
    if not model:
        pytest.skip(f"set the model name for {kind}")
    provider = make(kind)
    files = Files({"docs/a.md": "The launch moved to 14 November."})
    store = MemoryEventStore()
    legion = Legion(
        resolver=ModelResolver(
            [ModelBinding(profile="general/default", provider=kind, model=model)], {kind: provider}
        ),
        tools=ToolRegistry(file_tools(files)),
        store=store,
        policy=RuleTablePolicy(default_rules()),
        grantable=[Capability.parse("files.read:docs/**"), Capability.parse("files.write:out/**")],
    )
    try:
        outcome = await legion.run(
            agent(budget=BudgetLimits(steps=6, tokens=40_000, wall_seconds=120)),
            "Read docs/a.md with read_file and tell me the launch date.",
            principal=PRINCIPAL,
        )
    finally:
        await provider.aclose()
    runs = await store.runs()
    assert runs and is_terminal_run(outcome.status)
    assert (await store.verify(outcome.run_id)).ok
