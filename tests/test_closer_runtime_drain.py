"""Durable effect readback over real Core and an offline Contents/PR boundary."""

from __future__ import annotations

import copy
import time

import pytest
import test_closer_runtime_binding as support

import closer_campaign as cc
import closer_runtime_binding as rb


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    return support.bundle.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    return support.policy.__wrapped__(tmp_path, monkeypatch)


class NativeContents(support.Contents):
    def __init__(self):
        super().__init__()
        self.pr = None
        self.reads = 0

    def __call__(self, method, path, body):
        if "/pulls/" in path:
            assert method == "GET" and body is None
            self.reads += 1
            if self.pr is None:
                raise OSError("offline")
            return copy.deepcopy(self.pr)
        return super().__call__(method, path, body)


def snapshot():
    return {
        "pr_target": "org/repo#7",
        "head": "a" * 40,
        "base": "b" * 40,
        "merge": "c" * 40,
        "review_revision": "review-1",
        "revision": "revision-1",
        "criteria": {"source": "original source acceptance"},
        "labels": [],
    }


def operation(s, action="post_merge_verify", owner="original"):
    return {
        "id": "queue-operation-1",
        "owner": owner,
        "action": action,
        "revision": s["revision"],
        "deadline": time.time() + 60,
        "binding": {
            "pr_target": s["pr_target"],
            "head": s["head"],
            "merge": s["merge"],
            "review_revision": s["review_revision"],
            "source_targets": [],
        },
    }


def observed(s, *, labels=True):
    return {
        "head": {"sha": s["head"]},
        "merged": True,
        "merge_commit_sha": s["merge"],
        "labels": [{"name": "verify:compare"}] if labels else [],
    }


def record(binding, s, action="verify_request"):
    adapter = binding.adapters["org/repo"]
    spec = cc.ActionSpec("org/repo", 7, action, s["head"])
    return adapter, adapter.store.read(adapter.target_fence_key(spec))


def test_exact_request_readback_drains_and_is_idempotent(bundle, policy):
    transport, s = NativeContents(), snapshot()
    transport.pr = observed(s)
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)
    calls = []

    def effect():
        calls.append("label")
        return {"status": "completed"}

    first = binding.execute(s["pr_target"], "post_merge_verify", s, operation(s), effect)
    assert first["status"] == "completed" and first["core_reconciled"]
    _, rec = record(binding, s)
    assert rec["status"] == "succeeded" and rec["action"] == "verify_request"
    assert rec["mutation_intent"]["native_binding"]["criteria"] == s["criteria"]
    restarted = rb.RuntimeBinding(bundle, policy, transport=transport)
    again = restarted.execute(
        s["pr_target"], "post_merge_verify", s, operation(s, owner="next"), effect
    )
    assert again["status"] == "completed" and again["already_completed"] and calls == ["label"]


def test_unknown_request_cannot_drain_from_green_topology_or_different_merge(bundle, policy):
    transport, s = NativeContents(), snapshot()
    transport.pr = observed(s, labels=False)
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)
    result = binding.execute(
        s["pr_target"], "post_merge_verify", s, operation(s), lambda: {"status": "completed"}
    )
    assert result["status"] == "unknown"
    adapter, rec = record(binding, s)
    assert rec["status"] == "submitted"
    with pytest.raises(bundle.runtime.PolicyDenied, match="no reconciliation producer"):
        bundle.runtime.collect_reconciliation_evidence(bundle.runtime.spec_from_record(rec), {})
    transport.pr = {**observed(s), "merge_commit_sha": "d" * 40}
    assert binding.reconcile(s["pr_target"], operation(s))["status"] == "unknown"
    assert adapter.store.read(rec["target_fence_key"])["status"] == "submitted"


def test_restart_uses_original_spec_and_preserves_unknown_and_denial(bundle, policy):
    transport, s = NativeContents(), snapshot()
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)
    assert (
        binding.execute(
            s["pr_target"], "post_merge_verify", s, operation(s), lambda: {"status": "completed"}
        )["status"]
        == "unknown"
    )
    transport.pr = observed(s)
    # Original acknowledged owner may reconcile immediately. A successor with a live
    # lease remains fenced until Core permits read-only recovery; no owner is evicted.
    restarted = rb.RuntimeBinding(bundle, policy, transport=transport)
    assert restarted.reconcile(s["pr_target"], operation(s, owner="next"))["status"] == "unknown"
    altered = copy.deepcopy(s)
    altered["criteria"] = {"source": "new criteria"}
    assert (
        restarted.execute(
            s["pr_target"],
            "post_merge_verify",
            altered,
            operation(altered),
            lambda: pytest.fail("retried"),
        )["status"]
        == "unknown"
    )
    assert restarted.reconcile(s["pr_target"], operation(s))["status"] == "completed"
    _, rec = record(restarted, s)
    assert rec["criteria_fingerprint"] == cc.sha256_json(s["criteria"])


