"""Pure review regressions; detached tails never establish source admission.

Only local JSON custody and explicitly non-authorizing correlation data execute.
No provider, auth reader, native process, source issuer or remote effect is used.
"""

import ast
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_correction_enrollment_terminal as enrollment
import closer_correction_producer as producer
import closer_correction_source_record as source
import closer_dispatcher as closer
import closer_mechanical_source as mechanical
import closer_queue as queue
import closer_source_installation as installed
import dispatcher

SECRET = "SYNTHETIC_PRIVATE_ERROR_CONTENT"


def queue_data(tmp_path, prior):
    target = "owner/repo#1"
    frontier = {"frontier_id": "F", "targets": [target]}
    original = {
        "id": "original",
        "status": "submitted",
        "action": "disposition",
        "finite_frontier": frontier,
        "submitted_at": 1,
        "deadline": 10,
    }
    raw = queue._canonical_bytes(original).decode()
    sha = hashlib.sha256(raw.encode()).hexdigest()
    proof = {"operation_sha256": sha}
    result = {
        "correction_enrollment_terminal": proof,
        "target_complete": False,
        "act_authorized": False,
        "source_closure_authorized": False,
    }
    retained = {**original, "status": "completed", "invoked_at": 2, "result": result}
    item = {"operation": retained, "operation_history": [prior]}
    state = {
        "items": {},
        "frontiers": {
            "F": {
                "policy": frontier,
                "round": {"finite_frontier": frontier},
                "items": {target: item},
            }
        },
    }
    path = tmp_path.resolve() / "queue.json"
    path.write_bytes(queue._canonical_bytes(state))
    record = {
        "operation_json": raw,
        "operation_sha256": sha,
        "finite_frontier": frontier,
        "target": target,
        "queue_path": str(path),
    }
    return record, proof, target, frontier, state, path


@pytest.mark.parametrize("status", ["completed", "confirmed_not_started", "abandoned"])
def test_known_no_effect_history_preserves_current_completed_operation(tmp_path, status):
    record, proof, target, frontier, _state, path = queue_data(tmp_path, {"status": status})
    before = path.read_bytes()
    original, current = enrollment._queue_operation(record, proof, target, frontier, "original")
    assert original["status"] == "submitted" and current["status"] == "completed"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "status", ["failed", "unknown", "uncertain", "unresolved_effect", "running", "submitted"]
)
def test_history_never_erases_an_unknown_or_nonterminal_operation(tmp_path, status):
    record, proof, target, frontier, _state, path = queue_data(tmp_path, {"status": status})
    before = path.read_bytes()
    with pytest.raises(enrollment.EnrollmentUnknown):
        enrollment._queue_operation(record, proof, target, frontier, "original")
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["outcome", "result", "barrier"])
def test_known_history_status_cannot_hide_uncertainty(tmp_path, change):
    prior = {"status": "abandoned"}
    if change == "outcome":
        prior["outcome_uncertain"] = True
    elif change == "result":
        prior["result"] = {"status": "unknown"}
    else:
        prior["recovery_barrier"] = {"owned": True}
    record, proof, target, frontier, _state, _path = queue_data(tmp_path, prior)
    with pytest.raises(enrollment.EnrollmentUnknown):
        enrollment._queue_operation(record, proof, target, frontier, "original")


@pytest.mark.parametrize("status", ["confirmed_not_started", "abandoned"])
def test_current_operation_still_requires_completed(tmp_path, status):
    record, proof, target, frontier, state, path = queue_data(tmp_path, {"status": "completed"})
    state["frontiers"]["F"]["items"][target]["operation"]["status"] = status
    path.write_bytes(queue._canonical_bytes(state))
    with pytest.raises(enrollment.EnrollmentUnknown, match="completed source operation"):
        enrollment._queue_operation(record, proof, target, frontier, "original")


def projection():
    return {
        "schema": "closer-correction-controller-policy-projection/v1",
        "repository": "owner/repo",
        "purpose": "frontier-correlation-only",
        "controller_sha256": "a" * 64,
        "installation_sha256": "b" * 64,
    }


def backend(tmp_path):
    # Not a factory binding; the mocked projection below is correlation-only
    # DATA. No SourceInstallation or current invocation is issued by these tests.
    binding = SimpleNamespace(bundle=object(), policy={"revision": 1})
    return closer.Closer(object(), state_path=tmp_path / "state", runtime_binding=binding)


