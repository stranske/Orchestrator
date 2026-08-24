#!/usr/bin/env python3
"""evidence_schema.py - cluster evidence gaps into schema-growth candidates.

The raw `evidence_gaps` table intentionally stores evaluator text verbatim. In
practice those strings vary, so exact-string recurrence can stay at one even
when evaluators repeatedly ask for the same kind of missing evidence. This module
keeps the approval step explicit while making those recurring classes visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import feedback

DEFAULT_WINDOW_DAYS = 120
DEFAULT_MIN_RECURRENCE = 3
DEFAULT_PRUNE_AFTER_DAYS = 30
DEFAULT_MIN_INFLUENCE = 1
DEFAULT_MIN_DISTINCT_SUBJECTS = 3
DEFAULT_MAX_COUNTEREXAMPLE_RATIO = 0.5
DEFAULT_CANDIDATE_TTL_DAYS = 30

CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_gap_context (
  gap_key TEXT PRIMARY KEY, ts INTEGER NOT NULL, ref TEXT, evaluator TEXT,
  gap TEXT NOT NULL, subject_id TEXT NOT NULL, spec_hash TEXT NOT NULL,
  polarity TEXT NOT NULL DEFAULT 'positive', metadata_json TEXT
);
"""

SEMANTIC_SYNONYMS = {
    "pytest": "named_test",
    "test": "named_test",
    "tests": "named_test",
    "suite": "named_test",
    "smoke": "named_test",
    "cli": "named_test",
    "command-line": "named_test",
    "execution": "run",
    "executed": "run",
    "ran": "run",
    "results": "result",
    "output": "result",
    "logs": "result",
    "proof": "evidence",
    "evidence": "evidence",
    "negative": "deliberate_break",
    "mutation": "deliberate_break",
    "break": "deliberate_break",
}
SEMANTIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "no",
    "not",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
    "would",
}

RULES = [
    {
        "name": "post_action_state_capture",
        "rationale": "Evaluators repeatedly lacked post-click or post-action UI state evidence.",
        "patterns": [
            r"\bpost[- ]?(click|action)\b",
            r"\bafter (click|press|run|submit|accept|escalate)\b",
            r"\b(button|action|run analysis|dry-run discovery).*(not observed|not captured|not exercised)",
            r"\bclick behavior\b",
        ],
    },
    {
        "name": "upload_flow_evidence",
        "rationale": "Evaluators repeatedly lacked file upload or upload-validation evidence.",
        "patterns": [
            r"\bupload\b",
            r"\bfile\b.*\b(flow|picker|selection|validation)\b",
            r"\bcsv\b",
            r"\bdocument upload\b",
        ],
    },
    {
        "name": "browse_dialog_evidence",
        "rationale": "Evaluators repeatedly lacked Browse/file-picker/dialog interaction evidence.",
        "patterns": [
            r"\bbrowse\b",
            r"\bfile[- ]?picker\b",
            r"\bdialog\b",
            r"\bfolder[- ]?selection\b",
        ],
    },
    {
        "name": "error_recovery_evidence",
        "rationale": "Evaluators repeatedly lacked error, invalid-input, or recovery-path evidence.",
        "patterns": [
            r"\berror\b",
            r"\binvalid\b",
            r"\bmissing\b.*\b(root|input|file|data)\b",
            r"\brecovery\b",
            r"\bfailure\b",
            r"\bvalidation\b",
        ],
    },
    {
        "name": "screen_coverage_evidence",
        "rationale": "Evaluators repeatedly lacked screenshots/accessibility trees for screens or pages.",
        "patterns": [
            r"\b(accessibility tree|a11y)\b",
            r"\bscreen(s)? (not|were not)\b",
            r"\bpage(s)? (not|were not)\b",
            r"\bnot provided\b",
            r"\bnot captured\b",
            r"\bnot re-driven\b",
        ],
    },
    {
        "name": "keyboard_focus_evidence",
        "rationale": "Evaluators repeatedly lacked keyboard, focus, or accessibility-action evidence.",
        "patterns": [
            r"\bkeyboard\b",
            r"\bfocus\b",
            r"\baccessible action\b",
            r"\ba11y\b",
        ],
    },
    {
        "name": "sample_data_end_to_end_evidence",
        "rationale": "Evaluators repeatedly lacked sample-data or end-to-end successful-run evidence.",
        "patterns": [
            r"\bsample\b",
            r"\bend[- ]to[- ]end\b",
            r"\bsuccessful run\b",
            r"\bfull[- ]pipeline\b",
            r"\bvalid inputs\b",
        ],
    },
    {
        "name": "documentation_help_evidence",
        "rationale": "Evaluators repeatedly lacked documentation, help, tooltip, or instruction evidence.",
        "patterns": [
            r"\breadme\b",
            r"\bdocumentation\b",
            r"\btooltip\b",
            r"\bhover help\b",
            r"\binstructions?\b",
            r"\bhelp\b",
        ],
    },
]


