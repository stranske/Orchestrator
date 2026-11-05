# Issue 1154 mutation evidence

Check date: 2026-11-05.

Outcome 1: **76/76** collected pytest node IDs each reject one named temporary production
mutation with the intended failure at the mapped test line, then pass individually after
byte-for-byte restoration.

Commands:

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue1154_ranked_coverage.py tests/test_issue1154_tick_boundaries.py
/opt/anaconda3/bin/python3 tests/mutation_runner_1154.py --output tests/evidence_1154/results.json
```

Stop rule: one mutation/restoration attempt per case (120s pair cap, 55s per pytest process);
stop the batch on any STOP. Observed batch: **611.223s** total pair time; longest pair **12.269s**.

- [Case map](mutation_cases_1154.json): 76 node IDs with production before/after text.
- [Raw receipts](evidence_1154/results.json): unified diffs, pytest stdout, JUnit failures, restores.
- [Focused module output](evidence_1154/focused.txt): `76 passed`.

Full acceptance narrative (coverage, merge CI, native storage): [../docs/issue1154-acceptance.md](../docs/issue1154-acceptance.md).
