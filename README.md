# Legion

A small agent runtime where every effect passes one enforcement path and authority only narrows.

Legion runs LLM agents with tools. It does not care which model, which domain, or how you
authenticate. What it does care about is that nothing an agent does happens outside a single,
ordered, recorded check, and that the record is good enough to rebuild the run from.

Status: **pre-alpha, Phase 1 of 7**. One agent per run, no persistence across restarts yet, no
approvals yet, no MCP yet. See [the roadmap](docs/roadmap.md).

## What it is

- A Python library and a CLI. One process, no services, SQLite for the event log.
- A kernel that turns every tool call into an `Action` and passes it through one pipeline:
  kill check, lookup, schema, repeat detection, grant, policy, external authority, budget,
  credentials, execution with timeout, output checks, events.
- A `Grant` per task that carries capabilities, budget and identity, and can only be narrowed.
- An append-only, hash-chained event log that is the source of truth for the run.
- Provider adapters for any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, llama.cpp,
  gateways) and the Anthropic Messages API, plus a scripted provider for tests.

## What it is not

- Not a framework with graphs, crews or workflow designers.
- Not an observability platform, a vector store, a sandbox, or a policy language.
- Not secure against a malicious tool. Native Python tools are trusted code.
- Not production ready. Nothing here has been tested at scale or reviewed by anyone but its
  author.

## Why

Most agent frameworks treat governance as middleware and state as whatever is in the context
window. That makes three things hard to guarantee: that a sub-agent cannot do more than its
parent, that an approved action is the one that runs, and that a crash does not repeat a write.
Legion is an attempt to make those properties structural and testable in a codebase small enough
to read. [docs/assessment.md](docs/assessment.md) compares it with LangGraph, the OpenAI Agents
SDK, PydanticAI, OpenHands, Microsoft's Agent Framework and Agent Governance Toolkit, and others,
including where they are ahead.

## Quick start

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). No API key.

```bash
git clone <repo> legion && cd legion
uv sync
uv run legion init demo && cd demo
uv run legion agent validate agents/assistant.yaml
uv run legion run agents/assistant.yaml "Summarize the notes"
uv run legion runs
uv run legion inspect <run-id>
uv run legion verify <run-id>
```

The starter project runs a scripted model over a small notes folder. One note contains an
instruction to read `private/salaries.md`; the scripted model follows it, and Legion refuses:

```
  28 model.responded    calls read_note (50 in / 20 out)
  30 action.proposed    read_note on private/salaries.md  23a3fe0e913f
  31 action.refused     read_note: capability_denied: the task's grant does not cover
                        files.read:private/salaries.md
  ...
  58 run.completed      'I wrote a two-line summary of both notes to out/summary.md. ...'
ok 58 events, chain intact
```

That is the demo worth showing: the model complies, the runtime blocks. A demo where the model
refuses on its own proves nothing about the runtime.

To use a real model, edit `legion.yaml` and put a real binding first for `general/default`, for
example Ollama at `http://localhost:11434/v1` or Anthropic with `ANTHROPIC_API_KEY` set. The agent
file does not change.

## Architecture in one picture

```
APPLICATION   agents (YAML or code) and tools
HARNESS       Runtime → AgentLoop → ActionPipeline → Tool
                 │           │            │
                 └───────────┴────────────┴──▶ EventStore (hash chain) ──▶ projections
PORTS         ModelProvider · AccessProvider · CredentialResolver · PolicyDecisionPoint
              IdentityPort (NIA / MIA) · EventStore
MODEL         OpenAI-compatible endpoint · Anthropic · scripted
```

Details: [ARCHITECTURE.md](ARCHITECTURE.md). Decisions: [docs/adr/](docs/adr/). Event schema:
[docs/events.md](docs/events.md).

## Writing an agent

```yaml
name: notes-assistant
instructions: You summarize notes...
model:
  profile: general/default     # the operator decides which model satisfies this
  needs: [tools]
tools: [list_notes, read_note, write_summary]
capabilities:
  - files.read:notes/**
  - files.write:out/**
budget: {steps: 10, model_calls: 12, tool_calls: 10, tokens: 50000, wall_seconds: 120}
```

An agent can only request capabilities the operator lists as `grantable` in `legion.yaml`, and a
run refuses to start otherwise. Agent files are data; they cannot add tools, change policy or
name secrets.

## Writing a tool

```python
class ReadArgs(BaseModel):
    path: str

@tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: normalize(a.path))
def read_note(args: ReadArgs, ctx: ToolContext) -> str:
    """Read one file from the workspace."""
    ...
```

Every tool declares an effect class (`pure`, `read`, `write_idempotent`, `write`,
`external_irreversible`) and the capabilities it needs. The effect class decides whether a
timeout is retried or recorded as in doubt. The resource function tells Legion what the call
touches, so `files.read:notes/**` can allow `notes/a.md` and refuse `private/b.md`.

## Security philosophy

Assume the model can be fully manipulated at any time, and bound what that can cause:

- Authority is data attached to each task, checked for every action, and can only narrow.
- Every effect goes through one pipeline. There are no hooks that skip a step.
- Secrets are references until the moment of use and never enter model context or events.
- Legion is the only retrier, and every attempt is charged to the budget.
- The event log records refusals as carefully as successes.

What it cannot do is listed plainly in [THREAT_MODEL.md](THREAT_MODEL.md): it does not stop a
manipulated model from misusing authority it legitimately holds, it provides no sandbox, and it
cannot protect the log from whoever controls the host.

## Current limitations

- One agent per run. Delegation and parallel sub-agents are Phase 3; `Grant.attenuate` exists
  and is property-tested so the rule is fixed first.
- A crashed run cannot be resumed yet (Phase 2). The event log already contains what resume
  needs, including which actions were in flight.
- No human approval flow yet (Phase 2). Configuration that would require approval is refused at
  load time rather than silently treated as deny.
- No MCP (Phase 5), no streaming, no image input.
- The resolver does not probe endpoints; the operator declares what each model can do.
- Tool calls within one model turn run sequentially.
- Tested against recorded HTTP exchanges. The optional real-model tests have not been run in CI.
- The token budget caps output per call but not input, so one call can overshoot by the size of
  its prompt; cost is checked after each call. Details in the threat model.
- The event chain detects edits and gaps but not removal of the newest events, which needs an
  external record of the head.

## Roadmap

Durability and approvals, then delegation, then access methods and external agent runtimes
(Copilot SDK, Claude Agent SDK, Codex, as delegation targets with weaker guarantees, never
presented as plain models), then MCP, then a security-operations reference application, then
event exports and the NIA identity adapter. Details and acceptance criteria:
[docs/roadmap.md](docs/roadmap.md).

## Related projects

- **NIA**: a Go control plane for agent identity, credentials, kill switch and risk. Legion talks
  to it through `IdentityPort` (Phase 7) and can route tools through its gateway.
- **MIA**: a Python service for mandates and attenuating delegation. Also an `IdentityPort` target.

Neither is required.

## Development

```bash
uv sync
uv run pytest            # unit and conformance tests, no network
uv run ruff check src tests && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

Apache-2.0
