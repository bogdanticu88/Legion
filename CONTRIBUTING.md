# Contributing

Thanks for looking. Legion is small on purpose; the most useful contributions make it clearer,
safer or easier to build on, not bigger. For anything beyond a fix, open an issue first so we can
agree on the shape.

## Setup

You need Python 3.12 or 3.13 (the versions CI tests) and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra mcp      # the dev environment, with the optional MCP SDK
uv run pytest            # about three minutes; no API key or network needed
```

Without `--extra mcp`, the MCP tests are skipped and `mypy` can't check the MCP adapter; CI runs
both ways.

## What CI checks, and how to run it locally

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy                       # --strict, configured in pyproject.toml
uv run pytest                     # unit, conformance, property, crash and hostile-service tests
uv build && scripts/wheel-smoke.sh dist/legion_runtime-*.whl   # the wheel runs the quickstart
```

Useful subsets:

```bash
uv run pytest tests/conformance   # the contracts every authority and the kernel must keep
uv run pytest -m demo -v          # the demonstrations in docs/demos.md
```

Security scans, run before a release and welcome in PRs that touch parsing, secrets or I/O:

```bash
uvx bandit -r src -q
uvx semgrep scan --config p/python --config p/secrets --metrics=off src examples
uv export --locked --all-extras --no-dev --no-emit-project --no-hashes \
  --format requirements-txt -o /tmp/req.txt && uvx pip-audit --strict -r /tmp/req.txt
trufflehog filesystem . --exclude-paths=<(printf '%s\n' '\.venv/' '\.git/')   # if installed
```

## Tests that need something outside the repository

Marked `integration` and skipped unless configured. You don't need any of them to contribute.

```bash
# a real model: any OpenAI-compatible server, or Anthropic
LEGION_TEST_OPENAI_COMPAT_URL=http://127.0.0.1:11434/v1 LEGION_TEST_OPENAI_COMPAT_MODEL=llama3.1 \
  uv run pytest -m integration tests/integration/test_real_models.py
ANTHROPIC_API_KEY=... LEGION_TEST_ANTHROPIC_MODEL=claude-sonnet-5-5 \
  uv run pytest -m integration tests/integration/test_real_models.py

# NIA, the optional identity and credential authority: build nia-api from a NIA checkout
go build -o /tmp/nia-api ./cmd/api          # in the NIA repository
LEGION_TEST_NIA_BIN=/tmp/nia-api uv run pytest -m integration \
  tests/integration/test_nia_real.py tests/integration/test_nia_attacks_real.py \
  tests/integration/test_nia_credentials_real.py
```

`tests/integration/test_nia_credentials_real.py` also has a test against NIA's full compose stack,
run only with `LEGION_TEST_NIA_STACK` set (see the test for the JSON it expects). Everything NIA
does is also covered offline by stand-ins (`tests/nia_lab.py`, `tests/nia_cred_lab.py`).

## Where things are

```
src/legion/
  domain/      Action, Grant, capabilities, agents, budgets, errors: plain data, no I/O
  kernel/      the runtime: agent loop, action pipeline, approvals, credentials, delegation
  ports/       the protocols external authorities implement (identity, credentials)
  adapters/    optional implementations of those ports (NIA); loaded only when configured
  authority/   policy rules and the budget ledger
  events/      event types, the SQLite store and hash chain, state rebuilt from events
  models/      model providers (scripted, OpenAI-compatible, Anthropic)
  tools/       the @tool decorator, the registry, the MCP adapter
  access/      secret references and API-key access
  config/      legion.yaml loading and the starter project
  cli.py       the `legion` command
tests/
  unit/        most tests; *_lab.py files are hostile stand-ins for services
  conformance/ invariants and contracts, including property tests
  integration/ real models and real NIA, skipped by default
examples/      runnable demos and a minimal credential authority
docs/          guides, the event format, ADRs
```

A tool call's whole path is in `kernel/pipeline.py`; start there to understand what Legion
enforces.

## Before a pull request

- The checks above pass.
- New behaviour comes with tests. If you touch `kernel/pipeline.py`, test each step you changed.
- If you add an event type or payload field, update `docs/events.md` in the same PR.
- If you change how runs pause, resume or recover, add a test that crashes the run at the point
  you touched (`tests.support.crash_at`) and resumes it.
- Bigger design changes get an ADR in `docs/adr/`.

## Rules for the codebase

- Tools only run through `ActionPipeline`. Don't add another path, and don't add hooks that skip a
  step.
- Grants only get narrower. Only operator config or an external authority can widen one.
- No domain-specific code in `src/legion`. That goes in `examples/` or your own application.
- Nothing in `domain`, `kernel`, `ports` or `events` imports an adapter or names a specific
  external service; `tests/unit/test_nia_optional.py` checks this.
- Adapters don't retry. Legion does.
- Never make Legion run a `write` or `external_irreversible` call again on its own after it may
  have started. That's what `legion reconcile` is for.
- Secrets stay references until they're used and never end up in events, artifacts, errors or
  the prompt.

## Commits

A short imperative subject (`Refuse credentials bound to another call`), and a body saying why
when that isn't obvious. One change per commit where you can.
