"""Dispute evidence, read-only outcomes, strict offers and honest grading."""

import hashlib
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("diff_summary", [None]),
        ("diff_summary", [" "]),
        ("diff_summary", [17]),
        ("diff_summary", [{}]),
        ("diff_summary", [{"path": " "}]),
        ("diff_summary", [{"path": "tests/valid.py"}, None]),
        ("gate_runs", [None]),
        ("gate_runs", [" "]),
        ("gate_runs", [17]),
        ("gate_runs", [{}]),
        ("gate_runs", [{"name": "Gate", "conclusion": "SUCCESS"}]),
        ("gate_runs", ["gate-run", None]),
    ],
)
def test_packet_rejects_blank_or_malformed_evidence_members(field, value):
    row = {"target": "owner/repo#1"}
    packet = evidence(row)
    packet["ground_truth_evidence"][field] = value
    with pytest.raises(ValueError):
        retro.build_packet(row, packet)


def test_packet_accepts_complete_diff_and_both_gate_context_shapes():
    row = {"target": "owner/repo#1"}
    packet = evidence(row)
    packet["ground_truth_evidence"] = {
        "diff_summary": [{"path": "tests/valid.py", "additions": 1, "deletions": 0}],
        "gate_runs": [
            {"name": "Gate", "conclusion": "SUCCESS", "detailsUrl": "gate-run"},
            {"context": "Gate", "state": "SUCCESS", "targetUrl": "gate-status"},
        ],
    }
    assert (
        retro.build_packet(row, packet)["ground_truth_evidence"] == packet["ground_truth_evidence"]
    )


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
    assert result["summary"]["agree"] == 0
    assert result["summary"]["adjudicated"] == 0
    assert result["summary"]["proposed_decisions"] == 1
    assert result["summary"]["merge_rule_agreement_rate"] is None
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
    persisted = json.loads(path.read_text())
    assert persisted["summary"]["cost_usd"] == 2
    assert persisted["rows"][0]["role_run_id"] == result["rows"][0]["role_run_id"]
    assert "cost 2.0" in retro.weekly_line(path)
    with sqlite3.connect(private_brain) as conn:
        conn.execute("UPDATE outcomes SET durability='durable' WHERE run_id='original'")
    before = outcomes()
    refreshed = retro.run(dispatch=True, limit=0, path=path, db=private_brain)
    persisted = json.loads(path.read_text())
    assert persisted == refreshed
    assert persisted["rows"][0]["later_truth"] == "PASS"
    assert persisted["summary"]["disagree"] == 0
    assert outcomes() == before


@pytest.mark.parametrize(
    "context,offered",
    [
        ({}, False),
        ({"verifier_verdict": "PASS", "merge_disposition": "PASS"}, False),
        ({"verifier_verdict": "pass", "merge_disposition": " PASS "}, False),
        ({"verifier_verdict": "CONCERNS ", "merge_disposition": "concerns"}, False),
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
        missing = result["fact_missing"]
        assert bool(withheld or missing) is not offered
        if not context:
            assert withheld == []
            assert missing == [
                {
                    "capability_id": "role-adjudicator",
                    "surface": "closer-lane",
                    "fact": "verifier verdict or merge disposition unknown",
                }
            ]
        else:
            assert missing == []
        if withheld:
            assert withheld[0]["capability_id"] == "role-adjudicator"


def test_adjudicator_annotation_preserves_membership_when_verdict_is_unknown():
    for facts in (
        {},
        {"verifier_verdict": "UNKNOWN", "merge_disposition": "PASS"},
        {"verifier_verdict": "NON_PASS", "merge_disposition": "unknown"},
        {"verifier_verdict": " unknown ", "merge_disposition": "UNKNOWN"},
        {"verifier_verdict": [None], "merge_disposition": "PASS"},
        {"verifier_verdict": "PASS", "merge_disposition": {}},
    ):
        entries = [{"capability_id": "role-adjudicator"}]
        entry = entries[0]
        summary = advisor._annotate_preconditions(entries, "owner/repo", "", pr_facts=facts)
        assert entries == [entry]
        assert summary["declared"] == ["role-adjudicator"]
        assert "role-adjudicator" in summary["unevaluated"]
        advisor._filter_contested_verdict_offers(entries, summary)
        assert entries == []


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


def test_missing_verifier_comment_uses_real_selector(monkeypatch):
    pr = {
        "state": "MERGED",
        "mergeCommit": {"oid": "a" * 40},
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "comments": {"pageInfo": {"hasPreviousPage": False}, "nodes": [{}]},
    }
    monkeypatch.setattr(
        retro, "_gh_json", lambda _args: {"data": {"repository": {"pullRequest": pr}}}
    )
    with pytest.raises(
        ValueError, match="current merge-bound verifier decision missing or changed"
    ):
        retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})


