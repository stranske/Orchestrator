#!/usr/bin/env python3
"""tick_watchdog.py — a stuck tick must be LOUD, with its age and the command that would free it.

THE INCIDENT. launchd starts `orchestrate.sh --active` at :40 every hour and starts NO second
instance while one is running, so the running tick is a gate whose only drain is that tick ending.
The tick that began 2026-09-26 04:40Z forked the child that was to exec the MINING_HEALTH summary,
and the child never reached `exec`: it sat in bash's own here-document write (`heredoc_write` ->
`write()`), holding both ends of a pipe with 512 bytes in it and waiting for a reader that was
itself, after the exec the write was blocking. The tick shell waited on the child. Every cadence,
every ALERT line and every sweep that could have said so runs INSIDE the tick, so for five days and
twenty hours the symptom was silence.

Why 512 bytes: XNU starts every pipe at 512 bytes and grows it only while system-wide pipe memory
is under `maxpipekva` (16 MiB). bash 5.3 decided at build time that a here-document of up to 64 KiB
fits a pipe, and writes it in one call before exec. With the cap reached, a document over 512 bytes
cannot be written and nothing will ever read it. That cause is fixed where it lives -- the tick
feeds no here-document at all -- and this module is for the next cause nobody has seen yet.

WHAT IT DOES. `start` runs synchronously at the top of every --active tick: it prints the previous
tick's fate, records this tick, and spawns `watch` -- a separate process, in its own session, that
needs nothing from the tick to make progress. `watch` polls the process table and:

  * when a command the tick is waiting on has run `COMMAND_ALERT_S`, or the tick itself has run
    `TICK_ALERT_S`, prints an ALERT into the tick's log naming the command, its age and the exact
    `kill` that would free the tick, and writes the same into the record;
  * repeats that ALERT every `REALERT_S` while it stays true, so a stuck tick keeps the log's
    hourly rhythm instead of going quiet -- the incident's whole symptom was a log that stopped;
  * when the tick exits, records how long it ran and what it crossed.

The first line also says when ANY cadence step last recorded an outcome, and ALERTs once that is
older than the shortest step's stale bound. A tick that aborts before its first cadence step every
hour is never long-running, so the thresholds above can never see it -- and without this the
watchdog's own line would read "inside both thresholds" every hour while nothing ran.

REPORT-ONLY, by the owner's decision (2026-10-02): it never signals anything. That ends the
silence and not the latch -- a stuck tick still holds every later tick until someone runs the
printed `kill` -- which is why the drain is printed, not described. `mcp_server.fleet_summary`
carries the live record, so any session asking why the fleet is quiet is handed the same command.

Ages are AWAKE time (`time.monotonic`, which stops while the machine sleeps), so a laptop closed
mid-tick does not read as a hang on wake. A process is identified by pid AND start time, so a
recycled pid is never mistaken for the tick.

It is tick infrastructure, like the log rotation and the per-step kill switch, not a capability:
no surface is offered it and nothing selects it. Kill switch: `ORCH_DISABLE_STEPS=tick-watchdog`
(the tick then says on every run that nothing watches it). Thresholds: `ORCH_TICK_ALERT_S`,
`ORCH_TICK_COMMAND_ALERT_S`.

    python3 tick_watchdog.py start --state-dir DIR --tick-pid PID   # orchestrate.sh, top of the tick
    python3 tick_watchdog.py status [--state-dir DIR] [--json]       # is the current tick stuck?
    python3 tick_watchdog.py --selftest
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

RECORD_NAME = "tick-watchdog.json"
HISTORY_NAME = "tick-watchdog-history.jsonl"
# One line per tick. 2000 is ~83 days of hourly ticks; trimmed in place once it is 500 over.
HISTORY_KEEP = 2000
SCHEMA = 1

# THE THRESHOLDS, each defined once. `start` resolves them a single time and hands the same integers
# to the line it prints and to the `watch` process that applies them, so the threshold a tick is
# told and the one it is held to cannot differ. Measured 2026-10-02 on the tick that ran every daily
# and weekly cadence at once after the outage (77 min in all): its longest commands were
# durability_sweep at 20 min, periodic_report at 20 min and redirect_apply at 13 min. So 90 min is
# 4.5x the slowest command observed and 3 h is over twice that whole worst-case tick.
COMMAND_ALERT_S = 90 * 60
TICK_ALERT_S = 3 * 3600
COMMAND_ALERT_ENV = "ORCH_TICK_COMMAND_ALERT_S"
TICK_ALERT_ENV = "ORCH_TICK_ALERT_S"
# While anything is over its threshold the ALERT repeats at the tick's own period, so the log keeps
# one line an hour whether or not a tick can run.
REALERT_S = 3600
# How often `watch` looks. Overridable for the tests only; a threshold is applied to within a poll.
POLL_S = 15.0
POLL_ENV = "ORCH_TICK_WATCHDOG_POLL_S"
REFRESH_S = 60.0  # how often the live record is rewritten while nothing is wrong

PS_ARGV = ("ps", "-A", "-o", "pid=,ppid=,stat=,lstart=,command=")
PS_TIMEOUT_S = 30
# How far two readings of one process's start time may differ and still be the same process. Linux
# procps derives `lstart` from time(NULL) minus /proc/uptime, so consecutive `ps` calls can print one
# process's start a second apart; compared as exact strings, the tick read as gone on an early poll
# and the watcher left without a word (the first Linux CI run of this module, both Pythons). A pid
# recycled within this window of the original's start is not a case worth a false "exited" for.
START_TOLERANCE_S = 5


# ---------------------------------------------------------------- process table -----------------


@dataclass(frozen=True)
class Proc:
    """One row of the process table. `started` (ps `lstart`) is the half of the identity a recycled
    pid does not share, so the tick is recognised by (pid, started), never by a bare pid."""

    pid: int
    ppid: int
    started: str
    command: str
    state: str = ""

    @property
    def zombie(self) -> bool:
        """Exited and not yet reaped. Still in the table, with its pid and start time, so without
        this a tick whose parent has not called wait() would read as running forever."""
        return self.state.startswith("Z")


def parse_ps(text: str) -> dict[int, Proc]:
    """`ps -A -o pid=,ppid=,stat=,lstart=,command=` as {pid: Proc}. `lstart` is five words
    ("Fri Oct  2 01:36:00 2026"), normalised to single spaces so two snapshots compare equal."""
    procs: dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[8] if len(parts) > 8 else ""
        procs[pid] = Proc(pid, ppid, " ".join(parts[3:8]), command, parts[2])
    return procs


def snapshot() -> dict[int, Proc]:
    out = subprocess.run(
        PS_ARGV,
        capture_output=True,
        text=True,
        timeout=PS_TIMEOUT_S,
        check=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "LC_ALL": "C"},  # English day/month names, so `lstart` always parses
    ).stdout
    return parse_ps(out)


def start_epoch(started: str) -> float | None:
    try:
        return time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def same_start(a: Any, b: Any) -> bool:
    """Whether two `lstart` readings belong to one process (see START_TOLERANCE_S)."""
    if a == b:
        return True
    first, second = start_epoch(str(a)), start_epoch(str(b))
    return first is not None and second is not None and abs(first - second) <= START_TOLERANCE_S


def describe(proc: Proc, tick: Proc | None) -> str:
    """How an ALERT names a command. A child still showing the tick's own command line is a fork of
    the tick shell that has not reached `exec` -- the incident's exact shape -- and printing its argv
    would only print the tick again, so it is named for what it is."""
    if tick is not None and proc.command == tick.command:
        return (
            f"a fork of the tick shell that never reached exec (pid {proc.pid}: a subshell, a "
            "command substitution, or a redirection still being set up, such as a here-document)"
        )
    return f"`{proc.command[:120]}` (pid {proc.pid})"


# ---------------------------------------------------------------- the decision (pure) -----------


@dataclass
class Watch:
    """What `watch` knows about the tick it observes. Ages are on the monotonic clock."""

    tick_pid: int
    tick_started: str
    armed_at: float
    tick_alert_s: int
    command_alert_s: int
    exclude: frozenset[int] = frozenset()
    # Keyed by pid, each carrying the start time it was first seen with, so a start that reads a
    # second differently on the next poll is still the same command and its age keeps counting.
    first_seen: dict[int, tuple[str, float]] = field(default_factory=dict)
    reported: dict[int, str] = field(default_factory=dict)
    tick_reported: bool = False


@dataclass(frozen=True)
class Verdict:
    """What one look at the process table found: the BLOCKING quantities (ages) next to the
    thresholds they were compared with, and which crossings are new since the last look."""

    tick_gone: bool
    tick_age_s: float
    tick: Proc | None = None
    commands: tuple[tuple[Proc, float], ...] = ()  # the tick's commands, oldest first
    over: tuple[tuple[Proc, float], ...] = ()  # commands at or past COMMAND_ALERT_S
    new_over: tuple[tuple[Proc, float], ...] = ()  # ... first seen there on this look
    tick_over: bool = False
    tick_new_over: bool = False

    @property
    def oldest(self) -> tuple[Proc, float] | None:
        return self.commands[0] if self.commands else None

    @property
    def drain(self) -> str | None:
        """The exact command that frees the tick, or None when nothing is stuck. Killing the stuck
        command is enough: its step fails through its own error path and the tick goes on. Only a
        tick blocked with no command of its own -- the shell itself waiting -- needs the tick pid.
        """
        if self.over:
            return "kill " + " ".join(str(p.pid) for p, _ in self.over)
        if self.tick_over and self.tick is not None:
            return f"kill {self.tick.pid}"
        return None


def assess(watch: Watch, procs: Mapping[int, Proc], now: float) -> Verdict:
    """Compare the tick against its thresholds. Pure apart from the bookkeeping on `watch`.

    A COMMAND is a direct child of the tick shell. The shell runs its steps one at a time, so the
    child it is waiting on IS the current step, whatever the step is and however it got stuck. A
    command's age runs from when `watch` first saw it, so it is measured to within one poll."""
    tick = procs.get(watch.tick_pid)
    age = now - watch.armed_at
    if tick is None or tick.zombie or not same_start(tick.started, watch.tick_started):
        return Verdict(tick_gone=True, tick_age_s=age)
    commands = [
        p
        for p in procs.values()
        if p.ppid == watch.tick_pid
        and p.pid not in watch.exclude
        and p.pid != p.ppid
        and not p.zombie
    ]
    live = {p.pid: p for p in commands}
    for pid in list(watch.first_seen):
        current = live.get(pid)
        if current is None or not same_start(current.started, watch.first_seen[pid][0]):
            del watch.first_seen[pid]
            watch.reported.pop(pid, None)
    for proc in commands:
        watch.first_seen.setdefault(proc.pid, (proc.started, now))
    aged = tuple(
        sorted(((p, now - watch.first_seen[p.pid][1]) for p in commands), key=lambda pa: -pa[1])
    )
    over = tuple((p, a) for p, a in aged if a >= watch.command_alert_s)
    new_over = tuple((p, a) for p, a in over if p.pid not in watch.reported)
    watch.reported.update((p.pid, p.started) for p, _ in new_over)
    tick_over = age >= watch.tick_alert_s
    tick_new_over = tick_over and not watch.tick_reported
    watch.tick_reported = watch.tick_reported or tick_over
    return Verdict(
        tick_gone=False,
        tick_age_s=age,
        tick=tick,
        commands=aged,
        over=over,
        new_over=new_over,
        tick_over=tick_over,
        tick_new_over=tick_new_over,
    )


