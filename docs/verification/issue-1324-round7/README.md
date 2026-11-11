# Round 7 acceptance record

Check date: 2026-11-10. Outcome 1 is a zero exit and all focused acceptance tests passing for a real checkout of PR #1334. Command:

```
/opt/anaconda3/bin/python3 -m pytest tests/test_issue1232_ranked_coverage.py tests/test_verify_sharding.py -q --no-cov
```

Before editing: 32 passed in 13.71s; plugin-disabled sharding command also passed (22 tests, 13.26s). The supplied temporary Python 3.14 interpreter has been deleted, so its reported subprocess collection failure cannot be attributed from the truncated traceback. No dependency pins or collection rules will be changed speculatively.

Stop rule: three repair attempts or 60 minutes. Only failures printed by the focused acceptance path are repair targets. No full suite or verify run locally. Coverage artifacts are downloaded from CI; missing branch data remains unknown.

## Results

- Current acceptance: 33 passed in 13.42s. `acceptance.log` also records the plugin-disabled acceptance invocation (`-p no:cov -o addopts=`), matching the supplied runner's plugin configuration.
- All eight tests added since the salvaged three-test module have fresh deliberate production mutations: each named test failed (exit 1) and then passed after the exact original bytes were restored (exit 0). See `mutations.json` and each paired red/green log. No production change remains.
- Black with `--line-length 100`, Ruff, and `git diff --check` pass.
- The existing module occurs once in `shards` and once in `untimed_modules`; no new module was added, so no shard manifest edit is necessary. The acceptance sharding test verifies the full collection partition without executing the full suite.
- The fixture now restores argv and rejects unexpected subprocess/Popen starts. The backend test only constructs a command and writes its report inside tmp_path; it never dispatches that command.

## Gate evidence and ranked gap selection

`gate-targets.json` preserves the target records, metadata, source heads, run IDs, and SHA256 of the complete downloaded artifacts. Baseline is salvaged PR #1233 CI run 42431925653; comparison is pre-round head a58e10bf CI run 42863205636. Both aggregate pytest and selftests. Both explicitly report `branch_coverage: false`: they establish combined execution **statement** coverage, not blended statement-and-branch coverage.

| Target in required ranking | Salvaged Gate | Pre-round Gate | Additional covered statements |
| --- | --- | --- | --- |
| runtime_ac_panel | 289/338 (85.50%) | 337/338 (99.70%) | 48 |
| relearn_report | 243/260 (93.46%) | 260/260 (100%) | 17 |
| keepalive_supervisor | 615/659 (93.32%) | 632/659 (95.90%) | 17 |
| Total | 1147/1257 (91.25%) | 1229/1257 (97.77%) | 82 |

These are observations across complete CI runs, not an attribution of every gained statement to this PR: intervening main and other test changes also contribute. The supervisor gained an unrelated goal-drift hold label on main; its line numbers shift by three. Panel and relearn source are identical between those heads.

Selection in rank order: panel's only remaining statement (812) is the fallback after argparse's required mutually exclusive action group; valid CLI inputs cannot reach it. Do not weaken parsing or add an exclusion to execute it. Relearning has no missing statements. The supervisor's remaining backend option (659, formerly 656) can be exercised without a live worker; the new test asserts both the exact backend argument and derived recording command. Its mutation substitutes a wrong backend and the test fails.

Partial result: the observed pre-round improvement is 82 statements, 98 below the original approximately 180-unit goal if only these measured statement gains are counted. The new backend test targets one more statement; fresh pushed-head CI must measure that gain. Branch gains are UNKNOWN because both artifacts lack branch data. No coverage exclusions were added: each target still has its two pre-existing excluded lines. Panel's unreachable fallback is documented, not excluded; relearning needs branch evidence rather than duplicate statement tests. Supervisor still has 26 other missing statements in closure validation/locking/budget paths, Stage-2 candidate/error/replay branches, corpus filtering, and heartbeat failure handling. Those need additional hermetic scenarios; they are not claimed unreachable or completed by this partial chunk.

## Outstanding verification

Fresh pushed-head normal and flat exec-mirror CI must pass without floor/skip ceiling changes. The supplied Python 3.14 temporary-runtime failure remains unclassified: its traceback omits the underlying collection error and its interpreter is gone. Both repository fallback acceptance forms pass; this is not evidence that the deleted environment was repaired.