def test_marker_only_finding_does_not_dispatch(private_brain, tmp_path):
    packet = evidence({})
    packet["disputed_finding"][
        "body"
    ] = '<!-- verifier-corpus-decision/v1 {"verdict":"NON_PASS"} -->'
    result = retro.run(
        dispatch=True,
        path=tmp_path / "report.json",
        db=private_brain,
        evidence_reader=lambda _row: packet,
        runner=lambda **_kw: pytest.fail("marker-only evidence must not dispatch"),
    )
    assert "finding comment text" in result["rows"][0]["error"]


def test_concurrent_report_writes_publish_complete_private_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Barrier

    path = tmp_path / "report.json"
    barrier = Barrier(2)
    original_replace = Path.replace
    temporary_paths = []

    def synchronized_replace(source, target):
        temporary_paths.append(source)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", synchronized_replace)
    rows = [[{"case_id": "first"}], [{"case_id": "second"}]]

    def concurrent_writer(row):
        # Writers start together; replacement is now intentionally serialized
        # with native publication, so a barrier inside replace would deadlock.
        barrier.wait(timeout=10)
        return retro._persist_report(path, row, 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_writer, rows))
    assert len(set(temporary_paths)) == 2
    assert json.loads(path.read_text()) in results
    assert all(not p.exists() for p in temporary_paths)


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


def _complete_packet():
    revision = "a" * 40
    return {
        "ground_truth_evidence": {
            "diff_patch": {"revision": revision, "text": "diff --git a/x.py b/x.py\n"},
            "acceptance_criteria": [{"text": "a requirement", "revision": revision}],
        }
    }


def _metadata_packet():
    return {"ground_truth_evidence": {"diff_summary": [], "gate_runs": []}}


def _advice_row(packet, decision, truth):
    # The role records needs_more_evidence for every retrospective-source case, whatever it decided.
    return {
        "packet": packet,
        "decision": decision,
        "disposition": "needs_more_evidence",
        "later_truth": truth,
        "merge_rule_verdict": "PASS",
    }


def _advice_rows():
    return [
        _advice_row(_complete_packet(), "reject_blocker", "PASS"),  # agrees
        _advice_row(_complete_packet(), "uphold_blocker", "PASS"),  # disagrees
        _advice_row(_metadata_packet(), "reject_blocker", "PASS"),  # counts and statuses only
        _advice_row(_complete_packet(), "needs_more_evidence", "PASS"),  # abstained
        _advice_row(_complete_packet(), "reject_blocker", None),  # no later truth yet
    ]


def test_advice_graded_counts_committed_decisions_on_complete_packets_beside_the_strict_graded():
    summary = retro.summarize(_advice_rows())
    assert summary["advice_graded"] == 2
    assert summary["advice_agree"] == 1
    assert summary["advice_disagree"] == 1
    # The strict number is untouched: the role's plan, not the model's decision, is what it counts.
    assert summary["graded"] == 0 and summary["agree"] == 0 and summary["disagree"] == 0
    assert summary["adjudicated"] == 0 and summary["proposed_decisions"] == 4


def test_weekly_line_ends_with_the_advice_count_and_keeps_every_earlier_field(tmp_path):
    rows = _advice_rows()
    legacy = retro.summarize(rows)
    for key in ("advice_graded", "advice_agree", "advice_disagree"):
        legacy.pop(key)
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"summary": legacy, "rows": rows}))
    line = retro.weekly_line(path)
    assert line.endswith("advice graded 2 (agree 1, disagree 1))")
    assert "graded 0; costs measured" in line and "retry pending" in line
    # A fresh summary carries the keys and prints the same line.
    path.write_text(json.dumps({"summary": retro.summarize(rows), "rows": rows}))
    assert retro.weekly_line(path) == line
    # A summary that predates the field, with no rows to count from, is UNKNOWN and never 0.
    path.write_text(json.dumps({"summary": legacy}))
    unknown = retro.weekly_line(path)
    assert unknown.endswith("advice graded UNKNOWN)") and "advice graded 0" not in unknown


