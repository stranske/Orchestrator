import hashlib
from contextlib import nullcontext

import pytest

import closer_native_readmission as readmission
from closer_frontier_policy import make_frontier


def _frontier(frontier_id, targets=("target-1",)):
    return make_frontier(
        targets,
        owner="closer-owner",
        frontier_id=frontier_id,
        bindings={
            target: {"revision": "rev-" + target, "action_id": "a" * 64} for target in targets
        },
    )


def _reference(monkeypatch, tmp_path):
    import closer_host_restore_contract as host

    home = tmp_path / "home"
    monkeypatch.setattr(host.Path, "home", classmethod(lambda _cls: home))
    coord = home / ".codex/handoff/.lane-closer.lock.coord.lock"
    coord.parent.mkdir(parents=True)
    coord.write_bytes(b"")
    (tmp_path / "old-state.json.lock").write_bytes(b"")
    old_frontier = _frontier("old")
    raw = b'{"retained":"unavailable"}'
    old = {
        "campaign_id": "original",
        "owner": "closer-owner",
        "worker_config": {"finite_frontier": old_frontier},
    }
    proof = {"admission_nonce": "n" * 32}
    reference = {
        "manifest": {"package_digest": "b" * 64},
        "manifest_path": str(tmp_path / "old-manifest.json"),
        "state": old,
        "state_path": str(tmp_path / "old-state.json"),
        "state_sha256": "c" * 64,
        "worker": {"worker_id": "0123456789ab"},
        "result_path": str(tmp_path / "old-result.json"),
        "result_raw": raw,
        "result_sha256": hashlib.sha256(raw).hexdigest(),
        "proof": proof,
    }
    monkeypatch.setattr(readmission, "prospective_reference", lambda *_: reference)
    import closer_native_observer as native

    root = tmp_path / "native-observation"
    root.mkdir()
    (root / proof["admission_nonce"]).mkdir()
    for name in ("native-entry.lock", "native-supervisor.lock"):
        (root / name).write_bytes(b"")
    contract = {"campaign_root": str(tmp_path), "observation_root": str(root)}
    reference["manifest"]["mechanism_contract"] = contract
    monkeypatch.setattr(native, "contract", lambda _manifest: (None, None, None, root))
    new_manifest = {"package_digest": "d" * 64, "mechanism_contract": contract}
    new_manifest_path = tmp_path / "new-manifest.json"
    new_manifest_path.write_text("{}")
    new_raw = b'{"new":true}'
    cfg = {
        "runtime_manifest": str(new_manifest_path),
        "granularity": "round",
        "mechanism": "native-handoff/v1",
    }
    monkeypatch.setattr(
        readmission, "_new_runtime", lambda *_: (new_manifest_path, new_manifest, new_raw, cfg)
    )
    monkeypatch.setattr(readmission, "_native_locks", lambda _root: nullcontext())

    import closer_campaign as campaign

    class FakeCampaign:
        created = []

        def __init__(self, path):
            self.path = path

        def _locked(self):
            return nullcontext()

        def begin(self, *args, **kwargs):
            FakeCampaign.created.append((self.path, args, kwargs))

    monkeypatch.setattr(campaign, "Campaign", FakeCampaign)
    return old_frontier, raw, reference, new_manifest_path, cfg, FakeCampaign


def test_apply_consumes_one_proof_then_creates_distinct_campaign(monkeypatch, tmp_path):
    old_frontier, _raw, reference, manifest, cfg, fake = _reference(monkeypatch, tmp_path)
    fresh = _frontier("fresh")
    destination = tmp_path / "fresh-state.json"
    _plan, plan_sha = readmission._plan(
        reference, manifest, b'{"new":true}', cfg, destination, fresh
    )
    result = readmission.apply_prospective(
        "manifest",
        "old-state",
        "0123456789ab",
        manifest,
        cfg,
        destination,
        fresh,
        confirm_plan_sha256=plan_sha,
    )
    assert result["authority"] is False
    assert fake.created[0][2]["worker_config"]["finite_frontier"] == fresh
    with pytest.raises(readmission.ReadmissionHeld, match="already consumed"):
        readmission.apply_prospective(
            "manifest",
            "old-state",
            "0123456789ab",
            manifest,
            cfg,
            destination,
            fresh,
            confirm_plan_sha256=plan_sha,
        )
    assert old_frontier["frontier_id"] == "old"


def test_apply_rejects_target_drift_and_wrong_plan(monkeypatch, tmp_path):
    _old, _raw, _reference_value, manifest, cfg, _fake = _reference(monkeypatch, tmp_path)
    with pytest.raises(readmission.ReadmissionHeld, match="target scope"):
        readmission.apply_prospective(
            "manifest",
            "old",
            "0123456789ab",
            manifest,
            cfg,
            tmp_path / "new.json",
            _frontier("fresh", ("other",)),
            confirm_plan_sha256="0" * 64,
        )
    with pytest.raises(readmission.ReadmissionHeld, match="confirmation"):
        readmission.apply_prospective(
            "manifest",
            "old",
            "0123456789ab",
            manifest,
            cfg,
            tmp_path / "new.json",
            _frontier("fresh"),
            confirm_plan_sha256="0" * 64,
        )


def test_unknown_without_reconciliation_or_prospective_proof_is_held(tmp_path):
    with pytest.raises(readmission.ReadmissionHeld, match="signed prospective"):
        readmission.prospective_reference(tmp_path / "manifest", tmp_path / "missing", "worker")


def test_historical_reader_requires_exact_decision(monkeypatch):
    import sys
    from types import SimpleNamespace

    valid = {
        "schema": "native-retained-no-b-reconciliation/v1",
        "authority": False,
        "decision": {
            "status": "confirmed_no_b_current_observation",
            "authorize_fresh_prepare": True,
            "old_outcome": "unknown_preserved",
            "max_fresh_generations": 1,
        },
    }
    monkeypatch.setitem(
        sys.modules, "closer_retained_reconciliation", SimpleNamespace(validate=lambda *_: valid)
    )
    assert readmission.historical_reference("ref", "manifest", "state", "worker") == valid
    valid["decision"]["max_fresh_generations"] = 2
    with pytest.raises(readmission.ReadmissionHeld, match="does not authorize"):
        readmission.historical_reference("ref", "manifest", "state", "worker")
