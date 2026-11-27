"""Base freshness for merges and PR-run reuse for deploys (owner 2026-11-27, ci_tested_merge.py)."""

from __future__ import annotations

import json
import subprocess

import pytest

import ci_tested_merge as ctm
import merge_guard

REPO = "o/r"
HEAD = "h" * 40
BASE = "b" * 40
MERGE = "3" * 40
TREE = "t" * 40


def fake_gh(routes: dict[str, object]):
    """A run_fn answering `gh <args>` by the first route key found in the joined args."""

    def run(cmd, **kwargs):
        joined = " ".join(cmd)
        for key, out in routes.items():
            if key in joined:
                if out is None:
                    return subprocess.CompletedProcess(cmd, 1, "", "boom")
                text = out if isinstance(out, str) else json.dumps(out)
                return subprocess.CompletedProcess(cmd, 0, text, "")
        return subprocess.CompletedProcess(cmd, 1, "", f"no route for {joined}")

    return run


def _routes_for_tested_run(extra: dict | None = None) -> dict:
    routes = {
        "workflows/ci.yml/runs": {
            "workflow_runs": [{"id": 5, "html_url": "https://run/5", "created_at": "2026-11-27"}]
        },
        "runs/5/jobs": {"jobs": [{"id": 50}]},
        "jobs/50/logs": f"git fetch origin +{MERGE}:refs/remotes/pull/9/merge\n",
        f"git/commits/{MERGE}": {"parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}},
    }
    routes.update(extra or {})
    return routes


def commit(sha: str, subject: str) -> dict:
    return {"sha": sha, "commit": {"message": subject + "\n\nbody"}}


def test_tested_merge_reads_the_checked_out_test_merge():
    tm = ctm.tested_merge(REPO, HEAD, run_fn=fake_gh(_routes_for_tested_run()))
    assert tm == {
        "run_id": 5,
        "url": "https://run/5",
        "merge_sha": MERGE,
        "base_sha": BASE,
        "tree": TREE,
    }


def test_tested_merge_rejects_a_merge_of_another_head():
    routes = _routes_for_tested_run(
        {f"git/commits/{MERGE}": {"parents": [{"sha": BASE}, {"sha": "x" * 40}]}}
    )
    assert ctm.tested_merge(REPO, HEAD, run_fn=fake_gh(routes)) is None


def test_fresh_when_main_has_not_moved_prints_behind_zero():
    fr = ctm.freshness(
        REPO,
        9,
        HEAD,
        run_fn=fake_gh(_routes_for_tested_run({"compare/": {"ahead_by": 0, "commits": []}})),
    )
    assert fr["state"] == "fresh"
    assert fr["behind"] == 0 and fr["drainable"] == 0  # fully drained is reachable and printed
    assert "behind 0" in fr["line"]


def test_template_sync_disjoint_from_the_pr_is_exempt():
    routes = _routes_for_tested_run(
        {
            "compare/": {
                "ahead_by": 1,
                "commits": [commit("s" * 40, "chore: sync workflow templates")],
            },
            "pulls/9/files": "src/router.py\n",
            f"commits/{'s' * 40}": ".github/scripts/x.js\n",
        }
    )
    fr = ctm.freshness(REPO, 9, HEAD, run_fn=fake_gh(routes))
    assert fr["state"] == "exempt_drift"
    assert (fr["behind"], fr["exempt"], fr["non_exempt"]) == (1, 1, 0)


@pytest.mark.parametrize(
    ("subject", "touched"),
    [
        ("feat: a real change", ".github/scripts/x.js\n"),
        ("chore: sync workflow templates", "src/router.py\n"),  # exempt kind, but overlapping
    ],
)
def test_other_commits_or_overlapping_syncs_make_the_base_stale(subject, touched):
    routes = _routes_for_tested_run(
        {
            "compare/": {"ahead_by": 1, "commits": [commit("c" * 40, subject)]},
            "pulls/9/files": "src/router.py\n",
            f"commits/{'c' * 40}": touched,
        }
    )
    fr = ctm.freshness(REPO, 9, HEAD, run_fn=fake_gh(routes))
    assert fr["state"] == "stale"
    assert fr["non_exempt"] == 1 and fr["drainable"] == 1
    assert "drainable 1" in fr["line"]


def test_unreadable_ci_is_unmeasured_and_says_why():
    fr = ctm.freshness(REPO, 9, HEAD, run_fn=fake_gh({}))
    assert fr["state"] == "unmeasured" and "not blocking" in fr["line"]


def test_deploy_reuses_a_pr_run_that_tested_the_same_tree():
    sha = "d" * 40
    routes = _routes_for_tested_run(
        {
            f"commits/{sha}/pulls": [
                {"number": 9, "merge_commit_sha": sha, "merged_at": "x", "head": {"sha": HEAD}}
            ],
            f"commits/{sha}": {
                "files": [{"filename": "src/router.py"}],
                "parents": [{"sha": BASE}],
                "commit": {"tree": {"sha": TREE}},
            },
        }
    )
    out = ctm.deploy_reuse(REPO, sha, run_fn=fake_gh(routes))
    assert out["reuse"] is True and out["url"] == "https://run/5"


def test_deploy_reuse_looks_through_a_floor_reconcile_but_not_other_commits():
    sha, floor = "d" * 40, "f" * 40
    squash = {
        "files": [{"filename": "src/router.py"}],
        "parents": [{"sha": BASE}],
        "commit": {"tree": {"sha": TREE}},
    }
    pulls = [{"number": 9, "merge_commit_sha": sha, "merged_at": "x", "head": {"sha": HEAD}}]

    def routes(files):
        top = {"files": files, "parents": [{"sha": sha}], "commit": {"tree": {"sha": "z" * 40}}}
        return _routes_for_tested_run(
            {
                f"commits/{floor}/pulls": [],
                f"commits/{floor}": top,
                f"commits/{sha}/pulls": pulls,
                f"commits/{sha}": squash,
            }
        )

    ok = ctm.deploy_reuse(REPO, floor, run_fn=fake_gh(routes([{"filename": ".verify-floor.json"}])))
    assert ok["reuse"] is True and ".verify-floor.json" in ok["detail"]
    other = [{"filename": ".verify-floor.json"}, {"filename": "src/x.py"}]
    assert ctm.deploy_reuse(REPO, floor, run_fn=fake_gh(routes(other)))["reuse"] is False


def guarded(fresh, updates, merges, **kw):
    return merge_guard.guarded_merge(
        "o/r#9",
        expected_head=HEAD,
        confirm_merge=True,
        metadata_fn=lambda target: {
            "target": target,
            "title": "bounded work",
            "labels": [],
            "state": "OPEN",
            "is_draft": False,
            "closing_issues": ["o/r#1"],
            "head_branch": "claude/x",
        },
        gate_fn=lambda *args, **kwargs: None,
        preflight_fn=lambda target, expected_head: {"blocked": False, "head": expected_head},
        panel_fn=lambda *args, **kwargs: {"status": "disabled"},
        adjudicate_fn=lambda *args, **kwargs: None,
        review_loop_fn=lambda *args, **kwargs: {"required": False, "eligible": True},
        merge_fn=lambda cmd, **kwargs: merges.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, "merged", ""),
        remote_runs_fn=lambda *args, **kwargs: [],
        spawn_fn=lambda *args, **kwargs: {"status": "disabled"},
        freshness_fn=lambda repo, num, head: fresh,
        update_branch_fn=lambda repo, num, head: updates.append((repo, num, head)) or {"ok": True},
        **kw,
    )


