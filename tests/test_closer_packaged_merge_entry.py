"""Exercise the actual sealed subprocess entrypoints without a merge or provider."""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import closer_campaign as campaign
import closer_distribution_package as package
import closer_launcher as launcher

HEAD = "a" * 40


@pytest.fixture
def packaged(tmp_path):
    source = Path(campaign.__file__).parent
    public = package.build_public_runtime_descriptor(
        source,
        candidate_sha="b" * 40,
        state_versions={"campaign": 1, "release": 1},
        supported_core={
            "owner": "Workflows",
            "contract": campaign.CONTRACT,
            "core_version": "1",
            "package_digest": "c" * 64,
        },
    )
    runtime = tmp_path / "runtime"
    package.materialize_runtime_tree(source, runtime, public)
    host = package.host_binding_from_public(public, install_root=runtime)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HANDOFF_DIR": str(state / "handoff"),
        "ORCH_LOCAL_RUNTIME": str(state),
        "ORCH_STATE_DIR": str(state),
        "ORCH_FEEDBACK_DB": str(state / "feedback.sqlite"),
        "ORCH_RUNTIME_AC_SPEC_DIR": str(state / "specs"),
        "ORCH_FRONTEND_VERIFY_DIR": str(state / "missing-frontend-helper"),
    }
    return runtime, public, host, foreign, env


