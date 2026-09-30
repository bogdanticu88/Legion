# ADR 0020: NIA as identity authority, through IdentityPort

Status: accepted (Phase 5B.1)

## Context

Legion has had an `IdentityPort` since Phase 1 with only a null implementation. NIA, a separate
service, keeps agent identities and a kill switch. This phase connects the two for identity and
kill state only. It doesn't touch credentials: NIA can't issue a credential narrower than an
agent (docs/nia-integration-requirements.md), so it isn't a `CredentialAuthority`.

NIA as read at commit `e6802a1`, through its API rather than its code or storage:

- `GET /agents/{ref}` (read permission) returns the agent's record plus `effective_state` and
  `kill_sentinel_checked`. `effective_state` is `killed` when NIA's live check of its kill
  sentinel says so; `kill_sentinel_checked` is false when that live check itself failed. 404
  means the agent isn't registered.
- `POST /policy/kill` sets the kill sentinel, deletes the agent's grants and revokes its
  credentials. `POST /policy/restore` clears the sentinel only; grants and credentials stay gone.
- `suspended` exists as a state but nothing in NIA sets it. There's no agent-level disable.
- There's no per-action decision an operator-authenticated caller can ask for; NIA's gateway
  decides tool calls made with the agent's own credential, which Legion doesn't use.
- Delegation exists only as an unenforced graph edge.

## Decision

`legion.adapters.nia.NiaIdentityPort` implements `IdentityPort` over HTTP. Nothing in Legion's
kernel, domain or ports imports it; the config loader builds it when asked to.

```yaml
identity:
  provider: nia
  endpoint: https://nia.internal:8080   # NIA's control plane; plain http only to 127.0.0.1 or [::1]
  credential: env:LEGION_NIA_TOKEN      # an operator token with the viewer role is enough
  timeout_s: 5                          # one attempt, start to finish
  attempts: 2                           # only for connection failures and 502/503/504
  agents:                               # Legion agent name -> NIA agent ref
    notes-assistant: agent:notes
    helper: agent:helper
```

### How each IdentityPort call maps

| Legion | NIA | Notes |
|---|---|---|
| `agent_identity(name)` | mapping, then `GET /agents/{ref}` | no mapping: `identity_unknown`, the run doesn't start. 404: `identity_unknown`. Returns the NIA ref as `external_id` |
| `kill_state(identity)` | `GET /agents/{ref}` | `active` is active; `killed` and `suspended` are killed; anything else, or no confirmed live check, is `identity_unavailable` |
| `authorize(action, identity)` | `GET /agents/{ref}` | identity state only, checked again for this call: allowed if active. NIA has no per-action decision for Legion to ask for |
| `on_delegation(parent, child)` | `GET /agents/{ref}` for both | refuses the delegation unless both are mapped and active. NIA doesn't record the delegation |
| `evidence`, `credential_evidence` | nothing | NIA isn't a credential authority here |

The ref always comes from the mapping in `legion.yaml`, looked up by the Legion agent's name.
Nothing the model, a tool or a child says can choose it, and an identity carrying a different
`external_id` than the mapping gives is refused. Refs are 1 to 128 characters from
`[A-Za-z0-9._:@-]`, with no slash and not made only of dots, since each one is one URL path
segment (percent-encoded) and a URL library resolves `.` and `..`. The endpoint can't carry a
query, fragment or parameters, and its port has to be valid.

### Answers Legion accepts

A 200 whose body is uncompressed, a JSON object, at most 64 KiB, with no NaN, Infinity or number too
large for a float anywhere in it, whose `Ref` is the ref asked about, whose `effective_state` is `active`, `killed` or `suspended`, and whose
`kill_sentinel_checked` is `true`. Everything else fails closed:

| NIA answers | Legion |
|---|---|
| 404 | `identity_unknown` |
| 401, 403 | `identity_unavailable` ("refused Legion's credential") |
| 502, 503, 504, connection refused or dropped, timeout | tried again up to `attempts`, then `identity_unavailable`. One lookup can take up to `attempts` times `timeout_s`, plus a fraction of a second between tries |
| any other status, including redirects (not followed) | `identity_unavailable` |
| another agent's record, a non-object, bad JSON, compressed, too large, unknown state, unchecked kill sentinel | `identity_unavailable` |

