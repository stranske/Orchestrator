"""Offline fresh-discovery admission through the actual campaign transaction."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_closer_native_observer import host as host

import closer_campaign as campaign

_REAL_QUALIFY = campaign.qualify

NOW = 1_792_000_000.0
TARGET = "offline/repo#7"


def demand_inputs(**overrides):
    assignment = {
        "generation": 1,
        "generated_at": dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat(),
        "errors": [],
        "targets": [{"target": TARGET, "eligible": True, "due": NOW - 1}],
    }
    raw = json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode()
    qualified = {
        "package_digest": "a" * 64,
        "manifest_sha256": "b" * 64,
        "qualification_digest": "c" * 64,
    }
    request = {
        "schema": "closer-demand-request/v1",
        "campaign_id": "standing-closer",
        "owner": "existing-closer-owner",
        "campaign_generation": 1,
        "expected_revision": 1,
        "assignment_generation": 1,
        "assignment_sha256": hashlib.sha256(raw).hexdigest(),
        **qualified,
        "not_before": NOW - 1,
        "deadline": NOW + 120,
        "targets": [TARGET],
    }
    values = {
        "demand_assignment_bytes": raw,
        "demand_request": request,
        "qualified_package": qualified,
        "demand_now": NOW,
    }
    values.update(overrides)
    return values


def admit(obj, values):
    return obj.begin(
        "standing-closer",
        [TARGET],
        owner="existing-closer-owner",
        release_pin="a" * 64,
        worker_config={"granularity": "round"},
        **values,
    )


def test_fresh_due_discovery_commits_inside_existing_campaign_transaction(tmp_path):
    obj = campaign.Campaign(tmp_path / "standing.campaign.json")
    doc = admit(obj, demand_inputs())
    assert obj.read() == doc
    assert doc["demand_admission"]["schema"] == "closer-demand-admission/v1"
    assert doc["revision"] == 2 and doc["generation"] == 1
    assert doc["assignment_targets"] == [TARGET]
    assert doc["targets"] == {campaign.ROUND_UNIT: "pending"} and not doc["workers"]


@pytest.mark.parametrize(
    "damage",
    [
        "no_demand",
        "future",
        "future_row",
        "expired",
        "generation",
        "revision",
        "foreign_package",
        "incomplete",
        "stale_discovery",
    ],
)
def test_unadmitted_discovery_cannot_create_campaign(tmp_path, damage):
    obj = campaign.Campaign(tmp_path / "standing.campaign.json")
    values = demand_inputs()
    req = values["demand_request"]
    if damage == "future":
        req["not_before"] = NOW + 1
    elif damage == "expired":
        req["deadline"] = NOW - 1
    elif damage == "generation":
        req["campaign_generation"] = 2
    elif damage == "revision":
        req["expected_revision"] = 2
    elif damage == "foreign_package":
        values["qualified_package"]["package_digest"] = "d" * 64
    else:
        assignment = json.loads(values["demand_assignment_bytes"])
        if damage == "no_demand":
            assignment["targets"] = []
            req["targets"] = []
        elif damage == "future_row":
            assignment["targets"][0]["due"] = NOW + 1
        elif damage == "incomplete":
            assignment["errors"] = ["discovery incomplete"]
        else:
            assignment["generated_at"] = "2000-01-01T00:00:00+00:00"
        values["demand_assignment_bytes"] = json.dumps(assignment, sort_keys=True).encode()
        req["assignment_sha256"] = hashlib.sha256(values["demand_assignment_bytes"]).hexdigest()
    with pytest.raises((ValueError, RuntimeError)):
        admit(obj, values)
    assert not obj.path.exists()


def test_same_request_replay_preserves_exact_record_bytes_and_revision(tmp_path):
    obj = campaign.Campaign(tmp_path / "standing.campaign.json")
    values = demand_inputs()
    first = admit(obj, values)
    raw = obj.path.read_bytes()
    replay = admit(obj, dict(values, demand_now=NOW + 1))
    assert replay == first and obj.path.read_bytes() == raw


def test_two_concurrent_initiators_share_the_existing_state_transaction(tmp_path):
    path = tmp_path / "standing.campaign.json"
    values = demand_inputs()
    with ThreadPoolExecutor(max_workers=2) as pool:
        docs = list(
            pool.map(lambda _: admit(campaign.Campaign(path), copy.deepcopy(values)), range(2))
        )
    assert docs[0] == docs[1] == campaign.Campaign(path).read()
    assert docs[0]["revision"] == 2


def test_stale_generation_cannot_replace_an_accepted_campaign(tmp_path):
    obj = campaign.Campaign(tmp_path / "standing.campaign.json")
    values = demand_inputs()
    admit(obj, values)
    before = obj.path.read_bytes()
    values = copy.deepcopy(values)
    values["demand_request"]["campaign_generation"] = 0
    with pytest.raises((ValueError, RuntimeError)):
        admit(obj, values)
    assert obj.path.read_bytes() == before


@pytest.fixture
def publisher(host, tmp_path, monkeypatch):
    """Synthetic qualification transport only; no observed native capability issued."""
    import closer_native_observer as native

    manifest, _old_path, wrapper = host
    monkeypatch.setattr(campaign, "_now", lambda: NOW)
    root = tmp_path / "campaigns"
    controller = tmp_path / "publisher"
    controller.mkdir()
    source = controller / "demand_router.py"
    source.write_text("def main(argv=None):\n    return 0\n")
    state_path = root / "standing.campaign.json"
    cfg = {
        "mechanism": "native-handoff/v1",
        "policy": manifest["policy"],
        "apply": True,
        "coordinator_mode": "resource_scoped",
        "granularity": "round",
        "dispatch_state": str(root / "dispatch.json"),
        "coordinator_contract_pin": __import__("closer_pipeline").scoped_capability(),
        "assignment": str(controller / "closer-assignment.json"),
    }
    contract = {
        "schema": "closer-controller-demand/v1",
        "controller_root": str(controller),
        "campaign_state": str(state_path),
        "campaign_id": "standing-closer",
        "owner": "existing-closer-owner",
        "publisher_sha256": campaign.sha256_file(source),
        "worker_config": cfg,
    }
    manifest["mechanism_contract"]["controller_demand"] = contract
    pin = root / "pin.manifest.json"
    manifest["command"][manifest["command"].index("--manifest") + 1] = str(pin)
    manifest["command_digest"] = campaign.sha256_json(manifest["command"])
    campaign.write_manifest(pin, manifest)
    (native.contract(manifest)[3] / "native-qualification.json").write_text(
        "synthetic qualifier seam; not lifecycle evidence"
    )
    monkeypatch.setattr(campaign, "qualify", lambda *_: [])
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_DIR", str(root))
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "1")
    calls = []

    def launch(argv, log):
        # Both publisher and campaign locks must be released at process boundary.
        import subprocess
        import sys

        subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys\nfor path in sys.argv[1:]:\n with open(path,'a+b') as lock: fcntl.flock(lock,fcntl.LOCK_EX)",
                str(controller / ".demand.lock"),
                str(state_path.with_name(state_path.name + ".lock")),
            ],
            check=True,
            timeout=3,
            capture_output=True,
        )
        calls.append(argv)
        return 12345

    def discover(*, errors=None, future=False, empty=False, stamp=NOW):
        generated = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()
        rows = [] if empty else [{"target": TARGET, "due": stamp + 1 if future else stamp - 1}]
        campaign._atomic_write(
            controller / "demand-plan.json",
            {
                "generated_at": generated,
                "errors": errors or [],
                "ownership_complete": True,
                "routes": {"closer": rows},
            },
        )
        campaign._atomic_write(controller / "demand-state.json", {"last_complete_at": generated})
        campaign._atomic_write(
            controller / "closer-assignment.json",
            {"generated_at": generated, "errors": errors or [], "targets": rows},
        )

    return manifest, pin, root, controller, state_path, wrapper, calls, launch, discover


def test_post_discovery_bridge_commits_frozen_generation_then_invokes_normal_wrapper(publisher):
    import closer_native_observer as native

    manifest, pin, root, controller, state_path, wrapper, calls, launch, discover = publisher
    before = campaign.controller_demand_before_discovery()
    discover()
    outcome = campaign.controller_demand_after_discovery(before, launcher=launch, now=NOW)
    doc = campaign.Campaign(state_path).read()
    assert outcome["status"] == "admitted" and calls == [["/bin/bash", str(wrapper), "closer"]]
    assert doc["demand_admission"]["assignment_generation"] == 1 and doc["revision"] == 4
    ref = doc["demand_assignment"]
    assert json.loads(Path(ref["path"]).read_bytes())["generation"] == 1
    assert ref["sha256"] == campaign.sha256_file(ref["path"])
    assert json.loads((controller / "closer-assignment.json").read_bytes())["generation"] == 1
    assert str(native.contract(manifest)[3]) not in native._CURRENT_ENTRY
    import closer_mechanisms as mechanisms

    with pytest.raises(mechanisms.MechanismError, match="observation file unsafe"):
        campaign.tick(root, pin, launcher=lambda *_: pytest.fail("direct tick must not launch"))


@pytest.mark.parametrize(
    "bad", ["unchanged", "errors", "future", "no_demand", "mismatched_completion", "stale"]
)
def test_post_discovery_refusals_create_and_launch_nothing(publisher, bad):
    manifest, pin, root, controller, state_path, wrapper, calls, launch, discover = publisher
    if bad == "unchanged":
        discover()
    before = campaign.controller_demand_before_discovery()
    if bad != "unchanged":
        discover(
            errors=["incomplete"] if bad == "errors" else [],
            future=bad == "future",
            empty=bad == "no_demand",
            stamp=NOW - 601 if bad == "stale" else NOW,
        )
    if bad == "mismatched_completion":
        campaign._atomic_write(controller / "demand-state.json", {"last_complete_at": "different"})
    result = campaign.controller_demand_after_discovery(before, launcher=launch, now=NOW)
    assert result["status"] in {"refused", "inert"}
    assert not state_path.exists() and not calls


def test_two_post_discovery_initiators_share_one_canonical_admission_and_wake(publisher):
    _manifest, _pin, _root, _controller, state_path, _wrapper, calls, launch, discover = publisher
    contexts = [campaign.controller_demand_before_discovery() for _ in range(2)]
    discover()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda ctx: campaign.controller_demand_after_discovery(
                    ctx, launcher=launch, now=NOW
                ),
                contexts,
            )
        )
    assert sorted(row["status"] for row in results) == ["admitted", "replay"]
    assert len(calls) == 1 and campaign.Campaign(state_path).read()["revision"] == 4


def test_existing_terminal_unknown_owner_is_not_modified_or_revived(publisher):
    manifest, _pin, _root, _controller, path, _wrapper, calls, launch, discover = publisher
    obj = campaign.Campaign(path)
    obj.begin(
        "standing-closer",
        [TARGET],
        owner="existing-closer-owner",
        release_pin=manifest["package_digest"],
        worker_config=manifest["mechanism_contract"]["controller_demand"]["worker_config"],
    )
    obj.update(
        lambda doc: (
            doc.update(generation=2),
            doc["workers"].append(
                {"worker_id": "old-owned", "state": "terminal", "outcome": "unknown"}
            ),
        )
    )
    before_bytes = path.read_bytes()
    before = campaign.controller_demand_before_discovery()
    discover()
    result = campaign.controller_demand_after_discovery(before, launcher=launch, now=NOW)
    assert result["status"] == "refused" and "unresolved" in result["reason"]
    assert path.read_bytes() == before_bytes and not calls


def test_native_demand_origin_consumes_immutable_bytes_after_new_publication(publisher):
    import closer_native_observer as native

    manifest, _pin, _root, controller, state_path, _wrapper, _calls, launch, discover = publisher
    before = campaign.controller_demand_before_discovery()
    discover()
    campaign.controller_demand_after_discovery(before, launcher=launch, now=NOW)
    doc = campaign.Campaign(state_path).read()
    retained = Path(doc["demand_assignment"]["path"]).read_bytes()
    campaign._atomic_write(
        controller / "closer-assignment.json",
        {"generation": 999, "targets": [{"target": "foreign/repo#9"}]},
    )
    source, raw = native._demand_assignment_bytes(
        manifest, doc["demand_assignment"], native._campaign_identity(state_path, doc)
    )
    assert str(source) == doc["demand_assignment"]["path"] and raw == retained
    assert json.loads(raw)["generation"] == 1


@pytest.mark.parametrize("change", ["generation", "path", "owner", "campaign", "request", "bytes"])
def test_native_demand_origin_rejects_wrong_or_changed_snapshot(publisher, change):
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    manifest, _pin, _root, controller, state_path, _wrapper, _calls, launch, discover = publisher
    before = campaign.controller_demand_before_discovery()
    discover()
    campaign.controller_demand_after_discovery(before, launcher=launch, now=NOW)
    doc = campaign.Campaign(state_path).read()
    ref = copy.deepcopy(doc["demand_assignment"])
    identity = native._campaign_identity(state_path, doc)
    if change == "generation":
        ref["assignment_generation"] += 1
    elif change == "path":
        ref["path"] = str(controller / "closer-assignment.json")
    elif change == "owner":
        ref["owner"] = "other"
    elif change == "campaign":
        identity["state"] = str(state_path.with_name("foreign.campaign.json"))
    elif change == "request":
        ref["request_id"] = "invalid"
    else:
        Path(ref["path"]).write_bytes(b"changed")
    with pytest.raises((ValueError, mechanisms.MechanismError)):
        native._demand_assignment_bytes(manifest, ref, identity)


def test_wrapper_submission_unknown_is_owned_and_never_retried(publisher):
    _manifest, _pin, _root, controller, state_path, _wrapper, calls, _launch, discover = publisher
    contexts = [campaign.controller_demand_before_discovery() for _ in range(2)]
    discover()

    def unknown(*_):
        calls.append("submitted")
        raise TimeoutError("wrapper outcome unknown")

    with pytest.raises(TimeoutError):
        campaign.controller_demand_after_discovery(contexts[0], launcher=unknown, now=NOW)
    state = json.loads((controller / "demand-state.json").read_bytes())
    assert state["closer_campaign_demand"]["wake"]["status"] == "unknown"
    assert (
        campaign.controller_demand_after_discovery(contexts[1], launcher=unknown, now=NOW)["status"]
        == "replay"
    )
    before = campaign.controller_demand_before_discovery()
    discover(stamp=NOW + 1)
    assert (
        campaign.controller_demand_after_discovery(before, launcher=unknown, now=NOW + 1)["status"]
        == "held"
    )
    assert len(calls) == 1 and len(campaign.Campaign(state_path).read()["workers"]) == 0


def test_controller_entry_invokes_bridge_only_after_pinned_discovery_main(publisher, monkeypatch):
    _manifest, _pin, _root, controller, _state, _wrapper, _calls, launch, discover = publisher
    scope = {"discover": discover}
    source = controller / "demand_router.py"
    text = "def main(argv=None):\n    discover()\n    return 0\n"
    source.write_text(text)
    manifest = campaign.load_manifest(publisher[1])
    manifest["mechanism_contract"]["controller_demand"]["publisher_sha256"] = campaign.sha256_file(
        source
    )
    campaign.write_manifest(publisher[1], manifest)
    exec(compile(text, str(source), "exec", dont_inherit=True), scope)
    events = []
    monkeypatch.setattr(campaign, "tick_safe", lambda: events.append("owned-reconcile") or [])
    real_after = campaign.controller_demand_after_discovery

    def after(context):
        events.append("post-discovery")
        return real_after(context, launcher=launch, now=NOW)

    monkeypatch.setattr(campaign, "controller_demand_after_discovery", after)
    assert campaign.controller_entry([], scope["main"]) == 0
    assert events == ["owned-reconcile", "post-discovery"]
    assert campaign._LAST_CONTROLLER_DEMAND_REPORT["status"] == "admitted"


@pytest.mark.parametrize("ci_followup", [False, True])
def test_actual_normal_wrapper_and_controller_share_state_and_native_lease(
    publisher, tmp_path, monkeypatch, ci_followup
):
    """Real observed entry/worker/A-C; only external service executables are inert."""
    import os
    import subprocess
    import sys
    import time

    from test_closer_bounded_native_hooks import canonical_postrun, renderer
    from test_closer_native_lease import _minimal_prerun
    from test_closer_native_production_composition import _core, _fixture_worker_cleanup

    import closer_native_observer as native
    import closer_runtime_binding as binding

    monkeypatch.setattr(campaign, "_now", time.time)
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
            "#!" + sys.executable + "\n" + _CI_SERVICE.replace("SERVICE_PATH", repr(str(service)))
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
    dispatcher.write_bytes(Path(campaign.__file__).with_name("closer_dispatcher.py").read_bytes())
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
        assert observed["action"] == "wait" and observed.get("pending_check_run_ids") == [
            123
        ], json.dumps(observed)
        cfg.update(
            finite_frontier=frontier_policy.make_frontier(
                [TARGET],
                owner="existing-closer-owner",
                frontier_id="original-demand-cohort",
                bindings={
                    TARGET: {
                        "revision": observed["revision"],
                        "action_id": frontier_policy.action_identity(
                            TARGET, observed["revision"], "wait", observed["operation_binding"]
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
    before = campaign.controller_demand_before_discovery()
    discover(stamp=time.time())
    obj = campaign.Campaign(state_path)
    try:

        def publish_new_then_start(argv, log):
            import fcntl

            with (controller / ".demand.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                campaign._atomic_write(
                    controller / "closer-assignment.json",
                    {"generation": 999, "targets": [{"target": "foreign/repo#9"}], "errors": []},
                )
            return campaign.default_launcher(argv, log)

        outcome = campaign.controller_demand_after_discovery(
            before, launcher=publish_new_then_start
        )
        assert outcome["status"] == "admitted"
        limit = time.monotonic() + 20
        result = None
        while time.monotonic() < limit:
            doc = obj.read()
            workers = doc.get("workers") or []
            if workers:
                result_path = campaign.result_path(state_path, workers[0]["worker_id"])
                if result_path.exists():
                    result = json.loads(result_path.read_bytes())
                    break
            time.sleep(0.05)
        assert result is not None, {
            "doc": obj.read(),
            "log": state_path.with_name(state_path.name + ".demand-wrapper.log").read_text(),
        }
        admission = result["native_admission"]
        assert admission["campaign"]["state"] == str(state_path)
        assert admission["demand_assignment"] == obj.read()["demand_assignment"]
        assert admission["assignment"]["source_path"] == obj.read()["demand_assignment"]["path"]
        execution = json.loads(result["detail"])["native_execution"]
        assert execution["phases"]["A"]["admitted"] is True
        assert execution["phases"]["C"]["cleanup"]["exact_token_matched"] is True
        assert not (Path(os.environ["HANDOFF_DIR"]) / ".lane-closer.lock").exists()
        if ci_followup:
            assert result["outcome"] in {"ok", "scoped_continuation"}, result
            first_holds = obj.read().get("hold_followups") or {}
            service.write_text(json.dumps({"complete": True}))
            resumed = subprocess.run(
                ["/bin/bash", str(wrapper), "closer"], capture_output=True, text=True, timeout=30
            )
            assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
            doc = obj.read()
            assert doc["generation"] == 2 and len(doc["demand_continuations"]) == 1, doc
            origin = doc["demand_assignment"]
            identity = native._campaign_identity(state_path, doc)
            assert (
                native._demand_assignment_bytes(
                    manifest,
                    origin,
                    identity,
                    continuations=doc["demand_continuations"],
                    finite_frontier=cfg["finite_frontier"],
                )[1]
                == Path(origin["path"]).read_bytes()
            )
            with pytest.raises(RuntimeError, match="continuation"):
                native._demand_assignment_bytes(
                    manifest,
                    origin,
                    identity,
                    continuations=doc["demand_continuations"] * 2,
                    finite_frontier=cfg["finite_frontier"],
                )
            for hold_id, held in first_holds.items():
                assert doc["hold_followups"][hold_id]["deadline"] == held["deadline"]
                assert doc["hold_followups"][hold_id]["attempt_budget"] == held["attempt_budget"]
            again = subprocess.run(
                ["/bin/bash", str(wrapper), "closer"], capture_output=True, text=True, timeout=30
            )
            assert again.returncode == 0
            assert (
                obj.read()["generation"] == 2
                and obj.read()["demand_continuations"] == doc["demand_continuations"]
            )
            assert not (tmp_path / "provider-calls").exists()
            return
        old = subprocess.run(
            ["/bin/bash", str(wrapper), "closer"], capture_output=True, text=True, timeout=15
        )
        assert old.returncode == 0, (old.stdout, old.stderr)
        assert len(obj.read()["workers"]) == 1
        assert not (tmp_path / "provider-calls").exists()
        workers = obj.read()["workers"]
        worker_cfg = native.result_admission_config(
            obj.read()["worker_config"], admission, state_path, obj.read(), workers[0]
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_retained_demand_runtime", Path(manifest["module_dir"]) / "closer_runtime_binding.py"
        )
        retained_binding = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = retained_binding
        spec.loader.exec_module(retained_binding)
        pinned_hooks = retained_binding.PinnedNativeHooks(
            worker_cfg, runner=lambda *_a, **_k: pytest.fail("bad snapshot cannot start a helper")
        )
        assert pinned_hooks.assignment_source == Path(admission["demand_assignment"]["path"])
        assert pinned_hooks._dependencies_match(admission=True)
        Path(admission["demand_assignment"]["path"]).write_bytes(b"changed retained demand")
        assert not pinned_hooks._dependencies_match(admission=True)
        assert pinned_hooks.prerun(time.time() + 5) == (65, None)
        with pytest.raises((ValueError, RuntimeError, OSError)):
            retained_binding.PinnedNativeHooks(
                worker_cfg,
                runner=lambda *_a, **_k: pytest.fail(
                    "changed snapshot cannot fall back to canonical bytes"
                ),
            )
    finally:
        _fixture_worker_cleanup(obj, manifest, os.environ["HANDOFF_DIR"], tmp_path / "cleanup.json")


_CI_SERVICE = r"""import json,sys
from pathlib import Path
a=sys.argv[1:]; complete=json.loads(Path(SERVICE_PATH).read_text())["complete"]
endpoint=next((x for x in a if x=="graphql" or x.startswith("repos/")), "")
if endpoint=="graphql":
 pr={"number":7,"title":"Offline CI","body":"## Tasks\n- [ ] Ship the report","state":"OPEN","isDraft":False,"headRefOid":"a"*40,"headRefName":"fixture","headRepository":{"nameWithOwner":"offline/repo"},"baseRefOid":"b"*40,"mergeCommit":None,"mergedAt":None,"updatedAt":"2026-01-01T00:00:00Z","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN",
 "labels":{"nodes":[],"pageInfo":{"hasNextPage":False}},"timelineItems":{"nodes":[],"pageInfo":{"hasPreviousPage":False}},"closingIssuesReferences":{"nodes":[],"pageInfo":{"hasNextPage":False}},"reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":False}},"comments":{"nodes":[],"pageInfo":{"hasPreviousPage":False}},
 "commits":{"nodes":[{"commit":{"oid":"a"*40,"statusCheckRollup":{"state":"SUCCESS" if complete else "PENDING","contexts":{"nodes":[{"databaseId":123,"status":"COMPLETED" if complete else "IN_PROGRESS","conclusion":"SUCCESS" if complete else None}],"pageInfo":{"hasNextPage":False}}}}}]}}
 value={"data":{"repository":{"pullRequest":pr}}}
