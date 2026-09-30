# NIA is an optional adapter. Legion's core doesn't import it, doesn't name it, doesn't depend on
# it, and runs without any NIA process, configuration or environment. Adding or removing NIA is
# a legion.yaml change and nothing else.

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from legion.config.loader import load_config
from legion.config.templates import write_project
from legion.domain.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "legion"
CORE = ["kernel", "domain", "ports", "events", "authority", "tools", "models", "access"]


def test_importing_legion_loads_no_adapter() -> None:
    code = (
        "import sys\n"
        "import legion, legion.cli, legion.config.loader, legion.kernel.runtime,"
        " legion.kernel.pipeline, legion.kernel.credentials, legion.ports.credentials,"
        " legion.ports.identity, legion.events.projections\n"
        "bad = sorted(m for m in sys.modules if m.startswith('legion.adapters'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert out.stdout.strip() == ""


def test_core_code_does_not_name_nia() -> None:
    word = re.compile(r"\bnia\b", re.IGNORECASE)
    found = []
    for area in CORE:
        for path in (SRC / area).rglob("*.py"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if word.search(line):
                    found.append(f"{path.relative_to(SRC)}:{n}")
    assert found == []


def test_core_never_imports_the_adapters() -> None:
    found = []
    for area in CORE:
        for path in (SRC / area).rglob("*.py"):
            if re.search(r"^\s*(from|import)\s+legion\.adapters", path.read_text(), re.M):
                found.append(str(path.relative_to(SRC)))
    assert found == []


def test_no_nia_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = [*project["dependencies"]]
    for extra in project.get("optional-dependencies", {}).values():
        deps += extra
    assert not [d for d in deps if "nia" in d.lower()]


def test_legion_runs_with_no_nia_anywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from legion.cli import app

    for key in list(os.environ):
        if "NIA" in key:
            monkeypatch.delenv(key)
    write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "agents/assistant.yaml", "go", "--json"])
    assert '"status": "completed"' in result.output, result.output


def _with_nia(tmp_path: Path, **authority: Any) -> Path:
    write_project(tmp_path)
    path = tmp_path / "legion.yaml"
    config = yaml.safe_load(path.read_text())
    config["credential_authorities"] = {
        "nia": {
            "provider": "nia",
            "endpoint": "https://nia.internal:8080",
            "credential": "env:LEGION_NIA_ISSUER",
            "audience": "nia-gateway",
            "trusted": True,
            "agents": {"notes-assistant": "agent:notes"},
            **authority,
        }
    }
    config["credentials"] = {
        "github": {
            "authority": "nia",
            "provider": "nia-gateway",
            "permissions": {"repo.read": ["repo.read"]},
        }
    }
    path.write_text(yaml.safe_dump(config))
    return path


def test_adding_and_removing_nia_is_config_only(tmp_path: Path) -> None:
    with_nia = load_config(_with_nia(tmp_path / "a"))
    assert "nia" in with_nia.config.credential_authorities
    write_project(tmp_path / "b")
    without = load_config(tmp_path / "b" / "legion.yaml")
    assert without.config.credential_authorities == {}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"endpoint": "http://nia.internal:8080"}, "https"),
        ({"endpoint": "http://localhost:8080"}, "127.0.0.1"),
        ({"endpoint": "https://nia.internal:8080/?x=1"}, "query"),
        ({"credential": "plain-token"}, "env:NAME"),
        ({"audience": "*"}, "1-128"),
        ({"agents": {}}, "empty"),
        ({"agents": {"notes-assistant": "agent/../x"}}, "NIA agent refs"),
        ({"provider": "vault"}, "unknown credential authority provider"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_nia_authority_config_is_checked(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(_with_nia(tmp_path, **change))


def test_issuer_token_must_differ_from_the_identity_token(tmp_path: Path) -> None:
    path = _with_nia(tmp_path, credential="env:SAME")
    config = yaml.safe_load(path.read_text())
    config["identity"] = {
        "provider": "nia",
        "endpoint": "https://nia.internal:8080",
        "credential": "env:SAME",
        "agents": {"notes-assistant": "agent:notes"},
    }
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ConfigError, match="issuer role"):
        load_config(path)


def test_mappings_must_agree(tmp_path: Path) -> None:
    path = _with_nia(tmp_path)
    config = yaml.safe_load(path.read_text())
    config["identity"] = {
        "provider": "nia",
        "endpoint": "https://nia.internal:8080",
        "credential": "env:VIEWER",
        "agents": {"notes-assistant": "agent:other"},
    }
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ConfigError, match="differently"):
        load_config(path)


def test_mapping_can_come_from_the_identity_block(tmp_path: Path) -> None:
    path = _with_nia(tmp_path)
    config = yaml.safe_load(path.read_text())
    del config["credential_authorities"]["nia"]["agents"]
    config["identity"] = {
        "provider": "nia",
        "endpoint": "https://nia.internal:8080",
        "credential": "env:VIEWER",
        "agents": {"notes-assistant": "agent:notes"},
    }
    path.write_text(yaml.safe_dump(config))
    load_config(path)
    del config["identity"]
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ConfigError, match="needs `agents:`"):
        load_config(path)


async def test_built_authority_is_scrubbed_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGION_NIA_ISSUER", "issuer-CANARY-0f1e2d3c4b5a")
    loaded = load_config(_with_nia(tmp_path))
    legion = await loaded.build()
    # the issuer token is one of the secrets scrubbed from everything Legion records
    assert "env:LEGION_NIA_ISSUER" in [str(r) for r in legion.secret_refs]
    legion.store.close()  # type: ignore[attr-defined]
    await loaded.aclose()
    assert loaded.built_authorities == []
