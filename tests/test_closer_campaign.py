from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import install_closer_campaign_hook as hook

import closer_campaign as cc
from closer_release import ReleaseRecord

DIGEST = "c" * 64
CAND = "a" * 40


def test_fresh_creation_refuses_existing_identical_campaign_without_writing(tmp_path):
    campaign = cc.Campaign(tmp_path / "fresh.campaign.json")
    campaign.begin("fresh", ["target"], owner="owner", release_pin=DIGEST)
    before = campaign.path.read_bytes()
    with pytest.raises(cc.CampaignError, match="destination already exists"):
        campaign.begin("fresh", ["target"], owner="owner", release_pin=DIGEST, _require_absent=True)
    assert campaign.path.read_bytes() == before


@pytest.fixture(autouse=True)
def handoff(tmp_path, monkeypatch):
    monkeypatch.setattr("closer_runtime_binding.policy_problem", lambda policy: None)
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "0")


def make(tmp_path, targets=("t1", "t2", "t3"), cap=2):
    c = cc.Campaign(tmp_path / "campaign.json")
    c.begin(
        "camp",
        list(targets),
        owner="runner-a",
        release_pin=DIGEST,
        max_workers=cap,
        worker_config={"granularity": "target-delegate"},
    )
    return c


def cmd(target):
    return ["worker", target]


SOURCE_INIT_TARGET = "source/repo#984"
SOURCE_INIT_NOW = 1_792_000_000.0


def source_initialization():
    return {
        "hold": {
            "kind": "source_initialization",
            "reason": "qualified demand pending",
            "bootstrap_anchor_sha256": "d" * 64,
        },
        "correction_controller": {"config": {"state_path": "/fixed/new.campaign.json"}},
    }


def source_initial_demand(*, revision=1):
    assignment = {
        "generation": 1,
        "generated_at": datetime.fromtimestamp(SOURCE_INIT_NOW, timezone.utc).isoformat(),
        "errors": [],
        "targets": [{"target": SOURCE_INIT_TARGET, "due": SOURCE_INIT_NOW - 1}],
    }
    raw = json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode()
    package = {
        "package_digest": "a" * 64,
        "manifest_sha256": "b" * 64,
        "qualification_digest": "c" * 64,
    }
    request = {
        "schema": "closer-demand-request/v1",
        "campaign_id": "source-initialized",
        "owner": "source-owner",
        "campaign_generation": 1,
        "expected_revision": revision,
        "assignment_generation": 1,
        "assignment_sha256": hashlib.sha256(raw).hexdigest(),
        **package,
        "not_before": SOURCE_INIT_NOW - 1,
        "deadline": SOURCE_INIT_NOW + 120,
        "targets": [SOURCE_INIT_TARGET],
    }
    return {
        "demand_assignment_bytes": raw,
        "demand_request": request,
        "qualified_package": package,
        "demand_now": SOURCE_INIT_NOW,
    }


def begin_source_initialized(c, **kwargs):
    initialization = kwargs.pop("_source_initialization", source_initialization())
    return c.begin(
        "source-initialized",
        [SOURCE_INIT_TARGET],
        owner="source-owner",
        release_pin="a" * 64,
        worker_config={"granularity": "round"},
        _source_initialization=initialization,
        **kwargs,
    )


def test_source_initialization_creates_single_held_nonrunnable_record(tmp_path):
    campaign = cc.Campaign(tmp_path / "source-initialized.campaign.json")
    writes = []
    real_write = campaign._write_unlocked
    campaign._write_unlocked = lambda doc: (writes.append(dict(doc)), real_write(doc))

    doc = begin_source_initialized(campaign)

    assert len(writes) == 1
    assert doc["revision"] == 1 and doc["generation"] == 1
    assert doc["hold"] == source_initialization()["hold"]
    assert doc["correction_controller"] == source_initialization()["correction_controller"]
    assert doc["workers"] == []
    assert not any(key in doc for key in ("demand_admission", "demand_assignment", "demand_wake"))


