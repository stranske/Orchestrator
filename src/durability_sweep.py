#!/usr/bin/env python3
"""durability_sweep.py - resolve pending durability labels for merged orchestrator outcomes.

A merge is only a provisional success. Days later, this sweep re-checks merged PRs whose
feedback outcome still has durability='pending'. It patches only high-confidence outcomes:
reopened, reverted, or durable. Any ambiguity stays pending so the learner never treats an
undone merge as a durable win.

`--selftest` runs fully offline against a temp feedback store.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote

import feedback
import outcomes
import provision

GRACE_DAYS = 7
REVERT_WINDOW_DAYS = (
    21  # reverts land within days of a merge; bound the base-commit scan to this window
)
SECONDS_PER_DAY = 86400
MAX_REVERT_PRS = 50
# Fix-titled PRs are far more common than reverts, so the local-match window has to be wider
# or a busy repo's recent fixes push the relevant one off the end and the check silently misses.
MAX_FIX_PRS = 200
MAX_BASE_COMMITS = 100
EXPLICIT_MERGED_PR_RE = re.compile(r"\bPR\s+#(?P<num>\d+)\s+merged\b", re.IGNORECASE)


def _pending_merged_runs() -> list[dict]:
    """Runs whose outcome is a merged PR but durability has not been resolved yet."""
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT r.run_id, r.target, r.mode, r.pr_number, COALESCE(o.notes,'') "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE o.merged=1 AND COALESCE(o.durability,'pending')='pending' "
            "ORDER BY r.ts ASC"
        ).fetchall()
    return [
        {"run_id": rid, "target": target, "mode": mode, "pr_number": prn, "notes": notes}
        for rid, target, mode, prn, notes in rows
    ]


def _explicit_merged_pr_target(target: str, notes: str | None) -> str | None:
    repo, _num = provision.parse_target(target)
    if not repo:
        return None
    match = EXPLICIT_MERGED_PR_RE.search(notes or "")
    if not match:
        return None
    return f"{repo}#{int(match.group('num'))}"


def _state_lookup_target(run: dict) -> str:
    """Use an explicitly recorded merged PR when the run target was the source issue."""
    return _explicit_merged_pr_target(run["target"], run.get("notes")) or run["target"]


def _parse_gh_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _age_days(ts: int, now: int) -> int:
    return max(0, int((now - ts) // SECONDS_PER_DAY))


def _run_json(args: list[str], *, timeout: int = 30) -> object | None:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _gh_throttle(resource: str) -> None:
    """Pace/defer against the shared GitHub rate budget (gh_capacity) when ORCH_GH_THROTTLE=1;
    no-op + fail-open otherwise so the sweep never breaks on a missing/erroring module."""
    try:
        import gh_capacity

        gh_capacity.throttle_if_enabled(resource)
    except Exception:
        pass


def _gh_pr_details(target: str, mode: str | None) -> dict | None:
    """Live PR details beyond outcomes.py's state helpers: number, base, title, merge commit."""
    repo, num = provision.parse_target(target)
    if num is None:
        return None

    fields = "number,state,mergedAt,closedAt,mergeCommit,baseRefName,title,files"
    if mode == "local":
        arr = _run_json(
            [
                "gh",
                "pr",
                "list",
                "-R",
                repo,
                "--head",
                f"orchestrator/issue-{num}",
                "--state",
                "all",
                "--json",
                fields,
                "--limit",
                "1",
            ]
        )
        return arr[0] if isinstance(arr, list) and arr else None

    obj = _run_json(["gh", "pr", "view", str(num), "-R", repo, "--json", fields])
    return obj if isinstance(obj, dict) else None


def _merge_dicts(*items: dict | None) -> dict | None:
    out = {}
    for item in items:
        if isinstance(item, dict):
            out.update({k: v for k, v in item.items() if v is not None})
    return out or None


def _resolved_pr_state(run: dict, _state_fn=None) -> dict | None:
    """Resolve a run target to PR state, reusing outcomes.py helpers for direct/local targets."""
    target = _state_lookup_target(run)
    if _state_fn:
        pr = _state_fn(target)
        return pr if isinstance(pr, dict) else None

    mode = run.get("mode")
    base_state = outcomes._local_pr_state(target) if mode == "local" else outcomes._pr_state(target)
    if base_state is None:
        return None
    details = _gh_pr_details(target, mode)
    return _merge_dicts(base_state, details)


