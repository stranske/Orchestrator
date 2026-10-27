"""Necessary source-refusal/data/failure controls; no genuine admission or process.

Detached tail/finalizer controls do not execute or bypass authentication prefixes.
Every injected source producer fails; no authentication-success stub is used.
"""

import ast
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

import closer_coding_worker as worker  # noqa: E402
import closer_native_observer as native  # noqa: E402
import closer_runtime_binding as runtime  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_redaction_environment(monkeypatch):
    """Synthetic controls use no ambient credential-file redaction inputs."""
    monkeypatch.setenv("ORCH_REDACT_SECRET_FILES", "")


SECRET = "SYNTHETIC_USER_OR_CREDENTIAL_CONTENT"
HELD_FRAME = {
    "schema": "closer-coding-worker-held/v1",
    "status": "held",
    "reason": "coding_worker_source_refused",
    "diagnostic": {"stage": "issue_root", "code": "admitted_root_required"},
    "execution_authorized": False,
    "retry_authorized": False,
}


def root_failure():
    try:
        worker._issue_root(None, None, [])
    except worker.WorkerHeld as error:
        return native.coding_worker_held(error)
    raise AssertionError("invalid caller must refuse before source admission")


def source_function(name):
    return next(
        node
        for node in ast.parse(Path(runtime.__file__).read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def detached(body, namespace):
    """Run only explicitly selected presentation/post-observation mechanics."""
    names = ("detail",) if "detail" in namespace else ()
    function = ast.FunctionDef(
        name="necessary_detached_control",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=name) for name in names],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=body,
        decorator_list=[],
    )
    code = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(code, "<necessary-detached-control>", "exec"), namespace)
    return namespace["necessary_detached_control"](*(namespace[name] for name in names))


def test_actual_invalid_source_root_projects_exact_stage_without_authentication():
    frame = root_failure()
    assert frame["diagnostic"] == {"stage": "issue_root", "code": "admitted_root_required"}
    assert frame["execution_authorized"] is False and frame["retry_authorized"] is False
    assert native._coding_held_diagnostic(json.dumps(frame)) == frame["diagnostic"]


def test_actual_invalid_root_lifetime_projects_checked_code_only():
    root = worker._Root(object(), None, threading.get_ident(), os.getpid(), None, None, {}, b"", {})
    with pytest.raises(worker.WorkerHeld) as captured:
        root.check()
    assert native.coding_worker_held(captured.value)["diagnostic"] == {
        "stage": "root_check",
        "code": "root_lifetime_unavailable",
    }


def test_exception_text_or_custom_str_is_never_used():
    class PrivateError(RuntimeError):
        def __str__(self):
            raise AssertionError("exception string must not be inspected")

    for error in (PrivateError(SECRET), worker.WorkerHeld(SECRET)):
        frame = native.coding_worker_held(error)
        assert frame["diagnostic"] == {"stage": "worker_entry", "code": "source_refusal"}
        assert SECRET not in json.dumps(frame)


@pytest.mark.parametrize("change", ["extra", "stage", "code", "authority", "duplicate", "prefix"])
def test_arbitrary_child_json_is_not_a_typed_source_held_frame(change):
    value = root_failure()
    if change == "extra":
        value["user_text"] = SECRET
    elif change in {"stage", "code"}:
        value["diagnostic"][change] = SECRET
    elif change == "authority":
        value["execution_authorized"] = 0
    raw = json.dumps(value)
    if change == "duplicate":
        raw = raw[:-1] + ',"status":"held"}'
    elif change == "prefix":
        raw = SECRET + raw
    diagnostic = native._coding_held_diagnostic(raw)
    assert diagnostic == {"stage": "coding_b_output", "code": "no_typed_held_frame"}
    assert SECRET not in json.dumps(diagnostic)


def test_summary_is_bounded_non_authorizing_and_contains_no_stream_text():
    done = subprocess.CompletedProcess([], 78, json.dumps(root_failure()), SECRET)
    summary = native.coding_b_observation_summary(done, {"finished": {"cleanup_terminal": True}})
    assert summary["exit_code"] == 78 and summary["cleanup_terminal"] is True
    assert summary["execution_authorized"] is False and summary["acceptance_authorized"] is False
    assert SECRET not in json.dumps(summary)
    done.returncode = True
    with pytest.raises(RuntimeError, match="bounded coding B observation"):
        native.coding_b_observation_summary(done, {})