def _since(window_days: int, *, now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    return now - int(window_days) * 86400


def _gap_key(ts: int, ref: str, evaluator: str, gap: str) -> str:
    payload = json.dumps(
        [int(ts), str(ref or ""), str(evaluator or ""), str(gap or "")],
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def ensure_context_schema(conn=None) -> None:
    if conn is not None:
        conn.executescript(CONTEXT_SCHEMA)
        return
    with feedback._conn() as db:
        db.executescript(CONTEXT_SCHEMA)


def record_contextual_gap(
    ref: str,
    evaluator: str,
    gap: str,
    *,
    subject_id: str,
    spec_hash: str,
    polarity: str = "positive",
    metadata: dict | None = None,
    ts: int | None = None,
) -> str:
    """Record a normal gap plus canonical identity in the additive sidecar."""
    timestamp = int(time.time()) if ts is None else int(ts)
    if not subject_id or not spec_hash:
        raise ValueError("contextual evidence gap requires subject_id and spec_hash")
    if polarity not in {"positive", "counterexample"}:
        raise ValueError("invalid evidence gap polarity")
    key = _gap_key(timestamp, ref, evaluator, gap)
    with feedback._conn() as conn:
        ensure_context_schema(conn)
        conn.execute(
            "INSERT INTO evidence_gaps VALUES (?,?,?,?,?)",
            (timestamp, ref, evaluator, gap, "open"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO evidence_gap_context VALUES (?,?,?,?,?,?,?,?,?)",
            (
                key,
                timestamp,
                ref,
                evaluator,
                gap,
                subject_id,
                spec_hash,
                polarity,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
    return key


def normalize_gap_semantics(gap: str) -> tuple[str, ...]:
    """Normalize bounded proof-gap language without an LLM or embedding call."""
    text = str(gap or "").lower().replace("deliberate-break", "deliberate_break")
    text = text.replace("negative control", "deliberate_break")
    text = text.replace("command line", "command-line")
    tokens = []
    for token in re.findall(r"[a-z0-9_./:-]+", text):
        normalized = SEMANTIC_SYNONYMS.get(token, token)
        if normalized not in SEMANTIC_STOPWORDS:
            tokens.append(normalized)
    return tuple(sorted(set(tokens)))


def semantic_cluster_name(gap: str) -> str | None:
    tokens = set(normalize_gap_semantics(gap))
    if "named_test" in tokens and tokens & {"result", "run", "evidence", "deliberate_break"}:
        return "named_test_smoke_deliberate_break"
    rule = _rule_for_gap(gap)
    return str(rule["name"]) if rule else None


def cluster_gap_rows(
    rows: list[dict],
    *,
    min_distinct_subjects: int = DEFAULT_MIN_DISTINCT_SUBJECTS,
    max_counterexample_ratio: float = DEFAULT_MAX_COUNTEREXAMPLE_RATIO,
    now: int | None = None,
    candidate_ttl_days: int = DEFAULT_CANDIDATE_TTL_DAYS,
) -> list[dict]:
    """Create candidate-only semantic clusters weighted by subject/spec identity."""
    current = int(time.time()) if now is None else int(now)
    clusters: dict[str, list[dict]] = {}
    for row in rows:
        name = semantic_cluster_name(str(row.get("gap") or ""))
        if name:
            clusters.setdefault(name, []).append(dict(row))
    candidates = []
    for name, grouped in sorted(clusters.items()):
        positives = [row for row in grouped if row.get("polarity", "positive") != "counterexample"]
        counterexamples = [row for row in grouped if row.get("polarity") == "counterexample"]
        observations: dict[tuple[str, str], list[dict]] = {}
        unresolved = 0
        for row in positives:
            subject_id = str(row.get("subject_id") or "").strip()
            spec_hash = str(row.get("spec_hash") or "").strip()
            if not subject_id or not spec_hash:
                unresolved += 1
                continue
            observations.setdefault((subject_id, spec_hash), []).append(row)
        independent_subjects = sorted({subject for subject, _spec in observations})
        independent_specs = sorted({spec for _subject, spec in observations})
        # Independence is bounded by both canonical axes. Multiple specs or
        # evaluator rows for one subject remain one effective observation.
        effective_count = float(min(len(independent_subjects), len(independent_specs)))
        counterexample_identities = {
            (str(row.get("subject_id") or ""), str(row.get("spec_hash") or ""))
            for row in counterexamples
            if row.get("subject_id") and row.get("spec_hash")
        }
        evidence_identities = set(observations) | counterexample_identities
        negative_ratio = len(counterexample_identities) / max(1, len(evidence_identities))
        if effective_count < min_distinct_subjects or negative_ratio > max_counterexample_ratio:
            continue
        last_evidence_at = max(int(row.get("ts") or current) for row in grouped)
        core = {
            "name": name,
            "semantic_tokens": sorted(
                {
                    token
                    for row in grouped
                    for token in normalize_gap_semantics(row.get("gap") or "")
                }
            ),
            "independent_subjects": independent_subjects,
            "independent_specs": independent_specs,
            "effective_subject_count": effective_count,
            "evaluator_count": len(
                {str(row.get("evaluator")) for row in grouped if row.get("evaluator")}
            ),
            "raw_row_count": len(grouped),
            "unresolved_identity_rows": unresolved,
            "negative_ratio": negative_ratio,
            "examples": list(dict.fromkeys(str(row.get("gap") or "") for row in grouped))[:5],
            "counterexamples": [
                {
                    "subject_id": row.get("subject_id"),
                    "spec_hash": row.get("spec_hash"),
                    "evaluator": row.get("evaluator"),
                    "gap": row.get("gap"),
                    "ref": row.get("ref"),
                }
                for row in counterexamples
            ],
            "lifecycle": {
                "state": "clustered",
                "candidate_only": True,
                "promotion_allowed": False,
                "expires_at": last_evidence_at + candidate_ttl_days * 86400,
                "rollback": {
                    "action": "retire_candidate",
                    "reason": "no influence, expiry, or recurring harm",
                },
            },
        }
        core["candidate_id"] = _gap_key(
            last_evidence_at,
            name,
            str(len(observations)),
            json.dumps(core["semantic_tokens"]),
        )
        candidates.append(core)
    return candidates


def _rule_for_gap(gap: str) -> dict | None:
    text = " ".join(str(gap or "").lower().split())
    for rule in RULES:
        if any(re.search(pattern, text) for pattern in rule["patterns"]):
            return rule
    return None


def _gap_rows(window_days: int, *, now: int | None = None) -> list[dict]:
    since = _since(window_days, now=now)
    with feedback._conn() as c:
        ensure_context_schema(c)
        rows = c.execute(
            "SELECT ts, ref, evaluator, gap, status FROM evidence_gaps WHERE ts>=? "
            "ORDER BY ts DESC",
            (since,),
        ).fetchall()
        context_rows = c.execute(
            "SELECT ts,ref,evaluator,gap,subject_id,spec_hash,polarity "
            "FROM evidence_gap_context WHERE ts>=?",
            (since,),
        ).fetchall()
    contexts = {
        (ts, ref, evaluator, gap): {
            "subject_id": subject_id,
            "spec_hash": spec_hash,
            "polarity": polarity,
        }
        for ts, ref, evaluator, gap, subject_id, spec_hash, polarity in context_rows
    }
    return [
        {
            "ts": ts,
            "ref": ref,
            "evaluator": evaluator,
            "gap": gap,
            "status": status,
            **contexts.get((ts, ref, evaluator, gap), {}),
        }
        for ts, ref, evaluator, gap, status in rows
    ]


def _type_counts() -> dict:
    with feedback._conn() as c:
        counts = c.execute(
            "SELECT status, COUNT(*) FROM evidence_types GROUP BY status ORDER BY status"
        ).fetchall()
        active = c.execute(
            "SELECT name, added_ts, influence, rationale FROM evidence_types "
            "WHERE status='active' ORDER BY name"
        ).fetchall()
        retired = c.execute(
            "SELECT name, added_ts, influence, rationale FROM evidence_types "
            "WHERE status='retired' ORDER BY name"
        ).fetchall()
    return {
        "counts_by_status": {status: count for status, count in counts},
        "active": [
            {
                "name": name,
                "added_ts": added_ts,
                "influence": influence,
                "rationale": rationale,
            }
            for name, added_ts, influence, rationale in active
        ],
        "retired": [
            {
                "name": name,
                "added_ts": added_ts,
                "influence": influence,
                "rationale": rationale,
            }
            for name, added_ts, influence, rationale in retired
        ],
    }


def active_type_review(
    *,
    prune_after_days: int = DEFAULT_PRUNE_AFTER_DAYS,
    min_influence: int = DEFAULT_MIN_INFLUENCE,
    now: int | None = None,
) -> dict:
    now = int(time.time()) if now is None else now
    type_info = _type_counts()
    rows = []
    for row in type_info["active"]:
        added_ts = int(row.get("added_ts") or now)
        age_days = max(0, (now - added_ts) // 86400)
        influence = int(row.get("influence") or 0)
        if influence >= min_influence:
            status = "used"
            recommendation = "keep; cited by evaluator verdicts"
        elif age_days >= prune_after_days:
            status = "prune_candidate"
            recommendation = "consider retiring if the type is still uncited after review"
        else:
            status = "monitoring"
            recommendation = "too new to prune; wait for evaluator traffic"
        rows.append(
            {
                "name": row["name"],
                "age_days": age_days,
                "influence": influence,
                "status": status,
                "recommendation": recommendation,
                "rationale": row.get("rationale") or "",
            }
        )
    prune_candidates = [row for row in rows if row["status"] == "prune_candidate"]
    return {
        "prune_after_days": prune_after_days,
        "min_influence": min_influence,
        "active_count": len(rows),
        "prune_candidate_count": len(prune_candidates),
        "active": sorted(rows, key=lambda row: (row["status"], row["name"])),
        "recommendation": (
            "Review prune candidates before retiring them."
            if prune_candidates
            else "No active evidence type is prune-ready."
        ),
    }


def clustered_proposals(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_recurrence: int = DEFAULT_MIN_RECURRENCE,
    now: int | None = None,
) -> list[dict]:
    active_names = {row["name"] for row in _type_counts()["active"]}
    clusters: dict[str, dict] = {}
    for row in _gap_rows(window_days, now=now):
        if row["status"] != "open":
            continue
        rule = _rule_for_gap(row["gap"])
        if not rule:
            continue
        if rule["name"] in active_names:
            continue
        item = clusters.setdefault(
            rule["name"],
            {
                "name": rule["name"],
                "rationale": rule["rationale"],
                "recurrence": 0,
                "refs": set(),
                "evaluators": set(),
                "example_gaps": [],
            },
        )
        item["recurrence"] += 1
        if row["ref"]:
            item["refs"].add(row["ref"])
        if row["evaluator"]:
            item["evaluators"].add(row["evaluator"])
        if len(item["example_gaps"]) < 5 and row["gap"] not in item["example_gaps"]:
            item["example_gaps"].append(row["gap"])

    out = []
    for item in clusters.values():
        if item["recurrence"] < min_recurrence:
            continue
        out.append(
            {
                "name": item["name"],
                "recurrence": item["recurrence"],
                "ref_count": len(item["refs"]),
                "evaluator_count": len(item["evaluators"]),
                "rationale": item["rationale"],
                "example_gaps": item["example_gaps"],
                "approval_command": (
                    f"python3 evidence_schema.py --apply {item['name']} "
                    f"--confirm-type {item['name']}"
                ),
            }
        )
    return sorted(out, key=lambda row: (-row["recurrence"], row["name"]))


def build_report(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_recurrence: int = DEFAULT_MIN_RECURRENCE,
    prune_after_days: int = DEFAULT_PRUNE_AFTER_DAYS,
    min_influence: int = DEFAULT_MIN_INFLUENCE,
    now: int | None = None,
) -> dict:
    rows = _gap_rows(window_days, now=now)
    open_rows = [row for row in rows if row["status"] == "open"]
    type_info = _type_counts()
    proposals = clustered_proposals(window_days=window_days, min_recurrence=min_recurrence, now=now)
    semantic_candidates = cluster_gap_rows(
        open_rows,
        min_distinct_subjects=min_recurrence,
        now=now,
    )
    ready_candidates = bool(proposals or semantic_candidates)
    active_count = type_info["counts_by_status"].get("active", 0)
    review = active_type_review(
        prune_after_days=prune_after_days,
        min_influence=min_influence,
        now=now,
    )
    if active_count and ready_candidates:
        status = "active_with_proposals"
        recommendation = (
            "Active evidence types exist and additional recurring gap classes are ready for review."
        )
    elif active_count:
        status = "active"
        recommendation = "Active evidence types exist; monitor influence and prune unused types."
    elif ready_candidates:
        status = "approval_ready"
        recommendation = (
            "Recurring evidence-gap classes are ready for explicit candidate review; "
            "semantic candidates remain non-promotable until a compiled shadow contract is approved."
        )
    elif open_rows:
        status = "waiting_for_recurrence"
        recommendation = (
            "Evidence gaps exist, but no clustered proposal has reached the recurrence threshold."
        )
    else:
        status = "waiting_for_gaps"
        recommendation = "Run real evaluations that emit evidence_gaps."
    return {
        "generated_at": now or int(time.time()),
        "read_only": True,
        "window_days": window_days,
        "min_recurrence": min_recurrence,
        "status": status,
        "open_gap_rows": len(open_rows),
        "total_gap_rows": len(rows),
        "clustered_proposal_count": len(proposals),
        "clustered_proposals": proposals,
        "semantic_candidate_count": len(semantic_candidates),
        "semantic_candidates": semantic_candidates,
        "evidence_types": type_info,
        "active_type_review": review,
        "recommendation": recommendation,
    }


def apply_candidate(
    name: str,
    *,
    confirm_type: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_recurrence: int = DEFAULT_MIN_RECURRENCE,
    now: int | None = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("candidate evidence type name is required")
    if confirm_type != name:
        raise ValueError("--confirm-type must exactly match the candidate name")
    proposal = next(
        (
            item
            for item in clustered_proposals(
                window_days=window_days, min_recurrence=min_recurrence, now=now
            )
            if item["name"] == name
        ),
        None,
    )
    if not proposal:
        return {
            "name": name,
            "applied": False,
            "blocked_reason": "candidate is not recurrence-ready",
            "min_recurrence": min_recurrence,
            "window_days": window_days,
        }

    since = _since(window_days, now=now)
    matched_gaps = [
        row["gap"]
        for row in _gap_rows(window_days, now=now)
        if row["status"] == "open" and (_rule_for_gap(row["gap"]) or {}).get("name") == name
    ]
    with feedback._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute("SELECT status FROM evidence_types WHERE name=?", (name,)).fetchone()
        if existing:
            c.execute(
                "UPDATE evidence_types SET status='active', rationale=? WHERE name=?",
                (proposal["rationale"], name),
            )
        else:
            c.execute(
                "INSERT INTO evidence_types VALUES (?,?,?,?,?)",
                (name, now or int(time.time()), 0, "active", proposal["rationale"]),
            )
        marked = 0
        for gap in sorted(set(matched_gaps)):
            cur = c.execute(
                "UPDATE evidence_gaps SET status='approved' "
                "WHERE status='open' AND ts>=? AND gap=?",
                (since, gap),
            )
            marked += cur.rowcount
    return {
        "name": name,
        "applied": True,
        "gaps_marked": marked,
        "recurrence": proposal["recurrence"],
        "rationale": proposal["rationale"],
    }


def _selftest() -> None:
    import tempfile

    old_db = feedback.DB_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="evidence-schema-") as tmp:
            feedback.DB_PATH = Path(tmp) / "feedback.db"
            now = 1_800_000_000
            for idx, gap in enumerate(
                [
                    "No post-click accessibility tree for the results table.",
                    "Post action state after pressing Run was not captured.",
                    "Button click behavior on submit not observed.",
                    "Upload validation after CSV selection not exercised.",
                    "Document upload behavior was not exercised.",
                    "CSV upload flow error state not captured.",
                ]
            ):
                with feedback._conn() as c:
                    c.execute(
                        "INSERT INTO evidence_gaps VALUES (?,?,?,?,?)",
                        (now - idx, f"ref-{idx}", "judge", gap, "open"),
                    )
            report = build_report(window_days=30, min_recurrence=3, now=now)
            names = {item["name"] for item in report["clustered_proposals"]}
            assert "post_action_state_capture" in names, report
            assert "upload_flow_evidence" in names, report
            assert report["status"] == "approval_ready", report
            try:
                blocked = apply_candidate(
                    "post_action_state_capture",
                    confirm_type="wrong",
                    window_days=30,
                    min_recurrence=3,
                    now=now,
                )
            except ValueError as exc:
                assert "--confirm-type" in str(exc), exc
            else:
                raise AssertionError(f"expected confirm failure, got {blocked}")
            applied = apply_candidate(
                "post_action_state_capture",
                confirm_type="post_action_state_capture",
                window_days=30,
                min_recurrence=3,
                now=now,
            )
            assert applied["applied"] is True and applied["gaps_marked"] == 3, applied
            with feedback._conn() as c:
                active = c.execute(
                    "SELECT COUNT(*) FROM evidence_types WHERE status='active'"
                ).fetchone()[0]
                approved = c.execute(
                    "SELECT COUNT(*) FROM evidence_gaps WHERE status='approved'"
                ).fetchone()[0]
            assert active == 1 and approved == 3, (active, approved)
            for idx, gap in enumerate(
                [
                    "Post-click state after retry was not captured.",
                    "After pressing submit, the action result was not observed.",
                    "Button click behavior after save was not exercised.",
                ]
            ):
                with feedback._conn() as c:
                    c.execute(
                        "INSERT INTO evidence_gaps VALUES (?,?,?,?,?)",
                        (now - 100 - idx, f"active-ref-{idx}", "judge", gap, "open"),
                    )
            after = build_report(window_days=30, min_recurrence=3, now=now)
            after_names = {item["name"] for item in after["clustered_proposals"]}
            assert "post_action_state_capture" not in after_names, after
            assert "upload_flow_evidence" in after_names, after
            assert after["status"] == "active_with_proposals", after
            assert after["active_type_review"]["active_count"] == 1, after
            assert after["active_type_review"]["active"][0]["status"] == "monitoring", after
            with feedback._conn() as c:
                c.execute(
                    "UPDATE evidence_types SET added_ts=? WHERE name=?",
                    (now - 31 * 86400, "post_action_state_capture"),
                )
            stale = active_type_review(prune_after_days=30, now=now)
            assert stale["prune_candidate_count"] == 1, stale
            cited = feedback.record_evidence_type_citations(["post_action_state_capture"])
            assert cited == ["post_action_state_capture"], cited
            used = active_type_review(prune_after_days=30, now=now)
            assert used["active"][0]["status"] == "used", used
    finally:
        feedback.DB_PATH = old_db
    print("evidence_schema.py selftest: OK (cluster proposals, guarded approval)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cluster evidence gaps into evidence-type approval candidates."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--min-recurrence", type=int, default=DEFAULT_MIN_RECURRENCE)
    parser.add_argument("--prune-after-days", type=int, default=DEFAULT_PRUNE_AFTER_DAYS)
    parser.add_argument("--min-influence", type=int, default=DEFAULT_MIN_INFLUENCE)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--apply", default="", help="approve a clustered candidate by name")
    parser.add_argument("--confirm-type", default="")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.apply:
        result = apply_candidate(
            args.apply,
            confirm_type=args.confirm_type,
            window_days=args.window_days,
            min_recurrence=args.min_recurrence,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("applied") else 1

    report = build_report(
        window_days=args.window_days,
        min_recurrence=args.min_recurrence,
        prune_after_days=args.prune_after_days,
        min_influence=args.min_influence,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"evidence_schema: status={report['status']} "
            f"open_gaps={report['open_gap_rows']} "
            f"clustered_proposals={report['clustered_proposal_count']} "
            f"active_types={report['evidence_types']['counts_by_status'].get('active', 0)}"
        )
        for item in report["clustered_proposals"][:10]:
            print(
                f"  {item['name']}: recurrence={item['recurrence']} "
                f"refs={item['ref_count']} evaluators={item['evaluator_count']}"
            )
        review = report["active_type_review"]
        for item in review["active"][:10]:
            print(
                f"  active {item['name']}: status={item['status']} "
                f"influence={item['influence']} age_days={item['age_days']}"
            )
        print(f"  recommendation: {report['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
