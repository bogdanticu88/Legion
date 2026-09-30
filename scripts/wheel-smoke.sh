#!/usr/bin/env bash
# Install a built wheel into a fresh virtualenv, outside the source tree, and run the offline
# quickstart with it. Used by CI and by the release checklist.
#
#   uv build && scripts/wheel-smoke.sh dist/legion_runtime-*.whl
set -euo pipefail

wheel="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --quiet "$wheel"
legion="$work/venv/bin/legion"
py="$work/venv/bin/python"

# the version the package reports is the one in its metadata, and the typing marker ships
"$py" - <<'PY'
import importlib.metadata as md
import importlib.resources as res
import legion
assert legion.__version__ == md.version("legion-runtime"), (legion.__version__, md.version("legion-runtime"))
assert res.files("legion").joinpath("py.typed").is_file(), "py.typed missing from the wheel"
print("version", legion.__version__, "and py.typed ok")
PY
"$legion" --version
"$legion" --help > /dev/null

cd "$work"
"$legion" init demo > /dev/null
cd demo
out="$("$legion" run agents/assistant.yaml "Summarize the notes")"
echo "$out" | head -1
echo "$out" | head -1 | grep -q "completed; Legion refused 1 action (capability_denied)"
run_id="$(echo "$out" | head -1 | grep -o 'run_[0-9a-f]*')"
"$legion" verify "$run_id"

# the approval flow pauses with exit code 3
set +e
"$legion" run agents/publisher.yaml "Publish the meeting summary" > /dev/null
code=$?
set -e
test "$code" = 3
echo "wheel smoke test passed"