def test_advice_graded_is_a_measured_zero_when_nothing_qualifies(tmp_path):
    rows = [
        _advice_row(_metadata_packet(), "uphold_blocker", "FAIL"),
        _advice_row(_complete_packet(), "needs_more_evidence", "PASS"),
    ]
    summary = retro.summarize(rows)
    assert summary["advice_graded"] == 0
    assert summary["advice_agree"] == 0 and summary["advice_disagree"] == 0
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"summary": summary, "rows": rows}))
    assert retro.weekly_line(path).endswith("advice graded 0 (agree 0, disagree 0))")
    assert retro.weekly_line(tmp_path / "missing.json").startswith(
        "adjudicator shadow: cases UNKNOWN"
    )


def test_saved_error_rows_do_not_gain_window_dependent_grades(private_brain, tmp_path):
    path = tmp_path / "report.json"
    retro.run(dispatch=False, path=path, db=private_brain, evidence_reader=evidence)
    report = json.loads(path.read_text())
    for row in report["rows"]:
        row.pop("decision", None)
        row["error"] = "retained incomplete assessment"
        row["later_truth"] = None
        row["cost_usd"] = None
    path.write_text(json.dumps(report))
    result = retro.run(dispatch=False, limit=0, path=path, db=private_brain)
    assert result["rows"]
    assert all(row["later_truth"] is None for row in result["rows"])


@pytest.mark.parametrize("decision", ["uphold_blocker", "reject_blocker"])
@pytest.mark.parametrize("flag", [None, False, True])
def test_retrospective_metadata_cannot_upgrade_raw_proposal(decision, flag):
    case = {"target": "owner/repo#1", "source": "retrospective", **evidence({})}
    if flag is not None:
        case["metadata_only"] = flag
    raw = {**proposal(), "decision": decision}
    result = roles.run_adjudicator_agent(case=case, backend="gemini", proposal_json=raw)
    assert result["proposal"] == raw
    assert result["advisory_plan"]["decision"] == "needs_more_evidence"
    assert result["advisory_plan"]["evidence_gaps"]
    assert result["decision_source"] == "metadata_only_needs_more_evidence"
    assert result["case"].get("metadata_only") is flag


def test_non_retrospective_adjudication_retains_existing_contract():
    case = {"target": "owner/repo#1", **evidence({})}
    raw = proposal()
    result = roles.run_adjudicator_agent(case=case, backend="gemini", proposal_json=raw)
    assert result["advisory_plan"] == raw
    assert result["decision_source"] == "adjudicator_agent"


def test_five_real_metadata_proposals_keep_raw_history_without_grades(tmp_path, private_brain):
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures/adjudicator-retro-metadata-proposals.json"
    rows = json.loads(fixture.read_text())
    raw = json.loads(json.dumps(rows))
    report = retro._persist_report(tmp_path / "retro.json", rows, 122, db=private_brain)
    assert len(rows) == 5
    for current, original in zip(report["rows"], raw):
        assert current["proposal"] == original["proposal"]
        assert current["decision"] == original["decision"]
        assert current["backend_run_id"] == original["backend_run_id"]
        assert current["disposition"] == "needs_more_evidence"
        assert current["shadow_verdict"] is None
    assert report["summary"]["proposed_decisions"] == 5
    assert report["summary"]["adjudicated"] == report["summary"]["graded"] == 0
    assert report["summary"]["agreement_rate"] is None


