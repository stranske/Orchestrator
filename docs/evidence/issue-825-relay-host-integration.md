# Issue #825 — relay host integration evidence

Outcome 1: **0 confirmed post-deployment decision/exit joins** at the
2026-11-03T11:12:08Z check. Operational acceptance remains pending.
The proving command is `/opt/anaconda3/bin/python3 src/switch_review.py --text-out PATH`,
together with read-only matching of `relay-pacing.log`, `relay-exits.log`, and
`routing_decisions_v2.causal_context_json.relay_decision_id` against accepted
`influence_edges`. The same post-deployment observation window must have
`windowed-capacity-policy` acted_on > 0 and outcome > 0.

PR #827 (`419c166ca9a86dbed2aeb699fea8ae2b352b8f2b`) remains on main.
This repair changes evidence only; no demonstrated repository code failure was found.

## Host deployment, separate from repository delivery

The earlier repair recorded deployment at **2026-11-03T08:16:32Z**, with dry-run
token `20261103T081632Z-16936-3a24266f`. That token is not production proof.
Current readback confirms the integration is installed; this check did not deploy
or mutate host scripts. A repository merge does not deploy them.

`~/.codex/bin/handoff-relay.sh` is a regular host file, outside this repository.
It is not in a Git repository at its installed location. The inspected mirror
builder does not install it; `docs/LANE_ROUND_FEEDBACK.md` identifies the host
boundary. No separate tracked owning source was found in the repository or the
inspected host scripts.

In `choose_receiver`, the installed script calls `append-relay-pacing-decision`
through `mirror_reader`, replacing the pacing-log writer. Its returned token goes
through `RELAY_DECISION_ID` into request JSON `decision_id`. `relay-runner.sh`
decodes that field unchanged, prints it in the exit trailer, and supplies
`--relay-decision-id` only when nonempty. Append failure sets the token to empty
and emits no legacy fallback line. Both scripts pass `/bin/bash -n`.

Readback hashes (SHA-256):

```text
410e85d778e344c4e918b24ac94362e7dd6a35e6759ed39f274f3784252ed52f  handoff-relay.sh
10a262322b14c707bc40f2776da4a0ff94c2c29039402f3b97a352e09f97b2ae  relay-runner.sh
```

The relay file's mtime is 2026-11-03T08:16:20.032245Z. An mtime and the earlier
deployment note do not independently prove the exact deployment operation.

## Repository review and focused validation

Reviewed the merged append/generator and `record-lane-round` CLI changes, current
implementations, explicit-ID matching, conservative legacy adjacency and backlog
retirement in `src/feedback.py`, and `tests/test_relay_pacing_feedback.py`.
Explicit records do not fall back to adjacency; mismatched agents/lanes, future
decisions, and duplicate rounds do not earn credit. Legacy matching requires a
single decision strictly between unique exits. No code change is justified by
the focused result:

```text
$ /opt/anaconda3/bin/python3 -m pytest tests/test_relay_pacing_feedback.py -q
........................................................................ [ 88%]
.........                                                                [100%]
81 passed in 7.14s
```

The branch was rebased onto current origin/main before this check:

```text
origin/main=6800ac32408cb963efc765b466a093e895e74002
byte_identical=True
local_sha256=60a3d188d9090283176a40be51218b5bcc31b698fd463644ca73782d7a95ecfc
origin_main_sha256=60a3d188d9090283176a40be51218b5bcc31b698fd463644ca73782d7a95ecfc
```

The comparison used `git show origin/main:.verify-floor.json` as bytes and
`Path('.verify-floor.json').read_bytes()`, requiring equality before hashing.
The floor is unchanged.

## Counter semantics and production window

`relay_pacing.joined` is **cumulative**, not incremental: the importer initializes
it from existing accepted bindings intersected with persisted decisions, then
increments for newly bound rounds. Reread tests preserve its value. A positive
total alone cannot establish a new post-deployment join.

Window checked: **2026-11-03T08:16:32Z through 2026-11-03T11:12:08Z**.
The pacing log has the known dry-run token and three subsequent real decisions:

| Decision UTC | Token | Chosen / lane | Matching completed exit |
| --- | --- | --- | --- |
| 09:43:55 | `20261103T094355Z-8599-21b8716d` | claude_code / opener | absent |
| 09:50:55 | `20261103T095055Z-11889-3db4702f` | codex / opener | absent |
| 11:07:13 | `20261103T110713Z-48058-646615a5` | codex / closer | absent |

Across those four lines there are **0 duplicate tokens and 0 legacy lines**.
The live Brain has no accepted influence edge for any of the three real tokens.
At capture, the 09:50 request was in progress and the 11:07 request was queued;
neither was restarted or disturbed.

The only exit in the observation window is:

```text
=== 2026-11-03T08:16:46Z RELAY exit=78 agent=cursor lane=closer duration_s=35 decision_id=20261103T081308Z-44872-c7548588 ===
```

It matches persisted round `lane-round:7e446e4f2a318804b96a7c9e` and an accepted
edge, but its decision was at **08:13:08, before deployment**. It cannot prove the
new append writer. No `lane-round:` receipt for that exit remains in the current
`relay.log`; the persisted row and separate exit log are the available evidence.

## Outstanding acceptance

- Obtain a completed real exit for a post-deployment token, its hook receipt,
  and the accepted Brain edge, without replaying production hooks or fixtures.
- Obtain passing `src/verify.py` output and fresh switch-review text output.
- Require the capacity row and joined decisions in the same observation window.
  Earlier fleet totals are not acceptance for this deployment.
- The recorded deployment's 24-hour deadline is **2026-11-04T08:16:32Z**.
  If it passes without attributable evidence, record the criterion as unmet.

Blocking/drainable pair: acceptance blocked by **0 completed post-deployment
joins**; **3 real decisions** can drain through their existing receiver/exit path.
No writer or lane lock is released to clear this measurement.

## Verification and report limitations from this seat

`/opt/anaconda3/bin/python3 src/verify.py` was launched with a 600-second
process-group timeout. It had not emitted output at the documentation capture.
A second invocation, selecting absent private source paths through
`ORCH_FEEDBACK_DB` and `ORCH_CAPABILITIES_PATH`, returned:

```text
verify_empty_state_exit=124 elapsed_s=120.0
```

Its output file was empty. This is not a passing verifier result, and no cause
such as provider contention is inferred from silence. No floor change was made.

The `switch_review.py` CLI was invoked through `main` with the documented
`--text-out /private/tmp/issue825-check/switch-review.md` syntax. A runtime
registry filter excluded the owner-excluded repository before discovery;
production decisions, exits, and value-chain inputs were not replaced.
The first call exited 1 before writing a report:

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capabilities.json.lock'
```

A retry used a private byte copy of the real capability ledger, preserving its
production data and the live Brain input. It returned `switch_snapshot_exit=124`
after 120 seconds, without producing a text report. Existing fleet report totals
are deliberately not substituted for the requested fresh observation window.
