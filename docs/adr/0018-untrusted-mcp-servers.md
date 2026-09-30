# ADR 0018: MCP servers are untrusted and get no authority of their own

Status: accepted

## Context

ADR 0009 said MCP would come in as a tool adapter with an operator manifest. This is how it's
built. The target is the 2026-07-28 spec: stateless requests, no initialize handshake, tools
listed with `tools/list` and called with `tools/call`. The spec says tool annotations must be
treated as untrusted unless the server is, and that server names aren't unique.

What an MCP server can do to a client like Legion:

- describe a tool one way when it's reviewed and another way later
- put instructions in descriptions, schemas or results
- claim a tool is read-only or idempotent when it isn't
- offer extra tools, or two tools with the same name
- return huge, deeply nested or odd content (images, links, requests for more input)
- hang, fail, or drop the connection after it has already done the work
- lie in an error message
- write to stderr, which ends up in the operator's terminal

## Decision

### SDK

The official Python SDK (`mcp` 2.x), as the optional extra `mcp` (`uv sync --extra mcp`), so a
Legion without MCP doesn't pull it in. The SDK's own `MCPServer` is enough to build the hostile test servers.

FastMCP was the obvious alternative. It's built on the same SDK and mostly adds server-side
features (composition, proxies, auth providers) and a client with its own conveniences. Legion
only needs a client, needs to know exactly when a request goes out, and needs to turn off
anything that retries or answers on its own. One layer is easier to reason about than two, so
FastMCP isn't used. If the SDK's client ever gets in the way, the transport layer underneath it is
still usable directly.

### Manifest

Each server is declared in `legion.yaml` under `mcp_servers`: transport (`stdio` with a command,
or `http` with a url), secret references for env or headers, and a `tools` map. Only tools named
in the map are registered. Anything else the server offers is listed by `legion mcp inspect` and
never used.

For each tool the operator gives:

- `effect`: defaults to `external_irreversible`, so an unclassified tool needs approval under the
  default rules and is never repeated automatically
