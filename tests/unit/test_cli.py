import filecmp
import json
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from legion.cli import app
from legion.config.loader import load_agent, load_config
from legion.domain.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "legion" / "templates" / "project"
EXAMPLE = ROOT / "examples" / "hello_files"

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path


def cli(project: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--config", str(project / "legion.yaml")])
    return result.exit_code, result.output


def run_id_from(output: str) -> str:
    match = re.search(r"run_[0-9a-f]{16}", output)
    assert match, output
    return match.group(0)


def test_example_matches_the_init_template() -> None:
    for path in TEMPLATE.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            twin = EXAMPLE / path.relative_to(TEMPLATE)
            assert twin.is_file(), f"{twin} missing; regenerate with legion init"
            assert filecmp.cmp(path, twin, shallow=False), f"{twin} differs from the template"


def test_init_does_not_overwrite(project: Path) -> None:
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 1


def test_end_to_end(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    code, out = cli(project, "agent", "validate", "agents/assistant.yaml")
    assert code == 0, out

    code, out = cli(project, "run", "agents/assistant.yaml", "Summarize the notes", "--json")
    assert code == 0, out
    outcome = json.loads(out)
    assert outcome["status"] == "completed"
    summary = (project / "workspace" / "out" / "summary.md").read_text()
    assert "Launch moved" not in summary or "14 November" in summary
    assert "Salaries" not in summary

    run_id = outcome["run_id"]
    code, out = cli(project, "runs")
    assert run_id in out
    code, out = cli(project, "inspect", run_id)
    assert "capability_denied" in out and "private/salaries.md" in out
    code, out = cli(project, "inspect", run_id, "--json")
    lines = [json.loads(line) for line in out.splitlines()]
    assert [e["seq"] for e in lines] == list(range(1, len(lines) + 1))

    code, out = cli(project, "verify", run_id)
    assert code == 0 and "chain intact" in out

    conn = sqlite3.connect(project / ".legion" / "legion.db", isolation_level=None)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute(
        "UPDATE events SET body = replace(body, 'capability_denied', 'tool_completed') "
        "WHERE type = 'action.refused'"
    )
    conn.close()
    code, out = cli(project, "verify", run_id)
    assert code == 1 and "broken" in out


def test_providers_never_print_secrets(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-appear")
    code, out = cli(project, "providers")
    assert code == 0
    assert "(set)" in out and "(missing)" not in out
    assert "sk-should-not-appear" not in out.replace("\n", "")


def test_validate_reports_problems(project: Path) -> None:
    bad = project / "agents" / "bad.yaml"
    bad.write_text(
        "name: bad\ninstructions: x\ntools: [read_note]\ncapabilities: ['files.read:**']\n"
    )
    code, out = cli(project, "agent", "validate", str(bad))
    assert code == 1
    assert "not grantable" in out


def test_run_refuses_invalid_agent(project: Path) -> None:
    bad = project / "agents" / "bad.yaml"
    bad.write_text("name: Bad Name\ninstructions: x\n")
    code, out = cli(project, "run", str(bad), "x")
    assert code == 2
    assert "invalid" in out


def test_config_accepts_approval_settings(tmp_path: Path) -> None:
    text = (TEMPLATE / "legion.yaml").read_text().replace("ttl_seconds: 3600", "ttl_seconds: 600")
    (tmp_path / "legion.yaml").write_text(text)
    assert load_config(tmp_path / "legion.yaml").config.approvals.ttl_seconds == 600


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "legion.yaml").write_text("version: 1\nproviders: {}\nmodels: []\nsurprise: 1\n")
    with pytest.raises(ConfigError, match="surprise"):
        load_config(tmp_path / "legion.yaml")


def test_mcp_servers_without_the_sdk_fail_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "mcp" else real(name, *a)
    )
    (tmp_path / "legion.yaml").write_text(
        "version: 1\nproviders: {}\nmodels: []\n"
        "mcp_servers:\n  x:\n    transport: stdio\n    command: [x]\n"
    )
    with pytest.raises(ConfigError, match="uv sync --extra mcp"):
        load_config(tmp_path / "legion.yaml")


def test_config_requires_secret_references(tmp_path: Path) -> None:
    (tmp_path / "legion.yaml").write_text(
        "version: 1\nmodels: []\nproviders:\n  a:\n    kind: anthropic\n"
        "    access: {kind: api_key, secret: sk-literal-key}\n"
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path / "legion.yaml")


def test_agent_yaml_maps_to_the_spec() -> None:
    spec = load_agent(TEMPLATE / "agents" / "assistant.yaml")
    assert spec.name == "notes-assistant"
    assert [str(c) for c in spec.capabilities] == ["files.read:notes/**", "files.write:out/**"]
    assert spec.budget.steps == 10


def test_symlink_escape_refused(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    link = project / "workspace" / "notes" / "meeting.md"
    link.unlink()
    link.symlink_to(project / "workspace" / "private" / "salaries.md")
    _, out = cli(project, "run", "agents/assistant.yaml", "x", "--json")
    run_id = json.loads(out)["run_id"]
    _, events = cli(project, "inspect", run_id, "--json")
    failed = [json.loads(line) for line in events.splitlines() if '"tool.failed"' in line]
    assert any("goes through a link" in e["payload"]["message"] for e in failed)
    assert "outside the agent's grant" not in events


def test_tools_cannot_be_pointed_at_the_state_directory(project: Path) -> None:
    config = project / "legion.yaml"
    config.write_text(config.read_text().replace("workspace: workspace", "workspace: ."))
    code, out = cli(project, "agent", "validate", str(project / "agents" / "assistant.yaml"))
    assert code == 2
    assert "state directory" in out
