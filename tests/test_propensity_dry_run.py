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

# Smallest unique fragment that is ABSENT if the early reject is deleted.  No newlines, no
# indentation, no trailing colon — nothing a formatter owns.  count==1 is the uniqueness pin:
# a second site would let the first disappear without this needle going silent.
_DRY_RUN_GUARD_NEEDLE = 'if args.dry_run and args.command != "tick-evidence"'
_DRY_RUN_ERROR_NEEDLE = 'ap.error("--dry-run is supported only by tick-evidence")'
_DRY_RUN_GUARD_BLOCK = (
    '    if args.dry_run and args.command != "tick-evidence":\n'
    "        # This is intentionally a parser error rather than a per-command convention.  "
    "The global\n"
    "        # flag used to be accepted by every recording command but only tick-evidence "
    "honored it,\n"
    "        # which made a dry-run spelling silently mutate the ledger.  Reject before any "
    "command can\n"
    "        # load a store, emit a heartbeat, or reach a writer.\n"
    f"        {_DRY_RUN_ERROR_NEEDLE}\n"
)


def _seeded_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("offload")
    row.update(status="wired", matcher={"kind": "task_type"}, trigger_cadence="per task")
    capabilities.save({"offload": row}, ledger)
    return ledger


def _run(ledger: Path, *args: str, script: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "ORCH_CAPABILITY_HEARTBEATS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        # A temp script copy lives outside MODULE_DIR; sibling imports still resolve from there.
        "PYTHONPATH": str(paths.MODULE_DIR),
    }
    return subprocess.run(
        [
            sys.executable,
            str(script or (paths.MODULE_DIR / "capability_propensity.py")),
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


def test_dry_run_guard_is_wired_exactly_once():
    """Pin: the early reject exists at exactly one site.  Uniqueness decays quietly with `in`."""
    src = Path(cp.__file__).read_text()
    assert src.count(_DRY_RUN_GUARD_NEEDLE) == 1, src.count(_DRY_RUN_GUARD_NEEDLE)
    assert src.count(_DRY_RUN_ERROR_NEEDLE) == 1, src.count(_DRY_RUN_ERROR_NEEDLE)
    assert _DRY_RUN_GUARD_BLOCK in src


def test_useful_dry_run_writes_nothing(tmp_path: Path):
    """A real CLI invocation rejects dry-run before its otherwise-valid verdict can write.

    Acceptance contract (verified by break/restore, and by
    ``test_removing_dry_run_guard_restores_silent_write``): strip the early reject and this
    test FAILS because ``useful --dry-run`` returns 0 with ``recorded: true`` and mutates the
    scratch ledger; restore the reject and this test PASSES again.
    """
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


def test_removing_dry_run_guard_restores_silent_write(tmp_path: Path):
    """Deliberate break: a guard-stripped copy of the CLI writes under ``useful --dry-run``.

    Proves ``test_useful_dry_run_writes_nothing`` would catch the production defect if the
    early reject were deleted.  The live module is never mutated; only a temp copy is stripped.
    """
    src = Path(cp.__file__).read_text()
    assert _DRY_RUN_GUARD_BLOCK in src, "break fixture is stale: guard block not found"
    broken = src.replace(_DRY_RUN_GUARD_BLOCK, "", 1)
    assert _DRY_RUN_GUARD_NEEDLE not in broken, "strip must remove the only guard site"
    script = tmp_path / "capability_propensity_unguarded.py"
    script.write_text(broken)

    ledger = _seeded_ledger(tmp_path)
    before = ledger.read_bytes()
    result = _run(
        ledger,
        "useful",
        "--capability",
        "offload",
        "--experiment",
        "advice:dryrun-break",
        "--evidence",
        "unguarded dry-run must reproduce the silent ledger write",
        "--provenance",
        "machine_observed",
        "--dry-run",
        script=script,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"recorded": true' in result.stdout
    assert ledger.read_bytes() != before, "unguarded dry-run must mutate the scratch ledger"


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
