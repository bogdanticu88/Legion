# A real crash: the process running the tool exits in the middle of a write, and recovery goes
# through the CLI in fresh processes, like an operator would do it.

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from legion.cli import app

CRASHY = '''
import os
from pathlib import Path

from pydantic import BaseModel

from legion.domain.action import EffectClass
from legion.tools.base import ToolContext
from legion.tools.native import tool


class Args(BaseModel):
    line: str


@tool(effect=EffectClass.WRITE, capabilities=["files.write"], resource=lambda a: "out/log")
def append_line(args: Args, ctx: ToolContext) -> str:
    """Append a line to out/log."""
    target = Path(ctx.settings["config_dir"]) / "workspace" / "out" / "log"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as fh:
        fh.write(args.line + "\\n")
    if os.environ.get("LEGION_TEST_CRASH"):
        os._exit(9)
    return "appended"


TOOLS = [append_line]
'''

AGENT = """name: appender
instructions: Append one line.
model: {profile: demo/crash, needs: [tools]}
tools: [append_line]
capabilities: ["files.write:out/**"]
"""

SCRIPT = """- tool_calls:
    - name: append_line
      arguments: {line: hello}
- text: appended
"""


def legion(project: Path, *args: str, crash: bool = False) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "LEGION_TEST_CRASH"}
    if crash:
        env["LEGION_TEST_CRASH"] = "1"
    return subprocess.run(
        [sys.executable, "-c", "from legion.cli import app; app()", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_process_killed_mid_write_is_not_repeated(tmp_path: Path) -> None:
    assert CliRunner().invoke(app, ["init", str(tmp_path)]).exit_code == 0
    (tmp_path / "crashy.py").write_text(CRASHY)
    (tmp_path / "agents" / "appender.yaml").write_text(AGENT)
    (tmp_path / "scripts" / "crash.yaml").write_text(SCRIPT)
    config = (tmp_path / "legion.yaml").read_text()
    config = config.replace("  - tools.py\n", "  - tools.py\n  - crashy.py\n")
    config = config.replace(
        "providers:\n",
        "providers:\n  crash-script:\n    kind: scripted\n    script: scripts/crash.yaml\n",
    )
    config = config.replace(
        "models:\n",
        "models:\n  - profile: demo/crash\n    provider: crash-script\n    model: scripted\n",
    )
    (tmp_path / "legion.yaml").write_text(config)
    log = tmp_path / "workspace" / "out" / "log"

    died = legion(tmp_path, "run", "agents/appender.yaml", "append", crash=True)
    assert died.returncode == 9, died.stderr
    assert log.read_text() == "hello\n"

    runs = legion(tmp_path, "runs")
    run_id = next(w for w in runs.stdout.split() if w.startswith("run_"))
    assert "running" in runs.stdout

    blocked = legion(tmp_path, "resume", run_id, "--json")
    assert blocked.returncode == 3, blocked.stdout + blocked.stderr
    outcome = json.loads(blocked.stdout)
    assert outcome["status"] == "paused" and outcome["blocked_call"]
    assert log.read_text() == "hello\n"

    fixed = legion(
        tmp_path,
        "reconcile",
        run_id,
        outcome["blocked_call"],
        "--outcome",
        "applied",
        "--note",
        "saw the line in out/log",
    )
    assert fixed.returncode == 0, fixed.stderr

    done = legion(tmp_path, "resume", run_id, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "completed"
    assert log.read_text() == "hello\n"
    assert legion(tmp_path, "verify", run_id).returncode == 0
