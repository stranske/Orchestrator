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


def test_unknown_inputs_never_close_the_gate(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(router.claims, "active_claims", boom)
    monkeypatch.setattr(router.os, "getloadavg", boom)
    got = router.local_capacity(cores=10)
    assert got["ok"] and got["running"] is None and got["load1"] is None
    assert "local agent cap not enforced" in capsys.readouterr().err


def test_backup_seats_are_never_auto_selected(monkeypatch):
    calls = fake_select(monkeypatch)
    router.choose_mode("implement", {}, local=OK)
    assert all(not (only or set()) & router.BACKUP_AGENTS for _, only in calls)


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


def test_route_source_is_passed_through_on_both_sides(monkeypatch):
    """A start records which ranking chose its agent; choose_mode used to drop select_agent's attribution."""
    monkeypatch.setattr(
        router,
        "select_agent",
        lambda task_type, cap, **kw: {
            "agent": "codex",
            "route_source": "route_weights_comparative",
        },
    )
    monkeypatch.setattr(
        router,
        "select_remote_agent",
        lambda *a, **k: {"agent": "cursor", "route_source": "route_weights"},
    )
    local = router.choose_mode("implement", {}, local=OK)
    assert (local["mode"], local["route_source"]) == ("local", "route_weights_comparative")
    remote = router.choose_mode("implement", {}, local=FULL)
    assert (remote["mode"], remote["route_source"]) == ("remote", "route_weights")


# ---- A host with no local agents (the hosted GitHub seat, owner decision 2026-11-24) tells the router so with
# ORCH_LOCAL_AGENT_CAP=0. Without it a runner reads its absent claims file as "unknown", fails toward motion,
# and the router chooses LOCAL mode for a start that cannot run there.


def _boom(*a, **k):
    raise OSError("no claims file on a runner")


def test_runner_without_the_switch_would_choose_local(monkeypatch):
    # The defect this switch exists for: unreadable claims + no load reading = ok, so mode local.
    monkeypatch.delenv(router.LOCAL_AGENT_CAP_ENV, raising=False)
    monkeypatch.setattr(router.claims, "active_claims", _boom)
    monkeypatch.setattr(router.os, "getloadavg", _boom)
    fake_select(monkeypatch)
    assert router.choose_mode("implement", {})["mode"] == "local"


def test_zero_cap_offloads_every_start_to_hosted(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "0")
    monkeypatch.setattr(
        router.claims, "active_claims", _boom
    )  # never read when the host has no agents
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {})
    assert (pick["agent"], pick["mode"]) == ("cursor", "remote")
    assert pick["reason"].startswith(
        "offload to hosted: no local agents on this host (ORCH_LOCAL_AGENT_CAP=0)"
    )


def test_zero_cap_reports_blocking_and_drainable_and_who_drains(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "0")
    got = router.local_capacity(running=0, load1=0.1, cores=10)
    assert (
        got["ok"] is False and got["cap"] == 0 and got["drainable"] == 0 and got["no_local_agents"]
    )
    assert "local agents 0/0, drainable 0 here" in got["reason"]
    assert "a seat with local agents runs local-only work" in got["reason"]


def test_zero_cap_keeps_local_only_work_waiting(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "0")
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {}, needs_local="workflow-file edit")
    assert pick["agent"] is None and pick["mode"] is None
    assert (
        "no local agents on this host" in pick["reason"]
        and "waits for a local slot" in pick["reason"]
    )


def test_other_values_set_the_cap(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "2")
    assert router.local_capacity(running=2, load1=0.1, cores=10)["ok"] is False
    assert (
        router.local_capacity(running=1, load1=0.1, cores=10)["reason"]
        == "local agents 1/2, load 0.1/20"
    )


def test_unset_or_unreadable_setting_keeps_the_measured_cap(monkeypatch, capsys):
    monkeypatch.delenv(router.LOCAL_AGENT_CAP_ENV, raising=False)
    assert router.local_agent_cap() == (router.LOCAL_AGENT_CAP, "")
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "none")
    assert router.local_agent_cap() == (router.LOCAL_AGENT_CAP, "")
    assert "is not an integer" in capsys.readouterr().err
    assert router.local_capacity(running=1, load1=0.1, cores=10)["ok"] is True
