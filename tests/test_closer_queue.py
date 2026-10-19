import json
import threading
import time

import pytest

import closer_queue


def _snapshot(
    revision, action="collect_evidence", *, eligible=True, reason=None, trigger=None, **extra
):
    return {
        "revision": revision,
        "action": action,
        "eligible": eligible,
        "reason": reason,
        "next_trigger": trigger,
        "evidence": {"revision": revision, **extra},
    }


def _run(path, targets, observe, execute, **kwargs):
    held = set()

    def claim(target, owner):
        if target in held:
            return False
        held.add(target)
        return True

    def release(target, owner):
        held.discard(target)
        return True

    kwargs.setdefault("claim_fn", claim)
    kwargs.setdefault("holder_fn", lambda target: {"agent": "test"} if target in held else None)
    kwargs.setdefault("release_fn", release)
    return closer_queue.run_round(
        path,
        targets,
        observe,
        execute,
        deadline=time.time() + 20,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        **kwargs,
    )


@pytest.mark.parametrize("pair", [("collect_evidence", "review"), ("repair", "merge")])
def test_independent_actions_execute_concurrently_and_account_for_all(tmp_path, pair):
    path = tmp_path / "queue.json"
    barrier = threading.Barrier(2)
    actions = dict(zip(("a/r#1", "b/r#2"), pair))
    calls = []

    def observe(target):
        action = actions[target]
        return _snapshot("r1", action, eligible=action != "complete")

    def execute(target, action, snapshot, operation):
        assert snapshot["revision"] == "r1"
        assert operation["timeout_seconds"] >= 0
        calls.append(target)
        barrier.wait(timeout=2)
        actions[target] = "complete"
        return {"status": "completed", "advanced": True}

    receipt = _run(path, list(actions), observe, execute, max_workers=2)
    assert sorted(calls) == sorted(actions)
    assert receipt["valid"] and receipt["stop_valid"]
    assert receipt["accounted_targets"] == list(actions)
    assert receipt["eligible_remaining"] == []


def test_waiting_target_does_not_block_another_target(tmp_path):
    path = tmp_path / "queue.json"
    actions = {"a/r#1": "wait", "b/r#2": "collect_evidence"}
    calls = []

    def observe(target):
        if target == "a/r#1":
            return _snapshot(
                "r1", "wait", eligible=False, reason="checks pending", trigger="check conclusion"
            )
        return _snapshot("r1", actions[target])

    def execute(target, action, snapshot, operation):
        calls.append(target)
        actions[target] = "complete"
        return {"status": "completed", "advanced": True}

    receipt = _run(path, list(actions), observe, execute, max_workers=2)
    assert calls == ["b/r#2"]
    assert receipt["eligible_remaining"] == []
    assert receipt["valid"]
    assert receipt["accounting"][0]["next_trigger"] == "check conclusion"


def test_repository_alias_claim_prevents_parallel_mutations(tmp_path):
    path = tmp_path / "queue.json"
    targets = ["org/repo#1", "org/repo#2"]
    calls = []
    active = 0
    max_active = 0
    guard = threading.Lock()
    actions = {target: "merge" for target in targets}
    held = set()

    def observe(target):
        return {**_snapshot("r1", actions[target]), "claim_targets": [target]}

    def claim(target, owner):
        if target in held:
            return False
        held.add(target)
        return True

    def holder(target):
        return {"agent": "external"} if target in held else None

    def release(target, owner):
        held.discard(target)
        return True

    def execute(target, action, snapshot, operation):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        calls.append(target)
        time.sleep(0.02)
        actions[target] = "complete"
        with guard:
            active -= 1
        return {"status": "completed", "advanced": True}

    receipt = _run(
        path,
        targets,
        observe,
        execute,
        max_workers=2,
        claim_fn=claim,
        holder_fn=holder,
        release_fn=release,
    )
    assert len(calls) == 2
    assert max_active == 1
    assert receipt["valid"]


