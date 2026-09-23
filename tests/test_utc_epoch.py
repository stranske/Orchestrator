"""GitHub timestamps read as UTC, whatever the machine's zone — and the values the retired local-time
helper stored, converted exactly once.

Every test here runs in US Central, the owner's zone, because the defect was invisible anywhere else:
`int(mktime(strptime(iso[:19])) - time.timezone)` is exact on a UTC runner and an hour early for any
instant inside DST on a DST-zone machine. A regression test that inherits CI's zone passes whatever
the code does, so the zone is set here, and `utc_epoch.zone` refuses to run a body in any zone other
than the one asked for (the IANA name first, the equivalent POSIX rule for a machine without the tz
database)."""

from __future__ import annotations

import calendar
import json
import sqlite3
import time

import pytest

import agent_switches
import feedback
import fleet_shapes
import utc_epoch

SEPTEMBER_ISO = "2026-09-22T10:00:00Z"
SEPTEMBER = 1_790_071_200  # its true epoch; the retired helper read 1_790_067_600 in US Central
JANUARY = 1_768_471_200  # 2026-01-15T10:00:00Z


@pytest.fixture
def us_central():
    with utc_epoch.zone(
        utc_epoch.US_CENTRAL, standard_offset=utc_epoch.US_CENTRAL_STANDARD_OFFSET
    ) as name:
        assert time.localtime(SEPTEMBER).tm_isdst == 1, name
        yield name


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    fleet_shapes._LOOKUP_CACHE.clear()
    return tmp_path


def _utc(*fields: int) -> int:
    return calendar.timegm((*fields, 0, 0, 0))


def test_a_september_timestamp_reads_true_utc_in_us_central(us_central):
    assert utc_epoch.from_iso(SEPTEMBER_ISO) == SEPTEMBER
    assert utc_epoch.from_iso("2026-01-15T10:00:00Z") == JANUARY
    assert utc_epoch.from_iso("2026-09-22T10:00:00.123Z") == SEPTEMBER
    # What the retired helper computed here — the value measured on the owner's machine.
    assert utc_epoch.legacy_value(SEPTEMBER) == SEPTEMBER - 3600 == 1_790_067_600
    assert utc_epoch.legacy_value(JANUARY) == JANUARY, "outside DST it was exact"
    for junk in (None, "", "tomorrow", 1_790_071_200):
        assert utc_epoch.from_iso(junk) is None


def test_both_modules_read_github_through_the_one_helper(us_central):
    assert agent_switches._epoch is utc_epoch.from_iso
    assert fleet_shapes._epoch is utc_epoch.from_iso
    switch_fact = agent_switches._fact_from_graphql(
        {
            "timelineItems": {
                "nodes": [
                    {
                        "__typename": "LabeledEvent",
                        "createdAt": SEPTEMBER_ISO,
                        "label": {"name": "agent:codex"},
                    }
                ]
            },
            "commits": {"nodes": [{"commit": {"committedDate": SEPTEMBER_ISO}}]},
            "mergedAt": SEPTEMBER_ISO,
            "closedAt": SEPTEMBER_ISO,
        }
    )
    assert switch_fact["label_events"][0]["ts"] == SEPTEMBER
    assert switch_fact["commit_ts"] == [SEPTEMBER]
    assert switch_fact["merged_ts"] == switch_fact["closed_ts"] == SEPTEMBER
    shape_fact = fleet_shapes._fact_from_graphql(
        {"createdAt": SEPTEMBER_ISO, "mergedAt": SEPTEMBER_ISO}
    )
    assert shape_fact["created_ts"] == shape_fact["merged_ts"] == SEPTEMBER


def test_from_legacy_inverts_the_retired_helper_minute_by_minute_across_both_transitions(
    us_central,
):
    refused = []
    for start in (_utc(2026, 3, 7, 22, 0, 0), _utc(2026, 10, 31, 22, 0, 0)):
        for instant in range(start, start + 8 * 3600, 60):
            try:
                back = utc_epoch.from_legacy(utc_epoch.legacy_value(instant))
            except utc_epoch.Unconvertible:
                refused.append(instant)
                continue
            assert back == instant, time.strftime("%Y-%m-%dT%H:%M", time.gmtime(instant))
    # The retired helper sent 02:xx (a wall time that does not exist in Chicago that morning) and
    # 03:xx to the same value, so those two hours — and only those — cannot be recovered.
    assert refused == list(range(_utc(2026, 3, 8, 2, 0, 0), _utc(2026, 3, 8, 4, 0, 0), 60))
    assert utc_epoch.from_legacy(None) is None
    for not_an_epoch in (True, "1790067600", 1.5):
        with pytest.raises(utc_epoch.Unconvertible):
            utc_epoch.from_legacy(not_an_epoch)


