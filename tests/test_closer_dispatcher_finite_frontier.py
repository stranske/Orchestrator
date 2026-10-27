"""Offline CLI admission for the explicit immutable finite-frontier mode."""

import hashlib
import json
import subprocess
import sys
import time

import pytest
from closer_inert_dispatcher_support import InertCaller
from test_closer_reasoning_boundary import native_world as native_world
from test_dispatcher_controlled_data_only import world as _controlled_world

import adapters
import closer_dispatcher as dispatcher
import closer_pipeline as pipeline
import closer_resources as resources
import dispatcher as offload_dispatcher
from closer_frontier_policy import action_identity, make_frontier

controlled_world = _controlled_world
_REAL_DATA_ONLY_PROBLEM = adapters.data_only_problem


def install_inert_native_transport(w, world, monkeypatch):
    """Keep the public CLI/pool proof; replace only the old unproved emitter."""
    # native_world already owns the physical emitter; a second construction
    # would rewrite its executable after the original hash was admitted.
    caller = getattr(w, "inert_caller", None) or InertCaller(world, monkeypatch)
    monkeypatch.setattr(adapters, "data_only_problem", _REAL_DATA_ONLY_PROBLEM)

    def provider(agent, prompt, **kwargs):
        assert agent == "claude" and kwargs["data_only"] is True
        assert kwargs["max_network_retries"] == 0
        w.events["provider"] += 1
        w.events["prompts"].append(prompt)
        for pool in ("claude-subscription",):
            with pytest.raises(resources.ResourceBlocked):
                with resources.pool_slot(
                    w.root / "private-handoff" / "closer-profile-execution",
                    hashlib.sha256(pool.encode()).hexdigest(),
                    1,
                    deadline=time.time(),
                ):
                    pytest.fail("native reasoning did not hold the real shared pool")
        from test_closer_reasoning_boundary import declared_report_assessment

        response = declared_report_assessment(prompt, w.response, w.snapshot)
        return caller.response({"exit": 0, "output": json.dumps(response)}, agent, prompt, **kwargs)

    monkeypatch.setattr(offload_dispatcher, "offload", provider)
    return caller


def command(tmp_path, targets=("owner/repo#1",)):
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": t} for t in targets]}))
    selected = make_frontier(
        list(targets),
        owner="owner",
        frontier_id="selected",
        bindings={
            t: {"revision": "head-a", "action_id": action_identity(t, "head-a", "merge", {})}
            for t in targets
        },
    )
    pin = {
        "schema": resources.CONTRACT_SCHEMA,
        "layout": resources.LAYOUT,
        "pipeline_schema": pipeline.SCHEMA,
        "coordination_gates": 1,
    }
    args = [
        "run",
        "--assignment",
        str(assignment),
        "--state",
        str(tmp_path / "state.json"),
        "--pipeline",
        "--apply",
        "--workers",
        "--coordinator-mode",
        "resource_scoped",
        "--coordinator-contract-pin",
        json.dumps(pin),
        "--finite-frontier-json",
        json.dumps(selected),
        "--request-timeout-seconds",
        "25",
    ]
    return args, selected, assignment


def test_cli_passes_exact_frontier_without_aggregate_budget(tmp_path, monkeypatch):
    args, selected, _ = command(tmp_path)
    seen = {}

    def admitted(config, _parser, _rows, targets):
        seen.update(
            frontier=config.finite_frontier,
            budget=config.budget_seconds,
            timeout=config.request_timeout_seconds,
            targets=targets,
        )
        return 0

    monkeypatch.setattr(dispatcher, "_main_admitted", admitted)
    assert dispatcher.main(args) == 0
    assert seen == {
        "frontier": selected,
        "budget": None,
        "timeout": 25,
        "targets": selected["targets"],
    }


