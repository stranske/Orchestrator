"""Muse Code as an explicit-only, subscription-login dispatcher backend (issue #612).

Muse is Meta's terminal coding agent. The owner's rule: use the account's subscription capacity
through the CLI login, never direct API spend. Measured 2026-10-15 on the live console: a
`META_API_KEY` call with a Default-Project key lands in the pay-as-you-go ledger (8 requests, 81.5k
input tokens for two capped execs; the `Muse Code`-project key the login mints is untested) while keychain-login calls draw the subscription meter. `META_API_KEY` also always overrides
the login (`muse login --help`), so a stray key in the environment silently converts subscription use
into API spend. These tests pin the three properties that keep it on the login:

1. the argv is headless, sandboxed and does not import the owner's personal Claude/Codex rules;
2. the offload wrapper scrubs `META_API_KEY` from the agent subshell;
3. the seat is a flat subscription seat that the router never picks without explicit demand.
"""

import json
import re
import time
from pathlib import Path

import pytest

import adapters
import agent_auth_check
import agent_switches
import capacity
import dispatcher
import router


def test_muse_command_is_headless_sandboxed_and_isolated():
    argv = adapters.build_command("muse", "do the thing")
    assert argv[:2] == ["muse", "exec"], argv
    assert "do the thing" in argv, argv
    # Unattended, but the sandbox stays ON: --disable-approval skips prompts, --yolo drops both.
    assert "--disable-approval" in argv, argv
    for forbidden in ("--yolo", "--disable-sandbox", "--api-key-stdin"):
        assert forbidden not in argv, (forbidden, argv)
    # The owner's personal Claude Code/Codex rules and skills must not steer a fleet worker, and the
    # session log would persist the prompt on disk.
    assert "--no-foreign-personal-context" in argv, argv
    assert "--no-session-log" in argv, argv


def test_muse_is_a_flat_subscription_seat_and_never_auto_selected():
    seat = capacity.AGENTS["muse"]
    assert seat["model"] == "flat" and "muse" in seat["account"], seat
    assert capacity.agent_tiers()["muse"] == "flat"
    # Backup-only: reachable on explicit demand (`--agent muse`) and never by routine selection,
    # until measured outcomes earn it a prior. The same mechanism aider uses.
    assert "muse" in router.BACKUP_AGENTS, router.BACKUP_AGENTS


def test_muse_offload_wrapper_scrubs_the_api_key(monkeypatch):
    secret = "not-a-real-key-" + "x" * 8
    monkeypatch.setenv("META_API_KEY", secret)
    prelude = dispatcher._agent_runtime_prelude("muse")
    assert "unset META_API_KEY" in prelude, prelude
    # The scrub must never be implemented by copying the value into the wrapper text.
    assert secret not in prelude, "the key value leaked into the wrapper"
    # Only muse's wrapper carries it: every other agent's env is unchanged.
    assert "META_API_KEY" not in dispatcher._agent_runtime_prelude("vibe")


def test_muse_login_state_reads_the_subscription_login_never_its_contents(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    secret_marker = "SECRET-TOKEN-MARKER"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "meta": {
                        "mechanism": "oauth",
                        "storage": "keychain",
                        "obtained_via": "device_code",
                        "subs_active": True,
                        "user_email": secret_marker,
                    }
                },
            }
        )
    )
    monkeypatch.setitem(agent_auth_check.CREDENTIAL_FILES, "muse", (path, "subs_active"))
    live = agent_auth_check._credential_file_state("muse")
    assert live["present"] is True and live["key_present"] is True, live
    assert secret_marker not in json.dumps(live), "login contents leaked into the state"

    # A login without an active subscription is NOT a usable subscription seat.
    data = json.loads(path.read_text())
    data["providers"]["meta"]["subs_active"] = False
    path.write_text(json.dumps(data))
    assert agent_auth_check._credential_file_state("muse")["key_present"] is False

    # An absent login stays a hard, named failure with the refresh hint.
    monkeypatch.setitem(
        agent_auth_check.CREDENTIAL_FILES, "muse", (tmp_path / "absent.json", "subs_active")
    )
    gone = agent_auth_check._credential_file_state("muse")
    assert gone["present"] is False and gone["key_present"] is False, gone
    assert "muse login" in agent_auth_check.REFRESH_HINT["muse"]
    assert "muse" in agent_auth_check.AGENTS


