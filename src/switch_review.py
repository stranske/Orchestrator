#!/usr/bin/env python3
"""switch_review.py — held switches must be re-raised, not quietly forgotten.

THE FAILURE THIS PREVENTS. `ORCH_RANGE_LANE_ROLLOUT` was turned on as a bounded trial on
2026-07-08, reviewed 07-15, extended to 07-22 — and then nothing. It produced 2 dispatches (both
`transient_infra` rc=137) and 5 days were dispatch-skipped by a stale worktree, so the evidence was
too thin to either keep or revert. The decision was deferred and the deferral was never revisited.
A month later the flag was simply off, with no record of a decision having been made.

That is the same latched shape as every other bug in this system: a state whose exit depends on
somebody remembering. So this module does two things on a weekly cadence:

  1. **A held switch with a satisfied precondition gets raised.** If the machine-checkable criterion
     in `capability_recurrence_check.SWITCH_ON_CRITERIA` is met and the flag is still off, that is a
     decision waiting to be made, and it is surfaced as a non-blocking owner question.
  2. **A switch that is ON but NOT TRIGGERING gets raised.** This is the range-lane case exactly:
     enabling a lane that then dispatches nothing is indistinguishable from leaving it off, unless
     something notices. If a switch has been on for >= REVIEW_DAYS and its capability recorded no
     invocation in that window, the question comes back.

NON-BLOCKING BY CONSTRUCTION. Everything goes through `feedback.owner_questions`: deduped per scope,
auto-ratifying at expiry to a stated default, so an unanswered question can never accumulate into a
backlog. The default is always the conservative one — keep the current switch position — because
flipping a safety switch on silence is precisely what must not happen.

    python3 switch_review.py                # what is due for review
    python3 switch_review.py --json
    python3 switch_review.py --raise        # record owner questions (needs ORCH_SWITCH_REVIEW=1)
    python3 switch_review.py --selftest
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import capabilities
import paths

# How long a switch may sit unreviewed before the question comes back.
REVIEW_DAYS = 7
QUESTION_EXPIRY_DAYS = 7.0
APPLY_ENABLED = os.environ.get("ORCH_SWITCH_REVIEW", "").strip() == "1"

# Fleet template-delivery gates (Maint 68 promote + sync-branch canaries). Same horizon as switch
# review: a chain latched for a week with open canaries is the failure mode observed 2026-09-02.
FLEET_GATE_DAYS = REVIEW_DAYS
WORKFLOWS_REPO = "stranske/Workflows"
MAINT_68_WORKFLOW = "maint-68-sync-consumer-repos.yml"
MAINT_82_WORKFLOW = "maint-82-sync-dependency-campaign.yml"
CANARY_BRANCHES = frozenset({"sync/workflows-candidate", "sync/workflows-delivery"})
CONTINUATION_LOG_MARKER = "Dispatched due Maint 71"
GH_TIMEOUT_S = 30
MAINT_68_PROMOTE_LOOKBACK = 40
MAINT_82_LOG_RUN_BUDGET = 2
MAINT_68_REGISTRY_FALLBACK = [
    "stranske/Template",
    "stranske/Ready",
    "stranske/Collab-Admin",
    "stranske/learning-management-system",
    "stranske/Fine-Art-Archive",
]

# Test-only injection for every `gh` call in this module.
_GH_CALL_RUNNER: Callable[..., tuple[bool, str, str]] | None = None

# flag -> the capability whose invocations prove the switch is doing anything.
SWITCH_CAPABILITY = {
    "ORCH_RANGE_LANE_ROLLOUT": "range-lane-rollout",
    # REMAPPED 2026-08-22. This pointed at `deliberate-break-verifier` on the belief that the flag
    # gated the deliberate-break command. It does not (ORCH-ANCHOR:
    # runtime-ac-command-exec-gate — COMMAND_EXEC_GATED_TYPES excludes deliberate_break, verified by
    # executing a real spec both ways). Pointing the "ON but silent" arm at a capability the switch
    # cannot influence made this review unable to say anything true about either one. The capability
    # whose invocations DO prove this switch is doing something is the runtime-AC gate that runs the
    # command checks it authorises.
    "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "runtime-ac-checks",
    "ORCH_FRONTEND_VERIFY_START_BROWSER": "frontend-verifier",
    "ORCH_STRATEGY_EXPERIMENT": "strategy-experiments",
    "ORCH_EXPLORATION_MODE": "thompson-hybrid-routing",
}


def _last_invocation(cap_id: str, *, path=None) -> int:
    caps = capabilities.load(path or capabilities.REG)
    return int((caps.get(cap_id) or {}).get("last_invocation") or 0)


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Credit this capability at its declared entrypoint.

    Added immediately after the activation audit flagged `switch-review` with `no_heartbeat` — the
    same omission `issue-readiness` had. A module that reviews other capabilities' observability
    while recording nothing about itself is not a defensible position.
    """
    try:
        import capabilities as _caps

        _caps.production_heartbeat("switch-review", event_type, ref="switch_review.review")
    except Exception as exc:
        # Continue -- the review is the product and a telemetry failure must not block it. But say
        # so: a swallowed heartbeat leaves later audits classifying switch-review as unobserved,
        # and an unexplained silence is indistinguishable from a pass.
        print(f"switch_review: capability heartbeat failed: {exc}", file=sys.stderr)


MIRROR_DIR = Path(os.environ.get("ORCH_MIRROR", Path.home() / ".codex" / "orchestrator-mirror"))


