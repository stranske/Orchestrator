"""The two-by-three plan reuses the strategy experiment's frozen spec, or it refuses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import model_profile_trial as trial

SPEC = "Acceptance criteria: keep the frozen recurring-work body unchanged.\n"
EXP_ID = "strategy434-20261006"


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    orchestrator = tmp_path / "orchestrator"
    workflows = tmp_path / "workflows"
    orchestrator.mkdir()
    workflows.mkdir()
    (orchestrator / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workflows / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    return orchestrator, workflows


def _seal(
    tmp_path: Path,
    spec: str = SPEC,
    *,
    spec_file: Path | None = None,
    spec_sha256: str | None = None,
    exp_id: str = EXP_ID,
    write_meta: bool = True,
) -> tuple[Path, Path]:
    state = tmp_path / "state"
    experiments = tmp_path / "experiments"
    experiment = experiments / exp_id
    experiment.mkdir(parents=True)
    spec_path = experiment / "spec.md"
    spec_path.write_text(spec, encoding="utf-8")
    recorded = spec_path if spec_file is None else spec_file
    (experiment / "strategy.json").write_text(
        json.dumps({"exp_id": exp_id, "spec_file": str(recorded)}), encoding="utf-8"
    )
    if write_meta:
        sealed = (
            hashlib.sha256(spec.encode("utf-8")).hexdigest() if spec_sha256 is None else spec_sha256
        )
        (experiment / "meta.json").write_text(json.dumps({"spec_sha256": sealed}), encoding="utf-8")
    receipt = state / "capability-program" / "strategy-experiment.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"exp_id": exp_id, "status": "completed"}), encoding="utf-8")
    return state, experiments


def test_current_plan_binds_two_profiles_and_the_frozen_spec(tmp_path):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    trial.validate_trial_manifest(manifest)
    packet = manifest["frozen_packet"]
    assert packet["frozen_spec"] == SPEC
    assert packet["exp_id"] == EXP_ID
    assert packet["work_shape"] == "strategy-experiment-frozen-spec"
    assert packet["spec_sha256"] == "sha256:" + hashlib.sha256(SPEC.encode("utf-8")).hexdigest()
    assert manifest["selected_profile_ids"] == list(trial.CURRENT_TRIAL_PROFILE_IDS)
    assert manifest["instances_per_profile"] == 3
    assert len(manifest["requests"]) == 6
    for profile_id in trial.CURRENT_TRIAL_PROFILE_IDS:
        assert sum(row["profile_id"] == profile_id for row in manifest["requests"]) == 3
    assert not (state / "capability-program" / "profile-trial.json").exists()


def test_absent_receipt_stub_mismatch_and_foreign_spec_are_refused(tmp_path):
    orchestrator, workflows = _sources(tmp_path)
    with pytest.raises(trial.RecurringWorkUnavailable, match="receipt is absent"):
        trial.build_current_recurring_trial(
            orchestrator,
            workflows,
            state_dir=tmp_path / "missing-state",
            experiments_root=tmp_path / "missing-experiments",
        )

    state, experiments = _seal(tmp_path / "stub", spec="[SPEC_PROVENANCE: MISSING_SPEC_STUB]\n")
    with pytest.raises(trial.RecurringWorkUnavailable, match="missing-spec stub"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)

    state, experiments = _seal(tmp_path / "sha", spec_sha256="0" * 64)
    with pytest.raises(trial.RecurringWorkUnavailable, match="spec_sha256"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)

    foreign = tmp_path / "foreign" / "other.md"
    foreign.parent.mkdir()
    foreign.write_text(SPEC, encoding="utf-8")
    state, experiments = _seal(tmp_path / "foreign-spec", spec_file=foreign)
    with pytest.raises(trial.RecurringWorkUnavailable, match="not the frozen spec.md"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)
    assert not (state / "capability-program" / "profile-trial.json").exists()


def test_prepare_current_cli_writes_the_plan_and_not_the_program_summary(tmp_path, capsys):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    output = tmp_path / "outside" / "manifest.json"
    status = trial.main(
        [
            "prepare-current",
            "--orchestrator-root",
            str(orchestrator),
            "--workflows-root",
            str(workflows),
            "--output",
            str(output),
            "--seed",
            "14",
            "--capacity-state",
            "ok",
            "--state-dir",
            str(state),
            "--experiments-root",
            str(experiments),
        ]
    )
    assert status == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert printed["trial_id"] == manifest["trial_id"]
    assert manifest["frozen_packet"]["frozen_spec"] == SPEC
    assert len(manifest["requests"]) == 6
    assert not (state / "capability-program" / "profile-trial.json").exists()


def test_prepare_current_cli_refuses_a_missing_spec_without_writing(tmp_path, capsys):
    orchestrator, workflows = _sources(tmp_path)
    output = tmp_path / "outside" / "manifest.json"
    status = trial.main(
        [
            "prepare-current",
            "--orchestrator-root",
            str(orchestrator),
            "--workflows-root",
            str(workflows),
            "--output",
            str(output),
            "--state-dir",
            str(tmp_path / "state"),
            "--experiments-root",
            str(tmp_path / "experiments"),
        ]
    )
    assert status == 2
    assert "receipt is absent" in capsys.readouterr().err
    assert not output.exists()
