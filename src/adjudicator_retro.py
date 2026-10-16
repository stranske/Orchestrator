#!/usr/bin/env python3
"""Replay persisted verifier disputes through the existing shadow adjudicator.

Only role-run evidence and the report are written. Outcomes remain read-only;
missing verifier, diff, gate or later durability evidence stays unmeasured.

``--collect-case`` is intentionally separate from replay: it reads complete
UTF-8 source blobs from one locally available evaluated commit and writes a
separate collection record. Explicit git-path: acceptance locations reuse the
same bounded evaluated-revision blob reader. Complete means only complete for
the supplied inventory; exhaustiveness and acceptance semantics remain unverified.
It cannot fetch remotes, adjudicate, dispatch,
write Brain/outcomes/role records, or claim semantic acceptance passed.

Usage: ``python src/adjudicator_retro.py --collect-case CASE_ID --report
saved.json --output collection.json --repository /local/git/checkout``.  The
saved row must contain ``collection_requirements`` with ``source_paths`` (a
list of repository-relative regular files) and ``acceptance`` (a list of
``{"criterion": "...", "location": "..."}`` objects). An empty acceptance
list is explicit evidence that the acceptance inventory remains unresolved,
so collection succeeds with ``complete: false`` and prints its gaps; this is
an exit-0 collection result, not an acceptance result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import feedback
import fleet_shapes
import roles
import verifier_evidence

# This collector deliberately has no GitHub, Brain, role-routing, or artifact
# transport dependency.  It is a local, immutable-commit evidence boundary.
MAX_COLLECTION_SOURCE_BYTES = 1_048_576
MAX_DIFF_PATCH_BYTES = 256_000
# One generated line (a bookkeeping file's growing note) can be most of a patch, and the patch is
# then clipped as a whole. A diff line over this bound is shortened to its head instead, and listed.
MAX_DIFF_LINE_BYTES = 4096
DIFF_LINE_KEEP_BYTES = 1024
# The one dispatch budget for every adjudicator call this module makes: the retrospective loop,
# the judge-collected-case seam and the --timeout default all read it. It equals the role's own
# default (`roles.run_adjudicator_agent`, parameter `timeout`); a test pins the two together so
# they cannot drift apart again.
DISPATCH_TIMEOUT_S = 600
NATIVE_JOURNAL_APPLICATION_ID = 1313097550


def _valid_native_receipt(result: object, record: dict | None = None) -> bool:
    """A journal-owned binary receipt; malformed receipts never consume later revisions."""
    if not isinstance(result, dict) or result.get("errors"):
        return False
    proposal = result.get("proposal")
    if not isinstance(proposal, dict):
        return False
    try:
        if roles.ROLE_REGISTRY["adjudicator"].validate(proposal):
            return False
    except (TypeError, ValueError, KeyError):
        return False
    if proposal.get("decision") not in {"uphold_blocker", "reject_blocker"}:
        return False
    if not result.get("backend_run_id"):
        return False
    if record is not None and (
        result.get("backend_run_id") != record.get("backend_run_id")
        or result.get("raw_output") != record.get("raw_response")
    ):
        return False
    return True


class NativeAttemptJournal:
    """Private, append-only receipt rail for one native collected-case retry.

    Report JSON is deliberately not a source of authority.  This SQLite file is
    owned by the native rail and stores the reservation before dispatch and the
    raw provider response before any parsing or secondary side effect.  It does
    not solve the provider crash boundary: a reserved attempt without a raw
    receipt remains UNKNOWN and is never automatically redispatched.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables and app_id != NATIVE_JOURNAL_APPLICATION_ID:
                raise ValueError("native journal refuses a non-native SQLite database")
            conn.execute(f"PRAGMA application_id={NATIVE_JOURNAL_APPLICATION_ID}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS native_attempts ("
                "attempt_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, material_revision TEXT NOT NULL, "
                "case_digest TEXT NOT NULL, state TEXT NOT NULL, raw_response TEXT, backend_run_id TEXT, "
                "backend_usage TEXT, backend_error TEXT, validated_json TEXT, created_at INTEGER NOT NULL, "
                "updated_at INTEGER NOT NULL, report_sha256 TEXT NOT NULL, UNIQUE(case_id, material_revision))"
            )

    @classmethod
    def open_existing(cls, path: Path) -> NativeAttemptJournal:
        """Open an existing native journal without schema creation or repair."""
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != NATIVE_JOURNAL_APPLICATION_ID:
                raise ValueError("native cohort requires a native journal")
            conn.execute("SELECT attempt_id FROM native_attempts LIMIT 1")
        instance = cls.__new__(cls)
        instance.path = path
        return instance

    def reserve(
        self,
        case_id: str,
        revision: str,
        digest: str,
        report_sha256: str,
        *,
        await_start: bool = False,
    ) -> dict:
        """Reserve once across processes; any existing reservation blocks spending."""
        now = int(time.time())
        attempt_id = uuid.uuid4().hex
        state = "reserved" if await_start else "in_flight"
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT attempt_id,state,case_digest FROM native_attempts "
                "WHERE case_id=? AND (material_revision=? OR state IN ('reserved','in_flight','raw_persisted')) "
                "ORDER BY CASE WHEN state IN ('reserved','in_flight','raw_persisted') THEN 0 ELSE 1 END",
                (case_id, revision),
            ).fetchone()
            if current is not None:
                return {
                    "reserved": False,
                    "attempt_id": current[0],
                    "state": current[1],
                    "case_digest": current[2],
                }
            prior = conn.execute(
                "SELECT validated_json,backend_run_id,raw_response FROM native_attempts "
                "WHERE case_id=? AND state='validated'",
                (case_id,),
            ).fetchall()
            for validated_json, backend_run_id, raw_response in prior:
                try:
                    result = json.loads(validated_json)
                except (TypeError, ValueError):
                    continue
                if _valid_native_receipt(
                    result, {"backend_run_id": backend_run_id, "raw_response": raw_response}
                ):
                    return {"reserved": False, "attempt_id": None, "state": "validated_binary"}
            conn.execute(
                "INSERT INTO native_attempts(attempt_id,case_id,material_revision,case_digest,state,created_at,updated_at,report_sha256) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, case_id, revision, digest, state, now, now, report_sha256),
            )
        return {"reserved": True, "attempt_id": attempt_id, "state": state}

    def begin_invocation(self, attempt_id: str, response: dict) -> None:
        """Commit the UNKNOWN spend boundary before the dispatcher may start a process."""
        backend_id = response.get("run_id")
        if not isinstance(backend_id, str) or not backend_id:
            raise ValueError("native invocation requires its backend identity")
        with sqlite3.connect(self.path, timeout=30) as conn:
            changed = conn.execute(
                "UPDATE native_attempts SET state='in_flight',backend_run_id=?,updated_at=? "
                "WHERE attempt_id=? AND state='reserved'",
                (backend_id, int(time.time()), attempt_id),
            ).rowcount
            if changed != 1:
                raise ValueError("native attempt is not a pre-invocation reservation")

    def release_unstarted(self, attempt_id: str) -> bool:
        """Release only a producer-proven reservation that never crossed the start boundary."""
        with sqlite3.connect(self.path, timeout=30) as conn:
            changed = conn.execute(
                "DELETE FROM native_attempts WHERE attempt_id=? AND state='reserved'", (attempt_id,)
            ).rowcount
        return changed == 1

    def persist_raw(self, attempt_id: str, response: dict) -> None:
        """Commit the unparsed backend receipt before callers may inspect it."""
        if not isinstance(response, dict):
            raise ValueError("native backend receipt must be an object")
        raw = response.get("output", "")
        if not isinstance(raw, str):
            raw = json.dumps(raw, sort_keys=True)
        now = int(time.time())
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE native_attempts SET state='raw_persisted',raw_response=?,backend_run_id=?,"
                "backend_usage=?,backend_error=?,updated_at=? WHERE attempt_id=? AND state='in_flight'",
                (
                    raw,
                    response.get("run_id"),
                    json.dumps(response.get("usage") or {}, sort_keys=True),
                    str(response.get("error") or ""),
                    now,
                    attempt_id,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("native attempt is not an in-flight reservation")

    def validate(self, attempt_id: str, result: dict) -> None:
        """Record post-parse validation without ever reopening spend eligibility."""
        with sqlite3.connect(self.path, timeout=30) as conn:
            changed = conn.execute(
                "UPDATE native_attempts SET state=?,validated_json=?,updated_at=? "
                "WHERE attempt_id=? AND state='raw_persisted'",
                ("validated", json.dumps(result, sort_keys=True), int(time.time()), attempt_id),
            ).rowcount
            if changed != 1:
                raise ValueError("native validation requires exactly one raw persisted receipt")

    def verified(self, case_id: str, revision: str) -> dict | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM native_attempts WHERE case_id=? AND material_revision=? "
                "AND state='validated'",
                (case_id, revision),
            ).fetchone()
        return dict(row) if row is not None else None

    def has_binary_decision(self, case_id: str) -> bool:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT validated_json FROM native_attempts WHERE case_id=? AND state='validated'",
                (case_id,),
            ).fetchall()
        for (value,) in rows:
            try:
                result = json.loads(value)
            except (TypeError, ValueError):
                continue
            if _valid_native_receipt(result):
                return True
        return False


def _native_substantive_revision(case: roles.CollectedAdjudicationCase) -> str:
    """A spending key excluding administrative ownership and transport prose."""
    packet = roles._collected_snapshot(case)
    ground = packet.get("ground_truth_evidence", {})
    if not isinstance(ground, dict):
        raise ValueError("collected case lacks a material evidence revision")
    collection = ground.get("collected_evidence")
    mappings = ground.get("criterion_mapping")
    if not isinstance(collection, dict) or not isinstance(mappings, list):
        raise ValueError("collected case lacks a material evidence revision")
    # The collector's broader identity intentionally binds owned UNKNOWN wording.
    # Spending must not: an owner, next-action, location, or presentation-order
    # correction cannot purchase another provider attempt.  Keep the UNKNOWN ref
    # itself because adding/removing a required fact changes the evidence scope.
    material = {
        "target": packet.get("target"),
        "finding": packet.get("disputed_finding"),
        "collection": json.loads(json.dumps(collection)),
        "diff": sorted(
            ground.get("diff_summary", []), key=lambda item: json.dumps(item, sort_keys=True)
        ),
        "criteria": sorted(
            [
                {
                    "criterion": mapping.get("criterion"),
                    "evidence_refs": sorted(mapping.get("evidence_locations", [])),
                    "unknown_refs": sorted(
                        item["evidence_ref"]
                        for item in mapping.get("unknowns", [])
                        if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
                    ),
                }
                for mapping in mappings
                if isinstance(mapping, dict)
            ],
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }
    material["collection"].pop("repository", None)
    requirements = material["collection"].get("requirements")
    if isinstance(requirements, dict) and isinstance(requirements.get("source_paths"), list):
        requirements["source_paths"].sort()
    for key in ("sources", "before_sources", "supporting_sources", "acceptance_artifacts"):
        if isinstance(material["collection"].get(key), list):
            material["collection"][key].sort(key=lambda item: json.dumps(item, sort_keys=True))
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _validate_complete_history(row: dict) -> None:
    """Reject malformed or duplicate retained attempt identities before effects."""
    attempts = list(_attempt_rows([row]))
    identities = [item.get("backend_run_id") for item in attempts if item.get("backend_run_id")]
    if len(identities) != len(set(identities)):
        raise ValueError("saved attempt history has duplicate backend identities")


def _validate_original_report(rows: list[dict]) -> None:
    """Eagerly validate all originals before a reservation or backend touch."""
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str):
            raise ValueError("saved report rows require stable case IDs")
        if row["case_id"] in ids:
            raise ValueError("saved report has duplicate case IDs")
        ids.add(row["case_id"])
        _validate_complete_history(row)
        keys = ("run_id", "verifier_verdict", "adjudicated_verdict")
        if all(key in row for key in keys) and _case_id(row) != row["case_id"]:
            raise ValueError("saved report case ID does not match immutable identity")


