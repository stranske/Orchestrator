"""Failure-only verifier evidence survives its private JUnit temporary directory."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from pathlib import Path

import pytest

import credential_redaction
import verify


def _junit(path: Path, trace: str) -> None:
    path.write_text(
        "<testsuite><testcase classname='tests.transport' name='test_oversize'>"
        f"<failure message='oversize failed'>{trace}</failure>"
        "</testcase><testcase classname='tests.transport' name='test_ok'/>"
        "</testsuite>",
        encoding="utf-8",
    )


def _failed() -> dict:
    return {"returncode": 1, "failures": ["FAILED tests/test_transport.py::test_oversize"]}


def test_failure_evidence_retains_redacted_bounded_junit_trace_after_source_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "github_pat_abcdefghijklmnopqrstuvwxyz"
    job_root = tmp_path / "job"
    staging = job_root / "staging"
    staging.mkdir(mode=0o700, parents=True)
    temporary = job_root / "tmp" / "verify-run"
    temporary.parent.mkdir()
    temporary.mkdir()
    junit = temporary / "pytest.xml"
    _junit(junit, token + "\n" + "x" * (verify.FAILURE_EVIDENCE_TRACE_BYTES * 2))
    monkeypatch.setattr(credential_redaction, "_known_values", lambda: [token.encode()])

    destination = staging / "pytest-failure-evidence.json"
    verify.write_failure_evidence(destination, junit=junit, py=_failed())
    # This models verify.py's TemporaryDirectory cleanup and the supervisor's tmp retirement.
    shutil.rmtree(temporary.parent)

    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert saved["schema"] == "verify-failure-evidence/v1"
    assert saved["failed_cases"][0]["name"] == "test_oversize"
    assert token not in saved["failed_cases"][0]["trace"]
    assert "[REDACTED" in saved["failed_cases"][0]["trace"]
    assert saved["failed_cases"][0]["truncated"] is True
    assert destination.stat().st_size <= verify.FAILURE_EVIDENCE_MAX_BYTES
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_failure_evidence_refuses_noncanonical_or_existing_destination(tmp_path: Path) -> None:
    junit = tmp_path / "pytest.xml"
    _junit(junit, "trace")
    with pytest.raises(ValueError, match="absolute canonical"):
        verify.write_failure_evidence(
            Path("pytest-failure-evidence.json"), junit=junit, py=_failed()
        )
    destination = tmp_path / "pytest-failure-evidence.json"
    destination.write_text("already exists", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        verify.write_failure_evidence(destination, junit=junit, py=_failed())
    destination.unlink()
    destination.symlink_to(tmp_path / "outside")
    with pytest.raises(FileExistsError, match="already exists"):
        verify.write_failure_evidence(destination, junit=junit, py=_failed())


def test_failure_evidence_refuses_when_redaction_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junit = tmp_path / "pytest.xml"
    _junit(junit, "secret")
    monkeypatch.setenv(credential_redaction.REDACTION_DISABLED_ENV, "1")
    with pytest.raises(ValueError, match="credential redaction disabled"):
        verify.write_failure_evidence(
            tmp_path / "pytest-failure-evidence.json", junit=junit, py=_failed()
        )


def test_four_escape_heavy_failures_are_further_truncated_not_discarded(tmp_path: Path) -> None:
    junit = tmp_path / "pytest.xml"
    escaped = '\\"\n' * (verify.FAILURE_EVIDENCE_TRACE_BYTES * 2)
    cases = "".join(
        "<testcase classname='tests.escaped' name='test_%s'><failure>%s</failure></testcase>"
        % (index, escaped)
        for index in range(4)
    )
    junit.write_text("<testsuite>" + cases + "</testsuite>", encoding="utf-8")
    destination = tmp_path / "pytest-failure-evidence.json"
    verify.write_failure_evidence(
        destination,
        junit=junit,
        py={"returncode": 1, "failures": ["FAILED test_%s" % index for index in range(4)]},
    )

    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert destination.stat().st_size <= verify.FAILURE_EVIDENCE_MAX_BYTES
    assert saved["document_truncated"] is True
    assert [item["name"] for item in saved["failed_cases"]] == [
        "test_0",
        "test_1",
        "test_2",
        "test_3",
    ]
    assert all(
        len(item["trace"].encode("utf-8")) <= verify.FAILURE_EVIDENCE_TRACE_BYTES
        for item in saved["failed_cases"]
    )


def test_missing_junit_retains_redacted_pytest_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "github_pat_abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setattr(credential_redaction, "_known_values", lambda: [token.encode()])
    destination = tmp_path / "pytest-failure-evidence.json"
    verify.write_failure_evidence(
        destination,
        junit=tmp_path / "missing.xml",
        py={"returncode": 4, "usage_error": True, "failures": [], "output": "usage: " + token},
    )
    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert saved["junit_problem"]
    assert token not in saved["pytest_output"]["output"]
    assert "[REDACTED" in saved["pytest_output"]["output"]


def test_verify_writes_failure_evidence_before_its_temporary_directory_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def private_state():
        yield {"line": "test", "ledger": None}

    observed: dict[str, Path] = {}

    def failed_pytest(*, junit: Path, **_kwargs):
        _junit(junit, "actual trace")
        observed["junit"] = junit
        return verify.parse_pytest_output("1 failed in 1s", returncode=1, junit=junit)

    monkeypatch.setattr(verify, "private_state", private_state)
    monkeypatch.setattr(
        verify,
        "declared_rows_at_start",
        lambda _: {"state": "absent", "declared": 0, "missing": None},
    )
    monkeypatch.setattr(verify, "run_pytest", failed_pytest)
    monkeypatch.setattr(verify, "load_floor", lambda: {"collected": 1, "passed": 0})
    monkeypatch.setattr(verify, "selftest_modules", lambda: [])
    monkeypatch.setattr(
        verify, "run_selftests", lambda *a, **kw: {"ok": [], "failed": {}, "skipped": {}}
    )
    monkeypatch.setattr(verify, "run_gates", lambda **kw: {})
    monkeypatch.setattr(verify, "absent_entrypoint_line", lambda _: None)
    monkeypatch.setattr(verify, "core_sibling_skips", lambda _: 0)
    monkeypatch.setattr(verify, "mypy_exempt_modules", lambda: {})
    destination = tmp_path / "pytest-failure-evidence.json"
    code, text = verify.verify(failure_evidence_out=destination)

    assert code == 1 and "failure evidence retained" in text
    assert destination.is_file()
    assert not observed["junit"].exists()


def test_verify_success_leaves_no_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def private_state():
        yield {"line": "test", "ledger": None}

    def passing_pytest(*, junit: Path, **_kwargs):
        junit.write_text(
            "<testsuite><testcase classname='tests.transport' name='test_ok'/></testsuite>",
            encoding="utf-8",
        )
        return verify.parse_pytest_output("1 passed in 1s", junit=junit)

    monkeypatch.setattr(verify, "private_state", private_state)
    monkeypatch.setattr(
        verify,
        "declared_rows_at_start",
        lambda _: {"state": "absent", "declared": 0, "missing": None},
    )
    monkeypatch.setattr(verify, "run_pytest", passing_pytest)
    monkeypatch.setattr(verify, "load_floor", lambda: {"collected": 1, "passed": 1})
    monkeypatch.setattr(verify, "selftest_modules", lambda: [])
    monkeypatch.setattr(
        verify, "run_selftests", lambda *a, **kw: {"ok": [], "failed": {}, "skipped": {}}
    )
    monkeypatch.setattr(verify, "run_gates", lambda **kw: {})
    monkeypatch.setattr(verify, "absent_entrypoint_line", lambda _: None)
    monkeypatch.setattr(verify, "core_sibling_skips", lambda _: 0)
    monkeypatch.setattr(verify, "mypy_exempt_modules", lambda: {})
    destination = tmp_path / "pytest-failure-evidence.json"
    code, _ = verify.verify(failure_evidence_out=destination)

    assert code == 0
    assert not destination.exists()
