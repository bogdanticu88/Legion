# Landscape assessment

Written September 2026 before any code, to decide what Legion should and should not be. Sources
are listed at the end. Where a claim comes from a vendor announcement rather than from reading
the code, it says so.

## What exists

| Project | Solves well | Gap that matters for Legion |
|---|---|---|
| LangGraph 1.x | Graph orchestration, checkpointers, `interrupt()` for human input | Resume re-runs the interrupted node, so side effects repeat unless the developer makes them idempotent. Checkpoints are save points, not durable execution. No notion of authority. |
| Microsoft Agent Framework 1.0 (April 2026) | Successor to AutoGen (maintenance mode since October 2025) and Semantic Kernel. Multi-agent patterns, sessions, middleware, telemetry | Governance is middleware. Large surface. |
| Microsoft Agent Governance Toolkit (April 2026) | Policy engine, scope narrowing on delegation, signing, OWASP agentic risk mapping, adapters for other frameworks | The closest thing to Legion's security story. It is a governance layer over frameworks it does not own, so enforcement depends on those frameworks calling it. Broad scope (SRE, marketplace, RL training). |
| OpenAI Agents SDK | Handoffs, guardrails, tool approval with `RunState` pause and resume, tracing, Temporal integration, sandbox agents | Approval is per tool. No attenuation, thin budgets. |
| PydanticAI | Typed tools and outputs, model-agnostic, durable execution on Temporal, DBOS, Prefect, Restate | Durability is delegated to an engine. No authority model. A good reference for typed tools. |
| OpenHands Software Agent SDK (V1) | Event-sourced state, deterministic replay, immutable agent configuration, typed tools, MCP | Coding-focused. Security is a confirmation and risk-analyzer layer, not an authority model. The best reference for Legion's event design. |
| CrewAI | Role, crew and flow ergonomics | Delegation is prompt-driven and opaque. |
| smolagents | A minimal code-agent loop | Code-as-action gives the model the interpreter's full authority. |
| Letta | Stateful agents and memory | Memory-centric server, not a governed executor. |
| Google ADK, AWS Strands, Mastra | Cloud-aligned or TypeScript-first kits | Same gaps: no attenuation, no approval bound to arguments. |
| GitHub Copilot SDK (GA June 2026) | Embeds Copilot's agent runtime, custom tools, MCP, GitHub or BYOK auth | A whole runtime with its own loop, not an inference endpoint. |
| Claude Agent SDK | Claude Code's runtime as a library | Same. Per-user subscription use is allowed through the SDK; shared production automation is directed to API keys. |
| Codex SDK and app-server | JSON-RPC control of the local Codex runtime | Same. ChatGPT sign-in is interactive; API keys are the documented path for automation. |
| Temporal, DBOS, Restate | Real durable execution | Temporal needs a cluster, which rules it out as a requirement here. |
| CaMeL and follow-ups | Data-flow capabilities that make prompt injection structurally ineffective | Needs a restricted plan language and a two-model split. Too heavy for a general runtime, but the right direction for future work. |
| "Harness Engineering" study of eleven coding harnesses (arXiv 2609.00006) | Finds that none of Claude Code, Codex CLI, Gemini CLI, OpenHands, Aider and others import a general agent framework; all use hand-rolled async loops | Supports building a small kernel rather than wrapping a framework. |

## Mistakes that recur across frameworks

1. The context window is treated as the state, so resume means replaying a transcript and hoping.
2. Governance is middleware or callbacks, which can be skipped, misordered, or bypassed by a tool
   that calls another tool directly.
3. Delegation does not carry authority. Sub-agents are built from configuration, so a child can
   hold tools its parent never had, and spawning a child resets the budget.
4. Approval is a flag on a tool name, not bound to the exact arguments.
5. Retries happen in the SDK, in the loop and in the model at the same time.
6. Resume re-executes side effects without any notion of an outcome being unknown.
7. Provider abstractions either drop useful features or grow a flag for every vendor.
8. Tool descriptions from MCP servers are trusted and pasted into the prompt.
9. Subscription runtimes are wrapped as if they were model endpoints, which silently hands tool
   execution to someone else's loop.

