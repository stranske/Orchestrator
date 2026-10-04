"""gh_capacity's budget gate sees a REAL exhaustion, and a blind probe row never hides one.

`gh api rate_limit` is exempt from the limit and answers from another count. For core and graphql it
reported a fresh 5000/5000 while every real call was refused (2026-09-23), core 5000/5000 used 0
against a real call's 606 used (2026-10-02), and core 5000/5000 against a real 4934 with graphql 4998
against a real 4613 (2026-10-04). Two things let that figure reach the gate:

* `state()` reasoned over the NEWEST ledger row whatever its source, so a probe row written after a
  true count superseded it; and
* `_gate()` probed before every verdict, so the newest row was always the blind one.

So `_gh_gate` in orchestrate.sh could never SHED on a real core or graphql exhaustion, and neither
could `throttle_if_enabled` in the gh-heavy steps. The tests below drive the real module, and the
real `_gh_gate` shell function, against a stub `gh`. None reaches GitHub.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import gh_capacity
import paths

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"
BASH = shutil.which("bash") or "/bin/bash"


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "gh-rate-ledger.ndjson"
    monkeypatch.setattr(gh_capacity, "HANDOFF", tmp_path)
    monkeypatch.setattr(gh_capacity, "GH_LEDGER", path)
    return path


def _real(resource: str, remaining: int, *, source: str, age_s: float, reset_in: int) -> dict:
    now = time.time()
    return {
        "ts": now - age_s,
        "resource": resource,
        "remaining": remaining,
        "limit": 5000,
        "reset": int(now) + reset_in,
        "used": 5000 - remaining,
        "source": source,
    }


def _probe_runner(seen: list) -> object:
    """`gh api rate_limit` the way it answered for real: a fresh bucket, whatever is true."""
    reset = int(time.time()) + 3600
    fresh = {"limit": 5000, "remaining": 5000, "reset": reset, "used": 0}
    search = {"limit": 30, "remaining": 30, "reset": int(time.time()) + 60, "used": 0}
    out = json.dumps({"resources": {"core": fresh, "graphql": fresh, "search": search}})

    def run(cmd: list, **_: object) -> SimpleNamespace:
        seen.append(cmd)
        assert cmd[:3] == ["gh", "api", "rate_limit"], f"a probe-only reading asked {cmd}"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return run


@pytest.mark.parametrize("resource", sorted(gh_capacity.REAL_MEASURE))
@pytest.mark.parametrize("source", sorted(gh_capacity.REAL_SOURCES))
def test_a_blind_probe_row_never_supersedes_a_true_exhausted_call_row(
    ledger: Path, source: str, resource: str
) -> None:
    gh_capacity._append_ledger([_real(resource, 0, source=source, age_s=5, reset_in=1800)])
    gh_capacity.probe(runner=_probe_runner([]))  # written AFTER the true row, so it is newer
    newest = gh_capacity._latest_ledger(resource)
    assert newest is not None and newest["source"] == "probe", "setup: the blind row is newest"
    assert newest["remaining"] == 5000, "setup: the blind row reads a fresh bucket"

    st, meta = gh_capacity.state(resource)
    assert st == gh_capacity.SHED, (
        f"a blind rate_limit probe row superseded a true exhausted {source} row for {resource}: "
        f"newest-row-wins is back ({meta})"
    )
    assert meta["source"] == source and meta["remaining"] == 0, meta
    assert gh_capacity.throttle(resource, sleeper=lambda s: None)["action"] == "defer"


def test_a_search_gate_probe_does_not_reblind_the_in_loop_core_throttle(ledger: Path) -> None:
    """durability-sweep, keepalive-shadow and keepalive-backfill are gated on SEARCH alone, and each
    throttles CORE inside its loop. The search gate's probe writes a blind core row; the core
    throttle must still reason over the tick's real reading."""
    gh_capacity._append_ledger(
        [_real("core", 150, source="preflight", age_s=60, reset_in=1800)]  # 3%: SHED
    )
    seen: list = []
    assert gh_capacity._gate("search", runner=_probe_runner(seen)) == 0
    assert len(seen) == 1, "the search gate reads the free probe once and spends no search call"

    decision = gh_capacity.throttle("core", sleeper=lambda s: None)
    assert (
        decision["action"] == "defer"
    ), f"the search gate's probe re-blinded the in-loop core throttle: {decision}"
    assert decision["source"] == "preflight", decision


