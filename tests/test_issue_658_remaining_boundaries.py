"""Exercise residual production fallback paths without a live ledger or process."""

import socket
import subprocess
from contextlib import nullcontext

import pytest

import capabilities
import capability_advisor as advisor
import capability_propensity as propensity


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Refuse unstubbed external effects and isolate both runtime roots."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))

    def forbidden(*args, **kwargs):
        pytest.fail("residual boundary test attempted an external process or network call")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("socket", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden)


@pytest.mark.parametrize("classified", [False, True])
def test_unreadable_ranking_preserves_advice_on_both_classification_paths(monkeypatch, classified):
    """An optional ranking failure must preserve the matched capability itself."""
    caps = {
        "test-cap": {
            "status": "observed",
            "entrypoint": "test.py",
            "matcher": {"task_type": "implement"},
        }
    }
    monkeypatch.setattr(capabilities, "load_declared", lambda *args, **kwargs: caps)
    monkeypatch.setattr(advisor, "binding_suppressed", lambda *args: "")
    monkeypatch.setattr(advisor, "binding_for", lambda *args, **kwargs: {"test-cap": "declared"})
    monkeypatch.setattr(advisor, "surface_status", lambda *args, **kwargs: {"status": "declared"})
    monkeypatch.setattr(advisor, "direct_entry", lambda: {})
    monkeypatch.setattr(
        advisor,
        "classify_task",
        lambda text: (
            [{"task_type": "implement", "hits": ["fixture"], "score": 1}] if classified else []
        ),
    )

    def unreadable_rank(*args, **kwargs):
        raise OSError("isolated rank-store failure")

    monkeypatch.setattr(propensity, "rank", unreadable_rank)
    result = advisor.advise("fixture", surface="test", record=False)
    assert result["useful"] is True
    assert [row["capability_id"] for row in result["capabilities"]] == ["test-cap"]
    assert result["dispatch_ready_count"] == 0


@pytest.mark.parametrize(
    "matcher,trigger,reason",
    [
        ({"kind": ""}, {}, "empty_matcher_kind"),
        ({"lane": "closer"}, {"lane": "opener"}, "lane_mismatch"),
    ],
)
def test_empty_typed_and_mismatched_legacy_matchers_fail_closed(matcher, trigger, reason):
    """Malformed typed and nonmatching legacy declarations never authorize work."""
    assert capabilities._matches_trigger({"matcher": matcher}, trigger) == (False, [reason])


def test_expiry_renewal_with_invalid_prior_state_restores_observed(monkeypatch, tmp_path):
    """A malformed retirement record cannot restore a noncanonical authority state."""
    cap = {"expiry": 1, "status": "retired", "event_history": []}
    saved = []
    monkeypatch.setattr(capabilities, "_locked", lambda path: nullcontext())
    monkeypatch.setattr(
        capabilities, "_read_ledger_unlocked", lambda path: {"capabilities": {"one": cap}}
    )
    monkeypatch.setattr(
        capabilities, "_write_ledger_unlocked", lambda path, rows: saved.append(rows.copy())
    )
    monkeypatch.setattr(capabilities, "renewal_blocker", lambda *args, **kwargs: None)
    monkeypatch.setattr(capabilities, "expiry_retirement", lambda cap: {"from": "unrecognized"})
    monkeypatch.setattr(capabilities, "validate_capability", lambda *args, **kwargs: None)
    result = capabilities.renew(
        "one",
        reason="fixture evidence",
        evidence_refs=["fixture:one"],
        path=tmp_path / "ledger.json",
        timestamp=100,
    )
    assert result["status"] == "observed"
    assert cap["next_transition"] == "retired"
    assert saved[0]["one"]["status"] == "observed"
    assert cap["event_history"][0]["to"] == "observed"


def test_usage_report_explains_all_encoded_gates_without_claiming_promotion():
    """A checkable denominator is distinct from meeting the gate's bounds."""
    usage = {
        "total": 1,
        "active_last_28d": [],
        "never_invoked": ["one"],
        "capabilities_with_verdict": 0,
        "capabilities_with_outcome_link": 0,
        "gate_encoded": ["one"],
        "threshold_undefined": [],
        "rows": [],
    }
    text = capabilities.format_usage_report(usage)
    assert "All 1 gated capabilities have a machine-checkable threshold" in text
    assert "unmet bounds" in text
    assert "READY TO LIFT (gate satisfied; 1 gates are machine-checkable): 0" in text
