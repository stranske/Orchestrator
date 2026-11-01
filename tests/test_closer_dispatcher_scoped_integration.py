"""Offline production-entry and cross-process reservation integration, never native execution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_dispatcher_controlled_data_only import world as _controlled_world

import closer_dispatcher as d
import closer_resources as resources
import execution_profiles as profiles

controlled_world = _controlled_world

WORKER = Path(__file__).with_name("closer_dispatcher_scoped_worker.py")
ROOT = WORKER.parents[1]
MODULES = ROOT / "src" if (ROOT / "src").is_dir() else ROOT
CLI = MODULES / "closer_dispatcher.py"


def start(tmp_path, name, **kw):
    spec = {
        "directory": str(tmp_path / name),
        "common": str(tmp_path / "common"),
        "state": str(tmp_path / "state"),
        "target": "o/r#1",
        **kw,
    }
    child_path = ""
    if spec.get("postverify"):
        # Model assessment now authenticates its product worktree. Supply a real
        # isolated Git repository, exposing Git but no provider CLI in the child.
        directory = Path(spec["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        git = shutil.which("git")
        assert git is not None
        subprocess.run([git, "init", "--quiet", str(directory)], check=True)
        subprocess.run(
            [
                git,
                "-C",
                str(directory),
                "remote",
                "add",
                "origin",
                "https://github.com/" + spec["target"].split("#")[0] + ".git",
            ],
            check=True,
        )
        private_bin = directory / "private-bin"
        private_bin.mkdir()
        (private_bin / "git").symlink_to(git)
        ps = shutil.which("ps")
        assert ps is not None
        (private_bin / "ps").symlink_to(ps)
        child_path = str(private_bin)
    return subprocess.Popen(
        [sys.executable, str(WORKER), json.dumps(spec)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": str(tmp_path / "isolated-home"), "PATH": child_path},
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


def wait_for_model_gate(tmp_path, directory, target):
    """Wait for returned preparation and a real scheduled model gate, before ACT."""
    state = d._pipeline_state_path(tmp_path / "state", shadow=True)
    shard = resources.shard_dir(resources.scoped_root(state), target) / "state.json"
    limit = time.time() + 10
    while time.time() < limit:
        if "prepare" in events(directory) and shard.exists():
            doc = json.loads(shard.read_text())
            rows = [run["targets"].get(target, {}) for run in doc.get("runs", {}).values()]
            if any(row.get("stage") in {"interpret", "exception", "act"} for row in rows):
                return
        time.sleep(0.01)
    pytest.fail("returned preparation never reached the scoped model gate")


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
        if not round_args:
            assert verdict["required_round_id"] == receipt["round_id"]
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
    (common / "capacity.json").write_text(json.dumps(report(["claude-sonnet-5-5-low"])))
    first = start(tmp_path, "a", shadow=False, hold="offload", postverify=True)
    wait(tmp_path / "a" / "offload.entered")
    second = finish(start(tmp_path, "b", shadow=False, target="other/repo#2", postverify=True))
    assert "prepare" in events(tmp_path / "b")
    assert "offload_enter" not in events(tmp_path / "b")
    assert second["targets"]["other/repo#2"]["status"] != "completed"
    (tmp_path / "a" / "release").touch()
    receipt = finish(first)
    assert receipt["targets"]["o/r#1"]["status"] == "unknown"
    assert not receipt["validation"]["valid"]


def test_fake_gemini_offload_requires_explicit_simulated_tool_free_boundary(
    tmp_path, monkeypatch, controlled_world
):
    """Pure unsupported-route control after real ready evidence, never qualification."""
    from closer_inert_dispatcher_support import InertCaller
    from test_closer_dispatcher import snapshot

    import adapters
    import closer_evidence

    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "private-handoff"))
    caller = InertCaller(controlled_world, monkeypatch)
    current = snapshot(base="c" * 40)
    packet, ref, _proposal = caller.packet(current, tmp_path / "ready.evidence")
    ready = closer_evidence.action_evidence_readiness(
        current, packet, "collect_evidence", retained_dir=tmp_path / "ready.evidence"
    )
    assert ready["ready"] is True, ready
    assert adapters.data_only_problem("gemini", "assess") is not None
    result = d._controlled_offload(
        agent="gemini",
        prompt="Complete supplied offline request.",
        profile_id="gemini-3.8-flash-low",
        task_type="closer_interpret",
        operation_id="unsupported-gemini-offline-control",
        frontier=caller.frontier({current["target"]: current}),
        target=current["target"],
        basis={
            "target": current["target"],
            "revision": current["revision"],
            "head": current["head"],
            "base": current["base"],
            "packet_sha256": ref["sha256"],
        },
        deadline=time.time() + 10,
        offload=lambda *_a, **_k: pytest.fail("unqualified transport invoked"),
        mode="assess",
    )
    assert result["unsupported_assessment_tool_boundary"] is True
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert result["run_id"] is None
    assert not caller.results and controlled_world.events["auth"] == 0
    assert adapters.data_only_problem("gemini", "assess") is not None


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


@pytest.mark.parametrize("failure", ["capacity_false", "capacity_exception", "partial_pool"])
def test_finite_profile_prespawn_failure_resolves_held_pool_slots(tmp_path, monkeypatch, failure):
    import hashlib

    profile = {
        "agent": "fixture",
        "profile_id": "fixture-two-pool",
        "capacity_pool_ids": ["pool-a", "pool-b"],
    }
    selected = {
        "agent": "fixture",
        "profile_id": profile["profile_id"],
        "pools": list(profile["capacity_pool_ids"]),
    }
    target = "owner/repo#prespawn"
    frontier = {
        "frontier_id": "prespawn-frontier",
    }
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(profiles, "get_profile", lambda _profile_id: profile)
    monkeypatch.setattr(profiles, "profiles_for_agent", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(d, "_pipeline_low_route", lambda **_kwargs: (None, "exhausted"))
    monkeypatch.setattr(d, "_fresh_capacity_report", lambda: {"capacity": "fresh"})
    monkeypatch.setattr(
        d,
        "_profile_exception_qualified",
        lambda *_args, **_kwargs: failure != "capacity_false",
    )
    invoke_calls = []

    held = None
    if failure == "capacity_exception":
        monkeypatch.setattr(
            d,
            "_fresh_capacity_report",
            lambda: (_ for _ in ()).throw(RuntimeError("capacity read failed")),
        )
    elif failure == "partial_pool":
        second = hashlib.sha256(b"pool-b").hexdigest()
        held = resources.pool_slot(
            tmp_path / "closer-profile-execution",
            second,
            1,
            deadline=time.time() + 3,
        )
        held.__enter__()

    kwargs = {
        "lane": "low",
        "load": {},
        "deadline": time.time() + 3,
        "finite_frontier": frontier,
        "frontier_target": target,
    }
    try:
        expected = RuntimeError if failure == "capacity_exception" else resources.ResourceBlocked
        with pytest.raises(expected):
            d._execute_profile_route(
                selected,
                lambda *_: invoke_calls.append("invoke"),
                **kwargs,
            )
    finally:
        if held is not None:
            held.__exit__(None, None, None)

    assert invoke_calls == []
    first = hashlib.sha256(b"pool-a").hexdigest()
    record = json.loads(
        (tmp_path / "closer-profile-execution/pools" / first / "slot-0.json").read_text()
    )
    assert record["status"] == "resolved"
    assert record["resolution"] == "trusted_no_spawn"


@pytest.mark.parametrize("collision", ["pr", "source", "branch"])
def test_actual_entry_alias_collisions_hold_mutation_but_allow_prepare(tmp_path, collision):
    aliases = {
        "pr": {"pr_target": "delivery/repo#7"},
        "source": {"source": "source/repo#88"},
        "branch": {"branch": "shared-feature", "head_repository": "o/r"},
    }[collision]
    common = {"instrument_reservation": True, **aliases}
    first = start(tmp_path, "a", target="one/repo#1", hold="reservation", **common)
    wait(tmp_path / "a" / "reservation.entered")
    second = start(tmp_path, "b", target="two/repo#2", **common)
    try:
        wait_for_model_gate(tmp_path, tmp_path / "b", "two/repo#2")
        assert "reservation_enter" not in events(tmp_path / "b")
    finally:
        (tmp_path / "a" / "release").touch()
    assert finish(first)["targets"]["one/repo#1"]["status"] == "completed"
    receipt = finish(second)
    if "reservation_enter" in events(tmp_path / "b"):
        a = [json.loads(x) for x in (tmp_path / "a" / "events.jsonl").read_text().splitlines()]
        b = [json.loads(x) for x in (tmp_path / "b" / "events.jsonl").read_text().splitlines()]
        assert next(x["at"] for x in a if x["event"] == "reservation_exit") <= next(
            x["at"] for x in b if x["event"] == "reservation_enter"
        )
    assert receipt["targets"]["two/repo#2"]["status"] in {"completed", "deferred"}
    if receipt["targets"]["two/repo#2"]["status"] == "deferred":
        assert receipt["targets"]["two/repo#2"]["scope_proof"]


@pytest.mark.parametrize("crash", [False, True])
def test_actual_entry_unknown_action_keeps_alias_intents_after_return_or_crash(tmp_path, crash):
    first = start(
        tmp_path,
        "a",
        instrument_reservation=True,
        crash_reservation=crash,
        uncertain_reservation=not crash,
        branch="shared-feature",
        head_repository="o/r",
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
            tmp_path,
            "b",
            instrument_reservation=True,
            target="two/repo#2",
            branch="shared-feature",
            head_repository="o/r",
        )
    )
    assert "prepare" in events(tmp_path / "b")
    assert "reservation_enter" not in events(tmp_path / "b")
    assert other["targets"]["two/repo#2"]["status"] == "deferred"
    assert other["targets"]["two/repo#2"]["scope_proof"]
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
        head_repository="o/r",
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
        head_repository="o/r",
    )
    try:
        if mode == "resource_scoped":
            wait_for_model_gate(tmp_path, tmp_path / "b", "two/repo#2")
            assert "reservation_enter" not in events(tmp_path / "b")
    finally:
        (tmp_path / "a" / "release").touch()
    assert finish(first)["targets"]["one/repo#1"]["status"] == "completed"
    other = finish(second)
    assert other["targets"]["two/repo#2"]["status"] in {"completed", "deferred"}
    if other["targets"]["two/repo#2"]["status"] == "deferred":
        assert other["targets"]["two/repo#2"]["scope_proof"]
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


@pytest.mark.parametrize("failure", ["auth", "quota"])
def test_actual_entry_incomplete_native_auth_or_quota_signal_stays_unknown_without_retry(
    tmp_path, failure
):
    common = tmp_path / "common"
    common.mkdir()
    (common / "capacity.json").write_text(
        json.dumps(report(["claude-sonnet-5-5-low", "claude-sonnet-5-5-high-v2"]))
    )
    receipt = finish(start(tmp_path, "a", shadow=False, failure=failure, postverify=True))
    rows = [json.loads(x) for x in (tmp_path / "a" / "events.jsonl").read_text().splitlines()]
    calls = [x["profile"] for x in rows if x["event"] == "offload_enter"]
    assert len(calls) == 1
    assert all(x["data_only"] is True for x in rows if x["event"] == "offload_enter")
    # These stderr signals accompany an incomplete native result, not an
    # authenticated quota classification. UNKNOWN alone forbids retry/fallback;
    # the separate quota-capacity unit below exercises shared-pool suppression.
    row = receipt["targets"]["o/r#1"]
    assert row["status"] == "unknown"
    interpreted = row["results"]["interpret"]
    assert interpreted["classification"] == "provider_outcome_uncertain"
    actual = interpreted["provider_outcome"]["actual"]
    assert actual["raw_exit"] == 1 and actual["retry_safe"] is False
    completed = [x for x in rows if x["event"] == "offload_exit"]
    assert len(completed) == 1 and completed[0]["run_id"] == actual["run_id"]
    ref = completed[0]["provider_result_ref"]
    retained = Path(ref["path"])
    import hashlib

    assert retained.is_relative_to(tmp_path / "a" / "controlled" / "logs")
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == ref["sha256"]
    assert len(retained.read_bytes()) == ref["bytes"]
    native = json.loads(retained.read_bytes())["result"]
    assert native["process_started"] is True and native["outcome_uncertain"] is True
    assert native["process_observation"]["cleanup_terminal"] is True
    assert calls == ["claude-sonnet-5-5-low"]
    assert "claude-sonnet-5-5-high-v2" not in calls
    assert not receipt["validation"]["valid"]


@pytest.mark.parametrize("profile_id", ["claude-sonnet-5-5-low", "claude-sonnet-5-5-high-v2"])
def test_quota_capacity_snapshot_blocks_both_shared_pool_aliases(tmp_path, monkeypatch, profile_id):
    # Pure resource unit: a supplied current quota-capacity observation is
    # authoritative for this allocator test only, not native/provider proof or
    # production qualification. Hold all other capacity fields healthy.
    aliases = ["claude-sonnet-5-5-low", "claude-sonnet-5-5-high-v2"]
    selected = route(profile_id)
    lane = "low" if profile_id.endswith("-low") else "exception"
    pools = [profiles.get_profile(p)["capacity_pool_ids"] for p in aliases]
    assert pools[0] == pools[1] and len(pools[0]) == 1
    cap = report(aliases)
    cap["pools"][pools[0][0]].update(state="exhausted", reason="quota exhausted")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(d, "_fresh_capacity_report", lambda: cap)
    monkeypatch.setattr(d, "_exception_route", lambda **_kw: (None, "exhausted"))
    monkeypatch.setattr(d, "_pipeline_low_route", lambda **_kw: (None, "exhausted"))
    with pytest.raises(resources.ResourceBlocked):
        d._execute_profile_route(
            selected,
            lambda _p: pytest.fail("quota-exhausted shared pool executed"),
            lane=lane,
            load={},
            deadline=time.time() + 3,
        )
    # Same selected alias and all other inputs: fresh healthy pool evidence
    # removes this capacity refusal, proving the negative wasn't an unrelated
    # qualification/UNKNOWN fence.
    cap["pools"][pools[0][0]] = {"state": "ok"}
    value, actual = d._execute_profile_route(
        selected,
        lambda chosen: chosen["profile_id"],
        lane=lane,
        load={},
        deadline=time.time() + 3,
    )
    assert value == profile_id and actual == selected


def _write_scoped_receipt(root, *, round_id, run_id, finished_at, target="o/r#1"):
    resources.write_round_receipt(
        root,
        {
            "schema": "closer-pipeline-receipt/v1",
            "mode": "resource_scoped",
            "round_id": round_id,
            "run_ids": {target: run_id},
            "shards": {target: resources.shard_dir(root, target).name},
            "deadline": finished_at + 500.0,
            "elapsed_seconds": 1.0,
            "finished_at": finished_at,
            "assignment": [target],
            "carry_targets": [],
            "targets": {target: {"status": "completed"}},
            "scoped": {
                "contract": resources.CONTRACT_SCHEMA,
                "layout": resources.LAYOUT,
                "fenced_targets": [],
                "lease_wait_seconds": {},
                "max_lease_wait_seconds": 0.0,
                "completed": [target],
                "unknown_reservations": {},
                "unknown_reservations_drainable": 0,
            },
        },
    )


def test_validate_stop_without_round_id_uses_pipeline_validator_when_scoped_root_is_stale(
    tmp_path,
):
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    current_run = "current-pipeline-run"
    pipeline._save(
        pipeline_path,
        {
            "schema": pipeline.SCHEMA,
            "runs": {
                current_run: {
                    "created": 2000.0,
                    "deadline": 2500.0,
                    "assignment": ["o/r#1"],
                    "targets": {"o/r#1": {"status": "completed", "stage": "act"}},
                }
            },
            "carry": {},
        },
    )
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    stale_round = "b" * 32
    _write_scoped_receipt(
        root, round_id=stale_round, run_id="older-pipeline-run", finished_at=1000.0
    )
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert result.returncode == 0
    assert verdict.get("valid") is True
    assert verdict.get("stop_valid") is True
    assert verdict.get("run_id") == current_run
    assert "scoped_round_id_required" not in verdict.get("reasons", [])
    assert verdict.get("scoped_root_stale") is True
    assert verdict.get("newest_scoped_round_id") == stale_round
    assert verdict.get("validator") == "closer_pipeline.validate_stop"


def test_validate_stop_without_round_id_requires_id_when_scoped_round_is_newer_than_pipeline(
    tmp_path,
):
    """Legacy ledger run retained after a newer scoped round must not fall through."""
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    legacy_run = "legacy-pipeline-run"
    pipeline._save(
        pipeline_path,
        {
            "schema": pipeline.SCHEMA,
            "runs": {
                legacy_run: {
                    "created": 1000.0,
                    "deadline": 1500.0,
                    "assignment": ["o/r#1"],
                    "targets": {"o/r#1": {"status": "completed", "stage": "act"}},
                }
            },
            "carry": {},
        },
    )
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    current_scoped = "c" * 32
    _write_scoped_receipt(
        root,
        round_id=current_scoped,
        run_id="shard-run-not-in-ledger",
        finished_at=2000.0,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert result.returncode == 78
    assert verdict.get("valid") is False
    assert "scoped_round_id_required" in verdict.get("reasons", [])
    assert verdict.get("required_round_id") == current_scoped
    assert "scoped_root_stale" not in verdict


def test_validate_stop_freshness_uses_requested_assignment_not_unrelated_newer_run(
    tmp_path,
):
    """An unrelated newer pipeline run must not force stale fallback for this assignment."""
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    requested = "o/r#1"
    other = "other/repo#2"
    pipeline._save(
        pipeline_path,
        {
            "schema": pipeline.SCHEMA,
            "runs": {
                "requested-assignment-run": {
                    "created": 1000.0,
                    "deadline": 1500.0,
                    "assignment": [requested],
                    "targets": {requested: {"status": "completed", "stage": "act"}},
                },
                "unrelated-newer-run": {
                    "created": 3000.0,
                    "deadline": 3500.0,
                    "assignment": [other],
                    "targets": {other: {"status": "completed", "stage": "act"}},
                },
            },
            "carry": {},
        },
    )
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    current_scoped = "d" * 32
    _write_scoped_receipt(
        root,
        round_id=current_scoped,
        run_id="shard-run-for-requested",
        finished_at=2000.0,
        target=requested,
    )
    # Unrelated newer scoped receipt must not be selected as required_round_id either.
    _write_scoped_receipt(
        root,
        round_id="e" * 32,
        run_id="shard-run-for-other",
        finished_at=4000.0,
        target=other,
    )
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": requested}]}))
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
            "--assignment",
            str(assignment),
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert result.returncode == 78, result.stderr
    assert verdict.get("valid") is False
    assert "scoped_round_id_required" in verdict.get("reasons", [])
    assert verdict.get("required_round_id") == current_scoped
    assert "scoped_root_stale" not in verdict


def test_validate_stop_unrelated_only_ledger_requires_matching_scoped_round_id(tmp_path):
    """Ledger runs for other assignments must not force stale fallback when scoped work exists."""
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    requested = "o/r#1"
    other = "other/repo#2"
    pipeline._save(
        pipeline_path,
        {
            "schema": pipeline.SCHEMA,
            "runs": {
                "unrelated-only-run": {
                    "created": 3000.0,
                    "deadline": 3500.0,
                    "assignment": [other],
                    "targets": {other: {"status": "completed", "stage": "act"}},
                }
            },
            "carry": {},
        },
    )
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    current_scoped = "a" * 32
    _write_scoped_receipt(
        root,
        round_id=current_scoped,
        run_id="shard-run-for-requested",
        finished_at=2000.0,
        target=requested,
    )
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": requested}]}))
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
            "--assignment",
            str(assignment),
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert result.returncode == 78, result.stderr
    assert verdict.get("valid") is False
    assert "scoped_round_id_required" in verdict.get("reasons", [])
    assert verdict.get("required_round_id") == current_scoped
    assert "scoped_root_stale" not in verdict


def test_validate_stop_empty_ledger_with_assignment_still_marks_scoped_root_stale(tmp_path):
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    pipeline._save(pipeline_path, {"schema": pipeline.SCHEMA, "runs": {}, "carry": {}})
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    stale_round = "b" * 32
    _write_scoped_receipt(
        root, round_id=stale_round, run_id="older-pipeline-run", finished_at=1000.0
    )
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": "o/r#1"}]}))
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
            "--assignment",
            str(assignment),
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert "scoped_round_id_required" not in verdict.get("reasons", [])
    assert verdict.get("scoped_root_stale") is True
    assert verdict.get("newest_scoped_round_id") == stale_round
    assert verdict.get("validator") == "closer_pipeline.validate_stop"


def test_validate_stop_skips_ill_typed_receipt_assignment_without_crashing(tmp_path):
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    current_run = "current-pipeline-run"
    pipeline._save(
        pipeline_path,
        {
            "schema": pipeline.SCHEMA,
            "runs": {
                current_run: {
                    "created": 2000.0,
                    "deadline": 2500.0,
                    "assignment": ["o/r#1"],
                    "targets": {"o/r#1": {"status": "completed", "stage": "act"}},
                }
            },
            "carry": {},
        },
    )
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    bad_round = "c" * 32
    resources.write_round_receipt(
        root,
        {
            "schema": "closer-pipeline-receipt/v1",
            "mode": "resource_scoped",
            "round_id": bad_round,
            "run_ids": {"o/r#1": "shard-run"},
            "shards": {"o/r#1": resources.shard_dir(root, "o/r#1").name},
            "deadline": 2500.0,
            "elapsed_seconds": 1.0,
            "finished_at": 3000.0,
            "assignment": ["o/r#1", 2],
            "carry_targets": [],
            "targets": {"o/r#1": {"status": "completed"}},
            "scoped": {
                "contract": resources.CONTRACT_SCHEMA,
                "layout": resources.LAYOUT,
                "fenced_targets": [],
                "lease_wait_seconds": {},
                "max_lease_wait_seconds": 0.0,
                "completed": ["o/r#1"],
                "unknown_reservations": {},
                "unknown_reservations_drainable": 0,
            },
        },
    )
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": "o/r#1"}]}))
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
            "--assignment",
            str(assignment),
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert verdict.get("scoped_root_stale") is True
    assert "scoped_round_id_required" not in verdict.get("reasons", [])


def test_validate_stop_stale_fallback_pins_expected_run_when_sibling_created_is_null(
    tmp_path,
):
    import closer_pipeline as pipeline

    state = tmp_path / "state"
    pipeline_path = d._pipeline_state_path(state, shadow=True)
    good_run = "good-pipeline-run"
    pipeline._save(
        pipeline_path,
        {
            "schema": pipeline.SCHEMA,
            "runs": {
                good_run: {
                    "created": 2000.0,
                    "deadline": 2500.0,
                    "assignment": ["o/r#1"],
                    "targets": {"o/r#1": {"status": "completed", "stage": "act"}},
                },
                "null-created-run": {
                    "created": None,
                    "deadline": 2600.0,
                    "assignment": ["o/r#1"],
                    "targets": {"o/r#1": {"status": "completed", "stage": "act"}},
                },
            },
            "carry": {},
        },
    )
    root = resources.open_root(pipeline_path, pipeline_schema=pipeline.SCHEMA)
    stale_round = "d" * 32
    _write_scoped_receipt(
        root, round_id=stale_round, run_id="older-pipeline-run", finished_at=1000.0
    )
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate-stop",
            "--state",
            str(state),
            "--shadow",
        ],
        capture_output=True,
        text=True,
    )
    verdict = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert verdict.get("valid") is True
    assert verdict.get("run_id") == good_run
    assert verdict.get("scoped_root_stale") is True
    assert "scoped_round_id_required" not in verdict.get("reasons", [])


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


def test_legacy_runlock_covers_durable_boundary_and_owner_cleanup(tmp_path):
    first = start(tmp_path, "a", target="one/repo#1", mode="legacy", hold="release_boundary")
    second = None
    try:
        wait(tmp_path / "a" / "release_boundary.entered")
        second = start(tmp_path, "b", target="two/repo#2", mode="legacy", observe_runlock=True)
        marker = tmp_path / "b" / "runlock.contested"
        until = time.time() + 10
        while not marker.exists():
            assert second.poll() is None, finish(second)
            assert time.time() < until, "second legacy caller never contested the owned runlock"
            time.sleep(0.01)
        assert "prepare" not in events(tmp_path / "b")
    finally:
        (tmp_path / "a" / "release").touch()
    assert finish(first)["targets"]["one/repo#1"]["status"] == "completed"
    assert second is not None
    assert finish(second)["targets"]["two/repo#2"]["status"] == "completed"


@pytest.fixture(autouse=True)
def synthetic_role_admission(monkeypatch):
    # These tests measure process/pool boundaries with simulated providers.
    from closer_role_support import install_synthetic_role_verdict

    install_synthetic_role_verdict(monkeypatch)
