"""Offline descendant authority: real CLI receipts/queues/lineage; no providers."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_ci_followup import public_ci_world as public_ci_world
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_frontier import finite_host
from test_closer_native_observer import host as host

import closer_dispatcher as dispatcher
import closer_native_descendants as descendants
import closer_pipeline as pipeline
import closer_resources as resources


def followed(w, capsys):
    dispatcher.main(w.argv)
    capsys.readouterr()
    w.check.update(status="completed", conclusion="success")
    w.snapshot.update(checks="SUCCESS")
    dispatcher.main(w.argv)
    value = json.loads(capsys.readouterr().out)
    cfg = {
        "dispatch_state": str(w.raw),
        "finite_frontier": w.frontier,
        "assignment": w.argv[w.argv.index("--assignment") + 1],
    }
    return cfg, value


def test_real_descendant_receipts_cover_both_lineage_hops(public_ci_world, capsys):
    cfg, value = followed(public_ci_world, capsys)
    leaves = descendants._leaves(cfg, value["descendant_receipts"], frozenset(), [])
    assert len(leaves) == 2
    assert [row["active"] for row in leaves] == [False, True]
    assert leaves[-1]["domain"] == "known_held"
    descendants._validate_covered(cfg, leaves)
    assert public_ci_world.events["provider"] == 0


@pytest.mark.parametrize(
    "change", ["missing", "foreign", "hash", "frontier", "runs", "conflict", "cycle", "subset"]
)
def test_actual_child_coverage_refuses_unbound_authority(public_ci_world, capsys, change):
    cfg, value = followed(public_ci_world, capsys)
    refs = copy.deepcopy(value["descendant_receipts"])
    if change == "missing":
        refs.pop()
    elif change == "foreign":
        refs[0]["target"] = "foreign/repo#1"
    elif change == "hash":
        refs[0]["receipt_sha256"] = "0" * 64
    elif change == "frontier":
        refs[0]["finite_frontier"]["owner"] = "foreign"
    elif change == "runs":
        refs[0]["run_ids"] = {}
    elif change == "conflict":
        refs.append(dict(refs[0], receipt_sha256="0" * 64))
    elif change == "subset":
        cfg["finite_frontier"] = dict(cfg["finite_frontier"], targets=[])
    else:
        root = resources.scoped_root(dispatcher._pipeline_state_path(public_ci_world.raw))
        path = resources.shard_dir(root, "owner/repo#12") / "state.json"
        state = pipeline._load(path)
        links = list(state["stage_successors"].values())
        first = next(
            link
            for link in links
            if link["stage_predecessor"]["predecessor_frontier_id"]
            == cfg["finite_frontier"]["frontier_id"]
        )
        last = next(link for link in links if link is not first)
        first["stage_predecessor"]["predecessor_frontier_id"] = last["finite_frontier"][
            "frontier_id"
        ]
        pipeline._save(path, state)
    with pytest.raises((ValueError, KeyError, TypeError)):
        descendants._leaves(cfg, refs, frozenset(), [])


def test_old_receipt_requires_exact_finalized_original_coverage(public_ci_world, capsys):
    cfg, value = followed(public_ci_world, capsys)
    refs = value["descendant_receipts"]
    old = frozenset(ref["round_id"] for ref in refs)
    with pytest.raises(ValueError, match="historical"):
        descendants._leaves(cfg, refs, old, [])
    covered = descendants._leaves(cfg, refs, frozenset(), [])
    assert descendants._leaves(cfg, refs, old, covered) == covered
    changed = copy.deepcopy(covered)
    changed[0]["receipt_ref"]["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="historical"):
        descendants._leaves(cfg, refs, old, changed)


def test_unknown_actual_child_remains_unknown_never_historical(public_ci_world, capsys):
    public_ci_world.guard_mode = "uncertain"
    cfg, value = followed(public_ci_world, capsys)
    refs = value["descendant_receipts"]
    leaves = descendants._leaves(cfg, refs, frozenset(), [])
    assert leaves[-1]["domain"] == "unknown"
    with pytest.raises(ValueError, match="UNKNOWN"):
        descendants._leaves(cfg, refs, frozenset(ref["round_id"] for ref in refs), leaves)
    assert len(public_ci_world.merges) == 1 and not public_ci_world.verifies


def test_top_root_subset_cannot_be_replaced_by_descendant(public_ci_world, capsys):
    cfg, value = followed(public_ci_world, capsys)
    child = value["descendant_receipts"][-1]
    candidate = dict(child, assignment=[child["target"]])
    with pytest.raises(ValueError, match="binding"):
        descendants._root(cfg, candidate)
    descendants._root(cfg, value)


def test_signed_bundle_is_immutable_and_signature_digest_checked(tmp_path, monkeypatch):
    import closer_mechanisms as mechanisms

    root = tmp_path / "observer"
    nonce = root / ("a" * 48)
    nonce.mkdir(parents=True)
    mechanisms._key(root, create=True)
    cohort = {"original": "cohort"}
    monkeypatch.setattr(descendants, "_authority", lambda _: ({}, {}, root, cohort))
    record = {"schema": descendants.SCHEMA, "cohort": cohort, "admission_nonce": nonce.name}
    path = nonce / "descendant-receipts.json"
    ref = descendants._write_signed(path, record, root)
    assert descendants._write_signed(path, record, root) == ref
    assert descendants._read_signed({}, ref) == record
    with pytest.raises(ValueError):
        descendants._write_signed(path, dict(record, authority=True), root)
    with pytest.raises(ValueError):
        descendants._read_signed({}, dict(ref, sha256="0" * 64))
    raw = json.loads(path.read_bytes())
    raw["record"]["cohort"] = {"original": "foreign"}
    path.write_text(json.dumps(raw))
    ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        descendants._read_signed({}, ref)


@pytest.fixture
def native_descendant_world(capacity_host, public_ci_world, monkeypatch, capsys, request):
    """Actual signed native admission/worker handoff over real CLI descendants."""
    import subprocess

    import closer_campaign as campaign
    import closer_native_observer as native
    import closer_runtime_binding as binding

    w = public_ci_world
    import time

    from test_closer_native_capacity import _REAL_TIME

    monkeypatch.setattr(time, "time", _REAL_TIME)
    capacity_host[5][0] = _REAL_TIME()
    dispatcher.main(w.argv)
    initial = json.loads(capsys.readouterr().out)
    assert initial["targets"]["owner/repo#12"].get("stage") == "act", initial["targets"][
        "owner/repo#12"
    ]
    monkeypatch.setenv("HOME", str(capacity_host[4].parents[3]))
    manifest, manifest_path, obj, doc, source, clock, _frontier = finite_host(capacity_host)
    monkeypatch.setenv("HOME", str(source.parents[3]))
    # Feed the existing explicit original cohort to native publication.
    assignment = Path(w.argv[w.argv.index("--assignment") + 1])
    source.write_bytes(assignment.read_bytes())
    doc["owner"] = w.frontier["owner"]
    doc["assignment_targets"] = w.frontier["targets"]
    doc["worker_config"].update(finite_frontier=w.frontier, dispatch_state=str(w.raw))
    obj.update(
        lambda current: current.update(
            owner=doc["owner"],
            assignment_targets=doc["assignment_targets"],
            worker_config=doc["worker_config"],
        )
    )
    context = native.publish_admission(manifest, manifest_path, obj.path, doc)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    worker_id = "a" * 12
    fields = {
        "state": str(obj.path),
        "target": campaign.ROUND_UNIT,
        "owner": doc["owner"],
        "generation": str(doc["generation"]),
        "runtime-manifest": str(manifest_path),
        "mechanism": "native-handoff/v1",
        "result": str(campaign.result_path(obj.path, worker_id)),
    }
    argv = [str(native.contract(manifest)[2])]
    for key, value in fields.items():
        argv.extend(["--" + key, value])
    native.authorize_worker(manifest, argv)
    cfg["native_worker"] = fields
    obj.update(
        lambda current: current["workers"].append(
            {
                "worker_id": worker_id,
                "target": campaign.ROUND_UNIT,
                "state": "running",
                "outcome": None,
                "mechanism": "native-handoff/v1",
            }
        )
    )
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.begin_terminal_receipt(cfg)
    w.guard_mode = getattr(request, "param", "normal")
    w.check.update(status="completed", conclusion="success")
    w.snapshot.update(checks="SUCCESS")
    dispatcher.main(w.argv)
    candidate = json.loads(capsys.readouterr().out)
    assert candidate.get("descendant_receipts"), {
        k: candidate.get(k)
        for k in ("targets", "ci_wait_continuations", "merge_verification_successors")
    }
    assert hooks.set_terminal_receipt(
        subprocess.CompletedProcess([], 78, stdout=json.dumps(candidate)), cfg
    ), hooks.terminal_receipt_problem
    token = "token lease:17"
    lane = Path(__import__("os").environ["HANDOFF_DIR"]) / ".lane-closer.lock"
    digest = hashlib.sha256(token.encode()).hexdigest()
    cleanup = {
        "schema": "closer-native-lease-cleanup/v1",
        "lane": "closer",
        "lane_dir": str(lane),
        "released": True,
        "exact_token_matched": True,
        "expected_token_sha256": digest,
        "meta_sha256": digest,
        "released_lease_inode": 17,
        "released_at": clock[0],
    }
    ref = descendants.finalize_bundle(cfg, hooks.descendant_bundle, cleanup, token)
    outcome = (
        "unknown"
        if descendants.bundle_record(cfg, ref)["aggregate"] == "unknown"
        else "scoped_continuation"
    )
    proof = descendants.continuation_proof(cfg, {"descendant_bundle": ref})
    result = {
        "worker_id": worker_id,
        "target": campaign.ROUND_UNIT,
        "mechanism": "native-handoff/v1",
        "outcome": outcome,
        "native_admission": context,
        "detail": json.dumps({"descendant_bundle": ref, "proof": proof}),
    }
    path = Path(fields["result"])
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result))
    obj.update(lambda current: current["workers"][0].update(state="terminal", outcome=outcome))
    return w, cfg, ref, obj, hooks, candidate, cleanup, token


def test_finalized_known_bundle_requires_actual_worker_and_revalidates_bytes(
    native_descendant_world,
):
    w, cfg, ref, obj, hooks, candidate, *_ = native_descendant_world
    import closer_runtime_binding as binding

    leaves = descendants.observed_leaves(cfg, ref)
    assert len(leaves) == 2 and leaves[-1]["active"]
    assert leaves[-1]["source_worker"]["bundle_ref"] == ref
    assert leaves[-1]["source_worker"]["result_path"] == cfg["native_worker"]["result"]
    proof = descendants.continuation_proof(cfg, {"descendant_bundle": ref})
    assert proof and proof["held_targets"] == ["owner/repo#12"]
    assert binding.scoped_continuation_proof(cfg, proof) == proof
    assert (
        descendants.continuation_proof(cfg, {"descendant_bundle": hooks.descendant_bundle}) is None
    )
    assert descendants._history(cfg)
    result = Path(cfg["native_worker"]["result"])
    result.write_text(result.read_text().replace("scoped_continuation", "unknown"))
    with pytest.raises(ValueError, match="actual known"):
        descendants.observed_leaves(cfg, ref)


def test_capture_and_metrics_consume_actual_active_leaf(native_descendant_world):
    import closer_campaign as campaign
    import closer_hold_followup as holds

    w, cfg, ref, obj, hooks, candidate, *_ = native_descendant_world
    coverage = holds._covered_leaves(obj, obj.read())
    assert coverage
    doc = holds.capture(obj)
    active = [row for row in descendants.observed_leaves(cfg, ref) if row["active"]]
    assert len(active) == 1
    assert doc["hold_followups"], {
        "coverage": coverage,
        "row": pipeline._load(
            resources.shard_dir(
                resources.scoped_root(dispatcher._pipeline_state_path(w.raw)), "owner/repo#12"
            )
            / "state.json"
        )["frontiers"][active[0]["frontier"]["frontier_id"]],
    }
    hold = next(iter(doc["hold_followups"].values()))
    assert hold["source"]["descendant_coverage"] == active[0]
    assert hold["frontier"] == active[0]["frontier"]
    assert doc["worker_config"]["finite_frontier"] == cfg["finite_frontier"]
    actual = holds.validated_receipts(
        obj, obj.read(), campaign.load_manifest(cfg["runtime_manifest"])
    )
    assert len(actual) == 3


@pytest.mark.parametrize("native_descendant_world", ["normal", "uncertain"], indirect=True)
def test_reported_bundle_and_aggregate_prevent_root_only_success(native_descendant_world):
    """Exercise actual final-result routing with prevalidated native boundary seam."""
    import subprocess
    import sys
    import time

    import closer_runtime_binding as binding

    w, cfg, ref, obj, hooks, candidate, cleanup, token = native_descendant_world
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "fixture")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks._dependencies_match = lambda: True
    hooks.owns = lambda _token: True
    hooks.prerun = lambda _deadline: (0, token)
    hooks.begin_terminal_receipt = lambda _cfg: None

    def terminal(_done, _cfg):
        hooks.descendant_bundle = ref
        return True

    hooks.set_terminal_receipt = terminal

    def postrun(_token, _deadline):
        hooks.postrun_result = {
            "code": 0,
            "dependencies_valid": True,
            "cleanup": cleanup,
            "stderr": "",
        }
        return 0

    hooks.postrun = postrun
    command = [
        sys.executable,
        "fixture.py",
        "--assignment",
        cfg["assignment"],
        "--finite-frontier-json",
        json.dumps(cfg["finite_frontier"]),
        "--request-timeout-seconds",
        str(cfg["request_timeout_seconds"]),
    ]
    done = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps(dict(candidate, oversized_detail="x" * 20000))
    )
    outcome, detail = binding.run_native_round(
        command, cfg, runner=lambda *_a, **_k: done, hooks=hooks, now=time.time
    )
    reported = json.loads(detail)
    assert reported["descendant_bundle"] == ref
    assert "completed_round" not in reported
    expected = (
        "unknown"
        if descendants.bundle_record(cfg, ref)["aggregate"] == "unknown"
        else "scoped_continuation"
    )
    assert outcome == expected, detail
    assert len(w.merges) == 1 and w.events["provider"] == 0


def test_restart_current_b_requires_actual_prior_finalized_worker(
    native_descendant_world, capacity_host, capsys
):
    import subprocess

    import closer_campaign as campaign
    import closer_native_observer as native
    import closer_runtime_binding as binding

    w, old_cfg, old_ref, obj, _hooks, _candidate, *_ = native_descendant_world
    manifest = campaign.load_manifest(old_cfg["runtime_manifest"])
    capacity_host[6](2)
    invocation = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]
    invocation.pop("original_deadline")
    invocation["execution_policy"] = old_cfg["finite_frontier"]["schema"]
    doc = obj.update(lambda current: current.update(generation=2))
    context = native.publish_admission(manifest, Path(old_cfg["runtime_manifest"]), obj.path, doc)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    fields = dict(
        old_cfg["native_worker"],
        generation="2",
        result=str(campaign.result_path(obj.path, "b" * 12)),
    )
    argv = [str(native.contract(manifest)[2])]
    for key, value in fields.items():
        argv.extend(["--" + key, value])
    native.authorize_worker(manifest, argv)
    cfg["native_worker"] = fields
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.begin_terminal_receipt(cfg)
    dispatcher.main(w.argv)
    current = json.loads(capsys.readouterr().out)
    done = subprocess.CompletedProcess([], 78, stdout=json.dumps(current))
    assert hooks.set_terminal_receipt(done, cfg), hooks.terminal_receipt_problem
    record = descendants.bundle_record(cfg, hooks.descendant_bundle)
    assert len(record["leaves"]) == 2 and record["aggregate"] == "known_held"
    assert record["root_receipt"]["round_id"] not in hooks.terminal_watermark[2]
    assert all(
        row["receipt_ref"]["round_id"] in hooks.terminal_watermark[2] for row in record["leaves"]
    )
    assert len(w.merges) == 1 and len(w.verifies) == 1 and w.events["provider"] == 0
    Path(old_cfg["native_worker"]["result"]).unlink()
    assert not hooks.set_terminal_receipt(done, cfg)
    assert "historical" in hooks.terminal_receipt_problem["reason"]


@pytest.mark.parametrize("native_descendant_world", ["uncertain"], indirect=True)
def test_unknown_signed_worker_observation_never_supplies_historical_release(
    native_descendant_world,
):
    _w, cfg, ref, _obj, _hooks, *_ = native_descendant_world
    with pytest.raises(ValueError, match="finalization"):
        descendants.observed_leaves(cfg, ref)
    leaves = descendants.observed_leaves(cfg, ref, require_finalized=False)
    assert any(row["domain"] == "unknown" for row in leaves)
    assert all(row["source_worker"]["bundle_ref"] == ref for row in leaves)
    assert not descendants._history(cfg)
    assert descendants.continuation_proof(cfg, {"descendant_bundle": ref}) is None


@pytest.mark.parametrize("marker", ["exact", "wrong_nonce", "missing"])
def test_actual_pinned_postrun_gate_finalizes_only_verified_cleanup(
    native_descendant_world, marker
):
    import subprocess
    import time

    _w, cfg, ref, _obj, hooks, _candidate, cleanup, token = native_descendant_world
    hooks.bounded = True
    hooks.dependencies = {
        "assignment": (
            Path(cfg["assignment"]),
            hashlib.sha256(Path(cfg["assignment"]).read_bytes()).hexdigest(),
        )
    }
    hooks.assignment_source = Path(cfg["assignment"])
    hooks.lane = Path(cleanup["lane_dir"])
    hooks.scripts = {"postrun": Path("/offline/pinned-postrun")}
    hooks.now = time.time
    nonce = "a" * 48
    hooks.env = {"CLOSER_ADMISSION_NONCE": nonce}
    hooks._pins_match = lambda: True
    hooks._dependencies_match = lambda: True
    hooks.owns = lambda supplied: supplied == token
    observed = nonce if marker == "exact" else "b" * 48
    stdout = (
        ""
        if marker == "missing"
        else "CLOSER_NATIVE_CLEANUP_V1 " + observed + " " + json.dumps(cleanup)
    )
    hooks.runner = lambda *_a, **_k: subprocess.CompletedProcess(
        [],
        65,
        stdout=stdout,
        stderr="handoff-postrun: closer stop rejected; preserve ledger and resume actionable work",
    )
    assert hooks.postrun(token, time.time() + 10) == 65
    if marker == "exact":
        assert hooks.descendant_bundle == ref and hooks.descendant_bundle_problem is None
        assert hooks.postrun_result["cleanup"] == cleanup
    else:
        assert hooks.descendant_bundle != ref
        assert hooks.descendant_bundle_problem
        assert (
            descendants.continuation_proof(cfg, {"descendant_bundle": hooks.descendant_bundle})
            is None
        )


def test_real_native_hook_refuses_top_only_when_linked_child_missing(native_descendant_world):
    import subprocess

    _w, cfg, _ref, _obj, hooks, candidate, *_ = native_descendant_world
    missing = dict(candidate)
    missing.pop("descendant_receipts")
    assert not hooks.set_terminal_receipt(
        subprocess.CompletedProcess([], 78, stdout=json.dumps(missing)), cfg
    )
    assert "actual immutable receipt" in hooks.terminal_receipt_problem["reason"]
    assert "CLOSER_POSTRUN_ROUND_ID" not in hooks.env


@pytest.mark.parametrize(
    "field", ["owner", "generation", "state", "target", "result", "runtime-manifest"]
)
def test_descendant_seal_requires_exact_signed_original_worker(native_descendant_world, field):
    _w, cfg, _ref, _obj, _hooks, *_ = native_descendant_world
    wrong = copy.deepcopy(cfg)
    wrong["native_worker"][field] = "foreign"
    with pytest.raises(ValueError, match="original.*worker"):
        descendants._authority(wrong)


@pytest.fixture
def history_budget_world(tmp_path, monkeypatch):
    """Real signed envelopes and registry; expensive source validation is counted."""
    import closer_campaign as campaign
    import closer_mechanisms as mechanisms

    root = tmp_path / "history-observer"
    root.mkdir()
    mechanisms._key(root, create=True)
    cohort = {"campaign": "same-original"}
    monkeypatch.setattr(
        descendants._HistoryBudget, "authority", lambda _self, _cfg: ({}, {}, root, cohort)
    )
    monkeypatch.setattr(descendants, "_actual_known_worker", lambda *_a, **_k: {})
    validations = []
    monkeypatch.setattr(
        descendants, "_validate_covered", lambda _cfg, rows, **_kw: validations.append(len(rows))
    )
    obj = campaign.Campaign(tmp_path / "history.campaign.json")
    obj.begin(
        "history-budget", ["offline/repo#1"], owner="owner", release_pin="pin", worker_config={}
    )
    cfg = {"native_worker": {"state": str(obj.path)}}

    def bundle(index, count):
        nonce = root / (f"{index:032x}")
        nonce.mkdir()
        rows = [
            {
                "target": str(i),
                "domain": "known_held",
                "receipt_ref": {
                    "round_id": f"{i + 1:016x}",
                    "receipt_path": f"/offline/{i}.json",
                    "receipt_sha256": "a" * 64,
                },
            }
            for i in range(count)
        ]
        record = {
            "schema": descendants.SCHEMA,
            "cohort": cohort,
            "admission_nonce": nonce.name,
            "leaves": rows,
        }
        ref = descendants._write_signed(nonce / "descendant-receipts.json", record, root)
        finalized = {
            "schema": descendants.FINAL_SCHEMA,
            "cohort": cohort,
            "admission_nonce": nonce.name,
            "sealed_bundle": ref,
            "sealed_sha256": descendants._digest(record),
        }
        return descendants._write_signed(
            nonce / "descendant-receipts-finalized.json", finalized, root
        )

    def worker(index, ref=None, *, padding=""):
        worker_id = f"{index:012x}"
        obj.update(
            lambda doc: doc["workers"].append(
                {"worker_id": worker_id, "state": "terminal", "outcome": "scoped_continuation"}
            )
        )
        path = campaign.result_path(obj.path, worker_id)
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps({"detail": json.dumps({"descendant_bundle": ref}), "padding": padding})
        )
        return path

    return cfg, bundle, worker, validations


def test_history_deduplicates_bundle_before_revalidating_full_coverage(history_budget_world):
    cfg, bundle, worker, validations = history_budget_world
    ref = bundle(1, descendants.MAX_HISTORY_ROWS)
    worker(1, ref)
    worker(2, ref)
    leaves = descendants._history(cfg)
    assert len(leaves) == descendants.MAX_HISTORY_ROWS
    assert validations == [descendants.MAX_HISTORY_ROWS]


def test_history_global_leaf_budget_refuses_second_validation(history_budget_world):
    cfg, bundle, worker, validations = history_budget_world
    worker(1, bundle(1, descendants.MAX_HISTORY_ROWS))
    worker(2, bundle(2, descendants.MAX_HISTORY_ROWS))
    with pytest.raises(descendants._HistoryBudgetExceeded, match="leaf validation"):
        descendants._history(cfg)
    assert validations == [descendants.MAX_HISTORY_ROWS]


def test_history_byte_budget_includes_nonproof_results(history_budget_world, monkeypatch):
    cfg, _bundle, worker, validations = history_budget_world
    worker(1, padding="x" * 200)
    worker(2, padding="x" * 200)
    monkeypatch.setattr(
        descendants, "MAX_HISTORY_BYTES", Path(cfg["native_worker"]["state"]).stat().st_size + 400
    )
    with pytest.raises(descendants._HistoryBudgetExceeded, match="byte budget"):
        descendants._history(cfg)
    assert not validations


def test_history_stops_when_requested_original_rounds_are_authenticated(history_budget_world):
    cfg, bundle, worker, validations = history_budget_world
    # Newest exact proof covers all requested old rounds; older huge history is
    # never opened or validated, and unrelated covered rows are not accumulated.
    worker(1, bundle(1, descendants.MAX_HISTORY_ROWS))
    worker(2, bundle(2, 2))
    rows = descendants._history(cfg, {f"{1:016x}"})
    assert [row["receipt_ref"]["round_id"] for row in rows] == [f"{1:016x}"]
    assert validations == [2]


def test_history_failed_reads_cannot_reset_shared_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(descendants, "MAX_HISTORY_BYTES", 2 * descendants.MAX_BYTES)
    budget = descendants._HistoryBudget()
    missing = tmp_path / "missing"
    with pytest.raises(OSError):
        budget.read(missing)
    with pytest.raises(descendants._HistoryBudgetExceeded, match="byte budget"):
        budget.read(missing)
    assert budget.bytes_left == 0


def test_history_receipt_bytes_are_reserved_before_resource_rereads(tmp_path, monkeypatch):
    cfg = {"dispatch_state": str(tmp_path / "dispatch.json")}
    root = resources.scoped_root(Path(cfg["dispatch_state"] + ".pipeline.json"))
    path = root / "rounds" / (("a" * 16) + ".json")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 4096)
    ref = {"round_id": "a" * 16, "receipt_path": str(path)}
    budget = descendants._HistoryBudget()
    budget.bytes_left = 1024
    monkeypatch.setattr(
        resources,
        "read_round_receipt",
        lambda *_: pytest.fail("over-budget receipt cannot reach framework read"),
    )
    with pytest.raises(descendants._HistoryBudgetExceeded, match="source byte budget"):
        descendants._receipt(cfg, ref, [], {}, _history_budget=budget)


def test_history_source_bytes_are_reserved_before_lineage_validation(tmp_path, monkeypatch):
    import closer_stage_continuation as stages

    cfg = {
        "dispatch_state": str(tmp_path / "dispatch.json"),
        "finite_frontier": {"targets": ["offline/repo#1"]},
    }
    root = resources.scoped_root(Path(cfg["dispatch_state"] + ".pipeline.json"))
    shard = resources.shard_dir(root, "offline/repo#1") / "state.json"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"x" * 4096)
    budget = descendants._HistoryBudget()
    budget.bytes_left = 8192
    monkeypatch.setattr(
        stages,
        "active_leaf",
        lambda *_: pytest.fail("over-budget source cannot reach lineage validation"),
    )
    with pytest.raises(descendants._HistoryBudgetExceeded, match="source byte budget"):
        descendants._validate_covered(cfg, [{"target": "offline/repo#1"}], _history_budget=budget)


def test_history_authority_bytes_are_reserved_before_native_revalidation(
    native_descendant_world, monkeypatch
):
    _w, cfg, *_ = native_descendant_world
    budget = descendants._HistoryBudget()
    budget.bytes_left = 1
    monkeypatch.setattr(
        descendants,
        "_authority",
        lambda *_: pytest.fail("over-budget authority cannot reach native validation"),
    )
    with pytest.raises(descendants._HistoryBudgetExceeded, match="byte budget"):
        budget.authority(cfg)


def test_history_reserved_source_change_refuses_cached_authority(tmp_path):
    path = tmp_path / "source.json"
    path.write_text("original")
    budget = descendants._HistoryBudget()
    budget.reserve_path(path)
    path.write_text("replaced")
    with pytest.raises(ValueError, match="source changed"):
        budget.verify_sources()


@pytest.mark.parametrize("retained", ["original_queue", "source_snapshot"])
def test_history_retained_ci_proof_budget_precedes_real_framework_reads(
    public_ci_world, capsys, tmp_path, monkeypatch, retained
):
    """Real CLI lineage may reference large retained files outside its shard."""
    import closer_finite_reentry as reentry
    import closer_stage_continuation as stages

    cfg, value = followed(public_ci_world, capsys)
    covered = descendants._leaves(cfg, value["descendant_receipts"], frozenset(), [])
    target = "owner/repo#12"
    root = resources.scoped_root(dispatcher._pipeline_state_path(public_ci_world.raw))
    shard = resources.shard_dir(root, target) / "state.json"
    state = pipeline._load(shard)
    chain, _leaf, _entry = stages.linked_chain(state, cfg["finite_frontier"], target)
    hop = next(
        hop
        for hop in chain
        if hop["record"]["stage_predecessor"].get("kind") == "ci_no_effect_wait"
    )
    if retained == "original_queue":
        path = Path(hop["record"]["stage_predecessor"]["original_queue"]["path"])
        path.write_bytes(b"x" * (7 * 1024 * 1024))
    else:
        path = tmp_path / "large-retained-snapshot.json"
        path.write_text(json.dumps({"padding": "x" * (7 * 1024 * 1024)}))
        prep = hop["item"]["results"]["prepare"]
        prep["snapshot_ref"] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        hop["record"]["stage_predecessor"]["predecessor_row_sha256"] = pipeline._digest(hop["item"])
        pipeline._save(shard, state)
    # The real bounded lineage parser must still select the exact CI ancestor.
    assert stages.linked_chain(pipeline._load(shard), cfg["finite_frontier"], target)[0]
    actions = shard.parent / "state.actions" / (pipeline._digest(target) + ".json")
    base_cost = 32 * (shard.stat().st_size + actions.stat().st_size)
    for legacy in (Path(cfg["dispatch_state"]), Path(cfg["dispatch_state"] + ".pipeline.json")):
        if legacy.exists():
            base_cost += 4 * legacy.stat().st_size
    budget = descendants._HistoryBudget()
    budget.bytes_left = base_cost + 1024 * 1024
    monkeypatch.setattr(
        stages,
        "active_leaf",
        lambda *_: pytest.fail("retained proof budget must refuse before active_leaf"),
    )
    monkeypatch.setattr(
        reentry, "_read", lambda *_: pytest.fail("over-budget retained proof must not be opened")
    )
    with pytest.raises(descendants._HistoryBudgetExceeded, match="source byte budget"):
        descendants._validate_covered(cfg, covered, _history_budget=budget)
