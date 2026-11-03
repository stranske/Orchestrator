# Original-cohort native candidate

Issue #1072 runtime-dependency inventory follow-up (base `8e2cb85`): the
inventory now includes `runtime_module_sha256`, mapping each adjacent top-level
Python module name to its content hash in either the checkout's `src/` or the
flat mirror. A neighboring validator could previously change while the
retrospective script, HEAD and dirty flag stayed identical. The existing final
inventory comparison now rejects module changes, additions and removals too.
This records the selected local module files; `live_reader_binding` remains
unverified, and external dependencies and nested packages are outside this map.

Four new standard-library regression tests produced three failures and two
layout subtest errors before the repair. Afterward all 28 diagnostics, seven
native replay/publication tests and 23 module self-checks pass. `git diff --check`
passes. Full pytest execution/collection and both required Black commands remain
unavailable; installing the validation tools into `/tmp` failed. The test floor
needs fresh full collection when pytest is available. No commit or push was
attempted because the mandatory Black gate cannot pass.

Reconciliation of the prior commits found no newly satisfied operational task.
Authentic deployment, mirror, original report and journal inputs remain absent;
no production inventory or dispatch occurred. October 18 proof, current canonical
grades and fixed next-20 decisions remain unavailable here. Attempts to update
PR #1081's body with that reconciliation and add `needs-human` were rejected:
the tools require approval while this run's policy is `never`. Checkboxes remain
unchecked.

