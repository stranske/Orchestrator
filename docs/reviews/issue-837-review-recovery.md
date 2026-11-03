# Issue 837 review recovery

The durability gap suite is collected by ordinary pytest discovery. Its combined-CI
coverage must be measured and reported even when the rounded percentage is unchanged.
This recovery does not substitute focused pytest coverage for combined pytest+selftests.

## Reproducible focused before/after measurement

Measured with `/opt/anaconda3/bin/python3 -m coverage run --source=durability_sweep
-m pytest <selected suites> -q -o addopts=` and `coverage json`, on unchanged
`src/durability_sweep.py` from PR head eb2ded4ad1d56093c67881ed62136bb1b5299db3.
Before selects `test_durability_feedback_recovery.py`, `test_durability_fix_search_coverage.py`,
`test_durability_retry_horizon.py`, `test_durability_stated_repair.py`, and
`test_durability_sweep_exact_merge.py`; after adds `test_durability_sweep_coverage_gaps.py`.
No selftests or whole-repository coverage are included in these numbers.

| Target and measurement scope | Before | After |
| --- | ---: | ---: |
| durability_sweep, selected pytest suites | 720/1118 (64.4007%) | 917/1118 (82.0215%) |

The new suite adds 197 covered statements in this focused comparison. This is not a
claim that 197 previously missing combined-CI statements were covered. The prior PR
report records combined-CI missing counts separately; those values remain historical
until the current-head combined coverage artifact is available.

## Review corrections and evidence

- Pin the exact direct and inherited claim maps, and exercise the real inherited fallback.
- Read the actual restored outcome after undo rather than trusting the restored count.
- Pin unread recheck waiting/closed counts and the exact run/action detail.
- After transcript hash mismatch, read the stored completion event and prove both
  commented and delivery_complete remain false.
- Remove the overwritten hash update and source-line-number comment.
- Isolate the capability registry's captured default, router paths, handoff, and Brain DB;
  a lock-path tripwire rejects accidental access to the original capability registry.
- Assert argparse error code exactly equals 2 rather than matching the text.

Six semantic regressions were applied individually, failed assertions, then restored:
misattribute inherited claims; target the wrong undo row; omit unread waiting count;
persist delivery flags on hash mismatch; return exit 12; use import-bound live registry.
The implementation source was restored byte-identically after each source mutation.
Restored selected durability suites and the range gap suite pass. Formatter, linter,
and diff checks pass. Full current-head CI and its review floor remain separate gates.

## Acceptance completion (closer repair, 2026-11-03)

Revisions: #841 base `c5f6c1d979869751ee482e135c93b4d2ad8d32ad`, #841 head
`9ba1a6adf8997c49c57f47bede05c744d5a26d7f`, #841 merge `a538c8527d0e05f7cc78b30d599d61194d156078`,
repair base (main) `af6aec2`, repair commit with the strengthened assertions `d4ce9e2`.
Production source is unchanged; only the two test files named below change.

### Combined pytest+selftest coverage (CI `gate-coverage` artifacts)

Source: the `combined coverage (pytest + selftests, the honest number)` job's `coverage.json`
(`verify.py --coverage`), downloaded with `gh run download <run> -n gate-coverage`. #841's base
`c5f6c1d` is a floor-reconcile commit with no CI run; the nearest measured main push is `2dc8632`
(CI run 39485084375), whose four target modules are byte-identical to the base and whose total,
228, equals the issue's own figure. #841's PR-head run is 39481181495; its merge's main-push run
is 39487981121 and reads the same counts.

| Target | Missing statements before (2dc8632) | after (9ba1a6a PR head = a538c85 merge) | Delta |
| --- | ---: | ---: | ---: |
| `src/redirect_shadow.py` | 3 | 1 | −2 |
| `src/consumer_sync_artifact_ingest.py` | 16 | 15 | −1 |
| `src/range_lane_rollout.py` | 118 | 2 | −116 |
| `src/durability_sweep.py` | 91 | 3 | −88 |
| **Total** | **228** | **21** | **−207** |

Missing branches are **not measured by the repository's combined measure**: `pyproject.toml`
sets `[tool.coverage.run] branch = false`, and all three artifacts report
`meta.branch_coverage: false`. The issue's "statements+branches" figure of 228 is therefore
statements alone. A branch-inclusive measurement was taken locally (below) rather than changing
the repository's coverage configuration, which is out of this test-only scope.

