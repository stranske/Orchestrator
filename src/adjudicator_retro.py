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
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import feedback
import fleet_shapes
import roles
import verifier_evidence

# This collector deliberately has no GitHub, Brain, role-routing, or artifact
# transport dependency.  It is a local, immutable-commit evidence boundary.
MAX_COLLECTION_SOURCE_BYTES = 1_048_576


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


def build_packet(row: dict, evidence: dict) -> dict:
    """Require actual finding text and merge-bound diff/gate evidence, not merge=PASS."""
    case = {
        "target": row.get("target"),
        "source": "retrospective",
        "metadata_only": True,
        "disputed_finding": evidence.get("disputed_finding"),
        "ground_truth_evidence": evidence.get("ground_truth_evidence"),
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


def fetch_evidence(row: dict) -> dict:
    repo, sep, number = str(row.get("target") or "").partition("#")
    if not sep or not number.isdigit() or repo.count("/") != 1:
        raise ValueError("dispute target is not a PR key")
    owner, name = repo.split("/")
    query = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
      pullRequest(number:$n){number state headRefOid mergeCommit{oid}
      files(first:100){pageInfo{hasNextPage} nodes{path additions deletions}}
      comments(last:100){pageInfo{hasPreviousPage} nodes{body url author{login}}}
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
    return {
        "disputed_finding": {"ref": finding["url"], "body": finding["body"], "decision": decision},
        "ground_truth_evidence": {
            "head_sha": pr["headRefOid"],
            "merge_sha": pr["mergeCommit"]["oid"],
            "diff_ref": f"https://github.com/{repo}/pull/{number}/files",
            "diff_summary": pr["files"]["nodes"],
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


def measured_cost(run_id: str | None, db: Path | None = None) -> float | None:
    if not run_id:
        return None
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT cost_usd,source FROM costs WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row and row[1] in feedback.COMPLETE_COST_SOURCES else None


def _refresh_saved_verdicts(rows: list[dict], db: Path | None = None) -> None:
    """Grade saved verdicts even after their disputes leave the replay window."""
    verdicts = [row for row in rows if row.get("decision")]
    if not verdicts:
        return
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in verdicts:
            outcome = conn.execute(
                "SELECT merged,durability,durability_checked_ts FROM outcomes WHERE run_id=?",
                (row["run_id"],),
            ).fetchone()
            row["later_truth"] = later_truth(dict(outcome)) if outcome else None
            merged = outcome["merged"] if outcome else None
            row["merge_rule_verdict"] = (
                ("PASS" if merged else "FAIL") if merged is not None else None
            )
            row["cost_usd"] = measured_cost(row.get("backend_run_id"), db)


def _apply_evidence_floor(row: dict) -> None:
    """Preserve raw historical proposals; separate their effective disposition.

    This report's producer has never supplied inspected source/artifact contents.
    Legacy rows and explicit false flags therefore cannot upgrade its evidence.
    Applying the floor is local and requires no repeated backend call.
    """
    row["metadata_only"] = True
    row["disposition"] = "needs_more_evidence"
    if row.get("shadow_verdict") is not None:
        row.setdefault("raw_shadow_verdict", row["shadow_verdict"])
    row["shadow_verdict"] = None
    row["disposition_reason"] = (
        "Retrospective diff counts and gate statuses do not establish inspected "
        "source, byte parity or complete acceptance artifacts."
    )


def summarize(rows: list[dict]) -> dict:
    # Classify independently of stored flags, including pre-floor legacy rows.
    classified = [dict(row) for row in rows]
    for row in classified:
        _apply_evidence_floor(row)
    accepted = [
        row for row in classified if row.get("disposition") in {"uphold_blocker", "reject_blocker"}
    ]
    graded = [r for r in accepted if r.get("later_truth")]
    agree = sum(r["shadow_verdict"] == r["later_truth"] for r in graded)
    baseline = sum(r["merge_rule_verdict"] == r["later_truth"] for r in graded)
    measured_costs = [r["cost_usd"] for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    return {
        "cases": len(rows),
        "adjudicated": len(accepted),
        "proposed_decisions": sum(
            r.get("decision") in {"uphold_blocker", "reject_blocker"} for r in rows
        ),
        "metadata_only_cases": len(rows),
        "graded": len(graded),
        "agree": agree,
        "disagree": len(graded) - agree,
        "agreement_rate": agree / len(graded) if graded else None,
        "merge_rule_agreement_rate": baseline / len(graded) if graded else None,
        "cost_usd": sum(measured_costs) if measured_costs else None,
        "cost_measured_cases": len(measured_costs),
        "cost_per_case": sum(measured_costs) / len(measured_costs) if measured_costs else None,
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
        record.update({"blob_sha": blob, "byte_length": byte_length})
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


def run(
    *,
    dispatch: bool = False,
    limit: int = 5,
    path: Path | None = None,
    db: Path | None = None,
    evidence_reader=None,
    runner=None,
    retry: bool = False,
) -> dict:
    """Bounded, resumable shadow run; invalid packets are recorded and never dispatched."""
    path = path or report_path()
    reader = evidence_reader or fetch_evidence
    runner = runner or roles.run_adjudicator_agent
    previous = json.loads(path.read_text()) if path.exists() else {}
    saved = {r["case_id"]: r for r in previous.get("rows", [])}
    _refresh_saved_verdicts(list(saved.values()), db)
    population = disputes(db=db)
    attempted = 0
    # A paid verdict can outlive the replay window while its Brain write is
    # pending. Repair only the original role record, never redispatch that case.
    if dispatch and retry:
        for entry in saved.values():
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
            _persist_report(path, list(saved.values()), len(population))
    for row in population:
        identity = {k: row[k] for k in ("run_id", "verifier_verdict", "adjudicated_verdict")}
        case_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        old = saved.get(case_id)
        if old and (old.get("decision") or not retry):
            if old.get("decision"):
                old["later_truth"] = later_truth(row)
                old["cost_usd"] = measured_cost(old.get("backend_run_id"), db)
            continue
        if attempted >= max(0, limit):
            continue
        attempted += 1
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
        try:
            packet = build_packet(row, reader(row))
            entry["packet"] = packet
            if dispatch:
                path.parent.mkdir(parents=True, exist_ok=True)
                result = runner(
                    case=packet,
                    dispatch=True,
                    source="retrospective",
                    cwd=str(path.parent),
                    timeout=180,
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
                    _apply_evidence_floor(entry)
                entry["cost_usd"] = measured_cost(result.get("backend_run_id"), db)
        except (
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            subprocess.TimeoutExpired,
        ) as exc:
            entry["error"] = str(exc)
        saved[case_id] = entry
        # Persist every attempt so an interrupted batch does not repeat successful paid calls.
        _persist_report(path, list(saved.values()), len(population))
    # Saved cases can gain judged durability or measured cost without a new
    # paid attempt. Publish those refreshes for the weekly file reader too.
    return _persist_report(path, list(saved.values()), len(population))


def _persist_report(path: Path, rows: list[dict], population: int) -> dict:
    """Atomically publish the same shadow evidence returned to the caller."""
    for row in rows:
        _apply_evidence_floor(row)
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
        return (
            f"adjudicator shadow: cases {summary['cases']}, agree {summary['agree']}, "
            f"disagree {summary['disagree']}, cost {cost if cost is not None else 'UNKNOWN'} "
            f"(graded {summary['graded']}; costs measured {summary['cost_measured_cases']})"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "adjudicator shadow: cases UNKNOWN, agree UNKNOWN, disagree UNKNOWN, cost UNKNOWN"


def _selftest() -> None:
    try:
        build_packet({"target": "owner/repo#1"}, {})
    except ValueError:
        pass
    else:
        raise AssertionError("absent evidence was accepted")
    assert summarize([])["agreement_rate"] is None
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
    print("adjudicator_retro selftest: 7 checks passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
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
        "--retry",
        action="store_true",
        help="Repair pending Brain records without redispatch; retry failed evidence attempts",
    )
    args = parser.parse_args()
    if args.selftest:
        _selftest()
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
    result = run(dispatch=args.dispatch, limit=args.limit, retry=args.retry)
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
