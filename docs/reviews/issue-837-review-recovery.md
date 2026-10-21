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
