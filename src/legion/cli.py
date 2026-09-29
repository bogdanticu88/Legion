from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table

from legion.config.loader import Loaded, load_agent, load_config
from legion.config.templates import write_project
from legion.domain.errors import LegionError
from legion.domain.principal import Principal, PrincipalKind
from legion.domain.states import RunStatus
from legion.events.projections import TaskView
from legion.events.types import Event, EventType
from legion.kernel import operator
from legion.kernel.runtime import Legion, RunOutcome, load_state
from legion.tools.mcp import discover

app = typer.Typer(help="Legion: run agents through one enforcement path.", no_args_is_help=True)
agent_app = typer.Typer(help="Work with agent definitions.", no_args_is_help=True)
approval_app = typer.Typer(help="Inspect approvals.", no_args_is_help=True)
mcp_app = typer.Typer(help="Check MCP servers against their manifests.", no_args_is_help=True)
app.add_typer(agent_app, name="agent")
app.add_typer(approval_app, name="approval")
app.add_typer(mcp_app, name="mcp")

out = Console()
err = Console(stderr=True)

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to legion.yaml.", envvar="LEGION_CONFIG"),
]


# Anything that came from a run (model text, tool output, arguments, error messages) is untrusted.
# Rich would treat "[green]approved[/green]" in it as formatting, and terminal control or bidi
# characters can hide or rearrange text, so all of it goes through _safe before printing.
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _safe(value: object) -> str:
    return escape(_UNSAFE_CHARS.sub("?", str(value)))


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _load(path: Path) -> Loaded:
    try:
        return load_config(path)
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
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
    """List model bindings and whether their API keys are set."""
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
        legion = _run(_build_and_close(loaded))
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc
    errors, warnings = legion.check(agent)
    for warning in warnings:
        out.print(f"[yellow]warning:[/yellow] {_safe(warning)}")
    for error in errors:
        out.print(f"[red]error:[/red] {_safe(error)}")
    if errors:
        raise typer.Exit(1)
    out.print(
        f"[green]ok[/green] {agent.name} ({len(agent.tools)} tools, spec {agent.spec_hash[:12]})"
    )


async def _build_and_close(loaded: Loaded) -> Legion:
    try:
        return await loaded.build()
    finally:
        await loaded.aclose()


def _principal() -> Principal:
    # The local OS user. Legion doesn't authenticate approvers; anyone who can run this CLI
    # against the store is trusted (see THREAT_MODEL.md).
    return Principal(kind=PrincipalKind.HUMAN, id=f"local:{getpass.getuser()}")


EXIT_PAUSED = 3


def _report(outcome: RunOutcome, loaded: Loaded, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "run_id": outcome.run_id,
                    "status": outcome.status.value,
                    "output": outcome.output,
                    "structured": outcome.structured,
                    "error_code": outcome.error_code,
                    "error": outcome.error_message,
                    "approval_id": outcome.approval_id,
                    "blocked_call": outcome.blocked_call,
                }
            )
        )
    else:
        out.print(f"run {outcome.run_id}: {outcome.status.value}")
        if outcome.output:
            out.print(_safe(outcome.output))
        if outcome.error_code:
            out.print(f"[red]{_safe(outcome.error_code)}[/red]: {_safe(outcome.error_message)}")
        if outcome.approval_id:
            out.print("\n[bold]Approval required[/bold]")
            _show_approval(loaded, outcome.approval_id)
            out.print(
                f"\nlegion approve {outcome.approval_id}   or   legion deny {outcome.approval_id}"
                f"\nthen: legion resume {outcome.run_id}"
            )
        elif outcome.blocked_call:
            out.print(
                f"\n[bold]Can't continue automatically.[/bold] Call {outcome.blocked_call} may or "
                "may not have taken effect, and Legion won't run it again on its own.\n"
                "Check the target system, then record what happened:\n"
                f"  legion reconcile {outcome.run_id} {outcome.blocked_call} "
                "--outcome applied|not-applied|abandon"
            )
    if outcome.status is RunStatus.PAUSED:
        raise typer.Exit(EXIT_PAUSED)
    if outcome.status is not RunStatus.COMPLETED:
        raise typer.Exit(1)