def test_legacy_false_flag_and_saved_decisions_resume_without_redispatch(private_brain, tmp_path):
    path = tmp_path / "retro.json"
    first = retro.run(dispatch=False, path=path, db=private_brain, evidence_reader=evidence)
    row = first["rows"][0]
    row.update(
        decision="reject_blocker",
        proposal={**proposal(), "decision": "reject_blocker"},
        shadow_verdict="PASS",
        metadata_only=False,
        backend_run_id="backend-one",
    )
    row["packet"].pop("metadata_only", None)
    row["packet"]["metadata_only"] = False
    row["packet"]["ground_truth_evidence"].update(
        {
            "diff_patch": {"revision": "head", "text": "diff --git a/file"},
            "acceptance_criteria": [{"text": "named test passes", "revision": "head"}],
        }
    )
    row["disposition"] = "reject_blocker"
    path.write_text(json.dumps(first))
    with sqlite3.connect(private_brain) as conn:
        conn.execute("UPDATE costs SET cost_usd=2,source='ccusage' WHERE run_id='backend-one'")
        before = conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()
    raw = row["proposal"].copy()
    resumed = retro.run(
        dispatch=True,
        limit=5,
        path=path,
        db=private_brain,
        runner=lambda **kw: pytest.fail("saved case must not redispatch"),
    )
    with sqlite3.connect(private_brain) as conn:
        assert conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall() == before
    saved = resumed["rows"][0]
    assert saved["proposal"] == raw
    assert saved["decision"] == "reject_blocker"
    assert saved["shadow_verdict"] == "PASS"
    assert saved["disposition"] == "reject_blocker"
    assert resumed["summary"]["cost_usd"] == 2
    assert resumed["summary"]["adjudicated"] == resumed["summary"]["graded"] == 1


@pytest.mark.parametrize("paginated", [False, True])
def test_missing_gate_commit_is_evidence_gap(monkeypatch, paginated):
    decision = {"verdict": "NON_PASS"}
    pr = {
        "state": "MERGED",
        "headRefOid": "b" * 40,
        "mergeCommit": {"oid": "a" * 40},
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "comments": {
            "pageInfo": {"hasPreviousPage": False},
            "nodes": [{"body": "Finding", "url": "finding"}],
        },
        "commits": {"nodes": []},
    }
    fresh = {"headRefOid": pr["headRefOid"], "commits": {"nodes": []}}
    if paginated:
        pr["commits"] = {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                                "nodes": [],
                            }
                        }
                    }
                }
            ]
        }
    replies = iter([pr, fresh])
    monkeypatch.setattr(retro.verifier_evidence, "decision_from_pr", lambda *_: decision)
    monkeypatch.setattr(
        retro, "_gh_json", lambda _: {"data": {"repository": {"pullRequest": next(replies)}}}
    )
    with pytest.raises(ValueError, match="complete gate evidence unavailable"):
        retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})


@pytest.mark.parametrize("paginated", [False, True])
@pytest.mark.parametrize(
    "rollup",
    [
        {"other": True},
        {"contexts": None},
        {"contexts": {}},
        {"contexts": {"nodes": None, "pageInfo": {"hasNextPage": False}}},
        {"contexts": {"nodes": [], "pageInfo": None}},
        {"contexts": {"nodes": [], "pageInfo": {"hasNextPage": True}}},
        {"contexts": {"nodes": [None], "pageInfo": {"hasNextPage": False}}},
        {"contexts": {"nodes": [], "pageInfo": {}}},
    ],
    ids=[
        "missing",
        "null",
        "empty",
        "null-nodes",
        "null-page",
        "missing-cursor",
        "null-node",
        "missing-next",
    ],
)
def test_malformed_gate_contexts_are_evidence_gap(monkeypatch, paginated, rollup):
    decision = {"verdict": "NON_PASS"}
    pr = {
        "state": "MERGED",
        "headRefOid": "b" * 40,
        "mergeCommit": {"oid": "a" * 40},
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "comments": {
            "pageInfo": {"hasPreviousPage": False},
            "nodes": [{"body": "Finding", "url": "finding"}],
        },
        "commits": {"nodes": []},
    }
    fresh = {"headRefOid": pr["headRefOid"], "commits": {"nodes": []}}
    if paginated:
        pr["commits"] = {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                                "nodes": [],
                            }
                        }
                    }
                }
            ]
        }
    malformed = {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}
    if paginated:
        fresh["commits"] = malformed
    else:
        pr["commits"] = malformed
    replies = iter([pr, fresh])
    monkeypatch.setattr(retro.verifier_evidence, "decision_from_pr", lambda *_: decision)
    monkeypatch.setattr(
        retro, "_gh_json", lambda _: {"data": {"repository": {"pullRequest": next(replies)}}}
    )
    with pytest.raises(ValueError, match="complete gate evidence unavailable"):
        retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})


