#!/usr/bin/env python3
"""GitHub's timestamps as epoch seconds, read as the UTC they are — plus the exact conversion for the
values a retired local-time helper stored.

WHY THIS EXISTS (2026-09-23). `agent_switches.py` and `fleet_shapes.py` each carried the same
`_epoch`: `int(time.mktime(time.strptime(iso[:19], FMT)) - time.timezone)`. `mktime` reads the
struct as LOCAL time and decides DST per date, while `time.timezone` is the zone's STANDARD offset,
so on a machine in a DST zone every instant inside DST came out one hour early. On the owner's
America/Chicago machine `2026-09-22T10:00:00Z` read 1790067600 against a true 1790071200; a January
date was exact, and a UTC CI runner was exact everywhere, so CI and the live tick disagreed and
nothing that ran on CI could see it. Both modules now bind `_epoch` to `from_iso`: one definition,
so a fix cannot land in one copy and miss the other.

THE STORED VALUES. The retired helper's output survives in the Brain (`agent_switches.switched_ts`,
part of that table's key — feedback._rebase_agent_switches) and in both modules' per-PR facts
caches. `from_legacy` recovers each instant exactly: for the UTC wall clock W the helper returned
`mktime(W) - timezone`, so `localtime(value + timezone)` is W again and `timegm(W)` is the truth.
That holds wherever the helper was one-to-one, which is everywhere except the spring-forward hour,
where it sent two instants to one value. That value, and any value the helper could not have
produced in this zone, raises `Unconvertible` rather than being guessed. The conversion assumes the
value was computed in THIS process's zone: the Brain and the caches are machine-local, written by
this machine's tick.

A facts cache marks each entry that is on the UTC basis (`BASIS_KEY`) — per ENTRY, not per file.
Code from before this change passes the entries it loads through unchanged and writes the ones it
fetches itself unmarked, so a per-entry mark survives an older writer and still says which entries
that writer computed. A file-level mark would be dropped by the first older save, and the next load
would convert the already-converted entries a second time. `rebase_cache` converts only unmarked
entries; `stamp_cache` marks everything a fixed writer saves.

DEDUP (2026-09-23). Searched the tree for epoch / iso / utc / timestamp parsers. Five modules already
read GitHub timestamps correctly with `datetime.fromisoformat` — durability_sweep._parse_gh_ts
(shared by keepalive_outcomes), adapters._iso_to_epoch, ccusage_reconcile._parse_iso_ts,
switch_review._parse_iso_ts and backlog._blocker_expiry_ts — each with its own input contract
(numbers accepted, offsets honoured, broad excepts), and keepalive_outcomes._created_epoch pins the
exact `...Z` form. None holds the legacy conversion, and feedback.py cannot import them without a
cycle, so this module is new and those parsers are left as they are. `adapters._codex_rollout_for`
also calls `mktime`, correctly: it reads a LOCAL wall-clock stamp from a rollout filename. The
improvement log has no item for epoch, mktime, timezone, DST or timegm.

    python3 utc_epoch.py --selftest
"""

from __future__ import annotations

import calendar
import contextlib
import os
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

ISO_SECONDS = "%Y-%m-%dT%H:%M:%S"
BASIS_KEY = "epoch_basis"
UTC = "utc"
# The owner's zone, by name and as the equivalent POSIX rule for a machine without the tz database.
US_CENTRAL = ("America/Chicago", "CST6CDT,M3.2.0,M11.1.0")
US_CENTRAL_STANDARD_OFFSET = 6 * 3600


class Unconvertible(ValueError):
    """A stored value of the retired helper that maps to no single instant in this zone."""


def from_iso(iso: object) -> int | None:
    """Epoch seconds for a GitHub or `Date.toISOString()` timestamp (`2026-09-22T10:00:00Z`, with or
    without a fraction). The first 19 characters are read as the UTC wall clock, which is all GitHub
    writes, so the answer is the same in every zone. None for anything that is not one."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return calendar.timegm(time.strptime(iso[:19], ISO_SECONDS))
    except ValueError:
        return None


def legacy_value(instant: int) -> int:
    """What the retired helper returned, in this process's zone, for the UTC instant `instant` —
    kept executable so that `from_legacy` can check every answer it gives against it."""
    wall = time.gmtime(instant)
    return int(time.mktime((*wall[:8], -1)) - time.timezone)


def from_legacy(value: object) -> int | None:
    """The true UTC epoch for a value the retired helper stored on this machine. None passes through
    (the helper's own "no timestamp"). Raises Unconvertible for a value two instants produced (the
    spring-forward hour) or one the helper could not have produced in this zone."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise Unconvertible(f"not an epoch: {value!r}")
    try:
        instant = calendar.timegm(time.localtime(value + time.timezone))
        if legacy_value(instant) != value:
            raise Unconvertible(f"{value} is not a value the retired helper produces in this zone")
        step = time.timezone - time.altzone if time.daylight else 0
        if step and any(legacy_value(instant + d) == value for d in (step, -step)):
            raise Unconvertible(f"{value} was produced by two instants (the spring-forward hour)")
    except (OverflowError, OSError) as exc:
        raise Unconvertible(f"{value}: {exc}") from exc
    return instant


