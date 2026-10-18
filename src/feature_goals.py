"""Owner goals as a feedback surface: read, retain drift, report and reconcile issues.

Dedup: checked value_chain_monitor, firing_regressions, capability_targets and the
improvement log; none retains measurable owner goals. Reuse switch_review's
promotion reader and the existing fleet roster; this extends those feedback rails.
Missing evidence is never a measured zero. No dispatch or capability gate is changed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

GOAL_SILENCE_DAYS = 14
DAY = 86400
REPO = "stranske/Orchestrator"
EXCLUDED_REPOS = {"stranske/Collab-Admin"}


@dataclass(frozen=True)
class Measurement:
    value: float | None
    evidence_class: str
    as_of: int
    reason: str = ""
    # Only a source with a complete retrospective window may supply this.
    inactive_since: int | None = None


@dataclass(frozen=True)
class Goal:
    feature: str
    goal_id: str
    goal_text: str
    measure: str
    direction: str
    important: bool
    source: str


def _goal(feature: str, goal_id: str, text: str, symbol: str, direction="increase") -> Goal:
    return Goal(
        feature,
        goal_id,
        text,
        f"feature_goals.py:{symbol}",
        direction,
        True,
        "owner, 2026-10-17; Orchestrator#678",
    )


FEATURES = (
    _goal(
        "experiments", "G1", "multi-source feedback on sizable features", "multi_source_feedback"
    ),
    _goal("experiments", "G2", "comparative-advantage routing", "comparative_routing"),
    _goal("experiments", "G3", "random retest", "random_retest"),
    _goal("capacity-pacing", "G1", "end-of-period usage within pacing target band", "pacing_usage"),
    _goal(
        "capacity-pacing",
        "G2",
        "zero quota incidents while another pool is ok",
        "pacing_incidents",
        "zero",
    ),
    _goal(
        "capacity-pacing",
        "G3",
        "non-codex warn/shed dispatch share at least twice ok share",
        "pacing_share",
    ),
    _goal(
        "keepalive-acceptance",
        "G1",
        "share of completions citing a passing named acceptance test",
        "keepalive_acceptance",
    ),
    _goal(
        "sync-promotion",
        "G1",
        "days since last Maint 68 promote success",
        "sync_promotion",
        "decrease",
    ),
    _goal(
        "dedup-guard", "G1", "duplicate open PRs per issue over 72 hours", "duplicate_prs", "zero"
    ),
)


def unknown(now: int, query: str) -> Measurement:
    return Measurement(None, "unmeasured", now, query)


def _gh(args: list[str]) -> str:
    from switch_review import _gh_call

    ok, output, reason = _gh_call(args, timeout_s=90)
    if not ok:
        raise RuntimeError(reason)
    return output


def _brain() -> sqlite3.Connection:
    # Do not use feedback._conn(): it migrates/creates the live Brain on a read.
    import feedback

    return sqlite3.connect(f"{Path(feedback.DB_PATH).resolve().as_uri()}?mode=ro", uri=True)


def multi_source_feedback(now: int) -> Measurement:
    """Distinct scored judges per experiment, restricted to the named issue population."""
    with _brain() as conn:
        candidates = conn.execute(
            "SELECT e.experiment_id, r.target FROM evaluations_v2 e JOIN runs r "
            "ON r.experiment_id=e.experiment_id WHERE e.ts BETWEEN ? AND ? "
            "AND e.score IS NOT NULL AND e.evaluator_id IS NOT NULL "
            "GROUP BY e.experiment_id, r.target HAVING COUNT(DISTINCT e.evaluator_id)>=2",
            (now - GOAL_SILENCE_DAYS * DAY, now),
        ).fetchall()
    # Share exp_abcd's override/layout resolution; runs.target is a display string.
    from exp_abcd import exp_paths

    qualifying = set()
    unattributable = set()
    issue_failures = set()
    seen = set()
    for experiment, _target in candidates:
        if any(
            (_target or "").startswith(repo + suffix)
            for repo in EXCLUDED_REPOS
            for suffix in (" ", "#")
        ):
            continue
        if experiment in seen:
            continue
        seen.add(experiment)
        try:
            meta = json.loads((exp_paths(experiment) / "meta.json").read_text())
            subject = meta.get("subject") or {}
            # Producer experiments store the subject as the plain `owner/repo#N` string;
            # strategy/research experiments store a dict with `target`.
            targets = [
                subject if isinstance(subject, str) else None,
                subject.get("target") if isinstance(subject, dict) else None,
                meta.get("target"),
            ]
            match = None
            for target in targets:
                if isinstance(target, str):
                    match = re.search(r"(?<![\w./-])([\w.-]+/[\w.-]+)#(\d+)", target)
                    if match:
                        break
            if not match:
                raise ValueError("no issue target")
        except (OSError, ValueError, TypeError, AttributeError):
            unattributable.add(experiment)
            continue
        repo, number = match.groups()
        if repo in EXCLUDED_REPOS:
            continue
        try:
            issue = json.loads(_gh(["api", f"repos/{repo}/issues/{number}"]))
            body = issue.get("body") or ""
            labels = {label["name"] for label in issue["labels"]}
            if len(re.findall(r"(?m)^\s*[-*] \[[ xX]\]", body)) >= 9:
                qualifying.add(experiment)
            elif "experiment:forum" in labels:
                qualifying.add(experiment)
        except Exception:  # an unavailable/malformed issue cannot abort other experiments
            issue_failures.add(experiment)
    return Measurement(
        float(len(qualifying)),
        "Brain evaluations_v2 + experiment metadata + GitHub REST issues",
        now,
        reason=f"unattributable {len(unattributable)}; issue reads failed {len(issue_failures)}",
        inactive_since=(
            now - GOAL_SILENCE_DAYS * DAY if not qualifying and not issue_failures else None
        ),
    )


def comparative_routing(now: int) -> Measurement:
    """Task classes whose comparative top differs from the posterior top (#676 shadow report)."""
    try:
        import comparative
        import feedback

        result = comparative.report(feedback.resolve_db_path(), comparative.history_path(), now=now)
    except Exception as exc:  # an unreadable report is unmeasured, never zero
        return unknown(now, f"#676 comparative report unreadable: {type(exc).__name__}")
    status = result.get("status")
    if status == "not_testable_slack":
        # #676: capacity never bound in the window, so absolute routing was correct; not a zero.
        return unknown(
            now, "#676 slack: capacity never bound in the window; retest when a pool binds"
        )
    if status != "optimal" or result.get("classes_differing") is None:
        # A thin window can carry a count with status inputs_unmeasured; that is not a measurement.
        reasons = "; ".join(str(r) for r in result.get("reasons") or [])[:300]
        return unknown(now, f"#676 comparative report status {status}: {reasons}")
    return Measurement(
        float(result["classes_differing"]),
        "comparative shadow report (#676: pacing ledger + Brain posteriors)",
        now,
        reason=f"status {result.get('status')}; hosted consumer does not read comparative_rank yet",
    )


# The producer rewrites its state every active tick; older than this, the reading is stale.
PRODUCER_STATE_MAX_AGE = 2 * DAY


def random_retest(now: int) -> Measurement:
    """Share of route cells with a head-to-head inside the producer's coverage window."""
    try:
        import experiment_producer

        state = experiment_producer.read_state()
    except (OSError, ValueError) as exc:
        return unknown(now, f"experiment_producer state unreadable: {type(exc).__name__}")
    total, fresh, ts = state.get("cells_total"), state.get("cells_fresh"), state.get("last_draw_ts")
    if not total or fresh is None or not ts:
        return unknown(
            now, "experiment_producer state has no route cells (no active producer tick)"
        )
    if now - ts > PRODUCER_STATE_MAX_AGE:
        age_days = (now - ts) / DAY
        return unknown(now, f"producer not ticking: last draw {age_days:.1f} days ago")
    return Measurement(
        float(fresh) / float(total),
        "experiment_producer route cells (head-to-head within its coverage window)",
        int(ts),
        reason=f"{fresh}/{total} cells fresh",
    )


def _pacing(now: int, field: str) -> Measurement:
    import capacity

    reader = getattr(capacity, "pacing_report", None)
    if reader is None:
        return unknown(now, f"#668 capacity.py pacing-report {field} (source absent)")
    # Consume explicit evidence only; an absent/changed schema remains unknown.
    # Pass the ledger's clock: a reading stamped after `now` is rejected as invalid by measure().
    row = reader(now).get(field)
    if not isinstance(row, dict) or not row.get("evidence_class"):
        return unknown(now, f"#668 capacity.py pacing-report {field} with evidence class")
    if row.get("value") is None:
        return unknown(now, f"#668 pacing {field}: {row.get('reason') or 'unmeasured'}")
    return Measurement(
        float(row["value"]), row["evidence_class"], int(row["as_of"]), row.get("reason", "")
    )


def pacing_usage(now: int) -> Measurement:
    return _pacing(now, "usage_in_target_band")


def pacing_incidents(now: int) -> Measurement:
    return _pacing(now, "quota_incidents_while_other_ok")


def pacing_share(now: int) -> Measurement:
    return _pacing(now, "non_codex_share_at_least_double")


# The keepalive renders this line into the PR body on every verify round (Workflows#3985,
# keepalive_loop.js renderDeterministicGates); P/T counts named acceptance commands that passed.
DETERMINISTIC_GATES_RE = re.compile(r"(?m)^Deterministic gates: (\d+)/(\d+) passed")
# Workflows#3985 merged 2026-10-17T03:57Z: completions before it could not cite a gate.
KEEPALIVE_GATE_LIVE = "2026-10-17T03:57:07Z"
# Repos whose keepalive executes the gate. #3985: consumers "retain their current behavior until
# their execution step is delivered"; widen this when consumer delivery lands, so the measure
# grades agents where the gate exists instead of being pinned near zero by the rollout.
KEEPALIVE_GATE_REPOS = frozenset({"stranske/Workflows"})


def keepalive_acceptance(now: int) -> Measurement:
    """Share of keepalive PRs merged in the window whose body reports every named gate passing."""
    window = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - GOAL_SILENCE_DAYS * DAY))
    since = max(window, KEEPALIVE_GATE_LIVE)
    query = f"org:stranske is:pr is:merged merged:>={since} label:agents:keepalive"
    items: list[dict] = []
    for page in range(1, 11):
        payload = json.loads(
            _gh(
                [
                    "api",
                    "-X",
                    "GET",
                    "search/issues",
                    "-f",
                    f"q={query}",
                    "-f",
                    "per_page=100",
                    "-f",
                    f"page={page}",
                ]
            )
        )
        if payload.get("incomplete_results"):
            return unknown(now, "complete GitHub search for merged keepalive PRs (incomplete)")
        batch = payload.get("items") or []
        items += batch
        if len(batch) < 100:
            break
    else:
        return unknown(now, "merged keepalive PRs beyond 1000 search results")

    def repo_of(item: dict) -> str:
        return "/".join((item.get("repository_url") or "").split("/")[-2:])

    eligible = [item for item in items if repo_of(item) not in EXCLUDED_REPOS]
    population = [item for item in eligible if repo_of(item) in KEEPALIVE_GATE_REPOS]
    undelivered = len(eligible) - len(population)
    if not population:
        return unknown(
            now,
            f"merged keepalive PRs since {since} in gate repos (none; {undelivered} in repos "
            "without the gate)",
        )
    citing = 0
    for item in population:
        gates = DETERMINISTIC_GATES_RE.findall(item.get("body") or "")
        if gates:
            passed, total = (int(x) for x in gates[-1])
            citing += int(total > 0 and passed == total)
    return Measurement(
        citing / len(population),
        "GitHub REST search: merged keepalive PR bodies (Deterministic gates line)",
        now,
        reason=(
            f"{citing}/{len(population)} keepalive PRs merged since {since} in gate repos report "
            f"all named gates passing; {undelivered} merged in repos without the gate"
        ),
    )


