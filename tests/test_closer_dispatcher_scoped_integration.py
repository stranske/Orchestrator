"""Offline production-entry and cross-process reservation integration, never native execution."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import closer_dispatcher as d
import closer_resources as resources
import execution_profiles as profiles

WORKER = Path(__file__).with_name("closer_dispatcher_scoped_worker.py")
CLI = WORKER.parents[1] / "src" / "closer_dispatcher.py"


def start(tmp_path, name, **kw):
    spec = {
        "directory": str(tmp_path / name),
        "common": str(tmp_path / "common"),
        "state": str(tmp_path / "state"),
        "target": "o/r#1",
        **kw,
    }
    return subprocess.Popen(
        [sys.executable, str(WORKER), json.dumps(spec)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(proc):
    out, err = proc.communicate(timeout=25)
    assert out.strip(), err
    return json.loads(out)


def wait(path):
    limit = time.time() + 10
    while not path.exists():
        assert time.time() < limit, str(path)
        time.sleep(0.01)


def events(directory):
    path = directory / "events.jsonl"
    return (
        [json.loads(line)["event"] for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def report(profile_ids):
    selected = [profiles.get_profile(p) for p in profile_ids]
    return {
        "generated_at": time.time(),
        "agents": {p["agent"]: {"state": "ok"} for p in selected},
        "profiles": {p["profile_id"]: {"state": "ok"} for p in selected},
        "pools": {pool: {"state": "ok"} for p in selected for pool in p["capacity_pool_ids"]},
    }


def route(profile_id):
    p = profiles.get_profile(profile_id)
    return {"agent": p["agent"], "profile_id": profile_id, "pools": p["capacity_pool_ids"]}


def test_actual_cli_named_stop_and_no_legacy_fallback(tmp_path):
    receipt = finish(start(tmp_path, "a"))
    assert receipt["mode"] == "resource_scoped"
    assert receipt["round_id"] == receipt["action_c"]["round_id"]
    assert receipt["action_c"]["native_cleanup_performed"] is False
    assert receipt["validation"]["round_id"] == receipt["round_id"]
    for round_args, expected in [
        ([], "scoped_round_id_required"),
        (["--round-id", "a" * 32], "invalid_round_receipt"),
    ]:
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "validate-stop",
                "--state",
                str(tmp_path / "state"),
                "--shadow",
                *round_args,
            ],
            capture_output=True,
            text=True,
        )
        verdict = json.loads(result.stdout)
        assert result.returncode == 78 and not verdict["valid"]
        assert expected in verdict["reasons"][0]
    valid = subprocess.run(
        [sys.executable, str(CLI), *receipt["action_c"]["validate_stop_args"]],
        capture_output=True,
        text=True,
    )
    assert json.loads(valid.stdout)["round_id"] == receipt["round_id"]


@pytest.mark.parametrize(
    "first_mode,second_mode", [("legacy", "resource_scoped"), ("resource_scoped", "legacy")]
)
def test_cli_mixed_modes_gate_before_first_state_read(tmp_path, first_mode, second_mode):
    first = start(tmp_path, "a", mode=first_mode, hold="prepare")
    wait(tmp_path / "a" / "prepare.entered")
    second = finish(start(tmp_path, "b", mode=second_mode))
    assert second["targets"]["o/r#1"]["scoped_fence"]
    assert "prepare" not in events(tmp_path / "b")
    # A fully disjoint target prepares while the first target remains leased.
    other = finish(start(tmp_path, "c", target="other/repo#2", mode="resource_scoped"))
    assert "prepare" in events(tmp_path / "c")
    assert other["targets"]["other/repo#2"]["status"] == "completed"
    (tmp_path / "a" / "release").touch()
    assert finish(first)["targets"]["o/r#1"]["status"] == "completed"


def test_cli_cross_process_same_profile_pool_never_overlaps(tmp_path):
    common = tmp_path / "common"
    common.mkdir()
    (common / "capacity.json").write_text(json.dumps(report(["gemini-3.8-flash-low"])))
    first = start(tmp_path, "a", shadow=False, hold="offload")
    wait(tmp_path / "a" / "offload.entered")
    second = finish(start(tmp_path, "b", shadow=False, target="other/repo#2"))
    assert "prepare" in events(tmp_path / "b")
    assert "offload_enter" not in events(tmp_path / "b")
    assert second["targets"]["other/repo#2"]["status"] != "completed"
    (tmp_path / "a" / "release").touch()
    receipt = finish(first)
    assert receipt["targets"]["o/r#1"]["status"] == "uncertain"
    assert not receipt["validation"]["valid"]


@pytest.mark.parametrize("agent", d._EXCEPTION_ELIGIBLE_AGENTS)
def test_all_six_profiles_hold_exact_registry_pools_and_refresh(tmp_path, monkeypatch, agent):
    profile = profiles.profiles_for_agent(agent, transport="offload")[0]
    selected = route(profile["profile_id"])
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    reads = []
    monkeypatch.setattr(
        d, "_fresh_capacity_report", lambda: reads.append(1) or report([profile["profile_id"]])
    )

    def invoke(chosen):
        for pool in chosen["pools"]:
            import hashlib

            with pytest.raises(resources.ResourceBlocked):
                with resources.pool_slot(
                    tmp_path / "closer-profile-execution",
                    hashlib.sha256(pool.encode()).hexdigest(),
                    1,
                    deadline=time.time(),
                ):
                    pytest.fail("execution did not hold pool")
        return {"unknown_cost": None}

    result, actual = d._execute_profile_route(
        selected, invoke, lane="exception", load={}, deadline=time.time() + 3
    )
    assert reads and result["unknown_cost"] is None and actual == selected


def test_held_low_pool_reroutes_only_to_qualified_strong_pool(tmp_path, monkeypatch):
    import hashlib

    low = route("gemini-3.8-flash-low")
    strong_same_pool = route("gemini-3.8-flash-high")
    fallback = route("claude-sonnet-5-5-high")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(
        d,
        "_fresh_capacity_report",
        lambda: report([strong_same_pool["profile_id"], fallback["profile_id"]]),
    )

    def pick(**kw):
        assert strong_same_pool["profile_id"] in kw["unavailable_profiles"]
        assert low["profile_id"] in kw["unavailable_profiles"]
        return fallback, "available"

    monkeypatch.setattr(d, "_exception_route", pick)
    with resources.pool_slot(
        tmp_path / "closer-profile-execution",
        hashlib.sha256(low["pools"][0].encode()).hexdigest(),
        1,
        deadline=time.time() + 3,
    ):
        result, selected = d._execute_profile_route(
            strong_same_pool,
            lambda p: p["profile_id"],
            lane="exception",
            load={},
            deadline=time.time() + 3,
        )
    assert result == fallback["profile_id"] and selected == fallback


def test_prepared_issue_alias_keys_and_workspace_ignore_caller_keys(tmp_path):
    snap = {
        "target": "o/r#1",
        "pr_target": "o/r#7",
        "head": "a" * 40,
        "revision": "rev",
        "kind": "pr",
        "head_branch": "feature",
        "head_repository": "fork/r",
        "sources": [{"target": "o/r#1"}, {"target": "o/r#2"}],
    }
    keys = d._prepared_resource_keys(
        "o/r#1", {"revision": "rev", "resource_keys": []}, snap, {"o/r": str(tmp_path)}
    )
    assert set(keys) == {
        "pr:o/r#7",
        "issue:o/r#1",
        "issue:o/r#2",
        "branch:fork/r@feature",
        "worktree:" + str(tmp_path.resolve()),
    }
    with pytest.raises(resources.ResourceBlocked):
        d._prepared_resource_keys("o/r#1", {"revision": "stale"}, snap, {})


def test_provider_exception_after_execution_is_never_retried(tmp_path, monkeypatch):
    p = route("claude-sonnet-5-5-high")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(d, "_fresh_capacity_report", lambda: report([p["profile_id"]]))
    monkeypatch.setattr(
        d, "_exception_route", lambda **kw: pytest.fail("provider execution was retried")
    )

    def auth_failure(_route):
        raise resources.ResourceBlocked("auth outcome unknown")

    with pytest.raises(resources.ResourceBlocked, match="auth outcome"):
        d._execute_profile_route(
            p, auth_failure, lane="exception", load={}, deadline=time.time() + 3
        )


@pytest.mark.parametrize("collision", ["pr", "source", "branch"])
def test_actual_entry_alias_collisions_hold_mutation_but_allow_prepare(tmp_path, collision):
    aliases = {
        "pr": {"pr_target": "delivery/repo#7"},
        "source": {"source": "source/repo#88"},
        "branch": {"branch": "shared-feature"},
    }[collision]
    common = {"instrument_reservation": True, **aliases}
    first = start(tmp_path, "a", target="one/repo#1", hold="reservation", **common)
    wait(tmp_path / "a" / "reservation.entered")
    second = start(tmp_path, "b", target="two/repo#2", **common)
    wait(tmp_path / "b" / "reservation.attempted")
    assert "prepare" in events(tmp_path / "b")
    assert "reservation_enter" not in events(tmp_path / "b")
    (tmp_path / "a" / "release").touch()
    assert finish(first)["targets"]["one/repo#1"]["status"] == "completed"
    receipt = finish(second)
    if "reservation_enter" in events(tmp_path / "b"):
        a = [json.loads(x) for x in (tmp_path / "a" / "events.jsonl").read_text().splitlines()]
        b = [json.loads(x) for x in (tmp_path / "b" / "events.jsonl").read_text().splitlines()]
        assert next(x["at"] for x in a if x["event"] == "reservation_exit") <= next(
            x["at"] for x in b if x["event"] == "reservation_enter"
        )
    assert receipt["targets"]["two/repo#2"]["status"] in {"completed", "uncertain"}


@pytest.mark.parametrize("crash", [False, True])
def test_actual_entry_unknown_action_keeps_alias_intents_after_return_or_crash(tmp_path, crash):
    first = start(
        tmp_path,
        "a",
        instrument_reservation=True,
        crash_reservation=crash,
        uncertain_reservation=not crash,
        branch="shared-feature",
        target="one/repo#1",
    )
    if crash:
        out, _err = first.communicate(timeout=20)
        assert first.returncode == 9 and not out
    else:
        receipt = finish(first)
        assert receipt["targets"]["one/repo#1"]["status"] == "uncertain"
        assert not receipt["validation"]["valid"]
    root = resources.scoped_root(d._pipeline_state_path(tmp_path / "state", shadow=True))
    before = resources.unknown_reservations(root)
    assert before["branch:o/r@shared-feature"] == "one/repo#1"
    # Returned uncertainty and process death both retain the cross-target branch fence.
    other = finish(
        start(
            tmp_path, "b", instrument_reservation=True, target="two/repo#2", branch="shared-feature"
        )
    )
    assert "prepare" in events(tmp_path / "b")
    assert "reservation_enter" not in events(tmp_path / "b")
    assert other["targets"]["two/repo#2"]["status"] == "uncertain"
    assert resources.unknown_reservations(root)["branch:o/r@shared-feature"] == "one/repo#1"


@pytest.mark.parametrize("mode", ["legacy", "resource_scoped"])
def test_first_use_alias_fence_is_shared_with_legacy_before_scoped_root(tmp_path, mode):
    first = start(
        tmp_path,
        "a",
        target="one/repo#1",
        mode="legacy",
        instrument_reservation=True,
        hold="reservation",
        branch="shared-feature",
    )
    wait(tmp_path / "a" / "reservation.entered")
    root = resources.scoped_root(d._pipeline_state_path(tmp_path / "state", shadow=True))
    assert not root.exists()  # first-use legacy still reserves in the common coordination store
    second = start(
        tmp_path,
        "b",
        target="two/repo#2",
        mode=mode,
        instrument_reservation=True,
        branch="shared-feature",
    )
    if mode == "resource_scoped":
        wait(tmp_path / "b" / "reservation.attempted")
        assert "reservation_enter" not in events(tmp_path / "b")
    (tmp_path / "a" / "release").touch()
    assert finish(first)["targets"]["one/repo#1"]["status"] == "completed"
    other = finish(second)
    assert other["targets"]["two/repo#2"]["status"] in {"completed", "uncertain"}
    if mode == "legacy":
        doc = json.loads(d._pipeline_state_path(tmp_path / "state", shadow=True).read_text())
        # Completed runs are intentionally compacted by the existing scheduler;
        # the cache is shared JSON whose entries must survive the next writer.
        assert {row["target"] for row in doc["cache"].values()} >= {"one/repo#1", "two/repo#2"}


def test_post_reservation_capacity_change_prevents_execution(tmp_path, monkeypatch):
    selected = route("claude-sonnet-5-5-high")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    cap = report([selected["profile_id"]])
    cap["pools"][selected["pools"][0]]["state"] = "exhausted"
    monkeypatch.setattr(d, "_fresh_capacity_report", lambda: cap)
    monkeypatch.setattr(d, "_exception_route", lambda **kw: (None, "exhausted"))
    with pytest.raises(resources.ResourceBlocked):
        d._execute_profile_route(
            selected,
            lambda p: pytest.fail("stale capacity executed"),
            lane="exception",
            load={},
            deadline=time.time() + 3,
        )


def test_historical_unknown_intents_remain_fenced_after_store_move(tmp_path):
    import hashlib

    root = resources.open_root(tmp_path / "s", pipeline_schema="closer-pipeline/v1")
    key = "branch:o/r@old"
    directory = root / "reservations" / hashlib.sha256(key.encode()).hexdigest()[:24]
    directory.mkdir(parents=True)
    (directory / "intent.json").write_text(
        json.dumps({"key": key, "target": "o/r#1", "owner": "older", "state": "unknown"})
    )
    with pytest.raises(resources.ResourceBlocked):
        with resources.reserve(root, [key], target="o/r#2", owner="new", deadline=time.time() + 3):
            pytest.fail("historical unknown was ignored")
    assert resources.release_target(root, "o/r#1", owner="new") == []
    assert resources.unknown_reservations(root) == {key: "o/r#1"}


@pytest.mark.parametrize("failure,expected_calls", [("auth", 1), ("quota", 2)])
def test_actual_entry_auth_no_retry_and_quota_suppresses_every_pool_alias(
    tmp_path, failure, expected_calls
):
    common = tmp_path / "common"
    common.mkdir()
    (common / "capacity.json").write_text(
        json.dumps(report(["codex-6-luna-low", "gemini-3.8-flash-low", "gemini-4-argon-low"]))
    )
    receipt = finish(start(tmp_path, "a", shadow=False, failure=failure))
    rows = [json.loads(x) for x in (tmp_path / "a" / "events.jsonl").read_text().splitlines()]
    calls = [x["profile"] for x in rows if x["event"] == "offload_enter"]
    assert len(calls) == expected_calls
    pools = [profiles.get_profile(p)["capacity_pool_ids"][0] for p in calls]
    assert len(set(pools)) == len(pools)
    assert receipt["targets"]["o/r#1"]["status"] == "deferred"
    assert receipt["targets"]["o/r#1"]["deferred_reason"] == "provider_failure"
    # A measured provider stop is legitimate; it grants no target completion.
    assert receipt["validation"]["valid"] is True


def test_incompatible_contract_fences_legacy_even_for_disjoint_target(tmp_path):
    root = resources.open_root(tmp_path / "s", pipeline_schema="closer-pipeline/v1")
    path = root / "contract.json"
    path.write_text(json.dumps({"schema": "future", "layout": 2}))
    before = path.read_bytes()
    rows, runnable = resources.rollback_fence(tmp_path / "s", ["other/repo#2"])
    assert not runnable and rows["other/repo#2"]["scoped_fence"]
    assert path.read_bytes() == before


def test_canonical_duplicate_legacy_targets_fail_before_any_nested_lock(tmp_path):
    import closer_pipeline

    with pytest.raises(ValueError, match="canonically"):
        closer_pipeline.run_pipeline(tmp_path / "s", ["O/R#1", "o/r#1"], coordinator_mode="legacy")
    assert not (tmp_path / "s.coordination").exists()
