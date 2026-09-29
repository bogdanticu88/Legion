# ADR 0003: Authority is a Grant, and it only narrows

Status: accepted

## Context

My first design had separate primitives for capabilities, budgets and credentials. In practice
they go everywhere together and have to shrink together when work is delegated. Frameworks that
configure sub-agents separately let a child have tools its parent never had, and reset the budget
on every spawn.

## Decision

A `Grant` holds capabilities, budget limits, identity and expiry, and every task runs under one.
The root grant is what the agent asks for, and each item has to be in the operator's
`authority.grantable` list or the run won't start. `Grant.attenuate` makes a child grant and
raises if any part would be wider than the parent. What's been spent is tracked separately and
rebuilt from events.

Capabilities are strings like `files.read:notes/**`. That's really scoped permissions passed
around as data, closer to macaroons than to object capabilities, and the docs call it that.

## Consequences

- "Authority never grows through delegation" comes down to one function, which can be tested with
  generated inputs.
- The model can't create or widen a grant. Extra authority for a child has to come from outside
  (a human, NIA or MIA).
