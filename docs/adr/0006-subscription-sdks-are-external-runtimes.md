# ADR 0006: Subscription SDKs are external runtimes, not model providers

Status: accepted, but deprioritised. Driving other harnesses is off the main roadmap: it weakens
what Legion can promise. It may come back as an untrusted-worker adapter.

## Context

In 2026 the GitHub Copilot SDK, the Claude Agent SDK and the Codex app-server are the official ways
to use a subscription programmatically. Each of them runs its own agent loop and its own tools. If
I wrapped one as a `ModelProvider`, its tools would run outside Legion while the events suggested
Legion had checked them.

Scraping sessions or cookies, or calling undocumented consumer endpoints, is off the table for good.

## Decision

They'll be supported as an `ExternalAgentRuntime` that a task can hand work to. Each one declares
what Legion can and can't see or control (which Legion tools it can call back into, whether its
own tools are switched off). Events mark results that came from one. Only documented auth is used,
and per-user subscription access is documented as per-user.

## Consequences

- What Legion claims to enforce stays true.
- People with those subscriptions get a supported way in, with the limits written down.
