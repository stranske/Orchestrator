# Issue 1111: saved-report references for per-case evidence gaps

Task 2 advances but remains incomplete. The previous commit `ee0ffdd` changed
17 files, strengthening the inventory reader and retaining recovery evidence.
Its own receipts establish neither the original 71 identities nor a permitted
Brain inventory. All 26 task and acceptance checkboxes correctly remain
unchecked. See the prior [input recovery](../issue-1111-input-recovery/README.md).

The existing gap exporter now accepts an optional `--report SAVED_REPORT` with
a schema 2 manifest. It matches the complete run, target and verdict pair;
ambiguous rows never supply claims. Each unique matching row retains the report
file SHA256, row index and serialized-row SHA256, reported full source revision,
role/backend identifiers, decision and reported cost. Missing records, missing
packet targets, conflicting packet targets, unavailable revisions, absent role
identifiers and saved errors produce specific gaps. Owner-excluded cases retain
identity-only gaps and contribute no saved execution claims.

These are explicitly unverified report claims. Report summaries do not supply
per-case evidence. The exporter does not authenticate membership, verify the
claimed revision, open persisted role/cost records, dispatch or write outcomes.
The verified revision, result, role provenance and cost fields remain UNKNOWN,
even when a report claims a zero-dollar cost. Operational acceptance remains
false. Without `--report`, existing output behavior is preserved.

Example invocation, after original inputs are recovered:

```text
node scripts/export_adjudicator_case_gaps.js --db BRAIN --observed-at UNIX_SECONDS --manifest ORIGINAL_MANIFEST --report SAVED_REPORT
```

Output is JSON on stdout; the caller chooses a new evidence file. The saved
report, manifest and Brain remain unchanged. An unavailable Brain still exits 2
with explicit inventory gaps; invalid reports or run-only manifests exit 1
without partial output. No real cohort report was available for this round.

Verification on parent `ee0ffdd` plus the source/test change:

| Check | Result |
| --- | --- |
| Original exporter suite before edits | 6 passed |
| Final expanded tests against parent exporter | Original 6 passed; new 4 failed |
| Final exporter suite | 10 passed |
| Related inventory, batch, closer, receipts, completion and gap suites | All 6 files passed |
| Focused existing reader/Gate CLI checks | 8 passed |
| Both JavaScript syntax checks and `git diff --check` | Passed |

These are synthetic regression checks, not the three named pytest acceptance
runs or the two specified safety mutations. No Python files change. The selected
runner interpreter reports `No module named pytest`; no named pytest acceptance
or mutation/restoration receipt is claimed. The full CLI suite was not rerun,
and the 16 historical failures have no new dispositions here.

The checkout Git metadata is read-only; `git add` cannot create `.git/index.lock`.
The source/test commit was created with separate metadata under
`/tmp/issue-1111-case-records-delivery.git`. Its full commit ID and source hashes
are retained in [validation.json](validation.json); the transferable
[commit patch](source-commit.patch.gz) contains both changed JavaScript files.
The checkout branch was not advanced. Evidence files are retained in the
workspace for the automation handoff.

The PR body update, blocker comment and `needs-human` label were attempted.
Automatic approval review rejected all three because connector writes require
approval while this session's policy is `never`. No remote mutation completed.
[publication.json](publication.json) retains the errors and fresh readback of
PR #1112 as open with `isDraft=false`. The proposed body and comment are retained
alongside it, including correction of the premature `Closes #1111` preamble.

Successor ownership remains [issue #1111](https://github.com/stranske/Orchestrator/issues/1111)
and [PR #1112](https://github.com/stranske/Orchestrator/pull/1112). Recover the
authenticated original manifest and an owner-permitted persisted store first;
then reconcile saved claims with original role runs, revision evidence and cost
records. Do not substitute the later 37/95/122 populations. Real closer
observations, named safety experiments, summary reconciliation, the 16 failure
dispositions and authorized metadata publication remain unfinished.
