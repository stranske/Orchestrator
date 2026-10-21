"""Re-entry changes one proven-unstarted scheduling boundary, never effect authority."""

import fcntl
import json

import pytest

import closer_finite_reentry as retry
import closer_pipeline as pipeline
import closer_resources as resources
from closer_frontier_policy import make_frontier

TARGET = "owner/repo#1"


def fixture(tmp_path, stage="prepare", status="held"):
    selected = make_frontier(
        [TARGET],
        owner="owner",
        frontier_id="frozen",
        bindings={TARGET: {"revision": "rev", "action_id": "a" * 64}},
    )
    root = tmp_path / "queue.pipeline.json.scoped"
    path = resources.shard_dir(root, TARGET) / "state.json"
    path.parent.mkdir(parents=True)
    row = {
        "status": status,
        "stage": stage,
        "stage_status": status,
        "stage_started_at": 100.0,
        "stage_deadline": 200.0,
        "work_key": "request",
        "revision": "rev",
        "identity": TARGET,
        "results": {},
    }
    if stage == "prepare":
        row["error"] = "ResourceBlocked: no io pool slot before the deadline"
    if stage == "interpret":
        row["results"] = {
            "prepare": {"revision": "rev", "packet_ref": {"sha256": "preserved"}},
            "interpret": {
                "classification": "assessment_transport_unavailable",
                "status": "pending",
            },
        }
    entry = {
        "schema": "closer-pipeline-frontier/v2",
        "finite_frontier": selected,
        "run_id": pipeline._digest(["finite-frontier/v2", "frozen", "owner"])[:32],
        "items": {TARGET: row},
    }
    doc = {
        "schema": pipeline.SCHEMA,
        "runs": {"historical": {"untouched": True}},
        "carry": {},
        "cache": {},
        "frontiers": {"frozen": entry},
    }
    path.write_text(json.dumps(doc))
    (path.parent / "lease.json").write_text(
        json.dumps({"target": TARGET, "owner": "terminal", "epoch": 1, "finite_frontier": selected})
    )
    return path, selected, doc


def read(path):
    return json.loads(path.read_text())["frontiers"]["frozen"]["items"][TARGET]


def apply(path, frontier, event="trigger-1"):
    plan = retry.plan(path, frontier=frontier, target=TARGET, event=event)
    result = retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event=event,
        expected_plan_sha256=plan["plan_sha256"],
    )
    return plan, result


def scope_proof(path, doc):
    entry = doc["frontiers"]["frozen"]
    row = entry["items"][TARGET]
    row["error"] = "ScopeBlocked: quarantined_resource:repo:owner/repo"
    path.write_text(json.dumps(doc))
    proof = {
        "schema": "closer-scoped-admission/v1",
        "target": TARGET,
        "stage": row["stage"],
        "binding": {
            "state_path": str(path),
            "pipeline_run_id": entry["run_id"],
            "revision": "rev",
            "identity": TARGET,
            "work_key": "request",
        },
        "admitted": False,
        "callback_started": False,
        "reason": "quarantined_resource:repo:owner/repo",
        "observed_at": 110.0,
        "input_sha256": retry._digest(row["results"]),
    }
    location = path.parent.parent.parent / "scope-admissions" / "round" / "proof.json"
    location.parent.mkdir(parents=True)
    location.write_text(json.dumps(proof))
    return location, proof


def test_pool_reentry_preserves_history_descriptor_and_repeated_event(tmp_path):
    path, frontier, before = fixture(tmp_path)
    proposal, result = apply(path, frontier)
    assert result["status"] == "applied"
    after = json.loads(path.read_text())
    assert (
        after["runs"] == before["runs"]
        and after["frontiers"]["frozen"]["finite_frontier"] == frontier
    )
    row = read(path)
    assert row["status"] == "held" and row["reentry_ready"] and row["stage"] == "prepare"
    assert row["reentry_history"][0]["previous"] == before["frontiers"]["frozen"]["items"][TARGET]
    assert retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="trigger-1",
        expected_plan_sha256=proposal["plan_sha256"],
    )["idempotent"]


def test_transport_qualification_and_progress_preservation(tmp_path, monkeypatch):
    import adapters

    path, frontier, before = fixture(tmp_path, "interpret")
    monkeypatch.setattr(adapters, "data_only_problem", lambda *_: "absent")
    with pytest.raises(ValueError, match="still_unqualified"):
        retry.plan(path, frontier=frontier, target=TARGET, event="transport-fixed")
    monkeypatch.setattr(adapters, "data_only_problem", lambda *_: None)
    monkeypatch.setattr(adapters, "_data_only_claude_binary", lambda: "/qualified/claude")
    apply(path, frontier, "transport-fixed")
    row = read(path)
    assert row["stage"] == "interpret" and row["status"] == "held" and row["reentry_ready"]
    assert row["results"] == {
        "prepare": before["frontiers"]["frozen"]["items"][TARGET]["results"]["prepare"]
    }


