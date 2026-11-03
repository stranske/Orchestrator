"""A real formatter's extra reference cannot authorize an unrelated PR neighbor."""

import copy
import time

import pytest

import closer_correction_ownership as ownership
import closer_dispatcher as closer

REPO = "stranske/orchestrator"
PREFIX = "repos/" + REPO
OWNER = {"id": 23046322, "login": "stranske"}
F, P = 988, 10002  # P is synthetic: no delivery PR is created by these controls.


def cross(number, *, pr=False):
    issue = {
        "id": number + 100000,
        "number": number,
        "node_id": ("PR_" if pr else "I_") + str(number),
        "repository": {"full_name": REPO},
    }
    if pr:
        issue["pull_request"] = {}
    return {
        "event": "cross-referenced",
        "actor": OWNER,
        "created_at": "2026-10-28T01:00:00Z",
        "source": {"type": "issue", "issue": issue},
    }


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(closer.gh_capacity, "throttle", lambda *_a, **_k: {"action": "allow"})
    source = {
        "number": 983,
        "node_id": "I_983",
        "title": "original",
        "body": "unchanged",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    follow = dict(
        source, number=F, node_id="I_988", title="TEST corrective follow-up", assignees=[OWNER]
    )
    original = {
        "number": 984,
        "node_id": "PR_984",
        "state": "closed",
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40},
        "merge_commit_sha": "c" * 40,
    }
    # Actual formatter.run on the retained988 body emitted #984 in Scope and
    # Related Issues/PRs even when the initial P body was only 'Closes #988'.
    body = "Closes #988\n\nTEST: original test PR is #984.\nRelated Issues/PRs: #984"
    delivery = {
        "number": P,
        "node_id": "PR_10002",
        "state": "open",
        "draft": False,
        "merged": False,
        "body": body,
        "user": OWNER,
        "head": {"sha": "d" * 40, "ref": "codex/test-csv", "repo": {"id": 42, "full_name": REPO}},
        "base": {"sha": "e" * 40, "ref": "main", "repo": {"id": 42, "full_name": REPO}},
    }
    graph = {
        "data": {
            "repository": {
                "databaseId": 42,
                "nameWithOwner": REPO,
                "issue": {
                    "id": "I_988",
                    "number": F,
                    "closedByPullRequestsReferences": {
                        "nodes": [
                            {"id": "PR_10002", "number": P, "repository": {"nameWithOwner": REPO}}
                        ],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
                "pullRequest": {
                    "id": "PR_10002",
                    "number": P,
                    "state": "OPEN",
                    "isDraft": False,
                    "merged": False,
                    "headRefOid": "d" * 40,
                    "baseRefOid": "e" * 40,
                    "headRefName": "codex/test-csv",
                    "headRepository": {"nameWithOwner": REPO},
                    "body": body,
                    "closingIssuesReferences": {
                        "nodes": [
                            {"id": "I_988", "number": F, "repository": {"nameWithOwner": REPO}}
                        ],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
            }
        }
    }
    rows = {
        PREFIX + "/pulls/984": original,
        PREFIX + "/issues/983": source,
        PREFIX + "/issues/984/timeline": [cross(F), cross(P, pr=True)],
        PREFIX + "/issues/983/timeline": [cross(984, pr=True)],
        PREFIX + "/issues/988": follow,
        PREFIX
        + "/issues/988/events": [
            {"id": 1, "event": "assigned", "assignee": OWNER, "created_at": "2026-10-28T01:00:01Z"}
        ],
        PREFIX + "/issues/988/timeline": [cross(984, pr=True), cross(P, pr=True)],
        PREFIX + "/collaborators/stranske/permission": {"permission": "admin", "user": OWNER},
        PREFIX + "/pulls/10002": delivery,
        "graphql": graph,
    }
    seed = {
        "target": {
            "repository": REPO,
            "pr": "984",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "merge_sha": "c" * 40,
        },
        "source_issue": ownership._issue(source, 983),
        "finding": {"evidence": {"ref": "retained", "sha256": "f" * 64}},
        "scope": {"kind": "named"},
        "original_verifier": {"verification_event": {"event_id": "f" * 64}},
    }
    return rows, seed


def collect(rows, seed, *, race=None):
    calls = []
    counts = {}

    def api(args, **_kwargs):
        endpoint = args[3].split("?")[0]
        assert args[:2] == ["api", "--method"]
        assert args[2] == ("POST" if endpoint == "graphql" else "GET")
        if endpoint == "graphql":
            assert "closedByPullRequestsReferences(first:2)" in args[-1]
            assert "closingIssuesReferences(first:2)" in args[-1]
        calls.append(endpoint)
        counts[endpoint] = counts.get(endpoint, 0) + 1
        value = copy.deepcopy(rows[endpoint])
        if race and counts[endpoint] > 1:
            race(endpoint, value)
        return 0, value

    value = ownership.collect_ownership(
        github=closer.GitHub(deadline=time.time() + 60, api_fn=api),
        seed=seed,
        deadline=time.time() + 60,
    ).document()
    return value, calls


def test_formatter_generated_reference_allows_only_authenticated_owned_delivery(native):
    rows, seed = native
    value, calls = collect(rows, seed)
    assert value["ownership"]["kind"] == "existing" and value["incumbent"]["issue"][
        "number"
    ] == str(F)
    assert value["incumbent"]["owner"] == OWNER["login"]
    assert calls.count("graphql") == 2 and calls.count(PREFIX + "/pulls/10002") == 2
    assert len(calls) <= ownership.MAX_CALLS


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_F",
        "foreign",
        "draft",
        "closed",
        "merged",
        "wrong_author",
        "wrong_owner_id",
        "state_branch",
        "head",
        "body",
        "partial_closing",
        "partial_delivery",
        "ambiguous_closing",
        "nonunique_delivery",
        "wrong_node",
        "extra_PR",
        "source_assigned",
        "missing_reciprocal",
        "no_F",
        "wrong_permission",
    ],
)
def test_unrelated_or_unowned_delivery_remains_held(native, damage):
    rows, seed = native
    pr = rows[PREFIX + "/pulls/10002"]
    graph = rows["graphql"]["data"]["repository"]
    if damage == "wrong_F":
        graph["pullRequest"]["closingIssuesReferences"]["nodes"][0]["number"] = 989
    elif damage == "foreign":
        pr["head"]["repo"]["id"] = 999
    elif damage == "draft":
        pr["draft"] = True
    elif damage == "closed":
        pr["state"] = "closed"
    elif damage == "merged":
        pr["merged"] = True
    elif damage == "wrong_author":
        pr["user"] = {"login": "other", "id": 77}
    elif damage == "wrong_owner_id":
        pr["user"] = dict(OWNER, id=77)
    elif damage == "state_branch":
        pr["head"]["ref"] = "closer-correction-state"
        graph["pullRequest"]["headRefName"] = "closer-correction-state"
    elif damage == "head":
        pr["head"]["sha"] = "0" * 40
    elif damage == "body":
        pr["body"] += "changed"
    elif damage == "partial_closing":
        graph["pullRequest"]["closingIssuesReferences"]["pageInfo"]["hasNextPage"] = True
    elif damage == "partial_delivery":
        graph["issue"]["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"] = True
    elif damage == "ambiguous_closing":
        graph["pullRequest"]["closingIssuesReferences"]["nodes"] *= 2
    elif damage == "nonunique_delivery":
        graph["issue"]["closedByPullRequestsReferences"]["nodes"] *= 2
    elif damage == "wrong_node":
        graph["pullRequest"]["id"] = "PR_foreign"
    elif damage == "extra_PR":
        rows[PREFIX + "/issues/984/timeline"].append(cross(P + 1, pr=True))
    elif damage == "source_assigned":
        rows[PREFIX + "/issues/983"]["assignees"] = [OWNER]
    elif damage == "missing_reciprocal":
        rows[PREFIX + "/issues/988/timeline"] = [cross(P, pr=True)]
    elif damage == "no_F":
        rows[PREFIX + "/issues/984/timeline"] = [cross(P, pr=True)]
    elif damage == "wrong_permission":
        rows[PREFIX + "/collaborators/stranske/permission"]["permission"] = "read"
    with pytest.raises(ValueError):
        collect(rows, seed)


@pytest.mark.parametrize("endpoint", ["graphql", PREFIX + "/pulls/10002"])
def test_owned_delivery_identity_is_rechecked(native, endpoint):
    rows, seed = native

    def race(path, value):
        if path == endpoint:
            if path == "graphql":
                value["data"]["repository"]["pullRequest"]["headRefOid"] = "0" * 40
            else:
                value["body"] += "raced"

    with pytest.raises(ownership.OwnershipUnavailable, match="changed during observation"):
        collect(rows, seed, race=race)


def test_no_extra_pr_preserves_existing_collector_path_without_delivery_query(native):
    rows, seed = native
    rows[PREFIX + "/issues/984/timeline"] = [cross(F)]
    del rows["graphql"]
    del rows[PREFIX + "/pulls/10002"]
    value, calls = collect(rows, seed)
    assert value["ownership"]["kind"] == "existing"
    assert "graphql" not in calls and len(calls) == 16
