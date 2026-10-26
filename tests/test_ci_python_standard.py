"""Keep workflow runtime pins on the owner's Python 3.14/3.13 standard."""

import re
from pathlib import Path


def _python_312_pins(text):
    pin_line = re.compile(
        r"^\s*(?:-\s+)?(?:python-version|python-versions|primary-python-version|"
        r"PYTHON_VERSION|COVERAGE_REFERENCE_RUNTIME)\s*:\s*(.*)$"
    )
    violations = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = pin_line.match(line)
        if match is None:
            continue
        # Handle scalars, YAML arrays and JSON arrays wrapped in YAML quotes.
        value = match.group(1).split("#", 1)[0]
        tokens = re.split(r"[\s,\[\]\"']+", value)
        if any(re.fullmatch(r"3\.12(?:\.\d+)?", token) for token in tokens):
            violations.append(f"{lineno}: {value.strip()}")
    return violations


def _workflow_python_pin_violations(workflow_dir):
    workflows = sorted(set(workflow_dir.glob("*.yml")) | set(workflow_dir.glob("*.yaml")))
    assert workflows, "No workflow YAML files found"
    return [
        f"{workflow.name}:{violation}"
        for workflow in workflows
        for violation in _python_312_pins(workflow.read_text(encoding="utf-8"))
    ]


def test_workflow_python_pins_follow_standard():
    root = Path(__file__).resolve().parents[1]
    violations = _workflow_python_pin_violations(root / ".github" / "workflows")

    assert not violations, "Python 3.12 workflow runtime pins:\n" + "\n".join(violations)


def test_coverage_runtime_is_the_primary_runtime():
    root = Path(__file__).resolve().parents[1]
    workflow_dir = root / ".github" / "workflows"
    workflows = sorted(set(workflow_dir.glob("*.yml")) | set(workflow_dir.glob("*.yaml")))
    gates = [workflow for workflow in workflows if workflow.stem == "pr-00-gate"]
    assert gates, "No Gate workflow YAML found"
    for gate in gates:
        text = gate.read_text(encoding="utf-8")
        for key in (
            "coverage-python-version",
            "primary-python-version",
            "COVERAGE_REFERENCE_RUNTIME",
        ):
            values = re.findall(rf"^\s*{key}\s*:\s*([^\n]+)$", text, re.MULTILINE)
            pins = [value.split("#", 1)[0].strip().strip("\"'") for value in values]
            assert pins == ["3.14"], f"{gate.name}: {key} must be 3.14, got {pins}"


def test_yaml_workflow_python_pins_are_scanned(tmp_path):
    (tmp_path / "b.yml").write_text("python-version: '3.12'\n", encoding="utf-8")
    (tmp_path / "a.yaml").write_text("python-version: '3.12'\n", encoding="utf-8")

    assert _workflow_python_pin_violations(tmp_path) == [
        "a.yaml:1: '3.12'",
        "b.yml:1: '3.12'",
    ]


def test_python_312_family_pins_and_negative_controls():
    cases = [
        ("'3.12.1'", True),
        ("'3.12'", True),
        ("'3.120'", False),
        ("'3.1'", False),
        ("'3.13'", False),
        ("'3.14'", False),
        ('["3.12.4", "3.13"]', True),
        ('\'["3.13", "3.14"]\'', False),
    ]
    for key in (
        "python-version",
        "python-versions",
        "primary-python-version",
        "PYTHON_VERSION",
        "COVERAGE_REFERENCE_RUNTIME",
    ):
        for value, expected in cases:
            assert bool(_python_312_pins(f"  {key}: {value}\n")) is expected, (key, value)
