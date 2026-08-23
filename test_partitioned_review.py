from __future__ import annotations

import json
from pathlib import Path

import pytest

import dispatcher
import partitioned_review as review


def _corpus(items: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "review_id": "tmp-feedback-reconciliation",
        "objective": "Reconcile historical feedback against the current implementation.",
        "shared_context": ["Matching names are discovery hints, not defects."],
        "items": items,
    }


def _item(item_id: str, group: str, *, assertion_key: str | None = None) -> dict:
    return {
        "item_id": item_id,
        "assertion_key": assertion_key or item_id,
        "group_key": group,
        "assertion": f"Determine the current disposition of {item_id}.",
        "source_refs": [{"kind": "pull_request", "ref": f"owner/repo#{group.rsplit('-', 1)[-1]}"}],
    }


def _result(plan: dict, partition: dict, categories_by_item: dict[str, str] | None = None) -> dict:
    categories = review._empty_categories()
    categories_by_item = categories_by_item or {}
    for item in partition["items"]:
        category = categories_by_item.get(item["item_id"], "removed_product_surfaces")
        disposition = {
            "intentional_adapters": "intentional",
            "unresolved_design_dispositions": "unresolved",
            "confirmed_defects": "remaining",
        }.get(category, "satisfied")
        categories[category].append(
            {
                "item_id": item["item_id"],
                "assertion_key": item["assertion_key"],
                "disposition": disposition,
                "summary": f"Evidence-led disposition for {item['item_id']}.",
                "evidence": [
                    {
                        "type": "current_code",
                        "ref": "src/current.py:10",
                        "observation": "The current implementation was inspected.",
                    }
                ],
                "confidence": "high",
                "recommended_action": None,
            }
        )
    return {
        "schema_version": 1,
        "review_id": plan["review_id"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "categories": categories,
        "summary": "Partition complete.",
    }


def _success_offload(output: dict, index: int) -> dict:
    return {
        "agent": "gemini",
        "model": "gemini-test",
        "run_id": f"offload:gemini:{index}",
        "log": f"/tmp/offload-{index}.log",
        "exit": 0,
        "attempts": 1,
        "output": json.dumps(output),
    }


def _codex_event_stream(output: dict) -> str:
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "test-thread"},
            {
                "type": "item.started",
                "item": {"type": "agent_message", "text": ""},
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Reviewing the partition."},
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(output)},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
        )
    )


def test_strict_json_parser_extracts_final_codex_event_message() -> None:
    expected = {"schema_version": 1, "status": "complete"}

    assert review._parse_json_object(_codex_event_stream(expected)) == expected

    single_event = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(expected)},
        }
    )
    assert review._parse_json_object(single_event) == expected

    with pytest.raises(json.JSONDecodeError):
        review._parse_json_object('{"status":"complete"}\nnot-an-event')
    with pytest.raises(json.JSONDecodeError):
        review._parse_json_object(_codex_event_stream(expected) + "\ntrailing prose")
    with pytest.raises(json.JSONDecodeError):
        review._parse_json_object(_codex_event_stream(expected) + "\n{")
    with pytest.raises(json.JSONDecodeError):
        review._parse_json_object(_codex_event_stream(expected) + "\n[]")

    non_object_message = json.dumps(
        {"type": "agent_message", "text": json.dumps(["not", "an", "object"])}
    )
    with pytest.raises(ValueError, match="one strict JSON object"):
        review._parse_json_object(non_object_message)


def test_partition_prompt_explains_category_disposition_constraints() -> None:
    plan = review.partition_corpus(
        _corpus([_item("CW-1", "source-pr-61")]), max_prompt_chars=12_000
    )

    prompt = review.build_partition_prompt(plan, plan["partitions"][0])

    assert "intentional_adapters item uses disposition intentional" in prompt
    assert "unresolved_design_dispositions item uses disposition unresolved" in prompt
    assert "stale or incomplete ledger" in prompt


