# Receipt metadata failure preserves refusal reporting

The two remaining tasks stay unchecked. No delivered deployment or seven-day
acceptance is established by this runner.

The JavaScript auditor previously threw when a collector receipt contained a
null segment entry. That lost the report even though the active and archived
ledger segments remained readable. The auditor now treats the invalid entry as
an observation mismatch, retains the refusal count, reports UNKNOWN coverage
and unmet acceptance, and exits 1.

`ledger-before.log` records the two new regressions failing with the null-path
TypeError while the existing 49 tests pass. `ledger-tests.log` records all 51
tests passing after the fix. The cases use real collector observations on
disposable inputs, then corrupt only segment metadata. They exercise empty and
four-refusal ledgers, both lanes, active and archived segments, CLI exit status,
and byte-for-byte input preservation. These are regression fixtures, not live
deployment evidence. Syntax and whitespace checks pass in `checks.exit`.
No repository Python, workflow, protected file or verification floor changed.

`verify-context.json` identifies the exact source revision, selected interpreter,
private runtime overrides and start time. The complete verifier output and exit
status are retained separately. The verifier began before the JavaScript repair;
the repair remains uncommitted, so this run cannot authenticate a final delivery.
Every installed interpreter lacks pytest;
`pytest-prerequisite.log` reproduces the error. Dependency installation into
`/tmp` failed (`dependency-install.log`). The independent local_verify and
admission reproductions also fail because pytest is unavailable. No
capability-admission or heartbeat implementation defect has been demonstrated.
The full verifier exited 1: zero pytest cases against floor 8636, 105 passing
module selftests, five prerequisite-skipped modules, and one failing module
(local_verify). Admission failed with missing pytest; the activation audit
passed. The run took about seven minutes, and `verify.log` is the complete
unabridged command output.
`floor-comparison.json` records a failed byte comparison against the local
origin/main Git object and both revision identifiers. Unlike previous rounds,
origin/main has advanced to 5c413d73 and its floor differs. `floor-counts.json`
records delivered counts 8636/8634 versus main's 8645/8643, and both hashes.
The delivered floor remains unchanged; equality is
another unmet acceptance condition.

`deployment-paths.log` records the absent runtime and mirror paths on this runner.
No deployment or fresh observation from the deployment host's existing hourly
collector occurred. The latest retained host audit still records 188 legitimate
refusals and an incomplete baseline; none is discounted by this change.

Task reconciliation reviewed recent commits through 86b3ce23, including the
round 7 implementation and host round 5 evidence. The three implementation tasks
remain checked; the two remaining tasks cannot be checked. Three acceptance
boxes (native deployment, successful delivered verifier, seven-day zero-refusal
ledger) should be unchecked. Automatic approval review rejected the PR-body
correction, blocker comment and needs-human label because approval policy is
never, so those remote updates were not applied.

`staging.log` records git add failing with a read-only .git/index.lock. This
prevents the requested source/test commit. The changes remain in the checked-out
PR branch's working tree; no branch or pull request was created.
`pr-readiness.json` records PR 1365 open and ready for review.
