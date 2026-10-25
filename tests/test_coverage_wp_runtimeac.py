"""Hermetic regression coverage for runtime_ac.py uncovered lines."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

import runtime_ac


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Every target reads and writes only the test's disposable runtime."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


# capture_evidence_contract (line 94)


def test_capture_evidence_contract_wrong_hook():
    with pytest.raises(ValueError, match=r"^plan does not target the runtime_ac capture hook$"):
        runtime_ac.capture_evidence_contract({"capture_hook": "wrong"}, {})


def test_capture_evidence_contract_valid():
    with mock.patch("capability_compiler.capture_named_test_evidence", return_value={"ok": True}):
        result = runtime_ac.capture_evidence_contract(
            {"capture_hook": "runtime_ac.named_test_capture"}, {}
        )
        assert result == {"ok": True}


# _read_optional (line 180)


def test_read_optional_none():
    assert runtime_ac._read_optional(None) == ""


def test_read_optional_empty():
    assert runtime_ac._read_optional("") == ""


def test_read_optional_file(monkeypatch, tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("content")
    assert runtime_ac._read_optional(str(f)) == "content"


# build_authoring_prompt (line 193)


def test_build_authoring_prompt_empty_goal():
    with pytest.raises(ValueError, match=r"^goal must be non-empty$"):
        runtime_ac.build_authoring_prompt(goal="")


def test_build_authoring_prompt_with_all_params():
    p = runtime_ac.build_authoring_prompt(
        goal="Test", repo="o/r", target="o/r#1", context="ctx", context_file=None
    )
    assert "Repository: o/r" in p and "Target: o/r#1" in p and "Additional context:\nctx" in p


# _validate_string_list (lines 249, 251, 254)


def test_validate_string_list_not_list():
    errs = runtime_ac._validate_string_list("x", "p", nonempty=False)
    assert errs == ["p must be a list"]


def test_validate_string_list_empty_nonempty():
    errs = runtime_ac._validate_string_list([], "p", nonempty=True)
    assert errs == ["p must be a non-empty list"]


def test_validate_string_list_bad_item():
    errs = runtime_ac._validate_string_list(["  "], "p", nonempty=False)
    assert errs == ["p[0] must be a non-empty string"]


# _validate_required_mapping (line 260)


def test_validate_required_mapping_not_dict():
    errs = runtime_ac._validate_required_mapping("x", "p", ["k"])
    assert errs == ["p must be an object"]


def test_validate_required_mapping_missing():
    errs = runtime_ac._validate_required_mapping({}, "p", ["k"])
    assert errs == ["p.k is required"]


# _unsafe_command_reason (lines 267-268, 270, 279, 283, 285)


def test_unsafe_command_empty():
    assert runtime_ac._unsafe_command_reason("") == "must not be empty"


def test_unsafe_command_rm():
    r = runtime_ac._unsafe_command_reason("rm -rf /")
    assert r and "destructive" in r


def test_unsafe_command_sudo():
    r = runtime_ac._unsafe_command_reason("sudo x")
    assert r and "sudo" in r


def test_unsafe_command_git_reset():
    r = runtime_ac._unsafe_command_reason("git reset --hard")
    assert r and "mutating git" in r


def test_unsafe_command_gh_pr_merge():
    r = runtime_ac._unsafe_command_reason("gh pr merge")
    assert r and "mutating gh pr" in r


def test_unsafe_command_flag():
    r = runtime_ac._unsafe_command_reason("pytest --apply")
    assert r and "mutating flag" in r


def test_unsafe_command_safe():
    assert runtime_ac._unsafe_command_reason("pytest tests/") is None


# _command_review_warnings (lines 293, 296)


def test_command_review_warnings_pipe():
    w = runtime_ac._command_review_warnings("a | b", "p")
    assert any("|" in x for x in w)


def test_command_review_warnings_star():
    w = runtime_ac._command_review_warnings("a * b", "p")
    assert any("*" in x for x in w)


def test_command_review_warnings_clean():
    assert runtime_ac._command_review_warnings("pytest", "p") == []


# _evidence_for_check (line 322)


def test_evidence_for_check_frontend():
    assert runtime_ac._evidence_for_check({"type": "frontend"}) == {"runtime_behavior"}


def test_evidence_for_check_deliberate_break():
    assert runtime_ac._evidence_for_check({"type": "deliberate_break"}) == {
        "test_output",
        "deliberate_break",
    }


def test_evidence_for_check_command_pytest():
    assert runtime_ac._evidence_for_check({"type": "command", "command": "pytest"}) == {
        "test_output"
    }


def test_evidence_for_check_command_other():
    assert runtime_ac._evidence_for_check({"type": "command", "command": "echo x"}) == {
        "command_output"
    }


# _validate_check (lines 330, 334, 336, 346-347, 349, 355, 362, 364, 366, 371, 384, 390, 394, 404, 409)


def test_validate_check_not_dict():
    errs, _ = runtime_ac._validate_check("x", "p", set())
    assert any("must be an object" in e for e in errs)


def test_validate_check_empty_id():
    errs, _ = runtime_ac._validate_check({"id": "", "type": "command", "name": "x"}, "p", set())
    assert any("id must be a non-empty string" in e for e in errs)


def test_validate_check_bad_id():
    errs, _ = runtime_ac._validate_check(
        {"id": "123bad", "type": "command", "name": "x"}, "p", set()
    )
    assert any("id must start with a letter" in e for e in errs)


def test_validate_check_dup_id():
    errs, _ = runtime_ac._validate_check({"id": "x", "type": "command", "name": "x"}, "p", {"x"})
    assert any("duplicates" in e for e in errs)


def test_validate_check_bad_type():
    errs, _ = runtime_ac._validate_check({"id": "x", "type": "bad", "name": "x"}, "p", set())
    assert any("type must be one of" in e for e in errs)


def test_validate_check_empty_name():
    errs, _ = runtime_ac._validate_check({"id": "x", "type": "command", "name": ""}, "p", set())
    assert any("name must be a non-empty string" in e for e in errs)


def test_validate_check_command_missing_cmd():
    errs, _ = runtime_ac._validate_check({"id": "x", "type": "command", "name": "x"}, "p", set())
    assert any("command is required" in e or "command" in e for e in errs)


def test_validate_check_command_empty_cmd():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "command", "name": "x", "command": "", "expected": "exit_0"},
        "p",
        set(),
    )
    assert any("command must be a non-empty string" in e for e in errs)


def test_validate_check_command_unsafe():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "command", "name": "x", "command": "rm -rf /", "expected": "exit_0"},
        "p",
        set(),
    )
    assert any("command" in e and "destructive" in e for e in errs)


def test_validate_check_command_bad_expected():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "command", "name": "x", "command": "echo x", "expected": "bad"},
        "p",
        set(),
    )
    assert any("expected must be one of" in e for e in errs)


def test_validate_check_command_contains_no_field():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "command", "name": "x", "command": "echo x", "expected": "contains"},
        "p",
        set(),
    )
    assert any("contains is required" in e for e in errs)


def test_validate_check_command_regex_no_pattern():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "command", "name": "x", "command": "echo x", "expected": "regex"},
        "p",
        set(),
    )
    assert any("pattern is required" in e for e in errs)


def test_validate_check_frontend_missing():
    errs, _ = runtime_ac._validate_check({"id": "x", "type": "frontend", "name": "x"}, "p", set())
    assert any("url is required" in e for e in errs)
    assert any("assertions is required" in e for e in errs)


def test_validate_check_frontend_empty_url():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "frontend", "name": "x", "url": "", "assertions": ["text:a"]},
        "p",
        set(),
    )
    assert any("url must be a non-empty string" in e for e in errs)


def test_validate_check_frontend_empty_assertions():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "frontend", "name": "x", "url": "http://x.com", "assertions": []},
        "p",
        set(),
    )
    assert any("must be a non-empty list" in e for e in errs)


def test_validate_check_frontend_bad_assertion():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "frontend", "name": "x", "url": "http://x.com", "assertions": ["bad"]},
        "p",
        set(),
    )
    assert any("must start with" in e for e in errs)


def test_validate_check_frontend_bad_optional():
    errs, _ = runtime_ac._validate_check(
        {
            "id": "x",
            "type": "frontend",
            "name": "x",
            "url": "http://x.com",
            "assertions": ["text:a"],
            "click_text": 123,
        },
        "p",
        set(),
    )
    assert any("click_text must be a string or null" in e for e in errs)


def test_validate_check_deliberate_break_missing():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "deliberate_break", "name": "x"}, "p", set()
    )
    assert any("test_cmd is required" in e or "test_cmd" in e for e in errs)
    assert any("test_paths is required" in e or "test_paths" in e for e in errs)


def test_validate_check_deliberate_break_empty_cmd():
    errs, _ = runtime_ac._validate_check(
        {
            "id": "x",
            "type": "deliberate_break",
            "name": "x",
            "test_cmd": "",
            "test_paths": ["a.py"],
        },
        "p",
        set(),
    )
    assert any("test_cmd must be a non-empty string" in e for e in errs)


def test_validate_check_deliberate_break_unsafe():
    errs, _ = runtime_ac._validate_check(
        {
            "id": "x",
            "type": "deliberate_break",
            "name": "x",
            "test_cmd": "rm -rf /",
            "test_paths": ["a.py"],
        },
        "p",
        set(),
    )
    assert any("test_cmd" in e and "destructive" in e for e in errs)


def test_validate_check_deliberate_break_empty_paths():
    errs, _ = runtime_ac._validate_check(
        {
            "id": "x",
            "type": "deliberate_break",
            "name": "x",
            "test_cmd": "pytest",
            "test_paths": [],
        },
        "p",
        set(),
    )
    assert any("must be a non-empty list" in e for e in errs)


def test_validate_check_deliberate_break_bad_optional():
    errs, _ = runtime_ac._validate_check(
        {
            "id": "x",
            "type": "deliberate_break",
            "name": "x",
            "test_cmd": "pytest",
            "test_paths": ["a.py"],
            "worktree": 123,
        },
        "p",
        set(),
    )
    assert any("worktree must be a string or null" in e for e in errs)


def test_validate_check_manual_missing():
    errs, _ = runtime_ac._validate_check({"id": "x", "type": "manual", "name": "x"}, "p", set())
    assert any("instructions is required" in e for e in errs)


def test_validate_check_manual_empty():
    errs, _ = runtime_ac._validate_check(
        {"id": "x", "type": "manual", "name": "x", "instructions": ""}, "p", set()
    )
    assert any("instructions must be a non-empty string" in e for e in errs)


# validate_spec (lines 419, 424, 433, 435, 438, 441, 443, 447-448, 455, 464, 468-469, 471, 487, 490, 492, 500, 508-509, 511, 528, 532-533, 537-540, 544, 547, 549, 551)


def test_validate_spec_not_dict():
    errs = runtime_ac.validate_spec("x")
    assert any("must be a JSON object" in e for e in errs)


def test_validate_spec_missing_top():
    errs = runtime_ac.validate_spec({})
    assert any("verification is required" in e for e in errs)
    assert any("acceptance_criteria is required" in e for e in errs)
    assert any("verdict_policy is required" in e for e in errs)


def test_validate_spec_verification_missing():
    errs = runtime_ac.validate_spec(
        {"verification": {}, "acceptance_criteria": [], "verdict_policy": {}}
    )
    assert any("id is required" in e for e in errs)


def test_validate_spec_verification_empty_id():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("id must be a non-empty string" in e for e in errs)


def test_validate_spec_verification_bad_slug():
    errs = runtime_ac.validate_spec(
        {
            "verification": {
                "id": "Bad-Id",
                "title": "t",
                "goal": "g",
                "risk_level": "low",
            },
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("must be a lowercase slug" in e for e in errs)


def test_validate_spec_verification_empty_title():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("title must be a non-empty string" in e for e in errs)


def test_validate_spec_verification_bad_target():
    errs = runtime_ac.validate_spec(
        {
            "verification": {
                "id": "x",
                "title": "t",
                "goal": "g",
                "risk_level": "low",
                "target": 123,
            },
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("target must be a string or null" in e for e in errs)


def test_validate_spec_verification_bad_risk():
    errs = runtime_ac.validate_spec(
        {
            "verification": {
                "id": "x",
                "title": "t",
                "goal": "g",
                "risk_level": "bad",
            },
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("risk_level must be one of" in e for e in errs)


def test_validate_spec_runtime_context_not_dict():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "runtime_context": "bad",
        }
    )
    assert any("runtime_context must be an object" in e for e in errs)


def test_validate_spec_runtime_context_bad_worktree():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "runtime_context": {"worktree": 123},
        }
    )
    assert any("worktree must be a string or null" in e for e in errs)


def test_validate_spec_runtime_context_unsafe_serve():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "runtime_context": {"serve_command": "rm -rf /"},
        }
    )
    assert any("serve_command" in e and "destructive" in e for e in errs)


def test_validate_spec_criteria_not_list():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": "bad",
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("acceptance_criteria must be a list" in e for e in errs)


def test_validate_spec_criteria_empty():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("acceptance_criteria must be a non-empty list" in e for e in errs)


def test_validate_spec_criteria_dict_not_list():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": {
                "id": "AC1",
                "statement": "s",
                "evidence_required": ["manual_review"],
                "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
            },
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("acceptance_criteria must be a list" in e for e in errs)


def test_validate_spec_non_regression_dict_unsafe():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": [{"name": "n", "command": "rm -rf /"}],
        }
    )
    assert any("command" in e and "destructive" in e for e in errs)


def test_validate_spec_criterion_empty_id():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "",
                    "statement": "s",
                    "evidence_required": ["runtime_behavior"],
                    "checks": [],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("id must be a non-empty string" in e for e in errs)


def test_validate_spec_criterion_bad_id():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "123bad",
                    "statement": "s",
                    "evidence_required": ["runtime_behavior"],
                    "checks": [],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("id must start with a letter" in e for e in errs)


def test_validate_spec_criterion_empty_statement():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "",
                    "evidence_required": ["runtime_behavior"],
                    "checks": [],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("statement must be a non-empty string" in e for e in errs)


def test_validate_spec_criteria_missing_evidence():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["runtime_behavior"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("do not provide required evidence" in e for e in errs)


def test_validate_spec_non_regression_not_list():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": "bad",
        }
    )
    assert any("non_regression must be a list" in e for e in errs)


def test_validate_spec_non_regression_string_empty():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": [""],
        }
    )
    assert any("must be a non-empty string" in e for e in errs)


def test_validate_spec_non_regression_unsafe():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": ["rm -rf /"],
        }
    )
    assert any("command" in e and "destructive" in e for e in errs)


def test_validate_spec_non_regression_dict_missing_name():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": [{"command": "pytest"}],
        }
    )
    assert any("name is required" in e for e in errs)


def test_validate_spec_non_regression_dict_empty_command():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": [{"name": "n", "command": ""}],
        }
    )
    assert any("command must be a non-empty string" in e for e in errs)


def test_validate_spec_non_regression_bad_type():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
            "non_regression": [123],
        }
    )
    assert any("must be a string command or object" in e for e in errs)


def test_validate_spec_policy_missing():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {},
        }
    )
    assert any("require_runtime_evidence is required" in e for e in errs)


def test_validate_spec_policy_bad_boolean():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": "yes",
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("must be a boolean" in e for e in errs)


def test_validate_spec_policy_require_runtime_no_evidence():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": True,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("require_runtime_evidence is true but no frontend/runtime" in e for e in errs)


def test_validate_spec_policy_require_deliberate_break():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": True,
                "fail_on_missing_checks": True,
            },
        }
    )
    assert any("require_deliberate_break_for_tests is true but no" in e for e in errs)


def test_validate_spec_policy_bad_required_id():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
                "required_check_ids": ["UNKNOWN"],
            },
        }
    )
    assert any("references unknown check id" in e for e in errs)


def test_validate_spec_policy_bad_min_ratio():
    errs = runtime_ac.validate_spec(
        {
            "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
            "acceptance_criteria": [
                {
                    "id": "AC1",
                    "statement": "s",
                    "evidence_required": ["manual_review"],
                    "checks": [{"id": "C1", "type": "manual", "name": "n", "instructions": "i"}],
                }
            ],
            "verdict_policy": {
                "require_runtime_evidence": False,
                "require_deliberate_break_for_tests": False,
                "fail_on_missing_checks": True,
                "min_pass_ratio": 2.0,
            },
        }
    )
    assert any("min_pass_ratio must be between 0.0 and 1.0" in e for e in errs)


# parse_spec_json (line 616)


def test_parse_spec_json_plain():
    result = runtime_ac.parse_spec_json('{"verification": {"id": "x"}}')
    assert result == {"verification": {"id": "x"}}


def test_parse_spec_json_fence():
    result = runtime_ac.parse_spec_json('```json\n{"verification": {"id": "x"}}\n```')
    assert result == {"verification": {"id": "x"}}


def test_parse_spec_json_not_object():
    with pytest.raises(ValueError, match="must decode to an object"):
        runtime_ac.parse_spec_json("[1,2]")


# _frontend_command (lines 629, 631, 633)


def test_frontend_command_basic():
    result = runtime_ac._frontend_command({"url": "http://x.com", "assertions": ["text:a"]})
    assert "frontend_verify.py" in result and "--url" in result


def test_frontend_command_with_click():
    result = runtime_ac._frontend_command(
        {"url": "http://x.com", "assertions": [], "click_text": "b"}
    )
    assert "--click-text" in result


def test_frontend_command_with_then_text():
    result = runtime_ac._frontend_command(
        {"url": "http://x.com", "assertions": [], "then_text": "t"}
    )
    assert "--then-text" in result


def test_frontend_command_with_screenshot():
    result = runtime_ac._frontend_command(
        {"url": "http://x.com", "assertions": [], "screenshot": "s.png"}
    )
    assert "--screenshot" in result


def test_frontend_command_with_browser():
    result = runtime_ac._frontend_command(
        {"url": "http://x.com", "assertions": [], "browser_endpoint": "http://127.0.0.1:9222"}
    )
    assert "--browser-endpoint" in result


# _non_regression_command (line 663)


def test_non_regression_command_string():
    result = runtime_ac._non_regression_command("pytest", 0)
    assert result["id"] == "NR1" and result["command"] == "pytest"


def test_non_regression_command_dict():
    result = runtime_ac._non_regression_command({"id": "x", "name": "n", "command": "c"}, 0)
    assert result["id"] == "x" and result["name"] == "n"


# build_dry_run_plan (lines 675, 732, 755)


def test_build_dry_run_plan_invalid():
    with pytest.raises(ValueError, match="invalid runtime AC spec"):
        runtime_ac.build_dry_run_plan({})


def test_build_dry_run_plan_valid():
    plan = runtime_ac.build_dry_run_plan(runtime_ac._valid_spec())
    assert "verification_id" in plan and "planned_checks" in plan


def test_build_dry_run_plan_serve_warnings():
    spec = runtime_ac._valid_spec()
    spec["runtime_context"]["serve_command"] = "printf $HOME"
    plan = runtime_ac.build_dry_run_plan(spec)
    assert any("shell expansion marker" in w for w in plan["warnings"])


def test_build_dry_run_plan_non_regression_warnings():
    spec = runtime_ac._valid_spec()
    spec["non_regression"] = [{"name": "test", "command": "printf $HOME"}]
    plan = runtime_ac.build_dry_run_plan(spec)
    assert any("shell expansion marker" in w for w in plan["warnings"])


def test_build_dry_run_plan_non_regression_dict():
    spec = runtime_ac._valid_spec()
    spec["non_regression"] = [{"name": "test", "command": "pytest"}]
    plan = runtime_ac.build_dry_run_plan(spec)
    assert len(plan["non_regression"]) == 1


# format_plan_text (lines 827-828)


def test_format_plan_text_basic():
    plan = runtime_ac.build_dry_run_plan(runtime_ac._valid_spec())
    text = runtime_ac.format_plan_text(plan)
    assert "Runtime AC verification plan" in text


def test_format_plan_text_with_warnings():
    spec = runtime_ac._valid_spec()
    spec["runtime_context"]["serve_command"] = "printf $HOME"
    plan = runtime_ac.build_dry_run_plan(spec)
    text = runtime_ac.format_plan_text(plan)
    assert "Warnings:" in text


def test_format_plan_text_emit_commands():
    plan = runtime_ac.build_dry_run_plan(runtime_ac._valid_spec())
    text = runtime_ac.format_plan_text(plan, emit_commands=True)
    assert "Commands:" in text


# _coerce_result_doc (lines 863, 869)


def test_coerce_result_doc_list():
    result = runtime_ac._coerce_result_doc([{"id": "x", "status": "PASS"}])
    assert result == [{"id": "x", "status": "PASS"}]


def test_coerce_result_doc_dict_check_results():
    result = runtime_ac._coerce_result_doc({"check_results": [{"id": "x", "status": "PASS"}]})
    assert result == [{"id": "x", "status": "PASS"}]


def test_coerce_result_doc_invalid():
    with pytest.raises(ValueError, match="result JSON must be"):
        runtime_ac._coerce_result_doc("bad")


# evaluate_results (lines 884, 891, 894, 896, 899, 902, 974, 975, 976)


def test_evaluate_results_invalid_spec():
    with pytest.raises(ValueError, match="invalid runtime AC spec"):
        runtime_ac.evaluate_results({}, [])


def test_evaluate_results_not_dict():
    with pytest.raises(ValueError, match=r"result\[0\] must be an object"):
        runtime_ac.evaluate_results(runtime_ac._valid_spec(), ["bad"])


def test_evaluate_results_empty_id():
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        runtime_ac.evaluate_results(runtime_ac._valid_spec(), [{"id": "", "status": "PASS"}])


def test_evaluate_results_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate result ids"):
        runtime_ac.evaluate_results(
            runtime_ac._valid_spec(),
            [{"id": "AC1-FE", "status": "PASS"}, {"id": "AC1-FE", "status": "PASS"}],
        )


def test_evaluate_results_unknown_status():
    result = runtime_ac.evaluate_results(
        runtime_ac._valid_spec(), [{"id": "AC1-FE", "status": "UNKNOWN"}]
    )
    assert result["verdict"] in {"PASS", "FAIL", "NEEDS_REVIEW"}


def test_evaluate_results_all_pass():
    spec = runtime_ac._valid_spec()
    check_ids = runtime_ac._all_ac_check_ids(spec)
    result = runtime_ac.evaluate_results(
        spec, {"check_results": [{"id": cid, "status": "PASS"} for cid in check_ids]}
    )
    assert result["verdict"] == "PASS"


def test_evaluate_results_all_fail():
    spec = runtime_ac._valid_spec()
    check_ids = runtime_ac._all_ac_check_ids(spec)
    result = runtime_ac.evaluate_results(
        spec, {"check_results": [{"id": cid, "status": "FAIL"} for cid in check_ids]}
    )
    assert result["verdict"] == "FAIL"


def test_evaluate_results_needs_review():
    spec = runtime_ac._valid_spec()
    check_ids = runtime_ac._all_ac_check_ids(spec)
    result = runtime_ac.evaluate_results(
        spec, {"check_results": [{"id": cid, "status": "NEEDS_REVIEW"} for cid in check_ids]}
    )
    assert result["verdict"] == "NEEDS_REVIEW"


def test_evaluate_results_non_regression_fail():
    spec = runtime_ac._valid_spec()
    spec["non_regression"] = ["pytest"]
    result = runtime_ac.evaluate_results(
        spec,
        {
            "check_results": [
                {"id": "AC1-FE", "status": "PASS", "type": "frontend"},
                {"id": "AC1-TEST", "status": "PASS", "type": "command"},
                {"id": "AC1-BREAK", "status": "PASS", "type": "deliberate_break"},
                {"id": "AC2-FE", "status": "PASS", "type": "frontend"},
                {"id": "NR1", "status": "FAIL", "type": "non_regression"},
            ]
        },
    )
    assert result["verdict"] == "FAIL"


# record_gate_verdict (lines 1018, 1021, 1026, 1043)


def test_record_gate_verdict_basic(monkeypatch):
    with mock.patch("feedback.record_outcome"), mock.patch(
        "feedback.record_completion_event"
    ), mock.patch("feedback._completion_hash", return_value="h"):
        result = runtime_ac.record_gate_verdict("run1", {"verdict": "PASS", "verification_id": "x"})
        assert result["run_id"] == "run1"


# _completed_result (lines 1070-1075, 1084, 1087-1088, 1091, 1093, 1103-1106, 1108-1133)


def test_completed_result_frontend_pass():
    c = {"id": "C1", "type": "frontend"}
    comp = SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_frontend_fail():
    c = {"id": "C1", "type": "frontend"}
    comp = SimpleNamespace(returncode=0, stdout='{"ok": false, "error": "e"}', stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


def test_completed_result_deliberate_break_pass():
    c = {"id": "C1", "type": "deliberate_break", "test_cmd": "pytest"}
    comp = SimpleNamespace(returncode=0, stdout='{"verdict": "PASS"}', stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_deliberate_break_hollow():
    c = {"id": "C1", "type": "deliberate_break", "test_cmd": "pytest"}
    # Need node_verdict != PASS or hollow_nodes to be truthy for _hollow_carry_condition
    comp = SimpleNamespace(
        returncode=0,
        stdout='{"verdict": "PASS", "reason": "test reason", "node_verdict": "FAIL_HOLLOW_NODES", "hollow_nodes": ["x"]}',
        stderr="",
    )
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS" and r["reason"] == "test reason"


def test_completed_result_frontend_not_json():
    c = {"id": "C1", "type": "frontend"}
    comp = SimpleNamespace(returncode=0, stdout="not json", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


def test_completed_result_deliberate_break_not_dict():
    c = {"id": "C1", "type": "deliberate_break", "test_cmd": "pytest"}
    comp = SimpleNamespace(returncode=0, stdout="not valid json", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


def test_completed_result_command_exit_0_pass():
    c = {"id": "C1", "type": "command", "command": "echo x", "expected": "exit_0"}
    comp = SimpleNamespace(returncode=0, stdout="x", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_command_exit_0_fail():
    c = {"id": "C1", "type": "command", "command": "echo x", "expected": "exit_0"}
    comp = SimpleNamespace(returncode=1, stdout="x", stderr="e")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


def test_completed_result_command_exit_nonzero_pass():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "exit_nonzero"}
    comp = SimpleNamespace(returncode=1, stdout="", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_command_exit_nonzero_fail():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "exit_nonzero"}
    comp = SimpleNamespace(returncode=0, stdout="", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL" and "expected a nonzero" in r["reason"]


def test_completed_result_command_contains_pass():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "contains", "contains": "y"}
    comp = SimpleNamespace(returncode=0, stdout="y", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_command_contains_fail():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "contains", "contains": "y"}
    comp = SimpleNamespace(returncode=0, stdout="z", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


def test_completed_result_command_regex_pass():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "regex", "pattern": r"y.*"}
    comp = SimpleNamespace(returncode=0, stdout="yz", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_command_regex_fail():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "regex", "pattern": r"y.*"}
    comp = SimpleNamespace(returncode=0, stdout="zz", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


def test_completed_result_command_manual_review():
    c = {"id": "C1", "type": "command", "command": "x", "expected": "manual_review"}
    comp = SimpleNamespace(returncode=0, stdout="x", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "NEEDS_REVIEW"


def test_completed_result_non_regression_pass():
    c = {"id": "C1", "type": "non_regression", "command": "pytest"}
    comp = SimpleNamespace(returncode=0, stdout="x", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "PASS"


def test_completed_result_non_regression_fail():
    c = {"id": "C1", "type": "non_regression", "command": "pytest"}
    comp = SimpleNamespace(returncode=1, stdout="x", stderr="")
    r = runtime_ac._completed_result(c, comp, 0.1)
    assert r["status"] == "FAIL"


# _hollow_carry_condition (lines 1046, 1054-1057)


def test_hollow_carry_condition_with_nodes():
    assert (
        runtime_ac._hollow_carry_condition({"hollow_nodes": ["x"], "node_verdict": "PASS"}) is True
    )


def test_hollow_carry_condition_non_pass():
    assert runtime_ac._hollow_carry_condition({"node_verdict": "FAIL"}) is True


def test_hollow_carry_condition_clean():
    assert runtime_ac._hollow_carry_condition({"node_verdict": "PASS", "hollow_nodes": []}) is False


def test_hollow_carry_condition_empty():
    assert runtime_ac._hollow_carry_condition({}) is False


# _run_command_check (lines 1183-1184)


def test_run_command_check_timeout():
    c = {"id": "C1", "type": "command", "command": "python -c 'import time; time.sleep(2)'"}
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
        r = runtime_ac._run_command_check(c, cwd=runtime_ac.ORCH_DIR, timeout=1)
        assert r["status"] == "ERROR" and "timed out" in r["reason"]


def test_run_command_check_exception():
    c = {"id": "C1", "type": "command", "command": "x"}
    with mock.patch("subprocess.run", side_effect=Exception("err")):
        r = runtime_ac._run_command_check(c, cwd=runtime_ac.ORCH_DIR, timeout=30)
        assert r["status"] == "ERROR" and "err" in r["reason"]


# _planned_checks_for_run (line 1242)


def test_planned_checks_for_run_all():
    spec = runtime_ac._valid_spec()
    plan = runtime_ac.build_dry_run_plan(spec)
    checks = runtime_ac._planned_checks_for_run(plan, None)
    assert len(checks) > 0


def test_planned_checks_for_run_filtered():
    spec = runtime_ac._valid_spec()
    plan = runtime_ac.build_dry_run_plan(spec)
    # Find a non-runtime_context check
    check_id = None
    for c in plan["planned_checks"]:
        if c.get("type") != "runtime_context":
            check_id = c["id"]
            break
    assert check_id is not None, "No non-runtime_context check found"
    checks = runtime_ac._planned_checks_for_run(plan, {check_id})
    assert len(checks) == 1 and checks[0]["id"] == check_id


# run_verification (lines 1288, 1296)


def test_run_verification_requires_confirm():
    with pytest.raises(ValueError, match="--confirm-run is required"):
        runtime_ac.run_verification(runtime_ac._valid_spec(), confirm_run=False)


def test_run_verification_manual_check():
    spec = {
        "verification": {"id": "x", "title": "t", "goal": "g", "risk_level": "low"},
        "acceptance_criteria": [
            {
                "id": "AC1",
                "statement": "s",
                "evidence_required": ["manual_review"],
                "checks": [{"id": "AC1-M", "type": "manual", "name": "n", "instructions": "i"}],
            }
        ],
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": False,
            "fail_on_missing_checks": True,
        },
    }
    r = runtime_ac.run_verification(spec, confirm_run=True, timeout=30)
    assert r["check_results"][0]["status"] == "NEEDS_REVIEW"


def test_run_verification_command_gated():
    spec = {
        "verification": {"id": "v", "title": "t", "goal": "g", "risk_level": "low"},
        "acceptance_criteria": [
            {
                "id": "AC1",
                "statement": "s",
                "evidence_required": ["test_output"],
                "checks": [
                    {
                        "id": "c1",
                        "type": "command",
                        "name": "n",
                        "command": "pytest",
                        "expected": "exit_0",
                    }
                ],
            }
        ],
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": False,
            "fail_on_missing_checks": True,
            "min_pass_ratio": 1.0,
        },
    }
    r = runtime_ac.run_verification(spec, confirm_run=True, allow_command_checks=False, timeout=30)
    assert r["check_results"][0]["status"] == "SKIP"


# author_issue_spec (lines 1367, 1470, 1508, 1511)


def test_author_issue_spec_bad_issue():
    with pytest.raises(ValueError, match="must be owner/repo#number"):
        runtime_ac.author_issue_spec("bad", "body")


def test_author_issue_spec_empty_body():
    r = runtime_ac.author_issue_spec("owner/repo#1", "")
    assert len(r["acceptance_criteria"]) == 1


def test_author_issue_spec_with_target():
    r = runtime_ac.author_issue_spec("owner/repo#1", "## AC\n- x", target="t")
    assert r["verification"]["target"] == "t"


def test_author_issue_spec_with_worktree():
    r = runtime_ac.author_issue_spec("owner/repo#1", "## AC\n- x", worktree="/w")
    assert r["runtime_context"]["worktree"] == "/w"


# _author_cli (lines 1525-1527, 1537-1538)


def test_author_cli_with_body_file(monkeypatch, tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text("## Acceptance Criteria\n- Test")
    out_file = tmp_path / "spec.json"
    with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="{}")):
        r = runtime_ac._author_cli(
            ["--issue", "owner/repo#1", "--out", str(out_file), "--body-file", str(body_file)]
        )
        assert r == 0 and out_file.exists()


# _selftest (line 1696)


def test_selftest_callable():
    assert callable(runtime_ac._selftest)


# main (lines 1963-2039)


def test_main_validate_invalid(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{}")
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--validate", str(f)])
        assert r == 1


def test_main_validate_valid(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(runtime_ac._valid_spec()))
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--validate", str(f)])
        assert r == 0


def test_main_validate_json(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(runtime_ac._valid_spec()))
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--validate", str(f), "--json"])
        assert r == 0


def test_main_goal(monkeypatch):
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--goal", "Test"])
        assert r == 0


def test_main_plan(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(runtime_ac._valid_spec()))
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--plan", str(f)])
        assert r == 0


def test_main_run_no_confirm(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(runtime_ac._valid_spec()))
    with mock.patch(
        "sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})
    ), mock.patch("sys.stderr", new_callable=lambda: type("", (), {"write": lambda x: None})):
        runtime_ac.main(["--run", str(f)])
        # main catches the ValueError from run_verification and returns 2
        # But we can test run_verification directly
    with pytest.raises(ValueError, match="confirm-run"):
        runtime_ac.run_verification(runtime_ac._valid_spec(), confirm_run=False, timeout=30)


def test_main_results(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    rf = tmp_path / "r.json"
    spec = runtime_ac._valid_spec()
    f.write_text(json.dumps(spec))
    check_ids = runtime_ac._all_ac_check_ids(spec)
    rf.write_text(
        json.dumps({"check_results": [{"id": cid, "status": "PASS"} for cid in check_ids]})
    )
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--results", str(f), "--result-json", str(rf)])
        assert r == 0


def test_main_results_no_json(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(runtime_ac._valid_spec()))
    with mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        r = runtime_ac.main(["--results", str(f)])
        assert r == 2


def test_main_selftest(monkeypatch):
    with mock.patch.object(runtime_ac, "_selftest"), mock.patch(
        "sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})
    ):
        r = runtime_ac.main(["--selftest"])
        assert r == 0


def test_main_author(monkeypatch, tmp_path):
    out_file = tmp_path / "s.json"
    with mock.patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout='{"body": "## AC\\n- x"}', stderr=""),
    ), mock.patch("sys.stdout", new_callable=lambda: type("", (), {"write": lambda x: None})):
        assert runtime_ac.main(["author", "--issue", "owner/repo#1", "--out", str(out_file)]) == 0
