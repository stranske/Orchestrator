"""The adjudicator on NEW disputes, live, in shadow: certified once, graded on truth (#643)."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

import pytest

import adjudicator_live as live
import feedback
import paths
import roles
import switch_review


@pytest.fixture
def world(tmp_path, monkeypatch):
    sha = live._toy_repository(tmp_path)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    now = int(time.time())
    with feedback._conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES ('live-1',?,'owner/repo#1')", (now,)
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
            "VALUES ('live-1','NON_PASS','PASS',1)"
        )
    calls = []

    def runner(**kw):
        calls.append(kw)
        return {
            "backend_run_id": "backend-live",
            "backend": "stub",
            "proposal": {"decision": "uphold_blocker", "rationale": "x"},
            "advisory_plan": {"decision": "uphold_blocker"},
            "errors": [],
        }

    @contextmanager
    def provider(target, head):
        assert (target, head) == ("owner/repo#1", sha)
        yield str(tmp_path / "repo")

    snapshot = {"number": 1, "headRefOid": sha, "body": live.AC_BODY}
    monkeypatch.setattr(
        live.retro,
        "_gh_json",
        lambda _args: {"total_count": 0, "check_runs": []},
    )
    common = dict(
        db=feedback.DB_PATH,
        path=tmp_path / "live.json",
        now=now + 1,
        env={},
        evidence_reader=lambda _row: live._evidence_for(sha),
        runner=runner,
        provider=provider,
        snapshot_reader=lambda _t: snapshot,
    )
    return {
        "sha": sha,
        "now": now,
        "calls": calls,
        "common": common,
        "path": tmp_path / "live.json",
    }


def test_a_new_dispute_is_certified_and_advised_once_without_touching_outcomes(world):
    first = live.run_live(**world["common"])
    assert (first["attempted"], first["advised"], first["uncertified"]) == (1, 1, 0)
    row = first["rows"][0]
    assert row["certification"]["status"] == "certified" and row["source"] == "live"
    assert row["decision"] == "uphold_blocker" and row["disposition"] == "uphold_blocker"
    call = world["calls"][0]
    assert call["source"] == "live" and isinstance(call["case"], roles.CollectedAdjudicationCase)
    second = live.run_live(**world["common"])
    assert second["attempted"] == 0 and len(world["calls"]) == 1
    with sqlite3.connect(feedback.DB_PATH) as conn:
        assert conn.execute(
            "SELECT verifier_verdict,adjudicated_verdict FROM outcomes"
        ).fetchall() == [("NON_PASS", "PASS")]
    report = json.loads(world["path"].read_text())
    assert report["summary"]["cases"] == 1 and report["population"] == 1


def test_later_truth_grades_the_live_advice(world):
    live.run_live(**world["common"])
    checked = feedback.DURABILITY_DETECTION_SINCE + 10
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE outcomes SET durability='broke_later', durability_checked_ts=? WHERE run_id='live-1'",
            (checked,),
        )
    graded = live.run_live(**world["common"])
    # uphold_blocker against a delivery that broke later is agreement.
    assert graded["summary"]["graded"] == 1 and graded["summary"]["agree"] == 1
    line = live.weekly_line(world["path"])
    assert (
        line
        == "adjudicator live: disputes 1, advised 1, uncertified 0, graded 1 (agree 1, disagree 0)"
    )


def test_a_body_without_criteria_is_uncertified_and_never_dispatched(world):
    common = {
        **world["common"],
        "snapshot_reader": lambda _t: {
            "number": 1,
            "headRefOid": world["sha"],
            "body": "no criteria",
        },
    }
    result = live.run_live(**common)
    assert (result["attempted"], result["advised"], result["uncertified"]) == (1, 0, 1)
    summary = json.loads(world["path"].read_text())["summary"]
    assert summary["uncertified_reasons"] == {"no_criteria": 1}
    assert "uncertified 1 (no_criteria 1)" in live.weekly_line(world["path"])
    assert live.uncertified_class("qualified prompt exceeds byte limit; x") == "prompt_too_large"
    assert live.uncertified_class("something new") == "other"
    assert world["calls"] == []
    assert "explicit acceptance criteria" in result["rows"][0]["certification"]["reason"]


def test_certified_case_carries_check_runs_for_evaluated_sha(world):
    row = {
        "target": "owner/repo#1",
        "packet": live.retro.build_packet(
            {"target": "owner/repo#1"}, live._evidence_for(world["sha"])
        ),
    }
    detail_url = "https://api.github.com/repos/owner/repo/check-runs/2"

    def github_reader(args):
        endpoint = args[1]
        if "commits/" in endpoint:
            return {
                "total_count": 2,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "unit",
                        "conclusion": "success",
                        "url": "https://api.github.com/repos/owner/repo/check-runs/1",
                    },
                    {"id": 2, "name": "lint", "conclusion": "failure", "url": detail_url},
                ],
            }
        assert endpoint == detail_url
        return {"details_url": "https://github.com/owner/repo/actions/runs/9/job/2"}

    case = live.certify(
        row,
        world["path"].parent / "repo",
        {"number": 1, "headRefOid": world["sha"], "body": live.AC_BODY},
        github_reader=github_reader,
        runtime_events_reader=lambda: [
            {
                "event_id": "runtime-ac-1",
                "target": "owner/repo#1",
                "observed_head_sha": world["sha"],
                "audit_evidence": [
                    {
                        "command": "python3 -m pytest tests/test_proof.py::test_proof",
                        "status": "PASS",
                        "returncode": 0,
                    }
                ],
            }
        ],
        log_reader=lambda endpoint: (
            "first line\nlast failing line"
            if endpoint.endswith("/actions/jobs/2/logs")
            else pytest.fail(f"unexpected log endpoint: {endpoint}")
        ),
    )
    execution = live.roles._collected_snapshot(case)["ground_truth_evidence"][
        "live_execution_evidence"
    ]
    assert execution["evaluated_sha"] == world["sha"]
    assert [(run["name"], run["conclusion"], run["sha"]) for run in execution["check_runs"]] == [
        ("unit", "success", world["sha"]),
        ("lint", "failure", world["sha"]),
    ]
    assert execution["check_runs"][1]["api_url"] == detail_url
    assert execution["check_runs"][1]["log_tail"].endswith("last failing line")
    assert execution["check_runs"][1]["log_provenance"] == {
        "api_url": "https://api.github.com/repos/owner/repo/actions/jobs/2/logs",
        "sha": world["sha"],
    }
    assert execution["runtime_ac_command_results"] == [
        {
            "command": "tests/test_proof.py::test_proof",
            "status": "PASS",
            "returncode": 0,
            "event_id": "runtime-ac-1",
            "producer": "runtime_ac_gate",
            "target": "owner/repo#1",
            "sha": world["sha"],
        }
    ]


def test_named_test_command_requires_exact_pytest_target():
    assert live._same_named_test_command(
        "tests/test_proof.py::test_proof", "python3 -m pytest tests/test_proof.py::test_proof"
    )
    assert not live._same_named_test_command(
        "tests/test_proof.py::test_proof", "echo 'pytest tests/test_proof.py::test_proof'"
    )
    assert not live._same_named_test_command(
        "tests/test_proof.py::test_proof",
        "python3 -m pytest --collect-only tests/test_proof.py::test_proof",
    )


def test_check_runs_keep_pending_conclusions_and_mark_non_actions_logs_unavailable():
    def github_reader(args):
        if "commits/" in args[1]:
            return {
                "total_count": 2,
                "check_runs": [
                    {"id": 1, "name": "pending", "conclusion": None},
                    {"id": 2, "name": "external", "conclusion": "failure"},
                ],
            }
        return {"details_url": "https://example.invalid/external-check"}

    runs = live._check_run_evidence(
        "owner/repo#1", "a" * 40, github_reader, lambda _endpoint: pytest.fail("no log fetch")
    )
    assert runs[0]["conclusion"] is None
    assert runs[1]["log_unavailable"] == "Actions job URL unavailable"


def test_execution_evidence_paginates_and_bounds_failure_logs(monkeypatch):
    monkeypatch.setattr(live, "CHECK_RUN_PAGE_SIZE", 1)
    calls = []

    def reader(args):
        endpoint = args[1]
        calls.append(endpoint)
        if "/commits/" in endpoint:
            page = int(endpoint.rsplit("=", 1)[1])
            return {
                "total_count": 2,
                "check_runs": [{"id": page, "name": f"job-{page}", "conclusion": "failure"}],
            }
        job = endpoint.rsplit("/", 1)[1]
        return {"details_url": f"https://github.com/owner/repo/actions/runs/9/job/{job}"}

    result = live._execution_evidence(
        {"target": "owner/repo#1"},
        "a" * 40,
        ["Run `python3 -m pytest tests/test_proof.py`"],
        github_reader=reader,
        runtime_events_reader=lambda: [],
        log_reader=lambda _endpoint: "early\n" + "x" * 8_000 + "\nlast failure",
    )
    assert [row["name"] for row in result["check_runs"]] == ["job-1", "job-2"]
    assert any("per_page=1&page=2" in endpoint for endpoint in calls)
    assert all(row["log_tail"].endswith("last failure") for row in result["check_runs"])
    assert all(len(row["log_tail"].encode()) < 4_200 for row in result["check_runs"])
    assert result["runtime_ac_command_results"] == []
    assert result["unknowns"] == ["python3 -m pytest tests/test_proof.py"]


def test_named_command_results_reject_other_heads_and_commands():
    command = "python3 -m pytest tests/test_proof.py"
    event = {
        "event_id": "right",
        "target": "owner/repo#1",
        "observed_head_sha": "a" * 40,
        "audit_evidence": [{"command": command, "status": "PASS", "returncode": 0}],
    }
    wrong_head = {**event, "event_id": "other-head", "observed_head_sha": "b" * 40}
    wrong_target = {**event, "event_id": "other-target", "target": "owner/other#1"}
    wrong_command = {
        **event,
        "event_id": "other-command",
        "audit_evidence": [{"command": "echo " + command, "status": "PASS"}],
    }
    result = live._command_result(
        command, [wrong_head, wrong_target, wrong_command, event], "owner/repo#1", "a" * 40
    )
    assert result["event_id"] == "right"


def test_exact_head_runtime_events_are_not_hidden_by_newer_unrelated_events(world):
    payload = {
        "runtime_ac_gate": {
            "target": "owner/repo#1",
            "observed_head_sha": world["sha"],
            "audit_evidence": [{"command": "pytest test_proof.py", "status": "PASS"}],
        }
    }
    with feedback._conn() as conn:
        conn.execute(
            "INSERT INTO completion_events "
            "(event_id,schema_version,run_id,event_type,phase,producer,payload_json,"
            "content_hash,created_ts,updated_ts) "
            "VALUES ('exact-head',1,'live-1','verification','shadow','runtime_ac_gate',?,'h',1,1)",
            (json.dumps(payload),),
        )
        conn.executemany(
            "INSERT INTO completion_events "
            "(event_id,schema_version,run_id,event_type,phase,producer,payload_json,"
            "content_hash,created_ts,updated_ts) "
            "VALUES (?,1,'live-1','verification','shadow','runtime_ac_gate','{}','h',2,2)",
            [(f"unrelated-{i}",) for i in range(1_001)],
        )
    events = live._runtime_events_for_sha("owner/repo#1", world["sha"])
    assert [event["event_id"] for event in events] == ["exact-head"]
    assert events[0]["producer"] == "runtime_ac_gate"


def test_failure_detail_unavailable_keeps_the_check_conclusion():
    def reader(args):
        if "/commits/" in args[1]:
            return {
                "total_count": 1,
                "check_runs": [{"id": 7, "name": "unit", "conclusion": "failure"}],
            }
        raise RuntimeError("API temporarily unavailable")

    runs = live._check_run_evidence("owner/repo#1", "a" * 40, reader, lambda _url: "")
    assert runs[0]["conclusion"] == "failure"
    assert runs[0]["log_unavailable"] == "check-run detail unavailable"


def test_certification_keeps_the_qualified_prompt_size_refusal(world, monkeypatch):
    row = {
        "target": "owner/repo#1",
        "packet": live.retro.build_packet(
            {"target": "owner/repo#1"}, live._evidence_for(world["sha"])
        ),
    }
    monkeypatch.setattr(
        live, "_execution_evidence", lambda *args, **kwargs: {"oversized": "x" * 1_048_576}
    )
    with pytest.raises(ValueError, match="qualified prompt exceeds byte limit"):
        live.certify(
            row,
            world["path"].parent / "repo",
            {"number": 1, "headRefOid": world["sha"], "body": live.AC_BODY},
        )


def test_the_kill_switch_and_the_limit_hold(world):
    off = live.run_live(**{**world["common"], "env": {live.LIVE_SWITCH: "off"}})
    assert off["disabled"] and not world["path"].exists()
    none = live.run_live(**{**world["common"], "limit": 0})
    assert none["attempted"] == 0 and world["calls"] == []
    assert live.configured_limit({live.LIVE_LIMIT: "7"}) == 7
    assert live.configured_limit({live.LIVE_LIMIT: "x"}) == live.DEFAULT_LIMIT


def test_weekly_review_prints_the_live_line(world, monkeypatch):
    live.run_live(**world["common"])
    monkeypatch.setattr(live, "report_path", lambda: world["path"])
    rendered = switch_review.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "mirror_drift": {"status": "ok"},
            "adjudicator_live": live.weekly_line(),
        }
    )
    assert "adjudicator live: disputes 1, advised 1, uncertified 0, graded 0" in rendered
    # Deliberate break, in place: without the case-id dedup the same dispute is advised again.
    world["path"].write_text(json.dumps({"rows": [], "summary": {}, "population": 1}))
    again = live.run_live(**world["common"])
    assert again["attempted"] == 1 and len(world["calls"]) == 2


def test_a_failed_role_run_is_retried_up_to_the_cap_and_counted_as_failed(world):
    boom = {"n": 0}

    def failing_runner(**kw):
        boom["n"] += 1
        raise RuntimeError("backend down")

    common = {**world["common"], "runner": failing_runner}
    first = live.run_live(**common)
    assert (first["attempted"], first["advised"], first["uncertified"]) == (1, 0, 0)
    row = first["rows"][0]
    assert row["certification"]["status"] == "certified" and row["error"] == "backend down"
    assert row["attempts"] == 1 and "proposal" not in row
    assert "uncertified 0, failed 1, graded 0" in live.weekly_line(world["path"])
    second = live.run_live(**common)
    assert second["attempted"] == 1 and len(second["rows"]) == 1
    assert second["rows"][0]["attempts"] == 2
    assert second["rows"][0]["first_attempted_at"] == row["first_attempted_at"]
    third = live.run_live(**common)
    assert third["rows"][0]["attempts"] == live.MAX_ATTEMPTS
    fourth = live.run_live(**common)
    assert fourth["attempted"] == 0 and boom["n"] == live.MAX_ATTEMPTS, "out of attempts: final"
    summary = json.loads(world["path"].read_text())["summary"]
    assert (summary["failed"], summary["retrying"]) == (1, 0)
    # A backend that answers on the retry gets the case advised (fresh report, one failure first).
    world["path"].unlink()
    live.run_live(**common)
    recovered = live.run_live(**world["common"])
    assert recovered["advised"] == 1 and recovered["rows"][0]["attempts"] == 2


def test_a_provisioning_failure_is_an_error_not_a_refusal(world):
    @contextmanager
    def broken_provider(target, head):
        raise OSError("no disk")
        yield  # pragma: no cover

    result = live.run_live(**{**world["common"], "provider": broken_provider})
    row = result["rows"][0]
    assert result["uncertified"] == 0 and row["error"] == "no disk" and "certification" not in row
    assert world["calls"] == [] and not live._final(row)


def test_the_tick_asks_the_module_whether_the_switch_is_on(monkeypatch):
    monkeypatch.delenv(live.LIVE_SWITCH, raising=False)
    assert live.main(["--enabled"]) == 0
    monkeypatch.setenv(live.LIVE_SWITCH, "off")
    assert live.main(["--enabled"]) == 1
    monkeypatch.setenv(live.LIVE_SWITCH, "yes")
    assert live.main(["--enabled"]) == 0
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text(encoding="utf-8")
    needle = 'adjudicator_live.py" --enabled'
    assert text.count(needle) == 1, "the tick reads the live kill switch through the module, once"
    assert "ORCH_ADJUDICATOR_LIVE_SHADOW" not in text, "the shell never reads the switch itself"
