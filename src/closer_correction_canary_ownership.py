"""Fixed native ownership proof for the Source983/PR984 CSV canary.

This is deliberately not a generic topology policy and never establishes
absence.  It can authenticate only the already-existing issue988/PR989 work chain;
all other native references remain evidence and any active extra chain holds.
"""

from __future__ import annotations

from types import SimpleNamespace

import closer_correction_classification as contract
import closer_correction_ownership as ownership

REPOSITORY = "stranske/orchestrator"
REPOSITORY_ID = 1342306972
SOURCE_ISSUE = "983"
SOURCE_NODE_ID = "I_kwDOUAH6nM8AAAABaa2UNQ"
TARGET_PR = "984"
TARGET_NODE_ID = "PR_kwDOUAH6nM8AAAABLH3cQw"
TARGET_HEAD_SHA = "0507d28acf68d6bf21c68138d18d56cd01968827"
TARGET_BASE_SHA = "079b1bd32907653525839974451216ae349bf3a4"
TARGET_MERGE_SHA = "d1947b5b4775c2e6a07bb46b3f85eb2e28c1b001"
SOURCE_BODY_SHA256 = "64a22ce25c538cfeb98c058fc66bca2165ac829df1c037e5b95cefa22959646c"
VERIFICATION_EVENT = "334a0ec11f919e8d8469369b82a79d1de941535305f4b8b245d5bb01217c8cae"
CONSUMER_ID = "test-csv-followup988-codex"
ISSUE = "988"
ISSUE_NODE_ID = "I_kwDOUAH6nM8AAAABafDbPg"
DELIVERY_PR = "989"
DELIVERY_NODE_ID = "PR_kwDOUAH6nM8AAAABLLx7gQ"
OWNER_LOGIN = "stranske"
OWNER_ID = 23046322
_MAX_DISCOVERED = 16


def _need(value, reason):
    ownership._need(value, reason)


def _crossrefs(rows):
    found = {}
    for row in rows:
        if row.get("event") != "cross-referenced":
            continue
        issue = (row.get("source") or {}).get("issue") or {}
        repository = (issue.get("repository") or {}).get("full_name", "").lower()
        _need(repository == REPOSITORY, "foreign canary lineage")
        number = str(issue.get("number"))
        _need(number.isdigit(), "canary lineage number missing")
        node = issue.get("node_id")
        _need(isinstance(node, str) and node, "canary lineage node missing")
        is_pr = "pull_request" in issue
        _need(not is_pr or isinstance(issue["pull_request"], dict), "canary lineage type malformed")
        value = {"node_id": node, "is_pr": is_pr}
        _need(number not in found or found[number] == value, "ambiguous canary lineage")
        found[number] = value
    return found


def _case_seed(seed, docs):
    target = seed.get("target") or {}
    source = seed.get("source_issue") or {}
    verifier = (seed.get("original_verifier") or {}).get("verification_event") or {}
    _need(
        target.get("repository", "").lower() == REPOSITORY
        and str(target.get("pr")) == TARGET_PR
        and target.get("head_sha") == TARGET_HEAD_SHA
        and target.get("base_sha") == TARGET_BASE_SHA
        and target.get("merge_sha") == TARGET_MERGE_SHA
        and source.get("number") == SOURCE_ISSUE
        and source.get("node_id") == SOURCE_NODE_ID
        and source.get("body_sha256") == SOURCE_BODY_SHA256
        and seed.get("scope") == "substantive_implementation"
        and verifier.get("event_id") == VERIFICATION_EVENT
        and verifier.get("repo", "").lower() == REPOSITORY
        and str(verifier.get("pr")) == TARGET_PR
        and verifier.get("head_sha") == TARGET_HEAD_SHA
        and verifier.get("evaluated_sha") == TARGET_MERGE_SHA,
        "canary ownership seed differs",
    )
    _need(len(docs) == 2, "canary ownership documents missing")
    ownership._pull(docs[0], seed)
    _need(ownership._issue(docs[1], SOURCE_ISSUE) == source, "canary source changed")
    _need(docs[0].get("node_id") == TARGET_NODE_ID, "canary target node differs")