def _merge_sha(pr: dict) -> str | None:
    mc = pr.get("mergeCommit")
    if isinstance(mc, dict):
        return mc.get("oid") or mc.get("sha")
    if isinstance(mc, str):
        return mc
    return pr.get("mergeCommitOid") or pr.get("merge_sha") or pr.get("mergeCommitSha")


def _default_branch(repo: str) -> str | None:
    try:
        r = subprocess.run(
            [
                "gh",
                "repo",
                "view",
                repo,
                "--json",
                "defaultBranchRef",
                "-q",
                ".defaultBranchRef.name",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _contains_ref(text: str, number: int) -> bool:
    return re.search(rf"(?<!\d)#{number}(?!\d)", text or "") is not None


def _fetch_repo_revert_prs(repo: str) -> tuple[list | None, bool]:
    """Repo-wide 'Revert in:title' SEARCH (rate: REST search = 30/min).

    The result is identical for every PR in the repo, so callers cache it per repo and
    match locally — a bulk sweep over N PRs in a repo then costs ONE search, not N.
    Returns (merged_revert_prs, hit_limit); the list is None on search failure.
    """
    _gh_throttle("search")  # SEARCH = 30/min, the binding constraint on bulk sweeps
    arr = _run_json(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--search",
            "Revert in:title",
            "--json",
            "number,title,body",
            "--limit",
            str(MAX_REVERT_PRS),
        ]
    )
    if not isinstance(arr, list):
        return None, False
    return arr, len(arr) >= MAX_REVERT_PRS


def _fetch_repo_fix_prs(repo: str) -> tuple[list | None, bool]:
    """Repo-wide 'fix in:title' SEARCH, cached per repo and matched locally.

    Same shape and same reason as the revert search above: the result is identical for every PR
    in the repo, so a bulk sweep costs ONE search rather than N against a 30/min limit.
    """
    _gh_throttle("search")
    arr = _run_json(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--search",
            "fix in:title",
            "--json",
            "number,title,body,mergedAt",
            "--limit",
            str(MAX_FIX_PRS),
        ]
    )
    if not isinstance(arr, list):
        return None, False
    return arr, len(arr) >= MAX_FIX_PRS


def _fix_followup_status(
    repo: str,
    pr_number: int,
    merged_ts: int,
    fix_cache: dict | None = None,
    _fix_fn=None,
) -> tuple[bool | None, str]:
    """Did a LATER merged fix PR explicitly NAME this one? That is `broke_later`.

    A HIGH BAR, deliberately, and higher than the one escaped_defect_priority uses. That module
    ranks work on the loose signal — a fix commit touching the same file — which is decent
    evidence for ORDERING and poor evidence for TRAINING, because code is changed for many
    reasons and a fix on the same file may repair something this change never touched. This
    writes `outcomes.durability`, which the router learns from, so it demands an explicit
    reference: a fix-titled PR that names this PR number and merged AFTER it.

    ADDITIVE, AND THAT IS THE POINT. Anything other than a confident True falls through to the
    classification this function did not exist to change. A new check that could leave rows
    permanently pending would be a latch of its own, so an unavailable search costs the refinement
    and never the result.
    """
    fetch = _fix_fn or _fetch_repo_fix_prs
    if fix_cache is not None and repo in fix_cache:
        arr, hit_limit = fix_cache[repo]
    else:
        arr, hit_limit = fetch(repo)
        if fix_cache is not None:
            fix_cache[repo] = (arr, hit_limit)
    if arr is None:
        return None, "fix-PR search unavailable"
    for item in arr:
        number = item.get("number")
        if number == pr_number:
            continue  # a change cannot be the follow-up that reports its own breakage
        later = _parse_gh_ts(item.get("mergedAt"))
        if later is None or later <= merged_ts:
            continue  # a fix that landed FIRST cannot be repairing this
        title = item.get("title") or ""
        haystack = f"{title}\n{item.get('body') or ''}"
        if _contains_ref(haystack, pr_number):
            return True, f"later fix PR #{number} names this change"
    if hit_limit:
        return None, f"fix-PR search hit limit {MAX_FIX_PRS}"
    return False, "no later fix PR names this change"


