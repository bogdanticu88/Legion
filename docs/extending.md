# Extending Legion

Legion has a handful of seams where your code or configuration plugs in. Some are loaded from
`legion.yaml`; others currently need you to build `Legion` in Python yourself or add a line to
the config loader. This page says which is which.

Whatever you add, a tool call still goes through the one action pipeline: grant, policy,
approval, budget, credentials, then the tool. Extensions can narrow what happens; none of them
gets a path around the pipeline.

| Extension point | Interface | From `legion.yaml`? | Start from |
|---|---|---|---|
| Native tool | `@tool` in `legion/tools/native.py` | yes, `tool_modules` | [writing-tools.md](writing-tools.md) |
| Policy rules | `Rule` in `legion/authority/policy.py` | yes, `policy.rules` | the starter `legion.yaml` |
| Custom policy engine | `PolicyDecisionPoint` protocol, same file | no, Python only | `RuleTablePolicy` |
| Credential authority | `CredentialAuthority` in `legion/ports/credentials.py` | yes, `module:` | [examples/authorities.py](../examples/authorities.py) |
| Identity authority | `IdentityPort` in `legion/ports/identity.py` | only the built-in NIA adapter | `NullIdentityPort` |
| Model provider | `ModelProvider` in `legion/models/base.py` | only built-in kinds | `openai_compat.py` |
| MCP servers | `mcp_servers` config, `legion/tools/mcp.py` | yes | README, ADR 0018 |
| Reading runs | the event log, [events.md](events.md) | n/a | `legion inspect --json` |

The Python modules are Legion's internals. Before 1.0 they can change between releases; the
protocols above are the parts meant to be implemented.

## Tools

See [writing-tools.md](writing-tools.md). A module listed under `tool_modules` defines `TOOLS`,
and each tool declares its effect class, capabilities and resource.

## Policy rules

Rules in `legion.yaml` decide `allow`, `deny` or `require_approval` for calls matching a tool
name, a capability or an effect class (glob patterns). Deny beats approval, which beats allow. A
rule can also raise the credential assurance a call needs (`credential_assurance: bound`). A rule
that matches no tool or capability is a configuration error, so a typo can't leave you
unprotected.

```yaml
policy:
  default: allow
  rules:
    - decision: require_approval
      effect: external_irreversible
    - decision: require_approval
      capability: "files.write"
```

A different policy engine (OPA, Cedar, your own) implements `PolicyDecisionPoint.evaluate` and
is passed as `Legion(policy=...)` from Python; the config file can't name one yet.

## Credential authorities

A credential authority issues a credential for one call: exactly the permissions and resource
the call was authorized for, bound to that call, its Action and its Grant. Legion checks what it
says against what it asked for and decides how much it trusts the result
([ADR 0019](adr/0019-credential-authority.md)).

Implement three methods on an object with a `name`:

- `issue(request) -> IssuedCredential`: the secret, a reference, and evidence describing it
- `status(ref) -> CredentialStatus`: `ACTIVE`, `EXPIRED` or `REVOKED`; raise `UnknownCredential`
  for a reference you have no record of
- `revoke(ref)`

[`examples/authorities.py`](../examples/authorities.py) is a complete, minimal one. Load it:

```yaml
credential_authorities:
  local: {module: authorities.py, trusted: true}
credentials:
  github:
    authority: local
    provider: github
    permissions: {repo.read: [contents:read]}
```

`trusted: true` means its evidence can make a credential `verified` or `bound`; untrusted
authorities are `declared` at most. The module is operator code and runs in the Legion process.

### Try it

Starting from the to-do example in [writing-tools.md](writing-tools.md):

1. Copy `examples/authorities.py` from a Legion checkout into the project, next to `legion.yaml`.
2. Add the authority and a credential mapped onto it:

   ```yaml
   # file: legion.yaml (additions)
   credential_authorities:
     local: {module: authorities.py, trusted: true}
   credentials:
     todo_api:
       authority: local
       provider: todo-service
       permissions: {todos.write: [todo:append]}
   ```

3. Ask for the credential on the tool: `@tool(effect=EffectClass.WRITE, capabilities=["todos.write"], resource=lambda a: a.list, credentials=["todo_api"])`,
   and use it inside as `ctx.credentials["todo_api"].reveal()`.
4. Run the agent again, then `legion credentials <run-id>`: the credential for the inbox call was
   issued for exactly `todo:append` on `inbox`, bound to that call, assurance `bound`; the call to
   `boss` was refused before any credential was asked for.

The contract is executable: `tests/conformance/test_credential_authority_contract.py` runs the
same tests against every authority Legion knows about. Add yours to `AUTHORITIES` there and run
`uv run pytest tests/conformance`.

## Identity authorities

`IdentityPort` answers who an agent is, whether it is killed, whether an action is allowed by an
outside authority, and whether a delegation may happen. Without one, `NullIdentityPort` is used:
identities are local and nothing is ever killed. The config file can only select the built-in
NIA adapter today; your own implementation is passed as `Legion(identity=...)` from Python.

## Model providers

The built-in kinds are `scripted` (replays a file; what the starter project and the tests use),
`openai_compat` (any OpenAI-compatible server: Ollama, vLLM, llama.cpp, gateways) and
`anthropic`. A provider with another wire format implements `ModelProvider` (`kind`,
`supported` features, `generate`, `aclose`) and is passed to `ModelResolver` from Python. Providers don't retry;
Legion does. `tests/unit/test_providers.py` tests the built-ins against recorded responses, with
no network.

## MCP servers

Install the extra (`uv sync --extra mcp`, or `pip install 'legion-runtime[mcp]'`), list the server
and each tool you want under `mcp_servers` with a pin, and `legion mcp inspect <server>` shows
what the server offers and the pins to review. MCP tools go through the same pipeline and need
the same kind of capability as native tools. See the README and
[ADR 0018](adr/0018-untrusted-mcp-servers.md).

## Reading the event log

Every run is an append-only, hash-chained list of events in SQLite (`.legion/legion.db`).
`legion inspect <run-id> --json` prints them one per line; [events.md](events.md) documents every
type and field; `legion verify <run-id>` recomputes the chain. Exporters and analysis tools can
read the log without touching Legion's code.

## Embedding Legion in Python

`legion.kernel.runtime.Legion` is what the CLI builds from `legion.yaml`. Build it yourself to
plug in a policy engine, identity port or model provider the config file can't name.
`tests/support.py` (`build(...)`) is a compact example of wiring one up with a scripted model and
an in-memory event store. This is not yet a stable API.

## An advanced example: NIA

NIA is a separate project: a control plane for agent identity and credentials. Legion ships an
adapter for each of the two ports, both optional and imported only when configured:

- `legion.adapters.nia`: an `IdentityPort` backed by NIA's agent registry and kill switch
  ([ADR 0020](adr/0020-nia-identity-authority.md))
- `legion.adapters.nia_credentials`: a `CredentialAuthority` backed by NIA's scoped credentials
  ([ADR 0021](adr/0021-nia-credential-authority.md))

They show what a real external implementation involves: an HTTP client that fails closed,
translation of the service's evidence into Legion's generic shape without inventing agreement,
separate tokens for separate roles, and hostile-service tests (`tests/nia_lab.py`,
`tests/nia_cred_lab.py`). They follow the same contracts as everything else on this page; they
don't define them.
