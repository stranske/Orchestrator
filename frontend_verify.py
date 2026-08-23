#!/usr/bin/env python3
"""frontend_verify.py — orchestrator capability: VISION-FREE frontend/UI verification.

Backlog #2 / BRIEF_expand_range.md #1. Expands the fleet's RANGE: today agents can edit TS/React but
cannot run + observe a UI, so frontend PRs ship unverified. This drives chromium via a LOCAL Playwright
(node) helper and asserts against the ACCESSIBILITY TREE — token-cheap, deterministic, no multimodal
model (Playwright-MCP-style; research: microsoft/playwright-mcp). Any fleet lane (incl. text-only Codex /
vibe) can call it to verify real behavior on a served URL. Opt-in `--screenshot` for canvas/SVG surfaces.
If the automation sandbox cannot launch Chromium directly, pass `--browser-endpoint` or set
`ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT` to connect to an already-authorized Chrome/Chromium CDP endpoint.

Split mirrors the rest of the orchestrator: the node runtime + browsers live LOCAL at
`~/.codex/orchestrator/frontend-verify/` (like aider-venv); THIS Python module is the canonical interface
(on Dropbox). `--selftest` is OFFLINE (no browser) like the other modules; `verify(...)` is the live call.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

VERIFY_DIR = Path(
    os.environ.get(
        "ORCH_FRONTEND_VERIFY_DIR",
        Path.home() / ".codex" / "orchestrator" / "frontend-verify",
    )
)
VERIFY_JS = VERIFY_DIR / "verify.mjs"
BROWSER_ENDPOINT_ENV = (
    "ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT",
    "ORCH_FRONTEND_VERIFY_CDP_ENDPOINT",
)
BROWSER_ENDPOINT_HINT = (
    "Start an authorized Chrome/Chromium with --remote-debugging-port=9222, then pass "
    "--browser-endpoint http://127.0.0.1:9222 or set ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT."
)
DEFAULT_BROWSER_ENDPOINT = "http://127.0.0.1:9222"
DEFAULT_BROWSER_PROFILE = Path.home() / ".codex" / "orchestrator" / "frontend-verify" / "chrome-cdp"
CHROME_PATH_ENV = "ORCH_FRONTEND_VERIFY_CHROME_PATH"
CHROME_PROFILE_ENV = "ORCH_FRONTEND_VERIFY_CHROME_PROFILE"


def default_browser_endpoint() -> str | None:
    for env_name in BROWSER_ENDPOINT_ENV:
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def browser_endpoint_with_source(
    explicit: str | None = None,
) -> tuple[str | None, str | None]:
    if explicit:
        return explicit, "argument"
    for env_name in BROWSER_ENDPOINT_ENV:
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    return None, None


def _endpoint_port(endpoint: str | None) -> tuple[int | None, str | None]:
    endpoint = endpoint or DEFAULT_BROWSER_ENDPOINT
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None, "Use an http://host:port Chrome DevTools Protocol endpoint."
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None, "Refusing to launch a browser for a non-local CDP endpoint."
    if parsed.port is None:
        return None, "CDP endpoint must include a port, for example http://127.0.0.1:9222."
    return int(parsed.port), None


def browser_launch_commands(port: int = 9222, profile_dir: str | None = None) -> list[str]:
    profile = profile_dir or os.environ.get(
        CHROME_PROFILE_ENV, "$HOME/.codex/orchestrator/frontend-verify/chrome-cdp"
    )
    chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    chromium_app = "/Applications/Chromium.app/Contents/MacOS/Chromium"
    args = (
        f"--remote-debugging-port={int(port)} "
        f"--user-data-dir={profile} --no-first-run --no-default-browser-check"
    )
    return [
        f"{shlex.quote(chrome_app)} {args}",
        f"{shlex.quote(chromium_app)} {args}",
    ]


def _candidate_browser_paths(explicit: str | None = None) -> list[str]:
    candidates = [
        explicit,
        os.environ.get(CHROME_PATH_ENV),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    return [str(path) for path in candidates if path]


def _shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def start_browser_endpoint(
    endpoint: str | None,
    *,
    timeout: float = 8.0,
    browser_path: str | None = None,
    profile_dir: str | None = None,
    fetch_url=None,
    sleep=time.sleep,
    popen=subprocess.Popen,
) -> dict:
    endpoint = endpoint or DEFAULT_BROWSER_ENDPOINT
    port, error = _endpoint_port(endpoint)
    profile = Path(
        profile_dir or os.environ.get(CHROME_PROFILE_ENV) or DEFAULT_BROWSER_PROFILE
    ).expanduser()
    if error or port is None:
        return {
            "attempted": False,
            "ok": False,
            "endpoint": endpoint,
            "diagnostic": "browser_start_invalid_endpoint",
            "error": error,
            "hint": BROWSER_ENDPOINT_HINT,
        }
    selected = next(
        (path for path in _candidate_browser_paths(browser_path) if Path(path).exists()),
        None,
    )
    if not selected:
        return {
            "attempted": False,
            "ok": False,
            "endpoint": endpoint,
            "diagnostic": "browser_executable_missing",
            "error": "No Chrome/Chromium executable was found.",
            "launch_commands": browser_launch_commands(port, str(profile)),
        }
    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        selected,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    try:
        proc = popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {
            "attempted": True,
            "ok": False,
            "endpoint": endpoint,
            "diagnostic": "browser_start_failed",
            "error": str(exc),
            "command": _shell_join(argv),
        }
    deadline = time.monotonic() + max(0.0, float(timeout))
    probe = probe_browser_endpoint(endpoint, fetch_url=fetch_url, timeout=1.0)
    while not probe.get("reachable") and time.monotonic() < deadline:
        sleep(0.25)
        probe = probe_browser_endpoint(endpoint, fetch_url=fetch_url, timeout=1.0)
    return {
        "attempted": True,
        "ok": bool(probe.get("reachable")),
        "endpoint": endpoint,
        "diagnostic": ("browser_started" if probe.get("reachable") else "browser_start_not_ready"),
        "pid": getattr(proc, "pid", None),
        "browser_path": selected,
        "profile_dir": str(profile),
        "command": _shell_join(argv),
        "probe": probe,
        "hint": ("Browser endpoint is ready." if probe.get("reachable") else BROWSER_ENDPOINT_HINT),
    }


def _endpoint_version_url(endpoint: str) -> str | None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    base = endpoint.rstrip("/")
    return f"{base}/json/version"


def probe_browser_endpoint(endpoint: str | None, fetch_url=None, timeout: float = 2.0) -> dict:
    if not endpoint:
        return {
            "configured": False,
            "reachable": False,
            "diagnostic": "browser_endpoint_not_configured",
            "hint": BROWSER_ENDPOINT_HINT,
        }
    version_url = _endpoint_version_url(endpoint)
    if not version_url:
        return {
            "configured": True,
            "endpoint": endpoint,
            "reachable": False,
            "diagnostic": "browser_endpoint_invalid",
            "hint": "Use an http://host:port Chrome DevTools Protocol endpoint.",
        }
    fetch = fetch_url or (lambda url, timeout: urlopen(url, timeout=timeout).read())
    try:
        raw = fetch(version_url, timeout)
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    except (OSError, URLError, json.JSONDecodeError, TimeoutError) as exc:
        return {
            "configured": True,
            "endpoint": endpoint,
            "version_url": version_url,
            "reachable": False,
            "diagnostic": "browser_endpoint_connect_failed",
            "error": str(exc),
            "hint": BROWSER_ENDPOINT_HINT,
        }
    return {
        "configured": True,
        "endpoint": endpoint,
        "version_url": version_url,
        "reachable": True,
        "diagnostic": "browser_endpoint_ready",
        "browser": data.get("Browser"),
        "websocket_debugger_url": data.get("webSocketDebuggerUrl"),
    }


def doctor(
    *,
    browser_endpoint: str | None = None,
    require_browser_endpoint: bool = False,
    start_browser: bool = False,
    start_browser_timeout: float = 8.0,
    fetch_url=None,
    helper_path: Path | None = None,
    node_path: str | None = None,
    start_func=start_browser_endpoint,
) -> dict:
    helper = helper_path or VERIFY_JS
    endpoint, endpoint_source = browser_endpoint_with_source(browser_endpoint)
    if start_browser and not endpoint:
        endpoint = DEFAULT_BROWSER_ENDPOINT
        endpoint_source = "default_start_browser"
    endpoint_probe = probe_browser_endpoint(endpoint, fetch_url=fetch_url)
    browser_start = None
    if start_browser and not endpoint_probe.get("reachable"):
        browser_start = start_func(
            endpoint,
            timeout=start_browser_timeout,
            fetch_url=fetch_url,
        )
        endpoint_probe = probe_browser_endpoint(endpoint, fetch_url=fetch_url)
    helper_ok = helper.exists()
    resolved_node = node_path if node_path is not None else shutil.which("node")
    node_ok = bool(resolved_node)
    cdp_ok = bool(endpoint_probe.get("reachable"))
    ok = helper_ok and node_ok and (cdp_ok or not require_browser_endpoint)
    if cdp_ok:
        status = "ready"
    elif ok:
        status = "direct_launch_only"
    else:
        status = "not_ready"
    return {
        "ok": ok,
        "status": status,
        "helper": {
            "path": str(helper),
            "exists": helper_ok,
            "diagnostic": "helper_ready" if helper_ok else "helper_missing",
        },
        "node": {
            "available": node_ok,
            "path": resolved_node,
            "diagnostic": "node_ready" if node_ok else "node_missing",
        },
        "browser_endpoint_required": require_browser_endpoint,
        "browser_endpoint_source": endpoint_source,
        "browser_endpoint": endpoint_probe,
        "browser_start": browser_start,
        "launch_commands": browser_launch_commands(_endpoint_port(endpoint)[0] or 9222),
        "hint": (
            "CDP endpoint is ready for sandboxed frontend verification."
            if cdp_ok
            else BROWSER_ENDPOINT_HINT
        ),
    }


def build_node_argv(
    url: str,
    asserts=None,
    click_text=None,
    then_text=None,
    screenshot=None,
    timeout: int = 15000,
    browser_endpoint: str | None = None,
) -> list[str]:
    """Pure: map a verification request to the node helper's argv (selftested)."""
    argv = ["node", str(VERIFY_JS), "--url", url, "--timeout", str(int(timeout))]
    for a in asserts or []:
        argv += ["--assert", a]
    if click_text:
        argv += ["--click-text", click_text]
    if then_text:
        argv += ["--then-text", then_text]
    if screenshot:
        argv += ["--screenshot", screenshot]
    if browser_endpoint:
        argv += ["--browser-endpoint", browser_endpoint]
    return argv


