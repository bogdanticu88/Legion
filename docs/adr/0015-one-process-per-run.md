# ADR 0015: One process drives a run at a time

Status: accepted

## Context

If two processes resumed the same run, both would execute its open calls. The event log's
`expected_seq` check would reject the second writer's next append, but by then its tool may
already have run.

## Decision

A run is driven under an OS file lock (`flock`, or `msvcrt.locking` on Windows) in a `locks`
directory next to the SQLite store. `run`, `resume`, `approve`, `deny` and `reconcile` all take
it. If the process dies, the OS releases the lock, so a crashed run can be resumed. The in-memory
store uses an in-process lock.

## Consequences

- Single machine only. Multiple hosts sharing a store would need leases, which is out of scope.
- Approving a run that is currently executing is refused; approvals only land on paused runs.
