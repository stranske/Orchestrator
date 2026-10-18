# Capacity pacing (#668)

`src/pacing.py` owns the seven-day period, six-hour burn window, reserve, reset
relief, early-spend band and all three pass bounds. Math has no I/O; clocks are
explicit. Two observations are necessary for a burn estimate; measured zero burn
is distinct from undefined. The six-hour exponentially weighted interval rates
clip spanning intervals to the window. Fresh flat observations drain old spend
without another dispatch.

The reserve formula is `0.15 * max(0, 1 - 0.5*n)`: zero/one/two resets give
0.15/0.075/0. Positive routing weights are sampled by posterior × weight;
zero-weight warn pools form a second tier, and pacing-shed pools form the final
best-posterior tier. A zero weight does not itself cause shed. Auth, model and
manual holds retain their gates. Unknown/stale pacing retains native capacity
state; measured pacing can only worsen native state. Sampling remains limited to
`PACING_POOLS={"codex","claude"}` within identical policy tiers. Exploration,
non-pacing agents and existing policy-tier precedence remain unchanged.

## Authoritative sources and precedence

`src/pacing_usage.py:usage` selects the freshest timestamped weekly observation;
`usage_snapshot` shares that reading with marker recovery and integration. The
report uses the same integration, source labels and staleness constants.

| Pool | First-class observations | Weekly boundary |
| --- | --- | --- |
| Codex | `~/.codex/orchestrator/usage-poll/codex-usage.json`, source `codex-app-server:account/rateLimits/read`; session rollout `rate_limits.primary`, source `codex-rollout:rate_limits.primary` | `primary.resets_at`; T0 = T1 − seven days |
| Claude | `usage-poll/claude-usage.json` all-models weekly bucket, source `claude-cli:/usage`; desktop `~/Library/Application Support/Claude/plan-usage-history.json` `samples[].u.sd`, source `claude-desktop:plan-usage-history.json:u.sd` | Poll `weekly.resets_at`; T0 = T1 − seven days. A bank reset does not move this weekly boundary. |

The original `ts`, rollout event `timestamp`, or desktop `samples[].t` (milliseconds)
controls freshness, never file mtime. Polls win ties. Claude's poll boundary is
authoritative even when its textual minute jitters. No UTC minute is hard-coded.
After an observed boundary passes, roll T1 forward by whole seven-day weeks,
printing old T1, rolled T1 and poll age. The old period percentage becomes
unmeasured; only a new-period poll or desktop reading can supply usage.
A newer desktop percentage uses the observed poll boundary; without an observed
boundary (or explicit `claude_period_end` calibration), pacing is unknown.
Other Claude weekly buckets are printed with percentages and reset epochs but do
not drive state. A higher model-specific bucket generates an FYI naming it;
dispatch uses Sonnet, so this is informational.

A fresh post-incident observation clears a dated quota/rate-limit/capacity marker
only when **both** weekly and short-window usage are below 1. Short windows are
Claude poll `session.used_percent` (five hours), Codex `secondary.used_percent`
with `window_minutes=300`. Rollout secondary and desktop `u.fh` remain fallback
evidence. The short-window timestamp is checked independently: a newer weekly
reading cannot freshen an older short-window reading. Unknown/full/stale short
windows preserve the marker, as do unreadable/manual/auth/model holds.
Diagnostics give marker age, both usage quantities and the fresh-evidence or
cooldown drain condition. A fresh Codex poll also prevents a historical hand-file
consumption timestamp from bypassing this rule.

The session scan keeps the newest 300 stat-able files, reporting total, omitted,
unreadable and vanished counts. Files removed between glob, stat and open are
skipped; remaining evidence survives. Vanished files are included in unreadable
counts and separately named. A private, disposable cache stores sanitized usage
observations and validated append offsets. Inode/size/mtime and boundary hashes
detect replacement/truncation; incomplete lines are retried. Cache failure costs
speed only. Every scan prints duration, files read/cached, bytes processed and
malformed counts. The scan is shared once per capacity build.

