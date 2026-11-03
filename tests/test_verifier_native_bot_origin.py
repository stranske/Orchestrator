"""Native GraphQL Bot spelling must preserve exact selected-comment provenance."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

import verifier_evidence as evidence

REPO = "stranske/Orchestrator"
FIXTURE = (
    Path(__file__).parent / "fixtures/verifier_native_bot_origin/canary984-selected-origin.json"
)
COMMENT_ID = "6458980062"


def captured():
    return json.loads(FIXTURE.read_bytes())


def selected(pr):
    return next(c for c in pr["comments"]["nodes"] if c["url"].endswith(COMMENT_ID))


@pytest.mark.parametrize("login", ["github-actions", "github-actions[bot]"])
def test_actual_selected_bot_spelling_projects_canonical_origin(login):
    pr = captured()
    comment = selected(pr)
    comment["author"]["login"] = login
    decision = evidence.decision_from_pr(REPO, pr)
    assert decision["verdict"] == "NON_PASS"
    origin = evidence.terminal_origin_from_pr(REPO, pr, decision)
    assert origin == {
        "schema": "closer-terminal-comment-origin/v1",
        "comment_id": COMMENT_ID,
        "comment_sha256": hashlib.sha256(comment["body"].encode()).hexdigest(),
        "decision_sha256": evidence.verification_events.content_hash(
            json.loads(evidence.MARKER_RE.findall(comment["body"])[0])
        ),
        "source_url": comment["url"],
        "author": "github-actions[bot]",
        "actor_type": "Bot",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "user",
        "foreign_bot",
        "missing_type",
        "foreign_url",
        "wrong_node",
        "duplicate_marker",
        "wrong_marker",
        "partial_inventory",
        "wrong_selected_decision",
        "stale_history",
    ],
)
def test_native_identity_and_selected_inventory_guards_remain_required(mutation):
    pr = captured()
    decision = evidence.decision_from_pr(REPO, pr)
    comment = selected(pr)
    if mutation == "user":
        comment["author"]["__typename"] = "User"
    elif mutation == "foreign_bot":
        comment["author"]["login"] = "some-other-bot"
    elif mutation == "missing_type":
        del comment["author"]["__typename"]
    elif mutation == "foreign_url":
        comment["url"] = comment["url"].replace("Orchestrator", "Other")
    elif mutation == "wrong_node":
        comment["url"] += "/not-comment"
    elif mutation == "duplicate_marker":
        comment["body"] += "\n" + evidence.MARKER_RE.search(comment["body"])[0]
    elif mutation == "wrong_marker":
        comment["body"] = comment["body"].replace(evidence.MARKER, "wrong-decision/v1")
    elif mutation == "partial_inventory":
        pr["comments"]["pageInfo"]["hasPreviousPage"] = True
    elif mutation == "wrong_selected_decision":
        decision = copy.deepcopy(decision)
        decision["run_id"] = "1"
    elif mutation == "stale_history":
        # A current native terminal state that conflicts with selected outcome
        # must be processed by normal history selection, never bypassed by origin.
        newer = copy.deepcopy(comment)
        newer["url"] = newer["url"].replace(COMMENT_ID, "6458980063")
        raw = json.loads(evidence.MARKER_RE.findall(newer["body"])[0])
        raw["run_id"] = str(int(raw["run_id"]) + 1)
        raw["provider_verdicts"] = ["PASS", "PASS"]
        raw["ci_failed"] = False
        raw["verdict"] = "PASS"
        newer["body"] = "<!-- " + evidence.MARKER + " " + json.dumps(raw) + " -->"
        pr["comments"]["nodes"].append(newer)
    assert evidence.terminal_origin_from_pr(REPO, pr, decision) is None
