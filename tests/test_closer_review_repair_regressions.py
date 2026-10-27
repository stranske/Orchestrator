"""Bounded causal regressions for the local #963 review repair."""

import pytest
from test_closer_hold_escalation import run_preflight, world

import closer_frontier_policy as policy
import closer_hold_escalation as escalation
import closer_pipeline as pipeline
import closer_resources as resources
import closer_stage_continuation as continuation


def test_acknowledged_receiver_survives_resolved_hold_and_pruned_payload(
    tmp_path, monkeypatch, capsys
):
    c, m, p, route, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    run_preflight(route, capsys)
    row = next(iter(c.read()["hold_escalations"].values()))
    assert row["status"] == "acknowledged"
    c.update(lambda doc: doc["hold_followups"]["hold"].update(status="resolved"))
    from pathlib import Path

    Path(row["payload_ref"]).unlink()
    before = c.path.read_bytes()
    run_preflight(route, capsys)
    assert c.path.read_bytes() == before


@pytest.mark.parametrize("missing", [False, True])
def test_assignment_without_outstanding_delivery_needs_no_route(tmp_path, monkeypatch, missing):
    c, m, p, route, now = world(tmp_path, monkeypatch)
    if missing:
        c.path.unlink()
    monkeypatch.setattr(escalation, "route", lambda *_: pytest.fail("empty work needs no route"))
    assignment = {"targets": ["untouched"], "nested": {"value": 1}}
    result = escalation.preserve_assignment(p, "unrelated-controller", assignment)
    assert result == assignment and result is not assignment


