"""`reachable_set` — the front-door measurement, which until now nothing called.

HOW THIS FILE WAS FOUND, because the method is the point. `escaped_defect_priority` ranked
`src/capability_advisor.py` first: most fix commits in the window, most churn. Against the honest
COMBINED coverage report — pytest plus the `--selftest` subprocesses — the module reads 95.1%, and
`reachable_set`'s entire body sits in the 64 statements that remain. A grep then showed why: the
function has no caller anywhere in `src/` or `tests/`, and `_selftest_reach`, which its docstring
credits, calls `advise()` directly and reimplements its own assertions.

So the docstring's promise — "a shrinking front door now fails a test instead of going quiet" —
was not true. The function that measures whether capabilities can still be found was itself the
thing nobody could find. That is this repository's documented failure mode, and no amount of
pytest-only coverage could have surfaced it: pytest-only reports this module at 12.0% and cannot
tell an unexecuted guard from the 79 modules it simply cannot see.

These are pytest rather than selftest cases so `local_verify` can grade them per node, and so the
caller is one the whole suite runs rather than one more assertion inside a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import capabilities
import capability_advisor as ca

# A matcher that free text reaches: `advise` routes by task_type, and TASK_SIGNALS' first phrase
# for `testgen` is what `reachable_set` probes with.
ROUTED = {"field": "task_type", "operator": "in", "value": ["testgen"]}
# A matcher no free text can reach: it engages only by ENTERING a named gate.
GATE_ONLY = {"kind": "closer_gate", "name": "high_stakes_review"}


def _ledger(tmp_path: Path, **matchers: dict) -> Path:
    path = tmp_path / "capabilities.json"
    rows = {}
    for name, matcher in matchers.items():
        row = capabilities._blank_capability(name)
        row["status"] = "generated"
        row["matcher"] = matcher
        rows[name] = row
    capabilities.save(rows, path)
    return path


def test_a_capability_reachable_from_free_text_is_reported_with_its_task_types(tmp_path):
    out = ca.reachable_set(path=_ledger(tmp_path, routed_lane=ROUTED))
    assert out["reachable"]["routed_lane"] == ["testgen"]


def test_a_gate_only_capability_is_not_counted_as_reachable(tmp_path):
    """Reach means "findable from free text". Counting gate-entered capabilities would inflate it.

    The number would then stay healthy while the front door narrowed, which is the exact silence
    this function exists to break.
    """
    out = ca.reachable_set(path=_ledger(tmp_path, routed_lane=ROUTED, gate_only=GATE_ONLY))
    assert "routed_lane" in out["reachable"]
    assert "gate_only" not in out["reachable"]


def test_the_two_counts_are_not_a_ratio(tmp_path):
    """The second defect this function carried while nothing called it.

    `reachable` comes from `advise`, which loads through `capabilities.load` and so sees
    KNOWN_DECLARATIONS seeds. `declared_count` comes from `load_declared`, which reports only what
    the ledger itself declares. They count different populations, so `reachable_count /
    declared_count` is not a fraction — an EMPTY ledger measures reachable 1 of declared 0.
    Asserted directly, because the two names invite a division that means nothing.
    """
    empty = ca.reachable_set(path=_ledger(tmp_path))
    assert empty["declared_count"] == 0
    assert empty["reachable_count"] > 0, (
        "a seeded declaration is reachable with nothing declared — which is exactly why these "
        "two numbers must never be presented as a fraction"
    )


def test_the_actionable_set_is_reported_not_left_to_be_derived(tmp_path):
    """A declared capability no free text reaches is the drainable quantity, and it is named.

    Two numbers that do not divide cannot say WHICH capability left the front door. The list can,
    and it is the half a reader can act on.
    """
    out = ca.reachable_set(path=_ledger(tmp_path, routed_lane=ROUTED, gate_only=GATE_ONLY))
    assert "gate_only" in out["unreachable_declared"]
    assert "routed_lane" not in out["unreachable_declared"]
    assert out["unreachable_count"] == len(out["unreachable_declared"])


def test_tightening_a_matcher_makes_a_capability_leave_the_front_door(tmp_path):
    """The regression the docstring describes, reproduced in both directions.

    `adversarial-review` and `docs-drift-fix-agent` both carried advisory match history — they were
    once reachable — and left the front door when their matchers were tightened to gate shapes.
    Nothing noticed, because reach was never measured. Asserting the BEFORE as well as the AFTER is
    what makes this a regression test rather than a description of today's behaviour.
    """
    before = ca.reachable_set(path=_ledger(tmp_path / "before", was_reachable=ROUTED))
    assert "was_reachable" in before["reachable"]

    after = ca.reachable_set(path=_ledger(tmp_path / "after", was_reachable=GATE_ONLY))
    assert "was_reachable" not in after["reachable"], (
        "tightening a matcher to a gate shape must REMOVE the capability from the reachable set; "
        "if it does not, the measurement cannot detect the shrinkage it exists to detect"
    )
    assert after["reachable_count"] < before["reachable_count"]


def test_a_healthy_ledger_reaches_something_so_a_total_break_is_loud(tmp_path):
    """Zero reach against a non-empty ledger is the alarm, so the healthy case must not be zero.

    If `advise` ever stopped matching anything at all, every capability would fall out at once and
    `reachable_count` would read 0. That is only legible as a failure if a working ledger reliably
    produces a non-zero number, which is what this pins.
    """
    out = ca.reachable_set(path=_ledger(tmp_path, routed_lane=ROUTED))
    assert out["declared_count"] > 0
    assert out["reachable_count"] > 0


def test_every_task_type_is_probed(tmp_path):
    """Reach is measured across the whole signal table, not a sample of it.

    A capability reachable only from a task type the probe skipped would be reported as unreachable
    — a false alarm — and, worse, one that BECAME unreachable there would never be noticed.
    """
    assert ca.TASK_SIGNALS, "the signal table must not be empty"
    for task_type, signals in ca.TASK_SIGNALS.items():
        assert signals, f"{task_type} has no signal phrases, so nothing probes it"

    out = ca.reachable_set(path=_ledger(tmp_path, routed_lane=ROUTED))
    # EQUALITY against the whole table, not containment. The first version of this test asserted
    # only that the task types it saw were a SUBSET of the table — which is true of any prefix of
    # it, so cutting the probe to a single task type left this test green. It looked like a guard
    # and was not; the break demo is what showed it. `probed_task_types` exists so the claim is
    # checkable rather than inferable from whatever happened to match.
    assert out["probed_task_types"] == sorted(ca.TASK_SIGNALS), (
        "reach must be measured across the WHOLE signal table: a capability that became "
        "unreachable from a task type nobody probed would never be noticed"
    )
    named = {t for types in out["reachable"].values() for t in types}
    assert named <= set(ca.TASK_SIGNALS), named - set(ca.TASK_SIGNALS)


def test_an_empty_ledger_reports_zero_reach_without_raising(tmp_path):
    """A ledger with nothing in it is a legitimate state, not an error."""
    out = ca.reachable_set(path=_ledger(tmp_path))
    assert out["declared_count"] == 0
    assert out["unreachable_declared"] == []
    assert isinstance(out["reachable"], dict)


@pytest.mark.parametrize(
    "key",
    [
        "reachable",
        "reachable_count",
        "declared_count",
        "unreachable_declared",
        "unreachable_count",
        "probed_task_types",
    ],
)
def test_the_report_shape_is_stable(tmp_path, key):
    """A consumer reading a renamed key would see absence, which reads as zero reach."""
    assert key in ca.reachable_set(path=_ledger(tmp_path, routed_lane=ROUTED))
