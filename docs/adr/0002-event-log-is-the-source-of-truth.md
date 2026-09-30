# ADR 0002: The event log is the source of truth

Status: accepted

## Context

Most frameworks keep run state in objects and in the context window and add persistence later.
Resuming then means replaying a transcript and hoping the model picks up where it left off, and
retrofitting durability is how writes end up running twice.

## Decision

Every state change is written as an event before Legion acts on it. Run and task status, budget
use and the transcript are all computed from the events. Events are hash-chained the same way as
MIA's audit log, and the SQLite store rejects updates and deletes.

Resume came later, but from the first version on nothing important lives only in memory.

## Consequences

- Tests can rebuild a finished run from storage and compare it with the live one.
- Benchmarks and forensics read the same log the runtime uses.
- Model requests are stored as a hash plus message count and responses are stored in full, so the
  transcript isn't stored over and over.
- The chain shows tampering but doesn't prevent it. The host owner can rewrite it.