def test_source_pr_groups_are_bounded_by_item_and_prompt_limits() -> None:
    corpus = _corpus(
        [
            _item("CW-61-1", "source-pr-61"),
            _item("CW-61-2", "source-pr-61"),
            _item("CW-61-3", "source-pr-61"),
            _item("CW-62-1", "source-pr-62"),
            _item("CW-62-2", "source-pr-62"),
        ]
    )
    plan = review.partition_corpus(corpus, max_items=2, max_prompt_chars=12_000)

    assert [partition["group_key"] for partition in plan["partitions"]] == [
        "source-pr-61",
        "source-pr-61",
        "source-pr-62",
    ]
    assert [len(partition["items"]) for partition in plan["partitions"]] == [2, 1, 2]
    assert not review.validate_plan(plan)
    assert all(
        len(review.build_partition_prompt(plan, partition)) <= 12_000
        for partition in plan["partitions"]
    )

    one_item_partitions = review.partition_corpus(
        _corpus(
            [
                _item("CW-63-1", "source-pr-63"),
                _item("CW-63-2", "source-pr-63"),
            ]
        ),
        max_items=1,
        max_prompt_chars=12_000,
    )
    assert [len(partition["items"]) for partition in one_item_partitions["partitions"]] == [1, 1]
    duplicate_after_normalization = _corpus(
        [
            _item("CW-space", "source-pr-63"),
            _item(" CW-space ", "source-pr-63"),
        ]
    )
    with pytest.raises(ValueError, match="duplicates"):
        review.partition_corpus(duplicate_after_normalization, max_prompt_chars=12_000)


def test_fixed_schema_rejects_raw_name_scan_false_positive() -> None:
    plan = review.partition_corpus(
        _corpus([_item("CW-1", "source-pr-61")]), max_prompt_chars=12_000
    )
    partition = plan["partitions"][0]
    result = _result(plan, partition, {"CW-1": "confirmed_defects"})
    finding = result["categories"]["confirmed_defects"][0]
    finding["evidence"] = [
        {
            "type": "name_scan",
            "ref": "rg output",
            "observation": "The old name still appears somewhere.",
        }
    ]

    errors = review.validate_partition_result(result, plan, partition)

    assert any("raw name scans" in error for error in errors)
    finding["evidence"].append(
        {
            "type": "runtime",
            "ref": "pytest tests/test_surface.py",
            "observation": "The current behavior reproduces the defect.",
        }
    )
    assert not review.validate_partition_result(result, plan, partition)
    missing_category = json.loads(json.dumps(result))
    del missing_category["categories"]["historical_negative_assertions"]
    assert any(
        "must contain exactly" in error
        for error in review.validate_partition_result(missing_category, plan, partition)
    )


def test_timeout_provenance_is_retained_and_synthesis_fails_closed(tmp_path: Path) -> None:
    plan = review.partition_corpus(
        _corpus(
            [
                _item("CW-61", "source-pr-61"),
                _item("CW-62", "source-pr-62"),
            ]
        ),
        max_items=1,
        max_prompt_chars=12_000,
    )
    calls = 0

    def fake_offload(agent, prompt, **kwargs):
        nonlocal calls
        partition = plan["partitions"][calls]
        calls += 1
        if calls == 1:
            return _success_offload(
                _result(
                    plan,
                    partition,
                    {partition["items"][0]["item_id"]: "unresolved_design_dispositions"},
                ),
                calls,
            )
        return {
            "agent": agent,
            "model": "gemini-test",
            "run_id": "offload:gemini:timeout",
            "log": "/tmp/offload-timeout.log",
            "exit": 124,
            "attempts": 1,
            "output": "",
            "error": "timed out after 30s",
        }

    run = review.run_plan(
        plan,
        agent="gemini",
        cwd=tmp_path,
        results_dir=tmp_path / "results",
        timeout=30,
        offload_fn=fake_offload,
    )
    synthesis = review.synthesize_results(plan, results_dir=tmp_path / "results")
    timeout_envelope = json.loads(
        review._partition_path(
            tmp_path / "results", plan["partitions"][1]["partition_id"]
        ).read_text()
    )

    assert run["coverage_status"] == "incomplete"
    assert synthesis["coverage_status"] == "incomplete"
    assert synthesis["verdict"] == "INCOMPLETE"
    assert timeout_envelope["status"] == "failed"
    assert timeout_envelope["failure_reason"] == "timed out after 30s"
    assert timeout_envelope["provenance"]["run_id"] == "offload:gemini:timeout"
    assert timeout_envelope["provenance"]["timeout_s"] == 30
    assert timeout_envelope["provenance"]["source_refs"]

    def should_not_adjudicate(*_args, **_kwargs):
        raise AssertionError("an incomplete corpus must not spend an adjudication offload")

    incomplete_with_queue = review.synthesize_results(
        plan,
        results_dir=tmp_path / "results",
        adjudicator_agent="cursor",
        offload_fn=should_not_adjudicate,
    )
    assert incomplete_with_queue["adjudication"]["status"] == "needed"
    assert incomplete_with_queue["adjudicator"]["status"] == "skipped_incomplete_coverage"
    (tmp_path / "timeout-plan.json").write_text(json.dumps(plan))
    assert (
        review.main(
            [
                "synthesize",
                "--plan",
                str(tmp_path / "timeout-plan.json"),
                "--results-dir",
                str(tmp_path / "results"),
                "--output",
                str(tmp_path / "incomplete-synthesis.json"),
            ]
        )
        == 1
    )


