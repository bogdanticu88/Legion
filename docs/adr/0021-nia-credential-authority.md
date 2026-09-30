# ADR 0021: NIA as a credential authority, and Legion's own call id

Status: accepted (Phase 5B.3)

## Context

ADR 0019 gave Legion a generic `CredentialAuthority` port and decided that Legion, not the
authority, judges what a credential is worth. ADR 0020 connected NIA for identity and kill state.
NIA (Phase 5B.2, commit `40891e3`) now issues scoped credentials: bound to one NIA agent, one
resource, a set of NIA tools, one audience, and a caller's Action hash, call id and Grant
fingerprint, with a fixed expiry, and reports them active, expired, revoked or unknown.

Two things had to be settled before NIA could sit behind the port.

- The call id Legion put in credential requests was the model provider's tool call id. The model
  (or the provider) chooses it. It was made unique within a task before being recorded, but it
  wasn't Legion's, and a binding the model can name isn't a binding.
- NIA requires a resource; Legion's request allows none.

NIA is optional. Nothing in this ADR makes it required, and nothing in Legion's kernel, domain,
ports or events names it.

## Decision

### Legion's own call id

Every tool call gets a Legion call id: `lc-` and 32 hex characters of the digest of the
`event_id` Legion gave the `model.responded` event that recorded the call, and the call's
position among that response's tool calls (`events.projections.legion_call_id`). The event id is
a random UUID Legion generates; the model and provider choose nothing that goes into it.

- different for two identical calls, even with the same Action hash, and for calls in different
  tasks or runs
- the same for every attempt of one call, after a pause, after a crash, and when the run is
  replayed from the log, because it is derived from the log
- recorded on `action.proposed`, `tool.started`, `approval.requested` (and in its subject) and
  `credential.resolved`/`credential.refused`, shown by `legion inspect` and `legion credentials`,
  and passed to tools as `ToolContext.legion_call_id`

`CredentialRequest.call_id` is now this id. The approval binding includes it. The provider's id
stays as `call_id` in events and the transcript, where the model needs it to match results to
calls; it binds nothing and is shown as provenance.

Runs recorded before this change still replay: the id is derived from the log, so events without
the field get the same value, and a recorded value that doesn't match the derivation fails the
replay. One behaviour changes on purpose: an approval requested before the upgrade doesn't match
its binding any more (the binding now includes the Legion call id), so on resume it is
invalidated and has to be asked for again, rather than honoured.

### Generic additions to the port

Two, both optional and neither about NIA:

- `CredentialRequest.external_principal` and `CredentialEvidence.external_principal`: the
  identity authority's id for the acting agent (`AgentIdentity.external_id`), when there is one.
  Legion refuses evidence that names a different one. An authority that names none is judged on
  the rest.
- `UnknownCredential`, which `status` and `revoke` raise when the authority has no record of a
  reference; `IssuedCredential.also_scrub`, parts of a secret that are secret on their own.

### `NiaCredentialAuthority`

`legion.adapters.nia_credentials`, built by the config loader only when a credential authority
says `provider: nia`. It talks to NIA's control plane over HTTP and nothing else: no shared
database, files or imports.

```yaml
credential_authorities:
  nia:
    provider: nia
    endpoint: https://nia.internal:8080   # plain http only to 127.0.0.1 or [::1]
    credential: env:LEGION_NIA_ISSUER     # NIA's issuer role; not the identity block's token
    audience: nia-gateway                 # the NIA gateway the credential is for
    trusted: true
    timeout_s: 5                          # shorter than credential_policy.timeout_s
    attempts: 2                           # status and revoke only
    # agents: taken from the NIA identity block when left out; if both, they must agree
credentials:
  github:
    authority: nia
    provider: nia-gateway                 # must equal the audience
    permissions: {repo.read: [repo.read]} # Legion capability -> NIA tool names
    max_lifetime_s: 120                   # within NIA's maximum (900s by default)
```

Roles are separate: the identity port needs NIA's viewer role, this needs issuer (which can
issue and revoke scoped credentials and nothing else; it can't write grants). The loader refuses
the same secret reference for both. Both tokens are scrubbed from everything Legion records, go
only in the `Authorization` header, only to the configured endpoint, never through a proxy or CA
bundle from the environment, and redirects aren't followed.

### Request mapping