def test_in_utc_the_retired_helper_was_exact_and_conversion_is_the_identity():
    with utc_epoch.zone(("UTC",), standard_offset=0, dst=False):
        for instant in (SEPTEMBER, JANUARY, _utc(2026, 3, 8, 2, 30, 0)):
            assert utc_epoch.legacy_value(instant) == instant
            assert utc_epoch.from_legacy(instant) == instant


# ---------------------------------------------------------------- stored values -------------------


def _keepalive_pr(rid: str, ref: str, *, days_ago: int = 5) -> None:
    feedback.record_run(
        rid,
        ref,
        "implement",
        "codex",
        mode="remote",
        ts=int(time.time()) - days_ago * 86400,
        source="keepalive",
    )
    feedback.record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")


def _switch_fact(first: int, second: int, commits: list[int]) -> dict:
    return {
        "label_events": [
            {"ts": first, "kind": "added", "label": "agent:cursor"},
            {"ts": second, "kind": "added", "label": "agent:codex"},
        ],
        "commit_ts": commits,
        "state": "MERGED",
        "merged_ts": commits[-1],
        "closed_ts": commits[-1],
        "keepalive_state": {"read": "no_marker", "untrusted_markers": 0, "comments_read": 3},
    }


def _legacy(fact: dict) -> dict:
    """The fact as the retired helper cached it on this machine: every time an hour early in DST."""
    early = utc_epoch.legacy_value
    return {
        **fact,
        "label_events": [{**e, "ts": early(e["ts"])} for e in fact["label_events"]],
        "commit_ts": [early(t) for t in fact["commit_ts"]],
        "merged_ts": early(fact["merged_ts"]),
        "closed_ts": early(fact["closed_ts"]),
    }


def _write_legacy_facts(path, facts: dict) -> None:
    """Exactly what the pre-fix save_facts wrote: no basis mark on the file or on any entry."""
    path.write_text(
        json.dumps(
            {"schema": agent_switches.FACTS_SCHEMA, "version": 1, "updated_at": 1, "facts": facts}
        )
    )


TRUE_FACT = _switch_fact(SEPTEMBER, SEPTEMBER + 3600, [SEPTEMBER + 600, SEPTEMBER + 4000])


def _no_fetch(repo, numbers):
    raise AssertionError(f"nothing should be fetched: {repo} {numbers}")


def test_a_legacy_facts_cache_converts_once_and_survives_an_older_writer(us_central, brain):
    _keepalive_pr("a", "o/r#1")
    _write_legacy_facts(brain / "agent-switches-facts.json", {"o/r#1": _legacy(TRUE_FACT)})
    first = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    assert first["counts"]["facts_rebased_this_run"] == 1
    assert first["counts"]["facts_dropped_this_run"] == 0
    on_disk = json.loads((brain / "agent-switches-facts.json").read_text())["facts"]["o/r#1"]
    assert on_disk[utc_epoch.BASIS_KEY] == utc_epoch.UTC, "the conversion is persisted"
    assert {k: v for k, v in on_disk.items() if k != utc_epoch.BASIS_KEY} == TRUE_FACT
    second = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    assert second["counts"]["facts_rebased_this_run"] == 0
    assert agent_switches.load_facts(brain)["o/r#1"]["label_events"][1]["ts"] == SEPTEMBER + 3600
    # An older checkout loads the marked entry, passes it through unchanged, and adds one it fetched
    # itself, unmarked. Only that one may be converted.
    older = json.loads((brain / "agent-switches-facts.json").read_text())
    older["facts"]["o/r#2"] = _legacy(TRUE_FACT)
    (brain / "agent-switches-facts.json").write_text(json.dumps(older))
    loaded = agent_switches.load_facts(brain)
    assert loaded["o/r#1"]["label_events"][1]["ts"] == SEPTEMBER + 3600, "never converted twice"
    assert loaded["o/r#2"]["label_events"][1]["ts"] == SEPTEMBER + 3600


