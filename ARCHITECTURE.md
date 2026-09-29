# Architecture

Legion is a library with a CLI. It runs in one process, uses asyncio, and needs no services
beyond the model endpoint you point it at. This document describes the full design. Sections
marked with a phase describe work that is not built yet; see [docs/roadmap.md](docs/roadmap.md).

## Layers

```
MODEL        inference: a remote API, an enterprise gateway, or a local server
AGENT        a declarative spec: instructions, tools, requested capabilities, budget
HARNESS      Legion: runs agents, owns every effect, records every transition
APPLICATION  your code: defines agents and tools, starts runs, uses results
```

The harness never imports application code except through the tool and agent interfaces, and
nothing in `src/legion` knows what domain it is running in. The security-operations example
lives in `examples/`, not in the package.

## Components

```
            ┌────────────────────────── Legion (one process) ──────────────────────────┐
            │                                                                           │
 legion run │  Runtime ── builds Run, root Task, root Grant; resolves model binding      │
 ──────────▶│     │                                                                     │
            │     ▼                                                                     │
            │  AgentLoop (per task)                                                     │
            │     context ──▶ ModelCaller ──▶ ModelProvider adapter ──▶ model endpoint  │
            │        ▲             │ (retries, budget, events)                           │
            │        │             ▼                                                     │
            │        │        tool calls                                                 │
            │        │             │                                                     │
            │        │             ▼                                                     │
            │        │     ActionPipeline  ◀── the only code path that causes effects    │
            │        │      1 kill check          6 budget                               │
            │        │      2 lookup + offered    7 credentials                          │
            │        │      3 schema              8 execute (timeout, bounded retry)     │
            │        │      4 repeat detection    9 output check, redaction              │
            │        │      5 grant + policy     10 events                               │
            │        └──── tool result ◀─┘                                               │
            │                                                                           │
            │  EventStore (append-only, per-run hash chain)    ArtifactStore (sha256)   │
            └───────────────────────────────────────────────────────────────────────────┘
               ports: ModelProvider, AccessProvider, CredentialResolver, Tool,
                      PolicyDecisionPoint, IdentityPort, EventStore
```

## Core types

| Type | What it is | Mutable? |
|---|---|---|
| `AgentSpec` | Name, instructions, model requirement, tool names, requested capabilities, budget, optional output schema | No |
| `Task` | Objective, context, constraints, parent, creator, status | Status only, through a transition table |
| `Run` | One root task tree, its event log and ledger | Status only |
| `ToolSpec` | Name, description, input and output schema, required capabilities, effect class, timeout, credential names | No |
| `Action` | One proposed tool call in canonical form, with its hash | No |
| `Grant` | Capabilities, budget limits, identity context, expiry, parent grant | No; derive a narrower one with `attenuate` |
| `Ledger` | What a grant's budget has consumed | Append-only, rebuilt from events |
| `Decision` | Policy output: allow, deny, or require approval, with reasons | No |
| `Event` | One recorded transition, chained by hash | Never |

### Effect classes

Every tool declares one. The class decides retries, default policy and, from Phase 2, what
happens after a crash.

| Class | Meaning | Retry on timeout | After a crash (Phase 2) |
|---|---|---|---|
| `pure` | No external effect | Yes | Re-run |
| `read` | Reads external state | Yes | Re-run |
| `write_idempotent` | Writes, safe to repeat with the same key | Yes | Re-run with the same key |
| `write` | Writes, not safe to repeat | No, outcome is in doubt | Escalate to a human |
| `external_irreversible` | Cannot be undone (send, deploy, pay) | No | Escalate; denied by default policy |

### Capabilities and grants

A capability is `name[:resource]`, for example `files.read:notes/**`. A tool lists the capability
names it needs and, optionally, which argument identifies the resource. A grant covers a
requirement when the name matches (a trailing `.*` matches any suffix) and either the grant has no
resource constraint or the requirement's resource matches the grant's glob. A requirement with no
resource is never covered by a resource-constrained grant, and resources containing `..` segments
are never covered.

Delegation (Phase 3) derives a child grant with `Grant.attenuate`, which raises unless every
capability, every budget limit and the expiry are at most the parent's. The function exists and
is property-tested now so the rule is fixed before delegation is built.

### Policy

The pipeline asks a `PolicyDecisionPoint` about every action that the grant already covers.
Policy can only take authority away. The built-in implementation is a rule table matched on tool
name, capability and effect class, where any deny wins over any approval requirement, which wins
over any allow. There is no policy language; OPA, Cedar, NIA or MIA can sit behind the same
interface later.

In Phase 1 there is no approval flow, so a configuration that could produce `require_approval`
is rejected at load time rather than silently treated as deny.

## Events

Events are the source of truth. The run status, the task statuses, the ledger and the transcript
shown to the model are all projections of the event log, and a test rebuilds them from a stored
run and compares them with what the live run saw.

Each event carries `event_id`, `schema_version`, `run_id`, `seq`, `ts`, `type`, `task_id`,
`agent_id`, `parent_task_id`, a typed `payload`, correlation fields, `prev_hash` and `hash`.
`hash = sha256(prev_hash + canonical_json(event without hash))`, with a genesis of 64 zeros per
run, the same scheme MIA uses so one verifier handles both. The SQLite store refuses `UPDATE` and
`DELETE` with triggers.

