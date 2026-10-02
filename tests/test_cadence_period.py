"""A stamped cadence step fires at the period it MEANS, measured through orchestrate.sh itself.

`cadence_days` is not the period. `_due()` hands it to `find STAMP -mtime +N`, and `find` counts whole
days since the stamp and discards the remainder, so `+N` matches only once the stamp is at least N+1
days old: 0 is daily, 1 is every other day. The registry explained that in a comment on one row, and
four rows written after it declared 1 for steps documented as daily -- pattern-miner, fleet-shapes,
agent-switches and evidence-acquisition ran 48-50h apart while their own `[cadence] ... (daily ...)`
log lines said otherwise -- and coverage-testgen-trigger declared 7 for weekly and ran every eight
days. A comment is what failed, so these measure instead of describing:

1. each name in `cadence_registry.CADENCE_DAYS_FOR` fires at the period the word means;
2. every stamped row declares one of those names, checked by asking the tick's own `_cadence_due`
   whether the step is due just before and just after that period;
3. a step whose `[cadence]` log line names a period declares that period;
4. `inspect_cadence`, which restates the rule as `cadence_days + 1` days, calls a step stale only
   after the shell would have run it, and before it has missed a whole second run.

The shell is REPLAYED, not re-implemented. `_due` and `_cadence_due` are read out of orchestrate.sh,
each definition asserted unique because bash keeps the last one it reads, and run by bash over the
registry's own generated `cadence_stamp`/`cadence_days` with whatever `find` bash resolves -- the
resolution the tick makes. A Python model of `-mtime` would stay green while the shell drifted.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import cadence_registry
import paths

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"
# What each name MEANS, in hours. It lives here, not in the registry, because it is the meaning of the
# word and no convention changes it: the registry says which N spells "daily", this says how long a
# day is. A name the registry adds without an entry here fails the first test by name.
PERIOD_HOURS = {"daily": 24, "weekly": 7 * 24}
# How far either side of a period to probe. Minutes, not seconds: `find` measures from its own start,
# a moment after the stamps are aged, and the tick only looks once an hour anyway.
MARGIN_S = 5 * 60
STAMPED = tuple(row for row in cadence_registry.CADENCE_STEPS if row.get("success_stamp"))


@functools.lru_cache(maxsize=None)
def _definition(name: str) -> str:
    """One function exactly as orchestrate.sh defines it, grown line by line until bash parses it.

    Grown rather than read as one line, so reformatting the function is not a failure. The
    definition must be UNIQUE: bash keeps the last one it reads, so with two, every call site after
    the second would run code this test never saw.
    """
    lines = ORCHESTRATE.read_text(encoding="utf-8").splitlines()
    head = re.compile(rf"\s*(?:function\s+{re.escape(name)}\b|{re.escape(name)}\s*\(\))")
    starts = [i for i, line in enumerate(lines) if head.match(line)]
    assert (
        len(starts) == 1
    ), f"expected exactly one definition of {name}() in orchestrate.sh, found {len(starts)}"
    for end in range(starts[0] + 1, min(len(lines), starts[0] + 40) + 1):
        text = "\n".join(lines[starts[0] : end])
        if subprocess.run(["bash", "-n", "-c", text], capture_output=True).returncode == 0:
            return text
    raise AssertionError(f"{name}() in orchestrate.sh never parsed as a complete function")


def _replay(state: Path, conditions: dict[str, str]) -> set[str]:
    """Evaluate shell conditions with the tick's own `_due`/`_cadence_due`; return those that held."""
    script = "\n".join(
        [
            cadence_registry.shell_functions(),
            # The per-step kill switch is not what is under test. Stubbed, so an operator's
            # ORCH_DISABLE_STEPS cannot make a step read as never due.
            "_step_disabled() { return 1; }",
            # Nor is a declared retirement: a retired step runs at its declared period once it is
            # re-enabled, which is the only time it runs. tests/test_cadence_retirement.py replays
            # the real `_step_retired`.
            "_step_retired() { return 1; }",
            _definition("_due"),
            _definition("_cadence_due"),
            *(
                f"if {test}; then echo {shlex.quote(label)}; fi"
                for label, test in conditions.items()
            ),
        ]
    )
    env = {"PATH": os.environ.get("PATH") or os.defpath, "STAMP_DIR": str(state)}
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    # `if` swallows a failing condition's status, so a missing function would read as "not due"
    # everywhere. stderr is where that shows ("command not found"), and it must be empty.
    assert (
        proc.returncode == 0 and not proc.stderr.strip()
    ), f"the shell replay did not run cleanly (rc={proc.returncode}): {proc.stderr.strip()[:500]}"
    return set(proc.stdout.split())


