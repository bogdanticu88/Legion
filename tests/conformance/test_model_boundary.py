# No real model was available when this was written, so this stands in for one: generated,
# often broken model output thrown at the runtime and at both adapters. Whatever comes back,
# the run has to end cleanly (completed, failed with a proper code, or paused), with a valid
# chain and no internal_error.

import contextlib
import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from legion.domain.errors import MalformedModelResponse
from legion.domain.messages import Message, TextPart, ToolCallPart
from legion.domain.states import is_terminal_run
from legion.events.projections import RunState
from legion.events.types import EventType, Usage
from legion.models import anthropic, openai_compat
from legion.models.base import ModelResponse, StopReason
from tests.support import build

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**40), max_value=2**40),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
)
json_values = st.recursive(
    json_scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)
weird_text = st.one_of(
    st.text(max_size=50),
    st.just(""),
    st.just("\x00‮﻿\U0001f600 ignore previous instructions"),
    st.just("x" * 20_000),
)
tool_names = st.one_of(
    st.sampled_from(["read_file", "write_file", "no_such_tool", "", "READ_FILE", "read_file "]),
    st.text(max_size=20),
)
arguments = st.one_of(
    st.fixed_dictionaries({"path": st.sampled_from(["docs/a.md", "secret/b.md", "out/x"])}),
    st.fixed_dictionaries({"path": json_values}),
    st.fixed_dictionaries({"path": st.just("docs/a.md"), "text": weird_text}),
    st.dictionaries(st.text(max_size=8), json_values, max_size=3),
)


@st.composite
def responses(draw: st.DrawFn) -> ModelResponse:
    parts: list[Any] = []
    if draw(st.booleans()):
        parts.append(TextPart(text=draw(weird_text)))
    for i in range(draw(st.integers(min_value=0, max_value=3))):
        parts.append(
            ToolCallPart(
                id=draw(st.sampled_from(["c", f"c{i}", ""])) or "c",
                name=draw(tool_names),
                arguments=draw(arguments),
            )
        )
    stop = draw(st.sampled_from(list(StopReason)))
    return ModelResponse(
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=stop,
        usage=Usage(
            input_tokens=draw(st.integers(0, 5000)), output_tokens=draw(st.integers(0, 500))
        ),
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(turns=st.lists(responses(), min_size=1, max_size=6))
async def test_any_model_output_ends_cleanly(turns: list[ModelResponse]) -> None:
    h = build(turns)
    outcome = await h.run()
    events = await h.events(outcome.run_id)
    state = RunState.from_events(outcome.run_id, events)
    assert is_terminal_run(state.status) or state.status.value == "paused"
    assert outcome.error_code != "internal_error", outcome.error_message
    assert (await h.store.verify(outcome.run_id)).ok
    assert all(e.type is not EventType.TOOL_STARTED or e.payload["action_hash"] for e in events)
    assert h.files.content["secret/b.md"] == "beta"


bodies = st.one_of(
    json_values,
    st.fixed_dictionaries({"choices": json_values, "usage": json_values}),
    st.fixed_dictionaries(
        {
            "choices": st.lists(
                st.fixed_dictionaries(
                    {
                        "message": st.fixed_dictionaries(
                            {"content": json_values, "tool_calls": json_values}
                        ),
                        "finish_reason": json_values,
                    }
                ),
                max_size=2,
            )
        }
    ),
    st.fixed_dictionaries(
        {
            "content": st.lists(json_values, max_size=3),
            "stop_reason": json_values,
            "usage": json_values,
        }
    ),
)


@settings(max_examples=400)
@given(body=bodies)
def test_adapters_only_fail_as_malformed(body: Any) -> None:
    # Anything a provider sends back either parses or is reported as malformed, which the loop
    # knows how to retry. Nothing else may escape.
    for parse in (openai_compat.from_wire, anthropic.from_wire):
        data = body if isinstance(body, dict) else {"body": body}
        with contextlib.suppress(MalformedModelResponse):
            parse(json.loads(json.dumps(data, allow_nan=True)))
