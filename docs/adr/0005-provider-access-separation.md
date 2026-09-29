# ADR 0005: Provider and access are separate, capabilities are discovered

Status: accepted

## Context

The same model can be reached with an API key, through an enterprise gateway, through workload
identity, or locally with no authentication. Folding the access method into the provider
multiplies adapters. A lowest-common-denominator provider interface drops prompt caching,
reasoning blocks and native structured output.

## Decision

- `ModelProvider` speaks one wire protocol and reports `ModelCapabilities`.
- `AccessProvider` produces authentication for a request from secret references.
- `ModelResolver` maps an agent's `ModelRequirement` (a profile such as `reasoning/high` and the
  features it needs) to a configured binding and refuses to start when nothing fits.
- `ModelRequest.provider_options` is keyed by provider name; each adapter reads only its own key
  and cannot override the core fields.
- Reasoning blocks are opaque and are only replayed to the provider that produced them.
- Phase 1 adapters: OpenAI-compatible Chat Completions and Anthropic Messages, both over httpx.

## Consequences

- Agents name what they need, not which vendor provides it.
- Adding a gateway or a local server is usually configuration, not code.
