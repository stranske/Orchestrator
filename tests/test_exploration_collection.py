"""Hermetic coverage for autopilot round #1182 (exploration_collection, observability_dashboard, ccusage_reconcile)."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile

import advisor_hermetic
import pytest

import adapters
import claims
import exploration_collection as ec
import exploration_evidence_plan as eep
import feedback
import router


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_hermetic, "root", lambda: tmp_path)
    with advisor_hermetic.activated(monkeypatch):
        monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
        monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "feedback.db")
        # HANDOFF is captured outside ORCH_LOCAL_RUNTIME at import time.
        monkeypatch.setattr(adapters, "HANDOFF", tmp_path / "handoff")
        monkeypatch.setattr(adapters, "LEDGER", tmp_path / "handoff" / "ledger.ndjson")
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)

        attempted_external = []

        def guarded_subprocess_run(cmd, **kwargs):
            if (
                cmd
                and cmd[0] == "gh"
                and len(cmd) >= 3
                and cmd[1] == "api"
                and "langsmith_fleet_registry.json" in cmd[2]
            ):
                raw = json.dumps({"repos": []}).encode()
                body = json.dumps({"content": base64.b64encode(raw).decode("ascii")})
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd and cmd[0] == "agy" and "models" in cmd:
                # Offline catalog probe shape (tab-separated id + label).
                body = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd and cmd[0] == "cursor-agent" and "--list-models" in cmd:
                body = "composer-2.5 - Composer 2.5 (current)\n"
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd:
                joined = " ".join(str(c) for c in cmd)
                if len(cmd) >= 3 and cmd[0] == "codex" and cmd[1] == "login" and cmd[2] == "status":
                    return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
                if len(cmd) >= 3 and cmd[0] == "claude" and cmd[1] == "auth" and cmd[2] == "status":
                    return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
                if "ccusage" in joined and ("session" in joined or "blocks" in joined):
                    if "blocks" in joined:
                        body = json.dumps({"blocks": []})
                    else:
                        body = "{}"
                    return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            attempted_external.append((cmd, kwargs))
            pytest.fail("tests/test_coverage_round_1182.py attempted unstubbed external I/O")

        def forbid_external(*args, **kwargs):
            attempted_external.append((args, kwargs))
            pytest.fail("tests/test_coverage_round_1182.py attempted external I/O")

        monkeypatch.setattr(subprocess, "run", guarded_subprocess_run)
        monkeypatch.setattr(subprocess, "Popen", forbid_external)
        monkeypatch.setattr(socket, "socket", forbid_external)
        monkeypatch.setattr(socket, "create_connection", forbid_external)

        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp_under_tmp(suffix="", prefix=None, dir=None):
            root = tmp_path / "_tempfile_root"
            root.mkdir(parents=True, exist_ok=True)
            # Always parent beneath tmp_path so selftests never allocate outside the test sandbox.
            return real_mkdtemp(suffix=suffix, prefix=prefix or "tmp", dir=str(root))

        monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_under_tmp)

        yield

        assert attempted_external == []


def _capacity_payload() -> dict:
    return {
        "agents": {
            "claude": {"state": "ok"},
            "codex": {"state": "ok"},
            "cursor": {"state": "ok"},
        }
    }


def _eligible_acquisition(
    *, mode: str = "epsilon-greedy", eligible: bool = True, daily_cap: int = 4
) -> dict:
    return {
        "stage": "stage_2_supervised_collection",
        "next_action": "collect",
        "direct_mode_deficits": [
            {"mode": "epsilon-greedy", "remaining_outcome_runs": 5},
            {"mode": "thompson-hybrid", "remaining_outcome_runs": 2},
        ],
        "candidate_task_types": [
            {"task_type": "implement", "recommended": True},
            {"task_type": "testgen", "recommended": True},
        ],
        "supervised_collection": {
            "windows": [
                {
                    "mode": mode,
                    "eligible": eligible,
                    "max_exploratory_dispatches_per_day": daily_cap,
                }
            ]
        },
    }


def test_filter_backlog_low_risk_and_sanitize_paths(monkeypatch):
    plan = {
        "candidate_task_types": [
            {"task_type": "epic", "recommended": True},
            {"task_type": "implement", "recommended": True},
        ]
    }
    backlog = {"items": [{"target": "o/r#1", "task_type": "epic", "lane": "opener"}]}
    selected, skipped = ec._filter_backlog(plan, backlog, 5)
    assert not selected
    assert skipped[0]["reason"] == "task type is not low-risk for supervised collection"

    late_table = {
        "implement": {
            "agents": [
                {"agent": "codex", "mode": "full", "late": True},
                "not-a-dict",
            ]
        }
    }
    monkeypatch.setattr(router, "ROUTE_TABLE", late_table)
    assert ec._entry_is_late("implement", {"agent": "codex", "mode": "full"}) is True
    assert ec._entry_is_late("implement", {"agent": "claude", "mode": "full"}) is False

    decision = {
        "assignments": [
            {"lane": "closer", "task_type": "implement", "agent": "codex", "exploration": True},
            {"lane": "opener", "task_type": "epic", "agent": "codex", "exploration": True},
            {"lane": "opener", "task_type": "implement", "agent": "aider", "exploration": True},
            {
                "lane": "opener",
                "task_type": "implement",
                "agent": "codex",
                "mode": "full",
                "exploration": True,
            },
            {"lane": "opener", "task_type": "implement", "agent": "codex", "exploration": False},
        ]
    }
    monkeypatch.setattr(router, "BACKUP_AGENTS", {"aider"})
    clean, rejected = ec._sanitize_decision(decision, require_exploration=True)
    assert [row["reason"] for row in rejected] == [
        "not opener lane",
        "not a low-risk supervised-collection task type",
        "backup/paygo agent is not allowed in supervised exploration windows",
        "late/paygo route entry is not allowed in supervised exploration windows",
        "non-exploratory assignment does not collect direct exploration evidence",
    ]
    assert len(rejected) == 5
    assert "rejected 5" in " ".join(clean.get("notes") or [])


def test_claim_release_and_router_env_restore(monkeypatch):
    released = []

    def boom(target, agent):
        released.append((target, agent))
        if target == "bad":
            raise OSError("release failed")

    monkeypatch.setattr(claims, "release", boom)
    ec._release_rejected_claims(
        [
            {"assignment": {"target": "ok", "agent": "codex"}},
            {"assignment": {"target": "bad", "agent": "codex"}},
        ]
    )
    ec._release_assignments([{"target": "ok2", "agent": "claude"}])
    assert released == [("ok", "codex"), ("bad", "codex"), ("ok2", "claude")]

    monkeypatch.delenv("ORCH_EXPLORATION_MODE", raising=False)
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    with ec._temporary_router_env(mode="epsilon-greedy", rate=0.1):
        assert os.environ["ORCH_EXPLORATION_MODE"] == "epsilon-greedy"
    assert "ORCH_EXPLORATION_MODE" not in os.environ

    monkeypatch.setenv("ORCH_EXPLORATION_MODE", "keep-me")
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0.5")
    with ec._temporary_router_env(mode="thompson-hybrid", rate=0.2):
        pass
    assert os.environ["ORCH_EXPLORATION_MODE"] == "keep-me"
    assert os.environ["ORCH_EXPLORATION_RATE"] == "0.5"

    with ec._temporary_random_seed(None):
        pass


def test_seed_candidates_and_blocked_window_reasons(monkeypatch):
    assert ec._seed_candidates(42, 10) == [42]
    assert ec._seed_candidates(None, 0) == [None]

    monkeypatch.setattr(eep, "build_plan", lambda **kw: _eligible_acquisition())
    backlog = {"items": [{"target": "o/r#1", "task_type": "implement", "lane": "opener"}]}
    monkeypatch.setattr(
        eep,
        "build_plan",
        lambda **kw: _eligible_acquisition(daily_cap=0),
    )
    monkeypatch.setattr(eep, "SUPERVISED_COLLECTION_DAILY_CAP", 0)
    zero_cap = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        min_exploratory_dispatches=1,
        max_dispatches=1,
    )
    assert any("max dispatch cap is zero" in r for r in zero_cap["blocked_reasons"])
    monkeypatch.setattr(eep, "SUPERVISED_COLLECTION_DAILY_CAP", 4)
    monkeypatch.setattr(eep, "build_plan", lambda **kw: _eligible_acquisition())
    zero_min = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        min_exploratory_dispatches=0,
        max_dispatches=2,
    )
    assert any("min exploratory" in r for r in zero_min["blocked_reasons"])


def test_build_window_active_path_and_format_human(monkeypatch, tmp_path):
    acquisition = _eligible_acquisition()
    monkeypatch.setattr(eep, "build_plan", lambda **kw: acquisition)
    backlog = {
        "items": [
            {
                "target": "o/r#9",
                "task_type": "implement",
                "lane": "opener",
                "title": "Fix",
                "body": "body",
            }
        ]
    }

    def fake_plan(items, capacity_snapshot, **kwargs):
        exploratory = [
            {
                "target": "o/r#9",
                "task_type": "implement",
                "lane": "opener",
                "agent": "codex",
                "mode": "full",
                "exploration": kwargs.get("dry_run", True),
            }
        ]
        if kwargs.get("dry_run") is False:
            exploratory.append(
                {
                    "target": "o/r#9",
                    "task_type": "implement",
                    "lane": "opener",
                    "agent": "claude",
                    "mode": "full",
                    "exploration": False,
                }
            )
        return {"assignments": exploratory, "notes": []}

    monkeypatch.setattr(router, "plan", fake_plan)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    monkeypatch.setattr(ec, "_release_rejected_claims", lambda rows: None)
    monkeypatch.setattr(ec, "_release_assignments", lambda rows: None)
    handoff = tmp_path / "handoff"
    monkeypatch.setattr(router, "HANDOFF", handoff)
    monkeypatch.setattr(router, "DECISION_JSON", handoff / "decision.json")

    dry = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=True,
        rng_seed=0,
        min_exploratory_dispatches=1,
        seed_search_limit=3,
    )
    assert dry["seed_search"]["found"] is True
    text = ec.format_human({**dry, "blocked_reasons": ["cap blocked", "no seed"]})
    assert "blocked:" in text and "cap blocked" in text

    plan_for_seed = ec._plan_for_seed

    def never_enough(*args, **kwargs):
        return plan_for_seed(*args, **kwargs)

    monkeypatch.setattr(ec, "_plan_for_seed", never_enough)
    monkeypatch.setattr(ec, "_exploratory_count", lambda decision: 0)
    no_seed = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=True,
        min_exploratory_dispatches=99,
        seed_search_limit=2,
    )
    assert any("no router seed" in r for r in no_seed["blocked_reasons"])

    monkeypatch.setattr(ec, "_exploratory_count", lambda decision: 1)
    live = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=False,
        rng_seed=0,
        min_exploratory_dispatches=1,
        seed_search_limit=1,
    )
    assert live["active_dispatch"] is True

    monkeypatch.setattr(ec, "_exploratory_count", lambda decision: 0)
    race = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=False,
        rng_seed=0,
        min_exploratory_dispatches=1,
        seed_search_limit=1,
    )
    assert any("active claim race" in r for r in race["blocked_reasons"])


def test_exploration_collection_main_paths(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ec, "_selftest", lambda: None)
    assert ec.main(["--selftest"]) == 0

    monkeypatch.setenv("ORCH_EXPLORATION_EVIDENCE", "0")
    assert ec.main(["--apply"]) == 2
    cap = capsys.readouterr()
    err = cap.out + cap.err
    expected = "active dispatch requires --confirm-window and ORCH_EXPLORATION_EVIDENCE=1"
    assert err.strip() == expected

    monkeypatch.setattr(
        ec,
        "build_window",
        lambda **kw: {
            "eligible": True,
            "read_only": False,
            "decision": {"assignments": [{"target": "o/r#1"}]},
            "mode": "epsilon-greedy",
            "exploration_rate": 0.2,
            "direct_exploration_assignments": 1,
            "selected_backlog": [],
            "blocked_reasons": [],
            "seed_search": {},
            "acquisition_stage": "s",
            "acquisition_next_action": "n",
        },
    )
    monkeypatch.setattr(router, "HANDOFF", tmp_path / "h")
    monkeypatch.setattr(router, "DECISION_JSON", tmp_path / "h" / "decision.json")
    monkeypatch.setattr(ec.dispatcher, "run", lambda decision, dry_run=False: {"count": 1})
    monkeypatch.setenv("ORCH_EXPLORATION_EVIDENCE", "1")
    assert ec.main(["--apply", "--confirm-window", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dispatch_result"]["count"] == 1

    monkeypatch.setattr(
        ec,
        "build_window",
        lambda **kw: {
            "eligible": False,
            "read_only": True,
            "decision": {"assignments": []},
            "mode": "auto",
            "exploration_rate": 0.2,
            "direct_exploration_assignments": 0,
            "selected_backlog": [],
            "blocked_reasons": ["blocked"],
            "seed_search": {},
            "acquisition_stage": None,
            "acquisition_next_action": None,
        },
    )
    assert ec.main(["--apply", "--confirm-window"]) == 0

    monkeypatch.setattr(
        ec,
        "build_window",
        lambda **kw: {
            "eligible": True,
            "read_only": True,
            "decision": {"assignments": []},
            "mode": "auto",
            "exploration_rate": 0.2,
            "direct_exploration_assignments": 0,
            "selected_backlog": [],
            "blocked_reasons": [],
            "seed_search": {},
            "acquisition_stage": None,
            "acquisition_next_action": None,
        },
    )
    assert ec.main([]) == 0
    assert "exploration_collection:" in capsys.readouterr().out


def test_filter_backlog_skip_reasons():
    plan = {
        "candidate_task_types": [{"task_type": "implement", "recommended": True}],
    }
    items = [
        {"target": "o/r#1", "task_type": "implement", "lane": "opener"},
        {"target": "o/r#2", "task_type": "epic", "lane": "opener"},
        {"target": "o/r#3", "task_type": "implement", "lane": "closer"},
    ]
    selected, skipped = ec._filter_backlog(plan, {"items": items}, 5, exclude_targets={"o/r#1"})
    reasons = {row["target"]: row["reason"] for row in skipped}
    assert reasons["o/r#1"] == "target excluded from this supervised window"
    assert reasons["o/r#2"] == "task type not recommended for this collection window"
    assert reasons["o/r#3"] == "not opener lane"
    assert selected == []


def test_release_assignment_errors_are_swallowed(monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise OSError("nope")

    monkeypatch.setattr(claims, "release", boom)
    ec._release_assignments([{"target": "t", "agent": "codex"}])
    assert len(calls) == 1


def test_direct_evidence_prompts_and_attach():
    testgen = ec._direct_evidence_prompt(
        {"target": "o/r#12", "task_type": "testgen", "title": "T", "body": "B"}
    )
    assert testgen and "Closes #12" in testgen
    codemod = ec._direct_evidence_prompt(
        {"target": "o/r#13", "task_type": "codemod", "title": "C", "body": ""}
    )
    assert codemod and "no issue body" in codemod
    assert ec._direct_evidence_prompt({"task_type": "implement"}) is None
    decision = ec._attach_direct_evidence_prompts(
        {"assignments": [{"target": "o/r#12", "task_type": "testgen"}]},
        [{"target": "o/r#12", "task_type": "testgen", "title": "T", "body": "B"}],
    )
    assert decision["assignments"][0]["prompt"]


def test_build_window_acquisition_blocked(monkeypatch):
    monkeypatch.setattr(
        eep,
        "build_plan",
        lambda **kw: _eligible_acquisition(eligible=False),
    )
    blocked = ec.build_window(
        backlog_payload={"items": []},
        capacity_payload=_capacity_payload(),
    )
    assert any("does not currently mark" in r for r in blocked["blocked_reasons"])
    assert any("no eligible low-risk" in r for r in blocked["blocked_reasons"])


def test_exploration_collection_selftest_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "fb.db")
    ec._selftest()
