"""Revised authoritative requests can complete old non-voting reads of the same material."""

import copy
import json
from pathlib import Path

import pytest
from test_repeat_verification_events import event
from test_verifier_incomplete_roots import decision, incomplete

import verifier_evidence as verifier


@pytest.mark.parametrize("verdict", ["PASS", "NON_PASS"])
def test_revised_request_same_material_supersedes_only_older_incomplete(verdict):
    old = incomplete()
    fresh = decision(2, verdict, verification_event=event(request_sha256="e" * 64))
    before = copy.deepcopy([old, fresh])
    assert verifier.applicable_decision([fresh], [old]) is fresh
    assert [old, fresh] == before


@pytest.mark.parametrize(
    "change",
    ["both_digests", "subject", "newer", "pending", "advisory", "unadmitted", "invalid_digest"],
)
def test_material_equivalence_preserves_existing_refusals(change):
    old = incomplete()
    fresh = decision(2, "PASS", verification_event=event(request_sha256="e" * 64))
    if change == "both_digests":
        fresh["verification_event"] = event("f", request_sha256="e" * 64)
    elif change == "subject":
        fresh["verification_event"] = event(request_sha256="e" * 64, repo="o/other")
    elif change == "newer":
        old["run_id"] = "3"
    elif change == "pending":
        old["status"] = "pending"
    elif change == "advisory":
        fresh["assessment_identity"]["canonical"] = False
    elif change == "unadmitted":
        fresh.pop("result_admission")
    else:
        old["verification_event"]["material_sha256"] = "invalid"
        fresh["verification_event"]["material_sha256"] = "invalid"
    assert verifier.applicable_decision([fresh], [old]) is None


def test_every_old_chain_node_must_match_fixed_complete_anchor():
    old = incomplete()
    middle = incomplete(
        2,
        material="f",
        predecessor_event_id=old["verification_event"]["event_id"],
    )
    fresh = decision(3, "PASS", verification_event=event(request_sha256="e" * 64))
    # Old and middle share request; old and fresh share material. That is not
    # transitive permission to discard middle, which matches neither anchor digest.
    assert verifier.applicable_decision([fresh], [old, middle]) is None


def test_prior_semantic_verdict_is_not_an_incomplete_read():
    old = decision(1, "NON_PASS", verification_event=event())
    fresh = decision(2, "PASS", verification_event=event(request_sha256="e" * 64))
    assert verifier.applicable_decision([old, fresh]) is None


def actual_history():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/verification_event/1817_body_proof_same_material.json"
        ).read_text()
    )


def test_actual_1817_full_history_preserves_six_markers_and_selects_admitted_pass():
    data = actual_history()
    before = copy.deepcopy(data)
    current = verifier.decision_from_pr(data["repo"], data["pr"], include_pending=True)
    assert current.get("verdict") == "PASS"
    assert current["run_id"] == "40142437626"
    assert current["verification_event"]["event_id"] == (
        "1f83acb99686154be2b56a198450c83a48c2e42782237d88aa21c71c466e54e6"
    )
    assert len(current["_lifecycle_history"]) == 6
    assert data == before


def test_actual_1817_private_projection_retains_history_and_ignores_out_of_order_reads(
    monkeypatch, tmp_path
):
    import feedback

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("delivery", "stranske/Manager-Database#1817", "implement", "codex")
    feedback.record_outcome("delivery", merged=True)
    feedback.record_run("source", "stranske/Manager-Database#1816", "implement", "codex")
    feedback.record_outcome("source", merged=False)
    with feedback._conn() as conn:
        before_source = (
            conn.execute("SELECT * FROM runs WHERE run_id='source'").fetchall(),
            conn.execute("SELECT * FROM outcomes WHERE run_id='source'").fetchall(),
        )
    data = actual_history()
    old = copy.deepcopy(data)
    old["pr"]["comments"]["nodes"] = old["pr"]["comments"]["nodes"][:2]

    def parse(value):
        return verifier.decision_from_pr(value["repo"], value["pr"], include_pending=True)

    verifier.record_decision("delivery", data["repo"], 1817, parse(old))
    assert verifier.record_decision("delivery", data["repo"], 1817, parse(data))
    verifier.record_decision("delivery", data["repo"], 1817, parse(old))
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='delivery'"
        ).fetchone() == ("PASS",)
        assert before_source == (
            conn.execute("SELECT * FROM runs WHERE run_id='source'").fetchall(),
            conn.execute("SELECT * FROM outcomes WHERE run_id='source'").fetchall(),
        )
        rows = conn.execute(
            "SELECT payload_json FROM completion_events WHERE run_id='delivery' "
            "AND producer='verifier_evidence'"
        ).fetchall()
    retained = [json.loads(row[0])["verification"]["decision"] for row in rows]
    assert len(retained) == 6
    assert len({row["verification_event"]["event_id"] for row in retained}) == 2
