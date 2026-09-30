# ADR 0009: MCP is a tool adapter with an operator-owned manifest

Status: accepted, built (details in ADR 0018)

## Context

MCP is how most people will connect tools, but a server's description of its own tools is
untrusted text that goes into the prompt, and it can change after you've approved it.

## Decision

MCP tools come in through an adapter that implements the normal `Tool` protocol. The operator
writes a manifest that gives each tool its effect class, required capabilities, resource argument
and a pinned hash of the server's name, description and schema. Tools without a manifest entry
aren't registered, and a changed hash blocks the tool until the operator re-pins it. MCP doesn't
become Legion's internal model.

## Consequences

- An MCP server is governed only as far as its manifest says.
- Description changes are caught when connecting and again before each call.
