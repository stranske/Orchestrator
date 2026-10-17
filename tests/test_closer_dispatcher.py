"""Offline runtime-adapter contracts; never contact providers or live GitHub/state."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from types import SimpleNamespace

import pytest

import claims
import closer_dispatcher as adapter

TARGET = "o/r#7"
SOURCE = "o/r#2"
HEAD = "a" * 40
MERGE = "b" * 40


@pytest.fixture(autouse=True)
def no_live_state_or_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "private-handoff"))
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


def snapshot(**overrides):
    result = {
        "target": TARGET,
        "kind": "pr",
        "state": "OPEN",
        "head": HEAD,
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


@pytest.mark.parametrize("decision", [{"ready": True}, {"reviewer": "luna", "ready": True}, {}])
def test_green_ci_or_model_ready_is_not_acceptance(tmp_path, decision):
    current = snapshot()
    backend = closer(tmp_path, current, dispositions={TARGET: decision})
    action, _, eligible, _ = backend.select(current)
    assert action == "collect_evidence" and not eligible


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


def test_unresolved_outdated_thread_needs_review_before_merge_selection(tmp_path):
    current = snapshot(
        active_threads=1, threads=[{"id": "thread", "isResolved": False, "isOutdated": True}]
    )
    backend = closer(tmp_path, current, dispositions={TARGET: integrated(tmp_path, current)})
    assert backend.select(current)[0] == "review"


def test_existing_active_verifier_is_reused_without_duplicate_request(tmp_path):
    current = snapshot(state="MERGED", merge=MERGE)
    backend = closer(tmp_path, current, apply=True)
    endpoint = (
        "repos/o/r/actions/workflows/agents-verifier.yml/runs?per_page=100&status=in_progress"
    )
    backend.gh.responses[("GET", endpoint)] = {
        "total_count": 1,
        "workflow_runs": [{"id": 99, "status": "in_progress", "pull_requests": [{"number": 7}]}],
    }
    assert backend.initiate_verify(TARGET, current)["status"] == "deferred"
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


def test_luna_result_is_advisory_and_cannot_supply_completion_disposition(tmp_path, monkeypatch):
    current = snapshot()
    calls = []
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

    backend = closer(
        tmp_path, current, apply=True, dispositions={TARGET: integrated(tmp_path, current)}
    )
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
            "nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"state": "SUCCESS"}}}]
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


def test_actual_snapshot_counts_outdated_unresolved_threads(monkeypatch):
    document = _graphql_pr()
    document["reviewThreads"]["nodes"] = [{"id": "old", "isResolved": False, "isOutdated": True}]
    assert _snapshot_from_document(monkeypatch, document)["active_threads"] == 1


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

    class Process:
        pid = 43210
        returncode = 0

        def __init__(self):
            self.timeouts = []
            self.exited = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.exited = True

        def communicate(self, *, timeout):
            self.timeouts.append(timeout)
            if len(self.timeouts) == 1 and globals_timeout:
                raise adapter.subprocess.TimeoutExpired(["owned"], timeout)
            return "retained stdout", "retained stderr"

    globals_timeout = timeout
    process = Process()

    def popen(command, **kwargs):
        created.append((command, kwargs))
        return process

    monkeypatch.setattr(adapter.subprocess, "Popen", popen)
    monkeypatch.setattr(adapter.os, "killpg", lambda pid, signal: killed.append((pid, signal)))
    if timeout:
        with pytest.raises(adapter.subprocess.TimeoutExpired):
            adapter.bounded_run(["owned"], timeout=3)
        assert killed == [(process.pid, adapter.signal.SIGKILL)]
        assert process.timeouts == [3, 5]
    else:
        result = adapter.bounded_run(["owned"], timeout=3)
        assert result.returncode == 0 and result.stdout == "retained stdout"
        assert killed == []
    assert process.exited
    assert created[0][1]["start_new_session"] is True
    assert created[0][1]["stdout"] == adapter.subprocess.PIPE


def _private_worker(tmp_path, monkeypatch, *, decision=None, result=None, real_admission=False):
    calls = []
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
        snapshot(),
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
    assert kwargs["isolate"] is trusted_plan
    assert kwargs["mode"] == ("mid" if trusted_plan else "assess")
    assert backend.dispositions.get(TARGET) == decision


@pytest.mark.parametrize(
    "failure", ["quota exceeded", "unsupported model", "authentication failed"]
)
def test_generic_error_does_not_hide_provider_failure_in_output(tmp_path, monkeypatch, failure):
    backend, calls = _private_worker(
        tmp_path, monkeypatch, result={"exit": 1, "error": "worker failed", "output": failure}
    )
    assert (
        backend.worker(TARGET, "collect_evidence", snapshot(), operation("collect_evidence"))[
            "status"
        ]
        == "failed"
    )
    assert backend.workers_unavailable
    action, _, eligible, _ = backend.select(snapshot(target="o/r#8"))
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
    handoff.mkdir(parents=True)
    capacity = handoff / "capacity.json"
    capacity.write_text(
        json.dumps({"generated_at": time.time(), "agents": {"codex": {"state": "ok"}}})
    )
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
    for status, expected in [
        ("in_progress", "wait"),
        ("completed", "collect_evidence"),
        (None, "collect_evidence"),
    ]:
        current = snapshot(
            state="MERGED",
            merge=MERGE,
            labels=["verify:compare"],
            verifier_runs=([{"id": 99, "status": status}] if status else []),
        )
        backend = closer(tmp_path, current)
        action, _, eligible, _ = backend.select(current)
        assert action == expected and not eligible
        assert backend.gh.calls == []


def test_pending_advisory_survives_restart_without_another_luna_call(tmp_path, monkeypatch):
    backend, calls = _private_worker(tmp_path, monkeypatch)
    current = snapshot()
    op = operation("collect_evidence")
    result = backend.worker(TARGET, "collect_evidence", current, op)
    assert len(calls) == 1 and backend.observe(TARGET)["requires_coordinator"]
    backend.state_path.write_text(
        json.dumps(
            {
                "items": {
                    TARGET: {
                        "operation": {
                            **op,
                            "revision": current["revision"],
                            "status": "completed",
                            "result": result,
                        }
                    }
                }
            }
        )
    )
    restarted, new_calls = _private_worker(tmp_path, monkeypatch)
    observed = restarted.observe(TARGET)
    assert observed["requires_coordinator"]
    assert restarted.integration_pending(TARGET, current) == op["id"]
    assert new_calls == [] and len(calls) == 1


def test_integration_ack_is_bound_but_never_supplies_acceptance_or_merge(tmp_path, monkeypatch):
    backend, calls = _private_worker(tmp_path, monkeypatch)
    current = snapshot()
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
    assert backend.select(current)[0] != "merge"
    assert not adapter.acceptance_ok(current, ack)
    assert len(calls) == 1
    observed = backend.observe(TARGET)
    assert observed["operation_binding"] == original_binding
    backend.dispositions[TARGET] = {**integrated(tmp_path, current), **ack}
    assert backend.select(current)[0] == "merge"
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
    assert closer(tmp_path, current).select(current)[0] == "collect_evidence"
    attributed = {"id": 11, "status": "in_progress", "pull_requests": [{"number": 7}]}
    current = _snapshot_from_document(monkeypatch, document, [unrelated, manual, attributed])
    assert [run["id"] for run in current["verifier_runs"]] == [11]
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
        {"target": SOURCE, "body": issue["body"], "state": "open", "labels": ["needs-review"]}
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
    assert github.snapshot(TARGET)["sources"] == []
    assert not any(path.endswith(("/99", "/98", "/97")) for path in reads)
    document["body"] = "<!-- meta:issue:2 -->\n" + document["body"]
    for invalid in ({}, {"number": 2, "pull_request": {}, "body": "A PR is not a source issue"}):
        github, _ = _lineage_snapshot(monkeypatch, document, {"repos/o/r/issues/2": invalid})
        with pytest.raises(RuntimeError, match="not an issue"):
            github.snapshot(TARGET)
