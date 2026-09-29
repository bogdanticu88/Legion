# ADR 0007: Effect classes decide retries and resume

Status: accepted

## Context

Whether a tool call can be retried, or re-run after a crash, depends on what it does. Frameworks
that simply re-execute on resume run every write twice unless the tool author thought of it.

## Decision

Every tool declares `pure`, `read`, `write_idempotent`, `write` or `external_irreversible`, and
can't be registered without one. Only the first three get retried. A `write` or
`external_irreversible` call that times out is recorded as in doubt; that fails the task in Phase 1
and will go to a human from Phase 2. The default policy denies `external_irreversible` until
approvals exist.

## Consequences

- Tool authors have to think about effects once, when they declare the tool.
- If they declare the wrong class, Legion can't tell.
