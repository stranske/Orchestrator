"""Hermetic regression probes for issue 1154's ranked coverage targets."""

from __future__ import annotations

import copy
import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import capabilities
import capability_outcome_bridge as bridge
import capability_recurrence_check as recurrence
import dispatcher
import feedback
import mcp_server as mcp
import partitioned_review as review


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Any unexpected process, agent, or network operation fails even if swallowed."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(mcp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mcp, "HANDOFF", tmp_path / "handoff")
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("ranked coverage tests must not start external work")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("socket", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(dispatcher, "offload", forbidden)
    monkeypatch.setattr(capabilities, "production_heartbeat", forbidden)
    yield
    assert attempted == []


@pytest.mark.parametrize("as_json", [False, True])
def test_recurrence_cli_preserves_offline_and_output_choice(monkeypatch, capsys, as_json):
    calls = []
    report = {"total": 0, "would_fire": 0, "would_miss": 0, "rows": []}
    monkeypatch.setattr(recurrence, "replay", lambda *, offline: calls.append(offline) or report)
    args = ["--offline"] + (["--json"] if as_json else [])
    assert recurrence.main(args) == 0
    out = capsys.readouterr().out
    if as_json:
        assert json.loads(out) == report
    else:
        assert out == recurrence.format_report(report)
    assert calls == [True]


def corpus():
    return {
        "schema_version": 1,
        "review_id": "coverage-1154",
        "objective": "Inspect an identified assertion against current source evidence.",
        "shared_context": [],
        "items": [
            {
                "item_id": "one",
                "assertion_key": "assertion-one",
                "group_key": "group",
                "assertion": "The current code satisfies the assertion.",
                "source_refs": [{"kind": "issue", "ref": "owner/repo#1"}],
                "context": "  current source only  ",
            }
        ],
    }


@pytest.fixture
def plan():
    return review.partition_corpus(corpus())


def partition_result(plan, partition, category="removed_product_surfaces"):
    categories = review._empty_categories()
    for item in partition["items"]:
        categories[category].append(
            {
                "item_id": item["item_id"],
                "assertion_key": item["assertion_key"],
                "disposition": (
                    "unresolved" if category == "unresolved_design_dispositions" else "satisfied"
                ),
                "summary": "Inspected current implementation.",
                "confidence": "high",
                "recommended_action": None,
                "evidence": [
                    {
                        "type": "current_code",
                        "ref": "src/example.py:1",
                        "observation": "The current path implements the assertion.",
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "review_id": plan["review_id"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "categories": categories,
        "summary": "Reviewed all items.",
    }


def envelope(plan, partition, **updates):
    return {
        "schema_version": 1,
        "plan_sha256": plan["plan_sha256"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "status": "complete",
        "provenance": {"run_id": "fixture-run", "log": "fixture-log"},
        "result": partition_result(plan, partition),
        **updates,
    }


def test_atomic_json_preserves_old_result_and_removes_temporary_file_on_replace_failure(
    monkeypatch, tmp_path
):
    target = tmp_path / "result.json"
    target.write_text('{"old": true}\n')

    def fail_replace(source, dest):
        assert Path(source).parent == tmp_path
        assert dest == target
        raise OSError("replace refused")

    monkeypatch.setattr(review.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace refused"):
        review._atomic_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "content,message",
    [
        ('```json\n{"answer": 42}\n```', None),
        ('{"type":"turn.completed"}\n\n{"type":"thread.started"}', "decode"),
        ('{"type":"agent_message","text":""}', "non-empty string"),
        ('{"type":"agent_message","text":"not JSON"}', "one strict JSON object"),
        (
            '{"type":"turn.completed"}\n{"type":"agent_message","text":"not JSON"}',
            "final agent_message",
        ),
    ],
)
def test_review_transport_accepts_fences_but_refuses_missing_or_invalid_final_messages(
    content, message
):
    if message is None:
        assert review._parse_json_object(content) == {"answer": 42}
    elif message == "decode":
        with pytest.raises(json.JSONDecodeError):
            review._parse_json_object(content)
    else:
        with pytest.raises(ValueError, match=message):
            review._parse_json_object(content)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"max_prompt_chars": 1999}, "max_prompt_chars must be >= 2000"),
        ({"overlap": 2, "witness_agents": ["a"]}, "exactly one agent per witness"),
        ({"overlap": 2, "witness_agents": ["a", " "]}, "entries must be non-empty"),
        ({"max_prompt_chars": 2000}, "cannot fit max_prompt_chars"),
    ],
)
def test_partitioning_refuses_unusable_limits_and_witness_declarations(kwargs, message):
    with pytest.raises(ValueError, match=message):
        review.partition_corpus(corpus(), **kwargs)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("schema_version", 0, "plan.schema_version must equal 1"),
        ("review_id", " ", "plan.review_id must be a non-empty string"),
        ("partitions", [], "plan.partitions must be a non-empty list"),
        (
            "limits",
            {"max_items": 1, "max_prompt_chars": 24000, "overlap": 1},
            "plan.limits.overlap must be an integer >= 2 when present",
        ),
    ],
)
def test_plan_validation_names_invalid_top_level_fields(plan, field, value, message):
    plan[field] = value
    assert message in review.validate_plan(plan)