**Branch-inclusive combined measure (local, this machine).** Command, run in detached
checkouts of `c5f6c1d` (base) and `9ba1a6a` (head) with `branch = true` set in each
scratch copy's `pyproject.toml` only, and with `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` set to
empty temp dirs:
`/opt/anaconda3/bin/python3 src/verify.py --coverage --floor-may-lag`, then `coverage.json`.
Both runs exit 1 on the same three tests, none of which touches a target:
`test_agent_gh_config.py` (2) and `test_agent_workspace_never_broad.py` (1). The `adapters` and
`ccusage_reconcile` selftests also fail on both. All of these are machine-environment failures,
and the target suites passed in both runs. Base: 7232 passed, 27 skipped (2187 s). Head: 7271
passed, 27 skipped (2859 s).

| Target | Stmts missing base → head | Branches missing base → head | Units base → head | Delta |
| --- | ---: | ---: | ---: | ---: |
| `redirect_shadow.py` | 3 → 1 | 7 → 5 | 10 → 6 | −4 |
| `consumer_sync_artifact_ingest.py` | 16 → 15 | 10 → 9 | 26 → 24 | −2 |
| `range_lane_rollout.py` | 56 → 1 | 31 → 4 | 87 → 5 | −82 |
| `durability_sweep.py` | 91 → 3 | 58 → 10 | 149 → 13 | −136 |
| **Total** | **166 → 20** | **106 → 28** | **272 → 48** | **−224** |

The local base shows `range_lane_rollout.py` at 56 missing statements where CI shows 118. This
machine has the version-capable Codex binary, so the module's `--selftest` runs here. On a CI
runner `env_prereq.codex_profile_binary_absent()` skips it. CI's number is the authority for
statements. The local run is the only branch measurement, and on this machine's shape the delta
is 224 units, also above 180.

The delta is 207 statements, above the issue's goal of about 180. The 21 statements still
missing after #841, and why each is not meaningfully coverable:

- `redirect_shadow.py:1383`, `consumer_sync_artifact_ingest.py` 963, 973, 985, 996, 1003, 1012,
  1018, 1059, 1068, 1084, 1096, 1295, 1346, 1370: `raise AssertionError("Should have …")`
  inside each module's own `--selftest`. They run only when the selftest finds a regression;
  covering them means breaking the module.
- `consumer_sync_artifact_ingest.py:1215`, `durability_sweep.py:2218, 2382`: the selftests'
  mock-`gh` "unexpected command" guards; reached only when a selftest issues a call its fixture
  does not expect.
- `durability_sweep.py:2707`: the selftest's `ORCH_STATE_DIR` restore for a run that started
  with the variable unset; CI's run always sets it.
