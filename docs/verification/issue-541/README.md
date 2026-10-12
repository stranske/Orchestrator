# Dispatcher alias and logout recovery

Source #541 follows merged #520. An owner startup alias `gh="command gh"` caused zsh to expand the function name while parsing the proxy and bypass its refusal. After sourcing each owner startup file, remove only the gh alias before installing the guard function. The wrapper PATH layer and exact refused-command set remain unchanged.

Forward `.zlogout` through the redirected ZDOTDIR as well. Actual native zsh tests establish the shutdown contract: interactive login executes the owner logout once and preserves explicit exit23; tested `-c`, `-lc` and `-ic` modes do not execute it. Owner startup effects and allowed gh reads remain intact.

45 focused guard tests PASS. Nine new native startup/logout cases fail with assertions on unchanged merged production, then pass after byte-identical candidate restoration; zero errors/skips. Four startup phases include noninteractive and interactive/login modes and both refused auth logout and allowed auth status. Fake gh logs prove refused calls never reach the command. No real authentication, owner dotfiles, installed guard, credential scope, workflow permission or deployment is modified.

`proof.tar.gz` retains exact commands, raw console/JUnit, original/candidate source hashes and restoration receipt. `manifest.json` binds every decoded member and current source/tests. The tests use the established fake-environment fixture. Full private-state verifier and final-head hosted CI/review remain separate gates. Source acceptance requires actual post-merge comparison; this proof does not claim installed deployment.

Full private-state `python3 src/verify.py --reconcile-floor` exited0:2903pytestPASS/0fail/0skip,104module selftests,5capability gates,122mypy modules. It measured and raised the collection/pass floor from2894 to2903 with every skip/mypy ceiling unchanged. Full raw summary/command/current floor are bound in the archive; the focused proof additionally retains raw JUnit. Final hosted checks, seven-minute review floor and actual comparison remain pending.