HISTORICAL_RECOMMENDATION_PROSE = [
    "Recommend merge after manual review once gate logs confirm the regression.",
    "After manual review, merge may be appropriate once gates are green.",
    "Inspect whether labeling is appropriate after reviewing the diff.",
    "Human review is required before any merge decision.",
    "The maintainer may delegate follow-up to the runtime owner after inspection.",
    "Opening a follow-up PR may be warranted after acceptance is documented.",
    "Commit history needs inspection before disposition.",
    "Inspect the logs; recommend merge after manual review.",
]


def test_build_packet_includes_diff_and_acceptance_criteria():
    row = {"target": "owner/repo#1"}
    evidence = {
        "disputed_finding": {"ref": "finding", "body": "Finding body"},
        "ground_truth_evidence": {
            "diff_summary": [{"path": "tests/a.py", "additions": 1, "deletions": 0}],
            "gate_runs": [{"name": "Gate", "conclusion": "SUCCESS", "detailsUrl": "gate"}],
            "diff_patch": {"revision": "abc", "text": "diff --git a/tests/a.py"},
            "acceptance_criteria": [{"text": "pytest passes", "revision": "abc"}],
        },
    }
    packet = retro.build_packet(row, evidence)
    assert packet["metadata_only"] is False
    assert packet["ground_truth_evidence"]["diff_patch"]["text"].startswith("diff")
    assert packet["ground_truth_evidence"]["acceptance_criteria"][0]["text"] == "pytest passes"


def test_adjudicator_validator_accepts_recommendation_prose():
    role = roles.ROLE_REGISTRY["adjudicator"]
    base = proposal()
    for text in HISTORICAL_RECOMMENDATION_PROSE:
        assert role.validate({**base, "recommended_next_step": text}) == []
    assert role.validate({**base, "recommended_next_step": "merge this PR now"}) != []
    for text in (
        "open a PR",
        "create a branch",
        "Please open PR",
        "Please create branch",
        "commit the changes",
        "Commit changes to the branch",
        "Please commit updates",
        "Inspect the logs; merge this PR now",
        "Inspect the logs. Push the branch",
    ):
        assert role.validate({**base, "recommended_next_step": text}) != []


def test_floor_still_demotes_a_metadata_only_packet():
    metadata_row = {
        "decision": "reject_blocker",
        "packet": {"metadata_only": True},
        "later_truth": "PASS",
        "shadow_verdict": "PASS",
    }
    retro._apply_evidence_floor(metadata_row)
    assert metadata_row["disposition"] == "needs_more_evidence"
    summary = retro.summarize([metadata_row])
    assert summary["graded"] == 0

    full_row = {
        "decision": "reject_blocker",
        "disposition": "reject_blocker",
        "packet": {
            "metadata_only": False,
            "ground_truth_evidence": {
                "diff_patch": {"revision": "a", "text": "patch"},
                "acceptance_criteria": [{"text": "named test passes", "revision": "a"}],
            },
        },
        "later_truth": "PASS",
        "merge_rule_verdict": "PASS",
        "shadow_verdict": "PASS",
    }
    summary = retro.summarize([full_row])
    assert summary["graded"] == 1


