# Changelog

## 0.1.0a1 (not yet released)

The first public alpha. Legion works and is extensively tested; it is not production software.

What it does:

- **Bounded execution.** Every tool call is an Action checked in one pipeline against the task's
  grant (capabilities scoped to resources), policy rules and budget before any tool code runs.
  Refusals go back to the model as results, and `legion run` says what Legion refused.
- **Exact approvals.** Policy can require a person for a call; the approval is bound to that
  exact call (arguments, target, grant, tool definition, settings) and used once.
- **Recovery without repeated side effects.** Runs resume from an append-only, hash-chained
  SQLite log. After a crash, safe calls run again; a write that may have happened is left in
  doubt for an operator to reconcile.
- **Bounded delegation.** A child agent gets a narrower grant and a share of the parent's
  budget; depth, fan-out and task counts are capped.
- **MCP under the same rules.** Tools from MCP servers are used only if the operator pinned them,
  and go through the same checks.
- **Per-call credentials.** A credential authority issues a credential for one call; Legion
  checks the evidence against what the call was authorized for, refuses anything wider, and
  records the assurance it reached. Credentials are bound to Legion's own call id, not the one
  the model sent.
- **Optional NIA integrations.** An identity authority (kill state) and a credential authority
  (scoped per-call credentials) for NIA, a separate project. Legion doesn't need NIA.

New for this release, compared with the last development snapshot:

- `legion run` and `legion resume` end with what Legion refused (`completed; Legion refused 1
  action (capability_denied)`), and `--json` includes a `refused` list.
- Clearer errors for a missing API key (which provider, which variable, how to fix), a literal
  secret where a reference belongs (which key, never the value), a broken file in `agents/`, an
  unknown tool (lists the ones that exist) and the missing MCP extra (uv and pip).
- Tool and credential authority modules can use dataclasses: modules loaded from `legion.yaml`
  are registered before they run.
- `legion approval show` and `legion credentials` show Legion's call id first, with the model's
  id beside it.
- `legion init <dir>` prints the next commands, on one line each so they can be copied.
- `legion --version`; `py.typed`; one version source.
- Docs: a rewritten README, [docs/writing-tools.md](docs/writing-tools.md),
  [docs/extending.md](docs/extending.md), [examples/authorities.py](examples/authorities.py) (a
  minimal credential authority held to the conformance tests), and the complete limitations list
  in THREAT_MODEL.md.

Known limitations (complete list in THREAT_MODEL.md): one process on one machine with SQLite;
native tools run unsandboxed; approvers aren't authenticated; the log is tamper-evident, not
tamper-proof; credentials are checked just before dispatch, not at use, and authority evidence
isn't signed; resources are compared as strings; children run one at a time; tested with Python
3.12 and 3.13 on Linux and macOS.

## Development history

Written as the work happened, one milestone at a time, and kept as written. Later entries
sometimes change what earlier ones say; the section above describes the release.

### NIA as credential authority

- NIA as a credential authority: `legion.adapters.nia_credentials.NiaCredentialAuthority` behind
  the generic `CredentialAuthority` port, configured with `provider: nia` under
  `credential_authorities`, with its own issuer-role token and the identity block's agent map.
  NIA's evidence is translated into Legion's generic shape and judged by Legion's own assessment.
  A call without a resource gets no NIA credential (ADR 0021).
- Legion's own call id (`legion_call_id`), derived from the event log, replaces the model's tool
  call id as what credentials and approvals bind to. Approvals requested before the change are
  invalidated on resume.
- Generic additions: an optional external principal on credential requests and evidence,
  `UnknownCredential`, and `IssuedCredential.also_scrub`. When evidence names a different
  reference than the one handed over with the secret, the handed-over one is revoked.
- Neither NIA adapter takes proxy settings from the environment.

### NIA as identity authority

- NIA as identity authority: `legion.adapters.nia.NiaIdentityPort`, configured under `identity:`
  in `legion.yaml` with an explicit map from Legion agent names to NIA refs. It asks NIA's
  `GET /agents/{ref}` for identity and kill state, before model calls, tool calls and dispatch
  and after a resume, and fails closed (`identity_unknown`, `identity_unavailable`) on anything it
  can't confirm. Children need their own mapped, active identity.
- `action.authorized.external` records the external decision (provider, ref, state, time).
- Endpoints that get credentials over plain http must name a literal loopback address (127.0.0.1
  or [::1]); the name `localhost` is refused, since it can reach a different listener than meant.
  This applies to model providers with an API key and MCP servers too.
