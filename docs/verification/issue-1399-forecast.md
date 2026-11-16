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

Verified locally with Python 3.12.16:

- `python3 src/outcomes.py --selftest`: passed.
- `python3 src/triage_shadow.py --selftest`: passed.
- Standard-library harness `/tmp/check_rule_pick_forecast.py`: four checks passed,
  including six budget/cache scenarios and six final-read response variants.
- Additional isolated checks: both linked-consumption limit cases passed; completed
  consumption linking and an idempotent retry passed.
- `python3 -m py_compile src/outcomes.py tests/test_triage_shadow.py`: passed.
- `git diff --check`: passed.

The standard-library mutation check removed the actual
`_resolve_rule_pick_covers(conn, corpus_rows, search)` invocation in the linker.
The rule-pass check failed with `KeyError: 'pr_reads'` (exit 1). Restoring the
invocation produced a passing check (exit 0). Temporary transcripts are in
`/tmp/issue-1399-verification/rule-pass-{removed,restored}.log`.
This is supplemental evidence: the specifically requested pytest mutation run is
still unverified.

Required checks are blocked by the environment:

- `pytest tests/test_triage_shadow.py -m "not slow" -q`: command unavailable;
  `python3 -m pytest` also reports no module named pytest.
- `black --line-length 100 src/outcomes.py tests/test_triage_shadow.py`: command unavailable.
- `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`:
  command unavailable.
- Installing pytest and Black into `/tmp/orchestrator-task-venv` failed because
  the sandbox cannot resolve PyPI. The local package cache contains neither tool.

No tasks are marked complete and no commit or push was made: the task prompt
explicitly prohibits committing Python changes before Black passes. Follow-up
requires running the required formatting gate, the targeted pytest suite, and the
named pytest mutation check with the production invocation removed and restored,
then committing the source and tests on the existing branch. No production sync
was performed.
