"""Run the diagram-contract JS test in the ordinary Python Gate matrix.

tests/test_orchestrator_loop_svg.js guards orchestrator-loop.svg (one top-level description, no
conflict markers). Nothing executed it, so main sat red on it unnoticed. It is JavaScript, so this
wraps it the way tests/test_mirror_product_witnesses.py wraps the other Node witnesses; a GitHub
runner ships Node, and so does the owner's machine.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_orchestrator_loop_svg_contract():
    result = subprocess.run(
        ["node", "--test", "tests/test_orchestrator_loop_svg.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
