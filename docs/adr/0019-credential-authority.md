# ADR 0019: Credentials come from a credential authority and are checked against the Action

Status: accepted

## Context

Legion limits what an agent may ask for. It didn't limit what the credential behind a tool can
do. A grant for `repo.read:repo-A` could be carried out with an organisation-wide admin token,
and Legion had no way to know, let alone refuse. Until now every tool credential was a static
secret reference in `legion.yaml`: the same value for every run, task, child and resource, with
no expiry.

Three things have to be kept apart:

| | Question | Example |
|---|---|---|
| Identity | who is acting | human `ana`, through agent `boss`, then `reader` |
| Logical authority | what the task's Grant lets them do | `repo.read:repo-A` |
| Credential authority | what the downstream system will accept the credential for | `contents:read` on `repo-A` |

The credential itself is secret material. Credential authority is metadata about it, and it's
only worth something if someone Legion trusts vouches for it.

NIA is the reference authority, but today it can't issue a credential narrower than an agent (see
docs/nia-integration-requirements.md). So this ADR defines the contract on Legion's side and proves it against a test
authority; NIA gets the matching changes later.

## Decision

### A separate port

Issuing credentials is a `CredentialAuthority` port (`legion/ports/credentials.py`), separate from
`IdentityPort`. Identity says who is acting and whether they're killed; the authority issues and
vouches for credentials. `IdentityPort.credential()`, which nothing called, is gone. Nothing in
Legion imports an authority's own client library.

```python
class CredentialAuthority(Protocol):
    name: str
    async def issue(self, request: CredentialRequest) -> IssuedCredential: ...
    async def status(self, credential_ref: str) -> CredentialStatus: ...  # active, expired, revoked
    async def revoke(self, credential_ref: str) -> None: ...
```

`IssuedCredential` keeps the secret (a `Secret`, which never prints) apart from the evidence, and
carries the authority's `credential_ref` on its own so Legion can ask about and revoke the
credential even when the evidence is unusable. The authority gets a copy of the request, so it
can't change what Legion compares against and records.
Evidence is whatever the authority says about the credential: authority, credential reference,
provider, principal and agent, permissions, resource, issue and expiry times, and optionally the
Action hash, call id and Grant fingerprint it's bound to. The authority may also say `verified:
true`; that's recorded and ignored.

### Assurance

Legion, not the authority, decides how much it knows about a credential:

| Level | Meaning |
|---|---|
| `unverified` | nothing trustworthy is known. Static secrets, and any credential with missing or malformed evidence |
| `declared` | an authority Legion doesn't trust says what it can do, and that's within the request |
| `verified` | a trusted authority says what it can do, and that's within the request |
| `bound` | verified, and bound to this Action hash, call and Grant, for this principal and agent, with an expiry inside the allowed lifetime |

"Trusted" means the operator marked the authority `trusted` in `legion.yaml`; the authority's own
code is operator-supplied, like a tool module. Missing evidence is never more than `unverified`,
and an untrusted authority is never more than `declared`, whatever it says.

Evidence that shows **more** authority than was requested is refused outright, trusted or not,
because then Legion knows the credential is too strong. That holds even when the evidence doesn't
validate: malformed evidence whose `permissions` or `resource` still show more than was asked is
refused, not treated as merely missing. Refused are: extra permissions, a wider or missing
resource, another principal or agent, another Action, call or Grant, another provider or
authority, already expired, a lifetime over the maximum, issued in the future, or a credential
reference already used in this run. References are limited to 120 characters from
`[A-Za-z0-9._:/@+=-]`, so nothing about them changes on the way into the log, and reuse is checked
on a run-scoped digest of the exact reference that the log keeps, so it holds across a restart.

### What's required, and it only goes up

A call's credential must meet the highest of:

- `credential_policy.minimum` in `legion.yaml` (default `unverified`, so existing static
  credentials keep working)
- the credential mapping's `minimum` (default `bound`)
- `credential_assurance` on any policy rule that matches the call, whatever that rule's decision:

```yaml
policy:
  rules:
    - {decision: allow, capability: "repo.issue.*", credential_assurance: bound}
```

Nothing lowers it. If the requirement can't be met (the authority is down, times out, returns no
evidence, returns something weaker, or returns something wider), the call is refused. There's no
fallback to a static secret: a credential name is either static or mapped, never both.

MCP calls are held to the same requirement. An MCP server holds its own credential for the whole
process, which Legion can't issue or check, so it counts as `declared` if the operator wrote a
`credential_scope` and `unverified` otherwise. With a higher requirement the call doesn't reach
the server.

### The request comes from authorized state

The model never names a credential or a scope. For a tool that needs credential `github`, Legion
looks up the operator's mapping:

```yaml
credentials:
  github:
    authority: local
    provider: github
    permissions:
      repo.read: [contents:read]
      repo.issue.create: [issues:write]
    max_lifetime_s: 300
    minimum: bound
credential_authorities:
  local: {module: authorities.py, trusted: true}
