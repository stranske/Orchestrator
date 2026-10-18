"""A started local delegate reaches both the private ledger and the Brain's causal edge."""

import json

import pytest

import capabilities
import capability_outcome_bridge as bridge
import dispatcher
import feedback
import roles

LANES = {
    "testgen": "testgen-lane",
    "codemod": "codemod-campaign",
    "cross_repo": "cross-repo-coordination",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for name in LANES.values():
        row = capabilities._blank_capability(name)
        row["capability_version_id"] = f"{name}@test-v1"
        rows[name] = row
    capabilities.save(rows, ledger)
    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(dispatcher.adapters, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(
        dispatcher.adapters, "LEDGER", tmp_path / "handoff" / "capacity-ledger.ndjson"
    )
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(dispatcher.claims, "claim", lambda *_: True)
    monkeypatch.setattr(dispatcher.claims, "release", lambda *_: None)
    # A delegated workspace must not contain the isolated control-plane state.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(dispatcher.provision, "provision", lambda *_: workspace)
    monkeypatch.setattr(
        dispatcher.repo_knowledge, "append_context", lambda prompt, *_a, **_k: prompt
    )
    monkeypatch.setattr(
        roles, "activate_dispatch_roles", lambda _a, prompt, **_k: {"prompt": prompt}
    )
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *_a, **_k: ["true"])
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *_: "test-model")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_net_hygiene_prelude", lambda: "")
    # Run the real plan_dispatch and _spawn recording path; intercept only OS launch.
    monkeypatch.setattr(
        dispatcher.subprocess, "Popen", lambda *_a, **_k: type("Process", (), {"pid": 1234})()
    )
    return ledger


def _delegate(task_type):
    return dispatcher.delegate(
        "cursor", "stranske/Test#432", "opener", "Do the bounded work", task_type=task_type
    )


def _assert_credit(world, result, capability_id):
    assert "error" not in result
    row = json.loads(world.read_text())["capabilities"][capability_id]
    events = [e for e in row["event_history"] if e["type"] == "invocation"]
    assert len(events) == 1
    assert events[0]["ref"] == "stranske/Test#432"
    assert events[0]["metadata"]["run_id"] == result["run_id"]
    with feedback._conn() as db:
        event = db.execute(
            "SELECT payload_json FROM completion_events WHERE run_id=? AND phase='decision'",
            (result["run_id"],),
        ).fetchone()
        assert json.loads(event[0])["capability_ids"] == [capability_id]
        edges = db.execute(
            "SELECT capability_id, capability_version_id FROM influence_edges "
            "WHERE target_run_id=? AND influence_type='capability' AND accepted=1",
            (result["run_id"],),
        ).fetchall()
        assert {tuple(e) for e in edges} == {(capability_id, f"{capability_id}@test-v1")}

    # Follow the actual terminal outcome through the bridge, rather than assuming
    # a persisted decision tag is enough to credit the lane's result.
    feedback.record_outcome(
        result["run_id"], adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    report = bridge.attribute(bridge.collect(), known=set(LANES.values()))
    assert report["unattributed"] == []
    assert report["unknown_capability"] == []
    assert report["links"] == [
        {
            "capability_id": capability_id,
            "run_id": result["run_id"],
            "resolver": "run_tagged",
            "verdict": "PASS",
            "durability": "durable",
        }
    ]
    assert bridge.apply_links(report["links"], path=world)["written"] == 1
    assert bridge.apply_links(report["links"], path=world)["already_linked"] == 1
    rows = json.loads(world.read_text())["capabilities"]
    outcomes = [
        (name, event)
        for name, row in rows.items()
        for event in row["event_history"]
        if event["type"] == "outcome"
    ]
    assert len(outcomes) == 1
    name, event = outcomes[0]
    assert name == capability_id
    assert event["ref"] == result["run_id"]
    assert event["metadata"] == {
        "resolver": "run_tagged",
        "verdict": "PASS",
        "durability": "durable",
    }


def test_a_testgen_delegate_heartbeats_testgen_lane_and_tags_the_run(world):
    _assert_credit(world, _delegate("testgen"), "testgen-lane")
    # The audit finding must survive declaration reconciliation on a fresh ledger.
    for rows in (capabilities.load_declared(world), capabilities.load(world)):
        notes = rows["testgen-lane"]["notes"]
        for finding in ("2026-10-04", "44 testgen runs", "32 PASS", "26 durable", "dedup"):
            assert finding in notes


@pytest.mark.parametrize("task_type", ["codemod", "cross_repo"])
def test_codemod_and_cross_repo_delegates_tag_their_lanes(world, task_type):
    _assert_credit(world, _delegate(task_type), LANES[task_type])


def test_an_implement_delegate_tags_none_of_them(world):
    result = _delegate("implement")
    assert "error" not in result
    assert all(
        not r["last_invocation"] for r in json.loads(world.read_text())["capabilities"].values()
    )
    with feedback._conn() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM influence_edges WHERE target_run_id=? AND influence_type='capability'",
                (result["run_id"],),
            ).fetchone()[0]
            == 0
        )


