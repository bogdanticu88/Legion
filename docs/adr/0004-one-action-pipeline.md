# ADR 0004: One action pipeline, no middleware

Status: accepted

## Context

Governance implemented as optional middleware or callbacks can be skipped, reordered, or
bypassed by a tool that calls another tool directly. NIA made the same point for its gateway:
one decision function behind every entry point, because two copies drift.

## Decision

Every tool call becomes an `Action` and passes `ActionPipeline.execute`, which runs a fixed order:
kill check, lookup, offered-tool check, schema validation, repeat detection, grant, policy,
budget, credential resolution, execution with timeout and bounded retry, output validation and
redaction, events. There are no hooks that can skip a step. Extension points are the ports the
steps call (policy, identity, credentials), not insertion points between steps.

## Consequences

- Reviewing enforcement means reading one function.
- Tools get a `ToolContext` with their declared credentials and nothing that reaches the kernel,
  so a tool cannot call another tool around the pipeline through Legion. It can still do anything
  Python can; see the threat model.
