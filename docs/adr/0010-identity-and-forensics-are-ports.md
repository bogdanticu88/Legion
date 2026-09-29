# ADR 0010: NIA, MIA and forensics are ports, not modules

Status: accepted

## Context

NIA (a Go control plane with a gateway, credentials, kill switch and risk scoring) and MIA (a
Python mandate and delegation service) already cover identity and cross-run authority. A
separate forensics project will reconstruct incidents. Rebuilding any of them inside Legion
would duplicate work and blur the security boundary.

## Decision

Legion enforces inside one run: task grants, the ledger, approvals. External authorities are
reached through `IdentityPort` (agent identity, credentials, external authorization, kill state,
delegation notification, evidence references). The effective permission is Legion's grant
intersected with the external decision. Legion never mints identity credentials and never scores
risk across runs. Legion must work with no identity service at all.

For forensics, Legion only emits well-formed, chained events and documents that they are the
harness's own account.

## Consequences

- A NIA adapter can also route tool execution through NIA's gateway, so bypassing Legion does not
  bypass NIA.
- The ports exist in Phase 1 as protocols with a null implementation; adapters come in Phase 7.
