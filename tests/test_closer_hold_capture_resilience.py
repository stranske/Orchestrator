"""Capture failures cannot starve existing owned deadlines; no remote effects."""

import copy

import pytest
from test_closer_hold_safety_contracts import NOW, world

import closer_campaign as campaigns
import closer_hold_followup as holds


def full_world(tmp_path):
    obj, paths, manifest = world(tmp_path, count=2)
    original = list(obj.read()["hold_followups"].values())
    due, candidate = original

    def fill(doc):
        journal = doc["hold_followups"]
        journal.pop(candidate["hold_id"])
        for index in range(holds.MAX_HOLDS - 1):
            row = copy.deepcopy(due)
            row.update(hold_id=f"historical-{index}", status="escalated")
            journal[row["hold_id"]] = row
        journal[due["hold_id"]]["deadline"] = NOW + 1

    obj.update(fill)
    return obj, paths, manifest, due, candidate


def test_full_journal_still_expires_existing_hold_and_owns_refused_candidate(tmp_path, monkeypatch):
    obj, paths, manifest, due, candidate = full_world(tmp_path)
    before = obj.read()["hold_followups"]
    monkeypatch.setattr(
        holds, "_probe", lambda *_a, **_k: pytest.fail("no probe on blocked capture")
    )
    doc, reports = holds.consume(obj, manifest, "offline-manifest", now=NOW + 2)
    assert doc["hold_followups"][due["hold_id"]]["status"] == "escalated"
    assert len(doc["hold_followups"]) == holds.MAX_HOLDS
    assert all(
        doc["hold_followups"][key] == row for key, row in before.items() if key != due["hold_id"]
    )
    block = doc["hold_capture"]
    assert block["status"] == "blocked" and block["authority"] is False
    assert block["owner"] == doc["owner"] and block["escalation_owner"] == doc["owner"]
    assert block["candidate_count"] == 2
    assert block["candidate_ids_sha256"] == holds._digest(
        sorted([due["hold_id"], candidate["hold_id"]])
    )
    assert block["scope"]["frontier"] == doc["worker_config"]["finite_frontier"]
    assert block["scope"]["dispatch_state"] == doc["worker_config"]["dispatch_state"]
    assert any(row["kind"] == "hold_capture_blocked" for row in reports)
    assert any(
        row["kind"] == "hold_escalated" and row["hold_id"] == due["hold_id"] for row in reports
    )
    assert paths[candidate["target"]].exists()  # Original durable ownership source retained.


@pytest.mark.parametrize("damage", ["shard", "lineage"])
def test_capture_source_failure_still_expires_existing_hold(tmp_path, monkeypatch, damage):
    obj, paths, manifest = world(tmp_path, count=2)
    due, candidate = list(obj.read()["hold_followups"].values())

    def expire(doc):
        doc["hold_followups"][due["hold_id"]]["deadline"] = NOW + 1

    obj.update(expire)
    if damage == "shard":
        paths[candidate["target"]].write_text("{malformed")
    else:
        monkeypatch.setattr(
            holds,
            "_inherit_no_start_lineage",
            lambda *_a: (_ for _ in ()).throw(ValueError("bad lineage")),
        )
        obj.update(lambda doc: doc["hold_followups"].pop(candidate["hold_id"]))
    doc, reports = holds.consume(obj, manifest, "offline-manifest", now=NOW + 2)
    assert doc["hold_followups"][due["hold_id"]]["status"] == "escalated"
    assert doc["hold_capture"]["status"] == "blocked"
    assert doc["hold_capture"]["scope"]["frontier"] == doc["worker_config"]["finite_frontier"]
    assert doc["hold_capture"]["owner"] == doc["owner"]
    assert any(row["kind"] == "hold_capture_blocked" for row in reports)


def test_saturation_restart_remains_bounded_preserves_unknown_and_original_incident(tmp_path):
    obj, _paths, manifest, due, _candidate = full_world(tmp_path)
    doc, _ = holds.consume(obj, manifest, "offline-manifest", now=NOW + 2)
    before = copy.deepcopy(doc["hold_followups"])
    for at in (NOW + 3, NOW + 100, NOW + 1000):
        doc, _ = holds.consume(campaigns.Campaign(obj.path), manifest, "offline-manifest", now=at)
        assert len(doc["hold_followups"]) == holds.MAX_HOLDS
        assert doc["hold_followups"] == before
        assert doc["hold_capture"]["first_seen_at"] == NOW + 2
        assert doc["hold_capture"]["owner"] == due["owner"]
        assert doc["hold_capture"]["authority"] is False
        assert not holds._probe_allowed(doc)


