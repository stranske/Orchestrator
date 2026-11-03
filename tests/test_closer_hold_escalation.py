"""Real Campaign + rendered existing receiver/controller source; no providers."""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path

import pytest

import closer_campaign as campaigns
import closer_hold_escalation as escalation
from closer_hold_followup import _digest

FIXTURES = Path(__file__).parent / "fixtures"


def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = (tmp_path / "reviewed-repo-automation-controller").resolve()
    receiver = (tmp_path / escalation.RECIPIENT).resolve()
    root.mkdir()
    receiver.mkdir()
    manifest_path = tmp_path / "manifest.json"
    consumer = Path(escalation.__file__).resolve()
    consumer_sha = escalation.sha(consumer.read_bytes())
    controller = (FIXTURES / "continuity_controller_contract.py.txt").read_text()
    # Fixture contains recorded main, not an executable __main__ tail.
    if 'if __name__ == "__main__":' not in controller:
        controller += '\nif __name__ == "__main__":\n    main()\n'
    (root / "demand_router.py").write_text(
        escalation.render_controller(controller, str(manifest_path), str(consumer), consumer_sha)
    )
    refs = {}
    for phase, filename in (("preflight", "preflight.py"), ("terminal", "record_run.py")):
        source = (FIXTURES / ("escalation_" + filename + ".txt")).read_text()
        path = receiver / filename
        path.write_text(
            escalation.render_receiver(
                source, phase, str(manifest_path), str(consumer), consumer_sha
            )
        )
        refs[phase] = {"path": str(path), "sha256": escalation.sha(path.read_bytes())}
    dbpath = tmp_path / "app.db"
    now = time.time()
    with sqlite3.connect(dbpath) as db:
        db.execute(
            "CREATE TABLE automation_runs(thread_id TEXT,automation_id TEXT,status TEXT,source_cwd TEXT,created_at INTEGER,updated_at INTEGER)"
        )
        db.execute(
            "INSERT INTO automation_runs VALUES(?,?,?,?,?,?)",
            (
                "runtime-thread",
                escalation.RECIPIENT,
                "RUNNING",
                str(tmp_path),
                int((now + 0.01) * 1000),
                int(now * 1000),
            ),
        )
    monkeypatch.setenv("CODEX_THREAD_ID", "runtime-thread")
    monkeypatch.setenv("CODEX_SESSION_ID", "runtime-session")
    policy = {
        "schema": "closer-hold-escalation-route/v1",
        "recipient": escalation.RECIPIENT,
        "controller_root": str(root),
        "receiver_root": str(receiver),
        "destination": str(root / "escalator-assignment.json"),
        "ack_window_seconds": 600,
        "receiver_source_cwd": str(tmp_path),
        "app_db": str(dbpath),
        "controller_source": {
            "path": str(root / "demand_router.py"),
            "sha256": escalation.sha((root / "demand_router.py").read_bytes()),
        },
        "receiver_sources": refs,
        "consumer_source": {"path": str(consumer), "sha256": consumer_sha},
    }
    campaign = campaigns.Campaign(tmp_path / "campaign.json")
    doc = campaign.begin(
        campaign_id="campaign",
        owner="actual-owner",
        targets=["offline/repo#1"],
        release_pin="a" * 64,
        worker_config={"hold_escalation_route": policy},
    )
    hold = {
        "schema": "closer-hold-followup/v1",
        "hold_id": "hold",
        "campaign_id": "campaign",
        "owner": "actual-owner",
        "generation": 1,
        "release_pin": "a" * 64,
        "config_sha256": _digest(doc["worker_config"]),
        "target": "offline/repo#1",
        "revision": "revision",
        "run_id": "original-worker",
        "stage": "act",
        "execution_state": "action_outcome_unknown",
        "status": "escalated",
        "source": {"operation_id": "original-operation"},
        "release_proof": {},
        "event_selector": {},
        "created_at": now - 100,
        "next_check_at": now - 50,
        "deadline": now - 1,
        "attempt_budget": 1,
        "attempts": 1,
        "escalation_owner": "actual-owner",
    }
    campaign.update(lambda value: value.update(hold_followups={"hold": hold}))
    manifest = {
        "package_digest": "a" * 64,
        "mechanism_contract": {"hold_escalation_route": policy},
        "hold_escalation_campaign_path": str(campaign.path),
    }
    manifest_path.write_bytes(escalation.encoded(manifest))
    (root / "escalator-assignment.json").write_text('{"targets":[],"model":"unchanged"}')
    return campaign, manifest, str(manifest_path), policy, now


