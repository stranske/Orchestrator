"""Native-only scoped publication with immutable original invocation contexts."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_native_observer import host as host
from test_closer_reasoning_boundary import native_world as native_world

import closer_campaign as campaign
import closer_dispatcher
import closer_mechanisms as mechanisms
import closer_native_observer as native
import closer_operational_capacity as scoped

_REAL_PIPELINE_CAPACITY = closer_dispatcher._pipeline_capacity
_REAL_FRESH_CAPACITY = closer_dispatcher._fresh_capacity_report
_REAL_TIME = time.time


@pytest.fixture
def capacity_host(host, monkeypatch):
    manifest, path, wrapper = host
    manifest["mechanism_contract"]["capacity_publication"] = {
        "schema": "native-scoped-capacity/v1",
        "owner": "native-capacity-owner",
        "profile_ids": ["claude-sonnet-5-5-low"],
        "bounded_trial_agents": ["claude"],
        "admission_seconds": 600,
        "continue_on_assignment_change": True,
    }
    campaign.write_manifest(path, manifest)
    root = Path(manifest["mechanism_contract"]["observation_root"])
    mechanisms._key(root, create=True)
    source = (
        Path.home()
        / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
    )
    source.write_text('{"targets":[{"target":"offline/repo#1"}]}')
    state = Path(manifest["mechanism_contract"]["campaign_root"]) / "native.campaign.json"
    import closer_pipeline

    cfg = {
        "coordinator_contract_pin": closer_pipeline.scoped_capability(),
        "mechanism": mechanisms.NATIVE,
        "policy": manifest["policy"],
        "runtime_manifest": str(path),
        "apply": True,
        "coordinator_mode": "resource_scoped",
        "granularity": "round",
        "receipt_reserve_seconds": 180,
        "original_deadline": 42,
        "budget_seconds": 100,
        "dispatch_state": str(state.parent / "dispatch.json"),
        "assignment": str(state.parent / "old-native-assignment.json"),
        "native_hooks": {"postrun_dependencies": {"assignment": {"path": "old", "sha256": "old"}}},
    }
    obj = campaign.Campaign(state)
    doc = obj.begin(
        "native-capacity-test",
        ["offline/repo#1"],
        owner="owner",
        release_pin=manifest["package_digest"],
        worker_config=cfg,
    )
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def invocation(index):
        nonce = f"{index:032x}"
        (root / nonce).mkdir(mode=0o700)
        native._CURRENT_ENTRY[str(root)] = nonce
        native._CURRENT_ADMISSION[str(root)] = {
            "nonce": nonce,
            "started_at": clock[0],
            "original_deadline": clock[0] + 600,
        }
        native._write(
            root / "native-active.json",
            {"nonce": nonce, "status": "submitted", "binding": native.binding(manifest)},
        )

    invocation(1)
    yield manifest, path, obj, doc, source, clock, invocation
    native._CURRENT_ENTRY.pop(str(root), None)
    native._CURRENT_ADMISSION.pop(str(root), None)


def publish(fixture):
    manifest, path, obj, doc, *_ = fixture
    return native.publish_admission(manifest, path, obj.path, doc)


def test_two_normal_invocations_preserve_old_assignment_report_and_original_window(capacity_host):
    manifest, path, obj, doc, source, clock, invocation = capacity_host
    original_cfg = copy.deepcopy(doc["worker_config"])
    first = publish(capacity_host)
    paths = [Path(first[key]["path"]) for key in ("assignment", "report")]
    old = {p: p.read_bytes() for p in paths}
    assert scoped.validate_report(native._read(paths[1]), now=clock[0])
    clock[0] += 3600
    assert not scoped.validate_report(native._read(paths[1]), now=clock[0])
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    invocation(2)
    second = publish(capacity_host)
    assert second["original_deadline"] == clock[0] + 600
    assert second["assignment"]["path"] != first["assignment"]["path"]
    assert Path(second["assignment"]["path"]).read_bytes() == source.read_bytes()
    assert all(p.read_bytes() == raw for p, raw in old.items())
    assert doc["worker_config"] == original_cfg
    native.validate_admission(manifest, first, for_effects=False)
    with pytest.raises(mechanisms.MechanismError, match="expired"):
        native.validate_admission(manifest, first, for_effects=True)


@pytest.mark.parametrize(
    "state,outcome",
    [
        ("active", None),
        ("launched", None),
        ("unknown", None),
        ("terminal", "unknown"),
        ("denied", "denied"),
    ],
)
def test_active_unknown_or_denied_worker_cannot_receive_new_publication(
    capacity_host, state, outcome
):
    manifest, path, obj, doc, *_ = capacity_host
    doc["workers"] = [{"state": state, "outcome": outcome}]
    with pytest.raises(mechanisms.MechanismError, match="new pending"):
        publish(capacity_host)
    root = Path(manifest["mechanism_contract"]["observation_root"])
    assert not list(root.glob("*/scoped-capacity.json"))


def test_current_quota_marker_wins_without_provider_or_marker_mutation(capacity_host):
    manifest, _path, obj, _doc, *_ = capacity_host
    marker = Path.home() / ".codex/handoff/capacity-shed/claude"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"expires_at":2000,"category":"quota"}')
    raw = marker.read_bytes()
    context = publish(capacity_host)
    report = native._read(Path(context["report"]["path"]))
    assert report["agents"]["claude"]["state"] == "shed"
    assert report["profiles"]["claude-sonnet-5-5-low"]["state"] == "shed"
    assert marker.read_bytes() == raw
    assert obj.read()["workers"] == []


def test_new_hold_invalidates_published_report_without_refresh_or_new_budget(capacity_host):
    manifest, *_ = capacity_host
    context = publish(capacity_host)
    raw = Path(context["report"]["path"]).read_bytes()
    marker = Path.home() / ".codex/handoff/capacity-shed/claude"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"expires_at":2000,"category":"quota"}')
    with pytest.raises(mechanisms.MechanismError, match="inputs changed"):
        publish(capacity_host)
    assert Path(context["report"]["path"]).read_bytes() == raw
    assert context["original_deadline"] == 1600


def test_exact_worker_handoff_uses_original_context_and_budgets(capacity_host):
    manifest, path, obj, doc, _source, clock, _invocation = capacity_host
    context = publish(capacity_host)
    worker_id = "abcdef012345"
    result = campaign.result_path(obj.path, worker_id)
    fields = {
        "state": str(obj.path),
        "target": campaign.ROUND_UNIT,
        "owner": doc["owner"],
        "generation": "1",
        "result": str(result),
        "runtime-manifest": str(path),
        "mechanism": mechanisms.NATIVE,
    }
    argv = [
        manifest["mechanism_contract"]["interpreter"]["path"],
        manifest["module_path"],
        "worker",
    ]
    for key, value in fields.items():
        argv.extend(["--" + key, value])
    native.authorize_worker(manifest, argv)
    accepted = native.accept_worker(
        manifest,
        state=str(obj.path),
        target=campaign.ROUND_UNIT,
        owner=doc["owner"],
        generation=1,
        result=str(result),
        manifest_path=str(path),
        campaign_id=doc["campaign_id"],
    )
    assert accepted == context
    cfg = native.admission_config(manifest, doc["worker_config"], accepted)
    assert cfg["original_deadline"] == 1600
    assert cfg["dispatch_budget_seconds"] == 105
    assert cfg["assignment"] == context["assignment"]["path"]
    assert cfg["capacity_report"] == context["report"]["path"]
    worker = {"worker_id": worker_id, "target": campaign.ROUND_UNIT}
    clock[0] = 5000
    restored = native.result_admission_config(doc["worker_config"], context, obj.path, doc, worker)
    assert restored["assignment"] == cfg["assignment"]
    assert restored["original_deadline"] == 1600
    with pytest.raises(mechanisms.MechanismError, match="expired"):
        native.admission_config(manifest, doc["worker_config"], accepted)


@pytest.mark.parametrize("change", ["assignment", "report", "deadline", "campaign", "signature"])
def test_changed_context_fails_closed(capacity_host, change):
    manifest, *_ = capacity_host
    context = publish(capacity_host)
    if change in {"assignment", "report"}:
        Path(context[change]["path"]).write_text("changed")
    elif change == "signature":
        root = Path(manifest["mechanism_contract"]["observation_root"])
        value = native._read(root / context["nonce"] / "admission.json")
        value["signature"] = "f" * 64
        native._write(root / context["nonce"] / "admission.json", value)
    elif change == "campaign":
        context["campaign"]["owner"] = "foreign"
    else:
        context["original_deadline"] += 1
    with pytest.raises(mechanisms.MechanismError):
        native.validate_admission(manifest, context, for_effects=True)


def routed_report(path):
    report = scoped._read(Path(path))[0]
    assert scoped.validate_report(report, now=time.time())
    row = report["profiles"]["claude-sonnet-5-5-low"]
    return (
        {"agent": "claude", "profile_id": "claude-sonnet-5-5-low"}
        if row["state"] in {"ok", "warn"}
        else {"hold": row["state"]}
    )


@pytest.mark.parametrize("alive", [True, False])
def test_real_tick_never_republishes_for_active_or_unknown_worker(capacity_host, alive):
    manifest, path, obj, _doc, _source, clock, invocation = capacity_host
    launches = []

    def launch(argv, _log):
        launches.append(argv)
        return 60001

    kwargs = dict(
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=routed_report,
        native_probe=lambda *_: None,
        launcher=launch,
        max_steps=1,
        pid_alive=lambda _: True,
    )
    assert campaign.tick(obj.path.parent, path, **kwargs)[0]["kind"] == "launched"
    root = Path(manifest["mechanism_contract"]["observation_root"])
    old = {p: p.read_bytes() for p in root.glob("*/scoped-capacity.json")}
    native._write(
        root / "native-active.json",
        {"nonce": f"{1:032x}", "status": "terminal", "binding": native.binding(manifest)},
    )
    clock[0] += 3600
    invocation(2)
    kwargs["pid_alive"] = lambda _: alive
    result = campaign.tick(obj.path.parent, path, **kwargs)
    assert result[0]["kind"] == ("wait" if alive else "reconcile")
    assert len(launches) == 1
    assert {p: p.read_bytes() for p in root.glob("*/scoped-capacity.json")} == old
    assert not (root / f"{2:032x}" / "assignment.json").exists()


def test_real_tick_second_known_no_effect_invocation_refreshes_without_rewriting_old_context(
    capacity_host,
):
    manifest, path, obj, _doc, source, clock, invocation = capacity_host
    launches = []
    kwargs = dict(
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=routed_report,
        native_probe=lambda *_: None,
        launcher=lambda argv, _log: launches.append(argv) or 60001,
        max_steps=1,
        pid_alive=lambda _: True,
    )
    assert campaign.tick(obj.path.parent, path, **kwargs)[0]["kind"] == "launched"
    first_worker = obj.read()["workers"][0]
    first_context = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["context"]
    old_assignment = Path(first_context["assignment"]["path"]).read_bytes()
    # A real durable terminal no-start result is distinct from absent/unknown output.
    campaign.write_result(
        campaign.result_path(obj.path, first_worker["worker_id"]),
        first_worker["worker_id"],
        campaign.ROUND_UNIT,
        "unavailable",
        "fixture: denied before effects",
        mechanism=mechanisms.NATIVE,
        native_admission=first_context,
    )
    clock[0] += 3600
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    invocation(2)
    assert campaign.tick(obj.path.parent, path, **kwargs)[0]["kind"] == "launched"
    assert len(launches) == 2
    new_context = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["context"]
    assert new_context["original_deadline"] == clock[0] + 600
    assert new_context["assignment"]["path"] != first_context["assignment"]["path"]
    assert Path(first_context["assignment"]["path"]).read_bytes() == old_assignment
    assert obj.read()["worker_config"]["original_deadline"] == 42


def test_real_tick_quota_hold_does_not_launch_provider_worker(capacity_host):
    _manifest, path, obj, *_ = capacity_host
    marker = Path.home() / ".codex/handoff/capacity-shed/claude"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"expires_at":2000,"category":"quota"}')
    result = campaign.tick(
        obj.path.parent,
        path,
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=routed_report,
        native_probe=lambda *_: None,
        launcher=lambda *_a: pytest.fail("no worker/provider may launch"),
    )
    assert result[0]["kind"] == "wait" and result[0]["reason"] == "capacity_exhausted"
    assert obj.read()["workers"] == []


def test_real_worker_consumes_signed_original_context_without_mutating_campaign_cfg(
    capacity_host, monkeypatch
):
    import closer_runtime_binding as binding

    manifest, path, obj, _doc, *_ = capacity_host
    captured = {}

    def launch(argv, _log):
        captured["argv"] = argv
        return 60001

    campaign.tick(
        obj.path.parent,
        path,
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=routed_report,
        native_probe=lambda *_: None,
        launcher=launch,
        max_steps=1,
        pid_alive=lambda _: True,
    )
    row = obj.read()["workers"][0]
    monkeypatch.setattr(campaign, "qualify", lambda *_: [])

    def round_run(command, cfg, **_kw):
        captured["command"], captured["cfg"] = command, cfg
        return "unavailable", "fixture: no native effect started"

    monkeypatch.setattr(binding, "run_native_round", round_run)
    output = campaign.result_path(obj.path, row["worker_id"])
    assert (
        campaign.run_worker(
            str(obj.path),
            str(output),
            campaign.ROUND_UNIT,
            "owner",
            1,
            holder=lambda *_: {"agent": campaign.AGENT},
            native_probe=lambda *_: None,
            runtime_manifest=str(path),
            mechanism=mechanisms.NATIVE,
        )
        == "unavailable"
    )
    effective = captured["cfg"]
    assert effective["original_deadline"] == 1600
    assert effective["assignment"].endswith("/assignment.json")
    assert captured["command"][captured["command"].index("--budget-seconds") + 1] == "105"
    assert json.loads(output.read_text())["native_admission"]["original_deadline"] == 1600
    assert obj.read()["worker_config"]["original_deadline"] == 42


def test_hooks_accept_only_signed_nonce_snapshot_and_recheck_current_controller(capacity_host):
    import sys

    import closer_runtime_binding as binding

    manifest, path, _obj, doc, source, *_ = capacity_host
    context = publish(capacity_host)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    cfg["runtime_manifest"] = str(path)
    mirror = Path.home() / ".codex/orchestrator-mirror/closer_dispatcher.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_bytes(Path(binding.__file__).with_name("closer_dispatcher.py").read_bytes())
    for phase in ("prerun", "postrun"):
        script = path.parent / ("fake-" + phase)
        script.write_text(
            "#!/bin/bash\n# HANDOFF_EXACT_TOKEN_V1\n# CLOSER_BOUNDED_NO_RELAY_V1\n# CLOSER_NATIVE_ADMISSION_V1\n# CLOSER_NAMED_ROUND_V1\n"
        )
        cfg["native_hooks"][phase] = {"path": str(script), "sha256": native._hash(script)}
    module = Path(binding.__file__).with_name("closer_native_lease.py")
    cfg["native_hooks"].update(
        lease_module={"path": str(module), "sha256": native._hash(module)},
        bounded_no_new_relay=True,
    )
    cfg["native_hooks"]["postrun_dependencies"].update(
        interpreter={
            "path": str(Path(sys.executable).resolve()),
            "sha256": native._hash(Path(sys.executable).resolve()),
        },
        dispatcher={"path": str(mirror), "sha256": native._hash(mirror)},
    )
    cfg["handoff_agent"] = "cursor"
    hooks = binding.PinnedNativeHooks(cfg, runner=lambda *_a, **_k: pytest.fail("no native effect"))
    assert hooks.dependencies["assignment"][0] == Path(context["assignment"]["path"])
    assert hooks.env["CLOSER_POSTRUN_ASSIGNMENT"] == context["assignment"]["path"]
    assert hooks._dependencies_match(admission=True)
    source.write_text('{"targets":[{"target":"offline/repo#3"}]}')
    assert not hooks._dependencies_match(admission=True)
    assert hooks._dependencies_match()
    changed = copy.deepcopy(cfg)
    changed["assignment"] = str(source)
    with pytest.raises(binding.BindingError, match="share immutable"):
        binding.PinnedNativeHooks(changed, runner=lambda *_a, **_k: pytest.fail("no effect"))


def test_elapsed_original_budget_is_not_renewed_when_worker_starts_late(capacity_host):
    manifest, _path, _obj, doc, _source, clock, *_ = capacity_host
    context = publish(capacity_host)
    clock[0] = 1300
    with pytest.raises(mechanisms.MechanismError, match="no B/C/inner receipt budget"):
        native.admission_config(manifest, doc["worker_config"], context)
    assert context["original_deadline"] == 1600


def completed_standing_round(fixture, *, standing=True):
    from collections import Counter

    import test_closer_quarantine_integration as pipeline_fixture

    manifest, path, obj, _doc, *_ = fixture
    manifest["mechanism_contract"]["capacity_publication"][
        "continue_on_assignment_change"
    ] = standing
    campaign.write_manifest(path, manifest)
    root = native.contract(manifest)[3]
    active = native._read(root / "native-active.json")
    active["binding"] = native.binding(manifest)
    native._write(root / "native-active.json", active)
    campaign.tick(
        obj.path.parent,
        path,
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=routed_report,
        native_probe=lambda *_: None,
        launcher=lambda *_a: 60001,
        max_steps=1,
        pid_alive=lambda _: True,
    )
    worker = obj.read()["workers"][0]
    context = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["context"]
    cfg = native.admission_config(manifest, obj.read()["worker_config"], context)
    state = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    aggregate = pipeline_fixture.pipeline.run_pipeline(
        state,
        ["offline/repo#1"],
        **pipeline_fixture.callbacks(Counter()),
        resource_keys=lambda t, _c: pipeline_fixture.inventory(t),
        coordinator_mode="resource_scoped",
        deadline=time.time() + 15,
        max_io_workers=2,
        max_low_workers=1,
        max_exception_workers=1,
        capacity_fn=lambda: {"io": 2, "low": 1, "exception": 1},
        now=lambda: time.time(),
    )
    proof = native.completed_round_proof(manifest, cfg, aggregate, record_boundary=True)
    assert proof is not None, aggregate
    result = campaign.result_path(obj.path, worker["worker_id"])
    campaign.write_result(
        result,
        worker["worker_id"],
        campaign.ROUND_UNIT,
        "ok",
        json.dumps({"completed_round": proof}),
        mechanism=mechanisms.NATIVE,
        native_admission=context,
    )
    doc = campaign.reconcile(obj)
    assert doc["targets"] == {campaign.ROUND_UNIT: "done"}
    return cfg, context, proof, aggregate, result


def test_standing_rearms_only_changed_assignment_preserving_original_history(capacity_host):
    manifest, _path, obj, _doc, source, clock, _invocation = capacity_host
    _cfg, context, _proof, _aggregate, result = completed_standing_round(capacity_host)
    old = {p: p.read_bytes() for p in (Path(context["assignment"]["path"]), result)}
    doc = obj.read()
    assert native.rearm_on_assignment_change(obj, doc, manifest) == doc
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    clock[0] += 3600
    updated = native.rearm_on_assignment_change(obj, doc, manifest)
    assert updated["targets"] == {campaign.ROUND_UNIT: "pending"}
    assert updated["generation"] == 2 and updated["phase"] == "work"
    assert updated["workers"] == doc["workers"]
    transition = updated["assignment_transitions"][0]
    assert transition["accepted"] is False
    assert transition["previous_assignment_sha256"] == context["assignment"]["sha256"]
    assert all(p.read_bytes() == raw for p, raw in old.items())


def test_unrelated_new_quarantine_does_not_revoke_original_known_completion(
    capacity_host, tmp_path
):
    import test_closer_quarantine_integration as pipeline_fixture

    import closer_resources

    manifest, _path, obj, _doc, source, _clock, _invocation = capacity_host
    cfg, _context, proof, aggregate, _result = completed_standing_round(capacity_host)
    _raw, legacy, _held = pipeline_fixture.original_state(tmp_path / "unrelated-history")
    state = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    state.write_bytes(legacy.read_bytes())
    verdict = closer_resources.validate_stop(state, aggregate["round_id"], ["offline/repo#1"])
    assert verdict["global_stop_valid"] is False and verdict["scoped_completion_valid"] is True
    assert native.completed_round_proof(manifest, cfg, proof) == proof
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    assert native.rearm_on_assignment_change(obj, obj.read(), manifest)["generation"] == 2


@pytest.mark.parametrize(
    "damage", ["null-proof", "row-changed", "receipt-changed", "missing-boundary", "signature"]
)
def test_standing_never_rearms_missing_or_changed_original_success(capacity_host, damage):
    import closer_resources

    manifest, _path, obj, _doc, source, *_ = capacity_host
    cfg, context, proof, aggregate, result = completed_standing_round(capacity_host)
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    if damage == "null-proof":
        value = json.loads(result.read_text())
        value["detail"] = json.dumps({"completed_round": None})
        result.write_text(json.dumps(value))
    elif damage == "row-changed":
        root = closer_resources.scoped_root(Path(str(cfg["dispatch_state"]) + ".pipeline.json"))
        shard = closer_resources.shard_dir(root, "offline/repo#1") / "state.json"
        value = json.loads(shard.read_text())
        value["runs"][aggregate["run_ids"]["offline/repo#1"]]["targets"]["offline/repo#1"][
            "tampered"
        ] = True
        shard.write_text(json.dumps(value))
    elif damage == "receipt-changed":
        with Path(proof["receipt_path"]).open("ab") as stream:
            stream.write(b" ")
    else:
        boundary = (
            native.contract(manifest)[3]
            / context["nonce"]
            / ("completed-round-" + proof["round_id"] + ".json")
        )
        if damage == "missing-boundary":
            boundary.unlink()
        else:
            value = native._read(boundary)
            value["signature"] = "f" * 64
            native._write(boundary, value)
    with pytest.raises((mechanisms.MechanismError, KeyError)):
        native.rearm_on_assignment_change(obj, obj.read(), manifest)
    assert obj.read()["targets"] == {campaign.ROUND_UNIT: "done"}
    assert obj.read()["generation"] == 1


def test_native_finite_campaign_does_not_rearm_without_explicit_flag(capacity_host):
    manifest, _path, obj, _doc, source, *_ = capacity_host
    completed_standing_round(capacity_host, standing=False)
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    doc = obj.read()
    assert native.rearm_on_assignment_change(obj, doc, manifest) == doc


@pytest.mark.parametrize("a_seconds,expected", [(20, "85"), (110, None)])
def test_action_b_budget_is_clamped_after_a_within_original_window(
    capacity_host, a_seconds, expected
):
    import subprocess
    import sys

    import closer_runtime_binding as binding

    manifest, _path, _obj, doc, _source, clock, *_ = capacity_host
    context = publish(capacity_host)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "unused")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks._dependencies_match = lambda: True
    hooks.owns = lambda token: token == "original-token"
    events = []

    def prerun(_deadline):
        clock[0] += a_seconds
        return 0, "original-token"

    hooks.prerun = prerun

    def postrun(token, _deadline):
        events.append(("C", token))
        hooks.postrun_result = {"code": 0, "dependencies_valid": True}
        return 0

    hooks.postrun = postrun

    def runner(argv, **kwargs):
        events.append(("B", argv[argv.index("--budget-seconds") + 1]))
        return subprocess.CompletedProcess(argv, 78, stdout="{}")

    binding.run_native_round(
        [
            sys.executable,
            "offline.py",
            "--assignment",
            cfg["assignment"],
            "--budget-seconds",
            "105",
        ],
        cfg,
        runner=runner,
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert events == ([] if expected is None else [("B", expected)]) + [("C", "original-token")]
    assert cfg["original_deadline"] == 1600


@pytest.mark.parametrize("state", ["hold", "complete", "release"])
def test_standing_preserves_human_hold_completed_phase_and_release(capacity_host, state):
    manifest, _path, obj, _doc, source, *_ = capacity_host
    completed_standing_round(capacity_host)
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')

    def update(doc):
        if state == "hold":
            doc["hold"] = {"reason": "human pause"}
        elif state == "complete":
            doc["phase"] = "complete"
        else:
            doc["release_record"] = "retained-release"

    doc = obj.update(update)
    assert native.rearm_on_assignment_change(obj, doc, manifest) == doc
    assert doc["targets"] == {campaign.ROUND_UNIT: "done"} and doc["generation"] == 1


@pytest.mark.parametrize("missing_receipt", [False, True])
def test_native_b0_c0_requires_and_seals_exact_successful_named_receipt(
    capacity_host, missing_receipt
):
    import subprocess
    import sys

    import closer_runtime_binding as binding

    manifest, _path, _obj, _doc, _source, clock, *_ = capacity_host
    from test_closer_native_named_round import fresh_native_cfg

    cfg, context = fresh_native_cfg(capacity_host)
    boundary = native.contract(manifest)[3] / context["nonce"] / "completed-round-absent.json"
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "unused")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks._dependencies_match = lambda: True
    hooks.prerun = lambda _: (0, "original-token")
    hooks.owns = lambda token: token == "original-token"

    def postrun(_token, _deadline):
        hooks.postrun_result = {"code": 0, "dependencies_valid": True}
        return 0

    hooks.postrun = postrun

    def runner(argv, **_kwargs):
        from test_closer_native_named_round import fresh_scoped_round

        fresh = fresh_scoped_round(cfg, offset=0)
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({} if missing_receipt else fresh)
        )

    outcome, detail = binding.run_native_round(
        [
            sys.executable,
            "offline.py",
            "--assignment",
            cfg["assignment"],
            "--budget-seconds",
            "105",
        ],
        cfg,
        runner=runner,
        hooks=hooks,
        now=lambda: clock[0],
    )
    if missing_receipt:
        assert outcome == "unknown" and not boundary.exists()
    else:
        assert outcome == "ok", detail
        record = json.loads(detail)
        assert record["action_b_exit"] == record["action_c_exit"] == 0
        assert record["completed_round"]["boundary_global_stop_valid"] is True
        assert record["completed_round"]["accepted"] is False
        assert (
            native.completed_round_proof(manifest, cfg, record["completed_round"])
            == record["completed_round"]
        )


def test_oversized_round_receipt_is_bounded_before_hash_parse_or_validator(
    capacity_host, monkeypatch
):
    import closer_resources

    manifest, _path, _obj, doc, *_ = capacity_host
    context = publish(capacity_host)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    state = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    receipt = closer_resources.scoped_root(state) / "rounds" / ("a" * 32 + ".json")
    receipt.parent.mkdir(parents=True)
    with receipt.open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    original = Path.read_bytes

    def read_bytes(path):
        if path == receipt:
            pytest.fail("oversized receipt reached unbounded read_bytes")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(
        closer_resources, "validate_stop", lambda *_a: pytest.fail("oversize reached validator")
    )
    assert (
        native.completed_round_proof(
            manifest,
            cfg,
            {"round_id": "a" * 32, "receipt_path": str(receipt)},
            record_boundary=True,
        )
        is None
    )


def test_independent_validator_digest_mismatch_cannot_revalidate_success(
    capacity_host, monkeypatch
):
    import closer_resources

    manifest, *_ = capacity_host
    cfg, _context, proof, _aggregate, _result = completed_standing_round(capacity_host)
    validate = closer_resources.validate_stop

    def changed(*args, **kwargs):
        return {**validate(*args, **kwargs), "receipt_sha256": "f" * 64}

    monkeypatch.setattr(closer_resources, "validate_stop", changed)
    assert native.completed_round_proof(manifest, cfg, proof) is None


def test_native_invocation_without_low_profile_publishes_only_local_control_scope(
    capacity_host, monkeypatch
):
    import closer_dispatcher
    import execution_profiles

    manifest, _, _, _, _, clock, _ = capacity_host
    global_report = Path.home() / ".codex/handoff/capacity.json"
    global_report.parent.mkdir(parents=True, exist_ok=True)
    global_report.write_text('{"owner":"unrelated-model-user"}')
    original = global_report.read_bytes()
    monkeypatch.setattr(execution_profiles, "get_profile", lambda *_: None)
    with pytest.raises(mechanisms.MechanismError, match="active low"):
        native.capacity_contract(manifest)
    context = publish(capacity_host)
    report_path = Path(context["report"]["path"])
    report = native._read(report_path)
    assert scoped.validate_report(report, now=clock[0])
    assert report["control_plane_only"] is True
    assert report["profile_ids"] == []
    assert report["profiles"] == report["pools"] == {}
    assert report_path.name == "scoped-capacity.json"
    assert global_report.read_bytes() == original
    monkeypatch.setenv("CLOSER_CAPACITY_REPORT", str(report_path))
    assert closer_dispatcher._pipeline_capacity() == {"io": 4, "low": 0, "exception": 0}


def test_optional_provider_telemetry_cannot_gate_native_control_publication(
    capacity_host, monkeypatch
):
    manifest, _, _, _, _, clock, _ = capacity_host
    telemetry = Path.home() / ".codex/orchestrator/usage-poll"
    telemetry.parent.mkdir(parents=True, exist_ok=True)
    unrelated = Path.home() / "unrelated"
    unrelated.mkdir()
    telemetry.symlink_to(unrelated, target_is_directory=True)
    context = publish(capacity_host)
    report = native._read(Path(context["report"]["path"]))
    assert report["control_plane_only"] is True
    assert report["profile_ids"] == [] and report["input_sha256"] == {}
    assert scoped.validate_report(report, now=clock[0])
    assert native.validate_admission(manifest, context, for_effects=True) == context


def test_no_profile_native_admission_and_public_queue_composition(
    capacity_host, public_merge_world, monkeypatch, capsys
):
    import execution_profiles

    world = public_merge_world
    monkeypatch.setenv("HOME", str(capacity_host[4].parents[3]))
    capacity_host[5][0] = _REAL_TIME()
    monkeypatch.setattr(time, "time", _REAL_TIME)
    capacity_host[6](2)  # Fresh authentic invocation, aligned with real scheduler clock.
    monkeypatch.setattr(execution_profiles, "get_profile", lambda *_: None)
    context = publish(capacity_host)
    monkeypatch.setenv("CLOSER_CAPACITY_REPORT", context["report"]["path"])
    monkeypatch.setattr(closer_dispatcher, "_fresh_capacity_report", _REAL_FRESH_CAPACITY)
    monkeypatch.setattr(closer_dispatcher, "_pipeline_capacity", _REAL_PIPELINE_CAPACITY)
    assert native.validate_admission(capacity_host[0], context, for_effects=True) == context
    assert closer_dispatcher._pipeline_capacity() == {"io": 4, "low": 0, "exception": 0}
    assert closer_dispatcher.main(world.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(world.merges) == len(world.verifies) == 1, result
    assert world.events["provider"] == 0