| Legion | NIA | Conversion | Checked | Why it matters |
|---|---|---|---|---|
| `subject` (Legion agent) | path `{ref}` | operator mapping, agent -> NIA ref | mapped, and equal to `external_principal` when there is one | the credential is issued to the agent the Grant names, never one the model picked |
| `external_principal` | path `{ref}` | none | must equal the mapped ref | identity port and credential authority can't disagree about who acts |
| `permissions` | `permissions` | none; the operator's mapping names NIA tools | NIA charset | NIA checks each is a tool grant |
| `resource` | `resource` | none | required; NIA charset | NIA checks it is a data grant; no resource, no NIA credential |
| `provider` | `audience` | none | must equal the configured audience | the credential is for one gateway |
| `action_hash` | `action_hash` | none | 64 hex | binding |
| `call_id` (Legion's) | `call_id` | none | NIA charset | binding |
| `grant_fingerprint` | `grant_fingerprint` | none | 64 hex | binding |
| `max_lifetime_s` | `ttl_seconds` | none | NIA refuses above its maximum; nothing is clamped | whole life known |
| `principal` (human) | not sent | | | NIA doesn't know Legion's principals |

Issuance is sent once. It is tried again only if the connection was never made; once anything
may have reached NIA, a failure is a refusal, because a second request could leave a live
credential Legion never heard of.

### Evidence mapping

| NIA evidence | Legion evidence | How |
|---|---|---|
| `issuer` | `authority` | this authority's name only if it equals the configured issuer; otherwise a value that can't equal any Legion authority name |
| `principal` | `external_principal`, and `subject` | verbatim; `subject` is the Legion agent mapped to it when exactly one is, otherwise a value that can't match |
| `audience` | `provider` | verbatim |
| `permissions`, `resource`, `action_hash`, `call_id`, `grant_fingerprint`, `issued_at`, `expires_at`, `credential_ref` | same names | verbatim, wrong types left wrong |
| none | `principal` | copied from the request: NIA doesn't evidence it |
| none | `revocation_ref` | the credential ref, when NIA's evidence names it |
| anything else, including a `verified` flag | dropped | Legion decides assurance |

The adapter never makes a disagreement look like agreement. Legion's own assessment (ADR 0019)
then compares every field against the request, exactly as for any authority.

### Assurance

A trusted NIA whose evidence names this principal, Action, Legion call id, Grant, resource, a
subset of the permissions, this audience and a lifetime within the request's reaches `bound`,
the existing top level. There is no NIA-specific level. `bound` means Legion checked trusted
evidence tying the credential to this call. It does not mean NIA's gateway checks the Action or
call when the credential is used: it checks audience, tool, resource, status and kill state
(NIA's `docs/SCOPED_CREDENTIALS.md`), and can't see the rest.

### Status and revocation

| NIA answers | Legion reads |
|---|---|
| 200 `active` | active |
| 200 `expired` | expired: may be replaced by a fresh issuance for the same call |
| 200 `revoked` | revoked: refused, never reissued around |
| 404, or 200 `unknown` | unknown (`UnknownCredential`): refused |
| a reference this process didn't issue | unknown: refused |
| 401, 403, 429, 5xx, timeout, reset, redirect, compressed, oversized, malformed, another credential's or another principal's answer | unavailable: refused |

Revoked is never read as expired. `revoke` posts to NIA's revoke endpoint; revoking twice, or
revoking an expired credential, is fine.

### Retries, approvals, delegation, crashes

Unchanged generic semantics, now with Legion's call id:

- a retry of one call reuses its credential while NIA says it is active; if NIA says expired, a
  new one is issued for the same call id; revoked, unknown or unavailable stop the call
- an approval is consumed before credentials are asked for; a credential refusal ends the call,
  and the model repeating the action is a new call needing a new approval
- a child's credential is issued to the child's own NIA ref, under the child's Grant; evidence
  naming the parent or a sibling is refused
- a crash before `tool.started` means a fresh issuance on resume, for the same call id (the
  earlier credential is left to expire); after `tool.started`, a read runs again and a
  non-idempotent write is in doubt and never issued or run again. No secret is persisted for
  resume

### What happens, in order

Measured on one call with both NIA adapters configured:

1. `GET /agents/{ref}` (kill check at the start of the call)
2. `GET /agents/{ref}` (authorize)
3. `GET /agents/{ref}` (kill check after the budget charge)
4. `GET /agents/{ref}` (kill check before issuance)
5. `POST /agents/{ref}/scoped-credentials`
6. `GET /agents/{ref}/scoped-credentials/{cred}` (the last status check)
7. `GET /agents/{ref}` (the last kill check)
8. `tool.started` is recorded
9. the native tool runs with the credential

None of it is atomic. A kill or revocation after step 7 isn't seen by this call; whatever the
tool does with the credential after that is between the tool and whatever accepts it. For a
credential used at NIA's gateway, the gateway checks status and kill state again at use.

## Limitations

- NIA credentials need a resource. A call that names none gets none from NIA and is refused;
  other authorities are unaffected.
- The NIA gateway doesn't check the Action or the Legion call id at use. Within its life and
  scope a NIA credential is a bearer token.
- NIA's grants pair no tool with a resource, so a credential can combine any granted tool with
  any granted resource.
- Revocation isn't atomic with use, and the check-to-use window above remains.
- Evidence isn't signed; Legion trusts its TLS channel to NIA.
- Status is only answered for references this process issued; a restarted Legion re-issues.
- MCP servers still hold their own credentials; nothing here is Action-scoped MCP.

## Consequences

- Without a `provider: nia` authority, nothing changes: no NIA import, configuration, process or
  environment variable is involved.
- Another authority (Vault, a cloud IAM token exchange) can sit behind the same port and be held
  to the same conformance tests (`tests/conformance/test_credential_authority_contract.py`).
