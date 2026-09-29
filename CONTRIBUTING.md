# Contributing

## Setup

```bash
uv sync
uv run pytest
```

No API key is needed. Tests use the scripted provider and recorded HTTP exchanges. Tests that call
a real model are marked `integration` and are skipped unless their endpoint is configured:

```bash
LEGION_TEST_OPENAI_COMPAT_URL=http://localhost:11434/v1 LEGION_TEST_OPENAI_COMPAT_MODEL=llama3.1 \
  uv run pytest -m integration
ANTHROPIC_API_KEY=... LEGION_TEST_ANTHROPIC_MODEL=claude-sonnet-5-5 uv run pytest -m integration
```

## Before opening a pull request

- `uv run ruff check src tests`, `uv run ruff format --check src tests` and `uv run mypy` pass.
- `uv run pytest` passes, including `tests/conformance`.
- New behaviour has tests. A change to `kernel/pipeline.py` needs a test for every step it
  touches.
- Documentation describes what the code does. A new event type or payload field updates
  `docs/events.md` in the same pull request.

## Ground rules

- Every effect goes through `ActionPipeline`. Do not add a path to a tool that skips it, and do not
  add hooks that let a step be skipped.
- Authority only narrows. Nothing may widen a `Grant` except the operator configuration or an
  external grant authority.
- No domain concepts in `src/legion`. Security, coding or business logic belongs in `examples/` or
  in an application.
- Adapters do not retry. Legion retries, once, in one place.
- Secrets are references until the moment they are used, and never appear in events, artifacts or
  model context.
- Architectural changes get an ADR in `docs/adr/`.

## Commit messages

Imperative subject under 72 characters, body explaining why when it is not obvious.
