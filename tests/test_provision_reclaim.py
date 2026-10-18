"""Offline git witnesses for #718; all checkouts and event history are disposable."""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import provision

TARGET = "example/repo#718"
OPENER_BRANCH = "orchestrator/issue-718"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    repos = tmp_path / "repos"
    repos.mkdir()
    canon = repos / provision.repo_slug("example/repo")
    git(tmp_path, "clone", str(origin), str(canon))
    git(canon, "config", "user.email", "test@example.com")
    git(canon, "config", "user.name", "Provision Test")
    git(canon, "commit", "--allow-empty", "-m", "base")
    git(canon, "push", "-u", "origin", "main")
    git(canon, "branch", "pr-head")
    git(canon, "push", "origin", "pr-head")
    monkeypatch.setattr(provision, "REPOS_DIR", repos)
    monkeypatch.setattr(provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(provision, "base_branch", lambda repo: "main")
    monkeypatch.setattr(provision, "_pr_head_branch", lambda repo, num: "pr-head")
    monkeypatch.delenv("ORCH_ALLOW_STALE_WORKTREE_REUSE", raising=False)
    return canon, tmp_path / "holder [old]+ round"


def hold(layout, branch="pr-head"):
    canon, holder = layout
    git(canon, "worktree", "add", str(holder), branch)
    return holder


def events():
    return [json.loads(line) for line in provision.PROVISION_EVENTS_PATH.read_text().splitlines()]


@pytest.mark.parametrize("lane", ["closer", "opener"])
def test_a_clean_idle_holder_is_reclaimed(layout, lane, capsys):
    canon, _ = layout
    branch = "pr-head" if lane == "closer" else OPENER_BRANCH
    if lane == "opener":
        git(canon, "branch", branch)
    holder = hold(layout, branch)
    try:
        wt = provision.provision(TARGET, lane)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr)  # Keep git's holder path visible in the deliberate-break receipt.
        raise
    assert (wt / ".git").is_file()
    assert git(wt, "branch", "--show-current") == branch
    assert not holder.exists()
    assert (
        f"provision: reclaimed stale worktree {holder} holding {branch}" in capsys.readouterr().out
    )
    rows = events()
    assert len(rows) == 1
    assert rows[0] == {
        "ts": rows[0]["ts"],
        "target": TARGET,
        "lane": lane,
        "outcome": "reclaimed",
        "cause": "branch-held",
        "holder_path": str(holder),
    }
    assert git(canon, "ls-remote", "--heads", "origin", "pr-head")


@pytest.mark.parametrize("lane", ["closer", "opener"])
def test_b_dirty_holder_is_refused(layout, lane):
    canon, _ = layout
    branch = "pr-head" if lane == "closer" else OPENER_BRANCH
    if lane == "opener":
        git(canon, "branch", branch)
    holder = hold(layout, branch)
    dirty = holder / "nested" / "keep.txt"
    dirty.parent.mkdir()
    dirty.write_text("uncommitted work")
    with pytest.raises(RuntimeError) as caught:
        provision.provision(TARGET, lane)
    assert str(holder) in str(caught.value)
    assert "nested/keep.txt" in str(caught.value)
    assert dirty.read_text() == "uncommitted work"
    assert len(events()) == 1
    assert events()[0]["outcome"] == "failed"
    assert events()[0]["cause"] == "branch-held"


def test_c_live_holder_is_refused_naming_pid(layout):
    holder = hold(layout)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", str(holder)],
        cwd=layout[0].parent,
        start_new_session=True,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            provision.provision(TARGET, "closer")
        assert str(holder) in str(caught.value)
        if isinstance(caught.value, provision.ProbeBlindError):
            assert "process listing cannot see this process" in str(caught.value)
            assert events()[0]["cause"] == "probe-blind"
        else:
            assert str(proc.pid) in str(caught.value)
        assert holder.exists()
        assert proc.poll() is None
        assert len(events()) == 1
        assert events()[0]["cause"] in {"branch-held", "probe-blind"}
        assert events()[0]["outcome"] == "failed"
    finally:
        # Kill and reap the entire disposable process group.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


@pytest.mark.parametrize("ahead", [False, True])
def test_d_existing_opener_branch_preserves_commits(layout, ahead):
    canon, _ = layout
    git(canon, "switch", "-c", OPENER_BRANCH)
    if ahead:
        git(canon, "commit", "--allow-empty", "-m", "keep me")
    tip = git(canon, "rev-parse", "HEAD")
    git(canon, "switch", "main")
    wt = provision.provision(TARGET, "opener")
    assert git(wt, "rev-parse", "HEAD") == tip
    assert git(wt, "branch", "--show-current") == OPENER_BRANCH
    assert len(events()) == 1
    assert events()[0]["outcome"] == "reused"
    assert events()[0]["cause"] == "branch-exists"


