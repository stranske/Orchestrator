"""Negative source-inspection/interface mechanics; no native authority or process."""

import ast
import inspect
import sys
from types import SimpleNamespace

import pytest

import closer_coding_delivery as delivery
import closer_runtime_binding as binding


@pytest.mark.parametrize("name", ["_begin", "_verify", "_revoke", "require_coding_delivery"])
@pytest.mark.parametrize("frame_kind", ["actual", "none"])
def test_unavailable_frame_inspection_refuses_before_registry_effect(monkeypatch, name, frame_kind):
    frame = sys._getframe() if frame_kind == "actual" else None
    before = dict(delivery._ACTIVE)
    # An unavailable inspection primitive is negative DATA, never a fabricated
    # authorized caller frame or successful source validator.
    monkeypatch.setattr(delivery, "inspect", SimpleNamespace(currentframe=lambda: None))
    with pytest.raises(ValueError, match="original.*coding|coding.*required"):
        getattr(delivery, name)(frame)
    assert delivery._ACTIVE == before


@pytest.mark.parametrize("missing", ["B", "pinned_hook"])
def test_concrete_completion_interface_narrowing_follows_real_validator(missing):
    node = ast.parse(inspect.getsource(binding.run_native_round)).body[0]
    coding = next(
        n
        for n in node.body
        if isinstance(n, ast.If)
        and ast.unparse(n.test) == "cfg.get('coding_continuation') is not None"
    )
    body = next(n for n in coding.body if isinstance(n, ast.Try)).body
    validator = next(
        i
        for i, n in enumerate(body)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(x, ast.Call)
            and isinstance(x.func, ast.Attribute)
            and x.func.attr == "finalize_coding_native"
            for x in ast.walk(n)
        )
    )
    guard_index = next(
        i
        for i, n in enumerate(body)
        if isinstance(n, ast.If)
        and any(
            isinstance(x, ast.Constant)
            and x.value == "actual coding completion lacks pinned B/C results"
            for x in ast.walk(n)
        )
    )
    assert validator < guard_index
    actual_guard = body[guard_index]
    namespace = {
        "terminal_b": None if missing == "B" else SimpleNamespace(returncode=0),
        "hooks": object.__new__(binding.PinnedNativeHooks) if missing == "B" else object(),
        "PinnedNativeHooks": binding.PinnedNativeHooks,
        "BindingError": binding.BindingError,
    }
    # Only the actual rejection block executes; no completion validator is
    # stubbed successful and no source/native completion is inferred.
    with pytest.raises(binding.BindingError, match="pinned B/C results"):
        exec(
            compile(
                ast.Module(body=[actual_guard], type_ignores=[]),
                "actual-postvalidation-interface-guard",
                "exec",
            ),
            namespace,
        )
