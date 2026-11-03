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
                "c": {"outcome": "skipped", "message": f"{mark} missing runtime"},
                "d": {"outcome": "passed", "message": ""},
                "e": {"outcome": "skipped", "message": "another unrelated skip"},
            }
        },
    }
    assert verify.core_sibling_skips(py) == 2
    floor = {"mirror_skipped_max": 2, "mirror_core_sibling_skipped_max": 2}
    core_skips = verify.core_sibling_skips(py)
    actual = _actual(other=py["skipped"] - core_skips, core=core_skips)
    assert verify._ceiling_problems(floor, actual, shape=verify.EXEC_MIRROR) == []
    # One unrelated new skip still trips the general mirror ceiling; core stays in its bucket.
    py["verdicts"]["tests.test_x"]["f"] = {"outcome": "skipped", "message": "new unrelated skip"}
    py["skipped"] += 1
    core_skips = verify.core_sibling_skips(py)
    actual = _actual(other=py["skipped"] - core_skips, core=core_skips)
    over = verify._ceiling_problems(floor, actual, shape=verify.EXEC_MIRROR)
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


def test_core_sibling_skips_uncountable_without_verdicts():
    """Skips with no junit verdicts must not invent a Core count of zero.

    Otherwise 20 Core-sibling skips pass both `mirror_skipped_max=21` and
    `mirror_core_sibling_skipped_max=19` because every skip is charged to neither ceiling.
    """
    assert verify.core_sibling_skips({"skipped": 20}) is None
    assert verify.core_sibling_skips({"skipped": 0}) == 0
    assert verify.core_sibling_skips({"skipped": 0, "verdicts": {}}) == 0
    floor = {"mirror_skipped_max": 21, "mirror_core_sibling_skipped_max": 19}
    over = verify._ceiling_problems(
        floor,
        {
            "skipped_max": None,
            "core_sibling_skipped_max": None,
            "selftest_skipped_max": 0,
            "gate_skipped_max": 0,
            "mypy_exempt_max": 0,
        },
        shape=verify.EXEC_MIRROR,
    )
    assert len(over) == 2
    assert all("CEILING UNCHECKABLE" in msg for msg in over)
    assert any("mirror_skipped_max" in msg for msg in over)
    assert any("mirror_core_sibling_skipped_max" in msg for msg in over)


def test_junit_core_skips_survive_duplicate_class_and_method(tmp_path, monkeypatch):
    from types import SimpleNamespace

    mark = env_prereq.CORE_SIBLING_ABSENT_MARK
    junit = tmp_path / "pytest.xml"
    junit.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_a.TestShared" name="test_same">'
        '<skipped message="unrelated"/></testcase>'
        '<testcase classname="tests.test_b.TestShared" name="test_same">'
        f'<skipped message="{mark} missing runtime"/></testcase>'
        '<testcase classname="tests.test_c.TestExpected" name="test_same">'
        '<skipped type="pytest.xfail" message="expected failure"/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="2 skipped, 1 xfailed in 0.01s", stderr="", returncode=0
        ),
    )
    py = verify.run_pytest(junit=junit)
    # Gate replay keeps its existing deduplication and xfail exclusion.
    assert py["verdicts"] == {
        "TestShared": {"test_same": {"outcome": "skipped", "message": "unrelated"}},
        "TestExpected": {},
    }
    core_skips = verify.core_sibling_skips(py)
    assert core_skips == 1
    actual = _actual(other=py["skipped"] - core_skips, core=core_skips)
    over = verify._ceiling_problems(
        {"mirror_skipped_max": 1, "mirror_core_sibling_skipped_max": 0},
        actual,
        shape=verify.EXEC_MIRROR,
    )
    assert len(over) == 1 and "mirror_core_sibling_skipped_max" in over[0]
