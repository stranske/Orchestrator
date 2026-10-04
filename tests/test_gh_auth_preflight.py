"""The --active preflight tells "GitHub cannot answer now" from "not authenticated".

The check that sat at the top of orchestrate.sh was `gh auth status`, and gh 2.94.0 exits 1 for a
403 rate limit, a secondary limit, a 502, an unreachable host, a 401 and a missing token alike. Each
prints "The token in GH_TOKEN is invalid." except the last. (Measured 2026-10-02 by pointing gh at a
local stub server.) So on 2026-10-02T15:40Z, with the shared per-user REST budget spent, the tick
aborted on a present, valid token and ran nothing below the preflight: no heartbeats, no cadence, no
monitors. Its log also named the wrong cause.

`gh_capacity.py --auth-preflight` asks with real calls and exits 0 (authenticated), 75 (token
present, GitHub cannot answer: GitHub-dependent steps defer, local steps run) or 77 (no token, 401,
or a 403 that is not a rate limit: ABORT). Every test here runs the REAL shell from orchestrate.sh
against a stub `gh` that answers the way gh answered the stub server, so a regression is caught as
behaviour rather than as text.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import paths

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"
ANCHOR = "# ORCH-ANCHOR: " + "gh-auth-preflight"
HEARTBEAT_ANCHOR = "# ORCH-ANCHOR: " + "heartbeat-export"
MIRROR_READER_ANCHOR = "# ORCH-ANCHOR: " + "mirror-reader-reentry"
DEFERS = "GitHub-dependent steps defer, local steps run"
RESET = 4102444800  # 2100-01-01T00:00:00Z: a reset far enough out that no test run reaches it
RATE_LIMITED_LINE = (
    f"gh rate-limited until 2100-01-01T00:00:00Z: {DEFERS} "
    "(core 0/5000; HTTP 403: API rate limit exceeded for user ID 1.)"
)
BASH = shutil.which("bash") or "/bin/bash"

# What gh 2.94.0 printed and exited with for each answer of the local stub server (2026-10-02).
# `auth status` is here so the bare check this replaced can be restored and seen to fail.
GH_STUB = r"""
import json, os, sys