def test_plan_validation_reports_partition_and_item_corruption(monkeypatch, plan):
    partition = plan["partitions"][0]
    partition["agent"] = " "
    partition["group_key"] = " "
    partition["partition_digest"] = " "
    partition["items"].append(None)
    partition["items"][0]["assertion"] = " "
    plan["limits"]["max_items"] = 1
    errors = review.validate_plan(plan)
    for message in (
        "plan.partitions[0].agent must be a non-empty string when present",
        "plan.partitions[0].group_key must be a non-empty string",
        "plan.partitions[0].partition_digest must be a non-empty string",
        "plan.partitions[0].items exceeds max_items=1",
        "plan.partitions[0].items[0].assertion must be a non-empty string",
        "plan.partitions[0].items[0].group_key must match its partition",
        "plan.partitions[0].items[1] must be an object",
    ):
        assert any(error.startswith(message) for error in errors), (message, errors)

    def unrenderable(*args):
        raise ValueError("fixture prompt unavailable")

    monkeypatch.setattr(review, "build_partition_prompt", unrenderable)
    assert (
        "plan.partitions[0] prompt cannot be rendered: fixture prompt unavailable"
        in review.validate_plan(plan)
    )


def test_plan_validation_reports_duplicate_partitions_and_items_and_oversize_prompt(plan):
    plan["partitions"].append(copy.deepcopy(plan["partitions"][0]))
    plan["limits"]["max_prompt_chars"] = 2000
    errors = review.validate_plan(plan)
    assert "plan.partitions[1].partition_id duplicates 'p001-group'" in errors
    assert "plan.partitions[1].items[0].item_id duplicates 'one'" in errors
    assert "plan.partitions[0] exceeds max_prompt_chars=2000" in errors


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("assertion_key", "wrong", ".assertion_key must match the corpus item"),
        ("summary", " ", ".summary must be a non-empty string"),
        ("recommended_action", 7, ".recommended_action must be a non-empty string or null"),
        ("evidence", [], ".evidence must be a non-empty list"),
        ("evidence", [None], ".evidence[0] must contain exactly type, ref, observation"),
        (
            "evidence",
            [{"type": "invented", "ref": " ", "observation": " "}],
            ".evidence[0].type must be one of",
        ),
    ],
)
def test_finding_validation_refuses_unsupported_evidence(plan, field, value, message):
    partition = plan["partitions"][0]
    result = partition_result(plan, partition)
    finding = result["categories"]["removed_product_surfaces"][0]
    finding[field] = value
    errors = review.validate_partition_result(result, plan, partition)
    assert any(
        error.startswith("result.categories.removed_product_surfaces[0]" + message)
        for error in errors
    ), errors
    if field == "evidence" and value and isinstance(value[0], dict):
        assert any("ref and observation must be non-empty strings" in e for e in errors)


