# jsonschema follows a $ref it doesn't know by fetching it, over http or from a file. Legion's
# schemas are partly written by MCP servers, so nothing may be retrieved.

from pathlib import Path

import pytest
import yaml

from legion import schemas
from legion.config.loader import load_agent
from legion.config.templates import write_project
from legion.domain.errors import ConfigError


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write_project(tmp_path)
    return tmp_path


def test_schema_with_a_remote_ref_is_refused() -> None:
    with pytest.raises(ValueError, match="outside itself"):
        schemas.check({"type": "object", "properties": {"x": {"$ref": "http://10.0.0.1/s.json"}}})
    schemas.check(
        {
            "type": "object",
            "$defs": {"a": {"type": "string"}},
            "properties": {"x": {"$ref": "#/$defs/a"}},
        }
    )


def test_validation_does_not_read_files(tmp_path: Path) -> None:
    leaked = tmp_path / "secret.json"
    leaked.write_text('{"type": "string", "description": "FILE-CONTENT"}')
    schema = {"type": "object", "properties": {"x": {"$ref": leaked.as_uri()}}}
    with pytest.raises(Exception) as caught:
        schemas.validate({"x": 1}, schema)
    assert "FILE-CONTENT" not in str(caught.value)


def test_agent_output_schema_is_checked(project: Path) -> None:
    path = project / "agents" / "assistant.yaml"
    spec = yaml.safe_load(path.read_text())
    spec["output_schema"] = {"type": "object", "properties": {"a": {"$ref": "file:///etc/x"}}}
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(ConfigError, match="outside itself"):
        load_agent(path)


async def test_mcp_tool_whose_schema_points_outside_is_blocked() -> None:
    pytest.importorskip("mcp")
    from tests.mcp_lab import Lab, connect, pinned, server_config

    lab = Lab()
    lab.advertised["read_note"] = {
        "input_schema": {
            "type": "object",
            "properties": {"path": {"$ref": "http://169.254.169.254/latest/meta-data"}},
        }
    }
    config = await pinned(lab, server_config(read_note={"effect": "read", "resource_arg": "path"}))
    conn, found = await connect(lab, config)
    assert found.tools == []
    assert "outside itself" in found.blocked["read_note"]
    await conn.aclose()
