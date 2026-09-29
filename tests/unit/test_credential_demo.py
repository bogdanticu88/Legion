# examples/credential_demo.py has to keep showing what it says it shows.

import re
import subprocess
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[2] / "examples" / "credential_demo.py"


@pytest.mark.demo  # credentials: eight cases, local and deterministic
def test_credential_demo() -> None:
    result = subprocess.run(
        [sys.executable, str(DEMO)], capture_output=True, text=True, timeout=60, check=True
    )
    cases = {
        int(m.group(1)): body
        for m, body in zip(
            re.finditer(r"^CASE (\d+)", result.stdout, re.M),
            re.split(r"^CASE \d+.*$", result.stdout, flags=re.M)[1:],
            strict=True,
        )
    }
    assert "read_repo ran 1 time" in cases[1] and "got bound" in cases[1]
    assert "read_repo ran 0 time" in cases[2] and "permissions wider" in cases[2]
    assert "read_repo ran 0 time" in cases[3] and "child-reader" in cases[3]
    assert "read_repo ran 0 time" in cases[4] and "authority failed" in cases[4]
    assert "read_repo ran 1 time" in cases[5] and "got unverified" in cases[5]
    assert "bound to a different call" in cases[6] and "read_repo ran 1 time" in cases[6]
    assert "read_repo ran 0 time" in cases[7] and "no longer active" in cases[7]
    assert "read_repo ran 1 time" in cases[8] and cases[8].count("credential used") == 2
    assert "demo-token" not in result.stdout and "static-demo-token" not in result.stdout
