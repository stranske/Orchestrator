# Issue #1399 forecast verification

The linker and forecast now share consumption eligibility/traversal and live rule-cycle
selection. Both retain the ordered rule drain and `_PrSearch` cache/budget behavior.
An unknown response consuming the final read now leaves an exact reach: zero during
consumption, or the cycles already reached during the rule pass.

Regression tests in `tests/test_triage_shadow.py` cover failed-read reuse, repeated
unsettled reads, linked-consumption filtering before the limit, final-read reach, and
production forecasting without fetching PRs or persisting evidence. Existing
`summary_line` rendering already preserves judged/no-delivery/unresolved fields and
prints `drainable unknown`; the stronger offline check verifies that behavior.

Verified locally with `/opt/anaconda3/bin/python3` (3.12.2):

- `python3 src/outcomes.py --selftest`: passed.
- `python3 src/triage_shadow.py --selftest`: passed.
- Standard-library harness `/tmp/check_rule_pick_forecast.py`: four checks passed,
  including six budget/cache scenarios and six final-read response variants.
- Additional isolated checks: both linked-consumption limit cases passed; completed
  consumption linking and an idempotent retry passed.
- `python3 -m py_compile src/outcomes.py tests/test_triage_shadow.py`: passed.
- `git diff --check`: passed.

## Formatting gate

```text
$ black --line-length 100 src/outcomes.py tests/test_triage_shadow.py
reformatted tests/test_triage_shadow.py

All done! ✨ 🍰 ✨
1 file reformatted, 1 file left unchanged.

$ black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
All done! ✨ 🍰 ✨
712 files would be left unchanged.
```

## Pytest mutation: `_resolve_rule_pick_covers` in `link_triage_consumption`

Invocation temporarily removed from `src/outcomes.py::link_triage_consumption`, then restored.
Command: `/opt/anaconda3/bin/python3 -m pytest tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound -vv`

### Removed (exit 1)

```text
tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound FAILED [100%]

=================================== FAILURES ===================================
____________ test_linker_resolves_rule_picks_within_the_read_bound _____________

    def test_linker_resolves_rule_picks_within_the_read_bound(brain):
        ...
        result = outcomes.link_triage_consumption(now=10_000, pr_reader=read)
>       assert result["pr_reads"] == 1 <= outcomes.PR_READS_PER_CALL
               ^^^^^^^^^^^^^^^^^^
E       KeyError: 'pr_reads'

tests/test_triage_shadow.py:622: KeyError
=========================== short test summary info ============================
FAILED tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound - KeyError: 'pr_reads'
============================== 1 failed in 0.69s ===============================
```

### Restored (exit 0)

```text
tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound PASSED [100%]

============================== 1 passed in 0.27s ===============================
```

## Full `tests/test_triage_shadow.py`

```text
$ /opt/anaconda3/bin/python3 -m pytest tests/test_triage_shadow.py -q
........................................................................ [ 58%]
...................................................                      [100%]
123 passed in 10.17s
```

Committed on branch `codex/issue-1399` (PR #1400); production mirror sync not performed here.