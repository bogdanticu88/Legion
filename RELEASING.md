# Releasing

For the maintainer. Nothing here happens automatically: there is no release workflow, and a tag
or a PyPI upload only happens when the maintainer does it by hand.

## Before the first public release

These can't be done from the repository and aren't done yet:

- [ ] **Public repository URL.** Once the repository exists, add it to `pyproject.toml` in place
      of the comment `# [project.urls] goes here once the public repository exists`:

      ```toml
      [project.urls]
      Homepage = "<url>"
      Repository = "<url>"
      Issues = "<url>/issues"
      Changelog = "<url>/blob/main/CHANGELOG.md"
      ```

      The README's relative links (`docs/...`, `THREAT_MODEL.md`) don't resolve on a package
      index page; with the URL known, decide whether to make the important ones absolute.
- [ ] **Private vulnerability reporting** switched on (Settings, Code security), since
      SECURITY.md sends reporters to it. Decide whether to add an email fallback and a default
      disclosure window; SECURITY.md currently promises a reply within 5 working days and an
      agreed disclosure date.
- [ ] **A ruleset (or branch protection) on `main`:** pull requests required, force pushes and
      deletion blocked, and required status checks: `check (3.12)`, `check (3.13)`, `audit`,
      `base` and CodeQL's `analyze`.
- [ ] **Dependabot alerts**, **secret scanning** and **push protection** switched on.
- [ ] Labels used by the issue templates: `bug`, `proposal`; and `good first issue`.

## Release checklist

1. Clean tree on `main`, and the version in `src/legion/__init__.py` is the one being released
   (it's the only place the version is written).
2. `CHANGELOG.md` has a section for the version, with its known limitations.
3. Checks:

   ```bash
   uv sync --locked --extra mcp
   uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
   uv run pytest
   ```

4. Security scans (see CONTRIBUTING.md): Bandit, Semgrep, pip-audit, TruffleHog. Compare with the
   previous release; explain anything new before going on.
5. Integration tests where available: a real model, and NIA (`LEGION_TEST_NIA_BIN`).
6. Build and inspect:

   ```bash
   rm -rf dist && uv build
   unzip -l dist/*.whl                     # only legion/ and the dist-info
   tar tzf dist/*.tar.gz | less            # no .legion/, caches, secrets or scratch files
   uvx twine check dist/*
   scripts/wheel-smoke.sh dist/*.whl       # fresh venv: version, py.typed, quickstart, verify
   ```

7. Follow the README's quickstart in a fresh clone, as a stranger would, including the approval
   flow and `legion verify`.
8. Maintainer approval to release.
9. A signed tag, only after that approval: `git tag -s v0.1.0a1 -m "Legion 0.1.0a1"`. The tag is
   `v` plus the version exactly as in `__init__.py` (PEP 440, so `0.1.0a1`, not `0.1.0-alpha.1`).
10. Publishing to PyPI, only after approval, from the artifacts checked above. Trusted Publishing
    with attestations is the intended route once the repository is public.
