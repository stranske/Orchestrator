# Issue 1104: per-case evidence gap export

- [ ] Persist a shadow result or a specific evidence gap for every authenticated requested case, including source revision, role-run provenance and measured costs or explicit UNKNOWN values.

This change advances gap accounting only. The original 71 identities and the
Brain remain unavailable, as recorded in the prior
[recovery ledger](../issue-1104-cohort-recovery/reconciliation.json). No
authenticated manifest, original per-case execution or operational acceptance
is claimed. No task is newly complete.

`scripts/export_adjudicator_case_gaps.js` exports one evidence-gap record for
each supplied manifest identity, using the existing read-only inventory reader.
It retains separate disputes on the same run, inventory reasons, manifest
SHA256, exclusions, observation time, count discrepancies and collector hashes.
Every uncollected shadow revision, result, role-run provenance and cost is
explicitly `UNKNOWN`. An eligible inventory row does not become a shadow result.
Excluded identities remain accounting-only records; excluded outcomes and
timestamps are not read, and no further inspection is requested for them.

For a subsequently recovered manifest, callers can preserve stdout to a new
file using this command shape:

```sh
node scripts/export_adjudicator_case_gaps.js \
  --db /path/to/brain.db --observed-at UNIX_SECONDS \
  --manifest /path/to/requested-manifest.json --expected-count 71
```

The exporter does not authenticate that manifest or collect existing shadow
results, roles or measured costs. Those require separate evidence recovery.
Unavailable inventory returns exit 2 **with** the requested per-case gaps;
invalid input returns exit 1 with empty stdout. An empty manifest preserves the
71 unsupplied slots and cannot establish acceptance. This command was not run
against the original cohort because its manifest has not been recovered.

Six new tests pass in [focused.log](focused.log). They check both manifest
versions, unavailable inventory, distinct disputes, missing/contradictory
evidence, explicit unknowns, exclusion-before-read, collector/input digests,
file-backed CLI persistence, invalid-input rejection and unchanged input bytes.
The excluded-evidence fixture raises if its timestamp or outcome is evaluated.
All five related test files pass in [tests.log](tests.log). JavaScript syntax
and whitespace checks pass. No Python source changes, so the Python formatting
gate does not apply.

The exact acceptance names and mutations were recovered from a fresh
authenticated read of [originating issue #428](https://github.com/stranske/Orchestrator/issues/428),
consistent with the retained [source snapshot](../issue-1104-cohort-recovery/source-428.json):

- `tests/test_adjudicator_retro.py::test_packet_builder_needs_target_finding_and_ground_truth`
- `tests/test_adjudicator_retro.py::test_retro_records_verdicts_without_changing_outcomes`
- `tests/test_adjudicator_retro.py::test_closer_lane_offers_the_adjudicator_only_on_a_contested_verdict`

Required mutations: permit retrospective writes to outcomes (second test must
fail), and offer without a contested verdict (third test must fail), then
restore. The [fresh attempt](named-acceptance-attempt.log) cannot import pytest
with `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`. The other installed
CPython interpreters also lack pytest and Black; package installation attempts
returned no available distributions. No named passes, mutations or restoration
proof are claimed. Prior synthetic receipts remain unchanged.

Recent commits `3976a84` and `b98989f` were reviewed before implementation; the
ten metadata-generator tests pass again. Only the generator task is verified
complete. The proposed PR body also checks its duplicate in the full issue
text; no operational task is checked. The connector rejected that update
because writes require approval and this run's approval policy is `never`.
The proposed body is preserved at `/tmp/issue-1104-reconciled-pr-body.md`.
Fresh authenticated readback confirms PR #1105 is open and `draft=false`, with
the duplicate still unchecked remotely.

The checkout's Git metadata is read-only: staging cannot create
`.git/index.lock`. A source/test commit and transfer patch are prepared with
separate Git metadata under `/tmp/issue-1104-case-gaps-delivery`. Workspace
changes remain available for automation to collect. Historical recovery,
per-case operational persistence, named safety proof, real closer observation,
summary/cost reconciliation, the 16 CLI dispositions and remote metadata
publication remain incomplete.