def admit_collected_retry(
    *,
    original_report: Path,
    output: Path,
    case_id: str,
    case: roles.CollectedAdjudicationCase,
    journal: NativeAttemptJournal,
    dispatch: bool = False,
    backend: str | None = None,
    cwd: str = ".",
    timeout: int = DISPATCH_TIMEOUT_S,
    expected_report_sha256: str | None = None,
) -> dict:
    """Run one existing original case through the native receipt rail.

    ``original_report`` is read-only and ``output`` must be a fresh derived
    projection.  A current opaque collected capability is required; serialized
    packets and persisted-admission flags cannot enter this function.
    """
    if not isinstance(case, roles.CollectedAdjudicationCase):
        raise TypeError("native admission requires a freshly issued collected case")
    if output.exists() or output.is_symlink() or output.resolve() == original_report.resolve():
        raise ValueError("native projection output must be new and separate")
    if journal.path.resolve() in {original_report.resolve(), output.resolve()}:
        raise ValueError("native journal must be separate from report and projection")
    report_bytes = original_report.read_bytes()
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    if expected_report_sha256 is not None and expected_report_sha256 != report_hash:
        raise ValueError("original report changed before native projection")
    saved = json.loads(report_bytes)
    if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
        raise ValueError("saved report must contain a rows list")
    matches = [
        row for row in saved["rows"] if isinstance(row, dict) and row.get("case_id") == case_id
    ]
    if len(matches) != 1:
        raise ValueError("saved report must contain exactly one original case")
    _validate_original_report(saved["rows"])
    original = matches[0]
    if original.get("decision") not in {None, "needs_more_evidence"}:
        raise ValueError("native admission only resumes an original abstention")
    snapshot = roles._collected_snapshot(case)  # rechecks opaque issuance/digest
    qualification = snapshot.get("ground_truth_evidence", {}).get("qualification", {})
    original_packet = original.get("packet") or {}
    fresh_ground = snapshot.get("ground_truth_evidence") or {}
    original_ground = original_packet.get("ground_truth_evidence") or {}
    anchors = ("head_sha", "merge_sha", "diff_summary", "gate_runs")
    if (
        qualification.get("case_id") != case_id
        or snapshot.get("target") != original.get("target")
        or snapshot.get("disputed_finding") != original_packet.get("disputed_finding")
        or any(fresh_ground.get(key) != original_ground.get(key) for key in anchors)
    ):
        raise ValueError("collected capability does not bind the original case")
    revision = _native_substantive_revision(case)
    if journal.has_binary_decision(case_id):
        raise ValueError("native case already has a validated binary decision")
    if not dispatch:
        return {
            "source_report_sha256": report_hash,
            "original_case_id": case_id,
            "material_evidence_revision": revision,
            "native_admission": "prepared_no_dispatch",
        }
    reservation = journal.reserve(case_id, revision, case._digest, report_hash, await_start=True)
    if not reservation["reserved"]:
        # An in-flight/malformed/validated prior attempt is UNKNOWN/consumed, never
        # a reason to pay again.  The caller may inspect the journal manually.
        raise ValueError("native revision already reserved: " + reservation["state"])
    attempt_id = reservation["attempt_id"]
    try:
        result = roles.run_adjudicator_agent(
            case=case,
            backend=backend,
            dispatch=True,
            cwd=cwd,
            timeout=timeout,
            lifecycle_start_hook=lambda response: journal.begin_invocation(attempt_id, response),
            lifecycle_hook=lambda response: journal.persist_raw(attempt_id, response),
        )
    except BaseException:
        # A crash after the trusted start callback cannot match this DELETE,
        # even if no raw receipt reached the journal. Never infer no spend from
        # a missing response or from an arbitrary exception after invocation.
        journal.release_unstarted(attempt_id)
        raise
    if journal.release_unstarted(attempt_id):
        raise ValueError(
            "native dispatch made no provider invocation: " + "; ".join(result.get("errors") or [])
        )
    journal.validate(attempt_id, result)
    # Keep original evidence byte-for-byte untouched.  This is a projection only;
    # it remains ungraded unless a later trusted evaluator establishes truth.
    projected_rows = json.loads(json.dumps(saved["rows"]))
    selected = next(row for row in projected_rows if row["case_id"] == case_id)
    selected["native_projection"] = {
        "attempt_id": attempt_id,
        "material_evidence_revision": revision,
        "admission": "trusted_journal_pending_later_truth",
        "result": result,
    }
    projection = {
        "source_report_sha256": report_hash,
        "original_case_id": case_id,
        "material_evidence_revision": revision,
        "native_attempt_id": attempt_id,
        "native_admission": "trusted_journal_pending_later_truth",
        "original_paid_attempts": [
            _attempt_snapshot(item)
            for item in _attempt_rows([original])
            if item.get("backend_run_id")
        ],
        "result": result,
        "rows": projected_rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(projection, indent=2) + "\n")
    return projection


def refresh_native_projection(
    *,
    original_report: Path,
    output: Path,
    case_id: str,
    case: roles.CollectedAdjudicationCase,
    journal: NativeAttemptJournal,
    db: Path | None = None,
    truth_reader=None,
) -> dict:
    """Rebuild a native projection after restart from trusted receipts only.

    This is intentionally separate from ``summarize``: generic report JSON
    continues to floor qualified packets.  A native grade needs a fresh opaque
    collector capability, a matching trusted journal record, a validated binary
    proposal, and independently refreshed later truth.
    """
    if not isinstance(case, roles.CollectedAdjudicationCase):
        raise TypeError("native refresh requires a freshly issued collected case")
    if output.exists() or output.is_symlink() or output.resolve() == original_report.resolve():
        raise ValueError("native projection output must be new and separate")
    if journal.path.resolve() in {original_report.resolve(), output.resolve()}:
        raise ValueError("native journal must be separate from report and projection")
    report_bytes = original_report.read_bytes()
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    saved = json.loads(report_bytes)
    if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
        raise ValueError("saved report must contain a rows list")
    _validate_original_report(saved["rows"])
    originals = [row for row in saved["rows"] if row.get("case_id") == case_id]
    if len(originals) != 1:
        raise ValueError("saved report must contain exactly one original case")
    snapshot = roles._collected_snapshot(case)
    qualification = snapshot.get("ground_truth_evidence", {}).get("qualification", {})
    original_packet = originals[0].get("packet") or {}
    fresh_ground = snapshot.get("ground_truth_evidence") or {}
    original_ground = original_packet.get("ground_truth_evidence") or {}
    anchors = ("head_sha", "merge_sha", "diff_summary", "gate_runs")
    if (
        qualification.get("case_id") != case_id
        or snapshot.get("target") != originals[0].get("target")
        or snapshot.get("disputed_finding") != original_packet.get("disputed_finding")
        or any(fresh_ground.get(key) != original_ground.get(key) for key in anchors)
    ):
        raise ValueError("fresh collected capability does not bind the original case")
    revision = _native_substantive_revision(case)
    # Match the spend identity: checkout paths and UNKNOWN owner wording are
    # administrative metadata, while the original report and receipt remain bound.
    record = journal.verified(case_id, revision)
    if record is None or record.get("report_sha256") != report_hash:
        raise ValueError("no trusted native admission for this original report and fresh case")
    result = json.loads(record["validated_json"])
    # The spending key intentionally ignores administrative changes. Receipt
    # reuse also binds the complete contract actually supplied to the provider.
    # Read it from the journal-owned result, never a saved projection marker.
    receipt_case = result.get("case") if isinstance(result, dict) else None
    receipt_ground = (
        receipt_case.get("ground_truth_evidence") if isinstance(receipt_case, dict) else None
    )
    provenance = fresh_ground.get("criterion_provenance")
    if (
        not isinstance(provenance, dict)
        or not isinstance(receipt_ground, dict)
        or receipt_ground.get("criterion_provenance") != provenance
    ):
        raise ValueError("native receipt does not bind the complete task contract")
    proposal = result.get("proposal") if isinstance(result, dict) else None
    if not _valid_native_receipt(result, record):
        proposal = None
    decision = proposal.get("decision") if isinstance(proposal, dict) else None
    run_id = originals[0].get("run_id")
    later_truth = (
        (truth_reader or native_later_truth)(run_id, db) if isinstance(run_id, str) else None
    )
    native_verdict = (
        {"uphold_blocker": "FAIL", "reject_blocker": "PASS"}.get(decision)
        if isinstance(decision, str)
        else None
    )
    grade = int(native_verdict in {"PASS", "FAIL"} and later_truth in {"PASS", "FAIL"})
    agree = bool(grade and native_verdict == later_truth)
    rows = json.loads(json.dumps(saved["rows"]))
    selected = next(row for row in rows if row["case_id"] == case_id)
    selected["native_projection"] = {
        "attempt_id": record["attempt_id"],
        "material_evidence_revision": revision,
        "trusted": True,
        "decision": decision,
        "later_truth": later_truth,
        "graded": grade,
        "agree": agree,
    }
    projection = {
        "source_report_sha256": report_hash,
        "original_case_id": case_id,
        "native_admission": "trusted_journal_refreshed",
        "native_summary": {"graded": grade, "agree": int(agree), "unknown": int(not grade)},
        "rows": rows,
    }
    # This is one independently authenticated case projection, not a cohort
    # regrade.  Retain the source summary verbatim; consumers must read the
    # explicit native_summary instead of mistaking it for 58-case acceptance.
    source_summary = saved.get("summary") if isinstance(saved.get("summary"), dict) else {}
    projection["summary"] = source_summary
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(projection, indent=2) + "\n")
    return projection


def refresh_native_cohort(
    *,
    original_report: Path,
    output: Path,
    cases: dict[str, roles.CollectedAdjudicationCase],
    journal: NativeAttemptJournal,
    expected_source_hash: str,
    db: Path | None = None,
    truth_reader=None,
) -> dict:
    """Build one complete, nonpublishing cohort candidate from native receipts.

    Every validated receipt for this original report requires a freshly issued
    capability. JSON projections are never inputs. All original rows/history and
    the original summary remain verbatim; only native_summary counts native
    grades. This function cannot dispatch, reserve, replace the primary report,
    or turn an unadmitted original into a measured case.
    """
    if output.exists() or output.is_symlink() or output.resolve() == original_report.resolve():
        raise ValueError("native cohort output must be new and separate")
    if journal.path.resolve() in {original_report.resolve(), output.resolve()}:
        raise ValueError("native journal must be separate from report and cohort")
    before = original_report.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    if digest != expected_source_hash:
        raise ValueError("original report changed before native cohort")
    saved = json.loads(before)
    if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
        raise ValueError("saved report must contain a rows list")
    _validate_original_report(saved["rows"])
    if not isinstance(cases, dict) or any(
        not isinstance(key, str) or not isinstance(case, roles.CollectedAdjudicationCase)
        for key, case in cases.items()
    ):
        raise TypeError("native cohort requires freshly issued collected cases")
    originals = {row["case_id"]: row for row in saved["rows"]}

    def records():
        with sqlite3.connect(f"file:{journal.path.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute("PRAGMA application_id").fetchone()[0] != NATIVE_JOURNAL_APPLICATION_ID:
                raise ValueError("native cohort requires a native journal")
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM native_attempts WHERE report_sha256=? ORDER BY attempt_id",
                    (digest,),
                )
            ]

    journal_before = records()
    if any(record["case_id"] not in originals for record in journal_before):
        raise ValueError("native journal case is outside the original cohort")
    required = {record["case_id"] for record in journal_before if record["state"] == "validated"}
    if set(cases) != required:
        raise ValueError("native cohort requires every validated case and no extra cases")
    if any(originals[key].get("decision") not in {None, "needs_more_evidence"} for key in cases):
        raise ValueError("native cohort only resumes original abstentions")
    # Validate the complete recursive paid history before truth reads or output.
    original_paid = [
        row.get("backend_run_id")
        for row in _attempt_rows(saved["rows"])
        if row.get("backend_run_id")
    ]
    native_paid = [
        record["backend_run_id"] for record in journal_before if record["backend_run_id"]
    ]
    if len(set(native_paid)) != len(native_paid) or set(original_paid) & set(native_paid):
        raise ValueError("native cohort has duplicate paid backend identities")
    projections = {}
    with tempfile.TemporaryDirectory() as scratch:
        # First authenticate EVERY receipt/contract without touching the Brain.
        for index, (key, case) in enumerate(cases.items()):
            refresh_native_projection(
                original_report=original_report,
                output=Path(scratch) / f"check-{index}.json",
                case_id=key,
                case=case,
                journal=journal,
                truth_reader=lambda *_args: None,
            )
        for index, (key, case) in enumerate(cases.items()):
            projection = refresh_native_projection(
                original_report=original_report,
                output=Path(scratch) / f"truth-{index}.json",
                case_id=key,
                case=case,
                journal=journal,
                db=db,
                truth_reader=truth_reader,
            )
            projections[key] = next(
                row["native_projection"] for row in projection["rows"] if row["case_id"] == key
            )
    # No partial cohort escapes if a later capability/Brain read failed or a
    # concurrent writer changed either frozen input while truth was collected.
    if original_report.read_bytes() != before or records() != journal_before:
        raise ValueError("native cohort input changed during refresh")
    result = json.loads(before)
    for row in result["rows"]:
        # Never preserve a serialized projection as native authority.
        row.pop("native_projection", None)
        if row["case_id"] in projections:
            row["native_projection"] = projections[row["case_id"]]
    graded = sum(item["graded"] for item in projections.values())
    agree = sum(int(item["agree"]) for item in projections.values())
    result.update(
        source_report_sha256=digest,
        native_admission="trusted_journal_cohort_candidate",
        native_summary={
            "cases": len(originals),
            "admitted_cases": len(projections),
            "unadmitted_cases": len(originals) - len(projections),
            "graded": graded,
            "agree": agree,
            "disagree": graded - agree,
            "unknown": len(projections) - graded,
            "agreement_rate": agree / graded if graded else None,
            "pending_attempts": sum(record["state"] != "validated" for record in journal_before),
        },
        native_attempts=journal_before,
        publication="candidate_only; original summary and primary remain unchanged",
    )
    encoded = json.dumps(result, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publication without replacement, including a concurrent new file.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def report_path() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator")) / (
        "capability-program/adjudicator-retro.json"
    )