def _query(discovered):
    _need(0 < len(discovered) <= _MAX_DISCOVERED, "canary lineage exceeds bounded snapshot")
    fields = []
    for number, item in sorted(discovered.items(), key=lambda pair: int(pair[0])):
        alias = ("p" if item["is_pr"] else "i") + number
        if number == DELIVERY_PR:
            fields.append(
                alias
                + ":pullRequest(number:"
                + number
                + "){id number state isDraft merged headRefOid baseRefOid headRefName "
                "headRepository{nameWithOwner databaseId} body author{... on User{login databaseId}} "
                "closingIssuesReferences(first:2){nodes{id number repository{nameWithOwner}} "
                "pageInfo{hasNextPage}}}"
            )
        elif item["is_pr"]:
            fields.append(
                alias
                + ":pullRequest(number:"
                + number
                + "){id number state merged closingIssuesReferences(first:2)"
                "{nodes{id number repository{nameWithOwner}} pageInfo{hasNextPage}}}"
            )
        else:
            fields.append(
                alias
                + ":issue(number:"
                + number
                + "){id number state assignees(first:2){nodes{... on User{login databaseId}} pageInfo{hasNextPage}} "
                "labels(first:20){nodes{name} pageInfo{hasNextPage}} "
                "closedByPullRequestsReferences(first:2){nodes{id number repository{nameWithOwner}} "
                "pageInfo{hasNextPage}}}"
            )
    return (
        'query {repository(owner:"stranske",name:"Orchestrator"){databaseId nameWithOwner '
        + " ".join(fields)
        + "}}"
    )


def _snapshot(reads, discovered):
    query = _query(discovered)
    document = reads.get("graphql", fields={"query": query})
    repository = (document.get("data") or {}).get("repository") or {}
    _need(
        repository.get("databaseId") == REPOSITORY_ID
        and repository.get("nameWithOwner", "").lower() == REPOSITORY,
        "canary repository snapshot differs",
    )
    return document, query


def _candidate_lineage(reads, target):
    query = (
        'query {repository(owner:"stranske",name:"Orchestrator"){databaseId nameWithOwner '
        "issue(number:988){id timelineItems(first:100,itemTypes:[CROSS_REFERENCED_EVENT])"
        "{nodes{... on CrossReferencedEvent{actor{__typename ... on User{login databaseId}} "
        "createdAt source{__typename ... on Issue{id number repository{nameWithOwner databaseId}} "
        "... on PullRequest{id number repository{nameWithOwner databaseId}}}}} "
        "pageInfo{hasNextPage}}}}}"
    )
    document = reads.get("graphql", fields={"query": query})
    repository = (document.get("data") or {}).get("repository") or {}
    issue = repository.get("issue") or {}
    timeline = issue.get("timelineItems") or {}
    _need(
        repository.get("databaseId") == REPOSITORY_ID
        and repository.get("nameWithOwner", "").lower() == REPOSITORY
        and issue.get("id") == ISSUE_NODE_ID
        and isinstance(timeline.get("nodes"), list)
        and timeline.get("pageInfo", {}).get("hasNextPage") is False,
        "canary GraphQL lineage incomplete",
    )
    matching = []
    for event in timeline["nodes"]:
        source = (event or {}).get("source") or {}
        actor = (event or {}).get("actor") or {}
        repository = source.get("repository") or {}
        _need(
            isinstance(event, dict)
            and isinstance(event.get("createdAt"), str)
            and isinstance(source.get("id"), str)
            and type(source.get("number")) is int
            and isinstance(repository.get("nameWithOwner"), str)
            and type(repository.get("databaseId")) is int,
            "canary GraphQL lineage malformed",
        )
        ownership._instant(event["createdAt"])
        _need(
            actor.get("__typename") in {"User", "Bot", "Organization", "Mannequin"},
            "canary actor malformed",
        )
        if actor.get("__typename") == "User":
            _need(
                isinstance(actor.get("login"), str) and type(actor.get("databaseId")) is int,
                "canary user malformed",
            )
        if (
            source.get("__typename") == "PullRequest"
            and source.get("id") == TARGET_NODE_ID
            and source.get("number") == int(TARGET_PR)
            and repository.get("databaseId") == REPOSITORY_ID
            and repository.get("nameWithOwner", "").lower() == REPOSITORY
        ):
            _need(
                actor == {"__typename": "User", "login": OWNER_LOGIN, "databaseId": OWNER_ID},
                "canary target lineage actor differs",
            )
            matching.append(event)
    _need(len(matching) == 1, "canary target lineage missing or ambiguous")
    event = matching[0]
    _need(
        target.get("node_id") == TARGET_NODE_ID
        and (target.get("head") or {}).get("sha") == TARGET_HEAD_SHA,
        "canary target readback differs",
    )
    return (
        document,
        query,
        {
            "schema": "closer-correction-native-crossref/v2",
            "event": "cross-referenced",
            "actor_id": OWNER_ID,
            "created_at": event["createdAt"],
            "source_pr_node_id": TARGET_NODE_ID,
            "source_repository_id": REPOSITORY_ID,
            "source_repository": REPOSITORY,
            "source_pr": TARGET_PR,
            "source_head_sha": TARGET_HEAD_SHA,
        },
    )


