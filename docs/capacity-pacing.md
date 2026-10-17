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

Codex's machine-readable weekly source is
`~/.codex/sessions/**/rollout-*.jsonl`: `type=event_msg`,
`payload.type=token_count`, `payload.rate_limits.primary.window_minutes=10080`,
`used_percent` divided by 100, and `resets_at` as epoch T1. T0 is T1 minus seven
days. Freshness uses the line's own `timestamp`. Only these allowlisted fields
are extracted: credits and all other account payloads are never copied or printed.
The reader visits the newest 300 files by mtime and uses the newest timestamped
weekly observation; the number of older files excluded is printed explicitly.
A private temporary-file cache stores only sanitized usage observations and per-file
append offsets, validated against inode/size/mtime and boundary fingerprints.
Replacement/truncation forces a rescan; incomplete final lines are retried; original
event timestamps still control freshness. Cache failure costs speed, never changes a
capacity verdict. Every report prints scan duration, files read/cached, bytes processed,
malformed/unreadable counts, total files and omitted files. Warm scan measured
0.349s (7 appended files, 293 cached; 463,088 bytes), versus 9.701s cold
(300 files, 3,606,291,682 bytes). One usage snapshot per build is shared with
`_shed` through a scoped context and passed to pacing integration; active Codex
markers do not trigger a second rollout scan. The cache is derived, disposable data.
Refresh requires active Codex sessions, including `codex exec` dispatches.

A separate read-only scan of all files modified in the last week measured fresh
weekly evidence at **521/672 15-minute ticks (77.53%)**, using the two-tick freshness
threshold. It scanned 543/556 files (13 older mtimes excluded), with 16,857 distinct
weekly observations and zero malformed/unreadable records. The remaining 22.47%
of ticks lacked fresh usage; this is observed availability, not a refresh SLA.
Evidence: `docs/evidence/issue-668/rollout-freshness.txt`. This corrects the earlier
bounded app-support/SQLite search's absence claim; calibration is a fallback,
not a prerequisite for the rollout source.

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

Install the updated owner state from `docs/capacity-resets.sample.json` as
`~/.codex/orchestrator/capacity-resets.json`. One Codex bank entry remains after the observed 2026-10-17 14:55Z reset. Its expiry is UNKNOWN:
2026-11-16 is the owner's 30-day estimate from 2026-10-17. Every inventory FYI makes
that uncertainty explicit. Absence or malformed entries reports `resets=unknown`
and conservatively uses no reset relief, without claiming a measured zero balance.
Expired entries drop out on every read; consumed entries emit an FYI.

Rollout reset signals are detected from a >1-second reset-epoch change or a usage
percentage drop. The full-week scan found 47 such signals, including small
percent reversals and 2–4-second T1 jitter. To avoid false debits, a confirmed
banked consumption requires both a usage drop and a T1 advance greater than one
tick (900 seconds), before the old scheduled rollover. Signals and confirmed
consumptions are counted separately; ordinary scheduled rollover costs no bank
entry. This is an explicit refinement of the literal either-signal debit request
based on observed concurrent-session noise. Automatic inventory accounting is idempotent: consumed entries
are retained for audit, the oldest expiry is debited first, and `observed_at` marks
the inventory baseline so historical signals cannot debit a newly installed
balance. The sample baseline follows today's already-accounted reset. Read-only
builds calculate the effective inventory without writing; recording ticks persist
it. Manually entered `consumed_at` remains a fallback. Codex T0 follows the observed
T1 minus seven days; Claude retains its fixed anchor.

The weekly pacing latch (distinct from an active provider marker) lasts until a fresh measured weekly u<1 observation
postdates it, or the period/reset rolls. Thus a Claude five-hour 429 or a ledger
scan hit cannot latch weekly pacing for days despite fresh weekly headroom. With
missing or stale usage, the hold remains. Physical u>=1 still sheds.
A consumed reset releases
only a dated, active provider-quota marker older than the consumption event;
ordinary auth/model checks run afterward. A fresh post-incident usage sample
clears an active quota/rate-limit/capacity marker only when BOTH weekly usage and
short-window usage are measured below 1: Claude `u.fh`, Codex
`rate_limits.secondary.used_percent` with `window_minutes=300`. Unknown, full or
stale short-window evidence retains the marker; diagnostics print its age, both
usage quantities and the fresh-evidence/cooldown drain condition.
Manual/unreadable/auth/model holds retain their existing release rules.
For Claude, pre-consumption exhaustion samples are discarded without shifting T1.

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
coverage stays unmeasured. Reports print coverage=N/M ticks and observed counts
even when one missing tick prevents a period verdict. A zero non-Codex share in
both populations is unmeasured, never a vacuous PASS. Dispatch-share denominators count actual positive-count
`event=start` rows, not planner invocations. Other dispatch surfaces without such
records remain a coverage limitation, not evidence of population-wide success.

Every pacing reason prints the blocking quantity and a drainable quantity or reset
horizon. The clearance estimate holds u and the current smoothed burn fixed; new
spend can move it. All sampling, expiry handling and time advancement run while
shed. Unknown/stale pacing retains the native capacity state; measured pacing can only
worsen native state. Measured weighting applies only within identical policy tiers
(load, late, ok/warn, capacity bias, drain urgency); all-unknown pacing retains
legacy selection and epsilon exploration. `PACING_POOLS={"codex","claude"}` limits
sampling to these two pools when the legacy winner belongs to that set; an unrelated
legacy winner is returned unchanged. Legacy exploration is drawn first and remains
unchanged for other agents. The coordinator chose to retain posterior × weight
sampling between the pacing pools: posteriors Claude=0.766/Codex=0.612 with
weights 1/0.918 give 57.689%/42.311% of the pacing exploitation tier, despite
Claude's much higher deterministic legacy share. This is an explicit design decision.
Claude stays reserved unless its own
pacing is measured-ok (or the existing high-leverage/last-resort rule applies).
Both `burn_rate_per_day` and `burn_per_day` are emitted with identical values.
Export comparisons ignore clock/projection/burn diagnostics; usage, state and reset
changes still publish immediately, while diagnostics refresh on normal cadence.

The recovered branch emits `pool <p>: ũ=<x> ≤ 1−r_eff → ok`.

The runtime `handoff-relay.sh` is outside this PR. Its agent-choice follow-up is:

```bash
agent="$(python3 src/capacity.py pick-agent --candidates codex,claude --task-type implement)"
```

Use the configured local mirror entrypoint/interpreter when installing that line.
The CLI prints just the agent on stdout and all pacing candidates/numbers on stderr.
It reads learned posteriors without modifying them. Ready-for-review infrastructure
PR; only `autofix` label, no agent label, no source-workflow delegation.

Merge, mirror sync, runtime inventory installation, source freshness (calibration only for the fallback),
relay consumption, and reset-time pass measurements are separate acceptance
boundaries. None is established by local green tests alone.
