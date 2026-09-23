"""Paired agent-switch observations: every from→to switch a PR's label timeline shows lands in the
Brain with commits before/after and the outcome; bots and the owner are excluded; a PR gh cannot
return is named as missing. The hashed `sample` step was retired on 2026-09-22, when the opener
began labelling every PR it creates `agent:auto`; the tick still runs the measurement.

Since 2026-09-23 the same read also takes the latest keepalive state marker from a TRUSTED writer and
records each delegation_log entry as a `source='policy'` row with its delegation_source, because the
delegation policy never relabels and so its switches never appear in a label timeline."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import agent_switches
import feedback
import paths


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


def _pr(rid, repo, number, agent, *, days_ago=5, merged=True, durability="durable", source=None):
    ts = int(time.time()) - days_ago * 86400
    feedback.record_run(
        rid,
        f"{repo}#{number}",
        "implement",
        agent,
        mode="remote",
        ts=ts,
        source="keepalive",
        routing_metadata={"attribution_source": source} if source else None,
    )
    feedback.record_outcome(
        rid,
        adjudicated_verdict="PASS",
        verifier_verdict="PASS",
        merged=merged,
        durability=durability,
    )
    return ts


T0 = 1_700_000_000
NO_MARKER = {"read": "no_marker", "untrusted_markers": 0, "comments_read": 3}


def _switched_fact(*, auto=False):
    events = [{"ts": T0, "kind": "added", "label": "agent:cursor"}]
    if auto:
        events.append({"ts": T0 + 60, "kind": "added", "label": "agent:auto"})
    events += [
        {"ts": T0 + 3600, "kind": "removed", "label": "agent:cursor"},
        {"ts": T0 + 3600, "kind": "added", "label": "agent:codex"},
    ]
    return {
        "label_events": events,
        "commit_ts": [T0 + 600, T0 + 1200, T0 + 5000],
        "keepalive_state": dict(NO_MARKER),
    }


def _single_fact():
    return {
        "label_events": [{"ts": T0, "kind": "added", "label": "agent:codex"}],
        "commit_ts": [T0 + 600],
        "keepalive_state": dict(NO_MARKER),
    }


def _fetch_recorder(calls, switched_numbers=()):
    def fetch(repo, numbers):
        calls.append((repo, tuple(numbers)))
        out = {}
        for n in numbers:
            if n == 404:
                continue  # a PR gh cannot return
            out[n] = _switched_fact(auto=(n == 2)) if n in switched_numbers else _single_fact()
        return out

    return fetch


def _no_state_reads(repo, numbers):
    raise AssertionError(f"a fresh fact carries its state; nothing to backfill ({repo} {numbers})")


# --------------------------------------------------------------- keepalive state fixtures ---------


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))


# Two switches the policy recorded: a stall resolved from the route weights, then one from the static
# preference order. Reasons are the exact shapes agent_delegation_policy.js writes.
DELEGATION_LOG = [
    {
        "iteration": 5,
        "previous_agent": "codex",
        "chosen_agent": "claude",
        "reason": "codex-stalled (2 consecutive rounds without progress; "
        "delegation_source: route_weights)",
        "timestamp": _iso(T0 + 2000) + ".412Z",
    },
    {
        "iteration": 11,
        "previous_agent": "claude",
        "chosen_agent": "cursor",
        "reason": "claude-stalled (2 consecutive rounds without progress; "
        "delegation_source: static (route-weights-insufficient-evidence))",
        "timestamp": _iso(T0 + 4000) + ".007Z",
    },
]


def _node(kind, login, payload=None, *, text="Keepalive summary"):
    """A comment node in the GraphQL shape: a Bot login is its bare slug, without `[bot]`."""
    body = (
        text if payload is None else f"{text}\n\n<!-- keepalive-state:v1 {json.dumps(payload)} -->"
    )
    return {"author": {"__typename": kind, "login": login}, "body": body}


def _two_entry_nodes():
    return [
        _node("Bot", "coderabbitai", text="review"),
        # the pre-migration marker: keepalive later moved its state to an App-owned comment
        _node("Bot", "agents-workflows-bot", {"iteration": 0, "delegation_log": []}),
        _node(
            "Bot",
            "stranske-keepalive",
            {
                "iteration": 12,
                "current_agent": "cursor",
                "delegation_source": "static",
                "switch_count": 2,
                "delegation_log": DELEGATION_LOG,
            },
        ),
        _node("User", "stranske", text="looks good"),
    ]


def _untrusted_nodes():
    forged = {"iteration": 9, "switch_count": 1, "delegation_log": DELEGATION_LOG[:1]}
    return [
        _node("User", "mallory", forged),
        _node("Bot", "coderabbitai", forged),
        _node("User", "stranske-keepalive", forged),  # a User can never pass for the keepalive App
        _node("Bot", "stranske", forged),  # nor a Bot for the owner's PAT identity
    ]


# --------------------------------------------------------------- label path (unchanged) -----------


def test_derive_switches_reads_the_arriving_label_and_splits_commits():
    d = agent_switches.derive_switches(_switched_fact(auto=True))
    assert d["agents"] == ["cursor", "codex"] and d["auto_label"] is True
    assert d["switches"] == [
        {
            "from_agent": "cursor",
            "to_agent": "codex",
            "switched_ts": T0 + 3600,
            "commits_before": 2,
            "commits_after": 1,
        }
    ]
    assert agent_switches.derive_switches(_single_fact())["switches"] == []


def test_run_records_pairs_in_the_brain_and_excludes_bots(brain):
    _pr("c1", "o/r0", 1, "codex")  # switched cursor->codex, no auto
    _pr("c2", "o/r1", 2, "codex", durability="broke_later")  # switched, agent:auto present
    _pr("c3", "o/r1", 3, "codex")  # single agent
    _pr("d1", "o/r1", 404, "codex")  # gh cannot return it
    _pr("b1", "o/r0", 5, "none", source="bot:renovate")
    _pr("h1", "o/r0", 6, "none", source="human")
    _pr("u1", "o/r0", 7, "none")  # unattributed, still a PR whose labels can show a switch
    calls: list = []
    payload = agent_switches.run(
        window_days=60,
        state_dir=brain,
        fetch_fn=_fetch_recorder(calls, switched_numbers=(1, 2)),
        state_fetch_fn=_no_state_reads,
    )
    c = payload["counts"]
    assert c["prs"] == 5 and c["with_facts"] == 4 and c["missing_facts"] == 1
    assert c["switched_prs"] == 2 and c["switches"] == 2 and c["recorded_in_brain"] == 2
    assert c["auto_labeled"] == 1 and c["auto_and_switched"] == 1
    assert payload["missing_facts"] == ["o/r1#404"]
    cell = payload["transitions"]["cursor->codex"]
    assert (cell["n"], cell["merged"], cell["bad"], cell["auto_label"]) == (2, 2, 1, 1)
    assert cell["commits_before_median"] == 2.0 and cell["commits_after_median"] == 1.0
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT pr_ref, from_agent, to_agent, auto_label, commits_before, commits_after, merged, "
            "durability FROM agent_switches ORDER BY pr_ref"
        ).fetchall()
    assert rows == [
        ("o/r0#1", "cursor", "codex", 0, 2, 1, 1, "durable"),
        ("o/r1#2", "cursor", "codex", 1, 2, 1, 1, "broke_later"),
    ]
    asked = sorted(n for _, numbers in calls for n in numbers)
    assert asked == [1, 2, 3, 7, 404], "bots and the owner are never fetched"
    on_disk = json.loads((brain / "agent-switches.json").read_text())
    assert on_disk["counts"]["switched_prs"] == 2
    calls.clear()
    again = agent_switches.run(
        window_days=60,
        state_dir=brain,
        fetch_fn=_fetch_recorder(calls),
        state_fetch_fn=_no_state_reads,
    )
    assert calls == [] and again["counts"]["recorded_in_brain"] == 2, "facts are fetched once"
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_switches").fetchone()[0] == 2


def test_the_tick_runs_the_measurement_and_no_longer_samples():
    """Retired 2026-09-22: once the opener labels every PR it creates `agent:auto`, the hashed sample
    had no eligible population (it reported `candidates: 0` every tick) and no untreated arm. The tick
    must still run the measurement exactly once, and no executable line may read the old rate."""
    script = (paths.REPO_ROOT / "orchestrate.sh").read_text(encoding="utf-8")
    assert script.count("agent_switches.py" + '" run') == 1, "the measurement must run exactly once"
    assert script.count("agent_switches.py" + '" sample') == 0
    live = [
        line
        for line in script.splitlines()
        if "ORCH_AUTO_SWITCH_SAMPLE_RATE" in line and not line.lstrip().startswith("#")
    ]
    assert live == [], live
    assert not hasattr(agent_switches, "sample")
    with pytest.raises(SystemExit):
        agent_switches.main(["sample", "--rate", "0.5"])


def test_report_lines_name_the_unrecorded_state_and_the_base_rate(brain):
    summary = agent_switches.summary_for_report(brain)
    assert summary["state"] == "not yet recorded"
    assert agent_switches.render_report_lines(summary)[0].startswith("AGENT-SWITCHES: not yet")
    _pr("c1", "o/r0", 1, "codex")
    _pr("c2", "o/r1", 2, "codex")
    agent_switches.run(
        window_days=60,
        state_dir=brain,
        fetch_fn=_fetch_recorder([], switched_numbers=(1,)),
        state_fetch_fn=_no_state_reads,
    )
    lines = agent_switches.render_report_lines(agent_switches.summary_for_report(brain))
    assert lines[0].startswith("AGENT-SWITCHES (60d): 1 of 2 keepalive PRs")
    assert "sampling retired 2026-09-22" in lines[0]
    assert lines[1].strip() == "cursor->codex: 1 (merged 1, bad 0)"


# --------------------------------------------------------------- policy path ----------------------


def test_the_latest_trusted_marker_yields_its_two_delegation_entries():
    state = agent_switches.select_state(_two_entry_nodes(), older_comments=False)
    assert state["read"] == "marker"
    assert state["author"] == "stranske-keepalive[bot]", "GraphQL's bare Bot slug gets its suffix"
    assert state["switch_count"] == 2 and state["untrusted_markers"] == 0
    assert [e["delegation_source"] for e in state["delegation_log"]] == ["route_weights", "static"]
    fact = {
        "label_events": [],
        "commit_ts": [T0 + 1000, T0 + 3000, T0 + 5000],
        "keepalive_state": state,
    }
    policy = agent_switches.derive_policy_switches(fact)
    assert policy["malformed"] == 0
    assert policy["switches"] == [
        {
            "from_agent": "codex",
            "to_agent": "claude",
            "switched_ts": T0 + 2000,
            "iteration": 5,
            "delegation_source": "route_weights",
            "commits_before": 1,
            "commits_after": 2,
        },
        {
            "from_agent": "claude",
            "to_agent": "cursor",
            "switched_ts": T0 + 4000,
            "iteration": 11,
            "delegation_source": "static",
            "commits_before": 2,
            "commits_after": 1,
        },
    ]
    assert agent_switches.derive_switches(fact)["switches"] == [], "no label moved"


def test_a_marker_from_an_untrusted_author_is_ignored():
    only_untrusted = agent_switches.select_state(_untrusted_nodes(), older_comments=False)
    assert only_untrusted["read"] == "no_marker"
    assert only_untrusted["untrusted_markers"] == 4
    fact = {"label_events": [], "commit_ts": [], "keepalive_state": only_untrusted}
    assert agent_switches.derive_policy_switches(fact)["switches"] == []
    # An arbitrary LATER commenter cannot replace the preceding trusted state (GoalsAndPlumbing §4).
    later = agent_switches.select_state(
        _two_entry_nodes() + _untrusted_nodes(), older_comments=False
    )
    assert later["read"] == "marker" and later["author"] == "stranske-keepalive[bot]"
    assert len(later["delegation_log"]) == 2 and later["untrusted_markers"] == 4


def test_no_marker_is_a_measured_zero_and_a_truncated_read_is_not():
    plain = [_node("Bot", "coderabbitai", text="review"), _node("User", "stranske", text="lgtm")]
    none = agent_switches.select_state(plain, older_comments=False)
    assert none == {"read": "no_marker", "untrusted_markers": 0, "comments_read": 2}
    cut = agent_switches.select_state(plain, older_comments=True)
    assert cut["read"] == "truncated", "older comments may hold the marker: unread, not zero"
    broken = [_node("Bot", "stranske-keepalive", text="x <!-- keepalive-state:v1 {not json -->")]
    assert agent_switches.select_state(broken, older_comments=False)["read"] == "unparseable"
    prs = [{"ref": f"o/r#{n}", "repo": "o/r", "number": n, "merged": True} for n in (1, 2, 3)]
    facts = {
        "o/r#1": {"label_events": [], "commit_ts": [], "keepalive_state": none},
        "o/r#2": {"label_events": [], "commit_ts": [], "keepalive_state": cut},
        "o/r#3": {"label_events": [], "commit_ts": []},  # cached before the state was read
    }
    payload, recordable = agent_switches.aggregate(prs, facts, now=T0, window_days=60)
    c = payload["counts"]
    assert (c["state_read"], c["state_no_marker"], c["state_unread"]) == (1, 1, 2)
    assert payload["state_unread_reasons"] == {"not_yet_read": 1, "truncated": 1}
    assert c["policy_switches"] == 0 and recordable == []
    summary = {
        "state": "recorded",
        "counts": c,
        "state_unread_reasons": payload["state_unread_reasons"],
    }
    assert "from the keepalive state of 1 PRs (2 unread: not_yet_read 1, truncated 1)" in (
        agent_switches.policy_phrase(summary)
    )


def test_run_records_policy_rows_beside_unchanged_label_rows(brain):
    _pr("c1", "o/r0", 1, "codex")  # a label switch AND two policy switches
    _pr("c2", "o/r0", 2, "codex")  # only forged markers
    _pr("c3", "o/r0", 3, "codex")  # no marker at all
    states = {
        1: agent_switches.select_state(_two_entry_nodes(), older_comments=False),
        2: agent_switches.select_state(_untrusted_nodes(), older_comments=False),
        3: agent_switches.select_state(
            [_node("User", "stranske", text="hi")], older_comments=False
        ),
    }

    def fetch(repo, numbers):
        out = {}
        for n in numbers:
            fact = _switched_fact() if n == 1 else _single_fact()
            out[n] = {**fact, "keepalive_state": states[n]}
        return out

    payload = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=fetch, state_fetch_fn=_no_state_reads
    )
    c = payload["counts"]
    assert (c["switched_prs"], c["switches"]) == (1, 1), "the label counts are the label counts"
    assert (c["policy_switched_prs"], c["policy_switches"]) == (1, 2)
    assert (c["policy_route_weights"], c["policy_static"], c["policy_source_unknown"]) == (1, 1, 0)
    assert (c["state_read"], c["state_marker"], c["state_unread"]) == (3, 1, 0)
    assert c["untrusted_markers_ignored"] == 4
    assert (c["recorded_in_brain"], c["recorded_policy"]) == (3, 2)
    assert payload["policy_transitions"]["codex->claude"]["route_weights"] == 1
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT pr_ref, from_agent, to_agent, switched_ts, auto_label, commits_before, "
            "commits_after, merged, durability, source, delegation_source FROM agent_switches "
            "ORDER BY switched_ts"
        ).fetchall()
    assert rows == [
        ("o/r0#1", "codex", "claude", T0 + 2000, 0, 2, 1, 1, "durable", "policy", "route_weights"),
        ("o/r0#1", "cursor", "codex", T0 + 3600, 0, 2, 1, 1, "durable", "label", None),
        ("o/r0#1", "claude", "cursor", T0 + 4000, 0, 2, 1, 1, "durable", "policy", "static"),
    ]
    again = agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=_no_state_reads, state_fetch_fn=_no_state_reads
    )
    assert again["counts"]["recorded_in_brain"] == 3
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_switches").fetchone()[0] == 3


OLD_AGENT_SWITCHES = """
CREATE TABLE agent_switches (
  pr_ref TEXT NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL, switched_ts INTEGER NOT NULL,
  auto_label INTEGER NOT NULL DEFAULT 0, commits_before INTEGER, commits_after INTEGER,
  merged INTEGER, durability TEXT, recorded_ts INTEGER NOT NULL,
  PRIMARY KEY (pr_ref, switched_ts)
);
"""


def test_migration_widens_an_existing_table_and_keeps_every_row(monkeypatch, tmp_path):
    """A Brain created before 2026-09-23 keeps every row, gains the two columns, and its key widens
    so a policy row landing on a label row's second cannot delete it (INSERT OR REPLACE would)."""
    old_rows = [
        ("o/r#1", "cursor", "codex", T0, 0, 1, 2, 1, "durable", T0 + 9),
        ("o/r#2", "claude", "codex", T0 + 5, 1, 3, 0, 0, "abandoned", T0 + 9),
    ]
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(OLD_AGENT_SWITCHES)
        conn.executemany("INSERT INTO agent_switches VALUES (?,?,?,?,?,?,?,?,?,?)", old_rows)
    monkeypatch.setattr(feedback, "DB_PATH", db)
    with feedback._conn() as conn:
        carried = conn.execute(
            "SELECT pr_ref, from_agent, to_agent, switched_ts, auto_label, commits_before, "
            "commits_after, merged, durability, recorded_ts FROM agent_switches ORDER BY pr_ref"
        ).fetchall()
        kinds = conn.execute(
            "SELECT DISTINCT source, delegation_source FROM agent_switches"
        ).fetchall()
        conn.execute(
            agent_switches.INSERT_SWITCH,
            ("o/r#1", "codex", "claude", T0, 0, 1, 2, 1, "durable", T0 + 10, "policy", "static"),
        )
        both = conn.execute(
            "SELECT source FROM agent_switches WHERE pr_ref='o/r#1' ORDER BY source"
        ).fetchall()
        migrated = conn.execute("PRAGMA table_info(agent_switches)").fetchall()
    assert carried == old_rows and kinds == [("label", None)]
    assert both == [("label",), ("policy",)], "a policy row must not replace a label row"
    with feedback._conn() as conn:  # idempotent
        assert conn.execute("SELECT COUNT(*) FROM agent_switches").fetchone()[0] == 3
    fresh_db = tmp_path / "fresh.db"
    monkeypatch.setattr(feedback, "DB_PATH", fresh_db)
    with feedback._conn() as conn:
        fresh = conn.execute("PRAGMA table_info(agent_switches)").fetchall()
    assert migrated == fresh, "a migrated Brain and a new Brain hold the same table"


