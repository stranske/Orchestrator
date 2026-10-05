"""The remote delegation rail refuses a target whose ownership it could not read.

THE DEFECT (owner decision 2026-10-04 on stranske/Orchestrator#439). `dispatcher.delegate_remote`
applies `agent:<X>` only when `_remote_skip_reason` finds the target neither paused nor already
carrying an `agent:*` label. The labels came from `_target_labels`, which returned an EMPTY SET when
`gh` failed, so a read that did not answer was indistinguishable from "no labels" and the rail failed
OPEN. The tick logs show it twice, each time with a control in the same tick:

* 2026-08-18T03:40Z: Trend_Model_Project#5913 had carried `agent:codex` since 2026-08-16T20:03:08Z
  with no unlabel. The tick chose it `applied: true, skip: null` and labelled it `agent:gemini` at
  03:41:15Z, while the same tick skipped Workflows#2521 for its `agent:gemini`.
* 2026-08-22T03:40Z: #5944 had carried `agent:codex` since 01:11:09Z. The tick applied `agent:cursor`
  at 03:42:49Z and skipped its neighbours #5945 and #5943 for `agent:codex`.

The labelled agent never ran in either case, and #439 found both PASS rows false. The same shape
sat one read earlier on the same path. `claims.holder()` answered None ("free") for a claim
`_is_held` calls held when its meta could not be read, and None is what `tick.remote_tick` checks
before it delegates. And because claim meta was rewritten in place, a read that landed between
the truncation and the write made an OLD live claim look stale.

These tests drive the real `subprocess` path through a fake `gh` on PATH. They pin the relationship:
unknown is refused, and a known "no labels" still delegates. The refusal is never cached, so the
next read that answers clears it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import claims
import dispatcher
import tick

GH_STUB = """#!/bin/sh
echo "$*" >> "$GH_STUB_CALLS"
case "$*" in *--method*) exit 0;; esac
if [ -n "$GH_STUB_SLEEP" ]; then exec sleep "$GH_STUB_SLEEP"; fi
if [ -n "$GH_STUB_ERR" ]; then echo "$GH_STUB_ERR" >&2; fi
if [ -f "$GH_STUB_BODY" ]; then cat "$GH_STUB_BODY"; fi
exit "${GH_STUB_EXIT:-0}"
"""
HTTP_502 = "gh: Server Error (HTTP 502)"
NO_LABELS = {"number": 7, "labels": []}
OWNED = {"number": 7, "labels": [{"name": "agent:codex"}, {"name": "agents:keepalive"}]}


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake `gh` first on PATH. `gh.answer(...)` sets what the next read prints and exits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    calls = tmp_path / "gh-calls.log"
    calls.write_text("")
    body = tmp_path / "gh-body.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GH_STUB_CALLS", str(calls))
    monkeypatch.setenv("GH_STUB_BODY", str(body))

    class Gh:
        def answer(self, payload=None, *, text=None, exit_code=0, err="", sleep=""):
            body.write_text(text if text is not None else json.dumps(payload))
            monkeypatch.setenv("GH_STUB_EXIT", str(exit_code))
            monkeypatch.setenv("GH_STUB_ERR", err)
            monkeypatch.setenv("GH_STUB_SLEEP", sleep)

        def calls(self) -> list[str]:
            return calls.read_text().splitlines()

        def posts(self) -> list[str]:
            return [line for line in self.calls() if "--method" in line]

    return Gh()


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    """No test here may reach the live Brain: capture every decision the rail would record."""
    monkeypatch.setattr(dispatcher.feedback, "DB_PATH", tmp_path / "feedback.db")
    runs: list[tuple] = []
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *a, **k: runs.append(a))
    return runs


# --------------------------------------------------------------------------- the read


def test_a_failed_read_is_unknown_not_no_labels(gh):
    gh.answer(text="", exit_code=1, err=HTTP_502)
    labels, why = dispatcher._target_labels("stranske/Trend_Model_Project#5913")
    assert labels is None, labels
    assert why.startswith("gh exit 1:") and "HTTP 502" in why, why
    assert gh.calls() == ["api repos/stranske/Trend_Model_Project/issues/5913"], gh.calls()


@pytest.mark.parametrize(
    "printed",
    [
        "<html>secondary rate limit</html>",
        json.dumps({"message": "Moved Permanently"}),
        json.dumps({"labels": None}),
        json.dumps({"labels": [{"id": 1}]}),
        json.dumps({"labels": [{"name": "agent:codex"}, {"name": 5}]}),
        json.dumps([{"name": "agent:codex"}]),
    ],
    ids=["not-json", "no-label-list", "null-labels", "nameless", "one-bad-name", "not-an-issue"],
)
def test_output_that_is_not_an_issue_label_list_is_unknown(gh, printed):
    gh.answer(text=printed)
    labels, why = dispatcher._target_labels("o/r#7")
    assert labels is None and why, (labels, why)


def test_an_answered_empty_list_is_no_labels(gh):
    gh.answer(NO_LABELS)
    assert dispatcher._target_labels("o/r#7") == (set(), "")


def test_a_label_name_with_a_space_stays_one_name(gh):
    gh.answer({"labels": [{"name": "status: ready"}, {"name": "agent:codex"}]})
    assert dispatcher._target_labels("o/r#7") == ({"status: ready", "agent:codex"}, "")


def test_gh_missing_is_unknown(tmp_path, monkeypatch):
    empty = tmp_path / "no-gh-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    labels, why = dispatcher._target_labels("o/r#7")
    assert labels is None and why.startswith("gh could not run:"), (labels, why)


def test_a_read_that_hangs_is_unknown_within_its_bound(gh, monkeypatch):
    monkeypatch.setattr(dispatcher, "LABEL_READ_TIMEOUT_S", 1)
    gh.answer(NO_LABELS, sleep="5")
    started = time.monotonic()
    labels, why = dispatcher._target_labels("o/r#7")
    assert labels is None and why == "gh did not answer within 1s", (labels, why)
    assert time.monotonic() - started < 4, "the bound did not hold"


# --------------------------------------------------------------------------- the rail


def test_an_unread_target_is_refused_in_dry_run_and_active(gh, recorded):
    gh.answer(text="", exit_code=1, err=HTTP_502)
    shadow = dispatcher.delegate_remote("gemini", "o/r#7", dry_run=True)
    assert shadow["labels_read"] is False, shadow
    assert shadow["skip"].startswith("labels unread (gh exit 1:"), shadow
    assert "ownership unknown" in shadow["skip"], shadow
    live = dispatcher.delegate_remote("gemini", "o/r#7")
    assert (live["applied"], live["labels_read"]) == (False, False), live
    assert live["skip"] == shadow["skip"], "shadow and active must refuse for the same reason"
    assert gh.posts() == [], f"an unread target was labelled: {gh.posts()}"
    assert recorded == [], "a refused delegation must record no run"


def test_an_owned_target_is_refused_and_a_fresh_one_is_labelled(gh, recorded):
    gh.answer(OWNED)
    owned = dispatcher.delegate_remote("gemini", "o/r#7")
    assert owned["applied"] is False and "already in agent pipeline" in owned["skip"], owned
    assert owned["labels_read"] is True, owned
    gh.answer(NO_LABELS)
    fresh = dispatcher.delegate_remote("gemini", "o/r#7")
    assert (fresh["applied"], fresh["labels_read"]) == (True, True), fresh
    assert gh.posts() == ["api --method POST repos/o/r/issues/7/labels -f labels[]=agent:gemini"]
    assert len(recorded) == 1 and recorded[0][0] == "remote:o/r#7:gemini", recorded


def test_the_refusal_clears_on_the_next_read_that_answers(gh, recorded):
    """The drain is the next tick's read, and it runs while the target is refused. Nothing is
    cached between the two calls, so a refusal lasts exactly as long as GitHub cannot answer."""
    gh.answer(text="", exit_code=1, err=HTTP_502)
    assert dispatcher.delegate_remote("cursor", "o/r#7", dry_run=True)["skip"]
    gh.answer(NO_LABELS)
    again = dispatcher.delegate_remote("cursor", "o/r#7", dry_run=True)
    assert again["skip"] is None and again["labels_read"] is True, again


# --------------------------------------------------------------------------- the tick


def _cap() -> dict:
    return {"agents": {a: {"state": "ok"} for a in ("cursor", "codex", "claude", "gemini")}}


@pytest.fixture
def sandboxed_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(claims, "_handoff_dir", lambda: tmp_path / "handoff")
    monkeypatch.setattr(tick.feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0")

    def run(target: str, *, dry_run: bool) -> dict:
        return tick.remote_tick(
            [{"target": target, "task_type": "implement"}],
            _cap(),
            dry_run=dry_run,
            do_ingest=False,
            env={},
            research_tick_fn=lambda *a, **k: {"status": "skipped", "planned": [], "active": False},
        )

    return run


@pytest.mark.parametrize("dry_run", [True, False], ids=["shadow", "active"])
def test_the_tick_plan_names_the_refusal(gh, recorded, sandboxed_tick, dry_run):
    gh.answer(text="", exit_code=1, err=HTTP_502)
    out = sandboxed_tick("o/r#7", dry_run=dry_run)
    [row] = out["chosen"]
    assert row["labels_read"] is False and not row["applied"], row
    assert "labels unread" in row["skip"] and "HTTP 502" in row["skip"], row
    assert gh.posts() == [] and recorded == [], (gh.posts(), recorded)


def test_a_held_claim_with_unreadable_meta_has_an_unknown_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(claims, "_handoff_dir", lambda: tmp_path)
    target = "o/r#8"
    unstamped = claims._claims_dir() / claims._slug(target)
    unstamped.mkdir(parents=True)  # what claim() leaves between its mkdir and its stamp
    assert claims.holder(target) == {"target": target, "agent": None, "meta": "unreadable"}
    (unstamped / "meta").write_text('{"target": "o/r#8", "agent": "co')  # a meta that won't parse
    assert claims.holder(target) == {"target": target, "agent": None, "meta": "unreadable"}
    old = time.time() - claims.CLAIM_TTL_DEFAULT - 5
    os.utime(unstamped, (old, old))
    assert claims.holder(target) is None, "past the TTL it is stale, which reap_stale drains"


def test_the_tick_blocks_a_target_whose_claim_holder_is_unknown(gh, sandboxed_tick, tmp_path):
    gh.answer(NO_LABELS)
    (Path(tmp_path) / "handoff" / "claims" / claims._slug("o/r#8")).mkdir(parents=True)
    out = sandboxed_tick("o/r#8", dry_run=True)
    assert out["chosen"] == [], out
    assert out["blocked"] == [
        {"target": "o/r#8", "task_type": "implement", "reason": "claimed by an unknown holder"}
    ], out["blocked"]
    assert gh.calls() == [], "a blocked target must not even be read"
    claims.release("o/r#8")
    assert sandboxed_tick("o/r#8", dry_run=True)["chosen"][0]["skip"] is None, "free once released"


def test_a_reader_during_a_meta_rewrite_still_sees_the_claim_held(tmp_path, monkeypatch):
    """An in-place rewrite truncates `meta` before it writes. `_is_held` ages unreadable meta by the
    dir's mtime, so a read in that window called an OLD live claim stale, which the tick reads as
    free and `reap_stale` may remove. The rewrite is atomic now: a reader lands on the old record
    or the new one, never on the empty file between them."""
    monkeypatch.setattr(claims, "_handoff_dir", lambda: tmp_path)
    target = "o/r#9"
    assert claims.claim(target, "codex")  # stamped with this test's live pid
    claim_dir = claims._claims_dir() / claims._slug(target)
    old = time.time() - claims.CLAIM_TTL_DEFAULT - 5
    os.utime(claim_dir, (old, old))  # held by a live process for longer than the TTL
    seen: list = []
    real_write_text = Path.write_text

    def write_with_a_reader_in_the_window(self, data, *args, **kwargs):
        if self.parent == claim_dir:
            self.open("w").close()  # where every write starts: an empty file
            seen.append(claims.holder(target))
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_with_a_reader_in_the_window)
    assert claims.update_metadata(target, "codex", lane="closer") is True
    assert seen, "the probe never ran: the meta write no longer goes through Path.write_text"
    assert all(h is not None and h.get("agent") == "codex" for h in seen), seen
    assert claims.holder(target)["lane"] == "closer"
    assert not list(claim_dir.glob(".meta.*")), "the temporary file must not outlive the write"