# ---------------------------------------------------------------- thresholds ---------------------


@dataclass(frozen=True)
class Thresholds:
    tick_s: int
    command_s: int
    poll_s: float
    notes: tuple[str, ...] = ()


def thresholds(env: Mapping[str, str]) -> Thresholds:
    """This tick's thresholds. An unusable override is IGNORED with a note, never read as 0 or as
    "no threshold": a typo must not be the thing that silences the watchdog."""
    notes: list[str] = []

    def whole(name: str, default: int, minimum: int) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = minimum - 1
        if value < minimum:
            notes.append(f"ignored {name}={raw!r}: not a whole number of seconds >= {minimum}")
            return default
        return value

    raw_poll = (env.get(POLL_ENV) or "").strip()
    poll = POLL_S
    if raw_poll:
        try:
            poll = max(0.05, float(raw_poll))
        except ValueError:
            notes.append(f"ignored {POLL_ENV}={raw_poll!r}: not a number of seconds")
    return Thresholds(
        tick_s=whole(TICK_ALERT_ENV, TICK_ALERT_S, 1),
        command_s=whole(COMMAND_ALERT_ENV, COMMAND_ALERT_S, 0),
        poll_s=poll,
        notes=tuple(notes),
    )


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "an unknown time"
    total = max(0, int(round(float(seconds))))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def thresholds_phrase(t: Thresholds) -> str:
    text = (
        f"an ALERT fires if this tick runs past {fmt_duration(t.tick_s)}, or any one command past "
        f"{fmt_duration(t.command_s)}, of awake time (report-only)"
    )
    if t.notes:
        text += " (" + "; ".join(t.notes) + ")"
    return text