def test_result_validation_reports_shape_duplicate_and_category_disposition_errors(plan):
    partition = plan["partitions"][0]
    result = partition_result(plan, partition)
    result["extra"] = True
    finding = result["categories"]["removed_product_surfaces"][0]
    finding["extra"] = True
    result["categories"]["intentional_adapters"] = [copy.deepcopy(finding)]
    result["categories"]["unresolved_design_dispositions"] = [copy.deepcopy(finding)]
    errors = review.validate_partition_result(result, plan, partition)
    assert "result has unknown keys: ['extra']" in errors
    assert "result.categories.removed_product_surfaces[0] has unknown keys: ['extra']" in errors
    assert "result.categories.intentional_adapters[0].disposition must be 'intentional'" in errors
    assert (
        "result.categories.unresolved_design_dispositions[0].disposition must be 'unresolved'"
        in errors
    )
    assert "partition items classified more than once: ['one']" in errors
    assert review.validate_partition_result({}, plan, partition)[0].startswith(
        "result missing keys:"
    )
    assert review._finding_errors(None, "finding", {}) == ["finding must be an object"]


def test_envelope_validation_rejects_stale_plan_and_missing_provenance(plan):
    partition = plan["partitions"][0]
    value = envelope(
        plan,
        partition,
        schema_version=0,
        plan_sha256="stale",
        partition_id="other",
        provenance={"run_id": "fixture-run"},
    )
    assert review._validate_envelope(value, plan, partition) == [
        "envelope schema_version must equal 1",
        "plan_sha256 mismatch",
        "partition_id mismatch",
        "completed partition requires provenance.run_id and provenance.log",
    ]


@pytest.mark.parametrize("case", ["empty", "no-arms", "unproven", "unavailable"])
def test_round_registration_explains_why_no_arm_can_be_registered(monkeypatch, plan, case):
    import research_subjects

    writes = []
    monkeypatch.setattr(
        research_subjects, "record_research_round", lambda *a, **k: writes.append(a)
    )
    if case == "empty":
        plan["review_id"] = " "
        expected = "plan has no review_id"
    else:
        expected = "no agent actually did the work"

    def evidence(*args, **kwargs):
        if case == "unavailable":
            raise OSError("fixture database locked")
        return {"arms": []}

    monkeypatch.setattr(research_subjects, "round_arm_evidence", evidence)
    result = review.register_review_round(plan, [] if case == "no-arms" else ["unproven"])
    assert result == {
        "registered": False,
        "reason": "OSError: fixture database locked" if case == "unavailable" else expected,
    }
    assert writes == []


