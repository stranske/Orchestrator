"""Capture the requested mutations, restoring each source file before its GREEN rerun."""

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
CASES = [
    (
        "persisted-verdict",
        "src/experiment_producer.py",
        '    if state.get("trial_verdict") in {"pass", "fail"}:\n        return state["trial_verdict"]\n',
        "",
        "tests/test_experiment_producer.py::test_verdict_flake_does_not_overwrite_pass",
    ),
    (
        "stranded-experiment",
        "src/exp_abcd.py",
        '    spec_path = edir / "spec.md"\n',
        '    if os.environ.get("ORCH_EXPERIMENT_PRODUCER", "0") != "1":\n'
        '        return {**result, "status": "producer_disabled"}\n'
        '    spec_path = edir / "spec.md"\n',
        "tests/test_experiment_producer.py::test_producer_judges_are_two_neutral_agents_and_delivery_bypasses_synthesis[0]",
    ),
    (
        "coverage-saturation",
        "src/experiment_producer.py",
        "        if not candidate_cells:\n",
        "        if not drainable:\n",
        "tests/test_experiment_producer.py::test_shed_agent_does_not_latch_firing_launch[fresh_warn]",
    ),
]


def run(test):
    argv = [sys.executable, "-m", "pytest", "-q", "--tb=short", test]
    proc = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=60)
    return (
        proc.returncode,
        "$ " + " ".join(argv) + "\n" + proc.stdout + proc.stderr + f"\nEXIT={proc.returncode}\n",
    )


for name, filename, before, after, test in CASES:
    path = ROOT / filename
    original = path.read_bytes()
    source = original.decode()
    assert source.count(before) == 1, (name, "ambiguous mutation")
    try:
        path.write_text(source.replace(before, after, 1))
        red_rc, red = run(test)
        (OUT / f"{name}-red.log").write_text(red)
        assert red_rc == 1, (name, red)
    finally:
        path.write_bytes(original)
    assert path.read_bytes() == original
    green_rc, green = run(test)
    (OUT / f"{name}-green.log").write_text(
        f"RESTORED_SHA256={hashlib.sha256(original).hexdigest()}\n" + green
    )
    assert green_rc == 0, (name, green)
    print(f"{name}: RED=1 GREEN=0; source restored byte-for-byte", flush=True)
