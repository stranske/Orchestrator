"""Only freshly collected, inventory-qualified inputs leave the metadata floor."""

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

import adjudicator_retro as retro
import roles


@pytest.fixture
def qualified_input(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in retro._GIT_LOCATION_VARS}

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], env=env, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "inherited.py").write_text("first = 'λ'\nsecond = 2\nthird = 3\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "subject.py").write_text("value = 'évaluated'\n")
    (repo / "proof.txt").write_text("Entire proof " + "λ" * 2400)
    git("add", ".")
    git("commit", "-qm", "evaluated")
    sha = git("rev-parse", "HEAD")
    body = "## Tasks\n- Read the complete source.\n## Acceptance Criteria\n- Entire proof is inspected.\n"
    row = {
        "case_id": "one",
        "target": "owner/repo#1",
        "packet": {
            "target": "owner/repo#1",
            "disputed_finding": {
                "body": "Missing evidence " + "X" * 2400,
                "decision": {"evaluated_sha": sha},
            },
            "ground_truth_evidence": {
                "head_sha": sha,
                "merge_sha": sha,
                "diff_summary": [{"path": "subject.py"}, {"path": "proof.txt"}],
                "gate_runs": ["gate-run"],
            },
        },
        "collection_requirements": {
            "source_paths": ["subject.py", "proof.txt"],
            "acceptance": [{"criterion": "proof", "location": "git-path:proof.txt"}],
        },
        "inventory_provenance": {
            "source_criterion_tasks": body,
            "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        },
    }
    qualification = {
        "comparison_base": base,
        "snapshot": {"number": 1, "headRefOid": sha, "body": body},
        "criterion_mapping": [
            {
                "criterion": "Entire proof is inspected.",
                "evidence_locations": ["git-path:proof.txt"],
                "unknowns": [
                    {
                        "evidence_ref": "historical-installation",
                        "owner": "operator",
                        "next_action": "Inspect an authentic installed receipt.",
                    }
                ],
            }
        ],
    }
    return repo, row, qualification, git


def proposal(ref="git-path:proof.txt"):
    return {
        "decision": "reject_blocker",
        "confidence": "high",
        "rationale": "Complete bytes supplied.",
        "evidence_assessment": [
            {
                "claim": "proof unavailable",
                "status": "contradicted",
                "evidence_ref": ref,
                "reason": "Full immutable proof supplied.",
            }
        ],
        "ground_truth_refs": [ref],
        "recommended_next_step": "Inspect the named historical UNKNOWN.",
        "evidence_gaps": ["Historical authenticity remains unverified."],
    }


def test_collector_issued_input_reaches_existing_native_runner_without_truncation(
    qualified_input, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    original = copy.deepcopy(row)
    case = retro.qualify_collected_case(row, repo, qualification)
    captured = {}

    def offload(backend, prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return {"exit": 0, "run_id": "native-one", "output": json.dumps(proposal())}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    records = []
    monkeypatch.setattr(roles.feedback, "record_role_run", lambda **kwargs: records.append(kwargs))
    result = roles.run_adjudicator_agent(case=case, backend="gemini", dispatch=True, cwd=str(repo))
    assert result["decision_source"] == "adjudicator_agent"
    assert result["advisory_plan"]["decision"] == "reject_blocker"
    assert captured["isolate"] is True
    assert "X" * 2400 in captured["prompt"]
    assert "\\u03bb" * 2400 in captured["prompt"]
    assert "Entire proof is inspected." in captured["prompt"]
    assert "historical-installation" in captured["prompt"]
    assert records[0]["source"] == "retrospective"
    assert result["evidence_qualification"]["persisted_admission"] is False
    assert row == original
    # Even self-consistent serialized result fields do not authenticate native execution.
    forged = {
        **result,
        "packet": result["case"],
        "decision": "reject_blocker",
        "disposition": "reject_blocker",
        "shadow_verdict": "PASS",
        "later_truth": "PASS",
    }
    assert retro.summarize([forged])["adjudicated"] == 0


def test_native_admission_reserves_once_persists_raw_before_brain_and_preserves_original(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    row["run_id"] = "original-run"
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row]}))
    original_bytes = original.read_bytes()
    journal_path = tmp_path / "native.sqlite"
    journal = retro.NativeAttemptJournal(journal_path)
    order = []

    def offload(*_args, **_kwargs):
        order.append("offload")
        receipt = {"exit": 0, "run_id": "native-once", "output": json.dumps(proposal())}
        _kwargs["attempt_started_callback"]({"run_id": "native-once"})
        _kwargs["attempt_receipt_callback"](receipt)
        return {**receipt, "attempt_receipt_callback_called": True}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)

    def record_role(**_kwargs):
        order.append("brain")
        with sqlite3.connect(journal_path) as conn:
            assert (
                conn.execute("SELECT state FROM native_attempts").fetchone()[0] == "raw_persisted"
            )

    monkeypatch.setattr(roles.feedback, "record_role_run", record_role)
    projection = retro.admit_collected_retry(
        original_report=original,
        output=tmp_path / "projection.json",
        case_id="one",
        case=case,
        journal=journal,
        backend="gemini",
        dispatch=True,
        cwd=str(repo),
        expected_report_sha256=hashlib.sha256(original_bytes).hexdigest(),
    )
    assert original.read_bytes() == original_bytes
    assert projection["original_case_id"] == "one"
    assert order == ["offload", "brain"]
    with sqlite3.connect(journal_path) as conn:
        raw, state = conn.execute("SELECT raw_response,state FROM native_attempts").fetchone()
    assert json.loads(raw)["decision"] == "reject_blocker"
    assert state == "validated"
    with pytest.raises(ValueError, match="already reserved|validated binary"):
        retro.admit_collected_retry(
            original_report=original,
            output=tmp_path / "duplicate.json",
            case_id="one",
            case=case,
            journal=journal,
            backend="gemini",
            dispatch=True,
            cwd=str(repo),
        )
    monkeypatch.setattr(roles.dispatcher, "offload", lambda *_a, **_kw: pytest.fail("redispatch"))
    refreshed = retro.refresh_native_projection(
        original_report=original,
        output=tmp_path / "refreshed.json",
        case_id="one",
        case=case,
        journal=journal,
        truth_reader=lambda *_args: "PASS",
    )
    assert refreshed["native_summary"] == {"graded": 1, "agree": 1, "unknown": 0}
    assert len(refreshed["rows"]) == 1
    # Administrative reassignment and a different checkout must not strand a
    # paid receipt whose substantive evidence and original report are unchanged.
    moved_repo = tmp_path / "moved-repo"
    shutil.copytree(repo, moved_repo)
    changed_qualification = copy.deepcopy(qualification)
    changed_qualification["criterion_mapping"][0]["unknowns"][0].update(
        owner="new operator", next_action="Inspect the same receipt from the new checkout."
    )
    rebuilt = retro.qualify_collected_case(row, moved_repo, changed_qualification)
    assert rebuilt._digest != case._digest
    assert retro._native_substantive_revision(rebuilt) == retro._native_substantive_revision(case)
    restarted = retro.NativeAttemptJournal(journal_path)
    moved = retro.refresh_native_projection(
        original_report=original,
        output=tmp_path / "moved.json",
        case_id="one",
        case=rebuilt,
        journal=restarted,
        truth_reader=lambda *_args: "PASS",
    )
    assert moved["native_summary"] == refreshed["native_summary"]
    assert moved["rows"][0]["native_projection"]["attempt_id"] == projection["native_attempt_id"]
    # A different complete task contract must not reuse the paid receipt, even
    # when the spend identity and acceptance-criterion text remain unchanged.
    changed_row = copy.deepcopy(row)
    changed_contract = copy.deepcopy(qualification)
    body = qualification["snapshot"]["body"].replace(
        "Read the complete source.", "Do not read the source; inspect only metadata."
    )
    changed_row["inventory_provenance"] = {
        "source_criterion_tasks": body,
        "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    changed_contract["snapshot"]["body"] = body
    different = retro.qualify_collected_case(changed_row, repo, changed_contract)
    assert retro._native_substantive_revision(different) == retro._native_substantive_revision(case)
    rejected = tmp_path / "changed-contract.json"
    with pytest.raises(ValueError, match="complete task contract"):
        retro.refresh_native_projection(
            original_report=original,
            output=rejected,
            case_id="one",
            case=different,
            journal=restarted,
            truth_reader=lambda *_args: "PASS",
        )
    assert not rejected.exists()
    assert original.read_bytes() == original_bytes
    # Missing/corrupt/schema-less Brain storage is a failed read, not pending
    # durability. No projection may be published in any of these cases.
    for damage in ("missing", "corrupt", "missing-schema"):
        brain = tmp_path / (damage + ".sqlite")
        if damage == "corrupt":
            brain.write_bytes(b"not a SQLite database")
        elif damage == "missing-schema":
            with sqlite3.connect(brain) as conn:
                conn.execute("CREATE TABLE unrelated (value TEXT)")
        output = tmp_path / (damage + ".json")
        with pytest.raises(sqlite3.Error):
            retro.refresh_native_projection(
                original_report=original,
                output=output,
                case_id="one",
                case=rebuilt,
                journal=restarted,
                db=brain,
            )
        assert not output.exists()
    assert original.read_bytes() == original_bytes
    unknown = retro.refresh_native_projection(
        original_report=original,
        output=tmp_path / "unknown.json",
        case_id="one",
        case=case,
        journal=journal,
        truth_reader=lambda *_args: None,
    )
    assert unknown["native_summary"] == {"graded": 0, "agree": 0, "unknown": 1}
    with pytest.raises(ValueError, match="trusted native admission"):
        retro.refresh_native_projection(
            original_report=original,
            output=tmp_path / "forged.json",
            case_id="one",
            case=case,
            journal=retro.NativeAttemptJournal(tmp_path / "forged.sqlite"),
            truth_reader=lambda *_args: "PASS",
        )


@pytest.mark.parametrize("field", ["decision", "confidence", "status"])
@pytest.mark.parametrize("malformed", [[], {}])
def test_malformed_native_proposal_is_consumed_and_recoverable(
    qualified_input, tmp_path, monkeypatch, field, malformed
):
    repo, row, qualification, _ = qualified_input
    row["run_id"] = "original-run"
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row]}))
    invalid = proposal()
    if field == "status":
        invalid["evidence_assessment"][0][field] = malformed
    else:
        invalid[field] = malformed
    assert roles.ROLE_REGISTRY["adjudicator"].validate(invalid)
    calls = []

    def offload(*args, **kwargs):
        assert kwargs["max_network_retries"] == 0
        calls.append(True)
        response = {"exit": 0, "run_id": "paid-invalid", "output": json.dumps(invalid)}
        kwargs["attempt_started_callback"]({"run_id": "paid-invalid"})
        kwargs["attempt_receipt_callback"](response)
        return {**response, "attempt_receipt_callback_called": True}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    monkeypatch.setattr(roles.feedback, "record_role_run", lambda **kwargs: None)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    result = retro.admit_collected_retry(
        original_report=original,
        output=tmp_path / "attempt.json",
        case_id="one",
        case=case,
        journal=journal,
        backend="gemini",
        dispatch=True,
        cwd=str(repo),
    )
    assert result["result"]["errors"] and calls == [True]
    restarted = retro.NativeAttemptJournal(journal.path)
    refreshed = retro.refresh_native_projection(
        original_report=original,
        output=tmp_path / "refreshed.json",
        case_id="one",
        case=case,
        journal=restarted,
        truth_reader=lambda *args: "PASS",
    )
    assert refreshed["native_summary"]["graded"] == 0 and calls == [True]
    revision = retro._native_substantive_revision(case)
    assert not restarted.reserve("one", revision, case._digest, "source")["reserved"]
    assert restarted.reserve("one", "new-substantive-revision", case._digest, "source")["reserved"]


