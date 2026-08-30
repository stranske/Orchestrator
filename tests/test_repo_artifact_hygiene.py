"""Per-run CI artifacts must not be tracked, and the patterns that keep them out must not drift.

The scar: `langsmith-fleet-worker-attempt.json` sat TRACKED on main until 2026-08-23, holding the
execution telemetry of whichever PR last merged (#61: `gpt-5.6-terra`, `operation_role: worker`).
`stranske/Workflows` `reusable-codex-run.yml` rewrites it into the checkout root on every agent
round, so the autofix bot re-committed it on the next PR and #66 hit an add/add conflict on a path
neither side had a reason to touch.

Two rules in `CLAUDE.md` name why the committed copy was wrong, not merely inconvenient. §1's
tool-vs-evidence split puts one run's telemetry outside the tree. §2 requires execution provenance
to reach the learner through `feedback.py`'s tables; a git-churned copy of a `worker` attempt with a
resolved `selected_model` is a second, unmanaged store of exactly that evidence.

What is asserted here, and why each half is load-bearing:

1.  **The whole emitted family is ignored.** A literal filename would be escaped by the next
    artifact name, and the runner names this file after the ROLE it recorded — a verifier or
    evaluator attempt arrives as a sibling.
2.  **Nothing in the family is tracked.** Ignoring is not untracking. `git add -A` skips an ignored
    UNTRACKED path but stages an ignored TRACKED one, so the pattern alone would have left the
    original defect running: verified both ways against a scratch repository on 2026-08-23.
3.  **Sources and docs are NOT swallowed.** The negative half, and the reason the pattern is bounded
    to the two extensions `langsmith-fleet/v1` actually emits rather than a bare `*langsmith-fleet*`.
    Over-broad is the other failure: it would silently untrack a future contract doc, and the tree
    already carries `langsmith_*.py` sources one hyphen away from the artifact namespace.

Every question here is put to GIT, never to a reimplementation of gitignore precedence. `.gitignore`
records why: an early version of it put trailing comments on pattern lines, silently making every
pattern inert and staging 795 files instead of 141, and the rule taken from that is to verify with
`git check-ignore` and never with a hand-rolled parser. A parser here would agree with itself.

DELIBERATE BREAK -> REVERT, performed 2026-08-23, in all three directions the patterns can fail:

1.  Too narrow -- replacing the pattern pair with the bare literal
    `langsmith-fleet-worker-attempt.json` failed
    `test_emitted_artifact_family_is_ignored[langsmith-fleet.ndjson]` and its three siblings.
2.  Too broad by kind -- widening to `*langsmith-fleet*` failed
    `test_sources_and_docs_are_not_swallowed[docs/langsmith-fleet-contract.md]`.
3.  Too broad by DEPTH -- dropping the leading slash failed
    `test_sources_and_docs_are_not_swallowed[docs/contracts/schemas/langsmith-fleet-v1.schema.json]`.
    This one was found by a concurrent session on the upstream fix rather than by this file's first
    draft, which bounded by extension and thought that was enough. It is not: a contract schema is
    a .json, and stranske/Workflows tracks that exact path.

Each break was reverted to a byte-identical file and every case passed again.
"""

from __future__ import annotations

import subprocess

import pytest

import env_prereq

# Repo-root files, resolved through the shared rule rather than a local `parent.parent`:
# these tests live in `tests/` while the things they assert on live at the checkout root.
import paths

HERE = paths.REPO_ROOT

