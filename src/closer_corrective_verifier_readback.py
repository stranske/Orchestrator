"""Exact corrective evaluator input readback over existing native read transport.

Source pins record reviewed canonical code actually deployed centrally. Native
metadata, receipt and source checks authenticate the artifact; fixture hashes,
comment text or a compact corrective row alone never establish scope coverage.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import stat
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import closer_evidence
import verifier_evidence

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
WORKFLOW = ".github/workflows/agents-verifier.yml"
CALLEE = ".github/workflows/reusable-agents-verifier.yml"
CENTRAL = "stranske/Workflows"
# Whole caller/reusable and normative consumer source fingerprints. An unmerged
# candidate is not an installed/deployed producer and must not populate this.
# Four exact native-proven versions, never a moving-main or byte-equivalence rule.
# c18 remains a supported historical entry; retained run40620861168 actually
# checked out375, which therefore has its OWN exact entry. Current merged864
# includes the reviewed4307 collector fix;826 adds the reviewed4308 parser-only
# compatibility comment. Eventual run/attempt/helper/reusable,
# canonical-source, upload/artifact and owner/action joins remain unchanged.
DEPLOYED_SOURCE_PINS: tuple[dict, ...] = (
    {
        "caller_sha256": "ab9f3d7618f4b65b6c43b58104bdcd9c92320471dca8fd437b2c34b2c721f9f2",
        "central": {
            ".github/workflows/reusable-agents-verifier.yml": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
            ".github/scripts/verifier_gzip_assets.js": "b9782f228c0e34d27c9eb457df6ee002c0ddc29ee5c3db2081071392c3e3a6c4",
            ".github/scripts/verification_event.js": "d2b0e789efba0e966f5c68789e0e5ec8533e615660a7afad26513df67ef1785b",
            ".github/scripts/agents_verifier_context.js": "7e2ccc0031d754bfeaf9b8f878592b297cf47bb5ea1c3e435bc4be4fb7477b6c",
            "scripts/langchain/pr_verifier.py": "97c55f4d891c548fafd81de609f58a939627371b55a06a62a53f23e3b8b23435",
            "tools/verifier_corpus_evidence.py": "9b112e4207c5cc7986f2b2fbc5eb5c846e9b24dfc2f8c2eb803d1d627882fe45",
        },
        "helper_commit": "c18c14d945cb736e96982dcbb56ca56bfc185ed6",
        "reusable_sha256": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
    },
    {
        "caller_sha256": "ab9f3d7618f4b65b6c43b58104bdcd9c92320471dca8fd437b2c34b2c721f9f2",
        "central": {
            ".github/workflows/reusable-agents-verifier.yml": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
            ".github/scripts/verifier_gzip_assets.js": "b9782f228c0e34d27c9eb457df6ee002c0ddc29ee5c3db2081071392c3e3a6c4",
            ".github/scripts/verification_event.js": "d2b0e789efba0e966f5c68789e0e5ec8533e615660a7afad26513df67ef1785b",
            ".github/scripts/agents_verifier_context.js": "7e2ccc0031d754bfeaf9b8f878592b297cf47bb5ea1c3e435bc4be4fb7477b6c",
            "scripts/langchain/pr_verifier.py": "97c55f4d891c548fafd81de609f58a939627371b55a06a62a53f23e3b8b23435",
            "tools/verifier_corpus_evidence.py": "9b112e4207c5cc7986f2b2fbc5eb5c846e9b24dfc2f8c2eb803d1d627882fe45",
        },
        "helper_commit": "375f48c9b203417f20afc91f2d98ed8ab9c2ce58",
        "reusable_sha256": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
    },
    {
        "caller_sha256": "ab9f3d7618f4b65b6c43b58104bdcd9c92320471dca8fd437b2c34b2c721f9f2",
        "central": {
            ".github/workflows/reusable-agents-verifier.yml": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
            ".github/scripts/verifier_gzip_assets.js": "b9782f228c0e34d27c9eb457df6ee002c0ddc29ee5c3db2081071392c3e3a6c4",
            ".github/scripts/verification_event.js": "d2b0e789efba0e966f5c68789e0e5ec8533e615660a7afad26513df67ef1785b",
            ".github/scripts/agents_verifier_context.js": "0cb1329e483256ce4b40184d7ce8baff00bd1732449d399cdc40fb3e44a0c9a7",
            "scripts/langchain/pr_verifier.py": "97c55f4d891c548fafd81de609f58a939627371b55a06a62a53f23e3b8b23435",
            "tools/verifier_corpus_evidence.py": "9b112e4207c5cc7986f2b2fbc5eb5c846e9b24dfc2f8c2eb803d1d627882fe45",
        },
        "helper_commit": "86450a7a6ba26fdccc77ed50ce7876dc455689cc",
        "reusable_sha256": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
    },
    {
        "caller_sha256": "ab9f3d7618f4b65b6c43b58104bdcd9c92320471dca8fd437b2c34b2c721f9f2",
        "central": {
            ".github/workflows/reusable-agents-verifier.yml": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
            ".github/scripts/verifier_gzip_assets.js": "b9782f228c0e34d27c9eb457df6ee002c0ddc29ee5c3db2081071392c3e3a6c4",
            ".github/scripts/verification_event.js": "d2b0e789efba0e966f5c68789e0e5ec8533e615660a7afad26513df67ef1785b",
            ".github/scripts/agents_verifier_context.js": "0cb1329e483256ce4b40184d7ce8baff00bd1732449d399cdc40fb3e44a0c9a7",
            "scripts/langchain/pr_verifier.py": "ef95423bc7bb1592d0ad260880a991c5ad801dfe5d0be98d8ff02ac20f0c5630",
            "tools/verifier_corpus_evidence.py": "9b112e4207c5cc7986f2b2fbc5eb5c846e9b24dfc2f8c2eb803d1d627882fe45",
        },
        "helper_commit": "826ecfe743dc198aafb1b19c7d9b0b0ac4ff554e",
        "reusable_sha256": "4970235b5970773b72d824dc8b1a7fa9b0046a6ef19d1db20e752733e624ca52",
    },
)
SOURCE_FILES = (
    CALLEE,
    ".github/scripts/verifier_gzip_assets.js",
    ".github/scripts/verification_event.js",
    ".github/scripts/agents_verifier_context.js",
    "scripts/langchain/pr_verifier.py",
    "tools/verifier_corpus_evidence.py",
)
MODES = {
    "evaluate": ("evaluation", "Retain evaluation execution evidence"),
    "compare": ("comparison", "Upload comparison artifacts"),
}


class ReadbackHeld(ValueError):
    pass


def need(ok, reason):
    if not ok:
        raise ReadbackHeld(reason)


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            need(key not in value, "duplicate evaluator artifact JSON field")
            value[key] = item
        return value

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ReadbackHeld("nonfinite evaluator artifact JSON")
        ),
    )


def _document(backend, endpoint):
    value = backend.gh.api(endpoint, method="GET")
    need(type(value) is dict, "native evaluator document unavailable")
    return value


def _content(backend, repository, path, ref):
    doc = _document(backend, f"repos/{repository}/contents/{path}?ref={quote(ref,safe='')}")
    need(
        doc.get("type") == "file"
        and doc.get("path") == path
        and doc.get("encoding") == "base64"
        and type(doc.get("content")) is str,
        "native verifier source unavailable",
    )
    raw = base64.b64decode("".join(doc["content"].split()), validate=True)
    need(
        len(raw) <= closer_evidence.MAX_ARCHIVE_BYTES
        and doc.get("size") == len(raw)
        and doc.get("sha")
        == hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        "native verifier source blob differs",
    )
    return hashlib.sha256(raw).hexdigest()


def _source_pin(backend, repository, caller_ref, central_ref, reusable_ref=None):
    need(bool(DEPLOYED_SOURCE_PINS), "reviewed deployed corrective verifier source unavailable")
    caller = _content(backend, repository, WORKFLOW, caller_ref)
    central = {path: _content(backend, CENTRAL, path, central_ref) for path in SOURCE_FILES}
    reusable = _content(backend, CENTRAL, CALLEE, reusable_ref or central_ref)
    matches = [
        pin
        for pin in DEPLOYED_SOURCE_PINS
        if pin
        == {
            "caller_sha256": caller,
            "central": central,
            "helper_commit": central_ref,
            "reusable_sha256": reusable,
        }
    ]
    need(len(matches) == 1, "deployed corrective verifier source is unsupported")
    return copy.deepcopy(matches[0])


def _timestamp(value):
    need(type(value) is str, "native execution timestamp missing")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as error:
        raise ReadbackHeld("native execution timestamp invalid") from error


def _canonical_commit(backend, repository, commit):
    """Same merged-source witness as acquireRetainedExecution, no local attestation."""
    metadata = _document(backend, f"repos/{repository}")
    need(
        metadata.get("full_name") == repository
        and type(metadata.get("default_branch")) is str
        and metadata["default_branch"],
        "canonical verifier repository unavailable",
    )
    head = _document(
        backend, f"repos/{repository}/commits/{quote(metadata['default_branch'],safe='')}"
    )
    need(
        type(head.get("sha")) is str and re.fullmatch(r"[a-f0-9]{40}", head["sha"]),
        "canonical verifier commit unavailable",
    )
    comparison = _document(backend, f"repos/{repository}/compare/{commit}...{head['sha']}")
    need(
        comparison.get("status") in {"ahead", "identical"}
        and (comparison.get("base_commit") or {}).get("sha") == commit
        and (comparison.get("merge_base_commit") or {}).get("sha") == commit,
        "verifier caller/helper is not canonical merged source",
    )
    return {"branch": metadata["default_branch"], "sha": head["sha"]}


def _helper_checkout(backend, repository, job, checkout):
    """Port of verifier_pending_execution.acquireRetainedExecution's native log window."""
    log = backend.gh.read_text(f"repos/{repository}/actions/jobs/{job['id']}/logs")
    need(
        type(log) is str and len(log.encode()) <= 16777216,
        "native checkout log missing or oversized",
    )
    start, end = (
        _timestamp(checkout.get("started_at")),
        _timestamp(checkout.get("completed_at")) + 1,
    )
    selected = []
    for line in log.splitlines():
        stamp = line.split(" ", 1)[0]
        try:
            actual = _timestamp(stamp)
        except ReadbackHeld:
            continue
        if start <= actual < end:
            selected.append(line)
    window = "\n".join(selected)
    need(
        "repository: stranske/Workflows" in window and "path: .workflows-lib" in window,
        "native Workflows checkout origin unproven",
    )
    hashes = re.findall(r"git log -1 --format=%H\r?\n[^\n]*?\s([0-9a-f]{40})\s*(?:\n|$)", window)
    need(len(hashes) == 1, "native Workflows checkout revision absent or ambiguous")
    return hashes[0], hashlib.sha256(window.encode()).hexdigest()


