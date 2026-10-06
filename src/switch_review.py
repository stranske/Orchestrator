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
     invocation in that window, the question comes back. A consult trial is not an invocation of
     the switch's capability for this purpose: a trial from any session moved the field this read,
     so the row counts non-trial invocation events and names every trial it left out. Where the
     switch's owner can say what would drain its gate (`SWITCH_DRAIN`), the row carries that count
     beside the idleness, because "ON, idle" reads as patience while "ON, deficits 5/3, drainable
     0" is a deadlock.

NON-BLOCKING BY CONSTRUCTION. Everything goes through `feedback.owner_questions`: deduped per scope,
auto-ratifying at expiry to a stated default, so an unanswered question can never accumulate into a
backlog. The default is always the conservative one — keep the current switch position — because
flipping a safety switch on silence is precisely what must not happen.

SWITCH VALUES ARE READ AS THE TICK SEES THEM, and each row says where its value came from. Outside
the tick a switch orchestrate.sh exports ON by default is unset, so reading this process's
environment alone listed ORCH_REDIRECT_APPLY_BOOTSTRAP as "held off, a decision waiting to be made"
while every tick had it armed. So the CLI reads each switch from orchestrate.sh's prologue, executed
with this process's environment inherited (`env_as_the_tick_sees_it`), and the tick, whose
environment already IS the tick's, says so with `--env process` rather than executing its own
prologue a second time.

    python3 switch_review.py                # what is due for review
    python3 switch_review.py --json
    python3 switch_review.py --env process  # this process's environment alone (what the tick runs)
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

# How far either side of today the expiry notice looks (see `gate_expiry`). DERIVED from the review
# cadence rather than written as a second literal: the step runs at most every REVIEW_DAYS, so two of
# them put every expiry in at least one review before it lands, and one after, even with a run missed.
GATE_EXPIRY_NOTICE_DAYS = 2 * REVIEW_DAYS

# A capacity-shed marker placed by hand states no horizon, so it is named once it has held its seat
# for the latched-gate default of 14 days, which is also two reviews: it has been seen before.
SHED_MANUAL_HORIZON_DAYS = 2 * REVIEW_DAYS
# An expired marker leaves at the first capacity read after its expiry, and the tick reads capacity
# on every run, before this step (orchestrate.sh). One still on disk a day later is not leaving.
SHED_EXPIRED_GRACE_S = 86400

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
    # ADDED 2026-10-02. orchestrate.sh has exported this =1 by default since 2026-08-21 and its
    # switch-on criterion sat in `capability_recurrence_check.SWITCH_ON_CRITERIA`, but it was never
    # mapped HERE, so this review never examined it while it authorised nothing for 42 days: 759
    # metered judgements, every one `wait`. Its `invocation` heartbeat fires only on an authorised
    # apply (`redirect_apply.apply_one`), and the idle rule leaves consult trials out, so
    # ON-but-idle means exactly that. Its row carries the gate's drain (`SWITCH_DRAIN`). The two
    # tables must name the same switches; tests/test_switch_review_bootstrap_drain.py fails on a
    # flag that is in one and not the other.
    "ORCH_REDIRECT_APPLY_BOOTSTRAP": "redirect-apply-bootstrap",
}


# The evidence the ON-but-idle rule counts, declared in `review()`'s finding population. A rule that
# counts different evidence names different switches idle, and that EDIT must re-baseline the tick's
# grader rather than be scored as the sweep finding something (`capabilities.FINDING_POPULATION_KEY`).
IDLE_EVIDENCE = "non-trial invocation events"


def _age_days(stamp: int, now: int) -> float | None:
    return round((now - stamp) / 86400, 1) if stamp else None


def _invocation_evidence(cap_id: str, *, now: int, path=None) -> dict:
    """When this capability last did something, consult trials left out, and what was left out.

    A consult trial (`capability_propensity.record_trigger`) writes an `invocation` through the
    ungated `capabilities.heartbeat`, from any session. Read through the `last_invocation` field,
    one rail-exercise round on a switch-mapped capability made an idle ON switch read active for
    REVIEW_DAYS and hid its drain. A trial triggers an advised candidate; it is not the switch's own
    path running. So only the row's non-trial invocation EVENTS count
    (`capabilities.split_invocations`, the split the firing monitor reads), and the field counts for
    nothing on its own: a causal reconciliation sets it with no event, from influence edges the
    outcome bridge draws from lane consult trials' verdicts, which is a trial through a second door.

    `last` is the newest counted invocation, 0 when none ever counted. What was left out and is newer
    than it is reported, so "idle, one trial ignored" never reads like plain idleness:
    `trials_excluded`, those trials (every trial, when nothing counted), with the newest one's age in
    `newest_trial_days`; and `no_event_days`, the age of a `last_invocation` no trial explains, which
    moved with no invocation event. Each age is None when there is nothing to report.

    NO LEDGER ROW IS NOT "NEVER INVOKED". With no row for the capability on this machine (a fresh
    clone, CI's bootstrapped ledger) nothing it did can be read, so `trials_excluded` is None,
    unmeasured, never a zero, and the switch is still raised as idle, toward the alarm, saying why.
    """
    # `load_declared`, not `load`: this is a REPORT, and its cadence row promises "writes require
    # ORCH_SWITCH_REVIEW=1; report-only otherwise". The writing loader creates a missing ledger,
    # seeds declared gate rows, reconciles declarations and expires rows, and writes the result into
    # the shared ledger on every review, flag or no flag. `load_declared` writes nothing; what is
    # read here is measured state, which reconciliation never touches.
    cap = capabilities.load_declared(path or capabilities.REG).get(cap_id)
    if cap is None:
        return {
            "last": 0,
            "trials_excluded": None,
            "newest_trial_days": None,
            "no_event_days": None,
        }
    split = capabilities.split_invocations(cap)
    last = max(split["other"], default=0)
    excluded = [stamp for stamp in split["trial"] if stamp > last]
    field = int(cap.get("last_invocation") or 0)
    eventless = field > last and field not in split["trial"]
    return {
        "last": last,
        "trials_excluded": len(excluded),
        "newest_trial_days": _age_days(max(excluded, default=0), now),
        "no_event_days": _age_days(field, now) if eventless else None,
    }


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


def _exploration_gate() -> dict:
    """Keep an unreadable direct-mode evidence gate visible in the recurring review."""
    try:
        import exploration_review

        result = exploration_review.build_report(trials=20)
        status = result["status"]
        error = result.get("evidence_error")
        evidence = result.get("recorded_exploration_evidence") or {}
    except Exception as exc:  # noqa: BLE001
        status = "exploration_report_error"
        error = f"{type(exc).__name__}: {exc}"
        evidence = {}
    suspect = status in {"direct_mode_evidence_unreadable", "exploration_report_error"}
    return {
        "status": status,
        "suspect": suspect,
        "evidence_error": error,
        "arms": evidence.get("mode_counts", []),
        "window_days": evidence.get("window_days"),
        "drainable": (
            "repair the Brain read"
            if status == "direct_mode_evidence_unreadable"
            else (
                "repair exploration report generation"
                if status == "exploration_report_error"
                else None
            )
        ),
    }


def _expiry_row(cap_id: str, cap: dict, *, now: int) -> dict:
    blocker = capabilities.renewal_blocker(cap, now=now)
    usage = capabilities.usage_rate(cap, now=now)
    last = cap.get("last_invocation")
    return {
        "capability_id": cap_id,
        "status": cap.get("status"),
        "gated": bool(cap.get("gate_reason")),
        "expiry": int(cap["expiry"]),
        "expires_on": capabilities.utc_date(int(cap["expiry"])),
        # The row's own activity, so whoever decides to renew or let it go has the evidence at hand.
        "recent_invocations": usage["invocations"],
        "recent_window_days": usage["window_days"],
        "last_invocation_on": capabilities.utc_date(int(last)) if last else None,
        "renewals": sum(
            1
            for event in cap.get("event_history") or []
            if event.get("type") == capabilities.RENEWAL_EVENT
        ),
        "renewable": blocker is None,
        "blocker": blocker,
        "renew": capabilities.renew_command(cap_id),
    }