def test_native_spend_revision_ignores_administrative_unknown_assignment(qualified_input):
    repo, row, qualification, _ = qualified_input
    first = retro.qualify_collected_case(row, repo, qualification)
    changed = copy.deepcopy(qualification)
    changed["criterion_mapping"][0]["unknowns"][0].update(
        owner="new operator", next_action="Use a different review queue."
    )
    second = retro.qualify_collected_case(row, repo, changed)
    assert retro._native_substantive_revision(first) == retro._native_substantive_revision(second)
    assert (
        roles._collected_snapshot(first)["ground_truth_evidence"]["qualification"][
            "material_evidence_revision"
        ]
        != roles._collected_snapshot(second)["ground_truth_evidence"]["qualification"][
            "material_evidence_revision"
        ]
    )


def test_native_admission_preflight_is_no_dispatch_and_validates_entire_cohort(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    other = {"case_id": "other", "target": "owner/repo#2", "retry_attempts": [None]}
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row, other]}))
    monkeypatch.setattr(roles.dispatcher, "offload", lambda *_a, **_kw: pytest.fail("dispatch"))
    with pytest.raises(ValueError, match="invalid retry attempt history"):
        retro.admit_collected_retry(
            original_report=original,
            output=tmp_path / "projection.json",
            case_id="one",
            case=case,
            journal=retro.NativeAttemptJournal(tmp_path / "native.sqlite"),
            dispatch=False,
            cwd=str(repo),
        )
    other["retry_attempts"] = []
    original.write_text(json.dumps({"rows": [row, other]}))
    prepared = retro.admit_collected_retry(
        original_report=original,
        output=tmp_path / "projection.json",
        case_id="one",
        case=case,
        journal=retro.NativeAttemptJournal(tmp_path / "native.sqlite"),
        dispatch=False,
        cwd=str(repo),
    )
    assert prepared["native_admission"] == "prepared_no_dispatch"
    assert not (tmp_path / "projection.json").exists()