def test_run_plan_retries_corrupt_cache_through_default_transport_and_records_failure(
    monkeypatch, tmp_path, plan
):
    partition = plan["partitions"][0]
    path = review._partition_path(tmp_path, partition["partition_id"])
    path.parent.mkdir()
    path.write_text("{corrupt")
    calls = []
    monkeypatch.setattr(review, "register_review_round", lambda *a, **k: {"registered": False})

    def offload(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("fixture backend unavailable")

    monkeypatch.setattr(dispatcher, "offload", offload)
    summary = review.run_plan(plan, agent="fixture-agent", cwd=tmp_path, results_dir=tmp_path)
    assert summary["coverage_status"] == "incomplete"
    saved = json.loads(path.read_text())
    assert saved["status"] == "failed"
    assert saved["failure_reason"] == "offload raised: fixture backend unavailable"
    assert saved["provenance"]["exit"] == 70
    assert len(calls) == 1
    assert calls[0][0][0] == "fixture-agent"
    assert calls[0][1] == {
        "cwd": str(tmp_path),
        "timeout": 300,
        "isolate": False,
        "research_round": None,
    }
    with pytest.raises(ValueError, match="timeout must be >= 1 second"):
        review.run_plan(plan, agent="fixture-agent", cwd=tmp_path, results_dir=tmp_path, timeout=0)


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"exit": 0, "output": "{}"}, "partition result failed schema validation"),
        ({"exit": 0, "output": "not JSON"}, "partition result was not valid JSON"),
    ],
)
def test_run_plan_records_invalid_results_without_claiming_coverage(
    monkeypatch, tmp_path, plan, response, reason
):
    monkeypatch.setattr(review, "register_review_round", lambda *a, **k: {"registered": False})
    summary = review.run_plan(
        plan,
        agent="fixture",
        cwd=tmp_path,
        results_dir=tmp_path,
        offload_fn=lambda *a, **k: response,
    )
    saved = review._read_json(
        review._partition_path(tmp_path, plan["partitions"][0]["partition_id"])
    )
    assert saved["status"] == "invalid"
    assert saved["failure_reason"] == reason
    assert saved["validation_errors"]
    assert summary["incomplete_partition_ids"] == [plan["partitions"][0]["partition_id"]]


def adjudication(synthesis):
    return {
        "schema_version": 1,
        "review_id": synthesis["review_id"],
        "plan_sha256": synthesis["plan_sha256"],
        "decisions": [
            {
                "assertion_key": "assertion-one",
                "decision": "needs_more_evidence",
                "rationale": "The two sources disagree.",
                "evidence_refs": ["src/example.py:1"],
            }
        ],
    }


@pytest.mark.parametrize(
    "case,message",
    [
        ("not-object", "adjudication must be a JSON object"),
        ("shape", "adjudication must contain exactly"),
        ("schema", "adjudication.schema_version must equal 1"),
        ("provenance", "adjudication provenance does not match the synthesis"),
        ("list", "adjudication.decisions must be a list"),
        ("row", "adjudication.decisions[0] has an invalid shape"),
        ("key", "adjudication.decisions[0].assertion_key is not in the adjudication queue"),
        ("decision", "adjudication.decisions[0].decision must be one of"),
        ("rationale", "adjudication.decisions[0].rationale must be a non-empty string"),
        ("duplicate", "adjudication must decide every queued assertion exactly once"),
    ],
)
def test_adjudication_validator_requires_one_supported_decision_per_queued_assertion(
    plan, case, message
):
    synthesis = {
        **plan,
        "adjudication": {
            "conflicts": [{"assertion_key": "assertion-one"}],
            "unresolved_design_dispositions": [],
        },
    }
    value = adjudication(synthesis)
    if case == "not-object":
        value = None
    elif case == "shape":
        value.pop("review_id")
    elif case == "schema":
        value["schema_version"] = 0
    elif case == "provenance":
        value["plan_sha256"] = "stale"
    elif case == "list":
        value["decisions"] = None
    elif case == "row":
        value["decisions"] = [None]
    elif case == "key":
        value["decisions"][0]["assertion_key"] = None
    elif case == "decision":
        value["decisions"][0]["decision"] = "invented"
    elif case == "rationale":
        value["decisions"][0]["rationale"] = " "
    elif case == "duplicate":
        value["decisions"] *= 2
    errors = review._validate_adjudication(value, synthesis)
    assert any(error.startswith(message) for error in errors), errors


def test_synthesis_refuses_invalid_plans_and_reports_unreadable_envelopes(tmp_path, plan):
    with pytest.raises(ValueError, match="invalid plan"):
        review.synthesize_results({}, results_dir=tmp_path)
    path = review._partition_path(tmp_path, plan["partitions"][0]["partition_id"])
    path.parent.mkdir()
    path.write_text("{truncated")
    synthesis = review.synthesize_results(plan, results_dir=tmp_path, adjudicator_agent="fixture")
    assert synthesis["verdict"] == "INCOMPLETE"
    assert synthesis["partition_statuses"][0]["status"] == "invalid"
    assert synthesis["partition_statuses"][0]["errors"]
    assert synthesis["adjudicator"]["status"] == "skipped_incomplete_coverage"