def test_source_initialization_refused_demand_preserves_held_bytes(tmp_path):
    campaign = cc.Campaign(tmp_path / "source-initialized.campaign.json")
    begin_source_initialized(campaign)
    before = campaign.path.read_bytes()
    invalid = source_initial_demand()
    invalid["demand_request"]["assignment_sha256"] = "0" * 64

    with pytest.raises(cc.CampaignError, match="completed demand refused"):
        begin_source_initialized(campaign, **invalid)

    assert campaign.path.read_bytes() == before
    assert campaign.read()["hold"] == source_initialization()["hold"]


def test_source_initialization_admits_typed_snapshot_while_clearing_hold_in_one_write(tmp_path):
    campaign = cc.Campaign(tmp_path / "source-initialized.campaign.json")
    begin_source_initialized(campaign)
    writes = []
    real_write = campaign._write_unlocked
    campaign._write_unlocked = lambda doc: (writes.append(dict(doc)), real_write(doc))

    doc = begin_source_initialized(campaign, **source_initial_demand())

    assert len(writes) == 1
    assert doc["revision"] == 2 and doc["hold"] is None
    assert doc["demand_admission"]["schema"] == "closer-demand-admission/v1"
    assert doc["worker_config"]["assignment"] == doc["demand_assignment"]["path"]
    assert Path(doc["demand_assignment"]["path"]).is_file()
    assert writes[0]["hold"] is None and "demand_admission" in writes[0]


@pytest.mark.parametrize("mutation", ["different_initialization", "owned_followup"])
def test_source_initialization_mismatch_or_mutation_stays_held(tmp_path, mutation):
    campaign = cc.Campaign(tmp_path / "source-initialized.campaign.json")
    begin_source_initialized(campaign)
    if mutation == "owned_followup":
        campaign.update(lambda doc: doc.__setitem__("hold_followups", {}))
    before = campaign.path.read_bytes()
    values = source_initial_demand(revision=campaign.read()["revision"])
    if mutation == "different_initialization":
        changed = source_initialization()
        changed["hold"]["reason"] = "different"
        values["_source_initialization"] = changed

    with pytest.raises(cc.CampaignError, match="source initialization"):
        begin_source_initialized(campaign, **values)

    assert campaign.path.read_bytes() == before
    assert campaign.read()["hold"]["kind"] == "source_initialization"


def test_ordinary_begin_cannot_clear_source_initialization_hold(tmp_path):
    campaign = cc.Campaign(tmp_path / "source-initialized.campaign.json")
    begin_source_initialized(campaign)
    before = campaign.path.read_bytes()
    values = source_initial_demand()

    with pytest.raises(cc.CampaignError, match="completed demand refused"):
        campaign.begin(
            "source-initialized",
            [SOURCE_INIT_TARGET],
            owner="source-owner",
            release_pin="a" * 64,
            worker_config={"granularity": "round"},
            **values,
        )

    assert campaign.path.read_bytes() == before
    assert campaign.read()["hold"] == source_initialization()["hold"]


def test_malformed_source_initialization_never_creates_or_overwrites_campaign(tmp_path):
    absent = cc.Campaign(tmp_path / "absent.campaign.json")
    malformed = {"hold": source_initialization()["hold"]}

    with pytest.raises(ValueError, match="source initialization"):
        begin_source_initialized(absent, _source_initialization=malformed)

    assert not absent.path.exists()
    existing = cc.Campaign(tmp_path / "existing.campaign.json")
    begin_source_initialized(existing)
    before = existing.path.read_bytes()
    with pytest.raises(ValueError, match="source initialization"):
        begin_source_initialized(existing, _source_initialization=malformed)

    assert existing.path.read_bytes() == before


def test_source_initialization_rejects_first_write_mixed_with_typed_demand(tmp_path):
    campaign = cc.Campaign(tmp_path / "source-initialized.campaign.json")

    with pytest.raises(cc.CampaignError, match="must first publish a held campaign"):
        begin_source_initialized(campaign, **source_initial_demand())

    assert not campaign.path.exists()


