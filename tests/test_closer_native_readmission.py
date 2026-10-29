import hashlib
import json
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_native_readmission as readmission
from closer_frontier_policy import action_identity, make_frontier


def _initialized_world(monkeypatch, tmp_path):
    """Real held campaign bytes; only qualification/custody/remote seams are fake."""
    import closer_campaign as campaign
    import closer_correction_controller as controller
    import closer_dispatcher as dispatcher
    import closer_queue as queue
    import closer_runtime_binding as binding
    import closer_source_installation as installation

    target = "source/repo#984"
    observed = {
        "target": target,
        "action": "review",
        "eligible": True,
        "revision": "head-1",
        "operation_binding": {"source": "fixture"},
    }
    frontier = make_frontier(
        [target],
        owner="source-owner",
        frontier_id="fresh-authority",
        bindings={
            target: {
                "revision": observed["revision"],
                "action_id": action_identity(
                    target, observed["revision"], observed["action"], observed["operation_binding"]
                ),
            }
        },
    )
    state = tmp_path / "campaigns" / "fresh.campaign.json"
    root = tmp_path / "native-observation"
    root.mkdir()
    for name in ("native-entry.lock", "native-supervisor.lock"):
        (root / name).write_bytes(b"")
    manifest_path = tmp_path / "runtime" / "host-manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text("{}")
    config = {"state_path": str(state), "bootstrap_anchor_sha256": "a" * 64}
    manifest = {
        "package_digest": "b" * 64,
        "modules": {
            "closer_native_readmission.py": hashlib.sha256(
                Path(readmission.__file__).read_bytes()
            ).hexdigest()
        },
        "policy": {"correction_controller_installation": {"config": config}},
        "mechanism_contract": {
            "campaign_root": str(state.parent),
            "observation_root": str(root),
            "controller_demand": {
                "schema": "closer-controller-demand/v1",
                "campaign_state": str(state),
                "campaign_id": "fresh-campaign",
                "owner": "source-owner",
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    home = tmp_path / "home"
    monkeypatch.setattr(readmission.Path, "home", classmethod(lambda _cls: home))
    coord = home / ".codex/handoff/.lane-closer.lock.coord.lock"
    coord.parent.mkdir(parents=True)
    coord.write_bytes(b"")
    custody = tmp_path / "custody"
    custody.mkdir()
    (custody / "writer.lock").write_bytes(b"")
    inspection = {
        "identity": {"custody_path": str(custody)},
        "highwater": {"pending_transactions": []},
    }
    monkeypatch.setattr(campaign, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(campaign, "qualify", lambda *_a, **_k: [])
    monkeypatch.setattr(controller, "inspect_provisioned_custody", lambda _config: inspection)
    monkeypatch.setattr(binding, "binding_from_manifest", lambda _path: object())
    monkeypatch.setattr(
        installation,
        "controller_policy_projection",
        lambda _binding: {"purpose": "frontier-correlation-only"},
    )
    monkeypatch.setattr(dispatcher, "GitHub", lambda **_kwargs: object())
    monkeypatch.setattr(
        dispatcher,
        "Closer",
        lambda *_a, **_k: SimpleNamespace(observe=lambda _target: dict(observed)),
    )
    monkeypatch.setattr(queue, "_observation", lambda value, _target: value)
    (root / "native-qualification.json").write_text("qualified")
    cfg = {
        "granularity": "round",
        "mechanism": "native-handoff/v1",
        "runtime_manifest": str(manifest_path),
        "dispatch_state": str(tmp_path / "dispatch.json"),
        "finite_frontier": frontier,
    }
    initialization = readmission._initialization(manifest, state)
    campaign.Campaign(state).begin(
        "fresh-campaign",
        [target],
        owner="source-owner",
        release_pin=manifest["package_digest"],
        worker_config=cfg,
        _source_initialization=initialization,
    )
    now = time.time()
    assignment = json.dumps(
        {
            "generation": 1,
            "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "errors": [],
            "targets": [{"target": target, "due": now - 1}],
        },
        sort_keys=True,
    ).encode()
    package = {
        "package_digest": manifest["package_digest"],
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "qualification_digest": hashlib.sha256(
            (root / "native-qualification.json").read_bytes()
        ).hexdigest(),
    }
    request = {
        "schema": "closer-demand-request/v1",
        "campaign_id": "fresh-campaign",
        "owner": "source-owner",
        "campaign_generation": 1,
        "expected_revision": 1,
        "assignment_generation": 1,
        "assignment_sha256": hashlib.sha256(assignment).hexdigest(),
        **package,
        "not_before": now - 1,
        "deadline": now + 60,
        "targets": [target],
    }
    return SimpleNamespace(
        state=state,
        manifest_path=manifest_path,
        manifest=manifest,
        assignment=assignment,
        request=request,
        observed=observed,
        root=root,
    )


def test_admit_initialized_atomically_clears_only_matching_source_hold(monkeypatch, tmp_path):
    world = _initialized_world(monkeypatch, tmp_path)
    before = world.state.read_bytes()
    result = readmission.admit_initialized(
        world.manifest_path, world.state, world.assignment, world.request
    )
    after = __import__("closer_campaign").Campaign(world.state).read()
    assert result["status"] == "source_demand_admitted"
    assert (
        after["hold"] is None
        and after["demand_admission"]["schema"] == "closer-demand-admission/v1"
    )
    assert after["revision"] == 2 and before != world.state.read_bytes()


@pytest.mark.parametrize("failure", ["projection", "manifest", "action", "revision"])
def test_admit_initialized_refusals_preserve_real_held_campaign_bytes(
    monkeypatch, tmp_path, failure
):
    world = _initialized_world(monkeypatch, tmp_path)
    before = world.state.read_bytes()
    if failure == "projection":
        import closer_source_installation as installation

        monkeypatch.setattr(installation, "controller_policy_projection", lambda _binding: None)
    elif failure == "manifest":
        import closer_campaign as campaign

        def qualify(*_a, **_k):
            world.manifest_path.write_text('{"changed":true}')
            return []

        monkeypatch.setattr(campaign, "qualify", qualify)
    else:
        changed = dict(world.observed)
        if failure == "action":
            changed["action"] = "different"
        else:
            changed["revision"] = "head-2"
        import closer_dispatcher as dispatcher

        monkeypatch.setattr(
            dispatcher,
            "Closer",
            lambda *_a, **_k: SimpleNamespace(observe=lambda _target: changed),
        )
    with pytest.raises(readmission.ReadmissionHeld):
        readmission.admit_initialized(
            world.manifest_path, world.state, world.assignment, world.request
        )
    assert world.state.read_bytes() == before


def _admit_cli_files(world, tmp_path):
    assignment = tmp_path / "owned-assignment.json"
    request = tmp_path / "owned-request.json"
    assignment.write_bytes(world.assignment)
    request.write_text(json.dumps(world.request, sort_keys=True))
    return assignment, request


def test_admit_cli_passes_owned_input_bytes_and_reports_success_without_launch(
    monkeypatch, tmp_path, capsys
):
    world = _initialized_world(monkeypatch, tmp_path)
    assignment, request = _admit_cli_files(world, tmp_path)
    state_before = world.state.read_bytes()
    assignment_before = assignment.read_bytes()
    request_before = request.read_bytes()
    seen = {}

    def admitted(manifest, state, assignment_bytes, request_value):
        seen.update(
            manifest=manifest,
            state=state,
            assignment_bytes=assignment_bytes,
            request=request_value,
        )
        return {"authority": False, "status": "source_demand_admitted", "revision": 2}

    monkeypatch.setattr(readmission, "admit_initialized", admitted)
    exit_code = readmission.main(
        [
            "admit",
            "--manifest",
            str(world.manifest_path),
            "--state",
            str(world.state),
            "--assignment",
            str(assignment),
            "--request",
            str(request),
        ]
    )

    assert exit_code == 0
    assert seen == {
        "manifest": world.manifest_path,
        "state": world.state,
        "assignment_bytes": assignment_before,
        "request": json.loads(request_before),
    }
    assert json.loads(capsys.readouterr().out) == {
        "authority": False,
        "revision": 2,
        "status": "source_demand_admitted",
    }
    assert world.state.read_bytes() == state_before
    assert assignment.read_bytes() == assignment_before
    assert request.read_bytes() == request_before


def test_admit_cli_reports_held_without_changing_owned_inputs(monkeypatch, tmp_path, capsys):
    world = _initialized_world(monkeypatch, tmp_path)
    assignment, request = _admit_cli_files(world, tmp_path)
    before = (world.state.read_bytes(), assignment.read_bytes(), request.read_bytes())

    def held(*_args):
        raise readmission.ReadmissionHeld("source gate held")

    monkeypatch.setattr(readmission, "admit_initialized", held)
    exit_code = readmission.main(
        [
            "admit",
            "--manifest",
            str(world.manifest_path),
            "--state",
            str(world.state),
            "--assignment",
            str(assignment),
            "--request",
            str(request),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "authority": False,
        "reason": "source gate held",
        "status": "held",
    }
    assert (world.state.read_bytes(), assignment.read_bytes(), request.read_bytes()) == before


def test_guard_fresh_keeps_generic_bindings_exact_but_allows_controlled_action_rebind(
    monkeypatch, tmp_path
):
    """Only a controller-bound campaign may replace an action identity at one revision."""
    import claims
    import closer_host_restore_contract as host

    destination = tmp_path / "campaigns" / "fresh.campaign.json"
    destination.parent.mkdir()
    old = _frontier("old", ("source/repo#984",))
    fresh = json.loads(json.dumps(old))
    fresh["frontier_id"] = "fresh"
    fresh["bindings"]["source/repo#984"]["action_id"] = "f" * 64
    cfg = {"granularity": "round", "mechanism": "native-handoff/v1"}
    monkeypatch.setattr(host, "_lane_absent", lambda: None)
    monkeypatch.setattr(claims, "holder", lambda _target: None)

    generic = {"mechanism_contract": {"campaign_root": str(destination.parent)}}
    with pytest.raises(readmission.ReadmissionHeld, match="target scope"):
        readmission._guard_fresh(generic, cfg, destination, fresh, old)

    controlled = {
        "mechanism_contract": {
            "campaign_root": str(destination.parent),
            "controller_demand": {
                "schema": "closer-controller-demand/v1",
                "campaign_state": str(destination),
                "campaign_id": "fresh-campaign",
                "owner": "closer-owner",
            },
        },
        "policy": {
            "correction_controller_installation": {
                "config": {
                    "state_path": str(destination),
                    "bootstrap_anchor_sha256": "a" * 64,
                }
            }
        },
    }
    readmission._guard_fresh(controlled, cfg, destination, fresh, old)

    revision_drift = json.loads(json.dumps(fresh))
    revision_drift["bindings"]["source/repo#984"]["revision"] = "different-head"
    with pytest.raises(readmission.ReadmissionHeld, match="target scope"):
        readmission._guard_fresh(controlled, cfg, destination, revision_drift, old)

    owner_drift = json.loads(json.dumps(fresh))
    owner_drift["owner"] = "other-owner"
    with pytest.raises(readmission.ReadmissionHeld, match="owner changed"):
        readmission._guard_fresh(controlled, cfg, destination, owner_drift, old)


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

    RealCampaign = campaign.Campaign

    class FakeCampaign(RealCampaign):
        created = []

        def begin(self, *args, **kwargs):
            FakeCampaign.created.append((self.path, args, kwargs))
            return super().begin(*args, **kwargs)

    monkeypatch.setattr(campaign, "Campaign", FakeCampaign)
    return old_frontier, raw, reference, new_manifest_path, cfg, FakeCampaign


def test_apply_consumes_one_proof_then_creates_distinct_campaign(monkeypatch, tmp_path):
    old_frontier, _raw, reference, manifest, cfg, fake = _reference(monkeypatch, tmp_path)
    fresh = _frontier("fresh")
    destination = tmp_path / "fresh.campaign.json"
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
            tmp_path / "new.campaign.json",
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
            tmp_path / "new.campaign.json",
            _frontier("fresh"),
            confirm_plan_sha256="0" * 64,
        )


def test_unknown_without_reconciliation_or_prospective_proof_is_held(tmp_path):
    with pytest.raises(readmission.ReadmissionHeld, match="signed prospective"):
        readmission.prospective_reference(tmp_path / "manifest", tmp_path / "missing", "worker")


def test_new_runtime_requires_manifest_pin_for_executing_readmission_module(monkeypatch, tmp_path):
    import closer_campaign as campaign
    import closer_native_observer as native
    import closer_runtime_binding as binding

    manifest_path = tmp_path / "runtime" / "host-manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text("{}")
    manifest = {
        "modules": {
            Path(module.__file__)
            .name: hashlib.sha256(Path(module.__file__).read_bytes())
            .hexdigest()
            for module in (native, binding, readmission)
        }
    }
    monkeypatch.setattr(campaign, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(native, "contract", lambda _manifest: None)
    cfg = {"runtime_manifest": str(manifest_path)}
    assert readmission._new_runtime(manifest_path, cfg)[0] == manifest_path
    manifest["modules"]["closer_native_readmission.py"] = "0" * 64
    with pytest.raises(readmission.ReadmissionHeld, match="executing readmission"):
        readmission._new_runtime(manifest_path, cfg)


def test_failed_creation_retains_one_use_intent_without_claiming_success(monkeypatch, tmp_path):
    import json

    _old, _raw, reference, manifest, cfg, fake = _reference(monkeypatch, tmp_path)
    fresh = _frontier("fresh")
    destination = tmp_path / "fresh.campaign.json"
    planned = readmission.plan_prospective(
        "manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh
    )

    def failed_begin(*_args, **_kwargs):
        raise RuntimeError("creation storage failure")

    monkeypatch.setattr(fake, "begin", failed_begin)
    inputs = ("manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh)
    with pytest.raises(RuntimeError, match="creation storage failure"):
        readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])
    reservation = (
        tmp_path
        / "native-observation"
        / reference["proof"]["admission_nonce"]
        / ("readmission-" + reference["result_sha256"] + ".json")
    )
    intent = json.loads(reservation.read_bytes())
    assert intent["stage"] == "preparation_intent" and intent["authority"] is False
    assert not destination.exists()
    changed_frontier = _frontier("different")
    changed_plan = readmission.plan_prospective(
        "manifest", "old-state", "0123456789ab", manifest, cfg, destination, changed_frontier
    )
    with pytest.raises(readmission.ReadmissionHeld, match="intent differs"):
        readmission.apply_prospective(
            "manifest",
            "old-state",
            "0123456789ab",
            manifest,
            cfg,
            destination,
            changed_frontier,
            confirm_plan_sha256=changed_plan["plan_sha256"],
        )
    # An intent without a state is a write-ahead retry, not a consumed proof.
    monkeypatch.setattr(fake, "begin", fake.__mro__[1].begin)
    readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])
    assert destination.is_file()
    with pytest.raises(readmission.ReadmissionHeld, match="already consumed"):
        readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])


