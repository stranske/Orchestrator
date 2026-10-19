"""CLI regression coverage for capability_propensity's global ``--dry-run`` flag.

The flag is only implemented by ``tick-evidence``.  Every other command must reject
it before opening a ledger, emitting a heartbeat, or performing a write.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

import cadence_registry
import capabilities
import capability_advisor
import capability_propensity as cp
import paths

# Smallest unique fragment that is ABSENT if the early reject is deleted.  No newlines, no
# indentation, no trailing colon — nothing a formatter owns.  count==1 is the uniqueness pin:
# a second site would let the first disappear without this needle going silent.
_DRY_RUN_GUARD_NEEDLE = 'if args.dry_run and args.command != "tick-evidence"'
_DRY_RUN_ERROR_NEEDLE = 'ap.error("--dry-run is supported only by tick-evidence")'
_USEFUL_EXPERIMENTS = (
    ("advice:dryrun0001", ""),
    ("run:keepalive:stranske/Counter_Risk#1146:codex", "production_run"),
    ("role:dry-run-review", "production_run"),
)


def _without_dry_run_guard(src: str) -> str:
    """Remove the actual guard structurally, independent of comments or indentation."""
    expected = ast.parse(_DRY_RUN_GUARD_NEEDLE + ": pass").body[0]
    guards = [
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.If) and ast.dump(node.test) == ast.dump(expected.test)
    ]
    assert len(guards) == 1, "break fixture must find exactly one early reject"
    guard = guards[0]
    assert ast.dump(guard.body[0]) == ast.dump(ast.parse(_DRY_RUN_ERROR_NEEDLE).body[0])
    lines = src.splitlines(keepends=True)
    return "".join(lines[: guard.lineno - 1] + lines[guard.end_lineno :])


def _seeded_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("offload")
    row.update(status="wired", matcher={"kind": "task_type"}, trigger_cadence="per task")
    capabilities.save({"offload": row}, ledger)
    return ledger


def _run(
    ledger: Path,
    *args: str,
    script: Path | None = None,
    ledger_source: Literal["explicit", "default", "environment"] = "explicit",
) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "ORCH_CAPABILITY_HEARTBEATS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        # A temp script copy lives outside MODULE_DIR; sibling imports still resolve from there.
        "PYTHONPATH": str(paths.MODULE_DIR),
        "ORCH_LOCAL_RUNTIME": str(ledger.parent),
    }
    # Every child stays in scratch state, including deliberate guard-removal controls.
    env.pop("ORCH_CAPABILITIES_PATH", None)
    if ledger_source == "environment":
        env["ORCH_CAPABILITIES_PATH"] = str(ledger)
        env["ORCH_LOCAL_RUNTIME"] = str(ledger.parent / "fallback-runtime")
    if ledger_source != "explicit":
        # Heartbeats must also remain harmless when callers have enabled them.
        env["ORCH_CAPABILITY_HEARTBEATS"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(script or (paths.MODULE_DIR / "capability_propensity.py")),
            *args,
            *(["--ledger", str(ledger)] if ledger_source == "explicit" else []),
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
    assert _without_dry_run_guard(src) != src


def test_useful_dry_run_writes_nothing(tmp_path: Path):
    """A real CLI invocation rejects dry-run before its otherwise-valid verdict can write.

    Acceptance contract (verified by break/restore, and by
    ``test_removing_dry_run_guard_restores_silent_write``): strip the early reject and this
    test FAILS because ``useful --dry-run`` returns 0 with ``recorded: true`` and mutates the
    scratch ledger; restore the reject and this test PASSES again.
    """
    for index, (experiment, source) in enumerate(_USEFUL_EXPERIMENTS):
        _assert_useful_dry_run_writes_nothing(
            tmp_path / str(index), experiment=experiment, source=source
        )


def _assert_useful_dry_run_writes_nothing(
    tmp_path: Path, *, experiment: str, source: str, script: Path | None = None
):
    """Share the exact invocation and assertions with the deliberate guard-removal check."""
    ledger = _seeded_ledger(tmp_path)
    before = ledger.read_bytes()
    useful_args = (
        "useful",
        "--capability",
        "offload",
        "--experiment",
        experiment,
        "--evidence",
        "the advisory review found the missing edge before merge",
        "--provenance",
        "machine_observed",
        *(["--source", source] if source else []),
    )

    ordinary = _run(ledger, *useful_args, script=script)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    recorded = json.loads(ordinary.stdout)
    assert recorded["recorded"] is True
    assert recorded["experiment"] == experiment
    assert recorded["source"] == (source or "capability_propensity")
    assert (
        ledger.read_bytes() != before
    ), "the control must prove this argv would write without dry-run"

    ledger = _seeded_ledger(tmp_path)
    before = ledger.read_bytes()

    result = _run(ledger, *useful_args, "--dry-run", script=script)

    assert ledger.read_bytes() == before, "useful --dry-run mutated the scratch ledger"
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--dry-run is supported only by tick-evidence" in result.stderr


def test_removing_dry_run_guard_restores_silent_write(tmp_path: Path):
    """Deliberate break: a guard-stripped copy of the CLI writes under ``useful --dry-run``.

    Proves ``test_useful_dry_run_writes_nothing`` would catch the production defect if the
    early reject were deleted.  The live module is never mutated; only a temp copy is stripped.
    """
    src = Path(cp.__file__).read_text()
    broken = _without_dry_run_guard(src)
    assert _DRY_RUN_GUARD_NEEDLE not in broken, "strip must remove the only guard site"
    script = tmp_path / "capability_propensity_unguarded.py"
    script.write_text(broken)

    for index, (experiment, source) in enumerate(_USEFUL_EXPERIMENTS):
        with pytest.raises(AssertionError, match="useful --dry-run mutated the scratch ledger"):
            _assert_useful_dry_run_writes_nothing(
                tmp_path / str(index), experiment=experiment, source=source, script=script
            )


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


def test_tick_evidence_dry_run_passes_record_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """Dry-run changes only recording; ledger and budget still reach the tick guard."""
    seen: dict[str, object] = {}

    def guarded(**kwargs):
        seen.update(kwargs)
        return {"recorded": False}

    monkeypatch.setattr(cp, "tick_evidence_guarded", guarded)

    ledger = tmp_path / "capabilities.json"
    for dry_run in (True, False):
        seen.clear()
        assert (
            cp.main(
                [
                    "tick-evidence",
                    "--json",
                    "--ledger",
                    str(ledger),
                    "--budget-seconds",
                    "0",
                    *(["--dry-run"] if dry_run else []),
                ]
            )
            == 0
        )
        assert seen == {"record": not dry_run, "budget_s": 0, "path": ledger}
        assert capsys.readouterr().out == '{\n  "recorded": false\n}\n'


@pytest.mark.parametrize("phase", ["baselined", "evaluated"])
def test_tick_evidence_dry_run_preserves_ledger_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, phase: str
):
    """Exercise the real evaluator on both first-seen and regenerated artifacts."""
    ledger = _seeded_ledger(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    artifact = state_dir / "offload.json"
    artifact.write_text(json.dumps({"findings": [{"id": "first"}]}))
    monkeypatch.setenv("ORCH_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "0")
    monkeypatch.delenv("ORCH_TICK_EVIDENCE_DISABLED", raising=False)
    monkeypatch.setitem(
        capability_advisor.SURFACE_BINDINGS, cp.TICK_SURFACE, {"offload": "scratch binding"}
    )
    monkeypatch.setitem(
        cadence_registry.STEP_BY_KEY,
        "offload",
        {"artifact": artifact.name, "cadence_days": 0},
    )
    monkeypatch.setitem(cp.TICK_FINDING_FIELDS, "offload", {"findings": ("id",)})
    argv = ["tick-evidence", "--json", "--ledger", str(ledger)]

    if phase == "evaluated":
        assert cp.main(argv) == 0
        baseline = json.loads(capsys.readouterr().out)
        assert [row["capability_id"] for row in baseline["baselined"]] == ["offload"]
        artifact.write_text(json.dumps({"findings": [{"id": "regenerated"}]}))
        regenerated_at = int(artifact.stat().st_mtime) + 2
        os.utime(artifact, (regenerated_at, regenerated_at))

    def snapshot():
        return {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    before = snapshot()
    for _ in range(2):
        assert cp.main([*argv, "--dry-run"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["consulted"] is True, report
        assert [row["capability_id"] for row in report[phase]] == ["offload"], report
        for counter in ("matches_recorded", "triggers_recorded", "verdicts_recorded"):
            assert report[counter] == 0, report
        assert snapshot() == before, "tick-evidence --dry-run wrote ledger or tick state"

    # The same artifact must still be pending after inspection, and a recording run consumes it.
    assert cp.main(argv) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert [row["capability_id"] for row in recorded[phase]] == ["offload"], recorded
    assert snapshot() != before, "the recording control must persist its tick state"
    if phase == "baselined":
        assert ledger.read_bytes() != before[ledger.relative_to(tmp_path)]
        assert recorded["triggers_recorded"] == 1, recorded


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
            "--corroboration",
            "the follow-up check contradicted the recorded verdict",
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
            "landed",
            "--evidence",
            "the hand-done delivery passed its acceptance checks",
            "--provenance",
            "machine_observed",
            "--corroboration",
            "the acceptance check confirmed the delivered result",
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
    """Reject valid writes, including late verdicts and the separate consult-outcome store."""
    for ledger_source in ("explicit", "default", "environment"):
        for exists in (False, True):
            store = tmp_path / ledger_source / ("existing" if exists else "absent")
            ledger = store / "capabilities.json"
            if exists:
                _seeded_ledger(store)
                if command_args[0] == "late-outcome":
                    experiment = command_args[command_args.index("--experiment") + 1]
                    assert cp.record_trigger("offload", experiment, path=ledger)
                    assert cp.record_usefulness(
                        "offload",
                        experiment,
                        useful=True,
                        evidence="the initial review found the missing check",
                        provenance="machine_observed",
                        path=ledger,
                    )
            before = {p.name: p.read_bytes() for p in store.iterdir()} if exists else None

            result = _run(ledger, *command_args, "--dry-run", ledger_source=ledger_source)

            assert result.returncode == 2, result.stdout + result.stderr
            assert "--dry-run is supported only by tick-evidence" in result.stderr
            if exists:
                assert {p.name: p.read_bytes() for p in store.iterdir()} == before
            else:
                assert not store.exists(), "dry-run created an absent ledger directory"

            if ledger_source == "explicit" and exists and command_args[0] in {
                "trigger",
                "useful",
                "late-outcome",
                "decline",
                "consult-outcome",
                "record-repair",
            }:
                ordinary = _run(ledger, *command_args)
                assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
                recorded = json.loads(ordinary.stdout)
                result_key = "attached" if command_args[0] == "late-outcome" else "recorded"
                assert recorded[result_key] is True, recorded
                assert {p.name: p.read_bytes() for p in store.iterdir()} != before, (
                    "the control must prove this argv writes without dry-run"
                )
