# Original-cohort native candidate

Issue #579's merged #622 rail authenticates one original case at a time. A
single-case projection does not establish acceptance over the original cohort.
`refresh_native_cohort` joins all validated journal cases for the frozen original
report to fresh collector capabilities and independently reads later truth. It
makes no provider call, changes no journal or Brain record, and never replaces
the primary report. The original summary remains under `summary`; only the
explicit `native_summary` counts authenticated native grades. Unadmitted cases,
ungraded admitted cases, and pending attempts remain separate.

Run from the installed, guarded revision after native admission, with an existing
journal and a fresh output path:

```sh
python src/adjudicator_retro.py --native-cohort \
  --report /absolute/original-report.json \
  --expected-source-hash ORIGINAL_SHA256 \
  --native-journal /absolute/native-attempts.sqlite \
  --cohort-manifest /absolute/manifest.json \
  --output /absolute/new-cohort-candidate.json
```

The manifest is a list of objects with exactly `case_id`, `input_report`,
`repository`, and `qualification`. Paths resolve relative to the manifest.
Each validated case for this original report must appear exactly once; each
input report must contain exactly one matching row. The collector reissues the
opaque case from the evaluated Git objects and qualification, rather than
trusting a saved projection. Missing/corrupt journals fail without initialization.
All original recursive histories, identities and complete receipt contracts are
validated before any later-truth read. A late failure publishes no partial
cohort. Source/journal changes during refresh abort; output publication is atomic
and refuses replacement. A later primary publication must independently check
these input revisions again under the existing publisher and serial-writer gates.

The candidate retains original rows/history and all native attempt records. It
is private evidence, not a public attachment. Original cost fields still describe
the original attempts; native raw usage/error receipts remain separately visible
in `native_attempts`. No missing cost is treated as zero.

Validation on the candidate: 251 focused adjudicator/offload tests and 23
subtests pass; 15 new cohort cases cover multiple authenticated originals,
UNKNOWN later truth, preserved history, serialized-authority refusal, manifest
omissions, stale source hashes, duplicate paid identities, late contract/read
failures, changing inputs, exclusive output, and read-only journal admission.
Four independent guard-removal controls assertion-fail their intended tests;
byte-identical restoration passes all 15 cohort tests. Full hosted CI and the
seven-minute exact-head review floor remain delivery gates.

This bounded producer does not establish actual native graded >=29, original
cost coverage, canonical-primary publication, live shadow performance, or source
#579 completion. The original 58-case report and paid history remain unchanged.