# ---------------------------------------------------------------- the record ---------------------


def _iso(epoch: float | None = None) -> str:
    when = datetime.fromtimestamp(time.time() if epoch is None else epoch, tz=timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Loaded:
    """A record read from disk. `absent` and `unreadable` are different answers -- only one of
    them means there is nothing to report -- so they never share a sentinel."""

    state: str  # "absent" | "unreadable" | "ok"
    record: dict[str, Any] | None = None
    error: str = ""


def load_record(state_dir: Path) -> Loaded:
    path = Path(state_dir) / RECORD_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Loaded("absent")
    except OSError as exc:
        return Loaded("unreadable", error=f"{type(exc).__name__}: {exc}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        return Loaded("unreadable", error=f"not JSON: {exc}")
    if not isinstance(data, dict) or "status" not in data:
        return Loaded("unreadable", error="not a tick-watchdog record")
    return Loaded("ok", record=data)


def write_record(state_dir: Path, record: Mapping[str, Any]) -> None:
    path = Path(state_dir) / RECORD_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_history(state_dir: Path, record: Mapping[str, Any]) -> None:
    path = Path(state_dir) / HISTORY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(record), sort_keys=True) + "\n")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > HISTORY_KEEP + 500:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def _is_alive(procs: Mapping[int, Proc], pid: Any, started: Any) -> bool:
    proc = procs.get(pid) if isinstance(pid, int) else None
    return (
        proc is not None
        and not proc.zombie
        and (started is None or same_start(proc.started, started))
    )


