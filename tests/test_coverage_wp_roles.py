"""Hermetic coverage for the roles.py statements that no selftest executes (work package roles).

roles.py's own selftest drives the happy paths. This file pins what it never reaches: the
validators' rejection messages, the generated-role contract checks, the router-facing guards, the
dispatch seam that actually invokes a role, the backend-failure branches of every runner, the
human renderers and the command line. Every backend call, Brain write and capacity read is
stubbed, so nothing here starts a process, opens a connection or touches ~/.codex.
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import sys
import tempfile
from pathlib import Path

import pytest

import dispatcher
import epic_lane
import feedback
import redirect_shadow
import roles
import router


class Spy:
    """Records every call; returns a fixed result or raises a fixed error."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def _refuse_capacity_read():
    raise AssertionError("this test must pass cap= or stub router.load_capacity itself")


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Every target reads and writes only the test's disposable runtime."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    # The router's model-health check would otherwise run the agent CLIs' catalog probes.
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    for name in (
        "ORCH_CAPABILITY_HEARTBEATS",
        "ORCH_ROLE_SHADOW",
        "ORCH_ROLE_MAX_PER_CYCLE",
        "ORCH_PROMPT_ROLE_MAX_PER_CYCLE",
        "ORCH_DECOMPOSER_ROLE_MAX_PER_CYCLE",
        "ORCH_TRIAGE_ROLE_MAX_PER_CYCLE",
        "ORCH_ADJUDICATOR_ROLE_MAX_PER_CYCLE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "brain.db")
    # route_role reads learned weights from the Brain; an empty Brain answers [] without opening it.
    monkeypatch.setattr(feedback, "current_weights", lambda *args, **kwargs: [])
    monkeypatch.setattr(router, "load_capacity", _refuse_capacity_read)
    roles.reset_role_invocation_counts()
    yield
    roles.reset_role_invocation_counts()


CAP = {"agents": {name: {"state": "ok"} for name in ("cursor", "codex", "gemini", "vibe")}}

ISSUE_BODY = (
    "## Why\nAuthor a batch.\n\n## Tasks\n- [ ] Add the bounded change.\n\n"
    "## Acceptance Criteria\n- python3 -m pytest tests/test_role_prompt_offering.py -q "
    "passes; capture stdout in the PR.\n\n## Non-Goals\nNo dispatch gate changes.\n"
)

SCOPED_PROMPT = (
    "Implement owner/repo#1 only. Meet the acceptance criteria, validate with pytest, then "
    "commit, push, and open or update the PR with evidence."
)


def _valid_prompt_proposal():
    return {
        "task_type": "implement",
        "summary": "Implement the bounded fix.",
        "scoped_prompt": SCOPED_PROMPT,
        "definition_of_done": ["The bounded behavior works"],
        "acceptance_criteria": ["Focused test passes"],
        "validation": ["pytest -q"],
        "expected_paths": ["src/example.py"],
        "out_of_scope": ["No unrelated refactor"],
        "risk_flags": ["Preserve compatibility"],
        "confidence": "high",
    }


def _rec(target, batch_id=None, action="work_now", priority=1):
    return {
        "target": target,
        "action": action,
        "priority": priority,
        "reason": f"Reason for {target}.",
        "batch_id": batch_id,
    }


def _valid_triage(*targets):
    return {
        "summary": "Triage summary.",
        "recommendations": [_rec(target) for target in targets],
        "batches": [],
        "global_risks": [],
        "confidence": "high",
    }


def _valid_adjudication():
    return {
        "decision": "reject_blocker",
        "confidence": "high",
        "rationale": "Persisted runtime evidence contradicts the review claim.",
        "evidence_assessment": [
            {
                "claim": "runtime evidence is missing",
                "status": "contradicted",
                "evidence_ref": "runtime-ac:1",
                "reason": "The persisted check passed.",
            }
        ],
        "ground_truth_refs": ["runtime-ac:1"],
        "recommended_next_step": "Inspect runtime-ac:1 and the review note.",
        "evidence_gaps": [],
    }


def _manifest(**overrides):
    """A hand-built generated-role manifest carrying only what roles.py reads."""
    manifest = {
        "name": "gen-role",
        "description": "A generated shadow role.",
        "route_as": "review",
        "authority": "Advisory only.",
        "input_schema": {
            "kind": {"type": "string", "required": True, "enum": ["review"], "max_length": 16},
            "refs": {"type": "string_list", "max_items": 2},
        },
        "output_schema": {
            "decision": {"type": "string", "required": True, "enum": ["inspect", "defer"]},
            "rationale": {"type": "string", "required": True, "max_length": 40},
        },
        "selector": {"field": "kind", "operator": "equals", "value": "review"},
        "capacity_policy": {"max_invocations_per_cycle": 1},
        "prompt_hash": "sha256:aaa",
        "prompt_protocol": {
            "purpose": "Purpose line.",
            "instructions": ["Do A.", "Do B."],
            "max_context_chars": 60,
        },
        "lifecycle": {
            "expires_at": 4102444800,
            "kill_switch": {"env": "GEN_ROLE_OFF", "disabled_value": "1"},
            "rollback": {"predecessor": "role-adjudicator"},
            "predecessor": "role-adjudicator",
        },
        "capability_id": "role-gen-role",
        "manifest_id": "manifest-1",
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def generated_registry():
    roles.unregister_generated_role("gen-role")
    yield roles.GENERATED_ROLE_REGISTRY
    roles.unregister_generated_role("gen-role")
    roles.reset_role_invocation_counts()


# --------------------------------------------------------------------------------------
# Validators and small pure helpers
# --------------------------------------------------------------------------------------
def test_role_capability_event_forwards_only_when_heartbeats_are_enabled(monkeypatch):
    beats = Spy()
    monkeypatch.setattr(roles.capabilities, "heartbeat", beats)
    roles._role_capability_event("adjudicator", "success", ref="run-1", metadata={"k": 1})
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "0")
    roles._role_capability_event("adjudicator", "success", ref="run-1", metadata={"k": 1})
    assert beats.calls == []
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    roles._role_capability_event("adjudicator", "success", ref="run-1", metadata={"k": 1})
    roles._role_capability_event("triage", "match")
    assert beats.calls == [
        (("role-adjudicator", "success"), {"ref": "run-1", "metadata": {"k": 1}}),
        (("role-triage", "match"), {"ref": None, "metadata": None}),
    ]


@pytest.mark.parametrize("proposal", [[], None, "wait", 7])
def test_validate_redirect_rejects_anything_but_an_object(proposal):
    assert roles._validate_redirect(proposal) == ["proposal is not a JSON object"]


def test_validate_redirect_names_each_bad_field_and_accepts_the_clean_proposal():
    clean = {
        "action": "wait",
        "reason": "Lane is healthy.",
        "confidence": "high",
        "switch_agent": None,
    }
    assert roles._validate_redirect(clean) == []
    for reason in ("   ", 5):
        assert roles._validate_redirect({**clean, "reason": reason}) == [
            "reason must be a non-empty string"
        ]
    assert roles._validate_redirect({**clean, "confidence": "certain"}) == [
        "confidence must be one of ['high', 'low', 'medium']; got 'certain'"
    ]
    for switch in (5, "  "):
        assert roles._validate_redirect({**clean, "switch_agent": switch}) == [
            "switch_agent must be a string or null"
        ]
    assert roles._validate_redirect({**clean, "switch_agent": "codex"}) == []
    everything_wrong = {"action": "redirect", "reason": "", "confidence": None, "switch_agent": 3}
    assert roles._validate_redirect(everything_wrong) == [
        "reason must be a non-empty string",
        "confidence must be one of ['high', 'low', 'medium']; got None",
        "redirect requires a non-empty corrected_prompt",
        "switch_agent must be a string or null",
    ]


def test_as_lines_normalises_none_scalars_and_lists():
    assert roles._as_lines(None) == []
    assert roles._as_lines(["  a ", "", "   ", 5]) == ["a", "5"]
    assert roles._as_lines("  one line  ") == ["one line"]
    assert roles._as_lines("   ") == []
    assert roles._as_lines("") == []
    assert roles._as_lines(7) == ["7"]


def test_issue_body_validator_rejects_non_objects_and_blank_summaries():
    good = {"summary": "Author a batch.", "confidence": "high", "issue_body": ISSUE_BODY}
    assert roles._validate_issue_body(good) == []
    assert roles._validate_issue_body(["not", "an", "object"]) == ["proposal is not a JSON object"]
    for summary in ("   ", 4):
        assert roles._validate_issue_body({**good, "summary": summary}) == [
            "summary must be a non-empty string"
        ]
    assert roles._validate_issue_body({**good, "confidence": "certain"}) == [
        "confidence must be low, medium or high"
    ]


def test_prompt_validator_rejects_non_objects_and_repo_playbook_text():
    good = _valid_prompt_proposal()
    assert roles._validate_prompt_agent(good) == []
    assert roles._validate_prompt_agent("text") == ["proposal is not a JSON object"]
    leaked = {**good, "scoped_prompt": SCOPED_PROMPT + " Repo playbook (approved): never skip."}
    assert roles._validate_prompt_agent(leaked) == [
        "scoped_prompt must not include repo playbook text; dispatcher injects approved context"
    ]


def test_decomposer_validator_rejects_non_objects_and_defers_to_the_epic_schema():
    plan = epic_lane._valid_plan()
    assert roles._validate_decomposer(plan) == []
    assert roles._validate_decomposer(None) == ["proposal is not a JSON object"]
    broken = {**plan, "subtasks": []}
    errors = roles._validate_decomposer(broken)
    assert errors and errors == epic_lane.validate_plan(broken)


def test_triage_validator_rejects_non_objects_and_accepts_a_clean_proposal():
    assert roles._validate_triage_agent([]) == ["proposal is not a JSON object"]
    clean = _valid_triage("o/r#1", "o/r#2")
    clean["recommendations"][0]["batch_id"] = "B1"
    clean["batches"] = [{"id": "B1", "targets": ["o/r#1"], "reason": "Grouped.", "risk": "low"}]
    assert roles._validate_triage_agent(clean) == []


def test_triage_validator_names_every_malformed_field_in_order():
    proposal = {
        "summary": "ok",
        "confidence": "high",
        "global_risks": [],
        "stray": 1,
        "recommendations": [
            "not-an-object",
            {
                "target": " ",
                "action": "teleport",
                "priority": 9,
                "reason": "",
                "batch_id": "  ",
                "bogus": 1,
            },
        ],
        "batches": [
            3,
            {"id": "", "targets": [], "reason": " ", "risk": "extreme", "zzz": 1},
        ],
    }
    assert roles._validate_triage_agent(proposal) == [
        "unexpected top-level keys: ['stray']",
        "recommendations[0] is not an object",
        "recommendations[1] has unexpected keys: ['bogus']",
        "recommendations[1].target must be a non-empty string",
        "recommendations[1].action must be one of "
        "['defer', 'monitor', 'needs_scope', 'skip', 'work_now']",
        "recommendations[1].priority must be an integer from 1 to 5",
        "recommendations[1].reason must be a non-empty string",
        "recommendations[1].batch_id must be a string or null",
        "batches[0] is not an object",
        "batches[1] has unexpected keys: ['zzz']",
        "batches[1].id must be a non-empty string",
        "batches[1].targets must be a non-empty list of target strings",
        "batches[1].reason must be a non-empty string",
        "batches[1].risk must be one of ['high', 'low', 'medium']",
    ]


@pytest.mark.parametrize(
    ("priority", "valid"),
    [(0, False), (1, True), (5, True), (6, False), ("2", False), (2.0, False), (None, False)],
)
def test_triage_priority_is_an_integer_from_one_to_five(priority, valid):
    proposal = _valid_triage("o/r#1")
    proposal["recommendations"][0]["priority"] = priority
    expected = [] if valid else ["recommendations[0].priority must be an integer from 1 to 5"]
    assert roles._validate_triage_agent(proposal) == expected


@pytest.mark.xfail(
    strict=True,
    reason="_validate_triage_agent accepts JSON true as a priority because bool is an int "
    "subclass, although it demands an integer from 1 to 5 (the generated-role validator "
    "excludes bool explicitly)",
)
def test_triage_priority_rejects_a_boolean():
    proposal = _valid_triage("o/r#1")
    proposal["recommendations"][0]["priority"] = True
    assert roles._validate_triage_agent(proposal) == [
        "recommendations[0].priority must be an integer from 1 to 5"
    ]


def test_triage_context_rejects_duplicate_unknown_and_missing_targets():
    items = [{"target": "o/r#1"}, {"target": "o/r#2"}, {"target": "o/r#3"}]
    proposal = _valid_triage("o/r#1", "o/r#2", "o/r#3")
    assert roles._validate_triage_context(proposal, items) == []
    skewed = _valid_triage("o/r#1", "o/r#1", "o/r#9")
    assert roles._validate_triage_context(skewed, items) == [
        "duplicate recommendations for targets: ['o/r#1']",
        "recommendations reference unknown targets: ['o/r#9']",
        "missing recommendations for input targets: ['o/r#2', 'o/r#3']",
    ]


def test_triage_context_checks_batch_wiring_and_ignores_malformed_entries():
    items = [{"target": "o/r#1"}, {"target": "o/r#2"}]
    proposal = {
        "recommendations": [_rec("o/r#1", "B1"), "junk-recommendation", _rec("o/r#2", "B9")],
        "batches": [
            {"id": "B1", "targets": ["o/r#1", "o/r#7", "o/r#2"]},
            "junk-batch",
        ],
    }
    assert roles._validate_triage_context(proposal, items) == [
        "o/r#2 references unknown batch_id 'B9'",
        "batch 'B1' references unknown target 'o/r#7'",
        "batch 'B1' includes 'o/r#7' but its recommendation does not use that batch_id",
        "batch 'B1' includes 'o/r#2' but its recommendation does not use that batch_id",
    ]
    wired = {
        "recommendations": [_rec("o/r#1", "B1"), _rec("o/r#2", "B1")],
        "batches": [{"id": "B1", "targets": ["o/r#1", "o/r#2"]}],
    }
    assert roles._validate_triage_context(wired, items) == []


def test_baseline_triage_keeps_epics_and_closer_prs_visible_with_their_own_reasons():
    epic_reason = "Deterministic baseline keeps this visible for epic decomposition."
    closer_reason = "Deterministic baseline keeps closer PRs visible for inspection."
    plain_reason = "Deterministic baseline preserves backlog order."
    items = [
        {"target": "o/r#1", "task_type": "epic", "lane": "opener"},
        {"target": "o/r#2", "lane": "closer"},
        {"target": "o/r#3"},
        {"target": "o/r#4", "task_type": "epic", "lane": "closer"},
        {"target": "o/r#5"},
        {"target": "o/r#6"},
    ]
    baseline = roles._baseline_triage(items)
    assert baseline["summary"] == "Deterministic baseline preserves visible backlog order."
    assert baseline["confidence"] == "medium"
    assert [
        (r["target"], r["action"], r["priority"], r["reason"], r["batch_id"])
        for r in baseline["recommendations"]
    ] == [
        ("o/r#1", "work_now", 1, epic_reason, None),
        ("o/r#2", "monitor", 2, closer_reason, None),
        ("o/r#3", "work_now", 3, plain_reason, None),
        ("o/r#4", "monitor", 4, epic_reason, None),
        ("o/r#5", "work_now", 5, plain_reason, None),
        ("o/r#6", "work_now", 5, plain_reason, None),
    ]
    assert baseline["batches"] == [] and baseline["global_risks"] == []
    assert roles._baseline_triage([]) == {
        "summary": "Deterministic baseline preserves visible backlog order.",
        "recommendations": [],
        "batches": [],
        "global_risks": [],
        "confidence": "high",
    }


def test_compact_adjudication_case_trims_truncates_and_drops_unknown_keys():
    assert roles._compact_adjudication_case([]) == {}
    assert roles._compact_adjudication_case(None) == {}
    case = {
        "case_type": "  review  ",
        "target": "o/r#1",
        "disputed_finding": "x" * 2200,
        "blocker": "y" * 2201 + "   ",
        "acceptance_criteria": "z" * 2199 + " tail",
        "ground_truth_evidence": {"nested": ["kept as is"]},
        "metadata_only": True,
        "unlisted": "dropped",
    }
    assert roles._compact_adjudication_case(case) == {
        "case_type": "review",
        "target": "o/r#1",
        "disputed_finding": "x" * 2200,
        "blocker": "y" * 2200 + "...",
        "acceptance_criteria": "z" * 2199 + "...",
        "ground_truth_evidence": {"nested": ["kept as is"]},
        "metadata_only": True,
    }


@pytest.mark.parametrize(
    ("text", "imperative"),
    [
        ("", False),
        (" \n ", False),
        ("Inspect the cited gate output before deciding.", False),
        ("Merge the PR", True),
        ("Inspect the gate output; then merge the PR", True),
    ],
)
def test_imperative_next_step_check_treats_blank_text_as_not_imperative(text, imperative):
    assert roles._recommended_next_step_is_imperative(text) is imperative


def test_adjudicator_validator_rejects_non_objects_and_blank_text_fields():
    assert roles._validate_adjudicator([]) == ["proposal is not a JSON object"]
    base = roles._baseline_adjudication({})
    assert roles._validate_adjudicator(base) == []
    assert roles._validate_adjudicator({**base, "rationale": "  "}) == [
        "rationale must be a non-empty string"
    ]
    assert roles._validate_adjudicator({**base, "recommended_next_step": " "}) == [
        "recommended_next_step must be a non-empty string"
    ]
    for refs in (["ok", " "], "ref"):
        assert roles._validate_adjudicator({**base, "ground_truth_refs": refs}) == [
            "ground_truth_refs must be a list of non-empty strings"
        ]
    for gaps in ([""], "text"):
        assert roles._validate_adjudicator({**base, "evidence_gaps": gaps}) == [
            "evidence_gaps must be a list of non-empty strings"
        ]


def test_adjudicator_validator_requires_refs_or_gaps_for_the_decision_taken():
    base = roles._baseline_adjudication({})
    for decision in ("uphold_blocker", "reject_blocker"):
        assert roles._validate_adjudicator({**base, "decision": decision}) == [
            f"{decision} requires at least one ground_truth_ref"
        ]
        assert (
            roles._validate_adjudicator({**base, "decision": decision, "ground_truth_refs": ["g1"]})
            == []
        )
    assert roles._validate_adjudicator({**base, "evidence_gaps": []}) == [
        "needs_more_evidence requires at least one evidence_gap"
    ]
    upheld = {**base, "decision": "uphold_blocker", "ground_truth_refs": ["g1"]}
    assert roles._validate_adjudicator({**upheld, "evidence_gaps": []}) == []


def test_adjudicator_validator_checks_the_evidence_assessment_list_and_each_entry():
    base = roles._baseline_adjudication({})
    for assessments in ([], "text"):
        assert roles._validate_adjudicator({**base, "evidence_assessment": assessments}) == [
            "evidence_assessment must be a non-empty list"
        ]
    entry = {"claim": "c", "status": "supported", "evidence_ref": "ref", "reason": "r"}
    assert roles._validate_adjudicator({**base, "evidence_assessment": [entry]}) == []
    broken = {"claim": "", "status": "maybe", "evidence_ref": "  ", "reason": "", "extra": 1}
    assert roles._validate_adjudicator({**base, "evidence_assessment": ["bad", broken]}) == [
        "evidence_assessment[0] is not an object",
        "evidence_assessment[1] has unexpected keys: ['extra']",
        "evidence_assessment[1].claim must be a non-empty string",
        "evidence_assessment[1].status must be supported|contradicted|insufficient",
        "evidence_assessment[1].evidence_ref must be a string or null",
        "evidence_assessment[1].reason must be a non-empty string",
    ]


def test_adjudication_case_validator_names_the_case_type_and_missing_fields():
    assert roles._validate_adjudication_case([]) == ["case must be a JSON object"]
    full = {
        "case_type": "review",
        "target": "o/r#1",
        "disputed_finding": "f",
        "ground_truth_evidence": {"k": 1},
    }
    assert roles._validate_adjudication_case(full) == []
    assert roles._validate_adjudication_case({**full, "case_type": ""}) == []
    assert roles._validate_adjudication_case({**full, "case_type": "weird"}) == [
        "case_type must be one of ['adversarial', 'review', 'runtime_ac']"
    ]
    assert roles._validate_adjudication_case({}) == [
        "case.target is required",
        "case.disputed_finding is required",
        "case.ground_truth_evidence is required",
    ]


# --------------------------------------------------------------------------------------
# Generated roles
# --------------------------------------------------------------------------------------
GEN_SCHEMA = {
    "name": {"type": "string", "required": True, "max_length": 8},
    "count": {"type": "integer"},
    "flag": {"type": "boolean"},
    "tags": {"type": "string_list", "max_items": 2},
    "mode": {"type": "string", "enum": ["a", "b"]},
    "note": {"type": "string"},
}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], ["input must be an object"]),
        ({"name": "ok"}, []),
        ({"name": "ok", "zeta": 1, "alpha": 2}, ["input has unsupported keys: ['alpha', 'zeta']"]),
        ({}, ["input is missing required key: name"]),
        ({"name": "ok", "count": 0, "flag": False, "tags": [], "mode": "a"}, []),
        (
            {"name": 5, "count": True, "flag": 1, "tags": "x", "mode": 3},
            [
                "input.name must be string",
                "input.count must be integer",
                "input.flag must be boolean",
                "input.tags must be string_list",
                "input.mode must be string",
            ],
        ),
        ({"name": "   "}, ["input.name is empty or exceeds 8 characters"]),
        ({"name": "x" * 9}, ["input.name is empty or exceeds 8 characters"]),
        ({"name": "x" * 8}, []),
        ({"name": "ok", "mode": "c"}, ["input.mode is outside its enum"]),
        ({"name": "ok", "tags": ["a", "b", "c"]}, ["input.tags exceeds 2 items"]),
        ({"name": "ok", "tags": ["", "b"]}, ["input.tags contains an empty or oversized item"]),
        ({"name": "ok", "tags": ["x" * 513]}, ["input.tags contains an empty or oversized item"]),
        ({"name": "ok", "tags": ["x" * 512]}, []),
        (
            {"name": "ok", "tags": ["password=hunter22"]},
            ["input.tags contains secret-bearing text"],
        ),
        ({"name": "ok", "note": "the token is stored elsewhere"}, []),
        ({"name": "ok", "note": "password: hunter22"}, ["input.note contains secret-bearing text"]),
        ({"name": "ok", "note": "ghp_abcdefgh1"}, ["input.note contains secret-bearing text"]),
        ({"name": "ok", "note": "Bearer abcdefgh1"}, ["input.note contains secret-bearing text"]),
        ({"name": "ok", "note": "x" * 4097}, ["input.note is empty or exceeds 4096 characters"]),
    ],
)
def test_generated_payload_validator_enforces_the_declared_schema(payload, expected):
    assert roles._validate_generated_payload(payload, GEN_SCHEMA, label="input") == expected


