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


def test_generation_witness_does_not_feed_its_program_through_a_shell_pipe():
    from test_orchestrate_no_heredoc import _logical_code_lines

    script = ROOT / "tests/test_mirror_generation_imports.sh"
    assert not any("<<" in line for line in _logical_code_lines(script.read_text()))
    helper = ROOT / "tests/mirror_generation_imports_witness.py"
    assert helper.is_file()
    assert '"$repo/tests/mirror_generation_imports_witness.py" "$repo"' in script.read_text()
