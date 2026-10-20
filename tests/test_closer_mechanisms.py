"""Offline CLI supervisor tests. Provider boundary is simulated, never a live acceptance."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_closer_agent_launchers import host_runtime_fixture
from test_closer_runtime_binding import bundle as bundle

import adapters
import closer_agent_launchers as agents
import closer_campaign as campaign
import closer_distribution_package as package
import closer_mechanisms as mechanisms
import closer_runtime_binding as binding
import dispatcher


@pytest.fixture
def configured(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mechanisms, "AUTOMATIONS", home / ".codex" / "automations")
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN", Path(sys.executable))
    manifest, path, root, installed = host_runtime_fixture(tmp_path)
    policy = {
        "schema": mechanisms.POLICY,
        "mechanism": mechanisms.CLI,
        "lane": "closer",
        "worker_owner": "codex",
        "agent": "codex",
        "toml_sha256": {},
    }
    for name in (mechanisms.CONTROLLER, mechanisms.WORKERS[("closer", "codex")]):
        toml = mechanisms.AUTOMATIONS / name / "automation.toml"
        toml.parent.mkdir(parents=True)
        raw = b'status = "ACTIVE"\n'
        toml.write_bytes(raw)
        policy["toml_sha256"][str(toml)] = hashlib.sha256(raw).hexdigest()
    observation = tmp_path / "observations"
    observation.mkdir(mode=0o700)
    profile = agents.offload_profiles("codex")[0]["profile_id"]
    manifest.update(
        policy=policy,
        mechanism_contract={
            "schema": mechanisms.CONTRACT,
            "mechanism": mechanisms.CLI,
            "agent": "codex",
            "profile_id": profile,
            "observation_root": str(observation),
        },
    )
    manifest["command"] = agents.expected_host_launcher_command(
        manifest, manifest_path=path, root=root
    )
    manifest["command_digest"] = campaign.sha256_json(manifest["command"])
    path.write_text(json.dumps(manifest))
    profile = agents.offload_profiles("codex")[0]["profile_id"]
    return manifest, path, root, installed, profile


def transport_boundary(calls, *, unknown=False, ignore=False, failure=False, complex_argv=False):
    def offload(agent, prompt, **kwargs):
        # Simulated provider transport invokes the REAL pinned production launcher.
        # Only the external provider is simulated; PID/argv/durable child evidence is real.
        calls.append(prompt)
        argv = shlex.split(prompt.split("commands: ", 1)[1])
        rid = f"offline-simulated-provider-{len(calls)}"
        kwargs["attempt_started_callback"]({"run_id": rid, "agent": agent})
        if ignore:
            result = {"run_id": rid, "exit": 0, "output": "claimed success without execution"}
        else:
            provider_script = Path(kwargs["cwd"]) / "offline-simulated-provider.py"
            provider_script.write_text(
                "import json,subprocess,sys; "
                "a=json.loads(sys.argv[1]); p=subprocess.run(a); "
                "r=0 if a[-1]=='first' and p.returncode==-9 else p.returncode; "
                + ("sys.exit(1)" if failure else "sys.exit(r)")
            )
            provider_argv = [sys.executable, str(provider_script), json.dumps(argv)]
            if complex_argv:
                provider_argv += ["offline multiline\nargument — 雪", "x" * 16000]
            done = dispatcher.run_observed_offload(
                ["bash", "-lc", shlex.join(provider_argv)],
                callback=kwargs["process_started_callback"],
                observation={
                    "run_id": rid,
                    "agent": agent,
                    "profile_id": kwargs["profile_id"],
                    "provider_argv": provider_argv,
                },
                cwd=kwargs["cwd"],
                timeout=kwargs["timeout"],
                stdin=subprocess.DEVNULL,
            )
            result = {"run_id": rid, "exit": done.returncode, "output": done.stdout}
        kwargs["attempt_receipt_callback"](result)
        if unknown:
            result.update(outcome_uncertain=True, retry_safe=False)
        return result

    return offload


def test_actual_launcher_supervision_proves_cli_only_and_child_process_bootstrap(configured):
    manifest, path, _root, _installed, profile = configured
    calls = []
    receipt = mechanisms.supervise_cli(
        str(path),
        "codex",
        profile,
        timeout=8,
        budget_seconds=21,
        offload=transport_boundary(calls, complex_argv=True),
    )
    assert receipt.get("passed"), receipt
    assert len(calls) == 2
    assert receipt["desktop_qualified"] is False and receipt["queue_action_accepted"] is False
    assert len({p["pid"] for p in receipt["processes"]}) == 2
    assert all(
        p["source"] == "os-byte-argv"
        and p["os_ancestry"]
        and p["provider_pid"]
        and p["transport_pid"]
        and p["provider_argv_sha256"]
        for p in receipt["processes"]
    )
    assert mechanisms.qualification_problems(manifest) == []
    assert binding.desktop_problems(manifest, receipt) == [binding.DESKTOP_UNSUPPORTED]
    assert not campaign.check_promotion(manifest, ["codex"], [receipt])["promotable"]
    observation = Path(manifest["mechanism_contract"]["observation_root"])
    active = json.loads((observation / "active.json").read_text())
    intent = json.loads((observation / active["nonce"] / "first-intent.json").read_text())
    assert intent["binding"] == mechanisms.binding(manifest)
    assert intent["argv"][2] == "qualification-child"
    assert intent["original_deadline"] > 0
    readback = mechanisms.supervision_status(str(path))
    assert readback["retry_authorized"] is False
    assert readback["active"]["status"] == "terminal"
    assert readback["evidence"]["first-terminal"]["observed_terminal"]
    spawn = json.loads((observation / active["nonce"] / "first-provider-spawn.json").read_text())
    assert "provider_argv" not in spawn and "transport_argv" not in spawn
    assert spawn["provider_argv_sha256"]
    assert "offline multiline" not in json.dumps(readback)
    # Fresh process reads and validates the same persisted observation, not a
    # registration inherited from this test interpreter.
    observed = subprocess.run(
        [
            manifest["runtime"]["python"],
            manifest["launcher"]["path"],
            "cli-supervision-status",
            "--manifest",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(observed.stdout)["qualification_problems"] == []
    altered = json.loads(json.dumps(manifest))
    altered["mechanism_contract"]["profile_id"] = "different-profile"
    assert mechanisms.qualification_problems(altered)
    artifact = observation / active["nonce"] / "result.json"
    artifact.write_text("{}")
    assert mechanisms.qualification_problems(manifest)


@pytest.mark.parametrize("fault", ["ignored", "unknown", "failed"])
def test_missing_launcher_or_unknown_completion_holds_without_resubmission(configured, fault):
    manifest, path, _root, _installed, profile = configured
    calls = []
    record = mechanisms.supervise_cli(
        str(path),
        "codex",
        profile,
        timeout=2,
        budget_seconds=9,
        offload=transport_boundary(
            calls, ignore=fault == "ignored", unknown=fault == "unknown", failure=fault == "failed"
        ),
    )
    assert not record["passed"]
    assert len(calls) == 1
    assert mechanisms.qualification_problems(manifest)
    with pytest.raises(mechanisms.MechanismError, match="reconciliation"):
        mechanisms.supervise_cli(
            str(path),
            "codex",
            profile,
            timeout=2,
            budget_seconds=9,
            offload=transport_boundary(calls),
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "pause",
        "controller_pause",
        "foreign_path",
        "symlink",
        "duplicate_alias",
        "missing_controller",
    ],
)
def test_cli_canonical_policy_fails_closed_on_fresh_read(configured, fault):
    manifest, *_ = configured
    policy = manifest["policy"]
    paths = list(policy["toml_sha256"])
    worker = Path(paths[1])
    if fault in {"pause", "controller_pause"}:
        p = worker if fault == "pause" else Path(paths[0])
        raw = b'status = "PAUSED"\n'
        p.write_bytes(raw)
        # Even a newly pinned paused config never grants permission.
        policy["toml_sha256"][str(p)] = hashlib.sha256(raw).hexdigest()
    elif fault == "foreign_path":
        policy["toml_sha256"]["/tmp/foreign.toml"] = "a" * 64
    elif fault == "symlink":
        replacement = worker.with_name("other.toml")
        worker.rename(replacement)
        worker.symlink_to(replacement)
    elif fault == "duplicate_alias":
        policy["toml_sha256"][str(worker.parent / ".." / worker.parent.name / worker.name)] = (
            policy["toml_sha256"][str(worker)]
        )
    else:
        policy["toml_sha256"].pop(paths[0])
    assert binding.policy_problem(policy, mechanism=mechanisms.CLI)


def test_policy_bytes_changed_after_successful_read_are_held(configured):
    manifest, *_ = configured
    assert binding.policy_problem(manifest["policy"], mechanism=mechanisms.CLI) is None
    worker = Path(list(manifest["policy"]["toml_sha256"])[1])
    worker.write_text('status = "PAUSED"\n')
    assert binding.policy_problem(manifest["policy"], mechanism=mechanisms.CLI)


@pytest.mark.parametrize("other", [mechanisms.DESKTOP, mechanisms.NATIVE, "foreign/v1"])
def test_cli_policy_and_evidence_cannot_cross_mechanisms(configured, other):
    manifest, *_ = configured
    assert mechanisms.mechanism(manifest) == mechanisms.CLI
    with pytest.raises(mechanisms.MechanismError):
        mechanisms.mechanism(manifest, other)
    manifest["mechanism_contract"]["mechanism"] = other
    with pytest.raises(mechanisms.MechanismError):
        mechanisms.mechanism(manifest)


def test_unsigned_receipt_or_old_compat_child_cannot_qualify_cli(configured):
    manifest, *_ = configured
    root = Path(manifest["mechanism_contract"]["observation_root"])
    (root / "active.json").write_text(json.dumps({"status": "terminal"}))
    (root / "qualification.json").write_text(
        json.dumps({"kind": "launcher-entrypoint", "passed": True})
    )
    assert mechanisms.qualification_problems(manifest)


def test_child_cli_binding_is_in_closed_package_and_launch_command(configured):
    manifest, path, root, installed, _ = configured
    assert "closer_mechanisms.py" in manifest["modules"]
    assert (
        "closer_mechanisms.py"
        in json.loads((installed / "closer-runtime-package.json").read_text())["modules"]
    )
    argv = agents.expected_host_launcher_command(manifest, manifest_path=path, root=root)
    assert argv[-2:] == ["--mechanism", mechanisms.CLI]
    assert (
        agents.verify_runtime_manifest(
            manifest,
            manifest_path=path,
            root=root,
            launcher_argv=argv,
            own_launcher_path=installed / "closer_launcher.py",
        )
        == []
    )


def test_desktop_default_contract_is_unchanged_and_incomplete_native_stays_held(
    configured, monkeypatch
):
    manifest, *_ = configured
    manifest.pop("mechanism_contract")
    manifest["policy"] = {}
    assert mechanisms.mechanism(manifest) == mechanisms.DESKTOP
    assert binding.policy_problem({})
    manifest["mechanism_contract"] = {"schema": mechanisms.CONTRACT, "mechanism": mechanisms.NATIVE}
    monkeypatch.setattr(binding, "CoreBundle", lambda _pin: object())
    assert any(
        "execution mechanism remains held: native wrapper/actor is not supported" in p
        for p in campaign.qualify(manifest, "/tmp/offline-no-promotion")
    )


def test_cli_policy_never_unlocks_default_desktop_gate(configured):
    manifest, *_ = configured
    assert binding.policy_problem(manifest["policy"], mechanism=mechanisms.CLI) is None
    assert binding.policy_problem(manifest["policy"])
    assert binding.policy_problem(manifest["policy"], mechanism=mechanisms.DESKTOP)
    assert binding.policy_problem(manifest["policy"], mechanism=mechanisms.NATIVE)


@pytest.mark.parametrize("fault", ["agent", "profile", "missing_profile", "mechanism"])
def test_supervisor_never_submits_for_unqualified_identity(configured, fault):
    manifest, path, _root, _installed, profile = configured
    calls = []
    agent = "codex"
    if fault == "agent":
        agent = "claude"
    elif fault == "profile":
        profile = "foreign-profile"
    elif fault == "missing_profile":
        manifest["mechanism_contract"].pop("profile_id")
    else:
        manifest["mechanism_contract"]["mechanism"] = mechanisms.DESKTOP
    path.write_text(json.dumps(manifest))
    with pytest.raises((mechanisms.MechanismError, campaign.CampaignError)):
        mechanisms.supervise_cli(
            str(path), agent, profile, budget_seconds=125, offload=transport_boundary(calls)
        )
    assert calls == []


@pytest.mark.parametrize("mode", ["round", "target-delegate"])
def test_cli_worker_omitted_flags_cannot_bypass_pin_and_qualification(
    configured, tmp_path, monkeypatch, mode
):
    manifest, _path, _root, _installed, _profile = configured
    state = tmp_path / "worker.campaign.json"
    result = tmp_path / "worker.json"
    target = campaign.ROUND_UNIT if mode == "round" else "owner/repo#7"
    record = campaign.Campaign(state)
    record.begin(
        "offline-worker",
        [target],
        owner="offline-owner",
        release_pin=manifest["package_digest"],
        worker_config={
            "mechanism": mechanisms.CLI,
            "policy": manifest["policy"],
            "granularity": mode,
        },
    )
    record.update(
        lambda doc: doc["workers"].append(
            {"worker_id": "worker", "target": target, "state": "launched"}
        )
    )

    def forbidden(*a, **kw):
        pytest.fail("unqualified CLI worker cannot execute any transport")

    monkeypatch.setattr(campaign, "run_dispatcher", forbidden)
    monkeypatch.setattr(binding, "run_native_round", forbidden)
    outcome = campaign.run_worker(
        str(state),
        str(result),
        target,
        "offline-owner",
        1,
        holder=lambda _: {"agent": campaign.AGENT},
        native_probe=lambda _doc: None,
        delegate=forbidden,
        runtime_manifest=None,
        mechanism=None,
    )
    assert outcome == "unavailable"
    assert "pinned manifest" in json.loads(result.read_text())["detail"]


def test_direct_cli_delegate_cannot_substitute_policy(configured):
    manifest, path, _root, _installed, profile = configured
    cfg = {
        "mechanism": mechanisms.CLI,
        "policy": {**manifest["policy"], "extra": "different"},
        "runtime_manifest": str(path),
        "delegate": {"lane": "closer", "prompt": "offline"},
    }
    called = []
    outcome, detail = campaign.delegate_target(
        cfg, "owner/repo#7", "codex", profile, delegate=lambda *a, **kw: called.append(True)
    )
    assert outcome == "unavailable" and "policy" in detail
    assert called == []


def test_direct_launcher_without_provider_spawn_observation_is_rejected(configured):
    manifest, path, _root, _installed, profile = configured
    children = []

    def direct(agent, prompt, **kwargs):
        argv = shlex.split(prompt.split("commands: ", 1)[1])
        rid = "offline-direct-not-a-provider"
        kwargs["attempt_started_callback"]({"run_id": rid, "agent": agent})
        child = subprocess.Popen(
            argv, cwd=kwargs["cwd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        children.append(child)
        result = {"run_id": rid, "exit": 0}
        kwargs["attempt_receipt_callback"](result)
        return result

    try:
        result = mechanisms.supervise_cli(
            str(path), "codex", profile, timeout=1, budget_seconds=7, offload=direct
        )
        assert not result["passed"]
        assert mechanisms.qualification_problems(manifest)
    finally:
        # Only the test that owns this Popen handle cleans its own fixture.
        for child in children:
            child.kill()
            child.wait(timeout=3)


def test_real_core_cli_qualification_preserves_native_uncertainty(configured, bundle, tmp_path):
    old, _path, _root, _installed, profile = configured
    core = json.loads((bundle.root / "closer-core-package.json").read_text())
    supported = {
        "owner": "Workflows",
        "contract": campaign.CONTRACT,
        "core_version": "1",
        "package_digest": core["package_digest"],
    }
    public = package.build_public_runtime_descriptor(
        Path(campaign.__file__).parent,
        candidate_sha="c" * 40,
        state_versions={"campaign": 1, "release": 1},
        supported_core=supported,
    )
    installed = tmp_path / "actual-core-runtime"
    package.materialize_runtime_tree(Path(campaign.__file__).parent, installed, public)
    root = tmp_path / "actual-core-campaigns"
    root.mkdir()
    manifest = package.host_binding_from_public(
        public,
        install_root=installed,
        campaign_root=root,
        python=sys.executable,
        core_binding={**supported, "package_root": str(bundle.root)},
        policy=old["policy"],
        mechanism_contract=old["mechanism_contract"],
    )
    path = installed / "host-manifest.json"
    manifest["command"] = agents.expected_host_launcher_command(
        manifest, manifest_path=path, root=root
    )
    manifest["command_digest"] = campaign.sha256_json(manifest["command"])
    path.write_text(json.dumps(manifest))
    calls = []
    result = mechanisms.supervise_cli(
        str(path), "codex", profile, timeout=8, budget_seconds=21, offload=transport_boundary(calls)
    )
    assert result["passed"], result
    assert campaign.qualify(manifest, root) == []
    target = "owner/repo#7"
    native = tmp_path / "private-native-ledger.json"
    native.write_text(json.dumps({"items": {target: {"operation": {"status": "unknown"}}}}))
    record = campaign.Campaign(root / "offline.campaign.json")
    record.begin(
        "offline-core",
        [target],
        owner="offline",
        release_pin=manifest["package_digest"],
        worker_config={
            "granularity": "target-delegate",
            "mechanism": mechanisms.CLI,
            "policy": manifest["policy"],
            "dispatch_state": str(native),
        },
    )
    before = native.read_bytes()
    report = campaign.tick(
        root,
        path,
        mechanism=mechanisms.CLI,
        route_fn=lambda _: {"agent": "codex", "profile_id": profile},
        launcher=lambda *a: pytest.fail("CLI qualification cannot bypass native unknown operation"),
    )
    assert report[0]["kind"] == "wait" and report[0]["reason"] == "native_owner"
    assert "outcome unresolved" in report[0]["detail"]
    assert native.read_bytes() == before and len(calls) == 2


def test_early_lineage_failure_drains_owned_provider_with_original_budget(configured, monkeypatch):
    manifest, path, _root, _installed, profile = configured
    calls = []
    original = mechanisms._lineage

    def wrong_lineage(pid, **kwargs):
        rows = original(pid, **kwargs)
        for row in rows:
            row["argv_sha256"] = "offline lineage mismatch"
        return rows

    monkeypatch.setattr(mechanisms, "_lineage", wrong_lineage)
    began = time.time()
    result = mechanisms.supervise_cli(
        str(path),
        "codex",
        profile,
        timeout=2,
        budget_seconds=9,
        offload=transport_boundary(calls),
    )
    assert not result["passed"] and len(calls) == 1
    assert "actual selected CLI ancestor" in result["reason"]
    assert result["provider_thread_drained"] is True
    assert result["provider_result"]["run_id"] == "offline-simulated-provider-1"
    assert result["provider_result"]["exit"] == 0
    assert result["cleanup"][0]["target"] == "qualification-child"
    assert result["cleanup"][0]["signal_sent"]
    assert result["original_deadline"] - result["action_deadline"] == 5
    assert began <= result["original_deadline"] - 9 <= time.time()
    root = Path(manifest["mechanism_contract"]["observation_root"])
    active = json.loads((root / "active.json").read_text())
    intent = json.loads((root / active["nonce"] / "first-intent.json").read_text())
    assert intent["original_deadline"] == result["original_deadline"]
    assert intent["action_deadline"] == result["action_deadline"]
    assert active["status"] == "unknown"
    assert mechanisms.qualification_problems(manifest)


def test_controlled_crash_denial_does_not_try_alternate_group_cleanup(configured, monkeypatch):
    import errno
    import os

    manifest, path, _root, _installed, profile = configured
    calls, denied_pids, group_attempts = [], [], []
    original_kill, original_killpg = os.kill, os.killpg

    def denied(pid, sig):
        denied_pids.append(pid)
        raise PermissionError(errno.EPERM, "simulated controlled-crash denial")

    def tracked_group(pid, sig):
        # Dispatcher owns its normal timeout cleanup; the supervisor must not
        # route around its own denied controlled crash with a group signal.
        group_attempts.append((pid, sig))
        return original_killpg(pid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(mechanisms.os, "kill", denied)
        patch.setattr(mechanisms.os, "killpg", tracked_group)
        result = mechanisms.supervise_cli(
            str(path),
            "codex",
            profile,
            timeout=2,
            budget_seconds=9,
            offload=transport_boundary(calls),
        )
    try:
        assert not result["passed"] and len(calls) == 1
        assert len(denied_pids) == 1
        # Signal zero only probes liveness; after the controlled crash denial,
        # no alternate terminating group signal may be attempted.
        assert all(sig == 0 for _pid, sig in group_attempts)
        assert result["cleanup"][0]["phase"] == "controlled-crash"
        assert result["cleanup"][0]["error"]["type"] == "PermissionError"
        assert result["cleanup"][0]["error"]["errno"] == errno.EPERM
        assert result["cleanup"][0]["pid"] == denied_pids[0]
        assert "simulated controlled-crash denial" in result["reason"]
        assert mechanisms.qualification_problems(manifest)
    finally:
        # The denial was simulated. Clean only this test's exact child if its
        # external-boundary fixture has not already timed it out.
        for pid in denied_pids:
            try:
                original_kill(pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("probe", ["identity", "lineage", "terminal"])
def test_expired_observation_budget_never_starts_ps(monkeypatch, probe):
    monkeypatch.setattr(mechanisms.time, "monotonic", lambda: 20)
    monkeypatch.setattr(mechanisms.subprocess, "run", lambda *a, **kw: pytest.fail("expired ps"))
    with pytest.raises(mechanisms.MechanismError, match="budget|not terminal"):
        if probe == "identity":
            mechanisms._identity(10, ["offline"], deadline=19)
        elif probe == "lineage":
            mechanisms._lineage(10, deadline=19)
        else:
            mechanisms._wait_terminal({"pid": 10}, 19)


def test_slow_ancestry_probe_cannot_renew_original_budget(monkeypatch):
    clock = iter([10.0, 11.0])
    monkeypatch.setattr(mechanisms.time, "monotonic", lambda: next(clock))
    calls = []

    def observed(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "999999 Mon Oct 19 00:00:00 2026", "")

    monkeypatch.setattr(mechanisms.subprocess, "run", observed)
    with pytest.raises(mechanisms.MechanismError, match="budget exhausted"):
        mechanisms._lineage(10, deadline=10.5)
    assert len(calls) == 1
    assert calls[0]["timeout"] == 0.5


def test_claude_offload_uses_codex_supervising_policy_with_own_receipt(configured):
    manifest, path, _root, _installed, codex_profile = configured
    pins = dict(manifest["policy"]["toml_sha256"])
    claude_profile = agents.offload_profiles("claude")[0]["profile_id"]
    manifest["policy"]["agent"] = "claude"
    manifest["mechanism_contract"].update(agent="claude", profile_id=claude_profile)
    path.write_text(json.dumps(manifest))
    assert manifest["policy"]["worker_owner"] == "codex"
    assert mechanisms.configured_policy_problem(manifest["policy"]) is None
    assert manifest["policy"]["toml_sha256"] == pins
    # Admission of the configured policy is not qualification of its provider.
    assert mechanisms.qualification_problems(manifest)
    calls = []
    result = mechanisms.supervise_cli(
        str(path),
        "claude",
        claude_profile,
        timeout=8,
        budget_seconds=21,
        offload=transport_boundary(calls),
    )
    assert result["passed"], result
    assert result["agent"] == "claude" and result["profile_id"] == claude_profile
    assert len(calls) == 2
    assert mechanisms.qualification_problems(manifest) == []
    assert not result["desktop_qualified"] and not result["queue_action_accepted"]
    other = json.loads(json.dumps(manifest))
    other["policy"]["agent"] = "codex"
    other["mechanism_contract"].update(agent="codex", profile_id=codex_profile)
    assert mechanisms.configured_policy_problem(other["policy"]) is None
    assert mechanisms.qualification_problems(other)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_owner",
        "null_owner",
        "unknown_owner",
        "provider_as_owner",
        "missing_provider",
        "excluded_provider",
        "unknown_provider",
        "contract_provider_mismatch",
        "profile_provider_mismatch",
    ],
)
def test_supervising_owner_provider_roles_fail_closed_before_submission(configured, fault):
    manifest, path, _root, _installed, profile = configured
    policy = manifest["policy"]
    if fault == "missing_owner":
        policy.pop("worker_owner")
    elif fault == "null_owner":
        policy["worker_owner"] = None
    elif fault == "unknown_owner":
        policy["worker_owner"] = "unknown"
    elif fault == "provider_as_owner":
        policy["worker_owner"] = "claude"
        policy["agent"] = "claude"
    elif fault == "missing_provider":
        policy.pop("agent")
    elif fault == "excluded_provider":
        policy["agent"] = "aider"
        manifest["mechanism_contract"]["agent"] = "aider"
    elif fault == "unknown_provider":
        policy["agent"] = "unknown"
        manifest["mechanism_contract"]["agent"] = "unknown"
    elif fault == "contract_provider_mismatch":
        policy["agent"] = "claude"
    else:
        policy["agent"] = "claude"
        manifest["mechanism_contract"]["agent"] = "claude"
        # Original Codex profile must not follow a different provider.
        assert manifest["mechanism_contract"]["profile_id"] == profile
    path.write_text(json.dumps(manifest))
    calls = []
    with pytest.raises((mechanisms.MechanismError, campaign.CampaignError)):
        mechanisms.supervise_cli(
            str(path),
            policy.get("agent", "codex"),
            profile,
            timeout=2,
            budget_seconds=9,
            offload=transport_boundary(calls),
        )
    assert not calls


@pytest.mark.parametrize("explicit", [mechanisms.CLI, mechanisms.DESKTOP, "unknown/v1"])
def test_explicit_worker_mechanism_without_pin_refuses_without_claiming_it(
    configured, tmp_path, monkeypatch, explicit
):
    manifest, _path, _root, _installed, _profile = configured
    state, result = tmp_path / "w.campaign.json", tmp_path / "w.json"
    record = campaign.Campaign(state)
    record.begin(
        "offline",
        [campaign.ROUND_UNIT],
        owner="owner",
        release_pin=manifest["package_digest"],
        worker_config={"granularity": "round"},
    )
    record.update(
        lambda doc: doc["workers"].append(
            {"worker_id": "w", "target": campaign.ROUND_UNIT, "state": "launched"}
        )
    )

    def forbidden(*a, **kw):
        pytest.fail("unverified mechanism executed transport")

    monkeypatch.setattr(campaign, "run_dispatcher", forbidden)
    monkeypatch.setattr(binding, "run_native_round", forbidden)
    outcome = campaign.run_worker(
        str(state),
        str(result),
        campaign.ROUND_UNIT,
        "owner",
        1,
        holder=lambda _: {"agent": campaign.AGENT},
        native_probe=forbidden,
        delegate=forbidden,
        mechanism=explicit,
        runtime_manifest=None,
    )
    assert outcome == "unavailable"
    durable = json.loads(result.read_text())
    assert "pinned manifest" in durable["detail"]
    assert durable.get("mechanism") is None


@pytest.mark.parametrize("state", ["pending", "missing", "concurrent", "policy"])
def test_launcher_expected_supervision_refusals_are_json78(configured, monkeypatch, capsys, state):
    import fcntl

    import closer_launcher as launcher

    manifest, path, _root, _installed, profile = configured
    root = Path(manifest["mechanism_contract"]["observation_root"])
    monkeypatch.setattr(launcher, "pin_problems", lambda _manifest: [])
    monkeypatch.setattr(launcher, "load_runtime", lambda _manifest: campaign)
    args = [
        "qualify-cli",
        "--manifest",
        str(path),
        "--agent",
        "codex",
        "--profile-id",
        profile,
        "--timeout",
        "2",
        "--budget-seconds",
        "9",
    ]
    lock = None
    if state == "pending":
        (root / "active.json").write_text(json.dumps({"status": "unknown"}))
    elif state == "missing":
        args = ["cli-supervision-status", "--manifest", str(path)]
    elif state == "concurrent":
        lock = (root / "supervisor.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        Path(next(iter(manifest["policy"]["toml_sha256"]))).write_text('status="PAUSED"\n')
    try:
        assert launcher.main(args) == 78
        captured = capsys.readouterr()
        value = json.loads(captured.out)
        assert value["launcher"] == "refused" and value["problems"]
        assert "Traceback" not in captured.err
    finally:
        if lock:
            lock.close()
