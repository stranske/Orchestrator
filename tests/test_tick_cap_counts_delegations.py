"""The tick's per-tick cap counts delegations, never refusals, and a bound on examined items keeps a
backlog of owned PRs from turning into unbounded reads.

THE DEFECT (2026-10-04). `tick.remote_tick` deferred the rest of the backlog once `len(chosen)` reached
`ORCH_MAX_REMOTE_PER_TICK` (default 3). `chosen` also held every row `dispatcher.delegate_remote`
refused: paused, already carrying an `agent:*` label, or with labels GitHub did not return. A refusal
spends nothing remote, yet it held a slot, and `production_reserve` reserved research capacity for an
agent that would not run. Discovery lists a closer item only when its PR already carries an `agent:*`
label, and it lists the closer items first, so a live tick filled the cap with refusals and deferred
every item behind them until keepalive finished the PRs in front. The tick logs of the live period
(2026-06-15 to 09-02, 1,857 active ticks) show it: 2,826 slots held refusals and 25 held delegations;
546 ticks deferred an item while every slot held a refusal; 65 targets that carried no agent label
when first deferred were never examined in any tick.

These tests drive the real label read through a fake `gh` on PATH that answers per target, so every
count of reads below is a count of real `subprocess` calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import claims
import tick

GH_STUB = """#!/bin/sh
echo "$*" >> "$GH_STUB_CALLS"
case "$*" in *--method*)
  for arg in "$@"; do
    case "$arg" in repos/*/issues/*/labels) num="${arg%/labels}"; num="${num##*/}";; esac
  done
  if [ -f "$GH_STUB_DIR/$num.post-fails" ]; then exit 1; fi
  exit 0;;
