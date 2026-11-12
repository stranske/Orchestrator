# Host keepalive round 6

Checked November 11, 2026 America/Chicago on `4a5032f241d3276fa4073e84da1592e263e39c00` after merging
`origin/main` at `f2d1bf8619ffb7ad21c81d227b3b3542d6adde75`.
Zero of T1, T4, and T5 were verified complete.

## T1

The 2026-11-12T00:40:04.907857+00:00 hourly observation still names Workflows
#4637 at 2026-11-11T16:59:47.763115+00:00. The closer was refused by the #4635
opener holder with PID 49989. The ledger records reclaim of that same holder
at 2026-11-11T17:14:49.572600+00:00. Both current exact-target claims and the
holder path are absent. The handoff closer dispatcher inventories and changes
file contain no matching reservation. The current opener registry entry for
#4635 records a start after the incident. Historical reservation and writer-lock
custody remain unknown.

The fix already shipped through #1344 (`21e48b27`). A private pytest hook
made `_stale_orphans` return no candidates. The existing regression then
failed with the live-PID refusal. Without the hook, eight focused cases passed,
covering reclaim, active and dirty holders, native holder claims, unknown claim
ownership, and fail-closed process probes. The demonstrated failure already
has a repair. This round added no reclaim changes.

## T4 and native witnesses

The floor byte comparison exited zero against the fetched base above. Both
blobs have SHA-256 `5f22aedf5f12b0e7dc65309d471f6ac6bb6d05aa93af40344e4a3f945ae2df83`. The keepalive prohibits running the full suite
locally, which the bare verifier invokes. The prior [hosted verifier
aggregate](https://github.com/stranske/Orchestrator/actions/runs/43132032891/job/130899719150)
passed and reported the activation audit OK using sharded inputs. The complete
hosted job log is retained at `/tmp/orch1363-host-round6.oksBNL/hosted-aggregate.log`.
The bare invocation and its machine-local result remain unverified.

With `PYTHON=/opt/anaconda3/bin/python3`, the Node witness tests passed 39 of
39. The direct same-holder Python test reached the disabled exit-128 witness
in each lane and failed closed during restored reclaim because native
`pgrep` could not enumerate processes in this sandbox. Earlier hosted
same-holder evidence is recorded in [keepalive-round4.md](../keepalive-round4.md).

## T5

The read-only audit against
`/Users/teacher/.codex/orchestrator-mirror.generation-9413fb3c95374d538c04f17e0d53b6ce`
exited 1 with 188 refusals (147 branch-held and 41 branch-exists). The active
segment set differed from the collector snapshot, and continuous seven-day
coverage was incomplete. The audit left the active and retained segments and
the collector observation byte-identical. The installed generation has different
reclaim-test bytes from the delivery. This was an audit of the incumbent
installation. T5 requires a fresh observation of the delivered generation from
the existing hourly collector.

The remote branch advanced during the round. The merge retained the bot's
collector-receipt null-entry fix at `d8563d25`. The audit log above comes from
the merged implementation, whose 51 auditor tests also passed.
