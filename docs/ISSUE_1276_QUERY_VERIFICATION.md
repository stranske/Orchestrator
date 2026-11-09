# PR #1278 query verification — 2026-11-08

Starting exact local/remote head: `6001d28b9ed782f9d4c21116e73f4015dc86ab88` on `orch/issue-1276`.
Worktree inventory showed no preserved holder for this branch; the continuation worktree was clean.

Outcome: count of distinct validated remote completions, deduplicated by authenticated
round receipt path and SHA-256. A measured value requires a validated manifest,
same-pin independently observed desktop qualification for every enabled runner,
and an authenticated native worker admission and signed successful C0 round boundary.
A pin, desktop qualification, successful lifecycle or truthy completion flag alone is insufficient.

Real-input command (recorded before editing; check date 2026-11-08):

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

Final output: `84 passed, 10 skipped in 32.43s`. Skips are not acceptance evidence.
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
file-existence test. That was a false positive. The corrected query printed the
following unmeasured result. The default pin is missing and archived pins fail
validation or their campaign directories are absent. No valid live completion
was established. A controller-backed invocation cannot be claimed: source search
finds the observer registration API but no production registration/bootstrap
caller. A real authorized controller must supply independently observed matching
desktop metadata and signed guarded remote artifacts for a valid installed pin.
This change does not manufacture those missing inputs or alter archived pins.

```json
{
  "value": null,
  "evidence_class": "unmeasured",
  "as_of": 1794201778,
  "reason": "/Users/teacher/.codex/orchestrator/closer-campaigns: pin/desktop qualification unavailable: FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/closer-campaigns/pin.manifest.json'; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-capacity-source1041-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-contracts-source1034-20261031/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-dispatcher-bootstrap-source701d-20261031/campaigns: pin/desktop qualification unavailable: FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/runtime-campaigns/csv-dispatcher-bootstrap-source701d-20261031/campaigns/pin.manifest.json'; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-dispatcher-bootstrap-source701d-20261031-resume/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt10/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt11/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt12/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt13/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt14/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt2/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt3/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt4/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt4-inert-preparation-failure1/campaigns: campaign directory unreadable: FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt4-inert-preparation-failure1/campaigns'; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt5/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt6/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt7/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt8/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial-attempt9/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial.prelaunch-plan-failure-1f9/campaigns: pin/desktop qualification unavailable: CampaignError: closer_native_source_upgrade.py: digest differs from manifest; closer_owner_live_exercise.py: digest differs from manifest; modules set does not match pinned runtime modules; host Runtime binding differs from public package; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-domain-act-20261101-initial.prelaunch-state-pin-failure-884/campaigns: pin/desktop qualification unavailable: CampaignError: closer_native_source_upgrade.py: digest differs from manifest; closer_owner_live_exercise.py: digest differs from manifest; modules set does not match pinned runtime modules; host Runtime binding differs from public package; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-held-source1036-20261031/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-invitation-source1044-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-ownership-source1031-20261030/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; Runtime tree contains undeclared executable code; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-pagination-source1035-20261031/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-pre-effect-source1043-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-role-source1038-20261031/campaigns: campaign directory unreadable: FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/runtime-campaigns/csv-role-source1038-20261031/campaigns'; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-role-source1038-20261031-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-verified-source1043-retry-20261101-initial/campaigns: pin/desktop qualification unavailable: FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/runtime-campaigns/csv-verified-source1043-retry-20261101-initial/campaigns/pin.manifest.json'; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-verified-source1043-retry2-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules; /Users/teacher/.codex/orchestrator/runtime-campaigns/csv-verified-source1043-retry3-20261101-initial/campaigns: pin/desktop qualification unavailable: CampaignError: modules set does not match pinned runtime modules",
  "inactive_since": null
}
```

Review, current-head CI, the seven-minute floor, guarded merge, and post-merge
verification remain required; this record does not claim those stages complete.
