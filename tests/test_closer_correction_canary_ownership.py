"""The Source983/PR984 canary can authenticate only its known existing chain."""

from __future__ import annotations

import copy
import json
import time

import pytest

import closer_correction_canary_ownership as canary
import closer_correction_handoff as handoff
import closer_correction_ownership as ownership
import closer_dispatcher as closer


def issue(number, node, *, state="open", assignees=None, body="body", labels=None):
    return {
        "number": int(number),
        "node_id": node,
        "title": "native",
        "body": body,
        "state": state,
        "assignees": [] if assignees is None else assignees,
        "labels": [] if labels is None else labels,
    }


def cross(number, node, *, is_pr):
    source = {
        "type": "issue",
        "issue": {
            "id": int(number) + 1000,
            "number": int(number),
            "node_id": node,
            "repository": {"full_name": "stranske/Orchestrator"},
        },
    }
    if is_pr:
        source["issue"]["pull_request"] = {}
    return {
        "event": "cross-referenced",
        "source": source,
        "actor": {"id": 1},
        "created_at": "2026-10-30T00:00:00Z",
    }


def pull(number, node, *, target=False):
    value = {
        "number": int(number),
        "node_id": node,
        "state": "open",
        "merged": False,
        "draft": False,
        "user": {"login": canary.OWNER_LOGIN, "id": canary.OWNER_ID},
    }
    if target:
        value.update(
            head={"sha": canary.TARGET_HEAD_SHA},
            base={"sha": canary.TARGET_BASE_SHA},
            merge_commit_sha=canary.TARGET_MERGE_SHA,
        )
    else:
        value.update(
            head={
                "sha": "d" * 40,
                "ref": "canary-988",
                "repo": {"full_name": "stranske/Orchestrator", "id": canary.REPOSITORY_ID},
            },
            base={
                "sha": "e" * 40,
                "ref": "main",
                "repo": {"full_name": "stranske/Orchestrator", "id": canary.REPOSITORY_ID},
            },
            body="canary delivery",
        )
    return value