def test_e_empty_and_missing_ledger_are_distinct(capsys):
    assert provision.main(["--report", "--days", "7"]) == 0
    missing = capsys.readouterr().out
    assert "unmeasured: no ledger" in missing
    assert "= 0 over 7d" not in missing
    assert "provision: failed(branch-held|branch-exists) = " in missing
    provision.PROVISION_EVENTS_PATH.touch()
    assert provision.main(["--report", "--days", "7"]) == 0
    empty = capsys.readouterr().out
    assert "provision: failed(branch-held|branch-exists) = 0 over 7d" in empty
    assert "unmeasured" not in empty
    assert "provision: malformed_lines=0" in empty


@pytest.mark.parametrize("nested", [False, True])
def test_f_cwd_only_live_holder_is_refused_naming_pid(layout, nested):
    holder = hold(layout)
    cwd = holder
    if nested:
        # Git ignores empty directories, so the holder remains clean.
        cwd = holder / "nested"
        cwd.mkdir()
    proc = subprocess.Popen(["sleep", "300"], cwd=cwd, start_new_session=True)
    try:
        with pytest.raises(RuntimeError) as caught:
            provision.provision(TARGET, "closer")
        assert str(proc.pid) in str(caught.value)
        assert "live pid" in str(caught.value)
        assert holder.exists()
        assert proc.poll() is None
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


@pytest.mark.parametrize("lsof_mode", ["available", "no-matches", "missing"])
def test_g_symlink_alias_in_argv_is_refused(layout, tmp_path, monkeypatch, lsof_mode):
    # procps honors COLUMNS even with stdout captured; full argv must survive.
    monkeypatch.setenv("COLUMNS", "80")
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "lsof" and lsof_mode == "missing":
            raise FileNotFoundError("lsof missing")
        if args[0] == "lsof" and lsof_mode == "no-matches":
            return subprocess.CompletedProcess(args, 1, "", "")
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", probe)
    holder = hold(layout)
    alias = tmp_path / "alias"
    alias.symlink_to(holder, target_is_directory=True)
    # Python retains the alias argument without using it as cwd.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", str(alias)],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            provision.provision(TARGET, "closer")
        if lsof_mode == "missing":
            # Losing cwd inspection must still refuse, even if argv could be scanned.
            assert "cannot check live processes" in str(caught.value)
            assert "lsof missing" in str(caught.value)
        elif isinstance(caught.value, provision.ProbeBlindError):
            assert "process listing cannot see this process" in str(caught.value)
            assert events()[0]["cause"] == "probe-blind"
        else:
            assert str(proc.pid) in str(caught.value)
            assert "live pid" in str(caught.value)
        assert holder.exists()
        assert proc.poll() is None
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_missing_holder_directory_is_pruned(layout):
    holder = hold(layout)
    shutil.rmtree(holder)
    wt = provision.provision(TARGET, "closer")
    assert (wt / ".git").exists()
    assert str(holder) not in git(layout[0], "worktree", "list", "--porcelain")
    assert events()[0]["outcome"] == "ok"


@pytest.mark.parametrize("unique", [False, True])
def test_diverged_opener_only_resets_when_all_commits_are_on_remotes(layout, unique):
    canon, _ = layout
    git(canon, "switch", "-c", OPENER_BRANCH)
    git(canon, "commit", "--allow-empty", "-m", "old work")
    tip = git(canon, "rev-parse", "HEAD")
    if not unique:
        git(canon, "push", "origin", f"{OPENER_BRANCH}:archived-work")
    git(canon, "switch", "main")
    git(canon, "commit", "--allow-empty", "-m", "new base")
    git(canon, "push", "origin", "main")
    if unique:
        with pytest.raises(RuntimeError, match="commits absent from every remote ref"):
            provision.provision(TARGET, "opener")
        assert git(canon, "rev-parse", OPENER_BRANCH) == tip
        assert events()[0]["outcome"] == "failed"
        assert events()[0]["cause"] == "branch-exists"
    else:
        wt = provision.provision(TARGET, "opener")
        assert git(wt, "rev-parse", "HEAD") == git(canon, "rev-parse", "origin/main")
        assert git(canon, "rev-parse", "origin/archived-work") == tip
        assert events()[0]["outcome"] == "reclaimed"
        assert events()[0]["cause"] == "branch-reset"


