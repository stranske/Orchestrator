"""Coverage must measure the tests this project actually runs, and must never become a gate.

The scar: the Gate reported 48.45% coverage and a hotspot list headed by `outcomes.py` at 9.0%,
`watch.py` at 9.6%, `capability_recurrence_check.py` at 10.4%. Every one of the twelve modules it
named as worst is a module whose ONLY test is a `--selftest` — and `verify.py` runs those as
SUBPROCESSES, which a coverage run wrapped around pytest cannot see. Measured on 2026-08-23, under
their own selftests: `outcomes.py` 62%, `adversarial.py` 88%, `gh_capacity.py` 86%,
`keepalive_outcomes.py` 78%, all four exiting 0. 78 modules have no `test_*.py` at all — about
85,500 lines, 79.6% of non-test root Python.

So the number was not measuring a testing gap. It was reporting a blind spot and calling it a score,
which is this repository's founding defect in a new costume: a check that quietly passes over what it
never looked at.

What is asserted here:

1.  **Off by default, and the default path is byte-identical.** `child_argv` is the identity
    function unless `--coverage` is passed. Instrumenting ~90 subprocesses is much slower, and the
    default invocation is the one that produces the verdict — so turning measurement on must be a
    deliberate act, never a side effect.
2.  **All three runners are wrapped, not just pytest.** Wrapping pytest alone would reproduce the
    exact bug: selftests and gates are where most of the execution is.
3.  **Both child forms survive wrapping** — `module.py --selftest` and `-m pytest ...`. Coverage has
    to be inserted after the interpreter, not prepended to the command.
4.  **Absent data is reported as absent.** No data file must read as "no measurement", never as 0%
    and never as silence — the same three-way discipline `run_selftests` uses, for the same reason.
5.  **Coverage never changes the exit code.** It is a measurement, not a gate. Making it a gate here
    would enforce a threshold nobody agreed to, against a number that was wrong until this change —
    and would reward writing pytest wrappers around already-tested modules, which raises the metric
    and adds no assurance.
6.  **`.coveragerc` keeps `parallel = true`.** Without it every child overwrites one data file and
    the combine is meaningless — the failure would look like a plausible-but-wrong number, which is
    worse than no number.

DELIBERATE BREAK -> REVERT, performed 2026-08-23: setting `parallel = false` in `.coveragerc` failed
`test_coveragerc_enables_parallel_mode`; removing `child_argv` from the selftest runner failed
`test_all_three_runners_are_instrumented[selftests]`. Both reverted byte-identical and passed again.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

import verify

HERE = Path(__file__).resolve().parent
COVERAGERC = HERE / ".coveragerc"
VERIFY_SRC = (HERE / "verify.py").read_text(encoding="utf-8")


def test_coverage_is_off_by_default():
    assert verify.COVERAGE is False, (
        "verify.COVERAGE must default to False. Instrumenting ~90 subprocesses is much slower, and "
        "the default invocation is the one that produces the verdict — measurement must be opt-in."
    )


def test_child_argv_is_identity_when_coverage_is_off(monkeypatch):
    monkeypatch.setattr(verify, "COVERAGE", False)
    argv = ["/usr/bin/python3", "outcomes.py", "--selftest"]
    assert verify.child_argv(argv) == argv, (
        "with coverage off, child_argv must return the command UNCHANGED. Any difference means the "
        "default verdict path is no longer the path that was verified."
    )


@pytest.mark.parametrize(
    "argv,expected_tail",
    [
        (["py", "outcomes.py", "--selftest"], ["outcomes.py", "--selftest"]),
        (["py", "-m", "pytest", "-q"], ["-m", "pytest", "-q"]),
        (
            ["py", "capabilities.py", "--json", "validate"],
            ["capabilities.py", "--json", "validate"],
        ),
    ],
)
def test_child_argv_inserts_coverage_after_the_interpreter(monkeypatch, argv, expected_tail):
    monkeypatch.setattr(verify, "COVERAGE", True)
    got = verify.child_argv(argv)
    assert got == [argv[0], "-m", "coverage", "run", "--parallel-mode", *expected_tail], (
        "coverage must be inserted AFTER the interpreter so both child forms work: a module path "
        "and a `-m` invocation. Prepending would break one of them, and the broken one would "
        "silently contribute no data."
    )


# Compared with ALL whitespace removed. Black reflows these call sites whenever an argument list
# crosses the line limit, and a regex pinned to one particular line break would then fail for a
# reason that has nothing to do with instrumentation -- a test that cries wolf about formatting
# teaches people to ignore it.
SRC_SQUASHED = re.sub(r"\s+", "", VERIFY_SRC)


@pytest.mark.parametrize(
    "runner,needle",
    [
        ("pytest", 'cmd=child_argv([sys.executable,"-m","pytest"'),
        # The module is named by PATH, not by bare filename: `verify.py` runs with cwd at the
        # CHECKOUT root while the modules may live under `src/`, so a bare `{mod}.py` would resolve
        # against the wrong directory. The needle tracks the real call so instrumentation and
        # layout cannot drift apart silently.
        ("selftests", 'child_argv([sys.executable,str(MODULES/f"{mod}.py"),"--selftest"])'),
        ("gates", "child_argv([sys.executable,*argv])"),
    ],
)
def test_all_three_runners_are_instrumented(runner, needle):
    assert needle.replace(" ", "") in SRC_SQUASHED, (
        f"the {runner} runner no longer goes through child_argv. Wrapping pytest alone reproduces "
        "the original defect exactly: the selftests and gates are where most of the execution is — "
        "78 modules have no test_*.py at all."
    )


def test_coveragerc_enables_parallel_mode():
    assert COVERAGERC.is_file(), ".coveragerc is missing; without it `parallel` defaults to off"
    cfg = configparser.ConfigParser()
    cfg.read(COVERAGERC)
    assert cfg.getboolean("run", "parallel", fallback=False), (
        "`parallel = true` is load-bearing. Without it every instrumented child overwrites the same "
        "data file and the combine silently reports whichever process finished last — a "
        "plausible-but-wrong number, which is worse than no number."
    )


def test_coveragerc_omits_the_tests_themselves():
    cfg = configparser.ConfigParser()
    cfg.read(COVERAGERC)
    omit = cfg.get("run", "omit", fallback="")
    assert "test_*.py" in omit, (
        "test files must be omitted from the measurement. Counting the tests as covered source "
        "inflates the number with the one thing guaranteed to be executed."
    )


def test_absent_coverage_data_is_reported_as_absent(monkeypatch, tmp_path):
    """No data must read as 'no measurement' — never as 0%, never as silence."""
    monkeypatch.setattr(verify, "HERE", tmp_path)
    out = verify.coverage_combine_and_report()
    assert "NO DATA" in out, (
        "an empty data set must say so. Reporting 0% would be indistinguishable from a real, awful "
        "score, and reporting nothing would be indistinguishable from success — the founding defect."
    )


def test_coverage_never_changes_the_exit_code():
    """The report is printed after the verdict and must not feed into it."""
    main_src = VERIFY_SRC.split("def main()", 1)[1]
    # Pinned by PROPERTY, not by exact text. This was the whole call line as one literal until
    # 2026-08-23, when `--reconcile-floor` legitimately added a second kwarg and reformatted the
    # call across lines — which failed this test and read as a coverage regression while nothing
    # about coverage had changed. The literal was load-bearing for one reason only, and that
    # reason is now asserted directly: COVERAGE MUST NOT BE AN INPUT TO THE VERDICT. Other kwargs
    # are none of this test's business; the two assertions below still hold the ordering and the
    # no-touch rules, which are what "must not feed into it" actually means.
    assert "code, text = verify(" in main_src, "the exit code must still come from verify()"
    call_args = main_src.split("code, text = verify(", 1)[1].split(")", 1)[0]
    assert "coverage" not in call_args, (
        f"coverage is being passed into verify() ({call_args.strip()!r}). The verdict may not "
        "depend on a measurement — that is the threshold nobody agreed to."
    )
    report_at = main_src.find("coverage_combine_and_report()")
    return_at = main_src.find("return code")
    assert report_at != -1 and return_at != -1 and report_at < return_at, (
        "coverage_combine_and_report must run after the verdict is computed and must not alter "
        "`code`. Coverage is a measurement, not a gate: gating on it here would enforce a threshold "
        "nobody agreed to, against a number that was wrong until this change."
    )
    tail = main_src[report_at:return_at]
    assert (
        "code =" not in tail and "code +=" not in tail
    ), "the coverage branch must not touch the exit code"


def test_the_cli_help_actually_renders():
    """`verify.py --help` must not crash. This test exists because my own change broke it.

    argparse interpolates every help string with `% params` when it FORMATS help, so a literal `%`
    must be written `%%`. The first version of the --coverage help said "cannot see ~80% of this
    codebase"; argparse read `% o` as an `%o` octal conversion and `verify.py --help` died with
    `TypeError: %o format: an integer is required, not dict`, reported as
    `ValueError: badly formed help string`.

    The twelve tests written alongside that change all passed, because not one of them RENDERED the
    help — they inspected source text and monkeypatched a flag. CI caught it instead, on the
    `verify.py` gate, which is the check of last resort doing its job. A construction-time test that
    never exercises the constructed thing is the same defect this repository is named for, one layer
    up: it looked at the parts and never ran the whole.

    Rendering is also the only honest check here. Grepping for `%` in help strings would miss
    `%(default)s`, which is legitimate, and would flag it as a bug.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "verify.py", "--help"],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"`verify.py --help` exited {proc.returncode}. An unescaped `%` in a help string is the "
        f"likely cause — write it `%%`. Output:\n{combined[-600:]}"
    )
    assert (
        "badly formed help string" not in combined
    ), "argparse could not format a help string. A literal `%` in help= must be escaped as `%%`."
    assert "--coverage" in combined, "--help no longer documents --coverage"
