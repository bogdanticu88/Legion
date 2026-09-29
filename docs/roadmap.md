# Roadmap

Legion is a harness for running agents with real permissions: every action goes through one
checked path, authority only narrows, spending is capped, risky actions wait for a person, and a
crash never makes an uncertain action run twice. The next phases build a host agent with
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

## Next

| Phase | What | Done when |
|---|---|---|
| 3b | Several children at once. Budget reservations already make this safe on paper; what's missing is running them concurrently, cancelling siblings when one fails, and ordering their events | Property tests over random concurrent trees; a crash with three children mid-flight resumes all three without duplicates |
| 4 | Governed plans. The host writes its plan as a recorded object; a person can approve the plan once, and the harness refuses steps that fall outside it | A step outside the approved plan is refused; changing the plan needs a new approval |
| 5 | Built-in specialist agents (research, code, and the security-operations set) and a second, non-security example | The same host runs both examples without core changes |
| 6 | Memory with provenance. Remembered facts record where they came from; anything that came from untrusted input is marked and can't quietly steer later runs | A fact from tool output can't be used as an instruction without being labelled |
| 7 | MCP through the official SDK, with operator manifests and pinned description hashes | A changed tool description is blocked; unlisted tools are refused |
| 8 | Event export (JSONL, OpenTelemetry), NIA identity adapter and NIA gateway as a tool transport | A separate repo computes success, authority compliance, cost and latency from exports alone |

## Not planned

- Driving other harnesses (Claude Code, Codex, Copilot) as Legion agents. If it happens at all it
  will be as untrusted workers whose results are marked as such (ADR 0006).
- A self-modifying harness.
- Multiple hosts sharing one store, a scheduler, a web UI.