esac
num="${2##*/}"
if [ -f "$GH_STUB_DIR/$num.json" ]; then cat "$GH_STUB_DIR/$num.json"; exit 0; fi
echo "gh: Not Found (HTTP 404)" >&2
exit 1
"""
OWNED = ["agent:codex", "agents:keepalive"]


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake `gh` first on PATH. `gh.labels(n, [...])` sets what the read of `o/r#n` answers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    answers = tmp_path / "gh-answers"
    answers.mkdir()
    calls = tmp_path / "gh-calls.log"
    calls.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GH_STUB_CALLS", str(calls))
    monkeypatch.setenv("GH_STUB_DIR", str(answers))

    class Gh:
        def labels(self, number: int, names: list[str]) -> None:
            payload = {"number": number, "labels": [{"name": name} for name in names]}
            (answers / f"{number}.json").write_text(json.dumps(payload))

        def fail_post(self, number: int) -> None:
            (answers / f"{number}.post-fails").touch()

        def reads(self) -> list[str]:
            return [line for line in calls.read_text().splitlines() if "--method" not in line]

        def posts(self) -> list[str]:
            return [line for line in calls.read_text().splitlines() if "--method" in line]

    return Gh()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """No test here may reach the live Brain or claims: capture every run the rail would record."""
    monkeypatch.setattr(claims, "_handoff_dir", lambda: tmp_path / "handoff")
    monkeypatch.setattr(tick.feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0")
    runs: list[tuple] = []
    monkeypatch.setattr(tick.dispatcher.feedback, "record_run", lambda *a, **k: runs.append(a))
    return runs


def _cap() -> dict:
    return {"agents": {a: {"state": "ok"} for a in ("cursor", "codex", "claude", "gemini")}}


def _no_research(*args, **kwargs) -> dict:
    return {"status": "skipped", "planned": [], "active": False}


def _plan(items: list[dict], *, dry_run: bool = True, max_delegations=None, research=None):
    return tick.remote_tick(
        items,
        _cap(),
        dry_run=dry_run,
        do_ingest=False,
        env={},
        max_delegations=max_delegations,
        research_tick_fn=research or _no_research,
    )


def _item(number: int, labels: list[str] | None = None, **extra) -> dict:
    item = {"target": f"o/r#{number}", "task_type": "implement", **extra}
    if labels is not None:
        item["labels"] = labels
    return item


# --------------------------------------------------------------------------- the cap


def test_refusals_do_not_take_a_slot(gh, sandbox):
    """Three targets the live read shows owned, then one it does not, under a cap of 1. The old cap
    spent its slot on the first refusal and deferred the only target that could be delegated."""
    for n in (1, 2, 3):
        gh.labels(n, OWNED)
    gh.labels(4, [])
    out = _plan([_item(n) for n in (1, 2, 3, 4)], max_delegations=1)
    assert [row["target"] for row in out["chosen"]] == ["o/r#1", "o/r#2", "o/r#3", "o/r#4"], out
    assert out["chosen"][-1]["skip"] is None and out["deferred"] == [], out
    assert (out["delegations"], out["refused"], out["errors"]) == (1, 3, 0), out


def test_an_error_row_takes_no_slot(gh, sandbox):
    """A target with no PR number is a dispatcher error: no read, no label, no run. It is examined
    and counted as an error, never as the delegation that fills the cap."""
    gh.labels(5, [])
    out = _plan([{"target": "o/r", "task_type": "implement"}, _item(5)], max_delegations=1)
    assert out["chosen"][0]["error"] and out["chosen"][1]["skip"] is None, out
    assert (out["delegations"], out["errors"], out["deferred"]) == (1, 1, []), out
    assert gh.reads() == ["api repos/o/r/issues/5"], gh.reads()


def test_a_cap_of_zero_defers_everything_and_calls_none_of_it_drainable(gh, sandbox):
    """Reached with no delegations, the cap has nothing that frees it but the operator."""
    out = _plan([_item(n) for n in (1, 2)], max_delegations=0)
    assert out["deferred"] == ["o/r#1", "o/r#2"] and out["examined"] == 0, out
    assert (out["deferral"]["delegable"], out["deferral"]["drainable"]) == (2, 0), out
    assert gh.reads() == [], gh.reads()


# --------------------------------------------------------------------------- the examination bound


def test_a_backlog_of_owned_items_is_read_at_most_examine_cap_times(gh, sandbox):
    """Refusals no longer stop the loop, so this bound is what keeps a backlog of owned items from
    turning into unbounded label reads. Twenty owned targets with no discovery labels, cap 3: twelve
    reads, eight deferred, and the eight are named as blocking and NOT drainable, because what holds
    them is examined items that did not delegate, not this tick's delegations."""
    for n in range(1, 21):
        gh.labels(n, OWNED)
    out = _plan([_item(n) for n in range(1, 21)])
    assert (out["cap"], out["examine_cap"]) == (3, 3 * tick.EXAMINED_PER_DELEGATION) == (3, 12)
    assert len(gh.reads()) == out["examined"] == 12, (gh.reads(), out["examined"])
    assert out["deferral"]["by_examine_cap"] == [f"o/r#{n}" for n in range(13, 21)], out
    assert (out["deferral"]["delegable"], out["deferral"]["drainable"]) == (8, 0), out
    assert out["delegations"] == 0 and gh.posts() == [], out


def test_owned_closer_prs_at_the_head_cannot_hold_back_a_delegable_item(gh, sandbox):
    """The live shape: discovery lists thirty closer PRs, each already labelled `agent:codex`, ahead
    of one ready issue. The rail refuses the closers on their discovery labels, so they are examined
    after the issue, which is delegated first. In backlog order it sat behind the bound and every
    tick deferred it."""
    closers = [_item(n, OWNED, lane="closer") for n in range(1, 31)]
    for n in range(1, 31):
        gh.labels(n, OWNED)
    gh.labels(99, ["status: ready"])
    out = _plan(closers + [_item(99, ["status: ready"], lane="opener")])
    assert out["chosen"][0]["target"] == "o/r#99" and out["chosen"][0]["skip"] is None, out
    assert (out["delegations"], out["refused"], out["examined"]) == (1, 11, 12), out
    assert len(gh.reads()) == 12, gh.reads()
    assert len(out["deferral"]["by_examine_cap"]) == 19, out["deferral"]
    assert (out["deferral"]["delegable"], out["deferral"]["drainable"]) == (0, 0), out