def classify_helper_failure(
    stdout: str, stderr: str, returncode: int, browser_endpoint: str | None = None
) -> dict | None:
    """Best-effort classification for failures that happen before helper JSON is emitted."""
    message = f"{stdout}\n{stderr}"
    raw_tail = (stdout or "")[-500:]
    stderr_tail = (stderr or "")[-500:]
    if "MachPortRendezvous" in message or "bootstrap_check_in" in message:
        return {
            "ok": False,
            "error": "Chromium launch was blocked by the macOS sandbox.",
            "diagnostic": "browser_launch_permission_denied",
            "hint": BROWSER_ENDPOINT_HINT,
            "returncode": returncode,
            "raw": raw_tail,
            "stderr": stderr_tail,
        }
    if browser_endpoint and any(
        token in message
        for token in (
            "ECONNREFUSED",
            "ECONNRESET",
            "socket hang up",
            "WebSocket error",
            "Failed to fetch browser webSocket url",
        )
    ):
        return {
            "ok": False,
            "error": f"could not connect to browser endpoint {browser_endpoint}",
            "diagnostic": "browser_endpoint_connect_failed",
            "hint": BROWSER_ENDPOINT_HINT,
            "returncode": returncode,
            "raw": raw_tail,
            "stderr": stderr_tail,
        }
    if "Executable doesn't exist" in message or "Please run" in message:
        return {
            "ok": False,
            "error": "Playwright Chromium is not installed.",
            "diagnostic": "browser_not_installed",
            "hint": "Install the Playwright Chromium browser in ~/.codex/orchestrator/frontend-verify.",
            "returncode": returncode,
            "raw": raw_tail,
            "stderr": stderr_tail,
        }
    return None


