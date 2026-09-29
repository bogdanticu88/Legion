# Legion

Legion is my harness for running LLM agents with real permissions. It runs the agent loop, lets
the agent call tools, and checks every tool call against what the agent is allowed to do before
anything happens. Risky actions wait for a person to approve that exact action. Everything is
written to an append-only, hash-chained event log, and runs can be resumed from it after a pause
or a crash without repeating anything that may already have happened.

It doesn't care which model you use or what the agent is for. An agent can hand parts of a job
to other agents; each child gets narrower permissions and a share of its parent's budget, never
more.

Status: early (Phase 4 of 8, see [the roadmap](docs/roadmap.md)). Children run one at a time. MCP
servers work for tools only. I wouldn't point it at anything that matters.

## Why I'm building it

I kept running into the same gaps. Sub-agents can end up with more access than the agent that
started them. An approval is usually tied to a tool name rather than to the exact arguments. A
crash in the middle of a write often means the write runs again on resume. Run state tends to
live in the context window. Newer "harness of harnesses" projects like Raven go wide, driving
lots of agents; I wanted something narrower that you could trust with real access.

[docs/assessment.md](docs/assessment.md) has my notes on LangGraph, the OpenAI Agents SDK,
PydanticAI, OpenHands, Microsoft's Agent Framework and Agent Governance Toolkit, Raven and others,
including where they're ahead.

## Quick start

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). No API key; the starter project uses
scripted models so it behaves the same every time.

```bash
git clone <repo> legion && cd legion
uv sync
uv run legion init demo && cd demo
uv run legion run agents/assistant.yaml "Summarize the notes"
uv run legion inspect <run-id>
uv run legion verify <run-id>
```

One of the notes tells the assistant to also read `private/salaries.md`. The scripted model does,
and Legion refuses because the agent only has `files.read:notes/**`:

```
  30 action.proposed    read_note on private/salaries.md  23a3fe0e913f
  31 action.refused     read_note: capability_denied: the task's grant does not cover
                        files.read:private/salaries.md
```

I set it up so the model goes along with the injected instruction on purpose. If the model
refused by itself, the demo wouldn't show anything about the harness.

### Approvals

The second starter agent publishes to a channel, which can't be undone, so it stops and asks:

```bash
uv run legion run agents/publisher.yaml "Publish the meeting summary"   # exits 3: paused
uv run legion approvals
uv run legion approval show <approval-id>
uv run legion approve <approval-id> --note "checked the text"
uv run legion resume <run-id>
```

`approval show` prints the tool, target, arguments, who the agent acts for, and the model's own
explanation marked as untrusted. The approval covers that one call and nothing else: a different
channel, different text, a second identical call or changed settings all need a new approval.

### Delegation

The coordinator agent hands the notes job to the assistant:

```bash
uv run legion run agents/coordinator.yaml "Get the notes summarised"
uv run legion tasks <run-id>
```

```
 task                    agent        status     via call   tool calls  model calls
 task_1caa41b055074170   coordinator  completed  -          5/16        8/24
   task_44554b07c47079ec notes-assis… completed  call_1_1   4/8         6/12
```

The child only got the objective and a small context, not the coordinator's conversation. Its
grant is whatever the coordinator held, narrowed to what the assistant's spec asks for, and its 8
tool calls came out of the coordinator's 16. The coordinator's 5 is its own delegate call plus
the child's 4. A child can't be given a capability its parent doesn't have, can't be given more
budget than the parent has left, and can't delegate deeper than the parent allows.

### After a crash

If the process dies, `legion resume <run-id>` picks up from the log. Reads and other safe calls
that were running get run again. A write that was running is marked in doubt and the run waits
for you to check and say what happened:

```bash
uv run legion resume <run-id>          # paused: call w1 may or may not have taken effect
uv run legion reconcile <run-id> w1 --outcome applied --note "file is there"
uv run legion resume <run-id>
```

### MCP servers

MCP tools are used only if `legion.yaml` lists them, with an effect class, a resource argument
and a pin:

```bash
uv sync --extra mcp
uv run legion mcp inspect github      # current pins, what the server claims, unused tools
```