def _relation(value, field, number):
    relation = value.get(field) or {}
    _need(
        isinstance(relation, dict)
        and isinstance(relation.get("nodes"), list)
        and relation.get("pageInfo", {}).get("hasNextPage") is False,
        "canary relation pagination incomplete",
    )
    return relation.get("nodes") == [
        {
            "id": ISSUE_NODE_ID if number == ISSUE else DELIVERY_NODE_ID,
            "number": int(number),
            "repository": {"nameWithOwner": "stranske/Orchestrator"},
        }
    ]


def _validate_snapshot(document, discovered):
    repository = document["data"]["repository"]
    unselected = []
    for number, item in discovered.items():
        alias = ("p" if item["is_pr"] else "i") + number
        value = repository.get(alias)
        _need(
            isinstance(value, dict)
            and str(value.get("number")) == number
            and value.get("id") == item["node_id"],
            "canary discovered identity changed",
        )
        if number == ISSUE:
            _need(
                not item["is_pr"]
                and value.get("id") == ISSUE_NODE_ID
                and value.get("state") == "OPEN",
                "canary incumbent differs",
            )
            assignees = value.get("assignees") or {}
            _need(
                assignees.get("pageInfo", {}).get("hasNextPage") is False
                and assignees.get("nodes") == [{"login": OWNER_LOGIN, "databaseId": OWNER_ID}]
                and (value.get("labels") or {}).get("pageInfo", {}).get("hasNextPage") is False
                and _relation(value, "closedByPullRequestsReferences", DELIVERY_PR),
                "canary incumbent assignment or delivery differs",
            )
            continue
        if number == DELIVERY_PR:
            _need(
                item["is_pr"]
                and value.get("id") == DELIVERY_NODE_ID
                and value.get("state") == "OPEN"
                and value.get("isDraft") is False
                and value.get("merged") is False
                and value.get("author") == {"login": OWNER_LOGIN, "databaseId": OWNER_ID}
                and _relation(value, "closingIssuesReferences", ISSUE),
                "canary delivery differs",
            )
            continue
        if item["is_pr"]:
            _need(
                value.get("state") in {"CLOSED", "MERGED"} and value.get("merged") is True,
                "extra open owned canary delivery",
            )
            _need(
                isinstance(value.get("closingIssuesReferences"), dict)
                and isinstance(value["closingIssuesReferences"].get("nodes"), list)
                and value["closingIssuesReferences"].get("pageInfo", {}).get("hasNextPage") is False
                and not any(
                    node.get("id") == ISSUE_NODE_ID
                    for node in value["closingIssuesReferences"]["nodes"]
                    if isinstance(node, dict)
                ),
                "unselected delivery pagination incomplete",
            )
            unselected.append({"number": number, "node_id": item["node_id"], "kind": "closed"})
        else:
            assignees = value.get("assignees") or {}
            labels = value.get("labels") or {}
            closures = value.get("closedByPullRequestsReferences") or {}
            _need(
                assignees.get("pageInfo", {}).get("hasNextPage") is False
                and labels.get("pageInfo", {}).get("hasNextPage") is False
                and isinstance(closures.get("nodes"), list)
                and closures.get("pageInfo", {}).get("hasNextPage") is False,
                "unselected issue pagination incomplete",
            )
            state = value.get("state")
            _need(state in {"OPEN", "CLOSED"}, "unselected state malformed")
            agent_label = any(
                str(label.get("name", "")).lower().startswith(("agent:", "agents:"))
                for label in labels.get("nodes", [])
                if isinstance(label, dict)
            )
            if state == "OPEN" and (assignees.get("nodes") or agent_label or closures.get("nodes")):
                _need(False, "extra open owned canary issue")
            unselected.append(
                {
                    "number": number,
                    "node_id": item["node_id"],
                    "kind": "closed" if state == "CLOSED" else "unselected_unknown",
                }
            )
    _need(ISSUE in discovered and DELIVERY_PR in discovered, "canary delivery chain absent")
    return sorted(unselected, key=lambda item: int(item["number"])), repository["p" + DELIVERY_PR]


