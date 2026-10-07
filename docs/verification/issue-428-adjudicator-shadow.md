# Issue 428: retrospective adjudicator evidence

This extends the existing AdjudicatorAgent and Brain role-run recorder. Source implementation
and local replay are distinct from runtime publication, which remains manual.

## Keepalive later-failure comparison, 2026-10-07

The new CLI regression replaces a saved durable observation with each fleet failure signal
(`reverted`, `broke_later`, `reopened`, `abandoned`, `reworked`). Both comparison rates must
refresh against the new truth while preserving the cohort, costs, proposal identities,
metadata evidence floor and complete Brain snapshot. Restoring durability recovers the
original comparison without redispatch.

All ten Node CLI tests and five unittest evidence tests passed. A temporary source copy
deliberately reused cached later truth: the new regression failed with PASS instead of FAIL.
Byte-identical restoration passed. No repository Python source was changed.

Recent commits and the passing CLI suite support checking the report, closer binding and
switch-review implementation tasks. The named pytest acceptance tests and their two required
faults remain pending: pytest and Black are unavailable, and installation failed. The connector
rejected the PR-body update, blocker label and comment because approval is required while this
run's policy is `never`. The reconciled body is saved under `/tmp`. Git metadata is read-only;
the focused changes are committed in an isolated Git directory with a patch and bundle for transfer.

## Validation

- Named suite `python3 -m pytest tests/test_adjudicator_retro.py -q`: 8 passed.
- Broader role/advisor regression suite: 56 passed.
- Outcome-write fault: injected an UPDATE of original outcomes in the replay loop; the
  outcome-snapshot regression failed (exit 1). Byte-identical source restoration passed (exit 0).
- Unconditional-offer fault: let an adjudicator entry survive without a contested verdict;
  eligibility regression failed (exit 1). Byte-identical restoration passed (exit 0).
- Black, Ruff, touched-source mypy and `git diff --check`: pass.
- Final stable-source `python3 src/verify.py`: exit 0; the full private-state run
  passed 1,963 tests with one named prerequisite skip, 1,964 collected, 101 module selftests,
  118 mypy modules with zero exemptions. The private ledger lacked value-chain-monitor, so one
  coverage test and the corresponding gate skipped within the existing ceilings. Those skips
  are not proof of the missing row. Measured collection floor advanced from 1,956 to 1,964;
  all skip and exemption ceilings were preserved. Final timing: pytest 4m14s, selftests 35s, gates 24s.

## Real replay

The live 90-day Brain population contains 95 dispute rows; the issue's 71-row count is an older
snapshot. Seven bounded replay packets have real router-selected Gemini role runs, all tagged
`source=retrospective`, for Orchestrator #458, #446, #443, #456, #414, Workflows #3748 and
Manager-Database #1751. Initial packet collection refused truncated check evidence; reading
all exact-head pages recovered those cases without changing an outcome. Saved case IDs prevent
repeat paid calls; `--retry` is needed for diagnosed failed attempts.

Their later durability is pending: graded count 0 and both agreement rates UNKNOWN. The
transport's zero-dollar ledger placeholders are not complete cost telemetry: measured-cost
count 0 and total/per-case cost UNKNOWN. Remaining population replay belongs to the local
Reviewed Repo Merge Verify Closer after source review/CI, using
`python3 src/adjudicator_retro.py --dispatch --limit 5`; it can inspect the local Brain and
saved state report. A GitHub runner without that Brain cannot substitute fixtures.

No label, merge, comment or delivery-outcome action follows an adjudicator decision. The
closer offer requires the caller's actual recorded verifier and merge-disposition verdicts.
Missing or agreeing verdicts withhold it. Weekly switch review consumes the saved state report
and names missing report/evidence, ungraded durability and incomplete cost telemetry.

Machine-local transcripts and exact role-run IDs are retained under
`~/.codex/automations/pd-workloop-resume/evidence/20261005T1102Z`; the operational report is
`$ORCH_STATE_DIR/capability-program/adjudicator-retro.json`. These artifacts are not committed
as portable production evidence. Current-head CI, expected checks, complete review threads,
seven-minute push floor and postmerge `verify:compare` remain the delivery gates.


## Closer review reconciliation, round1320

