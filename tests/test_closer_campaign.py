from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import install_closer_campaign_hook as hook

import closer_campaign as cc
from closer_release import ReleaseRecord

DIGEST = "c" * 64
CAND = "a" * 40


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