@app.command()
def run(
    agent_file: Path,
    objective: str,
    config: ConfigOption = Path("legion.yaml"),
    as_json: Annotated[bool, typer.Option("--json", help="Print the outcome as JSON.")] = False,
) -> None:
    """Run an agent on an objective. Exits 3 if the run pauses for a human."""
    loaded = _load(config)

    async def go() -> RunOutcome:
        store = loaded.store()
        try:
            legion = await loaded.build(store)
            agent = load_agent(agent_file)
            return await legion.run(agent, objective, principal=_principal())
        finally:
            await loaded.aclose()
            store.close()

    try:
        outcome = _run(go())
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc
    _report(outcome, loaded, as_json)


@app.command()
def resume(
    run_id: str,
    config: ConfigOption = Path("legion.yaml"),
    as_json: Annotated[bool, typer.Option("--json", help="Print the outcome as JSON.")] = False,
) -> None:
    """Continue a paused or crashed run from its event log."""
    loaded = _load(config)

    async def go() -> RunOutcome:
        store = loaded.store()
        try:
            return await (await loaded.build(store)).resume(run_id, principal=_principal())
        finally:
            await loaded.aclose()
            store.close()

    try:
        outcome = _run(go())
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc
    _report(outcome, loaded, as_json)


def _origin_rows(origin: Any) -> list[tuple[str, str]]:
    if not isinstance(origin, dict):
        return []
    scope = origin.get("credential_scope") or "not declared"
    return [
        ("runs on", f"MCP server {origin.get('server')} as {origin.get('remote_tool')}"),
        ("server credential", f"{scope} (Legion's grant doesn't narrow this)"),
    ]


def _show_approval(loaded: Loaded, approval_id: str) -> None:
    store = loaded.store()
    try:
        state, approval = _run(operator.find(store, approval_id))
    finally:
        store.close()
    s = approval.subject
    expired = approval.expired(datetime.now(UTC))
    rows = [
        ("approval", approval.id),
        (
            "status",
            approval.status + (" (expired)" if expired and approval.status == "requested" else ""),
        ),
        ("run", f"{state.run_id}   task {approval.task_id}   call {approval.call_id}"),
        ("agent", f"{s.get('agent')} acting for {', '.join(s.get('on_behalf_of', []))}"),
        ("objective", str(s.get("objective", ""))),
        ("tool", f"{s.get('tool')}: {s.get('description', '')}"),
        ("effect", str(s.get("effect"))),
        ("target", str(s.get("resource") or "(no resource)")),
        ("needs", ", ".join(s.get("required", [])) or "-"),
        *_origin_rows(s.get("origin")),
        ("expires", approval.expires_at.isoformat(timespec="seconds")),
    ]
    if approval.decided_by:
        rows.append(("decided by", f"{approval.decided_by} {approval.note}".strip()))
    table = Table(show_header=False, box=None)
    for label, value in rows:
        table.add_row(f"[bold]{label}[/bold]", _safe(value))
    out.print(table)
    out.print("[bold]arguments[/bold]")
    out.print_json(json.dumps(s.get("arguments", {}), sort_keys=True))
    if s.get("model_note"):
        out.print(f"[bold]model says (untrusted)[/bold]\n{_safe(s['model_note'])}", highlight=False)
    out.print(f"[dim]binding {approval.binding_hash}[/dim]")