@pytest.mark.parametrize("mode", ["raises", "invalid-json", "nonzero"])
def test_synthesis_default_adjudicator_reports_transport_failures(
    monkeypatch, tmp_path, plan, mode
):
    partition = plan["partitions"][0]
    value = envelope(
        plan, partition, result=partition_result(plan, partition, "unresolved_design_dispositions")
    )
    review._atomic_json(review._partition_path(tmp_path, partition["partition_id"]), value)
    calls = []

    def offload(*args, **kwargs):
        calls.append((args, kwargs))
        if mode == "raises":
            raise RuntimeError("fixture adjudicator failed")
        return {"exit": 3 if mode == "nonzero" else 0, "output": "not JSON"}

    monkeypatch.setattr(dispatcher, "offload", offload)
    synthesis = review.synthesize_results(
        plan, results_dir=tmp_path, adjudicator_agent="fixture", cwd=tmp_path
    )
    result = synthesis["adjudicator"]
    assert synthesis["verdict"] == "NEEDS_ADJUDICATION"
    assert result["status"] == "failed"
    if mode == "raises":
        assert result["errors"] == ["offload raised: fixture adjudicator failed"]
    elif mode == "nonzero":
        assert result["errors"] == ["agent exited 3"]
    else:
        assert result["errors"][0].startswith("could not parse strict JSON adjudication:")
    assert len(calls) == 1
    assert calls[0][0][0] == "fixture"


def test_review_cli_disabled_help_and_prepare_failure_boundaries(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(review, "DISABLED", True)
    assert review.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"skipped": "ORCH_PARTITIONED_REVIEW_DISABLED=1"}
    monkeypatch.setattr(review, "DISABLED", False)
    monkeypatch.setattr(review, "_capability_heartbeat", lambda: calls.append("heartbeat"))
    assert review.main([]) == 2
    assert "usage:" in capsys.readouterr().out
    source = tmp_path / "corpus.json"
    target = tmp_path / "plan.json"
    source.write_text(json.dumps(corpus()))
    assert review.main(["prepare", "--corpus", str(source), "--plan", str(target)]) == 0
    prepared = json.loads(target.read_text())
    assert review.validate_plan(prepared) == []
    assert prepared["partitions"][0]["items"][0]["context"] == "current source only"
    assert json.loads(capsys.readouterr().out)["plan_sha256"] == prepared["plan_sha256"]
    assert calls == ["heartbeat", "heartbeat"]


