# Architecture

Legion is a Python library with a CLI. It runs in one process on asyncio and stores its events in
SQLite. The external things it talks to are the model endpoint and any MCP servers the operator
configures (as subprocesses or over https). Parts marked with a phase aren't built yet; see [docs/roadmap.md](docs/roadmap.md).

## Layers

```
MODEL        remote API, gateway or local server
AGENT        spec: instructions, tools, capabilities, budget
HARNESS      Legion: runs the agent, checks and records every action
APPLICATION  your code: defines agents and tools, starts runs
```

`src/legion` has no domain logic in it. Examples (including the planned security one) live under
`examples/`.

## Main pieces

```
legion run
    │
Runtime            builds the run, root task and grant, picks the model
    │
AgentLoop          ask model ─▶ tool calls ─▶ pipeline ─▶ results ─▶ ask model ...
    │                  │                          │
    │           ModelProvider                ActionPipeline ─▶ Tool
    │
EventStore         append-only, hash chain per run
    │
RunState           statuses, transcript, budget use, in-flight actions (all from events)
```

Ports (swap the implementation, keep the interface): `ModelProvider`, `AccessProvider`,
`CredentialResolver`, `Tool`, `PolicyDecisionPoint`, `IdentityPort`, `EventStore`,
`ArtifactStore`.

The types worth knowing:

- `AgentSpec`: the agent definition. Immutable.
- `TaskSpec`: objective, context, constraints, deadline. Task status lives in the events.
- `ToolSpec`: schema, required capabilities, effect class, timeout, credential names.
- `Action`: one proposed tool call in canonical form, plus its hash.
- `Grant`: capabilities, budget limits, identity, expiry. Immutable.
- `RunState`: rebuilt from events, never edited directly.

## The pipeline

`ActionPipeline.execute` in `kernel/pipeline.py` handles every tool call, in this order:

1. kill check (through `IdentityPort`)
2. tool exists and the agent was given it
3. arguments match the schema, resource extracted
4. repeat check: third identical call refused, fifth ends the run
5. grant covers the required capabilities
6. policy
7. external authority (`IdentityPort.authorize`)
8. for `delegate`: the delegation check (child agent, attenuation, budget, depth, fan-out)
9. approval, if policy asked for one: request it and pause, or check and consume a granted one
10. credentials resolved
11. tool-call budget
12. kill check again, then run with timeout and retries (for `delegate`: the identity service may
    veto, then the child is made and runs)
13. output schema, secret redaction, size limit

Each step writes events. There are no hooks between steps. The extension points are the things
the steps call (policy, identity port, credential resolver).

## Effect classes

Every tool declares one:

| Class | Retried on timeout? | Running when the process stopped |
|---|---|---|
| `pure` | yes | run again on resume |
| `read` | yes | run again on resume |
| `write_idempotent` | yes | run again on resume, same idempotency key |
| `write` | no, in doubt, run pauses | in doubt, run pauses for `legion reconcile` |
| `external_irreversible` | no, in doubt, run pauses | same; the default policy also asks for approval before running one |

Tools get `ctx.idempotency_key`, which is the action hash: the same on every retry and after a
resume, so an API that supports idempotency keys can drop duplicates.

## Capabilities and grants

A capability is `name[:resource]`, e.g. `files.read:notes/**`. `*` matches inside one path
segment and `**` matches across them. A grant with no resource covers any resource for that name,
and a trailing `.*` on the name covers anything under it. If a tool needs a resource-limited
capability but didn't say which resource it touches, the check fails. Resources containing `..`
never match.

The run's root grant is whatever the agent asks for, as long as the operator listed it under
`authority.grantable`.

`Grant.attenuate` is the only way to make a child grant. It refuses anything wider than the
parent (capabilities, limits, expiry, delegation depth and fan-out) and derives the child's
identity itself. Delegation is described below and in ADR 0016.

## Policy

Policy only runs for actions the grant already allows, and it can only say no. The built-in one is
a list of rules matched on tool name, capability and effect class. Deny beats require-approval,
which beats allow. OPA, Cedar, NIA or MIA could replace it behind the same interface. By default
`external_irreversible` tools require approval; listing `policy.rules` in `legion.yaml` replaces
that default, so keep an equivalent rule. A rule whose `tool` or `capability` matches nothing that
exists is refused at startup, since it would read like a control and do nothing.

