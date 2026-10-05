#!/usr/bin/env python3
"""Reproduce issue 417's named deliberate-break controls without editing checkout source."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "src" if (ROOT / "src").is_dir() else ROOT
TEST_FILE = ROOT / "tests" / "test_role_prompt_offering.py"

# Exact anchors fail closed when source changes, rather than measuring a no-op mutation.
CONTROLS = (
    (
        "capability_advisor.py",
        "test_research_program_surface_is_declared_and_binds_role_prompt",
        'CONSULT_SITES: dict[str, dict] = {\n    "research-program": {\n'
        '        "caller": "~/.codex/automations/research-program/driver.py",\n'
        '        "how": "the driver consults research-program before filing units '
        'that author issue batches",\n'
        "    },\n",
        "CONSULT_SITES: dict[str, dict] = {\n",
    ),
    (
        "capability_propensity.py",
        "test_wrong_moment_declines_never_demote",
        '    "wrong_moment": {\n        "demotable": False,',
        '    "wrong_moment": {\n        "demotable": True,',
    ),
    (
        "roles.py",
        "test_a_batch_counts_once_against_the_cycle_cap",
        "    results = []\n    for item in items:\n        item_args = common | item\n",
        "    results = []\n    for item_index, item in enumerate(items):\n"
        '        if selector and selector["invoked"] and item_index:\n'
        '            _ROLE_INVOCATION_COUNTS["prompt"] += 1\n'
        "        item_args = common | item\n",
    ),
)


def run_named_test(directory: Path, test_name: str, phase: str) -> dict:
    """Require one named assertion failure or one pass; import/usage errors are not proof."""
    if phase not in {"baseline", "broken", "restored"}:
        raise ValueError(f"Unknown control phase: {phase}")
    report = directory / f"{phase}.xml"
    # A retry must prove this child ran, even if an earlier attempt left a valid report.
    report.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-B",  # Never reuse a mutated module's bytecode after restoring its source.
        "-m",
        "pytest",
        f"{TEST_FILE}::{test_name}",
        "-q",
        "-m",
        "not slow",
        "-o",
        "addopts=",
        "-o",
        f"pythonpath={directory} {MODULES} {ROOT / 'tests'} {ROOT}",
        f"--junitxml={report}",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    expected_exit = 1 if phase == "broken" else 0
    if not report.is_file():
        raise RuntimeError(f"{test_name} ({phase}): no pytest report\n{output}")
    cases = list(ET.parse(report).iter("testcase"))
    valid = (
        result.returncode == expected_exit
        and len(cases) == 1
        and cases[0].get("name") == test_name
        and cases[0].find("error") is None
        and cases[0].find("skipped") is None
    )
    if valid:
        failures = cases[0].findall("failure")
        valid = (
            len(failures) == 1 and "AssertionError" in (failures[0].text or "")
            if phase == "broken"
            else not failures
        )
    if not valid:
        raise RuntimeError(f"{test_name} ({phase}): unexpected pytest result\n{output}")
    return {"phase": phase, "exit": result.returncode, "output": output}


def verify_controls() -> list[dict]:
    receipts = []
    for module_name, test_name, anchor, replacement in CONTROLS:
        source = MODULES / module_name
        original = source.read_bytes()
        text = original.decode("utf-8")
        if text.count(anchor) != 1:
            raise RuntimeError(f"{module_name}: mutation anchor must occur exactly once")
        with tempfile.TemporaryDirectory(prefix="role-offering-control-") as scratch:
            directory = Path(scratch)
            private_module = directory / module_name
            private_module.write_bytes(original)
            try:
                baseline = run_named_test(directory, test_name, "baseline")
                private_module.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")
                broken = run_named_test(directory, test_name, "broken")
            finally:
                private_module.write_bytes(original)
                if source.read_bytes() != original:
                    raise RuntimeError(f"{module_name}: checkout source changed during control")
            restored = run_named_test(directory, test_name, "restored")
            if source.read_bytes() != original:
                raise RuntimeError(f"{module_name}: checkout source changed after restoration")
            receipts.append(
                {"module": module_name, "test": test_name, "runs": [baseline, broken, restored]}
            )
    return receipts


def main() -> int:
    try:
        receipts = verify_controls()
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
