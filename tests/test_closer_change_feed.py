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
    return {"trusted": True, "source": "authenticated-webhook", "target": TARGET,
            "event_id": event_id, "sequence": sequence, "kind": kind,
            "head": head, "payload": payload or {"state": "success"}, **extra}


def test_duplicate_events_are_idempotent(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    event = _event("evt-1", 1)
    assert feed.apply_change(state, TARGET, event)["status"] == "applied"
    assert feed.apply_change(state, TARGET, event)["status"] == "duplicate"
    cached = feed.read_cached(state, TARGET)
    assert cached["snapshot"]["local_packet"] == {"status": "usable"}
    assert cached["snapshot"]["state"] == "success"
    assert cached["remote_changes"]["checks"]["payload"] == {"state": "success"}
    journal = json.loads(state.with_name("queue.json.changes.json").read_text())
    assert journal["event_ids"] == ["evt-1"]


def test_out_of_order_and_head_mismatch_fail_closed(tmp_path):
    state = tmp_path / "queue.json"
    _state(state)
    feed.apply_change(state, TARGET, _event("evt-2", 2, payload={"state": "failure"}))
    result = feed.apply_change(state, TARGET, _event("evt-1", 1, payload={"state": "success"}))
    assert result["status"] == "out_of_order"
    assert feed.read_cached(state, TARGET)["snapshot"]["state"] == "failure"
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
    feed.apply_change(state, TARGET, _event("reviews-1", 1, kind="reviews",
                                            payload={"threads": []}))
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
    feed.apply_change(state, TARGET, _event("head-2", 2, kind="commit_head",
                                            head=new_head, payload={"head": new_head}))
    cached = feed.read_cached(state, TARGET)
    assert cached["snapshot"]["head"] == new_head
    assert "checks" not in cached["remote_changes"]
    assert "checks" in cached["incomplete_categories"]
    assert cached["reconciliation_needed"] is True
