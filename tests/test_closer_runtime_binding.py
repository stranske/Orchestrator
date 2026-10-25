"""Offline production boundary tests: real Core bytes/store; no native queue run."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_campaign as cc
import closer_dispatcher as cd
import closer_runtime_binding as rb
import env_prereq


@pytest.fixture
def policy(tmp_path, monkeypatch):
    toml = tmp_path / "worker.toml"
    toml.write_text('status = "ACTIVE"\n')
    monkeypatch.setattr(rb, "_POLICY_READERS", {})
    rb.register_policy_reader(
        "native",
        lambda: {
            "source": "controller-app-readback",
            "observed_at": time.time(),
            "worker_toml_digests": {str(toml): rb._hash(toml)},
            "controller_status": "ACTIVE",
            "worker_statuses": ["ACTIVE"],
        },
    )
    return {"worker_tomls": [str(toml)], "reader_id": "native"}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    # Offline conformance is separate from production dependency qualification.
    monkeypatch.setattr(rb, "dependency_problems", lambda: [])
    # Snapshot every closed owned source/schema before executing; no import from draft.
    from closer_core_support import core_source

    reason = env_prereq.core_sibling_absent()
    if reason:
        pytest.skip(reason)
    source = core_source()
    root = tmp_path / "core"
    for name in rb.CORE_FILES:
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest)
    files = {name: rb._hash(root / name) for name in rb.CORE_FILES}
    descriptor = {
        "schema_version": 1,
        "package_kind": "closer-core",
        "owner": "Workflows",
        "contract": cc.CONTRACT,
        "core_version": "1",
        "entrypoints": {"runtime": "scripts/closer_runtime.py"},
        "source_commit": "a" * 40,
        "files": files,
        "dependencies": rb.CORE_DEPENDENCIES,
    }
    descriptor["package_digest"] = rb._digest(
        [
            "closer-core",
            "Workflows",
            "1",
            cc.CONTRACT,
            "a" * 40,
            sorted(files.items()),
            sorted(rb.CORE_DEPENDENCIES.items()),
        ]
    )
    (root / "closer-core-package.json").write_text(json.dumps(descriptor))
    return rb.CoreBundle(
        {"package_root": str(root), "package_digest": descriptor["package_digest"]}
    )


class Contents:
    """GitHub blob CAS boundary: real Core store wins or conflicts on server SHA."""

    def __init__(self):
        self.rows = {}
        self.writes = 0

    def __call__(self, method, path, body):
        key = path.split("?")[0]
        if method == "GET":
            if path.count("/") == 3:
                return {"default_branch": "main"}
            if "/git/ref/" in path:
                return {"object": {"sha": "a" * 40}}
            if key not in self.rows:
                error = OSError("not found")
                error.status = 404
                raise error
            return self.rows[key]
        assert method == "PUT"
        old = self.rows.get(key)
        if (old or {}).get("sha") != body.get("sha"):
            error = OSError("conflict")
            error.status = 409
            raise error
        self.writes += 1
        self.rows[key] = {
            "sha": hashlib.sha1(str(self.writes).encode()).hexdigest(),
            "encoding": "base64",
            "content": body["content"],
        }
        return {"content": {"sha": self.rows[key]["sha"]}}


def test_production_execute_requires_binding_but_legacy_remains(tmp_path):
    snap = {"revision": "r", "pr_target": "o/r#1"}
    gh = SimpleNamespace(snapshot=lambda target: snap, deadline=time.time() + 60)
    backend = cd.Closer(gh, state_path=tmp_path / "s", apply=True, require_runtime_binding=True)
    backend.select = lambda s: ("post_merge_verify", "yes", True, None)
    calls = []
    backend.prepare_verify = lambda *a: {"mutation_kind": "label", "request_basis": "c" * 64}
    backend.submit_verify = lambda *a: calls.append(a) or {"status": "completed"}
    op = {"owner": "mine", "deadline": time.time() + 30}
    assert backend.execute("o/r#1", "post_merge_verify", snap, op)["status"] == "blocked"
    assert not calls
    backend.require_runtime_binding = False
    assert backend.execute("o/r#1", "post_merge_verify", snap, op)["status"] == "completed"


def test_actual_execute_uses_shared_core_and_retains_unknown_across_heads(bundle, policy, tmp_path):
    binding = rb.RuntimeBinding(bundle, policy, transport=Contents())
    snap = {
        "revision": "r1",
        "head": "a" * 40,
        "review_revision": "review1",
        "pr_target": "Org/Repo#7",
    }
    gh = SimpleNamespace(snapshot=lambda t: snap, deadline=time.time() + 60)
    backend = cd.Closer(gh, state_path=tmp_path / "s", apply=True, runtime_binding=binding)
    backend.select = lambda s: ("post_merge_verify", "yes", True, None)
    calls = []
    backend.prepare_verify = lambda *a: {"mutation_kind": "label", "request_basis": "c" * 64}
    backend.submit_verify = lambda *a: calls.append(a) or {"status": "completed"}
    op = {"owner": "one", "deadline": time.time() + 30}
    assert backend.execute("Org/Repo#7", "post_merge_verify", dict(snap), op)["status"] == "unknown"
    assert binding.adapters["org/repo"].shared
    snap.update(head="b" * 40, revision="r2")
    assert backend.execute("Org/Repo#7", "post_merge_verify", dict(snap), op)["status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("action", ["respond_review", "close_source", "reconcile"])
def test_unsupported_semantics_never_dispatch(bundle, policy, action):
    binding = rb.RuntimeBinding(bundle, policy, transport=Contents())
    calls = []
    result = binding.execute(
        "o/r#1", action, {}, {"deadline": time.time() + 60}, lambda: calls.append(True)
    )
    assert "capability unsupported" in result["reason"] and not calls


def test_core_namespace_no_generic_shadow_and_schema_pin(bundle, tmp_path):
    import sys

    assert bundle.github.LocalOperationStore is bundle.runtime.LocalOperationStore
    assert sys.modules.get("scripts.closer_runtime") is not bundle.runtime
    relocated = tmp_path / "relocated"
    shutil.copytree(bundle.root, relocated)
    pin = {
        "package_root": str(relocated),
        "package_digest": bundle.namespace.removeprefix("_closer_core_"),
    }
    rb.CoreBundle(pin)
    schema = relocated / "fixtures/closer-operation/v1/operation.schema.json"
    schema.write_text("{}")
    with pytest.raises(rb.BindingError, match="digest mismatch"):
        rb.CoreBundle(pin)


def test_policy_paused_missing_stale_and_active_controller_cannot_override(policy):
    assert rb.policy_problem(policy) is None
    assert rb.policy_problem({})
    assert "unsupported" in rb.policy_problem({**policy, "reader_id": "missing"})
    Path(policy["worker_tomls"][0]).write_text('status = "PAUSED"\n')
    assert "paused" in rb.policy_problem(policy)


def test_stale_policy_reader_holds(policy):
    rb._POLICY_READERS["native"] = lambda: {"observed_at": time.time() - 301}
    assert "stale" in rb.policy_problem(policy)


def test_native_markers_exact_and_item_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    doc = {
        "campaign_id": "c",
        "targets": {cc.ROUND_UNIT: "pending"},
        "assignment_targets": ["o/r#1"],
        "worker_config": {"dispatch_state": str(tmp_path / "s")},
    }
    assert cc.native_barrier(doc, active={"x-closer": {}, "o/r#1": {"agent": "other"}}) is None
    lane = tmp_path / ".lane-closer.lock"
    lane.mkdir()
    (lane / "meta").write_text("1 1 closer codex\n")
    assert "lane owned" in cc.native_barrier(doc)
    doc["worker_config"]["native_lane_token"] = (lane / "meta").read_text()
    assert cc.native_barrier(doc, active={}) is None
    runlock = tmp_path / "s.pipeline.json.runlock"
    with runlock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        assert "coordinator" in cc.native_barrier(doc)
    assert cc.native_barrier(doc, active={}) is None


def test_forged_desktop_receipt_is_held():
    manifest = {
        "schema_version": 1,
        "contract": cc.CONTRACT,
        "candidate_sha": "a" * 40,
        "package_digest": "b" * 64,
        "state_versions": {"campaign": 1},
    }
    receipt = {
        "kind": "desktop-runner",
        "runner_id": "desktop",
        **manifest,
        "phases": {p: {"passed": True, "evidence": "trust me"} for p in cc.RUNNER_PHASES},
    }
    assert not cc.check_promotion(manifest, ["desktop"], [receipt])["promotable"]
    assert rb.DESKTOP_UNSUPPORTED in str(cc.check_promotion(manifest, ["desktop"], [receipt]))


def test_registered_observer_requires_hash_bound_native_phases(tmp_path):
    manifest = {"package_digest": "b" * 64, "state_versions": {"campaign": 1}}
    request = rb.qualification_request(manifest, ["desktop"])
    observed = {
        **request["binding"],
        "enabled_runners": ["desktop"],
        "runner_id": "desktop",
        "source": "native-desktop-events",
        "app_id": "app",
        "run_id": "run",
        "thread_id": "thread",
        "producer_id": "authorized-controller",
        "phases": {},
    }
    for phase in cc.RUNNER_PHASES:
        path = tmp_path / phase
        path.write_text(phase)
        observed["phases"][phase] = {
            "passed": True,
            "native_event_id": phase,
            "evidence": {"path": str(path), "sha256": rb._hash(path)},
        }
    receipt = {**observed, "request_id": request["request_id"]}
    reader = SimpleNamespace(observe=lambda request_id, runner_id: observed)
    assert rb.desktop_problems(manifest, receipt, reader) == []
    (tmp_path / "startup").write_text("tampered")
    assert rb.desktop_problems(manifest, receipt, reader)


@pytest.mark.parametrize(
    "code,timeout,successor",
    [(0, False, False), (65, False, False), (65, True, False), (65, False, True)],
)
def test_native_round_a_c_once_timeout_and_successor_lease(code, timeout, successor):
    import subprocess

    events = []

    class Hooks:
        def prerun(self, deadline):
            events.append("A")
            return 0, "old-token"

        def owns(self, token):
            return not successor

        def postrun(self, token, deadline):
            events.append("C-attempt")
            # Token-aware helper never removes a successor lease.
            if successor:
                assert token != "successor-token"
                return 65
            events.append("own-cleanup")
            return code

    def runner(command, **kwargs):
        events.append("B")
        assert 0 < kwargs["timeout"] <= 300
        if timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0)

    outcome, detail = rb.run_native_round(
        ["offline"], {"budget_seconds": 600}, runner=runner, hooks=Hooks()
    )
    assert events.count("A") == events.count("C-attempt") == 1
    assert events.count("B") == (0 if successor else 1)
    assert events.count("own-cleanup") == (0 if successor else 1)
    assert outcome == ("ok" if code == 0 and not timeout else "unknown")
    if code == 65:
        assert "C65" in detail and "no_matching_pipeline_run" in detail


def test_missing_token_safe_native_adapter_holds_before_a_b_c():
    result = rb.run_native_round(
        ["must-not-run"], {}, runner=lambda *a, **k: pytest.fail("native run")
    )
    assert result[0] == "unavailable" and "exact-token" in result[1]


def test_disjoint_target_advances_while_other_target_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(rb, "policy_problem", lambda p: None)
    campaign = cc.Campaign(tmp_path / "c.campaign.json")
    campaign.begin(
        "c",
        ["o/r#1", "o/r#2", "o/r#3"],
        owner="root",
        release_pin="a" * 64,
        worker_config={"granularity": "target-delegate"},
    )
    campaign.update(lambda d: d["targets"].update({"o/r#1": "reconcile"}))
    assert cc.claims.claim("o/r#2", "other")
    launched = []
    result = cc.step(
        campaign,
        worker_command=lambda t: [t],
        launcher=lambda argv, log: launched.append(argv) or 1,
    )
    assert result["target"] == "o/r#3" and len(launched) == 1


def test_real_packager_relocates_actual_runtime_core_and_launcher(bundle, tmp_path):
    import importlib.util
    import os
    import subprocess
    import sys

    # Use the independent Core packager's REAL API from an immutable copied file.
    from closer_core_support import core_source

    import closer_distribution_package as package

    reason = env_prereq.core_sibling_absent()
    if reason:
        pytest.skip(reason)
    source = core_source() / "scripts/closer_core_package.py"
    if not source.is_file():
        pytest.skip(
            f"{env_prereq.PREREQ_ABSENT_MARK} independent Core packager source unavailable "
            f"at {source}"
        )
    copied = tmp_path / "core_packager.py"
    shutil.copyfile(source, copied)
    loader = importlib.util.spec_from_file_location("_core_packager_test", copied)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    core_public = module.build_public_descriptor(bundle.root, source_commit="a" * 40)
    core_moved = tmp_path / "core-moved"
    module.materialize_package_tree(bundle.root, core_moved, core_public)
    assert module.verify_installed_tree(core_moved, core_public) == []
    core_pin = {
        "owner": "Workflows",
        "contract": cc.CONTRACT,
        "core_version": "1",
        "entrypoints": {"runtime": "scripts/closer_runtime.py"},
        "package_digest": core_public["package_digest"],
    }
    public = package.build_public_runtime_descriptor(
        Path(cc.__file__).parent,
        candidate_sha="b" * 40,
        state_versions={"campaign": 1, "release": 1},
        supported_core=core_pin,
    )
    assert str(Path.home()) not in json.dumps(public) and str(tmp_path) not in json.dumps(public)
    runtime = tmp_path / "runtime-original"
    package.materialize_runtime_tree(Path(cc.__file__).parent, runtime, public)
    moved = tmp_path / "runtime-moved"
    shutil.move(runtime, moved)
    host = package.host_binding_from_public(
        public,
        install_root=moved,
        core_binding={**core_pin, "package_root": str(core_moved)},
        python=sys.executable,
    )
    manifest_path = moved / "host-manifest.json"
    package.write_host_manifest(manifest_path, host)
    env = {**os.environ, "PYTHONPATH": "", "HANDOFF_DIR": str(tmp_path / "handoff")}
    # Actual Core CLI and actual Runtime/Launcher entrypoints. No helper scripts echo success.
    core = subprocess.run(
        [sys.executable, str(core_moved / "scripts/closer_runtime.py"), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert core.returncode == 0, core.stderr
    run = subprocess.run(
        [
            sys.executable,
            str(moved / "closer_launcher.py"),
            "run",
            "--manifest",
            str(manifest_path),
            "--root",
            str(tmp_path / "campaigns"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "held" in run.stdout and "policy missing" in run.stdout
    # Packaging success does not grant desktop qualification or remote execution.
    assert not cc.check_promotion(host, ["desktop"], [])["promotable"]
    (moved / "claims.py").write_text("# changed")
    refused = subprocess.run(
        [
            sys.executable,
            str(moved / "closer_launcher.py"),
            "run",
            "--manifest",
            str(manifest_path),
            "--root",
            str(tmp_path / "campaigns"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert refused.returncode == 78


def test_separate_production_bindings_share_canonical_owner_and_denials(
    bundle, policy, monkeypatch
):
    monkeypatch.setattr(bundle.runtime, "fetch_live_pr_labels", lambda spec: [])
    transport = Contents()
    one = rb.RuntimeBinding(bundle, policy, transport=transport)
    two = rb.RuntimeBinding(bundle, policy, transport=transport)
    snap = {"head": "a" * 40, "review_revision": "r1", "revision": "v1"}
    calls = []

    def denied():
        calls.append("first")
        raise PermissionError("native permission denied")

    assert (
        one.execute(
            "Org/Repo#7", "merge", snap, {"owner": "one", "deadline": time.time() + 60}, denied
        )["status"]
        == "blocked"
    )
    snap.update(head="b" * 40, revision="v2")
    result = two.execute(
        "org/repo#7",
        "merge",
        snap,
        {"owner": "two", "deadline": time.time() + 60},
        lambda: calls.append("second"),
    )
    assert "denied" in result["reason"] and calls == ["first"]


def test_real_merge_caller_keeps_native_guard_and_ac_hardblock(
    bundle, policy, tmp_path, monkeypatch
):
    import subprocess

    monkeypatch.setattr(bundle.runtime, "fetch_live_pr_labels", lambda spec: [])
    binding = rb.RuntimeBinding(bundle, policy, transport=Contents())
    snap = {"head": "a" * 40, "review_revision": "r1", "revision": "v1"}
    gh = SimpleNamespace(snapshot=lambda t: snap, deadline=time.time() + 60)
    calls = []

    guard_result = {"merge_executed": False, "blocked": True, "reason": "runtime AC gate blocked"}

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(guard_result))

    backend = cd.Closer(
        gh, state_path=tmp_path / "s", apply=True, runtime_binding=binding, run_fn=run
    )
    backend.select = lambda s: ("merge", "eligible", True, None)
    monkeypatch.setattr(cd, "acceptance_ok", lambda *a: False)
    op = {"owner": "one", "deadline": time.time() + 20}
    result = backend.execute("o/r#1", "merge", snap, op)
    assert result["native_result"]["status"] == "blocked"
    assert result["native_result"]["result"]["reason"] == "runtime AC gate blocked"
    assert len(calls) == 1
    # Final acceptance is not a premerge gate; the real native guard still is.
    # A different exact target can proceed only through that same exact-head guard.
    guard_result.update(merge_executed=True, blocked=False, reason=None)
    assert backend.execute("o/r#2", "merge", snap, op)["status"] == "unknown"
    # Native success without independent readback remains fenced.
    assert Path(calls[0][1]).name == "merge_guard.py"
    assert calls[0][3:6] == ["--expected-head", "a" * 40, "--confirm-merge"]
    assert calls[0][6:] == ["--skip-immediate-advisory"]


def test_remote_outage_and_local_store_never_confer_effect_authority(bundle, policy, monkeypatch):
    def offline(*args):
        raise ConnectionRefusedError("offline")

    binding = rb.RuntimeBinding(bundle, policy, transport=offline)
    snap = {"head": "a" * 40, "review_revision": "r1", "revision": "v1"}

    def effect():
        pytest.fail("unfenced effect")

    assert (
        binding.execute(
            "o/r#1", "merge", snap, {"owner": "one", "deadline": time.time() + 60}, effect
        )["status"]
        == "blocked"
    )
    binding.adapters["o/r"] = SimpleNamespace(shared=False)
    assert (
        "local Core store"
        in binding.execute(
            "o/r#1", "merge", snap, {"owner": "one", "deadline": time.time() + 60}, effect
        )["reason"]
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    binding = rb.RuntimeBinding(bundle, policy)
    assert (
        "env token"
        in binding.execute(
            "o/r#1", "merge", snap, {"owner": "one", "deadline": time.time() + 60}, effect
        )["reason"]
    )


def test_owned_core_helper_imports_do_not_shadow_runtime(bundle):
    import sys

    before = list(sys.path)
    helper = bundle.load("check_checks_reported")
    assert helper.GITHUB_API is bundle.load("api_client").GITHUB_API
    assert sys.path == before
    assert sys.modules.get("api_client") is not bundle.load("api_client")


def test_runtime_entrypoint_outside_pinned_set_is_rejected(tmp_path):
    import closer_launcher

    manifest = {"module_dir": str(tmp_path), "module_path": str(tmp_path / "arbitrary.py")}
    assert "runtime entrypoint is outside" in str(closer_launcher.pin_problems(manifest))


def test_production_native_script_adapter_uses_pins_and_exact_token_without_running_helpers(
    tmp_path,
):
    import subprocess

    scripts = {}
    for phase in ("prerun", "postrun"):
        path = tmp_path / phase
        path.write_text(
            "# offline artifact for pinned adapter protocol test\n# HANDOFF_EXACT_TOKEN_V1\n"
        )
        scripts[phase] = {"path": str(path), "sha256": rb._hash(path)}
    module = Path(rb.__file__).with_name("closer_native_lease.py")
    scripts["lease_module"] = {"path": str(module), "sha256": rb._hash(module)}
    cfg = {
        "native_hooks": scripts,
        "handoff_agent": "codex",
        "handoff_dir": str(tmp_path / "handoff"),
        "dispatch_state": str(tmp_path / "state"),
        "budget_seconds": 600,
    }
    lane = tmp_path / "handoff/.lane-closer.lock"
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == scripts["prerun"]["path"]:
            lane.mkdir(parents=True)
            (lane / "meta").write_text("original-token\n")
        elif argv[0] == scripts["postrun"]["path"]:
            assert kwargs["env"]["HANDOFF_EXPECTED_LANE_TOKEN"] == "original-token\n"
            (lane / "meta").unlink()
            lane.rmdir()
        else:
            assert argv == ["offline-dispatcher"]
        return subprocess.CompletedProcess(argv, 0)

    assert rb.run_native_round(["offline-dispatcher"], cfg, runner=runner)[0] == "ok"
    assert calls == [scripts["prerun"]["path"], "offline-dispatcher", scripts["postrun"]["path"]]
    Path(scripts["postrun"]["path"]).write_text("# changed unsafe helper")
    assert rb.run_native_round(["never"], cfg, runner=runner)[0] == "unavailable"
    assert len(calls) == 3


def test_declared_core_dependency_mismatch_holds_production(monkeypatch):
    from importlib import metadata

    versions = {"jsonschema": "4.21.0", "PyYAML": "6.0.3", "requests": "2.34.2"}
    monkeypatch.setattr(metadata, "version", lambda name: versions[name])
    assert rb.dependency_problems() == [
        "Core dependency unsupported: jsonschema 4.21.0; requires >=4.22.0"
    ]
