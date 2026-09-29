# ADR 0009: MCP is a tool adapter with operator-owned manifests

Status: accepted (implementation in Phase 5)

## Context

MCP is the common way to reach tools, but a server's own description of its tools is untrusted
input that ends up in the prompt, and servers can change descriptions after they are approved.

## Decision

MCP tools enter Legion through an adapter that implements the ordinary `Tool` protocol. An
operator-owned manifest supplies each tool's effect class, required capabilities, resource
argument and a pinned hash of the server's name, description and schema. A tool without a manifest
entry is not registered. A changed hash blocks the tool until the operator re-pins it. MCP never
becomes the internal architecture.

## Consequences

- Governance of an MCP server extends exactly as far as its manifest.
- Rug-pull changes are caught at connection time, not after use.
