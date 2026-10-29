import json
from pathlib import Path

import closer_launch_operator as operator
from closer_frontier_policy import action_identity, make_frontier


def test_ledger_keeps_unobserved_business_stages_unknown(tmp_path):
    campaign = tmp_path / "campaign.json"
    result = tmp_path / "result.json"
    terminal = tmp_path / "terminal.json"
    campaign.write_text(json.dumps({"schema_version": 1, "campaign_id": "goal-1"}))
    result.write_text(
        json.dumps({"detail": json.dumps({"native_execution": {"phases": {"A": {}}}})})
    )
    terminal.write_text(json.dumps({"passed": True, "kind": "native-wrapper-invocation"}))
    ledger = operator.acceptance_ledger(campaign, result, terminal)
    assert ledger["last_proven_boundary"] == {"stage": "ACTION A", "state": "observed"}
    assert [row["state"] for row in ledger["rows"][3:]] == ["UNKNOWN"] * 4


def test_launch_needs_matching_explicit_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(
        operator, "plan", lambda *_a, **_k: {"launch_permitted_by_diagnostic": True}
    )
    try:
        operator.main(
            [
                "launch",
                "--manifest",
                "/x",
                "--campaign",
                "/campaign",
                "--run-id",
                "r",
                "--confirm-run-id",
                "wrong",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("mismatched confirmation unexpectedly launched")
    assert "requires --confirm-run-id" in capsys.readouterr().err


def test_finite_unavailable_requires_controller_reentry_not_native_relaunch(tmp_path):
    campaign = tmp_path / "campaign.json"
    frontier = make_frontier(
        ["acme/one#1"],
        owner="runner-a",
        frontier_id="one-attempt",
        bindings={
            "acme/one#1": {
                "revision": "head-1",
                "action_id": action_identity("acme/one#1", "head-1", "review", {}),
            }
        },
    )
    campaign.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assignment_targets": ["acme/one#1"],
                "worker_config": {"runtime_manifest": "/manifest", "finite_frontier": frontier},
                "targets": {"*round*": "reconcile"},
                "workers": [{"worker_id": "w", "state": "terminal", "outcome": "unavailable"}],
            }
        )
    )
    gate = operator.campaign_gate(campaign, Path("/manifest"))
    assert gate["stage"] == "held"
    assert gate["accepted_dispositions"] == [
        "native-pre-dispatch-no-start/v2",
        "native-retained-no-b-reconciliation/v1",
    ]
