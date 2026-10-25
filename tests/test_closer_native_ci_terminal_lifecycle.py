"""Signed actual CI descendants and original Core correlation, entirely offline."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest
from test_closer_ci_followup import public_ci_world as _public_ci_world
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_descendants import native_descendant_world as native_descendant_world
from test_closer_native_observer import host as host
from test_closer_runtime_binding import Contents
from test_closer_runtime_binding import bundle as bundle

import closer_campaign as campaign
import closer_dispatcher as dispatcher
import closer_hold_followup as holds
import closer_pipeline as pipeline
import closer_runtime_binding as runtime
import verification_events


@pytest.fixture
def public_ci_world(request, monkeypatch, bundle):
    w = _public_ci_world.__wrapped__(request, monkeypatch)
    monkeypatch.setattr(bundle.runtime, "fetch_live_pr_labels", lambda *_: [])
    contents = Contents()

    def transport(method, path, body):
        if method == "GET" and path == "/repos/owner/repo/pulls/12":
            return {
                "head": {"sha": w.snapshot["head"]},
                "merged": w.snapshot["state"] == "MERGED",
                "merge_commit_sha": w.snapshot["merge"],
            }
        return contents(method, path, body)

    worker = w.root / "original-core-worker.toml"
    worker.write_text('status = "ACTIVE"\n')
    monkeypatch.setattr(runtime, "_POLICY_READERS", {})
    runtime.register_policy_reader(
        "signed-ci-original",
        lambda: {
            "source": "controller-app-readback",
            "observed_at": time.time(),
            "worker_toml_digests": {str(worker): runtime._hash(worker)},
            "controller_status": "ACTIVE",
            "worker_statuses": ["ACTIVE"],
        },
    )
    original = runtime.RuntimeBinding(
        bundle,
        {"worker_tomls": [str(worker)], "reader_id": "signed-ci-original"},
        transport=transport,
    )
    monkeypatch.setattr(runtime, "binding_from_manifest", lambda *_: original)
    manifest = w.root / "original-core-binding.json"
    manifest.write_text("{}")
    w.argv += ["--runtime-manifest", str(manifest)]
    w.argv[w.argv.index("--coordinator-contract-pin") + 1] = json.dumps(
        pipeline.scoped_capability()
    )
    return w


def terminal(world, monkeypatch):
    w, cfg, _ref, obj, *_ = world
    manifest = campaign.load_manifest(cfg["runtime_manifest"])
    doc = holds.capture(obj)
    held = next(iter(doc["hold_followups"].values()))
    assert held["release_proof"]["kind"] == "accepted_verifier_terminal"
    source = Path(held["source"]["state_path"])
    queue_path = source.parent / "state.actions" / (holds._digest(held["target"]) + ".json")
    import closer_queue as queue

    data = queue._read_state(queue_path)
    op = data["frontiers"][held["frontier"]["frontier_id"]]["items"][held["target"]]["operation"]
    assert op["result"]["core_request_correlation"]
    event = {
        "schema": verification_events.SCHEMA,
        "repo": "owner/repo",
        "pr": "12",
        "head_sha": w.snapshot["head"],
        "evaluated_sha": w.snapshot["merge"],
        "request_sha256": "c" * 64,
        "material_sha256": "e" * 64,
    }
    event["event_id"] = verification_events.event_id(event)
    marker = {
        "schema": "verifier-corpus-decision/v1",
        "verdict": "NON_PASS",
        "repo": "owner/repo",
        "pr": 12,
        "head_sha": w.snapshot["head"],
        "evaluated_sha": w.snapshot["merge"],
        "run_id": 42,
        "run_attempt": 1,
        "provider_verdicts": ["CONCERNS"],
        "ci_failed": False,
        "verification_event": event,
        "verification_request_basis": op["result"]["verification_request_basis"],
        "assessment_identity": {
            "mode": "compare",
            "provider": "anthropic",
            "model": "",
            "model2": "",
            "canonical": True,
        },
    }
    w.snapshot["verifier"] = marker
    w.snapshot["verifier_lifecycle"] = copy.deepcopy(marker)
    w.snapshot["verifier_runs"][0].update(status="completed", conclusion="success")
    w.snapshot["revision"] = "terminal-correlated-current"
    prior = dispatcher.gh_capacity.gh_run

    def read(args, **kwargs):
        if args[3] == "repos/owner/repo/actions/runs/42":
            return 0, {
                "id": 42,
                "run_attempt": 1,
                "event": "pull_request_target",
                "path": ".github/workflows/agents-verifier.yml",
                "status": "completed",
                "repository": {"full_name": "owner/repo"},
            }
        if args[3] == "repos/owner/repo/actions/runs/42/artifacts?per_page=100":
            return 0, {"artifacts": []}
        return prior(args, **kwargs)

    monkeypatch.setattr(dispatcher.gh_capacity, "gh_run", read)
    from closer_verifier_followup import source_requirements

    def forbidden(*_a, **_k):
        pytest.fail("read-only terminal follow-up attempted an offload")

    import dispatcher as offloader

    monkeypatch.setattr(offloader, "offload", forbidden)
    observer = holds._backend(cfg["runtime_manifest"], doc)
    assert observer.apply is False and observer.workers is True
    observed = observer.observe(held["target"])
    before_queue = queue_path.read_bytes()
    refused = observer.execute(held["target"], "disposition", observed, op)
    assert refused["status"] == "deferred" and refused["reason"] == "inventory only"
    assert queue_path.read_bytes() == before_queue
    assert observed.get("eligible") is True, {
        k: observed.get(k) for k in ("eligible", "action", "reason", "next_trigger")
    }
    assert (
        source_requirements(observed)
        == op["result"]["core_request_correlation"]["native_binding"][
            "verification_source_requirements"
        ]
    )
    return w, cfg, obj, manifest, held, source, queue_path


@pytest.mark.parametrize("crash", [False, True], ids=["terminal", "restart-after-intent"])
def test_actual_signed_leaf_terminal_rearms_only_original_full_cohort(
    native_descendant_world, monkeypatch, crash, capsys
):
    w, cfg, obj, manifest, held, source, queue_path = terminal(native_descendant_world, monkeypatch)
    before_source, before_queue = source.read_bytes(), queue_path.read_bytes()
    if crash:
        update = obj.update

        class Interruption(BaseException):
            pass

        def interrupt(callback):
            if callback.__name__ == "finish":
                raise Interruption()
            return update(callback)

        monkeypatch.setattr(obj, "update", interrupt)
        with pytest.raises(Interruption):
            holds.consume(obj, manifest, cfg["runtime_manifest"])
        original = obj.read()["hold_followups"][held["hold_id"]]
        assert original["status"] == "checking" and original["terminal_followup_intent"]
        obj = campaign.Campaign(obj.path)
    released, _reports = holds.consume(obj, manifest, cfg["runtime_manifest"])
    record = released["hold_followups"][held["hold_id"]]
    assert record["status"] == "released", record.get("last_result")
    assert record["source"]["descendant_coverage"]["frontier"] == held["frontier"]
    rearmed = holds.rearm(obj, released, manifest)
    assert rearmed["generation"] == 2
    assert rearmed["worker_config"]["finite_frontier"] == cfg["finite_frontier"]
    assert rearmed["targets"] == {campaign.ROUND_UNIT: "pending"}
    assert rearmed["hold_followups"][held["hold_id"]]["status"] == "consumed"
    assert holds.rearm(obj, obj.read(), manifest)["generation"] == 2
    assert source.read_bytes() == before_source and queue_path.read_bytes() == before_queue
    assert len(w.merges) == len(w.verifies) == 1 and w.events["provider"] == 0
    original_row = copy.deepcopy(
        pipeline._load(source)["frontiers"][held["frontier"]["frontier_id"]]["items"][
            held["target"]
        ]
    )
    # The next canonical coordinator has no qualified classifier in this
    # synthetic case. It must create its fresh successor/PREPARE and stop at
    # that explicit role hold, without reusing the prior accepted ACT.
    monkeypatch.setattr(dispatcher, "_profile_exception_qualified", lambda *_a, **_k: False)
    dispatcher.main(w.argv)
    followed = json.loads(capsys.readouterr().out)
    state = pipeline._load(source)
    links = state["stage_successors"]
    children = [
        value
        for value in links.values()
        if value["stage_predecessor"]["predecessor_frontier_id"] == held["frontier"]["frontier_id"]
    ]
    assert len(children) == 1, followed.get("merge_verification_successors")
    child = children[0]["finite_frontier"]
    fresh = state["frontiers"][child["frontier_id"]]["items"][held["target"]]
    assert fresh["results"]["prepare"]["revision"] == "terminal-correlated-current"
    assert fresh["results"]["prepare"]["snapshot_ref"]
    assert fresh["status"] == "held" and fresh["stage"] == "interpret", fresh
    pending = fresh["results"]["interpret"]
    assert pending["classification"] == "role_qualification_unavailable"
    assert pending["pending_evidence"]["target"] == held["target"]
    assert pending["pending_evidence"]["revision"] == child["bindings"][held["target"]]["revision"]
    assert (
        state["frontiers"][held["frontier"]["frontier_id"]]["items"][held["target"]] == original_row
    )
    assert len(w.merges) == len(w.verifies) == 1 and w.events["provider"] == 0