@app.command()
def approvals(config: ConfigOption = Path("legion.yaml")) -> None:
    """List approvals waiting for a decision."""
    loaded = _load(config)
    store = loaded.store()
    try:
        pending = _run(operator.approvals(store))
    finally:
        store.close()
    table = Table(
        Column("approval", no_wrap=True), Column("run", no_wrap=True), "tool", "target", "expires"
    )
    for p in pending:
        expires = p.approval.expires_at.isoformat(timespec="minutes")
        table.add_row(
            p.approval.id,
            p.run_id,
            _safe(p.approval.subject.get("tool")),
            _safe(p.approval.subject.get("resource") or "-"),
            "[red]expired[/red]" if p.expired else expires,
        )
    out.print(table)


@approval_app.command("show")
def approval_show(approval_id: str, config: ConfigOption = Path("legion.yaml")) -> None:
    """Show exactly what an approval would allow."""
    loaded = _load(config)
    try:
        _show_approval(loaded, approval_id)
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc


def _decide(approval_id: str, config: Path, *, approve: bool, note: str) -> None:
    loaded = _load(config)
    store = loaded.store()
    try:
        approval = _run(
            operator.decide(
                store, loaded.locks(), approval_id, approve=approve, by=_principal(), note=note
            )
        )
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc
    finally:
        store.close()
    out.print(f"{approval.id}: {approval.status}")
    out.print(f"next: legion resume {approval.subject.get('run_id')}")


NoteOption = Annotated[str, typer.Option("--note", help="Recorded with the decision.")]


@app.command()
def approve(
    approval_id: str, config: ConfigOption = Path("legion.yaml"), note: NoteOption = ""
) -> None:
    """Approve one specific proposed action. Check it with `legion approval show` first."""
    _decide(approval_id, config, approve=True, note=note)


@app.command()
def deny(
    approval_id: str, config: ConfigOption = Path("legion.yaml"), note: NoteOption = ""
) -> None:
    """Deny a proposed action. The agent is told, and can try something else."""
    _decide(approval_id, config, approve=False, note=note)


class Outcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not-applied"
    ABANDON = "abandon"


_OUTCOMES: dict[Outcome, Literal["applied", "not_applied", "abandon"]] = {
    Outcome.APPLIED: "applied",
    Outcome.NOT_APPLIED: "not_applied",
    Outcome.ABANDON: "abandon",
}


@app.command()
def reconcile(
    run_id: str,
    call_id: str,
    outcome: Annotated[Outcome, typer.Option("--outcome", help="What actually happened.")],
    config: ConfigOption = Path("legion.yaml"),
    note: NoteOption = "",
) -> None:
    """Record whether an in-doubt action took effect, so the run can continue (or stop)."""
    loaded = _load(config)
    store = loaded.store()
    value = _OUTCOMES[outcome]
    try:
        _run(
            operator.reconcile(
                store, loaded.locks(), run_id, call_id, outcome=value, by=_principal(), note=note
            )
        )
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc
    finally:
        store.close()
    if outcome is Outcome.ABANDON:
        out.print(f"run {run_id}: failed (abandoned)")
    else:
        out.print(f"recorded. next: legion resume {run_id}")


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
            s.run_id,
            _safe(s.agent),
            s.created_at.isoformat(timespec="seconds"),
            s.status,
            str(s.events),
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
            kind = f"[bold]{event.type.value:<18}[/bold]"
            out.print(f"{event.seq:>4} {time} {kind} {_safe(describe(event))}", highlight=False)


