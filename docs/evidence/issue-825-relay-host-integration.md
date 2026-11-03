# Issue #825 — relay host integration evidence

Repository work for explicit relay decision identity landed in PR #827 (`419c166`).
This note records the closer repair: host `handoff-relay.sh` integration and validation
artifacts. Host scripts are **not** committed in this repository.

## Host deployment (outside git)

- **Script:** `~/.codex/bin/handoff-relay.sh` (coordinator-owned; no Workflows sync source)
- **Change:** In `choose_receiver`, replaced hand-formatted `relay-pacing.log` lines with
  `rate_incidents.py append-relay-pacing-decision` via `mirror_reader` (same transport as
  `relay-runner.sh` / `record-lane-round`). On failure, `decision_id` stays empty and no legacy
  pacing line is written.
- **Deployed (UTC):** 2026-11-03T08:16:32Z (verified by dry-run append:
  `decision_id=20261103T081632Z-16936-3a24266f` on `lane=opener`).
- **Token path:** `RELAY_DECISION_ID` in relay request JSON → `relay-runner.sh`
  `record-lane-round --relay-decision-id` (unchanged from #827 wiring).

## Repository review (#827)

Inspected `src/rate_incidents.py` (`append_relay_pacing_decision`, CLI),
`src/feedback.py` (explicit ID join before legacy adjacency), and
`tests/test_relay_pacing_feedback.py`. No demonstrated test failures; no production code
changes in this repair PR.

## `joined` counter semantics

`record-lane-round` receipt field `relay_pacing.joined` is a **cumulative** importer total
after each ingest, not a per-round delta. Example from `relay.log` (2026-11-03):

```text
decision_id=20261103T071528Z-86487-1acb69bb ... RELAY exit=75 ...
  lane-round: {..., "relay_pacing": {"decisions": 484, "joined": 435, ...}}
```

Compare successive receipts by difference if a per-round join is needed.

## Production / operational acceptance

| Window | Evidence |
|--------|----------|
| Pre-deploy fleet | `$ORCH_STATE_DIR/switch-review.md` row: `windowed-capacity-policy: ... acted_on 379; outcome 379` |
| Post-deploy (append CLI) | **Pending** — needs a live relay round after deployment with matching `decision_id` in `relay-exits.log`, pacing log line, and `lane-round:` receipt (no fixture traffic). |

Do not treat dry-run pacing lines as production proof.
