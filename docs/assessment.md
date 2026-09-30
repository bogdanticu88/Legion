# Notes on existing frameworks

I wrote these in September 2026 before starting, to figure out what Legion should and shouldn't
try to be. Where something comes from an announcement rather than from reading the code, I say so.
Sources are at the bottom.

## What's out there

| Project | Good at | What's missing for my purposes |
|---|---|---|
| LangGraph | Graphs, checkpoints, `interrupt()` for human input | Resuming re-runs the interrupted node, so side effects repeat unless you make them idempotent yourself. No notion of authority |
| Microsoft Agent Framework 1.0 (Apr 2026) | Replaces AutoGen (maintenance mode since Oct 2025) and Semantic Kernel. Multi-agent patterns, middleware, telemetry | Governance is middleware. Big surface |
| Microsoft Agent Governance Toolkit (Apr 2026) | Policy engine, narrowing scope on delegation, signing, OWASP agentic mapping, adapters for other frameworks | The closest thing to what I want. It sits on top of frameworks it doesn't control, so it only enforces where they call it. Covers a lot more (SRE, marketplace, RL). Based on the announcement, I haven't read the code |
| OpenAI Agents SDK | Handoffs, guardrails, tool approval with `RunState` pause/resume, tracing, Temporal, sandboxes | Approval is per tool. No attenuation, not much in the way of budgets |
| PydanticAI | Typed tools and outputs, any model, durable execution on Temporal, DBOS, Prefect, Restate | Durability comes from the engine. No authority model. Good reference for typed tools |
| OpenHands SDK (V1) | Event-sourced state, deterministic replay, immutable agent config, MCP | Built for coding. Security is a confirmation/risk layer. This is the design I borrowed most from for events |
| CrewAI | Role/crew/flow ergonomics | Delegation happens in prompts, hard to follow |
| smolagents | Minimal code agents | Code-as-action gives the model the whole interpreter |
| Letta | Stateful agents, memory | A memory server rather than a governed executor |
| Google ADK, AWS Strands, Mastra | Cloud-flavoured or TypeScript kits | Same gaps as above |
| GitHub Copilot SDK (GA Jun 2026) | Embeds Copilot's agent runtime, custom tools, MCP, GitHub or BYOK auth | It's a whole runtime with its own loop, not a model endpoint |
| Claude Agent SDK | Claude Code as a library | Same. Per-user subscription use through the SDK is allowed; shared production automation should use API keys |
| Codex SDK / app-server | Drives the local Codex runtime over JSON-RPC | Same. ChatGPT sign-in is interactive; API keys are the documented way to automate |
| Temporal, DBOS, Restate | Real durable execution | Temporal needs a cluster, which I don't want to require |
| CaMeL and follow-ups | Tracking data flow so prompt injection can't change what the agent does | Needs a restricted plan language and two models. Too heavy for now, but where I'd like to go eventually |
| "Harness Engineering" study (arXiv 2609.00006) | Looked at eleven coding harnesses | None of them use a general agent framework; they all have their own loop. Made me more comfortable writing a small kernel |

## Problems I kept seeing

- Run state lives in the context window, so resuming means replaying a transcript.
- Governance is middleware or callbacks that can be skipped or called in the wrong order.
- Sub-agents are configured separately, so a child can have tools its parent didn't, and spawning
  one resets the budget.
- Approval attaches to a tool name, not to the arguments.
- The SDK retries, the loop retries and the model retries, all at once.
- Resume re-runs writes with no idea whether they already happened.
- Provider abstractions either drop features (caching, reasoning, structured output) or grow a
  flag per vendor.
- MCP tool descriptions go straight into the prompt.
- Subscription runtimes get wrapped as if they were plain models.

## Things I'm not going to build

Graph DSLs, workflow designers, vector memory, tracing dashboards, connector catalogues, a sandbox,
a universal model adapter, a policy language, a durable-execution engine. Other people have done
these well.

## Where Legion might add something

- Every tool call goes through one pipeline inside the runtime. Nothing to forget to call.
- Each task carries a grant that can only get narrower, and child budgets come out of the
  parent's.
- Recorded results get replayed, not re-run. A write that started and never finished is marked as
  in doubt and handled based on what kind of effect it has.
- Approvals are tied to the hash of the exact action, used once, and expire.
- Model provider and authentication are separate, and subscription SDKs are treated as external
  runtimes with weaker guarantees.
- The invariants are tested without any API key.

None of the individual pieces are new. Tracing, multi-agent, MCP and approvals exist elsewhere.
What Legion calls capabilities are really scoped permissions carried as data, closer to macaroons
than to object capabilities. The Governance Toolkit already talks about narrowing on delegation.
What I'm going for is enforcement inside a runtime I control, resume that knows about effects, and
a codebase you can read in an afternoon.

## Changes to my original list of building blocks

- Agent, Task, Run, Tool, Approval and Event stayed.
- Capability, Budget and Credential context folded into Grant. They always travel together and
  have to narrow together.
- Policy became an interface with a rule-table implementation.
- Evidence became Artifact. "Evidence" belongs to the forensics project.
- Added Action (the canonical tool call that policy checks and approvals point at) and Principal
  (who started the run, who approved).

## Sources

- LangGraph replay and idempotency: [Diagrid](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows), [Parmar](https://medium.com/@mehul_parmar/the-hidden-replay-risk-in-langgraph-how-durable-execution-can-burn-you-1d966141e71a)
- [Microsoft Agent Framework lineage](https://alexbevi.com/blog/2026/06/18/two-lineages-one-framework-how-autogen-and-semantic-kernel-became-the-microsoft-agent-framework/)
- [Agent Governance Toolkit announcement](https://opensource.microsoft.com/blog/2026/04/02/introducing-the-agent-governance-toolkit-open-source-runtime-security-for-ai-agents/)
- OpenAI Agents SDK: [running agents](https://openai.github.io/openai-agents-python/running_agents/), [sandboxes](https://developers.openai.com/api/docs/guides/agents/sandboxes)
- [PydanticAI durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)
- [OpenHands Software Agent SDK paper](https://arxiv.org/abs/2511.03690)
- [Harness Engineering](https://arxiv.org/abs/2609.00006)
- [CaMeL](https://arxiv.org/abs/2503.18813)
- [Copilot SDK GA](https://github.blog/changelog/2026-06-02-copilot-sdk-is-now-generally-available/)
- [Claude Agent SDK with a Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- Codex: [auth](https://learn.chatgpt.com/docs/auth), [app-server](https://learn.chatgpt.com/docs/app-server)