- `range_lane_rollout.py:478, 480`: the selftest's other environment-restore pair (prior
  `HANDOFF_DIR` present, `ORCH_EXPLORATION_RATE` absent). Coverable, and #841 did not cover it;
  main has since covered it with
  `test_offline_selftest_restores_prior_handoff_and_clears_added_rate` (not part of #841).

### Mutation-to-test matrix (every test #841 added or rewrote)

Command, per mutation, in an isolated detached checkout (`git worktree add --detach`):
apply one exact-string mutation to the production module (the anchor must match exactly one
site), run `/opt/anaconda3/bin/python3 -m pytest -q -o addopts= -p no:cacheprovider <test
node>`, `git checkout -- <module>`, check the restored file's sha256 matches the original, and
rerun the node. The matrix ran twice: on the #841 merge `a538c85` and on the repair commit
`d4ce9e2` (current main + the strengthened assertions). The table is the `d4ce9e2` run, so the
line numbers are that tree's. 39 tests, 42 mutations. Every mutation failed its test, every
restore was byte-identical, and every restored rerun passed.

| ID | Test | Mutation (module: semantic regression) | First failing assertion | Mutated → restored |
| --- | --- | --- | --- | --- |
| C1 | `test_safe_extract_rejects_resolve_escape_via_dest_symlink` | `consumer_sync_artifact_ingest.py`: resolved-path escape check disabled | `test_consumer_sync_artifact_ingest_coverage_gaps.py:273` — Failed: DID NOT RAISE ValueError | fail → pass |
| S1 | `test_historical_candidates_skips_non_dict_rows` | `redirect_shadow.py`: non-dict row guard removed | `test_redirect_shadow_helpers_cli_coverage.py:156` — AttributeError: 'list' object has no attribute 'get' | fail → pass |
| S2 | `test_main_falls_through_when_cmd_is_unhandled` | `redirect_shadow.py`: unknown subcommand returns 1 | `test_redirect_shadow_helpers_cli_coverage.py:173` — AssertionError: assert 1 == 0 | fail → pass |
| S3 | `test_historical_candidates_counts_missing_target` | `redirect_shadow.py`: missing-target rows not counted | `test_redirect_shadow_helpers_cli_coverage.py:199` — assert 0 == 1 | fail → pass |
| R1 | `test_cached_backlog_distinguishes_valid_invalid_and_missing_payloads` | `range_lane_rollout.py`: non-dict cache reported as missing | `test_range_lane_rollout_coverage_gaps.py:51` — AssertionError: assert {'source': 'c..., 'items': []} == {'source': 'c..., 'items': []} | fail → pass |
| R2 | `test_filter_backlog_keeps_only_limited_eligible_opener_tasks` | `range_lane_rollout.py`: max_items cap off by one | `test_range_lane_rollout_coverage_gaps.py:73` — AssertionError: assert ['o/r#1', 'o/r#4', 'o/r#5'] == ['o/r#1', 'o/r#4'] | fail → pass |
| R3 | `test_sanitize_decision_rejects_each_unsafe_assignment_kind` | `range_lane_rollout.py`: backup/paygo agent allowed | `test_range_lane_rollout_coverage_gaps.py:98` — AssertionError: assert ['not opener ...aign targets'] == ['not opener ...aign targets'] | fail → pass |
| R4 | `test_build_rollout_reports_zero_cap_and_router_rejections_without_dispatch` | `range_lane_rollout.py`: zero cap not blocked | `test_range_lane_rollout_coverage_gaps.py:118` — AssertionError: assert [{'target': '..._states': {}}] == [{'reason': '...spatches': 0}] | fail → pass |
| R5 | `test_build_rollout_reports_router_zero_assignment_and_formats_campaign` | `range_lane_rollout.py`: zero-assignment diagnosis inverted | `test_range_lane_rollout_coverage_gaps.py:146` — AssertionError: assert [] == [{'reason': '... 'notes': []}] | fail → pass |
| R6 | `test_main_refuses_unguarded_active_request` | `range_lane_rollout.py`: unguarded --apply exits 0 | `test_range_lane_rollout_coverage_gaps.py:152` — AssertionError: assert 0 == 2 | fail → pass |
| R7 | `test_main_rejects_record_campaign_without_a_campaign_file` | `range_lane_rollout.py`: --record-campaign without --campaign exits 0 | `test_range_lane_rollout_coverage_gaps.py:159` — assert 0 == 2 | fail → pass |
| R8 | `test_offline_selftest_exercises_guarded_planner_without_a_dispatch` | `range_lane_rollout.py`: selftest leaks its exploration rate | `test_range_lane_rollout_coverage_gaps.py:187` — AssertionError: assert '0' == '0.25' | fail → pass |
| R9 | `test_live_loader_and_rejected_claim_release_are_isolated` | `range_lane_rollout.py`: live loader drops scoped blockers | `test_range_lane_rollout_coverage_gaps.py:218` — AssertionError: assert {'source': 'l...scoped': {}}]} == {'source': 'l...old': True}}]} | fail → pass |
| R10 | `test_main_apply_writes_only_the_stubbed_handoff_and_reports_dispatch` | `range_lane_rollout.py`: apply no longer tags range-lane-rollout capability | `test_range_lane_rollout_coverage_gaps.py:267` — AssertionError: assert ['prior'] == ['prior', 'ra...lane-rollout'] | fail → pass |
| R11 | `test_main_returns_read_only_error_when_planning_fails` | `range_lane_rollout.py`: RuntimeError escapes read-only error path | `test_range_lane_rollout_coverage_gaps.py:278` — RuntimeError: nope | fail → pass |
| R12 | `test_release_rejected_claims_swallows_release_errors` | `range_lane_rollout.py`: claim-release errors propagate | `test_range_lane_rollout_coverage_gaps.py:288` — RuntimeError: claim store down for o/r#1/codex | fail → pass |
| R13 | `test_build_rollout_rejects_campaign_combined_with_backlog_or_non_codemod` | `range_lane_rollout.py`: campaign + cached backlog accepted | `test_range_lane_rollout_coverage_gaps.py:297` — AssertionError: Regex pattern did not match. | fail → pass |
| R14 | `test_build_rollout_releases_rejected_claims_when_not_dry_run` | `range_lane_rollout.py`: rejected claims released only on dry runs | `test_range_lane_rollout_coverage_gaps.py:341` — AssertionError: assert [] == [('o/r#2', 'codex')] | fail → pass |
| R15 | `test_main_selftest_skips_when_codex_profile_binary_absent` | `range_lane_rollout.py`: skip gate falls through and runs the selftest | `test_range_lane_rollout_coverage_gaps.py:355` — AssertionError: assert ['ran'] == [] | fail → pass |
| R16 | `test_main_selftest_runs_when_prereq_present` | `range_lane_rollout.py`: --selftest never runs the selftest | `test_range_lane_rollout_coverage_gaps.py:367` — AssertionError: assert [] == ['ok'] | fail → pass |
| R17 | `test_build_rollout_campaign_attaches_prompts_and_capability` | `range_lane_rollout.py`: campaign prompt not attached | `test_range_lane_rollout_coverage_gaps.py:406` — AssertionError: assert '' == 'do the codemod' | fail → pass |
| R18 | `test_main_apply_blocked_and_preview_human_paths` | `range_lane_rollout.py`: blocked apply reported as unblocked | `test_range_lane_rollout_coverage_gaps.py:425` — AssertionError: assert {'count': 0, 'blocked': False} == {'count': 0, 'blocked': True} | fail → pass |
| R19 | `test_main_apply_records_campaign_dispatches` | `range_lane_rollout.py`: campaign dispatches not recorded | `test_range_lane_rollout_coverage_gaps.py:512` — KeyError: 'dispatches' | fail → pass |
| D1 | `test_parse_ts_run_json_gh_helpers_and_merge_sha` | `durability_sweep.py`: string mergeCommit ignored | `test_durability_sweep_coverage_gaps.py:101` — AssertionError: assert None == 'abc' | fail → pass |
| D2 | `test_default_branch_timeout_and_empty` | `durability_sweep.py`: whitespace default branch returned | `test_durability_sweep_coverage_gaps.py:126` — AssertionError: assert '  \n' is None | fail → pass |
| D3 | `test_verifier_claims_skip_invalid_and_inherited_candidates` | `durability_sweep.py`: replacement-credit claims misattributed as direct | `test_durability_sweep_coverage_gaps.py:164` — AssertionError: assert {('o/r', 10): {'ka', 'local'}} == {('o/r', 10): {'ka'}} | fail → pass |
| D4 | `test_find_merge_rejects_missing_target_named_non_pr_and_no_branch` | `durability_sweep.py`: note-named non-PR falls through to branch search | `test_durability_sweep_coverage_gaps.py:195` — AssertionError: assert 'unanswered' == 'unjudgeable' | fail → pass |
| D5 | `test_read_fix_window_stops_when_split_cannot_narrow` | `durability_sweep.py`: split floor never stops the window | `test_durability_sweep_coverage_gaps.py:227` — AssertionError: assert ('fix-PR search spent its 8 reads' and '1000+ fix PRs' in 'fix-P... | fail → pass |
| D6 | `test_live_revert_and_normalize_and_delivered` | `durability_sweep.py`: completed no-revert-PR answer ignored | `test_durability_sweep_coverage_gaps.py:259` — assert None is False | fail → pass |
| D7 | `test_classify_durability_edge_states` | `durability_sweep.py`: ambiguous DRAFT state judged | `test_durability_sweep_coverage_gaps.py:286` — AssertionError: assert 'durable' is None | fail → pass |
| D8 | `test_load_retry_clocks_rejects_non_dict_and_find_unanswered` | `durability_sweep.py`: non-dict retry clocks not rejected | `test_durability_sweep_coverage_gaps.py:295` — AttributeError: 'list' object has no attribute 'items' | fail → pass |
| D9 | `test_undo_truncated_recheck_missing_log_and_bad_lines` | `durability_sweep.py`: one bad undo line aborts the restore | `test_durability_sweep_coverage_gaps.py:312` — assert 0 == 1 | fail → pass |
| D10 | `test_recheck_truncated_closes_bad_merge_and_unstated` | `durability_sweep.py`: unstated truncated re-check not counted closed | `test_durability_sweep_coverage_gaps.py:367` — assert 0 == 1 | fail → pass |
| D11 | `test_recheck_bare_reference_relabels_and_marks_dry_run` | `durability_sweep.py`: relabel counted as durable | `test_durability_sweep_coverage_gaps.py:413` — assert 0 == 1 | fail → pass |
| D12 | `test_post_merge_rechecks_guard_branches` | `durability_sweep.py`: spec/PR target binding not checked | `test_durability_sweep_coverage_gaps.py:455` — assert ([]) | fail → pass |
| D13 | `test_post_merge_budget_comment_and_transcript_errors` | `durability_sweep.py`: comment-fetch error swallowed | `test_durability_sweep_coverage_gaps.py:577` — assert (["owner/repo#5: 'NoneType' object is not iterable"] and 'comments down' in "own... | fail → pass |
| D14 | `test_post_merge_transcript_hash_mismatch_and_long_body` | `durability_sweep.py`: long transcript not pointed to | `test_durability_sweep_coverage_gaps.py:667` — AssertionError: assert (['body=runtime-AC post-merge FAIL on cccccccccccccccccccccccccc... | fail → pass |
| D15 | `test_post_merge_comment_path_hash_mismatch_after_receipt_ok` | `durability_sweep.py`: comment-path hash re-check removed | `test_durability_sweep_coverage_gaps.py:740` — assert (["owner/repo#16: ['gh', 'api', '-X', 'POST', 'repos/owner/repo/issues/18/commen... | fail → pass |
| D16 | `test_sweep_dry_run_stuck_truncated_and_unread` | `durability_sweep.py`: unread re-check not counted waiting | `test_durability_sweep_coverage_gaps.py:822` — assert 0 == 1 | fail → pass |
| D17 | `test_main_cli_undo_and_sweep` | `durability_sweep.py`: sweep summary line not printed to stderr | `test_durability_sweep_coverage_gaps.py:884` — AssertionError: assert 'ok' in '' | fail → pass |
| D17b | `test_main_cli_undo_and_sweep` | `durability_sweep.py`: --dry-run ignored by the CLI | `test_durability_sweep_coverage_gaps.py:882` — AssertionError: assert [{'grace_days..._run': False}] == [{'grace_days...y_run': True}] | fail → pass |
| D16b | `test_sweep_dry_run_stuck_truncated_and_unread` | `durability_sweep.py`: stuck role run not counted | `test_durability_sweep_coverage_gaps.py:792` — assert 0 >= 1 | fail → pass |

The six regressions recorded in the review section above were not tied to a named assertion,
so each was re-proved here: D3 (inherited claims misattributed), D16 (unread waiting count),
D15 (comment-path hash check, which guards the delivery flags), and R7 (argparse exit code 2).
The undo-row proof is replaced by D9, which tests a different mutation of the same function.
The sixth, the import-bound live registry, is the `isolated_runtime` fixture's lock-path
tripwire and not a test.

### Assertions strengthened (each failed to detect its intended regression on `a538c85`)

| Mutation on `a538c85` | Test | Why it survived | Change |
| --- | --- | --- | --- |
| R2 `>= max_items` → `>` | `test_filter_backlog_keeps_only_limited_eligible_opener_tasks` | no eligible item followed the cap | add eligible `o/r#5` after the cap |
| R3 backup-agent check removed | `test_sanitize_decision_rejects_each_unsafe_assignment_kind` | the aider row was still rejected by the campaign-target check, so the count stayed 4 | assert the four rejection reasons exactly |
| R15 skip branch falls through to `_selftest()` | `test_main_selftest_skips_when_codex_profile_binary_absent` | the real selftest passes on a provisioned machine | stub `_selftest` and assert it did not run |
| D17b CLI passes `dry_run=False` | `test_main_cli_undo_and_sweep` | the stub ignored its kwargs | assert `sweep_durability` received `grace_days=3, dry_run=True` |

All other 38 mutations failed on `a538c85` too.

### Focused restored verification

`/opt/anaconda3/bin/python3 -m pytest -q -o addopts= tests/test_consumer_sync_artifact_ingest_coverage_gaps.py
tests/test_durability_sweep_coverage_gaps.py tests/test_range_lane_rollout_coverage_gaps.py
tests/test_redirect_shadow_helpers_cli_coverage.py` on `d4ce9e2`: `70 passed in 13.76s`.
`black --check -l 100` and `ruff check` on the two changed test files: clean.