- `capabilities`: extra ones on top of the tool's own
- `resource_arg`: which argument is the resource for capability matching
- `pin`: required
- optionally `remote` (the name on the server), `description` (replaces the server's),
  `timeout_s`, `max_attempts`

### Names and capabilities

Tool key `create_issue` on server `github` becomes the local tool `mcp_github_create_issue` and
needs `mcp.github.create_issue`. Server ids are operator-chosen and unique in the config, so two
servers with a `delete_repo` tool are two different capabilities. The server's own name for
itself is recorded and shown as unverified; it plays no part in naming or matching.

Discovered isn't authorized. A tool the server offers is still:

1. not registered unless it's in the manifest and its pin matches
2. not offered to the model unless the agent lists it
3. refused unless the task's grant covers its capability and resource
4. then through policy, approval and budget like any other tool

All of this happens in the normal pipeline before anything is sent to the server.

### Pins and server identity

The server fingerprint is a digest of the server id, transport, command, cwd, url and the names
of the env and header secret references. A tool's pin is a digest of the fingerprint and
everything the server says about the tool: name, description, input and output schema,
annotations. So changing a description, a schema, an annotation, the command, the url or the
names of the secret references the server gets breaks every affected pin. Changing the secret
value behind a reference doesn't.

Pins are checked at startup. By default (`pin_check: every_call`) the tool list is fetched again
and the pin re-checked before every call. A tool whose pin no longer matches is blocked for the
rest of the process. The call fails cleanly, and nothing is sent. Re-pinning is an operator
decision: `legion mcp inspect <server>` shows the current pin next to the manifest entry.
`pin_check: discovery` skips the per-call check for servers where the extra request costs too
much.

The fingerprint describes how Legion reaches the server, not the code running there. A command
like `npx some-server` that fetches the latest version can change behaviour without changing a
pin. Pin versions in the command; `mcp inspect` warns when `npx`, `uvx` and similar runners are
given a package without one. A bare command name is found through `PATH`, so the pin covers the
name, not the binary.

### Descriptions and annotations

A tool's top-level description goes to the model after control and bidi characters are removed
and the text is cut to 1000 characters. The operator can replace it in the manifest. Text inside
the input schema (property descriptions, enums) goes to the model as the server wrote it: it's
pinned, so it can't change after review, but it isn't cleaned. Schemas may only use `$ref`
inside themselves; one that points at a URL or a file blocks the tool, and validation never
retrieves anything. Annotations
(`readOnlyHint` and the rest) are pinned and shown in `mcp inspect` as claims, and never used for
anything. The effect class comes from the manifest only.

### Results

A tool result is untrusted tool output like any other. Text content is passed on. Images, audio
and embedded blobs are replaced by a note. Resource links are shown and never fetched. A result
over `max_response_bytes` (64 KiB), deeper than `max_depth` (32) or with more than `max_items`
(1000) items in a list or object is withheld: the model gets an error saying the tool ran and its
response was withheld. An `isError` result goes back to the model as an error result.

Calls go through the SDK session's `call_tool` with `allow_input_required=True`, so a request
for more input comes back as a result instead of being answered by client callbacks. Legion
doesn't provide input to servers; the model gets an error saying so. The response cache is off
so every tool listing is fresh.

### Failures and recovery

- Anything that fails before the request goes out (can't connect, the pin check fails or times
  out) is a clean `ToolFailed`.
- Anything that fails after it may have gone out (timeout, dropped connection, a response the SDK
  rejects) is retryable for `pure`, `read` and `write_idempotent` tools (up to the tool's
  `max_attempts`, which defaults to 1 for MCP) and in doubt for `write` and
  `external_irreversible`. An in-doubt call pauses the run for `legion reconcile`, the same as
  native writes (ADR 0013). Legion doesn't send MCP servers an idempotency key, so
  `write_idempotent` should only be used for a server that dedupes on its own.
- Connecting and listing tools are bounded by `discovery_timeout_s` (10 s). A tool's `timeout_s`
  must be longer, so a stalled pin check can't look like a write that timed out.
- A server that's unreachable at startup contributes no tools; an agent that needs them refuses
  to start and says why.

### Approvals and delegation

The tool spec now carries an `origin` (server, fingerprint, remote name, pin, credential scope),
and the approval binding includes it. An approval for a call on one pinned tool doesn't cover the
same call after a re-pin or on another server. If the tool changes between approval and the call,
the pin check stops the call.

A child gets MCP tools the same way it gets native ones: from its spec, bounded by the parent's
grant. Nothing about MCP widens delegation.

### Credentials

Legion's grant limits which calls reach the server. It doesn't limit what the server can do with
its own credentials. A server holding a token that can delete every repo can do that on any call
Legion lets through, and a compromised server can do it without being called. The manifest has a
free-text `credential_scope` where the operator writes down what the server's credential can do.
It's recorded on every `action.proposed` and shown in `approval show`, marked as not narrowed by
Legion's grant.

`IdentityPort.credential_evidence(server)` is where an identity service (NIA) can report what
credential a server actually holds. If it returns something, that's recorded next to the call. No
secret is recorded, only references and whatever the identity service reports. Nothing
implements it yet.

End-to-end least privilege only holds if the server's own credential is as narrow as the grants
that reach it. Legion can't check that.

### Scope

Tools only. Resources and prompts aren't used: a prompt is server-written text meant for the
model's instructions, and resources would need their own read capabilities and limits. Sampling,
roots and logging are deprecated in the spec and not supported. Stdio server stderr goes to
`.legion/mcp/<server>.stderr.log` (mode 0600, not scrubbed) instead of the operator's terminal.
Remote `http` servers need `https`; plain `http` is allowed only to 127.0.0.1 or [::1], and URLs can't
carry a username or password. A stdio server's `cwd` is relative to `legion.yaml`, and its
env and header secrets are scrubbed from events and tool output like any other secret.

## Consequences

- An MCP tool is exactly as governed as a native tool, plus the pin checks. A server can't add
  tools, widen a tool, or change what a tool claims without the operator re-pinning.
- The per-call pin check doubles the requests to a server. `pin_check: discovery` is available
  but gives up catching a change between startup and the call.
- A result that tells the model to do something else can still make the model try; the grant
  decides. The hostile-server tests include this case.
- A server that did the work and then reports an error looks like a clean failure. Legion can't
  know better.
- Response limits apply to what reaches the model. The SDK has already read the whole message by
  then, so a server can still use a lot of memory.
