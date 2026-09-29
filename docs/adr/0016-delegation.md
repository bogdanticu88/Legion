# ADR 0016: Delegation is an action, and a child is a task in the same run

Status: accepted

## Context

A host agent needs to hand parts of a job to other agents. The rule that matters: delegation
may narrow authority and must never widen it, however many levels deep it goes, and it can't
be a way around the pipeline, approvals or budgets.

## Decision

Delegation is a built-in tool, `delegate`, so it goes through the same pipeline as any other
call: schema, grant (it needs `agent.delegate:<agent name>`), policy, external authority, then a
delegation check, then approval if policy asks for one. Only after all of that is the child
made. The check happens before approval so a refused delegation never uses up an approval.

A child is a task in the same run: same event log, same hash chain, same lock, same resume. Its
task and grant ids are derived from the parent's task and call id, so if the delegation is run
again after a pause or crash it finds the child it already made.

What the child gets, field by field:

| Grant field | Rule |
|---|---|
| capabilities | subset: each must be within the parent's grant and within the child agent's spec. Asking for anything outside either is refused, not silently dropped |
| budget: steps, model calls, tool calls, tokens, cost | at most what the parent has left, and reserved from the parent (ADR 0017) |
| budget: wall seconds | at most what the parent has left; not reserved, because the child runs inside the parent's time |
| expiry | the parent's, or earlier |
| task deadline | the parent's |
| identity | derived, never passed in: same principal, and the parent agent is appended to `on_behalf_of` |
| delegation depth | one less than the parent's; at 0 the child can't delegate |
| children per task | at most the parent's |
| issuer, depth, parent id | set by the harness |
| approvals | not delegated; an approval is bound to one call in one task (ADR 0014) |
| credentials | not in the grant; tools resolve their own, as always |

The operator sets a ceiling on depth (`authority.max_delegation_depth`) and on tasks per run
(`authority.max_tasks`). A child's delegation limits are the smaller of its own agent's settings
and what its parent allows (depth minus one, the parent's fan-out).

The child starts with the objective and the `context` the parent passes, and nothing else: not
the parent's conversation, instructions or results. When it ends, the parent gets a summary
(status, output, structured output, error, what it used) as the tool result, not the child's
transcript.

What happens to the parent when the child:

- completes: the parent gets the result and carries on
- fails (budget, loop, deadline, bad output, model errors): the parent gets a failed result and
  carries on
- needs approval, or has a write in doubt: the whole run pauses; resume goes back down through
  the parent's call to the child
- would fail while a write is still in flight: the write is marked in doubt and the run pauses
  instead, so the parent can't carry on as if it hadn't happened
- is killed, or any ancestor is: the run fails and every open task fails with it
- the run is cancelled: everything in flight is marked in doubt, every open task is cancelled

Children run one at a time: the parent's `delegate` call waits for its child. Running several
at once is a separate step (see the roadmap).

## Consequences

- "Authority never grows" is enforced in `Grant.attenuate` and the delegation check, and tested
  over random trees.
- The tools a child can use come from its own spec, not the parent's list. The capabilities
  still bound them, so capability names have to reflect how strong the credential behind them
  is.
- Injected text can travel from parent to child through `context`, and back through the result.
  The grants limit what it can make happen.
