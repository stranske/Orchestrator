"""The local keepalive loop (owner decision 2026-11-08): a LOCAL agent's PR gets the CI-feedback rounds a
hosted one gets — re-run on CI results with failures, task list + progress, acceptance commands before
done, rotation on stall — and every way out is motion: done (closer-core merges) or a hand-off to hosted
keepalive. These tests drive `Tick` with a fake GitHub and fake rails; nothing touches the network.
"""

from __future__ import annotations

import json

import pytest

import local_loop as ll

ISSUE_BODY = """## Scope
Make the widget fast.

## Tasks
- [ ] Add a cache
- [ ] Wire it into the widget

## Acceptance Criteria
- [ ] `pytest tests/test_widget.py` passes
"""


def pr_node(
    *,
    state="FAILURE",
    head="h1",
    failing=("Gate / tests",),
    labels=("agent:codex", ll.LABEL),
    body=ISSUE_BODY,
    number=7,
):
    contexts = [
        {
            "__typename": "CheckRun",
            "name": n,
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{100 + i}",
        }
        for i, n in enumerate(failing)
    ]
    return {
        "number": 3,
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "codex/issue-7",
        "headRefOid": head,
        "body": "Closes #7",
        "repository": {"nameWithOwner": "o/r"},
        "labels": {"nodes": [{"name": x} for x in labels]},
        "closingIssuesReferences": {
            "nodes": [{"number": number, "title": "t", "state": "OPEN", "body": body}]
        },
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "statusCheckRollup": (
                            None
                            if state is None
                            else {"state": state, "contexts": {"nodes": contexts}}
                        ),
                    }
                }
            ]
        },
    }


class FakeGH:
    def __init__(self, prs):
        self.prs = prs
        self.calls: list[tuple] = []

    def looped_prs(self):
        return self.prs

    def job_log(self, repo, job):
        self.calls.append(("log", job))
        return "2026-11-08T00:00:00Z ok line\n2026-11-08T00:00:01Z E   AssertionError: boom\n"

    def set_issue_body(self, repo, n, body):
        self.calls.append(("body", n, body))

    def add_labels(self, repo, n, labels):
        self.calls.append(("add", n, tuple(labels)))

    def remove_label(self, repo, n, label):
        self.calls.append(("remove", n, label))

    def ready(self, repo, n):
        self.calls.append(("ready", n))

    def comment(self, repo, n, body):
        self.calls.append(("comment", n, body))


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "STATE_DIR", tmp_path / "loop")
    monkeypatch.setattr(ll, "DISPATCH_LOG_DIR", tmp_path / "logs")
    return tmp_path


def make_tick(
    gh,
    *,
    now=1000.0,
    running=False,
    cap_ok=True,
    rotate=None,
    acceptance=None,
    log="",
    launches=None,
):
    launches = launches if launches is not None else []
    clock = {"t": now}

    def launch(agent, target, prompt, rationale):
        launches.append(
            {"agent": agent, "target": target, "prompt": prompt, "rationale": rationale}
        )
        return {"pid": 1, "log": "/tmp/round.log", "run_id": f"r{len(launches)}"}

    tick = ll.Tick(
        gh,
        apply=True,
        now=lambda: clock["t"],
        running=lambda targets: running,
        capacity=lambda: {
            "ok": cap_ok,
            "reason": (
                "local agents 3/3 (drainable 3)" if not cap_ok else "local agents 1/3, load 4.0/20"
            ),
            "drainable": 3,
        },
        rotate=rotate or (lambda task, tried: None),
        hosted_agent=lambda task, current: "codex",
        acceptance=acceptance or (lambda *a: {"result": "PASS", "detail": "ok"}),
        launch=launch,
        record_handoff=lambda *a: None,
        log_reader=lambda path: log,
    )
    return tick, launches, clock


