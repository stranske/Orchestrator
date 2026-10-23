"""Offline runtime-adapter contracts; never contact providers or live GitHub/state."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import claims
import closer_dispatcher as adapter
import closer_evidence
import closer_pipeline
import closer_queue

TARGET = "o/r#7"
SOURCE = "o/r#2"
HEAD = "a" * 40
MERGE = "b" * 40


@pytest.fixture(autouse=True)
def no_live_state_or_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "private-handoff"))

    def semantic_route(route, invoke, *, load, **_kwargs):
        try:
            return invoke(route), route
        finally:
            adapter._release_route_load(route, load)

    monkeypatch.setattr(adapter, "_execute_profile_route", semantic_route)
    monkeypatch.setattr(claims, "claim", lambda *a, **kw: True)
    monkeypatch.setattr(claims, "release", lambda *a, **kw: True)
    monkeypatch.setattr(claims, "update_metadata", lambda *a, **kw: True)
    monkeypatch.setattr(claims, "holder", lambda target: None)
    monkeypatch.setattr(adapter.gh_capacity, "throttle", lambda *a, **kw: {"action": "allow"})

    def forbidden(*args, **kwargs):
        raise AssertionError("live subprocess/provider/API call forbidden in adapter tests")

    monkeypatch.setattr(adapter.subprocess, "run", forbidden)
    monkeypatch.setattr(adapter.subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapter.gh_capacity, "gh_run", forbidden)


def stub_action_readiness_for_transport_test(monkeypatch):
    """These transport units start after admission; real collector admission is tested separately."""
    monkeypatch.setattr(
        closer_evidence,
        "action_evidence_readiness",
        lambda *_a, **_k: {
            "schema": "closer-action-evidence-readiness/v1",
            "action": _a[2],
            "ready": True,
            "mode": "acceptance",
            "gaps": [],
        },
    )


def snapshot(**overrides):
    result = {
        "target": TARGET,
        "kind": "pr",
        "state": "OPEN",
        "head": HEAD,
        "head_branch": "feature",
        "head_repository": "o/r",
        "merge": None,
        "body": "Delivery",
        "draft": False,
        "sources": [
            {
                "target": SOURCE,
                "body": "## Acceptance criteria\n- Preserve behavior",
                "state": "open",
            }
        ],
        "review_revision": "review-1",
        "active_threads": 0,
        "threads": [],
        "checks": "SUCCESS",
        "mergeable": "MERGEABLE",
        "labels": [],
        "verifier": None,
    }
    result.update(overrides)
    result["revision"] = adapter.digest(result)
    return result


def test_local_packet_progresses_with_github_unavailable_but_remote_action_waits(
    tmp_path, monkeypatch
):
    class OfflineGitHub:
        deadline = time.time() + 30

        def __init__(self):
            self.calls = 0

        def snapshot(self, _target):
            self.calls += 1
            raise ConnectionError("GitHub unavailable")

    gh = OfflineGitHub()
    local = snapshot()
    backend = adapter.Closer(
        gh,
        state_path=tmp_path / "queue.json",
        seed_snapshots={TARGET: local},
        local_first=True,
        apply=True,
        workers=True,
    )
    backend.select = lambda _s: ("review", "local review", True, None)
    backend.worker = lambda *_args, **_kwargs: {"status": "completed", "local_result": True}
    observed = backend.observe(TARGET)
    assert observed["action"] == "review" and observed["eligible"]
    result = backend.execute(TARGET, "review", observed, {"deadline": time.time() + 20})
    assert result["status"] == "completed" and result["local_result"]
    assert gh.calls == 0

    backend.select = lambda _s: ("merge", "remote merge", True, None)
    observed = backend.observe(TARGET)
    remote = backend.execute(TARGET, "merge", observed, {"deadline": time.time() + 20})
    assert remote["status"] == "not_started"
    assert remote["next_trigger"] == "targeted_remote_freshness_recheck"
    assert gh.calls == 1


def test_offline_retained_packet_runs_through_durable_queue(tmp_path):
    class OfflineGitHub:
        def __init__(self):
            self.calls = 0

        def snapshot(self, _target):
            self.calls += 1
            raise ConnectionError("offline")

    gh = OfflineGitHub()
    backend = adapter.Closer(
        gh,
        state_path=tmp_path / "queue.json",
        seed_snapshots={TARGET: snapshot()},
        local_first=True,
        apply=True,
        workers=True,
    )
    backend.select = lambda _s: ("review", "local review", True, None)
    backend.worker = lambda *_args, **_kwargs: {"status": "completed", "local_result": True}
    receipt = closer_queue.run_round(
        backend.state_path,
        [TARGET],
        backend.observe,
        backend.execute,
        reconcile=backend.reconcile,
        deadline=time.time() + 360,
        max_workers=1,
        max_passes=1,
    )
    row = next(row for row in receipt["accounting"] if row["target"] == TARGET)
    assert row["action"] == "review"
    assert row["operation_status"] == "completed"
    assert row["status"] == "deferred" and row["reason"] == "no_progress"
    durable = closer_queue._read_state(backend.state_path)["items"][TARGET]
    assert durable["operation"]["result"]["local_result"] is True
    assert gh.calls == 0


def test_pipeline_keeps_local_io_capacity_when_github_capacity_is_unavailable(monkeypatch):
    monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: None)
    monkeypatch.setattr(
        adapter.gh_capacity, "state", lambda *_args: (_ for _ in ()).throw(ConnectionError())
    )
    assert adapter._pipeline_capacity() == {"io": 4, "low": 0, "exception": 0}


@pytest.mark.parametrize("stale_binding", [None, "source", "reviews", "dispositions"])
def test_staged_prepare_reuses_retained_packet_during_github_outage(
    tmp_path, monkeypatch, stale_binding
):
    current = snapshot(base=MERGE)
    state = tmp_path / "state.json"
    packet = {
        "schema": "closer-evidence/v1",
        "collection_action": "collect_evidence",
        "head": HEAD,
        "base": MERGE,
        "bindings": {
            "source": closer_evidence._hash(current["sources"]),
            "reviews": closer_evidence._hash(current["review_revision"]),
            "dispositions": closer_evidence._hash({}),
        },
        "manifest": {"packet_sha256": "retained"},
        "sections": {"source": {"status": "complete", "content": current["sources"]}},
    }
    if stale_binding:
        packet["bindings"][stale_binding] = "stale-binding"
    packet_path = tmp_path / "packet.json"
    packet_bytes = json.dumps(packet).encode()
    packet_path.write_bytes(packet_bytes)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_bytes = json.dumps(current).encode()
    snapshot_path.write_bytes(snapshot_bytes)
    prior = {
        "revision": current["revision"],
        "snapshot": {"target": TARGET, "revision": current["revision"]},
        "packet_ref": {
            "path": str(packet_path),
            "sha256": hashlib.sha256(packet_bytes).hexdigest(),
        },
        "snapshot_ref": {
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        },
    }
    adapter._pipeline_state_path(state).write_text(
        json.dumps({"runs": {"prior": {"targets": {TARGET: {"results": {"prepare": prior}}}}}})
    )

    class OfflineGitHub:
        def __init__(self, *, deadline):
            self.deadline = deadline

        def snapshot(self, _target):
            raise ConnectionError("offline")

    monkeypatch.setattr(adapter, "GitHub", OfflineGitHub)
    monkeypatch.setattr(
        closer_evidence,
        "validate_packet",
        lambda p: isinstance(p, dict) and p.get("schema") == "closer-evidence/v1",
    )
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    monkeypatch.setattr(
        closer_evidence,
        "collect_evidence",
        lambda *_a, **_k: pytest.fail("retained packet must be reused"),
    )
    prepared = {}

    def run_pipeline(_path, targets, **callbacks):
        prepared.update(callbacks["prepare"](targets[0], {"deadline": time.time() + 30}))
        return {"valid": True}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            EVIDENCE_REFUSAL_REASONS=closer_pipeline.EVIDENCE_REFUSAL_REASONS,
        ),
    )
    backend = closer(tmp_path, current)
    backend.local_first = True
    args = SimpleNamespace(state=str(state), max_workers=1, shadow=False)
    if stale_binding:
        with pytest.raises(ConnectionError, match="offline"):
            adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 330)
        assert prepared == {}
    else:
        adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 330)
        assert prepared["snapshot"]["_local_cached"] is True
        assert prepared["remote_fresh"] is False
        assert prepared["revision"] == current["revision"]


class FakeGitHub:
    def __init__(self, current, responses=None):
        self.current = current
        self.deadline = time.time() + 2700
        self.responses = responses or {}
        self.calls = []
        self.reads = 0

    def snapshot(self, target):
        self.reads += 1
        return copy.deepcopy(self.current)

    def api(self, endpoint, *, method="GET", fields=None, **kwargs):
        self.calls.append((method, endpoint, fields))
        result = self.responses.get((method, endpoint), {})
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def integrated(tmp_path, current):
    evidence = tmp_path / "observed.json"
    evidence.write_text('{"actual_check": "PASS"}')
    ref = {"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
    return {
        "reviewer": "senior-coordinator",
        "head": current["head"],
        "source_digest": adapter.source_digest(current["sources"]),
        "review_revision": current["review_revision"],
        "criteria": [
            {
                "source": source["target"],
                "criterion": criterion,
                "result": "PASS",
                "evidence": [ref],
            }
            for source in current["sources"]
            for criterion in [source["body"], *adapter.requirements(source["body"])]
        ],
    }


def closer(tmp_path, current, **kwargs):
    return adapter.Closer(FakeGitHub(current), state_path=tmp_path / "state.json", **kwargs)


def operation(action="merge"):
    return {
        "id": "op-1",
        "owner": "offline-test-owner",
        "action": action,
        "binding": {"head": HEAD, "merge": MERGE, "pr_target": TARGET, "source_targets": [SOURCE]},
        "deadline": time.time() + 60,
    }


def _terminal_queue_state(tmp_path):
    path = tmp_path / "legacy-queue.json"
    finished_at = 1_700_000_000.0
    receipt = closer_queue.run_round(
        path,
        [TARGET],
        lambda _: {
            "revision": "terminal-revision",
            "action": "complete",
            "eligible": False,
            "evidence": {},
        },
        lambda *_: pytest.fail("complete action must not execute"),
        deadline=finished_at + 1200,
        receipt_reserve_seconds=0,
        now=lambda: finished_at,
    )
    assert receipt["stop_valid"] is True
    return json.loads(path.read_text())


def test_terminal_valid_legacy_round_gets_a_fresh_cli_budget(tmp_path):
    previous = _terminal_queue_state(tmp_path)
    state_path = tmp_path / "legacy-queue.json"
    state_path.write_text(json.dumps(previous))

    requested = time.time() + 2700
    assert previous["round"]["deadline"] < time.time()
    assert adapter._validly_finished_legacy_round(state_path, previous) is True
    assert adapter._deadline_with_legacy_round(requested, previous, state_path) == requested


def test_claim_held_row_without_a_real_claimant_cannot_renew_budget(tmp_path, monkeypatch):
    state_path = tmp_path / "legacy-queue.json"
    finished_at = 1_700_000_000.0
    receipt = closer_queue.run_round(
        state_path,
        [TARGET],
        lambda _: {
            "revision": "claim-held-revision",
            "action": "merge",
            "eligible": True,
            "reason": None,
            "next_trigger": None,
            "evidence": {},
        },
        lambda *_: pytest.fail("claim conflict must prevent execution"),
        deadline=finished_at + 1200,
        receipt_reserve_seconds=0,
        now=lambda: finished_at,
        claim_fn=lambda *_: False,
        holder_fn=lambda _target: {"agent": "somebody-else"},
        release_fn=lambda *_: True,
    )
    assert receipt["stop_valid"] is True
    state = json.loads(state_path.read_text())
    monkeypatch.setattr(closer_queue.claims, "holder", lambda _target: None)

    requested = time.time() + 2700
    assert adapter._validly_finished_legacy_round(state_path, state) is False
    assert (
        adapter._deadline_with_legacy_round(requested, state, state_path)
        == state["round"]["deadline"]
    )


def test_unfinished_legacy_round_keeps_original_deadline(tmp_path):
    previous = _terminal_queue_state(tmp_path)
    state_path = tmp_path / "legacy-queue.json"
    previous["round"]["status"] = "running"
    old_deadline = time.time() + 45
    previous["round"]["deadline"] = old_deadline

    requested = time.time() + 2700
    assert adapter._validly_finished_legacy_round(state_path, previous) is False
    assert adapter._deadline_with_legacy_round(requested, previous, state_path) == old_deadline


def test_malformed_terminal_receipt_cannot_renew_the_legacy_budget(tmp_path):
    previous = _terminal_queue_state(tmp_path)
    state_path = tmp_path / "legacy-queue.json"
    previous["round"]["receipt"]["accounted_targets"] = []
    old_deadline = time.time() + 45
    previous["round"]["deadline"] = old_deadline
    state_path.write_text(json.dumps(previous))

    requested = time.time() + 2700
    assert adapter._validly_finished_legacy_round(state_path, previous) is False
    assert adapter._deadline_with_legacy_round(requested, previous, state_path) == old_deadline


def test_malformed_legacy_round_without_a_deadline_fails_closed():
    with pytest.raises(RuntimeError, match="no safe deadline"):
        adapter._deadline_with_legacy_round(
            time.time() + 2700, {"round": {"status": "running"}}, "missing-state.json"
        )


@pytest.mark.parametrize(
    ("case", "expect_fresh_budget"),
    [("finished_valid", True), ("running", False), ("finished_malformed", False)],
)
def test_cli_staged_pipeline_deadline_uses_nested_valid_queue_receipt(
    tmp_path, monkeypatch, case, expect_fresh_budget
):
    state_path = tmp_path / "legacy-state.json"
    receipt = closer_queue.run_round(
        state_path,
        [TARGET],
        lambda _: {
            "revision": "terminal-revision",
            "action": "complete",
            "eligible": False,
            "evidence": {},
        },
        lambda *_: pytest.fail("complete action must not execute"),
        deadline=1_700_001_200,
        receipt_reserve_seconds=0,
        now=lambda: 1_700_000_000.0,
    )
    assert receipt["stop_valid"] is True
    state = json.loads(state_path.read_text())
    prior_deadline = state["round"]["deadline"]
    if case == "running":
        state["round"]["status"] = "running"
    elif case == "finished_malformed":
        state["round"]["receipt"]["accounted_targets"] = []
    state_path.write_text(json.dumps(state))

    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    observed = {}

    class FakeBackend:
        def __init__(self, github, **_kwargs):
            observed["github_deadline"] = github.deadline

    def run_staged(_args, _targets, _backend, deadline):
        observed["pipeline_deadline"] = deadline
        return {"status": "stubbed"}

    monkeypatch.setattr(adapter, "Closer", FakeBackend)
    monkeypatch.setattr(adapter, "GitHub", lambda *, deadline: SimpleNamespace(deadline=deadline))
    monkeypatch.setattr(adapter, "known_workspaces", lambda _targets: {})
    monkeypatch.setattr(adapter, "_run_staged_pipeline", run_staged)
    import closer_pipeline

    monkeypatch.setattr(closer_pipeline, "validate_stop", lambda *_a, **_kw: {"valid": True})

    exit_code = adapter.main(
        [
            "run",
            "--assignment",
            str(assignment_path),
            "--state",
            str(state_path),
            "--budget-seconds",
            "10",
            "--apply",
            "--workers",
            "--pipeline",
        ]
    )
    assert exit_code == 0
    assert observed["github_deadline"] == observed["pipeline_deadline"]
    if expect_fresh_budget:
        assert observed["pipeline_deadline"] > prior_deadline
    else:
        assert observed["pipeline_deadline"] == min(time.time() + 10 + 300, prior_deadline)


@pytest.mark.parametrize("decision", [{"ready": True}, {"reviewer": "luna", "ready": True}, {}])
def test_green_ci_or_model_ready_is_not_acceptance(tmp_path, decision):
    current = snapshot()
    backend = closer(tmp_path, current, dispositions={TARGET: decision})
    # A model's ready flag still cannot grant final source acceptance. It also
    # must not force final acceptance proof ahead of an otherwise guarded merge.
    assert adapter.acceptance_ok(current, decision) is False
    action, _, eligible, _ = backend.select(current)
    assert action == "merge" and eligible


def test_integrated_current_source_evidence_can_admit_guarded_merge(tmp_path):
    current = snapshot()
    backend = closer(tmp_path, current, dispositions={TARGET: integrated(tmp_path, current)})
    assert backend.select(current)[0] == "merge"


@pytest.mark.parametrize("changed", ["head", "review", "source"])
def test_mutation_boundary_rejects_changed_head_review_or_source(tmp_path, monkeypatch, changed):
    old = snapshot()
    fresh = copy.deepcopy(old)
    if changed == "head":
        fresh["head"] = "c" * 40
    elif changed == "review":
        fresh["review_revision"] = "review-2"
    else:
        fresh["sources"][0]["body"] += "\n- New requirement"
    fresh["revision"] = adapter.digest(fresh)
    calls = []
    monkeypatch.setitem(
        sys.modules, "merge_guard", SimpleNamespace(guarded_merge=lambda *a, **k: calls.append(k))
    )
    backend = closer(tmp_path, fresh, apply=True, dispositions={TARGET: integrated(tmp_path, old)})
    assert backend.execute(TARGET, "merge", old, operation())["status"] == "stale"
    assert calls == [] and backend.gh.calls == []


def test_read_only_execute_does_not_refresh_mutate_or_dispatch(tmp_path):
    current = snapshot()
    backend = closer(tmp_path, current, dispositions={TARGET: integrated(tmp_path, current)})
    assert backend.execute(TARGET, "merge", current, operation())["status"] == "deferred"
    assert backend.gh.reads == 0 and backend.gh.calls == []
    assert not (tmp_path / "state.json").exists()


def test_wait_is_specific_to_item_not_global_fleet_gate(tmp_path):
    waiting = snapshot(checks="PENDING")
    backend = closer(tmp_path, waiting)
    assert backend.select(waiting)[0] == "wait"
    ready = snapshot(target="o/other#8")
    backend.dispositions[ready["target"]] = integrated(tmp_path, ready)
    assert backend.select(ready)[0] == "merge"


def test_source_alias_is_advertised_to_scheduler_for_unique_writer_claims(tmp_path):
    current = snapshot()
    backend = closer(tmp_path, current, dispositions={TARGET: integrated(tmp_path, current)})
    observed = backend.observe(TARGET)
    assert set(observed["claim_targets"]) == {TARGET, SOURCE}


@pytest.mark.parametrize("where", ["pr", "source"])
def test_pause_label_blocks_automatic_completion(tmp_path, where):
    current = snapshot()
    if where == "pr":
        current["labels"] = ["agents:auto-pilot-pause"]
    else:
        current["sources"][0]["labels"] = ["agents:auto-pilot-pause"]
    backend = closer(tmp_path, current, dispositions={TARGET: integrated(tmp_path, current)})
    assert backend.select(current)[0] == "wait"


@pytest.mark.parametrize(
    "state,labels,expected",
    [
        ("OPEN", ["needs-human"], "merge"),
        ("MERGED", ["needs-human", "verify:compare"], "post_merge_verify"),
    ],
)
def test_generic_needs_human_label_is_not_a_scoped_decision(tmp_path, state, labels, expected):
    current = snapshot(state=state, labels=labels, merge=MERGE if state == "MERGED" else None)
    backend = closer(tmp_path, current)
    assert backend.select(current)[0] == expected


def test_generic_needs_human_preserves_actual_owner_blocker(tmp_path):
    current = snapshot(labels=["needs-human"])
    backend = closer(
        tmp_path,
        current,
        blockers={TARGET: {"reason": "owner confirmed required human input"}},
    )
    selected = backend.select(current)
    assert selected[0] == "wait"
    assert "scoped owner blocker " + TARGET in selected[1]


def test_source_alias_preserves_actual_delivery_owner_blocker(tmp_path):
    current = snapshot(target=SOURCE, pr_target=TARGET)
    backend = closer(
        tmp_path,
        current,
        blockers={TARGET: {"reason": "owner confirmed required human input"}},
    )
    selected = backend.select(current)
    assert selected[0] == "wait"
    assert "scoped owner blocker " + TARGET in selected[1]


def test_unparsed_normative_acceptance_prose_cannot_disappear(tmp_path):
    current = snapshot()
    current["sources"][0][
        "body"
    ] += "\n\nThe migration must preserve every existing consumer override."
    decision = integrated(tmp_path, current)
    prose = "The migration must preserve every existing consumer override."
    decision["criteria"] = [row for row in decision["criteria"] if row["criterion"] != prose]
    assert not adapter.acceptance_ok(current, decision)


def test_mutated_retained_artifact_invalidates_acceptance(tmp_path):
    current = snapshot()
    decision = integrated(tmp_path, current)
    (tmp_path / "observed.json").write_text("changed after senior integration")
    assert not adapter.acceptance_ok(current, decision)


def test_outdated_thread_is_retained_without_premerge_review_requirement(tmp_path):
    current = snapshot(
        active_threads=0, threads=[{"id": "thread", "isResolved": False, "isOutdated": True}]
    )
    backend = closer(tmp_path, current)
    assert backend.select(current)[0] == "merge"
    assert current["threads"][0]["isResolved"] is False


def test_existing_active_verifier_is_reused_without_duplicate_request(tmp_path):
    current = snapshot(state="MERGED", merge=MERGE)
    backend = closer(tmp_path, current, apply=True)
    # Earlier status inventories are complete, rather than FakeGitHub's unknown {}.
    backend.gh.responses[
        ("GET", "repos/o/r/actions/workflows/agents-verifier.yml/runs?per_page=100&status=queued")
    ] = {"total_count": 0, "workflow_runs": []}
    endpoint = (
        "repos/o/r/actions/workflows/agents-verifier.yml/runs?per_page=100&status=in_progress"
    )
    backend.gh.responses[("GET", endpoint)] = {
        "total_count": 1,
        "workflow_runs": [
            {
                "id": 99,
                "status": "in_progress",
                "pull_requests": [{"number": 7}],
                "run_attempt": 1,
                "head_sha": current["head"],
                "conclusion": None,
                "path": ".github/workflows/agents-verifier.yml",
                "event": "pull_request_target",
                "repository": {"full_name": "o/r"},
            }
        ],
    }
    result = backend.initiate_verify(TARGET, current)
    assert result["status"] == "resource_wait"
    assert result["verifier_resource_wait"]["target"] == TARGET
    assert result["verifier_resource_wait"]["blocking_run"]["id"] == 99
    assert all(method == "GET" for method, _, _ in backend.gh.calls)


def test_unknown_comment_write_is_not_blindly_repeated_by_reconciliation(tmp_path):
    current = snapshot(active_threads=1)
    decision = integrated(tmp_path, current)
    decision["response"] = decision["criteria"][0]["evidence"][0]
    backend = closer(tmp_path, current, apply=True, dispositions={TARGET: decision})
    backend.gh.responses[("POST", "repos/o/r/issues/7/comments")] = RuntimeError(
        "connection lost after submission"
    )
    op = operation("respond_review")
    with pytest.raises(RuntimeError, match="connection lost"):
        backend.execute(TARGET, "respond_review", current, op)
    before = list(backend.gh.calls)
    assert backend.reconcile(TARGET, op)["status"] == "unknown"
    assert backend.gh.calls == before


def test_unknown_closure_write_is_not_blindly_repeated(tmp_path, monkeypatch):
    current = snapshot(state="MERGED", merge=MERGE, verifier={"verdict": "PASS", "run_id": "42"})
    backend = closer(
        tmp_path, current, apply=True, dispositions={TARGET: integrated(tmp_path, current)}
    )
    calls = []

    def uncertain_runner(command, *, timeout):
        calls.append((command, timeout))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"errors": ["closure outcome unknown"], "closed": []})
        )

    backend.run_fn = uncertain_runner
    monkeypatch.setitem(
        sys.modules, "keepalive_supervisor", SimpleNamespace(close_verified_issues=object())
    )
    monkeypatch.setenv("ORCH_SUPERVISOR_CLOSE", "1")
    with pytest.raises(RuntimeError, match="closure actor outcome"):
        backend.close_sources(TARGET, current)
    assert calls[0][0][-1] == TARGET and 0 < calls[0][1] <= 2700
    assert backend.reconcile(TARGET, operation("close_source"))["status"] == "unknown"
    assert len(calls) == 1 and backend.gh.calls == []


def test_disappearing_sources_do_not_confirm_unknown_closure(tmp_path):
    backend = closer(tmp_path, snapshot(state="MERGED", merge=MERGE, sources=[]))
    assert backend.reconcile(TARGET, operation("close_source"))["status"] != "completed"


def assert_nonpass_accounting_prompt_contract(prompt):
    for required in (
        "This path requires at least one supplied nonpass_finding",
        "Only when ALL supplied nonpass_finding exact finding_id values are confidently accounted for",
        "wait, collect_evidence, post_merge_verify for a bounded evaluable question with retained new material, or repair with a validated bound mechanical plan",
        "For post_merge_verify, supply verification_request {question, material:[exact selector]}",
        "select only specific necessary retained material that makes the question evaluable",
        "Legacy reports without an event digest additionally require the supplied genuine necessary recovery progress",
        "substantive_needs [] if there is no OTHER semantic, ownership, security, or routing ambiguity",
        "missing acceptance proof in each unresolved_missing_evidence resolution's reason",
        "preserve every raw NON_PASS verdict, and leave decision.criteria empty",
        "Accounting never grants PASS or acceptance authority",
        "Resolved duplicate/nonissue claims still require exact raw evidence/proofs",
        "repair still requires the existing coordinator authorization",
        "Security/high-risk findings require senior review with classification uncertain and nonempty substantive_needs",
        "If any finding identity, meaning, ownership, security implication, or safe routing remains uncertain",
        "return classification uncertain with nonempty substantive_needs",
        "do not hide uncertainty to fit the accounting path",
    ):
        assert required in prompt
    assert "Otherwise return classification uncertain" not in prompt


def test_luna_result_is_advisory_and_cannot_supply_completion_disposition(tmp_path, monkeypatch):
    current = snapshot(
        state="MERGED",
        merge=MERGE,
        verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
    )
    calls = []
    _seed_exception_capacity(adapter.os.environ["HANDOFF_DIR"])
    monkeypatch.setitem(sys.modules, "capacity", SimpleNamespace(shed_marker=lambda agent: None))
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace())

    def offload(*args, **kwargs):
        calls.append((args, kwargs))
        return {"exit": 0, "output": '{"ready":true,"verdict":"PASS","merge":true}'}

    backend = closer(
        tmp_path,
        current,
        apply=True,
        workers=True,
        workspaces={"o/r": str(tmp_path)},
        offload_fn=offload,
    )
    result = backend.worker(TARGET, "collect_evidence", current, operation("collect_evidence"))
    assert result["advisory_only"] and result["status"] == "completed"
    assert calls[0][1]["profile_id"] == "codex-6-luna-low"
    assert backend.dispositions == {}
    assert backend.select(current)[0] == "wait"
    assert backend.gh.calls == []


def test_selective_exception_prompt_accounts_for_nonpass_without_pass_authority(
    tmp_path, monkeypatch
):
    current, prompts = snapshot(), []
    _seed_exception_capacity(adapter.os.environ["HANDOFF_DIR"])
    monkeypatch.setitem(sys.modules, "capacity", SimpleNamespace(shed_marker=lambda agent: None))
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace())

    def offload(_agent, prompt, **kwargs):
        prompts.append(prompt)
        return {"exit": 0, "output": '{"classification":"uncertain"}'}

    backend = closer(
        tmp_path,
        current,
        apply=True,
        workers=True,
        workspaces={"o/r": str(tmp_path)},
        offload_fn=offload,
    )
    backend.worker(
        TARGET,
        "collect_evidence",
        current,
        operation("collect_evidence"),
        advisory_context={"nonpass_findings": [{"finding_id": "fixture-finding"}]},
    )
    assert_nonpass_accounting_prompt_contract(prompts[0])
    assert "Never claim PASS for a raw NON_PASS verifier result" in prompts[0]
    assert backend.dispositions == {} and backend.gh.calls == []


def test_senior_acceptance_survives_own_source_state_transition(tmp_path):
    current = snapshot(state="MERGED", merge=MERGE, verifier={"verdict": "PASS", "run_id": "42"})
    decision = integrated(tmp_path, current)
    current["sources"][0]["state"] = "closed"
    backend = closer(tmp_path, current, dispositions={TARGET: decision})
    assert adapter.acceptance_ok(current, decision)
    assert backend.select(current)[0] == "complete"


def test_unknown_merge_cannot_be_credited_to_a_changed_head(tmp_path):
    backend = closer(tmp_path, snapshot(state="MERGED", merge=MERGE, head="c" * 40))
    op = {**operation("merge"), "expected_head": HEAD, "expected_merge": MERGE}
    assert backend.reconcile(TARGET, op)["status"] != "completed"


def test_unknown_verification_cannot_be_credited_to_a_changed_merge(tmp_path):
    backend = closer(tmp_path, snapshot(state="MERGED", merge="d" * 40, labels=["verify:compare"]))
    op = {**operation("post_merge_verify"), "expected_head": HEAD, "expected_merge": MERGE}
    assert backend.reconcile(TARGET, op)["status"] != "completed"


def test_merge_calls_existing_guard_with_exact_head_and_bounded_timeout(tmp_path, monkeypatch):
    current = snapshot()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"merge_executed": True, "blocked": False})
        )

    # Free-form original requests do not need invented Tasks/AC headings or
    # final acceptance rows before the native merge guard can evaluate the PR.
    current = snapshot(
        sources=[{"target": SOURCE, "body": "Fix the displayed total.", "state": "open"}]
    )
    backend = closer(tmp_path, current, apply=True)
    backend.run_fn = fake_run
    assert backend.execute(TARGET, "merge", current, operation())["status"] == "completed"
    command, kwargs = calls[0]
    assert command[1].endswith("merge_guard.py")
    assert command[command.index("--expected-head") + 1] == HEAD
    assert "--confirm-merge" in command and 0 < kwargs["timeout"] <= 60
    assert backend.gh.calls == []


def test_invalid_integrated_thread_identity_blocks_before_comment(tmp_path):
    current = snapshot(active_threads=1, threads=[{"id": "actual", "isResolved": False}])
    decision = integrated(tmp_path, current)
    decision.update(response=decision["criteria"][0]["evidence"][0], resolve_threads=["unrelated"])
    backend = closer(tmp_path, current, apply=True, dispositions={TARGET: decision})
    assert (
        backend.execute(TARGET, "respond_review", current, operation("respond_review"))["status"]
        == "blocked"
    )
    assert backend.gh.calls == []


def _graphql_pr():
    def connection(nodes):
        return {"nodes": nodes, "pageInfo": {"hasNextPage": False}}

    return {
        "number": 7,
        "body": "Delivery",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "codex/delivery",
        "headRepository": {"nameWithOwner": "o/r"},
        "mergedAt": None,
        "mergeCommit": None,
        "updatedAt": "2026-10-17T00:00:00Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "labels": connection([]),
        "closingIssuesReferences": connection(
            [
                {
                    "number": 2,
                    "body": "## Tasks\n- Preserve behavior",
                    "state": "OPEN",
                    "repository": {"nameWithOwner": "o/r"},
                    "labels": connection([]),
                }
            ]
        ),
        "reviewThreads": connection([]),
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": HEAD,
                        "statusCheckRollup": {
                            "state": "SUCCESS",
                            "contexts": connection([]),
                        },
                    }
                }
            ]
        },
        "comments": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
    }


def _snapshot_from_document(monkeypatch, document, runs=None):
    github = adapter.GitHub(deadline=time.time() + 60)

    def fake_api(endpoint, **kwargs):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": document}}}
        if "actions/workflows/agents-verifier.yml/runs" in endpoint:
            return {"workflow_runs": runs or []}
        return {"pull_request": {}}

    monkeypatch.setattr(github, "api", fake_api)
    return github.snapshot(TARGET)


def test_snapshot_retains_complete_native_label_timeline(monkeypatch):
    document = _graphql_pr()
    document["timelineItems"] = {
        "nodes": [
            {
                "id": "LE_native_7",
                "createdAt": "2026-10-17T23:19:06Z",
                "label": {"name": "verify:compare"},
            }
        ],
        "pageInfo": {"hasPreviousPage": False},
    }
    current = _snapshot_from_document(monkeypatch, document)
    assert current["head_branch"] == "codex/delivery"
    assert current["head_repository"] == "o/r"
    assert current["label_events_complete"] is True
    assert current["label_events"] == [
        {
            "id": "LE_native_7",
            "created_at": "2026-10-17T23:19:06Z",
            "label": "verify:compare",
        }
    ]
    document["timelineItems"]["pageInfo"]["hasPreviousPage"] = True
    assert _snapshot_from_document(monkeypatch, document)["label_events"] == []


def _verified_label_fixture():
    now = time.time()
    event_at = now - 10

    def stamp(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

    current = snapshot(
        state="MERGED",
        merge=MERGE,
        merged_at=stamp(now - 100),
        labels=["verify:compare"],
        label_events_complete=True,
        label_events=[
            {"id": "LE_native_7", "created_at": stamp(event_at), "label": "verify:compare"}
        ],
        verifier_run_identity_unknown=True,
    )
    op = {
        **operation("post_merge_verify"),
        "status": "completed",
        "revision": "before-label-revision",
        "invoked_at": now - 15,
        "completed_at": now - 5,
        "result": {"verification_key": f"{TARGET}:{MERGE}:compare"},
    }
    inner = {
        "status": "deferred",
        "action": "collect_evidence",
        "observation_freshness": "fresh",
        "revision": current["revision"],
        "evidence": current,
        "operation": op,
    }
    row = {
        "target": TARGET,
        "status": "deferred",
        "action": "collect_evidence",
        "eligible": False,
        "reason": "collect terminal or unattributed verifier evidence; do not rerun",
        "next_trigger": "bounded worker capacity/profile or senior closer",
        "observation_freshness": "fresh",
        "revision": current["revision"],
        "operation_id": op["id"],
        "operation_status": "completed",
    }
    receipt = {
        "stop_valid": True,
        "accounting": [row],
        "events": [
            {
                "kind": "revision_refreshed",
                "target": TARGET,
                "revision": op["revision"],
            },
            {
                "kind": "operation_submitted",
                "target": TARGET,
                "action": "post_merge_verify",
                "operation_id": op["id"],
            },
        ],
        "progress": [{"target": TARGET, "action": "post_merge_verify"}],
    }
    return row, receipt, inner


@pytest.mark.parametrize(
    "changed",
    [
        "wrong_head",
        "wrong_merge",
        "uncertain_operation",
        "absent_event",
        "stale_revision",
        "invalid_stop",
    ],
)
def test_verified_label_wait_rejects_unbound_progress(changed):
    row, receipt, inner = _verified_label_fixture()
    if changed == "wrong_head":
        inner["operation"]["binding"]["head"] = "c" * 40
    elif changed == "wrong_merge":
        inner["operation"]["binding"]["merge"] = "c" * 40
    elif changed == "uncertain_operation":
        inner["operation"]["status"] = "unknown"
    elif changed == "absent_event":
        inner["evidence"]["label_events"] = []
    elif changed == "stale_revision":
        inner["revision"] = "stale"
    else:
        receipt["stop_valid"] = False
    assert adapter._verified_post_merge_label_pending(TARGET, row, receipt, inner) is None


def test_verified_label_wait_survives_new_queue_receipt_without_old_events():
    row, receipt, inner = _verified_label_fixture()
    receipt["events"] = []
    receipt["progress"] = []
    pending = adapter._verified_post_merge_label_pending(TARGET, row, receipt, inner)
    assert pending["status"] == "pending"
    assert pending["pending_evidence"]["event_id"] == "LE_native_7"
    again = adapter._verified_post_merge_label_pending(TARGET, row, receipt, inner)
    assert again["pending_evidence"] == pending["pending_evidence"]
    inner["evidence"]["verifier"] = {"verdict": "PASS"}
    assert adapter._verified_post_merge_label_pending(TARGET, row, receipt, inner) is None


def _active_verifier_fixture():
    row, receipt, inner = _verified_label_fixture()
    current = inner["evidence"]
    run_id = 39078553053
    current["verifier_runs"] = [
        {
            "id": run_id,
            "status": "in_progress",
            "conclusion": None,
            "run_attempt": 1,
            "head_sha": HEAD,
            "path": ".github/workflows/agents-verifier.yml",
            "event": "pull_request_target",
            "linked_pr": TARGET,
            "created_at": datetime.fromtimestamp(time.time() - 8, timezone.utc).isoformat(),
        }
    ]
    current["verifier_run_identity_unknown"] = False
    current["revision"] = adapter.digest({k: v for k, v in current.items() if k != "revision"})
    inner.update(action="wait", revision=current["revision"])
    row.update(
        action="wait",
        reason=f"existing verifier run {run_id} active",
        revision=current["revision"],
    )
    receipt["events"] = []
    receipt["progress"] = []
    return row, receipt, inner


@pytest.mark.parametrize(
    "changed",
    [
        "unattributed",
        "wrong_head",
        "wrong_merge",
        "terminal",
        "unknown_status",
        "wrong_target",
        "stale_revision",
        "invalid_stop",
        "wrong_workflow",
    ],
)
def test_active_verifier_wait_requires_exact_linked_live_run(changed):
    row, receipt, inner = _active_verifier_fixture()
    run = inner["evidence"]["verifier_runs"][0]
    if changed == "unattributed":
        run.pop("linked_pr")
    elif changed == "wrong_head":
        run["head_sha"] = "c" * 40
    elif changed == "wrong_merge":
        inner["evidence"]["merge"] = "not-a-merge-sha"
    elif changed == "terminal":
        run.update(status="completed", conclusion="success")
    elif changed == "unknown_status":
        run["status"] = "unknown"
    elif changed == "wrong_target":
        inner["evidence"]["target"] = "o/r#8"
    elif changed == "stale_revision":
        inner["revision"] = "stale"
    elif changed == "invalid_stop":
        receipt["stop_valid"] = False
    else:
        run["path"] = ".github/workflows/unrelated.yml"
    assert adapter._verified_active_verifier_pending(TARGET, row, receipt, inner) is None


def test_active_verifier_wait_survives_later_cadence_without_native_attribution():
    row, receipt, inner = _active_verifier_fixture()
    pending = adapter._verified_active_verifier_pending(TARGET, row, receipt, inner)
    assert pending["status"] == "pending"
    assert pending["pending_kind"] == "async_operation"
    assert pending["pending_evidence"]["operation_id"] == "39078553053"
    assert pending["pending_evidence"]["workflow_run_id"] == 39078553053
    assert adapter._verified_active_verifier_pending(TARGET, row, receipt, inner) == pending
    inner["evidence"]["verifier"] = {"verdict": "NON_PASS"}
    assert adapter._verified_active_verifier_pending(TARGET, row, receipt, inner) is None


def test_staged_action_waits_on_confirmed_native_label_after_completed_verify(
    tmp_path, monkeypatch
):
    row, receipt, inner = _verified_label_fixture()
    current = inner["evidence"]

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    monkeypatch.setattr(
        adapter,
        "_pipeline_low_route",
        lambda **_kwargs: pytest.fail("durable label request must not invoke a low provider"),
    )
    receipt["events"] = []
    receipt["progress"] = []
    monkeypatch.setattr(closer_queue, "run_round", lambda *_a, **_k: receipt)
    monkeypatch.setattr(closer_queue, "_read_state", lambda _path: {"items": {TARGET: inner}})
    captured = {}
    action_state = tmp_path / "inner-actions.json"
    action_state.write_text("{}")

    def run_pipeline(_path, targets, **callbacks):
        prep = {
            "snapshot": current,
            "revision": current["revision"],
            "proposal": {"action": "collect_evidence"},
        }
        interpreted = callbacks["interpret"](
            targets[0],
            {
                "prepare": prep,
                "deadline": time.time() + 60,
                "action_state_path": str(action_state),
            },
        )
        assert interpreted["classification"] == "deterministic_post_merge_label_wait"
        captured["result"] = callbacks["act"](
            targets[0],
            {
                "prepare": prep,
                "interpret": interpreted,
                "deadline": time.time() + 60,
                "revision": current["revision"],
                "work_key": "confirmed-label-test",
                "action_state_path": str(action_state),
            },
        )
        return {"targets": {targets[0]: captured["result"]}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            EVIDENCE_REFUSAL_REASONS=closer_pipeline.EVIDENCE_REFUSAL_REASONS,
        ),
    )
    backend = closer(tmp_path, current)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)

    result = captured["result"]
    assert result["status"] == "pending"
    assert result["pending_kind"] == "external_event"
    assert result["pending_evidence"]["event_id"] == "LE_native_7"
    assert result["pending_evidence"]["operation_id"] == inner["operation"]["id"]
    assert result["pending_evidence"]["merge"] == MERGE
    assert result["queue_receipt"] is receipt
    assert backend.gh.calls == []


def test_staged_action_waits_on_pr_linked_active_run_without_provider_or_label(
    tmp_path, monkeypatch
):
    row, receipt, inner = _active_verifier_fixture()
    current = inner["evidence"]

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    monkeypatch.setattr(
        adapter,
        "_pipeline_low_route",
        lambda **_kwargs: pytest.fail("active verifier wait must not invoke a low provider"),
    )
    monkeypatch.setattr(closer_queue, "run_round", lambda *_a, **_k: receipt)
    monkeypatch.setattr(closer_queue, "_read_state", lambda _path: {"items": {TARGET: inner}})
    captured = {}

    def run_pipeline(_path, targets, **callbacks):
        prep = {
            "snapshot": current,
            "revision": current["revision"],
            "proposal": {"action": "wait", "reason": row["reason"]},
        }
        interpreted = callbacks["interpret"](
            targets[0], {"prepare": prep, "deadline": time.time() + 60}
        )
        assert interpreted["classification"] == "deterministic_wait"
        captured["result"] = callbacks["act"](
            targets[0],
            {
                "prepare": prep,
                "interpret": interpreted,
                "deadline": time.time() + 60,
                "revision": current["revision"],
                "work_key": "active-verifier-test",
                "action_state_path": str(tmp_path / "inner-actions.json"),
            },
        )
        return {"targets": {targets[0]: captured["result"]}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            EVIDENCE_REFUSAL_REASONS=closer_pipeline.EVIDENCE_REFUSAL_REASONS,
        ),
    )
    backend = closer(tmp_path, current)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)

    result = captured["result"]
    assert result["status"] == "pending"
    assert result["pending_kind"] == "async_operation"
    assert result["pending_evidence"]["operation_id"] == "39078553053"
    assert result["queue_receipt"] is receipt
    assert backend.gh.calls == []


@pytest.mark.parametrize(
    "threads,checks,expected_action,expected_active",
    [
        ([{"id": "old", "isResolved": False, "isOutdated": True}], "SUCCESS", "merge", 0),
        ([{"id": "old", "isResolved": False, "isOutdated": True}], "FAILURE", "repair", 0),
        ([{"id": "current", "isResolved": False, "isOutdated": False}], "SUCCESS", "review", 1),
        (
            [
                {"id": "old", "isResolved": False, "isOutdated": True},
                {"id": "resolved", "isResolved": True, "isOutdated": False},
                {"id": "current", "isResolved": False, "isOutdated": False},
            ],
            "SUCCESS",
            "review",
            1,
        ),
    ],
)
def test_current_review_boundary_uses_same_thread_rule_as_merge_guard(
    monkeypatch, tmp_path, threads, checks, expected_action, expected_active
):
    document = _graphql_pr()
    document["reviewThreads"]["nodes"] = threads
    document["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = checks
    current = _snapshot_from_document(monkeypatch, document)
    assert current["threads"] == threads  # Outdated findings remain available for investigation.
    assert current["active_threads"] == expected_active
    assert closer(tmp_path, current).select(current)[0] == expected_action


@pytest.mark.parametrize("connection", ["labels", "closingIssuesReferences", "reviewThreads"])
def test_incomplete_current_snapshot_cannot_authorize_action(monkeypatch, connection):
    document = _graphql_pr()
    document[connection]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(RuntimeError, match="incomplete"):
        _snapshot_from_document(monkeypatch, document)


def test_actual_snapshot_retains_source_hold_and_rejects_wrong_commit_checks(monkeypatch):
    document = _graphql_pr()
    document["closingIssuesReferences"]["nodes"][0]["labels"]["nodes"] = [{"name": "agents:paused"}]
    document["commits"]["nodes"][0]["commit"]["oid"] = "c" * 40
    observed = _snapshot_from_document(monkeypatch, document)
    assert observed["sources"][0]["labels"] == ["agents:paused"]
    assert observed["checks"] is None


def test_snapshot_binds_pending_check_run_ids_to_exact_head(monkeypatch):
    document = _graphql_pr()
    rollup = document["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    rollup["state"] = "PENDING"
    rollup["contexts"] = {
        "nodes": [
            {"databaseId": 901, "status": "IN_PROGRESS", "conclusion": None},
            {"databaseId": 902, "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "legacy/status", "state": "PENDING"},
        ],
        "pageInfo": {"hasNextPage": False},
    }
    observed = _snapshot_from_document(monkeypatch, document)
    assert observed["checks"] == "PENDING"
    assert observed["pending_check_run_ids"] == [901]
    assert observed["pending_check_head"] == HEAD


def test_inventory_main_has_no_state_write_or_worker_launch(tmp_path, monkeypatch, capsys):
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    state = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(adapter, "GitHub", lambda **kwargs: FakeGitHub(snapshot()))

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only inventory must not invoke scheduler/provider")

    monkeypatch.setitem(sys.modules, "closer_queue", SimpleNamespace(run_round=forbidden))
    assert adapter.main(["run", "--assignment", str(assignment), "--state", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["inventory"][0]["target"] == TARGET
    assert not state.exists()


def test_github_api_raises_typed_deadline_exhaustion_before_transport():
    calls = []
    github = adapter.GitHub(deadline=time.time() + 1, api_fn=lambda *_a, **_k: calls.append(True))
    with pytest.raises(adapter.RoundDeadlineExhausted) as caught:
        github.api("repos/o/r/issues/1")
    assert caught.value.deadline == github.deadline
    assert caught.value.remaining_seconds < 2
    assert calls == []


def test_github_api_raises_typed_capacity_deferral_with_ledger_evidence(monkeypatch):
    throttle = {
        "resource": "core",
        "state": "shed",
        "action": "defer",
        "slept_s": 0.0,
        "remaining": 50,
        "limit": 5000,
        "reset_in_s": 1072,
        "window_s": 3600,
        "pace_s": 0.0,
        "source": "call",
        "measured_s_ago": 134,
        "reason": "50/5000 remaining; defer to reset",
    }
    monkeypatch.setattr(adapter.gh_capacity, "throttle", lambda *_a, **_k: throttle)
    calls = []
    github = adapter.GitHub(deadline=time.time() + 60, api_fn=lambda *_a, **_k: calls.append(True))
    with pytest.raises(adapter.RoundCapacityDeferred) as caught:
        github.api("repos/o/r/issues/1")
    assert caught.value.resource == "core"
    assert caught.value.throttle == throttle
    assert caught.value.reset_at_epoch == pytest.approx(caught.value.observed_at + 1072)
    assert calls == []


def test_legacy_cli_keeps_requested_90_second_active_budget(tmp_path, monkeypatch, capsys):
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"errors": [], "targets": [{"target": TARGET}]}))
    start = 1_800_000_000.0
    monkeypatch.setattr(adapter.time, "time", lambda: start)
    monkeypatch.setattr(adapter, "GitHub", lambda **kwargs: FakeGitHub(snapshot()))
    captured = {}

    def fake_round(*_args, **kwargs):
        captured.update(kwargs)
        return {"stop_valid": True, "accounting": [], "events": []}

    monkeypatch.setattr(closer_queue, "run_round", fake_round)
    state = tmp_path / "state.json"
    assert (
        adapter.main(
            [
                "run",
                "--assignment",
                str(assignment),
                "--state",
                str(state),
                "--apply",
                "--budget-seconds",
                "90",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert captured["deadline"] == start + 390
    assert captured["receipt_reserve_seconds"] == 300
    assert captured["deadline"] - captured["receipt_reserve_seconds"] == start + 90


def test_truncated_verifier_history_is_unknown_not_authority_for_another_run(tmp_path, monkeypatch):
    document = _graphql_pr()
    document.update(state="MERGED", mergeCommit={"oid": MERGE})
    document["comments"]["pageInfo"]["hasPreviousPage"] = True
    current = _snapshot_from_document(monkeypatch, document)
    backend = closer(tmp_path, current, apply=True)
    action, _, eligible, _ = backend.select(current)
    assert action != "post_merge_verify" or not eligible
    assert backend.gh.calls == []


def test_new_scoped_blocker_invalidates_mutation_without_a_github_revision_change(
    tmp_path, monkeypatch
):
    current = snapshot()
    backend = closer(
        tmp_path, current, apply=True, dispositions={TARGET: integrated(tmp_path, current)}
    )
    backend.blockers[TARGET] = {
        "reason": "new human hold",
        "next_automatic_action": "owner disposition",
    }
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"merge_executed": True, "blocked": False})
        )

    backend.run_fn = fake_run
    result = backend.execute(TARGET, "merge", current, operation())
    assert result["status"] in {"blocked", "stale", "deferred"}
    assert calls == []


def test_existing_closure_actor_flag_is_preserved_not_enabled(tmp_path, monkeypatch):
    current = snapshot(state="MERGED", merge=MERGE, verifier={"verdict": "PASS", "run_id": "42"})
    backend = closer(
        tmp_path, current, apply=True, dispositions={TARGET: integrated(tmp_path, current)}
    )
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "keepalive_supervisor",
        SimpleNamespace(close_verified_issues=lambda **kw: calls.append(kw)),
    )
    monkeypatch.delenv("ORCH_SUPERVISOR_CLOSE", raising=False)
    assert backend.close_sources(TARGET, current)["status"] == "deferred"
    assert calls == [] and "ORCH_SUPERVISOR_CLOSE" not in adapter.os.environ
    assert backend.gh.calls == []


def test_available_enabled_closure_actor_is_composed_with_exact_pr(tmp_path, monkeypatch):
    current = snapshot(state="MERGED", merge=MERGE, verifier={"verdict": "PASS", "run_id": "42"})
    backend = closer(
        tmp_path, current, apply=True, dispositions={TARGET: integrated(tmp_path, current)}
    )
    calls = []

    def runner(command, *, timeout):
        calls.append((command, timeout))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"errors": [], "closed": [SOURCE]}))

    backend.run_fn = runner
    monkeypatch.setitem(
        sys.modules, "keepalive_supervisor", SimpleNamespace(close_verified_issues=object())
    )
    monkeypatch.setenv("ORCH_SUPERVISOR_CLOSE", "1")
    assert backend.close_sources(TARGET, current)["status"] == "completed"
    command, timeout = calls[0]
    assert (
        command[-1] == TARGET and "close_verified_issues" in command[2] and "limit=1" in command[2]
    )
    assert 0 < timeout <= 2700
    assert backend.gh.calls == []


def test_reconciliation_recognizes_only_same_bound_merge_and_source_set(tmp_path):
    current = snapshot(state="MERGED", merge=MERGE)
    backend = closer(tmp_path, current)
    assert backend.reconcile(TARGET, operation("merge"))["status"] == "completed"
    current["labels"] = ["verify:compare"]
    assert backend.reconcile(TARGET, operation("post_merge_verify"))["status"] == "completed"
    current["sources"][0]["state"] = "closed"
    assert backend.reconcile(TARGET, operation("close_source"))["status"] == "completed"
    current["sources"][0]["target"] = "o/r#99"
    assert backend.reconcile(TARGET, operation("close_source"))["status"] == "unknown"


def test_issue_delivery_alias_advertises_actual_pr_and_source_claims(tmp_path):
    current = snapshot(target=SOURCE, pr_target=TARGET)
    backend = closer(tmp_path, current)
    observed = backend.observe(SOURCE)
    assert set(observed["claim_targets"]) == {TARGET, SOURCE}
    assert observed["operation_binding"]["pr_target"] == TARGET


@pytest.mark.parametrize("timeout", [False, True], ids=["completed", "owned-group-timeout"])
def test_bounded_runner_owns_session_and_kills_only_its_process_group(monkeypatch, timeout):
    created = []
    killed = []
    probes = []

    class Pipe:
        closed = False

        def close(self):
            self.closed = True

    class Process:
        pid = 43210
        returncode = None

        def __init__(self):
            self.timeouts = []
            self.stdout, self.stderr = Pipe(), Pipe()

        def communicate(self, *, timeout):
            self.timeouts.append(timeout)
            if len(self.timeouts) == 1 and globals_timeout:
                raise adapter.subprocess.TimeoutExpired(["owned"], timeout)
            self.returncode = -9 if globals_timeout else 0
            return "retained stdout", "retained stderr"

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            assert self.returncode is not None
            return self.returncode

    globals_timeout = timeout
    process = Process()

    def popen(command, **kwargs):
        created.append((command, kwargs))
        return process

    def killpg(pid, sig):
        assert pid == process.pid
        if sig == 0:
            probes.append(pid)
            if process.poll() is not None:
                raise ProcessLookupError
        else:
            killed.append((pid, sig))

    monkeypatch.setattr(adapter.subprocess, "Popen", popen)
    monkeypatch.setattr(adapter.os, "killpg", killpg)
    if timeout:
        with pytest.raises(adapter.subprocess.TimeoutExpired):
            adapter.bounded_run(["owned"], timeout=3)
        assert killed == [(process.pid, adapter.signal.SIGKILL)]
        assert process.timeouts[0] == 3 and len(process.timeouts) == 2
        assert 0 < process.timeouts[1] <= 5
    else:
        result = adapter.bounded_run(["owned"], timeout=3)
        assert result.returncode == 0 and result.stdout == "retained stdout"
        assert killed == []
        assert process.timeouts == [3]
        assert probes and set(probes) == {process.pid}
    assert process.poll() is not None
    assert process.stdout.closed and process.stderr.closed
    assert created[0][1]["start_new_session"] is True
    assert created[0][1]["stdout"] == adapter.subprocess.PIPE


def _seed_exception_capacity(
    handoff_dir,
    *,
    profile_ids=("codex-6-luna-low", "codex-5.6-terra-medium"),
):
    import execution_profiles

    handoff = Path(handoff_dir)
    handoff.mkdir(parents=True, exist_ok=True)
    profiles = {}
    pools = {}
    for profile_id in profile_ids:
        row = execution_profiles.get_profile(profile_id)
        profiles[profile_id] = {
            "state": "ok",
            "capacity_pool_ids": row["capacity_pool_ids"],
        }
        for pool in row["capacity_pool_ids"]:
            pools[pool] = {"state": "ok"}
    (handoff / "capacity.json").write_text(
        json.dumps(
            {
                "generated_at": time.time(),
                "agents": {
                    agent: {"state": "ok"}
                    for agent in ("codex", "claude", "gemini", "cursor", "vibe", "muse")
                },
                "profiles": profiles,
                "pools": pools,
            }
        )
    )


def _private_worker(
    tmp_path, monkeypatch, *, decision=None, result=None, real_admission=False, current=None
):
    calls = []
    if real_admission:
        pass
    else:
        _seed_exception_capacity(adapter.os.environ["HANDOFF_DIR"])
    monkeypatch.setitem(sys.modules, "capacity", SimpleNamespace(shed_marker=lambda agent: None))

    def offload(*args, **kwargs):
        calls.append((args, kwargs))
        return result or {"exit": 0, "output": "private advisory result"}

    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace(offload=offload))

    def mapped_git(command, **kwargs):
        assert command[:3] == ["git", "-C", str(tmp_path)]
        if command[3:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=HEAD + "\n")
        assert command[3:] == ["status", "--porcelain"]
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(adapter.subprocess, "run", mapped_git)
    backend = closer(
        tmp_path,
        current or snapshot(),
        apply=True,
        workers=True,
        workspaces={"o/r": str(tmp_path)},
        dispositions={TARGET: decision or {}},
        offload_fn=None if real_admission else offload,
    )
    return backend, calls


@pytest.mark.parametrize(
    "paths", ["src/fix.py", [""], ["."], [".."], ["/tmp/outside"], [".git/config"], ["src/"]]
)
def test_unsafe_or_nonspecific_repair_paths_never_admit_writing_worker(
    tmp_path, monkeypatch, paths
):
    decision = {"repair": {"head": HEAD, "prompt": "Correct the diagnosed bug", "paths": paths}}
    backend, calls = _private_worker(tmp_path, monkeypatch, decision=decision)
    result = backend.worker(TARGET, "repair", snapshot(), operation("repair"))
    assert result["status"] == "blocked"
    assert calls == []


@pytest.mark.parametrize(
    "trusted_plan", [False, True], ids=["diagnose-only", "integrated-mechanical-plan"]
)
def test_repair_uses_readonly_luna_until_scoped_plan_then_supported_terra(
    tmp_path, monkeypatch, trusted_plan
):
    decision = (
        {"repair": {"head": HEAD, "prompt": "Correct the diagnosed bug", "paths": ["fix.py"]}}
        if trusted_plan
        else {}
    )
    backend, calls = _private_worker(tmp_path, monkeypatch, decision=decision)
    assert backend.worker(TARGET, "repair", snapshot(), operation("repair"))["advisory_only"]
    kwargs = calls[0][1]
    assert kwargs["profile_id"] == (
        "codex-5.6-terra-medium" if trusted_plan else "codex-6-luna-low"
    )
    assert kwargs["isolate"] is True
    assert kwargs["mode"] == ("mid" if trusted_plan else "assess")
    assert backend.dispositions.get(TARGET) == decision


@pytest.mark.parametrize(
    "failure", ["quota exceeded", "unsupported model", "authentication failed"]
)
def test_generic_error_does_not_hide_provider_failure_in_output(tmp_path, monkeypatch, failure):
    current = snapshot(
        state="MERGED",
        merge=MERGE,
        verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
    )
    backend, calls = _private_worker(
        tmp_path,
        monkeypatch,
        result={"exit": 1, "error": "worker failed", "output": failure},
        current=current,
    )
    assert (
        backend.worker(TARGET, "collect_evidence", current, operation("collect_evidence"))["status"]
        == "failed"
    )
    assert backend.workers_unavailable
    action, _, eligible, _ = backend.select(
        snapshot(
            target="o/r#8",
            state="MERGED",
            merge=MERGE,
            verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
        )
    )
    assert action == "collect_evidence" and not eligible
    assert len(calls) == 1


@pytest.mark.parametrize("admitted", [False, True])
def test_existing_campaign_guard_is_reused_and_caps_worker_timeout(tmp_path, monkeypatch, admitted):
    fake_home = tmp_path / "home"
    guard = fake_home / ".codex/automations/model-allocation/assess.py"
    guard.parent.mkdir(parents=True)
    guard.write_text(
        "def enforce_campaign_budget(args):\n    "
        + (
            "args.timeout = min(args.timeout, 7)\n"
            if admitted
            else "raise ValueError('existing campaign permit required')\n"
        )
    )
    monkeypatch.setattr(adapter.Path, "home", lambda: fake_home)
    handoff = adapter.Path(adapter.os.environ["HANDOFF_DIR"])
    _seed_exception_capacity(handoff)
    capacity = handoff / "capacity.json"
    before = capacity.read_bytes()
    backend, calls = _private_worker(tmp_path, monkeypatch, real_admission=True)
    result = backend.worker(TARGET, "collect_evidence", snapshot(), operation("collect_evidence"))
    if admitted:
        assert result["status"] == "completed" and calls[0][1]["timeout"] == 7
    else:
        assert result["status"] == "deferred" and calls == []
    assert capacity.read_bytes() == before


def test_stale_capacity_never_calls_provider_or_scans_sessions(tmp_path, monkeypatch):
    handoff = adapter.Path(adapter.os.environ["HANDOFF_DIR"])
    handoff.mkdir(parents=True)
    (handoff / "capacity.json").write_text(
        json.dumps({"generated_at": time.time() - 1801, "agents": {"codex": {"state": "ok"}}})
    )
    backend, calls = _private_worker(tmp_path, monkeypatch, real_admission=True)
    assert (
        backend.worker(TARGET, "collect_evidence", snapshot(), operation("collect_evidence"))[
            "status"
        ]
        == "deferred"
    )
    assert calls == []


@pytest.mark.parametrize("dirty", [False, True], ids=["wrong-head", "dirty-checkout"])
def test_repair_workspace_must_be_clean_and_match_approved_head(tmp_path, monkeypatch, dirty):
    decision = {
        "repair": {"head": HEAD, "prompt": "Apply reviewed correction", "paths": ["fix.py"]}
    }
    backend, calls = _private_worker(tmp_path, monkeypatch, decision=decision)

    def mismatched_git(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(HEAD if dirty else "c" * 40) if "rev-parse" in command else " M fix.py",
        )

    monkeypatch.setattr(adapter.subprocess, "run", mismatched_git)
    assert backend.worker(TARGET, "repair", snapshot(), operation("repair"))["status"] == "deferred"
    assert calls == []


def test_compare_label_waits_only_for_attributed_active_run_and_collects_other_outcomes(tmp_path):
    for status, expected, expected_eligible in [
        ("in_progress", "wait", False),
        ("completed", "post_merge_verify", True),
        (None, "post_merge_verify", True),
    ]:
        current = snapshot(
            state="MERGED",
            merge=MERGE,
            labels=["verify:compare"],
            verifier_runs=(
                [
                    {
                        "id": 99,
                        "status": status,
                        "linked_pr": TARGET,
                        "head_sha": HEAD,
                        "path": ".github/workflows/agents-verifier.yml",
                        "event": "pull_request_target",
                    }
                ]
                if status
                else []
            ),
        )
        backend = closer(tmp_path, current)
        action, _, eligible, _ = backend.select(current)
        assert action == expected and eligible is expected_eligible
        assert backend.gh.calls == []


def test_pending_advisory_survives_restart_without_another_luna_call(tmp_path, monkeypatch):
    backend, calls = _private_worker(tmp_path, monkeypatch)
    current = snapshot()
    op = operation("collect_evidence")
    result = backend.worker(TARGET, "collect_evidence", current, op)
    assert len(calls) == 1 and backend.observe(TARGET)["requires_coordinator"]
    persisted = closer_queue._new_state()
    persisted["items"][TARGET] = {
        "operation": {
            **op,
            "revision": current["revision"],
            "status": "completed",
            "result": result,
        }
    }
    backend.state_path.write_text(json.dumps(persisted))
    restarted, new_calls = _private_worker(tmp_path, monkeypatch)
    observed = restarted.observe(TARGET)
    assert observed["requires_coordinator"]
    assert restarted.integration_pending(TARGET, current) == op["id"]
    assert new_calls == [] and len(calls) == 1


def test_integration_ack_is_bound_but_never_supplies_acceptance_or_merge(tmp_path, monkeypatch):
    current = snapshot(
        state="MERGED",
        merge=MERGE,
        verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
    )
    backend, calls = _private_worker(tmp_path, monkeypatch, current=current)
    original_binding = backend.observe(TARGET)["operation_binding"]
    op = operation("collect_evidence")
    backend.worker(TARGET, "collect_evidence", current, op)
    ack = {
        "integrated_operation": op["id"],
        "reviewer": "senior",
        "head": HEAD,
        "review_revision": current["review_revision"],
        "source_digest": adapter.source_digest(current["sources"]),
    }
    for field in ack:
        backend.dispositions[TARGET] = {**ack, field: "wrong" if field != "reviewer" else ""}
        assert backend.observe(TARGET)["requires_coordinator"], field
    backend.dispositions[TARGET] = ack
    assert not backend.observe(TARGET)["requires_coordinator"]
    assert backend.select(current)[0] == "collect_evidence"
    assert not adapter.acceptance_ok(current, ack)
    assert len(calls) == 1
    observed = backend.observe(TARGET)
    assert observed["operation_binding"] == original_binding
    backend.dispositions[TARGET] = {**integrated(tmp_path, current), **ack}
    assert backend.select(current)[0] == "close_source"
    assert backend.gh.calls == [] and len(calls) == 1
    backend.dispositions[TARGET][
        "followup_task"
    ] = "Collect newly requested source-specific evidence"
    assert backend.observe(TARGET)["operation_binding"] != original_binding


def test_snapshot_verifier_wait_uses_only_current_pr_attribution(tmp_path, monkeypatch):
    document = _graphql_pr()
    document.update(state="MERGED", mergeCommit={"oid": MERGE})
    document["labels"]["nodes"] = [{"name": "verify:compare"}]
    unrelated = {"id": 9, "status": "in_progress", "pull_requests": [{"number": 8}]}
    manual = {"id": 10, "status": "in_progress", "pull_requests": []}
    current = _snapshot_from_document(monkeypatch, document, [unrelated, manual])
    assert current["verifier_run_identity_unknown"] and current["verifier_runs"] == []
    # Unrelated/unbound historical runs cannot prevent an initial exact-subject
    # request; the actual preflight still checks attributable active execution.
    assert closer(tmp_path, current).select(current)[0] == "post_merge_verify"
    attributed = {
        "id": 11,
        "status": "in_progress",
        "pull_requests": [{"number": 7}],
        "head_sha": HEAD,
        "path": ".github/workflows/agents-verifier.yml",
        "event": "pull_request_target",
    }
    current = _snapshot_from_document(monkeypatch, document, [unrelated, manual, attributed])
    assert [run["id"] for run in current["verifier_runs"]] == [11]
    assert current["verifier_runs"][0]["linked_pr"] == TARGET
    assert current["verifier_runs"][0]["head_sha"] == HEAD
    assert closer(tmp_path, current).select(current)[0] == "wait"


def _lineage_snapshot(monkeypatch, document, source_responses):
    github = adapter.GitHub(deadline=time.time() + 60)
    reads = []

    def fake_api(endpoint, **kwargs):
        reads.append(endpoint)
        if endpoint == "repos/o/r/issues/7":
            return {"pull_request": {}}
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": document}}}
        return source_responses.get(endpoint, {})

    monkeypatch.setattr(github, "api", fake_api)
    return github, reads


def test_authored_meta_source_lineage_is_retained_without_closing_keyword(tmp_path, monkeypatch):
    document = _graphql_pr()
    document["closingIssuesReferences"]["nodes"] = []
    document["body"] = "<!-- meta:source_issue:2 -->\nImplement the requested change."
    issue = {
        "number": 2,
        "body": "## Tasks\n- Preserve original assertion",
        "state": "open",
        "labels": [{"name": "needs-review"}],
    }
    github, reads = _lineage_snapshot(monkeypatch, document, {"repos/o/r/issues/2": issue})
    current = github.snapshot(TARGET)
    assert current["sources"] == [
        adapter._request_source(SOURCE, issue, via="authored_issue_lineage")
    ]
    assert reads.count("repos/o/r/issues/2") == 1
    assert adapter.requirements(current["sources"][0]["body"]) == ["Preserve original assertion"]
    assert SOURCE in closer(tmp_path, current).observe(TARGET)["claim_targets"]


def test_generated_related_issues_are_excluded_and_invalid_explicit_sources_fail_closed(
    monkeypatch,
):
    document = _graphql_pr()
    document["closingIssuesReferences"]["nodes"] = []
    document["body"] = (
        "Implementation description.\n<!-- auto-status-summary:start -->\n"
        "## Related Issues\nSource Issue #99\nhttps://github.com/o/r/issues/98\n"
        "<!-- meta:source_issue:97 -->\n"
    )
    github, reads = _lineage_snapshot(monkeypatch, document, {})
    observed = github.snapshot(TARGET)
    assert observed["source_inventory"]["status"] == "no_linked_issue"
    assert [source["target"] for source in observed["sources"]] == [TARGET]
    assert not any(path.endswith(("/99", "/98", "/97")) for path in reads)
    document["body"] = "<!-- meta:issue:2 -->\n" + document["body"]
    for invalid in ({}, {"number": 2, "pull_request": {}, "body": "A PR is not a source issue"}):
        github, _ = _lineage_snapshot(monkeypatch, document, {"repos/o/r/issues/2": invalid})
        with pytest.raises(RuntimeError, match="not an issue"):
            github.snapshot(TARGET)


def test_staged_prepare_runs_before_pending_ci_wait_selection(tmp_path, monkeypatch):
    current = snapshot(checks="PENDING")
    seen = {}

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    packet = {
        "schema": "closer-evidence/v1",
        "head": HEAD,
        "section_digests": {"source": "d"},
        "sections": {
            "source": {
                "status": "complete",
                "reason": None,
                "content": [{"target": SOURCE}],
                "provenance": {},
                "truncated": False,
            }
        },
        "manifest": {"packet_sha256": "e"},
    }
    monkeypatch.setitem(
        sys.modules,
        "closer_evidence",
        SimpleNamespace(
            collect_evidence=lambda snap, api, retained_dir, **kwargs: packet,
            collection_summary=closer_evidence.collection_summary,
            validate_packet=lambda value: False,
            _atomic_write=lambda path, data: (
                Path(path).parent.mkdir(parents=True, exist_ok=True),
                Path(path).write_bytes(data),
            ),
        ),
    )

    def run_pipeline(path, targets, **kwargs):
        seen["path"] = path
        seen["reconcile"] = kwargs["reconcile"]
        prepared = kwargs["prepare"](TARGET, {"deadline": time.time() + 30})
        seen["prepared"] = prepared
        assert prepared["proposal"]["action"] == "wait"
        assert prepared["proposal"]["next_trigger"] == "check conclusion change"
        assert prepared["evidence_summary"]["sections"]["source"]["status"] == "complete"
        seen["shadow"] = kwargs["interpret"](TARGET, {"prepare": prepared})
        seen["shadow_action"] = kwargs["act"](TARGET, {"prepare": prepared})
        return {"valid": True, "targets": {TARGET: {"status": "parked"}}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            EVIDENCE_REFUSAL_REASONS=closer_pipeline.EVIDENCE_REFUSAL_REASONS,
        ),
    )
    backend = closer(tmp_path, current, apply=True, workers=True)
    monkeypatch.setattr(
        backend, "execute", lambda *_a, **_k: pytest.fail("shadow mode must never execute")
    )
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=2, shadow=True)
    result = adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 330)
    assert result["valid"]
    assert seen["path"] == adapter._pipeline_state_path(args.state, shadow=True)
    assert seen["path"] != adapter._pipeline_state_path(args.state, shadow=False)
    assert seen["reconcile"] is None
    assert seen["prepared"]["snapshot"]["checks"] == "PENDING"
    assert seen["shadow"]["classification"] == "not_run_shadow"
    assert seen["shadow"]["authority"] is False
    assert seen["shadow_action"]["target_complete"] is True
    assert not (tmp_path / "state.json").exists()


def test_validate_stop_can_select_shadow_pipeline_ledger(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    shadow_path = adapter._pipeline_state_path(state, shadow=True)
    shadow_path.write_text("{}")
    seen = {}

    def validate(path, assignment_targets=None):
        seen["path"] = path
        return {"valid": False, "reasons": ["offline fixture"]}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(validate_stop=validate))
    assert adapter.main(["validate-stop", "--state", str(state), "--shadow"]) == 78
    capsys.readouterr()
    assert seen["path"] == shadow_path


def test_validate_missing_shadow_state_never_falls_back_to_production_queue(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "state.json"
    state.write_text("production queue state")
    monkeypatch.setattr(
        closer_queue,
        "validate_stop",
        lambda *_a, **_k: pytest.fail("shadow validation must not inspect production state"),
    )
    assert adapter.main(["validate-stop", "--state", str(state), "--shadow"]) == 78
    assert json.loads(capsys.readouterr().out)["reasons"] == ["missing_shadow_pipeline_state"]


def test_capacity_deferred_uses_route_exhaustion_proof_without_second_lookup(tmp_path, monkeypatch):
    import closer_pipeline

    captured = {}
    capacity_evidence = {
        "capacity": {
            "lane": "low",
            "available": 0,
            "required": 1,
            "sampled_at": time.time(),
        }
    }

    def run_pipeline(_path, targets, **callbacks):
        captured["result"] = callbacks["act"](
            targets[0],
            {
                "interpret": {
                    "classification": "capacity_deferred",
                    "resource_evidence": capacity_evidence,
                    "next_trigger": "capacity_recheck",
                }
            },
        )
        return {"targets": {targets[0]: captured["result"]}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            _valid_worker_capacity=closer_pipeline._valid_worker_capacity,
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_pipeline_capacity",
        lambda: pytest.fail("route exhaustion must not trigger a second capacity lookup"),
    )
    backend = closer(tmp_path, snapshot())
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)

    result = captured["result"]
    assert result["status"] == "deferred"
    assert result["reason"] == "worker_capacity"
    capacity = result["resource_evidence"]["capacity"]
    assert capacity["lane"] == "low"
    assert capacity["available"] == 0
    assert result["resource_evidence"] == capacity_evidence
    assert closer_pipeline._valid_worker_capacity(result["resource_evidence"], time.time())


@pytest.mark.parametrize(
    ("reason", "inner", "expected_status"),
    [
        ("claim_held", {"holder": {"agent": "other"}}, "deferred"),
        ("PR closed or draft", {}, "blocked"),
    ],
)
def test_only_proven_non_ci_waits_become_pipeline_continuations(
    tmp_path, monkeypatch, reason, inner, expected_status
):
    action_state = tmp_path / "inner-actions.json"
    queue_receipt = {
        "stop_valid": True,
        "accounting": [
            {
                "target": TARGET,
                "status": "ineligible",
                "eligible": False,
                "reason": reason,
                "next_trigger": "wait for fresh source condition",
                "action": "wait",
            }
        ],
        "events": [],
    }
    monkeypatch.setattr(closer_queue, "run_round", lambda *_a, **_k: queue_receipt)
    monkeypatch.setattr(
        closer_queue,
        "_read_state",
        lambda _path: {"items": {TARGET: inner}},
    )
    captured = {}

    def run_pipeline(_path, targets, **callbacks):
        captured["result"] = callbacks["act"](
            targets[0],
            {
                "prepare": {"snapshot": {}},
                "interpret": {"classification": "deterministic_wait"},
                "deadline": time.time() + 60,
                "revision": "observed-revision",
                "work_key": "ordinary-wait-test",
                "action_state_path": str(action_state),
            },
        )
        return {"targets": {targets[0]: captured["result"]}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            EVIDENCE_REFUSAL_REASONS=closer_pipeline.EVIDENCE_REFUSAL_REASONS,
        ),
    )
    backend = closer(tmp_path, snapshot())
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)

    result = captured["result"]
    assert result["status"] == expected_status
    assert result["next_trigger"] == "wait for fresh source condition"
    assert result["queue_receipt"] == queue_receipt
    if expected_status == "deferred":
        assert result["reason"] == "claim_held"
        assert result["resource_evidence"] == {"holder": {"agent": "other"}}


@pytest.mark.parametrize("case", ["malformed_worker", "source_hold"])
def test_staged_adapter_emits_only_durable_validated_inner_wait(tmp_path, monkeypatch, case):
    current = (
        snapshot(labels=["agents:paused"])
        if case == "source_hold"
        else snapshot(
            state="MERGED",
            merge=MERGE,
            verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
        )
    )
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: FakeGitHub(current))
    pipeline_path = adapter._pipeline_state_path(tmp_path / "state.json")
    action_path = (
        pipeline_path.parent
        / f"{pipeline_path.stem}.actions"
        / f"{closer_pipeline._digest(TARGET)}.json"
    )
    captured = {}

    def pipeline_stub(_path, _targets, **callbacks):
        interpretation = (
            {
                "proposal": {"raw_excerpt": "invalid"},
                "actual": {"exit": 0, "run_id": "real-low-run"},
            }
            if case == "malformed_worker"
            else {"classification": "deterministic_wait"}
        )
        captured["result"] = callbacks["act"](
            TARGET,
            {
                "state_path": str(pipeline_path),
                "action_state_path": str(action_path),
                "deadline": time.time() + 60,
                "revision": current["revision"],
                "work_key": "offline-bound-wait",
                "prepare": {"snapshot": current, "packet_ref": {}},
                "interpret": interpretation,
                "exception": {
                    "validated": {
                        "status": "exception",
                        "reason": "substantive_or_ambiguous_finding",
                    }
                },
            },
        )
        return {"targets": {TARGET: captured["result"]}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    backend = closer(tmp_path, current)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)
    result = captured["result"]
    assert result["status"] == "deferred"
    assert result["reason"] == "validated_inner_wait"
    assert result["queue_receipt"]["stop_valid"] is True
    assert result["resource_evidence"]["inner_wait"]["inner_path"] == str(action_path)
    assert not backend.gh.calls


def ordinary_nonpass_wait_fixture(
    tmp_path, monkeypatch, *, fresh_revision=False, linked_subject=False
):
    # Reuse the native input/comparison corpus fixture; do not stub integration.
    import test_closer_disposition

    import closer_disposition

    if linked_subject:
        monkeypatch.setattr(test_closer_disposition, "TARGET", "owner/repo#11")
    current, packet, ref, prepared, interpreted = test_closer_disposition.fixture(
        tmp_path,
        native=True,
        **({"pr_target": "owner/repo#12"} if linked_subject else {}),
    )
    current.update(state="MERGED", labels=[], draft=False, active_threads=0, mergeable="MERGEABLE")
    prepared.update(
        snapshot=copy.deepcopy(current),
        packet_ref=ref,
        identity=current["target"],
        semantic_cache_key="closer-evidence-semantic/v1:" + "e" * 64,
    )
    interpreted["proposal"]["decision"]["criteria"] = []
    interpreted["proposal"]["resolutions"] = [
        {
            "finding_id": row["finding_id"],
            "status": "unresolved_missing_evidence",
            "next_guarded_action": "wait",
            "reason": "Original acceptance proof is missing; source-owned follow-up required",
        }
        for row in closer_disposition.pending_nonpass_findings(current, packet)
    ]
    interpreted["substantive_needs"] = []
    if fresh_revision:
        current["revision"] = "fresh-same-head-revision"
    monkeypatch.setattr(adapter, "GitHub", lambda **kwargs: FakeGitHub(current))
    path = adapter._pipeline_state_path(tmp_path / "state.json")
    captured = {}

    def pipeline_stub(_path, _targets, **callbacks):
        context = {
            "state_path": str(path),
            "action_state_path": str(
                path.parent
                / f"{path.stem}.actions"
                / f"{closer_pipeline._digest(current['target'])}.json"
            ),
            "deadline": time.time() + 60,
            "revision": prepared["revision"],
            "identity": prepared["identity"],
            "prepare": prepared,
            "interpret": interpreted,
        }
        captured["act"] = callbacks["act"]
        captured["context"] = context
        captured["first"] = callbacks["act"](current["target"], context)
        return {"targets": {current["target"]: captured["first"]}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    backend = closer(tmp_path, current)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [current["target"]], backend, time.time() + 360)
    return current, prepared, interpreted, path, backend, captured


@pytest.mark.parametrize("linked_subject", [False, True])
def test_helper_accounted_native_nonpass_becomes_typed_wait_without_acceptance(
    tmp_path, monkeypatch, linked_subject
):
    native_pipeline = closer_pipeline.run_pipeline
    current, prep, interpreted, path, backend, captured = ordinary_nonpass_wait_fixture(
        tmp_path, monkeypatch, linked_subject=linked_subject
    )
    result = captured["first"]
    assert result["status"] == "deferred" and result["reason"] == "validated_inner_wait"
    proof = result["resource_evidence"]["inner_wait"]
    record_ref = proof["ordinary_nonpass_ref"]
    raw = Path(record_ref["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == record_ref["sha256"]
    record = json.loads(raw)
    assert record["disposition"]["acceptance_authority"] is False
    assert len(record["disposition"]["raw_verdicts"]) == 4
    assert interpreted["proposal"]["decision"]["criteria"] == []
    assert result["queue_receipt"]["stop_valid"] is True
    assert (
        closer_queue._read_state(Path(proof["inner_path"]))["items"][current["target"]]["operation"]
        is None
    )
    assert closer_pipeline._valid_inner_wait(
        path,
        current["target"],
        prep["revision"],
        prep["snapshot"],
        proof["reason"],
        result["next_trigger"],
        proof,
    )
    assert backend.dispositions == {} and backend.gh.calls == []

    # A later deterministic read may reuse only this already validated durable proof.
    context = copy.deepcopy(captured["context"])
    context["interpret"] = {
        "classification": "deterministic_inner_wait",
        "inner_wait_proof": proof,
        "reason": proof["reason"],
        "next_trigger": result["next_trigger"],
    }
    repeated = captured["act"](current["target"], context)
    assert repeated["status"] == "deferred"
    assert repeated["resource_evidence"]["inner_wait"]["ordinary_nonpass_ref"] == record_ref
    # Exercise the real outer stage handler too: the typed wait must not become
    # the former "act returned unsupported or missing status" failed-action row.
    outer = native_pipeline(
        path,
        [current["target"]],
        prepare=lambda *_: copy.deepcopy(prep),
        interpret=lambda *_: copy.deepcopy(interpreted),
        exception=lambda *_: pytest.fail("accounted NON_PASS requires no stronger worker"),
        act=captured["act"],
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 10,
    )
    row = outer["targets"][current["target"]]
    assert row["status"] == "deferred" and row["stage"] == "act"
    assert row.get("known_completed_action") is not True
    assert closer_pipeline.validate_stop(path)["valid"] is True


def test_nonpass_callback_preserves_complete_evidence_without_unused_disposition_duplicate(
    tmp_path, monkeypatch
):
    import test_closer_disposition

    import closer_disposition
    import dispatcher

    # Large but legitimate retained NON_PASS rows, without private queue data.
    prior = {
        "criteria": [
            {"result": "NON_PASS", "criterion": f"criterion-{index}", "reason": "gap " + "x" * 2100}
            for index in range(6)
        ]
    }
    current, packet, ref, prep, interpreted = test_closer_disposition.fixture(
        tmp_path, native=True, dispositions=prior
    )
    current.update(state="MERGED", labels=[], draft=False, active_threads=0, mergeable="MERGEABLE")
    target = current["target"]
    prepared_snapshot = tmp_path / "prepared-snapshot.json"
    prepared_snapshot.write_text(json.dumps(current))
    prep.update(
        snapshot=copy.deepcopy(current),
        identity=target,
        packet_ref=ref,
        snapshot_ref={
            "path": str(prepared_snapshot),
            "sha256": hashlib.sha256(prepared_snapshot.read_bytes()).hexdigest(),
        },
    )
    proposal = interpreted["proposal"]
    proposal["decision"]["criteria"] = []
    proposal["findings"] = [{"summary": "Original criterion proof remains missing"}]
    proposal["resolutions"] = [
        {
            "finding_id": row["finding_id"],
            "status": "unresolved_missing_evidence",
            "next_guarded_action": "wait",
            "reason": "Original proof remains unavailable; source-owned follow-up required",
        }
        for row in closer_disposition.pending_nonpass_findings(current, packet)
    ]
    original_proposal = copy.deepcopy(proposal)
    actual = {"exit": 0, "run_id": "offline-completed-low", "profile_id": "offline-low"}
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: FakeGitHub(current))
    monkeypatch.setattr(
        adapter,
        "_pipeline_low_route",
        lambda **_kwargs: (
            {"agent": "codex", "profile_id": "offline-low", "pools": []},
            "available",
        ),
    )
    calls = []
    monkeypatch.setattr(
        dispatcher,
        "offload",
        lambda *_args, **_kwargs: calls.append(True) or {**actual, "output": json.dumps(proposal)},
    )
    integrations = []
    real_integrate = closer_disposition.integrate_ordinary

    def integrate(*args, **kwargs):
        result = real_integrate(*args, **kwargs)
        integrations.append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(closer_disposition, "integrate_ordinary", integrate)
    backend = closer(tmp_path, current)
    backend.campaign_admission = lambda *_args: {"allowed": True}
    path = adapter._pipeline_state_path(tmp_path / "state.json")

    def pipeline_stub(_path, _targets, **callbacks):
        context = {
            "state_path": str(path),
            "action_state_path": str(
                path.parent / f"{path.stem}.actions" / f"{closer_pipeline._digest(target)}.json"
            ),
            "deadline": time.time() + 60,
            "revision": prep["revision"],
            "identity": target,
            "prepare": prep,
        }
        value = callbacks["interpret"](target, context)
        assert value["integration_status"] == "ordinary_nonpass"
        assert "integration_disposition" not in value
        assert value["proposal"] == original_proposal
        assert value["raw_findings"] == integrations[0]["raw_findings"]
        assert len(value["raw_findings"]) == len(proposal["resolutions"]) == 10
        assert closer_pipeline.CALLBACK_RESULT_BYTES == 32768
        assert closer_pipeline._bounded(value, limit=32768) == value
        with pytest.raises(ValueError, match="pipeline JSON exceeds 32768 bytes"):
            closer_pipeline._bounded(
                {**value, "integration_disposition": integrations[0]["disposition"]}, limit=32768
            )
        result = callbacks["act"](target, {**context, "interpret": value})
        assert len(integrations) == 2  # Native action independently rechecks all original proof.
        assert integrations[1] == integrations[0]
        assert result["reason"] == "validated_inner_wait"
        proof = result["resource_evidence"]["inner_wait"]
        assert closer_pipeline._valid_inner_wait(
            path, target, prep["revision"], current, proof["reason"], result["next_trigger"], proof
        )
        retained = json.loads(Path(proof["ordinary_nonpass_ref"]["path"]).read_bytes())
        assert retained["disposition"] == integrations[0]["disposition"]
        assert retained["disposition"]["acceptance_authority"] is False
        return {"targets": {target: result}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [target], backend, time.time() + 360)
    assert calls == [True] and backend.gh.calls == []


def test_native_nonpass_wait_survives_real_interpreter_restart(tmp_path, monkeypatch):
    current, prep, _interpreted, path, _backend, captured = ordinary_nonpass_wait_fixture(
        tmp_path, monkeypatch
    )
    first = captured["first"]
    proof = first["resource_evidence"]["inner_wait"]
    prep["producer_sha256"] = "2" * 64
    prep["proposal"] = {"action": "collect_evidence"}
    target = current["target"]
    closer_pipeline._save(
        path,
        {
            "schema": closer_pipeline.SCHEMA,
            "runs": {
                "prior": {
                    "created": time.time() - 10,
                    "targets": {
                        target: {
                            "status": "deferred",
                            "stage": "act",
                            "deferred_reason": "validated_inner_wait",
                            "revision": prep["revision"],
                            "results": {"prepare": prep},
                            "resource_evidence": first["resource_evidence"],
                        }
                    },
                }
            },
            "carry": {},
            "cache": {},
        },
    )
    restarted = closer(tmp_path, current)
    restarted.campaign_admission = lambda *_: {"allowed": True}
    assert restarted.dispositions == {} and restarted.validated_nonpass_waits == {}
    monkeypatch.setattr(
        adapter,
        "_pipeline_low_route",
        lambda **_kwargs: pytest.fail("valid retained wait must not dispatch another provider"),
    )
    seen = {}

    def pipeline_stub(_path, _targets, **callbacks):
        context = {**copy.deepcopy(captured["context"]), "prepare": copy.deepcopy(prep)}
        interpretation = callbacks["interpret"](target, context)
        assert interpretation["classification"] == "deterministic_inner_wait"
        assert (
            interpretation["inner_wait_proof"]["ordinary_nonpass_ref"]
            == proof["ordinary_nonpass_ref"]
        )
        result = callbacks["act"](target, {**context, "interpret": interpretation})
        seen["result"] = result
        return {"targets": {target: result}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [target], restarted, time.time() + 360)
    result = seen["result"]
    assert result["status"] == "deferred" and result["reason"] == "validated_inner_wait"
    assert (
        result["resource_evidence"]["inner_wait"]["ordinary_nonpass_ref"]
        == proof["ordinary_nonpass_ref"]
    )
    assert result["queue_receipt"]["stop_valid"] is True
    assert restarted.gh.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "prefix_only",
        "authority",
        "record_revision",
        "record_target",
        "disposition_hash",
        "finding_id",
        "native_pass",
        "native_open",
        "native_head",
        "operation",
        "receipt",
        "missing_record",
    ],
)
def test_nonpass_wait_requires_exact_retained_disposition_and_current_native_proof(
    tmp_path, monkeypatch, mutation
):
    current, prep, _interpreted, path, _backend, captured = ordinary_nonpass_wait_fixture(
        tmp_path, monkeypatch
    )
    result = captured["first"]
    proof = copy.deepcopy(result["resource_evidence"]["inner_wait"])
    record_ref = proof["ordinary_nonpass_ref"]
    record = json.loads(Path(record_ref["path"]).read_bytes())
    native_path = Path(proof["inner_path"])
    native = closer_queue._read_state(native_path)
    item = native["items"][current["target"]]
    if mutation == "prefix_only":
        proof.pop("ordinary_nonpass_ref")
    elif mutation == "missing_record":
        Path(record_ref["path"]).unlink()
    elif mutation.startswith("native_"):
        if mutation == "native_pass":
            item["evidence"]["verifier"]["verdict"] = "PASS"
        elif mutation == "native_open":
            item["evidence"]["state"] = "OPEN"
        else:
            item["evidence"]["head"] = "f" * 40
    elif mutation == "operation":
        item["operation"] = {"id": "unknown", "status": "uncertain"}
    elif mutation == "receipt":
        native["round"]["receipt"]["accounting"][0]["eligible"] = True
    else:
        if mutation == "authority":
            record["disposition"]["acceptance_authority"] = True
        elif mutation == "record_revision":
            record["revision"] = "stale"
        elif mutation == "record_target":
            record["target"] = "other/repo#1"
        elif mutation == "finding_id":
            record["disposition"]["resolutions"][0]["finding_id"] = "wrong"
        else:
            record["disposition_sha256"] = "0" * 64
        if mutation != "disposition_hash":
            record["disposition_sha256"] = closer_pipeline._digest(record["disposition"])
        import closer_evidence

        proof["ordinary_nonpass_ref"] = closer_evidence._retain_bytes(
            closer_pipeline._nonpass_wait_store(path), closer_pipeline._canonical(record), "json"
        )
    native_path.write_text(json.dumps(native))
    assert not closer_pipeline._valid_inner_wait(
        path,
        current["target"],
        prep["revision"],
        prep["snapshot"],
        proof["reason"],
        result["next_trigger"],
        proof,
    )


def test_nonpass_wait_same_head_new_revision_requires_fresh_preparation(tmp_path, monkeypatch):
    current, prep, _interpreted, _path, _backend, captured = ordinary_nonpass_wait_fixture(
        tmp_path, monkeypatch, fresh_revision=True
    )
    assert captured["first"]["status"] == "completed"
    assert captured["first"]["target_complete"] is False
    context = copy.deepcopy(captured["context"])
    context["revision"] = current["revision"]
    context["prepare"]["revision"] = current["revision"]
    context["prepare"]["snapshot"] = current
    second = captured["act"](current["target"], context)
    assert second["status"] == "deferred" and second["reason"] == "validated_inner_wait"


def test_fresh_inner_wait_revision_change_reprepares_before_valid_wait(tmp_path, monkeypatch):
    old = snapshot(labels=["agents:paused"], review_revision="before-fresh-read")
    fresh = snapshot(labels=["agents:paused"], review_revision="after-fresh-read")
    assert old["head"] == fresh["head"] and old["revision"] != fresh["revision"]
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: FakeGitHub(fresh))
    pipeline_path = adapter._pipeline_state_path(tmp_path / "state.json")
    action_path = (
        pipeline_path.parent
        / f"{pipeline_path.stem}.actions"
        / f"{closer_pipeline._digest(TARGET)}.json"
    )
    reason = "current item-scoped hold: ['agents:paused']"
    trigger = "hold owner supplies scoped disposition"
    receipt = closer_queue.run_round(
        action_path,
        [TARGET],
        lambda _target: {
            "target": TARGET,
            "revision": fresh["revision"],
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": reason,
            "next_trigger": trigger,
            "evidence": fresh,
        },
        lambda *_: pytest.fail("held wait must not execute"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    assert receipt["stop_valid"] is True
    returned = {"receipt": receipt}
    monkeypatch.setattr(closer_queue, "run_round", lambda *_a, **_k: returned["receipt"])
    captured = {}

    def pipeline_stub(_path, _targets, **callbacks):
        def context(prepared):
            return {
                "state_path": str(pipeline_path),
                "action_state_path": str(action_path),
                "deadline": time.time() + 60,
                "revision": prepared["revision"],
                "prepare": {"snapshot": prepared, "packet_ref": {}},
                "interpret": {"classification": "deterministic_wait"},
            }

        captured["first"] = callbacks["act"](TARGET, context(old))
        captured["second"] = callbacks["act"](TARGET, context(fresh))
        returned["receipt"] = {**receipt, "stop_valid": False}
        with pytest.raises(RuntimeError, match="valid continuation boundary"):
            callbacks["act"](TARGET, context(old))
        returned["receipt"] = receipt
        changed_head = {**old, "head": "f" * 40}
        captured["changed_head"] = callbacks["act"](TARGET, context(changed_head))
        return {"targets": {TARGET: captured["second"]}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    backend = closer(tmp_path, fresh)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)
    assert captured["first"]["status"] == "completed"
    assert captured["first"]["target_complete"] is False
    assert isinstance(captured["first"]["progress_key"], str)
    assert captured["second"]["reason"] == "validated_inner_wait"
    assert captured["changed_head"]["status"] == "blocked"


def test_same_head_inner_wait_requires_fresh_admission_and_matching_producer(tmp_path, monkeypatch):
    current = snapshot(
        state="MERGED",
        merge=MERGE,
        verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
    )
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: FakeGitHub(current))
    pipeline_path = adapter._pipeline_state_path(tmp_path / "state.json")
    action_path = (
        pipeline_path.parent
        / f"{pipeline_path.stem}.actions"
        / f"{closer_pipeline._digest(TARGET)}.json"
    )
    reason = "worker_json_invalid"
    trigger = "source owner supplies current-head proof or resolves the named finding"
    receipt = closer_queue.run_round(
        action_path,
        [TARGET],
        lambda _target: {
            "target": TARGET,
            "revision": current["revision"],
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": reason,
            "next_trigger": trigger,
            "evidence": current,
        },
        lambda *_: pytest.fail("terminal wait must not execute"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    semantic = "closer-evidence-semantic/v1:" + "1" * 64
    old_prepare = {
        "revision": current["revision"],
        "snapshot": current,
        "semantic_cache_key": semantic,
        "producer_sha256": "2" * 64,
        "proposal": {"action": "collect_evidence"},
    }
    proof = {
        "schema": closer_pipeline.INNER_WAIT_SCHEMA,
        "target": TARGET,
        "revision": current["revision"],
        "head": current["head"],
        "reason": reason,
        "next_trigger": trigger,
        "inner_path": str(action_path),
        "receipt_sha256": closer_pipeline._digest(receipt),
    }
    closer_pipeline._save(
        pipeline_path,
        {
            "schema": closer_pipeline.SCHEMA,
            "runs": {
                "prior": {
                    "created": time.time() - 10,
                    "targets": {
                        TARGET: {
                            "status": "deferred",
                            "deferred_reason": "validated_inner_wait",
                            "revision": current["revision"],
                            "results": {"prepare": old_prepare},
                            "resource_evidence": {"inner_wait": proof},
                        }
                    },
                }
            },
            "carry": {},
            "cache": {},
        },
    )
    backend = closer(tmp_path, current)
    admissions = []
    admission_allowed = [True]
    backend.campaign_admission = lambda *_: (
        admissions.append(True)
        or {
            "allowed": admission_allowed[0],
            "reason": "fresh admission required",
        }
    )
    monkeypatch.setattr(adapter, "_pipeline_low_route", lambda **_kwargs: (None, "saturated"))
    seen = {}

    def pipeline_stub(_path, _targets, **callbacks):
        old_context = {
            "state_path": str(pipeline_path),
            "action_state_path": str(action_path),
            "deadline": time.time() + 60,
            "revision": current["revision"],
            "work_key": "same-head-reuse",
            "prepare": old_prepare,
        }
        old_interpret = callbacks["interpret"](TARGET, old_context)
        assert old_interpret["classification"] == "deterministic_inner_wait"
        assert admissions == [True]
        # Check fresh permission while the persisted wait proof is still current.
        admission_allowed[0] = False
        denied = callbacks["interpret"](TARGET, old_context)
        assert denied["classification"] == "admission_deferred"
        admission_allowed[0] = True
        old_act = callbacks["act"](TARGET, {**old_context, "interpret": old_interpret})
        assert old_act["status"] == "deferred"
        changed_producer = {**old_context, "prepare": {**old_prepare, "producer_sha256": "3" * 64}}
        producer_interpret = callbacks["interpret"](TARGET, changed_producer)
        assert producer_interpret["classification"] == "evidence_pending"
        changed = snapshot(
            state="MERGED",
            merge=MERGE,
            verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
            head="b" * 40,
        )
        changed_context = {
            **old_context,
            "revision": changed["revision"],
            "prepare": {**old_prepare, "revision": changed["revision"], "snapshot": changed},
        }
        changed_interpret = callbacks["interpret"](TARGET, changed_context)
        assert changed_interpret["classification"] == "evidence_pending"
        assert len(admissions) == 2
        seen["result"] = old_act
        return {"targets": {TARGET: old_act}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)
    assert seen["result"]["reason"] == "validated_inner_wait"
    assert backend.gh.calls == []


@pytest.mark.parametrize(
    ("check_run_ids", "check_head", "expected_status"),
    [([901], HEAD, "pending"), ([], None, "blocked"), ([901], "f" * 40, "blocked")],
)
def test_staged_action_emits_pending_only_for_fresh_bound_check_run(
    tmp_path, monkeypatch, check_run_ids, check_head, expected_status
):
    import closer_evidence

    current = snapshot(
        base="c" * 40,
        checks="PENDING",
        pending_check_run_ids=check_run_ids,
        pending_check_head=check_head,
    )

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    packet = {
        "schema": "closer-evidence/v1",
        "target": TARGET,
        "head": HEAD,
        "base": current["base"],
        "producer_sha256": "d" * 64,
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
        "sections": {},
        "manifest": {"packet_sha256": "f" * 64},
    }
    monkeypatch.setattr(closer_evidence, "collect_evidence", lambda *_a, **_k: packet)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: False)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    backend = closer(tmp_path, current, apply=False, workers=False)
    observed = {}

    def run_pipeline(_path, targets, **callbacks):
        assert targets == [TARGET]
        deadline = time.time() + 60
        prep = callbacks["prepare"](TARGET, {"deadline": deadline})
        observed["result"] = callbacks["act"](
            TARGET,
            {
                "prepare": prep,
                "interpret": {"classification": "deterministic_wait"},
                "deadline": deadline,
                "revision": prep["revision"],
                "work_key": "pending-ci-test",
                "action_state_path": str(tmp_path / "actions.json"),
            },
        )
        return {"targets": {TARGET: observed["result"]}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(
            run_pipeline=run_pipeline,
            EVIDENCE_REFUSAL_REASONS=closer_pipeline.EVIDENCE_REFUSAL_REASONS,
        ),
    )
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)

    result = observed["result"]
    assert result["status"] == expected_status
    if expected_status == "pending":
        assert result["pending_kind"] == "ci"
        assert result["pending_evidence"]["check_run_id"] == 901
        assert result["pending_evidence"]["head"] == HEAD
        assert "901" in result["next_trigger"]
    assert not backend.apply


def test_pipeline_low_route_uses_any_live_low_profile_not_luna_assumption(tmp_path, monkeypatch):
    import execution_profiles
    import router

    report_path = tmp_path / "capacity.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": time.time(),
                "agents": {"gemini": {"state": "ok"}, "aider": {"state": "ok"}},
                "profiles": {
                    "gemini-flash-low": {"state": "ok"},
                    "aider-flash-low": {"state": "ok"},
                },
                "pools": {"gemini-api": {"state": "ok"}, "aider-paygo": {"state": "ok"}},
            }
        )
    )
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(
        execution_profiles,
        "profiles_for_agent",
        lambda agent, **kw: [
            {
                "profile_id": f"{agent}-flash-low",
                "reasoning_effort": "low",
                "capacity_pool_ids": ["aider-paygo" if agent == "aider" else "gemini-api"],
            }
        ],
    )
    selected = {}

    def choose(task_type, capacity, **kwargs):
        selected["only"] = kwargs.get("only")
        return {
            "agent": "gemini",
            "mode": "cheap",
            "state": "ok",
            "selected_profile_id": "gemini-flash-low",
        }

    monkeypatch.setattr(router, "select_agent", choose)
    route = adapter._pipeline_low_route()
    assert route["agent"] == "gemini"
    assert route["profile_id"] == "gemini-flash-low"
    assert selected["only"] == {"gemini"}
    monkeypatch.setattr(adapter.gh_capacity, "state", lambda *_: (adapter.gh_capacity.OK, {}))
    assert adapter._pipeline_capacity()["low"] == 1


def test_busy_shared_exception_pool_returns_valid_fresh_capacity_proof(tmp_path, monkeypatch):
    stub_action_readiness_for_transport_test(monkeypatch)
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import closer_evidence
    import closer_pipeline
    import dispatcher
    import execution_profiles

    current = snapshot(
        state="MERGED",
        merge=MERGE,
        base="c" * 40,
        verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
    )
    packet = {
        "schema": "closer-evidence/v1",
        "target": TARGET,
        "head": HEAD,
        "base": current["base"],
        "producer_sha256": "d" * 64,
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
        "sections": {},
        "manifest": {"packet_sha256": "f" * 64},
    }

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    monkeypatch.setattr(closer_evidence, "collect_evidence", lambda *_a, **_k: packet)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: True)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 1, "low": 1, "exception": 1})

    def reserve_low_route(**kwargs):
        if "codex-low" in kwargs["unavailable_profiles"]:
            return (None, "exhausted")
        load = kwargs["load"]
        load["codex"] = load.get("codex", 0) + 1
        load["pool:codex-subscription"] = load.get("pool:codex-subscription", 0) + 1
        return (
            {"agent": "codex", "profile_id": "codex-low", "pools": ["codex-subscription"]},
            "available",
        )

    monkeypatch.setattr(adapter, "_pipeline_low_route", reserve_low_route)

    def reserve_exception_route(**kwargs):
        load = kwargs["load"]
        if load.get("pool:codex-subscription", 0):
            return (None, "busy")
        load["codex"] = load.get("codex", 0) + 1
        load["pool:codex-subscription"] = load.get("pool:codex-subscription", 0) + 1
        return (
            {
                "agent": "codex",
                "profile_id": "codex-strong",
                "pools": ["codex-subscription"],
                "mode": "assess",
            },
            "available",
        )

    monkeypatch.setattr(adapter, "_exception_route", reserve_exception_route)
    monkeypatch.setattr(execution_profiles, "default_codex_profile", lambda _task: "codex-strong")
    monkeypatch.setattr(
        execution_profiles,
        "PROFILE_REGISTRY",
        {"codex-strong": {"capacity_pool_ids": ["codex-subscription"]}},
    )
    monkeypatch.setattr(
        adapter,
        "_fresh_capacity_report",
        lambda: {
            "profiles": {
                "codex-strong": {
                    "state": "ok",
                    "capacity_pool_ids": ["codex-subscription"],
                }
            },
            "pools": {"codex-subscription": {"state": "ok"}},
        },
    )
    # Routing is stubbed in this semantic test; the execution-pool suite uses real profiles.
    monkeypatch.setattr(execution_profiles, "profiles_for_agent", lambda *a, **kw: [])
    entered = threading.Event()
    release = threading.Event()

    def offload(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"exit": 1, "run_id": "transport-failure", "error": "offline fixture"}

    monkeypatch.setattr(dispatcher, "offload", offload)
    backend = closer(tmp_path, current, apply=True, workers=True)
    monkeypatch.setattr(
        backend, "campaign_admission", lambda *_a, **_k: {"allowed": True, "timeout": 60}
    )
    monkeypatch.setattr(
        backend, "worker", lambda *_a, **_k: pytest.fail("busy shared pool must defer")
    )
    captured = {}

    def pipeline_stub(_path, _targets, **callbacks):
        prep = callbacks["prepare"](TARGET, {"deadline": time.time() + 60})
        context = {"prepare": prep, "deadline": time.time() + 60}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(callbacks["interpret"], TARGET, context)
            assert entered.wait(5)
            result = callbacks["exception"](
                TARGET,
                {
                    "prepare": prep,
                    "interpret": {"proposal": {}, "substantive_needs": []},
                    "deadline": context["deadline"],
                    "work_key": "exception-capacity-test",
                },
            )
            captured["result"] = result
            release.set()
            future.result(timeout=5)
        return {"targets": {TARGET: {"status": "deferred"}}}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(run_pipeline=pipeline_stub))
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)

    deferred = captured["result"]
    assert deferred["reason"] == "worker_capacity"
    assert deferred["resource_evidence"]["capacity"]["active"] == 1
    assert closer_pipeline._valid_worker_capacity(deferred["resource_evidence"], time.time())


def test_queue_observation_failure_is_accounted_unknown_and_never_runs_action(tmp_path):
    def fail_observe(_target):
        raise RuntimeError("incomplete live read")

    def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("failed observation must not execute")

    receipt = closer_queue.run_round(
        tmp_path / "queue.json",
        ["o/r#99"],
        fail_observe,
        forbidden_execute,
        deadline=time.time() + 60,
        max_workers=1,
    )
    assert not receipt["stop_valid"]
    row = receipt["accounting"][0]
    assert row["reason"] == "observation_failed"
    assert row["next_trigger"] == "retry_observation"
    state = closer_queue._read_state(tmp_path / "queue.json")
    assert state["items"]["o/r#99"]["evidence"]["unknown"] is True
    assert state["items"]["o/r#99"]["evidence"]["error"] == "incomplete live read"


def test_legacy_local_running_operation_is_not_external_async_proof(tmp_path):
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "round": {},
                "items": {
                    TARGET: {
                        "operation": {
                            "id": "local-ledger-hash-only",
                            "status": "running",
                            "revision": "head-r1",
                            "action": "repair",
                            "repo_keys": ["o/r"],
                        }
                    }
                },
            }
        )
    )
    with pytest.raises(RuntimeError, match="lacks external-run or live-owner proof"):
        adapter._legacy_operation_dependency(
            legacy_path, TARGET, {"target": TARGET, "sources": [{"target": SOURCE}]}
        )
    # An unconfirmed legacy local write cannot become a valid external stop.
    import closer_pipeline

    state = tmp_path / "pipeline.json"
    closer_pipeline.run_pipeline(
        state,
        [TARGET],
        prepare=lambda *_: {"revision": "r", "identity": "i"},
        interpret=lambda *_: {"substantive_needs": []},
        exception=lambda *_: {"status": "unused"},
        act=lambda *_: adapter._legacy_operation_dependency(
            legacy_path, TARGET, {"target": TARGET, "sources": [{"target": SOURCE}]}
        ),
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 2,
        max_io_workers=1,
        max_low_workers=1,
        max_exception_workers=1,
    )
    assert closer_pipeline.validate_stop(state)["valid"] is False


def test_legacy_uncertain_and_private_integration_need_original_coordinator(tmp_path):
    legacy_path = tmp_path / "legacy.json"
    for item in (
        {"operation": {"id": "uncertain-5", "status": "uncertain", "repo_keys": ["o/r"]}},
        {"operation": {"id": "completed-6", "status": "completed"}, "requires_coordinator": True},
    ):
        legacy_path.write_text(
            json.dumps({"schema": "closer-queue/v1", "round": {}, "items": {TARGET: item}})
        )
        with pytest.raises(RuntimeError):
            adapter._legacy_operation_dependency(legacy_path, TARGET, {"sources": []})


@pytest.mark.parametrize("inner_exists", [False, True], ids=["no-inner", "inner-no-operation"])
@pytest.mark.parametrize(
    "legacy_uncertain", [False, True], ids=["no-legacy-op", "legacy-uncertain"]
)
def test_reconcile_never_requeues_act_while_original_legacy_operation_is_uncertain(
    tmp_path, monkeypatch, inner_exists, legacy_uncertain
):
    current = snapshot()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "round": {},
                "items": {
                    TARGET: (
                        {
                            "operation": {
                                "id": "original-uncertain",
                                "status": "uncertain",
                                "repo_keys": ["o/r"],
                            }
                        }
                        if legacy_uncertain
                        else {}
                    ),
                },
            }
        )
    )
    action_path = tmp_path / "inner.json"
    if inner_exists:
        action_path.write_text(
            json.dumps({"schema": "closer-queue/v1", "round": {}, "items": {TARGET: {}}})
        )
    captured = {}

    def pipeline_stub(_path, _targets, **callbacks):
        captured["result"] = callbacks["reconcile"](
            TARGET,
            {
                "prepare": {"snapshot": current},
                "revision": current["revision"],
                "action_state_path": str(action_path),
            },
        )
        return {"targets": {TARGET: captured["result"]}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    monkeypatch.setattr(
        closer_queue,
        "run_round",
        lambda *_a, **_k: pytest.fail("legacy uncertainty must not retry an action"),
    )
    backend = closer(tmp_path, current)
    args = SimpleNamespace(state=str(state_path), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)
    assert captured["result"]["outcome"] == (
        "unknown" if legacy_uncertain else "confirmed_not_started"
    )
    if legacy_uncertain:
        assert "legacy uncertain operation" in captured["result"]["reason"]


def test_low_proposal_accepts_only_one_complete_json_document():
    payload = {"target": TARGET, "head": HEAD, "classification": "uncertain"}
    raw = json.dumps(payload)
    assert adapter._parse_low_proposal(raw) == payload
    assert adapter._parse_low_proposal(f"```json\n{raw}\n```") == payload
    assert adapter._parse_low_proposal(f"```\n{raw}\n```") == payload
    for invalid in (
        f"Here is the result:\n```json\n{raw}\n```",
        f"```json\n{raw}\n```\n```json\n{raw}\n```",
        f"```json\n{raw[:-1]}\n```",
        f"```json\n{raw}\n```\nextra prose",
    ):
        assert adapter._parse_low_proposal(invalid) == {}
    for value in ([], "text", 1, None, True):
        document = json.dumps(value)
        assert adapter._parse_low_proposal(document) == {}
        assert adapter._parse_low_proposal(f"```json\n{document}\n```") == {}
    wrong = {**payload, "head": "f" * 40}
    assert adapter._parse_low_proposal(f"```json\n{json.dumps(wrong)}\n```") == wrong


@pytest.mark.parametrize(
    "premerge", [True, False], ids=["premerge-no-model", "postverify-bound-receipt"]
)
@pytest.mark.parametrize("fenced", [False, True], ids=["plain-json", "single-json-fence"])
def test_low_receipt_enters_existing_acceptance_and_exact_head_merge_guard(
    tmp_path, monkeypatch, fenced, premerge
):
    stub_action_readiness_for_transport_test(monkeypatch)
    from subprocess import CompletedProcess

    import closer_disposition
    import closer_evidence
    import dispatcher

    current = (
        snapshot(base="c" * 40)
        if premerge
        else snapshot(
            state="MERGED",
            merge=MERGE,
            base="c" * 40,
            verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": MERGE},
        )
    )
    raw = tmp_path / "receipt.xml"
    raw.write_text("<testsuite tests='1'><testcase name='pass'/></testsuite>")
    ref = {"path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}
    decision = {
        "reviewer": "pipeline:real-run-1",
        "head": HEAD,
        "source_digest": adapter.source_digest(current["sources"]),
        "review_revision": current["review_revision"],
        "criteria": [
            {
                "source": source["target"],
                "criterion": criterion,
                "result": "PASS",
                "evidence": [ref],
            }
            for source in current["sources"]
            for criterion in [source["body"], *adapter.requirements(source["body"])]
        ],
    }
    packet = {
        "schema": "closer-evidence/v1",
        "producer_sha256": "d" * 64,
        "target": TARGET,
        "head": HEAD,
        "base": current["base"],
        "section_digests": {"source": "a", "ci_artifacts": "b"},
        "sections": {
            "source": {
                "status": "complete",
                "reason": None,
                "content": [],
                "provenance": {},
                "truncated": False,
            },
            "ci_artifacts": {
                "status": "partial",
                "reason": None,
                "content": {"payload": "x" * 30000},
                "provenance": {},
                "truncated": False,
            },
            "diff_files": {
                "status": "complete",
                "reason": None,
                "content": {"patch": "y" * 30000},
                "provenance": {},
                "truncated": False,
            },
        },
        "manifest": {"packet_sha256": "packet"},
    }

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)

    def collect(_snap, _api, retained_dir, **_kwargs):
        path = Path(retained_dir) / "manifests" / "packet.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(packet))
        return packet

    monkeypatch.setattr(closer_evidence, "collect_evidence", collect)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: True)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    monkeypatch.setattr(
        adapter,
        "_pipeline_low_route",
        lambda **kw: (
            ({"agent": "gemini", "profile_id": "gemini-low", "pools": []}, "available")
            if kw.get("with_status")
            else {"agent": "gemini", "profile_id": "gemini-low", "pools": []}
        ),
    )
    worker_calls = []
    prompts = []

    def low_worker(_agent, prompt, **_kwargs):
        prompts.append(prompt)
        payload = json.dumps(
            {
                "target": TARGET,
                "head": HEAD,
                "base": current["base"],
                "classification": "relevant",
                "substantive_needs": [],
                "findings": [],
                "decision": {"criteria": []},
            }
        )
        return {
            "exit": 0,
            "run_id": "real-run-1",
            "model": "gemini-test",
            "profile_id": "gemini-low",
            "output": f"```json\n{payload}\n```" if fenced else payload,
        }

    monkeypatch.setattr(dispatcher, "offload", low_worker)
    monkeypatch.setattr(
        closer_disposition,
        "integrate_ordinary",
        lambda *_a, **_k: {
            "status": "accepted",
            "reason": "validated raw receipt",
            "disposition": decision,
            "raw_findings": [],
        },
    )
    guarded, closed = [], []

    def close_sources(current_backend, target, captured, **_kwargs):
        assert not premerge
        assert captured["state"] == "MERGED" and captured["merge"] == MERGE
        assert captured["verifier"]["evaluated_sha"] == MERGE
        assert adapter.acceptance_ok(captured, current_backend.dispositions[target])
        closed.append((target, captured["head"]))
        return {"status": "completed"}

    monkeypatch.setattr(adapter.Closer, "close_sources", close_sources)

    def run_guard(command, *, timeout):
        guarded.append((command, timeout))
        return CompletedProcess(
            command, 0, json.dumps({"merge_executed": True, "blocked": False}), ""
        )

    backend = closer(tmp_path, current, apply=True, workers=True, run_fn=run_guard)
    monkeypatch.setattr(
        backend, "campaign_admission", lambda *_a, **_k: {"allowed": True, "timeout": 60}
    )
    monkeypatch.setattr(
        backend, "worker", lambda *a, **k: worker_calls.append(a) or {"status": "failed"}
    )

    def guarded_queue_once(_path, selected, observe, execute, *, deadline, **_kwargs):
        target = selected[0]
        observed = observe(target)
        executed = execute(
            target,
            observed["action"],
            observed,
            {"deadline": deadline, "id": "fake-queue-operation", "owner": "inert-test-queue"},
        )
        return {
            "stop_valid": True,
            "accounting": [
                {
                    "target": target,
                    "status": "complete" if executed.get("status") == "completed" else "observed",
                    "eligible": True,
                    "reason": executed.get("reason"),
                    "action": observed["action"],
                }
            ],
            "events": [],
        }

    monkeypatch.setattr(closer_queue, "run_round", guarded_queue_once)

    def fake_pipeline(path, targets, **kwargs):
        prepared = kwargs["prepare"](TARGET, {"deadline": time.time() + 60})
        interpreted = kwargs["interpret"](
            TARGET, {"prepare": prepared, "deadline": time.time() + 60}
        )
        if premerge:
            assert interpreted["classification"] == "deterministic_merge"
            assert "actual" not in interpreted
        else:
            assert interpreted["actual"]["run_id"] == "real-run-1"
        action = kwargs["act"](
            TARGET,
            {
                "prepare": prepared,
                "interpret": interpreted,
                "deadline": time.time() + 60,
                "revision": prepared["revision"],
                "work_key": "work-1",
                "action_state_path": str(tmp_path / "actions.json"),
            },
        )
        return {"targets": {TARGET: action}}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(run_pipeline=fake_pipeline))
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    result = adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 360)
    assert result["targets"][TARGET]["status"] in {"completed", "pending", "deferred"}
    if premerge:
        assert prompts == [] and closed == []
        assert guarded and guarded[0][0][1].endswith("merge_guard.py")
        command = guarded[0][0]
        assert command[command.index("--expected-head") + 1] == HEAD
        assert "--confirm-merge" in command
    else:
        assert len(prompts) == 1 and not guarded
        assert closed == [(TARGET, HEAD)]
        mandatory = prompts[0].split(
            "Bounded packet samples (optional; refs above are authoritative):", 1
        )[0]
        prepared_ref = tmp_path / "state.json.evidence" / "manifests" / "packet.json"
        assert prepared_ref.is_file() and str(prepared_ref) in mandatory
        assert adapter.evidence_file_ok(ref)
    assert not worker_calls


def test_held_assignment_metadata_blocks_pipeline_before_worker_or_action(tmp_path, monkeypatch):
    import closer_evidence

    assignment = tmp_path / "assignment.json"
    assignment.write_text(
        json.dumps(
            {
                "errors": [],
                "targets": [
                    {
                        "target": TARGET,
                        "execution_state": "held",
                        "fingerprint": "row-fp-1",
                        "delivery_owner": "Workflows#4057",
                    }
                ],
            }
        )
    )
    current = snapshot(checks="SUCCESS", base="c" * 40)

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    packet = {
        "schema": "closer-evidence/v1",
        "target": TARGET,
        "head": HEAD,
        "base": current["base"],
        "producer_sha256": "d" * 64,
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
        "section_digests": {},
        "sections": {},
        "manifest": {"packet_sha256": "f" * 64},
    }
    monkeypatch.setattr(closer_evidence, "collect_evidence", lambda *_a, **_k: packet)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: False)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 1, "low": 1, "exception": 1})
    monkeypatch.setattr(
        adapter,
        "_pipeline_low_route",
        lambda **_kw: pytest.fail("held target must not dispatch a model"),
    )
    monkeypatch.setattr(
        closer_queue,
        "run_round",
        lambda *_a, **_k: pytest.fail("held target must not enter action queue"),
    )
    backend = closer(
        tmp_path,
        current,
        apply=True,
        workers=True,
        assignment_rows={TARGET: json.loads(assignment.read_text())["targets"][0]},
    )
    args = SimpleNamespace(
        state=str(tmp_path / "state.json"), assignment=str(assignment), max_workers=1, shadow=False
    )
    receipt = adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 330)
    row = receipt["targets"][TARGET]
    assert row["status"] == "pending"
    assert row["pending_kind"] == "external_event"
    assert row["pending_evidence"]["event_id"] == "row-fp-1"
    assert "Workflows#4057" in row["deferred_next_trigger"]


def test_assignment_guard_rechecks_immediately_before_action(tmp_path):
    current = snapshot()
    calls = []
    backend = closer(
        tmp_path,
        current,
        apply=True,
        assignment_guard_fn=lambda target: (
            calls.append(target)
            or {
                "status": "pending",
                "pending_kind": "external_event",
                "next_trigger": "Workflows#4057 releases assignment hold",
                "pending_evidence": {"event_id": "row-fp-1", "target": target},
            }
        ),
    )
    result = backend.execute(
        TARGET, "merge", current, {"deadline": time.time() + 30, "id": "unused"}
    )
    assert calls == [TARGET]
    assert result["status"] == "pending"
    assert not backend.gh.reads


def test_active_assignment_fingerprint_refresh_does_not_block_fresh_target(tmp_path):
    assignment = tmp_path / "assignment.json"
    assignment.write_text(
        json.dumps(
            {
                "errors": [],
                "targets": [
                    {
                        "target": TARGET,
                        "execution_state": "active",
                        "fingerprint": "new-controller-fingerprint",
                    }
                ],
            }
        )
    )
    assert (
        adapter.assignment_dependency(
            assignment, TARGET, expected_fingerprint="older-active-fingerprint"
        )
        is None
    )


@pytest.mark.parametrize("active_seconds", [90, 240, 2700])
def test_pipeline_deadline_preserves_active_budget_and_5m_receipt_reserve(
    tmp_path, monkeypatch, active_seconds
):
    import closer_evidence
    import closer_queue

    start = time.time()
    full_deadline = start + active_seconds + 300
    current = snapshot(base="c" * 40)

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    packet = {
        "schema": "closer-evidence/v1",
        "target": TARGET,
        "head": HEAD,
        "base": current["base"],
        "producer_sha256": "d" * 64,
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
        "sections": {},
        "manifest": {"packet_sha256": "f" * 64},
    }
    monkeypatch.setattr(closer_evidence, "collect_evidence", lambda *_a, **_k: packet)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: False)
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 4, "low": 1, "exception": 1})
    queue_calls = []

    def queue_once(*_a, **kwargs):
        queue_calls.append(kwargs)
        return {
            "stop_valid": True,
            "accounting": [
                {"target": TARGET, "status": "observed", "eligible": False, "reason": "hold"}
            ],
            "events": [],
        }

    monkeypatch.setattr(closer_queue, "run_round", queue_once)
    observed = {}

    def pipeline_stub(_path, _targets, **callbacks):
        observed["outer_deadline"] = callbacks["deadline"]
        observed["max_io_workers"] = callbacks["max_io_workers"]
        context_deadline = callbacks["deadline"]
        prep = callbacks["prepare"](TARGET, {"deadline": context_deadline})
        observed["prepare_deadline"] = context_deadline
        callbacks["act"](
            TARGET,
            {
                "prepare": prep,
                "deadline": context_deadline,
                "action_state_path": str(tmp_path / "inner.json"),
                "revision": prep["revision"],
                "work_key": "test",
            },
        )
        return {"targets": {TARGET: {"status": "completed"}}}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(run_pipeline=pipeline_stub))
    backend = closer(tmp_path, current, apply=True, workers=True)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], backend, full_deadline)

    active_cutoff = start + active_seconds
    assert abs(observed["outer_deadline"] - active_cutoff) < 0.01
    assert observed["prepare_deadline"] == observed["outer_deadline"]
    assert observed["max_io_workers"] == 4
    assert len(queue_calls) == 1
    assert abs(queue_calls[0]["deadline"] - full_deadline) < 0.01
    assert queue_calls[0]["receipt_reserve_seconds"] == 300
    # Inner action cutoff preserves the requested active budget literally.
    assert queue_calls[0]["deadline"] - 300 == observed["outer_deadline"]


def test_deterministic_dispatcher_states_skip_low_provider_stage(tmp_path, monkeypatch):
    import closer_evidence
    import dispatcher

    issue_target = "o/r#21"
    wait_target = "o/r#22"
    verify_target = "o/r#23"
    snaps = {
        issue_target: snapshot(target=issue_target, kind="issue", state="CLOSED"),
        wait_target: snapshot(target=wait_target, checks="PENDING"),
        verify_target: snapshot(target=verify_target, state="MERGED", merge=MERGE),
    }

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(None)
            self.deadline = deadline

        def snapshot(self, target):
            self.reads += 1
            row = copy.deepcopy(snaps[target])
            row["target"] = target
            row["revision"] = adapter.digest(
                {k: v for k, v in row.items() if k not in {"revision", "observed_at"}}
            )
            return row

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    monkeypatch.setattr(
        closer_evidence,
        "collect_evidence",
        lambda snap, *_a, **_k: {
            "schema": "closer-evidence/v1",
            "target": snap["target"],
            "head": snap.get("head"),
            "base": snap.get("base"),
            "producer_sha256": "d" * 64,
            "sections": {},
            "manifest": {"packet_sha256": hashlib.sha256(snap["target"].encode()).hexdigest()},
        },
    )
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: False)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    monkeypatch.setattr(
        dispatcher,
        "offload",
        lambda *_a, **_k: pytest.fail("deterministic state dispatched low provider"),
    )
    seen = {}

    def pipeline_stub(_path, targets, **callbacks):
        for target in targets:
            prep = callbacks["prepare"](target, {"deadline": time.time() + 60})
            seen[target] = callbacks["interpret"](
                target, {"prepare": prep, "deadline": time.time() + 60}
            )
        return {"targets": {target: {"status": "pending"} for target in targets}}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(run_pipeline=pipeline_stub))
    backend = closer(tmp_path, snapshot())
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(
        args, [issue_target, wait_target, verify_target], backend, time.time() + 60
    )
    assert seen[issue_target]["classification"] == "deterministic_complete"
    assert seen[wait_target]["classification"] == "deterministic_wait"
    assert seen[verify_target]["classification"] == "deterministic_post_merge_verify"
    assert all("actual" not in result for result in seen.values())


@pytest.mark.parametrize(
    "failure_kind",
    ["transport", "refusal", "inspection", "malformed", "quota", "quota_normal_exit"],
)
def test_low_failure_scope_preserves_item_holds_and_real_provider_failures(
    tmp_path, monkeypatch, failure_kind
):
    stub_action_readiness_for_transport_test(monkeypatch)
    import closer_disposition
    import closer_evidence
    import dispatcher

    first, second = "o/r#31", "o/r#32"
    base = "c" * 40
    snapshots = {target: snapshot(target=target, base=base) for target in (first, second)}

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(None)
            self.deadline = deadline

        def snapshot(self, target):
            return copy.deepcopy(snapshots[target])

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _p: True)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_a, **_k: [])
    backend = closer(tmp_path, snapshots[first])
    monkeypatch.setattr(
        backend, "campaign_admission", lambda *_a, **_k: {"allowed": True, "timeout": 60}
    )
    route_profiles = []

    def select_route(*, load, unavailable_profiles, with_status=False):
        if "gemini-low" not in unavailable_profiles:
            profile = "gemini-low"
        elif "claude-low" not in unavailable_profiles:
            profile = "claude-low"
        else:
            return (None, "exhausted") if with_status else None
        route_profiles.append(profile)
        route = {"agent": profile.split("-")[0], "profile_id": profile, "pools": []}
        return (route, "available") if with_status else route

    monkeypatch.setattr(adapter, "_pipeline_low_route", select_route)
    provider_profiles = []

    def offload(_agent, _prompt, **kwargs):
        profile = kwargs["profile_id"]
        provider_profiles.append(profile)
        assert "honest missing-evidence assessment is a completed deliverable" in _prompt
        assert "leave decision.criteria empty" in _prompt
        assert "findings (array of objects, never strings" in _prompt
        assert "evidence gaps as strings in substantive_needs" in _prompt
        assert_nonpass_accounting_prompt_contract(_prompt)
        assert "only when every original body and criterion is fully evidenced" in _prompt
        assert (
            "If acceptance proof is incomplete and the known NON_PASS accounting contract below does not apply"
            in _prompt
        )
        assert "read-only commands such as python3 -c" in _prompt
        if profile == "gemini-low" and kwargs["subject_id"] == first:
            result = {"exit": 1, "run_id": "dispatch-failed-31", "error": "unsupported model"}
            if failure_kind in {"refusal", "inspection", "quota_normal_exit"}:
                result.update(
                    exit=70,
                    raw_exit=0,
                    error="agent reported OFFLOAD_INCOMPLETE",
                    output=(
                        "OFFLOAD_INCOMPLETE: Cannot truthfully provide required PASS rows: "
                        "qualifying evidence is missing and the operational check is future-dated."
                        if failure_kind == "refusal"
                        else "OFFLOAD_INCOMPLETE: read-only inspection could not create a temporary file."
                    ),
                )
            if failure_kind == "malformed":
                result.update(exit=0, output='{"classification": "uncertain",')
            if failure_kind in {"quota", "quota_normal_exit"}:
                if failure_kind == "quota":
                    result.update(exit=3, raw_exit=3)
                result["rate_incident_evidence"] = {
                    "category": "quota_exhausted",
                    "is_authoritative": True,
                    "evidence_hash": "f" * 64,
                }
            return result
        return {
            "exit": 0,
            "run_id": "dispatch-success-32",
            "model": "claude-test",
            "profile_id": profile,
            "output": json.dumps(
                {
                    "target": kwargs["subject_id"],
                    "head": HEAD,
                    "base": base,
                    "classification": "relevant",
                    "substantive_needs": [],
                    "findings": [],
                    "decision": {"criteria": []},
                }
            ),
        }

    monkeypatch.setattr(dispatcher, "offload", offload)
    integrations = []
    ordinary_validation = closer_disposition.integrate_ordinary

    def integrate(target, *_a, **_k):
        integrations.append(target)
        return {"status": "wait", "reason": "evidence incomplete", "raw_findings": []}

    # Interpretation must not submit a refusal/malformed response for acceptance.
    monkeypatch.setattr(
        closer_disposition,
        "integrate_ordinary",
        integrate,
    )

    def packet_for(target):
        packet = {
            "schema": "closer-evidence/v1",
            "target": target,
            "head": HEAD,
            "base": base,
            "producer_sha256": "d" * 64,
            "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
            "sections": {},
            "manifest": {"packet_sha256": target.replace("/", "_").replace("#", "-")},
        }
        path = tmp_path / (target.replace("/", "_").replace("#", "-") + ".json")
        path.write_text(json.dumps(packet))
        snap = snapshots[target]
        snapshot_path = tmp_path / (target.replace("/", "_").replace("#", "-") + ".snapshot.json")
        snapshot_path.write_text(json.dumps(snap, sort_keys=True, separators=(",", ":")))
        return {
            "revision": snap["revision"],
            "identity": target,
            "producer_sha256": packet["producer_sha256"],
            "semantic_cache_key": packet["semantic_cache_key"],
            "snapshot": {
                "target": target,
                "head": HEAD,
                "base": base,
                "revision": snap["revision"],
                "checks": "SUCCESS",
            },
            "snapshot_ref": {
                "path": str(snapshot_path),
                "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            },
            "packet_ref": {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
            "evidence_summary": {"drift_sample": [], "sections": {}},
        }

    def pipeline_stub(_path, targets, **callbacks):
        values = {}
        for target in targets:
            values[target] = callbacks["interpret"](
                target, {"prepare": packet_for(target), "deadline": time.time() + 60}
            )
        if failure_kind in {"refusal", "inspection", "malformed"}:
            assert values[first]["actual"]["run_id"] == "dispatch-failed-31"
            assert values[first]["classification"] == "uncertain"
            assert values[first]["substantive_needs"]
            assert values[first]["integration_status"] == "wait"
            assert not values[first]["integration_disposition"]
            assert not values[first]["prior_provider_failures"]
            if failure_kind != "malformed":
                assert values[first]["actual"]["exit"] == 70
                assert values[first]["actual"]["raw_exit"] == 0
                assert values[first]["substantive_needs"] == ["incomplete_low_assessment"]
                assert "OFFLOAD_INCOMPLETE" in values[first]["proposal"]["raw_excerpt"]
                # A later action may validate the original receipt again. It
                # must still reject its actual exit70, regardless of raw_exit0.
                guarded = ordinary_validation(first, snapshots[first], {}, {}, values[first])
                assert guarded["status"] == "wait"
                assert guarded["reason"] == "worker_execution_unverified"
                assert not guarded.get("disposition")
        else:
            assert values[first]["actual"]["run_id"] == "dispatch-success-32"
            assert values[first]["prior_provider_failures"][0]["run_id"] == "dispatch-failed-31"
        assert values[second]["actual"]["run_id"] == "dispatch-success-32"
        return {"targets": values}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(run_pipeline=pipeline_stub))
    args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [first, second], backend, time.time() + 360)
    if failure_kind in {"refusal", "inspection", "malformed"}:
        assert route_profiles == ["gemini-low", "gemini-low"]
        assert integrations == [second]
    else:
        assert route_profiles == ["gemini-low", "claude-low", "claude-low"]
        assert integrations == [first, second]
    assert provider_profiles == route_profiles

    if failure_kind != "transport":
        return

    route_profiles.clear()
    provider_profiles.clear()

    def fail_all(_agent, _prompt, **kwargs):
        profile = kwargs["profile_id"]
        provider_profiles.append(profile)
        return {
            "exit": 1,
            "run_id": f"dispatch-failed-{len(provider_profiles)}",
            "error": "provider unavailable",
        }

    monkeypatch.setattr(dispatcher, "offload", fail_all)
    backend = closer(tmp_path, snapshots[first])
    monkeypatch.setattr(
        backend, "campaign_admission", lambda *_a, **_k: {"allowed": True, "timeout": 60}
    )
    last = {}

    def all_failed_pipeline_stub(_path, _targets, **callbacks):
        last.update(
            callbacks["interpret"](
                first, {"prepare": packet_for(first), "deadline": time.time() + 60}
            )
        )
        return {"targets": {first: last}}

    monkeypatch.setitem(
        sys.modules,
        "closer_pipeline",
        SimpleNamespace(run_pipeline=all_failed_pipeline_stub),
    )
    adapter._run_staged_pipeline(args, [first], backend, time.time() + 360)
    assert last["reason"] == "provider_failure"
    assert [row["run_id"] for row in last["resource_evidence"]["provider_failure"]["attempts"]] == [
        "dispatch-failed-1",
        "dispatch-failed-2",
    ]
    assert all(
        len(row["evidence_hash"]) == 64 and row["category"] == "transport_error"
        for row in last["resource_evidence"]["provider_failure"]["attempts"]
    )
    assert route_profiles == ["gemini-low", "claude-low"]
    assert provider_profiles == route_profiles


@pytest.mark.parametrize("accounting_pending", [False, True])
def test_uncertain_provider_result_stops_before_fallback_exception_or_action(
    tmp_path, monkeypatch, accounting_pending
):
    stub_action_readiness_for_transport_test(monkeypatch)
    import closer_evidence
    import dispatcher

    target = "o/r#41"
    base = "c" * 40
    current = snapshot(target=target, base=base)

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(None)
            self.deadline = deadline

        def snapshot(self, _target):
            return copy.deepcopy(current)

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda _packet: True)
    monkeypatch.setattr(closer_evidence, "drift_sample", lambda *_args, **_kwargs: [])
    backend = closer(tmp_path, current)
    monkeypatch.setattr(
        backend, "campaign_admission", lambda *_args, **_kwargs: {"allowed": True, "timeout": 60}
    )
    chosen = []

    def route(*, load, unavailable_profiles, with_status=False):
        profile = "gemini-low" if "gemini-low" not in unavailable_profiles else "claude-low"
        chosen.append(profile)
        value = {"agent": profile.split("-")[0], "profile_id": profile, "pools": []}
        return (value, "available") if with_status else value

    monkeypatch.setattr(adapter, "_pipeline_low_route", route)
    calls = []

    def offload(agent, _prompt, **kwargs):
        calls.append((agent, kwargs["profile_id"]))
        result = {
            "exit": 124,
            "run_id": "agy-turn-still-running-41",
            "outcome_status": "uncertain",
            "outcome_uncertain": True,
            "retry_safe": False,
            "error": "remote turn still in progress",
        }
        if accounting_pending:
            result.update(
                exit=70,
                raw_exit=0,
                provider_completed=True,
                output='{"classification":"relevant","findings":[]}',
                completion_persistence={
                    "schema": "offload-completion-persistence/v1",
                    "status": "pending",
                },
            )
        return result

    monkeypatch.setattr(dispatcher, "offload", offload)
    packets = {}

    def prepare(target_name):
        packet = {
            "schema": "closer-evidence/v1",
            "target": target_name,
            "head": HEAD,
            "base": base,
            "producer_sha256": "d" * 64,
            "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
            "sections": {},
            "manifest": {"packet_sha256": "f" * 64},
        }
        packet_path = tmp_path / "uncertain-packet.json"
        packet_path.write_text(json.dumps(packet))
        snap = copy.deepcopy(current)
        snap_path = tmp_path / "uncertain-snapshot.json"
        snap_path.write_text(json.dumps(snap, sort_keys=True, separators=(",", ":")))
        prepared = {
            "revision": snap["revision"],
            "identity": target_name,
            "semantic_cache_key": packet["semantic_cache_key"],
            "producer_sha256": packet["producer_sha256"],
            "snapshot": {
                "target": target_name,
                "head": HEAD,
                "base": base,
                "revision": snap["revision"],
            },
            "snapshot_ref": {
                "path": str(snap_path),
                "sha256": hashlib.sha256(snap_path.read_bytes()).hexdigest(),
            },
            "packet_ref": {
                "path": str(packet_path),
                "sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            },
            "evidence_summary": {"drift_sample": [], "sections": {}},
        }
        packets[target_name] = prepared
        return prepared

    def pipeline_stub(_path, targets, **callbacks):
        target_name = targets[0]
        interpreted = callbacks["interpret"](
            target_name,
            {"prepare": prepare(target_name), "deadline": time.time() + 60},
        )
        assert interpreted["status"] == "uncertain"
        assert interpreted["provider_outcome"]["actual"]["run_id"] == ("agy-turn-still-running-41")
        if accounting_pending:
            actual = interpreted["provider_outcome"]["actual"]
            assert actual["provider_completed"] and actual["raw_exit"] == 0
            assert actual["completion_persistence"]["status"] == "pending"
        assert "substantive_needs" in interpreted
        return {"targets": {target_name: interpreted}}

    monkeypatch.setitem(sys.modules, "closer_pipeline", SimpleNamespace(run_pipeline=pipeline_stub))
    args = SimpleNamespace(state=str(tmp_path / "pipeline-state.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [target], backend, time.time() + 360)

    assert chosen == ["gemini-low"]
    assert calls == [("gemini", "gemini-low")]


def _adopted_pipeline(tmp_path, *, active=False):
    import closer_pipeline

    path = adapter._pipeline_state_path(tmp_path / "legacy-queue.json")
    at = time.time() - 100
    closer_pipeline.run_pipeline(
        path,
        [TARGET],
        prepare=lambda target, _ctx: {"revision": "read-revision", "snapshot": {"target": target}},
        interpret=lambda *_: {"substantive_needs": []},
        exception=lambda *_: pytest.fail("no exception"),
        act=lambda *_: {"status": "completed", "target_complete": True},
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=at + 10,
        now=lambda: at,
    )
    if active:
        doc = closer_pipeline._load(path)
        latest = next(iter(doc["runs"].values()))
        latest["deadline"] = time.time() + 45
        latest["receipt_summary"]["deadline"] = latest["deadline"]
        closer_pipeline._save(path, doc)
    return path


def test_cli_pipeline_budget_does_not_renew_or_inherit_stale_legacy_accounting(
    tmp_path, monkeypatch, capsys
):
    start = time.time()
    monkeypatch.setattr(adapter.time, "time", lambda: start)
    state = _terminal_queue_state(tmp_path)
    state_path = tmp_path / "legacy-queue.json"
    state["items"][TARGET]["revision"] = "later-assessment"
    state_path.write_text(json.dumps(state))
    before = state_path.read_bytes()
    assert adapter._validly_finished_legacy_round(state_path, state) is False
    _adopted_pipeline(tmp_path)
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    seen = []
    monkeypatch.setattr(adapter, "GitHub", lambda *, deadline: SimpleNamespace(deadline=deadline))
    monkeypatch.setattr(adapter, "Closer", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(adapter, "known_workspaces", lambda _targets: {})
    monkeypatch.setattr(
        adapter,
        "_run_staged_pipeline",
        lambda _a, _t, _b, deadline: seen.append(deadline) or {"status": "budget-observed"},
    )
    assert (
        adapter.main(
            [
                "run",
                "--assignment",
                str(assignment),
                "--state",
                str(state_path),
                "--budget-seconds",
                "10",
                "--pipeline",
                "--apply",
                "--workers",
            ]
        )
        == 0
    )
    admission = json.loads(capsys.readouterr().out)["budget_admission"]
    assert seen == [start + 10 + 300]
    assert admission == {
        "reason": "valid_pipeline_stop",
        "scope": "pipeline",
        "requested_deadline": start + 310,
        "selected_deadline": start + 310,
        "legacy_deadline": state["round"]["deadline"],
        "legacy_state_digest": adapter.digest(state),
        "legacy_round_renewed": False,
    }
    assert state_path.read_bytes() == before
    assert (
        adapter._deadline_with_legacy_round(time.time() + 310, state, state_path)
        == state["round"]["deadline"]
    )


def test_expired_admission_reports_drainable_zero_as_deadlock(tmp_path, monkeypatch, capsys):
    import closer_resources

    state = _terminal_queue_state(tmp_path)
    state["items"][TARGET]["revision"] = "invalidates-old-stop"
    state_path = tmp_path / "legacy-queue.json"
    state_path.write_text(json.dumps(state))
    pipeline_path = adapter._pipeline_state_path(state_path)
    pipeline = closer_pipeline._empty()
    pipeline["runs"]["prior"] = {
        "created": 1,
        "deadline": 2,
        "assignment": [TARGET],
        "targets": {TARGET: {"status": "uncertain"}},
    }
    pipeline_path.write_text(json.dumps(pipeline))
    with closer_resources.legacy_reserve(
        closer_resources.scoped_root(pipeline_path),
        ["pr:" + TARGET],
        target=TARGET,
        owner="original-owner",
        deadline=time.time() + 5,
        causal_binding={"pipeline_run_id": "prior"},
    ):
        pass
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    monkeypatch.setattr(adapter, "GitHub", lambda *, deadline: SimpleNamespace(deadline=deadline))
    monkeypatch.setattr(adapter, "Closer", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(adapter, "known_workspaces", lambda _targets: {})
    monkeypatch.setattr(
        adapter, "_run_staged_pipeline", lambda *_: {"reason": "admission_deadline_expired"}
    )
    monkeypatch.setattr(closer_pipeline, "validate_stop", lambda *_a, **_k: {"valid": False})
    assert (
        adapter.main(
            [
                "run",
                "--assignment",
                str(assignment),
                "--state",
                str(state_path),
                "--pipeline",
                "--apply",
                "--workers",
            ]
        )
        == 78
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["budget_admission"]["selected_deadline"] == state["round"]["deadline"]
    assert receipt["blocking_reservations"] == 1
    assert receipt["drainable_reservations"] == 0
    assert receipt["deadlock"] is True
    assert receipt["reservation_recovery"][0]["recovery_path"] == "plan_legacy_reconciliation"
    assert receipt["reservation_recovery"][0]["reasons"]
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "malformed",
        "unexpired",
        "missing_receipt",
        "receipt_type",
        "receipt_run",
        "receipt_deadline",
        "receipt_time",
        "unrecognized_stop",
        "archive",
        "malformed_extra_run",
    ],
)
def test_independent_pipeline_budget_requires_its_own_admission_proof(tmp_path, case):
    import closer_pipeline

    legacy = _terminal_queue_state(tmp_path)
    state_path = tmp_path / "legacy-queue.json"
    legacy["items"][TARGET]["revision"] = "later-assessment"
    state_path.write_text(json.dumps(legacy))
    path = _adopted_pipeline(tmp_path, active=case == "unexpired")
    state = closer_pipeline._load(path)
    rid, previous = next(iter(state["runs"].items()))
    if case == "missing":
        path.unlink()
    elif case == "empty":
        closer_pipeline._save(path, closer_pipeline._empty())
    elif case == "malformed":
        path.write_text('{"schema":"untrusted"}')
    else:
        if case == "missing_receipt":
            previous.pop("receipt_summary")
        elif case == "receipt_type":
            previous["receipt_summary"] = "unbound"
        elif case == "receipt_run":
            previous["receipt_summary"]["run_id"] = "unbound"
        elif case == "receipt_deadline":
            previous["receipt_summary"]["deadline"] += 1
        elif case == "receipt_time":
            previous["receipt_summary"]["finished_at"] = time.time() + 1000
        elif case == "unrecognized_stop":
            previous["targets"][TARGET]["status"] = "unrecognized"
        elif case == "archive":
            previous["invalid_stop_archive"] = {"schema": "unbound"}
        elif case == "malformed_extra_run":
            state["runs"]["unreadable-newer"] = {"created": None}
        closer_pipeline._save(path, state)
    legacy_before = state_path.read_bytes()
    pipeline_before = path.read_bytes() if path.exists() else None
    selected, reason = adapter._deadline_with_pipeline_round(time.time() + 310, legacy, state_path)
    assert selected == legacy["round"]["deadline"]
    assert reason and reason != "valid_pipeline_stop"
    assert state_path.read_bytes() == legacy_before
    assert (path.read_bytes() if path.exists() else None) == pipeline_before


@pytest.mark.parametrize("status", ["submitted", "running", "uncertain"])
def test_fresh_pipeline_budget_never_releases_a_legacy_operation_barrier(tmp_path, status):
    state = _terminal_queue_state(tmp_path)
    state_path = tmp_path / "legacy-queue.json"
    state["items"][TARGET]["operation"] = {
        "id": "original-native-operation",
        "status": status,
        "repo_keys": [TARGET.split("#")[0]],
    }
    state_path.write_text(json.dumps(state))
    _adopted_pipeline(tmp_path)
    before = state_path.read_bytes()
    requested = time.time() + 310
    assert adapter._deadline_with_pipeline_round(requested, state, state_path) == (
        requested,
        "valid_pipeline_stop",
    )
    with pytest.raises(RuntimeError, match="reconcile it in the original closer queue"):
        adapter._legacy_operation_dependency(state_path, TARGET, snapshot())
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("branch", ["safe_pipeline_continuation", "expired_pipeline_recovery"])
def test_pipeline_budget_reports_native_admission_branch(tmp_path, monkeypatch, branch):
    import closer_pipeline

    state = _terminal_queue_state(tmp_path)
    state["items"][TARGET]["revision"] = "later-assessment"
    state_path = tmp_path / "legacy-queue.json"
    state_path.write_text(json.dumps(state))
    _adopted_pipeline(tmp_path)
    monkeypatch.setattr(closer_pipeline, "validate_stop", lambda *_a, **_k: {"valid": False})
    monkeypatch.setattr(
        closer_pipeline,
        "_safe_prior_continuation",
        lambda *_a: (branch == "safe_pipeline_continuation", False, set()),
    )
    monkeypatch.setattr(
        closer_pipeline,
        "_expired_invalid_round_recovery",
        lambda *_a: (branch == "expired_pipeline_recovery",),
    )
    requested = time.time() + 310
    assert adapter._deadline_with_pipeline_round(requested, state, state_path) == (
        requested,
        branch,
    )


@pytest.mark.parametrize("error", [AttributeError("bad proof"), RuntimeError("unreadable proof")])
def test_pipeline_budget_proof_exception_is_diagnosed_without_renewal(tmp_path, monkeypatch, error):
    import closer_pipeline

    state = _terminal_queue_state(tmp_path)
    state["items"][TARGET]["revision"] = "later-assessment"
    state_path = tmp_path / "legacy-queue.json"
    state_path.write_text(json.dumps(state))
    _adopted_pipeline(tmp_path)

    def reject(*_a, **_k):
        raise error

    monkeypatch.setattr(closer_pipeline, "validate_stop", reject)
    assert adapter._deadline_with_pipeline_round(time.time() + 310, state, state_path) == (
        state["round"]["deadline"],
        f"pipeline_adoption_unverifiable:{type(error).__name__}",
    )


@pytest.mark.parametrize(
    "stage,status",
    [
        ("prepare", "queued"),
        ("prepare", "running"),
        ("interpret", "queued"),
        ("interpret", "running"),
        ("exception", "running"),
    ],
)
def test_expired_interrupted_read_can_receive_independent_cli_budget(tmp_path, stage, status):
    import closer_pipeline

    legacy = _terminal_queue_state(tmp_path)
    legacy["items"][TARGET]["revision"] = "later-assessment"
    state_path = tmp_path / "legacy-queue.json"
    state_path.write_text(json.dumps(legacy))
    path = _adopted_pipeline(tmp_path)
    state = closer_pipeline._load(path)
    previous = next(iter(state["runs"].values()))
    previous.pop("receipt_summary")
    previous["targets"][TARGET] = {
        "status": status,
        "stage": stage,
        "results": {},
        "metrics": {"waits": 0, "errors": 0},
    }
    closer_pipeline._save(path, state)
    before = state_path.read_bytes(), path.read_bytes()
    requested = time.time() + 310
    assert adapter._deadline_with_pipeline_round(requested, legacy, state_path) == (
        requested,
        "expired_pipeline_recovery",
    )
    assert before == (state_path.read_bytes(), path.read_bytes())


@pytest.mark.parametrize("changed", [None, "head", "branch", "source"])
def test_scoped_native_operation_seam_rechecks_prepared_head_and_aliases(
    tmp_path, monkeypatch, changed
):
    import closer_resources

    current = snapshot(head_branch="feature", head_repository="o/r")
    gh = FakeGitHub(current)
    monkeypatch.setattr(adapter, "GitHub", lambda **kw: gh)
    monkeypatch.setattr(
        closer_evidence,
        "collect_evidence",
        lambda *a, **kw: {
            "schema": "closer-evidence/v1",
            "sections": {},
            "manifest": {"packet_sha256": "x"},
        },
    )
    monkeypatch.setattr(closer_evidence, "validate_packet", lambda *a: False)
    calls = []
    # Only the native effect is synthetic; the dispatcher's head/resource binding stays real.
    monkeypatch.setattr(
        adapter.Closer, "execute", lambda *a: calls.append(a) or {"status": "completed"}
    )

    def native_queue_stub(_path, targets, observe, execute, **kw):
        observed = observe(TARGET)
        execute(
            TARGET, observed["action"], observed, {"id": "offline", "deadline": time.time() + 20}
        )
        return {"stop_valid": True, "accounting": [{"target": TARGET, "status": "complete"}]}

    monkeypatch.setattr(closer_queue, "run_round", native_queue_stub)

    def pipeline_stub(_path, _targets, **callbacks):
        prep = callbacks["prepare"](TARGET, {"deadline": time.time() + 30})
        callbacks["resource_keys"](TARGET, {"prepare": prep})
        if changed == "head":
            gh.current["head"] = "f" * 40
        elif changed == "branch":
            gh.current["head_branch"] = "other"
        elif changed == "source":
            gh.current["sources"] = [
                {
                    "target": "o/r#99",
                    "body": "## Acceptance criteria\n- Preserve behavior",
                    "state": "open",
                }
            ]
        gh.current["revision"] = adapter.digest(gh.current)
        context = {
            "prepare": prep,
            "interpret": {},
            "deadline": time.time() + 30,
            "revision": prep["revision"],
            "state_path": str(_path),
            "action_state_path": str(tmp_path / "action.json"),
        }
        if changed:
            with pytest.raises(closer_resources.ResourceBlocked, match="actual operation changed"):
                callbacks["act"](TARGET, context)
            assert not calls
        else:
            assert callbacks["act"](TARGET, context)["target_complete"] is True
            assert calls
        return {}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    backend = closer(tmp_path, current, apply=True, workers=True)
    args = SimpleNamespace(
        state=str(tmp_path / "state"),
        shadow=False,
        max_workers=1,
        coordinator_mode="resource_scoped",
        coordinator_contract_pin=None,
    )
    adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 330)


@pytest.mark.parametrize("origin", ["<!-- workflow-source:local_request -->\n", ""])
def test_no_linked_issue_retains_pr_authored_request_and_revision(tmp_path, monkeypatch, origin):
    document = _graphql_pr()
    document["closingIssuesReferences"]["nodes"] = []
    document["title"] = "Stop Gate restarts on label events"
    document["body"] = (
        origin
        + "Remove only `labeled` from `on.pull_request.types`. Preserve every other trigger.\n"
        + "Refs stranske/Workflows#3983, stranske/Workflows#4082."
    )
    github, reads = _lineage_snapshot(monkeypatch, document, {})
    current = github.snapshot(TARGET)
    source = current["sources"][0]
    assert source["target"] == TARGET and source["kind"] == "pr_request"
    assert source["body"] == document["body"]
    assert source["provenance"]["via"] == "github_pr_body"
    assert source["title"] == document["title"]
    assert source["provenance"]["url"] == "https://github.com/o/r/pull/7"
    assert source["provenance"]["updated_at"] == document["updatedAt"]
    assert (
        source["provenance"]["body_sha256"] == hashlib.sha256(document["body"].encode()).hexdigest()
    )
    assert current["source_inventory"] == {
        "status": "no_linked_issue",
        "complete": True,
        "linked_issue_targets": [],
        "request_target": TARGET,
    }
    assert reads == ["repos/o/r/issues/7", "graphql"]
    section = closer_evidence._source_section(current["sources"], tmp_path)
    retained = section["content"][0]
    assert retained["body"] == document["body"]
    assert retained["provenance"] == source["provenance"]
    assert retained["request_revision"] == source["request_revision"]
    assert Path(retained["body_retained"]["path"]).read_text() == document["body"]
    # Capture never rewrites free prose into invented acceptance criteria.
    assert retained["requirements"] == []
    assert not adapter.acceptance_ok(
        current,
        {
            "reviewer": "test",
            "head": HEAD,
            "source_digest": adapter.source_digest(current["sources"]),
        },
    )
    document["title"] = "Preserve every other trigger"
    renamed = github.snapshot(TARGET)
    assert renamed["sources"][0]["request_revision"] != source["request_revision"]
    document["updatedAt"] = "2026-10-21T00:00:00Z"
    changed = github.snapshot(TARGET)
    assert changed["sources"][0]["request_revision"] != source["request_revision"]
    assert changed["revision"] != current["revision"]


@pytest.mark.parametrize("location", ["pr", "linked_issue", "authored_issue", "assigned_issue"])
def test_omitted_request_body_is_not_an_empty_original(monkeypatch, location):
    document = _graphql_pr()
    issue = {"number": 2, "state": "open", "labels": []}
    if location == "pr":
        del document["body"]
    elif location == "linked_issue":
        del document["closingIssuesReferences"]["nodes"][0]["body"]
    else:
        document["closingIssuesReferences"]["nodes"] = []
        document["body"] = "<!-- meta:issue:2 -->"
    github, _ = _lineage_snapshot(monkeypatch, document, {"repos/o/r/issues/2": issue})
    if location == "assigned_issue":
        monkeypatch.setattr(github, "api", lambda *_a, **_k: issue)
    with pytest.raises(adapter.CoreInformationError, match="body_transport_omission"):
        github.snapshot(SOURCE if location == "assigned_issue" else TARGET)


def test_null_body_is_explicit_empty_content_not_transport_omission(monkeypatch):
    document = _graphql_pr()
    document["closingIssuesReferences"]["nodes"] = []
    document["body"] = None
    github, _ = _lineage_snapshot(monkeypatch, document, {})
    current = github.snapshot(TARGET)
    assert current["sources"][0]["body"] == ""
    assert current["source_inventory"]["status"] == "no_linked_issue"
    assert not adapter.acceptance_ok(current, {})


def test_wrong_pr_identity_refuses_before_request_capture(monkeypatch):
    document = _graphql_pr()
    document["number"] = 8
    github, _ = _lineage_snapshot(monkeypatch, document, {})
    with pytest.raises(adapter.CoreInformationError, match="identity_mismatch"):
        github.snapshot(TARGET)


def test_source_fetch_failure_never_becomes_no_linked_issue_and_can_be_retried(monkeypatch):
    document = _graphql_pr()
    document["closingIssuesReferences"]["nodes"] = []
    document["body"] = "<!-- meta:issue:2 -->"
    github, _ = _lineage_snapshot(monkeypatch, document, {})
    original = github.api
    failed = True

    def fetch(endpoint, **kwargs):
        if endpoint == "repos/o/r/issues/2":
            if failed:
                raise RuntimeError("GitHub outcome unknown: source read failed")
            return {"number": 2, "body": "original request", "state": "open", "labels": []}
        return original(endpoint, **kwargs)

    monkeypatch.setattr(github, "api", fetch)
    with pytest.raises(RuntimeError, match="source read failed"):
        github.snapshot(TARGET)
    failed = False
    current = github.snapshot(TARGET)
    assert current["source_inventory"]["status"] == "linked_issues"
    assert current["sources"][0]["body"] == "original request"


def test_pr_request_never_routes_to_issue_closure(tmp_path, monkeypatch):
    current = snapshot(state="MERGED", verifier={"verdict": "PASS"})
    current["sources"] = [
        adapter._request_source(
            TARGET,
            {
                "body": "## Tasks\n- Preserve behavior",
                "state": "MERGED",
                "labels": [],
            },
            kind="pr_request",
            via="github_pr_body",
        )
    ]
    monkeypatch.setattr(adapter, "acceptance_ok", lambda *_: True)
    assert closer(tmp_path, current).select(current)[0] == "complete"
    current["sources"].append({"target": SOURCE, "state": "open", "body": "issue"})
    assert closer(tmp_path, current).select(current)[0] == "close_source"


def test_request_revision_changes_invalidate_acceptance_without_body_changes(tmp_path):
    current = snapshot()
    issue = {
        "body": current["sources"][0]["body"],
        "title": "Preserve behavior",
        "state": "open",
        "labels": [],
    }
    current["sources"] = [adapter._request_source(SOURCE, issue, via="closingIssuesReferences")]
    decision = integrated(tmp_path, current)
    assert adapter.acceptance_ok(current, decision)
    issue["title"] = "Preserve behavior and prevent duplicate writes"
    current["sources"] = [adapter._request_source(SOURCE, issue, via="closingIssuesReferences")]
    assert not adapter.acceptance_ok(current, decision)
    assert adapter.source_digest(current["sources"]) != decision["source_digest"]


def _dead_owner_reservations(tmp_path):
    import closer_resources as resources

    path = tmp_path / "pipeline.json"
    path.write_text('{"operations":{"original":"unknown"}}')
    root = resources.scoped_root(path)
    for kind, filename, owner in [
        ("pr", "intent.json", "99999999:123:original"),
        ("issue", "legacy-intent.json", "legacy"),
    ]:
        key = kind + ":" + TARGET.lower()
        directory = resources._key_dir(root, key)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "lock").touch()
        resources._atomic_write(
            directory / filename,
            {
                "key": key,
                "target": TARGET.lower(),
                "owner": owner,
                "state": "unknown" if kind == "pr" else "legacy_executor",
                "at": 1,
            },
        )
    return path, root


def _terminal_observation(target):
    return {
        "snapshot": {
            "target": target,
            "kind": "pr",
            "state": "MERGED",
            "revision": "remote-r1",
            "head": "a" * 40,
            "merge": "b" * 40,
            "observed_at": time.time(),
        },
        "native_writer": {"terminal": True, "evidence": []},
    }


def test_dead_owner_reservations_become_drainable(tmp_path, monkeypatch):
    import closer_resources as resources

    path, root = _dead_owner_reservations(tmp_path)
    original_ledger = path.read_bytes()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*intent.json")}
    calls = []

    def observe(target):
        calls.append(target)
        return _terminal_observation(target)

    monkeypatch.setattr(resources, "_terminal_remote_observation", observe)
    report = adapter._expired_admission_diagnostics(path)
    assert report["blocking_reservations"] == report["drainable_reservations"] == 2
    assert report["deadlock"] is False
    assert all(x["drainable"] for x in report["reservation_recovery"])
    plan = resources.plan_terminal_reconciliation(path, target=TARGET)
    result = resources.apply_terminal_reconciliation(
        path, target=TARGET, expected_plan_sha256=plan["plan_sha256"]
    )
    assert len(calls) == 3  # diagnostics, plan, fresh apply; never a cached observation
    assert resources.unknown_reservations(root) == {}
    assert path.read_bytes() == original_ledger  # original UNKNOWN still refuses effect retry
    audit = json.loads(Path(result["audit_path"]).read_text())
    assert audit["observation"]["snapshot"]["merge"] == "b" * 40
    import hashlib

    for row, retired in zip(audit["advertisements"], result["retired"]):
        assert Path(retired).read_bytes() == before[Path(row["path"])]
        assert hashlib.sha256(Path(retired).read_bytes()).hexdigest() == row["sha256"]
    assert not result["acceptance_authority"]
    assert not result["global_stop_authority"]
    assert not result["effect_retry_authority"]


def test_live_owner_reservation_stays_blocking(tmp_path, monkeypatch):
    import os

    import closer_resources as resources

    path, root = _dead_owner_reservations(tmp_path)
    core = next(tmp_path.rglob("intent.json"))
    value = json.loads(core.read_text())
    value["owner"] = f"{os.getpid()}:123:original"
    resources._atomic_write(core, value)
    monkeypatch.setattr(
        resources,
        "_terminal_remote_observation",
        lambda _: pytest.fail("live owner must refuse before remote reads"),
    )
    before = resources.unknown_reservations(root)
    report = adapter._expired_admission_diagnostics(path)
    assert report["drainable_reservations"] == 0
    assert report["deadlock"] is True
    assert report["reservation_reason_counts"]["owner_still_alive"] == 2
    assert resources.unknown_reservations(root) == before


def test_dead_owner_drain_deliberate_break_without_terminal_path(tmp_path, monkeypatch):
    """AC deliberate-break: unbound legacy with eligible=False stays undrainable.

    Restoring reasons=['missing_causal_run_binding'] / eligible=False for unbound
    legacy (and recovery_path none for Core) makes the named dead-owner acceptance
    fail; reverting restores drainable==blocking. Encodes the issue's break→revert.
    """
    import closer_resources as resources

    path, _root = _dead_owner_reservations(tmp_path)
    monkeypatch.setattr(
        resources,
        "_terminal_remote_observation",
        lambda target: _terminal_observation(target),
    )
    # Break: refuse the terminal plan so diagnostics keep the historical latch.
    monkeypatch.setattr(
        resources,
        "plan_terminal_reconciliation",
        lambda *a, **k: {
            "eligible": False,
            "advertisements": [],
            "reasons": ["missing_causal_run_binding"],
        },
    )
    broken = adapter._expired_admission_diagnostics(path)
    assert broken["drainable_reservations"] == 0
    assert broken["deadlock"] is True
    assert broken["reservation_reason_counts"].get("missing_causal_run_binding")
    assert all(not row["drainable"] for row in broken["reservation_recovery"])
    # Revert the deliberate break and confirm the named acceptance shape returns.
    monkeypatch.undo()
    monkeypatch.setattr(
        resources,
        "_terminal_remote_observation",
        lambda target: _terminal_observation(target),
    )
    restored = adapter._expired_admission_diagnostics(path)
    assert restored["drainable_reservations"] == restored["blocking_reservations"] == 2
    assert restored["deadlock"] is False
    assert all(row["drainable"] for row in restored["reservation_recovery"])


def test_terminal_reconciliation_resumes_after_audit_write_crash(tmp_path, monkeypatch):
    """Crash after immutable audit, before first rename, must resume — not latch."""
    import closer_resources as resources

    path, root = _dead_owner_reservations(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*intent.json")}
    monkeypatch.setattr(
        resources, "_terminal_remote_observation", lambda target: _terminal_observation(target)
    )
    plan = resources.plan_terminal_reconciliation(path, target=TARGET)
    assert plan["eligible"] is True
    real_rename = resources.os.rename
    attempts = {"n": 0}

    def block_first_rename(src, dst):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("synthetic crash after audit before first rename")
        return real_rename(src, dst)

    monkeypatch.setattr(resources.os, "rename", block_first_rename)
    with pytest.raises(OSError, match="synthetic crash after audit"):
        resources.apply_terminal_reconciliation(
            path, target=TARGET, expected_plan_sha256=plan["plan_sha256"]
        )
    assert resources.unknown_reservations(root)
    audit = (
        resources._reservation_base(resources.scoped_root(path)).parent
        / "reconciliation-audit"
        / f"{plan['plan_sha256']}.json"
    )
    assert audit.is_file()
    # Resume with the same plan hash: identical audit (ignoring observed_at) continues.
    monkeypatch.setattr(resources.os, "rename", real_rename)
    result = resources.apply_terminal_reconciliation(
        path, target=TARGET, expected_plan_sha256=plan["plan_sha256"]
    )
    assert result["status"] == "retired"
    assert result["audit_path"] == str(audit)
    assert resources.unknown_reservations(root) == {}
    for row, retired in zip(json.loads(audit.read_text())["advertisements"], result["retired"]):
        assert Path(retired).read_bytes() == before[Path(row["path"])]


@pytest.mark.parametrize(
    "failure", ["remote", "native", "changed", "lock", "group", "malformed_owner", "alias"]
)
def test_terminal_reconciliation_retains_unknown_when_evidence_fails(
    tmp_path, monkeypatch, failure
):
    import fcntl
    import os

    import closer_resources as resources

    path, root = _dead_owner_reservations(tmp_path)
    before = resources.unknown_reservations(root)
    observe = _terminal_observation
    plan = resources.plan_terminal_reconciliation(path, target=TARGET, observe=observe)
    assert plan["eligible"]
    lock = None
    if failure == "remote":

        def observe(_):
            raise RuntimeError("remote unavailable")

    elif failure == "native":

        def observe(target):
            return {**_terminal_observation(target), "native_writer": {"terminal": False}}

    elif failure == "changed":

        def observe(target):
            value = _terminal_observation(target)
            value["snapshot"]["revision"] = "new remote revision"
            return value

    elif failure == "lock":
        lock = next(tmp_path.rglob("intent.json")).parent.joinpath("lock").open("r+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif failure == "group":
        monkeypatch.setattr(os, "killpg", lambda *_: None)
    elif failure == "malformed_owner":
        core = next(tmp_path.rglob("intent.json"))
        value = json.loads(core.read_text())
        value["owner"] = "unknown"
        resources._atomic_write(core, value)
    elif failure == "alias":
        core = next(tmp_path.rglob("intent.json"))
        saved = tmp_path / "saved-intent"
        saved.write_bytes(core.read_bytes())
        core.unlink()
        core.symlink_to(saved)
    try:
        with pytest.raises((resources.ResourceBlocked, RuntimeError, ValueError)):
            resources.apply_terminal_reconciliation(
                path, target=TARGET, expected_plan_sha256=plan["plan_sha256"], observe=observe
            )
        assert resources.unknown_reservations(root) == before
        assert not list(tmp_path.rglob("*.retired"))
    finally:
        if lock:
            lock.close()


def test_legacy_reservation_refuses_unbound_new_intent(tmp_path):
    import closer_resources as resources

    root = resources.scoped_root(tmp_path / "pipeline")
    with pytest.raises(resources.ResourceBlocked, match="requires_causal_binding"):
        with resources.legacy_reserve(
            root, ["pr:" + TARGET], target=TARGET, owner="legacy", deadline=time.time() + 2
        ):
            pytest.fail("unbound writer admitted")
    assert resources.unknown_reservations(root) == {}


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_terminal_reconciliation_uses_live_snapshot_and_native_gate(monkeypatch, kind):
    import closer_resources as resources
    import review_writer_gate

    reads = []
    native = []
    snap = _terminal_observation(TARGET)["snapshot"]
    if kind == "issue":
        snap = {
            **snap,
            "kind": "issue",
            "pr_target": "o/r#44",
            "delivery_links": {"complete": True, "status": "resolved", "candidates": ["o/r#44"]},
        }

    class GitHub:
        def __init__(self, *, deadline):
            assert deadline > time.time()

        def snapshot(self, target):
            reads.append(target)
            return snap

        def api(self, endpoint):
            assert "/timeline?" in endpoint
            return []

    monkeypatch.setattr(adapter, "GitHub", GitHub)

    def terminal(repo, number, call):
        native.append((repo, number, callable(call)))
        return {"terminal": True, "evidence": ["all-head proof"]}

    monkeypatch.setattr(review_writer_gate, "writer_terminality", terminal)
    result = resources._terminal_remote_observation(TARGET)
    assert reads == [TARGET]
    expected = (
        ("o/r", 44, True)
        if kind == "issue"
        else (TARGET.split("#")[0], int(TARGET.split("#")[1]), True)
    )
    assert native == [expected]
    assert result["native_writer"]["evidence"] == ["all-head proof"]


def test_terminal_reconciliation_incomplete_remote_inventory_cannot_drain(tmp_path, monkeypatch):
    import closer_resources as resources
    import review_writer_gate

    path, root = _dead_owner_reservations(tmp_path)
    before = resources.unknown_reservations(root)
    snap = {
        **_terminal_observation(TARGET)["snapshot"],
        "kind": "issue",
        "delivery_links": {"complete": False, "status": "failed"},
    }
    monkeypatch.setattr(adapter, "GitHub", lambda **_: SimpleNamespace(snapshot=lambda _: snap))
    monkeypatch.setattr(
        review_writer_gate, "writer_terminality", lambda *_: pytest.fail("incomplete inventory")
    )
    plan = resources.plan_terminal_reconciliation(path, target=TARGET)
    assert plan["eligible"] is False
    assert plan["reasons"] == ["delivery_inventory_incomplete"]
    assert resources.unknown_reservations(root) == before


@pytest.mark.parametrize("mutation", ["intent", "owner", "malformed", "native"])
def test_terminal_observation_cannot_outlive_local_owner_proof(tmp_path, mutation):
    import os

    import closer_resources as resources

    path, root = _dead_owner_reservations(tmp_path)
    before = resources.unknown_reservations(root)

    def observe(target):
        if mutation == "malformed":
            return {"snapshot": [], "native_writer": []}
        if mutation == "native":
            return {"snapshot": _terminal_observation(target)["snapshot"], "native_writer": None}
        intent = next(tmp_path.rglob("intent.json"))
        value = json.loads(intent.read_text())
        value["owner" if mutation == "owner" else "at"] = (
            f"{os.getpid()}:123:revived" if mutation == "owner" else 2
        )
        intent.write_text(json.dumps(value))
        return _terminal_observation(target)

    report = resources.plan_terminal_reconciliation(path, target=TARGET, observe=observe)
    assert not report["eligible"]
    assert resources.unknown_reservations(root).keys() == before.keys()
    assert not list(tmp_path.rglob("*.retired"))


def test_terminal_source_inventory_checks_unmerged_referenced_pr(monkeypatch):
    import closer_resources as resources
    import review_writer_gate

    snap = {
        **_terminal_observation(TARGET)["snapshot"],
        "kind": "issue",
        "delivery_links": {"complete": True, "candidates": ["o/r#40"]},
    }
    queried = []

    def api(endpoint):
        assert "/timeline?" in endpoint
        return [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "url": "https://api.github.com/repos/o/r/issues/44",
                        "pull_request": {},
                    }
                },
            }
        ]

    monkeypatch.setattr(
        adapter, "GitHub", lambda **_: SimpleNamespace(snapshot=lambda _: snap, api=api)
    )

    def terminal(repo, number, call):
        queried.append(number)
        return {"terminal": number != 44, "reason": "pending unmerged writer", "evidence": []}

    monkeypatch.setattr(review_writer_gate, "writer_terminality", terminal)
    with pytest.raises(resources.ResourceBlocked, match="pending unmerged writer"):
        resources._terminal_remote_observation(TARGET)
    assert queried == [40, 44]


@pytest.mark.parametrize("malformed", [False, True])
def test_terminal_native_receipt_cannot_hide_other_provider_writer(monkeypatch, malformed):
    import closer_resources as resources
    import review_writer_gate

    snap = _terminal_observation(TARGET)["snapshot"]

    def api(endpoint):
        assert "/jobs?" in endpoint
        return {
            "jobs": (
                [None]
                if malformed
                else [
                    {
                        "name": "Keepalive / Run Claude",
                        "status": "in_progress",
                        "steps": [
                            {"name": "Run Claude", "status": "completed"},
                            {"name": "Commit and push changes", "status": "in_progress"},
                        ],
                    }
                ]
            )
        }

    monkeypatch.setattr(
        adapter, "GitHub", lambda **_: SimpleNamespace(snapshot=lambda _: snap, api=api)
    )

    def terminal(repo, number, call):
        # Even a matching completed Codex receipt must inspect the attempt's
        # other admitted implementation provider before retirement is possible.
        call("repos/o/r/actions/runs/1/attempts/1/jobs?per_page=100&page=1")
        return {"terminal": True, "evidence": ["completed Codex"]}

    monkeypatch.setattr(review_writer_gate, "writer_terminality", terminal)
    with pytest.raises(
        resources.ResourceBlocked, match="implementation_.*(malformed|not_terminal)"
    ):
        resources._terminal_remote_observation(TARGET)