def disputes(*, db: Path | None = None, now: int | None = None, days: int = 90) -> list[dict]:
    """Read the existing Brain, without schema initialization or outcome writes."""
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT r.run_id,r.target,r.ts,o.verifier_verdict,o.adjudicated_verdict,"
                "o.merged,o.durability,o.durability_checked_ts FROM runs r JOIN outcomes o "
                "USING(run_id) WHERE r.ts>=? AND o.verifier_verdict IS NOT NULL "
                "AND o.adjudicated_verdict IS NOT NULL "
                "AND o.verifier_verdict!=o.adjudicated_verdict ORDER BY r.ts DESC,r.run_id",
                ((now if now is not None else int(time.time())) - days * 86400,),
            )
        ]


def _packet_metadata_only(ground_truth: dict) -> bool:
    patch = ground_truth.get("diff_patch")
    patch_text = patch.get("text") if isinstance(patch, dict) else ""
    has_patch = (
        isinstance(patch, dict)
        and isinstance(patch_text, str)
        and bool(patch_text.strip())
        and isinstance(patch.get("revision"), str)
        and bool(patch["revision"].strip())
        and not patch.get("truncated", False)
    )
    criteria = ground_truth.get("acceptance_criteria")
    has_criteria = (
        isinstance(criteria, list)
        and bool(criteria)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            and isinstance(item.get("revision"), str)
            and bool(item["revision"].strip())
            for item in criteria
        )
    )
    return not (has_patch and has_criteria)


def _row_metadata_only(row: dict) -> bool:
    packet = row.get("packet")
    ground_truth = (packet or {}).get("ground_truth_evidence") if isinstance(packet, dict) else None
    if isinstance(ground_truth, dict):
        # Collector qualification currently grants semantic judgment only. A
        # serialized capability/flag cannot authenticate native report admission,
        # even when the packet also contains full patch and criterion fields.
        if "qualification" in ground_truth:
            return True
        return _packet_metadata_only(ground_truth)
    return True


def build_packet(row: dict, evidence: dict) -> dict:
    """Require actual finding text and merge-bound diff/gate evidence, not merge=PASS."""
    ground_truth = evidence.get("ground_truth_evidence")
    if not isinstance(ground_truth, dict):
        ground_truth = {}
    case = {
        "target": row.get("target"),
        "source": "retrospective",
        "metadata_only": _packet_metadata_only(ground_truth),
        "disputed_finding": evidence.get("disputed_finding"),
        "ground_truth_evidence": ground_truth,
    }
    if roles._validate_adjudication_case(case):
        raise ValueError("packet requires target, disputed_finding and ground_truth_evidence")
    finding = case["disputed_finding"]
    if not isinstance(finding, dict) or not isinstance(finding.get("body"), str):
        raise ValueError("packet requires verifier finding comment text")
    if not verifier_evidence.MARKER_RE.sub("", finding["body"]).strip():
        raise ValueError("packet requires verifier finding comment text")
    ground_truth = case["ground_truth_evidence"]
    if not isinstance(ground_truth, dict):
        raise ValueError("packet requires merged diff summary and gate runs")
    diff = ground_truth.get("diff_summary")
    gates = ground_truth.get("gate_runs")

    def nonblank(value):
        return isinstance(value, str) and bool(value.strip())

    def diff_entry(value):
        return nonblank(value) or isinstance(value, dict) and nonblank(value.get("path"))

    def gate_entry(value):
        if nonblank(value):
            return True
        if not isinstance(value, dict):
            return False
        return all(nonblank(value.get(key)) for key in ("name", "conclusion", "detailsUrl")) or all(
            nonblank(value.get(key)) for key in ("context", "state", "targetUrl")
        )

    has_diff = nonblank(diff) or (
        isinstance(diff, list) and bool(diff) and all(diff_entry(value) for value in diff)
    )
    if not has_diff:
        raise ValueError("packet requires merged diff summary")
    if not isinstance(gates, list) or not gates or not all(gate_entry(value) for value in gates):
        raise ValueError("packet requires gate runs")
    return case


def _gh_json(args: list[str]) -> dict:
    # Operators may wrap the entire CLI with detached-net; no GitHub mutations occur here.
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "GitHub evidence read failed")
    return json.loads(proc.stdout)


def _gate_rollup(pr: dict) -> dict:
    """Turn unavailable gate pages into a recoverable evidence gap."""
    try:
        rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
        if not isinstance(rollup, dict):
            raise ValueError("complete gate evidence unavailable")
        contexts = rollup["contexts"]
        if not isinstance(contexts, dict):
            raise ValueError("complete gate evidence unavailable")
        nodes, page = contexts["nodes"], contexts["pageInfo"]
        if (
            not isinstance(nodes, list)
            or any(not isinstance(node, dict) for node in nodes)
            or not isinstance(page, dict)
            or not isinstance(page.get("hasNextPage"), bool)
            or (
                page["hasNextPage"]
                and (not isinstance(page.get("endCursor"), str) or not page["endCursor"])
            )
        ):
            raise ValueError("complete gate evidence unavailable")
        return rollup
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("complete gate evidence unavailable") from exc


def _shorten_long_lines(patch_text: str) -> tuple[str, list[dict]]:
    """Cut each diff line over ``MAX_DIFF_LINE_BYTES`` to its head and a marker naming its size.

    Returns the text and one ``{"path", "diff_line", "bytes"}`` per shortened line, so what was
    left out is explicit in the packet and never a silent stop. ``path`` is the file named by the
    nearest preceding ``diff --git`` header, ``diff_line`` the 1-based line in the patch.
    """
    shortened: list[dict] = []
    lines = patch_text.split("\n")
    path = ""
    for number, line in enumerate(lines, 1):
        header = re.match(r"diff --git a/(.+?) b/", line)
        if header:
            path = header.group(1)
        encoded = line.encode("utf-8")
        if len(encoded) > MAX_DIFF_LINE_BYTES:
            head = encoded[:DIFF_LINE_KEEP_BYTES].decode("utf-8", errors="ignore")
            lines[number - 1] = f"{head}[... line shortened: {len(encoded)} bytes ...]"
            shortened.append({"path": path, "diff_line": number, "bytes": len(encoded)})
    return "\n".join(lines), shortened


def fetch_evidence(row: dict) -> dict:
    repo, sep, number = str(row.get("target") or "").partition("#")
    if not sep or not number.isdigit() or repo.count("/") != 1:
        raise ValueError("dispute target is not a PR key")
    owner, name = repo.split("/")
    query = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
      pullRequest(number:$n){number state headRefOid mergedAt mergeCommit{oid}
      files(first:100){pageInfo{hasNextPage} nodes{path additions deletions}}
      comments(last:100){pageInfo{hasPreviousPage} nodes{body url author{login}}}
      closingIssuesReferences(first:5){nodes{number body updatedAt lastEditedAt}}
      commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){
        pageInfo{hasNextPage endCursor} nodes{__typename ... on CheckRun{name conclusion detailsUrl}
        ... on StatusContext{context state targetUrl}}}}}}}}}}"""
    pr = _gh_json(
        [
            "api",
            "graphql",
            "-f",
            "query=" + query,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-F",
            "n=" + number,
        ]
    )["data"]["repository"]["pullRequest"]
    if not pr or pr["state"] != "MERGED":
        raise ValueError("merged PR evidence unavailable")
    if not (pr.get("mergeCommit") or {}).get("oid"):
        raise ValueError(
            "merged PR merge commit unavailable; retry when merge-bound evidence exists"
        )
    if pr["comments"]["pageInfo"]["hasPreviousPage"] or pr["files"]["pageInfo"]["hasNextPage"]:
        raise ValueError("truncated verifier comment or diff evidence")
    decision = verifier_evidence.decision_from_pr(repo, pr)
    if not decision or decision["verdict"] != row["verifier_verdict"]:
        raise ValueError("current merge-bound verifier decision missing or changed")
    comments = pr["comments"]["nodes"]
    finding = next(
        (
            c
            for c in reversed(comments)
            if (
                verifier_evidence.decision_from_pr(repo, {**pr, "comments": {"nodes": [c]}})
                == decision
            )
        ),
        None,
    )
    if finding is None:
        raise ValueError("merge-bound verifier finding comment unavailable")
    rollup = _gate_rollup(pr)
    contexts = rollup["contexts"]
    pages = 1
    while contexts["pageInfo"]["hasNextPage"]:
        if pages >= 20:
            raise ValueError("gate evidence exceeds bounded 20-page read")
        gate_query = """query($owner:String!,$name:String!,$n:Int!,$cursor:String!){
          repository(owner:$owner,name:$name){pullRequest(number:$n){headRefOid
          commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100,after:$cursor){
          pageInfo{hasNextPage endCursor} nodes{__typename ... on CheckRun{name conclusion detailsUrl}
          ... on StatusContext{context state targetUrl}}}}}}}}}}"""
        fresh = _gh_json(
            [
                "api",
                "graphql",
                "-f",
                "query=" + gate_query,
                "-f",
                "owner=" + owner,
                "-f",
                "name=" + name,
                "-F",
                "n=" + number,
                "-f",
                "cursor=" + contexts["pageInfo"]["endCursor"],
            ]
        )["data"]["repository"]["pullRequest"]
        if fresh["headRefOid"] != pr["headRefOid"]:
            raise ValueError("PR head changed while reading gate evidence")
        page = _gate_rollup(fresh)["contexts"]
        contexts["nodes"].extend(page["nodes"])
        contexts["pageInfo"] = page["pageInfo"]
        pages += 1
    gates = [
        c for c in contexts["nodes"] if "gate" in str(c.get("name", c.get("context", ""))).lower()
    ]
    if not gates:
        raise ValueError("no gate run in exact-head evidence")
    patch_proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/pulls/{number}",
            "-H",
            "Accept: application/vnd.github.v3.diff",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if patch_proc.returncode:
        raise ValueError("merged PR diff patch unavailable")
    patch_text, shortened_lines = _shorten_long_lines(patch_proc.stdout)
    patch_truncated = len(patch_text.encode("utf-8")) > MAX_DIFF_PATCH_BYTES
    if patch_truncated:
        patch_text = patch_text.encode("utf-8")[:MAX_DIFF_PATCH_BYTES].decode(
            "utf-8", errors="ignore"
        )
    acceptance_criteria: list[dict[str, str]] = []
    acceptance_criteria_errors: list[dict[str, str | int]] = []
    criteria_revision = pr["headRefOid"]
    merged_at = str(pr.get("mergedAt") or "")
    for issue in (pr.get("closingIssuesReferences") or {}).get("nodes") or []:
        if not isinstance(issue, dict):
            continue
        body = issue.get("body")
        if not isinstance(body, str):
            continue
        issue_updated = str(issue.get("updatedAt") or "")
        # ``updatedAt`` moves on any comment, label or reopen; only ``lastEditedAt`` (null when the
        # body was never edited) says the criteria text changed after the merge.
        issue_edited = str(issue.get("lastEditedAt") or "")
        if merged_at and issue_edited and issue_edited > merged_at:
            acceptance_criteria_errors.append(
                {
                    "issue": int(issue["number"]) if str(issue.get("number", "")).isdigit() else 0,
                    "error": (
                        "linked issue body was edited after merge; "
                        "merge-time acceptance criteria are unavailable"
                    ),
                }
            )
            continue
        try:
            texts = _criterion_texts(body)
        except ValueError as exc:
            acceptance_criteria_errors.append(
                {
                    "issue": int(issue["number"]) if str(issue.get("number", "")).isdigit() else 0,
                    "error": str(exc),
                }
            )
            continue
        criteria_revision = issue_updated or pr["headRefOid"]
        acceptance_criteria.extend(
            [
                {
                    "text": text,
                    "revision": criteria_revision,
                    "source": f"issue#{issue.get('number')}@{criteria_revision}",
                }
                for text in texts
            ]
        )
    return {
        "disputed_finding": {"ref": finding["url"], "body": finding["body"], "decision": decision},
        "ground_truth_evidence": {
            "head_sha": pr["headRefOid"],
            "merge_sha": pr["mergeCommit"]["oid"],
            "merge_bound_at": merged_at or None,
            "diff_ref": f"https://github.com/{repo}/pull/{number}/files",
            "diff_summary": pr["files"]["nodes"],
            "diff_patch": {
                "revision": pr["headRefOid"],
                "text": patch_text,
                **({"truncated": True} if patch_truncated else {}),
                **({"shortened_lines": shortened_lines} if shortened_lines else {}),
            },
            "acceptance_criteria": acceptance_criteria,
            "acceptance_criteria_errors": acceptance_criteria_errors,
            "acceptance_criteria_revision": criteria_revision,
            "gate_runs": gates,
        },
    }


def later_truth(row: dict) -> str | None:
    """Use only judged post-detection durability, the same labels fleet_shapes consumes."""
    checked = row.get("durability_checked_ts")
    if not isinstance(checked, int) or checked < feedback.DURABILITY_DETECTION_SINCE:
        return None
    durability = row.get("durability")
    if durability == "durable":
        return "PASS"
    if durability in fleet_shapes.BAD_DURABILITY:
        return "FAIL"
    return None


def native_later_truth(run_id: str, db: Path | None = None) -> str | None:
    """Read the original run's durable outcome without creating schema or writes."""
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT durability,durability_checked_ts FROM outcomes WHERE run_id=?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    return later_truth({"durability": row[0], "durability_checked_ts": row[1]})


