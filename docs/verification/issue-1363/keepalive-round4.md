# Keepalive round 4 — November 11, 2026

Hosted CI at `5756ff2649d9d6008f17fc8f98df4cf2cd88e47b` recorded
`test_native_reclaim_disabled_exit_128_names_holder[closer]` and
`test_native_reclaim_disabled_exit_128_names_holder[opener]` as passed in
`pytest-shard-6/shard-6.xml` and `mirror-shard-6/shard-6.json`. Both cases
require exit 128 and an unchanged holder inode, HEAD, and branch during the
disabled attempt, followed by native reclaim of that holder after restoring
the real function. [Hosted run and shard artifacts](https://github.com/stranske/Orchestrator/actions/runs/43128530236)

The hourly collector observation at `2026-11-12T00:40:04.907857Z` still
names Workflows #4637 as the newest refusal. The local-loop log records a
Vibe start error for the closer, naming the #4635 opener holder and PID
49989. The provision ledger records that same holder reclaimed at
`2026-11-11T17:14:49.572600Z`, followed by a #4637 dispatch at 17:14:52Z.
Both current claims and the old holder path are absent. The refusal-time
reservation and writer-lock owner remain unknown. The stale-orphan fix
from #1344 and its regression are already present. The hosted run passed
that regression and the dirty-holder and active-process protection cases.

The floor comparison against fetched `origin/main` exited zero. Both
blobs had SHA-256
`2b0df1cb5b074ffb3cebb3187fc8b092095f1ebf193917d91b9fa82e0a881580`.
The hosted checkout and flat-mirror verifier aggregates passed. The
unsharded verifier runs the full suite locally, so the keepalive's
local test limit leaves that exact invocation pending. Hosted aggregates
leave the machine-local capability state unverified.

The read-only ledger audit against the current installed generation
exited 1 with 188 refusals, including 147 branch-held and 41 branch-exists.
The current active segment differed from the collector snapshot, and
the seven-day baseline remained incomplete. Both retained segments had
identical hashes before and after the audit. The installer still reports
deployed revision `495fd9dc`. A fresh observation from the existing
hourly collector after delivery remains necessary for T5.
