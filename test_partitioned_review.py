from __future__ import annotations

import json
from pathlib import Path

import dispatcher
import partitioned_review as review
import pytest


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
        "source_refs": [
            {"kind": "pull_request", "ref": f"owner/repo#{group.rsplit('-', 1)[-1]}"}
        ],
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
        review._partition_path(tmp_path / "results", plan["partitions"][1]["partition_id"])
        .read_text()
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
    assert review.main(
        [
            "synthesize",
            "--plan",
            str(tmp_path / "timeout-plan.json"),
            "--results-dir",
            str(tmp_path / "results"),
            "--output",
            str(tmp_path / "incomplete-synthesis.json"),
        ]
    ) == 1


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
    assert review.main(
        [
            "synthesize",
            "--plan",
            str(tmp_path / "plan.json"),
            "--results-dir",
            str(tmp_path / "results"),
            "--output",
            str(tmp_path / "synthesis.json"),
        ]
    ) == 1


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
        "schema_version" in error
        for error in wrong_schema["partition_statuses"][0]["errors"]
    )
    envelope["schema_version"] = 1
    envelope["partition_digest"] = "stale-digest"
    envelope_path.write_text(json.dumps(envelope))

    stale = review.synthesize_results(plan, results_dir=results_dir)
    assert stale["coverage_status"] == "incomplete"
    assert stale["verdict"] == "INCOMPLETE"
    assert any(
        "partition_digest mismatch" in error
        for error in stale["partition_statuses"][0]["errors"]
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
    assert review.main(
        [
            "synthesize",
            "--plan",
            str(tmp_path / "complete-plan.json"),
            "--results-dir",
            str(results_dir),
            "--output",
            str(tmp_path / "complete-synthesis.json"),
        ]
    ) == 0


def test_dispatcher_exposes_partitioned_review_selftest() -> None:
    assert dispatcher.main(["review-corpus", "--selftest"]) == 0
