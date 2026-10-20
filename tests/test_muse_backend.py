"""Muse Code as an explicit-only, subscription-login dispatcher backend (issue #612).

Muse is Meta's terminal coding agent. The owner's rule: use the account's subscription capacity
through the CLI login, never direct API spend. Measured 2026-10-15 on the live console: a
`META_API_KEY` call with a Default-Project key lands in the pay-as-you-go ledger (8 requests, 81.5k
input tokens for two capped execs; the `Muse Code`-project key the login mints is untested) while keychain-login calls draw the subscription meter. `META_API_KEY` also always overrides
the login (`muse login --help`), so a stray key in the environment silently converts subscription use
into API spend. These tests pin the three properties that keep it on the login:

1. the argv is headless, sandboxed and does not import the owner's personal Claude/Codex rules;
2. the offload wrapper scrubs `META_API_KEY` from the agent subshell;
3. the seat is a bounded, metered subscription seat that the router never picks without explicit demand.
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
import feedback
import roles
import router


def _runtime_login_fixture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    login = home / ".config/muse/auth.json"
    login.parent.mkdir(parents=True)
    (login.parents[2] / "Library/Keychains").mkdir(parents=True)
    login.write_text(
        json.dumps({"providers": {"meta": {"mechanism": "oauth", "subs_active": True}}})
    )
    monkeypatch.setattr(adapters, "HOME", home)
    monkeypatch.setattr(adapters, "AGENT_RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(dispatcher, "REAL_HOME", home)
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")


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


def test_muse_offload_argv_and_isolated_runtime_home(tmp_path, monkeypatch):
    """The execution argv uses the supported file/JSON/step-bound contract and the dispatcher
    redirects mutable XDG state into the agent runtime while linking only the login record."""
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("do the thing")
    argv = adapters.build_command("muse", "ignored", prompt_file=prompt)
    assert argv[:2] == ["muse", "exec"]
    assert argv[argv.index("--prompt-file") + 1] == str(prompt)
    assert "--json" in argv and "--max-model-steps" in argv
    assert int(argv[argv.index("--max-model-steps") + 1]) > 0
    assert "--no-foreign-personal-context" in argv and "--no-session-log" in argv

    real_home = tmp_path / "home"
    login = real_home / ".config" / "muse" / "auth.json"
    login.parent.mkdir(parents=True)
    (login.parents[2] / "Library/Keychains").mkdir(parents=True)
    login.write_text("{}")
    monkeypatch.setattr(dispatcher, "REAL_HOME", real_home)
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    runtime = dispatcher._ensure_agent_runtime("muse")
    prelude = dispatcher._agent_runtime_prelude("muse")
    assert f"XDG_CONFIG_HOME={runtime / '.config'}" in prelude
    assert f"XDG_STATE_HOME={runtime / '.local/state'}" in prelude
    assert f"export HOME={runtime}" in prelude, prelude
    assert runtime / ".config" / "muse" != real_home / ".config" / "muse"
    assert f"MUSE_AUTH_PATH={runtime / '.config' / 'muse' / 'auth.json'}" in prelude
    linked = runtime / ".config" / "muse" / "auth.json"
    assert linked.is_symlink() and linked.resolve() == login.resolve()


def test_muse_seat_is_reported_and_never_reads_as_free(tmp_path, monkeypatch, capsys):
    seat = capacity.AGENTS["muse"]
    assert seat["model"] == "metered" and seat["account"] == "meta-muse-code-sub", seat
    # The immutable execution pool retains its paid subscription pricing tier. The capacity
    # model describes allowance metering; neither field claims an unlimited/free seat.
    assert capacity.agent_tiers()["muse"] == "flat"
    # Backup-only: reachable on explicit demand (`--agent muse`) and never by routine selection,
    # until measured outcomes earn it a prior. The same mechanism aider uses.
    assert "muse" in router.BACKUP_AGENTS, router.BACKUP_AGENTS

    # Exercise the JSON entry point offline, with an authenticated seat and private state.
    # Checking AGENTS alone misses a model omitted from the snapshot consumed by callers.
    monkeypatch.setattr(capacity, "HANDOFF", tmp_path)
    monkeypatch.setattr(capacity, "OUT", tmp_path / "capacity.json")
    monkeypatch.setattr(capacity, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "capacity-shed")
    monkeypatch.setattr(capacity, "_ccusage_active_block", lambda: None)
    monkeypatch.setattr(capacity, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(capacity, "_model_health", lambda *args: None)
    monkeypatch.setattr(
        capacity, "_auth_health", lambda agent: {"checked": True, "authenticated": True}
    )
    assert capacity.main(["--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot == json.loads(capacity.OUT.read_text())
    reported = snapshot["agents"]["muse"]
    assert reported["model"] == "metered"
    assert reported["account"] == "meta-muse-code-sub"
    assert reported["state"] == capacity.OK
    assert snapshot["pools"]["muse-subscription"]["limit"] is None

    # An observed rate limit overrides subscription availability without inventing a reset.
    capacity.SHED_DIR.mkdir()
    (capacity.SHED_DIR / "muse").write_text(
        json.dumps({"category": "rate_limit", "incident_id": "muse-observed-429"})
    )
    assert capacity.main(["--json"]) == 0
    limited = json.loads(capsys.readouterr().out)
    assert limited["agents"]["muse"]["model"] == "metered"
    assert limited["agents"]["muse"]["state"] == capacity.SHED
    assert limited["pools"]["muse-subscription"]["state"] == capacity.SHED
    assert capacity.shed_marker("muse")["reset_at"] is None


def test_muse_offload_wrapper_scrubs_the_api_key(tmp_path, monkeypatch):
    _runtime_login_fixture(tmp_path, monkeypatch)
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


def test_muse_feedback_row_records_measured_attempts_and_imputed_subscription_cost(
    tmp_path, monkeypatch
):
    """A joined feedback row carries observed facts rather than fabricated tokens/dollars or a
    provider reset. The zero is meaningful only with its imputation labels."""
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    feedback.record_run("muse-run", "owner/repo#1", "implement", "muse", mode="offload")
    feedback.record_muse_subscription_cost(
        "muse-run",
        invocation_count=2,
        window_start_ts=18_000,
        window_end_ts=36_000,
        observed_start_ts=18_123,
        observed_end_ts=18_140,
        wall_seconds=12.5,
        raw_exit_codes=[75, 0],
    )
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT r.agent,c.tokens_in,c.tokens_out,c.cost_usd,c.source,u.measurement_kind,"
            "u.muse_invocation_count,u.muse_window_id,u.muse_observed_start_ts,u.muse_observed_end_ts,"
            "u.muse_wall_seconds,u.muse_raw_exit_codes FROM runs r JOIN costs c ON r.run_id=c.run_id "
            "JOIN muse_subscription_usage u ON u.run_id=r.run_id "
            "WHERE r.run_id='muse-run'"
        ).fetchone()
    assert row == (
        "muse",
        None,
        None,
        0.0,
        "imputed:subscription",
        "imputed_subscription",
        2,
        "observation:rolling-5h:18000-36000;observed:18123-18140;provider-reset:unknown",
        18_123,
        18_140,
        12.5,
        "[75,0]",
    )
    assert "record_muse_subscription_cost(" in Path(dispatcher.__file__).read_text()


def test_muse_usage_extension_keeps_legacy_cost_writers_and_measured_rows(tmp_path, monkeypatch):
    """The additive usage table cannot widen or replace the historical seven-column cost contract."""
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    feedback.record_run("muse-measured", "owner/repo#2", "review", "muse", mode="offload")
    with feedback._conn() as conn:
        # This is the exact old writer shape that must remain valid after the Muse extension lands.
        conn.execute(
            "INSERT OR REPLACE INTO costs VALUES (?,?,?,?,?,?,?)",
            ("muse-measured", 10, 20, 4.25, 9.0, "ccusage", 1),
        )
    feedback.record_muse_subscription_cost(
        "muse-measured",
        invocation_count=1,
        window_start_ts=36_000,
        window_end_ts=54_000,
        observed_start_ts=36_100,
        observed_end_ts=36_105,
        wall_seconds=5.0,
        raw_exit_codes=[0],
    )
    with feedback._conn() as conn:
        cost = conn.execute(
            "SELECT tokens_in,tokens_out,cost_usd,latency_s,source FROM costs WHERE run_id=?",
            ("muse-measured",),
        ).fetchone()
        usage = conn.execute(
            "SELECT measurement_kind,muse_invocation_count,muse_raw_exit_codes "
            "FROM muse_subscription_usage WHERE run_id=?",
            ("muse-measured",),
        ).fetchone()
    assert cost == (10, 20, 4.25, 9.0, "ccusage")
    # Richer provider writers can supersede an imputation after it is recorded;
    # the ledger reconciler itself only writes source=ledger, never provider receipts.
    feedback.record_run("muse-later", "owner/repo#3", "review", "muse", mode="offload")
    feedback.record_muse_subscription_cost(
        "muse-later",
        invocation_count=1,
        window_start_ts=0,
        window_end_ts=18000,
        observed_start_ts=18000,
        observed_end_ts=18005,
        wall_seconds=5.0,
        raw_exit_codes=[0],
    )
    feedback.record_cost(
        "muse-later", tokens_in=10, tokens_out=20, cost_usd=4.25, source="langsmith"
    )
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT tokens_in,tokens_out,cost_usd,source FROM costs WHERE run_id='muse-later'"
        ).fetchone() == (10, 20, 4.25, "langsmith")
    assert usage == ("imputed_subscription", 1, "[0]")


def _native_muse_events(text="completed the bounded task"):
    """Sanitized framing from native exec receipt offload:muse:1792137802883875000.

    Preserve the actual protocol shapes; omit private prompt/session identifiers and timestamps.
    """
    events = []
    for kind, payload in [
        (
            "runtime.command.accepted",
            {"kind": "command_accepted", "command_id": "c1", "command_kind": "turn.submit"},
        ),
        (
            "session.run.linked",
            {
                "kind": "session_run_linked",
                "command_id": "c1",
                "run_stream": {"kind": "run", "id": "c1"},
            },
        ),
        (
            "run.lifecycle.started",
            {"kind": "run_started", "command_id": "c1", "run_stream": {"kind": "run", "id": "c1"}},
        ),
        (
            "run.terminal.completed",
            {
                "kind": "run_terminal",
                "command_id": "c1",
                "run_stream": {"kind": "run", "id": "c1"},
                "terminal": "completed",
                "text": text,
                "reason": None,
            },
        ),
    ]:
        events.append(
            {
                "schema_version": 1,
                "payload_schema_version": 1,
                "stream": {"kind": "session", "id": "s1"},
                "record_type": "reconciliation" if not events else "event",
                "durability": "durable",
                "causation_id": "c1",
                "payload_type": kind,
                "payload": payload,
            }
        )
    return events


def test_muse_jsonl_requires_a_terminal_textual_result():
    def encode(events):
        return "\n".join(json.dumps(e) for e in events)

    events = _native_muse_events()
    assert adapters.muse_terminal_text(encode(events)) == "completed the bounded task"
    assert adapters.muse_terminal_text(encode(events[:-1])) == ""
    # Authentic native review found this progress-only MSP counterexample in the old parser.
    progress = {
        "method": "item/completed",
        "params": {
            "item": {"kind": "agentMessage", "text": "Now applying a fix and rerunning tests."}
        },
    }
    assert adapters.muse_terminal_text(json.dumps(progress)) == ""
    assert adapters.muse_terminal_text(json.dumps({"type": "result", "result": "unbound"})) == ""
    assert adapters.muse_terminal_text(encode(events + [events[-1]])) == ""
    assert adapters.muse_terminal_text(encode(events) + "\n{truncated") == ""


@pytest.mark.parametrize("kind", ["run.lifecycle.failed", "unknown.durable.event"])
def test_muse_jsonl_rejects_unknown_conflicts_and_post_terminal_records(kind):
    events = _native_muse_events()
    conflict = dict(events[-1], payload_type=kind)
    assert (
        adapters.muse_terminal_text(
            "\n".join(json.dumps(e) for e in events[:-1] + [conflict, events[-1]])
        )
        == ""
    )
    assert adapters.muse_terminal_text("\n".join(json.dumps(e) for e in events + [conflict])) == ""


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_muse_jsonl_rejects_malformed_schema_version(version):
    events = _native_muse_events()
    events[-1]["schema_version"] = version
    assert adapters.muse_terminal_text("\n".join(json.dumps(e) for e in events)) == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("terminal", "failed"),
        ("command_id", "another-run"),
        ("run_stream", {"kind": "run", "id": "another-run"}),
        ("reason", "limit"),
        ("text", ""),
    ],
)
def test_muse_jsonl_rejects_failed_or_unbound_terminal(field, value):
    events = _native_muse_events()
    events[-1]["payload"][field] = value
    assert adapters.muse_terminal_text("\n".join(json.dumps(e) for e in events)) == ""


def test_muse_native_tool_results_preserve_the_bound_run_terminal():
    """Captured native tool errors are task outcomes, not a missing run response.

    The enclosing run completed usefully after reporting a denied read and a shell
    sandbox startup failure. Neither task success nor failure is a run terminal.
    """
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/muse-native-tool-events.json").read_text()
    )
    events = _native_muse_events("The read boundary held; shell nesting failed.")
    tool_events = fixture["events"]

    def encode(rows):
        return "\n".join(json.dumps(e) for e in rows)

    assert (
        adapters.muse_terminal_text(encode(events[:-1] + tool_events + events[-1:]))
        == events[-1]["payload"]["text"]
    )
    assert adapters.muse_terminal_text(encode(events[:-1] + tool_events)) == ""
    assert adapters.muse_terminal_text(encode(events + tool_events)) == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("command_id", "other"),
        ("run_stream", {"kind": "run", "id": "other"}),
        ("kind", "run_terminal"),
    ],
)
def test_muse_native_tool_results_reject_unbound_payloads(field, value):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/muse-native-tool-events.json").read_text()
    )
    for auxiliary in fixture["events"]:
        auxiliary["payload"][field] = value
        events = _native_muse_events()
        assert (
            adapters.muse_terminal_text(
                "\n".join(json.dumps(e) for e in events[:-1] + [auxiliary] + events[-1:])
            )
            == ""
        )


def test_muse_native_tool_results_reject_malformed_child_identity():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/muse-native-tool-events.json").read_text()
    )
    for auxiliary in fixture["events"]:
        payload = auxiliary["payload"]
        if auxiliary["payload_type"] == "tool.result":
            payload["call_id"] = None
        else:
            payload["event"]["task_id"] = "another-task"
        events = _native_muse_events()
        assert (
            adapters.muse_terminal_text(
                "\n".join(json.dumps(e) for e in events[:-1] + [auxiliary] + events[-1:])
            )
            == ""
        )


def test_muse_usage_is_recorded_for_offload_and_delegate():
    """Wiring pin: BOTH launch paths record usage, each under an `agent == muse` guard.

    The offload attempt loop and `_spawn` (which `run()` and `delegate()` share) each start a
    `muse exec`. Pins the smallest fragment that would be ABSENT if the wiring were removed, with
    nothing a formatter owns, and asserts the exact number of sites: the definition plus one call
    per path, so a deleted call cannot hide behind a surviving one.
    """
    src = Path(dispatcher.__file__).read_text()
    name = "_record_muse" + "_usage("
    assert src.count("def " + name) == 1, "exactly one definition"
    attempt_loop = src[src.index("for attempt in range(1, max_network_retries + 2):") :]
    offload = re.findall(r'if agent == "muse":' + r"[\s\S]{0,900}?" + re.escape(name), attempt_loop)
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


def test_muse_has_an_offload_profile_in_its_existing_subscription_pool():
    """A worker attempt needs a profile and a pool (`test_every_agent_can_record_a_worker_attempt`).
    The model is the one the CLI actually started on for this account, passed explicitly."""
    import execution_profiles

    profiles = execution_profiles.profiles_for_agent("muse", transport="offload")
    assert [p["profile_id"] for p in profiles] == [
        "muse-wonder-1.0",
        "muse-wonder-1.0-low",
    ], profiles
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
    (login.parents[2] / "Library/Keychains").mkdir(parents=True)
    login.write_text("{}")
    monkeypatch.setattr(dispatcher, "REAL_HOME", real_home)
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    base = dispatcher._ensure_agent_runtime("muse")
    linked = base / ".config" / "muse" / "auth.json"
    assert linked.is_symlink() and linked.resolve() == login.resolve(), linked


def test_muse_direct_dispatch_scrubs_the_key_from_a_copied_environment(tmp_path, monkeypatch):
    """`adapters.dispatch` hands `env` straight to subprocess.run, bypassing the dispatcher prelude.
    muse gets a COPY without META_API_KEY in both cases (inherited env and supplied mapping); the
    caller's mapping is never mutated and every other agent's environment is untouched."""
    _runtime_login_fixture(tmp_path, monkeypatch)
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
    assert src.count("process_env = agent_subprocess" + "_env(agent, env)") == 1
    assert src.count("env=process_env,") == 1


def test_muse_direct_dispatch_environment_is_xdg_isolated(tmp_path, monkeypatch):
    real_home = tmp_path / "home"
    login = real_home / ".config" / "muse" / "auth.json"
    login.parent.mkdir(parents=True)
    (login.parents[2] / "Library/Keychains").mkdir(parents=True)
    login.write_text("{}")
    monkeypatch.setattr(adapters, "HOME", real_home)
    monkeypatch.setattr(adapters, "AGENT_RUNTIME", tmp_path / "runtime")
    env = adapters.agent_subprocess_env(
        "muse", {"HOME": str(real_home), "META_API_KEY": "nope", "MUSE_AUTH_PATH": "other-login"}
    )
    runtime = tmp_path / "runtime" / "muse"
    assert env["XDG_CONFIG_HOME"] == str(runtime / ".config")
    assert env["XDG_STATE_HOME"] == str(runtime / ".local/state")
    assert env["HOME"] == str(runtime)
    assert Path(env["XDG_CONFIG_HOME"]) / "muse" != real_home / ".config" / "muse"
    assert env["MUSE_AUTH_PATH"] == str(runtime / ".config" / "muse" / "auth.json")
    assert env["TMPDIR"] == str(runtime / "tmp") and "META_API_KEY" not in env
    linked = runtime / ".config" / "muse" / "auth.json"
    assert linked.is_symlink() and linked.resolve() == login.resolve()
    # A dangling alias from an older runtime must point to today's intended OAuth record,
    # rather than merely suppressing FileExistsError and leaving authentication broken.
    linked.unlink()
    linked.symlink_to(tmp_path / "missing-old-login.json")
    adapters.agent_subprocess_env("muse", {"HOME": str(real_home)})
    assert linked.resolve() == login.resolve() and login.read_text() == "{}"
    linked.unlink()
    linked.write_text("unexpected private credential record")
    with pytest.raises(ValueError, match="unexpected non-link"):
        adapters.agent_subprocess_env("muse", {"HOME": str(real_home)})
    assert linked.read_text() == "unexpected private credential record"
    # The offload/delegate path uses the same repaired alias contract.
    linked.unlink()
    linked.symlink_to(tmp_path / "another-missing-login.json")
    monkeypatch.setattr(dispatcher, "REAL_HOME", real_home)
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    dispatcher._ensure_agent_runtime("muse")
    assert linked.resolve() == login.resolve()


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


def test_muse_is_eligible_for_declared_roles_but_never_auto_selected(monkeypatch):
    """Muse is declared for implement/review role exploration, while BACKUP_AGENTS keeps it out of
    ordinary deterministic selection. Explicit `only={"muse"}` is the deliberate entry point."""
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    monkeypatch.setattr(feedback, "current_weights", lambda *args, **kwargs: [])
    in_table = {e["agent"] for entries in router.ROUTE_TABLE.values() for e in _entries(entries)}
    assert "muse" in in_table
    assert "muse" in router.BACKUP_AGENTS
    assert "muse" in roles.ROLE_REGISTRY["prompt"].eligible_backends
    for role_name in ("redirect", "triage", "adjudicator"):
        assert "muse" in roles.ROLE_REGISTRY[role_name].eligible_backends
    cap = {"agents": {agent: {"state": "ok"} for agent in in_table}}
    assert router.select_agent("implement", cap)["agent"] != "muse"
    assert router.select_agent("review", cap)["agent"] != "muse"
    assert router.select_agent("implement", cap, only={"muse"})["agent"] == "muse"
    assert roles.route_role("prompt", cap, only={"muse"})["agent"] == "muse"
    assert roles.route_role("redirect", cap, only={"muse"})["agent"] == "muse"
    muse_only_capacity = {"agents": {agent: {"state": "shed"} for agent in in_table}}
    muse_only_capacity["agents"]["muse"] = {"state": "ok"}
    learned_muse_first = {"implement": {"muse": {"rank": 0}}}
    assert roles.route_role("prompt", muse_only_capacity, learned=learned_muse_first) is None
    assert roles.route_role("prompt", muse_only_capacity, only={"muse"})["agent"] == "muse"


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


def _muse_login(tmp_path, monkeypatch, *, mechanism="oauth", active=True):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"providers": {"meta": {"mechanism": mechanism, "subs_active": active}}})
    )
    monkeypatch.setitem(agent_auth_check.CREDENTIAL_FILES, "muse", (path, "subs_active"))
    return path


def test_muse_auth_health_reads_the_login_record(tmp_path, monkeypatch):
    """No CLI probe exists, so the free signal is the login record: absent or inactive is an
    EXPLICIT failure (capacity sheds on it); an unreadable record is not checked, never a failure.
    """
    path = _muse_login(tmp_path, monkeypatch)
    ok = adapters.auth_health("muse")
    assert ok["checked"] is True and ok["authenticated"] is True, ok
    path.write_text(
        json.dumps({"providers": {"meta": {"mechanism": "oauth", "subs_active": False}}})
    )
    bad = adapters.auth_health("muse")
    assert bad["checked"] is True and bad["authenticated"] is False, bad
    assert "muse login" in bad["reason"], bad
    path.unlink()
    gone = adapters.auth_health("muse")
    assert gone["authenticated"] is False, gone


def test_muse_credential_missing_is_reported_without_a_billing_call(tmp_path, monkeypatch):
    missing = tmp_path / "no-login.json"
    monkeypatch.setitem(agent_auth_check.CREDENTIAL_FILES, "muse", (missing, "subs_active"))

    def forbidden_call(*args, **kwargs):
        raise AssertionError("missing-credential health must not execute a provider CLI")

    monkeypatch.setattr(adapters.subprocess, "run", forbidden_call)
    health = adapters.auth_health("muse")
    assert health["checked"] is True and health["authenticated"] is False
    assert "muse login" in health["reason"]

    monkeypatch.setattr(adapters, "HOME", tmp_path / "missing-home")
    monkeypatch.setattr(adapters, "AGENT_RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(dispatcher, "REAL_HOME", tmp_path / "missing-home")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(adapters, "muse_prompt_file", forbidden_call)
    with pytest.raises(ValueError, match="login record is missing"):
        adapters.dispatch("muse", "never call a provider", cwd=str(workspace))
    with pytest.raises(ValueError, match="login record is missing"):
        dispatcher._agent_runtime_prelude("muse")

    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args, **kwargs: prompt)
    with pytest.raises(ValueError, match="login record is missing"):
        dispatcher.offload("muse", "never allocate a prompt or call a provider", cwd=str(workspace))


def test_muse_capacity_sheds_on_an_inactive_subscription_and_keeps_an_active_one(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    path = _muse_login(tmp_path, monkeypatch, active=True)
    assert capacity._classify("muse", capacity.AGENTS["muse"], None)[0] == capacity.OK
    path.write_text(
        json.dumps({"providers": {"meta": {"mechanism": "oauth", "subs_active": False}}})
    )
    state = capacity._classify("muse", capacity.AGENTS["muse"], None)
    assert state[0] == capacity.SHED and "not authenticated" in state[1], state


def test_muse_record_that_is_not_an_oauth_login_is_not_admitted(tmp_path, monkeypatch):
    """The CLI keeps ONE saved credential per provider (`muse logout` removes 'API key or account
    login'), so a key saved with `muse auth set` replaces the login record instead of sitting
    beside it. The gate admits only mechanism == oauth with an active subscription, so a record
    that is not an OAuth login cannot pass. (Whether a stored key could ever COEXIST with a login
    and take precedence is unverified: pinned here only as far as the record itself goes.)"""
    path = _muse_login(tmp_path, monkeypatch, mechanism="api_key", active=True)
    assert agent_auth_check._credential_file_state("muse")["key_present"] is False
    assert adapters.auth_health("muse")["authenticated"] is False
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    assert capacity._classify("muse", capacity.AGENTS["muse"], None)[0] == capacity.SHED
    assert path.exists()


def test_muse_subscription_receipt_survives_actual_ledger_reconciliation(tmp_path, monkeypatch):
    import ledger_reconcile

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    feedback.record_run("muse-reconcile", "owner/repo#1", "review", "muse", mode="offload")
    feedback.record_muse_subscription_cost(
        "muse-reconcile",
        invocation_count=1,
        window_start_ts=18000,
        window_end_ts=36000,
        observed_start_ts=36000,
        observed_end_ts=36012,
        wall_seconds=12.5,
        raw_exit_codes=[0],
    )
    ledger = tmp_path / "capacity.ndjson"
    ledger.write_text(
        "\n".join(
            json.dumps(
                {"run_id": "muse-reconcile", "agent": "muse", "event": event, "ts": ts, "exit": 0}
            )
            for event, ts in [("start", 36000), ("complete", 36012)]
        )
    )
    ledger_reconcile.reconcile(ledger)
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT c.tokens_in,c.tokens_out,c.cost_usd,c.source,u.muse_invocation_count,u.muse_raw_exit_codes FROM costs c JOIN muse_subscription_usage u USING(run_id) WHERE run_id='muse-reconcile'"
        ).fetchone()
    assert row == (None, None, 0.0, "imputed:subscription", 1, "[0]")
    feedback.record_cost(
        "muse-reconcile", tokens_in=20, tokens_out=5, cost_usd=1.25, source="ccusage"
    )
    ledger_reconcile.reconcile(ledger)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT tokens_in,tokens_out,cost_usd,source FROM costs WHERE run_id='muse-reconcile'"
        ).fetchone() == (20, 5, 1.25, "ccusage")


def test_muse_is_not_implicitly_offered_to_the_adversarial_reviewer():
    import adversarial

    offered = []

    def select(task, cap, *, only, learned):
        offered.append(only)
        return {"agent": "muse"} if "muse" in only else None

    result = adversarial.choose_reviewer(cap={}, learned={}, select_fn=select)
    assert all("muse" not in pool for pool in offered)
    assert result.get("agent") != "muse"


def test_muse_stale_auth_alias_repairs_are_serialized(tmp_path, monkeypatch):
    import fcntl
    import threading

    source = tmp_path / "login.json"
    source.write_text("unchanged OAuth record")
    alias = tmp_path / "runtime" / "auth.json"
    alias.parent.mkdir()
    alias.symlink_to(tmp_path / "stale-login.json")
    first_inside = threading.Event()
    second_observed = threading.Event()
    second_unlinked = threading.Event()
    release_first = threading.Event()
    errors = []
    unlink = Path.unlink
    flock = fcntl.flock

    def observed_unlink(path, *args, **kwargs):
        if path == alias:
            if threading.current_thread().name == "first-launcher":
                first_inside.set()
                assert release_first.wait(5)
            else:
                second_unlinked.set()
                second_observed.set()
        return unlink(path, *args, **kwargs)

    def observed_flock(fd, operation):
        if threading.current_thread().name == "second-launcher":
            second_observed.set()
        return flock(fd, operation)

    def launch():
        try:
            adapters.ensure_muse_auth_link(source, alias)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, "unlink", observed_unlink)
    monkeypatch.setattr(fcntl, "flock", observed_flock)
    first = threading.Thread(target=launch, name="first-launcher")
    second = threading.Thread(target=launch, name="second-launcher")
    first.start()
    try:
        assert first_inside.wait(5)
        second.start()
        assert second_observed.wait(5)
        # The second caller has reached either the lock or unlink. It must be at the lock.
        assert not second_unlinked.is_set()
    finally:
        release_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert alias.resolve() == source.resolve()
    assert source.read_text() == "unchanged OAuth record"


def test_muse_private_home_exposes_only_existing_keychains(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters.sys, "platform", "darwin")
    home, runtime = tmp_path / "home", tmp_path / "runtime"
    keychains = home / "Library/Keychains"
    keychains.mkdir(parents=True)
    adapters.ensure_muse_keychain_link(home, runtime)
    adapters.ensure_muse_keychain_link(home, runtime)
    alias = runtime / "Library/Keychains"
    assert alias.is_symlink() and alias.resolve() == keychains.resolve()
    assert not (runtime / ".config/muse/settings.json").exists()
    alias.unlink()
    alias.mkdir()
    with pytest.raises(ValueError, match="non-symlink"):
        adapters.ensure_muse_keychain_link(home, runtime)
    assert alias.is_dir() and not alias.is_symlink()


def test_muse_missing_keychain_refuses_before_provider_or_prompt(tmp_path, monkeypatch):
    _runtime_login_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(adapters.sys, "platform", "darwin")
    (adapters.HOME / "Library/Keychains").rmdir()
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *a: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *a, **k: None)
    monkeypatch.setattr(adapters, "muse_prompt_file", lambda *a: pytest.fail("prompt allocated"))
    monkeypatch.setattr(adapters.subprocess, "run", lambda *a, **k: pytest.fail("provider called"))
    with pytest.raises(ValueError, match="existing macOS Keychains"):
        adapters.dispatch("muse", "review")
    with pytest.raises(ValueError, match="existing macOS Keychains"):
        dispatcher.offload("muse", "review", cwd=str(tmp_path))


def test_muse_keychain_alias_is_macos_only(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters.sys, "platform", "linux")
    runtime = tmp_path / "runtime"
    adapters.ensure_muse_keychain_link(tmp_path / "home", runtime)
    assert not runtime.exists()