# What the `langsmith-fleet/v1` emitters can leave in a working tree. The first entry is the file
# that actually shipped on main; the rest are the sibling names the same producers already use
# elsewhere (`langsmith_fetch.DEFAULT_ARTIFACT_NAME`, its `langsmith-fleet-rollup-` prefix, the
# reusable-CI path `artifacts/langsmith/`) plus the role-swap this runner's naming scheme invites.
EMITTED_ARTIFACTS = (
    "langsmith-fleet-worker-attempt.json",
    "langsmith-fleet.ndjson",
    "langsmith-fleet-rollup-1.ndjson",
    "langsmith-fleet-verifier-attempt.json",
    "artifacts/langsmith/langsmith-fleet.ndjson",
    # A SECOND PRODUCER, same hazard, added 2026-08-24 after it actually happened. The family
    # above is written by reusable-codex-run.yml; these are written by setuptools, because a CI
    # step runs `pip install -e .` and generates metadata beside the sources. The name is UNKNOWN
    # precisely because pyproject.toml declares no [project] on purpose -- so the debris is named
    # after the absence of a decision, which is not a string anyone would think to ignore in
    # advance. On PR #113 the repo's own autofix bot committed all four generated files; CodeRabbit
    # caught it, nothing in this suite did. BOTH LOCATIONS are listed deliberately: a checkout
    # builds into src/, and the EXEC MIRROR IS FLAT, so the same build there lands at the root.
    "src/UNKNOWN.egg-info/PKG-INFO",
    "UNKNOWN.egg-info/PKG-INFO",
)

# Committable neighbours one hyphen away from the artifact namespace. Two things keep them safe:
# sources here use UNDERSCORES, and the debris patterns are ROOT-ANCHORED, so nothing at depth is
# reached. Bounding by extension alone was NOT enough -- a contract schema is a .json.
MUST_STAY_COMMITTABLE = (
    "langsmith_pull.py",
    "langsmith_direct.py",
    "langsmith_fetch.py",
    "docs/langsmith-fleet-contract.md",
    # The near-miss that made the patterns root-anchored. stranske/Workflows TRACKS exactly this
    # path, and this tree already keeps its sibling artifact-manifest-v1.schema.json, so an
    # unanchored `langsmith-fleet*.json` would silently untrack a load-bearing contract file.
    "docs/contracts/schemas/langsmith-fleet-v1.schema.json",
)


def require_git() -> None:
    """Skip, naming what is missing, when git cannot answer for this tree."""
    env_prereq.require(env_prereq.git_repo_absent())


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=HERE, capture_output=True, text=True, timeout=60, check=False
    )


def is_ignored(path: str) -> bool:
    """Ask git, not a parser. `check-ignore -q` exits 0 when the path is ignored, 1 when it is not."""
    proc = git("check-ignore", "-q", "--", path)
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed on {path!r} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.returncode == 0


@pytest.mark.parametrize("artifact", EMITTED_ARTIFACTS)
def test_emitted_artifact_family_is_ignored(artifact: str):
    require_git()
    assert is_ignored(artifact), (
        f"{artifact} is not ignored. stranske/Workflows reusable-codex-run.yml writes this family "
        "into the checkout root and its `git add -A` step then stages whatever the agent left "
        "behind, so an unignored member is re-committed on every PR and collides with the copy the "
        "last merge left on main — that is what broke PR #66. A literal filename is escaped by the "
        "next artifact name: keep .gitignore's `langsmith-fleet*.json` / `langsmith-fleet*.ndjson` "
        "pair, which covers the family without reaching sources or docs."
    )


def test_no_emitted_artifact_is_tracked():
    require_git()
    proc = git("ls-files", "--", "*langsmith-fleet*")
    assert proc.returncode == 0, f"git ls-files failed: {proc.stderr.strip()}"
    tracked = [line for line in proc.stdout.splitlines() if line.strip()]
    assert not tracked, (
        f"tracked langsmith-fleet artifact(s): {tracked}. Ignoring is not untracking — `git add -A` "
        "skips an ignored UNTRACKED path but stages an ignored TRACKED one, so .gitignore does "
        "nothing for a path still in the index. Untrack with `git rm --cached <path>`; the Actions "
        "upload-artifact step in reusable-codex-run.yml is the real transport, so nothing is lost. "
        "A per-run `operation_role: worker` record in git is also a second, unmanaged store of the "
        "provenance CLAUDE.md §2 routes through feedback.py's tables."
    )