def test_muse_label_is_an_agent_switch_label():
    assert "agent:muse" in agent_switches.AGENT_LABELS


def test_muse_usage_row_is_appended_per_exec(tmp_path, monkeypatch):
    """Muse exposes no usage readout (measured: no token/usage/quota field in `--json`), so the
    dispatcher counts its own execs: one TSV row per attempt, readable by `muse-usage.sh`."""
    log = tmp_path / "muse-usage.log"
    monkeypatch.setenv("ORCH_MUSE_USAGE_LOG", str(log))
    started = time.time() - 12
    dispatcher._record_muse_usage(started, "offload", 0, 12.4)
    dispatcher._record_muse_usage(started + 20, "offload", 1, 3.0)
    rows = [line.split("\t") for line in log.read_text().splitlines()]
    assert len(rows) == 2, rows
    assert rows[0][1:] == ["offload", "0", "12"], rows[0]
    assert rows[1][1:] == ["offload", "1", "3"], rows[1]
    assert rows[0][0].isdigit(), rows[0]
    # A log that cannot be written must never fail the offload it measures.
    monkeypatch.setenv("ORCH_MUSE_USAGE_LOG", str(tmp_path / "no-such-dir" / "deeper" / "x.log"))
    dispatcher._record_muse_usage(started, "offload", 0, 1.0)


def test_muse_usage_is_recorded_for_offload_and_delegate():
    """Wiring pin: BOTH launch paths record usage, each under an `agent == muse` guard.

    The offload attempt loop and `_spawn` (which `run()` and `delegate()` share) each start a
    `muse exec`. Pins the smallest fragment that would be ABSENT if the wiring were removed, with
    nothing a formatter owns, and asserts the exact number of sites: the definition plus one call
    per path, so a deleted call cannot hide behind a surviving one.
    """
    src = Path(dispatcher.__file__).read_text()
    name = "_record_muse" + "_usage("
    assert src.count(name) == 3, "definition + offload attempt + delegate spawn"
    offload = re.findall(r'if agent == "muse":\s*\n(?:\s*#[^\n]*\n)*\s*' + re.escape(name), src)
    spawn = re.findall(
        r'if d\["agent"\] == "muse":\s*\n(?:\s*#[^\n]*\n)*\s*' + re.escape(name), src
    )
    assert len(offload) == 1 and len(spawn) == 1, (len(offload), len(spawn))


def test_muse_is_refused_under_a_write_boundary():
    """muse already runs in its own sandbox; wrapping it in a seatbelt profile is a nested seatbelt
    that has not been shown to hold, so a bounded run refuses by name instead of running unbounded.
    """
    reason = adapters.write_boundary_unenforceable("muse")
    assert reason and "muse" in reason and "nested" in reason, reason
    # Other agents' answers are unchanged by the new branch.
    assert adapters.write_boundary_unenforceable("codex") != reason


def test_muse_has_an_offload_profile_in_its_own_flat_pool():
    """A worker attempt needs a profile and a pool (`test_every_agent_can_record_a_worker_attempt`).
    The model is the one the CLI actually started on for this account, passed explicitly."""
    import execution_profiles

    profiles = execution_profiles.profiles_for_agent("muse", transport="offload")
    assert [p["profile_id"] for p in profiles] == ["muse-wonder-1.0"], profiles
    pool = execution_profiles.CAPACITY_POOLS["muse-subscription"]
    assert pool["agent"] == "muse" and pool["tier"] == "flat", pool
    argv = adapters.build_command("muse", "x", requested_model="muse-wonder-1.0")
    assert argv[argv.index("--model") + 1] == "muse-wonder-1.0", argv
    assert "--model" not in adapters.build_command("muse", "x"), "no model unless one is requested"