def test_pure_helpers():
    items = ll.checklist(ISSUE_BODY)
    assert [i["id"] for i in items] == ["T1", "T2", "A1"] and not any(i["done"] for i in items)
    new, ticked = ll.tick(ISSUE_BODY, {"T2", "A1"})
    assert ticked == ["T2", "A1"]
    assert [i["done"] for i in ll.checklist(new)] == [False, True, True]
    log = "prompt says `LOOP-TASKS-DONE: <ids>` and `LOOP-TASKS-DONE: none`\n...\nLOOP-TASKS-DONE: T1, a1\n"
    assert ll.claimed_ids(log) == {"T1", "A1"}
    assert ll.claimed_ids("echoed prompt: `LOOP-TASKS-DONE: none`") == set()
    assert ll.error_lines("x\n2026Z E   boom\nfine") == ["E   boom"]
    assert ll.job_id("https://github.com/o/r/actions/runs/1/job/42") == "42"


def test_agent_running_waits_without_reads():
    gh = FakeGH([pr_node()])
    tick, launches, _ = make_tick(gh, running=True)
    tick.run()
    assert tick.items[0]["action"] == "agent-running" and not launches and not gh.calls


def test_ci_failure_starts_a_round_with_the_failing_lines_and_progress():
    gh = FakeGH([pr_node(failing=("Gate / tests", "lint"))])
    tick, launches, _ = make_tick(gh)
    tick.run()
    assert tick.items[0]["action"] == "round"
    prompt = launches[0]["prompt"]
    assert "Gate / tests" in prompt and "AssertionError: boom" in prompt
    assert "**Progress:** 0/3 tasks complete, 3 remaining" in prompt
    assert "- [ ] T1: Add a cache" in prompt and "LOOP-TASKS-DONE" in prompt
    assert (
        launches[0]["rationale"].startswith("local-loop round 2/12")
        and "local agents 1/3" in launches[0]["rationale"]
    )
    st = json.loads(ll.state_path("o/r#3").read_text())
    assert len(st["rounds"]) == 2 and st["rounds"][1]["head_before"] == "h1"
    # the agent exits without pushing: same head and red CI -> the next round reuses the cached logs
    tick2, launches2, _ = make_tick(gh, now=2000.0)
    tick2.run()
    assert tick2.items[0]["action"] == "round" and len(launches2) == 1
    assert (
        len([c for c in gh.calls if c[0] == "log"]) == 2
    )  # no second read of the same failing head


def test_green_with_acceptance_pass_and_claims_ticks_boxes_and_is_done():
    gh = FakeGH([pr_node(state="SUCCESS", failing=())])
    tick, launches, _ = make_tick(gh, log="LOOP-TASKS-DONE: T1, T2")
    tick.run()
    assert tick.items[0]["action"] == "done" and not launches
    bodies = [c[2] for c in gh.calls if c[0] == "body"]
    assert bodies and all(i["done"] for i in ll.checklist(bodies[-1]))
    assert any(c[0] == "comment" and "done after 1 round" in c[2] for c in gh.calls)
    assert ll.owns("o/r#3") is None  # done: closer-core's normal path merges it


def test_green_with_unchecked_tasks_runs_another_round():
    gh = FakeGH([pr_node(state="SUCCESS", failing=())])
    tick, launches, _ = make_tick(
        gh, acceptance=lambda *a: {"result": "UNRUNNABLE", "detail": "no gate commands"}
    )
    tick.run()
    assert tick.items[0]["action"] == "round" and "unchecked" in launches[0]["prompt"]
    assert ll.owns("o/r#3", now=1000.0)["status"] == "active"


def test_acceptance_fail_on_green_feeds_its_output():
    gh = FakeGH([pr_node(state="SUCCESS", failing=())])
    tick, launches, _ = make_tick(
        gh, acceptance=lambda *a: {"result": "FAIL", "detail": "1 failed: test_widget"}
    )
    tick.run()
    assert (
        "Acceptance commands failed" in launches[0]["prompt"]
        and "1 failed: test_widget" in launches[0]["prompt"]
    )


def run_round_without_push(gh, launches, clock, **kw):
    clock["t"] += 600
    tick, more, _ = make_tick(gh, now=clock["t"], launches=launches, **kw)
    tick.run()
    return tick


