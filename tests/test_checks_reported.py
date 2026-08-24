"""An absent check must not read like a passing one — the rule, held as a test.

Only the PURE part is exercised here. The rest of `check_checks_reported.py` is GitHub API calls,
and a test that mocked them would assert my idea of the API rather than the threshold rule, which
is the part with a decision in it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_checks_reported",
    Path(__file__).resolve().parent.parent / "scripts" / "check_checks_reported.py",
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_a_check_that_normally_runs_is_expected():
    """Seen on 12 of 12 merged PRs: structural, so its absence is a finding."""
    assert "python ci / lint-ruff" in mod.expected_from_counts({"python ci / lint-ruff": 12}, 12)


def test_an_event_driven_check_is_not_expected():
    """Seen once in 12: a keepalive round or label-gated job. Demanding it would cry wolf.

    This is the case that killed the union design: it reported 25-26 absences on entirely healthy
    PRs, and a check that cries wolf 25 times gets waived.
    """
    assert mod.expected_from_counts({"Keepalive next task (Claude)": 1}, 12) == set()


def test_the_threshold_never_falls_to_one():
    """With a tiny sample the fraction rounds toward 1, which would expect every one-off.

    DELIBERATE BREAK -> REVERT: drop the `max(2, ...)` floor and this fails at 1 sighting of 2,
    because int(2 * 0.75) == 1. The floor is load-bearing, not decorative.
    """
    assert mod.expected_from_counts({"ran-once": 1}, 2) == set()
    assert mod.expected_from_counts({"ran-twice": 2}, 2) == {"ran-twice"}


def test_a_holed_pr_cannot_erode_the_expected_set():
    """A merged PR that reported NOTHING must not dilute the denominator.

    Otherwise a run of held PRs erodes the expected set until nothing is expected — the check
    quietly disarming itself, which is the defect it exists to catch wearing the other hat. The
    caller enforces this by never appending such a PR to `contributors`; asserted here on the
    arithmetic it protects.
    """
    # 8 real contributors, a check seen on 6 of them: expected (6 >= int(8*0.75) == 6).
    assert mod.expected_from_counts({"summary": 6}, 8) == {"summary"}
    # Had two holed PRs been counted, the threshold would rise to 7 and the check would vanish
    # from the expected set — the erosion this guards against.
    assert mod.expected_from_counts({"summary": 6}, 10) == set()
