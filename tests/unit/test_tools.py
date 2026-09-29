import pytest
from pydantic import BaseModel, ValidationError

from legion.access.secrets import EnvResolver, Secret, SecretRef
from legion.domain.action import EffectClass
from legion.domain.errors import ConfigError, CredentialUnavailable, InvalidArguments
from legion.tools.base import ToolContext, ToolResult, ToolSpec
from legion.tools.native import tool
from legion.tools.registry import ToolRegistry

CTX = ToolContext(task_id="t", agent="a", call_id="c")


class Args(BaseModel):
    path: str
    limit: int = 10


async def test_native_tool_derives_schema_and_runs() -> None:
    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource_arg="path")
    async def read(args: Args, ctx: ToolContext) -> dict[str, object]:
        """Read something.

        Longer text that is not part of the description."""
        return {"path": args.path, "limit": args.limit}

    assert read.spec.description == "Read something."
    assert read.spec.input_schema["required"] == ["path"]
    assert read.resource_of({"path": "a/b"}) == "a/b"
    result = await read.invoke({"path": "x"}, CTX)
    assert result.data == {"path": "x", "limit": 10}
    assert result.for_model() == '{"limit": 10, "path": "x"}'


async def test_sync_tools_run_in_a_thread() -> None:
    @tool(effect=EffectClass.PURE)
    def add(args: Args, ctx: ToolContext) -> str:
        """Pure."""
        return args.path * 2

    assert (await add.invoke({"path": "ab"}, CTX)).content == "abab"


def test_resource_extraction_validates() -> None:
    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: a.path)
    async def read(args: Args, ctx: ToolContext) -> str:
        """Read."""
        return ""

    with pytest.raises(InvalidArguments):
        read.resource_of({"limit": 3})


@pytest.mark.parametrize(
    "kw",
    [
        {"name": "Bad-Name", "effect": EffectClass.PURE},
        {"name": "ok", "effect": EffectClass.WRITE},
        {"name": "ok", "effect": EffectClass.READ, "capabilities": ("files.*",)},
        {"name": "ok", "effect": EffectClass.READ, "capabilities": ("Files",)},
    ],
)
def test_spec_rejects_undeclared_or_bad_declarations(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ToolSpec(description="d", input_schema={"type": "object"}, **kw)  # type: ignore[arg-type]


def test_tool_signature_is_checked() -> None:
    with pytest.raises(TypeError):

        @tool(effect=EffectClass.PURE)
        def bad(args: dict, ctx: ToolContext) -> str:  # type: ignore[type-arg]
            return ""


def test_registry_refuses_duplicates() -> None:
    @tool(effect=EffectClass.PURE)
    def same(args: Args, ctx: ToolContext) -> str:
        """x"""
        return ""

    with pytest.raises(ConfigError):
        ToolRegistry([same, same])


def test_tool_result_rendering() -> None:
    assert ToolResult(content="a").for_model() == "a"
    assert ToolResult().for_model() == ""


async def test_secrets_do_not_print() -> None:
    secret = Secret("hunter2")
    assert "hunter2" not in repr(secret) and "hunter2" not in str(secret)
    assert "hunter2" not in f"{secret}"
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(secret)
    resolver = EnvResolver({"X": "v"})
    assert (await resolver.resolve(SecretRef.parse("env:X"))).reveal() == "v"
    with pytest.raises(CredentialUnavailable):
        await resolver.resolve(SecretRef.parse("env:MISSING"))
    with pytest.raises(ConfigError):
        SecretRef.parse("file:/etc/passwd")


def test_invalid_schemas_are_rejected_at_registration() -> None:
    with pytest.raises(ValidationError, match="invalid schema"):
        ToolSpec(
            name="t",
            description="d",
            effect=EffectClass.PURE,
            input_schema={"type": "object", "properties": {"x": {"type": "nonsense"}}},
        )