def _revert_pr_status(
    repo: str, pr_number: int, revert_cache: dict | None = None
) -> tuple[bool | None, str]:
    """Was `pr_number` reverted by a merged 'Revert ...' PR? Matches `pr_number` locally
    against the repo-wide revert search, cached per repo when `revert_cache` is supplied
    (the per-PR search otherwise hit the 30/min REST search limit on bulk sweeps)."""
    if revert_cache is not None and repo in revert_cache:
        arr, hit_limit = revert_cache[repo]
    else:
        arr, hit_limit = _fetch_repo_revert_prs(repo)
        if revert_cache is not None:
            # Cache even a failed search (arr is None): one search attempt per repo per run,
            # then fall back to the CORE-limited commit scan per PR rather than re-hitting search.
            revert_cache[repo] = (arr, hit_limit)
    if arr is None:
        return None, "revert PR search failed"
    for item in arr:
        title = item.get("title") or ""
        haystack = f"{title}\n{item.get('body') or ''}"
        if title.lower().startswith("revert") and _contains_ref(haystack, pr_number):
            return True, f"revert PR found: #{item.get('number')}"
    if hit_limit:
        return None, f"revert PR search hit limit {MAX_REVERT_PRS}"
    return False, "no revert PR found"


def _revert_commit_status(
    repo: str, base: str, merge_sha: str, merged_at: str
) -> tuple[bool | None, str]:
    # Bound the scan to a window AFTER the merge: a commit two months later is not "this merge failed",
    # and scanning every commit since an old merge always blows past MAX_BASE_COMMITS in active repos
    # (which forced every old merged PR to stay pending — the keepalive backfill exposed this).
    until_param = ""
    merged_ts = _parse_gh_ts(merged_at)
    if merged_ts is not None:
        until = _dt.datetime.fromtimestamp(
            merged_ts + REVERT_WINDOW_DAYS * SECONDS_PER_DAY, _dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        until_param = f"&until={quote(until, safe='')}"
    path = (
        f"repos/{repo}/commits?sha={quote(base, safe='')}"
        f"&since={quote(merged_at, safe='')}{until_param}&per_page={MAX_BASE_COMMITS}"
    )
    _gh_throttle("core")  # gh api = CORE (5000/hr)
    arr = _run_json(["gh", "api", path])
    if not isinstance(arr, list):
        return None, "base commit scan failed"
    needle = f"This reverts commit {merge_sha}"
    for item in arr:
        msg = (item.get("commit") or {}).get("message") or ""
        if needle in msg:
            sha = item.get("sha") or "unknown"
            return True, f"revert commit found: {sha}"
    if len(arr) >= MAX_BASE_COMMITS:
        return None, f"base commit scan hit limit {MAX_BASE_COMMITS}"
    return False, "no base-branch revert commit found"


def _live_revert_status(pr: dict, revert_cache: dict | None = None) -> tuple[bool | None, str]:
    repo = pr.get("repo")
    pr_number = pr.get("number") or pr.get("pr_number")
    merge_sha = _merge_sha(pr)
    merged_at = pr.get("mergedAt")
    base = pr.get("baseRefName")

    if not repo:
        return None, "missing repo"

    pr_check: tuple[bool | None, str] = (None, "missing PR number")
    if pr_number is not None:
        pr_check = _revert_pr_status(repo, int(pr_number), revert_cache=revert_cache)
        if pr_check[0] is True:
            return pr_check

    if not merge_sha:
        return None, "missing merge commit SHA"
    if not merged_at:
        return None, "missing mergedAt"
    if not base:
        base = _default_branch(repo)
    if not base:
        return None, "missing base branch"

    commit_check = _revert_commit_status(repo, base, merge_sha, merged_at)
    if commit_check[0] is True:
        return commit_check
    # No revert found. A completed "no revert PR" probe is sufficient in these PR-gated repos even when
    # the bounded commit scan was inconclusive (a high-churn window can still hit the page limit).
    if pr_check[0] is False or commit_check[0] is False:
        return False, "no revert PR or base-branch revert commit found"
    return None, commit_check[1]


def _normalize_revert_status(value) -> tuple[bool | None, str]:
    if isinstance(value, tuple) and len(value) == 2:
        return value
    if isinstance(value, dict):
        return value.get("reverted"), value.get("note") or value.get("reason") or "revert resolver"
    if value is True:
        return True, "revert resolver found revert"
    if value is False:
        return False, "revert resolver found no revert"
    return None, "revert resolver ambiguous"


def _revert_status(
    pr: dict, _revert_fn=None, revert_cache: dict | None = None
) -> tuple[bool | None, str]:
    if _revert_fn:
        return _normalize_revert_status(_revert_fn(pr))
    if "reverted" in pr:
        return _normalize_revert_status(pr.get("reverted"))
    return _live_revert_status(pr, revert_cache=revert_cache)


# Paths that are pure agent bookkeeping: touching only these delivers nothing to the product.
# Evidence for the guard (2026-08-18): Counter_Risk#791 "[design-system] Adopt the shared light
# theme" was CLOSED by PR #792 "Agent belt for #791", whose entire diff was one 240-line
# `.agents/issue-791-ledger.yml`. Zero app files. The design-system kit stayed vendored and
# uncalled -- verbatim the failure the tracker existed to fix -- and the run recorded merged=1, so
# the learner would have counted it as a durable success.
#
# Deliberately NARROW. A ledger-only PR is not automatically a false completion: Pension-Data#644
# and learning-management-system#366 were also ledger-only, but sibling PRs delivered the real work,
# so the parent issue was genuinely done. The signal is only trustworthy for the specific run whose
# own merged PR delivered nothing, which is exactly what this classifier is scoped to.
BOOKKEEPING_PATH_RE = re.compile(r"^\.agents/", re.IGNORECASE)


def delivered(pr: dict | None) -> bool | None:
    """Did this merged PR change anything outside agent bookkeeping?

    Returns True (delivered), False (bookkeeping only), or None (cannot tell).

    FAIL-SAFE DIRECTION: None and True both keep the existing durable path. Wrongly calling a real
    delivery "no delivery" would train a capable agent as incapable off a missing API field, which
    is worse than missing one false success -- so only a CONFIDENT, non-empty, all-bookkeeping file
    list downgrades anything.
    """
    if not isinstance(pr, dict) or "files" not in pr:
        return None
    files = pr.get("files")
    if not isinstance(files, list) or not files:
        return None  # no file list is unknown, never "delivered nothing"
    paths = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if path:
            paths.append(str(path))
    if not paths:
        return None
    return not all(BOOKKEEPING_PATH_RE.search(p) for p in paths)


def classify_durability(
    run: dict,
    pr: dict | None,
    *,
    grace_days: int = GRACE_DAYS,
    now: int | None = None,
    _revert_fn=None,
    revert_cache: dict | None = None,
    _fix_fn=None,
    fix_cache: dict | None = None,
) -> dict:
    """Pure-ish classifier. Returns a pending result whenever evidence is incomplete."""
    now = int(now or time.time())
    if not pr:
        return {"durability": None, "reason": "PR state unavailable"}

    repo, target_num = provision.parse_target(run["target"])
    pr = dict(pr)
    pr.setdefault("repo", repo)
    if run.get("mode") != "local":
        pr.setdefault("number", target_num)

    merged_at = pr.get("mergedAt")
    merged_ts = _parse_gh_ts(merged_at)
    if merged_ts is None:
        return {"durability": None, "reason": "missing or invalid mergedAt"}

    age = _age_days(merged_ts, now)
    if now - merged_ts < grace_days * SECONDS_PER_DAY:
        return {"durability": None, "reason": f"merge age {age}d < grace {grace_days}d"}

    state = (pr.get("state") or "").upper()
    if state == "OPEN":
        return {
            "durability": "reopened",
            "notes": f"durability_sweep: PR reopened after merge; merge age {age}d",
        }

    if state not in ("MERGED", "CLOSED"):
        return {"durability": None, "reason": f"ambiguous PR state {state or 'unknown'}"}

    reverted, revert_note = _revert_status(pr, _revert_fn=_revert_fn, revert_cache=revert_cache)
    if reverted is True:
        return {
            "durability": "reverted",
            "notes": f"durability_sweep: {revert_note}; merge age {age}d",
        }
    if reverted is None:
        return {"durability": None, "reason": revert_note}

    if delivered(pr) is False:
        paths = ", ".join(
            str(f.get("path") if isinstance(f, dict) else f) for f in (pr.get("files") or [])
        )[:160]
        return {
            "durability": "abandoned",
            "notes": (
                f"durability_sweep: merged but delivered nothing -- every changed path is "
                f"agent bookkeeping ({paths}); merge age {age}d"
            ),
        }

    # BROKE_LATER: merged, not reverted, delivered real work -- and then a later fix PR named it.
    # Checked here rather than earlier because a reverted or abandoned change is already
    # classified by a stronger signal, and re-labelling it would lose that.
    broke, fix_note = _fix_followup_status(
        pr.get("repo") or repo,
        int(pr.get("number") or target_num or 0),
        merged_ts,
        fix_cache=fix_cache,
        _fix_fn=_fix_fn,
    )
    if broke is True:
        return {
            "durability": "broke_later",
            "notes": f"durability_sweep: {fix_note}; held {age}d before that",
        }
    # ADDITIVE BY CONSTRUCTION. `broke is None` means the search could not answer, and that falls
    # through to the durable verdict this function reached before the check existed -- with the
    # reason recorded, so an unavailable search is visible rather than mistaken for a clean bill.
    # Anything else would let a new refinement strand rows in pending forever.
    return {
        "durability": "durable",
        "notes": f"durability_sweep: held {age}d; {revert_note}; {fix_note}",
    }


def sweep_durability(
    *,
    grace_days: int = GRACE_DAYS,
    dry_run: bool = False,
    _state_fn=None,
    _revert_fn=None,
    _now: int | None = None,
) -> dict:
    """Patch old merged+pending outcomes when their durability can be resolved with confidence."""
    summary: dict[str, Any] = {
        "checked": 0,
        "durable": 0,
        "reverted": 0,
        "reopened": 0,
        "skipped": 0,
        "details": [],
    }
    revert_cache: dict = {}  # repo -> cached revert search, so a bulk sweep does 1 search/repo
    fix_cache: dict = {}  # repo -> cached fix search, same reason: 1 search/repo, matched locally
    for run in _pending_merged_runs():
        summary["checked"] += 1
        pr = _resolved_pr_state(run, _state_fn=_state_fn)
        verdict = classify_durability(
            run,
            pr,
            grace_days=grace_days,
            now=_now,
            _revert_fn=_revert_fn,
            revert_cache=revert_cache,
            fix_cache=fix_cache,
        )
        durability = verdict.get("durability")
        if durability is None:
            summary["skipped"] += 1
            summary["details"].append(
                {
                    "run_id": run["run_id"],
                    "target": run["target"],
                    "action": "skip",
                    "reason": verdict.get("reason"),
                }
            )
            continue

        summary[durability] += 1
        detail = {
            "run_id": run["run_id"],
            "target": run["target"],
            "action": "patch",
            "durability": durability,
            "notes": verdict["notes"],
        }
        if dry_run:
            detail["dry_run"] = True
        else:
            feedback.record_outcome(run["run_id"], durability=durability, notes=verdict["notes"])
        summary["details"].append(detail)
    return summary


def _iso_days_ago(now: int, days: int) -> str:
    dt = _dt.datetime.fromtimestamp(now - days * SECONDS_PER_DAY, _dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _selftest_live_revert_scan(now: int):
    """Exercise the live revert resolver without the injected `reverted` shortcut."""
    old = _iso_days_ago(now, 10)

    def classify_with_canned_gh(
        pr_number: int, merge_sha: str, pr_rows: list[dict], commit_rows: list[dict]
    ) -> tuple[dict, list[list[str]]]:
        calls = []
        real_run_json = globals()["_run_json"]

        def fake_run_json(args: list[str], *, timeout: int = 30):
            calls.append(args)
            if args[:3] == ["gh", "pr", "list"]:
                return pr_rows
            if args[:2] == ["gh", "api"]:
                return commit_rows
            raise AssertionError(f"unexpected gh call: {args}")

        globals()["_run_json"] = fake_run_json
        try:
            return (
                classify_durability(
                    {"target": f"o/r#{pr_number}", "mode": "remote"},
                    {
                        "state": "MERGED",
                        "number": pr_number,
                        "mergedAt": old,
                        "baseRefName": "main",
                        "mergeCommit": {"oid": merge_sha},
                    },
                    grace_days=GRACE_DAYS,
                    now=now,
                ),
                calls,
            )
        finally:
            globals()["_run_json"] = real_run_json

    full_page_without_revert = [
        {"sha": f"noise-{i}", "commit": {"message": f"ordinary commit {i}"}}
        for i in range(MAX_BASE_COMMITS)
    ]
    verdict, calls = classify_with_canned_gh(
        101,
        "merge-trust",
        [{"number": 900, "title": "Revert unrelated change", "body": "Refs #999"}],
        full_page_without_revert,
    )
    assert verdict["durability"] == "durable", verdict

    # ---- no-delivery guard: a merge that changed only agent bookkeeping is NOT a durable win ----
    # Modelled on the real case: Counter_Risk#791 closed by PR #792 whose entire diff was
    # `.agents/issue-791-ledger.yml`, so merged=1 would have trained as a success.
    _old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=GRACE_DAYS + 5)
    _old_iso = _old.strftime("%Y-%m-%dT%H:%M:%SZ")
    _now_ts = int(time.time())

    def _verdict_for(files):
        pr = {
            "state": "MERGED",
            "number": 792,
            "mergedAt": _old_iso,
            "baseRefName": "main",
            "mergeCommit": {"oid": "deadbeef"},
        }
        if files is not None:
            pr["files"] = files
        return classify_durability(
            {"target": "o/r#792", "mode": "remote"},
            pr,
            grace_days=GRACE_DAYS,
            now=_now_ts,
            _revert_fn=lambda *a, **k: (False, "no revert found"),
        )

    ledger_only = _verdict_for([{"path": ".agents/issue-791-ledger.yml"}])
    assert ledger_only["durability"] == "abandoned", ledger_only
    assert "delivered nothing" in ledger_only["notes"], ledger_only
    # It must count as a REGRESSION, never a success, for the capability learner.
    assert "abandoned" in feedback.CAPABILITY_REGRESSION_DURABILITY

    # Real work still resolves durable...
    assert _verdict_for([{"path": "dashboard/theme.py"}])["durability"] == "durable"
    # ...and so does a MIXED diff (a ledger entry alongside real work is normal).
    assert _verdict_for([{"path": ".agents/x.yml"}, {"path": "app.py"}])["durability"] == "durable"

    # FAIL-SAFE: unknowable delivery must never manufacture a failure.
    for unknown in (None, []):  # type: ignore[var-annotated]  # deliberate mixed-type probe
        got = _verdict_for(unknown)
        assert got["durability"] == "durable", (unknown, got)

    # DELIBERATE BREAK -> REVERT: widen the bookkeeping pattern to match everything and real work
    # gets wrongly condemned — proving the pattern is what keeps the guard honest.
    _saved_re = BOOKKEEPING_PATH_RE
    try:
        globals()["BOOKKEEPING_PATH_RE"] = re.compile(r".")
        broken = _verdict_for([{"path": "dashboard/theme.py"}])
        assert (
            broken["durability"] == "abandoned"
        ), "break did not change behaviour — test is vacuous"
    finally:
        globals()["BOOKKEEPING_PATH_RE"] = _saved_re
    assert (
        _verdict_for([{"path": "dashboard/theme.py"}])["durability"] == "durable"
    ), "revert did not restore the guard"

    commit_paths = [args[2] for args in calls if len(args) >= 3 and args[:2] == ["gh", "api"]]
    assert commit_paths, calls
    old_ts = _parse_gh_ts(old)
    assert old_ts is not None, old
    expected_until = _dt.datetime.fromtimestamp(
        old_ts + REVERT_WINDOW_DAYS * SECONDS_PER_DAY, _dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    expected_until_param = f"&until={quote(expected_until, safe='')}"
    assert expected_until_param in commit_paths[0], commit_paths[0]

    verdict, _calls = classify_with_canned_gh(
        102,
        "merge-pr-reverted",
        [
            {
                "number": 901,
                "title": "Revert feature from orchestrator",
                "body": "Backs out the change from #102.",
            }
        ],
        [],
    )
    assert verdict["durability"] == "reverted", verdict

    verdict, _calls = classify_with_canned_gh(
        103,
        "merge-commit-reverted",
        [],
        [{"sha": "revert-sha", "commit": {"message": "This reverts commit merge-commit-reverted"}}],
    )
    assert verdict["durability"] == "reverted", verdict


def _selftest_revert_search_cached_per_repo(now: int):
    """A bulk sweep must issue ONE repo-wide revert SEARCH per repo, not one per PR
    (REST search = 30/min; the per-PR search hit a rate wall on the keepalive backfill)."""
    import shutil
    import tempfile
    from pathlib import Path

    saved_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="durability-sweep-cache-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    old = _iso_days_ago(now, 10)
    nums = (11, 12, 13)
    try:
        for n in nums:
            rid = f"cache-{n}"
            feedback.record_run(
                rid, f"o/r#{n}", "implement", "codex", mode="remote", ts=now - 20 * SECONDS_PER_DAY
            )
            feedback.record_outcome(
                rid, adjudicated_verdict="PASS", merged=True, durability="pending"
            )
        # PR state withholds the 'reverted' key so classification takes the live search path.
        states = {
            f"o/r#{n}": {
                "state": "MERGED",
                "number": n,
                "mergedAt": old,
                "baseRefName": "main",
                "mergeCommit": {"oid": f"sha-{n}"},
            }
            for n in nums
        }
        search_calls = {"n": 0}
        real_run_json = globals()["_run_json"]

        def fake_run_json(args: list[str], *, timeout: int = 30):
            if args[:3] == ["gh", "pr", "list"] and "--search" in args:
                search_calls["n"] += 1
                return [{"number": 900, "title": "Revert an unrelated PR", "body": "Refs #999"}]
            if args[:2] == ["gh", "api"]:
                return []  # empty base-commit scan -> no revert commit (CORE-limited, per-PR)
            raise AssertionError(f"unexpected gh call: {args}")

        globals()["_run_json"] = fake_run_json
        try:
            res = sweep_durability(
                grace_days=GRACE_DAYS, _state_fn=lambda t: states.get(t), _now=now
            )
        finally:
            globals()["_run_json"] = real_run_json

        # THE INVARIANT IS PER-REPO, NOT A MAGIC NUMBER. Three PRs in one repo cost TWO
        # repo-wide searches — one for reverts, one for fix follow-ups — because both are cached
        # per repo and matched locally. Were either searched per PR this would be 3 or 6, which is
        # the regression this pins; the 30/min REST search limit is what makes it matter.
        assert (
            search_calls["n"] == 2
        ), f"expected 2 repo-wide searches (revert + fix) for 3 PRs, got {search_calls['n']}"
        assert res["checked"] == 3 and res["durable"] == 3, res
    finally:
        feedback.DB_PATH = saved_db
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_broke_later(now: int):
    """A later fix PR that NAMES this change is broke_later; anything weaker is not.

    The bar is explicit reference on purpose. escaped_defect_priority ranks on the loose signal
    (a fix touching the same file), which is fine for ordering work and wrong for training the
    router — so this asserts the loose case does NOT produce the label.
    """
    pr = {
        "repo": "o/r",
        "number": 10,
        "state": "MERGED",
        "mergedAt": _iso_days_ago(now, 30),
        "files": [{"path": "src/real.py"}],
    }
    run = {"target": "o/r#10", "mode": "remote"}

    def _no_revert(_pr, **_kw):
        return False, "no revert"

    # (a) a later fix PR naming this change -> broke_later
    def _fix_names_it(_repo):
        return [
            {
                "number": 11,
                "title": "fix(core): repair regression",
                "body": "Fixes the regression introduced in #10",
                "mergedAt": _iso_days_ago(now, 5),
            }
        ], False

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_names_it, fix_cache={}
    )
    assert got["durability"] == "broke_later", got
    assert "#11" in got["notes"], got

    # (b) a fix that does NOT name it -> durable, not broke_later. Same file is not enough.
    def _fix_unrelated(_repo):
        return [
            {
                "number": 12,
                "title": "fix(core): unrelated",
                "body": "no reference at all",
                "mergedAt": _iso_days_ago(now, 5),
            }
        ], False

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_unrelated, fix_cache={}
    )
    assert got["durability"] == "durable", got

    # (c) a fix that landed BEFORE the merge cannot be repairing it
    def _fix_earlier(_repo):
        return [
            {
                "number": 13,
                "title": "fix: earlier",
                "body": "Refs #10",
                "mergedAt": _iso_days_ago(now, 60),
            }
        ], False

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_earlier, fix_cache={}
    )
    assert got["durability"] == "durable", got

    # (d) a change cannot be the follow-up that reports its own breakage
    def _fix_is_self(_repo):
        return [
            {
                "number": 10,
                "title": "fix: itself",
                "body": "Refs #10",
                "mergedAt": _iso_days_ago(now, 5),
            }
        ], False

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_is_self, fix_cache={}
    )
    assert got["durability"] == "durable", got

    # (e) ADDITIVE: an unavailable search costs the refinement, never the result. A new check
    # that could strand rows in pending forever would be a latch of its own.
    def _fix_unavailable(_repo):
        return None, False

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_unavailable, fix_cache={}
    )
    assert got["durability"] == "durable", got
    assert "unavailable" in got["notes"], got


