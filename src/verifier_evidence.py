"""Read merge-bound verdicts posted by the fleet verifier, never CI status alone."""

from __future__ import annotations

import hashlib
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


def _valid_request_correlation(record):
    if "verification_request_basis" not in record:
        return True
    value = record["verification_request_basis"]
    return isinstance(value, str) and verification_events.HEX256.fullmatch(value) is not None


def _valid_result_admission(record):
    admission = record.get("result_admission")
    assessment = record.get("assessment_identity")
    return (
        isinstance(admission, dict)
        and admission.get("schema") == "verifier-result-admission/v1"
        and admission.get("mode") in ("evaluate", "compare")
        and (not isinstance(assessment, dict) or admission.get("mode") == assessment.get("mode"))
        and all(
            isinstance(admission.get(key), str)
            and verification_events.HEX256.fullmatch(admission[key])
            for key in ("payload_sha256", "execution_receipt_sha256")
        )
    )


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
    if "result_admission" in decision and not _valid_result_admission(decision):
        return False
    return decision.get("verdict") == expected


def _request_correlations(comments, selected, candidates, repo, number, head, merge):
    """Authenticate inert aliases ONLY after normal terminal conflict selection."""
    if not isinstance(selected, dict) or "verdict" not in selected:
        return []
    event = selected.get("verification_event")
    assessment = selected.get("assessment_identity")
    if not (
        verification_events.valid_event(event, repo, number, head, merge)
        and isinstance(assessment, dict)
        and assessment.get("canonical") is True
        and assessment.get("mode") in {"evaluate", "compare"}
        and all(isinstance(assessment.get(key), str) for key in ("provider", "model", "model2"))
    ):
        return []
    prefix = f"https://github.com/{repo.lower()}/pull/{number}#issuecomment-"
    exact_url = re.compile(re.escape(prefix) + r"[1-9][0-9]*")
    references = {
        (url, verification_events.content_hash(raw))
        for raw, url in candidates
        if _public_record(raw) == selected and exact_url.fullmatch(url.lower())
    }
    aliases = []
    pattern = re.compile(r"<!-- verifier-request-correlation/v1 (\{[^\n]+\}) -->")
    prefix = f"https://github.com/{repo.lower()}/pull/{number}#issuecomment-"
    for comment in comments:
        if (
            not isinstance(comment, dict)
            or (comment.get("author") or {}).get("login") not in TRUSTED_AUTHORS
            or not exact_url.fullmatch(str(comment.get("url", "")).lower())
        ):
            continue
        for raw in pattern.findall(str(comment.get("body") or "")):
            try:
                alias = json.loads(raw)
                if not (
                    isinstance(alias, dict)
                    and alias.get("schema") == "verifier-request-correlation/v1"
                    and "verdict" not in alias
                    and alias.get("repo") == repo.lower()
                    and alias.get("pr") == str(number)
                    and alias.get("head_sha") == head
                    and alias.get("evaluated_sha") == merge
                    and _valid_request_correlation(alias)
                    and "verification_request_basis" in alias
                    and alias.get("verification_event") == event
                    and alias.get("assessment_identity") == assessment
                    and isinstance(alias.get("result_run_id"), str)
                    and alias["result_run_id"] == str(selected.get("run_id"))
                    and isinstance(alias.get("result_run_attempt"), str)
                    and alias["result_run_attempt"] == str(selected.get("run_attempt"))
                    and (alias.get("result_comment_url"), alias.get("result_decision_sha256"))
                    in references
                    and all(
                        isinstance(alias.get(key), str)
                        and re.fullmatch(r"[1-9][0-9]{0,19}", alias[key])
                        for key in ("correlation_run_id", "correlation_run_attempt")
                    )
                ):
                    continue
                aliases.append(
                    {
                        "record": alias,
                        "source_url": comment["url"],
                        "sha256": verification_events.content_hash(alias),
                    }
                )
            except (ValueError, TypeError, KeyError):
                continue
    return sorted(
        aliases,
        key=lambda item: (
            int(item["record"]["correlation_run_id"]),
            int(item["record"]["correlation_run_attempt"]),
            int(item["source_url"].rsplit("issuecomment-", 1)[-1]),
        ),
    )


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
    terminal_sources = []
    state_sources = []
    states = []
    unorderable_invalid_event = False
    invalid_observations = []
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
                state_sources.append((state, url))
            elif _same_subject(state, repo, number, head, merge):
                observation = _invalid_observation(state, repo, number, head, merge, url, raw)
                if observation is None:
                    unorderable_invalid_event = True
                else:
                    invalid_observations.append(observation)
        for raw in MARKER_RE.findall(str(comment.get("body") or "")):
            try:
                decision = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not decision_matches_identity(decision, repo, number, head, merge):
                if (
                    isinstance(decision, dict)
                    and ("verification_event" in decision or "result_admission" in decision)
                    and _same_subject(decision, repo, number, head, merge)
                ):
                    observation = _invalid_observation(
                        decision, repo, number, head, merge, url, raw
                    )
                    if observation is None:
                        unorderable_invalid_event = True
                    else:
                        invalid_observations.append(observation)
                continue
            run, attempt = str(decision["run_id"]), str(decision["run_attempt"])
            candidates.append((int(run), int(attempt), decision))
            terminal_sources.append((decision, url))
    if unorderable_invalid_event:
        return unusable_observation(repo, number, head, merge) if include_pending else None
    if not candidates and not states and not invalid_observations:
        return None
    records = [_public_record(item[2]) for item in candidates]
    states = [_public_record(state) for state in [*states, *invalid_observations]]
    selected = applicable_decision(records, states, include_pending=include_pending)
    if selected is None:
        return (
            {
                **unusable_observation(repo, number, head, merge),
                "_lifecycle_history": [*records, *states],
            }
            if include_pending
            else None
        )
    aliases = _request_correlations(comments, selected, terminal_sources, repo, number, head, merge)
    # Correlation is projected AFTER semantic conflict/lifecycle selection.
    # Invalid or conflicting optional metadata disables this bridge only.
    selected_sources = [
        raw
        for raw, _url in [*terminal_sources, *state_sources]
        if _public_record(raw) == selected and "verification_request_basis" in raw
    ]
    invalid_correlation = any(not _valid_request_correlation(raw) for raw in selected_sources)
    bases = {
        raw["verification_request_basis"]
        for raw in selected_sources
        if _valid_request_correlation(raw)
    }
    inert = (
        {"_request_correlation_unusable": True}
        if invalid_correlation or len(bases) > 1
        else {"verification_request_basis": next(iter(bases))} if bases else {}
    )
    return {
        **selected,
        "_lifecycle_history": [*records, *states],
        **inert,
        **({"_request_correlations": aliases} if aliases else {}),
    }