def measured_cost(run_id: str | None, db: Path | None = None) -> float | None:
    if not run_id:
        return None
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT cost_usd,source FROM costs WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row and row[1] in feedback.COMPLETE_COST_SOURCES else None


def _refresh_saved_verdicts(rows: list[dict], db: Path | None = None) -> None:
    """Refresh primary decisions; publication independently refreshes all cost receipts."""
    refreshable = [row for row in rows if row.get("decision")]
    if not refreshable:
        return
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in refreshable:
            outcome = conn.execute(
                "SELECT merged,durability,durability_checked_ts FROM outcomes WHERE run_id=?",
                (row["run_id"],),
            ).fetchone()
            row["later_truth"] = later_truth(dict(outcome)) if outcome else None
            merged = outcome["merged"] if outcome else None
            row["merge_rule_verdict"] = (
                ("PASS" if merged else "FAIL") if merged is not None else None
            )


def _apply_evidence_floor(row: dict) -> None:
    """Preserve raw historical proposals; separate their effective disposition.

    This report's producer has never supplied inspected source/artifact contents.
    Legacy rows and explicit false flags therefore cannot upgrade its evidence.
    Applying the floor is local and requires no repeated backend call.
    """
    if not _row_metadata_only(row):
        return
    row["metadata_only"] = True
    row["disposition"] = "needs_more_evidence"
    if row.get("shadow_verdict") is not None:
        row.setdefault("raw_shadow_verdict", row["shadow_verdict"])
    row["shadow_verdict"] = None
    row["disposition_reason"] = (
        "Retrospective diff counts and gate statuses do not establish inspected "
        "source, byte parity or complete acceptance artifacts."
    )


def _shadow_verdict_for_row(row: dict) -> str | None:
    verdicts = {"uphold_blocker": "FAIL", "reject_blocker": "PASS"}
    if row.get("shadow_verdict") in {"PASS", "FAIL"}:
        return row["shadow_verdict"]
    decision = row.get("decision")
    return verdicts.get(decision) if isinstance(decision, str) else None


def _attempt_rows(rows: list[dict]) -> Iterator[dict]:
    """Visit primary and retained attempts without promoting retries to cases."""
    for row in rows:
        yield row
        history = row.get("retry_attempts", [])
        if not isinstance(history, list) or any(not isinstance(r, dict) for r in history):
            raise ValueError("invalid retry attempt history")
        yield from _attempt_rows(history)


def _attempt_snapshot(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "retry_attempts"}


def _case_identity(row: dict) -> dict:
    return {k: row[k] for k in ("run_id", "verifier_verdict", "adjudicated_verdict")}


def _case_id(row: dict) -> str:
    return hashlib.sha256(json.dumps(_case_identity(row), sort_keys=True).encode()).hexdigest()


def _is_metadata_only_abstention(old: dict | None) -> bool:
    if not old:
        return False
    return old.get("decision") == "needs_more_evidence" and _row_metadata_only(old)


def _retry_pending(old: dict | None, *, retry: bool) -> bool:
    """The one definition of "this saved row is attempted again".

    Under ``retry`` a saved row is attempted again when it holds no decision, or when it is a
    ``needs_more_evidence`` abstention whose packet was metadata-only. Without ``retry`` no saved
    row is attempted again. ``run()``'s skip test, ``attempt_order`` and the summary's
    ``retry_pending`` count all read this, so they cannot drift apart.
    """
    if not retry or not old:
        return False
    return not old.get("decision") or _is_metadata_only_abstention(old)


def attempt_order(population: list[dict], saved: dict[str, dict], *, retry: bool) -> list[dict]:
    """Every feed row, in the order ``run()`` should spend its budget on them.

    Under ``retry``: saved rows that will be attempted again come first, then rows with no saved
    entry, then the rest, which ``run()`` only refreshes. Each group keeps feed order, newest
    first. Without ``retry`` the feed order is unchanged. The saved report lives in the file
    ``run()`` loads and ``disputes()`` reads only the Brain, so the order is applied here and
    ``disputes()`` stays a plain read.
    """
    if not retry:
        return list(population)

    def rank(row: dict) -> int:
        old = saved.get(_case_id(row))
        if old is None:
            return 1
        return 0 if _retry_pending(old, retry=True) else 2

    return sorted(population, key=rank)


def summarize(rows: list[dict]) -> dict:
    # Classify independently of stored flags, including pre-floor legacy rows.
    classified = [dict(row) for row in rows]
    for row in classified:
        if _row_metadata_only(row):
            _apply_evidence_floor(row)
        else:
            row["metadata_only"] = False
            row.setdefault("disposition", row.get("decision"))
            row["shadow_verdict"] = _shadow_verdict_for_row(row)
    accepted = [
        row for row in classified if row.get("disposition") in {"uphold_blocker", "reject_blocker"}
    ]
    graded = [r for r in accepted if r.get("later_truth")]
    agree = sum(_shadow_verdict_for_row(r) == r["later_truth"] for r in graded)
    baseline = sum(r["merge_rule_verdict"] == r["later_truth"] for r in graded)

    # A legacy dollar receipt proves an attempt even if its backend ID was not
    # retained. Preparation-only rows do not. Classify each attempt once, so
    # coverage numerators and denominator describe the same population.
    def valid_amount(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def measured_amount(row):
        report = row.get("cost_report") or {}
        if report.get("status") == "measured" and valid_amount(report.get("cost_usd")):
            return report["cost_usd"]
        value = row.get("cost_usd")
        return value if valid_amount(value) else None

    attempted_rows: list[dict] = []
    by_backend: dict[str, int] = {}

    def receipt_rank(row):
        if measured_amount(row) is not None:
            return 2
        return int((row.get("cost_report") or {}).get("status") == "imputed")

    for row in _attempt_rows(rows):
        backend_id = row.get("backend_run_id")
        if not (
            backend_id
            or measured_amount(row) is not None
            or (row.get("cost_report") or {}).get("status") in {"measured", "imputed"}
        ):
            continue
        if backend_id in by_backend:
            index = by_backend[backend_id]
            if receipt_rank(row) > receipt_rank(attempted_rows[index]):
                attempted_rows[index] = row
            continue
        if backend_id:
            by_backend[backend_id] = len(attempted_rows)
        attempted_rows.append(row)
    measured_costs = [
        measured_amount(row) for row in attempted_rows if measured_amount(row) is not None
    ]
    imputed_rows = [
        row
        for row in attempted_rows
        if measured_amount(row) is None
        and valid_amount((row.get("cost_report") or {}).get("cost_usd"))
        and (row.get("cost_report") or {}).get("status") == "imputed"
    ]
    estimated_costs = measured_costs + [
        row["cost_report"]["cost_usd"]
        for row in imputed_rows
        if valid_amount(row["cost_report"].get("cost_usd"))
    ]
    return {
        "cases": len(rows),
        "adjudicated": len(accepted),
        "proposed_decisions": sum(
            r.get("decision") in {"uphold_blocker", "reject_blocker"} for r in rows
        ),
        "metadata_only_cases": sum(1 for row in rows if _row_metadata_only(row)),
        "graded": len(graded),
        "agree": agree,
        "disagree": len(graded) - agree,
        "agreement_rate": agree / len(graded) if graded else None,
        "merge_rule_agreement_rate": baseline / len(graded) if graded else None,
        "cost_usd": sum(measured_costs) if measured_costs else None,
        "cost_attempted_cases": len(attempted_rows),
        "cost_imputed_cases": len(imputed_rows),
        "cost_unmeasured_cases": len(attempted_rows) - len(measured_costs) - len(imputed_rows),
        "cost_scale": feedback.COST_SCALE,
        "estimated_cost_usd": sum(estimated_costs) if estimated_costs else None,
        "cost_measured_cases": len(measured_costs),
        "cost_per_case": sum(measured_costs) / len(measured_costs) if measured_costs else None,
        # Saved rows a --retry run would attempt again: 0 when drained, never absent.
        "retry_pending": sum(_retry_pending(row, retry=True) for row in rows),
    }


def compare_proposals(rows: list[dict]) -> dict:
    """Compare raw metadata proposals, without granting them effective verdicts.

    Both rates use the same cases with a binary proposal, judged later truth and
    known merge disposition. Abstentions and missing evidence stay outside that
    denominator. This measures retrospective correlation, not accepted adjudication.
    """
    verdicts = {"uphold_blocker": "FAIL", "reject_blocker": "PASS"}
    compared = []
    proposed = pending = missing_merge = abstained = 0
    for row in rows:
        decision = row.get("decision")
        verdict = verdicts.get(decision) if isinstance(decision, str) else None
        truth = row.get("later_truth")
        baseline = row.get("merge_rule_verdict")
        comparable = (
            verdict is not None and truth in {"PASS", "FAIL"} and baseline in {"PASS", "FAIL"}
        )
        row["proposal_comparison"] = {
            "verdict": verdict,
            "agrees": verdict == truth if comparable else None,
            "merge_rule_agrees": baseline == truth if comparable else None,
        }
        if verdict is not None:
            proposed += 1
            if truth not in {"PASS", "FAIL"}:
                pending += 1
            elif baseline not in {"PASS", "FAIL"}:
                missing_merge += 1
        elif row.get("decision") == "needs_more_evidence":
            abstained += 1
        if comparable:
            compared.append(row["proposal_comparison"])
    agree = sum(case["agrees"] for case in compared)
    baseline_agree = sum(case["merge_rule_agrees"] for case in compared)
    return {
        "evidence_basis": "raw_metadata_proposals",
        "cases": len(rows),
        "proposed_decisions": proposed,
        "compared": len(compared),
        "pending_truth": pending,
        "missing_merge_disposition": missing_merge,
        "abstained": abstained,
        "unassessed": len(rows) - proposed - abstained,
        "agree": agree,
        "disagree": len(compared) - agree,
        "agreement_rate": agree / len(compared) if compared else None,
        "merge_rule_agree": baseline_agree,
        "merge_rule_disagree": len(compared) - baseline_agree,
        "merge_rule_agreement_rate": baseline_agree / len(compared) if compared else None,
    }


def _collection_requirements(case: dict) -> tuple[dict | None, list[dict]]:
    """Return only an explicitly saved inventory; never infer acceptance claims."""
    inventory = case.get("collection_requirements")
    if not isinstance(inventory, dict):
        return None, [
            {
                "kind": "missing_requirements_inventory",
                "detail": "saved case has no explicit inventory",
            }
        ]
    source_paths = inventory.get("source_paths")
    acceptance = inventory.get("acceptance")
    if not isinstance(source_paths, list) or not all(
        isinstance(path, str) for path in source_paths
    ):
        return None, [
            {
                "kind": "invalid_requirements_inventory",
                "detail": "source_paths must be a list of paths",
            }
        ]
    if not isinstance(acceptance, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("criterion"), str)
        and item["criterion"].strip()
        and isinstance(item.get("location"), str)
        and item["location"].strip()
        for item in acceptance
    ):
        return None, [
            {
                "kind": "invalid_requirements_inventory",
                "detail": "acceptance must be a list of criterion/location objects",
            }
        ]
    return {"source_paths": source_paths, "acceptance": acceptance}, []


