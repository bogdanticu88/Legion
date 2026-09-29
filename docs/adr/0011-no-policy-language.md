# ADR 0011: No custom policy language

Status: accepted

## Context

Policy languages are hard to design and harder to secure, and OPA/Rego and Cedar already exist.

## Decision

`PolicyDecisionPoint` is an interface. The built-in version is a rule table matched on tool name,
capability and effect class, where deny beats approval and approval beats allow. Anything more
goes in an adapter.

## Consequences

- The built-in policy can't look at argument values. Use resource extraction and capabilities for
  that, or an external engine.
