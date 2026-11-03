"""Execute the real offline measurement CLI in the two shipped layouts."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("layout", ["source", "flat"])
def test_actual_measurement_cli_has_no_ambient_import_dependency(tmp_path, layout):
    repository = Path(__file__).resolve().parents[1]
    source = repository / "src"
    if not (source / "closer_stage_feedback.py").is_file():
        source = repository
    root = tmp_path / layout
    modules = root / "src" if layout == "source" else root
    modules.mkdir(parents=True)
    for module in source.glob("*.py"):
        shutil.copy2(module, modules / module.name)
    script = root / "scripts" / "measure_closer_acceptance.py"
    script.parent.mkdir()
    shutil.copy2(repository / "scripts" / script.name, script)
    workload = root / "input.json"
    workload.write_bytes(
        (repository / "tests/fixtures/closer-category46/representative-workload.json").read_bytes()
    )
    home = root / "empty-home"
    home.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(workload)],
        cwd=root,
        env={"PATH": os.defpath, "HOME": str(home), "ORCH_MODEL_PROBE": "0"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["evidence_kind"] == "synthetic_fixture"
    assert report["summary"]["observed_branches"]["denominator"] == 5
    assert report["baseline"]["live_savings_usd"] is None
    assert report["baseline"]["live_model_call_avoidance"] is None
    assert report["authority"] is False
