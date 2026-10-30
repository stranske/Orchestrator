"""Mock native origin joins only; no actual source installation or API calls."""

import base64
import copy
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_corrective_verifier_readback as readback
import closer_dispatcher as native
import verifier_evidence


def raw_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@pytest.fixture
def retained(monkeypatch, request):
    mode = getattr(request, "param", "evaluate")
    stem = "evaluation" if mode == "evaluate" else "comparison"
    root = Path(__file__).parent / "fixtures/closer_corrective_instruction"
    manifest = json.loads((root / "v1-verification-event-manifest.json").read_text())
    binding = json.loads((root / "v1-required-evidence-row.json").read_text())
    manifest["assessment_identity"]["mode"] = mode
    manifest["execution_binding"]["evaluator_step"] = (
        "Run LLM evaluation" if mode == "evaluate" else "Run multi-provider comparison"
    )
    event = manifest["verification_event"]
    payload = raw_json({"verdict": "PASS", "synthetic": "not a model result or qualification"})
    receipt = dict(
        schema=f"workflows-verifier-{stem}-execution/v1",
        mode=mode,
        status="completed",
        decision_available=True,
        exit_code=0,
        repository="stranske/example",
        source_run_id="101",
        source_run_attempt="2",
        outputs={
            stem
            + ".json": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        },
    )
    terminal = dict(
        schema="verifier-corpus-decision/v1",
        repo="stranske/example",
        pr="9",
        head_sha="d" * 40,
        evaluated_sha="e" * 40,
        run_id="101",
        run_attempt="2",
        verdict="PASS",
        ci_failed=False,
        provider_verdicts=["PASS"],
        verification_event=event,
        assessment_identity=manifest["assessment_identity"],
        execution_binding=manifest["execution_binding"],
        result_admission=dict(
            schema="verifier-result-admission/v1",
            mode=mode,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            execution_receipt_sha256=hashlib.sha256(raw_json(receipt)).hexdigest(),
        ),
        corrective_verification_input=dict(
            schema="closer-corrective-verification-input/v1",
            verification_event=event,
            corrective_instruction=binding,
        ),
    )
    body = (
        "Synthetic settled report\n<!-- verifier-corpus-decision/v1 "
        + json.dumps(terminal, sort_keys=True)
        + " -->\n"
    )
    comment = dict(
        id=501,
        node_id="IC_501",
        updated_at="2026-10-27T18:00:00Z",
        body=body,
        user={"login": "github-actions[bot]", "type": "Bot", "id": 41898282},
        html_url="https://github.com/stranske/example/pull/9#issuecomment-501",
        issue_url="https://api.github.com/repos/stranske/example/issues/9",
    )
    members = {
        "verification-event-manifest.json": raw_json(manifest),
        stem + ".json": payload,
        stem + "-execution.json": raw_json(receipt),
        stem + "-comment.md": body.encode(),
    }
    run = dict(
        id=101,
        run_attempt=2,
        event="workflow_dispatch",
        path=readback.WORKFLOW,
        status="completed",
        conclusion="success",
        head_sha="a" * 40,
        repository={"id": 11, "full_name": "stranske/example"},
        head_repository={"id": 11, "full_name": "stranske/example"},
        referenced_workflows=[
            {"path": readback.CENTRAL + "/" + readback.CALLEE + "@main", "sha": "b" * 40}
        ],
    )
    jobs = dict(
        total_count=1,
        jobs=[
            dict(
                id=601,
                run_id=101,
                run_attempt=2,
                head_sha="a" * 40,
                status="completed",
                name="Run post-merge verifier",
                steps=[
                    dict(
                        name=name,
                        status="completed",
                        conclusion="success",
                        number=index + 1,
                        started_at=f"2026-10-27T18:00:0{index}.000Z",
                        completed_at=f"2026-10-27T18:00:0{index}.800Z",
                    )
                    for index, name in enumerate(
                        (
                            "Checkout Workflows scripts",
                            "Record verification event pending",
                            (
                                "Run LLM evaluation"
                                if mode == "evaluate"
                                else "Run multi-provider comparison"
                            ),
                            (
                                "Retain evaluation execution evidence"
                                if mode == "evaluate"
                                else "Upload comparison artifacts"
                            ),
                        )
                    )
                ],
            )
        ],
    )
    artifact = dict(
        id=701,
        name=stem + "-results-101",
        expired=False,
        workflow_run={
            "id": 101,
            "head_sha": "a" * 40,
            "repository_id": 11,
            "head_repository_id": 11,
        },
        created_at="2026-10-27T18:00:03.400Z",
        size_in_bytes=4096,
    )
    docs = {
        "repos/stranske/example/actions/runs/101/attempts/2": run,
        "repos/stranske/example/actions/runs/101": run,
        "repos/stranske/example/actions/runs/101/attempts/2/jobs?per_page=100": jobs,
        "repos/stranske/example/actions/runs/101/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [artifact],
        },
        "repos/stranske/example/issues/comments/501": comment,
        "repos/stranske/example/issues/9/comments?per_page=100&page=1": [comment],
    }
    for repository, commit, head in (
        ("stranske/example", "a" * 40, "f" * 40),
        (readback.CENTRAL, "b" * 40, "c" * 40),
    ):
        docs[f"repos/{repository}"] = {"full_name": repository, "default_branch": "main"}
        docs[f"repos/{repository}/commits/main"] = {"sha": head}
        docs[f"repos/{repository}/compare/{commit}...{head}"] = {
            "status": "ahead",
            "base_commit": {"sha": commit},
            "merge_base_commit": {"sha": commit},
        }
    hashes = {}
    for repo, path, ref in [("stranske/example", readback.WORKFLOW, "a" * 40)] + [
        (readback.CENTRAL, path, "b" * 40) for path in readback.SOURCE_FILES
    ]:
        raw = ("Synthetic source DATA only: " + path).encode()
        docs[f"repos/{repo}/contents/{path}?ref={ref}"] = dict(
            type="file",
            path=path,
            encoding="base64",
            content=base64.b64encode(raw).decode(),
            size=len(raw),
            sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        )
        hashes[path] = hashlib.sha256(raw).hexdigest()
    pin = dict(
        caller_sha256=hashes[readback.WORKFLOW],
        central={path: hashes[path] for path in readback.SOURCE_FILES},
        helper_commit="b" * 40,
        reusable_sha256=hashes[readback.CALLEE],
    )
    monkeypatch.setattr(
        readback, "DEPLOYED_SOURCE_PINS", (pin,)
    )  # Inert mock, not a source-deployment claim.
    calls = []

    def api(endpoint, *, method="GET"):
        assert method == "GET"
        calls.append(endpoint)
        assert endpoint in docs, endpoint
        return copy.deepcopy(docs[endpoint])

    logs = {
        "repos/stranske/example/actions/jobs/601/logs": "2026-10-27T18:00:00.100Z repository: stranske/Workflows\n"
        "2026-10-27T18:00:00.150Z path: .workflows-lib\n"
        "2026-10-27T18:00:00.200Z git log -1 --format=%H\n"
        "2026-10-27T18:00:00.300Z " + "b" * 40 + "\n"
    }

    def read_text(endpoint):
        calls.append(endpoint)
        assert endpoint in logs
        return logs[endpoint]

    backend = SimpleNamespace(
        gh=SimpleNamespace(api=api, read_text=read_text, deadline=time.time() + 60)
    )
    state = SimpleNamespace(
        backend=backend,
        docs=docs,
        logs=logs,
        members=members,
        artifact=artifact,
        run=run,
        jobs=jobs,
        comment=comment,
        receipt=receipt,
        terminal=terminal,
        calls=calls,
        record={
            "pr": {
                "repository": "stranske/example",
                "repository_id": 11,
                "pr_number": 9,
                "head_oid": "d" * 40,
            }
        },
        snapshot={
            "target": "stranske/example#9",
            "merge": "e" * 40,
            "verifier": verifier_evidence._public_record(terminal),
        },
    )

    def archive():
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            for name, raw in state.members.items():
                z.writestr(zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0)), raw)
        raw = stream.getvalue()
        state.artifact["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        return raw

    state.archive = archive
    monkeypatch.setattr(
        native, "native_artifact_fetcher", lambda snapshot: lambda artifact, **kwargs: archive()
    )
    # The native digest is present before retrieval, not minted by the reader.
    archive()
    return state


def test_mock_native_artifact_reader_joins_exact_receipt_attempt_source_and_terminal(retained):
    value = readback.read(retained.backend, retained.record, retained.snapshot)
    assert value["native_origin"]["artifact_id"] == 701
    assert value["native_origin"]["run_attempt"] == "2"
    assert (
        value["compact"]["corrective_instruction"]
        == value["manifest"]["material"]["required_evidence"][0]
    )
    assert all("POST" not in call for call in retained.calls)


def test_mock_native_archive_bytes_do_not_change_with_wall_clock(retained, monkeypatch):
    monkeypatch.setattr(zipfile.time, "localtime", lambda _seconds: (2001, 1, 1, 0, 0, 0, 0, 1, -1))
    first = retained.archive()
    monkeypatch.setattr(zipfile.time, "localtime", lambda _seconds: (2031, 1, 1, 0, 0, 0, 0, 1, -1))
    assert retained.archive() == first


@pytest.mark.parametrize(
    "damage",
    [
        "attempt",
        "expired",
        "duplicate_artifact",
        "source",
        "pin",
        "skipped",
        "receipt_attempt",
        "payload",
        "comment_changed",
        "no_pins",
    ],
)
def test_missing_or_forged_native_readback_remains_held(retained, monkeypatch, damage):
    if damage == "attempt":
        retained.run["run_attempt"] = 3
    elif damage == "expired":
        retained.artifact["expired"] = True
    elif damage == "duplicate_artifact":
        doc = retained.docs["repos/stranske/example/actions/runs/101/artifacts?per_page=100"]
        doc["artifacts"].append(copy.deepcopy(retained.artifact))
        doc["total_count"] = 2
    elif damage == "source":
        retained.docs["repos/stranske/example/contents/" + readback.WORKFLOW + "?ref=" + "a" * 40][
            "sha"
        ] = ("0" * 40)
    elif damage == "pin":
        monkeypatch.setattr(readback, "DEPLOYED_SOURCE_PINS", ({"wrong": "source"},))
    elif damage == "skipped":
        retained.jobs["jobs"][0]["steps"][1]["conclusion"] = "skipped"
    elif damage == "receipt_attempt":
        retained.receipt["source_run_attempt"] = "1"
        retained.members["evaluation-execution.json"] = raw_json(retained.receipt)
        retained.archive()
    elif damage == "payload":
        retained.members["evaluation.json"] += b"changed"
        retained.archive()
    elif damage == "comment_changed":
        retained.docs["repos/stranske/example/issues/comments/501"] = {
            **retained.comment,
            "updated_at": "2026-10-27T19:00:00Z",
        }
    else:
        monkeypatch.setattr(readback, "DEPLOYED_SOURCE_PINS", ())
    with pytest.raises(readback.ReadbackHeld):
        readback.read(retained.backend, retained.record, retained.snapshot)


def test_missing_deployed_support_refuses_before_any_native_read(retained, monkeypatch):
    monkeypatch.setattr(readback, "DEPLOYED_SOURCE_PINS", ())
    with pytest.raises(
        readback.ReadbackHeld, match="deployed corrective verifier source unavailable"
    ):
        readback.require_deployed(retained.backend, retained.record, "main")
    assert retained.calls == []


@pytest.mark.parametrize(
    "path",
    [".github/scripts/verifier_gzip_assets.js", "scripts/langchain/pr_verifier.py"],
)
def test_changed_corrective_selector_or_validator_source_cannot_reuse_support_pin(retained, path):
    # Authenticated Git content is valid but not the reviewed deployed source.
    # Neither the native checkout SHA nor a successful terminal can substitute
    # for these normative parser/validator bytes.
    assert path in readback.SOURCE_FILES
    document = retained.docs[f"repos/{readback.CENTRAL}/contents/{path}?ref=" + "b" * 40]
    raw = b"different valid native source bytes"
    document.update(
        content=base64.b64encode(raw).decode(),
        size=len(raw),
        sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
    )
    with pytest.raises(readback.ReadbackHeld, match="source is unsupported"):
        readback.read(retained.backend, retained.record, retained.snapshot)
    assert all("POST" not in call for call in retained.calls)


def test_duplicate_fixed_archive_member_is_not_selected_by_order():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("verification-event-manifest.json", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("verification-event-manifest.json", b"second")
    with pytest.raises(readback.ReadbackHeld, match="duplicated"):
        readback._members(stream.getvalue(), ["verification-event-manifest.json"])


def test_new_native_pending_event_cannot_leave_snapshot_old_pass_usable(retained):
    import verification_events

    state = {key: retained.terminal[key] for key in ("repo", "pr", "head_sha", "evaluated_sha")}
    event = copy.deepcopy(retained.terminal["verification_event"])
    event["material_sha256"] = "f" * 64
    event["event_id"] = verification_events.event_id(event)
    state.update(
        schema="verifier-event-state/v1",
        status="pending",
        run_id="102",
        run_attempt="1",
        verification_event=event,
        predecessor_event_id=retained.terminal["verification_event"]["event_id"],
    )
    retained.docs["repos/stranske/example/issues/9/comments?per_page=100&page=1"].append(
        {
            **retained.comment,
            "id": 502,
            "html_url": "https://github.com/stranske/example/pull/9#issuecomment-502",
            "body": "<!-- verifier-event-state/v1 " + json.dumps(state) + " -->",
        }
    )
    with pytest.raises(readback.ReadbackHeld, match="lifecycle changed"):
        readback.read(retained.backend, retained.record, retained.snapshot)


@pytest.mark.parametrize(
    "damage",
    [
        "outside_window",
        "ambiguous_sha",
        "wrong_repository",
        "unmerged_helper",
        "upload_window",
        "step_order",
        "job_attempt",
    ],
)
def test_native_checkout_and_upload_origin_are_not_inferred_from_workflow_sha(retained, damage):
    endpoint = "repos/stranske/example/actions/jobs/601/logs"
    if damage == "outside_window":
        retained.logs[endpoint] = retained.logs[endpoint].replace("18:00:00.", "18:00:10.")
    elif damage == "ambiguous_sha":
        retained.logs[endpoint] += retained.logs[endpoint]
    elif damage == "wrong_repository":
        retained.logs[endpoint] = retained.logs[endpoint].replace(
            "repository: stranske/Workflows", "repository: attacker/other"
        )
    elif damage == "unmerged_helper":
        retained.docs["repos/stranske/Workflows/compare/" + "b" * 40 + "..." + "c" * 40][
            "status"
        ] = "diverged"
    elif damage == "upload_window":
        retained.artifact["created_at"] = "2026-10-27T18:00:02.000Z"
    elif damage == "step_order":
        retained.jobs["jobs"][0]["steps"][0]["number"] = 50
    else:
        retained.jobs["jobs"][0]["run_attempt"] = 1
    with pytest.raises(readback.ReadbackHeld):
        readback.read(retained.backend, retained.record, retained.snapshot)


@pytest.mark.parametrize("retained", ["compare"], indirect=True)
def test_actual_existing_compare_mode_uses_its_exact_members_step_and_receipt(retained):
    value = readback.read(retained.backend, retained.record, retained.snapshot)
    assert value["terminal"]["result_admission"]["mode"] == "compare"
    assert (
        value["manifest"]["execution_binding"]["evaluator_step"] == "Run multi-provider comparison"
    )
    assert set(retained.members) == {
        "verification-event-manifest.json",
        "comparison.json",
        "comparison-execution.json",
        "comparison-comment.md",
    }


def test_unrelated_unparseable_old_comment_does_not_override_current_native_selector(retained):
    retained.docs["repos/stranske/example/issues/9/comments?per_page=100&page=1"].insert(
        0,
        {
            **retained.comment,
            "id": 499,
            "html_url": "https://github.com/stranske/example/pull/9#issuecomment-499",
            "body": '<!-- verifier-corpus-decision/v1 {"schema":invalid_old_json} -->',
        },
    )
    assert (
        readback.read(retained.backend, retained.record, retained.snapshot)["native_origin"][
            "comment_id"
        ]
        == 501
    )
