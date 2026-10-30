"""Bounded native reads for a correction's known PR/source ownership neighborhood.

This observes GitHub data; it grants no effect permission. Incomplete pagination,
ambiguous lineage, races, and unsupported connected events all hold UNKNOWN.
It deliberately does not search repository-wide legacy issue history.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone

import closer_correction_classification as contract

PAGE_SIZE = 15
MAX_ITEMS = 400
MAX_PAGES = (MAX_ITEMS + PAGE_SIZE - 1) // PAGE_SIZE
MAX_CALLS = 32
MAX_REPLY_BYTES = 131072
MAX_TOTAL_BYTES = 1048576
_TOKEN = object()


class OwnershipUnavailable(ValueError):
    pass


def _need(ok, reason):
    if not ok:
        raise OwnershipUnavailable(reason)


def _instant(value):
    _need(isinstance(value, str), "native event time unavailable")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _need(stamp.tzinfo is not None, "native event time must be zoned")
    return stamp.timestamp()


def _cross_projection(row, pull, repo):
    issue = row["source"]["issue"]
    _need(
        str(issue["number"]) == str(pull["number"]) and "pull_request" in issue,
        "native PR lineage differs",
    )
    return {
        "schema": "closer-correction-native-crossref/v1",
        "event": "cross-referenced",
        "actor_id": row["actor"]["id"],
        "created_at": row["created_at"],
        "source_issue_id": issue["id"],
        "source_node_id": issue["node_id"],
        "source_repository": repo,
        "source_pr": str(pull["number"]),
        "source_head_sha": pull["head"]["sha"],
    }


def _raw(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class NativeOwnership:
    """Immutable same-process observation, not a remote attestation or caller flag."""

    __slots__ = ("_raw", "_token")

    def __init__(self, *, _token, document):
        _need(_token is _TOKEN, "native ownership must come from collector")
        self._raw = _raw(document)
        self._token = _token

    def document(self):
        _need(self._token is _TOKEN, "native ownership origin missing")
        return json.loads(self._raw)


class _Reads:
    def __init__(self, github, deadline):
        import closer_dispatcher as closer

        _need(type(github) is closer.GitHub, "native GitHub reader required")
        self.github = closer.GitHub(
            deadline=min(float(github.deadline), float(deadline)), api_fn=github.api_fn
        )
        self.calls = 0
        self.bytes = 0
        self.refs = []

    def get(self, endpoint, *, fields=None):
        _need(self.calls < MAX_CALLS, "native ownership call budget exhausted")
        self.calls += 1
        value = self.github.api(
            endpoint,
            **({"method": "POST", "fields": fields, "resource": "graphql"} if fields else {}),
        )
        raw = _raw(value)
        self.bytes += len(raw)
        _need(
            len(raw) <= MAX_REPLY_BYTES and self.bytes <= MAX_TOTAL_BYTES,
            "native ownership response budget exhausted",
        )
        self.refs.append("github:" + endpoint + "#sha256=" + hashlib.sha256(raw).hexdigest())
        return value

    def pages(self, endpoint):
        found, identities = [], set()
        for page in range(1, MAX_PAGES + 1):
            rows = self.get(endpoint + f"?per_page={PAGE_SIZE}&page={page}")
            _need(isinstance(rows, list) and len(rows) <= PAGE_SIZE, "native page malformed")
            for row in rows:
                _need(isinstance(row, dict), "native event malformed")
                # A cross-reference lacks a numeric event ID in the REST shape.
                if row.get("event") == "cross-referenced":
                    source = row.get("source", {})
                    issue = source.get("issue", {})
                    _need(
                        source.get("type") == "issue"
                        and type(issue.get("id")) is int
                        and issue["id"] > 0
                        and issue.get("node_id")
                        and (row.get("actor") or {}).get("id")
                        and row.get("created_at"),
                        "native cross-reference incomplete",
                    )
                    _need(
                        type(row["actor"]["id"]) is int and row["actor"]["id"] > 0,
                        "native cross-reference actor incomplete",
                    )
                    _instant(row["created_at"])
                    identity = contract.digest(
                        {
                            "actor_id": row["actor"]["id"],
                            "created_at": row["created_at"],
                            "source_issue_id": issue["id"],
                            "source_node_id": issue["node_id"],
                        }
                    )
                elif row.get("event") == "committed":
                    _need(
                        re.fullmatch(r"[a-f0-9]{40}", str(row.get("sha"))) and row.get("node_id"),
                        "native committed identity missing",
                    )
                    identity = "committed:" + row["sha"] + ":" + row["node_id"]
                else:
                    _need(
                        type(row.get("id")) is int and row["id"] > 0,
                        "native event identity missing",
                    )
                    identity = str(row["id"])
                _need(identity not in identities, "native pagination overlaps")
                _need(
                    row.get("event") not in {"connected", "disconnected"},
                    "native connected lineage needs independent graph join",
                )
                identities.add(identity)
                _need(len(found) < MAX_ITEMS, "native ownership item budget exhausted")
                found.append(row)
            if len(rows) < PAGE_SIZE:
                return found
        raise OwnershipUnavailable("native pagination incomplete")


def _issue(row, number):
    _need(
        isinstance(row, dict)
        and str(row.get("number")) == str(number)
        and row.get("node_id")
        and isinstance(row.get("title"), str)
        and isinstance(row.get("body"), (str, type(None)))
        and row.get("state") in {"open", "closed"}
        and isinstance(row.get("assignees"), list)
        and isinstance(row.get("labels"), list),
        "native issue incomplete",
    )
    return {
        "number": str(number),
        "node_id": row["node_id"],
        "body_sha256": hashlib.sha256((row["body"] or "").encode()).hexdigest(),
    }


def _pull(row, seed):
    target = seed["target"]
    _need(
        isinstance(row, dict)
        and str(row.get("number")) == target["pr"]
        and row.get("node_id")
        and row.get("state") in {"open", "closed"}
        and (row.get("head") or {}).get("sha") == target["head_sha"]
        and (row.get("base") or {}).get("sha") == target["base_sha"]
        and row.get("merge_commit_sha") == target["merge_sha"],
        "native PR identity changed",
    )


def _related(rows, repository):
    related = {}
    for row in rows:
        if row.get("event") != "cross-referenced":
            continue
        issue = row["source"]["issue"]
        _need(
            (issue.get("repository") or {}).get("full_name", "").lower() == repository,
            "foreign native lineage",
        )
        _need(
            type(issue.get("number")) is int and issue["number"] > 0,
            "native lineage number missing",
        )
        key = str(issue["number"])
        _need(
            "pull_request" not in issue or isinstance(issue["pull_request"], dict),
            "native linked PR type malformed",
        )
        projection = (issue["node_id"], "pull_request" in issue)
        _need(key not in related or related[key] == projection, "ambiguous native lineage")
        related[key] = projection
    return related


def _owned_delivery_neighbor(reads, repo, candidate, owner, permission, neighbors, source):
    """Join one delivery and at most one owned, completely nonclosing reference."""
    from types import SimpleNamespace

    import closer_coding_publication as publication

    _need(1 <= len(neighbors) <= 2, "additional related PR unresolved")
    login, name = repo.split("/")
    delivery_count = 0
    for number, (node_id, is_pr) in neighbors.items():
        _need(is_pr, "additional related PR unresolved")
        # Both complete relation directions are native, not inferred from body text.
        query = (
            "query {repository(owner:"
            + json.dumps(login)
            + ",name:"
            + json.dumps(name)
            + "){databaseId nameWithOwner issue(number:"
            + str(candidate["number"])
            + "){id number closedByPullRequestsReferences(first:2){nodes{id number "
            + "repository{nameWithOwner}} pageInfo{hasNextPage}}} pullRequest(number:"
            + number
            + "){id number state isDraft merged headRefOid baseRefOid headRefName "
            + "headRepository{nameWithOwner} body closingIssuesReferences(first:2){nodes{id "
            + "number repository{nameWithOwner}} pageInfo{hasNextPage}}}}}"
        )
        if len(neighbors) == 2:
            original = (
                " original:issue(number:"
                + str(source["number"])
                + "){id number closedByPullRequestsReferences(first:2){nodes{id number "
                + "repository{nameWithOwner}} pageInfo{hasNextPage}}}"
            )
            query = query[:-2] + original + query[-2:]
        fields = {"query": query}
        document = reads.get("graphql", fields=fields)
        repository = document["data"]["repository"]
        issue, pr = repository["issue"], repository["pullRequest"]
        closes, deliveries = pr["closingIssuesReferences"], issue["closedByPullRequestsReferences"]
        if len(neighbors) == 2:
            original = repository.get("original")
            _need(isinstance(original, dict), "original source closing relation unresolved")
            _need(
                original["id"] == source["node_id"]
                and str(original["number"]) == source["number"]
                and original["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"] is False
                and original["closedByPullRequestsReferences"]["nodes"] == [],
                "original source closing relation unresolved",
            )
        _need(
            repository["nameWithOwner"].lower() == repo
            and issue["id"] == candidate["node_id"]
            and issue["number"] == candidate["number"]
            and pr["id"] == node_id
            and str(pr["number"]) == number
            and closes["pageInfo"]["hasNextPage"] is False
            and deliveries["pageInfo"]["hasNextPage"] is False
            and len(deliveries["nodes"]) == 1
            and any(
                deliveries["nodes"]
                == [
                    {
                        "id": known_node,
                        "number": int(known_number),
                        "repository": {"nameWithOwner": repository["nameWithOwner"]},
                    }
                ]
                for known_number, (known_node, _) in neighbors.items()
            ),
            "unique reciprocal owned delivery unavailable",
        )
        pull_path = f"repos/{repo}/pulls/{number}"
        pull = reads.get(pull_path)
        snapshot = {
            "pr_target": repo + "#" + number,
            "head": pr["headRefOid"],
            "base": pr["baseRefOid"],
            "head_branch": pr["headRefName"],
            "head_repository": pr["headRepository"]["nameWithOwner"],
            "body": pr["body"],
            "state": pr["state"],
            "draft": pr["isDraft"],
        }
        config = SimpleNamespace(
            repository=repo,
            repository_id=repository["databaseId"],
            writer_login=owner,
            writer_id=permission["user"]["id"],
        )
        if deliveries["nodes"][0]["id"] == node_id:
            _need(pr["merged"] is False, "owned delivery already merged")
            _need(
                closes["nodes"]
                == [
                    {
                        "id": candidate["node_id"],
                        "number": candidate["number"],
                        "repository": {"nameWithOwner": repository["nameWithOwner"]},
                    }
                ],
                "unique reciprocal owned delivery unavailable",
            )
            publication._owned_pr_projection(pull, snapshot, candidate["node_id"], config)
            delivery_count += 1
        else:
            head, base, author = (
                pull.get("head") or {},
                pull.get("base") or {},
                pull.get("user") or {},
            )
            _need(
                closes["nodes"] == []
                and pull.get("node_id") == node_id
                and str(pull.get("number")) == number
                and author.get("login") == owner
                and author.get("id") == config.writer_id
                and type(config.writer_id) is int
                and config.writer_id > 0
                and type(config.repository_id) is int
                and config.repository_id > 0
                and head.get("ref") not in {base.get("ref"), publication.ctl.BRANCH}
                and isinstance(head.get("ref"), str)
                and bool(head["ref"])
                and (head.get("repo") or {}).get("id") == config.repository_id
                and (base.get("repo") or {}).get("id") == config.repository_id
                and (head.get("repo") or {}).get("full_name", "").lower() == repo
                and (base.get("repo") or {}).get("full_name", "").lower() == repo
                and head.get("sha") == pr["headRefOid"]
                and base.get("sha") == pr["baseRefOid"]
                and all(
                    re.fullmatch(r"[a-f0-9]{40}", str(oid))
                    for oid in (head.get("sha"), base.get("sha"))
                )
                and head.get("ref") == pr["headRefName"]
                and pr["headRepository"]["nameWithOwner"].lower() == repo
                and pull.get("body") == pr["body"]
                and (
                    (
                        pull.get("state") == "open"
                        and pr["state"] == "OPEN"
                        and pull.get("merged") is False
                        and pr["merged"] is False
                    )
                    or (
                        pull.get("state") == "closed"
                        and pr["state"] == "MERGED"
                        and pull.get("merged") is True
                        and pr["merged"] is True
                    )
                )
                and pull.get("draft") is False
                and pr["isDraft"] is False,
                "informational PR native ownership or empty closing relation unresolved",
            )
        _need(
            reads.get(pull_path) == pull and reads.get("graphql", fields=fields) == document,
            "owned delivery changed during observation",
        )

    _need(delivery_count == 1, "unique reciprocal owned delivery unavailable")


def _observation(seed, incumbent, reads, observed, *, unselected=None):
    repo = seed["target"]["repository"]
    scope = {
        "target": seed["target"],
        "source_issue": seed["source_issue"],
        "finding_evidence": seed["finding"]["evidence"],
        "scope": seed["scope"],
    }
    effect = {
        "schema": "closer-correction-effect/v1",
        "repository": repo,
        "event_id": seed["original_verifier"]["verification_event"]["event_id"],
        "finding_evidence": seed["finding"]["evidence"],
        "source_issue": seed["source_issue"],
        "scope": seed["scope"],
    }
    wire = {
        "schema": "closer-correction-ownership/v1",
        "kind": "no_incumbent" if incumbent.get("kind") == "no_incumbent" else "existing",
        "seed_effect_key": contract.digest(effect),
        "source_scope_digest": contract.digest(scope),
        "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
        "evidence_refs": reads.refs,
    }
    if wire["kind"] == "existing":
        wire["incumbent"] = incumbent
    document = {
        "ownership": wire,
        "incumbent": incumbent,
        "seed_sha256": contract.digest(seed),
        "calls": reads.calls,
        "bytes": reads.bytes,
        "remote_authenticated": False,
    }
    if unselected is not None:
        document["unselected_native_references"] = unselected
    return NativeOwnership(_token=_TOKEN, document=document)


def collect_ownership(*, github, seed, deadline, now=None):
    """Read complete bounded known-subject timelines and re-read them for races.

    An empty search result is never used as absence proof. Existing ownership
    additionally needs native assignment events, PR lineage and write permission.
    Unsupported topology holds rather than inventing no-incumbent evidence.
    """
    observed = time.time() if now is None else float(now)
    _need(abs(observed - time.time()) <= 2, "native observation clock is not current")
    reads = _Reads(github, deadline)
    repo, pr = seed["target"]["repository"], seed["target"]["pr"]
    _need(
        re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repo) is not None, "native repository malformed"
    )
    source = seed["source_issue"]["number"]
    _need(str(pr).isdigit() and str(source).isdigit(), "native subjects malformed")
    paths = [f"repos/{repo}/pulls/{pr}", f"repos/{repo}/issues/{source}"]
    docs = [reads.get(p) for p in paths]
    _pull(docs[0], seed)
    _need(_issue(docs[1], source) == seed["source_issue"], "native source changed")
    timelines = [f"repos/{repo}/issues/{n}/timeline" for n in (pr, source)]
    rows = [reads.pages(p) for p in timelines]
    if repo == "stranske/orchestrator" and str(pr) == "984" and str(source) == "983":
        # This closed Source-owned canary identity can authenticate only its
        # existing assigned delivery. Other references remain unselected facts;
        # this branch never produces absence or authority for their work.
        _need(
            not docs[1]["assignees"]
            and not any(
                str(label.get("name", "") if isinstance(label, dict) else label)
                .lower()
                .startswith(("agent:", "agents:"))
                for label in docs[1]["labels"]
            ),
            "source agent ownership unresolved",
        )
        from closer_correction_canary_ownership import collect_confirmed_existing

        confirmed = collect_confirmed_existing(reads=reads, seed=seed, docs=docs, rows=rows)
        # The fixed helper already re-read every original and selected subject;
        # repeating those reads would spend the same finite observation budget.
        return _observation(
            seed,
            confirmed["incumbent"],
            reads,
            observed,
            unselected=confirmed["unselected_native_references"],
        )
    neighbors = _related(rows[0] + rows[1], repo)
    _need(neighbors.get(pr) == (docs[0]["node_id"], True), "original PR lineage absent")
    neighbors.pop(pr)
    source_link = neighbors.pop(source, None)
    _need(
        source_link is None or source_link == (seed["source_issue"]["node_id"], False),
        "native linked source node differs",
    )
    delivery_neighbors = {key: value for key, value in neighbors.items() if value[1]}
    neighbors = {key: value for key, value in neighbors.items() if not value[1]}
    _need(len(neighbors) <= 1, "multiple incumbent candidates")
    _need(not delivery_neighbors or len(neighbors) == 1, "additional related PR unresolved")
    # An assignment on the original source cannot be silently relabeled absence.
    _need(
        not any(
            str(label.get("name", "") if isinstance(label, dict) else label)
            .lower()
            .startswith(("agent:", "agents:"))
            for label in docs[1]["labels"]
        ),
        "source agent ownership unresolved",
    )
    _need(not docs[1]["assignees"], "source assignment requires existing owner review")
    incumbent = {"kind": "no_incumbent"}
    if neighbors:
        number, (node, _) = next(iter(neighbors.items()))
        issue_path = f"repos/{repo}/issues/{number}"
        candidate = reads.get(issue_path)
        identity = _issue(candidate, number)
        _need(
            identity["node_id"] == node
            and candidate["state"] == "open"
            and "pull_request" not in candidate
            and len(candidate["assignees"]) == 1,
            "incumbent identity/assignment unresolved",
        )
        owner = candidate["assignees"][0].get("login")
        _need(
            isinstance(owner, str) and re.fullmatch(r"[A-Za-z0-9-]+", owner),
            "incumbent owner malformed",
        )
        event_path = issue_path + "/events"
        timeline_path = issue_path + "/timeline"
        events, lineage = reads.pages(event_path), reads.pages(timeline_path)
        assigned = [
            e
            for e in events
            if e.get("event") in {"assigned", "unassigned"}
            and (e.get("assignee") or {}).get("login", "").lower() == owner.lower()
        ]
        assigned.sort(key=lambda event: (_instant(event.get("created_at")), event["id"]))
        _need(assigned and assigned[-1]["event"] == "assigned", "fresh assignment proof missing")
        matching = [
            e
            for e in lineage
            if e.get("event") == "cross-referenced"
            and str(e["source"]["issue"].get("number")) == pr
        ]
        _need(
            matching and _related(lineage, repo).get(pr) == (docs[0]["node_id"], True),
            "incumbent PR lineage missing",
        )
        permission = reads.get(f"repos/{repo}/collaborators/{owner}/permission")
        _need(
            permission.get("permission") in {"write", "maintain", "admin"}
            and (permission.get("user") or {}).get("login", "").lower() == owner.lower(),
            "incumbent write ownership unknown",
        )
        if delivery_neighbors:
            _owned_delivery_neighbor(
                reads, repo, candidate, owner, permission, delivery_neighbors, seed["source_issue"]
            )
        incumbent = {
            "issue": identity,
            "owner": owner,
            "assignment_ref": f"github:{repo}/issues/{number}/events/{assigned[-1]['id']}",
            "lineage_ref": f"github:{repo}/issues/{number}/timeline-sha256/"
            + contract.digest(_cross_projection(matching[0], docs[0], repo)),
        }
        _need(
            reads.get(issue_path) == candidate
            and reads.pages(event_path) == events
            and reads.pages(timeline_path) == lineage
            and reads.get(f"repos/{repo}/collaborators/{owner}/permission") == permission,
            "incumbent changed during observation",
        )
    _need(
        [reads.get(p) for p in paths] == docs and [reads.pages(p) for p in timelines] == rows,
        "native subjects changed during observation",
    )
    return _observation(seed, incumbent, reads, observed)