def run_preflight(policy, capsys):
    path = Path(policy["receiver_sources"]["preflight"]["path"])
    namespace = {"__file__": str(path), "__name__": "offline_receiver"}
    exec(compile(path.read_bytes(), str(path), "exec", dont_inherit=True), namespace)
    namespace["inventory"] = lambda: []
    namespace["guard"] = lambda: {}
    namespace["main"]()
    return json.loads(capsys.readouterr().out)


def test_actual_receiver_delivery_ack_duplicate_restart(tmp_path, monkeypatch, capsys):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    original = copy.deepcopy(c.read()["hold_followups"])
    doc, report = escalation.service(c, m, p, now=now)
    assert report[0]["kind"] == "escalation_delivered"
    payload = Path(policy["destination"]).read_bytes()
    # Receiver actor must start after durable request, independent of latest run.
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    output = run_preflight(policy, capsys)
    assert output["current_run"]["thread_id"] == "runtime-thread"
    doc = c.read()
    row = next(iter(doc["hold_escalations"].values()))
    assert row["status"] == "acknowledged"
    assert row["acknowledgment"]["request"]["operation_id"] == "original-operation"
    assert row["acknowledgment"]["authority"] is False
    assert doc["hold_followups"] == original
    escalation.service(campaigns.Campaign(c.path), m, p, now=now + 2)
    run_preflight(policy, capsys)
    assert Path(policy["destination"]).read_bytes() == payload
    assert c.read()["hold_escalations"] == doc["hold_escalations"]