def test_native_journal_raw_persisted_crash_blocks_changed_revision(tmp_path):
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    first = journal.reserve("case", "A" * 64, "digest", "report")
    journal.persist_raw(first["attempt_id"], {"run_id": "backend", "output": "{}"})
    blocked = journal.reserve("case", "B" * 64, "digest", "report")
    assert blocked["reserved"] is False
    assert blocked["state"] == "raw_persisted"


def test_native_reservation_releases_only_before_started_invocation(tmp_path):
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    reserved = journal.reserve("case", "A" * 64, "digest", "report", await_start=True)
    assert reserved["state"] == "reserved"
    assert journal.release_unstarted(reserved["attempt_id"]) is True
    again = journal.reserve("case", "A" * 64, "digest", "report", await_start=True)
    journal.begin_invocation(again["attempt_id"], {"run_id": "paid-one"})
    assert journal.release_unstarted(again["attempt_id"]) is False
    journal.persist_raw(again["attempt_id"], {"run_id": "paid-one", "output": "{}"})
    blocked = journal.reserve("case", "B" * 64, "digest", "report", await_start=True)
    assert blocked["reserved"] is False
    assert blocked["state"] == "raw_persisted"


def test_native_validate_refuses_missing_started_receipt(tmp_path):
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    reserved = journal.reserve("case", "A" * 64, "digest", "report", await_start=True)
    with pytest.raises(ValueError, match="raw persisted"):
        journal.validate(reserved["attempt_id"], {})


def test_native_no_provider_releases_reservation_without_projection(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    row["run_id"] = "original-run"
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row]}))
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    monkeypatch.setattr(roles, "route_role", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError, match="no provider"):
        retro.admit_collected_retry(
            original_report=original,
            output=tmp_path / "projection.json",
            case_id="one",
            case=case,
            journal=journal,
            dispatch=True,
            cwd=str(repo),
        )
    assert not (tmp_path / "projection.json").exists()
    assert (
        journal.reserve(
            "one",
            retro._native_substantive_revision(case),
            case._digest,
            hashlib.sha256(original.read_bytes()).hexdigest(),
            await_start=True,
        )["reserved"]
        is True
    )


def test_native_predispatch_failure_releases_same_revision_reservation(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    row["run_id"] = "original-run"
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row]}))
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")

    def predispatch_failure(*_args, **_kwargs):
        raise ValueError("provider rejected request before invocation")

    monkeypatch.setattr(roles.dispatcher, "offload", predispatch_failure)
    with pytest.raises(ValueError, match="before invocation"):
        retro.admit_collected_retry(
            original_report=original,
            output=tmp_path / "projection.json",
            case_id="one",
            case=case,
            journal=journal,
            backend="gemini",
            dispatch=True,
            cwd=str(repo),
        )
    assert not (tmp_path / "projection.json").exists()
    assert (
        journal.reserve(
            "one",
            retro._native_substantive_revision(case),
            case._digest,
            hashlib.sha256(original.read_bytes()).hexdigest(),
            await_start=True,
        )["reserved"]
        is True
    )


def test_native_started_raw_failure_preserves_unknown_and_blocks_all_revisions(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    row["run_id"] = "original-run"
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row]}))
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")

    def started_then_raw(*_args, **kwargs):
        kwargs["attempt_started_callback"]({"run_id": "paid-before-raw"})
        kwargs["attempt_receipt_callback"](
            {"exit": 0, "run_id": "paid-before-raw", "output": json.dumps(proposal())}
        )
        pytest.fail("raw receipt failure must stop completion")

    monkeypatch.setattr(roles.dispatcher, "offload", started_then_raw)
    monkeypatch.setattr(
        journal, "persist_raw", lambda *_args: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        retro.admit_collected_retry(
            original_report=original,
            output=tmp_path / "projection.json",
            case_id="one",
            case=case,
            journal=journal,
            backend="gemini",
            dispatch=True,
            cwd=str(repo),
        )
    with sqlite3.connect(journal.path) as conn:
        state, backend_id = conn.execute(
            "SELECT state,backend_run_id FROM native_attempts"
        ).fetchone()
    assert (state, backend_id) == ("in_flight", "paid-before-raw")
    assert not journal.reserve("one", "B" * 64, case._digest, "report", await_start=True)[
        "reserved"
    ]


def test_native_interrupt_after_raw_persistence_stays_unknown(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    row["run_id"] = "original-run"
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row]}))
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")

    def interrupted(*_args, **kwargs):
        kwargs["attempt_started_callback"]({"run_id": "paid-interrupted"})
        kwargs["attempt_receipt_callback"]({"run_id": "paid-interrupted", "output": "{}"})
        raise KeyboardInterrupt()

    monkeypatch.setattr(roles.dispatcher, "offload", interrupted)
    with pytest.raises(KeyboardInterrupt):
        retro.admit_collected_retry(
            original_report=original,
            output=tmp_path / "projection.json",
            case_id="one",
            case=case,
            journal=journal,
            backend="gemini",
            dispatch=True,
            cwd=str(repo),
        )
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT state FROM native_attempts").fetchone()[0] == "raw_persisted"
    assert not (tmp_path / "projection.json").exists()
    assert not journal.reserve("one", "B" * 64, case._digest, "report", await_start=True)[
        "reserved"
    ]


