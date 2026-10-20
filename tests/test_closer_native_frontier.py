"""Explicit native frontier admission; no provider/native processes are invoked."""

import json
import subprocess
from pathlib import Path

import pytest
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_frontier_policy as policy
import closer_mechanisms as mechanisms
import closer_native_observer as native
import closer_runtime_binding as binding


def finite_host(fixture):
    manifest, path, obj, doc, source, clock, _ = fixture
    manifest["mechanism_contract"]["capacity_publication"].update(
        schema="native-scoped-capacity/v2",
        execution_policy=policy.POLICY,
        request_timeout_seconds=30,
        freshness_seconds=900,
    )
    manifest["mechanism_contract"]["capacity_publication"].pop("admission_seconds")
    frontier = policy.make_frontier(
        ["offline/repo#1"],
        owner=doc["owner"],
        frontier_id="explicit-new-frontier",
        bindings={
            "offline/repo#1": {
                "revision": "head-1",
                "action_id": policy.action_identity("offline/repo#1", "head-1", "review", {}),
            }
        },
    )
    doc["worker_config"].update(finite_frontier=frontier, request_timeout_seconds=30)
    campaign.write_manifest(path, manifest)
    root = native.contract(manifest)[3]
    invocation = native._CURRENT_ADMISSION[str(root)]
    invocation.pop("original_deadline")
    invocation["execution_policy"] = policy.POLICY
    native._write(
        root / "native-active.json",
        {"nonce": invocation["nonce"], "status": "submitted", "binding": native.binding(manifest)},
    )
    return manifest, path, obj, doc, source, clock, frontier


def test_explicit_frontier_preserves_exact_descriptor_and_separates_freshness(capacity_host):
    manifest, path, obj, doc, _, clock, frontier = finite_host(capacity_host)
    clock[0] += 601  # greater than historical 600-second admission; no work-duration gate
    context = native.publish_admission(manifest, path, obj.path, doc)
    assert context["schema"] == "native-invocation-admission/v2"
    assert context["finite_frontier"] == frontier
    assert "original_deadline" not in context
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    assert cfg["finite_frontier"] == frontier
    assert cfg["request_timeout_seconds"] == 30
    assert cfg["execution_policy"] == policy.POLICY
    assert not {"original_deadline", "budget_seconds", "dispatch_budget_seconds"} & cfg.keys()
    # Evidence freshness remains a separate admission gate, with immutable old bytes.
    old = Path(context["report"]["path"]).read_bytes()
    clock[0] += 901
    with pytest.raises(mechanisms.MechanismError, match="inputs changed"):
        native.validate_admission(manifest, context, for_effects=True)
    native.validate_admission(manifest, context, for_effects=False)
    assert Path(context["report"]["path"]).read_bytes() == old


@pytest.mark.parametrize("change", ["timeout", "frontier", "old_schema", "budget", "missing"])
def test_native_finite_binding_requires_exact_new_admission(capacity_host, change):
    manifest, path, obj, doc, _, _, _ = finite_host(capacity_host)
    context = native.publish_admission(manifest, path, obj.path, doc)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    if change == "timeout":
        cfg["request_timeout_seconds"] = float("inf")
    elif change == "frontier":
        cfg["finite_frontier"]["frontier_id"] = "substituted"
    elif change == "old_schema":
        cfg["native_admission"]["schema"] = "native-invocation-admission/v1"
    elif change == "budget":
        cfg["original_deadline"] = 999999
    else:
        cfg.pop("finite_frontier")
    with pytest.raises(binding.BindingError):
        binding._finite_native_frontier(cfg)


@pytest.mark.parametrize("bad_timeout", [None, True, 0, -1, float("nan"), float("inf"), 121, 1e308])
def test_native_v2_requires_finite_positive_request_window(capacity_host, bad_timeout):
    manifest, *_ = finite_host(capacity_host)
    manifest["mechanism_contract"]["capacity_publication"]["request_timeout_seconds"] = bad_timeout
    with pytest.raises(mechanisms.MechanismError):
        native.capacity_contract(manifest)


def test_old_native_manifest_cannot_infer_frontier(capacity_host):
    manifest, path, obj, doc, *_ = capacity_host
    doc["worker_config"]["finite_frontier"] = policy.make_frontier(
        ["offline/repo#1"],
        owner="owner",
        frontier_id="must-not-infer",
        bindings={
            "offline/repo#1": {
                "revision": "head-1",
                "action_id": policy.action_identity("offline/repo#1", "head-1", "review", {}),
            }
        },
    )
    with pytest.raises(mechanisms.MechanismError, match="explicit native v2"):
        native.publish_admission(manifest, path, obj.path, doc)


def test_v2_cannot_renew_legacy_original_deadline(capacity_host):
    manifest, path, obj, doc, *_ = finite_host(capacity_host)
    native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["original_deadline"] = 1600
    with pytest.raises(mechanisms.MechanismError, match="differs"):
        native.publish_admission(manifest, path, obj.path, doc)


