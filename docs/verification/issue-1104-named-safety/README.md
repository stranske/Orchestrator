# Issue 1104: executable named safety contract and current runner gap

- [ ] Recover the exact three acceptance-test names and specified mutations from the originating issue and receipts. Run them on one recorded revision using the selected repository interpreter; capture each required named failure and byte-identical restoration with passing reruns.

Only the retained-generator task is verified complete. Commits `3976a84`,
`b98989f` and `0eb7d44` were reviewed before this change. The latter two explicitly
leave original cohort recovery and per-case operational evidence incomplete:
the authenticated original manifest and Brain are unavailable. Those blockers
are retained; no later count is used to infer original membership.

A fresh authenticated read of [originating issue #428](https://github.com/stranske/Orchestrator/issues/428)
confirms the three names and two deliberate faults already preserved in
`../issue-1104-cohort-recovery/source-428.json` and
`../issue-428-named-acceptance-20261007/validation.json`. The new
`scripts/run_adjudicator_acceptance.js` executes that exact contract:

1. Require a selected absolute interpreter path and clean acceptance inputs;
   record the Git revision, input hashes, interpreter/version and runner hash.
2. Check pytest availability and verify both mutation modules import from
   private temporary copies. Preserve virtualenv interpreter symlink spelling.
3. Run each named node separately, excluding slow tests. Verify native JUnit
   names, case counts, exits, skips, errors and outcomes.
4. Apply the recorded outcome-write and unconditional-offer faults in those
   copies only. Require the exact named assertion failures. Restore identical
   bytes and rerun the affected named test, including after an invalid witness.
5. Rerun all three nodes and verify repository input hashes remain unchanged.

Logs, native JUnit hashes, argv, mutation hashes, restoration hashes and any
gap are persisted in a new receipt directory. Existing receipt directories
are rejected. Timeouts, missing XML, collection errors, unrelated assertions,
unexpected cases and skipped tests cannot establish the named safety proof.
All state paths used by test subprocesses are private. The CLI has no provider
credentials and the named tests use their existing mocked transports. It never
edits production Python files, dispatches the historical cohort or changes the
operational Brain. Completing the named safety contract alone would still not
establish the other operational acceptance criteria.

The actual command for this checkout was:

```sh
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 \
  --output docs/verification/issue-1104-named-safety
```

It returned **exit 2, blocked**. [receipt.json](receipt.json) records revision
`0eb7d44cc6c75531ae58127fd7743d661131b270` and the specific
`selected_interpreter_pytest_unavailable` gap; the selected interpreter's
[native stderr](pytest-prerequisite.stderr.log) confirms the missing module.
Installing pytest and Black into `/tmp` also failed with no available
distributions. No named acceptance passes, deliberate mutation failures or
restored acceptance passes were produced by this real attempt. Repository
inputs remained byte-identical. No task or acceptance checkbox is newly complete.

Eleven runner tests pass. Their successful phase transcripts are **synthetic
orchestration fixtures**, with real interpreter/import checks and standard-library
XML parsing; they are not operational acceptance receipts. They cover the
retained contract, exact phase order, private mutation bytes, log hashes,
restoration after invalid witnesses, skip/error rejection, unavailable pytest,
interpreter selection, dirty-source refusal and CLI input validation.
[related.log](related.log) records passing checks for all seven related test
files. The eight focused inventory/Gate CLI cases also pass. JavaScript syntax
and diff whitespace checks pass. No Python files change, so the Python
pre-commit formatting gate does not apply.

PR-body reconciliation was attempted before implementation: only the duplicate
generator task would be checked. The connector rejected it because writes
require approval and the run's policy is `never`. The blocker comment and
`needs-human` label were likewise rejected. [publication-gap.json](publication-gap.json)
preserves that correction and the authenticated lifecycle readback: PR #1105
was externally merged at `2026-11-03T22:20:06Z`, and issue #1104 closed at
`2026-11-03T22:20:08Z`, while this iteration was still running. This agent did
not merge or close either item. These local changes are not in that merge;
the remote operational tasks remain unchecked. No publication or successor
handoff is claimed.

The checkout's `.git` is read-only; staging fails to create `.git/index.lock`.
Delivery uses separate writable Git metadata at
`/tmp/issue-1104-named-safety-delivery.git`, with a source/test commit and
`/tmp/issue-1104-named-safety.patch` for transfer. The checkout branch is not
advanced and nothing is pushed. Resume the exact command above with a new output
directory once the selected interpreter has pytest; original-cohort, real-closer,
summary/cost, individual CLI-disposition and publication work remains pending.
