"""Hermetic regression coverage for coverage-autopilot round 881."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import periodic_report
import repo_knowledge
import tick


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Every target reads and writes only the test's disposable runtime."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_periodic_small_summaries_preserve_unknowns_and_actionable_statuses(monkeypatch, tmp_path):
    assert periodic_report._process_action("renovate").startswith("review Renovate")
    assert periodic_report._process_action("other").startswith("review the recurring")
    assert periodic_report._process_rollup_row((None, 0, 0, 0, 0, 0, 0))["outcome_coverage"] is None
    assert periodic_report.issue_review_reason("x issue_review=Needs_Followup") == "needs_followup"
    assert periodic_report.issue_review_reason(None) is None

    plan = tmp_path / "stage2.json"
    assert periodic_report._stage2_live_plan_summary(plan)["status"] == "missing"
    plan.write_text("not json")
    assert periodic_report._stage2_live_plan_summary(plan)["status"] == "unreadable"
    plan.write_text(
        json.dumps(
            {"generated_at": "bad", "status": "ready", "lane_refused_live_candidate_count": 0}
        )
    )
    live = periodic_report._stage2_live_plan_summary(plan)
    assert live["status"] == "ready" and live["lane_refused_live_candidate_count"] == 0

    assert periodic_report._langsmith_telemetry_summary([], [])["status"] == "dry"
    assert (
        periodic_report._langsmith_telemetry_summary([{"source": "langsmith", "count": 1}], [])[
            "status"
        ]
        == "cost_only"
    )
    assert (
        periodic_report._langsmith_telemetry_summary([], [{"source": "langsmith", "count": 1}])[
            "status"
        ]
        == "trace_only"
    )
    assert (
        periodic_report._langsmith_telemetry_summary(
            [{"source": "langsmith", "count": 1}], [{"source": "langsmith", "count": 1}]
        )["status"]
        == "flowing"
    )

    hypotheses = tmp_path / "hypotheses.json"
    hypotheses.write_text(
        json.dumps(
            [
                {"id": "b", "claim": "x" * 121, "evidence": {}},
                {"id": "a", "evidence": {"status": "supported"}},
            ]
        )
    )
    summary = periodic_report._hypothesis_summary(hypotheses)
    assert summary["status_counts"] == {"supported": 1, "unknown": 1}
    assert summary["hypotheses"][0]["id"] == "a" and summary["hypotheses"][1]["claim"].endswith(
        "..."
    )

    state = tmp_path / "state"
    state.mkdir()
    assert periodic_report._consumer_sync_hygiene_summary(state)["available"] is False
    (state / "consumer-sync-artifact-ingest-report.json").write_text(
        json.dumps(
            {
                "generated_at": 1,
                "artifact_name": "a",
                "repositories": {"o/r": {"hygiene": {"oversized": True, "tracked_bytes": 4}}},
                "hygiene_escalation": {"digest": ["d"], "questions": ["q"], "untrackable_bytes": 2},
            }
        )
    )
    hygiene = periodic_report._consumer_sync_hygiene_summary(state)
    assert hygiene["available"] and hygiene["oversized_repositories"][0]["repository"] == "o/r"


def test_periodic_redirect_statuses_are_read_only(monkeypatch, tmp_path):
    corpus = tmp_path / "redirect.json"
    monkeypatch.setattr(periodic_report.redirect_shadow, "CORPUS_PATH", corpus)
    monkeypatch.setattr(periodic_report.redirect_shadow, "summarize", lambda _p: {"n": 0})
    monkeypatch.setattr(
        periodic_report.redirect_shadow,
        "collect_historical_from_keepalive",
        lambda **kwargs: {"candidate_count": 0, "would_collect": 0},
    )
    assert periodic_report._redirect_stage2_summary()["status"] == "no_proposal_corpus"
    corpus.write_text("{}")
    monkeypatch.setattr(
        periodic_report.redirect_shadow,
        "summarize",
        lambda _p: {"n": 1, "ready_for_analysis": False},
    )
    assert periodic_report._redirect_stage2_summary()["status"] == "collecting_proposals"
    monkeypatch.setattr(
        periodic_report.redirect_shadow,
        "summarize",
        lambda _p: {
            "n": 1,
            "ready_for_analysis": True,
            "ready_for_historical_replay_analysis": True,
            "ready_for_supervised_apply": True,
        },
    )
    assert (
        periodic_report._redirect_stage2_summary()["status"] == "ready_for_supervised_apply_review"
    )


@pytest.mark.parametrize(
    ("summary", "previews", "expected"),
    [
        (
            {"n": 1, "ready_for_analysis": True, "ready_for_historical_replay_analysis": False},
            ({"would_collect": 2}, {"would_collect": 0}),
            "collect_historical_replay",
        ),
        (
            {"n": 1, "ready_for_analysis": True, "ready_for_historical_replay_analysis": False},
            ({"would_collect": 0}, {"would_collect": 2}),
            "collect_calibration_replay",
        ),
        (
            {"n": 1, "ready_for_analysis": True, "ready_for_historical_replay_analysis": True},
            ({"would_collect": 0}, {"would_collect": 0}),
            "linking_outcomes",
        ),
    ],
)
def test_periodic_redirect_followup_statuses(monkeypatch, tmp_path, summary, previews, expected):
    corpus = tmp_path / "redirect.json"
    corpus.write_text("{}")
    monkeypatch.setattr(periodic_report.redirect_shadow, "summarize", lambda _p: summary)
    iterator = iter(previews)
    monkeypatch.setattr(
        periodic_report.redirect_shadow,
        "collect_historical_from_keepalive",
        lambda **kwargs: next(iterator),
    )
    assert periodic_report._redirect_stage2_summary(corpus)["status"] == expected


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ({"ready_for_ab": False}, "collecting_labels"),
        ({"ready_for_ab": True, "failure_outcomes": 0}, "success_heavy_underpowered"),
        (
            {"ready_for_ab": True, "failure_outcomes": 5, "meaningful_disagreements": 0},
            "failure_disagreement_underpowered",
        ),
    ],
)
def test_periodic_keepalive_waits_for_each_readiness_gate(monkeypatch, tmp_path, summary, expected):
    corpus = tmp_path / "keepalive.json"
    corpus.write_text("{}")
    monkeypatch.setattr(periodic_report.keepalive_shadow, "summarize", lambda _p: summary)
    monkeypatch.setattr(
        periodic_report,
        "_redirect_stage2_summary",
        lambda *args: {"ready_for_supervised_apply": False, "recommendation": "collect"},
    )
    assert periodic_report._keepalive_supervisor_summary(corpus)["status"] == expected


def test_periodic_rail_summary_and_numeric_rendering_are_fail_soft(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    assert periodic_report._rail_exercise_summary(state) == {
        "available": False,
        "reason": "no rail-exercise report",
    }
    (state / "rail-exercise-report.json").write_text(
        json.dumps(
            {"generated_at": 2, "tree": "t", "totals": {"passed": 1}, "skipped_named": ["x"]}
        )
    )
    assert periodic_report._rail_exercise_summary(state)["totals"] == {"passed": 1}
    assert periodic_report._fmt_num(None) == "n/a" and periodic_report._fmt_num(2.5) == "2.500"
    assert periodic_report._fmt_delta(None) == "n/a" and periodic_report._fmt_delta(2) == "+2"


def test_tick_issue_body_rechecks_open_state_and_writer_ownership(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"state": "OPEN", "title": "Title", "body": "Body", "labels": []}),
        )

    import backlog

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(backlog, "research_ownership_skip_reason", lambda labels: None)
    assert tick._fetch_issue_body("owner/repo#7") == "# Title\n\nBody"
    assert calls and calls[0][1]["timeout"] == 60
    monkeypatch.setattr(backlog, "research_ownership_skip_reason", lambda labels: "owned")
    assert tick._fetch_issue_body("owner/repo#7") is None
    assert tick._fetch_issue_body("broken") is None
    assert (
        tick._fetch_issue_body("owner/repo#7", issue_body_fn=lambda target: "injected")
        == "injected"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "CLOSED", "labels": []},
        {"state": "OPEN", "labels": "not-a-list"},
        {"state": "OPEN", "labels": [{"name": 3}]},
    ],
)
def test_tick_issue_body_rejects_unusable_github_answers(monkeypatch, payload):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    assert tick._fetch_issue_body("owner/repo#7") is None
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))
    )
    assert tick._fetch_issue_body("owner/repo#7") is None


def test_repo_knowledge_registry_helpers_keep_instance_content_and_validate_refs(tmp_path):
    seeded = [{"text": "Seed", "task_types": ["implement"]}]
    assert repo_knowledge._reseed_section(
        ["instance", {"text": "seed"}], seeded, section="gotchas"
    ) == [
        "instance",
        {"text": "Seed", "task_types": ["implement"]},
    ]
    registry = tmp_path / "registry.json"
    repo_knowledge.save(
        {"schema_version": 1, "repos": {"stranske/Trend_Model_Project": {"summary": ""}}}, registry
    )
    migrated = repo_knowledge.load(registry)
    assert migrated["schema_version"] == repo_knowledge.SEED_SCHEMA_VERSION
    assert migrated["repos"]["stranske/Trend_Model_Project"]["summary"]

    root = tmp_path / "repo"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("def useful_symbol(): pass\n")
    valid = repo_knowledge.validate_current_refs(
        root, [{"path": "docs/guide.md", "symbol": "useful_symbol"}]
    )
    assert valid["valid"] and valid["status"] == "current"
    stale = repo_knowledge.validate_current_refs(
        root, [{"path": "docs/guide.md", "symbol": "missing"}]
    )
    assert stale["status"] == "stale" and "stale current symbol" in stale["errors"][0]
    unsafe = repo_knowledge.validate_current_refs(root, [{"path": "../outside.md"}])
    assert unsafe["valid"] is False and "unsafe current path" in unsafe["errors"][0]


def test_repo_knowledge_scoping_export_and_memory_helpers(tmp_path):
    item = {"text": "Only opener", "task_types": ["implement"], "lanes": ["opener"]}
    assert repo_knowledge._applies(item, task_type="implement", lane="opener")
    assert not repo_knowledge._applies(item, task_type="research", lane="opener")
    assert repo_knowledge._matching_lines(
        [item, "always"], task_type="implement", lane="opener"
    ) == ["Only opener", "always"]
    assert repo_knowledge._limit_lines(["a", "b", "c", "d", "e"], 4) == [
        "a",
        "b",
        "- [truncated; see Orchestrator repo_knowledge.json]",
        "e",
    ]
    assert repo_knowledge._path_refs("read `docs/a.md` and `main`") == ["docs/a.md"]
    tokens = repo_knowledge._memory_query_tokens(
        "exact branch", task_type="implement", lane="opener"
    )
    assert "exact" in tokens and "implement" in tokens
    assert repo_knowledge._memory_item_visible(item, task_type=None, lane=None)
    assert not repo_knowledge._memory_item_visible(item, task_type="research", lane="opener")


def test_repo_knowledge_review_suggestions_and_memory_search_are_scoped(tmp_path):
    registry = tmp_path / "registry.json"
    repo_knowledge.save(
        {
            "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
            "repos": {
                "owner/repo": {
                    "summary": "Keep exact heads current.",
                    "definition_of_done": [{"text": "Run tests", "task_types": ["implement"]}],
                    "gotchas": [],
                    "validation": [],
                }
            },
        },
        registry,
    )
    payload = {
        "reviews": [
            {
                "body": "Must preserve exact head before merge.",
                "path": "src/a.py",
                "html_url": "https://example.test/c",
            }
        ]
    }
    suggestions = repo_knowledge.suggest_from_review_payload(
        payload, repo="owner/repo", path=registry
    )
    assert suggestions[0]["suggested_section"] == "gotchas"
    assert repo_knowledge._memory_match({"exact"}, "exact heads", base=0.1)["score"] > 1
    assert repo_knowledge._memory_match(set(), "text", base=0.2)["score"] == 0.2
    snapshot = {
        "runs": [
            {"run_id": "r1", "target": "owner/repo#1", "task_type": "implement", "agent": "codex"}
        ],
        "outcomes": [
            {"run_id": "r1", "notes": "Preserve exact head", "adjudicated_verdict": "PASS"}
        ],
    }
    results = repo_knowledge.search_repo_memory(
        "owner/repo#1", query="exact", task_type="implement", path=registry, snapshot=snapshot
    )
    assert {row["source"] for row in results} == {"approved-playbook", "feedback-outcome"}