def test_synthesis_flags_cross_partition_conflict_and_adjudicates_advisory(
    tmp_path: Path,
) -> None:
    plan = review.partition_corpus(
        _corpus(
            [
                _item("adapter-a", "source-pr-61", assertion_key="json-adapter"),
                _item("adapter-b", "source-pr-62", assertion_key="json-adapter"),
            ]
        ),
        max_items=1,
        max_prompt_chars=12_000,
    )
    partition_outputs = [
        _result(plan, plan["partitions"][0], {"adapter-a": "intentional_adapters"}),
        _result(plan, plan["partitions"][1], {"adapter-b": "confirmed_defects"}),
    ]
    calls = 0

    def run_offload(_agent, _prompt, **_kwargs):
        nonlocal calls
        output = partition_outputs[calls]
        calls += 1
        return _success_offload(output, calls)

    run = review.run_plan(
        plan,
        agent="gemini",
        cwd=tmp_path,
        results_dir=tmp_path / "results",
        offload_fn=run_offload,
    )
    assert run["coverage_status"] == "complete"

    def adjudicator_offload(_agent, _prompt, **_kwargs):
        return _success_offload(
            {
                "schema_version": 1,
                "review_id": plan["review_id"],
                "plan_sha256": plan["plan_sha256"],
                "decisions": [
                    {
                        "assertion_key": "json-adapter",
                        "decision": "reject_false_positive",
                        "rationale": "Caller-specific shapes make the adapter intentional.",
                        "evidence_refs": ["src/adapter.py:10"],
                    }
                ],
            },
            99,
        )

    synthesis = review.synthesize_results(
        plan,
        results_dir=tmp_path / "results",
        adjudicator_agent="cursor",
        cwd=tmp_path,
        offload_fn=adjudicator_offload,
    )

    assert synthesis["coverage_status"] == "complete"
    assert synthesis["verdict"] == "NEEDS_ADJUDICATION"
    assert synthesis["adjudication"]["conflicts"][0]["assertion_key"] == "json-adapter"
    assert synthesis["adjudicator"]["status"] == "complete"
    assert synthesis["adjudicator"]["provenance"]["run_id"] == "offload:gemini:99"
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    assert (
        review.main(
            [
                "synthesize",
                "--plan",
                str(tmp_path / "plan.json"),
                "--results-dir",
                str(tmp_path / "results"),
                "--output",
                str(tmp_path / "synthesis.json"),
            ]
        )
        == 1
    )


