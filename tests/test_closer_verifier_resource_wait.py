"""Native verifier prewrite resource waits are known no-effect, not owned runs."""

import copy
import hashlib
import json

import pytest
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_reasoning_boundary import native_world as native_world
from test_closer_verifier_attribution import archive

import closer_dispatcher as native
import closer_resources as resources


@pytest.fixture
def waiting_world(public_merge_world, monkeypatch):
    w = public_merge_world
    w.blocking_run = {
        "id": 99,
        "run_attempt": 1,
        "head_sha": "e" * 40,
        "status": "in_progress",
        "conclusion": None,
        "path": ".github/workflows/agents-verifier.yml",
        "event": "pull_request_target",
        "pull_requests": [{"number": 12}],
        "repository": {"full_name": "owner/repo"},
        "created_at": "2026-10-22T00:00:01Z",
    }
    transport = native.gh_capacity.gh_run
    w.inventory_requests = []
    corpus = archive()
    monkeypatch.setattr(native, "native_artifact_fetcher", lambda _: lambda *_a, **_k: corpus)

    def api(args, **kwargs):
        endpoint = args[3]
        if "/actions/workflows/agents-verifier.yml/runs?" in endpoint:
            w.inventory_requests.append(endpoint)
            if "&status=in_progress" in endpoint and w.blocking_run["status"] != "completed":
                return 0, {"total_count": 1, "workflow_runs": [copy.deepcopy(w.blocking_run)]}
            if "&status=" not in endpoint:
                return 0, {"total_count": 1, "workflow_runs": [copy.deepcopy(w.blocking_run)]}
            return 0, {"total_count": 0, "workflow_runs": []}
        if endpoint.endswith("/actions/runs/99/artifacts?per_page=100"):
            return 0, {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 7,
                        "name": "comparison-results-99",
                        "expired": False,
                        "size_in_bytes": len(corpus),
                        "digest": "sha256:" + hashlib.sha256(corpus).hexdigest(),
                        "workflow_run": {"id": 99, "head_sha": w.blocking_run["head_sha"]},
                    }
                ],
            }
        if endpoint.endswith("/actions/runs/99"):
            return 0, copy.deepcopy(w.blocking_run)
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    return w


def test_public_racing_same_target_verifier_is_named_no_effect_wait(waiting_world, capsys):
    w = waiting_world
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(w.merges) == 1 and not w.verifies and w.events["provider"] == 0
    child = result["merge_verification_successors"]["owner/repo#12"]["native_result"]
    assert child["validation"]["scoped_completion_valid"] is True, child["targets"]
    row = child["targets"]["owner/repo#12"]
    assert row["status"] == "held" and "error" not in row, row
    act = row["results"]["act"]
    assert act["pending_kind"] == "external_event"
    proof = act["pending_evidence"]
    assert proof["target"] == "owner/repo#12" and proof["blocking_run"]["id"] == 99
    assert proof["blocking_run"]["head_sha"] != "a" * 40
    assert proof["head"] == "a" * 40 and proof["merge"] == "d" * 40
    assert all(p.read_bytes() == raw for p, raw in w.originals.items())


