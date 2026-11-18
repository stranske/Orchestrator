"""Open PRs no lane journal names are an FYI measurement; unread journals never mean "unowned"."""

import json
from datetime import datetime, timezone

import pytest

import switch_review

NOW = 1_800_000_000
DAY = 86400
REPO = "stranske/Workflows"


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _roots(tmp_path, opener=(), closer=()):
    roots = {}
    for lane, targets in (("opener-core", opener), ("closer-core", closer)):
        root = tmp_path / lane
        root.mkdir()
        rounds = [
            {"ts": _iso(NOW - 3600), "items": [{"target": t, "action": "skip"} for t in targets]}
        ]
        (root / "rounds.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rounds), encoding="utf-8"
        )
        roots[lane] = root
    return roots


def _pr(number, *, idle_days=5, branch="codex/issue-1", labels=(), closes=(), draft=False):
    stamp = NOW - idle_days * DAY
    return {
        "number": number,
        "headRefName": branch,
        "isDraft": draft,
        "createdAt": _iso(stamp - DAY),
        "labels": [{"name": name} for name in labels],
        "commits": [{"committedDate": _iso(stamp)}],
        "closingIssuesReferences": [
            {
                "number": n,
                "repository": {"name": r.split("/")[1], "owner": {"login": r.split("/")[0]}},
            }
            for r, n in closes
        ],
    }


def _gh(prs_by_repo, calls=None):
    def fake(args, **_kwargs):
        if calls is not None:
            calls.append(args)
        repo = args[args.index("--repo") + 1]
        value = prs_by_repo[repo]
        if isinstance(value, str):
            return False, "", value
        return True, json.dumps(value), ""

    return fake


def test_an_idle_pr_no_lane_named_is_flagged(tmp_path):
    section = switch_review.unowned_open_prs(
        now=NOW,
        roots=_roots(tmp_path),
        gh_fn=_gh({REPO: [_pr(4714, branch="automation_run/x")]}),
        repos=[REPO],
    )
    assert section["status"] == "ok"
    assert section["suspect"] == 1
    row = section["rows"][0]
    assert (row["repo"], row["number"], row["idle_days"], row["closes"]) == (REPO, 4714, 5.0, [])
    text = "\n".join(switch_review.format_unowned_open_prs(section))
    assert "1 SUSPECT" in text and "stranske/Workflows#4714" in text and "closes no issue" in text


def test_a_pr_or_its_closing_issue_named_by_a_lane_is_owned(tmp_path):
    roots = _roots(tmp_path, opener=[f"{REPO}#10"], closer=[f"{REPO}#20"])
    prs = [_pr(20), _pr(21, closes=[(REPO, 10)])]
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: prs}), repos=[REPO]
    )
    assert section["suspect"] == 0 and section["rows"] == []
    text = "\n".join(switch_review.format_unowned_open_prs(section))
    assert "unowned open PRs: none in 1 repos" in text


def test_a_cross_repo_closing_link_is_matched_by_its_own_repo(tmp_path):
    roots = _roots(tmp_path, opener=["stranske/Orchestrator#5"])
    prs = [_pr(1, closes=[("stranske/Orchestrator", 5)]), _pr(2, closes=[(REPO, 5)])]
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: prs}), repos=[REPO]
    )
    assert [row["number"] for row in section["rows"]] == [2]
    assert section["rows"][0]["closes"] == [f"{REPO}#5"]


def test_a_journal_round_older_than_the_window_does_not_own_a_pr(tmp_path):
    roots = _roots(tmp_path)
    old = {
        "ts": _iso(NOW - switch_review.UNOWNED_WINDOW_S - 60),
        "items": [{"target": f"{REPO}#7", "action": "skip"}],
    }
    (roots["closer-core"] / "rounds.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: [_pr(7)]}), repos=[REPO]
    )
    assert [row["number"] for row in section["rows"]] == [7]
    assert section["rounds_in_window"]["closer-core"] == 0


@pytest.mark.parametrize(
    "pr",
    [
        _pr(1, draft=True),
        _pr(2, branch="sync/workflows-candidate"),
        _pr(3, idle_days=0.5),
        _pr(4, idle_days=1, branch="dependabot/pip/x"),
    ],
    ids=["draft", "sync-branch", "pushed-recently", "young-dependabot"],
)
def test_drafts_sync_branches_recent_pushes_and_young_dependabot_are_not_flagged(tmp_path, pr):
    section = switch_review.unowned_open_prs(
        now=NOW, roots=_roots(tmp_path), gh_fn=_gh({REPO: [pr]}), repos=[REPO]
    )
    assert section["status"] == "ok" and section["suspect"] == 0 and section["held"] == []