def _aged(state: Path, stamps: set[str], age_s: int) -> Path:
    state.mkdir(parents=True)
    then = time.time() - age_s
    for stamp in stamps:
        (state / stamp).touch()
        os.utime(state / stamp, (then, then))
    return state


def _stamps_due(tmp_path: Path, age_s: int, rows: tuple[dict, ...] = STAMPED) -> set[str]:
    """The step keys the tick would run with every stamp `age_s` old."""
    state = _aged(tmp_path / f"age-{age_s}", {row["success_stamp"] for row in rows}, age_s)
    return _replay(state, {row["key"]: f"_cadence_due {row['key']}" for row in rows})


def test_each_named_cadence_fires_at_the_period_its_name_means(tmp_path: Path) -> None:
    named = cadence_registry.CADENCE_DAYS_FOR
    unmeant = sorted(set(named) - set(PERIOD_HOURS))
    assert not unmeant, f"CADENCE_DAYS_FOR names {unmeant} with no period in PERIOD_HOURS; add one"
    wrong = []
    for name, days in named.items():
        period_s = PERIOD_HOURS[name] * 3600
        for age_s, expected in ((period_s - MARGIN_S, False), (period_s + MARGIN_S, True)):
            state = _aged(tmp_path / f"{name}-{age_s}", {"stamp"}, age_s)
            due = "probe" in _replay(state, {"probe": f'_due "$STAMP_DIR/stamp" {days}'})
            if due != expected:
                wrong.append(
                    f"{name!r} is spelled {days}, and `_due` says due={due} for a stamp "
                    f"{age_s / 3600:.2f}h old; a {PERIOD_HOURS[name]}h period needs due={expected}"
                )
    assert not wrong, "the named cadences do not fire at their periods:\n" + "\n".join(wrong)


def test_every_stamped_step_declares_a_named_cadence_and_fires_at_it(tmp_path: Path) -> None:
    assert STAMPED, "cadence_registry has no stamped steps -- nothing was measured"
    spelled = cadence_registry.CADENCE_DAYS_FOR
    name_of = {days: name for name, days in spelled.items()}
    named = tuple(row for row in STAMPED if row.get("cadence_days") in name_of)
    unnamed = tuple(row for row in STAMPED if row.get("cadence_days") not in name_of)
    wrong = []
    if unnamed:
        # Measure what each unnamed value actually does, so the failure states the real period.
        fires_at: dict[str, int] = {}
        for days in range(15):
            for key in _stamps_due(tmp_path / "sweep", days * 86400 + MARGIN_S, unnamed):
                fires_at.setdefault(key, days * 24)
        spellings = ", ".join(f"{name} is {days}" for name, days in spelled.items())
        for row in unnamed:
            fires = fires_at.get(row["key"])
            when = f"{fires}h" if fires is not None else "over 14 days"
            wrong.append(
                f"{row['key']}: cadence_days={row.get('cadence_days')!r} is not a named cadence, "
                f"and `_cadence_due` runs it once its stamp is {when} old. Named: {spellings}; "
                "name any other period in cadence_registry.CADENCE_DAYS_FOR first"
            )
    periods = {PERIOD_HOURS[name_of[row["cadence_days"]]] * 3600 for row in named}
    due_at = {
        age_s: _stamps_due(tmp_path, age_s)
        for period_s in periods
        for age_s in (period_s - MARGIN_S, period_s + MARGIN_S)
    }
    for row in named:
        name = name_of[row["cadence_days"]]
        period_s = PERIOD_HOURS[name] * 3600
        for age_s, expected in ((period_s - MARGIN_S, False), (period_s + MARGIN_S, True)):
            due = row["key"] in due_at[age_s]
            if due != expected:
                wrong.append(
                    f"{row['key']} is declared {name} ({PERIOD_HOURS[name]}h), and `_cadence_due` "
                    f"says due={due} for a stamp {age_s / 3600:.2f}h old"
                )
    assert not wrong, "cadence steps that do not run at the period they declare:\n" + "\n".join(
        wrong
    )