@pytest.mark.parametrize(
    "extra",
    [
        ["--budget-seconds", "600"],
        ["--request-timeout-seconds", "nan"],
        ["--request-timeout-seconds", "inf"],
        ["--request-timeout-seconds", "0"],
        ["--request-timeout-seconds", "121"],
    ],
)
def test_cli_rejects_budget_and_unbounded_request_before_admission(tmp_path, monkeypatch, extra):
    args, _, _ = command(tmp_path)
    monkeypatch.setattr(
        dispatcher, "_main_admitted", lambda *_: pytest.fail("invalid finite policy admitted")
    )
    with pytest.raises(SystemExit) as exc:
        dispatcher.main(args + extra)
    assert exc.value.code == 2


def test_cli_frontier_cannot_grow_after_descriptor_is_frozen(tmp_path, monkeypatch):
    args, _, assignment = command(tmp_path)
    assignment.write_text(
        json.dumps({"targets": [{"target": "owner/repo#1"}, {"target": "owner/repo#2"}]})
    )
    monkeypatch.setattr(
        dispatcher, "_main_admitted", lambda *_: pytest.fail("grown assignment admitted")
    )
    with pytest.raises(SystemExit) as exc:
        dispatcher.main(args)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "frontiers",
    [
        [],
        {"selected": None},
        {"selected": {"policy": {"schema": "wrong"}, "items": {}, "round": {}}},
    ],
)
def test_finite_integration_readback_rejects_corrupt_namespace(tmp_path, frontiers):
    state_path = tmp_path / "actions.json"
    state_path.write_text(
        json.dumps({"schema": "closer-queue/v1", "items": {}, "round": {}, "frontiers": frontiers})
    )
    selected = make_frontier(
        ["owner/repo#1"],
        owner="owner",
        frontier_id="selected",
        bindings={
            "owner/repo#1": {
                "revision": "head-a",
                "action_id": action_identity("owner/repo#1", "head-a", "merge", {}),
            }
        },
    )
    closer = dispatcher.Closer(None, state_path=state_path)
    closer.finite_frontier = selected
    with pytest.raises((RuntimeError, ValueError)):
        closer.integration_pending("owner/repo#1", {"revision": "head-a"})


@pytest.mark.parametrize(
    ("leaf", "message"),
    [
        (None, "target item is corrupt"),
        ({}, "target item is corrupt"),
        ({"operation": []}, "operation record is corrupt"),
        ({"operation": False}, "operation record is corrupt"),
        ({"operation": ""}, "operation record is corrupt"),
        ({"operation": 0}, "operation record is corrupt"),
        ({"operation": {}}, "operation record is corrupt"),
        ({"operation": {"status": "submitted"}}, "operation record is corrupt"),
    ],
)
def test_finite_integration_readback_rejects_present_malformed_target_leaf(tmp_path, leaf, message):
    target = "owner/repo#1"
    selected = make_frontier(
        [target],
        owner="owner",
        frontier_id="selected",
        bindings={
            target: {
                "revision": "head-a",
                "action_id": action_identity(target, "head-a", "merge", {}),
            }
        },
    )
    state_path = tmp_path / "actions.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "items": {},
                "round": {},
                "frontiers": {
                    "selected": {
                        "policy": selected,
                        "items": {target: leaf},
                        "round": {},
                    }
                },
            }
        )
    )
    closer = dispatcher.Closer(None, state_path=state_path)
    closer.finite_frontier = selected
    with pytest.raises(RuntimeError, match=message):
        closer.integration_pending(target, {"revision": "head-a"})