def test_generated_payload_validator_defaults_list_length_and_rejects_unknown_types():
    schema = {"items": {"type": "string_list"}, "ratio": {"type": "float"}}
    assert roles._validate_generated_payload({"items": ["a"] * 12}, schema, label="output") == []
    assert roles._validate_generated_payload({"items": ["a"] * 13}, schema, label="output") == [
        "output.items exceeds 12 items"
    ]
    assert roles._validate_generated_payload({"ratio": 1.5}, schema, label="output") == [
        "output.ratio must be float"
    ]


def test_generated_role_prompt_checks_the_input_and_bounds_the_context():
    manifest = _manifest()
    ctx = {"kind": "review", "refs": ["a"]}
    context_json = '{"kind":"review","refs":["a"]}'
    assert roles._generated_role_prompt(manifest, ctx) == "\n".join(
        [
            "Purpose line.",
            "Advisory only.",
            "Do A.",
            "Do B.",
            "Return exactly one JSON object matching the declared output keys: decision, rationale",
            "Input JSON:",
            context_json,
        ]
    )
    with pytest.raises(ValueError, match=r"^input is missing required key: kind$"):
        roles._generated_role_prompt(manifest, {})
    with pytest.raises(ValueError, match=r"^input\.kind must be string; input\.refs must be "):
        roles._generated_role_prompt(manifest, {"kind": 1, "refs": "x"})
    exact = _manifest(
        prompt_protocol={**manifest["prompt_protocol"], "max_context_chars": len(context_json)}
    )
    assert roles._generated_role_prompt(exact, ctx).endswith(context_json)
    tight = _manifest(
        prompt_protocol={
            **manifest["prompt_protocol"],
            "max_context_chars": len(context_json) - 1,
        }
    )
    with pytest.raises(ValueError, match=r"^generated role context exceeds prompt protocol bound$"):
        roles._generated_role_prompt(tight, ctx)


def test_generated_role_needs_a_route_prior_the_router_knows():
    with pytest.raises(ValueError, match=r"^generated role has unsupported route prior$"):
        roles.role_from_generated_manifest(_manifest(route_as="no-such-task"))
    role = roles.role_from_generated_manifest(_manifest(route_as="review"))
    assert role.eligible_backends == frozenset(
        entry["agent"] for entry in router.ROUTE_TABLE["review"]["agents"]
    )
    assert role.generated is True and role.mode is None
    assert role.input_keys == ("kind", "refs") and role.output_keys == ("decision", "rationale")


