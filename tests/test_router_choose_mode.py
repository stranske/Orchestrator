"""Local-first mode choice (owner decision 2026-11-08): the router decides WHERE a start runs. Local while
fewer than LOCAL_AGENT_CAP local agents run and the machine is not loaded; hosted keepalive otherwise;
work that must run locally waits for a slot. The reason is part of the answer, because every start
records it next to its mode."""

from __future__ import annotations

import router


def test_local_capacity_reports_blocking_and_drainable_together():
    free = router.local_capacity(running=1, load1=4.0, cores=10)
    assert free["ok"] and free["reason"] == "local agents 1/3, load 4.0/20"
    full = router.local_capacity(running=3, load1=4.0, cores=10)
    assert not full["ok"] and full["drainable"] == 3 and "3/3 (drainable 3" in full["reason"]
    hot = router.local_capacity(running=0, load1=25.0, cores=10)
    assert not hot["ok"] and "load 25.0/20" in hot["reason"]
    drained = router.local_capacity(running=0, load1=0.5, cores=10)
    assert drained["ok"] and drained["reason"].startswith("local agents 0/3")


def test_unknown_inputs_never_close_the_gate():
    assert router.local_capacity(running=None, load1=None, cores=10)["ok"]


def fake_select(monkeypatch, local_pick="codex", remote_pick="cursor"):
    calls = []

    def select_agent(task_type, cap, **kw):
        calls.append(
            (
                "local" if kw.get("profile_transport", "local") == "local" else "remote",
                kw.get("only"),
            )
        )
        if kw.get("profile_transport") == "remote":
            only = kw.get("only") or set()
            return {"agent": sorted(only)[0]} if only else None
        return (
            {"agent": local_pick}
            if local_pick and local_pick in (kw.get("only") or {local_pick})
            else None
        )

    monkeypatch.setattr(router, "select_agent", select_agent)
    monkeypatch.setattr(
        router,
        "select_remote_agent",
        lambda *a, **k: {"agent": remote_pick} if remote_pick else None,
    )
    return calls


OK = {"ok": True, "running": 1, "cap": 3, "drainable": 1, "reason": "local agents 1/3, load 4.0/20"}
FULL = {
    "ok": False,
    "running": 3,
    "cap": 3,
    "drainable": 3,
    "reason": "local agents 3/3 (drainable 3)",
}


def test_local_first_when_capacity_allows(monkeypatch):
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {}, local=OK)
    assert (pick["agent"], pick["mode"]) == ("codex", "local")
    assert pick["reason"] == "local first: local agents 1/3, load 4.0/20"


def test_offload_to_hosted_when_local_is_full(monkeypatch):
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {}, local=FULL)
    assert (pick["agent"], pick["mode"]) == ("cursor", "remote")
    assert pick["reason"] == "offload to hosted: local agents 3/3 (drainable 3)"


def test_offload_when_no_local_agent(monkeypatch):
    fake_select(monkeypatch, local_pick=None)
    pick = router.choose_mode("implement", {}, local=OK)
    assert pick["mode"] == "remote" and "no untried local agent" in pick["reason"]


def test_needs_local_waits_instead_of_offloading(monkeypatch):
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {}, local=FULL, needs_local="workflow-file edit")
    assert pick["mode"] is None and "waits for a local slot" in pick["reason"]
    pick = router.choose_mode("implement", {}, local=OK, needs_local="workflow-file edit")
    assert pick["mode"] == "local" and pick["reason"].startswith("needs local (workflow-file edit)")


def test_tried_agents_are_skipped_on_both_sides(monkeypatch):
    fake_select(monkeypatch, local_pick="codex", remote_pick="codex")
    pick = router.choose_mode("implement", {}, local=FULL, tried={"codex"})
    assert pick["mode"] == "remote" and pick["agent"] != "codex"