def test_stale_base_blocks_and_merges_main_into_the_branch():
    updates, merges = [], []
    fresh = {"state": "stale", "line": "base: behind 2 (non-exempt 1, drainable 1)"}
    out = guarded(fresh, updates, merges, require_fresh_base=True)
    assert out["blocked"] and not merges
    assert updates == [("o/r", 9, HEAD)]
    assert "re-run with its --expected-head" in out["reason"]


@pytest.mark.parametrize("state", ["fresh", "exempt_drift", "unmeasured"])
def test_fresh_exempt_or_unmeasured_base_merges(state):
    updates, merges = [], []
    out = guarded({"state": state, "line": state}, updates, merges, require_fresh_base=True)
    assert not out["blocked"] and merges and not updates
    assert out["base_freshness"]["state"] == state


def test_in_process_callers_are_unchanged_unless_they_ask():
    """closer-core calls guarded_merge in process for 16 repos; the check is the command line's default."""
    updates, merges = [], []
    out = guarded({"state": "stale", "line": "stale"}, updates, merges)
    assert not out["blocked"] and merges and "base_freshness" not in out


def test_command_line_requires_a_fresh_base_unless_allowed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        merge_guard, "guarded_merge", lambda target, **kw: seen.append(kw) or {"blocked": False}
    )
    assert merge_guard.main(["o/r#9", "--expected-head", HEAD]) == 0
    assert merge_guard.main(["o/r#9", "--expected-head", HEAD, "--allow-stale-base"]) == 0
    assert [kw["require_fresh_base"] for kw in seen] == [True, False]


