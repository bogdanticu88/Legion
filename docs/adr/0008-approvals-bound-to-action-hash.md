# ADR 0008: Approvals are tied to the action hash

Status: superseded by ADR 0014

## Context

If an approval is recorded against a tool name, the same tool can then run with different
arguments under that approval.

## Decision

An `Action` has a canonical form (tool, arguments, resource, grant id, task id) and a SHA-256 of it.
An approval stores that hash, who approved, when, and an expiry, and can be used once. Before
running, Legion checks the action's hash matches and the approval is unused and not expired. Until
approvals were built, config that could need an approval failed to load.

## Consequences

- Changing any argument after approval means asking again.
- Approvals need a run that can pause, which is why they come with crash recovery.