The agent then lists `mcp_github_create_issue` and asks for `mcp.github.create_issue:legion` like
any other capability. If the server changes the tool's description or schema, or the command or
credential used to reach it changes, the pin stops matching and the tool is blocked before
anything is sent. An MCP tool with no effect class given is treated as irreversible, so it needs
approval. See ARCHITECTURE.md and ADR 0018.

To try a real model, edit `legion.yaml` so a real binding is `general/default` (Ollama at
`http://localhost:11434/v1`, or Anthropic with `ANTHROPIC_API_KEY` set). The agent files stay the
same.

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

Every tool call becomes an `Action` and goes through `ActionPipeline`: kill check, lookup, schema,
repeat check, grant, policy, external authority, delegation check, approval, credentials, budget,
run with timeout, output checks. There's no other way for a tool to run. Delegating to another
agent is a tool call too, and a resumed run goes through the same steps.

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

The agent asks for a model profile and `legion.yaml` decides which model that means. An agent can
only ask for capabilities listed under `authority.grantable`, otherwise the run won't start.
Agent files can't add tools, change policy or name secrets.

## Tools

```python
class ReadArgs(BaseModel):
    path: str

@tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: normalize(a.path))
def read_note(args: ReadArgs, ctx: ToolContext) -> str:
    """Read one file from the workspace."""
    ...
```

Every tool declares an effect class (`pure`, `read`, `write_idempotent`, `write`,
`external_irreversible`) and the capabilities it needs. The effect class decides what happens on a
timeout or a crash: safe ones are retried, the others are never run twice automatically. The
`resource` function tells Legion what the call touches. Tools also get `ctx.idempotency_key`,
which stays the same across retries and resumes.

## Security notes

I assume the model can be talked into anything at any time. Legion doesn't try to detect that; it
limits what the model can actually do. Authority is attached to each task and only narrows, every
tool call is checked in one place, spending is capped, irreversible actions wait for a person,
and uncertain writes wait for an operator.

What it can't do is in [THREAT_MODEL.md](THREAT_MODEL.md). The big ones: a manipulated model can
still misuse whatever the grant allows, there's no sandbox, approvers aren't authenticated, and
whoever can write the event store can rewrite it or add to it.

## Known limitations

- Children run one at a time; the parent waits. Several at once is the next step.
- A child's tools come from its own spec. Capabilities bound them, but if a tool uses a stronger
  credential under the same capability name, delegation doesn't narrow that credential.
- A child's wall time is only recorded when it ends, so a child that pauses or crashes gets its
  full time again each stretch (still inside the root's time limit).
- Approvers are whoever runs the CLI as the local OS user. Nothing is signed.
- One machine. Locks are OS file locks next to the store.
- Legion never automatically repeats an action that may have happened. It doesn't promise
  exactly-once execution: an operator decides what happened to in-doubt actions.
- A tool that raises after it already did its work looks like a clean failure. Tools should raise
  `ActionInDoubt` when they don't know.
- Time a tool spent hanging before a crash, and tokens of a response lost in a crash, aren't
  charged.
- There's no command to cancel a paused run other than denying the approval or abandoning the
  in-doubt action.
- The token budget caps a call's output but not its input. Cost is checked after each call.
- The hash chain catches edited or missing events but not events cut off the end.
- MCP: tools only, no resources or prompts. Legion's grant decides which calls reach a server,
  not what the server's own credential allows; the operator records that as `credential_scope`.
  Pins cover how a server is reached and what it says about its tools, not the code it runs.
  A server that acts and then reports an error looks like a clean failure.
- No streaming or images. The operator declares what each model supports.
- The OpenAI-compatible adapter has been run against qwen2.5:1.5b on Ollama. That model is too
  small to use delegation, so delegation is only tested with scripted and generated model output.
  The Anthropic adapter hasn't been run against the real API.

## Roadmap

Next: several children at once, then plans a person can approve once, built-in specialist agents,
memory that remembers where facts came from, and exports plus the NIA identity adapter. Details in
[docs/roadmap.md](docs/roadmap.md).

## Related projects

NIA is my Go control plane for agent identity, credentials and kill switches. MIA is a Python
service for delegated mandates. Legion can talk to either through `IdentityPort` (later) but needs
neither.

## Development

```bash
uv sync --extra mcp
uv run pytest
uv run ruff check src tests && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

Apache-2.0