## What Legion should not build

Graph DSLs, workflow designers, vector memory, tracing backends, dashboards, connector
catalogues, sandboxes, a universal model adapter, a policy language, a durable-execution engine.

## Where Legion can differ

- **One path.** Every effect is an `Action` that passes one ordered pipeline inside the kernel.
  There is no second path and nothing to forget to call.
- **Authority as data that only narrows.** A `Grant` is attached to every task. Child grants are
  checked as subsets and child budgets are carved out of the parent's.
- **Honest side-effect semantics.** Recorded results are replayed, not re-executed. An action that
  started and never finished is in doubt, and its declared effect class decides what happens next.
- **Approvals bound to a canonical action hash**, single use, with expiry.
- **Provider and access kept apart**, with capability discovery and a per-provider escape hatch.
  Subscription SDKs are modelled as external runtimes with weaker guarantees.
- **Invariant tests** that run without any API key.

## What is not new here

Tracing, multi-agent orchestration, MCP support and pause-for-approval all exist elsewhere.
"Capability-based security" would overstate it: Legion carries attenuating scoped permissions as
data, closer to macaroon semantics than to object capabilities. The Agent Governance Toolkit
already talks about delegation narrowing. Legion's claim is narrower: enforcement inside a
runtime it owns, effect-class-aware resume, and a codebase small enough to read in an afternoon.

## Changes to the original primitive list

| Proposed | Outcome |
|---|---|
| Agent | Kept, as an immutable specification with no runtime state |
| Task | Kept, a node in a tree with its own state machine |
| Run | Kept, one root task tree plus its event log and budget |
| Tool | Kept, with a mandatory effect class |
| Capability | Became a value type inside a Grant |
| Policy | Kept as an interface; the built-in implementation is a rule table |
| Budget | Merged into Grant (limits) and Ledger (consumption) |
| Credential/Identity context | Merged into Grant as identity context; secrets are references resolved only at execution |
| Approval | Kept, bound to an action hash |
| Event | Kept, and made the source of truth |
| Evidence | Renamed Artifact. Evidence is the forensics project's word |
| (new) Grant | The authority envelope |
| (new) Action | The canonical proposed tool call that policy judges and approvals bind to |
| (new) Principal | Who started the run and who approves |

## Sources

- LangGraph replay and idempotency: [Diagrid](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows), [Parmar](https://medium.com/@mehul_parmar/the-hidden-replay-risk-in-langgraph-how-durable-execution-can-burn-you-1d966141e71a)
- [Microsoft Agent Framework lineage](https://alexbevi.com/blog/2026/06/18/two-lineages-one-framework-how-autogen-and-semantic-kernel-became-the-microsoft-agent-framework/)
- [Microsoft Agent Governance Toolkit announcement](https://opensource.microsoft.com/blog/2026/04/02/introducing-the-agent-governance-toolkit-open-source-runtime-security-for-ai-agents/)
- OpenAI Agents SDK: [running agents](https://openai.github.io/openai-agents-python/running_agents/), [sandboxes](https://developers.openai.com/api/docs/guides/agents/sandboxes)
- [PydanticAI durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)
- [OpenHands Software Agent SDK paper](https://arxiv.org/abs/2511.03690)
- [Harness Engineering, eleven coding harnesses](https://arxiv.org/abs/2609.00006)
- [CaMeL, Defeating Prompt Injections by Design](https://arxiv.org/abs/2503.18813)
- [Copilot SDK GA](https://github.blog/changelog/2026-06-02-copilot-sdk-is-now-generally-available/)
- [Claude Agent SDK with a Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- Codex: [authentication](https://learn.chatgpt.com/docs/auth), [app-server](https://learn.chatgpt.com/docs/app-server)