def test_no_build_metadata_is_tracked():
    """Ignoring is not untracking, and this family proves why the distinction needs its own test.

    The four `src/UNKNOWN.egg-info/*` files were COMMITTED before they were ignored, and adding
    the pattern afterwards does nothing to a path git already tracks -- `git check-ignore` would
    have said "ignored" while `git ls-files` still listed them, so the sibling ignore test above
    can pass on a repo that is still carrying the debris. Globbed rather than named: setuptools
    picks the directory name from the distribution, so it is only UNKNOWN while pyproject.toml
    declares no [project], and pinning the literal would stop matching the moment that changes.
    """
    require_git()
    proc = git("ls-files", "--", "*.egg-info/*", "*.egg-info")
    assert proc.returncode == 0, f"git ls-files failed: {proc.stderr.strip()}"
    tracked = [line for line in proc.stdout.splitlines() if line.strip()]
    assert not tracked, (
        f"tracked build metadata: {tracked}. Generated by `pip install -e .` in CI and then "
        "committed by the autofix bot, which stages whatever the working tree contains. Untrack "
        "with `git rm -r --cached <dir>`; the .gitignore entry alone will not remove it."
    )


@pytest.mark.parametrize("path", MUST_STAY_COMMITTABLE)
def test_sources_and_docs_are_not_swallowed(path: str):
    require_git()
    assert not is_ignored(path), (
        f"{path} is ignored, so the artifact pattern has grown past the artifacts. Over-broad is "
        "the other failure direction, and it is the easier mistake: drop the leading slash and a "
        "gitignore pattern matches at EVERY depth, so `langsmith-fleet*.json` swallows a tracked "
        "docs/contracts/schemas/langsmith-fleet-v1.schema.json -- which stranske/Workflows really "
        "does track. Bounding by extension does not save you there, because a schema is a .json. "
        "Keep the debris patterns root-anchored and name the nested artifact directory separately."
    )


# DELIBERATE BREAK -> REVERT for the coverage half, performed 2026-08-24, in all three directions
# these patterns can fail. Each broke exactly one case and nothing else, and the revert was
# byte-identical with all 19 green:
#
# 1.  Too narrow -- keeping `/.coverage` and dropping `/.coverage.*` failed
#     test_coverage_data_files_are_ignored[.coverage.a-host.12345.678901]. That is the direction
#     that matters most in practice: the combined database is ONE file per run, the parallel-mode
#     data files are one per instrumented child.
# 2.  Too broad by kind -- widening to `/.coverage*` failed
#     test_coverage_sources_are_not_swallowed[.coveragerc].
# 3.  Ignored but re-TRACKED -- `git add -f .coverage` against the correct patterns failed BOTH
#     test_no_coverage_data_file_is_tracked and test_coverage_data_files_are_ignored[.coverage],
#     the second because check-ignore is index-aware. Two reds, one remedy, and only one of the
#     messages names it; see that test's docstring.
#
# The DEPTH direction the langsmith patterns record has no in-tree near-miss here and is not
# claimed to: nothing tracked in this repo has a basename beginning `.coverage`. The anchoring is
# kept anyway because it is true of the producer -- verify.py globs and unlinks ROOT -- and because
# #72 made every debris pattern in .gitignore root-anchored.

# A THIRD PRODUCER, and the first one that is entirely OURS: coverage.py, driven by this repo's own
# `verify.py --coverage`. `--parallel-mode` writes one `.coverage.<host>.<pid>.<random>` per
# instrumented child and `coverage combine` merges them into `.coverage`. Both names are listed
# because the two are written by different steps, and ignoring only the combined file would leave
# one data file per subprocess -- ~90 of them per run -- unignored.
COVERAGE_DATA_FILES = (
    # The one that actually shipped: TRACKED from #109 until 2026-08-24, 90 KB of opaque SQLite
    # that arrived as a side effect of a typing PR -- every other file in it is about mypy.
    # Third family, third time nothing in this suite opposed it.
    ".coverage",
    ".coverage.a-host.12345.678901",
    # Added 2026-08-30 with the JSON emission in verify.py's combined run. It is a GENERATED
    # REPORT rather than a data file, but it lands in the same place for the same reason and
    # would be committed by the same accident -- `coverage.xml` is ignored by the template block
    # below, and nothing covered this name.
    "coverage.json",
)

