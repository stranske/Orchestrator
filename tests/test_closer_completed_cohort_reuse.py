"""Offline completed-cohort reuse through the existing controller and native wrapper."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_campaign_demand_integration import (
    _CI_SERVICE,
    _REAL_QUALIFY,
    TARGET,
)
from test_closer_campaign_demand_integration import host as host
from test_closer_campaign_demand_integration import publisher as publisher

import closer_campaign as campaign


@pytest.fixture
def completed_native_world(publisher, tmp_path, monkeypatch, request):
    import os
    import sys
    import time

    from test_closer_bounded_native_hooks import canonical_postrun, renderer
    from test_closer_native_lease import _minimal_prerun
    from test_closer_native_production_composition import _core

    import closer_native_observer as native
    import closer_runtime_binding as binding

    monkeypatch.setattr(campaign, "_now", time.time)
    ci_followup = True
    manifest, pin, root, controller, state_path, wrapper, _calls, _launch, discover = publisher
    seams = tmp_path / "offline-services"
    seams.mkdir()
    (seams / "sitecustomize.py").write_text(
        "import socket\ndef blocked(*a,**k): raise AssertionError('network forbidden')\nsocket.socket.connect=blocked\n"
    )
    gh = seams / "gh"
    gh.write_text("#!/bin/sh\necho 'offline GitHub evidence unavailable' >&2\nexit 1\n")
    gh.chmod(0o700)
    service = tmp_path / "ci-service.json"
    service.write_text(json.dumps({"complete": False}))
    if ci_followup:
        gh.write_text(
            "#!"
            + sys.executable
            + "\n"
            + _CI_SERVICE.replace("SERVICE_PATH", repr(str(service)))
            .replace(
                'elif endpoint=="repos/offline/repo/issues/7":',
                'elif endpoint=="repos/offline/repo/issues/8":\n value={"number":8,"state":"open","body":"## Tasks\\n- [ ] New work","title":"New demand","labels":[],"updated_at":"2026-01-02T00:00:00Z"}\nelif endpoint=="repos/offline/repo/issues/7":',
            )
            .replace('"pull_request":{},', "")
            .replace('"state":"open"', '"state":"closed"')
            .replace('"number":8,"state":"closed"', '"number":8,"state":"open"')
            .replace(
                'value={"data":{"repository":{"pullRequest":pr}}}',
                'value={"data":{"repository":{"issue":{"closedByPullRequestsReferences":{"nodes":[],"pageInfo":{"hasNextPage":False}}}}}}',
            )
        )
    for name in ("codex", "claude", "gemini", "cursor", "vibe", "muse"):
        exe = seams / name
        exe.write_text(
            "#!/bin/sh\necho forbidden-provider >> '"
            + str(tmp_path / "provider-calls")
            + "'\nexit 99\n"
        )
        exe.chmod(0o700)
    handoff = seams / "handoff"
    handoff.write_text("#!/bin/sh\nexit 0\n")
    handoff.chmod(0o700)
    for key, value in {
        "PATH": str(seams) + ":" + os.environ["PATH"],
        "PYTHONPATH": str(seams),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HANDOFF_BIN": str(handoff),
        "FAKE_HANDOFF": str(handoff),
        "FAKE_RELAY": str(seams / "forbidden-relay"),
        "EVENTS": str(tmp_path / "hook-events"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    if getattr(request, "param", None) == "ci-held":
        gh.write_text(
            "#!" + sys.executable + "\n" + _CI_SERVICE.replace("SERVICE_PATH", repr(str(service)))
        )
    manifest["portable"] = _core(tmp_path)
    manifest["package_digest"] = campaign.sha256_json(
        [
            manifest["candidate_sha"],
            sorted(manifest["modules"].items()),
            manifest["runtime_digest"],
            manifest["state_digest"],
            manifest["portable"],
        ]
    )
    manifest["distribution"]["core"]["pinned"] = manifest["portable"]
    manifest["mechanism_contract"]["capacity_publication"] = {
        "schema": "native-scoped-capacity/v1",
        "owner": "native-capacity-owner",
        "profile_ids": ["claude-sonnet-5-5-low"],
        "bounded_trial_agents": ["claude"],
        "admission_seconds": 600,
        "continue_on_assignment_change": True,
    }
    if ci_followup:
        manifest["mechanism_contract"]["capacity_publication"] = {
            "schema": "native-scoped-capacity/v2",
            "owner": "native-capacity-owner",
            "profile_ids": ["claude-sonnet-5-5-low"],
            "bounded_trial_agents": ["claude"],
            "execution_policy": "finite-frontier/v2",
            "request_timeout_seconds": 30,
            "freshness_seconds": 900,
            "continue_on_assignment_change": True,
        }
    original = wrapper.read_text()
    original = original.split(native.MARKER)[0] + original.split(native.END_MARKER)[1]
    wrapper.write_text(
        native.render_cursor_wrapper(
            original,
            manifest_path=pin,
            campaign_root=root,
            interpreter=Path(sys.executable).resolve(),
            launcher=Path(manifest["launcher"]["path"]),
        )
    )
    manifest["mechanism_contract"]["wrapper"]["sha256"] = native._hash(wrapper)
    mirror = Path.home() / ".codex/orchestrator-mirror"
    mirror.mkdir(parents=True)
    dispatcher = mirror / "closer_dispatcher.py"
    dispatcher.write_bytes((Path(manifest["module_dir"]) / "closer_dispatcher.py").read_bytes())
    pre, post = tmp_path / "native-pre", tmp_path / "native-post"
    pre.write_bytes(
        renderer.patch_prerun(
            (
                _minimal_prerun()
                + 'if false; then\n      "$HANDOFF_BIN" read-resume "$lane"\n      exit 0\nfi\n"$HANDOFF_BIN" pickup >/dev/null\n'
            ).encode(),
            bounded_round=True,
        )
    )
    post.write_bytes(renderer.patch_postrun(canonical_postrun().encode(), bounded_round=True))
    pre.chmod(0o700)
    post.chmod(0o700)
    lease = Path(manifest["module_dir"]) / "closer_native_lease.py"
    cfg = manifest["mechanism_contract"]["controller_demand"]["worker_config"]
    cfg.update(
        receipt_reserve_seconds=180,
        dispatch_workers=1,
        handoff_agent="codex",
        handoff_dir=os.environ["HANDOFF_DIR"],
    )
    discover(stamp=time.time())
    if ci_followup:
        import closer_dispatcher
        import closer_frontier_policy as frontier_policy

        backend = closer_dispatcher.Closer(
            closer_dispatcher.GitHub(deadline=time.time() + 30),
            state_path=tmp_path / "discovery-observation.json",
        )
        observed = backend.observe(TARGET)
        assert observed["action"] == (
            "wait" if getattr(request, "param", None) == "ci-held" else "complete"
        ), observed
        cfg.update(
            finite_frontier=frontier_policy.make_frontier(
                [TARGET],
                owner="existing-closer-owner",
                frontier_id="original-demand-cohort",
                bindings={
                    TARGET: {
                        "revision": observed["revision"],
                        "action_id": frontier_policy.action_identity(
                            TARGET,
                            observed["revision"],
                            observed["action"],
                            observed["operation_binding"],
                        ),
                    }
                },
            ),
            request_timeout_seconds=30,
            execution_policy=frontier_policy.POLICY,
        )
    cfg["native_hooks"] = {
        phase: {"path": str(path), "sha256": binding._hash(path)}
        for phase, path in (("prerun", pre), ("postrun", post))
    }
    cfg["native_hooks"].update(
        lease_module={"path": str(lease), "sha256": binding._hash(lease)},
        bounded_no_new_relay=True,
        postrun_dependencies={
            name: {"path": str(path), "sha256": binding._hash(path)}
            for name, path in {
                "interpreter": Path(sys.executable).resolve(),
                "dispatcher": dispatcher,
                "assignment": controller / "closer-assignment.json",
            }.items()
        },
    )
    # The dependency source must exist at qualification; discovery refreshes it
    # later, and native admission binds only the immutable admitted snapshot.
    discover(stamp=time.time())
    campaign.write_manifest(pin, manifest)
    monkeypatch.setenv("PYTHONPATH", str(seams) + ":" + manifest["module_dir"])
    monkeypatch.setattr(campaign, "qualify", _REAL_QUALIFY)
    qualified = native.supervise_native(manifest, pin, budget_seconds=45)
    assert qualified["passed"] and campaign.qualify(manifest, root) == []
    return SimpleNamespace(
        manifest=manifest,
        pin=pin,
        root=root,
        controller=controller,
        state_path=state_path,
        wrapper=wrapper,
        discover=discover,
        service=service,
    )


def _completed_worker(world):
    obj = campaign.Campaign(world.state_path)
    before = campaign.controller_demand_before_discovery()
    world.discover(stamp=time.time())
    assert campaign.controller_demand_after_discovery(before)["status"] == "admitted"
    until = time.monotonic() + 30
    while time.monotonic() < until:
        doc = obj.read()
        if doc.get("workers"):
            path = campaign.result_path(obj.path, doc["workers"][-1]["worker_id"])
            if path.exists():
                result = json.loads(path.read_bytes())
                assert result["outcome"] == "ok", result
                return obj, result
        time.sleep(0.05)
    pytest.fail("native completed worker did not finish")


def test_completed_demand_can_admit_next_cohort(completed_native_world, monkeypatch):
    world = completed_native_world
    obj, result = _completed_worker(world)
    import shutil

    retained = Path("/tmp/completed-cohort-actual-evidence")
    retained.mkdir(exist_ok=True)
    shutil.copy2(
        campaign.result_path(obj.path, result["worker_id"]), retained / "worker-result.json"
    )
    completed = json.loads(result["detail"])["completed_round"]
    shutil.copy2(completed["receipt_path"], retained / "round-receipt.json")
    assert json.loads(result["detail"]).get("cohort_completion"), json.loads(result["detail"]).get(
        "cohort_completion_unavailable"
    )
    doc = campaign.reconcile(obj)
    assert doc["targets"] == {campaign.ROUND_UNIT: "done"}
    first = doc["demand_admission"]
    previous = _discover_next(world)
    from concurrent.futures import ThreadPoolExecutor

    calls = []

    def launch(*_):
        calls.append(1)
        return 12345

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: campaign.controller_demand_after_discovery(previous, launcher=launch),
                range(2),
            )
        )
    assert sum(answer["status"] == "admitted" for answer in outcomes) == 1, outcomes
    assert calls == [1]
    after = obj.read()
    assert after["generation"] == doc["generation"] + 1
    assert after["owner"] == doc["owner"]
    assert after["demand_admission"]["request_id"] != first["request_id"]
    assert after["workers"] == doc["workers"]
    assert after["worker_config"]["finite_frontier"]["targets"] == ["offline/repo#8"]
    assert after["worker_config"]["finite_frontier"] != doc["worker_config"]["finite_frontier"]
    assert after.get("cohort_retirement")
    archive = Path(after["cohort_retirement"]["path"])
    archived_bytes = archive.read_bytes()
    archived = json.loads(archived_bytes)["record"]
    assert archived["previous"] == doc
    assert (
        archived["evidence"]["workers"][0]["result_bytes"]
        == campaign.result_path(obj.path, result["worker_id"]).read_text()
    )
    assert archived["authority"] is False
    assert archived["evidence"]["journals"]
    import hashlib

    for journal in archived["evidence"]["journals"]:
        assert hashlib.sha256(journal["bytes"].encode()).hexdigest() == journal["sha256"]
        assert journal["bytes"].encode() == Path(journal["path"]).read_bytes()
    assert campaign.current_cohort_workers(obj.path, after, world.manifest) == []
    assert campaign._retired_prefix(obj.path, after, world.manifest)["workers"] == len(
        doc["workers"]
    )
    # An unreferenced archive cannot establish retirement; the exact referenced
    # original bytes and retained registry prefix must remain authenticated.
    import copy

    altered = copy.deepcopy(after)
    altered["workers"][0]["outcome"] = "unknown"
    with pytest.raises(campaign.CampaignError, match="prefix"):
        campaign.current_cohort_workers(obj.path, altered, world.manifest)
    archive.write_bytes(archived_bytes + b" ")
    try:
        with pytest.raises(campaign.CampaignError):
            campaign.current_cohort_workers(obj.path, after, world.manifest)
    finally:
        archive.write_bytes(archived_bytes)
    for field, value in (("owner", "foreign-owner"), ("release_pin", "f" * 64)):
        foreign = copy.deepcopy(after)
        foreign[field] = value
        with pytest.raises(campaign.CampaignError):
            campaign.current_cohort_workers(obj.path, foreign, world.manifest)
    foreign = copy.deepcopy(after)
    foreign["demand_admission"]["request_id"] = "f" * 64
    with pytest.raises(campaign.CampaignError):
        campaign.current_cohort_workers(obj.path, foreign, world.manifest)
    # Reader and writer limits apply to authenticated archives and storage
    # orphans alike; exhaustion cannot prune history or mutate admission.
    with monkeypatch.context() as bounded:
        bounded.setattr(campaign, "COHORT_HISTORY_BYTES", len(archived_bytes) - 1)
        with pytest.raises(campaign.CampaignError, match="budget"):
            campaign.current_cohort_workers(obj.path, after, world.manifest)
    for index in range(campaign.COHORT_HISTORY_LIMIT - 1):
        (archive.parent / ("orphan-" + str(index))).write_bytes(b"orphan")
    other_record = copy.deepcopy(archived)
    other_record["next_request_id"] = "f" * 64
    with pytest.raises(campaign.CampaignError, match="storage capacity"):
        campaign._store_retirement(obj.path, world.manifest, other_record)
    assert obj.read() == after
    assert archive.read_bytes() == archived_bytes
    # The actual wrapper must enforce the NEW origin's deadline even though
    # authenticated retired workers are retained in the registry.
    import subprocess

    # This fixture's seams are a sibling of its campaign directory.
    seams = world.service.parent / "offline-services"
    offset = after["demand_admission"]["deadline"] - time.time() + 2
    original_seam = (seams / "sitecustomize.py").read_bytes()
    with (seams / "sitecustomize.py").open("a") as stream:
        stream.write(
            "\nimport time\n_real=time.time\ntime.time=lambda:_real()+" + repr(offset) + "\n"
        )
    expired = subprocess.run(
        ["/bin/bash", str(world.wrapper), "closer"], capture_output=True, text=True, timeout=30
    )
    import closer_native_observer as native

    observation_root = native.contract(world.manifest)[3]
    active = json.loads((observation_root / "native-active.json").read_bytes())
    terminal = json.loads((observation_root / active["nonce"] / "terminal.json").read_bytes())[
        "record"
    ]
    assert expired.returncode == 0
    assert terminal["tick_result"][0]["kind"] == "held"
    assert "not due or expired before start" in terminal["tick_result"][0]["reason"]
    assert obj.read()["workers"] == doc["workers"]
    assert obj.read()["generation"] == after["generation"]
    (seams / "sitecustomize.py").write_bytes(original_seam)
    resumed = subprocess.run(
        ["/bin/bash", str(world.wrapper), "closer"], capture_output=True, text=True, timeout=30
    )
    assert resumed.returncode == 0
    until = time.monotonic() + 30
    while time.monotonic() < until:
        current = obj.read()
        if len(current["workers"]) == len(doc["workers"]) + 1:
            path = campaign.result_path(obj.path, current["workers"][-1]["worker_id"])
            if path.exists():
                second = json.loads(path.read_bytes())
                break
        time.sleep(0.05)
    else:
        pytest.fail("fresh cohort did not reach its actual native worker result")
    assert (
        second["native_admission"]["finite_frontier"] == after["worker_config"]["finite_frontier"]
    )
    assert second["native_admission"]["demand_assignment"] == after["demand_assignment"]
    assert current["workers"][: len(doc["workers"])] == doc["workers"]
    assert archive.read_bytes() == archived_bytes
    assert not (world.state_path.parent.parent / "provider-calls").exists()


def test_completion_retirement_refuses_owned_or_unproved_history(
    completed_native_world, monkeypatch
):
    import copy
    import os

    world = completed_native_world
    obj, result = _completed_worker(world)
    doc = campaign.reconcile(obj)
    expected = campaign._retirement_evidence(obj.path, doc, world.manifest)
    assert expected["row_sha256"]
    variants = []
    for field, value in (
        ("phase", "hold"),
        ("hold", "explicit-stop"),
        ("release_record", {"released": True}),
        ("release_attempt", {"attempted": True}),
        ("targets", {campaign.ROUND_UNIT: "pending"}),
        ("hold_followups", {"h": {"status": "waiting"}}),
        ("hold_escalations", {"e": {"status": "pending"}}),
    ):
        changed = copy.deepcopy(doc)
        changed[field] = value
        variants.append(changed)
    for field, value in (
        ("state", "inflight"),
        ("outcome", "unknown"),
        ("outcome", "denied"),
        ("pid", os.getpid()),
    ):
        changed = copy.deepcopy(doc)
        changed["workers"][-1][field] = value
        variants.append(changed)
    import closer_mechanisms as mechanisms

    for changed in variants:
        with pytest.raises((campaign.CampaignError, mechanisms.MechanismError)):
            campaign._retirement_evidence(obj.path, changed, world.manifest)
    # Even an acknowledgement cannot discharge the original held budget.
    changed = copy.deepcopy(doc)
    changed["hold_followups"] = {"h": {"status": "escalated"}}
    changed["hold_escalations"] = {"e": {"status": "acknowledged"}}
    with pytest.raises(campaign.CampaignError, match="held budgets"):
        campaign._retirement_evidence(obj.path, changed, world.manifest)
    result_file = campaign.result_path(obj.path, result["worker_id"])
    original = result_file.read_bytes()
    changed_result = json.loads(original)
    detail = json.loads(changed_result["detail"])
    detail["native_execution"]["phases"]["C"]["cleanup"]["released"] = False
    changed_result["detail"] = json.dumps(detail)
    result_file.write_text(json.dumps(changed_result))
    try:
        with pytest.raises(campaign.CampaignError, match="attestation differs"):
            campaign._retirement_evidence(obj.path, doc, world.manifest)
    finally:
        result_file.write_bytes(original)
    assert campaign._retirement_evidence(obj.path, doc, world.manifest) == expected
    forged = copy.deepcopy(doc)
    forged["hold_followups"] = {"fake": {"hold_id": "fake", "status": "consumed"}}
    with pytest.raises(campaign.CampaignError, match="authenticated consumed"):
        campaign._retirement_evidence(obj.path, forged, world.manifest)


def _discover_next(world):
    previous = campaign.controller_demand_before_discovery()
    world.discover(stamp=time.time())
    for name, field in (("closer-assignment.json", "targets"), ("demand-plan.json", "routes")):
        path = world.controller / name
        data = json.loads(path.read_bytes())
        rows = data[field] if field == "targets" else data[field]["closer"]
        rows[0]["target"] = "offline/repo#8"
        campaign._atomic_write(path, data)
    return previous


@pytest.mark.parametrize("checkpoint", ["archive", "admitted"])
def test_retirement_crash_recovery_preserves_exact_origin(
    completed_native_world, monkeypatch, checkpoint
):
    world = completed_native_world
    obj, _result = _completed_worker(world)
    old = campaign.reconcile(obj)
    previous = _discover_next(world)
    original_write = campaign.Campaign._write_unlocked
    original_update = campaign.Campaign.update
    triggered = []

    def fail_write(self, doc):
        if doc.get("cohort_retirement") and not triggered:
            triggered.append(True)
            raise OSError("crash before canonical retirement commit")
        return original_write(self, doc)

    def fail_update(self, mutate, **kwargs):
        if mutate.__name__ == "record_wake" and not triggered:
            triggered.append(True)
            raise OSError("crash after admission before submission intent")
        return original_update(self, mutate, **kwargs)

    if checkpoint == "archive":
        monkeypatch.setattr(campaign.Campaign, "_write_unlocked", fail_write)
    else:
        monkeypatch.setattr(campaign.Campaign, "update", fail_update)
    calls = []
    with pytest.raises(OSError, match="crash"):
        campaign.controller_demand_after_discovery(previous, launcher=lambda *_: calls.append(1))
    assert not calls
    frozen = obj.read()
    if checkpoint == "archive":
        assert frozen == old
        assert list(obj.path.with_name(obj.path.name + ".cohorts").iterdir())
    else:
        assert frozen["generation"] == old["generation"] + 1
        assert frozen["demand_prepared"]["status"] == "prepared"
        assert not frozen.get("demand_wake")
    monkeypatch.setattr(campaign.Campaign, "_write_unlocked", original_write)
    monkeypatch.setattr(campaign.Campaign, "update", original_update)
    # Ordinary later discovery of the same material scope recovers the exact
    # committed known-not-submitted origin; it cannot extend its deadline.
    if checkpoint == "admitted":
        previous = _discover_next(world)
    outcome = campaign.controller_demand_after_discovery(
        previous, launcher=lambda *_: calls.append(1) or 12345
    )
    assert outcome["status"] == "admitted", outcome
    after = obj.read()
    assert calls == [1]
    assert after["generation"] == old["generation"] + 1
    assert after["workers"] == old["workers"]
    if checkpoint == "admitted":
        assert after["demand_admission"] == frozen["demand_admission"]
        assert after["demand_assignment"] == frozen["demand_assignment"]
        assert after["cohort_retirement"] == frozen["cohort_retirement"]
    # A duplicate/restart after durable submit never crosses the process boundary.
    duplicate = campaign.controller_demand_after_discovery(
        previous, launcher=lambda *_: calls.append(2)
    )
    assert duplicate["status"] != "admitted"
    assert calls == [1]


@pytest.mark.parametrize("completed_native_world", ["ci-held"], indirect=True)
def test_genuine_ci_stop_and_cleanup_cannot_retire(completed_native_world):
    world = completed_native_world
    obj, result = _completed_worker(world)
    detail = json.loads(result["detail"])
    # Actual native B0/C0 STOP succeeds for nonactionable CI waiting. The
    # producer records truthful terminality, never completion authority.
    assert detail.get("cohort_terminal"), detail
    assert not detail.get("cohort_completion")
    record = json.loads(Path(detail["cohort_terminal"]["path"]).read_bytes())["record"]
    assert record["completed"] is False
    assert record["native_execution"]["phases"]["C"]["cleanup"]["released"] is True
    doc = campaign.reconcile(obj)
    assert doc["targets"] == {campaign.ROUND_UNIT: "done"}
    with pytest.raises(campaign.CampaignError, match="full completion"):
        campaign._retirement_evidence(obj.path, doc, world.manifest)
    before = obj.path.read_bytes()
    outcome = campaign.controller_demand_after_discovery(
        _discover_next(world), launcher=lambda *_: pytest.fail("CI STOP cannot launch next cohort")
    )
    assert outcome["status"] == "held", outcome
    assert obj.path.read_bytes() == before
