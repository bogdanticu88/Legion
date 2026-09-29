# Roadmap

Each phase should leave the repo working, tested and documented.

I moved crash recovery ahead of delegation and MCP because adding it late is how writes end up
running twice. Approvals moved in with it because an approval needs a run that can pause.

| Phase | What | Done when |
|---|---|---|
| 0 | Assessment, architecture, ADRs, threat model | Docs agree with each other |
| 1 | One agent per run. Scripted, OpenAI-compatible and Anthropic providers. Native tools, the action pipeline, grants, policy, budgets (steps, calls, tokens, cost, wall clock), SQLite event log with hash chain, retries, repeat detection. CLI: `init`, `providers`, `agent validate`, `run`, `runs`, `inspect`, `verify` | Runs end to end without an API key. Every refusal path has a test. Budget overruns end the run. A planted secret never shows up in the database. Editing a stored event fails `verify`. State rebuilt from storage matches the live run |
| 2 | Resume after a crash, in-doubt handling by effect class, approvals (`approve`, `deny`, `resume`) tied to the action hash, single use, with expiry | Killing the process mid-tool and resuming does the right thing. An approved action with changed arguments is refused. Expired approvals are refused |
| 3 | `delegate` tool, narrower child grants, child budgets carved from the parent, parallel children, cancellation, limits on depth, fan-out and concurrency. A second, non-security example | Property tests show no child ever gets a capability or budget its parent lacked, and total spend never goes over the root budget |
| 4 | Gemini adapter. Gateway headers, workload identity for one cloud, OAuth where the provider documents it. One external runtime (Copilot SDK or Claude Agent SDK) | The same agent runs on three bindings without changes. Missing features stop the run from starting. Results from external runtimes are marked in the events |
| 5 | MCP through the official SDK, operator manifests, pinned description hashes, reconnects | A changed tool description gets blocked. Tools without a manifest entry are refused |
| 6 | Security-operations example: an orchestrator plus endpoint, identity and threat-intel agents on simulated tools, with containment behind approval | Runs the same way every time on the scripted provider, and optionally on a real model |
| 7 | Versioned event schema, JSONL and OpenTelemetry export, NIA adapter and NIA gateway as a tool transport, maybe MIA | A separate repo can compute success, authority compliance, cost and latency from the exports alone |
