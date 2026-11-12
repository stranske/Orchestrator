# Issue #1216 verification

## Bounded identity-artifact reads, 2026-11-12

Continued the first unchecked provenance task on `codex/issue-1216`, based on
`cb8c1f9036983bb347e9fd8ae11d810134d663a7`. The trusted provider-lookup gate and
explicitly unverified session measurements already exist on this branch. Found
that its 64 KiB artifact limit was enforced only after reading the whole file.
`_authoritative_identity_verified` now reads at most 65,537 bytes before refusing
oversized evidence, including evidence with a matching recomputed digest.

Extended the existing oversized case of
`test_malformed_identity_artifact_refuses_before_lookup_or_writes` to first
accept valid evidence padded to exactly 65,536 bytes, then enforce a cumulative
read budget for the oversized file. It still requires refusal before any
provider lookup, no trial state or capability-program summary, and zero rows
in all seven trial feedback tables. No test cases were added or removed, so
the collection floor is unchanged.

Reinstating the original unbounded read produced the intended failure:

```text
python3 -m pytest -q \
  tests/test_profile_trial_ingest.py::test_malformed_identity_artifact_refuses_before_lookup_or_writes \
  -k oversized -m 'not slow' --tb=short
Unbounded read: AssertionError: identity evidence must not use an unbounded read
1 failed, 6 deselected; exit 1.
Restored bounded read: 1 passed, 6 deselected; exit 0.
```

The source was restored in `finally` and verified byte for byte, SHA-256
`eeeaf69a3d194d2b88a6e6b5f5f4f0e11a08916242a64ecc6dbecc34d24acd07`.
Transcripts: `/tmp/orch-1216-bounded-read-before.txt` and
`/tmp/orch-1216-bounded-read-restored.txt`.

Using Python 3.14.10 and upstream pytest 9.2.0, the five requested test files
passed with **198 passed and 30 subtests passed**, exit 0, with `-m 'not slow'`.
The profile-trial selftest also exited 0. `/opt/anaconda3/bin/python3` is absent;
the same test arguments were run with the available interpreter. Official
tagged test/format tooling was assembled under `/tmp` after PyPI DNS failed;
repository dependencies were unchanged.

