"""Keep workflow runtime pins on the owner's Python 3.14/3.13 standard."""

from pathlib import Path

import yaml


def test_workflow_python_pins_follow_standard():
    pin_keys = {
        "python-version",
        "python-versions",
        "primary-python-version",
        "PYTHON_VERSION",
        "COVERAGE_REFERENCE_RUNTIME",
    }
    violations = []

    def check_pins(value, location, is_pin=False):
        if isinstance(value, dict):
            for key, child in value.items():
                check_pins(child, f"{location}.{key}", key in pin_keys)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check_pins(child, f"{location}[{index}]", is_pin)
        elif is_pin:
            # Reusable-workflow matrices are JSON arrays inside YAML strings.
            if isinstance(value, str) and value.lstrip().startswith("["):
                check_pins(yaml.safe_load(value), location, True)
            elif str(value) == "3.12":
                violations.append(f"{location}: {value!r}")

    root = Path(__file__).resolve().parents[1]
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    assert workflows, "No workflow YAML files found"
    for workflow in workflows:
        check_pins(yaml.safe_load(workflow.read_text(encoding="utf-8")), workflow.name)

    assert not violations, "Python 3.12 workflow runtime pins:\n" + "\n".join(violations)
