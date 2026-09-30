# Writing a tool

A Legion tool is a Python function with a declaration next to it: what effect it has, which
capability it needs, and which resource a call touches. Legion checks the declaration against the
task's grant before the function runs; the function itself only runs if every check passes.

This page builds one small tool in a starter project (`legion init ~/legion-demo`), wires it up,
and runs it with a scripted model so you can see a call allowed and a call refused. Every block
below is a whole file or a whole addition; nothing is left out.

## 1. The tool module

Create `todo_tools.py` next to `legion.yaml`:

```python
# file: todo_tools.py
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from legion.domain.action import EffectClass
from legion.tools.base import ToolContext
from legion.tools.native import tool

LIST_NAME = r"^[a-z][a-z0-9-]{0,31}$"


class AddArgs(BaseModel):
    list: str = Field(pattern=LIST_NAME, description="Which list, e.g. inbox.")
    item: str = Field(min_length=1, max_length=200)


class ShowArgs(BaseModel):
    list: str = Field(pattern=LIST_NAME)


def _path(ctx: ToolContext, name: str) -> Path:
    # config_dir is the directory holding legion.yaml; Legion sets it for every tool
    return Path(ctx.settings["config_dir"]) / "todos" / f"{name}.json"


def _load(path: Path) -> list[str]:
    return json.loads(path.read_text()) if path.exists() else []


@tool(effect=EffectClass.READ, capabilities=["todos.read"], resource=lambda a: a.list)
def show_todos(args: ShowArgs, ctx: ToolContext) -> list[str]:
    """Show the items on a to-do list."""
    return _load(_path(ctx, args.list))


@tool(effect=EffectClass.WRITE, capabilities=["todos.write"], resource=lambda a: a.list)
def add_todo(args: AddArgs, ctx: ToolContext) -> str:
    """Add one item to a to-do list."""
    path = _path(ctx, args.list)
    items = _load(path)
    items.append(args.item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items))
    return f"added to {args.list} ({len(items)} items)"


TOOLS = [show_todos, add_todo]
```

What each part does:

- **Arguments are a pydantic model.** Legion turns it into the JSON schema the model sees and
  validates every call against it before anything else. Keep fields as narrow as you can: the
  `pattern` above means a list name can't be a path.
- **`effect`** says what a call does, and decides what happens on timeouts and crashes:
  `pure` and `read` are retried and re-run after a crash; `write_idempotent` too, with
  `ctx.idempotency_key` staying the same; `write` and `external_irreversible` are never run twice
  automatically, and a crash mid-call leaves them in doubt for a person to reconcile. Appending
  to a list isn't idempotent, so `add_todo` is `write`. Pick the stricter class when unsure.
- **`capabilities`** are what a call needs, as names. **`resource`** says what a call touches,
  computed from the arguments. Together they make `todos.write:inbox`, which the task's grant has
  to cover, or the call is refused before your function runs.
- **The function** gets the validated arguments and a `ToolContext`: `settings` from
  `legion.yaml`, `credentials` the operator bound to the tool, `idempotency_key`, and the call's
  ids. Return a string, a list or dict (sent to the model as JSON), or a `ToolResult`. Raise
  `ToolRetryable` for a transient failure, `ActionInDoubt` if you don't know whether the effect
  happened; any other exception is reported to the model as a failure.
- **`TOOLS`** at module level lists what the module offers. Legion loads the module (it runs the
  file, like any Python import) and registers exactly these.

## 2. Tell Legion about it

In `legion.yaml`, add the module and a model binding for the scripted model below, and say how
much of the new capability agents may be given. Add to the existing lists:

```yaml
# file: legion.yaml (additions)
tool_modules:
  - todo_tools.py
providers:
  todo-script:
    kind: scripted
    script: scripts/todo.yaml
models:
  - profile: demo/todo
    provider: todo-script
    model: scripted
authority:
  grantable:
    - todos.read:inbox
    - todos.write:inbox
```

`authority.grantable` is the most any agent can hold. Name exact resources rather than `todos.*`
or `todos.write:**` unless you mean it.

## 3. An agent that uses it

```yaml
# file: agents/todo.yaml
name: todo-keeper
instructions: Keep the inbox to-do list up to date.
model:
  profile: demo/todo
  needs: [tools]
tools: [show_todos, add_todo]
capabilities:
  - todos.read:inbox
  - todos.write:inbox
budget: {steps: 6, model_calls: 6, tool_calls: 6}
```

And the scripted model, which adds to the inbox and then tries another list:

```yaml
# file: scripts/todo.yaml
- tool_calls:
    - name: add_todo
      arguments: {list: inbox, item: renew the certificate}
- tool_calls:
    - name: add_todo
      arguments: {list: boss, item: approve my raise}
- text: Added one item to the inbox.
```

## 4. Run it

```bash
legion agent validate agents/todo.yaml
legion run agents/todo.yaml "Note the certificate renewal"
```

The run completes with `Legion refused 1 action (capability_denied)`: the grant covers
`todos.write:inbox`, not `todos.write:boss`, so `add_todo` never ran for `boss`.
`legion inspect <run-id>` shows each proposed call and why it was allowed or refused.

## Security notes

- Tools are trusted code. They run in the Legion process with its permissions, and Legion can't
  stop a tool from doing more than it declares. The declaration is what Legion checks, so it
  has to match what the tool does.
- The resource function sees the arguments as the model wrote them. If the tool acts on
  something derived from them (a path, a URL), compute the resource the same way the tool does,
  and refuse anything ambiguous, as the starter `tools.py` does with paths and symlinks.
- Use `credentials=["name"]` on the decorator to receive a credential from `legion.yaml`, never
  an environment variable read inside the tool. Secrets passed that way are scrubbed from events
  and from tool output.
- Tool output goes back to the model as data. Don't put secrets in it.

## Testing a tool

Run it through Legion with a scripted model, as above, and assert on the events: `legion run
--json` prints the outcome (including `refused`), and `legion inspect --json` prints every event.
Legion's own tests do the same with `tests/support.py` (`build(...)` with a scripted provider);
see `tests/unit/test_tools.py`.
