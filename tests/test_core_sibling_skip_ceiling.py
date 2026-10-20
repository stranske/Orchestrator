"""Closer Core sibling skips are bounded separately from mirror git-family skips."""

from __future__ import annotations

import env_prereq
import verify


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
    actual = {
        "skipped_max": py["skipped"] - 2,
        "core_sibling_skipped_max": 2,
        "selftest_skipped_max": 0,
        "gate_skipped_max": 0,
        "mypy_exempt_max": 0,
    }
    assert verify._ceiling_problems(floor, actual, shape=verify.EXEC_MIRROR) == []
    actual["skipped_max"] = 3
    over = verify._ceiling_problems(floor, actual, shape=verify.EXEC_MIRROR)
    assert len(over) == 1 and "mirror_skipped_max" in over[0]
