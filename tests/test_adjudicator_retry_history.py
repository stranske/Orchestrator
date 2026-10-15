"""Paid retry lineage is accounting evidence, never another adjudicated case."""

import hashlib
import json
import sqlite3

import pytest

import adjudicator_retro as retro
import feedback


@pytest.fixture
def retry_case(tmp_path, monkeypatch):
    identity = {
        "run_id": "original-dispute",
        "verifier_verdict": "NON_PASS",
        "adjudicated_verdict": "PASS",
    }
    row = {**identity, "target": "owner/repo#1", "merged": 1}
    saved = {
        **row,
        "case_id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        "decision": "needs_more_evidence",
        "packet": {"metadata_only": True},
        "backend_run_id": "paid-original",
        "backend": "gemini",
        "cost_usd": None,
        "cost_report": {"status": "imputed", "cost_usd": 1.25},
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"rows": [saved], "population": 1}))
    monkeypatch.setattr(retro, "disputes", lambda **_: [row])
    monkeypatch.setattr(retro, "_refresh_saved_verdicts", lambda *_: None)
    monkeypatch.setattr(retro, "measured_cost", lambda *_: None)
    reports = {
        "paid-original": {"status": "imputed", "cost_usd": 1.25},
        "paid-retry": {"status": "imputed", "cost_usd": 0.75},
    }
    monkeypatch.setattr(
        feedback, "role_cost_report", lambda backend_id, *_args, **_kwargs: reports[backend_id]
    )
    packet = {
        "disputed_finding": {"ref": "finding", "body": "A retained finding"},
        "ground_truth_evidence": {
            "diff_summary": "A complete change",
            "gate_runs": ["gate"],
            "diff_patch": {"revision": "head", "text": "patch"},
            "acceptance_criteria": [{"revision": "head", "text": "named test"}],
        },
    }

    def run(result):
        return retro.run(
            dispatch=True,
            retry=True,
            path=path,
            evidence_reader=lambda _: packet,
            runner=lambda **_: result,
        )

    return path, saved, reports, run


def test_exception_after_paid_retry_retains_new_attempt(retry_case, monkeypatch):
    path, saved, _reports, run = retry_case

    def broken_cost_read(backend_id, _db):
        assert backend_id == "paid-retry"
        raise sqlite3.OperationalError("cost read unavailable after dispatch")

    monkeypatch.setattr(retro, "measured_cost", broken_cost_read)
    result = run({"backend_run_id": "paid-retry", "backend": "gemini", "errors": ["timeout"]})
    row = result["rows"][0]
    assert row["backend_run_id"] == saved["backend_run_id"]
    assert row["decision"] == "needs_more_evidence"
    assert row["retry_attempts"][-1]["backend_run_id"] == "paid-retry"
    assert "cost read unavailable" in row["retry_attempts"][-1]["cost_read_error"]
    assert result["summary"]["cost_attempted_cases"] == 2
    assert result["summary"]["estimated_cost_usd"] == 2
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize("successful", [False, True])
def test_retry_accounts_for_old_and_new_paid_attempts(retry_case, successful):
    _path, saved, _reports, run = retry_case
    result = {"backend_run_id": "paid-retry", "backend": "gemini", "errors": []}
    if successful:
        result["proposal"] = {"decision": "uphold_blocker"}
        result["advisory_plan"] = {"decision": "uphold_blocker"}
    else:
        result["errors"] = ["validator refused output"]
    report = run(result)
    row = report["rows"][0]
    ids = {row.get("backend_run_id"), *(r.get("backend_run_id") for r in row["retry_attempts"])}
    assert ids == {saved["backend_run_id"], "paid-retry"}
    assert report["summary"]["cases"] == 1
    assert report["summary"]["cost_attempted_cases"] == 2
    assert report["summary"]["cost_imputed_cases"] == 2
    assert report["summary"]["estimated_cost_usd"] == 2
    assert report["summary"]["cost_usd"] is None
    assert report["summary"]["adjudicated"] == int(successful)


def test_cost_summary_flattens_history_without_double_counting_backend_identity():
    report = retro.summarize(
        [
            {
                "backend_run_id": "paid-one",
                "cost_report": {"status": "imputed", "cost_usd": 9},
                "retry_attempts": [
                    {
                        "backend_run_id": "paid-two",
                        "cost_report": {"status": "imputed", "cost_usd": 3},
                        "retry_attempts": [
                            {
                                "backend_run_id": "paid-one",
                                "cost_report": {"status": "measured", "cost_usd": 2},
                            }
                        ],
                    },
                    {"backend_run_id": "paid-three", "cost_usd": None},
                ],
            }
        ]
    )
    assert report["cases"] == 1
    assert report["cost_attempted_cases"] == 3
    assert report["cost_measured_cases"] == 1
    assert report["cost_imputed_cases"] == 1
    assert report["cost_unmeasured_cases"] == 1
    assert report["cost_usd"] == 2
    assert report["estimated_cost_usd"] == 5
    assert report["graded"] == 0


