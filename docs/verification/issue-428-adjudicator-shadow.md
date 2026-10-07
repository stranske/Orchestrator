# Issue 428: retrospective adjudicator evidence

This extends the existing AdjudicatorAgent and Brain role-run recorder. Source implementation
and local replay are distinct from runtime publication, which remains manual.

## Keepalive gate-page evidence validation, 2026-10-07

- [x] Verify packet construction from the merge-bound verifier comment, merged diff
  summary and complete gate pages, router-chosen shadow dispatch, retrospective Brain
  role recording and unchanged outcomes using the existing collector and role.
- [ ] Run the three named pytest acceptance tests and their required outcome-write
  and unconditional-offer fault checks on the current tree.

Seven new Node regressions exercise the real `fetch_evidence`, packet builder and
AdjudicatorAgent with private persisted disputes and mocked GitHub reads/transport.
Both CheckRun and StatusContext gate entries reach the actual role prompt after a
three-page read. Head changes, timeouts, absent gates, the 20-page read bound and
truncated comment/diff inventories prevent routing, dispatch and Brain writes.
The complete case records `source=retrospective`; every case preserves all outcomes.

`node tests/test_adjudicator_retro_cli.js`: 25 passed, no skips. The five existing
evidence unittest tests also passed. In temporary source copies, dropping later
page entries, allowing a changed head and increasing the read bound to 21 each
failed its new regression (exit 1). All copies were restored byte for byte; the
checkout's Python source remained unchanged. Pytest and Black are absent;
isolated installation from PyPI failed. These Node checks do not substitute for
the pending named pytest acceptance checks.

GitHub access failed, preventing PR checkbox, blocker-label and readiness updates.
The checkout's Git metadata is read-only, so the focused test change and this note
are committed using an isolated Git directory under `/tmp`, with a patch and
bundle for transfer. The shared checkout retains the edits.

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


## Closer recovery: exact Git entry binding (PR #507)

The offline collector now treats inventory paths as literal Git paths. It accepts
exactly one NUL-terminated `ls-tree` entry whose filename exactly matches the
requested path, then reads size and content using that validated immutable blob
SHA. Directories, pathspec ambiguity, mismatched names, multiple entries and
unterminated output stay incomplete before blob reads. Literal metacharacters in
an existing filename remain supported. This changes no shadow adjudication,
report-write authority, Brain admission or retrospective cost policy.

Validation at the existing PR head plus this bounded repair: 50 affected Python
tests pass (including 25 collector cases), 10 Node CLI tests pass, module selftest
3 checks pass, Mypy and Ruff pass, and the full repository Black check leaves
368 files unchanged. Exact collection is 2543. The 2536 prior executed-node floor plus
7 new exercised nodes establishes the conservative union; every existing ceiling
is preserved. Hosted complete verification remains required; no new local full
suite execution is claimed.

Actual production control: with final tests held fixed, replace only the
collector source with the original 1c9266d version. Four regression executions fail
(trailing-slash directory, mismatched name, multiple entries and unterminated
entry); byte-identical candidate restoration returns 25 collector tests GREEN.
This establishes sensitivity to the source behavior, rather than a fixture-only
failure. The broader issue #428 remains open for its remaining evidence transport,
acceptance inventories, measured cost and later-truth requirements.


## Explicit evaluated-revision acceptance artifact transport

Dedup: the existing collector and its CLI already own immutable source byte collection.
The previous validator unconditionally marked every acceptance location missing. Concept searches,
current capability inventory, historical dormancy scan and the improvement-log accessor were
checked; this extends that collector rather than creating a second role, report or transport.
Sol6.1Medium assessment d517c8b91366feef94b24c035c4c1875032c1019f425cb057052f63bc345f2e2
confirmed this narrow increment and identified offline/lazy-fetch and replacement-object fences.
The assessed topic files at its a921fa7 checkout are byte-identical to merged main80c26f1; this
was independently checked after fetching main. The assessment is design evidence, not a test result.

An explicit saved requirement may now use `{"criterion":"named transcript",
"location":"git-path:docs/evidence/result.log"}`. The collector resolves that literal regular
blob only at the case's evaluated SHA; it retains the full UTF-8 bytes, Git blob ID, size and
SHA256, plus the exact criterion/location and inventory slot. Unsupported transports, unsafe or
invalid paths, missing or nonregular objects, invalid UTF-8, oversized and contradictory content
remain incomplete. Metadata from the requirement cannot overwrite collector provenance. Git
reads disable lazy fetching and replacement objects, preserving the no-network boundary.

`complete` means only complete byte collection for the supplied inventory, as explicitly recorded
by `completeness_scope=supplied_inventory_only`, `inventory_exhaustiveness=unverified` and
`acceptance_semantics=unassessed`. Possessing a test file or log does not prove the required
controls ran successfully. Empty/missing acceptance inventories still block collection. No
original saved report, metadata floor, outcomes, costs or accepted adjudications are changed.
Machine-local and hosted artifact transports remain unresolved; source428 stays OPEN.