def test_register_generated_role_refuses_nameless_static_and_conflicting_contracts(
    generated_registry,
):
    nameless = _manifest()
    del nameless["name"]
    for manifest in (_manifest(name=""), nameless):
        with pytest.raises(ValueError, match=r"^generated role name is required$"):
            roles.register_generated_role(manifest)
    with pytest.raises(ValueError, match=r"^generated role duplicates static role: redirect$"):
        roles.register_generated_role(_manifest(name="redirect"))
    assert "redirect" not in generated_registry
    first = roles.register_generated_role(_manifest())
    assert generated_registry["gen-role"] is first
    assert roles._ROLE_INVOCATION_COUNTS["gen-role"] == 0
    same = roles.register_generated_role(_manifest())
    assert generated_registry["gen-role"] is same
    message = r"^generated role already registered with different contract: gen-role$"
    with pytest.raises(ValueError, match=message):
        roles.register_generated_role(_manifest(prompt_hash="sha256:other"))
    assert generated_registry["gen-role"] is same


def test_get_role_resolves_static_then_generated_and_names_unknown_roles(generated_registry):
    assert roles.get_role("redirect") is roles.ROLE_REGISTRY["redirect"]
    with pytest.raises(ValueError, match=r"^unknown role 'gen-role'$"):
        roles.get_role("gen-role")
    registered = roles.register_generated_role(_manifest())
    assert roles.get_role("gen-role") is registered
    roles.unregister_generated_role("gen-role")
    with pytest.raises(ValueError, match=r"^unknown role 'gen-role'$"):
        roles.get_role("gen-role")


def _role_with_selector(selector):
    return roles.Role(
        name="sel",
        description="d",
        route_as="review",
        eligible_backends=frozenset(),
        mode=None,
        input_keys=(),
        output_keys=(),
        build_prompt=lambda ctx: "",
        validate=lambda proposal: [],
        selector=selector,
    )


def test_generated_selector_supports_equals_and_in_and_refuses_unknown_operators():
    equals = _role_with_selector({"field": "kind", "operator": "equals", "value": "review"})
    assert roles._generated_selector_matches(equals, {"kind": "review"}) is True
    assert roles._generated_selector_matches(equals, {"kind": "implement"}) is False
    member = _role_with_selector({"field": "kind", "operator": "in", "value": ["a", "review"]})
    assert roles._generated_selector_matches(member, {"kind": "review"}) is True
    assert roles._generated_selector_matches(member, {"kind": "other"}) is False
    assert roles._generated_selector_matches(member, {}) is False
    empty = _role_with_selector({"field": "kind", "operator": "in", "value": None})
    assert roles._generated_selector_matches(empty, {"kind": "review"}) is False
    unknown = _role_with_selector({"field": "kind", "operator": "contains", "value": "review"})
    assert roles._generated_selector_matches(unknown, {"kind": "review"}) is False
    assert roles._generated_selector_matches(_role_with_selector(None), {"kind": "x"}) is False


def test_generated_role_capability_registration_is_idempotent_per_manifest(monkeypatch, tmp_path):
    ledger = tmp_path / "capabilities.json"
    register = Spy()
    monkeypatch.setattr(roles.capabilities, "register", register)
    load = Spy(result={})
    monkeypatch.setattr(roles.capabilities, "load", load)
    roles._register_generated_role_capability(_manifest(), ledger)
    assert load.calls == []
    assert len(register.calls) == 1
    (capability_id, record, path), _ = register.calls[0]
    assert (capability_id, path) == ("role-gen-role", ledger)
    assert record["status"] == "shadow"
    assert record["activation_evidence"] == {"role_manifest_hash": "manifest-1"}

    register.calls.clear()
    ledger.write_text("{}")
    load.result = {"role-gen-role": {"activation_evidence": {"role_manifest_hash": "manifest-1"}}}
    assert roles._register_generated_role_capability(_manifest(), ledger) is None
    assert register.calls == [] and load.calls == [((ledger,), {"create": False})]
    message = r"^capability already registered with a different generated role$"
    with pytest.raises(ValueError, match=message):
        roles._register_generated_role_capability(_manifest(manifest_id="manifest-2"), ledger)
    load.result = {"role-gen-role": {}}
    with pytest.raises(ValueError, match=message):
        roles._register_generated_role_capability(_manifest(), ledger)
    assert register.calls == []


# --------------------------------------------------------------------------------------
# Routing and selectors
# --------------------------------------------------------------------------------------
def test_route_role_uses_the_route_prior_when_the_learned_weight_read_fails(monkeypatch):
    weights = Spy(error=RuntimeError("brain unavailable"))
    monkeypatch.setattr(feedback, "current_weights", weights)
    gemini_first = {"review": {"gemini": {"rank": 0}, "cursor": {"rank": 1}, "codex": {"rank": 2}}}
    pick = roles.route_role("redirect", cap=CAP, learned=gemini_first, exploration_rate=0.0)
    assert weights.calls == [(("role:redirect",), {})]
    assert pick["agent"] == "gemini"
    codex_first = {"review": {"codex": {"rank": 0}, "cursor": {"rank": 1}, "gemini": {"rank": 2}}}
    pick = roles.route_role("redirect", cap=CAP, learned=codex_first, exploration_rate=0.0)
    assert pick["agent"] == "codex"


def test_route_role_only_restricts_the_eligible_set_and_never_injects_a_backend(monkeypatch):
    real_select = router.select_agent
    select = Spy(error=AssertionError("an empty candidate set must not reach the router"))
    monkeypatch.setattr(router, "select_agent", select)
    disjoint = roles.route_role(
        "redirect", cap=CAP, learned={}, exploration_rate=0.0, only={"vibe"}
    )
    assert disjoint is None and select.calls == []
    monkeypatch.setattr(router, "select_agent", real_select)
    for agent in ("codex", "gemini"):
        narrowed = roles.route_role(
            "redirect", cap=CAP, learned={}, exploration_rate=0.0, only={agent}
        )
        assert narrowed["agent"] == agent


def test_selector_decision_survives_a_failed_brain_write(monkeypatch):
    recorder = Spy(error=RuntimeError("brain locked"))
    monkeypatch.setattr(feedback, "record_role_selector_event", recorder)
    decision = roles.select_role_activation(
        "triage", matched=True, gate_enabled=True, capacity_available=True, target="backlog:3"
    )
    assert decision["selector_status"] == "invoked" and decision["invoked"] is True
    assert decision["record_error"] == "brain locked" and decision["invocation_ordinal"] == 1
    assert recorder.calls == [
        (
            ("triage", "invoked"),
            {
                "reason": "matched",
                "target": "backlog:3",
                "matched": True,
                "invoked": True,
                "disagreement": False,
            },
        )
    ]
    quiet = roles.select_role_activation(
        "prompt", matched=True, gate_enabled=True, capacity_available=True, record=False
    )
    assert quiet["invoked"] is True and "record_error" not in quiet
    assert len(recorder.calls) == 1


def test_role_cap_prefers_the_role_specific_setting_and_defaults_to_one_when_unparseable():
    assert roles._role_cap({}, "prompt") == 1
    assert roles._role_cap({"ORCH_ROLE_MAX_PER_CYCLE": "4"}, "prompt") == 4
    both = {"ORCH_ROLE_MAX_PER_CYCLE": "4", "ORCH_PROMPT_ROLE_MAX_PER_CYCLE": "2"}
    assert roles._role_cap(both, "prompt") == 2
    assert roles._role_cap({"ORCH_ROLE_MAX_PER_CYCLE": "-3"}, "prompt") == 0
    assert roles._role_cap({"ORCH_ROLE_MAX_PER_CYCLE": "many"}, "prompt") == 1
    assert roles._role_cap({**both, "ORCH_PROMPT_ROLE_MAX_PER_CYCLE": "x"}, "prompt") == 1
    assert roles._role_cap({"ORCH_ROLE_MAX_PER_CYCLE": None}, "prompt") == 1
    assert roles._role_cap({"ORCH_PROMPT_ROLE_MAX_PER_CYCLE": "3"}, "triage") == 1


# --------------------------------------------------------------------------------------
# Backend output parsing
# --------------------------------------------------------------------------------------
def test_event_stream_message_texts_collects_agent_messages_from_both_event_shapes():
    stream = "\n".join(
        [
            "",
            "   ",
            "not json at all",
            "[1, 2]",
            '"a bare string"',
            json.dumps({"item": {"type": "agent_message", "text": "   "}}),
            json.dumps({"item": {"type": "agent_message", "text": "first"}}),
            json.dumps({"item": {"type": "tool_call", "text": "ignored"}}),
            json.dumps({"item": "not-a-dict"}),
            json.dumps({"type": "agent_message", "text": "second"}),
            json.dumps({"type": "agent_message", "text": "   "}),
            json.dumps({"type": "agent_message", "text": 5}),
            json.dumps({"type": "other", "text": "ignored too"}),
            json.dumps({"item": {"type": "agent_message", "text": ""}}),
        ]
    )
    assert roles._event_stream_message_texts(stream) == ["first", "second"]
    assert roles._event_stream_message_texts("") == []


def test_parse_json_reads_raw_fenced_streamed_and_embedded_json_and_gives_up_on_garbage():
    assert roles._parse_json(None) is None
    assert roles._parse_json("") is None
    assert roles._parse_json(' {"a": 1} ') == {"a": 1}
    assert roles._parse_json('```json\n{"b": 2}\n```') == {"b": 2}
    stream = "\n".join(
        [
            json.dumps({"type": "agent_message", "text": '{"early": true}'}),
            json.dumps({"type": "agent_message", "text": '{"late": true}'}),
        ]
    )
    assert roles._parse_json(stream) == {"late": True}
    unparseable_tail = "\n".join(
        [
            json.dumps({"type": "agent_message", "text": '{"early": true}'}),
            json.dumps({"type": "agent_message", "text": "no json in this one"}),
        ]
    )
    assert roles._parse_json(unparseable_tail) == {"early": True}
    assert roles._parse_json('prefix {"c": 3} suffix') == {"c": 3}
    assert roles._parse_json("prefix {not: json} suffix") is None
    assert roles._parse_json("no braces here") is None


@pytest.mark.xfail(
    strict=True,
    reason="_parse_json cuts a code fence out of the RAW event stream before it reads the stream, "
    "so a fenced JSON agent message (its newlines are escaped) is torn apart and never parsed",
)
def test_parse_json_reads_a_fenced_proposal_inside_an_event_stream_message():
    message = 'Result:\n```json\n{"late": true}\n```'
    stream = "\n".join(
        [
            json.dumps({"type": "turn.started"}),
            json.dumps({"item": {"type": "agent_message", "text": message}}),
        ]
    )
    assert roles._parse_json(stream) == {"late": True}


# --------------------------------------------------------------------------------------
# The seams that actually invoke a role
# --------------------------------------------------------------------------------------
EPIC_ASSIGNMENT = {
    "target": "owner/repo#7",
    "task_type": "epic",
    "lane": "epic_lane",
    "goal": "Deliver the epic",
    "context": "Some context",
    "target_detail": "The detail",
    "acceptance_criteria": ["ac1"],
    "constraints": ["c1"],
    "expected_paths": ["src/x.py"],
    "prompt": "short prompt",
}
SHADOW_ON = {"ORCH_ROLE_SHADOW": "1"}


def _selector_event(role, reason, target, run_id, *, accepted, disagreement):
    kwargs = {
        "reason": reason,
        "target": target,
        "matched": True,
        "invoked": True,
        "accepted": accepted,
        "disagreement": disagreement,
        "role_run_id": run_id,
    }
    return ((role, "invoked"), kwargs)


def _stub_selector_world(monkeypatch, agent="codex"):
    picks = Spy(result={"agent": agent, "mode": "full"})
    monkeypatch.setattr(roles, "route_role", picks)
    events = Spy(result={})
    monkeypatch.setattr(feedback, "record_role_selector_event", events)
    return picks, events


