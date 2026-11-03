# Retained later-truth results

The exporter now distinguishes missing truth, explicit `UNKNOWN`, invalid values
and retained binary values that remain unverified. Each attempt has its own
value hash, location and next evidence step. Parent/retry values and adjudicator
decisions cannot supply missing independent truth. Existing saved decisions,
paid history, authentication boundaries and dispatch restrictions are preserved.

The three [negative controls](negative-controls.log) failed before implementation.
All [111 focused regressions](regressions.log) pass afterward, with no skipped
tests. [Validation](validation.json) binds the logs and source hashes to source
commit `e81b3e77c0ad615eeed479fb8f1e566cdec073c0`, whose parent is `9c00d5b`.
No Python files changed, so the Python-specific Black gate does not apply.

The CLI was exercised on the existing selected-field historical projection:
58 rows, 64 attempts, 30 retained binary truth values and 34 missing values.
Of 19 saved binary decisions, five have no recorded truth. Their case identities,
targets, locations and paid locators are in the [historical summary](historical-truth-summary.json).
These are historical observations from an earlier backup, not authenticated
outcomes, original retry-manifest reconciliation, independent current truth or
canonical grades. No threshold or live-cohort result is established.

The [required-test attempt](acceptance-run/checks.json), made on the checkout
revision before edits, preserves collection and both required commands failing
before collection because pytest is absent. Accepted deployment identity remains
unverified. No new task checkbox is complete.

Correct both PR checklists consistently: recovery remains unchecked; only the
pre-execution inventory and ledger tasks, plus attribution/disclosure criterion,
are complete. This corrects the preceding follow-up's claim that partial recovery
completed the recovery task. PR-body correction, blocker comment and `needs-human`
label were all rejected because connector mutations require approval and this
run's policy is `never`. No remote update is claimed.

Checkout Git metadata is read-only. The source commit is in
`/tmp/issue-1096-truth-git-dc5twqhn/repo.git`; the source/evidence patch is
[`artifacts/issue-1096-truth.patch`](../../../../artifacts/issue-1096-truth.patch).
The checkout branch has not advanced. No provider call, production write,
canonical publication or mirror synchronization occurred.