def test_two_unproductive_rounds_rotate_then_hand_off():
    gh = FakeGH([pr_node(state="FAILURE")])
    launches: list = []
    tick, _, clock = make_tick(gh, launches=launches)
    tick.run()  # round 2 (round 1 = the start) pushed h1: productive
    # rounds that push nothing: same head h1, same red CI
    t = run_round_without_push(gh, launches, clock)  # closes round 2 unproductive -> round 3
    assert t.items[0]["action"] == "round"
    t = run_round_without_push(gh, launches, clock, rotate=lambda task, tried: "claude")
    assert t.items[0]["action"] == "round" and launches[-1]["agent"] == "claude"
    assert "rotated" in launches[-1]["prompt"]
    t = run_round_without_push(gh, launches, clock)
    t = run_round_without_push(
        gh, launches, clock
    )  # claude unproductive twice, no untried agent left
    assert t.items[0]["action"] == "handed-off"
    assert ("remove", 3, ll.LABEL) in gh.calls
    assert any(c[0] == "add" and "agents:keepalive" in c[2] for c in gh.calls)


def test_full_local_capacity_waits_then_hands_off():
    gh = FakeGH([pr_node()])
    tick, launches, clock = make_tick(gh, cap_ok=False)
    line = tick.run()
    assert tick.items[0]["action"] == "wait-capacity" and "drainable 3" in tick.items[0]["reason"]
    assert "waiting for a local slot 1 (blocking 1, drainable 3)" in line
    tick2, _, _ = make_tick(gh, now=clock["t"] + ll.CAPACITY_WAIT + 1, cap_ok=False)
    tick2.run()
    assert tick2.items[0]["action"] == "handed-off" and not launches


def test_round_cap_hands_off(monkeypatch):
    monkeypatch.setattr(ll, "ROUND_CAP", 2)
    gh = FakeGH([pr_node()])
    tick, launches, clock = make_tick(gh)
    tick.run()
    assert tick.items[0]["action"] == "round"
    t = run_round_without_push(gh, launches, clock)
    assert t.items[0]["action"] == "handed-off" and "round cap 2" in t.items[0]["reason"]


def test_one_owner_strips_hosted_labels():
    gh = FakeGH([pr_node(labels=("agent:codex", ll.LABEL, "agents:keepalive", "autofix"))])
    tick, _, _ = make_tick(gh, running=True)
    tick.run()
    assert ("remove", 3, "agents:keepalive") in gh.calls and ("remove", 3, "autofix") in gh.calls


def test_owns_expires_when_the_loop_stops_ticking():
    gh = FakeGH([pr_node()])
    tick, _, _ = make_tick(gh, running=True)
    tick.run()
    assert ll.owns("o/r#3", now=1000.0 + 60)
    assert ll.owns("o/r#3", now=1000.0 + ll.FRESH + 1) is None


def test_a_done_draft_is_marked_ready_for_the_closer():
    node = pr_node(state="SUCCESS", failing=())
    node["isDraft"] = True
    gh = FakeGH([node])
    tick, _, _ = make_tick(gh, log="LOOP-TASKS-DONE: T1, T2")
    tick.run()
    assert tick.items[0]["action"] == "done" and ("ready", 3) in gh.calls


def test_no_check_on_the_head_hands_off_after_the_wait():
    gh = FakeGH([pr_node(state=None)])
    tick, _, clock = make_tick(gh)
    tick.run()
    assert tick.items[0]["action"] == "wait-ci"
    tick2, _, _ = make_tick(gh, now=clock["t"] + ll.NO_CI_WAIT + 1)
    tick2.run()
    assert (
        tick2.items[0]["action"] == "handed-off" and "no check reported" in tick2.items[0]["reason"]
    )


def test_repeated_start_errors_hand_off():
    gh = FakeGH([pr_node()])
    for i in range(ll.START_ERRORS):
        tick, _, _ = make_tick(gh, now=1000.0 + i)
        tick.launch = lambda *a: {"error": "provision failed"}
        tick.run()
    assert (
        tick.items[0]["action"] == "handed-off" and "round starts failed" in tick.items[0]["reason"]
    )


def test_hand_off_adds_the_hosted_owner_before_removing_the_local_one(monkeypatch):
    monkeypatch.setattr(ll, "ROUND_CAP", 1)
    gh = FakeGH([pr_node(labels=("agent:codex", ll.LABEL, "agents:keepalive"))])
    tick, _, _ = make_tick(gh)
    tick.run()
    assert tick.items[0]["action"] == "handed-off"
    writes = [c for c in gh.calls if c[0] in {"add", "remove"}]
    assert writes[0] == ("remove", 3, "agents:keepalive")  # the stray label, stripped first
    add = next(i for i, c in enumerate(writes) if c[0] == "add")
    assert "agents:keepalive" in writes[add][2]  # re-added: the stale snapshot no longer hides it
    assert writes.index(("remove", 3, ll.LABEL)) > add


