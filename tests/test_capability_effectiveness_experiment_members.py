"""An edge onto an abcd experiment MEMBER run has no outcome path, and the report says so.

THE DEFECT (2026-10-12). `exp_abcd` records each member run with `capability_ids=
["abcd-experiment"]` and never writes its `outcomes` row: a member commits on an isolated branch,
never pushes, and is judged in `evaluations_v2`. So `capability_effectiveness` listed
abcd-experiment's 12 edges under "awaiting outcome resolution" for six weeks, a wait nothing could
end. Measured the same day on a copy of the live Brain and the live mirror's experiments: 15 member
runs, 0 outcomes; 257 synthesis promotions, none past `candidate_ready`; no `exp/*` branch ever
became a PR in the seven repos the experiments ran on. A winner's delivery, when there is one, is
credited to its SYNTHESIS run by `synthesis_promotion.default_mirror_outcome`.

THE RULE THIS PINS. Such an edge is `no_outcome_path`: counted, never scored, and never awaiting. A
row some other writer puts on a member run does not score it either, because that is not the
member's delivery and crediting a non-winning arm is the error. A synthesis run, which carries the
base member's identity too, is not a member run.

Member runs are written as `exp_abcd.prepare_arms` writes them: its run id, its routing metadata,
`feedback.record_run` with the capability."""

from __future__ import annotations

import time

import pytest

import capabilities
import capability_effectiveness as effectiveness
import exp_abcd
import feedback

T0 = 1_791_800_000  # 2026-10-12
VERSIONS = {"abcd-experiment": "cv:abcd", "offload": "cv:offload"}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        feedback,
        "_resolve_capability_versions",
        lambda ids: [VERSIONS[i] for i in ids] if all(i in VERSIONS for i in ids) else [],
    )
    monkeypatch.setattr(time, "time", lambda: float(T0))
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for cap_id in VERSIONS:
        rows[cap_id] = capabilities._blank_capability(cap_id)
        rows[cap_id]["status"] = "shadow"
    capabilities.save(rows, ledger)
    return ledger


def _member(exp_id: str, arm_id: str, agent: str) -> str:
    """One member run, recorded with the arguments `exp_abcd.prepare_arms` passes."""
    member = {
        "arm_id": arm_id,
        "agent": agent,
        "member_id": exp_abcd.member_identity(arm_id, agent, 0),
        "profile_id": None,
        "strategy": "single",
        "legacy": False,
    }
    run_id = exp_abcd._member_run_id(exp_id, member)
    feedback.record_run(
        run_id,
        f"o/r [exp {exp_id} arm {arm_id} member {member['member_id']}]",
        "implement",
        agent,
        mode="full",
        experiment_id=exp_id,
        capability_ids=["abcd-experiment"],
        influenced_by_workflow_ids=["exp_abcd"],
        routing_metadata=exp_abcd._member_routing_metadata(member),
    )
    return run_id


def _row(ledger) -> dict:
    rep = effectiveness.measure(path=ledger)
    return rep, next(r for r in rep["rows"] if r["capability_id"] == "abcd-experiment")


def test_member_edges_have_no_outcome_path_and_never_await(brain):
    for arm, agent in (("arm-codex", "codex"), ("arm-cursor", "cursor"), ("arm-gemini", "gemini")):
        _member("exp1", arm, agent)
    rep, row = _row(brain)
    arm = row["accepted"]
    assert row["attributed_edges"] == 3, row
    assert (arm["no_outcome_path"], arm["terminal"], arm["durable"]) == (3, 0, 0), arm
    assert arm["status"].startswith("unscoreable (3 edges"), arm["status"]
    assert "3 onto experiment member runs, no outcome path" in arm["status"], arm["status"]
    assert "abcd-experiment" not in rep["awaiting_outcomes"], rep["awaiting_outcomes"]
    assert "abcd-experiment" in rep["unscoreable"], rep["unscoreable"]
    assert "3 no-path" in effectiveness.format_report(rep)


def test_an_outcome_another_writer_put_on_a_member_run_is_not_scored(brain):
    run = _member("exp2", "arm-cursor", "cursor")
    feedback.record_outcome(
        run, verifier_verdict="PASS", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    _, row = _row(brain)
    arm = row["accepted"]
    assert (arm["no_outcome_path"], arm["terminal"], arm["durable"]) == (1, 0, 0), arm
    assert arm["durable_rate"] is None, arm


def test_an_ordinary_run_beside_member_runs_still_awaits(brain):
    _member("exp3", "arm-codex", "codex")
    feedback.record_run(
        "remote:o/r#7:codex",
        "o/r#7",
        "implement",
        "codex",
        mode="remote",
        pr_number=7,
        capability_ids=["abcd-experiment"],
    )
    rep, row = _row(brain)
    arm = row["accepted"]
    assert (arm["no_outcome_path"], arm["attributed"]) == (1, 2), arm
    assert arm["status"] == "not_yet_measurable", arm["status"]
    assert "abcd-experiment" in rep["awaiting_outcomes"]


@pytest.mark.parametrize(
    ("metadata", "member"),
    (
        (exp_abcd._member_routing_metadata({"arm_id": "a", "member_id": "a--m"}), True),
        (
            {
                **exp_abcd._member_routing_metadata({"arm_id": "a", "member_id": "a--m"}),
                "synthesis_base_member_id": "a--m",
            },
            False,
        ),
        ({"promotion_resume": True, "root_synthesis_run_id": "x:synth:a"}, False),
        ({}, False),
        (None, False),
        ('{"experiment_identity_version": 2, "experiment_member_id": "a--m"}', True),
    ),
)
def test_only_member_runs_are_member_targets(metadata, member):
    assert feedback.experiment_member_target(metadata) is member


def test_the_value_chain_never_counts_a_member_run_as_acted_on(brain):
    """The value-chain monitor reads the same edges with the same classifier, from its own read."""
    import value_chain_monitor as vcm

    for arm, agent in (("arm-codex", "codex"), ("arm-cursor", "cursor")):
        _member("exp4", arm, agent)
    edges = vcm._brain(feedback.DB_PATH, 0, T0 + 1)["edges"]
    own = [e for e in edges if e["capability_id"] == "abcd-experiment"]
    counts = vcm.outcome_counts("abcd-experiment", own)
    assert (counts["acted_on"], counts["acted_on_member_runs"], counts["outcome_open"]) == (0, 2, 0)
