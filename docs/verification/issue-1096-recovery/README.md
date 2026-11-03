# Acceptance evidence recovery for #1096

## Retained attempt diagnostics, 2026-11-03 UTC

The retry inventory now exposes ordered attempt observations, including nested
paid history, failure-field locations and JSON-value SHA-256 hashes. It separates
saved decisions, abstentions, failures, missing decisions and conflicting saved
fields. Failed retries and Brain persistence errors do not erase a parent binary
decision. Only uniquely matched manifest identities receive attempt references.
Private failure prose is omitted; inspect the original field to establish the
first actual broken step. These observations do not authenticate any outcome.

All [95 relevant regressions](attempt-diagnostics/validation.json) pass: 47 inventory,
26 runner and 22 ledger checks. Source commit `eac39f3eb8b6a1a59457d39a6ceeb0e5bcfc3275`
has checkout `6749ae79c00be3c9362a01ce117e46dc304d931e` as its parent.
The [required-test receipt](attempt-diagnostics/acceptance-run/checks.json), captured
before editing, again records missing pytest before collection. The
[pre-execution plan](attempt-diagnostics-plan.md) preserves the command and bounds.
No Python files changed.

The same earlier backup was [re-fetched and counted in memory](attempt-diagnostics/historical-acquisition.json):
58 rows, 64 recursive attempts, 26 `error`, eight `errors` and 11 `retry_error`
fields. These overlapping field counts are historical observations. Raw report
text was not written or passed through the new CLI. It is not the authenticated
native original, and no requested retry manifest was recovered.

Recent commits were reviewed; no additional task is complete. The local task
reconciliation below remains authoritative: recovery is partial, only the
pre-execution inventory and ledger tasks plus attribution/disclosure criterion
are verified complete. PR-body correction, blocker comment and `needs-human`
label were rejected because connector mutations require approval and this run's
policy is `never`. Remote checkboxes are unchanged. No provider call, publication,
production write or mirror synchronization occurred.

Git metadata is read-only. Source/evidence commits are preserved under
`/tmp/issue1096-attempt-diagnostics-git-8to3n09t/repo.git` and in the
[applyable patch](../../../artifacts/issue-1096-attempt-diagnostics.patch).

## Retained retry-manifest matching, 2026-11-03 UTC

The [bounded required-test attempt](retry-manifest-followup/acceptance-run/checks.json)
binds checkout revision `191dfb9f5bbc27a357cef8b5f16ad306f5eb3f45` to the selected
Python 3.14.9 interpreter, exact commands and output hashes. Collection, the four
named tests and the full retrospective suite exit 1 before collection with missing
pytest. Ordinary and isolated pip installation found no available distribution.
The [pre-execution plan](retry-manifest-followup/plan.md) records the stop rule.
The accepted deployment remains unverified; the required-test task stays open.

With that task blocked, the retained-report reader now accepts `--manifest` to
account for supplied retry identities without a Brain database. It preserves
manifest order and distinguishes matched, missing, ambiguous, incomplete and
conflicting identities, with retained-row locations. Version 2 matches the run,
target and verdict pair; legacy version 1 retains its narrower run/target scope.
Multiple saved rows remain ambiguous. Saved decisions, later truth and recursive
paid history are preserved; manifest matching does not authenticate outcomes or
establish original cohort provenance. Changing manifests invalidate the inventory.

All [92 regressions](retry-manifest-followup/validation.json) pass: 44 inventory,
26 runner and 22 ledger tests. The source commit is
`ebf5f3b1bd998e2f4f8ce8cff6fda7b5055079b2`, parent `191dfb9f5bbc27a357cef8b5f16ad306f5eb3f45`.
Only fixture manifests were exercised: the original requested retry manifest,
authenticated native receipts, deployment binding and fixed next-20 manifest are
still unavailable. No new task checkbox is complete. No provider call, production
write, dispatch or mirror synchronization occurred. No Python files changed.

Checkout Git metadata is read-only. The source/evidence commits are preserved in
`/tmp/issue1096-retry-manifest-git-j4117ize` and the
[applyable patch](../../../artifacts/issue-1096-retry-manifest.patch). The checkout
branch is unchanged.

