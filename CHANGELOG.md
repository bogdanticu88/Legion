# Changelog

## Unreleased

### Phase 1

- Kernel: agent loop, action pipeline, runtime facade. Every tool call passes one ordered
  pipeline with no hooks between steps.
- Authority: capabilities with resource globs, grants with attenuation (property-tested ahead of
  delegation), rule-table policy with deny precedence, budget ledger over steps, model calls, tool
  calls, tokens, cost and wall clock.
- Events: append-only SQLite and in-memory stores, per-run SHA-256 hash chain, projections that
  rebuild run state, transcript and budget from the log.
- Providers: OpenAI-compatible and Anthropic adapters over httpx, scripted provider for tests.
  API-key and no-auth access. Secrets as references, redacted from tool output.
- Failure model: one disposition per error, bounded retries with backoff charged to the budget,
  repeat detection, effect-class-aware tool retries, in-doubt recording for interrupted writes.
- CLI: `init`, `providers`, `agent validate`, `run`, `runs`, `inspect`, `verify`.
- Tests: unit, conformance (Hypothesis invariants), optional real-model integration tests.

### Phase 0

- Assessment, architecture, ADRs 0001 to 0012, threat model, roadmap.