def test_abstained_row_is_reattempted_when_the_packet_gains_diff_and_criteria(
    private_brain, tmp_path, monkeypatch
):
    path = tmp_path / "retro.json"
    population = [
        {
            "run_id": "r1",
            "target": "owner/repo#1",
            "verifier_verdict": "NON_PASS",
            "adjudicated_verdict": "PASS",
            "merged": 1,
        }
    ]
    monkeypatch.setattr(retro, "disputes", lambda **_: population)
    identity = {
        "run_id": "r1",
        "verifier_verdict": "NON_PASS",
        "adjudicated_verdict": "PASS",
    }
    case_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    saved = {
        **identity,
        "case_id": case_id,
        "target": "owner/repo#1",
        "decision": "needs_more_evidence",
        "packet": {"metadata_only": True, "target": "owner/repo#1"},
        "backend_run_id": "paid-attempt-1",
        "cost_usd": 0.12,
    }
    path.write_text(json.dumps({"rows": [saved], "population": 1}))
    calls = {"n": 0}

    def reader(_row):
        calls["n"] += 1
        return {
            "disputed_finding": {"ref": "f", "body": "Finding"},
            "ground_truth_evidence": {
                "diff_summary": [{"path": "a.py", "additions": 1, "deletions": 0}],
                "gate_runs": [{"name": "Gate", "conclusion": "SUCCESS", "detailsUrl": "g"}],
                "diff_patch": {"revision": "sha", "text": "diff"},
                "acceptance_criteria": [{"text": "test passes", "revision": "sha"}],
            },
        }

    retro.run(
        dispatch=False,
        retry=True,
        limit=1,
        path=path,
        db=private_brain,
        evidence_reader=reader,
    )
    assert calls["n"] == 1
    refreshed = json.loads(path.read_text())["rows"][0]
    assert refreshed["case_id"] == case_id
    assert refreshed["backend_run_id"] == "paid-attempt-1"
    assert refreshed["packet"]["metadata_only"] is True

    failed = retro.run(
        dispatch=True,
        retry=True,
        limit=1,
        path=path,
        db=private_brain,
        evidence_reader=reader,
        runner=lambda **_: {"errors": ["runner failed"], "backend_run_id": "paid-failed-retry"},
    )["rows"][0]
    assert failed["packet"]["metadata_only"] is True
    assert failed["backend_run_id"] == "paid-attempt-1"
    assert failed["retry_attempts"][-1]["backend_run_id"] == "paid-failed-retry"
    succeeded = retro.run(
        dispatch=True,
        retry=True,
        limit=1,
        path=path,
        db=private_brain,
        evidence_reader=reader,
        runner=lambda **_: {"proposal": proposal(), "backend_run_id": "paid-success"},
    )["rows"][0]
    assert succeeded["packet"]["metadata_only"] is False
    assert succeeded["decision"] == "uphold_blocker"
    assert succeeded["backend_run_id"] == "paid-success"
    assert len(json.loads(path.read_text())["rows"]) == 1

    identity = {
        "run_id": "r1",
        "verifier_verdict": "NON_PASS",
        "adjudicated_verdict": "PASS",
    }
    case_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    decided = {
        **saved,
        "case_id": case_id,
        "decision": "reject_blocker",
        "packet": {"metadata_only": True},
    }
    path.write_text(json.dumps({"rows": [decided], "population": 1}))
    calls["n"] = 0
    retro.run(
        dispatch=False,
        retry=True,
        limit=1,
        path=path,
        db=private_brain,
        evidence_reader=lambda _row: pytest.fail("must not re-read decided row"),
    )
    assert calls["n"] == 0


