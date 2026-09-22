"""Read merge-bound verdicts posted by the fleet verifier, never CI status alone."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

MARKER = "verifier-corpus-decision/v1"
MARKER_RE = re.compile(r"<!-- verifier-corpus-decision/v1 (\{[^\n]+\}) -->")
SHA_RE = re.compile(r"[0-9a-f]{40}")
TRUSTED_AUTHORS = {"github-actions", "github-actions[bot]"}
VALID_PROVIDER_VERDICTS = {"PASS", "CONCERNS", "FAIL", "NON_PASS"}


def decision_from_pr(repo: str, pr: dict[str, Any]) -> dict[str, Any] | None:
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
        for raw in MARKER_RE.findall(str(comment.get("body") or "")):
            try:
                decision = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(decision, dict) or decision.get("schema") != MARKER:
                continue
            if str(decision.get("repo") or "").lower() != repo.lower():
                continue
            if str(decision.get("pr") or "") != str(number):
                continue
            if decision.get("head_sha") != head or decision.get("evaluated_sha") != merge:
                continue
            run, attempt = str(decision.get("run_id") or ""), str(decision.get("run_attempt") or "")
            if not run.isdigit() or not attempt.isdigit() or int(run) < 1 or int(attempt) < 1:
                continue
            providers = decision.get("provider_verdicts")
            if (
                not isinstance(providers, list)
                or not providers
                or any(v not in VALID_PROVIDER_VERDICTS for v in providers)
                or type(decision.get("ci_failed")) is not bool
            ):
                continue
            expected = (
                "PASS"
                if not decision["ci_failed"] and all(v == "PASS" for v in providers)
                else "NON_PASS"
            )
            if decision.get("verdict") != expected:
                continue
            candidates.append((int(run), int(attempt), decision))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[:2])[2]


def fetch_decisions(repo: str, numbers: list[int]) -> dict[int, dict[str, Any]]:
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
            decision = decision_from_pr(repo, pr)
            if decision:
                found[pr["number"]] = decision
    return found
