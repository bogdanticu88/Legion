# Threat model

What Legion protects, what it only limits, and what it can't do anything about. "Now" means it's in
the current code; otherwise the phase is given.

## What's being protected

- credentials (model API keys, tool credentials)
- what a run is allowed to do (its grants)
- budget (money, tokens, time)
- the event log
- whatever the tools read or change

## Who's trusted

| Part | Trusted? | Notes |
|---|---|---|
| `legion.yaml`, tool modules, policy, agent files | yes | Written by the operator. Can grant anything; an agent file also sets its own budget. The loader refuses files that would load differently from how they read (repeated keys, aliases, rules that match nothing). |
| Legion and native Python tools | yes | Same process. A malicious tool can do anything Python can. |
| Model output | no | Shaped by everything the model read, including attacker text. |
| Tool output | no | Can contain injected instructions. |
| MCP servers: descriptions, schemas, annotations, results, errors, stderr | no | They end up in the prompt, can change, and can lie. The server's own credentials are outside Legion's control. |
| NIA, when configured as identity authority | yes, for identity and kill state | Its answers decide whether an agent may act. It says nothing about what an agent's credentials can do. |
| Human approver | mostly | Can be rushed or phished. Not authenticated: it's whoever runs the CLI. |
| The host | yes | Whoever owns it owns the process and the log. |

The main assumption: the model can be manipulated at any point. Legion doesn't try to notice. It
limits what the model can make happen.

## Threats

