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


def test_the_ratchet_only_raises(tmp_path, monkeypatch):
    """A name that has ever been expected stays expected until a human deletes its line.

    THE CASE THIS EXISTS FOR, found by dogfooding rather than by review: while `pr-00-gate.yml` sat
    held, every newly merged PR merged WITHOUT the Gate, so after twelve such merges the Gate's
    checks no longer appeared on 75% of the reference window and stopped counting as "normally
    reporting". The expected set fell 23 -> 14 and PR #91 was pronounced clean by the very tool
    written to catch that. A sustained outage is the case that matters most and it was the one the
    frequency rule could not see.

    DELIBERATE BREAK -> REVERT: make `reference_set` return `observed` instead of
    `observed | ratchet_names()` and this fails — the ratchet is present but unconsulted, which is
    this repo's founding defect (built and not wired).
    """
    ratchet = tmp_path / "expected-checks.json"
    ratchet.write_text('{"expected": ["python ci / lint-ruff", "summary"]}', encoding="utf-8")
    monkeypatch.setattr(mod, "RATCHET", ratchet)
    assert mod.ratchet_names() == {"python ci / lint-ruff", "summary"}

    # An absent file means "nothing ratcheted yet", never zero-expected: reading a missing ratchet
    # as an empty expectation would silently disable the check on a fresh checkout.
    monkeypatch.setattr(mod, "RATCHET", tmp_path / "does-not-exist.json")
    assert mod.ratchet_names() == set()


def test_the_ratchet_is_wired_into_the_expected_set():
    """The union with the ratchet must happen in `reference_set`, not merely be available."""
    src = (
        Path(__file__).resolve().parent.parent / "scripts" / "check_checks_reported.py"
    ).read_text(encoding="utf-8")
    # Split so the needle cannot match this line, and pinned to the FRAGMENT that carries the
    # meaning rather than the whole statement — CLAUDE.md's wiring-pin rule.
    needle = "observed | " + "ratchet_names()"
    # COUNT, not `in`: a pin proves the wiring only while its needle matches ONE site. Add a
    # second call site and deleting the one the pin MEANS leaves it green — a guard against
    # built-but-not-wired that is itself no longer wired. Measured 2026-08-29 across all six pins
    # in this repo: every one matched exactly once, so this asserts a property that HOLDS rather
    # than fixing a break, and it is the decay that it makes loud.
    assert src.count(needle) == 1, (
        f"the ratchet-union needle matches {src.count(needle)} site(s), not 1. At 0, "
        "reference_set no longer unions the ratchet and the expected set can erode to nothing "
        "during a sustained outage — exactly when this check matters. Above 1, the pin has "
        "stopped discriminating: narrow it to the site that carries the meaning."
    )