The existing CLI takes the explicit inventory already saved in its report input:
`python3 src/adjudicator_retro.py --collect-case CASE --report saved.json --output collected.json
--repository /local/repo`. The output must remain separate from the saved report.

Validation: 82 focused tests plus 23 subtests, 10 CLI tests, and all six module self-test checks pass. Exact collection is 2570; the floor adds 27 exercised nodes to the previous 2543 union, with every ceiling preserved. This does not claim a new full-suite run. Two deliberate faults (reading HEAD instead of the evaluated commit, and letting inventory fields overwrite provenance) each fail the targeted regression; byte-identical restoration passes. Black, Ruff and mypy pass. A supplied partial real inventory independently retrieved 12515 bytes whose SHA-256 and Git blob identity matched the evaluated commit; this demonstrates transport only, not exhaustive or semantic acceptance.

Same-round PR508 review found inherited Git repository-location variables could override the supplied repository. The child environment now removes GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_COMMON_DIR and GIT_NAMESPACE while retaining ordinary environment and the offline/identity fences. An actual foreign-GIT_DIR regression fails on 705a17e and passes after repair; seven additional environment cases verify stripping. Final focused gate 90 PASS / 23 subtests; exact collection 2578 with every ceiling preserved, not a new full-suite execution claim.

Keepalive CLI verification adds five end-to-end acceptance collection regressions: empty content,
Unicode content and a Unicode/tab/newline path, CRLF content, an executable file without a final
newline, and valid UTF-8 containing a NUL byte. Each invokes the production `--collect-case` CLI
after moving HEAD and deleting the working file, then independently checks the saved JSON content,
byte length, Git blob identity and SHA-256. The nonempty cases use an exact byte-length limit.
Every case also checks inventory identity, the collection-only scope markers, an unchanged saved
report and no Brain database creation. `node tests/test_adjudicator_retro_cli.js` passes all 15
tests; `python3 src/adjudicator_retro.py --selftest` passes all six checks. JavaScript syntax and
diff whitespace checks pass. No Python files or pytest collection counts change. The Python
suite and Black were unavailable in this runner, and package installation was blocked by network
access; the earlier full hosted verification requirement remains outstanding.

Verified keepalive task:

- [x] Retrospective collection can now capture acceptance artifacts stored at literal Git paths
  in the evaluated revision, recording their content and provenance.

Keepalive gap verification adds an end-to-end CLI regression with two identical valid acceptance
declarations and eleven unresolved locations in one supplied inventory. It verifies separate
inventory indices, preserved valid source/artifact bytes, and an exact missing-evidence gap for
each unavailable, unsafe, oversized, non-UTF-8, symlink, directory, gitlink, literal glob or
unsupported transport location. The oversized record retains its size without content or a
content hash. Both CLI output and the saved collection agree on the gaps; scope markers, the
original report and the absence of a Brain database are checked. The shared Git fixture helper
retains the existing isolated child environment and file-based output capture.

Validation in this round: all 16 CLI tests and all six collector self-test checks pass, as do
JavaScript syntax and diff whitespace checks. No Python files or pytest collection counts change.
Pytest and Black are unavailable in this runner; full hosted verification remains outstanding.
Recent commits 705a17e, 199a9a9 and 47e63d1 were reviewed before this follow-up. The GitHub
connector rejected the PR checklist reconciliation because its approval policy is `never`, so
the verified task state below has not been applied to the PR body in this round.

- [x] **New Features**
  - [x] Retrospective collection can capture acceptance artifacts at literal Git paths in the
    evaluated revision, recording their content and provenance.
  - [x] Collection checks artifacts against the supplied inventory and reports gaps for
    unavailable, unsafe, oversized, or invalid content.
  - [x] Results clarify that collection covers only the supplied inventory and does not verify
    its completeness or acceptance success.
- [x] **Documentation**
  - [x] Added guidance on acceptance-artifact collection and its limitations.

Further keepalive verification adds a CLI regression supplying false completeness, evaluated
revision, path, blob identity, byte length, content hash, content and inventory-index fields for
valid, missing and unsupported artifacts. Collected provenance remains authoritative, and the
unresolved entries retain their inventory gaps. All 17 CLI tests pass; the baseline six module
self-test checks, JavaScript syntax and diff whitespace checks pass. No Python files or pytest
collection counts change; pytest and Black remain unavailable. PR508 was verified open and ready
for review. Checklist reconciliation was attempted again, but the connector rejected the PR-body
update because approval policy is `never`; the checked local list above remains the verified state.
The local commit attempt was also blocked: `.git/index.lock` cannot be created because the Git
directory is mounted read-only. The test and this validation note remain uncommitted in the workspace.