def verify(
    url: str,
    asserts=None,
    click_text=None,
    then_text=None,
    screenshot=None,
    timeout: int = 15000,
    browser_endpoint: str | None = None,
) -> dict:
    """Live: run the Playwright helper and return its structured JSON verdict
    {ok, url, title, findings:[{type,target,pass,detail}], snapshot, error, diagnostic, hint}.
    """
    if not VERIFY_JS.exists():
        return {
            "ok": False,
            "error": f"verify.mjs not found at {VERIFY_JS} — run the frontend-verify setup "
            f"(npm i playwright + npx playwright install chromium in that dir)",
            "diagnostic": "helper_missing",
            "hint": "Install or restore the local frontend verification helper under ORCH_FRONTEND_VERIFY_DIR.",
        }
    if not shutil.which("node"):
        return {
            "ok": False,
            "error": "node not on PATH",
            "diagnostic": "node_missing",
            "hint": "Install Node.js or add it to PATH before running frontend_verify.py.",
        }
    endpoint = browser_endpoint if browser_endpoint is not None else default_browser_endpoint()
    argv = build_node_argv(url, asserts, click_text, then_text, screenshot, timeout, endpoint)
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=str(VERIFY_DIR),
            timeout=max(45, timeout / 1000 + 45),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "verifier timed out",
            "diagnostic": "verifier_timeout",
            "hint": "Increase --timeout or check whether the page is blocked on app startup/API calls.",
        }
    try:
        return json.loads(r.stdout)
    except Exception:
        classified = classify_helper_failure(r.stdout or "", r.stderr or "", r.returncode, endpoint)
        if classified:
            return classified
        return {
            "ok": False,
            "error": "could not parse verifier output",
            "diagnostic": "invalid_helper_output",
            "hint": "Inspect the helper stderr/stdout and rerun with a narrower URL/assertion.",
            "raw": (r.stdout or "")[-500:],
            "stderr": (r.stderr or "")[-300:],
        }