def test_muse_runtime_home_carries_the_login_record(tmp_path, monkeypatch):
    """Found by a live offload (2026-10-15): the dispatcher moves XDG_CONFIG_HOME into the per-agent
    runtime dir, Muse looks for its login record there, and without a link it reported "missing meta
    credentials" even though the owner is logged in. The record is linked, never copied, and the
    link target is the real file."""
    real_home = tmp_path / "home"
    login = real_home / ".config" / "muse" / "auth.json"
    login.parent.mkdir(parents=True)
    login.write_text("{}")
    monkeypatch.setattr(dispatcher, "REAL_HOME", real_home)
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    base = dispatcher._ensure_agent_runtime("muse")
    linked = base / ".config" / "muse" / "auth.json"
    assert linked.is_symlink() and linked.resolve() == login.resolve(), linked


def test_muse_direct_dispatch_scrubs_the_key_from_a_copied_environment(monkeypatch):
    """`adapters.dispatch` hands `env` straight to subprocess.run, bypassing the dispatcher prelude.
    muse gets a COPY without META_API_KEY in both cases (inherited env and supplied mapping); the
    caller's mapping is never mutated and every other agent's environment is untouched."""
    fake = "not-a-real-key-" + "y" * 8
    monkeypatch.setenv("META_API_KEY", fake)
    inherited = adapters.agent_subprocess_env("muse", None)
    assert (
        inherited is not None and "META_API_KEY" not in inherited
    ), "inherited env must be scrubbed"
    supplied = {"PATH": "/usr/bin", "META_API_KEY": fake}
    cleaned = adapters.agent_subprocess_env("muse", supplied)
    assert "META_API_KEY" not in cleaned and cleaned["PATH"] == "/usr/bin", cleaned
    assert supplied["META_API_KEY"] == fake, "the caller's mapping must not be mutated"
    assert adapters.agent_subprocess_env("vibe", None) is None, "other agents keep inheriting"
    assert adapters.agent_subprocess_env("vibe", supplied) is supplied, "other agents are untouched"
    # Wiring pin: dispatch passes the effective env, once.
    src = Path(adapters.__file__).read_text()
    assert src.count("env=agent_subprocess" + "_env(agent, env)") == 1


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "null",
        "not json at all",
        '{"providers": null}',
        '{"providers": []}',
        '{"providers": {"meta": null}}',
        '{"providers": {"meta": []}}',
        '{"providers": {"meta": {"mechanism": "oauth", "subs_active": "yes"}}}',
    ],
)
def test_muse_malformed_login_records_are_unusable_never_an_exception(text):
    assert agent_auth_check._login_present("muse", "subs_active", text) is False, text


def test_muse_usage_log_failure_is_reported_not_swallowed(tmp_path, monkeypatch, capsys):
    """A measurement that cannot be written must not fail the run, but it must not vanish either:
    the only consumption record is the one that was just lost, so say so on stderr."""
    monkeypatch.setenv("ORCH_MUSE_USAGE_LOG", str(tmp_path / "missing" / "deeper" / "x.log"))
    dispatcher._record_muse_usage(time.time(), "offload", 0, 1.0)  # must not raise
    err = capsys.readouterr().err
    assert "muse usage" in err and "not recorded" in err, err


def test_muse_has_no_route_table_entry_so_only_explicit_dispatch_reaches_it():
    """Explicit demand means `dispatcher.py offload/delegate --agent muse`. `select_agent` filters
    route-table entries and muse has none, so it can never be returned by selection, even with
    `only={"muse"}`; BACKUP_AGENTS then also holds if an entry is ever added."""
    in_table = {e["agent"] for entries in router.ROUTE_TABLE.values() for e in _entries(entries)}
    assert "muse" not in in_table, "a route-table entry would make selection able to return muse"
    assert "muse" in router.BACKUP_AGENTS


def _entries(value):
    """ROUTE_TABLE values are lists of entries, or mappings of lists, depending on the task type."""
    if isinstance(value, dict):
        for inner in value.values():
            yield from _entries(inner)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict) and "agent" in item:
                yield item
            else:
                yield from _entries(item)
