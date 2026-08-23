#!/usr/bin/env python3
"""Durable subject identity and acquisition controls for Orchestrator research.

The tables live in feedback._conn()'s SQLite database so subject selection is
joined to the existing Brain without inventing identities for legacy runs. This
module owns the additive schema while feedback.py is integrated independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import feedback

DEFAULT_COOLDOWN_HOURS = int(os.environ.get("ORCH_RESEARCH_SUBJECT_COOLDOWN_HOURS", "168"))
DEFAULT_UNEVALUATED_CAP = int(os.environ.get("ORCH_RESEARCH_UNEVALUATED_CAP", "25"))
DEFAULT_PER_SUBJECT_CAP = int(os.environ.get("ORCH_RESEARCH_PER_SUBJECT_CAP", "1"))
# THE EVALUATION WINDOW, DEFINED ONCE AND CONSUMED TWICE. `exp_abcd.followup` defaults its
# `max_age_days` to this value, and `unevaluated_experiment_ids` bounds its count by it, so the
# window the cap MEASURES and the window the drain can REACH are the same number by construction.
#
# They were not, and that deadlocked the whole research arm for five weeks. The cap counted every
# experiment lacking an `evaluations` row over ALL TIME (a raw Brain query), while `followup` could
# only ever pick up experiments younger than 14 days that still had their on-disk artifacts. So an
# experiment that aged out, or whose directory was reclaimed, counted against the cap FOREVER with
# no path to leave it. Measured 2026-08-21: 128 unevaluated against a cap of 25, **0 of them within
# 30 days** (range 50.9–67.5 days old) and **0 with an on-disk directory**, so the drain was 0 and
# the block was permanent. The research arm therefore planned nothing from ~2026-07-15 onward —
# which is exactly when objective anchors stopped (last anchor 2026-07-15 21:44) and why
# `human_calibration` has taken no new row since.
#
# Seventh instance of this project's signature bug: a gate whose clear path is blocked by the very
# thing it measures. The fix is NOT to weaken the cap — a genuinely pending experiment still counts,
# and that is what the cap is for.
EVALUABLE_WINDOW_DAYS = int(os.environ.get("ORCH_RESEARCH_EVALUABLE_WINDOW_DAYS", "14"))
OPEN_LIFECYCLES = {"planned", "active", "evaluable"}
LIFECYCLES = OPEN_LIFECYCLES | {"evaluated", "failed", "skipped"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_subjects (
  subject_id TEXT PRIMARY KEY,
  subject_family_id TEXT NOT NULL,
  canonical_target TEXT NOT NULL,
  task_type TEXT NOT NULL,
  spec_hash TEXT NOT NULL,
  base_sha TEXT,
  arm_set_hash TEXT NOT NULL,
  arms_json TEXT NOT NULL,
  profiles_json TEXT,
  lifecycle TEXT NOT NULL,
  exp_id TEXT,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  cooldown_until INTEGER,
  evaluable_ts INTEGER,
  evaluated_ts INTEGER,
  last_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_subjects_family_lifecycle
  ON research_subjects(subject_family_id, lifecycle, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_research_subjects_target
  ON research_subjects(canonical_target, task_type);
CREATE TABLE IF NOT EXISTS research_subject_experiments (
  exp_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  subject_family_id TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  cooldown_until INTEGER,
  last_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_subject_experiments_subject
  ON research_subject_experiments(subject_id, lifecycle, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_research_subject_experiments_family
  ON research_subject_experiments(subject_family_id, lifecycle);
CREATE TABLE IF NOT EXISTS research_subject_events (
  event_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  subject_id TEXT,
  subject_family_id TEXT,
  canonical_target TEXT,
  task_type TEXT,
  decision TEXT NOT NULL,
  reason TEXT,
  exp_id TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_subject_events_decision_ts
  ON research_subject_events(decision, ts);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def canonical_target(target: str) -> str:
    text = str(target or "").strip()
    if "#" not in text:
        return text.lower()
    repo, number = text.rsplit("#", 1)
    return f"{repo.strip().lower()}#{number.strip()}"


def normalize_spec(spec: str | None) -> str:
    lines = []
    for line in str(spec or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = re.sub(r"[ \t]+", " ", line.strip())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def subject_identity(
    target: str,
    task_type: str,
    spec: str | None,
    base_sha: str | None,
    arms: list[str] | tuple[str, ...],
    profiles: dict | list | None = None,
) -> dict:
    return subject_identity_from_hash(
        target,
        task_type,
        _hash(normalize_spec(spec)),
        base_sha,
        arms,
        profiles,
    )


def subject_identity_from_hash(
    target: str,
    task_type: str,
    spec_hash: str,
    base_sha: str | None,
    arms: list[str] | tuple[str, ...],
    profiles: dict | list | None = None,
) -> dict:
    """Canonical identity when safe event producers retain only a spec hash."""
    target_key = canonical_target(target)
    task_key = str(task_type or "implement").strip().lower()
    spec_hash = str(spec_hash or "").strip().lower()
    if spec_hash.startswith("sha256:"):
        spec_hash = spec_hash.split(":", 1)[1]
    if not re.fullmatch(r"[a-f0-9]{64}", spec_hash):
        raise ValueError("spec_hash must be a SHA-256 hex digest")
    base_key = str(base_sha or "unknown").strip().lower() or "unknown"
    arm_set = sorted({str(arm).strip().lower() for arm in arms if str(arm).strip()})
    profile_value = profiles or {}
    profile_json = json.dumps(profile_value, sort_keys=True, separators=(",", ":"))
    arm_payload = json.dumps(
        {"arms": arm_set, "profiles": profile_value},
        sort_keys=True,
        separators=(",", ":"),
    )
    arm_set_hash = _hash(arm_payload)
    family_payload = "|".join((target_key, task_key, spec_hash, base_key))
    family_id = f"subject-family:{_hash(family_payload)[:24]}"
    subject_payload = f"{family_payload}|{arm_set_hash}"
    return {
        "subject_id": f"subject:{_hash(subject_payload)[:24]}",
        "subject_family_id": family_id,
        "canonical_target": target_key,
        "task_type": task_key,
        "spec_hash": spec_hash,
        "base_sha": None if base_key == "unknown" else base_key,
        "arm_set_hash": arm_set_hash,
        "arms": arm_set,
        "arms_json": json.dumps(arm_set, separators=(",", ":")),
        "profiles_json": profile_json if profile_value else None,
    }


def completion_observation_id(
    subject_id: str | None, run_id: str, canonical_attempt_id: str | None
) -> str:
    """Stable across phase events; changes when subject/run/worker attempt changes."""
    return "sha256:" + _hash(f"{subject_id}|{run_id}|{canonical_attempt_id or 'unresolved'}")


def unevaluated_experiment_ids(
    conn: sqlite3.Connection,
    *,
    window_days: int | None = None,
    now: float | None = None,
) -> set[str]:
    """Experiments still PENDING evaluation — lacking evaluation rows AND still reachable.

    Bounded by `EVALUABLE_WINDOW_DAYS` (see the constant for the deadlock this fixes): an
    experiment older than the window can never be picked up by `exp_abcd.followup`, so counting it
    as pending measures something unreachable and latches the cap shut.

    FAILS SAFE toward the cap: an experiment whose newest run carries NO timestamp has unknown age
    and is COUNTED. Unknown age must not silently unblock the arm — the conservative direction here
    is to keep the cap honest, not to widen it.
    """
    window = EVALUABLE_WINDOW_DAYS if window_days is None else int(window_days)
    cutoff = (time.time() if now is None else float(now)) - max(0, window) * 86400.0
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT r.experiment_id, MAX(r.ts) FROM runs r "
                "WHERE r.experiment_id IS NOT NULL AND r.experiment_id<>'' "
                "AND NOT EXISTS (SELECT 1 FROM evaluations e "
                "WHERE e.experiment_id=r.experiment_id) "
                "GROUP BY r.experiment_id"
            ).fetchall()
            if row[1] is None or float(row[1]) >= cutoff
        }
    except (sqlite3.Error, TypeError, ValueError):
        return set()


DOMAIN_PREFIX = "domain/"


def domain_target(slug: str) -> str:
    """Canonical target for research that is not about a repository. Pure; selftested.

    Every research producer derives its target from a repo/issue, so research the owner actually
    runs most often -- a topic study, a tool comparison, a technique to learn -- has no target
    shape and therefore cannot be registered as a subject at all. `domain/<slug>` gives it one.

    The payoff is retrieval before learning: a registered domain subject makes prior research
    addressable by a later session, which is what "you have access to the research project showing
    how to use Luminar, don't you?" was asking for and not getting.
    """
    text = re.sub(r"[^a-z0-9]+", "-", str(slug or "").strip().lower()).strip("-")
    if not text:
        raise ValueError("domain research slug must contain at least one alphanumeric character")
    return f"{DOMAIN_PREFIX}{text}"


def is_domain_target(target: str) -> bool:
    """True when a canonical target names domain research rather than a repository."""
    return str(target or "").strip().lower().startswith(DOMAIN_PREFIX)


def record_domain_research(
    slug: str,
    spec: str,
    arms: list[str] | tuple[str, ...],
    *,
    exp_id: str,
    task_type: str = "research",
    profiles: dict | list | None = None,
    lifecycle: str = "active",
    reason: str = "domain_research",
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Register a non-repo research project as a subject and return its identity.

    `spec` is the real scope of the study -- the question asked, the rubric, the brief -- and is
    hashed, never stored raw. `arms` are the agents that actually did the work; a single-agent
    study is one arm and must not be padded to look comparative.

    Deliberately NO score or outcome is written here. There is no un-gameable success label for
    "was this research any good", and inventing one would corrupt the learner far more than the
    missing record does. Capture and retrieval only.
    """
    identity = subject_identity(domain_target(slug), task_type, spec, None, arms, profiles)
    record_subject(identity, lifecycle=lifecycle, exp_id=exp_id, reason=reason, conn=conn)
    return identity