def test_ordinary_resume_rejects_source_initialization_without_writing(tmp_path):
    campaign = cc.Campaign(tmp_path / "ordinary.campaign.json")
    campaign.begin(
        "ordinary",
        [SOURCE_INIT_TARGET],
        owner="source-owner",
        release_pin="a" * 64,
        worker_config={"granularity": "round"},
    )
    before = campaign.path.read_bytes()

    with pytest.raises(cc.CampaignError, match="source initialization is creation-only"):
        campaign.begin(
            "ordinary",
            [SOURCE_INIT_TARGET],
            owner="source-owner",
            release_pin="a" * 64,
            worker_config={"granularity": "round"},
            _source_initialization=source_initialization(),
        )

    assert campaign.path.read_bytes() == before


def launch_fn(pids):
    def launcher(command, log):
        pids.append(len(pids) + 1000)
        return pids[-1]

    return launcher


def test_cap_bounds_launches_and_wait_names_cap(tmp_path):
    c, pids = make(tmp_path), []
    for _ in range(2):
        assert (
            cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)[
                "kind"
            ]
            == "launched"
        )
    third = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)
    assert third == {"kind": "wait", "reason": "worker_cap", "active": 2, "cap": 2}
    assert len(pids) == 2


def test_dead_worker_without_result_is_unknown_and_never_replaced(tmp_path):
    c, pids = make(tmp_path, ("t1",)), []
    cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)
    action = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: False)
    assert action["kind"] == "reconcile"
    assert len(pids) == 1 and c.read()["targets"]["t1"] == "reconcile"
    # a fresh scheduler process reaches the same durable answer
    again = cc.step(
        cc.Campaign(c.path), worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: False
    )
    assert again["kind"] == "reconcile" and len(pids) == 1


def test_result_file_makes_worker_terminal_and_frees_slot(tmp_path):
    c, pids = make(tmp_path, ("t1", "t2"), cap=1), []
    first = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)
    r = cc.result_path(c.path, first["worker_id"])
    r.parent.mkdir(parents=True)
    r.write_text(json.dumps({"outcome": "ok"}))
    nxt = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: False)
    assert nxt["kind"] == "launched" and nxt["target"] == "t2"
    assert c.read()["targets"]["t1"] == "done"


def test_denial_holds_with_exact_reason_and_never_falls_back(tmp_path):
    c, pids = make(tmp_path), []
    first = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)
    r = cc.result_path(c.path, first["worker_id"])
    r.parent.mkdir(parents=True)
    r.write_text(json.dumps({"outcome": "denied", "detail": "policy: all-repo Maint82 denied"}))
    nxt = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: False)
    assert nxt["kind"] == "held" and nxt["reason"]["reason"] == "policy: all-repo Maint82 denied"
    assert len(pids) == 1


def test_launch_permission_error_holds_without_retry(tmp_path):
    c = make(tmp_path)

    def denied(command, log):
        raise PermissionError("Operation not permitted")

    out = cc.step(c, worker_command=cmd, launcher=denied)
    assert out["kind"] == "held" and out["reason"] == "Operation not permitted"
    assert c.read()["hold"]["reason"] == "Operation not permitted"


def test_capacity_exhaustion_leaves_durable_pending_action(tmp_path):
    c, pids = make(tmp_path), []
    out = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), capacity_ok=False)
    assert out["reason"] == "capacity_exhausted" and pids == []
    later = cc.step(
        cc.Campaign(c.path), worker_command=cmd, launcher=launch_fn(pids), capacity_ok=True
    )
    assert later["kind"] == "launched"


def test_finite_frontier_is_bound_to_campaign_targets_and_resume(tmp_path):
    from closer_frontier_policy import make_frontier

    c = cc.Campaign(tmp_path / "finite.json")
    targets = ["acme/one#1", "acme/two#2"]
    frontier = make_frontier(
        targets,
        owner="runner-a",
        frontier_id="frontier-1",
        bindings={target: {"revision": "rev-1", "action_id": "a" * 64} for target in targets},
    )
    created = c.begin(
        "finite-camp",
        targets,
        owner="runner-a",
        release_pin=DIGEST,
        worker_config={"granularity": "round", "finite_frontier": frontier},
    )
    assert created["worker_config"]["finite_frontier"] == frontier
    assert (
        c.begin(
            "finite-camp",
            targets,
            owner="runner-a",
            release_pin=DIGEST,
            worker_config={"granularity": "round", "finite_frontier": frontier},
        )
        == created
    )
    assert "closer_frontier_policy.py" in cc.module_closure(
        Path(cc.__file__).resolve().parent, cc.PINNED_MODULES
    )

    with pytest.raises(ValueError, match="does not match campaign targets"):
        c.begin(
            "different",
            ["acme/one#1"],
            owner="runner-a",
            release_pin=DIGEST,
            worker_config={"granularity": "round", "finite_frontier": frontier},
        )
    with pytest.raises(cc.CampaignError, match="immutable"):
        c.begin(
            "finite-camp",
            targets,
            owner="runner-a",
            release_pin=DIGEST,
            worker_config={"granularity": "round"},
        )