def invoke(packaged, entry, *args):
    runtime, _, _, cwd, env = packaged
    return subprocess.run(
        [sys.executable, "-B", str(runtime / entry), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def install_read_only_gh(packaged, tmp_path, scenario):
    """A fixed offline transport: unexpected commands cannot reach a real gh."""
    _, _, _, _, env = packaged
    fixture = tmp_path / "facts.json"
    log = tmp_path / "gh-calls.jsonl"
    now = time.time()
    old = "2020-01-01T00:00:00Z"
    pr = {
        "id": "PR_fixture",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "fixture-head",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "createdAt": old,
        "updatedAt": old,
        "headRepository": {"id": "R_fixture", "nameWithOwner": "owner/repo"},
        "headRef": {"target": {"oid": HEAD}},
        "readyForReview": {"nodes": []},
        "commits": {
            "nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"state": "SUCCESS"}}}]
        },
        "reviewThreads": {
            "totalCount": 0,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [],
        },
    }
    if scenario == "head_changed":
        pr["headRefOid"] = "d" * 40
    if scenario == "unresolved_thread":
        pr["reviewThreads"]["totalCount"] = 1
        pr["reviewThreads"]["nodes"] = [{"id": "T", "isResolved": False, "isOutdated": False}]
    if scenario == "incomplete_threads":
        pr["reviewThreads"]["totalCount"] = 1
    if scenario == "failed_checks":
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
    meta = {
        "title": "Routine fixture",
        "labels": [{"name": "runtime-ac"}] if scenario == "missing_ac_spec" else [],
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "closingIssuesReferences": [],
        "headRefName": "fixture-head",
    }
    activity = [
        {"timestamp": old, "ref": "refs/heads/fixture-head", "after": HEAD, "activity_type": "push"}
    ]
    if scenario == "missing_activity":
        activity = []
    if scenario == "review_floor":
        from datetime import datetime, timezone

        activity[0]["timestamp"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
    fixture.write_text(json.dumps({"pr": pr, "meta": meta, "activity": activity}))
    gh = Path(env["PATH"]) / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['FIXTURE_GH_LOG']).open('a') as f:\n"
        " f.write(json.dumps(args) + '\\n')\n"
        "facts = json.loads(Path(os.environ['FIXTURE_GH_FACTS']).read_text())\n"
        "if args[:2] == ['api', 'graphql']:\n"
        " print(json.dumps({'data': {'repository': {'pullRequest': facts['pr']}}}))\n"
        "elif args[:4] == ['api', '-X', 'GET', 'repos/owner/repo/activity']:\n"
        " print(json.dumps(facts['activity']))\n"
        "elif args == ['pr', 'view', '1', '-R', 'owner/repo', '--json', "
        "'title,labels,state,isDraft,mergeStateStatus,closingIssuesReferences,headRefName']:\n"
        " print(json.dumps(facts['meta']))\n"
        "else:\n"
        " print('forbidden fixture command', file=sys.stderr)\n"
        " sys.exit(99)\n"
    )
    gh.chmod(0o700)
    env.update(FIXTURE_GH_LOG=str(log), FIXTURE_GH_FACTS=str(fixture))
    return log


def test_public_and_direct_manifests_seal_subprocess_roots(packaged):
    runtime, public, host, _, _ = packaged
    for name in ("merge_guard.py", "exact_head_merge_gate.py", "frontend_verify.py"):
        assert name in campaign.module_closure(
            Path(campaign.__file__).parent, campaign.PINNED_MODULES
        )
        assert public["modules"][name] == hashlib.sha256((runtime / name).read_bytes()).hexdigest()
        assert host["modules"][name] == public["modules"][name]
    assert launcher.pin_problems(host, own_path=str(runtime / "closer_launcher.py")) == []


@pytest.mark.parametrize("entry", ["merge_guard.py", "frontend_verify.py"])
def test_real_packaged_help_has_no_source_path_or_host_dependency(packaged, entry):
    result = invoke(packaged, entry, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not list(packaged[0].rglob("*.pyc"))


@pytest.mark.parametrize(
    "name", ["merge_guard.py", "exact_head_merge_gate.py", "frontend_verify.py"]
)
@pytest.mark.parametrize("change", ["missing", "tampered"])
def test_packaged_entry_integrity_remains_mandatory(packaged, name, change):
    runtime, _, host, _, _ = packaged
    path = runtime / name
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    problems = launcher.pin_problems(host, own_path=str(runtime / "closer_launcher.py"))
    assert any(name in problem for problem in problems)


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("clean", None),
        ("head_changed", "observed head changed"),
        ("unresolved_thread", "review thread"),
        ("incomplete_threads", "pagination is incomplete"),
        ("failed_checks", "required checks"),
        ("missing_activity", "records no move"),
        ("review_floor", "review floor"),
        ("missing_ac_spec", "runtime AC gate would need a spec"),
    ],
)
def test_real_packaged_merge_dry_run_preserves_guards(packaged, tmp_path, scenario, reason):
    log = install_read_only_gh(packaged, tmp_path, scenario)
    result = invoke(packaged, "merge_guard.py", "owner/repo#1", "--expected-head", HEAD)
    assert result.returncode == (1 if reason else 0), result.stderr
    decision = json.loads(result.stdout)
    assert decision["merge_executed"] is False
    assert decision["dry_run"] is True
    if reason:
        assert decision["blocked"] is True
        assert reason in decision["reason"]
    else:
        assert decision["blocked"] is False
        assert decision["merge_cmd"][-2:] == ["--match-head-commit", HEAD]
        assert decision["review_loop"]["required"] is False
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls
    assert not any(call[:2] == ["pr", "merge"] for call in calls)
    assert not any("POST" in call or "PATCH" in call or "DELETE" in call for call in calls)


def test_active_packaged_merge_still_requires_expected_head(packaged):
    result = invoke(packaged, "merge_guard.py", "owner/repo#1", "--confirm-merge")
    decision = json.loads(result.stdout)
    assert result.returncode == 1
    assert decision["blocked"] is True
    assert decision["merge_executed"] is False
    assert "--expected-head is required" in decision["reason"]


def test_production_confirm_argv_cannot_bypass_packaged_exact_head_guard(packaged, tmp_path):
    log = install_read_only_gh(packaged, tmp_path, "head_changed")
    result = invoke(
        packaged,
        "merge_guard.py",
        "owner/repo#1",
        "--expected-head",
        HEAD,
        "--confirm-merge",
    )
    decision = json.loads(result.stdout)
    assert result.returncode == 1
    assert decision["dry_run"] is False
    assert decision["blocked"] is True
    assert decision["merge_executed"] is False
    assert decision["reason"] == "observed head changed"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"]


def test_packaged_frontend_does_not_invent_browser_capability(packaged):
    result = invoke(packaged, "frontend_verify.py", "--url", "http://127.0.0.1:1")
    assert result.returncode == 1, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is False
    assert verdict["diagnostic"] == "helper_missing"
    assert "verify.mjs" in verdict["error"]
