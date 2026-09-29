# ADR 0011: No custom policy language

Status: accepted

## Context

Policy languages are hard to design and harder to secure. OPA/Rego and Cedar already exist.

## Decision

`PolicyDecisionPoint` is a protocol. The built-in implementation is a rule table matched on tool
name glob, capability name glob and effect class, with deny winning over approval and approval
winning over allow. Anything richer is an adapter.

## Consequences

- The built-in policy cannot express conditions on argument values. Those belong in the tool's
  resource extraction, in capabilities, or in an external engine.