def test_execute_exception_is_reconciled_not_retried(tmp_path):
    path = tmp_path / "queue.json"
    calls = []

    def observe(target):
        return _snapshot("same", "merge")

    def execute(*args):
        calls.append("execute")
        raise TimeoutError("outcome uncertain")

    first = _run(path, ["org/repo#1"], observe, execute, owner="round-1")
    assert not first["valid"]
    assert calls == ["execute"]

    def reconcile(target, operation):
        return {"status": "unknown", "next_trigger": "inspect remote outcome"}

    second = _run(path, ["org/repo#1"], observe, execute, reconcile=reconcile, owner="round-1")
    assert calls == ["execute"]
    assert second["accounting"][0]["reason"] == "operation_uncertain"
    state = json.loads(path.read_text())
    assert state["items"]["org/repo#1"]["operation"]["status"] == "uncertain"


def test_pre_action_remote_read_failure_is_confirmed_not_started(tmp_path):
    path = tmp_path / "queue.json"
    target = "org/repo#1"
    calls = []

    def observe(_target):
        return _snapshot("same", "merge")

    def execute(*_args):
        calls.append("pre_action_read")
        return {
            "status": "not_started",
            "reason": "remote freshness unavailable",
            "next_trigger": "targeted_remote_freshness_recheck",
        }

    first = _run(path, [target], observe, execute, owner="offline-round")
    state = json.loads(path.read_text())
    operation = state["items"][target]["operation"]
    assert calls == ["pre_action_read"]
    assert operation["status"] == "confirmed_not_started"
    assert operation["next_trigger"] == "targeted_remote_freshness_recheck"
    assert not first["stop_valid"]


def test_completed_result_at_same_revision_is_preserved_without_repeat(tmp_path):
    path = tmp_path / "queue.json"
    calls = []

    def observe(target):
        return _snapshot("stable", "review")

    def execute(*args):
        calls.append(1)
        return {"status": "completed", "report": {"finding": "evidence"}}

    first = _run(path, ["org/repo#1"], observe, execute, owner="first")
    assert calls == [1]
    assert first["accounting"][0]["reason"] == "no_progress"

    second = _run(path, ["org/repo#1"], observe, execute, owner="first")
    assert calls == [1]
    state = json.loads(path.read_text())
    item = state["items"]["org/repo#1"]
    assert item["saved_operation_result"] == {"report": {"finding": "evidence"}}
    assert item["deferred_next_trigger"] == "integrate_saved_operation_result"
    assert not second["valid"]


def test_changed_coordinator_binding_allows_new_same_revision_action(tmp_path):
    path = tmp_path / "queue.json"
    calls = []
    plan = {"digest": "old"}

    def observe(target):
        return {
            **_snapshot("stable", "review"),
            "operation_binding": {"pr_target": target, "disposition_digest": plan["digest"]},
        }

    def execute(*args):
        calls.append(plan["digest"])
        return {"status": "completed", "advanced": False, "finding": plan["digest"]}

    _run(path, ["org/repo#1"], observe, execute, owner="first")
    plan["digest"] = "new-approved-plan"
    _run(path, ["org/repo#1"], observe, execute)
    assert calls == ["old", "new-approved-plan"]


def test_coordinator_integration_is_required_unless_budget_is_exhausted(tmp_path):
    path = tmp_path / "queue.json"
    target = "org/repo#1"
    actions = {target: "review", "org/repo#2": "wait"}
    requires = {target: False}

    def observe(which):
        action = actions[which]
        return {
            **_snapshot(
                "r1",
                action,
                eligible=action != "wait",
                reason="senior integration required" if requires.get(which) else "checks pending",
                trigger="senior integration" if requires.get(which) else "check conclusion",
            ),
            "requires_coordinator": requires.get(which, False),
        }

    def execute(which, action, snapshot, operation):
        assert which == target
        actions[which] = "wait"
        requires[which] = True
        return {"status": "completed", "advanced": True, "advisory_only": True}

    first = _run(path, list(actions), observe, execute)
    assert first["accounted_targets"] == list(actions)
    assert first["integration_required"] == [target]
    assert not first["valid"]
    assert f"coordinator_integration_required:{target}" in first["reasons"]

    requires[target] = False  # A trusted coordinator acknowledged the saved result.
    integrated = _run(path, list(actions), observe, execute)
    assert integrated["integration_required"] == []
    assert integrated["accounted_targets"] == list(actions)
    assert integrated["valid"]

    expired_path = tmp_path / "expired.json"

    def flagged_observation(_):
        return {
            **_snapshot(
                "r1",
                "wait",
                eligible=False,
                reason="senior integration required",
                trigger="integrate",
            ),
            "requires_coordinator": True,
        }

    _run(expired_path, [target], flagged_observation, lambda *args: None)
    saved = json.loads(expired_path.read_text())
    saved["round"]["status"] = "running"
    saved["round"]["deadline"] = time.time() - 10
    expired_path.write_text(json.dumps(saved))
    expired = closer_queue.run_round(
        expired_path,
        [target],
        lambda *_: pytest.fail("expired budget must not observe"),
        lambda *args: pytest.fail("expired budget must not execute"),
        reconcile=lambda *args: pytest.fail("expired budget must not reconcile"),
        deadline=time.time() + 1000,
        receipt_reserve_seconds=0,
    )
    assert expired["integration_required"] == [target]
    assert expired["accounting"][0]["observation_freshness"] == "cached_budget_exhausted"
    assert expired["accounting"][0]["reason"] == "receipt_reserve"
    assert expired["valid"]