## Ledger audit follow-up, 2026-11-03 UTC

The published [evidence ledger](evidence-ledger.json) now has a read-only
[audit command](../../../scripts/audit_adjudicator_evidence_ledger.js):

```sh
node scripts/audit_adjudicator_evidence_ledger.js \
  --ledger docs/verification/issue-1096-recovery/evidence-ledger.json
```

The [audit result](ledger-audit/audit.json) binds the ledger bytes, inventories
local artifacts, checks referenced receipt/log hashes, and retains separate
historical, current, mirror and dispatch attribution. External references remain
unauthenticated. This recovery audit does not verify historical source revisions,
deployment identity, native receipts or operational acceptance. It rejects grade
substitution, replacement cohort counts, lost October 18 disclosures, changed
logs, missing checksums and artifact references that escape its directory.

All [88 regressions](ledger-audit/validation.json) pass: 22 ledger, 26 runner and
40 inventory tests. Source commit `ff02e2e756f61828862b206c1a3795b1b3182b17`
has checkout `d84ae822cb6181d53e9a993712807dfc49fd41a5` as its parent.
The new [required-test receipt](ledger-audit-tests/checks.json), evaluated against
that checkout before source edits, still records missing pytest before collection
for both required test commands. The [plan](ledger-audit-plan.md) was written first.

Reconciled local tasks, after reviewing the recent commits and verifying the
ledger audit:

- [ ] Recover accepted/deployed/mirror bindings, original rows and fixed cohort;
  recovery remains partial, including unavailable raw October 18 proof.
- [x] Record the pre-execution command, expectation and stop rule; inventory paid
  attempts and reconcile dispatch protection with unverified writer ownership.
- [ ] Pass the four named tests and full retrospective suite on the accepted revision.
- [ ] Reconcile all original retries and perform eligible authenticated retries.
- [ ] Establish intermediate graded >=25 and canonical graded >=29.
- [ ] Recover at least 15 dated decisions in the original fixed next-20 cohort.
- [x] Publish the separately attributed evidence ledger.

The attribution/missing-proof disclosure acceptance criterion is verified; the
other four acceptance criteria remain open. No provider call, native journal
write, publication or mirror synchronization occurred. No Python files changed,
so the Python-specific Black gate does not apply.

Remote reconciliation, the blocker comment and `needs-human` label were rejected
because connector mutations require approval and this run's policy is `never`.
Remote checkboxes were not changed. Git metadata is read-only, so the source and
evidence commits are preserved under `/tmp/issue1096-ledger-git-vlq_uknf/.git` and
in the [applyable patch](../../../artifacts/issue-1096-ledger-audit.patch).

## Interpreter portability follow-up, 2026-11-03 at 19:43 UTC

The previous runner regression fixture hardcoded Python 3.14.9, which is absent
in this environment. Its initial run passed 16 tests and failed seven. It now
resolves an available parser interpreter; all 26 runner regressions and 40 inventory
regressions pass. The production runner also preserves the JUnit reader's command,
exit status, raw log and hash separately from the pytest invocation. Parser failure
and invalid output remain unsuccessful evidence. [Validation and log hashes](interpreter-followup/validation.json)
bind these observations to source commit `96dde1810f56562e0e23af6f0e603b3e48e7728a`.

The [pre-execution plan](interpreter-followup/plan.md) selected the current Python
3.14.10 installation. [Collection, the four named tests and the full suite](interpreter-followup/acceptance-run/checks.json)
all exit 1 before collection with `No module named pytest`. No new task is complete;
accepted deployment identity, authentic native grading/retry inputs and the fixed
next-20 manifest remain unavailable. Targeted connected searches recovered no fixed
manifest or individual original-retry artifact beyond the existing references.

Recent commits were reviewed before continuing. They add recovery inventories,
pre-dispatch checks and test-runner validation, not passing operational acceptance.
The attempted PR-body reconciliation of the duplicated recovery/pre-execution
checkboxes was rejected: the connector requires approval and this run's policy is
`never`. No PR-body change is claimed. PR #1097 was rechecked open with `draft=false`.

