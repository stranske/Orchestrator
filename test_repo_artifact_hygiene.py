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
from pathlib import Path

import pytest

import env_prereq

HERE = Path(__file__).resolve().parent

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