def stale_runners(*, now: float | None = None, mirror: Path | None = None) -> list[dict]:
    """Long-lived processes running mirror code OLDER than the mirror on disk.

    WHY THIS IS THE SAME DEFECT CLASS AS A HELD SWITCH. `orch-sync-mirror.sh` is treated as the
    deploy step, but Python caches modules at import: a process started before the sync keeps
    running the previous code for as long as it lives. Observed 2026-08-22 -- a cursor offload one
    minute AFTER a sync still used the pre-sync dispatcher, because four `mcp_server.py` processes
    (oldest 7h19m) each held their own copy. The fix looked broken and the code was fine.

    Silence is the symptom, exactly as with a switch nobody revisits: nothing errors, the run simply
    behaves like last week. FYI-only -- this NEVER kills anything, because a live process may be
    serving a session and a sweep that can restart the fleet is a worse hazard than stale code.
    """
    mirror = Path(mirror or MIRROR_DIR)
    try:
        newest = max((f.stat().st_mtime for f in mirror.glob("*.py")), default=0.0)
    except OSError:
        return []
    if not newest:
        return []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    resolved_now = float(now if now is not None else time.time())
    stale: list[dict] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or str(mirror) not in parts[2]:
            continue
        pid, etime, command = parts
        age = _etime_seconds(etime)
        if age is None:
            continue
        started = resolved_now - age
        if started >= newest:
            continue
        stale.append(
            {
                "pid": pid,
                "age_hours": round(age / 3600, 1),
                "stale_by_hours": round((newest - started) / 3600, 1),
                "command": command[:120],
                "reason": (
                    "started before the current mirror was written, so it is still running the "
                    "previous code; a sync does not reach a process that has already imported"
                ),
            }
        )
    return stale


def mirror_drift(*, mirror: Path | None = None, checkout: Path | None = None) -> dict:
    """Whether the deployed mirror actually carries the code that is on main.

    THE SYNC CAN SUCCEED AND LEAVE THE MIRROR WRONG, and that is not hypothetical: on 2026-08-30
    `orch-sync-mirror.sh` was run twice, reported nothing wrong both times, and copied faithfully
    from a checkout sitting EIGHT COMMITS behind `origin/main`. The mirror gained nothing and said
    so nowhere. Four merged changes stayed inert — including the module of the capability whose
    ledger row had just been registered — while every visible signal read "synced".

    So the question has TWO halves and only asking one is how that happened:

      1. Does the mirror match the checkout? Catches a sync that never ran.
      2. Is the checkout behind its upstream? Catches a sync that ran from stale code, which the
         first question CANNOT see, because after such a sync the two trees agree perfectly.

    Read from local refs (`rev-list @{u}..HEAD`), never a fetch: this runs inside a weekly sweep on
    a Dropbox volume where a network git call can hang for minutes, and a sweep that hangs is a
    sweep that gets disabled. An un-fetched checkout therefore UNDERCOUNTS, which is stated in the
    reason rather than presented as a clean bill.

    FYI-only, like everything else in this sweep. It never syncs anything: an automatic deploy is
    exactly the circuit breaker the manual sync exists to be.
    """
    mirror_dir = Path(mirror or MIRROR_DIR)
    checkout_dir = Path(checkout) if checkout else paths.MODULE_DIR
    out: dict = {
        "mirror": str(mirror_dir),
        "checkout": str(checkout_dir),
        "absent_from_mirror": [],
        "differing": [],
        "checkout_behind": None,
        "status": "unknown",
        "reason": "",
    }

    if not mirror_dir.is_dir():
        out["reason"] = (
            f"no mirror at {mirror_dir} — cannot compare, which is not the same as clean"
        )
        return out
    if not checkout_dir.is_dir():
        out["reason"] = f"no checkout modules at {checkout_dir} — nothing to compare against"
        return out

    for source in sorted(checkout_dir.glob("*.py")):
        deployed = mirror_dir / source.name
        if not deployed.exists():
            out["absent_from_mirror"].append(source.name)
            continue
        try:
            if source.read_bytes() != deployed.read_bytes():
                out["differing"].append(source.name)
        except OSError:
            out["differing"].append(source.name)

    behind_reason = ""
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", "@{u}..HEAD", "--"],
            cwd=paths.REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        ahead = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{u}", "--"],
            cwd=paths.REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if ahead.returncode == 0 and ahead.stdout.strip().isdigit():
            out["checkout_behind"] = int(ahead.stdout.strip())
        else:
            behind_reason = " (no upstream ref locally, so 'behind' is UNMEASURED, not zero)"
        del proc
    except (OSError, subprocess.SubprocessError):
        behind_reason = " (git unavailable, so 'behind' is UNMEASURED, not zero)"

    drifted = bool(out["absent_from_mirror"] or out["differing"]) or bool(out["checkout_behind"])
    out["status"] = "drifted" if drifted else "ok"
    if drifted:
        bits = []
        if out["absent_from_mirror"]:
            bits.append(f"{len(out['absent_from_mirror'])} module(s) absent from the mirror")
        if out["differing"]:
            bits.append(f"{len(out['differing'])} differing")
        if out["checkout_behind"]:
            bits.append(
                f"the checkout is {out['checkout_behind']} commit(s) behind upstream, so syncing "
                "again would deploy stale code and report success"
            )
        out["reason"] = "; ".join(bits) + behind_reason
    else:
        out["reason"] = (
            "mirror matches the checkout and the checkout is level with its upstream"
            + behind_reason
        )
    return out


def _etime_seconds(etime: str) -> int | None:
    """`ps` etime (`[[dd-]hh:]mm:ss`) as seconds."""
    text = etime.strip()
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    bits = text.split(":")
    try:
        nums = [int(b) for b in bits]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    return days * 86400 + nums[0] * 3600 + nums[1] * 60 + nums[2]


