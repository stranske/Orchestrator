"""Post-merge supervisor closure contracts; no live GitHub or Brain writes."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager

import pytest

import keepalive_supervisor as supervisor
import verifier_evidence

REPO = "stranske/Inv-Man-Intake"
TARGET = REPO + "#21"
SHA = "a" * 40


def closure_fixture(tmp_path, verdict="PASS"):
    decision = {
        "schema": verifier_evidence.MARKER,
        "repo": REPO,
        "pr": 21,
        "head_sha": "b" * 40,
        "evaluated_sha": SHA,
        "run_id": "10",
        "run_attempt": "1",
        "provider_verdicts": [verdict],
        "verdict": "PASS" if verdict == "PASS" else "NON_PASS",
        "ci_failed": False,
    }
    pr = {
        "number": 21,
        "state": "MERGED",
        "headRefOid": "b" * 40,
        "headRefName": "codex/issue-7-close",
        "mergeCommit": {"oid": SHA},
        "labels": {"nodes": [{"name": "agent:codex"}]},
        "closingIssuesReferences": {
            "nodes": [{"number": 7, "repository": {"nameWithOwner": REPO}}]
        },
        "comments": {"nodes": []},
    }
    if verdict != "MISSING":
        pr["comments"]["nodes"].append(
            {
                "author": {"login": "github-actions[bot]"},
                "url": f"https://github.com/{REPO}/pull/21#issuecomment-1",
                "body": "<!-- " + verifier_evidence.MARKER + " " + json.dumps(decision) + " -->",
            }
        )
    issue = {
        "number": 7,
        "state": "open",
        "updated_at": "2026-10-17T10:00:00Z",
        "labels": [],
        "body": "## Acceptance Criteria\n- `pytest tests/test_contract.py` passes.",
    }
    calls, recorded = [], []

    def gh(args):
        calls.append(args)
        if args[0] == "search":
            return [{"repository": {"nameWithOwner": REPO}, "number": 21}]
        if args[1].endswith("/comments"):
            if "-f" not in args:
                return [[]]
            pr["comments"]["nodes"].append(
                {"body": args[-1].removeprefix("body="), "author": {"login": "stranske"}}
            )
            return {}
        if "PATCH" in args:
            issue["state"] = "closed"
            return copy.deepcopy(issue)
        return copy.deepcopy(issue)

    options = dict(
        targets=[TARGET],
        env={supervisor.CLOSE_FLAG: "1", "ORCH_STATE_DIR": str(tmp_path)},
        gh=gh,
        pr_read=lambda target: copy.deepcopy(pr),
        ci_green=lambda repo, sha: True,
        gate=lambda *args: {"result": "PASS"},
        record=lambda *args: recorded.append(args),
        holds=lambda: set(),
    )
    return pr, issue, calls, recorded, options


def test_close_verifier_pass_confirms_action_and_records_outcome(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    options["gate"] = lambda *args: pytest.fail("PASS must not buy a redundant test execution")
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 1 and issue["state"] == "closed"
    assert recorded == [(REPO + "#7", SHA, "verifier PASS")]
    assert sum("PATCH" in call for call in calls) == 1
    assert any(call[1] == f"repos/{REPO}/issues/7/comments" and "-f" in call for call in calls)
    # A second run reconciles the same identity, without another external action.
    supervisor.close_verified_issues(**options)
    assert sum("PATCH" in call for call in calls) == 1


def test_close_gate_rule_requires_green_merge_sha_ci_and_gate(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, "MISSING")
    evidence = []
    options["ci_green"] = lambda repo, sha: evidence.append((repo, sha)) or True
    options["gate"] = lambda target, source, sha: evidence.append(
        (target, source["number"], sha)
    ) or {"result": "PASS"}
    assert supervisor.close_verified_issues(**options)["closed"] == 1
    assert evidence == [(REPO, SHA), (TARGET, 7, SHA), (REPO, SHA)]
    assert recorded[0][2] == "merge-SHA acceptance gates and green CI"


@pytest.mark.parametrize("verdict", ["CONCERNS", "FAIL", "NON_PASS"])
def test_close_never_overrides_nonpass_with_green_gates(tmp_path, verdict):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, verdict)
    options["gate"] = lambda *args: pytest.fail("nonpass must veto gate execution")
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 0 and issue["state"] == "open" and not recorded
    assert result["skips"] == {f"verdict={verdict}": 1}


@pytest.mark.parametrize("verdict", ["ERROR", "MALFORMED"])
def test_close_unknown_verifier_is_not_missing(tmp_path, verdict):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, verdict)
    assert supervisor.close_verified_issues(**options)["closed"] == 0
    assert not recorded and not any("PATCH" in call for call in calls)


def test_close_later_legacy_concerns_veto_earlier_pass(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    pr["comments"]["nodes"].append(
        {
            "author": {"login": "github-actions[bot]"},
            "body": "## Verification report\nVerdict: CONCERNS",
            "url": "unused",
        }
    )
    assert supervisor.close_verified_issues(**options)["closed"] == 0


@pytest.mark.parametrize(
    "reason", ["ci not green", "no gate commands", "assertion failed", "commands_disabled"]
)
def test_close_reports_exact_gate_and_ci_skips(tmp_path, reason):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, "MISSING")
    options["ci_green"] = lambda *args: reason != "ci not green"
    options["gate"] = lambda *args: {"result": "UNRUNNABLE", "reason": reason}
    result = supervisor.close_verified_issues(**options)
    assert result["skips"] == {reason: 1} and not recorded


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("reason", ["verdict=CONCERNS", "no gate commands", "ci not green"])
def test_close_cli_prints_counted_skip_reasons(tmp_path, monkeypatch, capsys, reason, json_output):
    pr, issue, calls, recorded, options = closure_fixture(
        tmp_path, "CONCERNS" if reason == "verdict=CONCERNS" else "MISSING"
    )
    pr["closingIssuesReferences"]["nodes"].append(
        {"number": 8, "repository": {"nameWithOwner": REPO}}
    )
    original = options["gh"]

    def gh(args):
        source = original(args)
        return {**source, "number": int(args[1].rsplit("/", 1)[1])}

    options["gh"] = gh
    options["ci_green"] = lambda *args: reason != "ci not green"
    options["gate"] = lambda *args: {"result": "UNRUNNABLE", "reason": "no gate commands"}
    actor = supervisor.close_verified_issues
    monkeypatch.setattr(supervisor, "_capability_heartbeat", lambda *args: None)
    monkeypatch.setattr(supervisor, "close_verified_issues", lambda **kwargs: actor(**options))

    assert supervisor.main(["--close-verified"] + (["--json"] if json_output else [])) == 0
    output = capsys.readouterr().out
    if json_output:
        result = json.loads(output)
        assert result["closed"] == 0 and result["skips"] == {reason: 2}
    else:
        assert "supervisor closures: 0; errors=0;" in output
        assert f'"{reason}": 2' in output
    assert issue["state"] == "open" and not recorded
    assert not any("PATCH" in call for call in calls)


@pytest.mark.parametrize("change", ["issue", "verdict", "merge", "hold"])
def test_close_revalidates_after_running_gate(tmp_path, change):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, "MISSING")
    held = set()
    options["holds"] = lambda: held

    def gate(*args):
        if change == "issue":
            issue["updated_at"] = "2026-10-17T10:01:00Z"
        elif change == "verdict":
            pr["comments"]["nodes"].append(
                {
                    "author": {"login": "github-actions[bot]"},
                    "body": "Verification: CONCERNS",
                    "url": "unused",
                }
            )
        elif change == "merge":
            pr["mergeCommit"] = {"oid": "c" * 40}
        else:
            held.add(REPO.lower() + "#7")
        return {"result": "PASS"}

    options["gate"] = gate
    result = supervisor.close_verified_issues(**options)
    assert result["skips"] == {"evidence changed before closure": 1}
    assert issue["state"] == "open" and not recorded


def test_close_preserves_explicit_human_hold(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    issue["labels"] = [{"name": "needs-human"}]
    assert supervisor.close_verified_issues(**options)["skips"] == {"source issue held": 1}
    assert not recorded


def test_close_disabled_has_no_reads_or_state_writes(tmp_path):
    state = tmp_path / "absent"
    result = supervisor.close_verified_issues(
        env={"ORCH_STATE_DIR": str(state)}, gh=lambda *args: pytest.fail("flag off")
    )
    assert result["closed"] == 0 and not result["enabled"] and not state.exists()


def test_close_excluded_target_never_read(tmp_path):
    result = supervisor.close_verified_issues(
        targets=["stranske/Collab-Admin#1"],
        env={supervisor.CLOSE_FLAG: "1", "ORCH_STATE_DIR": str(tmp_path)},
        pr_read=lambda *args: pytest.fail("excluded repository"),
    )
    assert not result["items"]


def test_close_search_is_restricted_to_supported_repositories(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    options["targets"] = None
    supervisor.close_verified_issues(**options)
    search = calls[0]
    assert "--repo" in search and "stranske/collab-admin" not in search
    assert search[search.index("--limit") + 1] == "3"


def test_close_receipt_written_only_after_confirmed_close(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    original = options["gh"]

    def gh(args):
        if "PATCH" in args:
            return {"state": "open"}
        return original(args)

    options["gh"] = gh
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 0 and not recorded
    assert "not confirmed" in result["items"][0]["reason"]


@pytest.mark.parametrize(
    "verdict,blocker,expected",
    [
        ("PASS", None, 1),
        ("MISSING", None, 1),
        ("CONCERNS", None, 0),
        ("MISSING", "ci not green", 0),
        ("MISSING", "no gate commands", 0),
        ("PASS", "unconfirmed", 0),
    ],
)
def test_close_records_real_private_brain_monitor_outcome_once(
    tmp_path, monkeypatch, capsys, verdict, blocker, expected
):
    import time

    import capabilities
    import feedback
    import value_chain_monitor as monitor

    now = 1_792_238_400
    capability = "live-keepalive-supervisor"
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability(capability)
    cap.update(status="wired", capability_version_id="cv:supervisor-close")
    capabilities.save({capability: cap}, ledger)
    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(time, "time", lambda: float(now))
    # Initialize a private schema so zero outcomes is measured, not an unread Brain.
    with feedback._conn():
        pass
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, verdict)
    options["record"] = supervisor._closure_record
    options["ci_green"] = lambda *args: blocker != "ci not green"

    def gate(*args):
        if blocker:
            return {"result": "UNRUNNABLE", "reason": blocker}
        return {"result": "PASS"}

    options["gate"] = gate
    if blocker == "unconfirmed":
        original = options["gh"]
        options["gh"] = lambda args: {} if "PATCH" in args else original(args)

    def monitor_row():
        inputs = monitor._brain(feedback.DB_PATH, now - monitor.WINDOW_DAYS * 86400, now)
        assert not inputs["errors"]
        report = monitor.report(now=now, path=ledger, env={}, inputs=inputs)
        row = next(row for row in report["rows"] if row["capability"] == capability)
        lines = monitor.format_lines(report)
        line = next(line for line in lines if line.startswith(f"  {capability}:"))
        assert f"; outcome {row['outcome']};" in line
        return row

    assert monitor_row()["outcome"] == 0
    before = monitor.supervisor_closure_acceptance(now, now=now)
    assert before["outcome"] == 0 and before["status"] == "pending"
    assert monitor.main(["--supervisor-enabled-at", str(now)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "pending"
    assert supervisor.close_verified_issues(**options)["closed"] == expected
    row = monitor_row()
    assert row["outcome"] == row["acted_on"] == expected
    # Closure credit must not claim a new successful implementation merge.
    assert row["outcome_success"] == 0
    database_before = feedback.DB_PATH.read_bytes()
    receipt = monitor.supervisor_closure_acceptance(now, now=now)
    assert receipt["outcome"] == expected
    assert receipt["status"] == ("met" if expected else "pending")
    assert monitor.main(["--supervisor-enabled-at", str(now)]) == (0 if expected else 1)
    assert json.loads(capsys.readouterr().out) == receipt
    assert feedback.DB_PATH.read_bytes() == database_before
    # Checking after the deadline retains timely evidence; an older closure does
    # not satisfy a later enablement window, even while the rolling row is positive.
    assert monitor.supervisor_closure_acceptance(now, now=now + 86401)["status"] == (
        "met" if expected else "deadline_missed"
    )
    later = monitor.supervisor_closure_acceptance(now + 1, now=now + 86401)
    assert later["outcome"] == 0 and later["status"] == "deadline_missed"
    missing = tmp_path / "missing.db"
    unread = monitor.supervisor_closure_acceptance(now, now=now, db=missing)
    assert unread["outcome"] is None and unread["status"] == "unmeasured"
    assert unread["errors"] and not missing.exists()
    for invalid in (0, -1, now + 1):
        with pytest.raises(ValueError, match="enablement time"):
            monitor.supervisor_closure_acceptance(invalid, now=now)
    if expected:
        rule = "verifier PASS" if verdict == "PASS" else "merge-SHA acceptance gates and green CI"
        supervisor._closure_record(REPO + "#7", SHA, rule)
    supervisor.close_verified_issues(**options)
    assert monitor_row()["outcome"] == expected
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == expected
        if expected:
            row = conn.execute("SELECT adjudicated_verdict,notes FROM outcomes").fetchone()
            assert row[0] == "PASS" and "Not a new implementation verdict" in row[1]
            conn.execute("UPDATE outcomes SET durability_checked_ts=?", (now + 86400,))
    if expected:
        assert monitor.supervisor_closure_acceptance(now, now=now + 86400)["status"] == "met"
        with feedback._conn() as conn:
            # An early action edge cannot launder a receipt recorded after 24h.
            conn.execute("UPDATE outcomes SET durability_checked_ts=?", (now + 86401,))
        late = monitor.supervisor_closure_acceptance(now, now=now + 86401)
        assert late["outcome"] == 0 and late["status"] == "deadline_missed"


def test_close_gate_uses_real_exact_merge_checkout_and_existing_runner(tmp_path, monkeypatch):
    import subprocess

    import provision

    checkout = tmp_path / "merge"
    (checkout / "tests").mkdir(parents=True)
    (checkout / "tests/test_contract.py").write_text(
        "def test_contract():\n    assert 2 + 2 == 4\n"
    )
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(args, cwd=checkout, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()

    @contextmanager
    def exact(target, requested):
        assert requested == sha
        yield checkout

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", exact)
    issue = {
        "target": REPO + "#7",
        "body": "## Acceptance Criteria\n- `pytest tests/test_contract.py` passes.",
    }
    assert supervisor._closure_gate(TARGET, issue, sha)["result"] == "PASS"
    (checkout / "tests/test_contract.py").write_text("def test_contract():\n    assert False\n")
    assert supervisor._closure_gate(TARGET, issue, sha)["result"] == "FAIL"


def test_close_gate_without_command_refuses_before_checkout(tmp_path, monkeypatch):
    import provision

    monkeypatch.setattr(
        provision, "temporary_exact_head_worktree", lambda *args: pytest.fail("no command")
    )
    result = supervisor._closure_gate(
        TARGET,
        {"target": REPO + "#7", "body": "## Acceptance Criteria\n- Owner checks panel."},
        SHA,
    )
    assert result["reason"] == "no gate commands"


def test_close_main_and_stage2_use_same_actor(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(supervisor, "_capability_heartbeat", lambda *args: None)
    monkeypatch.setattr(
        supervisor,
        "close_verified_issues",
        lambda **kwargs: called.append(kwargs) or {"closed": 0, "skips": {}},
    )
    monkeypatch.setattr(supervisor, "stage2_acquisition_plan", lambda **kwargs: {"plans": []})
    assert supervisor.main(["--close-verified", "--json"]) == 0
    assert supervisor.main(["--stage2-plan", "--json"]) == 0
    assert len(called) == 2


def test_close_limits_actual_closures_across_many_source_edges(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    pr["closingIssuesReferences"]["nodes"] = [
        {"number": number, "repository": {"nameWithOwner": REPO}} for number in range(7, 15)
    ]
    states = {}

    def gh(args):
        calls.append(args)
        if args[1].endswith("/comments"):
            return {} if "-f" in args else [[]]
        number = int(args[1].rsplit("/", 1)[1])
        if "PATCH" in args:
            states[number] = "closed"
        return {**issue, "number": number, "state": states.get(number, "open")}

    options["gh"] = gh
    assert supervisor.close_verified_issues(**options)["closed"] == 3
    assert len(recorded) == 3 and len(states) == 3


def test_close_record_failure_keeps_actual_action_and_budget(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)

    def unavailable(*args):
        raise OSError("private Brain unavailable")

    options["record"] = unavailable
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 1 and issue["state"] == "closed"
    assert result["items"][0]["outcome_error"] == "private Brain unavailable"


@pytest.mark.parametrize("kind", ["absent", "pending", "failed", "green"])
def test_close_ci_requires_actual_green_evidence_at_merge_sha(monkeypatch, kind):
    calls = []

    def gh(args, *, pages=False):
        calls.append(args)
        assert SHA in args[1]
        if args[1].endswith("/status"):
            return {"total_count": 0, "state": "pending"}
        check = {
            "status": "in_progress" if kind == "pending" else "completed",
            "conclusion": "failure" if kind == "failed" else "success",
        }
        return [{"check_runs": [] if kind == "absent" else [check]}]

    monkeypatch.setattr(supervisor, "_closure_gh", gh)
    assert supervisor._closure_ci_green(REPO, SHA) is (kind == "green")


def test_close_pr_adapter_reads_all_comments_and_binds_closing_edges(monkeypatch):
    calls = []

    def gh(args, *, pages=False):
        calls.append((args, pages))
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "number": 21,
                            "labels": {"pageInfo": {"hasNextPage": False}},
                            "closingIssuesReferences": {"pageInfo": {"hasNextPage": False}},
                        }
                    }
                }
            }
        return [
            [{"body": "first", "html_url": "one", "user": {"login": "owner"}}],
            [{"body": "last", "html_url": "two", "user": {"login": "github-actions[bot]"}}],
        ]

    monkeypatch.setattr(supervisor, "_closure_gh", gh)
    pr = supervisor._closure_pr(TARGET)
    assert [row["body"] for row in pr["comments"]["nodes"]] == ["first", "last"]
    assert calls[-1][1] is True


def test_close_unknown_owner_does_not_run_unrestricted_search(tmp_path):
    result = supervisor.close_verified_issues(
        owner="unknown-owner",
        env={supervisor.CLOSE_FLAG: "1", "ORCH_STATE_DIR": str(tmp_path)},
        gh=lambda *args: pytest.fail("unrestricted discovery"),
    )
    assert result["reason"] == "no supported repositories for owner"


def test_close_scan_can_pass_already_closed_prs_but_gate_attempts_are_bounded(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path, "MISSING")
    pr["closingIssuesReferences"]["nodes"] = [
        {"number": number, "repository": {"nameWithOwner": REPO}} for number in range(7, 15)
    ]

    def gh(args):
        number = int(args[1].rsplit("/", 1)[1])
        return {**issue, "number": number}

    attempted = []
    options.update(limit=20, gh=gh, gate=lambda *args: attempted.append(args) or {"result": "FAIL"})
    result = supervisor.close_verified_issues(**options)
    assert len(attempted) == 3 and result["gate_executions"] == 3
    assert result["skips"]["acceptance execution budget"] == 5


def test_close_discovery_limit_is_independent_of_action_limit(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    options.update(targets=None, limit=20)
    supervisor.close_verified_issues(**options)
    assert calls[0][calls[0].index("--limit") + 1] == "20"


@pytest.mark.parametrize("mode,expected_status", [("--stage2-plan", 0), ("--close-verified", 1)])
def test_closure_error_does_not_discard_stage2_plan(monkeypatch, capsys, mode, expected_status):
    monkeypatch.setattr(supervisor, "_capability_heartbeat", lambda *args: None)
    closure = {"closed": 0, "errors": 1, "skips": {"error: issue not found": 1}}
    monkeypatch.setattr(supervisor, "close_verified_issues", lambda **kwargs: closure)
    monkeypatch.setattr(
        supervisor, "stage2_acquisition_plan", lambda **kwargs: {"plans": ["retained"]}
    )
    assert supervisor.main([mode, "--json"]) == expected_status
    result = json.loads(capsys.readouterr().out)
    if mode == "--stage2-plan":
        assert result["plans"] == ["retained"]
        assert result["issue_closure"] == closure
    else:
        assert result == closure