def test_a_cached_fact_gets_its_state_backfilled_without_touching_its_labels(brain):
    for n in (1, 2, 3):
        _pr(f"c{n}", "o/r0", n, "codex")
    legacy = {k: v for k, v in _switched_fact().items() if k != "keepalive_state"}
    agent_switches.save_facts(
        brain, {f"o/r0#{n}": json.loads(json.dumps(legacy)) for n in (1, 2, 3)}, now=T0
    )
    asked: list = []

    def states(repo, numbers):
        asked.append(tuple(numbers))
        got = {1: agent_switches.select_state(_two_entry_nodes(), older_comments=False)}
        return {n: got[n] for n in numbers if n in got}  # 2 and 3 fail to read

    first = agent_switches.run(
        window_days=60,
        state_dir=brain,
        fetch_limit=2,
        fetch_fn=_no_state_reads,
        state_fetch_fn=states,
    )
    assert asked == [(1, 2)], "the budget bounds the backfill"
    assert first["counts"]["state_backfilled_this_run"] == 2
    assert first["counts"]["state_unread"] == 2, "a failed read and an unread fact are both unread"
    assert first["state_unread_reasons"] == {"failed": 1, "not_yet_read": 1}
    assert first["counts"]["policy_switches"] == 2 and first["counts"]["switches"] == 3
    cached = agent_switches.load_facts(brain)
    assert all(cached[f"o/r0#{n}"]["label_events"] == legacy["label_events"] for n in (1, 2, 3))
    assert cached["o/r0#2"]["keepalive_state"] == {"read": "failed", "attempts": 1}
    for _ in range(4):
        agent_switches.run(
            window_days=60, state_dir=brain, fetch_fn=_no_state_reads, state_fetch_fn=states
        )
    attempts = agent_switches.load_facts(brain)["o/r0#2"]["keepalive_state"]["attempts"]
    assert (
        attempts == agent_switches.STATE_READ_ATTEMPTS
    ), "a PR that never reads stops being retried"
    assert (
        sum(1 for numbers in asked for n in numbers if n == 2) == agent_switches.STATE_READ_ATTEMPTS
    )


