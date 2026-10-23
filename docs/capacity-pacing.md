# Capacity pacing (#668)

`src/pacing.py` owns the seven-day period, six-hour burn window, reserve, reset
relief, early-spend band and all three pass bounds. Math has no I/O; clocks are
explicit. A burn estimate requires three distinct timestamps spanning at least
one hour from the same source and period. Measured zero burn is distinct from
undefined. Retain the latest sample at or before the six-hour cutoff as an
anchor, plus the in-window observations, provided the anchor is no older than
`BURN_WINDOW_HOURS*3600 + 2*SAMPLE_TICK_SECONDS` (28,800 seconds, eight hours).
`SAMPLE_TICK_SECONDS = 3600`: the orchestrator tick records one pacing sample
per hour, with additional poll-driven snapshots. Two sample ticks allow for
normal sampling cadence around the six-hour cutoff; this allowance is separate
from the poll freshness threshold.
An older anchor is ignored and prints `anchor ignored: age=<s>s > bound=<s>s`;
span then starts at the oldest in-window observation. Compute interval rates from
their actual endpoints and clip each interval's exponential weight to the window. The observed
span is `min(6h, newest - anchor)`, so ordinary clock jitter can reach full
confidence without a sample landing exactly six hours before the newest one.
Negative usage jumps restart history; previous-period and other-source samples
cannot supply an anchor.

Confidence is `c = min(1, span_h/6)`. The damped projection is
`ũ_eff = u + c * burn_per_day * remaining_days`. Until the minimum sample rule
and full confidence are met, the unchanged schedule plus early-spend band decides
state. At full confidence the projection tier can reach shed and later print
`pool <p>: ũ=<x> ≤ 1−r_eff → ok` on recovery from either warn or shed.
This includes the normal shed → warn → ok path, with one drained line when
full-confidence pacing returns to ok. Fresh flat observations drain old
spend without another dispatch. `test_real_111_tick_replay_sheds_and_prints_drained_recovery`
replays the first 111 recorded ticks per pool (the live file had grown to 112
when captured), retaining only timestamps, usage fractions and source labels.
The replay uses observed weekly boundaries and an empty reset bank to exercise
projection shedding independently of reset relief. It prints blocking quantities
from that replay and an explicitly simulated consecutive-tick
flat-usage recovery; this is reachability evidence, not observed live recovery.

Routing retains the merged sustainable-rate/effective-burn ratio and ±0.25 tick
hysteresis. Stale or unmeasured rows retain the previous weight and its history
boundaries; only a confirmed period change or reset expires that carry. The
literal projection-based spec formula is exposed only as a pure comparison
function with direct endpoint/monotonicity tests; it is not wired into routing.