def graph():
    def relation(number, node):
        return {
            "nodes": [
                {
                    "id": node,
                    "number": int(number),
                    "repository": {"nameWithOwner": "stranske/Orchestrator"},
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }

    return {
        "data": {
            "repository": {
                "databaseId": canary.REPOSITORY_ID,
                "nameWithOwner": "stranske/Orchestrator",
                "i988": {
                    "id": canary.ISSUE_NODE_ID,
                    "number": 988,
                    "state": "OPEN",
                    "assignees": {
                        "nodes": [{"login": canary.OWNER_LOGIN, "databaseId": canary.OWNER_ID}],
                        "pageInfo": {"hasNextPage": False},
                    },
                    "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "closedByPullRequestsReferences": relation(
                        canary.DELIVERY_PR, canary.DELIVERY_NODE_ID
                    ),
                },
                "p989": {
                    "id": canary.DELIVERY_NODE_ID,
                    "number": 989,
                    "state": "OPEN",
                    "isDraft": False,
                    "merged": False,
                    "headRefOid": "d" * 40,
                    "baseRefOid": "e" * 40,
                    "headRefName": "canary-988",
                    "headRepository": {
                        "nameWithOwner": "stranske/Orchestrator",
                        "databaseId": canary.REPOSITORY_ID,
                    },
                    "body": "canary delivery",
                    "author": {"login": canary.OWNER_LOGIN, "databaseId": canary.OWNER_ID},
                    "closingIssuesReferences": relation(canary.ISSUE, canary.ISSUE_NODE_ID),
                },
                "p998": {
                    "id": "PR_998",
                    "number": 998,
                    "state": "MERGED",
                    "merged": True,
                    "closingIssuesReferences": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                },
                "i997": {
                    "id": "I_997",
                    "number": 997,
                    "state": "CLOSED",
                    "assignees": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "closedByPullRequestsReferences": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
                "i999": {
                    "id": "I_999",
                    "number": 999,
                    "state": "OPEN",
                    "assignees": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "closedByPullRequestsReferences": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
            }
        }
    }


def graph_lineage():
    return {
        "data": {
            "repository": {
                "databaseId": canary.REPOSITORY_ID,
                "nameWithOwner": "stranske/Orchestrator",
                "issue": {
                    "id": canary.ISSUE_NODE_ID,
                    "timelineItems": {
                        "nodes": [
                            {
                                "actor": {
                                    "__typename": "User",
                                    "login": canary.OWNER_LOGIN,
                                    "databaseId": canary.OWNER_ID,
                                },
                                "createdAt": "2026-10-30T00:00:00Z",
                                "source": {
                                    "__typename": "PullRequest",
                                    "id": canary.TARGET_NODE_ID,
                                    "number": int(canary.TARGET_PR),
                                    "headRefOid": canary.TARGET_HEAD_SHA,
                                    "repository": {
                                        "nameWithOwner": "stranske/Orchestrator",
                                        "databaseId": canary.REPOSITORY_ID,
                                    },
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
            }
        }
    }


def world(monkeypatch):
    monkeypatch.setattr(closer.gh_capacity, "throttle", lambda *_a, **_k: {"action": "allow"})
    source = issue(
        canary.SOURCE_ISSUE,
        canary.SOURCE_NODE_ID,
        body=(
            "Controlled public CSV fixture task for one real Closer end-to-end test. Every issue, "
            "branch and PR in this chain must be visibly labeled as a test. The initial partial fixture "
            "revision is intentionally incomplete and must never be described as completed implementation. "
            "Original requirements below remain unchanged before and after that initial merge.\n\n"
            "Tasks and acceptance criteria:\n"
            "- In tests/fixtures/closer_real_csv_canary/v1/csv_export.py, implement export_rows(rows, "
            "format) for fixed fields name and score. JSON must preserve the existing round-trip behavior.\n"
            "- CSV must emit exactly the header name,score, CRLF record endings and one record per input. "
            "Values containing commas, quotes, LF or CR must round-trip through Python csv.reader. Empty "
            "input must return the header alone.\n"
            "- Unsupported formats must raise ValueError.\n"
            "- Add verify_csv_contract.py in the same isolated fixture directory. It is an explicit "
            "standard-library-only contract script, not an automatically collected production test. Run it "
            "on the partial revision to retain a genuine failure, then after correction to retain a genuine pass.\n"
            "- Keep every change within these two fixture files; do not alter production modules, workflows, "
            "credentials, permissions or settings.\n"
            "- Genuine native Verify outcomes and corrective worker/publication proof are required before "
            "claiming this test complete. Mock evidence and an initial partial merge do not satisfy the task."
        ),
    )
    target = pull(canary.TARGET_PR, canary.TARGET_NODE_ID, target=True)
    candidate = issue(
        canary.ISSUE,
        canary.ISSUE_NODE_ID,
        assignees=[{"login": canary.OWNER_LOGIN, "id": canary.OWNER_ID}],
    )
    target_rows = [
        cross(canary.ISSUE, canary.ISSUE_NODE_ID, is_pr=False),
        cross(canary.DELIVERY_PR, canary.DELIVERY_NODE_ID, is_pr=True),
        cross("998", "PR_998", is_pr=True),
        cross("997", "I_997", is_pr=False),
    ]
    source_rows = [
        cross(canary.TARGET_PR, canary.TARGET_NODE_ID, is_pr=True),
        cross("999", "I_999", is_pr=False),
    ]
    candidate_events = [
        {
            "id": 77,
            "event": "assigned",
            "assignee": {"login": canary.OWNER_LOGIN},
            "created_at": "2026-10-30T00:00:00Z",
        }
    ]
    candidate_rows = [cross(canary.TARGET_PR, canary.TARGET_NODE_ID, is_pr=True)]
    paths = {
        f"repos/{canary.REPOSITORY}/pulls/{canary.TARGET_PR}": target,
        f"repos/{canary.REPOSITORY}/issues/{canary.SOURCE_ISSUE}": source,
        f"repos/{canary.REPOSITORY}/issues/{canary.TARGET_PR}/timeline": target_rows,
        f"repos/{canary.REPOSITORY}/issues/{canary.SOURCE_ISSUE}/timeline": source_rows,
        f"repos/{canary.REPOSITORY}/issues/{canary.ISSUE}": candidate,
        f"repos/{canary.REPOSITORY}/issues/{canary.ISSUE}/events": candidate_events,
        f"repos/{canary.REPOSITORY}/issues/{canary.ISSUE}/timeline": candidate_rows,
        f"repos/{canary.REPOSITORY}/collaborators/{canary.OWNER_LOGIN}/permission": {
            "permission": "write",
            "user": {"login": canary.OWNER_LOGIN, "id": canary.OWNER_ID},
        },
        f"repos/{canary.REPOSITORY}/pulls/{canary.DELIVERY_PR}": pull(
            canary.DELIVERY_PR, canary.DELIVERY_NODE_ID
        ),
        "graphql": graph(),
        "lineage_graphql": graph_lineage(),
    }
    calls = []

    def api(args, **kwargs):
        path = args[3]
        calls.append((path, copy.deepcopy(kwargs)))
        if path == "graphql":
            query = next(value[6:] for value in args if value.startswith("query="))
            if "timelineItems" in query:
                return 0, copy.deepcopy(paths["lineage_graphql"])
            return 0, copy.deepcopy(paths["graphql"])
        plain, _, query = path.partition("?")
        if query:
            fields = dict(item.split("=") for item in query.split("&"))
            size, page = int(fields["per_page"]), int(fields["page"])
            return 0, copy.deepcopy(paths[plain][(page - 1) * size : page * size])
        return 0, copy.deepcopy(paths[path])

    seed = {
        "target": {
            "repository": "stranske/orchestrator",
            "pr": canary.TARGET_PR,
            "head_sha": canary.TARGET_HEAD_SHA,
            "base_sha": canary.TARGET_BASE_SHA,
            "merge_sha": canary.TARGET_MERGE_SHA,
        },
        "source_issue": ownership._issue(source, canary.SOURCE_ISSUE),
        "finding": {"evidence": {"ref": "comparison.json#/results/0", "sha256": "f" * 64}},
        "scope": "substantive_implementation",
        "original_verifier": {
            "verification_event": {
                "event_id": canary.VERIFICATION_EVENT,
                "repo": "stranske/orchestrator",
                "pr": canary.TARGET_PR,
                "head_sha": canary.TARGET_HEAD_SHA,
                "evaluated_sha": canary.TARGET_MERGE_SHA,
            }
        },
    }
    reads = ownership._Reads(closer.GitHub(deadline=time.time() + 60, api_fn=api), time.time() + 60)
    return reads, seed, [target, source], [target_rows, source_rows], paths, calls


def prove(monkeypatch):
    reads, seed, docs, rows, paths, calls = world(monkeypatch)
    return (
        canary.collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows),
        reads,
        paths,
        calls,
    )


def test_known_canary_returns_only_existing_988_and_keeps_unselected_unknown(monkeypatch):
    actual, reads, _, calls = prove(monkeypatch)
    assert actual["incumbent"]["issue"]["number"] == canary.ISSUE
    assert actual["incumbent"]["owner"] == canary.OWNER_LOGIN
    assert actual["unselected_native_references"] == [
        {"number": "997", "node_id": "I_997", "kind": "closed"},
        {"number": "998", "node_id": "PR_998", "kind": "closed"},
        {"number": "999", "node_id": "I_999", "kind": "unselected_unknown"},
    ]
    assert reads.calls == len(calls) == 16
    assert reads.bytes < ownership.MAX_TOTAL_BYTES
    assert len(reads.refs) == reads.calls


@pytest.mark.parametrize(
    "section,field",
    [
        ("target", "head_sha"),
        ("target", "base_sha"),
        ("target", "merge_sha"),
        ("source_issue", "node_id"),
        ("source_issue", "body_sha256"),
        ("verification_event", "event_id"),
    ],
)
def test_reviewed_route_requires_full_identity_not_shared_example_numbers(
    monkeypatch, section, field
):
    _reads, seed, _docs, _rows, _paths, _calls = world(monkeypatch)
    assert canary.is_reviewed_seed(seed)
    changed = copy.deepcopy(seed)
    target = (
        changed["original_verifier"]["verification_event"]
        if section == "verification_event"
        else changed[section]
    )
    target[field] = "different"
    assert not canary.is_reviewed_seed(changed)


def test_collect_ownership_returns_standard_existing_incumbent_for_actual_seed_shape(monkeypatch):
    reads, seed, _, _, _, _ = world(monkeypatch)
    observed = ownership.collect_ownership(
        github=closer.GitHub(deadline=time.time() + 60, api_fn=reads.github.api_fn),
        seed=seed,
        deadline=time.time() + 60,
        now=time.time(),
    ).document()
    assert handoff._incumbent(observed["incumbent"])
    assert observed["ownership"]["kind"] == "existing"
    assert observed["unselected_native_references"][-1]["kind"] == "unselected_unknown"


@pytest.mark.parametrize("number", [canary.ISSUE, "999"])
@pytest.mark.parametrize("damage", ["node", "type"])
def test_conflicting_identity_between_native_timelines_holds_before_snapshot(
    monkeypatch, number, damage
):
    reads, seed, docs, rows, _paths, calls = world(monkeypatch)
    index = 1 if number == canary.ISSUE else 0
    node = canary.ISSUE_NODE_ID if number == canary.ISSUE else "I_999"
    rows[index].append(
        cross(number, "conflicting-node" if damage == "node" else node, is_pr=damage == "type")
    )
    with pytest.raises(ownership.OwnershipUnavailable, match="ambiguous canary lineage"):
        canary.collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows)
    assert calls == []


@pytest.mark.parametrize("timeline", [0, 1])
@pytest.mark.parametrize("number", [canary.TARGET_PR, canary.SOURCE_ISSUE])
@pytest.mark.parametrize("damage", ["node", "type"])
def test_context_identity_is_checked_before_removing_native_reference(
    monkeypatch, timeline, number, damage
):
    reads, seed, docs, rows, _paths, calls = world(monkeypatch)
    is_pr = number == canary.TARGET_PR
    node = canary.TARGET_NODE_ID if is_pr else canary.SOURCE_NODE_ID
    for row in rows:
        row[:] = [event for event in row if str(event["source"]["issue"]["number"]) != number]
    rows[timeline].append(
        cross(
            number,
            "conflicting-node" if damage == "node" else node,
            is_pr=not is_pr if damage == "type" else is_pr,
        )
    )
    with pytest.raises(ownership.OwnershipUnavailable, match="canary context lineage differs"):
        canary.collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows)
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [("assignees", [{"login": "other", "id": 9}]), ("labels", [{"name": "agents:other"}])],
)
def test_original_source_ownership_guard_runs_in_complete_collector(monkeypatch, field, value):
    reads, seed, _docs, _rows, paths, _calls = world(monkeypatch)
    paths[f"repos/{canary.REPOSITORY}/issues/{canary.SOURCE_ISSUE}"][field] = value
    with pytest.raises(ownership.OwnershipUnavailable, match="source agent ownership unresolved"):
        ownership.collect_ownership(
            github=closer.GitHub(deadline=time.time() + 60, api_fn=reads.github.api_fn),
            seed=seed,
            deadline=time.time() + 60,
            now=time.time(),
        )


@pytest.mark.parametrize(
    "damage",
    [
        "seed",
        "closure",
        "graphql_identity",
        "extra_owner",
        "agent_label",
        "closed_closes_selected",
        "missing",
        "changed",
    ],
)
def test_known_canary_refuses_wrong_or_changed_native_facts(monkeypatch, damage):
    reads, seed, docs, rows, paths, _ = world(monkeypatch)
    if damage == "seed":
        seed["original_verifier"]["verification_event"]["event_id"] = "wrong"
    elif damage == "closure":
        paths["graphql"]["data"]["repository"]["p989"]["closingIssuesReferences"] = {
            "nodes": [],
            "pageInfo": {"hasNextPage": False},
        }
    elif damage == "graphql_identity":
        paths["graphql"]["data"]["repository"]["p989"]["author"]["databaseId"] = 9
    elif damage == "extra_owner":
        paths["graphql"]["data"]["repository"]["i999"]["assignees"] = {
            "nodes": [{"login": "other", "id": 9}],
            "pageInfo": {"hasNextPage": False},
        }
    elif damage == "agent_label":
        paths["graphql"]["data"]["repository"]["i999"]["labels"] = {
            "nodes": [{"name": "agents:other"}],
            "pageInfo": {"hasNextPage": False},
        }
    elif damage == "closed_closes_selected":
        paths["graphql"]["data"]["repository"]["p998"]["closingIssuesReferences"] = {
            "nodes": [
                {
                    "id": canary.ISSUE_NODE_ID,
                    "number": int(canary.ISSUE),
                    "repository": {"nameWithOwner": "stranske/Orchestrator"},
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }
    elif damage == "missing":
        rows[0][:] = [row for row in rows[0] if row["source"]["issue"]["number"] != 989]
    else:
        paths[f"repos/{canary.REPOSITORY}/pulls/{canary.DELIVERY_PR}"]["user"]["id"] = 9
    with pytest.raises(ownership.OwnershipUnavailable):
        canary.collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows)


def test_known_canary_refuses_when_reader_budget_is_already_exhausted(monkeypatch):
    reads, seed, docs, rows, _, _ = world(monkeypatch)
    reads.calls = ownership.MAX_CALLS
    with pytest.raises(ownership.OwnershipUnavailable, match="call budget exhausted"):
        canary.collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows)


def test_foreign_lineage_identity_is_opaque_and_never_becomes_canary_authority(monkeypatch):
    reads, seed, docs, rows, paths, _ = world(monkeypatch)
    paths["lineage_graphql"]["data"]["repository"]["issue"]["timelineItems"]["nodes"].append(
        {
            "actor": {"__typename": "Bot"},
            "createdAt": "2026-10-30T00:01:00Z",
            "source": {
                "__typename": "Issue",
                "id": "I_foreign",
                "number": 4333,
                "repository": {"nameWithOwner": "excluded/foreign", "databaseId": 9},
            },
        }
    )
    actual = canary.collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows)
    assert actual["incumbent"]["issue"]["number"] == canary.ISSUE
    assert "excluded/foreign" not in json.dumps(actual, sort_keys=True)