def test_items_deferred_by_the_cap_drain_on_the_next_tick(gh, sandbox):
    """The cap's drain is its own delegations. Five ready items under a cap of 3: three delegate, two
    wait, both drainable. On the next tick the three carry their `agent:*` labels (in discovery and on
    the live read), so they are refused, take no slot, and the two delegate."""
    for n in range(1, 6):
        gh.labels(n, [])
    first = _plan([_item(n, []) for n in range(1, 6)])
    assert first["delegations"] == 3 and first["deferral"]["by_cap"] == ["o/r#4", "o/r#5"], first
    assert (first["deferral"]["delegable"], first["deferral"]["drainable"]) == (2, 2), first
    for n in (1, 2, 3):
        gh.labels(n, ["agent:codex"])
    labelled = [_item(n, ["agent:codex"]) for n in (1, 2, 3)] + [_item(n, []) for n in (4, 5)]
    second = _plan(labelled)
    assert [row["target"] for row in second["chosen"][:2]] == ["o/r#4", "o/r#5"], second
    assert (second["delegations"], second["refused"], second["deferred"]) == (2, 3, []), second


@pytest.mark.parametrize("failed_posts", [0, 1, 3])
def test_active_cap_deferrals_need_a_label_that_applied(gh, sandbox, failed_posts):
    """An attempted delegation still spends a slot, but a failed POST leaves its target unowned to
    take that slot again. The cap's deferrals drain while at least one label applied, and not when
    every POST failed. The second tick is the measured truth behind each first-tick count: with one
    POST of three failing, the two labels that applied free two slots and both deferred items go."""
    for n in range(1, 6):
        gh.labels(n, [])
        if n <= failed_posts:
            gh.fail_post(n)
    first = _plan([_item(n, []) for n in range(1, 6)], dry_run=False)
    assert first["delegations"] == 3 and len(gh.posts()) == 3, first
    assert sum(bool(row["applied"]) for row in first["chosen"]) == 3 - failed_posts
    assert first["deferral"]["delegable"] == 2, first
    assert first["deferral"]["drainable"] == (0 if failed_posts == 3 else 2), first
    applied = [n for n in (1, 2, 3) if n > failed_posts]
    for n in applied:  # what the next tick reads, live and in discovery, once a label applied
        gh.labels(n, ["agent:codex"])
    second = _plan(
        [_item(n, ["agent:codex"] if n in applied else []) for n in range(1, 6)], dry_run=False
    )
    delegated = {row["target"] for row in second["chosen"] if tick.is_delegation(row)}
    assert ({"o/r#4", "o/r#5"} <= delegated) is (first["deferral"]["drainable"] == 2), second


# --------------------------------------------------------------------------- what refusals no longer buy


def test_production_reserve_counts_only_rows_that_would_apply(gh, sandbox):
    """Research yields capacity to production rows that will run. A refused row runs no agent, so it
    reserves nothing; its target stays excluded from research, because it is another agent's work.
    """
    gh.labels(1, OWNED)
    gh.labels(2, [])
    seen: dict = {}

    def research(*args, **kwargs):
        seen.update(kwargs)
        return _no_research()

    out = _plan([_item(1), _item(2)], research=research)
    agent = out["chosen"][1]["agent"]
    assert out["chosen"][0]["skip"] and out["chosen"][1]["skip"] is None, out
    assert seen["production_reserve"] == {agent: 1}, seen["production_reserve"]
    assert {"o/r#1", "o/r#2"} <= seen["excluded_targets"], seen["excluded_targets"]


def test_a_refused_delegation_writes_no_role_edge(gh, sandbox, monkeypatch):
    """An active tick whose triage role disagrees with both targets. Only the target that is
    delegated records a run, so only it may carry the disagreement edge; written for the refused
    one, the edge pointed at a run id nothing recorded."""
    gh.labels(1, OWNED)
    gh.labels(2, [])
    triage = {
        "selector": {"invoked": True},
        "result": {"role_run_id": "role:triage:test"},
        "recommendations": {t: {"action": "skip"} for t in ("o/r#1", "o/r#2")},
    }
    monkeypatch.setattr(tick.roles, "activate_tick_triage", lambda *a, **k: triage)
    edges: list[dict] = []
    monkeypatch.setattr(tick.feedback, "record_influence_edge", lambda **k: edges.append(k))
    out = _plan([_item(1), _item(2)], dry_run=False)
    agent = out["chosen"][1]["agent"]
    assert out["chosen"][0]["applied"] is False and out["chosen"][1]["applied"] is True, out
    assert [run[0] for run in sandbox] == [f"remote:o/r#2:{agent}"], sandbox
    assert [edge["target_run_id"] for edge in edges] == [f"remote:o/r#2:{agent}"], edges
    assert edges[0]["accepted"] is False and edges[0]["source_run_id"] == "role:triage:test"
    assert gh.posts() == [f"api --method POST repos/o/r/issues/2/labels -f labels[]=agent:{agent}"]