def test_run_passes_the_dispatch_timeout_and_records_it(private_brain, tmp_path, monkeypatch):
    """The retrospective loop spends one named budget and saves it on the attempted row."""
    monkeypatch.setattr(
        retro,
        "disputes",
        lambda **_: [
            {
                "run_id": "r1",
                "target": "owner/repo#1",
                "verifier_verdict": "NON_PASS",
                "adjudicated_verdict": "PASS",
                "merged": 1,
            }
        ],
    )
    seen = []

    def runner(**kwargs):
        seen.append(kwargs)
        return {"errors": ["fake runner"], "backend_run_id": "fake-backend"}

    def attempt(path, **options):
        return retro.run(
            dispatch=True,
            limit=1,
            path=path,
            db=private_brain,
            evidence_reader=evidence,
            runner=runner,
            **options,
        )["rows"][0]

    row = attempt(tmp_path / "default.json")
    assert seen[-1]["timeout"] == retro.DISPATCH_TIMEOUT_S
    assert row["dispatch_timeout_s"] == retro.DISPATCH_TIMEOUT_S

    row = attempt(tmp_path / "custom.json", timeout=77)
    assert seen[-1]["timeout"] == 77
    assert row["dispatch_timeout_s"] == 77

    # A saved metadata-only abstention that is retried and fails keeps the budget on its history.
    identity = {"run_id": "r1", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"}
    saved = {
        **identity,
        "case_id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        "target": "owner/repo#1",
        "decision": "needs_more_evidence",
        "packet": {"metadata_only": True, "target": "owner/repo#1"},
        "backend_run_id": "paid-attempt-1",
        "cost_usd": 0.12,
    }
    retry_path = tmp_path / "retry.json"
    retry_path.write_text(json.dumps({"rows": [saved], "population": 1}))
    row = attempt(retry_path, retry=True, timeout=77)
    assert seen[-1]["timeout"] == 77
    assert row["retry_attempts"][-1]["dispatch_timeout_s"] == 77


def test_dispatch_timeout_default_equals_the_role_default(monkeypatch, capsys):
    """Pin the shared default and the named CLI override/rejection contract."""
    import inspect
    import sys

    role_default = inspect.signature(roles.run_adjudicator_agent).parameters["timeout"].default
    assert retro.DISPATCH_TIMEOUT_S == role_default

    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {"summary": {"cases": 0}}

    monkeypatch.setattr(retro, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["adjudicator_retro.py", "--dispatch", "--timeout", "77"])
    assert retro.main() == 0
    assert seen[-1]["timeout"] == 77
    before = len(seen)
    monkeypatch.setattr(sys, "argv", ["adjudicator_retro.py", "--dispatch", "--timeout", "0"])
    with pytest.raises(SystemExit) as exit_info:
        retro.main()
    assert exit_info.value.code == 2
    assert len(seen) == before, "a rejected budget must not reach run()"
    assert "--timeout must be at least 1 second" in capsys.readouterr().err


def test_main_passes_the_timeout_option_and_rejects_a_budget_below_one(monkeypatch, capsys):
    import sys

    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {"summary": {"cases": 0}}

    monkeypatch.setattr(retro, "run", fake_run)

    def invoke(*argv):
        monkeypatch.setattr(sys, "argv", ["adjudicator_retro.py", *argv])
        return retro.main()

    assert invoke("--dispatch", "--limit", "1") == 0
    assert seen[-1]["timeout"] == retro.DISPATCH_TIMEOUT_S
    assert invoke("--dispatch", "--limit", "1", "--timeout", "77") == 0
    assert seen[-1]["timeout"] == 77
    for bad in ("0", "-5"):
        before = len(seen)
        with pytest.raises(SystemExit) as exit_info:
            invoke("--dispatch", "--timeout", bad)
        assert exit_info.value.code == 2
        assert len(seen) == before, "a rejected budget must not reach run()"
    assert "--timeout must be at least 1 second" in capsys.readouterr().err


def test_judge_collected_case_dispatches_with_the_configured_timeout(tmp_path, monkeypatch):
    import sys

    report = tmp_path / "report.json"
    report.write_text(json.dumps({"rows": [{"case_id": "case-1"}]}))
    qualification = tmp_path / "qualification.json"
    qualification.write_text("{}")
    repository = tmp_path / "repository"
    repository.mkdir()
    seen = []

    def fake_agent(**kwargs):
        seen.append(kwargs)
        return {
            "shadow": True,
            "decision_source": "fake",
            "backend_run_id": "fake-backend",
            "evidence_qualification": {},
            "errors": [],
        }

    monkeypatch.setattr(retro, "qualify_collected_case", lambda *a, **kw: {"qualified": True})
    monkeypatch.setattr(roles, "run_adjudicator_agent", fake_agent)

    def invoke(output, *extra):
        argv = [
            "adjudicator_retro.py",
            "--judge-collected-case",
            "case-1",
            "--report",
            str(report),
            "--repository",
            str(repository),
            "--qualification",
            str(qualification),
            "--output",
            str(output),
            "--dispatch",
            *extra,
        ]
        monkeypatch.setattr(sys, "argv", argv)
        return retro.main()

    assert invoke(tmp_path / "default.json") == 0
    assert seen[-1]["timeout"] == retro.DISPATCH_TIMEOUT_S
    assert invoke(tmp_path / "custom.json", "--timeout", "77") == 0
    assert seen[-1]["timeout"] == 77
    assert (tmp_path / "custom.json").exists()


def test_retry_spends_the_limit_on_saved_pending_rows_before_new_disputes(
    private_brain, tmp_path, monkeypatch
):
    """Under --retry the budget goes to saved rows awaiting a retry before newer unsaved disputes."""
    identities = {
        "newer": {"run_id": "newer", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"},
        "older": {"run_id": "older", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"},
    }
    newer = {**identities["newer"], "target": "owner/repo#2", "merged": 1}
    older = {**identities["older"], "target": "owner/repo#1", "merged": 1}
    monkeypatch.setattr(retro, "disputes", lambda **_: [newer, older])  # newest first, as the feed
    saved = {
        **identities["older"],
        "case_id": hashlib.sha256(
            json.dumps(identities["older"], sort_keys=True).encode()
        ).hexdigest(),
        "target": "owner/repo#1",
        "decision": "needs_more_evidence",
        "packet": {"metadata_only": True, "target": "owner/repo#1"},
    }
    asked = []

    def reader(row):
        asked.append(row["target"])
        return evidence(row)

    def attempt(name, **options):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"rows": [saved], "population": 2}))
        asked.clear()
        retro.run(dispatch=False, path=path, db=private_brain, evidence_reader=reader, **options)
        return list(asked)

    assert attempt("one", retry=True, limit=1) == ["owner/repo#1"]
    assert attempt("two", retry=True, limit=2) == ["owner/repo#1", "owner/repo#2"]
    assert attempt("plain", retry=False, limit=1) == ["owner/repo#2"]
    by_id = {saved["case_id"]: saved}
    assert [r["run_id"] for r in retro.attempt_order([newer, older], by_id, retry=True)] == [
        "older",
        "newer",
    ]
    assert [r["run_id"] for r in retro.attempt_order([newer, older], by_id, retry=False)] == [
        "newer",
        "older",
    ]