@pytest.mark.parametrize("leaf", [{"observation_freshness": "fresh"}, {"operation": None}])
def test_finite_integration_readback_accepts_absent_or_null_operation(tmp_path, leaf):
    target = "owner/repo#1"
    selected = make_frontier(
        [target],
        owner="owner",
        frontier_id="selected",
        bindings={
            target: {
                "revision": "head-a",
                "action_id": action_identity(target, "head-a", "merge", {}),
            }
        },
    )
    state_path = tmp_path / "actions.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "items": {},
                "round": {},
                "frontiers": {
                    "selected": {
                        "policy": selected,
                        "items": {target: leaf},
                        "round": {},
                    }
                },
            }
        )
    )
    closer = dispatcher.Closer(None, state_path=state_path)
    closer.finite_frontier = selected
    assert closer.integration_pending(target, {"revision": "head-a"}) is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda operation: operation.update(status="unknown"),
        lambda operation: operation.update(status="unrecognized"),
        lambda operation: operation.update(revision="head-b"),
        lambda operation: operation.update(binding={"head": "different"}),
        lambda operation: operation.update(finite_frontier={"frontier_id": "other"}),
    ],
)
def test_finite_integration_readback_rejects_operation_outside_frozen_action(tmp_path, mutation):
    target = "owner/repo#1"
    binding = {"head": "head-a"}
    selected = make_frontier(
        [target],
        owner="owner",
        frontier_id="selected",
        bindings={
            target: {
                "revision": "head-a",
                "action_id": action_identity(target, "head-a", "merge", binding),
            }
        },
    )
    operation = {
        "id": "op-1",
        "status": "submitted",
        "action": "merge",
        "revision": "head-a",
        "binding": binding,
        "finite_frontier": selected,
    }
    mutation(operation)
    state_path = tmp_path / "actions.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "items": {},
                "round": {},
                "frontiers": {
                    "selected": {
                        "policy": selected,
                        "items": {target: {"operation": operation}},
                        "round": {},
                    }
                },
            }
        )
    )
    closer = dispatcher.Closer(None, state_path=state_path)
    closer.finite_frontier = selected
    with pytest.raises(
        RuntimeError, match="operation (record is corrupt|binding differs from frontier)"
    ):
        closer.integration_pending(target, {"revision": "head-a"})


