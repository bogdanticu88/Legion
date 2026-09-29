import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from legion.access.base import ApiKeyAccess, NoAuth
from legion.access.secrets import EnvResolver, SecretRef
from legion.authority.policy import RuleTablePolicy, default_rules
from legion.domain.agent import ModelFeature, ModelRequirement
from legion.domain.capability import Capability
from legion.domain.errors import (
    ConfigError,
    ContextExhausted,
    MalformedModelResponse,
    ModelAuthError,
    ModelRequestRejected,
    ModelUnavailable,
    RateLimited,
)
from legion.domain.messages import (
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    user_text,
)
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.kernel.runtime import Legion
from legion.models.anthropic import AnthropicProvider
from legion.models.base import ModelRequest, StopReason, ToolDefinition
from legion.models.openai_compat import OpenAICompatProvider
from legion.models.resolver import ModelBinding, ModelResolver
from legion.models.scripted import ScriptedProvider
from legion.tools.registry import ToolRegistry
from tests.support import PRINCIPAL, Files, agent, file_tools

KEY = "sk-ant-test-KEY-9d8c7b"


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)  # type: ignore[no-any-return]


def client(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


def openai(recorder: Recorder) -> OpenAICompatProvider:
    access = ApiKeyAccess(SecretRef.parse("env:OAI"), EnvResolver({"OAI": "sk-oai"}))
    return OpenAICompatProvider(base_url="http://gw/v1/", access=access, client=client(recorder))


def anthropic(recorder: Recorder) -> AnthropicProvider:
    access = ApiKeyAccess(SecretRef.parse("env:ANT"), EnvResolver({"ANT": KEY}))
    return AnthropicProvider(access=access, base_url="http://ant", client=client(recorder))


TOOL = ToolDefinition(name="read_file", description="Read", input_schema={"type": "object"})

CONVERSATION = (
    user_text("hi"),
    Message(
        role="assistant",
        parts=(
            ReasoningPart(
                provider="anthropic", data={"type": "thinking", "thinking": "hm", "signature": "s"}
            ),
            TextPart(text="reading"),
            ToolCallPart(id="t1", name="read_file", arguments={"path": "a"}),
            ToolCallPart(id="t2", name="read_file", arguments={"path": "b"}),
        ),
    ),
    Message(role="tool", parts=(ToolResultPart(call_id="t1", content="A"),)),
    Message(role="tool", parts=(ToolResultPart(call_id="t2", content="no", is_error=True),)),
)


def request(**kw: Any) -> ModelRequest:
    base: dict[str, Any] = {
        "model": "m",
        "system": "sys",
        "messages": CONVERSATION,
        "tools": (TOOL,),
        "max_output_tokens": 100,
    }
    base.update(kw)
    return ModelRequest(**base)


class TestOpenAICompat:
    async def test_request_shape(self) -> None:
        rec = Recorder(
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 10},
                    },
                },
            )
        )
        response = await openai(rec).generate(request())
        sent = rec.requests[0]
        assert str(sent.url) == "http://gw/v1/chat/completions"
        assert sent.headers["authorization"] == "Bearer sk-oai"
        body = rec.body()
        assert body["max_tokens"] == 100
        assert body["tools"][0]["function"]["name"] == "read_file"
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "tool"]
        assistant = body["messages"][2]
        assert assistant["content"] == "reading"
        assert json.loads(assistant["tool_calls"][1]["function"]["arguments"]) == {"path": "b"}
        assert "thinking" not in json.dumps(body)
        assert response.message.text == "done"
        assert response.stop_reason is StopReason.END
        assert (response.usage.input_tokens, response.usage.cache_read_tokens) == (20, 10)

    async def test_tool_calls_parsed(self) -> None:
        rec = Recorder(
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "x1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path": "a"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        )
        response = await openai(rec).generate(request())
        [call] = response.message.tool_calls
        assert (call.id, call.arguments) == ("x1", {"path": "a"})
        assert response.stop_reason is StopReason.TOOL_USE

    async def test_bad_tool_arguments_are_malformed(self) -> None:
        rec = Recorder(
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "x",
                                        "function": {"name": "read_file", "arguments": "{nope"},
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        )
        with pytest.raises(MalformedModelResponse):
            await openai(rec).generate(request())

    async def test_non_finite_numbers_are_malformed(self) -> None:
        body = '{"choices": [{"message": {"tool_calls": [{"id": "x", "function": {"name": "t", "arguments": "{\\"a\\": NaN}"}}]}}]}'
        rec = Recorder(httpx.Response(200, text=body))
        with pytest.raises(MalformedModelResponse):
            await openai(rec).generate(request())
        rec = Recorder(httpx.Response(200, text='{"content": [], "x": Infinity}'))
        with pytest.raises(MalformedModelResponse):
            await anthropic(rec).generate(request())

    async def test_structured_output_and_options(self) -> None:
        rec = Recorder(httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
        await openai(rec).generate(
            request(
                response_schema={"type": "object"},
                provider_options={"openai_compat": {"temperature": 0}, "anthropic": {"top_k": 3}},
            )
        )
        body = rec.body()
        assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
        assert body["temperature"] == 0
        assert "top_k" not in body

    async def test_options_cannot_override_core_fields(self) -> None:
        rec = Recorder()
        with pytest.raises(ConfigError):
            await openai(rec).generate(request(provider_options={"openai_compat": {"model": "x"}}))
        assert rec.requests == []

    async def test_completion_tokens_field(self) -> None:
        rec = Recorder(httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))
        provider = OpenAICompatProvider(
            base_url="http://gw",
            access=NoAuth(),
            client=client(rec),
            output_limit_field="max_completion_tokens",
        )
        await provider.generate(request())
        assert "max_completion_tokens" in rec.body() and "max_tokens" not in rec.body()
        assert "authorization" not in rec.requests[0].headers


