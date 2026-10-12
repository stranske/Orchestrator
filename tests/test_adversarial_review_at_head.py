"""The adversarial panel judges each (PR, head SHA) once, and a moved head is never shown an old
verdict.

THE DEFECT (measured 2026-10-05). The panel's only automatic caller was the tick's pre-delegation
hook, which ran on every hourly tick for every high-stakes closer item and keyed nothing: the same
head would have been reviewed again each hour, and the Brain event it recorded did not say which
head it had judged. `adversarial.review_at_head` is now the one entry, keyed on a hash over
(target, head, reviewer set) and recorded in the Brain's completion events.

These tests drive the real recorder and lookup against a disposable Brain; only the reviewers, the
worktree and its head are stubbed.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest

import adversarial
import feedback

TARGET = "o/r#7"
REVIEWERS = ["vibe", "gemini"]


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


class Panel:
    """Stub reviewers: counts every real judgment and returns the verdict it is set to."""

    def __init__(self, verdict: str = "BLOCKED") -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, worktree, reviewers, context):
        self.calls.append(worktree)
        blockers = [{"severity": "high", "finding": "policy fails open", "confidence": 0.9}]
        return {"verdict": self.verdict, "blockers": blockers if self.verdict == "BLOCKED" else []}


def _judge(panel: Panel, head: str, **kwargs):
    return adversarial.review_at_head(
        TARGET,
        head,
        reviewers=REVIEWERS,
        worktree=f"/wt/{head[:6]}",
        head_fn=lambda worktree: head,
        review_fn=panel,
        **kwargs,
    )


def test_the_same_head_is_judged_once_and_its_findings_come_back(brain):
    panel = Panel("BLOCKED")
    first = _judge(panel, "a" * 40)
    assert (first["status"], first["memo"], first["verdict"]) == ("executed", "none", "BLOCKED")
    assert first["inconclusive_at_head"] == 0, first  # a count, so zero is printed as zero
    again = _judge(panel, "a" * 40)
    assert (again["status"], again["memo"], again["verdict"]) == ("reused", "found", "BLOCKED")
    assert again["result"]["blockers"][0]["finding"] == "policy fails open", again
    assert len(panel.calls) == 1, panel.calls


def test_a_moved_head_is_judged_afresh_and_never_shown_the_old_verdict(brain):
    panel = Panel("BLOCKED")
    _judge(panel, "a" * 40)
    panel.verdict = "PASS"
    moved = _judge(panel, "b" * 40)
    assert (moved["status"], moved["verdict"]) == ("executed", "PASS"), moved
    assert len(panel.calls) == 2, panel.calls
    assert adversarial.recorded_verdict(TARGET, "a" * 40, REVIEWERS)["verdict"] == "BLOCKED"
    assert adversarial.recorded_verdict(TARGET, "b" * 40, REVIEWERS)["verdict"] == "PASS"


def test_another_reviewer_set_is_another_judgment(brain):
    panel = Panel("PASS")
    _judge(panel, "a" * 40)
    assert adversarial.recorded_verdict(TARGET, "a" * 40, ["codex"])["state"] == "none"


def test_an_inconclusive_panel_is_re_run_at_the_same_head(brain):
    """A reviewer shortfall is "NOT a pass; re-run the missing coverage". Reusing it would forbid
    exactly that re-run until an unrelated push, so only PASS and BLOCKED are reused."""
    panel = Panel("INCONCLUSIVE")
    assert _judge(panel, "c" * 40)["conclusive"] is False
    panel.verdict = "PASS"
    rerun = _judge(panel, "c" * 40)
    assert (rerun["status"], rerun["verdict"]) == ("executed", "PASS"), rerun
    assert rerun["inconclusive_at_head"] == 1 and len(panel.calls) == 2, rerun
    assert _judge(panel, "c" * 40)["status"] == "reused"


def test_a_worktree_at_another_commit_is_refused_and_nothing_is_recorded(brain):
    panel = Panel("PASS")
    out = adversarial.review_at_head(
        TARGET,
        "d" * 40,
        reviewers=REVIEWERS,
        worktree="/wt/stale",
        head_fn=lambda worktree: "e" * 40,
        review_fn=panel,
    )
    assert out["status"] == "head_mismatch" and out["observed_head"] == "e" * 40, out
    assert panel.calls == [], panel.calls
    assert adversarial.recorded_verdict(TARGET, "d" * 40, REVIEWERS)["state"] == "none"


def test_an_unreadable_brain_is_unknown_not_none_and_the_panel_still_runs(brain):
    panel = Panel("PASS")
    out = _judge(panel, "f" * 40, lookup_fn=lambda *a: {"state": "unknown", "error": "locked"})
    assert (out["status"], out["memo"]) == ("executed", "unknown"), out
    assert out["inconclusive_at_head"] is None and out["memo_error"] == "locked", out


def test_findings_are_served_only_while_the_artifact_matches_its_recorded_hash(brain):
    panel = Panel("BLOCKED")
    _judge(panel, "a" * 40)
    path = adversarial._panel_artifact_path(adversarial.panel_key(TARGET, "a" * 40, REVIEWERS))
    doc = json.loads(path.read_text())
    doc["result"]["blockers"] = []
    path.write_text(json.dumps(doc))
    reused = _judge(panel, "a" * 40)
    assert reused["status"] == "reused" and reused["verdict"] == "BLOCKED", reused
    assert reused["result"] is None, reused  # the verdict stands; edited findings are not served


@pytest.mark.parametrize("target,head", [("o/r", "a" * 40), (TARGET, "abc123"), (TARGET, "")])
def test_a_target_or_head_that_cannot_key_a_verdict_is_refused(brain, target, head):
    assert adversarial.review_at_head(target, head, reviewers=REVIEWERS)["status"] == "invalid"


def test_the_cli_reports_a_recorded_verdict_without_running_the_panel(brain, capsys):
    _judge(Panel("BLOCKED"), "a" * 40)
    argv = ["review", "--target", TARGET, "--head", "a" * 40, "--reviewers", "vibe,gemini"]
    assert adversarial.main(argv + ["--lookup-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["state"], out["verdict"]) == ("found", "BLOCKED"), out
    assert adversarial.main(["review", "--target", TARGET, "--head", "zz", "--lookup-only"]) == 2


def _blocked_followup():
    return {
        "key": adversarial.panel_key(TARGET, "a" * 40, REVIEWERS),
        "verdict": "BLOCKED",
        "reviewers": REVIEWERS,
        "result": {"blockers": [{"severity": "critical", "finding": "policy fails open"}]},
    }


class FollowupGitHub:
    """A disposable remote store; a create may commit without delivering its response."""

    def __init__(self):
        self.calls = []
        self.issues = []
        self.search_reply = None
        self.create_response = "ok"
        self.visible = True
        self.fail_source_lookup = False

    def __call__(self, cmd):
        self.calls.append(cmd)
        verb = cmd[1:3]
        if verb == ["issue", "list"]:
            if self.search_reply is not None:
                code, body = self.search_reply
                return subprocess.CompletedProcess(cmd, code, body, "search unavailable")
            reply = json.dumps(self.issues if self.visible else [])
        elif verb == ["label", "list"]:
            reply = json.dumps([{"name": "priority:high"}])
        elif verb == ["issue", "create"]:
            self.issues.append(
                {
                    "number": 90,
                    "title": cmd[cmd.index("--title") + 1],
                    "body": cmd[cmd.index("--body") + 1],
                }
            )
            if self.create_response == "timeout":
                raise subprocess.TimeoutExpired(cmd, 30)
            if self.create_response == "failed":
                return subprocess.CompletedProcess(cmd, 1, "", "response lost")
            reply = (
                "invalid response"
                if self.create_response == "malformed"
                else "https://github.com/o/r/issues/90\n"
            )
        elif verb == ["pr", "view"]:
            if self.fail_source_lookup:
                raise OSError("source lookup failed")
            reply = json.dumps({"closingIssuesReferences": []})
        else:
            raise AssertionError(f"unexpected mutation: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, reply, "")

    @property
    def creates(self):
        return sum(cmd[1:3] == ["issue", "create"] for cmd in self.calls)


def _file_blocked(gh):
    return adversarial.file_followup(TARGET, "a" * 40, _blocked_followup(), gh=gh)


@pytest.mark.parametrize(
    "reply",
    [(1, "[]"), (0, ""), (0, "bad JSON"), (0, "{}"), (0, "[{}]")],
)
def test_followup_search_failure_or_malformed_result_never_creates(brain, reply):
    gh = FollowupGitHub()
    gh.search_reply = reply
    out = _file_blocked(gh)
    assert out["filed"] is False and "error" in out, out
    assert gh.creates == 0, gh.calls
    gh.search_reply = None
    assert _file_blocked(gh)["issue"] == 90  # a read failure does not latch a create attempt
    assert gh.creates == 1


@pytest.mark.parametrize("response", ["failed", "timeout", "malformed"])
def test_followup_ambiguous_create_reconciles_before_any_retry(brain, response):
    gh = FollowupGitHub()
    gh.create_response = response
    first = _file_blocked(gh)
    assert first["filed"] is False and "error" in first, first
    record = adversarial._followup_record_path(_blocked_followup()["key"])
    assert record.exists(), "an ambiguous create must leave a durable pending receipt"
    assert json.loads(record.read_text())["state"] == "create_pending"
    # GitHub search can lag. Even several successful empty searches cannot justify another POST.
    gh.visible = False
    for _ in range(3):
        retry = _file_blocked(gh)
        assert retry["filed"] is False and retry.get("state") == "create_pending", retry
    gh.visible = True
    reconciled = _file_blocked(gh)
    assert reconciled["filed"] is False and reconciled["issue"] == 90, reconciled
    assert _file_blocked(gh)["issue"] == 90
    assert gh.creates == 1, gh.calls


def test_followup_reconciles_only_the_same_panel_key(brain):
    gh = FollowupGitHub()
    gh.create_response = "failed"
    _file_blocked(gh)
    body = gh.issues[0]["body"]
    gh.issues[0]["body"] = body.replace(_blocked_followup()["key"], "another-panel")
    out = _file_blocked(gh)
    assert out.get("state") == "create_pending" and "issue" not in out, out
    assert gh.creates == 1
    gh.issues[0]["body"] = body
    assert _file_blocked(gh)["issue"] == 90


def test_followup_remote_record_is_reused_without_a_local_receipt(brain):
    gh = FollowupGitHub()
    gh.issues = [
        {
            "number": 90,
            "title": adversarial.FOLLOWUP_TITLE.format(target=TARGET, short="a" * 7),
            "body": adversarial.followup_body(TARGET, "a" * 40, _blocked_followup()),
        }
    ]
    out = _file_blocked(gh)
    assert out["filed"] is False and out["issue"] == 90, out
    assert gh.creates == 0
    gh.visible = False
    assert _file_blocked(gh)["issue"] == 90


@pytest.mark.parametrize("duplicate", [False, True])
def test_followup_incomplete_or_duplicate_search_refuses_create(brain, duplicate):
    gh = FollowupGitHub()
    if duplicate:
        gh.issues = [
            {
                "number": n,
                "title": adversarial.FOLLOWUP_TITLE.format(target=TARGET, short="a" * 7),
                "body": adversarial.followup_body(TARGET, "a" * 40, _blocked_followup()),
            }
            for n in (90, 91)
        ]
    else:
        gh.issues = [{"number": n, "title": "other title", "body": ""} for n in range(1, 101)]
    out = _file_blocked(gh)
    assert out["filed"] is False and "error" in out, out
    assert gh.creates == 0


@pytest.mark.parametrize("contents", ["broken JSON", "null", "[]", '{"key":"another-panel"}'])
def test_followup_corrupt_record_never_authorizes_another_create(brain, contents):
    gh = FollowupGitHub()
    path = adversarial._followup_record_path(_blocked_followup()["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    out = _file_blocked(gh)
    assert out["filed"] is False and "error" in out, out
    assert gh.creates == 0, gh.calls


def test_followup_persistence_failure_prevents_create(brain, monkeypatch):
    gh = FollowupGitHub()

    def fail(path, value):
        raise OSError("state storage unavailable")

    monkeypatch.setattr(adversarial, "_write_followup_record", fail)
    out = _file_blocked(gh)
    assert out["filed"] is False and "error" in out, out
    assert gh.creates == 0


def test_followup_success_survives_failed_receipt_write(brain, monkeypatch):
    gh = FollowupGitHub()
    write = adversarial._write_followup_record

    def fail_receipt(path, value):
        if value.get("issue"):
            raise OSError("receipt write failed")
        write(path, value)

    monkeypatch.setattr(adversarial, "_write_followup_record", fail_receipt)
    assert "error" in _file_blocked(gh)
    monkeypatch.setattr(adversarial, "_write_followup_record", write)
    assert _file_blocked(gh)["issue"] == 90
    assert gh.creates == 1


def test_followup_filing_is_recorded_before_source_disposition(brain):
    gh = FollowupGitHub()
    gh.fail_source_lookup = True
    out = _file_blocked(gh)
    assert out["filed"] is True and out["issue"] == 90 and "error" in out, out
    gh.visible = False
    again = _file_blocked(gh)
    assert again["filed"] is False and again["issue"] == 90, again
    assert gh.creates == 1


def _followup_process(state, started, release, results, *, crash=False):
    """Fresh process, real lock and persistence, fake GitHub mutation recorded outside its memory."""
    os.environ["ORCH_STATE_DIR"] = state
    gh = FollowupGitHub()

    def run(cmd):
        if cmd[1:3] == ["issue", "create"]:
            with (Path(state) / "creates.log").open("a") as stream:
                stream.write("create\n")
                stream.flush()
                os.fsync(stream.fileno())
            if crash:
                os._exit(27)
            if not started.is_set():
                started.set()
                assert release.wait(10), "worker not released"
        return gh(cmd)

    results.put(_file_blocked(run))


def test_followup_concurrent_processes_create_at_most_once(brain):
    ctx = multiprocessing.get_context("spawn")
    started, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    owner = ctx.Process(target=_followup_process, args=(str(brain), started, release, results))
    contender = ctx.Process(target=_followup_process, args=(str(brain), started, release, results))
    try:
        owner.start()
        assert started.wait(10), "owner did not reach create"
        contender.start()
        contender.join(10)
        assert contender.exitcode == 0, "contender failed or waited behind the lock"
        release.set()
        owner.join(10)
        assert owner.exitcode == 0
        outputs = [results.get(timeout=5), results.get(timeout=5)]
        assert sum(out["filed"] for out in outputs) == 1, outputs
        assert (brain / "creates.log").read_text().splitlines() == ["create"]
        gh = FollowupGitHub()
        assert _file_blocked(gh)["issue"] == 90 and gh.creates == 0
    finally:
        release.set()
        for proc in (owner, contender):
            if proc.pid is not None:
                if proc.is_alive():
                    proc.terminate()
                proc.join(5)
        results.close()


def test_followup_crashed_process_keeps_pending_create_and_releases_lock(brain):
    ctx = multiprocessing.get_context("spawn")
    started, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    proc = ctx.Process(
        target=_followup_process,
        args=(str(brain), started, release, results),
        kwargs={"crash": True},
    )
    try:
        proc.start()
        proc.join(10)
        assert proc.exitcode == 27, "create crash did not occur"
        gh = FollowupGitHub()
        out = _file_blocked(gh)
        assert out.get("state") == "create_pending", out
        assert gh.creates == 0
        outcome = _blocked_followup()
        gh.issues = [
            {
                "number": 90,
                "title": adversarial.FOLLOWUP_TITLE.format(target=TARGET, short="a" * 7),
                "body": adversarial.followup_body(TARGET, "a" * 40, outcome),
            }
        ]
        assert _file_blocked(gh)["issue"] == 90
        assert gh.creates == 0
        assert (brain / "creates.log").read_text().splitlines() == ["create"]
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(5)
        results.close()
