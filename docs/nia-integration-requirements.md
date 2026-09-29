# What an identity and credential authority has to provide

This is the contract Legion needs from a credential authority, written from Legion's security
requirements (ADR 0019), not from what any authority does today. NIA is the intended reference
implementation; the last sections compare it with NIA as inspected on 2026-09-29 (commit
`e6802a1`). Nothing here is implemented in NIA, and Legion has no NIA adapter.

Field classes used below:

- **critical**: Legion's decision depends on it; wrong or missing means refuse or lower assurance
- **informational**: recorded for the operator, no decision depends on it
- **secret**: credential material; never logged, never shown to a model
- **evidence**: non-secret statement about a credential, recorded in the event log

## A. Credential authority (required)

Legion asks for a credential for one call, after the call is authorized, charged and checked for
kill state. The authority issues one no wider than asked, says what it is, and answers whether it's
still active.

### Request

| Field | Class | Notes |
|---|---|---|
| `principal` | critical | the human or service the run acts for, as Legion recorded it |
| `subject` | critical | the acting agent; for a child, the child |
| `on_behalf_of` | informational | the delegation chain, outermost first |
| `run_id`, `task_id` | informational | for the authority's records |
| `call_id` | critical | the call within the task; two identical calls share an Action hash |
| `action_hash` | critical | Legion's hash of the tool, arguments, resource, Grant and task |
| `grant_id`, `grant_fingerprint` | critical | the Grant the call runs under; the fingerprint is a hash of it |
| `provider` | critical | which downstream system the credential is for; also its audience |
| `permissions` | critical | the provider permissions the call needs, from the operator's mapping |
| `resource` | critical | the one resource the call acts on (null means the call names none) |
| `capabilities` | informational | the Legion capabilities that authorized it |
| `tool` | informational | the tool name |
| `max_lifetime_s` | critical | the longest the credential may live, whole life, not what's left |

Everything in the request comes from Legion's authorized state and the operator's configuration,
never from model text or tool output. The authority must not issue more than the request, and
must not widen it on its own initiative.

### Response

| Field | Class | Notes |
|---|---|---|
| credential material | secret | handed to the tool only; Legion scrubs it everywhere |
| `authority` | critical | the authority's name as configured in Legion |
| `credential_ref` | critical | unique, non-secret identifier, 1 to 120 characters from `[A-Za-z0-9._:/@+=-]`, returned alongside the credential and in the evidence (they must match); the same one twice in a run is refused |
| `provider` | critical | must equal the request |
| `principal`, `subject` | critical | must equal the request |
| `permissions` | critical | what the credential can actually do; a subset of the request |
| `resource` | critical | what it can act on; null means any resource, which is refused unless the request named none |
| `issued_at`, `expires_at` | critical | whole life within `max_lifetime_s`; timezone-aware |
| `action_hash`, `call_id`, `grant_fingerprint` | critical for `bound` | all three equal to the request, or the credential is at most `verified` |
| `revocation_ref` | informational | how the authority identifies it for revocation |
| `verified` | informational | the authority's own opinion; ignored |

Size limits: strings up to 512 characters, `credential_ref` up to 200, at most 64 permissions.
Anything outside the schema is treated as no evidence.

### Introspection, revocation, replay, failure

- `status(credential_ref)` answers `active`, `expired` or `revoked`. Expired and revoked must be
  distinguishable: Legion replaces an expired credential and refuses a revoked one. Anything else
  (an error, a timeout, another answer) makes Legion refuse the call. Legion asks immediately
  before every dispatch, including retries, and asks even when its own clock says the credential
  has expired.
- `revoke(credential_ref)` is best effort from Legion's side (a refused or unused credential);
  the authority must make a revoked credential fail at the downstream system, not only in
  `status`.
- Replay: Legion refuses a reference seen before in the same run, and a bound credential is only
  good for its call. The authority should make a credential single-use or call-bound at the
  downstream system where it can; Legion can't enforce that downstream.
- Failure: any error or timeout from `issue` means no credential and no call. Error messages must
  not contain credential material; Legion records the error type only, but other systems may not.

### Trust and channel

- Legion trusts an authority only if the operator marks it `trusted`. The adapter is operator
  code, like a tool module.
