"""Run the retained deployment witnesses in the ordinary Python Gate matrix."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def witness(command):
    env = dict(os.environ, PYTHON=sys.executable)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_deployment_evidence_collector_acceptance():
    witness(["node", "--test", "tests/test_capture_mirror_deployment_evidence.js"])


def test_paired_generation_import_witness():
    witness(["bash", "tests/test_mirror_generation_imports.sh"])