def test_definite_guard_outcome_is_not_uncertain_or_retried(tmp_path):
    path = tmp_path / "queue.json"
    calls = []

    def observe(target):
        return _snapshot("r1", "merge")

    def execute(*args):
        calls.append(1)
        return {
            "status": "blocked",
            "reason": "live guard denied",
            "next_trigger": "refresh evidence",
        }

    receipt = _run(path, ["org/repo#1"], observe, execute)
    state = json.loads(path.read_text())
    operation = state["items"]["org/repo#1"]["operation"]
    assert operation["status"] == "failed"
    assert operation["outcome"] == "blocked"
    assert receipt["accounting"][0]["next_trigger"] == "refresh evidence"
    assert calls == [1]


def test_operation_binding_is_durable_before_callback(tmp_path):
    path = tmp_path / "queue.json"
    target = "org/repo#7"
    expected = {
        "pr_target": target,
        "head": "a" * 40,
        "merge": "b" * 40,
        "source_targets": ["org/repo#2"],
        "review_revision": "review-9",
    }

    def observe(_target):
        snapshot = _snapshot(
            "revision-1",
            "merge",
            head=expected["head"],
            merge=expected["merge"],
            sources=[{"target": "org/repo#2"}],
            review_revision="review-9",
        )
        return {**snapshot, "operation_binding": expected}

    def execute(_target, _action, _snapshot, operation):
        saved = json.loads(path.read_text())["items"][target]["operation"]
        assert saved["status"] == "submitted"
        assert saved["binding"] == expected
        assert operation["binding"] == expected
        return {"status": "blocked", "next_trigger": "refresh evidence"}

    _run(path, [target], observe, execute)


def test_validate_stop_binds_assignment_and_source_revision(tmp_path):
    path = tmp_path / "queue.json"
    actions = {"org/repo#1": "complete"}

    def observe(target):
        return _snapshot("r1", "complete", eligible=False)

    _run(path, list(actions), observe, lambda *args: pytest.fail("must not execute"))
    assert closer_queue.validate_stop(path, ["org/repo#1"])["valid"]
    assert not closer_queue.validate_stop(path, ["org/repo#2"])["valid"]
    state = json.loads(path.read_text())
    state["round"]["source_revisions"]["org/repo#1"] = "tampered"
    path.write_text(json.dumps(state))
    assert "source_hash_mismatch" in closer_queue.validate_stop(path, ["org/repo#1"])["reasons"]


def test_claim_holder_callback_is_used_for_validation(tmp_path):
    path = tmp_path / "queue.json"
    target = "org/repo#1"
    blocker = {"agent": "other"}

    def observe(_target):
        return _snapshot("r1", "merge")

    receipt = _run(
        path,
        [target],
        observe,
        lambda *args: pytest.fail("claim conflict must prevent execution"),
        claim_fn=lambda *_: False,
        holder_fn=lambda _: blocker,
        release_fn=lambda *_: True,
    )
    assert receipt["accounting"][0]["reason"] == "claim_held"
    assert receipt["valid"]


