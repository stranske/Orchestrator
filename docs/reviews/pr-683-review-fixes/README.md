PR #683 review-fix evidence

- `g1-live-target-red.log`: temporarily restore parsing `runs.target` as only `owner/repo#N`. The live-format metadata regression test fails; the mutation was reverted.
- `suite-isolation-red.log`: temporarily disable the conftest autouse isolation. An uninjected `switch_review.review()` writes a history file and its no-write test fails. The demo overrides persistence to use empty goals and an explicit temporary path, so even the broken version cannot measure live sources or write live history. The mutation was reverted.
- `targeted-green.log`: required feature-goals and switch-review pytest selection, after reverting both breaks.
- `cli-isolation-green.log`: eleven switch-source/adversarial CLI tests; unrelated live credential scans and GitHub collection are isolated by their own fixtures. Production collectors and their direct tests are unchanged.
- `switch-states-green.log`: ten switch-source CLI tests; their fixture now returns an explicit unmeasured credential-scan stub rather than scanning live runtime files. Credential-redaction tests retain their own scan coverage.
- `switch-review-selftest.log`: isolated switch-review CLI selftest; its existing process-comparison skip is explicit.
- `live-g1.log`: source DB copied with `shutil.copy2` to a temporary directory before opening SQLite. G1 opens only that copy with `mode=ro`; no `report()` or issue mutation is invoked. The runtime generation's experiment directory is passed through the existing `ORCH_EXP_DIR` override, and issue attributes are read through REST. Measured value is zero, with no attribution or issue-read failures.

The full verifier's disposition is recorded in the PR body and its capture. Local tests and a read-only snapshot do not establish deployed tick filing or scheduled-task acceptance.

- `tracked-inputs-green.log`: five input-guard tests pass after adding the isolation fixture's report-module dependency to their miniature checkout.
- `verify.log`: last complete full verifier attempt, exit 1; 3,954 passed, 42 failed, four failed module selftests. It includes the scratch-fixture failure subsequently fixed above. No later full PASS is claimed.
- `main-baseline-replay.log`: seven representative failure nodes replayed against an unchanged archive of origin/main b5de92e; six fail and one passes. This establishes those six failures independently of the review fixes, not the disposition of every remaining failure.