def test_merge_readback_drains_without_verifier_labels(bundle, policy, monkeypatch):
    monkeypatch.setattr(bundle.runtime, "fetch_live_pr_labels", lambda spec: [])
    transport, s = NativeContents(), snapshot()
    transport.pr = observed(s, labels=False)
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)
    calls = []
    first = binding.execute(
        s["pr_target"],
        "merge",
        s,
        operation(s, action="merge"),
        lambda: calls.append(1) or {"status": "completed"},
    )
    assert first["status"] == "completed"
    _, rec = record(binding, s, "merge")
    assert rec["status"] == "succeeded"
    assert (
        binding.execute(
            s["pr_target"],
            "merge",
            s,
            operation(s, action="merge", owner="next"),
            lambda: pytest.fail("merged again"),
        )["status"]
        == "completed"
    )
    assert calls == [1]


def test_expired_restart_drains_original_without_fresh_execution_budget(
    bundle, policy, monkeypatch
):
    import datetime as dt

    transport, s = NativeContents(), snapshot()
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)
    assert (
        binding.execute(
            s["pr_target"], "post_merge_verify", s, operation(s), lambda: {"status": "completed"}
        )["status"]
        == "unknown"
    )
    transport.pr = observed(s)
    adapter, rec = record(binding, s)
    after = dt.datetime.fromisoformat(
        rec["lease"]["deadline"].replace("Z", "+00:00")
    ) + dt.timedelta(seconds=1)
    monkeypatch.setattr(bundle.runtime, "_utc_now", lambda: after)
    restarted = rb.RuntimeBinding(bundle, policy, transport=transport)
    op = operation(s, owner="successor")
    op["deadline"] = time.time() - 1
    assert restarted.reconcile(s["pr_target"], op)["status"] == "completed"
    done = adapter.store.read(rec["target_fence_key"])
    assert done["status"] == "succeeded" and done["ownership_scope"] == "reconcile_only"
    assert done["lease"]["deadline"] == rec["lease"]["deadline"]
    assert done["lease"]["epoch"] == rec["lease"]["epoch"] + 1


def test_new_dispatch_action_cannot_bypass_old_verify_unknown(bundle, policy):
    transport, s = NativeContents(), snapshot()
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)
    adapter = binding._adapter("org/repo")
    legacy = cc.ActionSpec("org/repo", 7, "verify", s["head"], "1" * 64, "old-review")

    class Port:
        def submit(self, *args):
            return "unknown"

    assert cc.submit_effect(adapter, Port(), legacy, {}, holder="legacy") == "unknown"
    transport.pr = observed(s)
    assert (
        binding.execute(
            s["pr_target"],
            "post_merge_verify",
            s,
            operation(s),
            lambda: pytest.fail("bypassed old unknown"),
        )["status"]
        == "unknown"
    )


def test_crash_before_submission_receipt_recovers_only_after_lease_expiry(
    bundle, policy, monkeypatch
):
    import datetime as dt

    transport, s = NativeContents(), snapshot()
    transport.pr = observed(s)
    binding = rb.RuntimeBinding(bundle, policy, transport=transport)

    def interrupted(*args, **kwargs):
        raise OSError("process interrupted before submitted CAS")

    monkeypatch.setattr(bundle.runtime, "mark_submitted", interrupted)
    assert (
        binding.execute(
            s["pr_target"], "post_merge_verify", s, operation(s), lambda: {"status": "completed"}
        )["status"]
        == "unknown"
    )
    _, rec = record(binding, s)
    assert rec["status"] == "intent_recorded"
    assert binding.reconcile(s["pr_target"], operation(s))["status"] == "unknown"
    after = dt.datetime.fromisoformat(
        rec["lease"]["deadline"].replace("Z", "+00:00")
    ) + dt.timedelta(seconds=1)
    monkeypatch.setattr(bundle.runtime, "_utc_now", lambda: after)
    assert binding.reconcile(s["pr_target"], operation(s, owner="restart"))["status"] == "completed"
