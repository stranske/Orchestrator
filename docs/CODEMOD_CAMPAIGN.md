# Add-only cache-ignore campaign

The existing codemod and range rails support an explicit campaign input. The versioned
`campaigns/gitignore-caches-2026-10.json` limits work to six repositories and five ignore
entries. Existing ignore text remains byte-identical; only absent entries are appended.
No workflow or template-synced file belongs to these work orders.

```sh
python3 src/codemod_lane.py --validate campaigns/gitignore-caches-2026-10.json
python3 src/codemod_lane.py --plan campaigns/gitignore-caches-2026-10.json --json
python3 src/codemod_lane.py --file-targets campaigns/gitignore-caches-2026-10.json
python3 src/range_lane_rollout.py --campaign campaigns/gitignore-caches-2026-10.json --max-dispatches 6
```

`--file-targets` reads each target's live ignore file, format contract, and validator.
It files one `codemod` work order for each repository still missing entries, using its
actual validator before submission. It does not assign agent labels to source issues.
A stable campaign marker deduplicates retries; each issue receipt is saved before the
next target. Completed repositories are omitted, and ambiguous discovery refuses.
The source issue receives the target URLs once per changed target set.

A preview re-reads only those filed issues and their linked PRs, skips owned or held
issues and refuses injected assignments outside the exact set. It does not consult
fleet discovery. The router selects subscription agents; the dispatcher receives the
explicit add-only work order and both existing capability tags. One ready PR closes
each source issue, with normal registry routing, checks, full threads, floor and merge
guard requirements.

Live dispatch still requires the operator's explicit one-off window:

```sh
ORCH_RANGE_LANE_ROLLOUT=1 python3 src/range_lane_rollout.py \
  --campaign campaigns/gitignore-caches-2026-10.json --max-dispatches 6 \
  --apply --confirm-rollout
```

This command changes no scheduler or default. `--apply` without both other controls
refuses before any target read, router claim, or dispatch. Never enable the flag in a
recurring job simply to obtain a report.

```sh
python3 src/range_lane_rollout.py --campaign campaigns/gitignore-caches-2026-10.json \
  --record-campaign --json
```

The per-repository report is `$ORCH_STATE_DIR/capability-program/codemod-campaign.json`.
It holds campaign identity, exact target receipts, linked PR heads and merge states,
started dispatch receipts, and attributed Brain durability/cost. Preview reports current
observations; `--record-campaign` saves them without dispatch. Missing Brain outcomes,
unattributed merges, pending durability and incomplete cost remain UNKNOWN/null.
Cost includes every recorded attempt for the exact target, including failed attempts
and attempts without a PR. A total is reported only when all attempts have outcomes
and complete cost evidence; durability refers to the merged delivery PRs.
The file is a projection of existing delivery/Brain evidence, not a new learning store.
A receipt for a different campaign is refused rather than silently overwritten.
The human summary prints `campaign: repos N, dispatched D, merged M`.

Acceptance runs `python3 -m pytest tests/test_codemod_campaign.py -q`, including the
named add-only and rollout-window tests. Removing the prompt's add-only requirement
must fail the first test; bypassing the window refusal must fail the second. Mirror
construction ships the campaign inputs from Git alongside their tests.
