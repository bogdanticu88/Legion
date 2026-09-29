# ADR 0001: Python 3.12 library and CLI, one process

Status: accepted

## Context

Legion needs good LLM and MCP libraries, structured cancellation for parallel sub-agents, typed
domain objects, and code that security reviewers can read. Python, TypeScript, Go and Rust were
compared. Go was the serious alternative: NIA is written in Go and a single binary is easy to
ship. Agent workloads are I/O bound, so raw concurrency performance does not decide it.

## Decision

Python 3.12 or later, asyncio with `TaskGroup`, pydantic v2 for domain types, mypy in strict
mode. One package, one process, a Typer CLI. No services, no hybrid language split.

## Consequences

- People who write tools and agents can do so in the language they already use.
- Distribution is `pip`/`uv`, not a single binary.
- Native Python tools run inside the harness process, so they are trusted code (see the threat
  model).
