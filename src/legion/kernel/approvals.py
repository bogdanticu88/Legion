from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from legion.canonical import digest
from legion.domain.action import Action
from legion.domain.messages import ToolCallPart
from legion.kernel.services import TaskRuntime
from legion.tools.base import ToolSpec

MODEL_NOTE_LIMIT = 500


def new_approval_id() -> str:
    return f"apr_{uuid.uuid4().hex[:16]}"


def binding(
    *,
    task: TaskRuntime,
    call: ToolCallPart,
    action: Action,
    tool: ToolSpec,
    settings: Mapping[str, str],
    credential_refs: Mapping[str, str],
) -> dict[str, Any]:
    # Everything that decides what this call will actually do. If any of it differs when the call
    # is about to run, the approval no longer applies. Timestamps, event ids and the model's
    # wording are left out on purpose: they change without changing the effect.
    return {
        "run_id": task.run_id,
        "task_id": task.task_id,
        "call_id": call.id,
        "agent": task.agent.name,
        "agent_spec_hash": task.agent.spec_hash,
        "tool": action.tool,
        "tool_spec": tool.model_dump(mode="json"),
        "arguments": action.arguments,
        "resource": action.resource,
        "effect": action.effect.value,
        "required": [str(c) for c in action.required],
        "grant": task.grant.model_dump(mode="json"),
        "identity": {
            "agent_ref": task.identity.agent_ref,
            "source": task.identity.source,
            "external_id": task.identity.external_id,
        },
        "settings": dict(settings),
        # references only, never values
        "credentials": {
            name: credential_refs[name] for name in tool.credentials if name in credential_refs
        },
    }


def binding_hash(bound: dict[str, Any]) -> str:
    return digest(bound)


def subject(
    *,
    task: TaskRuntime,
    call: ToolCallPart,
    action: Action,
    tool: ToolSpec,
    objective: str,
    model_note: str,
) -> dict[str, Any]:
    # What the approver reads. It is built from the same values as the binding, but it is only
    # for display; enforcement uses the binding hash.
    return {
        "run_id": task.run_id,
        "task_id": task.task_id,
        "call_id": call.id,
        "agent": task.agent.name,
        "on_behalf_of": list(task.grant.identity.on_behalf_of),
        "objective": objective[:300],
        "tool": action.tool,
        "description": tool.description,
        "effect": action.effect.value,
        "resource": action.resource,
        "arguments": action.arguments,
        "required": [str(c) for c in action.required],
        "model_note": model_note[:MODEL_NOTE_LIMIT],
        # for a remote tool: which server runs it and what its own credentials can do
        "origin": tool.origin,
    }
