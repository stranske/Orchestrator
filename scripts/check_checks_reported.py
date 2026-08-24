#!/usr/bin/env python3
"""An ABSENT check must not read like a passing one.

THE STRUCTURAL DEFECT THIS CLOSES, and the specific cause is deliberately not modelled.

`main` has no branch protection, so nothing structurally blocks a merge whose checks never
reported. And `gh pr checks` lists what DID report: a check that never started is not "red", it is
missing from the list, so a PR with no Gate at all reads exactly like a PR whose Gate passed. That
is this repository's founding defect -- silence indistinguishable from success -- and it has now
produced two separate incidents:

  * 2026-08-23: five python-ci jobs died at a shared install step for want of
    `.github/workflows/autofix-versions.env`. PRs #61/#64/#65 merged with all five red.
  * 2026-08-24: PR #90's Gate run was held at `action_required` with ZERO jobs, so the Gate never
    ran at all. It merged with no lint, no format and no typecheck, landing six F821s that were
    found only because somebody happened to run ruff by hand.

THE CAUSE WILL BE DIFFERENT NEXT TIME, so this tool does not look for holds. A check can go absent
because a workflow was held for review, cancelled, deleted, renamed, rate-limited, path-filtered by
mistake, or lost to a GitHub incident. All of those present identically to the merger, and all of
them are the same bug: something that was being checked stopped being checked, quietly. This tool
answers only "did every check that reports on a comparable PR also report here", which is true of
every one of those causes and needs no taxonomy.

WHY THE EXPECTED SET IS DERIVED, NOT LISTED. A hardcoded list of required check names is a second
copy of the CI topology, and this repo has an unbroken record of paired literals drifting apart.
The reference set is taken from a recently MERGED pull request instead, so it tracks reality on its
own and needs no maintenance. Path filters are not a false-positive source: a job skipped by a path
filter still REPORTS, with conclusion `skipped`. This compares which check NAMES reported at all,
never their conclusions -- conclusions are the Gate's business, presence is this tool's.

The safe direction is noise. A workflow that was deliberately deleted will be flagged until the
reference PR rotates past it; that is a one-line acknowledgement, whereas the opposite error ships
an unverified merge.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

REPO = "stranske/Orchestrator"
# Bot/advisory checks whose absence means nothing: they are opt-in, rate-limited, or driven by
# events unrelated to the head commit. Named individually -- never a prefix wildcard, which would
# quietly swallow a real check that happened to share a word.
IGNORED = frozenset(
    {
        "CodeRabbit",
        "claude-review",
    }
)


def _gh_json(path: str) -> object:
    proc = subprocess.run(
        ["gh", "api", path, "--paginate"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        # A rate limit is the expected degraded case, not a bug, and this runs every lane round --
        # dumping GitHub's full paragraph hourly would train the reader to skip the whole section.
        # One line, and it says plainly that absence is now UNKNOWN rather than absent-of-problems:
        # a reporter that cannot report must not read as a clean bill of health.
        if "rate limit" in err.lower():
            raise SystemExit(
                "GitHub rate limit reached — absent-check status is UNKNOWN this round, "
                "which is not the same as clean. Re-run after the limit resets."
            )
        raise SystemExit(f"gh api {path} failed: {err}")
    # --paginate concatenates JSON documents; take the objects in order.
    out, decoder, idx = [], json.JSONDecoder(), 0
    text = proc.stdout.strip()
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        out.append(obj)
        idx = end
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
    return out


def reported_names(sha: str) -> set[str]:
    """Which check NAMES reported on this commit, at any conclusion including skipped."""
    names: set[str] = set()
    for page in _gh_json(f"repos/{REPO}/commits/{sha}/check-runs?per_page=100"):
        for run in page.get("check_runs", []):  # type: ignore[union-attr]
            names.add(run["name"])
    return {n for n in names if n not in IGNORED}


# The reference is built from FREQUENCY across recent merged PRs, and both simpler designs were
# tried against the real incident first and rejected by it:
#
#   * ONE reference PR. Failed outright. PR #90 merged with no Gate, and the newest merged PR at
#     the time (#93) had no Gate checks either -- the hold had already swallowed the yardstick, so
#     #90 was declared healthy. No "is this reference substantial" guard rescues it: any single PR
#     can be holed.
#   * The UNION across recent merged PRs. Caught #90 correctly (21 absent) but reported 25-26
#     absences on PRs that were entirely healthy, because the union sweeps in event-driven checks
#     -- keepalive rounds, Claude-token detection, label-gated jobs -- which legitimately do not
#     run on every PR. A check that cries wolf 25 times gets waived, which would have made this
#     tool worse than nothing.
#
# So a check is EXPECTED when it reported on at least `EXPECTED_FRACTION` of the reference PRs.
# That is the property actually wanted: not "has this ever run" but "does this normally run".
# Structural checks (the Gate's jobs) appear on nearly every PR; event-driven ones appear on few.
# No hardcoded list of check names anywhere -- that would be a second copy of the CI topology, and
# paired literals in this repo have an unbroken record of drifting apart.
REFERENCE_PRS = 12
EXPECTED_FRACTION = 0.75

# THE RATCHET, and it exists because dogfooding this tool caught it disarming itself.
#
# Frequency alone erodes. While `pr-00-gate.yml` sat held, every newly merged PR merged WITHOUT the
# Gate -- so after twelve such merges the Gate's checks no longer appeared on 75% of the reference
# window, stopped counting as "normally reporting", and their absence stopped being flagged. The
# expected set fell from 23 names to 14 and PR #91 was pronounced clean by a tool written to catch
# exactly that. A sustained outage is the case that matters most, and it was the one case the
# frequency rule could not see.
#
# `expected-checks.json` is the high-water mark: a name that has ever been expected stays expected
# until somebody DELETES ITS LINE, which is a visible act in a diff. Same shape as the mypy exempt
# ratchet in pyproject.toml -- a ceiling that can only come down deliberately -- and the same
# reason: an automatic downward move is indistinguishable from the defect.
RATCHET = Path(__file__).resolve().parent.parent / "config" / "expected-checks.json"


def reference_set(exclude_pr: int | None = None) -> tuple[set[str], list[int]]:
    """Check names that report on at least EXPECTED_FRACTION of recent merged PRs."""
    counts: dict[str, int] = {}
    contributors: list[int] = []
    for page in _gh_json(f"repos/{REPO}/pulls?state=closed&per_page=40"):
        for pr in page:  # type: ignore[union-attr]
            if not pr.get("merged_at") or pr["number"] == exclude_pr:
                continue
            if len(contributors) >= REFERENCE_PRS:
                break
            seen = reported_names(pr["head"]["sha"])
            if not seen:
                # A merged PR that reported nothing at all is itself an instance of the defect.
                # It must not dilute the denominator, or a run of holed PRs would erode the
                # expected set until nothing is expected -- the gate quietly disarming itself.
                continue
            contributors.append(pr["number"])
            for name in seen:
                counts[name] = counts.get(name, 0) + 1
    if not contributors:
        raise SystemExit("no merged PR in the last 40 closed PRs reported any check")
    observed = expected_from_counts(counts, len(contributors))
    return observed | ratchet_names(), contributors


def ratchet_names() -> set[str]:
    """Names that have ever been expected. Missing file means "nothing ratcheted yet", not zero."""
    if not RATCHET.is_file():
        return set()
    return set(json.loads(RATCHET.read_text(encoding="utf-8")).get("expected", []))


def expected_from_counts(counts: dict[str, int], contributors: int) -> set[str]:
    """Which names are EXPECTED, given how often each was seen. Pure, so a test holds the rule.

    The `max(2, ...)` floor matters: with a small sample a fractional threshold can fall to 1,
    which would promote every one-off event-driven check into the expected set and bury a real
    absence under noise. Two sightings is the least that can distinguish "normally runs" from
    "ran once".
    """
    threshold = max(2, int(contributors * EXPECTED_FRACTION))
    return {n for n, c in counts.items() if c >= threshold}


def check_pr(number: int) -> int:
    pr = _gh_json(f"repos/{REPO}/pulls/{number}")[0]
    head = pr["head"]["sha"]  # type: ignore[index]
    here = reported_names(head)
    expected, contributors = reference_set(exclude_pr=number)
    missing = sorted(expected - here)
    print(f"PR #{number} head {head[:8]}: {len(here)} check(s) reported")
    print(
        f"  reference: {len(expected)} check(s) seen on >={int(EXPECTED_FRACTION*100)}% of {len(contributors)} merged PR(s)"
    )
    if not missing:
        print("  every check that reports on the reference also reported here.")
        return 0
    print(f"\n  {len(missing)} CHECK(S) NEVER REPORTED on this head:")
    for name in missing:
        print(f"    - {name}")
    print(
        "\n  These are not failures — they are ABSENCES, which `gh pr checks` cannot show you and\n"
        "  which read exactly like success. Do not merge on the strength of a green list until each\n"
        "  is explained. A workflow held for review, cancelled, deleted or renamed all land here.\n"
        "  If an absence is deliberate (a workflow really was removed), say so in the PR and merge;\n"
        "  the reference set will rotate past it on its own."
    )
    return 1


def sweep() -> int:
    """FYI for the lane prerun: every open PR with an absent check, and every held run.

    Reports only. It never blocks and never accumulates: the output is recomputed each run from
    live state, so there is no queue to drain and nothing to mark as read.
    """
    expected, contributors = reference_set()
    print(
        f"  reference: {len(expected)} check(s) seen on >={int(EXPECTED_FRACTION*100)}% of {len(contributors)} merged PR(s)"
    )
    findings = 0
    for page in _gh_json(f"repos/{REPO}/pulls?state=open&per_page=50"):
        for pr in page:  # type: ignore[union-attr]
            missing = sorted(expected - reported_names(pr["head"]["sha"]))
            if missing:
                findings += 1
                print(f"  PR #{pr['number']}: {len(missing)} absent — {', '.join(missing[:4])}")
    for page in _gh_json(f"repos/{REPO}/actions/runs?per_page=100"):
        held = sorted(
            {
                r["path"]
                for r in page.get("workflow_runs", [])  # type: ignore[union-attr]
                if r.get("conclusion") == "action_required"
            }
        )
        for path in held:
            findings += 1
            print(f"  HELD: {path} — a run reached `action_required` and executed no jobs")
        break
    if not findings:
        print("  no absent checks and no held runs.")
    return 0


def update_ratchet() -> int:
    """Raise the high-water mark. Never lowers it — see RATCHET."""
    observed, contributors = reference_set()
    before = ratchet_names()
    after = before | observed
    RATCHET.parent.mkdir(parents=True, exist_ok=True)
    RATCHET.write_text(json.dumps({"expected": sorted(after)}, indent=1) + "\n", encoding="utf-8")
    added = sorted(after - before)
    print(f"ratchet: {len(before)} -> {len(after)} name(s) over {len(contributors)} merged PR(s)")
    for name in added:
        print(f"  + {name}")
    if not added:
        print("  (no new names; nothing removed — this command never removes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="assert every expected check REPORTED on this PR's head")
    g.add_argument(
        "--update-ratchet",
        action="store_true",
        help=(
            "add currently-observed check names to config/expected-checks.json. RAISES ONLY -- it "
            "never removes a name, because an automatic downward move is indistinguishable from "
            "the defect this tool exists to find. To drop a name, delete its line by hand."
        ),
    )
    g.add_argument(
        "--sweep",
        action="store_true",
        help="report absent checks across open PRs and any held runs (FYI, never blocking)",
    )
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.update_ratchet:
        return update_ratchet()
    return sweep() if args.sweep else check_pr(args.pr)


if __name__ == "__main__":
    raise SystemExit(main())
