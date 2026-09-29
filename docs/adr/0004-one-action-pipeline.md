# ADR 0004: One pipeline for every tool call

Status: accepted

## Context

When governance is middleware or callbacks, it can be skipped, called in the wrong order, or
avoided by a tool that calls another tool directly. NIA ran into the same thing with its gateway
and ended up with one decision function behind every entry point.

## Decision

Every tool call becomes an `Action` and goes through `ActionPipeline.execute` in a fixed order
(listed in ARCHITECTURE.md). There are no hooks between steps. To change behaviour you swap what a
step calls (policy, identity port, credential resolver), not the steps.

## Consequences

- Reviewing enforcement means reading one function.
- Tools get a `ToolContext` with their own credentials, the tool settings, ids and an idempotency
  key, and no handle on the kernel, so they can't call other tools through Legion. They can still
  do anything Python can.