def test_review_heartbeat_failure_is_best_effort(monkeypatch):
    calls = []

    def heartbeat(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("fixture ledger unavailable")

    monkeypatch.setattr(capabilities, "production_heartbeat", heartbeat)
    assert review._capability_heartbeat() is None
    assert calls == [(("partitioned-review", "invocation"), {"ref": "partitioned_review.main"})]


def test_mcp_decline_vocabulary_degrades_without_breaking_health_tools(monkeypatch):
    monkeypatch.setitem(sys.modules, "capability_propensity", None)
    assert mcp._decline_kinds() == []


def test_mcp_health_names_watchdog_failure_instead_of_claiming_a_healthy_tick(
    monkeypatch, tmp_path
):
    import cadence_registry
    import exp_abcd
    import tick_watchdog

    class Volumes:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            assert query.startswith("SELECT COUNT(*) FROM ")
            return SimpleNamespace(fetchone=lambda: (0,))

    def unavailable(state_dir):
        assert state_dir == tmp_path
        raise OSError("fixture watchdog unavailable")

    monkeypatch.setattr(feedback, "_conn", Volumes)
    monkeypatch.setattr(cadence_registry, "CADENCE_STEPS", [])
    monkeypatch.setattr(exp_abcd, "ship_gate_stamp", lambda: tmp_path / ".last-ship-gate")
    monkeypatch.setattr(tick_watchdog, "summary", unavailable)
    result = mcp._fleet_summary()
    assert result["tick"] == {
        "state": "unavailable",
        "error": "OSError: fixture watchdog unavailable",
    }
    assert result["heartbeat_age_s"] is None
    assert result["cadence_stamps"] == {"last-ship-gate": None}
    assert set(result["db_volumes"].values()) == {0}


def test_mcp_rpc_reports_tool_errors_and_ignores_unknown_notifications():
    response = mcp._handle({"id": 17, "method": "tools/call", "params": {"name": "absent"}})
    assert response["id"] == 17
    assert response["result"]["isError"] is True
    assert "absent" in response["result"]["content"][0]["text"]
    assert mcp._handle({"method": "unknown-notification"}) is None


def forwarding_probe(args):
    return dict("literal", args["experiment"], reason=args.get("why"))


def test_mcp_forwarding_inspection_ignores_literal_positionals():
    assert mcp._forwarded_args(forwarding_probe, "dict") == {"experiment", "reason"}


def test_bridge_resolvers_refuse_unavailable_roles_and_invalid_serialized_tags(monkeypatch):
    monkeypatch.setitem(sys.modules, "roles", None)
    assert (
        bridge._role_capability_ids("work", {"influenced_by_run_id": "role:triage:fixture"}) == []
    )
    assert bridge._tagged_capability_ids("work", {"capability_ids": '["one", "", "two"]'}) == [
        "one",
        "two",
    ]
    assert bridge._tagged_capability_ids("work", {"capability_ids": "{corrupt"}) == []


def test_bridge_missing_registry_returns_no_known_capabilities_without_creating_it(monkeypatch):
    calls = []

    def unreadable(path):
        calls.append(path)
        raise OSError("fixture ledger unreadable")

    monkeypatch.setattr(capabilities, "load_declared", unreadable)
    assert bridge._known_capability_ids() == set()
    assert calls == [capabilities.REG]
    assert not capabilities.REG.exists()


def test_bridge_collect_survives_an_old_database_without_influence_edges(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs(run_id TEXT, task_type TEXT, agent TEXT, ts INTEGER,
                          source TEXT, target TEXT);
        CREATE TABLE outcomes(run_id TEXT, adjudicated_verdict TEXT, merged INTEGER,
                              durability TEXT, influenced_by_run_id TEXT);
        INSERT INTO runs VALUES ('work', 'implement', 'fixture', 1099511627776, 'keepalive', 'o/r#1');
        INSERT INTO outcomes VALUES ('work', 'PASS', 1, 'durable', NULL);
    """)
    monkeypatch.setattr(feedback, "_conn", lambda: conn)
    result = bridge.collect(verdict_index={"o/r#1": [{"capability_id": "lane"}]})
    assert len(result) == 1
    assert result[0]["capability_ids"] == []
    assert result[0]["fleet_verdicts"] == [{"capability_id": "lane"}]
    assert result[0]["verdict"] == "PASS"
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


@pytest.mark.parametrize(
    "dry_run,versions,expected",
    [
        (True, [], 1),
        (False, [], 0),
        (False, ["version-offload"], 1),
    ],
)
def test_offload_backfill_requires_a_recorded_backend_and_version(
    monkeypatch, dry_run, versions, expected
):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs(run_id TEXT, role_name TEXT, decomposition TEXT);
        CREATE TABLE influence_edges(target_run_id TEXT, influence_type TEXT, capability_id TEXT);
        INSERT INTO runs VALUES ('linked', 'triage', '{"backend_run_id":"backend:one"}');
        INSERT INTO runs VALUES ('replay', 'triage', '{}');
        INSERT INTO runs VALUES ('corrupt', 'triage', '{broken');
        INSERT INTO runs VALUES ('already', 'triage', '{"backend_run_id":"backend:two"}');
        INSERT INTO influence_edges VALUES ('already', 'capability', 'offload');
    """)
    calls = []
    monkeypatch.setattr(feedback, "_conn", lambda: conn)
    monkeypatch.setattr(feedback, "_resolve_capability_versions", lambda ids: versions)
    monkeypatch.setattr(
        feedback, "_record_influence_edge_in_conn", lambda c, **kw: calls.append(kw)
    )
    monkeypatch.setattr(feedback, "_propagate_outcome_lineage_in_conn", lambda c, rid: 2)
    result = bridge.backfill_offload_capability_edges(dry_run=dry_run)
    assert result == {
        "backfilled": expected,
        "edges_resolved": 2 if calls else 0,
        "dry_run": dry_run,
        "links": [{"run_id": "linked", "capability_id": "offload"}] if expected else [],
    }
    if not dry_run and versions:
        assert calls == [
            {
                "target_run_id": "linked",
                "influence_type": "capability",
                "influence_id": "version-offload",
                "source_run_id": "linked",
                "accepted": True,
                "capability_id": "offload",
                "capability_version_id": "version-offload",
            }
        ]
    else:
        assert calls == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


@pytest.mark.parametrize("dry_run", [False, True])
def test_compiled_bridge_attributes_only_explicit_durable_delivery(monkeypatch, dry_run):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs(run_id TEXT, target TEXT, routing_metadata TEXT);
        CREATE TABLE outcomes(run_id TEXT, durability TEXT);
        CREATE TABLE influence_edges(target_run_id TEXT, influence_type TEXT, capability_id TEXT);
        INSERT INTO runs VALUES ('explicit', 'o/r#1', '{"causal_context":{"capability_id":"rail"}}');
        INSERT INTO runs VALUES ('unclaimed', 'o/r#2', '{}');
        INSERT INTO outcomes VALUES ('explicit', 'durable');
        INSERT INTO outcomes VALUES ('unclaimed', 'durable');
    """)
    monkeypatch.setattr(feedback, "_conn", lambda: conn)
    monkeypatch.setattr(
        bridge,
        "compiled_workflow_subjects",
        lambda **kw: {
            "rail": {"version_id": "rail-v1", "subjects": ["o/r"]},
            "unversioned": {"subjects": ["o/r"]},
        },
    )
    calls = []
    monkeypatch.setattr(
        feedback, "_record_influence_edge_in_conn", lambda c, **kw: calls.append(kw)
    )
    monkeypatch.setattr(feedback, "_propagate_outcome_lineage_in_conn", lambda c, rid: 3)
    result = bridge.attribute_compiled_workflow_edges(dry_run=dry_run)
    assert result["attributed"] == 1
    assert result["subjects_seen"] == 2
    assert result["links"] == [
        {"capability_id": "rail", "target_run_id": "explicit", "subject_id": "o/r"}
    ]
    assert result["edges_resolved"] == (0 if dry_run else 3)
    assert calls == (
        []
        if dry_run
        else [
            {
                "target_run_id": "explicit",
                "influence_type": "capability",
                "influence_id": "rail-v1",
                "source_run_id": None,
                "accepted": True,
                "capability_id": "rail",
                "capability_version_id": "rail-v1",
            }
        ]
    )
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


@pytest.mark.parametrize("returncode", [0, 1])
def test_bridge_external_ci_reader_preserves_workflow_query_and_reports_gh_failure(
    monkeypatch, returncode
):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=returncode, stdout='[{"databaseId":42}]', stderr=" fixture gh failed "
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    if returncode:
        with pytest.raises(RuntimeError, match="^fixture gh failed$"):
            bridge._gh_workflow_runs("o/r", "ci.yml")
    else:
        assert bridge._gh_workflow_runs("o/r", "ci.yml") == [{"databaseId": 42}]
    assert calls == [
        (
            [
                "gh",
                "run",
                "list",
                "--repo",
                "o/r",
                "--workflow",
                "ci.yml",
                "--limit",
                str(bridge.EXTERNAL_CI_LOOKBACK_RUNS),
                "--json",
                "databaseId,status,conclusion,createdAt",
            ],
            {"capture_output": True, "text": True, "timeout": 60},
        )
    ]


@pytest.mark.parametrize("dry_run", [False, True])
def test_bridge_external_ci_dry_run_and_heartbeat_failure_are_visible(monkeypatch, dry_run):
    calls = []

    def heartbeat(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("fixture ledger unavailable")

    monkeypatch.setattr(capabilities, "heartbeat", heartbeat)
    result = bridge.ingest_external_ci_invocations(
        dry_run=dry_run,
        runs_fn=lambda *args: [
            {"status": "completed", "databaseId": 42},
            {"status": "in_progress"},
        ],
    )
    cid = next(iter(bridge.EXTERNAL_CI_CAPABILITIES))
    assert result["observed"] == {cid: {"runs_seen": 2, "completed": 1}}
    assert result["credited"] == 0
    if dry_run:
        assert result["errors"] == []
        assert calls == []
    else:
        assert result["errors"] == [{"capability_id": cid, "error": "fixture ledger unavailable"}]
        assert len(calls) == 1
        assert calls[0][1]["path"] == capabilities.REG


@pytest.mark.parametrize(
    "stage,key",
    [
        ("backfill_role_capability_edges", "role_capability_edges"),
        ("backfill_offload_capability_edges", "offload_capability_edges"),
        ("ingest_external_ci_invocations", "external_ci"),
        ("attribute_fleet_deliverable_edges", "fleet_deliverable_edges"),
        ("attribute_compiled_workflow_edges", "compiled_workflow_edges"),
    ],
)
def test_bridge_run_reports_failed_repairs_and_continues_the_heartbeat_path(
    monkeypatch, stage, key
):
    order = []

    def fail(**kwargs):
        order.append(stage)
        raise OSError("fixture unavailable")

    for name in (
        "backfill_role_capability_edges",
        "backfill_offload_capability_edges",
        "ingest_external_ci_invocations",
        "attribute_fleet_deliverable_edges",
        "attribute_compiled_workflow_edges",
    ):
        monkeypatch.setattr(bridge, name, lambda **kwargs: {})
    monkeypatch.setattr(bridge, stage, fail)
    monkeypatch.setattr(capabilities, "load", lambda *a, **kw: {"tagged": {}})
    row = {"run_id": "work", "verdict": "PASS", "capability_ids": ["tagged"]}

    def collect(**kwargs):
        order.append("collect")
        assert kwargs["lookback_days"] == 7
        return [row]

    monkeypatch.setattr(bridge, "collect", collect)
    monkeypatch.setattr(bridge, "RESOLVERS", [("run_tagged", bridge._tagged_capability_ids)])
    calls = []
    monkeypatch.setattr(capabilities, "heartbeat", lambda *a, **kw: calls.append((a, kw)) or True)
    result = bridge.run(lookback_days=7)
    assert result[key]["error"] == "fixture unavailable"
    assert result["terminal_outcomes"] == 1
    assert result["by_capability"] == {"tagged": 1}
    assert result["written"] == 1
    assert calls[0][0] == ("tagged", "outcome")
    assert calls[0][1]["ref"] == "work"
    assert order == (
        ["collect", stage] if stage == "attribute_compiled_workflow_edges" else [stage, "collect"]
    )


def test_bridge_cli_forwards_lookback_and_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bridge, "run", lambda **kw: calls.append(kw) or {"written": 0})
    assert bridge.main(["--lookback-days", "7", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"written": 0}
    assert calls == [{"lookback_days": 7, "dry_run": True}]
