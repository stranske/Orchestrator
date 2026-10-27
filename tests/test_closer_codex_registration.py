"""Pure and real-queue controls; all native metadata exchanges are mocked DATA."""

import ast
import copy
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import closer_codex_metadata as metadata
import closer_codex_registration as registration
import closer_coding_session_v2 as sessions
import closer_correction_controller as ctl
import closer_frontier_policy as finite
import closer_queue as queue
import execution_profiles


def manifest():
    profile_id = sessions.PROFILE
    profile_sha = ctl.source_digest(execution_profiles.get_profile(profile_id))
    budget = {
        "schema": sessions.BUDGET_SCHEMA,
        **{key: 100 for key in sessions.LIMIT_FIELDS},
        **{key: "UNKNOWN" for key in sessions.UNKNOWN_FIELDS},
    }
    budget["max_outer_submissions"] = 1
    template = {
        "agent": "codex",
        "runner": "existing-local-controller",
        "profile_id": profile_id,
        "profile_sha256": profile_sha,
        "reader_entrypoint": "closer_correction_controller.NativeConsumerReader",
        "reader_source_sha256": ctl._LOADED_SOURCE_SHA256,
        "native_bootstrap": {"schema": "synthetic-native-selection"},
        "transport_binding": "codex-source-owned-appserver-stdio/v1",
        sessions.POLICY_FIELD: {
            "schema": sessions.POLICY_SCHEMA,
            "consumer_id": "test",
            "agent": "codex",
            "profile_id": profile_id,
            "profile_sha256": profile_sha,
            "budget": budget,
        },
    }
    return {
        "policy": {
            registration.POLICY: {
                "schema": registration.SCHEMA,
                "consumer_id": "test",
                "target": "acme/repo#9",
                "registration": template,
            },
            metadata.POLICY: {"schema": metadata.PURPOSE, "profile_id": profile_id},
        }
    }


def test_plan_has_no_initial_account_or_authority():
    m = manifest()
    plan = registration.selected_plan(m)
    assert "account_sha256" not in plan["registration"]
    plan["registration"]["agent"] = "changed"
    assert m["policy"][registration.POLICY]["registration"]["agent"] == "codex"
    assert registration.selected_plan({}) is None


@pytest.mark.parametrize(
    "damage", ["account", "agent", "runner", "profile", "reader", "extra", "budget"]
)
def test_bad_initial_template_is_rejected_before_any_observation(damage):
    m = manifest()
    plan = m["policy"][registration.POLICY]
    value = plan["registration"]
    if damage == "account":
        value["account_sha256"] = "a" * 64
    elif damage == "agent":
        value["agent"] = "other"
    elif damage == "runner":
        value["runner"] = "new-runner"
    elif damage == "profile":
        value["profile_sha256"] = "a" * 64
    elif damage == "reader":
        value["reader_source_sha256"] = "a" * 64
    elif damage == "extra":
        plan["permission"] = True
    else:
        value[sessions.POLICY_FIELD]["budget"]["max_outer_submissions"] = 0
    with pytest.raises((ValueError, TypeError)):
        registration.selected_plan(m)


def test_prospective_observation_binds_exact_policy_and_original_manifest():
    m = manifest()
    target = "acme/repo#9"
    digest = "f" * 64
    value = registration.operation_observation(m, target, digest)
    assert value["action"] == "disposition" and value["eligible"] is True
    assert value["operation_binding"] == {
        "runtime_manifest_sha256": digest,
        metadata.POLICY + "_sha256": ctl.digest(m["policy"][metadata.POLICY]),
        registration.POLICY + "_sha256": ctl.digest(m["policy"][registration.POLICY]),
    }
    assert "correction_controller_sha256" not in value["operation_binding"]
    with pytest.raises(ValueError):
        registration.operation_observation(m, target, "not-a-digest")


def test_copied_operation_outside_real_callback_never_enters_metadata(monkeypatch):
    def forbidden(*args):
        pytest.fail("native observer must not be called")

    monkeypatch.setattr(metadata, "metadata_scope", forbidden, raising=False)
    with pytest.raises(ValueError, match="original_metadata_callback_required"):
        registration.execute_original(
            object(),
            manifest(),
            "f" * 64,
            "acme/repo#9",
            "disposition",
            {},
            {"id": "copied"},
        )