def test_unknown_scope_refusal_becomes_no_effect_hold(tmp_path):
    path, frontier, doc = fixture(tmp_path, "act", "unknown")
    location, _ = scope_proof(path, doc)
    apply(path, frontier, "terminal-pre-callback-proof")
    row = read(path)
    assert row["status"] == "held" and row["stage"] == "act"
    assert row["deferred_reason"] == "scope_quarantined"
    assert row["resource_evidence"]["confirmed_not_started"] is True
    assert row["resource_evidence"]["scope_refusal"]["path"] == str(location)


@pytest.mark.parametrize(
    "change", ["callback_started", "binding", "old_time", "input", "ambiguous"]
)
def test_scope_proof_must_be_exact_current_request(tmp_path, change):
    path, frontier, doc = fixture(tmp_path, "act", "unknown")
    location, proof = scope_proof(path, doc)
    if change == "callback_started":
        proof["callback_started"] = True
    if change == "binding":
        proof["binding"]["work_key"] = "earlier"
    if change == "old_time":
        proof["observed_at"] = 99
    if change == "input":
        proof["input_sha256"] = "altered"
    location.write_text(json.dumps(proof))
    if change == "ambiguous":
        (location.parent / "duplicate.json").write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="missing_or_ambiguous"):
        retry.plan(path, frontier=frontier, target=TARGET, event="event")


@pytest.mark.parametrize("where", ["items", "frontiers"])
def test_any_inner_operation_requires_real_reconciliation(tmp_path, where):
    path, frontier, doc = fixture(tmp_path, "act", "unknown")
    scope_proof(path, doc)
    inner = path.parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
    inner.parent.mkdir()
    inner.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "items": {},
                where: {"old": {"operation": {"status": "completed"}}},
            }
        )
    )
    with pytest.raises(ValueError, match="inner_operation"):
        retry.plan(path, frontier=frontier, target=TARGET, event="event")


@pytest.mark.parametrize("which", ["provider", "barrier", "actual_unknown", "paused"])
def test_uncertain_or_paused_rows_cannot_be_reopened(tmp_path, which):
    path, frontier, doc = fixture(tmp_path)
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    if which == "provider":
        row["provider_outcome_uncertain"] = {"run_id": "provider-started"}
    if which == "barrier":
        row["recovery_barrier"] = {"reason": "retained"}
    if which == "actual_unknown":
        row.update(status="unknown", stage="act", error="lost response")
    if which == "paused":
        row["status"] = "paused"
    path.write_text(json.dumps(doc))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        apply(path, frontier)
    assert path.read_bytes() == before


@pytest.mark.parametrize("which", ["row", "descriptor", "lease"])
def test_plan_cas_refuses_changed_inputs(tmp_path, which):
    path, frontier, doc = fixture(tmp_path)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="event")
    if which == "row":
        doc["frontiers"]["frozen"]["items"][TARGET]["extra"] = True
    if which == "descriptor":
        doc["frontiers"]["frozen"]["finite_frontier"]["owner"] = "new"
    if which == "lease":
        lease = path.parent / "lease.json"
        value = json.loads(lease.read_text())
        value["epoch"] = 2
        lease.write_text(json.dumps(value))
    else:
        path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        retry.apply(
            path,
            frontier=frontier,
            target=TARGET,
            event="event",
            expected_plan_sha256=proposal["plan_sha256"],
        )


@pytest.mark.parametrize("name", ["state.json.runlock", "lease.lock"])
def test_active_writer_is_never_adopted(tmp_path, name):
    path, frontier, _ = fixture(tmp_path)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="event")
    with (path.parent / name).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(resources.ResourceBlocked, match="active_finite_writer"):
            retry.apply(
                path,
                frontier=frontier,
                target=TARGET,
                event="event",
                expected_plan_sha256=proposal["plan_sha256"],
            )


def test_aliased_shard_is_rejected(tmp_path):
    path, frontier, _ = fixture(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="aliased_path"):
        retry.plan(link / "state.json", frontier=frontier, target=TARGET, event="event")


