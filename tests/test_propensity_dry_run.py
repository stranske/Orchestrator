"""CLI regression coverage for capability_propensity's global ``--dry-run`` flag.

The flag is only implemented by ``tick-evidence``.  Every other command must reject
it before opening a ledger, emitting a heartbeat, or performing a write.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import capabilities
import capability_propensity as cp
import paths


def _seeded_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("offload")
    row.update(status="wired", matcher={"kind": "task_type"}, trigger_cadence="per task")
    capabilities.save({"offload": row}, ledger)
    return ledger


def _run(ledger: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "ORCH_CAPABILITY_HEARTBEATS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [
            sys.executable,
            str(paths.MODULE_DIR / "capability_propensity.py"),
            *args,
            "--ledger",
            str(ledger),
        ],
        cwd=paths.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_useful_dry_run_writes_nothing(tmp_path: Path):
    """A real CLI invocation rejects dry-run before its otherwise-valid verdict can write."""
    ledger = _seeded_ledger(tmp_path)
    before = ledger.read_bytes()
    useful_args = (
        "useful",
        "--capability",
        "offload",
        "--experiment",
        "advice:dryrun0001",
        "--evidence",
        "the advisory review found the missing edge before merge",
        "--provenance",
        "machine_observed",
    )

    ordinary = _run(ledger, *useful_args)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert (
        ledger.read_bytes() != before
    ), "the control must prove this argv would write without dry-run"

    ledger = _seeded_ledger(tmp_path)
    before = ledger.read_bytes()

    result = _run(ledger, *useful_args, "--dry-run")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--dry-run is supported only by tick-evidence" in result.stderr
    assert ledger.read_bytes() == before


def test_useful_dry_run_does_not_create_an_absent_ledger(tmp_path: Path):
    ledger = tmp_path / "absent" / "capabilities.json"

    result = _run(
        ledger,
        "useful",
        "--capability",
        "offload",
        "--experiment",
        "advice:dryrun-absent",
        "--evidence",
        "the unsupported flag must stop before the ledger is opened",
        "--provenance",
        "machine_observed",
        "--dry-run",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--dry-run is supported only by tick-evidence" in result.stderr
    assert not ledger.parent.exists()


def test_tick_evidence_dry_run_passes_record_false(monkeypatch: pytest.MonkeyPatch, capsys):
    seen: dict[str, object] = {}

    def guarded(**kwargs):
        seen.update(kwargs)
        return {"recorded": False}

    monkeypatch.setattr(cp, "tick_evidence_guarded", guarded)

    assert cp.main(["tick-evidence", "--dry-run", "--json"]) == 0
    assert seen["record"] is False
    assert capsys.readouterr().out == '{\n  "recorded": false\n}\n'


@pytest.mark.parametrize(
    "command_args",
    [
        ("trigger", "--capability", "offload", "--experiment", "advice:dryrun0002"),
        (
            "useful",
            "--capability",
            "offload",
            "--experiment",
            "advice:dryrun0003",
            "--evidence",
            "a valid verdict would mutate the scratch ledger",
            "--provenance",
            "machine_observed",
        ),
        (
            "late-outcome",
            "--capability",
            "offload",
            "--experiment",
            "advice:dryrun0004",
            "--direction",
            "refutes",
            "--evidence",
            "the outcome contradicted the earlier verdict",
            "--provenance",
            "machine_observed",
        ),
        (
            "decline",
            "--capability",
            "offload",
            "--experiment",
            "advice:dryrun0005",
            "--reason",
            "the target has no usable landing zone",
        ),
        (
            "consult-outcome",
            "--experiment",
            "advice:dryrun0006",
            "--direction",
            "refutes",
            "--evidence",
            "the consult did not change the delivery",
            "--provenance",
            "machine_observed",
        ),
        ("find", "--defect", "dry-run guard", "--artifact", "tests/test_propensity_dry_run.py"),
        (
            "record-repair",
            "--capability",
            "offload",
            "--fix",
            "reject unsupported dry-run",
            "--artifact",
            "tests/test_propensity_dry_run.py",
        ),
        (
            "reoffer",
            "--capability",
            "offload",
            "--experiment",
            "advice:dryrun0007",
            "--kind",
            "wrong_match",
        ),
        ("migrate-fixture-provenance",),
        ("detect", "--apply"),
    ],
)
def test_writing_commands_reject_dry_run_before_mutation(
    tmp_path: Path, command_args: tuple[str, ...]
):
    ledger = _seeded_ledger(tmp_path)
    before = ledger.read_bytes()

    result = _run(ledger, *command_args, "--dry-run")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--dry-run is supported only by tick-evidence" in result.stderr
    assert ledger.read_bytes() == before
