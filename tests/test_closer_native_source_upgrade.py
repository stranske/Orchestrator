"""Regression tests for the completed=False, no-native-ACT upgrade boundary."""

import base64
import copy
import hashlib
import inspect
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_correction_controller as controller
import closer_native_source_upgrade as upgrade
import closer_release


def no_act():
    return {
        "correction": {
            "status": "owned_unknown",
            "reason": "owner source criterion differs from seed",
            "remote_authenticated": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        },
        "queue": {"events": [], "accounting": [{"operation_id": None, "operation_status": None}]},
    }


def test_completed_false_no_act_fixture_is_the_only_accepted_disposition():
    fixture = no_act()
    upgrade._no_act(fixture["correction"], fixture["queue"])
    for path, value in (
        (("correction", "act_authorized"), True),
        (("correction", "reason"), "different scope"),
        (("queue", "events"), [{"operation_id": "native"}]),
        (("queue", "accounting"), ["not-an-accounting-record"]),
    ):
        changed = json.loads(json.dumps(fixture))
        changed[path[0]][path[1]] = value
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._no_act(changed["correction"], changed["queue"])
    upgrade._incomplete_cohort({"completed": False}, {"accepted": False})
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._incomplete_cohort({"completed": True}, {"accepted": False})


def test_failed_before_pipeline_reference_is_literal_and_never_uses_completed_route(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "pin.manifest.json"
    state = tmp_path / "campaign.json"
    manifest.write_text(json.dumps({"candidate_sha": "dd8d71119b92e15ff85836cc44ad40e20207ac6f"}))
    state.write_text("{}")
    manifest_value = json.loads(manifest.read_text())
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    expected = {
        "authority": False,
        "accepted": False,
        "retry_authority": False,
        "outcome": "unknown",
        "manifest": manifest_value,
        "manifest_sha256": manifest_sha256,
        "worker": {"worker_id": "fa4c00f5e8c6"},
    }
    called = {}

    def read(actual_manifest, actual_state, worker):
        called.update(manifest=actual_manifest, state=actual_state, worker=worker)
        return expected

    monkeypatch.setitem(
        sys.modules,
        "closer_failed_predecessor_readback",
        SimpleNamespace(read_dd8_fa4_failed_before_pipeline=read),
    )
    monkeypatch.setattr(upgrade, "_completed_reference", lambda *_args: pytest.fail("wrong route"))
    actual, failed = upgrade._old_reference(manifest, state, "fa4c00f5e8c6")
    assert failed is True and actual is expected
    assert called == {"manifest": manifest, "state": state, "worker": "fa4c00f5e8c6"}


def test_failed_before_pipeline_reference_refuses_authority_or_wrong_worker(tmp_path, monkeypatch):
    manifest = tmp_path / "pin.manifest.json"
    state = tmp_path / "campaign.json"
    manifest.write_text(json.dumps({"candidate_sha": "dd8d71119b92e15ff85836cc44ad40e20207ac6f"}))
    state.write_text("{}")
    manifest_value = json.loads(manifest.read_text())
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    for changed in (
        {"authority": True},
        {"worker": {"worker_id": "different-worker"}},
    ):
        reference = {
            "authority": False,
            "accepted": False,
            "retry_authority": False,
            "outcome": "unknown",
            "manifest": manifest_value,
            "manifest_sha256": manifest_sha256,
            "worker": {"worker_id": "fa4c00f5e8c6"},
        }
        reference.update(changed)
        monkeypatch.setitem(
            sys.modules,
            "closer_failed_predecessor_readback",
            SimpleNamespace(read_dd8_fa4_failed_before_pipeline=lambda *_args: reference),
        )
        with pytest.raises(upgrade.SourceUpgradeHeld, match="failed-before-pipeline"):
            upgrade._old_reference(manifest, state, "fa4c00f5e8c6")


def test_clock_no_act_is_literal_and_refuses_effect_or_wrong_stop():
    fixture = {
        "correction": {
            "status": "owned_unknown",
            "reason": "native observation clock is not current",
            "remote_authenticated": False,
            "retry_safe": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        },
        "queue": {
            "events": [],
            "progress": [],
            "accounting": [
                {
                    "reason": "observation_failed",
                    "next_trigger": "retry_observation_next_round",
                    "operation_id": None,
                    "operation_status": None,
                }
            ],
        },
    }
    disposition = {
        "workers": 1,
        "coding_continuations": None,
        "result_stages": ["act", "exception", "interpret", "prepare"],
        "native_effect_contract": None,
    }
    upgrade._clock_no_act(
        fixture["correction"], fixture["queue"], strong_as_of=True, disposition=disposition
    )
    for path, value in (
        (("correction", "reason"), "different stop"),
        (("correction", "act_authorized"), True),
        (("queue", "events"), [{"operation_id": "effect"}]),
        (("queue", "accounting"), [{"operation_id": "effect"}]),
    ):
        changed = json.loads(json.dumps(fixture))
        changed[path[0]][path[1]] = value
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._clock_no_act(
                changed["correction"],
                changed["queue"],
                strong_as_of=True,
                disposition=disposition,
            )
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._clock_no_act(
            fixture["correction"], fixture["queue"], strong_as_of=False, disposition=disposition
        )
    for key, value in (
        ("workers", 2),
        ("coding_continuations", []),
        ("result_stages", ["exception", "interpret", "prepare"]),
        ("native_effect_contract", {"effect": "native"}),
    ):
        changed_disposition = dict(disposition)
        changed_disposition[key] = value
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._clock_no_act(
                fixture["correction"],
                fixture["queue"],
                strong_as_of=True,
                disposition=changed_disposition,
            )
    progressed = json.loads(json.dumps(fixture))
    progressed["queue"]["progress"] = [{"operation_id": "effect"}]
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._clock_no_act(
            progressed["correction"],
            progressed["queue"],
            strong_as_of=True,
            disposition=disposition,
        )
    for mutate in (
        lambda value: value.update(strong_as_of=1),
        lambda value: value["queue"]["accounting"].append(dict(value["queue"]["accounting"][0])),
        lambda value: value["queue"]["accounting"][0].update(reason="wrong"),
        lambda value: value["queue"]["accounting"][0].update(next_trigger="wrong"),
    ):
        changed = {"queue": json.loads(json.dumps(fixture["queue"])), "strong_as_of": True}
        mutate(changed)
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._clock_no_act(
                fixture["correction"],
                changed["queue"],
                strong_as_of=changed["strong_as_of"],
                disposition=disposition,
            )


def ownership_budget_no_act():
    correction = {
        "status": "owned_unknown",
        "reason": "native ownership response budget exhausted",
        "remote_authenticated": False,
        "retry_safe": False,
        "act_authorized": False,
        "source_closure_authorized": False,
    }
    accounting = {
        "action": "wait",
        "eligible": True,
        "next_trigger": "retry_observation_next_round",
        "observation_freshness": "fresh",
        "operation_id": None,
        "operation_status": None,
        "reason": "observation_failed",
        "requires_coordinator": False,
        "revision": None,
        "status": "deferred",
        "target": "stranske/Orchestrator#984",
    }
    queue = {
        "events": [],
        "progress": [],
        "integration_required": [],
        "reasons": [],
        "warnings": [],
        "valid": True,
        "stop_valid": True,
        "continuation_required": True,
        "accounting": [accounting],
        "deferred": [dict(accounting)],
    }
    disposition = {
        "workers": 1,
        "coding_continuations": None,
        "result_stages": ["act", "exception", "interpret", "prepare"],
        "native_effect_contract": None,
    }
    return correction, queue, disposition


def test_ownership_budget_no_act_is_literal_and_refuses_effect_or_wrong_stop():
    correction, queue, disposition = ownership_budget_no_act()
    upgrade._ownership_budget_no_act(correction, queue, strong_as_of=True, disposition=disposition)
    for mutate in (
        lambda value: value[0].update(act_authorized=True),
        lambda value: value[0].update(reason="different stop"),
        lambda value: value[1].update(events=[{"operation_id": "effect"}]),
        lambda value: value[1]["accounting"][0].update(operation_id="effect"),
        lambda value: value[1].update(deferred=[]),
        lambda value: value[2].update(coding_continuations=[]),
    ):
        changed = [
            json.loads(json.dumps(correction)),
            json.loads(json.dumps(queue)),
            json.loads(json.dumps(disposition)),
        ]
        mutate(changed)
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._ownership_budget_no_act(
                changed[0], changed[1], strong_as_of=True, disposition=changed[2]
            )
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._ownership_budget_no_act(
            correction, queue, strong_as_of=False, disposition=disposition
        )


def test_ownership_budget_dispatch_is_exact_candidate_only():
    correction, queue, disposition = ownership_budget_no_act()
    fixture = {
        "correction": correction,
        "queue_receipt": queue,
        "strong_as_of": True,
        "ownership_budget_disposition": disposition,
    }
    upgrade._diagnostic_no_act(upgrade.REVIEWED_OWNERSHIP_BUDGET_CANDIDATE, fixture)
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._diagnostic_no_act(upgrade.REVIEWED_CLOCK_CANDIDATE, fixture)
    changed = dict(fixture)
    changed["ownership_budget_disposition"] = None
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._diagnostic_no_act(upgrade.REVIEWED_OWNERSHIP_BUDGET_CANDIDATE, changed)


def seed_assessment_no_act():
    correction, queue, disposition = ownership_budget_no_act()
    correction["reason"] = "seed_assessment_identity_invalid"
    return correction, queue, disposition


def test_seed_assessment_no_act_is_literal_and_refuses_effect_or_other_identity():
    correction, queue, disposition = seed_assessment_no_act()
    upgrade._seed_assessment_no_act(correction, queue, strong_as_of=True, disposition=disposition)
    for mutate in (
        lambda value: value[0].update(reason="different"),
        lambda value: value[0].update(act_authorized=True),
        lambda value: value[1].update(events=[{"operation_id": "effect"}]),
        lambda value: value[1]["accounting"][0].update(operation_status="started"),
        lambda value: value[2].update(native_effect_contract={"effect": "native"}),
    ):
        changed = [
            json.loads(json.dumps(correction)),
            json.loads(json.dumps(queue)),
            json.loads(json.dumps(disposition)),
        ]
        mutate(changed)
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._seed_assessment_no_act(
                changed[0], changed[1], strong_as_of=True, disposition=changed[2]
            )


def test_seed_assessment_dispatch_requires_its_literal_candidate():
    correction, queue, disposition = seed_assessment_no_act()
    fixture = {
        "correction": correction,
        "queue_receipt": queue,
        "strong_as_of": True,
        "seed_assessment_disposition": disposition,
    }
    upgrade._diagnostic_no_act(upgrade.REVIEWED_SEED_ASSESSMENT_CANDIDATE, fixture)
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._diagnostic_no_act(upgrade.REVIEWED_OWNERSHIP_BUDGET_CANDIDATE, fixture)


def test_seed_assessment_reference_pins_worker_and_result(monkeypatch):
    good = {
        "manifest": {"candidate_sha": upgrade.REVIEWED_SEED_ASSESSMENT_CANDIDATE},
        "result_sha256": upgrade.REVIEWED_SEED_ASSESSMENT_RESULT_SHA256,
        "result": {
            "detail": json.dumps(
                {
                    "completed_round": {
                        "round_id": upgrade.REVIEWED_SEED_ASSESSMENT_ROUND_ID,
                        "receipt_sha256": upgrade.REVIEWED_SEED_ASSESSMENT_ROUND_SHA256,
                    }
                }
            )
        },
    }
    monkeypatch.setattr(upgrade, "_completed_reference", lambda *_args: good)
    assert (
        upgrade._seed_assessment_reference(
            Path("/private/old-manifest.json"),
            "/private/old-state.json",
            upgrade.REVIEWED_SEED_ASSESSMENT_WORKER_ID,
        )
        is good
    )
    for worker, result_sha256 in (
        ("0" * 12, upgrade.REVIEWED_SEED_ASSESSMENT_RESULT_SHA256),
        (upgrade.REVIEWED_SEED_ASSESSMENT_WORKER_ID, "0" * 64),
    ):
        changed = {**good, "result_sha256": result_sha256}
        monkeypatch.setattr(upgrade, "_completed_reference", lambda *_args: changed)
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._seed_assessment_reference(
                Path("/private/old-manifest.json"), "/private/old-state.json", worker
            )
    changed = json.loads(json.dumps(good))
    changed["result"]["detail"] = json.dumps(
        {"completed_round": {"round_id": "0" * 32, "receipt_sha256": "0" * 64}}
    )
    monkeypatch.setattr(upgrade, "_completed_reference", lambda *_args: changed)
    with pytest.raises(upgrade.SourceUpgradeHeld, match="completed-round"):
        upgrade._seed_assessment_reference(
            Path("/private/old-manifest.json"),
            "/private/old-state.json",
            upgrade.REVIEWED_SEED_ASSESSMENT_WORKER_ID,
        )


def test_clock_dispatch_requires_complete_clock_evidence_and_is_candidate_scoped():
    fixture = {
        "correction": {
            "status": "owned_unknown",
            "reason": "native observation clock is not current",
            "remote_authenticated": False,
            "retry_safe": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        },
        "queue_receipt": {
            "events": [],
            "progress": [],
            "accounting": [
                {
                    "reason": "observation_failed",
                    "next_trigger": "retry_observation_next_round",
                    "operation_id": None,
                    "operation_status": None,
                }
            ],
        },
        "clock_disposition": {
            "workers": 1,
            "coding_continuations": None,
            "result_stages": ["act", "exception", "interpret", "prepare"],
            "native_effect_contract": None,
        },
        "strong_as_of": True,
    }
    upgrade._diagnostic_no_act(upgrade.REVIEWED_CLOCK_CANDIDATE, fixture)
    for key in ("strong_as_of", "clock_disposition"):
        changed = dict(fixture)
        changed[key] = None
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._diagnostic_no_act(upgrade.REVIEWED_CLOCK_CANDIDATE, changed)
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._diagnostic_no_act(upgrade.REVIEWED_OLD_CANDIDATE, fixture)


def test_clock_predecessor_uses_only_its_own_module_and_descriptor_hashes(monkeypatch):
    modules = {"closer_campaign.py": "a" * 64}
    descriptor = {
        "candidate_sha": upgrade.REVIEWED_CLOCK_CANDIDATE,
        "modules": modules,
        "package_digest": "b" * 64,
        "state_versions": {"campaign": 1},
    }
    raw = json.dumps(descriptor, sort_keys=True).encode()
    manifest = {**descriptor, "public_runtime_descriptor": "closer-runtime-package.json"}
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_CLOCK_MODULES_SHA256",
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(
        upgrade, "REVIEWED_CLOCK_DESCRIPTOR_SHA256", hashlib.sha256(raw).hexdigest()
    )
    assert upgrade._reviewed_old_metadata(manifest, raw) == modules
    for candidate in (upgrade.REVIEWED_OLD_CANDIDATE, upgrade.REVIEWED_CAPACITY_CANDIDATE):
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._reviewed_old_metadata({**manifest, "candidate_sha": candidate}, raw)


def test_ownership_budget_predecessor_uses_only_its_own_module_and_descriptor_hashes(
    monkeypatch,
):
    modules = {"closer_campaign.py": "c" * 64}
    descriptor = {
        "candidate_sha": upgrade.REVIEWED_OWNERSHIP_BUDGET_CANDIDATE,
        "modules": modules,
        "package_digest": "d" * 64,
        "state_versions": {"campaign": 1},
    }
    raw = json.dumps(descriptor, sort_keys=True).encode()
    manifest = {**descriptor, "public_runtime_descriptor": "closer-runtime-package.json"}
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_OWNERSHIP_BUDGET_MODULES_SHA256",
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_OWNERSHIP_BUDGET_DESCRIPTOR_SHA256",
        hashlib.sha256(raw).hexdigest(),
    )
    assert upgrade._reviewed_old_metadata(manifest, raw) == modules
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._reviewed_old_metadata(
            {**manifest, "candidate_sha": upgrade.REVIEWED_CLOCK_CANDIDATE}, raw
        )


