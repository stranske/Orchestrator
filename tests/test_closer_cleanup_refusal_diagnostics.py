"""Retain a cleanup refusal without signaling an unverified process."""

import json
from types import SimpleNamespace

import pytest
from test_closer_native_production_composition import _fixture_worker_cleanup


def test_identity_refusal_retains_owned_boundary_and_prior_exception(tmp_path, monkeypatch):
    import test_closer_native_production_composition as composition

    obj = SimpleNamespace(
        path=tmp_path / "state.json",
        read=lambda: {"workers": [{"worker_id": "worker-1", "pid": 7654321}]},
    )
    monkeypatch.setattr(
        composition.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="different process\n"),
    )
    monkeypatch.setattr(
        composition.os, "killpg", lambda *_a: pytest.fail("unverified process signaled")
    )
    receipt = tmp_path / "refusal.json"
    try:
        raise RuntimeError("original test-body failure")
    except RuntimeError:
        with pytest.raises(AssertionError, match="refusing cleanup") as caught:
            _fixture_worker_cleanup(obj, {"module_path": "/owned/module.py"}, tmp_path, receipt)
    result = json.loads(receipt.read_text())
    assert result["prior_exception_type"] == "RuntimeError"
    assert result["observed_lstart_command"] == "different process\n"
    assert result["signal_attempted"] is False
    assert result["result_exists"] is False
    assert result["expected"][0] == str(obj.path)
    assert '"prior_exception_type": "RuntimeError"' in str(caught.value)