# Coverage NAMES that are source and must stay committable, each falsifying a DIFFERENT over-broad
# pattern; both verified against a scratch repository on 2026-08-24.
COVERAGE_MUST_STAY_COMMITTABLE = (
    # `/.coverage*` -- the natural thing to write, and one character from correct -- swallows
    # coverage.py's own config file. That is why .gitignore carries `/.coverage.*` with the dot.
    ".coveragerc",
    # A bare `*coverage*`, the annoyed-at-the-churn pattern, reaches the TRACKED tooling that reads
    # these artifacts. `tools/coverage_trend.py`, `scripts/ci_coverage_delta.py` and
    # `.github/workflows/maint-coverage-guard.yml` are its siblings.
    "tools/coverage_guard.py",
    # `/coverage*` -- the pattern that would have been written to catch coverage.json in one go --
    # reaches coverage.py's own config under its OTHER spelling. `.coveragerc` above falsifies the
    # dot-prefixed pattern; this falsifies the bare one, and they are different mistakes.
    "coverage.cfg",
)
# `coverage.xml` is deliberately absent from the list above: it is a generated REPORT, and the
# template-managed block at the bottom of .gitignore already ignores it. Asserting it stays
# committable would pin the opposite of the truth.


@pytest.mark.parametrize("artifact", COVERAGE_DATA_FILES)
def test_coverage_data_files_are_ignored(artifact: str):
    require_git()
    assert is_ignored(artifact), (
        f"{artifact} is not ignored. It is build output: verify.py's coverage_reset() UNLINKS "
        "ROOT/.coverage and ROOT/.coverage.* before every instrumented run and "
        "coverage_combine_and_report() writes them again, so a tracked copy makes "
        "`verify.py --coverage` a deletion of a binary followed by a re-add of different bytes — "
        "and an uncommitted one blocks a branch switch. Keep .gitignore's `/.coverage` and "
        "`/.coverage.*` pair: the combined database and the per-child parallel-mode data files are "
        "written by different steps, so one pattern does not cover the other."
    )


def test_no_coverage_data_file_is_tracked():
    """Ignoring is not untracking — and here the ignore test alone cannot tell you which you have.

    `git check-ignore` consults the INDEX by default, so it reports a TRACKED path as not-ignored
    even when a pattern matches it (confirmed both ways in a scratch repository on 2026-08-24:
    `--no-index` says IGNORED for the same file). The sibling test above therefore goes red on a
    re-track, but it names the pattern as the thing to fix, which is the wrong remedy — the pattern
    would already be right. This test names the real one: `git rm --cached`.

    Nothing is lost by untracking. The file is regenerated by any `verify.py --coverage` run, and
    the number CI reports comes from that run's own artifact, never from a committed copy.
    """
    require_git()
    proc = git("ls-files", "--", ".coverage", ".coverage.*")
    assert proc.returncode == 0, f"git ls-files failed: {proc.stderr.strip()}"
    tracked = [line for line in proc.stdout.splitlines() if line.strip()]
    assert not tracked, (
        f"tracked coverage data file(s): {tracked}. .gitignore does nothing for a path already in "
        "the index — `git add -A` skips an ignored UNTRACKED path but stages an ignored TRACKED "
        "one. Untrack with `git rm --cached .coverage`; every `verify.py --coverage` run rebuilds "
        "it. A committed coverage database is also a binary blob rewritten on every measurement, "
        "which is what put a 90 KB diff into #109, a typing PR that never mentioned coverage."
    )


@pytest.mark.parametrize("path", COVERAGE_MUST_STAY_COMMITTABLE)
def test_coverage_sources_are_not_swallowed(path: str):
    require_git()
    assert not is_ignored(path), (
        f"{path} is ignored, so the coverage pattern has grown past the coverage DATA. Over-broad "
        "is the other failure direction and it is one character away: `/.coverage*` swallows "
        "`.coveragerc`, coverage.py's own config, and a bare `*coverage*` reaches the tracked "
        "tooling that consumes these artifacts. Keep the patterns root-anchored and keep the dot — "
        "verify.py globs and unlinks the checkout ROOT, so that is the only place the debris lands."
    )
