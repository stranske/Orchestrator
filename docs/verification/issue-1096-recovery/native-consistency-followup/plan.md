# Native receipt consistency recovery plan

Recorded 2026-11-04 UTC before implementation against checkout
`5b761ed` (full identity will be retained in validation.json).

The first unchecked task is original retry reconciliation. Targeted connected
searches and the existing #579 and #575 receipts still do not supply the original
requested retry manifest or native journal. Historical ownership remains with
the existing native/program closer; current writer ownership is unverified.

The inventory currently removes an observed binary native result from unresolved
case recovery without checking whether its backend_run_id and raw_output agree
with the journal's backend_run_id and raw_response. The native validator requires
both equalities. Retain missing, malformed and contradictory receipt fields as
explicit evidence gaps. Consistency is necessary but cannot authenticate a
receipt, validate a proposal, qualify a case, refresh truth or authorize dispatch.
Do not publish private raw responses, error prose or proposal contents.

Baseline: run the inventory, acceptance runner and ledger regressions. Add
negative controls that fail against the existing source, implement the checks,
then rerun the focused suites. Verify CLI parity and report/manifest/journal byte
preservation. Record commands, counts, source hashes and output hashes.

Required Python acceptance command, after committing the source in writable Git
storage if necessary:

```sh
node scripts/run_adjudicator_acceptance.js --python /opt/hostedtoolcache/Python/3.14.10/x64/bin/python3 --revision <full-source-commit> --output <new-absolute-output-directory> --timeout 60000
```

Expected result: collection, four named tests and full retrospective suite pass
on the identified source. The selected Python currently lacks pytest. Run once,
with each subprocess bounded to 60 seconds, and retain any collection failure;
do not reinterpret it as a test pass or accepted deployment identity. Stop after
local verification and this bounded attempt. No paid dispatch, canonical update,
mirror sync or replacement cohort is authorized by the inventory. No Python
source edits are planned; the Python-specific Black gate does not apply.
