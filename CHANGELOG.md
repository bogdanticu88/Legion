# Changelog

## Unreleased

### Phase 4

- MCP tools through the official Python SDK, as the optional extra `mcp`. Servers and the tools
  to use from them are declared under `mcp_servers` in `legion.yaml`; nothing else a server
  offers is registered.
- Each MCP tool is `mcp_<server>_<tool>` and needs `mcp.<server>.<tool>`, so it's granted,
  offered, checked, approved and budgeted like a native tool.
- Pins over the server's identity and each tool's name, description, schemas and annotations,
  checked at startup and before every call. A changed tool is blocked for the rest of the
  process and nothing is sent.
- Effect class from the manifest, defaulting to `external_irreversible`. Annotations are recorded
  and ignored. Failures before sending are clean, after sending they're retried for reads and in
  doubt for anything else.
- Results reduced to text, with size, depth and item limits; images and blobs omitted, links not
  fetched, requests for more input end as errors.
- `action.proposed` has a `remote` field with the server, pin and declared credential scope, plus
  credential evidence from `IdentityPort.credential_evidence` when there is any. Approvals are
  bound to the tool's origin.
- `legion mcp inspect <server>`. Stdio server stderr goes to `.legion/mcp/<server>.stderr.log`.
- Tests: a hostile in-process MCP server (poisoned descriptions and results, tools appearing,
  vanishing and changing, duplicates, bad schemas, oversized and odd responses, timeouts,
  dropped connections, lying errors, input requests), approvals and delegation with MCP tools,
  and a real stdio server that dies mid-write, recovered through the CLI.

### Phase 3

- Delegation: the built-in `delegate` tool makes a child task that runs under a narrower grant.
  Capabilities must be within the parent's grant and the child agent's spec; limits come out of
  what the parent has left and are reserved, then settled when the child ends; depth, fan-out
  and tasks per run are capped; identity is derived by the harness.
- Children share the run's log, lock and resume. A child that needs approval or has a write in
  doubt pauses the run; other failures come back to the parent as a result. Killing an ancestor
  stops the subtree. Children run one at a time.
- `legion tasks` shows a run's task tree. `legion.yaml` gets `agents_dir`,
  `authority.max_delegation_depth` and `authority.max_tasks`. The starter project has a
  coordinator agent.
- An empty model response is now treated as malformed (seen with qwen2.5:1.5b on Ollama).
- Tests: delegation attenuation, budget, limits, identity, approvals, crashes and cancellation;
  property tests over random delegation trees, with and without crashes.

### Phase 2

- `legion resume` continues a paused or crashed run from its event log, through the same loop and
  pipeline as a fresh run. Interrupted safe calls run again; interrupted writes are marked in
  doubt and wait for `legion reconcile`.
- Approvals: policy can require one, the run pauses, and `legion approvals`, `approval show`,
  `approve` and `deny` handle it. An approval is bound to a hash of one call, used once, and
  expires.
- In-doubt writes during a live run now pause the run instead of failing it.
- One process per run, using OS file locks next to the store.
- Wall-clock time recorded per active stretch and carried across pauses and crashes.
- Model responses and their token charges written in one append.
- Tool call ids made unique within a task; `ctx.idempotency_key` for tools.
- Starter project has a second agent that needs approval.
- Fixes: negative or non-integer token counts from providers are rejected, non-list `tool_calls`
  are reported as malformed, CLI output from runs is escaped, `legion runs` shows real statuses.
- Tests: resume at each interruption point, approval tampering and replay, a real process killed
  mid-write, fuzzed model output, and property tests for recovery.

### Phase 1

- Agent loop, action pipeline and runtime. Every tool call goes through the pipeline.
- Capabilities with resource globs, grants with `attenuate` (tested ahead of delegation),
  rule-table policy, budgets for steps, model calls, tool calls, tokens, cost and wall clock.
- Append-only event log in SQLite or memory, hash-chained per run. Run state, transcript and
  budget use are rebuilt from it.
- OpenAI-compatible and Anthropic adapters on httpx, plus a scripted provider. API key or no auth.
  Secrets stay as references and get scrubbed from events and tool output.
- Retries with backoff charged to the budget, repeat detection, tool retries only where the effect
  class allows, and in-doubt records for interrupted writes.
- CLI: `init`, `providers`, `agent validate`, `run`, `runs`, `inspect`, `verify`.
- Unit tests, Hypothesis invariant tests, optional real-model tests.

### Phase 0

- Framework notes, architecture, ADRs 0001 to 0012, threat model, roadmap.