def test_invalid_native_receipts_neither_grade_nor_block_future_revision(tmp_path):
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    first = journal.reserve("case", "A" * 64, "digest", "report")
    journal.persist_raw(first["attempt_id"], {"run_id": "backend", "output": "{}"})
    journal.validate(
        first["attempt_id"],
        {
            "backend_run_id": "backend",
            "raw_output": "{}",
            "errors": ["bad"],
            "proposal": proposal(),
        },
    )
    assert journal.has_binary_decision("case") is False
    assert journal.reserve("case", "B" * 64, "digest", "report")["reserved"] is True


def test_native_journal_refuses_non_native_database(tmp_path):
    path = tmp_path / "brain.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE outcomes(run_id TEXT)")
    with pytest.raises(ValueError, match="non-native"):
        retro.NativeAttemptJournal(path)


def test_native_cli_mode_conflict_is_rejected(monkeypatch):
    monkeypatch.setattr("sys.argv", ["adjudicator_retro", "--selftest", "--native-admit", "one"])
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2


def test_serialized_or_copied_packet_cannot_manufacture_collector_authority(qualified_input):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    packet = roles._collected_snapshot(case)
    packet["metadata_only"] = False
    packet["ground_truth_evidence"]["qualification"]["persisted_admission"] = True
    result = roles.run_adjudicator_agent(
        case=json.loads(json.dumps(packet)), backend="gemini", proposal_json=proposal()
    )
    assert result["decision_source"] == "metadata_only_needs_more_evidence"
    assert roles.adjudication_metadata_only(case) is False
    with pytest.raises(TypeError):
        roles.CollectedAdjudicationCase()
    with pytest.raises(ValueError, match="unissued"):
        roles.adjudication_metadata_only(object.__new__(roles.CollectedAdjudicationCase))


def test_collector_capability_digest_is_rechecked_before_use(qualified_input):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    case._encoded += b" "
    with pytest.raises(ValueError, match="changed"):
        roles.run_adjudicator_agent(case=case, backend="gemini", proposal_json=proposal())


@pytest.mark.parametrize(
    "defect",
    [
        "missing-path",
        "duplicate-path",
        "wrong-base",
        "wrong-revision",
        "wrong-head",
        "altered-body",
        "altered-body-hash",
        "missing-provenance",
        "missing-mapping",
        "wrong-criterion",
        "foreign-artifact",
        "unowned-unknown",
        "forged-complete",
    ],
)
def test_unqualified_inventory_or_provenance_never_issues_case(qualified_input, defect):
    repo, row, qualification, _ = qualified_input
    if defect == "missing-path":
        row["collection_requirements"]["source_paths"].pop()
    elif defect == "duplicate-path":
        row["collection_requirements"]["source_paths"].append("proof.txt")
    elif defect == "wrong-base":
        qualification["comparison_base"] = "0" * 40
    elif defect == "wrong-revision":
        row["packet"]["ground_truth_evidence"]["merge_sha"] = "0" * 40
    elif defect == "wrong-head":
        qualification["snapshot"]["headRefOid"] = "0" * 40
    elif defect == "altered-body":
        qualification["snapshot"]["body"] += "changed"
    elif defect == "altered-body-hash":
        row["inventory_provenance"]["pr_body_sha256"] = "0" * 64
    elif defect == "missing-provenance":
        row.pop("inventory_provenance")
    elif defect == "missing-mapping":
        qualification["criterion_mapping"] = []
    elif defect == "wrong-criterion":
        qualification["criterion_mapping"][0]["criterion"] = "invented"
    elif defect == "foreign-artifact":
        qualification["criterion_mapping"][0]["evidence_locations"] = ["foreign"]
    elif defect == "unowned-unknown":
        qualification["criterion_mapping"][0]["unknowns"][0].pop("owner")
    elif defect == "forged-complete":
        row["collection_requirements"]["acceptance"][0]["location"] = "git-path:absent"
        row.update(complete=True, metadata_only=False)
    with pytest.raises(ValueError):
        retro.qualify_collected_case(row, repo, qualification)


@pytest.mark.parametrize("operation", ["delete", "rename", "type-change"])
def test_unsupported_git_changes_fail_closed(qualified_input, operation):
    repo, row, qualification, git = qualified_input
    qualification["comparison_base"] = git("rev-parse", "HEAD")
    if operation == "delete":
        (repo / "subject.py").unlink()
    elif operation == "rename":
        (repo / "subject.py").rename(repo / "renamed.py")
    else:
        (repo / "subject.py").unlink()
        (repo / "subject.py").symlink_to("proof.txt")
    git("add", ".")
    git("commit", "-qm", "unsupported")
    sha = git("rev-parse", "HEAD")
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"]["merge_sha"] = sha
    with pytest.raises(ValueError, match="deletion/type"):
        retro.qualify_collected_case(row, repo, qualification)


def test_qualified_proposal_cannot_cite_absent_evidence(qualified_input):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    result = roles.run_adjudicator_agent(
        case=case, backend="gemini", proposal_json=proposal("invented")
    )
    assert result["decision_source"] == "baseline_needs_more_evidence"
    assert "absent" in result["errors"][0]
    assert result["backend_run_id"] is None


def test_complete_prompt_budget_includes_unicode_and_context(qualified_input):
    repo, row, qualification, _ = qualified_input
    with pytest.raises(ValueError, match="prompt exceeds"):
        retro.qualify_collected_case(row, repo, qualification, prompt_byte_limit=6000)
    case = retro.qualify_collected_case(row, repo, qualification)
    with pytest.raises(ValueError, match="prompt exceeds"):
        roles.run_adjudicator_agent(case=case, context="C" * 1_048_576, backend="gemini")