def _selftest():
    import shutil
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp(prefix="durability-sweep-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    now = int(time.time())
    old = _iso_days_ago(now, 10)
    young = _iso_days_ago(now, 2)

    try:
        cases = [
            (
                "clean",
                "o/r#1",
                {
                    "state": "MERGED",
                    "number": 1,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "aaa"},
                    "reverted": False,
                },
                "durable",
            ),
            (
                "revert",
                "o/r#2",
                {
                    "state": "MERGED",
                    "number": 2,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "bbb"},
                    "reverted": True,
                },
                "reverted",
            ),
            (
                "open",
                "o/r#3",
                {
                    "state": "OPEN",
                    "number": 3,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "ccc"},
                    "reverted": False,
                },
                "reopened",
            ),
            (
                "young",
                "o/r#4",
                {
                    "state": "MERGED",
                    "number": 4,
                    "mergedAt": young,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "ddd"},
                    "reverted": False,
                },
                "pending",
            ),
            (
                "ambiguous",
                "o/r#5",
                {
                    "state": "MERGED",
                    "number": 5,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "eee"},
                    "reverted": None,
                },
                "pending",
            ),
        ]
        states = {}
        for run_id, target, state, _expected in cases:
            feedback.record_run(
                run_id, target, "implement", "codex", mode="remote", ts=now - 20 * SECONDS_PER_DAY
            )
            feedback.record_outcome(
                run_id, adjudicated_verdict="PASS", merged=True, durability="pending"
            )
            states[target] = state

        feedback.record_run(
            "explicit-pr-note",
            "o/r#9",
            "implement",
            "codex",
            mode="full",
            ts=now - 20 * SECONDS_PER_DAY,
        )
        feedback.record_outcome(
            "explicit-pr-note",
            adjudicated_verdict="PASS",
            merged=True,
            durability="pending",
            notes="local delegate completed through PR #10 merged by maintainer",
        )
        states["o/r#10"] = {
            "state": "MERGED",
            "number": 10,
            "mergedAt": old,
            "baseRefName": "main",
            "mergeCommit": {"oid": "fff"},
            "reverted": False,
        }

        res = sweep_durability(
            grace_days=GRACE_DAYS, _state_fn=lambda target: states.get(target), _now=now
        )
        assert res["checked"] == 6 and res["durable"] == 2 and res["reverted"] == 1, res
        assert res["reopened"] == 1 and res["skipped"] == 2, res

        with feedback._conn() as c:
            got = dict(c.execute("SELECT run_id, durability FROM outcomes").fetchall())
        for run_id, _target, _state, expected in cases:
            assert got[run_id] == expected, (run_id, got, expected)
        assert got["explicit-pr-note"] == "durable", got

        _selftest_live_revert_scan(now)
        _selftest_revert_search_cached_per_repo(now)
        _selftest_broke_later(now)

        print(
            "durability_sweep.py selftest: OK (old clean->durable, revert->reverted, "
            "open->reopened, young/ambiguous stay pending, live revert scan covered, "
            "searches cached per repo not per PR, and broke_later only on an explicit "
            "later-fix reference)"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sweep merged pending outcomes for durability.")
    parser.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    res = sweep_durability(grace_days=args.grace_days, dry_run=args.dry_run)
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