def gate_expiry(*, now: int | None = None, path=None) -> dict:
    """FYI: the ledger rows the expiry timeout retires soon, and the ones it has just retired.

    `capabilities` retires every live row whose `expiry` has passed. That retirement is the intended
    safe default, and the expiry is meant to be the moment someone asks whether the row still earns
    its place. But nothing announced the moment, and until `capabilities.renew` there was no answer
    but retirement, so a row's window closed by timeout: the decision-that-never-happened shape this
    sweep exists for. So this names each live row within GATE_EXPIRY_NOTICE_DAYS of its expiry, and
    each row its expiry retired within as many days, with the command that holds it.

    FYI ONLY. It renews nothing and raises no owner question. Unread, it changes nothing: the row
    retires at its expiry exactly as before. Every row leaves the notice on its own once the window
    passes, so nothing can pile up. `renewable` counts the rows `capabilities.renewal_blocker`
    accepts, the predicate `renew` itself enforces, so the notice never offers a drain that renew
    would refuse.
    """
    now = int(now if now is not None else time.time())
    window = GATE_EXPIRY_NOTICE_DAYS * 86400
    ledger_path = Path(path or capabilities.REG)
    report: dict = {
        "status": "ok",
        "window_days": GATE_EXPIRY_NOTICE_DAYS,
        "ttl_days": capabilities.GATED_TTL_DAYS,
        "rows_with_expiry": 0,
        "expiring": [],
        "lapsed": [],
        "renewable": 0,
        "next_expiry": None,
    }
    # UNMEASURED IS NOT ZERO. A ledger that is missing or unreadable must not print the drained
    # line: "nothing expires" is good news, and "could not look" is not.
    if not ledger_path.exists():
        return {
            **report,
            "status": "unknown",
            "measurement": f"unmeasured: no capability ledger at {ledger_path}",
        }
    try:
        later = []
        for cap_id, cap in sorted(capabilities.load_declared(ledger_path).items()):
            if not isinstance(cap, dict):
                continue
            if cap.get("status") not in capabilities.NOT_LIVE_STATES:
                if cap.get("expiry") is None:
                    continue
                report["rows_with_expiry"] += 1
                if int(cap["expiry"]) - now > window:
                    later.append((int(cap["expiry"]), cap_id))
                    continue
                row = _expiry_row(cap_id, cap, now=now)
                row["days_left"] = round((row["expiry"] - now) / 86400, 1)
                report["expiring"].append(row)
                continue
            retirement = capabilities.expiry_retirement(cap)
            if retirement is None or now - int(retirement.get("timestamp") or 0) > window:
                continue
            row = _expiry_row(cap_id, cap, now=now)
            row["retired_on"] = capabilities.utc_date(int(retirement.get("timestamp") or 0))
            row["status_before"] = retirement.get("from")
            report["lapsed"].append(row)
    except Exception as exc:  # noqa: BLE001
        return {
            **report,
            "status": "unknown",
            "measurement": f"unmeasured: capability ledger unreadable ({type(exc).__name__}: {exc})",
            "expiring": [],
            "lapsed": [],
            "rows_with_expiry": 0,
        }
    report["expiring"].sort(key=lambda row: (row["expiry"], row["capability_id"]))
    report["renewable"] = sum(
        1 for row in report["expiring"] + report["lapsed"] if row["renewable"]
    )
    if later:
        expiry, cap_id = min(later)
        report["next_expiry"] = {
            "capability_id": cap_id,
            "expires_on": capabilities.utc_date(expiry),
            "days_left": round((expiry - now) / 86400, 1),
        }
    return report


def capacity_shed(*, now: float | None = None, shed_dir: Path | None = None) -> dict:
    """FYI: every capacity-shed marker, what holds its seat and what clears it. Removes nothing.

    The shed gate drains itself: `capacity._shed` removes an expired marker on read, and the tick
    reads capacity on every run. A marker with no expiry has no drain but its removal. That is the
    documented way to stand a seat down by hand (`touch capacity-shed/<agent>`), and nothing ever
    said such a marker was still there: a held switch whose release depends on somebody remembering.

    Each marker is read by `capacity.shed_marker`, the reading the gate itself makes, and described
    by `capacity.shed_reason`, the sentence its seat prints. SUSPECT, by the latched-gate predicate:
    a manual or unreadable marker held past SHED_MANUAL_HORIZON_DAYS, or an expired marker still on
    disk SHED_EXPIRED_GRACE_S after its expiry, so its drain is not running. A marker for a name
    capacity reads as no seat holds nothing, and is named `inert`.

    `now` is the real clock, never the review's: expiries and file ages are real-world times, the
    same reason `stale_runners` takes no review clock.
    """
    import capacity

    now = time.time() if now is None else now
    directory = Path(shed_dir) if shed_dir is not None else capacity.SHED_DIR
    report: dict = {
        "status": "ok",
        "dir": str(directory),
        "horizon_days": SHED_MANUAL_HORIZON_DAYS,
        "markers": [],
        "suspect": 0,
    }
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        entries = []  # no marker was ever written here, and capacity reads that as no seat shed
    except OSError as exc:
        return {
            **report,
            "status": "unknown",
            "measurement": f"unmeasured: {directory} could not be listed "
            f"({type(exc).__name__}: {exc})",
        }
    for entry in entries:
        # Not a write in progress (`rate_incidents.ensure_shed` writes `.<agent>.*`, then renames),
        # nor a subdirectory such as `archive/`, unless capacity would read it as a seat's marker.
        if entry.name.startswith(".") or (
            entry.name not in capacity.AGENTS and not entry.is_file()
        ):
            continue
        marker = capacity.shed_marker(entry.name, now=now, shed_dir=directory)
        if marker is not None:  # None: removed between the listing and the read
            report["markers"].append(_shed_row(marker, entry, now=now))
    report["suspect"] = sum(1 for row in report["markers"] if row["suspect"] is not None)
    return report


def _shed_row(marker: dict, entry: Path, *, now: float) -> dict:
    import capacity

    since = marker["created_at"]
    if since is None:
        try:
            since = entry.stat().st_mtime
        except OSError:
            since = None
    row = {
        "agent": marker["agent"],
        "state": marker["state"],
        "read_by_capacity": marker["agent"] in capacity.AGENTS,
        "expires_at": marker["expires_at"],
        "incident_id": marker["incident_id"],
        "category": marker["category"],
        "age_days": None if since is None else round((now - since) / 86400, 1),
        "reason": capacity.shed_reason(marker),
        "suspect": None,
    }
    age = row["age_days"]
    if not row["read_by_capacity"]:
        return row
    if marker["state"] in ("manual", "unreadable") and age is not None:
        if age > SHED_MANUAL_HORIZON_DAYS:
            row["suspect"] = (
                f"held by hand for {age}d, past the {SHED_MANUAL_HORIZON_DAYS}d horizon; nothing "
                "clears it but removing it"
            )
    elif marker["state"] == "expired" and now - marker["expires_at"] > SHED_EXPIRED_GRACE_S:
        row["suspect"] = (
            f"expired {capacity.utc_stamp(marker['expires_at'])} and still on disk: a capacity "
            "read removes an expired marker, so none has run since or the removal fails"
        )
    return row


# The gate fields an ON-but-idle bootstrap row carries: what is measured and what is still needed.
_BOOTSTRAP_GATE_KEYS = (
    "synced_role_outcomes",
    "synced_needed",
    "linked_disagreements",
    "disagreements_needed",
    "bootstrap_needed",
)


def _redirect_bootstrap_inputs() -> dict[str, Path]:
    """The files the bootstrap's drain is read from, each resolved by the module that owns it.

    The three `redirect_apply.status` reads: the redirect corpus (the gate's deficits, and what was
    already judged or applied), the supervisor's stage-2 plan (its CURRENT candidates, so the
    drainable population) and its report directory (counted, never judged). Resolved here and passed
    explicitly, so the row names what it read and a test can point all three at a sandbox.
    """
    import keepalive_supervisor
    import redirect_apply
    import redirect_shadow

    return {
        "corpus_path": Path(redirect_shadow.CORPUS_PATH),
        "plan_path": redirect_apply.default_stage2_plan_path(),
        "report_dir": Path(keepalive_supervisor.DEFAULT_STAGE2_REPORT_DIR),
    }