def test_retry_pending_counts_saved_rows_and_prints_zero_when_drained(
    private_brain, tmp_path, monkeypatch
):
    identity = {"run_id": "r1", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"}
    monkeypatch.setattr(
        retro, "disputes", lambda **_: [{**identity, "target": "owner/repo#1", "merged": 1}]
    )
    pending = {
        **identity,
        "case_id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        "target": "owner/repo#1",
        "decision": "needs_more_evidence",
        "packet": {"metadata_only": True, "target": "owner/repo#1"},
    }

    def publish(saved_row, name):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"rows": [saved_row], "population": 1}))
        return path, retro.run(dispatch=False, limit=0, path=path, db=private_brain)["summary"]

    path, summary = publish(pending, "pending")
    assert summary["retry_pending"] == 1
    assert "retry pending 1" in retro.weekly_line(path)

    path, summary = publish({**pending, "decision": "reject_blocker"}, "drained")
    assert summary["retry_pending"] == 0 and summary["retry_pending"] is not None
    assert "retry pending 0" in retro.weekly_line(path)

    legacy = json.loads(path.read_text())
    del legacy["summary"]["retry_pending"]
    path.write_text(json.dumps(legacy))
    assert "retry pending UNKNOWN" in retro.weekly_line(path)


def test_retry_pending_reproduces_the_original_skip_rule():
    """Moving the skip test into one helper must not change which saved rows are attempted."""
    complete = {
        "ground_truth_evidence": {
            "diff_patch": {"revision": "a", "text": "patch"},
            "acceptance_criteria": [{"text": "named test passes", "revision": "a"}],
        }
    }
    packets = {
        "metadata_only": {"metadata_only": True, "target": "owner/repo#1"},
        "complete": {"metadata_only": False, **complete},
    }

    def original_attempts(old, retry):
        # The skip test as it stood before the helper existed, restated literally.
        reattempt = (
            retry
            and old
            and old.get("decision") == "needs_more_evidence"
            and retro._row_metadata_only(old)
        )
        skipped = old and not reattempt and (old.get("decision") or not retry)
        return not skipped

    for retry in (False, True):
        for decision in (None, "needs_more_evidence", "reject_blocker", "uphold_blocker"):
            for shape, packet in packets.items():
                old = {"decision": decision, "packet": packet}
                assert retro._retry_pending(old, retry=retry) == original_attempts(old, retry), (
                    retry,
                    decision,
                    shape,
                )
        assert retro._retry_pending(None, retry=retry) is False
