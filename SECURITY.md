# Security policy

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" button on the
Security tab rather than in a public issue. Include the commit, what you found and how to
reproduce it. I'll reply within 5 working days, and I'll credit you in the changelog unless you'd
rather I didn't.

## In scope

- Model or tool output causing an effect that skips part of the action pipeline
- A task acting outside its grant, or widening a grant
- Spending past a grant's budget
- Secret values ending up in the prompt, events, artifacts or logs
- Changes to the event log that `legion verify` misses (other than rewriting the whole chain from
  the host)
- Reusing an approval, or one approval being accepted for a different action (from Phase 2)

## Out of scope

- A manipulated model misusing access its grant legitimately gives it. That's the documented
  limit (see THREAT_MODEL.md).
- Malicious native tools. They're trusted code.
- Anything that needs control of the host.

## Supported versions

Until 1.0, only the latest commit on `main`.