def _armed_phrase(rec: Mapping[str, Any], now_epoch: float) -> str:
    armed = rec.get("armed_at")
    try:
        then = datetime.strptime(str(armed), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return f"armed {armed}"
    return f"armed {armed}, {fmt_duration(now_epoch - then.timestamp())} ago"


def previous_tick_line(
    loaded: Loaded, procs: Mapping[int, Proc], t: Thresholds, *, now_epoch: float | None = None
) -> str:
    """The line every tick prints about the one before it, and about what it will watch for.

    Its drained form -- the previous tick ended inside both thresholds -- is the line most ticks
    print, and a test holds its exact text: a report whose good-news branch no input can reach reads
    the day everything is healthy as a broken check."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    watching = thresholds_phrase(t)
    if loaded.state == "absent":
        return f"  tick-watchdog: armed — {watching}; no earlier record on this machine"
    if loaded.state == "unreadable" or loaded.record is None:
        return (
            f"  WARN: tick-watchdog: the previous tick's record is unreadable ({loaded.error}) — "
            f"its fate is UNKNOWN, not clean; armed — {watching}"
        )
    rec = loaded.record
    armed = _armed_phrase(rec, now_epoch)
    ran = fmt_duration(rec.get("awake_duration_s", rec.get("awake_age_s")))
    crossings = [c for c in rec.get("crossings") or [] if isinstance(c, Mapping)]
    status = rec.get("status")
    if status == "exited" and not crossings:
        return (
            f"  tick-watchdog: armed — {watching}; previous tick ({armed}) ran {ran}, inside both "
            "thresholds"
        )
    if status == "exited":
        first = crossings[0]
        return (
            f"  ALERT: tick-watchdog: the previous tick ({armed}) ran {ran} and crossed "
            f"{len(crossings)} threshold(s), first {first.get('what', '?')} at "
            f"{fmt_duration(first.get('age_s'))}; nothing was terminated (report-only), so it "
            f"held every later tick until it ended; armed — {watching}"
        )
    if status == "not_armed":
        return (
            f"  WARN: tick-watchdog: the previous tick ({armed}) ran UNWATCHED — "
            f"{rec.get('reason', 'its watcher did not start')}; armed — {watching}"
        )
    if status == "running":
        if _is_alive(procs, rec.get("tick_pid"), rec.get("tick_started")):
            drain = rec.get("drain")
            return (
                f"  WARN: tick-watchdog: another tick (pid {rec.get('tick_pid')}, {armed}) is "
                f"still running, so two ticks now overlap"
                + (f"; it is stuck — to unblock it: {drain}" if drain else "")
                + f"; armed — {watching}"
            )
        if _is_alive(procs, rec.get("watchdog_pid"), None):
            return (
                f"  tick-watchdog: armed — {watching}; the previous tick ({armed}) ended moments "
                "ago and its watchdog is still recording it"
            )
        return (
            f"  WARN: tick-watchdog: the previous tick (pid {rec.get('tick_pid')}, {armed}) ended "
            "without its watchdog recording an end — how long it ran and what it crossed are "
            f"UNKNOWN; armed — {watching}"
        )
    return (
        f"  WARN: tick-watchdog: the previous tick's record has status {status!r}, which this "
        f"version does not know; armed — {watching}"
    )


def cadence_phrase(
    newest: Mapping[str, Any] | None, bound_s: int, now_epoch: float
) -> tuple[str, str | None]:
    """(suffix for the tick's first line, ALERT line or None) from the newest cadence outcome."""
    if newest is None:
        return ("no cadence outcome recorded on this machine yet", None)
    age = now_epoch - float(newest["ts"])
    what = f"{newest.get('key')} {newest.get('kind')}"
    suffix = f"newest cadence outcome {fmt_duration(age)} ago ({what})"
    if age <= bound_s:
        return (suffix, None)
    return (
        suffix,
        f"  ALERT: tick-watchdog: no cadence step has recorded an outcome for {fmt_duration(age)} "
        f"(newest: {what} at {_iso(float(newest['ts']))}), past the {fmt_duration(bound_s)} a "
        "daily step may go — ticks are starting and reaching no cadence step; the lines after "
        "each tick header say where they stop",
    )


def cadence_report(state_dir: Path, now_epoch: float) -> tuple[str, str | None]:
    """`cadence_phrase` over the real stamps, read through the registry's own rules."""
    try:
        import cadence_registry

        report = cadence_registry.inspect_cadence(Path(state_dir), now=int(now_epoch))
        bound = cadence_registry.shortest_stale_after_s()
        newest = cadence_registry.newest_outcome(report)
    except Exception as exc:  # the line must still print; "unknown" is not "fresh"
        return (f"newest cadence outcome UNKNOWN ({type(exc).__name__}: {exc})"[:300], None)
    return cadence_phrase(newest, bound, now_epoch)


# ---------------------------------------------------------------- start / watch / status --------


def start(
    *,
    state_dir: Path,
    tick_pid: int,
    env: Mapping[str, str],
    out: TextIO | None = None,
    snapshot_fn: Callable[[], Mapping[int, Proc]] = snapshot,
    spawn: Callable[[list[str]], Any] | None = None,
) -> int:
    """Report the previous tick, record this one, spawn its watcher. Synchronous and quick, so the
    tick knows whether it is watched before it starts any step."""
    out = sys.stdout if out is None else out
    t = thresholds(env)
    state_dir = Path(state_dir)
    try:
        procs = snapshot_fn()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"  WARN: tick-watchdog: cannot read the process table ({exc}); not armed", file=out)
        return 2
    tick = procs.get(tick_pid)
    if tick is None:
        print(f"  WARN: tick-watchdog: tick pid {tick_pid} is not running; not armed", file=out)
        return 2
    previous = load_record(state_dir)
    suffix, cadence_alert = cadence_report(state_dir, time.time())
    print(f"{previous_tick_line(previous, procs, t)}; {suffix}", file=out)
    if cadence_alert:
        print(cadence_alert, file=out)
    out.flush()
    rec = previous.record
    if (
        previous.state == "ok"
        and rec is not None
        and rec.get("status") == "running"
        and not _is_alive(procs, rec.get("tick_pid"), rec.get("tick_started"))
        and not _is_alive(procs, rec.get("watchdog_pid"), None)
    ):
        append_history(state_dir, {**rec, "status": "unfinalized"})
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "running",
        "tick_pid": tick_pid,
        "tick_started": tick.started,
        "tick_command": tick.command[:200],
        "armed_at": _iso(),
        "tick_alert_s": t.tick_s,
        "command_alert_s": t.command_s,
        "poll_s": t.poll_s,
        "notes": list(t.notes),
        "watchdog_pid": None,
        "awake_age_s": 0,
        "crossings": [],
        "drain": None,
    }
    write_record(state_dir, record)
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
        "--state-dir",
        str(state_dir),
        "--tick-pid",
        str(tick_pid),
        "--tick-started",
        tick.started,
        "--armer-pid",
        str(os.getpid()),
        "--tick-alert-s",
        str(t.tick_s),
        "--command-alert-s",
        str(t.command_s),
        "--poll-s",
        str(t.poll_s),
    ]
    try:
        if spawn is None:
            # Its own session, so nothing that signals the tick's process group reaches it, and it
            # is never one of the tick's commands. stdout stays the tick's log, for its ALERTs.
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        else:
            spawn(argv)
    except OSError as exc:
        record.update(status="not_armed", reason=f"watcher did not start: {exc}"[:300])
        write_record(state_dir, record)
        print(f"  WARN: tick-watchdog: the watcher did not start ({exc}); not armed", file=out)
        return 2
    return 0


