"""A live recurring trial is six read-only Codex sessions, or it writes nothing.

Transport acknowledgement requires four binding lines in the final message;
recurring-work quality needs the computed line numbers and word counts in
``work_evidence=``. Cost is token counts from the session. An unverified model claim comes from a
``response.completed`` OpenAI response object or from ``turn.completed``'s
``server_model`` field. The CLI's ``turn_context``, and a generic ``model``
on ``turn.completed``, do not publish. The prompt must not contain the
completed ``work_evidence=`` answer. Neither session shape establishes trusted
provider provenance; both retain measurements without a verified-ingest marker.
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
    """Four acknowledgement lines and, optionally, the answer for SPEC."""
    lines = _required_lines(manifest, request)
    body = "\n".join(lines)
    if not include_work:
        return body + "\n"
    return body + "\n\nwork_evidence=1:6\n"


SANITIZED_REAL_STREAM = """{"type":"response.completed","response":{"object":"response","id":"resp_fixture001","model":"gpt-6-2025-astra","usage":{"input_tokens":42,"output_tokens":7}}}
{"type":"turn.completed","usage":{"input_tokens":42,"output_tokens":7}}
"""


def test_sanitized_real_stream_fixture_yields_one_openai_response():
    events = trial._jsonl_objects(SANITIZED_REAL_STREAM)
    got = trial._one_provider_response(events)
    assert got["model"] == "gpt-6-2025-astra"
    assert str(got["id"]).startswith("resp_")
    assert got["usage"] == {"input_tokens": 42, "output_tokens": 7}


def test_prompt_names_the_rubric_without_embedding_the_answer(tmp_path):
    """The prompt gives the algorithm and frozen spec without giving the result."""
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
    prompt = trial.recurring_trial_prompt(manifest, manifest["requests"][0])
    head, marker, tail = prompt.partition("Frozen specification:\n")
    assert marker == "Frozen specification:\n"
    assert tail == SPEC
    assert "work_evidence=1:6" not in head
    assert "line_number:word_count" in head
    assert "one to six # characters followed by whitespace or end of line" in head
    assert "work_evidence=" in head
    assert "workspace-write" not in prompt


@pytest.mark.parametrize(
    ("spec", "answer", "score"),
    [
        ("\nKeep receipts.\nKeep the logs.\nIgnore this line.", "2:2,3:3", 1.0),
        (
            "# Policy\nAcceptance criteria:\n\n- Keep receipts.\n- Save the logs.\n# Notes",
            "4:3,5:4",
            1.0,
        ),
        ("Acceptance criteria: Keep receipts.\n- Save the logs.", "1:2,2:4", 1.0),
        ("Acceptance criteria: Keep receipts.\n#123 remains open.", "1:2,2:3", 1.0),
        ("Acceptance criteria:\n#123 remains open.\n# Notes", "2:3", 1.0),
        ("Acceptance criteria: Keep receipts.\n####### plain text", "1:2,2:3", 1.0),
        ("Acceptance criteria: Keep receipts.\n###### Notes\nIgnore this.", "1:2", 1.0),
        ("Acceptance criteria: Keep receipts.\n#\nIgnore this.", "1:2", 1.0),
        ("Acceptance criteria: Keep receipts.\n\nIgnore this.", "1:2", 1.0),
        (
            "\n  ACCEPTANCE CRITERIA:\n\n  - Keep\treceipts.\n  - Save logs.\n  - Ignore this.",
            "4:3,5:3",
            1.0,
        ),
        ("Acceptance criteria: Keep receipts.\n- Save logs.\n- Ignore this.", "1:2,2:3", 1.0),
        ("Acceptance criteria:\n\n# Notes\nKeep receipts.", "3:2,4:2", 1.0),
        ("Acceptance criteria:", "", 0.0),
        (SPEC, "", 0.0),
        (SPEC, SPEC.strip(), 0.0),
        (SPEC, "keep the frozen recurring-work body unchanged.", 0.0),
        (SPEC, "1:5", 0.0),
        (SPEC, "1:6\nwork_evidence=1:5", 0.0),
    ],
)
def test_work_quality_checks_the_frozen_spec_task(spec, answer, score):
    manifest = {"frozen_packet": {"frozen_spec": spec}}
    assert trial._work_quality_score(manifest, f"work_evidence={answer}") == score


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


def test_run_current_preserves_unverified_response_measurements(tmp_path, monkeypatch):
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
    assert published["brain_ingest_enabled"] is False
    assert published["identity_verified"] is False
    assert summary["identity_verified"] is False
    assert summary["brain_ingest_enabled"] is False
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
    assert body["provenance"] == "codex-session-unverified/v1"
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 6
        rows = conn.execute(
            "SELECT reported_model,resolved_provider,resolved_model FROM execution_attempts"
        ).fetchall()
        assert all(row[0] and row[1:] == (None, None) for row in rows)
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "turn_fields",
    [{}, {"server_model": None}, {"model": "configured"}],
)
def test_cli_usage_without_a_provider_model_does_not_publish(tmp_path, monkeypatch, turn_fields):
    """The live Codex JSONL shape: usage, and at most the configured model."""
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
        fields = dict(turn_fields)
        if fields.get("model") == "configured":
            fields["model"] = request["requested_model"]
        session = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-live"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": request["requested_model"]},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 11, "output_tokens": 2},
                        **fields,
                    }
                ),
            ]
        )
        final_text = _final_message_ack_and_work(manifest, request, include_work=True)
        return session + "\n", final_text, "codex-cli 0.166.0"

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


def test_server_model_preserves_unverified_measurements(tmp_path, monkeypatch):
    """A session's server_model claim cannot authenticate its own provenance."""
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
    full_profile = trial.CURRENT_TRIAL_PROFILE_IDS[0]

    def execute(request, prompt):
        head, _, _tail = prompt.partition("Frozen specification:\n")
        assert "work_evidence=1:6" not in head
        include_work = request["profile_id"] == full_profile
        session = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10 * int(request["launch_ordinal"]),
                    "output_tokens": int(request["launch_ordinal"]),
                },
                "server_model": request["requested_model"],
            }
        )
        final_text = _final_message_ack_and_work(manifest, request, include_work=include_work)
        return session + "\n", final_text, "codex-cli 0.166.0"

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
    assert published["brain_ingest_enabled"] is False
    assert published["identity_verified"] is False
    assert summary["identity_verified"] is False
    assert summary["brain_ingest_enabled"] is False
    assert (summary["profile_count"], summary["instance_count"]) == (2, 6)
    assert summary["quality_by_profile"][full_profile] == 1.0
    assert summary["quality_by_profile"][trial.CURRENT_TRIAL_PROFILE_IDS[1]] == 0.0
    for profile_id in trial.CURRENT_TRIAL_PROFILE_IDS:
        attempts = [row for row in manifest["requests"] if row["profile_id"] == profile_id]
        assert summary["provider_resolved_identity_by_profile"][profile_id] == {
            "provider": "openai",
            "model": attempts[0]["requested_model"],
        }
        assert summary["cost_tokens_by_profile"][profile_id]["tokens_in"] == sum(
            10 * int(row["launch_ordinal"]) for row in attempts
        )
    artifact = json.loads(next((tmp_path / "artifacts").glob("identity-*.json")).read_text())
    assert artifact["object"] == "turn.completed"
    assert artifact["server_model"] == artifact["provider_resolved_model"]
    assert "response_id" not in artifact
    assert artifact["provenance"] == "codex-session-unverified/v1"
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0