def _selftest() -> None:
    global VERIFY_JS
    # pure argv mapping — repeatable asserts + flow + screenshot
    argv = build_node_argv(
        "http://x/",
        asserts=["text:Test Page", "role:button=Continue"],
        click_text="Continue",
        then_text="Next panel",
        screenshot="/tmp/s.png",
        timeout=9000,
    )
    assert argv[:4] == ["node", str(VERIFY_JS), "--url", "http://x/"], argv
    assert (
        argv.count("--assert") == 2 and "text:Test Page" in argv and "role:button=Continue" in argv
    ), argv
    assert "--click-text" in argv and "--then-text" in argv and "--screenshot" in argv, argv
    assert "9000" in argv, argv
    cdp_argv = build_node_argv(
        "http://x/", asserts=["text:hi"], browser_endpoint="http://127.0.0.1:9222"
    )
    assert cdp_argv[-2:] == ["--browser-endpoint", "http://127.0.0.1:9222"], cdp_argv
    classified = classify_helper_failure("", "MachPortRendezvous failed bootstrap_check_in", 1)
    assert classified and classified["diagnostic"] == "browser_launch_permission_denied", classified
    classified = classify_helper_failure(
        "", "connect ECONNREFUSED 127.0.0.1:9222", 1, "http://127.0.0.1:9222"
    )
    assert classified and classified["diagnostic"] == "browser_endpoint_connect_failed", classified
    ready_probe = probe_browser_endpoint(
        "http://127.0.0.1:9222",
        fetch_url=lambda url, timeout: b'{"Browser":"Chrome/fixture","webSocketDebuggerUrl":"ws://fixture"}',
    )
    assert (
        ready_probe["reachable"] is True and ready_probe["browser"] == "Chrome/fixture"
    ), ready_probe
    invalid_probe = probe_browser_endpoint("ws://127.0.0.1:9222")
    assert invalid_probe["diagnostic"] == "browser_endpoint_invalid", invalid_probe
    saved_env = {name: os.environ.get(name) for name in BROWSER_ENDPOINT_ENV}
    try:
        os.environ["ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT"] = "http://127.0.0.1:9222"
        os.environ.pop("ORCH_FRONTEND_VERIFY_CDP_ENDPOINT", None)
        assert default_browser_endpoint() == "http://127.0.0.1:9222"
        endpoint, source = browser_endpoint_with_source()
        assert (
            endpoint == "http://127.0.0.1:9222"
            and source == "ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT"
        )
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    import tempfile

    helper = Path(tempfile.mkdtemp()) / "verify.mjs"
    helper.write_text("// fixture\n", encoding="utf-8")
    doctor_out = doctor(
        browser_endpoint="http://127.0.0.1:9222",
        require_browser_endpoint=True,
        fetch_url=lambda url, timeout: b'{"Browser":"Chrome/fixture"}',
        helper_path=helper,
        node_path="/usr/bin/node",
    )
    assert doctor_out["ok"] is True and doctor_out["status"] == "ready", doctor_out
    blocked_doctor = doctor(
        require_browser_endpoint=True,
        fetch_url=lambda url, timeout: (_ for _ in ()).throw(OSError("refused")),
        helper_path=helper,
        node_path="/usr/bin/node",
    )
    assert blocked_doctor["ok"] is False and blocked_doctor["status"] == "not_ready", blocked_doctor
    start_state = {"ready": False}

    def fake_fetch(url, timeout):
        if start_state["ready"]:
            return b'{"Browser":"Chrome/started"}'
        raise OSError("refused")

    def fake_start(endpoint, **kwargs):
        start_state["ready"] = True
        return {
            "attempted": True,
            "ok": True,
            "endpoint": endpoint,
            "diagnostic": "browser_started",
        }

    started_doctor = doctor(
        require_browser_endpoint=True,
        start_browser=True,
        fetch_url=fake_fetch,
        helper_path=helper,
        node_path="/usr/bin/node",
        start_func=fake_start,
    )
    assert started_doctor["ok"] is True and started_doctor["status"] == "ready", started_doctor
    assert (
        started_doctor["browser_endpoint_source"] == "default_start_browser"
        and started_doctor["browser_start"]["diagnostic"] == "browser_started"
    ), started_doctor
    nonlocal_start = start_browser_endpoint(
        "http://example.com:9222",
        timeout=0,
        popen=lambda *args, **kwargs: None,
    )
    assert (
        nonlocal_start["attempted"] is False
        and nonlocal_start["diagnostic"] == "browser_start_invalid_endpoint"
    ), nonlocal_start
    parser = build_parser()
    parsed = parser.parse_args(
        ["--doctor", "--require-browser-endpoint", "--start-browser", "--json"]
    )
    assert (
        parsed.doctor
        and parsed.require_browser_endpoint
        and parsed.start_browser
        and parsed.json_output
    ), parsed
    # missing-helper path returns a clean error, not an exception
    saved = VERIFY_JS
    VERIFY_JS = Path(tempfile.mkdtemp()) / "nope.mjs"
    try:
        out = verify("http://x/", asserts=["text:hi"])
        assert out["ok"] is False and out["diagnostic"] == "helper_missing", out
    finally:
        VERIFY_JS = saved
    print(
        "frontend_verify.py selftest: OK (node-argv mapping: repeatable asserts + flow + screenshot + "
        "timeout + browser endpoint; doctor/preflight; guarded browser start; launch/endpoint diagnostics; "
        "missing-helper returns a clean error)"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Vision-free frontend verification via the accessibility tree."
    )
    p.add_argument("--url", help="page URL to verify (e.g. a running dev server)")
    p.add_argument(
        "--assert",
        dest="asserts",
        action="append",
        default=[],
        help='assertion: "text:<substr>" or "role:<role>[=<name>]" (repeatable)',
    )
    p.add_argument(
        "--click-text",
        help="optional: click the first element containing this text (before asserting)",
    )
    p.add_argument("--then-text", help="optional: after the click, wait for this text to appear")
    p.add_argument(
        "--screenshot",
        help="optional: also save a screenshot here (vision fallback for canvas/SVG)",
    )
    p.add_argument(
        "--browser-endpoint",
        help="optional Chrome/Chromium CDP endpoint, e.g. http://127.0.0.1:9222; "
        "also read from ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT",
    )
    p.add_argument("--timeout", type=int, default=15000)
    p.add_argument(
        "--doctor",
        action="store_true",
        help="check helper/node/CDP readiness without opening a page",
    )
    p.add_argument(
        "--require-browser-endpoint",
        action="store_true",
        help="with --doctor, fail unless a reachable CDP endpoint is configured",
    )
    p.add_argument(
        "--start-browser",
        action="store_true",
        help="with --doctor, start local Chrome/Chromium when the CDP endpoint is unreachable",
    )
    p.add_argument(
        "--start-browser-timeout",
        type=float,
        default=8.0,
        help="seconds to wait for a --start-browser endpoint to become reachable",
    )
    p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="accepted for doctor/automation compatibility; output is already JSON",
    )
    p.add_argument("--selftest", action="store_true")
    return p


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities

        capabilities.production_heartbeat(
            "frontend-verifier", event_type, ref="frontend_verify.main"
        )
    except Exception:
        pass


def main(argv) -> int:
    _capability_heartbeat()
    p = build_parser()
    ns = p.parse_args(argv)
    if ns.selftest:
        _selftest()
        return 0
    if ns.doctor:
        out = doctor(
            browser_endpoint=ns.browser_endpoint,
            require_browser_endpoint=ns.require_browser_endpoint,
            start_browser=ns.start_browser,
            start_browser_timeout=ns.start_browser_timeout,
        )
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    if not ns.url:
        p.error("--url is required (or use --doctor/--selftest)")
    out = verify(
        ns.url,
        ns.asserts,
        ns.click_text,
        ns.then_text,
        ns.screenshot,
        ns.timeout,
        ns.browser_endpoint,
    )
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
