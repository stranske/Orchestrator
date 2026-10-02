"""verify.py runs the module selftests several at a time, and reports them identically at any width.

Serially the 98 selftests were the longest phase of a run. They are independent processes, so they
now run `ORCH_VERIFY_JOBS` at a time (default: up to 8). Two things must hold for that to be safe,
and both are pinned here by behaviour:

  * they really do run at once — two selftests that each wait for the other can only both pass if
    they overlap, which no amount of fast serial execution can fake;
  * the RESULT is independent of the width and of finishing order — the ok list, the failures and
    the skips come back in discovery order, so the summary reads the same at 1 and at 8.

DELIBERATE BREAK -> REVERT, performed 2026-10-02: forcing one worker failed
`test_selftests_run_at_the_same_time` (the first side never met the second), and classifying in
scheduling order instead of discovery order failed `test_the_result_does_not_depend_on_the_width`.
Both reverted by string to byte-identical files that ran green again.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import verify

# Each side writes its own marker, then waits for the other's. The wait is a BOUND for the broken
# case, not a threshold the passing case races: overlapping processes meet in milliseconds.
_RENDEZVOUS = """
import pathlib, sys, time
if "--selftest" in sys.argv:
    here = pathlib.Path({folder!r})
    (here / "{me}.ready").write_text("x")
    deadline = time.monotonic() + 30
    while not (here / "{other}.ready").exists():
        if time.monotonic() > deadline:
            print("{me} never met {other}: the selftests ran one at a time")
            sys.exit(1)
        time.sleep(0.01)
    print("{me} selftest: OK (met {other})")
"""


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "MODULES", tmp_path)
    monkeypatch.setattr(verify, "HERE", tmp_path)
    return tmp_path


def _write(folder: Path, name: str, body: str) -> None:
    (folder / f"{name}.py").write_text(body)


def test_selftests_run_at_the_same_time(modules):
    for me, other in (("left", "right"), ("right", "left")):
        _write(modules, me, _RENDEZVOUS.format(folder=str(modules), me=me, other=other))
    got = verify.run_selftests(["left", "right"], jobs=2)
    assert got == {"ok": ["left", "right"], "failed": {}, "skipped": {}}, got


def test_the_result_does_not_depend_on_the_width(modules):
    mark = verify.PREREQ_ABSENT_MARK
    # Sizes differ so that the scheduling order (largest first) is NOT the discovery order.
    bodies = {
        "a_small": 'import sys\nif "--selftest" in sys.argv:\n    print("a_small selftest: OK")\n',
        "b_large": 'import sys\nif "--selftest" in sys.argv:\n    print("b_large selftest: OK")\n'
        + "# padding\n" * 400,
        "c_fails": 'import sys\nif "--selftest" in sys.argv:\n    sys.exit("c_fails: broken")\n',
        "d_skips": 'import sys\nif "--selftest" in sys.argv:\n'
        f'    print("d_skips selftest: {mark} the widget is absent")\n',
        "e_silent": 'import sys\nif "--selftest" in sys.argv:\n    sys.exit(0)\n',
        "f_medium": 'import sys\nif "--selftest" in sys.argv:\n    print("f_medium selftest: OK")\n'
        + "# padding\n" * 40,
    }
    for name, body in bodies.items():
        _write(modules, name, body)
    names = list(bodies)
    serial = verify.run_selftests(names, jobs=1)
    wide = verify.run_selftests(names, jobs=4)
    assert wide == serial, (serial, wide)
    assert serial["ok"] == ["a_small", "b_large", "f_medium"], serial["ok"]
    assert sorted(serial["failed"]) == ["c_fails", "e_silent"], serial["failed"]
    assert serial["skipped"] == {"d_skips": ["the widget is absent"]}, serial["skipped"]


@pytest.mark.parametrize(
    "raw,width,said",
    [
        (None, None, "at a time"),
        ("3", 3, f"3 at a time ({verify.JOBS_ENV}=3)"),
        ("1", 1, f"1 at a time ({verify.JOBS_ENV}=1)"),
        ("zero", None, "is not a positive integer"),
        ("0", None, "is not a positive integer"),
    ],
)
def test_the_width_is_always_said_and_a_bad_setting_is_named(monkeypatch, raw, width, said):
    if raw is None:
        monkeypatch.delenv(verify.JOBS_ENV, raising=False)
    else:
        monkeypatch.setenv(verify.JOBS_ENV, raw)
    jobs, phrase = verify.selftest_jobs()
    default = max(1, min(verify.DEFAULT_JOBS, os.cpu_count() or 1))
    assert jobs == (width or default), (raw, jobs)
    assert said in phrase, (raw, phrase)