## Configuration

`legion.yaml` and agent files are trusted: whoever writes them can grant anything. The loader's
job is to make sure they load the way they read. Unknown fields, a key given twice in the same
mapping, YAML aliases, non-finite numbers and literal secrets are refused, and so is a tool
setting that points at a directory containing the state directory, `legion.yaml`, a tool module
or the agents directory (whether it exists yet or not). Paths are relative to `legion.yaml`;
nothing expands `~` or `$VARS`.

## Approvals

When policy says `require_approval`, the pipeline records `approval.requested` and the run pauses
(`run.paused`); the process exits. `legion approval show` prints what exactly would run, then
`legion approve` or `legion deny`, then `legion resume`.

An approval is bound to one call. Legion hashes everything that decides what the call does: run,
task and call ids, agent spec, tool spec, arguments, resource, effect, required capabilities,
grant, agent identity, tool settings and credential references. When the call is about to run the
hash is rebuilt and compared; on a match the approval is consumed before the tool starts. Changing
anything in the list, using it for another call, using it twice or using it after it expires
(one hour by default) is refused. Details in ADR 0014.

The approver is whoever runs the CLI as the local OS user. Legion doesn't authenticate them.

## Events

Everything is derived from the event log: run and task status, budget used, the transcript sent to
the model. The live run applies each event to a `RunState` as it's written, and a test rebuilds
the same state from storage and compares.

Each event is hashed as `sha256(prev_hash + canonical_json(event without hash))`, starting from 64
zeros per run. That's the same scheme MIA uses. SQLite triggers block `UPDATE` and `DELETE`.
`legion verify` also checks that each body names its run and that the `seq` and `type` columns
agree with it, and refuses bodies with a duplicated JSON key. `inspect` refuses to show a run whose
chain doesn't verify.

The log is tamper-evident, not tamper-proof. The chain isn't keyed and there's no outside record
of the last hash, so anyone who can write the store file (the same OS user, which includes native
tools and MCP stdio servers) can drop the triggers, cut events off the end, delete a whole run,
or rewrite a run's chain from the start, and `verify` won't notice. Cutting a run back to just
after an approval makes resume run the approved call again. A forensics tool should treat these
events as Legion's own account of what it did.

Full list of events: [docs/events.md](docs/events.md).

## Models and access

These are three separate pieces:

- `ModelProvider` speaks one wire format: OpenAI-compatible, Anthropic, or scripted for tests.
- `AccessProvider` handles authentication. Right now that's none (local) or an API key. Gateways,
  workload identity and OAuth aren't built. A provider that gets an API key needs an https URL
  (plain http only to 127.0.0.1 or [::1], not the name `localhost`), and URLs can't carry a
  username or password.
- `ModelResolver` maps what the agent asks for (e.g. `general/default` with tools) to a configured
  binding. If nothing fits, the run doesn't start. A binding's features are what the operator
  declares, limited to what the adapter implements.

Provider-specific settings go in `models[].options` in `legion.yaml`, keyed by provider kind,
and each adapter ignores the others.
Reasoning blocks are passed back only to the provider that produced them.

The adapters use httpx instead of vendor SDKs so that Legion is the only thing retrying, and
every retry counts against the budget.

GitHub's Copilot SDK, the Claude Agent SDK and the Codex app-server run their own agent loops, so
their tools would run outside Legion's checks. I'm not treating them as model providers, and
driving them isn't on the roadmap any more (ADR 0006).

## Secrets

Config holds references like `env:ANTHROPIC_API_KEY`, never values, and a config error never
repeats a value it rejected. `Secret` wraps a value and won't print it. A tool gets only the
credentials its spec lists.

When a run starts or resumes, Legion resolves every secret reference in the configuration it can:
tool credentials, model API keys, and MCP servers' env and header values. Each value (and its
JSON-escaped form) is scrubbed from every event before it's written, keys included, and from tool
output before the model sees it; the longest value is replaced first. Provider error messages have
the request's own headers removed before they become errors. What this doesn't catch: values
under 4 characters, a secret a tool encodes or splits (base64, URL-encoding), a secret the model
types out that Legion never resolved, and whatever an MCP server writes to its stderr log.

