"""Offline integration of retained quarantine with the actual scoped coordinator."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

import pytest

import closer_dispatcher as dispatcher
import closer_pipeline as pipeline
import closer_quarantine as quarantine
import closer_resources as resources


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


def advertisement(path, key, *, target="owner/held#1", filename="legacy-intent.json"):
    ad = resources._key_dir(resources.scoped_root(path), resources.normalize_key(key)) / filename
    write(ad, {"key": key, "target": target, "owner": "historical-owner", "state": "unknown"})
    return ad


def original_state(tmp_path):
    raw = tmp_path / "dispatch.json"
    path = Path(str(raw) + ".pipeline.json")
    held = [f"owner/held#{number}" for number in range(1, 432)]
    document = pipeline._empty()
    document["runs"]["historical-round"] = {
        "created": time.time() - 7200,
        "deadline": time.time() - 3600,
        "assignment": sorted(held),
        "carry_targets": [],
        "targets": {
            target: {
                "status": "queued",
                "stage": "exception",
                "revision": "historical",
                "results": {},
            }
            for target in held
        },
    }
    write(path, document)
    write(
        raw,
        {
            "schema": "closer-queue/v1",
            "items": {held[0]: {"status": "queued", "operation": None}},
            "round": {},
        },
    )
    return raw, path, held


def inventory(target, aliases=()):
    keys = ["pr:" + target, "repo:" + target.split("#")[0], *aliases]
    for key in aliases:
        kind, value = key.split(":", 1)
        if kind in {"pr", "issue", "branch"}:
            keys.append("repo:" + value.split("#")[0].split("@")[0])
    return sorted(set(keys))


def callbacks(events, *, aliases=(), during_prepare=None, during_interpret=None):
    def prepare(target, _context):
        events[(target, "prepare")] += 1
        if during_prepare:
            during_prepare()
        return {
            "revision": "revision-1",
            "identity": target,
            "semantic_cache_key": "closer-evidence-semantic/v1:"
            + hashlib.sha256(target.encode()).hexdigest(),
            "aliases": list(aliases),
        }

    def interpret(target, _context):
        events[(target, "interpret")] += 1
        if during_interpret:
            during_interpret()
        return {"substantive_needs": []}

    def act(target, _context):
        events[(target, "simulated-effect")] += 1
        return {"status": "completed", "target_complete": True}

    def exception(*_args):
        pytest.fail("offline integration does not need any strong/provider callback")

    return {"prepare": prepare, "interpret": interpret, "act": act, "exception": exception}


def run(path, targets, cb, key_callback):
    return pipeline.run_pipeline(
        path,
        targets,
        **cb,
        resource_keys=key_callback,
        coordinator_mode="resource_scoped",
        deadline=time.time() + 15,
        max_io_workers=2,
        max_low_workers=1,
        max_exception_workers=1,
        capacity_fn=lambda: {"io": 2, "low": 1, "exception": 1},
    )


@pytest.mark.parametrize("alias_kind", ["pr", "issue", "branch", "worktree"])
def test_historical_queued_exceptions_and_ads_fence_aliases_but_disjoint_work_progresses(
    tmp_path, alias_kind
):
    raw, path, held = original_state(tmp_path)
    overlap = {
        "pr": "pr:owner/shared-pr#8",
        "issue": "issue:owner/shared-source#9",
        "branch": "branch:owner/shared-branch@feature",
        "worktree": "worktree:" + str((tmp_path / "shared-checkout").resolve()),
    }[alias_kind]
    ad = advertisement(path, overlap)
    originals = {source: source.read_bytes() for source in [raw, path, ad]}
    blocked = "owner/candidate#10"
    events = Counter()

    def keys(target, context):
        prepared = context.get("prepare") or {}
        return inventory(target, prepared.get("aliases", []))

    for cadence in range(3):
        healthy = f"owner/healthy#{cadence + 1}"
        cb = callbacks(events)
        original_prepare = cb["prepare"]

        def prepare(target, context):
            value = original_prepare(target, context)
            value["aliases"] = [overlap] if target == blocked else []
            return value

        receipt = run(path, [blocked, healthy], {**cb, "prepare": prepare}, keys)
        assert receipt["targets"][healthy]["status"] == "completed", receipt["targets"][healthy]
        assert events[(healthy, "interpret")] == 1
        assert events[(healthy, "simulated-effect")] == 1
        assert receipt["targets"][blocked]["status"] == "deferred"
        assert events[(blocked, "interpret")] == 0
        assert events[(blocked, "simulated-effect")] == 0
        assert not pipeline._validate_stop_legacy(path, held)["valid"]
        assert {source: source.read_bytes() for source in originals} == originals
        snapshot = quarantine.scope_snapshot(path)
        assert not snapshot["global_reasons"]
        assert "repo:owner/held" in snapshot["quarantine_keys"]
        assert overlap in snapshot["quarantine_keys"]
    assert (
        sum(count for (target, event), count in events.items() if event == "simulated-effect") == 3
    )


def test_new_retained_intent_during_prepare_prevents_low_and_effect(tmp_path):
    _raw, path, held = original_state(tmp_path)
    original = path.read_bytes()
    target = "owner/healthy#2"
    alias = "issue:owner/new-source#8"
    inserted = []
    events = Counter()

    def insert():
        inserted.append(advertisement(path, alias, filename="intent.json"))

    cb = callbacks(events, aliases=[alias], during_prepare=insert)
    receipt = run(
        path, [target], cb, lambda t, c: inventory(t, (c.get("prepare") or {}).get("aliases", []))
    )
    assert events[(target, "prepare")] == 1
    assert events[(target, "interpret")] == events[(target, "simulated-effect")] == 0
    assert receipt["targets"][target]["status"] == "deferred"
    assert inserted[0].exists() and path.read_bytes() == original
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


def test_changed_alias_before_act_is_fenced_by_fresh_resource_census(tmp_path):
    _raw, path, held = original_state(tmp_path)
    original = path.read_bytes()
    target = "owner/healthy#2"
    blocked = "pr:owner/held#1"
    aliases = []
    events = Counter()
    cb = callbacks(events, during_interpret=lambda: aliases.append(blocked))
    receipt = run(path, [target], cb, lambda t, _c: inventory(t, aliases))
    assert events[(target, "interpret")] == 1
    assert events[(target, "simulated-effect")] == 0
    assert receipt["targets"][target]["status"] == "deferred"
    assert path.read_bytes() == original
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


def test_scoped_budget_admission_never_renews_or_validates_historical_round(tmp_path):
    raw, path, held = original_state(tmp_path)
    before = path.read_bytes()
    old_deadline = json.loads(before)["runs"]["historical-round"]["deadline"]
    requested = time.time() + 90
    result = dispatcher._scoped_budget_admission(raw, requested)
    assert result["selected_deadline"] == result["requested_deadline"] == requested
    assert result["legacy_round_renewed"] is False and result["legacy_stop_authority"] is False
    assert result["snapshot"]["quarantine_keys"]
    assert path.read_bytes() == before
    assert json.loads(path.read_text())["runs"]["historical-round"]["deadline"] == old_deadline
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


@pytest.mark.parametrize("drift", ["head", "source-alias"])
def test_actual_dispatcher_act_refresh_rejects_changed_binding_without_external_effect(
    tmp_path, monkeypatch, drift
):
    import copy
    from types import SimpleNamespace

    import test_closer_dispatcher as runtime_fixture
    import test_closer_disposition as proof_fixture

    runtime_fixture.no_live_state_or_provider.__wrapped__(monkeypatch, tmp_path)
    _raw, path, held = original_state(tmp_path)
    original = path.read_bytes()
    current, _packet, ref, prep, interpreted = proof_fixture.fixture(tmp_path)
    current.update(
        state="OPEN",
        draft=False,
        active_threads=0,
        mergeable="MERGEABLE",
        labels=[],
        head_repository="owner/repo",
        head_branch="feature",
    )
    prep.update(
        snapshot=copy.deepcopy(current),
        packet_ref=ref,
        identity=current["target"],
        semantic_cache_key="closer-evidence-semantic/v1:" + "8" * 64,
    )
    snapshot_bytes = json.dumps(current, sort_keys=True).encode()
    snapshot_path = tmp_path / "prepared-snapshot.json"
    snapshot_path.write_bytes(snapshot_bytes)
    prep["snapshot_ref"] = {
        "path": str(snapshot_path),
        "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
    }
    github = runtime_fixture.FakeGitHub(copy.deepcopy(current))
    calls = Counter()

    def guard(*_args, **_kwargs):
        calls["external-effect"] += 1
        pytest.fail("changed prepared head/source cannot reach merge guard transport")

    backend = dispatcher.Closer(
        github, state_path=tmp_path / "dispatch.json", apply=True, run_fn=guard
    )
    monkeypatch.setattr(dispatcher, "GitHub", lambda **_kwargs: github)
    monkeypatch.setattr(
        dispatcher, "_pipeline_capacity", lambda: {"io": 1, "low": 1, "exception": 1}
    )
    original_run = pipeline.run_pipeline

    def run_real_scoped(state_path, targets, **kwargs):
        def prepare(_target, _context):
            calls["prepare"] += 1
            return copy.deepcopy(prep)

        def interpret(_target, _context):
            calls["interpret"] += 1
            changed = copy.deepcopy(current)
            if drift == "head":
                changed["head"] = "9" * 40
            else:
                changed["sources"].append(
                    {
                        "target": "owner/foreign#9",
                        "body": "## Tasks\n- [ ] New criterion",
                        "state": "open",
                    }
                )
            changed["revision"] = dispatcher.digest(changed)
            github.current = changed
            return {
                **copy.deepcopy(interpreted),
                "substantive_needs": [],
                "classification": "relevant",
            }

        kwargs.update(
            prepare=prepare,
            interpret=interpret,
            exception=lambda *_a: pytest.fail("no exception provider in offline gate test"),
        )
        return original_run(state_path, targets, **kwargs)

    monkeypatch.setattr(pipeline, "run_pipeline", run_real_scoped)
    args = SimpleNamespace(
        state=str(tmp_path / "dispatch.json"),
        max_workers=1,
        shadow=False,
        coordinator_mode="resource_scoped",
    )
    receipt = dispatcher._run_staged_pipeline(args, [current["target"]], backend, time.time() + 330)
    assert calls["prepare"] == calls["interpret"] == 1, receipt["targets"][current["target"]]
    assert calls["external-effect"] == 0
    assert receipt["targets"][current["target"]]["status"] != "completed"
    assert path.read_bytes() == original
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


@pytest.mark.parametrize("damage", ["binding", "rootpath", "bytes", "results"])
def test_named_scope_refusal_requires_exact_binding_location_and_bytes(tmp_path, damage):
    _raw, path, _held = original_state(tmp_path)
    target = "owner/candidate#2"
    alias = "issue:owner/retained-source#7"
    events = Counter()
    cb = callbacks(events, aliases=[alias], during_prepare=lambda: advertisement(path, alias))
    receipt = run(
        path, [target], cb, lambda t, c: inventory(t, (c.get("prepare") or {}).get("aliases", []))
    )
    run_id = receipt["run_ids"][target]
    shard = resources.shard_dir(resources.scoped_root(path), target) / "state.json"
    row = json.loads(shard.read_text())["runs"][run_id]["targets"][target]
    assert row["deferred_reason"] == "scope_quarantined"
    assert resources.valid_scope_refusal(shard, target, row, run_id)
    proof_path = Path(row["scope_proof"]["path"])
    proof_raw = proof_path.read_bytes()
    if damage == "binding":
        proof = json.loads(proof_raw)
        proof["binding"]["work_key"] = "unrelated-callback"
        write(proof_path, proof)
        row["scope_proof"]["sha256"] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    elif damage == "rootpath":
        foreign = tmp_path / "outside-admission-root.json"
        foreign.write_bytes(proof_raw)
        row["scope_proof"]["path"] = str(foreign)
    elif damage == "results":
        row["results"]["prepare"]["revision"] = "rewritten-retained-input"
    else:
        proof_path.write_bytes(proof_raw + b" ")
    assert not resources.valid_scope_refusal(shard, target, row, run_id)
    assert events[(target, "interpret")] == events[(target, "simulated-effect")] == 0


def test_fresh_disjoint_preparation_can_continue_after_proven_scope_refusal(tmp_path):
    raw, path, held = original_state(tmp_path)
    target = "owner/candidate#2"
    alias = "issue:owner/retained-source#7"
    ad = advertisement(path, alias)
    originals = {p: p.read_bytes() for p in [raw, path, ad]}
    events = Counter()
    cb = callbacks(events)
    phase = {"held": True}
    base_prepare = cb["prepare"]

    def prepare(name, context):
        result = base_prepare(name, context)
        result.update(
            aliases=[alias] if phase["held"] else [],
            revision="held-revision" if phase["held"] else "new-disjoint-revision",
            semantic_cache_key="closer-evidence-semantic/v1:"
            + ("a" if phase["held"] else "b") * 64,
        )
        return result

    cb["prepare"] = prepare

    def key_callback(target, context):
        return inventory(target, (context.get("prepare") or {}).get("aliases", []))

    first = run(path, [target], cb, key_callback)
    assert first["targets"][target]["deferred_reason"] == "scope_quarantined"
    proof = first["targets"][target]["scope_proof"]
    proof_raw = Path(proof["path"]).read_bytes()
    assert events[(target, "interpret")] == events[(target, "simulated-effect")] == 0
    phase["held"] = False
    second = run(path, [target], cb, key_callback)
    assert second["targets"][target]["status"] == "completed", [
        (r.get("reason"), r.get("run_id"), r.get("source_path"))
        for r in quarantine.scope_snapshot(path)["items"]
        if r.get("target") == target
    ]
    assert events[(target, "interpret")] == events[(target, "simulated-effect")] == 1
    assert Path(proof["path"]).read_bytes() == proof_raw
    assert {p: p.read_bytes() for p in originals} == originals
    assert alias in quarantine.scope_snapshot(path)["quarantine_keys"]
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


@pytest.mark.parametrize("tamper_origin", [False, True])
def test_excluded_scope_refusal_retains_original_proof_before_rejoining(tmp_path, tamper_origin):
    _raw, path, held = original_state(tmp_path)
    target = "owner/candidate#2"
    alias = "issue:owner/retained-source#7"
    advertisement(path, alias)
    events = Counter()
    phase = {"held": True}
    cb = callbacks(events)
    base_prepare = cb["prepare"]

    def prepare(name, context):
        result = base_prepare(name, context)
        result["aliases"] = [alias] if name == target and phase["held"] else []
        return result

    cb["prepare"] = prepare

    def keys(t, c):
        return inventory(t, (c.get("prepare") or {}).get("aliases", []))

    first = run(path, [target], cb, keys)
    original_id = first["run_ids"][target]
    shard = resources.shard_dir(resources.scoped_root(path), target) / "state.json"
    original_row = json.loads(shard.read_text())["runs"][original_id]["targets"][target]
    proof = Path(original_row["scope_proof"]["path"])
    proof_bytes = proof.read_bytes()
    other = "owner/unrelated#3"
    assert run(path, [other], cb, keys)["targets"][other]["status"] == "completed"
    doc = json.loads(shard.read_text())
    assert doc["runs"][original_id]["targets"][target] == original_row
    if tamper_origin:
        doc["runs"][original_id]["targets"][target]["results"]["prepare"]["revision"] = "altered"
        write(shard, doc)
    phase["held"] = False
    final = run(path, [target], cb, keys)
    if tamper_origin:
        assert final["targets"][target]["status"] == "deferred"
        assert events[(target, "interpret")] == events[(target, "simulated-effect")] == 0
    else:
        assert final["targets"][target]["status"] == "completed"
        assert events[(target, "interpret")] == events[(target, "simulated-effect")] == 1
    assert proof.read_bytes() == proof_bytes
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


def test_disjoint_scoped_rounds_can_overlap_callbacks_without_weakening_old_quarantine(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    raw, path, held = original_state(tmp_path)
    ad = advertisement(path, "branch:owner/held@old-effect")
    originals = {p: p.read_bytes() for p in [raw, path, ad]}
    entered = threading.Barrier(2)
    events = Counter()

    def worker(target):
        cb = callbacks(events)
        original_interpret = cb["interpret"]

        def interpret(name, context):
            entered.wait(timeout=5)
            return original_interpret(name, context)

        cb["interpret"] = interpret
        return pipeline.run_pipeline(
            path,
            [target],
            **cb,
            resource_keys=lambda t, _c: inventory(t),
            coordinator_mode="resource_scoped",
            deadline=time.time() + 15,
            max_io_workers=2,
            max_low_workers=2,
            max_exception_workers=1,
            capacity_fn=lambda: {"io": 2, "low": 2, "exception": 1},
        )

    targets = ["owner/live-a#2", "owner/live-b#3"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(worker, targets))
    for target, receipt in zip(targets, receipts):
        assert receipt["targets"][target]["status"] == "completed", receipt["targets"][target]
        assert events[(target, "simulated-effect")] == 1
    assert {p: p.read_bytes() for p in originals} == originals
    assert not pipeline._validate_stop_legacy(path, held)["valid"]


@pytest.mark.parametrize("stage", ["act", "reconcile"])
def test_expired_named_unknown_is_stop_boundary_but_never_admitted_domain(tmp_path, stage):
    """A copied interrupted-action row cannot inherit scoped delivery authority."""
    _raw, path, _held = original_state(tmp_path)
    target = "owner/healthy#7"
    events = Counter()
    receipt = run(path, [target], callbacks(events), lambda t, _c: inventory(t))
    run_id = receipt["run_ids"][target]
    shard = resources.shard_dir(resources.scoped_root(path), target) / "state.json"
    doc = json.loads(shard.read_text())
    row = doc["runs"][run_id]["targets"][target]
    row.update(status="uncertain", stage=stage, deferred_next_trigger="reconcile_before_retry")
    row.pop("known_completed_action", None)
    row["results"].pop("act", None)
    write(shard, doc)

    def after_deadline():
        return receipt["deadline"] + 1

    legacy = pipeline._validate_stop_legacy(
        shard, [target], now=after_deadline, expected_run_id=run_id
    )
    assert legacy["valid"] is True, legacy
    verdict = resources.validate_stop(path, receipt["round_id"], now=after_deadline)
    assert verdict["admitted_domain_valid"] is False
    assert any(
        "unresolved_admitted_outcome:" + target in r for r in verdict["admitted_domain_reasons"]
    )
    assert verdict["global_stop_valid"] is False


def test_no_admitted_targets_reject_extraneous_recorded_run_ids(tmp_path):
    _raw, path, _held = original_state(tmp_path)
    target = "owner/held#1"
    events = Counter()
    receipt = run(path, [target], callbacks(events), lambda t, _c: inventory(t))
    assert receipt["run_ids"] == {}
    assert not resources.validate_stop(path, receipt["round_id"])["admitted_domain_valid"]
    recorded = resources.scoped_root(path) / "rounds" / (receipt["round_id"] + ".json")
    changed = json.loads(recorded.read_text())
    changed["run_ids"] = {"owner/unassigned#99": "unrelated-run"}
    write(recorded, changed)
    verdict = resources.validate_stop(path, receipt["round_id"])
    assert verdict["admitted_domain_valid"] is False
    assert verdict["global_stop_valid"] is False
    assert events == Counter()


@pytest.mark.parametrize("prior_reference", ["valid", "missing", "changed"])
def test_scoped_preparation_reads_own_hash_bound_packet_and_cache(
    tmp_path, monkeypatch, prior_reference
):
    import copy
    import shutil
    from types import SimpleNamespace

    import test_closer_dispatcher as runtime_fixture
    import test_closer_disposition as proof_fixture

    import closer_change_feed
    import closer_evidence

    runtime_fixture.no_live_state_or_provider.__wrapped__(monkeypatch, tmp_path)
    state = tmp_path / "shared-dispatch.json"
    write(state, {"schema": "closer-queue/v1", "items": {}, "round": {}})
    original_shared = state.read_bytes()
    original_collect = closer_evidence.collect_evidence

    def collect_fixture(*args, **kwargs):
        kwargs["action"] = "collect_evidence"
        return original_collect(*args, **kwargs)

    monkeypatch.setattr(closer_evidence, "collect_evidence", collect_fixture)
    current, packet, original_ref, _prep, _interpreted = proof_fixture.fixture(tmp_path)
    assert closer_evidence.validate_packet(packet)
    current.update(
        state="OPEN",
        draft=False,
        active_threads=0,
        mergeable="MERGEABLE",
        labels=[],
        head_repository="owner/repo",
        head_branch="feature",
    )
    target = current["target"]
    retained_dir = Path(str(state) + ".evidence")
    manifest = retained_dir / "manifests" / Path(original_ref["path"]).name
    manifest.parent.mkdir(parents=True)
    shutil.copyfile(original_ref["path"], manifest)
    calls = Counter()
    prepared = []
    cache_paths = []
    original_read = closer_change_feed.read_cached

    def read_cached(path, name):
        cache_paths.append(Path(path))
        return original_read(path, name)

    monkeypatch.setattr(closer_change_feed, "read_cached", read_cached)

    class OfflineAfterFirst(runtime_fixture.FakeGitHub):
        def snapshot(self, name):
            calls["remote-read"] += 1
            if calls["remote-read"] > 1:
                raise ConnectionError("fresh remote evidence unavailable")
            return super().snapshot(name)

    github = OfflineAfterFirst(copy.deepcopy(current))
    monkeypatch.setattr(dispatcher, "GitHub", lambda **_kwargs: github)
    monkeypatch.setattr(
        dispatcher, "_pipeline_capacity", lambda: {"io": 1, "low": 1, "exception": 1}
    )

    def collect(*_args, **kwargs):
        calls["collect"] += 1
        assert kwargs["previous"] is None
        return copy.deepcopy(packet)

    monkeypatch.setattr(closer_evidence, "collect_evidence", collect)
    real_run = pipeline.run_pipeline

    def run_prepare_only(path, targets, **kwargs):
        actual_prepare = kwargs["prepare"]

        def observe_prepare(name, context):
            result = actual_prepare(name, context)
            prepared.append(copy.deepcopy(result))
            return result

        kwargs.update(
            prepare=observe_prepare,
            interpret=lambda *_a: {"substantive_needs": []},
            exception=lambda *_a: pytest.fail("no provider needed for cache regression"),
            act=lambda *_a: {"status": "completed", "target_complete": True},
        )
        return real_run(path, targets, **kwargs)

    monkeypatch.setattr(pipeline, "run_pipeline", run_prepare_only)
    backend = dispatcher.Closer(github, state_path=state, apply=True)
    backend.local_first = True
    args = SimpleNamespace(
        state=str(state), max_workers=1, shadow=False, coordinator_mode="resource_scoped"
    )
    first = dispatcher._run_staged_pipeline(args, [target], backend, time.time() + 330)
    assert first["targets"][target]["status"] == "completed", first
    assert calls == Counter({"remote-read": 1, "collect": 1})
    assert prepared[0]["remote_fresh"] is True
    assert state.read_bytes() == original_shared
    if prior_reference == "missing":
        manifest.unlink()
    elif prior_reference == "changed":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    second = dispatcher._run_staged_pipeline(args, [target], backend, time.time() + 330)
    if prior_reference == "valid":
        assert second["targets"][target]["status"] == "completed", second
        assert calls == Counter({"remote-read": 1, "collect": 1})
        assert len(prepared) == 2
        assert prepared[-1]["snapshot"]["_local_cached"] is True
        assert prepared[-1]["remote_fresh"] is False
        assert prepared[-1]["packet_ref"] == prepared[0]["packet_ref"]
    else:
        assert second["targets"][target]["status"] != "completed"
        assert calls == Counter({"remote-read": 2, "collect": 1})
        assert len(prepared) == 1
    assert len(cache_paths) == 2
    assert cache_paths[0] == cache_paths[1]
    assert cache_paths[0] != state
    assert cache_paths[0].is_relative_to(
        resources.scoped_root(dispatcher._pipeline_state_path(state))
    )
    assert state.read_bytes() == original_shared