def test_a_failed_ready_write_is_retried_not_marked_done():
    node = pr_node(state="SUCCESS", failing=())
    node["isDraft"] = True
    gh = FakeGH([node])

    def broken(repo, n):
        raise RuntimeError("gh pr ready: transient")

    gh.ready = broken
    tick, _, _ = make_tick(gh, log="LOOP-TASKS-DONE: T1, T2")
    tick.run()
    assert tick.items[0]["action"] == "error"
    assert json.loads(ll.state_path("o/r#3").read_text())["status"] == "active"


def test_pr_without_a_closing_issue_uses_its_own_task_list():
    node = pr_node(state="SUCCESS", failing=())
    node["closingIssuesReferences"] = {"nodes": []}
    node["body"] = ISSUE_BODY
    gh = FakeGH([node])
    tick, _, _ = make_tick(gh, log="LOOP-TASKS-DONE: T1, T2")
    tick.run()
    assert tick.items[0]["action"] == "done"
    assert any(c[0] == "body" and c[1] == 3 for c in gh.calls)  # ticked on the PR itself


def test_a_claim_without_a_push_ticks_nothing():
    gh = FakeGH([pr_node(state="SUCCESS", failing=())])
    tick, launches, clock = make_tick(
        gh, acceptance=lambda *a: {"result": "UNRUNNABLE", "detail": "no gate commands"}
    )
    tick.run()  # round 1 (the start) pushed but claimed nothing -> round 2
    assert tick.items[0]["action"] == "round"
    tick2, _, _ = make_tick(
        gh,
        now=clock["t"] + 600,
        log="LOOP-TASKS-DONE: T1, T2, A1",
        acceptance=lambda *a: {"result": "UNRUNNABLE", "detail": "no gate commands"},
    )
    tick2.run()  # round 2 pushed nothing: its claim is not evidence
    assert not [c for c in gh.calls if c[0] == "body"] and tick2.items[0]["action"] == "round"


def test_a_gate_error_is_not_cached_for_the_head():
    gh = FakeGH([pr_node(state="SUCCESS", failing=())])
    seen = []

    def gate(*a):
        seen.append(a)
        return {"result": "UNRUNNABLE", "detail": "gate error: fetch failed"}

    tick, _, _ = make_tick(gh, running=False, acceptance=gate)
    tick.run()
    assert "evidence" not in json.loads(ll.state_path("o/r#3").read_text())


def test_unknown_branch_prefix_gets_a_runnable_agent():
    node = pr_node(labels=(ll.LABEL,))
    node["headRefName"] = "orch/issue-7"
    st = ll.Tick(FakeGH([]), apply=False).new_state(node, "o/r#3", {"number": 7}, "o/r")
    assert st["agent"] == "codex" and st["rounds"][0]["log"] is None
    node["headRefName"] = "claude-code/issue-7"
    st = ll.Tick(FakeGH([]), apply=False).new_state(node, "o/r#3", {"number": 7}, "o/r")
    assert st["agent"] == "claude" and st["rounds"][0]["log"].endswith(".claude.log")


