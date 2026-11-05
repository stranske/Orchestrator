"""Hermetic regression probes for issue 1173's ranked coverage targets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import capabilities
import capability_outcome_bridge as bridge
import execution_profiles as profiles
import feedback
import gh_capacity
import issue_quality
import keepalive_outcomes as ko
import keepalive_shadow as shadow
import mcp_server
import partitioned_review as review


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every exercised helper receives a fake transport and temp-only storage."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_profile_identity_rejects_each_unsafe_identity_shape() -> None:
    profile = dict(next(iter(profiles.PROFILE_REGISTRY.values())))
    profile["transport_support"] = ["offload", "local"]
    profile["capacity_pool_ids"] = ["normal", "priority"]

    identity = profiles.profile_identity(profile)
    assert identity["transport_support"] == ["local", "offload"]
    assert identity["capacity_pool_ids"] == ["normal", "priority"]

    cases = [
        (None, "must be an object"),
        ({**profile, "agent": " "}, "missing or invalid"),
        ({**profile, "agent": "aider"}, "not an eligible"),
        ({**profile, "lifecycle_status": "retired"}, "not active"),
        ({**profile, "permission_mode": "unsafe"}, "unsupported"),
        ({**profile, "transport_support": ["offload", "offload"]}, "missing or invalid"),
        ({**profile, "transport_support": ["local"]}, "no supported offload"),
        ({**profile, "requires_explicit_selection": 1}, "flag is invalid"),
    ]
    for candidate, message in cases:
        with pytest.raises(ValueError, match=message):
            profiles.profile_identity(candidate)


def test_issue_quality_link_and_payload_helpers_preserve_only_valid_issue_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int]] = []

    def fake_json(args: list[str], *, timeout: int) -> object:
        calls.append((args, timeout))
        if args[1:3] == ["pr", "view"]:
            return {
                "closingIssuesReferences": [
                    {"number": "3"},
                    {"number": 3},
                    {"number": None},
                    {"number": "bad"},
                    None,
                ]
            }
        return {"number": 3, "title": "fixture", "body": "body", "url": "https://example"}

    monkeypatch.setattr(issue_quality.keepalive_outcomes, "_run_json", fake_json)
    assert issue_quality.fetch_linked_issue_numbers("owner/repo", 7) == [3]
    assert issue_quality.fetch_issue_body("owner/repo", 3) == {
        "number": 3,
        "title": "fixture",
        "body": "body",
        "url": "https://example",
    }
    assert issue_quality._coerce_issue_payload("body") == ("body", None, None)
    assert issue_quality._coerce_issue_payload({"body": 7, "title": 8, "url": 9}) == ("7", 8, 9)
    assert issue_quality._coerce_issue_payload({"title": "missing"}) is None
    assert issue_quality._coerce_issue_payload([]) is None
    assert all(timeout == issue_quality.GH_TIMEOUT_SECONDS for _, timeout in calls)


def test_shadow_live_and_backfill_failures_are_hermetic_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="owner/repo#N"):
        shadow.gather_signals("owner/repo", runner=lambda *_a, **_k: None)

    failed = SimpleNamespace(returncode=1, stdout="", stderr="not found")
    with pytest.raises(RuntimeError, match="not found"):
        shadow.gather_signals("owner/repo#7", runner=lambda *_a, **_k: failed)

    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout=json.dumps({"state": "MERGED", "labels": []})),
            SimpleNamespace(returncode=0, stdout='<!-- keepalive-state:v1 {"iteration":2} -->'),
        ]
    )
    signals = shadow.gather_signals("owner/repo#7", runner=lambda *_a, **_k: next(responses))
    assert signals["pr_state"] == "merged" and signals["iteration"] == 2
    assert shadow.extract_state_payload("<!-- keepalive-state:v1 malformed -->") is None
    assert (
        shadow.normalize_signals("o/r#1", {}, pr_state="closed", labels=[])["outcome"]
        == "closed_unmerged"
    )

    monkeypatch.setitem(
        sys.modules,
        "durability_sweep",
        SimpleNamespace(
            classify_durability=lambda _row, pr, **_kw: {
                "durability": "durable" if pr["repo"] else None
            }
        ),
    )
    assert (
        shadow._durability_outcome(
            "owner/repo#7",
            runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        )
        == "durable"
    )
    assert (
        shadow._durability_outcome(
            "owner/repo#7",
            runner=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="denied"),
        )
        is None
    )

    monkeypatch.setitem(
        sys.modules,
        "gh_capacity",
        SimpleNamespace(throttle_if_enabled=lambda _resource: (_ for _ in ()).throw(OSError())),
    )
    # _gh_throttle itself is intentionally fail-open for an optional pacing dependency.
    shadow._gh_throttle("core")

    corpus = tmp_path / "shadow.jsonl"
    corpus.write_text("\nnot-json\n" + json.dumps({"source": "backfill", "target": "o/r#1"}) + "\n")
    assert shadow._backfilled_targets(corpus) == {"o/r#1"}

    # A line that parses but is not an object used to abort the whole scan. Invalid JSON is
    # already skipped; a list, null, a target-less row, and a non-backfill row must be too.
    mixed = tmp_path / "mixed-shadow.jsonl"
    mixed.write_text(
        "\n".join(
            [
                "not-json",
                json.dumps(["not-a-row"]),
                "null",
                json.dumps({"source": "backfill"}),
                json.dumps({"source": "live", "target": "o/r#2"}),
                json.dumps({"source": "backfill", "target": "o/r#9"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert shadow._backfilled_targets(mixed) == {"o/r#9"}

    result = shadow.backfill(
        since="2026-01-01",
        until="2026-01-02",
        runner=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="search denied"),
        corpus_path=corpus,
    )
    assert result == {"since": "2026-01-01", "error": "search denied", "recorded": 0}


def _forbid_processes(monkeypatch: pytest.MonkeyPatch) -> list:
    """Any real subprocess is a test defect. Callers that need a transport pass a runner."""
    started: list = []

    def boom(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError(f"subprocess started: {args!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    for module in (ko, shadow, gh_capacity, bridge, review, mcp_server):
        if hasattr(module, "subprocess"):
            monkeypatch.setattr(module.subprocess, "run", boom)
    return started


def _corpus(items: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "review_id": "tmp-feedback-reconciliation",
        "objective": "Reconcile historical feedback against the current implementation.",
        "shared_context": ["Matching names are discovery hints, not defects."],
        "items": items,
    }


def _item(item_id: str, group: str, *, assertion: str | None = None) -> dict:
    return {
        "item_id": item_id,
        "assertion_key": item_id,
        "group_key": group,
        "assertion": assertion or f"Determine the current disposition of {item_id}.",
        "source_refs": [{"kind": "pull_request", "ref": f"owner/repo#{group.rsplit('-', 1)[-1]}"}],
    }


def _finding(item: dict, *, disposition: str = "remaining") -> dict:
    return {
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


def _completed_envelope(plan: dict, partition: dict, finding: dict) -> dict:
    categories = review._empty_categories()
    categories["confirmed_defects"].append(finding)
    return {
        "schema_version": 1,
        "review_id": plan["review_id"],
        "plan_sha256": plan["plan_sha256"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "status": "complete",
        "failure_reason": None,
        "validation_errors": [],
        "provenance": {"run_id": "offload:test", "log": "/tmp/offload-test.log", "exit": 0},
        "result": {
            "schema_version": 1,
            "review_id": plan["review_id"],
            "partition_id": partition["partition_id"],
            "partition_digest": partition["partition_digest"],
            "summary": "Partition complete.",
            "categories": categories,
        },
    }


def _brain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(
        feedback,
        "_resolve_capability_versions",
        lambda ids: [f"cv:{item}" for item in ids],
    )


def test_partitioned_review_missing_validation_and_round_arms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _forbid_processes(monkeypatch)
    _brain(monkeypatch, tmp_path)
    corpus = _corpus([_item("CW-1", "source-pr-61")])
    assert any("non-empty list" in error for error in review.validate_corpus(_corpus([])))
    with pytest.raises(ValueError, match="witness_agents without overlap"):
        review.partition_corpus(corpus, witness_agents=["solo"], overlap=1)

    real_validate = review.validate_plan

    def invalid_generated(plan):
        if isinstance(plan, dict) and plan.get("partitions"):
            return ["forced invalid plan"]
        return real_validate(plan)

    monkeypatch.setattr(review, "validate_plan", invalid_generated)
    with pytest.raises(AssertionError, match="generated invalid plan"):
        review.partition_corpus(corpus)
    monkeypatch.setattr(review, "validate_plan", real_validate)

    real_prompt = review.build_partition_prompt
    armed = {"final": False}

    def validate_then_arm(plan):
        errors = real_validate(plan)
        armed["final"] = True
        return errors

    def oversized_after_validation(plan, partition):
        text = real_prompt(plan, partition)
        return "x" * 30_000 if armed["final"] else text

    monkeypatch.setattr(review, "validate_plan", validate_then_arm)
    monkeypatch.setattr(review, "build_partition_prompt", oversized_after_validation)
    with pytest.raises(ValueError, match="exceeds max_prompt_chars"):
        review.partition_corpus(corpus, max_prompt_chars=12_000)
    monkeypatch.setattr(review, "validate_plan", real_validate)
    monkeypatch.setattr(review, "build_partition_prompt", real_prompt)

    plan = review.partition_corpus(corpus, max_prompt_chars=12_000)
    unknown = dict(plan)
    unknown["extra"] = True
    assert any("unknown keys" in error for error in review.validate_plan(unknown))
    empty_items = json.loads(json.dumps(plan))
    empty_items["partitions"][0]["items"] = []
    assert any(
        "items must be a non-empty list" in error for error in review.validate_plan(empty_items)
    )

    partition = plan["partitions"][0]
    foreign = _finding({**partition["items"][0], "item_id": "missing", "assertion_key": "missing"})
    categories = review._empty_categories()
    categories["confirmed_defects"].append(foreign)
    result = {
        "schema_version": 1,
        "review_id": plan["review_id"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "summary": "Partition complete.",
        "categories": categories,
    }
    errors = review.validate_partition_result(result, plan, partition)
    assert any("not in the partition" in error for error in errors), errors
    assert any("not classified" in error for error in errors), errors

    registered = review.register_review_round(
        plan, ["codex"], executing_agent="cursor", date="2026-01-01"
    )
    assert registered["registered"] is True, registered
    assert registered["arms"][0] == "cursor", registered
    assert registered["arms_unproven"] == ["codex"], registered

    def replace_fails(*_a, **_k):
        raise OSError("replace failed")

    def unlink_fails(_path):
        raise OSError("unlink failed")

    monkeypatch.setattr(review.os, "replace", replace_fails)
    monkeypatch.setattr(review.os, "unlink", unlink_fails)
    with pytest.raises(OSError, match="replace failed"):
        review._atomic_json(tmp_path / "nested" / "plan.json", {"kept": False})
    assert not (tmp_path / "nested" / "plan.json").exists()
    assert started == []


def test_partitioned_review_resume_synthesis_and_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _forbid_processes(monkeypatch)
    _brain(monkeypatch, tmp_path)
    plan = review.partition_corpus(
        _corpus([_item("CW-1", "source-pr-61")]), max_prompt_chars=12_000
    )
    partition = plan["partitions"][0]
    envelope = _completed_envelope(plan, partition, _finding(partition["items"][0]))
    result_path = review._partition_path(tmp_path / "results", partition["partition_id"])
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(envelope), encoding="utf-8")
    offloads: list = []
    run = review.run_plan(
        plan,
        agent="codex",
        cwd=tmp_path,
        results_dir=tmp_path / "results",
        offload_fn=lambda *_a, **_k: offloads.append(True),
        round_date="2026-01-01",
    )
    assert offloads == []
    assert run["partition_statuses"][0]["reused"] is True

    synthesis = review.synthesize_results(plan, results_dir=tmp_path / "results")
    assert synthesis["verdict"] == "FINDINGS_REMAIN", synthesis["verdict"]

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(review, "_capability_heartbeat", lambda *_a, **_k: None)

    def fake_run(_plan, **_kwargs):
        return {"coverage_status": "complete", "statuses": []}

    monkeypatch.setattr(review, "run_plan", fake_run)
    assert (
        review.main(
            [
                "run",
                "--plan",
                str(plan_path),
                "--results-dir",
                str(tmp_path / "out"),
                "--agent",
                "codex",
            ]
        )
        == 0
    )
    assert '"coverage_status": "complete"' in capsys.readouterr().out

    def incomplete(_plan, **_kwargs):
        return {"coverage_status": "incomplete", "statuses": []}

    monkeypatch.setattr(review, "run_plan", incomplete)
    assert (
        review.main(
            [
                "run",
                "--plan",
                str(plan_path),
                "--results-dir",
                str(tmp_path / "out"),
                "--agent",
                "codex",
            ]
        )
        == 1
    )

    real_partition = review.partition_corpus

    def allow_duplicate_witnesses(*args, **kwargs):
        if kwargs.get("witness_agents") == ["alpha", "alpha"]:
            return {"partitions": []}
        return real_partition(*args, **kwargs)

    monkeypatch.setattr(review, "partition_corpus", allow_duplicate_witnesses)
    with pytest.raises(AssertionError, match="duplicate witness agents must be refused"):
        review._selftest()
    assert started == []


def test_mcp_decline_guard_rejects_an_accepted_blank_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _forbid_processes(monkeypatch)
    import capability_propensity

    real_decline = capability_propensity.record_decline

    def accept_blank(capability_id, experiment_id, **kwargs):
        if not str(kwargs.get("reason") or "").strip():
            return {"recorded": True}
        return real_decline(capability_id, experiment_id, **kwargs)

    monkeypatch.setattr(capability_propensity, "record_decline", accept_blank)
    with pytest.raises(AssertionError, match="capability_decline accepted"):
        mcp_server._selftest_decline_schema_matches_record_decline()
    assert started == []


def test_capability_bridge_dry_run_unlinked_duplicate_and_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _forbid_processes(monkeypatch)
    _brain(monkeypatch, tmp_path)
    clock = {"now": 1_786_516_882}
    monkeypatch.setattr("time.time", lambda: float(clock["now"]))

    def role(run_id: str) -> None:
        clock["now"] += 1
        feedback.record_role_run(run_id, "triage", "snapshot", "gemini", backend_run_id="offload:1")

    def work(run_id: str) -> None:
        clock["now"] += 1
        feedback.record_run(run_id, "o/r#1", "implement", "gemini", mode="remote", pr_number=1)

    role("role:triage:dry")
    work("remote:o/r#1:dry")
    feedback.join_role_to_outcome("role:triage:dry", "remote:o/r#1:dry", accepted=True)
    preview = bridge.backfill_role_capability_edges(dry_run=True)
    assert preview["dry_run"] is True and preview["backfilled"] >= 1, preview
    assert preview["links"][0]["accepted"] is True

    role("role:triage:unlinked")
    work("remote:o/r#1:unlinked")
    feedback.join_role_to_outcome("role:triage:unlinked", "remote:o/r#1:unlinked", accepted=True)
    with feedback._conn() as conn:
        conn.execute("DELETE FROM completion_events")
        conn.commit()
    unlinked = bridge.backfill_role_capability_edges()
    assert unlinked["unlinked_edges"] >= 1, unlinked

    role("role:triage:pair")
    work("remote:o/r#2:pair")
    clock["now"] += 1
    feedback.record_influence_edge(
        target_run_id="remote:o/r#2:pair",
        influence_type="role",
        influence_id="role:triage:pair:early",
        source_run_id="role:triage:pair",
        accepted=False,
    )
    clock["now"] += 10
    feedback.record_influence_edge(
        target_run_id="remote:o/r#2:pair",
        influence_type="role",
        influence_id="role:triage:pair:late",
        source_run_id="role:triage:pair",
        accepted=False,
    )
    feedback.record_outcome(
        "remote:o/r#2:pair", adjudicated_verdict="PASS", merged=1, durability="durable"
    )
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE influence_edges SET outcome_verdict='PASS', merged=1, durability='durable' "
            "WHERE influence_id='role:triage:pair:early'"
        )
        conn.execute(
            "UPDATE influence_edges SET outcome_verdict='FAIL', merged=0, durability='reverted' "
            "WHERE influence_id='role:triage:pair:late'"
        )
        conn.commit()
    graded = bridge.backfill_role_capability_edges()
    with feedback._conn() as conn:
        verdicts = conn.execute(
            "SELECT outcome_verdict FROM influence_edges WHERE influence_type='capability' "
            "AND accepted=0 AND target_run_id='remote:o/r#2:pair'"
        ).fetchall()
    assert graded["counterfactual"] >= 1, graded
    assert verdicts and all(row[0] == "PASS" for row in verdicts), verdicts

    feedback.record_run(
        "keepalive:o/r#7:codex",
        "Owner/Repo#7",
        "implement",
        "codex",
        source="keepalive",
        pr_number=7,
    )
    feedback.record_completion_event("keepalive:o/r#7:codex")
    feedback.record_outcome(
        "keepalive:o/r#7:codex", adjudicated_verdict="PASS", merged=1, durability="durable"
    )
    fleet = bridge.attribute_fleet_deliverable_edges(
        verdict_index={
            "owner/repo#7": [{"capability_id": "role-triage", "version_id": "cv:role-triage"}]
        },
        dry_run=True,
    )
    assert fleet["dry_run"] is True and fleet["attributed"] == 1, fleet
    closed = bridge.attribute_fleet_deliverable_edges(verdict_index={}, dry_run=False)
    assert closed["attributed"] == 0
    assert started == []


def test_capability_bridge_selftest_runs_tagged_ledger_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _forbid_processes(monkeypatch)
    _brain(monkeypatch, tmp_path)
    ledger = tmp_path / "capabilities.json"

    def row(name: str) -> dict:
        blank = capabilities._blank_capability(name)
        blank.update(
            {
                "status": "wired",
                "owner": "orchestrator",
                "capability_version_id": f"cv:{name}",
                "entrypoint": "src/capability_outcome_bridge.py",
                "kill_switch": "ORCH_DISABLE_STEPS=bridge",
                "rollback": {"transition": "retired"},
                "expiry": 9_999_999_999,
            }
        )
        return blank

    capabilities.save(
        {
            "agy-runtime-isolation": row("agy-runtime-isolation"),
            "offload": row("offload"),
        },
        ledger,
    )
    monkeypatch.setattr(capabilities, "REG", ledger)
    bridge._selftest()
    assert started == []


def test_keepalive_outcomes_reads_and_ingest_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _forbid_processes(monkeypatch)
    _brain(monkeypatch, tmp_path)
    real_gh_throttle = ko._gh_throttle
    monkeypatch.setattr(ko, "_gh_throttle", lambda *_a, **_k: None)

    real_is_file = Path.is_file
    block_registry_probe = {"on": True}

    def is_file(self) -> bool:
        if block_registry_probe["on"]:
            raise OSError("eperm")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setenv("ORCH_REPO_REVIEW_REGISTRY", str(tmp_path / "eperm-registry.json"))
    assert ko._resolve_registry_path() == ko.DROPBOX_REGISTRY_PATH
    block_registry_probe["on"] = False

    monkeypatch.setattr(
        ko.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="not-json"),
    )
    assert ko._run_json(["gh"]) is None
    monkeypatch.setattr(ko, "_run_json", lambda *_a, **_k: ["not", "a dict"])
    assert ko._fetch_pr_context("o/r", 4) == ""
    assert ko._created_epoch({}) is None
    assert ko._created_epoch({"createdAt": "   "}) is None
    assert ko._task_type_from_labels(["verification"]) == "review"
    assert ko._gh_api_json("repos/o/r/pulls/1") == ["not", "a dict"]
    monkeypatch.setattr(ko, "_gh_api_json", lambda _path: ["nope"])
    assert ko._backfill_evidence("o/r", 1) == {}

    abandoned = {"durability": "abandoned", "notes": ""}
    assert (
        ko._maybe_mark_process_ignore(
            "o/r",
            {"state": "CLOSED", "number": "bad"},
            "docs",
            abandoned,
            lambda *_a: "process_ignore=duplicate",
        )
        is abandoned
    )
    assert (
        ko._maybe_mark_process_ignore(
            "o/r",
            {"state": "CLOSED", "number": 3},
            "docs",
            abandoned,
            lambda *_a: "",
        )
        is abandoned
    )
    assert ko._should_record_outcome({"durability": "durable"}, None) is False
    assert ko._should_record_outcome(
        {"verifier_verdict": "PASS", "durability": "durable"},
        {"verifier_verdict": "NON_PASS", "durability": "durable"},
    )

    feedback.record_run("known", "o/r#1", "implement", "codex", source="keepalive")
    feedback.record_outcome("known", adjudicated_verdict="PASS", merged=1, durability="durable")
    assert ko._existing_outcome("known")["durability"] == "durable"
    assert ko._update_attribution("missing-run", "codex", "agent_label") is None

    real_batch = ko._backfill_evidence_batch

    def one_batch(_repo, numbers):
        return {
            numbers[0]: {
                "labels": ["agent:codex"],
                "job_names": [],
                "author": "cursor",
                "summary": "",
                "head_ref": "codex/issue-1",
                "commit_identities": [],
                "created_ts": 1,
            }
        }

    monkeypatch.setattr(ko, "_backfill_evidence_batch", one_batch)
    for index in range(25):
        feedback.record_run(
            f"keepalive:o/r#{index}:none",
            f"o/r#{index}",
            "implement",
            ko.NON_AGENT,
            source="keepalive",
        )
    summary = ko.backfill_attribution(backfill_limit=25)
    assert summary["examined"] == 25, summary
    assert "examined 25/25" in capsys.readouterr().err
    monkeypatch.setattr(ko, "_backfill_evidence_batch", real_batch)

    graphql = {
        "data": {
            "repository": {
                "p1": None,
                "p2": "not-a-dict",
                "p3": {
                    "number": 3,
                    "headRefName": "codex/issue-3",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "author": None,
                    "labels": None,
                    "commits": None,
                    "body": None,
                },
            }
        }
    }
    monkeypatch.setattr(ko, "_run_json", lambda *_a, **_k: graphql)
    batched = ko._backfill_evidence_batch("o/r", [1, 2, 3])
    assert set(batched) == {3} and batched[3]["author"] is None

    def gh_capacity_boom(_resource):
        raise OSError("ledger locked")

    monkeypatch.setattr(gh_capacity, "throttle_if_enabled", gh_capacity_boom)
    real_gh_throttle("core")

    monkeypatch.setattr(ko, "_fetch_dispatch_times", lambda *_a, **_k: None)
    monkeypatch.setattr(ko, "_fetch_pr_context", lambda *_a, **_k: "")
    monkeypatch.setattr(
        ko,
        "_fetch_prs",
        lambda *_a, **_k: [
            {
                "number": 70,
                "state": "MERGED",
                "title": "dry-run fix read",
                "labels": ["agent:codex"],
                "body": "",
                "mergedAt": "2026-01-02T00:00:00Z",
                "headRefName": "codex/issue-70",
                "author": {"login": "cursor"},
            }
        ],
    )
    dry = ko.ingest_keepalive_outcomes(
        ["o/r"], dry_run=True, _verifier_fetch_fn=lambda *_a, **_k: {}
    )
    assert dry["prs_seen"] == 1

    import runtime_ac_gate

    def author_fails(*_a, **_k):
        raise RuntimeError("spec store down")

    monkeypatch.setattr(runtime_ac_gate, "author_keepalive_spec", author_fails)
    feedback.record_run(
        "remote:o/r#4:codex",
        "o/r#4",
        "implement",
        "codex",
        mode="remote",
        source="orchestrator_remote",
        pr_number=4,
    )
    feedback.record_run(
        ko._stable_non_agent_run_id("o/r", 8),
        "o/r#8",
        "implement",
        ko.NON_AGENT,
        mode="local",
        source="keepalive",
        pr_number=8,
    )
    feedback.record_run(
        "remote:o/r#5:codex",
        "o/r#5",
        "implement",
        "codex",
        mode="remote",
        source="orchestrator_remote",
        pr_number=5,
    )
    feedback.record_run(
        ko._stable_run_id("o/r", 9, "codex"),
        "elsewhere",
        "implement",
        "codex",
        mode="local",
        source="keepalive",
    )

    weird = ko.ingest_keepalive_outcomes(
        ["o/weird"],
        _pr_fetch_fn=lambda *_a, **_k: {"not": "a list"},
        _revert_fn=lambda _pr: (None, "stub"),
        _fix_fn=lambda *_a, **_k: ([], True),
    )
    assert weird["prs_seen"] == 0

    recorded: list = []
    monkeypatch.setattr(
        ko.durability_sweep,
        "verifier_candidate_run_ids",
        lambda *_a, **_k: {"remote:o/r#5:codex"},
    )
    monkeypatch.setattr(
        ko.verifier_evidence,
        "record_decision",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    def fetched(_repo, _days):
        return [
            {"state": "OPEN", "title": "missing number", "labels": ["agent:codex"], "body": ""},
            {
                "number": 4,
                "state": "CLOSED",
                "title": "remote already",
                "labels": [],
                "body": "",
            },
            {
                "number": 6,
                "state": "CLOSED",
                "title": "non-agent",
                "labels": [],
                "body": "",
                "author": {"login": "octocat"},
            },
            {
                "number": 8,
                "state": "CLOSED",
                "title": "existing non-agent run",
                "labels": [],
                "body": "",
            },
            {
                "number": 5,
                "state": "OPEN",
                "title": "existing remote",
                "labels": ["agent:codex"],
                "body": "",
                "headRefName": "codex/issue-5",
            },
            {
                "number": 9,
                "state": "CLOSED",
                "title": "existing keepalive run",
                "labels": ["agent:codex"],
                "body": "",
                "headRefName": "codex/issue-9",
            },
            {
                "number": 40,
                "state": "CLOSED",
                "title": "fresh non-agent",
                "labels": [],
                "body": "",
            },
        ]

    ingested = ko.ingest_keepalive_outcomes(
        ["o/r"],
        dry_run=True,
        include_non_agent=True,
        _pr_fetch_fn=fetched,
        _verifier_fetch_fn=lambda _repo, _nums: {},
        _revert_fn=lambda _pr: (None, "stub"),
        _fix_fn=lambda *_a, **_k: ([], True),
        _now=1_700_000_000,
    )
    assert ingested["non_agent_runs_recorded"] >= 1, ingested
    assert ingested["skipped_existing"] >= 1, ingested

    live = ko.ingest_keepalive_outcomes(
        ["o/r"],
        include_non_agent=False,
        _pr_fetch_fn=lambda _repo, _days: [
            {
                "number": 4,
                "state": "OPEN",
                "title": "shadow",
                "labels": ["agent:codex"],
                "body": "",
                "headRefName": "codex/issue-4",
            },
            {
                "number": 5,
                "state": "MERGED",
                "title": "verifier",
                "labels": ["agent:codex"],
                "body": "",
                "headRefName": "codex/issue-5",
                "mergedAt": "2026-01-02T00:00:00Z",
            },
        ],
        _verifier_fetch_fn=lambda _repo, _nums: {5: {"verdict": "PASS"}},
        _revert_fn=lambda _pr: (None, "stub"),
        _fix_fn=lambda *_a, **_k: ([], True),
        _now=1_700_000_000,
    )
    assert any("spec store down" in error for error in live["runtime_ac_shadow_errors"]), live
    assert recorded and recorded[0][0][0] == "remote:o/r#5:codex"

    monkeypatch.setattr(ko, "ingest_keepalive_outcomes", lambda *_a, **_k: {"prs_seen": 0})
    monkeypatch.setattr(
        ko.outcomes, "recheck_keepalive_replacements", lambda **_k: {"rechecked": 0}
    )
    assert ko.main(["--repos", "o/r", "--dry-run"]) == 0
    assert "prs_seen" in capsys.readouterr().out
    assert started == []


def test_issue_quality_parallel_fetch_skips_empty_and_non_lists() -> None:
    empty_records, empty_skipped = issue_quality.resolve_issue_records(
        [],
        linked_issue_fn=lambda *_a: (_ for _ in ()).throw(AssertionError("fetched")),
        body_fetch_fn=lambda *_a: (_ for _ in ()).throw(AssertionError("fetched")),
    )
    assert empty_records == [] and empty_skipped == []
    row = issue_quality.RunRow("run-1", 1, "o/r#4", 4, "durable")
    fetched: list[tuple[object, ...]] = []
    records, skipped = issue_quality.resolve_issue_records(
        [row],
        linked_issue_fn=lambda *_a: (4,),
        body_fetch_fn=lambda *args: fetched.append(args) or "body",
        max_workers=1,
    )
    assert fetched == []
    assert records == []
    assert skipped and {item["reason"] for item in skipped} == {"could not fetch issue body"}


def test_gh_capacity_failure_branches_and_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _forbid_processes(monkeypatch)
    monkeypatch.setattr(gh_capacity, "HANDOFF", tmp_path)
    ledger = tmp_path / "gh-rate-ledger.ndjson"
    monkeypatch.setattr(gh_capacity, "GH_LEDGER", ledger)
    monkeypatch.delenv("GH_RESERVE_SEARCH", raising=False)
    assert gh_capacity._env_int("GH_RESERVE_SEARCH", 3) == 3
    monkeypatch.setenv("GH_RESERVE_SEARCH", "9")
    assert gh_capacity._env_int("GH_RESERVE_SEARCH", 3) == 9
    monkeypatch.setenv("GH_RESERVE_SEARCH", "nope")
    assert gh_capacity._env_int("GH_RESERVE_SEARCH", 3) == 3

    gh_capacity._append_ledger([])
    assert not ledger.exists()
    ledger.write_text(
        "\nnot-json\n"
        + json.dumps({"resource": "core", "ts": "yesterday"})
        + "\n"
        + json.dumps({"resource": "core", "ts": 10, "remaining": 1, "limit": 2})
        + "\n",
        encoding="utf-8",
    )
    rows = gh_capacity._ledger_rows("core")
    assert rows == [{"resource": "core", "ts": 10, "remaining": 1, "limit": 2}]
    assert gh_capacity._split_headers_body("HTTP/2 200") == ({}, "HTTP/2 200")

    def explode(*_a, **_k):
        raise RuntimeError("transport down")

    assert gh_capacity.probe(runner=explode) is None
    assert (
        gh_capacity.probe(
            runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="[", stderr="")
        )
        is None
    )
    probed = gh_capacity.probe(
        runner=lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"resources": {"search": {"remaining": 3}, "core": "blind"}}),
            stderr="",
        )
    )
    assert probed["search"]["remaining"] == 3
    probe_rows = [
        json.loads(line) for line in ledger.read_text().splitlines() if line.strip().startswith("{")
    ]
    assert not any(
        row.get("source") == "probe" and row.get("resource") == "core" for row in probe_rows
    )

    transport = gh_capacity._preflight_call(["user"], runner=explode, timeout_s=1)
    assert transport["transport_error"].startswith("RuntimeError")
    bad_json = gh_capacity._preflight_call(
        ["user"],
        runner=lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout="HTTP/2 200\n\nnot-json", stderr=""
        ),
        timeout_s=1,
    )
    assert bad_json["body"] is None and bad_json["status"] == 200
    no_status = gh_capacity._classify_call(
        {"rc": 0, "status": None, "headers": {}, "body": None, "stderr": ""}, now=1
    )
    assert no_status["verdict"] == "ok" and no_status["detail"] == "gh succeeded"
    assert (
        gh_capacity._measured_row(
            {"headers": {}}, {"verdict": "ok"}, source="gate", resource="core", now=1
        )
        is None
    )

    def http(cmd, **_kwargs):
        body = json.dumps({"login": "measured"})
        if "rate_limit" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"resources": {"search": {"limit": 30, "remaining": 30}}}),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout=f"HTTP/2 200\n\n{body}", stderr="")

    measured = gh_capacity.measure("core", runner=http, now=1)
    assert measured["row"] is None and "no x-ratelimit headers" in measured["detail"]

    def headed(cmd, **_kwargs):
        headers = "\n".join(
            [
                "HTTP/2 200",
                "x-ratelimit-remaining: 4",
                "x-ratelimit-limit: 5000",
                "x-ratelimit-resource: core",
                "",
                "",
            ]
        )
        return SimpleNamespace(returncode=0, stdout=headers + "{}", stderr="")

    def ledger_locked(_rows):
        raise OSError("read-only ledger")

    monkeypatch.setattr(gh_capacity, "_append_ledger", ledger_locked)
    kept = gh_capacity.measure("core", runner=headed, now=2)
    assert kept["row"]["remaining"] == 4, kept
    preflight = gh_capacity.auth_preflight(runner=headed, now=2, record=True)
    assert preflight["verdict"] == "ok", preflight

    monkeypatch.setattr(gh_capacity, "_append_ledger", lambda _rows: None)

    def reading_runner(cmd, **_kwargs):
        if "rate_limit" in cmd:
            payload = {
                "resources": {"search": {"limit": 30, "remaining": 30, "reset": 9, "used": 0}}
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        headers = "\n".join(
            [
                "HTTP/2 200",
                "x-ratelimit-remaining: 4000",
                "x-ratelimit-limit: 5000",
                "x-ratelimit-reset: 99",
                "x-ratelimit-used: 1000",
                "",
                "",
            ]
        )
        return SimpleNamespace(returncode=0, stdout=headers + '{"login":"me"}', stderr="")

    snap = gh_capacity.build(runner=reading_runner)
    assert set(snap["resources"]) == set(gh_capacity.TRACKED)
    assert snap["probe_ok"] is True

    monkeypatch.setenv("ORCH_GH_THROTTLE", "1")

    def throttle_breaks(*_a, **_k):
        raise RuntimeError("pace failed")

    monkeypatch.setattr(gh_capacity, "throttle", throttle_breaks)
    assert gh_capacity.throttle_if_enabled("core") is None

    monkeypatch.setattr(gh_capacity, "_gate", lambda resource: 0 if resource == "search" else 7)
    assert gh_capacity.main(["--gate"]) == 7
    assert gh_capacity.main(["--gate", "search"]) == 0
    monkeypatch.setattr(
        gh_capacity,
        "auth_preflight",
        lambda **_k: {"line": "gh: authenticated as fixture", "exit": 0},
    )
    assert gh_capacity.main(["--auth-preflight"]) == 0
    assert "authenticated as fixture" in capsys.readouterr().out
    assert started == []


def test_keepalive_shadow_backfill_defaults_and_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _forbid_processes(monkeypatch)
    corpus = tmp_path / "shadow.jsonl"

    def runner(cmd, **_kwargs):
        if "search" in cmd:
            return SimpleNamespace(returncode=0, stdout="o/r#1\no/r#2\n", stderr="")
        if "state,labels" in cmd and "1" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        if "state,labels" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"state": "MERGED", "labels": []}),
                stderr="",
            )
        if "comments" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh command {cmd}")

    def durability_breaks(*_a, **_k):
        raise RuntimeError("durability down")

    seeded = shadow.backfill(
        runner=runner, durability_fn=durability_breaks, corpus_path=corpus, days=3, grace_days=1
    )
    assert seeded["recorded"] == 1, seeded
    row = json.loads(corpus.read_text(encoding="utf-8"))
    assert row["outcome"] == "merged" and row["source"] == "backfill"

    real_backfill = shadow.backfill

    def exercise_fallback(*args, **kwargs):
        fallback = kwargs.get("runner")
        if fallback is not None:
            fallback(["durability-view"], capture_output=True, text=True)
        return real_backfill(*args, **kwargs)

    monkeypatch.setattr(shadow, "backfill", exercise_fallback)
    shadow._selftest()
    monkeypatch.setattr(shadow, "backfill", real_backfill)

    monkeypatch.setattr(shadow, "summarize", lambda _path: {"n": 0})
    monkeypatch.setattr(
        shadow, "gather_signals", lambda _target: {"pr_state": "open", "outcome": None}
    )
    monkeypatch.setattr(shadow, "shadow_decide", lambda _signals: {"shadow_action": "wait"})
    monkeypatch.setattr(shadow, "record", lambda _decision, _corpus: {"appended": 1})
    monkeypatch.setattr(
        shadow,
        "backfill",
        lambda **_k: {"since": _k.get("since"), "recorded": 0},
    )
    assert shadow.main(["--summarize", "--corpus", str(corpus)]) == 0
    assert shadow.main(["--backfill", "--corpus", str(corpus)]) == 0
    assert shadow.main(["--shadow", "o/r#9", "--corpus", str(corpus)]) == 0
    messages: list[str] = []

    def error_returns(self, message):
        messages.append(message)

    monkeypatch.setattr(argparse.ArgumentParser, "error", error_returns)
    assert shadow.main(["--corpus", str(corpus)]) == 2
    assert messages and "required" in messages[0]
    assert "shadow_action" in capsys.readouterr().out
    assert started == []
    assert os.environ.get("ORCH_STATE_DIR")