- Without signed evidence, Legion is trusting the channel to the authority and the adapter. Signed
  evidence (asymmetric, with a key Legion is configured to trust, and a key id) would let Legion
  check statements without trusting the channel; Legion doesn't require it yet, and NIA doesn't
  produce it.

## B. Authenticated approval (optional, design only)

Today an approval's `by` is whoever ran the CLI, taken as given. That's not authentication, and
Legion doesn't claim it is. Trustworthy approval identity needs evidence from an identity provider
that a specific person approved a specific call:

| Field | Class | Notes |
|---|---|---|
| approver principal | critical | as the identity provider knows them |
| authentication method | informational | e.g. passkey, SSO session |
| identity provider | critical | which one; must be one Legion trusts |
| approval id and binding hash | critical | Legion's hash of the exact call (ADR 0014) |
| call id | critical | the call approved |
| decision and timestamp | critical | approve or deny, when |
| evidence reference | informational | the provider's id for the event |
| verification result | critical | computed by Legion from a signature or introspection, not asserted by the CLI |

The seam would be an `ApproverVerifier` that takes the provider's evidence and the binding hash
and returns a verified principal or refuses. It isn't implemented: nothing in Legion could produce
real evidence today, and an interface with only a test provider would add a claim without adding
security. `THREAT_MODEL.md` keeps saying approvers aren't authenticated.

## C. External checkpoints (optional, design only)

The event log is tamper-evident, not tamper-proof: anything that can write the store can cut a
run back or rewrite its chain. Detecting that needs state Legion's host can't change:

- Legion sends `(run_id, seq, chain_head)` to a checkpoint authority, at least at pauses, run end
  and after each approval.
