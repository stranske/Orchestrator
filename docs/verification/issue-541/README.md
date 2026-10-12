# Dispatcher alias and logout recovery

Source #541 follows merged #520. An owner startup alias `gh="command gh"` caused zsh to expand the function name while parsing the proxy and bypass its refusal. After sourcing each owner startup file, remove only the gh alias before installing the guard function. The wrapper PATH layer and exact refused-command set remain unchanged.

Forward `.zlogout` through the redirected ZDOTDIR as well. Actual native zsh tests establish the shutdown contract: interactive login executes the owner logout once and preserves explicit exit23; tested `-c`, `-lc` and `-ic` modes do not execute it. Owner startup effects and allowed gh reads remain intact.

45 focused guard tests PASS. Nine new native startup/logout cases fail with assertions on unchanged merged production, then pass after byte-identical candidate restoration; zero errors/skips. Four startup phases include noninteractive and interactive/login modes and both refused auth logout and allowed auth status. Fake gh logs prove refused calls never reach the command. No real authentication, owner dotfiles, installed guard, credential scope, workflow permission or deployment is modified.

`proof.tar.gz` retains exact commands, raw console/JUnit, original/candidate source hashes and restoration receipt. `manifest.json` binds every decoded member and current source/tests. The tests use the established fake-environment fixture. Full private-state verifier and final-head hosted CI/review remain separate gates. Source acceptance requires actual post-merge comparison; this proof does not claim installed deployment.

Full private-state `python3 src/verify.py --reconcile-floor` exited0:2903pytestPASS/0fail/0skip,104module selftests,5capability gates,122mypy modules. It measured and raised the collection/pass floor from2894 to2903 with every skip/mypy ceiling unchanged. Full raw summary/command/current floor are bound in the archive; the focused proof additionally retains raw JUnit. Final hosted checks, seven-minute review floor and actual comparison remain pending.

Supplemental Bash startup-alias regressions live in `tests/test_agent_gh_guard_bash.js`.
Replay with `node --test --test-isolation=none tests/test_agent_gh_guard_bash.js`.
They exercise inherited `BASH_ENV` with alias expansion enabled, both `-c` and `-lc`,
refused writes and allowed reads, an owner startup marker and an unrelated owner alias.
All four fail against unchanged production commit `4d4136d` and pass against the
candidate, with zero skips. Raw TAP/JUnit and exact commands/source hashes are in
`bash-alias-proof.tar.gz` and `bash-alias-proof.json`. Production runs use
an isolated source copy; candidate bytes never change. Every process uses temporary
owner files and fake gh. The original archive, every member hash, and its equality
to current dispatcher/pytest source were independently rechecked this round:
nine assertion REDs, nine GREENs and 45 focused GREENs, no errors or skips.

This runner has no pytest, Black or zsh; package installation fails on DNS access.
The supplemental Bash checks do not establish a fresh native zsh run. No Python
source changes were made this round, and verification floors/ceilings are unchanged.

Hosted CI on PR head `a75cec3` is blocked, as retained excerpts in
`hosted-ci-blocker.json` show. Its synthetic merge `6c5caa7` skipped all 14 native
zsh cases because zsh was absent. The nine added cases exceed existing ceilings:
checkout 40 skips versus 31 allowed, bare mirror 61 versus 52. An authorized human
must install zsh before verification in the repo-specific `.github/workflows/ci.yml`
jobs (including combined coverage and the flat mirror); shared Gate job setup
belongs in `stranske/Workflows` if it also needs that prerequisite. Workflow edits
are protected in this agent-standard run. Do not raise skip ceilings or remove
tests to admit the repair. Complete hosted checks, all review-thread pages,
seven-minute review floor, guarded terminal merge and actual `verify:compare`
remain pending.

Workspace git metadata is read-only; this round's scoped commit is made in a
writable temporary checkout. The GitHub connector also refused the required
`needs-human` label because mutations require approval and this run's approval
policy is `never`; the protected workflow blocker is recorded here for handoff.
