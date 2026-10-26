"""Offline producer and authenticated terminal correlation; no paid calls."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_closer_runtime_binding import bundle as bundle
from test_closer_runtime_binding import policy as policy

import closer_dispatcher as dispatcher
import closer_frontier_policy as frontier_policy
import closer_queue as queue
import closer_verifier_followup as followup
import verification_events
import verifier_evidence

TARGET = "offline/correlation#1"
BASIS = "e" * 64


def terminal_world(tmp_path, target=TARGET):
    repo, number = target.split("#")
    event = {
        "schema": verification_events.SCHEMA,
        "repo": repo,
        "pr": number,
        "head_sha": "a" * 40,
        "evaluated_sha": "b" * 40,
        "request_sha256": "c" * 64,
        "material_sha256": "d" * 64,
    }
    event["event_id"] = verification_events.event_id(event)
    binding = {"pr_target": target, "head": "a" * 40, "merge": "b" * 40}
    frontier = frontier_policy.make_frontier(
        [target],
        owner="offline-owner",
        frontier_id="original-post",
        bindings={
            target: {
                "revision": "original-revision",
                "action_id": frontier_policy.action_identity(
                    target, "original-revision", "post_merge_verify", binding
                ),
            }
        },
    )
    operation = {
        "id": "original-operation",
        "status": "completed",
        "action": "post_merge_verify",
        "revision": "original-revision",
        "finite_frontier": frontier,
        "binding": binding,
        "completed_at": 900,
        "result": {
            "verification_request_basis": BASIS,
            "verification_post_accepted": {
                "mutation_kind": "workflow_dispatch",
                "request_basis": BASIS,
                "correlation_input": BASIS,
            },
        },
    }
    native = {
        "pr_target": target,
        "head": binding["head"],
        "merge": binding["merge"],
        "queue_operation_id": operation["id"],
        "verification_mutation": copy.deepcopy(operation["result"]["verification_post_accepted"]),
    }
    operation["result"]["core_request_correlation"] = {
        "schema": "closer-core-request-correlation/v1",
        "operation_id": operation["id"],
        "core_operation_key": "offline-original-core",
        "core_target_fence_key": "offline-exact-fence",
        "core_package_digest": "a" * 64,
        "native_binding": native,
        "native_binding_sha256": followup.native_binding_digest(native),
    }
    path = tmp_path / "actions.json"
    path.write_text(
        json.dumps(
            {
                "schema": queue.SCHEMA,
                "items": {},
                "round": {},
                "frontiers": {
                    frontier["frontier_id"]: {
                        "policy": frontier,
                        "round": {},
                        "items": {target: {"operation": operation}},
                    }
                },
            }
        )
    )
    accepted = {
        "operation_frontier": frontier["frontier_id"],
        "operation_id": operation["id"],
        "event_id": BASIS,
        "head": binding["head"],
        "merge": binding["merge"],
    }
    marker = {
        "schema": verifier_evidence.MARKER,
        "repo": repo,
        "pr": number,
        "head_sha": binding["head"],
        "evaluated_sha": binding["merge"],
        "verification_event": event,
        "run_id": "50001",
        "run_attempt": "1",
        "provider_verdicts": ["PASS"],
        "ci_failed": False,
        "verdict": "PASS",
        "assessment_identity": {
            "canonical": True,
            "mode": "compare",
            "provider": "",
            "model": "",
            "model2": "",
        },
        "verification_request_basis": BASIS,
    }
    observation = {
        "target": target,
        "head": binding["head"],
        "merge": binding["merge"],
        "state": "MERGED",
        "eligible": True,
        "action": "close_source",
        "revision": "terminal-revision",
        "operation_binding": binding,
        "observed_at": 1000,
        "verifier": marker,
        "verifier_lifecycle": copy.deepcopy(marker),
    }
    observation.update(
        kind="pr",
        body="original-request",
        title="Original",
        sources=[],
        source_inventory={"complete": True},
    )
    native["verification_source_requirements"] = followup.source_requirements(observation)
    operation["result"]["core_request_correlation"]["native_binding_sha256"] = (
        followup.native_binding_digest(native)
    )
    # Original request proof is retained before the terminal is observed.
    original = json.loads(path.read_text())
    original["frontiers"][frontier["frontier_id"]]["items"][target]["operation"] = operation
    path.write_text(json.dumps(original))
    return path, target, accepted, observation


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-basis",
        "missing-basis",
        "pending-lifecycle",
        "old-observation",
        "cached",
        "different-head",
    ],
)
def test_terminal_followup_refuses_missing_stale_or_unrelated_evidence(tmp_path, mutation):
    path, target, accepted, observation = terminal_world(tmp_path)
    if mutation == "wrong-basis":
        observation["verifier"]["verification_request_basis"] = "f" * 64
    elif mutation == "missing-basis":
        observation["verifier"].pop("verification_request_basis")
    elif mutation == "pending-lifecycle":
        observation["verifier_lifecycle"] = {
            "schema": verifier_evidence.STATE_MARKER,
            "status": "pending",
        }
    elif mutation == "old-observation":
        observation["observed_at"] = 1
    elif mutation == "cached":
        observation["_local_cached"] = True
    else:
        observation["head"] = "f" * 40
    assert followup.terminal_proof(path, target, accepted, observation, now=1000) is None


@pytest.mark.parametrize("bad", [None, "", "E" * 64, "e" * 63, 123])
def test_optional_marker_basis_is_strict_and_roundtrips_trusted_parser(tmp_path, bad):
    _path, target, _accepted, observation = terminal_world(tmp_path)
    marker = observation["verifier"]
    repo, number = target.split("#")

    def parsed(record):
        return verifier_evidence.decision_from_pr(
            repo,
            {
                "number": int(number),
                "headRefOid": "a" * 40,
                "mergeCommit": {"oid": "b" * 40},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "github-actions[bot]"},
                            "url": f"https://github.com/{repo}/pull/{number}#issuecomment-1",
                            "body": "<!-- verifier-corpus-decision/v1 "
                            + json.dumps(record)
                            + " -->",
                        }
                    ]
                },
            },
        )

    assert parsed(marker)["verification_request_basis"] == BASIS
    legacy = copy.deepcopy(marker)
    legacy.pop("verification_request_basis")
    assert parsed(legacy)["verdict"] == "PASS"
    marker["verification_request_basis"] = bad
    invalid = parsed(marker)
    assert invalid["verdict"] == "PASS"
    assert "verification_request_basis" not in invalid
    assert invalid["_request_correlation_unusable"] is True


def test_initial_and_existing_label_dispatch_send_correlation_without_route_fallback(
    tmp_path, monkeypatch
):
    calls = []
    gh = SimpleNamespace(
        api=lambda endpoint, **kwargs: calls.append((endpoint, kwargs))
        or {"default_branch": "main"}
    )
    closer = dispatcher.Closer(gh, state_path=tmp_path / "queue", apply=True)
    snapshot = {"target": TARGET, "head": "a" * 40, "merge": "b" * 40, "labels": ["verify:compare"]}
    monkeypatch.setattr(closer, "_verification_preflight", lambda *a: None)
    prepared = closer.prepare_verify(TARGET, snapshot)
    assert prepared["fields"]["inputs[verification_request_basis]"] == prepared["request_basis"]
    result = closer.submit_verify(TARGET, snapshot, prepared)
    assert calls[-1][1]["fields"]["inputs[verification_request_basis]"] == prepared["request_basis"]
    assert result["verification_post_accepted"]["correlation_input"] == prepared["request_basis"]
    snapshot["labels"] = []
    prepared = closer.prepare_verify(TARGET, snapshot)
    assert prepared["mutation_kind"] == "workflow_dispatch"
    result = closer.submit_verify(TARGET, snapshot, prepared)
    assert result["verification_post_accepted"]["correlation_input"] == prepared["request_basis"]
    legacy = {
        "mutation_kind": "label",
        "endpoint": "original-label",
        "fields": {"labels[]": "verify:compare"},
        "request_basis": closer.verification_request_basis(snapshot),
    }
    result = closer.submit_verify(TARGET, snapshot, legacy)
    assert "correlation_input" not in result["verification_post_accepted"]


def test_acknowledgment_alone_cannot_retrofit_legacy_core_correlation(tmp_path):
    path, target, accepted, observation = terminal_world(tmp_path)
    document = json.loads(path.read_text())
    result = document["frontiers"][accepted["operation_frontier"]]["items"][target]["operation"][
        "result"
    ]
    result.pop("core_request_correlation")
    path.write_text(json.dumps(document))
    assert followup.terminal_proof(path, target, accepted, observation, now=1000) is None


@pytest.mark.parametrize("correlated_intent", [True, False])
def test_actual_core_intent_required_for_receipt_and_unknown_never_resubmits(
    tmp_path, monkeypatch, bundle, policy, correlated_intent
):
    import time

    from test_closer_runtime_binding import Contents

    import closer_runtime_binding as runtime

    monkeypatch.setattr(bundle.runtime, "fetch_live_pr_labels", lambda *_: [])
    contents = Contents()

    def transport(method, path, body):
        if method == "GET" and path == "/repos/offline/correlation/pulls/1":
            return {"head": {"sha": "a" * 40}, "merged": True, "merge_commit_sha": "b" * 40}
        return contents(method, path, body)

    binding = runtime.RuntimeBinding(bundle, policy, transport=transport)
    snapshot = {
        "head": "a" * 40,
        "merge": "b" * 40,
        "pr_target": TARGET,
        "criteria": "required-source",
        "review_revision": "review-1",
        "revision": "original-revision",
        "target": TARGET,
        "kind": "pr",
        "state": "MERGED",
        "body": "original-request",
        "title": "Original",
        "sources": [],
        "source_inventory": {"complete": True},
    }
    mutation = {
        "mutation_kind": "workflow_dispatch",
        "request_basis": BASIS,
        "fields": {"inputs[verification_request_basis]": BASIS} if correlated_intent else {},
    }
    calls = []

    def actual_post():
        calls.append("POST")
        return {
            "status": "completed",
            "verification_request_basis": BASIS,
            "verification_post_accepted": {
                "mutation_kind": "workflow_dispatch",
                "request_basis": BASIS,
                "correlation_input": BASIS,
            },
        }

    operation = {
        "id": "original-operation",
        "owner": "offline-original-owner",
        "deadline": time.time() + 60,
    }
    result = binding.execute(
        TARGET,
        "post_merge_verify",
        snapshot,
        operation,
        actual_post,
        verification_mutation=mutation,
    )
    if correlated_intent:
        assert result["status"] == "completed", result
        proof = result["core_request_correlation"]
        assert proof["native_binding"]["verification_mutation"]["correlation_input"] == BASIS
        assert proof["operation_id"] == operation["id"]
        stored = binding.adapters["offline/correlation"].store.read(proof["core_target_fence_key"])
        assert proof["native_binding_sha256"] == stored["guard_versions"]["native_binding"]
        path, target, accepted, observation = terminal_world(tmp_path)
        document = json.loads(path.read_text())
        document["frontiers"][accepted["operation_frontier"]]["items"][target]["operation"][
            "result"
        ] = result
        path.write_text(json.dumps(document))
        assert followup.terminal_proof(path, target, accepted, observation, now=1000) is not None
    else:
        assert result["status"] == "unknown", result
        assert "core_request_correlation" not in result
    # Recovery/duplicate execution uses the exact original shared fence. Neither
    # an unknown outcome nor a completed request may issue another POST.
    again = binding.execute(
        TARGET,
        "post_merge_verify",
        snapshot,
        operation,
        lambda: pytest.fail("accepted/unknown request cannot resubmit"),
        verification_mutation=mutation,
    )
    assert calls == ["POST"]
    assert "core_request_correlation" not in again


@pytest.mark.parametrize(
    "corrupt",
    [
        None,
        "basis",
        "author",
        "url",
        "result-url",
        "digest",
        "event",
        "assessment",
        "attempt",
        "pending",
        "conflict",
        "late-unrelated",
        "duplicate-identical",
    ],
)
def test_authenticated_duplicate_alias_reuses_only_selected_terminal_without_new_request(
    tmp_path, corrupt
):
    path, target, accepted, observation = terminal_world(tmp_path)
    marker = observation["verifier"]
    marker.pop("verification_request_basis")  # Existing result predates this request.
    marker["assessment_identity"] = {
        "mode": "compare",
        "provider": "",
        "model": "",
        "model2": "",
        "canonical": True,
    }
    marker["producer_detail"] = "retained Unicode ∆ evidence"  # Digest is FULL original object.
    repo, number = target.split("#")
    terminal_url = f"https://github.com/{repo}/pull/{number}#issuecomment-100"
    alias_url = f"https://github.com/{repo}/pull/{number}#issuecomment-101"
    if corrupt in {"late-unrelated", "duplicate-identical"}:
        terminal_url = f"https://github.com/{repo}/pull/{number}#issuecomment-8"
        alias_url = f"https://github.com/{repo}/pull/{number}#issuecomment-9"
    alias = {
        "schema": "verifier-request-correlation/v1",
        "repo": repo,
        "pr": number,
        "head_sha": marker["head_sha"],
        "evaluated_sha": marker["evaluated_sha"],
        "verification_request_basis": BASIS,
        "verification_event": marker["verification_event"],
        "assessment_identity": marker["assessment_identity"],
        "result_run_id": marker["run_id"],
        "result_run_attempt": marker["run_attempt"],
        "result_comment_url": terminal_url,
        "result_decision_sha256": verification_events.content_hash(marker),
        "correlation_run_id": "50002",
        "correlation_run_attempt": "1",
    }
    author = "github-actions[bot]"
    if corrupt == "basis":
        alias["verification_request_basis"] = "f" * 64
    elif corrupt == "author":
        author = "untrusted-user"
    elif corrupt == "url":
        alias_url = f"https://github.com/{repo}/pull/999#issuecomment-101"
    elif corrupt == "result-url":
        alias["result_comment_url"] = terminal_url + "9"
    elif corrupt == "digest":
        alias["result_decision_sha256"] = "f" * 64
    elif corrupt == "event":
        alias["verification_event"] = {**marker["verification_event"], "event_id": "f" * 64}
    elif corrupt == "assessment":
        alias["assessment_identity"] = {**marker["assessment_identity"], "model": "different"}
    elif corrupt == "attempt":
        alias["result_run_attempt"] = "2"
    comments = [
        {
            "author": {"login": "github-actions[bot]"},
            "url": terminal_url,
            "body": "<!-- verifier-corpus-decision/v1 " + json.dumps(marker) + " -->",
        },
        {
            "author": {"login": author},
            "url": alias_url,
            "body": "<!-- verifier-request-correlation/v1 " + json.dumps(alias) + " -->",
        },
    ]
    if corrupt == "pending":
        state = {
            **marker,
            "schema": verifier_evidence.STATE_MARKER,
            "status": "pending",
            "run_attempt": "2",
        }
        state.pop("verdict")
        comments.append(
            {
                "author": {"login": "github-actions[bot]"},
                "url": terminal_url + "2",
                "body": "<!-- verifier-event-state/v1 " + json.dumps(state) + " -->",
            }
        )
    elif corrupt == "conflict":
        other = {**marker, "verdict": "NON_PASS", "provider_verdicts": ["FAIL"]}
        comments.append(
            {
                "author": {"login": "github-actions[bot]"},
                "url": terminal_url + "2",
                "body": "<!-- verifier-corpus-decision/v1 " + json.dumps(other) + " -->",
            }
        )
    pr = {
        "number": int(number),
        "headRefOid": marker["head_sha"],
        "mergeCommit": {"oid": marker["evaluated_sha"]},
        "comments": {"nodes": comments},
    }
    parsed = verifier_evidence.decision_from_pr(repo, pr)
    lifecycle = verifier_evidence.decision_from_pr(repo, pr, include_pending=True)
    observation.update(verifier=parsed, verifier_lifecycle=lifecycle)
    before = path.read_bytes()
    first = followup.terminal_proof(path, target, accepted, observation, now=1000)
    again = followup.terminal_proof(path, target, accepted, observation, now=1000)
    assert path.read_bytes() == before  # This path has no submission or mutation API.
    assert first == again
    assert (first is not None) == (corrupt in {None, "late-unrelated", "duplicate-identical"})
    if corrupt in {"late-unrelated", "duplicate-identical"}:
        additional = copy.deepcopy(alias)
        if corrupt == "late-unrelated":
            additional["verification_request_basis"] = "f" * 64
            additional["correlation_run_id"] = "50003"
        comments.append(
            {
                "author": {"login": "github-actions[bot]"},
                "url": f"https://github.com/{repo}/pull/{number}#issuecomment-10",
                "body": "<!-- verifier-request-correlation/v1 " + json.dumps(additional) + " -->",
            }
        )
        observation.update(
            verifier=verifier_evidence.decision_from_pr(repo, pr),
            verifier_lifecycle=verifier_evidence.decision_from_pr(repo, pr, include_pending=True),
        )
        assert followup.terminal_proof(path, target, accepted, observation, now=1000) == first
    if first:
        assert first["run_id"] == "50001"
        assert first["correlation"]["kind"] == "authenticated_alias"
        assert parsed["verdict"] == "PASS" and "verification_request_basis" not in parsed


def _terminal_marker(target, head, merge, basis):
    repo, number = target.split("#")
    event = {
        "schema": verification_events.SCHEMA,
        "repo": repo,
        "pr": number,
        "head_sha": head,
        "evaluated_sha": merge,
        "request_sha256": "c" * 64,
        "material_sha256": "d" * 64,
    }
    event["event_id"] = verification_events.event_id(event)
    return {
        "schema": verifier_evidence.MARKER,
        "repo": repo,
        "pr": number,
        "head_sha": head,
        "evaluated_sha": merge,
        "verification_event": event,
        "run_id": "50001",
        "run_attempt": "1",
        "provider_verdicts": ["PASS"],
        "ci_failed": False,
        "verdict": "PASS",
        "assessment_identity": {
            "canonical": True,
            "mode": "compare",
            "provider": "",
            "model": "",
            "model2": "",
        },
        "verification_request_basis": basis,
    }


def _pending_campaign(tmp_path, monkeypatch, bundle, policy):
    from test_closer_pipeline_frontier import _accepted_request_round
    from test_closer_runtime_binding import Contents

    import closer_campaign as campaigns
    import closer_hold_followup as holds
    import closer_resources as resources
    import closer_runtime_binding as runtime

    target, basis = "acme/repo#42", "c" * 64
    monkeypatch.setattr(bundle.runtime, "fetch_live_pr_labels", lambda *_: [])
    contents = Contents()

    def transport(method, path, body):
        if method == "GET" and path == "/repos/acme/repo/pulls/42":
            return {"head": {"sha": "a" * 40}, "merged": True, "merge_commit_sha": "b" * 40}
        return contents(method, path, body)

    binding = runtime.RuntimeBinding(bundle, policy, transport=transport)
    calls = []

    def actual_effect(name, action, snapshot, operation):
        assert name == target and action == "post_merge_verify"
        return binding.execute(
            target,
            action,
            {
                "head": "a" * 40,
                "merge": "b" * 40,
                "pr_target": target,
                "criteria": "required-source",
                "review_revision": "review-1",
                "revision": "rev-1",
                "target": target,
                "kind": "pr",
                "state": "MERGED",
                "body": "original-request",
                "title": "Original",
                "sources": [],
                "source_inventory": {"complete": True},
            },
            operation,
            lambda: calls.append("POST")
            or {
                "status": "completed",
                "verification_request_basis": basis,
                "verification_post_accepted": {
                    "mutation_kind": "workflow_dispatch",
                    "request_basis": basis,
                    "correlation_input": basis,
                },
            },
            verification_mutation={
                "mutation_kind": "workflow_dispatch",
                "request_basis": basis,
                "fields": {"inputs[verification_request_basis]": basis},
            },
        )

    state_path = (
        resources.shard_dir(resources.scoped_root(tmp_path / "queue.pipeline.json"), target)
        / "state.json"
    )
    state_path.parent.mkdir(parents=True)
    path, target, frontier, posts, retained, result = _accepted_request_round(
        tmp_path, pipeline_path=state_path, effect=actual_effect
    )
    assert result["targets"][target]["status"] == "held" and calls == posts == ["POST"], result
    from pathlib import Path

    (path.parent / "lease.json").write_text(
        json.dumps(
            {
                "target": target,
                "owner": "original-terminal-native",
                "epoch": 1,
                "finite_frontier": frontier,
            }
        )
    )
    action_path = Path(retained["path"])
    operation = queue._read_state(action_path)["frontiers"][frontier["frontier_id"]]["items"][
        target
    ]["operation"]
    assert operation["result"]["core_request_correlation"]
    manifest_path = tmp_path / "manifest.json"
    manifest = {"package_digest": "a" * 64}
    manifest_path.write_text(json.dumps(manifest))
    canonical = tmp_path / "assignment.json"
    canonical.write_text(json.dumps({"targets": [{"target": target}]}))
    obj = campaigns.Campaign(tmp_path / "consumer.campaign.json")
    obj.begin(
        "real-core-terminal-followup",
        [target],
        owner=frontier["owner"],
        release_pin=manifest["package_digest"],
        worker_config={
            "finite_frontier": frontier,
            "dispatch_state": str(tmp_path / "queue"),
            "runtime_manifest": str(manifest_path),
            "assignment": str(canonical),
            "granularity": "round",
            "hold_followup_policy": {"attempt_budget": 1},
        },
    )
    obj.update(
        lambda doc: doc.update(
            workers=[
                {
                    "worker_id": "original-known-native",
                    "target": campaigns.ROUND_UNIT,
                    "state": "terminal",
                    "outcome": "scoped_continuation",
                }
            ],
            targets={campaigns.ROUND_UNIT: "reconcile"},
        )
    )
    marker = _terminal_marker(target, "a" * 40, "b" * 40, basis)
    observation = {
        "target": target,
        "head": "a" * 40,
        "merge": "b" * 40,
        "state": "MERGED",
        "revision": "terminal-revision",
        "action": "close_source",
        "eligible": True,
        "operation_binding": {
            "pr_target": target,
            "source_targets": [],
            "head": "a" * 40,
            "merge": "b" * 40,
        },
        "verifier": marker,
        "verifier_lifecycle": copy.deepcopy(marker),
    }
    observation.update(
        kind="pr",
        body="original-request",
        title="Original",
        sources=[],
        source_inventory={"complete": True},
    )
    monkeypatch.setattr(
        holds, "_backend", lambda *a: SimpleNamespace(observe=lambda *_: copy.deepcopy(observation))
    )
    # The host native admission/proof boundary has independent real-hook tests.
    # Keep actual Core, queue proof, consumer/journal, and successor code here.
    monkeypatch.setattr(
        holds,
        "_native_terminal_boundary",
        lambda *a: {"synthetic_native_boundary": "original-terminal"},
    )
    monkeypatch.setattr(holds, "_validate_terminal_boundary", lambda *a: None)
    return obj, path, action_path, canonical, frontier, observation, manifest, manifest_path, calls


def _terminal_successor_case(tmp_path, monkeypatch, bundle, policy):
    import time

    import closer_stage_continuation as stages

    _obj, path, action_path, canonical, frontier, observation, _manifest, _pin, posts = (
        _pending_campaign(tmp_path, monkeypatch, bundle, policy)
    )

    def create():
        observation["observed_at"] = time.time()
        return stages.create_successor(
            path,
            action_path,
            predecessor=frontier,
            target=observation["target"],
            observation=observation,
            canonical_path=canonical,
            canonical_row={"target": observation["target"]},
            admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
        )

    first = create()
    return path, action_path, observation, posts, first, create


@pytest.mark.parametrize("changed", ["checks", "pending", "mergeable", "threads", "action"])
def test_terminal_successor_reuses_original_link_after_witness_changes(
    tmp_path, monkeypatch, bundle, policy, changed
):
    path, action_path, observation, posts, first, create = _terminal_successor_case(
        tmp_path, monkeypatch, bundle, policy
    )
    before_journal, before_queue = path.read_bytes(), action_path.read_bytes()
    observation["revision"] = "new-observation-projection"
    if changed == "pending":
        observation.update(pending_check_run_ids=[918], pending_check_head=observation["head"])
    elif changed == "threads":
        observation.update(active_threads=1, threads=[{"id": "new-thread", "isResolved": False}])
    else:
        observation[changed] = {
            "checks": "PENDING",
            "mergeable": "UNKNOWN",
            "action": "disposition",
        }[changed]
    second = create()
    assert second["reused"] and second["finite_frontier"] == first["finite_frontier"]
    assert second["predecessor"] == first["predecessor"]
    assert path.read_bytes() == before_journal and action_path.read_bytes() == before_queue
    assert posts == ["POST"]


@pytest.mark.parametrize(
    "changed", ["request_basis", "operation_id", "event", "run_id", "run_attempt"]
)
def test_terminal_successor_never_reuses_another_stable_result(
    tmp_path, monkeypatch, bundle, policy, changed
):
    path, action_path, observation, posts, _first, create = _terminal_successor_case(
        tmp_path, monkeypatch, bundle, policy
    )
    before_journal, before_queue = path.read_bytes(), action_path.read_bytes()
    marker = observation["verifier"]
    if changed == "request_basis":
        marker["verification_request_basis"] = "f" * 64
    elif changed == "operation_id":
        # A stored link for another operation must not borrow the current
        # operation's independently revalidated terminal proof.
        state = json.loads(path.read_text())
        child = next(iter(state["stage_successors"].values()))
        child["stage_predecessor"]["accepted_terminal"]["operation_id"] = "another-operation"
        path.write_text(json.dumps(state))
        before_journal = path.read_bytes()
    elif changed == "event":
        event = marker["verification_event"]
        event["material_sha256"] = "f" * 64
        event["event_id"] = verification_events.event_id(event)
    else:
        marker[changed] = "50002" if changed == "run_id" else "2"
    observation["verifier_lifecycle"] = copy.deepcopy(marker)
    with pytest.raises(ValueError, match="correlated terminal proof|successor identity conflicts"):
        create()
    assert path.read_bytes() == before_journal and action_path.read_bytes() == before_queue
    assert posts == ["POST"]


@pytest.mark.parametrize("crash", [False, True])
def test_actual_core_campaign_terminal_wake_restart_and_fresh_successor_never_reposts(
    tmp_path, monkeypatch, bundle, policy, crash
):
    import time

    import closer_campaign as campaigns
    import closer_hold_followup as holds
    import closer_pipeline as pipeline
    import closer_resources as resources
    import closer_stage_continuation as stages

    obj, path, action_path, canonical, frontier, observation, manifest, manifest_path, posts = (
        _pending_campaign(tmp_path, monkeypatch, bundle, policy)
    )
    before_source, before_queue = path.read_bytes(), action_path.read_bytes()
    if crash:
        original_update = obj.update

        class Interruption(BaseException):
            pass

        def update(callback):
            if callback.__name__ == "finish":
                raise Interruption("after durable terminal sidecar")
            return original_update(callback)

        monkeypatch.setattr(obj, "update", update)
        with pytest.raises(Interruption):
            holds.consume(obj, manifest, str(manifest_path))
        held = next(iter(obj.read()["hold_followups"].values()))
        assert held["status"] == "checking" and held["attempts"] == 1
        assert held["terminal_followup_intent"]
        obj = campaigns.Campaign(obj.path)
    doc, _ = holds.consume(obj, manifest, str(manifest_path))
    held = next(iter(doc["hold_followups"].values()))
    assert held["status"] == "released" and held["attempts"] == 1
    assert path.read_bytes() == before_source and action_path.read_bytes() == before_queue
    rearmed = holds.rearm(obj, doc, manifest)
    assert rearmed["generation"] == 2 and rearmed["targets"] == {campaigns.ROUND_UNIT: "pending"}
    assert rearmed["worker_config"]["finite_frontier"] == frontier
    assert next(iter(rearmed["hold_followups"].values()))["status"] == "consumed"
    assert holds.rearm(obj, obj.read(), manifest)["generation"] == 2
    assert posts == ["POST"] and path.read_bytes() == before_source
    observation["observed_at"] = time.time()
    child = stages.create_successor(
        path,
        action_path,
        predecessor=frontier,
        target=observation["target"],
        observation=observation,
        canonical_path=canonical,
        canonical_row={"target": observation["target"]},
        admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
    )
    again = stages.create_successor(
        path,
        action_path,
        predecessor=frontier,
        target=observation["target"],
        observation=observation,
        canonical_path=canonical,
        canonical_row={"target": observation["target"]},
        admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
    )
    assert again["reused"] and again["finite_frontier"] == child["finite_frontier"]
    seen = []
    lease = resources.acquire(
        path.parent.parent.parent,
        observation["target"],
        owner="fresh-child-admission",
        finite_frontier=child["finite_frontier"],
    )
    try:
        with resources.fenced(lease):
            result = pipeline._run_pipeline_finite(
                path,
                [observation["target"]],
                frontier_target=observation["target"],
                finite_frontier=child["finite_frontier"],
                prepare=lambda *_: seen.append("fresh-PREPARE")
                or {"revision": observation["revision"]},
                interpret=lambda *_: {"substantive_needs": []},
                exception=lambda *_: {"route": "normal"},
                act=lambda *_: {"status": "completed", "target_complete": True},
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            )
    finally:
        lease.release()
    assert result["targets"][observation["target"]]["status"] == "completed"
    assert seen == ["fresh-PREPARE"] and posts == ["POST"]
    assert action_path.read_bytes() == before_queue


@pytest.mark.parametrize("second", ["f" * 64, "invalid", None])
def test_correlation_ambiguity_never_changes_semantic_verdict(tmp_path, second):
    path, target, accepted, observation = terminal_world(tmp_path)
    marker = observation["verifier"]
    repo, number = target.split("#")
    other = {**marker, "verification_request_basis": second}
    comments = [
        {
            "author": {"login": "github-actions[bot]"},
            "url": f"https://github.com/{repo}/pull/{number}#issuecomment-{index}",
            "body": "<!-- verifier-corpus-decision/v1 " + json.dumps(record) + " -->",
        }
        for index, record in enumerate((marker, other), start=1)
    ]
    pr = {
        "number": int(number),
        "headRefOid": "a" * 40,
        "mergeCommit": {"oid": "b" * 40},
        "comments": {"nodes": comments},
    }
    parsed = verifier_evidence.decision_from_pr(repo, pr)
    assert parsed["verdict"] == "PASS"
    assert parsed["_request_correlation_unusable"] is True
    assert "verification_request_basis" not in parsed
    observation.update(
        verifier=parsed,
        verifier_lifecycle=verifier_evidence.decision_from_pr(repo, pr, include_pending=True),
    )
    assert followup.terminal_proof(path, target, accepted, observation, now=1000) is None


def test_real_snapshot_incidental_comment_churn_preserves_followup_but_sources_change_it(
    tmp_path, monkeypatch
):
    from test_closer_dispatcher import _graphql_pr, _snapshot_from_document

    path, target, accepted, _toy = terminal_world(tmp_path, target="o/r#7")
    document = _graphql_pr()
    document.update(state="MERGED", mergeCommit={"oid": "b" * 40}, mergedAt="2026-10-25T00:00:00Z")
    marker = _terminal_marker(target, "a" * 40, "b" * 40, BASIS)
    document["comments"]["nodes"] = [
        {
            "author": {"login": "github-actions[bot]"},
            "url": "https://github.com/o/r/pull/7#issuecomment-8",
            "body": "<!-- verifier-corpus-decision/v1 " + json.dumps(marker) + " -->",
        }
    ]
    first = _snapshot_from_document(monkeypatch, document)
    backend = dispatcher.Closer(SimpleNamespace(), state_path=path)

    def projected(snapshot):
        return {
            **snapshot,
            "action": "close_source",
            "eligible": True,
            "operation_binding": backend.operation_binding(target, snapshot),
        }

    first = projected(first)
    stored = json.loads(path.read_text())
    core = stored["frontiers"][accepted["operation_frontier"]]["items"][target]["operation"][
        "result"
    ]["core_request_correlation"]
    core["native_binding"]["verification_source_requirements"] = followup.source_requirements(first)
    core["native_binding_sha256"] = followup.native_binding_digest(core["native_binding"])
    path.write_text(json.dumps(stored))
    original = followup.terminal_proof(path, target, accepted, first, now=first["observed_at"])
    assert original is not None
    document["updatedAt"] = "2026-10-25T01:00:00Z"
    document["comments"]["nodes"].append(
        {
            "author": {"login": "github-actions[bot]"},
            "url": "https://github.com/o/r/pull/7#issuecomment-9",
            "body": "verifier unrelated diagnostic receipt",
        }
    )
    changed = projected(_snapshot_from_document(monkeypatch, document))
    assert changed["revision"] != first["revision"]
    assert changed["review_revision"] != first["review_revision"]
    assert changed["operation_binding"] != first["operation_binding"]
    assert (
        followup.terminal_proof(path, target, accepted, changed, now=changed["observed_at"])
        == original
    )
    document["closingIssuesReferences"]["nodes"][0]["body"] += "\n- New substantive requirement"
    requirements_changed = projected(_snapshot_from_document(monkeypatch, document))
    assert (
        followup.terminal_proof(
            path, target, accepted, requirements_changed, now=requirements_changed["observed_at"]
        )
        is None
    )


@pytest.mark.parametrize("changed", ["body", "source-body", "hold-label", "thread", "head"])
def test_released_terminal_followup_refuses_substantive_change_before_rearm(
    tmp_path, monkeypatch, bundle, policy, changed
):
    import closer_hold_followup as holds

    obj, _path, _action, _canonical, _frontier, observation, manifest, manifest_path, posts = (
        _pending_campaign(tmp_path, monkeypatch, bundle, policy)
    )
    doc, _ = holds.consume(obj, manifest, str(manifest_path))
    assert next(iter(doc["hold_followups"].values()))["status"] == "released"
    if changed == "body":
        observation["body"] += " substantive"
    elif changed == "source-body":
        observation["sources"] = [{"target": "acme/repo#1", "body": "new source requirement"}]
    elif changed == "hold-label":
        observation["labels"] = ["hold"]
    elif changed == "thread":
        observation["threads"] = [
            {
                "id": "new-review",
                "isResolved": False,
                "comments": {"nodes": [{"body": "new unresolved criterion"}]},
            }
        ]
    else:
        observation["head"] = "f" * 40
    with pytest.raises(ValueError, match="terminal result or fresh successor binding changed"):
        holds.rearm(obj, doc, manifest)
    assert obj.read()["generation"] == 1 and posts == ["POST"]
