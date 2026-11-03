"""Local delegate dedup: all GitHub reads and worker/claim effects are stubbed."""

import json
import subprocess
from unittest.mock import Mock

import pytest

import dispatcher

TARGET = "o/r#731"


def rest_pr(number=734, body="Closes #731", branch="feature"):
    return {"number": number, "body": body, "head": {"ref": branch}}


def graph_page(numbers=(734,), refs=(731,), more=False, cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                    "nodes": [
                        {
                            "number": n,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {"number": ref, "repository": {"nameWithOwner": "o/r"}}
                                    for ref in refs
                                ],
                            },
                        }
                        for n in numbers
                    ],
                }
            }
        },
    }


@pytest.fixture
def world(monkeypatch, tmp_path):
    calls = []
    answers = {"rest": [[rest_pr()]], "graph": [graph_page()]}

    def run(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        assert "--paginate" in argv and "--slurp" in argv
        assert kwargs["timeout"] == dispatcher.LINKED_PR_READ_TIMEOUT_S
        calls.append(argv)
        answer = answers["graph" if argv[2] == "graphql" else "rest"]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    claim = Mock(return_value=True)
    reap = Mock()
    spawn = Mock(return_value=42)
    monkeypatch.setattr(dispatcher.claims, "claim", claim)
    monkeypatch.setattr(dispatcher.claims, "reap_stale", reap)
    monkeypatch.setattr(dispatcher.claims, "release", Mock())
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path)
    monkeypatch.setattr(
        dispatcher,
        "plan_dispatch",
        lambda a, **kw: {
            **a,
            "cwd": str(tmp_path),
            "run_id": "test-run",
        },
    )
    monkeypatch.setattr(dispatcher, "_spawn", spawn)
    monkeypatch.setattr(dispatcher, "_delegate_lane_heartbeat", Mock())
    monkeypatch.setattr(dispatcher.feedback, "record_run", Mock())
    return answers, calls, claim, reap, spawn


def delegate(**kwargs):
    return dispatcher.delegate("cursor", TARGET, "opener", "Bounded work", **kwargs)


def test_refuses_when_an_open_pr_closes_the_target(world):
    _, calls, claim, reap, spawn = world
    assert delegate() == {
        "error": "linked: open PR #734 close o/r#731; "
        "pass continue_pr=<N> to work on one of them"
    }
    claim.assert_not_called()
    reap.assert_not_called()
    spawn.assert_not_called()
    assert len(calls) == 2
    assert calls[0][2] == "repos/o/r/pulls?state=open&per_page=100"
    assert calls[1][2] == "graphql"


def test_continue_pr_allows_exactly_the_named_linked_pr(world):
    _, _, claim, _, spawn = world
    assert delegate(continue_pr=734)["pid"] == 42
    claim.assert_called_once_with(TARGET, "cursor")
    spawn.assert_called_once()
    claim.reset_mock()
    spawn.reset_mock()
    assert "linked: open PR #734" in delegate(continue_pr=736)["error"]
    claim.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.parametrize("continuation", [None, 734])
def test_read_failure_fails_closed_with_override_message(world, continuation):
    answers, _, claim, reap, spawn = world
    answers["graph"] = subprocess.CompletedProcess([], 1, "", "HTTP 502")
    error = delegate(continue_pr=continuation)["error"]
    assert error == "linked: unknown (gh exit 1: HTTP 502); retry when GitHub access recovers"
    claim.assert_not_called()
    reap.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "body,branch",
    [
        ("cLoSeS#731", "feature"),
        ("Closes: #731", "feature"),
        ("Fixes: #731", "feature"),
        ("Resolves: o/r#731", "feature"),
        ("Fixed o/r#731", "feature"),
        ("Resolves https://github.com/o/r/issues/731", "feature"),
        (None, "codex/issue-731"),
        ("", "orch/issue-731-repair"),
        ("", "muse/issue-731"),
        ("", "vibe/issue-731"),
        ("", "orchestrator/issue-731-fix"),
    ],
)
def test_rest_linkage_rules(world, body, branch):
    answers, *_ = world
    answers["rest"] = [[rest_pr(body=body, branch=branch)]]
    answers["graph"] = [graph_page(refs=())]
    assert dispatcher.linked_open_prs(TARGET) == [734]


