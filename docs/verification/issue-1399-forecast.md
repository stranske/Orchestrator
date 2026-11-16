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

## Follow-up verification in the hosted sandbox (2026-11-16)

Found an additional determinate case: two cycles with disjoint PR candidates and a
two-read budget always reach both cycles, independently of either response. Before
the follow-up change, the offline forecast returned `None`; a standalone Python
assertion expecting `2` failed. `_independent_rule_pick_reach` now proves reach
when candidates are disjoint and the worst-case preceding reads leave budget for
each cycle. It counts repeated non-final candidates separately so a young PR
cannot hide possible budget exhaustion.

Added 12 pytest cases comparing independent cycles with actual linker traversal,
including reverse corpus order, one- and two-read budgets, failed/negative/positive
responses, repeated young PRs, and no forecast fetches or persistence.

Current interpreter: `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`.
The earlier Python 3.12 results above describe a previous environment.

Checks completed in this environment:

- Both `src/outcomes.py --selftest` and `src/triage_shadow.py --selftest` passed.
- `python3 -m py_compile src/outcomes.py tests/test_triage_shadow.py` passed.
- `git diff --check` passed.
- Standalone standard-library harness `/tmp/verify_1399_reach.py` passed 24
  offline/injected/linker comparisons, including consumption-budget competition,
  shared PRs, repeated young PRs, and checks against forecast fetching or writing.
- Removing the actual `_resolve_rule_pick_covers(conn, corpus_rows, search)`
  invocation made that harness fail with `AssertionError: ([], 1)` (no cycle
  reached where one was forecast). Restoring the invocation restored all 24
  passing comparisons. Bytecode was cleared between mutations.

Required gates remain blocked in this environment:

```text
$ python3 -m pytest tests/test_triage_shadow.py -m 'not slow'
.../python3: No module named pytest

$ python3 -m pytest tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound -m 'not slow'
.../python3: No module named pytest

$ black --line-length 100 src/outcomes.py tests/test_triage_shadow.py
black: command not found

$ black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
black: command not found
```

The named pytest command was blocked both with the actual invocation removed
and restored; this is not a completed pytest mutation check. Installing pytest
and Black also failed because the sandbox could not resolve `pypi.org`; neither
package was available in the local package cache. The standalone harness does
not replace the required pytest suite. No additional checkboxes were marked
complete. Follow-up source/test changes remain **uncommitted** because the
mandatory Black gate cannot run. Required next step: provision pytest and Black,
run formatting and the required pytest checks, then commit on the existing branch.
