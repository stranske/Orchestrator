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
tail -n 40 "$relay_log" > "$tail"
python3 "$ORCH/rate_incidents.py" record-lane-round --agent "${other_agent/claude_code/claude}" --surface handoff-relay --lane "$next_lane" --exit "$status" --output-file "$tail"
```

Set `$tail` to a private temporary file and remove it after the call. Pass
`--receiver-reason "$reason"` when the relay has retained its actual routing
reason; otherwise the stored value is NULL. The optional `--ts` accepts a UTC
epoch second for replay or testing.

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
