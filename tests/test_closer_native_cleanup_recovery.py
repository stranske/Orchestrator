"""Native C65 needs exact cleanup plus positive no-submission evidence, not optimism."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_named_round import fresh_native_cfg
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_native_observer as native
import closer_runtime_binding as binding

TOKEN = "123 456 closer cursor HANDOFF_EXACT_TOKEN_V1:fixture:77\n"
NONCE = "a" * 48


def cleanup(token=TOKEN, lane=None):
    digest = hashlib.sha256(token.encode()).hexdigest()
    return {
        "schema": "closer-native-lease-cleanup/v1",
        "lane": "closer",
        "lane_dir": str(
            lane
            or Path(os.environ.get("HANDOFF_DIR") or Path.home() / ".codex/handoff")
            / ".lane-closer.lock"
        ),
        "expected_token_sha256": digest,
        "meta_sha256": digest,
        "released_lease_inode": 77,
        "released": True,
        "exact_token_matched": True,
        "released_at": 1110.0,
    }


def prepare(fixture):
    cfg, context = fresh_native_cfg(fixture)
    manifest, _path, obj, *_ = fixture
    worker = obj.read()["workers"][-1]
    root = native.contract(manifest)[3]
    cfg["native_worker"] = json.loads(
        (root / context["nonce"] / ("worker-" + worker["worker_id"] + ".json")).read_text()
    )["record"]["fields"]
    return cfg, context, worker


def hooks_for(cfg, clock, *, failure=None):
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.dependencies = {"interpreter": (Path(sys.executable).resolve(), "fixture")}
    hooks.interpreter_anchor = Path(sys.executable)
    hooks.env = {"CLOSER_ADMISSION_NONCE": NONCE}
    hooks._dependencies_match = lambda: failure != "dependencies"
    hooks.owns = lambda token: token == TOKEN

    def prerun(_deadline):
        clock[0] = 1110.0  # Beyond the existing aggregate B cutoff; no budgets changed.
        return 0, TOKEN

    hooks.prerun = prerun

    def postrun(_token, _deadline):
        record = cleanup()
        if failure == "missing":
            record = None
        elif failure == "successor":
            record["expected_token_sha256"] = "0" * 64
        elif failure == "already_absent":
            record["released"] = False
        hooks.postrun_result = {
            "code": 65,
            "dependencies_valid": failure != "dependencies",
            "stderr": "handoff-postrun: terminal named receipt unavailable; cleanup only",
            "refusal": "terminal_named_receipt_unavailable",
            "cleanup": record,
        }
        return 65

    hooks.postrun = postrun
    return hooks


def execute(fixture, *, failure=None):
    cfg, context, worker = prepare(fixture)
    clock = fixture[5]
    hooks = hooks_for(cfg, clock, failure=failure)
    calls = []

    def no_dispatch(*_args, **_kwargs):
        calls.append("B")
        pytest.fail("the positive no-start path must not call the dispatcher")

    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=no_dispatch,
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert not calls
    return cfg, context, worker, outcome, json.loads(detail)


def test_future_pre_b_refusal_seals_exact_cleanup_and_reconciles_pending(capacity_host):
    cfg, context, worker, outcome, detail = execute(capacity_host)
    assert outcome == "unavailable"
    proof = detail["native_no_start"]
    assert proof["phases"]["B"]["started"] is False
    assert proof["phases"]["C"]["cleanup"]["exact_token_matched"] is True
    assert proof["accepted"] is False
    manifest, _path, obj, *_ = capacity_host
    assert native.pre_dispatch_no_start_proof(manifest, cfg, proof) == proof
    result_path = campaign.result_path(obj.path, worker["worker_id"])
    campaign.write_result(
        str(result_path),
        worker["worker_id"],
        worker["target"],
        outcome,
        json.dumps(detail),
        mechanism="native-handoff/v1",
        native_admission=context,
    )
    raw = result_path.read_bytes()
    doc = campaign.reconcile(obj, pid_alive=lambda _: False, release_claim=lambda *_: None)
    assert doc["targets"]["*round*"] == "pending"
    assert doc["workers"][-1]["outcome"] == "unavailable"
    assert result_path.read_bytes() == raw  # Recovery never rewrites the original result.


@pytest.mark.parametrize("failure", ["missing", "successor", "already_absent", "dependencies"])
def test_c65_without_exact_cleanup_is_still_unknown(capacity_host, failure):
    _cfg, _context, _worker, outcome, detail = execute(capacity_host, failure=failure)
    assert outcome == "unknown"
    assert "native_no_start" not in detail
    assert detail["native_execution"]["phases"]["C"]["exit_code"] == 65


@pytest.mark.parametrize(
    "change", ["missing_boundary", "changed_boundary", "worker", "nonce", "proof", "duplicate"]
)
def test_no_start_proof_cannot_be_replayed_or_replaced(capacity_host, change):
    cfg, context, worker, _outcome, detail = execute(capacity_host)
    manifest = capacity_host[0]
    proof = copy.deepcopy(detail["native_no_start"])
    root = native.contract(manifest)[3]
    boundary = root / context["nonce"] / ("pre-dispatch-no-start-" + worker["worker_id"] + ".json")
    if change == "missing_boundary":
        boundary.unlink()
    elif change == "changed_boundary":
        boundary.write_text("{}")
    elif change == "worker":
        cfg["native_worker"]["result"] += ".other"
    elif change == "nonce":
        cfg["native_admission"]["nonce"] = "f" * 32
    elif change == "proof":
        proof["phases"]["B"]["started"] = True
    else:
        assert (
            native.pre_dispatch_no_start_proof(
                manifest,
                cfg,
                {"phases": proof["phases"], "execution": proof["execution"]},
                record_boundary=True,
            )
            is None
        )
        assert native.pre_dispatch_no_start_proof(manifest, cfg, proof) == proof
        return
    assert native.pre_dispatch_no_start_proof(manifest, cfg, proof) is None


@pytest.mark.parametrize("change", ["remove_proof", "mutate_proof", "plaintext_unknown"])
def test_reconciliation_does_not_promote_unproved_or_legacy_unknown(capacity_host, change):
    _cfg, context, worker, outcome, detail = execute(capacity_host)
    obj = capacity_host[2]
    if change == "remove_proof":
        del detail["native_no_start"]
    elif change == "mutate_proof":
        detail["native_no_start"]["phases"]["C"]["cleanup"]["released"] = False
    else:
        outcome = "unknown"
        detail = "dispatcher not started: old failure; ACTION C65"
    result_path = campaign.result_path(obj.path, worker["worker_id"])
    campaign.write_result(
        str(result_path),
        worker["worker_id"],
        worker["target"],
        outcome,
        detail if isinstance(detail, str) else json.dumps(detail),
        mechanism="native-handoff/v1",
        native_admission=context,
    )
    raw = result_path.read_bytes()
    doc = campaign.reconcile(obj, pid_alive=lambda _: False, release_claim=lambda *_: None)
    assert doc["targets"]["*round*"] == "reconcile"
    assert doc["workers"][-1]["outcome"] == "unknown"
    assert result_path.read_bytes() == raw


@pytest.mark.parametrize(
    "mutation", ["none", "missing", "duplicate", "nonce", "token", "absent", "inode"]
)
def test_cleanup_marker_requires_single_nonce_and_credential_match(tmp_path, mutation):
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.env = {"CLOSER_ADMISSION_NONCE": NONCE}
    hooks.lane = tmp_path / ".lane-closer.lock"
    receipt = cleanup(lane=hooks.lane)
    nonce = NONCE
    if mutation == "nonce":
        nonce = "b" * 48
    if mutation == "token":
        receipt["meta_sha256"] = "0" * 64
    if mutation == "absent":
        receipt["released"] = False
    if mutation == "inode":
        receipt["released_lease_inode"] = True
    line = "CLOSER_NATIVE_CLEANUP_V1 " + nonce + " " + json.dumps(receipt)
    stdout = line + "\n"
    if mutation == "missing":
        stdout = ""
    if mutation == "duplicate":
        stdout *= 2
    result = subprocess.CompletedProcess([], 65, stdout=stdout)
    assert bool(hooks.cleanup_receipt(TOKEN, result)) == (mutation == "none")


@pytest.mark.parametrize("mutation", ["none", "missing", "duplicate", "nonce", "reason"])
def test_typed_body_refusal_requires_one_original_nonce(mutation):
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.env = {"CLOSER_ADMISSION_NONCE": NONCE}
    nonce = "b" * 48 if mutation == "nonce" else NONCE
    reason = "different_failure" if mutation == "reason" else "terminal_named_receipt_unavailable"
    output = f"CLOSER_NATIVE_POSTRUN_REFUSAL_V1 {nonce} {reason}\n"
    if mutation == "missing":
        output = ""
    if mutation == "duplicate":
        output *= 2
    result = subprocess.CompletedProcess([], 65, stdout=output, stderr="unrelated diagnostic")
    assert bool(hooks.postrun_refusal(result)) == (mutation == "none")


@pytest.mark.parametrize("failure", ["timeout", "error", "binding_error"])
def test_any_possible_b_submission_stays_unknown_despite_cleanup(capacity_host, failure):
    cfg, _context, _worker = prepare(capacity_host)
    clock = capacity_host[5]
    hooks = hooks_for(cfg, clock)
    hooks.prerun = lambda _: (0, TOKEN)
    hooks.begin_terminal_receipt = lambda _: None
    calls = []

    def runner(argv, **kwargs):
        calls.append("B")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if failure == "binding_error":
            raise binding.BindingError("after submission")
        raise RuntimeError("after possible submission")

    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=runner,
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert calls == ["B"]
    assert outcome == "unknown"
    report = json.loads(detail)
    assert report["native_execution"]["phases"]["B"]["started"] is True
    assert "native_no_start" not in report


@pytest.mark.parametrize("change", ["late_cleanup", "inode", "missing_typed_refusal", "phase_time"])
def test_invalid_cleanup_or_phase_identity_cannot_seal(capacity_host, change):
    cfg, _context, _worker = prepare(capacity_host)
    clock = capacity_host[5]
    hooks = hooks_for(cfg, clock)
    original = hooks.postrun

    def postrun(token, deadline):
        code = original(token, deadline)
        if change == "late_cleanup":
            hooks.postrun_result["cleanup"]["released_at"] = 2000
        if change == "inode":
            hooks.postrun_result["cleanup"]["released_lease_inode"] = 78
        if change == "missing_typed_refusal":
            hooks.postrun_result.pop("refusal")
        if change == "phase_time":
            clock[0] = 999
        return code

    hooks.postrun = postrun
    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=lambda *_a, **_k: pytest.fail("no B"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert outcome == "unknown"
    assert "native_no_start" not in json.loads(detail)


def workspace_source(fixture):
    obj = fixture[2]
    path = obj.path.parent / "workspace-source.json"
    workspace = obj.path.parent / "isolated-execution"
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "remote",
            "add",
            "origin",
            "https://github.com/offline/repo.git",
        ],
        check=True,
    )
    raw = json.dumps(
        {
            "schema": "closer-workspace-bindings/v1",
            "historical": {},
            "execution": {"offline/repo#1": str(workspace)},
        }
    ).encode()
    path.write_bytes(raw)
    pin = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    obj.update(lambda doc: doc["worker_config"].update(workspaces=pin))
    return path, raw, pin


def test_native_workspace_binding_uses_immutable_original_bytes(capacity_host):
    source, original, pin = workspace_source(capacity_host)
    cfg, context, worker = prepare(capacity_host)
    manifest, _path, obj, *_ = capacity_host
    frozen = Path(context["workspace_config"]["path"])
    assert frozen.read_bytes() == original
    assert cfg["workspaces"] == {"path": str(frozen), "sha256": pin["sha256"]}
    source.write_text('{"different/repo#2":"/private/tmp/different"}')
    current = obj.read()["worker_config"]
    assert native.admission_config(manifest, current, context)["workspaces"] == cfg["workspaces"]
    cfg2 = native.result_admission_config(current, context, obj.path, obj.read(), worker)
    assert cfg2["workspaces"] == cfg["workspaces"]
    assert frozen.read_bytes() == original


def test_changed_workspace_pin_is_fenced_but_original_result_readback_is_stable(capacity_host):
    _source, _original, _pin = workspace_source(capacity_host)
    cfg, context, worker = prepare(capacity_host)
    manifest, _path, obj, *_ = capacity_host
    obj.update(lambda doc: doc["worker_config"]["workspaces"].update(sha256="f" * 64))
    doc = obj.read()
    with pytest.raises(RuntimeError, match="workspace configuration differs"):
        native.admission_config(manifest, doc["worker_config"], context)
    readback = native.result_admission_config(doc["worker_config"], context, obj.path, doc, worker)
    assert readback["workspaces"] == cfg["workspaces"]


def test_substituted_frozen_workspace_bytes_cannot_be_admitted(capacity_host):
    workspace_source(capacity_host)
    _cfg, context, _worker = prepare(capacity_host)
    Path(context["workspace_config"]["path"]).write_text("{}")
    with pytest.raises(RuntimeError, match="immutable workspace configuration differs"):
        native.validate_admission(capacity_host[0], context, for_effects=False)


@pytest.mark.parametrize("failure", ["version", "duplicate", "shape", "oversized", "symlink"])
def test_invalid_workspace_configuration_is_rejected_before_admission(capacity_host, failure):
    manifest, path, obj, *_ = capacity_host
    source, _raw, _pin = workspace_source(capacity_host)
    if failure == "version":
        source.write_text(
            '{"schema":"closer-workspace-bindings/v999","historical":{},"execution":{}}'
        )
    elif failure == "duplicate":
        source.write_text('{"offline/repo#1":"/private/tmp/a","offline/repo#1":"/private/tmp/b"}')
    elif failure == "shape":
        source.write_text("[]")
    elif failure == "oversized":
        source.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        actual = source.with_name("actual-workspaces.json")
        source.rename(actual)
        source.symlink_to(actual)
    pin = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    obj.update(lambda doc: doc["worker_config"].update(workspaces=pin))
    with pytest.raises((RuntimeError, ValueError)):
        native.publish_admission(manifest, path, obj.path, obj.read())


def test_cleanup_denial_is_preserved_and_not_retried(capacity_host):
    cfg, _context, _worker = prepare(capacity_host)
    clock = capacity_host[5]
    hooks = hooks_for(cfg, clock)
    calls = []

    def denied(token, deadline):
        calls.append("C")
        raise PermissionError("cleanup explicitly denied")

    hooks.postrun = denied
    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=lambda *_a, **_k: pytest.fail("no B"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert outcome == "denied" and calls == ["C"]
    report = json.loads(detail)
    assert report["native_execution"]["phases"]["C"]["denial"] == "cleanup explicitly denied"
    assert "native_no_start" not in report


def test_unexpected_failure_after_a_still_runs_c_once_and_stays_unknown(capacity_host):
    cfg, _context, _worker = prepare(capacity_host)
    clock = capacity_host[5]
    hooks = hooks_for(cfg, clock)
    calls = []
    original = hooks.postrun

    def bad_dependency():
        raise RuntimeError("unexpected after A")

    def postrun(token, deadline):
        calls.append("C")
        return original(token, deadline)

    hooks._dependencies_match = bad_dependency
    hooks.postrun = postrun
    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=lambda *_a, **_k: pytest.fail("no B"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert outcome == "unknown" and calls == ["C"]
    report = json.loads(detail)
    assert report["native_execution"]["phases"]["B"]["started"] is False
    assert "native_no_start" not in report


def test_pre_b_refusal_c0_without_cleanup_cannot_rearm(capacity_host):
    cfg, context, worker = prepare(capacity_host)
    clock = capacity_host[5]
    hooks = hooks_for(cfg, clock)

    def c0(token, deadline):
        hooks.postrun_result = {"code": 0, "dependencies_valid": True, "cleanup": None}
        return 0

    hooks.postrun = c0
    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=lambda *_a, **_k: pytest.fail("no B"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert outcome == "unknown"
    obj = capacity_host[2]
    campaign.write_result(
        str(campaign.result_path(obj.path, worker["worker_id"])),
        worker["worker_id"],
        worker["target"],
        outcome,
        detail,
        mechanism="native-handoff/v1",
        native_admission=context,
    )
    doc = campaign.reconcile(obj, pid_alive=lambda _: False, release_claim=lambda *_: None)
    assert doc["targets"]["*round*"] == "reconcile"


def test_stripped_proof_with_changed_c_code_still_requires_signed_recovery(capacity_host):
    _cfg, context, worker, outcome, detail = execute(capacity_host)
    del detail["native_no_start"]
    detail["native_execution"]["phases"]["C"]["exit_code"] = 0
    obj = capacity_host[2]
    campaign.write_result(
        str(campaign.result_path(obj.path, worker["worker_id"])),
        worker["worker_id"],
        worker["target"],
        outcome,
        json.dumps(detail),
        mechanism="native-handoff/v1",
        native_admission=context,
    )
    doc = campaign.reconcile(obj, pid_alive=lambda _: False, release_claim=lambda *_: None)
    assert doc["targets"]["*round*"] == "reconcile"
    assert doc["workers"][-1]["outcome"] == "unknown"


@pytest.mark.parametrize(
    "change", ["possible_B", "missing_execution", "worker", "nonce", "timestamp"]
)
def test_signed_proof_cannot_override_conflicting_execution_report(capacity_host, change):
    _cfg, context, worker, outcome, detail = execute(capacity_host)
    if change == "possible_B":
        detail["native_execution"]["phases"]["B"]["started"] = True
    if change == "missing_execution":
        del detail["native_execution"]
    if change == "worker":
        detail["native_execution"]["worker"]["result"] += ".foreign"
    if change == "nonce":
        detail["native_execution"]["admission_nonce"] = "f" * 32
    if change == "timestamp":
        detail["native_execution"]["runtime_binding_ready_at"] += 1
    obj = capacity_host[2]
    campaign.write_result(
        str(campaign.result_path(obj.path, worker["worker_id"])),
        worker["worker_id"],
        worker["target"],
        outcome,
        json.dumps(detail),
        mechanism="native-handoff/v1",
        native_admission=context,
    )
    doc = campaign.reconcile(obj, pid_alive=lambda _: False, release_claim=lambda *_: None)
    assert doc["targets"]["*round*"] == "reconcile"
    assert doc["workers"][-1]["outcome"] == "unknown"


@pytest.mark.parametrize("error", [PermissionError, RuntimeError])
def test_stale_cleanup_metadata_after_c_exception_never_promotes(capacity_host, error):
    cfg, context, worker = prepare(capacity_host)
    clock = capacity_host[5]
    hooks = hooks_for(cfg, clock)
    original = hooks.postrun

    def failed(token, deadline):
        original(token, deadline)
        raise error("explicit postrun readback failure")

    hooks.postrun = failed
    outcome, detail = binding.run_native_round(
        [sys.executable, "fixture.py", "--assignment", cfg["assignment"], "--budget-seconds", "10"],
        cfg,
        runner=lambda *_a, **_k: pytest.fail("no B"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert outcome == ("denied" if error is PermissionError else "unknown")
    assert "native_no_start" not in json.loads(detail)
    root = native.contract(capacity_host[0])[3]
    assert not (
        root / context["nonce"] / ("pre-dispatch-no-start-" + worker["worker_id"] + ".json")
    ).exists()


@pytest.mark.parametrize(
    "change",
    ["expired", "published_unknown", "future_phase", "result_symlink", "different_process"],
)
def test_new_boundary_cannot_backfill_expired_or_published_attempt(capacity_host, change):
    cfg, context, worker, _outcome, detail = execute(capacity_host)
    manifest, _path, obj, _doc, _source, clock, *_ = capacity_host
    proof = detail["native_no_start"]
    boundary = (
        native.contract(manifest)[3]
        / context["nonce"]
        / ("pre-dispatch-no-start-" + worker["worker_id"] + ".json")
    )
    boundary.unlink()  # Private fixture only; never a live receipt.
    result = campaign.result_path(obj.path, worker["worker_id"])
    if change == "expired":
        clock[0] = context["original_deadline"] + 1
    if change == "different_process":
        native._CURRENT_ENTRY.pop(str(native.contract(manifest)[3]), None)
    if change == "future_phase":
        clock[0] = proof["phases"]["C"]["finished_at"] - 1
    if change == "published_unknown":
        campaign.write_result(
            str(result), worker["worker_id"], worker["target"], "unknown", "old UNKNOWN"
        )
    if change == "result_symlink":
        result.parent.mkdir(exist_ok=True)
        result.symlink_to(result.parent / "missing-old-result")
    raw = result.read_bytes() if result.is_file() else None
    assert (
        native.pre_dispatch_no_start_proof(
            manifest,
            cfg,
            {"phases": proof["phases"], "execution": proof["execution"]},
            record_boundary=True,
        )
        is None
    )
    assert not boundary.exists()
    if raw is not None:
        assert result.read_bytes() == raw


def test_expired_readback_uses_only_already_sealed_original_proof(capacity_host):
    cfg, context, worker, _outcome, detail = execute(capacity_host)
    proof = detail["native_no_start"]
    capacity_host[5][0] = context["original_deadline"] + 1
    native._CURRENT_ENTRY.pop(str(native.contract(capacity_host[0])[3]), None)
    assert native.pre_dispatch_no_start_proof(capacity_host[0], cfg, proof) == proof


@pytest.mark.parametrize("outcome", sorted(campaign.WORKER_OUTCOMES))
def test_existing_worker_result_prevents_all_replayed_effects(tmp_path, monkeypatch, outcome):
    result = tmp_path / "abcdef123456.json"
    original = (
        json.dumps(
            {
                "worker_id": result.stem,
                "target": campaign.ROUND_UNIT,
                "outcome": outcome,
                "detail": "original receipt",
            }
        )
        + "\n"
    ).encode()
    result.write_bytes(original)

    def forbidden(*_args, **_kwargs):
        pytest.fail("existing result must fence before campaign/claim/provider access")

    monkeypatch.setattr(campaign.Campaign, "read", forbidden)
    assert (
        campaign.run_worker(
            str(tmp_path / "state"),
            str(result),
            campaign.ROUND_UNIT,
            "owner",
            2,
            runner=forbidden,
            holder=forbidden,
            native_probe=forbidden,
            delegate=forbidden,
        )
        == outcome
    )
    assert result.read_bytes() == original


@pytest.mark.parametrize(
    "kind",
    [
        "malformed",
        "deep_json",
        "duplicate",
        "wrong_worker",
        "wrong_target",
        "bad_outcome",
        "symlink",
        "dangling_symlink",
    ],
)
def test_unsafe_existing_worker_result_is_preserved_unknown(tmp_path, monkeypatch, kind):
    result = tmp_path / "abcdef123456.json"
    record = {"worker_id": result.stem, "target": campaign.ROUND_UNIT, "outcome": "ok"}
    if kind == "wrong_worker":
        record["worker_id"] = "111111111111"
    elif kind == "wrong_target":
        record["target"] = "owner/repo#1"
    elif kind == "bad_outcome":
        record["outcome"] = {"unknown": True}
    original = b"not JSON\n" if kind == "malformed" else json.dumps(record).encode()
    if kind == "deep_json":
        original = b"[" * 2000 + b"0" + b"]" * 2000
    if kind == "duplicate":
        original = original[:-1] + b', "outcome": "unknown"}'
    destination = tmp_path / "original.json"
    if "symlink" in kind:
        if kind == "symlink":
            destination.write_bytes(original)
        result.symlink_to(destination)
    else:
        result.write_bytes(original)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unsafe result must not replay work")

    monkeypatch.setattr(campaign.Campaign, "read", forbidden)
    assert (
        campaign.run_worker(
            str(tmp_path / "state"),
            str(result),
            campaign.ROUND_UNIT,
            "owner",
            2,
            runner=forbidden,
            holder=forbidden,
        )
        == "unknown"
    )
    if "symlink" in kind:
        assert result.is_symlink() and os.readlink(result) == str(destination)
        assert (
            destination.read_bytes() == original
            if destination.exists()
            else kind == "dangling_symlink"
        )
    else:
        assert result.read_bytes() == original


def test_result_publication_collision_preserves_completed_unknown(tmp_path, monkeypatch):
    result = tmp_path / "abcdef123456.json"
    original = json.dumps(
        {
            "worker_id": result.stem,
            "target": campaign.ROUND_UNIT,
            "outcome": "unknown",
            "detail": "concurrent terminal receipt",
        }
    ).encode()

    def interrupted_read(_self):
        result.write_bytes(original)
        raise campaign.CampaignError("worker interrupted after another process recorded result")

    monkeypatch.setattr(campaign.Campaign, "read", interrupted_read)
    assert (
        campaign.run_worker(str(tmp_path / "state"), str(result), campaign.ROUND_UNIT, "owner", 2)
        == "unknown"
    )
    assert result.read_bytes() == original
    assert list(tmp_path.glob(".abcdef123456.json.*")) == []


def test_worker_result_create_only_publishes_complete_receipt(tmp_path):
    result = tmp_path / "abcdef123456.json"
    campaign.write_result(
        result, result.stem, campaign.ROUND_UNIT, "unknown", "first", create_only=True
    )
    original = result.read_bytes()
    with pytest.raises(FileExistsError):
        campaign.write_result(
            result, result.stem, campaign.ROUND_UNIT, "unavailable", "replay", create_only=True
        )
    assert result.read_bytes() == original
    assert json.loads(original)["detail"] == "first"
    assert list(tmp_path.glob(".abcdef123456.json.*")) == []