def test_retained_receipt_replay_at_cap_keeps_bound_and_conflict_checks(tmp_path, monkeypatch):
    target = "offline/repo#1"
    frontier = policy.make_frontier_from_observations(
        {
            target: {
                "target": target,
                "revision": "r",
                "eligible": True,
                "action": "merge",
                "operation_binding": {"pr_target": target, "source_targets": []},
            }
        },
        owner="owner",
        frontier_id="child",
    )
    root = tmp_path / "resources"
    root.mkdir()
    monkeypatch.setattr(resources, "scoped_root", lambda _: root)
    receipt = {
        "round_id": "existing",
        "finite_frontier": frontier,
        "assignment": [target],
        "run_ids": {target: "run"},
    }
    monkeypatch.setattr(resources, "read_round_receipt", lambda _: (receipt, "a" * 64))
    native = {
        "round_id": "existing",
        "receipt_path": str(root / "rounds" / "existing.json"),
        "receipt_sha256": "a" * 64,
    }
    path = tmp_path / "pipeline.json"
    state = pipeline._empty()
    state["stage_successors"] = {"child": {"finite_frontier": frontier, "receipt_refs": []}}
    pipeline._save(path, state)
    retained = continuation.retain_receipt(
        path, root, target=target, frontier=frontier, native=native
    )
    state = pipeline._load(path)
    state["stage_successors"]["child"]["receipt_refs"] += [{"round_id": str(i)} for i in range(31)]
    pipeline._save(path, state)
    before = path.read_bytes()
    assert (
        continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native)
        == retained
    )
    assert path.read_bytes() == before
    receipt["round_id"] = native["round_id"] = "new"
    native["receipt_path"] = str(root / "rounds" / "new.json")
    with pytest.raises(ValueError, match="retention bound"):
        continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native)
    assert path.read_bytes() == before
    receipt["round_id"] = native["round_id"] = "existing"
    native["receipt_path"] = str(root / "rounds" / "existing.json")
    receipt["run_ids"] = {target: "different-run"}
    with pytest.raises(ValueError, match="identity conflicts"):
        continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "unsupported"},
        {"schema": "closer-acceptance-workload/v1", "evidence_kind": "synthetic_fixture", "now": 1},
        {
            "schema": "closer-acceptance-workload/v1",
            "evidence_kind": "synthetic_fixture",
            "receipts": [],
            "now": float("nan"),
        },
        [],
    ],
)
def test_malformed_measurement_has_operator_error_without_traceback(tmp_path, document):
    import json
    import subprocess
    import sys

    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(document))
    result = subprocess.run(
        [sys.executable, "scripts/measure_closer_acceptance.py", str(path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "invalid workload" in result.stderr and "Traceback" not in result.stderr


def test_terminal_fixture_concurrent_coverage_and_idempotent_ledger(tmp_path):
    import json
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).parent / "fixtures" / "escalation_record_run.py.txt"
    root = tmp_path / "receiver"
    root.mkdir()
    (root / "memory.md").write_text(
        json.dumps({"coverage_json": {"old/repo#1": {"head": "old-head"}}})
    )
    commands = []
    for i in range(8):
        db = tmp_path / f"run-{i}.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE automation_runs(thread_id TEXT,created_at INTEGER,automation_id TEXT)"
            )
            conn.execute(
                "INSERT INTO automation_runs VALUES(?,?,?)",
                (f"thread-{i}", 1, "reviewed-repo-stall-escalator"),
            )
        payload = {
            "outcome_class": "stall_but_blocked",
            "decision": "bounded fixture",
            "selected_target": f"offline/repo#{i}",
            "head": f"head-{i}",
        }
        commands.append(
            [
                sys.executable,
                str(script),
                "--payload-json",
                json.dumps(payload),
                "--root",
                str(root),
                "--app-db",
                str(db),
            ]
        )
    processes = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for cmd in commands
    ]
    for proc in processes:
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, err
    coverage = json.loads((root / "memory.md").read_text())["coverage_json"]
    assert len(coverage) == 9 and coverage["old/repo#1"]["head"] == "old-head"
    assert all(coverage[f"offline/repo#{i}"]["head"] == f"head-{i}" for i in range(8))
    retry = subprocess.run(commands[0], capture_output=True, text=True, timeout=5)
    assert retry.returncode == 0, retry.stderr
    coverage = json.loads((root / "memory.md").read_text())["coverage_json"]
    ledger = root / "metrics" / "run-ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 8 and len({r["thread_id"] for r in rows}) == 8
    missing = list(commands[0])
    missing[missing.index("--payload-json") + 1] = json.dumps(
        {
            "outcome_class": "stall_but_blocked",
            "decision": "head unavailable",
            "selected_target": "offline/repo#0",
        }
    )
    result = subprocess.run(missing, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((root / "memory.md").read_text())["coverage_json"]["offline/repo#0"]
        == coverage["offline/repo#0"]
    )
    assert len(ledger.read_text().splitlines()) == 8


@pytest.mark.parametrize("state,exited", [("Z", True), ("R", False)])
def test_nonchild_exit_detects_zombie_and_tolerates_permission_error(monkeypatch, state, exited):
    from types import SimpleNamespace

    import test_category46_closed_complete_metrics_join as joined

    def not_child(*_):
        raise ChildProcessError

    def inaccessible(*_):
        raise PermissionError

    monkeypatch.setattr(joined.os, "waitpid", not_child)
    monkeypatch.setattr(joined.os, "kill", inaccessible)
    monkeypatch.setattr(
        joined.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=state)
    )
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(joined.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(joined.time, "sleep", lambda _: None)
    if exited:
        joined._wait_actual_exit(12345, seconds=1)
    else:
        with pytest.raises(pytest.fail.Exception, match="did not exit"):
            joined._wait_actual_exit(12345, seconds=1)


def test_malformed_assignment_journal_does_not_prove_empty_work(tmp_path, monkeypatch):
    c, m, p, route, now = world(tmp_path, monkeypatch)
    c.update(lambda doc: doc.update(hold_escalations={"broken": {}}))
    with pytest.raises(ValueError, match="malformed escalation journal"):
        escalation.preserve_assignment(p, "unrelated-controller", {"targets": []})