def require_deployed(backend, record, branch):
    """Pre-POST support check. The eventual run is independently pinned again."""
    need(bool(DEPLOYED_SOURCE_PINS), "reviewed deployed corrective verifier source unavailable")
    repository = record["pr"]["repository"]
    repo = _document(backend, f"repos/{repository}")
    need(
        repo.get("id") == record["pr"]["repository_id"] and repo.get("default_branch") == branch,
        "native default workflow repository/branch differs",
    )
    own = _document(backend, f"repos/{repository}/branches/{quote(branch,safe='')}")
    central = _document(backend, f"repos/{CENTRAL}/branches/main")
    caller_ref = (own.get("commit") or {}).get("sha")
    central_ref = (central.get("commit") or {}).get("sha")
    need(
        all(
            type(oid) is str and re.fullmatch(r"[a-f0-9]{40}", oid)
            for oid in (caller_ref, central_ref)
        ),
        "native default workflow source identity missing",
    )
    return _source_pin(backend, repository, caller_ref, central_ref)


def _selected_comment(backend, record, snapshot):
    pr = record["pr"]
    selected = snapshot.get("verifier")
    need(
        verifier_evidence.decision_matches_identity(
            selected, pr["repository"], pr["pr_number"], pr["head_oid"], snapshot.get("merge")
        )
        and selected.get("verdict") == "PASS",
        "current selected terminal PASS unavailable",
    )
    matches, native_comments = [], []
    for page in range(1, 6):
        rows = backend.gh.api(
            f"repos/{pr['repository']}/issues/{pr['pr_number']}/comments?per_page=100&page={page}",
            method="GET",
        )
        need(type(rows) is list and len(rows) <= 100, "native terminal comment inventory malformed")
        native_comments.extend(rows)
        for comment in rows:
            if type(comment) is not dict:
                continue
            user = comment.get("user") or {}
            body = comment.get("body")
            if (
                user.get("login") != "github-actions[bot]"
                or user.get("type") != "Bot"
                or type(body) is not str
            ):
                continue
            if len(body.encode()) > 65536:
                continue
            values = verifier_evidence.MARKER_RE.findall(body)
            if len(values) != 1:
                continue
            try:
                raw = strict_json(values[0])
            except (ValueError, TypeError, UnicodeError):
                continue  # Existing causal selector below decides current unknowns.
            if type(raw) is dict and verifier_evidence._public_record(
                raw
            ) == verifier_evidence._public_record(selected):
                need(
                    type(comment.get("id")) is int
                    and comment["id"] > 0
                    and type(comment.get("node_id")) is str
                    and comment["node_id"]
                    and type(comment.get("updated_at")) is str
                    and comment["updated_at"]
                    and comment.get("html_url")
                    == f"https://github.com/{pr['repository']}/pull/{pr['pr_number']}#issuecomment-{comment['id']}"
                    and comment.get("issue_url")
                    == f"https://api.github.com/repos/{pr['repository']}/issues/{pr['pr_number']}",
                    "native terminal comment identity/version differs",
                )
                matches.append((comment, raw))
        if len(rows) < 100:
            break
    else:
        raise ReadbackHeld("native terminal comment inventory truncated")
    # Reuse the existing causal verifier selector over the COMPLETE fresh native
    # inventory. A newly pending/unknown event must not leave an older PASS live.
    current = verifier_evidence.decision_from_pr(
        pr["repository"],
        {
            "number": pr["pr_number"],
            "headRefOid": pr["head_oid"],
            "mergeCommit": {"oid": snapshot.get("merge")},
            "comments": {
                "nodes": [
                    {
                        "author": {"login": (row.get("user") or {}).get("login")},
                        "body": row.get("body"),
                        "url": row.get("html_url"),
                    }
                    for row in native_comments
                    if type(row) is dict
                ]
            },
        },
        include_pending=True,
    )
    need(
        type(current) is dict
        and verifier_evidence._public_record(current) == verifier_evidence._public_record(selected),
        "selected native verifier lifecycle changed",
    )
    need(bool(matches), "selected native terminal comment unavailable")
    need(
        all(raw == matches[0][1] for _, raw in matches),
        "selected terminal corrective metadata conflicts",
    )
    return min(matches, key=lambda row: row[0]["id"])