def test_full_capture_preserves_scheduled_existing_unknown_check(tmp_path, monkeypatch):
    obj, _paths, manifest, due, _candidate = full_world(tmp_path)
    obj.update(lambda doc: doc["hold_followups"][due["hold_id"]].update(deadline=NOW + 500))
    calls = []
    original_probe = holds._probe

    def probe(*args, **kwargs):
        calls.append(args[0]["hold_id"])
        return original_probe(*args, **kwargs)

    monkeypatch.setattr(holds, "_probe", probe)
    doc, reports = holds.consume(obj, manifest, "offline-manifest", now=NOW + 2)
    assert calls == [due["hold_id"]]
    row = doc["hold_followups"][due["hold_id"]]
    assert row["status"] == "waiting" and row["attempts"] == 1
    assert row["last_result"]["reason"] == "exact provider run still needs terminal reconciliation"
    assert any(item["kind"] == "hold_checked" for item in reports)
    assert not holds._probe_allowed(doc)  # No fresh PREPARE/cohort admission.


def test_repaired_source_clears_capture_block_without_replacing_original_bounds(tmp_path):
    obj, paths, manifest = world(tmp_path, count=2)
    original = copy.deepcopy(obj.read()["hold_followups"])
    path = next(iter(paths.values()))
    source = path.read_bytes()
    path.write_text("{broken")
    blocked = holds.capture(obj, now=NOW + 1)
    assert blocked["hold_capture"]["status"] == "blocked"
    path.write_bytes(source)
    recovered = holds.capture(campaigns.Campaign(obj.path), now=NOW + 2)
    assert recovered["hold_capture"]["status"] == "resolved"
    assert recovered["hold_capture"]["first_seen_at"] == NOW + 1
    assert recovered["hold_followups"] == original


def test_capture_block_cannot_probe_or_rearm_known_no_start(tmp_path, monkeypatch):
    obj, _paths, manifest = world(tmp_path, kind="role", count=2)
    candidates = list(obj.read()["hold_followups"].values())

    def saturate(doc):
        doc["hold_followups"].pop(candidates[1]["hold_id"])
        for index in range(holds.MAX_HOLDS - 1):
            row = copy.deepcopy(candidates[0])
            row.update(hold_id=f"old-{index}", status="consumed")
            doc["hold_followups"][row["hold_id"]] = row

    obj.update(saturate)
    monkeypatch.setattr(holds, "_probe", lambda *_a, **_k: pytest.fail("no fresh reentry"))
    doc, _ = holds.consume(obj, manifest, "offline-manifest", now=NOW + 1)
    assert doc["hold_capture"]["status"] == "blocked"
    assert doc["hold_followups"][candidates[0]["hold_id"]]["attempts"] == 0
    assert holds.rearm(obj, doc, manifest, now=NOW + 1) == doc
    assert doc["generation"] == 1


def test_failed_capture_cannot_assign_incident_to_changed_campaign_owner(tmp_path, monkeypatch):
    obj, paths, _manifest = world(tmp_path)
    next(iter(paths.values())).write_text("{broken")
    original = obj.update
    changed = False

    def race(mutate, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            original(lambda doc: doc.update(owner="new-owner"))
        return original(mutate, **kwargs)

    monkeypatch.setattr(obj, "update", race)
    with pytest.raises(ValueError, match="owner context changed"):
        holds.capture(obj, now=NOW + 1)
    assert "hold_capture" not in obj.read()


def test_oversized_preexisting_journal_refuses_before_validation_or_sort(tmp_path, monkeypatch):
    obj, _paths, manifest = world(tmp_path)

    def oversize(doc):
        seed = next(iter(doc["hold_followups"].values()))
        doc["hold_followups"] = {}
        for index in range(holds.MAX_HOLDS + 1):
            row = copy.deepcopy(seed)
            row.update(hold_id=f"oversized-{index}", status="escalated")
            doc["hold_followups"][row["hold_id"]] = row

    obj.update(oversize)
    monkeypatch.setattr(holds, "validate_hold", lambda *_: pytest.fail("oversized row validation"))
    with pytest.raises(ValueError, match="bound|journal"):
        holds.consume(obj, manifest, "offline-manifest", now=NOW + 2)
    assert len(obj.read()["hold_followups"]) == holds.MAX_HOLDS + 1
    assert obj.read()["hold_capture"]["status"] == "blocked"


@pytest.mark.parametrize("invalid", [None, [], ["row"], "journal"])
def test_nonmapping_journal_refuses_before_consumption(tmp_path, monkeypatch, invalid):
    obj, _paths, manifest = world(tmp_path)
    obj.update(lambda doc: doc.update(hold_followups=invalid))
    monkeypatch.setattr(
        holds, "validate_hold", lambda *_: pytest.fail("invalid journal validation")
    )
    with pytest.raises(ValueError, match="journal"):
        holds.consume(obj, manifest, "offline-manifest", now=NOW + 2)
    assert obj.read()["hold_followups"] == invalid
