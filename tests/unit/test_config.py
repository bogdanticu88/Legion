# legion.yaml and agent files are trusted, but they have to load the way they read. These cover
# configurations that used to load quietly into something other than what the file says.

from pathlib import Path

import pytest
import yaml

from legion.config.loader import load_agent, load_config
from legion.config.templates import write_project
from legion.domain.errors import ConfigError
from legion.tools.mcp import McpServerConfig, McpToolConfig, unpinned_package

PIN = "sha256:" + "0" * 64


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write_project(tmp_path)
    return tmp_path


def edit(project: Path, change: dict[str, object]) -> Path:
    path = project / "legion.yaml"
    config = yaml.safe_load(path.read_text())
    config.update(change)
    path.write_text(yaml.safe_dump(config))
    return path


def append(project: Path, text: str) -> Path:
    path = project / "legion.yaml"
    path.write_text(path.read_text() + text)
    return path


# how the file is read


def test_duplicate_key_is_refused(project: Path) -> None:
    # the reviewer sees require_approval; the old loader kept the second one
    path = project / "legion.yaml"
    text = path.read_text()
    marker = "reason: irreversible actions need a human to approve them"
    assert marker in text
    indent = text.split(marker)[0].rsplit("\n", 1)[1]
    path.write_text(text.replace(marker, f"{marker}\n{indent}decision: allow"))
    with pytest.raises(ConfigError, match="duplicate key 'decision'"):
        load_config(path)


def test_duplicate_top_level_key_is_refused(project: Path) -> None:
    with pytest.raises(ConfigError, match="duplicate key 'store'"):
        load_config(append(project, "store: elsewhere.db\n"))


def test_duplicate_key_in_agent_file_is_refused(project: Path) -> None:
    path = project / "agents" / "assistant.yaml"
    path.write_text(path.read_text() + "capabilities: ['files.read:**']\n")
    with pytest.raises(ConfigError, match="duplicate key 'capabilities'"):
        load_agent(path)


def test_aliases_are_refused(tmp_path: Path) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}, *a{i - 1}]" for i in range(1, 8)]
    path = tmp_path / "legion.yaml"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ConfigError, match="aliases"):
        load_config(path)


def test_python_tags_are_refused(tmp_path: Path) -> None:
    # the strict loader is a SafeLoader, so nothing gets constructed from a tag
    path = tmp_path / "legion.yaml"
    path.write_text("version: !!python/object/apply:os.getcwd []\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_huge_integer_is_a_config_error(project: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(append(project, f"extra_number: {'9' * 5000}\n"))


@pytest.mark.parametrize("value", [".inf", ".nan", "-.inf"])
def test_non_finite_numbers_are_refused(project: Path, value: str) -> None:
    with pytest.raises(ConfigError):
        load_config(append(project, f"retry:\n  max_delay: {value}\n"))


def test_non_finite_number_in_free_form_options_is_refused(project: Path) -> None:
    path = project / "legion.yaml"
    config = yaml.safe_load(path.read_text())
    config["models"][0]["options"] = {"anything": {"temperature": float("inf")}}
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ConfigError):
        load_config(path)