def test_crash_after_replace_reconciles_exact_bytes_without_write(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    real = escalation.atomic

    def crash(path, data):
        real(path, data)
        raise OSError("crash after replace")

    monkeypatch.setattr(escalation, "atomic", crash)
    with pytest.raises(OSError):
        escalation.service(c, m, p, now=now)
    row = next(iter(c.read()["hold_escalations"].values()))
    assert row["status"] == "outcome_unknown"
    monkeypatch.setattr(escalation, "atomic", lambda *args: pytest.fail("duplicate write"))
    doc, reports = escalation.service(c, m, p, now=now + 1)
    assert reports[0]["kind"] == "escalation_delivered"
    assert (
        next(iter(doc["hold_escalations"].values()))["delivery_operation_id"]
        == row["delivery_operation_id"]
    )


def test_unknown_different_bytes_never_retry(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    monkeypatch.setattr(
        escalation, "atomic", lambda *args: (_ for _ in ()).throw(OSError("unknown"))
    )
    with pytest.raises(OSError):
        escalation.service(c, m, p, now=now)
    doc, reports = escalation.service(c, m, p, now=now + 1)
    assert reports[0]["kind"] == "escalation_delivery_unknown"
    assert next(iter(doc["hold_escalations"].values()))["status"] == "outcome_unknown"


@pytest.mark.parametrize("fault", ["absent", "ambiguous", "foreign", "stale", "cwd", "status"])
def test_actual_receiver_refuses_wrong_runtime(tmp_path, monkeypatch, capsys, fault):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    with sqlite3.connect(policy["app_db"]) as db:
        if fault == "absent":
            db.execute("DELETE FROM automation_runs")
        elif fault == "ambiguous":
            db.execute("INSERT INTO automation_runs SELECT * FROM automation_runs")
        else:
            column, value = {
                "foreign": ("automation_id", "foreign"),
                "stale": ("created_at", 1),
                "cwd": ("source_cwd", "/foreign"),
                "status": ("status", "COMPLETED"),
            }[fault]
            db.execute(f"UPDATE automation_runs SET {column}=?", (value,))
    with pytest.raises(ValueError):
        run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "delivered"


def test_no_caller_json_actor_proof(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    with pytest.raises(ValueError, match="actual pinned receiver"):
        escalation.receiver_ack(p, policy["receiver_sources"]["preflight"]["path"])


def test_overdue_never_renews_or_releases_unknown(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 601)
    doc, report = escalation.service(c, m, p, now=now + 601)
    assert report[0]["kind"] == "escalation_ack_overdue"
    assert doc["hold_followups"]["hold"]["execution_state"] == "action_outcome_unknown"
    assert doc["hold_followups"]["hold"]["status"] == "escalated"


def test_missing_policy_stays_escalated(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    m["mechanism_contract"] = {}
    Path(p).write_bytes(escalation.encoded(m))
    original = Path(policy["destination"]).read_bytes()
    doc, report = escalation.service(c, m, p, now=now)
    assert report[0]["kind"] == "escalation_unrouted"
    assert "hold_escalations" not in doc
    assert Path(policy["destination"]).read_bytes() == original


def test_actual_controller_main_preserves_delivery_then_actual_receiver_ack(
    tmp_path, monkeypatch, capsys
):
    import argparse
    import fcntl
    from types import SimpleNamespace

    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    root = Path(policy["controller_root"])
    lanes = dict.fromkeys(("opener", "closer", "orphan", "escalator", "maintenance"))
    settings = {name: {"model": "unchanged", "reasoning_effort": "low"} for name in lanes}
    namespace = {
        "__name__": "offline_controller",
        "Path": Path,
        "argparse": argparse,
        "fcntl": fcntl,
        "json": json,
        "time": time,
        "ROOT": root,
        "STATE": root / "state.json",
        "PLAN": root / "plan.json",
        "LANES": lanes,
        "inventory": SimpleNamespace(
            scoped_blocker_states=lambda: (set(), set(), set()),
            ROOT=root.parent,
            iso_now=lambda: "offline",
        ),
        "load": lambda path, default: json.loads(path.read_bytes()) if path.exists() else default,
        "save": lambda path, value: escalation.atomic(path, escalation.encoded(value)),
        "app_settings": lambda: settings,
        "collect": lambda: ({"nodes": [], "issueCount": 0}, {"nodes": []}, {}, []),
        "running_lanes": lambda: set(),
        "running_lane_owners": lambda *args: {},
        "reconcile_ownership": lambda *args: ({}, []),
        "maintenance": SimpleNamespace(
            budget=SimpleNamespace(summary=lambda: {}),
            read_control=lambda: {},
            existing_worker=lambda: None,
        ),
        "maintenance_admissions": lambda *args: [],
        "accountability": SimpleNamespace(evaluate=lambda *args: ({}, [], [], [])),
        "ownership_gaps": lambda *args: [],
        "ownership_coverage_warnings": lambda *args: [],
        "repo_maintenance_admissions": lambda *args: {"assignments": [], "errors": []},
        "error_freeze": lambda *args: {"lapsed": False},
        "desired_settings": lambda *args: settings,
        "error_freeze_message": lambda *args: None,
        "frozen_lanes_message": lambda *args: None,
        "budget_alerts": lambda *args: None,
        "write_operational_memory": lambda *args: None,
    }
    path = root / "demand_router.py"
    exec(compile(path.read_bytes(), str(path), "exec", dont_inherit=True), namespace)
    namespace["build_routes"] = lambda *args: ({name: [] for name in lanes}, {}, [], [])
    assert namespace["main"]([]) == 0
    capsys.readouterr()
    current = json.loads(Path(policy["destination"]).read_bytes())
    assert current["targets"] == []
    assert len(current["closer_hold_escalations"]) == 1
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "acknowledged"


def test_actual_terminal_recorder_uses_exact_actor_and_ack(tmp_path, monkeypatch, capsys):
    import sys

    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    path = Path(policy["receiver_sources"]["terminal"]["path"])
    namespace = {"__file__": str(path), "__name__": "offline_terminal"}
    exec(compile(path.read_bytes(), str(path), "exec", dont_inherit=True), namespace)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(path),
            "--payload-json",
            json.dumps({"outcome_class": "no_productive_action", "decision": "received"}),
            "--root",
            policy["receiver_root"],
            "--app-db",
            policy["app_db"],
        ],
    )
    namespace["main"]()
    capsys.readouterr()
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "acknowledged"
    ledger = Path(policy["receiver_root"], "metrics/run-ledger.jsonl").read_text()
    assert json.loads(ledger)["thread_id"] == "runtime-thread"


@pytest.mark.parametrize(
    "field", ["operation_id", "run_id", "recipient", "destination", "revision", "hold_id"]
)
def test_reordered_or_foreign_assignment_request_cannot_ack(tmp_path, monkeypatch, capsys, field):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    path = Path(policy["destination"])
    assignment = json.loads(path.read_bytes())
    next(iter(assignment["closer_hold_escalations"].values()))[field] = "foreign"
    path.write_text(json.dumps(assignment))
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "delivered"


def test_reorder_addition_does_not_change_exact_receipt(tmp_path, monkeypatch, capsys):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    path = Path(policy["destination"])
    assignment = json.loads(path.read_bytes())
    assignment["unrelated"] = True
    path.write_text(json.dumps(assignment, indent=3))
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "acknowledged"


def test_known_not_started_read_failure_and_unknown_controller_no_new_delivery(
    tmp_path, monkeypatch
):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    path = Path(policy["destination"])
    path.write_text("malformed")
    with pytest.raises(ValueError):
        escalation.service(c, m, p, now=now)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "confirmed_not_started"
    path.write_text('{"targets":[]}')
    monkeypatch.setattr(
        escalation, "atomic", lambda *args: (_ for _ in ()).throw(OSError("unknown"))
    )
    with pytest.raises(OSError):
        escalation.service(c, m, p, now=now + 1)
    preserved = escalation.preserve_assignment(p, policy["controller_root"], {"targets": []})
    assert "closer_hold_escalations" not in preserved


@pytest.mark.parametrize("field", ["generation", "release_pin", "owner"])
def test_campaign_identity_drift_never_delivers(tmp_path, monkeypatch, field):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    original = Path(policy["destination"]).read_bytes()
    c.update(lambda value: value.update({field: 2 if field == "generation" else "foreign"}))
    if field == "release_pin":
        _, reports = escalation.service(c, m, p, now=now)
        assert reports[0]["kind"] == "escalation_unrouted"
    else:
        with pytest.raises(ValueError):
            escalation.service(c, m, p, now=now)
    assert Path(policy["destination"]).read_bytes() == original


def test_controller_reconciles_exact_crash_before_assignment_reformat(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    real = escalation.atomic

    def crash(path, payload):
        real(path, payload)
        raise OSError("after replace")

    monkeypatch.setattr(escalation, "atomic", crash)
    with pytest.raises(OSError):
        escalation.service(c, m, p, now=now)
    updated = escalation.preserve_assignment(
        p, policy["controller_root"], {"targets": [], "new_discovery": True}
    )
    assert "closer_hold_escalations" in updated
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "delivered"


def test_actual_receiver_ignores_newer_unrelated_run(tmp_path, monkeypatch, capsys):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    with sqlite3.connect(policy["app_db"]) as db:
        db.execute(
            "INSERT INTO automation_runs VALUES(?,?,?,?,?,?)",
            (
                "newest-foreign-thread",
                escalation.RECIPIENT,
                "RUNNING",
                str(tmp_path),
                int((now + 2) * 1000),
                int((now + 2) * 1000),
            ),
        )
    monkeypatch.setattr(escalation.time, "time", lambda: now + 3)
    output = run_preflight(policy, capsys)
    assert output["current_run"]["thread_id"] == "runtime-thread"
    assert (
        next(iter(c.read()["hold_escalations"].values()))["acknowledgment"]["actor"]["thread_id"]
        == "runtime-thread"
    )


def test_actual_hold_consume_calls_service_without_model_or_probe(tmp_path, monkeypatch):
    import closer_hold_followup as holds

    c, m, p, policy, now = world(tmp_path, monkeypatch)
    monkeypatch.setattr(holds, "capture", lambda campaign, now: campaign.read())
    monkeypatch.setattr(
        holds, "_probe", lambda *args, **kwargs: pytest.fail("escalated hold probed")
    )
    doc, reports = holds.consume(c, m, p, now=now)
    assert reports[0]["kind"] == "escalation_delivered"
    assert next(iter(doc["hold_escalations"].values()))["status"] == "delivered"


@pytest.mark.parametrize(
    "field", ["run_id", "operation_id", "ack_deadline", "recipient", "revision"]
)
def test_persisted_request_tampering_refuses_receiver(tmp_path, monkeypatch, capsys, field):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)

    def tamper(doc):
        request = next(iter(doc["hold_escalations"].values()))["request"]
        request[field] = now + 10000 if field == "ack_deadline" else "foreign"

    c.update(tamper)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    with pytest.raises(ValueError):
        run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "delivered"


def test_interleaved_service_claim_reuses_exact_delivered_request(tmp_path, monkeypatch):
    """A newer real service completes between the outer read and claim lock."""
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    real_update = c.update
    interleaved = []
    writes = []
    real_atomic = escalation.atomic

    def update(callback, **kwargs):
        if not interleaved:
            interleaved.append(True)
            competing = campaigns.Campaign(c.path)
            inner_doc, inner_reports = escalation.service(competing, m, p, now=now + 0.1)
            interleaved.append((inner_doc, inner_reports))
        return real_update(callback, **kwargs)

    def atomic(path, payload):
        writes.append((path, payload))
        real_atomic(path, payload)

    monkeypatch.setattr(c, "update", update)
    monkeypatch.setattr(escalation, "atomic", atomic)
    doc, reports = escalation.service(c, m, p, now=now)
    inner_doc, inner_reports = interleaved[1]
    assert inner_reports[0]["kind"] == "escalation_delivered"
    assert reports == []
    assert len(writes) == 1
    assert len(doc["hold_escalations"]) == 1
    assert doc["hold_escalations"] == inner_doc["hold_escalations"]
    request_id, row = next(iter(doc["hold_escalations"].items()))
    assert row["request"]["created_at"] == now + 0.1
    assert set(json.loads(Path(policy["destination"]).read_bytes())["closer_hold_escalations"]) == {
        request_id
    }
    restarted, reports = escalation.service(campaigns.Campaign(c.path), m, p, now=now + 1)
    assert reports == []
    assert restarted["hold_escalations"] == doc["hold_escalations"]
    assert len(writes) == 1


def test_stale_claimed_state_reread_after_competing_delivery(tmp_path, monkeypatch):
    """A real competing service finishes after claim, before demand.lock."""
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    real_flock = escalation.fcntl.flock
    real_atomic = escalation.atomic
    interleaved = []
    writes = []

    def flock(stream, operation):
        if str(getattr(stream, "name", "")).endswith(".demand.lock") and not interleaved:
            interleaved.append(True)
            inner_doc, inner_reports = escalation.service(
                campaigns.Campaign(c.path), m, p, now=now + 0.1
            )
            interleaved.append((inner_doc, inner_reports))
        return real_flock(stream, operation)

    def atomic(path, payload):
        writes.append((path, payload))
        real_atomic(path, payload)

    monkeypatch.setattr(escalation.fcntl, "flock", flock)
    monkeypatch.setattr(escalation, "atomic", atomic)
    doc, reports = escalation.service(c, m, p, now=now)
    inner_doc, inner_reports = interleaved[1]
    assert inner_reports[0]["kind"] == "escalation_delivered"
    assert not reports and len(writes) == 1
    assert doc["hold_escalations"] == inner_doc["hold_escalations"]
    assert len(doc["hold_escalations"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "foreign-operation"),
        ("request_id", "foreign-request"),
        ("destination", "/foreign/assignment.json"),
        ("payload_sha256", "0" * 64),
        ("readback_at", 0),
        ("readback_at", float("inf")),
    ],
)
def test_actual_receiver_refuses_corrupted_delivery_receipt(
    tmp_path, monkeypatch, capsys, field, value
):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)

    def tamper(doc):
        next(iter(doc["hold_escalations"].values()))["delivery_receipt"][field] = value

    c.update(tamper)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    with pytest.raises(ValueError):
        run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "delivered"