def test_forged_observation_caller_never_writes_source_custody():
    with pytest.raises(RuntimeError, match="actual source coding B observation required"):
        native.record_coding_b_observation({}, subprocess.CompletedProcess([], 78, "", ""), {})


@pytest.mark.parametrize("failure", [FileNotFoundError, ValueError, PermissionError])
def test_post_observation_tail_preserves_done_and_both_refusals(monkeypatch, failure):
    calls = []

    def refuse_observation(*_args):
        calls.append("observation")
        raise RuntimeError(SECRET)

    def refuse_delivery(*_args):
        calls.append("delivery")
        raise failure(SECRET)

    monkeypatch.setattr(native, "record_coding_b_observation", refuse_observation, raising=False)
    monkeypatch.setattr(native, "record_coding_b_delivery", refuse_delivery)
    done = subprocess.CompletedProcess([], 78, json.dumps(HELD_FRAME), SECRET)
    witness = {"finished": {"cleanup_terminal": True}}
    function = source_function("_observed_correction_b")
    result = detached(
        function.body[-2:],
        {
            "cfg": {"coding_continuation": {}},
            "done": done,
            "witness": witness,
            "native": native,
            "OSError": OSError,
            "ValueError": ValueError,
            "RuntimeError": RuntimeError,
            "KeyError": KeyError,
            "TypeError": TypeError,
            "AttributeError": AttributeError,
        },
    )
    assert result[0] is done and result[1] is witness
    assert calls == ["observation", "delivery"]
    assert witness["coding_b_observation"]["diagnostic"] == HELD_FRAME["diagnostic"]
    assert witness["coding_delivery_diagnostic"]["stage"] == "coding_b_delivery"
    assert witness["coding_observation_diagnostic"]["stage"] == "coding_b_observation"
    assert "reference" not in witness["coding_b_observation"]
    assert "coding_delivery" not in witness
    assert SECRET not in json.dumps(witness)


@pytest.mark.parametrize("denied", [False, True])
def test_delivery_failure_cannot_reach_terminal_receipt_success(denied):
    hooks = object.__new__(runtime.PinnedNativeHooks)

    def forbidden(*_args):
        pytest.fail("a delivery failure must not attempt terminal receipt binding")

    hooks.set_terminal_receipt = forbidden
    problem = {
        "stage": "coding_b_delivery",
        "code": "permission_denied" if denied else "file_missing",
    }
    node = next(
        item
        for item in ast.walk(source_function("run_native_round"))
        if isinstance(item, ast.If)
        and ast.unparse(item.test) == "cfg.get('native_admission') is not None"
        and any(
            isinstance(child, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "receipt_bound" for t in child.targets)
            for child in item.body
        )
    )
    result = detached(
        [
            node,
            ast.Return(
                ast.Tuple(
                    elts=[ast.Name("outcome", ast.Load()), ast.Name("receipt_bound", ast.Load())],
                    ctx=ast.Load(),
                )
            ),
        ],
        {
            "cfg": {"native_admission": {}},
            "cleanup_witness": {"coding_delivery_diagnostic": problem},
            "hooks": hooks,
            "PinnedNativeHooks": runtime.PinnedNativeHooks,
            "done": subprocess.CompletedProcess([], 0, "", ""),
            "outcome": "ok",
            "detail": "dispatcher exit0",
            "json": json,
        },
    )
    assert result == ("denied" if denied else "unknown", False)


def test_final_unknown_keeps_original_refusal_and_secondary_delivery_error():
    function = source_function("run_native_round")
    handler = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.ExceptHandler)
        and any(
            isinstance(child, ast.Constant)
            and child.value == "coding actual B/C terminal proof unavailable"
            for child in ast.walk(node)
        )
    )
    original = {
        "exit_code": 78,
        "cleanup_terminal": True,
        "diagnostic": HELD_FRAME["diagnostic"],
        "execution_authorized": False,
        "acceptance_authorized": False,
    }
    problem = {"stage": "coding_b_delivery", "code": "file_missing"}
    outcome, text = detached(
        handler.body,
        {
            "outcome": "unknown",
            "reported": lambda text: text,
            "json": json,
            "cleanup_witness": {
                "coding_b_observation": original,
                "coding_delivery_diagnostic": problem,
            },
            "native_source": native,
            "error": FileNotFoundError(SECRET),
        },
    )
    value = json.loads(text)
    assert outcome == "unknown"
    assert value["coding_b_observation"] == original
    assert value["coding_delivery_diagnostic"] == problem
    assert value["coding_terminal_diagnostic"]["stage"] == "coding_terminal"
    assert SECRET not in text