def test_stale_revision_before_execute_rejects_action(tmp_path):
    path = tmp_path / "queue.json"
    observations = 0
    calls = []

    def observe(target):
        nonlocal observations
        observations += 1
        revision = "old" if observations == 1 else "new"
        return _snapshot(revision, "merge")

    receipt = _run(path, ["org/repo#1"], observe, lambda *args: calls.append(args))
    assert calls == []
    assert receipt["accounting"][0]["reason"] == "revision_changed"
    assert not receipt["valid"]
    state = json.loads(path.read_text())
    assert state["round"]["source_hash"] == closer_queue._digest({"org/repo#1": "new"})


def test_exhausted_budget_launches_nothing_and_accounts_explicitly(tmp_path):
    path = tmp_path / "queue.json"
    calls = []
    receipt = closer_queue.run_round(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "review"),
        lambda *args: calls.append(args),
        deadline=100,
        now=lambda: 100,
        receipt_reserve_seconds=10,
        min_action_seconds=1,
    )
    assert calls == []
    assert receipt["accounting"][0]["reason"] == "receipt_reserve"
    assert receipt["accounting"][0]["next_trigger"] == "retry_observation_next_round"
    assert receipt["valid"]


def test_expired_resume_uses_only_cached_observation_and_accounts_new_target(tmp_path):
    path = tmp_path / "queue.json"
    existing = "org/repo#1"
    added = "org/repo#2"
    evidence = {
        "body": "cached source text",
        "sources": [{"target": "org/repo#0", "body": "acceptance"}],
        "observed_at": 12345,
    }

    _run(
        path,
        [existing],
        lambda _: {
            **_snapshot("cached-sha", "complete", eligible=False),
            "evidence": evidence,
        },
        lambda *args: pytest.fail("completed observation must not execute"),
    )
    state = json.loads(path.read_text())
    old_deadline = time.time() - 10
    state["round"]["status"] = "running"
    state["round"]["deadline"] = old_deadline
    path.write_text(json.dumps(state))

    receipt = closer_queue.run_round(
        path,
        [existing, added],
        lambda *_: pytest.fail("expired replay must not observe"),
        lambda *args: pytest.fail("expired replay must not execute"),
        reconcile=lambda *args: pytest.fail("expired replay must not reconcile"),
        deadline=time.time() + 1000,
        receipt_reserve_seconds=0,
    )

    saved = json.loads(path.read_text())
    old_item = saved["items"][existing]
    new_item = saved["items"][added]
    assert old_item["revision"] == "cached-sha"
    assert old_item["evidence"] == evidence
    assert old_item["observation_freshness"] == "cached_budget_exhausted"
    assert old_item["deferred_reason"] == "receipt_reserve"
    assert new_item["revision"] is None and new_item["evidence"] == {}
    assert new_item["observation_freshness"] == "unobserved_budget_exhausted"
    assert new_item["deferred_reason"] == "receipt_reserve"
    assert receipt["accounted_targets"] == [existing, added]
    assert receipt["source_hash"] == saved["round"]["source_hash"]
    assert receipt["valid"]


def test_cutoff_reached_by_action_prevents_post_action_observe(tmp_path):
    clock = [10.0]
    observations = []
    calls = []

    def observe(target):
        observations.append(target)
        return _snapshot("r1", "review")

    def execute(*_args):
        calls.append("execute")
        clock[0] = 18.0
        return {"status": "completed", "advanced": True}

    receipt = closer_queue.run_round(
        tmp_path / "queue.json",
        ["org/repo#1"],
        observe,
        execute,
        deadline=20,
        now=lambda: clock[0],
        receipt_reserve_seconds=2,
        min_action_seconds=1,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda _: None,
    )
    assert calls == ["execute"]
    assert observations == ["org/repo#1", "org/repo#1"]  # Initial and pre-action reads only.
    assert receipt["accounting"][0]["reason"] == "receipt_reserve"
    assert receipt["valid"]


