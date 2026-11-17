# PR #1438 continuation acceptance — 2026-11-17

Outcome: local comparative selection changes a deterministic winner only within
its capacity tier; remote selection retains learned ordering; attribution compares
the corresponding baseline after the same pacing fallback. Ambient exports are
isolated while deliberate test paths remain readable.

Command: `/opt/anaconda3/bin/python3 -m pytest tests/test_router_comparative.py -q`.
Stop rule recorded before editing: three failed repair attempts or 60 minutes;
report the first broken step if reached. No elapsed-time ownership eviction.

The branch was clean at fd6a886b. `git worktree list --porcelain` showed this sole
holder of orch/issue-1433 and no preserved holder for this branch. Existing
implementation and the PR body's deliberate-break evidence are retained.

## Observations

- Before production edits, new regressions printed **7 failed, 11 passed**:
  remote ordering; missing, disabled, ineffective and unchanged-winner fallback
  attribution; ambient HANDOFF_DIR and feedback.DB_PATH isolation.
- Revised acceptance command: **22 passed in 0.35s**, including the original five.
  The new fallback cases include flag-off, an unchanged two-seat winner, and a
  genuine comparative change in the unmeasured fallback tier. An RNG that refuses
  draws verifies that the baseline replay consumes no extra randomness.
- Enabled exports reject boolean, negative, fractional and null ranks. The
  separate non-boolean enable test remains.
- Missing files remain quiet. Malformed and unreadable exports return no rank,
  cache the failure, and print one diagnostic per path per process.
- Isolation invokes the actual fixture under each ambient source and verifies a
  deliberate replacement path works and restoring ambient state restores isolation.
- `/opt/anaconda3/bin/python3 -m pytest tests/test_pacing.py -q -k 'router or unmeasured or all_shed or warn_zero or weighted_routing or measured_codex_keeps_nonpacing_exploration_shares or pacing_includes_neutral_nonpool_candidates or remote_reserved_warn'`:
  **21 passed, 108 deselected in 2.75s**.
- A broader focused run of comparative, pacing and issue843 routing coverage
  printed **6 failed, 169 passed in 3.70s**. All six quota-marker recovery failures
  reproduce on an unchanged fd6a886b archive: **6 failed, 8 passed, 115 deselected
  in 0.78s** with the three marker-recovery test functions selected. They are
  outside this continuation's changes; no capacity-marker repair is attempted.
- `/opt/anaconda3/bin/python3 -m pytest --collect-only -q`:
  **9244 tests collected in 12.83s**, up 17 from 9227. Expected pass floor rises
  by the same 17; skip ceilings remain unchanged. config/test-shards.json already
  lists this module once in untimed_modules and once in its assigned shard.

## Limits and review disposition

Comparative ranking still does not control measured posterior-times-pacing
weighted exploitation. Its route_source attribution is confined to deterministic
selection/fallback; exploration and cold starts retain their prior attribution.
The baseline replay has identical candidates and five capacity prefix keys, so
when the revised selection reaches an unmeasured deterministic fallback, the
baseline reaches that same fallback without a random draw.

The five existing review findings are addressed by: once-per-path diagnostics;
local-only transport gating; matched-fallback attribution; resolved ambient-path
isolation; and enabled invalid-rank validation tests. Current-head CI and the
post-push review remain authoritative for delivery. Full suite was not run locally.
