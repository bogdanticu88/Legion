from __future__ import annotations

import asyncio
import getpass
import json
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Column, Table

from legion.config.loader import Loaded, load_agent, load_config
from legion.config.templates import write_project
from legion.domain.errors import LegionError
from legion.domain.principal import Principal, PrincipalKind
from legion.domain.states import RunStatus
from legion.events.types import Event, EventType

app = typer.Typer(help="Legion: run agents through one enforcement path.", no_args_is_help=True)
agent_app = typer.Typer(help="Work with agent definitions.", no_args_is_help=True)
app.add_typer(agent_app, name="agent")

out = Console()
err = Console(stderr=True)

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to legion.yaml.", envvar="LEGION_CONFIG"),
]


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _load(path: Path) -> Loaded:
    try:
        return load_config(path)
    except LegionError as exc:
        err.print(f"[red]error:[/red] {exc.message}")
        raise typer.Exit(2) from exc


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to create the project.")] = Path("."),
) -> None:
    """Create legion.yaml, an example agent, a tool module and a model script."""
    created = write_project(directory)
    if not created:
        err.print("nothing written: files already exist")
        raise typer.Exit(1)
    for path in created:
        out.print(f"created {path}")
    out.print('\nnext: legion run agents/assistant.yaml "Summarize the notes"')


@app.command()
def providers(config: ConfigOption = Path("legion.yaml")) -> None:
    """List model bindings and whether their credentials are available. Never prints secrets."""
    loaded = _load(config)
    table = Table("profile", "provider", "kind", "model", "features", "access", "pricing")
    for binding in loaded.config.models:
        provider = loaded.config.providers[binding.provider]
        access: str = provider.access.kind
        if provider.access.secret:
            name = provider.access.secret.split(":", 1)[1]
            access += f" {provider.access.secret} ({'set' if os.environ.get(name) else 'missing'})"
        pricing = (
            f"{binding.pricing.input_per_mtok}/{binding.pricing.output_per_mtok} per Mtok"
            if binding.pricing
            else "-"
        )
        table.add_row(
            binding.profile,
            binding.provider,
            provider.kind,
            binding.model,
            ", ".join(sorted(f.value for f in binding.features)),
            access,
            pricing,
        )
    out.print(table)


@agent_app.command("validate")
def validate(
    agent_file: Path,
    config: ConfigOption = Path("legion.yaml"),
) -> None:
    """Check that an agent can start under this configuration."""
    loaded = _load(config)
    try:
        agent = load_agent(agent_file)
        legion = loaded.build()
    except LegionError as exc:
        err.print(f"[red]error:[/red] {exc.message}")
        raise typer.Exit(2) from exc
    errors, warnings = legion.check(agent)
    for warning in warnings:
        out.print(f"[yellow]warning:[/yellow] {warning}")
    for error in errors:
        out.print(f"[red]error:[/red] {error}")
    if errors:
        raise typer.Exit(1)
    out.print(
        f"[green]ok[/green] {agent.name} ({len(agent.tools)} tools, spec {agent.spec_hash[:12]})"
    )


