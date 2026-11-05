"""Only a complete native nonclosing owned PR may coexist with unique F delivery."""

import copy
import time
from urllib.parse import parse_qs

import pytest
from test_closer_owned_delivery_lineage import PREFIX, REPO, cross
from test_closer_owned_delivery_lineage import native as native_base  # noqa: F401

import closer_correction_ownership as ownership
import closer_dispatcher as closer


@pytest.fixture
def neighborhood(native_base):  # noqa: F811 - shared inert fixture
    rows, seed = native_base
    # Exact observed topology: source983 mentions integration985, whose native
    # closing list is complete/empty; incumbent988 has unique delivery989.
    old_delivery = rows.pop(PREFIX + "/pulls/10002")
    old_delivery.update(number=989, node_id="PR_989")
    rows[PREFIX + "/pulls/989"] = old_delivery
    rows[PREFIX + "/issues/984/timeline"] = [cross(988), cross(989, pr=True)]
    rows[PREFIX + "/issues/983/timeline"] = [cross(984, pr=True), cross(985, pr=True)]
    rows[PREFIX + "/issues/988/timeline"] = [cross(984, pr=True), cross(989, pr=True)]
    graph = rows.pop("graphql")["data"]["repository"]
    graph["issue"]["closedByPullRequestsReferences"]["nodes"][0].update(id="PR_989", number=989)
    graph["pullRequest"].update(id="PR_989", number=989)
    graph["original"] = {
        "id": "I_983",
        "number": 983,
        "closedByPullRequestsReferences": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }
    rows["graph989"] = {"data": {"repository": graph}}
    info = copy.deepcopy(old_delivery)
    info.update(number=985, node_id="PR_985", body="Integration metadata mentions #983")
    info["head"]["sha"] = "f" * 40
    info["head"]["ref"] = "codex/integration"
    rows[PREFIX + "/pulls/985"] = info
    other = copy.deepcopy(graph)
    other["pullRequest"].update(
        id="PR_985",
        number=985,
        headRefOid="f" * 40,
        headRefName="codex/integration",
        body=info["body"],
    )
    other["pullRequest"]["closingIssuesReferences"]["nodes"] = []
    rows["graph985"] = {"data": {"repository": other}}
    return rows, seed


def observe(rows, seed, *, race=None):
    counts, calls = {}, []

    def api(args, **_kwargs):
        assert args[:2] == ["api", "--method"]
        path = args[3].split("?")[0]
        key = path
        if path == "graphql":
            assert args[2] == "POST" and "original:issue(number:983)" in args[-1]
            key = "graph985" if "pullRequest(number:985)" in args[-1] else "graph989"
        else:
            assert args[2] == "GET"
        calls.append(key)
        counts[key] = counts.get(key, 0) + 1
        value = copy.deepcopy(rows[key])
        if isinstance(value, list) and "?" in args[3]:
            query = parse_qs(args[3].split("?", 1)[1])
            size, page = int(query["per_page"][0]), int(query["page"][0])
            value = value[(page - 1) * size : page * size]
        if race and counts[key] > 1:
            race(key, value)
        return 0, value

    value = ownership.collect_ownership(
        github=closer.GitHub(deadline=time.time() + 60, api_fn=api),
        seed=seed,
        deadline=time.time() + 60,
    ).document()
    return value, calls


def test_native_two_pr_topology_keeps_unique_delivery_and_nonclosing_reference(neighborhood):
    rows, seed = neighborhood
    value, calls = observe(rows, seed)
    assert value["ownership"]["kind"] == "existing"
    assert value["incumbent"]["issue"]["number"] == "988"
    assert calls.count("graph985") == calls.count("graph989") == 2
    assert calls.count(PREFIX + "/pulls/985") == calls.count(PREFIX + "/pulls/989") == 2
    assert len(calls) == 24 <= ownership.MAX_CALLS


@pytest.mark.parametrize(
    "damage",
    [
        "info_closes_F",
        "info_closes_other",
        "info_partial",
        "source_closed",
        "source_partial",
        "two_true_deliveries",
        "info_foreign",
        "info_author",
        "info_draft",
        "info_closed",
        "info_merged",
        "info_head",
        "info_body",
        "info_node",
        "third_PR",
        "F_wrong_delivery",
        "info_missing_node",
    ],
)
def test_no_conflicting_delivery_or_unproven_informational_pr_is_ignored(neighborhood, damage):
    rows, seed = neighborhood
    graph = rows["graph985"]["data"]["repository"]
    pr = rows[PREFIX + "/pulls/985"]
    if damage in {"info_closes_F", "info_closes_other"}:
        graph["pullRequest"]["closingIssuesReferences"]["nodes"] = [
            {
                "id": "I_988",
                "number": 988 if damage == "info_closes_F" else 999,
                "repository": {"nameWithOwner": REPO},
            }
        ]
    elif damage == "info_partial":
        graph["pullRequest"]["closingIssuesReferences"]["pageInfo"]["hasNextPage"] = True
    elif damage == "source_closed":
        graph["original"]["closedByPullRequestsReferences"]["nodes"] = [{"id": "PR_985"}]
    elif damage == "source_partial":
        graph["original"]["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"] = True
    elif damage == "two_true_deliveries":
        for name in ["graph985", "graph989"]:
            rows[name]["data"]["repository"]["issue"]["closedByPullRequestsReferences"][
                "nodes"
            ].append({"id": "PR_985", "number": 985, "repository": {"nameWithOwner": REPO}})
    elif damage == "info_foreign":
        pr["head"]["repo"]["id"] = 999
    elif damage == "info_author":
        pr["user"] = {"id": 77, "login": "other"}
    elif damage == "info_draft":
        pr["draft"] = True
    elif damage == "info_closed":
        pr["state"] = "closed"
    elif damage == "info_merged":
        pr["merged"] = True
    elif damage == "info_head":
        pr["head"]["sha"] = "0" * 40
    elif damage == "info_body":
        pr["body"] += "changed"
    elif damage == "info_node":
        graph["pullRequest"]["id"] = "PR_wrong"
    elif damage == "info_missing_node":
        graph["original"]["id"] = "I_wrong"
    elif damage == "third_PR":
        rows[PREFIX + "/issues/983/timeline"].append(cross(999, pr=True))
    elif damage == "F_wrong_delivery":
        graph["issue"]["closedByPullRequestsReferences"]["nodes"][0]["number"] = 777
    with pytest.raises(ValueError):
        observe(rows, seed)