def collect_confirmed_existing(*, reads, seed, docs, rows):
    """Return only a re-read, native-authenticated existing issue988 incumbent."""
    _case_seed(seed, docs)
    _need(
        isinstance(rows, list) and len(rows) == 2 and all(isinstance(row, list) for row in rows),
        "canary timelines missing",
    )
    target_rows, source_rows = rows
    target_refs, source_refs = _crossrefs(target_rows), _crossrefs(source_rows)
    _need(
        target_refs.get(ISSUE) == {"node_id": ISSUE_NODE_ID, "is_pr": False}
        and target_refs.get(DELIVERY_PR) == {"node_id": DELIVERY_NODE_ID, "is_pr": True},
        "canary target lineage differs",
    )
    discovered = dict(source_refs)
    discovered.update(target_refs)
    discovered.pop(TARGET_PR, None)
    discovered.pop(SOURCE_ISSUE, None)
    document, query = _snapshot(reads, discovered)
    unselected, delivery_snapshot = _validate_snapshot(document, discovered)

    issue_path = f"repos/{REPOSITORY}/issues/{ISSUE}"
    pull_path = f"repos/{REPOSITORY}/pulls/{DELIVERY_PR}"
    events_path = issue_path + "/events"
    issue = reads.get(issue_path)
    events = reads.pages(events_path)
    lineage, lineage_query, lineage_projection = _candidate_lineage(reads, docs[0])
    permission = reads.get(f"repos/{REPOSITORY}/collaborators/{OWNER_LOGIN}/permission")
    pull = reads.get(pull_path)
    identity = ownership._issue(issue, ISSUE)
    assignees = issue.get("assignees")
    _need(
        identity["node_id"] == ISSUE_NODE_ID
        and issue.get("state") == "open"
        and isinstance(assignees, list)
        and len(assignees) == 1
        and assignees[0].get("login") == OWNER_LOGIN
        and assignees[0].get("id") == OWNER_ID,
        "canary incumbent readback differs",
    )
    assignments = [
        event
        for event in events
        if event.get("event") in {"assigned", "unassigned"}
        and (event.get("assignee") or {}).get("login", "").lower() == OWNER_LOGIN
    ]
    assignments.sort(key=lambda event: (ownership._instant(event.get("created_at")), event["id"]))
    _need(
        assignments and assignments[-1].get("event") == "assigned",
        "canary incumbent assignment missing",
    )
    _need(
        permission.get("permission") in {"write", "maintain", "admin"}
        and (permission.get("user") or {}).get("login", "").lower() == OWNER_LOGIN
        and (permission.get("user") or {}).get("id") == OWNER_ID,
        "canary incumbent permission differs",
    )
    _need(
        pull.get("node_id") == DELIVERY_NODE_ID
        and str(pull.get("number")) == DELIVERY_PR
        and pull.get("state") == "open"
        and pull.get("merged") is False
        and pull.get("draft") is False
        and (pull.get("user") or {}).get("login") == OWNER_LOGIN
        and (pull.get("user") or {}).get("id") == OWNER_ID,
        "canary delivery readback differs",
    )
    import closer_coding_publication as publication

    head_repository = delivery_snapshot.get("headRepository") or {}
    _need(
        isinstance(head_repository, dict)
        and isinstance(delivery_snapshot.get("body"), str)
        and isinstance(delivery_snapshot.get("headRefName"), str)
        and isinstance(delivery_snapshot.get("headRefOid"), str)
        and isinstance(delivery_snapshot.get("baseRefOid"), str)
        and isinstance(head_repository.get("nameWithOwner"), str)
        and isinstance(head_repository.get("databaseId"), int),
        "canary delivery projection incomplete",
    )
    publication._owned_pr_projection(
        pull,
        {
            "pr_target": REPOSITORY + "#" + DELIVERY_PR,
            "head": delivery_snapshot["headRefOid"],
            "base": delivery_snapshot["baseRefOid"],
            "head_branch": delivery_snapshot["headRefName"],
            "head_repository": head_repository["nameWithOwner"],
            "body": delivery_snapshot["body"],
            "state": delivery_snapshot["state"],
            "draft": delivery_snapshot["isDraft"],
        },
        ISSUE_NODE_ID,
        SimpleNamespace(
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            writer_login=OWNER_LOGIN,
            writer_id=OWNER_ID,
        ),
    )

    _need(
        reads.get(issue_path) == issue
        and reads.pages(events_path) == events
        and reads.get("graphql", fields={"query": lineage_query}) == lineage
        and reads.get(f"repos/{REPOSITORY}/collaborators/{OWNER_LOGIN}/permission") == permission
        and reads.get(pull_path) == pull
        and reads.get("graphql", fields={"query": query}) == document,
        "canary incumbent changed during observation",
    )
    _need(
        [
            reads.get(f"repos/{REPOSITORY}/pulls/{TARGET_PR}"),
            reads.get(f"repos/{REPOSITORY}/issues/{SOURCE_ISSUE}"),
        ]
        == docs
        and [
            reads.pages(f"repos/{REPOSITORY}/issues/{number}/timeline")
            for number in (TARGET_PR, SOURCE_ISSUE)
        ]
        == rows,
        "canary seed subjects changed during observation",
    )
    return {
        "incumbent": {
            "issue": identity,
            "owner": OWNER_LOGIN,
            "assignment_ref": f"github:{REPOSITORY}/issues/{ISSUE}/events/{assignments[-1]['id']}",
            "lineage_ref": f"github:{REPOSITORY}/issues/{ISSUE}/timeline-sha256/"
            + contract.digest(lineage_projection),
        },
        "unselected_native_references": unselected,
    }
