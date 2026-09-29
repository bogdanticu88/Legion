# Threat model

This covers the Legion runtime. It states what the harness defends against, what it only limits,
and what it cannot address. The status column says whether the mitigation exists today
(Phase 1) or is planned.

## Assets

- Credentials: model API keys and tool credentials.
- Authority: what a run is allowed to do, expressed as grants.
- Budget: money and compute a run may spend.
- Integrity of the record: the event log that says what happened.
- Data that tools read, and the systems that tools change.

## Trust boundaries

| Component | Trust | Why |
|---|---|---|
| Operator configuration (`legion.yaml`, tool modules, policy) | Trusted | Written by whoever runs Legion. It can grant anything. |
| Legion code and native Python tools | Trusted | They run in the same process. A malicious native tool can do anything Python can. |
| Model output | Untrusted | It is shaped by every input the model saw, including attacker-controlled text. |
| Tool output | Untrusted | It may contain injected instructions or data from hostile sources. |
| MCP server metadata and results (Phase 5) | Untrusted | Descriptions and schemas are prompt input and can change. |
| Human approver (Phase 2) | Trusted but fallible | Can be rushed, tricked or phished. |
| Host and its storage | Trusted | Whoever owns the host owns the process and the log. |

The central assumption: **the model may be fully manipulated at any time.** Legion does not try
to detect that. It limits what a manipulated model can cause.

## Threats

| Threat | Mitigation | Status | Residual risk |
|---|---|---|---|
| Direct prompt injection | Actions are bounded by the grant and policy regardless of what the model wants | Phase 1 | Anything the grant allows can still be triggered |
| Indirect prompt injection via tool output | Same bound. Tool output is returned as a tool result, never as instructions to the harness | Phase 1 | Legion does not track which data influenced which decision. Data-flow tagging (CaMeL style) is future work |
| Malicious or compromised native tool | None inside the process. Declared effect class and capabilities are documentation, not a sandbox | Accepted | Native tools are part of the trusted computing base. Review them like any other code |
| Malicious MCP server, tool poisoning, rug pull | Operator manifest, pinned description hash, unmanifested tools refused, descriptions never trusted | Phase 5 | A pinned server can still return hostile results |
| Tool output poisoning | Output schema validation where declared, size limit, redaction of known secret values | Phase 1 | Semantic poisoning passes any schema |
| Credential theft by the model | Secrets are references in config, resolved per request or per tool call, never placed in model context or events. Every resolved secret value, and its JSON-escaped form, is redacted from every event payload and from tool output before the model sees it | Phase 1 | A tool that transforms a secret (encodes, splits, hashes) defeats the scan. Secrets shorter than 4 characters are not redacted |
| Secret leakage into logs | `Secret` never prints its value; events carry references only; tests grep the stored log for a sentinel | Phase 1 | Secrets that arrive as plain tool arguments from the model are not recognized as secrets |
| Privilege escalation | Root grant must be covered by operator `grantable`; tools must be offered to the agent; capability match on name and resource; `..` in resources never matches | Phase 1 | Coarse capabilities (`files.*`) give coarse protection |
| Delegation-based escalation | `Grant.attenuate` refuses any widening of capabilities, limits or expiry; carving budgets from the parent's remaining ledger comes with delegation | Function in Phase 1, delegation in Phase 3 | Collusion between tasks within the same grant is not prevented |
| Cross-agent attacks, sub-agent impersonation | Children receive results through the harness only; identity of each task comes from the run, not from messages | Phase 3 | Content passed between agents can carry injections |
| Cross-tenant access | Not multi-tenant. Design rule: tenant is part of every storage key and every credential lookup | Not built | Do not run mutually distrustful tenants in one Legion process |
| Confused deputy | Tools act only with credentials the operator bound to them, never with a credential chosen by the model | Phase 1 | A tool that accepts a target from the model and has broad credentials is still a deputy. Constrain it with resource capabilities |
| Unsafe tool parameters, command injection | JSON Schema validation, resource extraction and capability matching per call; no shell tool | Phase 1 | Tools must still treat arguments as hostile input |
| Agent loops | Step, model-call and tool-call budgets; identical action refused on the third attempt and fatal on the fifth | Phase 1 | Loops of slightly different actions end only at the budget |
| Denial of wallet | Token and cost budgets per grant; cost budgets refuse to start without pricing; Legion is the only retrier and counts every attempt | Phase 1 | Usage is known only after a call. Output is capped by the remaining token budget, but input is not: one call can overshoot by the size of the prompt. Cost is checked only after each call, so one call can overshoot the cost limit by that call's price |
| Denial of service | Tool timeouts, wall-clock budget, bounded retries | Phase 1 | A native tool that ignores cancellation can hold a worker thread |
| Context poisoning | Transcript is a projection of events; nothing the model writes becomes configuration or authority | Phase 1 | Poisoned content stays in the transcript for the rest of the task |
| Event log manipulation | Append-only triggers, per-run hash chain, `legion verify` | Phase 1 | The host owner can rewrite the full chain, and removing events from the end of a run is not detectable without an external record of the head. External anchoring is the forensics project's job |
| Approval bypass | Approval bound to action hash, single use, expiry; config that needs approval is refused until then | Phase 2 | A phished approver approves the real hash |
| Kill or revocation ignored | Kill state checked before every model call and every tool execution through `IdentityPort` | Protocol in Phase 1, NIA adapter in Phase 7 | An in-flight tool call finishes |

## What Legion cannot defend against

- A model that has been manipulated into misusing authority it legitimately holds.
- Malicious native tool code.
- Escape from the host, because Legion provides no sandbox. Run it inside one when tools touch
  anything valuable.
- A compromised host, including rewriting of the event log.
- A compromised or careless approver.
- Exfiltration through channels a grant allows, for example writing sensitive text to an allowed
  file that someone else reads.
- Correct behaviour of external runtimes (Phase 4) beyond what their declared guarantee level says.
