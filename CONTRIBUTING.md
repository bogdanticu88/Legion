# Contributing

## Setup

```bash
uv sync
uv run pytest
```

You don't need an API key. The tests use the scripted provider and recorded HTTP responses. The
tests that call a real model are marked `integration` and skip unless you point them somewhere:

```bash
LEGION_TEST_OPENAI_COMPAT_URL=http://localhost:11434/v1 LEGION_TEST_OPENAI_COMPAT_MODEL=llama3.1 \
  uv run pytest -m integration
ANTHROPIC_API_KEY=... LEGION_TEST_ANTHROPIC_MODEL=claude-sonnet-5-5 uv run pytest -m integration
```

## Before a pull request

- `uv run ruff check src tests`, `uv run ruff format --check src tests` and `uv run mypy` pass.
- `uv run pytest` passes, including `tests/conformance`.
- New behaviour comes with tests. If you touch `kernel/pipeline.py`, test each step you changed.
- If you add an event type or payload field, update `docs/events.md` in the same PR.
- If you change how runs pause, resume or recover, add a test that crashes the run at the point
  you touched (`tests.support.crash_at`) and resumes it.

## Rules for the codebase

- Tools only run through `ActionPipeline`. Don't add another path, and don't add hooks that skip a
  step.
- Grants only get narrower. Only operator config or an external authority can widen one.
- No domain-specific code in `src/legion`. That goes in `examples/` or your own application.
- Adapters don't retry. Legion does.
- Never make Legion run a `write` or `external_irreversible` call again on its own after it may
  have started. That's what `legion reconcile` is for.
- Secrets stay references until they're used and never end up in events, artifacts or the prompt.
- Bigger design changes get an ADR in `docs/adr/`.

## Commits

Short imperative subject, and a body when the reason isn't obvious.
