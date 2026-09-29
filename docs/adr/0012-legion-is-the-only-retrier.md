# ADR 0012: Legion is the only retrier

Status: accepted

## Context

Vendor SDKs retry internally, agent loops retry, and models retry by calling a tool again. Stacked
retries turn one outage into a bill.

## Decision

Model adapters use httpx directly with no transport retries. All retries happen in one place,
bounded by `RetryPolicy`, and each attempt is recorded and counted against the budget. Tool
retries are limited by effect class. Repeated identical actions from the model are refused on the
third attempt and fail the task on the fifth.

## Consequences

- Adapters are a few hundred lines each instead of a dependency.
- New provider features need adapter work instead of an SDK upgrade.
