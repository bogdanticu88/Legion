# File tools for the notes example.

from __future__ import annotations

import posixpath
from pathlib import Path

from pydantic import BaseModel, Field

from legion.domain.action import EffectClass
from legion.tools.base import ToolContext
from legion.tools.native import tool


def _relative(path: str) -> str:
    if path.startswith("/") or "\\" in path:
        return "/" + path.lstrip("/")
    return posixpath.normpath(path)


def _workspace(ctx: ToolContext) -> Path:
    return (Path(ctx.settings["config_dir"]) / ctx.settings["workspace"]).resolve()


def _inside(ctx: ToolContext, relative: str) -> Path:
    # The capability check only saw the path text, so refuse symlinks that land somewhere else.
    root = _workspace(ctx)
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target.relative_to(root).as_posix() != relative:
        raise ValueError("path leaves the workspace or goes through a link")
    return target


class ListArgs(BaseModel):
    folder: str = Field(default="notes", description="Folder inside the workspace.")


class ReadArgs(BaseModel):
    path: str = Field(description="File path inside the workspace, e.g. notes/meeting.md.")


class WriteArgs(BaseModel):
    path: str = Field(description="Destination inside the workspace, e.g. out/summary.md.")
    text: str = Field(max_length=10_000)


@tool(
    effect=EffectClass.READ,
    capabilities=["files.read"],
    resource=lambda a: _relative(a.folder).rstrip("/") + "/",
)
def list_notes(args: ListArgs, ctx: ToolContext) -> list[str]:
    """List the files in a workspace folder."""
    folder = _inside(ctx, _relative(args.folder))
    return sorted(p.name for p in folder.iterdir() if p.is_file())


@tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: _relative(a.path))
def read_note(args: ReadArgs, ctx: ToolContext) -> str:
    """Read one file from the workspace."""
    return _inside(ctx, _relative(args.path)).read_text(encoding="utf-8")


@tool(
    effect=EffectClass.WRITE_IDEMPOTENT,
    capabilities=["files.write"],
    resource=lambda a: _relative(a.path),
)
def write_summary(args: WriteArgs, ctx: ToolContext) -> str:
    """Write text to a file in the workspace, replacing it if it exists."""
    target = _inside(ctx, _relative(args.path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args.text, encoding="utf-8")
    return f"wrote {len(args.text)} characters to {args.path}"


class PublishArgs(BaseModel):
    channel: str = Field(pattern=r"^[a-z0-9-]{1,32}$", description="Where to publish, e.g. team.")
    text: str = Field(max_length=2_000)


# "Publishing" here just writes a file, but it stands in for something you can't take back
# (a message, a deploy), so it's declared irreversible and the default policy asks a human.
@tool(
    effect=EffectClass.EXTERNAL_IRREVERSIBLE,
    capabilities=["notes.publish"],
    resource=lambda a: a.channel,
)
def publish_summary(args: PublishArgs, ctx: ToolContext) -> str:
    """Publish a summary to a channel. Once published it can't be unpublished."""
    target = _inside(ctx, f"published/{args.channel}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args.text, encoding="utf-8")
    return f"published to {args.channel}"


TOOLS = [list_notes, read_note, write_summary, publish_summary]