def test_repository_exclusion_is_in_both_remote_queries(monkeypatch):
    queries = []

    def gh(args, **kwargs):
        queries.append(next(a[2:] for a in args if a.startswith("q=")))
        return json.dumps({"data": {"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}})

    monkeypatch.setattr(ll, "_gh", gh)
    assert ll.GitHub().looped_prs() == []
    assert ll.GitHub().merged_prs() == []
    assert ll.GitHub().closed_unmerged_prs() == []
    assert len(queries) == 3
    assert all("-repo:stranske/collab-admin" in q for q in queries)


def test_excluded_inventory_never_enters_disposition_or_counts():
    pr = pr_node()
    pr["repository"]["nameWithOwner"] = "STRANSKE/Collab-Admin"
    gh = FakeGH([pr])
    tick, _, _ = make_tick(gh)
    assert tick.step(pr) == {}
    assert "prs 0" in tick.run()
    assert tick.items == []
    assert tick.counts == {}
    assert gh.calls == []


def test_excluded_cache_cannot_claim_custody_or_appear_in_status(monkeypatch):
    target = "stranske/Collab-Admin#1"
    ll.STATE_DIR.mkdir()
    # An unreadable/malformed cached entry must be skipped by its filename.
    ll.state_path(target).mkdir()
    assert ll.owns(target) is None
    gh = FakeGH([])
    monkeypatch.setattr(gh, "merged_prs", lambda: [], raising=False)
    monkeypatch.setattr(gh, "closed_unmerged_prs", lambda: [], raising=False)
    assert ll.status(gh)["looped"] == 0


def test_status_says_what_became_of_each_pr_not_only_what_the_loop_last_did(monkeypatch):
    """stranske/Orchestrator#1260 (2026-11-08): the loop called it done, the owner closed it unmerged, and
    status kept listing it as done. Each row now carries its GitHub outcome."""
    ll.STATE_DIR.mkdir()
    for target, status in (("o/r#1", "done"), ("o/r#2", "done"), ("o/r#3", "active")):
        ll.state_path(target).write_text(
            json.dumps({"target": target, "status": status, "rounds": [{}]})
        )
    gh = FakeGH([])
    node = lambda n: {"number": n, "repository": {"nameWithOwner": "o/r"}}  # noqa: E731
    monkeypatch.setattr(gh, "merged_prs", lambda: [node(1)], raising=False)
    monkeypatch.setattr(gh, "closed_unmerged_prs", lambda: [node(2)], raising=False)
    res = ll.status(gh)
    assert {r["target"]: r["outcome"] for r in res["prs"]} == {
        "o/r#1": "merged",
        "o/r#2": "closed unmerged",
        "o/r#3": "open",
    }
    assert (res["merged"], res["closed_unmerged"]) == (1, 1)
    assert res["pass_a_line"].endswith("looped 3, merged 1, closed unmerged 1")


def test_an_applied_tick_records_one_invocation_heartbeat_once_declared(tmp_path, monkeypatch):
    import capabilities

    ledger = tmp_path / "capabilities.json"
    monkeypatch.setattr(capabilities, "REG", ledger)
    tick_ = type("T", (), {"items": [{}, {}], "counts": {"round": 1}})()
    assert ll.record_heartbeat(tick_) is None  # no row yet: nothing written
    adoptions = []
    monkeypatch.setattr(
        capabilities, "load_declared", lambda path: {"local-loop": {"status": "wired"}}
    )
    monkeypatch.setattr(
        capabilities,
        "adopt_module_version",
        lambda cid, **kw: adoptions.append(cid) or {"adopted": False, "reason": "no source"},
    )
    assert ll.record_heartbeat(tick_) is None  # lineage refused: said on stderr, nothing written
    assert adoptions == ["local-loop"]
    monkeypatch.setattr(
        capabilities,
        "adopt_module_version",
        lambda cid, **kw: {"adopted": True, "capability_version_id": "v1"},
    )
    calls = []
    monkeypatch.setattr(capabilities, "heartbeat", lambda *a, **kw: calls.append((a, kw)) or True)
    assert ll.record_heartbeat(tick_) is True  # lineage adopted, then the heartbeat
    ((args, kw),) = calls
    assert args == ("local-loop", "invocation")
    assert kw["metadata"] == {"prs": 2, "counts": {"round": 1}}
    assert kw["idempotency_key"].startswith("local-loop:")


def test_ended_prs_reads_every_page(monkeypatch):
    pages = iter(
        [
            {
                "nodes": [
                    {"number": n, "repository": {"nameWithOwner": "o/r"}} for n in range(100)
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            },
            {
                "nodes": [{"number": 100, "repository": {"nameWithOwner": "o/r"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        ]
    )
    afters = []

    def gh(args, **kwargs):
        afters.append(next((a for a in args if a.startswith("after=")), None))
        return json.dumps({"data": {"search": next(pages)}})

    monkeypatch.setattr(ll, "_gh", gh)
    assert len(ll.GitHub().merged_prs()) == 101
    assert afters == [None, "after=c1"]
