"""Codex assess sandbox probe/fallback and retained adapter/reporting contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import adapters
import dispatcher
import execution_profiles
import switch_review

PREAMBLE = dispatcher.ASSESS_FALLBACK_PREAMBLE
REPO = Path(__file__).resolve().parents[1]


def _readonly_profile() -> dict:
    profile = dict(execution_profiles.get_profile("codex-6.1-sol-medium"))
    profile["profile_id"] = "codex-assess-readonly-test"
    profile["permission_mode"] = "read-only"
    return profile


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.feedback, "record_execution_attempt", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *args, **kwargs: (False, "test")
    )
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(adapters, "profile_codex_binary", lambda: "codex")
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")


def _install_run(monkeypatch, probe, *, agent_exit: int = 0, agent_stdout: str = "answer\n"):
    calls: list[dict] = []

    def run(cmd, cwd=None, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        calls.append({"cmd": argv, "timeout": kwargs.get("timeout"), "cwd": cwd})
        if argv[:2] == ["bash", "-lc"]:
            return subprocess.CompletedProcess(cmd, agent_exit, agent_stdout, "")
        # Child network probe: `codex sandbox … bash -lc 'gh api rate_limit'`
        # (no longer a bare `rate_limit` argv token).
        if argv[:2] == ["codex", "sandbox"] and any(
            isinstance(a, str) and "rate_limit" in a for a in argv
        ):
            result = probe(argv, kwargs)
            if isinstance(result, BaseException):
                raise result
            return result
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    return calls


def _shell(calls: list[dict]) -> str:
    for call in calls:
        if call["cmd"][:2] == ["bash", "-lc"]:
            return call["cmd"][2]
    return ""


def _probe_call(calls: list[dict]) -> dict:
    return next(
        call
        for call in calls
        if call["cmd"][:2] == ["codex", "sandbox"]
        and any(isinstance(a, str) and "rate_limit" in a for a in call["cmd"])
    )


def _ledger(tmp_path: Path) -> list[dict]:
    path = tmp_path / "capacity-ledger.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _completed(cmd, code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, code, stdout, stderr)


def test_sandbox_build_command_emits_workspace_write_without_widening_json(tmp_path):
    brief = "Run gh api rate_limit"
    cmd = adapters.build_command(
        "codex", brief, mode="assess", cwd=tmp_path, sandbox_mode="workspace-write"
    )
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--json" not in cmd
    assert cmd[-1] == brief
    unchanged = adapters.build_command("codex", "brief", mode="assess", cwd=tmp_path)
    assert unchanged[unchanged.index("--sandbox") + 1] == "read-only"


def test_sandbox_restricted_profile_cannot_widen_in_build_command(tmp_path):
    with pytest.raises(ValueError, match="restricted profile"):
        adapters.build_command(
            "codex",
            "brief",
            mode="assess",
            cwd=tmp_path,
            profile=_readonly_profile(),
            sandbox_mode="workspace-write",
        )


@pytest.mark.parametrize("sandbox", ["workspace-write", "danger-full-access"])
def test_sandbox_explicit_readonly_permission_refuses_widening(tmp_path, sandbox):
    with pytest.raises(ValueError, match="cannot widen|unsupported sandbox"):
        adapters.build_command(
            "codex", "brief", cwd=tmp_path, permission_mode="read-only", sandbox_mode=sandbox
        )


def test_sandbox_explicit_selection_preserves_outer_seat_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "codex_bypass_inner_sandbox", lambda: True)
    cmd = adapters.build_command("codex", "brief", cwd=tmp_path, sandbox_mode="read-only")
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_sandbox_probe_command_is_inside_the_assess_sandbox(tmp_path, monkeypatch):
    seen = {}

    def run(cmd, cwd=None, **kwargs):
        seen["cmd"] = list(cmd)
        seen["timeout"] = kwargs.get("timeout")
        return _completed(cmd, 1, "", "dial tcp: lookup api.github.com: no such host")

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    assess_argv = ["codex", "exec", "--sandbox", "read-only", "--cd", str(tmp_path), "brief"]
    verdict = dispatcher._sandbox_has_network(assess_argv, cwd=str(tmp_path))
    expected = dispatcher._sandbox_probe_command(assess_argv)
    assert seen["cmd"] == expected
    assert seen["cmd"][:2] == ["codex", "sandbox"]
    assert seen["cmd"][2:4] == ["--permission-profile", ":read-only"]
    assert "--include-managed-config" in seen["cmd"]
    assert "-c" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--cd") + 1] == str(tmp_path)
    assert seen["cmd"][-4:] == ["--", "bash", "-lc", "gh api rate_limit"]
    assert not (
        len(seen["cmd"]) >= 3 and seen["cmd"][1] == "sandbox" and seen["cmd"][2] == "--sandbox"
    ), "regression: `codex sandbox` does not accept exec's `--sandbox` (clap rejects)"
    assert seen["timeout"] <= dispatcher.SANDBOX_NETWORK_PROBE_TIMEOUT_S == 5.0
    assert verdict["classification"] == "dns" and verdict["has_network"] is False


def test_sandbox_probe_timeout_is_bounded_by_five_seconds(tmp_path, monkeypatch):
    """A sleeping child must be stopped by the production subprocess deadline."""
    real_run = subprocess.run
    timeouts = []
    child = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(dispatcher, "_sandbox_probe_command", lambda argv: child)

    def run(cmd, cwd=None, **kwargs):
        timeouts.append(kwargs["timeout"])
        return real_run(cmd, cwd=cwd, **kwargs)

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    verdict = dispatcher._sandbox_has_network(
        ["codex", "exec", "--sandbox", "read-only", "--cd", str(tmp_path), "brief"],
        cwd=str(tmp_path),
    )
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 5.0
    assert verdict["classification"] == "timeout" and verdict["has_network"] is False
    assert 5.0 <= verdict["elapsed_s"] < 10.0, verdict


def test_sandbox_auth_and_rate_json_are_not_network_failures(tmp_path, monkeypatch):
    answers = iter(
        [
            _completed(
                ["gh"], 1, "", "HTTP 401: Bad credentials (https://api.github.com/rate_limit)"
            ),
            _completed(
                ["gh"],
                0,
                json.dumps({"resources": {"core": {"limit": 5000}}, "rate": {"remaining": 401}}),
                "",
            ),
            _completed(["gh"], 1, "", "connect: connection refused"),
        ]
    )

    def run(cmd, cwd=None, **kwargs):
        return next(answers)

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    argv = ["codex", "exec", "--sandbox", "read-only", "--cd", str(tmp_path), "brief"]
    auth = dispatcher._sandbox_has_network(argv, cwd=str(tmp_path))
    rate = dispatcher._sandbox_has_network(argv, cwd=str(tmp_path))
    connection = dispatcher._sandbox_has_network(argv, cwd=str(tmp_path))
    assert auth["classification"] == "auth_or_api" and auth["has_network"] is True
    assert rate["classification"] == "available" and rate["has_network"] is True
    assert connection["classification"] == "connection" and connection["has_network"] is False


def test_sandbox_network_failure_falls_back_to_workspace_write(tmp_path, monkeypatch, capsys):
    """Deliberate-break target: availability skips workspace-write and the preamble."""
    _isolate(tmp_path, monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda cmd, kwargs: _completed(cmd, 1, "", "dial tcp: lookup api.github.com: no such host"),
    )
    result = dispatcher.offload(
        "codex",
        "Run gh api rate_limit and print the JSON.",
        cwd=str(tmp_path),
        mode="assess",
    )
    shell = _shell(calls)
    assert "workspace-write" in shell and PREAMBLE in shell, (
        "workspace-write and the fallback preamble are absent\n" + shell
    )
    assert "read-only" not in shell
    probe = _probe_call(calls)
    assert probe["cmd"][:2] == ["codex", "sandbox"]
    assert probe["cmd"][2:4] == ["--permission-profile", ":read-only"]
    assert probe["cmd"][-4:] == ["--", "bash", "-lc", "gh api rate_limit"]
    assert probe["timeout"] <= 5.0
    assert result["sandbox"] == "workspace-write" and result["sandbox_fallback"] is True
    assert result["sandbox_network"]["classification"] == "dns"
    rows = _ledger(tmp_path)
    assert rows and all(row.get("sandbox_fallback") is True for row in rows)
    assert {row["event"] for row in rows} >= {"start", "complete"}
    err = capsys.readouterr().err
    assert dispatcher.ASSESS_FALLBACK_DIAGNOSTIC in err


def test_sandbox_connection_and_timeout_also_fall_back(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    connection_calls = _install_run(
        monkeypatch, lambda cmd, kwargs: _completed(cmd, 1, "", "connect: connection refused")
    )
    connection = dispatcher.offload("codex", "probe", cwd=str(tmp_path), mode="assess")
    assert connection["sandbox_network"]["classification"] == "connection"
    assert "workspace-write" in _shell(connection_calls)

    def boom(cmd, kwargs):
        assert kwargs["timeout"] <= 5.0
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    timeout_calls = _install_run(monkeypatch, boom)
    timed = dispatcher.offload("codex", "probe", cwd=str(tmp_path), mode="assess")
    assert timed["sandbox_network"]["classification"] == "timeout"
    assert timed["sandbox"] == "workspace-write"
    assert "workspace-write" in _shell(timeout_calls)
    assert _probe_call(timeout_calls)["timeout"] <= 5.0


def test_sandbox_auth_rejection_keeps_read_only(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda cmd, kwargs: _completed(cmd, 1, "", "HTTP 403: Requires authentication"),
    )
    result = dispatcher.offload("codex", "Run gh api rate_limit", cwd=str(tmp_path), mode="assess")
    shell = _shell(calls)
    assert "read-only" in shell and "workspace-write" not in shell
    assert PREAMBLE not in shell
    assert result["sandbox"] == "read-only" and result["sandbox_fallback"] is False
    assert result["sandbox_network"]["classification"] == "auth_or_api"
    assert all("sandbox_fallback" not in row for row in _ledger(tmp_path))
    assert dispatcher.ASSESS_FALLBACK_DIAGNOSTIC not in capsys.readouterr().err


def test_sandbox_network_success_leaves_the_command_unchanged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    body = json.dumps({"rate": {"limit": 5000, "remaining": 4733}})
    calls = _install_run(monkeypatch, lambda cmd, kwargs: _completed(cmd, 0, body, ""))
    result = dispatcher.offload("codex", "Run gh api rate_limit", cwd=str(tmp_path), mode="assess")
    shell = _shell(calls)
    assert "read-only" in shell and "workspace-write" not in shell and PREAMBLE not in shell
    assert result["sandbox_network"]["classification"] == "available"
    assert all("sandbox_fallback" not in row for row in _ledger(tmp_path))


def test_sandbox_failed_fallback_still_persists_the_ledger_flag(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _install_run(
        monkeypatch,
        lambda cmd, kwargs: _completed(cmd, 1, "", "curl: (6) Could not resolve host"),
        agent_exit=1,
        agent_stdout="OFFLOAD_INCOMPLETE: could not resolve host\n",
    )
    result = dispatcher.offload("codex", "Run gh api rate_limit", cwd=str(tmp_path), mode="assess")
    assert result["exit"] != 0 and result["sandbox_fallback"] is True
    rows = _ledger(tmp_path)
    assert [row["event"] for row in rows] == ["start", "complete"]
    assert all(row["sandbox_fallback"] is True for row in rows)


def test_sandbox_restricted_profile_refuses_without_fallback(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    profile = _readonly_profile()
    real_get = execution_profiles.get_profile

    def fake_get(item):
        if item == profile["profile_id"] or (
            isinstance(item, dict) and item.get("profile_id") == profile["profile_id"]
        ):
            return execution_profiles.validate_profile(profile)
        return real_get(item)

    monkeypatch.setattr(execution_profiles, "get_profile", fake_get)
    calls = _install_run(
        monkeypatch,
        lambda cmd, kwargs: _completed(cmd, 1, "", "no such host"),
    )
    result = dispatcher.offload(
        "codex",
        "Run gh api rate_limit",
        cwd=str(tmp_path),
        mode="assess",
        profile_id=profile["profile_id"],
    )
    assert result["exit"] == 2 and result["process_started"] is False
    assert "restricted profile" in result["error"]
    assert _shell(calls) == ""
    assert _ledger(tmp_path) == []
    err = capsys.readouterr().err
    assert "restricted profile" in err
    assert dispatcher.ASSESS_FALLBACK_DIAGNOSTIC not in err


def test_sandbox_probe_does_not_force_network_access(tmp_path, monkeypatch):
    """Regression: the probe must not independently enable network access.

    A networkless assess policy must not be overridden by the probe adding
    network={access="allow"} permissions. The probe uses the same resolved
    policy as the assess child.
    """
    _isolate(tmp_path, monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda cmd, kwargs: _completed(cmd, 1, "", "dial tcp: lookup api.github.com: no such host"),
    )
    result = dispatcher.offload(
        "codex",
        "Run gh api rate_limit",
        cwd=str(tmp_path),
        mode="assess",
    )
    probe = _probe_call(calls)
    assert probe["cmd"][2:4] == ["--permission-profile", ":read-only"]
    assert "--include-managed-config" in probe["cmd"]
    assert "-c" not in probe["cmd"], "probe must not override network or filesystem policy"
    assert result["sandbox_fallback"] is True
    assert result["sandbox_network"]["classification"] == "dns"


@pytest.mark.parametrize("setup_elapsed", [1.25, 5.0])
def test_sandbox_probe_timeout_accounts_for_elapsed_time(tmp_path, monkeypatch, setup_elapsed):
    """Setup consumes the same five-second allowance as the subprocess."""
    from types import SimpleNamespace

    clock = iter([100.0, 100.0 + setup_elapsed, 100.0 + setup_elapsed])
    monkeypatch.setattr(dispatcher, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    calls = []

    def run(cmd, **kwargs):
        calls.append(kwargs["timeout"])
        assert kwargs["timeout"] == 5.0 - setup_elapsed
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    verdict = dispatcher._sandbox_has_network(["codex", "exec"], cwd=str(tmp_path))
    assert calls == ([3.75] if setup_elapsed < 5.0 else [])
    assert verdict["classification"] == "timeout" and verdict["has_network"] is False
    assert verdict["elapsed_s"] == setup_elapsed


def test_sandbox_probe_late_return_is_not_success(tmp_path, monkeypatch):
    from types import SimpleNamespace

    clock = iter([100.0, 100.0, 105.1, 105.1])
    monkeypatch.setattr(dispatcher, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(
        dispatcher.subprocess, "run", lambda *a, **k: _completed(a[0], 0, '{"rate": {}}')
    )
    verdict = dispatcher._sandbox_has_network(["codex", "exec"], cwd=str(tmp_path))
    assert verdict["classification"] == "timeout" and verdict["has_network"] is False


@pytest.mark.parametrize(
    "error,classification",
    [
        ("HTTP 401: Bad credentials", "auth_or_api"),
        ("HTTP 403: API rate limit exceeded", "auth_or_api"),
        ("HTTP 500: Internal Server Error", "auth_or_api"),
        ("gh auth login", "auth_or_api"),
        ("unrecognized option --permission-profile", "unclassified"),
        ("sandbox-exec: sandbox_apply: Operation not permitted", "unclassified"),
    ],
)
def test_sandbox_non_network_rejections_never_widen(
    tmp_path, monkeypatch, capsys, error, classification
):
    _isolate(tmp_path, monkeypatch)
    calls = _install_run(monkeypatch, lambda cmd, kwargs: _completed(cmd, 1, "", error))
    result = dispatcher.offload("codex", "Run gh api rate_limit", cwd=str(tmp_path), mode="assess")
    assert result["sandbox_network"]["classification"] == classification
    assert result["sandbox"] == "read-only" and result["sandbox_fallback"] is False
    assert PREAMBLE not in _shell(calls)
    assert "workspace-write" not in _shell(calls)
    assert dispatcher.ASSESS_FALLBACK_DIAGNOSTIC not in capsys.readouterr().err


def test_sandbox_unstarted_probe_does_not_authorize_fallback(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(dispatcher.subprocess, "run", fail)
    verdict = dispatcher._sandbox_has_network(["codex", "exec"], cwd=str(tmp_path))
    assert verdict["classification"] == "probe_unstarted"
    assert dispatcher._sandbox_network_available(verdict)
    assert dispatcher._sandbox_network_available({}), "unknown is not a network failure"


def test_sandbox_fallback_preserves_write_roots_and_profile(tmp_path, monkeypatch):
    import shlex

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(adapters, "codex_bypass_inner_sandbox", lambda: False)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    calls = _install_run(monkeypatch, lambda cmd, kwargs: _completed(cmd, 1, "", "no such host"))
    result = dispatcher.offload(
        "codex",
        "Run gh api rate_limit",
        cwd=str(tmp_path),
        mode="assess",
        profile_id="codex-6.1-sol-medium",
        write_boundary={"protect": [str(tmp_path)], "allow": [str(allowed)]},
    )
    argv = shlex.split(_shell(calls))
    assert result["sandbox_fallback"] is True
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--add-dir") + 1] == str(allowed)
    assert argv.count("--add-dir") == 1
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert PREAMBLE in argv[-1] and "WRITE BOUNDARY:" in argv[-1]
    assert "sandbox=workspace-write sandbox_fallback=true" in Path(result["log"]).read_text()


def test_sandbox_fallback_rechecks_protected_write_roots(tmp_path, monkeypatch, capsys):
    """A root restricted during the probe must not become a fallback write grant."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(adapters, "codex_bypass_inner_sandbox", lambda: False)
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    workspace.mkdir()
    allowed.mkdir()

    def probe(cmd, kwargs):
        # The original read-only command grants no extra roots. Before building
        # workspace-write, a newly protected descendant makes this grant unsafe.
        monkeypatch.setattr(
            adapters,
            "protected_workspace_anchors",
            lambda: [(allowed / "control-plane", "the test control plane")],
        )
        return _completed(cmd, 1, "", "no such host")

    calls = _install_run(monkeypatch, probe)
    result = dispatcher.offload(
        "codex",
        "Run gh api rate_limit",
        cwd=str(workspace),
        mode="assess",
        write_boundary={"protect": [str(tmp_path)], "allow": [str(allowed)]},
    )
    assert _probe_call(calls)
    assert result["exit"] == 2 and result["process_started"] is False
    assert result["sandbox"] == "read-only" and result["sandbox_fallback"] is False
    assert result["run_id"] is None
    assert "contains the test control plane" in result["error"]
    assert _shell(calls) == ""
    assert _ledger(tmp_path) == []
    assert list((tmp_path / "logs").iterdir()) == []
    diagnostic = capsys.readouterr().err
    assert "workspace-write fallback refused" in diagnostic
    assert dispatcher.ASSESS_FALLBACK_DIAGNOSTIC not in diagnostic


