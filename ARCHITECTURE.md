# Architecture

Legion is a Python library with a CLI. It runs in one process on asyncio and stores its events in
SQLite. The only external thing it talks to is the model endpoint. Parts marked with a phase
aren't built yet; see [docs/roadmap.md](docs/roadmap.md).

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
`external_irreversible` tools require approval.

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

This catches edits, not a determined attacker who owns the host and can rewrite the whole chain.
A forensics tool should treat these events as Legion's own account of what it did.

Full list of events: [docs/events.md](docs/events.md).

## Models and access

These are three separate pieces:

- `ModelProvider` speaks one wire format: OpenAI-compatible, Anthropic, or scripted for tests.
- `AccessProvider` handles authentication. Right now that's none (local) or an API key. Gateways,
  workload identity and OAuth (where the provider documents it) come in Phase 4.
- `ModelResolver` maps what the agent asks for (e.g. `general/default` with tools) to a configured
  binding. If nothing fits, the run doesn't start. A binding's features are what the operator
  declares, limited to what the adapter implements.

Provider-specific settings go in `provider_options[kind]`, and each adapter ignores the others.
Reasoning blocks are passed back only to the provider that produced them.

The adapters use httpx instead of vendor SDKs so that Legion is the only thing retrying, and
every retry counts against the budget.

GitHub's Copilot SDK, the Claude Agent SDK and the Codex app-server run their own agent loops, so
their tools would run outside Legion's checks. I'm not treating them as model providers, and
driving them isn't on the roadmap any more (ADR 0006).

## Secrets

Config holds references like `env:ANTHROPIC_API_KEY`, never values. `Secret` wraps a value and
won't print it. A tool gets only the credentials its spec lists. Every secret value resolved
during a run is scrubbed (along with its JSON-escaped form) from every event before it's written,
and from tool output before the model sees it.

## Identity (NIA and MIA)

Legion only enforces inside a single run. Identity, issuing credentials, per-agent grants, kill
switches and risk across runs belong to an external service reached through `IdentityPort`. The
default `NullIdentityPort` never kills and never vetoes.

| Method | NIA | MIA |
|---|---|---|
| `kill_state(identity)` | kill sentinel, revoked credential | mandate revoked or suspect |
| `authorize(action, identity)` | gateway decision | `authz.authorize` |
| `credential(agent_ref, purpose)` | `POST /agents/{ref}/credentials` | token exchange |
| `on_delegation(parent, child)` | register the child with a subset of grants | `mandates.delegate` |
| `evidence(action_hash)` | incidents, audit | audit |

An action runs only if both Legion's grant and the external service allow it. The kill check
covers the task's own identity and every ancestor's, so killing an agent stops everything it
delegated to. NIA can also sit in
front of the tools as a gateway (`POST /tools/{tool}/call` or `/mcp`), which means someone who gets
around Legion still has to get past NIA. For now only the interface and the null version exist.

## Failures

Each error has a disposition and the loop only looks at that:

- `retryable`: model timeouts, rate limits, 5xx, malformed responses, read-tool timeouts. Retried
  with backoff up to a limit, and each attempt is charged.
- `recoverable`: unknown tool, bad arguments, denied, tool raised, repeated call. The model is told
  and can try something else.
- `fatal`: budget exceeded, auth failure, context too long, loop, a write in doubt. The run ends.

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

Tool sources plug in behind one `Tool` protocol: native Python now, MCP in Phase 5. A tool without
an effect class and capabilities can't be registered. Names must match `[a-z][a-z0-9_]{0,63}`
because that works with every provider.

The model only sees the tools the agent lists, and calling anything else gets refused. Native
tools run inside the Legion process, so the checks decide whether a tool runs but not what its code
does. That's why there's no shell tool.

## Multi-tenancy (not built)

Everything is keyed by run, and that's where a tenant id would go. When I add it, the tenant has to
be part of every storage key and every credential lookup, not a filter on top.
