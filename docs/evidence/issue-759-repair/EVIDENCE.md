<!-- closer-core/v1 repair-of=stranske/Orchestrator#759@762 -->

Refs #759

- [x] Record an actual deliberate-break failure and reverted pass for each of the nine added tests.
- [x] Provide passing CI pytest evidence and complete comparable Gate coverage accounting, documenting any shortfall and omitted-target reasons. Branch misses are explicitly unmeasured by the existing CI procedure; no blended delta is claimed.
- [x] Document the repository ranking evidence used to select these targets.

<!-- workflow-source:local_request -->

## Scope and acceptance (checked 2026-11-03)

Retain all tests merged by #762. This repair corrects the previous evidence document and records actual executions; it changes no tests or production code. Branch rebased on main `15dae5f5b5c3b0d8f775c51772cfcddb1b382c43` before the runs. The previously open repair PR #1067 is reused, so this task creates no second PR.

Acceptance outcome: nine named tests each fail at an assertion after a targeted production mutation and pass after byte-for-byte restoration, with the two retained test files passing together. Stop rule: 60 minutes or two failed reproductions of the same required check. No assertion needed strengthening.

Required command and actual output (also in `focused-pytest.txt`):

```text
$ /opt/anaconda3/bin/python3 -m pytest -q tests/test_local_verify_coverage.py tests/test_repo_knowledge_cli_coverage.py
............................                                             [100%]
28 passed in 0.41s
```

## Actual deliberate break / revert executions

Each row used `/opt/anaconda3/bin/python3 -B -m pytest -q <exact-node>` on 2026-11-03. Each RED exited 1 with the named test's assertion failure; each reverted GREEN exited 0. Full commands, mutation text, pytest failure details and passes are in [mutation-runs.txt](mutation-runs.txt). Only one production mutation was present at a time; a `finally` block restored the original bytes before GREEN. `git diff --exit-code origin/main -- src/local_verify.py src/repo_knowledge.py tests/test_local_verify_coverage.py tests/test_repo_knowledge_cli_coverage.py` passed afterward.

| Test | Temporary replacement | Observed RED | Reverted GREEN |
|---|---|---|---|
| `test_run_argv_preserves_partial_text_after_a_timeout` | `return report, ""` | `E       AssertionError: assert '' == 'partial node output'` | 1 passed |
| `test_main_renders_json_and_transcript_without_changing_the_verdict` | `return 1` | `E       AssertionError: assert 1 == 0` | 1 passed |
| `test_main_exports_agent_rules_and_requires_complete_apply_arguments` | `print("broken export", end="")` | `E       AssertionError: assert 'broken export' == 'export:owner/repo'` | 1 passed |
| `test_main_exports_all_registered_repos` | `if False` | `E       assert '{}' == '{\n  "a/repo": "A"\n}'` | 1 passed |
| `test_main_validates_agent_exports_and_preserves_status` | `return 0` | `E       AssertionError: assert 0 == 1` | 1 passed |
| `test_main_suggests_from_snapshot` | `print("broken snapshot")` | `E       AssertionError: assert '/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-279/test_main_suggests_from_snapsh0/snapshot.json' in 'broken snapshot\n'` | 1 passed |
| `test_main_searches_with_all_optional_filters` | `include_runs=True,` | `E       AssertionError: assert {'target': 'o...'opener', ...} == {'target': 'o...'opener', ...}` | 1 passed |
| `test_main_audits_scopes_in_machine_and_human_formats` | `return 0` | `E       AssertionError: assert 0 == 1` | 1 passed |
| `test_main_suggests_from_docs_with_requested_sections` | `include_root_docs=False,` | `E       AssertionError: assert {'path': Posi...': False, ...} == {'path': Posi...s': True, ...}` | 1 passed |

## Recovered whole-CI coverage, not standalone test totals

Both historical payloads were recovered on 2026-11-03. The repository's producer is `python3 src/verify.py --coverage --floor-may-lag`, combining pytest and module selftests. In these historical workflows the **CI** job publishes the exact `gate-coverage` artifact. The separate **Gate** workflow has **zero artifacts** on both runs; its pytest-only coverage table is not substituted for the combined measurement.