def test_dispatch_seam_invokes_prompt_then_decomposer_and_threads_the_accepted_output(monkeypatch):
    picks, events = _stub_selector_world(monkeypatch)
    prompt_runner = Spy(
        result={
            "role_run_id": "role:prompt:codex:1",
            "proposal": {"task_type": "epic"},
            "errors": [],
            "dispatch_prompt": "AUTHORED PROMPT\n",
            "decision_source": "prompt_agent",
        }
    )
    plan = {"epic": {"title": "Epic"}, "subtasks": []}
    decomposer_runner = Spy(
        result={
            "role_run_id": "role:decomposer:codex:2",
            "proposal": plan,
            "errors": [],
            "decision_source": "decomposer_agent",
        }
    )
    out = roles.activate_dispatch_roles(
        EPIC_ASSIGNMENT,
        "BASELINE PROMPT\n",
        cwd="/work",
        env=SHADOW_ON,
        cap=CAP,
        prompt_runner=prompt_runner,
        decomposer_runner=decomposer_runner,
    )
    route_kwargs = {"cap": CAP, "exploration_rate": 0.0}
    assert picks.calls == [(("prompt",), route_kwargs), (("decomposer",), route_kwargs)]
    common = {
        "target": "owner/repo#7",
        "goal": "Deliver the epic",
        "context": "Some context",
        "repo": "owner/repo",
        "cap": CAP,
        "backend": "codex",
        "dispatch": True,
        "cwd": "/work",
    }
    prompt_only = {
        "task_type": "epic",
        "target_detail": "The detail",
        "lane": "epic_lane",
        "acceptance_criteria": ["ac1"],
        "constraints": ["c1"],
        "expected_paths": ["src/x.py"],
    }
    assert prompt_runner.calls == [((), {**prompt_only, **common})]
    assert decomposer_runner.calls == [((), common)]
    assert out["prompt"] == (
        "AUTHORED PROMPT\n\nADVISORY DECOMPOSER PLAN (deterministic epic schema validated; "
        "rails remain authoritative):\n" + json.dumps(plan, sort_keys=True)
    )
    assert out["accepted_role_run_ids"] == ["role:prompt:codex:1", "role:decomposer:codex:2"]
    assert out["rejected_role_run_ids"] == []
    assert [(s["role"], s["reason"], s["accepted"]) for s in out["selectors"]] == [
        ("prompt", "underspecified", True),
        ("decomposer", "epic_lane", True),
    ]
    assert out["results"] == [
        {
            "role": "prompt",
            "role_run_id": "role:prompt:codex:1",
            "accepted": True,
            "decision_source": "prompt_agent",
            "errors": [],
        },
        {
            "role": "decomposer",
            "role_run_id": "role:decomposer:codex:2",
            "accepted": True,
            "decision_source": "decomposer_agent",
            "errors": [],
        },
    ]
    assert [call for call in events.calls if "role_run_id" in call[1]] == [
        _selector_event(
            "prompt",
            "underspecified",
            "owner/repo#7",
            "role:prompt:codex:1",
            accepted=True,
            disagreement=False,
        ),
        _selector_event(
            "decomposer",
            "epic_lane",
            "owner/repo#7",
            "role:decomposer:codex:2",
            accepted=True,
            disagreement=False,
        ),
    ]


@pytest.mark.parametrize(
    ("result", "accepted", "bucket"),
    [
        ({"role_run_id": "r1", "proposal": {"x": 1}, "errors": []}, True, "accepted_role_run_ids"),
        (
            {"role_run_id": "r1", "proposal": {"x": 1}, "errors": ["invalid"]},
            False,
            "rejected_role_run_ids",
        ),
        ({"role_run_id": "r1", "proposal": None, "errors": []}, False, "rejected_role_run_ids"),
        ({"role_run_id": None, "proposal": {"x": 1}, "errors": []}, False, None),
    ],
)
def test_dispatch_seam_accepts_only_a_recorded_valid_error_free_proposal(
    monkeypatch, result, accepted, bucket
):
    _picks, events = _stub_selector_world(monkeypatch)
    runner = Spy(
        result={**result, "dispatch_prompt": "AUTHORED", "decision_source": "prompt_agent"}
    )
    out = roles.activate_dispatch_roles(
        {"target": "owner/repo#8", "prompt": "short"},
        "BASELINE",
        cwd=".",
        env=SHADOW_ON,
        cap=CAP,
        prompt_runner=runner,
        decomposer_runner=Spy(error=AssertionError("not an epic")),
    )
    assert out["prompt"] == ("AUTHORED" if accepted else "BASELINE")
    for name in ("accepted_role_run_ids", "rejected_role_run_ids"):
        assert out[name] == (["r1"] if name == bucket else [])
    assert out["selectors"][0]["accepted"] is accepted
    assert [call for call in events.calls if "role_run_id" in call[1]] == [
        _selector_event(
            "prompt",
            "underspecified",
            "owner/repo#8",
            result["role_run_id"],
            accepted=accepted,
            disagreement=not accepted,
        )
    ]
    (entry,) = out["results"]
    assert entry["accepted"] is accepted and entry["errors"] == result["errors"]


def test_dispatch_seam_falls_back_to_body_and_target_and_survives_a_failed_event_write(
    monkeypatch,
):
    picks, _events = _stub_selector_world(monkeypatch)

    def record(role_name, status, **kwargs):
        if "role_run_id" in kwargs:
            raise RuntimeError("brain down")
        return {}

    monkeypatch.setattr(feedback, "record_role_selector_event", record)
    runner = Spy(
        result={
            "role_run_id": "r1",
            "proposal": {"x": 1},
            "errors": [],
            "dispatch_prompt": "AUTHORED",
            "decision_source": "prompt_agent",
        }
    )
    out = roles.activate_dispatch_roles(
        {"target": "no-issue-number", "body": "Body text"},
        "BASELINE",
        cwd=".",
        env=SHADOW_ON,
        cap=CAP,
        prompt_runner=runner,
    )
    assert runner.calls == [
        (
            (),
            {
                "task_type": "implement",
                "target_detail": "Body text",
                "lane": None,
                "acceptance_criteria": [],
                "constraints": [],
                "expected_paths": [],
                "target": "no-issue-number",
                "goal": "no-issue-number",
                "context": "",
                "repo": "",
                "cap": CAP,
                "backend": "codex",
                "dispatch": True,
                "cwd": ".",
            },
        )
    ]
    assert out["selectors"][0]["record_error"] == "brain down"
    assert out["selectors"][0]["accepted"] is True
    assert out["prompt"] == "AUTHORED" and out["accepted_role_run_ids"] == ["r1"]
    assert len(picks.calls) == 1


def test_dispatch_seam_invokes_nothing_when_gated_dry_run_capped_or_without_capacity(monkeypatch):
    picks, events = _stub_selector_world(monkeypatch)
    runner = Spy(error=AssertionError("no role may run"))

    def run(env, **kwargs):
        return roles.activate_dispatch_roles(
            EPIC_ASSIGNMENT,
            "BASE",
            cwd=".",
            env=env,
            cap=CAP,
            prompt_runner=runner,
            decomposer_runner=runner,
            **kwargs,
        )

    def statuses(out):
        return [(s["selector_status"], s["reason"]) for s in out["selectors"]]

    gated = run({})
    assert statuses(gated) == [("matched_not_invoked", "shadow_gate_disabled")] * 2
    assert gated["prompt"] == "BASE" and gated["results"] == [] and picks.calls == []
    events.calls.clear()
    dry = run(SHADOW_ON, dry_run=True)
    assert statuses(dry) == [("matched_not_invoked", "shadow_gate_disabled")] * 2
    assert picks.calls == [] and events.calls == []
    capped = run({**SHADOW_ON, "ORCH_ROLE_MAX_PER_CYCLE": "0"})
    assert statuses(capped) == [("matched_not_invoked", "per_cycle_invocation_cap")] * 2
    picks.result = None
    starved = run(SHADOW_ON)
    assert statuses(starved) == [("matched_not_invoked", "no_role_capacity")] * 2
    assert runner.calls == []


def test_tick_triage_runs_one_bounded_snapshot_and_indexes_recommendations_by_target(monkeypatch):
    picks, events = _stub_selector_world(monkeypatch, agent="cursor")
    first, second = _rec("o/r#1"), _rec("o/r#2", action="defer", priority=3)
    result = {
        "role_run_id": "role:triage:cursor:1",
        "proposal": {"summary": "s"},
        "errors": [],
        "advisory_plan": {"recommendations": [first, {"action": "skip"}, "junk", second]},
    }
    runner = Spy(result=result)
    items = [{"target": "o/r#1"}, {"target": "o/r#2"}]
    out = roles.activate_tick_triage(items, CAP, env=SHADOW_ON, runner=runner)
    assert picks.calls == [(("triage",), {"cap": CAP, "exploration_rate": 0.0})]
    assert runner.calls == [
        (
            (),
            {
                "backlog_items": items,
                "cap": CAP,
                "backend": "cursor",
                "dispatch": True,
                "max_items": 20,
                "cwd": ".",
            },
        )
    ]
    assert out["result"] is result
    assert out["recommendations"] == {"o/r#1": first, "o/r#2": second}
    selector = out["selector"]
    assert (selector["selector_status"], selector["reason"], selector["accepted"]) == (
        "invoked",
        "bounded_backlog_snapshot",
        True,
    )
    assert events.calls == [
        (
            ("triage", "invoked"),
            {
                "reason": "bounded_backlog_snapshot",
                "target": "backlog:2",
                "matched": True,
                "invoked": True,
                "disagreement": False,
            },
        ),
        _selector_event(
            "triage",
            "bounded_backlog_snapshot",
            "backlog:2",
            "role:triage:cursor:1",
            accepted=True,
            disagreement=False,
        ),
    ]


@pytest.mark.parametrize(
    ("overrides", "accepted"),
    [
        ({}, True),
        ({"errors": ["bad"]}, False),
        ({"proposal": None}, False),
        ({"role_run_id": None}, False),
    ],
)
def test_tick_triage_accepts_only_a_recorded_valid_error_free_snapshot(
    monkeypatch, overrides, accepted
):
    _picks, events = _stub_selector_world(monkeypatch)
    plan = {"recommendations": [_rec("o/r#1")]}
    result = {
        "role_run_id": "role:triage:codex:1",
        "proposal": {"summary": "s"},
        "errors": [],
        "advisory_plan": plan,
        **overrides,
    }
    out = roles.activate_tick_triage(
        [{"target": "o/r#1"}], CAP, env=SHADOW_ON, runner=Spy(result=result)
    )
    assert out["selector"]["accepted"] is accepted
    assert out["recommendations"] == {"o/r#1": plan["recommendations"][0]}
    assert [call for call in events.calls if "role_run_id" in call[1]] == [
        _selector_event(
            "triage",
            "bounded_backlog_snapshot",
            "backlog:1",
            result["role_run_id"],
            accepted=accepted,
            disagreement=not accepted,
        )
    ]


def test_tick_triage_tolerates_a_missing_plan_and_a_failed_event_write(monkeypatch):
    _stub_selector_world(monkeypatch)
    monkeypatch.setattr(feedback, "record_role_selector_event", Spy(error=RuntimeError("down")))
    for plan in ({}, {"advisory_plan": None}, {"advisory_plan": {"recommendations": None}}):
        roles.reset_role_invocation_counts()
        runner = Spy(result={"role_run_id": "r1", "proposal": {"s": 1}, "errors": [], **plan})
        out = roles.activate_tick_triage([{"target": "o/r#1"}], CAP, env=SHADOW_ON, runner=runner)
        assert out["recommendations"] == {}
        assert out["selector"]["record_error"] == "down"
        assert out["selector"]["accepted"] is True


def test_tick_triage_never_runs_without_items_the_gate_a_cap_or_capacity(monkeypatch):
    picks, events = _stub_selector_world(monkeypatch)
    runner = Spy(error=AssertionError("triage must not run"))
    items = [{"target": "o/r#1"}]
    for kwargs, status, reason in (
        ({"env": SHADOW_ON, "dry_run": True}, "matched_not_invoked", "shadow_gate_disabled"),
        ({"env": {}}, "matched_not_invoked", "shadow_gate_disabled"),
        (
            {"env": {**SHADOW_ON, "ORCH_TRIAGE_ROLE_MAX_PER_CYCLE": "0"}},
            "matched_not_invoked",
            "per_cycle_invocation_cap",
        ),
    ):
        out = roles.activate_tick_triage(items, CAP, runner=runner, **kwargs)
        assert out["result"] is None and out["recommendations"] == {}
        assert (out["selector"]["selector_status"], out["selector"]["reason"]) == (status, reason)
    empty = roles.activate_tick_triage([], CAP, env=SHADOW_ON, runner=runner)
    assert empty["selector"]["selector_status"] == "no_matching_work"
    picks.result = None
    starved = roles.activate_tick_triage(items, CAP, env=SHADOW_ON, runner=runner)
    assert starved["selector"]["reason"] == "no_role_capacity"
    assert runner.calls == []


DISAGREEING = (
    {"target": "o/r#5"},
    {"verdict": "pass", "evidence_ref": "gate:1"},
    {"result": {"verdict": "fail"}, "lineage": "review:1"},
)
DISAGREEMENT_CASE = {
    "case_type": "runtime_ac",
    "target": "o/r#5",
    "gate_verdict": "PASS",
    "panel_verdict": "FAIL",
    "disputed_finding": "runtime gate PASS disagrees with review panel FAIL",
    "ground_truth_evidence": {"runtime_ac": "gate:1", "review": "review:1"},
}