def test_an_unconvertible_cached_fact_is_dropped_and_fetched_again(us_central, brain):
    _keepalive_pr("a", "o/r#1")
    spring_forward = _utc(2026, 3, 8, 2, 30, 0)
    fact = _switch_fact(spring_forward, spring_forward + 600, [spring_forward + 60, JANUARY])
    _write_legacy_facts(brain / "agent-switches-facts.json", {"o/r#1": _legacy(fact)})
    calls = []

    def fetch(repo, numbers):
        calls.append((repo, tuple(numbers)))
        return {n: TRUE_FACT for n in numbers}

    payload = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=fetch, state_fetch_fn=_no_fetch
    )
    assert payload["counts"]["facts_dropped_this_run"] == 1
    assert calls == [("o/r", (1,))], "the dropped fact is read again from GitHub"
    assert agent_switches.load_facts(brain)["o/r#1"]["label_events"][1]["ts"] == SEPTEMBER + 3600


LEGACY_COLUMNS = (
    "pr_ref TEXT NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL, "
    "switched_ts INTEGER NOT NULL, auto_label INTEGER NOT NULL DEFAULT 0, commits_before INTEGER, "
    "commits_after INTEGER, merged INTEGER, durability TEXT, recorded_ts INTEGER NOT NULL"
)
# The key before #330, which feedback._widen_agent_switches rebuilds on open, and the widened key
# (source added 2026-09-23) the live Brain already carries.
KEY_SHAPES = {
    "pr_ref+switched_ts": ("", "pr_ref, switched_ts"),
    "pr_ref+switched_ts+source": (
        ", source TEXT NOT NULL DEFAULT 'label', delegation_source TEXT",
        "pr_ref, switched_ts, source",
    ),
}


