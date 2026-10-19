from __future__ import annotations

import socket
import subprocess

import pytest

import local_verify


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("coverage test attempted an external process or network connection")

    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


def test_capture_evidence_contract_requires_the_local_hook(monkeypatch):
    captured = {}

    def capture(plan, result):
        captured.update(plan=plan, result=result)
        return {"ok": True}

    monkeypatch.setattr("capability_compiler.capture_named_test_evidence", capture)
    assert local_verify.capture_evidence_contract(
        {"capture_hook": "local_verify.named_test_capture"}, {"status": "PASS"}
    ) == {"ok": True}
    assert captured["result"] == {"status": "PASS"}
    with pytest.raises(ValueError, match="does not target"):
        local_verify.capture_evidence_contract({}, {})


def test_tail_and_console_bound_long_output_and_absent_runs():
    assert local_verify._tail("abcdef", 3) == "def"
    assert local_verify._tail("abc", 3) == "abc"
    assert local_verify._console(None) == "(the run did not happen)"
    assert local_verify._console({"returncode": 0}) == "(no output)"
    assert local_verify._console({"stdout_tail": "out\n", "stderr_tail": "err\n"}) == "out\nerr"
    assert local_verify._rc(None) == "n/a"
    assert local_verify._rc({"returncode": None}) == "killed/timed out"
    assert local_verify._rc({"returncode": 4}) == "4"


def test_run_records_success_and_timeout_without_starting_any_agent(monkeypatch, tmp_path):
    calls = []

    def successful_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "good", "")

    monkeypatch.setattr(local_verify.subprocess, "run", successful_run)
    report = local_verify._run("python -m pytest", tmp_path, 9)
    assert report["ok"] is True
    assert report["stdout_tail"] == "good"
    assert calls[0][0] == ["python", "-m", "pytest"]

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["pytest"], 9, output=b"partial")

    monkeypatch.setattr(local_verify.subprocess, "run", timed_out)
    timeout = local_verify._run("pytest", tmp_path, 9)
    assert timeout["returncode"] is None
    assert timeout["stderr_tail"] == "timed out after 9s"
    assert timeout["stdout_tail"] == ""


@pytest.mark.parametrize(
    ("command", "expected"),
    [("python3.12 -m pytest", "python3.12"), ("pytest tests", local_verify.sys.executable)],
)
def test_analysis_python_only_reuses_python_commands(command, expected):
    assert local_verify._analysis_python(command) == expected
    assert local_verify._analysis_python("'unterminated") == local_verify.sys.executable


def test_parse_node_outcomes_uses_last_verbose_outcome_before_summary():
    text = """tests/a.py::test_one PASSED [ 50%]
tests/a.py::test_two FAILED [100%]
tests/a.py::test_one FAILED [100%]
short test summary info
FAILED tests/a.py::test_two - detail
"""
    assert local_verify._parse_node_outcomes(text) == [
        ("tests/a.py::test_one", "FAILED"),
        ("tests/a.py::test_two", "FAILED"),
    ]


@pytest.mark.parametrize(
    ("report", "python", "expected"),
    [
        ({"returncode": None}, "python", "timed out after 7s"),
        ({"returncode": 5}, "python", "collected no tests"),
        ({"returncode": 2}, "python", "collection error"),
        ({"returncode": 4, "stderr_tail": "bad option"}, "python", "rejected"),
        (
            {"returncode": 1, "stderr_tail": "No module named pytest"},
            "chosen-python",
            "not importable",
        ),
        ({"returncode": 1}, "python", "no parseable"),
    ],
)
def test_node_probe_reason_names_each_unavailable_attribution(report, python, expected):
    assert expected in local_verify._node_probe_reason(report, python, 7)


def test_analyse_nodes_marks_hollow_and_inconclusive_nodes(monkeypatch, tmp_path):
    report = {"returncode": 1, "ok": False, "stdout_tail": "", "stderr_tail": ""}
    monkeypatch.setattr(
        local_verify,
        "_run_argv",
        lambda *_args, **_kwargs: (
            report,
            "tests/a.py::test_real FAILED\ntests/a.py::test_old PASSED\ntests/a.py::test_skip SKIPPED\n",
        ),
    )
    result = local_verify._analyse_nodes(tmp_path, "python -m pytest", ["tests/a.py"], 12)
    assert result["verdict"] == "FAIL_HOLLOW_NODES"
    assert result["counts"] == {"nodes": 3, "discriminates": 1, "hollow": 1, "inconclusive": 1}
    assert result["hollow_nodes"] == ["tests/a.py::test_old"]