Fallback: agent-specific `~/.codex/usage-snapshots/weekly-*.json` with a calibrated
per-pool cap. Mixed-agent totals are rejected, and calendar-week starts must match
the pool's actual period. `capacity.py calibrate-codex` needs at least two distinct
quota incidents with agent-specific weekly totals and period starts (or matching
source-labelled samples). It refuses to guess a cap. A 429 after a fresh model
said ok records a calibration miss; only measured corrections lower a known cap.

## Poll health, inventory and coverage

`pacing.POLL_TICK_SECONDS=600`, `POLL_STALE_TICKS=3` and
`STALE_AFTER_SECONDS=POLL_TICK_SECONDS*POLL_STALE_TICKS` define the single poll
window. Age ≤ 1800 seconds is fresh; age > 1800 is stale. The existing capacity
recording cadence (`TICK_SECONDS=900`) is separate and unchanged. Both tick FYIs
and `capacity.py pacing-report` show each poller's last-ok timestamp, age,
`ticks missed=age/600`, and the three-tick/1800-second allowance. Missing/invalid
polls also report unhealthy. A stale poll prints:

```text
POLLER STALE: claude age=1801.0s — launchd com.stranske.usage-poll; allowance=3 ticks/1800s; drain: successful scheduled poll
```

The consumer tolerates failed/time-out Claude polls. Pollers leave the latest
file untouched on failure, preserving last-ok age. A fresh alternative source
may still measure pacing; unhealthy polling remains visible independently.

When the Codex poll is fresh, available, unexpired `reset_credits` entries **are**
the inventory. Count and expiries come from those entries, not an inferred debit
or the summary count. Identifiers are discarded before pacing evidence is
recorded or printed. Missing credit inventory is unknown, including when weekly
usage is measurable. No reset-credit consumption RPC is ever sent.
`~/.codex/orchestrator/capacity-resets.json` is a fallback only when polling is
stale/absent. Without either inventory, report `resets=unknown` and apply the
conservative reserve. Both poll and hand-file counts are printed when polling is
fresh, along with the selected expiries.

Before this follow-up, the hand file reported one reset after incorrectly treating
the October 17 plan rollover as credit consumption. The live poll reports **two**
available resets, expiring **2026-10-29T18:47:16Z** and **2026-11-06T23:19:56Z**.
Fresh polling disables hand-file auto-debits entirely: a reset-epoch jump and
usage drop with unchanged credits cannot consume one. When falling back, the
existing idempotent oldest-expiry accounting remains: both a usage drop and a
T1 advance greater than a capacity tick, before the scheduled rollover, are
required. Signals (epoch jump or percentage drop) and inferred consumptions are
counted separately. Consumed entries remain for audit; `observed_at` excludes
older events. This PR does not rewrite runtime inventory.

`pacing-report` retains pass numbers **1–3** and the evidence classes:

1. Usage at completed T1 within the fresh-source allowance and reserve bounds.
2. Incidents while another pool was ok, with actual poll coverage and ledger evidence.
3. Non-Codex dispatch share in Codex ok versus warn/shed hours (target ≥2×),
   counting positive-count `event=start` records.

Passes 2 and 3 call **one shared coverage counter** for the same latest completed
pool period: at each 600-second slot time, the poll in force for **each** provider
must be no more than `STALE_AFTER_SECONDS` (1800 seconds) old. Fresh capacity-state
evidence for both pools is also required. A pre-period poll may cover the first slot;
a future poll cannot cover an earlier slot. Jitter and a healthy 650-second cadence
therefore do not create artificial misses. The longest poll gap must also be no
more than 1800 seconds: an outage over that allowance leaves at least one slot
uncovered even when it falls between the sampled ticks.
History comes from `usage-poll/{codex,claude}-usage.ndjson` plus latest files;
duplicate timestamps add no new evidence, and recording capacity snapshots
cannot manufacture poll coverage. A full week needs 1008/1008 slots. Incident
classification and dispatch-state evidence still require fresh capacity snapshots;
poll coverage alone never establishes a pass. Reports print covered/expected ticks
and longest poll gap versus the 1800-second allowance, uncovered slots, a drain
condition, and observed counts even when incomplete. Missing ledgers and missing populations
stay unmeasured; zero share in both populations is never a vacuous PASS.
The first requested check is **2026-10-18 05:00Z**, using the observed poll T1.