# --- The real `_gh_gate`, the way the tick reaches it -------------------------------------------
# The preflight measures a healthy budget at tick start; the budget is spent before the next gated
# step; `rate_limit` keeps answering 5000/5000 throughout. The stub counts its `/user` calls: the
# first is the preflight's, every later one is a gate's reading.
GH_STUB = r"""
import json, os, sys

args = sys.argv[1:]
log = os.environ["GH_STUB_CALLS"]
with open(log, "a") as f:
    f.write(" ".join(args) + "\n")
with open(log) as f:
    users = sum(1 for line in f if line.split()[:3] == ["api", "--include", "user"])
RESET = int(os.environ["GH_STUB_RESET"])
SCENARIO = os.environ["GH_STUB_SCENARIO"]


def answer(status, body, headers, rc):
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    sys.stdout.write(f"HTTP/2.0 {status} X\r\n{head}\r\n{json.dumps(body)}")
    sys.exit(rc)


def budget(remaining, resource="core"):
    return {"X-Ratelimit-Limit": "5000", "X-Ratelimit-Remaining": str(remaining),
            "X-Ratelimit-Reset": str(RESET), "X-Ratelimit-Resource": resource}


if args[:2] == ["api", "rate_limit"]:
    fresh = {"limit": 5000, "remaining": 5000, "reset": RESET, "used": 0}
    search = {"limit": 30, "remaining": 30, "reset": RESET, "used": 0}
    print(json.dumps({"resources": {"core": fresh, "graphql": fresh, "search": search}}))
    sys.exit(0)
if "graphql" in args:
    answer(200, {"data": {"viewer": {"login": "stub-user"}}}, budget(4990, "graphql"), 0)
if users > 1 and SCENARIO == "spent-mid-tick":
    answer(403, {"message": "API rate limit exceeded for user ID 1."}, budget(0), 1)
if users > 1 and SCENARIO == "secondary-mid-tick":
    msg = "You have exceeded a secondary rate limit."
    answer(403, {"message": msg}, {"Retry-After": "60"}, 1)
answer(200, {"login": "stub-user"}, budget(4321), 0)
"""


def _one_line_function(name: str) -> str:
    text = ORCHESTRATE.read_text(encoding="utf-8")
    found = [line for line in text.splitlines() if line.startswith(f"{name}() {{")]
    assert (
        len(found) == 1
    ), f"expected exactly one one-line definition of {name}() in orchestrate.sh"
    return found[0]


def _run_tick_gate(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess, list]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    py = bin_dir / "python3"
    py.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    calls = tmp_path / "gh-calls.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "HANDOFF_DIR": str(tmp_path / "handoff"),
        "GH_STUB_CALLS": str(calls),
        "GH_STUB_SCENARIO": scenario,
        "GH_STUB_RESET": str(int(time.time()) + 1800),
    }
    script = "\n".join(
        [
            "set -euo pipefail",
            f"ORCH={shlex.quote(str(paths.MODULE_DIR))}",
            'gh_defer_reason=""',
            _one_line_function("_gh_deferred"),
            _one_line_function("_gh_gate"),
            'python3 "$ORCH/gh_capacity.py" --auth-preflight',
            "if _gh_gate core; then echo GATE-OPEN; else echo GATE-DEFERRED; fi",
        ]
    )
    proc = subprocess.run(
        [BASH, "-c", script], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    seen = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, seen


@pytest.mark.parametrize(
    "scenario, verdict, line",
    [
        (
            "spent-mid-tick",
            "GATE-DEFERRED",
            "gh_capacity gate[core]: shed — 0/5000 remaining; refused (HTTP 403: API rate limit "
            "exceeded for user ID 1.) until ",
        ),
        (
            "secondary-mid-tick",
            "GATE-DEFERRED",
            "gh_capacity gate[core]: shed — refused (HTTP 403: You have exceeded a secondary rate "
            "limit.) until ",
        ),
        (
            "healthy",
            "GATE-OPEN",
            "gh_capacity gate[core]: ok — 4321/5000 remaining (86%); ok "
            "(measured by a real call 0s ago)",
        ),
    ],
    ids=["spent-mid-tick", "secondary-mid-tick", "healthy"],
)
def test_the_tick_gate_reads_core_with_a_real_call_and_sees_a_mid_tick_exhaustion(
    tmp_path: Path, scenario: str, verdict: str, line: str
) -> None:
    proc, calls = _run_tick_gate(tmp_path, scenario)
    assert proc.returncode == 0, proc.stderr
    assert (
        "gh: authenticated as stub-user (core 4321/5000, graphql 4990/5000)" in proc.stdout
    ), "setup: the preflight measured a healthy budget at tick start"
    assert proc.stdout.rstrip().endswith(verdict), (
        f"{scenario}: the gate answered {proc.stdout.split()[-1]!r}, expected {verdict!r}; an "
        f"exhaustion that began after the preflight is invisible again\n{proc.stderr}"
    )
    assert line in proc.stderr, proc.stderr
    assert [c.split()[:3] for c in calls] == [
        ["api", "--include", "user"],  # the preflight's REST reading
        ["api", "--include", "graphql"],  # the preflight's GraphQL reading
        ["api", "--include", "user"],  # the gate's own reading: one real call
    ], calls
    assert not [c for c in calls if c.startswith("api rate_limit")], (
        "the gate asked the exempt endpoint for core, whose fresh 5000/5000 reads every exhaustion "
        f"as headroom: {calls}"
    )