def test_adjudicator_activation_invokes_the_role_on_a_genuine_disagreement(monkeypatch):
    picks, events = _stub_selector_world(monkeypatch, agent="gemini")
    result = {"role_run_id": "role:adjudicator:gemini:1", "proposal": {"d": 1}, "errors": []}
    runner = Spy(result=result)
    out = roles.activate_adjudicator_disagreement(*DISAGREEING, CAP, env=SHADOW_ON, runner=runner)
    assert picks.calls == [(("adjudicator",), {"cap": CAP, "exploration_rate": 0.0})]
    assert runner.calls == [
        (
            (),
            {
                "case": DISAGREEMENT_CASE,
                "cap": CAP,
                "backend": "gemini",
                "dispatch": True,
                "cwd": ".",
            },
        )
    ]
    assert out["case"] == DISAGREEMENT_CASE and out["result"] is result
    selector = out["selector"]
    assert (selector["selector_status"], selector["reason"]) == (
        "invoked",
        "persisted_evidence_disagreement",
    )
    assert selector["accepted"] is True and selector["disagreement"] is True
    assert events.calls[-1] == _selector_event(
        "adjudicator",
        "persisted_evidence_disagreement",
        "o/r#5",
        "role:adjudicator:gemini:1",
        accepted=True,
        disagreement=True,
    )


@pytest.mark.parametrize(
    ("overrides", "accepted"),
    [
        ({}, True),
        ({"errors": ["bad"]}, False),
        ({"proposal": None}, False),
        ({"role_run_id": ""}, False),
    ],
)
def test_adjudicator_activation_accepts_only_a_recorded_valid_error_free_proposal(
    monkeypatch, overrides, accepted
):
    _picks, events = _stub_selector_world(monkeypatch)
    result = {"role_run_id": "r1", "proposal": {"d": 1}, "errors": [], **overrides}
    out = roles.activate_adjudicator_disagreement(
        *DISAGREEING, CAP, env=SHADOW_ON, runner=Spy(result=result)
    )
    assert out["selector"]["accepted"] is accepted
    assert events.calls[-1] == _selector_event(
        "adjudicator",
        "persisted_evidence_disagreement",
        "o/r#5",
        result["role_run_id"],
        accepted=accepted,
        disagreement=True,
    )


def test_adjudicator_activation_survives_a_failed_event_write(monkeypatch):
    _stub_selector_world(monkeypatch)
    monkeypatch.setattr(feedback, "record_role_selector_event", Spy(error=RuntimeError("down")))
    runner = Spy(result={"role_run_id": "r1", "proposal": {"d": 1}, "errors": []})
    out = roles.activate_adjudicator_disagreement(*DISAGREEING, CAP, env=SHADOW_ON, runner=runner)
    assert out["selector"]["record_error"] == "down"
    assert out["selector"]["accepted"] is True and out["result"]["role_run_id"] == "r1"


def test_adjudicator_activation_skips_agreeing_evidence_and_every_closed_gate(monkeypatch):
    picks, events = _stub_selector_world(monkeypatch, agent="gemini")
    runner = Spy(error=AssertionError("the adjudicator must not run"))
    item, gate_status, _review_status = DISAGREEING
    agreeing = {"result": {"verdict": "PASS"}}
    out = roles.activate_adjudicator_disagreement(
        item, gate_status, agreeing, CAP, env=SHADOW_ON, runner=runner
    )
    assert out["case"] is None and out["result"] is None
    assert (out["selector"]["selector_status"], out["selector"]["reason"]) == (
        "no_matching_work",
        "evidence_agrees_or_incomplete",
    )
    assert picks.calls == []
    closed = roles.activate_adjudicator_disagreement(*DISAGREEING, CAP, env={}, runner=runner)
    assert closed["case"] == DISAGREEMENT_CASE and closed["result"] is None
    assert closed["selector"]["reason"] == "shadow_gate_disabled"
    events.calls.clear()
    dry = roles.activate_adjudicator_disagreement(
        *DISAGREEING, CAP, env=SHADOW_ON, dry_run=True, runner=runner
    )
    assert dry["result"] is None and dry["selector"]["reason"] == "shadow_gate_disabled"
    assert events.calls == []
    capped = roles.activate_adjudicator_disagreement(
        *DISAGREEING,
        CAP,
        env={**SHADOW_ON, "ORCH_ADJUDICATOR_ROLE_MAX_PER_CYCLE": "0"},
        runner=runner,
    )
    assert capped["selector"]["reason"] == "per_cycle_invocation_cap"
    picks.result = None
    starved = roles.activate_adjudicator_disagreement(
        *DISAGREEING, CAP, env=SHADOW_ON, runner=runner
    )
    assert starved["selector"]["reason"] == "no_role_capacity" and starved["result"] is None
    assert runner.calls == []


# --------------------------------------------------------------------------------------
# Runners: no capacity, a failing backend, an unrecordable run
# --------------------------------------------------------------------------------------
EMPTY_CAP = {"agents": {}}
NO_CAPACITY = {"cap": EMPTY_CAP, "learned": {}, "exploration_rate": 0.0}
REPORT = {"target": "owner/repo#1", "state": "running", "recommended_action": "wait"}
WAIT_PROPOSAL = {
    "action": "wait",
    "reason": "Lane is healthy.",
    "confidence": "high",
    "corrected_prompt": None,
    "switch_agent": None,
}
PLAN = epic_lane._valid_plan()
CASE = {
    "case_type": "runtime_ac",
    "target": "o/r#5",
    "disputed_finding": "runtime evidence is missing",
    "ground_truth_evidence": [{"ref": "runtime-ac:1", "status": "PASS"}],
}
DECOMPOSER_PARSE = "could not parse a JSON decomposition plan from the backend output"
TRIAGE_PARSE = "could not parse a JSON triage proposal from the backend output"
ADJUDICATOR_PARSE = "could not parse a JSON adjudication proposal from the backend output"
FAILURE_CASES = [
    (3, "boom", ["backend exit=3 boom"]),
    (3, None, ["backend exit=3"]),
    (0, "ignored", []),
    (None, "ignored", []),
]


def _forbid_backend(monkeypatch):
    offload = Spy(error=AssertionError("no backend may be launched"))
    monkeypatch.setattr(dispatcher, "offload", offload)
    return offload


def _redirect(**kwargs):
    return roles.run_redirect_agent(
        REPORT, "AC", dispatch=True, backend="codex", proposal_json=WAIT_PROPOSAL, **kwargs
    )


def test_redirect_dispatch_without_capacity_names_the_reason_and_launches_nothing(monkeypatch):
    offload = _forbid_backend(monkeypatch)
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    result = roles.run_redirect_agent(REPORT, "AC", dispatch=True, **NO_CAPACITY)
    assert result["errors"] == ["no eligible backend has capacity for the redirect role"]
    assert result["backend"] is None and result["routing"] is None
    assert result["proposal"] is None and result["decision_source"] == "baseline_policy"
    assert result["role_run_id"] is None and result["backend_run_id"] is None
    assert offload.calls == [] and record.calls == []
    quiet = roles.run_redirect_agent(REPORT, "AC", dispatch=False, **NO_CAPACITY)
    assert quiet["errors"] == []


def test_redirect_role_run_that_cannot_be_recorded_is_reported_and_not_stamped_on_the_plan(
    monkeypatch,
):
    monkeypatch.setattr(feedback, "record_role_run", Spy(error=RuntimeError("brain down")))
    failed = _redirect()
    assert failed["role_record_error"] == "brain down" and failed["role_run_id"] is None
    assert "accepted_role_run_id" not in failed["plan"]
    assert failed["proposal"] == WAIT_PROPOSAL and failed["errors"] == []
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    recorded = _redirect()
    assert recorded["role_record_error"] is None
    assert recorded["role_run_id"].startswith("role:redirect:codex:")
    assert recorded["plan"]["accepted_role_run_id"] == recorded["role_run_id"]
    ((args, kwargs),) = record.calls
    assert args == (recorded["role_run_id"], "redirect", "owner/repo#1", "codex")
    assert kwargs == {
        "reasoning_level": "full",
        "backend_run_id": None,
        "action": "wait",
        "decision_source": "redirect_agent",
        "proposal": WAIT_PROPOSAL,
        "model": None,
        "source": "live-dispatch",
        "report_state": "running",
    }


