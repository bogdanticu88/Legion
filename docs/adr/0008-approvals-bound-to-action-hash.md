# ADR 0008: Approvals are bound to a canonical action hash

Status: accepted (implementation in Phase 2)

## Context

An approval recorded against a tool name lets the approved call be replayed with different
arguments.

## Decision

An `Action` has a canonical form (tool, validated arguments, resource, grant id, task id) and a
SHA-256 over its canonical JSON. An approval records the hash, the approver, the time, the scope
(single use) and an expiry. Execution checks that the hash of the action about to run equals the
approved hash, and that the approval has not been used or expired. Until Phase 2, configuration
that could require approval is rejected at load time.

## Consequences

- Changing any argument after approval invalidates it.
- Approval needs durable pause, which is why it ships with persistence and not before.