## Credentials for tool calls

A tool that needs a credential names it (`credentials=["github"]`). The name is either a static
secret reference, recorded as `unverified` on every call, or a mapping onto a credential
authority (ADR 0019). For a mapped credential Legion builds the request from the authorized
Action and the operator's mapping only (permissions per capability, the Action's resource, the
task's identity, the Action hash, Legion's own call id and Grant fingerprint), asks the
authority, and checks
what comes back. Anything wider than the request is refused. Legion decides the assurance:
`unverified`, `declared` (untrusted authority), `verified` (trusted authority) or `bound`
(trusted, and bound to this call and Grant). The required level is the highest of
`credential_policy.minimum`, the mapping's `minimum` and any matching policy rule's
`credential_assurance`; it's never lowered, and an MCP call is held to it too. Credentials are
issued after the budget charge and a kill check; then, immediately before `tool.started`, each one
is checked for expiry and with the authority for revocation, and the kill state is checked again.
An expired credential is replaced (once before a call starts, or before a safe retry); a revoked
one, or one the authority can't vouch for, refuses the call. Each decision is recorded as
`credential.resolved` or `credential.refused` without the secret, and
`legion credentials <run-id>` shows them.

The check before dispatch is the last one Legion makes. What happens between it and the downstream
system using the credential isn't something Legion can see; ADR 0019 describes that window.

The call id a credential is bound to is Legion's, derived from the `model.responded` event that
recorded the call (ADR 0021). The model's or provider's tool call id is kept for the transcript
and as provenance, and binds nothing.

Authorities are plug-ins behind the port: an operator module (`module:`), or a built-in adapter
(`provider: nia`, ADR 0021). The kernel sees only `CredentialAuthority`, `CredentialRequest` and
generic evidence, and the conformance tests in `tests/conformance` hold every implementation to
the same behaviour. The adapter is imported only when configured.

## Identity (NIA and MIA)

Legion only enforces inside a single run. Identity, issuing credentials, per-agent grants, kill
switches and risk across runs belong to an external service reached through `IdentityPort`. The
default `NullIdentityPort` never kills and never vetoes.

| Method | NIA today (ADR 0020) | MIA |
|---|---|---|
| `agent_identity(name)` | mapped ref, looked up with `GET /agents/{ref}` | mandate subject |
| `kill_state(identity)` | `effective_state` with a confirmed live kill check | mandate revoked or suspect |
| `authorize(action, identity)` | identity state only; NIA has no per-action decision for Legion | `authz.authorize` |
| `on_delegation(parent, child)` | both mapped and active, or refused; NIA records nothing | `mandates.delegate` |
| `evidence(action_hash)` | nothing | audit |
| `credential_evidence(server)` | nothing | - |

An action runs only if both Legion's grant and the external service allow it. The kill check
covers the task's own identity and every ancestor's, so killing an agent stops everything it
delegated to.

`legion.adapters.nia` is the NIA identity adapter, configured under `identity:` in `legion.yaml`
with an explicit map from Legion agent names to NIA refs and a viewer-role token.
`legion.adapters.nia_credentials` is the NIA credential authority (ADR 0021), configured under
`credential_authorities:` with an issuer-role token and the same map. Both talk to NIA's control
plane over HTTP only, fail closed on anything they can't confirm, and aren't imported unless
configured; neither is needed to run Legion. A NIA credential used at NIA's gateway is checked
there too (tool, resource, status, kill state), but the gateway doesn't see Legion's Action or
call. Legion's tools don't go through the gateway unless a tool calls it.

## Failures

Each error has a disposition and the loop only looks at that:

- `retryable`: model timeouts, rate limits, 5xx, malformed responses, read-tool timeouts. Retried
  with backoff up to a limit, and each attempt is charged.
- `recoverable`: unknown tool, bad arguments, denied, tool raised, repeated call. The model is told
  and can try something else.