def _seed_pre_fix_brain(path, extra: str, key: str, rows: list[tuple]) -> None:
    """A Brain as the pre-fix code left it: agent_switches rows keyed by hour-early times, and no
    migration marker. Written with raw sqlite3 because feedback._conn() would migrate it."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE agent_switches ({LEGACY_COLUMNS}{extra}, PRIMARY KEY ({key}))")
        conn.executemany(
            "INSERT INTO agent_switches (pr_ref, from_agent, to_agent, switched_ts, auto_label, "
            "commits_before, commits_after, merged, durability, recorded_ts) "
            "VALUES (?,?,?,?,0,1,1,1,'durable',?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _switch_rows() -> list[tuple]:
    with feedback._conn() as conn:
        return conn.execute(
            "SELECT pr_ref, from_agent, to_agent, switched_ts FROM agent_switches "
            "ORDER BY pr_ref, switched_ts"
        ).fetchall()


@pytest.mark.parametrize("shape", sorted(KEY_SHAPES))
def test_pre_fix_brain_rows_are_rekeyed_once_and_a_rerun_writes_no_twin(us_central, brain, shape):
    extra, key = KEY_SHAPES[shape]
    early = utc_epoch.legacy_value
    # o/r#1 is in the run's window; o/r#7 and o/r#9 have aged out of it, so only the migration can
    # reach them. o/r#7 switched twice an hour apart: its first switch's true key is the second's
    # stored one, so moving the rows one at a time in place would violate the key.
    assert early(SEPTEMBER + 3600) == SEPTEMBER
    _seed_pre_fix_brain(
        feedback.DB_PATH,
        extra,
        key,
        [
            ("o/r#1", "cursor", "codex", early(SEPTEMBER + 3600), 100),
            ("o/r#7", "cursor", "codex", early(SEPTEMBER), 100),
            ("o/r#7", "codex", "claude", early(SEPTEMBER + 3600), 100),
            ("o/r#9", "claude", "codex", early(SEPTEMBER - 86400 * 90), 100),
            ("o/r#8", "codex", "gemini", early(JANUARY), 100),
        ],
    )
    _keepalive_pr("a", "o/r#1")  # the first feedback._conn(): the migration runs here
    expected = [
        ("o/r#1", "cursor", "codex", SEPTEMBER + 3600),
        ("o/r#7", "cursor", "codex", SEPTEMBER),
        ("o/r#7", "codex", "claude", SEPTEMBER + 3600),
        ("o/r#8", "codex", "gemini", JANUARY),
        ("o/r#9", "claude", "codex", SEPTEMBER - 86400 * 90),
    ]
    assert _switch_rows() == expected
    with feedback._conn() as conn:
        (raw,) = conn.execute(
            "SELECT detail FROM data_migrations WHERE name=?",
            (feedback.AGENT_SWITCHES_UTC_MIGRATION,),
        ).fetchone()
    detail = json.loads(raw)
    assert (detail["examined"], detail["rebased"], detail["unconvertible"]) == (5, 4, 0)
    assert detail["deduplicated"] == 0
    _write_legacy_facts(brain / "agent-switches-facts.json", {"o/r#1": _legacy(TRUE_FACT)})
    payload = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    assert payload["counts"]["recorded_in_brain"] == 1
    assert _switch_rows() == expected, "the corrected switch replaced its row, not doubled it"
    agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    assert _switch_rows() == expected, "a second open and run shift nothing again"


def test_a_pre_fix_policy_row_is_rekeyed_and_rederived_onto_the_same_key(us_central, brain):
    """A policy switch is keyed by its delegation_log entry's timestamp, which the facts cache keeps
    as raw ISO, so the fixed parser derives the true instant directly. The row the retired helper
    wrote an hour early is re-keyed onto that instant, and the next run replaces it rather than
    doubling it. Its commits split against the CONVERTED commit times: a cache left on the old basis
    would put both commits before the switch."""
    extra, key = KEY_SHAPES["pr_ref+switched_ts+source"]
    conn = sqlite3.connect(str(feedback.DB_PATH))
    try:
        conn.execute(f"CREATE TABLE agent_switches ({LEGACY_COLUMNS}{extra}, PRIMARY KEY ({key}))")
        conn.execute(
            "INSERT INTO agent_switches (pr_ref, from_agent, to_agent, switched_ts, auto_label, "
            "commits_before, commits_after, merged, durability, recorded_ts, source, "
            "delegation_source) VALUES ('o/r#1','codex','claude',?,1,1,1,1,'durable',100,"
            "'policy','route_weights')",
            (utc_epoch.legacy_value(SEPTEMBER),),
        )
        conn.commit()
    finally:
        conn.close()
    _keepalive_pr("a", "o/r#1")  # the migration runs here
    read = (
        "SELECT pr_ref, from_agent, to_agent, switched_ts, source, commits_before, commits_after "
        "FROM agent_switches"
    )
    with feedback._conn() as c:
        assert [row[:5] for row in c.execute(read)] == [
            ("o/r#1", "codex", "claude", SEPTEMBER, "policy")
        ]
    fact = {
        "label_events": [{"ts": SEPTEMBER - 7200, "kind": "added", "label": "agent:codex"}],
        "commit_ts": [SEPTEMBER - 3000, SEPTEMBER + 600],
        "state": "MERGED",
        "merged_ts": SEPTEMBER + 600,
        "closed_ts": SEPTEMBER + 600,
        "keepalive_state": {
            "read": "marker",
            "author": "stranske-keepalive[bot]",
            "switch_count": 1,
            "delegation_log": [
                {
                    "iteration": 5,
                    "previous_agent": "codex",
                    "chosen_agent": "claude",
                    "reason": "codex-stalled (x; delegation_source: route_weights)",
                    "timestamp": SEPTEMBER_ISO,
                    "delegation_source": "route_weights",
                }
            ],
            "untrusted_markers": 0,
            "comments_read": 1,
        },
    }
    _write_legacy_facts(brain / "agent-switches-facts.json", {"o/r#1": _legacy(fact)})
    payload = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    assert payload["counts"]["recorded_policy"] == 1
    with feedback._conn() as c:
        assert list(c.execute(read)) == [("o/r#1", "codex", "claude", SEPTEMBER, "policy", 1, 1)]


def test_a_row_an_older_checkout_writes_after_the_migration_is_replaced(us_central, brain):
    _keepalive_pr("a", "o/r#1")
    agent_switches.save_facts(brain, {"o/r#1": TRUE_FACT}, now=1)
    agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    with feedback._conn() as conn:  # the hour-early twin a pre-fix checkout would INSERT OR REPLACE
        conn.execute(
            "INSERT INTO agent_switches (pr_ref, from_agent, to_agent, switched_ts, auto_label, "
            "commits_before, commits_after, merged, durability, recorded_ts) "
            "VALUES ('o/r#1','cursor','codex',?,0,1,1,1,'durable',1)",
            (utc_epoch.legacy_value(SEPTEMBER + 3600),),
        )
    assert len(_switch_rows()) == 2
    agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_fetch, state_fetch_fn=_no_fetch
    )
    assert _switch_rows() == [("o/r#1", "cursor", "codex", SEPTEMBER + 3600)]


def test_a_row_the_helper_never_wrote_keeps_its_key_and_a_twin_landing_on_it_merges(
    us_central, brain
):
    # 01:00Z on 2026-11-01 is 01:00 CST, an hour the retired helper never produced here (it read
    # that wall clock as CDT), so a row holding it was written in another zone and cannot be
    # converted: it keeps its key. The hour-early row for the same instant converts ONTO that key,
    # which makes it the same switch recorded twice, and the later-recorded row survives.
    utc_written = _utc(2026, 11, 1, 1, 0, 0)
    with pytest.raises(utc_epoch.Unconvertible):
        utc_epoch.from_legacy(utc_written)
    hour_early = utc_epoch.legacy_value(utc_written)
    assert utc_epoch.from_legacy(hour_early) == utc_written
    _seed_pre_fix_brain(
        feedback.DB_PATH,
        *KEY_SHAPES["pr_ref+switched_ts"],
        [
            ("o/r#1", "cursor", "codex", hour_early, 100),
            ("o/r#1", "gemini", "codex", utc_written, 200),
        ],
    )
    assert _switch_rows() == [("o/r#1", "gemini", "codex", utc_written)]
    with feedback._conn() as conn:
        (raw,) = conn.execute(
            "SELECT detail FROM data_migrations WHERE name=?",
            (feedback.AGENT_SWITCHES_UTC_MIGRATION,),
        ).fetchone()
    detail = json.loads(raw)
    assert (detail["unconvertible"], detail["deduplicated"], detail["rebased"]) == (1, 1, 0)


def test_a_dst_spanning_pr_measures_its_true_hours_to_merge(us_central, brain):
    feedback.record_run(
        "k", "o/r#1", "implement", "codex", ts=int(time.time()) - 10 * 86400, source="keepalive"
    )
    feedback.record_outcome("k", merged=True, durability="durable")
    opened, merged = _utc(2026, 10, 31, 20, 0, 0), _utc(
        2026, 11, 2, 20, 0, 0
    )  # 48 h, over fall-back
    legacy = {
        "title": "fix: x",
        "labels": [],
        "paths": ["src/x.py"],
        "commits": 1,
        "created_ts": utc_epoch.legacy_value(opened),
        "merged_ts": utc_epoch.legacy_value(merged),
    }
    assert (legacy["merged_ts"] - legacy["created_ts"]) / 3600 == 49.0, "the retired reading"
    path = brain / "fleet-shapes-facts.json"
    path.write_text(json.dumps({"schema": fleet_shapes.FACTS_SCHEMA, "facts": {"o/r#1": legacy}}))
    reader = fleet_shapes.time_to_merge_summary(brain, 60)  # a reader converts in memory
    assert reader["codex"]["median_hours"] == 48.0
    payload = fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_no_fetch)
    assert payload["counts"]["facts_rebased_this_run"] == 1
    assert payload["shapes"][0]["agents"]["codex"]["hours_to_merge_median"] == 48.0
    saved = json.loads(path.read_text())["facts"]["o/r#1"]
    assert (saved["created_ts"], saved["merged_ts"], saved[utc_epoch.BASIS_KEY]) == (
        opened,
        merged,
        utc_epoch.UTC,
    )
    again = fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_no_fetch)
    assert again["counts"]["facts_rebased_this_run"] == 0
    assert again["shapes"][0]["agents"]["codex"]["hours_to_merge_median"] == 48.0
