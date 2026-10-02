"""The two gates that are test files report this run's pytest verdicts instead of re-running them.

`verify.py` runs pytest over `tests/`, then runs five gates, and two of those gates ARE test files
pytest has just run — against the same private ledger, in the same run. So every admission check
executed twice per run (three times counting the module's own selftest), and the second time cost
as much as the first. Now pytest writes its per-test XML, `verify.read_junit` turns it into
verdicts, and each gate replays a recorded verdict instead of executing the check again. The gate
keeps its own line and headline in the summary; only the duplicate execution goes.

The direction that matters is the failure direction: a verdict that cannot be read must cost time,
never a check. So these cases pin that an absent, unreadable, foreign or partial record makes the
gate EXECUTE what it has no verdict for, that a recorded skip and a recorded failure surface exactly
as they would have run, and that both real gate scripts are wired to the record at all.

DELIBERATE BREAK -> REVERT, performed 2026-10-02, each reverted by string to a byte-identical file
that ran green again:
  * a recorded skip returned instead of raising: the replay case and both gates' skip cases failed;
  * each gate script calling `fn()` instead of `run_or_replay`: that gate's failure and skip cases
    failed;
  * `read_junit` keeping an xfail: `test_read_junit_keeps_only_what_a_check_can_be_replayed_from`
    failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import env_prereq
import paths
import verify

GATES = ("test_capability_admission", "test_capability_set_coverage")

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_gate" name="test_passes" time="0.1"/>
  <testcase classname="tests.test_gate" name="test_skips" time="0.0">
    <skipped type="pytest.skip" message="the widget ledger is absent">detail</skipped>
  </testcase>
  <testcase classname="tests.test_gate" name="test_fails" time="0.0">
    <failure message="AssertionError: one is not two">trace</failure>
  </testcase>
  <testcase classname="tests.test_gate" name="test_errors" time="0.0">
    <error message="failed on setup with &quot;KeyError&quot;">trace</error>
  </testcase>
  <testcase classname="tests.test_gate" name="test_teardown" time="0.0"/>
  <testcase classname="tests.test_gate" name="test_teardown" time="0.0">
    <error message="failed on teardown">trace</error>
  </testcase>
  <testcase classname="tests.test_gate" name="test_expected" time="0.0">
    <skipped type="pytest.xfail" message="known">detail</skipped>
  </testcase>
  <testcase classname="tests.test_gate.TestGrouped" name="test_in_class" time="0.0"/>
  <testcase classname="test_flat" name="test_in_mirror" time="0.0"/>
</testsuite></testsuites>
"""


def test_read_junit_keeps_only_what_a_check_can_be_replayed_from(tmp_path):
    xml = tmp_path / "pytest.xml"
    xml.write_text(JUNIT)
    got = verify.read_junit(xml)
    assert got["test_gate"] == {
        "test_passes": {"outcome": "passed", "message": ""},
        "test_skips": {"outcome": "skipped", "message": "the widget ledger is absent"},
        "test_fails": {"outcome": "failed", "message": "AssertionError: one is not two"},
        "test_errors": {"outcome": "failed", "message": 'failed on setup with "KeyError"'},
        # A second <testcase> for a teardown error: the more severe verdict wins.
        "test_teardown": {"outcome": "failed", "message": "failed on teardown"},
    }, got["test_gate"]
    # An xfail is NOT replayable, so it is absent and a gate would execute it.
    assert "test_expected" not in got["test_gate"]
    # The flat mirror's classname is the bare stem; a class keys under its own name.
    assert got["test_flat"] == {"test_in_mirror": {"outcome": "passed", "message": ""}}
    assert got["TestGrouped"] == {"test_in_class": {"outcome": "passed", "message": ""}}


@pytest.mark.parametrize("content", [None, "", "<not-xml", "<testsuites></testsuites>"])
def test_an_absent_or_unreadable_junit_yields_no_verdicts(tmp_path, content):
    xml = tmp_path / "pytest.xml"
    if content is not None:
        xml.write_text(content)
    assert verify.read_junit(xml) == {}


def _verdicts(tmp_path: Path, blob) -> Path:
    path = tmp_path / "verdicts.json"
    path.write_text(blob if isinstance(blob, str) else json.dumps(blob))
    return path


