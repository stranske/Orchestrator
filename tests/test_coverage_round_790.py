"""Hermetic regression coverage for the ranked round-790 targets."""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest

import backlog
import pattern_miner
import roles


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep every target's runtime paths inside the test-owned temporary directory."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_pattern_miner_reads_json_and_jsonl_without_ambiguous_transport(tmp_path, monkeypatch):
    records = [{"event": 1}, {"event": 2}]
    json_path = tmp_path / "events.json"
    json_path.write_text(json.dumps(records), encoding="utf-8")
    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.write_text('{"event": 1}\n\n{"event": 2}\n', encoding="utf-8")

    assert pattern_miner._read_envelopes(json_path) == records
    assert pattern_miner._read_envelopes(jsonl_path) == records
    monkeypatch.setattr(pattern_miner.sys, "stdin", StringIO('{"event": 3}\n'))
    assert pattern_miner._read_envelopes(pattern_miner.Path("-")) == [{"event": 3}]

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text('{"event": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="JSON list"):
        pattern_miner._read_envelopes(invalid_path)


def test_backlog_strict_fetch_never_treats_failed_or_truncated_discovery_as_empty(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"number": 1}]), stderr="")

    monkeypatch.setattr(backlog.subprocess, "run", fake_run)
    assert backlog._gh_strict(["issue", "list", "-R", "owner/repo"]) == [{"number": 1}]
    assert calls[0][0][:2] == ["gh", "issue"]
    assert calls[0][1]["timeout"] == 60

    monkeypatch.setattr(
        backlog.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="not authorized"),
    )
    with pytest.raises(RuntimeError, match="not authorized"):
        backlog._gh_strict(["issue", "list"])

    monkeypatch.setattr(
        backlog,
        "_gh_strict",
        lambda args: [{"number": number} for number in range(1000)],
    )
    with pytest.raises(RuntimeError, match="ownership is incomplete"):
        backlog.strict_fetch_issues("owner/repo")


def test_prompt_and_triage_validators_reject_incomplete_or_misaddressed_advice():
    valid_prompt = {
        "task_type": "implement",
        "summary": "Bounded change",
        "scoped_prompt": "Meet acceptance, validate, commit, push, and open a PR.",
        "definition_of_done": ["Target behavior is verified"],
        "acceptance_criteria": ["Focused test passes"],
        "validation": ["pytest -q"],
        "expected_paths": ["src/target.py"],
        "out_of_scope": ["No refactor"],
        "risk_flags": [],
        "confidence": "high",
    }
    assert roles._validate_prompt_agent(valid_prompt) == []
    invalid_prompt = {**valid_prompt, "scoped_prompt": "You are Codex; validate only."}
    errors = roles._validate_prompt_agent(invalid_prompt)
    assert "scoped_prompt must not include agent persona text; dispatcher adds that" in errors
    assert "scoped_prompt must mention 'commit'" in errors

    items = [
        {"target": "owner/repo#1", "labels": "priority:high", "body": "x" * 1500},
        {"target": "", "title": "ignored"},
    ]
    compact = roles._compact_backlog_items(items, max_items=1)
    assert compact == [
        {
            "target": "owner/repo#1",
            "task_type": "implement",
            "lane": "",
            "title": "",
            "labels": ["priority:high"],
            "body": "x" * 1400 + "...",
        }
    ]


def test_adjudicator_validator_rejects_mutating_step_after_an_inspection_clause():
    proposal = {
        "decision": "needs_more_evidence",
        "confidence": "medium",
        "rationale": "The supplied evidence does not resolve the disputed claim.",
        "evidence_assessment": [
            {
                "claim": "The gate is incomplete",
                "status": "insufficient",
                "evidence_ref": None,
                "reason": "Only metadata was supplied.",
            }
        ],
        "ground_truth_refs": [],
        "recommended_next_step": "Inspect the named gate output before deciding.",
        "evidence_gaps": ["The gate output itself is missing."],
    }
    assert roles._validate_adjudicator(proposal) == []

    errors = roles._validate_adjudicator(
        {**proposal, "recommended_next_step": "Inspect the output; then merge the PR."}
    )
    assert "recommended_next_step must not contain mutating execution instructions" in errors