@pytest.mark.parametrize(
    ("repository", "target", "expected"),
    [
        ("stranske/orchestrator", "stranske/Orchestrator#984", True),
        ("stranske/orchestrator", "STRANSKE/ORCHESTRATOR#984", True),
        ("owner/repo", "owner/repo#1", True),
        ("stranske/other", "stranske/Orchestrator#984", False),
        ("other/orchestrator", "stranske/Orchestrator#984", False),
        (None, "stranske/Orchestrator#984", False),
    ],
)
def test_projection_repository_identity_uses_exact_case_insensitive_match(
    tmp_path, monkeypatch, repository, target, expected
):
    owner = backend(tmp_path)
    value = {**projection(), "repository": repository}
    monkeypatch.setattr(installed, "controller_policy_projection", lambda _: value)
    actual = owner.operation_binding(target, {})
    assert ("correction_controller_sha256" in actual) is expected
    assert ("correction_installation_sha256" in actual) is expected
    if expected:
        assert actual["correction_controller_sha256"] == "a" * 64
        assert actual["correction_installation_sha256"] == "b" * 64


def test_unavailable_projection_does_not_flip_same_instance_action_identity(tmp_path, monkeypatch):
    owner = backend(tmp_path)
    calls = []

    def intermittent(_binding):
        calls.append(1)
        return None if len(calls) == 1 else projection()

    monkeypatch.setattr(installed, "controller_policy_projection", intermittent)
    first = owner.operation_binding("owner/repo#1", {})
    assert owner.operation_binding("owner/repo#1", {}) == first
    assert calls == [1] and "correction_controller_sha256" not in first


@pytest.mark.parametrize("change", ["policy", "binding", "bundle", "mechanism", "source"])
def test_source_identity_change_invalidates_only_the_missing_cache(tmp_path, monkeypatch, change):
    owner = backend(tmp_path)
    calls = []

    def intermittent(_binding):
        calls.append(1)
        return None if len(calls) == 1 else projection()

    monkeypatch.setattr(installed, "controller_policy_projection", intermittent)
    assert "correction_controller_sha256" not in owner.operation_binding("owner/repo#1", {})
    if change == "policy":
        owner.runtime_binding.policy["revision"] = 2
    elif change == "binding":
        owner.runtime_binding = SimpleNamespace(bundle=object(), policy={"revision": 1})
    elif change == "bundle":
        owner.runtime_binding.bundle = object()
    elif change == "mechanism":
        owner.runtime_binding.mechanism = "changed"
    else:
        monkeypatch.setattr(installed, "_SOURCE_SHA256", "f" * 64)
    assert owner.operation_binding("owner/repo#1", {})["correction_controller_sha256"] == "a" * 64
    assert calls == [1, 1]


def test_positive_projection_is_never_a_cached_qualification(tmp_path, monkeypatch):
    owner = backend(tmp_path)
    values = iter([projection(), None])
    monkeypatch.setattr(installed, "controller_policy_projection", lambda _: next(values))
    assert owner.operation_binding("owner/repo#1", {})["correction_controller_sha256"] == "a" * 64
    assert "correction_controller_sha256" not in owner.operation_binding("owner/repo#1", {})


@pytest.mark.parametrize("error", [NameError(SECRET), source.SourceRecordUnavailable(SECRET)])
def test_coding_registration_diagnostic_is_fixed_vocabulary(error):
    value = source._failure_diagnostic(error, "coding_registration")
    assert value["stage"] == "coding_registration"
    assert value["exception"] == type(error).__name__
    assert value["code"] == "unclassified_error"
    assert SECRET not in json.dumps(value)
    assert source._bounded_failure_diagnostic(value) == value


def test_registration_failure_tail_retains_cause_without_new_authority(monkeypatch):
    import closer_coding_continuation as continuation

    def refused(*_args):
        raise NameError(SECRET)

    monkeypatch.setattr(continuation, "retain_after_confirmation_locked", refused)
    tree = ast.parse(inspect.getsource(source.execute_candidate)).body[0]
    statements = next(n.body for n in ast.walk(tree) if isinstance(n, ast.With))
    # Select the existing post-confirmation data tail only, never any validator.
    start = next(
        i
        for i, n in enumerate(statements)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "coding_reference" for t in n.targets)
    )
    tail = statements[start:]
    function = ast.FunctionDef(
        name="tail",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=tail,
        decorator_list=[],
    )
    namespace = {
        "terminal": {"synthetic": "post-confirmation input only"},
        "controller": object(),
        "custody": object(),
        "operation": {},
        "selection": {},
        "issue": {},
        "published": {},
        "enrolled": {},
        "_failure_diagnostic": source._failure_diagnostic,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
            "detached-source-confirmation-tail",
            "exec",
        ),
        namespace,
    )
    value = namespace["tail"]()["coding_continuation"]
    assert value["status"] == "held" and value["execution_authorized"] is False
    assert value["failure_diagnostic"]["exception"] == "NameError"
    assert SECRET not in json.dumps(value)


