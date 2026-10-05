"""Dispute evidence, read-only outcomes, strict offers and honest grading."""

import json
import sqlite3
import time

import pytest

import adjudicator_retro as retro
import capabilities
import capability_advisor as advisor
import feedback
import roles


def evidence(_row):
    return {
        "disputed_finding": {"ref": "verifier-comment", "body": "Missing acceptance test"},
        "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
    }


def test_packet_builder_needs_target_finding_and_ground_truth():
    row = {"target": "owner/repo#1"}
    assert retro.build_packet(row, evidence(row))["source"] == "retrospective"
    for key in ("disputed_finding", "ground_truth_evidence"):
        packet = evidence(row)
        packet.pop(key)
        with pytest.raises(ValueError):
            retro.build_packet(row, packet)
    with pytest.raises(ValueError):
        retro.build_packet({}, evidence(row))


@pytest.fixture
def private_brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    ledger = tmp_path / "capabilities.json"
    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(ledger))
    with feedback._conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES ('original',?,'owner/repo#1')",
            (int(time.time()),),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged,durability,durability_checked_ts) VALUES ('original','NON_PASS','PASS',1,'broke_later',?)",
            (int(time.time()),),
        )
        conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend-one',0,'ledger')")
    return db


def proposal():
    return {
        "decision": "uphold_blocker",
        "confidence": "high",
        "rationale": "The later regression confirms the missing criterion.",
        "evidence_assessment": [
            {
                "claim": "Missing test",
                "status": "supported",
                "evidence_ref": "gate-run",
                "reason": "The regression is reproducible",
            }
        ],
        "ground_truth_refs": ["gate-run"],
        "recommended_next_step": "Inspect the regression evidence",
        "evidence_gaps": [],
    }


def test_retro_records_verdicts_without_changing_outcomes(private_brain, tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: {"agent": "gemini"})
    monkeypatch.setattr(roles, "_role_capability_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        roles.dispatcher,
        "offload",
        lambda *a, **kw: {
            "run_id": "backend-one",
            "output": json.dumps(proposal()),
            "exit": 0,
        },
    )

    def outcomes():
        with sqlite3.connect(private_brain) as conn:
            return conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()

    before = outcomes()
    path = tmp_path / "report.json"
    result = retro.run(dispatch=True, path=path, db=private_brain, evidence_reader=evidence)
    assert (
        outcomes() == before
    )  # includes absence of NEW outcome rows, not just old verdict equality
    assert result["summary"]["agree"] == 1
    assert result["summary"]["merge_rule_agreement_rate"] == 0
    assert result["summary"]["cost_usd"] is None
    with sqlite3.connect(private_brain) as conn:
        recorded = conn.execute(
            "SELECT source,decomposition FROM runs WHERE role_name='adjudicator'"
        ).fetchone()
    assert recorded[0] == "retrospective"
    assert json.loads(recorded[1])["proposal"]["decision"] == "uphold_blocker"
    with sqlite3.connect(private_brain) as conn:
        conn.execute("UPDATE costs SET cost_usd=2,source='ccusage' WHERE run_id='backend-one'")
    again = retro.run(dispatch=True, path=path, db=private_brain, evidence_reader=evidence)
    assert again["rows"][0]["role_run_id"] == result["rows"][0]["role_run_id"]
    assert outcomes() == before
    assert again["summary"]["cost_usd"] == 2


@pytest.mark.parametrize(
    "context,offered",
    [
        ({}, False),
        ({"verifier_verdict": "PASS", "merge_disposition": "PASS"}, False),
        ({"verifier_verdict": "NON_PASS", "merge_disposition": "PASS"}, True),
    ],
)
def test_closer_lane_offers_the_adjudicator_only_on_a_contested_verdict(
    tmp_path, monkeypatch, context, offered
):
    ledger = tmp_path / "ledger.json"
    row = capabilities._blank_capability("role-adjudicator")
    row["status"] = "generated"
    capabilities.save({"role-adjudicator": row}, ledger)
    monkeypatch.setattr(advisor, "PR_FACTS_FETCH", lambda *a: {})
    for text in ("xyzzy plugh", "review disputed verifier results"):
        result = advisor.advise(
            text,
            surface="closer-lane",
            repository="owner/repo",
            context=context,
            record=False,
            path=ledger,
        )
        ids = {m["capability_id"] for m in result["capabilities"]}
        assert ("role-adjudicator" in ids) is offered
        withheld = result["precondition"]["withheld"]
        assert bool(withheld) is not offered
        if withheld:
            assert withheld[0]["capability_id"] == "role-adjudicator"


def test_adjudicator_annotation_preserves_membership_when_verdict_is_unknown():
    entries = [{"capability_id": "role-adjudicator"}]
    entry = entries[0]
    summary = advisor._annotate_preconditions(entries, "owner/repo", "", pr_facts={})
    assert entries == [entry]
    assert summary["declared"] == ["role-adjudicator"]
    assert "role-adjudicator" in summary["unevaluated"]


def test_mcp_forwards_recorded_verdicts(monkeypatch):
    import mcp_server

    calls = []
    monkeypatch.setattr(advisor, "advise", lambda *args, **kwargs: calls.append(kwargs) or {})
    mcp_server._call_tool(
        "capability_advice",
        {"task": "disputed result", "verifier_verdict": "NON_PASS", "merge_disposition": "PASS"},
    )
    assert calls[0]["context"] == {"verifier_verdict": "NON_PASS", "merge_disposition": "PASS"}
    tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "capability_advice")
    assert {"verifier_verdict", "merge_disposition"} <= tool["inputSchema"]["properties"].keys()


def test_missing_evidence_never_dispatches_or_becomes_agreement(private_brain, tmp_path):
    def refuse(**kw):
        pytest.fail("invalid packet must not dispatch")

    result = retro.run(
        dispatch=True,
        path=tmp_path / "report.json",
        db=private_brain,
        evidence_reader=lambda r: {},
        runner=refuse,
    )
    assert result["rows"][0]["error"]
    assert result["summary"]["agreement_rate"] is None
    assert result["summary"]["graded"] == 0


def test_pending_or_pre_detection_durability_is_not_ground_truth():
    for durability in ("pending", "unjudgeable"):
        assert (
            retro.later_truth({"durability": durability, "durability_checked_ts": int(time.time())})
            is None
        )
    assert retro.later_truth({"durability": "durable", "durability_checked_ts": 1}) is None


def test_weekly_line_names_unmeasured_cost_and_grade_denominator(tmp_path):
    path = tmp_path / "report.json"
    assert "cases UNKNOWN" in retro.weekly_line(path)
    path.write_text(json.dumps({"summary": retro.summarize([{"decision": "needs_more_evidence"}])}))
    line = retro.weekly_line(path)
    assert "cases 1, agree 0, disagree 0, cost UNKNOWN" in line
    assert "graded 0; costs measured 0" in line