def test_a_hold_label_lists_the_pr_without_counting_it(tmp_path):
    prs = [_pr(1196, labels=["agents:paused"]), _pr(4711, labels=["agents:keepalive"])]
    section = switch_review.unowned_open_prs(
        now=NOW, roots=_roots(tmp_path), gh_fn=_gh({REPO: prs}), repos=[REPO]
    )
    assert section["suspect"] == 1
    assert section["rows"][0]["route_labels"] == ["agents:keepalive"]
    assert [(row["number"], row["held_by"]) for row in section["held"]] == [
        (1196, ["agents:paused"])
    ]
    text = "\n".join(switch_review.format_unowned_open_prs(section))
    assert "labels agents:keepalive" in text and "held by a hold label, not counted: 1" in text


def test_an_unreadable_journal_is_unknown_and_reads_no_pr(tmp_path):
    roots = _roots(tmp_path)
    (roots["opener-core"] / "rounds.jsonl").unlink()
    calls = []
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: [_pr(1)]}, calls), repos=[REPO]
    )
    assert calls == []
    assert section["status"] == "unknown" and section["suspect"] == 0
    assert "opener-core" in section["measurement"]
    text = "\n".join(switch_review.format_unowned_open_prs(section))
    assert "unowned open PRs: unknown" in text and "none in" not in text


def test_a_failed_repo_is_unmeasured_and_known_suspects_stay(tmp_path):
    gh = _gh({REPO: [_pr(1)], "stranske/Template": "HTTP 502"})
    section = switch_review.unowned_open_prs(
        now=NOW, roots=_roots(tmp_path), gh_fn=gh, repos=[REPO, "stranske/Template"]
    )
    assert section["status"] == "unknown" and section["repos_checked"] == 1
    assert "stranske/Template: HTTP 502" in section["measurement"]
    assert section["suspect"] == 1


def test_a_list_that_is_not_json_is_unmeasured_not_a_crash(tmp_path):
    def gh(_args, **_k):
        return True, "not json", ""

    section = switch_review.unowned_open_prs(
        now=NOW, roots=_roots(tmp_path), gh_fn=gh, repos=[REPO]
    )
    assert section["status"] == "unknown" and section["repos_checked"] == 0
    assert "pr list unreadable" in section["measurement"]


def test_a_listing_that_fills_the_limit_is_incomplete_and_keeps_known_rows(tmp_path):
    named = [f"{REPO}#{n}" for n in range(1, switch_review.UNOWNED_PR_LIMIT)]
    roots = _roots(tmp_path, closer=named)
    prs = [_pr(n) for n in range(1, switch_review.UNOWNED_PR_LIMIT + 1)]
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: prs}), repos=[REPO]
    )
    # Every listed PR but the last is owned; an unread 101st could be unowned.
    assert [row["number"] for row in section["rows"]] == [switch_review.UNOWNED_PR_LIMIT]
    assert section["status"] == "unknown"
    assert "100-PR limit" in section["measurement"]
    text = "\n".join(switch_review.format_unowned_open_prs(section))
    assert "none in" not in text and "unknown" in text


@pytest.mark.parametrize(
    "drop, value",
    [
        ("commits", None),
        ("labels", None),
        ("closingIssuesReferences", None),
        ("commits", []),
        ("commits", [{"committedDate": "not a date"}]),
    ],
    ids=["no-commits-key", "no-labels-key", "no-links-key", "empty-commits", "bad-date"],
)
def test_a_pr_missing_evidence_is_unmeasured_and_others_still_count(tmp_path, drop, value):
    broken = _pr(1)
    if value is None:
        del broken[drop]
    else:
        broken[drop] = value
    section = switch_review.unowned_open_prs(
        now=NOW, roots=_roots(tmp_path), gh_fn=_gh({REPO: [broken, _pr(2)]}), repos=[REPO]
    )
    assert [row["number"] for row in section["rows"]] == [2]
    assert section["status"] == "unknown"
    assert f"{REPO}#1: unreadable" in section["measurement"]