@pytest.mark.parametrize("failure", [None, "timeout", "denied"])
def test_new_frontier_a_and_c_are_bounded_without_aggregate_b_cutoff(
    capacity_host, monkeypatch, failure
):
    manifest, path, obj, doc, _, clock, frontier = finite_host(capacity_host)
    context = native.publish_admission(manifest, path, obj.path, doc)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    events = []

    class Hooks(binding.PinnedNativeHooks):
        def __init__(self):
            self.bounded = True
            self.interpreter_anchor = Path("/offline/python")
            self.dependencies = {"interpreter": (self.interpreter_anchor, "unused")}
            self.env = {}

        def prerun(self, deadline):
            events.append(("A", deadline))
            clock[0] += 20
            return 0, "exact-token"

        def owns(self, token):
            return token == "exact-token"

        def _dependencies_match(self):
            return True

        def begin_terminal_receipt(self, _cfg):
            pass

        def set_terminal_receipt(self, done, _cfg):
            return True

        def postrun(self, token, deadline):
            events.append(("C", deadline, token))
            self.postrun_result = {"code": 0, "dependencies_valid": True}
            return 0

    def runner(command, **kwargs):
        events.append(("B", kwargs["timeout"]))
        clock[0] += 5000  # cumulative useful work exceeds all former aggregate windows
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if failure == "denied":
            raise PermissionError("isolated execution denied")
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(native, "completed_round_proof", lambda *_a, **_kw: {"offline": "proof"})
    command = [
        "/offline/python",
        "--assignment",
        cfg["assignment"],
        "--finite-frontier-json",
        json.dumps(frontier),
        "--request-timeout-seconds",
        "30",
    ]
    outcome, _ = binding.run_native_round(
        command, cfg, runner=runner, hooks=Hooks(), now=lambda: clock[0]
    )
    assert outcome == ({None: "ok", "timeout": "unknown", "denied": "denied"}[failure])
    assert events == [("A", 1030.0), ("B", None), ("C", 6050.0, "exact-token")]


@pytest.mark.parametrize("mutation", ["budget", "frontier", "timeout", "duplicate", "abbreviation"])
def test_finite_dispatch_command_rejects_drift(capacity_host, mutation):
    *_, frontier = finite_host(capacity_host)
    command = [
        "python",
        "--finite-frontier-json",
        json.dumps(frontier),
        "--request-timeout-seconds",
        "30",
    ]
    if mutation == "budget":
        command += ["--budget-seconds", "999"]
    elif mutation == "frontier":
        command[2] = "{}"
    elif mutation == "timeout":
        command[-1] = "nan"
    elif mutation == "duplicate":
        command += ["--request-timeout-seconds", "30"]
    else:
        command += ["--bud=999"]
    with pytest.raises(binding.BindingError):
        binding._finite_dispatch_command(command, frontier, 30)


def test_finite_native_does_not_rearm_for_assignment_change(capacity_host):
    manifest, _path, obj, doc, source, _clock, _frontier = finite_host(capacity_host)
    doc["targets"] = {campaign.ROUND_UNIT: "done"}
    source.write_text('{"targets":[{"target":"offline/repo#2"}]}')
    assert native.rearm_on_assignment_change(obj, doc, manifest) == doc
    assert doc["generation"] == 1


def test_finite_admission_cannot_seal_legacy_no_start_proof(capacity_host):
    manifest, path, obj, doc, *_ = finite_host(capacity_host)
    context = native.publish_admission(manifest, path, obj.path, doc)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    assert native.pre_dispatch_no_start_proof(manifest, cfg, {}, record_boundary=True) is None
    directory = native.contract(manifest)[3] / context["nonce"]
    assert not list(directory.glob("no-start-*.json"))


def test_native_finite_terminal_receipt_and_completion_bind_exact_frontier(capacity_host):
    import closer_pipeline
    import closer_resources

    manifest, path, obj, doc, _source, _clock, frontier = finite_host(capacity_host)
    context = native.publish_admission(manifest, path, obj.path, doc)
    cfg = native.admission_config(manifest, doc["worker_config"], context)
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.begin_terminal_receipt(cfg)
    # Pure native proof mechanics use local fakes. End-to-end eligibility is
    # separately guarded by the production-dispatcher callback registration.
    original = closer_pipeline._require_dispatcher_finite_callbacks
    closer_pipeline._require_dispatcher_finite_callbacks = lambda _callbacks: None
    try:
        receipt = closer_pipeline.run_pipeline(
            Path(str(cfg["dispatch_state"]) + ".pipeline.json"),
            frontier["targets"],
            coordinator_mode="resource_scoped",
            coordinator_contract_pin={
                "schema": closer_resources.CONTRACT_SCHEMA,
                "layout": closer_resources.LAYOUT,
                "pipeline_schema": closer_pipeline.SCHEMA,
                "coordination_gates": 1,
            },
            finite_frontier=frontier,
            request_timeout_seconds=30,
            prepare=lambda _t, _c: {"revision": "head-1"},
            interpret=lambda _t, _c: {"substantive_needs": []},
            act=lambda _t, _c: {"status": "completed", "target_complete": True},
            exception=lambda _t, _c: {},
            capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            now=lambda: _clock[0],
        )
    finally:
        closer_pipeline._require_dispatcher_finite_callbacks = original
    done = subprocess.CompletedProcess([], 0, stdout=json.dumps(receipt))
    assert hooks.set_terminal_receipt(done, cfg)
    foreign = dict(receipt, finite_frontier=dict(frontier, frontier_id="foreign-frontier"))
    assert native.completed_round_proof(manifest, cfg, foreign, record_boundary=True) is None
    proof = native.completed_round_proof(manifest, cfg, receipt, record_boundary=True)
    assert proof is not None, closer_resources.validate_stop(
        Path(str(cfg["dispatch_state"]) + ".pipeline.json"),
        receipt["round_id"],
        frontier["targets"],
    )
    assert proof["finite_frontier"] == frontier
    assert native.completed_round_proof(manifest, cfg, proof) == proof
    wrong = dict(receipt, finite_frontier=dict(frontier, frontier_id="foreign-frontier"))
    assert not hooks.set_terminal_receipt(
        subprocess.CompletedProcess([], 0, stdout=json.dumps(wrong)), cfg
    )
    assert native.completed_round_proof(manifest, cfg, wrong, record_boundary=True) is None
