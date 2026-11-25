"""A host with no local agents (the hosted GitHub seat, owner decision 2026-11-24) tells the router so with
ORCH_LOCAL_AGENT_CAP=0. Without it a runner reads its absent claims file as "unknown", fails toward motion,
and the router chooses LOCAL mode for a start that cannot run there."""

from __future__ import annotations

import router
from test_router_choose_mode import fake_select


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
    monkeypatch.setattr(router.claims, "active_claims", _boom)  # never read when the host has no agents
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {})
    assert (pick["agent"], pick["mode"]) == ("cursor", "remote")
    assert pick["reason"].startswith("offload to hosted: no local agents on this host (ORCH_LOCAL_AGENT_CAP=0)")


def test_zero_cap_reports_blocking_and_drainable_and_who_drains(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "0")
    got = router.local_capacity(running=0, load1=0.1, cores=10)
    assert got["ok"] is False and got["cap"] == 0 and got["drainable"] == 0 and got["no_local_agents"]
    assert "local agents 0/0, drainable 0 here" in got["reason"]
    assert "a seat with local agents runs local-only work" in got["reason"]


def test_zero_cap_keeps_local_only_work_waiting(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "0")
    fake_select(monkeypatch)
    pick = router.choose_mode("implement", {}, needs_local="workflow-file edit")
    assert pick["agent"] is None and pick["mode"] is None
    assert "no local agents on this host" in pick["reason"] and "waits for a local slot" in pick["reason"]


def test_other_values_set_the_cap(monkeypatch):
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "2")
    assert router.local_capacity(running=2, load1=0.1, cores=10)["ok"] is False
    assert router.local_capacity(running=1, load1=0.1, cores=10)["reason"] == "local agents 1/2, load 0.1/20"


def test_unset_or_unreadable_setting_keeps_the_measured_cap(monkeypatch, capsys):
    monkeypatch.delenv(router.LOCAL_AGENT_CAP_ENV, raising=False)
    assert router.local_agent_cap() == (router.LOCAL_AGENT_CAP, "")
    monkeypatch.setenv(router.LOCAL_AGENT_CAP_ENV, "none")
    assert router.local_agent_cap() == (router.LOCAL_AGENT_CAP, "")
    assert "is not an integer" in capsys.readouterr().err
    assert router.local_capacity(running=1, load1=0.1, cores=10)["ok"] is True