def test_process_probe_failure_refuses_reclaim(layout, monkeypatch):
    holder = hold(layout)
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 2, "", "process inspection denied")
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", probe)
    with pytest.raises(RuntimeError, match="cannot check live processes"):
        provision.provision(TARGET, "closer")
    assert holder.exists()
    assert events()[0]["outcome"] == "failed"


@pytest.mark.parametrize("failure", ["missing", "denied", "error-one"])
def test_lsof_failure_refuses_reclaim(layout, monkeypatch, failure):
    holder = hold(layout)
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "lsof":
            if failure == "missing":
                raise FileNotFoundError("lsof missing")
            return subprocess.CompletedProcess(
                args, 1 if failure == "error-one" else 2, "", "inspection denied"
            )
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", probe)
    with pytest.raises(RuntimeError, match="cannot check live processes.*lsof"):
        provision.provision(TARGET, "closer")
    assert holder.exists()
    assert events()[0]["outcome"] == "failed"


@pytest.mark.parametrize("nested", [False, True])
def test_cwd_probe_parses_pid_and_resolves_alias(tmp_path, monkeypatch, nested):
    holder = tmp_path / "real"
    holder.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(holder, target_is_directory=True)
    cwd = alias / "nested" if nested else alias
    calls = []

    def probe(args, **kwargs):
        calls.append(args)
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args == ["lsof", "-a", "-d", "cwd", "-Fn"]
        return subprocess.CompletedProcess(
            args, 0, f"p10\nfcwd\nn{holder}-unrelated\np42\nfcwd\nn{cwd}\n", ""
        )

    monkeypatch.setattr(provision, "_run", probe)
    assert provision._live_holder_pids(holder) == {"42"}
    assert sum(args[0] == "lsof" for args in calls) == 1


def test_visible_probes_allow_idle_holder(tmp_path, monkeypatch):
    def probe(args, **kwargs):
        if args[0] == "lsof":
            output = f"p{os.getpid()}\nn{Path.cwd()}\n"
        elif args[0] == "ps":
            output = f"{os.getpid()} python -m pytest\n"
        elif args == ["pgrep", "-f", "."]:
            output = f"{os.getpid()}\n"
        else:
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(provision, "_run", probe)
    assert provision._live_holder_pids(tmp_path) == set()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("option", [False, True])
def test_argv_probe_resolves_symlink_alias(tmp_path, monkeypatch, nested, option):
    holder = tmp_path / "real"
    holder.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(holder, target_is_directory=True)
    argument = str(alias / "nested" if nested else alias)
    if option:
        argument = "--worktree=" + argument

    def probe(args, **kwargs):
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 1, "", "")
        assert args == ["ps", "-axww", "-o", "pid=,command="]
        return subprocess.CompletedProcess(args, 0, f"42 python worker.py {argument}\n", "")

    monkeypatch.setattr(provision, "_run", probe)
    assert provision._live_holder_pids(holder) == {"42"}