def test_real_queue_retains_proposal_but_installs_no_consumer(tmp_path, monkeypatch):
    m = manifest()
    target = "acme/repo#9"
    binding = object()
    observed = registration.operation_observation(m, target, "f" * 64)
    frontier = finite.make_frontier_from_observations(
        {target: observed}, owner="test-owner", frontier_id="metadata-only-test"
    )
    calls = []
    guard = object()
    handle = object()
    reference = {
        "name": "result.json",
        "sha256": "a" * 64,
        "execution_authorized": False,
    }

    @contextmanager
    def scope(actual, operation):
        assert actual is binding and queue._currently_invoking(target, operation)
        calls.append("real-callback")
        yield guard

    def qualify(actual):
        assert actual is guard
        calls.append("mocked-metadata")
        return reference

    def read(actual, ref):
        assert actual is guard and ref is reference
        return handle

    def retain(actual, selected):
        assert actual is guard and selected is handle
        return {
            "name": "proposal.json",
            "sha256": "b" * 64,
            "execution_authorized": False,
            "registration_installed": False,
        }

    monkeypatch.setitem(sys.modules, "closer_codex_metadata_admission", metadata)
    monkeypatch.setattr(metadata, "metadata_scope", scope, raising=False)
    monkeypatch.setattr(metadata, "qualify_admitted_callback", qualify, raising=False)
    monkeypatch.setattr(metadata, "read_admitted_account", read, raising=False)
    monkeypatch.setattr(metadata, "retain_admitted_initial_registration", retain, raising=False)

    def execute(name, action, snapshot, operation):
        return registration.execute_original(
            binding, m, "f" * 64, name, action, snapshot, operation
        )

    queue.run_round(
        tmp_path / "queue.json",
        [target],
        lambda _: copy.deepcopy(observed),
        execute,
        owner="test-owner",
        deadline=None,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
        finite_frontier=frontier,
        request_timeout_seconds=10,
    )
    assert calls == ["real-callback", "mocked-metadata"]
    saved = json.loads((tmp_path / "queue.json").read_text())
    actual = next(iter(saved["frontiers"].values()))["items"][target]["operation"]
    assert actual["status"] == "completed"
    assert actual["result"]["registration_proposal"]["name"] == "proposal.json"
    assert actual["result"]["registration_installed"] is False
    assert m["policy"][registration.POLICY]["registration"].get("account_sha256") is None


def test_wrong_original_binding_stops_before_metadata(tmp_path, monkeypatch):
    m = manifest()
    target = "acme/repo#9"
    observed = registration.operation_observation(m, target, "f" * 64)
    frontier = finite.make_frontier_from_observations(
        {target: observed}, owner="test-owner", frontier_id="wrong-metadata-test"
    )
    calls = []

    def execute(name, action, snapshot, operation):
        changed = copy.deepcopy(operation)
        changed["binding"][metadata.POLICY + "_sha256"] = "e" * 64
        with pytest.raises(ValueError, match="original_metadata_callback_required"):
            registration.execute_original(object(), m, "f" * 64, name, action, snapshot, changed)
        calls.append("refused")
        return {"status": "completed"}

    queue.run_round(
        tmp_path / "queue.json",
        [target],
        lambda _: copy.deepcopy(observed),
        execute,
        owner="test-owner",
        deadline=None,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
        finite_frontier=frontier,
        request_timeout_seconds=10,
    )
    assert calls == ["refused"]


def test_detached_tick_caller_cannot_select_native_entry():
    with pytest.raises(ValueError, match="original_native_tick_required"):
        registration.run_from_native_admission(object(), {}, manifest(), "/tmp/no-manifest", {})


def test_tick_hook_precedes_route_and_cannot_fall_through_to_worker():
    import closer_campaign

    tree = ast.parse(Path(closer_campaign.__file__).read_text())
    tick = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tick")
    hooks = [
        n
        for n in ast.walk(tick)
        if isinstance(n, ast.If) and "codex_initial_consumer" in ast.unparse(n.test)
    ]
    assert len(hooks) == 1 and isinstance(hooks[0].body[-1], ast.Continue)
    assert any(
        isinstance(n, ast.Call)
        and ast.unparse(n.func) == "closer_codex_registration.run_from_native_admission"
        for n in ast.walk(hooks[0])
    )
    route = min(
        n.lineno
        for n in ast.walk(tick)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "route_fn"
    )
    assert hooks[0].lineno < route


