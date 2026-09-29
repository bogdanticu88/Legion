# ADR 0013: Resume works from the event log, and uncertain writes wait for a person

Status: accepted

## Context

A run can stop anywhere: before a model call, halfway through one, after a response but before
it's recorded, in the middle of a tool, after a tool did something but before Legion wrote that
down. Resuming has to pick the right thing to do in each case without trusting memory or the
model to remember what happened, and without ever repeating something that may already have
happened.

## Decision

`legion resume` rebuilds the run from the log (after verifying the chain), re-checks the recorded
agent and grant against today's configuration, and then goes through the same loop as a fresh
run. The loop's first step, `_settle`, finishes whatever the last model turn left open, both after
a fresh response and after a resume, so there is one code path.

What happens to each kind of interruption:

| Where it stopped | What resume does |
|---|---|
| before or during a model call | calls the model again; the lost call stays charged |
| after a response, before it was recorded | same as above; the response is gone |
| after a response was recorded, before its tools ran | runs the tools through the full pipeline |
| a tool had been proposed but not started | runs it through the full pipeline |
| a `pure`, `read` or `write_idempotent` tool was running | records `action.interrupted`, runs it again through the pipeline with the same idempotency key |
| a `write` or `external_irreversible` tool was running | records `action.in_doubt` and pauses. It is never run again automatically |
| the task finished but the run wasn't closed | closes the run |
| waiting for approval | stays paused until someone decides |

An in-doubt action waits for `legion reconcile <run> <call> --outcome applied|not-applied|abandon`.
`applied` and `not-applied` tell the model what really happened; `abandon` fails the run. After
`not-applied` the model can propose the action again, and that goes through the pipeline (and
approval, if policy asks for it) as a new action.

The same pause happens during a live run when a write times out or a tool raises `ActionInDoubt`.

Budget use comes from the log, so a restart can't reset it. Wall-clock time is recorded for every
active stretch; for a stretch that ended in a crash, the time up to the last recorded event is
charged when the run is resumed. Time spent paused doesn't count.

## Consequences

- Legion does not claim exactly-once execution. It claims something narrower: from an intact
  log, it never automatically repeats a `write` or `external_irreversible` call that may already
  have taken effect. Calls declared `pure`, `read` or `write_idempotent` are run again, so a
  wrong effect class breaks this, and so does a log cut back by someone who can write the store.
- A response lost to a crash wasn't charged for tokens, and a tool that hung before a crash
  wasn't charged for the time it hung. Both are small and documented.
- Some work gets charged twice (an interrupted model call, the step it was in). That errs toward
  stopping early rather than overspending.