def test_redirect_corpus_recording_hands_over_the_run_and_never_fails_the_main_path(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(feedback, "record_role_run", Spy())
    corpus = Spy(result={"recorded": True, "corpus": "shadow.jsonl"})
    monkeypatch.setattr(redirect_shadow, "record_proposal", corpus)
    explicit = tmp_path / "explicit.jsonl"
    result = _redirect(record_corpus=True, corpus_path=str(explicit), source="unit-test")
    assert result["corpus_record"] == {"recorded": True, "corpus": "shadow.jsonl"}
    ((_, handed),) = corpus.calls
    assert handed["corpus_path"] == explicit and isinstance(handed["corpus_path"], Path)
    assert handed["role_run_id"] == result["role_run_id"] and handed["source"] == "unit-test"
    assert (handed["acceptance_criteria"], handed["proposal"]) == ("AC", WAIT_PROPOSAL)
    assert (handed["decision_source"], handed["errors"]) == ("redirect_agent", [])
    assert (handed["backend"], handed["backend_run_id"]) == ("codex", None)
    assert handed["plan"]["action"] == "wait" and "accepted_role_run_id" not in handed["plan"]

    default_path = tmp_path / "default.jsonl"
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", default_path)
    _redirect(record_corpus=True)
    assert corpus.calls[-1][1]["corpus_path"] == default_path
    assert corpus.calls[-1][1]["source"] == "live-dispatch"

    corpus.error = OSError("disk full")
    failed = _redirect(record_corpus=True)
    assert failed["corpus_record"] == {"recorded": False, "error": "disk full"}
    assert failed["proposal"] == WAIT_PROPOSAL and failed["errors"] == []
    assert failed["role_run_id"].startswith("role:redirect:codex:")

    corpus.calls.clear()
    plain = _redirect()
    assert plain["corpus_record"] is None and corpus.calls == []


def test_prompt_agent_refuses_an_unknown_output_kind_before_building_anything(monkeypatch):
    monkeypatch.setattr(dispatcher, "build_prompt", Spy(error=AssertionError("nothing is built")))
    with pytest.raises(ValueError, match=r"^output must be dispatch_prompt or issue_body$"):
        roles.run_prompt_agent(target="o/r#1", goal="g", output="markdown")


def test_prompt_agent_without_capacity_reports_it_and_keeps_the_baseline_prompt(monkeypatch):
    build = Spy(result="BASELINE PROMPT")
    monkeypatch.setattr(dispatcher, "build_prompt", build)
    offload = _forbid_backend(monkeypatch)
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    result = roles.run_prompt_agent(
        target="o/r#1",
        goal="the goal",
        target_detail="the detail",
        lane="opener",
        dispatch=True,
        **NO_CAPACITY,
    )
    assert result["errors"] == ["no eligible backend has capacity for the prompt role"]
    assert build.calls == [(("implement", "o/r#1", "the detail"), {"lane": "opener"})]
    assert result["baseline_prompt"] == result["dispatch_prompt"] == "BASELINE PROMPT"
    assert result["decision_source"] == "baseline_dispatcher" and result["proposal"] is None
    assert result["backend"] is None and result["role_run_id"] is None
    assert offload.calls == [] and record.calls == []
    quiet = roles.run_prompt_agent(target="o/r#1", goal="g", **NO_CAPACITY)
    assert quiet["errors"] == []


def test_prompt_batch_refuses_malformed_batches_before_routing(monkeypatch):
    route = Spy(error=AssertionError("a malformed batch must not be routed"))
    monkeypatch.setattr(roles, "route_role", route)
    good_item = {"target": "o/r#1", "goal": "g"}
    with pytest.raises(ValueError, match=r"^output must be dispatch_prompt or issue_body$"):
        roles.run_prompt_batch([good_item], output="markdown")
    for batch in ([], "not a list", None):
        with pytest.raises(ValueError, match=r"^batch must be a non-empty list of item objects$"):
            roles.run_prompt_batch(batch)
    for item in ("a string", {**good_item, "surprise": 1}):
        message = r"^batch item has unknown fields or is not an object$"
        with pytest.raises(ValueError, match=message):
            roles.run_prompt_batch([item])
    assert route.calls == []


def test_decomposer_dispatch_without_capacity_names_the_reason_and_keeps_the_planner_prompt(
    monkeypatch,
):
    offload = _forbid_backend(monkeypatch)
    result = roles.run_decomposer_agent(goal="Ship it", dispatch=True, **NO_CAPACITY)
    assert result["errors"] == ["no eligible backend has capacity for the decomposer role"]
    assert result["decision_source"] == "baseline_epic_lane" and result["proposal"] is None
    assert result["dispatch_prompts"] == [] and result["backend"] is None
    assert result["baseline_prompt"] == epic_lane.build_planner_prompt(goal="Ship it")
    assert offload.calls == []


@pytest.mark.parametrize(("exit_code", "error", "prefix"), FAILURE_CASES)
def test_decomposer_reports_a_failing_backend_and_an_unparseable_plan(
    monkeypatch, exit_code, error, prefix
):
    raw = "I could not do it"
    offload = Spy(
        result={"run_id": "b1", "model": "m1", "output": raw, "exit": exit_code, "error": error}
    )
    monkeypatch.setattr(dispatcher, "offload", offload)
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    result = roles.run_decomposer_agent(
        goal="Ship it", target="o/r#4", backend="codex", dispatch=True
    )
    assert result["errors"] == prefix + [DECOMPOSER_PARSE]
    assert result["proposal"] is None and result["decision_source"] == "baseline_epic_lane"
    assert result["backend_run_id"] == "b1" and result["raw_output"] == raw
    subject = feedback.work_item_subject("o/r#4")
    assert subject
    assert offload.calls == [
        (
            ("codex", result["prompt"]),
            {"cwd": ".", "mode": "full", "timeout": 600, "subject_id": subject},
        )
    ]
    ((args, kwargs),) = record.calls
    assert args == (result["role_run_id"], "decomposer", "o/r#4", "codex")
    assert kwargs == {
        "reasoning_level": "full",
        "backend_run_id": "b1",
        "action": None,
        "decision_source": "baseline_epic_lane",
        "proposal": None,
        "model": "m1",
    }


def test_decomposer_role_run_that_cannot_be_recorded_is_reported(monkeypatch):
    monkeypatch.setattr(feedback, "record_role_run", Spy(error=RuntimeError("brain down")))
    result = roles.run_decomposer_agent(
        goal="Ship it", backend="codex", dispatch=True, proposal_json=PLAN
    )
    assert result["role_record_error"] == "brain down" and result["role_run_id"] is None
    assert result["decision_source"] == "decomposer_agent" and result["proposal"] == PLAN
    assert [prompt["id"] for prompt in result["dispatch_prompts"]] == ["E1", "E2", "E3"]
    assert result["errors"] == []


def test_decomposer_reports_success_to_the_ledger_only_for_a_live_valid_plan(monkeypatch):
    events = Spy()
    monkeypatch.setattr(roles, "_role_capability_event", events)
    monkeypatch.setattr(feedback, "record_role_run", Spy())
    offload = Spy(result={"run_id": "b9", "model": "m", "output": json.dumps(PLAN), "exit": 0})
    monkeypatch.setattr(dispatcher, "offload", offload)
    result = roles.run_decomposer_agent(
        goal="Ship it", target="o/r#4", backend="codex", dispatch=True
    )
    assert result["errors"] == [] and result["decision_source"] == "decomposer_agent"
    assert events.calls == [
        (("decomposer", "match"), {"metadata": {"target": "o/r#4"}}),
        (("decomposer", "invocation"), {"metadata": {"backend": "codex"}}),
        (
            ("decomposer", "success"),
            {"ref": "b9", "metadata": {"decision_source": "decomposer_agent"}},
        ),
    ]
    events.calls.clear()
    replay = roles.run_decomposer_agent(
        goal="Ship it", target="o/r#4", backend="codex", dispatch=True, proposal_json=PLAN
    )
    assert replay["errors"] == []
    assert events.calls == [(("decomposer", "match"), {"metadata": {"target": "o/r#4"}})]
    events.calls.clear()
    offload.result = {"run_id": "b10", "output": json.dumps({"epic": {}}), "exit": 0}
    invalid = roles.run_decomposer_agent(
        goal="Ship it", target="o/r#4", backend="codex", dispatch=True
    )
    assert invalid["errors"] and invalid["proposal"] is None
    assert [call[0] for call in events.calls] == [
        ("decomposer", "match"),
        ("decomposer", "invocation"),
    ]


def test_triage_dispatch_without_capacity_names_the_reason_and_keeps_the_baseline_order(
    monkeypatch,
):
    offload = _forbid_backend(monkeypatch)
    result = roles.run_triage_agent(
        backlog_items=[{"target": "o/r#1"}], dispatch=True, **NO_CAPACITY
    )
    assert result["errors"] == ["no eligible backend has capacity for the triage role"]
    assert result["decision_source"] == "baseline_backlog_order" and result["proposal"] is None
    assert result["advisory_plan"] == result["baseline"] and result["backend"] is None
    assert offload.calls == []


@pytest.mark.parametrize(("exit_code", "error", "prefix"), FAILURE_CASES)
def test_triage_reports_a_failing_backend_and_an_unparseable_proposal(
    monkeypatch, exit_code, error, prefix
):
    raw = "nothing useful"
    offload = Spy(
        result={"run_id": "b2", "model": "m2", "output": raw, "exit": exit_code, "error": error}
    )
    monkeypatch.setattr(dispatcher, "offload", offload)
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    result = roles.run_triage_agent(
        backlog_items=[{"target": "o/r#1"}], backend="codex", dispatch=True, cap=CAP
    )
    assert result["errors"] == prefix + [TRIAGE_PARSE]
    assert result["proposal"] is None and result["decision_source"] == "baseline_backlog_order"
    assert result["backend_run_id"] == "b2" and result["raw_output"] == raw
    assert offload.calls == [
        (("codex", result["prompt"]), {"cwd": ".", "mode": "full", "timeout": 600})
    ]
    ((args, kwargs),) = record.calls
    assert args == (result["role_run_id"], "triage", "triage:1-items", "codex")
    assert kwargs == {
        "reasoning_level": "full",
        "backend_run_id": "b2",
        "action": "work_now:1",
        "decision_source": "baseline_backlog_order",
        "proposal": None,
        "model": "m2",
    }


def test_triage_role_run_that_cannot_be_recorded_is_reported(monkeypatch):
    monkeypatch.setattr(feedback, "record_role_run", Spy(error=RuntimeError("brain down")))
    result = roles.run_triage_agent(
        backlog_items=[{"target": "o/r#1"}],
        backend="codex",
        dispatch=True,
        cap=CAP,
        proposal_json=_valid_triage("o/r#1"),
    )
    assert result["role_record_error"] == "brain down" and result["role_run_id"] is None
    assert result["decision_source"] == "triage_agent" and result["errors"] == []


def test_adjudicator_refuses_live_dispatch_for_an_invalid_case(monkeypatch):
    offload = _forbid_backend(monkeypatch)
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    result = roles.run_adjudicator_agent(
        case={"case_type": "review"}, backend="codex", dispatch=True
    )
    case_errors = [
        "case.target is required",
        "case.disputed_finding is required",
        "case.ground_truth_evidence is required",
    ]
    assert result["errors"] == case_errors + ["case validation failed; refusing live dispatch"]
    assert result["decision_source"] == "baseline_needs_more_evidence"
    assert result["proposal"] is None and result["advisory_plan"]["evidence_gaps"] == case_errors
    assert offload.calls == [] and result["backend_run_id"] is None
    assert result["role_run_id"].startswith("role:adjudicator:codex:")
    assert record.calls == [((), result["role_record"])]
    assert result["role_record"]["action"] == "needs_more_evidence"


def _acknowledging_offload(proposal):
    return Spy(
        result={
            "run_id": "b3",
            "model": "m3",
            "output": json.dumps(proposal),
            "exit": 0,
            "usage": {"input_tokens": 5, "output_tokens": 7},
            "attempt_receipt_callback_called": True,
        }
    )


def test_adjudicator_requires_the_dispatcher_to_acknowledge_the_attempt_receipt(monkeypatch):
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    hook, start = Spy(), Spy()
    offload = Spy(result={"run_id": "b1", "output": "{}", "exit": 0})
    monkeypatch.setattr(dispatcher, "offload", offload)
    message = r"^native dispatcher did not acknowledge the attempt receipt callback$"
    with pytest.raises(RuntimeError, match=message):
        roles.run_adjudicator_agent(case=CASE, backend="codex", dispatch=True, lifecycle_hook=hook)
    assert record.calls == []
    unhooked = roles.run_adjudicator_agent(case=CASE, backend="codex", dispatch=True)
    assert unhooked["backend_run_id"] == "b1"
    assert offload.calls[-1][1]["max_network_retries"] is None

    offload = _acknowledging_offload(_valid_adjudication())
    monkeypatch.setattr(dispatcher, "offload", offload)
    result = roles.run_adjudicator_agent(
        case=CASE, backend="codex", dispatch=True, lifecycle_hook=hook, lifecycle_start_hook=start
    )
    assert result["errors"] == [] and result["decision_source"] == "adjudicator_agent"
    ((_, kwargs),) = offload.calls
    assert kwargs["attempt_receipt_callback"] is hook
    assert kwargs["attempt_started_callback"] is start
    assert kwargs["max_network_retries"] == 0 and kwargs["isolate"] is False
    assert kwargs["subject_id"] == feedback.work_item_subject("o/r#5")


@pytest.mark.parametrize(("exit_code", "error", "prefix"), FAILURE_CASES)
def test_adjudicator_reports_a_failing_backend_and_an_unparseable_proposal(
    monkeypatch, exit_code, error, prefix
):
    offload = Spy(
        result={
            "run_id": "b4",
            "model": "m4",
            "output": "no json here",
            "exit": exit_code,
            "error": error,
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }
    )
    monkeypatch.setattr(dispatcher, "offload", offload)
    record = Spy()
    monkeypatch.setattr(feedback, "record_role_run", record)
    result = roles.run_adjudicator_agent(case=CASE, backend="codex", dispatch=True)
    assert result["errors"] == prefix + [ADJUDICATOR_PARSE]
    assert result["proposal"] is None and result["backend_run_id"] == "b4"
    assert result["decision_source"] == "baseline_needs_more_evidence"
    assert result["raw_output"] == "no json here"
    ((args, kwargs),) = record.calls
    assert args == () and kwargs == result["role_record"]
    assert (kwargs["backend_run_id"], kwargs["model"], kwargs["action"]) == (
        "b4",
        "m4",
        "needs_more_evidence",
    )
    assert (kwargs["cost_tokens_in"], kwargs["cost_tokens_out"], kwargs["imputed"]) == (5, 7, False)


# --------------------------------------------------------------------------------------
# Human renderers
# --------------------------------------------------------------------------------------
APPLY_HINT = (
    "  python3 redirect_plan.py --apply --confirm-target <exact-target> --next-agent <agent> "
    "--report-json <report-with-policy_decision>.json"
)


def test_format_human_lists_identity_errors_plan_and_the_apply_command(monkeypatch):
    plan_text = Spy(result="PLAN TEXT")
    monkeypatch.setattr(roles.redirect_plan, "format_human", plan_text)
    full = {
        "role": "redirect",
        "backend": "codex",
        "decision_source": "redirect_agent",
        "shadow": True,
        "role_run_id": "role:redirect:codex:1",
        "role_record_error": "brain down",
        "corpus_record": {"recorded": True, "corpus": "shadow.jsonl"},
        "errors": ["first", "second"],
        "plan": {"action": "wait"},
    }
    assert roles.format_human(full) == "\n".join(
        [
            "role=redirect backend=codex decision_source=redirect_agent shadow=True",
            "role_run_id=role:redirect:codex:1",
            "role_record_error=brain down",
            "corpus_recorded=shadow.jsonl",
            "errors: first; second",
            "",
            "PLAN TEXT",
            "",
            "SHADOW: advisory only - nothing was changed. To act after review:",
            APPLY_HINT,
        ]
    )
    assert plan_text.calls == [(({"action": "wait"},), {})]
    assert roles.format_human({}) == "\n".join(
        [
            "role=None backend=- decision_source=None shadow=None",
            "",
            "PLAN TEXT",
            "",
            "SHADOW: advisory only - nothing was changed. To act after review:",
            APPLY_HINT,
        ]
    )
    assert plan_text.calls[-1] == (({},), {})
    for corpus_record in ({"recorded": False, "corpus": "x.jsonl"}, "not a mapping"):
        assert "corpus_recorded" not in roles.format_human({"corpus_record": corpus_record})


def test_format_prompt_human_shows_the_dispatch_prompt_and_the_shadow_notice():
    shadow = "SHADOW: advisory prompt only - nothing was delegated or changed."
    full = {
        "role": "prompt",
        "backend": "gemini",
        "decision_source": "prompt_agent",
        "shadow": True,
        "role_run_id": "r1",
        "role_record_error": "down",
        "errors": ["e1", "e2"],
        "dispatch_prompt": "Do the thing.\n\n",
    }
    assert roles.format_prompt_human(full) == "\n".join(
        [
            "role=prompt backend=gemini decision_source=prompt_agent shadow=True",
            "role_run_id=r1",
            "role_record_error=down",
            "errors: e1; e2",
            "",
            "--- dispatch_prompt ---",
            "Do the thing.",
            "",
            shadow,
        ]
    )
    assert roles.format_prompt_human({}) == "\n".join(
        [
            "role=None backend=- decision_source=None shadow=None",
            "",
            "--- dispatch_prompt ---",
            "",
            "",
            shadow,
        ]
    )


def test_format_decomposer_human_previews_each_subtask_prompt_up_to_600_characters():
    shadow = "SHADOW: advisory decomposition only - no subtasks were delegated or changed."
    base = {"id": "E", "lane": "opener", "task_type": "implement"}
    full = {
        "role": "decomposer",
        "backend": "cursor",
        "decision_source": "decomposer_agent",
        "shadow": True,
        "role_run_id": "r2",
        "role_record_error": "down",
        "errors": ["e1", "e2"],
        "dispatch_prompts": [
            {**base, "id": "E1", "dependencies": [], "prompt": "  short prompt  "},
            {**base, "id": "E2", "lane": "local", "dependencies": ["E1"], "prompt": "x" * 601},
            {**base, "id": "E3", "dependencies": ["E1", "E2"], "prompt": "   "},
            {**base, "id": "E4", "prompt": "y" * 600},
        ],
    }
    assert roles.format_decomposer_human(full) == "\n".join(
        [
            "role=decomposer backend=cursor decision_source=decomposer_agent shadow=True",
            "role_run_id=r2",
            "role_record_error=down",
            "errors: e1; e2",
            "dispatch_prompts=4",
            "",
            "--- dispatch_prompts ---",
            "E1: lane=opener task_type=implement deps=[]",
            "short prompt",
            "",
            "E2: lane=local task_type=implement deps=['E1']",
            "x" * 600 + "...",
            "",
            "E3: lane=opener task_type=implement deps=['E1', 'E2']",
            "E4: lane=opener task_type=implement deps=[]",
            "y" * 600,
            "",
            shadow,
        ]
    )
    assert roles.format_decomposer_human({"role": "decomposer", "baseline_prompt": "BASE\n\n"}) == (
        "\n".join(
            [
                "role=decomposer backend=- decision_source=None shadow=None",
                "dispatch_prompts=0",
                "",
                "--- baseline_planner_prompt ---",
                "BASE",
                shadow,
            ]
        )
    )


def test_format_triage_human_lists_recommendations_batches_and_risks():
    shadow = (
        "SHADOW: advisory triage only - no claims, labels, delegates, or routing decisions "
        "were changed."
    )
    first = {"priority": 1, "action": "work_now", "target": "o/r#1", "batch_id": "B1"}
    second = {"priority": 3, "action": "defer", "target": "o/r#2", "batch_id": None}
    full = {
        "role": "triage",
        "backend": "vibe",
        "decision_source": "triage_agent",
        "shadow": True,
        "role_run_id": "r3",
        "role_record_error": "down",
        "errors": ["e1"],
        "visible_items": [{"target": "a"}, {"target": "b"}],
        "omitted_count": 4,
        "advisory_plan": {
            "summary": "Two items.",
            "recommendations": [{**first, "reason": "Ready."}, {**second, "reason": "Blocked."}],
            "batches": [
                {"id": "B1", "risk": "low", "targets": ["o/r#1", "o/r#3"], "reason": "Grouped."}
            ],
            "global_risks": ["Risk one", "Risk two"],
        },
    }
    assert roles.format_triage_human(full) == "\n".join(
        [
            "role=triage backend=vibe decision_source=triage_agent shadow=True",
            "role_run_id=r3",
            "role_record_error=down",
            "errors: e1",
            "visible_items=2 omitted=4",
            "",
            "--- triage ---",
            "Two items.",
            "- p1 work_now o/r#1 batch=B1: Ready.",
            "- p3 defer o/r#2: Blocked.",
            "",
            "--- batches ---",
            "- B1 risk=low: o/r#1, o/r#3 - Grouped.",
            "",
            "--- risks ---",
            "- Risk one",
            "- Risk two",
            "",
            shadow,
        ]
    )
    assert roles.format_triage_human({}) == "\n".join(
        [
            "role=None backend=- decision_source=None shadow=None",
            "visible_items=0 omitted=0",
            "",
            "--- triage ---",
            "",
            shadow,
        ]
    )


def test_format_adjudicator_human_lists_the_decision_evidence_gaps_and_next_step():
    shadow = (
        "SHADOW: advisory adjudication only - panel/adversarial aggregation, gates, merges, "
        "labels, claims, and delegation were not changed."
    )
    full = {
        "role": "adjudicator",
        "backend": "claude",
        "decision_source": "adjudicator_agent",
        "shadow": True,
        "role_run_id": "r4",
        "role_record_error": "down",
        "errors": ["e1"],
        "case": {"target": "o/r#5", "case_type": "runtime_ac"},
        "advisory_plan": {
            "decision": "reject_blocker",
            "confidence": "high",
            "rationale": "Evidence contradicts.",
            "evidence_assessment": [
                {
                    "status": "contradicted",
                    "evidence_ref": "runtime-ac:1",
                    "claim": "missing",
                    "reason": "It passed.",
                },
                {
                    "status": "insufficient",
                    "evidence_ref": None,
                    "claim": "unknown",
                    "reason": "No data.",
                },
            ],
            "ground_truth_refs": ["runtime-ac:1"],
            "evidence_gaps": ["Gap one"],
            "recommended_next_step": "Inspect runtime-ac:1.",
        },
    }
    assert roles.format_adjudicator_human(full) == "\n".join(
        [
            "role=adjudicator backend=claude decision_source=adjudicator_agent shadow=True",
            "role_run_id=r4",
            "role_record_error=down",
            "errors: e1",
            "target=o/r#5 case_type=runtime_ac",
            "",
            "--- adjudication ---",
            "decision=reject_blocker confidence=high",
            "Evidence contradicts.",
            "",
            "--- evidence_assessment ---",
            "- contradicted [runtime-ac:1]: missing - It passed.",
            "- insufficient: unknown - No data.",
            "",
            "--- ground_truth_refs ---",
            "- runtime-ac:1",
            "",
            "--- evidence_gaps ---",
            "- Gap one",
            "",
            "--- next_step ---",
            "Inspect runtime-ac:1.",
            "",
            shadow,
        ]
    )
    assert roles.format_adjudicator_human({}) == "\n".join(
        [
            "role=None backend=- decision_source=None shadow=None",
            "target=- case_type=-",
            "",
            "--- adjudication ---",
            "decision=None confidence=None",
            "",
            shadow,
        ]
    )


# --------------------------------------------------------------------------------------
# Input readers and the command line
# --------------------------------------------------------------------------------------
def _write_json(directory, name, payload):
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_read_backlog_items_names_a_missing_default_artifact_and_reads_it_when_present(
    monkeypatch, tmp_path
):
    default = tmp_path / "handoff" / "backlog.json"
    monkeypatch.setattr(roles.backlog_mod, "BACKLOG_JSON", default)
    with pytest.raises(ValueError) as missing:
        roles._read_backlog_items("")
    assert str(missing.value) == (
        f"no --backlog-json given and the default artifact is absent: {default}. "
        "Run `python3 src/backlog.py --live` to produce it, or pass --backlog-json."
    )
    default.parent.mkdir()
    default.write_text(json.dumps([{"target": "o/r#1"}]), encoding="utf-8")
    assert roles._read_backlog_items("") == [{"target": "o/r#1"}]


def test_read_backlog_items_accepts_a_list_an_items_or_backlog_object_or_stdin(
    monkeypatch, tmp_path
):
    items = [{"target": "o/r#1"}]
    assert roles._read_backlog_items(_write_json(tmp_path, "list.json", items)) == items
    both = {"items": items, "backlog": [{"target": "o/r#9"}]}
    assert roles._read_backlog_items(_write_json(tmp_path, "both.json", both)) == items
    only_backlog = {"backlog": items}
    assert roles._read_backlog_items(_write_json(tmp_path, "backlog.json", only_backlog)) == items
    shadowed = {"items": "not a list", "backlog": items}
    assert roles._read_backlog_items(_write_json(tmp_path, "shadowed.json", shadowed)) == items
    message = r"^backlog JSON must be a list or an object with an items/backlog list$"
    for payload in ({"items": "not a list"}, {"other": items}, "text", 5):
        with pytest.raises(ValueError, match=message):
            roles._read_backlog_items(_write_json(tmp_path, "bad.json", payload))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"items": items})))
    assert roles._read_backlog_items("-") == items


