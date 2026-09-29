# Changelog

## Unreleased

### Phase 2

- `legion resume` continues a paused or crashed run from its event log, through the same loop and
  pipeline as a fresh run. Interrupted safe calls run again; interrupted writes are marked in
  doubt and wait for `legion reconcile`.
- Approvals: policy can require one, the run pauses, and `legion approvals`, `approval show`,
  `approve` and `deny` handle it. An approval is bound to a hash of one call, used once, and
  expires.
- In-doubt writes during a live run now pause the run instead of failing it.
- One process per run, using OS file locks next to the store.
- Wall-clock time recorded per active stretch and carried across pauses and crashes.
- Model responses and their token charges written in one append.
- Tool call ids made unique within a task; `ctx.idempotency_key` for tools.
- Starter project has a second agent that needs approval.
- Fixes: negative or non-integer token counts from providers are rejected, non-list `tool_calls`
  are reported as malformed, CLI output from runs is escaped, `legion runs` shows real statuses.
- Tests: resume at each interruption point, approval tampering and replay, a real process killed
  mid-write, fuzzed model output, and property tests for recovery.

### Phase 1

- Agent loop, action pipeline and runtime. Every tool call goes through the pipeline.
- Capabilities with resource globs, grants with `attenuate` (tested ahead of delegation),
  rule-table policy, budgets for steps, model calls, tool calls, tokens, cost and wall clock.
- Append-only event log in SQLite or memory, hash-chained per run. Run state, transcript and
  budget use are rebuilt from it.
- OpenAI-compatible and Anthropic adapters on httpx, plus a scripted provider. API key or no auth.
  Secrets stay as references and get scrubbed from events and tool output.
- Retries with backoff charged to the budget, repeat detection, tool retries only where the effect
  class allows, and in-doubt records for interrupted writes.
- CLI: `init`, `providers`, `agent validate`, `run`, `runs`, `inspect`, `verify`.
- Unit tests, Hypothesis invariant tests, optional real-model tests.

### Phase 0

- Framework notes, architecture, ADRs 0001 to 0012, threat model, roadmap.