`identity_unknown` and `identity_unavailable` are fatal: at the start of a run the run doesn't
start; during a run it fails, and anything in flight is marked in doubt as with any fatal error.
NIA being unavailable never turns into local allow, and the adapter never switches itself off. A
retry can only repeat the question; it can't make an unconfirmed answer acceptable.

Nothing NIA sends is passed on except the ref Legion asked about and the state. Error messages are
built from Legion's own values and status codes; no response body, header or library error text
is repeated, because any of them could carry the token. The token is added to the request as it
goes out (so it's never a local variable a traceback would show), must be printable ASCII, goes
only in the `Authorization` header and only to the configured endpoint, and is scrubbed from
events like any configured secret. One thing outside Legion's control: with DEBUG logging on,
httpcore logs response headers as they arrive, so a NIA (or proxy) that echoed the token in a
header would put it in the log.

### When NIA is asked

Every place Legion already checks kill state: before each model call, at the start of each tool
call, after the budget charge, and immediately before `tool.started` (ADR 0019); `authorize` asks
once more per call. A run that's resumed, after a pause or a crash, looks its identity up again
before anything else, so a kill while it was paused or down is seen before any new call. An
in-doubt write stays in doubt whatever NIA says; a kill doesn't make it safe to repeat, and an
active answer doesn't either.

### Delegation

Each child agent needs its own mapping and its own active NIA identity. A child isn't checked
under its parent's ref, and an unmapped, unknown or killed child is refused at the delegation
step, before it exists. A child that already exists and is picked up again on resume isn't
delegated again: if its identity has gone, the run fails (`identity_unknown` or `killed`). Once running, a child's kill checks cover its own ref and every
ancestor's. If two Legion agents are mapped to the same NIA ref, that's the operator's explicit
choice and NIA can't tell them apart.

### Events

`action.authorized.external` records `provider`, `ref`, `state` and `checked_at` for the decision
the call ran under. A kill ends the run with `killed` and a message naming the NIA ref.
`identity_unknown` and `identity_unavailable` appear as the run's error code.

## Trust boundary

- Legion trusts NIA's answers about identity and kill state. A compromised NIA can let a killed
  agent run or stop a healthy one.
- The answers come over HTTP. Over plain http (allowed only to a literal loopback address) or a network Legion
  doesn't control, whoever can change the traffic can change the answers. Nothing NIA sends is
  signed.
- NIA saying an agent is active says nothing about what that agent's credentials can do
  downstream; that's ADR 0019, and NIA isn't the credential authority for it.
- A kill in NIA stops Legion at its next check. It doesn't undo a call already dispatched, and it
  doesn't close the window between Legion's last check and the downstream use.
- Kill state is only as current as NIA's own: NIA's API and gateway only agree when they share
  their policy backend and credential store, and NIA's in-memory backend forgets kills when it
  restarts (a re-registered agent then reads as active).
- Plain http is allowed only to a literal loopback address. The name `localhost` isn't, because
  it resolves to both 127.0.0.1 and ::1 and whatever listens on the one the client picks gets the
  token. Even a literal loopback address trusts every local process: with NIA bound to a
  wildcard address, another process can take the loopback address on the same port on some
  systems (seen on macOS). Use https, or bind NIA to the specific loopback address.

## Other behaviour worth knowing

- `suspended` (which NIA never sets today) is treated as killed, and reported as `killed`.
- A killed agent's run is created before the first kill check refuses it, so it shows up in
  `legion runs` as failed with `killed`.
- Changing an agent's mapping and resuming a run moves that run to the new ref; approvals given
  before the change don't cover calls after it, since the approval binding includes the ref.
- Every `action.authorized` event now has an `external` field; it's null without an identity
  authority.

## Consequences

- Legion runs as before without an `identity` block.
- NIA is asked several times per tool call. Each is one GET; `timeout_s` bounds each.
- Everything above is tested against a stand-in NIA that misbehaves on request
  (`tests/nia_lab.py`) and against a real `nia-api` binary (`tests/integration/test_nia_real.py`,
  set `LEGION_TEST_NIA_BIN`).