- `fatal`: budget exceeded, auth failure, context too long, loop. The run ends; anything still
  running is recorded as in doubt.
- `escalate`: approval needed, or an action is in doubt. The run pauses for a person.

## Resume

`legion resume` checks the chain, rebuilds the run from the log, checks the recorded agent and
grant against today's configuration (authority can shrink between runs, never grow; the model
binding has to be the same), then continues through the normal loop. The loop starts by finishing
whatever the last model turn left open, so a resumed run and a fresh one use the same code.
Recorded responses and results are reused, never requested or run again. What happens to each
kind of interruption is in ADR 0013.

Budget use comes from the log, so it survives restarts. Time spent paused isn't charged; time up
to the last event of a crashed stretch is.

A run is driven by one process at a time, enforced with an OS file lock next to the store
(ADR 0015).

## Delegation

An agent can hand a sub-task to another agent with the built-in `delegate` tool. It's an
ordinary call as far as the pipeline is concerned: it needs `agent.delegate:<agent name>`, policy
and the external authority are asked, and it can require approval. Before approval there's a
delegation check: the child agent exists in the catalog (`agents_dir`), its capabilities are
within the parent's grant and its own spec, it gets no more budget than the parent has left, depth
and fan-out allow it, and the run is under its task cap. The identity service gets a chance to
veto (`IdentityPort.on_delegation`). Then the child is made.

A child is a task in the same run, with its own grant, identity, transcript and budget. It sees
only the objective and context it was given. Its steps, calls, tokens and cost are reserved from
the parent while it runs and settled when it ends (ADR 0017). The parent gets a structured result,
not the child's conversation. A child that needs a person pauses the whole run; a child that fails
returns a failed result; a killed ancestor stops the subtree. Children run one at a time for now.
Details in ADR 0016.

`legion tasks <run-id>` shows the tree.

## Tools

Tool sources plug in behind one `Tool` protocol: native Python and MCP. A tool without an effect
class, or without capabilities unless it's `pure`, can't be registered. Names must match
`[a-z][a-z0-9_]{0,63}` because that works with every provider. Tool and agent schemas can only use
`$ref` inside themselves; Legion never fetches a schema from a URL or a file.

The model only sees the tools the agent lists, and calling anything else gets refused. Native
tools run inside the Legion process, so the checks decide whether a tool runs but not what its code
does. That's why there's no shell tool.

### MCP

MCP tools are ordinary tools with a remote `invoke` (ADR 0018). The operator lists each server
and the tools to use from it in `legion.yaml`:

```yaml
mcp_servers:
  github:
    transport: stdio
    command: [github-mcp-server, stdio]
    env: {GITHUB_TOKEN: env:GITHUB_TOKEN}
    credential_scope: fine-grained token, issues read/write on legion only
    tools:
      create_issue:
        effect: write
        resource_arg: repo
        pin: sha256:...
```

That gives the tool `mcp_github_create_issue`, which needs `mcp.github.create_issue:<repo>`. At
startup Legion connects, lists the server's tools and registers only manifest entries whose pin
matches; by default it checks the pin again before each call (`pin_check: discovery` turns that
off). `legion mcp inspect github` shows the current pins, what the server claims about each tool,
the tools it offers that aren't used, and a warning if the command runs a package with no version
(`npx some-server`), since then the code can change without any pin changing. A bare command like
`github-mcp-server` is found through `PATH`; the pin covers the name, not the binary.

The model sees the server's top-level description, cleaned of control and bidi characters and
capped at 1000 characters, unless the manifest replaces it. Text inside the input schema
(property descriptions, enums) goes to the model as the server wrote it; it's pinned, not
cleaned. Results are reduced to text within size limits. The effect class and everything to do
with authority come from the manifest. Each `action.proposed` for an MCP tool records where it
ran and the credential scope the operator declared. Stdio servers run as the same OS user as
Legion, so like native tools they can write the store; `cwd` is relative to `legion.yaml`, and
their stderr goes to a log file readable only by that user.

## Multi-tenancy (not built)

Everything is keyed by run, and that's where a tenant id would go. When I add it, the tenant has to
be part of every storage key and every credential lookup, not a filter on top.
