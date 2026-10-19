"""Executing paths of closer_campaign: fence, worker, scheduled resume, release, pin, harness."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import install_closer_campaign_hook as hook
from test_closer_campaign import CAND, DIGEST, journal

import closer_campaign as cc

SRC = Path(cc.__file__).resolve().parent


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "0")
    monkeypatch.setattr("closer_runtime_binding.policy_problem", lambda policy: None)
    monkeypatch.setattr(
        "closer_runtime_binding.run_native_round",
        lambda command, cfg, runner: cc.run_dispatcher(
            command, timeout=cfg.get("worker_timeout", 120), apply=True, runner=runner
        ),
    )


class Port:
    def __init__(self, outcome=None, exc=None):
        self.outcome, self.exc, self.calls = outcome, exc, 0

    def submit(self, key, action):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.outcome


# ---- dispatcher adapter and worker


def fake_run(code=0, exc=None):
    calls = []

    def run(command, **kw):
        calls.append(command)
        if exc:
            raise exc
        return subprocess.CompletedProcess(command, code, b"", b"")

    run.calls = calls
    return run


@pytest.mark.parametrize(
    "kwargs,apply,outcome",
    [
        ({"code": 0}, True, "ok"),
        ({"code": -9}, True, "unknown"),
        ({"code": 77}, True, "denied"),
        ({"code": 3}, True, "unknown"),
        ({"code": 3}, False, "unavailable"),
        ({"exc": subprocess.TimeoutExpired("x", 1)}, True, "unknown"),
    ],
)
def test_dispatcher_outcomes_never_call_timeout_a_safe_retry(kwargs, apply, outcome):
    got, _ = cc.run_dispatcher(["x"], timeout=1, apply=apply, runner=fake_run(**kwargs))
    assert got == outcome


def started(tmp_path, cfg=None):
    c = cc.Campaign(tmp_path / "c.campaign.json")
    c.begin(
        "camp",
        ["t1"],
        owner="o",
        release_pin=DIGEST,
        worker_config=cfg or {"assignment": "a.json", "dispatch_state": "s.json", "apply": True},
    )
    pids = []
    cc.step(
        c,
        worker_command=lambda t: ["w", t],
        launcher=lambda cmd, log: pids.append(1) or os.getpid(),
        pid_alive=lambda p: True,
    )
    doc = c.read()
    return c, doc["workers"][0]["worker_id"]


def test_worker_runs_dispatcher_and_writes_durable_result(tmp_path):
    c, wid = started(tmp_path)
    runner = fake_run(0)
    out = cc.run_worker(
        str(c.path),
        str(cc.result_path(c.path, wid)),
        cc.ROUND_UNIT,
        "o",
        1,
        runner=runner,
        module_dir=SRC,
    )
    assert out == "ok"
    assert runner.calls[0][1].endswith("closer_dispatcher.py") and runner.calls[0][2] == "run"
    assert "--apply" in runner.calls[0]
    doc = cc.reconcile(c, pid_alive=lambda p: False, release_claim=lambda *a: True)
    assert doc["targets"][cc.ROUND_UNIT] == "done"


def test_stale_generation_worker_is_fenced_with_no_dispatch(tmp_path):
    c, wid = started(tmp_path)
    runner = fake_run(0)
    out = cc.run_worker(
        str(c.path),
        str(cc.result_path(c.path, wid)),
        cc.ROUND_UNIT,
        "o",
        99,
        runner=runner,
        module_dir=SRC,
    )
    assert out == "unavailable" and runner.calls == []


def test_timed_out_worker_reconciles_never_safe_retry(tmp_path):
    c, wid = started(tmp_path)
    cc.run_worker(
        str(c.path),
        str(cc.result_path(c.path, wid)),
        cc.ROUND_UNIT,
        "o",
        1,
        runner=fake_run(exc=subprocess.TimeoutExpired("x", 1)),
        module_dir=SRC,
    )
    doc = cc.reconcile(c, pid_alive=lambda p: False, release_claim=lambda *a: True)
    assert doc["targets"][cc.ROUND_UNIT] == "reconcile"
    assert cc.next_action(doc)["kind"] == "reconcile"


def test_takeover_requires_terminal_workers_and_fences_old_generation(tmp_path):
    c, wid = started(tmp_path)
    with pytest.raises(cc.CampaignError):
        c.takeover("o2", pid_alive=lambda p: True)
    assert cc.step(c, worker_command=lambda t: ["w"], owner_generation=7)["kind"] == "fenced"


# ---- release resume


def test_resume_release_cas_attempt_and_unchanged_verifier(tmp_path):
    rec = journal(tmp_path, [("installing", {})])
    snap = rec.read()
    c = cc.Campaign(tmp_path / "c.campaign.json")
    c.begin("camp", [], owner="o", release_pin=DIGEST)
    launched = []

    def launcher(cmd, log):
        launched.append(cmd)
        return 555

    def go(**over):
        exp = {"candidate_sha": CAND, "revision": snap["revision"], "snapshot_digest": DIGEST}
        exp.update(over)
        return cc.resume_release(
            rec.path,
            c,
            expected=exp,
            verifier_digest="v" * 64,
            launcher=launcher,
            release_command=lambda p: ["rw", p],
            pid_alive=lambda p: True,
        )

    assert go(revision=snap["revision"] + 1)["action"] == "refused"
    assert go(snapshot_digest="0" * 64)["action"] == "refused"
    first = go()
    assert first["action"] == "launched_release" and first["plan"] == "resume_install"
    assert go()["action"] == "wait"  # same attempt actively running
    assert len(launched) == 1
    cc.write_result(cc.result_path(c.path, first["attempt_id"]), "x", "release", "ok", "d")
    assert go()["action"] == "skip_unchanged"  # same input: verifier not rerun
    assert len(launched) == 1


def test_resume_release_dead_attempt_reconciles_not_relaunches(tmp_path):
    rec = journal(tmp_path, [("installing", {})])
    c = cc.Campaign(tmp_path / "c.campaign.json")
    c.begin("camp", [], owner="o", release_pin=DIGEST)
    kw = dict(
        expected={"candidate_sha": CAND},
        verifier_digest="v" * 64,
        release_command=lambda p: ["rw"],
        launcher=lambda cmd, log: 9,
    )
    cc.resume_release(rec.path, c, pid_alive=lambda p: True, **kw)
    out = cc.resume_release(rec.path, c, pid_alive=lambda p: False, **kw)
    assert out["action"] == "reconcile"


def test_acceptance_pending_executes_nothing(tmp_path):
    gen = "generation-v1:" + "d" * 64
    rec = journal(
        tmp_path,
        [
            ("installing", {}),
            ("installed", {"installed_generation": gen}),
            ("acceptance_pending", {}),
        ],
    )
    c = cc.Campaign(tmp_path / "c.campaign.json")
    c.begin("camp", [], owner="o", release_pin=DIGEST)
    launched = []
    out = cc.resume_release(
        rec.path,
        c,
        expected={"candidate_sha": CAND, "generation": gen},
        verifier_digest="v" * 64,
        launcher=lambda *a: launched.append(a),
        release_command=lambda p: ["rw"],
    )
    assert out["action"] == "await_acceptance" and not launched
    assert out["reverify"] is False and out["reinstall"] is False


def test_release_worker_exception_is_unknown(tmp_path):
    class Rec:
        def __init__(self, path):
            pass

        def verify_and_install(self, **kw):
            raise RuntimeError("died mid-install")

    out = cc.run_release_worker(
        {"record": "r", "args": {}}, str(tmp_path / "a1.json"), release_factory=Rec
    )
    assert out == "unknown"


# ---- manifest, pin, harness (real subprocesses)


def pinned_dir(tmp_path, dispatcher="import sys\nif __name__ == '__main__': sys.exit(0)\n"):
    d = tmp_path / "installed"
    d.mkdir()
    for name in (
        "closer_campaign.py",
        "closer_runtime_binding.py",
        "closer_native_lease.py",
        "closer_launcher.py",
        "closer_release.py",
        "claims.py",
    ):
        shutil.copy(SRC / name, d / name)
    with (d / "closer_runtime_binding.py").open("a") as stream:
        stream.write(
            "\n# Offline subprocess fixture: fake dispatcher only.\npolicy_problem = lambda policy: None\n"
            "def run_native_round(command, cfg, *, runner):\n"
            "    import closer_campaign as cc\n"
            "    return cc.run_dispatcher(command, timeout=cfg.get('worker_timeout', 120), apply=True, runner=runner)\n"
        )
    (d / "closer_dispatcher.py").write_text(dispatcher)
    return d


def manifest_for(d, tmp_path):
    path = tmp_path / "pin.manifest.json"
    m = cc.make_manifest(
        d, candidate_sha=CAND, state_versions={"campaign": 1, "release": 1}, manifest_path=path
    )
    cc.write_manifest(path, m)
    return m, path


def test_manifest_binds_digests_and_detects_tamper(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    assert cc.verify_modules(m) == [] and len(m["package_digest"]) == 64
    (d / "claims.py").write_text("# tampered\n")
    assert cc.verify_modules(m) == ["claims.py: digest differs from manifest"]
    with pytest.raises(cc.CampaignError):
        cc.load_manifest(path)


def test_harness_lifecycle_real_processes_and_receipt_is_bound(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    receipt = cc.produce_harness_receipt(m, path, tmp_path / "work")
    assert cc.check_harness_receipt(m, receipt) == []
    first, second = receipt["processes"]
    assert first["exit_code"] == -9 and second["exit_code"] == 0 and first["pid"] != second["pid"]
    assert receipt["desktop_runtime_validated"] is False
    # never satisfies promotion: the actual desktop runtime is still pending
    verdict = cc.check_promotion(m, ["harness"], [receipt])
    assert not verdict["promotable"] and "desktop-agent runtime validation" in str(verdict)
    # pin selection uses the bound receipt
    assert cc.select_pin([m], None, [receipt])["pin"] == m["package_digest"]


def test_shape_only_self_reported_receipt_is_rejected(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    receipt = cc.produce_harness_receipt(m, path, tmp_path / "work")
    forged = json.loads(json.dumps(receipt))
    forged["phases"]["handoff"]["evidence"] = {"path": str(path), "sha256": "0" * 64}
    forged["processes"][0]["exit_code"] = 0
    forged["runtime_digest"] = "9" * 64
    problems = cc.check_harness_receipt(m, forged)
    assert len(problems) >= 3
    assert cc.select_pin([m], None, [forged])["pin"] is None


def test_running_campaign_keeps_its_pin(tmp_path):
    d = pinned_dir(tmp_path)
    m, _ = manifest_for(d, tmp_path)
    doc = {"phase": "work", "release_pin": m["package_digest"]}
    assert cc.select_pin([m], doc, [])["pin"] == m["package_digest"]
    assert cc.select_pin([], doc, [])["pin"] is None


# ---- scheduled second process resumption (real processes)


def fresh_capacity(path, ok=True):
    path.write_text(
        json.dumps({"generated_at": time.time(), "agents": {"claude": {"available": ok}}})
    )


TICK_PROGRAM = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import closer_campaign as cc\n"
    "import closer_runtime_binding as binding\n"
    "binding.policy_problem = lambda policy: None\n"
    "def route(cap):\n"
    "    ok = json.load(open(cap))['agents']['claude']['available']\n"
    "    return {'agent': 'claude', 'profile_id': 'p'} if ok else {'hold': 'x'}\n"
    "print(json.dumps(cc.tick(sys.argv[2], sys.argv[2] + '/pin.manifest.json',\n"
    "    capacity_json=sys.argv[3], route_fn=route, qualifier=lambda m, r: []), sort_keys=True))\n"
)


def run_tick(module, root, env, capacity=None):
    """A separate process per tick (qualifier and router injected: no real receipt exists)."""
    cmd = [sys.executable, "-c", TICK_PROGRAM, str(Path(module).parent), str(root), str(capacity)]
    done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_second_scheduled_process_resumes_after_capacity_failure_and_worker_death(tmp_path):
    d = pinned_dir(tmp_path, dispatcher="import time\ntime.sleep(60)\n")
    root = tmp_path / "campaigns"
    root.mkdir()
    m, path = manifest_for(d, tmp_path)
    shutil.copy(path, root / "pin.manifest.json")
    env = {**os.environ, "HANDOFF_DIR": str(tmp_path / "handoff"), "PYTHONPATH": ""}
    cap = tmp_path / "cap.json"
    camp = cc.Campaign(root / "one.campaign.json")
    camp.begin(
        "camp",
        ["t1"],
        owner="o",
        release_pin=m["package_digest"],
        worker_config={
            "assignment": "a",
            "dispatch_state": "s",
            "apply": True,
            "worker_timeout": 120,
        },
    )
    module = d / "closer_campaign.py"
    fresh_capacity(cap, ok=False)
    first = run_tick(module, root, env, cap)  # process 1: capacity exhausted
    assert first[-1]["reason"] == "capacity_exhausted"
    fresh_capacity(cap, ok=True)
    second = run_tick(module, root, env, cap)  # process 2: discovers state, launches
    assert second[0]["kind"] == "launched"
    pid = second[0]["pid"]
    try:
        for _ in range(50):  # wait for the worker to be running its dispatcher
            time.sleep(0.1)
        os.killpg(pid, signal.SIGKILL)  # worker dies with the write in flight
        time.sleep(0.3)
        third = run_tick(module, root, env, cap)  # process 3: must reconcile, not relaunch
        assert third[0]["kind"] == "reconcile"
        assert camp.read()["targets"][cc.ROUND_UNIT] == "reconcile"
        assert len(camp.read()["workers"]) == 1
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_scheduled_tick_completes_target_with_real_worker(tmp_path):
    d = pinned_dir(tmp_path)
    root = tmp_path / "campaigns"
    root.mkdir()
    m, path = manifest_for(d, tmp_path)
    shutil.copy(path, root / "pin.manifest.json")
    env = {**os.environ, "HANDOFF_DIR": str(tmp_path / "handoff")}
    cap = tmp_path / "cap.json"
    fresh_capacity(cap)
    camp = cc.Campaign(root / "one.campaign.json")
    camp.begin(
        "camp",
        ["t1"],
        owner="o",
        release_pin=m["package_digest"],
        worker_config={"assignment": "a", "dispatch_state": "s", "apply": True},
    )
    module = d / "closer_campaign.py"
    assert run_tick(module, root, env, cap)[0]["kind"] == "launched"
    for _ in range(100):
        time.sleep(0.1)
        if any((tmp_path / "campaigns" / "one.campaign.json.results").glob("*.json")):
            break
    out = run_tick(module, root, env, cap)
    assert camp.read()["targets"][cc.ROUND_UNIT] == "done" and out[-1]["kind"] == "complete"


def test_tick_refuses_newer_state_and_tampered_pin(tmp_path):
    d = pinned_dir(tmp_path)
    root = tmp_path / "campaigns"
    root.mkdir()
    m, path = manifest_for(d, tmp_path)
    (root / "x.campaign.json").write_text(json.dumps({"schema_version": 2}))
    before = (root / "x.campaign.json").read_bytes()
    out = cc.tick(root, path, qualifier=lambda m, r: [])
    assert out[0]["kind"] == "refused" and (root / "x.campaign.json").read_bytes() == before
    (d / "claims.py").write_text("# tampered\n")
    assert cc.tick(root, path)[0]["kind"] == "skipped"


# ---- hook: pinned load, not a plain import


CONTROLLER = (
    "def main(argv: list[str] | None = None) -> int:\n"
    "    import demand_router\n    return demand_router.main(argv)\n"
)


def render_and_load(tmp_path, d, manifest_path):
    out = tmp_path / "controller.py"
    out.write_text(hook.render(CONTROLLER, str(manifest_path)))
    ns: dict = {}
    exec(compile(out.read_text(), str(out), "exec"), ns)
    return ns


def test_hook_loads_pinned_module_and_falls_back_on_digest_mismatch(tmp_path, monkeypatch):
    d = pinned_dir(tmp_path)
    # The hook's known lazy dependencies are part of its declared closure.
    for name in ("router", "execution_profiles", "closer_distribution_package"):
        (d / (name + ".py")).write_text("# pinned lazy fixture\n")
    m, path = manifest_for(d, tmp_path)
    monkeypatch.syspath_prepend(str(d))
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    for filename in m["modules"]:
        name = Path(filename).stem
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)
    ns = render_and_load(tmp_path, d, path)
    loaded = ns["_load_pinned_closer_campaign"]()
    assert loaded is not None and Path(loaded.__file__) == d / "closer_campaign.py"
    for filename in m["modules"]:
        monkeypatch.delitem(sys.modules, Path(filename).stem, raising=False)
    (d / "claims.py").write_text("# tampered\n")
    assert ns["_load_pinned_closer_campaign"]() is None


def test_hook_installer_rejects_unpinned_manifest(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    (d / "closer_release.py").write_text("# tampered\n")
    c = tmp_path / "c.py"
    c.write_text(CONTROLLER)
    with pytest.raises(SystemExit):
        hook.main(
            ["--controller", str(c), "--manifest", str(path), "--out", str(tmp_path / "o.py")]
        )


def test_controller_entry_default_tick_runs_then_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "1")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    seen = []
    assert cc.controller_entry(["x"], lambda a: seen.append(a) or 5) == 5 and seen == [["x"]]
    monkeypatch.setattr(cc, "tick", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    (tmp_path / "closer-campaigns").mkdir()
    (tmp_path / "closer-campaigns" / "pin.manifest.json").write_text("{}")
    assert cc.tick_safe()[0]["kind"] == "error"  # never raises into the controller
    assert cc.controller_entry(["y"], lambda a: 6) == 6


def test_manifest_pinned_tick_command_runs_with_explicit_root(tmp_path):
    directory = pinned_dir(tmp_path)
    root = tmp_path / "empty-campaigns"
    manifest = cc.make_manifest(
        directory, candidate_sha=CAND, state_versions={"campaign": 1}, campaign_root=root
    )
    assert manifest["command"][3:5] == ["--root", str(root.resolve())]
    assert manifest["command"][-2:] == ["--manifest", str(root.resolve() / "pin.manifest.json")]
    assert manifest["command_digest"] == cc.sha256_json(manifest["command"])
    cc.write_manifest(root / "pin.manifest.json", manifest)
    done = subprocess.run(manifest["command"], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report[0]["kind"] == "held"
    assert report[0]["reason"] == "version not qualified"


def test_manifest_binds_arbitrary_output_and_configured_root(tmp_path, monkeypatch):
    directory = pinned_dir(tmp_path)
    root = tmp_path / "configured-campaigns"
    output = tmp_path / "different" / "custom-name.json"
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_DIR", str(root))
    assert (
        cc.main(
            [
                "make-manifest",
                "--module-dir",
                str(directory),
                "--candidate-sha",
                CAND,
                "--state-version",
                "campaign=1",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    manifest = cc.load_manifest(output)
    assert manifest["command"][3:5] == ["--root", str(root.resolve())]
    assert manifest["command"][-2:] == ["--manifest", str(output.resolve())]
    calls = []
    monkeypatch.setattr(
        cc, "tick", lambda root, manifest, **kwargs: calls.append((root, manifest)) or []
    )
    assert cc.main(manifest["command"][2:]) == 0
    assert calls == [(str(root.resolve()), str(output.resolve()))]
    with pytest.raises(cc.CampaignError, match="output differs"):
        cc.write_manifest(tmp_path / "wrong-output.json", manifest)
    assert not (tmp_path / "wrong-output.json").exists()