@app.command()
def tasks(run_id: str, config: ConfigOption = Path("legion.yaml")) -> None:
    """Show a run's tasks: who handed work to whom, where each one is, and what it used."""
    loaded = _load(config)
    store = loaded.store()
    try:
        state = _run(load_state(store, run_id))
    except LegionError as exc:
        err.print(f"[red]error:[/red] {_safe(exc.message)}")
        raise typer.Exit(2) from exc
    finally:
        store.close()
    table = Table(
        Column("task", no_wrap=True), "agent", "status", "via call", "tool calls", "model calls"
    )

    def usage(view: TaskView, dimension: str) -> str:
        grant = view.grant if view.parent_id else state.grant
        limit = (grant or {}).get("budget", {}).get(dimension)
        used = state.used(view.grant_id, dimension) + state.committed(view.grant_id, dimension)
        return f"{used}/{limit if limit is not None else '-'}"

    def walk(task_id: str, depth: int) -> None:
        view = state.tasks[task_id]
        table.add_row(
            "  " * depth + view.id,
            _safe(view.agent),
            view.status.value,
            _safe(view.delegated_by or "-"),
            usage(view, "tool_calls"),
            usage(view, "model_calls"),
        )
        for child_id in view.children.values():
            walk(child_id, depth + 1)

    if state.root_task_id in state.tasks:
        walk(state.root_task_id, 0)
    out.print(table)


@mcp_app.command("inspect")
def mcp_inspect(server_id: str, config: ConfigOption = Path("legion.yaml")) -> None:
    """Show what a server offers, how it matches the manifest, and the pins to review."""
    loaded = _load(config)
    if server_id not in loaded.config.mcp_servers:
        err.print(f"[red]error:[/red] no MCP server {_safe(server_id)} in the configuration")
        raise typer.Exit(2)
    manifest = loaded.config.mcp_servers[server_id].tools

    async def go() -> Any:
        conn = loaded.connection(server_id)
        try:
            return conn, await discover(conn), await conn.remote_tools()
        finally:
            await loaded.aclose()

    try:
        conn, found, remote = _run(go())
    except Exception as exc:
        err.print(f"[red]error:[/red] can't reach {_safe(server_id)}: {_safe(type(exc).__name__)}")
        raise typer.Exit(2) from exc
    out.print(f"server {_safe(server_id)}  fingerprint {conn.fingerprint[:16]}")
    out.print(f"says it is: {_safe(conn.server_info)} (not verified)")
    table = Table("tool", "status", "pin to review", "claims (untrusted)")
    for key, cfg in manifest.items():
        status = (
            "[green]ok[/green]"
            if key not in found.blocked
            else f"[red]{_safe(found.blocked[key])}[/red]"
        )
        definition = remote.get(cfg.remote or key)
        claims = (
            _safe(definition.annotations.model_dump(exclude_none=True))
            if definition is not None and definition.annotations
            else "-"
        )
        table.add_row(_safe(key), status, found.pins.get(key, "-"), claims)
    for name in found.unlisted:
        table.add_row(_safe(name), "[dim]not in manifest, never used[/dim]", "-", "-")
    out.print(table)
    if found.blocked:
        raise typer.Exit(1)


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
        case EventType.APPROVAL_REQUESTED:
            subject = p["subject"]
            target = f" on {subject['resource']}" if subject.get("resource") else ""
            return f"{p['approval_id']} for {subject['tool']}{target}"
        case EventType.APPROVAL_GRANTED | EventType.APPROVAL_DENIED:
            return f"{p['approval_id']} by {p['by']} {p['note']}".rstrip()
        case (
            EventType.APPROVAL_EXPIRED
            | EventType.APPROVAL_CONSUMED
            | EventType.APPROVAL_INVALIDATED
            | EventType.TASK_AWAITING_APPROVAL
        ):
            return str(p["approval_id"])
        case EventType.RUN_PAUSED:
            return f"{p['reason']} {p.get('approval_id') or p.get('call_id') or ''}".rstrip()
        case EventType.RUN_RESUMED:
            return f"by {p['by']} (was {p['previous_status']})"
        case EventType.TASK_BLOCKED:
            return f"{p['call_id']}: {p['reason']}"
        case EventType.ACTION_INTERRUPTED:
            return f"{p['call_id']} [{p['effect']}] will run again"
        case EventType.ACTION_RECONCILED:
            return f"{p['call_id']} {p['outcome']} by {p['by']}"
        case EventType.OUTPUT_REJECTED:
            return str(p["reason"])
        case _:
            return ""