The reserve formula is `0.15 * max(0, 1 - 0.5*n)`: zero/one/two resets give
0.15/0.075/0. Positive routing weights are sampled by posterior × weight;
zero-weight warn pools form a second tier, and pacing-shed pools form the final
best-posterior tier. A zero weight does not itself cause shed. Auth, model and
manual holds retain their gates. Unknown/stale pacing retains native capacity
state; measured pacing can only worsen native state. Sampling remains limited to `PACING_POOLS={"codex","claude"}` within identical
policy tiers. When a non-pool agent wins the rank (Cursor-top relay, or
Cursor-ok beside Codex warn/shed) but a measured pool peer is present, the
winner is kept and a pacing receipt records real weights at neutral 1.0 for the
unmeasured agent — instead of aborting to legacy (`weights=*:unknown` on almost
every #812 relay). Exploration, non-pacing selection shares and policy-tier
precedence remain unchanged.

## Authoritative sources and precedence

`src/pacing_usage.py:usage` selects the freshest timestamped weekly observation;
`usage_snapshot` shares that reading with marker recovery and integration. The
report uses the same integration, source labels and staleness constants. It reads
one persisted `~/.codex/handoff/capacity.json` snapshot through the tick's loader
and passes that history to integration without writing capacity or sample ledgers.
Each pool prints `routed_weight=0.451 (tick ts=..., age=...s)` from that snapshot
and `report_weight=...` computed at report time. Missing persisted weights print
`routed_weight=unmeasured`; missing tick timestamps/ages also stay unmeasured.
`weight_delta` is report minus routed weight, shown only when its magnitude exceeds
0.01. The `w_p_prev→w_p` diagnostic describes this report's hysteresis step from
the persisted weight, rather than the prior tick's step, with both weights printed
to three decimals (stored diagnostics retain full precision). Without a persisted
prior it prints `unmeasured→<computed> (hypothetical step from an unpersisted default)`.
For a shed pool, the report explains that weight is informational: the router uses
the state tier, and shed
pools receive work only when no ok/warn pool can take the task.

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
This rollout-scan cache is a pre-existing write in the report path: it uses an
atomic replace in the temporary directory and is not a handoff file.

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
   counting positive-count `event=start` records. Codex **warn** hours count as
   constrained even when its routing weight remains high. That combination can
   make pass 3 hard to satisfy; a high weight does not exempt warn hours.

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
Install **all** Python files (including `usage_history.py`), `usage-poll.sh`, and
`py-standard.sh`
on **local disk** at `~/.codex/bin`;
launchd cannot execute them from CloudStorage. Render the plist's `__HOME__`
placeholder to the absolute home directory and install it at
`~/Library/LaunchAgents/com.stranske.usage-poll.plist`. Its label is
`com.stranske.usage-poll`, `StartInterval=600`, with `RunAtLoad=true`. Create
`~/.codex/orchestrator/usage-poll` for stdout/stderr logs before loading it.

The wrapper sources `~/.codex/bin/py-standard.sh`: the owner standard is Python
3.14 (uv-managed or Homebrew), with 3.13 as backup. It refuses to fall back to
macOS system 3.9 or the Anaconda 3.12 base. `ORCH_STANDARD_PYTHON` selects an
override only if it reports 3.13 or 3.14; other versions are ignored. The existing
`ORCH_USAGE_POLL_PYTHON` explicit venv selection remains supported only for
3.13/3.14; unsupported or unavailable legacy overrides are ignored with a
diagnostic. If no standard interpreter is available, the wrapper fails explicitly.

The Claude poll owns a dedicated CLI login. The owner signs in once:

```bash
CLAUDE_CONFIG_DIR=~/.codex/claude-poll claude auth login --email <owner>
```

For the Claude poll only, the wrapper unsets `CLAUDE_CODE_OAUTH_TOKEN` and
`CLAUDECODE` and sets `CLAUDE_CONFIG_DIR` to `$HOME/.codex/claude-poll`.
`CLAUDE_POLL_CONFIG_DIR` overrides that directory; use the same directory for
the one-off login. Codex's environment is unchanged. The setup-token is not used:
user-filed claude-code issues #24200, #22450 and #28935 report that it is
inference-only and lacks the `user:profile` scope needed by `/usage` (request
#81015). Issues #54443 and #88583 report refresh-token rotation races and
credential wipes when the default login is shared with other sessions. These are
user reports, not official authentication guarantees; the dedicated directory
isolates the poller's login from the desktop app and other CLI sessions.
At the CLI invocation boundary, `claude-usage-poll.py` also removes inherited
`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` so API/gateway authentication cannot
override the subscription login. See [Anthropic's API-key guidance](https://support.claude.com/en/articles/12304248-manage-api-key-environment-variables-in-claude-code).

If this login lapses, the poll goes stale and the stale alert fires
(`usage-poll: claude STALE`). Recover with one re-login in the same directory.
The seven-day check-in passes when Claude poll age is at most 30 minutes on at
least 95% of ticks over seven days; this is an acceptance target, not a measured
result of installing these files.

`prune-claude-keychain-slots.py` is an optional manual maintenance helper, dry-run
by default. It reads item metadata only and always keeps the default credential
slot, the default poller slot and any `CLAUDE_POLL_CONFIG_DIR` override's slot.
`--apply` deletes only other slots last written more than 30 days ago
(`--days` may increase that threshold); `--keep` preserves
additional slot suffixes. Deleting a slot requires that config directory to log
in again. Selection uses modification metadata, not evidence of last use.
Deletions are restricted to the inspected `login.keychain-db`.
Review the dry-run selection before applying it, using the selected Python:

```bash
source ~/.codex/bin/py-standard.sh
"$STANDARD_PYTHON" ~/.codex/bin/prune-claude-keychain-slots.py
```

The wrapper attempts both providers even when one fails and returns
non-zero for any failure. After both polls it checks each latest file's own `ts`.
The wrapper defines `STALE_AFTER_SECONDS=1800` once, mirroring
`pacing.STALE_AFTER_SECONDS` with a parity test. Missing/invalid timestamps are
stale with `age=unknown`. Stale alerts append to
`${HANDOFF_ALERT_LOG:-$HOME/.codex/handoff/alerts.log}`, at most once per poller
per six hours during an outage. A successful poll with a fresh file appends one
`RECOVERED` transition. Age, allowance and a controlled, sanitized failure message
(first 120 characters) accompany stale alerts; unrecognized provider diagnostics
are redacted. These notifications feed the weekly lane-alerts digest as FYI and
never add a routing or blocking gate. Install the new `usage_alerts.py` helper
alongside the wrapper when deploying this change.

Pollers publish latest via atomic replacement only after
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

## Capacity publication cadence

The route-weights export attempts publication hourly; `relearn` remains weekly.
The owner switch `ORCH_ROUTE_WEIGHTS_PUBLISH=1` is still required. Worsening and
recovery states each need two consecutive local tick observations before the
published capacity row changes. The second tick drains the hold; changing state,
missing/stale input or a gap over two hours resets it. Repeated reads of the same
snapshot do not count as another tick. The tick records observations independently
of publication backoff and prints `local`, `published` and `seen=<n>/2` when held.

Read-only check: `python3 src/route_weights_export.py --publish-lag`. Each pool prints
`pool <p>: local=<state> published=<state> published_age=<s>s lag=<ok|STALE|held(n/2)>`.
`published_age` measures the artifact `generated_at`; `observed_age` additionally labels the
pool observation age. Mismatches beyond `PUBLISH_LAG_STALE_S=7200` seconds are stale; a fresh
first tick prints `held(1/2)`. Unmeasured ages print `unmeasured` without a seconds suffix.
`all pools in sync` appears only when every state matches; failed remote reads show
`published=unmeasured`. Observation age alone is not lag. `window` is static configuration
and is a silent trigger: a window change waits for the next meaningful trigger or the
24 h refresh floor. Immediate capacity triggers are the damped `state`,
`period_end` moves over `PERIOD_END_TOLERANCE_S=3600` seconds against the published baseline,
and `resets_available`; existing non-capacity triggers such as learned weights
remain immediate. Usage fractions, routing weights, period starts, reset expiries, reserve,
measurement/source/staleness and burn diagnostics remain published but cannot trigger a push.
The refresh floor is 24 h for non-state diagnostic diffs (`PUBLISH_REFRESH_FLOOR_S`).
Comparative/lambda drift and every `comparative_*` ranking/reserve row field do not trigger
a push; they ship with the next trigger or refresh. A refresh carries diagnostics once the
published `generated_at` is over 24 hours old, or its age is unmeasured, keeping the artifact
inside consumers' 14-day freshness limit when ticks/publication succeed. Unchanged and refresh
logs print `published_age=<s>/floor=<s>`, including an explicit `refresh` marker when due.
Worst-case published `used_fraction`/`observed_at` staleness is about 25 h (24 h plus one tick)
without another trigger. The 2 h bound for a held pool is persistent state-change propagation,
not absolute diagnostic age: the retained row may already be old, and repeated state flaps
can extend that age. An accepted state push carries fresh numbers. Workflows reads only
capacity state; gating on `observed_at` freshness would be misled.
This query makes no persistent state or Git-ref writes.
Check date: 2026-10-19 after one day of production ticks. Successful hourly ticks
should propagate persistent state changes within two hours; failures/backoff and
disabled publication can extend that bound.

Read-only production count: `python3 src/route_weights_export.py --publish-history 24h`.
It reads all pages of the `exports/route-weights` commit history since now minus 24 h via
`gh api repos/stranske/Orchestrator/commits?sha=exports/route-weights&since=<UTC>` and prints
`commits_24h=<n> (state flips local: see log) lag=<ok|STALE|held(n/2)|unmeasured>` after
the per-pool lag details. A failed history fetch prints `commits_24h=unmeasured`; it never
publishes or updates persistent local state. The coordinator owns the 2026-10-20 check-in
registration after merge; compare the count with damped state flips in the tick log and
record other immediate triggers separately. Export start/end UTC times and duration in
that log make cadence skips measurable. The hourly due threshold is 30 minutes after the
previous success stamp, allowing for tick jitter and export duration.

## Relay decision identity and backlog (#825)

The coordinator-owned relay must emit a unique token for each pacing decision and pass
it through to the exit hook. Prefer
`python3 "$ORCH/rate_incidents.py" append-relay-pacing-decision ...` (prints
`<utc>-<pid>-<random>`, appends `decision_id=<id>` to `relay-pacing.log`); or generate
with `generate-relay-decision-id` and write the field yourself. Pass the same value to
`python3 "$ORCH/rate_incidents.py" record-lane-round --relay-decision-id <id>`
alongside the existing agent, surface, lane, exit and output-file arguments. Tokens are
capped at 176 characters so the `relay-pacing:id:<token>` join key stays within the
192-character completion-identifier limit. This repository supplies the append/generator
CLIs, importer and record hook; the coordinator updates `~/.codex/bin/handoff-relay.sh`
after merge. Omit `--relay-decision-id` when generation fails rather than passing an empty
token.

The round persists the token as `relay_decision_id` in its routing record's
`causal_context_json`; the decision and outcome telemetry retain `decision_id`.
Exactly one round with that ID, the same lane and chosen agent, and an exit at or
after the decision earns an accepted influence edge. IDs disambiguate concurrent
lanes and same-second exits. Unknown or mismatched IDs earn no temporal fallback.
Legacy decisions and rounds without IDs retain the conservative serial-adjacency
join. Reimporting or rotating the log preserves settled bindings.

Unmatched pre-2026-10-20T11:58Z legacy decisions (no `decision_id`, still unbound
after explicit and adjacency join) are marked once as `status=unjoined` with
`unjoined_reason=no round recorded (pre-2026-10-20T11:58Z relay subshell bug)`,
including decisions that follow an earlier exit on the same lane. Decisions with
IDs and already joined decisions are preserved. The importer reports `unjoined`
separately from `joined`. These terminal orphans stop extending the legacy scan
and create neither outcome events nor acted-on credit. A pending modern decision
remains eligible after log rotation.

Acceptance after merge and the relay update: within 24 hours, inspect
`record-lane-round` / relay.log `lane-round:` output for `joined` matching the
decisions with recorded exits, and the `switch_review.py --text-out` monitor row
for `windowed-capacity-policy` with `acted_on > 0` and `outcome > 0`. This live
check is separate from the repository's offline tests.

## Relay monitor population (#812)

The `windowed-capacity-policy` row in `switch_review.py --text-out` measures relay
demand from validated, deduplicated decisions in `HANDOFF_PACING_LOG` (default
`~/.codex/handoff/relay-pacing.log`) over the same report window. This read is
independent of ingestion and joins: an eligible unjoined decision contributes to
demand. An absent or unreadable log leaves demand unmeasured; an empty valid
population is zero. The reader shares the importer's identity and validation rules.

Accepted, version-matched Brain decision events contribute to `usable`, including
saved decisions whose log has rotated. Accepted joins retain their existing
`acted_on` and terminal `outcome` counts. For these operational relay outcomes,
`success` counts joined rounds with both `status=completed` and `exit_status=0`;
failed, rate-limited and lane-busy (`exit=75`) rounds earn no operational success.
Durable PR delivery continues to use the existing lifecycle predicate on delivery
edges, and both contributors land in `outcome_success`. The row's `success_scope`
names that combined unit. No Brain outcome row, routing setting, or
durable-delivery verdict is synthesized from a lane exit. The printed
`relay pass (2026-10-27)` line grades version-exact joins against log demand
within ±5% (see `value_chain_monitor.relay_pass_number`); it stays `pending`
until that UTC date. Relay picks that include an unmeasured peer (Cursor) beside
a measured Codex/Claude pool keep the Cursor winner and attach a pacing receipt,
so decision weights reach the pacing log the weekly pass reads.