def test_modified_sources_include_complete_before_and_after_blobs(qualified_input):
    repo, row, qualification, git = qualified_input
    qualification["comparison_base"] = git("rev-parse", "HEAD")
    (repo / "subject.py").write_text("value = 'changed'\n")
    git("add", ".")
    git("commit", "-qm", "modified")
    sha = git("rev-parse", "HEAD")
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"].update(
        merge_sha=sha, head_sha=sha, diff_summary=[{"path": "subject.py"}]
    )
    row["collection_requirements"]["source_paths"] = ["subject.py"]
    qualification["snapshot"]["headRefOid"] = sha
    case = retro.qualify_collected_case(row, repo, qualification)
    collection = roles._collected_snapshot(case)["ground_truth_evidence"]["collected_evidence"]
    assert collection["sources"][0]["bytes_utf8"] == "value = 'changed'\n"
    assert collection["before_sources"][0]["bytes_utf8"] == "value = 'évaluated'\n"
    assert collection["before_sources"][0]["evaluated_sha"] == qualification["comparison_base"]
    assert collection["sources"][0]["git_mode"] == "100644"


def test_cli_qualified_dry_run_preserves_saved_report_and_refuses_output_reuse(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    report, qual, output = (
        tmp_path / name for name in ("saved.json", "qualification.json", "new.json")
    )
    report.write_text(json.dumps({"rows": [row]}))
    qual.write_text(json.dumps(qualification))
    original = report.read_bytes()
    monkeypatch.setattr(roles, "route_role", lambda *args, **kwargs: {"agent": "gemini"})
    monkeypatch.setattr(
        roles.dispatcher, "offload", lambda *args, **kwargs: pytest.fail("dry run dispatched")
    )
    monkeypatch.setattr(
        roles.feedback, "record_role_run", lambda **kwargs: pytest.fail("dry run wrote Brain")
    )
    argv = [
        "adjudicator_retro",
        "--judge-collected-case",
        "one",
        "--report",
        str(report),
        "--qualification",
        str(qual),
        "--repository",
        str(repo),
        "--output",
        str(output),
    ]
    monkeypatch.setattr("sys.argv", argv)
    assert retro.main() == 0
    result = json.loads(output.read_text())
    assert result["decision_source"] == "baseline_needs_more_evidence"
    assert result["backend_run_id"] is None
    assert result["evidence_qualification"]["scope"] == "semantic_judgment_only"
    assert report.read_bytes() == original
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert json.loads(output.read_text()) == result


@pytest.mark.parametrize("field", ["qualification", "inventory_provenance", "snapshot"])
@pytest.mark.parametrize("value", [[1], "invalid"])
def test_qualification_rejects_non_object_json_fields(qualified_input, field, value):
    repo, row, qualification, _ = qualified_input
    if field == "qualification":
        qualification = value
    elif field == "inventory_provenance":
        row[field] = value
    else:
        qualification[field] = value
    with pytest.raises(ValueError, match="object"):
        retro.qualify_collected_case(row, repo, qualification)


@pytest.mark.parametrize(
    "saved", [None, [], "invalid", {"rows": None}, {"rows": {}}, {"rows": [None]}]
)
def test_cli_rejects_malformed_report_shapes_without_traceback(
    qualified_input, tmp_path, monkeypatch, capsys, saved
):
    repo, _, qualification, _ = qualified_input
    report, qual, output = (
        tmp_path / name for name in ("saved.json", "qualification.json", "new.json")
    )
    report.write_text(json.dumps(saved))
    qual.write_text(json.dumps(qualification))
    monkeypatch.setattr(
        "sys.argv",
        [
            "adjudicator_retro",
            "--judge-collected-case",
            "one",
            "--report",
            str(report),
            "--qualification",
            str(qual),
            "--repository",
            str(repo),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


@pytest.fixture
def native_cli(qualified_input, tmp_path, monkeypatch):
    repo, row, qualification, _ = qualified_input
    report, enriched, qual, output, journal = (
        tmp_path / name
        for name in (
            "original.json",
            "enriched.json",
            "qualification.json",
            "output.json",
            "native.db",
        )
    )
    report.write_text(json.dumps({"rows": [row]}))
    enriched.write_bytes(report.read_bytes())
    qual.write_text(json.dumps(qualification))
    monkeypatch.setattr(
        roles.dispatcher, "offload", lambda *args, **kwargs: pytest.fail("invalid CLI dispatched")
    )

    def argv(mode):
        return [
            "adjudicator_retro",
            mode,
            "one",
            "--report",
            str(report),
            "--case-input",
            str(enriched),
            "--repository",
            str(repo),
            "--qualification",
            str(qual),
            "--output",
            str(output),
            "--native-journal",
            str(journal),
            "--expected-source-hash",
            hashlib.sha256(report.read_bytes()).hexdigest(),
        ]

    return argv, report, enriched, output, journal


@pytest.mark.parametrize("mode", ["--native-admit", "--native-refresh"])
@pytest.mark.parametrize("which", ["original", "enriched"])
@pytest.mark.parametrize("saved", [[], {"rows": None}, {"rows": {}}, {"rows": [None]}])
def test_native_cli_rejects_malformed_reports_without_traceback(
    native_cli, monkeypatch, capsys, mode, which, saved
):
    argv, report, enriched, output, journal = native_cli
    (report if which == "original" else enriched).write_text(json.dumps(saved))
    before = report.read_bytes()
    monkeypatch.setattr("sys.argv", argv(mode))
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert report.read_bytes() == before
    assert not output.exists() and not journal.exists()


@pytest.mark.parametrize("mode", ["--native-admit", "--native-refresh"])
def test_native_cli_reports_corrupt_journal_without_traceback(
    native_cli, monkeypatch, capsys, mode
):
    argv, report, _, output, journal = native_cli
    before = report.read_bytes()
    journal.write_bytes(b"not a SQLite database")
    monkeypatch.setattr("sys.argv", argv(mode))
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists() and report.read_bytes() == before


@pytest.mark.parametrize("mode", ["--native-admit", "--native-refresh"])
@pytest.mark.parametrize("conflict", [False, True])
def test_native_cli_errors_name_selected_mode(monkeypatch, capsys, mode, conflict):
    monkeypatch.setattr(
        "sys.argv", ["adjudicator_retro", mode, "one"] + (["--retry"] if conflict else [])
    )
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert mode + (" cannot be combined" if conflict else " requires") in capsys.readouterr().err


def supporting_descriptor(repo, row, git, *, ranges=None):
    sha = row["packet"]["ground_truth_evidence"]["merge_sha"]
    data = (repo / "inherited.py").read_bytes()
    return {
        "path": "inherited.py",
        "evaluated_sha": sha,
        "blob_sha": git("rev-parse", sha + ":inherited.py"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "line_ranges": ranges or [],
    }


@pytest.mark.parametrize("ranges", [[], [[1, 1], [3, 3]]])
def test_supporting_sources_are_recollected_separately_and_reach_role_prompt(
    qualified_input, monkeypatch, ranges
):
    repo, row, qualification, git = qualified_input
    descriptor = supporting_descriptor(repo, row, git, ranges=ranges)
    qualification["supporting_sources"] = [descriptor]
    before = copy.deepcopy(row)
    (repo / "inherited.py").write_text("working tree decoy")
    case = retro.qualify_collected_case(row, repo, qualification)
    packet = roles._collected_snapshot(case)
    ground = packet["ground_truth_evidence"]
    collection = ground["collected_evidence"]
    assert {x["path"] for x in collection["sources"]} == {"subject.py", "proof.txt"}
    support = collection["supporting_sources"][0]
    assert support["sha256"] == descriptor["sha256"]
    assert support["exhaustive"] is (not ranges)
    if ranges:
        assert "bytes_utf8" not in support
        assert [x["bytes_utf8"] for x in support["excerpts"]] == ["first = 'λ'\n", "third = 3\n"]
        for excerpt in support["excerpts"]:
            assert excerpt["sha256"] == hashlib.sha256(excerpt["bytes_utf8"].encode()).hexdigest()
    else:
        assert support["bytes_utf8"] == "first = 'λ'\nsecond = 2\nthird = 3\n"
    assert ground["qualification"]["persisted_admission"] is False
    assert ground["qualification"]["external_exhaustiveness"] == "unverified"
    revision = ground["qualification"]["material_evidence_revision"]
    assert len(revision) == 64
    assert (
        roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification)) == packet
    )
    seen = {}

    def offload(backend, prompt, **kwargs):
        seen["prompt"] = prompt
        return {
            "exit": 0,
            "run_id": "synthetic-supporting",
            "output": json.dumps(proposal("supporting-git-path:inherited.py")),
        }

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    monkeypatch.setattr(roles.feedback, "record_role_run", lambda **kwargs: None)
    result = roles.run_adjudicator_agent(case=case, backend="gemini", dispatch=True)
    assert result["decision_source"] == "adjudicator_agent"
    assert "first = '\\u03bb'" in seen["prompt"]
    assert "working tree decoy" not in seen["prompt"]
    assert row == before
    assert (
        retro.summarize([{"packet": packet, "decision": "reject_blocker", "later_truth": "PASS"}])[
            "adjudicated"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "defect",
    [
        "not-list",
        "not-object",
        "forged-hash",
        "forged-blob",
        "stale-revision",
        "diff-collision",
        "duplicate",
        "unsafe-path",
        "injected-text",
        "injected-complete",
        "ranges-not-list",
        "range-zero",
        "range-overlap",
        "range-reversed",
        "range-beyond-file",
        "range-bool",
        "range-float",
        "range-string",
        "range-shape",
    ],
)
def test_supporting_manifest_refuses_forgery_and_invalid_excerpts(qualified_input, defect):
    repo, row, qualification, git = qualified_input
    item = supporting_descriptor(repo, row, git)
    qualification["supporting_sources"] = [item]
    if defect == "not-list":
        qualification["supporting_sources"] = {}
    elif defect == "not-object":
        qualification["supporting_sources"] = ["invalid"]
    elif defect == "forged-hash":
        item["sha256"] = "0" * 64
    elif defect == "forged-blob":
        item["blob_sha"] = "0" * 40
    elif defect == "stale-revision":
        item["evaluated_sha"] = qualification["comparison_base"]
    elif defect == "diff-collision":
        item["path"] = "subject.py"
        item["blob_sha"] = git("rev-parse", item["evaluated_sha"] + ":subject.py")
        item["sha256"] = hashlib.sha256((repo / "subject.py").read_bytes()).hexdigest()
    elif defect == "duplicate":
        qualification["supporting_sources"].append(copy.deepcopy(item))
    elif defect == "unsafe-path":
        item["path"] = "../inherited.py"
    elif defect == "injected-text":
        item["bytes_utf8"] = "forged"
    elif defect == "injected-complete":
        item["complete"] = True
    else:
        item["line_ranges"] = {
            "ranges-not-list": None,
            "range-zero": [[0, 1]],
            "range-overlap": [[1, 2], [2, 3]],
            "range-reversed": [[2, 1]],
            "range-beyond-file": [[1, 4]],
            "range-bool": [[True, 1]],
            "range-float": [[1.0, 1]],
            "range-string": [["1", 1]],
            "range-shape": [[1]],
        }[defect]
    with pytest.raises(ValueError):
        retro.qualify_collected_case(row, repo, qualification)


def test_material_revision_binds_supporting_coverage_and_owned_unknowns(qualified_input):
    repo, row, qualification, git = qualified_input

    def revision():
        return roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]

    original = revision()
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    full = revision()
    qualification["supporting_sources"][0]["line_ranges"] = [[1, 1]]
    excerpt = revision()
    qualification["criterion_mapping"][0]["unknowns"][0][
        "next_action"
    ] = "Recover authentic receipt."
    assert len({original, full, excerpt, revision()}) == 4


def test_supporting_sources_cannot_bypass_complete_prompt_byte_ceiling(qualified_input):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    case = retro.qualify_collected_case(row, repo, qualification)
    packet = roles._collected_snapshot(case)
    prompt = roles.ROLE_REGISTRY["adjudicator"].build_prompt(
        {"case": packet, "complete_collected_case": True}
    )
    with pytest.raises(ValueError, match="prompt exceeds"):
        retro.qualify_collected_case(
            row, repo, qualification, prompt_byte_limit=len(prompt.encode()) - 1
        )


def test_material_revision_is_independent_of_checkout_location(qualified_input, tmp_path):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    other = tmp_path / "other-checkout"
    shutil.copytree(repo, other)
    revisions = [
        roles._collected_snapshot(retro.qualify_collected_case(row, location, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]
        for location in [repo, other]
    ]
    assert revisions[0] == revisions[1]


def test_material_revision_ignores_supporting_order_and_snapshot_transport(qualified_input):
    repo, row, qualification, git = qualified_input
    one = supporting_descriptor(repo, row, git)
    two = copy.deepcopy(one)
    # Independent paths with identical bytes are still distinct evidence sources.
    (repo / "other.py").write_bytes((repo / "inherited.py").read_bytes())
    git("add", "other.py")
    git("commit", "-qm", "another source")
    sha = git("rev-parse", "HEAD")
    # Both supporting files are inherited at a later evaluated commit that only changes subject.
    qualification["comparison_base"] = sha
    (repo / "subject.py").write_text("new value\n")
    git("add", "subject.py")
    git("commit", "-qm", "evaluated subject")
    sha = git("rev-parse", "HEAD")
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"].update(
        merge_sha=sha, head_sha=sha, diff_summary=[{"path": "subject.py"}]
    )
    row["collection_requirements"]["source_paths"] = ["subject.py"]
    qualification["snapshot"]["headRefOid"] = sha
    one["evaluated_sha"] = two["evaluated_sha"] = sha
    two["path"] = "other.py"
    qualification["supporting_sources"] = [one, two]

    def revision():
        return roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]

    first = revision()
    qualification["supporting_sources"].reverse()
    assert revision() == first
    qualification["snapshot"]["transport_timestamp"] = "later collection"
    assert revision() == first


@pytest.mark.parametrize("forged_admission", [False, True])
def test_serialized_supporting_case_cannot_grade_with_complete_patch_and_criteria(
    qualified_input, forged_admission
):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    packet = roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))
    ground = packet["ground_truth_evidence"]
    ground["diff_patch"] = {"text": "actual-looking patch", "revision": ground["merge_sha"]}
    ground["acceptance_criteria"] = [
        {"text": "Complete criterion.", "revision": ground["head_sha"]}
    ]
    ground["qualification"]["persisted_admission"] = forged_admission
    report_row = {
        "packet": packet,
        "decision": "reject_blocker",
        "disposition": "reject_blocker",
        "shadow_verdict": "PASS",
        "later_truth": "PASS",
        "merge_rule_verdict": "PASS",
    }
    summary = retro.summarize([report_row])
    assert summary["adjudicated"] == summary["graded"] == 0
    retro._apply_evidence_floor(report_row)
    assert report_row["disposition"] == "needs_more_evidence"