def test_actual_receiver_refuses_changed_original_payload(tmp_path, monkeypatch, capsys):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    row = next(iter(c.read()["hold_escalations"].values()))
    Path(row["payload_ref"]).write_text('{"closer_hold_escalations":{}}')
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    with pytest.raises(ValueError):
        run_preflight(policy, capsys)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == "delivered"


def test_actual_receiver_lock_wait_crosses_deadline_without_backdated_ack(
    tmp_path, monkeypatch, capsys
):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    clock = [now + 1]
    monkeypatch.setattr(escalation.time, "time", lambda: clock[0])
    real_flock = escalation.fcntl.flock

    def flock(stream, operation):
        result = real_flock(stream, operation)
        if str(getattr(stream, "name", "")).endswith(".demand.lock"):
            clock[0] = now + 601
        return result

    monkeypatch.setattr(escalation.fcntl, "flock", flock)
    run_preflight(policy, capsys)
    row = next(iter(c.read()["hold_escalations"].values()))
    assert row["status"] == "delivered"
    assert "acknowledgment" not in row and "acknowledged_at" not in row


def test_delivery_lock_wait_uses_actual_clock_not_scheduled_timestamp(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    original = Path(policy["destination"]).read_bytes()
    clock = [now]
    monkeypatch.setattr(escalation.time, "time", lambda: clock[0])
    real_flock = escalation.fcntl.flock

    def flock(stream, operation):
        result = real_flock(stream, operation)
        if str(getattr(stream, "name", "")).endswith(".demand.lock"):
            clock[0] = now + 601
        return result

    monkeypatch.setattr(escalation.fcntl, "flock", flock)
    doc, reports = escalation.service(c, m, p, now=now)
    assert reports[0]["kind"] == "escalation_ack_overdue"
    assert next(iter(doc["hold_escalations"].values()))["status"] == "ack_overdue"
    assert Path(policy["destination"]).read_bytes() == original


def test_receipt_readback_after_deadline_never_records_timely_delivery(tmp_path, monkeypatch):
    c, m, p, policy, now = world(tmp_path, monkeypatch)
    clock = [now]
    monkeypatch.setattr(escalation.time, "time", lambda: clock[0])
    real_atomic = escalation.atomic

    def atomic(path, payload):
        real_atomic(path, payload)
        clock[0] = now + 601

    monkeypatch.setattr(escalation, "atomic", atomic)
    doc, reports = escalation.service(c, m, p, now=now)
    row = next(iter(doc["hold_escalations"].values()))
    assert reports[0]["kind"] == "escalation_ack_overdue"
    assert row["status"] == "ack_overdue"
    assert row["delivery_receipt"]["readback_at"] == now + 601
    assert "acknowledgment" not in row


@pytest.mark.parametrize("hold_status", [None, "waiting", "checking", "released", "consumed"])
def test_no_escalation_work_is_quiet_without_route(tmp_path, monkeypatch, hold_status):
    c, m, p, _policy, now = world(tmp_path, monkeypatch)

    def select(doc):
        if hold_status is None:
            doc.pop("hold_followups")
        else:
            doc["hold_followups"]["hold"]["status"] = hold_status

    c.update(select)
    before = c.path.read_bytes()
    monkeypatch.setattr(
        escalation, "route", lambda *_: pytest.fail("quiet service must not require a route")
    )
    doc, report = escalation.service(c, {}, "unconfigured-manifest", now=now)
    assert report == [] and doc == c.read() and c.path.read_bytes() == before


@pytest.mark.parametrize("status", ["confirmed_not_started", "outcome_unknown", "delivered"])
def test_pending_request_is_not_quiet_without_route(tmp_path, monkeypatch, status):
    c, m, p, _policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)

    def pending(doc):
        doc["hold_followups"]["hold"]["status"] = "waiting"
        next(iter(doc["hold_escalations"].values()))["status"] = status

    c.update(pending)
    before = c.path.read_bytes()
    doc, report = escalation.service(c, {}, "unconfigured-manifest", now=now)
    assert report[0]["kind"] == "escalation_unrouted"
    assert c.path.read_bytes() == before and doc == c.read()


