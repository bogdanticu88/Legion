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
  `tool.failed` (with `will_retry: false`), `action.refused` or `action.reconciled`. If a fatal
  error ends the run in the middle of a call (loop, expired grant, missing credential, budget,
  kill), the call doesn't get its own ending. `task.failed` covers it, preceded by
  `action.in_doubt` if the tool had started.
- A child's `task.created`, its `budget.reserved` events and the parent's `task.waiting` are
  written in one append. Its settlement and the parent's `task.resumed` come after the child's
  last event and before the parent's `tool.completed` for the delegate call.
- A call can be proposed more than once (after a pause or crash). It's still one call: same
  `call_id`, and repeat detection counts it once.
- For a call that needed approval, `approval.consumed` comes before its `action.authorized` and
  `tool.started`.
- A paused run ends with `run.paused`, right after `task.awaiting_approval` or `task.blocked`,
  written together in one append. A resumed run continues with `run.resumed`.
- Every active stretch ends with a `budget.consumed` for `wall_seconds`, except one that ends in
  `internal_error`. A stretch that ended in a crash is charged when the run is resumed.
- `tool.started` always comes after an `action.authorized` with the same `action_hash` and a
  `budget.consumed` for `tool_calls`.
- `model.requested` always comes after a `budget.consumed` for `model_calls`.
- When a run fails or is cancelled, every action that started and didn't finish gets an
  `action.in_doubt` before `task.failed` or `task.cancelled`. On `internal_error` this is best
  effort.
- `tool.completed` with `is_error: true` still means the tool ran. If its output failed the tool's
  output schema, the output is left out and the content says the action took effect.
- The last event is `run.completed`, `run.failed`, `run.cancelled`, or `run.paused` while the run
  waits for a person.

## Types

| Type | Payload | When |
|---|---|---|
| `run.created` | `agent`, `agent_spec`, `agent_spec_hash`, `provider`, `model`, `profile`, `grant`, `root_task_id`, `config_hash` | run starts; `grant` is the full root grant |
| `run.started` | | after the root task is created |
| `run.completed` | `output` | root task finished |
| `run.failed` | `error_code`, `message`, `disposition` | a fatal error ended the run |
| `run.cancelled` | `reason` | the asyncio task was cancelled |
| `task.created` | `task`, `grant_id`, `agent`, and for a child: `grant`, `agent_spec`, `provider`, `model`, `delegated_by` | `task` is the full spec. A child's `parent_task_id` is set and `delegated_by` is the parent's call id |
| `task.started` | | loop begins |
| `task.completed` | `output`, `structured`, `truncated` | model answered without tool calls and the answer was accepted |
| `task.failed` | `error_code`, `message`, `disposition` | |
| `task.cancelled` | `reason` | |
| `model.requested` | `attempt`, `provider`, `model`, `message_count`, `tool_names`, `request_hash`, `max_output_tokens` | before each attempt. The request itself isn't stored since it comes from the transcript; the hash lets you compare two requests. Resume doesn't use it |
| `model.responded` | `attempt`, `message`, `stop_reason`, `usage`, `cost_usd`, `latency_ms` | `message` is stored in full |
| `model.failed` | `attempt`, `error_code`, `message`, `disposition`, `will_retry`, `retry_in_ms` | |
| `action.proposed` | `call_id`, `tool`, `arguments`, `action_hash`, `effect`, `resource`, `required`, `remote` | the call passed lookup and schema checks. `remote` is null for native tools; for MCP it has `kind`, `server`, `server_fingerprint`, `remote_tool`, `pin`, `credential_scope` and, if the identity service reported one, `credential_evidence` |
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
| `task.waiting` | `child_task_id` | a parent is waiting for a child |
| `budget.reserved` | `grant_id`, `child_grant_id`, `child_task_id`, `dimension`, `amount` | a child's share, held from the parent while the child runs |
| `budget.settled` | `grant_id`, `child_grant_id`, `child_task_id`, `dimension`, `reserved`, `used` | the child ended; the reservation is released and `used` is charged to the parent |
| `run.paused` | `reason` (`approval` or `reconciliation`), `approval_id`, `call_id` | the run is waiting for a person |
| `run.resumed` | `by`, `previous_status`, `config_hash` | `legion resume`; `previous_status` is `running` if the process had crashed |
| `task.awaiting_approval` | `approval_id` | |
| `task.blocked` | `call_id`, `reason` | an action is in doubt |
| `task.resumed` | | |
| `action.interrupted` | `call_id`, `action_hash`, `effect` | a safe-to-repeat call was running when the process stopped; it will run again |
| `action.reconciled` | `call_id`, `action_hash`, `outcome` (`applied` or `not_applied`), `by`, `note` | an operator said what happened to an in-doubt action |
| `approval.requested` | `approval_id`, `call_id`, `action_hash`, `binding_hash`, `subject`, `expires_at` | `subject` is what the approver is shown; `binding_hash` is what is enforced |
| `approval.granted`, `approval.denied` | `approval_id`, `by`, `note` | |
| `approval.expired` | `approval_id` | noticed at use, at resume, or when someone tried to decide |
| `approval.consumed` | `approval_id`, `call_id` | just before the approved call runs |
| `approval.invalidated` | `approval_id`, `reason` | the call no longer matched what was approved |

## Codes

Refusals: `unknown_tool`, `tool_not_offered`, `invalid_arguments`, `capability_denied`,
`policy_denied`, `approval_denied`, `approval_expired`, `approval_mismatch`, `approval_reused`,
`delegation_refused`, `repeated_action`.

Pausing (`disposition: escalate`): `approval_required`, `action_in_doubt`.

Fatal: `config_error`, `no_model_binding`, `budget_exceeded`, `deadline_exceeded`, `killed`,
`loop_detected`, `credential_unavailable`, `grant_expired`, `abandoned`,
`model_auth_error`, `model_request_rejected`, `context_exhausted`, `final_output_invalid`,
`invalid_transition`, `concurrent_append`, `internal_error`.

Retried, then fatal if they keep happening: `model_timeout`, `model_unavailable`, `rate_limited`,
`malformed_model_response`. A run that fails on one of these records `disposition: retryable`, which
tells you it was retried first.

Tool errors the model gets told about: `tool_failed`, `tool_timeout`, `tool_retryable`.
`invalid_tool_output` shows up inside `tool.completed` content.
