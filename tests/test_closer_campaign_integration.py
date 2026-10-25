"""Core-owned action fences, work-unit granularity, router-based capacity, launcher, qualification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from closer_core_support import core_source
from test_closer_campaign import DIGEST
from test_closer_campaign_exec import Port, manifest_for, pinned_dir

import claims
import closer_campaign as cc
import env_prereq

DRAFT = core_source() / "scripts/closer_runtime.py"
_CORE_SIBLING_ABSENT = env_prereq.core_sibling_absent()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "0")
    monkeypatch.setattr("closer_runtime_binding.policy_problem", lambda policy: None)
    monkeypatch.setattr(
        "closer_runtime_binding.run_native_round",
        lambda command, cfg, runner: cc.run_dispatcher(
            command, timeout=cfg.get("worker_timeout", 120), apply=True, runner=runner
        ),
    )


def spec(head="a", crit="c", action="merge"):
    """Core-valid values: 40-hex head, 64-hex criteria fingerprint, a review revision."""
    return cc.ActionSpec("Org/Repo", 7, action, head * 40, crit * 64, "r1")


# ------------------------------------------------ 1. Core-owned fences gate every path


class FakeCore:
    """Contract double: key from repo+PR+action only; denial and unresolved states are barriers."""

    contract = cc.CONTRACT

    def __init__(self):
        self.rows: dict = {}

    def key(self, s):
        return (s.repository.lower(), s.pr_number, s.action)

    def claim(self, s, holder):
        row = self.rows.get(self.key(s))
        if row and row["state"] == "denied":
            return {"state": "denied"}
        if row and row["state"] in {"intent", "unknown"}:
            return {"state": "unknown"}
        self.rows[self.key(s)] = {"state": "claimed"}
        return {"state": "go", "handle": self.key(s)}

    def record_intent(self, handle, intent):
        self.rows[handle] = {"state": "intent"}

    def settle(self, handle, outcome):
        done = {"ok": "done", "unavailable": "done", "denied": "denied"}
        self.rows[handle] = {"state": done.get(outcome, "unknown")}


@pytest.mark.parametrize("adapter", [None, object()])
def test_absent_or_wrong_adapter_holds_on_preferred_and_basic_paths(adapter):
    preferred, basic = Port("ok"), cc.LocalOnlyPort()
    assert cc.submit_effect(adapter, preferred, spec(), {}, holder="a") == "held"
    assert cc.submit_effect(adapter, basic, spec(), {}, holder="a") == "held"
    assert preferred.calls == 0


def test_barriers_survive_head_change_and_apply_to_both_paths():
    core, timed_out = FakeCore(), Port(exc=TimeoutError())
    assert cc.submit_effect(core, timed_out, spec("a"), {}, holder="a") == "unknown"
    good = Port("ok")
    for port in (good, cc.LocalOnlyPort()):  # preferred again and the basic fallback
        assert cc.submit_effect(core, port, spec("b", "d"), {}, holder="a") == "unknown"
    assert good.calls == 0 and timed_out.calls == 1
    denied = FakeCore()
    assert cc.submit_effect(denied, Port(exc=PermissionError()), spec(), {}, holder="a") == "denied"
    again = Port("ok")
    assert cc.submit_effect(denied, again, spec("9"), {}, holder="a") == "denied"
    assert again.calls == 0


def test_unreadable_store_holds_never_passes():
    class Broken(FakeCore):
        def claim(self, s, holder):
            raise OSError("store unreadable")

    port = Port("ok")
    assert cc.submit_effect(Broken(), port, spec(), {}, holder="a") == "held" and port.calls == 0


def test_pinned_core_must_match_digest_and_contract(tmp_path):
    bad = tmp_path / "core.py"
    bad.write_text("CONTRACT = 'other'\n")
    with pytest.raises(cc.CampaignError):
        cc.PinnedPortable(bad, "0" * 64)
    pinned = cc.PinnedPortable(bad, cc.sha256_file(bad))
    with pytest.raises(cc.CampaignError):  # digest ok, API/contract absent: no substitute
        pinned.operation_adapter(tmp_path / "ops")


@pytest.mark.skipif(
    _CORE_SIBLING_ABSENT is not None,
    reason=_CORE_SIBLING_ABSENT or "Closer Core sibling absent",
)
def test_adapter_over_digest_pinned_core_copy(tmp_path, monkeypatch):
    """Compatibility of the adapter with a digest-pinned COPY of the Core draft (not deployed)."""
    # Core reads fixtures/closer-operation/v1/operation.schema.json relative to itself: that
    # second file is NOT covered by the single-file digest pin (reported gap).
    (tmp_path / "scripts").mkdir()
    core = tmp_path / "scripts" / "closer_runtime.py"
    shutil.copy(DRAFT, core)
    shutil.copytree(
        DRAFT.parents[1] / "fixtures" / "closer-operation",
        tmp_path / "fixtures" / "closer-operation",
    )
    adapter = cc.PinnedPortable(core, cc.sha256_file(core)).operation_adapter(tmp_path / "ops")
    monkeypatch.setattr(adapter.m, "fetch_live_pr_labels", lambda spec: [])
    assert adapter.target_fence_key(spec("a", "c")) == adapter.target_fence_key(spec("b", "d"))
    assert adapter.target_fence_key(spec()) != adapter.target_fence_key(spec(action="verify"))
    first = cc.submit_effect(adapter, Port(exc=TimeoutError()), spec("a"), {}, holder="a")
    assert first == "unknown"
    retry = Port("ok")
    assert cc.submit_effect(adapter, retry, spec("b", "d"), {}, holder="a") == "unknown"
    assert cc.submit_effect(adapter, cc.LocalOnlyPort(), spec("e"), {}, holder="a") == "unknown"
    assert retry.calls == 0
    verify, port = spec(action="verify"), Port("ok")
    assert cc.submit_effect(adapter, port, verify, {}, holder="a") == "ok"
    # Core will not call a port's `ok` terminal: the target stays fenced until read-back
    assert cc.submit_effect(adapter, port, spec("b", action="verify"), {}, holder="a") == "unknown"
    assert port.calls == 1
    status = adapter.reconcile(
        verify,
        "a",
        result="desired_result_observed",
        source="github_readback",
        observation_digest="1" * 64,
    )
    assert status == "succeeded"
    assert cc.submit_effect(adapter, port, spec("b", action="verify"), {}, holder="a") == "ok"
    recon = spec(action="reconcile")
    assert cc.submit_effect(adapter, Port(exc=PermissionError()), recon, {}, holder="a") == "denied"
    later = spec("f", action="reconcile")
    assert cc.submit_effect(adapter, Port("ok"), later, {}, holder="a") == "denied"


# ------------------------------------------------ 2. work-unit granularity


def begin(tmp_path, targets, mode="round", **cfg):
    c = cc.Campaign(tmp_path / "c.campaign.json")
    config = {"granularity": mode, "assignment": "a.json", "dispatch_state": "s.json", **cfg}
    c.begin("camp", targets, owner="o", release_pin=DIGEST, max_workers=2, worker_config=config)
    return c


def stepper(c, pids, **kw):
    def launcher(cmd, log):
        pids.append(cmd)
        return os.getpid()

    return cc.step(
        c, worker_command=lambda t: ["w", t], launcher=launcher, pid_alive=lambda p: True, **kw
    )


def test_round_is_one_serialised_whole_assignment_unit(tmp_path):
    c, pids = begin(tmp_path, ["o/r#1", "o/r#2", "o/r#3"]), []
    assert list(c.read()["targets"]) == [cc.ROUND_UNIT] and c.read()["max_workers"] == 1
    assert stepper(c, pids)["kind"] == "launched"
    second = stepper(c, pids)
    assert second["kind"] == "wait" and second["reason"] == "workers_running" and len(pids) == 1


def test_round_worker_runs_the_whole_assignment_once_and_refuses_a_target(tmp_path):
    c, pids = begin(tmp_path, ["o/r#1", "o/r#2"], dispatch_workers=3, apply=True), []
    stepper(c, pids)
    wid = c.read()["workers"][0]["worker_id"]
    run = []

    def runner(command, **kw):
        run.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    out_path = str(cc.result_path(c.path, wid))
    # A refused invocation has its own durable result; the real worker must never
    # overwrite an earlier receipt (including one bound to a different target).
    refused_path = str(tmp_path / "invalid-invocation" / f"{wid}.json")
    refused = cc.run_worker(str(c.path), refused_path, "o/r#1", "o", 1, runner=runner)
    assert refused in {"denied", "unavailable"}
    assert run == []  # a per-target worker on a round campaign is refused
    assert cc.run_worker(str(c.path), out_path, cc.ROUND_UNIT, "o", 1, runner=runner) == "ok"
    assert len(run) == 1 and run[0][run[0].index("--max-workers") + 1] == "3"
    assert "--target" not in run[0]
    doc = cc.reconcile(c, pid_alive=lambda p: False, release_claim=lambda *a: True)
    assert doc["targets"][cc.ROUND_UNIT] == "done"
    assert cc.next_action(doc)["kind"] == "complete"  # the round is never launched twice


def delegate_double():
    calls = []

    def delegate(agent, target, lane, prompt, **kw):
        calls.append((agent, target, kw.get("profile_id")))
        return {"pid": 4242, "log": "l", "worktree": "w"}

    delegate.calls = calls
    return delegate


ROUTE = {"agent": "gemini", "profile_id": "gemini-low"}
DELEGATE_CFG = {"lane": "l", "prompt": "p"}


def worker_call(c, act, **over):
    kw = dict(agent="gemini", profile_id="gemini-low", route_fn=lambda: ROUTE)
    kw.update(over)
    path = str(cc.result_path(c.path, act["worker_id"]))
    return cc.run_worker(str(c.path), path, act["target"], "o", 1, **kw)


def test_two_bounded_target_workers_never_duplicate_a_target(tmp_path):
    c = begin(tmp_path, ["o/r#1", "o/r#2"], "target-delegate", delegate=DELEGATE_CFG)
    pids, delegate = [], delegate_double()
    a, b = stepper(c, pids), stepper(c, pids)
    assert {a["target"], b["target"]} == {"o/r#1", "o/r#2"}
    assert stepper(c, pids)["reason"] == "workers_running"  # both slots busy; no third launch
    for act in (a, b):
        assert worker_call(c, act, delegate=delegate) == "ok"
    assert sorted(t for _, t, _ in delegate.calls) == ["o/r#1", "o/r#2"]
    assert {p for _, _, p in delegate.calls} == {"gemini-low"}


def test_route_mismatch_and_foreign_real_target_claim_hold_before_effects(tmp_path):
    c = begin(tmp_path, ["o/r#1"], "target-delegate", delegate=DELEGATE_CFG)
    act = stepper(c, [])
    act = {"worker_id": c.read()["workers"][0]["worker_id"], "target": "o/r#1"}
    delegate = delegate_double()
    assert worker_call(c, act, agent="claude", delegate=delegate) == "unavailable"
    stale = lambda: {"hold": "stale"}  # noqa: E731
    assert worker_call(c, act, route_fn=stale, delegate=delegate) == "unavailable"
    assert claims.claim("o/r#1", "codex")  # native owner of the exact real target
    assert worker_call(c, act, delegate=delegate) == "unavailable"
    assert delegate.calls == []


@pytest.mark.parametrize("key", ["o/r#1", "closer-lane", "x-coordinator"])
def test_round_does_not_globalize_item_claims_or_substrings(tmp_path, key):
    c, pids = begin(tmp_path, ["o/r#1"]), []
    assert claims.claim(key, "codex")
    assert stepper(c, pids)["kind"] == "launched"


def test_unreadable_legacy_dispatch_state_holds(tmp_path):
    bad = tmp_path / "legacy.json"
    bad.write_text("{not json")
    c, pids = begin(tmp_path, ["o/r#1"], dispatch_state=str(bad)), []
    assert stepper(c, pids)["reason"] == "native_owner" and pids == []


def test_unsupported_granularity_refuses(tmp_path):
    with pytest.raises(ValueError):
        begin(tmp_path, ["o/r#1"], "per-pr-magic")
    c = begin(tmp_path, ["o/r#1"])
    c.update(lambda d: d["worker_config"].update(granularity="per-pr-magic"))
    pids = []
    assert cc.next_action(c.read())["kind"] == "held" and stepper(c, pids)["kind"] == "held"
    assert pids == []
    result = str(tmp_path / "w.json")
    assert cc.run_worker(str(c.path), result, cc.ROUND_UNIT, "o", 1) == "unavailable"


# ------------------------------------------------ 3. routing follows the router


class FakeProfiles:
    TABLE = {"claude": "claude-low", "gemini": "gemini-low", "aider": "aider-low"}

    @staticmethod
    def profiles_for_agent(agent, transport=None):
        if agent not in FakeProfiles.TABLE:
            return []
        return [
            {
                "agent": agent,
                "profile_id": FakeProfiles.TABLE[agent],
                "reasoning_effort": "low",
                "capacity_pool_ids": [agent],
            }
        ]


class FakeRouter:
    @staticmethod
    def select_agent(task, report, only=None, **kw):
        return (
            {"agent": sorted(only)[0], "selected_profile_id": FakeProfiles.TABLE[sorted(only)[0]]}
            if only
            else None
        )


def capacity(tmp_path, states, age=0):
    path = tmp_path / "cap.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": time.time() - age,
                "agents": {a: {"state": s} for a, s in states.items()},
                "profiles": {FakeProfiles.TABLE[a]: {"state": s} for a, s in states.items()},
                "pools": {a: {"state": s} for a, s in states.items()},
            }
        )
    )
    return path


def route(path, **kw):
    return cc.select_route(path, router=FakeRouter, profiles=FakeProfiles, **kw)


def test_route_uses_router_choice_not_a_hardcoded_agent(tmp_path):
    only_gemini = capacity(tmp_path, {"claude": "exhausted", "gemini": "ok", "aider": "ok"})
    assert route(only_gemini) == {"agent": "gemini", "profile_id": "gemini-low"}
    assert route(capacity(tmp_path, {"claude": "warn", "gemini": "ok"}))["agent"] == "claude"
    assert route(capacity(tmp_path, {"aider": "ok"}))["hold"]  # Aider is never eligible


@pytest.mark.parametrize("ranks", [None, {}, {"closer_interpret": {"gemini": 0}}])
def test_route_without_learned_feedback_retains_qualified_router_prior(tmp_path, ranks):
    seen = []

    class ColdStartRouter(FakeRouter):
        @staticmethod
        def learned_ranks():
            return ranks

        @staticmethod
        def select_agent(task, report, only=None, **kw):
            seen.append((only, kw["learned"]))
            return FakeRouter.select_agent(task, report, only=only, **kw)

    report = capacity(tmp_path, {"claude": "exhausted", "gemini": "ok", "aider": "ok"})
    assert cc.select_route(report, router=ColdStartRouter, profiles=FakeProfiles) == {
        "agent": "gemini",
        "profile_id": "gemini-low",
    }
    assert seen == [({"gemini"}, ranks.get("closer_interpret") if ranks is not None else None)]


def test_stale_partial_or_missing_capacity_holds_without_running_capacity_py(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no subprocess"))
    assert "stale" in route(capacity(tmp_path, {"claude": "ok"}, age=99999))["hold"]
    assert "missing" in route(tmp_path / "none.json")["hold"]
    (tmp_path / "p.json").write_text(json.dumps({"generated_at": time.time(), "agents": {}}))
    assert "partial" in route(tmp_path / "p.json")["hold"]


def test_tick_dispatches_the_provider_it_selected(tmp_path):
    d = pinned_dir(tmp_path)
    root = tmp_path / "campaigns"
    root.mkdir()
    m, path = manifest_for(d, tmp_path)
    shutil.copy(path, root / "pin.manifest.json")
    camp = cc.Campaign(root / "one.campaign.json")
    camp.begin(
        "camp",
        ["o/r#1"],
        owner="o",
        release_pin=m["package_digest"],
        worker_config={"granularity": "target-delegate", "delegate": DELEGATE_CFG},
    )
    cmds = []
    out = cc.tick(
        root,
        path,
        route_fn=lambda cap: ROUTE,
        qualifier=lambda m, r: [],
        launcher=lambda c, log: cmds.append(c) or os.getpid(),
        pid_alive=lambda p: True,
    )
    assert out[0]["kind"] == "launched"
    assert cmds[0][cmds[0].index("--agent") + 1] == "gemini"
    assert cmds[0][cmds[0].index("--profile-id") + 1] == "gemini-low"


def test_tick_holds_launch_when_router_has_no_route(tmp_path):
    d = pinned_dir(tmp_path)
    root = tmp_path / "campaigns"
    root.mkdir()
    m, path = manifest_for(d, tmp_path)
    camp = cc.Campaign(root / "one.campaign.json")
    camp.begin("camp", ["o/r#1"], owner="o", release_pin=m["package_digest"])
    out = cc.tick(
        root,
        path,
        route_fn=lambda cap: {"hold": "stale"},
        qualifier=lambda m, r: [],
        launcher=lambda *a: pytest.fail("launched without capacity"),
        pid_alive=lambda p: True,
    )
    assert out[-1]["reason"] == "capacity_exhausted"


# ------------------------------------------------ 4. launcher and manifest


def test_manifest_names_runtime_launcher_and_workflows_owned_core(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    dist = m["distribution"]
    assert dist["core"]["owner"] == "Workflows" and dist["orchestrator"]["owner"] == "Orchestrator"
    assert m["launcher"]["sha256"] == m["modules"]["closer_launcher.py"]
    assert cc.validate_manifest(m) == []
    bad = json.loads(json.dumps(m))
    bad["distribution"]["core"]["owner"] = "Orchestrator"
    bad["launcher"]["version"] = "99"
    assert len(cc.validate_manifest(bad)) == 2


def launcher_config(d, enabled=True):
    entry = {
        "runner_id": "desktop-launcher",
        "enabled": enabled,
        "interface": "entrypoint",
        "argv": [sys.executable, str(d / "closer_launcher.py")],
    }
    return {"launchers": [entry]}


def test_launcher_entrypoint_is_actually_started_and_observed(tmp_path, monkeypatch):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "secret")  # must not reach the compatibility child
    (receipt,) = cc.produce_launcher_receipt(m, path, launcher_config(d), tmp_path / "work")
    assert receipt["kind"] == "launcher-entrypoint"
    assert receipt["desktop_runtime_validated"] is False
    assert cc.check_harness_receipt(m, receipt) == []
    first, second = receipt["processes"]
    assert first["exit_code"] == -9 and second["exit_code"] == 0
    assert "desktop-app control interface" in receipt["adapter_limitation"]
    assert receipt["compat_mode"]["repository_mutation"] is False
    assert receipt["compat_mode"]["credentials"] == "none"
    verdict = cc.check_promotion(m, ["desktop-launcher"], [receipt])
    assert not verdict["promotable"] and "lifecycle only" in str(verdict)
    off = cc.produce_launcher_receipt(m, path, launcher_config(d, enabled=False), tmp_path / "w2")
    assert off == []


def test_launcher_refuses_credentials_tamper_and_foreign_argv(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    base = [
        sys.executable,
        str(d / "closer_launcher.py"),
        "compat-child",
        "--dir",
        str(tmp_path),
        "--role",
        "first",
        "--manifest",
        str(path),
    ]
    leaked = {**os.environ, "ORCH_CLOSER_COMPAT": "1", "GH_TOKEN": "x"}
    assert subprocess.run(base, env=leaked, capture_output=True).returncode == 78
    foreign = {"launchers": [{"runner_id": "r", "enabled": True, "argv": [sys.executable, "o.py"]}]}
    with pytest.raises(cc.CampaignError):
        cc.produce_launcher_receipt(m, path, foreign, tmp_path / "w")
    (d / "closer_launcher.py").write_text("# tampered\n")
    with pytest.raises(cc.CampaignError):
        cc.produce_launcher_receipt(m, path, launcher_config(d), tmp_path / "w3")


# ------------------------------------------------ 5. qualification before effects


def test_unqualified_pin_is_inert_and_has_no_fallback(tmp_path):
    d = pinned_dir(tmp_path)
    root = tmp_path / "campaigns"
    root.mkdir()
    m, path = manifest_for(d, tmp_path)
    camp = cc.Campaign(root / "one.campaign.json")
    camp.begin(
        "camp",
        ["o/r#1"],
        owner="o",
        release_pin=m["package_digest"],
        worker_config={"assignment": "a", "dispatch_state": "s"},
    )
    out = cc.tick(
        root,
        path,
        route_fn=lambda c: ROUTE,
        launcher=lambda *a: pytest.fail("effect before qualification"),
    )
    assert out[0]["kind"] == "held" and out[0]["reason"] == "version not qualified"
    text = " ".join(out[0]["problems"])
    assert "promotion receipts unreadable" in text and "pinned core unusable" in text
    assert camp.read()["workers"] == []


def test_state_incompatibility_and_missing_receipts_are_judged(tmp_path):
    d = pinned_dir(tmp_path)
    m, path = manifest_for(d, tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "promotion.json").write_text(json.dumps({"runners": ["x"], "receipts": []}))
    assert any("x: no runner receipt" in p for p in cc.qualify(m, root))
    m2 = dict(m, state_versions={"campaign": 2})
    assert any("state version 2" in p for p in cc.qualify(m2, root))
