"""A stuck tick is LOUD: it names the stuck command, its age and the `kill` that frees the tick.

On 2026-09-26 04:40Z the hourly tick forked a child that blocked inside bash's own here-document
write and never reached exec. launchd starts no tick while one runs, so for five days and twenty
hours nothing ran -- and every line that could have said so runs inside the tick. `tick_watchdog`
is an observer armed at the top of every --active tick, OUTSIDE it, and report-only by the owner's
decision: it signals nothing, and instead prints the exact drain.

The first test is the incident end to end with no timing in it: a command that can never finish on
its own (it writes more than any pipe holds into a pipe only it could read) is reported with its
drain, is still alive afterwards, and the printed drain -- run exactly as printed -- frees the tick.
Before this module existed nothing reported it at all, which is what the test fails on.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

import cadence_registry
import paths
import tick_watchdog

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"
WATCHDOG = paths.MODULE_DIR / "tick_watchdog.py"
# A child that can never complete on its own: no pipe holds 4 MB, and the only reader is itself.
DEADLOCKED = "import os; r, w = os.pipe(); os.write(w, b'x' * 4_000_000)"
# Generous ceilings for waiting on real processes; they bound a broken run, they decide nothing.
WAIT_S = 60.0


def _wait_for(predicate, what: str, timeout: float = WAIT_S, state: Path | None = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    # Say what the watcher DID record: a timeout alone told the first Linux CI failure nothing.
    seen = ""
    if state is not None:
        history = state / tick_watchdog.HISTORY_NAME
        seen = f"; record={_record(state)!r}; history=" + (
            history.read_text(encoding="utf-8") if history.exists() else "<none>"
        )
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}{seen}")


def _record(state: Path) -> dict[str, Any]:
    return tick_watchdog.load_record(state).record or {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fast_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            tick_watchdog.POLL_ENV: "0.05",
            tick_watchdog.TICK_ALERT_ENV: "3600",
            tick_watchdog.COMMAND_ALERT_ENV: "0",
        }
    )
    env.update(extra)
    return env


# ---------------------------------------------------------------- the incident, end to end -------


def test_a_stuck_tick_is_reported_with_the_kill_that_frees_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    tick = tmp_path / "tick.sh"
    tick.write_text(
        "set -euo pipefail\n"
        f'{shlex.quote(sys.executable)} {shlex.quote(str(WATCHDOG))} start --state-dir "$STATE" '
        '--tick-pid "$$"\n'
        f"{shlex.quote(sys.executable)} -c {shlex.quote(DEADLOCKED)} "
        '|| echo "STEP-FAILED rc=$?"\n'
        'echo "TICK-REACHED-ITS-END"\n',
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        ["bash", str(tick)],
        env=_fast_env(STATE=str(state)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stuck_pid = None
    try:
        # The child is caught as soon as it forks; wait until it has exec'd, so the record names
        # the command rather than the fork (same pid either way -- `kill` frees it in both cases).
        rec = _wait_for(
            lambda: "os.pipe()" in (_record(state).get("oldest_command") or {}).get("command", "")
            and _record(state).get("drain")
            and _record(state),
            "a drain naming the stuck command",
            state=state,
        )
        stuck_pid = rec["oldest_command"]["pid"]
        assert rec["drain"] == f"kill {stuck_pid}", rec
        assert [c["pid"] for c in rec["crossings"]] == [stuck_pid], rec
        # REPORT-ONLY: the stuck command is still there, and so is the tick waiting on it.
        assert _alive(stuck_pid) and proc.poll() is None
        # The drain, exactly as printed, is what frees the tick.
        subprocess.run(shlex.split(rec["drain"]), check=True, timeout=WAIT_S)
        out, _ = proc.communicate(timeout=WAIT_S)
    finally:
        for pid in (stuck_pid, proc.pid):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)
        if proc.poll() is None:
            proc.wait(timeout=WAIT_S)
    assert proc.returncode == 0, out
    assert f"to unblock: kill {stuck_pid}" in out, out
    assert "STEP-FAILED rc=143" in out and "TICK-REACHED-ITS-END" in out, out
    final = _wait_for(lambda: _record(state).get("status") == "exited" and _record(state), "exit")
    assert final["crossings"][0]["kind"] == "command", final
    history = (state / tick_watchdog.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["status"] for line in history] == ["exited"], history

    # ... and the NEXT tick's first line says what happened to this one.
    line = io.StringIO()
    rc = tick_watchdog.start(
        state_dir=state, tick_pid=os.getpid(), env={}, out=line, spawn=lambda _argv: None
    )
    assert rc == 0
    first = line.getvalue().splitlines()[0]
    assert first.startswith("  ALERT: tick-watchdog: the previous tick"), first
    assert "crossed 1 threshold(s)" in first and "nothing was terminated (report-only)" in first


# ---------------------------------------------------------------- report-only, by decision -------


def _table(rows: list[tuple[int, int, str]]) -> str:
    return "".join(f"{pid} {ppid} S Fri Oct  2 01:36:00 2026 {cmd}\n" for pid, ppid, cmd in rows)


def _watch(
    tmp_path: Path, tables: list[str], *, step_s: float, tick_s: int, command_s: int
) -> tuple[str, dict[str, Any]]:
    """`watch` over a scripted process table, its clock moving only when it sleeps."""
    now = [0.0]
    snaps = iter(tables)
    last = [tick_watchdog.parse_ps(tables[-1])]

    def snap():
        try:
            last[0] = tick_watchdog.parse_ps(next(snaps))
        except StopIteration:
            pass
        return last[0]

    def advance(_seconds: float) -> None:
        now[0] += step_s

    out = io.StringIO()
    tick_watchdog.watch(
        state_dir=tmp_path,
        tick_pid=100,
        tick_started="Fri Oct 2 01:36:00 2026",
        armer_pid=-1,
        tick_alert_s=tick_s,
        command_alert_s=command_s,
        poll_s=0.0,
        out=out,
        snapshot_fn=snap,
        sleep_fn=advance,
        clock=lambda: now[0],
        max_polls=len(tables),
    )
    return out.getvalue(), _record(tmp_path)


def test_the_watchdog_never_signals_anything(tmp_path: Path, monkeypatch) -> None:
    def refuse(*_args) -> None:
        raise AssertionError("the tick watchdog is report-only; it must never send a signal")

    monkeypatch.setattr(os, "kill", refuse)
    monkeypatch.setattr(os, "killpg", refuse)
    stuck = _table([(100, 1, "bash orchestrate.sh --active"), (200, 100, "python3 hang.py")])
    out, rec = _watch(tmp_path, [stuck] * 6, step_s=600, tick_s=1800, command_s=900)
    assert "to unblock: kill 200" in out and "past its 30m00s threshold" in out, out
    assert rec["drain"] == "kill 200" and rec["status"] == "running", rec


def test_a_stuck_tick_alerts_once_per_crossing_then_hourly(tmp_path: Path) -> None:
    stuck = _table([(100, 1, "bash orchestrate.sh --active"), (200, 100, "python3 hang.py")])
    # 30 looks, 10 min apart: command over at 1h30m, tick over at 3h, then hourly repeats.
    out, rec = _watch(tmp_path, [stuck] * 30, step_s=600, tick_s=3 * 3600, command_s=90 * 60)
    alerts = [line for line in out.splitlines() if "ALERT: tick-watchdog" in line]
    assert len([a for a in alerts if "per-command threshold" in a]) == 1, alerts
    assert len([a for a in alerts if "past its 3h00m threshold" in a]) == 1, alerts
    # Hourly after the latest ALERT: 1h30m (command) -> 2h30m, then 3h00m (tick) -> 4h00m.
    repeats = [a for a in alerts if "is still running" in a]
    assert [r.split(" at ", 1)[1][:5] for r in repeats] == ["2h30m", "4h00m"], alerts
    assert all("To unblock: kill 200" in r for r in repeats), alerts
    assert [c["kind"] for c in rec["crossings"]] == ["command", "tick"], rec


def test_a_fork_that_never_reached_exec_is_named_for_what_it_is(tmp_path: Path) -> None:
    # The incident's own shape: the stuck child still shows the tick's command line.
    fork = _table(
        [(100, 1, "bash orchestrate.sh --active"), (95005, 100, "bash orchestrate.sh --active")]
    )
    out, _rec = _watch(tmp_path, [fork] * 3, step_s=600, tick_s=3600, command_s=600)
    assert "a fork of the tick shell that never reached exec (pid 95005" in out, out
    assert "such as a here-document" in out and "kill 95005" in out, out


def test_a_shell_blocked_with_no_command_drains_by_the_tick_pid(tmp_path: Path) -> None:
    alone = _table([(100, 1, "bash orchestrate.sh --active")])
    out, rec = _watch(tmp_path, [alone] * 4, step_s=1200, tick_s=3600, command_s=600)
    assert "no command is running, so the tick shell itself is blocked" in out, out
    assert rec["drain"] == "kill 100", rec


def test_a_start_time_that_jitters_by_a_second_is_the_same_process(tmp_path: Path) -> None:
    """On Linux `lstart` is computed, not stored, so two readings of one process need not match to
    the second. Compared as exact strings, a one-second difference would read as the tick having
    exited, and the watcher would finalize and leave with a stuck command unreported."""
    a = (
        "100 1 S Fri Oct  2 01:36:00 2026 bash orchestrate.sh --active\n"
        "200 100 S Fri Oct  2 01:36:05 2026 python3 hang.py\n"
    )
    b = a.replace("01:36:00", "01:36:01").replace("01:36:05", "01:36:04")
    out, rec = _watch(tmp_path, [a, b] * 3, step_s=600, tick_s=3600 * 9, command_s=1500)
    assert rec["status"] == "running", rec  # never read as exited
    # ... and the command's age kept accumulating across the flips, so it crossed its threshold.
    assert "to unblock: kill 200" in out and rec["drain"] == "kill 200", (out, rec)


def test_the_process_table_keeps_whole_command_lines() -> None:
    """Linux procps cuts every line to 80 columns when its output is a pipe, which left the first
    Linux CI run's record naming the stuck command as `/opt/hostedtoolcache/Python/3.12.1`: the
    ALERT could not say what was stuck. A command line far wider than any terminal must survive."""
    tail = "y" * 300
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)  # " + tail], stdin=subprocess.DEVNULL
    )
    try:
        row = _wait_for(lambda: tick_watchdog.snapshot().get(child.pid), "the child in ps")
        assert row.command.endswith(tail), f"command truncated to {len(row.command)} chars"
    finally:
        child.kill()
        child.wait(timeout=WAIT_S)


def test_a_recycled_tick_pid_reads_as_the_tick_having_exited(tmp_path: Path) -> None:
    other = "100 1 S Sat Oct  3 09:00:00 2026 something else\n"
    out, rec = _watch(tmp_path, [other], step_s=1, tick_s=3600, command_s=600)
    assert rec["status"] == "exited" and not out, (rec, out)


# ---------------------------------------------------------------- the first line of every tick --


def _stamp(state: Path, name: str, age_s: float) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / name).touch()
    then = time.time() - age_s
    os.utime(state / name, (then, then))


def _start(state: Path, env: dict[str, str] | None = None) -> list[str]:
    out = io.StringIO()
    rc = tick_watchdog.start(
        state_dir=state, tick_pid=os.getpid(), env=env or {}, out=out, spawn=lambda _argv: None
    )
    assert rc == 0, out.getvalue()
    return out.getvalue().splitlines()


def test_the_drained_line_is_reachable_and_exact(tmp_path: Path) -> None:
    """Latched-gate question 4: what the watchdog prints when everything is healthy. Most ticks
    print this line, so it is pinned whole -- a good-news branch no input reaches is a latch."""
    state = tmp_path / "state"
    armed = time.time() - 3600
    tick_watchdog.write_record(
        state,
        {
            "status": "exited",
            "armed_at": tick_watchdog._iso(armed),
            "awake_duration_s": 842,
            "crossings": [],
        },
    )
    _stamp(state, ".last-durability-sweep", 25 * 60)
    lines = _start(state)
    assert len(lines) == 1, lines
    assert re.fullmatch(
        r"  tick-watchdog: armed — an ALERT fires if this tick runs past 3h00m, or any one command "
        r"past 1h30m, of awake time \(report-only\); previous tick \(armed \S+Z, 1h00m ago\) ran "
        r"14m02s, inside both thresholds; newest cadence outcome 25m0\ds ago \(durability-sweep "
        r"success\)",
        lines[0],
    ), lines[0]


def test_a_tick_that_reaches_no_cadence_step_alerts(tmp_path: Path) -> None:
    """Every tick aborting before its first cadence step is never long-running, so the thresholds
    cannot see it -- the newest outcome of ANY step going stale can."""
    state = tmp_path / "state"
    _stamp(state, ".last-durability-sweep", 3 * 86400)
    _stamp(state, ".fail-pattern-miner", 2 * 86400)  # a failure is an outcome too
    lines = _start(state)
    assert lines[0].endswith("newest cadence outcome 2d00h ago (pattern-miner failure)"), lines
    assert len(lines) == 2 and lines[1].startswith(
        "  ALERT: tick-watchdog: no cadence step has recorded an outcome for 2d00h"
    ), lines


def test_newest_outcome_is_the_newest_of_any_step_success_or_failure() -> None:
    report = {
        "steps": [
            {"key": "a", "last_success_ts": 10, "last_failure_ts": None},
            {"key": "b", "last_success_ts": None, "last_failure_ts": 30},
            {"key": "c", "last_success_ts": 20, "last_failure_ts": 5},
        ]
    }
    assert cadence_registry.newest_outcome(report) == {"key": "b", "kind": "failure", "ts": 30}
    assert cadence_registry.newest_outcome({"steps": []}) is None
    # ONE stale rule: the watchdog's bound is the registry's own rule for its shortest step.
    assert cadence_registry.shortest_stale_after_s() == cadence_registry.stale_after_seconds(
        {"cadence_days": 0}
    )


def test_a_previous_tick_that_left_no_end_is_unknown_and_kept(tmp_path: Path) -> None:
    state = tmp_path / "state"
    tick_watchdog.write_record(
        state,
        {
            "status": "running",
            "tick_pid": 2**22 + 12345,  # above any pid either platform hands out
            "tick_started": "Fri Oct 2 01:36:00 2026",
            "watchdog_pid": None,
            "armed_at": "2026-10-02T01:36:01Z",
        },
    )
    lines = _start(state)
    assert lines[0].startswith("  WARN: tick-watchdog: the previous tick") and "UNKNOWN" in lines[0]
    history = (state / tick_watchdog.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(h)["status"] for h in history] == ["unfinalized"], history


def test_an_unreadable_record_is_unknown_not_clean(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / tick_watchdog.RECORD_NAME).write_text("{not json", encoding="utf-8")
    assert "fate is UNKNOWN, not clean" in _start(state)[0]


def test_threshold_typos_never_switch_the_watchdog_off() -> None:
    t = tick_watchdog.thresholds(
        {tick_watchdog.TICK_ALERT_ENV: "soon", tick_watchdog.COMMAND_ALERT_ENV: "-1"}
    )
    assert (t.tick_s, t.command_s) == (tick_watchdog.TICK_ALERT_S, tick_watchdog.COMMAND_ALERT_S)
    assert len(t.notes) == 2 and "ignored" in tick_watchdog.thresholds_phrase(t)


# ---------------------------------------------------------------- wiring in orchestrate.sh ------


def test_orchestrate_arms_the_watchdog_before_any_step() -> None:
    text = ORCHESTRATE.read_text(encoding="utf-8")
    needle = "tick_watchdog.py" + '" start'
    assert text.count(needle) == 1, f"expected one arming site, found {text.count(needle)}"
    switch = "_step_disabled " + "tick-watchdog"
    assert text.count(switch) == 1, "the kill switch must be consulted exactly once"
    lines = text.splitlines()
    arm = next(i for i, line in enumerate(lines) if needle in line)
    definition = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{")  # runs only when called
    steps = [
        i
        for i, line in enumerate(lines)
        if 'python3 "$ORCH/' in line.split("#", 1)[0]
        and needle not in line
        and not definition.match(line)
    ]
    assert steps and min(steps) > arm, (
        f"the watchdog arms at line {arm + 1} but a step runs at line {min(steps) + 1}: a step "
        "above the arming point runs unwatched"
    )


@contextmanager
def _arming_block() -> Iterator[str]:
    """The arming block exactly as orchestrate.sh has it, with the helpers it calls."""
    lines = ORCHESTRATE.read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("# ORCH-ANCHOR: tick-watchdog")
    )
    end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
    defs = []
    for name in ("_step_disabled", "_warn_unknown_disable_steps"):
        head = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
        tail = next(i for i in range(head, len(lines)) if lines[i] == "}")
        defs.append("\n".join(lines[head : tail + 1]))
    yield "\n".join([cadence_registry.shell_functions(), *defs, *lines[start : end + 1]])


def _run_block(state: Path, **env: str) -> subprocess.CompletedProcess:
    with _arming_block() as block:
        script = "\n".join(
            [
                "set -euo pipefail",
                f"ORCH={shlex.quote(str(paths.MODULE_DIR))}",
                f"STAMP_DIR={shlex.quote(str(state))}",
                "mode=active",
                block,
                "_warn_unknown_disable_steps",
            ]
        )
    return subprocess.run(
        ["bash", "-c", script],
        env=_fast_env(**env),
        capture_output=True,
        text=True,
        timeout=WAIT_S,
        check=False,
    )


def test_the_kill_switch_disarms_it_and_says_so(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    proc = _run_block(state, ORCH_DISABLE_STEPS="tick-watchdog")
    assert proc.returncode == 0, proc.stderr
    assert "[disabled] tick-watchdog skipped by ORCH_DISABLE_STEPS" in proc.stdout, proc.stdout
    assert "nothing watches this tick" in proc.stdout, proc.stdout
    # A registered key: the typo check stays quiet, so the switch does not read as a lie.
    assert "unknown step" not in proc.stderr, proc.stderr
    assert not (state / tick_watchdog.RECORD_NAME).exists()


def test_the_arming_block_as_written_arms_and_records_the_tick(tmp_path: Path) -> None:
    state = tmp_path / "state"
    proc = _run_block(state, ORCH_DISABLE_STEPS="")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("  tick-watchdog: armed"), proc.stdout
    final = _wait_for(
        lambda: _record(state).get("status") == "exited" and _record(state), "the watcher's end"
    )
    assert final["watchdog_pid"] and not final["crossings"], final


def _stuck_record(target_pid: int, target_started: str) -> dict[str, Any]:
    return {
        "status": "running",
        "tick_pid": 2**22 + 12345,
        "tick_started": "Fri Oct 2 01:36:00 2026",
        "armed_at": "2026-10-02T01:36:01Z",
        "awake_age_s": 7 * 3600,
        "tick_alert_s": 3 * 3600,
        "drain": f"kill {target_pid}",
        "drain_targets": [{"pid": target_pid, "started": target_started}],
        "crossings": [{"kind": "tick", "pid": 1, "age_s": 3 * 3600}],
    }


def _me() -> tick_watchdog.Proc:
    return _wait_for(lambda: tick_watchdog.snapshot().get(os.getpid()), "this process in ps")


def test_fleet_summary_carries_the_tick_record(tmp_path: Path, monkeypatch) -> None:
    import mcp_server

    me = _me()  # a target that IS still the process the record named
    tick_watchdog.write_record(tmp_path, _stuck_record(me.pid, me.started))

    @contextmanager
    def empty_brain():
        conn = sqlite3.connect(":memory:")
        for table in (
            "runs",
            "outcomes",
            "costs",
            "evaluations",
            "human_calibration",
            "owner_questions",
            "resume_tokens",
        ):
            conn.execute(f"CREATE TABLE {table} (id INTEGER)")
        yield conn

    monkeypatch.setattr(mcp_server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mcp_server.feedback, "_conn", empty_brain)
    tick = mcp_server._fleet_summary()["tick"]
    assert tick["drain"] == f"kill {me.pid}" and tick["status"] == "running", tick
    assert f"STUCK — to unblock: kill {me.pid}" in tick["line"], tick


def test_a_drain_whose_target_has_exited_is_never_advertised(tmp_path: Path) -> None:
    """A record outlives its watcher. If the target exited and its pid was handed to an unrelated
    process, printing the recorded `kill` would hand a reader the command that kills the wrong
    process -- so the drain is re-checked against a fresh process table every time it is shown."""
    tick_watchdog.write_record(tmp_path, _stuck_record(2**22 + 999, "Fri Oct 2 01:40:00 2026"))
    report = tick_watchdog.summary(tmp_path)
    assert report["drain"] is None, report
    assert "kill" not in report["line"] and "no longer applies" in report["line"], report
    me = _me()  # same pid as a live process, but NOT the start time the record named: recycled
    tick_watchdog.write_record(tmp_path, _stuck_record(me.pid, "Fri Oct 2 01:40:00 2025"))
    assert tick_watchdog.summary(tmp_path)["drain"] is None


def test_ticks_that_never_reach_a_cadence_step_alert_from_first_arming(tmp_path: Path) -> None:
    """A machine that has NEVER recorded a cadence outcome has no newest outcome to age, so the
    alert needs a baseline: the first tick the watchdog armed here."""
    state = tmp_path / "state"
    two_days_ago = tick_watchdog._iso(time.time() - 2 * 86400)
    tick_watchdog.write_record(
        state, {"status": "exited", "armed_at": two_days_ago, "first_armed_at": two_days_ago}
    )
    lines = _start(state)
    assert len(lines) == 2 and lines[1].startswith(
        "  ALERT: tick-watchdog: no cadence step has recorded an outcome in the 2d00h"
    ), lines
    assert tick_watchdog.load_record(state).record["first_armed_at"] == two_days_ago  # carried
    fresh = tmp_path / "fresh"
    assert len(_start(fresh)) == 1  # the first arming starts the clock and alerts nothing


@pytest.mark.parametrize("seconds,text", [(842, "14m02s"), (5400, "1h30m"), (504000, "5d20h")])
def test_durations_read_at_the_scale_they_happened(seconds: int, text: str) -> None:
    assert tick_watchdog.fmt_duration(seconds) == text
