# hello_files

The project `legion init` creates, committed so it can be read without running anything. A test
keeps it identical to `src/legion/templates/project`.

```bash
cd examples/hello_files
uv run legion run agents/assistant.yaml "Summarize the notes"
uv run legion inspect <run-id>
```

The scripted model reads `notes/meeting.md`, follows an instruction planted in it to read
`private/salaries.md`, and is refused with `capability_denied` because the agent's grant covers
`files.read:notes/**` only. It then writes `workspace/out/summary.md`.

Nothing here is security-specific. The same runtime runs any agent whose tools declare their
effects and capabilities.