def test_unchanged_frontier_waits_then_terminal_event_fresh_frontier_verifies_once(
    waiting_world, capsys
):
    import time

    import closer_frontier_policy as policy
    import closer_pipeline as pipeline

    w = waiting_world
    assert native.main(w.argv) == 78
    first = json.loads(capsys.readouterr().out)
    child = first["merge_verification_successors"]["owner/repo#12"]["native_result"]
    event = child["targets"]["owner/repo#12"]["results"]["act"]["pending_evidence"]["event_id"]
    shard = resources.shard_dir(resources.scoped_root(w.ledger), "owner/repo#12") / "state.json"
    old = copy.deepcopy(pipeline._load(shard)["frontiers"])
    inventory_reads = len(w.inventory_requests)
    assert native.main(w.argv) == 78
    capsys.readouterr()
    # An unchanged retained child waits for its exact external terminal trigger;
    # ordinary observation performs no optional history/capture polling.
    assert w.inventory_requests[inventory_reads:] == []
    assert not w.verifies and len(w.merges) == 1
    w.blocking_run.update(status="completed", conclusion="success")
    # The next campaign is causally bound to a real external terminal read,
    # rather than a new namespace around an unchanged blocker or UNKNOWN effect.
    terminal = native.GitHub(deadline=time.time() + 25).api("repos/owner/repo/actions/runs/99")
    assert terminal["id"] == 99 and terminal["run_attempt"] == 1
    assert terminal["status"] == "completed" and event.endswith(":99:attempt:1:terminal")
    observer = native.Closer(
        native.GitHub(deadline=time.time() + 25), state_path=w.raw, apply=True, workers=True
    )
    frontier = policy.make_frontier_from_observations(
        {"owner/repo#12": observer.observe("owner/repo#12")},
        owner=w.frontier["owner"],
        frontier_id="run99-attempt1-terminal",
    )
    argv = list(w.argv)
    argv[argv.index("--finite-frontier-json") + 1] = json.dumps(frontier)
    assert native.main(argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(w.verifies) == 1 and len(w.merges) == 1 and w.events["provider"] == 0, result
    assert native.main(argv) == 78
    capsys.readouterr()
    assert len(w.verifies) == 1
    after = pipeline._load(shard)["frontiers"]
    assert all(after[key]["items"] == entry["items"] for key, entry in old.items())


@pytest.mark.parametrize("linked", [[{"number": 12}]])
def test_prewrite_blocking_run_never_becomes_target_owned_async(waiting_world, capsys, linked):
    w = waiting_world
    w.blocking_run["pull_requests"] = linked
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["merge_verification_successors"]["owner/repo#12"]["native_result"]["targets"][
        "owner/repo#12"
    ]
    assert row["results"]["act"]["pending_kind"] == "external_event", row
    assert row["results"]["act"]["pending_evidence"]["blocking_run"]["pull_requests"] == linked
    assert not w.verifies


def test_incomplete_active_inventory_never_authorizes_write(waiting_world, monkeypatch, capsys):
    w = waiting_world
    transport = native.gh_capacity.gh_run

    def api(args, **kwargs):
        if "/actions/workflows/agents-verifier.yml/runs?" in args[3] and "&status=" in args[3]:
            return 0, {"total_count": 2, "workflow_runs": [copy.deepcopy(w.blocking_run)]}
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["merge_verification_successors"]["owner/repo#12"]["native_result"]["targets"][
        "owner/repo#12"
    ]
    assert row["status"] == "held" and "error" not in row and not w.verifies, row
    assert (
        row["results"]["act"]["pending_evidence"]["collection_cause"]
        == "active_inventory_incomplete"
    )


def test_racing_empty_active_page_is_refetched_before_dispatch(waiting_world, monkeypatch, capsys):
    w = waiting_world
    w.blocking_run.update(status="completed", conclusion="success")
    transport = native.gh_capacity.gh_run
    queued_reads = []

    def api(args, **kwargs):
        if "/agents-verifier.yml/runs?" in args[3] and "&status=queued" in args[3]:
            queued_reads.append(args[3])
            # Exact retained #714 response: count and empty page raced.
            return 0, {"total_count": 2 if len(queued_reads) == 1 else 0, "workflow_runs": []}
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    assert native.main(w.argv) == 78
    capsys.readouterr()
    assert len(queued_reads) >= 2 and len(w.verifies) == 1
    assert w.events["provider"] == 0
    assert native.main(w.argv) == 78
    capsys.readouterr()
    assert len(w.verifies) == 1


def test_active_verifier_on_second_page_still_prevents_post(waiting_world, monkeypatch, capsys):
    w = waiting_world
    transport = native.gh_capacity.gh_run
    pages = []

    def api(args, **kwargs):
        endpoint = args[3]
        if "/agents-verifier.yml/runs?" in endpoint and "&status=in_progress" in endpoint:
            pages.append(endpoint)
            if "&page=2" in endpoint:
                return 0, {"total_count": 101, "workflow_runs": [copy.deepcopy(w.blocking_run)]}
            foreign = [
                {**copy.deepcopy(w.blocking_run), "id": 1000 + i, "pull_requests": [{"number": 13}]}
                for i in range(100)
            ]
            return 0, {"total_count": 101, "workflow_runs": foreign}
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["merge_verification_successors"]["owner/repo#12"]["native_result"]["targets"][
        "owner/repo#12"
    ]
    proof = row["results"]["act"]["pending_evidence"]
    assert proof["blocking_run"]["id"] == 99
    assert proof["inventory_read"]["endpoint"].endswith("&page=2")
    assert proof["inventory_read"]["status"] == "observed"
    assert len(proof["inventory_read"]["sha256"]) == 64
    assert any("&page=2" in page for page in pages) and not w.verifies


def test_fresh_complete_inventory_wakes_new_frontier_without_resetting_old_wait(
    waiting_world, monkeypatch, capsys
):
    import time

    import closer_frontier_policy as policy
    import closer_pipeline as pipeline

    w = waiting_world
    w.blocking_run.update(status="completed", conclusion="success")
    transport = native.gh_capacity.gh_run
    incomplete = True

    def api(args, **kwargs):
        if incomplete and "/agents-verifier.yml/runs?" in args[3] and "&status=queued" in args[3]:
            return 0, {"total_count": 2, "workflow_runs": []}
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    assert native.main(w.argv) == 78
    first = json.loads(capsys.readouterr().out)
    child = first["merge_verification_successors"]["owner/repo#12"]["native_result"]
    assert (
        child["targets"]["owner/repo#12"]["results"]["act"]["pending_evidence"]["collection_cause"]
        == "active_inventory_incomplete"
    )
    shard = resources.shard_dir(resources.scoped_root(w.ledger), "owner/repo#12") / "state.json"
    old = copy.deepcopy(pipeline._load(shard)["frontiers"])
    incomplete = False
    gh = native.GitHub(deadline=time.time() + 25)
    endpoint = (
        "repos/owner/repo/actions/workflows/agents-verifier.yml/runs?per_page=100&status=queued"
    )
    assert gh.api(endpoint) == {"total_count": 0, "workflow_runs": []}
    observer = native.Closer(gh, state_path=w.raw, apply=True, workers=True)
    frontier = policy.make_frontier_from_observations(
        {"owner/repo#12": observer.observe("owner/repo#12")},
        owner=w.frontier["owner"],
        frontier_id="complete-active-inventory-after-read-wait",
    )
    argv = list(w.argv)
    argv[argv.index("--finite-frontier-json") + 1] = json.dumps(frontier)
    assert native.main(argv) == 78
    capsys.readouterr()
    assert len(w.verifies) == 1 and w.events["provider"] == 0
    after = pipeline._load(shard)["frontiers"]
    assert all(after[key]["items"] == entry["items"] for key, entry in old.items())


def test_timeout_after_label_post_remains_uncertain(public_merge_world, monkeypatch, capsys):
    w = public_merge_world
    transport = native.gh_capacity.gh_run
    writes = []

    def api(args, **kwargs):
        if args[3].endswith("/issues/12/labels"):
            writes.append(args)
            raise TimeoutError("effect readback unavailable")
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["merge_verification_successors"]["owner/repo#12"]["native_result"]["targets"][
        "owner/repo#12"
    ]
    assert len(writes) == 1 and row["status"] == "unknown"
    assert "pending_evidence" not in row.get("results", {}).get("act", {})
    assert native.main(w.argv) == 78
    capsys.readouterr()
    assert len(writes) == 1


def test_retained_resource_proof_rejects_tampered_identity(waiting_world, capsys):
    import closer_pipeline as pipeline
    import closer_queue as queue

    w = waiting_world
    assert native.main(w.argv) == 78
    capsys.readouterr()
    shard = resources.shard_dir(resources.scoped_root(w.ledger), "owner/repo#12") / "state.json"
    action = shard.parent / "state.actions" / f'{pipeline._digest("owner/repo#12")}.json'
    doc = queue._read_state(action)
    bucket = next(v for k, v in doc["frontiers"].items() if k.startswith("stage-successor"))
    operation = bucket["items"]["owner/repo#12"]["operation"]
    assert queue.verifier_resource_wait_proof(operation, "owner/repo#12") is not None
    changes = [
        ("target", "foreign/repo#9"),
        ("head", "f" * 40),
        ("merge", "f" * 40),
        ("endpoint", "repos/foreign/repo/actions/runs"),
        ("blocking_run.id", 0),
        ("blocking_run.status", "completed"),
        ("blocking_run.run_attempt", 0),
        ("blocking_run.path", ".github/workflows/untrusted.yml"),
    ]
    for field, value in changes:
        changed = copy.deepcopy(operation)
        proof = changed["result"]["verifier_resource_wait"]
        if "." in field:
            parent, key = field.split(".")
            proof[parent][key] = value
        else:
            proof[field] = value
        proof["sha256"] = queue._digest({k: v for k, v in proof.items() if k != "sha256"})
        assert queue.verifier_resource_wait_proof(changed, "owner/repo#12") is None, field
    changed = copy.deepcopy(operation)
    changed["result"]["verifier_resource_wait"]["sha256"] = "0" * 64
    assert queue.verifier_resource_wait_proof(changed, "owner/repo#12") is None


def test_arbitrary_callback_cannot_assert_native_no_effect(tmp_path):
    import time

    import closer_queue as queue

    target = "owner/repo#12"
    binding = {"pr_target": target, "head": "a" * 40, "merge": "d" * 40}

    def observe(_target):
        return {
            "target": target,
            "revision": "revision",
            "action": "post_merge_verify",
            "eligible": True,
            "operation_binding": binding,
        }

    def forged(_target, _action, _snapshot, _operation):
        proof = {
            "schema": "closer-verifier-prewrite-wait/v1",
            "boundary": "before_verify_label_post",
            "target": target,
            "subject_pr": target,
            "revision": "revision",
            "head": "a" * 40,
            "merge": "d" * 40,
            "endpoint": "repos/owner/repo/actions/workflows/agents-verifier.yml/runs?per_page=100&status=in_progress",
            "observed_at": time.time(),
            "blocking_run": {
                "id": 99,
                "run_attempt": 1,
                "head_sha": "e" * 40,
                "status": "in_progress",
                "conclusion": None,
                "path": ".github/workflows/agents-verifier.yml",
                "event": "pull_request_target",
                "pull_requests": [],
                "repository": {"full_name": "owner/repo"},
            },
        }
        proof["sha256"] = queue._digest(proof)
        return {"status": "resource_wait", "verifier_resource_wait": proof}

    path = tmp_path / "queue.json"
    receipt = queue.run_round(
        path,
        [target],
        observe,
        forged,
        deadline=time.time() + 20,
        receipt_reserve_seconds=0,
        max_passes=1,
        max_workers=1,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )
    assert receipt["stop_valid"] is False
    operation = queue._read_state(path)["items"][target]["operation"]
    assert operation["status"] == "uncertain"
    assert operation["error_type"] == "ValueError"
    assert receipt["accounting"][0]["reason"] == "operation_uncertain"


def test_foreign_same_basename_module_cannot_self_authenticate_native_wait():
    import types

    import closer_queue as queue

    foreign = types.ModuleType("foreign_adapter")
    foreign.__file__ = native.__file__
    foreign.bounded_run = native.bounded_run
    exec(
        compile("class Closer:\n    def execute(self,*args): return {}\n", native.__file__, "exec"),
        vars(foreign),
    )
    backend = foreign.Closer()
    backend.offload_fn = None
    backend.run_fn = native.bounded_run
    backend.gh = types.SimpleNamespace(api_fn=native.gh_capacity.gh_run)
    backend.runtime_binding = None
    backend.require_runtime_binding = False
    assert queue._native_verify_wait_executor(backend.execute) is False


@pytest.mark.parametrize("linked", [[], [{"number": 858}]])
def test_unrelated_active_repository_run_does_not_create_target_dependency(
    waiting_world, capsys, linked
):
    w = waiting_world
    w.blocking_run["pull_requests"] = linked
    assert native.main(w.argv) == 78
    capsys.readouterr()
    assert len(w.verifies) == 1 and len(w.merges) == 1
