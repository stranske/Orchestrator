# Issue #839 evidence repair

Checked **2026-11-03**. This repairs the evidence from #1060 and preserves all
productive tests and malformed/nonfinite timestamp fixes delivered by #842.
It adds no coverage or production changes.

- [x] Link successful CI pytest containing all ten new cases.
- [x] Capture each mutation failure and restored pass, with commands and output.
- [x] Report supported partial coverage and its limitations.
- [x] Cite historical repository ranking output and distinguish missing evidence.
- [x] Remove unsupported and contradictory assertions.

## Successful pytest

[CI run 39488345332, verdict job 118854556372](https://github.com/stranske/Orchestrator/actions/runs/39488345332/job/118854556372)
succeeded for #842 head `feff391af64038f118db167dc0b866a917523b06`:

```text
pytest:     7528 passed, 0 failed, 30/31 max (7558 collected; floor 7509 — 49 BEHIND)
selftests:  108 of 115 modules ran, 7/7 max skipped
```

The actual checkout was merge `4d4636e900dd242468301a76db0338bca7f910df` into
`a538c8527d0e05f7cc78b30d599d61194d156078`. That merge adds exactly the ten
unconditional cases in `tests/test_issue839_ranked_coverage.py` and the narrow
production fixes. Baseline collected 7548 cases; head collected 7558. The test file
is byte-for-byte unchanged in this repair (git blob
`758ba8dc3eeab41c50dc0615c43acad59d20e72d`). The retained CI output is aggregate,
not ten individual verdicts.

[Gate run 39488345963, Python 3.12 job 118854827616](https://github.com/stranske/Orchestrator/actions/runs/39488345963/job/118854827616)
also succeeded: `7434 passed, 124 skipped, 6 warnings in 992.36s`.
Gate selected `not quarantine and not slow`; these ten cases carry neither marker.

The required first local command used fresh directories for both runtime knobs:

```sh
ORCH_STATE_DIR=$(mktemp -d /private/tmp/issue839-state.XXXXXX) \
ORCH_LOCAL_RUNTIME=$(mktemp -d /private/tmp/issue839-runtime.XXXXXX) \
/opt/anaconda3/bin/python3 -m pytest tests/test_issue839_ranked_coverage.py -v
```

Exit 0: `10 passed in 0.51s`. After all mutations were restored, the same focused
command with fresh directories exited 0: `10 passed in 0.29s`.
[Final verbose output](docs/verification/issue-839-repair/restored-pytest.txt) names
all ten cases. No full suite or release/verify pipeline was run locally.

## Captured break-and-restore executions

[Full transcript](docs/verification/issue-839-repair/mutations.txt) contains every
exact replacement, single-node command, stdout/stderr, exit code, and restored pass
(only trailing whitespace is normalized).
Each command used `/opt/anaconda3/bin/python3 -m pytest
'tests/test_issue839_ranked_coverage.py::<node>' -v`, isolated `ORCH_STATE_DIR`
and `ORCH_LOCAL_RUNTIME`, and temporary
`PYTHONPYCACHEPREFIX` to avoid stale bytecode. Source was restored in `finally`
and checked byte-for-byte. No mutation is included in the PR.

Every row has **mutated exit 1; restored exit 0 (one passed)**. Node names omit
only the common file prefix.

| # | Node | Actual mutation | Captured failure |
| --- | --- | --- | --- |
| 1 | `test_redirect_helpers_keep_invalid_corpus_rows_nonfatal` | `_existing_sweep_fingerprints`: replace timestamp conversion with `ts = 0` | `AssertionError: assert set() == {'from-report', 'good'}` |
| 2 | `test_claim_inputs_and_experiment_recording_cover_skip_paths` | `_claim_watch_inputs`: return `[]` for `expected_paths` | `AssertionError: assert [] == ['tests/', 'src/']` |
| 3 | `test_sweep_and_recording_are_advisory_and_count_every_skip` | `sweep`: change `"mutates_state": False` to `True` | `assert True is False` |
| 4 | `test_doctor_and_watch_helpers_handle_bad_inputs_without_live_access` | `_read_report_status`: change exception fallback `age_seconds = None` to `0` | `assert 0 is None` |
| 5 | `test_classify_format_and_cli_paths_stay_read_only` | `classify_lane`: disable missing-input guard with `False and ...` | `AssertionError: assert 'running' == 'missing'` |
| 6 | `test_redirect_sweep_main_cli_paths_are_advisory` | `main`: return 1 after printing doctor output | `AssertionError: assert 1 == 0` for `main(['--doctor'])` |
| 7 | `test_nonfinite_corpus_timestamp_is_skipped[inf]` | Remove `OverflowError` from corpus timestamp handler | `OverflowError: cannot convert float infinity to integer` |
| 8 | `test_nonfinite_corpus_timestamp_is_skipped[-inf]` | Same removal, separate parameter execution | `OverflowError: cannot convert float infinity to integer` |
| 9 | `test_nonfinite_report_timestamp_has_unknown_age[inf]` | Remove `OverflowError` from report timestamp handler | `OverflowError: cannot convert float infinity to integer` |
| 10 | `test_nonfinite_report_timestamp_has_unknown_age[-inf]` | Same removal, separate parameter execution | `OverflowError: cannot convert float infinity to integer` |

Rows 7–10 are behavioral exception failures at conversion, before assertions finish;
they are not claimed as `AssertionError`s. They reproduce the inspection crash the
tests prevent. Rows 3 and 4 now fail assertions inside the actual affected tests;
row 4 exercises explicit `now=100`. Rows 8–10 replace the prior mutations that
would not fail their cited tests.

The capture harness initially could not find a proposed guard string for row 5;
it stopped before mutating or running that node. The corrected actual guard is
recorded in the transcript. No failed test execution was hidden or retried.

## Comparable CI coverage: supported partial result

The historical `.github/workflows/ci.yml` combined job executed this existing
command on **both** comparable trees with isolated runtime directories:

```sh
python3 src/verify.py --coverage --floor-may-lag 2>&1 | tee /tmp/combined.txt
```

These are recovered CI executions, not new local full-suite runs. Baseline is the
tested merge's exact first parent, not today's main or focused pytest-only coverage.
`git diff a538c852 4d4636e9 --stat` contains only:

```text
 src/redirect_sweep.py                  |  15 +-
 tests/test_issue839_ranked_coverage.py | 647 +++++++++++++++++++++++++++++++++
 2 files changed, 659 insertions(+), 3 deletions(-)
```

The coverage workflow, `src/verify.py`, and `pyproject.toml` do not differ across
these trees. Both payloads use coverage.py 7.16.2 with `branch_coverage: false`.

| Measurement | Baseline | #842 tested merge |
| --- | --- | --- |
| Tree | `a538c8527d0e05f7cc78b30d599d61194d156078` | `4d4636e900dd242468301a76db0338bca7f910df` |
| CI run | [39487981121](https://github.com/stranske/Orchestrator/actions/runs/39487981121) | [39488345332](https://github.com/stranske/Orchestrator/actions/runs/39488345332) |
| Combined measurement job | [118853305725](https://github.com/stranske/Orchestrator/actions/runs/39487981121/job/118853305725), success | [118854556173](https://github.com/stranske/Orchestrator/actions/runs/39488345332/job/118854556173), success |
| `gate-coverage` artifact | [12295566251](https://github.com/stranske/Orchestrator/actions/runs/39487981121/artifacts/12295566251) | [12295346597](https://github.com/stranske/Orchestrator/actions/runs/39488345332/artifacts/12295346597) |
| pytest | 7518 passed, 0 failed, 7548 collected | 7528 passed, 0 failed, 7558 collected |

Baseline **workflow overall failed**; its combined measurement job succeeded.
Do not interpret it as an all-green baseline. Both measured 108 selftests and seven
skipped modules. [Retained provenance](docs/verification/issue-839-repair/ci-provenance.txt)
contains checkout/command/output excerpts, hashes, missing-line lists and metadata.
Original `coverage.json` files are preserved losslessly inside
[baseline-coverage.zip](docs/verification/issue-839-repair/baseline-coverage.zip) and
[head-coverage.zip](docs/verification/issue-839-repair/head-coverage.zip).

| Target | Baseline statements / missing lines | Head statements / missing lines | Missing-line reduction | Missing branches (both) |
| --- | ---: | ---: | ---: | --- |
| `src/redirect_sweep.py` | 437 / 88 | 446 / 5 | 83 | unmeasured |
| `src/watch.py` | 486 / 62 | 486 / 15 | 47 | unmeasured |
| Total | 923 / 150 | 932 / 20 | **130** | **unmeasured** |

Recalculate from the retained payloads without running tests:

```python
import json, zipfile
from pathlib import Path
root = Path('docs/verification/issue-839-repair')
for phase in ('baseline', 'head'):
    with zipfile.ZipFile(root / f'{phase}-coverage.zip') as archive:
        data = json.loads(archive.read('coverage.json'))
    for target in ('src/redirect_sweep.py', 'src/watch.py'):
        summary = data['files'][target]['summary']
        lines = summary['missing_lines']
        branches = summary.get('missing_branches')
        units = lines + branches if branches is not None else None
        print(phase, target, lines, branches, units)
```

Use `missing_lines + missing_branches`, never `num_partial_branches`. Missing branch
data is unknown, not zero. The supported line-only reduction is **130**, **50 below
180** on that measure. The statement-plus-branch reduction and its shortfall remain
unmeasured. Nine extra executable statements in the production fix mean covered-line
gain (139) differs from missing-line reduction (130).

This is **partial combined-CI evidence, not a verified Gate line-plus-branch
reduction**. Historical Gate run 39488345963 currently lists zero retained artifacts;
its pytest-only payload cannot be recovered from that listing. The issue explicitly
identifies combined pytest+selftests as authoritative, so these recovered combined
payloads are useful but do not prove the requested branch total. Current Gate
disables reusable-job coverage and consumes CI's combined artifact; current CI uses
shards and aggregation, so its commands must not be substituted for the historical
measurement. This repair changes no workflow settings.

Remaining historical head misses are exactly `redirect_sweep.py` lines
460, 461, 835, 1008, 1009 and `watch.py` lines 153, 174, 228, 375, 378, 379,
392, 393, 395, 396, 552, 554, 614, 943, 944. They are unexercised in that run;
the data does not establish that they are unreachable or require live state.
No extra tests were requested. Gains from earlier PRs on the first four targets
are not credited to #842 or this repair.

## Original repository ranking

[Original issue #839](https://github.com/stranske/Orchestrator/issues/839) contains
the historical generated ranking output, preserved in
[original-ranking.txt](docs/verification/issue-839-repair/original-ranking.txt).
It orders by escaped-defect signals, then churn, then uncovered mass:

| Rank | Target | Reported uncovered units | Escaped signals | Churn |
| --- | --- | ---: | ---: | ---: |
| 1 | `keepalive_outcomes.py` | 43 | 3 | 6 |
| 2 | `issue_quality.py` | 5 | 3 | 6 |
| 3 | `experiment_recovery.py` | 9 | 2 | 5 |
| 4 | `research_usage_guard.py` | 27 | 2 | 2 |
| 5 | `redirect_sweep.py` | 88 | 1 | 5 |
| 6 | `watch.py` | 62 | 1 | 5 |

The historical local dispatch log independently records #839 opening on
2026-10-20 at 13:00:50 -0500 with six targets and 234 units (excerpt in provenance).
[PR #842](https://github.com/stranske/Orchestrator/pull/842) explicitly chooses the
**final two** ranked targets. This verifies historical selection from rendered
output; they were not the highest-ranked two. The underlying original ranker
JSON/input snapshot has not been recovered. The issue's label "statements+branches"
is not independently verified by the retained line-only payloads. No current
ranking was substituted as historical proof.

Refs #839
