"""merge_guard: a caller's exact-head acceptance PASS supplies an unrun runtime-AC gate's verdict."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import merge_guard  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40


def meta(_target):
    return {"state": "OPEN", "is_draft": False, "labels": ["acceptance-criteria"], "title": "t"}


def gate(status, blocks=True):
    return lambda item, **_: {"status": status, "blocks": blocks}


def evaluate(gate_fn, evidence, head=HEAD):
    return merge_guard.evaluate_merge_gate(
        "stranske/demo#1",
        dry_run=False,
        metadata_fn=meta,
        gate_fn=gate_fn,
        expected_head=head,
        acceptance_evidence=evidence,
    )


def test_unrun_gate_is_satisfied_by_an_exact_head_pass():
    # stranske/Orchestrator#1354 (2026-11-12): a label armed the gate, its runner was off in
    # closer-core, and the PR was closed unmerged on `runtime AC gate required_but_not_run`.
    for status in ("required_but_not_run", "missing_spec"):
        result = evaluate(gate(status), {"head": HEAD, "result": "PASS", "source": "closer-core"})
        assert result["blocked"] is False, result
        assert result["gate"]["status"] == "satisfied_by_caller_evidence"
        assert result["gate"]["unrun_status"] == status


def test_unrun_gate_still_blocks_without_matching_pass():
    for evidence in (
        None,
        {"head": HEAD, "result": "FAIL"},
        {"head": HEAD, "result": "UNRUNNABLE"},
        {"head": OTHER, "result": "PASS"},
        {"head": HEAD[:12], "result": "PASS"},
    ):
        result = evaluate(gate("required_but_not_run"), evidence)
        assert result["blocked"] is True, evidence
        assert result["reason"] == "runtime AC gate required_but_not_run"
    assert evaluate(gate("required_but_not_run"), {"head": HEAD, "result": "PASS"}, head=None)[
        "blocked"
    ]


def test_an_executed_gate_verdict_is_never_overridden():
    result = evaluate(gate("executed"), {"head": HEAD, "result": "PASS"})
    assert result["blocked"] is True
    assert result["reason"] == "runtime AC gate executed"


def test_guarded_merge_passes_the_evidence_through():
    seen = {}

    def fake_evaluate(target, **kwargs):
        seen.update(kwargs)
        return {"target": target, "blocked": True, "reason": "stop here", "metadata": {}}

    original = merge_guard.evaluate_merge_gate
    merge_guard.evaluate_merge_gate = fake_evaluate
    try:
        merge_guard.guarded_merge(
            "stranske/demo#1",
            expected_head=HEAD,
            preflight_fn=lambda *_a, **_k: {"blocked": False},
            acceptance_evidence={"head": HEAD, "result": "PASS"},
        )
    finally:
        merge_guard.evaluate_merge_gate = original
    assert seen["expected_head"] == HEAD
    assert seen["acceptance_evidence"] == {"head": HEAD, "result": "PASS"}
