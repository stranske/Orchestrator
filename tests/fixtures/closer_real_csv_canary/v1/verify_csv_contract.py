"""Explicit standard-library contract script for the Closer CSV canary fixture.

Invoke this file directly. It is not a pytest module: ``__test__ = False`` and
it defines no ``test_*`` functions, so pytest does not collect it. Pass one
``csv_export.py`` path to check that revision; the default is the sibling
module. ``--chain-evidence`` reads the public issue, branch and PR chain and
reports
whether a trusted native verifier decision and closer publication proof are
tied to the corrected head. A free-text verdict line is not that proof. A
trusted corpus decision on the partial merge is reported and is not that proof
either. A corpus decision whose evaluated commit is only the open pull
request's test-merge SHA is reported and is not that proof either: Agents
Verifier refuses an unmerged pull request, and that SHA is not a published
merge. Skipped Agents Verifier runs on earlier commits of the corrective PR,
and publication text that names only the owned delivery PR, are reported and
are not that proof either. A keepalive completion that only observed this head
after starting on an earlier revision is reported and is not worker or
publication proof for this head. The same rule applies to the owned delivery
head: a keepalive receipt that started earlier and only observed that head is
not a worker completion of it and is not closer publication proof. A reduced
publication comment is not the stored producer record. Custody JSON is read
when that record's schema sits inside the object. The command does not change
the default contract exit.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__test__ = False


def _load_export(path=None):
    path = Path(path) if path is not None else Path(__file__).resolve().parent / "csv_export.py"
    spec = importlib.util.spec_from_file_location("closer_csv_canary_export", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _csv_text(module, rows):
    try:
        output = module.export_rows(rows, "csv")
    except NotImplementedError as exc:
        raise AssertionError("CSV contract is not implemented") from exc
    assert isinstance(output, str), "CSV export must return a string"
    return output


def _assert_crlf_records(text, record_count):
    """Record endings outside quoted fields are CRLF, never a bare CR or LF."""
    assert text.startswith("name,score\r\n"), repr(text[:32])
    seen = 0
    index = 0
    in_quotes = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_quotes:
            if char == '"':
                if index + 1 < length and text[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
            index += 1
            continue
        if char == '"':
            in_quotes = True
            index += 1
            continue
        if char in "\r\n":
            assert text.startswith(
                "\r\n", index
            ), f"record terminator is not CRLF at offset {index}"
            seen += 1
            index += 2
            continue
        index += 1
    assert not in_quotes, "unterminated quoted field"
    assert seen == record_count, f"expected {record_count} CRLF records, found {seen}"


def _assert_csv_round_trip(module, rows):
    text = _csv_text(module, rows)
    _assert_crlf_records(text, 1 + len(rows))
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed[0] == ["name", "score"], parsed[:1]
    expected = [[row["name"], str(row["score"])] for row in rows]
    assert parsed[1:] == expected, parsed[1:]
    assert len(parsed) == 1 + len(rows)


def _assert_json(module, rows):
    text = module.export_rows(rows, "json")
    assert text == json.dumps(rows), "JSON serialization changed"
    loaded = json.loads(text)
    assert len(loaded) == len(rows)
    for original, got in zip(rows, loaded, strict=True):
        assert got["name"] == original["name"]
        assert got["score"] == original["score"]


def _assert_unsupported(module, rows):
    for fmt in ("xml", "CSV", "", "tsv"):
        try:
            module.export_rows(rows, fmt)
        except ValueError:
            continue
        except Exception as exc:
            raise AssertionError(
                f"format {fmt!r} raised {type(exc).__name__}, expected ValueError"
            ) from exc
        raise AssertionError(f"format {fmt!r} did not raise ValueError")


def run_contract(path=None):
    module = _load_export(path)
    plain = [{"name": "alpha", "score": 1}, {"name": "beta", "score": 2}]
    _assert_json(module, plain)
    _assert_json(
        module,
        [{"name": "ada", "score": 10, "extra": "kept"}, {"name": "café", "score": 2.5}],
    )
    assert module.export_rows([], "json") == "[]", "empty JSON input did not round-trip as []"
    _assert_unsupported(module, plain)
    empty = _csv_text(module, [])
    assert empty == "name,score\r\n", repr(empty)
    _assert_csv_round_trip(module, plain)
    special = [
        {"name": "a,b", "score": "1"},
        {"name": 'say "hi"', "score": "2"},
        {"name": "line\nbreak", "score": "3"},
        {"name": "cr\rhere", "score": "4"},
        {"name": "both\r\n", "score": 'quote, "and" comma'},
        {"name": 'mix, "q"\r\nend', "score": "5\r\n,x"},
        {"name": 'a,b "c"\n\r', "score": "together"},
    ]
    _assert_csv_round_trip(module, special)


class _ObservationError(RuntimeError):
    """The public chain could not be read, so missing evidence was not observed."""


_PARTIAL_HEAD = "0507d28acf68d6bf21c68138d18d56cd01968827"
_PARTIAL_MERGE = "d1947b5b4775c2e6a07bb46b3f85eb2e28c1b001"
_PARTIAL_PR = "984"
_CORRECTIVE_PR = "1168"
_OWNED_DELIVERY_PR = "989"
_CHAIN_ISSUES = (983, 988)
_CHAIN_PRS = (984, 989, 1166, 1168)
_CHAIN_BRANCHES = (
    "cursor/issue-983",
    "codex/issue-983-declared-csv-canary",
    "codex/test-csv-followup-988-open",
)
_PUBLICATION_MARKERS = (
    "closer-publication-attempt-data/v1",
    "closer-publication-source-handoff/v3",
    "closer-publication-source-handoff-reference/v1",
    "closer-publication-baseline-data/v2",
)
_VERIFIER_MARKER = "verifier-event-state/v1 "
_CORPUS_MARKER = "verifier-corpus-decision/v1 "
_TRUSTED_VERIFIER_AUTHORS = {"github-actions", "github-actions[bot]"}
_PROVIDER_VERDICTS = {"PASS", "CONCERNS", "FAIL", "NON_PASS"}
_VERIFIER_WORKFLOW = "agents-verifier.yml"
_VERIFY_LABELS = ("verify:checkbox", "verify:evaluate", "verify:compare")
_RUNNER_RECEIPT_SCHEMA = "runner-completion-receipt/v1"
_REPOSITORY = "stranske/Orchestrator"
# Stored by closer_coding_publication_io.retain_confirmed_result. A smaller comment is not that record.
_PUBLICATION_SCHEMA = "closer-confirmed-existing-pr-publication/v1"
_RESULT_REFERENCE_SCHEMA = "closer-owned-pr-publication-result-reference/v1"
_AUTHORITY_FLAGS = (
    "git_execution_authorized",
    "native_publication_authorized",
    "provider_execution_authorized",
    "act_authorized",
    "global_stop_valid",
    "acceptance_authorized",
    "domain_completed",
    "merge_authorized",
    "source_closure_authorized",
    "retry_authorized",
)
_STORED_PUBLICATION_KEYS = frozenset(
    {
        "schema",
        "request",
        "original",
        "findings",
        "required_tests",
        "pr",
        "commit",
        "ref",
        "provenance",
        *_AUTHORITY_FLAGS,
    }
)
_OWNED_PR_KEYS = frozenset(
    {
        "repository",
        "repository_id",
        "pr_number",
        "pr_node_id",
        "issue_node_id",
        "owner_login",
        "owner_id",
        "writer_login",
        "writer_id",
        "head_repo_id",
        "base_repo_id",
        "head_ref",
        "head_oid",
        "base_ref",
        "base_oid",
        "body_sha256",
        "state",
        "is_draft",
        "is_merged",
        "closing_issue_node_ids",
    }
)
_NATIVE_COMMIT_KEYS = frozenset({"repository_id", "oid", "tree_oid", "parent_oids"})
_NATIVE_REF_KEYS = frozenset({"repository_id", "ref", "oid", "object_type"})
_PROVENANCE_KEYS = frozenset(
    {
        "handoff_reference",
        "coding_terminal_reference",
        "publication_origin_reference",
        "io_admission_reference",
        "io_operation_sha256",
        "push_ack_sha256",
        "native_readback_sha256",
        "index_binding_sha256",
        "original_admission_sha256",
        "original_operation_sha256",
        "source_closure_sha256",
        "scope_issue",
    }
)


def _marked_objects(text, marker):
    """Decode marker JSON objects. A broken marker does not hide a later one."""
    found_objects = []
    cursor = 0
    while True:
        found = text.find(marker, cursor)
        if found < 0:
            break
        payload = text[found + len(marker) :]
        brace = payload.find("{")
        boundary = payload.find("-->")
        next_marker = payload.find(marker)
        if brace < 0 or any(0 <= end < brace for end in (boundary, next_marker)):
            cursor = found + len(marker)
            continue
        try:
            event, consumed = json.JSONDecoder().raw_decode(payload[brace:])
        except json.JSONDecodeError:
            cursor = found + len(marker)
            continue
        if isinstance(event, dict):
            found_objects.append(event)
        cursor = found + len(marker) + brace + consumed
    return found_objects


def _verifier_events(text):
    return [
        event
        for event in _marked_objects(text, _VERIFIER_MARKER)
        if event.get("schema") == "verifier-event-state/v1"
    ]


def _positive_identifier(value):
    text = str(value or "")
    return bool(text) and text.isdigit() and not text.startswith("0")


def _corpus_verdict_ok(event):
    providers = event.get("provider_verdicts")
    if (
        not _positive_identifier(event.get("run_id"))
        or not _positive_identifier(event.get("run_attempt"))
        or not isinstance(providers, list)
        or not providers
        or len(providers) > 16
        or any(not isinstance(item, str) or item not in _PROVIDER_VERDICTS for item in providers)
        or type(event.get("ci_failed")) is not bool
    ):
        return False
    expected = (
        "PASS"
        if not event["ci_failed"] and all(item == "PASS" for item in providers)
        else "NON_PASS"
    )
    return event.get("verdict") == expected


def _corpus_identity_ok(event):
    return (
        isinstance(event, dict)
        and event.get("schema") == "verifier-corpus-decision/v1"
        and str(event.get("repo") or "").lower() == _REPOSITORY.lower()
        and _corpus_verdict_ok(event)
    )


def _sha40(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def _valid_corpus_decision(event, corrective_sha):
    """A terminal decision names this head and a different evaluated commit.

    Binding that commit to a published merge is a separate check. The partial
    revision's head and merge are never a corrective evaluated commit.
    """
    evaluated = event.get("evaluated_sha")
    return (
        _corpus_identity_ok(event)
        and str(event.get("pr")) == _CORRECTIVE_PR
        and event.get("head_sha") == corrective_sha
        and _sha40(corrective_sha)
        and corrective_sha not in {_PARTIAL_HEAD, _PARTIAL_MERGE}
        and _sha40(evaluated)
        and evaluated not in {_PARTIAL_HEAD, _PARTIAL_MERGE, corrective_sha}
    )


def _published_merge_sha(record, corrective_sha):
    """The merge commit of a merged pull request, never an open test-merge SHA."""
    merge_sha = record.get("merge_sha") or ""
    if (
        record.get("merged") is True
        and _sha40(merge_sha)
        and _sha40(corrective_sha)
        and merge_sha not in {_PARTIAL_HEAD, _PARTIAL_MERGE, corrective_sha}
    ):
        return merge_sha
    return ""


def _merge_bound_corpus(corpus, corrective_pr, corrective_sha):
    """Keep only decisions evaluated at the published merge commit."""
    merge_sha = _published_merge_sha(corrective_pr, corrective_sha)
    bound = []
    unbound = []
    for decision in corpus:
        if (
            merge_sha
            and decision.get("head_sha") == corrective_sha
            and decision.get("evaluated_sha") == merge_sha
        ):
            bound.append(decision)
        else:
            unbound.append(decision)
    return bound, unbound


def _valid_partial_corpus_decision(event):
    """The partial merge's own decision is evidence about that revision only."""
    return (
        _corpus_identity_ok(event)
        and str(event.get("pr")) == _PARTIAL_PR
        and event.get("head_sha") == _PARTIAL_HEAD
        and event.get("evaluated_sha") == _PARTIAL_MERGE
    )


