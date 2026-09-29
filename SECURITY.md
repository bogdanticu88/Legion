# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting ("Report a
vulnerability" on the repository's Security tab). Please do not open a public issue.

Include the version or commit, a description, and steps to reproduce. You should receive an
acknowledgement within 5 working days. Fixes are credited in the changelog unless you ask
otherwise.

## In scope

- Any way for model output or tool output to cause an effect that skips a step of the action
  pipeline
- Any way for a task to act with authority its grant does not cover, or to widen a grant
- Budget bypass: spending beyond a grant's limits
- Secret values reaching model context, events, artifacts or logs
- Event log modification that `legion verify` does not detect, other than rewriting the whole
  chain from the host
- Approval reuse or approval of one action being accepted for another (from Phase 2)

## Out of scope

- Misuse of authority a grant legitimately holds after the model has been manipulated. This is
  the documented limit of the design (THREAT_MODEL.md).
- Behaviour of malicious native tools. They are trusted code.
- Anything requiring control of the host.

## Supported versions

Pre-1.0: only the latest commit on `main` receives fixes.
