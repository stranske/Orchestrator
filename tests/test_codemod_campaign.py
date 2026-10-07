from __future__ import annotations

import base64
import copy
import json

import pytest

import codemod_lane as lane
import range_lane_rollout as rollout


@pytest.fixture
def campaign():
    import paths

    return json.loads((paths.REPO_ROOT / "campaigns/gitignore-caches-2026-10.json").read_text())


@pytest.fixture
def github(campaign, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    import paths

    validator = (paths.REPO_ROOT / ".github/scripts/issue_format.py").read_text()
    issues = {}
    calls = []
    ignores = {repo: "existing/\n.mypy_cache/\n" for repo in campaign["campaign"]["repos"]}

    def gh(args):
        calls.append(args)
        if args[0] == "api" and "/contents/" in args[1]:
            repo, file = args[1].removeprefix("repos/").split("/contents/")
            content = (
                ignores[repo]
                if file == ".gitignore"
                else (
                    validator
                    if file.endswith("issue_format.py")
                    else "actual target format contract"
                )
            )
            return {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
        if args[:2] == ["issue", "list"]:
            repo = args[args.index("--repo") + 1]
            return [copy.deepcopy(issues[repo])] if repo in issues else []
        if args[0] == "api" and args[1].endswith("/comments"):
            return {"id": 1}
        if args[:2] == ["label", "list"]:
            return [{"name": "refactor"}]
        if args[0] == "api" and args[1].endswith("/labels"):
            return {"name": "codemod"}
        if args[0] == "api":
            repo = args[1].removeprefix("repos/").removesuffix("/issues")
            body = next(a.removeprefix("body=") for a in args if a.startswith("body="))
            number = len(issues) + 10
            issues[repo] = {
                "number": number,
                "body": body,
                "state": "OPEN",
                "title": "Add ignores",
                "labels": [{"name": "codemod"}],
                "url": f"https://github.com/{repo}/issues/{number}",
            }
            return {"number": number, "html_url": issues[repo]["url"]}
        if args[:2] == ["issue", "view"]:
            return copy.deepcopy(issues[args[args.index("--repo") + 1]])
        if args[:2] == ["pr", "list"]:
            return []
        raise AssertionError(args)

    import backlog

    monkeypatch.setattr(backlog, "load_scoped_blockers", lambda: {})
    return gh, calls, ignores, issues


def test_the_campaign_validates_and_targets_only_missing_entries(campaign, github):
    gh, calls, ignores, _ = github
    assert campaign["campaign"]["repos"] == [
        "stranske/Counter_Risk",
        "stranske/Manager-Database",
        "stranske/Inv-Man-Intake",
        "stranske/trip-planner",
        "stranske/learning-management-system",
        "stranske/Pension-Data",
    ]
    assert campaign["ignore_entries"] == [
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        "coverage.xml",
        ".coverage",
    ]
    assert campaign["recipe"]["tool"] == "custom"
    assert campaign["rollout"]["pr_strategy"] == "per_repo"
    assert campaign["add_only"] is True
    assert lane.validate_campaign(campaign) == []
    for original in ignores.values():
        modified = lane.append_missing_ignores(original)
        assert modified.startswith(original)
        assert modified.count(".mypy_cache/") == 1
        assert lane.append_missing_ignores(modified) == modified
        assert all(e in modified.splitlines() for e in lane.IGNORE_ENTRIES)
    program = lane.file_targets(campaign, gh=gh)
    assert set(program["repos"]) == set(campaign["campaign"]["repos"])
    assert all(r["missing"] == list(lane.IGNORE_ENTRIES[1:]) for r in program["repos"].values())
    assert all(r["durable"] is None and r["cost_usd"] is None for r in program["repos"].values())
    lane.file_targets(campaign, gh=gh)
    creates = [a for a in calls if a[0] == "api" and a[1].endswith("/issues")]
    assert len(creates) == 6
    assert sum(a[0] == "api" and a[1].endswith("/labels") for a in calls) == 6
    assert sum(a[0] == "api" and a[1].endswith("/comments") for a in calls) == 1

    def unexpected_github_call(args):
        pytest.fail(f"invalid campaign reached GitHub: {args}")

    for instruction in [
        "Remove an existing ignore line.",
        "Overwrite .gitignore with the five entries.",
        "Truncate .gitignore before appending the entries.",
        "Erase an existing ignore line.",
        "Drop an existing ignore line.",
    ]:
        broken = copy.deepcopy(campaign)
        broken["delegate_prompt"] += " " + instruction
        assert "delegate_prompt contradicts the add-only contract" in lane.validate_campaign(broken)
        with pytest.raises(ValueError, match="add-only contract"):
            lane.file_targets(broken, gh=unexpected_github_call)


@pytest.mark.parametrize("original", ["", "x", "x\r\n", "# note\n!keep\n", " .coverage \n"])
def test_preserves_original_content_and_is_idempotent(original):
    modified = lane.append_missing_ignores(original)
    assert modified.startswith(original)
    assert lane.append_missing_ignores(modified) == modified
    if "\r\n" in original:
        assert "\n" not in modified.replace("\r\n", "")


def test_already_complete_repositories_are_not_filed(campaign, github):
    gh, calls, ignores, _ = github
    first = campaign["campaign"]["repos"][0]
    ignores[first] = "\n".join(lane.IGNORE_ENTRIES) + "\n"
    program = lane.file_targets(campaign, gh=gh)
    assert program["repos"][first]["state"] == "already-complete"
    assert "target" not in program["repos"][first]
    assert program["repos"][first]["merged"] is None
    assert program["repos"][first]["durable"] is None
    assert program["repos"][first]["cost_usd"] is None
    assert sum(a[0] == "api" and a[1].endswith("/issues") for a in calls) == 5


def test_target_format_failure_prevents_issue_creation(campaign, github):
    gh, calls, _, _ = github

    def invalid(args):
        if args[0] == "api" and args[1].endswith("issue_format.py"):
            return {
                "encoding": "base64",
                "content": base64.b64encode(b"raise SystemExit(1)").decode(),
            }
        return gh(args)

    with pytest.raises(ValueError, match="issue format rejected"):
        lane.file_targets(campaign, gh=invalid)
    assert not any(a[0] == "api" and a[1].endswith("/issues") for a in calls)


def test_rollout_with_a_campaign_input_previews_exactly_those_targets_and_refuses_without_the_window(
    campaign, github, monkeypatch, capsys
):
    gh, calls, _, issues = github
    lane.file_targets(campaign, gh=gh)
    monkeypatch.setattr(lane, "_gh", gh)
    monkeypatch.setattr(
        rollout,
        "_load_backlog_payload",
        lambda **kw: pytest.fail("campaign broadened into discovery"),
    )
    monkeypatch.setattr(rollout.router, "learned_ranks", lambda: {})
    seen = []

    def plan(items, capacity, **kw):
        seen.extend(items)
        return {
            "assignments": [
                {"target": i["target"], "task_type": "codemod", "lane": "opener", "agent": "gemini"}
                for i in items
            ]
        }

    monkeypatch.setattr(rollout.router, "plan", plan)
    monkeypatch.setattr(rollout, "_dispatch_preview", lambda d: list(d["assignments"]))
    result = rollout.build_rollout(campaign=campaign, max_dispatches=6, capacity_payload={})
    assert {r["target"] for r in seen} == {
        r["target"] for r in lane.file_targets(campaign, gh=gh)["repos"].values()
    }
    assert result["backlog_source"] == "campaign:gitignore-caches-2026-10"
    assert len(result["dispatch_preview"]) == 6
    assert all(
        "Implement this add-only campaign" in a["prompt"] for a in result["decision"]["assignments"]
    )
    monkeypatch.delenv(rollout.ENV_FLAG, raising=False)
    monkeypatch.setattr(
        rollout, "build_rollout", lambda **kw: pytest.fail("ungated apply reached router")
    )
    assert (
        rollout.main(["--apply", "--confirm-rollout", "--campaign", "not-read.json", "--json"]) == 2
    )
    assert "active dispatch requires" in capsys.readouterr().out


def test_foreign_router_assignment_is_rejected():
    decision, rejected = rollout._sanitize_decision(
        {
            "assignments": [
                {
                    "target": "wrong/repo#1",
                    "task_type": "codemod",
                    "lane": "opener",
                    "agent": "gemini",
                }
            ]
        },
        {"codemod"},
        {"allowed/repo#2"},
    )
    assert decision["assignments"] == []
    assert "escaped" in rejected[0]["reason"]


def test_open_linked_pr_and_human_hold_prevent_duplicate_dispatch(campaign, github, monkeypatch):
    gh, _, _, issues = github
    lane.file_targets(campaign, gh=gh)
    first, second = campaign["campaign"]["repos"][:2]
    issues[second]["labels"] = [{"name": "needs-human"}]

    def linked(args):
        if args[:2] == ["pr", "list"] and args[args.index("--repo") + 1] == first:
            return [
                {
                    "number": 9,
                    "state": "OPEN",
                    "mergedAt": None,
                    "closingIssuesReferences": [{"number": issues[first]["number"]}],
                }
            ]
        return gh(args)

    result = lane.campaign_backlog(campaign, gh=linked)
    assert len(result["items"]) == 4
    assert not any(i["target"].startswith((first + "#", second + "#")) for i in result["items"])


def test_campaign_receipt_cannot_escape_repository(campaign, github):
    gh, _, _, _ = github
    program = lane.file_targets(campaign, gh=gh)
    first = campaign["campaign"]["repos"][0]
    program["repos"][first]["target"] = "other/repo#44"
    lane._write_program(lane.campaign_program_path(campaign), program)
    with pytest.raises(ValueError, match="escaped"):
        lane.campaign_backlog(campaign, gh=gh)


def test_attributed_outcomes_preserve_unknown_and_complete_cost(tmp_path, monkeypatch):
    import sqlite3

    import feedback

    path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    prs = [{"number": 91}]
    assert lane.campaign_measure("owner/repo#7", prs)["durable"] is None
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE runs(run_id TEXT, target TEXT, pr_number INTEGER); CREATE TABLE outcomes(run_id TEXT, merged INTEGER, durability TEXT); CREATE TABLE costs(run_id TEXT,cost_usd REAL,source TEXT);"
        )
        conn.execute("INSERT INTO runs VALUES ('run','owner/repo#7',91)")
        conn.execute("INSERT INTO outcomes VALUES ('run',1,'pending')")
        conn.execute("INSERT INTO costs VALUES ('run',0,'ledger')")
    result = lane.campaign_measure("owner/repo#7", prs)
    assert result["durable"] is None and result["cost_usd"] is None
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE outcomes SET durability='durable'")
        conn.execute(
            "UPDATE costs SET cost_usd=2.5,source=?", (next(iter(feedback.COMPLETE_COST_SOURCES)),)
        )
    result = lane.campaign_measure("owner/repo#7", prs)
    assert result["durable"] is True and result["cost_usd"] == 2.5
    assert lane.campaign_measure("owner/repo#7", [{"number": 92}])["durable"] is None


def test_partial_filing_failure_reuses_prior_issue_receipts(campaign, github):
    gh, calls, _, _ = github
    second = campaign["campaign"]["repos"][1]

    def failing(args):
        if args[0] == "api" and args[1] == f"repos/{second}/contents/.gitignore":
            raise RuntimeError("temporary API failure")
        return gh(args)

    with pytest.raises(RuntimeError, match="temporary API"):
        lane.file_targets(campaign, gh=failing)
    saved = json.loads(lane.campaign_program_path(campaign).read_text())
    assert len(saved["repos"]) == 1
    lane.file_targets(campaign, gh=gh)
    assert sum(a[0] == "api" and a[1].endswith("/issues") for a in calls) == 6


def test_no_confirmation_refuses_even_with_window(monkeypatch, capsys):
    monkeypatch.setenv(rollout.ENV_FLAG, "1")
    monkeypatch.setattr(
        rollout, "build_rollout", lambda **kw: pytest.fail("unconfirmed apply reached router")
    )
    assert rollout.main(["--apply", "--campaign", "not-read.json"]) == 2


def test_invalid_campaign_cannot_broaden_scope(campaign):
    for key, value in [("add_only", False), ("ignore_entries", ["wrong/"])]:
        broken = copy.deepcopy(campaign)
        broken[key] = value
        assert lane.validate_campaign(broken)
    broken = copy.deepcopy(campaign)
    broken["scope"]["include_globs"] = [".github/workflows/*"]
    assert lane.validate_campaign(broken)
    broken = copy.deepcopy(campaign)
    broken["campaign"]["repos"].append(broken["campaign"]["repos"][0])
    assert lane.validate_campaign(broken)


@pytest.mark.parametrize("empty", [{"body": None}, {}])
def test_empty_issue_bodies_cannot_crash_campaign_discovery(campaign, github, empty):
    gh, _calls, _ignores, _issues = github

    def with_empty_issue(args):
        result = gh(args)
        if args[:2] == ["issue", "list"]:
            return [*result, {"number": 999, **empty}]
        return result

    program = lane.file_targets(campaign, gh=with_empty_issue)
    assert len(program["repos"]) == len(campaign["campaign"]["repos"])


@pytest.mark.parametrize("empty", [{"body": None}, {}])
def test_empty_target_body_is_missing_provenance(campaign, github, empty):
    gh, _calls, _ignores, issues = github
    lane.file_targets(campaign, gh=gh)
    row = next(iter(issues.values()))
    row.pop("body")
    row.update(empty)
    with pytest.raises(ValueError, match="campaign provenance missing"):
        lane.campaign_backlog(campaign, gh=gh)


@pytest.mark.parametrize("explicit", [False, True])
def test_blocked_apply_preserves_preview_receipt_without_explicit_record(
    campaign, tmp_path, monkeypatch, capsys, explicit
):
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(json.dumps(campaign))
    monkeypatch.setenv(rollout.ENV_FLAG, "1")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        rollout,
        "build_rollout",
        lambda **kwargs: {
            "campaign": {"campaign_id": campaign["campaign"]["id"], "repos": {}},
            "eligible": False,
            "decision": {"assignments": []},
        },
    )
    writes = []
    monkeypatch.setattr(lane, "_write_program", lambda *args: writes.append(args))
    monkeypatch.setattr(
        rollout.dispatcher, "run", lambda **kwargs: pytest.fail("blocked apply dispatched")
    )
    argv = ["--apply", "--confirm-rollout", "--campaign", str(campaign_path), "--json"]
    if explicit:
        argv.append("--record-campaign")
    assert rollout.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["dispatch_result"]["blocked"]
    assert bool(writes) is explicit