@pytest.mark.parametrize("data_only", [False, True])
def test_maintenance_rejection_is_known_pre_spawn_refusal(monkeypatch, data_only):
    import closer_correction_maintenance as maintenance

    monkeypatch.setattr(maintenance._CURRENT, "scope", object(), raising=False)
    monkeypatch.setenv("ORCH_OFFLOAD_DISABLED", "1")
    result = dispatcher.offload("claude", "SYNTHETIC", data_only=data_only)
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert result["retry_safe"] is False and result["run_id"] is None and result["exit"] == 2
    assert result["error"] == "deterministic correction maintenance forbids provider offload"
    assert "disabled" not in result  # Maintenance guard still precedes kill switch.


def test_invalid_utf8_terminal_tail_uses_executed_observation_failure():
    tree = ast.parse(inspect.getsource(dispatcher.run_observed_offload)).body[0]
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Constant) and c.value == "invalid_utf8_output"
            for c in ast.walk(n.test)
        )
    )
    observed = {"invalid_utf8_output": True, "cleanup_terminal": True}
    namespace = {
        "_coding_session_runtime": SimpleNamespace(
            capture_limit=lambda _: 131072,
            SessionHeld=dispatcher._coding_session_runtime.SessionHeld,
        ),
        "sys": SimpleNamespace(_getframe=lambda: None),
        "observed": observed,
        "child": SimpleNamespace(returncode=0),
        "stdout": "synthetic retained output",
        "stderr": "synthetic retained stderr",
        "ObservedOffloadFailure": dispatcher.ObservedOffloadFailure,
    }
    with pytest.raises(dispatcher.ObservedOffloadFailure) as raised:
        exec(
            compile(
                ast.Module(body=[node], type_ignores=[]), "detached-post-cleanup-refusal", "exec"
            ),
            namespace,
        )
    assert raised.value.observation is observed and raised.value.returncode == 0
    assert raised.value.stdout == namespace["stdout"] and raised.value.stderr == namespace["stderr"]


@pytest.mark.parametrize("paths", [[["a.py"], ["other.py"]], [["other.py"], ["a.py"]]])
def test_unmatched_diagnostic_does_not_erase_a_matching_finding(paths):
    tree = ast.parse(inspect.getsource(mechanical.collect_source_registration)).body[0]

    def diagnostic_assign(n):
        return (
            isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "parse_diagnostics"
        )

    node = next(
        n
        for n in ast.walk(tree)
        if diagnostic_assign(n)
        or (isinstance(n, ast.Try) and len(n.body) == 1 and diagnostic_assign(n.body[0]))
    )
    loop = ast.For(
        target=ast.Name(id="paths", ctx=ast.Store()),
        iter=ast.Name(id="candidates", ctx=ast.Load()),
        body=[
            node,
            ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="retained", ctx=ast.Load()), attr="append", ctx=ast.Load()
                    ),
                    args=[ast.Name(id="diagnostics", ctx=ast.Load())],
                    keywords=[],
                )
            ),
        ],
        orelse=[],
    )
    namespace = {
        "candidates": paths,
        "retained": [],
        "policy": SimpleNamespace(operation="format"),
        "log": "would reformat a.py",
        "parse_diagnostics": mechanical.parse_diagnostics,
        "SourceUnavailable": mechanical.SourceUnavailable,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
            "detached-diagnostic-candidate-loop",
            "exec",
        ),
        namespace,
    )
    assert namespace["retained"] == [
        [{"path": "a.py", "code": "black-format", "message": "would reformat a.py"}]
    ]


def test_existing_retained_reference_formula_matches_actual_writer(tmp_path):
    root = tmp_path.resolve() / "custody"
    root.mkdir(mode=0o700)
    value = {"unicode": "é", "values": [1, False, None]}
    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf8")
    sha = hashlib.sha256(raw).hexdigest()
    expected = {
        "path": str(root / "raw" / sha[:2] / (sha + ".correction-decision.json")),
        "sha256": sha,
        "bytes": len(raw),
    }
    assert producer.retain(root, value, "correction-decision.json") == expected
    assert Path(expected["path"]).read_bytes() == raw
