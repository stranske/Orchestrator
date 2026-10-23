"""Read merge-bound verdicts posted by the fleet verifier, never CI status alone."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import verification_events

MARKER = "verifier-corpus-decision/v1"
MARKER_RE = re.compile(r"<!-- verifier-corpus-decision/v1 (\{[^\n]+\}) -->")
OBSERVATION_SCHEMA = "verifier-evidence-observation/v1"
STATE_MARKER = "verifier-event-state/v1"
STATE_MARKER_RE = re.compile(r"<!-- verifier-event-state/v1 (\{[^\n]+\}) -->")
SHA_RE = re.compile(r"[0-9a-f]{40}")
TRUSTED_AUTHORS = {"github-actions", "github-actions[bot]"}
VALID_PROVIDER_VERDICTS = {"PASS", "CONCERNS", "FAIL", "NON_PASS"}


def decision_matches_identity(decision: Any, repo: str, number: int, head: Any, merge: Any) -> bool:
    """Validate a parsed marker's exact delivery identity and verdict semantics.

    Author/comment provenance remains the responsibility of decision_from_pr;
    this predicate never turns a diagnostic artifact into a trusted marker.
    """
    if (
        not isinstance(decision, dict)
        or type(number) is not int
        or number < 1
        or not SHA_RE.fullmatch(str(head))
        or not SHA_RE.fullmatch(str(merge))
        or decision.get("schema") != MARKER
        or str(decision.get("repo") or "").lower() != repo.lower()
        or str(decision.get("pr") or "") != str(number)
        or decision.get("head_sha") != head
        or decision.get("evaluated_sha") != merge
    ):
        return False
    run, attempt = str(decision.get("run_id") or ""), str(decision.get("run_attempt") or "")
    providers = decision.get("provider_verdicts")
    if (
        not run.isdigit()
        or not attempt.isdigit()
        or int(run) < 1
        or int(attempt) < 1
        or not isinstance(providers, list)
        or not providers
        or len(providers) > 16
        or any(not isinstance(v, str) or v not in VALID_PROVIDER_VERDICTS for v in providers)
        or type(decision.get("ci_failed")) is not bool
    ):
        return False
    expected = (
        "PASS" if not decision["ci_failed"] and all(v == "PASS" for v in providers) else "NON_PASS"
    )
    if "verification_event" in decision and not verification_events.valid_event(
        decision["verification_event"], repo, number, head, merge
    ):
        return False
    return decision.get("verdict") == expected


def decision_from_pr(
    repo: str, pr: dict[str, Any], *, include_pending: bool = False
) -> dict[str, Any] | None:
    """Return the newest exact-identity decision from a trusted verifier comment."""
    number = pr.get("number")
    head = pr.get("headRefOid")
    merge = (pr.get("mergeCommit") or {}).get("oid")
    if not isinstance(number, int) or not SHA_RE.fullmatch(str(head)):
        return None
    if not SHA_RE.fullmatch(str(merge)):
        return None
    comments = (pr.get("comments") or {}).get("nodes") or []
    candidates = []
    states = []
    invalid_current_event = False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = (comment.get("author") or {}).get("login")
        if author not in TRUSTED_AUTHORS:
            continue
        url = str(comment.get("url") or "")
        if not url.lower().startswith(
            f"https://github.com/{repo.lower()}/pull/{number}#issuecomment-"
        ):
            continue
        for raw in STATE_MARKER_RE.findall(str(comment.get("body") or "")):
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if _valid_state(state, repo, number, head, merge):
                states.append(state)
            elif _same_subject(state, repo, number, head, merge):
                invalid_current_event = True
        for raw in MARKER_RE.findall(str(comment.get("body") or "")):
            try:
                decision = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not decision_matches_identity(decision, repo, number, head, merge):
                if (
                    isinstance(decision, dict)
                    and "verification_event" in decision
                    and _same_subject(decision, repo, number, head, merge)
                ):
                    invalid_current_event = True
                continue
            run, attempt = str(decision["run_id"]), str(decision["run_attempt"])
            candidates.append((int(run), int(attempt), decision))
    if invalid_current_event:
        return unusable_observation(repo, number, head, merge) if include_pending else None
    if not candidates and not states:
        return None
    records = [_public_record(item[2]) for item in candidates]
    states = [_public_record(state) for state in states]
    selected = applicable_decision(records, states, include_pending=include_pending)
    if selected is None:
        return unusable_observation(repo, number, head, merge) if include_pending else None
    return {**selected, "_lifecycle_history": [*records, *states]}


def unusable_observation(repo, number, head, merge):
    return {
        "schema": OBSERVATION_SCHEMA,
        "repo": repo.lower(),
        "pr": str(number),
        "head_sha": head,
        "evaluated_sha": merge,
        "status": "unusable",
        "reason": "trusted current event is malformed, orphaned or causally conflicting",
    }


def is_unusable_observation(record, repo, number):
    return (
        isinstance(record, dict)
        and record.get("schema") == OBSERVATION_SCHEMA
        and record.get("repo") == repo.lower()
        and record.get("pr") == str(number)
        and record.get("status") == "unusable"
        and SHA_RE.fullmatch(str(record.get("head_sha")))
        and SHA_RE.fullmatch(str(record.get("evaluated_sha")))
    )


def _public_record(record):
    """Strip untrusted incidental fields before retaining or projecting a marker."""
    result = {
        key: record[key]
        for key in (
            "schema",
            "repo",
            "pr",
            "head_sha",
            "evaluated_sha",
            "run_id",
            "run_attempt",
            "verdict",
            "ci_failed",
            "provider_verdicts",
            "status",
            "predecessor_event_id",
            "assessment_identity",
            "verification_event",
            "retry_kind",
            "retryable",
            "failure_cause",
            "execution_binding",
        )
        if key in record
    }
    if "verification_event" in result:
        result["verification_event"] = {
            key: result["verification_event"][key]
            for key in (*verification_events.CORE, "event_id")
        }
    return result


def _same_subject(record, repo, number, head, merge):
    return (
        isinstance(record, dict)
        and str(record.get("repo", "")).lower() == repo.lower()
        and str(record.get("pr")) == str(number)
        and record.get("head_sha") == head
        and record.get("evaluated_sha") == merge
    )


def _valid_state(state, repo, number, head, merge):
    return (
        isinstance(state, dict)
        and state.get("schema") == STATE_MARKER
        and state.get("repo") == repo.lower()
        and str(state.get("pr")) == str(number)
        and state.get("head_sha") == head
        and state.get("evaluated_sha") == merge
        and state.get("status") in {"pending", "incomplete"}
        and str(state.get("run_id", "")).isdigit()
        and str(state.get("run_attempt", "")).isdigit()
        and int(state["run_id"]) > 0
        and int(state["run_attempt"]) > 0
        and verification_events.valid_event(
            state.get("verification_event"), repo, number, head, merge
        )
    )


def applicable_decision(decisions, states=(), *, include_pending=False):
    """Choose a causal event, then the latest attempt *within that event*.

    A newer workflow ID cannot make an unrelated evidence event current. Pending,
    incomplete, orphaned or competing successor events cannot supply a PASS.
    All input records here must already have trusted author/subject provenance.
    """
    legacy = [d for d in decisions if "verification_event" not in d]
    baseline = max(legacy, key=_attempt) if legacy else None
    records = [d for d in [*decisions, *states] if "verification_event" in d]
    if not records:
        return baseline
    nodes = {}
    parents = {}
    for record in records:
        identity = record["verification_event"]["event_id"]
        parent = record.get("predecessor_event_id")
        if parent is not None and not verification_events.HEX256.fullmatch(str(parent)):
            return None
        if identity in parents and parents[identity] != parent:
            return None
        parents[identity] = parent
        prior = nodes.get(identity)
        if prior is None or _attempt(record) > _attempt(prior):
            nodes[identity] = record
        elif _attempt(record) == _attempt(prior):
            # Incomplete publication/execution cannot resurrect a nominal PASS.
            if record.get("status") == "incomplete":
                if prior.get("status") == "incomplete" and record != prior:
                    return None
                nodes[identity] = record
            elif prior.get("status") == "incomplete":
                continue
            # A terminal record completes a started marker for the same attempt.
            elif "verdict" in record and "verdict" not in prior:
                nodes[identity] = record
            elif "verdict" in record and "verdict" in prior and record != prior:
                return None
    current = verification_events.legacy_id(baseline) if baseline else None
    visited = set()
    while True:
        children = [identity for identity, parent in parents.items() if parent == current]
        if not children:
            break
        if len(children) != 1 or children[0] in visited:
            return None
        current = children[0]
        visited.add(current)
    if len(visited) != len(nodes):
        return None  # Missing causal predecessor, cycle, stale root, or concurrent branch.
    latest = nodes[current] if visited else baseline
    return (
        latest
        if latest and (include_pending or latest.get("verdict") in {"PASS", "NON_PASS"})
        else None
    )


def _attempt(record):
    return int(record["run_id"]), int(record["run_attempt"])


def fetch_decisions(
    repo: str, numbers: list[int], *, include_pending: bool = False
) -> dict[int, dict[str, Any]]:
    """Fetch recent comments in GraphQL batches; an incomplete read stays unknown."""
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return {}
    found: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(numbers), 20):
        chunk = numbers[offset : offset + 20]
        aliases = " ".join(
            f"p{int(number)}:pullRequest(number:{int(number)}){{number headRefOid "
            "mergeCommit{oid} comments(last:100){nodes{body url author{login}} "
            "pageInfo{hasPreviousPage}}}"
            for number in chunk
        )
        query = f'query {{repository(owner:"{owner}",name:"{name}"){{{aliases}}}}}'
        try:
            try:
                import gh_capacity

                gh_capacity.throttle_if_enabled("graphql")
            except Exception:
                pass  # Evidence remains unknown if the optional throttle is unavailable.
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode:
            continue
        try:
            data = json.loads(result.stdout)["data"]["repository"]
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for pr in data.values():
            if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                continue
            if (pr.get("comments") or {}).get("pageInfo", {}).get("hasPreviousPage"):
                continue  # A truncated comment history cannot prove an absent or latest marker.
            decision = decision_from_pr(repo, pr, include_pending=include_pending)
            if decision:
                found[pr["number"]] = decision
    return found


def record_decision(run_id: str, repo: str, number: int, decision: dict[str, Any]) -> bool:
    """Retain authenticated event history and patch only its applicable outcome.

    The caller supplies its already-resolved delivery-to-run attribution. This
    method does not infer ownership or treat a report's own repo field as trust.
    """
    if is_unusable_observation(decision, repo, number):
        import feedback

        with feedback._conn() as conn:
            previous = conn.execute(
                "SELECT verifier_verdict FROM outcomes WHERE run_id=?", (run_id,)
            ).fetchone()
            conn.execute("UPDATE outcomes SET verifier_verdict=NULL WHERE run_id=?", (run_id,))
            return bool(previous and previous[0] is not None)
    valid = decision_matches_identity(
        decision, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
    )
    if not valid and not _valid_state(
        decision, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
    ):
        return False
    import feedback

    ancestry = decision.get("_lifecycle_history", [])
    current = _public_record(decision)
    if not isinstance(ancestry, list) or len(ancestry) > 1000:
        return False
    for ancestor in ancestry:
        if not isinstance(ancestor, dict) or not (
            decision_matches_identity(
                ancestor, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
            )
            or _valid_state(
                ancestor, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
            )
        ):
            return False
    incoming = [_public_record(row) for row in ancestry] + [current]
    decision = current
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM completion_events WHERE run_id=? AND producer=? AND event_type='verification'",
            (run_id, "verifier_evidence"),
        ).fetchall()
        history = []
        for row in rows:
            try:
                retained = json.loads(row[0])["verification"]["decision"]
            except (ValueError, KeyError, TypeError):
                return False
            if not (
                decision_matches_identity(
                    retained, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
                )
                or _valid_state(
                    retained, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
                )
            ):
                return False
            history.append(retained)
        additions = []
        for candidate in incoming:
            same = [
                d
                for d in history
                if _attempt(d) == _attempt(candidate)
                and ("verdict" in d) == ("verdict" in candidate)
                and d.get("status") == candidate.get("status")
            ]
            if same and any(d != candidate for d in same):
                return False  # No imports/projections on an immutable attempt conflict.
            if not same:
                history.append(candidate)
                additions.append(candidate)
        for candidate in additions:
            identifier = verification_events.content_hash(
                {
                    "run_id": run_id,
                    "repo": repo.lower(),
                    "pr": str(number),
                    "workflow_run": str(candidate["run_id"]),
                    "attempt": str(candidate["run_attempt"]),
                    "record_kind": "terminal" if "verdict" in candidate else candidate["status"],
                }
            )
            feedback._record_completion_event_in_conn(
                conn,
                run_id,
                event_type="verification",
                phase="verification",
                producer="verifier_evidence",
                attempt_id=f"{candidate['run_id']}:{candidate['run_attempt']}",
                event_id=identifier,
                status=(
                    "succeeded"
                    if "verdict" in candidate
                    else "pending" if candidate.get("status") == "pending" else "failed"
                ),
                payload={"verification": {"decision": candidate}},
            )
        current = applicable_decision(
            [d for d in history if "verdict" in d], [d for d in history if "verdict" not in d]
        )
        if current is None:
            # An ambiguous fresh event must not leave an old PASS in the scalar
            # current-status projection. Original verdict events remain retained.
            conn.execute("UPDATE outcomes SET verifier_verdict=NULL WHERE run_id=?", (run_id,))
            return False
        previous = conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id=?", (run_id,)
        ).fetchone()
        if previous and previous[0] == current["verdict"]:
            return False
        feedback._record_outcome_in_conn(conn, run_id, verifier_verdict=current["verdict"])
        return True


def fetch_lifecycle(repo: str, numbers: list[int]) -> dict[int, dict[str, Any]]:
    """Include authenticated pending/incomplete current events for status projection."""
    return fetch_decisions(repo, numbers, include_pending=True)
