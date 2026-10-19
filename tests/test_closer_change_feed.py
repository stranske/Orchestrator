from __future__ import annotations

import json

import closer_change_feed as feed
import closer_queue

TARGET = "acme/widgets#42"
HEAD = "a" * 40


def _state(path, *, head=HEAD, evidence=None):
    state = closer_queue._new_state()
    state["items"][TARGET] = {
        "evidence": {"head": head, "local_packet": {"status": "usable"}, **(evidence or {})},
        "operation": None,
        "operation_history": [],
    }
    closer_queue._atomic_write(path, state)


def _event(event_id, sequence, kind="checks", *, head=HEAD, payload=None, **extra):
    return {
        "trusted": True,
        "source": "authenticated-webhook",
        "target": TARGET,
        "event_id": event_id,
        "sequence": sequence,
        "kind": kind,
        "head": head,
        "payload": payload or {"checks": "SUCCESS"},
        **extra,
    }


def test_duplicate_events_are_idempotent(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    event = _event("evt-1", 1)
    assert feed.apply_change(state, TARGET, event)["status"] == "applied"
    assert feed.apply_change(state, TARGET, event)["status"] == "duplicate"
    cached = feed.read_cached(state, TARGET)
    assert cached["snapshot"]["local_packet"] == {"status": "usable"}
    assert cached["snapshot"]["checks"] == "SUCCESS"
    assert cached["remote_changes"]["checks"]["payload"] == {"checks": "SUCCESS"}
    journal = json.loads(state.with_name("queue.json.changes.json").read_text())
    assert journal["event_ids"] == ["evt-1"]


def test_out_of_order_and_head_mismatch_fail_closed(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    feed.apply_change(state, TARGET, _event("evt-2", 2, payload={"checks": "FAILURE"}))
    result = feed.apply_change(state, TARGET, _event("evt-1", 1, payload={"checks": "SUCCESS"}))
    assert result["status"] == "out_of_order"
    assert feed.read_cached(state, TARGET)["snapshot"]["checks"] == "FAILURE"
    try:
        feed.apply_change(state, TARGET, _event("wrong-head", 3, head="b" * 40))
    except ValueError as exc:
        assert "head" in str(exc)
    else:
        raise AssertionError("mismatched head must be rejected")


def test_missed_events_require_reconciliation_and_keep_local_packet(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    result = feed.apply_change(state, TARGET, _event("gap", 4, missed_events=True))
    assert result["status"] == "reconciliation_needed"
    cached = feed.read_cached(state, TARGET)
    assert cached["reconciliation_needed"] is True
    assert "checks" in cached["incomplete_categories"]
    assert cached["snapshot"]["local_packet"] == {"status": "usable"}
    feed.apply_change(state, TARGET, _event("rescan", 5, reconciled=True))
    cached = feed.read_cached(state, TARGET)
    assert cached["reconciliation_needed"] is False
    assert "checks" not in cached["incomplete_categories"]


def test_offline_and_mixed_targets_use_only_their_local_queue_entries(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    other = "acme/widgets#43"
    current = closer_queue._read_state(state)
    current["items"][other] = {"evidence": {"head": "c" * 40, "local_packet": "other"}}
    closer_queue._atomic_write(state, current)

    # No fetcher is involved: offline reads return the cache and explicit unknowns.
    cached = feed.read_cached(state, TARGET)
    assert cached["change_feed"]["offline_safe"] is True
    assert set(cached["incomplete_categories"]) == set(feed.KINDS)
    assert feed.read_cached(state, other)["snapshot"]["local_packet"] == "other"
    feed.apply_change(
        state, TARGET, _event("reviews-1", 1, kind="reviews", payload={"threads": []})
    )
    assert "reviews" in feed.read_cached(state, TARGET)["remote_changes"]
    assert "reviews" not in feed.read_cached(state, other)["remote_changes"]


def test_untrusted_and_wrong_target_events_are_rejected(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    event = _event("evt", 1, trusted=False)
    for bad in (event, {**_event("evt2", 1), "target": "other#1"}):
        try:
            feed.apply_change(state, TARGET, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid event must be rejected")


def test_new_head_invalidates_old_head_dependent_changes(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    feed.apply_change(state, TARGET, _event("checks-1", 1))
    new_head = "d" * 40
    feed.apply_change(
        state,
        TARGET,
        _event("head-2", 2, kind="commit_head", head=new_head, payload={"head": new_head}),
    )
    cached = feed.read_cached(state, TARGET)
    assert cached["snapshot"]["head"] == new_head
    assert "checks" not in cached["remote_changes"]
    assert "checks" in cached["incomplete_categories"]
    assert cached["reconciliation_needed"] is True


def test_category_allowlist_rejects_state_spoof_and_revision_payload(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    for event in (
        _event("spoof-state", 1, kind="checks", payload={"state": "CLOSED"}),
        _event("spoof-revision", 1, kind="checks", payload={"revision": "forged"}),
    ):
        try:
            feed.apply_change(state, TARGET, event)
        except ValueError as exc:
            assert "unsupported" in str(exc)
        else:
            raise AssertionError("unsupported category field must be rejected")


def test_applied_deltas_advance_stable_local_revision(tmp_path):
    state = tmp_path / "queue.json"
    _state(state, evidence={"revision": "base-revision"})
    feed.apply_change(state, TARGET, _event("checks-1", 1))
    after_first = closer_queue._read_state(state)["items"][TARGET]
    revision_one = after_first["revision"]
    assert revision_one == after_first["evidence"]["revision"]
    assert revision_one != "base-revision"
    feed.apply_change(state, TARGET, _event("checks-1", 1))
    assert closer_queue._read_state(state)["items"][TARGET]["revision"] == revision_one
    feed.apply_change(
        state, TARGET, _event("reviews-1", 1, kind="reviews", payload={"active_threads": 0})
    )
    after_second = closer_queue._read_state(state)["items"][TARGET]
    assert after_second["revision"] == after_second["evidence"]["revision"]
    assert after_second["revision"] != revision_one


def test_full_packet_reconciliation_keeps_pending_intent_and_closes_gap(tmp_path):
    state = tmp_path / "queue.json"
    _state(state, evidence={"target": TARGET, "revision": "base"})
    current = closer_queue._read_state(state)
    current["items"][TARGET]["operation"] = {
        "id": "remote-intent",
        "status": "uncertain",
        "action": "merge",
    }
    closer_queue._atomic_write(state, current)
    feed.apply_change(state, TARGET, _event("gap", 4, missed_events=True))
    watermarks = feed.read_cached(state, TARGET)["watermarks"]
    fresh = {"target": TARGET, "head": HEAD, "revision": "fresh", "checks": "SUCCESS"}
    assert feed.record_snapshot(
        state,
        TARGET,
        fresh,
        packet_ref={"sha256": "packet"},
        expected_watermarks=watermarks,
    )
    cached = feed.read_cached(state, TARGET)
    assert cached["snapshot"]["revision"] == "fresh"
    assert not cached["incomplete_categories"]
    assert not cached["reconciliation_needed"]
    persisted = closer_queue._read_state(state)["items"][TARGET]
    assert persisted["operation"]["id"] == "remote-intent"
    assert persisted["packet_ref"] == {"sha256": "packet"}


def test_full_packet_cas_does_not_overwrite_change_arriving_during_fetch(tmp_path):
    state = tmp_path / "queue.json"
    _state(state, evidence={"target": TARGET, "revision": "base"})
    baseline = feed.read_cached(state, TARGET)["watermarks"]
    feed.apply_change(state, TARGET, _event("later-check", 1))
    stale = {"target": TARGET, "head": HEAD, "revision": "stale", "checks": "PENDING"}
    assert not feed.record_snapshot(state, TARGET, stale, expected_watermarks=baseline)
    assert feed.read_cached(state, TARGET)["snapshot"]["checks"] == "SUCCESS"
