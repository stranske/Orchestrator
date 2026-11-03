# Original-cohort native candidate

Before any dispatch, read the immutable source, current canonical report and
existing native journal with the non-mutating inventory mode:

```sh
python src/adjudicator_retro.py --inventory \
  --report /absolute/original-report.json \
  --canonical-report /absolute/capability-program/adjudicator-retro.json \
  --native-journal /absolute/native-attempts.sqlite \
  --deployed-root /absolute/deployed-checkout \
  --mirror-root /absolute/published-mirror
```

The JSON on stdout pins report hashes, ordered original case IDs, recursive paid
attempt identities, and every journal attempt, including other source reports
and unresolved reservations. Raw response and parsed-result bytes are represented
only by hashes. It refuses a missing/foreign journal without creating or repairing
it, rejects changed canonical lineage, and aborts if report or journal inputs
change during observation. It cannot dispatch, publish, refresh truth or repair
role records; `--dispatch`, `--retry` and other modes are refused.

Code observations retain the selected path, resolved mirror generation, script
hash, and checkout HEAD/dirty status when that location owns Git metadata. They
do not prove that a scheduler or running reader selected those paths. Omitted
code locations and historical observations are explicitly unavailable. Retained
canonical summary numbers and journal validation states are inventory facts,
not authenticated grading or retry authorization. Native publication still
requires fresh collector capabilities and journal authentication below.

