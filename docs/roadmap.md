# Roadmap

Legion is a runtime for agents with real permissions: every action goes through one checked path,
authority only narrows, spending is capped, risky actions wait for a person, and a crash doesn't
make an uncertain write run twice. It isn't trying to become a general agent framework.

Each step should leave the repository working, tested and documented. Nothing below is a promise
of a date.

## In 0.1.0a1

- One agent loop with native tools, the action pipeline, grants, policy rules, budgets, and a
  SQLite event log with a hash chain; retries and repeat detection. Scripted, OpenAI-compatible
  and Anthropic providers.
- Resume from the log after a pause or crash, with effect-aware handling of interrupted calls and
  operator reconciliation of in-doubt writes. Approvals bound to one exact call, single use, with
  expiry.
- Delegation: a child task under a narrower grant with budget reserved from the parent; depth,
  fan-out and task limits. Children run one at a time.
- MCP tools through the official SDK, with operator manifests, pins checked before every call,
  and hostile-server tests. Tools only.
- Credentials checked against the call: a `CredentialAuthority` port, assurance levels decided by
  Legion, a minimum set in config or policy, a check just before dispatch, and Legion's own call
  id as the binding.
- Optional NIA integrations: NIA as identity authority (kill state) and as credential authority
  (scoped per-call credentials).

The development history, step by step, is in [CHANGELOG.md](../CHANGELOG.md) and the ADRs.

## Next

| What | Done when |
|---|---|
| Several children at once. Budget reservations already make this safe on paper; what's missing is running them concurrently, cancelling siblings when one fails, and ordering their events | Property tests over random concurrent trees; a crash with three children mid-flight resumes all three without duplicates |
| Governed plans. The host writes its plan as a recorded object; a person can approve the plan once, and the harness refuses steps that fall outside it | A step outside the approved plan is refused; changing the plan needs a new approval |
| Closing the credential check-to-use window for tools called through NIA's gateway: the gateway checking a credential against the call it was issued for. Needs a NIA change ([requirements](nia-integration-requirements.md)) | A credential presented for a different call to the same tool and resource is refused at the gateway |
| Built-in specialist agents and a second, non-security example | The same host runs both examples without core changes |
| Memory with provenance. Remembered facts record where they came from; anything from untrusted input is marked and can't quietly steer later runs | A fact from tool output can't be used as an instruction without being labelled |
| Event export (JSONL, OpenTelemetry) | A separate repository computes success, authority compliance, cost and latency from exports alone |

Smaller items that would help people using the alpha: a crash scenario that can be driven from
the CLI, loading identity authorities and model providers from `legion.yaml` the way credential
authorities are, filters for `legion inspect`, and testing on Python 3.14 and Windows.

## Not planned

- Driving other harnesses (Claude Code, Codex, Copilot) as Legion agents. If it happens at all it
  will be as untrusted workers whose results are marked as such (ADR 0006).
- A self-modifying harness.
- Multiple hosts sharing one store, a scheduler, a web UI, a connector catalogue.