def _parse_iso_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _gh_call(args: list[str], *, timeout_s: int = GH_TIMEOUT_S) -> tuple[bool, str, str]:
    """Run one `gh` invocation. Returns (ok, stdout, error_reason_for_unmeasured)."""
    if _GH_CALL_RUNNER is not None:
        return _GH_CALL_RUNNER(args, timeout_s=timeout_s)
    cmd = ["gh", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", f"unmeasured: gh timed out after {timeout_s}s ({' '.join(args[:4])})"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", f"unmeasured: gh unavailable ({exc})"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "gh failed").strip().replace("\n", " ")[:160]
        return False, "", f"unmeasured: {err}"
    return True, proc.stdout or "", ""


def _parse_maint_68_registry_yaml(content: str) -> list[str]:
    repos: list[str] = []
    in_repos = False
    for line in content.splitlines():
        if "REGISTERED_CONSUMER_REPOS:" in line:
            in_repos = True
            continue
        if in_repos:
            if (
                line.strip()
                and not line.startswith(" ")
                and not line.startswith("-")
                and ":" in line
            ):
                break
            cleaned = line.strip().strip("-").strip().strip('"').strip("'").strip()
            if cleaned and "/" in cleaned:
                repos.append(cleaned)
    return repos


def _registered_consumer_repos(
    repos_fn: Callable[[], list[str]] | None = None,
    *,
    gh_fn: Callable[..., tuple[bool, str, str]] | None = None,
) -> tuple[list[str], str]:
    if repos_fn is not None:
        return list(repos_fn()), ""
    try:
        from consumer_sync_artifact_ingest import TEST_REGISTRY

        if TEST_REGISTRY:
            return list(TEST_REGISTRY), ""
    except Exception:  # noqa: BLE001
        pass
    gh = gh_fn or _gh_call
    ok, out, reason = gh(
        [
            "api",
            "repos/stranske/Workflows/contents/.github/workflows/maint-68-sync-consumer-repos.yml",
        ]
    )
    if ok:
        try:
            payload = json.loads(out or "{}")
            content = base64.b64decode(payload["content"]).decode("utf-8")
            repos = _parse_maint_68_registry_yaml(content)
            if repos:
                return repos, ""
        except (KeyError, json.JSONDecodeError, ValueError) as exc:
            reason = f"unmeasured: maint-68 registry parse failed ({exc})"
    if not reason:
        reason = "unmeasured: maint-68 registry fetch failed"
    # Use the same static fallback as consumer_sync_artifact_ingest.get_maint_68_repos so
    # offline sweeps still scan the cohort rather than reporting zero repos.
    return list(MAINT_68_REGISTRY_FALLBACK), reason


def _checks_all_green(status_check_rollup: object) -> bool:
    if not isinstance(status_check_rollup, dict):
        return False
    state = str(status_check_rollup.get("state") or "").upper()
    if state == "SUCCESS":
        return True
    contexts = status_check_rollup.get("contexts") or []
    if not isinstance(contexts, list) or not contexts:
        return False
    return all(str((row or {}).get("state") or "").upper() == "SUCCESS" for row in contexts)


_REVIEW_THREADS_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    " reviewThreads(first: 100) { nodes { isResolved } }"
    " } } }"
)


