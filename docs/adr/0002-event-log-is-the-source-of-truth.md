# ADR 0002: The event log is the source of truth

Status: accepted

## Context

Most frameworks keep state in objects and in the model's context window, then bolt persistence
on later. Resume then means replaying a transcript and hoping the model reconstructs where it
was. Retrofitting durability after the fact is the usual source of duplicated side effects.

## Decision

Every meaningful transition is an event appended to a per-run log before the runtime acts on it.
Run status, task status, the budget ledger and the transcript sent to the model are projections
of that log. Events are chained by hash (`sha256(prev_hash + canonical_json)`, genesis of 64
zeros, same scheme as MIA). The SQLite store refuses updates and deletes.

Resume logic is Phase 2, but from Phase 1 no state that matters lives only in memory.

## Consequences

- A test can rebuild a finished run from storage and compare it with the live run.
- Benchmarks and forensics read the same log the runtime uses.
- The transcript is not stored twice. Model requests are recorded by hash and message count, and
  responses in full, to avoid quadratic storage.
- The chain is tamper evidence, not proof: the host owner can rewrite it.
