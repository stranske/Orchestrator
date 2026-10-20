"""Hermetic branch coverage for the guarded range-lane rollout planner."""

from __future__ import annotations

import json

import pytest

import capabilities
import feedback
import range_lane_rollout as rollout


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """A planner test must never discover or dispatch from the live runtime."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    # load() captured REG as a default at import time: pass the isolated path explicitly.
    original_load = capabilities.load
    original_lock = capabilities._locked

    def isolated_lock(path, **kwargs):
        assert path == registry, f"live capability path reached: {path}"
        return original_lock(path, **kwargs)

    monkeypatch.setattr(capabilities, "_locked", isolated_lock)
    registry = runtime / "capabilities.json"
    monkeypatch.setattr(capabilities, "REG", registry)
    monkeypatch.setattr(
        capabilities, "load", lambda path=registry, **kwargs: original_load(path, **kwargs)
    )
    monkeypatch.setattr(rollout.router, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(rollout.router, "DECISION_JSON", tmp_path / "handoff" / "decision.json")
    monkeypatch.setattr(rollout.router, "BACKLOG_JSON", tmp_path / "handoff" / "backlog.json")
    monkeypatch.setattr(rollout.backlog, "BACKLOG_JSON", tmp_path / "handoff" / "backlog.json")


def test_cached_backlog_distinguishes_valid_invalid_and_missing_payloads(tmp_path):
    cache = tmp_path / "backlog.json"
    cache.write_text(json.dumps({"items": [{"target": "o/r#1"}]}), encoding="utf-8")
    assert rollout._cached_backlog_payload(cache) == {
        "items": [{"target": "o/r#1"}],
        "source": "cached",
    }

    cache.write_text("[]", encoding="utf-8")
    assert rollout._cached_backlog_payload(cache) == {"source": "cached-invalid", "items": []}

    cache.unlink()
    assert rollout._cached_backlog_payload(cache) == {"source": "cached-missing", "items": []}


def test_filter_backlog_keeps_only_limited_eligible_opener_tasks():
    selected, skipped = rollout._filter_backlog(
        {
            "items": [
                {"target": "o/r#1", "task_type": "testgen", "lane": "opener", "labels": ["x"]},
                {"target": "o/r#2", "task_type": "testgen", "lane": "closer"},
                {"target": "o/r#3", "task_type": "implement", "lane": "opener"},
                {"task_type": "testgen", "lane": "opener"},
                {"target": "o/r#4", "task_type": "testgen", "lane": "opener"},
            ]
        },
        task_types={"testgen"},
        max_items=2,
    )
    assert [item["target"] for item in selected] == ["o/r#1", "o/r#4"]
    assert selected[0]["labels"] == ["x"]
    assert [item["reason"] for item in skipped] == [
        "not opener lane",
        "not a range-lane task type",
        "missing target",
    ]


def test_sanitize_decision_rejects_each_unsafe_assignment_kind():
    decision, rejected = rollout._sanitize_decision(
        {
            "assignments": [
                {"target": "o/r#1", "task_type": "testgen", "lane": "closer", "agent": "codex"},
                {"target": "o/r#2", "task_type": "implement", "lane": "opener", "agent": "codex"},
                {"target": "o/r#3", "task_type": "testgen", "lane": "opener", "agent": "aider"},
                {"target": "o/r#4", "task_type": "testgen", "lane": "opener", "agent": "codex"},
                {"target": "o/r#5", "task_type": "testgen", "lane": "opener", "agent": "codex"},
            ],
            "notes": ["router result"],
        },
        {"testgen"},
        {"o/r#4"},
    )
    assert [item["target"] for item in decision["assignments"]] == ["o/r#4"]
    assert len(rejected) == 4
    assert decision["notes"] == ["router result", "rejected 4 unsafe range-lane assignments"]


def test_build_rollout_reports_zero_cap_and_router_rejections_without_dispatch(monkeypatch):
    payload = {"items": [{"target": "o/r#1", "task_type": "testgen", "lane": "opener"}]}
    previewed = []
    monkeypatch.setattr(
        rollout, "_dispatch_preview", lambda decision: previewed.append(decision) or []
    )

    zero = rollout.build_rollout(
        backlog_payload=payload, capacity_payload={}, max_dispatches=0, dry_run=True
    )
    assert zero["eligible"] is False
    assert zero["blocked_details"] == [{"reason": "max_dispatch_cap_zero", "max_dispatches": 0}]
    assert not previewed

    monkeypatch.setattr(
        rollout.router,
        "plan",
        lambda *_args, **_kwargs: {
            "assignments": [],
            "rejections": [{"target": "o/r#1", "reason": "held", "claimed_by": "other"}],
        },
    )
    rejected = rollout.build_rollout(
        backlog_payload=payload, capacity_payload={}, max_dispatches=1, dry_run=True
    )
    assert rejected["eligible"] is False
    assert rejected["blocked_reasons"] == ["o/r#1: held by other"]
    assert rejected["blocked_details"] == [
        {"target": "o/r#1", "reason": "held", "claimed_by": "other"}
    ]


def test_build_rollout_reports_router_zero_assignment_and_formats_campaign(monkeypatch):
    monkeypatch.setattr(rollout.router, "plan", lambda *_args, **_kwargs: {"assignments": []})
    report = rollout.build_rollout(
        backlog_payload={"items": [{"target": "o/r#1", "task_type": "testgen", "lane": "opener"}]},
        capacity_payload={},
        max_dispatches=1,
    )
    assert report["blocked_details"] == [{"reason": "router_zero_assignments", "notes": []}]
    report["campaign"] = {"repos": {"o/r": {"merged": True}, "o/s": {"merged": False}}}
    assert "campaign: repos 2, dispatched 0, merged 1" in rollout.format_human(report)


def test_main_refuses_unguarded_active_request(capsys):
    assert rollout.main(["--apply"]) == 2
    assert "requires" in capsys.readouterr().out


def test_main_rejects_record_campaign_without_a_campaign_file():
    with pytest.raises(SystemExit) as exc:
        rollout.main(["--record-campaign"])
    assert exc.value.code == 2


def test_offline_selftest_exercises_guarded_planner_without_a_dispatch(monkeypatch, tmp_path):
    """The module selftest is useful coverage only when its preview cannot launch anything."""
    # HANDOFF unset → pop restore; exploration rate pre-set → assign restore (both CI gaps).
    monkeypatch.delenv("HANDOFF_DIR", raising=False)
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0.25")
    monkeypatch.setattr(
        rollout.dispatcher,
        "run",
        lambda decision, **kwargs: {
            "launched": [
                {
                    "target": item["target"],
                    "task_type": item["task_type"],
                    "lane": item["lane"],
                    "agent": item["agent"],
                    "mode": "remote",
                    "model": "stub",
                    "cwd": str(tmp_path),
                }
                for item in decision.get("assignments", [])
            ]
        },
    )
    rollout._selftest()
    assert "HANDOFF_DIR" not in __import__("os").environ
    assert __import__("os").environ.get("ORCH_EXPLORATION_RATE") == "0.25"


def test_live_loader_and_rejected_claim_release_are_isolated(monkeypatch):
    monkeypatch.setattr(rollout.backlog, "SUPPORTED_REPOS", ["o/r"])
    monkeypatch.setattr(rollout.backlog, "live_fetch_issues", object())
    monkeypatch.setattr(rollout.backlog, "live_fetch_prs", object())
    monkeypatch.setattr(rollout.backlog, "load_scoped_blockers", lambda: {"hold": True})
    monkeypatch.setattr(
        rollout.backlog,
        "build_backlog",
        lambda repos, issues, prs, **kwargs: [{"target": "o/r#1", "repos": repos, **kwargs}],
    )
    assert rollout._load_backlog_payload() == {
        "source": "live",
        "items": [{"target": "o/r#1", "repos": ["o/r"], "scoped": {"hold": True}}],
    }

    calls = []
    monkeypatch.setattr(
        rollout.claims, "release", lambda target, agent: calls.append((target, agent))
    )
    rollout._release_rejected_claims(
        [{"assignment": {"target": "o/r#1", "agent": "codex"}}, {"assignment": {}}]
    )
    assert calls == [("o/r#1", "codex")]


def test_main_apply_writes_only_the_stubbed_handoff_and_reports_dispatch(
    monkeypatch, tmp_path, capsys
):
    handoff = tmp_path / "handoff"
    decision_path = handoff / "decision.json"
    monkeypatch.setenv(rollout.ENV_FLAG, "1")
    monkeypatch.setattr(rollout.router, "HANDOFF", handoff)
    monkeypatch.setattr(rollout.router, "DECISION_JSON", decision_path)
    monkeypatch.setattr(
        rollout,
        "build_rollout",
        lambda **_kwargs: {
            "eligible": True,
            "decision": {"assignments": [{"target": "o/r#1", "capability_ids": ["prior"]}]},
            "counts": {"selected": 1, "assigned": 1, "dispatched": 0},
        },
    )
    beats = []
    monkeypatch.setattr(
        rollout.capabilities,
        "production_heartbeat",
        lambda *args, **kwargs: beats.append((args, kwargs)),
    )
    monkeypatch.setattr(
        rollout.dispatcher,
        "run",
        lambda decision, **kwargs: {
            "launched": [{"target": item["target"]} for item in decision["assignments"]]
        },
    )

    assert rollout.main(["--apply", "--confirm-rollout", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["dispatched"] == 1
    assert json.loads(decision_path.read_text())["assignments"][0]["capability_ids"] == [
        "prior",
        "range-lane-rollout",
    ]
    assert [call[0][1] for call in beats] == ["match", "invocation", "success"]


def test_main_returns_read_only_error_when_planning_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        rollout, "build_rollout", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert rollout.main(["--json"]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "nope", "read_only": True}


def test_release_rejected_claims_swallows_release_errors(monkeypatch):
    def boom(target, agent):  # noqa: ANN001
        raise RuntimeError(f"claim store down for {target}/{agent}")

    monkeypatch.setattr(rollout.claims, "release", boom)
    # Must not raise: a failed release must not abort the rollout sanitize path.
    rollout._release_rejected_claims(
        [{"assignment": {"target": "o/r#1", "agent": "codex"}}, {"assignment": {}}]
    )


def test_build_rollout_rejects_campaign_combined_with_backlog_or_non_codemod():
    campaign = {"campaign": {"id": "c1"}}
    with pytest.raises(ValueError, match="cannot be combined"):
        rollout.build_rollout(campaign=campaign, backlog_payload={"items": []})
    with pytest.raises(ValueError, match="cannot be combined"):
        rollout.build_rollout(campaign=campaign, cached_backlog=True)
    with pytest.raises(ValueError, match="supports only codemod"):
        rollout.build_rollout(campaign=campaign, task_types={"testgen"})


def test_build_rollout_releases_rejected_claims_when_not_dry_run(monkeypatch):
    released = []
    monkeypatch.setattr(
        rollout.claims, "release", lambda target, agent: released.append((target, agent))
    )
    monkeypatch.setattr(
        rollout.router,
        "plan",
        lambda *_a, **_k: {
            "assignments": [
                {
                    "target": "o/r#1",
                    "task_type": "testgen",
                    "lane": "opener",
                    "agent": "codex",
                },
                {
                    "target": "o/r#2",
                    "task_type": "testgen",
                    "lane": "closer",
                    "agent": "codex",
                },
            ],
            "notes": [],
        },
    )
    monkeypatch.setattr(rollout, "_dispatch_preview", lambda decision: [])
    report = rollout.build_rollout(
        backlog_payload={
            "items": [
                {"target": "o/r#1", "task_type": "testgen", "lane": "opener"},
                {"target": "o/r#2", "task_type": "testgen", "lane": "opener"},
            ]
        },
        capacity_payload={},
        max_dispatches=2,
        dry_run=False,
    )
    assert released == [("o/r#2", "codex")]
    assert any(d.get("reason") == "unsafe_assignment_rejected" for d in report["blocked_details"])
    assert [a["target"] for a in report["decision"]["assignments"]] == ["o/r#1"]


def test_main_selftest_skips_when_codex_profile_binary_absent(monkeypatch):
    monkeypatch.setattr(
        rollout.env_prereq,
        "selftest_skipped",
        lambda name, reason: True,
    )
    assert rollout.main(["--selftest"]) == 0


def test_main_selftest_runs_when_prereq_present(monkeypatch):
    ran = []
    monkeypatch.setattr(
        rollout.env_prereq,
        "selftest_skipped",
        lambda name, reason: False,
    )
    monkeypatch.setattr(rollout, "_selftest", lambda: ran.append("ok"))
    assert rollout.main(["--selftest"]) == 0
    assert ran == ["ok"]


def test_build_rollout_campaign_attaches_prompts_and_capability(monkeypatch):
    monkeypatch.setattr(
        rollout.codemod_lane,
        "campaign_backlog",
        lambda campaign: {
            "source": "campaign",
            "program": {"repos": {"o/r": {}}},
            "items": [
                {
                    "target": "o/r#1",
                    "task_type": "codemod",
                    "lane": "opener",
                    "prompt": "do the codemod",
                }
            ],
        },
    )
    monkeypatch.setattr(
        rollout.router,
        "plan",
        lambda *_a, **_k: {
            "assignments": [
                {
                    "target": "o/r#1",
                    "task_type": "codemod",
                    "lane": "opener",
                    "agent": "codex",
                    "capability_ids": ["prior"],
                }
            ],
            "notes": [],
        },
    )
    monkeypatch.setattr(rollout, "_dispatch_preview", lambda decision: [])
    report = rollout.build_rollout(campaign={"campaign": {"id": "c1"}}, max_dispatches=1)
    assignment = report["decision"]["assignments"][0]
    assert assignment["prompt"] == "do the codemod"
    assert assignment["capability_ids"] == ["prior", "codemod-campaign"]
    assert report["campaign"] == {"repos": {"o/r": {}}}


def test_main_apply_blocked_and_preview_human_paths(monkeypatch, capsys):
    monkeypatch.setenv(rollout.ENV_FLAG, "1")
    monkeypatch.setattr(
        rollout,
        "build_rollout",
        lambda **_kwargs: {
            "eligible": False,
            "decision": {"assignments": []},
            "counts": {"selected": 0, "assigned": 0, "dispatched": 0},
            "blocked_reasons": ["max dispatch cap is zero"],
        },
    )
    assert rollout.main(["--apply", "--confirm-rollout", "--json"]) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["dispatch_result"] == {"count": 0, "blocked": True}

    monkeypatch.setattr(
        rollout,
        "build_rollout",
        lambda **_kwargs: {
            "eligible": True,
            "read_only": True,
            "max_dispatches": 1,
            "task_types": ["testgen"],
            "decision": {"assignments": [{"target": "o/r#1"}]},
            "counts": {"selected": 1, "assigned": 1, "dispatched": 0},
            "blocked_reasons": [],
            "blocked_details": [],
            "guards": {},
        },
    )
    assert rollout.main([]) == 0
    assert "range_lane_rollout:" in capsys.readouterr().out


def test_main_apply_records_campaign_dispatches(monkeypatch, tmp_path, capsys):
    handoff = tmp_path / "handoff"
    decision_path = handoff / "decision.json"
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text('{"campaign": {"id": "c1"}}', encoding="utf-8")
    program_path = tmp_path / "program.json"
    written = []

    monkeypatch.setenv(rollout.ENV_FLAG, "1")
    monkeypatch.setattr(rollout.router, "HANDOFF", handoff)
    monkeypatch.setattr(rollout.router, "DECISION_JSON", decision_path)
    monkeypatch.setattr(
        rollout.codemod_lane,
        "parse_campaign_json",
        lambda _text: {"campaign": {"id": "c1", "repos": ["o/r"]}},
    )
    monkeypatch.setattr(
        rollout,
        "build_rollout",
        lambda **_kwargs: {
            "eligible": True,
            "decision": {
                "assignments": [
                    {
                        "target": "o/r#9",
                        "task_type": "codemod",
                        "lane": "opener",
                        "agent": "codex",
                        "capability_ids": [],
                    }
                ]
            },
            "counts": {"selected": 1, "assigned": 1, "dispatched": 0},
            "campaign": {"repos": {"o/r": {"target": "o/r#9"}}},
        },
    )
    monkeypatch.setattr(rollout.capabilities, "production_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        rollout.dispatcher,
        "run",
        lambda decision, **kwargs: {
            "launched": [{"target": "o/r#9", "agent": "codex"}],
        },
    )
    monkeypatch.setattr(rollout.codemod_lane, "campaign_program_path", lambda _c: program_path)
    monkeypatch.setattr(
        rollout.codemod_lane,
        "_write_program",
        lambda path, payload: written.append((path, payload)),
    )

    assert (
        rollout.main(
            [
                "--apply",
                "--confirm-rollout",
                "--record-campaign",
                "--campaign",
                str(campaign_path),
                "--json",
            ]
        )
        == 0
    )
    assert written and written[0][0] == program_path
    repos = written[0][1]["repos"]
    assert repos["o/r"]["dispatches"] == [{"target": "o/r#9", "agent": "codex"}]
    assert json.loads(capsys.readouterr().out)["counts"]["dispatched"] == 1