def test_postwrite_resume_seals_exact_held_campaign_without_second_begin(monkeypatch, tmp_path):
    _old, _raw, reference, manifest, cfg, fake = _reference(monkeypatch, tmp_path)
    fresh = _frontier("fresh")
    destination = tmp_path / "fresh.campaign.json"
    planned = readmission.plan_prospective(
        "manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh
    )
    import closer_native_observer as native

    actual_immutable = native._immutable
    calls = []

    def fail_only_known(path, raw):
        calls.append(path)
        if str(path).endswith(".known.json"):
            raise OSError("simulated crash after campaign creation")
        return actual_immutable(path, raw)

    monkeypatch.setattr(native, "_immutable", fail_only_known)
    inputs = ("manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh)
    with pytest.raises(OSError, match="after campaign creation"):
        readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])
    assert destination.is_file() and len(fake.created) == 1
    monkeypatch.setattr(native, "_immutable", actual_immutable)
    result = readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])
    assert result["known"].endswith(".known.json") and len(fake.created) == 1


def test_interrupted_preparation_refuses_noninitial_state(monkeypatch, tmp_path):
    import closer_native_observer as native

    _old, _raw, _reference_value, manifest, cfg, fake = _reference(monkeypatch, tmp_path)
    fresh = _frontier("fresh")
    destination = tmp_path / "fresh.campaign.json"
    planned = readmission.plan_prospective(
        "manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh
    )
    inputs = ("manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh)
    original = native._immutable

    def fail_before_known(path, raw):
        if path.name.endswith(".known.json"):
            raise OSError("creation receipt unavailable")
        return original(path, raw)

    monkeypatch.setattr(native, "_immutable", fail_before_known)
    with pytest.raises(OSError, match="creation receipt unavailable"):
        readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])
    monkeypatch.setattr(native, "_immutable", original)
    # A created worker is never recoverable as a preparation, even if its intent matches.
    document = json.loads(destination.read_text())
    document["workers"] = [{"worker_id": "different", "state": "started"}]
    destination.write_text(json.dumps(document))
    before = destination.read_bytes()
    with pytest.raises(readmission.ReadmissionHeld, match="not the exact untouched preparation"):
        readmission.apply_prospective(*inputs, confirm_plan_sha256=planned["plan_sha256"])
    assert destination.read_bytes() == before
    assert len(fake.created) == 1