@pytest.mark.parametrize("key", ["graph985", PREFIX + "/pulls/985"])
def test_informational_pr_is_freshly_reread(neighborhood, key):
    rows, seed = neighborhood

    def race(path, value):
        if path == key:
            if path == "graph985":
                value["data"]["repository"]["pullRequest"]["headRefOid"] = "0" * 40
            else:
                value["body"] += "raced"

    with pytest.raises(ownership.OwnershipUnavailable, match="changed during observation"):
        observe(rows, seed, race=race)


@pytest.mark.parametrize("damage", ["nonempty", "partial", "identity"])
def test_delivery_query_cannot_contradict_original_source_relation(neighborhood, damage):
    rows, seed = neighborhood
    source = rows["graph989"]["data"]["repository"]["original"]
    if damage == "nonempty":
        source["closedByPullRequestsReferences"]["nodes"] = [{"id": "PR_985"}]
    elif damage == "partial":
        source["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"] = True
    else:
        source["id"] = "I_other"
    with pytest.raises(ownership.OwnershipUnavailable, match="original source closing relation"):
        observe(rows, seed)


def test_postmerge_informational_pr_is_not_treated_as_delivery(neighborhood):
    rows, seed = neighborhood
    rows[PREFIX + "/pulls/985"].update(state="closed", merged=True)
    rows["graph985"]["data"]["repository"]["pullRequest"].update(state="MERGED", merged=True)
    value, calls = observe(rows, seed)
    assert value["ownership"]["kind"] == "existing"
    assert value["incumbent"]["issue"]["number"] == "988"
    assert len(calls) == 24


@pytest.mark.parametrize(
    "damage",
    [
        "rest_closed_unmerged",
        "graphql_closed_merged",
        "rest_open_merged",
        "graphql_open_merged",
        "merged_draft",
        "merged_identity",
        "merged_body",
    ],
)
def test_incoherent_merged_information_never_clears_a_hold(neighborhood, damage):
    rows, seed = neighborhood
    rest = rows[PREFIX + "/pulls/985"]
    native = rows["graph985"]["data"]["repository"]["pullRequest"]
    rest.update(state="closed", merged=True)
    native.update(state="MERGED", merged=True)
    if damage == "rest_closed_unmerged":
        rest["merged"] = False
    elif damage == "graphql_closed_merged":
        native["state"] = "CLOSED"
    elif damage == "rest_open_merged":
        rest["state"] = "open"
    elif damage == "graphql_open_merged":
        native["state"] = "OPEN"
    elif damage == "merged_draft":
        native["isDraft"] = rest["draft"] = True
    elif damage == "merged_identity":
        rest["node_id"] = "PR_other"
    elif damage == "merged_body":
        rest["body"] += "changed"
    with pytest.raises(ownership.OwnershipUnavailable):
        observe(rows, seed)


def test_merged_information_state_is_rechecked(neighborhood):
    rows, seed = neighborhood
    rows[PREFIX + "/pulls/985"].update(state="closed", merged=True)
    rows["graph985"]["data"]["repository"]["pullRequest"].update(state="MERGED", merged=True)

    def race(path, value):
        if path == "graph985":
            value["data"]["repository"]["pullRequest"]["state"] = "CLOSED"

    with pytest.raises(ownership.OwnershipUnavailable, match="changed during observation"):
        observe(rows, seed, race=race)


@pytest.mark.parametrize("events,held", [(29, False), (30, True), (100, True)])
def test_multi_page_incumbent_keeps_original_finite_call_budget(neighborhood, events, held):
    rows, seed = neighborhood
    for number in (984, 983, 988):
        key = PREFIX + "/issues/" + str(number) + "/timeline"
        rows[key].extend(
            {"event": "labeled", "id": 100000 + index, "label": {"name": "inert"}}
            for index in range(events - len(rows[key]))
        )
    assert ownership.MAX_CALLS == 32
    if held:
        with pytest.raises(ownership.OwnershipUnavailable, match="call budget exhausted"):
            observe(rows, seed)
    else:
        value, calls = observe(rows, seed)
        assert value["ownership"]["kind"] == "existing"
        assert len(calls) == 30 <= ownership.MAX_CALLS
