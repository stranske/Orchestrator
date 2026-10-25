"""An invoked guard refusal is a known no-merge boundary, not an unstarted callback."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_dispatcher import (
    closer,
    no_live_state_or_provider,  # noqa: F401
    snapshot,
)
from test_closer_finite_reentry import TARGET, read
from test_closer_prequeue_reconciliation import boundary as prequeue_boundary  # noqa: F401
from test_closer_prequeue_reconciliation import write
from test_closer_prewrite_reconciliation import apply, plan, prewrite  # noqa: F401

import closer_dispatcher as adapter
import closer_finite_reentry as retry
import closer_queue as queue
import closer_resources as resources
import merge_guard

_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


def refusal(target=TARGET, *, modern=False):
    def forbidden(*_a, **_k):
        raise AssertionError("a preflight refusal cannot call merge or downstream work")

    result = merge_guard.guarded_merge(
        target,
        expected_head="a" * 40,
        confirm_merge=True,
        preflight_fn=lambda *_a, **_k: {"blocked": True, "reason": "current exact-head gate"},
        merge_fn=forbidden,
        metadata_fn=forbidden,
    )
    if not modern:
        result.pop("no_merge_command", None)  # exact legacy924 producer shape
    return result


@pytest.fixture
def merge_boundary(request, monkeypatch):
    case = None
    if getattr(request, "param", False):
        import sys

        import test_closer_finite_reentry
        import test_closer_prequeue_reconciliation
        import test_closer_prewrite_reconciliation

        case = json.loads(
            (Path(__file__).parent / "fixtures/native_merge_guard_refusal_1012.json").read_text()
        )
        for mod in (
            sys.modules[__name__],
            test_closer_finite_reentry,
            test_closer_prequeue_reconciliation,
            test_closer_prewrite_reconciliation,
        ):
            monkeypatch.setattr(mod, "TARGET", case["target"])
    value = request.getfixturevalue("prewrite")
    path, frontier, doc, receipts, module, inner = value
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    prep = row["results"]["prepare"]
    snap = retry._receipt(prep["snapshot_ref"])
    snap.pop("merge", None)
    if case:
        # Replay original protocol fields in private fixture paths. Source pins
        # below are inert fixture pins; actual installed provenance is separately
        # checked by the retained-source read-only plan, never fabricated here.
        actual = case["bucket"]["items"][TARGET]["operation"]
        snap.update(head=actual["binding"]["head"], revision=actual["revision"])
        row.update(case["outer_boundary"])
        frontier.clear()
        frontier.update(copy.deepcopy(case["frontier"]))
        entry = doc["frontiers"].pop("frozen")
        entry["finite_frontier"] = copy.deepcopy(frontier)
        entry["run_id"] = retry.pipeline._digest(
            ["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]]
        )[:32]
        doc["frontiers"][frontier["frontier_id"]] = entry
        prep.update(revision=row["revision"])
        prep["native_effect_contract"].update(revision=row["revision"], head=snap["head"])
        lease = json.loads((path.parent / "lease.json").read_text())
        lease["finite_frontier"] = frontier
        lease["target"] = resources.canonical_target(TARGET)
        row["scope_lease"]["target"] = resources.canonical_target(TARGET)
        write(path.parent / "lease.json", lease)
    prep["snapshot_ref"] = write(Path(prep["snapshot_ref"]["path"]), snap)
    prep["native_effect_contract"]["action"] = "merge"
    prep["proposal"]["action"] = "merge"
    row["results"]["interpret"]["classification"] = "deterministic_merge"
    write(path, doc)
    proof_path = next((path.parent.parent.parent / "scope-admissions").glob("*/*.json"))
    proof = json.loads(proof_path.read_text())
    proof["input_sha256"] = retry._digest(row["results"])
    if case:
        proof["binding"]["revision"] = row["revision"]
        proof["binding"]["pipeline_run_id"] = entry["run_id"]
        proof["observed_at"] = row["stage_started_at"] + 0.01
    write(proof_path, proof)
    module.write_text("# immutable legacy adapter fixture\n")
    (module.parent / "merge_guard.py").write_text("# immutable preflight return fixture\n")
    for name, attr in {
        "closer_dispatcher.py": "MERGE_GUARD_SOURCE_SHA256",
        "closer_queue.py": "MERGE_GUARD_QUEUE_SHA256",
        "closer_pipeline.py": "MERGE_GUARD_PIPELINE_SHA256",
        "merge_guard.py": "MERGE_GUARD_SCRIPT_SHA256",
    }.items():
        monkeypatch.setattr(
            retry, attr, hashlib.sha256((module.parent / name).read_bytes()).hexdigest()
        )
    installation = retry._receipt(receipts["installation"])
    installation["candidate_sha"] = retry.MERGE_GUARD_CANDIDATE
    installation["verification"]["snapshot_digest"] = retry._snapshot_digest(module.parent)
    receipts["installation"] = write(Path(receipts["installation"]["path"]), installation)
    lifecycle = retry._receipt(receipts["lifecycle"])
    argv = lifecycle["source_identity"].pop("logical_command")
    if case:
        argv[argv.index("--finite-frontier-json") + 1] = json.dumps(frontier)
        lifecycle["source_identity"]["frontier"] = frontier
        lifecycle["argv_sha256"] = hashlib.sha256(
            json.dumps([argv[0], str(module), *argv[2:]]).encode()
        ).hexdigest()
    lifecycle["source_identity"].pop("installed_generation")
    readback = {
        "installed_source": installation["candidate_sha"],
        "installed_generation": installation["installed_generation"],
        "snapshot_digest": installation["verification"]["snapshot_digest"],
        "mirror_target": module.parent.name,
        "matches_strict_verified_payload": True,
    }
    ref = write(module.parent.parent / "readback.json", readback)
    lifecycle["source_identity"].update(
        installed_receipt=ref["path"],
        installed_receipt_sha256=ref["sha256"],
        snapshot_digest=readback["snapshot_digest"],
    )
    receipts["invocation"] = write(module.parent.parent / "invocation.json", {"command": argv})
    stdout = retry._receipt(
        {"path": lifecycle["native_stdout"], "sha256": lifecycle["stdout_sha256"]}
    )
    stdout["targets"][TARGET] = copy.deepcopy(row)
    if case:
        stdout["finite_frontier"] = frontier
        stdout["run_ids"][TARGET] = entry["run_id"]
    lifecycle["stdout_sha256"] = write(Path(lifecycle["native_stdout"]), stdout)["sha256"]
    receipts["lifecycle"] = write(Path(receipts["lifecycle"]["path"]), lifecycle)
    data = json.loads(inner.read_text())
    data.setdefault("round", {})
    bucket = data["frontiers"]["frozen"]
    op = bucket["items"][TARGET]["operation"]
    op.update(action="merge", outcome="blocked", result={"result": refusal()})
    op["binding"]["merge"] = None
    bucket["round"]["events"][0]["action"] = "merge"
    bucket["round"]["receipt"]["accounting"][0]["action"] = "merge"
    if case:
        data["frontiers"][frontier["frontier_id"]] = copy.deepcopy(case["bucket"])
        data["frontiers"].pop("frozen", None)
    write(inner, data)
    return value


def test_authenticated_guard_refusal_retires_only_owner_without_replaying(merge_boundary):
    path, _frontier, doc, _refs, _module, inner = merge_boundary
    root = path.parent.parent.parent
    own = resources._key_dir(root, "pr:" + TARGET) / "intent.json"
    foreign = resources._key_dir(root, "branch:owner/repo@other") / "intent.json"
    write(own, {"key": "pr:" + TARGET, "target": TARGET, "owner": "terminal", "state": "unknown"})
    write(
        foreign,
        {
            "key": "branch:owner/repo@other",
            "target": TARGET,
            "owner": "foreign",
            "state": "unknown",
        },
    )
    old_inner, old_foreign = inner.read_bytes(), foreign.read_bytes()
    proposal = plan(merge_boundary)
    assert proposal["kind"] == "native_merge_guard_refusal"
    assert apply(merge_boundary, proposal)["status"] == "applied"
    row = read(path)
    assert row["status"] == "held" and row["stage_status"] == "completed"
    assert row["resource_evidence"]["confirmed_no_effect"] is True
    assert "confirmed_not_started" not in row["resource_evidence"]
    assert "reentry_ready" not in row
    assert row["results"] == doc["frontiers"]["frozen"]["items"][TARGET]["results"]
    assert row["reentry_history"][0]["previous"] == doc["frontiers"]["frozen"]["items"][TARGET]
    assert inner.read_bytes() == old_inner and foreign.read_bytes() == old_foreign
    assert not own.exists()
    assert apply(merge_boundary, proposal)["idempotent"] is True


@pytest.mark.parametrize(
    "field",
    ["attempted", "malformed", "target", "revision", "head", "receipt", "history", "uncertain"],
)
def test_guard_legacy_proof_refuses_ambiguous_effects(merge_boundary, field):
    _path, _frontier, _doc, _refs, _module, inner = merge_boundary
    data = json.loads(inner.read_text())
    bucket = data["frontiers"]["frozen"]
    item = bucket["items"][TARGET]
    op = item["operation"]
    report = op["result"]["result"]
    if field == "attempted":
        report.update(merge_executed=True, merge_returncode=1)
    elif field == "malformed":
        report["merge_executed"] = 0
    elif field == "target":
        report["target"] = "foreign/repo#99"
    elif field == "revision":
        op["revision"] = "other"
    elif field == "head":
        op["binding"]["head"] = "c" * 40
    elif field == "receipt":
        bucket["round"]["receipt"]["valid"] = True
    elif field == "history":
        item["operation_history"] = [{"status": "completed"}]
    else:
        op["outcome_uncertain"] = True
    write(inner, data)
    before = inner.read_bytes()
    with pytest.raises(ValueError):
        plan(merge_boundary)
    assert inner.read_bytes() == before


@pytest.mark.parametrize("which", ["invocation", "readback", "source", "guard"])
def test_guard_legacy_source_join_is_exact(merge_boundary, which):
    _path, _frontier, _doc, refs, module, _inner = merge_boundary
    if which == "invocation":
        p = Path(refs["invocation"]["path"])
        d = json.loads(p.read_text())
        d["command"].append("--unsafe")
        refs["invocation"] = write(p, d)
    elif which == "readback":
        life = retry._receipt(refs["lifecycle"])
        Path(life["source_identity"]["installed_receipt"]).write_text("{}")
    else:
        (module if which == "source" else module.parent / "merge_guard.py").write_text("changed")
    with pytest.raises(ValueError):
        plan(merge_boundary)


def native_refusal(tmp_path, monkeypatch, *, trusted=True):
    current = snapshot()
    backend = closer(tmp_path, current, apply=True, workers=True)
    # Real adapter and real queue, hermetic transport boundary only.
    backend.select = lambda _snap: ("merge", "eligible", True, None)
    calls = []

    def run_guard(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout=json.dumps(refusal("o/r#7", modern=True)))

    monkeypatch.setattr(adapter, "bounded_run", run_guard)
    backend.run_fn = run_guard
    backend.gh.api_fn = adapter.gh_capacity.gh_run
    if not trusted:
        backend.run_fn = lambda *a, **kw: run_guard(*a, **kw)
    observed = backend.observe("o/r#7")
    path = tmp_path / "queue.json"
    receipt = queue.run_round(
        path,
        ["o/r#7"],
        backend.observe,
        backend.execute,
        deadline=time.time() + 60,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )
    return receipt, queue._read_state(path), calls, observed


def test_non_remote_native_adapter_does_not_discharge_local_guard_work(tmp_path, monkeypatch):
    receipt, state, calls, _observed = native_refusal(tmp_path, monkeypatch)
    assert len(calls) == 1
    assert receipt["stop_valid"] is False
    assert state["items"]["o/r#7"]["deferred_reason"] == "operation_failed"
    assert queue.merge_guard_refusal_proof(state["items"]["o/r#7"]["operation"], "o/r#7") is None


def test_foreign_executor_cannot_assert_no_merge_boundary(tmp_path, monkeypatch):
    receipt, state, calls, _observed = native_refusal(tmp_path, monkeypatch, trusted=False)
    assert len(calls) == 1
    assert receipt["stop_valid"] is False
    assert state["items"]["o/r#7"]["operation"]["status"] == "failed"


def exercise_public_native_chain(merge_boundary, tmp_path, monkeypatch, capsys, *, mode="refused"):
    """Real plan/CAS/retirement -> CLI/census/leases -> adapter/queue/guard hold.

    The retained protocol is the #1012 failure shape. Synthetic source pins and
    transport are fixture-only; no subprocess or provider is launched.
    """
    from test_closer_dispatcher_finite_frontier import command

    import closer_frontier_policy as frontiers
    import closer_pipeline as pipeline

    path, old_frontier, _doc, _refs, _module, inner = merge_boundary
    for name in ("HOME", "ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME", "HANDOFF_DIR", "ORCH_FLEET_ROOT"):
        monkeypatch.setenv(name, str(tmp_path / ("private-" + name)))
    raw = tmp_path / "queue"
    ledger = tmp_path / "queue.pipeline.json"
    raw.write_text(json.dumps({"schema": queue.SCHEMA, "items": {}, "round": {}}))
    foreign = {
        f"foreign/repo{i}#1": {
            "status": "unknown",
            "stage": "act",
            "stage_status": "unknown",
            "revision": "old",
            "results": {},
        }
        for i in range(23)
    }
    import test_closer_native_admission_classification as prior_native
    from test_closer_read_barrier_remote_scope import read_barrier_fixture

    original_prepared = prior_native.prepared

    def prepare_source_issue(*args, **kwargs):
        source = TARGET.split("#")[0] + "#948"
        kwargs["snapshot_overrides"] = {
            "target": source,
            "pr_target": TARGET,
            "head_repository": TARGET.split("#")[0],
            "sources": [
                {
                    "target": source,
                    "body": "## Tasks\n- Preserve original source obligations",
                    "state": "open",
                    "labels": [],
                }
            ],
        }
        values = original_prepared(*args, **kwargs)
        origin_path, _raw, snap, prior_row, prior_doc, *_rest = values
        prior_doc["runs"]["original"]["targets"] = {snap["target"]: prior_row}
        write(origin_path, prior_doc)
        return values

    monkeypatch.setattr(prior_native, "prepared", prepare_source_issue)
    prior = tmp_path / "earlier-read-barrier"
    barrier_path, barrier_inner, _barrier_row, global_doc, _barrier_target = read_barrier_fixture(
        prior, monkeypatch
    )
    rebound_inner = ledger.parent / f"{ledger.stem}.actions" / barrier_inner.name
    rebound_inner.parent.mkdir(parents=True, exist_ok=True)
    rebound_inner.write_bytes(barrier_inner.read_bytes())
    prior_receipt_bytes = rebound_inner.read_bytes()
    for name, kind in (("snapshot_ref", "snapshots"), ("packet_ref", "manifests")):
        ref = _barrier_row["results"]["prepare"][name]
        dest = Path(str(raw) + ".evidence") / kind / Path(ref["path"]).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(ref["path"]).read_bytes())
        ref["path"] = str(dest)
    global_doc["runs"]["retained-foreign-unknowns"] = {
        "created": 1,
        "deadline": 2,
        "assignment": sorted(foreign),
        "targets": foreign,
        "carry_targets": [],
    }
    write(ledger, global_doc)
    # The scoped fixture's old historical namespace is intentionally empty;
    # actual foreign unresolved operations live in the retained registered ledger.
    old = json.loads(path.read_text())
    old["runs"] = {}
    write(path, old)
    # This changes the stdout-bound document only outside its exact target row.
    resources.open_root(ledger, pipeline_schema=pipeline.SCHEMA)
    before_global = ledger.read_bytes()
    retained_inner = inner.read_bytes()
    current = snapshot(
        target=TARGET,
        head=(
            json.loads(
                (
                    Path(__file__).parent / "fixtures/native_merge_guard_refusal_1012.json"
                ).read_text()
            )["current_head"]
            if TARGET == "stranske/Inv-Man-Intake#1012"
            else "c" * 40
        ),
        state="OPEN",
        kind="pr",
        draft=False,
        head_branch="feature",
        head_repository=TARGET.split("#")[0],
        sources=[
            {
                "target": TARGET.split("#")[0] + "#948",
                "state": "open",
                "body": "## Tasks\n- Preserve feature behavior",
                "labels": [],
            }
        ],
    )
    case = None
    if TARGET == "stranske/Inv-Man-Intake#1012":
        case = json.loads(
            (Path(__file__).parent / "fixtures/native_merge_guard_refusal_1012.json").read_text()
        )
        current = copy.deepcopy(case["current_snapshot"])
    monkeypatch.setattr(adapter.GitHub, "snapshot", lambda *_: copy.deepcopy(current))

    # Only metadata transport is replaced. Real CLI, coordinator, queue,
    # registered callbacks, pool accounting and target lease remain active.
    def gh(args, **_kwargs):
        assert args[:3] == ["api", "--method", "GET"], args
        if "/compare/" in args[3]:
            return 0, {"status": "ahead", "files": []}
        if args[3] == "graphql":
            return 0, {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            }
        raise AssertionError("unexpected metadata request: " + repr(args))

    monkeypatch.setattr(adapter.gh_capacity, "gh_run", gh)
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: None)
    monkeypatch.setattr(adapter.Closer, "campaign_admission", lambda *_a, **_k: {"allowed": True})
    calls = []
    guard_reads = []
    guard_reports = []
    guard_clock = [time.time()]
    guard_anchor = guard_clock[0] - (60 if mode == "floor" else 1000)
    guard_probes = []

    def inert_guard(argv, **_kwargs):
        assert (
            "--expected-head" in argv and argv[argv.index("--expected-head") + 1] == current["head"]
        )
        assert "--remote-only" in argv
        calls.append(argv)
        if mode == "uncertain":
            raise TimeoutError("guard transport lost after invocation")
        from datetime import datetime, timezone

        import exact_head_merge_gate

        stamp = datetime.fromtimestamp(guard_anchor, timezone.utc).isoformat()
        graph_reads = 0

        def recorded_github(command, **_kw):
            nonlocal graph_reads
            guard_reads.append(command)
            if command[:3] == ["gh", "api", "graphql"]:
                graph_reads += 1
                pr = {
                    "id": "recorded1012",
                    "state": "OPEN",
                    "isDraft": False,
                    "headRefOid": current["head"],
                    "headRefName": current["head_branch"],
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": (
                        "CLEAN"
                        if graph_reads == 1 or mode in {"attempted", "floor", "success"}
                        else "BEHIND"
                    ),
                    "createdAt": stamp,
                    "updatedAt": stamp,
                    "headRepository": {
                        "id": "recorded-repository",
                        "nameWithOwner": current["head_repository"],
                    },
                    "headRef": {"target": {"oid": current["head"]}},
                    "readyForReview": {"nodes": []},
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": current["head"],
                                    "statusCheckRollup": {"state": "SUCCESS"},
                                }
                            }
                        ]
                    },
                    "reviewThreads": {
                        "totalCount": 0,
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                }
                payload = {"data": {"repository": {"pullRequest": pr}}}
            elif command[:4] == ["gh", "api", "-X", "GET"]:
                payload = [
                    {
                        "ref": "refs/heads/" + current["head_branch"],
                        "after": current["head"],
                        "timestamp": stamp,
                        "activity_type": "push",
                    }
                ]
            elif command[:3] == ["gh", "pr", "view"]:
                payload = {
                    "title": current.get("title", "feature"),
                    "labels": current["labels"],
                    "state": "OPEN",
                    "isDraft": False,
                    "mergeStateStatus": "CLEAN",
                    "closingIssuesReferences": [],
                    "headRefName": current["head_branch"],
                }
            else:
                raise AssertionError("unexpected guard transport: " + repr(command))
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        def no_final_post(command, **_kw):
            if mode in {"attempted", "success", "floor"}:
                assert command[command.index("--match-head-commit") + 1] == current["head"]
                assert command[:3] == ["gh", "pr", "merge"]
                # Inert final mutation transport. A failed real POST could have
                # taken effect, so this must never receive no-effect authority.
                if mode != "attempted":
                    current.update(state="MERGED", merge="d" * 40)
                    current["revision"] = adapter.digest(
                        {k: v for k, v in current.items() if k != "revision"}
                    )
                return SimpleNamespace(
                    returncode=1 if mode == "attempted" else 0,
                    stdout="",
                    stderr="inert command only",
                )
            raise AssertionError("final changed-state preflight must prevent gh pr merge")

        monkeypatch.setattr(merge_guard, "_admission_read", recorded_github)
        monkeypatch.setattr(merge_guard, "_admission_clock", lambda: guard_clock[0])
        guard_probes.append(
            lambda: exact_head_merge_gate.snapshot(
                TARGET,
                expected_head=current["head"],
                run_fn=recorded_github,
                now_fn=lambda: guard_clock[0],
            )
        )
        report = merge_guard.guarded_merge(
            TARGET,
            expected_head=current["head"],
            confirm_merge=True,
            remote_only=True,
            skip_immediate_advisory=True,
            spec_dir=tmp_path / "private-specs",
            preflight_fn=lambda target, **kw: exact_head_merge_gate.snapshot(
                target, run_fn=recorded_github, now_fn=lambda: guard_clock[0], **kw
            ),
            metadata_fn=lambda target: merge_guard.pr_metadata(target, run_fn=recorded_github),
            merge_fn=no_final_post,
            remote_runs_fn=lambda *_: [],
            record_outcome_fn=lambda *_a, **_k: pytest.fail("no synthetic outcome publication"),
        )
        if mode == "floor" and report["blocked"]:

            assert report["preflight"]["review_floor_remaining_seconds"] > 0
            guard_reports.append(report)
            return SimpleNamespace(returncode=1, stdout=json.dumps(report))
        assert report["final_preflight"]["blocked"] is (
            mode not in {"attempted", "success", "floor"}
        )
        assert report["merge_executed"] is (mode in {"attempted", "success", "floor"})
        assert ("no_merge_command" in report) is (mode not in {"attempted", "success", "floor"})
        assert graph_reads == 2
        assert report["review_loop"].get("required") is False
        assert report["remote_only_guard"]["runtime_ac_required"] is False
        guard_reports.append(report)
        return SimpleNamespace(returncode=1, stdout=json.dumps(report))

    monkeypatch.setattr(adapter, "bounded_run", inert_guard)
    args, _selected, _assignment = command(tmp_path, (TARGET,))
    if case:
        _assignment.write_text(json.dumps(case["canonical_assignment"]))
    args[args.index("--state") + 1] = str(raw)
    observer = adapter.Closer(
        adapter.GitHub(deadline=time.time() + 25), state_path=raw, apply=True, workers=True
    )
    new_frontier = frontiers.make_frontier_from_observations(
        {TARGET: observer.observe(TARGET)}, owner="new-state-owner", frontier_id="new-current-state"
    )
    args[args.index("--finite-frontier-json") + 1] = json.dumps(new_frontier)
    # The old UNKNOWN is a genuine fence until its exact proof is applied.
    assert adapter.main(args) == 78
    initial = json.loads(capsys.readouterr().out)
    assert initial.get("run_ids") == {}, initial
    assert calls == []
    proposal = plan(merge_boundary)
    assert apply(merge_boundary, proposal)["status"] == "applied"
    assert inner.read_bytes() == retained_inner
    assert ledger.read_bytes() == before_global
    if mode == "foreign":
        intent = (
            resources._key_dir(
                resources.scoped_root(ledger), "repo:" + TARGET.split("#")[0].lower()
            )
            / "legacy-intent.json"
        )
        write(
            intent,
            {
                "key": "repo:" + TARGET.split("#")[0].lower(),
                "target": TARGET,
                "owner": "foreign-uncertain-owner",
                "state": "unknown",
            },
        )
        original_intent = intent.read_bytes()
        assert adapter.main(args) == 78
        blocked = json.loads(capsys.readouterr().out)
        assert blocked["run_ids"] == {} and calls == []
        assert intent.read_bytes() == original_intent
        assert inner.read_bytes() == retained_inner and ledger.read_bytes() == before_global
        return
    assert adapter.main(args) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1, {
        "targets": {
            k: {
                q: v.get(q)
                for q in ["stage", "status", "error", "deferred_reason", "deferred_next_trigger"]
            }
            for k, v in result["targets"].items()
        },
        "initial": result.get("initial_scope_refusal"),
        "validation": result.get("validation", {}).get("reasons"),
    }
    row = result["targets"][TARGET]
    if mode == "success":
        assert row["stage"] == "act" and row["status"] == "held"
        assert row["results"]["act"]["status"] == "completed"
        successors = result.get("merge_verification_successors", {})
        assert successors[TARGET]["status"] == "verification_successor", successors
        assert successors[TARGET]["lineage"]["execution_targets"] == [TARGET]
        assert ledger.read_bytes() == before_global
        return
    if mode in {"uncertain", "attempted"}:
        assert row["status"] == "unknown" and row["stage"] == "act", row
        assert result["scoped"]["unknown_reservations"]
        assert adapter.main(args) == 78
        capsys.readouterr()
        assert len(calls) == 1
        assert ledger.read_bytes() == before_global
        return
    assert row["status"] == "held" and row["stage"] == "act", result
    assert row["deferred_reason"] == "merge_guard_refused", row
    assert result["scoped"]["unknown_reservations"] == {}
    assert result["blocking_reservations"] == 0
    assert len(guard_reports) == 1 and len(guard_reads) >= (2 if mode == "floor" else 4)
    assert guard_reports[0]["no_merge_command"]["expected_head"] == current["head"]
    stop = pipeline.validate_stop(
        path,
        [TARGET],
        finite_frontier=new_frontier,
        frontier_target=TARGET,
        expected_run_id=result["run_ids"][TARGET],
    )
    assert stop["valid"] is True, stop
    assert inner.read_bytes() != retained_inner  # new frontier appends, old bucket intact
    assert (
        json.loads(inner.read_text())["frontiers"][old_frontier["frontier_id"]]
        == json.loads(retained_inner)["frontiers"][old_frontier["frontier_id"]]
    )
    assert ledger.read_bytes() == before_global
    assert rebound_inner.read_bytes() == prior_receipt_bytes
    assert json.loads(ledger.read_text())["runs"]["retained-foreign-unknowns"]["targets"] == foreign
    # Repeating the exact completed hold cannot invoke the guard again.
    assert adapter.main(args) == 78
    capsys.readouterr()
    assert len(calls) == 1
    if mode == "floor":
        queue_before = json.loads(inner.read_text())
        hot = queue_before["frontiers"][new_frontier["frontier_id"]]["items"][TARGET]
        history = []
        for number in range(queue.MAX_OPERATION_HISTORY):
            old = copy.deepcopy(hot["operation"])
            old["id"] = "prior-guard-" + str(number)
            old["result"]["merge_guard_refusal"]["operation_id"] = old["id"]
            history.append(old)
        hot["operation_history"] = history
        write(inner, queue_before)
        guard_clock[0] += 421
        assert guard_probes[-1]()["blocked"] is False  # real gate now admits same head
        before = path.read_bytes(), inner.read_bytes()
        captured_current = copy.deepcopy(current)
        for changed in (
            {"checks": "PENDING"},
            {"active_threads": 1, "review_revision": "new-review"},
            {"head": "e" * 40},
        ):
            current.update(changed)
            current["revision"] = "e" * 64
            with pytest.raises(
                ValueError, match="guard_readmission_requires_fresh_immutable_binding"
            ):
                retry.plan(
                    path,
                    frontier=new_frontier,
                    target=TARGET,
                    event="changed-observation",
                    assignment_path=_assignment,
                )
            current.clear()
            current.update(copy.deepcopy(captured_current))
        saved_assignment = _assignment.read_bytes()
        assignment_doc = json.loads(saved_assignment)
        assignment_doc["targets"][0]["execution_state"] = "paused"
        try:
            _assignment.write_text(json.dumps(assignment_doc))
            with pytest.raises(ValueError, match="guard_readmission_assignment_changed"):
                retry.plan(
                    path,
                    frontier=new_frontier,
                    target=TARGET,
                    event="not-authorized",
                    assignment_path=_assignment,
                )
        finally:
            _assignment.write_bytes(saved_assignment)
        assert (path.read_bytes(), inner.read_bytes()) == before
        with pytest.raises(ValueError, match="guard_readmission_requires_current_assignment"):
            retry.plan(path, frontier=new_frontier, target=TARGET, event="clock-passed")
        # Interrupt apply after its durable prepared audit but before outer-row
        # commit, then resume the SAME public API/plan under the same lease.
        real_apply = retry.apply
        real_save = retry.pipeline._save_snapshot
        interrupted = []

        def apply_with_one_interruption(*aa, **kw):
            if not interrupted and str(kw.get("event", "")).startswith("guard-admission-"):

                def stop_before_commit(location, document):
                    candidate = document["frontiers"][new_frontier["frontier_id"]]["items"][TARGET]
                    if candidate.get("reentry_ready", {}).get("kind") == "native_guard_readmission":
                        interrupted.append(True)
                        raise OSError("inert interruption after prepared audit")
                    return real_save(location, document)

                with monkeypatch.context() as local:
                    local.setattr(retry.pipeline, "_save_snapshot", stop_before_commit)
                    with pytest.raises(OSError, match="inert interruption"):
                        real_apply(*aa, **kw)
            return real_apply(*aa, **kw)

        real_queue_readmission = retry.guard_queue_readmission
        consumed = []

        def inspect_live_queue_readmission(context, action_path, selected, target, digest):
            assert real_queue_readmission(context, action_path, selected, target, digest) is True
            malformed = copy.deepcopy(context)
            malformed["guard_readmission"]["plan_sha256"] = "0" * 64
            assert real_queue_readmission(malformed, action_path, selected, target, digest) is False

            # A genuine observed epoch drift invalidates the live lease even
            # if bytes are restored; never continue that owner after a stale
            # fence. Isolate this one decision seam instead of editing its lease.
            def stale_fence():
                raise resources.ResourceBlocked("inert stale epoch")

            with monkeypatch.context() as check:
                check.setattr(resources, "fence_for", lambda _path: stale_fence)
                assert (
                    real_queue_readmission(context, action_path, selected, target, digest) is False
                )
            isolated = tmp_path / "foreign-reader.json"
            isolated.write_text(
                json.dumps(
                    {
                        "context": context,
                        "inner": str(action_path),
                        "frontier": selected,
                        "target": target,
                        "sha": digest,
                    }
                )
            )
            code = "import json,sys; import closer_finite_reentry as r; d=json.load(open(sys.argv[1])); print(r.guard_queue_readmission(d['context'],d['inner'],d['frontier'],d['target'],d['sha']))"
            with monkeypatch.context() as private_reader:
                private_reader.setattr(subprocess, "Popen", _REAL_POPEN)
                child = _REAL_SUBPROCESS_RUN(
                    [sys.executable, "-c", code, str(isolated)],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": str(Path(retry.__file__).parent),
                    },
                    timeout=10,
                )
            assert child.returncode == 0 and child.stdout.strip() == "False", child.stderr
            assert (
                resources.acquire(
                    resources.scoped_root(ledger),
                    target,
                    owner="competing-native-owner",
                    finite_frontier=selected,
                    request_deadline=time.time(),
                )
                is None
            )
            consumed.append(copy.deepcopy(context))
            return True

        with monkeypatch.context() as local:
            local.setattr(retry, "apply", apply_with_one_interruption)
            local.setattr(retry, "guard_queue_readmission", inspect_live_queue_readmission)
            assert adapter.main(args) == 78
        assert interrupted == [True] and len(consumed) == 1
        resumed = json.loads(capsys.readouterr().out)
        assert len(calls) == 2, resumed.get("merge_verification_successors", {})
        continuation = resumed["merge_verification_successors"][TARGET]
        assert continuation["status"] == "verification_successor"
        native = continuation["guard_readmission"]["native_result"]
        assert native["targets"][TARGET]["results"]["act"]["status"] == "completed"
        assert continuation["lineage"]["execution_targets"] == [TARGET]
        new_queue = json.loads(inner.read_text())
        assert (
            len(
                new_queue["frontiers"][new_frontier["frontier_id"]]["items"][TARGET][
                    "operation_history"
                ]
            )
            == queue.MAX_OPERATION_HISTORY
        )
        audit = json.loads(Path(continuation["guard_readmission"]["audit"]["audit"]).read_text())
        saved_queue = json.loads(Path(audit["inner_before"]["path"]).read_text())
        assert (
            len(
                saved_queue["frontiers"][new_frontier["frontier_id"]]["items"][TARGET][
                    "operation_history"
                ]
            )
            == queue.MAX_OPERATION_HISTORY
        )
        assert (
            saved_queue["frontiers"][new_frontier["frontier_id"]]["items"][TARGET]["operation"]
            == hot["operation"]
        )
        assert (
            new_queue["frontiers"][old_frontier["frontier_id"]]
            == json.loads(before[1])["frontiers"][old_frontier["frontier_id"]]
        )
        retained = json.loads(before[0])
        fresh_state = json.loads(path.read_text())
        assert (
            fresh_state["frontiers"][old_frontier["frontier_id"]]
            == retained["frontiers"][old_frontier["frontier_id"]]
        )
        fresh_row = fresh_state["frontiers"][new_frontier["frontier_id"]]["items"][TARGET]
        assert fresh_row["status"] == "held"
        assert fresh_row["results"]["act"]["status"] == "completed"
        assert fresh_row["results"]["prepare"] == row["results"]["prepare"]
        assert fresh_row["reentry_history"][-1]["previous"]["results"] == row["results"]


def test_actual_1012_inner_guard_refusal_protocol_is_accepted_without_rewrite(tmp_path):
    case = json.loads(
        (Path(__file__).parent / "fixtures/native_merge_guard_refusal_1012.json").read_text()
    )
    target, frontier = case["target"], case["frontier"]
    path = tmp_path / "state.json"
    inner = tmp_path / "state.actions" / (retry.pipeline._digest(target) + ".json")
    write(
        inner,
        {
            "schema": queue.SCHEMA,
            "items": {},
            "round": {},
            "frontiers": {frontier["frontier_id"]: case["bucket"]},
        },
    )
    operation = case["bucket"]["items"][target]["operation"]
    snap = {
        "target": target,
        "revision": operation["revision"],
        "head": operation["binding"]["head"],
    }
    row = {
        **case["outer_boundary"],
        "results": {"prepare": {"snapshot_ref": write(tmp_path / "snapshot.json", snap)}},
    }
    before = inner.read_bytes()
    proof = retry._native_prewrite_inner(
        path, frontier, target, row, contract=retry._prewrite_contract(retry.MERGE_GUARD_CANDIDATE)
    )
    assert proof["operation_id"] == "e87296f5cc0f077df9cca54f2f76c167"
    assert proof["next_trigger"] == "refresh_merge_guard_before_action"
    assert inner.read_bytes() == before


def test_guard_reconciliation_then_public_native_frontier_preserves_foreign_fences(
    merge_boundary, tmp_path, monkeypatch, capsys
):
    exercise_public_native_chain(merge_boundary, tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("merge_boundary", [True], indirect=True)
def test_retained_1012_reconstruction_through_public_native_chain(
    merge_boundary, tmp_path, monkeypatch, capsys
):
    exercise_public_native_chain(merge_boundary, tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("merge_boundary", [True], indirect=True)
@pytest.mark.parametrize("mode", ["foreign", "uncertain", "attempted"])
def test_retained_1012_chain_preserves_real_foreign_and_uncertain_fences(
    merge_boundary, tmp_path, monkeypatch, capsys, mode
):
    exercise_public_native_chain(merge_boundary, tmp_path, monkeypatch, capsys, mode=mode)


def test_foreign_callback_cannot_forge_typed_guard_boundary(tmp_path):
    target = "owner/repo#17"

    def observe(_):
        return {
            "target": target,
            "action": "merge",
            "eligible": True,
            "revision": "revision",
            "operation_binding": {"head": "a" * 40, "merge": None, "pr_target": target},
        }

    def forged(_target, _action, _snapshot, operation):
        report = refusal(target, modern=True)
        return {
            "status": "blocked",
            "result": report,
            "merge_guard_refusal": {
                "schema": "closer-merge-guard-refusal/v1",
                "target": target,
                "subject_pr": target,
                "head": "a" * 40,
                "revision": operation["revision"],
                "operation_id": operation["id"],
                "report_sha256": queue._digest(report),
                "callback_invoked": True,
                "merge_command_invoked": False,
                "native_remote_only": True,
            },
        }

    path = tmp_path / "queue.json"
    receipt = queue.run_round(
        path,
        [target],
        observe,
        forged,
        deadline=time.time() + 20,
        max_workers=1,
        max_passes=1,
        receipt_reserve_seconds=0,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )
    assert receipt["stop_valid"] is False
    assert queue._read_state(path)["items"][target]["operation"]["status"] == "uncertain"


@pytest.mark.parametrize("merge_boundary", [True], indirect=True)
def test_timed_floor_requires_supported_readmission_not_bare_event(
    merge_boundary, tmp_path, monkeypatch, capsys
):
    exercise_public_native_chain(merge_boundary, tmp_path, monkeypatch, capsys, mode="floor")


@pytest.mark.parametrize("merge_boundary", [True], indirect=True)
def test_actual1012_inert_success_reaches_bound_verification_successor(
    merge_boundary, tmp_path, monkeypatch, capsys
):
    exercise_public_native_chain(merge_boundary, tmp_path, monkeypatch, capsys, mode="success")


@pytest.mark.parametrize("change", ["head", "phase", "attempted", "remote", "hash"])
def test_modern_guard_marker_tampering_never_grants_no_effect(change):
    target = "owner/repo#1"
    report = refusal(target, modern=True)
    op = {
        "id": "op",
        "revision": "revision",
        "action": "merge",
        "status": "failed",
        "outcome": "blocked",
        "binding": {"pr_target": target, "head": "a" * 40, "merge": None},
        "result": {"result": report},
    }
    op["result"]["merge_guard_refusal"] = {
        "schema": "closer-merge-guard-refusal/v1",
        "target": target,
        "subject_pr": target,
        "head": "a" * 40,
        "revision": "revision",
        "operation_id": "op",
        "report_sha256": queue._digest(report),
        "callback_invoked": True,
        "merge_command_invoked": False,
        "native_remote_only": True,
    }
    assert queue.merge_guard_refusal_proof(op, target)
    if change == "head":
        report["no_merge_command"]["expected_head"] = "b" * 40
    elif change == "phase":
        report["no_merge_command"]["boundary"] = "after_gh_pr_merge"
    elif change == "attempted":
        report["merge_executed"] = True
    elif change == "remote":
        op["result"]["merge_guard_refusal"]["native_remote_only"] = 1
    else:
        report["reason"] = "modified"
    if change != "hash":
        op["result"]["merge_guard_refusal"]["report_sha256"] = queue._digest(report)
    assert queue.merge_guard_refusal_proof(op, target) is None
