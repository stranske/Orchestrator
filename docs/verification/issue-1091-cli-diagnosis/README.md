# Issue 1091: retained CLI failures

The baseline on `4cd67511ae44bc7038d7e01de5feda5a63f06419` reproduces all
16 failures (51 passes). The bounded test repair resolves six (57 passes,
10 failures). The operational tasks and CLI-resolution task remain unchecked.
`validation.json` binds the tested files and lists every retained failure by
its exact test name. `baseline.log` and `after.log` retain the assertions.
Transcript trailing whitespace is normalized; raw and retained hashes are recorded.

The timeout repair follows merged #609 (`c8c4afe`): transport **and** saved
`dispatch_timeout_s` must be 600. The new summary keys follow #610 (`3f39fd4`)
and #636 (`4ef2d3e`); exact assertions now include retry/advice counts rather
than ignoring extra keys. No cost expectation was relaxed.

The switch-review witness now isolates the unrelated, intentionally persistent
`feature_goals.report` through the existing `feature_goals_fn` seam and the
unrelated `runtime_ac_gate.post_merge_summary` reader, which opens `feedback._conn`.
The actual adjudicator reader and formatter still run for all ten fixtures.
Filesystem snapshots and the original forbidden-call checks remain active;
legacy retry/advice counts must render UNKNOWN and current counts must render
their measured values. This repairs test isolation, not production read-only
behavior of the entire weekly report.

| Retained failure (test-name suffix) | Verified disposition |
| --- | --- |
| shadow roles, newest PR #1 | PASS: obsolete 180-second timeout |
| same PR, newest run 1 | PASS: obsolete 180-second timeout |
| shadow roles, newest PR #2 | PASS: obsolete 180-second timeout |
| same PR, newest run 2 | PASS: obsolete 180-second timeout |
| bounded batches, abstentions and invalid responses | OPEN: zero receipt dropped; measured count 3 versus expected 4; additive summary keys corrected |
| gate pages: numeric-run-order | OPEN: finding-comment selection rejects multi-comment history before pagination |
| gate pages: numeric-attempt-order | OPEN: same finding-selection defect, not evidence of lexicographic ordering |
| raw proposals and measured costs | OPEN: zero receipt dropped; measured count 3 versus expected 4; summary/line schema corrected |
| switch review without replay or writes | PASS: isolated unrelated producers and corrected line schema |
| reuses saved cases with late durability/costs | OPEN: zero receipt dropped; measured count 4 versus expected 5; additive summary keys corrected |
| late abstention after outcome deletion | OPEN: zero receipt dropped; initial measured count 2 versus expected 3 |
| late invalid-response costs | OPEN: native zero becomes null instead of 0 |
| withdraws stale costs | OPEN: measured count 1 versus expected 2 after withdrawal; remaining zero receipt dropped |
| mixed merge dispositions | OPEN: zero receipt dropped; measured count 3 versus expected 4 |
| empty report | PASS: exact retry/advice schema restored; all rates/costs remain unknown |
| stale comparisons until durability rejudged | OPEN: zero receipt dropped; measured count 3 versus expected 4 |

`cost-readers.json` is a private synthetic SQLite probe of unchanged production
code. `adjudicator_retro.measured_cost` reads a complete-source zero as 0;
`feedback.role_cost_report` requires `> 0`, so `_persist_report_locked` replaces
it with null and excludes it from the measured denominator. A positive receipt
agrees across all three reads. The inconsistency is demonstrated; this round
does not weaken zero-cost assertions or choose a new billing policy.

`numeric-diagnosis.log` records a temporary diagnostic copy of the test, **not**
a passing replacement for the two failing regressions. Both stop after one
read with `merge-bound verifier finding comment unavailable`. `fetch_evidence`
compares `decision_from_pr` dictionaries for the whole PR and for one comment;
their `_lifecycle_history` arrays differ. The legacy selector itself compares
numeric run/attempt tuples. A repair must bind the selected terminal finding
without dropping lifecycle conflict checks or author/subject provenance.

The 57 passing cases include saved-history retention, aged-out role-record
recovery, stale comparison rebuilds, missing-outcome withdrawal, regrading and
Gate-gap continuation. They do not prove that the ten failing paths work.
Related checks pass: metadata (7), inventory (26), historical receipts (9),
evidence unittests (11) and retrospective selftest (23 checks).

The original #428 issue and its authenticated comment history were reviewed.
They provide aggregate cohort sizes and a #445 run reference, not the complete
original identity manifest. `brain-inventory.json` records an unavailable
default database, not an observed empty inventory. No historical replay, native
role dispatch, outcome write or live closer observation occurred.

Black and pytest are unavailable; an attempted temporary package installation
failed. Consequently no Python source was changed or committed, and no fresh
named pytest/mutation acceptance or full target-source CI is claimed. The next
code repair needs the required Python formatting/test tools, then the two
production defects above can be fixed against the retained regressions.

Metadata reconciliation was attempted before implementation. Connector approval
review rejected the #1074/#1075/#1092 body changes, blocker comment and
`needs-human` label because writes require approval while policy is `never`.
Remote checkboxes remain unchanged. The seven metadata regressions still pass;
their existence is not successful publication. Checkout Git metadata is also
read-only; a source commit and transfer patch are prepared with separate Git
metadata under `/tmp/1091-cli-delivery`, leaving workspace edits for collection.
PR #1092 was initially open and ready, then independently merged at
`2026-11-03T17:21:37Z` with unchanged head `4cd6751`. This run did not merge or
successfully change a PR. These additional local changes need a subsequent
ready-for-review delivery; the merged PR is not reported as open.
