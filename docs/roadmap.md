# Roadmap

Legion is a harness for running agents with real permissions: every action goes through one
checked path, authority only narrows, spending is capped, risky actions wait for a person, and a
crash doesn't make an uncertain write run twice. The next phases build a host agent with
specialists on top of that, taking some ideas from Raven (see [assessment.md](assessment.md))
without trying to be a harness of other harnesses.

Each phase should leave the repo working, tested and documented.

## Done

| Phase | What |
|---|---|
| 0 | Framework notes, architecture, ADRs, threat model |
| 1 | One agent per run. Scripted, OpenAI-compatible and Anthropic providers. Native tools, the action pipeline, grants, policy, budgets, SQLite event log with hash chain, retries, repeat detection. CLI: `init`, `providers`, `agent validate`, `run`, `runs`, `inspect`, `verify` |
| 2 | Resume from the log after a pause or crash. Effect-aware handling of interrupted calls, operator reconciliation for in-doubt writes. Approvals bound to one call, single use, with expiry. Per-run file locks. CLI: `resume`, `approvals`, `approval show`, `approve`, `deny`, `reconcile` |
| 3 | Delegation. A `delegate` tool that makes a child task under a narrower grant with budget reserved from the parent; depth, fan-out and task limits; derived child identity; results returned as summaries; pause, resume, crash recovery and approvals across the task tree. CLI: `tasks`. Children run one at a time |
| 4 | MCP tools through the official SDK. Operator manifest per server; pins over server identity and each tool's description, schemas and annotations, checked at startup and before each call; effect classes from the manifest, defaulting to irreversible; response limits; failures classified as clean, retryable or in doubt; credential scope recorded on every call. Hostile-server test lab. CLI: `mcp inspect`. Tools only |
| 5A | Credentials checked against the call. A `CredentialAuthority` port issues a credential per call from an operator mapping; Legion builds the request from the authorized Action, refuses anything wider, decides assurance (`unverified`, `declared`, `verified`, `bound`) and enforces a minimum set in config or policy, including for MCP. Credentials are issued after budget and kill checks and checked again just before dispatch and before each retry; expiry is replaced, revocation isn't. Static credentials recorded as unverified. Hostile authority lab, demo, NIA integration requirements. CLI: `credentials`. Native tools only |
| 5B.1 | NIA as identity authority through `IdentityPort`: explicit agent-to-ref mapping, kill state from NIA's live check, fail closed on anything unconfirmed, children need their own identity. Hostile NIA stand-in and a test against a real `nia-api`. No NIA changes. Not a credential authority |

## Next

| Phase | What | Done when |
|---|---|---|
| 3b | Several children at once. Budget reservations already make this safe on paper; what's missing is running them concurrently, cancelling siblings when one fails, and ordering their events | Property tests over random concurrent trees; a crash with three children mid-flight resumes all three without duplicates |
| 5B.2+ | NIA as credential authority: scoped, call-bound credentials with status in NIA and a `CredentialAuthority` adapter, then enforcement at NIA's gateway ([requirements](nia-integration-requirements.md)) | The hostile-authority suite passes against the NIA adapter; a call gets a `bound` credential from a real NIA and a widened one is refused |
| 6 | Governed plans. The host writes its plan as a recorded object; a person can approve the plan once, and the harness refuses steps that fall outside it | A step outside the approved plan is refused; changing the plan needs a new approval |
| 7 | Built-in specialist agents (research, code, and the security-operations set) and a second, non-security example | The same host runs both examples without core changes |
| 8 | Memory with provenance. Remembered facts record where they came from; anything that came from untrusted input is marked and can't quietly steer later runs | A fact from tool output can't be used as an instruction without being labelled |
| 9 | Event export (JSONL, OpenTelemetry) and NIA's gateway as a tool transport | A separate repo computes success, authority compliance, cost and latency from exports alone |

## Not planned

- Driving other harnesses (Claude Code, Codex, Copilot) as Legion agents. If it happens at all it
  will be as untrusted workers whose results are marked as such (ADR 0006).
- A self-modifying harness.
- Multiple hosts sharing one store, a scheduler, a web UI.