class TestAnthropic:
    async def test_request_shape(self) -> None:
        rec = Recorder(
            httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "thinking", "thinking": "plan", "signature": "sig"},
                        {"type": "text", "text": "ok"},
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "read_file",
                            "input": {"path": "c"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 7,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 4,
                    },
                },
            )
        )
        response = await anthropic(rec).generate(request())
        sent = rec.requests[0]
        assert str(sent.url) == "http://ant/v1/messages"
        assert sent.headers["x-api-key"] == KEY
        assert sent.headers["anthropic-version"] == "2023-06-01"
        body = rec.body()
        assert body["system"] == "sys"
        assert body["tools"][0]["input_schema"] == {"type": "object"}
        assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
        assistant = body["messages"][1]["content"]
        assert assistant[0] == {"type": "thinking", "thinking": "hm", "signature": "s"}
        results = body["messages"][2]["content"]
        assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
        assert results[1]["is_error"] is True

        assert response.stop_reason is StopReason.TOOL_USE
        assert response.message.tool_calls[0].arguments == {"path": "c"}
        assert isinstance(response.message.parts[0], ReasoningPart)
        assert response.usage.total == 26

    async def test_foreign_reasoning_dropped(self) -> None:
        rec = Recorder(httpx.Response(200, json={"content": [], "stop_reason": "end_turn"}))
        foreign = Message(
            role="assistant",
            parts=(ReasoningPart(provider="other", data={"type": "x"}), TextPart(text="t")),
        )
        await anthropic(rec).generate(request(messages=(user_text("hi"), foreign)))
        assert rec.body()["messages"][1]["content"] == [{"type": "text", "text": "t"}]

    @pytest.mark.parametrize(
        ("status", "payload", "headers", "error"),
        [
            (
                401,
                {"type": "error", "error": {"type": "authentication_error", "message": "x"}},
                {},
                ModelAuthError,
            ),
            (
                429,
                {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}},
                {"retry-after": "7"},
                RateLimited,
            ),
            (
                529,
                {"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
                {},
                ModelUnavailable,
            ),
            (
                400,
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "prompt is too long: 300000 tokens",
                    },
                },
                {},
                ContextExhausted,
            ),
            (
                400,
                {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}},
                {},
                ModelRequestRejected,
            ),
        ],
    )
    async def test_errors_map_to_dispositions(
        self, status: int, payload: dict[str, Any], headers: dict[str, str], error: type
    ) -> None:
        rec = Recorder(httpx.Response(status, json=payload, headers=headers))
        with pytest.raises(error) as info:
            await anthropic(rec).generate(request())
        if error is RateLimited:
            assert info.value.retry_after == 7.0

    async def test_transport_failure_is_unavailable(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        provider = AnthropicProvider(
            access=NoAuth(), client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
        )
        with pytest.raises(ModelUnavailable):
            await provider.generate(request())

    async def test_non_json_body_is_malformed(self) -> None:
        rec = Recorder(httpx.Response(200, text="<html>"))
        with pytest.raises(MalformedModelResponse):
            await anthropic(rec).generate(request())


async def test_api_key_not_logged(tmp_path: Path) -> None:
    rec = Recorder(
        httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "a",
                        "name": "read_file",
                        "input": {"path": "docs/a.md"},
                    }
                ],
                "stop_reason": "tool_use",
            },
        ),
        httpx.Response(
            200, json={"content": [{"type": "text", "text": "fine"}], "stop_reason": "end_turn"}
        ),
    )
    store = SqliteEventStore(tmp_path / "e.db")
    legion = Legion(
        resolver=ModelResolver(
            [ModelBinding(profile="general/default", provider="ant", model="claude")],
            {"ant": anthropic(rec)},
        ),
        tools=ToolRegistry(file_tools(Files({"docs/a.md": "alpha"}))),
        store=store,
        policy=RuleTablePolicy(default_rules()),
        grantable=[Capability.parse("files.read:**"), Capability.parse("files.write:**")],
    )
    outcome = await legion.run(agent(), "read it", principal=PRINCIPAL)
    assert outcome.status is RunStatus.COMPLETED
    assert all(r.headers["x-api-key"] == KEY for r in rec.requests)
    store.close()
    for path in tmp_path.iterdir():
        assert KEY.encode() not in path.read_bytes()


class TestResolver:
    def test_resolve(self) -> None:
        script = ScriptedProvider([])
        rec = Recorder()
        resolver = ModelResolver(
            [
                ModelBinding(profile="p", provider="oai", model="small", features=frozenset()),
                ModelBinding(
                    profile="p",
                    provider="oai",
                    model="big",
                    features=frozenset({ModelFeature.TOOLS, ModelFeature.REASONING}),
                ),
                ModelBinding(profile="q", provider="script", model="s"),
            ],
            {"oai": openai(rec), "script": script},
        )
        resolved = resolver.resolve(ModelRequirement(profile="p"))
        assert resolved.binding.model == "big"
        # The binding claims reasoning, but the OpenAI-compatible adapter cannot use it.
        assert ModelFeature.REASONING not in resolved.features
        with pytest.raises(ConfigError, match="reasoning"):
            resolver.resolve(
                ModelRequirement(profile="p", needs=frozenset({ModelFeature.REASONING}))
            )
        with pytest.raises(ConfigError, match="no model"):
            resolver.resolve(ModelRequirement(profile="missing"))

    def test_unknown_provider(self) -> None:
        with pytest.raises(ConfigError):
            ModelResolver([ModelBinding(profile="p", provider="nope", model="m")], {})