- A stand-in NIA for tests (`tests/nia_lab.py`) and an integration test against a real `nia-api`
  (`LEGION_TEST_NIA_BIN`). No NIA changes; NIA wasn't used for credentials yet (see NIA as credential authority).

### Credential authorities

- A `CredentialAuthority` port, separate from `IdentityPort`, issues a credential per call from
  an operator mapping (capability to provider permissions). Legion builds the request from the
  authorized Action, checks the evidence that comes back and decides the assurance: `unverified`,
  `declared`, `verified` or `bound`. Anything wider than the request is refused.
- Credentials are now resolved after the budget charge and a second kill check, just before the
  tool starts, and recorded as `credential.resolved` or `credential.refused` (never the secret).
- Static `env:` credentials keep working and are recorded as `unverified`;
  `credential_policy.minimum` can refuse them. MCP provenance records `credential_assurance`.
- Policy rules can set `credential_assurance`; the requirement is the highest of the global
  minimum, the mapping and matching rules, and failing to meet it refuses the call. MCP calls
  are held to it too, with the server's credential counting as declared or unverified.
- Before every attempt, including retries, a held credential is checked: identity not killed,
  inside its lifetime, still active at the authority. An expired one is replaced; a revoked one
  refuses the call. A credential that expires just before the call starts is replaced once.
- Just before dispatch, and before every retry: each credential is checked for expiry and with its
  authority for revocation, then the kill state once more. Unknown or unreachable counts as not
  active. A refused credential is revoked and its secret still scrubbed.
- Evidence is bounded and cleaned before it's recorded; the whole life of a credential, not only
  what's left, has to fit the maximum lifetime; bound credentials are tied to the call id as well
  as the Action hash and Grant.
- `credential.resolved`/`credential.refused` record principal, Grant, requested and evidenced
  authority, times, references and whether the evidence was wider. `legion credentials <run-id>`
  shows them.
- `examples/credential_demo.py` (eight cases), `docs/nia-integration-requirements.md` (the
  credential-authority contract, designs for authenticated approvals and external checkpoints, NIA
  gap analysis and the ways it could be integrated).
- `IdentityPort.credential()` removed (it was never called). The MCP server credential claim is
  `ServerCredentialClaim`, and its `verified` field is now `claimed_verified`.

### Package name

- The distribution is now called `legion-runtime`, because `legion` on PyPI is an unrelated
  project. The import package and the `legion` command are unchanged. Nothing is published.

### Security review fixes

- Schemas can't reach outside themselves. jsonschema used to follow a `$ref` to a URL or a
  `file://` path, so a server-written tool schema could make Legion send a request or read a
  local file into an error message. Validation now never retrieves anything, and tool and agent
  schemas with an outside `$ref` are refused.
- Secret scrubbing covers every secret the configuration references (tool credentials, model API
  keys, MCP env and headers) from the start of a run and after a resume, dictionary keys
  included, longest value first. Provider errors no longer carry the request's auth header.
- `legion.yaml` and agent files: repeated keys, YAML aliases, non-finite numbers, bad store paths
  and trailing newlines in names are refused; policy rules that match no tool or capability are
  refused; provider URLs are checked like MCP URLs and can't carry credentials; config errors
  don't repeat rejected secrets; the tool-setting check also covers paths that don't exist yet,
  `legion.yaml`, tool modules and the agents directory. Granted patterns can't contain `..`.
- MCP: `cwd` is relative to `legion.yaml`, stdio/http leftovers are refused, the stderr log is
  mode 0600, `mcp inspect` warns about `npx`/`uvx` packages without a version, and the missing-SDK
  error no longer points at the unrelated `legion` package on PyPI.
- Events: `verify` checks run ids, the `seq` and `type` columns and duplicated JSON keys;
  `inspect` refuses a broken chain; `approvals` says which runs it left out. A run's first three
  events are one append, and a crash between asking for approval and pausing is recorded as a
  pause on resume.
- CI has a job without the `mcp` extra. `uv run pytest -m demo` runs five demonstrations
  (docs/demos.md).
- Docs corrected where they claimed more than the code does: scrubbing, retries of
  `write_idempotent` MCP tools, what pins cover, what resume re-runs, and what the hash chain
  detects.

### MCP tools

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

### Delegation

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

### Resume and approvals

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

### Kernel

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

### Design

- Framework notes, architecture, ADRs 0001 to 0012, threat model, roadmap.