The collection CLI summary now exposes the same `completeness_scope=supplied_inventory_only`,
`inventory_exhaustiveness=unverified` and `acceptance_semantics=unassessed` fields as the saved
collection, for both complete and incomplete results. A new end-to-end regression collects a
failure transcript from a partial inventory: byte collection succeeds while acceptance stays
unassessed, even when the saved case and inventory claim otherwise. An undeclared artifact is
not collected; saved verdicts/costs remain unchanged and no Brain database is created.

This round reviewed commits 705a17e through 2d96f20 before continuing and verified the existing
17 CLI tests and six self-test checks. After the summary change, all 18 CLI tests and six self-test
checks pass; JavaScript syntax and diff whitespace checks pass. Pytest and Black remain unavailable,
including after an unsuccessful installation attempt. Both required Black commands were attempted
and could not run, so these Python changes are not committed or pushed. No pytest nodes were added
and the collection floor is unchanged. Full hosted verification remains required. The PR checklist
reconciliation was attempted before implementation, but the connector required approval while the
run's approval policy is `never`; the verified local checklist above remains the task record.

## Keepalive bounded-batch comparison regression, 2026-10-07

- [x] Verify agreement against the merge rule, measured cost per case and counts for
  a bounded shadow batch containing binary proposals, an abstention and an invalid response.
- [ ] Run the three named pytest acceptance tests and their required outcome-write
  and unconditional-offer faults on the current tree.

The new CLI test uses the real adjudicator role with mocked routing and transport. Two
successive two-call batches preserve the four-case dispute population. Only two binary
proposals enter the comparison: proposal agreement is 50% and merge-rule agreement is
0%. All four measured calls, including the zero-cost abstention and paid invalid response,
contribute to the $4.75 total and $1.1875 cost per measured case. The saved report matches
the returned report; resuming repeats no calls, preserves original runs and outcomes,
and records exactly four retrospective role runs. Effective verdicts remain ungraded.

Validation: `node tests/test_adjudicator_retro_cli.js` passes all 26 tests. The five
existing evidence unittest tests pass. In temporary production-source copies, excluding
paid invalid responses from measured costs and admitting abstentions to the binary
comparison each fail the new test; restoring the source makes it pass. These controls
do not replace the named pytest acceptance faults. Diff whitespace checks pass.

Pytest and Black are absent from every installed Python interpreter; the attempted
PyPI installation failed on DNS resolution. No Python files change in this round.
The checkout's Git directory is read-only, so the tested JavaScript change and this
note are preserved as an isolated commit, patch and bundle under `/tmp` for transfer.

## Keepalive comparison and role-record reconciliation, 2026-10-07

Reviewed commits `8434b0f`, `008e003` and `0d2fa18` before continuing. Current-run CLI
verification supports these existing implementations:

- [x] Compare saved raw proposals with judged later truth and the merge-rule baseline;
  persist agreement rates, measured cost per case and counts in the state report.
- [x] Bind `role-adjudicator` on `closer-lane` with `requires_pr: contested_verdict`.
- [x] Render the adjudicator shadow cases/agreement/cost line in switch review.
- [ ] Run the three named pytest acceptance tests on the current tree.
- [ ] Run the outcome-write and unconditional-offer faults against those named pytest tests.

The initial CLI run passed 25/26 tests: the dispatch witness incorrectly paired report
rows ordered by dispute recency with Brain records ordered by target. It now joins by
role-run ID and explicitly verifies both recency orders, retaining all outcome, routing,
metadata and resume assertions. A new comparison regression removes one saved outcome:
both rules withdraw that case from their denominator, preserve paid costs and role-run
identities, and refresh the report without recreating the outcome.

Validation: all 28 CLI tests, five evidence unittest tests and seven retrospective selftest
checks pass. JavaScript syntax and diff whitespace checks pass. Isolated production-source
faults that write outcomes, offer unconditionally or retain truth for a missing outcome
each fail the corresponding CLI witness; byte-identical restoration passes. These CLI
controls do not replace the explicitly named pytest acceptance checks. No Python files or
pytest collection counts change.

Pytest and Black are absent; installing them into `/tmp` failed. The PR-body reconciliation
and `needs-human` label were rejected because connector mutations require approval and
this run's approval policy is `never`. The checkout's Git directory is read-only, so the
change is preserved as an isolated commit and patch under `/tmp`. PR #509 was confirmed
open and ready for review. The two pytest acceptance checkboxes remain unchecked.

## Evaluated merge6ed9809: named pytest acceptance revalidated

[Fresh named acceptance receipt](issue-428-named-acceptance-20261007/README.md)
retains39passed and actual outcome-write/unconditional-offer faults with exact
restoration. It supersedes earlier pytest-unavailable notes for these exact
current tests, while preserving actual509compareCONCERNS and the source428
inventory/adjudication/cost/later-truth remainder. No whole-issuePASS is claimed.
