#!/usr/bin/env python3
"""objective_anchor.py — machine-ground-truth anchors for judge calibration (item 12b).

WHY: the calibration model and judge-reliability's human-error leg both need TRUSTED scores to
regress judge scores against — and no human code-review gate is available in this deployment (see LOCAL_POLICY.md), so the
trusted label must come from the machine. Same philosophy as the Brain's durability label: cheap,
un-gameable, produced as a side effect of work the fleet already does.

DECISIVE-SIGNALS-ONLY policy: an arm gets an anchor ONLY when the evidence is unambiguous —
its patch does not apply (1.0), does not compile (2.0), its own changed tests fail in the patched
tree (2.5), its tests pass but ALSO pass on the un-patched base (hollow, 3.5), or its tests pass
and correctly FAIL on base (deliberate-break verified, 8.0 — local_verify's philosophy scoped to
the arm's own test files). Ambiguous arms (no test changes, missing env, pytest timeouts) get NO
anchor: noisy anchors would poison exactly the calibration they exist to feed.

Anchors land in the human_calibration table via human_calibration.record_anchor with
note="objective:<label>", so judge_reliability and the calibration model consume them unchanged.
Hooked into exp_abcd.evaluate() (env gate ORCH_OBJECTIVE_ANCHOR, default ON, time budget
ORCH_OBJECTIVE_ANCHOR_BUDGET_S); the CLI supports per-experiment and --latest backfill runs.
`--selftest` is fully offline (signals injected)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import feedback
import human_calibration
import provision

EXP_DIR = Path(
    os.environ.get("ORCH_EXP_DIR", Path(__file__).resolve().parent / "experiments")
)
TEST_FILE_RE = re.compile(r"(^|/)(tests?/|test_[^/]+\.py$|[^/]+_test\.py$)")
PYTEST_TIMEOUT_S = 240
DEFAULT_BUDGET_S = 600.0

SCORES = {
    "apply-fail": 1.0,
    "compile-fail": 2.0,
    "tests-fail": 2.5,
    "hollow-tests": 3.5,
    "tests-pass-break-verified": 8.0,
}


def score_from_signals(sig: dict) -> tuple[float, str] | None:
    """Map raw arm signals to (anchor_score, label), or None when not decisive. Pure.

    applies is tri-state: False = the patch demonstrably fails to apply (decisive);
    None = the environment could not even try (no clone / worktree failure) — NOT decisive."""
    if sig.get("applies") is None:
        return None
    if sig.get("applies") is False:
        return SCORES["apply-fail"], "apply-fail"
    if sig.get("compile_ok") is False:
        return SCORES["compile-fail"], "compile-fail"
    probe = sig.get("probe")  # None | {"patched_pass": bool, "base_pass": bool}
    if not probe:
        return None
    if not probe.get("patched_pass"):
        return SCORES["tests-fail"], "tests-fail"
    if probe.get("base_pass"):
        return SCORES["hollow-tests"], "hollow-tests"
    return SCORES["tests-pass-break-verified"], "tests-pass-break-verified"


def _run(cmd: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _changed_files(patch_text: str) -> list[str]:
    out = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                out.append(path)
    return out


def arm_signals(
    repo: str, base: str, patch_path: Path, *, pytest_timeout: int = PYTEST_TIMEOUT_S
) -> dict:
    """Raw objective signals for one arm's persisted diff, against the LOCAL canonical clone.

    Runs in throwaway `git worktree`s of the base ref; the clone itself is never mutated. Env
    problems (missing clone, pytest env rc not in {0,1}, timeouts) leave the signal ambiguous
    (probe=None + detail) — score_from_signals then declines to anchor."""
    repo_dir = provision.REPOS_DIR / provision.repo_slug(repo)
    sig: dict = {"applies": False, "compile_ok": None, "probe": None, "detail": ""}
    if not repo_dir.exists():
        sig["detail"] = f"no local clone at {repo_dir}"
        sig["applies"] = None  # unknown, not decisive
        return sig
    # Diffs are collected against origin/<base> (collect()'s merge-base), and the clone's LOCAL
    # <base> branch can lag origin (observed live: main != origin/main) — apply against the same
    # ref the diff was cut from or clean patches false-fail.
    remote_ref = f"origin/{base}"
    probe_ref = _run(["git", "rev-parse", "--verify", "--quiet", remote_ref], repo_dir)
    if probe_ref.returncode == 0:
        base = remote_ref
    patch_text = patch_path.read_text(errors="replace")
    changed = _changed_files(patch_text)
    test_files = [f for f in changed if TEST_FILE_RE.search(f)]
    py_files = [f for f in changed if f.endswith(".py")]
    tmp = Path(tempfile.mkdtemp(prefix="objective-anchor-"))
    wt, base_wt = tmp / "wt", tmp / "base"
    try:
        add = _run(["git", "worktree", "add", "--detach", str(wt), base], repo_dir)
        if add.returncode != 0:
            sig["applies"] = None
            sig["detail"] = f"worktree add failed: {(add.stderr or '')[:200]}"
            return sig
        applied = _run(["git", "apply", "--whitespace=nowarn", str(patch_path)], wt)
        if applied.returncode != 0:
            sig["detail"] = (applied.stderr or "")[:200]
            return sig  # applies stays False — decisive apply-fail
        sig["applies"] = True
        present_py = [f for f in py_files if (wt / f).exists()]
        if present_py:
            comp = _run([sys.executable, "-m", "py_compile", *present_py], wt)
            sig["compile_ok"] = comp.returncode == 0
            if comp.returncode != 0:
                sig["detail"] = (comp.stderr or "")[:200]
                return sig  # decisive compile-fail
        present_tests = [f for f in test_files if (wt / f).exists()]
        if present_tests:
            patched = _run(
                [sys.executable, "-m", "pytest", "-x", "-q", *present_tests],
                wt,
                timeout=pytest_timeout,
            )
            if patched.returncode not in (0, 1):
                sig["detail"] = f"pytest env rc={patched.returncode}"
                return sig  # env problem — not decisive
            # Overlay the arm's test files onto a CLEAN base: hollow tests pass without the impl.
            add2 = _run(
                ["git", "worktree", "add", "--detach", str(base_wt), base], repo_dir
            )
            if add2.returncode != 0:
                sig["detail"] = "base worktree failed"
                return sig
            overlaid = []
            for f in present_tests:
                dst = base_wt / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((wt / f).read_bytes())
                overlaid.append(f)
            based = _run(
                [sys.executable, "-m", "pytest", "-x", "-q", *overlaid],
                base_wt,
                timeout=pytest_timeout,
            )
            if based.returncode not in (0, 1):
                sig["detail"] = f"base pytest env rc={based.returncode}"
                return sig
            sig["probe"] = {
                "patched_pass": patched.returncode == 0,
                "base_pass": based.returncode == 0,
            }
    except subprocess.TimeoutExpired:
        sig["probe"] = None
        sig["detail"] = "pytest timeout"
    finally:
        for w in (wt, base_wt):
            if w.exists():
                try:
                    _run(["git", "worktree", "remove", "--force", str(w)], repo_dir)
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
    return sig


def _arm_base(repo: str, base: str, exp_id: str, artifact_id: str) -> str | None:
    """Recover an arm's TRUE cut point when meta.json predates base_sha pinning (2026-07-08):
    experiment branches (exp/<exp_id>-<agent>, see exp_abcd.exp_branch) persist in the canonical
    clone after worktree GC, so merge-base(origin/<base>, branch) is the commit the arm actually
    forked from. Applying the arm's diff there instead of today's origin/<base> removes the
    base-drift false apply-fails. Returns a SHA, or None when the branch is gone (caller falls
    back to origin/<base> resolution inside arm_signals)."""
    repo_dir = provision.REPOS_DIR / provision.repo_slug(repo)
    if not repo_dir.exists():
        return None
    branch = f"exp/{exp_id}-{artifact_id}"
    probe = _run(["git", "rev-parse", "--verify", "--quiet", branch], repo_dir)
    if probe.returncode != 0:
        return None
    mb = _run(["git", "merge-base", f"origin/{base}", branch], repo_dir)
    if mb.returncode != 0:
        return None
    sha = (mb.stdout or "").strip()
    return sha or None


def _existing_anchor_refs() -> set[str]:
    try:
        with feedback._conn() as c:
            rows = c.execute("SELECT ref FROM human_calibration").fetchall()
    except Exception:
        return set()
    return {str(r[0]) for r in rows if r and r[0]}


def anchor_experiment(
    exp_id: str,
    *,
    apply: bool = True,
    signals_fn=arm_signals,
    exp_dir: Path | None = None,
    budget_s: float | None = None,
) -> dict:
    """Anchor every decisive arm of one experiment; skip ambiguous/already-anchored arms."""
    if budget_s is None:
        try:
            budget_s = float(
                os.environ.get("ORCH_OBJECTIVE_ANCHOR_BUDGET_S", "") or DEFAULT_BUDGET_S
            )
        except ValueError:
            budget_s = DEFAULT_BUDGET_S
    started = time.time()
    edir = (exp_dir or EXP_DIR) / exp_id
    meta = json.loads((edir / "meta.json").read_text())
    # Base resolution, fairest-first (origin/<base> drifts on active repos and applying an arm's
    # diff to a moved base false-fails as apply-fail): (1) the pinned cut SHA (prepare() records
    # base_sha since 2026-07-08); (2) per-arm merge-base against the persisted exp branch
    # (_arm_base, for pre-pin experiments); (3) origin/<base> as the last resort.
    repo, base_name = meta["repo"], meta.get("base") or "main"
    pinned = meta.get("base_sha")
    existing = _existing_anchor_refs()
    out: dict = {"exp_id": exp_id, "anchored": [], "skipped": []}
    # Imported lazily because exp_abcd imports this module only from evaluate().
    import exp_abcd

    for member in exp_abcd.experiment_members(meta):
        agent = member["agent"]
        identity = member["member_id"]
        artifact_member = None if member["legacy"] else identity
        ref = f"{exp_id}:{identity}"
        patch = edir / exp_abcd.exp_diff_path(agent, artifact_member)
        common = {
            "agent": agent,
            "arm_id": member.get("arm_id"),
            "member_id": identity,
            "profile_id": member.get("profile_id"),
        }
        if not patch.exists() or patch.stat().st_size == 0:
            out["skipped"].append({**common, "reason": "no-diff"})
            continue
        if ref in existing:
            out["skipped"].append({**common, "reason": "already-anchored"})
            continue
        if time.time() - started > budget_s:
            out["skipped"].append({**common, "reason": "budget-exhausted"})
            continue
        arm_base = pinned or _arm_base(repo, base_name, exp_id, identity) or base_name
        sig = signals_fn(repo, arm_base, patch)
        decisive = score_from_signals(sig)
        if not decisive:
            out["skipped"].append(
                {**common, "reason": sig.get("detail") or "not-decisive"}
            )
            continue
        score, label = decisive
        result = human_calibration.record_anchor(
            experiment_id=exp_id,
            implementer=identity,
            score=score,
            note=f"objective:{label}",
            apply=apply,
            confirm_anchor=ref if apply else None,
            arm_id=member.get("arm_id"),
            member_id=identity if not member["legacy"] else None,
            profile_id=member.get("profile_id"),
            agent=agent,
        )
        out["anchored"].append(
            {**common, "score": score, "label": label, "applied": result["applied"]}
        )
    return out


def _latest_exp_ids(n: int) -> list[str]:
    dirs = [d for d in EXP_DIR.iterdir() if d.is_dir() and (d / "meta.json").exists()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [d.name for d in dirs[: max(0, n)]]


def _selftest() -> None:
    # Pure mapping: every decisive path + the not-decisive refusals.
    assert score_from_signals({"applies": False}) == (1.0, "apply-fail")
    # applies=None (env could not even try) must NOT be decisive — the falsy trap:
    assert score_from_signals({"applies": None, "detail": "no clone"}) is None
    assert score_from_signals({"applies": True, "compile_ok": False}) == (2.0, "compile-fail")
    assert score_from_signals({"applies": True, "compile_ok": True, "probe": None}) is None
    assert score_from_signals(
        {"applies": True, "probe": {"patched_pass": False, "base_pass": False}}
    ) == (2.5, "tests-fail")
    assert score_from_signals(
        {"applies": True, "probe": {"patched_pass": True, "base_pass": True}}
    ) == (3.5, "hollow-tests")
    assert score_from_signals(
        {"applies": True, "probe": {"patched_pass": True, "base_pass": False}}
    ) == (8.0, "tests-pass-break-verified")
    assert _changed_files("+++ b/a.py\n+++ b/tests/test_a.py\n+++ /dev/null\n") == [
        "a.py",
        "tests/test_a.py",
    ]
    assert TEST_FILE_RE.search("tests/test_x.py") and TEST_FILE_RE.search("pkg/x_test.py")
    assert not TEST_FILE_RE.search("src/contest.py")

    # Recording path with injected signals + temp DB: decisive arms anchored, ambiguous skipped,
    # second run skips already-anchored.
    tmp = Path(tempfile.mkdtemp(prefix="objective-anchor-selftest-"))
    old_db = feedback.DB_PATH
    try:
        feedback.DB_PATH = tmp / "t.db"
        edir = tmp / "exps" / "EXP1"
        edir.mkdir(parents=True)
        (edir / "meta.json").write_text(
            json.dumps(
                {"repo": "o/r", "base": "main", "agents": ["good", "broken", "vague", "empty"]}
            )
        )
        for agent in ("good", "broken", "vague"):
            (edir / f"diff-{agent}.patch").write_text("+++ b/x.py\n")
        (edir / "diff-empty.patch").write_text("")
        fake = {
            "good": {"applies": True, "compile_ok": True,
                     "probe": {"patched_pass": True, "base_pass": False}},
            "broken": {"applies": False, "detail": "does not apply"},
            "vague": {"applies": True, "compile_ok": True, "probe": None,
                      "detail": "pytest env rc=4"},
        }
        res = anchor_experiment(
            "EXP1", signals_fn=lambda repo, base, p: fake[p.stem.split("-", 1)[1]],
            exp_dir=tmp / "exps",
        )
        got = {a["agent"]: (a["score"], a["label"]) for a in res["anchored"]}
        assert got == {
            "good": (8.0, "tests-pass-break-verified"),
            "broken": (1.0, "apply-fail"),
        }, res
        reasons = {s["agent"]: s["reason"] for s in res["skipped"]}
        assert reasons["vague"] == "pytest env rc=4" and reasons["empty"] == "no-diff", res
        again = anchor_experiment(
            "EXP1", signals_fn=lambda repo, base, p: fake[p.stem.split("-", 1)[1]],
            exp_dir=tmp / "exps",
        )
        again_reasons = {s["agent"]: s["reason"] for s in again["skipped"]}
        assert not again["anchored"], again
        assert again_reasons["good"] == "already-anchored", again
        # anchors are consumable by the calibration stack unchanged
        anchors = human_calibration.parse_human_anchors(
            [(1, "EXP1:good", json.dumps({"experiment_id": "EXP1", "implementer": "good", "score": 8.0}), "objective:x")]
        )
        assert anchors and anchors[0]["score"] == 8.0, anchors
        # zero budget: arms are skipped as budget-exhausted, signals never run
        edir2 = tmp / "exps" / "EXP2"
        edir2.mkdir(parents=True)
        (edir2 / "meta.json").write_text(
            json.dumps({"repo": "o/r", "base": "main", "agents": ["good"]})
        )
        (edir2 / "diff-good.patch").write_text("+++ b/x.py\n")

        def _must_not_run(*_a):
            raise AssertionError("signals must not run when budget is exhausted")

        res_budget = anchor_experiment(
            "EXP2", signals_fn=_must_not_run, exp_dir=tmp / "exps", budget_s=-1
        )
        assert not res_budget["anchored"], res_budget
        assert res_budget["skipped"][0]["reason"] == "budget-exhausted", res_budget
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)
    # merge-base fallback: an old arm's true cut point is recovered from its persisted exp
    # branch even after origin/<base> moved on (throwaway git repo; provision.REPOS_DIR swapped).
    gtmp = Path(tempfile.mkdtemp(prefix="objective-anchor-git-"))
    old_repos = provision.REPOS_DIR
    try:
        provision.REPOS_DIR = gtmp
        rdir = gtmp / provision.repo_slug("o/r")
        rdir.mkdir(parents=True)
        gid = ["-c", "user.email=t@t", "-c", "user.name=t"]
        assert _run(["git", "init", "-q", "-b", "main"], rdir).returncode == 0
        (rdir / "f.txt").write_text("one\n")
        _run(["git", "add", "."], rdir)
        _run(["git", *gid, "commit", "-qm", "c1"], rdir)
        cut_sha = _run(["git", "rev-parse", "HEAD"], rdir).stdout.strip()
        _run(["git", "branch", "exp/EXPG-codex"], rdir)
        (rdir / "f.txt").write_text("two\n")  # base moves on after the cut
        _run(["git", *gid, "commit", "-aqm", "c2"], rdir)
        _run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], rdir)
        assert _arm_base("o/r", "main", "EXPG", "codex") == cut_sha, "cut point recovered"
        assert _arm_base("o/r", "main", "EXPG", "ghost") is None, "gone branch -> None"
        assert _arm_base("missing/repo", "main", "EXPG", "codex") is None
    finally:
        provision.REPOS_DIR = old_repos
        shutil.rmtree(gtmp, ignore_errors=True)

    print(
        "objective_anchor.py selftest: OK (decisive score map, refusal on ambiguity, "
        "record + already-anchored dedupe, anchor consumability, merge-base cut-point fallback)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record machine-ground-truth anchors for experiment arms (item 12b)."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--exp-id", help="Anchor one experiment id.")
    parser.add_argument("--latest", type=int, help="Anchor the N most recent experiments.")
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not record.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    exp_ids = []
    if args.exp_id:
        exp_ids.append(args.exp_id)
    if args.latest:
        exp_ids.extend(_latest_exp_ids(args.latest))
    if not exp_ids:
        parser.error("need --exp-id or --latest N (or --selftest)")
    results = [
        anchor_experiment(e, apply=not args.dry_run) for e in dict.fromkeys(exp_ids)
    ]
    if args.as_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for res in results:
            print(
                f"objective_anchor {res['exp_id']}: anchored={len(res['anchored'])} "
                f"skipped={len(res['skipped'])}"
            )
            for a in res["anchored"]:
                print(f"  + {a['agent']}: {a['score']:g} ({a['label']})")
            for s in res["skipped"]:
                print(f"  - {s['agent']}: {s['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
