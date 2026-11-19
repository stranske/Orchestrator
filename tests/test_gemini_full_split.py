"""The owner's live full-tier Gemini split (Argon vs Carbon), driven through plan_dispatch."""

from __future__ import annotations

import json

import pytest

import dispatcher
import gemini_full_split as split

ARMS = ["gemini-4-argon-high", "gemini-4-carbon-high"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    built = []
    monkeypatch.setattr(dispatcher.provision, "worktree_path", lambda *_: workspace)
    monkeypatch.setattr(dispatcher.repo_knowledge, "append_context", lambda prompt, *a, **k: prompt)
    monkeypatch.setattr(dispatcher.adapters, "workspace_path", lambda path: path)
    monkeypatch.setattr(dispatcher.adapters, "broad_workspace_reason", lambda *_: None)
    monkeypatch.setattr(dispatcher.adapters, "tier_ceiling", lambda agent: None)

    def build(agent, prompt, *a, **k):
        built.append(k.get("requested_model"))
        return [agent, prompt]

    monkeypatch.setattr(dispatcher.adapters, "build_command", build)
    import roles

    monkeypatch.setattr(roles, "activate_dispatch_roles", lambda a, prompt, **k: {"prompt": prompt})
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *_: "")
    monkeypatch.setattr(split, "assigned_targets", lambda conn=None: {})
    spec = tmp_path / "gemini-full-split.json"
    spec.write_text(
        json.dumps(
            {
                "policy": split.POLICY,
                "arms": ARMS,
                "start_ts": 0,
                "end_ts": 4e9,
                "target_prs": 30,
                "max_assignments": 60,
            }
        )
    )
    monkeypatch.setattr(split, "SPEC_PATH", spec)
    monkeypatch.setattr(split, "STATE_PATH", tmp_path / "state.json")
    return built


def _plan(**extra):
    assignment = {
        "agent": "gemini",
        "target": "stranske/Ready#41",
        "task_type": "implement",
        "lane": "opener",
        "prompt": "Do work",
        "caller_pinned": True,
        **extra,
    }
    return dispatcher.plan_dispatch(assignment, dry_run=True)


def test_an_opener_implement_run_gets_a_recorded_arm(world):
    plan = _plan()
    meta = plan["routing_metadata"]
    expected, seed = split.arm_for("stranske/Ready#41", ARMS)
    assert meta["profile_policy_version"] == split.POLICY
    assert meta["selected_profile_id"] == expected
    assert meta["profile_assignment_probability"] == 0.5
    assert meta["profile_rng_seed"] == seed
    assert world == [expected]  # the arm reaches agy as the requested model


def test_a_quota_capped_run_is_left_on_its_normal_path(world, monkeypatch):
    monkeypatch.setattr(dispatcher.adapters, "tier_ceiling", lambda agent: "mid")
    meta = _plan()["routing_metadata"]
    assert meta.get("profile_policy_version") != split.POLICY
    assert meta["split_skip_reason"] == "tier ceiling below full (quota cap)"


@pytest.mark.parametrize(
    "extra, reason",
    [
        ({"selected_profile_id": "gemini-4-argon-high"}, "model already chosen"),
        ({"lane": "closer"}, "not an opener start"),
        ({"mode": "mid"}, "mode mid is not full"),
        ({"task_type": "testgen"}, None),
    ],
)
def test_runs_outside_the_split_are_untouched(world, extra, reason):
    meta = _plan(**extra)["routing_metadata"] or {}
    assert meta.get("profile_policy_version") != split.POLICY
    if reason:
        assert meta.get("split_skip_reason") == reason


def test_the_split_stops_at_its_pr_target_but_a_retry_keeps_its_arm(world, monkeypatch):
    split.STATE_PATH.write_text(json.dumps({"prs": 30, "ts": 0}))
    meta = _plan()["routing_metadata"]
    assert meta["split_skip_reason"] == "split complete: 30/30 PRs"
    monkeypatch.setattr(split, "assigned_targets", lambda conn=None: {"stranske/Ready#41": ARMS[1]})
    assert _plan()["routing_metadata"]["profile_policy_version"] == split.POLICY


def test_the_backstop_stops_assignment_when_the_readout_never_runs(world, monkeypatch):
    monkeypatch.setattr(
        split, "assigned_targets", lambda conn=None: {f"o/r#{n}": ARMS[0] for n in range(60)}
    )
    assert (
        _plan()["routing_metadata"]["split_skip_reason"] == "assignment backstop reached: 60 issues"
    )


def test_an_unreadable_spec_fails_toward_normal_routing(world):
    split.SPEC_PATH.write_text("{not json")
    meta = _plan()["routing_metadata"]
    assert meta.get("profile_policy_version") != split.POLICY
    assert meta["split_skip_reason"].startswith("unreadable spec")


def _arm(prs, durable, cost=None):
    return {"prs": prs, "durable_share": durable / prs if prs else None, "cost_per_durable": cost}


def test_verdict_follows_the_preregistered_rule():
    assert "INCONCLUSIVE" in split._verdict({ARMS[0]: _arm(7, 5), ARMS[1]: _arm(15, 9)}, ARMS)
    assert "DOES NOT REPLACE" in split._verdict({ARMS[0]: _arm(15, 9), ARMS[1]: _arm(15, 6)}, ARMS)
    unmeasured = split._verdict({ARMS[0]: _arm(15, 9), ARMS[1]: _arm(15, 10)}, ARMS)
    assert "cost leg UNMEASURED" in unmeasured and "REPLACES" not in unmeasured
    assert "REPLACES" in split._verdict(
        {ARMS[0]: _arm(15, 9, cost=2.0), ARMS[1]: _arm(15, 10, cost=3.5)}, ARMS
    )
    assert "DOES NOT REPLACE" in split._verdict(
        {ARMS[0]: _arm(15, 9, cost=2.0), ARMS[1]: _arm(15, 10, cost=4.5)}, ARMS
    )


def test_a_quota_refused_run_is_recognised_from_its_own_log_block(tmp_path, monkeypatch):
    monkeypatch.setattr(split, "DISPATCH_LOG_DIR", tmp_path)
    (tmp_path / "o__r_7.gemini.log").write_text(
        "=== t1 dispatch gemini/full -> o/r#7 run_id=a ===\nerror: RESOURCE_EXHAUSTED (code 429)\n"
        "=== t2 dispatch gemini/full -> o/r#7 run_id=b ===\nworked\n"
    )
    assert split.quota_refused("a", "o/r#7") is True
    assert split.quota_refused("b", "o/r#7") is False
    assert split.quota_refused("c", "o/r#7") is False


def test_a_later_agents_pr_is_not_credited_to_the_split_arm(monkeypatch):
    rows = [
        {
            "number": 1,
            "headRefName": "orch/issue-7",
            "state": "OPEN",
            "createdAt": "2026-11-11T19:09:17Z",
            "mergedAt": None,
            "url": "u1",
        },
    ]
    monkeypatch.setattr(split, "_gh_json", lambda args: rows)
    started = split._iso_ts("2026-11-11T18:06:03Z")
    other_agent = split._iso_ts("2026-11-11T19:03:09Z")
    assert split.find_pr("o/r#7", started, other_agent) is None
    assert split.find_pr("o/r#7", started, None)["number"] == 1
