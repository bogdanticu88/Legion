# ADR 0017: A child's budget is reserved from its parent and settled when it ends

Status: accepted

## Context

If a parent with $10 could give each of two children $10, delegation would create money. The
same goes for steps, calls and tokens.

## Decision

When a child is made, its limit for each additive dimension (steps, model calls, tool calls,
tokens, cost) is recorded as a reservation against the parent (`budget.reserved`), in the same
append that creates the child. While the child runs, the parent's usage is its own spending plus
everything reserved. When the child ends, the reservation is released and the child's actual
usage, including its own children's, is charged to the parent (`budget.settled`).

So at every point, for every grant:

    own spending + reserved for running children + used by finished children <= limit

A child's limit is the smallest of its agent's default, what the delegate call asked for, and
what the parent has left after paying for the delegate call itself. Asking for more than is left
is refused. Wall time is capped but not reserved: children run inside their parent's time.

Settlement records what the child really used even if that's slightly over its limit (a model
call's prompt isn't known in advance), because that was really spent.

## Consequences

- A parent with a child running can't spend what the child might need, even if the child ends
  up using less.
- Nothing is double counted: a child's spending is charged to the child, and reaches the parent
  only through settlement.
- The property tests found one real bug here before this was written down: the planner gave the
  child the parent's last tool call, then the delegate call itself was charged on top.