def _redirect_bootstrap_drain(env: Mapping[str, str], *, now: int) -> dict:
    """The bootstrap's Stage-2 deficits beside its drainable count, from `redirect_apply.status`.

    `status()` owns that pair, so nothing is composed here: the deficits are its gate, and the
    drainable count is its free screen of the supervisor's CURRENT candidates, which runs no role and
    spends no offload. Every input is explicit and only read: the files from
    `_redirect_bootstrap_inputs`; the `env` that just read this switch as ON, so status never
    re-resolves the flag by executing the tick's prologue; and `link_preview=False`, which skips its
    one Brain read, because that opens a connection that runs the schema script and commits, and this
    row does not use what it reads.

    `drainable` is an int, and 0 is a measurement; None means the candidate population could not be
    read, and `reason` says why. A read that fails is REPORTED, never raised: the switch is ON and
    idle either way, so its row is raised either way, and only the drain goes unmeasured.
    """
    drain: dict = {"source": "redirect_apply.status", "inputs": {}}
    try:
        import redirect_apply

        inputs = _redirect_bootstrap_inputs()
        drain["inputs"] = {name: str(value) for name, value in inputs.items()}
        out = redirect_apply.status(
            inputs["corpus_path"],
            env=dict(env),
            report_dir=inputs["report_dir"],
            plan_path=inputs["plan_path"],
            now=now,
            link_preview=False,
        )
        population = out.get("drainable_population") or {}
        drain.update(
            gate={key: out["gate"][key] for key in _BOOTSTRAP_GATE_KEYS},
            drainable=out["drainable"],
            current_candidates=out["current_candidates"],
            population=str(population.get("status") or "unknown"),
            reason=str(population.get("reason") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        drain.update(
            gate=None,
            drainable=None,
            current_candidates=None,
            population="unmeasured",
            reason=f"redirect_apply.status raised {type(exc).__name__}: {exc}"[:240],
        )
    drain["summary"] = _bootstrap_drain_summary(drain)
    return drain


def _bootstrap_drain_summary(drain: dict) -> str:
    """The drain in one sentence, which the report and the owner question both print.

    Every state has its own words, because only some of them are good news: a measured zero (the
    deadlock), a positive count (the apply step could act), an unknown population, a finished gate,
    and a read that failed. None and 0 must never print alike.
    """
    gate = drain.get("gate")
    if not gate:
        return f"drain UNMEASURED — {drain.get('reason') or 'no reason recorded'}"
    if not gate.get("bootstrap_needed"):
        return (
            "FINISHED — the Stage-2 deficits are closed (synced_role_outcomes "
            f"{gate.get('synced_role_outcomes')}, linked_disagreements "
            f"{gate.get('linked_disagreements')}), so the bootstrap has disarmed itself and idle "
            "is its end state: the switch can go off"
        )
    needed = (gate.get("synced_needed"), gate.get("disagreements_needed"))
    deficits = (
        f"Stage-2 deficits {needed[0]}/{needed[1]} (needs {needed[0]} more synced_role_outcomes "
        f"and {needed[1]} more linked_disagreements)"
    )
    drainable = drain.get("drainable")
    if drainable is None:
        reason = drain.get("reason") or "the candidate population was not read"
        return f"{deficits}, drainable UNKNOWN — {reason}"
    current = drain.get("current_candidates")
    if drainable == 0:
        return (
            f"{deficits}, drainable 0 of {current} current candidate(s) — nothing current can "
            "drain them: a deadlock, not patience"
        )
    # `--screen` in text mode prints only the candidates that FAIL the screen; the JSON lists every
    # candidate, so it is the one form that names the ones this count is about.
    return (
        f"{deficits}, drainable {drainable} of {current} current candidate(s) — they pass the free "
        "screen and none was authorised; `redirect_apply.py --screen --json` lists them "
        "(passes_screen: true)"
    )


# flag -> the reader of that switch's DRAINABLE quantity, run only for its ON-but-idle row.
#
# "ON, idle" reads as patience; "ON, deficits 5/3, drainable 0" is a deadlock. Only the second is a
# diagnosis, so a switch whose owner can say what would drain its gate carries that count beside the
# idleness: the runtime rule, blocking and drainable quantity in one place. A reader must keep
# `switch_states` as cheap as its docstring promises (local reads only: no gh, no heartbeat, no
# write) and must report a failure rather than raise it.
SWITCH_DRAIN: dict[str, Callable[..., dict]] = {
    "ORCH_REDIRECT_APPLY_BOOTSTRAP": _redirect_bootstrap_drain,
}


def env_as_the_tick_sees_it() -> tuple[dict[str, str], dict[str, str]]:
    """Each reviewed switch's value as THE TICK sees it, and where each value came from.

    For a reader OUTSIDE the tick. orchestrate.sh exports some switches ON by default, so a process
    reading only its own environment sees them unset, and so off, while every tick has them on: an
    interactive run listed ORCH_REDIRECT_APPLY_BOOTSTRAP under "held off, a decision waiting to be
    made" and a re-offer echoed it as `gate_state: off` while the tick had it armed. This resolves
    each mapped switch with `capability_recurrence_check.as_the_tick_sees_it`, the one resolver that
    EXECUTES the prologue, conditionals included, and names every source. A value this process sets
    meets what it would meet in a real tick: kept when non-empty, replaced by the default when empty,
    and overridden where a prologue conditional says so.

    It runs bash, so a CLI entry point calls it and `switch_states()` never does: that read must stay
    cheap, and a library or test caller must never execute orchestrate.sh. The tick does not call it
    either, because its environment already IS the tick's (`main --env process`).

    Returns `(env, sources)`: the values that are set, and one source per mapped switch.
    """
    import capability_recurrence_check as rc

    env: dict[str, str] = {}
    sources: dict[str, str] = {}
    for flag in sorted(SWITCH_CAPABILITY):
        value, sources[flag] = rc.as_the_tick_sees_it(flag)
        if value is not None:
            env[flag] = value
    return env, sources


def switch_states(
    *,
    now: int | None = None,
    env: Mapping[str, str] | None = None,
    path=None,
    sources: Mapping[str, str] | None = None,
) -> dict:
    """The switch rows alone: each `SWITCH_CAPABILITY` flag that is held off or on but idle.

    `review()` is a SWEEP. Beside these rows it runs `fleet_gates` (live `gh` calls: one PR listing
    per consumer repo, one GraphQL review-thread query per open canary), `mirror_drift` (git),
    `stale_runners` (ps), the exploration gate (a Brain read) and a heartbeat. A consumer
    that needs only which flag holds which capability must not pay for any of that, and must not be
    able to reach GitHub: `capability_propensity.declared_facts` called `review()` twice per lookup
    to echo a gate, so one `offer-improvements` pass ran the whole sweep twice for every bound
    capability, and the propensity selftest ran it against real GitHub. This is the part of the
    review such a consumer needs, and it lives HERE so the flag -> capability mapping, and the
    on/off/idle reading of it, still have exactly one owner.

    An ON-but-idle row may also carry its switch's `drain` (`SWITCH_DRAIN`): what could clear the
    gate behind the idle switch, beside what blocks it. It is read only for a switch already ON and
    idle, from local files and read-only, so still no gh, no heartbeat and no Brain; a consumer whose
    environment holds the switch off pays nothing for it.

    Every row carries the switch's `value` (None when unset) and its `value_source`, in
    `capability_recurrence_check.as_the_tick_sees_it`'s vocabulary. `sources` supplies them, as
    `env_as_the_tick_sees_it()` returns them. Without it, a value from a passed `env` is `explicit`,
    and a read of this process's environment is `ambient` where the switch is set and
    `tick-unconsulted` where it is not: the tick may well set it, and nothing here asked. That is
    the label that keeps "unset in this process" from reading as a measured off, and it is all this
    function does about the tick: resolving it runs bash.
    """
    import capability_recurrence_check as rc

    now = int(now if now is not None else time.time())
    explicit = env is not None
    env = os.environ if env is None else env
    window = REVIEW_DAYS * 86400
    due, quiet = [], []

    for flag, cap_id in sorted(SWITCH_CAPABILITY.items()):
        value = env.get(flag)
        on = bool(value) and value != "0"
        if sources is not None and flag in sources:
            source = str(sources[flag])
        elif explicit:
            source = "explicit"
        else:
            source = "ambient" if value is not None else rc.TICK_UNCONSULTED
        criterion = rc.SWITCH_ON_CRITERIA.get(flag)

        if not on:
            # OFF: raise only when a criterion exists, so an unconditioned switch is not nagged
            # about forever. An unconditioned switch is a documentation gap, reported separately.
            if rc.tick_value_unknown(source):
                # UNKNOWN IS NOT OFF. Off is this process's reading, and the tick's could differ.
                reason = (
                    "reads OFF in this process only: the tick's value is UNKNOWN (its "
                    "value_source says why), so whether the tick holds it off is not known"
                )
            elif criterion:
                reason = (
                    "held off; a machine-checkable precondition is recorded, so this is a "
                    "decision waiting to be made"
                )
            else:
                reason = "held off with NO recorded switch-on criterion"
            due.append(
                {
                    "flag": flag,
                    "capability": cap_id,
                    "state": "off",
                    "value": value,
                    "value_source": source,
                    "criterion": criterion,
                    "reason": reason,
                    "has_criterion": bool(criterion),
                }
            )
            continue

        # ON but silent for the whole window: enabling it changed nothing observable. A consult
        # trial is not the switch's capability doing anything (`_invocation_evidence`).
        evidence = _invocation_evidence(cap_id, now=now, path=path)
        last = evidence["last"]
        if last == 0 or (now - last) > window:
            row: dict = {
                "flag": flag,
                "capability": cap_id,
                "state": "on",
                "value": value,
                "value_source": source,
                "idle_days": _age_days(last, now),
                "trials_excluded": evidence["trials_excluded"],
                "newest_trial_days": evidence["newest_trial_days"],
                "no_event_days": evidence["no_event_days"],
                "reason": (
                    f"ON but {cap_id} recorded no invocation outside consult trials in the last "
                    f"{REVIEW_DAYS}d — an enabled switch that dispatches nothing is "
                    "indistinguishable from one left off"
                ),
            }
            reader = SWITCH_DRAIN.get(flag)
            if reader is not None:
                row["drain"] = reader(env, now=now)
            quiet.append(row)
    return {"held_off": due, "on_but_idle": quiet}


def firing_regressions(*, now: int, path=None, state_dir: Path | None = None) -> dict:
    """Consume the weekly firing report without running or recording the monitor.

    Re-read the ledger and cadence files: a historical alarm must not claim the
    heartbeat is still silent after a repair. No finding is cleared or acted on.
    """
    import capability_firing_monitor as firing

    root = (
        state_dir
        if state_dir is not None
        else Path(os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex/orchestrator")))
    )
    report_path = root / "capability-firing-monitor.json"
    try:
        report = json.loads(report_path.read_text())
        if not isinstance(report, dict) or not isinstance(report.get("generated_at"), int):
            raise ValueError("missing integer generated_at")
        findings: dict[str, list[str]] = {}
        for kind in ("regressed", "overdue"):
            values = report[kind]
            if not isinstance(values, list):
                raise ValueError(f"{kind} is not a list")
            for row in values:
                cap_id = row["capability_id"]
                if not isinstance(cap_id, str) or not cap_id:
                    raise ValueError("missing capability_id")
                findings.setdefault(cap_id, []).append(kind)
        ledger = capabilities.load_declared(path or capabilities.REG)
        rows = []
        for cap_id, kinds in sorted(findings.items()):
            cap = ledger.get(cap_id)
            last = int(cap.get("last_invocation") or 0) if cap is not None else None
            steps = firing.step_evidence(cap_id, now=now, state_dir=root)
            rows.append(
                {
                    "capability_id": cap_id,
                    "findings": kinds,
                    "last_heartbeat": last,
                    "step_evidence": steps,
                }
            )
        return {
            "status": "ok",
            "report_path": str(report_path),
            "generated_at": report["generated_at"],
            "rows": rows,
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "status": "unknown",
            "report_path": str(report_path),
            "reason": str(exc),
            "rows": [],
        }


def _firing_lines(section: dict) -> list[str]:
    """Render heartbeat and step times independently; absent evidence stays unknown."""

    def date(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else "never"

    lines = ["## Firing regressions and overdue steps", ""]
    if section.get("status") != "ok":
        return lines + [f"  UNKNOWN — {section.get('reason')}", ""]
    lines.append(f"  monitor snapshot: {date(section['generated_at'])}")
    for row in section["rows"]:
        last = row["last_heartbeat"]
        lines.append(f"  {row['capability_id']}: {', '.join(row['findings'])}")
        lines.append(
            f"      ledger last heartbeat: {date(last) if last is not None else 'UNKNOWN'}"
        )
        if last and last > section["generated_at"]:
            lines.append(
                "      heartbeat recorded since monitor snapshot; finding retained as history"
            )
        for step in row["step_evidence"]:
            stamp, age = step["stamp_mtime"], step["stamp_age_seconds"]
            if stamp is None:
                phrase = f"step last ran UNKNOWN ({step['stamp_error'] or 'no stamp declared'})"
            elif age < 0:
                phrase = f"step last ran UNKNOWN (future stamp {date(stamp)})"
            elif last is not None and stamp > (last or 0) and age <= step["stale_after_seconds"]:
                phrase = f"heartbeat silent, step ran {date(stamp)}"
            else:
                phrase = f"step last ran {date(stamp)}"
            lines.append(f"      {step['step']}: {phrase}; stamp age seconds={age}")
            lines.append(
                f"      artifact mtime: {date(step['artifact_mtime']) if step['artifact_mtime'] is not None else 'UNKNOWN'}"
                f"; {step['artifact_path'] or 'no artifact declared'}"
            )
            if step.get("gate"):
                lines.append(f"      enclosing-step evidence only; {step['gate']}")
        if not row["step_evidence"]:
            lines.append("      step last ran UNKNOWN (no declared cadence carrier)")
    return lines + [""]


def adversarial_shape_population(*, state_dir: Path | None = None, now: int | None = None) -> dict:
    """Read the fleet counter's exact population, never infer risk from its top-three classes."""
    import adversarial
    import fleet_shapes

    root = state_dir or fleet_shapes.default_state_dir()
    now = int(time.time()) if now is None else now
    report_path = root / "fleet-shapes.json"
    try:
        payload = json.loads(report_path.read_text())
        section = dict(payload["adversarial_shape"])
        stamp = payload["generated_at"]
        if type(stamp) is not int or not 0 <= now - stamp <= 2 * 86400:
            raise ValueError("fleet-shapes population is stale or future-dated")
        if section.get("rule") != adversarial.shape_rule_id():
            raise ValueError("fleet-shapes population was measured under a different shape rule")
        for key in ("shape_candidates", "label_candidates", "population", "unknown"):
            if type(section.get(key)) is not int or section[key] < 0:
                raise ValueError(f"invalid {key}")
        if section["population"] != payload["counts"]["prs"]:
            raise ValueError("population disagrees with fleet-shapes PR count")
        if any(
            section[k] > section["population"]
            for k in ("shape_candidates", "label_candidates", "unknown")
        ):
            raise ValueError("candidate count exceeds population")
        if section.get("status") not in {"ok", "partial"} or (section["status"] == "ok") != (
            section["unknown"] == 0
        ):
            raise ValueError("measurement status disagrees with unknown count")
        return {**section, "report_path": str(report_path), "generated_at": stamp}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "unknown", "report_path": str(report_path), "reason": str(exc)}


def adversarial_shape_line(section: dict) -> str:
    if section.get("status") not in {"ok", "partial"}:
        return f"adversarial-review: high-stakes candidates unmeasured ({section.get('reason', 'missing population')})"
    line = (
        f"adversarial-review: high-stakes candidates {section['shape_candidates']} of "
        f"{section['population']} merged PRs (shape rule), {section['label_candidates']} by label"
    )
    if section["unknown"]:
        line += f"; {section['unknown']} PRs unmeasured (counts are lower bounds)"
    return line


def review(
    *,
    now: int | None = None,
    env: Mapping[str, str] | None = None,
    path=None,
    sources: Mapping[str, str] | None = None,
    value_chain_inputs: dict | None = None,
) -> dict:
    """Which held-or-idle switches are due for an owner decision, and why."""
    _capability_heartbeat()

    now = int(now if now is not None else time.time())
    states = switch_states(now=now, env=env, path=path, sources=sources)
    due, quiet = states["held_off"], states["on_but_idle"]

    # NOT `now=now`: `stale_runners` derives a process start from `now - etime` and compares it to
    # the mirror's real mtime, so an injected review clock (the selftest uses 2023) puts every start
    # before every mirror write and reports every live process stale. Two real-world quantities must
    # be compared on the real clock; `now` here dates the REVIEW, not the process table.
    runners = stale_runners()
    import runtime_ac_gate

    runtime_ac_shadow = runtime_ac_gate.shadow_summary(now=now)
    value_chain = {"total": 0, "rows": [], "errors": [], "disabled": True}
    if (env if env is not None else os.environ).get("ORCH_VALUE_CHAIN_MONITOR", "1") != "0":
        import value_chain_monitor

        try:
            capabilities.production_heartbeat(
                "value-chain-monitor", "invocation", ref="switch_review.review"
            )
        except Exception as exc:
            print(f"switch_review: value-chain heartbeat failed: {exc}", file=sys.stderr)
        try:
            value_chain = value_chain_monitor.report(
                now=now, path=path, env=env, inputs=value_chain_inputs
            )
        except Exception as exc:  # noqa: BLE001 — preserve the weekly artifact with honest errors
            value_chain = {
                "total": 0,
                "rows": [],
                "errors": [
                    *(value_chain_inputs or {}).get("errors", []),
                    f"Value-chain report failed: {exc}",
                ],
                "disabled": False,
            }
    return {
        "generated_at": now,
        "adversarial_shape": adversarial_shape_population(now=now),
        "value_chain": value_chain,
        "runtime_ac_shadow": runtime_ac_shadow,
        "review_days": REVIEW_DAYS,
        # The switches this report may name, and the evidence its idle rule counts. Declared so that
        # EDITING either is not graded as a finding: `capability_propensity.tick_evidence`
        # re-baselines a report whose declared population changed, where an undeclared one would
        # score a newly mapped switch's first row, or a row a new rule names idle, as the sweep
        # having found something (`capabilities.FINDING_POPULATION_KEY`).
        capabilities.FINDING_POPULATION_KEY: {
            "switches": sorted(SWITCH_CAPABILITY),
            "idle_evidence": IDLE_EVIDENCE,
        },
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
        "firing_regressions": firing_regressions(now=now, path=path),
        "exploration_gate": _exploration_gate(),
        # Same rule again: a row retired by its expiry with nobody having looked is a decision that
        # never happened. FYI only, so it is not counted in `raise_count` and raises no question.
        "gate_expiry": gate_expiry(now=now, path=path),
        # And a seat stood down by hand is a switch held off. FYI only, on the real clock.
        "capacity_shed": capacity_shed(),
        "raise_count": len(due) + len(quiet),
    }


def raise_questions(rep: dict, *, dry_run: bool = True) -> dict:
    """Record ONE non-blocking, auto-expiring owner question per due switch.

    Never for a row whose tick value is UNKNOWN (`capability_recurrence_check.tick_value_unknown`).
    Its "off" is one process's reading, so the question could ask the owner to turn on a switch the
    tick already has on, and its default would ratify that misreading at expiry. Such a row is
    named under `unknown_tick_value` instead, never dropped without a word. The tick never produces
    one: it reads its own environment, which IS the tick's.
    """
    import capability_recurrence_check as rc

    raised: list = []
    deduped: list = []
    errors: list = []
    unknown: list = []
    for row in rep["held_off"] + rep["on_but_idle"]:
        flag, cap_id, state = row["flag"], row["capability"], row["state"]
        if rc.tick_value_unknown(str(row.get("value_source") or "")):
            unknown.append(flag)
            continue
        if state == "off":
            question = (
                f"{flag} is off and {cap_id}'s switch-on precondition is recorded. "
                f"Turn it on, or restate the criterion?"
            )
            default = "keep it off; re-ask in a week"
        else:
            question = (
                f"{flag} is ON but {cap_id} has recorded no invocation outside consult trials in "
                f"{REVIEW_DAYS}d. Keep it on, turn it off, or fix what feeds it?"
            )
            if row.get("drain"):
                # The pair is what makes the question answerable at a glance. Its text changes only
                # when a number in it moves, so a gate that stays stuck stays ONE deduped question.
                question += f" Its gate: {row['drain']['summary']}."
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
    return {
        "raised": raised,
        "already_open": deduped,
        "errors": errors,
        "unknown_tick_value": unknown,
        "dry_run": dry_run,
    }


def value_phrase(row: dict) -> str:
    """A row's switch value and where it came from, in words.

    Every source has its own sentence, because the readings mean different things and two of them
    must never print alike: "unset" (nothing sets the switch) and "0" (something set it off) both
    read OFF. Nor may an UNKNOWN tick value read as either.
    """
    import capability_recurrence_check as rc

    value, source = row.get("value"), str(row.get("value_source") or "")
    shown = "unset" if value is None else repr(str(value))
    if source == "ambient":
        return f"{shown} — set in this process's environment"
    if source == "tick":
        return (
            f"{shown} — set by orchestrate.sh's prologue (this process does not set it, or sets "
            "a value the prologue replaces)"
        )
    if source == "explicit":
        return f"{shown} in the environment passed to this review"
    if source == "unset":
        return "unset — neither this process nor orchestrate.sh's prologue sets it"
    if source.startswith(rc.TICK_UNRESOLVED_PREFIX):
        reason = source[len(rc.TICK_UNRESOLVED_PREFIX) :]
        return (
            f"{shown} in this process, and the tick's value is UNKNOWN: orchestrate.sh's "
            f"prologue did not evaluate ({reason})"
        )
    if source == rc.TICK_UNCONSULTED:
        return (
            f"{shown} in this process, and the tick's value is UNKNOWN: nothing asked "
            "orchestrate.sh's prologue, which may set it"
        )
    return f"{shown} — source {source or 'not recorded'}"


def idle_phrase(row: dict) -> str:
    """An ON-but-idle row's idleness: a number of days, or never, which is not a number of days.

    And never is a measurement: a capability with no ledger row on this machine was never READ
    (`trials_excluded` is None), which must not print as never invoked.
    """
    if row.get("trials_excluded") is None:
        return "UNMEASURED (no ledger row for it on this machine)"
    days = row.get("idle_days")
    return "never invoked" if days is None else f"{days}d"


def not_counted_phrase(row: dict) -> str:
    """What the idle rule left out of an ON-but-idle row, in words; empty when nothing was.

    Empty is plain idleness. Anything else is "idle, a trial ignored", which must never read alike:
    the ledger's `last_invocation` can look recent while the row says idle.
    """
    parts = []
    trials = int(row.get("trials_excluded") or 0)
    if trials:
        newest = row.get("newest_trial_days")
        inside = newest is not None and newest <= REVIEW_DAYS
        parts.append(
            f"{trials} consult trial{'' if trials == 1 else 's'}, the newest {newest}d ago"
            + (f", inside the {REVIEW_DAYS}d window" if inside else "")
            + " (a trial triggers an advised candidate; it is not the switch's own path running)"
        )
    if row.get("no_event_days") is not None:
        parts.append(
            f"a last_invocation {row['no_event_days']}d ago that no invocation event recorded "
            "(a causal reconciliation sets it, from edges a consult trial's verdict can make)"
        )
    return "; ".join(parts)


def profile_trial_summary_line() -> str | None:
    """One-line profile-trial status from the capability-program artifact, when present."""
    path = (
        Path(os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex" / "orchestrator")))
        / "capability-program"
        / "profile-trial.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    profiles = int(payload.get("profile_count") or 0)
    instances = int(payload.get("instance_count") or 0)
    identity = "Y" if payload.get("identity_verified") else "N"
    costs = payload.get("cost_tokens_by_profile") or {}
    quality = payload.get("quality_by_profile") or {}
    cost_bits = (
        "/".join(str((costs.get(pid) or {}).get("tokens_in", "n/a")) for pid in sorted(costs))
        or "n/a"
    )
    qual_bits = "/".join(str(quality.get(pid, "n/a")) for pid in sorted(quality)) or "n/a"
    return (
        f"profile trial: profiles {profiles}, instances {instances}, "
        f"identity verified {identity}, quality {qual_bits}, cost {cost_bits}"
    )


def format_report(rep: dict) -> str:
    lines = [
        "# Switch review — held switches must be revisited, not forgotten",
        "",
        f"  review window: {rep['review_days']}d",
        f"  due for a decision: {rep['raise_count']}",
        "",
    ]
    lines += [adversarial_shape_line(rep.get("adversarial_shape", {})), ""]
    trial_line = profile_trial_summary_line()
    if trial_line:
        lines += [trial_line, ""]
    if rep.get("value_chain") and not rep["value_chain"].get("disabled"):
        import value_chain_monitor

        lines += value_chain_monitor.format_lines(rep["value_chain"])
    if "issue_size_quality" in rep:
        import issue_size_quality

        lines += issue_size_quality.format_lines(rep["issue_size_quality"])
    if rep["held_off"]:
        lines += ["## Held OFF", ""]
        for row in rep["held_off"]:
            lines.append(f"  {row['flag']}  ({row['capability']})")
            lines.append(f"      value: {value_phrase(row)}")
            lines.append(f"      {row['reason']}")
            if row.get("criterion"):
                lines.append(f"      switch on when: {row['criterion'][:150]}")
            lines.append("")
    if rep["on_but_idle"]:
        lines += ["## ON but not triggering — the range-lane failure mode", ""]
        for row in rep["on_but_idle"]:
            lines.append(f"  {row['flag']}  ({row['capability']})  idle={idle_phrase(row)}")
            lines.append(f"      value: {value_phrase(row)}")
            lines.append(f"      {row['reason']}")
            uncounted = not_counted_phrase(row)
            if uncounted:
                lines.append(f"      not counted: {uncounted}")
            if row.get("drain"):
                lines.append(f"      drain: {row['drain']['summary']}")
            lines.append("")
    if "firing_regressions" in rep:
        lines += _firing_lines(rep["firing_regressions"])
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
    exploration_gate = rep.get("exploration_gate") or {}
    if exploration_gate.get("window_days") is not None and not exploration_gate.get("suspect"):
        arms = {row["mode"]: row for row in exploration_gate.get("arms") or []}
        lines += [
            "## Exploration arms",
            "",
            f"  window={exploration_gate.get('window_days')}d; "
            "PASS denominator=graded G; durable denominator=completed durability sweeps D",
        ]
        for mode in ("epsilon-greedy", "thompson-hybrid"):
            arm = arms.get(mode, {})
            pass_rate = arm.get("pass_rate")
            durable_rate = arm.get("durable_rate")
            pass_text = "unmeasured" if pass_rate is None else f"{pass_rate:.1%}"
            durable_text = "unmeasured" if durable_rate is None else f"{durable_rate:.1%}"
            lines.append(
                f"  {mode}: exploration decisions N={arm.get('runs', 0)} "
                f"graded G={arm.get('graded_runs', 0)} PASS={pass_text} "
                f"durable={durable_text} D={arm.get('durability_runs', 0)}"
            )
        lines.append("")
    if exploration_gate.get("suspect"):
        lines += [
            "## Exploration evidence gate",
            "",
            f"  SUSPECT — {exploration_gate['status']}: {exploration_gate['evidence_error']}",
            f"  drainable: {exploration_gate['drainable']}",
            "",
        ]
    expiry = rep.get("gate_expiry")
    if rep.get("runtime_ac_shadow") is not None:
        import runtime_ac_gate

        lines += [runtime_ac_gate.format_shadow_summary(rep["runtime_ac_shadow"]), ""]
    if expiry is not None:
        lines += format_gate_expiry(expiry)
    shed = rep.get("capacity_shed")
    if shed is not None:
        lines += format_capacity_shed(shed)
    if (
        not rep["raise_count"]
        and not rep.get("stale_runners")
        and (rep.get("mirror_drift") or {}).get("status") == "ok"
        and not (rep.get("fleet_gates") or {}).get("suspect")
        and not exploration_gate.get("suspect")
        and (
            expiry is None
            or (expiry.get("status") == "ok" and not expiry["expiring"] and not expiry["lapsed"])
        )
        and (shed is None or (shed.get("status") == "ok" and shed.get("suspect") == 0))
    ):
        lines += ["  Nothing due. Every switch is either triggering or has a fresh decision.", ""]
    return "\n".join(lines)


def format_gate_expiry(section: dict) -> list[str]:
    """The notice in words. It ALWAYS prints, because the drained state is a statement too."""
    lines = ["## Gate expiry (FYI only; nothing here renews anything)", ""]
    if section.get("status") != "ok":
        return lines + [f"  NOT MEASURED — {section.get('measurement', 'no reason recorded')}", ""]
    window = section["window_days"]
    expiring, lapsed = section["expiring"], section["lapsed"]
    if not expiring and not lapsed:
        upcoming = section.get("next_expiry")
        if upcoming:
            lines.append(
                f"  nothing expires within {window}d and nothing was retired by its expiry in the "
                f"last {window}d. {section['rows_with_expiry']} live row(s) carry an expiry; the "
                f"next is {upcoming['capability_id']} on {upcoming['expires_on']} "
                f"(in {upcoming['days_left']}d)"
            )
        else:
            lines.append(
                "  no live ledger row carries an expiry, and none was retired by one in the last "
                f"{window}d, so the timeout has nothing to retire"
            )
        return lines + [""]
    lines.append(
        f"  {len(expiring)} live row(s) expire within {window}d and {len(lapsed)} were retired by "
        f"their expiry in the last {window}d; renewable {section['renewable']}"
    )

    def activity(row: dict) -> str:
        # Two different measurements, both labelled: the count is invocation EVENTS in the usage
        # window, while `last_invocation` may also be advanced by causal reconciliation from the Brain.
        last = row["last_invocation_on"] or "never"
        return (
            f"{row['recent_invocations']} invocation event(s) in {row['recent_window_days']}d, "
            f"last invocation {last}; renewed {row['renewals']}x before"
        )

    for row in expiring:
        when = (
            f"in {row['days_left']}d"
            if row["days_left"] >= 0
            else "already passed; retires at the next writing load"
        )
        lines.append(
            f"    {row['capability_id']}  {row['status']}  expires {row['expires_on']} ({when})  "
            f"{activity(row)}"
        )
        if not row["renewable"]:
            lines.append(f"        not renewable: {row['blocker']}")
    for row in lapsed:
        lines.append(
            f"    {row['capability_id']}  retired by its expiry {row['retired_on']} (was "
            f"{row['status_before']})  {activity(row)}"
        )
        if not row["renewable"]:
            lines.append(f"        not renewable: {row['blocker']}")
    lines += [
        f"  renew one: {capabilities.renew_command('<id>')}",
        "  Unrenewed, a row retires at its expiry: the safe default, and it takes no action from "
        f"anyone. A renewal records why the row should stay and the evidence, and holds it "
        f"{section['ttl_days']}d from that day.",
        "",
    ]
    return lines


def format_capacity_shed(section: dict) -> list[str]:
    """The shed markers in words. It ALWAYS prints, because no seat shed is a statement too."""
    lines = ["## Capacity shed markers (FYI only; nothing here removes a marker)", ""]
    if section.get("status") != "ok":
        return lines + [f"  NOT MEASURED — {section.get('measurement', 'no reason recorded')}", ""]
    markers = section["markers"]
    if not markers:
        return lines + [f"  no seat is shed: no marker in {section['dir']}", ""]
    lines.append(f"  {len(markers)} marker(s) in {section['dir']}; {section['suspect']} SUSPECT")
    for row in markers:
        if not row["read_by_capacity"]:
            lines.append(f"    {row['agent']}  inert: capacity reads no seat by this name")
            continue
        lines.append(f"    {row['agent']}  {row['reason']}")
        if row["suspect"] is not None:
            lines.append(f"        SUSPECT — {row['suspect']}")
    return lines + [""]


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
    global _GH_CALL_RUNNER
    _selftest_stale_runners()
    _selftest_fleet_gates()
    # NO NETWORK. `review()` runs `fleet_gates` with the real `gh` unless a runner is injected, and
    # nothing in `_selftest_review` asserts on the fleet gates (`_selftest_fleet_gates` owns those,
    # with its own fakes), so every `review()` there was a live sweep of the consumer fleet.
    gh_calls: list = []

    def no_network(args, *, timeout_s=GH_TIMEOUT_S):
        gh_calls.append(args)
        return False, "", "unmeasured: the selftest has no network"

    saved_runner, _GH_CALL_RUNNER = _GH_CALL_RUNNER, no_network
    try:
        _selftest_review(gh_calls)
    finally:
        _GH_CALL_RUNNER = saved_runner
    _selftest_gate_expiry()
    _selftest_capacity_shed()
    proof: dict = {
        "status": "ok",
        "generated_at": 1_800_000_000,
        "rows": [
            {
                "capability_id": "test-cap",
                "findings": ["overdue"],
                "last_heartbeat": 1,
                "step_evidence": [
                    {
                        "step": "test",
                        "stamp_mtime": 1_799_999_940,
                        "stamp_age_seconds": 60,
                        "stale_after_seconds": 86400,
                        "artifact_mtime": None,
                        "artifact_path": None,
                    }
                ],
            }
        ],
    }
    assert "heartbeat silent, step ran" in "\n".join(_firing_lines(proof))
    proof["rows"][0]["step_evidence"][0]["stamp_age_seconds"] = 30 * 86400
    text = "\n".join(_firing_lines(proof))
    assert "step last ran" in text and "heartbeat silent, step ran" not in text
    assert "UNKNOWN" in "\n".join(_firing_lines({"status": "unknown", "reason": "absent"}))
    assert "candidates 0 of 0" in adversarial_shape_line(
        {
            "status": "ok",
            "shape_candidates": 0,
            "population": 0,
            "label_candidates": 0,
            "unknown": 0,
        }
    )
    assert "unmeasured" in adversarial_shape_line({"status": "unknown"})

    print(
        "switch_review.py selftest: OK (held-off raised, ON-but-idle re-raised after the window, "
        "recently-triggering stays silent, a consult trial is named and not counted, "
        "'0' is off and prints apart from unset, dry-run inert, "
        "fleet_gates SUSPECT rule, "
        "switch_states is the review's rows without the sweep, review writes no ledger, an idle "
        "bootstrap row carries its drain read from a sandbox, the expiry notice names soon/lapsed "
        "rows and states its drained and unmeasured states, the shed sweep names each marker's "
        "drain and removes none)"
    )


def _selftest_capacity_shed() -> None:
    """Each marker named with its drain; drained and unmeasured print apart; nothing is removed."""
    import tempfile

    now, day = 1_800_000_000.0, 86400
    with tempfile.TemporaryDirectory(prefix="switch-review-shed-") as td:
        shed_dir = Path(td) / "capacity-shed"
        # DRAINED, by construction: a directory nothing ever wrote reads as no seat shed...
        none = capacity_shed(now=now, shed_dir=shed_dir)
        assert none["status"] == "ok" and none["markers"] == [] and none["suspect"] == 0, none
        assert "no seat is shed" in "\n".join(format_capacity_shed(none))
        # ...and one that cannot be listed is NOT MEASURED, never drained.
        (Path(td) / "a-file").write_text("")
        unlisted = capacity_shed(now=now, shed_dir=Path(td) / "a-file")
        assert unlisted["status"] == "unknown", unlisted
        assert "NOT MEASURED" in "\n".join(format_capacity_shed(unlisted))
        shed_dir.mkdir()
        active = {"expires_at": now + 3600, "incident_id": "i1", "category": "quota"}
        (shed_dir / "codex").write_text(json.dumps({**active, "created_at": now}))
        (shed_dir / "claude").write_text("")  # stood down by hand, 20 days ago
        os.utime(shed_dir / "claude", (now - 20 * day, now - 20 * day))
        (shed_dir / "cursor").write_text(json.dumps({"expires_at": now - 2 * day}))
        (shed_dir / "claude_code").write_text("")  # no seat by that name
        (shed_dir / ".codex.tmp").write_text("{}")  # a write in progress
        (shed_dir / "archive").mkdir()
        before = sorted(path.name for path in shed_dir.iterdir())
        rep = capacity_shed(now=now, shed_dir=shed_dir)
        assert sorted(path.name for path in shed_dir.iterdir()) == before, "the sweep removed one"
        rows = {row["agent"]: row for row in rep["markers"]}
        assert sorted(rows) == ["claude", "claude_code", "codex", "cursor"], rows
        assert rows["codex"]["suspect"] is None and "shed until" in rows["codex"]["reason"], rows
        assert "held by hand for 20.0d" in (rows["claude"]["suspect"] or ""), rows["claude"]
        assert "still on disk" in (rows["cursor"]["suspect"] or ""), rows["cursor"]
        assert rows["claude_code"]["read_by_capacity"] is False, rows["claude_code"]
        assert rows["claude_code"]["suspect"] is None and rep["suspect"] == 2, rep
        text = "\n".join(format_capacity_shed(rep))
        assert "2 SUSPECT" in text and "claude_code  inert" in text, text


def _selftest_gate_expiry() -> None:
    import tempfile

    now, day = 1_700_000_000, 86400
    with tempfile.TemporaryDirectory(prefix="switch-review-expiry-") as td:
        reg = Path(td) / "capabilities.json"

        def row(cap_id: str, **fields) -> dict:
            rec = capabilities._blank_capability(cap_id)
            rec.update({"status": "wired", **fields})
            return rec

        def expired(cap_id: str, days_ago: int, reason: str) -> dict:
            event = {"timestamp": now - days_ago * day, "type": "transition", "from": "wired"}
            return row(
                cap_id,
                status="retired",
                expiry=now - days_ago * day,
                event_history=[{**event, "to": "retired", "reason": reason}],
            )

        timeout = capabilities.EXPIRY_RETIREMENT_REASON
        capabilities.save(
            {
                "soon": row("soon", expiry=now + 3 * day),
                "later": row("later", expiry=now + 40 * day),
                "lapsed": expired("lapsed", 2, timeout),
                "decided": expired("decided", 2, "no longer wanted"),
                "long-gone": expired("long-gone", GATE_EXPIRY_NOTICE_DAYS + 1, timeout),
                "no-expiry": row("no-expiry"),
            },
            reg,
        )
        before = reg.read_bytes()
        got = gate_expiry(now=now, path=reg)
        assert reg.read_bytes() == before, "the expiry notice wrote the ledger it reads"
        assert [r["capability_id"] for r in got["expiring"]] == ["soon"], got["expiring"]
        assert [r["capability_id"] for r in got["lapsed"]] == ["lapsed"], got["lapsed"]
        assert got["rows_with_expiry"] == 2 and got["renewable"] == 2, got
        assert got["next_expiry"]["capability_id"] == "later", got["next_expiry"]
        assert "renew --name soon" in got["expiring"][0]["renew"], got["expiring"][0]
        text = "\n".join(format_gate_expiry(got))
        assert "soon" in text and "lapsed" in text and "decided" not in text, text

        # DRAINED is a statement, not silence; UNMEASURED is not drained.
        capabilities.save({"later": row("later", expiry=now + 40 * day)}, reg)
        drained = gate_expiry(now=now, path=reg)
        assert not drained["expiring"] and not drained["lapsed"], drained
        assert "the next is later on" in "\n".join(format_gate_expiry(drained))
        capabilities.save({"no-expiry": row("no-expiry")}, reg)
        none = "\n".join(format_gate_expiry(gate_expiry(now=now, path=reg)))
        assert "no live ledger row carries an expiry" in none, none
        missing = gate_expiry(now=now, path=Path(td) / "absent.json")
        assert missing["status"] == "unknown", missing
        assert "NOT MEASURED" in "\n".join(format_gate_expiry(missing))
        assert not (Path(td) / "absent.json").exists(), "the notice created a ledger"


def _selftest_review(gh_calls: list) -> None:
    import tempfile
    from pathlib import Path

    now = 1_700_000_000
    with tempfile.TemporaryDirectory(prefix="switch-review-") as td:
        reg = Path(td) / "capabilities.json"
        caps = {}
        for cap_id in SWITCH_CAPABILITY.values():
            rec = capabilities._blank_capability(cap_id)
            rec["status"] = "generated"
            caps[cap_id] = rec
        capabilities.save(caps, reg)
        saved_ledger = reg.read_bytes()

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
        # ...and the three reviews above were REPORTS: none wrote the ledger. It lacks every
        # declared gate row, so the writing loader would have seeded them all into it on the first.
        assert reg.read_bytes() == saved_ledger, "review() wrote the capability ledger it reads"

        def invoked(*events: tuple[int, str]) -> None:
            # As heartbeats record them: each invocation event, and the field the newest moves.
            row = caps["range-lane-rollout"]
            row["event_history"] = [
                {"timestamp": stamp, "type": "invocation", "ref": ref} for stamp, ref in events
            ]
            row["last_invocation"] = max(stamp for stamp, _ in events)
            capabilities.save(caps, reg)

        # ON and RECENTLY triggering -> silent, no question.
        invoked((now - 2 * 86400, "routing-decision.json"))
        rep3 = review(now=now, env=env, path=reg)
        assert not rep3["on_but_idle"], rep3["on_but_idle"]

        # ON but last invocation just past the window -> raised again. This is the component that
        # makes "turned it on and forgot" impossible.
        invoked((now - (REVIEW_DAYS + 1) * 86400, "routing-decision.json"))
        rep4 = review(now=now, env=env, path=reg)
        assert {r["flag"] for r in rep4["on_but_idle"]} == {"ORCH_RANGE_LANE_ROLLOUT"}, rep4
        assert rep4["on_but_idle"][0]["idle_days"] == REVIEW_DAYS + 1
        assert rep4["on_but_idle"][0]["trials_excluded"] == 0, "plain idleness, measured"
        assert "not counted" not in format_report(rep4), format_report(rep4)

        # ...and a CONSULT TRIAL since then is not the switch doing anything: still idle, from the
        # same invocation, with the trial named beside the idleness rather than hidden in it.
        invoked(
            (now - (REVIEW_DAYS + 1) * 86400, "routing-decision.json"),
            (now - 86400, capabilities.ADVICE_REF_PREFIX + "0123456789ab"),
        )
        trialled = review(now=now, env=env, path=reg)["on_but_idle"]
        assert [r["flag"] for r in trialled] == ["ORCH_RANGE_LANE_ROLLOUT"], trialled
        assert trialled[0]["idle_days"] == REVIEW_DAYS + 1, trialled[0]
        assert (trialled[0]["trials_excluded"], trialled[0]["newest_trial_days"]) == (1, 1.0)
        assert "1 consult trial, the newest 1.0d ago, inside the" in not_counted_phrase(trialled[0])

        # "0" counts as off, not on.
        rep5 = review(now=now, env={"ORCH_RANGE_LANE_ROLLOUT": "0"}, path=reg)
        assert "ORCH_RANGE_LANE_ROLLOUT" in {r["flag"] for r in rep5["held_off"]}
        # ...and does not PRINT as unset: each row names its value and where that came from.
        by_flag = {r["flag"]: r for r in rep5["held_off"]}
        zero, unset = by_flag["ORCH_RANGE_LANE_ROLLOUT"], by_flag["ORCH_STRATEGY_EXPERIMENT"]
        assert (zero["value"], zero["value_source"]) == ("0", "explicit"), zero
        assert (unset["value"], unset["value_source"]) == (None, "explicit"), unset
        assert value_phrase(zero) != value_phrase(unset), (value_phrase(zero), value_phrase(unset))

        # Dry run never writes, and the default is always conservative.
        # raise_questions covers BOTH lists, so the ON-but-idle switch appears alongside the
        # held-off ones; what matters is that the idle one is not dropped.
        out = raise_questions(rep4, dry_run=True)
        assert out["dry_run"], out
        assert "ORCH_RANGE_LANE_ROLLOUT" in out["raised"], out
        assert len(out["raised"]) == len(rep4["held_off"]) + len(rep4["on_but_idle"]), out

        # THE ROWS WITHOUT THE SWEEP. `switch_states` is what a consumer reads instead of `review()`
        # (`capability_propensity.declared_facts`), so it must return exactly the review's rows —
        # one owner, one reading — and nothing else: no gh call, and no heartbeat, because reading
        # the rows is not a switch review and must not be credited as one.
        beats: list = []
        real_beat = globals()["_capability_heartbeat"]
        globals()["_capability_heartbeat"] = lambda *a, **k: beats.append(a)
        # The bootstrap is ON as well, so a row that carries a DRAIN is held to the same rules. Its
        # drain is read from a sandbox (no corpus, a fresh plan with no candidates), never the live
        # files: `_redirect_bootstrap_inputs` is the one seam all three paths come through.
        plan = Path(td) / "stage2-plan.json"
        plan.write_text(json.dumps({"generated_at": now - 60, "plans": []}), encoding="utf-8")
        sandbox = {
            "corpus_path": Path(td) / "corpus.jsonl",
            "plan_path": plan,
            "report_dir": Path(td) / "reports",
        }
        real_inputs = globals()["_redirect_bootstrap_inputs"]
        globals()["_redirect_bootstrap_inputs"] = lambda: dict(sandbox)
        both_on = {**env, "ORCH_REDIRECT_APPLY_BOOTSTRAP": "1"}
        try:
            calls_before = len(gh_calls)
            swept = review(now=now, env=both_on, path=reg)
            # POSITIVE CONTROL: the injected runner IS the seam the sweep uses, and the heartbeat
            # stub IS the one it calls, or the two "none" assertions below would be vacuous.
            assert len(gh_calls) > calls_before, "the injected runner is not the sweep's gh seam"
            assert beats, "the heartbeat stub is not the one review() calls"
            calls_before, beats_before = len(gh_calls), len(beats)
            rows = switch_states(now=now, env=both_on, path=reg)
            assert len(gh_calls) == calls_before, "switch_states reached gh: it is not the sweep"
            assert (
                len(beats) == beats_before
            ), "switch_states heartbeated: a consumer's read is not a review"
        finally:
            globals()["_capability_heartbeat"] = real_beat
            globals()["_redirect_bootstrap_inputs"] = real_inputs
        assert rows == {"held_off": swept["held_off"], "on_but_idle": swept["on_but_idle"]}, rows
        assert rows["held_off"] and rows["on_but_idle"], "both lists populated, or equality is weak"
        drained = [r for r in rows["on_but_idle"] if r["flag"] == "ORCH_REDIRECT_APPLY_BOOTSTRAP"]
        assert drained and drained[0]["drain"]["drainable"] == 0, drained
        assert drained[0]["drain"]["inputs"]["plan_path"] == str(plan), drained[0]["drain"]
        assert "drainable 0 of 0" in format_report(swept), format_report(swept)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--raise",
        dest="do_raise",
        action="store_true",
        help="record owner questions (requires ORCH_SWITCH_REVIEW=1)",
    )
    ap.add_argument(
        "--env",
        choices=("tick", "process"),
        default="tick",
        help=(
            "where switch values come from. tick (the default): as the tick sees them, from "
            "orchestrate.sh's prologue executed with this process's environment inherited, so a "
            "value set here is treated as a real tick treats it. process: this process's "
            "environment alone, a switch it does not set being unset; the tick passes this, "
            "because its environment IS the tick's"
        ),
    )
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.env == "process":
        env = dict(os.environ)
        sources = {flag: "ambient" if flag in env else "unset" for flag in SWITCH_CAPABILITY}
    else:
        env, sources = env_as_the_tick_sees_it()
    import capability_recurrence_check as recurrence

    if args.env != "process":
        for flag in ("ORCH_DISPATCH_LANE", "ORCH_VALUE_CHAIN_MONITOR"):
            value, source = recurrence.as_the_tick_sees_it(flag)
            sources[flag] = source
            if value is not None:
                env[flag] = value
    # Only the production weekly caller registers the declaration; reports and
    # branch tests never introduce a row into the shared live ledger.
    value_chain_inputs = None
    if env.get("ORCH_VALUE_CHAIN_MONITOR", "1") != "0":
        import feedback
        import value_chain_monitor
        from backlog import SUPPORTED_REPOS

        repos = list(
            dict.fromkeys(
                [
                    *SUPPORTED_REPOS,
                    "stranske/Orchestrator",
                    "stranske/Doc-Lineage",
                    "stranske/Deliverable-Render",
                    "stranske/Manager-Mosaic",
                ]
            )
        )

        try:
            if os.environ.get("ORCH_CAPABILITY_HEARTBEATS") == "1":
                ledger = capabilities.load_declared(capabilities.REG)
                if "value-chain-monitor" not in ledger:
                    capabilities.register(
                        "value-chain-monitor",
                        capabilities.KNOWN_DECLARATIONS["value-chain-monitor"],
                    )
            value_chain_inputs = value_chain_monitor.collect_inputs(
                now=int(time.time()),
                gh_fn=_gh_call,
                repos=repos,
                db=feedback.DB_PATH,
            )
        except Exception as exc:  # noqa: BLE001 — retain setup failure in the report
            value_chain_inputs = {
                "errors": [f"Value-chain setup or input collection failed: {exc}"]
            }
    rep = review(env=env, sources=sources, value_chain_inputs=value_chain_inputs)
    # The production weekly caller collects the curve, rather than leaving a CLI-only instrument.
    # Pure report/selftest readers keep their no-network and no-state-write contract.
    if (
        "issue-size-quality"
        not in os.environ.get("ORCH_DISABLE_STEPS", "").replace(",", " ").split()
    ):
        import issue_size_quality

        try:
            rep["issue_size_quality"] = issue_size_quality.run(
                gh_fn=_gh_call, repos=issue_size_quality.fleet_repos()
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — preserve the weekly report on unavailable evidence
            rep["issue_size_quality"] = {"status": "unknown", "errors": [str(exc)]}
    import adversarial

    try:
        rep["adversarial_shape_measurement_recorded"] = adversarial.record_shape_measurement(
            rep.get("adversarial_shape", {"status": "unknown"}),
            now=rep.get("generated_at", int(time.time())),
        )
    except (OSError, ValueError) as exc:
        rep["adversarial_shape_measurement_error"] = str(exc)
    if args.do_raise:
        if not APPLY_ENABLED:
            print("refusing to raise: set ORCH_SWITCH_REVIEW=1", file=sys.stderr)
            return 2
        rep["questions"] = raise_questions(rep, dry_run=False)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