def test_rename_sources_count_and_listings_are_paginated():
    """A sync renaming a file the PR edits is not disjoint; every page of a listing is read."""
    seen: list[str] = []
    routes = _routes_for_tested_run(
        {
            "compare/": {
                "ahead_by": 1,
                "commits": [commit("c" * 40, "chore: sync workflow templates")],
            },
            "pulls/9/files": "src/old.py\n",
            f"commits/{'c' * 40}": "src/new.py\nsrc/old.py\n",  # gh --jq prints both rename paths
        }
    )
    base_run = fake_gh(routes)

    def run(cmd, **kwargs):
        seen.append(" ".join(cmd))
        return base_run(cmd, **kwargs)

    fr = ctm.freshness(REPO, 9, HEAD, run_fn=run)
    assert fr["state"] == "stale"
    listings = [c for c in seen if "pulls/9/files" in c or f"commits/{'c' * 40}" in c]
    assert listings and all("--paginate" in c and "previous_filename" in c for c in listings)


def test_a_listing_at_githubs_file_ceiling_is_not_trusted_as_disjoint():
    many = "".join(f"other/{i}.js\n" for i in range(ctm.FILE_LIST_CEILING))
    routes = _routes_for_tested_run(
        {
            "compare/": {
                "ahead_by": 1,
                "commits": [commit("c" * 40, "chore: sync workflow templates")],
            },
            "pulls/9/files": "src/router.py\n",
            f"commits/{'c' * 40}": many,
        }
    )
    assert ctm.freshness(REPO, 9, HEAD, run_fn=fake_gh(routes))["state"] == "stale"


def test_a_floor_named_rename_is_not_floor_only():
    """The parent would qualify for reuse, so only counting the rename's source path refuses it."""
    sha, parent = "e" * 40, "d" * 40
    pulls = [{"number": 9, "merge_commit_sha": parent, "merged_at": "x", "head": {"sha": HEAD}}]
    squash = {
        "files": [{"filename": "src/router.py"}],
        "parents": [{"sha": BASE}],
        "commit": {"tree": {"sha": TREE}},
    }

    def top(files):
        return {"files": files, "parents": [{"sha": parent}], "commit": {"tree": {"sha": "z" * 40}}}

    def routes(files):
        return _routes_for_tested_run(
            {
                f"commits/{sha}/pulls": [],
                f"commits/{sha}": top(files),
                f"commits/{parent}/pulls": pulls,
                f"commits/{parent}": squash,
            }
        )

    plain = routes([{"filename": ".verify-floor.json"}])
    assert ctm.deploy_reuse(REPO, sha, run_fn=fake_gh(plain))["reuse"] is True
    renamed = routes([{"filename": ".verify-floor.json", "previous_filename": "src/x.py"}])
    assert ctm.deploy_reuse(REPO, sha, run_fn=fake_gh(renamed))["reuse"] is False