@pytest.mark.parametrize(
    "body,branch",
    [
        ("Closes #7310", "codex/issue-7310"),
        ("References #731", "feature"),
        ("Closes other/repo#731", "feature"),
        ("Fixes https://github.com/other/repo/issues/731", "feature"),
        ("", "feature/codex/issue-731"),
        ("", "codex/issue-731oops"),
    ],
)
def test_unrelated_references_do_not_block(world, body, branch):
    answers, *_ = world
    answers["rest"] = [[rest_pr(body=body, branch=branch)]]
    answers["graph"] = [graph_page(refs=())]
    assert "error" not in delegate()
    assert delegate(continue_pr=734)["error"] == (
        "linked: continue_pr=734 is not an open PR linked to o/r#731"
    )


def test_graphql_linkage_matches_repository_and_survives_rest_without_links(world):
    answers, *_ = world
    answers["rest"] = [[rest_pr(body="")]]
    assert dispatcher.linked_open_prs(TARGET) == [734]
    refs = answers["graph"][0]["data"]["repository"]["pullRequests"]["nodes"][0]
    refs["closingIssuesReferences"]["nodes"][0]["repository"]["nameWithOwner"] = "other/repo"
    assert dispatcher.linked_open_prs(TARGET) == []


def test_all_pages_union_and_deduplicate_all_linkage_rules(world):
    answers, calls, *_ = world
    answers["rest"] = [[rest_pr()], [rest_pr(736, "", "claude/issue-731-fix")]]
    answers["graph"] = [graph_page(more=True, cursor="page-1"), graph_page((736,), ())]
    assert dispatcher.linked_open_prs(TARGET) == [734, 736]
    assert len(calls) == 2, "one paginated command per API"
    assert "#734, #736" in delegate()["error"]
    assert delegate(continue_pr=736)["pid"] == 42


@pytest.mark.parametrize(
    "failure",
    [
        "rest-exit",
        "timeout",
        "missing-gh",
        "not-json",
        "bad-rest",
        "bad-graph",
        "graphql-errors",
        "pr-truncation",
        "ref-truncation",
        "changed-population",
        "missing-body",
        "null-nodes",
        "null-refs",
    ],
)
def test_incomplete_or_malformed_reads_never_claim(world, failure):
    answers, _, claim, _, spawn = world
    connection = answers["graph"][0]["data"]["repository"]["pullRequests"]
    if failure == "rest-exit":
        answers["rest"] = subprocess.CompletedProcess([], 1, "", "rate limited")
    elif failure == "timeout":
        answers["rest"] = subprocess.TimeoutExpired(["gh"], 30)
    elif failure == "missing-gh":
        answers["rest"] = FileNotFoundError("gh unavailable")
    elif failure == "not-json":
        answers["rest"] = subprocess.CompletedProcess([], 0, "<html>", "")
    elif failure == "bad-rest":
        answers["rest"] = [{"message": "busy"}]
    elif failure == "bad-graph":
        answers["graph"] = [{"data": {"repository": None}}]
    elif failure == "graphql-errors":
        answers["graph"][0]["errors"] = [{"message": "quota exhausted"}]
    elif failure == "pr-truncation":
        connection["pageInfo"]["hasNextPage"] = True
    elif failure == "ref-truncation":
        connection["nodes"][0]["closingIssuesReferences"]["pageInfo"]["hasNextPage"] = True
    elif failure == "changed-population":
        connection["nodes"] = []
    elif failure == "missing-body":
        del answers["rest"][0][0]["body"]
    elif failure == "null-nodes":
        connection["nodes"] = None
    else:
        connection["nodes"][0]["closingIssuesReferences"]["nodes"] = None
    assert "linked: unknown (" in delegate()["error"]
    claim.assert_not_called()
    spawn.assert_not_called()


def test_failed_read_is_retried_without_cached_refusal(world):
    answers, *_ = world
    answers["rest"] = OSError("offline")
    assert "offline" in delegate()["error"]
    answers["rest"] = [[]]
    answers["graph"] = [graph_page((), ())]
    assert "error" not in delegate()


def test_cli_forwards_continue_pr(world, capsys):
    assert (
        dispatcher.main(
            [
                "delegate",
                "--agent",
                "cursor",
                "--target",
                TARGET,
                "--prompt",
                "Repair the existing PR",
                "--continue-pr",
                "734",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["pid"] == 42


def test_invalid_target_refuses_without_github_calls(world):
    _, calls, *_ = world
    assert dispatcher.linked_open_prs("bad-target") is None
    assert not calls
