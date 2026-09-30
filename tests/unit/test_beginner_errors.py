# The mistakes a new user makes first, and whether the error says what happened, why and what to
# do, without repeating anything that could be a secret.

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from legion.cli import app
from legion.domain.errors import ConfigError
from legion.tools.mcp import McpServerConfig

runner = CliRunner()
PASTED = "sk-live-CANARY-3f9a1c7e5b2d"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    monkeypatch.chdir(tmp_path)
    return tmp_path


def edit_config(project: Path, change: dict[str, object]) -> None:
    path = project / "legion.yaml"
    data = yaml.safe_load(path.read_text())
    data.update(change)
    path.write_text(yaml.safe_dump(data))


def test_broken_sibling_agent_file_says_why_it_matters(project: Path) -> None:
    (project / "agents" / "draft.yaml").write_text("name: draft\n")
    result = runner.invoke(app, ["run", "agents/assistant.yaml", "go"])
    assert result.exit_code == 2
    text = " ".join(result.output.split())
    assert "draft.yaml" in text and "instructions" in text
    assert "is loaded for each run" in text and "move it out of agents/" in text


def test_literal_credential_names_the_key_and_file_not_the_value(project: Path) -> None:
    edit_config(project, {"credentials": {"github": PASTED}})
    result = runner.invoke(app, ["run", "agents/assistant.yaml", "go"])
    text = " ".join(result.output.split())
    assert result.exit_code == 2
    assert "legion.yaml" in text and "credentials.github must be a secret reference" in text
    assert PASTED not in result.output


def test_literal_provider_key_names_the_key_not_the_value(project: Path) -> None:
    data = yaml.safe_load((project / "legion.yaml").read_text())
    data["providers"]["anthropic"]["access"]["secret"] = PASTED
    (project / "legion.yaml").write_text(yaml.safe_dump(data))
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 2
    assert "access.secret must be a secret reference" in " ".join(result.output.split())
    assert PASTED not in result.output


def test_literal_mcp_env_value_never_reaches_any_error_text() -> None:
    with pytest.raises(ConfigError) as info:
        McpServerConfig(transport="stdio", command=("x",), env={"TOKEN": PASTED})
    assert "env.TOKEN must be a secret reference" in info.value.message
    assert PASTED not in str(info.value) and PASTED not in repr(info.value)


def test_missing_api_key_says_which_provider_and_how_to_fix(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = yaml.safe_load((project / "legion.yaml").read_text())
    for binding in data["models"]:
        if binding["profile"] == "general/default":
            binding.update(provider="anthropic", model="claude-test")
    (project / "legion.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["run", "agents/assistant.yaml", "go"])
    text = " ".join(result.output.split())
    assert "model provider 'anthropic' needs env:ANTHROPIC_API_KEY" in text
    assert "Set ANTHROPIC_API_KEY in the environment" in text
    assert "legion providers" in text


def test_unknown_tool_lists_the_ones_that_exist(project: Path) -> None:
    spec = (project / "agents" / "assistant.yaml").read_text()
    (project / "typo.yaml").write_text(spec.replace("write_summary", "write_sumary"))
    result = runner.invoke(app, ["run", "typo.yaml", "go"])
    text = " ".join(result.output.split())
    assert "unknown tools: write_sumary" in text and "write_summary" in text


def test_missing_mcp_extra_mentions_pip_too(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from legion.tools import mcp

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "mcp" else real(name, *a)
    )
    with pytest.raises(ConfigError, match="legion-runtime\\[mcp\\]"):
        mcp.check_servers({"gh": McpServerConfig(transport="stdio", command=("x",))})
