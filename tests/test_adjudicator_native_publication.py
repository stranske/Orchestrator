"""Private-fixture controls for authenticated native canonical publication."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_adjudicator_qualified_input import proposal
from test_adjudicator_qualified_input import qualified_input as _qualified_input

import adjudicator_retro as retro
import roles


@pytest.fixture(name="qualified_input")
def qualified_input_fixture(tmp_path):
    """Re-export the real collector fixture without sharing its module state."""
    return _qualified_input.__wrapped__(tmp_path)


def _source(tmp_path: Path):
    row = {
        "case_id": "case-a",
        "run_id": "original-a",
        "target": "owner/repo#1",
        "decision": "needs_more_evidence",
        "retry_attempts": [{"backend_run_id": "original-paid"}],
        "packet": {"ground_truth_evidence": {}},
    }
    source = {"rows": [row], "summary": {"cases": 1, "graded": 0}, "population": 299}
    original = tmp_path / "original.json"
    canonical = tmp_path / "canonical.json"
    encoded = json.dumps(source, indent=2).encode() + b"\n"
    original.write_bytes(encoded)
    canonical.write_bytes(encoded)
    return original, canonical, source


def _record(journal, original: Path, backend_id: str):
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    reserved = journal.reserve("case-a", backend_id, "digest-" + backend_id, source_hash)
    journal.persist_raw(reserved["attempt_id"], {"run_id": backend_id, "output": "{}"})
    journal.validate(
        reserved["attempt_id"],
        {"backend_run_id": backend_id, "raw_output": "{}", "proposal": {}, "errors": ["fixture"]},
    )


def _candidate(*, original_report: Path, output: Path, journal, **_kwargs):
    original = original_report
    result = json.loads(original.read_text())
    result["rows"][0]["native_projection"] = {
        "attempt_id": "selected",
        "trusted": True,
        "decision": "reject_blocker",
        "later_truth": "PASS",
        "graded": 1,
        "agree": True,
    }
    result["native_summary"] = {
        "cases": 1,
        "admitted_cases": 1,
        "unadmitted_cases": 0,
        "graded": 1,
        "agree": 1,
        "disagree": 0,
        "unknown": 0,
        "agreement_rate": 1.0,
        "pending_attempts": 0,
    }
    with sqlite3.connect(journal.path) as conn:
        conn.row_factory = sqlite3.Row
        result["native_attempts"] = [
            dict(row) for row in conn.execute("SELECT * FROM native_attempts ORDER BY attempt_id")
        ]
    output.write_text(json.dumps(result))
    return result


def test_native_publication_rebuilds_summary_preserves_originals_and_accumulates(
    tmp_path, monkeypatch
):
    original, canonical, source = _source(tmp_path)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    _record(journal, original, "native-paid-a")
    monkeypatch.setattr(retro, "refresh_native_cohort", _candidate)
    monkeypatch.setattr(
        retro.feedback,
        "role_cost_report",
        lambda *_args, **_kwargs: {"status": "unmeasured", "cost_usd": None},
    )
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    first = retro.publish_native_cohort(
        original_report=original,
        canonical_path=canonical,
        cases={},
        journal=journal,
        expected_source_hash=source_hash,
        expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
        lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
    )
    published = json.loads(canonical.read_text())
    assert published["summary"]["graded"] == 1
    assert published["rows"][0]["retry_attempts"] == source["rows"][0]["retry_attempts"]
    assert published["population"] == 299
    assert published["native_publication"]["paid_backend_ids"] == ["native-paid-a", "original-paid"]

    _record(journal, original, "native-paid-b")
    second = retro.publish_native_cohort(
        original_report=original,
        canonical_path=canonical,
        cases={},
        journal=journal,
        expected_source_hash=source_hash,
        expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
        lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
    )
    assert first["historical_summary"] == second["historical_summary"]
    assert json.loads(canonical.read_text())["summary"]["graded"] == 1


def test_native_publication_rejects_stale_or_foreign_canonical_and_generic_clobber(
    tmp_path, monkeypatch
):
    original, canonical, _ = _source(tmp_path)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    monkeypatch.setattr(retro, "refresh_native_cohort", _candidate)
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="canonical report changed"):
        retro.publish_native_cohort(
            original_report=original,
            canonical_path=canonical,
            cases={},
            journal=journal,
            expected_source_hash=source_hash,
            expected_canonical_hash="0" * 64,
            lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
        )
    canonical.write_text(
        json.dumps({"rows": [], "native_publication": {"source_report_sha256": "foreign"}})
    )
    with pytest.raises(ValueError, match="foreign"):
        retro.publish_native_cohort(
            original_report=original,
            canonical_path=canonical,
            cases={},
            journal=journal,
            expected_source_hash=source_hash,
            expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
        )
    with pytest.raises(ValueError, match="generic retrospective run"):
        retro.run(path=canonical, dispatch=False, evidence_reader=lambda _row: {})


def test_native_publication_rejects_atomic_replace_failure(tmp_path, monkeypatch):
    original, canonical, _ = _source(tmp_path)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    _record(journal, original, "native-paid-a")
    monkeypatch.setattr(retro, "refresh_native_cohort", _candidate)
    before = canonical.read_bytes()
    real_replace = Path.replace

    def fail_replace(self, target):
        if Path(target) == canonical:
            raise OSError("disk failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        retro.publish_native_cohort(
            original_report=original,
            canonical_path=canonical,
            cases={},
            journal=journal,
            expected_source_hash=hashlib.sha256(original.read_bytes()).hexdigest(),
            expected_canonical_hash=hashlib.sha256(before).hexdigest(),
            lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
        )
    assert canonical.read_bytes() == before


def test_custom_canonical_uses_and_requires_the_generic_persistence_lock(tmp_path, monkeypatch):
    original, canonical, _ = _source(tmp_path)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    _record(journal, original, "native-paid-a")
    shared = tmp_path / ".orchestrator-mirror.publish.lock"
    assert retro._publication_lock_path(canonical) == shared
    monkeypatch.setattr(retro, "refresh_native_cohort", _candidate)
    with pytest.raises(ValueError, match="lock"):
        retro.publish_native_cohort(
            original_report=original,
            canonical_path=canonical,
            cases={},
            journal=journal,
            expected_source_hash=hashlib.sha256(original.read_bytes()).hexdigest(),
            expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            lock_path=tmp_path / "different.lock",
        )


def _validated_native(journal, original, case, backend_id):
    snapshot = roles._collected_snapshot(case)
    raw = json.dumps(proposal())
    reservation = journal.reserve(
        snapshot["ground_truth_evidence"]["qualification"]["case_id"],
        retro._native_substantive_revision(case),
        case._digest,
        hashlib.sha256(original.read_bytes()).hexdigest(),
    )
    journal.persist_raw(reservation["attempt_id"], {"run_id": backend_id, "output": raw})
    result = roles.run_adjudicator_agent(case=case, proposal_json=proposal())
    result.update(backend_run_id=backend_id, raw_output=raw, case=snapshot)
    assert not result["errors"]
    journal.validate(reservation["attempt_id"], result)


def test_real_native_publication_authenticates_case_and_preserves_history(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    row["decision"] = "needs_more_evidence"
    row["run_id"] = "original-run"
    row["retry_attempts"] = [{"backend_run_id": "original-paid"}]
    case = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": [row], "summary": {"graded": 0}, "population": 299}))
    canonical = tmp_path / "canonical.json"
    canonical.write_bytes(original.read_bytes())
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    _validated_native(journal, original, case, "native-paid")
    monkeypatch.setattr(
        retro.feedback,
        "role_cost_report",
        lambda *_args, **_kwargs: {"status": "unmeasured", "cost_usd": None},
    )
    result = retro.publish_native_cohort(
        original_report=original,
        canonical_path=canonical,
        cases={"one": case},
        journal=journal,
        expected_source_hash=hashlib.sha256(original.read_bytes()).hexdigest(),
        expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
        truth_reader=lambda *_args: "PASS",
        lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
    )
    published = json.loads(canonical.read_text())
    assert result["summary"]["graded"] == published["summary"]["graded"] == 1, result["rows"][0][
        "native_projection"
    ]
    assert published["rows"][0]["retry_attempts"] == row["retry_attempts"]
    assert published["native_publication"]["paid_backend_ids"] == [
        "native-paid",
        "original-paid",
    ]
    # The ordinary reader's threshold is deliberately the canonical summary.
    assert (published["summary"]["graded"] >= 1) is True


def test_real_cumulative_publication_retains_a_then_b_and_reader_threshold(
    qualified_input, tmp_path, monkeypatch
):
    repo, prototype, qualification, _ = qualified_input
    rows = []
    cases = {}
    for index in range(29):
        row = json.loads(json.dumps(prototype))
        row.update(
            case_id=f"case-{index}", run_id=f"original-{index}", decision="needs_more_evidence"
        )
        row["retry_attempts"] = [{"backend_run_id": f"original-paid-{index}"}]
        rows.append(row)
        cases[row["case_id"]] = retro.qualify_collected_case(row, repo, qualification)
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"rows": rows, "population": 299, "summary": {"graded": 0}}))
    canonical = tmp_path / "canonical.json"
    canonical.write_bytes(original.read_bytes())
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    monkeypatch.setattr(
        retro.feedback,
        "role_cost_report",
        lambda *_a, **_k: {"status": "unmeasured", "cost_usd": None},
    )
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    admitted = {}
    for index, row in enumerate(rows):
        case_id = row["case_id"]
        admitted[case_id] = cases[case_id]
        _validated_native(journal, original, cases[case_id], f"native-paid-{index}")
        if index not in {0, 1, 27, 28}:
            continue
        result = retro.publish_native_cohort(
            original_report=original,
            canonical_path=canonical,
            cases=admitted,
            journal=journal,
            expected_source_hash=source_hash,
            expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            truth_reader=lambda *_a: "PASS",
            lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
        )
        assert result["summary"]["graded"] == index + 1
        assert (json.loads(canonical.read_text())["summary"]["graded"] >= 29) == (index == 28)
        assert result["rows"][0]["native_projection"]["graded"] == 1
        assert len(result["native_publication"]["paid_backend_ids"]) == 29 + index + 1
        assert result["population"] == 299
    before = canonical.read_bytes()
    with pytest.raises(
        ValueError,
        match="prior authenticated attempt|no trusted native admission|lost|every validated case",
    ):
        retro.publish_native_cohort(
            original_report=original,
            canonical_path=canonical,
            cases={"case-28": cases["case-28"]},
            journal=journal,
            expected_source_hash=source_hash,
            expected_canonical_hash=hashlib.sha256(before).hexdigest(),
            truth_reader=lambda *_a: "PASS",
            lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
        )
    assert canonical.read_bytes() == before


def test_native_publication_holds_journal_writer_lock_through_replace(tmp_path, monkeypatch):
    original, canonical, _ = _source(tmp_path)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    _record(journal, original, "native-paid-a")
    monkeypatch.setattr(retro, "refresh_native_cohort", _candidate)
    real_replace = Path.replace
    locked = []

    def inspect_replace(self, target):
        if Path(target) == canonical:
            with sqlite3.connect(journal.path, timeout=0) as contender:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("UPDATE native_attempts SET updated_at=updated_at+1")
                locked.append(True)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", inspect_replace)
    retro.publish_native_cohort(
        original_report=original,
        canonical_path=canonical,
        cases={},
        journal=journal,
        expected_source_hash=hashlib.sha256(original.read_bytes()).hexdigest(),
        expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
        lock_path=tmp_path / ".orchestrator-mirror.publish.lock",
    )
    assert locked == [True]


def test_destination_lock_contends_across_state_environments(tmp_path, monkeypatch):
    import fcntl
    import os
    import subprocess
    import sys

    canonical = tmp_path / "capability-program" / "adjudicator-retro.json"
    canonical.parent.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    shared = retro._publication_lock_path(canonical)
    with shared.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                """import fcntl, sys
