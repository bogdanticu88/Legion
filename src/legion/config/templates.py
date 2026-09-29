from __future__ import annotations

import shutil
from importlib.resources import as_file, files
from pathlib import Path


def write_project(directory: Path) -> list[Path]:
    """Copy the starter project into `directory`, skipping files that already exist."""
    created: list[Path] = []
    with as_file(files("legion.templates") / "project") as source:
        for path in sorted(source.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            target = directory / path.relative_to(source)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            created.append(target)
    return created
