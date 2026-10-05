"""Independent demand and first-break regressions, including the real weekly consumer."""

import json

import capabilities
import capability_admission
import capability_advisor
import switch_review
import value_chain_monitor as monitor

NOW = 1791183600


def ledger(tmp_path, names=("role-prompt",)):
    path = tmp_path / "capabilities.json"
    for name in names:
        capabilities.register(name, {"status": "wired"}, path)
    return path


def complete_row(**changes):
    return dict(
        situation_count=2, offered=1, invocation_count=1, usable=1, acted_on=1, outcome=1, **changes
    )


def test_unmeasured_demand_never_prints_as_zero(tmp_path):
    section = monitor.report(now=NOW, path=ledger(tmp_path), env={})
    assert section["rows"][0]["situation_count"] is None
    text = "\n".join(monitor.format_lines(section))
    assert "demand unmeasured" in text
    assert "demand 0" not in text
    assert "no_situation" not in text


def test_first_break_is_the_earliest_failed_step():
    row = complete_row()
    assert monitor.first_break(row) == "works"
    row["outcome"] = 0
    assert monitor.first_break(row) == "no_outcome"
    row["acted_on"] = 0
    assert monitor.first_break(row) == "not_acted_on"
    row["usable"] = 0
    assert monitor.first_break(row) == "unusable"
    row["invocation_count"] = 0
    assert monitor.first_break(row) == "not_invoked"
    row["offered"] = 0
    assert monitor.first_break(row) == "not_offered"
    row["situation_count"] = 0
    assert monitor.first_break(row) == "no_situation"
    row["situation_count"] = None
    assert monitor.first_break(row) is None


def test_input_off_names_the_flag_for_a_dispatch_lane_dependent_row(tmp_path):
    path = ledger(tmp_path, ("role-triage",))
    row = monitor.report(now=NOW, path=path, env={"ORCH_DISPATCH_LANE": "0"})["rows"][0]
    assert row["input_off"] == ["input_off:ORCH_DISPATCH_LANE"]
    assert row["situation_count"] is None
    assert monitor.report(now=NOW, path=path, env={})["rows"][0]["input_off"] == []
    assert (
        monitor.report(now=NOW, path=path, env={"ORCH_DISPATCH_LANE": "1"})["rows"][0]["input_off"]
        == []
    )


def test_switch_review_prints_one_line_per_live_capability(tmp_path, monkeypatch):
    path = ledger(tmp_path, ("role-prompt", "role-decomposer", "retired-cap"))
    capabilities.transition("retired-cap", "retired", reason="fixture", path=path)
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **kw: {"held_off": [], "on_but_idle": []}
    )
    monkeypatch.setattr(switch_review, "stale_runners", lambda: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **kw: {})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {})
    monkeypatch.setattr(switch_review, "gate_expiry", lambda **kw: {})
    rep = switch_review.review(now=NOW, env={}, path=path)
    text = switch_review.format_report(rep)
    assert rep["value_chain"]["total"] == 2
    assert text.count("role-prompt:") == 1
    assert text.count("role-decomposer:") == 1
    assert "retired-cap:" not in text
    assert (
        switch_review.review(now=NOW, env={"ORCH_VALUE_CHAIN_MONITOR": "0"}, path=path)[
            "value_chain"
        ]["disabled"]
        is True
    )


def test_situation_registry_counts_input_not_invocations():
    issues = [dict(createdAt="2026-10-01T01:00:00Z", state="OPEN", body="- [ ] task\n" * 12)] * 3
    inputs = {"fleet_issues": issues}
    assert monitor.situation_count("role-prompt", inputs) == 3
    assert monitor.situation_count("role-decomposer", inputs) == 3
    assert monitor.situation_count("role-triage", inputs) == 3
    assert monitor.situation_count("role-prompt", {"fleet_issues": []}) == 0
    assert (
        monitor.situation_count(
            "role-prompt", {"fleet_issues": [{**i, "in_window": False} for i in issues]}
        )
        == 0
    )
    assert monitor.situation_count("testgen-lane", {}) is None
    assert (
        monitor.situation_count(
            "testgen-lane",
            {
                "precondition_probes": {
                    "testgen-lane": [
                        {"precondition_met": True},
                        {"precondition_met": None},
                        {"precondition_met": False},
                    ]
                }
            },
        )
        == 1
    )
    assert monitor.situation_count("capacity", {"cadence_inputs": {"capacity": 2}}) == 2


def test_persisted_dispute_count_is_independent_of_role_invocation():
    inputs = {
        "completion_events": [
            {
                "payload": {
                    "role_ids": ["adjudicator"],
                    "result": {
                        "disagreement": True,
                        "selector_reason_id": "persisted_evidence_disagreement",
                        "invoked": False,
                    },
                }
            }
        ]
    }
    assert monitor.situation_count("role-adjudicator", inputs) == 1
    assert monitor.situation_count("role-adjudicator", {}) is None