def test_seed_assessment_predecessor_uses_only_its_own_module_and_descriptor_hashes(
    monkeypatch,
):
    modules = {"closer_campaign.py": "e" * 64}
    descriptor = {
        "candidate_sha": upgrade.REVIEWED_SEED_ASSESSMENT_CANDIDATE,
        "modules": modules,
        "package_digest": "f" * 64,
        "state_versions": {"campaign": 1},
    }
    raw = json.dumps(descriptor, sort_keys=True).encode()
    manifest = {**descriptor, "public_runtime_descriptor": "closer-runtime-package.json"}
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_SEED_ASSESSMENT_MODULES_SHA256",
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_SEED_ASSESSMENT_DESCRIPTOR_SHA256",
        hashlib.sha256(raw).hexdigest(),
    )
    assert upgrade._reviewed_old_metadata(manifest, raw) == modules
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._reviewed_old_metadata(
            {**manifest, "candidate_sha": upgrade.REVIEWED_OWNERSHIP_BUDGET_CANDIDATE}, raw
        )


def test_pagination_predecessor_uses_only_its_own_module_and_descriptor_hashes(
    tmp_path, monkeypatch
):
    modules = {"closer_campaign.py": "1" * 64}
    descriptor = {
        "candidate_sha": upgrade.REVIEWED_PAGINATION_CANDIDATE,
        "modules": modules,
        "package_digest": "2" * 64,
        "state_versions": {"campaign": 1},
    }
    raw = json.dumps(descriptor, sort_keys=True).encode()
    manifest = {**descriptor, "public_runtime_descriptor": "closer-runtime-package.json"}
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_PAGINATION_MODULES_SHA256",
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(
        upgrade, "REVIEWED_PAGINATION_DESCRIPTOR_SHA256", hashlib.sha256(raw).hexdigest()
    )
    assert upgrade._reviewed_old_metadata(manifest, raw) == modules
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._reviewed_old_metadata(
            {**manifest, "candidate_sha": upgrade.REVIEWED_SEED_ASSESSMENT_CANDIDATE}, raw
        )
    import closer_role_qualification as roles
    import execution_profiles

    original_id = (
        "sonnet-current-role-qualification:"
        "5ba94b55db1e4dcfbac97e378e5eaa3031141f45dd1f6522d9e4fbbd27854ec9"
    )
    original = roles.ROLE_QUALIFICATIONS[original_id]
    profile = execution_profiles.get_profile(original["profile_identity"]["profile_id"])
    historical_now = original["qualified_at"] + 1
    # The newly reviewed 3684 row is future at D4's signed observation time,
    # so the global live registry correctly refuses historical selection.
    assert (
        roles.qualified_record(profile, roles.STRONG_CRITERION_ADJUDICATION, now=historical_now)
        is None
    )
    assert (
        roles.qualified_record(
            profile,
            roles.STRONG_CRITERION_ADJUDICATION,
            now=historical_now,
            records={original_id: copy.deepcopy(original)},
        )
        == original
    )
    assert "records=historical_records" in inspect.getsource(
        upgrade._pagination_preinstruction_diagnostic
    )
    old_path = tmp_path / "pagination-old-manifest.json"
    old_path.write_bytes(raw)
    sealed = {"state_sha256": "a" * 64, "result_sha256": "b" * 64}
    calls = []
    monkeypatch.setattr(
        upgrade,
        "_sealed_pagination_reference",
        lambda *_: (calls.append("sealed") or sealed),
    )
    monkeypatch.setattr(
        upgrade,
        "_pagination_reference",
        lambda *_: pytest.fail("D4 must not enter current-admission cohort reader"),
    )
    monkeypatch.setattr(
        roles,
        "qualified_record",
        lambda *_a, **_kw: pytest.fail("D4 historical read must not consult current role"),
    )
    assert upgrade._old_reference(old_path, tmp_path / "unused-state.json", "unused-worker") == (
        sealed,
        False,
    )
    assert calls == ["sealed"]
    _assert_pagination_prepare_held_rereads(tmp_path, monkeypatch)