from pathlib import Path
import adjudicator_retro as retro
with retro._publication_lock_path(Path(sys.argv[1])).open('a+') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('contended')
    else:
        print('incorrectly acquired')
""",
                str(canonical),
            ],
            env={
                **os.environ,
                "ORCH_STATE_DIR": str(tmp_path / "other"),
                "PYTHONPATH": str(Path(retro.__file__).parent),
            },
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    assert child.stdout.strip() == "contended"
    live = Path.home() / ".codex/orchestrator/capability-program/adjudicator-retro.json"
    assert (
        retro._publication_lock_path(live)
        == Path.home() / ".codex/.orchestrator-mirror.publish.lock"
    )


@pytest.mark.parametrize("fail_directory", [False, True])
def test_native_publication_syncs_directory_after_replace_and_reports_failure(
    tmp_path, monkeypatch, fail_directory
):
    import os
    import stat

    original, canonical, _ = _source(tmp_path)
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    _record(journal, original, "native-paid-a")
    monkeypatch.setattr(retro, "refresh_native_cohort", _candidate)
    real_fsync = os.fsync
    seen = []
    directory_fd = []

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            # The new canonical generation must already be visible here.
            assert json.loads(canonical.read_text())["summary"]["graded"] == 1
            seen.append("directory")
            directory_fd.append(fd)
            if fail_directory:
                raise OSError("directory durability unknown")
        else:
            seen.append("file")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    kwargs = dict(
        original_report=original,
        canonical_path=canonical,
        cases={},
        journal=journal,
        expected_source_hash=hashlib.sha256(original.read_bytes()).hexdigest(),
        expected_canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest(),
    )
    if fail_directory:
        with pytest.raises(OSError, match="directory durability unknown"):
            retro.publish_native_cohort(**kwargs)
    else:
        retro.publish_native_cohort(**kwargs)
    assert seen[-2:] == ["file", "directory"]
    # A post-rename fsync error is surfaced, never misreported as a rollback.
    assert json.loads(canonical.read_text())["summary"]["graded"] == 1
    with pytest.raises(OSError):
        os.fstat(directory_fd[0])
    with sqlite3.connect(journal.path, timeout=0) as conn:
        conn.execute("BEGIN IMMEDIATE")
