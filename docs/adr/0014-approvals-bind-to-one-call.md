# ADR 0014: An approval covers one call, as it was proposed

Status: accepted (replaces the plan in ADR 0008)

## Context

The approval has to mean "this exact action", not "the agent may do deploys now". It also has to
survive the process exiting while it waits, and it must not become reusable because of a crash.

## Decision

When policy returns `require_approval` for an action that the grant, policy and external
authority otherwise allow, Legion records `approval.requested` and pauses the run. The process
exits. A person runs `legion approval show`, then `legion approve` or `legion deny`, and then
`legion resume`.

The approval is bound to a SHA-256 over:

- run, task and call ids
- agent name and spec hash
- tool name and the full tool spec (schema, effect class, capabilities, credential names)
- arguments, resource, effect class and required capabilities
- the full grant (capabilities, limits, identity, expiry)
- the agent identity from the identity port
- the tool settings and the credential references (names and `env:` refs, never values)

It leaves out timestamps, event ids, the model's wording and the budget state, since
those change without changing what the call does. Policy isn't in the binding either, because
policy is evaluated again when the call runs.

Just before the tool runs, Legion builds the binding again and compares hashes. On a match the
approval is marked `consumed` before the tool starts, so a crash after that can't make it usable
for a second, different execution. Two narrow exceptions: if the process died after consuming it
but before the tool started, the same call may still use it; and a safe-to-repeat call
interrupted mid-run may run again under it.

States: `requested`, then `granted`, `denied` or `expired`; `granted` then `consumed`, `expired`
or `invalidated` (when the binding no longer matches). Each move is an event, and the projection
rejects any move not in that list, so an out-of-order or forged sequence fails on replay.

The human sees tool, description, effect, target, arguments, required capabilities, agent, who
it acts for, the objective, and the model's accompanying text marked as untrusted, along with the
binding hash. All of it is escaped before printing.

## Consequences

- Approving HOST-A doesn't allow HOST-B, a second identical call, or the same call after the
  settings changed.
- Approvals expire (one hour by default, `approvals.ttl_seconds`).
- The approver is the local OS user. Legion doesn't authenticate approvers, and anyone who can
  write the event store could append an approval. The store must sit outside anything tools can
  write; Legion refuses the obvious misconfiguration. Signed approvals would close this properly.
