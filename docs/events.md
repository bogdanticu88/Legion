# Events

Schema version 1. Run status, the transcript and budget use are all rebuilt from these
(`legion.events.projections.RunState`). To read them from outside, use `legion inspect --json` or
the `EventStore` interface.

These are Legion's own record of what it did. Editing one breaks the chain, but someone who owns
the host can rewrite the whole thing, so treat them as a claim to check against other sources.

## Envelope

| Field | |
|---|---|
| `event_id` | random hex id |
| `schema_version` | `1` |
| `run_id` | |
| `seq` | starts at 1, no gaps within a run |
| `ts` | UTC, when the event was created |
| `type` | see below |
| `task_id`, `agent_id`, `parent_task_id` | set on task events |
| `correlation` | extra keys; `action_hash` on action and tool events once there's an Action |
| `payload` | depends on type |
| `prev_hash` | previous event's `hash`, or 64 zeros for the first one |
| `hash` | `sha256(prev_hash + canonical_json(everything except hash))` |

Canonical JSON means sorted keys, `(",", ":")` separators, UTF-8, no NaN, timestamps ending in `Z`
and decimals as strings.

## Ordering you can rely on

- A run starts with `run.created`, `task.created`, `run.started`.
- While the run keeps going, each tool call from the model ends with one of `tool.completed`,
  `tool.failed` (with `will_retry: false`) or `action.refused`. If a fatal error ends the run in
  the middle of a call (loop, expired grant, missing credential, budget, kill), the call doesn't
  get its own ending. `task.failed` covers it, preceded by `action.in_doubt` if the tool had
  started.
- `tool.started` always comes after an `action.authorized` with the same `action_hash` and a
  `budget.consumed` for `tool_calls`.
- `model.requested` always comes after a `budget.consumed` for `model_calls`.
- When a run fails or is cancelled, every action that started and didn't finish gets an
  `action.in_doubt` before `task.failed` or `task.cancelled`. On `internal_error` this is best
  effort.
- `tool.completed` with `is_error: true` still means the tool ran. If its output failed the tool's
  output schema, the output is left out and the content says the action took effect.
- The last event is `run.completed`, `run.failed` or `run.cancelled`.

## Types

| Type | Payload | When |
|---|---|---|
| `run.created` | `agent`, `agent_spec`, `agent_spec_hash`, `provider`, `model`, `profile`, `grant`, `root_task_id`, `config_hash` | run starts; `grant` is the full root grant |
| `run.started` | | after the root task is created |
| `run.completed` | `output` | root task finished |
| `run.failed` | `error_code`, `message`, `disposition` | a fatal error ended the run |
| `run.cancelled` | `reason` | the asyncio task was cancelled |
| `task.created` | `task`, `grant_id`, `agent` | `task` is the full spec |
| `task.started` | | loop begins |
| `task.completed` | `output`, `structured`, `truncated` | model answered without tool calls and the answer was accepted |
| `task.failed` | `error_code`, `message`, `disposition` | |
| `task.cancelled` | `reason` | |
| `model.requested` | `attempt`, `provider`, `model`, `message_count`, `tool_names`, `request_hash`, `max_output_tokens` | before each attempt. The request itself isn't stored since it comes from the transcript; the hash is there so resume (Phase 2) can check it rebuilt the same one |
| `model.responded` | `attempt`, `message`, `stop_reason`, `usage`, `cost_usd`, `latency_ms` | `message` is stored in full |
| `model.failed` | `attempt`, `error_code`, `message`, `disposition`, `will_retry`, `retry_in_ms` | |
| `action.proposed` | `call_id`, `tool`, `arguments`, `action_hash`, `effect`, `resource`, `required` | the call passed lookup and schema checks |
| `action.refused` | `call_id`, `tool`, `action_hash`, `reason_code`, `message` | the model gets `message`. `action_hash` is null if the call was refused before it became an Action |
| `action.repeated` | `call_id`, `tool`, `repeat_key`, `count` | third or later identical call |
| `action.authorized` | `call_id`, `action_hash`, `reasons` | grant, policy and external check all passed |
| `action.in_doubt` | `call_id`, `action_hash`, `effect`, `reason` | started, outcome unknown |
| `tool.started` | `call_id`, `action_hash`, `attempt` | |
| `tool.completed` | `call_id`, `action_hash`, `content`, `is_error`, `artifact`, `truncated`, `redactions`, `latency_ms` | `content` is exactly what the model sees |
| `tool.failed` | `call_id`, `action_hash`, `attempt`, `error_code`, `message`, `disposition`, `will_retry` | |
| `budget.consumed` | `grant_id`, `dimension`, `amount`, `total` | anything charged |
| `budget.exceeded` | `grant_id`, `dimension`, `limit`, `attempted` | a limit stopped the run |
| `output.rejected` | `attempt`, `reason` | final answer didn't match the agent's output schema |

## Codes

Refusals: `unknown_tool`, `tool_not_offered`, `invalid_arguments`, `capability_denied`,
`policy_denied`, `approval_unavailable`, `repeated_action`.

Fatal: `config_error`, `no_model_binding`, `budget_exceeded`, `deadline_exceeded`, `killed`,
`loop_detected`, `action_in_doubt`, `credential_unavailable`, `grant_expired`,
`model_auth_error`, `model_request_rejected`, `context_exhausted`, `final_output_invalid`,
`invalid_transition`, `concurrent_append`, `internal_error`.

Retried, then fatal if they keep happening: `model_timeout`, `model_unavailable`, `rate_limited`,
`malformed_model_response`. A run that fails on one of these records `disposition: retryable`, which
tells you it was retried first.

Tool errors the model gets told about: `tool_failed`, `tool_timeout`, `tool_retryable`.
`invalid_tool_output` shows up inside `tool.completed` content.