Nullable mergeCommit now has a distinct merge-evidence unavailable error before any
verifier selection, so the resumable case records a precise retry condition. The MCP
capability_advice schema accepts both recorded verdict facts and forwards them as
context. Annotation preserves its documented membership/order invariant; a named
post-annotation eligibility step withholds non-contested adjudicator offers and
reports them separately in precondition.withheld. The summary describes all annotated
candidates, including the explicitly withheld population. No outcome, model default,
shadow gate or publication authority changes.

Focused retrospective tests passed 12 cases including merge-null refusal, direct
annotation invariance, MCP forwarding and contested/unknown/equal verdict eligibility.
Advisor, MCP and retrospective module selftests passed. Current-head CI and private
mirror/live-state-copy validation are still required after integration and push.

## Keepalive comparison validation, 2026-10-06

- [x] Compare saved proposals with later durability and the merge-rule baseline, and
  persist agreement rates, case counts and measured per-case costs in the state report.

`node tests/test_adjudicator_retro_cli.js` passed all four CLI tests. The added regression
withdraws trusted observation dates from saved durable/reverted cases, confirms those
cases leave both comparison denominators, then restores the dates at the detection
boundary and verifies the original comparison returns. Role identities, measured costs
and the entire Brain snapshot remain unchanged by each refresh. Metadata proposals
remain separate from accepted verdicts.

Deliberately replacing the durability cutoff with zero made the added test fail (exit 1).
The Python source was restored byte for byte; the four CLI tests then passed (exit 0).
The existing five evidence unittest tests and the retrospective selftest also passed.

The named pytest acceptance tests and their required outcome-write/unconditional-offer
fault checks remain pending for this round: pytest and Black are absent, and PyPI could
not be reached from this runner. This commit changes JavaScript tests and this verification
note only; it does not change Python source or claim the named pytest checks passed.

## Keepalive eligibility reconciliation, 2026-10-06

Reviewed the six latest commits (`2cec3a8` through `51cf5e2`) before making changes.
The comparison report, closer-lane binding and weekly line already exist. Current-run
verification supports these task checkboxes:

- [x] Compare saved raw proposals with later durability and the merge-rule baseline;
  persist agreement rates, measured costs and counts in the state report.
- [x] Bind `role-adjudicator` on `closer-lane` with `requires_pr: contested_verdict`.
- [x] Render `adjudicator shadow: cases N, agree A, disagree D, cost C` in switch review.
- [ ] Run the three named pytest acceptance tests on the current tree.
- [ ] Run the required deliberate outcome-write and unconditional-offer faults against
  the named pytest tests, then restore and verify they pass.

The new JavaScript regression exercises both advisor classification paths across ten
contexts: missing either verdict, unknown or blank evidence, agreeing verdicts and disputes
in either direction. Each ineligible result must explain the missing fact or agreement.
All five CLI tests pass, including saved durability/cost refresh and weekly rendering.
The existing five evidence unittest tests and three retrospective selftest checks pass.

Fault controls ran in temporary source copies with private Brain databases: an
`outcomes.merged` UPDATE made the snapshot witnesses fail (exit 1), and treating
agreeing verdicts as contested made the new eligibility witness fail (exit 1).
Each copied source was restored byte for byte and all five CLI tests passed (exit 0).
The repository's Python source remained unchanged. These controls verify the
JavaScript witnesses; they do not replace the pending named pytest fault checks.

GitHub access verified PR #506 is open and ready for review. The attempted PR-body
checkbox reconciliation was refused by the connector because it requires approval;
this runner's approval policy is `never`. The PR body therefore remains unchanged.
Pytest and Black are unavailable in both the active and system Python environments;
attempts to install them from PyPI failed. No Python files change in this round, and
the named pytest acceptance checks remain pending rather than being inferred from the
JavaScript witnesses.

The checkout's `.git` directory is read-only: `git add` cannot create `index.lock`.
The changes are committed using an isolated Git directory under `/tmp`, with a
single-commit bundle and patch for transfer; the shared checkout retains the edits.

## Keepalive shadow-dispatch regression, 2026-10-06

Added two JavaScript regressions for the first task's packet and dispatch boundaries.
They use private Brain databases and the real packet builder and AdjudicatorAgent,
with mocked routing and transport. Two persisted disputes follow different router
choices, retain verifier/diff/gate evidence in the prompts, dispatch in isolation,
and record proposals as `source=retrospective` role runs. A one-case limit bounds
each dispatch, and resuming saved decisions neither recollects nor dispatches them.
Eight incomplete-packet variants fail before routing, transport or role recording;
a repaired dry packet clears its previous error without writing to the Brain.
Full outcome snapshots include both original rows and possible new rows.

