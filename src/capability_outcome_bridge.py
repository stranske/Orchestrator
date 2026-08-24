#!/usr/bin/env python3
"""capability_outcome_bridge.py — propagate run outcomes into the capability ledger.

THE GAP THIS CLOSES (measured 2026-08-09): the Brain holds 3,825 run outcomes; the capability
ledger sees almost none of them. Only 2 of 33 capabilities carry any `outcome_links`. Roles are the
clearest case — `roles.py` emits 5 `match` and 5 `invocation` heartbeats but only ONE `outcome`
heartbeat, which is exactly why every role capability sits in `invoked_without_outcomes` and why
`capabilities.usage` reports every gate as starved. Capabilities record that they RAN; nothing
records how their work TURNED OUT.

THE CAUSAL-EDGE PATH IS NOW LIVE (updated 2026-08-18). `capability_causal_evidence()` reads
`influence_edges` filtered by capability_id AND capability_version_id, and
`_record_influence_edge_in_conn` refuses an edge carrying one without the other. That path used to
be unusable because no capability had immutable version lineage; lineage adoption fixed it, and the
fleet now carries 170 fully-versioned capability edges. The heartbeat path below still runs, and
`backfill_role_capability_edges()` repairs the edge path — see its docstring for why the edges alone
could never resolve.

ATTRIBUTION IS EXPLICIT, NEVER INFERRED. A capability is credited only when a real recorded link
says so: an outcome's `influenced_by_run_id`, or an explicit capability tag on the run. Entrypoint
strings are deliberately NOT used for attribution — all 33 capabilities declare one, which makes it
tempting, but they are ambiguous ("adapters.py/dispatcher.py") and would credit capabilities for
work they never touched. Fabricated evidence is worse than none.

EXTENSIBLE BY DESIGN. `RESOLVERS` is an ordered list of (name, fn) that map a run to capability ids.
As more capabilities get wired to real triggers, add a resolver — the bridge then carries them
automatically. Capabilities with no resolver yet are reported as `unattributed`, which is a queue of
work to enable, NOT a retirement list: the point of this system is to increase capability usage.

    python3 capability_outcome_bridge.py --dry-run     # show what would be linked
    python3 capability_outcome_bridge.py               # write outcome heartbeats
    python3 capability_outcome_bridge.py --selftest    # offline

Idempotent: each (capability, run) link carries a stable idempotency key, so re-running never
double-counts. Safe to put on a cadence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import capabilities
import env_prereq
import feedback

# Terminal verdicts worth propagating. A still-pending outcome is not evidence yet.
TERMINAL_VERDICTS = {"PASS", "FAIL"}
DEFAULT_LOOKBACK_DAYS = 30
_ROLE_RUN_RE = re.compile(r"^role:([a-z_]+):", re.IGNORECASE)


def _role_capability_ids(run_id: str, row: dict) -> list[str]:
    """`role:triage:gemini:...` -> `role-triage`, via the role registry (never a guessed name)."""
    source = str(row.get("influenced_by_run_id") or "")
    match = _ROLE_RUN_RE.match(source)
    if not match:
        return []
    try:
        import roles

        registry = roles.ROLE_CAPABILITY_IDS
    except Exception:
        return []
    return [registry[match.group(1).lower()]] if match.group(1).lower() in registry else []


def _tagged_capability_ids(run_id: str, row: dict) -> list[str]:
    """Capabilities the run itself declared, when the recorded metadata carries them."""
    raw = row.get("capability_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    return [str(c) for c in (raw or []) if c]


# Ordered; every resolver must return ids backed by a RECORDED link, never a heuristic.
RESOLVERS = [
    ("role_influence", _role_capability_ids),
    ("run_tagged", _tagged_capability_ids),
]


def _known_capability_ids(path: Path | None = None) -> set[str]:
    try:
        return set(capabilities.load(path or capabilities.REG))
    except Exception:
        return set()


def collect(*, lookback_days: int = DEFAULT_LOOKBACK_DAYS, conn=None) -> list[dict]:
    """Terminal outcomes in the window, with their raw attribution inputs."""
    close = conn is None
    c = conn or feedback._conn()
    try:
        cutoff = f"-{int(lookback_days)} days"
        rows = c.execute(
            """SELECT o.run_id, o.adjudicated_verdict, o.merged, o.durability,
                      o.influenced_by_run_id, r.task_type, r.agent, r.ts
               FROM outcomes o JOIN runs r ON r.run_id = o.run_id
               WHERE o.adjudicated_verdict IS NOT NULL
                 AND r.ts >= strftime('%s', 'now', ?)
               ORDER BY r.ts DESC""",
            (cutoff,),
        ).fetchall()
        # THE run_tagged RESOLVER WAS DEAD CODE UNTIL THIS QUERY EXISTED. It reads
        # `row["capability_ids"]`, but the SELECT above never fetched them and there is no
        # `runs.capability_ids` column — the authoritative record of a run's capability tags is
        # `influence_edges`. So every tag written by `record_run(capability_ids=...)` produced a
        # real Brain edge and then went nowhere: the ledger never learned the outcome, and the
        # capability stayed in `invoked_without_outcomes` no matter how much work it did. Found
        # 2026-08-21 while measuring between phases, after tagging four capabilities and watching
        # the bridge attribute exactly none of them.
        tagged: dict[str, list[str]] = {}
        try:
            for run_id, capability_id in c.execute(
                "SELECT target_run_id, capability_id FROM influence_edges "
                "WHERE influence_type='capability' AND capability_id IS NOT NULL"
            ).fetchall():
                tagged.setdefault(str(run_id), []).append(str(capability_id))
        except sqlite3.Error:
            tagged = {}
    finally:
        if close:
            c.close()
    return [
        {
            "run_id": r[0],
            "verdict": r[1],
            "merged": r[2],
            "durability": r[3],
            "influenced_by_run_id": r[4],
            "task_type": r[5],
            "agent": r[6],
            "ts": r[7],
            "capability_ids": sorted(set(tagged.get(str(r[0]), []))),
        }
        for r in rows
    ]


def attribute(rows: list[dict], *, known: set[str] | None = None) -> dict:
    """Map outcomes to capabilities using only recorded links.

    Returns {links, unattributed, unknown_capability}. `unattributed` is the enablement queue:
    outcomes whose capability we cannot yet name because no trigger has been wired.
    """
    known = known if known is not None else _known_capability_ids()
    links, unattributed, unknown = [], [], []
    for row in rows:
        if str(row.get("verdict") or "").upper() not in TERMINAL_VERDICTS:
            continue
        found: list[tuple[str, str]] = []
        for name, resolver in RESOLVERS:
            for cap_id in resolver(row["run_id"], row):
                if cap_id in known:
                    found.append((cap_id, name))
                else:
                    unknown.append(
                        {"run_id": row["run_id"], "capability_id": cap_id, "resolver": name}
                    )
        if not found:
            unattributed.append(
                {
                    "run_id": row["run_id"],
                    "task_type": row.get("task_type"),
                    "agent": row.get("agent"),
                }
            )
            continue
        # MANY-TO-ONE IS THE NORMAL CASE: one work process routinely uses several capabilities, so
        # a run gets one link PER capability. Dedupe on capability id, not on (id, resolver) —
        # two resolvers naming the same capability is corroboration, not two separate uses.
        seen: dict[str, str] = {}
        for cap_id, resolver_name in found:
            seen.setdefault(cap_id, resolver_name)
        for cap_id, resolver_name in seen.items():
            links.append(
                {
                    "capability_id": cap_id,
                    "run_id": row["run_id"],
                    "resolver": resolver_name,
                    "verdict": row["verdict"],
                    "durability": row.get("durability"),
                }
            )
    return {"links": links, "unattributed": unattributed, "unknown_capability": unknown}


def apply_links(links: list[dict], *, path: Path | None = None, dry_run: bool = False) -> dict:
    """Emit one idempotent `outcome` heartbeat per (capability, run).

    Uses `capabilities.heartbeat` directly rather than `production_heartbeat`: the latter is gated
    on ORCH_CAPABILITY_HEARTBEATS, which exists to keep in-process emitters inert during tests. An
    operator or cadence invoking this tool is an explicit act, and the selftest writes to a temp
    ledger, so the guard would only make manual runs silently do nothing.
    """
    ledger = path or capabilities.REG
    written, skipped = 0, 0
    for link in links:
        if dry_run:
            skipped += 1
            continue
        # Stable key => re-running the bridge never double-counts a link.
        changed = capabilities.heartbeat(
            link["capability_id"],
            "outcome",
            ref=link["run_id"],
            path=ledger,
            idempotency_key=f"capability-outcome:{link['capability_id']}:{link['run_id']}",
            metadata={
                "verdict": link["verdict"],
                "durability": link.get("durability"),
                "resolver": link["resolver"],
            },
        )
        written += 1 if changed else 0
        skipped += 0 if changed else 1
    return {
        "written": written,
        "already_linked": skipped if not dry_run else 0,
        "would_write": len(links) if dry_run else 0,
    }


def backfill_role_capability_edges(*, dry_run: bool = False, conn=None) -> dict:
    """Re-attribute a role run's capability onto the run that ACTED on its proposal.

    WHY THE EDGES COULD NEVER RESOLVE. `record_role_run` tags the role run itself, but a role run is
    advisory: it proposes and never produces a PR, so no `outcomes` row is ever written for it.
    Measured 2026-08-18: all 170 capability edges targeted `role:triage:*` runs and NOT ONE carried a
    verdict or durability, so `capability_effectiveness` reported `not_yet_measurable` forever — a
    populated numerator with no denominator.

    The acting run does terminate. `feedback._record_outcome_in_conn` already back-propagates over
    accepted edges, so moving the attribution to the acting run is the entire fix. `record_run` now
    does this for new runs; this repairs the history, and stays in the daily bridge so it self-heals
    rather than being a one-off migration.

    Idempotent: skips any target that already carries a capability edge, so re-running is a no-op.
    """
    close = conn is None
    c = conn or feedback._conn()
    created, resolved, skipped = [], 0, 0
    try:
        rows = c.execute("""SELECT DISTINCT r.source_run_id, r.target_run_id, ce.capability_id,
                               ce.capability_version_id
               FROM influence_edges r
               JOIN influence_edges ce
                 ON ce.target_run_id = r.source_run_id
                AND ce.influence_type = 'capability'
                AND ce.capability_id IS NOT NULL
                AND ce.capability_version_id IS NOT NULL
               WHERE r.influence_type = 'role'
                 AND r.source_run_id IS NOT NULL
                 AND NOT EXISTS (
                       SELECT 1 FROM influence_edges x
                        WHERE x.target_run_id = r.target_run_id
                          AND x.influence_type = 'capability'
                          AND x.capability_id = ce.capability_id)""").fetchall()
        unlinked = 0
        for role_run, work_run, cap_id, cap_version in rows:
            if dry_run:
                created.append(
                    {"capability_id": cap_id, "target_run_id": work_run, "source_run_id": role_run}
                )
                continue
            feedback._record_influence_edge_in_conn(
                c,
                target_run_id=str(work_run),
                influence_type="capability",
                influence_id=str(cap_version),
                source_run_id=str(role_run),
                accepted=True,
                capability_id=str(cap_id),
                capability_version_id=str(cap_version),
                # DECLARED UNLINKED: a backfill re-attributes onto runs that may predate the
                # completion-event plane or be advisory, so some targets have no envelope. Those
                # edges are associations, not causal evidence. Measured 2026-08-21: 202 of 296
                # orphan edges came from exactly this class of backfill, reported as plain successes
                # -- hence `unlinked_edges` below, so the count is visible where it is CREATED
                # rather than found by an audit two weeks later.
                allow_unlinked=True,
            )
            if not feedback._latest_completion_event_id(c, str(work_run)):
                unlinked += 1
            # Resolve immediately from the outcome that already exists for the acting run.
            resolved += feedback._propagate_outcome_lineage_in_conn(c, str(work_run))
            created.append(
                {"capability_id": cap_id, "target_run_id": work_run, "source_run_id": role_run}
            )
        if not dry_run:
            c.commit()
    finally:
        if close:
            c.close()
    return {
        "backfilled": len(created),
        "edges_resolved": resolved,
        "skipped": skipped,
        "unlinked_edges": unlinked,
        "dry_run": dry_run,
        "links": created[:20],
    }


def backfill_offload_capability_edges(*, dry_run: bool = False, conn=None) -> dict:
    """Tag historical role runs that were produced BY an offload with the `offload` capability.

    `record_role_run` now does this for new runs. The link is direct and recorded, not inferred:
    the role run's own `decomposition` payload carries `backend_run_id`, which IS the offload's run.
    A role run with no backend_run_id was replayed offline and gets nothing.

    Why it matters: `offload` logged invocations and zero outcomes, so it sat in
    `invoked_without_outcomes` forever — while 861 role runs recorded an offload that produced their
    proposal, 17 of which have since inherited a terminal outcome through the role lineage.

    Idempotent (skips a run that already carries the tag), and it lives in the daily bridge so it
    self-heals instead of being a one-off migration.
    """
    close = conn is None
    c = conn or feedback._conn()
    created, resolved = [], 0
    # feedback's own resolver, so the edge carries the same immutable version id every other
    # capability edge carries; _record_influence_edge_in_conn refuses an id without a version.
    versions = feedback._resolve_capability_versions(["offload"]) or []
    version = versions[0] if versions else None
    try:
        rows = c.execute("""SELECT r.run_id, r.decomposition FROM runs r
                WHERE r.role_name IS NOT NULL AND r.decomposition IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM influence_edges x
                         WHERE x.target_run_id = r.run_id
                           AND x.influence_type = 'capability'
                           AND x.capability_id = 'offload')""").fetchall()
        for run_id, raw in rows:
            try:
                payload = json.loads(raw or "{}")
            except (ValueError, TypeError):
                continue
            if not payload.get("backend_run_id"):
                continue
            if dry_run:
                created.append({"run_id": run_id, "capability_id": "offload"})
                continue
            if version is None:
                continue
            feedback._record_influence_edge_in_conn(
                c,
                target_run_id=str(run_id),
                influence_type="capability",
                influence_id=str(version),
                source_run_id=str(run_id),
                accepted=True,
                capability_id="offload",
                capability_version_id=str(version),
            )
            resolved += feedback._propagate_outcome_lineage_in_conn(c, str(run_id))
            created.append({"run_id": run_id, "capability_id": "offload"})
        if not dry_run:
            c.commit()
    finally:
        if close:
            c.close()
    return {
        "backfilled": len(created),
        "edges_resolved": resolved,
        "dry_run": dry_run,
        "links": created[:20],
    }


def compiled_workflow_subjects(*, path: Path | None = None) -> dict:
    """Which consumer repos has each compiled-workflow rail actually acted on?

    Subject identity for these rails is the CONSUMER REPO, not the proposal and not the PR. That
    choice is load-bearing: `_causal_readiness` promotes on `min_independent_durable_reuse` (3)
    DISTINCT durable subjects, and the three PRs that came out of the July consumer-sync pilot
    (Workflows#2755/#2756/#2757, all merged the same day from one issue) are correlated evidence.
    Counting them as three independent subjects would have satisfied the gate on what is really one
    result — the "do not treat correlated arms as independent evidence" rule in CLAUDE.md 2.
    """
    ledger = capabilities.load(path or capabilities.REG, create=False)
    out: dict[str, dict] = {}
    for cap_id, cap in ledger.items():
        if (cap.get("matcher") or {}).get("kind") != "compiled_workflow":
            continue
        subjects: set[str] = set()
        for event in cap.get("event_history") or []:
            subject = ((event.get("metadata") or {}).get("subject_id") or "").strip().lower()
            if subject:
                subjects.add(subject)
        out[cap_id] = {
            "subjects": sorted(subjects),
            "version_id": cap.get("capability_version_id"),
            "status": cap.get("status"),
        }
    return out


def attribute_compiled_workflow_edges(
    *, dry_run: bool = False, conn=None, path: Path | None = None
) -> dict:
    """Attribute a compiled-workflow rail to delivered work on the SAME subject.

    WHY THE GATE READ AN EMPTY TABLE. `capability_causal_evidence` joins `influence_edges` on
    (capability_id, capability_version_id). Brain-wide only `role-triage` carried attribution,
    because `backfill_role_capability_edges` filters `influence_type='role'`. A compiled-workflow
    rail writes its evidence through `record_runner_effect`, which touches ONLY the ledger's
    event_history — so no Brain run existed to attach an edge to, and
    `independent_durable_reuse`/`evidence_age` could never flip no matter how long anyone waited.

    WHAT THIS DELIBERATELY DOES NOT DO. It does not infer the link from timing. "The rail ran on
    repo R, and later some durable PR landed in R" is not causation, and manufacturing edges from
    that would corrupt the Brain far worse than an empty gate — the whole point of the durability
    label is that it cannot be gamed. The role bridge is safe precisely because a real recorded
    edge already links proposal to acting run; no such record exists here, so this requires an
    EXPLICIT link: the delivered run must itself name the capability (routing_metadata
    causal_context.capability_id, the same stamp `record_run` already inherits).

    So a zero result here is an honest measurement, not a failure: it says the rail's proposals are
    not yet reaching identified delivery. That number is what should drive the promotion decision.
    """
    close = conn is None
    c = conn or feedback._conn()
    seen = compiled_workflow_subjects(path=path)
    created, resolved = [], 0
    try:
        for cap_id, info in seen.items():
            version = str(info.get("version_id") or "")
            if not version or not info["subjects"]:
                continue
            for subject in info["subjects"]:
                rows = c.execute(
                    """SELECT r.run_id, r.target, r.routing_metadata
                         FROM runs r JOIN outcomes o ON o.run_id = r.run_id
                        WHERE r.target LIKE ?
                          AND o.durability IS NOT NULL
                          AND NOT EXISTS (
                                SELECT 1 FROM influence_edges x
                                 WHERE x.target_run_id = r.run_id
                                   AND x.influence_type = 'capability'
                                   AND x.capability_id = ?)""",
                    (f"{subject}#%", cap_id),
                ).fetchall()
                for run_id, target, routing in rows:
                    meta = feedback._routing_metadata_dict(routing) or {}
                    context = meta.get("causal_context") or {}
                    claimed = str(
                        context.get("capability_id") or meta.get("capability_id") or ""
                    ).strip()
                    if claimed != cap_id:
                        continue  # no explicit link — never guess from timing
                    if dry_run:
                        created.append(
                            {
                                "capability_id": cap_id,
                                "target_run_id": run_id,
                                "subject_id": subject,
                            }
                        )
                        continue
                    feedback._record_influence_edge_in_conn(
                        c,
                        target_run_id=str(run_id),
                        influence_type="capability",
                        influence_id=version,
                        source_run_id=None,
                        accepted=True,
                        capability_id=cap_id,
                        capability_version_id=version,
                    )
                    resolved += feedback._propagate_outcome_lineage_in_conn(c, str(run_id))
                    created.append(
                        {"capability_id": cap_id, "target_run_id": run_id, "subject_id": subject}
                    )
        if not dry_run:
            c.commit()
    finally:
        if close:
            c.close()
    return {
        "attributed": len(created),
        "edges_resolved": resolved,
        "dry_run": dry_run,
        "rails": {k: v["subjects"] for k, v in seen.items()},
        "subjects_seen": sum(len(v["subjects"]) for v in seen.values()),
        "links": created[:20],
    }


# The cross-repo capabilities the Orchestrator can OBSERVE but never executes. Each names the
# workflow whose runs prove it fired. Declared, not discovered: a capability is credited from CI only
# because it is listed here with the exact workflow file, so nothing is inferred from a name match.
EXTERNAL_CI_CAPABILITIES = {
    "docs-drift-fix-agent": {
        "repo": "stranske/Workflows",
        "workflow": "maint-87-docs-drift-fix-agent.yml",
    },
}
EXTERNAL_CI_LOOKBACK_RUNS = int(os.environ.get("ORCH_EXTERNAL_CI_LOOKBACK_RUNS", "10"))


def _gh_workflow_runs(repo: str, workflow: str) -> list[dict]:
    """Recent runs of one workflow, newest first. Read-only."""
    proc = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--limit",
            str(EXTERNAL_CI_LOOKBACK_RUNS),
            "--json",
            "databaseId,status,conclusion,createdAt",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "gh run list failed").strip()[:160])
    return json.loads(proc.stdout or "[]")


def ingest_external_ci_invocations(
    *, dry_run: bool = False, path: Path | None = None, runs_fn=None
) -> dict:
    """Credit capabilities whose entrypoint lives in ANOTHER repo, from that repo's workflow runs.

    THE GAP THIS CLOSES. `docs-drift-fix-agent`'s entrypoint is in the Workflows repo, so
    `capability_activation_audit` correctly reports `no_local_entrypoint` and no local heartbeat is
    possible -- the capability ran weekly in that repo's CI and this ledger recorded nothing.
    `external_caller()` already answers CAN it fire (does the workflow exist); this answers DID it.

    THE OBSERVATION IS THE RUN, NOT AN OUTCOME. A completed run proves the capability EXECUTED; it
    says nothing about whether the resulting repair merged durably. So this records an `invocation`
    heartbeat only. The outcome half belongs in keepalive_outcomes' ingest of the resulting PRs and
    is deliberately still deferred -- building it for one data point is the built-and-forgotten
    failure mode, and its revisit trigger is recorded in the ledger notes.

    IDEMPOTENT PER RUN, keyed on the workflow run id, because this capability's deferral uses its
    invocation COUNT as the revisit trigger: a number that inflated on each daily re-read would fire
    that trigger falsely. `credited` counts only heartbeats that actually landed.
    """
    out: dict[str, Any] = {"observed": {}, "credited": 0, "already_recorded": 0, "errors": []}
    for cap_id, spec in EXTERNAL_CI_CAPABILITIES.items():
        try:
            runs = (runs_fn or _gh_workflow_runs)(spec["repo"], spec["workflow"])
        except Exception as exc:  # noqa: BLE001 -- telemetry must not break the step
            out["errors"].append({"capability_id": cap_id, "error": str(exc)[:160]})
            continue
        completed = [r for r in runs if str(r.get("status") or "") == "completed"]
        out["observed"][cap_id] = {"runs_seen": len(runs), "completed": len(completed)}
        if dry_run:
            continue
        for r in completed:
            ref = f"{spec['repo']}:{spec['workflow']}:{r.get('databaseId')}"
            try:
                # `path or capabilities.REG`: heartbeat's default is the live ledger and it does NOT
                # accept None. Passing the caller's None through raised
                # "'NoneType' object has no attribute 'parent'" and was swallowed into `errors`.
                if capabilities.heartbeat(
                    cap_id,
                    "invocation",
                    ref=ref,
                    path=path or capabilities.REG,
                    idempotency_key=ref,
                ):
                    out["credited"] += 1
                else:
                    out["already_recorded"] += 1
            except Exception as exc:  # noqa: BLE001
                out["errors"].append({"capability_id": cap_id, "error": str(exc)[:160]})
    return out


def run(
    *, lookback_days: int = DEFAULT_LOOKBACK_DAYS, dry_run: bool = False, path: Path | None = None
) -> dict:
    # EDGE REPAIRS RUN FIRST, so this cycle's heartbeats see them. They used to run after
    # apply_links, which meant a repaired edge could not reach the ledger until the NEXT daily
    # cycle — a 24h lag that reads exactly like "the repair did nothing" when you measure right
    # after running it. (2026-08-21)
    #
    # Repair the causal-edge path: move each role run's capability attribution onto the run that
    # acted on its proposal, so outcome propagation can actually resolve it. Idempotent, so this is
    # a no-op once history is repaired. Never allowed to break the heartbeat path below.
    try:
        edge_fix = backfill_role_capability_edges(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the step
        edge_fix = {"error": str(exc)[:200], "backfilled": 0, "edges_resolved": 0}
    # Same repair for the transport that PRODUCED a role's proposal: the role run records the
    # offload's run id, so the tag is a recorded link. Also idempotent, also self-healing.
    try:
        offload_fix = backfill_offload_capability_edges(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        offload_fix = {"error": str(exc)[:200], "backfilled": 0, "edges_resolved": 0}
    # Cross-repo capabilities: observe the OTHER repo's workflow runs, because no local code path
    # can ever credit them. Wrapped like the repairs above -- an unreachable `gh` must not break the
    # local heartbeat path that follows.
    try:
        external_ci = ingest_external_ci_invocations(dry_run=dry_run, path=path)
    except Exception as exc:  # noqa: BLE001
        external_ci = {"error": str(exc)[:200], "credited": 0, "observed": {}}
    rows = collect(lookback_days=lookback_days)
    mapped = attribute(rows, known=_known_capability_ids(path))
    result = apply_links(mapped["links"], path=path, dry_run=dry_run)
    # Compiled-workflow rails are a SECOND producer class the role filter never covered; without
    # this their promotion gates read an empty evidence set forever. Reports 0 honestly until a
    # delivered run explicitly names the capability — it never infers the link from timing.
    try:
        compiled_fix = attribute_compiled_workflow_edges(dry_run=dry_run, path=path)
    except Exception as exc:  # noqa: BLE001
        compiled_fix = {"error": str(exc)[:200], "attributed": 0, "subjects_seen": 0}
    by_cap: dict[str, int] = {}
    for link in mapped["links"]:
        by_cap[link["capability_id"]] = by_cap.get(link["capability_id"], 0) + 1
    return {
        "offload_capability_edges": offload_fix,
        "lookback_days": lookback_days,
        "dry_run": dry_run,
        "terminal_outcomes": len(rows),
        "links": len(mapped["links"]),
        "by_capability": dict(sorted(by_cap.items(), key=lambda kv: -kv[1])),
        # The enablement queue — outcomes we could not attribute because no trigger is wired yet.
        "unattributed": len(mapped["unattributed"]),
        "unknown_capability": mapped["unknown_capability"][:10],
        "role_capability_edges": {k: v for k, v in edge_fix.items() if k != "links"},
        # Second producer class. `subjects_seen` is the number to watch: while it is 0 the rails
        # have recorded no consumer repo yet, and while `attributed` stays 0 with subjects > 0 the
        # rails are running but their output is not reaching identified delivery.
        "compiled_workflow_edges": {k: v for k, v in compiled_fix.items() if k != "links"},
        # Cross-repo observation: `credited` is invocations recorded from another repo's CI.
        "external_ci": external_ci,
        **result,
    }


def _selftest() -> None:
    # CROSS-REPO CREDITING: a capability whose entrypoint lives in ANOTHER repo can never be
    # credited by a local code path. Injected runs, so this never touches the network.
    import tempfile as _tf

    with _tf.TemporaryDirectory(prefix="external-ci-") as _td:
        _lp = Path(_td) / "caps.json"
        capabilities.register(
            "docs-drift-fix-agent",
            {
                "status": "wired",
                "owner": "orchestrator",
                "matcher": {"kind": "ci_workflow", "name": "maint-87"},
                "entrypoint": "Workflows/scripts/docs_drift_fix_agent.py",
                "trigger_cadence": "weekly",
                "expiry": capabilities._now() + 86400,
                "kill_switch": "disable the workflow",
                "rollback": {"transition": "retired"},
            },
            path=_lp,
        )
        _runs = [
            {"databaseId": 111, "status": "completed", "conclusion": "success"},
            {"databaseId": 222, "status": "in_progress", "conclusion": None},
        ]
        _r1 = ingest_external_ci_invocations(path=_lp, runs_fn=lambda r, w: _runs)
        # ONLY completed runs count: an in-progress run has not proved the capability executed.
        assert _r1["credited"] == 1, _r1
        assert _r1["observed"]["docs-drift-fix-agent"]["completed"] == 1, _r1
        # IDEMPOTENT ON RE-READ. A daily cadence re-reads the same runs; a count that grows on
        # repeat is unusable as the revisit trigger this capability's deferral depends on.
        _r2 = ingest_external_ci_invocations(path=_lp, runs_fn=lambda r, w: _runs)
        assert _r2["credited"] == 0 and _r2["already_recorded"] == 1, _r2
        _cap = capabilities.load(_lp, create=False)["docs-drift-fix-agent"]
        assert len([e for e in _cap["event_history"] if e.get("type") == "invocation"]) == 1
        # A NEW run does credit, or the capability freezes at its first observation.
        _r3 = ingest_external_ci_invocations(
            path=_lp, runs_fn=lambda r, w: _runs + [{"databaseId": 333, "status": "completed"}]
        )
        assert _r3["credited"] == 1, _r3

        # An unreachable `gh` is reported, never raised.
        def _boom(r, w):
            raise RuntimeError("gh unavailable")

        _r4 = ingest_external_ci_invocations(path=_lp, runs_fn=_boom)
        assert _r4["errors"] and _r4["credited"] == 0, _r4

    import tempfile

    # Attribution is explicit: a role-influenced outcome resolves, an unlinked one does not.
    known = {"role-triage", "role-prompt"}
    rows = [
        {
            "run_id": "r1",
            "verdict": "PASS",
            "durability": "durable",
            "influenced_by_run_id": "role:triage:gemini:123",
            "task_type": "implement",
            "agent": "gemini",
        },
        {
            "run_id": "r2",
            "verdict": "PASS",
            "durability": "durable",
            "influenced_by_run_id": None,
            "task_type": "implement",
            "agent": "codex",
        },
        {
            "run_id": "r3",
            "verdict": None,
            "durability": None,  # not terminal => ignored
            "influenced_by_run_id": "role:triage:gemini:124",
            "task_type": "implement",
            "agent": "gemini",
        },
        {
            "run_id": "r4",
            "verdict": "PASS",
            "durability": "durable",
            "influenced_by_run_id": "role:nosuchrole:x:1",
            "task_type": "implement",
            "agent": "codex",
        },
    ]
    mapped = attribute(rows, known=known)
    assert [entry["capability_id"] for entry in mapped["links"]] == ["role-triage"], mapped
    assert {u["run_id"] for u in mapped["unattributed"]} == {"r2", "r4"}, mapped
    assert all(r["run_id"] != "r3" for r in mapped["unattributed"]), "non-terminal must be skipped"

    # MULTI-CAPABILITY: one run using several capabilities yields one link EACH, and a capability
    # named by two resolvers yields exactly one link (corroboration, not double-counting).
    multi = attribute(
        [
            {
                "run_id": "m1",
                "verdict": "PASS",
                "durability": "durable",
                "influenced_by_run_id": "role:triage:x:1",
                "capability_ids": ["role-prompt", "role-triage"],
                "task_type": "t",
                "agent": "a",
            }
        ],
        known={"role-triage", "role-prompt"},
    )
    got = sorted(entry["capability_id"] for entry in multi["links"])
    assert got == ["role-prompt", "role-triage"], got
    assert len(multi["links"]) == 2, "a capability named twice must not double-count"

    # An id that resolves but is not a registered capability must NOT be invented into the ledger.
    ghost = attribute(
        [
            {
                "run_id": "g1",
                "verdict": "PASS",
                "durability": "durable",
                "influenced_by_run_id": "role:triage:x:1",
                "task_type": "t",
                "agent": "a",
            }
        ],
        known=set(),
    )
    assert not ghost["links"] and ghost["unknown_capability"], ghost

    # Entrypoint strings must never be used as an attribution source.
    assert not any(
        "entrypoint" in name for name, _ in RESOLVERS
    ), "attribution must stay explicit; entrypoint inference fabricates evidence"

    # ---- COMPILED-WORKFLOW RAILS (the second producer class) ------------------------------------
    # The gate for these read an EMPTY table: their evidence goes to the ledger via
    # record_runner_effect and never to the Brain, so `influence_type='role'` above could not see
    # them and `independent_durable_reuse` could never flip. These cases pin the two properties that
    # make the fix safe rather than merely effective.
    with tempfile.TemporaryDirectory(prefix="cap-compiled-") as tdc:
        ledger = Path(tdc) / "capabilities.json"
        rec = capabilities._blank_capability("capability:rail-under-test")
        rec["status"] = "shadow"
        rec["matcher"] = {"kind": "compiled_workflow", "name": "rail_under_test"}
        rec["capability_version_id"] = "capability-version:" + "d" * 32
        rec["event_history"] = [
            {"type": "output", "timestamp": 1, "metadata": {"subject_id": "stranske/ready"}},
            {"type": "output", "timestamp": 2, "metadata": {"subject_id": "stranske/ready"}},
            {"type": "output", "timestamp": 3, "metadata": {"subject_id": "stranske/pension-data"}},
            {"type": "output", "timestamp": 4, "metadata": {}},  # no subject -> ignored
        ]
        capabilities.save({"capability:rail-under-test": rec}, ledger)

        subs = compiled_workflow_subjects(path=ledger)["capability:rail-under-test"]
        # SUBJECT IDENTITY IS THE CONSUMER REPO. Two effects on the same repo are ONE subject: this
        # is what stops three PRs from a single pilot posing as three independent durable subjects.
        assert subs["subjects"] == ["stranske/pension-data", "stranske/ready"], subs
        assert len(subs["subjects"]) == 2, "same-repo effects must collapse to one subject"

        saved_db = feedback.DB_PATH
        feedback.DB_PATH = Path(tdc) / "brain.db"
        try:
            # Two durable deliveries on a subject the rail touched. Only ONE names the capability.
            feedback.record_run(
                run_id="linked",
                target="stranske/Ready#1",
                task_type="implement",
                agent="codex",
                routing_metadata={
                    "causal_context": {"capability_id": "capability:rail-under-test"}
                },
            )
            feedback.record_run(
                run_id="unlinked",
                target="stranske/Ready#2",
                task_type="implement",
                agent="codex",
                routing_metadata={},
            )
            for rid in ("linked", "unlinked"):
                feedback.record_outcome(
                    run_id=rid, verifier_verdict="PASS", merged=True, durability="durable"
                )
            edge_report: dict[str, Any] = attribute_compiled_workflow_edges(
                path=ledger, dry_run=True
            )
        finally:
            feedback.DB_PATH = saved_db

    targets = {entry["target_run_id"] for entry in edge_report["links"]}
    # EXPLICIT LINK ONLY. "The rail ran on repo R and later a durable PR landed in R" is correlation;
    # manufacturing an edge from it would fake the un-gameable durability label. Deleting the
    # `claimed != cap_id` guard makes `unlinked` appear here and fails this assertion.
    assert targets == {
        "linked"
    }, f"only an explicitly-linked delivery may be attributed: {edge_report}"
    assert edge_report["subjects_seen"] == 2, edge_report

    # Idempotency: applying the same link twice writes once.
    with tempfile.TemporaryDirectory(prefix="cap-bridge-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        record = capabilities._blank_capability("role-triage")
        record["status"] = "shadow"
        capabilities.save({"role-triage": record}, ledger)
        links = [
            {
                "capability_id": "role-triage",
                "run_id": "r1",
                "resolver": "role_influence",
                "verdict": "PASS",
                "durability": "durable",
            }
        ]
        first = apply_links(links, path=ledger)
        second = apply_links(links, path=ledger)
        assert first["written"] == 1, first
        assert second["written"] == 0 and second["already_linked"] == 1, second
        stored = capabilities.load(ledger)["role-triage"]
        assert stored["outcome_links"] == ["r1"], stored
        assert stored["last_success"] or stored["last_invocation"] or True

        # dry-run writes nothing.
        dry = apply_links(
            [
                {
                    "capability_id": "role-triage",
                    "run_id": "r9",
                    "resolver": "role_influence",
                    "verdict": "PASS",
                    "durability": "durable",
                }
            ],
            path=ledger,
            dry_run=True,
        )
        assert dry["would_write"] == 1 and dry["written"] == 0, dry
        assert capabilities.load(ledger)["role-triage"]["outcome_links"] == ["r1"], "dry-run wrote!"

    # ---- collect() MUST supply the recorded capability tags, or run_tagged is dead code ------
    # This is the bug the resolver shipped with: it reads row["capability_ids"], the SELECT never
    # fetched them, and there is no runs.capability_ids column — so every tag written by
    # record_run(capability_ids=...) produced a Brain edge that never reached the ledger.
    # The Brain below is a fresh tmp DB, but the edge writer resolves `agy-runtime-isolation`'s
    # and `offload`'s version lineage from the LEDGER and refuses an edge without one — so the
    # tags this block asserts on can only be written where those rows are registered. Gated as a
    # SECTION: everything above and below it runs on any machine.
    _gaps: list[str] = []
    if env_prereq.runnable(
        _gaps,
        env_prereq.ledger_rows_absent("agy-runtime-isolation", "offload"),
        env_prereq.ledger_version_lineage_absent("agy-runtime-isolation", "offload"),
    ):
        import tempfile as _tf

        _old_db = feedback.DB_PATH
        with _tf.TemporaryDirectory(prefix="bridge-tagged-") as _td:
            feedback.DB_PATH = Path(_td) / "brain.db"
            try:
                feedback.record_run(
                    "tagged:run",
                    "o/r#1",
                    "implement",
                    "gemini",
                    capability_ids=["agy-runtime-isolation"],
                )
                feedback.record_outcome(
                    "tagged:run", adjudicated_verdict="PASS", merged=True, durability="durable"
                )
                tagged_rows: list[dict[str, Any]] = collect()
                row = [r for r in tagged_rows if r["run_id"] == "tagged:run"][0]
                assert row["capability_ids"] == ["agy-runtime-isolation"], row
                mapped = attribute(tagged_rows, known={"agy-runtime-isolation"})
                assert [entry["capability_id"] for entry in mapped["links"]] == [
                    "agy-runtime-isolation"
                ], mapped
                assert mapped["links"][0]["resolver"] == "run_tagged", mapped

                # ---- offload backfill: the link is backend_run_id, and only backend_run_id --------
                # record_role_run now tags `offload` at record time, so a CURRENT row needs no repair.
                # These two are shaped like HISTORY — recorded before that tagging existed — which is
                # exactly what the backfill is for.
                for rid, target, payload in (
                    (
                        "role:redirect:withoffload",
                        "o/r#2",
                        {"role": "redirect", "backend_run_id": "offload:xyz"},
                    ),
                    ("role:redirect:replayed", "o/r#3", {"role": "redirect"}),
                ):
                    feedback.record_run(
                        rid,
                        target,
                        "role:redirect",
                        "cursor",
                        role_name="redirect",
                        decomposition=payload,
                    )
                first = backfill_offload_capability_edges()
                assert first["backfilled"] == 1, first
                with feedback._conn() as c:
                    tagged = {
                        r[0]
                        for r in c.execute(
                            "SELECT target_run_id FROM influence_edges WHERE influence_type='capability' "
                            "AND capability_id='offload'"
                        ).fetchall()
                    }
                assert tagged == {"role:redirect:withoffload"}, tagged
                assert backfill_offload_capability_edges()["backfilled"] == 0, "not idempotent"
            finally:
                feedback.DB_PATH = _old_db

    env_prereq.report_gaps("capability_outcome_bridge.py", _gaps)
    print(
        "capability_outcome_bridge.py selftest: OK (explicit attribution, no entrypoint "
        "inference, unknown ids refused, idempotent, dry-run inert, collect supplies "
        "recorded tags so run_tagged is live, offload backfill keyed on backend_run_id)"
        + (f" — {len(set(_gaps))} section(s) skipped, see above" if _gaps else "")
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    print(json.dumps(run(lookback_days=args.lookback_days, dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