def test_whitespace_decoy_does_not_replace_effective_exact_ignore(tmp_path, campaign, github):
    import os
    import subprocess

    git_env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    ):
        git_env.pop(name, None)

    original = " .coverage \n"
    repaired = lane.append_missing_ignores(original)
    assert repaired.startswith(original)
    assert all(entry in repaired.splitlines() for entry in lane.IGNORE_ENTRIES)
    assert lane.append_missing_ignores(repaired) == repaired
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env)
    (tmp_path / ".gitignore").write_text(repaired)
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--no-index", ".coverage"],
            capture_output=True,
            text=True,
            env=git_env,
        ).returncode
        == 0
    )
    body = lane.target_issue_body(campaign, "stranske/Ready", original)
    scope = body.split("## Scope\n", 1)[1].split("\n## Non-Goals", 1)[0]
    assert "`.coverage`" in scope

    gh, _calls, ignores, _issues = github
    repo = campaign["campaign"]["repos"][0]
    ignores[repo] += original
    assert ".coverage" in lane.file_targets(campaign, gh=gh)["repos"][repo]["missing"]


@pytest.mark.parametrize("durability", ["pending", "durable"])
def test_pr_target_receipts_preserve_unobserved_source_attempt_cost(
    tmp_path, monkeypatch, durability
):
    import sqlite3

    import feedback

    path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE runs(run_id TEXT, target TEXT, pr_number INTEGER);"
            "CREATE TABLE outcomes(run_id TEXT, merged INTEGER, durability TEXT);"
            "CREATE TABLE costs(run_id TEXT,cost_usd REAL,source TEXT);"
        )
        conn.execute("INSERT INTO runs VALUES ('keeper','owner/repo#91',91)")
        conn.execute("INSERT INTO outcomes VALUES ('keeper',1,?)", (durability,))
        conn.execute(
            "INSERT INTO costs VALUES ('keeper',1.25,?)",
            (next(iter(feedback.COMPLETE_COST_SOURCES)),),
        )
        conn.execute("INSERT INTO runs VALUES ('decoy','other/repo#91',91)")
        conn.execute("INSERT INTO outcomes VALUES ('decoy',1,'broke_later')")
        conn.execute(
            "INSERT INTO costs VALUES ('decoy',500,?)",
            (next(iter(feedback.COMPLETE_COST_SOURCES)),),
        )
    result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
    assert result["durable"] is (True if durability == "durable" else None)
    assert result["observed_complete_cost_usd"] == 1.25
    assert result["cost_usd"] is None
    assert "source" in result["measurement"].lower()


