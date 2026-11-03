# Production branch-failure accounting (2026-11-03)

Read-only inspection of the ten `failed(branch-held|branch-exists)` events in the
7-day window before repair. None are treated as reclaim-code defects without
target-specific evidence; safety refusals remain in the archived ledger.

| Target | Lane | Cause | Holder / note | Classification |
|--------|------|-------|---------------|----------------|
| stranske/Workflows#4239 | opener | branch-exists | (no holder) | Local branch `closer/issue-4239` exists; opener expects `orchestrator/issue-4239`. Lane naming mismatch, not missing reclaim. |
| stranske/Workflows#4137 | opener | branch-exists | (no holder) | Same pattern: `closer/issue-4137` present locally. |
| stranske/Workflows#4407 | closer | branch-held | `stranske__Workflows__3958__opener` | Holder had untracked coverage artifacts and a live process (`pgrep` pid on path). Legitimate dirty/live refusal (4 events). |
| stranske/Portable-Alpha-Extension-Model#2460 | closer | branch-held | `stranske__Portable-Alpha-Extension-Model__2375__opener` | Untracked coverage/pytest artifacts and live pid on holder path (3 events). Legitimate refusal. |
| stranske/Orchestrator#1059 | closer | branch-held | `stranske__Orchestrator__843__opener` | Holder directory already gone; failure coincided with live dispatcher/agent processes referencing orchestrator paths. Legitimate live-path refusal, not idle reclaim miss. |

Operational baseline: full ledger copied to `production-ledger-archive.ndjson` in this
directory; active `~/.codex/orchestrator/provision-events.ndjson` rotated to
`provision-events.ndjson.pre-718-repair-2026-11-03` so the rolling 7-day metric
measures post-repair production only without deleting historical events.
