"""Named C uses terminal B's exact immutable scoped receipt; never latest state."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_closer_bounded_native_hooks import canonical_postrun, renderer
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_capacity import completed_standing_round
from test_closer_native_observer import host as host

import closer_native_lease as lease
import closer_resources as resources
import closer_runtime_binding as binding


def fresh_native_cfg(fixture):
    from test_closer_native_capacity import routed_report

    import closer_campaign as campaign
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    manifest, path, obj, *_ = fixture
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
    context = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["context"]
    return native.admission_config(manifest, obj.read()["worker_config"], context), context


def fresh_scoped_round(cfg, *, offset=0.1):
    import time
    from collections import Counter

    import test_closer_quarantine_integration as fixture

    return fixture.pipeline.run_pipeline(
        Path(str(cfg["dispatch_state"]) + ".pipeline.json"),
        ["offline/repo#1"],
        **fixture.callbacks(Counter()),
        resource_keys=lambda target, _ctx: fixture.inventory(target),
        coordinator_mode="resource_scoped",
        deadline=time.time() + 15,
        max_io_workers=2,
        max_low_workers=1,
        max_exception_workers=1,
        capacity_fn=lambda: {"io": 2, "low": 1, "exception": 1},
        now=lambda: time.time() + offset,
    )


@pytest.mark.parametrize(
    "mutation",
    ["none", "missing", "mode", "path", "hash", "run_ids", "assignment", "round_id", "exit"],
)
def test_terminal_binding_requires_actual_named_receipt(capacity_host, mutation):
    cfg, _context, _proof, aggregate, _result = completed_standing_round(capacity_host)
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.begin_terminal_receipt(cfg)
    aggregate = fresh_scoped_round(cfg)
    candidate = dict(aggregate)
    code = 0
    if mutation == "missing":
        candidate = {}
    elif mutation == "mode":
        candidate["mode"] = "legacy"
    elif mutation == "path":
        candidate["receipt_path"] += ".foreign"
    elif mutation == "hash":
        candidate["receipt_sha256"] = "f" * 64
    elif mutation == "run_ids":
        candidate["run_ids"] = {}
    elif mutation == "assignment":
        candidate["assignment"] = ["foreign/repo#1"]
    elif mutation == "round_id":
        candidate["round_id"] = "f" * 32
    elif mutation == "exit":
        code = 1
    hooks.env = {"CLOSER_POSTRUN_ROUND_ID": "inherited-stale-id"}
    assert hooks.set_terminal_receipt(
        subprocess.CompletedProcess([], code, stdout=json.dumps(candidate)), cfg
    ) is (mutation == "none")
    if mutation == "none":
        assert hooks.env["CLOSER_POSTRUN_ROUND_ID"] == aggregate["round_id"]
    else:
        assert "CLOSER_POSTRUN_ROUND_ID" not in hooks.env
        assert hooks.terminal_receipt_problem["reason"]


@pytest.mark.parametrize("mode", ["valid", "missing", "malformed", "foreign"])
def test_rendered_c_validates_named_real_scoped_round_and_cleans_once(
    capacity_host, tmp_path, mode
):
    cfg, _context, _proof, aggregate, _result = completed_standing_round(capacity_host)
    pipeline = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    assert (
        resources.validate_stop(pipeline, aggregate["round_id"], ["offline/repo#1"])[
            "global_stop_valid"
        ]
        is True
    )
    root = tmp_path / "private-lease"
    root.mkdir()
    lane = root / ".lane-closer.lock"
    lease.acquire(root, "closer", "codex", os.getpid())
    token = (lane / "meta").read_text()
    events = tmp_path / "events"
    fake = tmp_path / "fake-handoff"
    fake.write_text('#!/bin/bash\necho end-round >> "$EVENTS"\n')
    fake.chmod(0o700)
    validator = tmp_path / "named-validator.py"
    validator.write_text(
        "import argparse,json,os\nfrom pathlib import Path\n"
        "import closer_resources as r\nfrom closer_dispatcher import targets_from_assignment\n"
        "p=argparse.ArgumentParser();p.add_argument('verb');p.add_argument('--state');"
        "p.add_argument('--assignment');p.add_argument('--round-id',required=True);a=p.parse_args()\n"
        "v=r.validate_stop(Path(a.state+'.pipeline.json'),a.round_id,targets_from_assignment(Path(a.assignment)))\n"
        "Path(os.environ['EVENTS']).write_text('validate:'+a.round_id+'\\n')\n"
        "raise SystemExit(0 if v.get('global_stop_valid') is True else 65)\n"
    )
    script = tmp_path / "postrun"
    script.write_bytes(renderer.patch_postrun(canonical_postrun().encode(), bounded_round=True))
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(binding.__file__).parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "EVENTS": str(events),
        "FAKE_HANDOFF": str(fake),
        "FAKE_RELAY": "/must-not-launch-relay",
        "HOME": str(tmp_path),
        "HANDOFF_LANE_MUTEX_DIR": str(lane),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_EXPECTED_LANE_TOKEN": token,
        "CLOSER_NATIVE_LEASE_PY": str(Path(lease.__file__)),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "CLOSER_POSTRUN_DEPENDENCIES_VALID": "1",
        "CLOSER_POSTRUN_PYTHON": sys.executable,
        "CLOSER_POSTRUN_DISPATCHER": str(validator),
        "CLOSER_POSTRUN_ASSIGNMENT": cfg["assignment"],
        "CLOSER_POSTRUN_REQUIRE_NAMED_ROUND": "1",
        "CLOSER_NO_NEW_RELAY": "1",
        "CLOSER_ROUND_STATE": cfg["dispatch_state"],
    }
    env.pop("CLOSER_POSTRUN_ROUND_ID", None)
    if mode != "missing":
        env["CLOSER_POSTRUN_ROUND_ID"] = (
            aggregate["round_id"] if mode == "valid" else "f" * 32 if mode == "foreign" else "x;id"
        )
    done = subprocess.run(
        ["/bin/bash", str(script), "closer", "no_op"],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert done.returncode == (0 if mode == "valid" else 65), done.stderr
    assert not lane.exists()
    rows = events.read_text().splitlines()
    assert rows.count("hygiene") == 1
    if mode == "valid":
        assert rows == [
            "validate:" + aggregate["round_id"],
            "end-round",
            "normal-hygiene",
            "hygiene",
        ]
    else:
        assert "end-round" not in rows


@pytest.mark.parametrize("failure", ["timeout", "malformed", "foreign"])
def test_unknown_b_runs_c_once_without_inherited_named_round(capacity_host, failure):
    cfg, _context, _proof, aggregate, _result = completed_standing_round(capacity_host)
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "fixture")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks.env = {"CLOSER_POSTRUN_ROUND_ID": aggregate["round_id"]}
    hooks._dependencies_match = lambda: True
    hooks.prerun = lambda _: (0, "exact-fixture-token")
    hooks.owns = lambda token: token == "exact-fixture-token"
    events = []

    def postrun(token, _deadline):
        events.append(("C", token))
        assert hooks.env["CLOSER_POSTRUN_REQUIRE_NAMED_ROUND"] == "1"
        assert "CLOSER_POSTRUN_ROUND_ID" not in hooks.env
        hooks.postrun_result = {"code": 65, "dependencies_valid": True}
        return 65

    hooks.postrun = postrun

    def runner(argv, **_kwargs):
        events.append(("B", None))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        candidate = dict(aggregate, receipt_path=aggregate["receipt_path"] + ".foreign")
        return subprocess.CompletedProcess(
            argv, 0, stdout="not-json" if failure == "malformed" else json.dumps(candidate)
        )

    command = [sys.executable, "fixture.py", "--assignment", cfg["assignment"]]
    if cfg.get("finite_frontier"):
        command += [
            "--finite-frontier-json",
            json.dumps(cfg["finite_frontier"]),
            "--request-timeout-seconds",
            str(cfg["request_timeout_seconds"]),
        ]
    else:
        command += ["--budget-seconds", "1"]
    outcome, detail = binding.run_native_round(
        command,
        cfg,
        runner=runner,
        hooks=hooks,
        now=lambda: 1000,
    )
    assert events == [("B", None), ("C", "exact-fixture-token")]
    assert outcome == "unknown" and "ACTION C65" in detail


def test_changed_admission_before_b_is_binding_refusal(capacity_host, monkeypatch):
    """Pinned hooks translate typed admission drift before any B submission."""
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    cfg, _context, _proof, _aggregate, _result = completed_standing_round(capacity_host)
    hooks = object.__new__(binding.PinnedNativeHooks)
    original_validate = native.validate_admission
    calls = 0

    def changed_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise mechanisms.MechanismError("inputs changed")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(native, "validate_admission", changed_once)
    with pytest.raises(
        binding.BindingError, match="native admission unavailable before dispatcher: inputs changed"
    ):
        hooks.begin_terminal_receipt(cfg)
    assert calls == 1


def test_old_successful_same_assignment_cannot_complete_new_b(capacity_host):
    cfg, _context, _proof, aggregate, _result = completed_standing_round(capacity_host)
    boundary = Path(_context["assignment"]["path"]).parent / (
        "completed-round-" + aggregate["round_id"] + ".json"
    )
    boundary.unlink()  # Only this fixture's signed boundary; the genuine old receipt remains.
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "fixture")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks._dependencies_match = lambda: True
    hooks.prerun = lambda _: (0, "exact-fixture-token")
    hooks.owns = lambda _: True
    events = []

    def postrun(token, _deadline):
        events.append(token)
        hooks.postrun_result = {"code": 0, "dependencies_valid": True}
        return 0

    hooks.postrun = postrun
    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "1"],
        cfg,
        runner=lambda argv, **_: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(aggregate)),
        hooks=hooks,
        now=lambda: 1000,
    )
    assert outcome == "unknown" and "predates this ACTION B" in detail
    assert events == ["exact-fixture-token"]
    assert "CLOSER_POSTRUN_ROUND_ID" not in hooks.env


def test_receipt_watermark_is_immutable_and_cannot_restart_submission(capacity_host):
    cfg, context, _proof, aggregate, _result = completed_standing_round(capacity_host)
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.begin_terminal_receipt(cfg)
    path = Path(context["assignment"]["path"]).parent / "action-b-receipt-watermark.json"
    original = path.read_bytes()
    record = json.loads(original)["record"]
    assert aggregate["round_id"] in record["prior_round_ids"]
    assert record["admission_nonce"] == context["nonce"]
    assert record["assignment_sha256"] == context["assignment"]["sha256"]
    with pytest.raises(FileExistsError):
        hooks.begin_terminal_receipt(cfg)
    assert path.read_bytes() == original
    assert Path(aggregate["receipt_path"]).is_file()


@pytest.mark.parametrize("tamper", [False, True])
def test_receipt_without_intact_pre_b_watermark_is_never_admitted(capacity_host, tamper):
    cfg, _context, _proof, _aggregate, _result = completed_standing_round(capacity_host)
    hooks = object.__new__(binding.PinnedNativeHooks)
    if tamper:
        hooks.begin_terminal_receipt(cfg)
        hooks.terminal_watermark[0].write_text("{}")
    fresh = fresh_scoped_round(cfg)
    assert not hooks.set_terminal_receipt(
        subprocess.CompletedProcess([], 0, stdout=json.dumps(fresh)), cfg
    )
    assert "watermark unavailable" in hooks.terminal_receipt_problem["reason"]
    assert "CLOSER_POSTRUN_ROUND_ID" not in hooks.env
