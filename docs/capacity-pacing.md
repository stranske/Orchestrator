# Capacity pacing (#668)

`src/pacing.py` owns the seven-day periods, six-hour burn window, 15-minute tick,
source staleness after two ticks, reserve, reset relief, early-spend band and all
three pass bounds. Math has no I/O; clocks are passed explicitly. Two observations
are necessary for a burn estimate. A measured zero burn is distinct from undefined.
The six-hour exponentially weighted interval rates clip spanning intervals to the
window; fresh flat observations drain old spend out without another dispatch.

The literal revised reserve formula is authoritative: `0.15 * max(0, 1 - 0.5*n)`:
zero/one/two resets give 0.15/0.075/0. The brief's one-reset example of 0.1125 conflicts
with that formula. The routing formula also reaches zero at the reserve boundary,
before shed: positive weights are sampled by posterior × weight, zero-weight warn
pools form a second tier, and pacing-shed pools form the final best-posterior tier.
A warn pool never becomes shed merely because its weight is zero. Explicit auth,
model or manual holds are not quota pacing and cannot be bypassed.

## Source findings (2026-10-17, read-only)

Claude's desktop usage source is
`~/Library/Application Support/Claude/plan-usage-history.json`. It held 236 samples
with keys `t` (milliseconds), `org` (never exported), and `u: {fh, sd}` (five-hour and
seven-day percentages). Median consecutive observation interval: 900.054 seconds.
Latest observed source timestamp: 1792198800402 ms (stale at implementation time).
Refresh is app-driven, not guaranteed. The reader uses the observation timestamp,
not the file mtime. Its fixed weekly anchor is 2026-10-18 04:59Z; subsequent anchors
are seven days apart, including 2026-10-25. A banked reset does not move that anchor.

No usable Codex percent store was located. Search locations included:

- `~/Library/Application Support/Codex` and `com.openai.codex`:
  local/session storage were searched for `rate_limits`, `primary_window`,
  `used_percent`; app cache/state filenames were inventoried.
- `~/.codex/.codex-global-state.json`: no usage percentage payload found;
  rate-limit announcement dismissal keys are not quota observations.
- Read-only SQLite schemas in `~/.codex`: `state_5.sqlite`, `logs_2.sqlite`,
  `thread_history_1.sqlite`, `queue_1.sqlite`, `goals_1.sqlite`, `memories_1.sqlite`.
  None exposed a provider percentage/rate-limit table or column. Token accounting
  is not a plan percentage. Logs may contain text not indexed as structured usage.
- Matching OpenAI/Claude paths under `~/Library/Containers`,
  `~/Library/Group Containers`, `~/Library/Caches`, and `~/Library/Logs`.
  The matching filename inventory was limited to depth four; deeper caches,
  compressed stores and proprietary app internals remain unverified.

This is a bounded absence finding, not proof a private Codex store cannot exist.
No auth stores, tokens or arbitrary log excerpts were printed. A new stable source
needs a schema-specific reader and fixture before being trusted.

Fallback: `~/.codex/usage-snapshots/weekly-*.json`, plus a calibrated per-pool cap.
All three files use `agent=all`; the latest file names both Claude and Codex in
its metadata, and those combined totals are rejected. An older file includes
single-agent weeks, but its latest matching observation is stale (2026-09-04). Agent-specific calendar-week totals are usable
only when their start equals the actual pool's period start; they cannot measure
a bank-reset-shifted or different anchored week. The active ccusage five-hour block
remains an existing native limiter, not an interchangeable weekly numerator. A
source path in future agent-specific snapshots is represented by its stable source
class, allowing successive snapshots to feed the same EWMA. Each tick records
`source`, source `ts`, and `recorded_at`; samples used as tick observations also keep
`source_ts`. Stale records remain visible and never become fresh usage.

`src/capacity.py calibrate-codex` writes
`~/.codex/orchestrator/capacity-calibration.json` only with at least two distinct
quota incidents that carry agent-specific `extra.weekly_tokens` and
`extra.period_start`, or matching source-labelled token samples within two ticks.
It uses their median and preserves all evidence. The 81 Codex incidents inspected
had no such projections; calibration must currently refuse. No cap or T0 is guessed.
An unscaled agent-specific token sample can seed calibration without an existing
cap. A 429 while the prior fresh model said ok appends a calibration miss; a known
cap is lowered to observed spend. Missing correction inputs remain unmeasured.

## Runtime inventory and reset events

Install the exact owner state from `docs/capacity-resets.sample.json` as
`~/.codex/orchestrator/capacity-resets.json`. The two Codex expiry dates are UNKNOWN:
2026-11-16 is the owner's 30-day estimate from 2026-10-17. Every inventory FYI makes
that uncertainty explicit. Absence or malformed entries reports `resets=unknown`
and conservatively uses no reset relief, without claiming a measured zero balance.
Expired entries drop out on every read; consumed entries emit an FYI.

To record a real consumption event, preserve its entry and add `consumed_at` as an
ISO UTC timestamp. Codex T0 jumps to the latest consumption instant and T1=T0+7d;
Claude keeps its anchor. Fresh usage is required in that period. Physical exhaustion
or a ledger 429 latches shed until the period rolls or an explicit reset occurs;
new inventory alone does not erase actual exhaustion.

The implementation sandbox denied both the requested sibling checkpoint and the
runtime inventory destination. Checkpoints are append-only at
`/private/tmp/pacing-668-CHECKPOINT.md`; the inventory sample is shipped here, but
runtime installation is an explicit outstanding deployment action.

## Consumption and verification

The normal capacity CLI records samples in
`~/.codex/handoff/pacing-usage-samples.ndjson` and `pacing_snapshot` rows in
`capacity-ledger.ndjson`; read-only `build()` and route export do not invent ticks.
`src/capacity.py pacing-report` prints all three pass queries and evidence classes.
Usage-at-reset needs a fresh snapshot within two ticks of completed T1. An empty
incident ledger is measured zero only with full-period snapshot coverage; absent
coverage stays unmeasured. Dispatch-share denominators count actual positive-count
`event=start` rows, not planner invocations. Other dispatch surfaces without such
records remain a coverage limitation, not evidence of population-wide success.

Every pacing reason prints the blocking quantity and a drainable quantity or reset
horizon. The clearance estimate holds u and the current smoothed burn fixed; new
spend can move it. All sampling, expiry handling and time advancement run while
shed. The recovered branch emits `pool <p>: ũ=<x> ≤ 1−r_eff → ok`.

The runtime `handoff-relay.sh` is outside this PR. Its agent-choice follow-up is:

```bash
agent="$(python3 src/capacity.py pick-agent --candidates codex,claude --task-type implement)"
```

Use the configured local mirror entrypoint/interpreter when installing that line.
The CLI prints just the agent on stdout and all pacing candidates/numbers on stderr.
It reads learned posteriors without modifying them. Ready-for-review infrastructure
PR; only `autofix` label, no agent label, no source-workflow delegation.

Merge, mirror sync, runtime inventory installation, source calibration/freshness,
relay consumption, and reset-time pass measurements are separate acceptance
boundaries. None is established by local green tests alone.