def _summarize_corpus(event):
    return {
        "verdict": event.get("verdict"),
        "run_id": event.get("run_id"),
        "run_attempt": event.get("run_attempt"),
        "head_sha": event.get("head_sha"),
        "evaluated_sha": event.get("evaluated_sha"),
    }


def _corpus_from_comment(record, corrective_sha):
    """Accept only the native verifier's own pull-request comment."""
    if not _native_comment(record, _CORRECTIVE_PR, _TRUSTED_VERIFIER_AUTHORS):
        return []
    return [
        _summarize_corpus(event)
        for event in _marked_objects(record.get("body") or "", _CORPUS_MARKER)
        if _valid_corpus_decision(event, corrective_sha)
    ]


def _partial_corpus_from_comment(record):
    """Accept only the native verifier's own comment on the partial pull request."""
    if not _native_comment(record, _PARTIAL_PR, _TRUSTED_VERIFIER_AUTHORS):
        return []
    return [
        _summarize_corpus(event)
        for event in _marked_objects(record.get("body") or "", _CORPUS_MARKER)
        if _valid_partial_corpus_decision(event)
    ]


def _event_bound_to(event, sha):
    return sha in (event.get("head_sha"), event.get("evaluated_sha"))


def _non_pending_state(event):
    status = event.get("status")
    return isinstance(status, str) and status not in {"", "pending"}


def _summarize_event(event):
    return {
        "pr": str(event.get("pr")),
        "status": event.get("status"),
        "run_id": event.get("run_id"),
        "head_sha": event.get("head_sha"),
        "evaluated_sha": event.get("evaluated_sha"),
        "failure_cause": event.get("failure_cause"),
    }


def _classify_events(events, corrective_sha):
    """Separate diagnostic states by revision; only corpus decisions prove completion."""
    partial = []
    corrective = []
    rejected_partial_sha = corrective_sha in {_PARTIAL_HEAD, _PARTIAL_MERGE}
    for event in events:
        summary = _summarize_event(event)
        bound_partial = _event_bound_to(event, _PARTIAL_HEAD) or _event_bound_to(
            event, _PARTIAL_MERGE
        )
        if bound_partial and str(event.get("pr")) == _PARTIAL_PR:
            partial.append(summary)
        if (
            corrective_sha
            and not rejected_partial_sha
            and _event_bound_to(event, corrective_sha)
            and str(event.get("pr")) == _CORRECTIVE_PR
            and _non_pending_state(event)
        ):
            corrective.append(summary)
    return partial, corrective


def _native_comment(record, number, authors):
    return (
        record.get("author") in authors
        and re.fullmatch(
            re.escape(f"https://github.com/{_REPOSITORY}/pull/{number}#issuecomment-")
            + r"[1-9][0-9]*",
            str(record.get("url") or ""),
        )
        is not None
    )


def _events_from_comment(record, number, head, merge):
    if not _native_comment(record, number, _TRUSTED_VERIFIER_AUTHORS):
        return []
    return [
        event
        for event in _verifier_events(record["body"])
        if event.get("repo") == _REPOSITORY.lower()
        and str(event.get("pr")) == str(number)
        and isinstance(head, str)
        and re.fullmatch(r"[a-f0-9]{40}", head)
        and isinstance(merge, str)
        and re.fullmatch(r"[a-f0-9]{40}", merge)
        and event.get("head_sha") == head
        and event.get("evaluated_sha") == merge
        and _positive_identifier(event.get("run_id"))
        and _positive_identifier(event.get("run_attempt"))
        and event.get("status") in ("pending", "incomplete")
    ]


def _comment_body(comment):
    return comment.get("body", "") if isinstance(comment, dict) else comment