calls, sc = os.environ["GH_STUB_CALLS"], os.environ["GH_STUB_SCENARIO"]
with open(calls, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
RESET = {reset}


def budget(remaining, resource="core"):
    return {{"X-Ratelimit-Limit": "5000", "X-Ratelimit-Remaining": str(remaining),
            "X-Ratelimit-Reset": str(RESET), "X-Ratelimit-Resource": resource}}


def answer(status, body, headers, rc, err=""):
    head = "".join(f"{{k}}: {{v}}\r\n" for k, v in headers.items())
    sys.stdout.write(f"HTTP/2.0 {{status}} X\r\n{{head}}\r\n{{json.dumps(body)}}")
    if err:
        sys.stderr.write(err + "\n")
    sys.exit(rc)


args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    if sc == "ok":
        sys.exit(0)
    if sc == "no-token":
        sys.stderr.write("You are not logged into any GitHub hosts. To log in, run: gh auth login\n")
        sys.exit(1)
    sys.stderr.write("  X Failed to log in to github.com account stub-user (GH_TOKEN)\n")
    sys.stderr.write("  - The token in GH_TOKEN is invalid.\n")
    sys.exit(1)
if args[:2] == ["api", "rate_limit"]:  # the exempt endpoint: a fresh bucket, whatever is true
    print(json.dumps({{"resources": {{"core": {{"limit": 5000, "remaining": 5000,
                                              "reset": RESET, "used": 0}}}}}}))
    sys.exit(0)
graphql = "graphql" in args
if sc == "no-token":
    sys.stderr.write("To get started with GitHub CLI, please run:  gh auth login\n")
    sys.exit(4)
if sc == "unreachable":
    sys.stderr.write('Get "https://api.github.com/user": dial tcp: lookup api.github.com: no such host\n')
    sys.exit(1)
if sc == "server":
    answer(502, {{"message": "Server Error"}}, {{}}, 1, "gh: Server Error (HTTP 502)")
if sc == "bad-creds":
    answer(401, {{"message": "Bad credentials"}}, {{}}, 1, "gh: Bad credentials (HTTP 401)")
if sc == "suspended":
    answer(403, {{"message": "Sorry. Your account was suspended."}}, budget(4000), 1)
if sc == "rate-limited":
    msg = ("API rate limit exceeded for user ID 1. If you reach out to GitHub Support for help, "
           "please include the request ID AC11:12F56A.")
    answer(403, {{"message": msg}}, budget(0), 1, f"gh: {{msg}} (HTTP 403)")
if sc == "secondary":
    msg = "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
    answer(403, {{"message": msg}}, {{"Retry-After": "60"}}, 1, f"gh: {{msg}} (HTTP 403)")
if sc == "graphql-limited" and graphql:
    body = {{"errors": [{{"type": "RATE_LIMITED", "message": "API rate limit exceeded for user ID 1."}}]}}
    answer(200, body, budget(0, "graphql"), 1, "gh: API rate limit exceeded for user ID 1.")
if graphql:
    answer(200, {{"data": {{"viewer": {{"login": "stub-user"}}}}}}, budget(4990, "graphql"), 0)
answer(200, {{"login": "stub-user"}}, budget(4321), 0)
"""


def _text() -> str:
    return ORCHESTRATE.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The script without its comments, so a pin cannot match the prose that explains it."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _preflight_block() -> str:
    """The pinned tick's real preflight, ending before heartbeat activation."""
    text = _text()
    assert text.count(ANCHOR) == 1, f"expected exactly one {ANCHOR!r} in orchestrate.sh"
    assert text.count(HEARTBEAT_ANCHOR) == 1, f"expected exactly one {HEARTBEAT_ANCHOR!r}"
    assert (
        text.count(MIRROR_READER_ANCHOR) == 1
    ), f"expected exactly one {MIRROR_READER_ANCHOR!r} in orchestrate.sh"
    start, end = text.index(ANCHOR), text.index(HEARTBEAT_ANCHOR)
    assert (
        text.index(MIRROR_READER_ANCHOR) < start < end
    ), "pin executable reads before the auth preflight, and authenticate before heartbeats"
    return text[start:end]


def _one_line_function(name: str) -> str:
    found = [line for line in _text().splitlines() if line.startswith(f"{name}() {{")]
    assert (
        len(found) == 1
    ), f"expected exactly one one-line definition of {name}() in orchestrate.sh"
    return found[0]


def _install_stubs(tmp_path: Path) -> Path:
    """A bin dir holding the stub `gh` and a `python3` that is this interpreter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + GH_STUB.format(reset=RESET), encoding="utf-8")
    gh.chmod(0o755)
    py = bin_dir / "python3"
    py.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return bin_dir


def _run_preflight(
    tmp_path: Path,
    scenario: str,
    *,
    mode: str = "active",
    block: str | None = None,
    orch: Path | None = None,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the real preflight block, then one `_gh_gate`, the way the tick reaches them."""
    bin_dir = _install_stubs(tmp_path)
    calls = tmp_path / "gh-calls.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "HANDOFF_DIR": str(tmp_path / "handoff"),
        "GH_STUB_SCENARIO": scenario,
        "GH_STUB_CALLS": str(calls),
    }
    script = "\n".join(
        [
            "set -euo pipefail",
            f"ORCH={shlex.quote(str(orch or paths.MODULE_DIR))}",
            f"mode={mode}",
            _one_line_function("_gh_deferred"),
            _one_line_function("_gh_gate"),
            block if block is not None else _preflight_block(),
            'echo "PREFLIGHT-PASSED"',
            "if _gh_gate core; then echo GATE-OPEN; else echo GATE-DEFERRED; fi",
        ]
    )
    proc = subprocess.run(
        [BASH, "-c", script], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    seen = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, seen


def test_the_preflight_sits_above_the_heartbeat_export() -> None:
    block = _preflight_block()  # asserts both anchors once, in that order
    # WIRING PIN (fragment, count == 1): the classifier call the preflight is built on.
    needle = 'gh_capacity.py" --auth-' + "preflight"
    assert _text().count(needle) == 1, f"expected exactly one {needle!r} in orchestrate.sh"
    assert needle in block, "the classifier call must sit inside the preflight block"


def test_mirror_reader_pins_the_active_preflight() -> None:
    """Authentication itself must read code from the selected tick generation."""
    text = _text()
    assert (
        text.count(MIRROR_READER_ANCHOR) == 1
    ), "expected exactly one mirror-reader reentry anchor"
    assert text.index(MIRROR_READER_ANCHOR) < text.index(
        ANCHOR
    ), "the tick must select its executable generation before loading the auth preflight"


def test_gh_auth_status_is_not_the_preflight() -> None:
    """It answers 1 for a rate limit, a 5xx and an unreachable host as for a refused token."""
    assert ("gh auth " + "status") not in _code(_text()), (
        "`gh auth status` is back as a check in orchestrate.sh; it cannot tell a rate-limited "
        "token from a missing one"
    )


def test_a_rate_limited_token_runs_the_tick_with_the_named_line(tmp_path: Path) -> None:
    proc, calls = _run_preflight(tmp_path, "rate-limited")
    assert "ABORT" not in proc.stderr and proc.returncode == 0, (
        "a rate-limited token aborted the tick: 'GitHub cannot answer now' was read as "
        f"'not authenticated'\n{proc.stderr}"
    )
    assert "PREFLIGHT-PASSED" in proc.stdout, proc.stdout
    assert f"  {RATE_LIMITED_LINE}\n" in proc.stdout, proc.stdout
    # The deferral reaches the existing budget gate, which skips without asking the exempt endpoint.
    assert proc.stdout.rstrip().endswith("GATE-DEFERRED"), proc.stdout
    assert "gh_capacity gate[core]: deferred by the gh preflight" in proc.stderr, proc.stderr
    assert not [c for c in calls if c.startswith("api rate_limit")], calls


@pytest.mark.parametrize(
    "scenario, expected",
    [
        ("secondary", "gh rate-limited until "),
        ("graphql-limited", "gh rate-limited until 2100-01-01T00:00:00Z: "),
        ("unreachable", f"gh unavailable: {DEFERS} (rest: Get "),
        ("server", f"gh unavailable: {DEFERS} (rest: HTTP 502: Server Error)"),
    ],
)
def test_github_unable_to_answer_defers_and_never_aborts(
    tmp_path: Path, scenario: str, expected: str
) -> None:
    proc, _ = _run_preflight(tmp_path, scenario)
    assert proc.returncode == 0 and "ABORT" not in proc.stderr, (scenario, proc.stderr)
    assert f"  {expected}" in proc.stdout, proc.stdout
    assert proc.stdout.rstrip().endswith("GATE-DEFERRED"), proc.stdout


@pytest.mark.parametrize("scenario", ["no-token", "bad-creds", "suspended"])
def test_a_missing_or_refused_credential_still_aborts(tmp_path: Path, scenario: str) -> None:
    proc, _ = _run_preflight(tmp_path, scenario)
    assert proc.returncode == 1, (scenario, proc.returncode, proc.stdout)
    assert "  ABORT: gh not authenticated (rest: " in proc.stderr, proc.stderr
    assert "refusing --active so we don't delegate on stale or blind state." in proc.stderr
    assert "PREFLIGHT-PASSED" not in proc.stdout, "--active must never run past a refused token"


def test_an_authenticated_token_runs_everything(tmp_path: Path) -> None:
    proc, calls = _run_preflight(tmp_path, "ok")
    assert proc.returncode == 0, proc.stderr
    expected = "gh: authenticated as stub-user (core 4321/5000, graphql 4990/5000)"
    assert f"  {expected}\n" in proc.stdout, proc.stdout
    assert proc.stdout.rstrip().endswith("GATE-OPEN"), proc.stdout
    assert [c.split()[:3] for c in calls] == [
        ["api", "--include", "user"],
        ["api", "--include", "graphql"],
        ["api", "--include", "user"],  # the budget gate reads core with a real call, not rate_limit
    ], calls


def test_a_classifier_that_fails_defers_and_says_so(tmp_path: Path) -> None:
    """A broken classifier measured nothing; that must not read as "not authenticated"."""
    broken = tmp_path / "broken-orch"
    broken.mkdir()
    (broken / "gh_capacity.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    proc, _ = _run_preflight(tmp_path, "ok", orch=broken)
    assert proc.returncode == 0 and "ABORT" not in proc.stderr, proc.stderr
    assert f"  gh preflight could not classify (exit 3): {DEFERS}\n" in proc.stdout, proc.stdout
    assert proc.stdout.rstrip().endswith("GATE-DEFERRED"), proc.stdout


def test_a_shadow_tick_runs_no_preflight(tmp_path: Path) -> None:
    proc, calls = _run_preflight(tmp_path, "rate-limited", mode="shadow")
    assert proc.returncode == 0, proc.stderr
    assert "gh rate-limited" not in proc.stdout, "shadow is attended and read-only; no preflight"
    assert "deferred by the gh preflight" not in proc.stderr, proc.stderr
    # Its one GitHub question is the budget gate's own real reading, which sees the spent budget
    # that the exempt rate_limit endpoint reported as a fresh 5000/5000.
    assert calls == ["api --include user"], calls
    assert proc.stdout.rstrip().endswith("GATE-DEFERRED"), proc.stdout
    assert "gh_capacity gate[core]: shed — 0/5000 remaining; refused (HTTP 403: " in proc.stderr


# --- The whole tick, every module stubbed -------------------------------------------------------
# The block tests prove the verdict. This proves what the tick does with it: run the REAL
# orchestrate.sh --active over a module directory where every `$ORCH/<name>.py` is a stub that logs
# its call, and read which steps ran. Every cadence step is due (empty state dir), so every step
# block is reached. `gh_capacity.py --gate` answers SHED so no budget-gated step can reach GitHub,
# and gh itself is pointed at a dead proxy with a fake token in case anything else tries.
MODULE_STUB = """import os, sys
from pathlib import Path
with open(os.environ["STUB_CALLS"], "a") as f:
    f.write(Path(__file__).name + " " + " ".join(sys.argv[1:]) + "\\n")
if "--json" in sys.argv or "validate" in sys.argv:
    print("{}")
"""
GH_CAPACITY_STUB = MODULE_STUB + """if "--auth-preflight" in sys.argv:
    print(os.environ["STUB_PREFLIGHT_LINE"])
    raise SystemExit(int(os.environ["STUB_PREFLIGHT_RC"]))
if "--gate" in sys.argv:
    raise SystemExit(75)
"""
PREFLIGHT = {
    "ok": (0, "gh: authenticated as stub-user (core 4321/5000, graphql 4990/5000)"),
    "deferred": (75, RATE_LIMITED_LINE),
    "refused": (77, "gh not authenticated (rest: HTTP 401: Bad credentials)"),
}
# Steps that reach GitHub and have no budget gate of their own: each must skip on a deferred tick.
DEFERRED_STEPS = (
    "tick.py --active",
    "redirect_apply.py",
    "capability_activation_audit.py",
    "fleet_shapes.py",
    "agent_switches.py",
)
# Local steps: each must still run on a deferred tick. That is the whole point of not aborting.
LOCAL_STEPS = (
    "tick_watchdog.py start",
    "capacity.py",
    "exp_abcd.py followup",
    "capabilities.py sweep",
    "capability_firing_monitor.py",
    "capability_propensity.py",
    "capability_outcome_bridge.py",
    "relearn_report.py",
)


def _run_tick(
    tmp_path: Path, verdict: str, *, lane_live: bool
) -> tuple[subprocess.CompletedProcess, list[str]]:
    orch = tmp_path / "orch"
    orch.mkdir()
    for name in sorted(set(re.findall(r"\$ORCH/([a-z_]+)\.py", _text()))):
        if name == "cadence_registry":
            shutil.copy(paths.MODULE_DIR / "cadence_registry.py", orch / "cadence_registry.py")
        else:
            stub = GH_CAPACITY_STUB if name == "gh_capacity" else MODULE_STUB
            (orch / f"{name}.py").write_text(stub, encoding="utf-8")
    home = tmp_path / "home"
    (home / ".codex" / "handoff").mkdir(
        parents=True
    )  # where a live lane writes its yield heartbeat
    calls = tmp_path / "calls.log"
    rc, line = PREFLIGHT[verdict]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "ORCH_DIR": str(orch),
        "ORCH_STATE_DIR": str(tmp_path / "state"),
        "ORCH_LOCAL_RUNTIME": str(tmp_path / "runtime"),
        "HANDOFF_DIR": str(tmp_path / "handoff"),
        "ORCH_HC_PING": "/bin/true",
        "STUB_CALLS": str(calls),
        "STUB_PREFLIGHT_RC": str(rc),
        "STUB_PREFLIGHT_LINE": line,
        # Belt and braces: nothing here may reach GitHub with a real credential.
        "GH_TOKEN": "sandbox-not-a-token",
        "GH_CONFIG_DIR": str(tmp_path / "gh-config"),
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
    }
    if lane_live:
        env.update(
            ORCH_DISPATCH_LANE="1",
            ORCH_RANGE_LANE_ROLLOUT="1",
            ORCH_RANGE_LANE_TRIAL_UNTIL="2999-12-31",
        )
    proc = subprocess.run(
        [BASH, str(ORCHESTRATE), "--active"],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    seen = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, seen


def _ran(calls: list[str], step: str) -> bool:
    return any(call.startswith(step) for call in calls)


@pytest.mark.parametrize("lane_live", [False, True])
def test_a_deferred_tick_runs_its_local_steps_and_none_that_reach_github(
    tmp_path: Path, lane_live: bool
) -> None:
    proc, calls = _run_tick(tmp_path, "deferred", lane_live=lane_live)
    assert proc.returncode == 0, f"a deferred tick must complete\n{proc.stdout}\n{proc.stderr}"
    assert f"  {RATE_LIMITED_LINE}\n" in proc.stdout, proc.stdout
    ran_github = [step for step in DEFERRED_STEPS if _ran(calls, step)]
    assert not ran_github, f"GitHub-dependent steps ran on a deferred tick: {ran_github}"
    missing_local = [step for step in LOCAL_STEPS if not _ran(calls, step)]
    assert not missing_local, f"local steps did not run on a deferred tick: {missing_local}"
    assert "  remote tick SKIPPED — gh deferred by the preflight" in proc.stdout, proc.stdout
    assert not [
        c for c in calls if c.startswith("gh_capacity.py --gate")
    ], "a deferred tick must not probe the budget: the preflight already measured it"
    if lane_live:
        assert not _ran(calls, "claims.py") and not _ran(calls, "backlog.py"), calls
        assert not _ran(calls, "range_lane_rollout.py"), "live range dispatch on a deferred tick"
        heartbeat = tmp_path / "home" / ".codex" / "handoff" / "orchestrator.json"
        assert not heartbeat.exists(), "the lanes must not yield to a tick that dispatches nothing"


@pytest.mark.parametrize("lane_live", [False, True])
def test_an_authenticated_tick_runs_the_github_steps(tmp_path: Path, lane_live: bool) -> None:
    proc, calls = _run_tick(tmp_path, "ok", lane_live=lane_live)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    for step in ("tick.py --active", "redirect_apply.py", "capability_activation_audit.py"):
        assert _ran(calls, step), f"{step} did not run on an authenticated tick"
    # The two GraphQL readers sit behind the budget gate, which this sandbox answers SHED.
    assert _ran(calls, "gh_capacity.py --gate graphql"), calls
    if lane_live:
        assert _ran(calls, "claims.py reap") and _ran(calls, "backlog.py --live"), calls
        assert _ran(
            calls, "range_lane_rollout.py --cached-backlog --json --max-dispatches 1 --apply"
        )
        heartbeat = tmp_path / "home" / ".codex" / "handoff" / "orchestrator.json"
        assert json.loads(heartbeat.read_text())["pid"], "a live lane writes the yield heartbeat"


def test_a_refused_token_still_aborts_the_whole_tick(tmp_path: Path) -> None:
    proc, calls = _run_tick(tmp_path, "refused", lane_live=False)
    assert proc.returncode == 1, proc.stdout
    assert "  ABORT: gh not authenticated (rest: HTTP 401: Bad credentials);" in proc.stderr
    # mirror_reader pins the tick generation before the auth preflight; it is not dispatch.
    after = [
        c
        for c in calls
        if not c.startswith(
            (
                "tick_watchdog.py",
                "cadence_registry.py",
                "gh_capacity.py",
                "mirror_reader.py",
            )
        )
    ]
    assert not after, f"steps ran after a refused token: {after}"