def test_a_cadence_log_line_names_the_period_its_step_declares() -> None:
    words = "|".join(map(re.escape, cadence_registry.CADENCE_DAYS_FOR))
    site = re.compile(r"(\s*)(?:if|elif)\b[^#\n]*?_cadence_due ([a-z0-9-]+)")
    lines = ORCHESTRATE.read_text(encoding="utf-8").splitlines()
    claims: dict[str, list[str]] = {}
    for i, line in enumerate(lines):
        match = site.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        for body in lines[i + 1 :]:
            # The step's block ends at its own `fi`, or at the next branch of its chain: a log
            # line past that point belongs to some other step.
            stripped = body.lstrip()
            if len(body) - len(stripped) <= indent and re.match(r"(?:fi|elif|else)\b", stripped):
                break
            if stripped.startswith("echo") and "[cadence]" in stripped:
                named = re.findall(rf"\b({words})\b", stripped)
                if named:
                    claims.setdefault(match.group(2), []).append(named[0])
                break
    assert claims, "no `[cadence]` log line names a period -- the scan found nothing to check"
    wrong = []
    for key, said in sorted(claims.items()):
        row = cadence_registry.STEP_BY_KEY.get(key)
        for name in said:
            if row is None:
                wrong.append(f"{key}: its log line says {name}, and the registry has no such step")
            elif row.get("cadence_days") != cadence_registry.CADENCE_DAYS_FOR[name]:
                wrong.append(
                    f"{key}: its `[cadence]` log line says {name}, spelled "
                    f"{cadence_registry.CADENCE_DAYS_FOR[name]}, and the registry declares "
                    f"cadence_days={row.get('cadence_days')!r}"
                )
    assert not wrong, "log lines that name a period the step does not run at:\n" + "\n".join(wrong)


def test_inspect_cadence_staleness_sits_between_due_and_a_missed_run(tmp_path: Path) -> None:
    name_of = {days: name for name, days in cadence_registry.CADENCE_DAYS_FOR.items()}
    rows = tuple(row for row in STAMPED if row.get("cadence_days") in name_of)
    assert rows, "no stamped step declares a named cadence -- nothing was measured"
    wrong = []
    for row in rows:
        period_s = PERIOD_HOURS[name_of[row["cadence_days"]]] * 3600
        # Just due is not late; a whole run missed is. Anywhere between is the report's own grace.
        for missed, expected in ((0, "fresh"), (1, "stale")):
            age_s = (1 + missed) * period_s + MARGIN_S
            state = _aged(tmp_path / f"{row['key']}-{missed}", {row["success_stamp"]}, age_s)
            # Measured as the step runs when re-enabled: a retired step is never late, it is
            # `retired`, and tests/test_cadence_retirement.py holds that verdict.
            live = {**row, cadence_registry.RETIRED_FIELD: None}
            report = cadence_registry.inspect_cadence(state, now=int(time.time()), registry=(live,))
            status = report["steps"][0]["success_status"]
            if status != expected:
                wrong.append(f"{row['key']}: {status} at {age_s / 3600:.1f}h, expected {expected}")
    assert not wrong, "inspect_cadence disagrees with the shell about when a step is late:\n" + (
        "\n".join(wrong)
    )