def test_read_case_json_reads_files_and_stdin_and_demands_an_object(monkeypatch, tmp_path):
    assert roles._read_case_json(_write_json(tmp_path, "case.json", CASE)) == CASE
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"target": "from-stdin"})))
    assert roles._read_case_json("-") == {"target": "from-stdin"}
    with pytest.raises(ValueError, match=r"^case JSON must be an object$"):
        roles._read_case_json(_write_json(tmp_path, "list.json", [1, 2]))
    monkeypatch.setattr(sys, "stdin", io.StringIO('"text"'))
    with pytest.raises(ValueError, match=r"^case JSON must be an object$"):
        roles._read_case_json("-")


def test_cli_route_prints_the_routed_backend_as_text_or_json(monkeypatch, capsys):
    route = Spy(result={"agent": "codex", "mode": "full"})
    monkeypatch.setattr(roles, "route_role", route)
    assert roles.main(["route", "--role", "triage", "--high-leverage"]) == 0
    assert capsys.readouterr().out == "role=triage backend=codex\n"
    assert roles.main(["route", "--json"]) == 0
    assert (
        capsys.readouterr().out == json.dumps({"agent": "codex", "mode": "full"}, indent=2) + "\n"
    )
    assert route.calls == [
        (("triage",), {"high_leverage": True}),
        (("redirect",), {"high_leverage": False}),
    ]
    route.result = None
    assert roles.main(["route"]) == 0
    assert capsys.readouterr().out == "role=redirect backend=(none available)\n"
    assert roles.main(["route", "--json"]) == 0
    assert capsys.readouterr().out == "null\n"


def test_cli_redirect_reads_the_report_history_and_proposal_files(monkeypatch, tmp_path, capsys):
    report = {"target": "o/r#1", "state": "stalled"}
    history = [{"state": "stalled"}]
    proposal = {"action": "wait"}
    runner = Spy(result={"role": "redirect", "backend": "codex"})
    monkeypatch.setattr(roles, "run_redirect_agent", runner)
    monkeypatch.setattr(roles, "format_human", Spy(result="HUMAN TEXT"))
    code = roles.main(
        [
            "redirect",
            "--report-json",
            _write_json(tmp_path, "report.json", report),
            "--ac",
            "AC text",
            "--attempt-history-json",
            _write_json(tmp_path, "history.json", history),
            "--backend",
            "codex",
            "--dispatch",
            "--proposal-json",
            _write_json(tmp_path, "proposal.json", proposal),
            "--lane",
            "opener",
            "--task-type",
            "implement",
            "--next-agent",
            "cursor",
            "--high-leverage",
            "--record-corpus",
            "--corpus",
            "/corpus/shadow.jsonl",
        ]
    )
    assert code == 0 and capsys.readouterr().out == "HUMAN TEXT\n"
    assert runner.calls == [
        (
            (report, "AC text"),
            {
                "attempt_history": history,
                "backend": "codex",
                "dispatch": True,
                "proposal_json": proposal,
                "high_leverage": True,
                "lane": "opener",
                "task_type": "implement",
                "next_agent": "cursor",
                "record_corpus": True,
                "corpus_path": "/corpus/shadow.jsonl",
            },
        )
    ]


def test_cli_redirect_defaults_read_stdin_and_json_prints_the_result(monkeypatch, capsys):
    report = {"target": "o/r#2", "state": "running"}
    result = {"role": "redirect", "backend": None}
    runner = Spy(result=result)
    monkeypatch.setattr(roles, "run_redirect_agent", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(report)))
    assert roles.main(["redirect", "--json"]) == 0
    assert capsys.readouterr().out == json.dumps(result, indent=2) + "\n"
    assert runner.calls == [
        (
            (report, ""),
            {
                "attempt_history": None,
                "backend": None,
                "dispatch": False,
                "proposal_json": None,
                "high_leverage": False,
                "lane": None,
                "task_type": None,
                "next_agent": None,
                "record_corpus": False,
                "corpus_path": None,
            },
        )
    ]


def _usage_error(capsys, argv):
    with pytest.raises(SystemExit) as raised:
        roles.main(argv)
    assert raised.value.code == 2
    return capsys.readouterr().err.rstrip().splitlines()[-1]


