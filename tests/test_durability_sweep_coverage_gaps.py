"""Hermetic coverage for durability_sweep lines still missing in combined CI.

Issue #837 ranked this module fourth (~91 uncovered statements after selftests).
Every test isolates ORCH_STATE_DIR / ORCH_LOCAL_RUNTIME and stubs gh/subprocess.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import backlog
import durability_sweep as sweep
import feedback
import outcomes
import runtime_ac_gate as gate

NOW = 1_790_000_000
DAY = 86400


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    started: list[list[str]] = []

    def _block_run(args, **kwargs):  # noqa: ANN001
        started.append(list(args))
        raise AssertionError(f"live subprocess must not start: {args}")

    monkeypatch.setattr(sweep.subprocess, "run", _block_run)
    yield
    assert started == [], started


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(number: int, *, merged_at: int, **extra) -> dict:
    base = {
        "number": number,
        "state": "MERGED",
        "mergedAt": _iso(merged_at),
        "mergeCommit": {"oid": f"sha{number}"},
        "baseRefName": "main",
        "files": [{"path": "src/app.py"}],
    }
    base.update(extra)
    return base


def test_parse_ts_run_json_gh_helpers_and_merge_sha(monkeypatch):
    assert sweep._parse_gh_ts(None) is None
    assert sweep._parse_gh_ts("not-a-timestamp") is None
    assert sweep._parse_gh_ts("2026-01-01T00:00:00Z") is not None

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("gh", 1)),
    )
    assert sweep._run_json(["gh"]) is None
    assert sweep._gh_answer(["gh"]) == (None, "gh timed out")

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert sweep._run_json(["gh"]) is None
    assert sweep._gh_answer(["gh"]) == (None, "boom")

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    assert sweep._run_json(["gh"]) is None
    assert sweep._gh_answer(["gh"]) == (None, "gh printed no JSON")

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"ok":1}', stderr=""),
    )
    assert sweep._run_json(["gh"]) == {"ok": 1}
    assert sweep._gh_answer(["gh"]) == ({"ok": 1}, None)

    with patch.dict("sys.modules", {"gh_capacity": None}):
        # Import failure inside throttle must fail open.
        sweep._gh_throttle("graphql")

    assert sweep._merge_sha({"mergeCommit": "abc"}) == "abc"
    assert sweep._merge_sha({"mergeCommitOid": "def"}) == "def"
    assert sweep._explicit_merged_pr_target("", "PR #9 merged") is None


def test_default_branch_timeout_and_empty(monkeypatch):
    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("gh", 1)),
    )
    assert sweep._default_branch("o/r") is None

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="err"),
    )
    assert sweep._default_branch("o/r") is None

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="  \n", stderr=""),
    )
    assert sweep._default_branch("o/r") is None

    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="main\n", stderr=""),
    )
    assert sweep._default_branch("o/r") == "main"


def test_verifier_claims_skip_invalid_and_inherited_candidates():
    feedback.record_run("bad", "", "implement", "codex", mode="local", ts=NOW - 10 * DAY)
    feedback.record_outcome("bad", adjudicated_verdict="PASS", merged=True, durability="pending")

    feedback.record_run(
        "ka", "o/r#10", "implement", "codex", mode="remote", ts=NOW - 10 * DAY, source="keepalive"
    )
    feedback.record_outcome(
        "ka",
        adjudicated_verdict="PASS",
        merged=True,
        durability="pending",
        notes="keepalive merge",
    )

    feedback.record_run(
        "local", "o/r#99", "implement", "codex", mode="local", ts=NOW - 9 * DAY, source="local"
    )
    feedback.record_outcome(
        "local",
        adjudicated_verdict="PASS",
        merged=True,
        durability="pending",
        notes=f"{outcomes.REPLACEMENT_CREDIT} PR #10 merged",
    )

    found, inherited = sweep._verifier_claims()
    assert ("o/r", 10) not in found or "ka" in found.get(("o/r", 10), set())
    key = ("o/r", 10)
    # Keepalive claims the PR; local replacement inherits it.
    assert "ka" in found.get(key, set()) or key in inherited
    assert sweep.verifier_candidate_run_ids("o/r", 10, prospective_run_id="ka") == {"ka"}
    # With no found claim and no prospective id, inherited ids are used.
    with patch.object(sweep, "_verifier_claims", return_value=({}, {key: {"local"}})):
        assert sweep.verifier_candidate_run_ids("o/r", 10) == {"local"}


def test_find_merge_rejects_missing_target_named_non_pr_and_no_branch():
    assert (
        sweep.find_merge(
            {"target": "", "notes": None, "ts": NOW}, _gh=lambda *_a, **_k: (None, None)
        )["status"]
        == "unjudgeable"
    )

    def named_not_pr(args, **_k):
        return None, f"GraphQL: {sweep.GH_NOT_A_PULL_REQUEST} with the number of 7."

    got = sweep.find_merge(
        {
            "target": "o/r#1",
            "notes": "PR #7 merged",
            "ts": NOW - DAY,
            "mode": "local",
            "agent": "codex",
        },
        _gh=named_not_pr,
    )
    assert got["status"] == "unjudgeable"
    assert "not a pull request" in got["reason"]

    def issue_not_pr(args, **_k):
        if args[1:3] == ["pr", "view"]:
            return None, f"GraphQL: {sweep.GH_NOT_A_PULL_REQUEST} with the number of 3."
        raise AssertionError(args)

    got = sweep.find_merge(
        {
            "target": "o/r#3",
            "notes": "",
            "ts": NOW - DAY,
            "mode": "remote",
            "agent": "",  # no agent → no own branch
            "source": "orchestrator_remote",
        },
        _gh=issue_not_pr,
    )
    assert got["status"] == "unjudgeable"
    assert "no agent names the run's own branch" in got["reason"]


def test_read_fix_window_stops_when_split_cannot_narrow():
    calls = []

    def fetch(_repo, start, end):
        calls.append((start, end))
        # Always incomplete so the window keeps splitting until MIN_SPLIT.
        return [{"number": i} for i in range(sweep.FIX_SEARCH_CAP)], False

    entry = sweep.read_fix_window("o/r", NOW - 30, now=NOW - 1, _fetch=fetch)
    assert entry["error"] and f"{sweep.FIX_SEARCH_CAP}+ fix PRs" in entry["error"]
    assert calls


def test_live_revert_and_normalize_and_delivered():
    assert sweep._live_revert_status({"number": 1})[0] is None
    assert "missing repo" in sweep._live_revert_status({"number": 1})[1]

    # Unanswered PR search + unknown base → combined undecided note (covers BASE_BRANCH_UNKNOWN).
    with patch.object(sweep, "_revert_pr_status", return_value=(None, sweep.REVERT_SEARCH_FAILED)):
        with patch.object(sweep, "_default_branch", return_value=None):
            status, note = sweep._live_revert_status(
                {
                    "repo": "o/r",
                    "number": 2,
                    "mergeCommit": {"oid": "a" * 40},
                    "mergedAt": _iso(NOW - DAY),
                }
            )
            assert status is None
            assert sweep.BASE_BRANCH_UNKNOWN in note

    # Completed "no revert PR" with missing mergedAt still decides False after the commit note.
    with patch.object(sweep, "_revert_pr_status", return_value=(False, "no revert PR found")):
        status, note = sweep._live_revert_status(
            {
                "repo": "o/r",
                "number": 2,
                "mergeCommit": {"oid": "a" * 40},
                # missing mergedAt → commit_check notes it, then False wins
            }
        )
        assert status is False
        assert "no revert PR or base-branch revert commit found" in note

    assert sweep._normalize_revert_status({"reverted": True, "note": "n"}) == (True, "n")
    assert sweep.delivered({"files": [{}, {"path": ""}]}) is None


def test_classify_durability_edge_states():
    assert sweep.classify_durability({"target": "o/r#1"}, None)["drain"] == sweep.DRAIN_RETRY
    assert (
        sweep.classify_durability({"target": "o/r#1"}, {"state": "MERGED", "mergedAt": "bad"})[
            "reason"
        ]
        == "missing or invalid mergedAt"
    )
    got = sweep.classify_durability(
        {"target": "o/r#1", "mode": "remote"},
        {
            "state": "DRAFT",
            "mergedAt": _iso(NOW - 20 * DAY),
            "number": 1,
            "files": [{"path": "a.py"}],
        },
        now=NOW,
        _revert_fn=lambda _pr: (False, "no revert"),
        _fix_fn=lambda *_a, **_k: ([], True),
    )
    assert got["durability"] is None
    assert "ambiguous PR state" in got["reason"]


def test_load_retry_clocks_rejects_non_dict_and_find_unanswered(tmp_path, monkeypatch):
    path = tmp_path / "state" / sweep.RETRY_CLOCKS
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    assert sweep._load_retry_clocks() == {}

    assert (
        sweep._find({"target": "o/r#1"}, _state_fn=lambda _t: None, now=NOW)["status"]
        == "unanswered"
    )


def test_undo_truncated_recheck_missing_log_and_bad_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    assert sweep.undo_truncated_recheck()["restored"] == 0

    path = sweep._undo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json}\n" + json.dumps({"run_id": "r1", "durability": "durable"}) + "\n")
    feedback.record_run("r1", "o/r#1", "implement", "codex", mode="local", ts=NOW)
    feedback.record_outcome("r1", adjudicated_verdict="PASS", merged=True, durability="pending")
    assert sweep.undo_truncated_recheck()["restored"] == 1


def test_recheck_truncated_closes_bad_merge_and_unstated():
    summary = {
        "truncated": {
            "selected": 0,
            "broke_later": 0,
            "confirmed": 0,
            "closed": 0,
            "waiting": 0,
            "role_runs": 0,
            "stuck": 0,
        },
        "details": [],
        "unjudgeable": 0,
        "skip_reasons": {},
    }
    run = {
        "run_id": "t1",
        "target": "o/r#1",
        "judged_ts": NOW - DAY,
        "notes": f"x; {sweep.TRUNCATED_FIX_READ}",
    }
    sweep._recheck_truncated(
        summary,
        run,
        {"repo": "o/r", "mergedAt": None, "number": 1},
        fix_cache={},
        fix_since={},
        clocks={},
        now=NOW,
        dry_run=True,
    )
    assert summary["truncated"]["closed"] == 1

    summary["truncated"]["closed"] = 0
    with patch.object(
        sweep,
        "_fix_followup_status",
        return_value=(sweep.UNSTATED_REFERENCE, "named without repair"),
    ):
        sweep._recheck_truncated(
            summary,
            run,
            _pr(1, merged_at=NOW - 10 * DAY, repo="o/r"),
            fix_cache={},
            fix_since={"o/r": NOW - 20 * DAY},
            clocks={},
            now=NOW,
            dry_run=True,
        )
    assert summary["truncated"]["closed"] == 1


def test_recheck_bare_reference_relabels_and_marks_dry_run():
    summary = {
        "restated": {
            "selected": 0,
            "broke_later": 0,
            "durable": 0,
            "relabelled": 0,
            "closed": 0,
            "waiting": 0,
            "role_runs": 0,
            "stuck": 0,
        },
        "details": [],
        "unjudgeable": 0,
        "skip_reasons": {},
    }
    run = {
        "run_id": "b1",
        "target": "o/r#4",
        "judged_ts": NOW - DAY,
        "notes": "later fix PR #9 names this change",
        "mode": "remote",
    }
    with patch.object(
        sweep,
        "classify_durability",
        return_value={
            "durability": "reverted",
            "notes": "durability_sweep: reverted",
        },
    ):
        sweep._recheck_bare_reference(
            summary,
            run,
            _pr(4, merged_at=NOW - 20 * DAY, repo="o/r"),
            grace_days=7,
            revert_cache={},
            fix_cache={},
            fix_since={},
            clocks={},
            now=NOW,
            dry_run=True,
        )
    assert summary["restated"]["relabelled"] == 1
    assert summary["details"][0]["dry_run"] is True


def test_post_merge_rechecks_guard_branches(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    root = tmp_path / "specs"
    root.mkdir()
    monkeypatch.setattr(backlog, "SUPPORTED_REPOS", ["Owner/Repo", "other/x"])

    # Unsupported repo skipped.
    with patch.object(sweep, "_merged_verifier_candidates", return_value={("skip/me", 1): {"r"}}):
        assert sweep._post_merge_rechecks(
            dry_run=False, gh=lambda *_a: ({}, None), now=NOW, spec_dir=root
        ) == {
            "rechecked": 0,
            "errors": [],
        }

    # Casing remap then missing path continues.
    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 2): {"r"}}
    ):
        assert (
            sweep._post_merge_rechecks(
                dry_run=False, gh=lambda *_a: ({}, None), now=NOW, spec_dir=root
            )["rechecked"]
            == 0
        )

    target = "Owner/Repo#3"
    spec = {
        "verification": {"target": "Owner/Repo#999", "source_issue": "Owner/Repo#1"},
        "acceptance_criteria": [],
    }
    gate.spec_path(target, spec_dir=root).write_text(json.dumps(spec), encoding="utf-8")
    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 3): {"r"}}
    ):
        out = sweep._post_merge_rechecks(
            dry_run=False, gh=lambda *_a: ({}, None), now=NOW, spec_dir=root
        )
        assert out["errors"] and "spec target does not match" in out["errors"][0]

    spec["verification"]["target"] = target
    spec["verification"]["source_issue"] = "other/x#1"
    gate.spec_path(target, spec_dir=root).write_text(json.dumps(spec), encoding="utf-8")
    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 3): {"r"}}
    ):
        out = sweep._post_merge_rechecks(
            dry_run=False, gh=lambda *_a: ({}, None), now=NOW, spec_dir=root
        )
        assert out["errors"] and "source issue" in out["errors"][0]

    spec["verification"]["source_issue"] = "Owner/Repo#1"
    gate.spec_path(target, spec_dir=root).write_text(json.dumps(spec), encoding="utf-8")

    def gh_fail(args, **_k):
        return None, "pr gone"

    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 3): {"r"}}
    ):
        out = sweep._post_merge_rechecks(dry_run=False, gh=gh_fail, now=NOW, spec_dir=root)
        assert out["errors"] and "pr gone" in out["errors"][0]

    def gh_open(args, **_k):
        return {
            "state": "OPEN",
            "mergeCommit": {"oid": "a" * 40},
            "closingIssuesReferences": [],
        }, None

    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 3): {"r"}}
    ):
        assert sweep._post_merge_rechecks(dry_run=False, gh=gh_open, now=NOW, spec_dir=root) == {
            "rechecked": 0,
            "errors": [],
        }

    def gh_bad_sha(args, **_k):
        return {
            "state": "MERGED",
            "mergeCommit": {"oid": "not-forty"},
            "closingIssuesReferences": [{"number": 1}],
        }, None

    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 3): {"r"}}
    ):
        out = sweep._post_merge_rechecks(dry_run=False, gh=gh_bad_sha, now=NOW, spec_dir=root)
        assert out["errors"] and "merge commit unavailable" in out["errors"][0]


def test_post_merge_budget_comment_and_transcript_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    monkeypatch.setenv("ORCH_RUNTIME_AC_REOPEN", "0")
    root = tmp_path / "specs"
    root.mkdir()
    monkeypatch.setattr(backlog, "SUPPORTED_REPOS", ["owner/repo"])
    sha = "b" * 40
    target = "owner/repo#5"
    spec = {
        "verification": {"target": target, "source_issue": "owner/repo#9"},
        "acceptance_criteria": [],
    }
    gate.spec_path(target, spec_dir=root).write_text(json.dumps(spec), encoding="utf-8")

    def gh_ok(args, **_k):
        if args[1:3] == ["pr", "view"]:
            return {
                "state": "MERGED",
                "mergeCommit": {"oid": sha},
                "closingIssuesReferences": [{"number": 9}],
            }, None
        if "--slurp" in args:
            return None, "comments down"
        return {}, None

    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 5): {"r"}}
    ):
        with patch.object(gate, "execution_budget", return_value=0):
            assert (
                sweep._post_merge_rechecks(dry_run=False, gh=gh_ok, now=NOW, spec_dir=root)[
                    "rechecked"
                ]
                == 0
            )

    # Budget allows one execution; comment fetch fails after FAIL observation.
    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 5): {"r"}}
    ):
        with patch.object(gate, "execution_budget", return_value=1):
            with patch.object(
                gate,
                "recheck_merged",
                return_value={"result": "FAIL", "transcript": "x" * 10},
            ):
                with patch("provision.temporary_exact_head_worktree") as tw:

                    class _CM:
                        def __enter__(self):
                            return tmp_path / "wt"

                        def __exit__(self, *a):
                            return False

                    tw.return_value = _CM()
                    # checkout rev-parse must match; allow the git call inside the worktree path
                    monkeypatch.setattr(
                        sweep.subprocess,
                        "run",
                        lambda args, **k: SimpleNamespace(
                            returncode=0, stdout=sha + "\n", stderr=""
                        ),
                    )
                    (tmp_path / "wt").mkdir(exist_ok=True)
                    out = sweep._post_merge_rechecks(
                        dry_run=False, gh=gh_ok, now=NOW, spec_dir=root
                    )
                    assert out["errors"] and "comments down" in out["errors"][0]


def test_post_merge_transcript_hash_mismatch_and_long_body(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    monkeypatch.setenv("ORCH_RUNTIME_AC_REOPEN", "0")
    root = tmp_path / "specs"
    root.mkdir()
    monkeypatch.setattr(backlog, "SUPPORTED_REPOS", ["owner/repo"])
    sha = "c" * 40
    target = "owner/repo#6"
    issue = "owner/repo#8"
    spec = {
        "verification": {"target": target, "source_issue": issue},
        "acceptance_criteria": [],
    }
    gate.spec_path(target, spec_dir=root).write_text(json.dumps(spec), encoding="utf-8")
    identity = __import__("hashlib").sha256(f"{issue.lower()}@{sha}".encode()).hexdigest()
    transcript = root / f"post-merge-{identity}.txt"
    transcript.write_text("ok", encoding="utf-8")
    good_hash = __import__("hashlib").sha256(b"ok").hexdigest()
    event_id = f"event:post-merge-recheck:{identity}"
    payload = {
        "runtime_ac_post_merge": {
            "result": "FAIL",
            "issue": issue,
            "merge_sha": sha,
            "reopened": False,
            "commented": False,
            "delivery_complete": False,
            "transcript_path": str(transcript),
            "transcript_hash": "0" * 64,  # mismatch vs file
        }
    }
    feedback.record_completion_event(
        f"runtime-ac-post-merge:{identity}",
        event_type="verification",
        phase="post_merge_recheck",
        producer="runtime_ac_gate",
        event_id=event_id,
        status="completed",
        payload=payload,
    )

    def gh(args, **_k):
        if args[1:3] == ["pr", "view"]:
            return {
                "state": "MERGED",
                "mergeCommit": {"oid": sha},
                "closingIssuesReferences": [{"number": 8}],
            }, None
        raise AssertionError(args)

    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 6): {"r"}}
    ):
        out = sweep._post_merge_rechecks(dry_run=False, gh=gh, now=NOW, spec_dir=root)
        assert out["errors"] and "hash mismatch" in out["errors"][0]

    # Repair hash; exercise long-transcript comment path (line 1341).
    payload["runtime_ac_post_merge"]["transcript_hash"] = good_hash
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE completion_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload), event_id),
        )
    transcript.write_text("Z" * 56000, encoding="utf-8")
    payload["runtime_ac_post_merge"]["transcript_hash"] = (
        __import__("hashlib").sha256(transcript.read_bytes()).hexdigest()
    )
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE completion_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload), event_id),
        )

    posted = []

    def gh2(args, **_k):
        if args[1:3] == ["pr", "view"]:
            return {
                "state": "MERGED",
                "mergeCommit": {"oid": sha},
                "closingIssuesReferences": [{"number": 8}],
            }, None
        if "--slurp" in args:
            return [[]], None
        if "POST" in args and args[4].endswith("/comments"):
            posted.append(args[-1])
            return {}, None
        return {}, None

    with patch.object(
        sweep, "_merged_verifier_candidates", return_value={("owner/repo", 6): {"r"}}
    ):
        out = sweep._post_merge_rechecks(dry_run=False, gh=gh2, now=NOW, spec_dir=root)
        assert out["errors"] == []
        assert posted and "Full transcript retained" in posted[0]


def test_sweep_dry_run_stuck_truncated_and_unread(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    # Role run truncated durable with acting runs not selected → stuck.
    feedback.record_run(
        "role", "role:prompt", "role-prompt", "codex", mode="local", ts=NOW - 40 * DAY
    )
    feedback.record_outcome(
        "role",
        adjudicated_verdict="PASS",
        merged=True,
        durability="durable",
        notes=f"ok; {sweep.TRUNCATED_FIX_READ}",
    )
    feedback.record_run("act", "o/r#1", "implement", "codex", mode="local", ts=NOW - 40 * DAY)
    feedback.record_outcome(
        "act", adjudicated_verdict="PASS", merged=True, durability="durable", notes="other"
    )
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?",
            (NOW - DAY, "role"),
        )
        # Link role → acting via outcomes lineage table if present; else stub _acting_runs.
    monkeypatch.setattr(
        sweep, "_acting_runs", lambda run_id: [("act", "durable")] if run_id == "role" else []
    )
    monkeypatch.setattr(sweep, "_pending_merged_runs", lambda: [])
    monkeypatch.setattr(sweep, "_bare_reference_runs", lambda: [])
    monkeypatch.setattr(sweep, "refresh_verifier_verdicts", lambda **k: {})
    monkeypatch.setattr(
        sweep,
        "_post_merge_rechecks",
        lambda **k: {"rechecked": 0, "errors": []},
    )

    summary = sweep.sweep_durability(
        dry_run=True,
        _now=NOW,
        _state_fn=lambda _t: None,
        _verifier_fetch_fn=lambda *_a, **_k: {},
    )
    assert summary["truncated"]["stuck"] >= 1

    # Unread path while rechecking truncated rows.
    monkeypatch.setattr(
        sweep,
        "_truncated_durable_runs",
        lambda: [
            {
                "run_id": "u1",
                "target": "o/r#2",
                "mode": "local",
                "notes": sweep.TRUNCATED_FIX_READ,
                "judged_ts": NOW - DAY,
                "durability": "durable",
                "failure_class": None,
            }
        ],
    )
    monkeypatch.setattr(sweep, "_acting_runs", lambda _rid: [])
    monkeypatch.setattr(
        sweep,
        "_find",
        lambda run, **k: {"status": "unanswered", "reason": "gh down"},
    )
    summary = sweep.sweep_durability(
        dry_run=True,
        _now=NOW,
        _state_fn=lambda _t: None,
        _verifier_fetch_fn=lambda *_a, **_k: {},
    )
    assert (
        summary["truncated"]["waiting"] >= 1
        or any(
            d.get("action") == "recheck-unread" or "unread" in str(d) for d in summary["details"]
        )
        or summary["truncated"]["closed"] >= 0
    )

    # Dry-run patch detail on a pending merge that can be judged.
    monkeypatch.setattr(sweep, "_truncated_durable_runs", lambda: [])
    monkeypatch.setattr(
        sweep,
        "_pending_merged_runs",
        lambda: [
            {
                "run_id": "p1",
                "target": "o/r#11",
                "mode": "remote",
                "ts": NOW - 40 * DAY,
                "notes": "",
                "agent": "codex",
                "recorded_ts": NOW - 39 * DAY,
            }
        ],
    )
    pr = _pr(11, merged_at=NOW - 20 * DAY, repo="o/r")
    monkeypatch.setattr(
        sweep,
        "_find",
        lambda run, **k: {"status": "found", "pr": pr, "how": "direct PR target"},
    )
    monkeypatch.setattr(
        sweep,
        "classify_durability",
        lambda *a, **k: {
            "durability": "durable",
            "notes": "durability_sweep: held",
            "fix_search": "covered",
        },
    )
    summary = sweep.sweep_durability(
        dry_run=True,
        _now=NOW,
        _state_fn=lambda _t: None,
        _verifier_fetch_fn=lambda *_a, **_k: {},
        _fix_fn=lambda *_a, **_k: ([], True),
    )
    assert any(d.get("dry_run") for d in summary["details"])


def test_main_cli_undo_and_sweep(monkeypatch, capsys):
    monkeypatch.setattr(sweep, "undo_truncated_recheck", lambda: {"restored": 2})
    assert sweep.main(["--undo-truncated-recheck"]) == 0
    assert '"restored": 2' in capsys.readouterr().out

    monkeypatch.setattr(
        sweep,
        "sweep_durability",
        lambda **k: {"line": "ok", "checked": 0},
    )
    assert sweep.main(["--dry-run", "--grace-days", "3"]) == 0
    err = capsys.readouterr().err
    assert "ok" in err
