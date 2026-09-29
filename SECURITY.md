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
- Changes to the event log that `legion verify` misses, other than the known ones: cutting events
  off the end, deleting a whole run, and rewriting or appending to a chain by someone who can
  write the store (see docs/events.md)
- A config file that loads differently from how it reads
- Reusing an approval, or one approval being accepted for a different action
- Resume or restart running something twice that may already have taken effect, resetting a
  budget, or skipping a check
- Making the approval screen show something other than what would run

## Out of scope

- A manipulated model misusing access its grant legitimately gives it. That's the documented
  limit (see THREAT_MODEL.md).
- Malicious native tools. They're trusted code.
- Anything that needs control of the host.

## Supported versions

Until 1.0, only the latest commit on `main`.