def test_finite_frontier_campaign_refuses_corrupted_target_snapshot(tmp_path):
    from closer_frontier_policy import make_frontier

    c = cc.Campaign(tmp_path / "finite-corrupt.json")
    targets = ["acme/one#1"]
    frontier = make_frontier(
        targets,
        owner="runner-a",
        frontier_id="frontier-1",
        bindings={target: {"revision": "rev-1", "action_id": "a" * 64} for target in targets},
    )
    c.begin(
        "finite-camp",
        targets,
        owner="runner-a",
        release_pin=DIGEST,
        worker_config={"granularity": "round", "finite_frontier": frontier},
    )
    c.update(lambda doc: doc.update(assignment_targets=["acme/two#2"]))
    with pytest.raises(cc.CampaignError, match="finite frontier record is invalid"):
        c.read()


def test_finite_frontier_campaign_dispatch_uses_exact_cli_flags(tmp_path):
    import closer_dispatcher
    import closer_pipeline
    from closer_frontier_policy import make_frontier

    target = "acme/one#1"
    frontier = make_frontier(
        [target],
        owner="runner-a",
        frontier_id="dispatch-frontier",
        bindings={target: {"revision": "rev-1", "action_id": "a" * 64}},
    )
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": target}]}))
    command = cc.dispatch_command(
        {
            "assignment": str(assignment),
            "dispatch_state": str(tmp_path / "dispatch.json"),
            "dispatch_workers": 2,
            "coordinator_mode": "resource_scoped",
            "coordinator_contract_pin": closer_pipeline.scoped_capability(),
            "finite_frontier": frontier,
            "execution_policy": "finite-frontier/v2",
            "request_timeout_seconds": 30,
            "apply": True,
            "runtime_manifest": str(tmp_path / "runtime.json"),
        },
        "/offline/python",
        tmp_path,
    )
    assert "--budget-seconds" not in command
    assert command[command.index("--finite-frontier-json") + 1] == json.dumps(
        frontier, sort_keys=True, separators=(",", ":")
    )
    assert command[command.index("--request-timeout-seconds") + 1] == "30"

    from contextlib import contextmanager

    import closer_resources

    @contextmanager
    def no_effect_admission(*_args, **_kwargs):
        yield object()

    captured = {}
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(closer_resources, "coordination_admission", no_effect_admission)
        monkeypatch.setattr(
            closer_dispatcher,
            "_main_admitted",
            lambda args, _parser, _rows, _targets: captured.update(vars(args)) or 0,
        )
        assert closer_dispatcher.main(command[2:]) == 0
    finally:
        monkeypatch.undo()
    assert captured["finite_frontier"] == frontier
    assert captured["request_timeout_seconds"] == 30
    assert captured["budget_seconds"] is None
    assert captured["coordinator_mode"] == "resource_scoped"
    assert "--domain-act-timeout-seconds" not in command
    domain_command = cc.dispatch_command(
        {
            "assignment": str(assignment),
            "dispatch_state": str(tmp_path / "domain-dispatch.json"),
            "dispatch_workers": 2,
            "coordinator_mode": "resource_scoped",
            "coordinator_contract_pin": closer_pipeline.scoped_capability(),
            "finite_frontier": frontier,
            "execution_policy": "finite-frontier/v2",
            "request_timeout_seconds": 120,
            "domain_act_timeout_seconds": 600,
            "apply": True,
            "runtime_manifest": str(tmp_path / "runtime.json"),
        },
        "/offline/python",
        tmp_path,
    )
    assert domain_command[domain_command.index("--domain-act-timeout-seconds") + 1] == "600"