Issue #1072 Git-location follow-up on 2026-11-03 (base `26bfc15`): both
inventory Git reads now reuse the collector's `_local_git` environment isolation.
Inherited `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or `GIT_COMMON_DIR` could
otherwise redirect the reported revision or tracked status despite `git -C`.
Two real-Git regressions cover independent repositories and linked worktrees;
before the repair they produce four failed assertions and one error. Afterward,
all 24 diagnostic tests, seven native replay/publication CLI tests and 23 module
self-checks pass. `git diff --check` passes. The parent environment is preserved.

Production inputs remain absent, so no authentic inventory or dispatch occurred
and all operational checkboxes remain incomplete. Pytest and Black are missing;
installation into `/tmp` failed with no package versions available. Both required
Black commands failed with `command not found`, blocking commit and push. The
GitHub tools rejected both the `needs-human` label and blocker comment on PR
#1081 because they require approval while this run's policy is `never`. PR #1081
was verified open with `draft=false`. October 18 and next-20 evidence remain
unavailable.

Issue #1072 report-identity follow-up on 2026-11-03 (base `57bd9f6`): inventory
now pins both report locations and filesystem identities across its reads and
returns the originally observed resolved paths. Byte-identical atomic replacement
or a symlink switch otherwise passed content checks and could misattribute the
original cohort or canonical identity. Four new standard-library tests cover
replacement, symlink switching, disappearance and stable read-only observation
for both reports. Before the fix, the replacement and switching controls failed;
afterward all 22 diagnostic tests, seven native replay/publication CLI tests and
23 module self-checks pass. `git diff --check` also passes.

The configured runtime directory and canonical report are absent in this runner.
No authentic production inventory or dispatch was performed, and operational
checkboxes remain incomplete. Missing October 18 evidence and fixed next-20
observations remain unavailable. Targeted pytest cannot start because pytest is
missing. Both required Black commands cannot start because Black is missing;
installation into `/tmp` failed with no package versions available. No commit or
push was attempted because the mandatory Black gate could not pass. Adding
`needs-human` and posting the blocker on PR #1081 were rejected because those
tools require approval while this run's policy is `never`.

Issue #1072 journal-identity follow-up on 2026-11-03 (base `0e772b6`): reviewed
the recent inventory, publication-guard and diagnostic commits. None verifies an
operational checkbox: the configured canonical report and independent outcomes
database are still absent in this runner. Production inventory, retries, grading
and dated live-shadow observations remain unverified. No dispatch was attempted.

The inventory now pins the journal's resolved location and filesystem identity.
SQLite connections retain the old file after atomic replacement; comparing only
records on that connection could otherwise miss a replacement journal containing
new UNKNOWN attempts. Four new standard-library regressions failed before the fix
and pass afterward: identical-file replacement, replacement with a new unresolved
attempt, a symlink switch with identical receipts, and journal disappearance.
All 18 diagnostic tests and all 23 module self-checks pass. The inventory retains
its read-only behavior and grants no receipt, retry or grading authority.

The required Black formatting and repository-wide check commands cannot run:
Black is missing, as is pytest; installation attempts found no available packages.
No commit or push was made because the required Black gate cannot pass. Updating
PR #1081's body to record task reconciliation was rejected because the tool
requires approval while this run's approval policy is `never`; no checkbox was
changed. This local note records the reconciliation without claiming completion.
Attempts to add `needs-human` and post the blocker comment on PR #1081 were
rejected for the same approval-policy reason. PR #1081 remains open and ready
for review.

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

The code locations are reobserved before the final report/journal comparison.
A changed script hash, checkout revision, tracked clean/dirty status, or resolved
mirror generation aborts inventory, including disappearance of a selected location.
This detects changes to the recorded code observations; it does not establish
live-reader binding or inventory every dependency in a dirty checkout.

Issue #1072 code-stability follow-up on 2026-11-03: six new standard-library tests
reproduce the stale-pin bug before the fix, with six failing assertions across
five negative tests. After the fix, all 14 diagnostic tests and all 23 module
self-checks pass. Report and journal bytes remain unchanged. Targeted pytest and
both required Black commands cannot run because pytest and Black are absent;
installation into `/tmp` fails because PyPI DNS is unavailable. Git metadata is
also read-only. No commit or push was made. The configured canonical report and
outcomes database remain absent, so production inventory, retry/publication,
historical observations, and all operational task checkboxes remain incomplete.
Attempts to add `needs-human` to issue #1072 and post the blocker on PR #1081
were refused because those tools require approval and this run's policy is `never`.

The inventory also prints `row_diagnostics` in original cohort order. Each entry
names the first retained break: unavailable packet, missing substantive evidence,
recorded attempt error, current proposal-validator failure, inconsistent saved
decision, abstention, missing binary decision, or unavailable retained later truth.
`source_row` locates that evidence in the immutable original. Even a complete
saved proposal and truth label still require fresh native authentication.
These diagnoses describe retained inputs, not a fresh collection or outcome read.
Native blockers are separate: every unresolved reservation/invocation/raw receipt
has its attempt ID, state and UNKNOWN outcome, including foreign-source records
that block the same case identity. A terminal foreign-source attempt requires
review; a journal state called validated still requires receipt authentication.
No saved projection supplies a grade or authorizes retry spending.

Task reconciliation for the next issue #1072 iteration: commits `a7098f8` and
`72805f7` implement inventory and CLI/publication controls but explicitly leave
production execution unverified. Neither operational task can be checked off.
GitHub currently reports PR #1073 merged, so no merged PR body was changed.
The configured canonical report and independent outcomes database are absent in
this runner. The per-row operational task also remains open until diagnostics and
authenticated fresh checks run against the authentic original cohort.

Local diagnostic verification: eight standard-library unittest tests pass,
including real inventory CLI execution, unchanged original/canonical/journal
bytes, preserved recursive paid identities and decided rows, unresolved foreign
attempts, malformed proposals, and refusal to trust serialized projections.
The module selftest passes all 23 checks. Targeted pytest cannot start because
pytest is missing. Black is also missing, and installing the tools into `/tmp`
failed. The required Black formatting/check gate cannot pass, so these Python
changes have not been committed or pushed. No native dispatch or publication
was attempted, and no acceptance checkbox was changed.
Staging verification also fails because `.git/index.lock` is read-only. Attempts
to add `needs-human` and post the blocker comment on issue #1072 were rejected:
the tools require approval while this run's approval policy is `never`.

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