def test_run_argv_preserves_partial_text_after_a_timeout(monkeypatch, tmp_path):
    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["pytest"], 9, output="partial node output")

    monkeypatch.setattr(local_verify.subprocess, "run", timed_out)
    report, stdout = local_verify._run_argv(["pytest"], tmp_path, 9)
    assert report["returncode"] is None
    assert report["stdout_tail"] == "partial node output"
    assert stdout == "partial node output"


def test_main_renders_json_and_transcript_without_changing_the_verdict(monkeypatch, capsys):
    monkeypatch.setattr(local_verify, "_capability_heartbeat", lambda: None)
    calls = []

    def verify(worktree, **kwargs):
        calls.append((worktree, kwargs))
        return {"ok": True, "verdict": "PASS", "worktree": worktree}

    monkeypatch.setattr(local_verify, "verify", verify)
    monkeypatch.setattr(local_verify, "record_verdict", lambda run_id, result: {"run_id": run_id})
    monkeypatch.setattr(local_verify, "break_transcript", lambda result: "transcript\n")

    assert local_verify.main(["--worktree", "/candidate", "--test-cmd", "pytest"]) == 0
    assert '"verdict": "PASS"' in capsys.readouterr().out
    assert calls == [
        (
            "/candidate",
            {"base_ref": "HEAD", "test_cmd": "pytest", "test_paths": None, "timeout": 120},
        )
    ]

    assert (
        local_verify.main(
            [
                "--worktree",
                "/candidate",
                "--test-cmd",
                "pytest",
                "--record-run-id",
                "run-1",
                "--transcript",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "transcript\n"


def test_analyse_nodes_rejects_non_python_candidate_paths(tmp_path):
    result = local_verify._analyse_nodes(tmp_path, "pytest", ["notes.txt"], 12)
    assert result["supported"] is False
    assert "no candidate path" in result["reason"]


def test_overlay_validation_explains_node_ids_and_full_overlay():
    result = {
        "red": {"ok": False},
        "test_paths": ["tests/a.py::test_x"],
        "copied_test_paths": [],
        "changed_paths": ["tests/a.py"],
    }
    reasons = local_verify.invalid_demonstration(result)
    assert len(reasons) == 1 and "not copied" in reasons[0]
    assert local_verify.overlay_covers_every_change(
        {"red": {"ok": False}, "changed_paths": ["tests/a.py"], "copied_test_paths": ["tests"]}
    )
    assert local_verify.name_nodes(["a", "b", "c"], limit=2) == "a, b (+1 more)"


def test_break_transcript_quotes_invalid_demonstration_and_safe_fence():
    result = {
        "verdict": "PASS",
        "base_ref": "main",
        "worktree": "/work",
        "test_paths": ["tests/a.py"],
        "green": {
            "cmd": "pytest tests/a.py",
            "returncode": 0,
            "stdout_tail": "green ```",
            "stderr_tail": "",
        },
        "red": {"returncode": 1, "stdout_tail": "red", "stderr_tail": ""},
        "node_verdict": "PASS",
        "hollow_nodes": [],
        "node_analysis": {"counts": {"nodes": 1}},
        "changed_paths": [],
        "copied_test_paths": [],
    }
    transcript = local_verify.break_transcript(result, worktree_label="candidate")
    assert "RED — the gate" in transcript and "GREEN — the same gate" in transcript
    assert "````" in transcript
    assert "All 1 candidate nodes fail" in transcript


def test_changed_paths_and_candidate_copy_handle_rename_and_directories(monkeypatch, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tests").mkdir()
    (worktree / "tests" / "new.py").write_text("x = 1\n")
    destination = tmp_path / "destination"
    destination.mkdir()

    def fake_git(_worktree, args):
        if args[:2] == ["diff", "--name-only"]:
            return subprocess.CompletedProcess(args, 0, b"src/a.py\n", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def fake_status(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            [], 0, " R old.py -> tests/new.py\n?? tests/extra.py\n", ""
        )

    monkeypatch.setattr(local_verify, "_git", fake_git)
    monkeypatch.setattr(local_verify.subprocess, "run", fake_status)
    assert local_verify._changed_paths(worktree, "main") == [
        "src/a.py",
        "tests/extra.py",
        "tests/new.py",
    ]
    assert local_verify._copy_candidate_paths(
        worktree, destination, ["tests/new.py", "missing.py"]
    ) == ["tests/new.py"]
    assert (destination / "tests" / "new.py").read_text() == "x = 1\n"
    (worktree / "tests" / "nested").mkdir()
    (worktree / "tests" / "nested" / "extra.py").write_text("nested = True\n")
    assert local_verify._copy_candidate_paths(worktree, destination, ["tests"]) == ["tests"]
    assert (destination / "tests" / "nested" / "extra.py").read_text() == "nested = True\n"
