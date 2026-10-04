# Same-repository Gate status refusal — Workflows #3399

Source: https://github.com/stranske/Workflows/issues/3399. This bounded consumer-owned Gate repair does not close the 17-repository campaign.

Baseline `d326e59f5afeffd078cae488daa6b15a091640ac` lets the real retry helper return null for a refused commit-status write on a same-repository PR. The workflow then publishes nothing and succeeds, retaining whatever older status was already present. Forks have an expected read-only token; same-repository refusals are unexpected and must fail visibly.

The production workflow scripts run under Node with the actual retry helper; only the GitHub client and core are test doubles. New expectations over the unchanged baseline workflow fail for same-repository success/failure verdicts and the helper's 404 refusal route: **3 failed, 21 deselected**. After the null-response guard, the entire existing fork/deleted-fork, same-repository, rate-limit and origin/comment suite passes: **24 passed**. No tokens, permissions, event triggers or shared helper semantics changed. The fork summary fallback and non-success verdict floor remain intact.

Commands:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q --override-ini addopts= -k same_repo_refusal
/opt/anaconda3/bin/python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q --override-ini addopts=
/opt/anaconda3/bin/python3 -m black --check --line-length 100 --exclude '(\.venv|\.workflows-lib|node_modules)' .
/opt/anaconda3/bin/python3 -m ruff check tests/test_gate_commit_status_fork_tolerance.py
git diff --check
```

Full Black checked 297 files. This is repo-specific recovery of Orchestrator's consumer-owned create-only Gate, as requested by the source campaign. Current exact-head CI, full review-thread enumeration, seven-minute floor, repository merge_guard and post-merge comparison remain required. No installed mirror publication or source issue closure is claimed.

Keepalive verification on 2026-10-04 strengthens the refusal controls to assert the exact head and verdict in the diagnostic, a single publication attempt, and no fork fallback. A successful same-repository publication is now exercised alongside a successful fork publication. The suite still collects 24 tests, so no collection floor changes are needed. This round leaves the production workflow untouched.

The baseline was read with `git show d326e59f5afeffd078cae488daa6b15a091640ac:.github/workflows/pr-00-gate.yml` through a temporary module-scoped fixture overriding only `_workflow_lines`; the actual retry helper and all assertions were retained. That control produced **3 failed, 21 deselected**. The current production workflow produced **24 passed**; adding the CI configuration and verdict-replay tests produced **48 passed**. Every pytest command used `-m "not slow"`.

The broad local run produced **1371 passed, 26 failed, 24 skipped**: 22 failures attempted writes under the runner's read-only `~/.codex`, two needed the unavailable coverage package, and two synthesis-promotion checks reported verifier errors. This is not a full local-suite PASS claim. The existing remote head `ccd4a71` has passing Gate/Python CI; fresh exact-head CI remains required after this follow-up. Pinned pytest 9.1.1 and Black 26.5.1 sources were obtained from their upstream GitHub repositories into `/tmp` because the runner had neither tool and could not reach PyPI.

The required repository-wide Black check, with line length 100 and exclusion `(\.workflows-lib|node_modules)`, passes for all **297 files**. `git diff --check` also passes.

Verified task status for this round:

- [x] Fail visibly on same-repository null commit-status publication while retaining expected fork handling.
- [x] Three unchanged-production-workflow refusal controls fail; the repaired 24-test suite passes.
- [x] Fork/deleted-fork and rate-limit behavior remain covered.
- [ ] Apply this follow-up to the PR branch and pass fresh exact-head CI; retain the seven-minute floor and repository merge_guard before merging.

The workspace `.git` is read-only, so the follow-up source/test commit was created in `/tmp/gate-status-refusal-review`; its patch is `/tmp/gate-status-refusal-followup.patch`. The validated two-file change also remains in the runner working tree for a writable runner to commit. GitHub checkbox, `needs-human` label, and blocker-comment updates were attempted but refused with `MCP tool call requires approval, but approval policy is never`; the remote task checkbox therefore remains unchanged. PR #413 was verified open and ready for review.
