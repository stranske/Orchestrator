"""Integration-shaped tests for retained no-B reconciliation.

The fixture deliberately uses the real HMAC verifier and real read-only
controller custody inspector.  Only GitHub's network transport is replaced.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

import closer_correction_controller as ctl
import closer_host_restore_contract as host
import closer_mechanisms as mechanisms
import closer_native_readmission as readmission
import closer_retained_reconciliation as reconcile
from closer_frontier_policy import make_frontier


def _write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    Path(path).chmod(0o600)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ref(path):
    return {"path": str(path), "sha256": _sha(path)}


def _world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(host.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(reconcile.Path, "home", classmethod(lambda _cls: home))
    root = tmp_path / "observation"
    root.mkdir(mode=0o700, parents=True)
    key = root / "supervisor.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    state = tmp_path / "campaign.json"
    result_path = Path(str(state) + ".results") / "worker.json"
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    module = module_dir / "one.py"
    _write(module, b"pass\n")
    nonce = "nonce"
    invocation = root / nonce
    invocation.mkdir(mode=0o700)
    config = {
        "state_path": str(state),
        "repository": "stranske/orchestrator",
        "repository_id": 1,
        "epoch": "e" * 64,
        "bootstrap_source_sha": "a" * 40,
        "source_sha": "f" * 40,
        "writer_id": 1,
        "writer_login": "tester",
        "writer_type": "User",
        "bootstrap_anchor_sha256": "0" * 64,
        "max_bytes": 100000,
        "max_entries": 10,
        "max_subjects": 10,
    }
    index = {
        "schema": ctl.SCHEMA,
        "repository": config["repository"],
        "repository_id": 1,
        "epoch": config["epoch"],
        "bootstrap_source_sha": config["bootstrap_source_sha"],
        "revision": 0,
        "previous_sha256": None,
        "last_action": None,
        "entries": [],
    }
    anchor = {
        "revision": 0,
        "commit_sha": "c" * 40,
        "blob_sha": "d" * 40,
        "state_sha256": ctl.digest(index),
        "previous_anchor_sha256": None,
        "result_name": None,
        "result_sha256": None,
    }
    config["bootstrap_anchor_sha256"] = ctl.digest(anchor)
    custody = Path(str(state) + ".evidence") / ctl.ControllerConfig(**config).namespace
    custody.mkdir(parents=True)
    custody.chmod(0o700)
    _write(
        custody / "namespace.json",
        ctl.canonical(
            {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": ctl.digest(ctl.ControllerConfig(**config).source_binding()),
            }
        ),
    )
    _write(custody / "highwater-0000000000000000.json", ctl.canonical(anchor))
    _write(custody / "writer.lock", b"")
    manifest_path = tmp_path / "host-manifest.json"
    manifest = {
        "candidate_sha": "f" * 40,
        "command_digest": "q" * 64,
        "runtime_digest": "r" * 64,
        "state_digest": "s" * 64,
        "state_versions": {"campaign": 1},
        "module_dir": str(module_dir),
        "modules": {"one.py": _sha(module)},
        "mechanism_contract": {"observation_root": str(root)},
        "policy": {"correction_controller_installation": {"config": config}},
        "package_digest": "1" * 64,
    }
    binding = {
        "mechanism": mechanisms.NATIVE,
        "mechanism_contract_digest": mechanisms.digest(manifest["mechanism_contract"]),
        "policy_digest": mechanisms.digest(manifest["policy"]),
        **{key: manifest.get(key) for key in mechanisms.PIN_KEYS},
    }
    admission = {
        "nonce": nonce,
        "schema": "native-invocation-admission/v2",
        "runtime_manifest": str(manifest_path),
    }
    fields = {"state": str(state), "result": str(result_path)}
    detail = {
        "actual_detail": "dispatcher outcome unknown: MechanismError; ACTION C65; preserve no_matching_pipeline_run and ledger",
        "native_execution": {
            "schema": "native-round-execution/v1",
            "admission_nonce": nonce,
            "worker": fields,
            "phases": {
                "A": {"admitted": True, "exit_code": 0, "token_sha256": "a" * 64, "lease_inode": 7},
                "B": {"started": False, "started_at": None, "finished_at": None},
                "C": {
                    "attempted": True,
                    "exit_code": 65,
                    "started_at": 1,
                    "finished_at": 3,
                    "cleanup": {
                        "schema": "closer-native-lease-cleanup/v1",
                        "lane": "closer",
                        "lane_dir": str(home / ".codex/handoff/.lane-closer.lock"),
                        "expected_token_sha256": "a" * 64,
                        "meta_sha256": "a" * 64,
                        "released_lease_inode": 7,
                        "exact_token_matched": True,
                        "released": True,
                        "released_at": 2,
                    },
                },
            },
        },
    }
    result = {
        "worker_id": "worker",
        "outcome": "unknown",
        "target": "*round*",
        "mechanism": mechanisms.NATIVE,
        "native_admission": admission,
        "detail": json.dumps(detail, sort_keys=True),
    }
    _write(result_path, result)
    records = {
        "admission.json": {"binding": binding, "context": admission},
        "worker-worker.json": {
            "schema": "native-worker-handoff/v1",
            "binding": binding,
            "nonce": nonce,
            "admission": admission,
            "fields": fields,
        },
        "terminal.json": {
            "schema": "native-wrapper-lifecycle/v1",
            "source": "owned-native-wrapper-process",
            "passed": True,
            "binding": binding,
            "nonce": nonce,
            "wrapper": {"pid": 11},
            "entry": {"pid": 12},
            "tick_result": [{"kind": "launched", "worker_id": "worker", "pid": 13}],
        },
    }
    for name, record in records.items():
        _write(
            invocation / name,
            {"record": record, "signature": mechanisms._signature(record, key.read_bytes())},
        )
    for name in (
        "assignment.json",
        "scoped-capacity.json",
        "scoped-capacity.json.lock",
        "startup.json",
    ):
        _write(invocation / name, b"{}")
    _write(manifest_path, manifest)
    admission["runtime_manifest"] = str(manifest_path)
    # Re-sign the records after finalizing the context's manifest path.
    for name, record in records.items():
        if "context" in record:
            record["context"] = admission
        if "admission" in record:
            record["admission"] = admission
        _write(
            invocation / name,
            {"record": record, "signature": mechanisms._signature(record, key.read_bytes())},
        )
    active_nonce = "a" * 32
    active_directory = root / active_nonce
    active_directory.mkdir(mode=0o700)
    active_terminal = {**records["terminal.json"], "nonce": active_nonce}
    _write(
        active_directory / "terminal.json",
        {
            "record": active_terminal,
            "signature": mechanisms._signature(active_terminal, key.read_bytes()),
        },
    )
    _write(
        root / "native-active.json",
        {"status": "terminal", "nonce": active_nonce, "binding": binding},
    )
    owner = "old-owner"
    frontier = make_frontier(
        ["stranske/Orchestrator#984"],
        owner=owner,
        frontier_id="old",
        bindings={"stranske/Orchestrator#984": {"revision": "r1", "action_id": "1" * 64}},
    )
    admission["campaign"] = {
        "state": str(state),
        "campaign_id": "campaign",
        "owner": owner,
        "generation": 1,
    }
    records["admission.json"]["context"] = admission
    records["worker-worker.json"]["admission"] = admission
    records["worker-worker.json"]["fields"]["owner"] = owner
    detail["native_execution"]["worker"] = fields
    result["native_admission"] = admission
    result["detail"] = json.dumps(detail, sort_keys=True)
    _write(result_path, result)
    for name, record in records.items():
        _write(
            invocation / name,
            {"record": record, "signature": mechanisms._signature(record, key.read_bytes())},
        )
    campaign = {
        "targets": {"*round*": "reconcile"},
        "workers": [
            {
                "worker_id": "worker",
                "state": "terminal",
                "outcome": "unknown",
                "pid": 13,
                "detail": result["detail"],
            }
        ],
        "owner": owner,
        "worker_config": {
            "runtime_manifest": str(manifest_path),
            "finite_frontier": frontier,
            "dispatch_state": str(tmp_path / "dispatch"),
        },
        "correction_controller": {"config": config},
        "campaign_id": "campaign",
        "revision": 1,
        "generation": 1,
    }
    _write(state, campaign)
    for leaf in ("native-entry.lock", "native-supervisor.lock"):
        _write(root / leaf, b"")
    _write(state.with_name(state.name + ".lock"), b"")
    _write(home / ".codex/handoff/.lane-closer.lock.coord.lock", b"")
    monkeypatch.setattr(host, "_dead", lambda _pid: None)
    monkeypatch.setattr(reconcile, "CANDIDATE", binding["candidate_sha"])
    monkeypatch.setattr(reconcile, "WORKER", "worker")
    monkeypatch.setattr(reconcile, "NONCE", nonce)
    monkeypatch.setattr(reconcile, "MODULES", {})
    monkeypatch.setattr(
        reconcile,
        "MODULE_SET_SHA256",
        hashlib.sha256(
            json.dumps(manifest["modules"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(reconcile, "MANIFEST_SHA256", _sha(manifest_path))
    monkeypatch.setattr(reconcile, "HEAD", "h" * 40)
    monkeypatch.setattr(reconcile, "STATE_REVISION", campaign["revision"])
    monkeypatch.setattr(reconcile, "STATE_SHA256", _sha(state))
    monkeypatch.setattr(reconcile, "RESULT_SHA256", _sha(result_path))

    def gh(endpoint):
        content = base64.b64encode(ctl.canonical(index)).decode()
        rows = {
            "pulls/989": {
                "number": 989,
                "state": "open",
                "draft": False,
                "head": {"sha": "h" * 40},
                "body": "#988",
            },
            "issues/988": {"number": 988, "state": "open", "body": "#984"},
            "pulls/984": {
                "number": 984,
                "merged": True,
                "merge_commit_sha": "d1947b5b4775c2e6a07bb46b3f85eb2e28c1b001",
            },
            "git/ref/heads/"
            + ctl.BRANCH: {
                "ref": "refs/heads/" + ctl.BRANCH,
                "object": {"sha": anchor["commit_sha"]},
            },
            "contents/"
            + ctl.INDEX_PATH
            + "?ref="
            + anchor["commit_sha"]: {
                "sha": anchor["blob_sha"],
                "encoding": "base64",
                "size": len(ctl.canonical(index)),
                "content": content,
            },
        }
        return rows[endpoint]

    monkeypatch.setattr(reconcile, "_gh", gh)
    return manifest_path, state, result_path, custody


def test_plan_and_seal_preserve_unknown_bytes(tmp_path, monkeypatch):
    manifest, state, result, _ = _world(tmp_path, monkeypatch)
    before = result.read_bytes()
    plan = reconcile.plan(manifest, state, "worker")
    sealed = reconcile.collect(manifest, state, "worker")
    assert plan["sealed"] is False and sealed["sealed"] is True and result.read_bytes() == before
    reference = {key: sealed[key] for key in ("path", "sha256")}
    assert reconcile.validate(reference, manifest, state, "worker") == sealed["record"]
    result.write_bytes(before + b" ")
    with pytest.raises((ValueError, RuntimeError)):
        reconcile.validate(reference, manifest, state, "worker")


@pytest.mark.parametrize("changed", ["revision", "unrelated_state_field", "result_encoding"])
def test_first_observation_requires_exact_reviewed_census(tmp_path, monkeypatch, changed):
    manifest, state, result, _ = _world(tmp_path, monkeypatch)
    if changed == "result_encoding":
        # Even the same parsed result is not the original retained byte stream.
        result.write_bytes(result.read_bytes() + b" ")
    else:
        document = json.loads(state.read_bytes())
        if changed == "revision":
            document["revision"] += 1
        else:
            document["unreviewed"] = "all checked lifecycle fields remain identical"
        _write(state, document)
    before = state.read_bytes(), result.read_bytes()
    with pytest.raises(reconcile.ReconciliationError, match="reviewed original census"):
        reconcile.collect(manifest, state, "worker")
    assert (state.read_bytes(), result.read_bytes()) == before
    assert not list(tmp_path.rglob("retained-no-b-current-*.json"))


def test_duplicate_collect_and_changing_github_snapshot_refuse(tmp_path, monkeypatch):
    manifest, state, _, _ = _world(tmp_path, monkeypatch)
    reconcile.collect(manifest, state, "worker")
    with pytest.raises(FileExistsError):
        reconcile.collect(manifest, state, "worker")
    manifest, state, _, _ = _world(tmp_path / "changed", monkeypatch)
    original = reconcile._gh
    calls = 0

    def changing(endpoint):
        nonlocal calls
        value = dict(original(endpoint))
        calls += 1
        if endpoint == "pulls/989" and calls > 5:
            value["body"] = "#988 changed"
        return value

    monkeypatch.setattr(reconcile, "_gh", changing)
    with pytest.raises(reconcile.ReconciliationError):
        reconcile.plan(manifest, state, "worker")


def test_validate_allows_only_explicit_hash_pinned_preparation_intent(tmp_path, monkeypatch):
    manifest, state, result, _ = _world(tmp_path, monkeypatch)
    sealed = reconcile.collect(manifest, state, "worker")
    root = json.loads(manifest.read_text())["mechanism_contract"]["observation_root"]
    intent = Path(root) / "nonce" / ("readmission-reconciled-" + _sha(result) + ".json")
    _write(intent, {"schema": "closer-native-readmission/v1", "stage": "preparation_intent"})
    reference = {key: sealed[key] for key in ("path", "sha256")}
    exact = _ref(intent)
    with pytest.raises(reconcile.ReconciliationError):
        reconcile.validate(reference, manifest, state, "worker")
    assert (
        reconcile.validate(reference, manifest, state, "worker", allowed_intent=exact)
        == sealed["record"]
    )
    intent.write_text('{"changed":true}')
    with pytest.raises(reconcile.ReconciliationError):
        reconcile.validate(reference, manifest, state, "worker", allowed_intent=exact)


def test_historical_consumer_creates_one_fresh_campaign(tmp_path, monkeypatch):
    manifest, state, result, _ = _world(tmp_path, monkeypatch)
    sealed = reconcile.collect(manifest, state, "worker")
    old = json.loads(state.read_text())
    old_bytes, result_bytes = state.read_bytes(), result.read_bytes()
    fresh = make_frontier(
        ["stranske/Orchestrator#984"],
        owner="old-owner",
        frontier_id="fresh",
        bindings={"stranske/Orchestrator#984": {"revision": "r1", "action_id": "1" * 64}},
    )
    new_root = tmp_path / "new-campaigns"
    new_root.mkdir()
    new_obs = tmp_path / "new-observation"
    new_obs.mkdir()
    for leaf in ("native-entry.lock", "native-supervisor.lock"):
        _write(new_obs / leaf, b"")
    new_manifest = {
        "package_digest": "new-package",
        "mechanism_contract": {"campaign_root": str(new_root), "observation_root": str(new_obs)},
    }
    new_path = tmp_path / "new-manifest.json"
    _write(new_path, new_manifest)
    cfg = {
        "runtime_manifest": str(new_path),
        "mechanism": mechanisms.NATIVE,
        "granularity": "round",
    }
    monkeypatch.setattr(
        readmission,
        "_new_runtime",
        lambda _p, _c: (new_path, new_manifest, new_path.read_bytes(), cfg),
    )
    destination = new_root / "fresh.campaign.json"
    proposal = readmission.plan_historical(
        sealed, manifest, state, "worker", new_path, cfg, destination, fresh
    )
    outcome = readmission.apply_historical(
        sealed,
        manifest,
        state,
        "worker",
        new_path,
        cfg,
        destination,
        fresh,
        confirm_plan_sha256=proposal["plan_sha256"],
    )
    assert (
        outcome["new_state"] == str(destination)
        and state.read_bytes() == old_bytes
        and result.read_bytes() == result_bytes
    )
    created = json.loads(destination.read_text())
    assert created["owner"] == old["owner"] and created["worker_config"]["finite_frontier"] == fresh
    with pytest.raises(readmission.ReadmissionHeld):
        readmission.apply_historical(
            sealed,
            manifest,
            state,
            "worker",
            new_path,
            cfg,
            new_root / "second.campaign.json",
            fresh,
            confirm_plan_sha256=proposal["plan_sha256"],
        )


def test_historical_plan_refuses_invalid_reconciliation_signature(tmp_path, monkeypatch):
    manifest, state, _result, _ = _world(tmp_path, monkeypatch)
    sealed = reconcile.collect(manifest, state, "worker")
    envelope = json.loads(Path(sealed["path"]).read_text())
    envelope["signature"] = "bad"
    os.chmod(sealed["path"], 0o600)
    Path(sealed["path"]).write_text(json.dumps(envelope))
    new_root = tmp_path / "new-campaigns"
    new_root.mkdir()
    new_obs = tmp_path / "new-observation"
    new_obs.mkdir()
    for leaf in ("native-entry.lock", "native-supervisor.lock"):
        _write(new_obs / leaf, b"")
    new_manifest = {
        "package_digest": "new-package",
        "mechanism_contract": {"campaign_root": str(new_root), "observation_root": str(new_obs)},
    }
    new_path = tmp_path / "new-manifest.json"
    _write(new_path, new_manifest)
    cfg = {
        "runtime_manifest": str(new_path),
        "mechanism": mechanisms.NATIVE,
        "granularity": "round",
    }
    monkeypatch.setattr(
        readmission,
        "_new_runtime",
        lambda _p, _c: (new_path, new_manifest, new_path.read_bytes(), cfg),
    )
    fresh = make_frontier(
        ["stranske/Orchestrator#984"],
        owner="old-owner",
        frontier_id="fresh",
        bindings={"stranske/Orchestrator#984": {"revision": "r1", "action_id": "1" * 64}},
    )
    with pytest.raises(readmission.ReadmissionHeld):
        readmission.plan_historical(
            sealed,
            manifest,
            state,
            "worker",
            new_path,
            cfg,
            new_root / "fresh.campaign.json",
            fresh,
        )


@pytest.mark.parametrize("kind", ["sink", "custody", "result", "remote"])
def test_reconciliation_refuses_changed_boundaries(tmp_path, monkeypatch, kind):
    manifest, state, result, custody = _world(tmp_path, monkeypatch)
    if kind == "sink":
        Path(str(tmp_path / "dispatch") + ".pipeline.json").write_text("occupied")
    elif kind == "custody":
        (custody / "extra.json").write_text("{}")
    elif kind == "result":
        result.write_text("{}")
    else:
        monkeypatch.setattr(reconcile, "HEAD", "z" * 40)
    with pytest.raises(
        (reconcile.ReconciliationError, host.Refused, ValueError, KeyError, TypeError)
    ):
        reconcile.plan(manifest, state, "worker")
