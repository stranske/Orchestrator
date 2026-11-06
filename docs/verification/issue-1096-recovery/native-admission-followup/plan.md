# Native retry admission diagnostics

Recorded 2026-11-04 UTC before implementation on checkout `5435e6a`.
The first incomplete task remains original retry reconciliation. Existing evidence
does not identify an authenticated original retry manifest or current native journal,
and the current issue read supplies neither. No paid retry is eligible here.

Classify exact current native admission refusal messages in retained error fields.
Distinguish immutable report changes, collected-case identity, existing binary
decisions and reserved revisions. A refusal describes a failed admission, not a
new provider outcome or proof of no historical spend. Contradictory refusal and
execution fields must remain unresolved. Later retry errors must not overwrite a
parent's saved decision or borrow its paid receipt.

Run the inventory suite before implementation. Add controls for exact messages,
changed/contradictory messages and requested-row CLI recovery, and verify they
fail before implementing the classifications. Rerun the inventory and related
runner/ledger suites once after implementation; inspect output and input byte
preservation. No Python source changes are planned.

After committing source in writable Git storage, run once:

```sh
node scripts/run_adjudicator_acceptance.js --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 --revision FULL_SOURCE_COMMIT --output NEW_ABSOLUTE_DIRECTORY --timeout 60000
```

Expected acceptance result is four named tests and the full retrospective suite
passing. The selected interpreter currently lacks pytest; retain failed commands
without claiming acceptance. Bound each subprocess to 60 seconds and stop after
this attempt. No production dispatch, publication, mirror write, replacement
cohort or task completion follows from these diagnostics.