def test_campaign_measure_combines_source_and_linked_pr_attempts(tmp_path, monkeypatch):
    import sqlite3

    import feedback

    path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    source = next(iter(feedback.COMPLETE_COST_SOURCES))
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE runs(run_id TEXT, target TEXT, pr_number INTEGER);"
            "CREATE TABLE outcomes(run_id TEXT, merged INTEGER, durability TEXT);"
            "CREATE TABLE costs(run_id TEXT,cost_usd REAL,source TEXT);"
        )
        for run, target, pr, merged, cost in [
            ("opener", "owner/repo#7", 91, 1, 2.5),
            ("keeper", "owner/repo#91", 91, 1, 1.25),
            ("failed", "owner/repo#7", None, 0, 0.5),
        ]:
            conn.execute("INSERT INTO runs VALUES (?,?,?)", (run, target, pr))
            conn.execute("INSERT INTO outcomes VALUES (?,?,'durable')", (run, merged))
            conn.execute("INSERT INTO costs VALUES (?,?,?)", (run, cost, source))
    result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
    assert result["durable"] is True
    assert result["cost_usd"] == 4.25
    assert result["observed_complete_cost_usd"] == 4.25
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE costs SET source='ledger' WHERE run_id='failed'")
    assert lane.campaign_measure("owner/repo#7", [{"number": 91}])["cost_usd"] is None


