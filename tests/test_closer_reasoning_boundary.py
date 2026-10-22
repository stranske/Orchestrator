"""Offline native finite reasoning boundaries; no provider or external effects."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

import closer_dispatcher as d
import closer_pipeline as cp
import closer_resources as resources
import execution_profiles

TARGET = "owner/repo#12"
SOURCE = "owner/repo#2"
HEAD = "a" * 40
BASE = "b" * 40
PROFILE = "claude-sonnet-5-5-low"


@pytest.fixture
def native_world(tmp_path, monkeypatch):
    """Real CLI/backend/registry/coordinator; fake only external observations/transport."""
    from test_closer_dispatcher_finite_frontier import command
    from test_closer_quarantine_integration import advertisement, original_state

    root = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(root / "private-home"))
    monkeypatch.setenv("HANDOFF_DIR", str(root / "private-handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(root / "private-orch"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(root / "private-runtime"))
    monkeypatch.setenv("ORCH_FLEET_ROOT", str(root / "empty-fleet"))
    raw, ledger, held = original_state(root)
    ad = advertisement(ledger, "pr:" + TARGET, target=held[0])
    originals = {path: path.read_bytes() for path in (raw, ledger, ad)}
    snapshot = {
        "target": TARGET,
        "kind": "pr",
        "state": "MERGED",
        "head": HEAD,
        "base": BASE,
        "merge": "d" * 40,
        "verifier": {
            "schema": "verifier-corpus-decision/v1",
            "verdict": "PASS",
            "repo": "owner/repo",
            "pr": 12,
            "head_sha": HEAD,
            "evaluated_sha": "d" * 40,
            "run_id": 34,
            "run_attempt": 1,
            "provider_verdicts": ["PASS"],
            "ci_failed": False,
        },
        "revision": "head-a",
        "review_revision": "review-1",
        "checks": "SUCCESS",
        "threads": [],
        "active_threads": 0,
        "mergeable": "MERGEABLE",
        "draft": False,
        "labels": [],
        "head_branch": "feature",
        "head_repository": "owner/repo",
        "sources": [
            {
                "target": SOURCE,
                "body": "## Tasks\n- [ ] Preserve exact report output",
                "state": "open",
                "labels": [],
            }
        ],
    }
    events = {"prepare": 0, "provider": 0, "effect": 0, "prompts": []}

    def observed(self, target):
        assert target == TARGET
        events["prepare"] += 1
        return copy.deepcopy(snapshot)

    monkeypatch.setattr(d.GitHub, "snapshot", observed)

    def github_transport(args, **_kwargs):
        assert args[:3] == ["api", "--method", "GET"] or args[:3] == ["api", "--method", "POST"]
        endpoint = args[3]
        if "/compare/" in endpoint:
            return 0, {
                "status": "ahead",
                "files": [
                    {
                        "filename": "report.py",
                        "status": "added",
                        "patch": "+def report(): return 'original'",
                        "sha": "c" * 40,
                    }
                ],
            }
        if "/actions/runs?" in endpoint:
            return 0, {"workflow_runs": []}
        if endpoint == "graphql":
            return 0, {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            }
        pytest.fail("unexpected external GitHub transport: " + endpoint)

    monkeypatch.setattr(d.gh_capacity, "gh_run", github_transport)
    monkeypatch.setattr(d.gh_capacity, "throttle", lambda *_a, **_kw: {"action": "allow"})
    monkeypatch.setattr(d.Closer, "campaign_admission", lambda *_a, **_kw: {"allowed": True})
    import adapters

    monkeypatch.setattr(
        adapters,
        "data_only_problem",
        lambda agent, mode="assess": (
            None if agent == "claude" and mode == "assess" else "unsupported"
        ),
    )
    profile = execution_profiles.get_profile(PROFILE)
    report = {
        "generated_at": time.time(),
        "agents": {profile["agent"]: {"state": "ok"}},
        "profiles": {PROFILE: {"state": "ok"}},
        "pools": {pool: {"state": "ok"} for pool in profile["capacity_pool_ids"]},
    }
    monkeypatch.setattr(d, "_fresh_capacity_report", lambda: report)
    monkeypatch.setattr(d, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    import dispatcher

    response = {
        "target": TARGET,
        "head": HEAD,
        "base": BASE,
        "classification": "relevant",
        "substantive_needs": [],
        "findings": [],
        "decision": {"criteria": []},
        "drift_mismatches": [],
        "drift_review_complete": True,
        "drift_summary": "Synthetic transport inspected supplied bounded samples; no authority.",
    }

    def provider(agent, prompt, **kwargs):
        assert agent == profile["agent"] and kwargs["profile_id"] == PROFILE
        assert kwargs["data_only"] is True
        assert kwargs["max_network_retries"] == 0
        events["provider"] += 1
        events["prompts"].append(prompt)
        for pool in profile["capacity_pool_ids"]:
            with pytest.raises(resources.ResourceBlocked):
                with resources.pool_slot(
                    root / "private-handoff" / "closer-profile-execution",
                    hashlib.sha256(pool.encode()).hexdigest(),
                    1,
                    deadline=time.time(),
                ):
                    pytest.fail("native reasoning did not hold the real shared pool")
        resources.finite_pool_no_start()  # Synthetic transport spawns no provider process.
        return {"exit": 0, "run_id": "synthetic-reasoning-only", "output": json.dumps(response)}

    monkeypatch.setattr(dispatcher, "offload", provider)
    import closer_queue

    def effect(*_args, **_kwargs):
        events["effect"] += 1
        pytest.fail("historically fenced target reached queue/effect execution")

    monkeypatch.setattr(closer_queue, "run_round", effect)
    argv, frontier, _assignment = command(root, (TARGET,))
    argv[argv.index("--state") + 1] = str(raw)
    return SimpleNamespace(
        root=root,
        raw=raw,
        ledger=ledger,
        ad=ad,
        originals=originals,
        snapshot=snapshot,
        events=events,
        argv=argv,
        frontier=frontier,
        report=report,
        response=response,
    )


def test_real_finite_cli_reasons_over_overlap_then_refuses_action(native_world, capsys):
    w = native_world
    code = d.main(w.argv)
    result = json.loads(capsys.readouterr().out)
    assert w.events["prepare"] == 1, result["targets"][TARGET]
    assert w.events["provider"] == 1, result
    assert w.events["effect"] == 0
    assert code == 78
    assert all(path.read_bytes() == original for path, original in w.originals.items())
    assert cp.validate_stop(w.ledger)["valid"] is False
    shard = resources.shard_dir(resources.scoped_root(w.ledger), TARGET) / "state.json"
    state = json.loads(shard.read_text())
    row = state["frontiers"][w.frontier["frontier_id"]]["items"][TARGET]
    assert row["stage"] == "act", row
    assert row["status"] != "completed"


def invoke(w, capsys):
    code = d.main(w.argv)
    result = json.loads(capsys.readouterr().out)
    return code, result


def protected_unchanged(w):
    assert all(path.read_bytes() == raw for path, raw in w.originals.items())
    assert w.events["effect"] == 0


def test_reasoning_with_valid_incomplete_evidence_reports_gaps_without_pass(native_world, capsys):
    w = native_world
    w.snapshot["base"] = None
    w.response.update(
        classification="uncertain", substantive_needs=["base commit and raw diff are unavailable"]
    )
    _code, _result = invoke(w, capsys)
    assert w.events["provider"] == 1
    prompt = w.events["prompts"][0]
    mandatory = json.JSONDecoder().raw_decode(prompt[prompt.index('{"mandatory":') :])[0][
        "mandatory"
    ]
    context = mandatory["data_only_evidence"]
    assert context["readiness"]["ready"] is False
    assert context["readiness"]["gaps"]
    assert context["sections"]["source"]["content_supplied"] is True
    assert "No file, shell, network, MCP or delegated tools" in prompt
    protected_unchanged(w)


def test_invalid_retained_packet_never_reaches_model(native_world, monkeypatch, capsys):
    w = native_world
    original = d.collect_native_evidence

    def corrupt(*args, **kwargs):
        packet = original(*args, **kwargs)
        manifest = args[2] / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
        manifest.write_text('{"schema":"invalid"}')
        return packet

    monkeypatch.setattr(d, "collect_native_evidence", corrupt)
    invoke(w, capsys)
    assert w.events["prepare"] == 1 and w.events["provider"] == 0
    protected_unchanged(w)


def test_substantive_reply_cannot_enter_exception_repair(native_world, capsys):
    w = native_world
    w.response["substantive_needs"] = ["security-sensitive repair requires owner decision"]
    invoke(w, capsys)
    assert w.events["provider"] == 1
    shard = resources.shard_dir(resources.scoped_root(w.ledger), TARGET) / "state.json"
    row = json.loads(shard.read_text())["frontiers"][w.frontier["frontier_id"]]["items"][TARGET]
    assert row["stage"] == "exception" and row["status"] != "completed"
    assert "quarantined_resource" in row["error"]
    protected_unchanged(w)


def test_new_frontier_cannot_rearm_own_unknown_or_reconcile_it(native_world, capsys):
    w = native_world
    invoke(w, capsys)
    assert w.events["provider"] == 1
    shard = resources.shard_dir(resources.scoped_root(w.ledger), TARGET) / "state.json"
    original = shard.read_bytes()
    frontier = copy.deepcopy(w.frontier)
    frontier["frontier_id"] = "second-unrelated-attempt"
    w.argv[w.argv.index("--finite-frontier-json") + 1] = json.dumps(frontier)
    _code, result = invoke(w, capsys)
    assert w.events["provider"] == 1 and w.events["prepare"] == 1
    assert shard.read_bytes() == original
    assert "unresolved_target_shard" in result["targets"][TARGET]["error"]
    protected_unchanged(w)


def test_generic_legacy_scoped_callbacks_still_stop_at_overlap(native_world, capsys):
    w = native_world
    i = w.argv.index("--finite-frontier-json")
    del w.argv[i : i + 2]
    w.argv += ["--budget-seconds", "10"]
    invoke(w, capsys)
    assert w.events["prepare"] == w.events["provider"] == 0
    protected_unchanged(w)


def test_global_corruption_is_not_an_observation_permission(native_world, capsys):
    w = native_world
    w.ledger.write_text('{"corrupt":true}')
    w.originals[w.ledger] = w.ledger.read_bytes()
    invoke(w, capsys)
    assert w.events["prepare"] == w.events["provider"] == 0
    protected_unchanged(w)


def test_current_assignment_pause_remains_a_native_wait(native_world, capsys):
    w = native_world
    assignment = w.argv[w.argv.index("--assignment") + 1]
    from pathlib import Path

    Path(assignment).write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "target": TARGET,
                        "execution_state": "paused",
                        "fingerprint": "synthetic-owner-pause",
                    }
                ]
            }
        )
    )
    invoke(w, capsys)
    assert w.events["provider"] == 0
    protected_unchanged(w)


def test_live_alias_owner_still_blocks_readonly_reasoning(native_world, capsys):
    w = native_world
    w.argv[w.argv.index("--request-timeout-seconds") + 1] = "0.2"
    root = resources.scoped_root(w.ledger)
    resources.open_root(w.ledger, pipeline_schema=cp.SCHEMA)
    with resources.scope_locks(
        root, ["pr:" + TARGET, "repo:owner/repo"], deadline=time.time() + 1, now=time.time
    ):
        invoke(w, capsys)
    assert w.events["prepare"] == w.events["provider"] == 0
    protected_unchanged(w)


def test_scope_proofs_distinguish_observation_from_effect(native_world, capsys):
    w = native_world
    invoke(w, capsys)
    directory = resources.scoped_root(w.ledger) / "scope-admissions"
    proofs = [json.loads(path.read_text()) for path in directory.rglob("*.json")]
    read = [
        proof for proof in proofs if proof["stage"] in {"target_admission", "prepare", "interpret"}
    ]
    assert read and all(proof["purpose"] == "observation" and proof["admitted"] for proof in read)
    assert all(proof["historical_overlaps"]["quarantine_keys"] for proof in read)
    effect = [proof for proof in proofs if proof["stage"] == "act"]
    assert effect and all(
        proof["purpose"] == "effect" and proof["admitted"] is False for proof in effect
    )
    assert all(proof["legacy_stop_authority"] is False for proof in proofs)
    protected_unchanged(w)


@pytest.mark.parametrize("damage", ["digest", "stale", "global", "inventory", "target", "purpose"])
def test_observation_does_not_waive_scope_integrity_or_inventory(native_world, damage):
    import closer_quarantine as quarantine

    w = native_world
    resources.open_root(w.ledger, pipeline_schema=cp.SCHEMA)
    snapshot = quarantine.scope_snapshot(w.ledger)
    keys = ["pr:" + TARGET, "repo:owner/repo"]
    assert quarantine.scope_problem(snapshot, TARGET, keys, purpose="observation") is None
    assert quarantine.scope_problem(snapshot, TARGET, keys).startswith("quarantined_resource:")
    target, purpose = TARGET, "observation"
    if damage == "digest":
        snapshot["snapshot_sha256"] = "0" * 64
    elif damage == "stale":
        w.ad.write_text(w.ad.read_text() + "\n")
    elif damage == "global":
        w.ledger.write_text('{"corrupt":true}')
        snapshot = quarantine.scope_snapshot(w.ledger)
    elif damage == "inventory":
        keys = ["pr:" + TARGET]
    elif damage == "target":
        target = "unbounded"
    else:
        purpose = "unrestricted"
    assert quarantine.scope_problem(snapshot, target, keys, purpose=purpose) is not None
