# Issue558 review recovery

The original follow-up concurrency/search/create proof remains in ../issue-558-followup.
This additional recovery qualifies restoration, explicit author exclusion, live-state confinement,
closure retry and actual squash checkout, without a paid reviewer or live Brain write.

- 78 focused tests PASS after integration onto main 00d9f964; both module selftests PASS.
- Final production replay: restore the actual entire original 89a500ad adversarial.py, execute the
  same 12 behavioral cases with raw console/JUnit, restore byte-identical corrected production,
  then run the same cases GREEN. The archive includes the replay command, source/test hashes,
  raw logs/JUnit and local_verify verdict. Two new checkout-argument API tests are covered by
  the 14-case green suite, rather than misrepresented as behavioral mutation failures.
- The native sandbox-exec shell probe attempted writes to disposable sibling source, Brain and
  sentinel files. All three were refused and original bytes survived; the review cwd remained
  writable. This proves this native shell boundary, not every provider CLI or deployment.
  When enforcement is unavailable, the production review path dispatches zero reviewers and
  reports INCONCLUSIVE. Custom live state/Brain locations are protected too.
- Dirty/untracked panel worktrees are refused without deleting unknown data. Restoration checks
  both Git return codes and verifies clean state before retry; failed restoration records
  INCONCLUSIVE and lineage without dispatching another reviewer.
- Explicit authors are filtered and reduced coverage remains INCONCLUSIVE. Source closure has a
  separately persisted completion flag, requires a merged PR, and retries without another create.
- Detached review fetches the actual mergeCommit.oid but retains the original PR head for memo
  and follow-up identity. Previously cached premerge verdicts cannot qualify another checkout.
- Exact integrated collection 3134 matches the historical floor at this stage; the later author-exclusion
  record raises the floor to 3144. All current-main skip/selftest/mypy
  ceilings are preserved. Black, focused Ruff and mypy passed. Fresh hosted full checkout/mirror
  verification, review threads and seven-minute floor remain required after the final push.

proof.tar.gz is lossless; manifest.json binds every member and the archive. Historical evidence
retains its original source hashes; final receipts name the bytes each actually executed.
