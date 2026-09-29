# ADR 0006: Subscription SDKs are external runtimes, not model providers

Status: accepted (implementation in Phase 4)

## Context

Subscription-backed access with official programmatic support exists in 2026 through the GitHub
Copilot SDK, the Claude Agent SDK and the Codex app-server. Each of them runs its own agent loop
and executes tools itself. Wrapping one as a `ModelProvider` would hand tool execution to a loop
Legion does not control while the events still claimed Legion enforced it.

Credential scraping, cookie extraction and undocumented consumer endpoints are out of scope,
permanently.

## Decision

These SDKs will be supported as an `ExternalAgentRuntime` that a task can delegate to. Each
declares a guarantee level: which Legion tools it may call back into, whether its own built-in
tools are disabled, and whether its actions are visible to Legion. Events mark every result that
came from an external runtime. Only documented authentication paths are used, and per-user
subscription access is documented as per-user.

## Consequences

- Legion's enforcement claims stay true.
- Users of those subscriptions get a supported path with its limits written down.