elif endpoint=="repos/offline/repo/issues/7":
 value={"number":7,"pull_request":{},"state":"open","body":"## Tasks\n- [ ] Ship the report","title":"Offline CI","labels":[],"updated_at":"2026-01-01T00:00:00Z"}
elif endpoint.startswith("repos/offline/repo/check-runs/123"):
 value={"id":123,"head_sha":"a"*40,"status":"completed" if complete else "in_progress","conclusion":"success" if complete else None,"name":"required-test","html_url":"https://github.com/offline/repo/checks/123"}
elif "/compare/" in endpoint:value={"status":"ahead","ahead_by":1,"files":[],"commits":[]}
elif "/actions/runs" in endpoint:value={"total_count":0,"workflow_runs":[],"artifacts":[]}
elif endpoint.endswith(("/files","/comments","/reviews")):value=[]
else:
 print("offline service refuses "+endpoint,file=sys.stderr);sys.exit(1)
prefix="HTTP/2 200\nx-ratelimit-limit: 5000\nx-ratelimit-remaining: 4999\nx-ratelimit-reset: 9999999999\n\n" if "--include" in a or "-i" in a else ""
print(prefix+json.dumps([value] if "--slurp" in a else value))
"""


@pytest.mark.parametrize("crash", ["before_commit", "after_commit", None])
def test_demand_rearm_persists_sidecar_before_generation_commit(tmp_path, monkeypatch, crash):
    """Transaction ordering only; the sealer fixture grants no native authority."""
    from test_closer_hold_safety_contracts import (
        NOW as hold_now,
    )
    from test_closer_hold_safety_contracts import (
        only_hold,
        successful_no_start_world,
    )

    import closer_hold_followup as holds
    import closer_native_observer as native

    obj, _paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original = only_hold(obj)
    doc, _reports = holds.consume(obj, manifest, str(manifest_path), now=hold_now)
    obj.update(lambda d: d.update(demand_admission={"fixture": "ordering-only"}))
    doc = obj.read()
    receipt = tmp_path / "ordering-only-sidecar.json"
    calls = []

    def seal(path, value, next_generation, releases):
        assert obj.path == path and obj._read_unlocked()["generation"] == 1
        assert next_generation == 2 and value["generation"] == 1
        assert releases and value["hold_followups"][original["hold_id"]]["status"] == "released"
        receipt.write_text("ordering fixture, not signed native evidence")
        calls.append(next_generation)
        return {"path": str(receipt), "sha256": campaign.sha256_file(receipt)}

    monkeypatch.setattr(native, "seal_demand_hold_continuation", seal)
    write = obj._write_unlocked

    def crashing_write(value):
        if value["generation"] == 2:
            assert receipt.exists() and value["demand_continuations"][0][
                "sha256"
            ] == campaign.sha256_file(receipt)
            if crash == "before_commit":
                raise SystemExit("before campaign commit")
            write(value)
            if crash == "after_commit":
                raise SystemExit("after campaign commit")
        else:
            write(value)

    monkeypatch.setattr(obj, "_write_unlocked", crashing_write)
    if crash:
        with pytest.raises(SystemExit):
            holds.rearm(obj, doc, manifest, now=hold_now + 1)
    else:
        holds.rearm(obj, doc, manifest, now=hold_now + 1)
    restarted = campaign.Campaign(obj.path)
    durable = restarted.read()
    assert calls == [2] and receipt.exists()
    if crash == "before_commit":
        assert durable["generation"] == 1 and not durable.get("demand_continuations")
        assert only_hold(restarted)["status"] == "released"
    else:
        assert durable["generation"] == 2 and len(durable["demand_continuations"]) == 1
        retained = only_hold(restarted)
        assert retained["deadline"] == original["deadline"]
        assert retained["attempt_budget"] == original["attempt_budget"]
        assert retained["attempts"] == 1 and retained["status"] == "consumed"
        holds.rearm(restarted, durable, manifest, now=hold_now + 2)
        assert restarted.read()["generation"] == 2 and calls == [2]


@pytest.mark.parametrize(
    "damage", [None, "missing", "signature", "generation", "origin", "duplicate", "cohort"]
)
def test_signed_demand_continuation_reader_requires_exact_contiguous_scope(publisher, damage):
    """Signed-envelope reader fixture only; this does not attest observed entry."""
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    manifest, _pin, _root, _controller, state_path, _wrapper, *_ = publisher
    obj = campaign.Campaign(state_path)
    values = demand_inputs()
    # Use the reviewed publisher's exact canonical identity and snapshot location.
    values["qualified_package"]["package_digest"] = manifest["package_digest"]
    values["demand_request"]["package_digest"] = manifest["package_digest"]
    doc = obj.begin(
        "standing-closer",
        [TARGET],
        owner="existing-closer-owner",
        release_pin=manifest["package_digest"],
        worker_config={"granularity": "round"},
        **values,
    )
    origin = doc["demand_assignment"]
    raw = Path(origin["path"]).read_bytes()
    identity = native._campaign_identity(state_path, {**doc, "generation": 2})
    root = native.contract(manifest)[3]
    nonce = "d" * 32
    path = root / nonce / ("demand-continuation-" + "e" * 64 + ".json")
    path.parent.mkdir()
    record = {
        "schema": "native-demand-continuation/v1",
        "origin": origin,
        "cohort": native._demand_cohort(raw, None),
        "campaign": {k: identity[k] for k in ("state", "campaign_id", "owner")},
        "release_pin": manifest["package_digest"],
        "binding": native.binding(manifest),
        "previous_generation": 1,
        "next_generation": 2,
        "releases": [{"hold_id": "fixture-reader-only"}],
        "entry_nonce": nonce,
    }
    if damage == "generation":
        record["previous_generation"] = 2
    if damage == "origin":
        record["origin"] = {**origin, "request_id": "foreign"}
    if damage == "cohort":
        record["cohort"] = "foreign"
    signature = mechanisms._signature(record, mechanisms._key(root, create=True))
    if damage == "signature":
        signature = "0" * 64
    path.write_text(json.dumps({"record": record, "signature": signature}))
    refs = [{"path": str(path), "sha256": campaign.sha256_file(path)}]
    if damage == "missing":
        refs = []
    if damage == "duplicate":
        refs *= 2

    def read():
        return native._demand_assignment_bytes(manifest, origin, identity, continuations=refs)

    if damage:
        with pytest.raises((RuntimeError, ValueError), match="continuation"):
            read()
    else:
        assert read()[1] == raw
        # A fresh reader (no in-memory entry registration) accepts the same durable proof.
        native._CURRENT_ENTRY.clear()
        assert read()[1] == raw


@pytest.mark.parametrize("owned", ["known_worker", "durable_hold"])
def test_new_demand_does_not_replace_existing_owned_campaign(tmp_path, owned):
    obj = campaign.Campaign(tmp_path / "standing.campaign.json")
    doc = admit(obj, demand_inputs())
    if owned == "known_worker":
        obj.update(
            lambda d: d["workers"].append(
                {"worker_id": "old", "state": "terminal", "outcome": "scoped_continuation"}
            )
        )
    else:
        obj.update(lambda d: d.update(hold_followups={"existing": {"status": "released"}}))
    before = obj.path.read_bytes()
    values = demand_inputs()
    assignment = json.loads(values["demand_assignment_bytes"])
    assignment["generation"] = 2
    raw = json.dumps(assignment).encode()
    values["demand_assignment_bytes"] = raw
    values["demand_request"].update(
        assignment_generation=2,
        assignment_sha256=hashlib.sha256(raw).hexdigest(),
        expected_revision=obj.read()["revision"],
    )
    with pytest.raises(campaign.CampaignError, match="remains owned"):
        admit(obj, values)
    assert obj.path.read_bytes() == before
    assert obj.read()["demand_assignment"] == doc["demand_assignment"]


def test_continuation_sealer_cannot_issue_without_observed_native_entry(publisher):
    import closer_native_observer as native

    manifest, pin, _root, _controller, state_path, _wrapper, *_ = publisher
    obj = campaign.Campaign(state_path)
    doc = admit(obj, demand_inputs())
    doc["worker_config"]["runtime_manifest"] = str(pin)
    root = native.contract(manifest)[3]
    with pytest.raises(RuntimeError, match="observed native entry|observation file unsafe"):
        native.seal_demand_hold_continuation(state_path, doc, 2, [{"hold_id": "untrusted"}])
    assert not list(root.glob("*/demand-continuation-*.json"))


def test_controller_rejects_foreign_executable_code_with_pinned_filename(publisher, monkeypatch):
    _manifest, _pin, _root, controller, state, _wrapper, calls, launch, discover = publisher
    source = controller / "demand_router.py"
    retained = source.read_bytes()
    namespace = {"discover": discover}
    exec(
        compile("def main(argv=None):\n    discover()\n    return 0\n", str(source), "exec"),
        namespace,
    )
    monkeypatch.setattr(campaign, "tick_safe", lambda: [])
    actual = campaign.controller_demand_after_discovery
    monkeypatch.setattr(
        campaign,
        "controller_demand_after_discovery",
        lambda context: actual(context, launcher=launch, now=NOW),
    )
    assert campaign.controller_entry([], namespace["main"]) == 0
    assert source.read_bytes() == retained and not calls and not state.exists()
    assert campaign._LAST_CONTROLLER_DEMAND_REPORT["status"] == "held"


def test_deadline_crossing_during_seal_preserves_original_release(tmp_path, monkeypatch):
    from test_closer_hold_safety_contracts import NOW as hold_now
    from test_closer_hold_safety_contracts import only_hold, successful_no_start_world

    import closer_hold_followup as holds
    import closer_native_observer as native

    obj, _paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    holds.consume(obj, manifest, str(manifest_path), now=hold_now)
    original = only_hold(obj)
    obj.update(lambda d: d.update(demand_admission={"fixture": "ordering-only"}))
    before = obj.path.read_bytes()

    def seal(*args):
        monkeypatch.setattr(holds.time, "time", lambda: original["deadline"] + 1)
        return {"path": "/tmp/ordering-only", "sha256": "a" * 64}

    monkeypatch.setattr(native, "seal_demand_hold_continuation", seal)
    with pytest.raises(ValueError, match="expired during native seal"):
        holds.rearm(obj, obj.read(), manifest, now=hold_now + 1)
    assert obj.path.read_bytes() == before and only_hold(obj)["status"] == "released"
    assert obj.read()["generation"] == 1 and not obj.read().get("demand_continuations")


@pytest.mark.parametrize("crash", ["before_campaign_commit", "after_campaign_commit_before_wake"])
def test_initial_admission_restart_preserves_one_request_and_eventually_wakes(
    publisher, monkeypatch, crash
):
    _manifest, _pin, _root, controller, path, _wrapper, calls, launch, discover = publisher
    context = campaign.controller_demand_before_discovery()
    discover()
    write = campaign.Campaign._write_unlocked
    interrupted = []

    def cut(self, doc):
        if (
            self.path == path
            and doc.get("demand_admission")
            and not doc.get("demand_wake")
            and not interrupted
        ):
            interrupted.append(crash)
            if crash == "before_campaign_commit":
                raise SystemExit("injected before original admission commit")
            write(self, doc)
            raise SystemExit("injected after original admission commit, before wake intent")
        return write(self, doc)

    with monkeypatch.context() as patch:
        patch.setattr(campaign.Campaign, "_write_unlocked", cut)
        with pytest.raises(SystemExit, match="injected"):
            campaign.controller_demand_after_discovery(context, launcher=launch, now=NOW)
    assert interrupted == [crash] and not calls
    state = json.loads((controller / "demand-state.json").read_bytes())
    prepared = state["closer_campaign_demand"]
    assert prepared["wake"]["status"] == "prepared"
    original_request = prepared["request"]
    before = campaign.Campaign(path).read()
    if crash == "after_campaign_commit_before_wake":
        assert before["demand_admission"]["request_id"] == prepared["wake"]["request_id"]
    # Re-enter the SAME completed discovery, not a new generation/authorization.
    campaign.controller_demand_after_discovery(context, launcher=launch, now=NOW + 1)
    after = campaign.Campaign(path).read()
    assert (
        len(calls) == 1
    ), "known prepared/no-submit crash must not permanently lose the original wake"
    assert after["demand_wake"]["status"] == "started"
    assert after["demand_admission"]["admission_count"] == 1
    assert (
        after["demand_admission"]["assignment_generation"]
        == original_request["assignment_generation"]
    )
    assert after["demand_admission"]["deadline"] == original_request["deadline"]
    if before is not None:
        assert after["demand_admission"] == before["demand_admission"]
    # Restart a second time: no duplicate submission or rewritten original receipt.
    exact = path.read_bytes()
    campaign.controller_demand_after_discovery(context, launcher=launch, now=NOW + 2)
    assert len(calls) == 1 and path.read_bytes() == exact


@pytest.mark.parametrize("barrier", ["submitted", "unknown", "denied", "expired"])
def test_prepared_restart_never_retries_uncertain_or_expired_wake(publisher, monkeypatch, barrier):
    _manifest, _pin, _root, controller, path, _wrapper, calls, launch, discover = publisher
    context = campaign.controller_demand_before_discovery()
    discover()
    write = campaign.Campaign._write_unlocked

    def stop_after_admission(self, doc):
        write(self, doc)
        if self.path == path and doc.get("demand_admission") and not doc.get("demand_wake"):
            raise SystemExit("known no-submit cut")

    with monkeypatch.context() as patch:
        patch.setattr(campaign.Campaign, "_write_unlocked", stop_after_admission)
        with pytest.raises(SystemExit):
            campaign.controller_demand_after_discovery(context, launcher=launch, now=NOW)
    obj = campaign.Campaign(path)
    if barrier != "expired":
        obj.update(
            lambda doc: doc.update(
                demand_wake={"status": barrier, "request_id": doc["demand_admission"]["request_id"]}
            )
        )
    original = path.read_bytes()
    at = NOW + 121 if barrier == "expired" else NOW + 1
    campaign.controller_demand_after_discovery(context, launcher=launch, now=at)
    assert not calls and path.read_bytes() == original


@pytest.mark.parametrize("new_discovery", ["same", "future", "different", "expired"])
def test_fresh_controller_restart_does_not_replace_accepted_unsubmitted_request(
    publisher, monkeypatch, new_discovery
):
    _manifest, _pin, _root, controller, path, _wrapper, calls, launch, discover = publisher
    first_context = campaign.controller_demand_before_discovery()
    discover()
    original_write = campaign.Campaign._write_unlocked
    cut = []

    def after_original_commit(self, doc):
        original_write(self, doc)
        if (
            self.path == path
            and doc.get("demand_admission")
            and not doc.get("demand_wake")
            and not cut
        ):
            cut.append(True)
            raise SystemExit("after admission, before submission")

    with monkeypatch.context() as patch:
        patch.setattr(campaign.Campaign, "_write_unlocked", after_original_commit)
        with pytest.raises(SystemExit):
            campaign.controller_demand_after_discovery(first_context, launcher=launch, now=NOW)
    original = campaign.Campaign(path).read()["demand_admission"]
    assert not calls
    # A genuine next controller invocation captures the previous plan, then
    # completes another discovery of the SAME material target/action.
    restarted_context = campaign.controller_demand_before_discovery()
    discover(stamp=NOW + 1, future=new_discovery == "future")
    if new_discovery == "different":
        for name, key in (("closer-assignment.json", "targets"), ("demand-plan.json", "routes")):
            location = controller / name
            value = json.loads(location.read_bytes())
            rows = value[key] if key == "targets" else value[key]["closer"]
            rows[0]["target"] = "offline/other#8"
            campaign._atomic_write(location, value)
    campaign.controller_demand_after_discovery(
        restarted_context, launcher=launch, now=NOW + (121 if new_discovery == "expired" else 1)
    )
    durable = campaign.Campaign(path).read()
    assert len(calls) == (1 if new_discovery == "same" else 0)
    assert (
        durable["demand_admission"] == original
    ), "restart must recover original accepted receipt, not issue a replacement generation/deadline"
    assert durable["demand_admission"]["admission_count"] == 1


@pytest.mark.parametrize(
    "barrier",
    ["hold", "phase", "release", "deadline", "worker", "owner", "pin", "generation", "scope"],
)
def test_submit_transaction_rechecks_current_stop_scope_and_deadline(
    publisher, monkeypatch, barrier
):
    _manifest, _pin, _root, _controller, state, _wrapper, calls, launch, discover = publisher
    context = campaign.controller_demand_before_discovery()
    discover()
    update = campaign.Campaign.update
    hit = []

    def interleave(self, callback, **kwargs):
        if callback.__name__ == "record_wake" and not hit:
            hit.append(barrier)

            def stop(doc):
                if barrier == "hold":
                    doc["hold"] = "explicit-stop-after-admission"
                elif barrier == "phase":
                    doc["phase"] = "release"
                elif barrier == "release":
                    doc["release_attempt"] = {"status": "submitted"}
                elif barrier == "deadline":
                    monkeypatch.setattr(
                        campaign, "_now", lambda: doc["demand_admission"]["deadline"] + 1
                    )
                elif barrier == "worker":
                    doc["workers"].append({"worker_id": "interleaved", "state": "running"})
                elif barrier == "owner":
                    doc["owner"] = "foreign"
                elif barrier == "pin":
                    doc["release_pin"] = "f" * 64
                elif barrier == "generation":
                    doc["generation"] += 1
                elif barrier == "scope":
                    doc["assignment_targets"] = ["offline/other#8"]

            update(self, stop)
        return update(self, callback, **kwargs)

    monkeypatch.setattr(campaign.Campaign, "update", interleave)
    with pytest.raises(campaign.CampaignError, match="changed before submission intent"):
        campaign.controller_demand_after_discovery(context, launcher=launch, now=NOW)
    assert hit == [barrier] and not calls
    doc = campaign.Campaign(state).read()
    assert not doc.get("demand_wake") and doc["demand_admission"]["admission_count"] == 1


def test_submitted_marker_survives_wake_cache_loss_without_retry(publisher):
    _manifest, _pin, _root, _controller, state, _wrapper, calls, _launch, discover = publisher
    context = campaign.controller_demand_before_discovery()
    discover()

    def unknown(argv, log):
        calls.append(argv)
        raise OSError("submission outcome unknown")

    with pytest.raises(OSError):
        campaign.controller_demand_after_discovery(context, launcher=unknown, now=NOW)
    obj = campaign.Campaign(state)
    receipt = obj.read()["demand_admission"]
    assert obj.read()["demand_prepared"]["status"] == "submitted"
    obj.update(lambda doc: doc.pop("demand_wake"))
    next_context = campaign.controller_demand_before_discovery()
    discover(stamp=NOW + 1)
    outcome = campaign.controller_demand_after_discovery(
        next_context, launcher=unknown, now=NOW + 1
    )
    assert outcome["status"] == "held" and len(calls) == 1
    assert obj.read()["demand_admission"] == receipt


@pytest.mark.parametrize(
    "case", ["delivery", "qualification", "policy", "native_owner", "foreign_path"]
)
def test_controller_services_only_pinned_communication_without_native_entry(
    publisher, tmp_path, monkeypatch, case
):
    from test_closer_hold_escalation import world

    import closer_hold_escalation as escalation
    import closer_hold_followup as holds
    import closer_native_observer as native

    manifest, pin, root, _controller, state_path, _wrapper, *_ = publisher
    scratch = tmp_path / "communication-fixture"
    scratch.mkdir()
    source_campaign, _minimal, _source_pin, route, _stamp = world(scratch, monkeypatch)
    doc = source_campaign.read()
    cfg = doc["worker_config"]
    cfg.update(mechanism="native-handoff/v1", policy=manifest["policy"], runtime_manifest=str(pin))
    doc["release_pin"] = manifest["package_digest"]
    for hold in doc["hold_followups"].values():
        hold.update(release_pin=doc["release_pin"], config_sha256=holds._digest(cfg))
    obj = campaign.Campaign(state_path)
    obj.update(lambda _doc: None, create=doc)
    manifest["mechanism_contract"]["hold_escalation_route"] = route
    manifest["hold_escalation_campaign_path"] = str(state_path)
    if case == "foreign_path":
        manifest["hold_escalation_campaign_path"] = str(source_campaign.path)
    campaign.write_manifest(pin, manifest)
    if case == "policy":
        Path(next(iter(manifest["policy"]["toml_sha256"]))).write_text('status = "PAUSED"\n')
    checks, deliveries = [], []
    monkeypatch.setattr(
        campaign,
        "qualify_communication",
        lambda *_: checks.append(True) or (["not qualified"] if case == "qualification" else []),
    )
    actual_service = escalation.service

    def service(*args, **kwargs):
        assert len(args) == 3 and not kwargs  # service obtains fresh write clocks.
        deliveries.append(args[0].path)
        return actual_service(*args, **kwargs)

    monkeypatch.setattr(escalation, "service", service)

    def forbidden(*_args, **_kwargs):
        pytest.fail("communication path reached native ownership or ACT machinery")

    monkeypatch.setattr(holds, "consume", forbidden)
    monkeypatch.setattr(holds, "rearm", forbidden)
    monkeypatch.setattr(native, "publish_admission", forbidden)
    monkeypatch.setattr(campaign, "default_launcher", forbidden)
    monkeypatch.setattr(campaign, "tick", forbidden)
    if case == "native_owner":
        # Ownership-routing seam only; no native capability is issued in this test.
        (native.contract(manifest)[3] / "native-active.json").write_text("{}")
        monkeypatch.setattr(native, "entry_active", lambda *_: True)
        monkeypatch.setattr(campaign, "tick", lambda *_: [{"kind": "native-owner"}])
    output = campaign.tick_safe(str(root))
    if case == "delivery":
        assert deliveries == [state_path] and len(checks) == 1
        assert any(row["kind"] == "escalation_delivered" for row in output)
        request = next(iter(obj.read()["hold_escalations"].values()))
        assert request["status"] == "delivered"
        assert json.loads(Path(route["destination"]).read_bytes())["closer_hold_escalations"]
        campaign.tick_safe(str(root))
        assert len(obj.read()["hold_escalations"]) == 1
    else:
        assert not deliveries
        if case == "native_owner":
            assert output == [{"kind": "native-owner"}] and not checks
        elif case == "qualification":
            assert len(checks) == 1 and any(
                row.get("reason") == "version not qualified" for row in output
            )
        elif case == "policy":
            assert not checks and any(
                row.get("kind") in {"held", "error"}
                and row.get("reason") != "native tick requires observed wrapper entry"
                for row in output
            )
        else:
            assert any("escapes pinned root" in row.get("reason", "") for row in output)


@pytest.mark.parametrize(
    "fault", ["unknown", "corrupt", "unsafe", "unreadable", "signature", "artifact", "module"]
)
def test_real_static_qualification_keeps_unknown_native_owned_while_communicating(
    publisher, tmp_path, monkeypatch, fault
):
    """Genuine supervisor receipt; faulted current observations grant no ACT."""
    import os
    import sys
    import time

    from test_closer_hold_escalation import world
    from test_closer_native_production_composition import _core

    import closer_hold_followup as holds
    import closer_native_observer as native

    monkeypatch.setattr(campaign, "_now", time.time)
    monkeypatch.setattr(campaign, "qualify", _REAL_QUALIFY)
    manifest, pin, root, _controller, state_path, _wrapper, *_ = publisher
    seams = tmp_path / "static-qualification-offline"
    seams.mkdir()
    (seams / "sitecustomize.py").write_text(
        "import socket\ndef blocked(*a,**k): raise AssertionError('network forbidden')\nsocket.socket.connect=blocked\n"
    )
    for name in ("gh", "codex", "claude", "gemini", "cursor", "vibe", "muse"):
        path = seams / name
        path.write_text(
            "#!"
            + sys.executable
            + "\nfrom pathlib import Path\nPath("
            + repr(str(tmp_path / "forbidden-service-probe"))
            + ").write_text('forbidden')\nraise SystemExit(99)\n"
        )
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(seams) + ":" + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(seams) + ":" + manifest["module_dir"])
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
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
    scratch = tmp_path / "static-qualification-communication"
    scratch.mkdir()
    source_campaign, _minimal, _source_pin, route, _stamp = world(scratch, monkeypatch)
    doc = source_campaign.read()
    cfg = doc["worker_config"]
    cfg.update(mechanism="native-handoff/v1", policy=manifest["policy"], runtime_manifest=str(pin))
    doc["release_pin"] = manifest["package_digest"]
    for held in doc["hold_followups"].values():
        held.update(release_pin=doc["release_pin"], config_sha256=holds._digest(cfg))
    obj = campaign.Campaign(state_path)
    obj.update(lambda _doc: None, create=doc)
    manifest["mechanism_contract"]["hold_escalation_route"] = route
    manifest["hold_escalation_campaign_path"] = str(state_path)
    original_wrapper = _wrapper.read_text()
    original_wrapper = (
        original_wrapper.split(native.MARKER)[0] + original_wrapper.split(native.END_MARKER)[1]
    )
    _wrapper.write_text(
        native.render_cursor_wrapper(
            original_wrapper,
            manifest_path=pin,
            campaign_root=root,
            interpreter=Path(sys.executable).resolve(),
            launcher=Path(manifest["launcher"]["path"]),
        )
    )
    manifest["mechanism_contract"]["wrapper"]["sha256"] = native._hash(_wrapper)
    campaign.write_manifest(pin, manifest)
    qualified = native.supervise_native(manifest, pin, budget_seconds=45)
    assert qualified["passed"] and _REAL_QUALIFY(manifest, root) == []
    assert campaign.qualify_communication(manifest, root) == []
    observation_root = native.contract(manifest)[3]
    active = observation_root / "native-active.json"
    if fault == "unknown":
        value = json.loads(active.read_bytes())
        value["status"] = "submitted"
        active.write_text(json.dumps(value))
    elif fault == "corrupt":
        active.write_text("{")
    elif fault == "unsafe":
        active.unlink()
        foreign = tmp_path / "foreign-active.json"
        foreign.write_text("{}")
        active.symlink_to(foreign)
    elif fault == "unreadable":
        active.chmod(0)
    elif fault == "signature":
        receipt = observation_root / "native-qualification.json"
        value = json.loads(receipt.read_bytes())
        value["signature"] = "0" * 64
        receipt.write_text(json.dumps(value))
    elif fault == "artifact":
        receipt = json.loads((observation_root / "native-qualification.json").read_bytes())
        (observation_root / receipt["record"]["nonce"] / "child-result.json").write_text("{}")
    else:
        module = Path(manifest["module_dir"]) / "closer_hold_escalation.py"
        module.write_bytes(module.read_bytes() + b"\n# changed pinned module\n")
    assert _REAL_QUALIFY(manifest, root), "Full ACT qualifier must stay held"
    static_fault = fault in {"signature", "artifact", "module"}
    assert bool(campaign.qualify_communication(manifest, root)) == static_fault

    def forbidden(*_args, **_kwargs):
        pytest.fail("communication qualification reached ACT/native execution")

    monkeypatch.setattr(campaign, "tick", forbidden)
    monkeypatch.setattr(native, "publish_admission", forbidden)
    monkeypatch.setattr(holds, "consume", forbidden)
    monkeypatch.setattr(holds, "rearm", forbidden)
    output = campaign.tick_safe(str(root))
    assert not (tmp_path / "forbidden-service-probe").exists()
    if not static_fault:
        assert any(
            row.get("reason") == "native tick requires observed wrapper entry" for row in output
        )
    if static_fault:
        assert not obj.read().get("hold_escalations")
        assert any(
            row.get("reason") == "version not qualified" or row.get("kind") == "error"
            for row in output
        )
    else:
        assert any(row.get("kind") == "escalation_delivered" for row in output), output
        assert next(iter(obj.read()["hold_escalations"].values()))["status"] == "delivered"
        if fault in {"corrupt", "unsafe", "unreadable"}:
            assert any(row.get("kind") == "error" for row in output)
