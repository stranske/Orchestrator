# stranske/Orchestrator#771 follow-up evidence

<!-- closer-core/v1 repair-of=stranske/Orchestrator#771@772 -->

**Refs #771; follow-up #1099.** Retrieval date: **2026-11-05**.

This record distinguishes recovered CI history from local verification performed for this follow-up. The earlier completed checklist was unsupported and has been replaced. No historical mutation execution or controlled coverage comparison is claimed.

### Reconciliation of the previous iteration

Commit `26cd4b777d9205b56592a8d13210ed983d1911a0` preserved the CI records and corrected the coverage accounting. Re-running the offline CI verifier and its tests confirmed the completed follow-up items below. [Reconciliation records](reconciliation.json) map those results to the duplicated task numbers in the supplied task list: **10 of 20 entries** have supporting evidence. This does not mean all acceptance criteria are satisfied.

The GitHub service reports PR #1143 as **merged and closed**, with `draft=false`. Its body was not updated: a merged PR cannot meet the required open/non-draft handoff invariant. The local checklist and reconciliation record preserve the verified progress; no PR was created or changed in this round.

## Follow-up tasks

- [x] Identify the two original-head workflow runs and pytest jobs and preserve their logs, metadata, retrieval commands, results, and dates: [CI manifest](ci/manifest.json), [failed shell retrievals](ci/gh-attempts.json), and the index below. The connected GitHub service recovered the logs after shell networking failed.
- [ ] Recover complete mutation failure/restoration pairs for all five original tests. [Source inspection](mutations/source-inspection.json) identifies the exact five tests. The [prepared mutation plan](mutations/plan.json) and [tested replay script](../../../scripts/replay_issue_771_mutations.js) advance reproduction, but runtime proof remains blocked.
- [ ] Recover or reproduce a controlled baseline/post-change coverage comparison. [Partial coverage records](coverage/records.json) preserve matching configuration and one post-change measurement, with the missing baseline and branch results explicitly unknown.
- [x] Remove invalid coverage arithmetic and the uncovered-majority claim; report only the [observed statement measurement and limitations](#coverage-measurement-and-comparability).
- [x] Replace unsupported completion claims with linked evidence and retain [unknown historical ranking provenance](#historical-ranking-provenance).

## Acceptance checklist

- [ ] Passing pytest execution on the literal original commit `18ad97a054dab7f6039dd42c8131bee04267748b`. The preserved CI logs prove passing execution for runs **associated with** that PR head, but their checked-out commit is the temporary PR merge `12c5c58517863f747972538c9bb17c568bc0bf49`.
- [ ] An isolated checkout with exactly the five original tests reports five passes, and every test has a complete named assertion failure, restored pass, and restoration check.
- [ ] Coverage records disclose exact selected test populations and permit a controlled comparison excluding later additions. Historical configuration and whole-job totals are retained, but a matching baseline and per-test population records are unavailable here.
- [x] Unsupported reduction and uncovered-majority claims have been replaced with the [preserved statement measurement](coverage/job-117832439523.log), recalculable arithmetic, and explicit limits below.
- [x] Historical ranking provenance [remains explicitly unknown](#historical-ranking-provenance); the [retrieved issue text](issue-771.json) records a ranking claim, not its underlying provenance or proof of the original selection contract.

## CI records and tested commits

Both workflow run APIs identify PR head **`18ad97a054dab7f6039dd42c8131bee04267748b`**, branch `orchestrator/issue-771`, event `pull_request`, and conclusion `success`. The final merge on main was `b1511de27d025a65b4424144a405fd2625ffca28`; it is distinct from the temporary CI merge.

| Workflow | Run / job | Metadata | Preserved log | Measured pytest result |
|---|---|---|---|---|
| CI (workflow ID `339745209`, `.github/workflows/ci.yml`) | `39180639406` / `117832439061`, `verify.py (real pytest counts + selftests + capability gates)` | [run](ci/run-39180639406.json), [jobs](ci/jobs-39180639406.json) | [complete job log](ci/job-117832439061.log) | `5443 passed, 0 failed, 27/31 max skipped (5470 collected; floor 5465 — 5 BEHIND)` |
| Gate (workflow ID `340345639`, `.github/workflows/pr-00-gate.yml`) | `39180639803` / `117832552676`, `python ci / python 3.12` | [run](ci/run-39180639803.json), [jobs](ci/jobs-39180639803.json) | [complete job log](ci/job-117832552676.log) | `5429 passed, 41 skipped, 4 warnings in 407.67s (0:06:47)` |

**Actual consumer checkout in both logs:** `12c5c58517863f747972538c9bb17c568bc0bf49`, logged as `Merge 18ad97a054dab7f6039dd42c8131bee04267748b into 4951a5a42b48464c4ace9f970c090f109bf12711`. This is the first `git log -1 --format=%H` result. Gate later checks out Workflows helper commit `e9b9424bbae233f44dca3a0b81faaee985372864` under `.workflows-lib`; that is not the consumer checkout.

**Commands and environments recorded in the logs:**

- CI invokes `python3 src/verify.py --floor-may-lag` for the pull-request event, using Python **3.14.8**, private `ORCH_STATE_DIR` / `ORCH_LOCAL_RUNTIME`, pytest plus module selftests and capability gates. The conditional shell block also includes the main-branch command without `--floor-may-lag`.
- Gate invokes `pytest -p ci_test_growth "${args[@]}"`, using Python **3.12.15**, pytest **9.1.1**, `PYTEST_MARKERS=not quarantine and not slow`, coverage enabled, `PYTEST_WORKERS=auto`, and xdist's `loadgroup` scheduler. The complete argument-construction block, including coverage reports, is preserved in the job log; this document does not substitute an inferred expanded command for that transcript.
- Both jobs and their pytest/Verify steps conclude `success`; Gate also logs `Checking pytest: success`. A numeric process exit status is not exposed in the retrieved metadata and is **unknown**, rather than an invented numeric exit record.

### Retrieval commands, results, and integrity

The four requested shell commands were executed on 2026-11-05:

```text
gh run view 39180639406 --repo stranske/Orchestrator --json headSha,conclusion,jobs
gh run view 39180639803 --repo stranske/Orchestrator --json headSha,conclusion,jobs
gh run view 39180639406 --repo stranske/Orchestrator --job 117832439061 --log
gh run view 39180639803 --repo stranske/Orchestrator --job 117832552676 --log
```

All exited **1** with `error connecting to api.github.com`. Exact commands, timestamps, exit statuses, and separate stdout/stderr files are indexed in [gh-attempts.json](ci/gh-attempts.json). These failures are not presented as successful log retrievals.

The connected GitHub service subsequently fetched each run's REST metadata, job metadata, and decoded job log successfully. [manifest.json](ci/manifest.json) records the exact tool names and arguments, successful retrieval results, date, and SHA-256 digests. Logs retain the decoded UTF-8 content, ANSI escapes and BOM, with LF and final newline. Successful connector results are separate from shell exit statuses.

The repository-specific [offline verifier](../../../scripts/verify_issue_771_ci.js) checks original run/job identities, original run head, successful pytest step, raw-log digests, measured pytest summaries, and the actual first checkout. It deliberately reports `exact_head_execution: false` for both records. Its [nine tests](../../../tests/test_issue_771_ci_evidence.js) reject missing pytest summaries, corrupt logs, wrong heads, unrelated jobs, failed pytest counts/steps, and a merge checkout relabeled as the original head.

```text
node tests/test_issue_771_ci_evidence.js
node scripts/verify_issue_771_ci.js
```

Local command output and exit statuses are retained in [local-validation.json](ci/local-validation.json). These are new evidence-verifier tests, not new executions of the five historical feedback tests.

## Original test scope and incomplete mutation evidence

The exact-head test source was recovered through `github.fetch_file` and is retained as [original-tests.py.txt](mutations/original-tests.py.txt). Its Git blob SHA is `e4f448e592e65b9625f393c783d6fae447e0b924`; the source hash, retrieval commit, and five AST-inspected test IDs are in [source-inspection.json](mutations/source-inspection.json).

| Original test ID | Complete mutation failure / restored pass / removal proof |
|---|---|
| `test_run_queries_keep_pending_and_mode_scopes_distinct` | Not retained; incomplete |
| `test_snapshot_json_exports_initialized_tables_to_the_requested_path` | Not retained; incomplete |
| `test_evidence_registry_normalizes_citations_and_retires_unused_rows` | Not retained; incomplete |
| `test_evidence_proposals_and_approval_require_recurrence_then_reactivate` | Not retained; incomplete |
| `test_role_selector_metrics_preserve_no_match_and_rejection_signals` | Not retained; incomplete |

The earlier abbreviated assertions and claimed restored passes were not complete execution records and are not counted as proofs. The earlier five-pass excerpt at final merge `b1511de` lacked a preserved command transcript and is likewise not counted as newly verified evidence.

Three later tests in the current module are outside the original population: `test_model_epoch_cost_telemetry_and_review_rounds_are_isolated`, `test_learning_population_and_cost_telemetry_reject_incomplete_or_implausible_data`, and `test_owner_questions_and_work_item_subject_preserve_scope_and_expiry`. They must be excluded from any isolated reproduction or attribution comparison.

Local blockers are recorded with separate stdout/stderr in [source-inspection.json](mutations/source-inspection.json): `git show 18ad97a:tests/test_feedback_registry_queries.py` cannot resolve the original head in the local object database (exit **128**), and the available Python interpreter cannot import pytest (exit **1**). Installing pytest/Black/coverage into a temporary virtual environment failed because PyPI DNS was unavailable. No deliberate mutation was applied, and no restored pass is claimed. A connected-service source retrieval is source inspection, not runtime verification.

### Prepared mutation replay (new follow-up work)

The connected GitHub service recovered `src/feedback.py` at the original commit, Git blob **`5cdb301107b1e8167b18578b0842917c14a5dd97`**. The complete five target functions, their starting lines, and source identity are preserved in [historical-functions.json](mutations/historical-functions.json). [plan.json](mutations/plan.json) records the exact retrieval arguments and five replacements: invert the mode predicate, overstate snapshot row counts, remove case-insensitive citation deduplication, reject recurrence at its threshold, and reinstate invoked double-counting. Each replacement matches exactly once within its historical function. These are newly prepared mutations, not recovered execution records.

The repository-specific [replay script](../../../scripts/replay_issue_771_mutations.js) requires a separate clean checkout at the literal original commit and checks both production/test Git blob identities before changing anything. It checks the five collected node IDs, requires an initial five-pass result, then saves each patch, named assertion failure, byte restoration hash, clean Git diff, and restored one-pass result. It restores the source in a `finally` block and requires a final five-pass result and clean checkout. Commands use argument arrays, include `-m "not slow"`, and retain separate stdout/stderr, timestamps, exit codes, signals, and errors. Output must be a new directory outside the historical checkout. Its manifest stays incomplete if any prerequisite or pair fails.

On a host with that checkout and pytest installed in the selected interpreter, the invocation is:

```text
node scripts/replay_issue_771_mutations.js /path/to/isolated-historical-checkout /path/to/selected-python /path/to/new-mutation-records
```

The [12 replay tests](../../../tests/test_issue_771_mutation_replay.js), plus the nine existing CI verifier tests, report **21 passed / zero failed** in the preserved [replay output](mutations/replay-validation/unit-tests/replay-tests.stdout.txt) and [CI verifier test output](mutations/replay-validation/unit-tests/ci-tests.stdout.txt), with [commands and exit codes](mutations/replay-validation/unit-tests/commands.json). Test doubles exercise restoration and rejection behavior; they are not executions of the original Python tests. The recorder also has a real subprocess test for separate stdout/stderr, exit status, argument boundaries, and missing executables.

[Validation commands and results](mutations/replay-validation/commands.json) retain fresh `git show` (exit **128**) and pytest availability (exit **1**) probes. A no-checkout temporary Git clone was used only to exercise the [rejected preflight](mutations/replay-validation/preflight/manifest.json): its actual head was `26cd4b7…`, so replay exited **1** before any source mutation or pytest execution. No historical checkout or original test run is claimed by that probe. All five runtime mutation rows above remain incomplete.

## Coverage measurement and comparability

The preserved [combined coverage job log](coverage/job-117832439523.log) is job **`117832439523`**, run **`39180639406`**, command:

```text
python3 src/verify.py --coverage --floor-may-lag 2>&1 | tee /tmp/combined.txt
```

The [CI job metadata](ci/jobs-39180639406.json) reports success. The log records Python **3.14.8**, **113 instrumented processes**, **5,470 collected / 5,443 passed / 27 skipped pytest tests**, and **100 of 107 selftests run / seven skipped**. The separate coverage/pytest dependency versions for this combined job are unknown: its quiet install does not retain a full dependency inventory. Gate's pytest version must not be substituted for this separate job's version. Exact per-test collection and selftest identities are not retained here.

The historical configuration bytes were inspected at original head `18ad97a054dab7f6039dd42c8131bee04267748b` and requested baseline `3b379f8b60046b1b6b21b360a931472d0b6db9c3`:

| Source | Retained complete content | Git blob SHA at both commits |
|---|---|---|
| `pyproject.toml` | [configuration](coverage/historical-pyproject.toml.txt) | `cdf71323ecb03f631f9bf2f8734146a10d95c3e0` |
| `.github/workflows/ci.yml` | [read-only historical snapshot](coverage/historical-ci.yml.txt) | `c95ad637961ab568f680da5908771376f52b11e5` |
| `src/verify.py` | [historical implementation](coverage/historical-verify.py.txt) | `a90705e480de5365ed5963f502a3cfb4f31b5571` |

[coverage/records.json](coverage/records.json) records source URLs, exact baseline `git show` commands and exits, hashes, and byte equality. The configuration declares `parallel = true`, `source = ["src"]`, **`branch = false`**, and the same omissions/exclusion rules at both commits. `verify.py --coverage` instruments pytest and selftest subprocesses and combines their data. There is no historical shard matrix in these snapshots. Identical configuration alone does not establish identical test populations or a comparable measurement.

The observed post-change report is statement-only:

| Metric for `src/feedback.py` | Observed value |
|---|---|
| Statements | 2,825 |
| Missing statements | 226 |
| Covered statements | 2,599 = 2,825 - 226 |
| Statement coverage | 92.0% as printed; 2,599 / 2,825 × 100 = 92.0% rounded to one decimal |
| Branch count / missing branches | **Unknown; branch coverage was not enabled in the inspected configuration** |
| Matching baseline missing statements | **Unknown** |
| Like-for-like statement delta | **Unknown** |
| Combined statement-and-branch delta | **Unknown** |
| Shortfall against approximately 180 combined units | **Unknown** |

The earlier comparison mixed the issue's **232 combined statement-and-branch units** with **226 missing statements**. Those quantities cannot establish a six-unit reduction, a single-digit improvement, or a numeric shortfall against 180 combined units. Most statements were covered in the recovered report; it does not support the claim that most of the module remained uncovered.

This is a recovered whole-job measurement on the temporary PR merge, not a controlled attribution experiment for the five tests. A valid comparison still needs fixed background tests and configuration, the five original tests explicitly included/excluded, later additions excluded, and the selector production fix accounted for separately. Without matching baseline reports and branch measurements, no causal delta is reported. No assumptions about unreachable ingestion, learning, delegation, or provider paths are used to explain the missing coverage.

The selector fix in PR #772 removed redundant counting of `invoked` from the payload after counting it from selector status. That changes production behavior as well as the tested lines; any future attribution comparison must disclose which version runs in each arm. Configuration agreement does not isolate that effect.

## Historical ranking provenance

The newly retrieved [issue #771 snapshot](issue-771.json) names `src/feedback.py`, states **232 uncovered statements+branches**, and claims ranking by escaped defects, churn, then uncovered mass. It also lists eight escaped-defect signals and churn 15. These are claims in the issue text; the underlying ranking input, contemporaneous ranking dump, autopilot execution record, and within-module function-selection rationale have not been recovered.

**Historical ranking provenance remains unknown. This follow-up does not prove the original target-selection contract.** The issue text and PR tests do not establish how the ranking was computed or why these five functions were selected. No post-hoc selection rationale is treated as historical evidence.