@pytest.mark.parametrize("inventory", ["source_paths", "diff_summary"])
def test_material_revision_ignores_unordered_changed_inventory(qualified_input, inventory):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]

    def revision():
        return roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]

    first = revision()
    if inventory == "source_paths":
        row["collection_requirements"]["source_paths"].reverse()
    else:
        row["packet"]["ground_truth_evidence"]["diff_summary"].reverse()
    assert revision() == first


def test_supporting_manifest_count_is_bounded_before_git_reads(qualified_input, monkeypatch):
    repo, row, qualification, git = qualified_input
    item = supporting_descriptor(repo, row, git)
    monkeypatch.setattr(
        retro, "_collect_git_blob", lambda *args: pytest.fail("oversized manifest read Git")
    )
    with pytest.raises(ValueError, match="descriptor count limit"):
        retro._collect_supporting_sources(
            [item] * (retro.MAX_SUPPORTING_SOURCES + 1), repo, item["evaluated_sha"], [], 1000
        )


@pytest.mark.parametrize("mode", ["--native-admit", "--native-refresh"])
@pytest.mark.parametrize("which", ["original", "enriched"])
@pytest.mark.parametrize("nested", ["packet", "ground_truth_evidence"])
@pytest.mark.parametrize("invalid", [None, [], "invalid"])
def test_native_cli_rejects_malformed_nested_objects(
    native_cli, monkeypatch, capsys, mode, which, nested, invalid
):
    argv, report, enriched, output, journal = native_cli
    path = report if which == "original" else enriched
    saved = json.loads(path.read_text())
    row = saved["rows"][0]
    if nested == "packet":
        row["packet"] = invalid
    else:
        row["packet"]["ground_truth_evidence"] = invalid
    path.write_text(json.dumps(saved))
    before = report.read_bytes()
    monkeypatch.setattr("sys.argv", argv(mode))
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert "must be an object" in capsys.readouterr().err
    assert report.read_bytes() == before
    assert not output.exists() and not journal.exists()