All six PR #1193 review threads were reread; the existing
[source/test dispositions](#review-dispositions-for-pr-1193) remain supported.
For the committed predecessor `cb8c1f9036983bb347e9fd8ae11d810134d663a7`,
[CI run 43142315903](https://github.com/stranske/Orchestrator/actions/runs/43142315903)
and [Gate run 43142316543](https://github.com/stranske/Orchestrator/actions/runs/43142316543)
both concluded `success`. These results do not validate this round's local
changes. No new live trial or production provider provenance is claimed.

Black 26.10.1 formatted both changed Python files with line length 100. This
sandbox prevents Black's process workers from starting, so serial checks of
850 tracked Python files populated its normal cache. The required unmodified
CLI check then passed:

```text
PATH=/tmp/orch-1216-bin:$PATH \
  PYTHONPATH=/tmp/orch-1216-tools:/usr/local/lib/python3.14/dist-packages:/opt/pipx/venvs/yamllint/lib/python3.14/site-packages:/usr/lib/python3/dist-packages \
  XDG_CACHE_HOME=/tmp/orch-1216-cache BLACK_NUM_WORKERS=1 \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
709 files would be left unchanged; exit 0.
```

After the boundary-control addition and mutation restoration, the same five-file
suite passed again: 198 passed and 30 subtests passed, exit 0. `git diff --check`
also passed.

- [x] Verify bounded artifact validation and the existing trusted-provenance
  boundary while preserving explicitly unverified measurements.
- [ ] Commit the source, regression test and evidence on the existing branch.
- [ ] Obtain CI for a delivered head containing this round's changes.

The commit attempt stopped at `git add`:

```text
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

No commit was created. Updating the first duplicate unchecked provenance task,
adding `needs-human`, and posting the blocker comment were each rejected by
automatic approval review: `MCP tool call requires approval, but approval policy
is never`. None of those remote mutations occurred. The prepared single-task
checkbox reconciliation is `/tmp/orch-1352-provenance-checkbox.md`. Delivery
requires an environment permitting writes to this branch's `.git`; final-head
CI and the remote checklist remain outstanding.

## Malformed identity artifact verification, 2026-11-12

Reviewed `4dc6ced4`, `f133e3c8`, and `78474cdf` and their implementation,
regression tests, mutation transcripts and dated incomplete live-run evidence
before continuing the first unchecked provenance task. The unchecked Full
Issue Text checklist duplicates the implemented tasks. Reconciliation is
prepared at `/tmp/orch-1352-reconciled-body.md`, preserving unchecked combined
review/final-head CI tasks. The body update was rejected by automatic approval
review: `MCP tool call requires approval, but approval policy is never`.
No remote checkbox changed.

- [x] Verify malformed local evidence cannot reach trusted provider lookup or
  persist verified trial data, even with a recomputed digest.
- [x] Run the five requested test files and the profile-trial selftest.
- [ ] Commit the test changes on the existing branch and obtain final-head CI.

`test_malformed_identity_artifact_refuses_before_lookup_or_writes` adds seven
cases: absent file, directory, invalid JSON, invalid UTF-8, JSON null, JSON list,
and oversized valid JSON with matching identity fields. Each first verifies a
passing authenticated-lookup fixture, damages the first validated artifact,
recomputes its digest when applicable, and then requires refusal before lookup.
Every case asserts zero rows in all seven trial feedback tables and no trial
state or capability-program summary. The fixture is offline evidence; it does
not establish production provider provenance.

Python 3.14.10 and upstream pytest 9.2.0 were used; the literal requested
`/opt/anaconda3/bin/python3` is absent. Pinned official tooling sources were
assembled under `/tmp` because package installation could not resolve PyPI.
Repository dependency files were unchanged.

```text
PYTHONPATH=/tmp/orch-1216-tools:/usr/lib/python3/dist-packages \
  ORCH_STATE_DIR=/tmp/orch-1216-test-state PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python3 -m pytest -q tests/test_profile_trial_ingest.py \
  tests/test_profile_trial_run.py tests/test_profile_trial_recurring_spec.py \
  tests/test_model_profile_trial_bridge.py tests/test_profile_trial_instances.py \
  -m 'not slow'
Baseline: 189 passed, 30 subtests passed; exit 0.
After the additions and after Black: 196 passed, 30 subtests passed; exit 0.
Profile-trial selftest: exit 0.
```

The seven-case increase changes collection/pass expectations from 8741/8739
to 8748/8746, preserving every ceiling. This is an inventory-based delta,
not an observed full-suite passing claim.

Black 26.10.1 formatted the changed test with line length 100. After the
sandbox's worker pool stalled, serial checks populated Black's normal cache.
The required unmodified CLI check then exited 0:

```text
PATH="/tmp/orch-1216-bin:$PATH" \
  PYTHONPATH=/tmp/orch-1216-tools:/opt/hostedtoolcache/Python/3.14.10/x64/lib/python3.14/site-packages:/opt/hostedtoolcache/Python/3.14.10/x64/lib/python3.14/site-packages/pip/_vendor:/usr/lib/python3/dist-packages \
  XDG_CACHE_HOME=/tmp/orch-1216-cache BLACK_NUM_WORKERS=1 \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
701 files would be left unchanged; exit 0.
```

`git diff --check` passed. PR #1352 was rechecked open and ready (`draft=false`)
at `4dc6ced4dfcb69b8bc84169d4f71168525219f24` after the delivery attempts.

All six current PR #1193 review threads were reread. The existing
[source/test dispositions](#review-dispositions-for-pr-1193) remain supported.
For committed head `4dc6ced4dfcb69b8bc84169d4f71168525219f24`,
[Gate run 43126042843](https://github.com/stranske/Orchestrator/actions/runs/43126042843)
and [CI run 43126042496](https://github.com/stranske/Orchestrator/actions/runs/43126042496)
concluded `success`. Both sets of six checkout/mirror shards, CI aggregates,
and Gate lint-format, lint-ruff and typecheck-mypy passed at that exact SHA.
These results cover the predecessor of these local test changes. No new live
trial or final-head CI success is claimed; the recorded live trial remains
explicitly incomplete for trusted provider identity.

Delivery was attempted with:

```text
git add tests/test_profile_trial_ingest.py .verify-floor.json \
  docs/verification/issue-1216-provenance.md
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

No commit was created. Requests to add `needs-human` and post the blocker
comment were also rejected by automatic approval review with
`MCP tool call requires approval, but approval policy is never`. Neither
remote mutation occurred. The verified working-tree changes require an
environment permitting writes to this branch's `.git`; remote checklist
reconciliation and final delivered-head CI remain outstanding.

## Provider usage type verification, 2026-11-12

Reviewed commits `f133e3c8`, `78474cdf`, `78f4dc9e` and their existing source,
tests and evidence on `codex/issue-1216` before continuing. The unchecked
Full Issue Text tasks duplicate the checked automated summary. The implemented
provenance boundary, rubric, forged-ID cases, bridge usage cases, six-attempt
persistence tests and recorded mutations support checking those duplicates.
The dated live trial below remains explicitly incomplete for provider identity;
this round does not claim a new live execution or trusted production collector.

The prepared reconciliation is `/tmp/orch-1352-reconciled-body.md`. Updating
PR #1352's body was rejected by automatic approval review with
`MCP tool call requires approval, but approval policy is never`; the remote
checkboxes were not changed. Combined review/final-head CI tasks remain unchecked
in the prepared body because predecessor checks cannot validate new local tests.

The first unchecked implementation task already has the trusted-lookup gate.
This follow-up adds 15 cases to
`tests/test_profile_trial_ingest.py::test_provider_usage_must_be_measured_integer_counts_before_any_writes`.
An authenticated response must also have measured integer usage. The cases cover
missing, null, list, string and empty usage; either missing count; and either
count supplied as an equal-valued float, boolean, negative integer or string.
Each starts with a passing authenticated-lookup control, corrupts the final
validated attempt, confirms every response ID was looked up, and asserts all
seven feedback tables empty with no trial state or capability-program summary.
These are offline lookup fixtures, not evidence of live provider identity.

Validation uses Python 3.14.10 and upstream pytest 9.2.0. The literal requested
`/opt/anaconda3/bin/python3` is absent. PyPI DNS failed, so the official tagged
tool sources were assembled under `/tmp`, using the installed system/vendored
dependencies; repository dependencies were unchanged.

```text
PYTHONPATH=/tmp/orch-1216-tools:/usr/lib/python3/dist-packages \
  ORCH_STATE_DIR=/tmp/orch-1216-test-state PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python3 -m pytest -q tests/test_profile_trial_ingest.py \
  tests/test_profile_trial_run.py tests/test_profile_trial_recurring_spec.py \
  tests/test_model_profile_trial_bridge.py tests/test_profile_trial_instances.py \
  -m 'not slow'
Baseline: 174 passed, 30 subtests passed; exit 0.
With the added cases: 189 passed, 30 subtests passed; exit 0.

PYTHONPATH=src ORCH_STATE_DIR=/tmp/orch-1216-selftest-state \
  python3 src/model_profile_trial.py --selftest
model_profile_trial.py selftest: OK; exit 0.
```

A deliberate mutation removed only `type(value) is not int or` from
`_authoritative_identity_verified`'s usage check. With the same test environment:

```text
python3 -m pytest -q \
  tests/test_profile_trial_ingest.py::test_provider_usage_must_be_measured_integer_counts_before_any_writes \
  -k equal-float -m 'not slow' --tb=short
FAILED ...[equal-float-input] - Failed: DID NOT RAISE ValueError
FAILED ...[equal-float-output] - Failed: DID NOT RAISE ValueError
2 failed, 13 deselected; exit 1.
Restored source: 2 passed, 13 deselected; exit 0.
```

The source was restored in `finally` and matched SHA-256
`1cdc0149de2198ebde78afa2c66852bead27c9b690f5e953cafcb90f79a3007c`.
Full local mutation transcripts are
`/tmp/orch-1216-usage-mutation-failing.txt` and
`/tmp/orch-1216-usage-mutation-restored.txt`. The 15-case delta adjusts collection
and pass expectations from 8726/8724 to 8741/8739; every ceiling is preserved.
Those are inventory-based expectations, not a full-suite passing claim.

All six current PR #1193 threads were reread on 2026-11-12. The existing
[source/test dispositions](#review-dispositions-for-pr-1193) remain supported;
the new cases strengthen the independent-response usage check. For remote head
`f133e3c8593c7109afbadf2606bd8a568aea7f20`,
[Gate run 43124012822](https://github.com/stranske/Orchestrator/actions/runs/43124012822)
and [CI run 43124012448](https://github.com/stranske/Orchestrator/actions/runs/43124012448)
both concluded `success`. All six checkout shards, six flat-mirror shards,
both CI aggregates, and Gate's lint-format, lint-ruff and typecheck-mypy passed.
These checks cover the predecessor head only; CI for this round is pending.

Black 26.10.1 formatted the changed test with line length 100. The worker pool
stalled in this sandbox. Single-file CLI checks passed over all 842 tracked
Python files; after serially filling any remaining normal-cache entries, the
required repository-wide check passed without modifying Black:

```text
PATH="/tmp/orch-1216-venv/bin:$PATH" \
  PYTHONPATH=/tmp/orch-1216-tools:/tmp/orch-1216-venv/lib/python3.14/site-packages/pip/_vendor \
  BLACK_NUM_WORKERS=1 XDG_CACHE_HOME=/tmp/orch-1216-cache \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
701 files would be left unchanged; exit 0.
```

The complete five-file suite also passed again after restoring the mutation:
189 passed and 30 subtests passed; exit 0. `git diff --check` passes.

Delivery attempt:

```text
git add tests/test_profile_trial_ingest.py .verify-floor.json \
  docs/verification/issue-1216-provenance.md
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

No commit was created. The `needs-human` label and blocker-comment requests
were each rejected by automatic approval review with
`MCP tool call requires approval, but approval policy is never`; neither
remote mutation occurred. PR #1352 was rechecked open and ready (`draft=false`),
still at `f133e3c8593c7109afbadf2606bd8a568aea7f20`. Commit, remote checkbox
reconciliation and CI for a delivered head containing this round remain open.

## Provider lookup batch verification, 2026-11-11

Based on PR #1352 head `78474cdf015f76178d22bb1243da171616f8901d`, on the
existing `codex/issue-1216` branch. The first unchecked task duplicates the
implemented independent-provenance boundary. This round expands its regression
evidence in `tests/test_profile_trial_ingest.py` without changing that boundary.

- [x] Verify independently trusted provider provenance is required for every
  attempt, while the existing session tests preserve explicitly unverified
  measurements.
- [x] Verify first, middle, and last attempt refusal before any feedback or
  published state writes, including provider lookup exceptions.
- [x] Repeat both requested failing mutations and restored passing tests;
  additionally prove the expanded tests catch checking only the first attempt.
- [ ] Commit this round's test changes on the checked-out PR branch.
- [ ] Record required CI for a delivered head containing these changes.

`test_provider_response_must_independently_confirm_the_trial_measurement`
now contains 33 cases, replacing six. Each starts with a successful fake
authenticated lookup as a control, then changes one response at one attempt
position. Cases cover object type, ID, model, completion status, each of run ID,
profile ID and packet hash, each input/output usage count, a missing response,
and a lookup exception. They assert the exact lookup prefix through the bad
attempt, all seven feedback tables empty, and no state or capability-program
summary file. The fixture is offline evidence, not live provider provenance.

The targeted five-file baseline passed with 147 tests and 30 subtests. The same
command passed after the expansion and again after restoring every mutation:

```text
PYTHONPATH=/tmp/orch-1216-tools:/tmp/orch-1216-venv/lib/python3.14/site-packages/pip/_vendor \
  ORCH_STATE_DIR=/tmp/orch-1216-state /tmp/orch-1216-venv/bin/python -m pytest -q \
  tests/test_profile_trial_ingest.py tests/test_profile_trial_run.py \
  tests/test_profile_trial_recurring_spec.py tests/test_model_profile_trial_bridge.py \
  tests/test_profile_trial_instances.py -m 'not slow'
174 passed, 30 subtests passed; exit 0.

PYTHONPATH=src ORCH_STATE_DIR=/tmp/orch-1216-selftest-state \
  python3 src/model_profile_trial.py --selftest
model_profile_trial.py selftest: OK; exit 0.
```

Python is 3.14.10 and pytest is 9.2.0. `/opt/anaconda3/bin/python3` is absent;
these are equivalent test arguments with the available interpreter, not a claim
that the literal Anaconda command ran. PyPI installation could not find the
pinned packages, so official tagged pytest/Black sources and their dependencies
were assembled under `/tmp`; repository dependencies were unchanged.

Each deliberate mutation changed only `src/model_profile_trial.py` and was
restored in a `finally` block before the corresponding passing run. The command
for each test below was the interpreter/environment above with
`-m pytest -q <test> -m 'not slow' --tb=short`.

| Test in `tests/test_profile_trial_ingest.py` | Deliberate source mutation | Intended failure | Restored run |
| --- | --- | --- | --- |
| `test_ingest_is_one_transaction_and_a_mid_write_failure_leaves_nothing[_record_run_in_conn-2]` | In `finalize_trial`, commit `attempts[:1]` through a separate `feedback.ingest_profile_trial(..., commit_ingest_marker=False)` before the normal transaction | Exit 1; `AssertionError: runs`, `assert 1 == 0`; 1 failed | Exit 0; 1 passed |
| `test_local_identity_claims_require_an_independent_provider_lookup` | Return `True` instead of `False` when `_authoritative_identity_verified` has no trusted lookup | Exit 1; `Failed: DID NOT RAISE ValueError`; 1 failed | Exit 0; 1 passed |
| `test_provider_response_must_independently_confirm_the_trial_measurement` | Iterate over `attempts[:1]` instead of every attempt in `_authoritative_identity_verified` | Exit 1; all 22 `middle`/`last` cases fail with `DID NOT RAISE ValueError` or `DID NOT RAISE RuntimeError`; 11 first-attempt cases pass | Exit 0; all 33 passed |

All restorations matched SHA-256
`1cdc0149de2198ebde78afa2c66852bead27c9b690f5e953cafcb90f79a3007c`.
The 27-case increase adjusts only collection/pass expectations, from
8699/8697 to 8726/8724; every ceiling is preserved. This is a targeted-suite
delta, not an observed full-suite result.

All six current PR #1193 threads were reread; the existing
[review dispositions](#review-dispositions-for-pr-1193) remain supported by the
source and this passing suite. The provenance disposition now also has explicit
failure-before-write coverage for each attempt position and lookup exceptions.

For the base head `78474cdf015f76178d22bb1243da171616f8901d`,
[Gate run 43122276375](https://github.com/stranske/Orchestrator/actions/runs/43122276375)
and [CI run 43122277157](https://github.com/stranske/Orchestrator/actions/runs/43122277157)
both concluded `success`. The checkout shards, flat-mirror shards, both CI
aggregates, and Gate's lint-format/lint-ruff/typecheck-mypy checks passed.
These results cover the base head only. No new live trial is claimed; the
existing dated six-attempt record below remains explicitly incomplete for
provider provenance.

Black 26.10.1 formatted the changed test at line length 100. Its worker pool
stalled in this sandbox, so the unmodified single-file CLI was invoked over all
842 tracked Python files to populate its normal cache; every invocation passed.
The required repository-wide command then exited 0:

```text
PATH="/tmp/orch-1216-venv/bin:$PATH" \
  PYTHONPATH=/tmp/orch-1216-tools:/tmp/orch-1216-venv/lib/python3.14/site-packages/pip/_vendor \
  BLACK_NUM_WORKERS=1 XDG_CACHE_HOME=/tmp/orch-1216-cache \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
701 files would be left unchanged; exit 0.

git add tests/test_profile_trial_ingest.py .verify-floor.json \
  docs/verification/issue-1216-provenance.md
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

`git diff --check` passes. No commit was created. Adding `needs-human` and
posting the blocker comment to PR #1352 were each rejected by automatic approval
review: `MCP tool call requires approval, but approval policy is never`.
Those remote mutations did not occur. The PR was verified open and ready
(`draft=false`). Delivery and final-head CI remain incomplete until these
working-tree changes can be committed to the existing branch.

## Local keepalive round 4, 2026-11-11

GitHub returned six current, unresolved threads on the closed PR #1193,
with no additional thread or comment pages. The [dispositions below](#review-dispositions-for-pr-1193)
cover all six. The three production-code findings were rechecked against
`_authoritative_identity_verified`, `_recurring_work_rubric`, and
`_work_quality_score`. The remaining threads concern the bridge, response-ID,
and session-only persistence regression tests. The five-file A1 command
using `/opt/anaconda3/bin/python3` exited 0 with 147 passed and 30 subtests
passed both before and after integrating main.

The [CI run](https://github.com/stranske/Orchestrator/actions/runs/43117087142)
and [Gate run](https://github.com/stranske/Orchestrator/actions/runs/43117087920)
both succeeded for predecessor head `78f4dc9eef7105867f722fc6523f5a164ec4373b`.
The PR conflicted with main `495fd9dc` only in `.verify-floor.json`.
Integration carries the prior head's verified 63-case delta over the new
main floor, yielding 8699 collected and 8697 passed as CI expectations.
Read the [checks on PR #1352](https://github.com/stranske/Orchestrator/pull/1352/checks)
for the commit containing this section. T6 and A4 require results bound to
that delivered head.

## Bridge usage and persisted-attempt follow-up, 2026-11-11

Reviewed commits `4a1d0da0`, `b87a8cb0`, `044bb908`, `01f80adf`, and
`428ecd40` on the existing `codex/issue-1216` branch before editing. The
unchecked full-issue tasks repeat the checked automated summary. The original
five-file baseline passes with **132 tests and 30 subtests**. Existing source,
tests, mutation transcripts and the dated incomplete live trial below support
reconciling the implementation tasks; final-head CI remains open for this
round's changes. A replacement PR body was prepared at
`/tmp/orch-1352-reconciled-body.md`, but its remote update was rejected with
`MCP tool call requires approval, but approval policy is never`.

- [x] Revalidate the independently trusted lookup requirement, unverified
  session measurements, frozen-spec rubric and response-ID refusal cases.
- [x] Extend bridge usage validation to both input and output counts, including
  matching nulls, bidirectional null/integer and null/zero mismatches, matching
  zero, and omitted artifact fields preserving positive, zero and unknown usage.
- [x] Verify that the six returned session-only attempt IDs equal the six
  persisted IDs and that every persisted run retains its profile and input/output
  counts. The zero verified-ingest-marker assertion remains exercised.
- [x] Reread all six current PR #1193 review threads; the dispositions recorded
  below still match current source and passing tests. The null-usage disposition
  now also covers both output counts and explicit zero.
- [ ] Commit this round's test changes: `.git` is mounted read-only.
- [ ] Reconcile remote PR checkboxes: automatic approval review rejected the update.
- [ ] Record required hosted CI results for a delivered head containing this round.

Validation uses Python 3.14.10, pytest 9.2.0 and Black 26.10.1. The requested
`/opt/anaconda3/bin/python3` is absent. PyPI DNS resolution failed, so official
upstream tagged sources were assembled under `/tmp/orch-1216-tools`, alongside
vendored dependencies from the temporary virtual environment's pip installation.
No repository dependencies were changed. Tool versions match the previous round.

```text
PYTHONPATH=/tmp/orch-1216-tools:/tmp/orch-1216-venv/lib/python3.14/site-packages/pip/_vendor \
  ORCH_STATE_DIR=/tmp/orch-1216-test-state /tmp/orch-1216-venv/bin/python -m pytest -q \
  tests/test_profile_trial_ingest.py tests/test_profile_trial_run.py \
  tests/test_profile_trial_recurring_spec.py tests/test_model_profile_trial_bridge.py \
  tests/test_profile_trial_instances.py -m 'not slow'
147 passed, 30 subtests passed; exit 0.

PYTHONPATH=src ORCH_STATE_DIR=/tmp/orch-1216-selftest-state \
  /tmp/orch-1216-venv/bin/python src/model_profile_trial.py --selftest
model_profile_trial.py selftest: OK; exit 0.
```

Black formatted both edited Python files with line length 100. The sandbox
blocked multiprocessing and stalled its thread-worker path, so the unmodified
Black single-file CLI entrypoint was invoked for each tracked Python file to
populate its normal cache. All 841 invocations exited successfully. The required
repository-wide command then passed:

```text
PATH="/tmp/orch-1216-venv/bin:$PATH" \
  PYTHONPATH=/tmp/orch-1216-tools:/tmp/orch-1216-venv/lib/python3.14/site-packages/pip/_vendor \
  BLACK_NUM_WORKERS=1 XDG_CACHE_HOME=/tmp/orch-1216-cache \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
700 files would be left unchanged; exit 0.
```

`git diff --check` also passes. PR #1352 was rechecked open and ready
(`draft=false`) with head `428ecd40b465aeb04ee20c53c8a1098d821db53e`.

The targeted suite grows by fifteen cases. Collection/pass expectations increase
from 8634/8632 to 8649/8647 with every ceiling unchanged. These are inventory-based
expectations, not a full-suite passing claim.

The bridge cases are `test_identity_artifact_tokens_must_agree_with_attempt`,
`test_identity_artifact_explicit_null_usage`, and
`test_omitted_identity_artifact_usage_preserves_attempt` in
`tests/test_model_profile_trial_bridge.py`. The persisted-row readback is in
`tests/test_profile_trial_recurring_spec.py::test_publish_current_records_measured_usage_quality_and_identity`.

Existing-head CI was inspected on `428ecd40b465aeb04ee20c53c8a1098d821db53e`:
[CI run 43113066885](https://github.com/stranske/Orchestrator/actions/runs/43113066885)
and [Gate run 43113066943](https://github.com/stranske/Orchestrator/actions/runs/43113066943)
both concluded `success`. All six checkout pytest shards, all six flat-mirror
shards and both CI aggregates passed. Gate's shard-1 lint-format, lint-ruff and
typecheck-mypy passed. These hosted checks cover the base head only.

Delivery attempt:

```text
git add tests/test_model_profile_trial_bridge.py \
  tests/test_profile_trial_recurring_spec.py .verify-floor.json
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

The `needs-human` label and blocker-comment requests were also rejected by
automatic approval review with the same approval-policy error. No branch or PR
was created. Prior dated
mutation and live-run evidence remains historical evidence; no new live trial
or final-head CI success is claimed here.

## Response-ID rejection follow-up, 2026-11-11

Reviewed recent commits `4a1d0da0`, `e19da646`, `b87a8cb0`, `044bb908`, and
`01f80adf` before continuing. The unchecked tasks in the PR's full issue text
duplicate implementation already present in its automated summary. The local
five-file baseline passed with **121 tests and 30 subtests**. Reconciliation:

- [x] Independent provider lookup required; session measurements remain
  explicitly unverified (ingest/run tests).
- [x] Bounded frozen-spec rubric and headingless, multiline, empty-answer,
  and criterion-echo cases (run tests).
- [x] Forged provenance and response IDs with recomputed artifact digests
  refused before trial feedback writes (ingest/run tests).
- [x] Matching null usage, both mismatch directions, omitted artifact fields,
  and six distinct session-only attempts without ingest markers (bridge and
  recurring-spec tests).
- [x] Early-commit and identity-gate mutations have named failing/restored
  runs recorded below; the restored tests pass in this round's suite.
- [x] Prior live six-attempt `run-current` record below explicitly reports
  incomplete provider provenance. No new live trial was attempted here.
- [ ] Review dispositions and required CI for the final delivered head:
  all six current PR #1193 threads were reread and match the dispositions
  below, but hosted checks do not cover this round's local changes.
- [ ] Commit this round's test changes on the checked-out PR branch.
- [ ] Reconcile the remote PR body: the update request was refused with
  `MCP tool call requires approval, but approval policy is never`.

Expanded `test_invalid_or_unconfirmed_response_ids_refuse_even_with_a_trusted_lookup`
from seven to eighteen cases. Missing IDs and explicit JSON null are separate
cases. Boolean/container IDs, Unicode, newline/space, hyphen/underscore, and
another attempt's valid response IDs are also refused after the artifact digest
is recomputed. Malformed IDs never reach the authenticated lookup; earlier valid
attempts may be checked first. Every refusal checks all seven trial feedback
tables, the requested state file, and the capability-program summary for absence.

The collection and pass expectations increase by eleven, from 8623/8621 to
8634/8632. All ceilings remain unchanged; this is an expected delta, not a
repository-wide passing result.

Validation uses Python 3.14.10, pytest 9.2.0, and Black 26.10.1. The requested
`/opt/anaconda3/bin/python3` is absent here. PyPI DNS resolution failed; pinned
official upstream tool sources were assembled under `/tmp/orch-1216-tools`,
without changing repository dependencies.

```text
PYTHONPATH=/tmp/orch-1216-tools ORCH_STATE_DIR=/tmp/orch-1216-test-state \
  /tmp/orch-1216-venv/bin/python -m pytest -q \
  tests/test_profile_trial_ingest.py tests/test_profile_trial_run.py \
  tests/test_profile_trial_recurring_spec.py tests/test_model_profile_trial_bridge.py \
  tests/test_profile_trial_instances.py -m 'not slow'
132 passed, 30 subtests passed; exit 0.

PYTHONPATH=src ORCH_STATE_DIR=/tmp/orch-1216-selftest-state \
  /tmp/orch-1216-venv/bin/python src/model_profile_trial.py --selftest
model_profile_trial.py selftest: OK; exit 0.
```

Black 26.10.1 formatted the changed test at line length 100. After normal
single-file CLI checks populated its cache to avoid the sandbox's stalled
worker pool, the required full command passed:

```text
PATH="/tmp/orch-1216-venv/bin:$PATH" PYTHONPATH=/tmp/orch-1216-tools \
  BLACK_NUM_WORKERS=1 XDG_CACHE_HOME=/tmp/orch-1216-cache \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
700 files would be left unchanged; exit 0.
```

Additional deliberate mutation: remove only the response-ID `re.fullmatch`
condition in `_authoritative_identity_verified`, then run:

```text
PYTHONPATH=/tmp/orch-1216-tools ORCH_STATE_DIR=/tmp/orch-1216-test-state \
  /tmp/orch-1216-venv/bin/python -m pytest -q tests/test_profile_trial_ingest.py \
  -k invalid_or_unconfirmed_response_ids -m 'not slow' --tb=short
Mutated: 9 failed, 9 passed, 35 deselected; exit 1.
Restored: 18 passed, 35 deselected; exit 0.
```

The nine intended failures identify the empty string, `resp_`, `request_123`,
`resp_bad/id`, `resp_ñ`, a trailing newline, embedded space, hyphen, and
underscore. They fail because malformed IDs reach the lookup only when the
syntax guard is removed. Source restoration was byte-for-byte, SHA-256
`1cdc0149de2198ebde78afa2c66852bead27c9b690f5e953cafcb90f79a3007c`.
The passing full targeted suite above ran after restoration.

Existing-head CI evidence was read for
`01f80adf3ffff5bd559b973b90d0ff9ceaec918b`:
[CI run 43109900747](https://github.com/stranske/Orchestrator/actions/runs/43109900747)
and [Gate run 43109901655](https://github.com/stranske/Orchestrator/actions/runs/43109901655)
both concluded `success`. All six checkout and six exec-mirror pytest shards,
both aggregate jobs, and Gate lint-format/lint-ruff/typecheck-mypy succeeded.
These checks establish the existing head's status, not CI for these local edits.

Delivery attempt:

```text
git add tests/test_profile_trial_ingest.py .verify-floor.json \
  docs/verification/issue-1216-provenance.md
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

The PR-body update, `needs-human` label, and blocker-comment requests were each
refused with `MCP tool call requires approval, but approval policy is never`.
No remote mutation occurred. PR #1352 was rechecked open with `draft=false`
and head `01f80adf3ffff5bd559b973b90d0ff9ceaec918b`. Completing delivery requires
a run that can write the existing branch's Git metadata and PR metadata;
no new branch or PR was created.

## Local keepalive rubric follow-up, 2026-11-11

Based on checked-out PR #1352 head
`cdfa64d45e354799c25290d05ddddde2e23d186a`, branch `codex/issue-1216`.
The provenance boundary and requested headingless, multiline, empty-answer,
and criterion-echo cases were already implemented. The first unchecked
provenance task was revalidated by the five-file baseline below; this round
advances the suggested rubric task with a reproduced scoring correction.

- [x] Verify the existing independent-provenance ingestion boundary and
  explicitly unverified session measurements in the targeted suite.
- [x] Correct and verify the bounded frozen-specification rubric's heading
  boundary, including the previously requested answer cases.
- [ ] Commit the source and tests to the checked-out PR branch. `git add`
  fails because `.git` is mounted read-only.
- [ ] Obtain required CI results for a delivered commit containing this fix.

`_recurring_work_rubric` previously stopped at every `#` prefix. Ordinary
criteria such as `#123 remains open.` and `####### plain text` therefore
caused valid answers to score zero. The scorer now stops at one to six
hashes followed by whitespace or end of line, and `recurring_trial_prompt`
states that same rule explicitly. Eight added parameter cases also verify
six-hash and empty headings, blank termination, skipped leading blanks,
indented case-insensitive labels, tab-separated words, and the two-selection
limit. Existing headingless, multiline, empty-answer, criterion-echo, and
duplicate-evidence cases remain exercised.

Validation used Python 3.14.10 with pytest 9.2.0 and Black 26.10.1. The requested
`/opt/anaconda3/bin/python3` is absent in this environment. Direct PyPI access
failed DNS resolution, so pinned upstream tool sources were assembled under
`/tmp/orch-1216-tools`; repository dependency files were unchanged.

```text
PYTHONPATH=/tmp/orch-1216-tools ORCH_STATE_DIR=/tmp/orch-1216-baseline-state \
  /tmp/orch-1216-venv/bin/python -m pytest -q \
  tests/test_profile_trial_ingest.py tests/test_profile_trial_run.py \
  tests/test_profile_trial_recurring_spec.py tests/test_model_profile_trial_bridge.py \
  tests/test_profile_trial_instances.py -m 'not slow'
Before changes: 113 passed, 30 subtests passed; exit 0.

PYTHONPATH=/tmp/orch-1216-tools ORCH_STATE_DIR=/tmp/orch-1216-test-state \
  /tmp/orch-1216-venv/bin/python -m pytest -q tests/test_profile_trial_run.py \
  -k work_quality_checks -m 'not slow' --tb=short
New cases with original implementation: 3 failed, 15 passed, 15 deselected; exit 1.
The failures were the two #123 criteria cases and the seven-hash criteria case
in test_work_quality_checks_the_frozen_spec_task (expected 1.0, actual 0.0).

Repeat the five-file command with ORCH_STATE_DIR=/tmp/orch-1216-test-state:
After the correction: 121 passed, 30 subtests passed; exit 0.

PYTHONPATH=src ORCH_STATE_DIR=/tmp/orch-1216-selftest-state \
  /tmp/orch-1216-venv/bin/python src/model_profile_trial.py --selftest
model_profile_trial.py selftest: OK; exit 0.

PYTHONPATH=/tmp/orch-1216-tools BLACK_NUM_WORKERS=1 \
  XDG_CACHE_HOME=/tmp/orch-1216-cache /tmp/orch-1216-venv/bin/python -m black \
  --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
700 files would be left unchanged; exit 0.
```

Black first checked the changed Python files with line length 100. Its worker
path stalled under sandbox restrictions, so each tracked Python file was
checked separately using the unmodified pinned CLI before the full invocation
above passed against its populated formatter cache.

The targeted suite grew by exactly eight cases. `.verify-floor.json` increases
collection and pass expectations by eight and preserves all ceilings.
Repository-wide collection stopped with three import errors (missing `yaml`
and `pydantic`); no full-suite result is claimed.

`git diff --check` passes. PR #1352 was verified open and ready (`draft=false`)
at the base head above. The `needs-human` label request and blocker-comment
request were each rejected with `MCP tool call requires approval, but approval
policy is never`; neither remote mutation occurred. This round does not claim
a new live trial or final-head CI success.

## Local keepalive round 2, 2026-11-11

The named acceptance command using `/opt/anaconda3/bin/python3` exited 0
with **113 passed and 30 subtests passed**. The two changed test files add
13 cases to the previous collection and pass expectations. The full
suite runs in CI.

### Deliberate break and restoration

Both commands used the requested interpreter and `-m pytest -q --tb=short`.
After each run, `src/model_profile_trial.py` was restored byte for byte.
Restored SHA-256: `4a91202bfdff96916ce43557e236195bcb9157a9ab88ea6369203170c5f0fb12`.

| Test in `tests/test_profile_trial_ingest.py` | Mutation in `src/model_profile_trial.py` | Deliberate failure | Restored run |
| --- | --- | --- | --- |
| `test_ingest_is_one_transaction_and_a_mid_write_failure_leaves_nothing[_record_run_in_conn-2]` | Commit the first attempt with a separate feedback call before the normal trial transaction | Exit 1, `AssertionError: runs`, `assert 1 == 0` | Exit 0, 1 passed |
| `test_local_identity_claims_require_an_independent_provider_lookup` | Make `_authoritative_identity_verified` succeed when the independent lookup is absent | Exit 1, `Failed: DID NOT RAISE ValueError` | Exit 0, 1 passed |

### Review dispositions for PR #1193

Round 3 re-read the complete review-thread listing on 2026-11-11. All six
threads were unresolved and current on the closed predecessor PR. The
dispositions below describe their status on successor PR #1352.

| Thread | Disposition on this branch |
| --- | --- |
| [Independent response provenance](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714910) | `_authoritative_identity_verified` requires a lookup supplied by trusted application code and compares the returned response ID, model, completion state, trial metadata and token usage. `test_local_identity_claims_require_an_independent_provider_lookup` rejects the forged local claim before feedback writes. The CLI fails closed because it has no trusted collector. |
| [Headingless specifications](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714928) | `_recurring_work_rubric` uses the first two nonblank lines when there is no criteria block. `test_work_quality_checks_the_frozen_spec_task` gives `2:2,3:3` a score of `1.0` for a headingless spec with a leading blank, and gives literal criterion echoes `0.0`. |
| [Empty or repeated evidence](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714945) | `_work_quality_score` requires a nonempty rubric and exactly one complete evidence line. `test_work_quality_checks_the_frozen_spec_task` gives an empty heading and empty answer `0.0`, rejects duplicate evidence, and covers multiline and inline criteria. |
| [Matching null usage](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714963) | `test_identity_artifact_explicit_null_usage` proves matching nulls stay unmeasured and mismatches fail in both directions. `test_identity_artifact_tokens_must_agree_with_attempt` verifies that omitted artifact fields preserve attempt usage. |
| [Independent response ID rejection](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714994) | `test_invalid_or_unconfirmed_response_ids_refuse_even_with_a_trusted_lookup` recomputes the artifact digest after removing or changing the ID and verifies all trial feedback tables remain empty. |
| [Session-only feedback](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493715009) | `test_publish_current_records_measured_usage_quality_and_identity` checks six distinct returned attempt IDs, six distinct persisted `execution_attempts`, and zero `profile_trial_ingests` rows with explicitly unverified session evidence. |

Round 3 repeated the acceptance command with `/opt/anaconda3/bin/python3`.
It exited 0 with **113 passed and 30 subtests passed**. For hosted CI, use the
[PR #1352 checks](https://github.com/stranske/Orchestrator/pull/1352/checks)
for the commit containing this record. Match the check run's head SHA to the
PR head before accepting its results.

### Live run-current record

From 20:35:43 to 20:36:55 UTC, one invocation of
`/opt/anaconda3/bin/python3 src/model_profile_trial.py run-current` used the
PR worktree as the Orchestrator source and the local `stranske__Workflows`
mirror at `86e74e7842183048c2fe1b9a5ad5fd304fbce70d`. Both source manifests
matched readback. The shared Codex pool was supplied as `warn` from the
20:29:58 UTC native capacity poll, which reported 87% weekly usage and
ordinary usage allowed. The runner was Codex CLI 0.167.0.

The deployed receipt for `strategy434-20261006` matched the archived receipt
byte for byte. The archived specification also appeared byte for byte in
three retained evaluation prompts. The original spec path was absent, so
the receipt and archived inputs were copied to an isolated directory. Only
the copied `strategy.json` path was made relative to its copied `spec.md`.
Outputs targeted that directory's `state/profile-trial-state.json`,
`state/capability-program/profile-trial.json`, quarantine database, and
`attempts/` directory.

Trial `model-profile-trial:b538078409df3bff6c8b478b` bound all six final
messages to the packet and frozen specification. Each final matched the
corresponding native session's completed turn, from which input and output
tokens were read. The rubric expected `1:21,3:8`.

| Ordinal | Requested profile | Instance | Input tokens | Output tokens | Rubric score |
| --- | --- | --- | --- | --- | --- |
| 1 | `codex-5.6-terra-high` | 3 | 14380 | 315 | 1.0 |
| 2 | `codex-6-astra-high` | 3 | 19584 | 260 | 1.0 |
| 3 | `codex-5.6-terra-high` | 1 | 18775 | 415 | 1.0 |
| 4 | `codex-5.6-terra-high` | 2 | 14489 | 402 | 1.0 |
| 5 | `codex-6-astra-high` | 1 | 19584 | 267 | 1.0 |
| 6 | `codex-6-astra-high` | 2 | 15298 | 278 | 1.0 |

**Provider identity and publication are incomplete.** The command exited 2
with `provider_resolved_identity_unavailable` after the six completions.
The CLI produced no identity artifacts, quarantine database, trial state
or new program summary. The existing deployed summary identified a
different three-instance trial. The per-attempt usage and quality above
come from the six saved sessions and final messages. They are grouped by
requested profile and carry no independently verified provider model.
The private evidence directory
`~/.codex/orchestrator/measurement/orch-1352-20261111/` holds the originals,
their hashes in `readback.json`, and the reconstructed manifest validated
against all six run IDs.

## Historical round 1 record, 2026-11-11

This record covers the source issue's narrowed provenance slice on
`codex/issue-1216`, based on `3987c448`. These changes are **uncommitted** because
this run mounts `.git` read-only. This is local implementation evidence, not a
claim that the full issue or final-head CI is complete.

- [x] Require independent provider provenance for verified ingestion while
  preserving explicitly unverified session measurements. Verified locally by
  the named regression tests below.
- [ ] Deliver the changes in a commit on the checked-out PR branch.
- [ ] Complete the remaining rubric, bridge, mutation-transcript, review,
  production-trial, and final-head CI tasks from the original issue.

## Implemented boundary

[`model_profile_trial.py`](../../src/model_profile_trial.py) accepts a
`provider_response_lookup` from trusted application code, separately from the
result JSON. The callback must authenticate provider provenance independently
of session transcripts and their local artifacts. Verification binds a unique,
valid response ID to a completed provider response, the model, trial run,
profile, frozen packet, and measured usage. Missing lookup or confirmation
refuses verified ingestion before any feedback writes. Authority names and
matching local digests alone confer no trust.

`results_from_codex_sessions` labels its measurements and identity artifacts
unverified. Publishing those measurements retains usage and quality in six
distinct attempt rows without a verified-ingest marker. The CLI has no trusted
provider collector; production identity verification remains **incomplete**.
No live provider execution or six-attempt production record is claimed.

## Validation

Pinned pytest 9.2.0 on Python 3.14.10:

```text
python3 -m pytest -q tests/test_profile_trial_ingest.py tests/test_profile_trial_run.py tests/test_profile_trial_recurring_spec.py tests/test_model_profile_trial_bridge.py tests/test_profile_trial_instances.py -m "not slow"
100 passed, 30 subtests passed; exit 0
```

`test_forged_session_provenance_cannot_enable_verified_ingest` exercises both
`response.completed` and `server_model` claims with forged verification flags
and recomputed artifact digests. A verified-ingest request leaves all seven
trial feedback tables empty. Explicit unverified recording then retains six
distinct attempts, measured usage, and quality, with zero ingest markers.

`test_local_identity_claims_require_an_independent_provider_lookup`,
`test_invalid_or_unconfirmed_response_ids_refuse_even_with_a_trusted_lookup`,
and `test_provider_response_must_independently_confirm_the_trial_measurement`
cover missing/invalid/forged IDs and independent response mismatches. Existing
successful-ingest tests now supply a separate fake authenticated lookup; they
are offline fixtures, not production provenance evidence.

Additional checks:

- `node --test tests/test_profile_trial_cli.js`: exit 0 on Python 3.14.10.
- `python3 src/model_profile_trial.py --selftest`: exit 0.
- Related summary/model-profile tests: 25 passed on Python 3.12.16 with an
  isolated writable `ORCH_STATE_DIR`; this was a supplemental compatibility run.
- Pinned Black 26.10.1 formatted the changed Python files and passed
  `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`:
  699 files unchanged, exit 0. Sandbox worker restrictions required checking
  files individually first; the final unmodified CLI invocation passed using
  that formatter cache. The earlier Black 25.1.0 result is not acceptance evidence.
- Pinned pytest collection: 8,611 cases. Baseline copies of the five trial files
  collect 84 cases; the updated files contain 100. `.verify-floor.json` therefore
  adds 16 to the collection and expected-pass counts, preserving every ceiling.
  A full-suite passing run is not claimed.
- `git diff --check`: exit 0.

The requested `/opt/anaconda3/bin/python3` does not exist here. Test tools were
assembled under `/tmp` from pinned upstream sources through the GitHub connector
because direct package installation could not resolve PyPI. Repository
dependency files were not changed. Black source tag: `26.10.1`; pytest source tag:
`9.2.0`.

## Delivery blockers

```text
git add src/model_profile_trial.py ...
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

PR #1352 is open and ready (`draft=false`). Adding `needs-human` and posting a
blocker comment were each refused by the GitHub tool with:
`MCP tool call requires approval, but approval policy is never`.
Neither remote mutation occurred. A run permitting writes to the existing
branch's `.git` is required to commit these reviewed working-tree changes;
the PR label/comment also require an environment permitting those mutations.