def _case_evaluated_sha(case: dict) -> str | None:
    raw_packet = case.get("packet")
    packet: dict[Any, Any] = raw_packet if isinstance(raw_packet, dict) else {}
    raw_finding = packet.get("disputed_finding")
    finding: dict[Any, Any] = raw_finding if isinstance(raw_finding, dict) else {}
    raw_decision = finding.get("decision")
    decision: dict[Any, Any] = raw_decision if isinstance(raw_decision, dict) else {}
    raw_ground_truth = packet.get("ground_truth_evidence")
    ground_truth: dict[Any, Any] = raw_ground_truth if isinstance(raw_ground_truth, dict) else {}
    sha = decision.get("evaluated_sha") or ground_truth.get("merge_sha")
    return (
        sha
        if isinstance(sha, str)
        and len(sha) == 40
        and all(c in "0123456789abcdef" for c in sha.lower())
        else None
    )


_GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    }
)


def _local_git(repo: Path, *args: str) -> bytes:
    """Read only local object data; callers must not turn this into a fetch path."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        timeout=30,
        env={
            **{key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARS},
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        },
    ).stdout


def _safe_source_path(path: str) -> bool:
    if "\0" in path:
        return False
    try:
        path.encode("utf-8")
    except UnicodeError:
        return False
    candidate = Path(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts


def validate_collected_evidence(result: dict) -> list[dict]:
    """Fail closed: byte integrity is distinct from acceptance completeness."""
    gaps = list(result.get("gaps", []))
    requirements = result.get("requirements")
    records = result.get("sources", [])
    if not isinstance(requirements, dict):
        if not any(gap.get("kind") == "missing_requirements_inventory" for gap in gaps):
            gaps.append(
                {"kind": "missing_requirements_inventory", "detail": "requirements unavailable"}
            )
    else:
        expected = requirements.get("source_paths", [])
        observed = {record.get("path") for record in records if record.get("complete")}
        for path in expected:
            if path not in observed:
                gaps.append({"kind": "missing_source_coverage", "path": path})
        if not requirements.get("acceptance"):
            gaps.append(
                {
                    "kind": "missing_acceptance_inventory",
                    "detail": "source bytes alone cannot establish acceptance completeness",
                }
            )
        # Exact declared identity matters: one artifact must not satisfy another
        # inventory slot merely because its criterion or path happens to match.
        for index, criterion in enumerate(requirements.get("acceptance", [])):
            covered = any(
                record.get("complete") is True
                and record.get("inventory_index") == index
                and record.get("criterion") == criterion["criterion"]
                and record.get("location") == criterion["location"]
                and record.get("evaluated_sha") == result.get("evaluated_sha")
                for record in result.get("acceptance_artifacts", [])
            )
            if not covered:
                gaps.append({"kind": "missing_acceptance_evidence", "criterion": criterion})
    unique = []
    seen = set()
    for gap in gaps:
        encoded = json.dumps(gap, sort_keys=True, default=str)
        if encoded not in seen:
            unique.append(gap)
            seen.add(encoded)
    return unique


def _collect_git_blob(
    repository: Path, evaluated_sha: str, path: str, byte_limit: int
) -> tuple[dict[str, Any], list[dict]]:
    """Read one regular evaluated-revision blob without lazy fetch or replacements."""
    gaps: list[dict] = []
    record: dict[str, Any] = {
        "path": path,
        "evaluated_sha": evaluated_sha,
        "complete": False,
    }
    if not _safe_source_path(path):
        gaps.append({"kind": "unsafe_source_path", "path": path})
        return record, gaps
    try:
        # ``ls-tree -- <path>`` still interprets its final argument as a
        # pathspec.  Keep it literal, then require its NUL-delimited
        # response to name exactly one regular-file entry.  A directory
        # or a glob/pathspec must never silently select its first child.
        tree_entry = _local_git(
            repository,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            evaluated_sha,
            "--",
            path,
        )
        entries = tree_entry.split(b"\0")
        expected_name = path.encode("utf-8")
        if len(entries) != 2 or not entries[0] or entries[1]:
            raise ValueError("source path did not resolve to exactly one Git entry")
        metadata_and_name = entries[0].split(b"\t", 1)
        if len(metadata_and_name) != 2 or metadata_and_name[1] != expected_name:
            raise ValueError("source path did not resolve to the requested Git entry")
        metadata = metadata_and_name[0].split()
        if (
            len(metadata) != 3
            or metadata[0] not in {b"100644", b"100755"}
            or metadata[1] != b"blob"
            or len(metadata[2]) != 40
            or any(c not in b"0123456789abcdef" for c in metadata[2])
        ):
            raise ValueError("source path is not a regular Git blob")
        blob = metadata[2].decode("ascii")
        byte_length = int(_local_git(repository, "cat-file", "-s", blob))
        record.update(
            {"blob_sha": blob, "byte_length": byte_length, "git_mode": metadata[0].decode("ascii")}
        )
        if byte_length < 0:
            raise ValueError("negative Git blob size")
        if byte_length > byte_limit:
            gaps.append(
                {
                    "kind": "source_byte_limit_exceeded",
                    "path": path,
                    "limit": byte_limit,
                    "actual": byte_length,
                }
            )
            return record, gaps
        data = _local_git(repository, "cat-file", "blob", blob)
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        gaps.append({"kind": "missing_source_object", "path": path, "sha": evaluated_sha})
        return record, gaps
    if len(data) != record["byte_length"]:
        gaps.append({"kind": "source_length_mismatch", "path": path})
    record["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        record["bytes_utf8"] = data.decode("utf-8")
    except UnicodeDecodeError:
        gaps.append({"kind": "unsupported_source_encoding", "path": path})
    else:
        record["complete"] = len(data) == record["byte_length"]
    return record, gaps


def collect_case_evidence(
    case: dict,
    *,
    repository: Path,
    byte_limit: int = MAX_COLLECTION_SOURCE_BYTES,
) -> dict:
    """Collect source bytes from the case's evaluated commit, without side effects.

    This is collection only.  It neither changes the supplied saved report nor
    calls the adjudicator, feedback database, role records, or remote services.
    """
    result: dict[str, Any] = {
        "schema": "adjudicator-retro-collection/v1",
        "case_id": case.get("case_id"),
        "repository": str(repository.resolve()),
        "evaluated_sha": _case_evaluated_sha(case),
        "requirements": None,
        "sources": [],
        "acceptance_artifacts": [],
        "completeness_scope": "supplied_inventory_only",
        "inventory_exhaustiveness": "unverified",
        "acceptance_semantics": "unassessed",
        "gaps": [],
    }
    requirements, gaps = _collection_requirements(case)
    result["requirements"] = requirements
    result["gaps"].extend(gaps)
    evaluated_sha = result["evaluated_sha"]
    if not evaluated_sha:
        result["gaps"].append(
            {"kind": "missing_evaluated_sha", "detail": "case has no exact evaluated revision"}
        )
    elif not repository.is_dir():
        result["gaps"].append({"kind": "missing_local_repository", "detail": str(repository)})
    else:
        try:
            _local_git(repository, "cat-file", "-e", evaluated_sha + "^{commit}")
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            result["gaps"].append({"kind": "missing_evaluated_commit", "sha": evaluated_sha})
            evaluated_sha = None
    if requirements and evaluated_sha:
        for path in requirements["source_paths"]:
            record, gaps = _collect_git_blob(repository, evaluated_sha, path, byte_limit)
            result["sources"].append(record)
            result["gaps"].extend(gaps)
        for index, item in enumerate(requirements["acceptance"]):
            location = item["location"]
            if location.startswith("git-path:"):
                record, gaps = _collect_git_blob(
                    repository, evaluated_sha, location.removeprefix("git-path:"), byte_limit
                )
            else:
                record = {"complete": False, "evaluated_sha": evaluated_sha}
                gaps = [{"kind": "unsupported_acceptance_transport", "location": location}]
            record.update({"criterion": item["criterion"], "location": item["location"]})
            record["inventory_index"] = index
            result["acceptance_artifacts"].append(record)
            result["gaps"].extend(gaps)
    result["gaps"] = validate_collected_evidence(result)
    result["complete"] = not result["gaps"]
    return result


def collect_saved_case(
    report: Path,
    case_id: str,
    output: Path,
    repository: Path,
    *,
    byte_limit: int = MAX_COLLECTION_SOURCE_BYTES,
) -> dict:
    """Write a separate collection record for exactly one immutable saved case."""
    try:
        aliases_report = report.resolve() == output.resolve() or os.path.samefile(report, output)
    except OSError:
        aliases_report = report.resolve() == output.resolve()
    if aliases_report:
        raise ValueError("collection output must be separate from the saved report")
    saved = json.loads(report.read_text())
    cases = [row for row in saved.get("rows", []) if row.get("case_id") == case_id]
    if len(cases) != 1:
        raise ValueError("saved report must contain exactly one matching case_id")
    result = collect_case_evidence(cases[0], repository=repository, byte_limit=byte_limit)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def prepare_collected_case(
    report: Path,
    case_id: str,
    output: Path,
    repository: Path,
    *,
    byte_limit: int = MAX_COLLECTION_SOURCE_BYTES,
    packet_byte_limit: int = MAX_COLLECTION_SOURCE_BYTES,
) -> dict:
    """Prepare complete collected bytes for the existing role, without dispatch/admission.

    Recollect immutable Git objects instead of trusting caller-written collection
    flags/hashes. The legacy retrospective evidence floor remains in force: byte
    transport is not an exhaustive inventory or a semantic acceptance decision.
    """
    if byte_limit < 1 or packet_byte_limit < 1:
        raise ValueError("collection and packet byte limits must be positive")
    # Exclusive output also rejects symlink/hardlink aliases and historical receipts.
    if output.exists() or output.is_symlink() or output.resolve() == report.resolve():
        raise ValueError("prepared case output must be new and separate from saved report")
    saved = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
        raise ValueError("saved report must contain a rows list")
    cases = [
        row for row in saved["rows"] if isinstance(row, dict) and row.get("case_id") == case_id
    ]
    if len(cases) != 1:
        raise ValueError("saved report must contain exactly one matching case_id")
    row = cases[0]
    raw_packet = row.get("packet")
    if not isinstance(raw_packet, dict) or raw_packet.get("target") != row.get("target"):
        raise ValueError("saved packet target must match the case target")
    packet = build_packet(row, raw_packet)
    collection = collect_case_evidence(row, repository=repository, byte_limit=byte_limit)
    if not collection["complete"] or not (collection.get("requirements") or {}).get("source_paths"):
        raise ValueError("incomplete collected case: " + json.dumps(collection["gaps"]))
    # Keep the nested dict: the role compacts strings, but preserves typed evidence.
    packet["ground_truth_evidence"] = {
        **packet["ground_truth_evidence"],
        "collected_evidence": collection,
    }
    encoded = (json.dumps(packet, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    # The role renders JSON with ASCII escapes; Unicode can expand substantially.
    # Bound both the stored UTF-8 case and that actual case rendering.
    role_case_bytes = len(json.dumps(packet, indent=2, sort_keys=True).encode("utf-8"))
    if max(len(encoded), role_case_bytes) > packet_byte_limit:
        raise ValueError("prepared case exceeds packet byte limit; evidence was not truncated")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never replace an output created concurrently after the initial check.
    with output.open("xb") as stream:
        stream.write(encoded)
    return packet


def _criterion_texts(body: str) -> list[str]:
    """Read explicit one-line Markdown acceptance items; never invent a contract."""
    criteria: list[str] = []
    active = False
    for line in body.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if heading:
            active = heading[1].strip().lower() == "acceptance criteria"
        elif active:
            item = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.+)$", line)
            if item:
                text = item[1].strip()
                if text not in criteria:
                    criteria.append(text)
            elif line.strip().startswith("<!--"):
                active = False
            elif line.strip():
                raise ValueError("unsupported multiline acceptance criterion; qualify explicitly")
    if not criteria:
        raise ValueError("full provenance has no explicit acceptance criteria")
    return criteria


MAX_SUPPORTING_SOURCES = 32


def _collect_supporting_sources(
    manifest: Any, repository: Path, sha: str, changed_paths: list[str], byte_limit: int
) -> list[dict]:
    """Recollect inherited Git bytes; supporting paths never expand the PR diff.

    Descriptors bind the full blob, even when explicit inclusive line ranges
    select a smaller, non-exhaustive prompt excerpt. Caller-supplied contents
    and completeness flags are never evidence.
    """
    if not isinstance(manifest, list):
        raise ValueError("supporting_sources must be a list")
    if len(manifest) > MAX_SUPPORTING_SOURCES:
        raise ValueError("supporting manifest exceeds descriptor count limit")
    records = []
    seen = set(changed_paths)
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "evaluated_sha",
            "blob_sha",
            "sha256",
            "line_ranges",
        }:
            raise ValueError("supporting source descriptor has invalid fields")
        path = item["path"]
        if not isinstance(path, str) or not _safe_source_path(path) or path in seen:
            raise ValueError("supporting paths must be unique, safe and outside the diff")
        seen.add(path)
        if item["evaluated_sha"] != sha:
            raise ValueError("supporting source must bind the evaluated revision")
        record, gaps = _collect_git_blob(repository, sha, path, byte_limit)
        if gaps or not record["complete"]:
            raise ValueError("supporting source collection failed: " + json.dumps(gaps))
        if item["blob_sha"] != record["blob_sha"] or item["sha256"] != record["sha256"]:
            raise ValueError("supporting source blob/hash mismatch")
        ranges = item["line_ranges"]
        if not isinstance(ranges, list):
            raise ValueError("supporting line_ranges must be a list")
        lines = record["bytes_utf8"].splitlines(keepends=True)
        previous_end = 0
        excerpts = []
        for span in ranges:
            if (
                not isinstance(span, list)
                or len(span) != 2
                or any(type(n) is not int for n in span)
                or not (previous_end < span[0] <= span[1] <= len(lines))
            ):
                raise ValueError("supporting line ranges must be ordered, disjoint and in bounds")
            text = "".join(lines[span[0] - 1 : span[1]])
            excerpts.append(
                {
                    "start_line": span[0],
                    "end_line": span[1],
                    "bytes_utf8": text,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
            previous_end = span[1]
        record["line_ranges"] = ranges
        record["exhaustive"] = not ranges
        if ranges:
            record.pop("bytes_utf8")
            record["excerpts"] = excerpts
        records.append(record)
    return sorted(records, key=lambda record: record["path"])


def qualify_collected_case(
    row: dict,
    repository: Path,
    qualification: dict,
    *,
    byte_limit: int = MAX_COLLECTION_SOURCE_BYTES,
    prompt_byte_limit: int = MAX_COLLECTION_SOURCE_BYTES,
) -> roles.CollectedAdjudicationCase:
    """Issue semantic-judgment input after fresh collection and inventory checks.

    The local saved snapshot's authenticity remains unverified. Git proves the
    complete first-parent squash diff and actual immutable contents, not external
    inventory exhaustiveness or acceptance semantics. JSON flags grant nothing.
    """
    if byte_limit < 1 or prompt_byte_limit < 1:
        raise ValueError("collection and prompt byte limits must be positive")
    if not isinstance(row, dict) or not isinstance(qualification, dict):
        raise ValueError("saved case and qualification must be JSON objects")
    raw = row.get("packet")
    if not isinstance(raw, dict) or raw.get("target") != row.get("target"):
        raise ValueError("saved packet target must match the case target")
    packet = build_packet(row, raw)
    sha = _case_evaluated_sha(row)
    if sha is None:
        raise ValueError("qualification requires an exact evaluated commit")
    # Squash comparison semantics are explicit. A PR's moving baseRefOid is
    # not the evaluated merge's first parent and cannot substitute for it.
    parent = _local_git(repository, "rev-parse", sha + "^1").decode().strip()
    if qualification.get("comparison_base") != parent:
        raise ValueError("qualification base must be evaluated commit's first parent")
    if packet["ground_truth_evidence"].get("merge_sha") != sha:
        raise ValueError("finding and merge evidence revisions disagree")
    changed = (
        _local_git(
            repository, "diff-tree", "--no-renames", "--name-status", "-r", "-z", parent, sha
        )
        .decode("utf-8")
        .split("\0")
    )
    if changed[-1] != "" or (len(changed) - 1) % 2:
        raise ValueError("malformed complete Git diff inventory")
    entries = list(zip(changed[:-1:2], changed[1:-1:2]))
    if not entries or any(status not in {"A", "M"} for status, _path in entries):
        raise ValueError(
            "qualification supports add/modify regular blobs only; deletion/type change refused"
        )
    actual_paths = [path for _status, path in entries]
    requirements, gaps = _collection_requirements(row)
    declared = (requirements or {}).get("source_paths", [])
    diff = packet["ground_truth_evidence"].get("diff_summary")
    if (
        gaps
        or len(set(declared)) != len(declared)
        or not isinstance(diff, list)
        or any(not isinstance(item, dict) for item in diff)
        or len({item.get("path") for item in diff}) != len(diff)
        or set(declared) != set(actual_paths)
        or {item.get("path") for item in diff} != set(actual_paths)
    ):
        raise ValueError("declared, saved PR and complete Git diff inventories must match exactly")
    provenance = row.get("inventory_provenance")
    snapshot = qualification.get("snapshot")
    if not isinstance(provenance, dict) or not isinstance(snapshot, dict):
        raise ValueError("inventory_provenance and snapshot must be JSON objects")
    body = provenance.get("source_criterion_tasks")
    if (
        not isinstance(body, str)
        or not body.strip()
        or hashlib.sha256(body.encode()).hexdigest() != provenance.get("pr_body_sha256")
        or snapshot.get("body") != body
        or str(snapshot.get("number")) != str(row["target"]).rsplit("#", 1)[-1]
        or snapshot.get("headRefOid") != packet["ground_truth_evidence"].get("head_sha")
    ):
        raise ValueError(
            "complete criterion provenance must match the saved target/head/body snapshot"
        )
    criteria = _criterion_texts(body)
    mappings = qualification.get("criterion_mapping")
    if not isinstance(mappings, list) or len(mappings) != len(criteria):
        raise ValueError("every explicit acceptance criterion needs one evidence/UNKNOWN mapping")
    collection = collect_case_evidence(row, repository=repository, byte_limit=byte_limit)
    if not collection["complete"]:
        raise ValueError("incomplete collected case: " + json.dumps(collection["gaps"]))
    before_sources = []
    for status, path in entries:
        if status == "M":
            before, before_gaps = _collect_git_blob(repository, parent, path, byte_limit)
            if before_gaps or not before["complete"]:
                raise ValueError("incomplete before-source evidence: " + json.dumps(before_gaps))
            before_sources.append(before)
    collection["before_sources"] = before_sources
    supporting = _collect_supporting_sources(
        qualification.get("supporting_sources", []), repository, sha, actual_paths, byte_limit
    )
    collection["supporting_sources"] = supporting
    locations = {item["location"] for item in collection["acceptance_artifacts"]}
    for criterion, mapping in zip(criteria, mappings):
        if not isinstance(mapping, dict) or mapping.get("criterion") != criterion:
            raise ValueError(
                "criterion mappings must preserve the full ordered acceptance contract"
            )
        refs, unknowns = mapping.get("evidence_locations", []), mapping.get("unknowns", [])
        if (
            not isinstance(refs, list)
            or any(ref not in locations for ref in refs)
            or not isinstance(unknowns, list)
            or not (refs or unknowns)
            or any(
                not isinstance(item, dict)
                or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ("evidence_ref", "owner", "next_action")
                )
                for item in unknowns
            )
        ):
            raise ValueError("criterion mapping needs collected artifacts or named owned UNKNOWNs")
    refs = set(actual_paths) | locations
    refs.update("supporting-git-path:" + item["path"] for item in supporting)
    for mapping in mappings:
        refs.update(item["evidence_ref"] for item in mapping.get("unknowns", []))
    packet["ground_truth_evidence"] = {
        **packet["ground_truth_evidence"],
        "collected_evidence": collection,
        "criterion_provenance": {
            "body": body,
            "sha256": provenance["pr_body_sha256"],
            "snapshot": {key: snapshot[key] for key in ("number", "headRefOid", "body")},
        },
        "criterion_mapping": mappings,
        "qualified_ground_truth_refs": sorted(refs),
        "qualification": {
            "case_id": row.get("case_id"),
            "comparison_base": parent,
            "diff_entries": entries,
            "scope": "semantic_judgment_only",
            "snapshot_authenticity": "unverified",
            "external_exhaustiveness": "unverified",
            "acceptance_semantics": "unassessed",
            "persisted_admission": False,
        },
    }
    packet["metadata_only"] = False
    # A deterministic content revision is an identity, never persisted retry or
    # grading authority. Bind all recollected bytes, mappings and explicit UNKNOWNs.
    revision_input = json.loads(json.dumps(packet))
    # A checkout location is transport metadata, not a new evidence revision.
    revision_collection = revision_input["ground_truth_evidence"]["collected_evidence"]
    revision_collection.pop("repository")
    # The qualification check above compares these inventories as sets. Their
    # presentation order must not manufacture a new material revision either.
    revision_collection["requirements"]["source_paths"].sort()
    revision_collection["sources"].sort(key=lambda record: record["path"])
    revision_input["ground_truth_evidence"]["diff_summary"].sort(key=lambda item: item["path"])
    packet["ground_truth_evidence"]["qualification"]["material_evidence_revision"] = hashlib.sha256(
        json.dumps(revision_input, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    result = roles._issue_collected_case(packet, prompt_byte_limit)
    prompt = roles.ROLE_REGISTRY["adjudicator"].build_prompt(
        {"case": roles._collected_snapshot(result), "complete_collected_case": True}
    )
    if len(prompt.encode("utf-8")) > prompt_byte_limit:
        raise ValueError("qualified prompt exceeds byte limit; evidence was not truncated")
    return result


def run(
    *,
    dispatch: bool = False,
    limit: int = 5,
    path: Path | None = None,
    db: Path | None = None,
    evidence_reader=None,
    runner=None,
    retry: bool = False,
    timeout: int = DISPATCH_TIMEOUT_S,
) -> dict:
    """Bounded, resumable shadow run; invalid packets are recorded and never dispatched.

    ``timeout`` is the per-dispatch budget in seconds. It is saved on every attempted row as
    ``dispatch_timeout_s`` so a row's history says which budget it ran under. Under ``retry`` the
    ``limit`` is spent on saved rows awaiting a retry before any newer unsaved dispute
    (``attempt_order``).
    """
    path = path or report_path()
    reader = evidence_reader or fetch_evidence
    runner = runner or roles.run_adjudicator_agent
    previous = json.loads(path.read_text()) if path.exists() else {}
    saved = {r["case_id"]: r for r in previous.get("rows", [])}
    # Exhaust the lazy traversal before recovery or paid dispatch can mutate state.
    # A malformed later history must not strand an earlier paid attempt on save.
    for _ in _attempt_rows(list(saved.values())):
        pass
    _refresh_saved_verdicts(list(saved.values()), db)
    population = disputes(db=db)
    attempted = 0
    # A paid verdict can outlive the replay window while its Brain write is
    # pending. Repair only the original role record, never redispatch that case.
    if dispatch and retry:
        for entry in _attempt_rows(list(saved.values())):
            record = entry.get("role_record")
            if not entry.get("role_record_error") or not record:
                continue
            if attempted >= max(0, limit):
                break
            attempted += 1
            try:
                feedback.record_role_run(**record)
                entry["role_run_id"] = record["run_id"]
                entry["role_record_error"] = None
            except Exception as exc:
                entry["role_record_error"] = str(exc)
            _persist_report(path, list(saved.values()), len(population), db=db)
    for row in attempt_order(population, saved, retry=retry):
        identity = _case_identity(row)
        case_id = _case_id(row)
        old = saved.get(case_id)
        reattempt_abstention = bool(retry and _is_metadata_only_abstention(old))
        if old and not _retry_pending(old, retry=retry):
            if old.get("decision"):
                old["later_truth"] = later_truth(row)
            continue
        if attempted >= max(0, limit):
            continue
        attempted += 1
        # `reattempt_abstention` is derived from `old`, but keep the optional
        # value narrowed here as well: this copy must only be made for an
        # existing persisted row.
        preserved = dict(old) if reattempt_abstention and old is not None else None
        entry = {
            **identity,
            "case_id": case_id,
            "target": row["target"],
            "later_truth": later_truth(row),
            "merge_rule_verdict": (
                ("PASS" if row["merged"] else "FAIL") if row["merged"] is not None else None
            ),
            "cost_usd": None,
        }
        result = None
        try:
            packet = build_packet(row, reader(row))
            entry["packet"] = packet
            if dispatch:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Recorded before the call, so an attempt that times out or raises still names
                # the budget it ran under; retry history copies it with the rest of the row.
                entry["dispatch_timeout_s"] = timeout
                result = runner(
                    case=packet,
                    dispatch=True,
                    source="retrospective",
                    cwd=str(path.parent),
                    timeout=timeout,
                )
                entry.update(
                    {
                        k: result.get(k)
                        for k in (
                            "role_run_id",
                            "backend_run_id",
                            "backend",
                            "errors",
                            "role_record_error",
                            "role_record",
                        )
                    }
                )
                if result.get("proposal") and not result.get("errors"):
                    entry["decision"] = result["proposal"]["decision"]
                    entry["proposal"] = result["proposal"]
                    entry["disposition"] = (result.get("advisory_plan") or {}).get(
                        "decision", "needs_more_evidence"
                    )
                    entry["shadow_verdict"] = _shadow_verdict_for_row(
                        {**entry, "decision": entry.get("decision")}
                    )
                    if packet.get("metadata_only"):
                        _apply_evidence_floor(entry)
                try:
                    entry["cost_usd"] = measured_cost(result.get("backend_run_id"), db)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RuntimeError,
                    sqlite3.Error,
                ) as exc:
                    # A valid paid proposal survives an unavailable accounting read.
                    entry["cost_usd"] = None
                    entry["cost_read_error"] = str(exc)
        except (
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            subprocess.TimeoutExpired,
        ) as exc:
            if preserved is not None:
                preserved["retry_error"] = str(exc)
                if dispatch and result is not None:
                    entry["error"] = str(exc)
                    preserved["retry_attempts"] = [
                        *preserved.get("retry_attempts", []),
                        _attempt_snapshot(entry),
                    ]
                saved[case_id] = preserved
            else:
                entry["error"] = str(exc)
                saved[case_id] = entry
            _persist_report(path, list(saved.values()), len(population), db=db)
            continue
        if preserved is not None:
            if not dispatch or not result or not result.get("proposal") or result.get("errors"):
                # Preserve retry eligibility while accounting for the new attempt.
                if dispatch:
                    preserved["retry_attempts"] = [
                        *preserved.get("retry_attempts", []),
                        _attempt_snapshot(entry),
                    ]
                entry = preserved
            else:
                # A successful replacement must not erase the original paid attempt.
                entry["retry_attempts"] = [
                    *preserved.get("retry_attempts", []),
                    _attempt_snapshot(preserved),
                ]
        saved[case_id] = entry
        # Persist every attempt so an interrupted batch does not repeat successful paid calls.
        _persist_report(path, list(saved.values()), len(population), db=db)
    # Saved cases can gain judged durability or measured cost without a new
    # paid attempt. Publish those refreshes for the weekly file reader too.
    return _persist_report(path, list(saved.values()), len(population), db=db)


def _persist_report(path: Path, rows: list[dict], population: int, db: Path | None = None) -> dict:
    """Atomically publish the same shadow evidence returned to the caller."""
    for row in rows:
        if _row_metadata_only(row):
            _apply_evidence_floor(row)
    for row in _attempt_rows(rows):
        if row.get("backend_run_id"):
            try:
                row["cost_report"] = feedback.role_cost_report(
                    row["backend_run_id"], row.get("backend"), db=db
                )
                row["cost_usd"] = (
                    row["cost_report"].get("cost_usd")
                    if row["cost_report"].get("status") == "measured"
                    else None
                )
                row.pop("cost_refresh_error", None)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
                # Accounting availability cannot erase an already paid identity.
                row["cost_usd"] = None
                row["cost_report"] = {"status": "unmeasured", "cost_usd": None}
                row["cost_refresh_error"] = str(exc)
    comparison = compare_proposals(rows)
    summary = summarize(rows)
    comparison.update(
        {key: summary[key] for key in ("cost_usd", "cost_measured_cases", "cost_per_case")}
    )
    report = {
        "generated_at": int(time.time()),
        "shadow": True,
        "source": "retrospective",
        "population": population,
        "rows": rows,
        "summary": summary,
        "proposal_comparison": comparison,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report


def weekly_line(path: Path | None = None) -> str:
    try:
        summary = json.loads((path or report_path()).read_text())["summary"]
        cost = summary["cost_usd"]
        pending = summary.get("retry_pending")
        return (
            f"adjudicator shadow: cases {summary['cases']}, agree {summary['agree']}, "
            f"disagree {summary['disagree']}, cost {cost if cost is not None else 'UNKNOWN'} "
            f"(graded {summary['graded']}; costs measured {summary['cost_measured_cases']}; "
            f"cost measured {summary['cost_measured_cases']} of {summary.get('cost_attempted_cases', summary['cases'])}, "
            f"imputed {summary.get('cost_imputed_cases', 0)}, "
            f"unmeasured {summary.get('cost_unmeasured_cases', 0)}; "
            f"estimated accounted USD {summary.get('estimated_cost_usd') if summary.get('estimated_cost_usd') is not None else 'UNMEASURED'}; "
            f"scale {feedback.COST_SCALE}; "
            f"retry pending {'UNKNOWN' if pending is None else pending})"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "adjudicator shadow: cases UNKNOWN, agree UNKNOWN, disagree UNKNOWN, cost UNKNOWN"


def _selftest() -> None:
    with tempfile.TemporaryDirectory() as native_root:
        journal_path = Path(native_root) / "native.sqlite"
        journal = NativeAttemptJournal(journal_path)
        unstarted = journal.reserve("unstarted", "A", "digest", "report", await_start=True)
        assert journal.release_unstarted(unstarted["attempt_id"])
        unstarted = journal.reserve("unstarted", "A", "digest", "report", await_start=True)
        journal.begin_invocation(unstarted["attempt_id"], {"run_id": "started"})
        assert not journal.release_unstarted(unstarted["attempt_id"])
        assert not journal.reserve("unstarted", "B", "digest", "report")["reserved"]
        try:
            journal.validate(unstarted["attempt_id"], {"errors": []})
        except ValueError:
            pass
        else:
            raise AssertionError("validation accepted a missing raw receipt")
        reservation = journal.reserve("original", "A", "digest", "report")
        journal.persist_raw(reservation["attempt_id"], {"run_id": "paid", "output": "{}"})
        assert not journal.reserve("original", "B", "digest", "report")["reserved"]
        journal.validate(
            reservation["attempt_id"], {"errors": ["malformed proposal"], "proposal": None}
        )
        restarted = NativeAttemptJournal(journal_path)
        assert not restarted.reserve("original", "A", "digest", "report")["reserved"]
        assert restarted.reserve("original", "B", "digest", "report")["reserved"]
        assert not restarted.has_binary_decision("original")
    with tempfile.TemporaryDirectory() as supporting_root:
        repository = Path(supporting_root)
        _local_git(repository, "init", "-q")
        (repository / "inherited.py").write_text("first\nsecond\n")
        _local_git(repository, "add", "inherited.py")
        _local_git(
            repository,
            "-c",
            "user.name=Selftest",
            "-c",
            "user.email=selftest@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "supporting source",
        )
        sha = _local_git(repository, "rev-parse", "HEAD").decode().strip()
        descriptor = {
            "path": "inherited.py",
            "evaluated_sha": sha,
            "blob_sha": _local_git(repository, "rev-parse", sha + ":inherited.py").decode().strip(),
            "sha256": hashlib.sha256(b"first\nsecond\n").hexdigest(),
            "line_ranges": [[1, 1]],
        }
        baseline = _collect_supporting_sources([descriptor], repository, sha, [], 1000)
        assert baseline[0]["excerpts"][0]["bytes_utf8"] == "first\n"
        for invalid, changed in [
            ({**descriptor, "sha256": "0" * 64}, []),
            (descriptor, ["inherited.py"]),
            ({**descriptor, "line_ranges": [[1, 3]]}, []),
        ]:
            try:
                _collect_supporting_sources([invalid], repository, sha, changed, 1000)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid supporting source accepted")
            assert _collect_supporting_sources([descriptor], repository, sha, [], 1000) == baseline
    assert _row_metadata_only(
        {
            "packet": {
                "ground_truth_evidence": {
                    "qualification": {"persisted_admission": True},
                    "diff_patch": {"text": "patch", "revision": "sha"},
                    "acceptance_criteria": [{"text": "criterion", "revision": "sha"}],
                }
            }
        }
    )
    assert _collect_supporting_sources([], Path("."), "a" * 40, [], 1) == []
    try:
        _collect_supporting_sources({}, Path("."), "a" * 40, [], 1)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed supporting manifest accepted")
    try:
        build_packet({"target": "owner/repo#1"}, {})
    except ValueError:
        pass
    else:
        raise AssertionError("absent evidence was accepted")
    assert summarize([])["agreement_rate"] is None
    cost = summarize(
        [{"backend_run_id": "native", "cost_report": {"status": "imputed", "cost_usd": 3}}]
    )
    assert cost["cost_imputed_cases"] == 1 and cost["cost_measured_cases"] == 0
    assert cost["estimated_cost_usd"] == 3 and cost["cost_usd"] is None
    assert later_truth({"durability": "pending"}) is None
    assert not _safe_source_path("bad\0path")
    criterion = {"criterion": "named artifact", "location": "git-path:proof.txt"}
    result: dict[str, Any] = {
        "evaluated_sha": "a" * 40,
        "requirements": {"source_paths": [], "acceptance": [criterion]},
        "acceptance_artifacts": [
            {**criterion, "inventory_index": 0, "evaluated_sha": "a" * 40, "complete": True}
        ],
    }
    assert validate_collected_evidence(result) == []
    result["acceptance_artifacts"][0]["inventory_index"] = 1
    assert validate_collected_evidence(result)[0]["kind"] == "missing_acceptance_evidence"
    try:
        prepare_collected_case(Path("absent"), "case", Path("out"), Path("."), byte_limit=0)
    except ValueError:
        pass
    else:
        raise AssertionError("nonpositive preparation budget was accepted")
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "malformed.json"
        path.write_text(
            json.dumps(
                {"rows": [{"case_id": "bad", "retry_attempts": [{"retry_attempts": [None]}]}]}
            )
        )
        original = path.read_bytes()
        try:
            run(path=path, db=Path(root) / "absent.db", dispatch=True, retry=True)
        except ValueError as exc:
            assert str(exc) == "invalid retry attempt history"
        else:
            raise AssertionError("malformed history reached dispatch preparation")
        assert path.read_bytes() == original
    import inspect

    role_default = inspect.signature(roles.run_adjudicator_agent).parameters["timeout"].default
    assert DISPATCH_TIMEOUT_S == role_default, "retro dispatch budget drifted from the role default"
    fresh = {"run_id": "new", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"}
    aged = {"run_id": "old", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"}
    queue = {_case_id(aged): {"decision": None}}
    assert [r["run_id"] for r in attempt_order([fresh, aged], queue, retry=True)] == ["old", "new"]
    assert [r["run_id"] for r in attempt_order([fresh, aged], queue, retry=False)] == ["new", "old"]
    assert summarize([{"decision": None}])["retry_pending"] == 1
    assert summarize([{"decision": "reject_blocker"}])["retry_pending"] == 0
    print("adjudicator_retro selftest: 18 checks passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--native-cohort", action="store_true")
    parser.add_argument("--cohort-manifest", type=Path)
    parser.add_argument("--native-admit", metavar="CASE_ID")
    parser.add_argument("--native-refresh", metavar="CASE_ID")
    parser.add_argument(
        "--case-input",
        type=Path,
        help="enriched report with original case anchors; used by --native-admit and --native-refresh",
    )
    parser.add_argument("--native-journal", type=Path)
    parser.add_argument("--expected-source-hash")
    parser.add_argument(
        "--collect-case",
        metavar="CASE_ID",
        help="collect local immutable-commit evidence for one saved case; never adjudicates or dispatches",
    )
    parser.add_argument(
        "--prepare-collected-case",
        metavar="CASE_ID",
        help="prepare source-bound case JSON for the existing role; no dispatch or admission",
    )
    parser.add_argument("--packet-byte-limit", type=int, default=MAX_COLLECTION_SOURCE_BYTES)
    parser.add_argument(
        "--judge-collected-case",
        metavar="CASE_ID",
        help="qualify fresh collected input for the existing shadow role",
    )
    parser.add_argument(
        "--qualification",
        type=Path,
        help="explicit squash comparison, full PR snapshot and criterion mappings",
    )
    parser.add_argument(
        "--report", type=Path, help="saved retrospective report used by collection/preparation"
    )
    parser.add_argument(
        "--output", type=Path, help="separate collection output used by --collect-case"
    )
    parser.add_argument("--repository", type=Path, help="local Git checkout used by --collect-case")
    parser.add_argument("--byte-limit", type=int, default=MAX_COLLECTION_SOURCE_BYTES)
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Run router-chosen shadow role; never apply verdicts",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--timeout",
        type=int,
        default=DISPATCH_TIMEOUT_S,
        metavar="SECONDS",
        help="per-dispatch budget for each adjudicator call (default: the role's own default)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Repair pending Brain records without redispatch; retry failed evidence attempts",
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be at least 1 second")
    if args.selftest and any((args.native_admit, args.native_refresh, args.native_cohort)):
        parser.error("--selftest cannot be combined with a native mode")
    if args.selftest:
        _selftest()
        return 0
    if args.native_cohort:
        if any(
            (
                args.native_admit,
                args.native_refresh,
                args.dispatch,
                args.retry,
                args.collect_case,
                args.prepare_collected_case,
                args.judge_collected_case,
                args.case_input,
                args.qualification,
                args.repository,
            )
        ):
            parser.error("--native-cohort cannot dispatch or combine with another mode")
        if not all(
            (
                args.report,
                args.output,
                args.native_journal,
                args.expected_source_hash,
                args.cohort_manifest,
            )
        ):
            parser.error(
                "--native-cohort requires --report --output --native-journal "
                "--expected-source-hash --cohort-manifest"
            )
        try:
            manifest = json.loads(args.cohort_manifest.read_text())
            if not isinstance(manifest, list) or len(manifest) > 1000:
                raise ValueError("cohort manifest must be a bounded list")
            cases = {}
            for entry in manifest:
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {"case_id", "input_report", "repository", "qualification"}
                    or any(not isinstance(value, str) or not value for value in entry.values())
                ):
                    raise ValueError("cohort manifest requires case_id and three explicit paths")
                key = entry["case_id"]
                if key in cases:
                    raise ValueError("cohort manifest has duplicate case IDs")

                def location(name):
                    return args.cohort_manifest.parent / entry[name]

                enriched = json.loads(location("input_report").read_text())
                if not isinstance(enriched, dict) or not isinstance(enriched.get("rows"), list):
                    raise ValueError("enriched cohort input requires rows")
                matches = [
                    row
                    for row in enriched["rows"]
                    if isinstance(row, dict) and row.get("case_id") == key
                ]
                if len(matches) != 1:
                    raise ValueError("cohort input must contain exactly one matching case")
                cases[key] = qualify_collected_case(
                    matches[0],
                    location("repository"),
                    json.loads(location("qualification").read_text()),
                    byte_limit=args.byte_limit,
                    prompt_byte_limit=args.packet_byte_limit,
                )
            result = refresh_native_cohort(
                original_report=args.report,
                output=args.output,
                cases=cases,
                journal=NativeAttemptJournal.open_existing(args.native_journal),
                expected_source_hash=args.expected_source_hash,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            parser.error(str(exc))
        print(json.dumps(result["native_summary"], indent=2))
        return 0
    if args.native_admit or args.native_refresh:
        if args.native_admit and args.native_refresh:
            parser.error("choose exactly one of --native-admit and --native-refresh")
        native_case_id = args.native_admit or args.native_refresh
        mode_flag = "--native-admit" if args.native_admit else "--native-refresh"
        if args.native_refresh and args.dispatch:
            parser.error("--native-refresh never dispatches")
        if any(
            (args.collect_case, args.prepare_collected_case, args.judge_collected_case, args.retry)
        ):
            parser.error(f"{mode_flag} cannot be combined with another adjudicator mode")
        if not all(
            (
                args.report,
                args.case_input,
                args.repository,
                args.qualification,
                args.output,
                args.native_journal,
                args.expected_source_hash,
            )
        ):
            parser.error(
                f"{mode_flag} requires --report --case-input --repository --qualification --output "
                "--native-journal and --expected-source-hash"
            )
        try:
            if hashlib.sha256(args.report.read_bytes()).hexdigest() != args.expected_source_hash:
                raise ValueError("original report changed before native operation")
            original = json.loads(args.report.read_text(encoding="utf-8"))
            enriched = json.loads(args.case_input.read_text(encoding="utf-8"))
            for label, saved_report in (("original", original), ("enriched", enriched)):
                if not isinstance(saved_report, dict) or not isinstance(
                    saved_report.get("rows"), list
                ):
                    raise ValueError(f"{label} report must contain a rows list")
                if any(not isinstance(row, dict) for row in saved_report["rows"]):
                    raise ValueError(f"{label} report rows must be objects")
                for row in saved_report["rows"]:
                    packet = row.get("packet")
                    if not isinstance(packet, dict):
                        raise ValueError(f"{label} report row packet must be an object")
                    if not isinstance(packet.get("ground_truth_evidence"), dict):
                        raise ValueError(
                            f"{label} report row ground_truth_evidence must be an object"
                        )
            original_rows = [
                row for row in original.get("rows", []) if row.get("case_id") == native_case_id
            ]
            enriched_rows = [
                row for row in enriched.get("rows", []) if row.get("case_id") == native_case_id
            ]
            if len(original_rows) != 1 or len(enriched_rows) != 1:
                raise ValueError("both reports must contain exactly one original case")
            for key in ("case_id", "run_id", "target", "verifier_verdict", "adjudicated_verdict"):
                if original_rows[0].get(key) != enriched_rows[0].get(key):
                    raise ValueError("enriched input must preserve original identity anchors")
            for key in ("disputed_finding",):
                if (original_rows[0].get("packet") or {}).get(key) != (
                    enriched_rows[0].get("packet") or {}
                ).get(key):
                    raise ValueError("enriched input must preserve original finding anchors")
            original_ground = (original_rows[0].get("packet") or {}).get(
                "ground_truth_evidence"
            ) or {}
            enriched_ground = (enriched_rows[0].get("packet") or {}).get(
                "ground_truth_evidence"
            ) or {}
            for key in ("head_sha", "merge_sha", "diff_summary", "gate_runs"):
                if original_ground.get(key) != enriched_ground.get(key):
                    raise ValueError("enriched input must preserve original evidence anchors")
            case = qualify_collected_case(
                enriched_rows[0],
                args.repository,
                json.loads(args.qualification.read_text()),
                byte_limit=args.byte_limit,
                prompt_byte_limit=args.packet_byte_limit,
            )
            if args.native_refresh:
                result = refresh_native_projection(
                    original_report=args.report,
                    output=args.output,
                    case_id=native_case_id,
                    case=case,
                    journal=NativeAttemptJournal(args.native_journal),
                )
            else:
                result = admit_collected_retry(
                    original_report=args.report,
                    output=args.output,
                    case_id=native_case_id,
                    case=case,
                    journal=NativeAttemptJournal(args.native_journal),
                    dispatch=args.dispatch,
                    cwd=str(args.repository),
                    timeout=args.timeout,
                    expected_report_sha256=args.expected_source_hash,
                )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2))
        return 0
    if args.judge_collected_case:
        if (
            args.collect_case
            or args.prepare_collected_case
            or not all((args.report, args.repository, args.qualification, args.output))
        ):
            parser.error(
                "--judge-collected-case requires report, repository, qualification and new output"
            )
        if args.output.exists() or args.output.is_symlink():
            parser.error("judgment output must be new; saved reports/receipts cannot be replaced")
        try:
            saved = json.loads(args.report.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
                raise ValueError("saved report must contain a rows list")
            if any(not isinstance(row, dict) for row in saved["rows"]):
                raise ValueError("saved report rows must all be JSON objects")
            rows = [row for row in saved["rows"] if row.get("case_id") == args.judge_collected_case]
            if len(rows) != 1:
                raise ValueError("saved report must contain exactly one matching case_id")
            case = qualify_collected_case(
                rows[0],
                args.repository,
                json.loads(args.qualification.read_text()),
                byte_limit=args.byte_limit,
                prompt_byte_limit=args.packet_byte_limit,
            )
            result = roles.run_adjudicator_agent(
                case=case,
                dispatch=args.dispatch,
                source="retrospective",
                cwd=str(args.repository),
                timeout=args.timeout,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as stream:
                stream.write(json.dumps(result, indent=2) + "\n")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "shadow",
                        "decision_source",
                        "backend_run_id",
                        "evidence_qualification",
                        "errors",
                    )
                },
                indent=2,
            )
        )
        return 0
    if args.prepare_collected_case:
        if (
            args.collect_case
            or args.dispatch
            or not all((args.report, args.output, args.repository))
        ):
            parser.error(
                "--prepare-collected-case requires --report, --output, --repository; "
                "it cannot collect-only or dispatch"
            )
        try:
            packet = prepare_collected_case(
                args.report,
                args.prepare_collected_case,
                args.output,
                args.repository,
                byte_limit=args.byte_limit,
                packet_byte_limit=args.packet_byte_limit,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "target": packet["target"],
                    "metadata_only": True,
                    "output": str(args.output),
                    "dispatched": False,
                }
            )
        )
        return 0
    if args.collect_case:
        if not args.report or not args.output or not args.repository or args.byte_limit < 1:
            parser.error(
                "--collect-case requires --report, --output, --repository, and a positive --byte-limit"
            )
        try:
            result = collect_saved_case(
                args.report,
                args.collect_case,
                args.output,
                args.repository,
                byte_limit=args.byte_limit,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "case_id": result["case_id"],
                    "complete": result["complete"],
                    "completeness_scope": result["completeness_scope"],
                    "inventory_exhaustiveness": result["inventory_exhaustiveness"],
                    "acceptance_semantics": result["acceptance_semantics"],
                    "gaps": result["gaps"],
                },
                indent=2,
            )
        )
        return 0
    result = run(dispatch=args.dispatch, limit=args.limit, retry=args.retry, timeout=args.timeout)
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