- Baseline: [CI run 39162698522](https://github.com/stranske/Orchestrator/actions/runs/39162698522), push checkout `77ba39c604227489974cc87a52c944658981df82`; artifact `12149357631` (`gate-coverage`). Combined coverage job succeeded. Overall CI failed because the non-coverage verification job rejected main's lagging collection floor; pytest itself had 5216 passed, 0 failed, 27 skipped, 5243 collected. Baseline [Gate 39162698786](https://github.com/stranske/Orchestrator/actions/runs/39162698786) succeeded.
- PR #762: [CI run 39162854519](https://github.com/stranske/Orchestrator/actions/runs/39162854519), PR head `7624cadfc1b864af5920a9a1a29c9ac9982241d3`, tested merge checkout `661ccd6c330073ce1c6f70b9d0108c4320154476` into that exact baseline; artifact `12149512946` (`gate-coverage`). CI succeeded: 5225 passed, 0 failed, 27 skipped, 5252 collected. [Gate 39162854784](https://github.com/stranske/Orchestrator/actions/runs/39162854784) succeeded, including Python 3.12/3.13, format, Ruff and mypy. Gate Python 3.13 printed `5211 passed, 41 skipped, 9408 warnings in 314.04s (0:05:14)`.
- Both combined runs instrumented 113 processes. Target production sources and coverage configuration were unchanged by #762, which added only the nine tests. Coverage.py 7.16.2, same 74010 whole-repo statements, same target statement counts, same `branch = false` setting. Complete downloaded payloads were inspected; [coverage-extract.json](coverage-extract.json) preserves full target records, global totals, metadata, artifact IDs and SHA-256 hashes of the complete payloads. [historical-ci-results.txt](historical-ci-results.txt) retains checkout identity and printed summaries.

| Target | Baseline statements / misses | PR statements / misses | Baseline branch misses | PR branch misses | Comparable statement-miss reduction |
|---|---:|---:|---|---|---:|
| `src/local_verify.py` | 441 / 37 | 441 / 17 | unmeasured | unmeasured | 20 |
| `src/repo_knowledge.py` | 1335 / 238 | 1335 / 173 | unmeasured | unmeasured | 65 |
| Targets combined | 1776 / 275 | 1776 / 190 | unmeasured | unmeasured | **85** |
| Whole repository | 74010 / 10053 | 74010 / 9969 | unmeasured | unmeasured | 84 |

Formula: `(37 + 238) - (17 + 173) = 85` fewer target statement misses. Repository net reduction is 84, so other files net one additional miss between executions; it is not attributed to this tests-only change. Whole-repo statement coverage: 86.4167004458857% → 86.53019862180787%.

**Statement-plus-branch delta remains UNKNOWN.** Both complete authoritative payloads set `meta.branch_coverage=false` and omit branch counters; missing branch counts are not zero. The existing CI procedure cannot supply historical blended misses. Changing CI or inventing a new local branch baseline would exceed this evidence repair and would not recover the historical Gate measurement. This is the explicit accounting limitation, even though historical statement evidence was recovered.

Reproduction of artifact retrieval:

```sh
gh run download 39162698522 -n gate-coverage -D /tmp/issue759-coverage/baseline
gh run download 39162854519 -n gate-coverage -D /tmp/issue759-coverage/pr
gh run view 39162854519 --log
gh run view 39162854784 --log
```

## Shortfall, stopping boundary and omitted work

The measured target improvement is **85 statements, 95 below approximately 180**; a combined statement-plus-branch shortfall cannot be computed because branches were not measured. This repair itself adds **zero** coverage units. The earlier document's claim of a 259-unit improvement was invalid: executing a small test subset does not measure incremental whole-CI coverage.

The original chunk stopped after two timeout/output tests and seven CLI routing tests. Its PR acknowledged that a local combined run did not finish within its command surface's 30-second ceiling. The record does not prove a broader untestability boundary or why no more tests were written. This repair stops because the owner expressly limited it to evidence for those nine tests and prohibited expansion merely to reach 180.

Neither selected module was omitted entirely. Both have remaining misses (17 and 173 statements respectively); those paths were outside the retained nine-test chunk. No evidence establishes that they cannot meaningfully be covered. In particular filesystem work can use temporary directories, and subprocess/network paths can be stubbed: the previous assertion that all remaining code requires non-hermetic tests is withdrawn. Review-JSON/PR suggestion routes were not covered by these nine tests; that is an omitted path, not a demonstrated impossibility. Follow-up list only: remaining statement paths and currently unmeasured branch coverage; no implementation expansion here.

## Repository ranking evidence

The authoritative selection is the explicit Scope in [issue #759](https://github.com/stranske/Orchestrator/issues/759): escaped-defect priority, then churn, then uncovered mass, in this order:

| Order | Target | Escaped-defect signals | Churn | Issue's uncovered units | Fix commit touched file |
|---|---|---:|---:|---:|---|
| 1 | `src/local_verify.py` | 6 | 9 | 35 | yes |
| 2 | `src/repo_knowledge.py` | 4 | 8 | 238 | yes |

The issue says approximately 273 units total. Recovered baseline CI instead measures 37 + 238 = 275 statement misses; the local_verify difference is explicitly retained rather than treating the issue estimate as a measurement. The issue's ranking inputs support selection of these two modules and order; this repair does not claim to have independently reconstructed the original fleet ranking generator. Public CLI preference, file size, naming patterns and the configured coverage threshold are not ranking evidence.

## Current repair checks

The required focused pytest command passes on the rebased branch; all nine RED/GREEN executions pass the discrimination check. Existing GitHub CI/Gate workflows run on the updated repair PR. Their fresh results are reported in the PR; historical #762 success is kept separate from current-head status. No thresholds, floors, workflow configuration, runtime flags, tests or production sources changed.
