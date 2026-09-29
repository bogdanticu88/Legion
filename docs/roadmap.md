# Roadmap

Each phase ends with the repository runnable, the tests green and the documentation updated.
Order changed from the first draft: durability moved before delegation and MCP because adding it
later is the usual way side effects end up running twice, and approvals moved in with durability
because an approval needs a run that can pause.

| Phase | Scope | Acceptance criteria |
|---|---|---|
| 0 | Assessment, architecture, ADRs, threat model, interfaces | Documents agree with each other |
| 1 | Single agent. Scripted, OpenAI-compatible and Anthropic providers with API-key and no-auth access. Native tools. Action pipeline. Grant, policy, ledger (steps, model calls, tool calls, tokens, cost, wall clock). Event store in memory and SQLite with hash chain. Failure dispositions, bounded retries, repeat detection. CLI: `init`, `providers`, `agent validate`, `run`, `runs`, `inspect`, `verify` | End-to-end run with no API key. Every denial path tested. Budget exhaustion ends the run with `budget.exceeded`. A sentinel secret never appears in the database. Chain verifies and a tampered row fails verification. Projections rebuilt from storage match the live run |
| 2 | Resume from events after a crash. In-doubt handling by effect class. Approval pause, `approve`, `deny`, `resume`. Action-hash binding, single use, expiry | A process killed during a tool call resumes correctly. An approved action with changed arguments is refused. An expired approval is refused |
| 3 | `delegate` built-in, attenuation, ledger carve-out, parallel children, cancellation and failure propagation, depth, fan-out and concurrency limits. A second, non-security example | Property tests: no child ever holds a capability or budget its parent lacked, and total spend never exceeds the root budget over random delegation trees |
| 4 | Gemini adapter. Access providers beyond API keys: gateway headers, workload identity for one cloud, OAuth only where documented. One `ExternalAgentRuntime` (Copilot SDK or Claude Agent SDK) with a declared guarantee level | The same agent runs unchanged on three bindings. A capability mismatch refuses to start. External results are marked in events |
| 5 | MCP adapter on the official SDK. Operator manifests, pinned description hashes, disconnect handling | A changed tool description is blocked. An unmanifested tool is refused |
| 6 | Security-operations reference application: orchestrator plus endpoint, identity and threat-intelligence investigators, simulated tools, containment behind approval | The scenario runs deterministically on the scripted provider and optionally on a real model |
| 7 | Versioned event schema, JSONL export, OpenTelemetry GenAI span export, NIA `IdentityPort` adapter and NIA gateway tool transport, optional MIA adapter | A separate repository computes success, authority compliance, cost and latency metrics from exports alone |