`node tests/test_adjudicator_retro_cli.js`: seven tests passed.
`PYTHONPATH=src python3 -m unittest discover -s tests -p test_adjudicator_retro_evidence.py -v`:
five tests passed. In temporary source copies, writing `outcomes.merged`, disabling
retrospective isolation and forcing a backend each made the new dispatch regression
fail (exit 1). Both copied modules were restored byte for byte; all seven Node tests
passed afterward (exit 0). These controls do not replace the named pytest controls.

Pytest and Black are absent from both Python environments, and PyPI name resolution
failed. No Python source changes or task-completion claims are made. The named
pytest acceptance tests and their required faults remain pending. The checkout's
Git directory is read-only, so this round also supplies an isolated commit, patch
and bundle under `/tmp` for transfer.

## Keepalive cost comparison regression, 2026-10-06

- [x] Verify retrospective agreement rates, measured costs and case counts in the
  saved report, including withdrawal and restoration of complete cost telemetry.
- [ ] Run the three named pytest acceptance tests and their required outcome-write
  and unconditional-offer faults on the current tree.

The new CLI regression withdraws a complete cost source and deletes another cost
record after both have been measured. The persisted summary and proposal comparison
must clear those stale costs, count a measured zero in the cost denominator, retain
the cost of an abstention and report unknown totals when no complete costs remain.
Restoring telemetry recovers the original totals. Agreement rates, comparison case
counts, saved role identities and the entire Brain snapshot remain unchanged by
each refresh.

`node tests/test_adjudicator_retro_cli.js`: eight tests passed.
`PYTHONPATH=src python3 -m unittest discover -s tests -p test_adjudicator_retro_evidence.py -v`:
five tests passed. In a temporary source copy, retaining a saved measured cost
instead of refreshing it made the new test fail: it reported 4.5 instead of 0.5.
The copied Python source was restored byte for byte, and the regression passed.
Repository Python files are unchanged; no Python formatting claim is made.

Pytest and Black are still unavailable. Local cache/tool searches and installation
attempts could not supply them, so the named pytest acceptance tests and their
specific deliberate-break controls remain pending. The checkout's Git metadata is
read-only; the test and this note are committed through an isolated Git directory
under `/tmp`, with a patch and bundle for transfer.

PR #507 was verified open with `draft=false` at head
`8ec62f83de2281e12d46be8e2ad61482844087c0`. Attempts to reconcile the three verified
implementation checkboxes, add `needs-human` and post the validation/blocker comment
were each rejected by the connector: mutations require approval, while this run's
approval policy is `never`. Remote tracking remains unchanged.

## Keepalive mixed-disposition comparison, 2026-10-07

Reviewed the recent collector and keepalive commits before continuing. The report,
contested-verdict closer binding and switch-review line already exist. All eight
existing CLI regressions passed before changes, supporting reconciliation of those
three implementation checkboxes; the two named pytest acceptance checkboxes remain
unchecked. The connector rejected the PR-body update, `needs-human` label and blocker
comment because mutations require approval and this run's approval policy is `never`. The proposed
reconciled body is retained in `/tmp/orchestrator-pr507-reconciled-body.md`.

The added JavaScript regression compares raw proposals against later truth across
merged, unmerged and unknown merge dispositions, including a reworked outcome.
An unknown merge disposition excludes its case from both agreement denominators;
when that fact arrives, both denominators admit it. Measured costs retain their
independent population, including an abstention and a measured zero. Saved proposal
identities, effective evidence floors and complete Brain snapshots remain unchanged.

All nine CLI tests, all five evidence unittest tests and the three module selftest
checks passed. In temporary source copies, forcing known merge dispositions to PASS
and admitting unknown merge dispositions each made the new regression fail. Both
copies were restored byte for byte; repository Python source is unchanged.

Pytest and Black are unavailable in both Python environments checked; an installation
attempt could not resolve a pytest distribution. The named pytest acceptance tests
and their specific outcome-write/unconditional-offer faults remain pending. This
round changes JavaScript tests and this note only. Git cannot create `index.lock` in
the read-only checkout metadata; an isolated Git directory holds the commit and a
bundle for transfer.
