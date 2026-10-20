"""Closer Core sibling skips are bounded separately from mirror git-family skips."""

from __future__ import annotations

import env_prereq
import verify


def _actual(*, other: int, core: int) -> dict:
    return {
        "skipped_max": other,
        "core_sibling_skipped_max": core,
        "selftest_skipped_max": 0,
        "gate_skipped_max": 0,
        "mypy_exempt_max": 0,
    }


def test_core_sibling_mark_partitions_mirror_skip_ceiling():
    mark = env_prereq.CORE_SIBLING_ABSENT_MARK
    py = {
        "skipped": 4,
        "verdicts": {
            "tests.test_x": {
                "a": {"outcome": "skipped", "message": f"{mark} missing sibling"},
                "b": {"outcome": "skipped", "message": "not a git repository"},
                "c": {"outcome": "skipped", "message": f"{mark} missing packager"},
                "d": {"outcome": "passed", "message": ""},
            }
        },
    }
    assert verify.core_sibling_skips(py) == 2
    floor = {"mirror_skipped_max": 2, "mirror_core_sibling_skipped_max": 2}
    actual = _actual(other=2, core=2)
    assert verify._ceiling_problems(floor, actual, shape=verify.EXEC_MIRROR) == []
    # One unrelated new skip still trips the general mirror ceiling; core stays in its bucket.
    over = verify._ceiling_problems(floor, _actual(other=3, core=2), shape=verify.EXEC_MIRROR)
    assert len(over) == 1 and "mirror_skipped_max" in over[0]
    assert "Skip categories:" in over[0]
    assert (
        "Closer Core sibling skipped test(s) 2/2 via `mirror_core_sibling_skipped_max`" in over[0]
    )
    assert "skipped test(s) 3/2 via `mirror_skipped_max`" in over[0]
    assert "CLOSER_CORE_SOURCE" in over[0]
    assert "git family" in over[0]


def test_core_sibling_ceiling_breach_names_categories():
    floor = {"mirror_skipped_max": 21, "mirror_core_sibling_skipped_max": 19}
    over = verify._ceiling_problems(floor, _actual(other=21, core=20), shape=verify.EXEC_MIRROR)
    assert len(over) == 1 and "mirror_core_sibling_skipped_max" in over[0]
    assert "Skip categories:" in over[0]
    assert (
        "Closer Core sibling skipped test(s) 20/19 via `mirror_core_sibling_skipped_max`" in over[0]
    )
    assert "skipped test(s) 21/21 via `mirror_skipped_max`" in over[0]
    assert "CLOSER_CORE_SOURCE" in over[0]
