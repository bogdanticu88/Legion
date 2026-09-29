# ADR 0007: Effect classes decide retries, policy defaults and resume

Status: accepted

## Context

Whether a tool call may be retried, or re-run after a crash, depends on what it does to the
world. Frameworks that re-execute on resume make every write run twice unless the tool author
noticed.

## Decision

Every tool declares one of `pure`, `read`, `write_idempotent`, `write`, `external_irreversible`.
Registration fails without it. Timeouts and retryable errors are retried only for the first three.
A `write` or `external_irreversible` action that times out is recorded as in doubt: in Phase 1
that fails the task, and from Phase 2 it escalates to a human. The default policy denies
`external_irreversible` until approvals exist.

## Consequences

- Tool authors must think about effects once, at declaration.
- A mis-declared effect class is an operator error the runtime cannot detect.