```

The request's permissions are the union of the mapped permissions for every capability the
Action requires; a required capability with no mapping means no credential and no call. The
resource is the Action's resource. The identity, run, task, call, Action hash and Grant
fingerprint come from the task. Nothing comes from the model's text, tool output, a child's
result or anything a server says. The same Action and Grant always give the same request.

### Order in the pipeline

The per-call steps are:

1. kill check
2. lookup, offered to this agent
3. schema and resource
4. `action.proposed`
5. repeat check
6. grant
7. policy (which may raise the credential requirement)
8. external authority (`IdentityPort.authorize`)
9. delegation plan, for `delegate`
10. approval, if policy asks for one (consumed here)
11. `action.authorized`
12. budget: the attempt is charged, or the run stops
13. kill check
14. credentials: each one the call doesn't hold yet is issued and assessed; `credential.resolved`
    or `credential.refused`. An MCP call's server credential is compared with the requirement
    here too
15. last look before dispatch: each held credential is inside its lifetime and its authority says
    it's active; then the kill check once more
16. `tool.started`, then the tool runs with the credentials in its `ToolContext`

Before this ADR, credentials were resolved between 11 and 12. Now nothing is issued for a call that
won't run: out of budget, killed, or waiting for an approval.

The secret is added to the scrub list the moment the authority returns it, before its evidence is
read. Anything that goes wrong after that (evidence that raises when read, a return value that
isn't an `IssuedCredential`) refuses the call and is recorded by exception type only.

If a credential is refused, any credential already issued for the call is revoked (best effort,
and only possible when the authority gave a reference), then `credential.refused` and
`action.refused` are written in one append. A crash can't leave a refused call that resume would
run under its approval. The model gets only the reason code and a fixed message; the
details, which can hold text the authority chose, go into `credential.refused`: scrubbed of every
known secret first, then cleaned of control and invisible characters and cut to 200 characters. An authority's exception is recorded by type only, because its message
could hold the secret it was minting.

### Expiry, revocation and retries

Steps 12 to 16 run on every attempt. A retry (only `pure`, `read` and `write_idempotent` calls are
ever retried) keeps the credentials of the same call, and step 15 decides whether it may use them.
The binding needs no check: a retry is the same Action, call and Grant. The authority is asked for
the credential's status every time, even when Legion's own clock already says it has expired,
because a credential that is both expired and revoked is revoked.

| Before an attempt, the credential is | pure | read | write_idempotent | write, external_irreversible |
|---|---|---|---|---|
| inside its lifetime, active | reused | reused | reused | not retried at all |
| expired (the authority says so, or it says active and Legion's clock says expired) | replaced with a new one for the same call, which goes through every check | same | same | not retried |
| revoked, whether or not it has also expired | call refused, nothing issued | same | same | not retried |
| status unknown (any answer other than active, expired or revoked; no reference to ask about) | refused | same | same | not retried |
| authority unreachable or timing out | refused | same | same | not retried |
| agent or an ancestor killed | run stops, nothing issued (checked before each round of issuing, and again after) | same | same | not retried |

This is the same whatever the required assurance: an authority that can't be asked is never
treated as having said yes. An expired credential is replaced because expiry is a clock, not a
decision; a revoked one isn't, because asking again would go around the authority's decision.

A credential that expires between being issued and dispatch (step 15) is replaced once, since
nothing has been sent; if the new one has expired too, the call is refused.

A `write` or `external_irreversible` call that may have been sent is never repeated: after a crash
or a timeout it's in doubt (ADR 0013) and waits for `legion reconcile`, and a fresh credential
being available changes nothing.

### Time of check and time of use

What Legion can promise: identity and credential state are checked immediately before dispatch
(step 15), after everything else, and nothing is issued for a call that fails an earlier step.
What it can't: that nothing changes between that check and the moment the downstream system acts
on the credential. The remaining window is:

- after the last check, while `tool.started` is written and the tool function starts
- the tool's own time talking to the downstream system

A revocation or kill in that window isn't seen by Legion. A kill is noticed at the next check
(the next model call or attempt) and the run stops, but a call that already started isn't undone
or claimed not to have happened; if it's a write that may have gone out, it's in doubt.
Revoking a credential while a call is using it doesn't undo what the call did. Closing the window
needs the downstream system to check the credential itself when it's used: a gateway, a broker
that holds the credential, or a proof-of-possession token. That's for the integration contract,
not something Legion can do alone.

### Delegation

A child's credential is requested with the child's identity (the child agent, on behalf of its
parent), the child's Grant fingerprint and the child's call. So
`credential(child) <= Grant(child) <= Grant(parent)` holds wherever Legion can compare authority:
the child Grant is at most the parent's (ADR 0016), the request is derived from the child Grant,
and evidence wider than the request, or bound to the parent's or a sibling's call or Grant, is
refused. A child asking for a resource or capability its parent doesn't hold is refused at the
delegation step, before any credential exists. A child that's out of its reserved budget gets no
credential (step 12 comes first). The authority isn't a second delegation mechanism: it's only
ever asked for what a Grant Legion already derived allows.

### Approvals

An approval is consumed at step 10, before any credential exists. A credential problem after that
doesn't give the approval back.

| After approval | Approval | Needs re-approval | Call binding changed | Runs |
|---|---|---|---|---|
| credential accepted | consumed | no | no | yes, once |
| credential too broad, wrong principal, wrong call, malformed, different resource | consumed | yes, for any new call | no, but this call is over | no |
| authority unavailable | consumed | yes | no | no |
| credential expires before dispatch, replaced | consumed once | no | no (same call, same Action) | yes, once |
| crash before `tool.started` | consumed | no: resume runs the same call | no | yes, once, with a new credential |
| crash after `tool.started` (write) | consumed | no | no | in doubt, waits for reconcile |
| agent killed before dispatch | consumed | the run is over | no | no |

A new call with the same arguments is a new call: its own approval, never the old one.

### Crash and resume

Credentials are never persisted, so a restart has nothing to resurrect or leak. With a crash at
each point below and a resume:

| Crash | Sent? | Fresh credential? | Retry? | In doubt? |
|---|---|---|---|---|
| before the credential request (after the budget charge) | no | yes | the call runs once on resume | no |
| during the request (inside the authority) | no | yes | runs once | no |
| after the credential came back, before it's recorded | no | yes | runs once | no |
| after `credential.resolved` | no | yes | runs once | no |
| after the last check, before `tool.started` | no | yes | runs once | no |
| after `tool.started`, before or during the tool, after its result, before `tool.completed` | maybe | only for a safe call that runs again | read: runs again. write: never | write: yes |

Credential references are tracked in the log, so a reference the authority hands out twice in one
run is refused even across a restart. Budget is charged per attempt, as before.

### What the log records

`credential.resolved` and `credential.refused` carry, per credential per call: the principal and
agent, the Grant id and fingerprint, the provider, the required and achieved assurance, the
requested permissions and resource, the permissions and resource the evidence showed (empty when
there was no usable evidence), the authority's credential reference and its run-scoped digest,
the revocation reference, issue and expiry times, whether the evidence was wider than the
request, and the problems found. Run, task,
call and Action hash come from the envelope. Evidence is schema-checked and bounded before
anything is kept (references as above; other strings up to 512 characters; at most 64
permissions of up to 200 characters); anything else is malformed and counts as no evidence.
Everything the authority supplied is scrubbed of known secrets, then stripped of control and
invisible characters and cut to 200 characters in the event.

`legion credentials <run-id>` shows this per call; `--json` gives the full structured records.

### MCP

MCP isn't covered by any of this. An MCP server holds its own credential for the whole process;
Legion can't issue it, bind it to a call or ask whether it's active. So an MCP call's credential
assurance is `declared` (the operator wrote a `credential_scope`) or `unverified`, never more,
however narrow the Grant is, and with a higher requirement the call is refused before the server
sees it. `credential_scope` is operator-declared metadata, not proof.

Per-call MCP credentials would need, at least:

- a per-call credential delegated for the Action (token exchange on each call), with an audience
  and resource binding the MCP server or its downstream checks
- a broker that holds the downstream credential and injects it per call, so the server never holds
  a standing one
- evidence from the server or broker that the call used that credential, checkable by Legion
- proof of possession where a bearer token could be replayed

None of that exists yet. NIA's gateway today injects one static, process-wide downstream token for
MCP, which is not enough for verified or bound per-Action authority.

### Static credentials

A plain `env:NAME` credential still works where the requirement is `unverified`, which is the
default. It's recorded as `unverified` on every call, and any requirement above that (global,
mapping or policy rule) refuses it. It isn't scoped, Grant-bound, Action-bound or verified, and
nothing about it is least privilege: it has whatever authority the secret has. Missing or
malformed evidence, from any authority, is `unverified` too, and well-formed evidence from an
untrusted authority is `declared` at most.

`action.proposed.remote` for an MCP call records `credential_assurance` as above. What an identity
service says about a server's credential is recorded as a claim (`claimed_verified`), and doesn't
change it.

## Consequences

- Where a trusted authority can issue a credential bound to the Action, the credential used for a
  call can't represent more than the Grant allowed for that call. Where it can't, the assurance
  level on each call says so.
- The approval binding includes each credential's description (the static reference, or a hash of
  the mapping), so changing a mapping invalidates approvals given under the old one.
- The assurance is only as good as the trusted authority. Legion checks what the authority says;
  it can't check what the downstream system actually does with the credential. A signed evidence
  format would let Legion check the authority's statements without trusting the channel; that
  needs the authority's side (NIA) first.
- There's no production authority in Legion. The tests use a deterministic hostile one
  (`tests/credential_lab.py`), and `examples/credential_demo.py` shows eight cases with a small
  local one.
- A credential the authority issued without a usable reference can't be asked about before
  dispatch, so it's refused whatever the requirement.
- Credential references are only checked for reuse within a run. An authority that hands out the
  same reference in two runs isn't caught by that check; binding to the call and Grant is what
  stops the credential being used for another call.
- Resources are compared as exact strings (with glob patterns on the requesting side). An
  authority or downstream system that normalises them differently (case, trailing slash,
  encoding, look-alike characters) gets its evidence refused, not reinterpreted.