def test_review_loop_survives_real_plan_and_spawn_with_one_chain_identity(world, monkeypatch):
    from types import SimpleNamespace

    launches = []
    monkeypatch.setattr(
        dispatcher.subprocess,
        "Popen",
        lambda argv, **kw: launches.append((argv, kw)) or SimpleNamespace(pid=4321),
    )
    result = dispatcher.delegate(
        "cursor",
        "stranske/Test#432",
        "opener",
        "Repair the same PR",
        review_loop=True,
        review_round=2,
        review_run_id="original-chain",
    )
    assert "error" not in result
    command = launches[0][0][-1]
    assert "review_loop.py after-writer" in command
    assert "--round 2" in command and "--run-id original-chain" in command
    assert '--writer-exit "$orch_dispatch_rc"' in command
    assert command.index("claims.py release") < command.index("review_loop.py after-writer")
    # Completion bookkeeping failure must not suppress the fail-closed review
    # status; the hook separately sees the actual writer's exit and fresh claims.
    assert "; " in command.split("review_loop.py after-writer")[0]
    assert command.endswith("exit $orch_dispatch_rc")
    assert "--writer-run-id" in command
    with feedback._conn() as conn:
        metadata = json.loads(
            conn.execute(
                "SELECT routing_metadata FROM runs WHERE run_id=?", (result["run_id"],)
            ).fetchone()[0]
        )
    assert metadata["review_loop"] is True
    assert metadata["review_run_id"] == "original-chain"


def test_plain_delegate_does_not_start_optional_review(world, monkeypatch):
    from types import SimpleNamespace

    launches = []
    monkeypatch.setattr(
        dispatcher.subprocess,
        "Popen",
        lambda argv, **kw: launches.append(argv) or SimpleNamespace(pid=4321),
    )
    assert "error" not in _delegate("implement")
    assert "review_loop.py after-writer" not in launches[0][-1]


def test_review_prompt_requires_explicit_pr_marker_without_merge_permission(world, monkeypatch):
    prompts = []
    import roles

    monkeypatch.setattr(
        roles,
        "activate_dispatch_roles",
        lambda _a, prompt, **kw: prompts.append(prompt) or {"prompt": prompt},
    )
    dispatcher.delegate(
        "cursor",
        "stranske/Test#432",
        "opener",
        "Bounded work",
        review_loop=True,
    )
    assert "PR_URL=https://github.com/" in prompts[0]
    assert "Do not merge" in prompts[0]


def test_review_loop_refuses_launch_if_opt_in_cannot_be_persisted(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("Brain unavailable")

    monkeypatch.setattr(feedback, "record_run", refuse)
    monkeypatch.setattr(
        dispatcher.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("untracked review-loop writer"),
    )
    with pytest.raises(RuntimeError, match="required completion lineage"):
        dispatcher.delegate("cursor", "stranske/Test#432", "opener", "Bounded", review_loop=True)


@pytest.mark.parametrize("failure", ["claimed", "unbuildable", "spawn"])
def test_a_delegate_that_does_not_start_never_invokes_the_lane(world, monkeypatch, failure):
    if failure == "claimed":
        monkeypatch.setattr(dispatcher.claims, "claim", lambda *_: False)
        monkeypatch.setattr(dispatcher.claims, "holder", lambda *_: {"agent": "other"})
    elif failure == "unbuildable":
        monkeypatch.setattr(dispatcher, "plan_dispatch", lambda *_a, **_k: None)
    else:

        def fail(*_a, **_k):
            raise OSError("worker could not start")

        monkeypatch.setattr(dispatcher.subprocess, "Popen", fail)
    if failure == "spawn":
        with pytest.raises(OSError, match="could not start"):
            _delegate("testgen")
    else:
        assert "error" in _delegate("testgen")
    assert all(
        not r["last_invocation"] for r in json.loads(world.read_text())["capabilities"].values()
    )


@pytest.mark.parametrize("task_type", list(LANES))
def test_generic_run_does_not_credit_delegate_lane(world, task_type):
    result = dispatcher.run(
        {
            "assignments": [
                {
                    "agent": "cursor",
                    "target": "stranske/Test#432",
                    "task_type": task_type,
                    "prompt": "bounded work",
                }
            ]
        },
        heartbeat=False,
    )
    with feedback._conn() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM influence_edges WHERE influence_type='capability'"
            ).fetchone()[0]
            == 0
        )
    assert all(not row["last_invocation"] for row in capabilities.load(world).values())
    assert result["count"] == 1
    assert not result["skipped"]


def test_started_delegate_survives_heartbeat_write_failure(world, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(capabilities, "heartbeat", fail)
    result = _delegate("testgen")
    assert "error" not in result
    assert result["pid"] == 1234
    assert result["run_id"]