# --------------------------------------------------------------------------- the plan's own arithmetic


def test_every_item_is_examined_or_deferred_and_the_counts_are_the_rows(gh, sandbox, tmp_path):
    """The counters are the loop's own; this pins them to the rows they count, so the plan and the
    headline cannot describe a different tick from the one that ran."""
    gh.labels(1, OWNED)
    gh.labels(2, [])
    gh.labels(3, ["agents:paused"])
    held = tmp_path / "handoff" / "claims" / claims._slug("o/r#4")
    held.mkdir(parents=True)  # held, holder unknown: blocked before any read
    items = [_item(1), _item(2), _item(3), _item(4), {"target": "o/r"}] + [
        _item(n, []) for n in (6, 7, 8)
    ]
    for n in (6, 7, 8):
        gh.labels(n, [])
    out = _plan(items, max_delegations=2)
    rows = out["chosen"]
    assert out["delegations"] == sum(tick.is_delegation(row) for row in rows) == 2, out
    assert out["refused"] == sum(bool(row["skip"]) for row in rows) == 2, out
    assert out["errors"] == sum(bool(row["error"]) for row in rows) == 1, out
    examined = len(rows) + len(out["blocked"]) + len(out["no_capacity"])
    assert out["examined"] == examined and examined + len(out["deferred"]) == len(items), out
    assert out["deferral"]["by_cap"] == out["deferred"] == ["o/r#7", "o/r#8"], out


# --------------------------------------------------------------------------- the headline


def test_the_headline_prints_each_bound_beside_its_count_and_blocking_beside_drainable(gh, sandbox):
    for n in range(1, 21):
        gh.labels(n, OWNED)
    out = _plan([_item(n) for n in range(1, 21)])
    assert tick.plan_headline(out, "PLAN", lane_live=False) == (
        "TICK-PLAN: delegations 0/3 would apply (shadow); 12 refused; examined 12/12; "
        "label reads 12 answered, 0 unanswered (refused); 0 no capacity, 0 blocked; "
        "deferred 8 (delegable 8, drainable 0) -> PLAN"
    )


def test_the_headline_when_nothing_is_refused(gh, sandbox):
    """Question 4 of the latched-gate check: the line a drained refusal prints is one some input
    produces. Counts are asserted, never truthiness, because zero is the answer here."""
    for n in (1, 2):
        gh.labels(n, [])
    line = tick.plan_headline(_plan([_item(n) for n in (1, 2)]), "PLAN", lane_live=False)
    assert "delegations 2/3 would apply (shadow); 0 refused; examined 2/12;" in line, line
    assert "deferred 0 (delegable 0, drainable 0)" in line, line


def test_the_fully_drained_line_is_one_an_empty_backlog_produces(gh, sandbox):
    assert tick.plan_headline(_plan([]), "PLAN", lane_live=False) == (
        "TICK-PLAN: delegations 0/3 would apply (shadow); 0 refused; examined 0/12; "
        "label reads 0 answered, 0 unanswered (refused); 0 no capacity, 0 blocked; "
        "deferred 0 (delegable 0, drainable 0) -> PLAN"
    )


def test_an_active_headline_counts_attempts_and_applications(gh, sandbox):
    gh.labels(1, [])
    gh.labels(2, OWNED)
    line = tick.plan_headline(_plan([_item(1), _item(2)], dry_run=False), "PLAN", lane_live=True)
    assert line.startswith("TICK-PLAN: delegations 1/3 attempted, 1 applied; 1 refused;"), line


def test_the_headline_names_dispatcher_errors_beside_refusals(gh, sandbox):
    gh.labels(5, [])
    out = _plan([{"target": "o/r", "task_type": "implement"}, _item(5)], max_delegations=1)
    line = tick.plan_headline(out, "PLAN", lane_live=False)
    assert "delegations 1/1 would apply (shadow); 0 refused, 1 dispatcher errors;" in line, line


def test_a_plan_without_the_counts_prints_unknown_never_zero():
    line = tick.plan_headline({"chosen": []}, Path("PLAN"), lane_live=False)
    assert "delegations ?/? would apply (shadow); ? refused; examined ?/?;" in line, line
    assert "deferred 0 (delegable ?, drainable ?)" in line, line