@pytest.mark.parametrize("terminal", ["acknowledged", "ack_overdue"])
def test_finished_request_without_escalated_hold_is_quiet(tmp_path, monkeypatch, capsys, terminal):
    c, m, p, _policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    if terminal == "acknowledged":
        monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
        run_preflight(_policy, capsys)
    else:
        monkeypatch.setattr(escalation.time, "time", lambda: now + 601)
        escalation.service(c, m, p, now=now + 601)
    assert next(iter(c.read()["hold_escalations"].values()))["status"] == terminal
    c.update(lambda doc: doc["hold_followups"]["hold"].update(status="resolved"))
    before = c.path.read_bytes()
    monkeypatch.setattr(escalation, "route", lambda *_: pytest.fail("finished work is quiet"))
    _doc, report = escalation.service(c, {}, "unconfigured-manifest", now=now + 602)
    assert report == [] and c.path.read_bytes() == before


@pytest.mark.parametrize(
    "corruption",
    [
        "holds_none",
        "holds_list",
        "hold_row",
        "journal_none",
        "journal_list",
        "request_row",
        "request_status",
    ],
)
def test_malformed_journal_never_proves_empty(tmp_path, monkeypatch, corruption):
    c, m, p, _policy, now = world(tmp_path, monkeypatch)
    if corruption == "request_status":
        escalation.service(c, m, p, now=now)

    def damage(doc):
        doc["hold_followups"]["hold"]["status"] = "waiting"
        if corruption == "holds_none":
            doc["hold_followups"] = None
        elif corruption == "holds_list":
            doc["hold_followups"] = []
        elif corruption == "hold_row":
            doc["hold_followups"]["hold"] = {}
        elif corruption == "journal_none":
            doc["hold_escalations"] = None
        elif corruption == "journal_list":
            doc["hold_escalations"] = []
        elif corruption == "request_row":
            doc["hold_escalations"] = {"bad": {}}
        else:
            next(iter(doc["hold_escalations"].values()))["status"] = "unrecognized"

    c.update(damage)
    before = c.path.read_bytes()
    monkeypatch.setattr(escalation, "route", lambda *_: pytest.fail("corruption cannot be empty"))
    with pytest.raises(ValueError):
        escalation.service(c, {}, "unconfigured-manifest", now=now)
    assert c.path.read_bytes() == before


def test_pending_request_with_changed_hold_does_not_silently_become_quiet(tmp_path, monkeypatch):
    c, m, p, _policy, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    c.update(lambda doc: doc["hold_followups"]["hold"].update(status="waiting"))
    before = c.path.read_bytes()
    with pytest.raises(ValueError, match="original escalated hold changed"):
        escalation.service(c, m, p, now=now + 1)
    assert c.path.read_bytes() == before