def _members(archive, names):
    need(
        type(archive) is bytes and len(archive) <= closer_evidence.MAX_ARCHIVE_BYTES,
        "native evaluator archive bound exceeded",
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        infos = z.infolist()
        wanted = {}
        for name in names:
            matches = [item for item in infos if item.filename == name]
            need(len(matches) == 1, "native evaluator member missing or duplicated")
            item = matches[0]
            need(
                not item.is_dir()
                and not stat.S_ISLNK(item.external_attr >> 16)
                and item.file_size <= closer_evidence.MAX_ARCHIVE_BYTES,
                "native evaluator member unsafe or oversized",
            )
            wanted[name] = item
        need(
            sum(item.file_size for item in wanted.values()) <= closer_evidence.MAX_ARCHIVE_BYTES,
            "native evaluator expanded member bound exceeded",
        )
        return {name: z.read(item) for name, item in wanted.items()}


def read(backend, record, snapshot):
    """Return authenticated retained bytes; caller retains existing ACT guards."""
    comment, terminal = _selected_comment(backend, record, snapshot)
    repo = record["pr"]["repository"]
    run_id = str(terminal["run_id"])
    attempt = str(terminal["run_attempt"])
    run = _document(backend, f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
    latest = _document(backend, f"repos/{repo}/actions/runs/{run_id}")
    need(
        str(run.get("id")) == run_id
        and str(run.get("run_attempt")) == attempt
        and str(latest.get("run_attempt")) == attempt
        and run.get("event") == "workflow_dispatch"
        and run.get("path") == WORKFLOW
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and (run.get("repository") or {}).get("id") == record["pr"]["repository_id"]
        and (run.get("repository") or {}).get("full_name") == repo
        and (run.get("head_repository") or {}).get("full_name") == repo
        and (run.get("head_repository") or {}).get("id") == record["pr"]["repository_id"],
        "native evaluator run/attempt/workflow differs",
    )
    callees = [
        row
        for row in run.get("referenced_workflows", [])
        if type(row) is dict
        and type(row.get("path")) is str
        and row["path"].startswith(CENTRAL + "/" + CALLEE + "@")
    ]
    need(
        len(callees) == 1
        and type(callees[0].get("sha")) is str
        and re.fullmatch(r"[a-f0-9]{40}", callees[0]["sha"]),
        "native reusable verifier source missing",
    )
    _canonical_commit(backend, repo, run["head_sha"])
    _canonical_commit(backend, CENTRAL, callees[0]["sha"])
    pin = None  # Actual helper checkout is authenticated below, never inferred from this SHA.
    mode = (terminal.get("assessment_identity") or {}).get("mode")
    need(type(mode) is str and mode in MODES, "native evaluator mode unavailable")
    stem, upload = MODES[mode]
    jobs = _document(
        backend, f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    )
    rows = jobs.get("jobs")
    need(
        type(rows) is list and jobs.get("total_count") == len(rows) and len(rows) < 100,
        "native evaluator job inventory incomplete",
    )
    candidates = [
        job
        for job in rows
        if type(job) is dict
        and (
            job.get("name") == "Run post-merge verifier"
            or str(job.get("name", "")).endswith(" / Run post-merge verifier")
        )
    ]
    need(len(candidates) == 1, "native evaluator job absent or ambiguous")
    job = candidates[0]
    need(
        type(job.get("id")) is int
        and job["id"] > 0
        and job.get("status") == "completed"
        and len({row.get("id") for row in rows}) == len(rows)
        and all(
            type(row) is dict
            and row.get("run_id") == int(run_id)
            and row.get("run_attempt") == int(attempt)
            and row.get("head_sha") == run["head_sha"]
            for row in rows
        ),
        "native evaluator job identity differs",
    )
    steps = job.get("steps")
    expected_step = (terminal.get("execution_binding") or {}).get("evaluator_step")
    need(
        type(steps) is list
        and expected_step in {"Run LLM evaluation", "Run multi-provider comparison"},
        "native evaluator step binding missing",
    )
    proven_steps = []
    for name in (
        "Checkout Workflows scripts",
        "Record verification event pending",
        expected_step,
        upload,
    ):
        exact = [step for step in steps if type(step) is dict and step.get("name") == name]
        need(
            len(exact) == 1
            and exact[0].get("status") == "completed"
            and exact[0].get("conclusion") == "success",
            "native evaluator/upload step not successful",
        )
        step = exact[0]
        need(
            type(step.get("number")) is int and step["number"] > 0,
            "native evaluator step number missing",
        )
        need(
            _timestamp(step.get("started_at")) <= _timestamp(step.get("completed_at")),
            "native evaluator step interval invalid",
        )
        proven_steps.append(step)
    need(
        [step["number"] for step in proven_steps]
        == sorted({step["number"] for step in proven_steps}),
        "native evaluator step ordering differs",
    )
    helper_sha, checkout_log_sha = _helper_checkout(backend, repo, job, proven_steps[0])
    _canonical_commit(backend, CENTRAL, helper_sha)
    pin = _source_pin(backend, repo, run["head_sha"], helper_sha, callees[0]["sha"])
    inventory = _document(backend, f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
    artifacts = inventory.get("artifacts")
    need(
        type(artifacts) is list
        and inventory.get("total_count") == len(artifacts)
        and len(artifacts) < 100,
        "native evaluator artifact inventory incomplete",
    )
    found = [
        artifact
        for artifact in artifacts
        if type(artifact) is dict and artifact.get("name") == stem + "-results-" + run_id
    ]
    need(len(found) == 1, "native evaluator artifact absent or ambiguous")
    artifact = found[0]
    need(
        type(artifact.get("id")) is int
        and artifact["id"] > 0
        and artifact.get("expired") is False
        and (artifact.get("workflow_run") or {}).get("id") == int(run_id)
        and (artifact.get("workflow_run") or {}).get("head_sha") == run["head_sha"]
        and (artifact.get("workflow_run") or {}).get("repository_id")
        == record["pr"]["repository_id"]
        and (artifact.get("workflow_run") or {}).get("head_repository_id")
        == record["pr"]["repository_id"]
        and type(artifact.get("size_in_bytes")) is int
        and 0 < artifact["size_in_bytes"] <= closer_evidence.MAX_ARCHIVE_BYTES
        and _timestamp(proven_steps[-1]["started_at"])
        <= _timestamp(artifact.get("created_at"))
        <= _timestamp(proven_steps[-1]["completed_at"]),
        "native evaluator artifact identity/retention differs",
    )
    import closer_dispatcher

    deadline = time.monotonic() + min(25, max(0, float(backend.gh.deadline) - time.time()))
    archive = closer_dispatcher.native_artifact_fetcher(snapshot)(
        artifact, deadline=deadline, max_bytes=closer_evidence.MAX_ARCHIVE_BYTES
    )
    archive_sha = hashlib.sha256(archive).hexdigest()
    need(
        artifact.get("digest") == "sha256:" + archive_sha, "native evaluator archive digest differs"
    )
    names = (
        "verification-event-manifest.json",
        stem + ".json",
        stem + "-execution.json",
        stem + "-comment.md",
    )
    members = _members(archive, names)
    manifest = strict_json(members[names[0]])
    receipt = strict_json(members[names[2]])
    need(
        type(receipt) is dict
        and receipt.get("schema") == f"workflows-verifier-{stem}-execution/v1"
        and receipt.get("mode") == mode
        and receipt.get("status") == "completed"
        and receipt.get("decision_available") is True
        and type(receipt.get("exit_code")) is int
        and receipt["exit_code"] == 0
        and receipt.get("repository") == repo
        and str(receipt.get("source_run_id")) == run_id
        and str(receipt.get("source_run_attempt")) == attempt,
        "exact evaluator execution receipt identity differs",
    )
    output = (receipt.get("outputs") or {}).get(stem + ".json")
    need(
        type(output) is dict
        and output.get("sha256") == hashlib.sha256(members[names[1]]).hexdigest()
        and type(output.get("bytes")) is int
        and output["bytes"] == len(members[names[1]])
        and output.get("truncated", False) is False
        and output.get("original_complete", True) is True
        and not output.get("retention_error_type"),
        "exact retained evaluator payload differs",
    )
    admission = terminal.get("result_admission")
    need(
        type(admission) is dict
        and admission.get("schema") == "verifier-result-admission/v1"
        and admission.get("mode") == mode
        and admission.get("payload_sha256") == hashlib.sha256(members[names[1]]).hexdigest()
        and admission.get("execution_receipt_sha256")
        == hashlib.sha256(members[names[2]]).hexdigest(),
        "selected terminal evaluator admission differs",
    )
    need(
        members[names[3]].decode("utf-8") == comment["body"],
        "native terminal comment differs from retained settled report",
    )
    compact = terminal.get("corrective_verification_input")
    need(
        type(compact) is dict
        and set(compact) == {"schema", "verification_event", "corrective_instruction"}
        and compact["schema"] == "closer-corrective-verification-input/v1"
        and compact["verification_event"]
        == manifest.get("verification_event")
        == terminal.get("verification_event"),
        "terminal corrective input/event differs",
    )
    # Re-read that exact native comment version after transport, not just its ID.
    current = _document(backend, f"repos/{repo}/issues/comments/{comment['id']}")
    need(
        all(
            current.get(key) == comment.get(key)
            for key in ("id", "node_id", "updated_at", "body", "user", "issue_url", "html_url")
        ),
        "native terminal comment changed during readback",
    )
    settled_comment, settled_terminal = _selected_comment(backend, record, snapshot)
    need(
        settled_comment == comment and settled_terminal == terminal,
        "native terminal lifecycle changed during artifact transport",
    )
    latest_after = _document(backend, f"repos/{repo}/actions/runs/{run_id}")
    need(
        str(latest_after.get("run_attempt")) == attempt
        and latest_after.get("status") == "completed",
        "native evaluator attempt changed during artifact transport",
    )
    return {
        "manifest": manifest,
        "terminal": terminal,
        "compact": compact,
        "native_origin": {
            "run_id": run_id,
            "run_attempt": attempt,
            "artifact_id": artifact["id"],
            "archive_sha256": archive_sha,
            "comment_id": comment["id"],
            "comment_node_id": comment["node_id"],
            "updated_at": comment["updated_at"],
            "comment_sha256": hashlib.sha256(comment["body"].encode()).hexdigest(),
            "source_pin": pin,
            "helper_sha": helper_sha,
            "checkout_log_sha256": checkout_log_sha,
        },
    }
