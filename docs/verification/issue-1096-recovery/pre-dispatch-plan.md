# Pre-dispatch inventory plan, 2026-11-03 UTC

Recorded before executing the updated exporter on recovered real input.
Source parent: ed048b3417585c8d3f5353b098341eeab7c2bd8c; changed JavaScript
will be bound by SHA-256 in the new validation receipt. This is a runner check,
not a claim that the source is the accepted deployed revision.

## Command and expected result

```sh
node scripts/export_adjudicator_inventory.js \
  --report /tmp/issue-1096-recovered-historical-report.json \
  --observed-at CHECK_TIME \
  --evidence-class historical_snapshot \
  --evidence-reference 'dropbox:id:tDSHdCztYNUAAAAAADfgrw@65de8f715bb690369d613'
```

Replace CHECK_TIME with the measured UTC UNIX time. Recover the same connected
Dropbox text input, not a new cohort. Expect 58 retained rows, population 40,
38 recursive paid identities, saved graded 0. The text extraction is not proof
of original byte identity. There is no available production native journal in
this runner, so omit `--journal`; native attempts and reconciliation counts must
be null/unavailable rather than zero. A runtime operator with the authentic
existing journal can supply `--journal EXISTING_NATIVE_SQLITE` to observe its
locators in a single read-only transaction. That still cannot authenticate grades.

Expected stop reasons include unavailable native journal, unverified live writer,
unverified accepted/deployed/mirror bindings, missing immutable source and case
qualification, and absent receipt authentication and independent later truth.
No result from this exporter authorizes dispatch.

## Bounded stop and native route

One recovered-input inventory only. Stop on changing input, malformed identities,
or unexpected counts. No provider call, Brain write, journal initialization,
reservation release, canonical publication, mirror write or publication-lock
acquisition. Native journal reads cannot prove whether a live writer is active;
an empty or absent journal cannot establish ownership.

The requested `--dispatch --retry --limit 21` cannot run against a native canonical
publication: `run` and `_persist_report` refuse it. Retain those protections.
The existing route is `admit_collected_retry` followed by `publish_native_cohort`,
using authenticated original source, qualified cases, journal and independent
truth; publication requires expected source/canonical hashes and the shared
destination lock. Native `reserved` attempts require producer proof before
release; `in_flight` remains UNKNOWN until a receipt is recovered; `raw_persisted`
resumes validation without redispatch. Even a `validated` row must be authenticated
by the native projection before grading. Live writer coordination remains a
separate required operational step, not something this runner may infer.

## Prior task reconciliation

Reviewed ed048b3: 11 committed files, including inventory code/tests and historical
evidence. Its 32 Node checks passed, but named pytest/full retrospective commands
failed before collection. Recovery remains partial: accepted/deployed/mirror
identities, native receipts and fixed next-20 membership are unavailable. No new
task is complete. The PR summary's checked recovery task should be reopened to
match its detailed unchecked task; all other checkboxes stay open.

GitHub rejected the PR-body correction because connector mutations require
approval and this run's policy is `never`. This file records the correction for
handoff; the remote checkbox is not claimed updated.