def test_existing_unowned_destination_refuses_before_writing_intent(monkeypatch, tmp_path):
    _old, _raw, reference, manifest, cfg, _fake = _reference(monkeypatch, tmp_path)
    fresh = _frontier("fresh")
    destination = tmp_path / "fresh.campaign.json"
    destination.write_text("{}")
    planned = readmission.plan_prospective(
        "manifest", "old-state", "0123456789ab", manifest, cfg, destination, fresh
    )
    with pytest.raises(readmission.ReadmissionHeld, match="unowned fresh campaign"):
        readmission.apply_prospective(
            "manifest",
            "old-state",
            "0123456789ab",
            manifest,
            cfg,
            destination,
            fresh,
            confirm_plan_sha256=planned["plan_sha256"],
        )
    reservation = (
        tmp_path
        / "native-observation"
        / reference["proof"]["admission_nonce"]
        / ("readmission-" + reference["result_sha256"] + ".json")
    )
    assert not reservation.exists()


def _preparation_receipts(
    world,
    document,
    *,
    flat_plan=False,
    token_overrides=None,
    plan_overrides=None,
    known_overrides=None,
):
    """Write one complete immutable preparation pair with targeted mutations."""
    manifest_raw = world.manifest_path.read_bytes()
    producer = hashlib.sha256(Path(readmission.__file__).read_bytes()).hexdigest()
    binding = {
        "producer_sha256": producer,
        "manifest": str(world.manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
    }
    plan = {"producer_sha256": producer}
    if flat_plan:
        plan.update(
            {
                "new_manifest": binding["manifest"],
                "new_manifest_sha256": binding["manifest_sha256"],
            }
        )
    else:
        plan["new"] = {
            "manifest": binding["manifest"],
            "manifest_sha256": binding["manifest_sha256"],
        }
    plan.update(plan_overrides or {})
    token = {
        "schema": "closer-native-readmission/v1",
        "stage": "preparation_intent",
        "authority": False,
        **binding,
        "plan": plan,
    }
    token.update(token_overrides or {})
    intent = world.root / "intent.json"
    intent.write_text(json.dumps(token, sort_keys=True))
    document["preparation_origin"] = {
        "path": str(intent),
        "sha256": hashlib.sha256(intent.read_bytes()).hexdigest(),
    }
    world.state.write_text(json.dumps(document, sort_keys=True))
    known = {
        "schema": "closer-native-readmission/v1",
        "stage": "preparation_known",
        "authority": False,
        "intent_sha256": document["preparation_origin"]["sha256"],
        **binding,
        "state": str(world.state),
        "state_sha256": hashlib.sha256(world.state.read_bytes()).hexdigest(),
    }
    known.update(known_overrides or {})
    readmission._known_path(intent).write_text(json.dumps(known, sort_keys=True))
    return intent, token, known


def test_preparation_known_requires_exact_origin_and_initial_state(monkeypatch, tmp_path):
    world = _initialized_world(monkeypatch, tmp_path)
    document = json.loads(world.state.read_text())
    intent, _token, _known = _preparation_receipts(world, document)
    readmission._known_path(intent).unlink()
    with pytest.raises(
        readmission.ReadmissionHeld, match="preparation known receipt is unavailable"
    ):
        readmission.require_preparation_known(world.state, document, initial=True)
    _preparation_receipts(world, document)
    readmission.require_preparation_known(world.state, document, initial=True)
    document["preparation_origin"]["sha256"] = "0" * 64
    with pytest.raises(
        readmission.ReadmissionHeld, match="preparation intent differs from campaign origin"
    ):
        readmission.require_preparation_known(world.state, document, initial=True)


def test_preparation_known_accepts_flat_historical_plan_binding(monkeypatch, tmp_path):
    world = _initialized_world(monkeypatch, tmp_path)
    document = json.loads(world.state.read_text())
    _preparation_receipts(world, document, flat_plan=True)
    readmission.require_preparation_known(world.state, document, initial=True)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("intent_producer", "preparation intent producer differs from current runtime"),
        ("plan_producer", "preparation intent producer differs from current runtime"),
        ("plan_manifest_hash", "preparation intent producer differs from current runtime"),
        ("known_producer", "preparation known receipt producer differs from current runtime"),
        ("known_manifest", "preparation known receipt producer differs from current runtime"),
        ("known_manifest_hash", "preparation known receipt producer differs from current runtime"),
        ("manifest_module", "prepared manifest does not pin the executing readmission boundary"),
        ("manifest_bytes", "preparation intent producer differs from current runtime"),
    ],
)
def test_preparation_known_rejects_tampered_producer_binding(
    monkeypatch, tmp_path, mutation, message
):
    world = _initialized_world(monkeypatch, tmp_path)
    document = json.loads(world.state.read_text())
    if mutation == "intent_producer":
        _preparation_receipts(world, document, token_overrides={"producer_sha256": "0" * 64})
    elif mutation == "plan_producer":
        _preparation_receipts(world, document, plan_overrides={"producer_sha256": "0" * 64})
    elif mutation == "plan_manifest_hash":
        _preparation_receipts(
            world,
            document,
            plan_overrides={
                "new": {"manifest": str(world.manifest_path), "manifest_sha256": "0" * 64}
            },
        )
    elif mutation == "known_producer":
        _preparation_receipts(world, document, known_overrides={"producer_sha256": "0" * 64})
    elif mutation == "known_manifest":
        _preparation_receipts(
            world, document, known_overrides={"manifest": str(tmp_path / "other.json")}
        )
    elif mutation == "known_manifest_hash":
        _preparation_receipts(world, document, known_overrides={"manifest_sha256": "0" * 64})
    elif mutation == "manifest_module":
        _preparation_receipts(world, document)
        world.manifest["modules"]["closer_native_readmission.py"] = "0" * 64
        world.manifest_path.write_text(json.dumps(world.manifest, sort_keys=True))
    else:
        _preparation_receipts(world, document)
        world.manifest["policy"]["same_module_pin_changed_bytes"] = True
        world.manifest_path.write_text(json.dumps(world.manifest, sort_keys=True))
    with pytest.raises(readmission.ReadmissionHeld, match=message):
        readmission.require_preparation_known(world.state, document, initial=True)


def test_admit_initialized_blocks_tampered_preparation_producer(monkeypatch, tmp_path):
    world = _initialized_world(monkeypatch, tmp_path)
    document = json.loads(world.state.read_text())
    _preparation_receipts(world, document, known_overrides={"producer_sha256": "0" * 64})
    before = world.state.read_bytes()
    with pytest.raises(
        readmission.ReadmissionHeld,
        match="preparation known receipt producer differs from current runtime",
    ):
        readmission.admit_initialized(
            world.manifest_path, world.state, world.assignment, world.request
        )
    assert world.state.read_bytes() == before


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
        sys.modules,
        "closer_retained_reconciliation",
        SimpleNamespace(validate=lambda *_, **__: valid),
    )
    assert readmission.historical_reference("ref", "manifest", "state", "worker") == valid
    valid["decision"]["max_fresh_generations"] = 2
    with pytest.raises(readmission.ReadmissionHeld, match="does not authorize"):
        readmission.historical_reference("ref", "manifest", "state", "worker")