def test_finite_campaign_does_not_relaunch_unavailable_worker(tmp_path):
    from closer_frontier_policy import make_frontier

    target = "acme/one#1"
    frontier = make_frontier(
        [target],
        owner="runner-a",
        frontier_id="single-attempt",
        bindings={target: {"revision": "rev-1", "action_id": "a" * 64}},
    )
    c = cc.Campaign(tmp_path / "finite-no-retry.json")
    c.begin(
        "finite-no-retry",
        [target],
        owner="runner-a",
        release_pin=DIGEST,
        worker_config={
            "granularity": "round",
            "finite_frontier": frontier,
            "execution_policy": "finite-frontier/v2",
        },
    )
    worker_id = "worker-finite-unavailable"
    c.update(
        lambda doc: doc["workers"].append(
            {
                "worker_id": worker_id,
                "target": cc.ROUND_UNIT,
                "claim_target": "finite-no-retry:__round__",
                "pid": 12345,
                "state": "active",
            }
        )
    )
    result = cc.result_path(c.path, worker_id)
    result.parent.mkdir(parents=True)
    cc.write_result(result, worker_id, cc.ROUND_UNIT, "unavailable", "capacity was absent")
    doc = cc.reconcile(c, pid_alive=lambda _pid: True, release_claim=lambda *_: True)
    assert doc["targets"][cc.ROUND_UNIT] == "reconcile"
    assert cc.next_action(doc)["kind"] == "reconcile"


def test_claim_held_elsewhere_blocks_launch(tmp_path):
    c, pids = make(tmp_path, ("t1",)), []
    out = cc.step(c, worker_command=cmd, launcher=launch_fn(pids), claim=lambda t, a: False)
    assert out["reason"] == "target_claimed_elsewhere" and pids == []


def test_local_only_port_never_mutates_remote():
    assert cc.LocalOnlyPort().submit("op", {}) == "unavailable"


def test_status_prints_drainable_and_drained_state(tmp_path):
    c, pids = make(tmp_path, ("t1",)), []
    cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)
    assert "workers=1/2 drainable=1" in cc.status_line(c.read())
    w = c.read()["workers"][0]
    r = cc.result_path(c.path, w["worker_id"])
    r.parent.mkdir(parents=True)
    r.write_text(json.dumps({"outcome": "ok"}))
    doc = cc.reconcile(c, pid_alive=lambda p: False)
    assert "workers=0/2 drainable=0" in cc.status_line(doc)
    assert cc.next_action(doc) == {"kind": "complete"}


def test_newer_state_schema_stops_runner_before_write(tmp_path):
    p = tmp_path / "campaign.json"
    p.write_text(json.dumps({"schema_version": 2, "campaign_id": "x"}))
    before = p.read_bytes()
    with pytest.raises(cc.IncompatibleState):
        cc.Campaign(p).update(lambda d: d.update(phase="x"))
    assert p.read_bytes() == before


def test_begin_refuses_other_owner_or_pin(tmp_path):
    c = make(tmp_path)
    with pytest.raises(cc.CampaignError):
        c.begin("camp", ["t1"], owner="runner-b", release_pin=DIGEST)
    with pytest.raises(cc.CampaignError):
        c.begin("camp", ["t1"], owner="runner-a", release_pin="d" * 64)


# ---- release plan: #794 shape


def journal(tmp_path, stage_steps):
    rec = ReleaseRecord(tmp_path / "release.json")
    s = rec.begin(CAND, "generation-v1:" + "b" * 64, "closer")
    s = rec.transition("verifying", lane_owner="closer", expected_revision=s["revision"])
    s = rec.record_verification(
        lane_owner="closer",
        expected_revision=s["revision"],
        verified=True,
        source_identity="e" * 64,
        snapshot_path=tmp_path / "snap",
        snapshot_digest=DIGEST,
        log_path=tmp_path / "v.log",
        exit_code=0,
    )
    for stage, fields in stage_steps:
        s = rec.transition(stage, lane_owner="closer", expected_revision=s["revision"], **fields)
    return rec


