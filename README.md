# Legion

Legion is a small runtime for LLM agents. It runs an agent, lets it call tools, and checks every
tool call against what the agent is allowed to do before anything happens. Everything it does is
written to an append-only, hash-chained event log, and the run's state is rebuilt from that log.

It doesn't care which model you use or what the agent is for. The first reference application
will be security operations, but nothing in the core knows about security.

This is early work (Phase 1 of 7, see [the roadmap](docs/roadmap.md)). One agent per run, no
resume after a crash, no approvals, no MCP yet. I wouldn't run it against anything that matters.

## Why I'm building it

I kept running into the same gaps in existing frameworks. Sub-agents can end up with more access
than the agent that spawned them. An approval is usually tied to a tool name rather than to the
exact arguments. A crash in the middle of a write often means the write runs again on resume.
And the state of a run tends to live in the context window.

Legion tries to fix those in the runtime itself, in code small enough to read in an afternoon.
[docs/assessment.md](docs/assessment.md) has my notes on LangGraph, the OpenAI Agents SDK,
PydanticAI, OpenHands, Microsoft's Agent Framework and Agent Governance Toolkit and others,
including the places where they're ahead.

## Quick start

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). No API key.

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

The starter project uses a scripted model, so it behaves the same every time. One of the notes
tells the assistant to also read `private/salaries.md`. The scripted model does, and Legion
refuses because the agent only has `files.read:notes/**`:

```
  30 action.proposed    read_note on private/salaries.md  23a3fe0e913f
  31 action.refused     read_note: capability_denied: the task's grant does not cover
                        files.read:private/salaries.md
  ...
  58 run.completed      'I wrote a two-line summary of both notes to out/summary.md. ...'
ok 58 events, chain intact
```

I set it up so the model goes along with the injected instruction on purpose. If the model
refused by itself, the demo wouldn't show anything about the runtime.

To try a real model, edit `legion.yaml` so a real binding is `general/default` (Ollama at
`http://localhost:11434/v1`, or Anthropic with `ANTHROPIC_API_KEY` set). The agent file stays
the same.

## How it fits together

```
agents + tools (your code)
        │
Runtime ──▶ AgentLoop ──▶ ActionPipeline ──▶ Tool
   │            │               │
   └────────────┴───────────────┴──▶ EventStore ──▶ run state
        │
ModelProvider (OpenAI-compatible, Anthropic, scripted)
```

Each tool call becomes an `Action` and goes through `ActionPipeline`: kill check, lookup, schema,
repeat check, grant, policy, external authority, budget, credentials, run with timeout, output
checks. There's no other way for a tool to run.

More in [ARCHITECTURE.md](ARCHITECTURE.md), the decisions in [docs/adr/](docs/adr/), and the
event format in [docs/events.md](docs/events.md).

## Agents

```yaml
name: notes-assistant
instructions: You summarize notes...
model:
  profile: general/default
  needs: [tools]
tools: [list_notes, read_note, write_summary]
capabilities:
  - files.read:notes/**
  - files.write:out/**
budget: {steps: 10, model_calls: 12, tool_calls: 10, tokens: 50000, wall_seconds: 120}
```

The agent asks for a model profile and the operator's `legion.yaml` decides which model that
means. An agent can only ask for capabilities listed under `authority.grantable` in `legion.yaml`,
otherwise the run won't start. Agent files can't add tools, change policy or name secrets.

## Tools

```python
class ReadArgs(BaseModel):
    path: str

@tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: normalize(a.path))
def read_note(args: ReadArgs, ctx: ToolContext) -> str:
    """Read one file from the workspace."""
    ...
```

Every tool has to declare an effect class (`pure`, `read`, `write_idempotent`, `write`,
`external_irreversible`) and the capabilities it needs. Reads get retried on timeout; a `write`
that times out is recorded as in doubt and never retried. The `resource` function tells Legion what
the call touches, which is how `files.read:notes/**` allows `notes/a.md` but not `private/b.md`.

## Security notes

I assume the model can be talked into anything at any time. Legion doesn't try to detect that; it
limits what the model can actually do. Authority is attached to each task and only ever narrows,
every tool call is checked in one place, secrets stay out of the model's context and the log, and
refusals are logged the same way as successes.

What it can't do is in [THREAT_MODEL.md](THREAT_MODEL.md). The big ones: a manipulated model can
still misuse whatever the grant allows, there's no sandbox, and whoever owns the machine can
rewrite the log.

## Known limitations

- One agent per run. Delegation is Phase 3, although `Grant.attenuate` already exists and is
  tested.
- No resume after a crash yet (Phase 2). The log already records which actions were in flight.
- No approvals yet (Phase 2). Config that needs approval is rejected at load time.
- No MCP, streaming or images.
- The operator declares what each model supports; the resolver doesn't probe endpoints.
- Tool calls in one model turn run one after another.
- Adapters are tested against recorded HTTP responses. I haven't run the real-model tests in CI.
- The token budget caps a call's output but not its input, so one call can go over by the size of
  its prompt. Cost is only checked after each call.
- The hash chain catches edited or missing events but not events cut off the end.

## Roadmap

Next is crash recovery and approvals, then delegation, then more access methods and support for
external agent runtimes (Copilot SDK, Claude Agent SDK, Codex), then MCP, the security-operations
example, and exports plus the NIA adapter. Details in [docs/roadmap.md](docs/roadmap.md).

## Related projects

NIA is my Go control plane for agent identity, credentials and kill switches. MIA is a Python
service for delegated mandates. Legion can talk to either through `IdentityPort` (Phase 7) but
needs neither.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

Apache-2.0