def _unresolved_review_threads(repo: str, number: int, *, gh_fn) -> tuple[int | None, str]:
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None, "unmeasured: invalid repo slug"
    owner, name = parts
    ok, out, reason = gh_fn(
        [
            "api",
            "graphql",
            "-f",
            f"query={_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
    )
    if not ok:
        return None, reason
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        return None, "unmeasured: reviewThreads GraphQL JSON parse failed"
    threads = (
        ((payload.get("data") or {}).get("repository") or {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes")
    )
    if not isinstance(threads, list):
        return None, "unmeasured: reviewThreads nodes missing"
    unresolved = sum(1 for row in threads if not (row or {}).get("isResolved"))
    return unresolved, ""


def _promote_age_days(*, now: int, gh_fn) -> dict:
    ok, out, reason = gh_fn(
        [
            "run",
            "list",
            "--repo",
            WORKFLOWS_REPO,
            "--workflow",
            MAINT_68_WORKFLOW,
            "--limit",
            str(MAINT_68_PROMOTE_LOOKBACK),
            "--json",
            "databaseId,displayTitle,conclusion,createdAt,status",
        ]
    )
    if not ok:
        return {
            "days_since_success": None,
            "last_success_at": None,
            "display_title": None,
            "measurement": reason,
        }
    try:
        runs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {
            "days_since_success": None,
            "last_success_at": None,
            "display_title": None,
            "measurement": "unmeasured: maint-68 run list JSON parse failed",
        }
    for row in runs:
        if not isinstance(row, dict):
            continue
        title = str(row.get("displayTitle") or "")
        if "promote" not in title.lower():
            continue
        if str(row.get("conclusion") or "") != "success":
            continue
        created = _parse_iso_ts(str(row.get("createdAt") or ""))
        if created is None:
            continue
        age_days = round((now - created) / 86400, 1)
        return {
            "days_since_success": age_days,
            "last_success_at": row.get("createdAt"),
            "display_title": title,
            "measurement": "measured",
            "run_id": row.get("databaseId"),
        }
    return {
        "days_since_success": None,
        "last_success_at": None,
        "display_title": None,
        "measurement": (
            f"unmeasured: no successful Maint 68 promote in last {MAINT_68_PROMOTE_LOOKBACK} runs"
        ),
    }


def _open_canaries(*, now: int, gh_fn, repos_fn) -> dict:
    repos, registry_reason = _registered_consumer_repos(repos_fn, gh_fn=gh_fn)
    if not repos:
        return {
            "open": [],
            "open_count": None,
            "drainable_count": None,
            "repos_checked": 0,
            "measurement": registry_reason or "unmeasured: no registered consumer repos",
        }
    open_rows: list[dict] = []
    repo_errors: list[str] = []
    for repo in repos:
        ok, out, reason = gh_fn(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "30",
                "--json",
                "number,headRefName,createdAt,statusCheckRollup,isDraft",
            ]
        )
        if not ok:
            repo_errors.append(f"{repo}: {reason}")
            continue
        try:
            prs = json.loads(out or "[]")
        except json.JSONDecodeError:
            repo_errors.append(f"{repo}: unmeasured: pr list JSON parse failed")
            continue
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            branch = str(pr.get("headRefName") or "")
            if branch not in CANARY_BRANCHES:
                continue
            if pr.get("isDraft"):
                continue
            created = _parse_iso_ts(str(pr.get("createdAt") or ""))
            age_days = None if created is None else round((now - created) / 86400, 1)
            checks_green = _checks_all_green(pr.get("statusCheckRollup"))
            unresolved, thread_reason = _unresolved_review_threads(
                repo, int(pr["number"]), gh_fn=gh_fn
            )
            drainable = (
                checks_green and unresolved == 0 and age_days is not None and thread_reason == ""
            )
            open_rows.append(
                {
                    "repo": repo,
                    "number": pr.get("number"),
                    "branch": branch,
                    "age_days": age_days,
                    "checks_green": checks_green,
                    "unresolved_threads": unresolved,
                    "drainable": drainable,
                    "thread_measurement": thread_reason,
                }
            )
    if repo_errors and not open_rows:
        return {
            "open": [],
            "open_count": None,
            "drainable_count": None,
            "repos_checked": len(repos),
            "measurement": "; ".join(repo_errors[:3]),
        }
    drainable_count = sum(1 for row in open_rows if row.get("drainable"))
    measurement = "measured"
    if registry_reason:
        measurement = f"measured with registry gap ({registry_reason})"
    if repo_errors:
        measurement = f"{measurement}; {len(repo_errors)} repo(s) unreadable"
    return {
        "open": open_rows,
        "open_count": len(open_rows),
        "drainable_count": drainable_count,
        "repos_checked": len(repos),
        "measurement": measurement,
        "repo_errors": repo_errors,
    }


def _maint_82_continuation_hours(*, now: int, gh_fn) -> dict:
    ok, out, reason = gh_fn(
        [
            "run",
            "list",
            "--repo",
            WORKFLOWS_REPO,
            "--workflow",
            MAINT_82_WORKFLOW,
            "--limit",
            "15",
            "--json",
            "databaseId,conclusion,createdAt,status",
        ]
    )
    if not ok:
        return {"hours_since_dispatch": None, "measurement": reason}
    try:
        runs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {
            "hours_since_dispatch": None,
            "measurement": "unmeasured: maint-82 run list JSON parse failed",
        }
    completed = [
        row for row in runs if isinstance(row, dict) and str(row.get("status") or "") == "completed"
    ]
    if not completed:
        return {
            "hours_since_dispatch": None,
            "measurement": "unmeasured: no completed maint-82 runs in lookback",
        }
    # Log download is one call per run; budget to the newest few so the sweep stays bounded.
    for row in completed[:MAINT_82_LOG_RUN_BUDGET]:
        run_id = row.get("databaseId")
        if run_id is None:
            continue
        log_ok, log_out, log_reason = gh_fn(
            ["run", "view", str(run_id), "--repo", WORKFLOWS_REPO, "--log"],
            timeout_s=GH_TIMEOUT_S,
        )
        if not log_ok:
            return {
                "hours_since_dispatch": None,
                "measurement": (
                    "unmeasured: maint-82 log fetch deferred " f"(per-run download; {log_reason})"
                ),
            }
        if CONTINUATION_LOG_MARKER not in log_out:
            continue
        created = _parse_iso_ts(str(row.get("createdAt") or ""))
        if created is None:
            continue
        return {
            "hours_since_dispatch": round((now - created) / 3600, 1),
            "last_dispatch_at": row.get("createdAt"),
            "run_id": run_id,
            "measurement": "measured",
        }
    return {
        "hours_since_dispatch": None,
        "measurement": (
            f"unmeasured: no '{CONTINUATION_LOG_MARKER}' in last "
            f"{MAINT_82_LOG_RUN_BUDGET} completed maint-82 logs"
        ),
    }


def fleet_gates(
    *,
    now: int | None = None,
    gh_fn: Callable[..., tuple[bool, str, str]] | None = None,
    repos_fn: Callable[[], list[str]] | None = None,
) -> dict:
    """Report-only fleet template-delivery gate health (Maint 68 promote + sync canaries)."""
    resolved_now = int(now if now is not None else time.time())
    gh = gh_fn or _gh_call
    promote = _promote_age_days(now=resolved_now, gh_fn=gh)
    canaries = _open_canaries(now=resolved_now, gh_fn=gh, repos_fn=repos_fn)
    continuation = _maint_82_continuation_hours(now=resolved_now, gh_fn=gh)

    promote_days = promote.get("days_since_success")
    open_count = canaries.get("open_count")
    drainable_count = canaries.get("drainable_count")
    stale_canaries = [
        row
        for row in (canaries.get("open") or [])
        if isinstance(row.get("age_days"), (int, float)) and row["age_days"] > FLEET_GATE_DAYS
    ]

    suspect = False
    suspect_reason = ""
    clear_paths = ""
    if promote_days is not None and promote_days > FLEET_GATE_DAYS and stale_canaries:
        suspect = True
        suspect_reason = (
            f"template-delivery chain SUSPECT: promote stale {promote_days}d / "
            f"open canaries {open_count} (drainable {drainable_count})"
        )
        clear_paths = (
            "clears when a successful Maint 68 promote runs OR every open canary >7d merges/closes"
        )
    elif promote.get("measurement", "").startswith("unmeasured"):
        suspect_reason = promote["measurement"]
    elif canaries.get("measurement", "").startswith("unmeasured"):
        suspect_reason = canaries["measurement"]

    status = "suspect" if suspect else "ok"
    if promote.get("measurement", "").startswith("unmeasured") and open_count is None:
        status = "unknown"

    return {
        "status": status,
        "gate_horizon_days": FLEET_GATE_DAYS,
        "promote": promote,
        "canaries": canaries,
        "maint_82_continuation": continuation,
        "suspect": suspect,
        "suspect_reason": suspect_reason,
        "clear_paths": clear_paths,
        "stale_canary_count": len(stale_canaries),
    }


def review(*, now: int | None = None, env: Mapping[str, str] | None = None, path=None) -> dict:
    """Which held-or-idle switches are due for an owner decision, and why."""
    import capability_recurrence_check as rc

    _capability_heartbeat()

    now = int(now if now is not None else time.time())
    env = os.environ if env is None else env
    window = REVIEW_DAYS * 86400
    due, quiet = [], []

    for flag, cap_id in sorted(SWITCH_CAPABILITY.items()):
        value = env.get(flag)
        on = bool(value) and value != "0"
        last = _last_invocation(cap_id, path=path)
        criterion = rc.SWITCH_ON_CRITERIA.get(flag)

        if not on:
            # OFF: raise only when a criterion exists, so an unconditioned switch is not nagged
            # about forever. An unconditioned switch is a documentation gap, reported separately.
            due.append(
                {
                    "flag": flag,
                    "capability": cap_id,
                    "state": "off",
                    "criterion": criterion,
                    "reason": (
                        "held off; a machine-checkable precondition is recorded, so this is a "
                        "decision waiting to be made"
                        if criterion
                        else "held off with NO recorded switch-on criterion"
                    ),
                    "has_criterion": bool(criterion),
                }
            )
            continue

        # ON but silent for the whole window: enabling it changed nothing observable.
        idle_days = (now - last) / 86400 if last else None
        if last == 0 or (now - last) > window:
            quiet.append(
                {
                    "flag": flag,
                    "capability": cap_id,
                    "state": "on",
                    "idle_days": None if idle_days is None else round(idle_days, 1),
                    "reason": (
                        f"ON but {cap_id} recorded no invocation in the last {REVIEW_DAYS}d — "
                        "an enabled switch that dispatches nothing is indistinguishable from "
                        "one left off"
                    ),
                }
            )

    # NOT `now=now`: `stale_runners` derives a process start from `now - etime` and compares it to
    # the mirror's real mtime, so an injected review clock (the selftest uses 2023) puts every start
    # before every mirror write and reports every live process stale. Two real-world quantities must
    # be compared on the real clock; `now` here dates the REVIEW, not the process table.
    runners = stale_runners()
    return {
        "generated_at": now,
        "review_days": REVIEW_DAYS,
        "held_off": due,
        "on_but_idle": quiet,
        "unconditioned": [d["flag"] for d in due if not d["has_criterion"]],
        # Reported here rather than in a second auditor, per the standing rule: a stale runner
        # is a switch-shaped problem -- something was decided and the decision never landed.
        "stale_runners": runners,
        # Same rule, one step earlier in the same chain: a stale RUNNER is code the sync could not
        # reach, and a drifted MIRROR is code the sync did not carry. Both are decisions that never
        # landed, so both belong in this sweep rather than in a second auditor.
        "mirror_drift": mirror_drift(),
        "fleet_gates": fleet_gates(now=now),
        "raise_count": len(due) + len(quiet),
    }


def raise_questions(rep: dict, *, dry_run: bool = True) -> dict:
    """Record ONE non-blocking, auto-expiring owner question per due switch."""
    raised: list = []
    deduped: list = []
    errors: list = []
    for row in rep["held_off"] + rep["on_but_idle"]:
        flag, cap_id, state = row["flag"], row["capability"], row["state"]
        if state == "off":
            question = (
                f"{flag} is off and {cap_id}'s switch-on precondition is recorded. "
                f"Turn it on, or restate the criterion?"
            )
            default = "keep it off; re-ask in a week"
        else:
            question = (
                f"{flag} is ON but {cap_id} has recorded no invocation in "
                f"{REVIEW_DAYS}d. Keep it on, turn it off, or fix what feeds it?"
            )
            default = "keep the current position; re-ask in a week"
        if dry_run:
            raised.append(flag)
            continue
        try:
            import feedback

            res = feedback.record_owner_question(
                question,
                default,
                repo="orchestrator",
                target=f"switch:{flag}",
                options=["on", "off", "investigate"],
                expires_days=QUESTION_EXPIRY_DAYS,
            )
            (deduped if res.get("deduped") else raised).append(flag)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{flag}: {str(exc)[:100]}")
    return {"raised": raised, "already_open": deduped, "errors": errors, "dry_run": dry_run}


def format_report(rep: dict) -> str:
    lines = [
        "# Switch review — held switches must be revisited, not forgotten",
        "",
        f"  review window: {rep['review_days']}d",
        f"  due for a decision: {rep['raise_count']}",
        "",
    ]
    if rep["held_off"]:
        lines += ["## Held OFF", ""]
        for row in rep["held_off"]:
            lines.append(f"  {row['flag']}  ({row['capability']})")
            lines.append(f"      {row['reason']}")
            if row.get("criterion"):
                lines.append(f"      switch on when: {row['criterion'][:150]}")
            lines.append("")
    if rep["on_but_idle"]:
        lines += ["## ON but not triggering — the range-lane failure mode", ""]
        for row in rep["on_but_idle"]:
            lines.append(f"  {row['flag']}  ({row['capability']})  idle={row['idle_days']}d")
            lines.append(f"      {row['reason']}")
            lines.append("")
    if rep["unconditioned"]:
        lines += [
            "## Held with NO recorded criterion (a documentation gap, fix in "
            "SWITCH_ON_CRITERIA)",
            "",
        ]
        lines += [f"  {f}" for f in rep["unconditioned"]] + [""]
    if rep.get("stale_runners"):
        lines += [
            "## Running code older than the mirror — a sync does not reach a live process",
            "",
        ]
        for row in rep["stale_runners"]:
            lines.append(
                f"  pid {row['pid']}  up {row['age_hours']}h  " f"stale by {row['stale_by_hours']}h"
            )
            lines.append(f"      {row['command']}")
            lines.append("")
        lines += [
            "  FYI only: restart these to pick up the current mirror. Nothing is killed "
            "automatically -- a live process may be serving a session.",
            "",
        ]
    drift = rep.get("mirror_drift") or {}
    if drift.get("status") != "ok":
        lines += ["## The deployed mirror does not carry what the checkout has", ""]
        if drift.get("status") == "unknown":
            lines += [f"  NOT MEASURED — {drift.get('reason', 'no reason recorded')}", ""]
        else:
            absent = drift.get("absent_from_mirror") or []
            differing = drift.get("differing") or []
            if absent:
                lines.append(f"  absent from the mirror ({len(absent)}): {', '.join(absent[:6])}")
            if differing:
                lines.append(f"  differing ({len(differing)}): {', '.join(differing[:6])}")
            if drift.get("checkout_behind"):
                lines.append(
                    f"  the checkout is {drift['checkout_behind']} commit(s) behind upstream — "
                    "syncing from it would deploy stale code AND report success"
                )
            lines += [
                "",
                "  FYI only: run `orch-sync-mirror.sh` after bringing the checkout up to date. "
                "Nothing is deployed automatically -- the manual sync is the circuit breaker "
                "between an agent's change and the dispatcher that dispatches agents.",
                "",
            ]
    fleet = rep.get("fleet_gates") or {}
    if fleet:
        lines += ["## Fleet template-delivery gates (Maint 68 promote + sync canaries)", ""]
        promote = fleet.get("promote") or {}
        if promote.get("measurement", "").startswith("unmeasured"):
            lines.append(f"  Maint 68 promote: {promote['measurement']}")
        elif promote.get("days_since_success") is None:
            lines.append(f"  Maint 68 promote: {promote.get('measurement', 'unmeasured')}")
        else:
            lines.append(
                f"  Maint 68 promote: last success {promote['days_since_success']}d ago "
                f"({promote.get('display_title', 'n/a')})"
            )
        canaries = fleet.get("canaries") or {}
        if (
            canaries.get("measurement", "").startswith("unmeasured")
            and canaries.get("open_count") is None
        ):
            lines.append(f"  sync canaries: {canaries['measurement']}")
        else:
            lines.append(
                f"  sync canaries: open={canaries.get('open_count')} "
                f"drainable={canaries.get('drainable_count')} "
                f"(repos checked={canaries.get('repos_checked', 0)})"
            )
            for row in canaries.get("open") or []:
                unresolved = row.get("unresolved_threads")
                unresolved_text = "unmeasured" if unresolved is None else str(unresolved)
                lines.append(
                    f"    {row.get('repo')}#{row.get('number')} "
                    f"{row.get('branch')} age={row.get('age_days')}d "
                    f"checks_green={row.get('checks_green')} "
                    f"unresolved_threads={unresolved_text}"
                )
        continuation = fleet.get("maint_82_continuation") or {}
        if continuation.get("measurement", "").startswith("unmeasured"):
            lines.append(f"  Maint 82 continuation: {continuation['measurement']}")
        elif continuation.get("hours_since_dispatch") is not None:
            lines.append(
                f"  Maint 82 continuation: last dispatch {continuation['hours_since_dispatch']}h ago"
            )
        if fleet.get("suspect"):
            lines.append(f"  SUSPECT — {fleet.get('suspect_reason')}")
            if fleet.get("clear_paths"):
                lines.append(f"      {fleet['clear_paths']}")
        lines.append("")
    if (
        not rep["raise_count"]
        and not rep.get("stale_runners")
        and (rep.get("mirror_drift") or {}).get("status") == "ok"
        and not (rep.get("fleet_gates") or {}).get("suspect")
    ):
        lines += ["  Nothing due. Every switch is either triggering or has a fresh decision.", ""]
    return "\n".join(lines)


def _selftest_stale_runners() -> None:
    """A sync does not reach a process that has already imported the code."""
    import tempfile

    # `ps` etime, all four shapes, including the one that must refuse rather than guess.
    assert _etime_seconds("07:19:52") == 26392
    assert _etime_seconds("04:30") == 270
    assert _etime_seconds("1-02:03:04") == 93784
    assert _etime_seconds("bogus") is None

    # An empty mirror yields nothing: no mtime means no claim, never "everything is stale".
    with tempfile.TemporaryDirectory(prefix="switch-review-mirror-") as td:
        assert stale_runners(mirror=Path(td)) == []

    # DETERMINISTIC CLASSIFICATION, exercised with no dependence on the real process table. Until
    # 2026-08-23 the only coverage of the compare-and-classify path needed a live process running
    # mirror code, so on any CI runner this selftest returned early and the logic below could
    # regress unseen -- the normal case going unchecked, which is the exact hole verify.py exists
    # to close. A fabricated `ps` line plus a controlled mirror mtime pins both directions.
    with tempfile.TemporaryDirectory(prefix="switch-review-fake-") as td:
        fake_mirror = Path(td)
        stamp = fake_mirror / "dispatcher.py"
        stamp.write_text("# mirror code\n", encoding="utf-8")
        mtime = 1_800_000_000.0
        os.utime(stamp, (mtime, mtime))
        real_run = subprocess.run

        def fake_ps(*_a, **_k):
            class R:
                stdout = f"  4242    07:19:52 python3 {fake_mirror}/dispatcher.py --tick\n"

            return R()

        subprocess.run = fake_ps
        try:
            age = 7 * 3600 + 19 * 60 + 52  # the etime above, in seconds
            # STALE: the process started one hour BEFORE the mirror was written.
            rows = stale_runners(now=mtime + age - 3600, mirror=fake_mirror)
            assert len(rows) == 1, rows
            assert rows[0]["pid"] == "4242", rows
            assert rows[0]["stale_by_hours"] == 1.0, rows
            assert rows[0]["reason"], "a stale runner must say why it is stale"
            # NOT STALE: the same process started one hour AFTER the mirror was written.
            assert stale_runners(now=mtime + age + 3600, mirror=fake_mirror) == []

            # An unparseable etime must be skipped, never guessed at.
            def fake_ps_bogus(*_a, **_k):
                class R:
                    stdout = f"  4242    bogus python3 {fake_mirror}/dispatcher.py\n"

                return R()

            subprocess.run = fake_ps_bogus
            assert stale_runners(now=mtime + age, mirror=fake_mirror) == []
        finally:
            subprocess.run = real_run

    live = stale_runners()
    if not live:
        # Prerequisite absent (nothing is running from the mirror), NAMED rather than silently
        # passing -- the comparison below needs a real process to compare against.
        print(
            "switch_review: stale-runner comparison skipped (no process running mirror code)",
            file=sys.stderr,
        )
        return
    # THE COMPARISON, exercised in both directions against the real process table: shift `now`
    # forward far enough that every process appears to have started AFTER the mirror was written,
    # and nothing may be reported stale. Without this the check could report every process forever
    # and still look correct.
    assert (
        stale_runners(now=time.time() + 20 * 365 * 86400) == []
    ), "a process started after the mirror was written is not stale"
    for row in live:
        assert row["stale_by_hours"] >= 0 and row["pid"].isdigit(), row
        assert row["reason"], "a stale runner must say why it is stale"


def _selftest_fleet_gates() -> None:
    """Template-delivery chain latched when promote and canaries both exceed the horizon."""
    now = 1_700_000_000
    promote_ts = datetime.fromtimestamp(now - 8 * 86400, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    canary_ts = datetime.fromtimestamp(now - 8 * 86400, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cont_ts = datetime.fromtimestamp(now - 12 * 3600, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    maint68_runs = json.dumps(
        [
            {
                "databaseId": 68001,
                "displayTitle": "Maint 68 promote consumers",
                "conclusion": "success",
                "createdAt": promote_ts,
                "status": "completed",
            }
        ]
    )
    pr_list = json.dumps(
        [
            {
                "number": 42,
                "headRefName": "sync/workflows-candidate",
                "createdAt": canary_ts,
                "statusCheckRollup": {"state": "FAILURE", "contexts": []},
                "isDraft": False,
            }
        ]
    )
    review_threads = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"nodes": [{"isResolved": False}]},
                    }
                }
            }
        }
    )
    maint82_runs = json.dumps(
        [
            {
                "databaseId": 82001,
                "conclusion": "success",
                "createdAt": cont_ts,
                "status": "completed",
            }
        ]
    )
    maint82_log = f"setup\n{CONTINUATION_LOG_MARKER} candidate-lane\n"

    def fake_gh(args, *, timeout_s=30):
        if args[:2] == ["run", "list"] and MAINT_68_WORKFLOW in args:
            return True, maint68_runs, ""
        if args[:2] == ["run", "list"] and MAINT_82_WORKFLOW in args:
            return True, maint82_runs, ""
        if args[:2] == ["pr", "list"]:
            return True, pr_list, ""
        if args[:2] == ["api", "graphql"] and "reviewThreads" in " ".join(args):
            return True, review_threads, ""
        if args[:2] == ["run", "view"] and "--log" in args:
            return True, maint82_log, ""
        return False, "", f"unmeasured: unexpected gh call {args!r}"

    def repos():
        return ["stranske/Example"]

    rep = fleet_gates(now=now, gh_fn=fake_gh, repos_fn=repos)
    assert rep["suspect"], rep
    assert "promote stale 8.0d" in rep["suspect_reason"], rep["suspect_reason"]
    assert "open canaries 1 (drainable 0)" in rep["suspect_reason"], rep["suspect_reason"]
    assert "clears when" in rep["clear_paths"], rep

    text = format_report(
        {
            "generated_at": now,
            "review_days": REVIEW_DAYS,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "stale_runners": [],
            "mirror_drift": {"status": "ok"},
            "fleet_gates": rep,
            "raise_count": 0,
        }
    )
    assert "SUSPECT" in text, text
    assert "drainable=0" in text, text
    assert "Nothing due" not in text, text

    def fail_gh(args, *, timeout_s=30):
        return False, "", "unmeasured: auth required"

    bad = fleet_gates(now=now, gh_fn=fail_gh, repos_fn=repos)
    assert bad["promote"]["measurement"].startswith("unmeasured"), bad
    assert bad["canaries"]["open_count"] is None, bad
    assert bad["canaries"]["drainable_count"] is None, bad

    young_ts = datetime.fromtimestamp(now - 2 * 86400, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    young_pr_list = json.dumps(
        [
            {
                "number": 7,
                "headRefName": "sync/workflows-delivery",
                "createdAt": young_ts,
                "statusCheckRollup": {"state": "SUCCESS", "contexts": []},
                "isDraft": False,
            }
        ]
    )

    def young_gh(args, *, timeout_s=30):
        if args[:2] == ["run", "list"] and MAINT_68_WORKFLOW in args:
            return True, maint68_runs, ""
        if args[:2] == ["run", "list"] and MAINT_82_WORKFLOW in args:
            return True, maint82_runs, ""
        if args[:2] == ["pr", "list"]:
            return True, young_pr_list, ""
        if args[:2] == ["api", "graphql"] and "reviewThreads" in " ".join(args):
            return (
                True,
                json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {"reviewThreads": {"nodes": []}},
                            }
                        }
                    }
                ),
                "",
            )
        if args[:2] == ["run", "view"] and "--log" in args:
            return True, maint82_log, ""
        return False, "", f"unmeasured: unexpected gh call {args!r}"

    young = fleet_gates(now=now, gh_fn=young_gh, repos_fn=repos)
    assert not young[
        "suspect"
    ], "SUSPECT must require a canary older than the horizon, not merely an open canary"


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    _selftest_stale_runners()
    _selftest_fleet_gates()
    now = 1_700_000_000
    with tempfile.TemporaryDirectory(prefix="switch-review-") as td:
        reg = Path(td) / "capabilities.json"
        caps = {}
        for cap_id in SWITCH_CAPABILITY.values():
            rec = capabilities._blank_capability(cap_id)
            rec["status"] = "generated"
            caps[cap_id] = rec
        capabilities.save(caps, reg)

        # ALL OFF -> each with a recorded criterion is raised as a pending decision.
        rep = review(now=now, env={}, path=reg)
        flags = {r["flag"] for r in rep["held_off"]}
        assert flags == set(SWITCH_CAPABILITY), flags
        assert not rep["on_but_idle"], rep["on_but_idle"]
        # Switches WITH a criterion must be distinguishable from those without. Every real switch
        # now HAS one (that gap was closed 2026-08-20), so the mechanism is tested with a synthetic
        # flag — otherwise this assertion would quietly go vacuous the moment a gap is fixed.
        assert not rep["unconditioned"], f"a real switch lost its criterion: {rep['unconditioned']}"
        saved_map = dict(SWITCH_CAPABILITY)
        try:
            SWITCH_CAPABILITY["ORCH_SYNTHETIC_NO_CRITERION"] = "range-lane-rollout"
            gap = review(now=now, env={}, path=reg)
            assert gap["unconditioned"] == ["ORCH_SYNTHETIC_NO_CRITERION"], gap["unconditioned"]
            assert "NO recorded criterion" in format_report(gap)
        finally:
            SWITCH_CAPABILITY.clear()
            SWITCH_CAPABILITY.update(saved_map)
        assert "ORCH_RANGE_LANE_ROLLOUT" not in rep["unconditioned"], rep["unconditioned"]

        # THE RANGE-LANE CASE: ON, but the capability recorded nothing -> re-raised.
        env = {"ORCH_RANGE_LANE_ROLLOUT": "1"}
        rep2 = review(now=now, env=env, path=reg)
        idle = {r["flag"] for r in rep2["on_but_idle"]}
        assert idle == {"ORCH_RANGE_LANE_ROLLOUT"}, idle
        assert "ORCH_RANGE_LANE_ROLLOUT" not in {r["flag"] for r in rep2["held_off"]}
        text = format_report(rep2)
        assert "ON but not triggering" in text and "range-lane failure mode" in text

        # ON and RECENTLY triggering -> silent, no question.
        caps["range-lane-rollout"]["last_invocation"] = now - 2 * 86400
        capabilities.save(caps, reg)
        rep3 = review(now=now, env=env, path=reg)
        assert not rep3["on_but_idle"], rep3["on_but_idle"]

        # ON but last invocation just past the window -> raised again. This is the component that
        # makes "turned it on and forgot" impossible.
        caps["range-lane-rollout"]["last_invocation"] = now - (REVIEW_DAYS + 1) * 86400
        capabilities.save(caps, reg)
        rep4 = review(now=now, env=env, path=reg)
        assert {r["flag"] for r in rep4["on_but_idle"]} == {"ORCH_RANGE_LANE_ROLLOUT"}, rep4
        assert rep4["on_but_idle"][0]["idle_days"] == REVIEW_DAYS + 1

        # "0" counts as off, not on.
        rep5 = review(now=now, env={"ORCH_RANGE_LANE_ROLLOUT": "0"}, path=reg)
        assert "ORCH_RANGE_LANE_ROLLOUT" in {r["flag"] for r in rep5["held_off"]}

        # Dry run never writes, and the default is always conservative.
        # raise_questions covers BOTH lists, so the ON-but-idle switch appears alongside the
        # held-off ones; what matters is that the idle one is not dropped.
        out = raise_questions(rep4, dry_run=True)
        assert out["dry_run"], out
        assert "ORCH_RANGE_LANE_ROLLOUT" in out["raised"], out
        assert len(out["raised"]) == len(rep4["held_off"]) + len(rep4["on_but_idle"]), out

    print(
        "switch_review.py selftest: OK (held-off raised, ON-but-idle re-raised after the window, "
        "recently-triggering stays silent, '0' is off, dry-run inert, fleet_gates SUSPECT rule)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--raise",
        dest="do_raise",
        action="store_true",
        help="record owner questions (requires ORCH_SWITCH_REVIEW=1)",
    )
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = review()
    if args.do_raise:
        if not APPLY_ENABLED:
            print("refusing to raise: set ORCH_SWITCH_REVIEW=1", file=sys.stderr)
            return 2
        rep["questions"] = raise_questions(rep, dry_run=False)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