@pytest.mark.parametrize("witness", ["response.completed", "server_model"])
def test_forged_session_provenance_cannot_enable_verified_ingest(tmp_path, monkeypatch, witness):
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
    sessions = []
    for request in manifest["requests"]:
        final_text = _final_message_ack_and_work(manifest, request, include_work=True)
        session, _ = _session(
            request,
            model=request["requested_model"],
            tokens_in=42,
            tokens_out=7,
            final_text=final_text,
        )
        if witness == "server_model":
            session = json.dumps(
                {
                    "type": "turn.completed",
                    "server_model": request["requested_model"],
                    "usage": {"input_tokens": 42, "output_tokens": 7},
                }
            )
        sessions.append(
            {"run_id": request["run_id"], "session_text": session, "final_text": final_text}
        )
    results = trial.results_from_codex_sessions(
        manifest,
        sessions,
        artifact_dir=tmp_path / "artifacts",
        cli_version="offline-cli",
        runner_version="offline-runner",
    )
    assert results["identity_verified"] is False
    for attempt in results["attempts"]:
        assert attempt["identity_evidence"]["authority"] == trial.UNVERIFIED_SESSION_AUTHORITY
        path = Path(attempt["identity_evidence"]["artifact_ref"])
        payload = json.loads(path.read_text())
        assert payload["provenance"] == "codex-session-unverified/v1"
        # The attacker controls the artifact AND its claimed digest.
        payload.update(
            identity_verified=True,
            provenance="provider-verified",
            object="response",
            response_id="resp_forged",
        )
        path.write_text(json.dumps(payload))
        attempt["identity_evidence"]["artifact_sha256"] = (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
    results["identity_verified"] = True
    looked_up = []

    def lookup(response_id):
        looked_up.append(response_id)
        return None

    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        trial.finalize_trial(
            manifest, results, ingest_brain=True, now=2000, provider_response_lookup=lookup
        )
    assert looked_up == ["resp_forged"]
    with feedback._conn() as conn:
        for table in (
            "runs",
            "execution_attempts",
            "outcomes",
            "execution_traces",
            "completion_events",
            "influence_edges",
            "profile_trial_ingests",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert not (state / "capability-program" / "profile-trial.json").exists()

    results["identity_verified"] = False
    recorded = trial.publish_measured_recurring_trial(manifest, results, now=2001)
    assert recorded["identity_verified"] is False
    assert recorded["brain_ingest_enabled"] is False
    assert len(set(recorded["recorded_attempt_ids"])) == 6
    summary = json.loads((state / "capability-program" / "profile-trial.json").read_text())
    assert summary["identity_verified"] is False
    assert summary["brain_ingest_enabled"] is False
    assert summary["aggregate_tokens_in"] == 6 * 42
    assert summary["aggregate_tokens_out"] == 6 * 7
    assert summary["quality_by_profile"] == results["quality_by_profile"]
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 6
        rows = conn.execute(
            "SELECT reported_model,resolved_provider,resolved_model FROM execution_attempts"
        ).fetchall()
        assert all(row[0] and row[1:] == (None, None) for row in rows)
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0


def test_server_model_disagrees_with_the_response_object(tmp_path):
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
    session = "\n".join(
        [
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "object": "response",
                        "id": "resp_arm",
                        "model": request["requested_model"],
                        "usage": {"input_tokens": 4, "output_tokens": 1},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                    "server_model": request["requested_model"] + "-other",
                }
            ),
        ]
    )
    with pytest.raises(ValueError, match="disagrees with server_model"):
        trial.measure_codex_session(
            manifest,
            request,
            session,
            _final_message_ack_and_work(manifest, request, include_work=True),
        )


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