def test_running_operation_respects_worker_limit_but_other_assignment_is_accounted(tmp_path):
    path = tmp_path / "queue.json"
    targets = ["org/one#1", "org/two#2"]

    def observe(target):
        if target == targets[1]:
            return _snapshot("r1", "review")
        return _snapshot("r1", "merge")

    def execute(target, action, snapshot, operation):
        assert target == targets[0]
        return {"status": "running", "next_trigger": "poll operation"}

    receipt = _run(path, targets, observe, execute, max_workers=1)
    assert receipt["accounted_targets"] == targets
    assert receipt["accounting"][0]["reason"] == "operation_running"
    assert receipt["accounting"][1]["reason"] == "worker_capacity"
    assert receipt["valid"]


def test_running_round_restart_cannot_extend_original_deadline(tmp_path):
    path = tmp_path / "queue.json"
    target = "org/repo#1"
    receipt = _run(
        path, [target], lambda _: _snapshot("r1", "complete", eligible=False), lambda *args: None
    )
    state = json.loads(path.read_text())
    original_deadline = state["round"]["deadline"]
    state["round"]["status"] = "running"
    path.write_text(json.dumps(state))
    resumed = closer_queue.run_round(
        path,
        [target],
        lambda _: _snapshot("r1", "complete", eligible=False),
        lambda *args: pytest.fail("completed target must not execute"),
        deadline=original_deadline + 3600,
        receipt_reserve_seconds=0,
    )
    assert resumed["deadline"] == original_deadline
    assert resumed["owner"] == receipt["owner"]


def test_pass_limit_is_not_permission_to_stop_with_actionable_work(tmp_path):
    path = tmp_path / "queue.json"
    count = 0

    def observe(_):
        return _snapshot(f"r{count}", "review" if count < 2 else "complete", eligible=count < 2)

    def execute(*_):
        nonlocal count
        count += 1
        return {"status": "completed", "advanced": True}

    first = _run(path, ["org/repo#1"], observe, execute, max_passes=1)
    assert count == 1 and not first["stop_valid"]
    assert "pass_limit_requires_continuation:org/repo#1" in first["reasons"]
    second = _run(path, ["org/repo#1"], observe, execute, max_passes=1)
    assert count == 2 and second["stop_valid"]
    assert second["deadline"] == first["deadline"]


def test_assignment_refresh_of_unfinished_round_preserves_original_budget(tmp_path):
    path = tmp_path / "queue.json"
    first = _run(
        path,
        ["a/r#1"],
        lambda _: _snapshot("r1", "review"),
        lambda *_: {"status": "completed", "report": "requires integration"},
    )
    assert not first["stop_valid"]
    resumed = closer_queue.run_round(
        path,
        ["b/r#2"],
        lambda _: _snapshot("r1", "complete", eligible=False),
        lambda *_: pytest.fail("complete target"),
        deadline=first["deadline"] + 3600,
    )
    assert resumed["deadline"] == first["deadline"]
    assert resumed["owner"] == first["owner"]
    assert resumed["accounted_targets"] == ["b/r#2"]


def test_resumed_reconcile_receives_original_deadline_not_callers_extension(tmp_path):
    path = tmp_path / "queue.json"
    first = _run(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "merge"),
        lambda *_: {"status": "running", "next_trigger": "poll remote"},
    )
    state = json.loads(path.read_text())
    state["round"]["status"] = "running"
    path.write_text(json.dumps(state))
    seen = []

    def reconcile(_, operation):
        seen.append(operation["deadline"])
        return {"status": "running", "next_trigger": "poll remote"}

    closer_queue.run_round(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "merge"),
        lambda *_: pytest.fail("pending operation must not execute twice"),
        reconcile=reconcile,
        deadline=first["deadline"] + 3600,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda _: None,
    )
    assert seen == [first["deadline"]]


@pytest.mark.parametrize("fresh_action,fresh_eligible", [("wait", False), ("repair", True)])
def test_same_revision_status_transition_does_not_execute_stale_action(
    tmp_path, fresh_action, fresh_eligible
):
    observations = 0
    calls = []

    def observe(_):
        nonlocal observations
        observations += 1
        if observations == 1:
            return _snapshot("samehead", "merge")
        return _snapshot(
            "samehead",
            fresh_action,
            eligible=fresh_eligible,
            reason="human hold" if not fresh_eligible else None,
            trigger="hold released" if not fresh_eligible else None,
        )

    def execute(_, action, snapshot, operation):
        calls.append(action)
        return {"status": "blocked", "next_trigger": "fix prerequisite"}

    _run(tmp_path / "queue.json", ["org/repo#1"], observe, execute)
    assert "merge" not in calls
    assert calls == (["repair"] if fresh_eligible else [])


