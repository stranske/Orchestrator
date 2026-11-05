"""Hermetic coverage for the issue #1150 ranked gaps.

State stays in the test tmp dir. Every subprocess and socket entry point is
stubbed; tests that need a process result inject a fake and never start one.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import adversarial
import cadence_registry
import completion_event_adapter
import feedback
import keepalive_evidence
import research_subjects
import runtime_ac

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
TARGET = "owner/repo#1"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "0")
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "brain.db")
    attempted: list = []

    def forbid(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("issue 1150 test attempted an external process or socket")

    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    yield tmp_path
    assert attempted == []


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _envelope() -> dict:
    spec_hash = "a" * 64
    base_sha = "b" * 40
    run_id = "run-1150"
    attempt_id = "attempt-1150"
    arms = ["codex"]
    profiles = ["codex:stable"]
    identity = completion_event_adapter.derive_completion_identity_ids(
        canonical_target="owner/repo#7",
        task_type="implement",
        normalized_spec_hash=spec_hash,
        base_sha=base_sha,
        run_id=run_id,
        attempt_id=attempt_id,
        subject_arms=arms,
        subject_profiles=profiles,
    )
    return {
        "schema": completion_event_adapter.ENVELOPE_SCHEMA,
        "version": completion_event_adapter.ENVELOPE_VERSION,
        "event": {
            "schema_version": completion_event_adapter.EVENT_SCHEMA_VERSION,
            "event_id": "event-1150",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "event_type": "completion",
            "phase": "verification",
            "producer": "orchestrator_remote",
            "status": "complete",
            "validation_status": "accepted",
            "payload": {"capability_ids": ["capability:coverage"]},
            "content_hash": "sha256:" + "d" * 64,
            "redaction_count": 0,
            "created_ts": 100,
            "updated_ts": 101,
        },
        "identity": {
            "subject_id": identity["subject_id"],
            "observation_id": identity["observation_id"],
            "family_id": identity["family_id"],
            "attempt_id": attempt_id,
            "attempt_resolution": "resolved",
            "canonical_target": "Owner/Repo#007",
            "repository": "owner/repo",
            "task_type": "implement",
            "normalized_spec_hash": "sha256:" + spec_hash,
            "base_sha": base_sha,
            "profile_id": "codex:stable",
            "arm_id": "codex",
            "resolved_provider": "openai",
            "resolved_model": "gpt-5.6",
            "subject_arms": arms,
            "subject_profiles": profiles,
        },
    }


def _reject(raw: dict) -> tuple[str, ...]:
    with pytest.raises(completion_event_adapter.EnvelopeError) as exc:
        completion_event_adapter.adapt_completion_event_envelope(raw)
    return exc.value.reasons


# ----- completion_event_adapter ---------------------------------------------


def test_capability_effect_rejects_fields_outside_the_contract():
    with pytest.raises(ValueError, match="invalid_capability_effect_fields"):
        completion_event_adapter.validate_capability_effect_record(
            {"schema": "x", "unexpected": "field"}
        )


def test_adapter_rejects_missing_event_field_repo_mismatch_arm_and_attempt_join():
    missing = _envelope()
    missing["event"]["event_id"] = ""
    assert any(reason == "missing_event_event_id" for reason in _reject(missing))

    mismatch = _envelope()
    mismatch["identity"]["repository"] = "other/repo"
    assert "repository_target_mismatch" in _reject(mismatch)

    arm = _envelope()
    arm["identity"]["arm_id"] = "other"
    assert "selected_arm_not_in_subject_set" in _reject(arm)

    joined = _envelope()
    joined["event"]["attempt_id"] = "different-attempt"
    assert "event_attempt_join_mismatch" in _reject(joined)


# ----- adversarial ----------------------------------------------------------


def _stamp(timestamp: int) -> dict:
    return {
        "type": "match",
        "ref": adversarial.SHAPE_MEASUREMENT_REF,
        "timestamp": timestamp,
        "metadata": {"rule": adversarial.shape_rule_id(), "status": "ok"},
    }


def test_shape_readiness_reports_unreadable_ledger_and_cramped_weeks(monkeypatch):
    import capabilities

    def unreadable(*_args, **_kwargs):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(capabilities, "load_declared", unreadable)
    ready, reason = adversarial.shape_shadow_readiness(now=10)
    assert ready is False
    assert reason == "weekly shape evidence unavailable: ledger unreadable"

    monkeypatch.setattr(
        capabilities,
        "load_declared",
        lambda *_a, **_k: {"adversarial-review": {"event_history": [_stamp(1000), _stamp(1001)]}},
    )
    ready, reason = adversarial.shape_shadow_readiness(now=1001)
    assert ready is False
    assert reason == "weekly shape measurements are less than a week apart"


def test_shape_measurement_refuses_counts_above_the_population(monkeypatch):
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    recorded = adversarial.record_shape_measurement(
        {
            "status": "ok",
            "rule": adversarial.shape_rule_id(),
            "shape_candidates": 3,
            "label_candidates": 0,
            "population": 1,
        },
        now=50,
    )
    assert recorded is False


def test_first_json_returns_none_for_empty_text_and_broken_objects():
    assert adversarial._first_json("") is None
    assert adversarial._first_json("{not json}") is None
    assert adversarial._first_json('prefix {"blocker": false}')["blocker"] is False


def test_recorded_verdict_is_unknown_when_the_brain_cannot_be_read(monkeypatch):
    def broken_conn(*_args, **_kwargs):
        raise sqlite3.OperationalError("brain locked")

    monkeypatch.setattr(feedback, "_conn", broken_conn)
    verdict = adversarial.recorded_verdict(TARGET, HEAD, ["codex"])
    assert verdict["state"] == "unknown"
    assert "brain locked" in verdict["error"]


def test_recorded_verdict_skips_malformed_payloads_and_unreadable_artifacts():
    key = adversarial.panel_key(TARGET, HEAD, ["codex"])
    conn = feedback._conn()
    try:
        conn.execute(
            "INSERT INTO completion_events ("
            "event_id, schema_version, run_id, event_type, phase, producer, status, "
            "validation_status, payload_json, content_hash, created_ts, updated_ts"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "bad",
                1,
                "run",
                "panel",
                "verification",
                "adversarial",
                "pass",
                "accepted",
                "{",
                "sha256:" + "e" * 64,
                10,
                20,
            ),
        )
        payload = {
            "adjudication_id": key,
            "verification": {"adjudicated_verdict": "PASS"},
            "artifact_refs": [
                {"kind": "panel_result", "content_hash": feedback._completion_hash("{")}
            ],
        }
        conn.execute(
            "INSERT INTO completion_events ("
            "event_id, schema_version, run_id, event_type, phase, producer, status, "
            "validation_status, payload_json, content_hash, created_ts, updated_ts"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "good",
                1,
                "run",
                "panel",
                "verification",
                "adversarial",
                "pass",
                "accepted",
                json.dumps(payload),
                "sha256:" + "f" * 64,
                11,
                10,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    missing = adversarial.recorded_verdict(TARGET, HEAD, ["codex"])
    assert missing["state"] == "found"
    assert missing["result"] is None

    path = adversarial._panel_artifact_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    broken = adversarial.recorded_verdict(TARGET, HEAD, ["codex"])
    assert broken["state"] == "found"
    assert broken["result"] is None


def test_panel_artifact_write_and_record_report_io_and_brain_failures(monkeypatch):
    def replace_fails(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(adversarial.os, "replace", replace_fails)
    written = adversarial._write_panel_artifact("sha256:" + "1" * 64, {"result": {}})
    assert written["error"] == "disk full"
    monkeypatch.setattr(adversarial.os, "replace", os.replace)

    def record_fails(*_args, **_kwargs):
        raise RuntimeError("completion insert failed")

    monkeypatch.setattr(feedback, "record_completion_event", record_fails)
    recorded = adversarial._record_panel(TARGET, HEAD, ["codex"], "panel-key", {"verdict": "PASS"})
    assert recorded["recorded"] is False
    assert "completion insert failed" in recorded["error"]


def test_worktree_head_and_git_boundary_fail_closed(monkeypatch, isolated_runtime):
    import adapters

    failed = _completed(1, stderr="fatal: not a git repository")
    monkeypatch.setattr(adversarial, "_git", lambda *_args: failed)
    with pytest.raises(RuntimeError, match="rev-parse HEAD failed"):
        adversarial._worktree_head(str(isolated_runtime / "wt"))

    monkeypatch.setattr(adapters, "write_boundary_unenforceable", lambda _agent: False)
    worktree = isolated_runtime / "review"
    worktree.mkdir()
    assert adversarial._git_write_boundary(str(worktree), "codex") is None


def _lookup_none(*_args):
    return {"state": "none", "key": "memo-key"}


def test_review_at_head_rejects_a_bad_checkout_and_a_failed_provision():
    invalid = adversarial.review_at_head(
        TARGET, HEAD, checkout_head="not-a-sha", reviewers=["codex"], lookup_fn=_lookup_none
    )
    assert invalid["status"] == "invalid"
    assert invalid["detail"] == "invalid checkout head"

    def explode(_target, _head):
        raise RuntimeError("worktree provision failed")

    failed = adversarial.review_at_head(
        TARGET,
        HEAD,
        reviewers=["codex"],
        lookup_fn=_lookup_none,
        provision_fn=explode,
    )
    assert failed["status"] == "failed"
    assert "worktree provision failed" in failed["error"]


def test_review_at_head_fails_the_seat_and_a_restore_that_leaves_the_tree_dirty(isolated_runtime):
    worktree = str(isolated_runtime / "tree")

    def clean(_wt):
        return {"head": HEAD, "dirty": ""}

    def boom(_wt, _seat, _context):
        raise RuntimeError("reviewer crashed")

    crashed = adversarial.review_at_head(
        TARGET,
        HEAD,
        reviewers=["codex"],
        worktree=worktree,
        lookup_fn=_lookup_none,
        head_fn=lambda _wt: HEAD,
        state_fn=clean,
        review_fn=boom,
    )
    assert crashed["status"] == "failed"
    assert "reviewer crashed" in crashed["error"]

    calls = {"n": 0}

    def dirty_after(_wt):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"head": HEAD, "dirty": ""}
        return {"head": HEAD, "dirty": "M src/a.py"}

    restored = adversarial.review_at_head(
        TARGET,
        HEAD,
        reviewers=["codex"],
        worktree=worktree,
        lookup_fn=_lookup_none,
        head_fn=lambda _wt: HEAD,
        state_fn=dirty_after,
        review_fn=lambda *_a: {"verdict": "PASS"},
        restore_fn=lambda *_a: None,
        record_fn=lambda *_a: {"recorded": True},
    )
    assert restored["status"] == "executed"
    assert restored["result"]["attempts"][0]["outcome"] == "restore_failed"
    assert "restore did not return a clean tree" in restored["result"]["attempts"][0]["error"]


def test_followup_lock_invalid_state_bad_labels_and_source_closure(monkeypatch, isolated_runtime):
    import fcntl

    def locked_out(*_args, **_kwargs):
        raise BlockingIOError()

    monkeypatch.setattr(fcntl, "flock", locked_out)
    locked = adversarial.file_followup(
        TARGET, HEAD, {"verdict": "BLOCKED", "key": "lock-key", "result": {"blockers": []}}
    )
    assert locked == {"filed": False, "reason": "follow-up locked; retry", "key": "lock-key"}

    record = isolated_runtime / "followup.json"
    record.write_text(json.dumps({"key": "lock-key", "state": "weird"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid follow-up state"):
        adversarial._file_followup_locked(
            TARGET,
            HEAD,
            {"verdict": "BLOCKED", "result": {"blockers": []}},
            "lock-key",
            record,
            lambda _cmd: _completed(0, "[]"),
        )

    fresh = isolated_runtime / "fresh.json"

    def gh(cmd):
        if cmd[1:3] == ["issue", "list"]:
            return _completed(0, "[]")
        if cmd[1:3] == ["label", "list"]:
            return _completed(0, "not-json")
        if cmd[1:3] == ["issue", "create"]:
            return _completed(1, "")
        raise AssertionError(cmd)

    with pytest.raises(RuntimeError, match="follow-up create response failed"):
        adversarial._file_followup_locked(
            TARGET,
            HEAD,
            {"verdict": "BLOCKED", "result": {"blockers": [{"severity": "critical"}]}},
            "create-key",
            fresh,
            gh,
        )

    out = {"issue": 9, "repo": "owner/repo", "closed_source": [], "key": "k"}
    failed_lookup = adversarial._close_followup_sources(
        TARGET, dict(out), lambda _cmd: _completed(1, ""), fresh, filed=True
    )
    assert "source lookup failed" in failed_lookup["error"]

    open_pr = adversarial._close_followup_sources(
        TARGET,
        dict(out),
        lambda _cmd: _completed(
            0, json.dumps({"state": "OPEN", "closingIssuesReferences": [{"number": 4}]})
        ),
        fresh,
        filed=True,
    )
    assert "source closure requires a merged PR" in open_pr["error"]

    def source_state(cmd):
        if cmd[1:3] == ["pr", "view"]:
            return _completed(
                0,
                json.dumps(
                    {
                        "state": "MERGED",
                        "closingIssuesReferences": [
                            {
                                "number": 4,
                                "repository": {"name": "repo", "owner": {"login": "owner"}},
                            }
                        ],
                    }
                ),
            )
        return _completed(1, "")

    missing_state = adversarial._close_followup_sources(
        TARGET, dict(out), source_state, fresh, filed=True
    )
    assert "source state lookup failed" in missing_state["error"]


def test_spawn_post_merge_panel_fails_on_lookup_and_on_popen(monkeypatch, isolated_runtime):
    calls = {"n": 0}

    def gh_lookup(_cmd, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _completed(1, "")
        if calls["n"] == 2:
            return _completed(0, json.dumps({"state": "OPEN"}))
        return _completed(0, json.dumps({"state": "MERGED", "mergeCommit": {}}))

    monkeypatch.setattr(subprocess, "run", gh_lookup)
    assert (
        "merged commit lookup failed"
        in adversarial.spawn_post_merge_panel(TARGET, HEAD, "ctx", set())["error"]
    )
    assert (
        "PR is not merged"
        in adversarial.spawn_post_merge_panel(TARGET, HEAD, "ctx", set())["error"]
    )
    assert (
        "merged commit is unavailable"
        in adversarial.spawn_post_merge_panel(TARGET, HEAD, "ctx", set())["error"]
    )

    def popen_fails(*_args, **_kwargs):
        raise OSError("cannot fork")

    spawned = adversarial.spawn_post_merge_panel(
        TARGET, HEAD, "ctx", {"codex"}, popen=popen_fails, merge_head_fn=lambda _target: HEAD
    )
    assert spawned["status"] == "spawn_failed"
    assert "cannot fork" in spawned["error"]


def test_capability_heartbeat_swallows_recorder_failures(monkeypatch):
    import capabilities

    called = {}

    def broken(*_args, **_kwargs):
        called["event"] = _args[1]
        raise RuntimeError("heartbeat store down")

    monkeypatch.setattr(capabilities, "production_heartbeat", broken)
    adversarial._capability_heartbeat("match")
    assert called["event"] == "match"


def test_review_cli_followup_and_usage(monkeypatch, capsys):
    monkeypatch.setattr(
        adversarial,
        "review_at_head",
        lambda *_a, **_k: {"status": "executed", "verdict": "BLOCKED"},
    )
    monkeypatch.setattr(adversarial, "file_followup", lambda *_a, **_k: {"filed": True, "issue": 4})
    assert (
        adversarial._review_cli(
            ["--target", TARGET, "--head", HEAD, "--followup", "--reviewers", "codex"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["followup"]["issue"] == 4

    monkeypatch.setattr(
        adversarial, "review_at_head", lambda *_a, **_k: {"status": "failed", "error": "no seat"}
    )
    assert (
        adversarial._review_cli(["--target", TARGET, "--head", HEAD, "--reviewers", "codex"]) == 2
    )

    assert adversarial.main([]) == 0
    usage = capsys.readouterr().out
    assert "usage: adversarial.py" in usage


# ----- runtime_ac -----------------------------------------------------------


def test_unsafe_command_and_unknown_check_evidence():
    reason = runtime_ac._unsafe_command_reason('pytest "unterminated')
    assert reason is not None
    assert reason.startswith("cannot be parsed safely:")
    assert runtime_ac._evidence_for_check({"type": "not-a-check"}) == set()


def test_validate_spec_skips_non_objects_rejects_bad_checks_and_ratio():
    spec = runtime_ac._valid_spec()
    spec["acceptance_criteria"].append("not-an-object")
    spec["acceptance_criteria"].append(
        {
            "id": "AC9",
            "statement": "needs a check list",
            "evidence_required": ["test_output"],
            "checks": "nope",
        }
    )
    spec["verdict_policy"]["min_pass_ratio"] = "high"
    errors = runtime_ac.validate_spec(spec)
    assert any(error.endswith("checks must be a list") for error in errors)
    assert "verdict_policy.min_pass_ratio must be a number" in errors


def test_dry_run_plan_copies_regex_pattern_and_warns_when_validation_is_bypassed(monkeypatch):
    spec = runtime_ac._valid_spec()
    command = next(
        check
        for criterion in spec["acceptance_criteria"]
        for check in criterion["checks"]
        if check["type"] == "command"
    )
    command["expected"] = "regex"
    command["pattern"] = "passed"
    plan = runtime_ac.build_dry_run_plan(spec)
    planned = next(item for item in plan["planned_checks"] if item["id"] == command["id"])
    assert planned["pattern"] == "passed"

    monkeypatch.setattr(runtime_ac, "validate_spec", lambda _spec: [])
    spec["acceptance_criteria"][0]["evidence_required"] = ["manual_review"]
    warned = runtime_ac.build_dry_run_plan(spec)
    assert any("missing evidence types" in warning for warning in warned["warnings"])


def test_bounded_break_result_and_regex_command_reject_bad_payloads():
    assert runtime_ac.bounded_deliberate_break_result({"verdict": "PASS", "error": 1}) is None
    assert (
        runtime_ac.bounded_deliberate_break_result(
            {"verdict": "PASS", "green": {"stdout_tail": 1, "stderr_tail": ""}}
        )
        is None
    )
    result = runtime_ac._completed_result(
        {"id": "c", "type": "command", "expected": "regex", "pattern": "ok", "command": "true"},
        _completed(2, stdout=""),
        0.1,
    )
    assert result["status"] == "FAIL"
    assert result["reason"] == "command exited 2"
    assert runtime_ac._repository_python(["echo", "hi"], sys.executable) == ["echo", "hi"]


def test_run_verification_errors_when_a_command_check_has_no_command(monkeypatch, isolated_runtime):
    monkeypatch.setattr(runtime_ac, "validate_spec", lambda _spec: [])
    spec = runtime_ac._valid_spec()
    spec["runtime_context"]["worktree"] = str(isolated_runtime)
    command = next(
        check
        for criterion in spec["acceptance_criteria"]
        for check in criterion["checks"]
        if check["type"] == "command"
    )
    command["command"] = "   "
    report = runtime_ac.run_verification(
        spec,
        confirm_run=True,
        allow_command_checks=True,
        check_ids=[command["id"]],
    )
    matched = next(item for item in report["check_results"] if item["id"] == command["id"])
    assert matched["status"] == "ERROR"
    assert matched["reason"] == "no command to run"


def test_author_issue_spec_rejects_a_target_that_cannot_become_a_slug():
    with pytest.raises(ValueError, match="invalid authored spec"):
        runtime_ac.author_issue_spec("owner/repo#1", "", target="!!!")


def test_runtime_ac_cli_validate_plan_run_results_and_goal(monkeypatch, isolated_runtime, capsys):
    missing = isolated_runtime / "missing.json"
    assert runtime_ac.main(["--validate", str(missing)]) == 2
    assert "Could not read or parse" in capsys.readouterr().err
    assert runtime_ac.main(["--plan", str(missing)]) == 2
    assert "Could not build runtime AC plan" in capsys.readouterr().err

    spec = runtime_ac._valid_spec()
    command = next(
        check
        for criterion in spec["acceptance_criteria"]
        for check in criterion["checks"]
        if check["type"] == "command"
    )
    command["expected"] = "regex"
    command["pattern"] = "passed"
    path = isolated_runtime / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert runtime_ac.main(["--plan", str(path), "--json"]) == 0
    plan_out = capsys.readouterr().out
    planned = json.loads(plan_out)
    assert planned["verification_id"] == spec["verification"]["id"]

    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_k: {"gate": {"verdict": "PASS"}, "results": []},
    )
    monkeypatch.setattr(
        runtime_ac,
        "record_gate_verdict",
        lambda run_id, _gate: {"recorded": True, "run_id": run_id},
    )
    assert (
        runtime_ac.main(["--run", str(path), "--confirm-run", "--record-run-id", "run-1150"]) == 0
    )
    ran = json.loads(capsys.readouterr().out)
    assert ran["feedback"]["run_id"] == "run-1150"

    monkeypatch.setattr(
        runtime_ac, "run_verification", lambda *_a, **_k: {"gate": {"verdict": "FAIL"}}
    )
    assert runtime_ac.main(["--run", str(path), "--confirm-run"]) == 1
    assert json.loads(capsys.readouterr().out)["gate"]["verdict"] == "FAIL"

    result_path = isolated_runtime / "results.json"
    result_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runtime_ac, "evaluate_results", lambda *_a, **_k: {"verdict": "PASS"})
    assert (
        runtime_ac.main(
            [
                "--results",
                str(path),
                "--result-json",
                str(result_path),
                "--record-run-id",
                "run-1150",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["feedback"]["recorded"] is True
    assert runtime_ac.main(["--results", str(missing), "--result-json", str(missing)]) == 2
    assert "Could not evaluate runtime AC results" in capsys.readouterr().err

    assert runtime_ac.main(["--goal", "check the gate", "--json"]) == 0
    assert "prompt" in json.loads(capsys.readouterr().out)


# ----- keepalive_evidence ---------------------------------------------------


def test_pr_number_run_json_and_linked_issue_filters(monkeypatch):
    assert keepalive_evidence._pr_number({"target": "owner/repo", "run_id": "keepalive"}) is None

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert keepalive_evidence._run_json(["gh", "pr", "view"]) is None
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("gh"))
    )
    assert keepalive_evidence._run_json(["gh"]) is None

    responses = [
        {"closingIssuesReferences": "not-a-list"},
        {"closingIssuesReferences": ["skip-me", {"number": None}, {"number": 4}]},
        {"number": "nope", "state": "OPEN"},
    ]

    def fake_json(_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(keepalive_evidence, "_run_json", fake_json)
    assert keepalive_evidence._linked_issues("owner/repo", 4) == []
    assert keepalive_evidence._linked_issues("owner/repo", 4) == []


def test_dedupe_skips_blank_open_issues_and_repeated_search_hits():
    candidate = {
        "seed": "delivery regressed badly",
        "evidence": {"pr": 4, "title": "Fix delivery regression now"},
    }
    seen: list[int] = []

    def search(_repo, _query):
        return [{"number": 9, "title": "unrelated", "state": "OPEN"}, {"number": 9, "title": "dup"}]

    keepalive_evidence._dedupe_candidate(
        candidate,
        "owner/repo",
        linked_issues_fn=lambda *_a: [
            {"state": "OPEN"},
            {"state": "OPEN", "number": "nope"},
            {"state": "CLOSED", "number": 3},
        ],
        issue_search_fn=search,
    )
    assert candidate["possible_duplicate"] is False
    seen.append(candidate["dup_issue"])
    assert seen == [None]


def test_gotcha_section_ends_at_the_next_top_level_item_and_tooling_seed():
    text = "- Known gotchas:\n  - never ship this broken path\n- Next section\n"
    assert keepalive_evidence._gotcha_lines(text) == ["never ship this broken path"]
    seed = keepalive_evidence._process_signal_seed("owner/repo", "tooling", 2, 30)
    assert "tooling or CI workflow changes are unstable" in seed


def test_human_summary_lists_candidates_and_the_empty_case():
    populated = keepalive_evidence._human_summary(
        {
            "repo": "owner/repo",
            "lookback_days": 7,
            "candidates": [
                {
                    "severity": "HIGH",
                    "type": "regression",
                    "seed": "delivery broke",
                    "possible_duplicate": True,
                    "dup_issue": 3,
                }
            ],
            "process_signals": [{"severity": "MED", "work_type": "tooling", "seed": "ci churn"}],
            "signals": {"durable_rate": 0.5},
        }
    )
    assert "possible duplicate #3" in populated
    assert "MED tooling: ci churn" in populated
    empty = keepalive_evidence._human_summary(
        {
            "repo": "owner/repo",
            "lookback_days": 7,
            "candidates": [],
            "process_signals": [],
            "signals": {"durable_rate": None},
        }
    )
    assert "No dynamic issue candidates." in empty
    assert "No process signals." in empty


# ----- cadence_registry -----------------------------------------------------


def test_retirement_helpers_and_cli_are_inert_without_a_declared_hold(isolated_runtime, capsys):
    assert cadence_registry.re_enable_when({"key": "plain"}) is None
    assert cadence_registry.retirement_lifted_by({"key": "plain"}, {"ORCH_TEST": "1"}) is None
    assert cadence_registry.main(["shell", "--state-dir", str(isolated_runtime)]) == 0
    assert "cadence_stamp()" in capsys.readouterr().out
    assert cadence_registry.main(["--state-dir", str(isolated_runtime)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "steps" in report


# ----- research_subjects ----------------------------------------------------


def test_unevaluated_ids_and_round_identity_fail_closed_without_tables():
    class Broken:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("no runs table")

    assert research_subjects.unevaluated_experiment_ids(Broken()) == set()
    empty = sqlite3.connect(":memory:")
    assert research_subjects.read_round_identity(empty, "missing") is None
    empty.close()


def test_round_arms_ignore_corrupt_json_and_findings_require_a_real_arm():
    conn = feedback._conn()
    rid, identity = research_subjects.record_research_round(
        "owner/repo", "audit", "2026-11-05", "scope", ["codex"], conn=conn
    )
    conn.execute("UPDATE research_subjects SET arms_json=?", ("{",))
    conn.commit()
    evidence = research_subjects.round_arm_evidence(rid, conn=conn)
    assert evidence["registered"] == []
    feedback.record_run(
        "attempted", "owner/repo#1", "implement", "claude", experiment_id=rid, conn=conn
    )
    evidence = research_subjects.round_arm_evidence(rid, conn=conn)
    assert evidence["attempted"] == ["claude"]
    assert evidence["arms"] == ["claude"]

    with pytest.raises(ValueError, match="arm that produced it"):
        research_subjects.record_finding_issue(
            rid, "owner/repo#2", arm="  ", identity=identity, conn=conn
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        research_subjects.record_finding_issue(
            rid,
            "owner/repo#2",
            arm="claude",
            identity=identity,
            implementation_run_id="  ",
            conn=conn,
        )
    feedback.record_run("impl-ok", "owner/repo#2", "implement", "claude", conn=conn)
    event_id = research_subjects.record_finding_issue(
        rid,
        "owner/repo#2",
        arm="claude",
        identity=identity,
        implementation_run_id="impl-ok",
        conn=conn,
    )
    assert event_id.startswith("subject-event:")
    conn.close()


def test_implementation_binding_skips_corrupt_events_and_names_an_unscored_run():
    conn = feedback._conn()
    rid, identity = research_subjects.record_research_round(
        "owner/findings", "audit", "2026-11-05", "scope", ["codex"], conn=conn
    )
    conn.execute(
        "INSERT INTO research_subject_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bad-filed", 1, None, None, None, None, "finding_filed", "x", rid, "{"),
    )
    feedback.record_run("bare-run", "owner/findings#4", "implement", "codex", conn=conn)
    research_subjects.record_finding_issue(
        rid, "owner/findings#4", arm="codex", identity=identity, conn=conn
    )
    conn.execute(
        "INSERT INTO research_subject_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bad-impl", 2, None, None, None, None, "finding_implemented", "x", rid, "{"),
    )
    research_subjects.record_finding_implementation(rid, "owner/findings#4", "bare-run", conn=conn)
    report = research_subjects.resolve_round_durability(rid, conn=conn)
    assert report["unresolved_by_reason"]["bound_run_has_no_outcome"] == 1
    conn.close()


def test_durability_edge_failure_is_counted_not_fatal(monkeypatch):
    conn = feedback._conn()
    rid, identity = research_subjects.record_research_round(
        "owner/edges", "audit", "2026-11-05", "scope", ["codex"], conn=conn
    )
    feedback.record_run("impl", "owner/edges#5", "implement", "claude", conn=conn)
    feedback.record_run(
        "arm-run", "owner/edges", "implement", "codex", experiment_id=rid, conn=conn
    )
    conn.execute(
        "INSERT INTO outcomes (run_id, verifier_verdict, durability) VALUES (?,?,?)",
        ("impl", "PASS", "durable"),
    )
    research_subjects.record_finding_issue(
        rid,
        "owner/edges#5",
        arm="codex",
        identity=identity,
        implementation_run_id="impl",
        conn=conn,
    )

    def refuse(**_kwargs):
        raise RuntimeError("edge store refused")

    monkeypatch.setattr(feedback, "record_influence_edge", refuse)
    report = research_subjects.resolve_round_durability(rid, conn=conn, apply_edges=True)
    assert report["per_arm_durability"] == {"codex": {"durable": 1}}
    assert report["edges_written"] == 0
    conn.close()


def test_subject_admission_cooldown_cap_and_lifecycle_edges():
    identity = research_subjects.subject_identity(
        "owner/repo#8", "implement", "same spec", "abc123", ["codex"]
    )
    other = research_subjects.subject_identity(
        "owner/repo#8", "implement", "same spec", "abc123", ["claude"]
    )
    research_subjects.record_subject(
        other, lifecycle="active", exp_id="exp-open", cooldown_hours=0, now=2_000_000
    )
    research_subjects.record_subject(
        identity, lifecycle="evaluated", exp_id="exp-done", cooldown_hours=0, now=2_000_000
    )
    capped = research_subjects.assess_candidate(
        target="owner/repo#8",
        task_type="implement",
        spec="same spec",
        base_sha="abc123",
        arms=["codex"],
        now=2_000_000,
        per_subject_cap=1,
    )
    assert capped["reason"] == "subject_backlog_cap"
    assert capped["eligible"] is False

    cooling = research_subjects.subject_identity(
        "owner/repo#9", "implement", "other spec", "def456", ["codex"]
    )
    research_subjects.record_subject(
        cooling, lifecycle="evaluated", exp_id="exp-cool", cooldown_hours=10, now=1_000_000
    )
    cooled = research_subjects.assess_candidate(
        target="owner/repo#9",
        task_type="implement",
        spec="other spec",
        base_sha="def456",
        arms=["codex"],
        now=1_000_000,
    )
    assert cooled["reason"] == "subject_cooldown"
    assert cooled["eligible"] is False

    with pytest.raises(ValueError, match="invalid research subject lifecycle"):
        research_subjects.record_subject(identity, lifecycle="nope", exp_id="bad")
    with pytest.raises(ValueError, match="invalid research subject lifecycle"):
        research_subjects.mark_lifecycle("exp-done", "nope")
    assert research_subjects.mark_lifecycle("exp-done", "failed", reason="scored") is True
    assert research_subjects.prior_experiment_count(identity) >= 1


def test_weights_and_summary_close_an_empty_brain(monkeypatch):
    def empty_conn(*_args, **_kwargs):
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(feedback, "_conn", empty_conn)
    assert research_subjects.effective_evidence_weights() == {}
    summary = research_subjects.summary()
    assert summary["registered_subjects"] == 0
    assert summary["window_days"] == 90


def test_weights_close_after_reading_a_real_brain():
    conn = feedback._conn()
    research_subjects.ensure_schema(conn)
    conn.close()
    assert research_subjects.effective_evidence_weights() == {}


def test_rounds_report_counts_failures_and_summary_survives_a_broken_report(monkeypatch):
    now = 1_800_000_000
    feedback.record_run(
        "off-fail",
        "owner/repo#1",
        "implement",
        "codex",
        mode="offload",
        experiment_id="owner/repo:audit:2026-11-05",
        ts=now,
    )
    feedback.record_outcome("off-fail", adjudicated_verdict="FAIL", failure_class="")
    report = research_subjects.rounds_report(30, now=now)
    assert report["fail"] == 1
    assert report["by_kind"]["audit"]["fail"] == 1

    def broken(*_args, **_kwargs):
        raise RuntimeError("rounds down")

    monkeypatch.setattr(research_subjects, "rounds_report", broken)
    section = research_subjects.summary_for_report(7)
    assert section == {"window_days": 7, "error": "RuntimeError: rounds down"}


def test_research_cli_records_findings_and_reports_refusals(capsys):
    assert (
        research_subjects.main(
            [
                "finding-filed",
                "--round-id",
                "missing:audit:2026-01-01",
                "--issue",
                "owner/repo#3",
                "--arm",
                "codex",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["recorded"] is False

    rid, _identity = research_subjects.record_research_round(
        "owner/cli", "audit", "2026-11-05", "scope", ["codex"]
    )
    assert (
        research_subjects.main(
            ["finding-filed", "--round-id", rid, "--issue", "owner/cli#3", "--arm", "nobody"]
        )
        == 1
    )
    assert "arm_not_in_subject_set" in json.loads(capsys.readouterr().out)["reason"]

    feedback.record_run("cli-impl", "owner/cli#3", "implement", "codex")
    assert (
        research_subjects.main(
            [
                "finding-filed",
                "--round-id",
                rid,
                "--issue",
                "owner/cli#3",
                "--arm",
                "codex",
                "--implementation-run-id",
                "cli-impl",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["recorded"] is True

    assert (
        research_subjects.main(
            [
                "finding-implemented",
                "--round-id",
                rid,
                "--issue",
                "owner/cli#9",
                "--run-id",
                "missing-run",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["recorded"] is False

    assert research_subjects.main(["round-durability", "--round-id", rid]) == 0
    assert json.loads(capsys.readouterr().out)["round_id"] == rid
    assert research_subjects.main([]) == 0
    assert "registered_subjects" in json.loads(capsys.readouterr().out)