This is tamper evidence against casual edits, not proof. Whoever controls the host can rewrite the
whole chain. External anchoring belongs to a separate forensics project, which should treat
Legion's events as the harness's own account of what happened, not as ground truth.

See [docs/events.md](docs/events.md) for every event type and payload.

## Models and access

Three separate concerns:

- **ModelProvider** speaks one wire protocol (`openai_compat`, `anthropic`, `scripted` for tests)
  and reports what it supports through `ModelCapabilities`.
- **AccessProvider** decides how requests are authenticated: none (local), API key, and later
  enterprise gateway headers, workload identity and documented OAuth flows.
- **ModelResolver** maps an agent's requirement, such as `general/default` needing tools, to a
  configured binding of provider, model and access. If no binding satisfies what the agent needs,
  the run does not start.

Provider-specific options travel in `ModelRequest.provider_options[provider_name]` and are ignored
by every other provider. Reasoning blocks are kept as opaque metadata and only sent back to the
provider that produced them.

Adapters talk HTTP through httpx rather than vendor SDKs, so Legion is the only place retries
happen and every retry is counted against the budget.

### External agent runtimes (Phase 4)

GitHub's Copilot SDK, the Claude Agent SDK and the Codex app-server are agent runtimes, not
inference endpoints: their own loop runs the tools. Legion will support them as an
`ExternalAgentRuntime` that a task can delegate to, with a declared guarantee level, and events will
mark every result that came back from one. They are never presented as a `ModelProvider`, because
that would claim enforcement Legion does not have.

## Secrets

Configuration holds references such as `env:ANTHROPIC_API_KEY`, never values. A `Secret` wraps
the value, and its `repr` and `str` never show it. Access providers resolve secrets per request.
Tools receive only the credentials their spec declares, resolved at step 7. Tool output is scanned
for the values of those credentials and redacted before it reaches the model or the event log.

## Identity integration (NIA and MIA)

Legion enforces within one run. Identity, credential issuance, per-agent grants, kill state and
risk across runs belong to external authorities reached through `IdentityPort`. The default
`NullIdentityPort` is local-only and never kills.

| IdentityPort method | NIA | MIA |
|---|---|---|
| `kill_state(agent_ref)` | kill sentinel, revoked credential | mandate revoked or suspect |
| `authorize(action, identity)` | gateway decision for the tool call | `authz.authorize` |
| `credential(agent_ref, purpose)` | `POST /agents/{ref}/credentials` | token exchange |
| `on_delegation(parent, child, grant)` | register child and grant a subset | `mandates.delegate` |
| `evidence(action_hash)` | incidents and audit records | audit records |

The effective permission for an action is the Legion grant intersected with the external
decision. A second integration mode routes tool execution through NIA's gateway
(`POST /tools/{tool}/call` or `/mcp`), so that a bypassed Legion still meets NIA. The kill check
runs before every tool execution and before every model call.

In Phase 1 only the protocol and the null implementation exist.

## Failure model

Every error type carries one disposition. The loop reads the disposition, not the type.

| Disposition | Meaning | Examples |
|---|---|---|
| `retryable` | Retry with bounded backoff, each attempt counted | model timeout, rate limit, 5xx, malformed response, read-tool timeout |
| `recoverable` | Tell the model and let it choose again | unknown tool, invalid arguments, capability or policy denial, tool raised, repeated action |
| `fatal` | Fail the task and the run | budget exceeded, auth failure, context exhausted, loop detected, write in doubt (Phase 1) |
| `escalate` | Pause for a human (Phase 2) | write in doubt, credential expired |

Retries are bounded by `RetryPolicy` and by the budget, so a model cannot cause unbounded retries
by producing bad output. Five identical actions in one task is a fatal loop; the third is refused
with a recoverable error first.

## Persistence and resume (Phase 2)

Resume rebuilds the run from events. Model responses and tool results that were recorded are
replayed, never requested or executed again. An action with `tool.started` and no completion is
in doubt, and its effect class decides what happens (table above).

## Delegation (Phase 3)

A built-in `delegate` tool creates a child task with an attenuated grant and a budget carved out
of the parent's ledger. Limits: depth, children per task, concurrent tasks. A child fails, times
out or is cancelled as a unit and the parent receives a structured result. Authority for a child
beyond the parent's can only come from an external `GrantAuthority`, never from a model.

## Tools

Tool sources are adapters over one `Tool` protocol: native Python today, MCP in Phase 5, others
later. A tool must declare its effect class and required capabilities or it cannot be registered.
Tool names are limited to `[a-z][a-z0-9_]{0,63}` so the same name works with every provider.

The model sees only the tools the agent lists, and calling any other tool is refused. Native tools
are trusted code running in the harness process: capability checks decide whether a call happens,
not what the code does once it runs. For that reason there is no shell tool.

## Multi-tenancy (not built)

Every store is keyed by run, and the run is where a tenant id will be added. The rule for later
work: tenant is part of the key of every store and every lookup, never a filter applied
afterwards, and credentials are resolved through a tenant-scoped resolver so one tenant's
reference cannot name another tenant's secret.