def test_a_commit_list_at_the_cap_is_unmeasured_not_idle(tmp_path):
    capped = _pr(1)
    capped["commits"] = capped["commits"] * switch_review.UNOWNED_COMMIT_CAP
    section = switch_review.unowned_open_prs(
        now=NOW, roots=_roots(tmp_path), gh_fn=_gh({REPO: [capped]}), repos=[REPO]
    )
    assert section["rows"] == [] and section["status"] == "unknown"
    assert "newest push may be past the cap" in section["measurement"]


def _write_rounds(root, stamps):
    (root / "rounds.jsonl").write_text(
        "".join(
            json.dumps({"ts": _iso(stamp), "items": [{"target": "x/y#1", "action": "skip"}]}) + "\n"
            for stamp in stamps
        ),
        encoding="utf-8",
    )


def test_a_full_tail_that_misses_the_window_narrows_it_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_review, "UNOWNED_JOURNAL_LINES", 3)
    roots = _roots(tmp_path)
    # Opener's three-line tail reaches back 10 h, closer's 30 h: the shorter one bounds the window.
    _write_rounds(roots["opener-core"], [NOW - 10 * 3600, NOW - 5 * 3600, NOW - 3600])
    _write_rounds(roots["closer-core"], [NOW - 30 * 3600, NOW - 20 * 3600, NOW - 3600])
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: []}), repos=[REPO]
    )
    assert section["window_hours"] == 10.0
    assert section["window_note"] == "journal tails reach back 10.0h, less than 72h"
    text = "\n".join(switch_review.format_unowned_open_prs(section))
    assert "note: journal tails reach back 10.0h" in text


def test_a_full_tail_that_reaches_the_window_edge_adds_no_note(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_review, "UNOWNED_JOURNAL_LINES", 2)
    roots = _roots(tmp_path)
    edge = NOW - switch_review.UNOWNED_WINDOW_S
    _write_rounds(roots["opener-core"], [edge, NOW - 3600])
    _write_rounds(roots["closer-core"], [edge - 3600, NOW - 3600])
    section = switch_review.unowned_open_prs(
        now=NOW, roots=roots, gh_fn=_gh({REPO: []}), repos=[REPO]
    )
    assert section["window_hours"] == 72 and "window_note" not in section
    assert section["rounds_in_window"] == {"opener-core": 2, "closer-core": 1}


def test_the_weekly_caller_carries_the_section_and_the_suite_default_is_off(monkeypatch, capsys):
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setenv("ORCH_DISABLE_STEPS", "issue-size-quality")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(switch_review, "credential_exposure", lambda **_k: {"status": "disabled"})
    monkeypatch.setattr(
        switch_review, "fleet_delivery_measures", lambda **_k: {"status": "disabled"}
    )
    monkeypatch.setattr(switch_review, "review", lambda **_k: {"generated_at": NOW})
    sentinel = {"status": "ok", "sentinel": True}
    seen = []

    def fake_scan(**kwargs):
        seen.append(kwargs)
        return sentinel

    monkeypatch.setattr(switch_review, "unowned_open_prs", fake_scan)
    assert switch_review.main(["--json", "--env", "process"]) == 0
    assert seen == []  # conftest turns the live scan off for every test
    assert json.loads(capsys.readouterr().out)["unowned_open_prs"]["status"] == "disabled"

    monkeypatch.setenv("ORCH_UNOWNED_PR_SCAN", "1")
    assert switch_review.main(["--json", "--env", "process"]) == 0
    assert seen == [{"now": NOW}]
    assert json.loads(capsys.readouterr().out)["unowned_open_prs"] == sentinel


def test_a_scan_that_raises_keeps_the_weekly_report(monkeypatch, capsys):
    monkeypatch.setenv("ORCH_UNOWNED_PR_SCAN", "1")
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setenv("ORCH_DISABLE_STEPS", "issue-size-quality")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(switch_review, "credential_exposure", lambda **_k: {"status": "disabled"})
    monkeypatch.setattr(
        switch_review, "fleet_delivery_measures", lambda **_k: {"status": "disabled"}
    )
    monkeypatch.setattr(switch_review, "review", lambda **_k: {"generated_at": NOW})

    def boom(**_k):
        raise RuntimeError("backlog import failed")

    monkeypatch.setattr(switch_review, "unowned_open_prs", boom)
    assert switch_review.main(["--json", "--env", "process"]) == 0
    section = json.loads(capsys.readouterr().out)["unowned_open_prs"]
    assert section["status"] == "unknown" and "backlog import failed" in section["measurement"]
    assert "unknown" in "\n".join(switch_review.format_unowned_open_prs(section))