def test_yaml_errors_do_not_quote_the_line(project: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(append(project, "credentials: {github: [sk-QUOTED-SECRET\n"))
    assert "QUOTED" not in str(caught.value)


@pytest.mark.parametrize("store", ["", "  ", "a\x00b"])
def test_unusable_store_path_is_refused(project: Path, store: str) -> None:
    with pytest.raises(ConfigError, match="store"):
        load_config(edit(project, {"store": store}))


def test_trailing_newline_in_a_name_is_refused(project: Path) -> None:
    path = project / "agents" / "assistant.yaml"
    spec = yaml.safe_load(path.read_text())
    spec["name"] = "notes-assistant\n"
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(ConfigError, match="name"):
        load_agent(path)


# policy


async def test_rule_for_a_tool_that_does_not_exist_is_refused(project: Path) -> None:
    rules = [{"decision": "require_approval", "tool": "publish-summary"}]
    loaded = load_config(edit(project, {"policy": {"default": "allow", "rules": rules}}))
    with pytest.raises(ConfigError, match="no tool matches 'publish-summary'"):
        await loaded.build()
    await loaded.aclose()


async def test_rule_for_a_capability_nothing_needs_is_refused(project: Path) -> None:
    rules = [{"decision": "deny", "capability": "Notes.Publish"}]
    loaded = load_config(edit(project, {"policy": {"default": "allow", "rules": rules}}))
    with pytest.raises(ConfigError, match=r"capability matching 'Notes\.Publish'"):
        await loaded.build()
    await loaded.aclose()


async def test_rules_that_match_something_are_fine(project: Path) -> None:
    rules = [
        {"decision": "require_approval", "tool": "publish_*"},
        {"decision": "deny", "capability": "agent.delegate"},
    ]
    loaded = load_config(edit(project, {"policy": {"default": "allow", "rules": rules}}))
    await loaded.build()
    await loaded.aclose()


# paths tools can write


async def test_tool_setting_over_a_state_dir_that_does_not_exist_yet(project: Path) -> None:
    loaded = load_config(
        edit(project, {"tool_settings": {"workspace": "newws"}, "store": "newws/.legion/l.db"})
    )
    with pytest.raises(ConfigError, match="state directory"):
        await loaded.build()
    assert not (project / "newws").exists()


async def test_tool_setting_over_the_config_is_refused(project: Path) -> None:
    loaded = load_config(
        edit(project, {"tool_settings": {"workspace": "."}, "store": "/tmp/elsewhere/l.db"})
    )
    with pytest.raises(ConfigError, match=r"legion\.yaml"):
        await loaded.build()


# endpoints and secrets


@pytest.mark.parametrize(
    ("url", "access", "problem"),
    [
        ("http://models.example.com/v1", "api_key", "https"),
        # the name reaches whichever of 127.0.0.1 and ::1 the client picks
        ("http://localhost:11434/v1", "api_key", "https"),
        ("https://user:pw@models.example.com/v1", "none", "username or password"),
        ("file:///etc/passwd", "none", "http"),
        ("ftp://localhost/v1", "none", "http"),
    ],
)
def test_provider_url_is_checked(project: Path, url: str, access: str, problem: str) -> None:
    config = yaml.safe_load((project / "legion.yaml").read_text())
    provider: dict[str, object] = {"kind": "openai_compat", "base_url": url}
    if access == "api_key":
        provider["access"] = {"kind": "api_key", "secret": "env:SOME_KEY"}
    config["providers"]["checked"] = provider
    with pytest.raises(ConfigError, match=problem):
        load_config(edit(project, {"providers": config["providers"]}))


def test_plain_http_to_a_local_model_without_a_key_is_fine(project: Path) -> None:
    config = yaml.safe_load((project / "legion.yaml").read_text())
    config["providers"]["lan"] = {"kind": "openai_compat", "base_url": "http://10.0.0.5:8000/v1"}
    load_config(edit(project, {"providers": config["providers"]}))


def test_literal_secret_is_refused_without_repeating_it(project: Path) -> None:
    config = yaml.safe_load((project / "legion.yaml").read_text())
    config["providers"]["leaky"] = {
        "kind": "anthropic",
        "access": {"kind": "api_key", "secret": "sk-ant-LITERAL-1234"},
    }
    with pytest.raises(ConfigError) as caught:
        load_config(edit(project, {"providers": config["providers"]}))
    assert "LITERAL" not in str(caught.value)


def test_secret_with_no_auth_is_refused(project: Path) -> None:
    config = yaml.safe_load((project / "legion.yaml").read_text())
    config["providers"]["odd"] = {
        "kind": "anthropic",
        "access": {"kind": "none", "secret": "env:KEY"},
    }
    with pytest.raises(ConfigError, match="access kind is none"):
        load_config(edit(project, {"providers": config["providers"]}))


def test_credentials_must_be_references_at_load(project: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(edit(project, {"credentials": {"github": "ghp_LITERALSECRET"}}))
    assert "LITERALSECRET" not in str(caught.value)


@pytest.mark.parametrize(
    ("server", "problem"),
    [
        ({"transport": "http", "url": "https://u:p@mcp.example.com/mcp"}, "username or password"),
        ({"transport": "http", "url": "http://localhost.evil.example/mcp"}, "https"),
        ({"transport": "stdio", "command": ["x"], "url": "https://a.example"}, "don't apply"),
        ({"transport": "http", "url": "https://a.example", "command": ["x"]}, "don't apply"),
        ({"transport": "stdio", "command": [""]}, "no command"),
    ],
)
def test_mcp_server_config_is_checked(server: dict[str, object], problem: str) -> None:
    config = McpServerConfig.model_validate({**server, "tools": {"t": {"pin": PIN}}})
    with pytest.raises(ConfigError, match=problem):
        config.check("srv")


@pytest.mark.parametrize(
    ("command", "package"),
    [
        (
            ["npx", "-y", "@modelcontextprotocol/server-github"],
            "@modelcontextprotocol/server-github",
        ),
        (["npx", "-y", "@modelcontextprotocol/server-github@1.2.0"], None),
        (["uvx", "mcp-server-git"], "mcp-server-git"),
        (["uvx", "mcp-server-git==0.6.2"], None),
        (["github-mcp-server", "stdio"], None),
    ],
)
def test_unversioned_runner_packages_are_noticed(command: list[str], package: str | None) -> None:
    config = McpServerConfig(transport="stdio", command=tuple(command), tools={})
    assert unpinned_package(config) == package


def test_mcp_tool_config_still_requires_a_pin() -> None:
    with pytest.raises(ValueError):
        McpToolConfig()  # type: ignore[call-arg]
