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
8. credentials resolved
9. tool-call budget
10. kill check again, then run with timeout and retries
11. output schema, secret redaction, size limit

Each step writes events. There are no hooks between steps. The extension points are the things
the steps call (policy, identity port, credential resolver).

## Effect classes

Every tool declares one:

| Class | Retried on timeout? | After a crash (Phase 2) |
|---|---|---|
| `pure` | yes | re-run |
| `read` | yes | re-run |
| `write_idempotent` | yes | re-run |
| `write` | no, marked in doubt | ask a human |
| `external_irreversible` | no | ask a human; denied by default policy for now |

## Capabilities and grants

A capability is `name[:resource]`, e.g. `files.read:notes/**`. `*` matches inside one path
segment and `**` matches across them. A grant with no resource covers any resource for that name,
and a trailing `.*` on the name covers anything under it. If a tool needs a resource-limited
capability but didn't say which resource it touches, the check fails. Resources containing `..`
never match.

The run's root grant is whatever the agent asks for, as long as the operator listed it under
`authority.grantable`.

`Grant.attenuate` is the rule for delegation: a child can't get more capabilities, bigger limits
or a later expiry than its parent. I built and tested it before delegation itself (Phase 3) so
the rule is settled first. Two gaps remain for Phase 3: it compares the child with the parent's
limits rather than with what the parent has left, and it doesn't derive the child's identity from
the parent.

## Policy

Policy only runs for actions the grant already allows, and it can only say no. The built-in one is
a list of rules matched on tool name, capability and effect class. Deny beats require-approval,
which beats allow. OPA, Cedar, NIA or MIA could replace it behind the same interface. There's no
approval flow yet, so a config that uses `require_approval` fails to load.

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

GitHub's Copilot SDK, the Claude Agent SDK and the Codex app-server run their own agent loops.
In Phase 4 they'll be supported as external runtimes a task can hand work to, with the results
marked as such in the events. I'm not treating them as model providers, because their tools would
run outside Legion's checks.

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

An action runs only if both Legion's grant and the external service allow it. NIA can also sit in
front of the tools as a gateway (`POST /tools/{tool}/call` or `/mcp`), which means someone who gets
around Legion still has to get past NIA. For now only the interface and the null version exist.

## Failures

Each error has a disposition and the loop only looks at that:

- `retryable`: model timeouts, rate limits, 5xx, malformed responses, read-tool timeouts. Retried
  with backoff up to a limit, and each attempt is charged.
- `recoverable`: unknown tool, bad arguments, denied, tool raised, repeated call. The model is told
  and can try something else.
- `fatal`: budget exceeded, auth failure, context too long, loop, a write in doubt. The run ends.

Phase 2 adds `escalate`, which pauses the run for a human.

## Resume (Phase 2)

Resume will rebuild the run from events. Recorded model responses and tool results are reused,
not requested again. An action that started and never finished is in doubt, and its effect class
decides what to do (table above).

## Delegation (Phase 3)

A built-in `delegate` tool will start a child task with a narrower grant and a slice of the
parent's remaining budget, with limits on depth, fan-out and concurrency. Extra authority for a
child can only come from outside (a human, NIA or MIA), never from the model.

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
