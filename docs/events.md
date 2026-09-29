# Events

Schema version 1. Events are the source of truth for a run: status, transcript and budget are
rebuilt from them (`legion.events.projections.RunState`). Benchmarks and forensic tools should
read them through `legion inspect --json` or the `EventStore` interface.

Events are Legion's own account of what it did. They are chained so that editing one is
detectable, but whoever controls the host can rewrite a whole chain. Treat them as a
well-structured claim to be checked against other sources, not as ground truth.

## Envelope

| Field | Meaning |
|---|---|
| `event_id` | Random 128-bit hex id |
| `schema_version` | `1` |
| `run_id` | The run this event belongs to |
| `seq` | 1-based, gap-free within a run |
| `ts` | UTC timestamp, taken when the event was drafted |
| `type` | One of the types below |
| `task_id`, `agent_id`, `parent_task_id` | Set for task-scoped events |
| `correlation` | Extra keys; `action_hash` on every action and tool event that has an Action |
| `payload` | Type-specific, below |
| `prev_hash` | `hash` of the previous event in the run, or 64 zeros for the first |
| `hash` | `sha256(prev_hash + canonical_json(all fields except hash))` |

Canonical JSON: sorted keys, `(",", ":")` separators, UTF-8, no NaN, UTC timestamps ending in
`Z`, decimals as strings.

## Ordering guarantees

- `run.created`, `task.created`, `run.started` come first, in that order.
- In a run that continues, every tool call from the model ends in exactly one of:
  `tool.completed`, `tool.failed` with `will_retry: false`, or `action.refused`. When a fatal error
  ends the run in the middle of a call (loop detected, grant expired, missing credential, budget,
  kill), the call has no ending event of its own: the run's `task.failed` is its ending, and if
  the call had started, an `action.in_doubt` precedes it.
- `tool.started` is always preceded by `action.authorized` with the same `action_hash`, and by a
  `budget.consumed` for `tool_calls`.
- `model.requested` is always preceded by `budget.consumed` for `model_calls`.
- A failed or cancelled run emits `action.in_doubt` for every started, unfinished action before
  `task.failed` or `task.cancelled`, including on the `internal_error` path where possible.
- `tool.completed` with `is_error: true` means the tool ran. Output that failed the tool's
  output schema is withheld, and the content says the action took effect.
- The last event of a finished run is `run.completed`, `run.failed` or `run.cancelled`.

## Types

| Type | Payload | Emitted when |
|---|---|---|
| `run.created` | `agent`, `agent_spec`, `agent_spec_hash`, `provider`, `model`, `profile`, `grant`, `root_task_id`, `config_hash` | A run starts. `grant` is the full root grant |
| `run.started` | none | After the root task is created |
| `run.completed` | `output` | The root task completed |
| `run.failed` | `error_code`, `message`, `disposition` | A fatal error ended the run |
| `run.cancelled` | `reason` | The run's asyncio task was cancelled |
| `task.created` | `task`, `grant_id`, `agent` | A task exists; `task` is its full spec |
| `task.started` | none | The loop begins |
| `task.completed` | `output`, `structured`, `truncated` | The model answered without tool calls and the answer was accepted |
| `task.failed` | `error_code`, `message`, `disposition` | A fatal error in this task |
| `task.cancelled` | `reason` | Cancellation |
| `model.requested` | `attempt`, `provider`, `model`, `message_count`, `tool_names`, `request_hash`, `max_output_tokens` | Before each attempt. The request is not stored: it is derived from the transcript, and `request_hash` lets a resumed run confirm it rebuilt the same request (Phase 2) |
| `model.responded` | `attempt`, `message`, `stop_reason`, `usage`, `cost_usd`, `latency_ms` | A response arrived. `message` is stored in full |
| `model.failed` | `attempt`, `error_code`, `message`, `disposition`, `will_retry`, `retry_in_ms` | An attempt failed |
| `action.proposed` | `call_id`, `tool`, `arguments`, `action_hash`, `effect`, `resource`, `required` | A tool call passed lookup and schema validation and became an Action |
| `action.refused` | `call_id`, `tool`, `action_hash`, `reason_code`, `message` | Refused before execution. `action_hash` is null if refusal came before an Action existed. The model receives `message` |
| `action.repeated` | `call_id`, `tool`, `repeat_key`, `count` | The same tool and arguments were requested for the third time or more |
| `action.authorized` | `call_id`, `action_hash`, `reasons` | Grant, policy and external authority all allowed it |
| `action.in_doubt` | `call_id`, `action_hash`, `effect`, `reason` | A started action's outcome is unknown |
| `tool.started` | `call_id`, `action_hash`, `attempt` | Execution begins |
| `tool.completed` | `call_id`, `action_hash`, `content`, `is_error`, `artifact`, `truncated`, `redactions`, `latency_ms` | Execution finished. `content` is exactly what the model receives |
| `tool.failed` | `call_id`, `action_hash`, `attempt`, `error_code`, `message`, `disposition`, `will_retry` | Execution failed |
| `budget.consumed` | `grant_id`, `dimension`, `amount`, `total` | Anything was charged |
| `budget.exceeded` | `grant_id`, `dimension`, `limit`, `attempted` | A limit stopped the run |
| `output.rejected` | `attempt`, `reason` | A final answer failed the agent's output schema |

## Refusal codes

`unknown_tool`, `tool_not_offered`, `invalid_arguments`, `capability_denied`, `policy_denied`,
`approval_unavailable`, `repeated_action`.

## Error codes

Fatal: `config_error`, `budget_exceeded`, `deadline_exceeded`, `killed`, `loop_detected`,
`action_in_doubt`, `credential_unavailable`, `grant_expired`, `model_auth_error`,
`model_request_rejected`, `context_exhausted`, `final_output_invalid`, `internal_error`.

Retryable, and fatal once retries run out: `model_timeout`, `model_unavailable`, `rate_limited`,
`malformed_model_response`. When one of these ends a run, `run.failed` records the error's own
disposition, `retryable`, so the record shows it was retried and not refused outright.

Configuration and integrity errors that can also end a run: `no_model_binding`,
`invalid_transition`, `concurrent_append`.

Tool errors reported to the model: `tool_failed`, `tool_timeout`, `tool_retryable`,
`invalid_tool_output` (appears inside `tool.completed` content, see above).
