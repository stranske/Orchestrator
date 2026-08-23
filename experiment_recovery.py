#!/usr/bin/env python3
"""Rebuild experiment directories from surviving git branches so aged-out experiments can evaluate.

WHY THIS EXISTS. `exp_abcd.followup` evaluates from an ON-DISK experiment directory (meta.json +
spec.md + per-arm diffs). The Brain holds 128 experiments with no `evaluations` row, and **not one of
them had a directory** — 192 directories existed, all belonging to already-evaluated experiments. So
the evidence read as lost.

(Correction worth keeping, because it is the exact mistake this codebase's CLAUDE.md warns about:
the first measurement of this used a HARDCODED `~/.codex/orchestrator/experiments` and reported the
experiments directory as empty. `exp_abcd.EXP_DIR` is TREE-RELATIVE — the live one is the mirror's
`experiments/`, which held 192 directories the whole time. The conclusion survived because it never
rested on that number: all 128 are 50.9-67.5 days old, so they are past `followup`'s `max_age_days`
window regardless of whether a directory exists. Read the module's constant, never a path you
assumed.)

It is not lost. Experiment arms are committed to `exp/<exp_id>-<agent>` branches in the SHARED
per-repo store (`~/.codex/orchestrator/repos/<slug>`), which the worktree GC deliberately preserves:
its 2026-08-18 record states that every branch ref lives in the shared store, so `git checkout` (or
here, `git diff`) reproduces the exact source after the worktree directory is gone. Measured
2026-08-21: **423 of 423 arms across all 128 experiments still have their branch** — 100% recoverable,
nothing dropped.

WHAT IT REBUILDS, AND FROM WHERE. Only recorded facts; nothing is invented.
  * `meta.json`  <- the Brain (`runs`: exp_id, agents, target, task_type) plus the branch itself.
  * per-arm diff <- `git diff $(git merge-base <branch> origin/<base>) <branch>`, which is the SAME
    computation `exp_abcd.collect` performs, against the branch instead of a deleted worktree. The
    merge-base anchor is load-bearing: diffing against a moved `origin/<base>` would expand the patch
    to the whole repository, and `objective_anchor` already recovers a pre-pin cut point the same way.
  * `spec.md`    <- the target issue body via `gh`, cached per subject (16 distinct subjects behind
    128 experiments, so this is 16 calls, not 128).

WHAT IT DOES NOT DO. It never writes `evaluations`, never records an anchor, and never judges — it
only restores the inputs so the EXISTING `exp_abcd` path can run unchanged. It refuses to fabricate a
missing arm: an experiment whose branch is gone is reported `unrecoverable`, never padded with an
empty diff, because an empty diff is a real and meaningful verdict (`apply-fail`/no-delivery) and
manufacturing one would forge machine ground truth.

CORRELATED EVIDENCE — READ BEFORE ACTING ON THE OUTPUT. The 128 experiments cover only **16 distinct
subjects**, and the top three account for 98 of them (manager-database-1283 x50, workflows-2710 x35,
workflows-2478 x13) because the fleet retried a stuck issue for weeks. CLAUDE.md §2 requires repeated
research on one subject to retain its subject weight and forbids treating correlated arms as
independent evidence. The downstream consumers already do this — `capability_effectiveness._arm_stats`
counts distinct subjects rather than attempts, and `relearn_quality` retains subject weight — so this
module deliberately does NOT dedupe. It reports the subject histogram instead, so the correlation is
visible at the point of use rather than silently baked in here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import exp_abcd
import feedback
import provision

DEFAULT_BASE = os.environ.get("ORCH_EXPERIMENT_RECOVERY_BASE", "main")
GH_TIMEOUT_S = int(os.environ.get("ORCH_EXPERIMENT_RECOVERY_GH_TIMEOUT", "60"))
GIT_TIMEOUT_S = int(os.environ.get("ORCH_EXPERIMENT_RECOVERY_GIT_TIMEOUT", "120"))


def _git(store: Path, *args: str, timeout: int = GIT_TIMEOUT_S) -> str:
    """Read-only git against the shared store. Never raises; callers treat '' as absent."""
    try:
        out = subprocess.run(
            ["git", "-C", str(store), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_store(repo: str) -> Path:
    return provision.REPOS_DIR / provision.repo_slug(repo)


def _canonical_repo(target: str) -> str:
    """`stranske/Workflows [exp tick-...]` -> `stranske/Workflows`; experiment targets carry a tag."""
    return str(target or "").split(" ")[0].split("#")[0].strip()


def unevaluated_experiments(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every experiment the Brain holds with no evaluation row, with its recorded arms.

    Deliberately UNBOUNDED by age — the whole point is to reach the aged-out ones that
    `research_subjects.EVALUABLE_WINDOW_DAYS` correctly excludes from the live cap.
    """
    db = conn or feedback._conn()
    rows = db.execute(
        "SELECT r.experiment_id, GROUP_CONCAT(DISTINCT r.agent), MAX(r.target), "
        "MAX(r.task_type), MAX(r.ts) FROM runs r "
        "WHERE r.experiment_id IS NOT NULL AND r.experiment_id<>'' "
        "AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.experiment_id=r.experiment_id) "
        "GROUP BY r.experiment_id ORDER BY MAX(r.ts) DESC"
    ).fetchall()
    out = []
    for exp_id, agents, target, task_type, ts in rows:
        out.append(
            {
                "exp_id": str(exp_id),
                "agents": [a for a in str(agents or "").split(",") if a],
                "target": str(target or ""),
                "repo": _canonical_repo(target),
                "task_type": str(task_type or "implement"),
                "ts": ts,
            }
        )
    return out


