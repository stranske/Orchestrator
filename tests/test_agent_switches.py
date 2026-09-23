"""Paired agent-switch observations: every from→to switch a PR's label timeline shows lands in the
Brain with commits before/after and the outcome; bots and the owner are excluded; a PR gh cannot
return is named as missing. The hashed `sample` step was retired on 2026-09-22, when the opener
began labelling every PR it creates `agent:auto`; the tick still runs the measurement."""

from __future__ import annotations

import json
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


def _switched_fact(*, auto=False):
    events = [{"ts": T0, "kind": "added", "label": "agent:cursor"}]
    if auto:
        events.append({"ts": T0 + 60, "kind": "added", "label": "agent:auto"})
    events += [
        {"ts": T0 + 3600, "kind": "removed", "label": "agent:cursor"},
        {"ts": T0 + 3600, "kind": "added", "label": "agent:codex"},
    ]
    return {"label_events": events, "commit_ts": [T0 + 600, T0 + 1200, T0 + 5000]}


def _single_fact():
    return {
        "label_events": [{"ts": T0, "kind": "added", "label": "agent:codex"}],
        "commit_ts": [T0 + 600],
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
        window_days=60, state_dir=brain, fetch_fn=_fetch_recorder(calls, switched_numbers=(1, 2))
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
    again = agent_switches.run(window_days=60, state_dir=brain, fetch_fn=_fetch_recorder(calls))
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
        window_days=60, state_dir=brain, fetch_fn=_fetch_recorder([], switched_numbers=(1,))
    )
    lines = agent_switches.render_report_lines(agent_switches.summary_for_report(brain))
    assert lines[0].startswith("AGENT-SWITCHES (60d): 1 of 2 keepalive PRs")
    assert "sampling retired 2026-09-22" in lines[0]
    assert lines[1].strip() == "cursor->codex: 1 (merged 1, bad 0)"
