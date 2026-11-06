"""A live recurring trial is six read-only Codex sessions, or it writes nothing.

Transport acknowledgement requires four binding lines in the final message;
recurring-work quality needs a ``work_evidence=`` line quoting the rubric from
the frozen spec. Cost is token counts from the session, and the provider model
comes only from a ``response.completed`` OpenAI response object. The CLI's
``turn_context`` alone does not publish.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import feedback
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


def _seal(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    experiments = tmp_path / "experiments"
    experiment = experiments / EXP_ID
    experiment.mkdir(parents=True)
    spec_path = experiment / "spec.md"
    spec_path.write_text(SPEC, encoding="utf-8")
    (experiment / "strategy.json").write_text(
        json.dumps({"exp_id": EXP_ID, "spec_file": str(spec_path)}), encoding="utf-8"
    )
    (experiment / "meta.json").write_text(
        json.dumps({"spec_sha256": hashlib.sha256(SPEC.encode()).hexdigest()}),
        encoding="utf-8",
    )
    receipt = state / "capability-program" / "strategy-experiment.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"exp_id": EXP_ID, "status": "completed"}), encoding="utf-8")
    return state, experiments


def _required_lines(manifest: dict, request: dict) -> list[str]:
    packet = manifest["frozen_packet"]
    return [
        f"packet_hash={manifest['packet_hash']}",
        f"spec_sha256={packet['spec_sha256']}",
        f"profile_id={request['profile_id']}",
        f"run_id={request['run_id']}",
    ]


def _final_message_ack_and_work(manifest: dict, request: dict, *, include_work: bool) -> str:
    """Four acknowledgement lines, optional blank line and work_evidence (rubric quote)."""
    lines = _required_lines(manifest, request)
    body = "\n".join(lines)
    if not include_work:
        return body + "\n"
    rubric = trial._recurring_work_rubric_line(manifest["frozen_packet"]["frozen_spec"])
    return body + "\n\n" + f"work_evidence={rubric}\n"


SANITIZED_REAL_STREAM = """{"type":"response.completed","response":{"object":"response","id":"resp_fixture001","model":"gpt-6-2025-astra","usage":{"input_tokens":42,"output_tokens":7}}}
{"type":"turn.completed","usage":{"input_tokens":42,"output_tokens":7}}
"""


def test_sanitized_real_stream_fixture_yields_one_openai_response():
    events = trial._jsonl_objects(SANITIZED_REAL_STREAM)
    got = trial._one_provider_response(events)
    assert got["model"] == "gpt-6-2025-astra"
    assert str(got["id"]).startswith("resp_")
    assert got["usage"] == {"input_tokens": 42, "output_tokens": 7}


def test_echo_only_ack_lines_score_zero_recurring_work_quality(tmp_path):
    """Four transport lines without work_evidence= cannot pass recurring-work scoring."""
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
    request = manifest["requests"][0]
    ack, quality = trial._acknowledgement_score(
        manifest,
        request,
        "\n".join(_required_lines(manifest, request)) + "\n",
    )
    assert ack is True
    assert quality == 0.0


def _session(
    request: dict, *, model: str, tokens_in: int, tokens_out: int, final_text: str
) -> tuple[str, str]:
    response = {
        "type": "response.completed",
        "response": {
            "object": "response",
            "id": f"resp_{request['launch_ordinal']}",
            "model": model,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        },
    }
    turn = {
        "type": "turn.completed",
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }
    return json.dumps(response) + "\n" + json.dumps(turn) + "\n", final_text


def test_read_only_argv_cannot_select_a_workspace_write_sandbox():
    argv = trial.codex_read_only_argv(
        Path("/usr/bin/codex"),
        {"requested_model": "gpt-6-astra"},
        "prompt",
        Path("/tmp/final.txt"),
    )
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "workspace-write" not in argv


def test_run_current_records_measured_quality_cost_and_response_identity(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    full_profile = trial.CURRENT_TRIAL_PROFILE_IDS[0]
    partial_profile = trial.CURRENT_TRIAL_PROFILE_IDS[1]

    # Same seed and clock as the runner, so the echoed lines name its packet.
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )

    def execute(request, prompt):
        assert "Frozen specification:" in prompt
        assert SPEC in prompt
        assert "workspace-write" not in prompt
        assert "work_evidence=" in prompt
        include_work = request["profile_id"] == full_profile
        session, final_text = _session(
            request,
            model=request["requested_model"],
            tokens_in=10 * int(request["launch_ordinal"]),
            tokens_out=int(request["launch_ordinal"]),
            final_text=_final_message_ack_and_work(manifest, request, include_work=include_work),
        )
        return session, final_text, "codex-cli 0.153.2"

    published = trial.run_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
        state_path=tmp_path / "outside-state.json",
        artifact_dir=tmp_path / "artifacts",
        execute=execute,
    )
    summary = json.loads((state / "capability-program" / "profile-trial.json").read_text())
    assert published["attempt_count"] == 6
    assert published["brain_ingest_enabled"] is True
    assert summary["identity_verified"] is True
    assert summary["exp_id"] == EXP_ID
    assert summary["work_shape"] == "strategy-experiment-frozen-spec"
    assert (summary["profile_count"], summary["instance_count"]) == (2, 6)
    assert summary["quality_by_profile"][full_profile] == 1.0
    assert summary["quality_by_profile"][partial_profile] == 0.0
    for profile_id in trial.CURRENT_TRIAL_PROFILE_IDS:
        attempts = [row for row in manifest["requests"] if row["profile_id"] == profile_id]
        assert summary["cost_tokens_by_profile"][profile_id] == {
            "tokens_in": sum(10 * int(row["launch_ordinal"]) for row in attempts),
            "tokens_out": sum(int(row["launch_ordinal"]) for row in attempts),
        }
        assert summary["provider_resolved_identity_by_profile"][profile_id] == {
            "provider": "openai",
            "model": attempts[0]["requested_model"],
        }
    artifact = next((tmp_path / "artifacts").glob("identity-*.json"))
    body = json.loads(artifact.read_text(encoding="utf-8"))
    assert body["object"] == "response"
    assert str(body["response_id"]).startswith("resp_")
    assert body["provider_resolved_model"] in {
        row["requested_model"] for row in manifest["requests"]
    }
    assert "turn_context" not in artifact.read_text(encoding="utf-8")


def test_cli_turn_context_does_not_publish_a_trial(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")

    def execute(request, _prompt):
        # The configured model matches the profile. That is still not a provider
        # response, and it must not be copied into provider_resolved_model.
        session = json.dumps(
            {"type": "turn_context", "payload": {"model": request["requested_model"]}}
        )
        return session + "\n", "packet_hash=ignored\n", "codex-cli 0.153.2"

    with pytest.raises(ValueError, match="provider_resolved_identity_unavailable"):
        trial.run_current_recurring_trial(
            orchestrator,
            workflows,
            seed=14,
            now=1000,
            capacity_state="ok",
            state_dir=state,
            experiments_root=experiments,
            state_path=tmp_path / "outside-state.json",
            artifact_dir=tmp_path / "artifacts",
            execute=execute,
        )
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "quarantine-feedback.db").exists()
    assert list((tmp_path / "artifacts").glob("identity-*.json")) == []


def test_missing_usage_does_not_publish(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )

    def execute(request, _prompt):
        final_text = _final_message_ack_and_work(manifest, request, include_work=True)
        response = {
            "type": "response.completed",
            "response": {
                "object": "response",
                "id": f"resp_{request['launch_ordinal']}",
                "model": request["requested_model"],
            },
        }
        return json.dumps(response) + "\n", final_text, "codex-cli 0.153.2"

    with pytest.raises(ValueError, match="tokens_in"):
        trial.run_current_recurring_trial(
            orchestrator,
            workflows,
            seed=14,
            now=1000,
            capacity_state="ok",
            state_dir=state,
            experiments_root=experiments,
            artifact_dir=tmp_path / "artifacts",
            execute=execute,
        )
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "quarantine-feedback.db").exists()


def test_run_current_cli_refuses_a_missing_runner_without_writing(tmp_path, capsys):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    status = trial.main(
        [
            "run-current",
            "--orchestrator-root",
            str(orchestrator),
            "--workflows-root",
            str(workflows),
            "--state",
            str(tmp_path / "outside-state.json"),
            "--state-dir",
            str(state),
            "--experiments-root",
            str(experiments),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--codex-bin",
            str(tmp_path / "missing-codex"),
            "--capacity-state",
            "ok",
            "--seed",
            "14",
        ]
    )
    assert status == 2
    assert "absent" in capsys.readouterr().err
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "outside-state.json").exists()