def test_unmeasured_keeps_the_read_error():
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "HTTP 403: API rate limit exceeded")

    fr = ctm.freshness(REPO, 9, HEAD, run_fn=run)
    assert fr["state"] == "unmeasured"
    assert "rate limit exceeded" in fr["reason"] and fr["gh_errors"]


def test_update_branch_pins_the_expected_head_and_reports_failure():
    calls: list[list[str]] = []

    def ok(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, '{"message": "Updating pull request branch."}', ""
        )

    out = ctm.update_branch(REPO, 9, HEAD, run_fn=ok)
    assert out["ok"] is True
    assert calls[0][:4] == ["gh", "api", "-X", "PUT"]
    assert "repos/o/r/pulls/9/update-branch" in calls[0]
    assert f"expected_head_sha={HEAD}" in calls[0]

    def refused(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "HTTP 422: expected head sha didn't match")

    bad = ctm.update_branch(REPO, 9, HEAD, run_fn=refused)
    assert bad["ok"] is False and "expected head sha" in bad["detail"]


def test_cli_prints_json_and_exits_by_verdict(capsys):
    fresh = fake_gh(_routes_for_tested_run({"compare/": {"ahead_by": 0, "commits": []}}))
    assert ctm.main(["freshness", "--repo", REPO, "--pr", "9", "--head", HEAD], run_fn=fresh) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "fresh"
    stale = fake_gh(
        _routes_for_tested_run(
            {
                "compare/": {"ahead_by": 1, "commits": [commit("c" * 40, "feat: x")]},
                "pulls/9/files": "src/a.py\n",
            }
        )
    )
    assert ctm.main(["freshness", "--repo", REPO, "--pr", "9", "--head", HEAD], run_fn=stale) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "stale"
    assert ctm.main(["deploy-reuse", "--repo", REPO, "--sha", "d" * 40], run_fn=fake_gh({})) == 1
    assert json.loads(capsys.readouterr().out)["reuse"] is False


def test_base_is_read_again_right_before_the_merge():
    """Main can move during the panel and preflight: a stale base at the final read still blocks."""
    states = iter([{"state": "fresh", "line": "fresh"}, {"state": "stale", "line": "stale now"}])
    updates, merges = [], []
    out = merge_guard.guarded_merge(
        "o/r#9",
        expected_head=HEAD,
        confirm_merge=True,
        metadata_fn=lambda target: {
            "target": target,
            "title": "bounded work",
            "labels": [],
            "state": "OPEN",
            "is_draft": False,
            "closing_issues": ["o/r#1"],
            "head_branch": "claude/x",
        },
        gate_fn=lambda *args, **kwargs: None,
        preflight_fn=lambda target, expected_head: {"blocked": False, "head": expected_head},
        panel_fn=lambda *args, **kwargs: {"status": "disabled"},
        adjudicate_fn=lambda *args, **kwargs: None,
        review_loop_fn=lambda *args, **kwargs: {"required": False, "eligible": True},
        merge_fn=lambda cmd, **kwargs: merges.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, "merged", ""),
        remote_runs_fn=lambda *args, **kwargs: [],
        spawn_fn=lambda *args, **kwargs: {"status": "disabled"},
        require_fresh_base=True,
        freshness_fn=lambda repo, num, head: next(states),
        update_branch_fn=lambda repo, num, head: updates.append(head) or {"ok": True},
    )
    assert out["blocked"] and not merges and updates == [HEAD]
    assert out["base_freshness"]["state"] == "fresh"
    assert out["base_freshness_final"]["state"] == "stale"
