# ADR 0012: Only Legion retries

Status: accepted

## Context

Vendor SDKs retry, agent loops retry, and models retry by calling the tool again. Stack those and
one outage turns into a big bill.

## Decision

The model adapters use httpx directly without transport retries. All retrying happens in one
place, limited by `RetryPolicy`, and every attempt is recorded and charged. Tools are only retried
if their effect class allows it. A model repeating the same call is refused the third time and
stopped the fifth.

## Consequences

- Adapters are a couple of hundred lines each instead of a dependency.
- New provider features need adapter changes rather than an SDK upgrade.