def watch(
    *,
    state_dir: Path,
    tick_pid: int,
    tick_started: str,
    armer_pid: int,
    tick_alert_s: int,
    command_alert_s: int,
    poll_s: float,
    out: TextIO | None = None,
    snapshot_fn: Callable[[], Mapping[int, Proc]] = snapshot,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_polls: int | None = None,
) -> int:
    """Observe one tick until it exits. Never signals anything."""
    out = sys.stdout if out is None else out
    state_dir = Path(state_dir)
    w = Watch(
        tick_pid=tick_pid,
        tick_started=tick_started,
        armed_at=clock(),
        tick_alert_s=tick_alert_s,
        command_alert_s=command_alert_s,
        exclude=frozenset({armer_pid, os.getpid()}),
    )
    record: dict[str, Any] = dict(load_record(state_dir).record or {})
    if record.get("tick_pid") != tick_pid or record.get("tick_started") != tick_started:
        record = {
            "schema": SCHEMA,
            "status": "running",
            "tick_pid": tick_pid,
            "tick_started": tick_started,
            "armed_at": _iso(),
            "tick_alert_s": tick_alert_s,
            "command_alert_s": command_alert_s,
            "poll_s": poll_s,
            "crossings": [],
            "drain": None,
        }
    record["watchdog_pid"] = os.getpid()
    wall_armed = time.time()

    def save() -> None:
        # Only while the file is still THIS tick's: a watcher that outlives its tick must not
        # overwrite the next tick's record. History is append-only, so it is always safe.
        current = load_record(state_dir).record or {}
        if current.get("tick_pid") in (None, tick_pid) and current.get("tick_started") in (
            None,
            tick_started,
        ):
            write_record(state_dir, record)

    def say(line: str) -> None:
        print(line, file=out)
        out.flush()

    save()
    last_refresh = clock()
    saved_command: tuple[Any, Any] = (None, None)
    last_alert: float | None = None
    polls = 0
    errors = 0
    while True:
        polls += 1
        try:
            procs = snapshot_fn()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            errors += 1
            record["snapshot_errors"] = errors
            record["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            save()
            if max_polls is not None and polls >= max_polls:
                return 1
            sleep_fn(poll_s)
            continue
        now = clock()
        v = assess(w, procs, now)
        record["awake_age_s"] = int(v.tick_age_s)
        record["last_poll_at"] = _iso()
        if v.tick_gone:
            record.update(
                status="exited",
                ended_at=_iso(),
                awake_duration_s=int(v.tick_age_s),
                wall_duration_s=int(time.time() - wall_armed),
                drain=None,
            )
            record.pop("oldest_command", None)
            save()
            append_history(state_dir, record)
            return 0
        oldest = v.oldest
        if oldest is not None:
            record["oldest_command"] = {
                "pid": oldest[0].pid,
                "command": oldest[0].command[:200],
                "age_s": int(oldest[1]),
            }
        else:
            record.pop("oldest_command", None)
        record["drain"] = v.drain
        changed = False
        for proc, age in v.new_over:
            what = describe(proc, v.tick)
            record.setdefault("crossings", []).append(
                {"at": _iso(), "kind": "command", "what": what, "pid": proc.pid, "age_s": int(age)}
            )
            say(
                f"  ALERT: tick-watchdog: {what} has run {fmt_duration(age)} of awake time, past "
                f"the {fmt_duration(command_alert_s)} per-command threshold; the tick (pid "
                f"{tick_pid}) is waiting on it, and launchd starts no tick until this one ends. "
                f"Report-only, nothing is terminated — to unblock: kill {proc.pid}"
            )
            changed = True
        if v.tick_new_over:
            culprit = (
                f"{describe(oldest[0], v.tick)} has run {fmt_duration(oldest[1])}"
                if oldest
                else "no command is running, so the tick shell itself is blocked"
            )
            record.setdefault("crossings", []).append(
                {
                    "at": _iso(),
                    "kind": "tick",
                    "what": f"the tick itself ({culprit})",
                    "pid": tick_pid,
                    "age_s": int(v.tick_age_s),
                }
            )
            say(
                f"  ALERT: tick-watchdog: the tick (pid {tick_pid}) has run "
                f"{fmt_duration(v.tick_age_s)} of awake time, past its "
                f"{fmt_duration(tick_alert_s)} threshold — {culprit}. Report-only, nothing is "
                f"terminated — to unblock: {v.drain}"
            )
            changed = True
        if changed:
            last_alert = now
        elif v.drain and last_alert is not None and now - last_alert >= REALERT_S:
            say(
                f"  ALERT: tick-watchdog: the tick (pid {tick_pid}) is still running at "
                f"{fmt_duration(v.tick_age_s)} of awake time"
                + (
                    f"; {describe(oldest[0], v.tick)} has run {fmt_duration(oldest[1])}"
                    if oldest
                    else ""
                )
                + f". To unblock: {v.drain}"
            )
            last_alert = now
            changed = True
        record["alerts"] = int(record.get("alerts") or 0) + (1 if changed else 0)
        # The live record names the command the tick is on NOW: rewritten whenever that changes
        # (a new step, or a fork that has since exec'd), not only on an ALERT or a refresh.
        current = (record.get("oldest_command") or {}).get("pid"), (
            record.get("oldest_command") or {}
        ).get("command")
        if changed or current != saved_command or clock() - last_refresh >= REFRESH_S:
            save()
            saved_command = current
            last_refresh = clock()
        if max_polls is not None and polls >= max_polls:
            return 0
        sleep_fn(poll_s)


def summary(state_dir: Path, procs: Mapping[int, Proc] | None = None) -> dict[str, Any]:
    """The live record as `status` and `mcp_server.fleet_summary` report it: the current tick's
    blocking quantities beside their thresholds, and the command that would free it."""
    loaded = load_record(state_dir)
    if procs is None:
        try:
            procs = snapshot()
        except (OSError, subprocess.SubprocessError, ValueError):
            procs = {}
    out: dict[str, Any] = {"state": loaded.state, "line": status_line(loaded, procs)}
    if loaded.error:
        out["error"] = loaded.error
    rec = loaded.record or {}
    for key in (
        "status",
        "tick_pid",
        "armed_at",
        "awake_age_s",
        "awake_duration_s",
        "tick_alert_s",
        "command_alert_s",
        "oldest_command",
        "crossings",
        "drain",
        "last_poll_at",
        "ended_at",
    ):
        if key in rec:
            out[key] = rec[key]
    if rec.get("status") == "running":
        out["tick_alive"] = _is_alive(procs, rec.get("tick_pid"), rec.get("tick_started"))
        out["watcher_alive"] = _is_alive(procs, rec.get("watchdog_pid"), None)
    return out


def status_line(loaded: Loaded, procs: Mapping[int, Proc]) -> str:
    if loaded.state != "ok" or loaded.record is None:
        detail = f": {loaded.error}" if loaded.error else ""
        return f"tick-watchdog: no readable record ({loaded.state}{detail})"
    rec = loaded.record
    if rec.get("status") == "not_armed":
        return (
            f"tick-watchdog: the last tick (armed {rec.get('armed_at')}) ran UNWATCHED — "
            f"{rec.get('reason', 'its watcher did not start')}"
        )
    if rec.get("status") != "running":
        return (
            f"tick-watchdog: last tick (armed {rec.get('armed_at')}) {rec.get('status')} after "
            f"{fmt_duration(rec.get('awake_duration_s'))}; "
            f"{len(rec.get('crossings') or [])} threshold crossing(s)"
        )
    watcher = "alive" if _is_alive(procs, rec.get("watchdog_pid"), None) else "NOT RUNNING"
    tick = "running" if _is_alive(procs, rec.get("tick_pid"), rec.get("tick_started")) else "gone"
    oldest = rec.get("oldest_command") or {}
    cmd = (
        f"; oldest command `{str(oldest.get('command'))[:80]}` {fmt_duration(oldest.get('age_s'))} "
        f"of {fmt_duration(rec.get('command_alert_s'))}"
        if oldest
        else ""
    )
    drain = f"; STUCK — to unblock: {rec['drain']}" if rec.get("drain") else ""
    return (
        f"tick-watchdog: tick pid {rec.get('tick_pid')} {tick}, armed {rec.get('armed_at')}, "
        f"{fmt_duration(rec.get('awake_age_s'))} of {fmt_duration(rec.get('tick_alert_s'))} "
        f"awake{cmd}{drain}; watcher pid {rec.get('watchdog_pid')} {watcher}, last poll "
        f"{rec.get('last_poll_at', 'never')}"
    )


def default_state_dir() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR") or Path.home() / ".codex" / "orchestrator")


# ---------------------------------------------------------------- selftest -----------------------


def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    table = parse_ps(
        "  100     1 Ss   Fri Oct  2 01:36:00 2026 bash orchestrate.sh --active\n"
        "  200   100 S    Fri Oct  2 01:36:05 2026 python3 pattern_miner.py run\n"
        "  201   200 S    Fri Oct  2 01:36:06 2026 gh api graphql\n"
        "  300   100 S    Fri Oct  2 01:36:01 2026 python3 tick_watchdog.py start\n"
        "  999     1 S    Fri Oct  2 00:00:00 2026 unrelated\n"
    )
    check(table[100].started == "Fri Oct 2 01:36:00 2026", "lstart is normalised")
    w = Watch(100, table[100].started, 0.0, 600, 60, exclude=frozenset({300}))
    v = assess(w, table, 10.0)
    check(not v.tick_gone and v.drain is None, "young: nothing to drain")
    check(v.oldest is not None and v.oldest[0].pid == 200, "the armer is never a command")
    v = assess(w, table, 75.0)
    check([p.pid for p, _ in v.new_over] == [200], "a command past its threshold is reported")
    check(v.drain == "kill 200", "with the kill that frees the tick")
    v = assess(w, table, 80.0)
    check(not v.new_over and v.drain == "kill 200", "reported once, still drainable")
    v = assess(w, table, 600.0)
    check(v.tick_new_over and not assess(w, table, 601.0).tick_new_over, "tick crossing once")
    fork = dict(table)
    fork[200] = Proc(200, 100, table[200].started, table[100].command)
    check("never reached exec" in describe(fork[200], fork[100]), "a pre-exec fork is named")
    recycled = dict(table)
    recycled[100] = Proc(100, 1, "Sat Oct  3 09:00:00 2026", "something else")
    check(assess(w, recycled, 700.0).tick_gone, "a recycled tick pid is not the tick")
    jitter = dict(table)
    jitter[100] = Proc(100, 1, "Fri Oct 2 01:36:01 2026", table[100].command)
    check(not assess(w, jitter, 700.0).tick_gone, "a start read a second later is the same tick")
    reaped_late = dict(table)
    reaped_late[100] = Proc(100, 1, table[100].started, table[100].command, "Z")
    check(assess(w, reaped_late, 700.0).tick_gone, "an unreaped (zombie) tick has exited")

    t = thresholds({})
    check((t.tick_s, t.command_s) == (TICK_ALERT_S, COMMAND_ALERT_S), "defaults")
    bad = thresholds({TICK_ALERT_ENV: "soon", COMMAND_ALERT_ENV: "-5"})
    check((bad.tick_s, bad.command_s) == (TICK_ALERT_S, COMMAND_ALERT_S), "typos ignored")
    check(len(bad.notes) == 2, "and named")
    drained = previous_tick_line(
        Loaded(
            "ok",
            {
                "status": "exited",
                "armed_at": "2026-10-02T00:40:01Z",
                "awake_duration_s": 842,
                "crossings": [],
            },
        ),
        {},
        t,
        now_epoch=datetime(2026, 10, 2, 1, 40, 1, tzinfo=timezone.utc).timestamp(),
    )
    check(
        drained.endswith(
            "(armed 2026-10-02T00:40:01Z, 1h00m ago) ran 14m02s, inside both thresholds"
        ),
        f"the drained line is reachable: {drained!r}",
    )
    check(previous_tick_line(Loaded("absent"), {}, t).startswith("  tick-watchdog: armed"), "first")
    check("UNKNOWN" in previous_tick_line(Loaded("unreadable", error="x"), {}, t), "unreadable")
    unwatched = Loaded("ok", {"status": "not_armed", "armed_at": "x", "reason": "watcher: ENOMEM"})
    check("ran UNWATCHED — watcher: ENOMEM" in previous_tick_line(unwatched, {}, t), "not armed")
    sweep = {"key": "durability-sweep", "kind": "success", "ts": 1000}
    fresh = cadence_phrase(sweep, 129600, 1000 + 3600)
    check(fresh[1] is None and "1h00m ago" in fresh[0], "a fresh cadence outcome is a suffix only")
    late = cadence_phrase(sweep, 129600, 1000 + 129601)
    check(late[1] is not None and "no cadence step" in late[1], "a stale one ALERTs")
    check(cadence_phrase(None, 129600, 0)[1] is None, "none recorded is said, not alarmed")

    # A REAL process that can never finish on its own -- it writes more than any pipe holds into a
    # pipe only it could read, the incident's shape without needing the kernel's pipe cap -- watched
    # with this process standing in for the tick and a clock that jumps past the threshold.
    child = subprocess.Popen(
        [sys.executable, "-c", "import os; r, w = os.pipe(); os.write(w, b'x' * 4_000_000)"],
        stdin=subprocess.DEVNULL,
    )
    try:
        me = None
        for _ in range(100):
            table = snapshot()
            me = table.get(os.getpid())
            if me is not None and child.pid in table:
                break
            time.sleep(0.05)
        check(me is not None and child.pid in table, "the deadlocked child is in the table")
        if me is not None:
            # The clock moves only when `watch` sleeps: each poll is 120 s of awake time later.
            fake_now = [0.0]

            def advance(_seconds: float) -> None:
                fake_now[0] += 120.0

            sink = io.StringIO()
            with tempfile.TemporaryDirectory(prefix="tick-watchdog-selftest-") as tmp:
                scratch = Path(tmp)
                watch(
                    state_dir=scratch,
                    tick_pid=os.getpid(),
                    tick_started=me.started,
                    armer_pid=-1,
                    tick_alert_s=3600,
                    command_alert_s=60,
                    poll_s=0.0,
                    out=sink,
                    sleep_fn=advance,
                    clock=lambda: fake_now[0],
                    max_polls=2,
                )
                rec = load_record(scratch).record or {}
            check(f"to unblock: kill {child.pid}" in sink.getvalue(), "the ALERT names the drain")
            check(rec.get("drain") == f"kill {child.pid}", "and so does the record")
            check(child.poll() is None, "report-only: the watchdog signalled nothing")
    finally:
        child.kill()
        child.wait(timeout=10)

    if failures:
        print("tick_watchdog selftest FAILED:\n  " + "\n  ".join(failures))
        return 1
    print(
        "tick_watchdog selftest: OK (process identity survives pid reuse and zombies read as exited; "
        "a command or a tick past its threshold is reported once with the kill that frees it; a "
        "pre-exec fork is named as one; typos never disable a threshold; the drained line is "
        "reachable; a tick that reaches no cadence step ALERTs; a real self-deadlocked writer is "
        "reported and never signalled)"
    )
    return 0


# ---------------------------------------------------------------- cli ----------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start", help="report the previous tick, record this one, arm")
    start_p.add_argument("--state-dir", type=Path, default=None)
    start_p.add_argument("--tick-pid", type=int, required=True)
    watch_p = sub.add_parser("watch", help="(spawned by start) observe one tick until it exits")
    watch_p.add_argument("--state-dir", type=Path, required=True)
    watch_p.add_argument("--tick-pid", type=int, required=True)
    watch_p.add_argument("--tick-started", required=True)
    watch_p.add_argument("--armer-pid", type=int, required=True)
    watch_p.add_argument("--tick-alert-s", type=int, required=True)
    watch_p.add_argument("--command-alert-s", type=int, required=True)
    watch_p.add_argument("--poll-s", type=float, required=True)
    status_p = sub.add_parser("status", help="the current tick's ages against its thresholds")
    status_p.add_argument("--state-dir", type=Path, default=None)
    status_p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state_dir = args.state_dir or default_state_dir()
    if args.command == "start":
        return start(state_dir=state_dir, tick_pid=args.tick_pid, env=os.environ)
    if args.command == "watch":
        return watch(
            state_dir=state_dir,
            tick_pid=args.tick_pid,
            tick_started=args.tick_started,
            armer_pid=args.armer_pid,
            tick_alert_s=args.tick_alert_s,
            command_alert_s=args.command_alert_s,
            poll_s=args.poll_s,
        )
    report = summary(state_dir)
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else report["line"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