def test_saved_retry_costs_refresh_without_redispatch(retry_case):
    path, _saved, reports, run = retry_case
    run({"backend_run_id": "paid-retry", "backend": "gemini", "errors": ["timeout"]})
    reports["paid-retry"] = {"status": "measured", "cost_usd": 4}
    result = retro.run(path=path, limit=0)
    assert result["rows"][0]["retry_attempts"][-1]["cost_report"] == reports["paid-retry"]
    assert result["summary"]["cost_usd"] == 4
    assert result["summary"]["estimated_cost_usd"] == 5.25
    assert result["summary"]["cases"] == 1


def test_success_after_failed_retry_keeps_the_complete_paid_lineage(retry_case):
    _path, _saved, reports, run = retry_case
    run({"backend_run_id": "paid-retry", "backend": "gemini", "errors": ["timeout"]})
    reports["paid-third"] = {"status": "imputed", "cost_usd": 0.5}
    result = run(
        {
            "backend_run_id": "paid-third",
            "backend": "gemini",
            "proposal": {"decision": "uphold_blocker"},
            "advisory_plan": {"decision": "uphold_blocker"},
        }
    )
    row = result["rows"][0]
    assert {row["backend_run_id"], *(r["backend_run_id"] for r in row["retry_attempts"])} == {
        "paid-original",
        "paid-retry",
        "paid-third",
    }
    assert result["summary"]["cases"] == result["summary"]["adjudicated"] == 1
    assert result["summary"]["cost_attempted_cases"] == 3
    assert result["summary"]["estimated_cost_usd"] == 2.5
    assert result["summary"]["cost_usd"] is None


def test_successful_retry_survives_cost_error_without_repeat_dispatch(retry_case, monkeypatch):
    path, _saved, _reports, run = retry_case

    def unavailable(*_args):
        raise sqlite3.OperationalError("accounting temporarily unavailable")

    monkeypatch.setattr(retro, "measured_cost", unavailable)
    result = run(
        {
            "backend_run_id": "paid-retry",
            "backend": "gemini",
            "proposal": {"decision": "uphold_blocker"},
            "advisory_plan": {"decision": "uphold_blocker"},
        }
    )
    row = result["rows"][0]
    assert row["backend_run_id"] == "paid-retry"
    assert row["decision"] == "uphold_blocker"
    assert row["retry_attempts"][-1]["backend_run_id"] == "paid-original"
    repeated = retro.run(
        dispatch=True,
        retry=True,
        path=path,
        runner=lambda **_: pytest.fail("must not pay again for an already valid proposal"),
    )
    assert repeated["rows"][0]["decision"] == "uphold_blocker"
    assert repeated["summary"]["cost_attempted_cases"] == 2


@pytest.mark.parametrize("receipt", ["imputed", "unmeasured", "error"])
def test_withdrawn_historical_cost_never_remains_measured(retry_case, monkeypatch, receipt):
    path, _saved, reports, run = retry_case
    report = run({"backend_run_id": "paid-retry", "backend": "gemini", "errors": ["timeout"]})
    attempt = report["rows"][0]["retry_attempts"][-1]
    attempt["cost_usd"] = 7
    attempt["cost_report"] = {"status": "measured", "cost_usd": 7}
    path.write_text(json.dumps(report))
    reports["paid-retry"] = {"status": receipt, "cost_usd": 0.75 if receipt == "imputed" else None}

    def refreshed(backend_id, *_args, **_kwargs):
        if backend_id == "paid-retry" and receipt == "error":
            raise sqlite3.OperationalError("receipt unavailable")
        return reports[backend_id]

    monkeypatch.setattr(feedback, "role_cost_report", refreshed)
    result = retro.run(path=path, limit=0)
    attempt = result["rows"][0]["retry_attempts"][-1]
    assert attempt["backend_run_id"] == "paid-retry"
    assert attempt["cost_usd"] is None
    assert result["summary"]["cost_measured_cases"] == 0
    assert result["summary"]["cost_usd"] is None
    assert result["summary"]["cost_attempted_cases"] == 2
    assert result["summary"]["estimated_cost_usd"] == (2 if receipt == "imputed" else 1.25)


def test_cost_refresh_failure_still_persists_paid_identity(retry_case, monkeypatch):
    path, _saved, reports, run = retry_case

    def unavailable(backend_id, *_args, **_kwargs):
        if backend_id == "paid-retry":
            raise sqlite3.OperationalError("billing read unavailable")
        return reports[backend_id]

    monkeypatch.setattr(feedback, "role_cost_report", unavailable)
    result = run({"backend_run_id": "paid-retry", "backend": "gemini", "errors": ["timeout"]})
    row = result["rows"][0]["retry_attempts"][-1]
    assert row["backend_run_id"] == "paid-retry"
    assert row["cost_refresh_error"] == "billing read unavailable"
    assert result["summary"]["cost_attempted_cases"] == 2
    assert result["summary"]["cost_unmeasured_cases"] == 1
    assert result["summary"]["cost_usd"] is None
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize("history", [{}, [None]])
def test_malformed_retry_history_is_unknown_not_silent_underaccounting(history):
    with pytest.raises(ValueError, match="retry attempt"):
        retro.summarize([{"backend_run_id": "paid-one", "retry_attempts": history}])
