# ADR 0003: Grant is the authority primitive and only narrows

Status: accepted

## Context

The original design listed Capability, Budget and Credential Context as separate primitives. In
practice they travel together and must narrow together when a task delegates. Frameworks that
configure sub-agents independently let a child hold tools its parent never had and reset budgets
on every spawn.

## Decision

A `Grant` bundles capabilities, budget limits, identity context and expiry. Every task runs
under exactly one grant. The root grant is the agent's requested capabilities, each of which must
be covered by the operator's `authority.grantable` list, or the run does not start.
`Grant.attenuate` derives a child grant and raises unless every part is at most the parent's.
Consumption is tracked separately in a `Ledger` rebuilt from events.

Capabilities are strings of the form `name[:resource]` with glob matching on the resource. This is
attenuating scoped permission carried as data, closer to macaroons than to object capabilities,
and the documentation says so.

## Consequences

- "Authority never increases through delegation" is a property of one function and can be tested
  with generated inputs.
- The model can never create or widen a grant. Wider authority for a child must come from an
  external grant authority (a human, NIA or MIA).
