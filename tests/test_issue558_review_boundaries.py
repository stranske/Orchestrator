"""Production boundary regressions for the retained issue558 acceptance contract."""

import json
import subprocess
from pathlib import Path

import pytest

import adapters
import adversarial as adv
import feedback

HEAD = "a" * 40
MERGED = "b" * 40


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("failing_call", [1, 2])
def test_restore_checks_each_git_return_code(monkeypatch, failing_call):
    calls = []

    def git(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, int(len(calls) == failing_call), "", "refused")

    monkeypatch.setattr(adv, "_git", git)
    with pytest.raises(RuntimeError, match="restore failed"):
        adv._restore_worktree("/private/review", HEAD)


def test_untracked_reviewer_file_invalidates_the_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.txt").write_text("base")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    head = adv._worktree_head(str(tmp_path))
    (tmp_path / "conftest.py").write_text('raise RuntimeError("shadow")')
    assert adv._tree_changed(adv._worktree_state(str(tmp_path)), head)
    with pytest.raises(RuntimeError, match="untracked"):
        adv._restore_worktree(str(tmp_path), head)
    assert (tmp_path / "conftest.py").exists(), "refuse unknown data; never delete it"


def judge(**kwargs):
    return adv.review_at_head(
        "o/r#7",
        HEAD,
        worktree="/private/review",
        head_fn=lambda _: kwargs.get("checkout_head", HEAD),
        **kwargs
    )


def test_dirty_initial_tree_dispatches_no_reviewer(brain):
    calls = []
    out = judge(
        reviewers=["vibe"],
        state_fn=lambda _: {"head": HEAD, "dirty": "?? shadow"},
        review_fn=lambda *a: calls.append(a),
    )
    assert out["status"] == "failed" and calls == []


def test_restore_failure_records_inconclusive_without_second_reviewer(brain):
    calls = []
    states = iter([{"head": HEAD, "dirty": ""}, {"head": HEAD, "dirty": " M changed"}])

    def review(*a):
        calls.append(a)
        return {"verdict": "PASS"}

    def restore(*a):
        raise RuntimeError("restore refused")

    out = judge(
        reviewers=["auto"],
        choose_fn=lambda _: {"agent": "vibe"},
        state_fn=lambda _: next(states),
        restore_fn=restore,
        review_fn=review,
    )
    assert out["verdict"] == "INCONCLUSIVE" and len(calls) == 1
    assert out["result"]["attempts"][0]["outcome"] == "restore_failed"
    assert out["lineage"]["event_id"]


def test_explicit_author_excluded_and_coverage_inconclusive(brain):
    seats = []

    def review(wt, reviewers, context):
        seats.append(reviewers)
        return {"verdict": "PASS"}

    out = judge(reviewers=["codex", "vibe"], exclude={"codex"}, review_fn=review)
    assert seats == [["vibe"]] and out["verdict"] == "INCONCLUSIVE"


def test_unenforceable_boundary_never_dispatches(monkeypatch):
    monkeypatch.setattr(
        adapters, "write_boundary_unenforceable", lambda _: "nested sandbox unavailable"
    )
    monkeypatch.setattr(
        adv.dispatcher, "offload", lambda *a, **k: pytest.fail("unconfined dispatch")
    )
    out = adv.review("/private/review", ["codex"], "test", veto_threshold=1)
    assert out["verdict"] == "INCONCLUSIVE" and out["by_reviewer"]["codex"]["status"] == "no_seat"


def test_enforced_boundary_protects_live_home_and_sibling(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "write_boundary_unenforceable", lambda _: None)
    monkeypatch.setattr(
        adv, "_git", lambda *a: subprocess.CompletedProcess(a, 0, str(tmp_path / "gitdir"), "")
    )
    out = adv._git_write_boundary("vibe", str(tmp_path / "review"))
    assert str(Path.home()) in out["protect"] and str(tmp_path) in out["protect"]
    assert out["allow"] == []