Issue #1072 runner check on 2026-11-03: production state and the deployed mirror
are unavailable in the CI workspace. The retained source-issue observation
([receipt](https://github.com/stranske/Orchestrator/issues/579#issuecomment-6229128915))
reports 58 rows, 108 original plus two native paid identities and one grade;
these are historical claims, not a current inventory. Neither October 18 proof
nor a fixed next-20 live-shadow observation was recovered here. The operational
inventory task remains incomplete until this mode runs against the authentic
runtime inputs; no dispatch was attempted from this workspace.

Local implementation validation: all 482 adjudicator Python tests pass with
`-m "not slow"`, including 19 inventory controls and the four original named
regressions; the module selftest passes 23 checks. Full collection measures
14,721 tests. The separate JavaScript CLI suite has 47 passing and 16 failing
tests; all 16 failing test names reproduce on unchanged HEAD, including its
stale 180-second timeout expectation. Existing retry/publication behavior was
not changed by this inventory mode.

The required repository-wide Black check passes with the pinned Black 26.10.1:
993 Python files remain unchanged at line length 100. The official formatter
ran serially because this sandbox cannot use its process executor. Staging the
change was refused with `.git/index.lock: Read-only file system`; the runner's
read-only Git metadata prevents the required commit in this environment.

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
All original recursive histories and the selected current receipt contracts are
validated before any later-truth read. `native_receipt_scope` explicitly separates
selected contract-checked receipts from retained, ungraded historical revisions.
A journal state named validated is not proof of a valid binary verdict. Repeated
original paid identities are retained as source facts and exposed through
`original_paid_identity_summary`; they never create an additional native grade.
New native backend IDs must be unique and disjoint from every original paid ID. A late failure publishes no partial
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

## Canonical publication

`publish_native_cohort` is the only path that writes an authenticated native
cohort to the canonical report. It starts from immutable source-report bytes,
requires a fresh opaque collected case for every validated journal case, and
rebuilds the candidate through `refresh_native_cohort`; a saved projection is
never an authority input.

Publication takes source and canonical SHA-256 compare-and-swap values and a
nonblocking `.orchestrator-mirror.publish.lock`. The canonical file must be the
immutable source on first publication, or a prior publication carrying the same
source hash. It is replaced only after a final source, journal and canonical
recheck, through an fsynced temporary file.

Original rows, their order, primary decisions, recursive paid histories, and
historical summary are retained. Native projections remain separate per row.
The canonical `summary` reports only authenticated effective native judgments;
its cost keys combine retained original and journal backend identities once.
Absent or unreadable accounting remains `unmeasured`, never zero.

Generic retrospective `run` and `_persist_report` refuse a report marked as a
native publication. The marker blocks accidental overwrite but cannot grant a
native grade: journal receipt authentication and fresh collection are repeated
for every publication.

Canonical and generic writers share the same destination lock, including custom
report paths; an alternate lock cannot be supplied to evade serialization. The
cohort CLI optionally accepts a `replay_runner` registry key per manifest entry.
It executes that fixed producer freshly and passes its opaque receipt into
qualification; manifest JSON cannot import a replay receipt or change its binding.

## Retry handoff for a native canonical report

The requested `--dispatch --retry --limit 21` command belongs to the generic
retrospective writer. Once the canonical report carries `native_publication`,
that command must stop before evidence reads, role-record repair or dispatch.
Its CLI error directs the operator to native admission and publication. Do not
remove the marker, replay a copy through the generic writer, or replace the
original report with the canonical publication.

After collecting the authentic inventory above, handle each eligible original
abstention through the existing single-case admission command:

```sh
python src/adjudicator_retro.py --native-admit ORIGINAL_CASE_ID \
  --report /absolute/immutable-original-report.json \
  --expected-source-hash PINNED_ORIGINAL_SHA256 \
  --case-input /absolute/enriched-original-case.json \
  --repository /absolute/evaluated-git-checkout \
  --qualification /absolute/qualification.json \
  --native-journal /absolute/native-attempts.sqlite \
  --output /absolute/new-case-projection.json \
  --dispatch
```

Omit `--dispatch` for preparation only. The enriched input preserves the original
identity, finding and evaluated-revision anchors; collection reissues an opaque
case from local Git objects. The journal preserves prior paid attempts and
refuses repeat spend for a consumed revision, a binary decision or an unresolved
started invocation. A missing raw response remains UNKNOWN. The requested limit
of 21 is an upper bound on selected eligible original retries, not permission
to redispatch 21 rows. Native admission does not use generic `--retry` or
`--limit`; record each actual outcome or its first broken step separately.

Build the manifest from **all** validated journal cases for the immutable source,
including prior admissions. Refresh canonical grading without another dispatch:

```sh
python src/adjudicator_retro.py \
  --native-publish /absolute/capability-program/adjudicator-retro.json \
  --report /absolute/immutable-original-report.json \
  --expected-source-hash PINNED_ORIGINAL_SHA256 \
  --expected-canonical-hash PINNED_CURRENT_CANONICAL_SHA256 \
  --native-journal /absolute/native-attempts.sqlite \
  --cohort-manifest /absolute/manifest.json
```

Publication needs no `--output`; `--native-cohort` still requires a fresh output
path for its separate candidate. Choose exactly one mode. Use the current
canonical hash for each publication and re-inventory after a compare-and-swap
failure. The publisher reauthenticates receipts and freshly collected cases,
preserves original decisions and history, and reads independent later truth.
An authenticated decision without later truth remains ungraded. Neither this
procedure nor fixture validation supplies missing production or October 18 proof.

Issue #1072 follow-up on 2026-11-03: the native CLI no longer requires an unused
output for publication, rejects simultaneous candidate/publication modes and
names missing hash inputs. Regression coverage exercises a single native
admission followed by two publications as independent later truth becomes
available, plus refusal of the original generic retry command before side effects.
Production execution and task completion remain unverified in this runner.

Direct Python verification passes the admission/two-publication scenario with
the real collector, SQLite journal and separate outcomes database, plus four CLI
refusal scenarios. The same publication scenario fails on unchanged `a7098f8`
because the CLI demands the unused output. The module selftest passes 23 checks.
These direct checks do not replace pytest: this runner has neither pytest nor
Black, and installation failed because PyPI DNS resolution is unavailable.
The targeted pytest command and required repository-wide Black command therefore
could not run. Staging also fails with `.git/index.lock: Read-only file system`;
no commit or push was made and no task checkbox was changed.
GitHub reports source issue #1072 closed and PR #1073 merged. Attempts to add
`needs-human` and post the blocker comment on the source issue were rejected:
the tools require approval while this run's approval policy is `never`.
