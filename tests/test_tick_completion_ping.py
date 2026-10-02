"""A COMPLETED --active tick pings the external dead-man's switch, and nothing else does.

The tick hung from 2026-09-26T04:40Z to 2026-10-02T01:35Z, 5d20h inside one here-doc, and printed
nothing in all that time. No step failed, so there was no `.fail-*` stamp and no ALERT; launchd
never starts a second instance of a running job, so every hourly firing behind it was skipped; and
every staleness monitor the tick has (capability-firing-monitor, switch-review, tick-evidence) runs
INSIDE the tick. A detector that can only run while the gate is open cannot report the gate shut.

So the alarm lives outside the machine: the last block of orchestrate.sh runs
`~/.codex/bin/hc-ping.sh orchestrator`, and healthchecks.io alerts when those pings stop. Three
properties make the ping a signal rather than noise, and each one is executed here, not read:

1. It fires only after every step. A ping from the top of the tick would keep arriving from a tick
   that hangs halfway, which is the very failure it exists to catch.
2. It fires only in --active mode, so a hand-run shadow tick cannot mask a dead launchd job.
3. It can never fail the tick. A missing script, a failing script or no network reads as silence,
   and silence is what the external check turns into an alert.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import paths

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"
ANCHOR = "# ORCH-ANCHOR: " + "completion-ping"
# Call sites of anything that does a step's work. None of them may follow the ping.
STEP_CALL = re.compile(r'_cadence_due |python3 "\$ORCH/|_gh_gate |\bgh (search|api|pr) ')


def _text() -> str:
    return ORCHESTRATE.read_text(encoding="utf-8")


def _block() -> str:
    text = _text()
    assert text.count(ANCHOR) == 1, f"expected exactly one {ANCHOR!r} in orchestrate.sh"
    return text[text.index(ANCHOR) :]


def _run(
    tmp_path: Path,
    *,
    mode: str,
    stub_rc: int = 0,
    stub: str = "override",
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the real completion-ping block under bash, the way the tick reaches it.

    `stub` chooses where hc-ping.sh lives: "override" (ORCH_HC_PING names it), "default"
    ($HOME/.codex/bin/hc-ping.sh, with HOME pointed at tmp_path) or "missing" (nothing there).
    """
    calls_log = tmp_path / "calls.log"
    home = tmp_path / "home"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    target = home / ".codex" / "bin" / "hc-ping.sh"
    if stub == "override":
        target = tmp_path / "stub-hc-ping.sh"
        env["ORCH_HC_PING"] = str(target)
    if stub != "missing":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls_log}"\nexit {stub_rc}\n', encoding="utf-8"
        )
        target.chmod(0o755)
    script = f"set -euo pipefail\nmode={mode}\n{_block()}\necho TICK-END\n"
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [bash, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    calls = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
    return proc, calls


def test_the_ping_is_wired_exactly_once() -> None:
    # WIRING PIN (smallest fragment, count == 1): the call that names the orchestrator check.
    needle = 'hc-ping.sh}" ' + "orchestrator"
    assert _text().count(needle) == 1, f"expected exactly one {needle!r} in orchestrate.sh"


def test_no_step_runs_after_the_ping() -> None:
    lines = _text().splitlines()
    anchor = next(i for i, line in enumerate(lines) if line.startswith(ANCHOR))
    late = [
        f"line {i + 1}: {line.strip()}"
        for i, line in enumerate(lines)
        if i > anchor and STEP_CALL.search(line.split("#", 1)[0])
    ]
    assert not late, (
        "a step after the completion ping would let a tick that hangs in it keep pinging:\n"
        + "\n".join(late)
    )
    earlier = [line for line in lines[:anchor] if STEP_CALL.search(line.split("#", 1)[0])]
    assert earlier, "no step call sites found above the ping — the regex or the layout changed"


def test_an_active_tick_pings_the_orchestrator_check(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, mode="active")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["orchestrator"]
    assert proc.stdout.rstrip().endswith("TICK-END")


def test_the_default_script_path_is_under_home(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, mode="active", stub="default")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["orchestrator"], "unset ORCH_HC_PING must fall back to ~/.codex/bin/hc-ping.sh"


def test_a_shadow_tick_never_pings(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, mode="shadow")
    assert proc.returncode == 0, proc.stderr
    assert calls == [], "a hand-run shadow tick would mask a dead launchd job"


def test_a_failing_ping_never_fails_the_tick(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, mode="active", stub_rc=7)
    assert calls == ["orchestrator"]
    assert proc.returncode == 0, "set -e must not turn a failed ping into a failed tick"
    assert proc.stdout.rstrip().endswith("TICK-END")


def test_a_missing_script_is_silent_and_harmless(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, mode="active", stub="missing")
    assert calls == []
    assert proc.returncode == 0
    assert proc.stderr == "", "a missing hc-ping.sh must not add noise to the tick log"
