"""Exercise the documented queries with fixture files, never the live poll log."""

import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import pacing

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/capacity-pacing.md"
NOW = dt.datetime(2026, 10, 30, 1, 30, tzinfo=dt.UTC).timestamp()


def doc_query(filename, home):
    commands = [line for line in DOC.read_text().splitlines() if line.startswith("python3 -c ")]
    (command,) = [line for line in commands if filename in line]
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"] and len(argv) == 3
    result = subprocess.run(
        [sys.executable, "-c", f"import time; time.time=lambda: {NOW!r}; exec({argv[2]!r})"],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_sparse_sampling_comment_matches_documented_allowance():
    source = Path(pacing.__file__).read_text()
    doc = DOC.read_text()
    assert "Allowance for sparse sampling; observed p95 ≈ 26 min." in source
    assert "allowance for sparse sampling; observed p95 ≈ 26 min." in doc
    assert "one pacing sample per hour" not in source + doc


def test_claude_checkin_and_first_live_deviations_are_documented():
    doc = DOC.read_text()
    for required in (
        "2026-10-30 01:30Z",
        "`pacing-claude-login-7d`",
        "coordinator-registered local check-in",
        "`~/.codex/orchestrator/briefs/pacing-claude-login-probe.sh`",
        "it is not part of the repo",
        "first live version",
        "`ORCH_USAGE_POLL_PYTHON` override only if it reports Python 3.13/3.14",
        '"ignoring unsupported ORCH_USAGE_POLL_PYTHON"',
        "uses the standard interpreter",
        "`claude-usage-poll.py` removes inherited",
        "`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`",
        "override the subscription login",
    ):
        assert required in doc


def utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("present", [True, False])
def test_documented_seven_day_poll_success_query(tmp_path, present):
    path = tmp_path / ".codex/orchestrator/usage-poll/poll.log"
    if present:
        path.parent.mkdir(parents=True)
        path.write_text(
            f"{utc(NOW - 7 * 86400 - 1)} claude rc=0 old\n"
            f"{utc(NOW - 7 * 86400)} claude rc=0 boundary\n"
            f"{utc(NOW - 600)} claude rc=1 failed\n"
            f"{utc(NOW)} claude rc=0 latest\n"
            f"{utc(NOW + 1)} claude rc=0 future\n"
            f"{utc(NOW)} codex rc=0 other provider\n"
            "continuation: claude rc=0\n"
        )
    expected = (
        "claude polls=3 ok=2 success_rate=66.7%"
        if present
        else "claude polls=0 ok=0 success_rate=unmeasured"
    )
    assert doc_query("poll.log", tmp_path) == expected


@pytest.mark.parametrize("present", [True, False])
def test_documented_seven_day_fresh_tick_query(tmp_path, present):
    path = tmp_path / ".codex/orchestrator/usage-poll/claude-usage.ndjson"
    if present:
        path.parent.mkdir(parents=True)
        # Exactly 1800 s is fresh; 1801 s isn't. Future polls cannot cover a tick.
        # One boundary poll covers three initial ticks; one recent poll covers
        # the final four ticks. All missing ticks remain in the denominator.
        stamps = [NOW + 1, NOW - 1800, NOW - 7 * 86400, NOW - 7 * 86400 - 1801]
        path.write_text("\n".join(json.dumps({"ts": ts}) for ts in stamps) + "\n")
    expected = (
        "claude ticks=1008 fresh=7 age_le_1800_rate=0.7%"
        if present
        else "claude ticks=1008 fresh=0 age_le_1800_rate=0.0%"
    )
    assert doc_query("claude-usage.ndjson", tmp_path) == expected