def subject_of(exp_id: str) -> str:
    """`tick-<ts>-<subject>` -> `<subject>`; the correlated-evidence key, not a unique id."""
    parts = str(exp_id).split("-", 2)
    if len(parts) == 3 and parts[0] == "tick" and parts[1].isdigit():
        return parts[2]
    return str(exp_id)


def arm_diff(repo: str, exp_id: str, agent: str, base: str = DEFAULT_BASE) -> str | None:
    """The arm's real delta, anchored at its merge-base. None means the branch is gone."""
    store = repo_store(repo)
    if not store.exists():
        return None
    branch = exp_abcd.exp_branch(exp_id, agent)
    if not _git(store, "rev-parse", "--verify", "--quiet", branch).strip():
        return None
    base_ref = f"origin/{base}"
    merge_base = _git(store, "merge-base", branch, base_ref).strip() or base_ref
    return _git(store, "--no-pager", "diff", merge_base, branch)


def _spec_for(target: str, *, cache: dict[str, str], gh_fn=None) -> str:
    """Issue body via gh, cached per subject. A missing body degrades to a stub, never to a crash."""
    key = _canonical_repo(target)
    number = ""
    if "#" in target:
        number = target.split("#", 1)[1].split(" ")[0].strip()
    ck = f"{key}#{number}"
    if ck in cache:
        return cache[ck]
    body = ""
    if key and number:
        if gh_fn is not None:
            body = gh_fn(key, number)
        else:
            try:
                out = subprocess.run(
                    ["gh", "issue", "view", number, "--repo", key, "--json", "title,body"],
                    capture_output=True,
                    text=True,
                    timeout=GH_TIMEOUT_S,
                )
                if out.returncode == 0:
                    data = json.loads(out.stdout or "{}")
                    body = f"# {data.get('title') or ''}\n\n{data.get('body') or ''}".strip()
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                body = ""
    if not body:
        # HONEST STUB, and it says so. Judges must not be handed a fabricated specification; a stub
        # that names its own provenance lets the reader discount that experiment's judge scores while
        # its OBJECTIVE anchor (which needs no spec) stays fully trustworthy.
        body = (
            f"[spec unavailable at recovery time for {ck or target}]\n\n"
            "This experiment's original spec.md was reclaimed with its worktree and the issue body "
            "could not be re-fetched. Objective anchors for this experiment remain valid (they are "
            "computed from the diff alone); judge scores against this stub are NOT comparable to "
            "judge scores from a real spec and should be read with that caveat."
        )
    cache[ck] = body
    return body