def test_acceptance_pending_plan_never_reverifies_or_reinstalls(tmp_path):
    gen = "generation-v1:" + "d" * 64
    rec = journal(
        tmp_path,
        [
            ("installing", {}),
            ("installed", {"installed_generation": gen}),
            ("acceptance_pending", {}),
        ],
    )
    plan = cc.release_plan(rec.read())
    assert plan["action"] == "await_acceptance" and not plan["reverify"] and not plan["reinstall"]
    assert plan["revision"] == 6 and plan["sealed_receipt"] is True


def test_verified_stage_resumes_install_from_receipt_without_reverify(tmp_path):
    plan = cc.release_plan(journal(tmp_path, []).read())
    assert plan["action"] == "resume_install" and plan["reverify"] is False


def test_prepared_stage_is_the_only_one_that_plans_verification(tmp_path):
    rec = ReleaseRecord(tmp_path / "r.json")
    rec.begin(CAND, "generation-v1:" + "b" * 64, "closer")
    assert cc.release_plan(rec.read())["reverify"] is True
    assert cc.release_plan(None)["action"] == "none"


def test_release_resume_action_is_next_action_and_outranks_launch(tmp_path):
    c = make(tmp_path)
    rec = journal(tmp_path, [])
    assert cc.next_action(c.read(), release=rec.read())["kind"] == "release_resume"


# ---- promotion gating


def manifest():
    return {
        "schema_version": 1,
        "contract": cc.CONTRACT,
        "candidate_sha": CAND,
        "package_digest": DIGEST,
        "state_versions": {"campaign": 1, "release": 1},
    }


def receipt(runner="desktop-1", **over):
    r = {
        "kind": "desktop-runner",
        "runner_id": runner,
        "package_digest": DIGEST,
        "state_versions": {"campaign": 1, "release": 1},
        "phases": {p: {"passed": True, "evidence": f"ref:{p}"} for p in cc.RUNNER_PHASES},
    }
    r.update(over)
    return r


def test_promotion_requires_every_enabled_runner_receipt():
    ok = cc.check_promotion(manifest(), ["desktop-1"], [receipt()])
    assert not ok["promotable"]  # shape-only desktop claims are never qualification
    assert "desktop observer unsupported" in str(ok)
    missing = cc.check_promotion(manifest(), ["desktop-1", "desktop-2"], [receipt()])
    assert not missing["promotable"] and "desktop-2: no runner receipt" in missing["problems"]
    assert not cc.check_promotion(manifest(), ["desktop-1"], [])["promotable"]


@pytest.mark.parametrize(
    "over",
    [
        {"kind": "unit_test"},
        {"package_digest": "f" * 64},
        {"state_versions": {"campaign": 2, "release": 1}},
    ],
)
def test_promotion_rejects_unproven_or_mismatched_receipt(over):
    assert not cc.check_promotion(manifest(), ["desktop-1"], [receipt(**over)])["promotable"]


def test_promotion_requires_each_phase_with_evidence():
    r = receipt()
    r["phases"]["crash_restart"] = {"passed": True}
    out = cc.check_promotion(manifest(), ["desktop-1"], [r])
    assert any("crash_restart" in p for p in out["problems"])


def test_partial_promotion_keeps_one_owner_and_rollback_needs_compatible_state(tmp_path):
    c, pids = make(tmp_path), []
    cc.step(c, worker_command=cmd, launcher=launch_fn(pids), pid_alive=lambda p: True)
    doc = c.read()
    assert cc.promotion_owner(doc, DIGEST)["may_write"]
    assert not cc.promotion_owner(doc, "e" * 64)["may_write"]
    prev = dict(manifest(), package_digest="e" * 64)
    assert not cc.rollback_decision(prev, {"campaign": 1, "release": 1}, doc)[
        "allowed"
    ]  # live workers
    assert not cc.rollback_decision(prev, {"campaign": 2, "release": 1}, None)["allowed"]
    assert cc.rollback_decision(prev, {"campaign": 1, "release": 1}, None)["allowed"]


# ---- CLI / controller hook