def test_bounded_isolation_copy_preserves_filters_and_returns_complete_tree(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("bounded payload")
    (source / "skip.pyc").write_bytes(b"ignored")
    monkeypatch.setattr(offload_dispatcher, "OFFLOAD_DIR", tmp_path / "offloads")
    isolated = offload_dispatcher._isolate_offload_cwd(source, timeout=5)
    assert (isolated / "keep.txt").read_text() == "bounded payload"
    assert not (isolated / "skip.pyc").exists()
    assert not list((tmp_path / "offloads").glob("*.partial"))


def test_finite_worktree_probe_has_a_local_timeout(monkeypatch, tmp_path):
    seen = {}

    def timeout(*_args, **kwargs):
        seen.update(kwargs)
        raise __import__("subprocess").TimeoutExpired("git", kwargs["timeout"])

    monkeypatch.setattr(offload_dispatcher.subprocess, "run", timeout)
    with pytest.raises(TimeoutError, match="owned request window expired"):
        offload_dispatcher._is_git_worktree(tmp_path, timeout=0.25)
    assert seen["timeout"] == 0.25


def test_bounded_run_reaps_child_when_start_publication_fails(tmp_path, monkeypatch):
    reservation = {
        "reservation_id": "publication-failure",
        "target": "owner/repo#owned",
        "frontier_id": "frontier-owned",
        "agent": "github",
        "profile_id": "github-api",
    }
    root = tmp_path / "pool-root"
    child_processes = []
    real_popen = dispatcher.subprocess.Popen

    def capture_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get("args")
        if command and command[0] == sys.executable:
            child_processes.append(child)
        return child

    def publication_failure(_observation):
        raise resources.ResourceBlocked("injected start publication failure")

    def successful_birth_probe(command, **kwargs):
        assert command[0] == "ps"
        return subprocess.CompletedProcess(command, 0, "Tue Oct 20 05:35:00 2026\n", "")

    monkeypatch.setattr(dispatcher.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(dispatcher.subprocess, "run", successful_birth_probe)
    monkeypatch.setattr(resources, "finite_pool_process_started", publication_failure)
    with resources.pool_slot(
        root,
        "github-api",
        1,
        deadline=time.time() + 3,
        finite_frontier=True,
        reservation=reservation,
    ):
        with pytest.raises(resources.ResourceBlocked, match="publication failure"):
            dispatcher.bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=3)
        assert child_processes and child_processes[0].poll() is not None
    record = json.loads((root / "pools/github-api/slot-0.json").read_text())
    assert record["status"] == "resolved"
    assert record["resolution"] == "process_group_absent"


def test_public_finite_reentry_reuses_packet_and_reaches_native_interpret(
    native_world, monkeypatch, capsys, controlled_world
):
    """Public CLI -> real held row -> public CAS re-entry -> same production callbacks."""
    import copy

    import adapters

    w = native_world
    caller = install_inert_native_transport(w, controlled_world, monkeypatch)
    target = w.snapshot["target"]
    monkeypatch.setattr(adapters, "data_only_problem", lambda *_: "fixture CLI absent")
    assert dispatcher.main(w.argv + ["--max-workers", "1"]) == 78
    capsys.readouterr()
    shard = resources.shard_dir(resources.scoped_root(w.ledger), target) / "state.json"
    original_state = json.loads(shard.read_text())
    original = original_state["frontiers"][w.frontier["frontier_id"]]["items"][target]
    assert original["status"] == "held" and original["stage"] == "interpret"
    assert original["results"]["interpret"]["classification"] == "assessment_transport_unavailable"
    prepared = copy.deepcopy(original["results"]["prepare"])
    packet = prepared["packet_ref"]
    from pathlib import Path

    packet_bytes = Path(packet["path"]).read_bytes()
    assert w.events["provider"] == w.events["effect"] == 0
    frontier_file = w.root / "frozen-frontier.json"
    frontier_file.write_text(json.dumps(w.frontier))
    monkeypatch.setattr(adapters, "data_only_problem", _REAL_DATA_ONLY_PROBLEM)
    retry_args = [
        "finite-reentry",
        "--state",
        str(shard),
        "--frontier",
        str(frontier_file),
        "--target",
        target,
        "--event",
        "qualified-installed-assessment-transport",
    ]
    assert dispatcher.main(retry_args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["stage"] == "interpret" and plan["kind"] == "qualified_assessment_transport"
    apply_args = retry_args + ["--apply", "--expected-plan-sha256", plan["plan_sha256"]]
    assert dispatcher.main(apply_args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    reopened = json.loads(shard.read_text())["frontiers"][w.frontier["frontier_id"]]["items"][
        target
    ]
    assert reopened["status"] == "held" and reopened["stage"] == "interpret"
    assert reopened["stage_status"] == "queued" and reopened["reentry_ready"]
    assert reopened["results"]["prepare"] == prepared
    assert reopened["reentry_history"][0]["previous"] == original
    assert dispatcher.main(w.argv + ["--max-workers", "1"]) == 78
    result = json.loads(capsys.readouterr().out)
    assert w.events["prepare"] == 1, result
    assert w.events["provider"] == len(caller.results) == 1, result
    assert caller.results[0]["exit"] == 0 and caller.results[0]["run_id"]
    assert w.events["effect"] == 0  # Existing historical advertisement remains authoritative.
    after = json.loads(shard.read_text())
    entry = after["frontiers"][w.frontier["frontier_id"]]
    assert entry["finite_frontier"] == w.frontier
    assert entry["items"][target]["results"]["prepare"] == prepared
    assert (
        entry["items"][target]["results"]["interpret"]["classification"]
        != "assessment_transport_unavailable"
    )
    assert Path(packet["path"]).read_bytes() == packet_bytes
    assert all(path.read_bytes() == raw for path, raw in w.originals.items())
    # A no-start re-entry cannot make a later unknown effect retryable or replay it.
    assert dispatcher.main(w.argv + ["--max-workers", "1"]) == 78
    capsys.readouterr()
    assert w.events["provider"] == 1 and w.events["effect"] == 0


def test_public_finite_max_workers_caps_actual_target_executor(native_world, monkeypatch, capsys):
    import concurrent.futures
    import copy

    import adapters

    w = native_world
    targets = (w.snapshot["target"], "owner/repo#13")
    args, _frontier, _assignment = command(w.root, targets)
    args[args.index("--state") + 1] = str(w.raw)

    def observed(_self, target):
        assert target in targets
        snapshot = copy.deepcopy(w.snapshot)
        snapshot["target"] = target
        snapshot["verifier"]["pr"] = int(target.split("#")[1])
        return snapshot

    monkeypatch.setattr(dispatcher.GitHub, "snapshot", observed)
    from closer_frontier_policy import make_frontier_from_observations

    observer = dispatcher.Closer(
        dispatcher.GitHub(deadline=time.time() + 25),
        state_path=w.raw,
        apply=True,
        workers=True,
    )
    frontier = make_frontier_from_observations(
        {target: observer.observe(target) for target in targets},
        owner=_frontier["owner"],
        frontier_id=_frontier["frontier_id"],
    )
    args[args.index("--finite-frontier-json") + 1] = json.dumps(frontier)
    monkeypatch.setattr(adapters, "data_only_problem", lambda *_: "fixture CLI absent")
    actual = concurrent.futures.ThreadPoolExecutor
    observed_ceilings = []

    class RecordedExecutor(actual):
        def __init__(self, *a, **kw):
            observed_ceilings.append(kw.get("max_workers", a[0] if a else None))
            super().__init__(*a, **kw)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", RecordedExecutor)
    assert dispatcher.main(args + ["--max-workers", "1"]) == 78
    result = json.loads(capsys.readouterr().out)
    assert observed_ceilings == [1], result
    assert set(result["targets"]) == set(targets)
    assert all(
        row["results"]["interpret"]["classification"] == "assessment_transport_unavailable"
        for row in result["targets"].values()
    )
    assert w.events["provider"] == w.events["effect"] == 0
    assert all(path.read_bytes() == raw for path, raw in w.originals.items())


def test_public_finite_reentry_refuses_unknown_effect_and_never_replays(
    native_world, monkeypatch, capsys, controlled_world
):
    import closer_queue

    w = native_world
    caller = install_inert_native_transport(w, controlled_world, monkeypatch)
    target = w.snapshot["target"]
    # This self-owned test advertisement is the only alias overlap. Preserve all
    # actual historical ledger bytes; removing its fixture copy allows the normal
    # guarded ACT callback to reach the simulated external queue transport.
    w.ad.unlink()
    calls = []

    def uncertain_queue(*_a, **_kw):
        calls.append("possibly-started-effect")
        raise TimeoutError("synthetic external effect result was lost")

    monkeypatch.setattr(closer_queue, "run_round", uncertain_queue)
    assert dispatcher.main(w.argv + ["--max-workers", "1"]) == 78
    capsys.readouterr()
    assert calls == ["possibly-started-effect"]
    shard = resources.shard_dir(resources.scoped_root(w.ledger), target) / "state.json"
    row = json.loads(shard.read_text())["frontiers"][w.frontier["frontier_id"]]["items"][target]
    assert row["status"] == "unknown" and row["stage"] == "act"
    frozen = w.root / "unknown-effect-frontier.json"
    frozen.write_text(json.dumps(w.frontier))
    assert (
        dispatcher.main(
            [
                "finite-reentry",
                "--state",
                str(shard),
                "--frontier",
                str(frozen),
                "--target",
                target,
                "--event",
                "operator-wants-retry",
            ]
        )
        == 78
    )
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "held"
    assert dispatcher.main(w.argv + ["--max-workers", "1"]) == 78
    capsys.readouterr()
    assert calls == ["possibly-started-effect"]
    assert w.events["provider"] == len(caller.results) == 1
    assert all(path.read_bytes() == raw for path, raw in w.originals.items() if path != w.ad)