def test_recorded_verdicts_are_read_only_from_a_readable_record_for_this_file(tmp_path):
    flag = env_prereq.PYTEST_RESULTS_FLAG
    mine = {"test_a": {"outcome": "passed"}, "test_b": {"outcome": "xpassed"}, "test_c": "junk"}
    good = _verdicts(tmp_path, {"test_gate": mine, "test_other": {"test_a": {"outcome": "failed"}}})
    here = "/anywhere/tests/test_gate.py"
    assert env_prereq.recorded_verdicts([here, flag, str(good)], here) == {
        "test_a": {"outcome": "passed"}
    }
    unreadable = [
        [here],  # no flag at all: run by hand
        [here, flag],  # the flag with no path
        [here, flag, str(tmp_path / "missing.json")],
        [here, flag, str(_verdicts(tmp_path, "{not json"))],
        [here, flag, str(_verdicts(tmp_path, ["a", "list"]))],
    ]
    for argv in unreadable:
        assert env_prereq.recorded_verdicts(argv, here) == {}, argv
    other = "/anywhere/tests/test_unrecorded.py"
    assert env_prereq.recorded_verdicts([other, flag, str(good)], other) == {}


def test_a_replay_surfaces_exactly_as_the_check_would_have():
    ran: list[str] = []

    def test_check():
        ran.append("ran")

    env_prereq.run_or_replay(test_check, {})
    assert ran == ["ran"], "a check with no verdict must execute"
    env_prereq.run_or_replay(test_check, {"test_check": {"outcome": "passed", "message": ""}})
    assert ran == ["ran"], "a recorded pass must not execute the check again"
    skipped = {"test_check": {"outcome": "skipped", "message": "the widget is absent"}}
    with pytest.raises(env_prereq.MissingPrerequisite, match="the widget is absent"):
        env_prereq.run_or_replay(test_check, skipped)
    failed = {"test_check": {"outcome": "failed", "message": "AssertionError: one is not two"}}
    with pytest.raises(AssertionError) as caught:
        env_prereq.run_or_replay(test_check, failed)
    assert str(caught.value) == "one is not two"
    assert ran == ["ran"]


def _checks(stem: str) -> list[str]:
    """The checks a gate script runs, by the rule its `main()` uses."""
    module = __import__(stem)
    return sorted(k for k, v in vars(module).items() if k.startswith("test_") and callable(v))


def _run_gate(stem: str, tmp_path: Path, verdicts: dict) -> subprocess.CompletedProcess:
    record = _verdicts(tmp_path, {stem: verdicts})
    env = {k: v for k, v in os.environ.items() if k not in ("ORCH_CAPABILITY_HEARTBEATS",)}
    env.update(
        PYTHONPATH=str(paths.MODULE_DIR),
        ORCH_CAPABILITIES_PATH=str(tmp_path / "ledger" / "capabilities.json"),
        ORCH_FEEDBACK_DB=str(tmp_path / "brain" / "orchestrator.db"),
        ORCH_STATE_DIR=str(tmp_path / "state"),
    )
    return subprocess.run(
        [
            sys.executable,
            str(paths.TESTS_DIR / f"{stem}.py"),
            env_prereq.PYTEST_RESULTS_FLAG,
            str(record),
        ],
        cwd=paths.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.mark.parametrize("stem", GATES)
def test_each_gate_reports_a_recorded_failure_without_running_the_check(stem, tmp_path):
    names = _checks(stem)
    assert names, f"{stem} defines no checks"
    proc = _run_gate(
        stem, tmp_path, {n: {"outcome": "failed", "message": "recorded failure"} for n in names}
    )
    out = proc.stdout
    assert proc.returncode == 1, out[-2000:] + proc.stderr[-2000:]
    for name in names:
        assert f"  FAIL {name}  (pytest verdict, this run)" in out, (name, out[-2000:])
    assert out.count("recorded failure") == len(names), out[-2000:]


@pytest.mark.parametrize("stem", GATES)
def test_each_gate_reports_a_recorded_skip_with_its_reason(stem, tmp_path):
    names = _checks(stem)
    proc = _run_gate(
        stem,
        tmp_path,
        {n: {"outcome": "skipped", "message": "the widget is absent"} for n in names},
    )
    out = proc.stdout
    assert proc.returncode == 0, out[-2000:] + proc.stderr[-2000:]
    for name in names:
        assert f"  SKIP {name}  (pytest verdict, this run)" in out, (name, out[-2000:])
    marked = [ln for ln in out.splitlines() if env_prereq.PREREQ_ABSENT_MARK in ln]
    assert len(marked) == len(names) and all("the widget is absent" in ln for ln in marked), marked


def test_a_check_with_no_recorded_verdict_is_executed(tmp_path):
    stem = "test_capability_admission"
    names = _checks(stem)
    unrecorded = "test_waivers_are_bounded_not_just_dated"
    assert unrecorded in names
    verdicts = {
        n: {"outcome": "failed", "message": "recorded failure"} for n in names if n != unrecorded
    }
    proc = _run_gate(stem, tmp_path, verdicts)
    lines = [ln for ln in proc.stdout.splitlines() if ln.rstrip().endswith(unrecorded)]
    assert len(lines) == 1, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "(pytest verdict" not in lines[0], lines[0]