def _assert_pagination_prepare_held_rereads(tmp_path, monkeypatch):
    """Exercise both public preparation rereads, stopping before creation."""
    import closer_campaign as campaign
    import closer_frontier_policy as policy
    import closer_native_observer as native
    import closer_native_readmission as readmission

    def document(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return path

    frontier = {"targets": ["stranske/Orchestrator#984"]}
    cfg = {"finite_frontier": frontier}
    old_manifest = {
        "candidate_sha": upgrade.REVIEWED_PAGINATION_CANDIDATE,
        "policy": {"correction_controller_installation": {"config": {}}},
    }
    old_path = document("old-manifest.json", old_manifest)
    old_state = document("old-state.json", {"worker_config": cfg})
    old_result = document("old-result.json", {"outcome": "scoped_continuation"})
    new_path = document("new-manifest.json", {"candidate_sha": "new"})
    raw = new_path.read_bytes()
    manifest = json.loads(raw)
    wrapper = document("wrapper", {})
    destination = tmp_path / "fresh-campaign.json"
    seal = tmp_path / "seal.json"
    known = document("known.json", {})
    digest = "a" * 64

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def canonical_sha(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    reference = {
        "worker": {"state": "terminal", "outcome": "scoped_continuation"},
        "result_sha256": sha(old_result),
        "state_sha256": sha(old_state),
    }
    record = {
        "schema": upgrade.SCHEMA,
        "stage": "sealed_pre_cas",
        "plan_sha256": digest,
        "new": {
            "manifest": str(new_path),
            "manifest_sha256": sha(new_path),
            "state": str(destination),
            "custody": {},
        },
        "old": {
            "manifest": str(old_path),
            "manifest_sha256": sha(old_path),
            "state": str(old_state),
            "state_sha256": sha(old_state),
            "result": str(old_result),
            "result_sha256": sha(old_result),
            "worker_id": upgrade.REVIEWED_PAGINATION_WORKER_ID,
            "failed_before_pipeline": False,
            "native_index": {},
            "diagnostic": {},
        },
        "new_release": {"path": "release.json"},
        "wrapper": {"path": str(wrapper), "after_sha256": sha(wrapper)},
        "held_recipe": {
            "worker_config": cfg,
            "frontier": frontier,
            "worker_config_sha256": canonical_sha(cfg),
            "frontier_sha256": canonical_sha(frontier),
        },
        "locks": [],
    }
    seal.write_text(json.dumps({"record": record}))
    monkeypatch.setattr(upgrade, "_manifest", lambda _: (new_path, raw, manifest))
    monkeypatch.setattr(
        upgrade,
        "_read_signed_upgrade",
        lambda path, *_a, **kw: (
            path,
            b"sealed",
            {**record, **({"status": "installed"} if kw["name"] == upgrade._KNOWN_NAME else {})},
        ),
    )
    monkeypatch.setattr(upgrade, "_plan_sha", lambda _: digest)
    monkeypatch.setattr(upgrade, "_upgrade_path", lambda *_: known)
    monkeypatch.setattr(upgrade, "_installed_release", lambda *_: record["new_release"])
    monkeypatch.setattr(native, "contract", lambda *_: None)
    monkeypatch.setattr(campaign, "qualify", lambda *_: [])
    calls = []
    monkeypatch.setattr(upgrade, "_old_module_bytes", lambda _: calls.append("modules"))
    monkeypatch.setattr(
        upgrade,
        "_sealed_pagination_reference",
        lambda *_: (calls.append("pagination") or reference),
    )
    monkeypatch.setattr(
        upgrade,
        "_completed_reference",
        lambda *_a, **_kw: pytest.fail(
            "scoped pagination predecessor must not use the completed reader"
        ),
    )
    monkeypatch.setattr(upgrade, "_old_native_terminal", lambda *_: None)
    monkeypatch.setattr(upgrade.host, "_lane_absent", lambda: None)
    inspection = {"highwater": {"anchor": {}}}
    monkeypatch.setattr(upgrade, "_old_custody_quiet", lambda *_: inspection)
    monkeypatch.setattr(upgrade, "fresh_read_native_index", lambda *_: {})
    monkeypatch.setattr(readmission, "_new_runtime", lambda *_: (new_path, manifest, raw, cfg))
    monkeypatch.setattr(policy, "validate_frontier", lambda value, **_: value)
    monkeypatch.setattr(readmission, "_guard_fresh", lambda *_: None)
    monkeypatch.setattr(upgrade.host, "_hold", lambda *_: nullcontext())
    monkeypatch.setattr(upgrade, "_inert_new_custody", lambda *_: {})
    monkeypatch.setattr(readmission, "_plan", lambda *_: ({}, digest))
    monkeypatch.setattr(readmission, "_intent_token", lambda *_: "token")

    class BeforeCreation(Exception):
        pass

    def stop_before_creation(*_):
        assert calls == ["modules", "pagination", "modules", "pagination"]
        raise BeforeCreation

    monkeypatch.setattr(readmission, "_recover_or_create", stop_before_creation)
    with pytest.raises(BeforeCreation):
        upgrade.prepare_held(seal, confirm_plan_sha256=digest)
    assert not destination.exists()
    assert not seal.with_name("source-upgrade-held-consumed.json").exists()


def test_historical_clock_reader_is_literal_facts_only_and_keeps_generic_readers_out():
    program = upgrade._historical_clock_reader_program()
    compile(program, "<historical-clock-reader>", "exec")
    assert repr(upgrade.REVIEWED_CLOCK_WORKER_ID) in program
    assert repr(upgrade.REVIEWED_CLOCK_RESULT_SHA256) in program
    assert "n.capacity_contract(m,control_plane_only=True)" in program
    assert "execution.get('admission_started_at')!=started" in program
    assert "mech._signature" in program
    assert "validate_stop" in program
    assert "n.validate_admission(" not in program
    assert "n.completed_round_proof(" not in program
    assert "for_effects=True" not in program


def test_historical_ownership_budget_reader_is_the_same_facts_only_reader_with_own_pins():
    program = upgrade._historical_ownership_budget_reader_program()
    compile(program, "<historical-ownership-budget-reader>", "exec")
    assert repr(upgrade.REVIEWED_OWNERSHIP_BUDGET_WORKER_ID) in program
    assert repr(upgrade.REVIEWED_OWNERSHIP_BUDGET_RESULT_SHA256) in program
    assert repr(upgrade.REVIEWED_CLOCK_WORKER_ID) not in program
    assert repr(upgrade.REVIEWED_CLOCK_RESULT_SHA256) not in program
    assert "mech._signature" in program
    assert "validate_stop" in program
    assert "n.validate_admission(" not in program
    assert "for_effects=True" not in program


@pytest.mark.parametrize(
    "mutated",
    [
        repr(upgrade.REVIEWED_CLOCK_WORKER_ID) * 2,
        repr(upgrade.REVIEWED_CLOCK_RESULT_SHA256) * 2,
    ],
)
def test_historical_ownership_budget_reader_refuses_nonunique_literal_substitutions(
    monkeypatch, mutated
):
    monkeypatch.setattr(upgrade, "_historical_clock_reader_program", lambda: mutated)

    with pytest.raises(upgrade.SourceUpgradeHeld, match="historical clock reader"):
        upgrade._historical_ownership_budget_reader_program()


@pytest.mark.parametrize("case", ["wrong-worker", "extra-worker", "continuation"])
def test_historical_clock_reader_refuses_other_custody_before_evidence_io(tmp_path, case):
    import closer_campaign as campaign

    worker = upgrade.REVIEWED_CLOCK_WORKER_ID
    state = {"workers": [{"worker_id": worker}]}
    if case == "wrong-worker":
        worker = "0" * 12
    elif case == "extra-worker":
        state["workers"].append({"worker_id": "0" * 12})
    else:
        state["coding_continuations"] = []
    namespace = {
        "w": worker,
        "d": state,
        "c": campaign,
        "s": tmp_path / "absent-campaign.json",
    }
    with pytest.raises(SystemExit) as caught:
        exec(upgrade._historical_clock_reader_program(), namespace)
    assert caught.value.code == 38
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw", [b"not JSON", b'{"native_admission":{}}'])
def test_historical_clock_reader_pins_result_bytes_before_decoding(tmp_path, raw):
    import closer_campaign as campaign

    worker = upgrade.REVIEWED_CLOCK_WORKER_ID
    state_path = tmp_path / "old-campaign.json"
    result_path = campaign.result_path(state_path, worker)
    result_path.parent.mkdir()
    result_path.write_bytes(raw)
    namespace = {
        "w": worker,
        "d": {"workers": [{"worker_id": worker}]},
        "c": campaign,
        "s": state_path,
        "hashlib": hashlib,
        "json": json,
    }
    # Real bounded cohort reads run here. Invalid JSON must be rejected by its
    # literal byte pin before parsing or consulting any authority-bearing data.
    with pytest.raises(SystemExit) as caught:
        exec(upgrade._historical_clock_reader_program(), namespace)
    assert caught.value.code == 58
    assert result_path.read_bytes() == raw


def test_old_diagnostic_refuses_a_non_source_module_closure_before_subprocess(
    tmp_path, monkeypatch
):
    # Isolate the module fence on every supported CI Python. The production
    # predecessor's physical interpreter is exercised separately.
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_OLD_INTERPRETER_SHA256",
        hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
    )
    module = tmp_path / "closer_correction_bridge.py"
    module.write_text("# exact old source\n")
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(
        json.dumps({"module_dir": str(tmp_path), "modules": {module.name: "0" * 64}})
    )
    state = tmp_path / "old.campaign.json"
    state.write_text('{"workers": []}')
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(upgrade.SourceUpgradeHeld, match="old .* unavailable"):
        upgrade._old_diagnostic(
            manifest, state, "worker", interpreter=Path(sys.executable).resolve()
        )
    assert calls == []


def test_installed_release_refuses_a_modified_inspector_before_execution(tmp_path, monkeypatch):
    """The global readback helper itself is a pinned pre-execution witness."""
    home = tmp_path / "home"
    mirror = home / ".codex/orchestrator-mirror"
    inspector = mirror / "scripts/install_verified_snapshot.py"
    inspector.parent.mkdir(parents=True)
    inspector.write_text("# substituted inspector\n")
    journal = tmp_path / "release.json"
    journal.write_text("{}")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    class Record:
        def __init__(self, _path):
            pass

        def read(self):
            return {
                "stage": "installed",
                "candidate_sha": "a" * 40,
                "installed_generation": "generation",
                "verification": {
                    "verified": True,
                    "snapshot_digest": "b" * 64,
                    "snapshot_path": str(snapshot),
                },
            }

    calls = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(closer_release, "ReleaseRecord", Record)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(upgrade.SourceUpgradeHeld, match="inspector differs"):
        upgrade._installed_release(journal, "a" * 40)
    assert calls == []


def _installed_release_fixture(tmp_path, monkeypatch, *, mutate=False):
    home = tmp_path / "home"
    mirror = home / ".codex/orchestrator-mirror"
    inspector = mirror / "scripts/install_verified_snapshot.py"
    inspector.parent.mkdir(parents=True)
    observed = tmp_path / "inspector-observed.json"
    inspector.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['INSPECTOR_OBSERVED']).write_text(json.dumps({'file': __file__, 'argv0': sys.argv[0], 'argv': sys.argv[1:]}))\n"
        "if os.environ.get('MUTATE_INSPECTOR') == '1' and '--generation-token' in sys.argv:\n"
        " pathlib.Path(__file__).write_text('# changed during inspector execution\\n')\n"
        "print('generation-v1:fixture' if '--generation-token' in sys.argv else 'd' * 64)\n"
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    journal = tmp_path / "release.json"
    journal.write_text("{}")

    class Record:
        def __init__(self, _path):
            pass

        def read(self):
            return {
                "stage": "installed",
                "candidate_sha": "a" * 40,
                "installed_generation": "generation-v1:fixture",
                "verification": {
                    "verified": True,
                    "snapshot_digest": "d" * 64,
                    "snapshot_path": str(snapshot),
                },
            }

    calls = []
    actual_run = upgrade.subprocess.run

    def capture_run(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_run(*args, **kwargs)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("INSPECTOR_OBSERVED", str(observed))
    if mutate:
        monkeypatch.setenv("MUTATE_INSPECTOR", "1")
    monkeypatch.setattr(closer_release, "ReleaseRecord", Record)
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_INSTALL_VERIFIED_SNAPSHOT_SHA256",
        hashlib.sha256(inspector.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(upgrade.subprocess, "run", capture_run)
    return journal, inspector, observed, calls


def test_installed_release_executes_the_pinned_inspector_with_physical_identity(
    tmp_path, monkeypatch
):
    journal, inspector, observed, calls = _installed_release_fixture(tmp_path, monkeypatch)

    release = upgrade._installed_release(journal, "a" * 40)

    assert release["installed_generation"] == "generation-v1:fixture"
    assert release["snapshot_digest"] == "d" * 64
    assert len(calls) == 2
    for args, kwargs in calls:
        command = args[0]
        assert command[:3] == [str(Path(sys.executable).resolve()), "-I", "-B"]
        assert kwargs["input"] == inspector.read_text()
    observed_call = json.loads(observed.read_text())
    assert observed_call["file"] == observed_call["argv0"] == str(inspector)
    assert observed_call["argv"][-1] == "--digest"


def test_installed_release_refuses_inspector_mutation_during_execution(tmp_path, monkeypatch):
    journal, _inspector, _observed, calls = _installed_release_fixture(
        tmp_path, monkeypatch, mutate=True
    )

    with pytest.raises(upgrade.SourceUpgradeHeld, match="inspector changed"):
        upgrade._installed_release(journal, "a" * 40)
    assert len(calls) == 2


def _shared_digest_installed_release_fixture(tmp_path, monkeypatch, *, mutate_helper=False):
    home = tmp_path / "home"
    mirror = home / ".codex/orchestrator-mirror"
    inspector = mirror / "scripts/install_verified_snapshot.py"
    helper = mirror / "source_snapshot_digest.py"
    inspector.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(
        "def snapshot_digest(snapshot, *, installer_name):\n"
        "    assert installer_name == 'install_verified_snapshot.py'\n"
        "    return 'd' * 64\n"
    )
    inspector.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
        "import source_snapshot_digest as shared\n"
        "if Path(shared.__file__) != Path(__file__).resolve().parents[1] / 'source_snapshot_digest.py':\n"
        " raise SystemExit(9)\n"
        "if '--generation-token' in sys.argv:\n"
        " if __import__('os').environ.get('MUTATE_HELPER') == '1':\n"
        "  Path(__file__).resolve().parents[1].joinpath('source_snapshot_digest.py').write_text(\"VALUE = 'evil'\\n\")\n"
        " print('generation-v1:fixture')\n"
        "else:\n"
        " print(shared.snapshot_digest(Path(sys.argv[1]), installer_name=Path(__file__).name))\n"
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    journal = tmp_path / "release.json"
    journal.write_text("{}")

    class Record:
        def __init__(self, _path):
            pass

        def read(self):
            return {
                "stage": "installed",
                "candidate_sha": "a" * 40,
                "installed_generation": "generation-v1:fixture",
                "verification": {
                    "verified": True,
                    "snapshot_digest": "d" * 64,
                    "snapshot_path": str(snapshot),
                },
            }

    calls = []
    actual_run = upgrade.subprocess.run

    def capture_run(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_run(*args, **kwargs)

    monkeypatch.setenv("HOME", str(home))
    if mutate_helper:
        monkeypatch.setenv("MUTATE_HELPER", "1")
    monkeypatch.setattr(closer_release, "ReleaseRecord", Record)
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_SHARED_DIGEST_INSTALLER_SHA256",
        hashlib.sha256(inspector.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_SHARED_DIGEST_HELPER_SHA256",
        hashlib.sha256(helper.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(upgrade.subprocess, "run", capture_run)
    return journal, inspector, helper, calls


def test_installed_release_preloads_the_pinned_shared_digest_helper(tmp_path, monkeypatch):
    journal, inspector, helper, calls = _shared_digest_installed_release_fixture(
        tmp_path, monkeypatch
    )

    release = upgrade._installed_release(journal, "a" * 40)

    assert release["digest_helper"] == str(helper)
    assert release["digest_helper_sha256"] == hashlib.sha256(helper.read_bytes()).hexdigest()
    assert helper.parent == inspector.parents[1]
    assert not (helper.parent / "src").exists()
    assert len(calls) == 2
    for args, kwargs in calls:
        command = args[0]
        assert command[5:7] == [str(inspector), str(helper)]
        payload = json.loads(kwargs["input"])
        assert payload == {"helper": helper.read_text(), "inspector": inspector.read_text()}


def test_installed_release_refuses_unpinned_shared_digest_helper_before_execution(
    tmp_path, monkeypatch
):
    journal, _inspector, helper, calls = _shared_digest_installed_release_fixture(
        tmp_path, monkeypatch
    )
    helper.write_text("def snapshot_digest(*args, **kwargs): return 'substituted'\n")

    with pytest.raises(upgrade.SourceUpgradeHeld, match="digest helper differs"):
        upgrade._installed_release(journal, "a" * 40)
    assert calls == []


def test_installed_release_refuses_missing_shared_digest_helper_before_execution(
    tmp_path, monkeypatch
):
    journal, _inspector, helper, calls = _shared_digest_installed_release_fixture(
        tmp_path, monkeypatch
    )
    helper.unlink()

    with pytest.raises(upgrade.SourceUpgradeHeld, match="digest helper unavailable"):
        upgrade._installed_release(journal, "a" * 40)
    assert calls == []


def test_installed_release_detects_shared_helper_mutation_after_captured_execution(
    tmp_path, monkeypatch
):
    journal, _inspector, _helper, calls = _shared_digest_installed_release_fixture(
        tmp_path, monkeypatch, mutate_helper=True
    )

    with pytest.raises(upgrade.SourceUpgradeHeld, match="digest helper changed"):
        upgrade._installed_release(journal, "a" * 40)
    # The second invocation still used captured helper bytes; a mutable import
    # would instead fail the digest readback with the substituted value.
    assert len(calls) == 2


def test_identity_rejects_principal_epoch_and_anchor_changes():
    old = {
        "policy": {
            "correction_controller_installation": {
                "config": {
                    "repository": "stranske/Workflows",
                    "repository_id": 1,
                    "epoch": "e",
                    "writer_id": 2,
                    "writer_login": "owner",
                    "writer_type": "User",
                    "bootstrap_anchor_sha256": "a",
                }
            }
        }
    }
    for key in old["policy"]["correction_controller_installation"]["config"]:
        new = json.loads(json.dumps(old))
        new["policy"]["correction_controller_installation"]["config"][key] = "changed"
        with pytest.raises(upgrade.SourceUpgradeHeld, match="identity changed"):
            upgrade._same_identity(old, new)


def test_lawful_policy_rebase_refuses_unreviewed_controller_policy_changes(tmp_path):
    """Only the reviewed source/root/transport/helper substitutions may change."""
    module_dir = tmp_path / "runtime"
    module_dir.mkdir()
    helper = module_dir / "closer_codex_confinement_probe.py"
    helper.write_text("# confinement probe\n")
    effects = "e" * 64
    old_policy = {
        "correction_controller_installation": {
            "config": {
                "source_sha": "a" * 40,
                "state_path": "/private/tmp/old.campaign.json",
                "native_transport_source_sha256": "b" * 64,
                "max_entries": 4,
                "consumers": {
                    "native": {
                        "allowed_paths": ["/private/tmp/old"],
                        "native_bootstrap": {
                            "private_root": "/private/tmp/old-private",
                            "confinement": {
                                "helper_path": "/private/tmp/old-probe.py",
                                "helper_sha256": "c" * 64,
                            },
                        },
                    }
                },
            }
        },
        "publication": {"target": "fixed", "permissions": ["read"]},
    }
    new_state = tmp_path / "campaigns" / "fresh.campaign.json"
    new_state.parent.mkdir()
    new = {
        "candidate_sha": "d" * 40,
        "module_dir": str(module_dir),
        "modules": {
            "closer_correction_native_effects.py": effects,
            helper.name: hashlib.sha256(helper.read_bytes()).hexdigest(),
        },
        "policy": json.loads(json.dumps(old_policy)),
    }
    config = new["policy"]["correction_controller_installation"]["config"]
    config["source_sha"] = new["candidate_sha"]
    config["state_path"] = str(new_state)
    config["native_transport_source_sha256"] = effects
    bootstrap = config["consumers"]["native"]["native_bootstrap"]
    bootstrap["private_root"] = str(new_state.parent.parent / "controller-private")
    bootstrap["confinement"] = {
        "helper_path": str(helper),
        "helper_sha256": new["modules"][helper.name],
    }

    assert upgrade._rebased_policy({"policy": old_policy}, new, new_state) == new["policy"]
    for mutate in (
        lambda value: value["correction_controller_installation"]["config"].__setitem__(
            "max_entries", 99
        ),
        lambda value: value["correction_controller_installation"]["config"]["consumers"][
            "native"
        ].__setitem__("allowed_paths", ["/elsewhere"]),
        lambda value: value["correction_controller_installation"]["config"].__setitem__(
            "native_transport_source_sha256", "f" * 64
        ),
        lambda value: value["publication"].__setitem__("permissions", ["write"]),
    ):
        changed = json.loads(json.dumps(new))
        mutate(changed["policy"])
        with pytest.raises(upgrade.SourceUpgradeHeld, match="outside lawful rebase"):
            upgrade._rebased_policy({"policy": old_policy}, changed, new_state)


def test_rebase_rebinds_only_owned_stdio_reader_to_current_runtime_digest(tmp_path):
    module_dir = tmp_path / "runtime"
    module_dir.mkdir()
    helper = module_dir / "closer_codex_confinement_probe.py"
    helper.write_text("# confinement probe\n")
    old_reader_sha, new_reader_sha = "a" * 64, "d" * 64

    def consumer(entrypoint, reader_sha256):
        return {
            "reader_entrypoint": entrypoint,
            "reader_source_sha256": reader_sha256,
            "native_bootstrap": {
                "private_root": "/private/tmp/old-private",
                "confinement": {
                    "helper_path": "/private/tmp/old-probe.py",
                    "helper_sha256": "c" * 64,
                },
            },
        }

    old_policy = {
        "correction_controller_installation": {
            "config": {
                "source_sha": "a" * 40,
                "state_path": "/private/tmp/old.campaign.json",
                "native_transport_source_sha256": "b" * 64,
                "consumers": {
                    "owned": consumer("closer_codex_owned_stdio.SourceReader", old_reader_sha),
                    "legacy": consumer(
                        "closer_correction_controller.NativeConsumerReader", old_reader_sha
                    ),
                    "unknown": consumer("example.Reader", old_reader_sha),
                },
            }
        }
    }
    new_state = tmp_path / "campaigns" / "fresh.campaign.json"
    new_state.parent.mkdir()
    expected = json.loads(json.dumps(old_policy))
    config = expected["correction_controller_installation"]["config"]
    config["source_sha"] = "e" * 40
    config["state_path"] = str(new_state)
    config["native_transport_source_sha256"] = "f" * 64
    for value in config["consumers"].values():
        value["native_bootstrap"]["private_root"] = str(
            new_state.parent.parent / "controller-private"
        )
        value["native_bootstrap"]["confinement"] = {
            "helper_path": str(helper),
            "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        }
    config["consumers"]["owned"]["reader_source_sha256"] = new_reader_sha
    new = {
        "candidate_sha": "e" * 40,
        "module_dir": str(module_dir),
        "modules": {
            "closer_correction_native_effects.py": "f" * 64,
            helper.name: hashlib.sha256(helper.read_bytes()).hexdigest(),
            "closer_codex_owned_stdio.py": new_reader_sha,
        },
        "policy": expected,
    }
    actual = upgrade._rebased_policy({"policy": old_policy}, new, new_state)
    consumers = actual["correction_controller_installation"]["config"]["consumers"]
    assert consumers["owned"]["reader_source_sha256"] == new_reader_sha
    assert consumers["legacy"]["reader_source_sha256"] == old_reader_sha
    assert consumers["unknown"]["reader_source_sha256"] == old_reader_sha
    for bad in (None, "short", "D" * 64):
        changed = json.loads(json.dumps(new))
        changed["modules"]["closer_codex_owned_stdio.py"] = bad
        with pytest.raises(
            upgrade.SourceUpgradeHeld, match="owned stdio reader source unavailable"
        ):
            upgrade._rebased_policy({"policy": old_policy}, changed, new_state)


@pytest.mark.parametrize(
    ("principal", "message"),
    [
        ({"id": 7, "login": "owner", "type": "User"}, None),
        ({"id": 8, "login": "different", "type": "User"}, "native index principal changed"),
    ],
)
def test_fresh_index_is_get_only_and_rejects_principal_drift(monkeypatch, principal, message):
    config = {
        "state_path": "/private/tmp/state",
        "repository": "stranske/Orchestrator",
        "repository_id": 1,
        "epoch": "e" * 64,
        "bootstrap_source_sha": "a" * 40,
        "source_sha": "a" * 40,
        "writer_id": 7,
        "writer_login": "owner",
        "writer_type": "User",
        "bootstrap_anchor_sha256": "b" * 64,
        "max_bytes": 1024,
        "max_entries": 4,
        "max_subjects": 4,
    }
    data = {
        "schema": controller.SCHEMA,
        "repository": config["repository"],
        "repository_id": 1,
        "epoch": config["epoch"],
        "bootstrap_source_sha": config["bootstrap_source_sha"],
        "revision": 0,
        "previous_sha256": None,
        "last_action": None,
        "entries": [],
    }
    raw = json.dumps(data, separators=(",", ":")).encode()
    inspection = {
        "highwater": {
            "anchor": {
                "commit_sha": "c" * 40,
                "blob_sha": "blob",
                "state_sha256": controller.digest(data),
            }
        }
    }
    calls = []
    replies = [
        {"object": {"sha": "c" * 40}},
        {"sha": "blob", "encoding": "base64", "content": base64.b64encode(raw).decode()},
        principal,
        {"id": 1, "full_name": "stranske/Orchestrator"},
    ]

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies.pop(0)))

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    if message:
        with pytest.raises(upgrade.SourceUpgradeHeld, match=message):
            upgrade.fresh_read_native_index(config, inspection)
    else:
        assert upgrade.fresh_read_native_index(config, inspection)["commit_sha"] == "c" * 40
    assert all(argv[:2] == ["gh", "api"] for argv in calls)