def test_missing_brain_is_unknown_and_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    result = monitor._brain(path, NOW - 90 * 86400, NOW)
    assert result["edges"] is None
    assert result["completion_events"] is None
    assert result["errors"]
    assert not path.exists()


def test_counts_exclude_fixture_trials_and_ungraded_edges(tmp_path):
    path = ledger(tmp_path)
    row = capabilities.load(path, create=False)["role-prompt"]
    row["event_history"] = [
        {"timestamp": NOW, "type": "invocation"},
        {"timestamp": NOW, "type": "success"},
        {"timestamp": NOW, "type": "invocation", "ref": "advice:trial"},
        {
            "timestamp": NOW,
            "type": "success",
            "metadata": {"evidence_provenance": "fixture_observed"},
        },
        {"timestamp": NOW - 100 * 86400, "type": "invocation"},
    ]
    capabilities.save({"role-prompt": row}, path)
    before = path.read_bytes()
    inputs = {
        "fleet_issues": [],
        "edges": [
            {
                "capability_id": "role-prompt",
                "accepted": 1,
                "outcome_verdict": "PASS",
                "durability": "pending",
            },
            {
                "capability_id": "role-prompt",
                "accepted": 0,
                "outcome_verdict": "PASS",
                "durability": "durable",
            },
            {
                "capability_id": "role-prompt",
                "accepted": 1,
                "outcome_verdict": "PASS",
                "durability": "durable",
                "counterfactual": 1,
            },
        ],
    }
    report = monitor.report(now=NOW, path=path, env={}, inputs=inputs)
    row = report["rows"][0]
    assert row["invocation_count"] == 1
    assert row["usable"] == 1
    assert row["acted_on"] == 1
    assert row["outcome"] == 0
    assert path.read_bytes() == before


def test_fleet_population_paginates_and_rejects_partial_evidence():
    calls = []

    def gh(args):
        calls.append(args[-1])
        is_second = "after:" in args[-1]
        issue = {
            "number": 2 if is_second else 1,
            "createdAt": "2026-10-01T01:00:00Z",
            "body": "",
            "state": "OPEN",
        }
        return (
            True,
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "issues": {
                                "nodes": [issue],
                                "pageInfo": {"hasNextPage": not is_second, "endCursor": "next"},
                            }
                        }
                    }
                }
            ),
            "",
        )

    rows = monitor.fleet_issues(gh, NOW - 90 * 86400, NOW, ["stranske/Orchestrator"])
    assert len(rows) == 2
    assert len(calls) == 4
    assert any("states:OPEN" in q for q in calls)
    assert monitor.fleet_issues(lambda args: (False, "", "inaccessible"), 0, NOW, ["a/b"]) is None

    def broken(args):
        return (
            True,
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "issues": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": True, "endCursor": None},
                            }
                        }
                    }
                }
            ),
            "",
        )

    assert monitor.fleet_issues(broken, 0, NOW, ["a/b"]) is None


def test_new_capability_has_all_nine_admission_parts(tmp_path):
    path = ledger(tmp_path)
    capabilities.register(
        "value-chain-monitor", capabilities.KNOWN_DECLARATIONS["value-chain-monitor"], path
    )
    report = capability_admission.report(path=path)
    row = next(r for r in report["rows"] if r["capability_id"] == "value-chain-monitor")
    assert row["admitted"], row
    assert "value-chain-monitor" in capability_advisor.SURFACE_BINDINGS["tick"]
    assert "value-chain-monitor" in capability_advisor.SURFACE_BINDINGS["rail-exercise:audit"]
    assert monitor.recurrence_fixture()


def test_weekly_cli_honors_kill_switch_without_reading_inputs(monkeypatch, capsys):
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setattr(
        monitor,
        "collect_inputs",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not collect")),
    )
    monkeypatch.setattr(
        switch_review, "review", lambda **kw: {"disabled": kw["env"]["ORCH_VALUE_CHAIN_MONITOR"]}
    )
    assert switch_review.main(["--env", "process", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["disabled"] == "0"


def test_evaluated_precondition_is_measured_without_an_invocation(tmp_path):
    path = ledger(tmp_path, ("deliberate-break-verifier",))
    advice = {
        "task": "bounded acceptance repair",
        "capabilities": [{"capability_id": "deliberate-break-verifier", "precondition_met": True}],
    }
    capability_advisor._record_matches(advice, surface="opener-lane", path=path)
    row = monitor.report(path=path, env={}, inputs={"edges": []})["rows"][0]
    assert row["situation_count"] == 1
    assert row["invocation_count"] == 0
    assert row["first_break"] == "not_invoked"