@app.command()
def run(
    agent_file: Path,
    objective: str,
    config: ConfigOption = Path("legion.yaml"),
    as_json: Annotated[bool, typer.Option("--json", help="Print the outcome as JSON.")] = False,
) -> None:
    """Run an agent on an objective."""
    loaded = _load(config)
    principal = Principal(kind=PrincipalKind.HUMAN, id=f"local:{getpass.getuser()}")

    async def go() -> Any:
        store = loaded.store()
        try:
            legion = loaded.build(store)
            agent = load_agent(agent_file)
            return await legion.run(agent, objective, principal=principal)
        finally:
            await loaded.aclose()
            store.close()

    try:
        outcome = _run(go())
    except LegionError as exc:
        err.print(f"[red]error:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    if as_json:
        out.print_json(
            json.dumps(
                {
                    "run_id": outcome.run_id,
                    "status": outcome.status.value,
                    "output": outcome.output,
                    "structured": outcome.structured,
                    "error_code": outcome.error_code,
                    "error": outcome.error_message,
                }
            )
        )
    else:
        out.print(f"run {outcome.run_id}: {outcome.status.value}")
        if outcome.output:
            out.print(outcome.output)
        if outcome.error_code:
            out.print(f"[red]{outcome.error_code}[/red]: {outcome.error_message}")
    if outcome.status is not RunStatus.COMPLETED:
        raise typer.Exit(1)


@app.command()
def runs(config: ConfigOption = Path("legion.yaml")) -> None:
    """List recorded runs."""
    loaded = _load(config)
    store = loaded.store()
    try:
        summaries = _run(store.runs())
    finally:
        store.close()
    table = Table(Column("run", no_wrap=True, min_width=20), "agent", "created", "status", "events")
    for s in summaries:
        table.add_row(
            s.run_id, s.agent, s.created_at.isoformat(timespec="seconds"), s.status, str(s.events)
        )
    out.print(table)


@app.command()
def inspect(
    run_id: str,
    config: ConfigOption = Path("legion.yaml"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Print raw events as JSON lines.")
    ] = False,
) -> None:
    """Show a run's events in order."""
    loaded = _load(config)
    store = loaded.store()
    try:
        events = _run(store.read(run_id))
    finally:
        store.close()
    if not events:
        err.print(f"no run {run_id}")
        raise typer.Exit(1)
    for event in events:
        if as_json:
            print(json.dumps({**event.body(), "hash": event.hash}, sort_keys=True))
        else:
            time = event.ts.strftime("%H:%M:%S.%f")[:-3]
            out.print(
                f"{event.seq:>4} {time} [bold]{event.type.value:<18}[/bold] {describe(event)}",
                highlight=False,
            )


@app.command()
def verify(run_id: str, config: ConfigOption = Path("legion.yaml")) -> None:
    """Recompute a run's hash chain from the stored events."""
    loaded = _load(config)
    store = loaded.store()
    try:
        result = _run(store.verify(run_id))
    finally:
        store.close()
    if result.checked == 0 and result.ok:
        err.print(f"no run {run_id}")
        raise typer.Exit(1)
    if result.ok:
        out.print(f"[green]ok[/green] {result.checked} events, chain intact")
        return
    out.print(f"[red]broken[/red] at seq {result.bad_seq}: {result.reason}")
    raise typer.Exit(1)


def describe(event: Event) -> str:
    p = event.payload
    match event.type:
        case EventType.RUN_CREATED:
            return f"agent={p['agent']} model={p['provider']}/{p['model']}"
        case EventType.TASK_CREATED:
            return f"{event.task_id} objective={p['task']['objective'][:60]!r}"
        case EventType.MODEL_REQUESTED:
            return f"attempt {p['attempt']}, {p['message_count']} messages"
        case EventType.MODEL_RESPONDED:
            usage = p["usage"]
            calls = [part["name"] for part in p["message"]["parts"] if part["type"] == "tool_call"]
            what = f"calls {', '.join(calls)}" if calls else "answers"
            return f"{what} ({usage['input_tokens']} in / {usage['output_tokens']} out)"
        case EventType.MODEL_FAILED | EventType.TOOL_FAILED:
            retry = " (will retry)" if p["will_retry"] else ""
            return f"{p['error_code']}: {p['message']}{retry}"
        case EventType.ACTION_PROPOSED:
            resource = f" on {p['resource']}" if p["resource"] else ""
            return f"{p['tool']}{resource} [{p['effect']}] {p['action_hash'][:12]}"
        case EventType.ACTION_REFUSED:
            return f"{p['tool']}: {p['reason_code']}: {p['message']}"
        case EventType.ACTION_AUTHORIZED:
            return f"{p['action_hash'][:12]}"
        case EventType.ACTION_REPEATED:
            return f"{p['tool']} x{p['count']}"
        case EventType.ACTION_IN_DOUBT:
            return f"{p['action_hash'][:12]} [{p['effect']}] {p['reason']}"
        case EventType.TOOL_STARTED:
            return f"{p['call_id']} attempt {p['attempt']}"
        case EventType.TOOL_COMPLETED:
            extra = " truncated" if p["truncated"] else ""
            return f"{p['call_id']} {len(p['content'])} chars in {p['latency_ms']}ms{extra}"
        case EventType.BUDGET_CONSUMED:
            return f"{p['dimension']} +{p['amount']} = {p['total']}"
        case EventType.BUDGET_EXCEEDED:
            return f"{p['dimension']} limit {p['limit']}"
        case EventType.TASK_COMPLETED | EventType.RUN_COMPLETED:
            return repr(p["output"][:80])
        case EventType.TASK_FAILED | EventType.RUN_FAILED:
            return f"{p['error_code']}: {p['message']}"
        case EventType.OUTPUT_REJECTED:
            return str(p["reason"])
        case _:
            return ""