def test_campaign_measure_rejects_pr_target_number_mismatch(tmp_path, monkeypatch):
    import sqlite3

    import feedback

    path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE runs(run_id TEXT, target TEXT, pr_number INTEGER);"
            "CREATE TABLE outcomes(run_id TEXT, merged INTEGER, durability TEXT);"
            "CREATE TABLE costs(run_id TEXT,cost_usd REAL,source TEXT);"
        )
        conn.execute("INSERT INTO runs VALUES ('wrong','owner/repo#91',92)")
        conn.execute("INSERT INTO outcomes VALUES ('wrong',1,'durable')")
        conn.execute(
            "INSERT INTO costs VALUES ('wrong',5,?)", (next(iter(feedback.COMPLETE_COST_SOURCES)),)
        )
    result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
    assert result["durable"] is None and result["cost_usd"] is None
    assert "mismatch" in result["measurement"]


def test_observed_cost_survives_missing_merged_pr_receipt(tmp_path, monkeypatch):
    import sqlite3

    import feedback

    path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE runs(run_id TEXT, target TEXT, pr_number INTEGER);"
            "CREATE TABLE outcomes(run_id TEXT, merged INTEGER, durability TEXT);"
            "CREATE TABLE costs(run_id TEXT,cost_usd REAL,source TEXT);"
        )
        conn.execute("INSERT INTO runs VALUES ('keeper','owner/repo#91',91)")
        conn.execute("INSERT INTO outcomes VALUES ('keeper',1,'durable')")
        conn.execute(
            "INSERT INTO costs VALUES ('keeper',1.25,?)",
            (next(iter(feedback.COMPLETE_COST_SOURCES)),),
        )
    result = lane.campaign_measure("owner/repo#7", [{"number": 91}, {"number": 92}])
    assert result["observed_complete_cost_usd"] == 1.25
    assert result["cost_usd"] is None
    assert result["durable"] is None
    assert "incomplete Brain attribution" in result["measurement"]
