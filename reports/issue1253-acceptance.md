# Issue #1253 acceptance evidence

Checked 2026-11-09 UTC. This repair records evidence for merged PR [#1297](https://github.com/stranske/Orchestrator/pull/1297); it changes no tests, production code, pins, floors, workflows, or coverage configuration. Refs #1253.

## Acceptance and bound recorded before mutations

Outcome: every test method added by #1297 fails under a named narrow mutation, passes immediately after restoring the original source bytes, and the complete focused module passes after all restorations. Command:

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue1253_ranked_coverage.py
```

No checkout-selected virtual environment was present; the owner-selected fallback above ran Python 3.12. The CI runtime is Python 3.14. Stop rule recorded before mutation: at most two mutation choices per node, 110 seconds per child command (under two minutes), 75 minutes total; stop on an unrestored change or a genuine test defect. No stop condition occurred. All 16 first-choice mutations were detected. No local full-suite or verify/release run was executed.

## Revision and prior-work recovery

- GitHub readback: #1297 is merged, not draft, merged at `2026-11-09T13:18:34Z`; merge `2aa3d69b2e5542416436f9ce847f9b449b412f02`, PR head `0ba90811d6a30455a2620f321ecaca54e5225170`.
- GitHub PR base is `30752f569b2cc9763dfcba227d4a8a273be869f8`; actual merge parent is `94b64ed8c9e2a8de1f3e05e55487ebf214063d3f`. Both are measured below to separate intervening main changes.
- Provisioned `closer-r1/issue-1253` started clean at current main `7cff442f`, which contains #1297. `git branch --list 'closer*/issue-1253'` found only this branch; `git log origin/main..closer-r1/issue-1253 --oneline` was empty. No missing local branch commits needed merging. Kept all of #1297; discarded no earlier implementation.
- Inspected #1297's complete discussion and repository evidence paths before rerunning proofs. No usable executed per-node mutation transcript was recovered. `git diff 2aa3d69b^ 2aa3d69b -- tests/test_issue1253_ranked_coverage.py` identifies exactly 16 new test methods; the other 30 methods already existed in main via #1255. This report proves those 16 additions without redoing the earlier implementation.

## Focused and repository CI pytest

Executed focused output before mutation:

```text
46 passed, 1 warning, 84 subtests passed in 0.77s
```

Executed focused output after every mutation was restored:

```text
46 passed, 1 warning, 84 subtests passed in 0.75s
```

The warning is the installed anyio pytest plugin's deprecated `CallSpec2` import; pytest-asyncio also prints an unset-loop-scope deprecation. Neither is a test failure and neither was changed here.

Successful repository CI evidence is [CI run 42621011568](https://github.com/stranske/Orchestrator/actions/runs/42621011568), on #1297's final PR head: all six pytest shards, all six flat-mirror shards, and both aggregate jobs succeeded. The aggregate [job 129173911319](https://github.com/stranske/Orchestrator/actions/runs/42621011568/job/129173911319) prints:

```text
pytest:     8352 passed, 0 failed, 27/31 max (8381 collected; floor 8365 — 16 BEHIND)
```

The reported post-merge failing job is [129185063545, `verify.py aggregate + combined coverage`](https://github.com/stranske/Orchestrator/actions/runs/42624339689/job/129185063545), in cancelled run `42624339689`. All six pytest shards succeeded and it prints the same zero-failure pytest counts, but fails the main-only collection-floor equality:

```text
floor is BEHIND reality: 8381 collected > floor 8365 — the floor is 16 test(s) permissive
```

That is distinct from cancelled job `129184116667`, `reconcile the collection floor (main only)`. Cancellation is not successful CI evidence. PR CI explicitly allows a lagging floor; main's reconcile job owns the drain. Blocking/drainable pair at that revision: 16 floor entries behind / 16 entries drainable by the existing main-only reconcile job while the verification gate is closed. Subsequent main floor commits are present; this repair does not edit the floor or bypass a review floor.

Recovered commands from `.github/workflows/ci.yml` (documented here, **not run locally**):

```sh
# Six remote shards, N=1..6; ORCH_STATE_DIR/ORCH_LOCAL_RUNTIME are runner temp dirs.
python3 src/verify.py --pytest-shard N/6 --coverage --shard-output shard-results
# PR aggregate (main omits --floor-may-lag):
python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag
```

`verify.py` invokes `python -m pytest -q -rfEs -p no:cacheprovider --no-header`, with `-n 4`, shard file arguments and JUnit evidence. It combines worker coverage with all module selftests, then publishes `gate-coverage/coverage.json`. The reusable Gate's coverage toggle is off; its shard artifacts contain indexes, not coverage JSON. Thus the repository-owned combined `gate-coverage` artifact is the authoritative available measurement, not a plain focused `pytest --cov` estimate.

## Executed break and immediate revert

Each row ran exactly this node command once with its listed production mutation, restored source bytes in `finally`, then ran the identical node command again:

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue1253_ranked_coverage.py::RankedCoverageTests::TEST_METHOD
```

All red exits are 1; all restored exits are 0. `failed` counts can be subtest counts: pytest prints `SUBFAILED(...)` with the exact node rather than `FAILED` for those cases. A parent `1 passed` alongside failed subtests is not a passing run. The CLI mutation hit the test's deny mock, never actual discovery. Source-byte hashes matched after restoration, and `git diff --exit-code -- src tests` succeeded after all 32 isolated commands. The temporary recorder needed to recognize `SUBFAILED` as well as `FAILED`; it resumed from saved results without rerunning completed mutations. Raw local command/output logs were retained under `/private/tmp/issue1253-evidence/` during execution; the durable evidence follows.

| Test method (exact node suffix) | Deliberate production mutation | Named detection / red output | Immediate restored output |
| --- | --- | --- | --- |
| `test_miner_requires_named_verification_for_positive_evidence` | `src/pattern_miner.py`: `elif not verification_name:` → `elif False and not verification_name:` | `FAILED`; `1 failed, 1 warning in 0.68s` | `1 passed, 1 warning, 2 subtests passed in 0.63s` |
| `test_miner_rejected_artifact_cannot_become_positive_evidence` | `src/pattern_miner.py`: `artifact_accepted = artifact.status in {` → `artifact_accepted = artifact.status in {"rejected",` | `FAILED`; `1 failed, 1 warning in 0.68s` | `1 passed, 1 warning in 0.70s` |
| `test_miner_adjudicated_failure_overrides_passing_verifier` | `src/pattern_miner.py`: `verification_payload.get("adjudicated_verdict") or verification_payload.get("verifier_verdict")` → `verification_payload.get("verifier_verdict")` | `SUBFAILED(adjudicated_verdict='fail')`; `1 failed, 1 passed, 1 warning, 1 subtests passed in 0.63s` | `1 passed, 1 warning, 2 subtests passed in 0.62s` |
| `test_miner_health_counts_exclusions_and_rejections_only_in_current_snapshot` | `src/pattern_miner.py`: `self.exclusions.append(Rejection(exc.event_id, "out_of_scope", tuple(exc.reasons)))` → `self.rejections.append(Rejection(exc.event_id, "out_of_scope", tuple(exc.reasons)))` | `SUBFAILED(health='rejecting')`; `2 failed, 1 passed, 1 warning, 2 subtests passed in 0.68s` | `1 passed, 1 warning, 4 subtests passed in 0.59s` |
| `test_miner_assembly_names_each_missing_phase_without_normalizing` | `src/pattern_miner.py`: `f"missing_phase:{phase}" for phase in PHASES` → `f"absent_phase:{phase}" for phase in PHASES` | `SUBFAILED(phase='trigger')`; `7 failed, 1 passed, 1 warning in 0.78s` | `1 passed, 1 warning, 7 subtests passed in 0.60s` |
| `test_miner_assembly_checks_timestamp_order_at_every_phase_boundary` | `src/pattern_miner.py`: `if by_phase[left].occurred_at > by_phase[right].occurred_at:` → `if by_phase[left].occurred_at >= by_phase[right].occurred_at:` | `SUBFAILED(left='trigger', right='decision', delta=0)`; `6 failed, 1 passed, 1 warning, 12 subtests passed in 0.73s` | `1 passed, 1 warning, 18 subtests passed in 0.61s` |
| `test_miner_empty_snapshot_keeps_loaded_candidate_and_next_action` | `src/pattern_miner.py`: `existing_candidates = list(self.candidates) if preserve_existing else []` → `existing_candidates = []` | `FAILED`; `1 failed, 1 warning in 0.67s` | `1 passed, 1 warning in 0.62s` |
| `test_miner_loaded_snapshot_adds_and_refreshes_candidates_without_losing_retirements` | `src/pattern_miner.py`: `existing_tombstones = list(self.tombstones) if preserve_existing else []` → `existing_tombstones = []` | `SUBFAILED(offset=0)`; `2 failed, 1 passed, 1 warning in 0.70s` | `1 passed, 1 warning, 2 subtests passed in 0.62s` |
| `test_miner_persisted_retirement_requires_new_evidence_to_revive` | `src/pattern_miner.py`: `or candidate_evidence_at > previous_evidence_at` → `or candidate_evidence_at >= previous_evidence_at` | `FAILED`; `1 failed, 1 warning in 0.68s` | `1 passed, 1 warning in 0.61s` |
| `test_miner_snapshot_preserves_distinct_tombstone_identities` | `src/pattern_miner.py`: `item.capability_id, item.successor, ): item` → `item.capability_id, ): item` | `FAILED`; `1 failed, 1 warning in 0.64s` | `1 passed, 1 warning in 0.61s` |
| `test_miner_serialization_failure_removes_partial_write_and_preserves_state` | `src/pattern_miner.py`: `os.unlink(temp_name)` → `pass # deliberate mutation: leave partial write` | `FAILED`; `1 failed, 1 warning in 0.68s` | `1 passed, 1 warning in 0.60s` |
| `test_backlog_strict_issue_discovery_refuses_truncated_ownership` | `src/backlog.py`: `if len(rows) >= 1000:` → `if len(rows) > 1000:` | `SUBFAILED(count=1000)`; `1 failed, 1 passed, 1 warning, 3 subtests passed in 0.67s` | `1 passed, 1 warning, 4 subtests passed in 0.63s` |
| `test_backlog_strict_pr_discovery_refuses_truncated_ownership` | `src/backlog.py`: `if len(rows) >= 200:` → `if len(rows) > 200:` | `SUBFAILED(count=200)`; `1 failed, 1 passed, 1 warning, 3 subtests passed in 0.68s` | `1 passed, 1 warning, 4 subtests passed in 0.61s` |
| `test_backlog_research_retains_healthy_repo_when_pr_ownership_is_truncated` | `src/backlog.py`: `if len(rows) >= 200:` → `if len(rows) > 200:` | `FAILED`; `1 failed, 1 warning in 0.66s` | `1 passed, 1 warning in 0.62s` |
| `test_backlog_selftest_cli_does_not_enter_live_discovery` | `src/backlog.py`: `if "--selftest" in argv:` → `if "--selftest" in argv and "--live" not in argv:` | `FAILED`; `1 failed, 1 warning, 1 error in 0.72s` | `1 passed, 1 warning in 0.58s` |
| `test_paths_flat_selftest_validates_checkout_root` | `src/paths.py`: `assert REPO_ROOT == MODULE_DIR, (REPO_ROOT, MODULE_DIR)` → `pass # deliberate mutation: omit flat-root validation` | `FAILED`; `1 failed, 1 warning in 0.65s` | `1 passed, 1 warning in 0.59s` |

Executed 2026-11-09 13:55:06–13:57:00 UTC; each isolated node took under one second of pytest time. The 110-second child limit was never approached.

## Ranked selection and measured partial delivery

The repository ranking recorded in [issue #1253](https://github.com/stranske/Orchestrator/issues/1253) is escaped-defect signals, then churn, then uncovered mass. The issue's older snapshot (`4cd9bbe4`, 2026-11-08T09:15:20Z) ranked:

| Rank | Target | Escaped signals | Churn | Old claimed uncovered units |
| --- | --- | ---: | ---: | ---: |
| 1 | pattern_miner | 3 | 8 | 28 |
| 2 | backlog | 3 | 7 | 38 |
| 3 | capability_ir | none stated | 3 | 12 |
| 4 | redirect_policy | none stated | 3 | 7 |
| 5 | coverage_testgen_trigger | none stated | 2 | 13 |
| 6 | paths | none stated | 1 | 7 |

These sum to 105, already 75 short of 180 even before intervening work. #1297 concentrated its additional discrimination on the first two escaped-defect targets plus the remaining flat-layout statement in paths. This preserves that delivered scope; it does not retroactively claim that the smaller third-through-fifth targets were newly covered by #1297.

Downloaded the same `gate-coverage/coverage.json` producer at all three revisions, using `gh run download RUN -R stranske/Orchestrator -n gate-coverage -D TEMP_DIR`. Each is coverage.py 7.17.2, format 3, instrumented pytest plus selftests, `branch_coverage: false`. Both base and merged `pyproject.toml` set `branch = false`.

| Measurement | Revision | CI run | Artifact |
| --- | --- | --- | --- |
| PR base | 30752f56 | [42584465463](https://github.com/stranske/Orchestrator/actions/runs/42584465463) | 13658013681 |
| Actual merge parent | 94b64ed8 | [42617503167](https://github.com/stranske/Orchestrator/actions/runs/42617503167) | 13674095076 |
| Merged revision | 2aa3d69b | [42624339689](https://github.com/stranske/Orchestrator/actions/runs/42624339689) | 13677742243 |

Cancelled overall runs still uploaded their completed measurement; they are used for coverage only, never as passing CI verdicts. Target source files are unchanged across these revisions. Read `files['src/NAME.py'].summary.missing_lines` and `num_statements` from each payload:

| Target | Statements | Base missing statements | Parent missing statements | Merged missing statements | Statement reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| pattern_miner | 498 | 0 | 0 | 0 | 0 |
| backlog | 398 | 0 | 0 | 0 | 0 |
| capability_ir | 138 | 0 | 0 | 0 | 0 |
| redirect_policy | 96 | 0 | 0 | 0 | 0 |
| coverage_testgen_trigger | 147 | 0 | 0 | 0 | 0 |
| paths | 59 | 1 (line 139) | 1 (line 139) | 0 | 1 |
| **Total** | **1336** | **1** | **1** | **0** | **1** |

The observed reduction is **1 statement, 179 short of 180**. Missing branches and statement-plus-branch reduction are **UNKNOWN**, because these artifacts contain no branch measurement; absence of branch fields is not zero missing branches. No branch settings or thresholds were changed to create a number. This is the permitted partial-delivery explanation, not a claim that #1297 achieved 180 combined units.

- **capability_ir:** no missing statements remained at either base. The pre-existing 30-test module already exercises candidate validation and persisted schema/version rejection. #1297 adds no direct IR test methods; there is no measured statement gain left to claim. Untested branch mass remains unknown.
- **redirect_policy:** no missing statements remained at either base. Pre-existing tests cover bare/wrapped JSON input, heartbeat failure and first-drift inspection. #1297 adds no direct redirect-policy methods. Adding duplicate statement coverage would not improve the available metric; branch mass remains unknown.
- **coverage_testgen_trigger:** no missing statements remained at either base. Pre-existing tests cover invalid coverage values, disappearing reports and ranked CLI output. #1297 adds no direct trigger methods. No new statement gain is attributable to it; branch mass remains unknown.

The base overall statement coverage was already 95.2187%, versus the issue's older 87.64% blended claim; the merged payload reports 95.2204% statements. These are different measures, not proof of a 90% blended goal. No padding tests or production changes were made. Additional branch instrumentation would be separate scope, not an acceptance-report repair.

Payload SHA-256 (extracted JSON, not ZIP):

```text
base: b08c0ee1993b20220657117944ed0ec7b541541109adc8425f344eb42ee69283
parent: 101cae10e07b7cb12951dabb9a29f7cb3b0479ea04bbcb745fd3339ec1234b8a
merged: 1ea3544f485f41f30691f7e22f04dba080831442304c8da6219aa934a1d516b2
```
