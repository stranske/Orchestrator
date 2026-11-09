# PR #1278 query verification — 2026-11-09

Starting exact local/remote head: `fdb84f5618b50dcc76a5661740f4a87d22c6ba5a` on `orch/issue-1276`.
Worktree inventory showed no preserved holder for this branch; the continuation worktree was clean.

Outcome: count of distinct validated remote completions, deduplicated by authenticated
round receipt path and SHA-256. A measured value requires a validated manifest,
same-pin independently observed desktop qualification for every enabled runner,
and an authenticated native worker admission and signed successful C0 round boundary.
A pin, desktop qualification, successful lifecycle or truthy completion flag alone is insufficient.

Real-input command (recorded 2026-11-09):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/bin/python3 -c 'import json,time; from dataclasses import asdict; import feature_goals as f; print(json.dumps(asdict(f.closer_campaign_completion(int(time.time())))))'
```

Stop rule: at most three focused repair/test cycles or 60 minutes, whichever comes
first; if still failing, stop at the first broken step and ask whether to stop.
No live worker, observer, registration, receipt, or completion was created.

## Focused checks

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tests/test_feature_goals.py tests/test_closer_runtime_binding.py
```

Final output: `84 passed, 10 skipped in 31.59s` (2026-11-09). Skips are not acceptance evidence.
The first run printed `16 failed, 66 passed, 10 skipped in 36.16s`: the small
manifest fixture lacked its required runtime entrypoint. After correcting that
fixture, the second run printed `83 passed, 10 skipped in 32.70s`. The final run
also covers an explicitly registered observer that returns only self-reported success.

The positive query test reuses the existing signed native host/round fixtures,
real manifest validation, real desktop hash-bound phase observation, signed worker
admission validation, and signed completed-round validation. It creates synthetic
local pipeline inputs; it does not demonstrate a new live remote completion.
The same test rejects changed receipts, foreign campaigns, pin mismatches, unrelated
mechanisms and truthy completion flags. Repeated calls preserve registry and file bytes.
Additional cases cover a bare pin, unrelated/missing observers, harness receipts,
desktop request/digest/artifact mismatches, malformed/non-object JSON, an external
configured root, and permission-denied directory enumeration.

## Real-input result

The old query printed `1.0` on these roots using its self-created observer and
file-existence test. That was a false positive. The corrected query prints the
following unmeasured result (as_of 1794207112 on 2026-11-09). The default pin is missing
and archived pins fail validation or their campaign directories are absent. No valid
live completion was established. A controller-backed invocation cannot be claimed: source
search finds the observer registration API but no production registration/bootstrap caller.
A real authorized controller must supply independently observed matching desktop metadata
and signed guarded remote artifacts for a valid installed pin. This change does not
manufacture those missing inputs or alter archived pins.

```json
{
  "value": null,
  "evidence_class": "unmeasured",
  "as_of": 1794207112,
  "reason": "/Users/teacher/.codex/orchestrator/closer-campaigns: pin/desktop qualification unavailable: FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/closer-campaigns/pin.manifest.json'; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-capacity-source1041-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; ... (38 additional campaign directories with similar errors)",
  "inactive_since": null
}
```

Review, current-head CI, the seven-minute floor, guarded merge, and post-merge
verification remain required; this record does not claim those stages complete.