@pytest.fixture
def native_cohort(qualified_input, tmp_path, monkeypatch):
    repo, row, qualification, _ = qualified_input
    originals = []
    cases = {}
    for number in (1, 2):
        item = copy.deepcopy(row)
        item.update(case_id=str(number), run_id=f"original-{number}")
        item["target"] = f"owner/repo#{number}"
        item["packet"]["target"] = item["target"]
        contract = copy.deepcopy(qualification)
        contract["snapshot"]["number"] = number
        cases[str(number)] = retro.qualify_collected_case(item, repo, contract)
        originals.append(item)
    source = tmp_path / "original-cohort.json"
    source.write_text(
        json.dumps(
            {
                "rows": originals,
                "summary": {"graded": 0},
                "population": 299,
                "preserved": "original",
            }
        )
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    journal = retro.NativeAttemptJournal(tmp_path / "cohort.sqlite")
    counter = iter((1, 2))

    def offload(*_args, **kwargs):
        number = next(counter)
        receipt = {"exit": 0, "run_id": f"native-{number}", "output": json.dumps(proposal())}
        kwargs["attempt_started_callback"]({"run_id": receipt["run_id"]})
        kwargs["attempt_receipt_callback"](receipt)
        return {**receipt, "attempt_receipt_callback_called": True}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    monkeypatch.setattr(roles.feedback, "record_role_run", lambda **_kwargs: None)
    for key, case in cases.items():
        retro.admit_collected_retry(
            original_report=source,
            output=tmp_path / f"admit-{key}.json",
            case_id=key,
            case=case,
            journal=journal,
            dispatch=True,
        )
    # A cohort refresh must never attempt another provider call.
    monkeypatch.setattr(
        roles.dispatcher, "offload", lambda *_args, **_kwargs: pytest.fail("cohort dispatched")
    )
    return source, digest, journal, cases


def test_native_cohort_authenticates_multiple_cases_and_preserves_original(native_cohort, tmp_path):
    source, digest, journal, cases = native_cohort
    before = source.read_bytes()
    with sqlite3.connect(journal.path) as conn:
        journal_before = conn.execute("SELECT * FROM native_attempts").fetchall()
    result = retro.refresh_native_cohort(
        original_report=source,
        output=tmp_path / "candidate.json",
        cases=cases,
        journal=journal,
        expected_source_hash=digest,
        truth_reader=lambda run_id, _db: "PASS" if run_id == "original-1" else None,
    )
    assert result["summary"] == {"graded": 0}
    assert result["population"] == 299
    assert result["native_summary"] == {
        "cases": 2,
        "admitted_cases": 2,
        "unadmitted_cases": 0,
        "graded": 1,
        "agree": 1,
        "disagree": 0,
        "unknown": 1,
        "agreement_rate": 1.0,
        "pending_attempts": 0,
    }
    original = json.loads(before)
    for old, new in zip(original["rows"], result["rows"]):
        assert {key: value for key, value in new.items() if key != "native_projection"} == old
    assert source.read_bytes() == before
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT * FROM native_attempts").fetchall() == journal_before
    # Serialized cohort rows still cannot bypass generic metadata floors.
    assert retro.summarize(result["rows"])["graded"] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "late-history",
        "missing-case",
        "serialized-case",
        "hash",
        "late-contract",
        "duplicate-backend",
        "late-truth",
        "source-race",
        "journal-race",
    ],
)
def test_native_cohort_fails_without_partial_output(native_cohort, tmp_path, damage):
    source, digest, journal, cases = native_cohort
    calls = []

    def truth(run_id, _db):
        calls.append(run_id)
        if run_id == "original-2":
            if damage == "late-truth":
                raise sqlite3.OperationalError("Brain read failed")
            if damage == "source-race":
                source.write_text(source.read_text() + " ")
            if damage == "journal-race":
                with sqlite3.connect(journal.path) as conn:
                    conn.execute("UPDATE native_attempts SET updated_at=updated_at+1")
        return "PASS"

    if damage == "late-history":
        saved = json.loads(source.read_text())
        saved["rows"][-1]["retry_attempts"] = [{"retry_attempts": "malformed"}]
        source.write_text(json.dumps(saved))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    elif damage == "missing-case":
        cases = {"1": cases["1"]}
    elif damage == "serialized-case":
        cases = {**cases, "2": roles._collected_snapshot(cases["2"])}
    elif damage == "hash":
        digest = "0" * 64
    elif damage == "late-contract":
        with sqlite3.connect(journal.path) as conn:
            value = conn.execute(
                "SELECT validated_json FROM native_attempts WHERE case_id='2'"
            ).fetchone()[0]
            result = json.loads(value)
            result["case"]["ground_truth_evidence"]["criterion_provenance"] = {}
            conn.execute(
                "UPDATE native_attempts SET validated_json=? WHERE case_id='2'",
                (json.dumps(result),),
            )
    elif damage == "duplicate-backend":
        with sqlite3.connect(journal.path) as conn:
            conn.execute("UPDATE native_attempts SET backend_run_id='native-1' WHERE case_id='2'")
    output = tmp_path / "refused-cohort.json"
    with pytest.raises((ValueError, TypeError, sqlite3.Error)):
        retro.refresh_native_cohort(
            original_report=source,
            output=output,
            cases=cases,
            journal=journal,
            expected_source_hash=digest,
            truth_reader=truth,
        )
    assert not output.exists()
    if damage not in {"late-truth", "source-race", "journal-race"}:
        assert calls == []


