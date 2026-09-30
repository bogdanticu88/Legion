# ADR 0005: Model provider and access method are separate

Status: accepted

## Context

The same model can be reached with an API key, through a company gateway, with workload identity,
or locally with no auth. Combining provider and access multiplies the number of adapters. A
provider interface that only covers what everyone supports loses caching, reasoning and native
structured output.

## Decision

- `ModelProvider` speaks one wire format and lists the features it supports.
- `AccessProvider` builds the auth headers from secret references.
- `ModelResolver` maps an agent's requirement (a profile like `reasoning/high` plus needed
  features) to a configured binding, and refuses to start if none fits.
- Provider options (`models[].options` in `legion.yaml`, `provider_options` on the request) are
  keyed by provider kind. Each adapter reads its own key and can't overwrite the core request
  fields with it.
- Reasoning blocks only go back to the provider that produced them.
- The first version ships OpenAI-compatible and Anthropic adapters, both on httpx.

## Consequences

- Agents say what they need, not which vendor to use.
- A new gateway or local server is usually just config.