The per-agent `pacing` contract consumed by #676 keeps `state`, `used_fraction`,
`allowed_fraction`, `period_end`, `resets_available`, and extras including
`burn_per_day`. `measurement=measured|unknown` and reset expiries are additive. Poller health
stays in tick/report FYIs so changing age cannot republish identical routing exports. State remains the existing ok/warn/shed/unknown enum. Reasons print
blocking quantities and a drain condition/reset horizon; sampling and expiry
handling continue while shed.

## Scheduled installation

The repository record of truth is [`scripts/usage_poll/`](../scripts/usage_poll/).
Install **all** Python files (including `usage_history.py`) and the shell wrapper
on **local disk** at `~/.codex/bin`;
launchd cannot execute them from CloudStorage. Render the plist's `__HOME__`
placeholder to the absolute home directory and install it at
`~/Library/LaunchAgents/com.stranske.usage-poll.plist`. Its label is
`com.stranske.usage-poll`, `StartInterval=600`, with `RunAtLoad=true`. Create
`~/.codex/orchestrator/usage-poll` for stdout/stderr logs before loading it.

The wrapper explicitly uses `/opt/anaconda3/bin/python3`, or an explicitly selected
`ORCH_USAGE_POLL_PYTHON` venv interpreter, and fails if unavailable. It never falls
back to Apple's Python. It attempts both providers even when one fails and returns
non-zero for any failure. Pollers publish latest via atomic replacement only after
successful command, parse and history append. Errors do not echo raw provider
responses. Codex sends only initialize, initialized notification, and
`account/rateLimits/read`; Claude invokes the local `claude -p /usage` command.
Claude makes up to three attempts, three seconds apart, with 40 seconds per try
(`CLAUDE_USAGE_POLL_TIMEOUT` may explicitly override the per-try timeout). Each
attempt uses `/tmp`, closed stdin and a new process group; timeout kills the whole
group. A non-subscription “Total cost” view or missing/invalid weekly line retries.
Exhaustion reports every try with sanitized diagnostics and preserves latest.
The plist deliberately omits `ProcessType=Background` and `LowPriorityIO` to match
the live unthrottled setup; this does not prove the launchd timeout is resolved.

Primary Codex and all-models Claude fields remain strict. Invalid Codex secondary,
Claude session or other weekly buckets become null with an output `warnings` list.
Codex output retains only allowlisted usage and credit status/expiry fields.

Each poller atomically caps its ndjson history to 14 days plus one stale allowance
(to retain the latest completed seven-day period and its boundary poll throughout
the next period), at most 4096 lines and 8 MiB. It prints
`rotated_out` and retained line counts on every append, including zero rotations.
Routing reads only the last `POLL_HISTORY_TAIL_BYTES` (2 MiB) of Codex history and
prints omitted byte counts when limited; completed-period coverage uses the
retained history. A cap can remove evidence, so incomplete periods remain
unmeasured and reports show the missing quantities.
Parser tests use fixtures and mocked failures; they do not call either provider.

This PR records the already-running pollers; it does not reload launchd or change
macOS approvals. The coordinator owns the reported Claude launchd timeout.
The requested checkpoint under `~/.codex/orchestrator/briefs/` is outside this
seat's writable sandbox; the append was denied. The fallback checkpoint is
`/private/tmp/pacing-followup-codex-CHECKPOINT.md`. Source merge, runtime deployment,
healthy polling, relay consumption and completed-period passes remain separate
verification boundaries.