def test_source_closure_retries_without_second_create(brain):
    creates = []
    closes = []

    def gh(cmd):
        verb = cmd[1:3]
        code = 0
        if verb == ["issue", "list"]:
            body = "[]"
        elif verb == ["label", "list"]:
            body = "[]"
        elif verb == ["issue", "create"]:
            creates.append(cmd)
            body = "https://github.com/o/r/issues/90"
        elif verb == ["pr", "view"]:
            body = json.dumps(
                {
                    "state": "MERGED",
                    "closingIssuesReferences": [
                        {"number": 6, "repository": {"owner": {"login": "o"}, "name": "r"}}
                    ],
                }
            )
        elif verb == ["issue", "view"]:
            body = '{"state":"OPEN"}'
        elif verb == ["issue", "close"]:
            closes.append(cmd)
            code = int(len(closes) == 1)
            body = ""
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, code, body, "failed" if code else "")

    outcome = {
        "verdict": "BLOCKED",
        "key": "closure-retry",
        "reviewers": ["vibe"],
        "result": {"blockers": []},
    }
    first = adv.file_followup("o/r#7", HEAD, outcome, gh=gh)
    assert first.get("closure_complete") is False
    second = adv.file_followup("o/r#7", HEAD, outcome, gh=gh)
    assert second.get("closure_complete") is True and len(creates) == 1 and len(closes) == 2


def test_label_list_failure_does_not_prevent_pending_receipt(brain):
    def gh(cmd):
        verb = cmd[1:3]
        if verb == ["issue", "list"]:
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        if verb == ["label", "list"]:
            return subprocess.CompletedProcess(cmd, 1, "not JSON", "denied")
        if verb == ["issue", "create"]:
            return subprocess.CompletedProcess(cmd, 1, "", "lost")
        raise AssertionError(cmd)

    out = adv.file_followup("o/r#7", HEAD, {"verdict": "BLOCKED", "key": "label-failure"}, gh=gh)
    assert out["filed"] is False
    assert (
        json.loads(adv._followup_record_path("label-failure").read_text())["state"]
        == "create_pending"
    )


def test_merged_checkout_keeps_original_pr_memo_key(brain):
    out = judge(checkout_head=MERGED, reviewers=["vibe"], review_fn=lambda *a: {"verdict": "PASS"})
    assert out["head"] == HEAD and out["result"]["evaluated_head"] == MERGED
    assert adv.recorded_verdict("o/r#7", HEAD, ["vibe"])["state"] == "found"


def test_detached_spawn_binds_actual_merge_and_original_head(brain):
    calls = []

    class Process:
        pid = 1

    out = adv.spawn_post_merge_panel(
        "o/r#7",
        HEAD,
        "context",
        {"codex"},
        merge_head_fn=lambda _: MERGED,
        popen=lambda cmd, **k: calls.append(cmd) or Process(),
    )
    assert out["status"] == "scheduled_after_merge"
    assert calls[0][calls[0].index("--head") + 1] == HEAD
    assert calls[0][calls[0].index("--checkout-head") + 1] == MERGED


def test_boundary_protects_configured_state_and_database(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "write_boundary_unenforceable", lambda _: None)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "external-state"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "external-brain" / "feedback.db")
    monkeypatch.setattr(
        adv, "_git", lambda *a: subprocess.CompletedProcess(a, 0, str(tmp_path / "gitdir"), "")
    )
    out = adv._git_write_boundary("vibe", str(tmp_path / "review"))
    assert str(tmp_path / "external-state") in out["protect"]
    assert str(tmp_path / "external-brain") in out["protect"]


def test_boundary_refuses_a_cwd_that_regrants_live_state(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "write_boundary_unenforceable", lambda _: None)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "review" / "live-state"))
    monkeypatch.setattr(
        adv, "_git", lambda *a: subprocess.CompletedProcess(a, 0, str(tmp_path / "gitdir"), "")
    )
    assert adv._git_write_boundary("vibe", str(tmp_path / "review")) is None
