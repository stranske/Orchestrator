#!/usr/bin/env python3
"""Outcomes for REVIEW runs: a closer-judge decision graded by what became of the merge it judged.

WHY THIS EXISTS (P3 part B, measured 2026-11-27 on the live Brain, runs since 2026-11-10 14:00Z).
Every `task_type='review'` run is an offload (2,017 of them), and 34 had an outcome, all of them
research-program word counts. So neither learner, nor the comparative LP, could say which family is
better at review. The largest caller is closer-core's judge: 838 runs on 184 issues in that window.
It is asked, for an issue whose PR merged while the post-merge verifier did not PASS, whether the
issue may close (`close`) or still needs `evidence`, a `repair` or a `followup`, and the closer acts
on the answer. The decision and the PR it was about were kept only in the closer's ledger row
(`<state>/targets/<repo>_<issue>_<pr>.json`, key `repo#issue@pr`, `judgement.result.run_id`), and
that row is dropped when the target completes, days before the merge's durability is judged
(`durability_sweep.GRACE_DAYS`). So no outcome could ever be written for these runs.

THE RULE.
1. `ingest` copies each ledger judgement into the Brain table `review_decisions` (one row per run,
   first record kept): the run id the closer stored, the decision it acted on, `subject_id`
   (`repo#issue`) and `review_of` (`repo#pr`), both from the ledger KEY. A row is taken only when
   its run exists in the Brain as a `review` run whose own `subject_id` names the same issue. It runs
   on every tick, because the ledger keeps only the latest judgement of a row.
2. `label` grades a `close` decision by the merge of the PR it judged, as the durability sweep
   judged it on that PR's own run rows (a keepalive run on `repo#pr`, or a run whose `pr_number`
   is that PR): `durable` is PASS, `broke_later` / `reverted` / `reopened` / `reworked` /
   `abandoned` is FAIL, both with that durability. A merge the sweep closed as `unjudgeable` gives
   the review run the same: durability `unjudgeable`, class `unjudgeable_merge`, excluded from
   learning. A merge still `pending` waits for the sweep (its own drain); no merged run row waits
   too, re-read every pass. Conflicting verdicts on one merge write nothing.
3. `evidence` and `repair` decisions have NO outcome rule. They are counted under `awaiting_rule`,
   never graded (lever 4 below says why).

ROUND RECORDS (2026-11-27, second unit). The ledger read lost 27 of the first 40 judge runs after
deploy (18 of 26 `close`): the closer replaces or drops the row before the hourly tick reads it. Both
lanes also append every judgement to their own `rounds.jsonl` (an item with the issue `target`, the
deciding `provider`, and for the closer the `pr`), which they never rewrite. `ingest_rounds` reads
those, every tick and from the start of the file, so history since 2026-11-01 is recovered too. The
run is the item's `judge.run_id` when the lane wrote one (both lanes from 2026-11-27); before that it
is the one `review` run the lane's judge made on that issue with that agent inside the round (after
the previous round's end, not after its own); two such runs are counted `ambiguous` and skipped.

THE OPENER JUDGE (same shape). It decides `start` / `park` / `done` for an issue before any work. A
`start` the round acted on (item action `start`) is graded by the merge of the PR the opener linked
to that issue (`pr` in its target row), with the same rule as a closer `close`. A judgement the round
did not act on (format-unfixed, no capacity, would-start ...) is skipped as `not_acted`; an acted
start with no linked PR yet waits (`no_pr_yet`, re-read every pass). This grades the start together
with the work that followed it: a durable merge says the issue was ready to start; it does not
separate the judge from the implementing agent.

LEVER 4 (2026-11-27). Two more decisions are graded, each by a GitHub fact, never by a check or by
another judge (one GraphQL batch per pass, only for decisions not yet final; no answer writes nothing
and is re-read next pass):
- closer `followup` claims the remainder is real, separable work. The lane writes the issue it filed
  into the round item that filed it (`followup`, and `route` when not `followup`). The run is that
  item's `judge.run_id`, or the one decision by the item's agent in the latest round that judged the
  same issue and PR, when it is a `followup` (else `followup_unattributed`). Graded by the filed
  issue: closed `NOT_PLANNED` / `DUPLICATE` is FAIL (durability `abandoned`); closed by merged PRs
  whose merges the sweep judged `durable` is PASS; open, closed with no merged PR, or a merge still
  pending wait; a filed merge that failed is not graded (that grades the implementer). A `[backlog]`
  route is parked by design and has no rule; a `followup` the lane never filed is `followup_not_acted`.
  Confound, stated: "an agent implemented it and it held" does not prove the remainder was needed.
- opener `done` (acted: `closed-done`) claims the issue was already delivered. A reopen at or after
  the close is FAIL (`reopened`); closed for the sweep's `GRACE_DAYS` with no reopen is PASS
  (`durable`); younger waits.
`park`, `repair`, `evidence`, and the model's raw answer before the host's downgrade have no rule:
see the lever-4 concept note for why.

FINISHING THE JOINS (2026-11-27, measured on a copy of the live Brain: 365 graded before, 448 after).
- A merge also counts from a run on the PR itself (target `repo#pr`, no `pr_number`: local
  delegates). 41 merged decisions had only such rows and waited as `no_merge_row` forever.
- A round with two or more same-agent runs (the lane re-judged after evidence or an unusable
  answer) takes the latest run whose own offload log answered the decision the round acted on
  (`answered`); all 155 `ambiguous` rounds resolved. A round no log answers stays `ambiguous`.
- A PR the sweep closed `abandoned` with nothing merged is final, not a wait. An opener `start` on
  it is graded by the issue's own fate with the follow-up rule (closed not planned FAIL, delivered
  by a durable merge PASS); a closer `close` on it is counted `pr_closed_unmerged`, no rule.

The label is the durability sweep's or GitHub's issue state, never a green check (CLAUDE.md §2). This module writes only
outcome rows it owns (notes start with `NOTE_PREFIX`) and never touches another writer's row.
Kill switch: `ORCH_DISABLE_STEPS=review-outcomes`. Rollback: `restore` deletes the outcome rows
this module wrote; `review_decisions` is additive.

CLI:
  python3 src/review_outcomes.py run [--json]       ingest + label (the tick's step)
  python3 src/review_outcomes.py probe [--since TS] the value chain, one line per graded rule
  (`rule <lane> <decision>: decisions N, graded N`), and the pass number (closer runs after PASS_SINCE)
  python3 src/review_outcomes.py restore            delete this module's outcome rows
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import feedback

PRODUCER = "closer-core.judge"
OPENER = "opener-core.judge"
NOTE_PREFIX = "review outcome (closer-judge): "
OPENER_NOTE_PREFIX = "review outcome (opener-judge): "
NOTE_PREFIXES = {PRODUCER: NOTE_PREFIX, OPENER: OPENER_NOTE_PREFIX}
DECISIONS = frozenset({"close", "evidence", "repair", "followup"})
GRADED_DECISIONS = frozenset({"close", "followup"})
OPENER_DECISIONS = frozenset({"start", "park", "done"})
GRADED = {PRODUCER: GRADED_DECISIONS, OPENER: frozenset({"start", "done"})}
# Decisions graded by the durability the sweep gave the merge in `review_of` (the first rule).
MERGE_GRADED = {PRODUCER: frozenset({"close"}), OPENER: frozenset({"start"})}
# GitHub's close reasons that say the filed follow-up was not work worth doing.
NOT_DONE_REASONS = frozenset({"NOT_PLANNED", "DUPLICATE"})
GH_BATCH = 40
# The round action that means the opener acted on the decision; any other is `not_acted`.
OPENER_ACTED = {"start": "start", "park": "park", "done": "closed-done"}
TARGET_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<issue>\d+)$")
FAILED_MERGES = frozenset({"broke_later", "reverted", "reopened", "reworked", "abandoned"})
FINAL_MERGES = FAILED_MERGES | {"durable"}
KEY_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<issue>\d+)@(?P<pr>\d+)$")
# The pass number (P3 part B): this many graded outcomes on closer-judge runs made after deploy.
PASS_OUTCOMES = 100
# Deploy of the first unit (2026-11-27T06:10:38Z, the first decision recorded): the pass number
# counts closer-judge runs made at or after it.
PASS_SINCE = 1795759838


# The capability's value chain (value_chain_monitor row `review-outcomes`, 2026-11-28). The grades
# are acted on when a lane judge is picked from the learned weights that carry them: the weekly
# relearn folds them into the `review` bucket, and roles.route_role reads that bucket for the
# adjudicator (roles.ROLE_WEIGHTS_FROM_ROUTE_AS). The judge run records the bucket and the chosen
# agent's observations in routing_metadata.route_decision.
CAPABILITY_ID = "review-outcomes"
ROUTE_BUCKET = "review"
# Build pass number (2026-11-28): this many lane-judge picks drawn from weights that carry grades.
ACTED_PASS = 20


def graded_weights_since(con) -> int | None:
    """When the learned weights first carried the grades: the first relearn written after
    PASS_SINCE. Every grade was written after PASS_SINCE and the relearn window covers it, so that
    version and every later one count them. None until the weekly relearn has run once."""
    row = con.execute("SELECT MIN(ts) FROM route_weights WHERE ts > ?", (PASS_SINCE,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _drawn_from_grades(raw, ts: int, weights_since: int | None) -> bool:
    if weights_since is None or ts < weights_since:
        return False
    try:
        decision = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("route_decision")
    except (ValueError, TypeError, AttributeError):
        return False
    return (
        isinstance(decision, dict)
        and decision.get("learned_bucket") == ROUTE_BUCKET
        and int(decision.get("learned_n_obs") or 0) > 0
    )


def value_chain(con, since: int, now: int) -> dict:
    """The value chain over lane-judge runs in [since, now], from the Brain alone.

    demand: lane-judge runs (closer and opener judges' offloads). invocation: those with a recorded
    decision. usable: those graded PASS/FAIL. acted_on: judge picks drawn from the graded weights
    (bucket `review`, chosen agent with observations, after the first relearn that carried grades).
    outcome: acted-on runs graded since, success: PASS. Set beside outcome: excluded (graded into an
    excluded class), awaiting the sweep (a graded decision kind not labelled yet) and ungraded (no
    decision, or a decision with no outcome rule)."""
    callers = (_caller(closer_state()), _caller(opener_state()))
    prefixes = tuple(NOTE_PREFIXES.values())
    weights_since = graded_weights_since(con)
    n: Counter[str] = Counter()
    for ts, producer, decision, notes, verdict, meta in con.execute(
        "SELECT r.ts, d.producer, d.decision, o.notes, o.adjudicated_verdict, r.routing_metadata "
        "FROM runs r LEFT JOIN review_decisions d ON d.run_id=r.run_id "
        "LEFT JOIN outcomes o ON o.run_id=r.run_id "
        "WHERE r.task_type='review' AND r.target IN (?,?) AND r.ts BETWEEN ? AND ?",
        (*callers, since, now),
    ):
        n["demand"] += 1
        n["invocation_count"] += decision is not None
        ours = str(notes or "").startswith(prefixes)
        graded = ours and bool(verdict)
        n["usable"] += graded
        if not _drawn_from_grades(meta, int(ts), weights_since):
            continue
        n["acted_on"] += 1
        if graded:
            n["outcome"] += 1
            n["outcome_success"] += verdict == "PASS"
        elif ours:
            n["outcome_excluded"] += 1
        elif decision in GRADED.get(producer, ()):
            n["outcome_awaiting_sweep"] += 1
        else:
            n["outcome_ungraded"] += 1
    fields = (
        "demand",
        "invocation_count",
        "usable",
        "acted_on",
        "outcome",
        "outcome_success",
        "outcome_excluded",
        "outcome_awaiting_sweep",
        "outcome_ungraded",
    )
    return {
        **{f: n[f] for f in fields},
        "outcome_open": 0,
        "outcome_no_merge": 0,
        "graded_weights_since": weights_since,
    }


def acted_pass(con, now: int) -> dict:
    """Build pass number: lane-judge picks drawn from graded weights since they first carried them."""
    since = graded_weights_since(con)
    value = 0 if since is None else value_chain(con, since, now)["acted_on"]
    return {
        "measure": "lane-judge picks drawn from graded review weights",
        "value": value,
        "pass_at": ACTED_PASS,
        "passed": value >= ACTED_PASS,
        "since": since,
    }


def closer_state() -> Path:
    return Path(
        os.environ.get("CLOSER_CORE_STATE", Path.home() / ".codex" / "orchestrator" / "closer-core")
    )


def opener_state() -> Path:
    return Path(
        os.environ.get("OPENER_CORE_STATE", Path.home() / ".codex" / "orchestrator" / "opener-core")
    )


def _caller(state: Path) -> str:
    """`runs.target` of a lane judge's offload (dispatcher: `offload:<cwd>`)."""
    return "offload:" + str(state / "scratch")


def _ledger_judgements(state: Path):
    """(path, key, judgement) for every closer ledger row that stores a routed judgement."""
    for path in sorted((state / "targets").glob("*.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            yield path, None, None
            continue
        if not isinstance(row, dict):
            continue
        judgement = row.get("judgement")
        if isinstance(judgement, dict) and isinstance(judgement.get("result"), dict):
            yield path, row.get("key"), judgement


def ingest(state: Path | None = None, *, now: int | None = None) -> Counter:
    """Copy each ledger judgement with a Brain run id into `review_decisions`."""
    state = state or closer_state()
    now = int(time.time()) if now is None else int(now)
    counts: Counter = Counter()
    with feedback._conn() as c:
        for _path, key, judgement in _ledger_judgements(state):
            if judgement is None:
                counts["unreadable"] += 1
                continue
            result = judgement["result"]
            run_id = result.get("run_id")
            if not run_id:
                counts["no_run_id"] += 1  # CLI fallback: no Brain run to grade
                continue
            match = KEY_RE.match(str(key or ""))
            decision = result.get("decision")
            if not match or decision not in DECISIONS:
                counts["malformed"] += 1
                continue
            subject = f"{match['repo']}#{match['issue']}"
            run = c.execute(
                "SELECT task_type, json_extract(routing_metadata,'$.subject_id') FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run:
                counts["unknown_run"] += 1
                continue
            if run[0] != "review" or str(run[1] or "").lower() != subject.lower():
                counts["subject_mismatch"] += 1
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO review_decisions "
                "(run_id, producer, subject_id, review_of, decision, judged_ts, recorded_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    PRODUCER,
                    subject,
                    f"{match['repo']}#{match['pr']}",
                    decision,
                    int(judgement["at"]) if isinstance(judgement.get("at"), (int, float)) else None,
                    now,
                ),
            )
            counts["recorded" if cur.rowcount else "known"] += 1
    return counts


def _round_ts(value) -> int | None:
    try:
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError, OverflowError):
        return None


def _round_items(state: Path, counts: Counter):
    """(round_ts, previous_round_ts, item) for every judged item in `<state>/rounds.jsonl`."""
    path = state / "rounds.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        counts["rounds_missing"] += 1
        return
    rounds = []
    for line in lines:
        try:
            rnd = json.loads(line)
        except ValueError:
            counts["round_unreadable"] += 1
            continue
        ts = _round_ts(rnd.get("ts")) if isinstance(rnd, dict) else None
        if ts is None:
            counts["round_unreadable"] += 1
            continue
        items = rnd.get("items")
        rounds.append(
            (
                ts,
                (
                    [i for i in items if isinstance(i, dict) and isinstance(i.get("judge"), dict)]
                    if isinstance(items, list)
                    else []
                ),
            )
        )
    rounds.sort(key=lambda r: r[0])
    previous = None
    for ts, items in rounds:
        for item in items:
            yield ts, previous, item
        previous = ts


def _judge_runs(c, state: Path) -> dict:
    """{(subject lower, agent): [(ts, run_id)]} for the lane judge's review runs."""
    out: dict = defaultdict(list)
    for run_id, agent, ts, subject in c.execute(
        "SELECT run_id, agent, ts, json_extract(routing_metadata,'$.subject_id') FROM runs "
        "WHERE task_type='review' AND target=?",
        (_caller(state),),
    ):
        if subject:
            out[(str(subject).lower(), agent)].append((ts, run_id))
    return out


def _log_dir() -> Path:
    import dispatcher  # the one definition of where offload logs live

    return dispatcher.DISPATCH_LOG_DIR


_OBJECT_START = re.compile(r"^[ \t]*\{", re.M)


def _answers(text: str, decisions) -> list[str]:
    """Every decision named by a JSON object that opens a line of `text`, in order. An answer may
    be pretty-printed over many lines; codex prints it inside an `agent_message` event."""
    found: list[str] = []
    decoder = json.JSONDecoder()
    for start in _OBJECT_START.finditer(text):
        try:
            obj, _end = decoder.raw_decode(text, start.end() - 1)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            found += _answers(str(item.get("text") or ""), decisions)
        elif obj.get("decision") in decisions:
            found.append(str(obj["decision"]))
    return found


def answered(run_id: str, decisions) -> str | None:
    """The decision a judge run answered, read from its own offload log (its last answer), or None."""
    parts = str(run_id).split(":")
    if len(parts) != 3 or parts[0] != "offload":
        return None
    try:
        text = (_log_dir() / f"offload.{parts[1]}.{parts[2]}.log").read_text(errors="replace")
    except OSError:
        return None
    said = _answers(text, decisions)
    return said[-1] if said else None


def _opener_pr(state: Path, target: str) -> int | None:
    path = state / "targets" / (re.sub(r"[^a-z0-9]+", "_", target.lower()) + ".json")
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or str(row.get("target", "")).lower() != target.lower():
        return None
    pr = row.get("pr") or row.get("invite_pr")
    if isinstance(pr, int):
        return pr
    return int(pr) if isinstance(pr, str) and pr.isdigit() else None


def ingest_rounds(producer: str, state: Path | None = None, *, now: int | None = None) -> Counter:
    """Copy every judgement in a lane's round records into `review_decisions`."""
    opener = producer == OPENER
    state = state or (opener_state() if opener else closer_state())
    now = int(time.time()) if now is None else int(now)
    decisions = OPENER_DECISIONS if opener else DECISIONS
    counts: Counter = Counter()
    with feedback._conn() as c:
        runs = _judge_runs(c, state)
        for round_ts, previous, item in _round_items(state, counts):
            judge = item["judge"]
            decision = judge.get("decision")
            match = TARGET_RE.match(str(item.get("target") or ""))
            if not match or decision not in decisions:
                counts["malformed"] += 1
                continue
            subject = item["target"]
            if opener:
                if item.get("action") != OPENER_ACTED[decision]:
                    counts["not_acted"] += 1
                    continue
                pr = _opener_pr(state, subject) if decision == "start" else None
                if decision == "start" and pr is None:
                    counts["no_pr_yet"] += 1  # the opener links the PR later; re-read every pass
                    continue
            else:
                pr = item.get("pr")
                if not (isinstance(pr, int) or str(pr).isdigit()):
                    counts["malformed"] += 1
                    continue
            run_id = judge.get("run_id")
            if run_id:
                run = c.execute(
                    "SELECT task_type, json_extract(routing_metadata,'$.subject_id') FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if not run:
                    counts["unknown_run"] += 1
                    continue
                if run[0] != "review" or str(run[1] or "").lower() != subject.lower():
                    counts["subject_mismatch"] += 1
                    continue
            else:
                low = previous if previous is not None else round_ts - 3600
                found = [
                    rid
                    for ts, rid in sorted(runs.get((subject.lower(), judge.get("provider")), []))
                    if low <= ts <= round_ts
                ]
                if len(found) > 1:
                    # Two or more runs in one round: the lane re-judged (after collecting evidence,
                    # or after an unusable answer) and acted on the LAST usable answer. Take the
                    # latest run whose own log answered the decision the round acted on. A round
                    # already resolved on an earlier pass is not re-read.
                    done = {
                        rid
                        for (rid,) in c.execute(
                            "SELECT run_id FROM review_decisions WHERE producer=? AND subject_id=? "
                            "AND judged_ts=? AND decision=?",
                            (producer, subject, round_ts, decision),
                        )
                    }
                    said = [rid for rid in found if rid in done] or [
                        rid for rid in found if answered(rid, decisions) == decision
                    ]
                    if said:
                        counts["resolved_by_log"] += 1
                        found = said[-1:]
                if len(found) != 1:
                    counts["ambiguous" if found else "no_run"] += 1
                    continue
                run_id = found[0]
            cur = c.execute(
                "INSERT OR IGNORE INTO review_decisions "
                "(run_id, producer, subject_id, review_of, decision, judged_ts, recorded_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    producer,
                    subject,
                    f"{match['repo']}#{pr}" if pr is not None else "",
                    decision,
                    round_ts,
                    now,
                ),
            )
            counts["recorded" if cur.rowcount else "known"] += 1
    return counts


# The work runs on a PR, as the sweep's rows name them: a run whose pr_number is the PR (target the
# PR or its issue), or a run on the PR itself (a local delegate or keepalive continuing on its
# branch) that names the PR in its target and leaves pr_number empty. 2026-11-27: 41 merged
# decisions had only the second kind and waited forever as `no_merge_row`. A target naming the
# ISSUE with no pr_number is not taken: which PR it worked on is not on the row.
_PR_RUNS = (
    "COALESCE(r.task_type,'')!='review' AND ((r.pr_number=? AND lower(r.target) IN (?,?)) "
    "OR (r.pr_number IS NULL AND lower(r.target)=?))"
)


def _pr_run_args(subject_id: str, review_of: str) -> tuple:
    repo, pr = review_of.rsplit("#", 1)
    issue = subject_id.rsplit("#", 1)[1]
    return (int(pr), f"{repo.lower()}#{pr}", f"{repo.lower()}#{issue}", f"{repo.lower()}#{pr}")


def closed_unmerged(c, subject_id: str, review_of: str) -> bool:
    """True when the sweep closed the PR's work as `abandoned` with nothing merged: a final state."""
    return bool(
        c.execute(
            "SELECT 1 FROM runs r JOIN outcomes o ON o.run_id=r.run_id "
            "WHERE o.merged=0 AND o.durability='abandoned' AND " + _PR_RUNS + " LIMIT 1",
            _pr_run_args(subject_id, review_of),
        ).fetchone()
    )


def merge_verdict(c, subject_id: str, review_of: str) -> tuple[str, str]:
    """(state, durability) of the merge `review_of`, from the sweep's verdicts on its run rows.

    state: `final` (durability is a sweep verdict), `unjudgeable`, `pending`, `no_merge_row` or
    `conflict`."""
    rows = c.execute(
        "SELECT DISTINCT o.durability FROM runs r JOIN outcomes o ON o.run_id=r.run_id "
        "WHERE o.merged=1 AND " + _PR_RUNS,
        _pr_run_args(subject_id, review_of),
    ).fetchall()
    durabilities = {str(d or "pending") for (d,) in rows}
    if not durabilities:
        return "no_merge_row", ""
    final = durabilities & FINAL_MERGES
    if len(final) > 1:
        return "conflict", ",".join(sorted(final))
    if final:
        return "final", final.pop()
    if "unjudgeable" in durabilities:
        return "unjudgeable", "unjudgeable"
    return "pending", "pending"


def _owned(c, run_id: str):
    row = c.execute(
        "SELECT adjudicated_verdict, durability, failure_class, notes FROM outcomes WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None, False
    return row, str(row[3] or "").startswith(tuple(NOTE_PREFIXES.values()))


def _write(c, run_id: str, verdict, durability: str, failure_class, notes: str, counts: Counter):
    """Write (or re-write) this module's outcome row for `run_id`; never another writer's row."""
    existing, owned = _owned(c, run_id)
    if existing is not None and not owned:
        counts["foreign_outcome"] += 1
        return
    if existing is not None and tuple(existing[:3]) == (verdict, durability, failure_class):
        counts["graded" if verdict else "excluded"] += 1
        return
    if existing is not None:  # the evidence changed: replace, including NULLs
        c.execute(
            "UPDATE outcomes SET adjudicated_verdict=?, durability=?, failure_class=?, "
            "failure_class_origin=?, notes=?, durability_checked_ts=? WHERE run_id=?",
            (
                verdict,
                durability,
                failure_class,
                "own" if failure_class else None,
                notes,
                int(time.time()),
                run_id,
            ),
        )
    else:
        feedback._record_outcome_in_conn(
            c,
            run_id,
            adjudicated_verdict=verdict,
            durability=durability,
            notes=notes,
            failure_class=failure_class,
        )
    counts["written"] += 1
    counts["graded" if verdict else "excluded"] += 1


def _grace_seconds() -> int:
    # ONE constant with the sweep: a `done` holds for as long as a merge must hold to be durable.
    from durability_sweep import GRACE_DAYS

    return int(GRACE_DAYS) * 86400


def acted_followups(c, state: Path | None = None) -> tuple[dict, Counter]:
    """{run_id: (follow-up issue, route)} for every closer `followup` decision the lane acted on.

    The closer writes the filed issue into the round item that filed it (`action` `followup`,
    `followup` the issue number, `route` when it is not `followup`). That item names the run when
    the lane recorded `judge.run_id`; before that, the run is the latest decision recorded for the
    same issue and PR at or before the round, and only when it is this `followup`, by the agent the
    item names. Anything else is `unattributed`, never guessed."""
    state = state or closer_state()
    counts: Counter = Counter()
    out: dict = {}
    for round_ts, _previous, item in _round_items(state, counts):
        judge = item["judge"]
        if item.get("action") != "followup" or judge.get("decision") != "followup":
            continue
        number, target, pr = item.get("followup"), str(item.get("target") or ""), item.get("pr")
        match = TARGET_RE.match(target)
        if not match or not str(number).isdigit() or not str(pr).isdigit():
            counts["followup_malformed"] += 1
            continue
        run_id = judge.get("run_id")
        if not run_id:
            # Every decision of the latest round that judged this issue and PR (a weak and a strong
            # judge can share one round); exactly one must be this agent's `followup`.
            rows = c.execute(
                "SELECT d.run_id, d.decision, r.agent FROM review_decisions d "
                "JOIN runs r ON r.run_id=d.run_id WHERE d.producer=? AND lower(d.subject_id)=? "
                "AND d.review_of=? AND d.judged_ts=(SELECT MAX(judged_ts) FROM review_decisions "
                "WHERE producer=? AND lower(subject_id)=? AND review_of=? AND judged_ts<=?)",
                (PRODUCER, target.lower(), f"{match['repo']}#{int(pr)}") * 2 + (round_ts,),
            ).fetchall()
            mine = [r for r in rows if r[2] == judge.get("provider")]
            if len(mine) != 1 or mine[0][1] != "followup":
                counts["followup_unattributed"] += 1
                continue
            run_id = mine[0][0]
        filed = (f"{match['repo']}#{int(number)}", str(item.get("route") or "followup"))
        if out.setdefault(run_id, filed) != filed:
            counts["followup_conflict"] += 1
            out[run_id] = None
    return {k: v for k, v in out.items() if v}, counts


_ISSUE_FIELDS = (
    "state stateReason closedAt "
    "timelineItems(itemTypes:[REOPENED_EVENT,CLOSED_EVENT],last:50){nodes{__typename "
    "... on ReopenedEvent{createdAt} ... on ClosedEvent{createdAt}}} "
    "closedByPullRequestsReferences(first:25,includeClosedPrs:true){pageInfo{hasNextPage} "
    "nodes{number merged repository{nameWithOwner}}}"
)


def _gh_graphql(query: str):
    import subprocess

    try:
        r = subprocess.run(
            ["gh", "api", "graphql", "-f", "query=" + query],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return json.loads(r.stdout).get("data") if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def issue_fates(issues, *, _gh=None) -> dict:
    """{`repo#n`: issue record, or None when GitHub did not answer} in batches of GH_BATCH."""
    gh = _gh or _gh_graphql
    keys = sorted(set(issues))
    out: dict = {}
    for i in range(0, len(keys), GH_BATCH):
        chunk = keys[i : i + GH_BATCH]
        parts = []
        for k, key in enumerate(chunk):
            repo, number = key.rsplit("#", 1)
            owner, name = repo.split("/", 1)
            parts.append(
                f'i{k}: repository(owner:"{owner}",name:"{name}"){{issue(number:{int(number)})'
                f"{{{_ISSUE_FIELDS}}}}}"
            )
        data = gh("query{" + " ".join(parts) + "}")
        for k, key in enumerate(chunk):
            node = (data or {}).get(f"i{k}") if isinstance(data, dict) else None
            out[key] = node.get("issue") if isinstance(node, dict) else None
    return out


def _gh_ts(value) -> int | None:
    return _round_ts(str(value or "").replace(".000Z", "Z"))


def grade_done(issue: dict | None, judged_ts: int, now: int, grace: int):
    """(verdict, durability, why) for an acted opener `done`, or (None, state, why) to wait."""
    if not issue:
        return None, "gh_unanswered", ""
    events = [
        (n.get("__typename"), _gh_ts(n.get("createdAt")))
        for n in issue["timelineItems"]["nodes"]
        if n
    ]
    # The judge's close is the last close at or before the round that recorded it (the round's
    # timestamp is written after the close); a reopen counts from that close on, never before it.
    closes = [ts for kind, ts in events if kind == "ClosedEvent" and ts is not None]
    anchor = max((ts for ts in closes if ts <= judged_ts), default=judged_ts)
    reopens = [
        ts for kind, ts in events if kind == "ReopenedEvent" and ts is not None and ts >= anchor
    ]
    if reopens or issue.get("state") == "OPEN":
        when = time.strftime("%Y-%m-%d", time.gmtime(min(reopens))) if reopens else "since"
        return (
            "FAIL",
            "reopened",
            f"the issue was reopened ({when}) after the judge closed it as done",
        )
    if now - judged_ts < grace:
        return None, "done_in_grace", ""
    return (
        "PASS",
        "durable",
        f"the issue stayed closed {grace // 86400} days after the judge closed it",
    )


def grade_followup(c, filed: str, issue: dict | None):
    """(verdict, durability, failure_class, why) for a filed follow-up, or (None, state, ...) to wait."""
    if not issue:
        return None, "gh_unanswered", None, ""
    if issue.get("state") != "CLOSED":
        return None, "followup_open", None, ""
    closed = time.strftime("%Y-%m-%d", time.gmtime(_gh_ts(issue.get("closedAt")) or 0))
    reason = str(issue.get("stateReason") or "")
    if reason in NOT_DONE_REASONS:
        return "FAIL", "abandoned", None, f"{filed} was closed {reason.lower()} on {closed}"
    closing = issue["closedByPullRequestsReferences"]
    if (closing.get("pageInfo") or {}).get("hasNextPage"):
        return None, "closing_prs_truncated", None, ""  # never grade from part of the list
    repo = filed.rsplit("#", 1)[0]
    prs = sorted(
        {
            # a PR in another repository can close the issue: keep the PR's own repository
            f"{(n.get('repository') or {}).get('nameWithOwner') or repo}#{int(n['number'])}"
            for n in closing["nodes"]
            if n and n["merged"]
        }
    )
    if not prs:
        return None, "closed_without_merge", None, ""
    verdicts = {pr: merge_verdict(c, filed, pr) for pr in prs}
    states = {state for state, _d in verdicts.values()}
    for waiting in ("conflict", "pending", "no_merge_row"):
        if waiting in states:
            return None, waiting, None, ""
    finals = {d for state, d in verdicts.values() if state == "final"}
    names = ", ".join(prs)
    if states == {"final"} and finals == {"durable"}:
        return "PASS", "durable", None, f"{filed} was closed by merged {names}, judged durable"
    if finals - {"durable"}:
        # The filed work merged and did not hold: that grades the implementer, not the claim that
        # the remainder was real work. No grade.
        return None, "followup_merge_failed", None, ""
    return None, "unjudgeable", feedback.UNJUDGEABLE_MERGE, f"the merge of {names} is unjudgeable"


def label(
    state: Path | None = None, *, filed: dict | None = None, now: int | None = None, _gh=None
) -> Counter:
    """Grade every recorded decision that has an outcome rule; idempotent.

    `filed` is `acted_followups`' map; without it, it is read from the closer's round records."""
    counts: Counter = Counter()
    now = int(time.time()) if now is None else int(now)
    with feedback._conn() as c:
        if filed is None:
            filed, more = acted_followups(c, state)
            counts.update({k: v for k, v in more.items() if k.startswith("followup_")})
        decisions = c.execute(
            "SELECT run_id, producer, subject_id, review_of, decision, judged_ts FROM review_decisions "
            "ORDER BY run_id"
        ).fetchall()
        done_rows: list = []
        followup_rows: list = []
        unmerged_starts: list = []
        for run_id, producer, subject_id, review_of, decision, judged_ts in decisions:
            if decision not in GRADED.get(producer, ()):
                counts[f"awaiting_rule:{decision}"] += 1
                continue
            if decision in MERGE_GRADED.get(producer, ()):
                if not review_of:
                    counts[f"awaiting_rule:{decision}"] += 1
                    continue
                merge_state, durability = merge_verdict(c, subject_id, review_of)
                if merge_state == "no_merge_row" and closed_unmerged(c, subject_id, review_of):
                    if producer == OPENER:
                        # The started PR never merged. The start claimed the issue was worth
                        # starting: graded by the issue's own fate, as a filed follow-up is.
                        unmerged_starts.append((run_id, subject_id, review_of))
                    else:
                        # A closer `close` on a PR that never merged: no rule (what `close` claims
                        # about a PR that did not merge is not one machine fact).
                        counts["pr_closed_unmerged"] += 1
                    continue
                if merge_state in {"pending", "no_merge_row", "conflict"}:
                    counts[merge_state] += 1
                    continue
                if merge_state == "final":
                    verdict = "PASS" if durability == "durable" else "FAIL"
                    failure_class = None
                else:
                    verdict, failure_class = None, feedback.UNJUDGEABLE_MERGE
                notes = (
                    f"{NOTE_PREFIXES[producer]}{decision} on {subject_id}; the merge of {review_of} "
                    f"was judged {durability} by the durability sweep"
                )
                _write(c, run_id, verdict, durability, failure_class, notes, counts)
                continue
            existing, owned = _owned(c, run_id)
            if owned and (existing[0] or existing[2]):  # final: the issue is not re-read
                counts["graded"] += 1
                continue
            if producer == OPENER:  # `done`: acted rows only (ingest_rounds keeps closed-done)
                if judged_ts is None:
                    counts["done_no_time"] += 1
                    continue
                done_rows.append((run_id, subject_id, int(judged_ts)))
            elif run_id not in filed:
                counts["followup_not_acted"] += 1  # judged, never filed (or filed unattributably)
            elif filed[run_id][1] != "followup":
                # A `[backlog]` issue is parked by design (no agent, nothing waits on it): its fate
                # says nothing about whether the remainder was real. No rule.
                counts["followup_backlog_route"] += 1
            else:
                followup_rows.append((run_id, subject_id, filed[run_id][0]))
        if not done_rows and not followup_rows and not unmerged_starts:
            return counts
        c.commit()  # end the write transaction: the GitHub read must not hold the Brain's lock
        fates = issue_fates(
            [s for _r, s, _t in done_rows]
            + [f for _r, _s, f in followup_rows]
            + [s for _r, s, _p in unmerged_starts],
            _gh=_gh,
        )
        for run_id, subject_id, review_of in unmerged_starts:
            verdict, durability, failure_class, why = grade_followup(
                c, subject_id, fates.get(subject_id)
            )
            if verdict is None and failure_class is None:
                counts[f"start_unmerged:{durability}"] += 1
                continue
            _write(
                c,
                run_id,
                verdict,
                durability,
                failure_class,
                f"{OPENER_NOTE_PREFIX}start on {subject_id}; the started {review_of} closed "
                f"unmerged; {why}",
                counts,
            )
        grace = _grace_seconds()
        for run_id, subject_id, judged_ts in done_rows:
            verdict, durability, why = grade_done(fates.get(subject_id), judged_ts, now, grace)
            if verdict is None:
                counts[durability] += 1
                continue
            _write(
                c,
                run_id,
                verdict,
                durability,
                None,
                f"{OPENER_NOTE_PREFIX}done on {subject_id}; {why}",
                counts,
            )
        for run_id, subject_id, issue in followup_rows:
            verdict, durability, failure_class, why = grade_followup(c, issue, fates.get(issue))
            if verdict is None and failure_class is None:
                counts[durability] += 1
                continue
            _write(
                c,
                run_id,
                verdict,
                durability,
                failure_class,
                f"{NOTE_PREFIX}followup on {subject_id}; {why}",
                counts,
            )
    return counts


def run(state: Path | None = None, opener: Path | None = None) -> dict:
    return {
        "ingest": dict(ingest(state)),
        "rounds": dict(ingest_rounds(PRODUCER, state)),
        "opener_rounds": dict(ingest_rounds(OPENER, opener)),
        "label": dict(label(state)),
    }


def restore() -> int:
    with feedback._conn() as c:
        return sum(
            c.execute("DELETE FROM outcomes WHERE notes LIKE ?", (prefix + "%",)).rowcount
            for prefix in NOTE_PREFIXES.values()
        )


def _db_ro():
    import sqlite3

    return sqlite3.connect(Path(feedback.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)


def _chain(c, producer: str, state: Path, since: int) -> tuple[dict, Counter]:
    chain: dict = defaultdict(Counter)
    prefix = NOTE_PREFIXES[producer]
    for agent, ts, decision, verdict, notes in c.execute(
        "SELECT r.agent, r.ts, d.decision, o.adjudicated_verdict, o.notes "
        "FROM runs r LEFT JOIN review_decisions d ON d.run_id=r.run_id AND d.producer=? "
        "LEFT JOIN outcomes o ON o.run_id=r.run_id "
        "WHERE r.task_type='review' AND r.target=?",
        (producer, _caller(state)),
    ):
        row = chain[agent]
        row["judge_runs"] += 1
        post = ts >= since
        row["post_deploy_runs"] += post
        if decision is None:
            continue
        row["decision_recorded"] += 1
        row["post_deploy_decisions"] += post
        row[f"decision:{decision}"] += 1
        if str(notes or "").startswith(prefix):
            if verdict:
                row["outcome"] += 1
                row["pass" if verdict == "PASS" else "fail"] += 1
                row[f"outcome:{decision}"] += 1
                row[f"{'pass' if verdict == 'PASS' else 'fail'}:{decision}"] += 1
                row["post_deploy_outcome"] += post
            else:
                row["excluded"] += 1
    totals: Counter = Counter()
    for row in chain.values():
        totals.update(row)
    return {a: dict(r) for a, r in sorted(chain.items())}, totals


def probe(since: int | None = None) -> dict:
    """The value chain per family, from the situation population (every lane-judge run).

    `since` (default `PASS_SINCE`, the first unit's deploy) bounds the pass number to closer-judge
    runs made after deploy; the chain counts are all-time."""
    with feedback._conn():  # ensure the table exists before a read-only open
        pass
    c = _db_ro()
    try:
        since = int(since if since is not None else PASS_SINCE)
        families, totals = _chain(c, PRODUCER, closer_state(), since)
        opener_families, opener_totals = _chain(c, OPENER, opener_state(), since)
        post = totals["post_deploy_outcome"]
        acted = acted_pass(c, int(time.time()))
        return {
            "acted_pass": acted,
            "since": since,
            "families": families,
            "totals": dict(totals),
            "opener": {"families": opener_families, "totals": dict(opener_totals)},
            "pass_number": {
                "measure": "graded closer-judge review outcomes on runs made after deploy",
                "value": post,
                "pass_at": PASS_OUTCOMES,
                "passed": post >= PASS_OUTCOMES,
            },
        }
    finally:
        c.close()


def _summary(name: str, graded: str, t: dict) -> str:
    return (
        f"review outcomes ({name}): judge runs {t.get('judge_runs', 0)}; decision recorded "
        f"{t.get('decision_recorded', 0)}; {graded} {t.get('decision:' + graded, 0)}; graded "
        f"{t.get('outcome', 0)} (PASS {t.get('pass', 0)}, FAIL {t.get('fail', 0)}); "
        f"excluded {t.get('excluded', 0)}"
    )


def _family_lines(families: dict, graded: str) -> list[str]:
    return [
        f"  {agent}: runs {row.get('judge_runs', 0)}, decisions {row.get('decision_recorded', 0)}, "
        f"{graded} {row.get('decision:' + graded, 0)}, graded {row.get('outcome', 0)} "
        f"(PASS {row.get('pass', 0)}, FAIL {row.get('fail', 0)})"
        for agent, row in families.items()
    ]


def format_probe(result: dict) -> str:
    t = result["totals"]
    lines = [_summary("closer judge", "close", t), *_family_lines(result["families"], "close")]
    opener = result.get("opener") or {"families": {}, "totals": {}}
    lines += [
        _summary("opener judge", "start", opener["totals"]),
        *_family_lines(opener["families"], "start"),
    ]
    for name, producer_totals in (("closer", t), ("opener", opener["totals"])):
        for decision in sorted(GRADED[PRODUCER if name == "closer" else OPENER]):
            lines.append(
                f"rule {name} {decision}: decisions {producer_totals.get('decision:' + decision, 0)}, "
                f"graded {producer_totals.get('outcome:' + decision, 0)} "
                f"(PASS {producer_totals.get('pass:' + decision, 0)}, "
                f"FAIL {producer_totals.get('fail:' + decision, 0)})"
            )
    pn = result["pass_number"]
    lines.append(
        f"post-deploy ingest coverage: decisions {t.get('post_deploy_decisions', 0)} of "
        f"{t.get('post_deploy_runs', 0)} judge runs (a run with no decision answered unusably, "
        "was one of two same-agent runs on its issue in one round, or is not in a round record yet)"
    )
    lines.append(
        f"post-deploy outcomes {pn['value']} / {pn['pass_at']} ({'PASS' if pn['passed'] else 'not yet'}; "
        f"runs since {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(result['since']))})"
    )
    acted = result.get("acted_pass")
    if acted:
        when = (
            "awaiting the first weekly relearn after grading began"
            if acted["since"] is None
            else "since " + time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(acted["since"]))
        )
        lines.append(
            f"judge picks from graded weights {acted['value']} / {acted['pass_at']} "
            f"({'PASS' if acted['passed'] else 'not yet'}; {when})"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--json", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("--since", type=int)
    p.add_argument("--json", action="store_true")
    sub.add_parser("restore")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        out = run()
        # The hourly tick ran the grader (capability ledger evidence; never raises).
        feedback._capability_daily_heartbeat("review-outcomes", "invocation", ref="tick")
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            lab = out["label"]
            i = Counter(out["ingest"]) + Counter(out["rounds"]) + Counter(out["opener_rounds"])
            awaiting_rule = sum(v for k, v in lab.items() if k.startswith("awaiting_rule:"))
            skipped = ", ".join(
                f"{k} {i[k]}"
                for k in (
                    "resolved_by_log",
                    "ambiguous",
                    "no_run",
                    "not_acted",
                    "no_pr_yet",
                    "rounds_missing",
                )
                if i.get(k)
            )
            unmerged_start = ", ".join(
                f"{k.split(':', 1)[1]} {v}"
                for k, v in sorted(lab.items())
                if k.startswith("start_unmerged:")
            )
            print(
                f"review_outcomes: ingested {i.get('recorded', 0)} new decisions ({i.get('known', 0)} known"
                f"{'; ' + skipped if skipped else ''}); "
                f"graded {lab.get('graded', 0)} (written {lab.get('written', 0)}), excluded {lab.get('excluded', 0)}; "
                f"awaiting the sweep {lab.get('pending', 0)}, drainable {lab.get('pending', 0)}; "
                f"no merged run row {lab.get('no_merge_row', 0)} (re-read every pass); "
                f"close on a PR closed unmerged {lab.get('pr_closed_unmerged', 0)} (final, no rule); "
                f"start on a PR closed unmerged, by the issue: {unmerged_start or 'none waiting'}; "
                f"conflict {lab.get('conflict', 0)}; no outcome rule {awaiting_rule}; "
                f"followup: open {lab.get('followup_open', 0)} (drains when the issue closes), "
                f"backlog route {lab.get('followup_backlog_route', 0)}, not acted "
                f"{lab.get('followup_not_acted', 0)}, closed without a merge "
                f"{lab.get('closed_without_merge', 0)}, merge failed {lab.get('followup_merge_failed', 0)}; "
                f"done: in grace {lab.get('done_in_grace', 0)} (drains at the sweep's grace); "
                f"GitHub unanswered {lab.get('gh_unanswered', 0)} (re-read every pass)"
            )
        return 0
    if args.cmd == "probe":
        res = probe(args.since)
        print(json.dumps(res, sort_keys=True) if args.json else format_probe(res))
        return 0
    print(f"review_outcomes: deleted {restore()} outcome rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