def test_stalled_batch_does_not_starve_untouched_targets(tmp_path):
    targets = ["a/r#1", "b/r#2", "c/r#3"]
    actions = {t: "review" for t in targets}
    calls = []

    def execute(target, *_):
        calls.append(target)
        if target != targets[0]:
            actions[target] = "complete"
        return {"status": "completed", "advanced": target != targets[0]}

    _run(
        tmp_path / "queue.json",
        targets,
        lambda t: _snapshot("r1", actions[t], eligible=actions[t] != "complete"),
        execute,
        max_workers=1,
    )
    assert sorted(calls) == sorted(targets)


@pytest.mark.parametrize("shared_source", [False, True])
def test_different_ledgers_cannot_overlap_repository_or_source_writers(tmp_path, shared_source):
    first_started, finish_first = threading.Event(), threading.Event()
    held = {}
    guard = threading.Lock()

    def claim(target, owner):
        with guard:
            if target in held:
                return False
            held[target] = owner
            return True

    def release(target, owner):
        with guard:
            assert held.pop(target) == owner
        return True

    def holder(target):
        with guard:
            return {"agent": held[target]} if target in held else None

    target1 = "org/a#1"
    target2 = "org/b#2" if shared_source else "org/a#2"
    actions = {target1: "repair", target2: "repair"}

    def observe(target):
        return _snapshot(
            "r1",
            actions[target],
            eligible=actions[target] != "complete",
            sources=[{"target": "org/source#9"}] if shared_source else [],
        )

    def execute(target, *_):
        assert target == target1
        first_started.set()
        assert finish_first.wait(timeout=3)
        actions[target] = "complete"
        return {"status": "completed", "advanced": True}

    errors = []

    def first_round():
        try:
            _run(
                tmp_path / "first.json",
                [target1],
                observe,
                execute,
                claim_fn=claim,
                release_fn=release,
                holder_fn=holder,
            )
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=first_round)
    thread.start()
    try:
        assert first_started.wait(timeout=3)
        second = _run(
            tmp_path / "second.json",
            [target2],
            observe,
            lambda *_: pytest.fail("overlapping writer launched"),
            claim_fn=claim,
            release_fn=release,
            holder_fn=holder,
        )
        assert second["accounting"][0]["reason"] == "claim_held"
        assert second["stop_valid"]
    finally:
        finish_first.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not errors and not held


def test_receipt_cannot_self_classify_unfinished_state_as_complete(tmp_path):
    path = tmp_path / "queue.json"
    receipt = _run(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "review"),
        lambda *_: {"status": "completed"},
    )
    assert not receipt["stop_valid"]
    forged = json.loads(json.dumps(receipt))
    forged["accounting"][0].update(status="complete", eligible=False)
    forged["continuation_required"] = False
    verdict = closer_queue.validate_stop(path, ["org/repo#1"], receipt=forged)
    assert not verdict["stop_valid"]
    assert "accounting_state_mismatch:org/repo#1" in verdict["reasons"]


def test_expired_recovery_records_truthful_resource_stop_without_callbacks(tmp_path):
    path = tmp_path / "queue.json"
    first = closer_queue.run_round(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "merge"),
        lambda *_: {"status": "uncertain", "next_trigger": "inspect existing operation"},
        deadline=20,
        now=lambda: 10,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda _: None,
    )
    assert not first["stop_valid"]
    operation_id = json.loads(path.read_text())["items"]["org/repo#1"]["operation"]["id"]
    expired = closer_queue.run_round(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "merge"),
        lambda *_: pytest.fail("expired recovery must not invoke execution"),
        reconcile=lambda *_: pytest.fail("expired recovery must not invoke reconciliation"),
        deadline=1000,
        now=lambda: 30,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        claim_fn=lambda *_: pytest.fail("expired recovery must not claim"),
        release_fn=lambda *_: True,
        holder_fn=lambda _: None,
    )
    assert expired["stop_valid"] and expired["continuation_required"]
    assert expired["deadline"] == 20
    assert expired["warnings"] == ["deadline_overrun"]
    assert expired["deadline_overrun_seconds"] == 10
    assert expired["accounting"][0]["reason"] == "receipt_reserve"
    saved = json.loads(path.read_text())["items"]["org/repo#1"]["operation"]
    assert saved["id"] == operation_id and saved["status"] == "uncertain"


