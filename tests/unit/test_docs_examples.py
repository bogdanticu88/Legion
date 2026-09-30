# The documentation's examples, run as written. docs/writing-tools.md is applied to a fresh starter
# project exactly as its blocks say, and the README's quickstart claims are checked against the
# CLI's real output.

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from legion.cli import app

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def file_blocks(doc: Path) -> dict[str, str]:
    # fenced blocks whose first line is "# file: <path>", keyed by that path
    blocks: dict[str, str] = {}
    for body in re.findall(r"```(?:python|yaml)\n(.*?)```", doc.read_text(), re.S):
        # blocks inside a list item are indented; dedent so YAML and Python parse
        first, _, rest = textwrap.dedent(body).partition("\n")
        match = re.match(r"# file: (\S+)", first)
        if match:
            blocks[match.group(1)] = rest
    return blocks


def merge(base: Any, extra: Any) -> Any:
    if isinstance(base, dict) and isinstance(extra, dict):
        return {k: merge(base.get(k), v) if k in base else v for k, v in {**base, **extra}.items()}
    if isinstance(base, list) and isinstance(extra, list):
        return base + [x for x in extra if x not in base]
    return extra


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_writing_tools_guide_runs_as_written(project: Path) -> None:
    blocks = file_blocks(ROOT / "docs" / "writing-tools.md")
    assert set(blocks) == {
        "todo_tools.py",
        "legion.yaml",
        "agents/todo.yaml",
        "scripts/todo.yaml",
    }, sorted(blocks)
    config = yaml.safe_load((project / "legion.yaml").read_text())
    (project / "legion.yaml").write_text(
        yaml.safe_dump(merge(config, yaml.safe_load(blocks.pop("legion.yaml"))))
    )
    for name, text in blocks.items():
        (project / name).write_text(text)

    result = runner.invoke(app, ["agent", "validate", "agents/todo.yaml"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["run", "agents/todo.yaml", "Note the renewal"])
    assert result.exit_code == 0, result.output
    first = result.output.splitlines()[0]
    assert first.endswith("completed; Legion refused 1 action (capability_denied)"), first
    assert json.loads((project / "todos" / "inbox.json").read_text()) == ["renew the certificate"]
    assert not (project / "todos" / "boss.json").exists()


def test_guide_needs_its_tools_list() -> None:
    # the registration the guide relies on: without TOOLS the module is refused, clearly
    from legion.domain.errors import ConfigError
    from legion.tools.registry import load_tool_module

    source = file_blocks(ROOT / "docs" / "writing-tools.md")["todo_tools.py"]
    assert "\nTOOLS = [show_todos, add_todo]\n" in source
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "todo_tools.py"
        path.write_text(source.replace("TOOLS = [show_todos, add_todo]", ""))
        with pytest.raises(ConfigError, match="must define TOOLS"):
            load_tool_module(path)


def test_readme_quickstart_output_matches_the_cli(project: Path) -> None:
    readme = (ROOT / "README.md").read_text()
    result = runner.invoke(app, ["run", "agents/assistant.yaml", "Summarize the notes"])
    first = result.output.splitlines()[0]
    claimed = "completed; Legion refused 1 action (capability_denied)"
    assert first.endswith(claimed) and claimed in readme


def test_default_quickstart_needs_no_nia(project: Path) -> None:
    # in a fresh interpreter, so modules other tests imported can't hide an import here
    import os
    import subprocess
    import sys

    script = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from legion.cli import app\n"
        "r = CliRunner().invoke(app, ['run', 'agents/assistant.yaml', 'Summarize the notes'])\n"
        "assert r.exit_code == 0, r.output\n"
        "print(sorted(m for m in sys.modules if m.startswith('legion.adapters')))\n"
    )
    env = {k: v for k, v in os.environ.items() if "NIA" not in k}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=project, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
    config = (project / "legion.yaml").read_text()
    assert "nia" not in config.lower()


def test_demo_count_matches_the_demos() -> None:
    import re as _re

    text = (ROOT / "docs" / "demos.md").read_text()
    words = {"five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
    claimed = _re.match(r"# Demos\n\n(\w+) demonstrations", text)
    assert claimed, "docs/demos.md should open with how many demonstrations there are"
    sections = _re.findall(r"^## ([A-Z])\. ", text, _re.M)
    rows = _re.findall(r"^\| ([A-Z]) \|", text, _re.M)
    assert words[claimed.group(1).lower()] == len(sections) == len(rows)
    marker = (ROOT / "pyproject.toml").read_text()
    assert not _re.search(r'"demo: the (five|six|seven|eight) ', marker)


@pytest.mark.parametrize(
    "stale",
    [
        "no production credential authority",
        "isn't used for credentials yet",
        "exports plus the NIA identity adapter",
        "git clone <repo>",
    ],
)
def test_current_docs_have_no_stale_claims(stale: str) -> None:
    current = ["README.md", "ARCHITECTURE.md", "THREAT_MODEL.md", "CONTRIBUTING.md"]
    current += [f"docs/{n}" for n in ("demos.md", "roadmap.md", "extending.md", "writing-tools.md")]
    for name in current:
        assert stale not in (ROOT / name).read_text(), f"{name} says {stale!r}"


def test_extending_guide_authority_runs_as_written(project: Path) -> None:
    guide = file_blocks(ROOT / "docs" / "writing-tools.md")
    config = yaml.safe_load((project / "legion.yaml").read_text())
    config = merge(config, yaml.safe_load(guide.pop("legion.yaml")))
    extra = file_blocks(ROOT / "docs" / "extending.md")
    config = merge(config, yaml.safe_load(extra["legion.yaml"]))
    (project / "legion.yaml").write_text(yaml.safe_dump(config))
    source = guide.pop("todo_tools.py").replace(
        '@tool(effect=EffectClass.WRITE, capabilities=["todos.write"], resource=lambda a: a.list)',
        '@tool(effect=EffectClass.WRITE, capabilities=["todos.write"], resource=lambda a: a.list, '
        'credentials=["todo_api"])',
    )
    assert 'credentials=["todo_api"]' in source
    (project / "todo_tools.py").write_text(source)
    for name, text in guide.items():
        (project / name).write_text(text)
    (project / "authorities.py").write_text((ROOT / "examples" / "authorities.py").read_text())

    result = runner.invoke(app, ["run", "agents/todo.yaml", "Note the renewal", "--json"])
    assert result.exit_code == 0, result.output
    run_id = json.loads(result.output)["run_id"]
    result = runner.invoke(app, ["credentials", run_id, "--json"])
    [row] = [json.loads(line) for line in result.output.splitlines()]
    assert row["decision"] == "used" and row["assurance"] == "bound"
    assert row["permissions"] == ["todo:append"] and row["resource"] == "inbox"
    assert row["authority"] == "local"


def test_no_internal_milestone_labels() -> None:
    # development was tracked in numbered milestones; those names mean nothing to a reader
    import subprocess

    files = subprocess.run(
        ["git", "ls-files", "*.md", "*.py", "*.yaml", "*.yml", "*.toml", "*.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    label = re.compile(r"\b[Pp]hase \d")
    found = [
        f"{name}:{i}"
        for name in files
        if (ROOT / name).is_file()
        for i, line in enumerate((ROOT / name).read_text().splitlines(), 1)
        if label.search(line)
    ]
    assert found == []
