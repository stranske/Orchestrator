# Cached reviewer exclusion and bounded subprocess recovery

At original head 424c643285cc7b6745d0fcce4f82f757c5fe20ae the eight new regression cases fail. The corrected production path passes all eight. Automatic PASS and BLOCKED self-reviews cannot be reused after author exclusion; missing, malformed or overlapping reviewer identity fails closed. The merge guard applies the same rule and its suggested manual command includes the author exclusion. Reviewer identities are included in the hashed artifact. Same-second judgments use insertion order rather than random event IDs.

Git fetch, follow-up GitHub calls, and the merge-commit lookup have 60-second bounds. Timeout tests prove spawn failure and release of the follow-up lock for another attempt. No provider call or live Brain write was needed.

The first local verifier exposed nondeterministic same-second reuse and returned FAIL_BROKEN; that record is retained. After its production fix and deterministic tie-order assertion, local_verify reports PASS with candidate GREEN/base RED. Final focused cache/panel suite: 66 PASS; broader affected suites: 123 PASS before the tie-order correction, whose affected 66 cases were rerun. Both module selftests, Black, Ruff and mypy pass. Exact collection is 3152 (prior 3144 + eight executed regressions). All skip/mypy ceilings remain unchanged. This is bounded regression proof; fresh hosted complete verification remains required.

The archive retains original logs, JUnit and verifier results. The manifest binds final production/tests and archive bytes. Exact-head topology, review threads, and the seven-minute post-push floor remain merge prerequisites.

## Final post-push scheduling finding

A new valid review finding stopped the merge of 1ad3747. A cached author-excluded pre-merge PASS had suppressed post-merge scheduling. Two new production-path assertions fail at that head and pass after repair: an enabled panel schedules review of the actual merge commit even when a PR-head verdict exists, carrying the author exclusion; an explicitly disabled panel never schedules a cached BLOCKED result. The detached consumer retains its evaluated-head check and may reuse only a verdict for its actual checkout. Original cached verdicts remain advisory evidence. The affected suites pass68 cases, guard selftest/Black/Ruff/mypy pass, exact collection3154 (+2). All ceilings unchanged; fresh hosted verification and a new420-second floor are required. Raw second-round proof is retained separately in merge-scheduling-proof.tar.gz.