@pytest.mark.parametrize("field", ["submitted_at", "invoked_at"])
def test_expired_resource_stop_cannot_admit_new_intent_or_callback_at_cutoff(tmp_path, field):
    path = tmp_path / "queue.json"
    closer_queue.run_round(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "review"),
        lambda *_: {"status": "uncertain"},
        deadline=20,
        now=lambda: 10,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda _: None,
    )
    state = json.loads(path.read_text())
    state["items"]["org/repo#1"]["operation"][field] = 20
    path.write_text(json.dumps(state))
    receipt = closer_queue.run_round(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("r1", "review"),
        lambda *_: pytest.fail("expired"),
        reconcile=lambda *_: pytest.fail("expired"),
        deadline=1000,
        now=lambda: 30,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        holder_fn=lambda _: None,
    )
    assert not receipt["stop_valid"]
    assert f"operation_after_cutoff:org/repo#1:{field}" in receipt["reasons"]


def test_exact_cutoff_with_zero_minimum_does_not_launch(tmp_path):
    receipt = closer_queue.run_round(
        tmp_path / "queue.json",
        ["org/repo#1"],
        lambda _: _snapshot("r1", "review"),
        lambda *_: pytest.fail("cutoff must be exclusive"),
        deadline=20,
        now=lambda: 20,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        claim_fn=lambda *_: pytest.fail("must not claim"),
        holder_fn=lambda _: None,
    )
    assert receipt["stop_valid"] and receipt["warnings"] == []


def test_cutoff_expiring_during_claim_preparation_releases_without_intent(tmp_path):
    clock = [1.0]
    held = set()

    def claim(target, _):
        held.add(target)
        clock[0] = 9.0
        return True

    def release(target, _):
        held.remove(target)
        return True

    receipt = closer_queue.run_round(
        tmp_path / "queue.json",
        ["org/repo#1"],
        lambda _: _snapshot("r1", "merge"),
        lambda *_: pytest.fail("late execution"),
        deadline=10,
        now=lambda: clock[0],
        receipt_reserve_seconds=1,
        min_action_seconds=0,
        claim_fn=claim,
        release_fn=release,
        holder_fn=lambda _: None,
    )
    assert not held and receipt["stop_valid"]
    assert receipt["accounting"][0]["operation_id"] is None


def test_queued_callback_rechecks_cutoff_before_entering_adapter():
    result = closer_queue._invoke_before_cutoff(
        lambda *_: pytest.fail("adapter cannot start after cutoff"),
        "org/repo#1",
        "merge",
        {},
        {"deadline": 20},
        lambda: 20,
    )
    assert result == (None, None)


def test_initial_observation_exhausting_budget_prevents_later_observation_callbacks(tmp_path):
    clock = [0.0]
    observations = []

    def observe(target):
        observations.append((target, clock[0]))
        clock[0] = 10.0
        return _snapshot("r1", "review")

    receipt = closer_queue.run_round(
        tmp_path / "queue.json",
        ["a/r#1", "b/r#2"],
        observe,
        lambda *_: pytest.fail("execution cannot start at cutoff"),
        deadline=10,
        now=lambda: clock[0],
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        claim_fn=lambda *_: pytest.fail("claims cannot start at cutoff"),
        holder_fn=lambda _: None,
    )
    assert observations == [("a/r#1", 0.0)]
    assert receipt["stop_valid"] and receipt["continuation_required"]
    row = receipt["accounting"][1]
    assert row["revision"] is None
    assert row["observation_freshness"] == "unobserved_budget_exhausted"
    assert row["reason"] == "receipt_reserve"