def test_stale_partition_digest_cannot_be_reused_or_synthesized_complete(
    tmp_path: Path,
) -> None:
    plan = review.partition_corpus(
        _corpus([_item("CW-stale", "source-pr-64")]),
        max_prompt_chars=12_000,
    )
    partition = plan["partitions"][0]
    calls = 0

    def fake_offload(_agent, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return _success_offload(_result(plan, partition), calls)

    results_dir = tmp_path / "results"
    first = review.run_plan(
        plan,
        agent="gemini",
        cwd=tmp_path,
        results_dir=results_dir,
        offload_fn=fake_offload,
    )
    assert first["coverage_status"] == "complete"
    envelope_path = review._partition_path(results_dir, partition["partition_id"])
    envelope = json.loads(envelope_path.read_text())
    envelope["schema_version"] = 2
    envelope_path.write_text(json.dumps(envelope))
    wrong_schema = review.synthesize_results(plan, results_dir=results_dir)
    assert wrong_schema["coverage_status"] == "incomplete"
    assert any(
        "schema_version" in error for error in wrong_schema["partition_statuses"][0]["errors"]
    )
    envelope["schema_version"] = 1
    envelope["partition_digest"] = "stale-digest"
    envelope_path.write_text(json.dumps(envelope))

    stale = review.synthesize_results(plan, results_dir=results_dir)
    assert stale["coverage_status"] == "incomplete"
    assert stale["verdict"] == "INCOMPLETE"
    assert any(
        "partition_digest mismatch" in error for error in stale["partition_statuses"][0]["errors"]
    )

    resumed = review.run_plan(
        plan,
        agent="gemini",
        cwd=tmp_path,
        results_dir=results_dir,
        offload_fn=fake_offload,
    )
    assert resumed["coverage_status"] == "complete"
    assert calls == 2, "a stale envelope must be re-run, not reused"
    (tmp_path / "complete-plan.json").write_text(json.dumps(plan))
    assert (
        review.main(
            [
                "synthesize",
                "--plan",
                str(tmp_path / "complete-plan.json"),
                "--results-dir",
                str(results_dir),
                "--output",
                str(tmp_path / "complete-synthesis.json"),
            ]
        )
        == 0
    )


def test_dispatcher_exposes_partitioned_review_selftest() -> None:
    assert dispatcher.main(["review-corpus", "--selftest"]) == 0


def test_review_round_is_registered_as_one_subject_and_stamped_on_every_partition(
    tmp_path: Path, monkeypatch
) -> None:
    """A corpus review fans one scope out to several agents — the same shape as a UX panel.

    Nothing bound those offloads together, so each partition was an unrelated run against an
    ephemeral temp path and thousands of offload runs across six agents produced nothing the
    learner could compare. The round id is what makes them ONE subject with a real arm set.

    Written because the existing test doubles take `**kwargs`: they would have swallowed
    `research_round` silently, so a binding that never happened would look identical to one that
    did — this repo's founding defect wearing a different hat.
    """
    import feedback
    import research_subjects

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    plan = review.partition_corpus(
        _corpus([_item("CW-71", "source-pr-71"), _item("CW-72", "source-pr-72")]),
        max_items=1,
        max_prompt_chars=12_000,
    )
    seen_rounds: list = []
    calls = 0

    def fake_offload(agent, prompt, **kwargs):
        nonlocal calls
        seen_rounds.append(kwargs.get("research_round"))
        partition = plan["partitions"][calls]
        calls += 1
        return _success_offload(
            _result(
                plan,
                partition,
                {partition["items"][0]["item_id"]: "unresolved_design_dispositions"},
            ),
            calls,
        )

    run = review.run_plan(
        plan,
        agent="gemini",
        cwd=tmp_path,
        results_dir=tmp_path / "results",
        timeout=30,
        offload_fn=fake_offload,
        round_agents=["gemini", "cursor", "codex"],
        round_date="2026-08-22",
    )

    info = run["research_round"]
    assert info["registered"] is True, info
    expected_target = research_subjects.domain_target(plan["review_id"])
    assert info["round_id"] == f"{expected_target}:review-corpus:2026-08-22", info
    # THE ARM SET IS THE AGENTS THAT DID THE WORK, in the order the round declared them.
    assert info["arms"] == ["gemini", "cursor", "codex"], info

    # Every partition offload carries the round, so the attempts join back to one subject.
    assert seen_rounds and all(r == info["round_id"] for r in seen_rounds), seen_rounds

    # The subject really landed, and it is ONE subject for the whole round.
    subject = research_subjects  # readability
    rows = (
        __import__("sqlite3")
        .connect(feedback.DB_PATH)
        .execute("SELECT subject_id, canonical_target, task_type, base_sha FROM research_subjects")
        .fetchall()
    )
    assert len(rows) == 1, rows
    assert rows[0][0] == info["subject_id"]
    assert rows[0][1] == expected_target
    # base_sha is INAPPLICABLE, not missing: a corpus review is not cut from a commit.
    assert rows[0][3] in (None, ""), rows[0]
    assert subject.is_domain_target(rows[0][1])

    # A ONE-AGENT ROUND IS ONE ARM. Padding it would make one opinion look like a panel's
    # agreement, so the default must never invent companions.
    solo = review.register_review_round(plan, ["gemini"], date="2026-08-23")
    assert solo["arms"] == ["gemini"], solo
    assert solo["subject_id"] != info["subject_id"], "arm set must change the subject identity"

    # Capture is SUBORDINATE to the review: a Brain failure is reported, never fatal, never silent.
    monkeypatch.setattr(
        research_subjects,
        "record_research_round",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("brain offline")),
    )
    broken = review.register_review_round(plan, ["gemini"], date="2026-08-24")
    assert broken["registered"] is False
    assert "brain offline" in broken["reason"], broken


