"""Retained adapter permission and interval-reporting contracts for PR #1141.

Automatic dispatcher probe/fallback tests remain at a459028c for issue #1133.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import adapters
import execution_profiles
import switch_review

REPO = Path(__file__).resolve().parents[1]


def _readonly_profile() -> dict:
    profile = dict(execution_profiles.get_profile("codex-6.1-sol-medium"))
    profile["profile_id"] = "codex-assess-readonly-test"
    profile["permission_mode"] = "read-only"
    return profile


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
    cmd = adapters.build_command(
        "codex", "brief", cwd=tmp_path, sandbox_mode="read-only"
    )
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


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
            "src/switch_review.py",
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


def test_sandbox_switch_review_json_cli_accepts_iso_window(tmp_path, capsys):
    assert switch_review.main([
        "--assess-sandbox-fallback", "--since", "2026-11-06T00:00:00Z",
        "--now", "1793923201", "--ledger", str(tmp_path / "missing.ndjson"), "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["window_start"] == 1793923200
    assert report["window_end"] == 1794528000
    assert report["window_complete"] is False
    assert report["status"] == "unmeasured"
    assert report["fallback_runs"] is None