def research_round_id(area: str, kind: str, date: str) -> str:
    """Stable id for one multi-agent research round, e.g. `stranske/Workflows:audit:2026-08-16`.

    Mirrors the shape UX-review panels already use, because audits have the same structure: one
    scope, several agents working it in parallel. The round id is what binds those agents into ONE
    subject with a real arm set -- without it each offloaded agent is an unrelated run against an
    ephemeral temp path, which is why thousands of offload runs across six agents produced nothing
    the learner could compare.
    """
    area = str(area or "").strip().lower()
    kind = re.sub(r"[^a-z0-9]+", "-", str(kind or "").strip().lower()).strip("-")
    date = str(date or "").strip()
    if not area or not kind or not date:
        raise ValueError("research round needs area, kind and date")
    return f"{area}:{kind}:{date}"


def record_research_round(
    area: str,
    kind: str,
    date: str,
    spec: str,
    arms: list[str] | tuple[str, ...],
    *,
    task_type: str | None = None,
    base_sha: str | None = None,
    profiles: dict | list | None = None,
    lifecycle: str = "active",
    conn: sqlite3.Connection | None = None,
) -> tuple[str, dict]:
    """Register a multi-agent research round as ONE subject. Returns (round_id, identity).

    `arms` must be the agents that actually did the work. An audit round fanned out to four agents
    is four arms and is comparable evidence; a round done by one agent is one arm and must not be
    padded, because a forged arm set manufactures independence the evidence does not have.
    """
    arm_list = [str(a).strip() for a in arms if str(a).strip()]
    if not arm_list:
        raise ValueError("a research round needs at least one arm")
    round_id = research_round_id(area, kind, date)
    identity = subject_identity(area, task_type or kind, spec, base_sha, arm_list, profiles)
    record_subject(
        identity, lifecycle=lifecycle, exp_id=round_id, reason=f"{kind}_round", conn=conn
    )
    return round_id, identity


