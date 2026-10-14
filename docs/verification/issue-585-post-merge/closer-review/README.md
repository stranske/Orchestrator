# Current review recovery

The configured timeout and malformed summary regressions fail against b1918e29's production loader (4 failures) and pass after repair (4 passes). The integrated runtime and exact-merge suite passes 81 tests. Collection is 3349 with all inherited ceilings preserved. This is focused evidence, not a new full-suite verdict.

The hosted test-quality job mistakenly parsed ORCH_RUNTIME_AC_REOPEN=1 as a pytest node. The PR body now carries the explicit exact named-test marker. Running the unchanged checker with this body returns PASS. Its baseline fails on an unavailable sweep API, so that result alone does not prove attribution to the forced-PASS mutation. The original forced-PASS RED/restoredGREEN transcripts remain in the parent evidence directory.

The review repair is pushed after the explicit marker is saved, so the synchronize event captures that current metadata. Fresh complete CI, expected topology, review threads and seven-minute review floor remain required. Source585's post-sync native M5b and October27 observations remain open; no production flag or deployment is changed.
