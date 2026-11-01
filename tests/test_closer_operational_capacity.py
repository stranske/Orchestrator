"""Offline operational-only decisions; no provider or native actions."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

import capacity
import closer_dispatcher
import closer_operational_capacity as scoped
import pacing_usage

NOW = 2_000_000_000
PROFILE = "claude-sonnet-5-5-low"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    polls = tmp_path / ".codex/orchestrator/usage-poll"
    sheds = tmp_path / ".codex/handoff/capacity-shed"
    polls.mkdir(parents=True)
    sheds.mkdir(parents=True)
    manifest = tmp_path / "runtime.json"
    manifest.write_text(
        json.dumps({"modules": {"closer_operational_capacity.py": sha(Path(scoped.__file__))}})
    )
    cfg = dict(
        schema=scoped.SCHEMA,
        owner="offline-owner",
        original_deadline=NOW + 600,
        profile_ids=[PROFILE],
        runtime_manifest=str(manifest),
        runtime_manifest_sha256=sha(manifest),
        bounded_trial_agents=["claude"],
    )
    # Any accidental use of broader readers is a regression, even when they succeed.
    for module, names in (
        (capacity, ("build", "_shed", "_ccusage_active_block")),
        (pacing_usage, ("usage", "usage_snapshot")),
    ):
        for name in names:
            monkeypatch.setattr(
                module, name, lambda *a, **k: pytest.fail("private/global reader used")
            )
    return cfg, polls, sheds


def poll(now=NOW, *, codex=False, percent=24):
    weekly = dict(used_percent=percent, resets_at=NOW + 10_000)
    short = dict(used_percent=0, resets_at=NOW + 1000)
    if codex:
        weekly["window_minutes"] = 10080
        short["window_minutes"] = 300
        return dict(
            ts=now,
            source="codex-app-server:account/rateLimits/read",
            primary=weekly,
            secondary=short,
            ordinary_usage_allowed=True,
        )
    weekly["label"] = "all models"
    return dict(ts=now, source="claude-cli:/usage", weekly=weekly, session=short)


def test_fresh_metrics_exact_new_profiles_and_shared_balance(host):
    cfg, polls, _ = host
    cfg["profile_ids"] += [
        "claude-sonnet-5-5-high",
        "muse-wonder-1.0-low",
        "cursor-gpt-5.6-luna-low",
    ]
    (polls / "claude-usage.json").write_text(json.dumps(poll()))
    report = scoped.evaluate(cfg, now=NOW)
    assert set(report["agents"]) == set(scoped.AGENTS)
    assert set(report["profiles"]) == set(cfg["profile_ids"])
    assert report["agents"]["claude"]["state"] == "ok"
    assert (
        report["profiles"][PROFILE]["capacity_pool_ids"]
        == report["profiles"]["claude-sonnet-5-5-high"]["capacity_pool_ids"]
    )
    assert report["pools"]["claude-subscription"]["used"] is None
    assert scoped.validate_report(report, now=NOW)


def test_declared_profiles_require_a_current_valid_operational_report(host, monkeypatch):
    """Scoped admission cannot be forged by a schema/profile_ids lookalike."""
    cfg, _, _ = host
    report = scoped.evaluate(cfg, now=NOW)
    monkeypatch.setattr(scoped.time, "time", lambda: NOW)
    assert scoped.declared_profile_ids(report) == (PROFILE,)

    stale = copy.deepcopy(report)
    stale["expires_at"] = NOW
    assert scoped.declared_profile_ids(stale) == ()

    malformed = {"schema": scoped.SCHEMA, "profile_ids": [PROFILE]}
    assert scoped.declared_profile_ids(malformed) == ()

    global_report = copy.deepcopy(report)
    global_report.pop("schema")
    assert scoped.declared_profile_ids(global_report) is None

    report_path = Path(cfg["runtime_manifest"]).parent / "oversized-report.json"
    monkeypatch.setenv("CLOSER_CAPACITY_REPORT", str(report_path))
    for field in ("original_deadline", "generated_at", "expires_at"):
        overflowing = copy.deepcopy(report)
        overflowing[field] = 10**4000
        assert scoped.declared_profile_ids(overflowing) == ()
        report_path.write_text(json.dumps(overflowing))
        assert closer_dispatcher._fresh_capacity_report() is None


@pytest.mark.parametrize("kind", ["missing", "stale"])
def test_trial_permission_is_not_quota_freshness(host, kind):
    cfg, polls, _ = host
    if kind == "stale":
        (polls / "claude-usage.json").write_text(json.dumps(poll(NOW - 4000)))
    report = scoped.evaluate(cfg, now=NOW)
    row = report["agents"]["claude"]
    assert row["state"] == "warn" and row["remaining_quota"] == "unknown"
    assert row["observation"]["ts"] == (NOW - 4000 if kind == "stale" else None)
    assert not row["observation"]["poll"]["fresh"]
    cfg["bounded_trial_agents"] = []
    assert scoped.evaluate(cfg, now=NOW)["agents"]["claude"]["state"] == "unknown"


@pytest.mark.parametrize(
    "mutation", ["ordinary_false", "weekly100", "short100", "manual", "active", "invalid"]
)
def test_known_constraints_never_overridden_or_cleared(host, mutation):
    cfg, polls, sheds = host
    value = poll(codex=True)
    if mutation == "ordinary_false":
        value["ordinary_usage_allowed"] = False
    elif mutation == "weekly100":
        value["primary"]["used_percent"] = 100
    elif mutation == "short100":
        value["secondary"]["used_percent"] = 100
    else:
        (sheds / "codex").write_text(
            {"manual": "", "active": json.dumps({"expires_at": NOW + 100}), "invalid": "not-json"}[
                mutation
            ]
        )
    (polls / "codex-usage.json").write_text(json.dumps(value))
    (sheds / "gemini").write_text(json.dumps({"expires_at": NOW + 200, "category": "quota"}))
    before = {p: p.read_bytes() for p in sheds.iterdir()}
    result = scoped.evaluate(cfg, now=NOW)
    assert result["agents"]["codex"]["state"] == "shed"
    assert result["agents"]["gemini"]["state"] == "shed"
    assert before == {p: p.read_bytes() for p in sheds.iterdir()}


@pytest.mark.parametrize("value", [True, -1, 101, "24"])
def test_contradictory_metrics_cannot_gain_trial_permission(host, value):
    cfg, polls, _ = host
    data = poll()
    data["weekly"]["used_percent"] = value
    (polls / "claude-usage.json").write_text(json.dumps(data))
    assert scoped.evaluate(cfg, now=NOW)["agents"]["claude"]["state"] == (
        "shed" if value == 101 else "unknown"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_deadline", NOW),
        ("original_deadline", NOW + 901),
        ("profile_ids", []),
        ("profile_ids", ["unknown"]),
        ("bounded_trial_agents", ["aider"]),
        ("owner", ""),
        ("runtime_manifest_sha256", "0" * 64),
    ],
)
def test_invalid_scope_refuses(host, field, value):
    cfg, _, _ = host
    cfg[field] = value
    with pytest.raises((ValueError, KeyError)):
        scoped.evaluate(cfg, now=NOW)


def test_cas_publication_and_consumer_deadline_scope(host, tmp_path, monkeypatch):
    cfg, _, _ = host
    monkeypatch.setattr(scoped.time, "time", lambda: NOW)
    output = tmp_path / "scoped-round.json"
    report = scoped.publish(cfg, output, expected_sha256=None)
    original = output.read_bytes()
    with pytest.raises(scoped.CapacityError, match="compare-and-swap"):
        scoped.publish(cfg, output, expected_sha256=None)
    assert output.read_bytes() == original
    wrong = {**cfg, "owner": "different-owner"}
    with pytest.raises(scoped.CapacityError, match="owner"):
        scoped.publish(wrong, output, expected_sha256=sha(output))
    scoped.publish(cfg, output, expected_sha256=sha(output))
    monkeypatch.setenv("CLOSER_CAPACITY_REPORT", str(output))
    assert closer_dispatcher._fresh_capacity_report()["profile_ids"] == [PROFILE]
    monkeypatch.setattr(scoped.time, "time", lambda: NOW + 601)
    assert closer_dispatcher._fresh_capacity_report() is None
    altered = copy.deepcopy(report)
    altered["profiles"]["codex-6.1-sol-low"] = {"state": "ok"}
    assert not scoped.validate_report(altered, now=NOW)
    with pytest.raises(scoped.CapacityError):
        scoped.publish(cfg, tmp_path / "capacity.json", expected_sha256=None)


def test_symlink_inputs_and_expired_marker_preserved(host, tmp_path):
    cfg, polls, sheds = host
    marker = sheds / "claude"
    marker.write_text(json.dumps({"expires_at": NOW - 1}))
    old = marker.read_bytes()
    assert scoped.evaluate(cfg, now=NOW)["agents"]["claude"]["state"] == "warn"
    assert marker.read_bytes() == old
    (polls / "claude-usage.json").symlink_to(tmp_path / "anything")
    with pytest.raises(scoped.CapacityError, match="symlink"):
        scoped.evaluate(cfg, now=NOW)


@pytest.mark.parametrize(
    "field,value", [("ts", NOW + 1), ("ts", True), ("source", "foreign-provider"), ("ts", None)]
)
def test_foreign_or_invalid_observation_never_becomes_trial(host, field, value):
    cfg, polls, _ = host
    data = poll()
    data[field] = value
    (polls / "claude-usage.json").write_text(json.dumps(data))
    report = scoped.evaluate(cfg, now=NOW)
    assert report["agents"]["claude"]["state"] == "unknown"
    assert not scoped.validate_admitted_report(
        report,
        now=NOW,
        policy={
            "owner": cfg["owner"],
            "profile_ids": cfg["profile_ids"],
            "bounded_trial_agents": cfg["bounded_trial_agents"],
        },
        runtime_manifest=cfg["runtime_manifest"],
        runtime_manifest_sha256=cfg["runtime_manifest_sha256"],
    )


@pytest.mark.parametrize("new_constraint", ["marker", "ordinary_false"])
def test_new_operational_constraint_invalidates_prior_report_without_mutation(host, new_constraint):
    cfg, polls, sheds = host
    report = scoped.evaluate(cfg, now=NOW)
    assert scoped.validate_report(report, now=NOW)
    if new_constraint == "marker":
        changed = sheds / "claude"
        changed.write_text(json.dumps({"expires_at": NOW + 600, "category": "quota"}))
    else:
        changed = polls / "claude-usage.json"
        changed.write_text(
            json.dumps(
                {
                    **poll(),
                    "weekly": {
                        "used_percent": 100,
                        "resets_at": NOW + 10_000,
                        "label": "all models",
                    },
                }
            )
        )
    original = changed.read_bytes()
    assert not scoped.validate_report(report, now=NOW)
    assert changed.read_bytes() == original


def test_unselected_usage_poll_does_not_invalidate_selected_scope(host):
    cfg, polls, _ = host
    report = scoped.evaluate(cfg, now=NOW)
    assert set(report["input_sha256"]) == {
        str(polls.parent.parent / "handoff/capacity-shed/claude"),
        str(polls / "claude-usage.json"),
    }
    (polls / "codex-usage.json").write_text(
        json.dumps({**poll(codex=True), "ordinary_usage_allowed": False})
    )
    assert scoped.validate_report(report, now=NOW)


def test_selected_codex_usage_remains_a_fail_closed_input(host):
    cfg, polls, _ = host
    cfg["profile_ids"].append("codex-6.1-sol-low")
    (polls / "codex-usage.json").write_text(json.dumps(poll(codex=True)))
    report = scoped.evaluate(cfg, now=NOW)
    assert scoped.validate_report(report, now=NOW)
    policy = {
        "owner": cfg["owner"],
        "profile_ids": cfg["profile_ids"],
        "bounded_trial_agents": cfg["bounded_trial_agents"],
    }
    assert scoped.validate_admitted_report(
        report,
        now=NOW,
        policy=policy,
        runtime_manifest=cfg["runtime_manifest"],
        runtime_manifest_sha256=cfg["runtime_manifest_sha256"],
    )
    (polls / "codex-usage.json").write_text(
        json.dumps({**poll(codex=True), "ordinary_usage_allowed": False})
    )
    assert not scoped.validate_report(report, now=NOW)
    assert not scoped.validate_admitted_report(
        report,
        now=NOW,
        policy=policy,
        runtime_manifest=cfg["runtime_manifest"],
        runtime_manifest_sha256=cfg["runtime_manifest_sha256"],
    )


def test_marker_hash_and_decision_are_same_observation(host, monkeypatch):
    cfg, _, sheds = host
    original = capacity.shed_marker

    def racing(agent, **kwargs):
        value = original(agent, **kwargs)
        if agent == "claude":
            (sheds / agent).write_text(json.dumps({"expires_at": NOW + 500}))
        return value

    monkeypatch.setattr(capacity, "shed_marker", racing)
    with pytest.raises(scoped.CapacityError, match="changed during"):
        scoped.evaluate(cfg, now=NOW)


def test_trial_policy_follows_accounting_capability_not_permanent_agent_category(host, monkeypatch):
    cfg, _, _ = host
    cfg["bounded_trial_agents"] = list(scoped.AGENTS)
    report = scoped.evaluate(cfg, now=NOW)
    assert report["agents"]["vibe"]["state"] == "warn"
    assert report["agents"]["codex"]["state"] == "unknown"  # native ordinary permission missing
    assert report["agents"]["gemini"]["state"] == "unknown"  # windowed counters missing
    monkeypatch.setitem(capacity.AGENTS, "gemini", {"model": "flat"})
    assert scoped.evaluate(cfg, now=NOW)["agents"]["gemini"]["state"] == "warn"
    monkeypatch.setitem(capacity.AGENTS, "claude", {"model": "ccusage", "block_token_limit": 100})
    assert scoped.evaluate(cfg, now=NOW)["agents"]["claude"]["state"] == "unknown"


@pytest.mark.parametrize("window", ["weekly", "session"])
@pytest.mark.parametrize("percent", [100, 101])
def test_expired_claude_exhaustion_still_blocks_explicit_trial(host, window, percent):
    cfg, polls, _ = host
    data = poll(NOW - 4000)
    data[window]["used_percent"] = percent
    data["weekly"]["resets_at"] = NOW - 1
    data["session"]["resets_at"] = NOW - 1
    (polls / "claude-usage.json").write_text(json.dumps(data))
    report = scoped.evaluate(cfg, now=NOW)
    assert report["agents"]["claude"]["state"] == "shed"
    assert report["profiles"][PROFILE]["state"] == "shed"
    assert report["agents"]["claude"]["bounded_trial"] is False


def test_explicit_control_plane_qualification_never_grants_model_capacity(host, monkeypatch):
    cfg, _, _ = host
    cfg.update(control_plane_only=True, profile_ids=[], bounded_trial_agents=[])
    monkeypatch.setattr(scoped.execution_profiles, "get_profile", lambda *_: None)
    report = scoped.evaluate(cfg, now=NOW)
    assert scoped.validate_report(report, now=NOW)
    assert report["profiles"] == report["pools"] == {}
    assert report["profile_ids"] == []
    damaged = copy.deepcopy(report)
    damaged["profiles"] = {PROFILE: {"state": "ok"}}
    assert not scoped.validate_report(damaged, now=NOW)
    cfg["control_plane_only"] = False
    with pytest.raises(scoped.CapacityError, match="scope"):
        scoped.evaluate(cfg, now=NOW)