def _graphql_pr(number, *, nodes, older=False):
    return {
        "number": number,
        "state": "MERGED",
        "mergedAt": "2023-11-14T22:13:20Z",
        "closedAt": "2023-11-14T22:13:20Z",
        "timelineItems": {
            "nodes": [
                {
                    "__typename": "LabeledEvent",
                    "createdAt": "2023-11-14T22:13:20Z",
                    "label": {"name": "agent:codex"},
                }
            ]
        },
        "commits": {"nodes": [{"commit": {"committedDate": "2023-11-14T22:23:20Z"}}]},
        "comments": {"pageInfo": {"hasPreviousPage": older}, "nodes": nodes},
    }


def test_the_facts_query_reads_the_state_in_the_same_request():
    """Facts are fetched once per PR, after it closes — so the state must come with them."""
    queries: list[str] = []

    def gh_json(args):
        queries.append(args[-1])
        prs = {"p7": _graphql_pr(7, nodes=_two_entry_nodes()), "p8": _graphql_pr(8, nodes=[])}
        return {"data": {"repository": prs}}

    facts = agent_switches.fetch_facts("o/r", [7, 8], gh_json=gh_json)
    assert len(queries) == 1, "one request carries the label timeline, commits and comments"
    assert queries[0].count(agent_switches.STATE_SELECTION) == 2
    assert facts[7]["keepalive_state"]["author"] == "stranske-keepalive[bot]"
    assert len(facts[7]["keepalive_state"]["delegation_log"]) == 2
    assert facts[8]["keepalive_state"]["read"] == "no_marker"
    assert facts[7]["label_events"][0]["label"] == "agent:codex" and facts[7]["commit_ts"]