No Python source changed, so the Python-specific Black gate does not apply.
The checkout Git metadata remains read-only. The source and evidence commits are
preserved in `/tmp/issue1096-interpreter-git-oe_clm4j` and the accompanying
[patch](../../../artifacts/issue-1096-interpreter-followup.patch).

## Suite completeness follow-up, 2026-11-03

The runner previously accepted a full-suite JUnit file containing only the four
named tests. It now requires exact identities from a separate collection,
including parameterized cases, and rejects missing or duplicate suite results.
Its revision check also covers the runner and root pytest configuration.
All [23 runner regressions](suite-completeness-run/runner-tests.log) pass.

[The bounded run](suite-completeness-run/checks.json) evaluates source commit
`b2e375553429ab2f632b41214e02f904204b9fa2`, parent `bfee009c368dd170682524924dd7e3a58678d1ee`.
Collection, named tests and the full suite still fail before collection because
the selected interpreter lacks pytest. A bounded temporary installation attempt
found no available distribution. The accepted deployment remains unverified;
no task checkbox is newly complete. [The plan](suite-completeness-plan.md) records
the command and stop rule. Checkout Git metadata is read-only, so the source and
evidence commits are preserved in isolated Git storage and
`artifacts/issue-1096-suite-completeness.patch`.

The connector rejected the blocker comment and `needs-human` label because they
require approval and this run's approval policy is `never`. PR #1097 was observed
open with `draft=false`; this run made no remote changes.

## Required-test follow-up at 19:06 UTC

The four named tests and full retrospective suite were attempted using the selected
Python 3.14.9 interpreter on isolated source commit
`42ee516d3219c98129a9f4837a1edd35937148d8`. Both exit 1 before collection with
`No module named pytest`. [The receipt](acceptance-run/checks.json) binds exact
commands, logs, source hashes, tree identity and unchanged inputs. The accepted
deployment is still unverified. This task and all operational acceptance remain open.

The new [repository test runner](../../../scripts/run_adjudicator_acceptance.js)
preserves both failed commands and cannot report success from absent, empty,
skipped, duplicate or unrelated JUnit evidence. It bounds each command to 60 seconds,
requires explicit revision/interpreter selection, refuses dirty inputs and existing
output directories, and invalidates changing inputs. Its 14 regression tests and
the existing 40 inventory tests pass; commands, output hashes and source hashes are
in [runner-validation.json](acceptance-run/runner-validation.json). No Python files
changed, so the Python-specific Black gate does not apply.

[The pre-execution plan](acceptance-test-plan.md) records the expected failure and
stop rule. There was no provider call, canonical publication or mirror write. An
environment with pytest available to the selected interpreter and evidence binding
the accepted revision are required for the next test attempt. The original native
receipts, writer coordination and fixed cohort gaps also remain unchanged.

The checkout's Git metadata is read-only. The source commit and the subsequent
evidence commit are preserved in `/tmp/issue-1096-acceptance-git-1gfr70z1`; the
two-commit [patch](../../../artifacts/issue-1096-acceptance-tests.patch) applies to
checkout HEAD `0ecb2c1`. The original branch is unchanged. PR #1097 was observed
open with `draft=false` at handoff. Earlier observations below retain their
original check context.

GitHub rejected this follow-up's blocker comment and `needs-human` label because
connector mutations require approval while the run's approval policy is `never`.
Neither remote change is claimed. The required test task stays unchecked.

## Earlier recovery observations

The first task is advanced, not complete. Current deployed/mirror revisions,
the immutable native source and journal, live writer ownership, and the previously
fixed next-20 manifest have not been recovered. Do not replace that manifest with
a newly selected cohort. No production dispatch or publication is authorized by
this inventory.

## Recorded before the real-input inventory

Check date: 2026-11-03 UTC. Selected repository interpreter:
`/opt/hostedtoolcache/Python/3.14.9/x64/bin/python` (the CI major/minor).
Local checkout: `27fa292335fcb3da15f203ab37ae1004d5cd2db2`. This is the
implementation/test source under inspection, not an accepted deployment claim.