def test_live_probe_result_refuses_reclaim(layout, monkeypatch):
    """Exercise the live-PID branch even when the host cannot enumerate processes."""
    holder = hold(layout)
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "pgrep":
            assert args == ["pgrep", "-f", re.escape(str(holder))]
            return subprocess.CompletedProcess(args, 0, "12345\n", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(
                args, 0, f"12345 python worker.py {shlex.quote(str(holder))}\n", ""
            )
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", probe)
    with pytest.raises(RuntimeError) as caught:
        provision.provision(TARGET, "closer")
    assert str(holder) in str(caught.value)
    assert "live pid 12345" in str(caught.value)
    assert holder.exists()
    assert events()[0]["outcome"] == "failed"


def test_fresh_and_existing_worktree_emit_one_event_each(layout, monkeypatch):
    wt = provision.provision(TARGET, "opener")
    assert provision.provision(TARGET, "opener") == wt
    (wt / "dirty.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="dirty.txt"):
        provision.provision(TARGET, "opener")
    monkeypatch.setenv("ORCH_ALLOW_STALE_WORKTREE_REUSE", "1")
    assert provision.provision(TARGET, "opener") == wt
    assert [e["outcome"] for e in events()] == ["ok", "reused", "failed", "reused"]


@pytest.mark.parametrize("lane", ["closer", "opener"])
def test_failed_fetch_preserves_cached_ref_provisioning(layout, monkeypatch, lane):
    run = provision._run

    def fail_fetch(args, **kwargs):
        if "fetch" in args:
            assert kwargs["check"] is False
            return subprocess.CompletedProcess(args, 1, "", "offline")
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", fail_fetch)
    wt = provision.provision(TARGET, lane)
    assert (wt / ".git").is_file()
    assert events()[0]["outcome"] == "ok"


def test_clone_failure_is_recorded(monkeypatch):
    def fail(repo):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(provision, "ensure_canonical", fail)
    with pytest.raises(RuntimeError, match="clone failed"):
        provision.provision(TARGET, "closer")
    assert len(events()) == 1
    assert events()[0]["outcome"] == "failed"
    assert events()[0]["cause"] == "provision-error"


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_is_recorded_and_reraised(monkeypatch, interruption):
    def interrupt(*args):
        raise interruption()

    monkeypatch.setattr(provision, "_provision", interrupt)
    with pytest.raises(interruption):
        provision.provision(TARGET, "closer")
    assert len(events()) == 1
    assert events()[0]["outcome"] == "failed"
    assert events()[0]["cause"] == "interrupted"


def test_report_skips_torn_line(capsys):
    row = {"ts": datetime.now(timezone.utc).isoformat(), "outcome": "ok", "cause": "new-worktree"}
    provision.PROVISION_EVENTS_PATH.write_text(json.dumps(row) + '\n{"ts":')
    provision.report(7)
    out = capsys.readouterr().out
    assert "provision: malformed_lines=1" in out
    assert "provision: ok = 1" in out


def test_report_counts_only_recent_branch_failures(capsys):
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": (now - timedelta(days=age)).isoformat(), "outcome": outcome, "cause": cause}
        for age, outcome, cause in [
            (1, "failed", "branch-held"),
            (2, "failed", "branch-exists"),
            (3, "failed", "provision-error"),
            (1, "reclaimed", "branch-held"),
            (8, "failed", "branch-held"),
        ]
    ]
    provision.PROVISION_EVENTS_PATH.write_text("".join(json.dumps(row) + "\n" for row in rows))
    provision.main(["--report", "--days", "7"])
    out = capsys.readouterr().out
    assert "provision: failed(branch-held|branch-exists) = 2 over 7d" in out
    assert "provision: failed = 3" in out
    assert "provision: reclaimed(branch-held) = 1" in out


def test_report_cli_honors_temporary_ledger_env():
    result = subprocess.run(
        [sys.executable, str(Path(provision.__file__)), "--report", "--days", "2"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "unmeasured: no ledger" in result.stdout
    assert "over 2d" in result.stdout


@pytest.mark.parametrize("pgrep_match", [False, True])
def test_argv_sibling_prefix_is_not_a_live_holder(tmp_path, monkeypatch, pgrep_match):
    holder = tmp_path / "wt__1"
    sibling = tmp_path / "wt__10"

    def probe(args, **kwargs):
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 0, f"p{os.getpid()}\nn{Path.cwd()}\n", "")
        if args == ["pgrep", "-f", "."]:
            return subprocess.CompletedProcess(args, 0, f"{os.getpid()}\n", "")
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(
                args, 0 if pgrep_match else 1, "42\n" if pgrep_match else "", ""
            )
        assert args[0] == "ps"
        return subprocess.CompletedProcess(
            args, 0, f"{os.getpid()} python -m pytest\n42 python {sibling}/worker.py\n", ""
        )

    monkeypatch.setattr(provision, "_run", probe)
    assert provision._live_holder_pids(holder) == set()


@pytest.mark.parametrize("blind", ["lsof", "pgrep", "ps"])
def test_blind_probe_refuses_and_records_ledger(layout, monkeypatch, blind):
    holder = hold(layout)
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "lsof":
            output = f"p{os.getpid()}\nn{Path.cwd()}\n"
        elif args[0] == "ps":
            output = f"{os.getpid()} python -m pytest\n"
        elif args[0] == "pgrep":
            output = f"{os.getpid()}\n" if args[-1] == "." else ""
        else:
            return run(args, **kwargs)
        if args[0] == blind:
            output = ""
        return subprocess.CompletedProcess(args, 0 if output or args[0] == "ps" else 1, output, "")

    monkeypatch.setattr(provision, "_run", probe)
    with pytest.raises(provision.ProbeBlindError) as caught:
        provision.provision(TARGET, "closer")
    assert str(caught.value) == (
        f"cannot verify that no process holds {holder}: "
        f"process listing cannot see this process ({blind})"
    )
    assert holder.exists()
    assert events()[0]["outcome"] == "failed"
    assert events()[0]["cause"] == "probe-blind"