def restore_experiment(
    row: dict,
    *,
    base: str = DEFAULT_BASE,
    spec_cache: dict[str, str] | None = None,
    apply: bool = False,
    exp_dir: Path | None = None,
    gh_fn=None,
) -> dict:
    """Rebuild one experiment's directory from its branches. Read-only unless `apply`."""
    spec_cache = {} if spec_cache is None else spec_cache
    exp_id, repo = row["exp_id"], row["repo"]
    root = (exp_dir or exp_abcd.EXP_DIR) / exp_id
    arms, missing = {}, []
    for agent in row["agents"]:
        d = arm_diff(repo, exp_id, agent, base=base)
        if d is None:
            missing.append(agent)
        else:
            arms[agent] = d
    result = {
        "exp_id": exp_id,
        "repo": repo,
        "subject": subject_of(exp_id),
        "arms_recovered": sorted(arms),
        "arms_missing": sorted(missing),
        "nonempty_arms": sorted(a for a, d in arms.items() if d.strip()),
        "status": (
            "recoverable" if arms and not missing else ("partial" if arms else "unrecoverable")
        ),
        "applied": False,
    }
    if not apply or not arms:
        return result
    root.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": 2,
        "repo": repo,
        "base": base,
        "base_sha": None,
        "agents": sorted(arms),
        "exp_id": exp_id,
        "task_type": row.get("task_type") or "implement",
        "arms": [],
        "members": [],
        # PROVENANCE: this directory was reconstructed, not produced by prepare(). Anything reading
        # it should know the logs are synthetic and the spec may be a stub.
        "recovered_from_branches": True,
        "recovery_missing_arms": sorted(missing),
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    (root / "spec.md").write_text(_spec_for(row["target"], cache=spec_cache, gh_fn=gh_fn))
    for agent, diff in arms.items():
        (root / exp_abcd.exp_diff_path(agent)).write_text(diff)
        # followup requires a log per agent and reads only its MTIME (idle check). A recovered arm
        # has finished by definition, so an explicit marker is honest and satisfies the gate without
        # pretending to be captured agent output.
        log_p = root / exp_abcd.exp_log_path(agent)
        log_p.write_text(
            f"[recovered from branch {exp_abcd.exp_branch(exp_id, agent)}; "
            "original agent log was reclaimed with the worktree]\n"
        )
        # STAMP THE LOG WITH WHEN THE ARM ACTUALLY RAN, not when recovery wrote the file.
        # `followup` reads log mtime as an idleness signal ("is this arm still working?"), and a
        # freshly-written log claims the arm is live right now — which is false for work that
        # finished 50+ days ago, and makes every recovered experiment skip as `still-running`
        # forever. The Brain's recorded `runs.ts` is the truthful value, so use it.
        # meta.json's mtime is deliberately left FRESH: it means "when this record was
        # reconstructed", which is a different question and is what `max_age_days` asks.
        if row.get("ts"):
            try:
                os.utime(log_p, (float(row["ts"]), float(row["ts"])))
            except (OSError, TypeError, ValueError):
                pass
    result["applied"] = True
    return result


def plan(
    *,
    conn: sqlite3.Connection | None = None,
    base: str = DEFAULT_BASE,
    limit: int | None = None,
) -> dict:
    """Read-only recoverability report over every unevaluated experiment."""
    rows = unevaluated_experiments(conn)
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    results = [restore_experiment(r, base=base, apply=False) for r in rows]
    counts = Counter(r["status"] for r in results)
    subjects = Counter(r["subject"] for r in results)
    return {
        "experiments": len(results),
        "counts": dict(counts),
        "arms_recovered": sum(len(r["arms_recovered"]) for r in results),
        "arms_missing": sum(len(r["arms_missing"]) for r in results),
        "distinct_subjects": len(subjects),
        "subject_histogram": subjects.most_common(),
        "results": results,
    }


