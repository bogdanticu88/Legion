# Secrets the configuration can resolve are scrubbed from the first event of a run, including
# after a restart, whichever tool or server they come back from.

from pathlib import Path

import pytest

from legion.access.secrets import EnvResolver, SecretRef
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.events.types import EventType
from legion.kernel.services import _redact_tree, redact_text
from legion.models.scripted import call, reply
from tests.support import Files, SimulatedCrash, agent, build, crash_at

TOKEN = "tool-cred-SENTINEL-7c1d2e"
BINDINGS = {"api_token": SecretRef.parse("env:API_TOKEN")}


def assert_clean(tmp_path: Path) -> None:
    for path in (p for p in tmp_path.rglob("*") if p.is_file()):
        assert TOKEN.encode() not in path.read_bytes(), path


async def test_known_secret_is_scrubbed_before_any_tool_asks_for_it(tmp_path: Path) -> None:
    # read_file has no credentials, but the file happens to contain one
    store = SqliteEventStore(tmp_path / "e.db")
    h = build(
        [call("read_file", {"path": "docs/a.md"}), reply("ok")],
        store=store,
        files=Files({"docs/a.md": f"API_TOKEN={TOKEN}"}),
        credentials=EnvResolver({"API_TOKEN": TOKEN}),
        credential_bindings=BINDINGS,
    )
    outcome = await h.run(agent(tools=["read_file"], capabilities=["files.read:docs/**"]))
    [done] = await h.payloads(outcome.run_id, EventType.TOOL_COMPLETED)
    assert "[redacted]" in done["content"]
    assert TOKEN not in h.provider.requests[-1].model_dump_json()
    store.close()
    assert_clean(tmp_path)


async def test_secrets_are_still_scrubbed_after_a_restart(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "e.db")
    steps = [
        call("read_file", {"path": "docs/b.md"}, id="r1"),
        call("read_file", {"path": "docs/a.md"}, id="r2"),
        reply("ok"),
    ]
    files = Files({"docs/a.md": f"API_TOKEN={TOKEN}", "docs/b.md": "nothing"})
    common = {"credentials": EnvResolver({"API_TOKEN": TOKEN}), "credential_bindings": BINDINGS}
    h = build(
        steps,
        store=store,
        files=files,
        by_turn=True,
        faults=crash_at("after:tool.completed"),
        **common,
    )
    spec = agent(tools=["read_file"], capabilities=["files.read:docs/**"])
    with pytest.raises(SimulatedCrash):
        await h.run(spec)
    [summary] = await store.runs()
    outcome = await h.restart(**common).resume(summary.run_id)
    assert outcome.status is RunStatus.COMPLETED
    contents = [p["content"] for p in await h.payloads(summary.run_id, EventType.TOOL_COMPLETED)]
    assert any("[redacted]" in c for c in contents)
    store.close()
    assert_clean(tmp_path)


def test_longer_secret_is_replaced_whole() -> None:
    # shorter one first: replacing it first would leave the longer one's tail behind
    text, _ = redact_text("x=abcd1234-and-more-secret", ["abcd1234", "abcd1234-and-more-secret"])
    assert text == "x=[redacted]"


def test_dictionary_keys_are_scrubbed() -> None:
    assert _redact_tree({TOKEN: {"v": TOKEN}}, {TOKEN}) == {"[redacted]": {"v": "[redacted]"}}