def sync_promotion(now: int) -> Measurement:
    from switch_review import _gh_call, _promote_age_days

    row = _promote_age_days(now=now, gh_fn=_gh_call)
    value = row.get("days_since_success")
    if value is None:
        return unknown(now, "Maint 68 promote success: " + row.get("measurement", "missing source"))
    return Measurement(
        float(value),
        "GitHub Maint 68 successful promote",
        now,
        inactive_since=(
            int(datetime.fromisoformat(row["last_success_at"].replace("Z", "+00:00")).timestamp())
            if row.get("last_success_at") and float(value) >= GOAL_SILENCE_DAYS
            else None
        ),
    )


def duplicate_prs(now: int) -> Measurement:
    """Excess overlapping open PRs, where at least one opened in the last 72h.

    Read all open PRs so an older original plus a recent duplicate is counted.
    Every connection is checked for truncation; partial populations are unknown.
    """
    from issue_size_quality import fleet_repos

    repos = sorted(set(fleet_repos()) - EXCLUDED_REPOS)
    if not repos:
        return unknown(now, "non-empty fleet repository roster")
    per_issue: dict[str, list[int]] = defaultdict(list)
    for repo in repos:
        owner, name = repo.split("/")
        query = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){
          pullRequests(states:OPEN,first:100,after:$cursor){pageInfo{hasNextPage endCursor}
          nodes{number createdAt closingIssuesReferences(first:100){pageInfo{hasNextPage}
          nodes{number repository{nameWithOwner}}}}}}}"""
        cursor = None
        seen = set()
        while True:
            args = [
                "api",
                "graphql",
                "-f",
                "query=" + query,
                "-f",
                "owner=" + owner,
                "-f",
                "name=" + name,
            ]
            if cursor:
                args += ["-f", "cursor=" + cursor]
            payload = json.loads(_gh(args))
            if payload.get("errors"):
                raise ValueError(str(payload["errors"]))
            connection = payload["data"]["repository"]["pullRequests"]
            for pr in connection["nodes"]:
                refs = pr["closingIssuesReferences"]
                if refs["pageInfo"]["hasNextPage"]:
                    return unknown(
                        now, f"complete closingIssuesReferences for {repo}#{pr['number']}"
                    )
                created = int(
                    datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00")).timestamp()
                )
                for ref in refs["nodes"]:
                    target_repo = ref["repository"]["nameWithOwner"]
                    if target_repo not in EXCLUDED_REPOS:
                        per_issue[f"{target_repo}#{ref['number']}"].append(created)
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
            if not cursor or cursor in seen:
                return unknown(now, "complete paginated open PR population")
            seen.add(cursor)
    count = sum(
        len(rows) - 1 for rows in per_issue.values() if len(rows) > 1 and max(rows) >= now - 3 * DAY
    )
    return Measurement(float(count), "GitHub closingIssuesReferences, open overlaps over 72h", now)


def measure(goal: Goal, now: int) -> Measurement:
    try:
        module, symbol = goal.measure.removesuffix(".py").split(":")
        module = module.removesuffix(".py")
        result = getattr(importlib.import_module(module), symbol)(now)
        if result.value is not None and (
            not math.isfinite(result.value) or result.as_of > now or result.as_of < now - DAY
        ):
            return unknown(now, f"fresh finite {goal.measure} (invalid or stale measurement)")
        return result
    except Exception as exc:  # fail open at each source boundary, retaining the named query
        return unknown(now, f"{goal.measure}: {type(exc).__name__}: {exc}")


def _state(goal: Goal, measurement: dict, history: list[dict], now: int) -> str:
    value = measurement["value"]
    if value is None:
        return "unmeasured"
    if goal.direction == "zero":
        return "ok" if value == 0 else "wrong_direction"
    if goal.direction == "decrease":
        if value >= GOAL_SILENCE_DAYS:
            return "wrong_direction"
    elif value == 0:
        return "measured_zero"
    previous = [
        row
        for row in history
        if row["as_of"] <= now - GOAL_SILENCE_DAYS * DAY and row["measurement"]["value"] is not None
    ]
    if previous:
        old = previous[-1]["measurement"]["value"]
        if (goal.direction == "increase" and value < old) or (
            goal.direction == "decrease" and value > old
        ):
            return "wrong_direction"
    return "ok"


def drift(history: list[dict], now: int | None = None, *, goals: Sequence[Goal] = FEATURES) -> dict:
    now = int(time.time()) if now is None else now
    rows = []
    for goal in goals:
        observations = sorted(
            (
                r
                for r in history
                if r["feature"] == goal.feature
                and r["goal_id"] == goal.goal_id
                and r["as_of"] <= now
            ),
            key=lambda r: r["as_of"],
        )
        if not observations:
            observations = [{"as_of": now, "measurement": asdict(unknown(now, goal.measure))}]
        current = observations[-1]
        if current["as_of"] < now - DAY:
            current = {
                "as_of": now,
                "measurement": asdict(
                    unknown(now, f"fresh {goal.measure} (last observation stale)")
                ),
            }
            observations.append(current)
        state = _state(goal, current["measurement"], observations, now)
        since = now
        for index in range(len(observations) - 1, -1, -1):
            observation = observations[index]
            if (
                _state(goal, observation["measurement"], observations[:index], observation["as_of"])
                == "ok"
            ):
                break
            evidence_since = observation["measurement"].get("inactive_since")
            since = (
                min(observation["as_of"], evidence_since)
                if evidence_since is not None
                else observation["as_of"]
            )
        evidence_since = current["measurement"].get("inactive_since")
        if evidence_since is not None and evidence_since <= now:
            since = min(since, evidence_since)
        days = max(0, (now - since) / DAY) if state != "ok" else 0
        rows.append(
            {
                **asdict(goal),
                "measurement": current["measurement"],
                "state": state,
                "days": days,
                "drifting": goal.important and state != "ok" and days >= GOAL_SILENCE_DAYS,
            }
        )
    return {
        "as_of": now,
        "features": len({g.feature for g in goals}),
        "goals": rows,
        "drifting": len({r["feature"] for r in rows if r["drifting"]}),
        "horizon_days": GOAL_SILENCE_DAYS,
    }


HISTORY_PATH_ENV = "ORCH_FEATURE_GOALS_HISTORY"


def history_path() -> Path:
    """`$ORCH_FEATURE_GOALS_HISTORY` when set (tests), else `$ORCH_STATE_DIR/feature-goals-history.jsonl`."""
    override = os.environ.get(HISTORY_PATH_ENV)
    if override:
        return Path(override)
    return (
        Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
        / "feature-goals-history.jsonl"
    )


def _persist_report(
    now: int | None = None,
    *,
    path: Path | None = None,
    goals: Sequence[Goal] = FEATURES,
    measure_fn: Callable = measure,
) -> dict:
    now = int(time.time()) if now is None else now
    path = history_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    # One append/read transaction across tick and weekly reader, not a second event store.
    import fcntl

    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        history = []
        errors = []
        last_line = ""
        for number, line in enumerate(handle, 1):
            last_line = line
            try:
                row = json.loads(line)
                if not {"feature", "goal_id", "as_of", "measurement"} <= row.keys():
                    raise ValueError("incomplete history row")
                m = row["measurement"]
                if (
                    not isinstance(m, dict)
                    or not {"value", "as_of", "reason", "evidence_class"} <= m.keys()
                    or not isinstance(row["as_of"], (int, float))
                    or not math.isfinite(row["as_of"])
                    or (
                        m["value"] is not None
                        and (
                            not isinstance(m["value"], (int, float))
                            or not math.isfinite(m["value"])
                        )
                    )
                ):
                    raise ValueError("invalid history measurement")
                history.append(row)
            except (ValueError, TypeError, AttributeError) as exc:
                errors.append(f"history line {number}: {exc}")
        # A truncated final record must not consume the first fresh append too.
        if last_line and not last_line.endswith("\n"):
            handle.write("\n")
        for goal in goals:
            measurement = measure_fn(goal, now)
            row = {
                "feature": goal.feature,
                "goal_id": goal.goal_id,
                "as_of": now,
                "measurement": asdict(measurement),
            }
            history.append(row)
            handle.write(json.dumps(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    result = drift(history, now, goals=goals)
    result["errors"] = []
    result["warnings"] = errors
    result["history_writable"] = True
    return result


def report(
    now: int | None = None,
    *,
    path: Path | None = None,
    goals: Sequence[Goal] = FEATURES,
    measure_fn: Callable = measure,
) -> dict:
    now = int(time.time()) if now is None else now
    try:
        return _persist_report(now, path=path, goals=goals, measure_fn=measure_fn)
    except OSError as exc:
        # Weekly review must remain readable if its history store is unavailable.
        # No fabricated persistence or age: fail the machine step and name the loss.
        rows = [
            {
                "feature": g.feature,
                "goal_id": g.goal_id,
                "as_of": now,
                "measurement": asdict(unknown(now, f"{g.measure}; history unavailable: {exc}")),
            }
            for g in goals
        ]
        result = drift(rows, now, goals=goals)
        result["history_writable"] = False
        result["errors"] = [f"history unavailable: {exc}"]
        return result


def format_lines(section: dict) -> list[str]:
    lines = [
        "## Feature goals",
        "",
        f"feature goals: {section['features']} features, {section['drifting']} drifting",
        f"Drift horizon: {GOAL_SILENCE_DAYS} days",
        "",
        "| Feature / goal | Goal | Measure | Last value | Drift |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in section["goals"]:
        m = row["measurement"]
        value = (
            f"unmeasured — would need {m['reason']}"
            if row["state"] == "unmeasured"
            else "measured zero" if m["value"] == 0 else str(m["value"])
        )
        values = [
            f"{row['feature']}/{row['goal_id']}",
            row["goal_text"],
            row["measure"],
            f"{value} [{m['evidence_class']}; as_of {m['as_of']}]",
            f"{row['state']} for {row['days']:g} days",
        ]
        lines.append(
            "| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |"
        )
    warnings = section.get("warnings", [])
    if warnings:
        lines.append(
            f"History warning: skipped {len(warnings)} unparseable rows: " + "; ".join(warnings)
        )
    lines.extend(f"History error: {error}" for error in section.get("errors", []))
    return lines + [""]


def reconcile_issues(section: dict, *, gh: Callable = _gh) -> dict:
    """One issue per feature; deterministic goal marker follows the selected drifting goal.

    Read every state so a recurrence reopens the existing issue. Reconcile all markers
    for a feature on recovery. Unknown evidence can never close a drift issue.
    """
    # --paginate emits one JSON array per REST page. --slurp retains page boundaries.
    pages = json.loads(
        gh(
            [
                "api",
                f"repos/{REPO}/issues?labels=goal-drift&state=all&per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
    )
    issues = [issue for page in pages for issue in page if "pull_request" not in issue]
    if any(row["drifting"] for row in section["goals"]):
        for label, color in [("goal-drift", "B60205"), ("priority:high", "D93F0B")]:
            try:
                gh(["api", f"repos/{REPO}/labels/{label}"])
            except RuntimeError as exc:
                if "404" not in str(exc):
                    raise
                gh(
                    [
                        "api",
                        "-X",
                        "POST",
                        f"repos/{REPO}/labels",
                        "-f",
                        f"name={label}",
                        "-f",
                        f"color={color}",
                    ]
                )
    changes = []
    for feature in sorted({row["feature"] for row in section["goals"]}):
        rows = [r for r in section["goals"] if r["feature"] == feature]
        existing = [i for i in issues if f"<!-- goal-drift:{feature}:" in (i.get("body") or "")]
        bad = [r for r in rows if r["drifting"]]
        if not bad:
            if all(r["state"] == "ok" for r in rows):
                for issue in existing:
                    if issue["state"] == "open":
                        gh(
                            [
                                "api",
                                "-X",
                                "PATCH",
                                f"repos/{REPO}/issues/{issue['number']}",
                                "-f",
                                "state=closed",
                            ]
                        )
                        changes.append("closed")
            continue
        row = sorted(bad, key=lambda r: (-r["days"], r["goal_id"]))[0]
        title = f"[goal-drift] {feature}: {row['goal_text']} — {row['state']} for {int(row['days'])} days"
        markers = "\n".join(f"<!-- goal-drift:{feature}:{r['goal_id']} -->" for r in bad)
        body = (
            markers
            + "\n\n"
            + "\n".join(format_lines({**section, "goals": rows, "features": 1, "drifting": 1}))
        )
        body += "\nFYI only. Repair the measuring query or restore goal activity; no capability is blocked, paused or expired."
        if existing:
            issue = sorted(existing, key=lambda i: (i["state"] != "open", i["number"]))[0]
            # REST PATCH replaces labels; preserve labels added by other owners.
            labels = {"goal-drift", "priority:high"}
            labels.update(label["name"] for label in issue.get("labels", []))
            args = [
                "api",
                "-X",
                "PATCH",
                f"repos/{REPO}/issues/{issue['number']}",
                "-f",
                "state=open",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ]
            for label in sorted(labels):
                args.extend(["-f", f"labels[]={label}"])
            gh(args)
            changes.append("updated")
        else:
            gh(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{REPO}/issues",
                    "-f",
                    f"title={title}",
                    "-f",
                    f"body={body}",
                    "-f",
                    "labels[]=goal-drift",
                    "-f",
                    "labels[]=priority:high",
                ]
            )
            changes.append("created")
    return {"changes": changes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_argument_group("actions")
    group.add_argument("--report", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--file-drift-issues", action="store_true")
    args = parser.parse_args(argv)
    if not any((args.report, args.check, args.file_drift_issues)):
        parser.error("at least one action is required")
    section = report()
    print("\n".join(format_lines(section)))
    if not section.get("history_writable", True):
        return 1
    if args.file_drift_issues:
        print(json.dumps(reconcile_issues(section)))
    return 3 if args.check and section["drifting"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
