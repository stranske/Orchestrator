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
3. `evidence`, `repair` and `followup` decisions have NO outcome rule yet. They are counted under
   `awaiting_rule`, never graded. That is 57% of decisions on the window above, so `close`
   precision is what the learners see for review until a rule for them lands.

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
separate the judge from the implementing agent. `park` and `done` have no rule yet.

The label is the durability sweep's, never a green check (CLAUDE.md §2). This module writes only
outcome rows it owns (notes start with `NOTE_PREFIX`) and never touches another writer's row.
Kill switch: `ORCH_DISABLE_STEPS=review-outcomes`. Rollback: `restore` deletes the outcome rows
this module wrote; `review_decisions` is additive.

CLI:
  python3 src/review_outcomes.py run [--json]       ingest + label (the tick's step)
  python3 src/review_outcomes.py probe [--since TS] the value chain and the pass number
  (closer runs after PASS_SINCE)
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
GRADED_DECISIONS = frozenset({"close"})
OPENER_DECISIONS = frozenset({"start", "park", "done"})
GRADED = {PRODUCER: GRADED_DECISIONS, OPENER: frozenset({"start"})}
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
                [i for i in items if isinstance(i, dict) and isinstance(i.get("judge"), dict)]
                if isinstance(items, list)
                else [],
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


def _opener_pr(state: Path, target: str) -> int | None:
    path = state / "targets" / (re.sub(r"[^a-z0-9]+", "_", target.lower()) + ".json")
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or str(row.get("target", "")).lower() != target.lower():
        return None
    pr = row.get("pr") or row.get("invite_pr")
    return int(pr) if isinstance(pr, int) or str(pr).isdigit() else None


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
                    for ts, rid in runs.get((subject.lower(), judge.get("provider")), [])
                    if low <= ts <= round_ts
                ]
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


def merge_verdict(c, subject_id: str, review_of: str) -> tuple[str, str]:
    """(state, durability) of the merge `review_of`, from the sweep's verdicts on its run rows.

    state: `final` (durability is a sweep verdict), `unjudgeable`, `pending`, `no_merge_row` or
    `conflict`."""
    repo, pr = review_of.rsplit("#", 1)
    issue = subject_id.rsplit("#", 1)[1]
    rows = c.execute(
        "SELECT DISTINCT o.durability FROM runs r JOIN outcomes o ON o.run_id=r.run_id "
        "WHERE o.merged=1 AND COALESCE(r.task_type,'')!='review' "
        "AND r.pr_number=? AND lower(r.target) IN (?,?)",
        (
            int(pr),
            f"{repo.lower()}#{pr}",
            f"{repo.lower()}#{issue}",
        ),
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


def label() -> Counter:
    """Grade every recorded decision that has an outcome rule; idempotent."""
    counts: Counter = Counter()
    with feedback._conn() as c:
        decisions = c.execute(
            "SELECT run_id, producer, subject_id, review_of, decision FROM review_decisions ORDER BY run_id"
        ).fetchall()
        for run_id, producer, subject_id, review_of, decision in decisions:
            if decision not in GRADED.get(producer, ()) or not review_of:
                counts[f"awaiting_rule:{decision}"] += 1
                continue
            state, durability = merge_verdict(c, subject_id, review_of)
            if state in {"pending", "no_merge_row", "conflict"}:
                counts[state] += 1
                continue
            if state == "final":
                verdict = "PASS" if durability == "durable" else "FAIL"
                failure_class = None
            else:
                verdict, failure_class = None, feedback.UNJUDGEABLE_MERGE
            existing, owned = _owned(c, run_id)
            if existing is not None and not owned:
                counts["foreign_outcome"] += 1
                continue
            if existing is not None and tuple(existing[:3]) == (verdict, durability, failure_class):
                counts["graded" if verdict else "excluded"] += 1
                continue
            notes = (
                f"{NOTE_PREFIXES[producer]}{decision} on {subject_id}; the merge of {review_of} was judged "
                f"{durability} by the durability sweep"
            )
            if existing is not None:  # the sweep re-judged the merge: replace, including NULLs
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
    return counts


def run(state: Path | None = None, opener: Path | None = None) -> dict:
    return {
        "ingest": dict(ingest(state)),
        "rounds": dict(ingest_rounds(PRODUCER, state)),
        "opener_rounds": dict(ingest_rounds(OPENER, opener)),
        "label": dict(label()),
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
        return {
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
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            lab = out["label"]
            i = Counter(out["ingest"]) + Counter(out["rounds"]) + Counter(out["opener_rounds"])
            awaiting_rule = sum(v for k, v in lab.items() if k.startswith("awaiting_rule:"))
            skipped = ", ".join(
                f"{k} {i[k]}"
                for k in ("ambiguous", "no_run", "not_acted", "no_pr_yet", "rounds_missing")
                if i.get(k)
            )
            print(
                f"review_outcomes: ingested {i.get('recorded', 0)} new decisions ({i.get('known', 0)} known"
                f"{'; ' + skipped if skipped else ''}); "
                f"graded {lab.get('graded', 0)} (written {lab.get('written', 0)}), excluded {lab.get('excluded', 0)}; "
                f"awaiting the sweep {lab.get('pending', 0)}, drainable {lab.get('pending', 0)}; "
                f"no merged run row {lab.get('no_merge_row', 0)} (re-read every pass); "
                f"conflict {lab.get('conflict', 0)}; no outcome rule {awaiting_rule}"
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