@pytest.mark.parametrize("agent,mode", [("codex", "mid"), ("cursor", "assess")])
def test_sandbox_probe_only_runs_for_codex_assess(tmp_path, monkeypatch, agent, mode):
    _isolate(tmp_path, monkeypatch)

    def unexpected(*args, **kwargs):
        pytest.fail("probe must only run for Codex assess")

    monkeypatch.setattr(dispatcher, "_sandbox_has_network", unexpected)
    stdout = (
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}})
        if agent == "codex"
        else "answer\n"
    )
    _install_run(monkeypatch, unexpected, agent_stdout=stdout)
    result = dispatcher.offload(agent, "read the brief", cwd=str(tmp_path), mode=mode)
    assert result["exit"] == 0
    assert all("sandbox_fallback" not in row for row in _ledger(tmp_path))


def _row(ts: int, **fields) -> str:
    return json.dumps({"ts": ts, **fields})


def test_sandbox_switch_review_names_seven_days_and_unverified_windows(tmp_path):
    now = 1_800_000_000
    since = now - 2 * 86400
    log = tmp_path / "assess.log"
    log.write_text("OFFLOAD_INCOMPLETE: curl: (6) Could not resolve host\n")
    ledger = tmp_path / "capacity-ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                _row(
                    since + 10,
                    event="start",
                    mode="assess",
                    run_id="offload:codex:1",
                    sandbox_fallback=True,
                ),
                _row(
                    since + 20,
                    event="complete",
                    mode="assess",
                    run_id="offload:codex:1",
                    sandbox_fallback=True,
                    exit=70,
                    error="agent reported OFFLOAD_INCOMPLETE",
                    log_file=str(log),
                ),
                _row(
                    since + 30,
                    event="complete",
                    mode="assess",
                    run_id="offload:codex:2",
                    exit=70,
                    error="agent reported OFFLOAD_INCOMPLETE",
                    log_file=str(tmp_path / "missing.log"),
                ),
                "{not json",
            ]
        )
        + "\n"
    )
    open_window = switch_review.assess_sandbox_fallback_report(
        ledger_path=ledger, now=now, since=since
    )
    assert open_window["window_days"] == switch_review.ASSESS_SANDBOX_WINDOW_DAYS == 7
    assert open_window["fallback_runs"] == 1
    assert open_window["network_caused_assess_incompletions"] == 1
    assert open_window["status"] == "unverified"
    assert "seven-day window has not elapsed" in open_window["reason"]
    assert "lack classifiable evidence" in open_window["reason"]
    text = switch_review.format_assess_sandbox_fallback(open_window)
    assert "fallback_runs 1" in text and "network-caused assess incompletions 1" in text
    assert "(7d)" in text and "unverified:" in text

    closed = switch_review.assess_sandbox_fallback_report(
        ledger_path=ledger, now=since + 8 * 86400, since=since
    )
    assert closed["window_complete"] is True
    assert closed["status"] == "unverified"  # the missing log is still unclassified

    clean = tmp_path / "clean.ndjson"
    clean.write_text(
        _row(
            since + 10,
            event="complete",
            mode="assess",
            run_id="offload:codex:3",
            exit=0,
            sandbox_fallback=True,
        )
        + "\n"
    )
    measured = switch_review.assess_sandbox_fallback_report(
        ledger_path=clean, now=since + 8 * 86400, since=since
    )
    assert measured["status"] == "measured"
    assert measured["fallback_runs"] == 1
    assert measured["network_caused_assess_incompletions"] == 0
    assert "unverified" not in switch_review.format_assess_sandbox_fallback(measured)

    missing = switch_review.assess_sandbox_fallback_report(
        ledger_path=tmp_path / "absent.ndjson", now=now, since=since
    )
    assert missing["status"] == "unmeasured"
    assert missing["fallback_runs"] is None
    assert missing["network_caused_assess_incompletions"] is None
    absent_line = switch_review.format_assess_sandbox_fallback(missing)
    assert "unmeasured" in absent_line and "fallback_runs 0" not in absent_line

    proc = subprocess.run(
        [
            sys.executable,
            str(Path(switch_review.__file__).resolve()),
            "--assess-sandbox-fallback",
            "--ledger",
            str(ledger),
            "--since",
            str(since),
            "--now",
            str(now),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "fallback_runs 1" in proc.stdout
    assert "network-caused assess incompletions 1" in proc.stdout
    assert "unverified:" in proc.stdout
    assert "seven-day window has not elapsed" in proc.stdout

    # Explicit bounds exclude neighbouring periods and duplicate start/complete rows.
    interval_ledger = tmp_path / "interval.ndjson"
    end = since + 3 * 86400
    interval_ledger.write_text(
        "\n".join(
            _row(
                stamp,
                event=event,
                mode="assess",
                run_id=run_id,
                sandbox_fallback=True,
                exit=70,
                error="OFFLOAD_INCOMPLETE: Could not resolve host",
            )
            for stamp, event, run_id in [
                (since - 1, "complete", "before"),
                (since, "start", "inside"),
                (since + 1, "complete", "inside"),
                (end - 1, "complete", "last"),
                (end, "complete", "at-end"),
                (end + 1, "complete", "after"),
            ]
        )
        + "\n"
    )
    interval = switch_review.assess_sandbox_fallback_report(
        ledger_path=interval_ledger, since=since, until=end, now=end + 86400
    )
    assert interval["window_start"] == since and interval["window_end"] == end
    assert interval["window_days"] == 3 and interval["window_complete"] is True
    assert interval["fallback_runs"] == 2
    assert interval["network_caused_assess_incompletions"] == 2
    assert interval["status"] == "measured"
    interval_text = switch_review.format_assess_sandbox_fallback(interval)
    assert "(3d) [" in interval_text and "Z)" in interval_text

    partial = switch_review.assess_sandbox_fallback_report(
        ledger_path=interval_ledger, since=since, until=end, now=since
    )
    assert partial["fallback_runs"] == 1
    assert partial["network_caused_assess_incompletions"] == 0
    assert partial["status"] == "unverified"
    assert partial["reason"] == "observation window has not elapsed"


def test_sandbox_switch_review_json_cli_accepts_iso_window(tmp_path, capsys):
    assert (
        switch_review.main(
            [
                "--assess-sandbox-fallback",
                "--since",
                "2026-11-06T00:00:00Z",
                "--now",
                "1793923201",
                "--ledger",
                str(tmp_path / "missing.ndjson"),
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["window_start"] == 1793923200
    assert report["window_end"] == 1794528000
    assert report["window_complete"] is False
    assert report["status"] == "unmeasured"
    assert report["fallback_runs"] is None

    ledger = tmp_path / "explicit.ndjson"
    ledger.write_text("")
    assert (
        switch_review.main(
            [
                "--assess-sandbox-fallback",
                "--since",
                "2026-11-06T02:00:00+02:00",
                "--until",
                "2026-11-13T00:00:00Z",
                "--now",
                "1794528000",
                "--ledger",
                str(ledger),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "[2026-11-06T00:00:00Z, 2026-11-13T00:00:00Z)" in text
    assert "fallback_runs 0; network-caused assess incompletions 0" in text
    assert "unverified" not in text

    # Unix epoch zero is an explicit start rather than an omitted argument.
    switch_review.main(
        [
            "--assess-sandbox-fallback",
            "--since",
            "0",
            "--until",
            "86400",
            "--now",
            "86400",
            "--ledger",
            str(ledger),
            "--json",
        ]
    )
    assert json.loads(capsys.readouterr().out)["window_start"] == 0

    for arguments, message in [
        (["--since", "invalid"], "invalid _parse_window_start value"),
        (["--until", "2026-11-13"], "until requires since"),
        (["--since", "10", "--until", "10"], "end must be after window start"),
        (["--since", "10", "--until", "9"], "end must be after window start"),
    ]:
        with pytest.raises(SystemExit) as exc:
            switch_review.main(["--assess-sandbox-fallback", *arguments])
        assert exc.value.code == 2
        error = capsys.readouterr().err
        assert message in error and "Traceback" not in error
    with pytest.raises(SystemExit) as exc:
        switch_review.main(["--since", "0", "--until", "86400"])
    assert exc.value.code == 2
    assert "require --assess-sandbox-fallback" in capsys.readouterr().err
