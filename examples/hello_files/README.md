# hello_files

This is what `legion init` creates. It's committed so you can read it without running anything,
and a test keeps it in sync with `src/legion/templates/project`.

```bash
cd examples/hello_files
uv run legion run agents/assistant.yaml "Summarize the notes"
uv run legion inspect <run-id>
```

The scripted model reads `notes/meeting.md`, which tells it to also read `private/salaries.md`. It
tries, and gets `capability_denied` because the agent only has `files.read:notes/**`. Then it
writes `workspace/out/summary.md`.

`agents/publisher.yaml` shows approvals: publishing can't be undone, so the run pauses until you
`legion approve` it, then `legion resume` finishes it.

There's nothing security-specific in here. Any agent works the same way as long as its tools
declare their effects and capabilities.