def round_identity(round_id: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The subject identity a round is registered under, or None when it is unregistered.

    One reader, three callers (the two finding recorders and the CLI). The join was written out by
    hand in `main()` and again in `feedback._record_completion_identity`; a third hand-rolled copy
    is how two seams end up disagreeing about which subject a round belongs to.
    """
    db = conn or feedback._conn()
    close = conn is None
    try:
        ensure_schema(db)
        row = db.execute(
            "SELECT s.subject_id, s.subject_family_id, s.canonical_target, s.task_type "
            "FROM research_subject_experiments x "
            "JOIN research_subjects s ON s.subject_id=x.subject_id WHERE x.exp_id=?",
            (round_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "subject_id": row[0],
            "subject_family_id": row[1],
            "canonical_target": row[2],
            "task_type": row[3],
        }
    finally:
        if close:
            db.close()


def round_arm_evidence(round_id: str, *, conn: sqlite3.Connection | None = None) -> dict:
    """The arms a round can HONESTLY claim, split by which record admits each one.

    Two independent facts, and neither of them is a caller's declaration:

    * `registered` -- the arm set stored on the round's subject by `record_subject`.
    * `attempted`  -- the agents that actually have a run bound to this round by `experiment_id`,
      which is what `dispatcher.offload(research_round=...)` writes.

    ONE READER FOR THE MEMBERSHIP RULE, because the rule already exists at a second seam:
    `completion_event_adapter` rejects an envelope whose `arm_id` falls outside its subject's arm
    set (`selected_arm_not_in_subject_set`). A second hand-rolled copy of that test is how two
    seams drift into disagreeing about who was in a round -- and an arm set is exactly the kind of
    thing that must not be judged differently in two places, because the learner treats each arm
    as independent evidence.
    """
    db = conn or feedback._conn()
    close = conn is None
    try:
        ensure_schema(db)
        registered: list[str] = []
        if _table_exists(db, "research_subjects") and _table_exists(
            db, "research_subject_experiments"
        ):
            row = db.execute(
                "SELECT s.arms_json FROM research_subject_experiments x "
                "JOIN research_subjects s ON s.subject_id=x.subject_id WHERE x.exp_id=?",
                (round_id,),
            ).fetchone()
            if row is not None:
                try:
                    registered = sorted(
                        {
                            str(arm).strip().lower()
                            for arm in json.loads(row[0] or "[]")
                            if str(arm).strip()
                        }
                    )
                except (TypeError, ValueError):
                    registered = []
        attempted: list[str] = []
        if _table_exists(db, "runs"):
            attempted = sorted(
                {
                    str(agent).strip().lower()
                    for (agent,) in db.execute(
                        "SELECT DISTINCT agent FROM runs WHERE experiment_id=?", (round_id,)
                    ).fetchall()
                    if str(agent or "").strip()
                }
            )
        return {
            "round_id": round_id,
            "registered": registered,
            "attempted": attempted,
            "arms": sorted(set(registered) | set(attempted)),
        }
    finally:
        if close:
            db.close()


def _validated_issue_run(conn: sqlite3.Connection, canonical_issue: str, run_id: str) -> str:
    """A run may be bound to an issue only if the Brain says that run targeted that issue.

    The binding is the causal claim, so it is the thing that has to be checked. Accepting an
    unverified run id would replace one inference (newest run on the target) with a worse one (a
    caller's assertion about a run nobody looked up).
    """
    rid = str(run_id or "").strip()
    if not rid:
        raise ValueError("an implementation run id cannot be empty")
    row = conn.execute("SELECT LOWER(target) FROM runs WHERE run_id=?", (rid,)).fetchone()
    if row is None:
        raise ValueError(f"unknown implementation run {rid!r}")
    recorded = str(row[0] or "")
    if recorded != canonical_issue:
        raise ValueError(
            f"run {rid!r} targeted {recorded!r}, not {canonical_issue!r}; "
            "a binding may not re-target a finding"
        )
    return rid


FINDING_FILED = "finding_filed"
FINDING_IMPLEMENTED = "finding_implemented"


def record_finding_issue(
    round_id: str,
    issue_target: str,
    *,
    arm: str,
    identity: dict,
    finding_ref: str | None = None,
    implementation_run_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Record that one arm's audit finding became a real issue. THE linkage B was missing.

    Everything downstream of here already exists: an issue becomes a PR, a merged PR gets a
    durability label from `durability_sweep`, and `influence_edges` already back-propagates that
    label over accepted edges. What did not exist was the fact "round R, arm A produced issue N" --
    and it cannot be inferred, because the only written trace is a free-form prose line
    (`_Surfaced by the maint-69 outage investigated in #3007._`). Parsing that into a causal edge
    would attribute work to an agent on the strength of a sentence, and a wrong attribution trains
    the learner on a fiction. So the filer records it as a fact at filing time.

    WHY THIS LABEL IS UN-GAMEABLE, which is the whole reason B is worth doing: the agent that found
    the defect decides neither of the two things that score it. Whether the finding is filed is
    decided by the filer's prove-before-file check, and whether the fix HOLDS is decided months
    later by real work landing on top of it. An audit finding that was wrong gets falsified by
    the codebase itself -- a better terminal signal than production delivery has.

    THE ARM IS VALIDATED, NOT TRUSTED. `arm` decides which agent inherits the issue's durability
    and which run becomes the source of an accepted influence edge, so an unchecked caller-supplied
    value could attribute one agent's finding to another -- and the learner would then train on the
    fiction, which is the one failure this linkage exists to avoid. Membership is read from
    `round_arm_evidence`: the round's registered arm set, or a run actually bound to the round.
    Both are records; neither is a claim made at filing time.

    `implementation_run_id` is optional and almost always unknown here -- an issue is filed BEFORE
    anything implements it. Use `record_finding_implementation` when the delivering run exists.
    """
    db = conn or feedback._conn()
    close = conn is None
    try:
        ensure_schema(db)
        arm_key = str(arm or "").strip().lower()
        if not arm_key:
            raise ValueError("a filed finding needs the arm that produced it")
        evidence = round_arm_evidence(round_id, conn=db)
        if not evidence["arms"]:
            # FAIL LOUD, NOT SILENT: with no arm set there is nothing to validate against, and
            # accepting the filing would record an attribution no record can support.
            raise ValueError(
                f"round {round_id!r} has no registered or attempted arm; register the round "
                "before filing its findings"
            )
        if arm_key not in evidence["arms"]:
            raise ValueError(
                f"arm_not_in_subject_set: {arm_key!r} is not an arm of round {round_id!r} "
                f"(registered={evidence['registered']}, attempted={evidence['attempted']})"
            )
        metadata = {
            "issue_target": canonical_target(issue_target),
            "arm": arm_key,
            "finding_ref": finding_ref,
        }
        if implementation_run_id:
            metadata["implementation_run_id"] = _validated_issue_run(
                db, metadata["issue_target"], implementation_run_id
            )
        return record_event(
            FINDING_FILED,
            identity=identity,
            reason=f"{arm_key}_finding_filed",
            exp_id=round_id,
            metadata=metadata,
            conn=db,
        )
    finally:
        if close:
            db.close()


def record_finding_implementation(
    round_id: str,
    issue_target: str,
    implementation_run_id: str,
    *,
    identity: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Bind a filed finding to THE run that implemented it. The causal half of the linkage.

    `record_finding_issue` can only be called when the issue is filed, which is necessarily BEFORE
    any run exists to implement it -- so the delivering run cannot be persisted at filing time, and
    `resolve_round_durability` used to guess it with `ORDER BY r.ts DESC LIMIT 1` over every run
    that had ever targeted the issue. That is not a causal association: a later review, audit or
    unrelated run on the same target silently REPLACED the implementation outcome and rewrote both
    `per_arm_durability` and the accepted influence edge, so an arm's measured durability depended
    on whatever touched its issue most recently.

    This is the drain for the `ambiguous_outcome_runs` count `resolve_round_durability` reports. It
    records a statement -- this run delivered that issue -- and validates it against the Brain: the
    run must exist and must have targeted that issue. The ARM IS INHERITED from the filing rather
    than accepted as an argument, so a binding can never re-attribute a finding to another agent.

    Same event log, one more `decision`. A second table for "which run delivered this" would be the
    parallel store the learning-loop rules forbid.
    """
    db = conn or feedback._conn()
    close = conn is None
    try:
        ensure_schema(db)
        issue_key = canonical_target(issue_target)
        filed_arm: str | None = None
        for (blob,) in db.execute(
            "SELECT metadata_json FROM research_subject_events "
            "WHERE exp_id=? AND decision=? ORDER BY ts",
            (round_id, FINDING_FILED),
        ).fetchall():
            try:
                meta = json.loads(blob or "{}")
            except (TypeError, ValueError):
                continue
            if str(meta.get("issue_target") or "") == issue_key:
                filed_arm = str(meta.get("arm") or "").strip().lower() or None
        if filed_arm is None:
            raise ValueError(
                f"no filed finding for {issue_key!r} in round {round_id!r}; "
                "record the filing before binding its implementation"
            )
        run_id = _validated_issue_run(db, issue_key, implementation_run_id)
        return record_event(
            FINDING_IMPLEMENTED,
            identity=identity or round_identity(round_id, conn=db) or {},
            reason=f"{filed_arm}_finding_implemented",
            exp_id=round_id,
            metadata={
                "issue_target": issue_key,
                "arm": filed_arm,
                "implementation_run_id": run_id,
            },
            conn=db,
        )
    finally:
        if close:
            db.close()


def resolve_round_durability(
    round_id: str, *, conn: sqlite3.Connection | None = None, apply_edges: bool = False
) -> dict:
    """Inherit the downstream durability of the issues a round's findings produced.

    Reads only recorded facts: the `finding_filed` events for this round, the `finding_implemented`
    binding that names each issue's delivering run, and the durability that run's outcome already
    carries. It computes nothing about quality itself.

    WITH NO BINDING IT RESOLVES ONLY AN UNAMBIGUOUS TARGET -- exactly one run with an outcome. Two
    or more and the finding stays unresolved as `ambiguous_outcome_runs`, because picking among them
    is a guess: whichever heuristic chose (newest, first, "looks like an implement") would let a
    later review or audit run on the same issue rewrite the arm's durability. An honest gap beats a
    plausible mis-attribution, and the gap has a named drain.

    `apply_edges` writes the linkage into `influence_edges` as an `experiment` edge from the arm's
    round run to the implementing run, so the EXISTING propagation carries the label rather than a
    second durability path growing beside it. Off by default: reading is safe, writing is a change.

    Reports resolved AND unresolved counts together. "3 durable" alone would read as a verdict on
    the round when 40 findings are still unlanded; the pair is the honest statement.
    """
    db = conn or feedback._conn()
    close = conn is None
    try:
        ensure_schema(db)
        rows = db.execute(
            "SELECT metadata_json FROM research_subject_events "
            "WHERE exp_id=? AND decision=? ORDER BY ts",
            (round_id, FINDING_FILED),
        ).fetchall()
        filed: list[dict] = []
        bound: dict[str, str] = {}
        for (blob,) in rows:
            try:
                meta = json.loads(blob or "{}")
            except (TypeError, ValueError):
                continue
            if meta.get("issue_target"):
                filed.append(meta)
                # A filing that already knew its delivering run carries it; almost never the case,
                # since an issue is filed before anything implements it.
                if meta.get("implementation_run_id"):
                    bound[str(meta["issue_target"])] = str(meta["implementation_run_id"])
        # EXPLICIT BINDINGS WIN. A `finding_implemented` event is a recorded, Brain-validated
        # statement that one run delivered one issue. The latest statement per issue is the one in
        # force, so a correction can replace a mistake; that is a declaration being superseded, not
        # a causal fact being inferred from run order.
        for (blob,) in db.execute(
            "SELECT metadata_json FROM research_subject_events "
            "WHERE exp_id=? AND decision=? ORDER BY ts",
            (round_id, FINDING_IMPLEMENTED),
        ).fetchall():
            try:
                meta = json.loads(blob or "{}")
            except (TypeError, ValueError):
                continue
            if meta.get("issue_target") and meta.get("implementation_run_id"):
                bound[str(meta["issue_target"])] = str(meta["implementation_run_id"])
        # The round's own arm runs, bound by `experiment_id` when the round was dispatched.
        arm_runs: dict[str, str] = {}
        for run_id, agent in db.execute(
            "SELECT run_id, agent FROM runs WHERE experiment_id=?", (round_id,)
        ).fetchall():
            arm_runs.setdefault(str(agent or "").lower(), str(run_id))
        per_arm: dict[str, dict[str, int]] = {}
        unresolved = 0
        unresolved_reasons: Counter = Counter()
        edges_written = 0
        for meta in filed:
            arm = str(meta.get("arm") or "").lower()
            target = meta["issue_target"]
            # LOWER() on both sides: `canonical_target` lowercases the recorded issue while
            # `runs.target` keeps the repo's real casing, so a literal compare silently found
            # nothing and every finding looked unlanded.
            #
            # NO `ORDER BY r.ts DESC LIMIT 1`, WHICH IS THE WHOLE POINT. Taking the newest outcome
            # run on a target is not a causal association -- a later review, audit or unrelated run
            # on the same issue would silently replace the implementation outcome and rewrite this
            # arm's durability. So: an explicit binding if one was recorded, otherwise exactly one
            # candidate, otherwise UNRESOLVED. Ambiguity is reported, never guessed away.
            bound_run = bound.get(target)
            if bound_run:
                candidates = db.execute(
                    "SELECT r.run_id, COALESCE(o.durability,'pending') FROM runs r "
                    "JOIN outcomes o ON o.run_id=r.run_id WHERE r.run_id=?",
                    (bound_run,),
                ).fetchall()
                if not candidates:
                    # The delivering run is named but has not been scored yet. Pending, not absent.
                    unresolved += 1
                    unresolved_reasons["bound_run_has_no_outcome"] += 1
                    continue
            else:
                candidates = db.execute(
                    "SELECT r.run_id, COALESCE(o.durability,'pending') FROM runs r "
                    "JOIN outcomes o ON o.run_id=r.run_id WHERE LOWER(r.target)=? "
                    "ORDER BY r.run_id",
                    (target,),
                ).fetchall()
                if not candidates:
                    unresolved += 1
                    unresolved_reasons["no_outcome_run"] += 1
                    continue
                if len(candidates) > 1:
                    unresolved += 1
                    unresolved_reasons["ambiguous_outcome_runs"] += 1
                    continue
            target_run, durability = str(candidates[0][0]), str(candidates[0][1])
            bucket = per_arm.setdefault(arm, {})
            bucket[durability] = bucket.get(durability, 0) + 1
            if apply_edges and arm in arm_runs:
                try:
                    feedback.record_influence_edge(
                        target_run_id=target_run,
                        influence_type="experiment",
                        influence_id=round_id,
                        accepted=True,
                        source_run_id=arm_runs[arm],
                        allow_unlinked=True,
                        metadata={"audit_round": round_id, "arm": arm, "issue": target},
                    )
                    edges_written += 1
                except Exception:  # noqa: BLE001 - reported via the counts, never fatal
                    pass
        return {
            "round_id": round_id,
            "findings_filed": len(filed),
            "per_arm_durability": per_arm,
            # BLOCKING AND DRAINABLE IN ONE PLACE: a resolved count without its unresolved twin
            # reads as a verdict on the round when most findings simply have not landed yet.
            "resolved": len(filed) - unresolved,
            "unresolved": unresolved,
            "unresolved_by_reason": dict(sorted(unresolved_reasons.items())),
            # THE BLOCKING QUANTITY AND ITS DRAIN IN THE SAME PLACE. `unresolved: 12` reads as "be
            # patient" whatever the cause; the split says which. `no_outcome_run` drains when the
            # work lands and `bound_run_has_no_outcome` when the run is scored -- both happen on
            # their own. `ambiguous_outcome_runs` drains through NOTHING but an explicit binding,
            # so it is the one number that can sit still forever, and it names its own command.
            "drainable_by_binding": int(unresolved_reasons.get("ambiguous_outcome_runs", 0)),
            "drain": (
                "research_subjects.py finding-implemented --round-id <round> "
                "--issue <owner/repo#N> --run-id <run>"
            ),
            "arms_with_runs": sorted(arm_runs),
            "edges_written": edges_written,
        }
    finally:
        if close:
            db.close()


def _effective_lifecycle(conn: sqlite3.Connection, row: tuple) -> str:
    lifecycle, exp_id = str(row[0]), row[1]
    if exp_id:
        evaluated = conn.execute(
            "SELECT 1 FROM evaluations WHERE experiment_id=? LIMIT 1", (exp_id,)
        ).fetchone()
        if evaluated:
            return "evaluated"
    return lifecycle


def prior_experiment_count(identity: dict, *, conn: sqlite3.Connection | None = None) -> int:
    """Independent subject-selection history, separate from quality outcomes."""
    db = conn or feedback._conn()
    close = conn is None
    if not _table_exists(db, "research_subject_experiments"):
        if close:
            db.close()
        return 0
    if identity.get("base_sha") is None:
        count = db.execute(
            "SELECT COUNT(*) FROM research_subject_experiments x "
            "JOIN research_subjects s ON s.subject_id=x.subject_id "
            "WHERE s.canonical_target=? AND s.task_type=? AND s.spec_hash=?",
            (
                identity["canonical_target"],
                identity["task_type"],
                identity["spec_hash"],
            ),
        ).fetchone()[0]
    else:
        count = db.execute(
            "SELECT COUNT(*) FROM research_subject_experiments WHERE subject_family_id=?",
            (identity["subject_family_id"],),
        ).fetchone()[0]
    if close:
        db.close()
    return int(count or 0)


def assess_candidate(
    *,
    target: str,
    task_type: str,
    spec: str | None,
    base_sha: str | None,
    arms: list[str] | tuple[str, ...],
    profiles: dict | list | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
    unevaluated_cap: int = DEFAULT_UNEVALUATED_CAP,
    per_subject_cap: int = DEFAULT_PER_SUBJECT_CAP,
) -> dict:
    """Read-only admission decision with explicit, machine-readable blockers."""
    identity = subject_identity(target, task_type, spec, base_sha, arms, profiles)
    db = conn or feedback._conn()
    close = conn is None
    now = int(now or time.time())
    try:
        # `now` is threaded through deliberately: this function already takes an injected clock, and
        # a window computed from the REAL clock while the rest of the decision uses the injected one
        # is two clocks disagreeing — the same shape as the two windows this whole fix is about.
        backlog_ids = unevaluated_experiment_ids(db, now=now)
        backlog_count = len(backlog_ids)
        # THE GATE MUST REPORT ITS OWN DRAIN, not just its blocker. `backlog_count` is now the
        # REACHABLE count (bounded by EVALUABLE_WINDOW_DAYS); `backlog_total` is every unevaluated
        # experiment including the ones past the drain's reach. Reporting one number is what let
        # this gate sit at "128 of 25" for five weeks reading as ordinary backpressure — the pair
        # `reachable 0 / total 128` names the deadlock on sight. See ~/.claude/skills/latched-gate-check.
        backlog_total = len(unevaluated_experiment_ids(db, window_days=10**9, now=now))
        if max(0, int(unevaluated_cap)) <= backlog_count:
            return {
                **identity,
                "eligible": False,
                "reason": "unevaluated_backlog_cap",
                "unevaluated_backlog": backlog_count,
                "unevaluated_backlog_total": backlog_total,
                "unevaluated_cap": max(0, int(unevaluated_cap)),
            }
        if not _table_exists(db, "research_subjects"):
            return {
                **identity,
                "eligible": True,
                "reason": "admitted",
                "unevaluated_backlog": backlog_count,
                "unevaluated_backlog_total": backlog_total,
                "unevaluated_cap": max(0, int(unevaluated_cap)),
            }
        if identity.get("base_sha") is None:
            exact = db.execute(
                "SELECT x.lifecycle,x.exp_id,x.cooldown_until FROM research_subject_experiments x "
                "JOIN research_subjects s ON s.subject_id=x.subject_id "
                "WHERE s.canonical_target=? AND s.task_type=? AND s.spec_hash=? "
                "AND s.arm_set_hash=? ORDER BY x.updated_ts DESC LIMIT 1",
                (
                    identity["canonical_target"],
                    identity["task_type"],
                    identity["spec_hash"],
                    identity["arm_set_hash"],
                ),
            ).fetchone()
        else:
            exact = db.execute(
                "SELECT lifecycle,exp_id,cooldown_until FROM research_subject_experiments "
                "WHERE subject_id=? ORDER BY updated_ts DESC LIMIT 1",
                (identity["subject_id"],),
            ).fetchone()
        if exact:
            lifecycle = _effective_lifecycle(db, exact[:2])
            if lifecycle in OPEN_LIFECYCLES:
                return {
                    **identity,
                    "eligible": False,
                    "reason": f"subject_{lifecycle}",
                    "existing_exp_id": exact[1],
                    "unevaluated_backlog": backlog_count,
                    "unevaluated_backlog_total": backlog_total,
                    "unevaluated_cap": max(0, int(unevaluated_cap)),
                }
            if int(exact[2] or 0) > now:
                return {
                    **identity,
                    "eligible": False,
                    "reason": "subject_cooldown",
                    "cooldown_until": int(exact[2]),
                    "existing_exp_id": exact[1],
                    "unevaluated_backlog": backlog_count,
                    "unevaluated_backlog_total": backlog_total,
                    "unevaluated_cap": max(0, int(unevaluated_cap)),
                }
        family_open = 0
        if identity.get("base_sha") is None:
            family_rows = db.execute(
                "SELECT x.lifecycle,x.exp_id FROM research_subject_experiments x "
                "JOIN research_subjects s ON s.subject_id=x.subject_id "
                "WHERE s.canonical_target=? AND s.task_type=? AND s.spec_hash=?",
                (
                    identity["canonical_target"],
                    identity["task_type"],
                    identity["spec_hash"],
                ),
            ).fetchall()
        else:
            family_rows = db.execute(
                "SELECT lifecycle,exp_id FROM research_subject_experiments "
                "WHERE subject_family_id=?",
                (identity["subject_family_id"],),
            ).fetchall()
        for lifecycle, exp_id in family_rows:
            if _effective_lifecycle(db, (lifecycle, exp_id)) in OPEN_LIFECYCLES:
                family_open += 1
        if family_open >= max(1, int(per_subject_cap)):
            return {
                **identity,
                "eligible": False,
                "reason": "subject_backlog_cap",
                "subject_open_backlog": family_open,
                "per_subject_cap": max(1, int(per_subject_cap)),
                "unevaluated_backlog": backlog_count,
                "unevaluated_backlog_total": backlog_total,
                "unevaluated_cap": max(0, int(unevaluated_cap)),
            }
        return {
            **identity,
            "eligible": True,
            "reason": "admitted",
            "unevaluated_backlog": backlog_count,
            "unevaluated_backlog_total": backlog_total,
            "unevaluated_cap": max(0, int(unevaluated_cap)),
        }
    finally:
        if close:
            db.close()


def record_event(
    decision: str,
    *,
    identity: dict | None = None,
    target: str | None = None,
    task_type: str | None = None,
    reason: str | None = None,
    exp_id: str | None = None,
    metadata: dict | None = None,
    conn: sqlite3.Connection | None = None,
    ts: int | None = None,
) -> str:
    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    ts = int(ts or time.time())
    identity = identity or {}
    target_key = identity.get("canonical_target") or canonical_target(target or "")
    task_key = identity.get("task_type") or str(task_type or "implement")
    raw = "|".join(
        str(value or "")
        for value in (
            ts,
            decision,
            identity.get("subject_id"),
            target_key,
            task_key,
            reason,
            exp_id,
            json.dumps(metadata or {}, sort_keys=True),
        )
    )
    event_id = f"subject-event:{_hash(raw)[:24]}"
    db.execute(
        "INSERT OR REPLACE INTO research_subject_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            ts,
            identity.get("subject_id"),
            identity.get("subject_family_id"),
            target_key or None,
            task_key or None,
            str(decision),
            reason,
            exp_id,
            json.dumps(metadata, sort_keys=True) if metadata else None,
        ),
    )
    db.commit()
    if close:
        db.close()
    return event_id


def record_subject(
    identity: dict,
    *,
    lifecycle: str,
    exp_id: str,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
    reason: str | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
) -> None:
    lifecycle = str(lifecycle)
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"invalid research subject lifecycle: {lifecycle}")
    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    now = int(now or time.time())
    cooldown_until = now + max(0, int(cooldown_hours)) * 3600
    existing = db.execute(
        "SELECT created_ts FROM research_subjects WHERE subject_id=?",
        (identity["subject_id"],),
    ).fetchone()
    db.execute(
        "INSERT OR REPLACE INTO research_subjects "
        "(subject_id,subject_family_id,canonical_target,task_type,spec_hash,base_sha,"
        "arm_set_hash,arms_json,profiles_json,lifecycle,exp_id,created_ts,updated_ts,"
        "cooldown_until,evaluable_ts,evaluated_ts,last_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity["subject_id"],
            identity["subject_family_id"],
            identity["canonical_target"],
            identity["task_type"],
            identity["spec_hash"],
            identity.get("base_sha"),
            identity["arm_set_hash"],
            identity["arms_json"],
            identity.get("profiles_json"),
            lifecycle,
            exp_id,
            int(existing[0]) if existing else now,
            now,
            cooldown_until,
            now if lifecycle == "evaluable" else None,
            now if lifecycle == "evaluated" else None,
            reason,
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO research_subject_experiments "
        "(exp_id,subject_id,subject_family_id,lifecycle,created_ts,updated_ts,"
        "cooldown_until,last_reason) VALUES (?,?,?,?,?,?,?,?)",
        (
            exp_id,
            identity["subject_id"],
            identity["subject_family_id"],
            lifecycle,
            now,
            now,
            cooldown_until,
            reason,
        ),
    )
    record_event(
        "launched" if lifecycle in OPEN_LIFECYCLES else lifecycle,
        identity=identity,
        reason=reason,
        exp_id=exp_id,
        conn=db,
        ts=now,
    )
    db.commit()
    if close:
        db.close()


def mark_lifecycle(
    exp_id: str,
    lifecycle: str,
    *,
    reason: str | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
) -> bool:
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"invalid research subject lifecycle: {lifecycle}")
    db = conn or feedback._conn()
    close = conn is None
    if not _table_exists(db, "research_subject_experiments"):
        if close:
            db.close()
        return False
    now = int(now or time.time())
    db.execute(
        "UPDATE research_subjects SET lifecycle=?,updated_ts=?,last_reason=?,"
        "evaluable_ts=CASE WHEN ?='evaluable' THEN ? ELSE evaluable_ts END,"
        "evaluated_ts=CASE WHEN ?='evaluated' THEN ? ELSE evaluated_ts END "
        "WHERE exp_id=?",
        (lifecycle, now, reason, lifecycle, now, lifecycle, now, exp_id),
    )
    db.execute(
        "UPDATE research_subject_experiments SET lifecycle=?,updated_ts=?,last_reason=? "
        "WHERE exp_id=?",
        (lifecycle, now, reason, exp_id),
    )
    changed = db.execute("SELECT changes()").fetchone()[0] > 0
    if changed:
        row = db.execute(
            "SELECT s.subject_id,s.subject_family_id,s.canonical_target,s.task_type "
            "FROM research_subject_experiments x JOIN research_subjects s "
            "ON s.subject_id=x.subject_id WHERE x.exp_id=?",
            (exp_id,),
        ).fetchone()
        identity = {
            "subject_id": row[0],
            "subject_family_id": row[1],
            "canonical_target": row[2],
            "task_type": row[3],
        }
        record_event(lifecycle, identity=identity, reason=reason, exp_id=exp_id, conn=db, ts=now)
    db.commit()
    if close:
        db.close()
    return changed


def effective_evidence_weights(
    *, conn: sqlite3.Connection | None = None, task_type: str | None = None
) -> dict[str, float]:
    """Return weights for explicitly linked research runs; legacy rows are omitted."""
    db = conn or feedback._conn()
    close = conn is None
    if not _table_exists(db, "research_subjects"):
        if close:
            db.close()
        return {}
    query = (
        "SELECT r.run_id,r.agent,x.subject_family_id FROM runs r "
        "JOIN research_subject_experiments x ON x.exp_id=r.experiment_id "
        "WHERE r.experiment_id IS NOT NULL"
    )
    params: list = []
    if task_type is not None:
        query += " AND r.task_type=?"
        params.append(task_type)
    rows = db.execute(query, params).fetchall()
    by_agent_subject: dict[tuple[str, str], list[str]] = defaultdict(list)
    for run_id, agent, family_id in rows:
        by_agent_subject[(str(agent or "unknown"), str(family_id))].append(str(run_id))
    weights = {
        run_id: 1.0 / len(run_ids) for run_ids in by_agent_subject.values() for run_id in run_ids
    }
    if close:
        db.close()
    return weights


def summary(
    *, conn: sqlite3.Connection | None = None, window_days: int = 90, now: int | None = None
) -> dict:
    db = conn or feedback._conn()
    close = conn is None
    now = int(now or time.time())
    since = now - max(1, int(window_days)) * 86400
    unevaluated_ids = unevaluated_experiment_ids(db)
    if not _table_exists(db, "research_subjects"):
        if close:
            db.close()
        return {
            "window_days": window_days,
            "registered_subjects": 0,
            "independent_subjects": 0,
            "unevaluated_backlog": len(unevaluated_ids),
            "unevaluated_cap": DEFAULT_UNEVALUATED_CAP,
            "unevaluated_backlog_cap_reached": len(unevaluated_ids) >= DEFAULT_UNEVALUATED_CAP,
            "lifecycle_counts": {},
            "true_task_type_distribution": {},
            "duplicate_rejections": 0,
            "rejections_by_reason": {},
            "research_production_collisions": 0,
            "effective_sample_count": 0.0,
            "registered_run_count": 0,
        }
    rows = db.execute("SELECT subject_family_id,task_type FROM research_subjects").fetchall()
    experiment_rows = db.execute(
        "SELECT subject_family_id,lifecycle,exp_id FROM research_subject_experiments"
    ).fetchall()
    lifecycle_counts: Counter = Counter()
    task_counts: Counter = Counter()
    families = set()
    for family_id, task_type in rows:
        families.add(str(family_id))
        task_counts[str(task_type)] += 1
    for _family_id, lifecycle, exp_id in experiment_rows:
        lifecycle_counts[_effective_lifecycle(db, (lifecycle, exp_id))] += 1
    event_rows = db.execute(
        "SELECT decision,COALESCE(reason,'') FROM research_subject_events WHERE ts>=?",
        (since,),
    ).fetchall()
    rejection_reasons = Counter(reason for decision, reason in event_rows if decision == "rejected")
    duplicate_reasons = {
        "duplicate_candidate_in_plan",
        "subject_active",
        "subject_evaluable",
        "subject_planned",
        "subject_cooldown",
        "subject_backlog_cap",
    }
    weights = effective_evidence_weights(conn=db)
    result = {
        "window_days": window_days,
        "registered_subjects": len(rows),
        "registered_experiments": len(experiment_rows),
        "independent_subjects": len(families),
        "unevaluated_backlog": len(unevaluated_ids),
        "unevaluated_cap": DEFAULT_UNEVALUATED_CAP,
        "unevaluated_backlog_cap_reached": len(unevaluated_ids) >= DEFAULT_UNEVALUATED_CAP,
        "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
        "true_task_type_distribution": dict(sorted(task_counts.items())),
        "duplicate_rejections": sum(
            count for reason, count in rejection_reasons.items() if reason in duplicate_reasons
        ),
        "rejections_by_reason": dict(sorted(rejection_reasons.items())),
        "research_production_collisions": sum(
            1
            for decision, reason in event_rows
            if decision == "rejected" and reason == "production_reserved"
        ),
        "effective_sample_count": round(sum(weights.values()), 6),
        "registered_run_count": len(weights),
    }
    if close:
        db.close()
    return result


def _selftest() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(feedback.SCHEMA)
    feedback._migrate_schema(conn)
    ensure_schema(conn)
    now = 2_000_000_000
    one = subject_identity("Owner/Repo#1", "testgen", "A  spec\n", "ABC", ["codex", "cursor"])
    same = subject_identity("owner/repo#1", "testgen", "A spec", "abc", ["cursor", "codex"])
    assert one["subject_id"] == same["subject_id"], (one, same)
    first = assess_candidate(
        target="Owner/Repo#1",
        task_type="testgen",
        spec="A spec",
        base_sha="abc",
        arms=["cursor", "codex"],
        conn=conn,
        now=now,
        unevaluated_cap=99,
    )
    assert first["eligible"] and first["reason"] == "admitted", first

    # THE UNEVALUATED CAP MUST COUNT ONLY WHAT THE DRAIN CAN REACH. Regression for the deadlock
    # measured 2026-08-21: 128 unevaluated experiments (all 50.9-67.5 days old, none with on-disk
    # artifacts) held the cap of 25 shut permanently, because `followup` can only pick up
    # experiments inside EVALUABLE_WINDOW_DAYS. The cap counted over all time, so the number could
    # never fall and the research arm planned nothing for five weeks.
    cap_now = now
    conn.execute(
        "INSERT INTO runs (run_id,ts,agent,task_type,target,experiment_id) VALUES (?,?,?,?,?,?)",
        ("run-fresh", cap_now - 2 * 86400, "codex", "testgen", "Owner/Repo#9", "exp-fresh"),
    )
    conn.execute(
        "INSERT INTO runs (run_id,ts,agent,task_type,target,experiment_id) VALUES (?,?,?,?,?,?)",
        ("run-stale", cap_now - 60 * 86400, "codex", "testgen", "Owner/Repo#8", "exp-stale"),
    )
    conn.execute(
        "INSERT INTO runs (run_id,ts,agent,task_type,target,experiment_id) VALUES (?,?,?,?,?,?)",
        ("run-nots", None, "codex", "testgen", "Owner/Repo#7", "exp-nots"),
    )
    pending = unevaluated_experiment_ids(conn, now=cap_now)
    # The 2-day-old one is genuinely pending and MUST still count — the cap is not being weakened.
    assert "exp-fresh" in pending, pending
    # The 60-day-old one is past the drain's reach; counting it is what latched the gate.
    assert "exp-stale" not in pending, pending
    # FAIL SAFE: unknown age counts, so a missing timestamp can never silently unblock the arm.
    assert "exp-nots" in pending, pending
    # THE GATE MUST NAME ITS OWN DRAIN. Reporting only the blocker is what made a five-week
    # deadlock read as ordinary backpressure; the pair reachable-vs-total names it on sight.
    verdict = assess_candidate(
        target="Owner/Repo#42",
        task_type="testgen",
        spec="s",
        base_sha="f0",
        arms=["codex", "cursor"],
        conn=conn,
        now=cap_now,
        unevaluated_cap=99,
    )
    assert verdict["unevaluated_backlog_total"] >= verdict["unevaluated_backlog"], verdict
    assert verdict["unevaluated_backlog_total"] >= 3, verdict  # includes the 60-day-old one
    assert verdict["unevaluated_backlog"] == 2, verdict  # reachable: fresh + unknown-age only
    # ...and an evaluated experiment leaves the count by the original path, unchanged.
    conn.execute(
        "INSERT INTO evaluations (experiment_id,implementer,evaluator,score,ts) VALUES (?,?,?,?,?)",
        ("exp-fresh", "codex", "cursor", 7.0, cap_now),
    )
    assert "exp-fresh" not in unevaluated_experiment_ids(conn, now=cap_now), "evaluated must clear"
    # THE TWO WINDOWS ARE ONE NUMBER BY CONSTRUCTION, not by comment: followup defaults to it.
    import inspect as _inspect

    import exp_abcd as _exp_abcd

    assert (
        _inspect.signature(_exp_abcd.followup).parameters["max_age_days"].default
        == EVALUABLE_WINDOW_DAYS
    ), "followup window drifted from the cap window"
    record_subject(one, lifecycle="active", exp_id="exp-one", conn=conn, now=now)
    second = assess_candidate(
        target="owner/repo#1",
        task_type="testgen",
        spec="A spec",
        base_sha="abc",
        arms=["codex", "cursor"],
        conn=conn,
        now=now + 1,
        unevaluated_cap=99,
    )
    assert not second["eligible"] and second["reason"] == "subject_active", second

    identities = [one]
    for index in (2, 3):
        identity = subject_identity(
            f"owner/repo#{index}", "testgen", f"spec {index}", "abc", ["codex"]
        )
        identities.append(identity)
        record_subject(identity, lifecycle="evaluated", exp_id=f"exp-{index}", conn=conn, now=now)
    for index in range(20):
        conn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"corr-{index}", now, "o/r#1", "testgen", "codex", "exp-one", "experimental"),
        )
    for index in (2, 3):
        conn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                f"ind-{index}",
                now,
                f"o/r#{index}",
                "testgen",
                "codex",
                f"exp-{index}",
                "experimental",
            ),
        )
        conn.execute(
            "INSERT INTO evaluations (experiment_id,implementer,evaluator,score,rank,verdict,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"exp-{index}", "codex", "judge", 8.0, 1, None, now),
        )
    weights = effective_evidence_weights(conn=conn, task_type="testgen")
    assert len(weights) == 22 and abs(sum(weights.values()) - 3.0) < 1e-9, weights
    # A legacy experiment remains usable elsewhere but receives no invented subject mapping.
    conn.execute(
        "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
        "VALUES ('legacy',?,?,?,?,?,?)",
        (now, "o/r#legacy", "testgen", "codex", "legacy-exp", "experimental"),
    )
    assert "legacy" not in effective_evidence_weights(conn=conn)
    report = summary(conn=conn, now=now)
    assert report["effective_sample_count"] == 3.0, report
    assert report["registered_run_count"] == 22, report
    conn.close()

    # --- domain research namespace (line C): non-repo research must be registrable ---
    assert domain_target("  Luminar Editing!! ") == "domain/luminar-editing"
    assert domain_target("SBA Portfolio") == "domain/sba-portfolio"
    for bad in ("", "   ", "!!!", None):
        try:
            domain_target(bad)
        except ValueError:
            pass
        else:  # a blank slug must not become the target "domain/"
            raise AssertionError(f"blank slug accepted: {bad!r}")
    assert is_domain_target("domain/luminar-editing")
    assert not is_domain_target("stranske/Ready#1")
    dconn = sqlite3.connect(":memory:")
    ensure_schema(dconn)
    dident = record_domain_research(
        "SBA Portfolio",
        "history + portfolio construction",
        ["codex", "claude"],
        exp_id="domain:sba-2026-08-21",
        conn=dconn,
    )
    assert dident["canonical_target"] == "domain/sba-portfolio", dident
    assert dident["arms"] == ["claude", "codex"], dident
    drow = dconn.execute(
        "SELECT s.canonical_target, s.task_type, x.exp_id FROM research_subject_experiments x "
        "JOIN research_subjects s ON s.subject_id = x.subject_id"
    ).fetchone()
    assert drow == ("domain/sba-portfolio", "research", "domain:sba-2026-08-21"), drow
    # A one-agent study is ONE arm; padding it would forge comparative evidence.
    solo = record_domain_research(
        "Luminar Editing",
        "curves tool",
        ["claude"],
        exp_id="domain:luminar-1",
        conn=dconn,
    )
    assert solo["arms"] == ["claude"], solo
    assert solo["subject_id"] != dident["subject_id"], "distinct topics must be distinct subjects"
    dconn.close()

    # --- multi-agent research rounds (line B): audits fan out to several agents ---
    assert (
        research_round_id("stranske/Workflows", "Audit", "2026-08-16")
        == "stranske/workflows:audit:2026-08-16"
    )
    for bad in (("", "audit", "2026-01-01"), ("a", "", "2026-01-01"), ("a", "audit", "")):
        try:
            research_round_id(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"incomplete round id accepted: {bad}")
    rconn = sqlite3.connect(":memory:")
    ensure_schema(rconn)
    rid, rident = record_research_round(
        "stranske/Workflows",
        "audit",
        "2026-08-16",
        "8 audit categories",
        ["codex", "gemini", "cursor", "vibe"],
        conn=rconn,
    )
    assert rid == "stranske/workflows:audit:2026-08-16", rid
    # Every arm retained: an audit fanned out to four agents is FOUR arms of comparable evidence.
    assert rident["arms"] == ["codex", "cursor", "gemini", "vibe"], rident
    assert rconn.execute(
        "SELECT x.exp_id FROM research_subject_experiments x JOIN research_subjects s "
        "ON s.subject_id = x.subject_id WHERE s.canonical_target='stranske/workflows'"
    ).fetchone() == (rid,), "round must join exp_id -> subject"
    # A solo round is ONE arm and must not be padded into false independence.
    _, solo_round = record_research_round(
        "local/Reader",
        "audit",
        "2026-08-09",
        "scope",
        ["claude"],
        conn=rconn,
    )
    assert solo_round["arms"] == ["claude"], solo_round
    for empty in ([], ["", "  "]):
        try:
            record_research_round("a/b", "audit", "2026-01-01", "s", empty, conn=rconn)
        except ValueError:
            pass
        else:
            raise AssertionError("a round with no real arm was accepted")
    rconn.close()

    # --- FINDING LINKAGE: the arm is VALIDATED, and durability is never inferred from run order.
    fconn = sqlite3.connect(":memory:")
    fconn.executescript(feedback.SCHEMA)
    ensure_schema(fconn)
    frid, frident = record_research_round(
        "stranske/Findings", "audit", "2026-08-23", "scope", ["codex", "cursor"], conn=fconn
    )
    # A second round, so "a real agent that is an arm SOMEWHERE" is a case the test actually has.
    record_research_round("stranske/Other", "audit", "2026-08-23", "s", ["gemini"], conn=fconn)
    record_finding_issue(frid, "stranske/Findings#1", arm="codex", identity=frident, conn=fconn)
    # AN UNKNOWN ARM AND A REAL AGENT FROM ANOTHER ROUND ARE BOTH REFUSED. Either would let one
    # agent's finding train the learner as another agent's work, and the accepted influence edge
    # would then come from a run that produced nothing.
    for bogus in ("nobody", "gemini"):
        try:
            record_finding_issue(
                frid, "stranske/Findings#2", arm=bogus, identity=frident, conn=fconn
            )
        except ValueError as exc:
            assert "arm_not_in_subject_set" in str(exc), exc
        else:
            raise AssertionError(f"arm outside the round's set accepted: {bogus}")
    try:
        record_finding_issue(
            "no/such:audit:2026-08-23",
            "stranske/Findings#3",
            arm="codex",
            identity=frident,
            conn=fconn,
        )
    except ValueError as exc:
        assert "no registered or attempted arm" in str(exc), exc
    else:
        raise AssertionError("a finding was filed against an unregistered round")

    def _scored_run(run_id: str, target: str, agent: str, durability: str, ts: int) -> None:
        fconn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent) VALUES (?,?,?,?,?)",
            (run_id, ts, target, "implement", agent),
        )
        fconn.execute(
            "INSERT INTO outcomes (run_id,verifier_verdict,merged,durability) VALUES (?,?,?,?)",
            (run_id, "PASS", 1, durability),
        )
        fconn.commit()

    # ONE scored run on the issue is unambiguous, so it resolves with no binding needed.
    _scored_run("impl-1", "stranske/Findings#1", "claude", "durable", 1000)
    one = resolve_round_durability(frid, conn=fconn)
    assert one["per_arm_durability"] == {"codex": {"durable": 1}}, one
    assert (one["resolved"], one["unresolved"], one["drainable_by_binding"]) == (1, 0, 0), one

    # A LATER REVIEW RUN ON THE SAME ISSUE MUST NOT REWRITE THE ARM'S DURABILITY. Under
    # `ORDER BY r.ts DESC LIMIT 1` this one extra row silently turned `durable` into `reverted`.
    _scored_run("review-1", "stranske/Findings#1", "codex", "reverted", 2000)
    amb = resolve_round_durability(frid, conn=fconn)
    assert amb["per_arm_durability"] == {}, amb
    assert amb["unresolved_by_reason"] == {"ambiguous_outcome_runs": 1}, amb
    # The blocking number and its drain in one place: 1 unresolved, 1 of them drainable HERE.
    assert (amb["unresolved"], amb["drainable_by_binding"]) == (1, 1), amb

    # THE DRAIN: naming the delivering run resolves it, and to the run NAMED -- not the newest.
    record_finding_implementation(frid, "stranske/Findings#1", "impl-1", conn=fconn)
    fixed = resolve_round_durability(frid, conn=fconn)
    assert fixed["per_arm_durability"] == {"codex": {"durable": 1}}, fixed
    assert (fixed["unresolved"], fixed["drainable_by_binding"]) == (0, 0), fixed

    # A BINDING IS VALIDATED AGAINST THE BRAIN, so the fix cannot degrade into a caller's
    # assertion: an unknown run and a run that targeted a different issue are both refused.
    _scored_run("impl-elsewhere", "stranske/Findings#7", "claude", "durable", 3000)
    for bad_run in ("no-such-run", "impl-elsewhere"):
        try:
            record_finding_implementation(frid, "stranske/Findings#1", bad_run, conn=fconn)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unvalidated implementation run accepted: {bad_run}")
    # A binding needs a filing to bind to; inventing one would create an arm-less attribution.
    try:
        record_finding_implementation(frid, "stranske/Findings#7", "impl-elsewhere", conn=fconn)
    except ValueError as exc:
        assert "no filed finding" in str(exc), exc
    else:
        raise AssertionError("an implementation was bound to a finding nobody filed")
    fconn.close()

    print(
        "research_subjects.py selftest: OK (canonical identity, active/cooldown gate, "
        "legacy-safe provenance, independent-subject effective sample count, "
        "domain research namespace, multi-agent research rounds, validated finding arms, "
        "bound-not-inferred issue durability)"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI so the ISSUE FILER can record a linkage without importing this module.

    The filer is `file-agent-issue`, invoked by an agent in its own session; a subprocess call is
    the only seam it has. Without a caller `record_finding_issue` would be one more fully-built
    dormant feature, which is this project's dominant defect class, not a bug.
    """
    import argparse

    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(prog="research_subjects.py")
    sub = parser.add_subparsers(dest="cmd")
    filed = sub.add_parser("finding-filed", help="record that a round's finding became an issue")
    filed.add_argument("--round-id", required=True)
    filed.add_argument("--issue", required=True, help="owner/repo#N the finding became")
    filed.add_argument("--arm", required=True, help="the agent that produced the finding")
    filed.add_argument("--finding-ref", help="stable id of the finding inside the round")
    filed.add_argument(
        "--implementation-run-id",
        help="the run that delivered the issue, when it is already known (rarely, at filing time)",
    )
    impl = sub.add_parser(
        "finding-implemented",
        help="bind a filed finding to THE run that implemented it (drains ambiguity)",
    )
    impl.add_argument("--round-id", required=True)
    impl.add_argument("--issue", required=True, help="owner/repo#N the finding became")
    impl.add_argument("--run-id", required=True, help="the run that delivered that issue")
    dur = sub.add_parser("round-durability", help="inherit downstream durability for a round")
    dur.add_argument("--round-id", required=True)
    dur.add_argument(
        "--apply-edges",
        action="store_true",
        help="write the linkage into influence_edges (default: read only)",
    )
    args = parser.parse_args(argv)
    if args.cmd == "finding-filed":
        conn = feedback._conn()
        try:
            ensure_schema(conn)
            identity = round_identity(args.round_id, conn=conn)
            if identity is None:
                # FAIL LOUD, NOT SILENT. An unregistered round means the linkage would hang off no
                # subject and inherit nothing; saying so is the difference between a fixable
                # mistake and a fact that quietly never existed.
                print(
                    json.dumps(
                        {"recorded": False, "reason": f"no registered round {args.round_id}"}
                    )
                )
                return 1
            try:
                event_id = record_finding_issue(
                    args.round_id,
                    args.issue,
                    arm=args.arm,
                    identity=identity,
                    finding_ref=args.finding_ref,
                    implementation_run_id=args.implementation_run_id,
                    conn=conn,
                )
            except ValueError as exc:
                # A REFUSED ATTRIBUTION IS A RESULT, NOT A CRASH. The filer is a subprocess seam;
                # a named reason and a non-zero exit is what it can act on.
                print(json.dumps({"recorded": False, "reason": str(exc)}))
                return 1
        finally:
            conn.close()
        print(
            json.dumps(
                {
                    "recorded": True,
                    "event_id": event_id,
                    "round_id": args.round_id,
                    "issue": args.issue,
                    "arm": args.arm,
                }
            )
        )
        return 0
    if args.cmd == "finding-implemented":
        conn = feedback._conn()
        try:
            ensure_schema(conn)
            try:
                event_id = record_finding_implementation(
                    args.round_id, args.issue, args.run_id, conn=conn
                )
            except ValueError as exc:
                print(json.dumps({"recorded": False, "reason": str(exc)}))
                return 1
        finally:
            conn.close()
        print(
            json.dumps(
                {
                    "recorded": True,
                    "event_id": event_id,
                    "round_id": args.round_id,
                    "issue": args.issue,
                    "implementation_run_id": args.run_id,
                }
            )
        )
        return 0
    if args.cmd == "round-durability":
        print(
            json.dumps(
                resolve_round_durability(args.round_id, apply_edges=args.apply_edges),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(json.dumps(summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