def test_plan_apply_public_cli_then_same_frontier_resumes_existing_stage(tmp_path, capsys):
    path, frontier, doc = fixture(tmp_path)
    frozen = tmp_path / "frontier.json"
    frozen.write_text(json.dumps(frontier))
    args = [
        "--state",
        str(path),
        "--frontier",
        str(frozen),
        "--target",
        TARGET,
        "--event",
        "pool-now-free",
    ]
    assert retry.main(args) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert retry.main(args + ["--apply", "--expected-plan-sha256", proposal["plan_sha256"]]) == 0
    seen = []
    lease = resources.acquire(
        path.parent.parent.parent, TARGET, owner="resuming", finite_frontier=frontier
    )
    try:
        with resources.fenced(lease):
            receipt = pipeline._run_pipeline_finite(
                path,
                [TARGET],
                frontier_target=TARGET,
                finite_frontier=frontier,
                prepare=lambda t, c: (
                    seen.append(("prepare", c["finite_frontier"])) or {"revision": "rev"}
                ),
                interpret=lambda *_: {"substantive_needs": []},
                exception=lambda *_: {"route": "normal"},
                act=lambda *_: {"status": "completed", "target_complete": True},
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            )
    finally:
        lease.release()

    assert seen == [("prepare", frontier)]
    assert receipt["targets"][TARGET]["status"] == "completed"
    assert json.loads(path.read_text())["runs"] == doc["runs"]


def test_audit_recovers_crash_before_state_replace(tmp_path, monkeypatch):
    path, frontier, _ = fixture(tmp_path)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="event")
    real_save = pipeline._save_snapshot
    monkeypatch.setattr(
        pipeline, "_save_snapshot", lambda *_: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        retry.apply(
            path,
            frontier=frontier,
            target=TARGET,
            event="event",
            expected_plan_sha256=proposal["plan_sha256"],
        )
    assert read(path)["status"] == "held"
    monkeypatch.setattr(pipeline, "_save_snapshot", real_save)
    result = retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="event",
        expected_plan_sha256=proposal["plan_sha256"],
    )
    assert result["idempotent"] and read(path)["status"] == "held" and read(path)["reentry_ready"]


def test_unchanged_boundary_cannot_be_rearmed_with_new_token(tmp_path):
    path, frontier, _ = fixture(tmp_path)
    apply(path, frontier, "first")
    doc = json.loads(path.read_text())
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    row.update(
        status="held",
        stage_status="held",
        error="ResourceBlocked: no io pool slot before the deadline",
    )
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="unchanged_boundary_already_retried"):
        retry.plan(path, frontier=frontier, target=TARGET, event="different-token-same-failure")


def test_inner_submitted_outcome_without_operation_is_not_no_effect(tmp_path):
    path, frontier, _ = fixture(tmp_path)
    inner = path.parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
    inner.parent.mkdir()
    inner.write_text(
        json.dumps({"schema": "closer-queue/v1", "items": {TARGET: {"status": "submitted"}}})
    )
    with pytest.raises(ValueError, match="inner_outcome"):
        retry.plan(path, frontier=frontier, target=TARGET, event="event")


def test_scope_refusal_reference_survives_finite_exception(tmp_path):
    path, frontier, doc = fixture(tmp_path, "act", "pending")
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    row["stage_status"] = "queued"
    path.write_text(json.dumps(doc))
    proof = {"path": "/retained/proof.json", "sha256": "exact-proof"}

    def refuse(*_):
        raise resources.ScopeBlocked("quarantined_resource:repo:owner/repo", proof)

    result = pipeline._run_pipeline_finite(
        path,
        [TARGET],
        frontier_target=TARGET,
        finite_frontier=frontier,
        prepare=lambda *_: {},
        interpret=lambda *_: {},
        exception=lambda *_: {},
        act=refuse,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
    )
    assert result["targets"][TARGET]["status"] == "unknown"
    assert read(path)["scope_refusal"] == proof


def test_reentry_never_retires_foreign_resource_reservations(tmp_path):
    path, frontier, _ = fixture(tmp_path)
    root = path.parent.parent.parent
    foreign = root / "reservations" / "historical" / "intent.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"owner":"historical-owner","status":"unknown"}')
    before = foreign.read_bytes()
    apply(path, frontier)
    assert foreign.read_bytes() == before


def test_scoped_target_worker_ceiling_is_separate_from_lane_capacity(tmp_path, monkeypatch):
    import threading
    import time

    targets = ["owner/repo#1", "owner/repo#2", "owner/repo#3"]
    selected = make_frontier(
        targets,
        owner="owner",
        frontier_id="many",
        bindings={t: {"revision": "r", "action_id": "a" * 64} for t in targets},
    )
    active = [0, 0]
    guard = threading.Lock()

    def run(*_args, **kwargs):
        with guard:
            active[0] += 1
            active[1] = max(active)
        time.sleep(0.01)
        with guard:
            active[0] -= 1
        return {"targets": {kwargs["frontier_target"]: {"status": "completed"}}}

    # Unit test of scheduling only; production registration is tested separately.
    monkeypatch.setattr(pipeline, "_require_dispatcher_finite_callbacks", lambda *_: None)
    monkeypatch.setattr(pipeline, "_run_pipeline_finite", run)
    resources.run_scoped(
        tmp_path / "pipeline.json",
        targets,
        finite_frontier=selected,
        request_timeout_seconds=10,
        prepare=lambda *_: {},
        interpret=lambda *_: {},
        exception=lambda *_: {},
        act=lambda *_: {},
        capacity_fn=lambda: {"io": 4, "low": 2, "exception": 1},
        max_target_workers=1,
        max_io_workers=4,
        max_low_workers=2,
        max_exception_workers=1,
    )
    assert active[1] == 1


