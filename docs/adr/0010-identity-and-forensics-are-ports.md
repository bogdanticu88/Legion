# ADR 0010: NIA, MIA and forensics stay outside

Status: accepted

## Context

NIA (Go: gateway, credentials, kill switch, risk scoring) and MIA (Python: mandates and
delegation) already cover identity and authority across runs. A separate forensics project will
reconstruct incidents. Rebuilding any of that inside Legion would duplicate them and blur who's
responsible for what.

## Decision

Legion only enforces inside a run: task grants, budget, approvals. Everything else goes through
`IdentityPort` (identity, credentials, external authorization, kill state, delegation, evidence).
An action needs both Legion's grant and the external service to allow it. Legion never issues
identity credentials, never scores risk across runs, and works with no identity service at all.

For forensics Legion just writes good, chained events and says clearly that they're its own
account.

## Consequences

- A NIA adapter can also send tool calls through NIA's gateway, so getting around Legion doesn't
  get around NIA.
- Phase 1 has the interface and a null implementation; adapters come in Phase 7.
