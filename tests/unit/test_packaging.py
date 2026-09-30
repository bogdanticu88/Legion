# What gets shipped: one version, the typing marker, nothing that isn't the package.

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import legion

ROOT = Path(__file__).resolve().parents[2]


def test_version_has_one_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/legion/__init__.py"
    assert legion.__version__ == "0.1.0a1"


def test_typing_marker_is_in_the_source_tree() -> None:
    assert (ROOT / "src" / "legion" / "py.typed").is_file()


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("uv") is None:
        pytest.skip("uv isn't installed")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True, capture_output=True
    )
    [built] = out.glob("*.whl")
    return built


def test_wheel_ships_the_marker_and_the_same_version(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        [metadata] = [n for n in names if n.endswith(".dist-info/METADATA")]
        version = next(
            line.split(": ", 1)[1]
            for line in z.read(metadata).decode().splitlines()
            if line.startswith("Version: ")
        )
    assert "legion/py.typed" in names
    assert version == legion.__version__
    stray = [
        n
        for n in names
        if not n.startswith(("legion/", "legion_runtime-"))
        or "__pycache__" in n
        or n.endswith((".pyc", ".db", ".log"))
        or "/.legion/" in n
    ]
    assert stray == []


def test_downstream_type_checkers_see_legions_types(tmp_path: Path) -> None:
    # With py.typed, mypy checks a wrong use of Legion's types; without it, it would only say the
    # package has no types.
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from legion.domain.action import EffectClass\ncount: int = EffectClass.READ\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", str(sample)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "missing library stubs or py.typed marker" not in result.stdout, result.stdout
    assert "Incompatible types in assignment" in result.stdout, result.stdout
