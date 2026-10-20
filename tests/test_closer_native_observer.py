"""Native wrapper qualification never claims a Desktop scheduler or calls providers."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import closer_campaign as campaign
import closer_mechanisms as mechanisms
import closer_native_observer as native
import closer_runtime_binding as runtime_binding


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "private-handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    automations = home / ".codex/automations"
    monkeypatch.setattr(mechanisms, "AUTOMATIONS", automations)
    pins = {}
    for name in (mechanisms.CONTROLLER, "imi-merge-verify-closer"):
        path = automations / name / "automation.toml"
        path.parent.mkdir(parents=True)
        path.write_text('status = "ACTIVE"\n')
        pins[str(path)] = native._hash(path)
    modules = tmp_path / "modules"
    shutil.copytree(
        Path(campaign.__file__).parent, modules, ignore=shutil.ignore_patterns("__pycache__")
    )
    observations = tmp_path / "native-observations"
    observations.mkdir(mode=0o700)
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    manifest_path = tmp_path / "manifest.json"
    python = Path(sys.executable).resolve()
    manifest = campaign.make_manifest(
        modules,
        candidate_sha="a" * 40,
        python=str(python),
        state_versions={"campaign": 1, "release": 1},
        manifest_path=manifest_path,
        campaign_root=campaigns,
    )
    wrapper = home / ".codex/bin/cursor-lane.sh"
    wrapper.parent.mkdir(parents=True)
    source = '#!/bin/bash\nset -euo pipefail\nLANE="${1:?}"\ncase "$LANE" in\ncloser|opener) ;;\n*) exit 2 ;;\nesac\n\nexport PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"\n'
    source += "echo FORBIDDEN_LEGACY_PROVIDER >&2\nexit 99\n"
    rendered = native.render_cursor_wrapper(
        source,
        manifest_path=manifest_path,
        campaign_root=campaigns,
        interpreter=python,
        launcher=modules / "closer_launcher.py",
    )
    wrapper.write_text(rendered)
    enabled = home / ".codex/handoff/closer-native-handoff.enabled"
    enabled.parent.mkdir(parents=True)
    enabled.touch()
    manifest["policy"] = {
        "schema": native.POLICY,
        "mechanism": mechanisms.NATIVE,
        "lane": "closer",
        "worker_owner": "codex",
        "agent": "cursor",
        "toml_sha256": pins,
    }
    manifest["mechanism_contract"] = {
        "schema": mechanisms.CONTRACT,
        "mechanism": mechanisms.NATIVE,
        "actor": "cursor",
        "wrapper": {"path": str(wrapper), "sha256": native._hash(wrapper)},
        "interpreter": {"path": str(python), "sha256": native._hash(python)},
        "observation_root": str(observations),
        "campaign_root": str(campaigns),
    }
    campaign.write_manifest(manifest_path, manifest)
    return manifest, manifest_path, wrapper


def test_native_policy_is_distinct_and_pause_wins(host):
    manifest, _, _ = host
    assert mechanisms.mechanism(manifest) == mechanisms.NATIVE
    assert runtime_binding.policy_problem(manifest["policy"], mechanism=mechanisms.NATIVE) is None
    assert runtime_binding.policy_problem(manifest["policy"], mechanism=mechanisms.CLI)
    worker = Path(next(path for path in manifest["policy"]["toml_sha256"] if "imi-merge" in path))
    worker.write_text('status = "PAUSED"\n')
    assert native.policy_problem(manifest["policy"])


@pytest.mark.parametrize(
    "change", ["wrapper", "interpreter", "actor", "policy", "closure", "optin"]
)
def test_bad_host_binding_holds_before_any_wrapper_start(host, monkeypatch, change):
    manifest, path, wrapper = host
    if change == "wrapper":
        wrapper.write_text(wrapper.read_text() + "# changed\n")
    elif change == "interpreter":
        manifest["mechanism_contract"]["interpreter"]["sha256"] = "f" * 64
    elif change == "actor":
        manifest["mechanism_contract"]["actor"] = "claude_code"
    elif change == "policy":
        manifest["policy"]["schema"] = "caller-policy/v1"
    elif change == "closure":
        manifest["modules"].pop("closer_native_observer.py")
    else:
        (Path.home() / ".codex/handoff/closer-native-handoff.enabled").unlink()
    monkeypatch.setattr(
        native.subprocess, "Popen", lambda *a, **k: pytest.fail("no process may start")
    )
    with pytest.raises((mechanisms.MechanismError, OSError)):
        native.supervise_native(manifest, path, budget_seconds=20)


def test_unpatched_wrapper_cannot_fall_through_to_provider(host, monkeypatch):
    manifest, path, wrapper = host
    wrapper.write_text("#!/bin/bash\necho FORBIDDEN_LEGACY_PROVIDER\n")
    manifest["mechanism_contract"]["wrapper"]["sha256"] = native._hash(wrapper)
    monkeypatch.setattr(
        native.subprocess, "Popen", lambda *a, **k: pytest.fail("legacy wrapper must never start")
    )
    with pytest.raises(mechanisms.MechanismError, match="exact pinned entry"):
        native.supervise_native(manifest, path, budget_seconds=20)


def test_native_receipt_cannot_promote_desktop(host):
    manifest, _, _ = host
    result = campaign.check_promotion(
        manifest, ["cursor"], [{"kind": "native-wrapper-qualification", "runner_id": "cursor"}]
    )
    assert not result["promotable"]
    assert "desktop observer unsupported" in str(result)


def test_ordinary_entry_without_observation_cannot_launch(host, monkeypatch):
    manifest, path, _ = host
    monkeypatch.setattr(native, "_own_identity", lambda *_a, **_k: pytest.fail("not qualified yet"))
    with pytest.raises(mechanisms.MechanismError, match="qualification held"):
        native.native_entry(manifest, path, manifest["mechanism_contract"]["campaign_root"])
    assert not (
        Path(manifest["mechanism_contract"]["observation_root"]) / "native-active.json"
    ).exists()


def test_request_cannot_read_outside_observation_root(host):
    manifest, path, _ = host
    with pytest.raises(mechanisms.MechanismError, match="outside private"):
        native._request(path, manifest)


def test_real_owned_wrapper_handoff_restart_and_durable_result(host):
    manifest, path, _ = host
    result = native.supervise_native(manifest, path, budget_seconds=45)
    assert result["passed"] and result["desktop_qualified"] is False
    assert result["scheduler_origin"] == "unobserved"
    assert result["lifecycle"]["crash_exit"] == -9
    assert native.qualification_problems(manifest) == []
    root = Path(manifest["mechanism_contract"]["observation_root"])
    artifact = root / result["nonce"] / "child-result.json"
    exact = artifact.read_bytes()
    artifact.write_bytes(exact + b" ")
    assert "phase artifact changed" in str(native.qualification_problems(manifest))
    artifact.write_bytes(exact)
    assert native.qualification_problems(manifest) == []
    old = (root / "native-qualification.json").read_bytes()
    # Caller JSON cannot clear a missing return and buy another submission.
    mechanisms._write(root / "native-active.json", {"status": "submitted", "nonce": "0" * 32})
    assert "unknown" in str(native.qualification_problems(manifest))
    with pytest.raises(mechanisms.MechanismError, match="unknown"):
        native.supervise_native(manifest, path, budget_seconds=45)
    assert (root / "native-qualification.json").read_bytes() == old


def test_direct_native_tick_cannot_claim_wrapper_provenance(host, monkeypatch):
    manifest, path, _ = host
    monkeypatch.setattr(campaign, "qualify", lambda *_: [])
    root = Path(manifest["mechanism_contract"]["observation_root"])
    native._write(root / "native-active.json", {"status": "terminal", "nonce": "b" * 32})
    assert campaign.tick(
        manifest["mechanism_contract"]["campaign_root"], path, qualifier=lambda *_: []
    ) == [{"kind": "held", "reason": "native tick requires observed wrapper entry"}]


def test_exact_prelaunch_worker_handoff_survives_entry_transition(host):
    manifest, path, _ = host
    root = Path(manifest["mechanism_contract"]["observation_root"])
    nonce = "b" * 32
    (root / nonce).mkdir()
    native._write(
        root / "native-active.json",
        {"status": "submitted", "nonce": nonce, "binding": native.binding(manifest)},
    )
    mechanisms._key(root, create=True)
    native._CURRENT_ENTRY[str(root)] = nonce
    fields = dict(
        state=str(path.parent / "one.campaign.json"),
        target="__round__",
        owner="owner",
        generation=1,
        result=str(path.parent / "abcdef012345.json"),
        manifest_path=str(path),
    )
    argv = [manifest["mechanism_contract"]["interpreter"]["path"], "campaign.py", "worker"]
    for key, value in fields.items():
        flag = "runtime-manifest" if key == "manifest_path" else key
        argv += ["--" + flag, str(value)]
    argv += ["--mechanism", mechanisms.NATIVE]
    try:
        native.authorize_worker(manifest, argv)
        native._CURRENT_ENTRY.pop(str(root))
        native.accept_worker(manifest, **fields)
        assert native.entry_active(manifest)
        native._CURRENT_ENTRY.pop(str(root))
        with pytest.raises(mechanisms.MechanismError, match="differs"):
            native.accept_worker(manifest, **{**fields, "target": "foreign"})
        assert str(root) not in native._CURRENT_ENTRY
    finally:
        native._CURRENT_ENTRY.pop(str(root), None)


def test_phase_artifact_tamper_holds_native_qualification(host):
    manifest, _, _ = host
    # A caller's JSON and nominal terminal status cannot substitute signed phases.
    root = Path(manifest["mechanism_contract"]["observation_root"])
    native._write(root / "native-qualification.json", {"record": {}, "signature": "a" * 64})
    assert native.qualification_problems(manifest)


@pytest.fixture
def scoped_receipt(tmp_path, monkeypatch):
    import closer_resources

    assignment = tmp_path / "assignment.json"
    assignment.write_text('{"targets":[{"target":"owner/repo#1"},{"target":"owner/repo#2"}]}')
    state = tmp_path / "dispatch.json"
    pipeline = Path(str(state) + ".pipeline.json")
    round_id = "a" * 64
    receipt = closer_resources.scoped_root(pipeline) / "rounds" / (round_id + ".json")
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"immutable":"original named receipt"}')
    cfg = {
        "mechanism": mechanisms.NATIVE,
        "assignment": str(assignment),
        "dispatch_state": str(state),
        "handoff_dir": str(tmp_path / "handoff"),
        "native_hooks": {
            "postrun_dependencies": {
                "assignment": {"path": str(assignment), "sha256": native._hash(assignment)}
            }
        },
    }
    verdict = {
        "round_id": round_id,
        "admitted_domain_valid": True,
        "global_stop_valid": False,
        "run_ids": {"owner/repo#1": "b" * 64},
        "held_targets": ["owner/repo#2"],
        "admitted_domain_reasons": [],
    }
    calls = []

    def validate(path, named, targets):
        calls.append((path, named, targets))
        return verdict

    monkeypatch.setattr(closer_resources, "validate_stop", validate)
    return (
        cfg,
        {
            "round_id": round_id,
            "receipt_path": str(receipt),
            "receipt_sha256": native._hash(receipt),
            "run_ids": dict(verdict["run_ids"]),
        },
        verdict,
        calls,
    )


def test_scoped_continuation_revalidates_named_domain_and_never_global_stop(scoped_receipt):
    cfg, candidate, verdict, calls = scoped_receipt
    proof = runtime_binding.scoped_continuation_proof(cfg, candidate)
    assert proof["accepted"] is False and proof["global_stop_valid"] is False
    assert proof["held_targets"] == ["owner/repo#2"]
    assert calls == [
        (
            Path(cfg["dispatch_state"] + ".pipeline.json"),
            candidate["round_id"],
            ["owner/repo#1", "owner/repo#2"],
        )
    ]
    assert runtime_binding.scoped_continuation_proof(cfg, proof) == proof


@pytest.mark.parametrize(
    "change",
    [
        "absent_field",
        "unknown",
        "foreign_run",
        "global_ok",
        "receipt_changed",
        "assignment_changed",
        "live_lane",
        "alias",
    ],
)
def test_scoped_continuation_keeps_unknown_or_changed_domain_held(scoped_receipt, change):
    cfg, candidate, verdict, _ = scoped_receipt
    proof = runtime_binding.scoped_continuation_proof(cfg, candidate)
    if change == "absent_field":
        verdict.pop("admitted_domain_valid")
    elif change == "unknown":
        verdict["admitted_domain_reasons"] = ["unknown_act"]
    elif change == "foreign_run":
        verdict["run_ids"] = {"foreign/repo#3": "c" * 64}
    elif change == "global_ok":
        verdict["global_stop_valid"] = True
    elif change == "receipt_changed":
        Path(candidate["receipt_path"]).write_text("changed")
    elif change == "assignment_changed":
        Path(cfg["assignment"]).write_text("changed")
    elif change == "live_lane":
        (Path(cfg["handoff_dir"]) / ".lane-closer.lock").mkdir(parents=True)
    else:
        proof["receipt_path"] += "/../alias"
    assert runtime_binding.scoped_continuation_proof(cfg, proof) is None


def test_signal_denial_retained_without_alternate_cleanup(host, monkeypatch):
    import errno
    import time

    manifest, path, _ = host
    root = Path(manifest["mechanism_contract"]["observation_root"])
    directory = root / ("e" * 32)
    directory.mkdir()
    calls = []

    class Child:
        pid = 321
        returncode = None

        def kill(self):
            calls.append("signal")
            raise PermissionError(errno.EPERM, "fixture denial")

        def poll(self):
            return self.returncode

        def communicate(self, **_kwargs):
            calls.append("drain")
            self.returncode = 78
            return "", ""

    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_k: Child())
    monkeypatch.setattr(mechanisms, "_identity", lambda *_a, **_k: {})
    monkeypatch.setattr(
        native, "_wait", lambda *_a: {"pid": 321, "binding": native.binding(manifest)}
    )
    request = {
        "nonce": "e" * 32,
        "binding": native.binding(manifest),
        "action_deadline": time.monotonic() + 5,
        "hard_deadline": time.monotonic() + 10,
    }
    with pytest.raises(PermissionError):
        native._qualification_lifecycle(manifest, path, request, directory)
    assert calls == ["signal", "drain"]
    denial = native._read(directory / "denial.json")
    assert denial["errno"] == errno.EPERM and denial["type"] == "PermissionError"


def test_native_preview_cannot_overwrite_host_wrapper(host, monkeypatch):
    import importlib.util

    manifest, path, wrapper = host
    script = Path(__file__).parents[1] / "scripts/render_closer_native_entry.py"
    spec = importlib.util.spec_from_file_location("native_preview", script)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    before = wrapper.read_bytes()
    with pytest.raises(SystemExit):
        renderer.main(
            [
                "--source",
                str(wrapper),
                "--manifest",
                str(path),
                "--campaign-root",
                manifest["mechanism_contract"]["campaign_root"],
                "--out",
                str(wrapper),
            ]
        )
    assert wrapper.read_bytes() == before


@pytest.mark.parametrize(
    "failure",
    [None, "not_started", "timeout", "denied", "arbitrary_c", "cleanup_denied", "missing_api"],
)
def test_round_continuation_needs_terminal_b_and_exact_known_c_boundary(scoped_receipt, failure):
    import json
    import subprocess
    from types import SimpleNamespace

    cfg, candidate, verdict, _ = scoped_receipt
    cfg.update(budget_seconds=100, receipt_reserve_seconds=15)
    hooks = object.__new__(runtime_binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "unused")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks._dependencies_match = lambda: True
    hooks.prerun = lambda _deadline: (0, "exact-token")
    hooks.owns = lambda token: token == "exact-token"
    events = []

    def postrun(token, _deadline):
        events.append(("C", token))
        hooks.postrun_result = {
            "code": 65,
            "dependencies_valid": True,
            "stderr": "handoff-postrun: closer stop rejected; preserve ledger and resume actionable work",
        }
        if failure == "arbitrary_c":
            hooks.postrun_result["stderr"] = "unrelated failure"
        if failure == "cleanup_denied":
            hooks.postrun_result[
                "stderr"
            ] += "\nhandoff-lease: release refused rc=65; lane retained"
        return 65

    hooks.postrun = postrun

    def runner(command, **kwargs):
        events.append("B")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "denied":
            raise PermissionError("fixture denied")
        return SimpleNamespace(returncode=78, stdout=json.dumps(candidate))

    if failure == "missing_api":
        verdict.pop("admitted_domain_valid")
    if failure == "not_started":
        verdict.update(admitted_domain_valid=False, not_started_domain_valid=True, run_ids={})
        candidate["run_ids"] = {}
    result, detail = runtime_binding.run_native_round(
        [sys.executable, "dispatcher.py", "--assignment", cfg["assignment"]],
        cfg,
        runner=runner,
        hooks=hooks,
    )
    assert events == ["B", ("C", "exact-token")]
    if failure == "not_started":
        assert result == "unavailable"
        assert json.loads(detail)["proof"]["domain_outcome"] == "not_started"
    elif failure is None:
        assert result == "scoped_continuation"
        receipt = json.loads(detail)
        assert receipt["action_b_exit"] == 78 and receipt["action_c_exit"] == 65
        assert receipt["proof"]["accepted"] is False
    else:
        assert result != "scoped_continuation"
        assert "C65" in detail


def test_campaign_scoped_continuation_is_pending_not_accepted_and_revalidates(
    scoped_receipt, tmp_path
):
    import json
    from types import SimpleNamespace

    cfg, candidate, verdict, _ = scoped_receipt
    proof = runtime_binding.scoped_continuation_proof(cfg, candidate)
    state = tmp_path / "campaign.json"
    worker = {
        "worker_id": "abcdef012345",
        "state": "active",
        "target": campaign.ROUND_UNIT,
        "claim_target": "campaign:round",
    }
    doc = {"workers": [worker], "targets": {campaign.ROUND_UNIT: "running"}, "worker_config": cfg}

    def update(mutate):
        mutate(doc)
        return doc

    obj = SimpleNamespace(path=state, update=update)
    campaign.write_result(
        campaign.result_path(state, worker["worker_id"]),
        worker["worker_id"],
        campaign.ROUND_UNIT,
        "scoped_continuation",
        json.dumps({"proof": proof}),
        mechanism=mechanisms.NATIVE,
    )
    campaign.reconcile(obj, release_claim=lambda *_: None)
    assert doc["targets"][campaign.ROUND_UNIT] == "pending"
    assert worker["outcome"] == "scoped_continuation"
    worker["state"] = "active"
    verdict["admitted_domain_valid"] = False
    campaign.reconcile(obj, release_claim=lambda *_: None)
    assert doc["targets"][campaign.ROUND_UNIT] == "reconcile"
    assert worker["outcome"] == "unknown"


def test_native_qualification_cannot_authorize_direct_cli_provider(host, monkeypatch):
    import closer_agent_launchers as launchers
    import execution_profiles

    manifest, path, _ = host
    monkeypatch.setattr(launchers, "preflight_runtime", lambda *_a, **_k: (manifest, []))
    profile = execution_profiles.default_codex_profile("offload", "mid")
    plan, problems = launchers.plan_cli_run(
        "codex", profile, "no provider", cwd=path.parent, manifest_path=path, root=path.parent
    )
    assert plan is None
    assert problems == [
        "native host capability requires its observed wrapper entry, not CLI offload"
    ]


def test_normal_entry_cannot_replace_active_supervisor_intent(host, monkeypatch):
    import fcntl

    manifest, path, _ = host
    root = Path(manifest["mechanism_contract"]["observation_root"])
    original = {"nonce": "f" * 32, "status": "submitted"}
    native._write(root / "native-active.json", original)
    monkeypatch.setattr(native, "_native_entry", lambda *_a: pytest.fail("entry must remain held"))
    with (root / "native-supervisor.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(mechanisms.MechanismError, match="already active"):
            native.native_entry(manifest, path, manifest["mechanism_contract"]["campaign_root"])
    assert native._read(root / "native-active.json") == original


def test_supervisor_relative_manifest_matches_canonical_wrapper_entry(host, monkeypatch):
    manifest, path, _ = host
    monkeypatch.chdir(path.parent)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no launch"))
    # Reaching the budget check proves the canonical wrapper block matched.
    with pytest.raises(mechanisms.MechanismError, match="budget must be"):
        native.supervise_native(manifest, Path(path.name), budget_seconds=0)


def test_preview_uses_pinned_launcher_and_interpreter_not_runtime_alias(host, tmp_path):
    import importlib.util
    import json

    manifest, path, wrapper = host
    alias = tmp_path / "module-alias"
    alias.symlink_to(Path(manifest["module_path"]).parent, target_is_directory=True)
    manifest["launcher"]["path"] = str(alias / "closer_launcher.py")
    runtime_alias = tmp_path / "runtime-python"
    runtime_alias.symlink_to(manifest["mechanism_contract"]["interpreter"]["path"])
    manifest["runtime"]["python"] = str(runtime_alias)
    path.write_text(json.dumps(manifest))
    raw = wrapper.read_text()
    source = tmp_path / "original-wrapper.sh"
    source.write_text(
        raw[: raw.index(native.MARKER)]
        + raw[raw.index(native.END_MARKER) + len(native.END_MARKER) :].lstrip("\n")
    )
    output = tmp_path / "preview.sh"
    script = Path(__file__).parents[1] / "scripts/render_closer_native_entry.py"
    spec = importlib.util.spec_from_file_location("native_preview_pins", script)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    assert (
        renderer.main(
            [
                "--source",
                str(source),
                "--manifest",
                str(path),
                "--campaign-root",
                manifest["mechanism_contract"]["campaign_root"],
                "--out",
                str(output),
            ]
        )
        == 0
    )
    expected = native.wrapper_block(
        path.resolve(),
        manifest["mechanism_contract"]["campaign_root"],
        manifest["mechanism_contract"]["interpreter"]["path"],
        manifest["launcher"]["path"],
    )
    assert output.read_text().count(expected) == 1
    assert str(alias / "closer_launcher.py") in output.read_text()
    assert str(runtime_alias) not in output.read_text()