def test_actual_tick_calls_metadata_queue_and_never_selects_model_route(tmp_path, monkeypatch):
    """Real tick/caller/queue; native qualification and metadata are explicit DATA seams."""
    import hashlib
    from types import SimpleNamespace

    import closer_campaign as campaign
    import closer_mechanisms as mechanisms
    import closer_native_observer as native
    import closer_runtime_binding as runtime

    m = manifest()
    m.update(
        candidate_sha="a" * 40,
        mechanism_contract={"schema": "closer-mechanism/v1", "mechanism": "native-handoff/v1"},
        package_digest="c" * 64,
        module_dir=str(Path(registration.__file__).parent),
        runtime={"python": sys.executable},
        module_path="unused-in-this-private-purpose",
        modules={Path(registration.__file__).name: registration._SOURCE_SHA256},
    )
    root = tmp_path / "campaigns"
    root.mkdir()
    state = root / "private.campaign.json"
    state.write_text("{}")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(m))
    manifest_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    target = "acme/repo#9"
    observed = registration.operation_observation(m, target, manifest_sha)
    frontier = finite.make_frontier_from_observations(
        {target: observed}, owner="test-owner", frontier_id="actual-tick-metadata"
    )
    document = {
        "phase": "work",
        "release_pin": m["package_digest"],
        "owner": "test-owner",
        "workers": [],
        "worker_config": {"mechanism": mechanisms.NATIVE, "policy": m["policy"]},
    }
    obj = SimpleNamespace(path=state, read=lambda: document)
    admission = {
        "campaign": {"state": str(state)},
        "runtime_manifest": str(path),
        "report": {"path": str(tmp_path / "synthetic-capacity.json")},
        "finite_frontier": frontier,
        "request_timeout_seconds": 35,
    }
    binding = object()
    origin = SimpleNamespace(path=str(path), sha256=manifest_sha)
    monkeypatch.setattr(campaign, "Campaign", lambda _: obj)
    monkeypatch.setattr(campaign, "load_manifest", lambda _: m)
    monkeypatch.setattr(campaign, "reconcile", lambda *a, **k: document)
    monkeypatch.setattr(campaign, "next_action", lambda *a, **k: {"kind": "launch"})
    monkeypatch.setattr(mechanisms, "mechanism", lambda *a, **k: mechanisms.NATIVE)
    monkeypatch.setattr(runtime, "policy_for_mechanism", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "binding_from_manifest", lambda _: binding)
    monkeypatch.setattr(runtime, "_factory_manifest_origin", lambda _: origin)
    monkeypatch.setattr(native, "entry_active", lambda _: True)
    monkeypatch.setattr(native, "contract", lambda _: (None, None, Path(sys.executable), tmp_path))
    monkeypatch.setattr(
        native, "capacity_contract", lambda *a, **k: {"schema": "native-scoped-capacity/v2"}
    )
    monkeypatch.setattr(native, "publish_admission", lambda *a: admission)
    monkeypatch.setattr(native, "validate_admission", lambda *a, **k: None)
    monkeypatch.setattr(native, "rearm_on_hold_followup", lambda *a: document)
    monkeypatch.setattr(native, "rearm_on_assignment_change", lambda *a: document)
    import closer_hold_followup

    monkeypatch.setattr(closer_hold_followup, "validated_receipts", lambda *a: [])
    monkeypatch.setattr(closer_hold_followup, "consume", lambda *a: (document, []))
    guard = object()
    handle = object()
    calls = []

    @contextmanager
    def scope(actual, operation):
        assert actual is binding and queue._currently_invoking(target, operation)
        calls.append("original-callback")
        yield guard

    monkeypatch.setitem(sys.modules, "closer_codex_metadata_admission", metadata)
    monkeypatch.setattr(metadata, "metadata_scope", scope, raising=False)
    monkeypatch.setattr(
        metadata,
        "qualify_admitted_callback",
        lambda g: {
            "name": "synthetic-result.json",
            "sha256": "f" * 64,
            "execution_authorized": False,
        },
        raising=False,
    )
    monkeypatch.setattr(metadata, "read_admitted_account", lambda g, r: handle, raising=False)
    monkeypatch.setattr(
        metadata,
        "retain_admitted_initial_registration",
        lambda g, h: {
            "name": "synthetic-proposal.json",
            "sha256": "e" * 64,
            "execution_authorized": False,
            "registration_installed": False,
        },
        raising=False,
    )
    monkeypatch.setenv("CLOSER_CAPACITY_REPORT", "restore-this-original-selection")

    def forbidden(*a, **k):
        pytest.fail("metadata-only tick must not select route or launch worker")

    results = campaign.tick(
        root, path, qualifier=lambda *a: [], route_fn=forbidden, launcher=forbidden
    )
    assert calls == ["original-callback"]
    assert results[0]["kind"] == "codex_initial_account_qualification"
    assert results[0]["metadata_result"]["registration_installed"] is False
    assert results[0]["queue"]["valid"] is True
    import os

    assert os.environ["CLOSER_CAPACITY_REPORT"] == "restore-this-original-selection"
    assert document["workers"] == [] and document["phase"] == "work"