Recovered input: Dropbox file `id:tDSHdCztYNUAAAAAADfgrw`, revision
`65de8f715bb690369d613`, at
`/Learning/Code/Audits/Orchestrator/2026-10-15-w2-followups/regrade/adjudicator-retro-BEFORE-2026-10-15T2256Z.json`.
It was fetched through the connected Dropbox text extractor into a private local
temporary file. Its file metadata records 2,386,539 bytes. The local file hash
binds the extracted text; extraction does not establish original byte identity.
The private report contains provider material and is not committed here.

Command to execute once, with the measured UTC UNIX time replacing CHECK_TIME:

```sh
node scripts/export_adjudicator_inventory.js \
  --report /tmp/issue-1096-recovered-historical-report.json \
  --observed-at CHECK_TIME \
  --evidence-class historical_snapshot \
  --evidence-reference 'dropbox:id:tDSHdCztYNUAAAAAADfgrw@65de8f715bb690369d613'
```

Expected result: 58 retained rows in saved order, 38 unique recursive paid
identities, retained population 40 and retained graded 0. The disagreement
between population and rows must remain visible. This snapshot is dated by its
own `generated_at`, separately from the October 15 backup and November 3 recovery.
It cannot establish which 40 cases formed the initially requested cohort.

Bounded stop rule: one local read-only inventory, no provider invocation, Brain
read, native journal initialization, canonical overwrite, or mirror write. Stop
on malformed/duplicate case identities or changing input. Missing production
artifacts remain unavailable. A successful inventory is not operational acceptance.

The requested `--dispatch --retry --limit 21` is not executed: the retained
canonical publication requires the existing authenticated native admission and
publication path, its immutable source/journal/qualification manifests, fresh
independent truth, and reconciliation with the live writer. Neither an old
completion note nor this runner's empty runtime directory proves current writer
ownership. No new paid attempt is justified from this snapshot alone.

## Observed result and remaining gaps

The one real-input inventory matched the expectations above. Its output is in
`historical-inventory.json`; `evidence-ledger.json` separates recovered historical
observations, this run's observations, historical mirror synchronization, and
historical dispatch references. Raw native authentication receipts are still
unavailable. The local extracted file is 2,386,541 bytes, two more than the remote
file metadata: its SHA-256 binds local extracted text only.

October 18 is **not wholly missing**. The dated
[tracker observation](https://github.com/stranske/Orchestrator/issues/575#issuecomment-6251544073)
and the connected capability-program audit record both report saved graded 1,
generic in-memory recount graded 0, advice graded 2, and live graded 0. The raw
Brain snapshot, native receipt authentication, and fixed next-20 membership
proof for that date were not recovered. The generic recount does not replace
the native canonical grade. The October 22 follow-up separately reports advice
graded 4 against the still-stale saved advice count 2. Neither is graded >=25
or >=29 evidence.

The historical canonical publication reference is SHA-256
`f4117a581b36e02925511fd70e6f8a6debda5ed073ab80b6301906eb29d7d56a`,
with immutable source
`8a6649a27bb5ee05001cc580691a98aa26404f0f3a0c85d845ada9e6046a069c`.
That later source has 108 original paid identities plus two native ones according
to its retained observation. It is not the earlier recovered snapshot with 38
paid identities. Those counts must not be substituted for one another.

Checks and source hashes are bound in `checks.json`. Inventory regressions and
the existing Python module selftest pass. The four named pytest nodes and full
retrospective suite both fail before collection because pytest is absent.
Installing it was attempted and failed on blocked network name resolution.
No Python files changed; the Python pre-commit Black condition does not apply.
No acceptance checkbox is marked complete by these partial results.

The runner refused normal staging with `.git/index.lock: Read-only file system`.
The source, tests and receipts are committed using an isolated Git directory
under `/tmp/issue-1096-review-commit`, with this checkout as its work tree and
the exact checkout HEAD as parent. The accompanying format-patch is available
at `artifacts/issue-1096-recovery.patch`. The automation branch itself is unchanged.

Retry-manifest follow-up handoff: PR #1097 was checked open with `draft=false`.
Automatic approval review rejected this run's blocker comment and `needs-human`
label because connector writes require approval while the run policy is `never`.
Neither remote change is claimed; the blockers and required inputs are recorded
locally above.
