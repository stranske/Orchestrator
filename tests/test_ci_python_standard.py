"""Keep workflow runtime pins on the owner's Python 3.14/3.13 standard."""

import re
from pathlib import Path


def test_workflow_python_pins_follow_standard():
    pin_line = re.compile(
        r"^\s*(?:-\s+)?(?:python-version|python-versions|primary-python-version|"
        r"PYTHON_VERSION|COVERAGE_REFERENCE_RUNTIME)\s*:\s*(.*)$"
    )
    violations = []
    root = Path(__file__).resolve().parents[1]
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    assert workflows, "No workflow YAML files found"
    for workflow in workflows:
        for lineno, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = pin_line.match(line)
            if match is None:
                continue
            # Handle scalars, YAML arrays and JSON arrays wrapped in YAML quotes.
            value = match.group(1).split("#", 1)[0]
            tokens = re.split(r"[\s,\[\]\"']+", value)
            if "3.12" in tokens:
                violations.append(f"{workflow.name}:{lineno}: {value.strip()}")

    assert not violations, "Python 3.12 workflow runtime pins:\n" + "\n".join(violations)
