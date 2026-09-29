# Changelog

## Unreleased

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
