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
| `legion.yaml`, tool modules, policy | yes | Written by the operator. Can grant anything. |
| Legion and native Python tools | yes | Same process. A malicious tool can do anything Python can. |
| Model output | no | Shaped by everything the model read, including attacker text. |
| Tool output | no | Can contain injected instructions. |
| MCP server descriptions and results (Phase 5) | no | They end up in the prompt and can change. |
| Human approver (Phase 2) | mostly | Can be rushed or phished. |
| The host | yes | Whoever owns it owns the process and the log. |

The main assumption: the model can be manipulated at any point. Legion doesn't try to notice. It
limits what the model can make happen.

## Threats

| Threat | What Legion does | When | What's left |
|---|---|---|---|
| Prompt injection, direct or through tool output | Every action is checked against the grant and policy whatever the model wants. Tool output goes back to the model as data, never to the harness as instructions | now | Anything the grant allows can still happen. There's no tracking of which input influenced which decision |
| Malicious native tool | nothing | - | Tools are trusted code. Review them |
| Malicious MCP server, tool poisoning, rug pulls | Operator manifest, pinned description hash, unlisted tools refused | Phase 5 | A pinned server can still return hostile results |
| Poisoned tool output | Output schema checks, size limit, secret redaction | now | Schemas don't catch meaning |
| Credential theft | Config holds references only. Secrets never go into model context. Resolved secret values are scrubbed from every event and from tool output | now | A tool that encodes or splits a secret gets past the scrubbing. Secrets under 4 characters aren't scrubbed |
| Secrets in logs | `Secret` won't print its value. Tests check the database file for a planted secret | now | A secret the model passes as a plain argument isn't recognised as one |
| Privilege escalation | Root grant limited by `grantable`, tool must be given to the agent, capability checked on name and resource, `..` never matches | now | Broad capabilities like `files.*` give broad access |
| Escalation through delegation | `Grant.attenuate` refuses anything wider than the parent | function now, delegation Phase 3 | Two tasks under the same grant can still cooperate |
| Agents attacking each other, impersonation | Child results come back through the harness; a task's identity comes from the run, not from messages | Phase 3 | Content passed between agents can carry injections |
| Cross-tenant access | Not multi-tenant | not built | Don't run tenants that distrust each other in one process |
| Confused deputy | Tools only get credentials the operator bound to them | now | A tool with broad credentials that takes a target from the model is still a deputy. Limit it with resource capabilities |
| Bad parameters, command injection | Schema validation, resource checks, no shell tool | now | Tools still have to treat arguments as hostile |
| Loops | Step and call budgets; identical call refused the 3rd time, run ended the 5th | now | Loops of slightly different calls only stop at the budget |
| Runaway spending | Token and cost budgets, cost limits need pricing configured, Legion is the only retrier and charges every attempt | now | Usage is only known after a call. Output is capped to the remaining tokens, input isn't, so one call can go over by the size of its prompt. Cost is checked after each call |
| Denial of service | Tool timeouts, wall-clock budget, bounded retries | now | A sync tool that ignores cancellation keeps its thread busy |
| Context poisoning | Nothing the model writes becomes config or authority | now | Poisoned text stays in the transcript for the rest of the task |
| Log tampering | Append-only triggers, hash chain, `legion verify` | now | The host owner can rewrite the whole chain, and events cut off the end aren't detected without an outside record of the last hash |
| Approval bypass | Approval tied to the action hash, single use, expires | Phase 2 | A phished approver approves the real thing |
| Kill switch ignored | Kill state checked before every model call and tool run | interface now, NIA adapter Phase 7 | A tool call already running finishes |

## Out of reach

- A model manipulated into misusing access it legitimately has.
- Malicious tool code.
- Escaping the machine. Legion has no sandbox; run it in one if the tools touch anything valuable.
- A compromised host, including rewriting the log.
- A compromised or careless approver.
- Leaking data through something the grant allows, like writing it to a file someone else reads.
- What external runtimes (Phase 4) do beyond their declared guarantees.
