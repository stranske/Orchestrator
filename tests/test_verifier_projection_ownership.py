"""PR-owned verification must not compete with its source-issue delivery credit."""

import json
from pathlib import Path

import pytest
from test_verifier_incomplete_roots import recovered_703

import durability_sweep as sweep
import feedback
import verifier_evidence as verifier


@pytest.fixture(autouse=True)
def isolated_brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def source_delivery(run="issue", issue=668, pr=703):
    feedback.record_run(
        run,
        f"stranske/Orchestrator#{issue}",
        "implement",
        "codex",
        mode="local",
        source="orchestrator_local",
    )
    feedback.record_outcome(
        run,
        merged=True,
        adjudicated_verdict="PASS",
        durability="pending",
        notes=f"local delegate PR #{pr} merged on the branch this run pushed (orch/{issue}-work)",
    )


def pr_owner(run="pr", pr=703, source="keepalive"):
    feedback.record_run(
        run,
        f"stranske/Orchestrator#{pr}",
        "implement",
        "none",
        mode="remote",
        source=source,
        pr_number=pr,
    )
    feedback.record_outcome(
        run,
        merged=True,
        notes=f"PR #{pr} merged; durability pending sweep",
    )


def test_source_issue_credit_does_not_compete_with_pr_owned_projection():
    source_delivery()
    pr_owner()
    assert sweep.verifier_candidate_run_ids("stranske/Orchestrator", 703) == {"pr"}
    assert ("pr", "stranske/orchestrator", 703) in sweep._missing_verifier_runs()
    assert not any(row[0] == "issue" for row in sweep._missing_verifier_runs())


def test_prospective_pr_owner_preserves_existing_delivery_fallback():
    source_delivery()
    assert sweep.verifier_candidate_run_ids("stranske/Orchestrator", 703) == {"issue"}
    assert sweep.verifier_candidate_run_ids(
        "stranske/Orchestrator", 703, prospective_run_id="pr"
    ) == {"pr"}


@pytest.mark.parametrize("direct", [False, True])
def test_multiple_equal_role_claims_stay_ambiguous(direct):
    if direct:
        pr_owner("first")
        pr_owner("second")
    else:
        source_delivery("first", issue=668)
        source_delivery("second", issue=669)
    assert sweep.verifier_candidate_run_ids("stranske/Orchestrator", 703) == {
        "first",
        "second",
    }
    assert sweep._missing_verifier_runs() == []


def test_other_delivery_does_not_shadow_source_issue_fallback():
    source_delivery()
    pr_owner(pr=704)
    assert sweep.verifier_candidate_run_ids("stranske/Orchestrator", 703) == {"issue"}
    assert sweep.verifier_candidate_run_ids("stranske/Other", 703) == set()


def test_direct_local_pr_owner_is_not_demoted_by_its_agent_source():
    pr_owner("remote")
    pr_owner("local", source="orchestrator_local")
    assert sweep.verifier_candidate_run_ids("stranske/Orchestrator", 703) == {
        "remote",
        "local",
    }
    assert sweep._missing_verifier_runs() == []


def test_703_projection_retains_delivery_history_without_accepting_source_issue():
    source_delivery()
    pr_owner()
    with feedback._conn() as conn:
        before = conn.execute(
            "SELECT r.*, o.* FROM runs r JOIN outcomes o USING(run_id) WHERE r.run_id='issue'"
        ).fetchone()
    data = json.loads(
        (
            Path(__file__).parent
            / "fixtures/verification_event/older_incomplete_then_complete.json"
        ).read_text()
    )
    recovered = recovered_703(data)
    current = verifier.decision_from_pr(data["repo"], recovered["pr"], include_pending=True)
    assert current["head_sha"] == data["pr"]["headRefOid"]
    assert current["evaluated_sha"] == data["pr"]["mergeCommit"]["oid"]
    assert sweep.verifier_candidate_run_ids(data["repo"], 703) == {"pr"}
    assert verifier.record_decision("pr", data["repo"], 703, current)
    assert not verifier.record_decision("pr", data["repo"], 703, current)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='pr'"
        ).fetchone() == ("PASS",)
        assert (
            conn.execute(
                "SELECT r.*, o.* FROM runs r JOIN outcomes o USING(run_id) WHERE r.run_id='issue'"
            ).fetchone()
            == before
        )
        rows = conn.execute(
            "SELECT payload_json FROM completion_events WHERE run_id='pr' "
            "AND producer='verifier_evidence'"
        ).fetchall()
    retained = [json.loads(row[0])["verification"]["decision"] for row in rows]
    assert len(retained) == 7
    assert sum(row.get("status") == "incomplete" for row in retained) == 2
    assert len({row["verification_event"]["event_id"] for row in retained}) == 2