def test_native_cohort_refuses_overwrite_and_clears_forged_markers(native_cohort, tmp_path):
    source, digest, journal, cases = native_cohort
    output = tmp_path / "existing.json"
    output.write_text("existing")
    with pytest.raises(ValueError, match="new and separate"):
        retro.refresh_native_cohort(
            original_report=source,
            output=output,
            cases=cases,
            journal=journal,
            expected_source_hash=digest,
        )
    assert output.read_text() == "existing"
    saved = json.loads(source.read_text())
    saved["rows"][0]["native_projection"] = {"trusted": True, "graded": 999}
    # An unadmitted cohort cannot inherit serialized native authority.
    source.write_text(json.dumps(saved))
    empty = retro.NativeAttemptJournal(tmp_path / "empty-native.sqlite")
    result = retro.refresh_native_cohort(
        original_report=source,
        output=tmp_path / "unadmitted.json",
        cases={},
        journal=empty,
        expected_source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        truth_reader=lambda *_args: pytest.fail("unadmitted truth read"),
    )
    assert result["native_summary"]["graded"] == 0
    assert result["native_summary"]["unadmitted_cases"] == 2
    assert all("native_projection" not in row for row in result["rows"])


@pytest.mark.parametrize("kind", ["missing", "not-native", "schema-less"])
def test_native_cohort_existing_journal_never_initializes(tmp_path, kind):
    path = tmp_path / "journal.sqlite"
    if kind != "missing":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
            if kind == "schema-less":
                conn.execute(f"PRAGMA application_id={retro.NATIVE_JOURNAL_APPLICATION_ID}")
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((sqlite3.Error, ValueError)):
        retro.NativeAttemptJournal.open_existing(path)
    assert (path.read_bytes() if path.exists() else None) == before


def test_native_cohort_cli_does_not_create_a_journal(tmp_path, monkeypatch):
    source = tmp_path / "original.json"
    source.write_text(json.dumps({"rows": [], "summary": {"graded": 0}}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]")
    missing = tmp_path / "missing.sqlite"
    output = tmp_path / "output.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "retro",
            "--native-cohort",
            "--report",
            str(source),
            "--cohort-manifest",
            str(manifest),
            "--native-journal",
            str(missing),
            "--expected-source-hash",
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        retro.main()
    assert exc.value.code == 2
    assert not missing.exists()
    assert not output.exists()
