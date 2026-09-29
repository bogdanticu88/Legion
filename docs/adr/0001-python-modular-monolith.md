# ADR 0001: Python library and CLI in one process

Status: accepted

## Context

I looked at Python, TypeScript, Go and Rust. Go was the real alternative since NIA is written in
it and a single binary is nice to ship. But agent work is mostly waiting on network calls, so raw
performance isn't the deciding factor, and most people writing tools and agents use Python.

## Decision

Python 3.12+, asyncio, pydantic for the domain types, mypy strict. One package, one process, a
Typer CLI. No services.

## Consequences

- Tool authors can use the language they already know.
- Installed with pip or uv, not shipped as a binary.
- Native tools run in the Legion process, so they're trusted code (see the threat model).