| Threat | What Legion does | When | What's left |
|---|---|---|---|
| Prompt injection, direct or through tool output | Every action is checked against the grant and policy whatever the model wants. Tool output goes back to the model as data, never to the harness as instructions | now | Anything the grant allows can still happen. There's no tracking of which input influenced which decision |
| Malicious native tool | nothing | - | Tools are trusted code. Review them |
| Malicious MCP server, tool poisoning, rug pulls | Only tools in the operator's manifest are registered, each with a pin over the server's identity (transport, command, cwd, URL, secret reference names) and the tool's name, description, schemas and annotations. Pins are checked at startup and, by default, before every call; a changed tool is blocked and nothing is sent. The top-level description is cleaned and capped, and can be replaced by the operator | now | A pinned server can still return hostile results. Schema text reaches the model uncleaned. The pin doesn't cover secret values or the code: `npx pkg` without a version, or a command found through `PATH`, can change underneath (`mcp inspect` warns about unversioned runners). `pin_check: discovery` gives up the per-call check |
| MCP discovery treated as authority | Each MCP tool needs `mcp.<server>.<tool>`, has to be listed by the agent and granted to the task, and goes through the same pipeline. Same-named tools on two servers are different capabilities | now | |
| MCP annotations lying about effects | The effect class comes from the manifest and defaults to `external_irreversible`. Annotations are recorded and shown, never used | now | An operator who sets `effect: read` on a tool that writes gets retries on it |
| MCP result steering the model | Results are tool output: text only, images and blobs replaced, links never fetched, size, depth and item limits. Whatever the model then asks for is checked against the grant | now | The model can still use anything the grant allows |
| MCP server acting and then failing | Failure before sending is clean. After sending, `pure`, `read` and `write_idempotent` tools are retryable (up to `max_attempts`, default 1 for MCP) and re-run on resume; `write` and `external_irreversible` go in doubt. A request for more input ends as an error | now | A server that did the work and returns an error looks like a clean failure. Legion sends no idempotency key to MCP servers, so only mark a tool `write_idempotent` if the server dedupes on its own |
| MCP server credentials broader than the grant | The operator writes down `credential_scope`, recorded on every call and shown on approvals. `IdentityPort.credential_evidence` can report the real credential | interface now, NIA adapter later | Legion's grant limits which calls reach the server, not what the server's own credential allows. No end-to-end least privilege unless the server's credential is also narrow |
| MCP server hanging or flooding | Connect and listing bounded by `discovery_timeout_s`, calls by `timeout_s`. Oversized responses are withheld from the model. Stdio stderr goes to a log file readable only by the user, not the terminal. Remote servers need https (plain http only to 127.0.0.1 or [::1]) and URLs can't carry credentials | now | The SDK reads a whole response into memory before Legion sees its size. The stderr log isn't scrubbed |
| Poisoned tool output | Output schema checks, size limit, secret redaction | now | Schemas don't catch meaning |
| Credential theft | Config holds references only, and config errors don't repeat rejected values. Every secret reference in the configuration (tool credentials, model API keys, MCP env and headers) is resolved when a run starts or resumes and scrubbed from every event and from tool output, keys included. Provider errors have the request's own headers removed | now | A tool that encodes or splits a secret gets past the scrubbing. Secrets under 4 characters aren't scrubbed. An MCP server's stderr log isn't scrubbed |
| Secrets in logs | `Secret` won't print its value. Tests check the database file for a planted secret | now | A secret the model passes as a plain argument isn't recognised as one |
| Privilege escalation | Root grant limited by `grantable`, tool must be given to the agent, capability checked on name and resource, `..` never matches | now | Broad capabilities like `files.*` give broad access |
| Escalation through delegation | Delegation is an action through the pipeline, needing `agent.delegate:<name>`. Child capabilities must be within the parent's grant and the child's spec; depth and fan-out only shrink; a run has a task cap; `Grant.attenuate` refuses anything wider. Property-tested over random trees | now | Tools a child can use come from its spec. If a tool uses a stronger credential under the same capability name, the credential isn't attenuated. Name capabilities by the strength of what's behind them |
| Budget multiplication through children | Child budgets are reserved from the parent and settled when the child ends; a child can't be given more than the parent has left after paying for the delegation | now | A child that overshoots its token limit by one call's prompt is charged to the parent in full |
| Impersonation between agents | Child identity is derived by the harness (same principal, parent added to the chain); nothing identity-related can be passed to `delegate`. Kill checks cover every ancestor | now | |
| Approval reuse across agents | An approval is bound to one call in one task; siblings and parents need their own | now | |
| Injection travelling between agents | The child only gets the objective and context it's given; the parent gets a result summary, not the transcript | now | Whatever is in that context or result can still carry injected text. Grants limit what it can do |
| Cross-tenant access | Not multi-tenant | not built | Don't run tenants that distrust each other in one process |
| Confused deputy | Tools only get credentials the operator bound to them | now | A tool with broad credentials that takes a target from the model is still a deputy. Limit it with resource capabilities |
| Credential stronger than the grant | A credential mapped to a credential authority is requested for exactly the Action's permissions and resource, and refused if the authority's evidence shows anything wider, another principal, Action, call or Grant, too long a life, or a reused reference. Assurance (`unverified`, `declared`, `verified`, `bound`) is recorded per call; a minimum set globally, per mapping or by policy rule is never lowered, applies to MCP calls too, and is checked again before each retry along with expiry and revocation | now, for native tools with a mapped credential | Static `env:` credentials and MCP server credentials are unverified or declared: they carry whatever authority the secret has. Assurance is only as good as the trusted authority; Legion checks what it says, not what the downstream system does |
| Credential checked, then used later | Identity, expiry and revocation are checked immediately before dispatch and before every retry; an expired credential is replaced, a revoked or unconfirmable one refuses the call, and nothing is issued for a call that's out of budget, killed or waiting for approval | now | The check and the downstream use aren't atomic. A revocation or kill after Legion's last check isn't seen until the next one, and a call already started isn't undone. Closing that needs enforcement where the credential is used (gateway, broker, proof of possession) |
| Credential authority lying or failing | Evidence is schema-checked and bounded; missing or malformed evidence is unverified; untrusted authorities are declared at most; a trusted one's widening, substitution or overlong life is refused; errors and timeouts refuse the call and are recorded by type only; a refused credential's secret is still scrubbed | now | A trusted authority that issues exactly what's asked but lies about it isn't caught. Nothing is signed, so the channel to it is trusted too. Credential references are only checked for reuse within a run |
| Bad parameters, command injection | Schema validation, resource checks, no shell tool | now | Tools still have to treat arguments as hostile |
| Loops | Step and call budgets; identical call refused the 3rd time, run ended the 5th | now | Loops of slightly different calls only stop at the budget |
| Runaway spending | Token and cost budgets, cost limits need pricing configured, Legion is the only retrier and charges every attempt | now | Usage is only known after a call. Output is capped to the remaining tokens, input isn't, so one call can go over by the size of its prompt. Cost is checked after each call |
| Denial of service | Tool timeouts, wall-clock budget, bounded retries | now | A sync tool that ignores cancellation keeps its thread busy |
| Context poisoning | Nothing the model writes becomes config or authority | now | Poisoned text stays in the transcript for the rest of the task |
| Log tampering | Append-only triggers, hash chain, `legion verify` (which also checks run ids, the `seq` and `type` columns and duplicated keys); `inspect` refuses a broken chain | now | Tamper-evident, not tamper-proof. Anything that can write the store file (same OS user, including native tools and MCP stdio servers) can drop the triggers, rewrite a run's chain from the start, delete a whole run, or cut events off the end, and none of that is detected without an outside record of the last hash. Cutting a run back to just after an approval makes resume run that call again. External checkpoints that would catch this are designed (docs/nia-integration-requirements.md, section C), not built |
| Config that reads one way and loads another | Repeated keys and YAML aliases are refused; a policy rule whose `tool` or `capability` matches nothing is refused; non-finite numbers and bad paths are config errors | now | Listing `policy.rules` replaces the default approval rule; an operator who leaves it out gets no approvals |
| Schema references reaching out | Tool and agent schemas may only use `$ref` inside themselves; validation never retrieves anything, so a server-written schema can't make Legion fetch a URL or read a file | now | |
| Remote tool reading a resource differently | Resources are matched as strings, `..` segments never match, granted patterns can't contain `..` | now | A server that decodes or normalises (`legion%2F..%2Fx`, case) may act on something a glob grant didn't mean. Prefer exact resources for remote tools |
| Approval bypass or reuse | Approval bound to a hash of one call (arguments, target, grant, agent, tool spec, settings, credential refs), consumed before the tool runs, expires, re-checked when the call runs, including after a restart. A credential refused after approval doesn't give the approval back, and a new call needs its own | now | A phished approver approves the real thing |
| Spoofed approval screen | Everything printed from a run has markup escaped and control/bidi characters replaced | now | The approver still has to read it |
| Forged approval | Approvals are events in the log; the projection rejects impossible state changes; configs that let a tool setting reach the state directory, `legion.yaml`, a tool module or the agents directory are refused, even before those paths exist | now | Anyone who can write the store (same OS user, native tools, MCP stdio servers, or a tool given paths there by its own logic) can append a valid approval. Approvers aren't authenticated; the contract for authenticated approvals is designed (docs/nia-integration-requirements.md, section B), not built |
| Duplicate side effects after a crash or restart | Recorded results are reused, never re-run. Interrupted `write`/`external_irreversible` calls are in doubt and wait for an operator. One process per run via file locks | now | Only on one machine. A tool that raises after doing its work (instead of raising `ActionInDoubt`) looks like a clean failure |
| Budget reset by restart | Budget use is rebuilt from the log; crashed stretches are charged on resume | now | Time a tool spent hanging before a crash and tokens of a response lost to a crash aren't charged. A child's wall time is only charged when it ends, so a crash or pause gives a child its full time again (the root's time limit still holds) |
| Resume skipping checks | Resume uses the same loop and pipeline; the recorded grant has to still be grantable and the model binding unchanged | now | |
| Kill switch ignored | Kill state checked before every model call and tool run, again just before dispatch, and after a resume. With NIA configured (ADR 0020) the state comes from NIA, and anything NIA can't confirm stops the run | now | A tool call already running finishes. A kill after the last check before dispatch isn't seen for that call |
| Identity authority wrong or unreachable | NIA's answer has to name the agent asked about, carry a confirmed live kill check and parse within 64 KiB; errors, redirects, timeouts and anything else fail closed; refs come only from the operator's mapping; the token goes only to the configured endpoint and is scrubbed; compressed answers are refused | now, with NIA | Legion trusts what NIA says. A compromised NIA, or anyone who can change the traffic, decides identity and kill state; nothing NIA sends is signed. Plain http is allowed only to a literal loopback address, and even that trusts every local process that can take the port |

## Out of reach

- A model manipulated into misusing access it legitimately has.
- Malicious tool code.
- Escaping the machine. Legion has no sandbox; run it in one if the tools touch anything valuable.
- A compromised host, including rewriting the log.
- A compromised or careless approver.
- Leaking data through something the grant allows, like writing it to a file someone else reads.
- What any external agent runtime does, if one is ever added (ADR 0006).
- What an MCP server does with its own credentials, whether or not Legion calls it.