def test_ready_hold_requires_live_lease_and_exact_audit(tmp_path):
    path, frontier, _ = fixture(tmp_path)
    apply(path, frontier)
    row = read(path)
    with pytest.raises(ValueError, match="live_production_lease"):
        retry.ready(path, frontier, TARGET, row)
    lease = resources.acquire(
        path.parent.parent.parent, TARGET, owner="real-resume", finite_frontier=frontier
    )
    try:
        with resources.fenced(lease):
            assert retry.ready(path, frontier, TARGET, row)
            changed = dict(row, unexpected_change=True)
            with pytest.raises(ValueError, match="does_not_bind"):
                retry.ready(path, frontier, TARGET, changed)
    finally:
        lease.release()


def test_missing_ready_audit_never_reopens_hold(tmp_path):
    path, frontier, _ = fixture(tmp_path)
    _, result = apply(path, frontier)
    __import__("pathlib").Path(result["audit"]).unlink()
    lease = resources.acquire(
        path.parent.parent.parent, TARGET, owner="real-resume", finite_frontier=frontier
    )
    try:
        with resources.fenced(lease):
            with pytest.raises(OSError):
                retry.ready(path, frontier, TARGET, read(path))
    finally:
        lease.release()
    assert read(path)["status"] == "held"


def test_audit_recovers_crash_after_state_replace(tmp_path, monkeypatch):
    path, frontier, _ = fixture(tmp_path)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="event")
    real_atomic = resources._atomic_write
    count = [0]

    def crash_second(path, doc):
        count[0] += 1
        if count[0] == 2:
            raise OSError("crash after state committed")
        return real_atomic(path, doc)

    monkeypatch.setattr(resources, "_atomic_write", crash_second)
    with pytest.raises(OSError):
        retry.apply(
            path,
            frontier=frontier,
            target=TARGET,
            event="event",
            expected_plan_sha256=proposal["plan_sha256"],
        )
    assert read(path)["reentry_ready"]
    monkeypatch.setattr(resources, "_atomic_write", real_atomic)
    result = retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="event",
        expected_plan_sha256=proposal["plan_sha256"],
    )
    assert result["idempotent"]


@pytest.mark.parametrize(
    "mutation",
    ["root", "frontiers", "items", "results", "status", "stage", "error", "history", "deep"],
)
def test_public_cli_holds_malformed_records_without_traceback(tmp_path, capsys, mutation):
    import closer_dispatcher

    path, frontier, doc = fixture(tmp_path)
    frozen = tmp_path / "frontier.json"
    frozen.write_text(json.dumps(frontier))
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    if mutation == "root":
        doc = []
    if mutation == "frontiers":
        doc["frontiers"] = []
    if mutation == "items":
        doc["frontiers"]["frozen"]["items"] = []
    if mutation == "results":
        row["results"] = []
    if mutation == "status":
        row["status"] = {}
    if mutation == "stage":
        row["stage"] = []
    if mutation == "error":
        row["error"] = {}
    if mutation == "history":
        row["reentry_history"] = ["not-an-entry"]
    if mutation == "deep":
        path.write_text('{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}")
    else:
        path.write_text(json.dumps(doc))
    before = path.read_bytes()
    assert (
        closer_dispatcher.main(
            [
                "finite-reentry",
                "--state",
                str(path),
                "--frontier",
                str(frozen),
                "--target",
                TARGET,
                "--event",
                "observed-recovery",
            ]
        )
        == 78
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "held" and result["reason"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", [[], {}])
def test_public_cli_holds_malformed_inner_status(tmp_path, capsys, status):
    path, frontier, _ = fixture(tmp_path)
    frozen = tmp_path / "frontier.json"
    frozen.write_text(json.dumps(frontier))
    inner = path.parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
    inner.parent.mkdir()
    inner.write_text(
        json.dumps({"schema": "closer-queue/v1", "items": {TARGET: {"status": status}}})
    )
    assert (
        retry.main(
            [
                "--state",
                str(path),
                "--frontier",
                str(frozen),
                "--target",
                TARGET,
                "--event",
                "event",
            ]
        )
        == 78
    )
    assert json.loads(capsys.readouterr().out)["reason"] == "malformed_inner_status"