def terminal_origin_from_pr(repo, pr, decision):
    """Project the actual selected bot comment separately from immutable findings.

    This records local authenticated-read provenance, not remote correction authority.
    Absence/partial inventory never supplies a comment or decision identity.
    """
    import hashlib

    if (
        not isinstance(decision, dict)
        or decision.get("verdict") != "NON_PASS"
        or (pr.get("comments") or {}).get("pageInfo", {}).get("hasPreviousPage") is not False
        or _public_record(decision_from_pr(repo, pr) or {}) != _public_record(decision)
    ):
        return None
    number = pr.get("number")
    exact = re.compile(
        r"https://github\.com/"
        + re.escape(repo.lower())
        + r"/pull/"
        + str(number)
        + r"#issuecomment-([1-9][0-9]*)\Z"
    )
    origins = []
    for comment in (pr.get("comments") or {}).get("nodes") or []:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or {}
        if not isinstance(author, dict):
            continue
        body, url = comment.get("body"), comment.get("url")
        match = exact.fullmatch(url.lower()) if isinstance(url, str) else None
        if (
            author.get("login") not in TRUSTED_AUTHORS
            or author.get("__typename") != "Bot"
            or not isinstance(body, str)
            or len(body.encode()) > 65536
            or match is None
        ):
            continue
        markers = MARKER_RE.findall(body)
        if len(markers) != 1:
            continue
        try:
            raw = json.loads(markers[0])
            if _public_record(raw) != _public_record(decision):
                continue
            origins.append(
                {
                    "schema": "closer-terminal-comment-origin/v1",
                    "comment_id": match[1],
                    "comment_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "decision_sha256": verification_events.content_hash(raw),
                    "source_url": url,
                    "author": "github-actions[bot]",
                    "actor_type": "Bot",
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return min(origins, key=lambda row: int(row["comment_id"])) if origins else None


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


def _invalid_observation(record, repo, number, head, merge, url, raw):
    if not all(
        re.fullmatch(r"[1-9][0-9]{0,19}", str(record.get(key, "")))
        for key in ("run_id", "run_attempt")
    ):
        return None  # No invented chronology for malformed/unavailable identity.
    return {
        **unusable_observation(repo, number, head, merge),
        "reason": "invalid_verifier_evidence",
        "run_id": str(record["run_id"]),
        "run_attempt": str(record["run_attempt"]),
        "source_url": url,
        "raw_marker_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }


def _valid_observation(record, repo, number, head, merge):
    return bool(
        is_unusable_observation(record, repo, number)
        and record.get("head_sha") == head
        and record.get("evaluated_sha") == merge
        and record.get("reason") == "invalid_verifier_evidence"
        and "verification_event" not in record
        and "verdict" not in record
        and all(
            re.fullmatch(r"[1-9][0-9]{0,19}", str(record.get(key, "")))
            for key in ("run_id", "run_attempt")
        )
        and re.fullmatch(r"[0-9a-f]{64}", str(record.get("raw_marker_sha256", "")))
        and str(record.get("source_url", ""))
        .lower()
        .startswith(f"https://github.com/{repo.lower()}/pull/{number}#issuecomment-")
    )


def _masked_by_observation(selected, states):
    return selected is not None and any(
        _valid_observation(
            row, selected["repo"], selected["pr"], selected["head_sha"], selected["evaluated_sha"]
        )
        and _attempt(row) >= _attempt(selected)
        for row in states
    )


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
            "result_admission",
            "retained_revalidation",
        )
        if key in record
    }
    if record.get("schema") == OBSERVATION_SCHEMA:
        result.update(
            {
                key: record[key]
                for key in ("reason", "source_url", "raw_marker_sha256")
                if key in record
            }
        )
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


def _superseded_incomplete_roots(nodes, parents, terminal_events, admitted_events):
    """Retain history without making an older failed read govern a fresh review.

    Only an earlier, incomplete independent chain with no semantic verdict can
    be superseded by a complete review of the same request or material, bound
    to the exact delivery. Same material can accompany revised requirements;
    the new admitted review, not the failed read, assesses those requirements.
    Real verdict conflicts and newer observations retain the causal checks.
    """
    roots = [identity for identity, parent in parents.items() if parent is None]

    def chain(root):
        seen = [root]
        while True:
            children = [identity for identity, parent in parents.items() if parent == seen[-1]]
            if not children:
                return seen
            if len(children) != 1 or children[0] in seen:
                return None
            seen.append(children[0])

    chains = [chain(root) for root in roots]
    if any(part is None for part in chains):
        return set()
    semantic = [part for part in chains if any(identity in terminal_events for identity in part)]
    if len(semantic) != 1:
        return set()
    # Keep the supersession established by a completed assessment even while
    # its same event or explicit successor has a newer pending attempt.
    selected_id = next(
        identity for identity in reversed(semantic[0]) if identity in terminal_events
    )
    # Legacy markers may come from a producer that published before validating
    # its execution receipt. They cannot establish a new supersession edge.
    selected = admitted_events.get(selected_id)
    if (
        selected is None
        or selected.get("verdict") not in {"PASS", "NON_PASS"}
        or not isinstance(selected.get("assessment_identity"), dict)
        or selected["assessment_identity"].get("canonical") is not True
    ):
        return set()

    def same_request_or_material(record):
        event = record["verification_event"]
        anchor = selected["verification_event"]
        return all(
            event.get(key) == anchor.get(key) for key in ("repo", "pr", "head_sha", "evaluated_sha")
        ) and any(
            isinstance(event.get(key), str)
            and verification_events.HEX256.fullmatch(event[key])
            and event[key] == anchor.get(key)
            for key in ("request_sha256", "material_sha256")
        )

    obsolete = set()
    for part in chains:
        if part is semantic[0] or any(identity in terminal_events for identity in part):
            continue
        if (
            nodes[part[-1]].get("status") == "incomplete"
            and all(same_request_or_material(nodes[identity]) for identity in part)
            and all(_attempt(nodes[identity]) < _attempt(selected) for identity in part)
        ):
            obsolete.update(part)
    return obsolete


def _retry_event_parents(records):
    """Recover an omitted retry edge only from its proven no-evaluation attempt.

    The immutable event identifies the same input, not a new causal branch. An
    older authenticated no-evaluation attempt can supply its unchanged parent
    when a later producer omits that optional field. Explicit conflicting edges,
    mixed metadata in one attempt and an originally root event stay ambiguous.
    Records remain untouched so the original publication is still auditable.
    """
    grouped = {}
    for record in records:
        grouped.setdefault(record["verification_event"]["event_id"], []).append(record)
    parents = {}
    for identity, rows in grouped.items():
        declared = set()
        for row in rows:
            parent = row.get("predecessor_event_id")
            if parent is not None and (
                not isinstance(parent, str) or not verification_events.HEX256.fullmatch(parent)
            ):
                return None
            declared.add(parent)
        if len(declared) == 1:
            parents[identity] = declared.pop()
            continue
        explicit = declared - {None}
        if len(explicit) != 1:
            return None
        parent = explicit.pop()
        omitted = [row for row in rows if row.get("predecessor_event_id") is None]
        first_omitted = min(_attempt(row) for row in omitted)
        linked = [row for row in rows if row.get("predecessor_event_id") == parent]
        if any(_attempt(row) >= first_omitted for row in linked):
            return None
        latest_linked = max(_attempt(row) for row in linked)
        prior = [row for row in linked if _attempt(row) == latest_linked]
        if any("verdict" in row for row in prior) or not any(
            row.get("status") == "incomplete"
            and row.get("retry_kind") == "known_no_evaluation"
            and row.get("retryable") is True
            and row.get("failure_cause")
            in {"validated_no_evaluation", "evaluator_step_not_invoked"}
            and isinstance(row.get("assessment_identity"), dict)
            and row["assessment_identity"].get("canonical") is True
            and isinstance(row["assessment_identity"].get("mode"), str)
            and row["assessment_identity"].get("mode") in {"evaluate", "compare"}
            and all(
                isinstance(row["assessment_identity"].get(key), str)
                for key in ("provider", "model", "model2")
            )
            for row in prior
        ):
            return None
        # Recovering an edge grants no authority to skip another attempt's
        # uncertainty or contradictory assessment metadata. Check the complete
        # retry history, including omitted-parent attempts, without altering it.
        attempts = {}
        for row in rows:
            attempts.setdefault(_attempt(row), []).append(row)
        for attempt_rows in attempts.values():
            assessment = attempt_rows[0].get("assessment_identity")
            if any(row.get("assessment_identity") != assessment for row in attempt_rows):
                return None
            if any(
                row.get("status") == "incomplete"
                and (
                    row.get("retry_kind") != "known_no_evaluation"
                    or row.get("retryable") is not True
                    or row.get("failure_cause")
                    not in {"validated_no_evaluation", "evaluator_step_not_invoked"}
                )
                for row in attempt_rows
            ):
                return None
        parents[identity] = parent
    return parents


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
        return None if _masked_by_observation(baseline, states) else baseline
    nodes = {}
    parents = _retry_event_parents(records)
    if parents is None:
        return None
    terminal_events = {}
    admitted_events = {}
    for record in records:
        identity = record["verification_event"]["event_id"]
        if "verdict" in record and (
            identity not in terminal_events
            or _attempt(record) < _attempt(terminal_events[identity])
        ):
            terminal_events[identity] = record
        if (
            "verdict" in record
            and _valid_result_admission(record)
            and (
                identity not in admitted_events
                or _attempt(record) < _attempt(admitted_events[identity])
            )
        ):
            admitted_events[identity] = record
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
    # A previous incomplete read is not an acceptance verdict or an uncertain
    # consequential effect. Do not let it invalidate a later complete review of
    # the same request or material. Preserve every input record and other branches.
    if baseline is None:
        for identity in _superseded_incomplete_roots(
            nodes, parents, terminal_events, admitted_events
        ):
            nodes.pop(identity)
            parents.pop(identity)
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
    if _masked_by_observation(latest, states):
        return None
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
    ancestry = decision.get("_lifecycle_history", [])
    if not isinstance(ancestry, list) or len(ancestry) > 1000:
        return False
    unusable = is_unusable_observation(decision, repo, number)
    ordered_observations = any(
        _valid_observation(
            row, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
        )
        for row in ancestry
        if isinstance(row, dict)
    )
    if (
        unusable
        and not ordered_observations
        and not _valid_observation(
            decision, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
        )
    ):
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
    valid_observation = _valid_observation(
        decision, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
    )
    if (
        not valid
        and not _valid_state(
            decision, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
        )
        and not valid_observation
        and not (unusable and ordered_observations)
    ):
        return False
    import feedback

    current = _public_record(decision)
    for ancestor in ancestry:
        if not isinstance(ancestor, dict) or not (
            decision_matches_identity(
                ancestor, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
            )
            or _valid_state(
                ancestor, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
            )
            or _valid_observation(
                ancestor, repo, number, decision.get("head_sha"), decision.get("evaluated_sha")
            )
        ):
            return False
    incoming = [_public_record(row) for row in ancestry] + (
        [current] if not unusable or valid_observation else []
    )
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
                or _valid_observation(
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
                if (
                    d == candidate
                    if candidate.get("schema") == OBSERVATION_SCHEMA
                    else _attempt(d) == _attempt(candidate)
                )
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
                    "record_kind": (
                        "observation:" + verification_events.content_hash(candidate)
                        if candidate.get("schema") == OBSERVATION_SCHEMA
                        else "terminal" if "verdict" in candidate else candidate["status"]
                    ),
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
