"""Explicit standard-library contract script for the Closer CSV canary fixture.

Invoke this file directly. It is not a pytest module and defines no test items.
``--chain-evidence`` reads the public issue, branch and PR chain and reports
whether native Verify and closer publication proof are tied to the corrected
head. It does not change the default contract exit.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _load_export():
    path = Path(__file__).resolve().parent / "csv_export.py"
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
    try:
        module.export_rows(rows, "xml")
    except ValueError:
        return
    raise AssertionError("unsupported format did not raise ValueError")


def run_contract():
    module = _load_export()
    plain = [{"name": "alpha", "score": 1}, {"name": "beta", "score": 2}]
    _assert_json(module, plain)
    _assert_unsupported(module, plain)
    empty = _csv_text(module, [])
    assert empty == "name,score\r\n", repr(empty)
    _assert_csv_round_trip(module, plain)
    special = [
        {"name": "a,b", "score": "1"},
        {"name": 'say "hi"', "score": "2"},
        {"name": "line\nbreak", "score": "3"},
        {"name": "cr\rhere", "score": "4"},
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
_REPOSITORY = "stranske/Orchestrator"


def _verifier_events(text):
    events = []
    cursor = 0
    while True:
        found = text.find(_VERIFIER_MARKER, cursor)
        if found < 0:
            break
        payload = text[found + len(_VERIFIER_MARKER) :]
        brace = payload.find("{")
        if brace < 0:
            break
        try:
            event, consumed = json.JSONDecoder().raw_decode(payload[brace:])
        except json.JSONDecodeError:
            cursor = found + len(_VERIFIER_MARKER)
            continue
        if isinstance(event, dict) and event.get("schema") == "verifier-event-state/v1":
            events.append(event)
        cursor = found + len(_VERIFIER_MARKER) + brace + consumed
    return events


def _event_bound_to(event, sha):
    return sha in {event.get("head_sha"), event.get("evaluated_sha")}


def _is_outcome(event):
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
    """Keep partial-merge verifier events out of the corrected-head result."""
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
            and _is_outcome(event)
        ):
            corrective.append(summary)
    return partial, corrective


def _publication_tied(comments, corrective_sha):
    if not corrective_sha or corrective_sha in {_PARTIAL_HEAD, _PARTIAL_MERGE}:
        return False
    for body in comments:
        if corrective_sha in body and any(marker in body for marker in _PUBLICATION_MARKERS):
            return True
    return False


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


def _assess(events, comments, records, corrective_sha, partial_verdicts=(), corrective_verdicts=()):
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
    return {
        "partial": partial,
        "corrective": corrective,
        "partial_verdicts": list(partial_verdicts),
        "corrective_verdicts": list(corrective_verdicts),
        "publication": _publication_tied(comments, corrective_sha),
        "unlabeled": unlabeled,
        "branch_labels": branch_labels,
    }


def _format_report(assessment, corrective_sha, local_head):
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
    partial_outcomes = [event for event in partial_events if _is_outcome(event)]
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
    if assessment["corrective"] or assessment["corrective_verdicts"]:
        lines.append("native_verify_for_corrective_head: PRESENT")
        for event in assessment["corrective"]:
            lines.append(
                "corrective_verifier_event: "
                + f"status={event['status']} run_id={event['run_id']} "
                + f"head={event['head_sha']} evaluated={event['evaluated_sha']}"
            )
        for verdict in assessment["corrective_verdicts"]:
            lines.append(f"corrective_verifier_verdict: {verdict}")
    else:
        lines.append(
            "native_verify_for_corrective_head: MISSING "
            "no non-pending verifier-event-state on PR 1168 is bound to its head"
        )
    if assessment["publication"]:
        lines.append("closer_publication_proof_for_corrective_head: PRESENT")
    else:
        lines.append(
            "closer_publication_proof_for_corrective_head: MISSING "
            "no closer-publication marker in the chain comments contains the corrective head"
        )
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
    native_present = bool(assessment["corrective"] or assessment["corrective_verdicts"])
    complete = native_present and assessment["publication"] and not assessment["unlabeled"]
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


def _decode_json(status, raw, label):
    if status != 200:
        raise _ObservationError(f"{label} returned HTTP {status}")
    try:
        return json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ObservationError(f"{label} was not JSON") from exc


def _comment_bodies(fetch, number):
    bodies = []
    for page in range(1, 6):
        status, raw = fetch(
            f"/repos/{_REPOSITORY}/issues/{number}/comments?per_page=100&page={page}"
        )
        batch = _decode_json(status, raw, f"comments for {number}")
        if not isinstance(batch, list):
            raise _ObservationError(f"comments for {number} were not a list")
        for item in batch:
            if isinstance(item, dict):
                bodies.append(item.get("body") or "")
        if len(batch) < 100:
            break
    return bodies


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
    return {
        "kind": "pr",
        "name": str(number),
        "title": document.get("title") or "",
        "labels": _label_names(document),
        "present": True,
        "head_ref": ref if isinstance(ref, str) else "",
    }


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
    for number in _CHAIN_ISSUES + _CHAIN_PRS:
        bodies = _comment_bodies(fetch, number)
        comments.extend(bodies)
        for body in bodies:
            events.extend(_verifier_events(body))
            verdicts = _verdict_lines(body)
            if str(number) == _PARTIAL_PR:
                partial_verdicts.extend(verdicts)
            elif str(number) == _CORRECTIVE_PR and corrective_sha in body:
                corrective_verdicts.extend(verdicts)
    return corrective_sha, events, comments, records, partial_verdicts, corrective_verdicts


def _report_chain_evidence():
    try:
        (
            corrective_sha,
            events,
            comments,
            records,
            partial_verdicts,
            corrective_verdicts,
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
        ),
        corrective_sha,
        _local_head(),
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
    done = dict(pending, status="complete", failure_cause=None)
    partial_only, accepted = _classify_events([partial_event, pending, done], corrective)
    if len(partial_only) != 1 or partial_only[0]["status"] != "incomplete":
        raise AssertionError("partial verifier outcome was dropped or misread")
    if accepted != [_summarize_event(done)]:
        raise AssertionError("corrected-head verifier outcome was not isolated")
    if _classify_events([done], _PARTIAL_HEAD)[1]:
        raise AssertionError("partial sha was accepted as a corrective outcome")
    tied = "<!-- closer-publication-source-handoff/v3 --> " + corrective
    if not _publication_tied([tied], corrective):
        raise AssertionError("publication marker tied to the corrective head was ignored")
    if _publication_tied(["closer-publication-source-handoff/v3 " + _PARTIAL_MERGE], corrective):
        raise AssertionError("partial publication text was credited to the corrective head")
    if _publication_tied(["closer-publication-source-handoff/v3"], corrective):
        raise AssertionError("untied publication marker counted as proof")
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
    if "native_verify_for_corrective_head: PRESENT" not in held_text:
        raise AssertionError("corrective prose verdict was ignored")
    if "closer_publication_proof_for_corrective_head: MISSING" not in held_text:
        raise AssertionError("a verdict replaced missing publication proof")
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
    if ready_code != 0 or "completion: evidence present" not in ready:
        raise AssertionError("complete synthetic evidence did not report present")
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
        return _report_chain_evidence()
    if args:
        print("unsupported arguments", file=sys.stderr)
        return 1
    try:
        run_contract()
    except AssertionError:
        traceback.print_exc()
        print("contract assertion failed", file=sys.stderr)
        return 1
    print("contract ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