def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="exp-recovery-") as td:
        root = Path(td)
        # A real git repo standing in for the shared per-repo store.
        store = root / "store"
        store.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        def run(*a):
            subprocess.run(
                ["git", "-C", str(store), *a], check=True, capture_output=True, text=True, env=env
            )

        run("init", "-q", "-b", "main")
        (store / "f.txt").write_text("base\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        exp_id = "tick-1782960020-owner-repo-1"
        br = exp_abcd.exp_branch(exp_id, "codex")
        run("checkout", "-q", "-b", br)
        (store / "f.txt").write_text("base\ncandidate\n")
        run("add", "-A")
        run("commit", "-qm", "arm")
        run("checkout", "-q", "main")
        # Base moves AFTER the arm branched — the drift that makes a naive diff swallow the repo.
        (store / "unrelated.txt").write_text("later\n")
        run("add", "-A")
        run("commit", "-qm", "later")
        run("update-ref", "refs/remotes/origin/main", "HEAD")

        orig_repos = provision.REPOS_DIR
        provision.REPOS_DIR = root
        try:
            slug = provision.repo_slug("owner/repo")
            (root / slug).symlink_to(store)
            d = arm_diff("owner/repo", exp_id, "codex")
            assert d is not None and "candidate" in d, d
            # MERGE-BASE ANCHORING: the arm never touched unrelated.txt, so a diff that mentions it
            # is anchored at the moved base and would misattribute later work to this candidate.
            assert (
                "unrelated.txt" not in d
            ), "diff must be anchored at the merge-base, not origin/base"
            # A branch that does not exist is REPORTED, never invented as an empty diff.
            assert arm_diff("owner/repo", exp_id, "vibe") is None
            assert arm_diff("owner/repo", "no-such-exp", "codex") is None

            row = {
                "exp_id": exp_id,
                "agents": ["codex", "vibe"],
                "target": "owner/repo#1",
                "repo": "owner/repo",
                "task_type": "implement",
                "ts": 0,
            }
            dry = restore_experiment(row, apply=False)
            assert dry["status"] == "partial", dry
            assert dry["arms_recovered"] == ["codex"] and dry["arms_missing"] == ["vibe"], dry
            assert dry["applied"] is False
            expdir = root / "experiments"
            assert not expdir.exists(), "read-only plan must not create anything"

            got = restore_experiment(
                row,
                apply=True,
                exp_dir=expdir,
                gh_fn=lambda repo, num: "# Title\n\nreal spec body",
            )
            assert got["applied"] is True
            edir = expdir / exp_id
            meta = json.loads((edir / "meta.json").read_text())
            assert meta["agents"] == ["codex"] and meta["recovered_from_branches"] is True, meta
            assert meta["recovery_missing_arms"] == ["vibe"], meta
            assert "real spec body" in (edir / "spec.md").read_text()
            assert "candidate" in (edir / exp_abcd.exp_diff_path("codex")).read_text()
            assert (edir / exp_abcd.exp_log_path("codex")).exists(), "followup needs a log per arm"
            # The log must claim the arm finished WHEN IT RAN. A recovery-time mtime reads as
            # "still running" to followup and every recovered experiment skips forever.
            row_ts = {**row, "ts": 1_700_000_000}
            shutil.rmtree(edir)
            restore_experiment(row_ts, apply=True, exp_dir=expdir, gh_fn=lambda r, n: "spec")
            log_mtime = (edir / exp_abcd.exp_log_path("codex")).stat().st_mtime
            assert abs(log_mtime - 1_700_000_000) < 2, log_mtime
            # ...while meta.json stays FRESH, because its mtime answers a different question
            # (when was this reconstructed) and gates followup's max_age_days window.
            import time as _time

            assert _time.time() - (edir / "meta.json").stat().st_mtime < 300, "meta must stay fresh"
            # The missing arm must leave NO artifact — a zero-byte diff is a real verdict elsewhere.
            assert not (edir / exp_abcd.exp_diff_path("vibe")).exists(), "must not fabricate an arm"

            # An unfetchable spec degrades to a stub that NAMES ITSELF, so judge scores from it stay
            # discountable and the objective anchor (diff-only) is unaffected.
            stub = _spec_for("owner/repo#7", cache={}, gh_fn=lambda r, n: "")
            assert "spec unavailable" in stub and "Objective anchors" in stub, stub

            assert subject_of("tick-1782960020-owner-repo-1") == "owner-repo-1"
            assert subject_of("labelinv1") == "labelinv1"
        finally:
            provision.REPOS_DIR = orig_repos
            shutil.rmtree(root / provision.repo_slug("owner/repo"), ignore_errors=True)
    print(
        "experiment_recovery.py selftest: OK (merge-base anchoring, missing-arm refusal, "
        "read-only plan, self-naming spec stub, provenance in meta)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true", help="write the rebuilt experiment directories")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    report = plan(base=args.base, limit=args.limit)
    if args.apply:
        cache: dict[str, str] = {}
        applied = []
        for row in (
            unevaluated_experiments()[: args.limit] if args.limit else unevaluated_experiments()
        ):
            applied.append(restore_experiment(row, base=args.base, spec_cache=cache, apply=True))
        report["results"] = applied
        report["applied"] = sum(1 for r in applied if r["applied"])
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"experiments={report['experiments']} counts={report['counts']} "
            f"arms_recovered={report['arms_recovered']} arms_missing={report['arms_missing']}"
        )
        print(
            f"distinct subjects={report['distinct_subjects']} (correlated evidence — see module docstring)"
        )
        for subject, n in report["subject_histogram"][:10]:
            print(f"   {n:4d}x  {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
