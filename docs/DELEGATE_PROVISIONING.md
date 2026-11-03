# Delegate branch naming and stale worktrees

`dispatcher.plan_dispatch` selects the branch for new opener worktrees. Its single
`INFRA_REPOS` classification contains `stranske/Workflows` and
`stranske/Orchestrator`: these default to `orch/issue-N`. Product targets use the
selected agent's registry prefix, for example `codex/issue-N` or `gemini/issue-N`.
Repo-level work uses `PREFIX/LANE`. A descriptive slug may be appended to an issue
branch. The selected policy is appended to the worker prompt so an older task brief
cannot silently restore the registry prefix for infrastructure work.

The explicit delegation interface accepts an override:

```sh
python3 src/dispatcher.py delegate --agent cursor --target stranske/Workflows#123 \
  --branch-prefix orch/ --prompt 'Implement the bounded issue and open a ready PR.'
```

`delegate(..., branch_prefix="team/custom")` and routing assignments containing
`branch_prefix` use the same policy. A trailing slash is optional. Prefix segments
accept letters, digits, underscores and hyphens and cannot begin with a hyphen.
Closer dispatches continue tracking the PR's actual head branch; they ignore this
opener option. Direct `provision` callers that supply no branch retain their existing
`orchestrator/` default.

This changes no cap-health code or ownership inference. Infrastructure PRs avoid
registry-prefix ownership; callers may still explicitly use keepalive labels when
wanted. This command adds no labels itself.

If creating an opener branch fails because that branch already exists, provisioning
logs the recovery, runs `git worktree prune --expire now`, and retries **once** by
checking out the existing branch. It preserves commits rather than resetting to the
current base. Git refuses a branch still checked out by a live or locked worktree;
no forced checkout, branch deletion or `-B` reset is used on this retry. Other Git
failures propagate immediately. An existing target worktree on a different branch
is refused for inspection, even with stale-reuse enabled. Existing cleanliness and
divergence checks still apply to reusable worktrees.

Offline verification:

```sh
pytest tests/test_dispatcher_provision.py
python3 src/dispatcher.py --selftest
python3 src/provision.py --selftest
python3 src/verify.py
```

The tests exercise the dispatcher-to-provisioner branch handoff and worker prompt,
CLI/API overrides, product defaults, closer behavior, real Git recovery with commit
preservation, and refusal to steal a live branch. For the deliberate break, replace
the infrastructure default `"orch"` with `agent` in `_dispatch_branch`: the
`test_infra_prefix` gate must fail, then pass after restoration.

Post-merge acceptance remains a separate live boundary: delegate one authorized
Workflows issue, inspect its ready PR's `headRefName` for `orch/`, and confirm that
cap-health does not list it as opener-owned. The issue also asks for zero
infrastructure PRs in `total_opener_owned` at every cap-health read over seven days
(check 2026-10-24). Offline tests do not establish either live observation.

This extends the existing dispatcher/provision rails; stages, components, role
classification and feedback surfaces are unchanged.
