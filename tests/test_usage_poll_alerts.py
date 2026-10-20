"""Temp-HOME scheduled wrapper tests; no provider commands or live alert writes."""

import datetime as dt
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pacing

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/usage_poll"


def alert_module():
    spec = importlib.util.spec_from_file_location("usage_alerts", SCRIPTS / "usage_alerts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wrapper(tmp_path):
    home = tmp_path / "home"
    installed = home / ".codex/bin"
    installed.mkdir(parents=True)
    for name in ("usage-poll.sh", "py-standard.sh", "usage_alerts.py"):
        shutil.copyfile(SCRIPTS / name, installed / name)
    # These tests exercise real alert Python on every CI runtime, including 3.12.
    # Standard-interpreter version selection is covered independently by fake
    # interpreters in test_usage_poll_shell.py, so do not depend on host installs.
    standard = installed / "standard-python"
    standard.write_text(
        '#!/bin/bash\nif [[ "$1" == -c ]]; then printf "3.14\\n"; exit 0; fi\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    standard.chmod(0o755)
    for name in ("codex", "claude"):
        (installed / f"{name}-usage-poll.py").write_text(
            "import os, sys\n"
            f'name = "{name}"\n'
            'mode = os.environ.get("FAKE_POLL_MODE", "failed")\n'
            'if mode == "failed":\n'
            '    print(f"{name} usage poll FAILED; latest unchanged", file=sys.stderr)\n'
            "    sys.exit(1)\n"
        )
    latest = home / ".codex/orchestrator/usage-poll"
    latest.mkdir(parents=True)
    log = home / ".codex/handoff/alerts.log"

    def run(mode="failed", override=None):
        env = dict(
            os.environ,
            HOME=str(home),
            ORCH_STANDARD_PYTHON=str(standard),
            ORCH_USAGE_POLL_PYTHON=str(standard),
            FAKE_POLL_MODE=mode,
        )
        env.pop("HANDOFF_ALERT_LOG", None)
        if override:
            env["HANDOFF_ALERT_LOG"] = str(override)
        result = subprocess.run(
            ["/bin/bash", str(installed / "usage-poll.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == (1 if mode == "failed" else 0), result.stderr
        return (override or log).read_text().splitlines()

    return latest, log, run


def test_shell_stale_allowance_matches_pacing():
    shell = (SCRIPTS / "usage-poll.sh").read_text()
    definitions = re.findall(r"^STALE_AFTER_SECONDS=(\d+)$", shell, re.M)
    assert definitions == [str(pacing.STALE_AFTER_SECONDS)]


def test_stale_deduplicates_then_recovers_once_from_own_ts(wrapper):
    latest, log, run = wrapper
    for name in ("codex", "claude"):
        (latest / f"{name}-usage.json").write_text(json.dumps(dict(ts=time.time() - 2000)))
    lines = run()
    assert len(lines) == 2
    for name in ("codex", "claude"):
        line = next(line for line in lines if f": {name} " in line)
        assert re.search(r"STALE age=20\d\ds allowance=1800s last_error=", line)
        assert f"{name} usage poll FAILED; latest unchanged" in line
    assert run() == lines
    for name in ("codex", "claude"):
        path = latest / f"{name}-usage.json"
        path.write_text(json.dumps(dict(ts=time.time())))
        os.utime(path, (1, 1))  # Fresh content with old mtime is still fresh.
    recovered = run("succeeded")
    assert len(recovered) == 4
    assert all(" RECOVERED age=" in line for line in recovered[2:])
    assert run("succeeded") == recovered


def test_missing_file_is_stale_unknown_and_log_can_be_overridden(wrapper, tmp_path):
    latest, log, run = wrapper
    override = tmp_path / "digest/alerts.log"
    lines = run(override=override)
    assert len(lines) == 2
    assert all("STALE age=unknown allowance=1800s" in line for line in lines)
    assert run(override=override) == lines
    assert not log.exists()


def test_success_without_fresh_file_does_not_claim_recovery(wrapper):
    latest, log, run = wrapper
    lines = run()
    assert run("succeeded") == lines


def test_fresh_file_without_success_does_not_claim_recovery(wrapper):
    latest, log, run = wrapper
    lines = run()
    for name in ("codex", "claude"):
        (latest / f"{name}-usage.json").write_text(json.dumps(dict(ts=time.time())))
    assert run() == lines
    assert len(run("succeeded")) == 4


def test_six_hour_repeat_is_per_poller_and_recurs_after_new_outage(tmp_path):
    alerts = alert_module()
    now = 1792300000
    home = tmp_path / "home"
    log = tmp_path / "alerts.log"
    results = [
        (name, False, f"{name} usage poll FAILED; latest unchanged") for name in ("codex", "claude")
    ]
    alerts.update_alerts(home, log, 1800, results, now=now)
    alerts.update_alerts(home, log, 1800, results, now=now + 21599)
    assert len(log.read_text().splitlines()) == 2
    alerts.update_alerts(home, log, 1800, results, now=now + 21600)
    assert len(log.read_text().splitlines()) == 4
    latest = home / ".codex/orchestrator/usage-poll"
    latest.mkdir(parents=True)
    (latest / "codex-usage.json").write_text(json.dumps(dict(ts=now + 21601)))
    alerts.update_alerts(home, log, 1800, [("codex", True, "")], now=now + 21601)
    assert "codex RECOVERED" in log.read_text().splitlines()[-1]
    (latest / "codex-usage.json").unlink()
    alerts.update_alerts(home, log, 1800, results, now=now + 21602)
    assert len(log.read_text().splitlines()) == 6
    assert "codex STALE" in log.read_text().splitlines()[-1]


@pytest.mark.parametrize("ts", [None, "bad", float("nan"), {}, float("inf"), 1792300000 + 86400])
def test_invalid_timestamp_is_unknown_not_a_false_recovery(tmp_path, ts):
    alerts = alert_module()
    home = tmp_path / "home"
    latest = home / ".codex/orchestrator/usage-poll"
    latest.mkdir(parents=True)
    (latest / "codex-usage.json").write_text(json.dumps(dict(ts=ts)))
    log = tmp_path / "alerts.log"
    alerts.update_alerts(home, log, 1800, [("codex", True, "")], now=1792300000)
    assert "STALE age=unknown" in log.read_text()


def test_alert_diagnostics_are_controlled_and_visibly_bounded():
    alerts = alert_module()
    message = "claude usage poll FAILED; latest unchanged: " + "; ".join(
        f"try{i}: invalid weekly usage" for i in (1, 2, 3)
    )
    assert len(message) > 120
    assert alerts.safe_message(message, "claude") == message[:120]
    assert alerts.safe_message("untrusted provider response", "claude") == (
        "poll failed (diagnostic redacted)"
    )


@pytest.mark.parametrize("suffix", ["Z", "+02:00"])
@pytest.mark.parametrize("age,stale", [(1800, False), (1801, True)])
def test_iso_timestamp_drives_stale_decision_at_exact_allowance(tmp_path, suffix, age, stale):
    alerts = alert_module()
    now = 1792300000
    observed = dt.datetime.fromtimestamp(now - age, dt.timezone.utc)
    ts = (
        observed.isoformat().replace("+00:00", "Z")
        if suffix == "Z"
        else observed.astimezone(dt.timezone(dt.timedelta(hours=2))).isoformat()
    )
    home = tmp_path / "home"
    latest = home / ".codex/orchestrator/usage-poll"
    latest.mkdir(parents=True)
    (latest / "codex-usage.json").write_text(json.dumps(dict(ts=ts)))
    log = tmp_path / "alerts.log"
    alerts.update_alerts(home, log, 1800, [("codex", True, "")], now=now)
    assert log.exists() is stale
    if stale:
        assert "STALE age=1801s allowance=1800s" in log.read_text()