- The authority keeps its own record, on storage the Legion host can't write, and returns a
  receipt that can be checked without trusting Legion (a signature over the tuple and the
  authority's time, with a published key).
- `legion verify` asks the authority for the highest checkpoint it holds for the run. Local
  `seq 80` against external `seq 100`, or a different head at the same seq, means rollback or
  rewrite.
- If the authority is unavailable: checkpointing at run end can be asynchronous with a retry
  queue, but `verify` must say "not checked against the authority", not "ok". A deployment that
  needs it can refuse to resume a run whose last checkpoint wasn't acknowledged.
- Remaining trust: the checkpoint authority itself, its key, and the window between the last
  checkpoint and a rollback.

An HMAC key stored outside the database is not this: whoever can verify with a symmetric key can
also forge, and checkpoints kept next to the chain can be rolled back with it. Nothing here is
implemented; a `CheckpointPort` can follow when there's an authority to talk to.

## D. MCP brokering (future)

An MCP server today holds one credential for the whole process. Per-call authority needs:

- a per-call delegated credential (token exchange per Action) with audience and resource bound to
  the downstream system
- a broker that holds the downstream credential and injects it per call, so the server has no
  standing credential
- evidence that the call used that credential, checkable by Legion
- proof of possession where bearer tokens could be replayed
- server-side or gateway enforcement that checks the credential when it's used

Until then Legion treats MCP credentials as `declared` or `unverified` (ADR 0019).

## NIA today against this contract

From reading NIA's code at `e6802a1` (not its README).

| Requirement | NIA today | Gap | Change size | Consequence of the gap |
|---|---|---|---|---|
| agent identity | agents registered by ref, with owner and state | no principal / on-behalf-of chain | S | Legion's principal and delegation chain can't be matched against NIA's |
| credential issuance | `POST /agents/{ref}/credentials`, random secret, hash stored | none for issuance itself | - | usable |
| credential scoping | none; a credential carries all of its agent's grants | scope field and enforcement | L | any NIA credential is at least agent-wide: wider than a call, refused |
| permission scope | grants are `tool`/`data` names, exact match | per-credential permission set | M | can't express `contents:read` for one call |
| resource scope | resources checked only for data classified sensitive | per-credential resource | M | resource widening can't be prevented by NIA |
| maximum TTL | optional `ttl_seconds`, no default or maximum | required short TTL with a ceiling | S | long-lived credentials are refused by Legion's lifetime check |
| Action binding | none | bind to `action_hash` | M | at most `verified`, never `bound` |
| call binding | none | bind to `call_id` | S (with the above) | same |
| Grant binding | none | bind to `grant_fingerprint` | S (with the above) | same |
| child credentials | `delegates_to` graph edge, not enforced | child credential as a subset of the parent's | L | delegation can't be narrowed at NIA |
| audience | none; MCP token is process-wide | per-provider audience | M | a credential isn't tied to one downstream |
| revocation | per credential, checked live on every gateway request | none | - | usable |
| kill state | per agent, sentinel plus cascade | no global kill; restore doesn't restore grants | S | usable through `IdentityPort` |
| introspection | none exposed; resolution happens inside the gateway | a status (introspect) endpoint that tells expired from revoked | S | Legion can't do its pre-dispatch check against NIA |
| credential evidence | issuance response has id, kind, times; no authority statement | evidence response per the contract | M | nothing to assess: unverified |
| operator authentication | static bearer tokens with global roles | per-agent scoping | M | informational for Legion |
| approval identity evidence | none | contract B | L | approvers stay unauthenticated |
| audit chain | one global SHA-256 chain, arguments not hashed | per-run or Legion-linked records | M | NIA's audit can't be matched to Legion calls |
| checkpoint independence | HMAC checkpoints, created on request, stored with the chain | asymmetric receipts on separate storage | M | not an independent anchor |
| verifiable receipts | none | signed decisions / evidence | L | Legion must trust the channel |
| MCP downstream brokering | one static token for all MCP calls | contract D | L | MCP stays declared/unverified |

What NIA can do for Legion today: identity, kill state (and so the kill checks in steps 1, 13 and
15), per-request authorization as an external authority (`POST /tools/{tool}/call` with no
downstream), issuance of agent-wide credentials, and revocation. What it can't: any credential
narrower than an agent, which is what Legion's central invariant needs.

## Phase 5B options

### A. Extend NIA to the full contract

- Security: the strongest. Credentials scoped and bound per call; NIA's gateway already sees every
  call through it, so it can check scope and revocation at use, which is the only thing that closes
  Legion's check-to-use window.
- NIA changes: scope, binding and TTL ceiling on credentials; child credentials; introspection;
  evidence responses; later signed evidence and per-call MCP credentials. Large, but each piece
  stands alone.
- Legion changes: a `CredentialAuthority` adapter and an `IdentityPort` adapter, both over HTTP.
- Coupling: through the two ports only; no NIA code in Legion core.
- Compatibility: additive in NIA (new fields and endpoints); existing agent-wide credentials keep
  working.
- Testability: NIA's in-process gateway tests plus Legion's hostile authority suite run against
  the adapter.
- Operations: one authority to run. Needs shared state (Postgres) between NIA's API and gateway,
  as NIA already does for credentials.
- MCP: gives the path to per-call MCP credentials through NIA's gateway.

### B. Use NIA as it is, at its real assurance

- Security: identity, kill and revocation through `IdentityPort`. Credentials from NIA would be
  agent-wide; Legion's comparison refuses them for per-call use, so in practice they'd be static
  secrets marked `unverified`.
- Changes: an `IdentityPort` adapter in Legion; nothing in NIA.
- Little effort, and nothing is overstated, but it doesn't move the central invariant at all.

### C. NIA for identity and revocation, a separate credential broker

- Security: could reach `bound` without changing NIA's credential model.
- Adds a second service that holds downstream credentials, with its own identity, storage and
  audit, duplicating what NIA's gateway already does. More to operate and to trust.

### D. Defer

- Keeps the static-credential honesty from 5A. No progress on effective authority.

### Recommendation

A, staged, with B's `IdentityPort` adapter first since it needs no NIA changes:

1. `IdentityPort` adapter over NIA (identity, kill state, authorize). Nothing in NIA changes.
2. In NIA: scoped, call-bound credentials with a TTL ceiling, an evidence response and
   introspection; Legion's `CredentialAuthority` adapter. This is the step that makes `bound`
   possible with a real authority.
3. In NIA: enforcement of credential scope and revocation at the gateway when the credential is
   used, for tools called through NIA.
4. Child credentials, signed evidence, and per-call MCP credentials, in that order.

A is the only option where the component that issues the credential is also one that can refuse
it at use, and NIA already has the gateway and revocation pieces that make that true. C would
build a second NIA. B is worth doing as step 1 but isn't a destination.