def rebase_cache(
    facts: dict[str, dict[str, Any]],
    rebase_fact: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """The cache with every unmarked entry converted by `rebase_fact` and marked, plus the counts
    (rebased, dropped). An entry `rebase_fact` cannot convert is DROPPED rather than kept on the
    wrong basis, so the next run fetches that PR again through `from_iso`."""
    out: dict[str, dict[str, Any]] = {}
    rebased = dropped = 0
    for ref, fact in facts.items():
        if fact.get(BASIS_KEY) == UTC:
            out[ref] = fact
            continue
        try:
            out[ref] = {**rebase_fact(fact), BASIS_KEY: UTC}
        except Unconvertible:
            dropped += 1
            continue
        rebased += 1
    return out, rebased, dropped


def stamp_cache(facts: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every entry marked as UTC basis — what a writer that reads through `from_iso` saves."""
    return {ref: {**fact, BASIS_KEY: UTC} for ref, fact in facts.items()}


@contextlib.contextmanager
def zone(candidates: tuple[str, ...], *, standard_offset: int, dst: bool = True) -> Iterator[str]:
    """Run the body in the first candidate zone the C library honours — its standard offset is
    `standard_offset` and it observes DST iff `dst` — and restore the caller's zone afterwards. Name
    the IANA zone first and a POSIX rule after it: without the tz database the name falls back to
    UTC silently, and a DST check run in UTC passes whatever the code does."""
    saved = os.environ.get("TZ")
    try:
        for candidate in candidates:
            os.environ["TZ"] = candidate
            time.tzset()
            if time.timezone == standard_offset and bool(time.daylight) == dst:
                yield candidate
                return
        raise RuntimeError(f"no candidate zone took effect: {candidates}")
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


# ---------------------------------------------------------------- selftest ------------------------


def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        if not cond:
            failures.append(what)

    september, true_september = "2026-09-22T10:00:00Z", 1_790_071_200
    with zone(US_CENTRAL, standard_offset=US_CENTRAL_STANDARD_OFFSET) as name:
        check(from_iso(september) == true_september, f"September in {name}: {from_iso(september)}")
        check(from_iso("2026-01-15T10:00:00Z") == 1_768_471_200, "January")
        check(from_iso("2026-09-22T10:00:00.123Z") == true_september, "a fraction is not read")
        check(from_iso("") is None and from_iso(None) is None and from_iso("soon") is None, "junk")
        # The retired arithmetic reproduces the value measured on the owner's machine.
        check(legacy_value(true_september) == 1_790_067_600, "retired helper, September")
        check(from_legacy(1_790_067_600) == true_september, "September recovered")
        check(from_legacy(None) is None, "None passes through")
        # Every hour of 2026 round-trips except the two the spring-forward collision joins.
        start = calendar.timegm((2026, 1, 1, 0, 0, 0, 0, 0, 0))
        refused = []
        for instant in range(start, start + 365 * 86400, 3600):
            try:
                back = from_legacy(legacy_value(instant))
            except Unconvertible:
                refused.append(time.strftime("%m-%dT%H", time.gmtime(instant)))
                continue
            check(back == instant, f"round trip {instant} -> {back}")
        check(refused == ["03-08T02", "03-08T03"], f"refused {refused}")
        facts = {
            "old": {"merged_ts": 1_790_067_600},
            "new": {"merged_ts": true_september, BASIS_KEY: UTC},
            "gap": {"merged_ts": legacy_value(calendar.timegm((2026, 3, 8, 2, 30, 0, 0, 0, 0)))},
        }
        out, rebased, dropped = rebase_cache(
            facts, lambda f: {**f, "merged_ts": from_legacy(f.get("merged_ts"))}
        )
        check((rebased, dropped) == (1, 1), f"counts {(rebased, dropped)}")
        check(out["old"]["merged_ts"] == true_september, "unmarked entry converted")
        check(out["new"] is facts["new"], "a marked entry is never converted twice")
        check("gap" not in out, "an unconvertible entry is dropped for refetch")
        check(all(f[BASIS_KEY] == UTC for f in stamp_cache(facts).values()), "stamp marks all")
    with zone(("UTC",), standard_offset=0, dst=False):
        check(from_iso(september) == true_september, "UTC reads the same")
        check(legacy_value(true_september) == true_september, "retired helper was exact in UTC")
        check(from_legacy(true_september) == true_september, "and converts to itself")
    if failures:
        print("utc_epoch.py selftest: FAIL — " + "; ".join(failures))
        return 1
    print(
        "utc_epoch.py selftest: OK (September reads true UTC in US Central, the retired helper's "
        "values convert back exactly across 2026, the spring-forward pair is refused, a marked cache "
        "entry is never converted twice, UTC is the identity)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