@pytest.mark.parametrize(
    "flags",
    [
        ["--target", "t"],
        ["--goal", "g"],
        ["--proposal-json", "proposal.json"],
        ["--context-file", "context.txt"],
        ["--context", "inline"],
    ],
)
def test_cli_prompt_batch_refuses_single_item_flags(capsys, tmp_path, flags):
    batch = _write_json(tmp_path, "batch.json", [{"target": "o/r#1", "goal": "g"}])
    last = _usage_error(capsys, ["prompt", "--batch", batch, *flags])
    assert last.endswith("error: --batch uses item context/target/goal/proposal_json from its JSON")


def test_cli_prompt_batch_turns_input_and_output_failures_into_usage_errors(
    monkeypatch, capsys, tmp_path
):
    empty = _write_json(tmp_path, "empty.json", [])
    assert _usage_error(capsys, ["prompt", "--batch", empty]).endswith(
        "error: batch must be a non-empty list of item objects"
    )
    absent = str(tmp_path / "absent.json")
    unreadable = _usage_error(capsys, ["prompt", "--batch", absent])
    assert unreadable.endswith(f"error: [Errno 2] No such file or directory: '{absent}'")

    result = {"batch_id": "prompt-batch:abc", "output": "dispatch_prompt", "items": []}
    monkeypatch.setattr(roles, "run_prompt_batch", Spy(result=result))
    writer = Spy(error=OSError("disk full"))
    monkeypatch.setattr(roles, "write_prompt_batch", writer)
    batch = _write_json(tmp_path, "batch.json", [{"target": "o/r#1", "goal": "g"}])
    argv = ["prompt", "--batch", batch, "--cwd", str(tmp_path)]
    assert _usage_error(capsys, argv).endswith("error: disk full")
    assert writer.calls == [((result, tmp_path / "prompt-batch-abc"), {})]


@pytest.mark.parametrize("flags", [[], ["--target", "o/r#1"], ["--goal", "g"]])
def test_cli_prompt_needs_both_a_target_and_a_goal_unless_it_is_a_batch(capsys, flags):
    assert _usage_error(capsys, ["prompt", *flags]).endswith(
        "error: prompt requires --target and --goal, or --batch"
    )


def test_cli_decompose_passes_flags_and_file_context_through(monkeypatch, tmp_path, capsys):
    plan = {"epic": {"title": "T"}}
    runner = Spy(result={"role": "decomposer"})
    monkeypatch.setattr(roles, "run_decomposer_agent", runner)
    monkeypatch.setattr(roles, "format_decomposer_human", Spy(result="DECOMPOSE TEXT"))
    context_file = tmp_path / "context.txt"
    context_file.write_text("FILE CONTEXT", encoding="utf-8")
    argv = [
        "decompose",
        "--goal",
        "G",
        "--repo",
        "o/r",
        "--target",
        "o/r#2",
        "--context",
        "INLINE",
        "--context-file",
        str(context_file),
        "--subtask-count",
        "3",
        "--backend",
        "codex",
        "--dispatch",
        "--proposal-json",
        _write_json(tmp_path, "plan.json", plan),
        "--cwd",
        "/work",
        "--timeout",
        "42",
        "--high-leverage",
    ]
    assert roles.main(argv) == 0 and capsys.readouterr().out == "DECOMPOSE TEXT\n"
    assert (
        roles.main(["decompose", "--goal", "G", "--context-file", str(context_file), "--json"]) == 0
    )
    assert capsys.readouterr().out == json.dumps({"role": "decomposer"}, indent=2) + "\n"
    assert runner.calls == [
        (
            (),
            {
                "goal": "G",
                "repo": "o/r",
                "target": "o/r#2",
                "context": "INLINE\n\nFILE CONTEXT",
                "subtask_count": 3,
                "backend": "codex",
                "dispatch": True,
                "proposal_json": plan,
                "high_leverage": True,
                "cwd": "/work",
                "timeout": 42,
            },
        ),
        (
            (),
            {
                "goal": "G",
                "repo": "",
                "target": "",
                "context": "FILE CONTEXT",
                "subtask_count": None,
                "backend": None,
                "dispatch": False,
                "proposal_json": None,
                "high_leverage": False,
                "cwd": ".",
                "timeout": 600,
            },
        ),
    ]


def test_cli_triage_reads_the_backlog_and_passes_flags_through(monkeypatch, tmp_path, capsys):
    items = [{"target": "o/r#1"}, {"target": "o/r#2"}]
    proposal = _valid_triage("o/r#1", "o/r#2")
    runner = Spy(result={"role": "triage"})
    monkeypatch.setattr(roles, "run_triage_agent", runner)
    monkeypatch.setattr(roles, "format_triage_human", Spy(result="TRIAGE TEXT"))
    context_file = tmp_path / "context.txt"
    context_file.write_text("FILE CONTEXT", encoding="utf-8")
    argv = [
        "triage",
        "--backlog-json",
        _write_json(tmp_path, "backlog.json", {"items": items}),
        "--context",
        "INLINE",
        "--context-file",
        str(context_file),
        "--max-items",
        "5",
        "--backend",
        "cursor",
        "--dispatch",
        "--proposal-json",
        _write_json(tmp_path, "proposal.json", proposal),
        "--cwd",
        "/work",
        "--timeout",
        "42",
        "--high-leverage",
    ]
    assert roles.main(argv) == 0 and capsys.readouterr().out == "TRIAGE TEXT\n"
    backlog_file = _write_json(tmp_path, "backlog.json", items)
    assert roles.main(["triage", "--backlog-json", backlog_file, "--json"]) == 0
    assert capsys.readouterr().out == json.dumps({"role": "triage"}, indent=2) + "\n"
    assert runner.calls == [
        (
            (),
            {
                "backlog_items": items,
                "context": "INLINE\n\nFILE CONTEXT",
                "max_items": 5,
                "backend": "cursor",
                "dispatch": True,
                "proposal_json": proposal,
                "high_leverage": True,
                "cwd": "/work",
                "timeout": 42,
            },
        ),
        (
            (),
            {
                "backlog_items": items,
                "context": "",
                "max_items": 20,
                "backend": None,
                "dispatch": False,
                "proposal_json": None,
                "high_leverage": False,
                "cwd": ".",
                "timeout": 600,
            },
        ),
    ]


def test_cli_adjudicate_reads_the_case_and_passes_flags_through(monkeypatch, tmp_path, capsys):
    proposal = _valid_adjudication()
    runner = Spy(result={"role": "adjudicator"})
    monkeypatch.setattr(roles, "run_adjudicator_agent", runner)
    monkeypatch.setattr(roles, "format_adjudicator_human", Spy(result="ADJUDICATE TEXT"))
    context_file = tmp_path / "context.txt"
    context_file.write_text("FILE CONTEXT", encoding="utf-8")
    case_file = _write_json(tmp_path, "case.json", CASE)
    argv = [
        "adjudicate",
        "--case-json",
        case_file,
        "--context",
        "INLINE",
        "--context-file",
        str(context_file),
        "--backend",
        "gemini",
        "--dispatch",
        "--proposal-json",
        _write_json(tmp_path, "proposal.json", proposal),
        "--cwd",
        "/work",
        "--timeout",
        "42",
        "--high-leverage",
    ]
    assert roles.main(argv) == 0 and capsys.readouterr().out == "ADJUDICATE TEXT\n"
    assert roles.main(["adjudicate", "--case-json", case_file, "--json"]) == 0
    assert capsys.readouterr().out == json.dumps({"role": "adjudicator"}, indent=2) + "\n"
    assert runner.calls == [
        (
            (),
            {
                "case": CASE,
                "context": "INLINE\n\nFILE CONTEXT",
                "backend": "gemini",
                "dispatch": True,
                "proposal_json": proposal,
                "high_leverage": True,
                "cwd": "/work",
                "timeout": 42,
            },
        ),
        (
            (),
            {
                "case": CASE,
                "context": "",
                "backend": None,
                "dispatch": False,
                "proposal_json": None,
                "high_leverage": False,
                "cwd": ".",
                "timeout": 600,
            },
        ),
    ]


def test_cli_link_outcome_records_the_link_and_credits_the_role_capability_once_synced(
    monkeypatch, capsys
):
    joined = {"synced": True, "task_type": "role:triage", "linked": True}
    join = Spy(result=joined)
    monkeypatch.setattr(feedback, "join_role_to_outcome", join)
    events = Spy()
    monkeypatch.setattr(roles, "_role_capability_event", events)
    argv = ["link-outcome", "--role-run-id", "role:triage:cursor:1", "--influenced-run-id", "run-9"]
    assert roles.main(argv) == 0
    assert capsys.readouterr().out == f"{joined}\n"
    assert join.calls == [
        (("role:triage:cursor:1", "run-9"), {"accepted": True, "notes": None}),
    ]
    assert events.calls == [
        (
            ("triage", "outcome"),
            {"ref": "run-9", "metadata": {"role_run_id": "role:triage:cursor:1"}},
        )
    ]
    events.calls.clear()
    assert roles.main([*argv, "--not-accepted", "--notes", "wrong lane", "--json"]) == 0
    assert capsys.readouterr().out == json.dumps(joined, indent=2) + "\n"
    assert join.calls[-1] == (
        ("role:triage:cursor:1", "run-9"),
        {"accepted": False, "notes": "wrong lane"},
    )
    assert len(events.calls) == 1


@pytest.mark.parametrize(
    "joined",
    [
        {"synced": False, "task_type": "role:triage"},
        {"synced": True, "task_type": "role:mystery"},
        {"synced": True, "task_type": "implement"},
        {"synced": True, "task_type": None},
    ],
)
def test_cli_link_outcome_credits_no_capability_for_an_unsynced_or_unknown_role(
    monkeypatch, capsys, joined
):
    monkeypatch.setattr(feedback, "join_role_to_outcome", Spy(result=joined))
    events = Spy()
    monkeypatch.setattr(roles, "_role_capability_event", events)
    argv = ["link-outcome", "--role-run-id", "role-run", "--influenced-run-id", "run-9"]
    assert roles.main(argv) == 0
    assert capsys.readouterr().out == f"{joined}\n"
    assert events.calls == []


def test_cli_summarize_proposals_prints_the_corpus_summary_as_text_or_json(monkeypatch, capsys):
    summary = {
        "n": 7,
        "disagreement_rate": 0.25,
        "disagreements": 2,
        "proposal_action_distribution": {"wait": 3},
        "baseline_action_distribution": {"inspect": 4},
        "backend_distribution": {"codex": 7},
        "synced_role_outcomes": 1,
        "ready_for_supervised_apply": False,
    }
    summarize = Spy(result=summary)
    monkeypatch.setattr(redirect_shadow, "summarize", summarize)
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", Path("/default/shadow.jsonl"))
    assert roles.main(["summarize-proposals", "--corpus", "/corpus/other.jsonl"]) == 0
    assert capsys.readouterr().out == (
        "RedirectAgent shadow corpus: 7 proposals\n"
        "  Disagreement rate: 0.25 (2 disagreed)\n"
        "  Proposal actions: {'wait': 3}\n"
        "  Baseline actions: {'inspect': 4}\n"
        "  Backends: {'codex': 7}\n"
        "  Synced role outcomes: 1\n"
        "  Ready for supervised apply: False\n"
    )
    assert roles.main(["summarize-proposals", "--json"]) == 0
    assert capsys.readouterr().out == json.dumps(summary, indent=2) + "\n"
    assert summarize.calls == [
        ((Path("/corpus/other.jsonl"),), {}),
        ((Path("/default/shadow.jsonl"),), {}),
    ]


# The selftest opens Brain connections that feedback.py never closes (it relies on collection);
# collecting them here keeps that module's ResourceWarning out of the tests that run after this one.
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_selftest_fails_loudly_when_a_changed_collector_input_would_be_admitted(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(roles, "_collected_snapshot", lambda case: {})
    with pytest.raises(AssertionError, match=r"^changed collector input was admitted$"):
        roles._selftest()
    assert feedback.DB_PATH == tmp_path / "runtime" / "brain.db"
    gc.collect()


def test_main_returns_zero_without_running_anything_for_a_command_it_has_no_branch_for(
    monkeypatch,
):
    def unknown_command(self, args=None, namespace=None):
        return argparse.Namespace(cmd="bogus")

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", unknown_command)
    forbidden = Spy(error=AssertionError("an unrecognised command must not run anything"))
    for name in (
        "route_role",
        "run_redirect_agent",
        "run_prompt_agent",
        "run_prompt_batch",
        "run_decomposer_agent",
        "run_triage_agent",
        "run_adjudicator_agent",
    ):
        monkeypatch.setattr(roles, name, forbidden)
    monkeypatch.setattr(feedback, "join_role_to_outcome", forbidden)
    monkeypatch.setattr(redirect_shadow, "summarize", forbidden)
    assert roles.main(["bogus"]) == 0
    assert forbidden.calls == []
