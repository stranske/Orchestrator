"""Explicit standard-library contract script for the Closer CSV canary fixture.

Invoke this file directly. It is not a pytest module and defines no test items.
``--chain-evidence`` reads the public issue, branch and PR chain and reports
whether a trusted native verifier decision and closer publication proof are
tied to the corrected head. A free-text verdict line is not that proof. The
command does not change the default contract exit.
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
_CORPUS_MARKER = "verifier-corpus-decision/v1 "
_TRUSTED_VERIFIER_AUTHORS = {"github-actions", "github-actions[bot]"}
_PROVIDER_VERDICTS = {"PASS", "CONCERNS", "FAIL", "NON_PASS"}
_VERIFIER_WORKFLOW = "agents-verifier.yml"
_REPOSITORY = "stranske/Orchestrator"


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
        if brace < 0:
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


def _valid_corpus_decision(event, corrective_sha):
    """A terminal decision counts only for this corrective head, never the partial merge."""
    return (
        isinstance(event, dict)
        and event.get("schema") == "verifier-corpus-decision/v1"
        and str(event.get("repo") or "").lower() == _REPOSITORY.lower()
        and str(event.get("pr")) == _CORRECTIVE_PR
        and event.get("head_sha") == corrective_sha
        and corrective_sha not in {_PARTIAL_HEAD, _PARTIAL_MERGE}
        and _corpus_verdict_ok(event)
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
    if (record.get("author") or "") not in _TRUSTED_VERIFIER_AUTHORS:
        return []
    url = str(record.get("url") or "")
    prefix = f"https://github.com/{_REPOSITORY}/pull/{_CORRECTIVE_PR}#issuecomment-"
    if not url.lower().startswith(prefix.lower()):
        return []
    return [
        _summarize_corpus(event)
        for event in _marked_objects(record.get("body") or "", _CORPUS_MARKER)
        if _valid_corpus_decision(event, corrective_sha)
    ]


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


def _assess(
    events,
    comments,
    records,
    corrective_sha,
    partial_verdicts=(),
    corrective_verdicts=(),
    corpus=(),
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
    return {
        "partial": partial,
        "corrective": corrective,
        "partial_verdicts": list(partial_verdicts),
        "corrective_verdicts": list(corrective_verdicts),
        "corpus": list(corpus),
        "publication": _publication_tied(comments, corrective_sha),
        "unlabeled": unlabeled,
        "branch_labels": branch_labels,
    }


def _format_report(assessment, corrective_sha, local_head, verifier_runs=()):
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
    if assessment["corrective"] or assessment["corpus"]:
        lines.append("native_verify_for_corrective_head: PRESENT")
        for event in assessment["corrective"]:
            lines.append(
                "corrective_verifier_event: "
                + f"status={event['status']} run_id={event['run_id']} "
                + f"head={event['head_sha']} evaluated={event['evaluated_sha']}"
            )
        for decision in assessment["corpus"]:
            lines.append(
                "corrective_corpus_decision: "
                + f"verdict={decision['verdict']} run_id={decision['run_id']} "
                + f"attempt={decision['run_attempt']} head={decision['head_sha']} "
                + f"evaluated={decision['evaluated_sha']}"
            )
    else:
        lines.append(
            "native_verify_for_corrective_head: MISSING "
            "no trusted verifier-corpus-decision/v1 or non-pending "
            "verifier-event-state/v1 on PR 1168 is bound to its head"
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
    native_present = bool(assessment["corrective"] or assessment["corpus"])
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
    corpus = []
    for number in _CHAIN_ISSUES + _CHAIN_PRS:
        page = _comment_records(fetch, number)
        for record in page:
            body = record["body"]
            comments.append(body)
            events.extend(_verifier_events(body))
            verdicts = _verdict_lines(body)
            if str(number) == _PARTIAL_PR:
                partial_verdicts.extend(verdicts)
            elif str(number) == _CORRECTIVE_PR and corrective_sha in body:
                corrective_verdicts.extend(verdicts)
                corpus.extend(_corpus_from_comment(record, corrective_sha))
    runs = _verifier_workflow_runs(fetch, corrective_sha)
    return (
        corrective_sha,
        events,
        comments,
        records,
        partial_verdicts,
        corrective_verdicts,
        corpus,
        runs,
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
        ),
        corrective_sha,
        _local_head(),
        runs,
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
    valid_state = {
        "schema": "verifier-event-state/v1",
        "pr": _CORRECTIVE_PR,
        "status": "complete",
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
    if ready_code != 0 or "completion: evidence present" not in ready:
        raise AssertionError("complete synthetic evidence did not report present")
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
    if corpus_code != 0 or "corrective_corpus_decision: verdict=PASS" not in corpus_text:
        raise AssertionError("trusted corpus decision did not count")
    if (
        "agents_verifier_run: id=1" not in corpus_text
        or "completion: evidence present" not in corpus_text
    ):
        raise AssertionError("a skipped verifier run changed the corpus decision")

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
            return 200, b"[]"
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
                    }
                ).encode(),
            )
        if "/issues/" in path:
            return 200, json.dumps({"title": "test: chain", "labels": [{"name": "test"}]}).encode()
        raise AssertionError("unexpected fetch " + path)

    observed = _observe_chain(fake_fetch)
    if observed[6] != [_summarize_corpus(decision)] or observed[7][0]["conclusion"] != "skipped":
        raise AssertionError("chain observation dropped the corpus decision or the verifier run")
    observed_text, observed_code = _format_report(
        _assess(
            observed[1],
            observed[2],
            observed[3],
            observed[0],
            observed[4],
            observed[5],
            observed[6],
        ),
        observed[0],
        observed[0],
        observed[7],
    )
    if observed_code != 2 or "native_verify_for_corrective_head: PRESENT" not in observed_text:
        raise AssertionError("observed corpus decision was not reported")
    if "closer_publication_proof_for_corrective_head: MISSING" not in observed_text:
        raise AssertionError("observed chain invented publication proof")

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