def test_cli_status_is_read_only_and_reports_release(tmp_path, capsys):
    c = make(tmp_path)
    before = c.path.read_bytes()
    rec = journal(tmp_path, [])
    assert cc.main(["status", "--state", str(c.path), "--release-record", str(rec.path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["next_action"]["kind"] == "release_resume" and out["release"]["reverify"] is False
    assert c.path.read_bytes() == before


def test_cli_check_promotion_exit_code(tmp_path):
    m, r = tmp_path / "m.json", tmp_path / "r.json"
    m.write_text(json.dumps(manifest()))
    r.write_text(json.dumps([]))
    assert (
        cc.main(["check-promotion", "--manifest", str(m), "--receipts", str(r), "--runner", "d"])
        == 78
    )


def test_controller_entry_routes_only_campaign_subcommand(capsys):
    seen = []
    assert cc.controller_entry(["other", "x"], lambda a: seen.append(a) or 7) == 7
    assert seen == [["other", "x"]]
    assert (
        cc.controller_entry(
            ["closer-campaign", "status", "--state", "/nonexistent/x.json"], lambda a: 7
        )
        == 2
    )  # ours: no record -> exit 2, fallback untouched


def test_hook_installer_patches_exact_shape_only(tmp_path):
    src = (
        "def main(argv: list[str] | None = None) -> int:\n"
        "    import demand_router\n    return demand_router.main(argv)\n"
    )
    out = hook.render(src, "/m.json")
    assert "campaign.controller_entry(argv, demand_router.main)" in out
    assert "import closer_campaign\n" not in out  # never a plain import
    assert hook.render(out, "/m.json") == out
    with pytest.raises(SystemExit):
        hook.render("def main():\n    pass\n", "/m.json")
    with pytest.raises(SystemExit):
        hook.render(src + src, "/m.json")


def test_begin_checks_and_creates_before_another_owner_can_interleave(tmp_path, monkeypatch):
    import selectors
    import subprocess

    campaign = cc.Campaign(tmp_path / "new.campaign.json")
    original_lock, original_write = campaign._locked, campaign._write_unlocked
    used = {"attempt": False, "release": False, "write": False}
    peer_result = []
    child_code = """
import fcntl, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import closer_campaign as cc
path = Path(sys.argv[1])
with path.with_name(path.name + '.lock').open('a+b') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('ATTEMPT_BLOCKED', flush=True)
    else:
        print('ATTEMPT_FREE', flush=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
try:
    result = cc.Campaign(path).begin('camp', ['target'], owner='other', release_pin=sys.argv[2])
    print(json.dumps({'status': 'created', 'owner': result['owner']}), flush=True)
except cc.CampaignError as exc:
    print(json.dumps({'status': 'refused', 'reason': str(exc)}), flush=True)
"""
    peer = None

    class FirstRelease:
        def __init__(self, stream):
            self.stream = stream

        def close(self):
            self.stream.close()
            used["release"] = True
            output, error = peer.communicate(timeout=10)
            assert peer.returncode == 0, error
            peer_result.append(json.loads(output))

    def _stdout_readable(stream, timeout: float) -> bool:
        """Wait for pipe readiness without select's platform descriptor ceiling."""
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            return bool(selector.select(timeout))

    def first_lock():
        nonlocal peer
        stream = original_lock()
        if peer is not None:
            return stream
        peer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(campaign.path),
                DIGEST,
                str(Path(cc.__file__).parent),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert _stdout_readable(peer.stdout, 10), "competitor never attempted ownership"
        assert peer.stdout.readline().strip() == "ATTEMPT_BLOCKED"
        used["attempt"] = True
        return FirstRelease(stream)

    def observed_write(doc):
        used["write"] = True
        assert used["attempt"]
        original_write(doc)

    monkeypatch.setattr(campaign, "_locked", first_lock)
    monkeypatch.setattr(campaign, "_write_unlocked", observed_write)
    try:
        created = campaign.begin("camp", ["target"], owner="first", release_pin=DIGEST)
        assert used == {"attempt": True, "release": True, "write": True}
        assert peer_result[0]["status"] == "refused"
        assert "owned" in peer_result[0]["reason"]
        assert created["owner"] == "first" and created["revision"] == 1
        assert campaign.read() == created
    finally:
        if peer is not None and peer.poll() is None:
            peer.kill()
            peer.communicate(timeout=10)