def _positive_int(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def _sha256_text(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _publication_objects(text):
    """Read marker-prefixed records and custody JSON whose schema is inside the object."""
    found = []
    for event in _marked_objects(text, _PUBLICATION_SCHEMA):
        if isinstance(event, dict):
            found.append(event)
    cursor = 0
    while True:
        brace = text.find("{", cursor)
        if brace < 0:
            break
        try:
            event, consumed = json.JSONDecoder().raw_decode(text[brace:])
        except json.JSONDecodeError:
            cursor = brace + 1
            continue
        if consumed <= 0:
            cursor = brace + 1
            continue
        cursor = brace + consumed
        if isinstance(event, dict) and event.get("schema") == _PUBLICATION_SCHEMA:
            found.append(event)
    return found


def _reference_ok(reference, schema, prefix):
    return (
        isinstance(reference, dict)
        and reference.get("schema") == schema
        and _sha256_text(reference.get("sha256"))
        and reference.get("name") == prefix + reference["sha256"] + ".json"
    )


def _optional_result_reference(value):
    extra = set(value) - set(_STORED_PUBLICATION_KEYS)
    if extra not in (set(), {"publication_result_reference"}):
        return False
    if not set(_STORED_PUBLICATION_KEYS) <= set(value):
        return False
    if not extra:
        return True
    return _reference_ok(
        value.get("publication_result_reference"),
        _RESULT_REFERENCE_SCHEMA,
        "publication-result-",
    )


def _producer_publication(value, corrective_sha):
    """Accept the stored result shape only. A reduced comment is not custody."""
    if (
        not isinstance(value, dict)
        or value.get("schema") != _PUBLICATION_SCHEMA
        or not _optional_result_reference(value)
        or any(value.get(flag) is not False for flag in _AUTHORITY_FLAGS)
        or type(value.get("request")) is not dict
        or type(value.get("original")) is not dict
        or type(value.get("findings")) is not list
        or type(value.get("required_tests")) is not list
    ):
        return False
    pr = value.get("pr")
    commit = value.get("commit")
    ref = value.get("ref")
    provenance = value.get("provenance")
    if not all(isinstance(item, dict) for item in (pr, commit, ref, provenance)):
        return False
    if set(pr) != set(_OWNED_PR_KEYS) or set(commit) != set(_NATIVE_COMMIT_KEYS):
        return False
    if set(ref) != set(_NATIVE_REF_KEYS):
        return False
    provenance_extra = set(provenance) - set(_PROVENANCE_KEYS)
    if provenance_extra not in (set(), {"publication_result_reference"}):
        return False
    if not set(_PROVENANCE_KEYS) <= set(provenance):
        return False
    if provenance_extra and not _reference_ok(
        provenance.get("publication_result_reference"),
        _RESULT_REFERENCE_SCHEMA,
        "publication-result-",
    ):
        return False
    number = str(pr.get("pr_number"))
    repo_id = pr.get("repository_id")
    parent_oids = commit.get("parent_oids")
    scope = provenance.get("scope_issue")
    if (
        type(pr.get("pr_number")) is not int
        or number not in (_CORRECTIVE_PR, _OWNED_DELIVERY_PR)
        or pr.get("repository") != _REPOSITORY.lower()
        or pr.get("owner_login") != "stranske"
        or pr.get("writer_login") != "stranske"
        or pr.get("owner_id") != 23046322
        or pr.get("writer_id") != 23046322
        or not _positive_int(repo_id)
        or pr.get("head_repo_id") != repo_id
        or pr.get("base_repo_id") != repo_id
        or pr.get("head_oid") != corrective_sha
        or pr.get("state") != "open"
        or pr.get("is_draft") is not False
        or pr.get("is_merged") is not False
        or not isinstance(pr.get("pr_node_id"), str)
        or not pr["pr_node_id"]
        or not isinstance(pr.get("issue_node_id"), str)
        or not pr["issue_node_id"]
        or pr.get("closing_issue_node_ids") != [pr["issue_node_id"]]
        or not isinstance(pr.get("head_ref"), str)
        or not pr["head_ref"]
        or not isinstance(pr.get("base_ref"), str)
        or not pr["base_ref"]
        or pr["base_ref"] == pr["head_ref"]
        or not _sha40(pr.get("base_oid"))
        or not _sha256_text(pr.get("body_sha256"))
        or commit.get("repository_id") != repo_id
        or commit.get("oid") != corrective_sha
        or not _sha40(commit.get("tree_oid"))
        or type(parent_oids) is not list
        or not parent_oids
        or any(not _sha40(item) for item in parent_oids)
        or ref.get("repository_id") != repo_id
        or ref.get("oid") != corrective_sha
        or ref.get("ref") != "refs/heads/" + pr["head_ref"]
        or ref.get("object_type") != "commit"
        or type(scope) is not dict
        or set(scope) != {"number", "node_id", "body_sha256"}
        or not _positive_int(scope.get("number"))
        or not isinstance(scope.get("node_id"), str)
        or not scope["node_id"]
        or not _sha256_text(scope.get("body_sha256"))
    ):
        return False
    references = {
        "handoff_reference": (
            "closer-publication-source-handoff-reference/v1",
            "publication-source-handoff-",
        ),
        "coding_terminal_reference": (
            "closer-coding-worker-terminal-reference/v1",
            "coding-worker-terminal-",
        ),
        "publication_origin_reference": (
            "closer-native-owned-pr-publication-origin-reference/v1",
            "native-publication-origin-",
        ),
        "io_admission_reference": (
            "closer-publication-io-admission-reference/v1",
            "publication-io-admission-",
        ),
    }
    for key, (schema, prefix) in references.items():
        if not _reference_ok(provenance.get(key), schema, prefix):
            return False
    for key in (
        "io_operation_sha256",
        "push_ack_sha256",
        "native_readback_sha256",
        "index_binding_sha256",
        "original_admission_sha256",
        "original_operation_sha256",
        "source_closure_sha256",
    ):
        if not _sha256_text(provenance.get(key)):
            return False
    # Same canonical bytes as the producer's native readback digest.
    try:
        readback = {
            "schema": "closer-owned-pr-readback-data/v1",
            "pr": pr,
            "commit": commit,
            "ref": ref,
            "native_publication_authorized": False,
        }
        digest = hashlib.sha256(
            json.dumps(
                readback,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    except (TypeError, ValueError):
        return False
    return provenance.get("native_readback_sha256") == digest


def _publication_tied(comments, corrective_sha):
    """Read confirmed publisher records, never pre-publication DATA or SHA prose.

    retain_confirmed_result stores this shape in local custody. Its native writer
    is the owned PR author (stranske), not github-actions. A reduced comment is
    not that record. Custody JSON is recognized when the schema is inside the
    object. Until a genuine record is exposed, its absence remains incomplete.
    """
    if not corrective_sha or corrective_sha in (_PARTIAL_HEAD, _PARTIAL_MERGE):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        for value in _publication_objects(comment.get("body") or ""):
            pr = value.get("pr")
            number = str(pr.get("pr_number")) if isinstance(pr, dict) else ""
            if number not in (_CORRECTIVE_PR, _OWNED_DELIVERY_PR):
                continue
            if not _native_comment(comment, number, ("stranske",)):
                continue
            if _producer_publication(value, corrective_sha):
                return True
    return False


def _publication_markers(comments):
    found = []
    for comment in comments:
        body = _comment_body(comment)
        for marker in _PUBLICATION_MARKERS:
            if marker in body and marker not in found:
                found.append(marker)
    return found


def _decode_receipt(blob):
    padded = blob + ("=" * ((4 - len(blob) % 4) % 4))
    try:
        payload = json.loads(base64.b64decode(padded, validate=True))
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _receipt_blobs(text):
    """Split keepalive receipt comments without treating their payload as proof."""
    found = []
    cursor = 0
    prefix = "<!-- runner-"
    while True:
        start = text.find(prefix, cursor)
        if start < 0:
            break
        kind_end = text.find(":", start + len(prefix))
        kind = text[start + len(prefix) : kind_end] if kind_end > start else ""
        blob_at = text.find("base64:", start)
        end = text.find(" -->", start)
        if kind not in {"completion", "reservation"} or blob_at < 0 or end < 0 or blob_at > end:
            cursor = start + len(prefix)
            continue
        found.append((kind, text[blob_at + len("base64:") : end].strip()))
        cursor = end + 4
    return found


def _keepalive_for_head(comments, corrective_sha):
    """Keepalive receipts name a worker run. They are not Verify or publication.

    A completion belongs to the head it started on. ``observed_head_sha`` is the
    commit that run pushed. Counting that observation as a completion of the new
    head would treat the previous round as worker proof for this one.
    """
    completions = []
    reservations = []
    observed_only = []
    if not corrective_sha or corrective_sha in {_PARTIAL_HEAD, _PARTIAL_MERGE}:
        return completions, reservations, observed_only
    for comment in comments:
        for kind, blob in _receipt_blobs(_comment_body(comment)):
            payload = _decode_receipt(blob)
            if payload is None:
                continue
            record = payload.get("record")
            if not isinstance(record, dict):
                record = payload
            owns = corrective_sha in (record.get("head_sha"), payload.get("head_sha"))
            observed = record.get("observed_head_sha")
            if owns:
                if kind == "completion":
                    if payload.get("schema") != _RUNNER_RECEIPT_SCHEMA:
                        continue
                    completions.append(str(record.get("status") or ""))
                else:
                    reservations.append(str(record.get("status") or ""))
            elif (
                kind == "completion"
                and observed == corrective_sha
                and payload.get("schema") == _RUNNER_RECEIPT_SCHEMA
            ):
                observed_only.append(
                    {
                        "start_head": str(record.get("head_sha") or ""),
                        "status": str(record.get("status") or ""),
                    }
                )
    return completions, reservations, observed_only


def _record_named(records, kind, name):
    for record in records:
        if record.get("kind") == kind and str(record.get("name")) == name:
            return record
    return {}


def _corrective_record(records):
    return _record_named(records, "pr", _CORRECTIVE_PR)


def _owned_delivery_record(records):
    return _record_named(records, "pr", _OWNED_DELIVERY_PR)


def _text_labeled_test(title, labels):
    folded = (title or "").lstrip().lower()
    if folded.startswith("test:") or folded.startswith("test "):
        return True
    return any(str(label).lower() == "test" for label in labels)


def _branch_labeled_test(name):
    return "test" in (name or "").lower()


def _branch_label_reason(record, records):
    """A branch is visibly a test when its name says so, or its chain PR does.

    GitHub branch refs have no label API. A rename that only copies the ref is
    visible through ``visible_name``. The live head ``cursor/issue-983`` cannot
    be renamed without the bootstrap recreating the unlabeled name, so the PR
    that points at it has to carry the ``test`` label or a ``test:`` title.
    """
    if not record.get("present"):
        return ""
    if _branch_labeled_test(record.get("name") or ""):
        return "name"
    visible = record.get("visible_name") or ""
    if visible and visible != record.get("name") and _branch_labeled_test(visible):
        return "visible-name"
    name = record.get("name")
    for other in records:
        if other.get("kind") != "pr" or other.get("head_ref") != name:
            continue
        if _text_labeled_test(other.get("title"), other.get("labels") or []):
            return "pr:" + str(other.get("name"))
    return ""


def _verdict_lines(text):
    """Return the comment's headline verdict, not later provider-table cells."""
    needle = "**Verdict:**"
    start = text.find(needle)
    if start < 0:
        return []
    rest = text[start + len(needle) :].lstrip()
    token = rest.split(None, 1)[0].strip("*.`,") if rest else ""
    if token and all(character.isupper() or character == "_" for character in token):
        return [token]
    return []


def _assess(
    events,
    comments,
    records,
    corrective_sha,
    partial_verdicts=(),
    corrective_verdicts=(),
    corpus=(),
    partial_corpus=(),
    unbound_corpus=(),
):
    partial, corrective = _classify_events(events, corrective_sha)
    unlabeled = []
    branch_labels = []
    for record in records:
        if record["kind"] == "branch":
            via = _branch_label_reason(record, records)
            branch_labels.append({"name": record["name"], "via": via})
            labeled = bool(via)
        else:
            labeled = _text_labeled_test(record.get("title"), record.get("labels") or [])
        if not labeled:
            unlabeled.append(record)
    completions, reservations, observed_only = _keepalive_for_head(comments, corrective_sha)
    owned = _owned_delivery_record(records)
    owned_head = owned.get("head_sha") or ""
    same_owned_head = bool(owned_head) and owned_head == corrective_sha
    if same_owned_head or not owned_head:
        owned_completions, owned_reservations, owned_observed = [], [], []
    else:
        owned_completions, owned_reservations, owned_observed = _keepalive_for_head(
            comments, owned_head
        )
    return {
        "partial": partial,
        "corrective": corrective,
        "partial_verdicts": list(partial_verdicts),
        "corrective_verdicts": list(corrective_verdicts),
        "corpus": list(corpus),
        "unbound_corpus": list(unbound_corpus),
        "partial_corpus": list(partial_corpus),
        "publication": _publication_tied(comments, corrective_sha),
        "publication_markers": _publication_markers(comments),
        "keepalive_completions": completions,
        "keepalive_reservations": reservations,
        "keepalive_observed_only": observed_only,
        "corrective_pr": _corrective_record(records),
        "owned_delivery": owned,
        "owned_publication": (
            False if same_owned_head or not owned_head else _publication_tied(comments, owned_head)
        ),
        "owned_worker_completions": owned_completions,
        "owned_worker_reservations": owned_reservations,
        "owned_worker_observed_only": owned_observed,
        "unlabeled": unlabeled,
        "branch_labels": branch_labels,
    }


def _format_report(assessment, corrective_sha, local_head, verifier_runs=(), earlier_runs=()):
    lines = [
        "chain-evidence",
        f"repository: {_REPOSITORY}",
        f"corrective_pr: {_CORRECTIVE_PR}",
        f"corrective_head: {corrective_sha or 'unobserved'}",
        f"local_head: {local_head or 'unobserved'}",
        f"partial_pr: {_PARTIAL_PR}",
        f"partial_head: {_PARTIAL_HEAD}",
        f"partial_merge: {_PARTIAL_MERGE}",
        "partial_merge_satisfies_criterion: no",
    ]
    if local_head and corrective_sha and local_head != corrective_sha:
        lines.append("local_head_matches_corrective_pr: no")
    partial_events = assessment["partial"]
    partial_outcomes = [event for event in partial_events if _non_pending_state(event)]
    partial_pending = len(partial_events) - len(partial_outcomes)
    if partial_events or assessment["partial_verdicts"]:
        lines.append(
            "partial_native_verify: "
            + f"{len(partial_outcomes)} non-pending event(s) and {partial_pending} pending "
            + "event(s) on pr 984; not accepted for the corrected head"
        )
        for event in partial_events:
            lines.append(
                "partial_verifier_event: "
                + f"status={event['status']} run_id={event['run_id']} "
                + f"head={event['head_sha']} evaluated={event['evaluated_sha']} "
                + f"failure_cause={event['failure_cause']}"
            )
        for verdict in assessment["partial_verdicts"]:
            lines.append(
                "partial_verifier_verdict: "
                + f"{verdict} on pr 984; not accepted for the corrected head"
            )
    else:
        lines.append("partial_native_verify: none_observed")
    if assessment.get("partial_corpus"):
        for decision in assessment["partial_corpus"]:
            lines.append(
                "partial_corpus_decision: "
                + f"verdict={decision['verdict']} run_id={decision['run_id']} "
                + f"attempt={decision['run_attempt']} head={decision['head_sha']} "
                + f"evaluated={decision['evaluated_sha']} "
                + "on pr 984; not accepted for the corrected head"
            )
    else:
        lines.append(
            "partial_corpus_decision: none_observed "
            "no trusted verifier-corpus-decision/v1 on PR 984 is bound to the partial head and merge"
        )
    corrective_pr = assessment.get("corrective_pr") or {}
    bound_corpus, unbound_corpus = _merge_bound_corpus(
        list(assessment.get("corpus") or []) + list(assessment.get("unbound_corpus") or []),
        corrective_pr,
        corrective_sha,
    )
    if bound_corpus:
        lines.append("native_verify_for_corrective_head: PRESENT")
        for decision in bound_corpus:
            lines.append(
                "corrective_corpus_decision: "
                + f"verdict={decision['verdict']} run_id={decision['run_id']} "
                + f"attempt={decision['run_attempt']} head={decision['head_sha']} "
                + f"evaluated={decision['evaluated_sha']}"
            )
    else:
        lines.append(
            "native_verify_for_corrective_head: MISSING "
            "no trusted verifier-corpus-decision/v1 on PR 1168 is bound to its merged commit"
        )
    for decision in unbound_corpus:
        lines.append(
            "corpus_decision_not_on_merged_commit: "
            + f"verdict={decision['verdict']} run_id={decision['run_id']} "
            + f"attempt={decision['run_attempt']} head={decision['head_sha']} "
            + f"evaluated={decision['evaluated_sha']} "
            + "not a native verify outcome"
        )
    for event in assessment["corrective"]:
        lines.append(
            "corrective_verifier_state_not_completion_proof: "
            + f"status={event['status']} run_id={event['run_id']} "
            + f"head={event['head_sha']} evaluated={event['evaluated_sha']}"
        )

    for verdict in assessment["corrective_verdicts"]:
        lines.append(f"unstructured_verdict: {verdict} not accepted as native proof")
    if verifier_runs:
        lines.append(
            "agents_verifier_workflow_for_corrective_head: observed; a run is not a decision"
        )
        for run in verifier_runs:
            lines.append(
                "agents_verifier_run: "
                + f"id={run['id']} event={run['event']} status={run['status']} "
                + f"conclusion={run['conclusion']}"
            )
    else:
        lines.append(
            "agents_verifier_workflow_for_corrective_head: NONE "
            "no Agents Verifier run is bound to this head; it runs after merge"
        )
    if earlier_runs:
        lines.append(
            "agents_verifier_earlier_pr_commits: "
            + f"{len(earlier_runs)} run(s) on earlier commits of PR {_CORRECTIVE_PR}; "
            + "not a decision for the current head"
        )
        for run in earlier_runs:
            lines.append(
                "agents_verifier_earlier_run: "
                + f"id={run['id']} head={run['head_sha']} event={run['event']} "
                + f"status={run['status']} conclusion={run['conclusion']}"
            )
    else:
        lines.append("agents_verifier_earlier_pr_commits: none")
    if assessment["publication"]:
        lines.append("closer_publication_proof_for_corrective_head: PRESENT")
    else:
        lines.append(
            "closer_publication_proof_for_corrective_head: MISSING "
            "no stored retain_confirmed_result record for the corrective head; "
            "local custody not observed; a reduced comment is not that record"
        )
    markers = assessment.get("publication_markers") or []
    if markers:
        lines.append("publication_markers_observed: " + ",".join(markers))
    else:
        lines.append("publication_markers_observed: none")
    completions = assessment.get("keepalive_completions") or []
    reservations = assessment.get("keepalive_reservations") or []
    observed_only = assessment.get("keepalive_observed_only") or []
    if completions or reservations:
        lines.append(
            "keepalive_runner_for_corrective_head: "
            + f"completions={len(completions)} reservations={len(reservations)} "
            + "not accepted as native verify or publication proof"
        )
    else:
        lines.append(
            "keepalive_runner_for_corrective_head: none "
            "for this head; a keepalive receipt would not be publication proof"
        )
    if observed_only:
        lines.append(
            "keepalive_prior_completion_observed_head: "
            + f"{len(observed_only)} completion(s) started on another head and "
            + "observed this head; not a worker completion of this head and not "
            + "publication proof"
        )
        for item in observed_only:
            lines.append(
                "keepalive_prior_completion: "
                + f"start={item['start_head']} status={item['status']}"
            )
    else:
        lines.append("keepalive_prior_completion_observed_head: none")
    if "merged" in corrective_pr:
        if corrective_pr["merged"]:
            lines.append(
                "corrective_pr_merged: yes merge_sha="
                + (corrective_pr.get("merge_sha") or "missing")
            )
        else:
            lines.append("corrective_pr_merged: no")
            prospective = corrective_pr.get("merge_sha") or ""
            if _sha40(prospective):
                lines.append(
                    "open_pr_test_merge_sha: "
                    + prospective
                    + " not a native Verify evaluated commit"
                )
            lines.append(
                "native_verify_blocker: PR "
                + _CORRECTIVE_PR
                + " is unmerged; Agents Verifier refuses an unmerged pull request"
            )
    if corrective_pr:
        labels = [str(label) for label in (corrective_pr.get("labels") or [])]
        verify_labels = [label for label in _VERIFY_LABELS if label in labels]
        if verify_labels:
            lines.append("verify_label_on_corrective_pr: " + ",".join(verify_labels))
        else:
            lines.append(
                "verify_label_on_corrective_pr: absent "
                + "verify:checkbox, verify:evaluate, verify:compare"
            )
    owned = assessment.get("owned_delivery") or {}
    if owned:
        owned_head = owned.get("head_sha") or "missing"
        merged = "yes" if owned.get("merged") else "no"
        lines.append(
            "owned_delivery_pr: " + f"{_OWNED_DELIVERY_PR} head={owned_head} merged={merged}"
        )
        if owned_head == corrective_sha:
            lines.append(
                "owned_delivery_publication: same head as corrective pr; not separate proof"
            )
        elif assessment.get("owned_publication"):
            lines.append(
                "owned_delivery_publication: PRESENT "
                "marker names the owned delivery head; not accepted for corrective pr "
                + _CORRECTIVE_PR
            )
        else:
            lines.append(
                "owned_delivery_publication: MISSING "
                "no stored retain_confirmed_result record for the owned delivery head; "
                "a reduced comment is not that record"
            )
        if owned_head != corrective_sha:
            owned_completions = assessment.get("owned_worker_completions") or []
            owned_reservations = assessment.get("owned_worker_reservations") or []
            owned_observed = assessment.get("owned_worker_observed_only") or []
            if owned_completions or owned_reservations:
                lines.append(
                    "owned_delivery_worker: "
                    + f"completions={len(owned_completions)} "
                    + f"reservations={len(owned_reservations)} "
                    + "not accepted as native verify or publication proof"
                )
            else:
                lines.append(
                    "owned_delivery_worker: none "
                    "for this head; a keepalive receipt would not be publication proof"
                )
            if owned_observed:
                lines.append(
                    "owned_delivery_worker_prior_observation: "
                    + f"{len(owned_observed)} completion(s) started on another head and "
                    + "observed the owned delivery head; not a worker completion of that "
                    + "head and not publication proof"
                )
                for item in owned_observed:
                    lines.append(
                        "owned_delivery_worker_prior: "
                        + f"start={item['start_head']} status={item['status']}"
                    )
            else:
                lines.append("owned_delivery_worker_prior_observation: none")
    for item in assessment.get("branch_labels") or []:
        if item["via"]:
            lines.append(f"branch_label: {item['name']} labeled=yes via={item['via']}")
        else:
            lines.append(f"branch_label: {item['name']} labeled=no")
    if assessment["unlabeled"]:
        lines.append("test_labeled_chain: MISSING")
        for record in assessment["unlabeled"]:
            lines.append(
                "unlabeled: "
                + f"{record['kind']} {record['name']} title={record.get('title')!r} "
                + f"labels={list(record.get('labels') or [])} present={record.get('present')}"
            )
    else:
        lines.append("test_labeled_chain: PRESENT")
    native_present = bool(bound_corpus)
    # Completion requires the published merge's own decision and publication marker.
    complete = (
        native_present
        and assessment["publication"]
        and not assessment["unlabeled"]
        and corrective_pr.get("merged") is True
    )
    lines.append("completion: evidence present" if complete else "completion: evidence incomplete")
    return "\n".join(lines), 0 if complete else 2


def _repo_root():
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def _local_head():
    root = _repo_root()
    if root is None:
        return None
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = output.strip()
    return sha if len(sha) == 40 else None


def _live_fetch(path):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise _ObservationError("GitHub token missing; chain evidence was not observed")
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "closer-csv-canary-chain-evidence",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        raise _ObservationError(f"GitHub request failed for {path}: {exc}") from exc


def _decode_json(status, raw, label):
    if status != 200:
        raise _ObservationError(f"{label} returned HTTP {status}")
    try:
        return json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ObservationError(f"{label} was not JSON") from exc


def _comment_records(fetch, number):
    """Read every comment page. A full last page is an observation failure, not absence."""
    records = []
    max_pages = 5
    for page in range(1, max_pages + 1):
        status, raw = fetch(
            f"/repos/{_REPOSITORY}/issues/{number}/comments?per_page=100&page={page}"
        )
        batch = _decode_json(status, raw, f"comments for {number}")
        if not isinstance(batch, list):
            raise _ObservationError(f"comments for {number} were not a list")
        for item in batch:
            if not isinstance(item, dict):
                continue
            user = item.get("user")
            author = user.get("login") if isinstance(user, dict) else ""
            records.append(
                {
                    "body": item.get("body") or "",
                    "author": author or "",
                    "url": item.get("html_url") or "",
                }
            )
        if len(batch) < 100:
            return records
    raise _ObservationError(f"comments for {number} exceeded {max_pages} pages")


def _verifier_workflow_runs(fetch, sha):
    if not sha or sha in {_PARTIAL_HEAD, _PARTIAL_MERGE}:
        return []
    status, raw = fetch(
        "/repos/"
        + _REPOSITORY
        + "/actions/workflows/"
        + _VERIFIER_WORKFLOW
        + "/runs?head_sha="
        + sha
        + "&per_page=20"
    )
    if status == 404:
        return []
    document = _decode_json(status, raw, "agents verifier runs")
    runs = document.get("workflow_runs") if isinstance(document, dict) else None
    if not isinstance(runs, list):
        raise _ObservationError("agents verifier runs were not a list")
    bound = []
    for run in runs:
        if not isinstance(run, dict) or run.get("head_sha") != sha:
            continue
        bound.append(
            {
                "id": run.get("id"),
                "event": run.get("event"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
            }
        )
    return bound


def _bound_verifier_runs(fetch, corrective_sha, merge_sha):
    """A run on the partial merge is not evidence for the corrected head.

    After a real merge, workflow_dispatch records the default-branch SHA. That
    SHA can be this PR's merge commit. The partial merge commit stays excluded.
    """
    runs = _verifier_workflow_runs(fetch, corrective_sha)
    if not merge_sha or merge_sha in {corrective_sha, _PARTIAL_HEAD, _PARTIAL_MERGE}:
        return runs
    seen = {run["id"] for run in runs}
    for run in _verifier_workflow_runs(fetch, merge_sha):
        if run["id"] not in seen:
            runs.append(run)
    return runs


def _pull_commit_shas(fetch, number):
    """One page of pull commits. A full page is an observation failure, not the head."""
    status, raw = fetch(f"/repos/{_REPOSITORY}/pulls/{number}/commits?per_page=100")
    document = _decode_json(status, raw, f"commits for {number}")
    if not isinstance(document, list):
        raise _ObservationError(f"commits for {number} were not a list")
    if len(document) >= 100:
        raise _ObservationError(f"commits for {number} exceeded one page")
    shas = []
    for item in document:
        sha = item.get("sha") if isinstance(item, dict) else None
        if not isinstance(sha, str) or len(sha) != 40:
            raise _ObservationError(f"commits for {number} omitted a sha")
        shas.append(sha)
    return shas


def _branch_verifier_runs(fetch, branch):
    """Runs for the corrective branch. Truncation is a failure, not an empty history."""
    encoded = urllib.parse.quote(branch, safe="")
    status, raw = fetch(
        "/repos/"
        + _REPOSITORY
        + "/actions/workflows/"
        + _VERIFIER_WORKFLOW
        + "/runs?branch="
        + encoded
        + "&per_page=20"
    )
    if status == 404:
        return []
    document = _decode_json(status, raw, "agents verifier branch runs")
    runs = document.get("workflow_runs") if isinstance(document, dict) else None
    if not isinstance(runs, list):
        raise _ObservationError("agents verifier branch runs were not a list")
    total = document.get("total_count") if isinstance(document, dict) else None
    if (isinstance(total, int) and total > len(runs)) or len(runs) >= 20:
        raise _ObservationError("agents verifier branch runs were truncated")
    found = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        sha = run.get("head_sha")
        if not isinstance(sha, str) or len(sha) != 40:
            continue
        found.append(
            {
                "id": run.get("id"),
                "event": run.get("event"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "head_sha": sha,
            }
        )
    return found


def _earlier_commit_runs(branch_runs, commit_shas, current_sha):
    """Keep skipped runs on earlier commits of this PR out of the current-head result."""
    allowed = set(commit_shas) - {_PARTIAL_HEAD, _PARTIAL_MERGE, current_sha}
    earlier = []
    for run in branch_runs:
        if run.get("head_sha") in allowed:
            earlier.append(run)
    return earlier


def _label_names(document):
    return [
        label.get("name")
        for label in document.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ]


def _issue_record(fetch, number):
    status, raw = fetch(f"/repos/{_REPOSITORY}/issues/{number}")
    document = _decode_json(status, raw, f"issue {number}")
    return {
        "kind": "issue" if document.get("pull_request") is None else "pr",
        "name": str(number),
        "title": document.get("title") or "",
        "labels": _label_names(document),
        "present": True,
    }


def _pull_record(fetch, number):
    status, raw = fetch(f"/repos/{_REPOSITORY}/pulls/{number}")
    document = _decode_json(status, raw, f"pull {number}")
    head = document.get("head") or {}
    ref = head.get("ref") if isinstance(head, dict) else ""
    sha = head.get("sha") if isinstance(head, dict) else ""
    record = {
        "kind": "pr",
        "name": str(number),
        "title": document.get("title") or "",
        "labels": _label_names(document),
        "present": True,
        "head_ref": ref if isinstance(ref, str) else "",
        "head_sha": sha if isinstance(sha, str) and len(sha) == 40 else "",
    }
    if isinstance(document.get("merged"), bool):
        merge_sha = document.get("merge_commit_sha")
        record["merged"] = document["merged"] is True
        record["merge_sha"] = (
            merge_sha if isinstance(merge_sha, str) and len(merge_sha) == 40 else ""
        )
    return record


def _pull_head(fetch, number):
    status, raw = fetch(f"/repos/{_REPOSITORY}/pulls/{number}")
    document = _decode_json(status, raw, f"pull {number}")
    head = document.get("head") or {}
    sha = head.get("sha")
    if not isinstance(sha, str) or len(sha) != 40:
        raise _ObservationError(f"pull {number} head sha missing")
    return sha


def _branch_record(fetch, name):
    encoded = urllib.parse.quote(name, safe="")
    status, raw = fetch(f"/repos/{_REPOSITORY}/branches/{encoded}")
    if status == 404:
        return {
            "kind": "branch",
            "name": name,
            "title": "",
            "labels": [],
            "present": False,
        }
    document = _decode_json(status, raw, f"branch {name}")
    visible = document.get("name") if isinstance(document.get("name"), str) else name
    return {
        "kind": "branch",
        "name": name,
        "visible_name": visible,
        "title": "",
        "labels": [],
        "present": True,
        "sha": (document.get("commit") or {}).get("sha"),
    }


def _observe_chain(fetch):
    corrective_sha = _pull_head(fetch, int(_CORRECTIVE_PR))
    records = [_issue_record(fetch, number) for number in _CHAIN_ISSUES]
    records.extend(_pull_record(fetch, number) for number in _CHAIN_PRS)
    records.extend(_branch_record(fetch, name) for name in _CHAIN_BRANCHES)
    comments = []
    events = []
    partial_verdicts = []
    corrective_verdicts = []
    corpus = []
    unbound_corpus = []
    partial_corpus = []
    for number in _CHAIN_ISSUES + _CHAIN_PRS:
        page = _comment_records(fetch, number)
        for record in page:
            body = record["body"]
            comments.append(record)
            subject = _record_named(records, "pr", str(number))
            events.extend(
                _events_from_comment(
                    record, number, subject.get("head_sha"), subject.get("merge_sha")
                )
            )
            verdicts = _verdict_lines(body)
            if str(number) == _PARTIAL_PR:
                partial_verdicts.extend(verdicts)
                partial_corpus.extend(_partial_corpus_from_comment(record))
            elif str(number) == _CORRECTIVE_PR and corrective_sha in body:
                corrective_verdicts.extend(verdicts)
                for decision in _corpus_from_comment(record, corrective_sha):
                    if decision.get("evaluated_sha") == _published_merge_sha(
                        subject, corrective_sha
                    ):
                        corpus.append(decision)
                    else:
                        unbound_corpus.append(decision)
    corrective_pr = _corrective_record(records)
    runs = _bound_verifier_runs(
        fetch, corrective_sha, _published_merge_sha(corrective_pr, corrective_sha)
    )
    commits = _pull_commit_shas(fetch, int(_CORRECTIVE_PR))
    if corrective_sha not in commits:
        raise _ObservationError("corrective head is not a commit on its pull request")
    branch = corrective_pr.get("head_ref") or ""
    if not branch:
        raise _ObservationError("corrective pull request head ref missing")
    earlier = _earlier_commit_runs(_branch_verifier_runs(fetch, branch), commits, corrective_sha)
    return (
        corrective_sha,
        events,
        comments,
        records,
        partial_verdicts,
        corrective_verdicts,
        corpus,
        runs,
        earlier,
        partial_corpus,
        unbound_corpus,
    )


def _report_chain_evidence():
    try:
        (
            corrective_sha,
            events,
            comments,
            records,
            partial_verdicts,
            corrective_verdicts,
            corpus,
            runs,
            earlier,
            partial_corpus,
            unbound_corpus,
        ) = _observe_chain(_live_fetch)
    except _ObservationError as exc:
        print(f"chain-evidence observation failed: {exc}", file=sys.stderr)
        return 1
    text, code = _format_report(
        _assess(
            events,
            comments,
            records,
            corrective_sha,
            partial_verdicts,
            corrective_verdicts,
            corpus,
            partial_corpus,
            unbound_corpus,
        ),
        corrective_sha,
        _local_head(),
        runs,
        earlier,
    )
    print(text)
    return code


def _self_check():
    corrective = "c" * 40
    partial_event = {
        "schema": "verifier-event-state/v1",
        "pr": _PARTIAL_PR,
        "status": "incomplete",
        "run_id": "40603448727",
        "head_sha": _PARTIAL_HEAD,
        "evaluated_sha": _PARTIAL_MERGE,
        "failure_cause": "retained_provider_evaluation",
    }
    pending = dict(
        partial_event,
        pr=_CORRECTIVE_PR,
        status="pending",
        head_sha=corrective,
        evaluated_sha=corrective,
    )
    done = dict(pending, status="incomplete", failure_cause="validated_no_evaluation")
    partial_only, accepted = _classify_events([partial_event, pending, done], corrective)
    if len(partial_only) != 1 or partial_only[0]["status"] != "incomplete":
        raise AssertionError("partial verifier outcome was dropped or misread")
    if accepted != [_summarize_event(done)]:
        raise AssertionError("corrected-head verifier outcome was not isolated")
    if _classify_events([done], _PARTIAL_HEAD)[1]:
        raise AssertionError("partial sha was accepted as a corrective outcome")

    def publication_comment(body):
        return {
            "body": body,
            "author": "stranske",
            "url": f"https://github.com/{_REPOSITORY}/pull/{_CORRECTIVE_PR}#issuecomment-99",
        }

    tied = "<!-- closer-publication-source-handoff/v3 --> " + corrective
    if _publication_tied([publication_comment(tied)], corrective):
        raise AssertionError("unstructured handoff was accepted as publication")
    if _publication_tied(
        [publication_comment("closer-publication-source-handoff/v3 " + _PARTIAL_MERGE)], corrective
    ):
        raise AssertionError("partial publication text was credited to the corrective head")
    if _publication_tied([publication_comment("closer-publication-source-handoff/v3")], corrective):
        raise AssertionError("untied publication marker counted as proof")
    receipt = {
        "schema": _RUNNER_RECEIPT_SCHEMA,
        "record": {
            "head_sha": corrective,
            "observed_head_sha": corrective,
            "status": "completed",
        },
    }
    receipt_body = (
        "<!-- runner-completion:cursor:1168:v1 base64:"
        + base64.b64encode(json.dumps(receipt).encode()).decode()
        + " -->"
    )
    if _publication_tied([publication_comment(receipt_body)], corrective):
        raise AssertionError("keepalive receipt counted as publication proof")
    blocked = _assess(
        [],
        [receipt_body],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
                "merged": False,
                "merge_sha": "",
            }
        ],
        corrective,
    )
    blocked_text, blocked_code = _format_report(blocked, corrective, corrective)
    if blocked_code != 2 or blocked["publication"]:
        raise AssertionError("keepalive receipt completed the canary")
    if "native_verify_blocker: PR 1168 is unmerged" not in blocked_text:
        raise AssertionError("unmerged verifier blocker was not reported")
    if "keepalive_runner_for_corrective_head: completions=1" not in blocked_text:
        raise AssertionError("keepalive receipt bound to the head was not reported")
    if "not accepted as native verify or publication proof" not in blocked_text:
        raise AssertionError("keepalive receipt was accepted as proof")
    if "keepalive_prior_completion_observed_head: none" not in blocked_text:
        raise AssertionError("matching start head was reported as a prior observation")
    prior_start = "a" * 40
    prior_receipt = {
        "schema": _RUNNER_RECEIPT_SCHEMA,
        "record": {
            "head_sha": prior_start,
            "observed_head_sha": corrective,
            "status": "completed",
        },
    }
    prior_body = (
        "<!-- runner-completion:cursor:1168:v1 base64:"
        + base64.b64encode(json.dumps(prior_receipt).encode()).decode()
        + " -->"
    )
    prior_completions, prior_reservations, prior_observed = _keepalive_for_head(
        [prior_body, receipt_body], corrective
    )
    if prior_completions != ["completed"] or prior_reservations:
        raise AssertionError("this head's own completion was reclassified")
    if prior_observed != [{"start_head": prior_start, "status": "completed"}]:
        raise AssertionError("prior observed completion was counted as this head's run")
    prior_assessment = _assess(
        [],
        [prior_body],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
                "merged": False,
                "merge_sha": "",
            }
        ],
        corrective,
    )
    prior_text, prior_code = _format_report(prior_assessment, corrective, corrective)
    if prior_code != 2 or prior_assessment["publication"]:
        raise AssertionError("prior observed completion completed the canary")
    if "keepalive_runner_for_corrective_head: none " not in prior_text:
        raise AssertionError("prior observation was counted as this head's keepalive run")
    if "keepalive_prior_completion: start=" + prior_start not in prior_text:
        raise AssertionError("prior completion start head was not reported")
    if "not publication proof" not in prior_text:
        raise AssertionError("prior observed completion was accepted as publication proof")
    if not _text_labeled_test("test: chain", []) or not _text_labeled_test("Work", ["test"]):
        raise AssertionError("visible test label was missed")
    if _text_labeled_test("Controlled Closer E2E", ["agent:cursor"]):
        raise AssertionError("unlabeled title counted as a test label")
    if _branch_labeled_test("cursor/issue-983"):
        raise AssertionError("cursor/issue-983 counted as a test-labeled branch")
    if not _branch_labeled_test("codex/test-csv-followup-988-open"):
        raise AssertionError("test branch name was not visible")
    unlabeled = _assess(
        [partial_event],
        ["no publication"],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
            },
            {
                "kind": "branch",
                "name": "cursor/issue-983",
                "title": "",
                "labels": [],
                "present": True,
            },
        ],
        corrective,
    )
    text, code = _format_report(unlabeled, corrective, corrective)
    if code != 2 or "native_verify_for_corrective_head: MISSING" not in text:
        raise AssertionError("missing corrective verify was not reported")
    if "closer_publication_proof_for_corrective_head: MISSING" not in text:
        raise AssertionError("missing publication proof was not reported")
    if "publication_markers_observed: none" not in text:
        raise AssertionError("absent publication marker was not reported")
    if "verify_label_on_corrective_pr: absent" not in text:
        raise AssertionError("missing verify label was not reported")
    if "partial_merge_satisfies_criterion: no" not in text:
        raise AssertionError("partial merge was not excluded")
    if "unlabeled: branch cursor/issue-983" not in text:
        raise AssertionError("unlabeled branch was omitted")
    if "branch_label: cursor/issue-983 labeled=no" not in text:
        raise AssertionError("unlabeled branch name was reported as labeled")
    renamed = _assess(
        [],
        [],
        [
            {
                "kind": "branch",
                "name": "codex/issue-983-declared-csv-canary",
                "visible_name": "codex/test-issue-983-declared-csv-canary",
                "title": "",
                "labels": [],
                "present": True,
            }
        ],
        corrective,
    )
    if renamed["unlabeled"]:
        raise AssertionError("renamed visible branch name was not a test label")
    linked = _assess(
        [],
        [],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
                "head_ref": "cursor/issue-983",
            },
            {
                "kind": "branch",
                "name": "cursor/issue-983",
                "title": "",
                "labels": [],
                "present": True,
            },
        ],
        corrective,
    )
    linked_text, _linked_code = _format_report(linked, corrective, corrective)
    if (
        linked["unlabeled"]
        or "branch_label: cursor/issue-983 labeled=yes via=pr:1168" not in linked_text
    ):
        raise AssertionError("test-labeled PR did not label its head branch")
    bare = _assess(
        [],
        [],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "Cursor bootstrap",
                "labels": ["agent:cursor"],
                "present": True,
                "head_ref": "cursor/issue-983",
            },
            {
                "kind": "branch",
                "name": "cursor/issue-983",
                "title": "",
                "labels": [],
                "present": True,
            },
        ],
        corrective,
    )
    if not any(record["name"] == "cursor/issue-983" for record in bare["unlabeled"]):
        raise AssertionError("unlabeled PR counted as a branch test label")
    if _verdict_lines("intro\n**Verdict:** FAIL\nnext") != ["FAIL"]:
        raise AssertionError("verifier verdict line was not read")
    if _verdict_lines("**Verdict:** FAIL\n**Verdict:** CONCERNS\n") != ["FAIL"]:
        raise AssertionError("a later verdict cell replaced the headline verdict")
    held = _assess(
        [partial_event],
        ["no publication"],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
            }
        ],
        corrective,
        partial_verdicts=["FAIL"],
        corrective_verdicts=["PASS"],
    )
    held_text, held_code = _format_report(held, corrective, corrective)
    if held_code != 2 or "partial_verifier_verdict: FAIL" not in held_text:
        raise AssertionError("partial FAIL verdict was accepted or dropped")
    if "native_verify_for_corrective_head: PRESENT" in held_text:
        raise AssertionError("free-text verdict counted as native proof")
    if "unstructured_verdict: PASS not accepted as native proof" not in held_text:
        raise AssertionError("free-text verdict was not reported")
    if "closer_publication_proof_for_corrective_head: MISSING" not in held_text:
        raise AssertionError("a verdict replaced missing publication proof")
    if "agents_verifier_workflow_for_corrective_head: NONE" not in held_text:
        raise AssertionError("absent verifier run was not reported")
    decision = {
        "schema": "verifier-corpus-decision/v1",
        "repo": _REPOSITORY,
        "pr": _CORRECTIVE_PR,
        "head_sha": corrective,
        "evaluated_sha": "e" * 40,
        "run_id": "40603448727",
        "run_attempt": "1",
        "verdict": "PASS",
        "ci_failed": False,
        "provider_verdicts": ["PASS"],
    }
    decision_body = "<!-- verifier-corpus-decision/v1 " + json.dumps(decision) + " -->"
    decision_url = f"https://github.com/{_REPOSITORY}/pull/{_CORRECTIVE_PR}#issuecomment-99"
    trusted = {
        "body": decision_body,
        "author": "github-actions[bot]",
        "url": decision_url,
    }
    if _corpus_from_comment(trusted, corrective) != [_summarize_corpus(decision)]:
        raise AssertionError("trusted corpus decision was dropped")
    if _corpus_from_comment(dict(trusted, author="coderabbitai[bot]"), corrective):
        raise AssertionError("untrusted corpus decision counted as native proof")
    if _corpus_from_comment(dict(trusted, body="**Verdict:** PASS " + corrective), corrective):
        raise AssertionError("free-text verdict counted as a corpus decision")
    if _valid_corpus_decision(dict(decision, verdict="NON_PASS"), corrective):
        raise AssertionError("derived verdict mismatch was accepted")
    if _valid_corpus_decision(dict(decision, head_sha=_PARTIAL_HEAD), _PARTIAL_HEAD):
        raise AssertionError("partial head was accepted as a corrective decision")
    if _valid_corpus_decision(dict(decision, evaluated_sha=_PARTIAL_MERGE), corrective):
        raise AssertionError("partial merge was accepted as the corrective evaluated commit")
    if _valid_corpus_decision(dict(decision, evaluated_sha=corrective), corrective):
        raise AssertionError("head sha was accepted as the corrective evaluated commit")
    partial_decision = {
        "schema": "verifier-corpus-decision/v1",
        "repo": _REPOSITORY,
        "pr": _PARTIAL_PR,
        "head_sha": _PARTIAL_HEAD,
        "evaluated_sha": _PARTIAL_MERGE,
        "run_id": "40620861168",
        "run_attempt": "1",
        "verdict": "NON_PASS",
        "ci_failed": False,
        "provider_verdicts": ["FAIL", "FAIL"],
    }
    partial_body = "<!-- verifier-corpus-decision/v1 " + json.dumps(partial_decision) + " -->"
    partial_url = f"https://github.com/{_REPOSITORY}/pull/{_PARTIAL_PR}#issuecomment-6458980062"
    partial_record = {
        "body": partial_body,
        "author": "github-actions[bot]",
        "url": partial_url,
    }
    partial_summary = _summarize_corpus(partial_decision)
    if _partial_corpus_from_comment(partial_record) != [partial_summary]:
        raise AssertionError("trusted partial corpus decision was dropped")
    if _partial_corpus_from_comment(dict(partial_record, author="coderabbitai[bot]")):
        raise AssertionError("untrusted partial corpus decision counted")
    if _partial_corpus_from_comment(dict(partial_record, url=decision_url)):
        raise AssertionError("partial corpus decision on the corrective PR counted")
    if _corpus_from_comment(partial_record, corrective):
        raise AssertionError("partial corpus decision counted for the corrective head")
    if _valid_partial_corpus_decision(dict(partial_decision, head_sha=corrective)):
        raise AssertionError("corrective head was accepted as the partial corpus decision")
    if _valid_partial_corpus_decision(dict(partial_decision, pr=_CORRECTIVE_PR)):
        raise AssertionError("corrective PR number was accepted as the partial corpus decision")
    if _valid_partial_corpus_decision(dict(partial_decision, evaluated_sha=corrective)):
        raise AssertionError("a partial decision evaluated at the corrective head was accepted")
    partial_only = _assess(
        [],
        [tied],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
            },
            {
                "kind": "branch",
                "name": "codex/test-csv-followup-988-open",
                "title": "",
                "labels": [],
                "present": True,
            },
        ],
        corrective,
        partial_corpus=[partial_summary],
    )
    partial_text, partial_code = _format_report(partial_only, corrective, corrective)
    partial_line = (
        "partial_corpus_decision: verdict=NON_PASS run_id=40620861168 attempt=1 head="
        + _PARTIAL_HEAD
        + " evaluated="
        + _PARTIAL_MERGE
        + " on pr 984; not accepted for the corrected head"
    )
    if partial_code != 2 or partial_line not in partial_text:
        raise AssertionError("partial corpus decision was dropped or accepted")
    if "native_verify_for_corrective_head: PRESENT" in partial_text:
        raise AssertionError("partial corpus decision counted as corrective proof")
    if "completion: evidence incomplete" not in partial_text:
        raise AssertionError("partial corpus decision completed the corrective head")
    valid_state = {
        "schema": "verifier-event-state/v1",
        "pr": _CORRECTIVE_PR,
        "status": "incomplete",
        "run_id": "1",
        "head_sha": corrective,
        "evaluated_sha": corrective,
    }
    blob = (
        "<!-- verifier-event-state/v1 {not-json} -->\n"
        "<!-- verifier-event-state/v1 no-brace -->\n"
        + "<!-- verifier-event-state/v1 "
        + json.dumps(valid_state)
        + " -->\n"
        + "<!-- verifier-event-state/v1 "
        + json.dumps(dict(valid_state, run_id="2"))
        + " -->"
    )
    if [event["run_id"] for event in _verifier_events(blob)] != ["1", "2"]:
        raise AssertionError("verifier marker scan dropped a later event")
    present = _assess(
        [done],
        [tied],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
            },
            {
                "kind": "branch",
                "name": "codex/test-csv-followup-988-open",
                "title": "",
                "labels": [],
                "present": True,
            },
        ],
        corrective,
    )
    ready, ready_code = _format_report(present, corrective, corrective)
    if ready_code != 2 or "completion: evidence incomplete" not in ready:
        raise AssertionError("incomplete diagnostic states incorrectly reported completion")
    if "publication_markers_observed: closer-publication-source-handoff/v3" not in ready:
        raise AssertionError("tied publication marker was not named")
    corpus_only = _assess(
        [],
        [tied],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
            },
            {
                "kind": "branch",
                "name": "codex/test-csv-followup-988-open",
                "title": "",
                "labels": [],
                "present": True,
            },
        ],
        corrective,
        corpus=[_summarize_corpus(decision)],
    )
    corpus_text, corpus_code = _format_report(
        corpus_only,
        corrective,
        corrective,
        [{"id": 1, "event": "pull_request_target", "status": "completed", "conclusion": "skipped"}],
    )
    if corpus_code != 2 or "native_verify_for_corrective_head: PRESENT" in corpus_text:
        raise AssertionError("corpus decision without a merged commit counted as native verify")
    if "corpus_decision_not_on_merged_commit: verdict=PASS" not in corpus_text:
        raise AssertionError("unbound corpus decision was not reported")
    if (
        "agents_verifier_run: id=1" not in corpus_text
        or "completion: evidence incomplete" not in corpus_text
    ):
        raise AssertionError("a skipped verifier run changed the corpus decision")
    ancestor = "a" * 40
    earlier_only = [
        {
            "id": 7,
            "event": "pull_request_target",
            "status": "completed",
            "conclusion": "skipped",
            "head_sha": ancestor,
        }
    ]
    if [
        run["id"]
        for run in _earlier_commit_runs(
            earlier_only
            + [
                {
                    "id": 1,
                    "head_sha": _PARTIAL_HEAD,
                    "event": "pull_request_target",
                    "status": "completed",
                    "conclusion": "skipped",
                },
                {
                    "id": 3,
                    "head_sha": corrective,
                    "event": "pull_request_target",
                    "status": "completed",
                    "conclusion": "skipped",
                },
                {
                    "id": 4,
                    "head_sha": "d" * 40,
                    "event": "pull_request_target",
                    "status": "completed",
                    "conclusion": "skipped",
                },
            ],
            [_PARTIAL_HEAD, ancestor, corrective],
            corrective,
        )
    ] != [7]:
        raise AssertionError("partial or current-head verifier run counted as an earlier commit")
    earlier_text, earlier_code = _format_report(blocked, corrective, corrective, (), earlier_only)
    if earlier_code != 2 or "agents_verifier_earlier_run: id=7" not in earlier_text:
        raise AssertionError("earlier skipped verifier run was not reported")
    if "native_verify_for_corrective_head: PRESENT" in earlier_text:
        raise AssertionError("earlier skipped verifier run counted as a native outcome")
    kept_text, kept_code = _format_report(corpus_only, corrective, corrective, (), earlier_only)
    if kept_code != 2 or "completion: evidence incomplete" not in kept_text:
        raise AssertionError("earlier skipped verifier run changed the incomplete outcome")
    owned_head = "b" * 40
    owned_assessment = _assess(
        [],
        ["closer-publication-attempt-data/v1 " + owned_head],
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
                "merged": False,
                "merge_sha": "",
                "head_sha": corrective,
            },
            {
                "kind": "pr",
                "name": _OWNED_DELIVERY_PR,
                "title": "TEST: owned",
                "labels": ["test"],
                "present": True,
                "merged": False,
                "head_sha": owned_head,
            },
        ],
        corrective,
    )
    owned_text, owned_code = _format_report(owned_assessment, corrective, corrective)
    if owned_code != 2 or owned_assessment["publication"] or owned_assessment["owned_publication"]:
        raise AssertionError("owned delivery publication completed the corrective head")
    if "owned_delivery_publication: MISSING" not in owned_text:
        raise AssertionError("owned delivery publication was not reported")
    if "closer_publication_proof_for_corrective_head: MISSING" not in owned_text:
        raise AssertionError("owned delivery publication was accepted for the corrective head")
    if "owned_delivery_worker: none" not in owned_text:
        raise AssertionError("absent owned delivery worker was not reported")
    if "owned_delivery_worker_prior_observation: none" not in owned_text:
        raise AssertionError("absent owned delivery observation was not reported")
    owned_start = "e" * 40
    owned_observed_receipt = {
        "schema": _RUNNER_RECEIPT_SCHEMA,
        "record": {
            "head_sha": owned_start,
            "observed_head_sha": owned_head,
            "status": "completed",
        },
    }
    owned_started_receipt = {
        "schema": _RUNNER_RECEIPT_SCHEMA,
        "record": {
            "head_sha": owned_head,
            "observed_head_sha": owned_head,
            "status": "completed",
        },
    }
    owned_bodies = []
    for payload in (owned_observed_receipt, owned_started_receipt):
        owned_bodies.append(
            "<!-- runner-completion:cursor:"
            + _OWNED_DELIVERY_PR
            + ":v1 base64:"
            + base64.b64encode(json.dumps(payload).encode()).decode()
            + " -->"
        )
    owned_worker = _assess(
        [],
        owned_bodies,
        [
            {
                "kind": "pr",
                "name": "1168",
                "title": "test: chain",
                "labels": ["test"],
                "present": True,
                "merged": False,
                "merge_sha": "",
                "head_sha": corrective,
            },
            {
                "kind": "pr",
                "name": _OWNED_DELIVERY_PR,
                "title": "TEST: owned",
                "labels": ["test"],
                "present": True,
                "merged": False,
                "head_sha": owned_head,
            },
        ],
        corrective,
    )
    owned_worker_text, owned_worker_code = _format_report(owned_worker, corrective, corrective)
    if (
        owned_worker_code != 2
        or owned_worker["publication"]
        or owned_worker["owned_publication"]
        or owned_worker["owned_worker_completions"] != ["completed"]
        or owned_worker["owned_worker_observed_only"]
        != [{"start_head": owned_start, "status": "completed"}]
    ):
        raise AssertionError("owned delivery keepalive receipt was accepted as publication")
    if "owned_delivery_worker: completions=1 reservations=0" not in owned_worker_text:
        raise AssertionError("owned delivery worker completion was not reported")
    if "not accepted as native verify or publication proof" not in owned_worker_text:
        raise AssertionError("owned delivery worker completion was accepted as proof")
    if (
        "owned_delivery_worker_prior: start=" + owned_start not in owned_worker_text
        or "not a worker completion of that head" not in owned_worker_text
    ):
        raise AssertionError("owned delivery prior observation was counted as its worker")

    def truncated(_path):
        return (
            200,
            json.dumps({"total_count": 21, "workflow_runs": [{"head_sha": ancestor}]}).encode(),
        )

    try:
        _branch_verifier_runs(truncated, "cursor/issue-983")
    except _ObservationError as exc:
        if "truncated" not in str(exc):
            raise
    else:
        raise AssertionError("truncated verifier history was accepted")

    def full_commits(_path):
        return 200, json.dumps([{"sha": ancestor}] * 100).encode()

    try:
        _pull_commit_shas(full_commits, int(_CORRECTIVE_PR))
    except _ObservationError as exc:
        if "exceeded" not in str(exc):
            raise
    else:
        raise AssertionError("a full commit page was treated as complete")

    def fake_fetch(path):
        if "/comments" in path:
            if f"/issues/{_CORRECTIVE_PR}/comments" in path and "page=1" in path:
                return (
                    200,
                    json.dumps(
                        [
                            {
                                "body": decision_body,
                                "user": {"login": "github-actions[bot]"},
                                "html_url": decision_url,
                            }
                        ]
                    ).encode(),
                )
            if f"/issues/{_PARTIAL_PR}/comments" in path and "page=1" in path:
                return (
                    200,
                    json.dumps(
                        [
                            {
                                "body": partial_body,
                                "user": {"login": "github-actions[bot]"},
                                "html_url": partial_url,
                            }
                        ]
                    ).encode(),
                )
            return 200, b"[]"
        if "/commits?" in path:
            return 200, json.dumps([{"sha": corrective}, {"sha": ancestor}]).encode()
        if "/actions/workflows/" in path and "branch=" in path:
            return (
                200,
                json.dumps(
                    {
                        "workflow_runs": [
                            {
                                "id": 7,
                                "event": "pull_request_target",
                                "status": "completed",
                                "conclusion": "skipped",
                                "head_sha": ancestor,
                            },
                            {
                                "id": 42073591202,
                                "event": "pull_request_target",
                                "status": "completed",
                                "conclusion": "skipped",
                                "head_sha": corrective,
                            },
                        ]
                    }
                ).encode(),
            )
        if "/actions/workflows/" in path:
            return (
                200,
                json.dumps(
                    {
                        "workflow_runs": [
                            {
                                "id": 42073591202,
                                "event": "pull_request_target",
                                "status": "completed",
                                "conclusion": "skipped",
                                "head_sha": corrective,
                            }
                        ]
                    }
                ).encode(),
            )
        if "/branches/" in path:
            branch_name = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
            visible = branch_name
            if branch_name == "codex/issue-983-declared-csv-canary":
                visible = "codex/test-issue-983-declared-csv-canary"
            return 200, json.dumps({"name": visible, "commit": {"sha": corrective}}).encode()
        if "/pulls/" in path:
            return (
                200,
                json.dumps(
                    {
                        "title": "test: chain",
                        "labels": [{"name": "test"}],
                        "head": {"sha": corrective, "ref": "cursor/issue-983"},
                        "merged": True,
                        "merge_commit_sha": "e" * 40,
                    }
                ).encode(),
            )
        if "/issues/" in path:
            return 200, json.dumps({"title": "test: chain", "labels": [{"name": "test"}]}).encode()
        raise AssertionError("unexpected fetch " + path)

    state = dict(
        valid_state,
        repo=_REPOSITORY.lower(),
        run_attempt="1",
        evaluated_sha="e" * 40,
        status="incomplete",
    )
    raw_state = "<!-- " + _VERIFIER_MARKER + json.dumps(state) + " -->"
    for malformed in ([], {}):
        if not (
            not _event_bound_to({"head_sha": malformed, "evaluated_sha": malformed}, corrective)
        ):
            raise AssertionError("self-check regression: 2017")
        malformed_receipt = dict(receipt, head_sha=malformed, record={"head_sha": malformed})
        malformed_body = (
            "<!-- runner-completion:cursor:1168:v1 base64:"
            + (base64.b64encode(json.dumps(malformed_receipt).encode()).decode())
            + " -->"
        )
        if not (_keepalive_for_head([malformed_body], corrective) == ([], [], [])):
            raise AssertionError("self-check regression: 2024")

    def observation_with(body, author="github-actions[bot]", url=decision_url):
        def fetch(path):
            if f"/issues/{_CORRECTIVE_PR}/comments" in path:
                return (
                    200,
                    json.dumps(
                        [
                            {
                                "body": body,
                                "user": {"login": author},
                                "html_url": url,
                            }
                        ]
                    ).encode(),
                )
            return fake_fetch(path)

        return _observe_chain(fetch)

    two = raw_state + "\n" + raw_state
    if not (len(observation_with(two)[1]) == 2):
        raise AssertionError("self-check regression: 2046")
    if not (len(observation_with(_VERIFIER_MARKER + "{broken} -->\n" + raw_state)[1]) == 1):
        raise AssertionError("self-check regression: 2047")
    if not (not observation_with(_VERIFIER_MARKER + "no opening brace -->")[1]):
        raise AssertionError("self-check regression: 2048")
    if not (not observation_with(raw_state, author="attacker")[1]):
        raise AssertionError("self-check regression: 2049")
    if not (not observation_with(raw_state, url=partial_url)[1]):
        raise AssertionError("self-check regression: 2050")
    if not (not observation_with(raw_state, url=decision_url + "forged")[1]):
        raise AssertionError("self-check regression: 2051")
    for field, value in (
        ("head_sha", []),
        ("evaluated_sha", {}),
        ("repo", "other/repo"),
        ("pr", "984"),
        ("run_attempt", []),
        ("schema", "forged/v1"),
    ):
        forged = "<!-- " + _VERIFIER_MARKER + json.dumps(dict(state, **{field: value})) + " -->"
        if not (not observation_with(forged)[1]):
            raise AssertionError("self-check regression: 2061")

    # Network-free publication provenance checks; these values are test data only.
    pub = {
        "schema": "closer-confirmed-existing-pr-publication/v1",
        "pr": {
            "pr_number": 1168,
            "repository": _REPOSITORY.lower(),
            "owner_login": "stranske",
            "writer_login": "stranske",
            "owner_id": 23046322,
            "writer_id": 23046322,
            "head_oid": corrective,
            "head_ref": "cursor/issue-983",
        },
        "commit": {"oid": corrective},
        "ref": {"oid": corrective, "ref": "refs/heads/cursor/issue-983", "object_type": "commit"},
        "provenance": {},
    }
    for key, schema, prefix in (
        (
            "handoff_reference",
            "closer-publication-source-handoff-reference/v1",
            "publication-source-handoff-",
        ),
        (
            "coding_terminal_reference",
            "closer-coding-worker-terminal-reference/v1",
            "coding-worker-terminal-",
        ),
        (
            "publication_origin_reference",
            "closer-native-owned-pr-publication-origin-reference/v1",
            "native-publication-origin-",
        ),
        (
            "io_admission_reference",
            "closer-publication-io-admission-reference/v1",
            "publication-io-admission-",
        ),
    ):
        pub["provenance"][key] = {
            "schema": schema,
            "sha256": "a" * 64,
            "name": prefix + "a" * 64 + ".json",
        }
    for key in (
        "io_operation_sha256",
        "push_ack_sha256",
        "native_readback_sha256",
        "index_binding_sha256",
        "original_admission_sha256",
        "original_operation_sha256",
        "source_closure_sha256",
    ):
        pub["provenance"][key] = "b" * 64
    pub["provenance"]["native_readback_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "schema": "closer-owned-pr-readback-data/v1",
                "pr": pub["pr"],
                "commit": pub["commit"],
                "ref": pub["ref"],
                "native_publication_authorized": False,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    publication = {
        "author": "stranske",
        "url": decision_url,
        "body": pub["schema"] + " " + json.dumps(pub),
    }
    if _publication_tied([publication], corrective):
        raise AssertionError("reduced publication comment counted as producer custody")
    if not (not _publication_tied([dict(publication, author="github-actions[bot]")], corrective)):
        raise AssertionError("self-check regression: 2137")
    if not (not _publication_tied([dict(publication, url=partial_url)], corrective)):
        raise AssertionError("self-check regression: 2138")
    if not (not _publication_tied([dict(publication, body=tied)], corrective)):
        raise AssertionError("self-check regression: 2139")

    def stored_publication(head):
        repo_id = 1
        owned_pr = {
            "repository": _REPOSITORY.lower(),
            "repository_id": repo_id,
            "pr_number": 1168,
            "pr_node_id": "PR_1168",
            "issue_node_id": "I_983",
            "owner_login": "stranske",
            "owner_id": 23046322,
            "writer_login": "stranske",
            "writer_id": 23046322,
            "head_repo_id": repo_id,
            "base_repo_id": repo_id,
            "head_ref": "cursor/issue-983",
            "head_oid": head,
            "base_ref": "main",
            "base_oid": "e" * 40,
            "body_sha256": "c" * 64,
            "state": "open",
            "is_draft": False,
            "is_merged": False,
            "closing_issue_node_ids": ["I_983"],
        }
        native_commit = {
            "repository_id": repo_id,
            "oid": head,
            "tree_oid": "f" * 40,
            "parent_oids": ["1" * 40],
        }
        native_ref = {
            "repository_id": repo_id,
            "ref": "refs/heads/cursor/issue-983",
            "oid": head,
            "object_type": "commit",
        }
        provenance = {
            "scope_issue": {
                "number": 983,
                "node_id": "I_983",
                "body_sha256": "d" * 64,
            }
        }
        for key, schema, prefix in (
            (
                "handoff_reference",
                "closer-publication-source-handoff-reference/v1",
                "publication-source-handoff-",
            ),
            (
                "coding_terminal_reference",
                "closer-coding-worker-terminal-reference/v1",
                "coding-worker-terminal-",
            ),
            (
                "publication_origin_reference",
                "closer-native-owned-pr-publication-origin-reference/v1",
                "native-publication-origin-",
            ),
            (
                "io_admission_reference",
                "closer-publication-io-admission-reference/v1",
                "publication-io-admission-",
            ),
        ):
            provenance[key] = {
                "schema": schema,
                "sha256": "a" * 64,
                "name": prefix + ("a" * 64) + ".json",
            }
        for key in (
            "io_operation_sha256",
            "push_ack_sha256",
            "native_readback_sha256",
            "index_binding_sha256",
            "original_admission_sha256",
            "original_operation_sha256",
            "source_closure_sha256",
        ):
            provenance[key] = "b" * 64
        record = {
            "schema": _PUBLICATION_SCHEMA,
            "request": {"kind": "self-check"},
            "original": {"kind": "self-check"},
            "findings": [],
            "required_tests": [],
            "pr": owned_pr,
            "commit": native_commit,
            "ref": native_ref,
            "provenance": provenance,
        }
        for flag in _AUTHORITY_FLAGS:
            record[flag] = False
        record["provenance"]["native_readback_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    "schema": "closer-owned-pr-readback-data/v1",
                    "pr": owned_pr,
                    "commit": native_commit,
                    "ref": native_ref,
                    "native_publication_authorized": False,
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return record

    pub = stored_publication(corrective)
    publication = {
        "author": "stranske",
        "url": decision_url,
        "body": pub["schema"] + " " + json.dumps(pub),
    }
    if not _publication_tied([publication], corrective):
        raise AssertionError("stored publication record was dropped")
    if not _publication_tied([dict(publication, body=json.dumps(pub))], corrective):
        raise AssertionError("custody JSON publication record was dropped")
    authorized = json.loads(json.dumps(pub))
    authorized["native_publication_authorized"] = True
    if _publication_tied([dict(publication, body=json.dumps(authorized))], corrective):
        raise AssertionError("authority flag counted as confirmed publication")
    merged_snapshot = json.loads(json.dumps(pub))
    merged_snapshot["pr"]["is_merged"] = True
    merged_snapshot["provenance"]["native_readback_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "schema": "closer-owned-pr-readback-data/v1",
                "pr": merged_snapshot["pr"],
                "commit": merged_snapshot["commit"],
                "ref": merged_snapshot["ref"],
                "native_publication_authorized": False,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    if _publication_tied([dict(publication, body=json.dumps(merged_snapshot))], corrective):
        raise AssertionError("merged publication snapshot counted as the open producer record")
    changed_head = json.loads(json.dumps(pub))
    changed_head["pr"]["head_oid"] = []
    if not (
        not _publication_tied(
            [dict(publication, body=pub["schema"] + " " + json.dumps(changed_head))], corrective
        )
    ):
        raise AssertionError("self-check regression: 2142")
    complete_records = [
        {
            "kind": "pr",
            "name": "1168",
            "title": "test: chain",
            "labels": ["test"],
            "present": True,
            "merged": True,
            "merge_sha": "e" * 40,
            "head_sha": corrective,
        }
    ]
    complete = _assess(
        [], [publication], complete_records, corrective, corpus=[_summarize_corpus(decision)]
    )
    complete_text, complete_code = _format_report(complete, corrective, corrective)
    if complete_code != 0 or "completion: evidence present" not in complete_text:
        raise AssertionError("validated native decision and publication did not complete")
    if "corrective_corpus_decision: verdict=PASS" not in complete_text:
        raise AssertionError("merged corpus decision was not reported")
    prospective = "d" * 40
    open_decision = dict(decision, evaluated_sha=prospective)
    open_records = [
        {
            "kind": "pr",
            "name": "1168",
            "title": "test: chain",
            "labels": ["test"],
            "present": True,
            "merged": False,
            "merge_sha": prospective,
            "head_sha": corrective,
        }
    ]
    open_assessment = _assess(
        [],
        [publication],
        open_records,
        corrective,
        corpus=[_summarize_corpus(open_decision)],
    )
    open_text, open_code = _format_report(open_assessment, corrective, corrective)
    if open_code == 0 or "completion: evidence present" in open_text:
        raise AssertionError("open PR test-merge corpus decision completed the canary")
    if "native_verify_for_corrective_head: PRESENT" in open_text:
        raise AssertionError("open PR test-merge corpus decision counted as native verify")
    if "open_pr_test_merge_sha: " + prospective not in open_text:
        raise AssertionError("open PR test-merge sha was not reported")
    if "not a native Verify evaluated commit" not in open_text:
        raise AssertionError("test-merge sha was accepted as a verify commit")
    if "corpus_decision_not_on_merged_commit: verdict=PASS" not in open_text:
        raise AssertionError("test-merge corpus decision was not named as non-proof")
    diagnostic_only = _assess([done], [publication], complete_records, corrective)
    diagnostic_text, diagnostic_code = _format_report(diagnostic_only, corrective, corrective)
    if (
        diagnostic_code != 2
        or "native_verify_for_corrective_head: PRESENT" in diagnostic_text
        or "corrective_verifier_state_not_completion_proof:" not in diagnostic_text
    ):
        raise AssertionError("incomplete verifier state was credited as native completion proof")
    partial_publication = json.loads(json.dumps(pub))
    partial_publication["pr"]["head_oid"] = _PARTIAL_MERGE
    if _publication_tied(
        [publication_comment(pub["schema"] + " " + json.dumps(partial_publication))], corrective
    ):
        raise AssertionError("partial structured publication counted for the corrective head")
    damaged = json.loads(json.dumps(pub))
    damaged["provenance"]["coding_terminal_reference"]["sha256"] = []
    if not (
        not _publication_tied(
            [dict(publication, body=pub["schema"] + " " + json.dumps(damaged))], corrective
        )
    ):
        raise AssertionError("self-check regression: 2163")

    observed = _observe_chain(fake_fetch)
    if observed[6] != [_summarize_corpus(decision)] or observed[7][0]["conclusion"] != "skipped":
        raise AssertionError("chain observation dropped the corpus decision or the verifier run")
    if observed[10]:
        raise AssertionError("merged corpus decision was treated as unbound")
    if observed[8] != earlier_only:
        raise AssertionError("chain observation dropped the earlier skipped verifier run")
    if observed[9] != [partial_summary]:
        raise AssertionError("chain observation dropped the partial corpus decision")
    observed_text, observed_code = _format_report(
        _assess(
            observed[1],
            observed[2],
            observed[3],
            observed[0],
            observed[4],
            observed[5],
            observed[6],
            partial_corpus=observed[9],
        ),
        observed[0],
        observed[0],
        observed[7],
        observed[8],
    )
    if observed_code != 2 or "native_verify_for_corrective_head: PRESENT" not in observed_text:
        raise AssertionError("observed corpus decision was not reported")
    if "closer_publication_proof_for_corrective_head: MISSING" not in observed_text:
        raise AssertionError("observed chain invented publication proof")
    if partial_line not in observed_text or "completion: evidence present" in observed_text:
        raise AssertionError("observed partial corpus decision was accepted or omitted")

    def open_fetch(path):
        if "/actions/workflows/" in path and "head_sha=" + prospective in path:
            raise AssertionError("open PR test-merge was queried as a verifier run")
        status, raw = fake_fetch(path)
        if "/pulls/" in path and "/commits" not in path:
            document = json.loads(raw.decode())
            document["merged"] = False
            document["merge_commit_sha"] = prospective
            return status, json.dumps(document).encode()
        return status, raw

    open_observed = _observe_chain(open_fetch)
    if open_observed[6]:
        raise AssertionError("unmerged corpus decision was accepted")
    if open_observed[10] != [_summarize_corpus(decision)]:
        raise AssertionError("unmerged corpus decision was not retained for reporting")
    open_observed_text, open_observed_code = _format_report(
        _assess(
            open_observed[1],
            open_observed[2],
            open_observed[3],
            open_observed[0],
            open_observed[4],
            open_observed[5],
            open_observed[6],
            partial_corpus=open_observed[9],
            unbound_corpus=open_observed[10],
        ),
        open_observed[0],
        open_observed[0],
        open_observed[7],
        open_observed[8],
    )
    if (
        open_observed_code != 2
        or "native_verify_for_corrective_head: PRESENT" in open_observed_text
    ):
        raise AssertionError("observed unmerged corpus decision counted as native verify")
    if "open_pr_test_merge_sha: " + prospective not in open_observed_text:
        raise AssertionError("observed open PR test-merge sha was not reported")
    if "corpus_decision_not_on_merged_commit:" not in open_observed_text:
        raise AssertionError("observed unmerged corpus decision was not reported")

    merge_only = "m" * 40

    def run_fetch(path):
        sha = path.split("head_sha=")[-1].split("&")[0]
        if sha == _PARTIAL_MERGE:
            raise AssertionError("partial merge was queried as the corrective verifier run")
        if sha == corrective:
            payload = {"workflow_runs": []}
        elif sha == merge_only:
            payload = {
                "workflow_runs": [
                    {
                        "id": 9,
                        "event": "workflow_dispatch",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": merge_only,
                    }
                ]
            }
        else:
            raise AssertionError("unexpected verifier query " + path)
        return 200, json.dumps(payload).encode()

    if _bound_verifier_runs(run_fetch, corrective, _PARTIAL_MERGE):
        raise AssertionError("partial merge run was credited to the corrective head")
    bound_runs = _bound_verifier_runs(run_fetch, corrective, merge_only)
    if bound_runs != [
        {
            "id": 9,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        }
    ]:
        raise AssertionError("corrective merge commit run was not observed")
    run_text, run_code = _format_report(
        _assess(
            [],
            [],
            [
                {
                    "kind": "pr",
                    "name": "1168",
                    "title": "test: chain",
                    "labels": ["verify:evaluate"],
                    "present": True,
                    "merged": True,
                    "merge_sha": merge_only,
                }
            ],
            corrective,
        ),
        corrective,
        corrective,
        bound_runs,
    )
    if run_code != 2 or "a run is not a decision" not in run_text:
        raise AssertionError("a verifier run was treated as a decision")
    if "corrective_pr_merged: yes merge_sha=" + merge_only not in run_text:
        raise AssertionError("merged corrective PR was not reported")
    if "verify_label_on_corrective_pr: verify:evaluate" not in run_text:
        raise AssertionError("verify label was not reported")
    if "native_verify_for_corrective_head: PRESENT" in run_text:
        raise AssertionError("merge-commit run counted as a native outcome")

    def full_page(_path):
        return (
            200,
            json.dumps([{"body": "x", "user": {"login": "x"}, "html_url": ""}] * 100).encode(),
        )

    try:
        _comment_records(full_page, 1168)
    except _ObservationError as exc:
        if "exceeded" not in str(exc):
            raise
    else:
        raise AssertionError("exhausted comment pages were treated as complete")
    had_token = os.environ.get("GITHUB_TOKEN")
    os.environ["GITHUB_TOKEN"] = had_token or "self-check"
    original_open = urllib.request.urlopen

    def explode(*_args, **_kwargs):
        raise TimeoutError("timed out")

    urllib.request.urlopen = explode
    try:
        try:
            _live_fetch("/repos/" + _REPOSITORY + "/pulls/" + _CORRECTIVE_PR)
        except _ObservationError as exc:
            if "GitHub request failed" not in str(exc):
                raise
        else:
            raise AssertionError("timeout was not reported as an observation failure")
    finally:
        urllib.request.urlopen = original_open
        if had_token is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = had_token
    print("chain-evidence self-check ok")
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--self-check"]:
        try:
            return _self_check()
        except AssertionError:
            traceback.print_exc()
            print("chain-evidence self-check failed", file=sys.stderr)
            return 1
    if args == ["--chain-evidence"]:
        # Owner, 2026-11-08: current closer-core evidence replaces the retired
        # desktop publication contract. Keep the historical reader explicit.
        root = Path(__file__).resolve().parents[4]
        source = root / "src" / "closer_csv_completion.py"
        if not source.is_file():
            source = root / "closer_csv_completion.py"
        original_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("native_closer_csv_completion", source)
            if spec is None or spec.loader is None:
                raise _ObservationError("current closer-core completion reader unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.path.insert(0, str(source.parent))
            spec.loader.exec_module(module)
            return module.main(["--report"])
        except (_ObservationError, ImportError, OSError) as exc:
            print(f"chain-evidence observation failed: {exc}", file=sys.stderr)
            return 1
        finally:
            sys.path[:] = original_path
    if args == ["--legacy-chain-evidence"]:
        return _report_chain_evidence()
    export_path = None
    if len(args) == 1 and not args[0].startswith("-"):
        export_path = args[0]
    elif args:
        print("unsupported arguments", file=sys.stderr)
        return 1
    try:
        run_contract(export_path)
    except AssertionError:
        traceback.print_exc()
        print("contract assertion failed", file=sys.stderr)
        return 1
    print("contract ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