def test_audit_round_inherits_downstream_durability_and_never_infers_it(tmp_path, monkeypatch):
    """B's durability half: the round inherits what the issues its findings produced achieved.

    Everything downstream already existed — an issue becomes a PR, a merged PR gets a durability
    label from `durability_sweep`, and `influence_edges` back-propagates it. The missing fact was
    "round R, arm A produced issue N", which CANNOT be inferred: the only written trace is a
    free-form prose line (`_Surfaced by the maint-69 outage investigated in #3007._`), and parsing
    a sentence into a causal edge attributes work to an agent on the strength of grammar.

    The label is un-gameable because the finding's author decides neither half: whether it is filed
    is the filer's call, and whether the fix HOLDS is decided later by real work landing on top.
    """
    import feedback
    import research_subjects

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    round_id, identity = research_subjects.record_research_round(
        research_subjects.domain_target("audit-x"),
        "review-corpus",
        "2026-08-22",
        "the objective",
        ["codex", "cursor"],
        task_type="review",
    )

    # The round's own arm runs, bound by experiment_id exactly as offload binds them.
    for agent in ("codex", "cursor"):
        feedback.record_run(
            f"round:{agent}", f"offload:/tmp/{agent}", "review", agent, experiment_id=round_id
        )

    # Two issues the findings produced. One landed durably, one was abandoned, one never landed.
    for target, durability in (("stranske/Repo#11", "durable"), ("stranske/Repo#12", "abandoned")):
        feedback.record_run(f"impl{target[-2:]}", target, "implement", "claude")
        feedback.record_outcome(
            f"impl{target[-2:]}", verifier_verdict="PASS", merged=True, durability=durability
        )
    research_subjects.record_finding_issue(
        round_id, "stranske/Repo#11", arm="codex", identity=identity
    )
    research_subjects.record_finding_issue(
        round_id, "stranske/Repo#12", arm="cursor", identity=identity
    )
    research_subjects.record_finding_issue(
        round_id, "stranske/Repo#99", arm="codex", identity=identity
    )  # never implemented

    got = research_subjects.resolve_round_durability(round_id, apply_edges=True)
    assert got["findings_filed"] == 3, got
    # INHERITED, not judged: each arm carries what its own issue actually achieved.
    assert got["per_arm_durability"]["codex"] == {"durable": 1}, got
    assert got["per_arm_durability"]["cursor"] == {"abandoned": 1}, got
    # Resolved AND unresolved together — 1 durable alone would read as a verdict on the round.
    assert (got["resolved"], got["unresolved"]) == (2, 1), got
    assert got["edges_written"] == 2, got

    # The edge rides the EXISTING propagation, so durability arrives on the edge itself rather
    # than through a second durability path growing beside `influence_edges`.
    import sqlite3

    edges = (
        sqlite3.connect(feedback.DB_PATH)
        .execute(
            "SELECT source_run_id, durability FROM influence_edges WHERE influence_type='experiment' "
            "AND influence_id=? ORDER BY source_run_id",
            (round_id,),
        )
        .fetchall()
    )
    assert edges == [("round:codex", "durable"), ("round:cursor", "abandoned")], edges

    # NEVER INFERRED: a finding with no recorded issue contributes nothing, and reading without
    # `apply_edges` writes no edge at all.
    empty = research_subjects.resolve_round_durability("round:nonexistent")
    assert empty["findings_filed"] == 0 and empty["edges_written"] == 0, empty
    readonly = research_subjects.resolve_round_durability(round_id)
    assert readonly["edges_written"] == 0, "reading must not write"
