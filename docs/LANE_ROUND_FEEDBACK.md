# Relay round feedback

`rate_incidents.py record-lane-round` records a completed opener or closer
receiver in the existing `routing_decisions_v2` table. A `lane_round` row has
the observed agent, lane, exit status, error class, timestamp and optional
receiver reason. Profile selection fields remain NULL; the profile learner
counts only `profile_selection` rows.

The relay hook belongs in `~/.codex/bin/handoff-relay.sh`, after the receiver
process exits and before it prints the `RELAY exit=` trailer. That script is
outside this repository and is not changed by this PR. With `$relay_log`,
`$status`, `$next_lane`, `$other_agent` and `$ORCH` set by the relay, the hook is:

```bash
# At decision time (before dispatch): emit decision_id=<id> on the pacing log line.
# Invoke the script by path (same as record-lane-round) so $ORCH need not be on PYTHONPATH.
# On failure leave decision_id empty and omit --relay-decision-id rather than aborting.
decision_id="$(python3 "$ORCH/rate_incidents.py" generate-relay-decision-id)" || decision_id=""
# After the receiver exits:
tail -n 40 "$relay_log" > "$tail"
python3 "$ORCH/rate_incidents.py" record-lane-round \
  --agent "${other_agent/claude_code/claude}" --surface handoff-relay --lane "$next_lane" \
  --exit "$status" --output-file "$tail" \
  ${decision_id:+--relay-decision-id "$decision_id"}
```

Set `$tail` to a private temporary file and remove it after the call. Pass
`--receiver-reason "$reason"` when the relay has retained its actual routing
reason; otherwise the stored value is NULL. The optional `--ts` accepts a UTC
epoch second for replay or testing. Prefer `--relay-decision-id` so interleaved
opener/closer rounds join by explicit ID; omit it when generation failed or for
legacy lines (empty tokens are rejected and would drop the round).

Every exit writes a decision row. Exit zero writes no incident, even if a
successful summary quotes a rate-limit message. A nonzero exit creates an
incident and capacity shed only when the tail contains an authoritative quota,
credit or rate-limit error. Other failures record `error_class=unknown` and do
not shed. The CLI prints the decision ID, error class, incident ID if any, and
whether a shed marker was written. Invalid or absent output files fail closed.

The `--surface` value is recorded on authoritative capacity incidents; the
relay uses `handoff-relay`. For a `try again at Sep 19th, 2026 3:11 AM` message without a timezone, the
clock is interpreted in the relay host's local timezone. UTC and numeric UTC
offset suffixes and full month names are accepted. If the time is missing, malformed or in the
past, the existing six-hour cooldown applies. A later valid reset time extends
the shed marker through that time. Operators should configure the relay host's
timezone to match the provider display timezone.


## Relay pacing consumption (#812)

The existing exit hook now also reads `HANDOFF_PACING_LOG`, defaulting to
`$HANDOFF_DIR/relay-pacing.log` (`~/.codex/handoff/relay-pacing.log` without an override).
After merge and mirror sync, no additional launcher installation is required.
`feedback.ingest_relay_pacing(path, now=epoch)` also permits offline replays into
an explicitly selected `ORCH_FEEDBACK_DB`. Do not run replays against the production Brain.

The capability owner is **windowed-capacity-policy**; the value-chain row printed by
`switch_review.py --text-out PATH` is its only grading surface. No capability is added.
Each valid log line is a sanitized decision event with source, chosen, candidates,
states, weights, lane and boolean fallback, including `fallback=yes`. Identical log
lines share a content-derived ID. Only a single decision between successive observed
exits in that lane, with a matching chosen agent, earns an accepted edge to the
lane-round ID. Codex, Claude (`claude_code` in logs), Cursor and Muse are supported.
Multiple decisions, timestamp ties, a changed receiver, or an absent round never earn
credit. Binding is one-to-one even when an older round arrives late. The round hook
commits the lane-round row first; the importer then writes events and edges in its
own transaction. Rereading does not add counts or change the decision's original window.
Settled events are not rewritten. Pending decisions survive log rotation, and only
rounds since the oldest unresolved decision are matched, preserving the preceding
exit boundary. Sorted timestamp lookup avoids quadratic history scans under the
Brain write lock.

The target completion event records `completed` (exit zero), `rate-limited`
(nonzero plus authoritative capacity evidence), or `failed` (other nonzero).
These observed terminal results count as `outcome` in the capacity row; they are not
PR acceptance or durable-delivery evidence and do not increment `outcome_success`.
Import errors are returned as `relay_pacing.status=unavailable` in the hook receipt;
provider-incident recording still runs. Missing logs report `absent`.

For the 2026-10-27 check, compare decision timestamps in the same UTC window against
this row's accepted edges (the normal monitor window is 90 days), retaining any
other capacity-consumer edges separately when comparing to relay log counts.
Inspect `completion_events` with producer `feedback.relay_pacing` for unjoined
decisions and chosen-agent outcome payloads. A fixture pass does not establish
one-day production consumption or the weekly +/-5% acceptance criterion.
