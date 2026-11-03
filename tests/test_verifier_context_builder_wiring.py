"""Keep the actual consumer's full collector boundary in the normal Python Gate."""

import subprocess
from pathlib import Path


def test_full_verifier_context_builder_contract():
    result = subprocess.run(
        ["node", "--test", "tests/test_verifier_context_builder_contract.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