def test_a_pr_whose_comments_cannot_be_read_keeps_its_label_timeline():
    label_only = agent_switches._pr_selection(9, with_state=False)
    with_state = agent_switches._pr_selection(9, with_state=True)
    assert "comments(" not in label_only
    assert with_state == label_only[:-2] + agent_switches.STATE_SELECTION + " }"
    queries: list[str] = []

    def gh_json(args):
        queries.append(args[-1])
        if "comments(" in args[-1]:
            return None  # the comments connection fails, e.g. a resource limit
        pr = _graphql_pr(9, nodes=[])
        pr.pop("comments")
        return {"data": {"repository": {"p9": pr}}}

    facts = agent_switches.fetch_facts("o/r", [9], gh_json=gh_json)
    assert facts[9]["label_events"] and facts[9]["commit_ts"], "the label timeline survives"
    assert facts[9]["keepalive_state"] == {"read": "failed", "attempts": 1}
    assert [("comments(" in q) for q in queries] == [True, False]


def test_report_and_tick_line_carry_label_policy_and_route_weights_counts(brain):
    _pr("c1", "o/r0", 1, "codex")
    state = agent_switches.select_state(_two_entry_nodes(), older_comments=False)

    def fetch(repo, numbers):
        return {n: {**_switched_fact(), "keepalive_state": state} for n in numbers}

    agent_switches.run(
        window_days=60, state_dir=brain, fetch_fn=fetch, state_fetch_fn=_no_state_reads
    )
    summary = agent_switches.summary_for_report(brain)
    policy = (
        "policy switches 2 on 1 PRs, 1 via route_weights (1 static, 0 unknown), "
        "from the keepalive state of 1 PRs (0 unread)"
    )
    lines = agent_switches.render_report_lines(summary)
    assert "(1 label switches; 0 facts missing); " + policy in lines[0]
    assert "  policy codex->claude: 1 (1 via route_weights, merged 1, bad 0)" in lines
    tick = agent_switches.render_tick_line(summary)
    assert (
        tick.startswith("  SWITCHES: 1 of 1 keepalive PRs switched agents by label")
        and policy in tick
    )
    assert "3 recorded" in tick
    script = (paths.REPO_ROOT / "orchestrate.sh").read_text(encoding="utf-8")
    assert script.count("agent_switches.py" + '" tick-line') == 1, "the tick prints the shared line"
    assert script.count("SWITCHES" + ": {") == 0, "and holds no second rendering of its own"


def test_an_artifact_from_before_the_state_read_reports_unmeasured_not_zero(brain):
    old = {"window_days": 60, "counts": {"switched_prs": 9, "with_facts": 899, "switches": 10}}
    agent_switches.write_json_atomic(brain / "agent-switches.json", old)
    summary = agent_switches.summary_for_report(brain)
    line = agent_switches.render_report_lines(summary)[0]
    assert "policy switches not measured (this artifact predates the keepalive-state read)" in line
    assert "policy switches 0" not in line and "route_weights" not in line
    assert "policy switches not measured" in agent_switches.render_tick_line(summary)
