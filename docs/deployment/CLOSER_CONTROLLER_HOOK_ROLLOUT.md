# Existing Closer campaign hook: staged deployment manifest

Status: SOURCE CORRECTION TESTED; NOT AUTHORIZED FOR INSTALL, NOT PRODUCTION QUALIFIED.

The installer-only follow-up rejects differing/malformed marked bindings and cached
pinned modules (including the same path), requiring a fresh controller process.
Both prior strict expected failures were demonstrated as real failures before the
fix and are now ordinary passing regressions. The baseline matrix below records
the original preparation evidence; this source correction does not complete the
remaining demand/assignment/coexistence or production qualification requirements.
Old marked loader templates are rejected rather than silently upgraded: render
from a reviewed unhooked controller snapshot and inspect the resulting preview.
Category 5 baseline: Orchestrator `6b574927`; branch `codex/category5-deployment`.
Scope: existing `closer_campaign.controller_entry/tick_safe` and
`scripts/install_closer_campaign_hook.py`. Keep the existing Luna controller.
No new scheduler, launcher-model project, credential, or persistent access change.
This manifest deliberately leaves exact final package identity unresolved rather
than treating an in-progress source branch as a qualified installable package.

## Read-only host checkpoint (2026-10-25 UTC)

| Target | Observed value |
|---|---|
| Controller | `~/.codex/automations/reviewed-repo-automation-controller/controller.py` |
| Controller main | delegates only to `demand_router.main(argv)` |
| Controller settings | ACTIVE, Luna low, minutes 0/15/30/45 |
| Controller SHA256 | `9b8b9541165522f00bef9f7493107de8c1ea3a0a53c0486a4a042192ddb89c08` |
| demand_router SHA256 | `4e0ff69db75f61d2d1d899669e4c6497e0b9e7ad8b0c8c09b87091a2b4bbe7f9` |
| Default campaign pin | `~/.codex/orchestrator/closer-campaigns/pin.manifest.json` absent |
| Existing Sol worker | ACTIVE, hourly :20, gpt-6.1-sol medium |
| Cursor wrapper SHA256 | `c7fda82f86b38424141ce661b4fbfef9e27ebd94c93f8e5cfbcac71d1c582e6a` |
| Cursor closer pin | independent `native-final855-5ebf4269-policy8cc-20261021` package |

These are snapshots, not permission to restore them over newer changes. The older
hourly :35 native route must be inventoried with its actual scheduler owner before
cutover; wrapper inspection alone does not establish its timer or exclusivity.
Read-only follow-up checked [issue #942](https://github.com/stranske/Orchestrator/issues/942)
against the existing log and pinned manifest: 83 parsed launcher results, all
`refused`; both pinned automation policy files currently mismatch their hashes.
The issue's detailed refusal reason and launchd exit status were not independently
re-executed here. This old path is not a known-good rollback target. Do not refresh
its policy pins, revive it, copy receipts, or touch its UNKNOWN campaign as part
of this installer correction. Final trigger reconciliation belongs to the exact
qualified package and approved cutover owner.

No live file, schedule, security/closure flag, or automation record was modified.
Disk preflight: approximately 99 GiB free. Process inspection was sandbox-denied;
only one lightweight pytest process was used, no broad suite or provider call.

## Tested readiness matrix

The focused baseline run passed 58 tests and retained one strict expected failure.
The final hook-only rerun passed 21 tests with two strict expected failures; machine results are handed off alongside this document. The second expected failure was added after causal same-path cache testing.
A passing component test is not an observed installed automatic cycle.

| Requirement | Evidence / result | Remaining prerequisite |
|---|---|---|
| Pinned loader, lazy closure and tamper refusal | Existing loader subprocess tests PASS | Seal and qualify exact final runtime closure |
| Missing pin / opt-out | Dedicated acceptance PASS; no tick/state creation | No claim that existing campaign equals current eligible demand |
| Existing owned-campaign continuation | controller ticks then preserves fallback/exit code PASS | This supports reconciliation, not fresh demand admission |
| Current no-demand and due-time gates | NOT established by hook | Complete fresh discovery, current eligible/due targets, errors freeze, live/UNKNOWN owner barrier before NEW campaign admission |
| Atomic assignment | Host `save()` uses same-directory temp + `os.replace`; read-only inspection | Freeze fresh accepted generation, assignment bytes/hash/targets as ONE admission; end-to-end crash/concurrency tests still required |
| Shared lane lease | Existing native-lease focused tests PASS, including concurrent winner, successor protection and crash fence | Prove BOTH old and new installed triggers use exact same canonical lease/state and reconciler |
| Restart | Existing second-scheduled-process crash/capacity test PASS | Exact deployed package restart/unknown effects observation |
| One whole assignment | Existing serial round/worker tests PASS | No second campaign/state namespace for same assignment |
| Valid-stop | Existing queue assignment/source-revision binding test PASS | Exact deployment's pipeline receipt plus automatic-cycle substantive work |
| Substantive integration | Acceptance verifies existing dispatcher `run --assignment --state --apply --workers --pipeline --runtime-manifest` | Preserve runtime AC, fresh evidence, review, merge guard and native verifier; do not replace dispatcher with a launch-only LLM |
| Unqualified pin | Existing integration test PASS, no launch/fallback | Exact mechanism-specific qualification receipts |
| Repin / repeated render | FIXED: same-pin valid shape idempotent; differing/malformed/multiple bindings reject | Deploy only reviewed fresh template preview; old hook templates require explicit rerender |
| Same-path cached module after package update | FIXED: loader refuses any already-cached closure module before import-state changes | Use a fresh controller process; shared import objects are never reloaded or deleted |
| Native provenance | Direct controller tick is not observed wrapper entry | Qualified native wrapper path or independently qualified intended mechanism, never forged native origin |
| Old/new trigger coexistence | NOT proven | Approved exact cutover transaction and overlapping-trigger test; retain old lane ownership/unknown barriers |
| Source closure | `ORCH_SUPERVISOR_CLOSE` gate remains unchanged | Separate explicit approval; keep opt-in off in package/worker environment |

## Exact package record required before installation

The deployment owner must fill every field with evidence from the same frozen
package; any missing field means HOLD. Role/hold/execution-contract branches are
inputs, not substitutes for this record.

```json
{
  "status": "HOLD_PENDING_QUALIFIED_EXACT_PACKAGE_AND_DEPLOYMENT_AUTHORIZATION",
  "source_commit": null,
  "package_directory": null,
  "package_digest": null,
  "manifest_path": null,
  "manifest_sha256": null,
  "module_closure_digests": null,
  "python_path_and_sha256": null,
  "mechanism": null,
  "mechanism_qualification_receipts": null,
  "role_qualification_and_confinement_receipts": null,
  "assignment_generation_hash_and_due_admission": null,
  "shared_lease_state_and_old_trigger_overlap_receipt": null,
  "controller_before_and_preview_sha256": null,
  "native_wrapper_before_and_preview_sha256": null,
  "policy_settings_pins": null,
  "rollback_receipt_and_state_compatibility": null,
  "deployment_authorization": null,
  "source_closure_opt_in": false
}
```

## Rollout transaction (prepare now; apply only after prerequisites)

1. Parent integrates frozen source owner changes and installer mismatch and same-path module-cache fix (or enforced fresh-process package isolation). Run
   the focused hook, demand-admission, ownership, restart and valid-stop contracts
   against THAT package; coordinate broad checks with core owner. Qualify exact
   roles, installed adapters/confinement and mechanism without substituting fixture
   success for provider/host qualification. No paid calls are authorized here.
2. Read current controller, router, wrapper, schedule and worker ownership hashes.
   Inventory :20 Sol and :35 native triggers and their canonical assignment, lane
   lease, state and unknown-operation store. Reject drift from reviewed snapshot.
   Resolve sequencing: current hook ticks BEFORE demand discovery, so new-demand
   initiation needs fresh admitted assignment; existing campaign reconciliation
   may proceed separately. Incomplete discovery preserves accepted assignments.
3. Capture byte-exact backups with mode, hashes and paths outside live targets.
   Snapshot metadata only for durable campaign/state; never restore stale state
   over live or UNKNOWN effects. Resolve mechanism policy pins affected by wrapper
   or settings changes and requalify them.
4. Render to an offline preview using the EXISTING installer. This command is
   a template until the exact package record has all non-null values:
   `python scripts/install_closer_campaign_hook.py --controller <captured-controller> --manifest <qualified-exact-manifest> --out <controller-preview>`.
   Input and output must differ. Inspect embedded `CLOSER_CAMPAIGN_MANIFEST`,
   single main/hook, syntax and complete diff. A marked source with a different
   manifest is a hard deployment HOLD, never a successful no-op repin.
5. Freeze assignment atomically with complete generation, due/eligible targets,
   package identity, report freshness and original state/lease bindings. Test
   no-demand, future due, incomplete discovery, HELD/UNKNOWN owners, restart,
   simultaneous old/new entry and source drift with zero provider/action effects.
6. Obtain deployment authorization for the exact reviewed controller/package and
   any necessary old-trigger cutover. Current delegation expressly does NOT
   authorize installing, schedule disabling/changing, security/closure flags or
   persistent access changes. Do not use a new schedule to evade this boundary.
7. Authorized deployment owner acquires existing exact lane ownership, verifies
   before hashes again, writes fsynced same-directory staged files and atomically
   replaces only reviewed targets. Do not unlink/steal incumbents, reset unknown
   operations, install an unqualified package, or create an alternate state root.
   Publish pin only in the authorized transaction after package verification.
8. Observe one actual Luna cycle: fresh accepted demand -> one claimed bounded
   dispatcher round -> substantive same-run evidence -> assignment-bound valid
   stop/reconciliation. Also observe no-demand/future-due zero dispatch, process
   restart, and overlapping legacy trigger zero duplicate starts. Mark lifecycle
   UNKNOWN until observed. Hourly Sol is not needed just to launch this hook, but
   retiring its duplicate launch path needs the separately approved cutover.

## Rollback transaction

- Stop admitting NEW campaign work using the approved controller/pin rollback;
  do not cancel an incumbent or delete its state, lease, result, queue or ledger.
  No schedule or opt-out mutation is authorized by this preparation task.
- Compare current hashes with installed-after hashes. On drift, HOLD and hand off;
  do not overwrite a newer owner's controller or wrapper.
- Validate `rollback_decision` against actual campaign/release versions and pins.
  If workers pin the newer release, use its compatible reconciler until terminal
  or report exact incompatibility. Never force an older reader onto newer state.
- Restore byte-exact reviewed controller/wrapper/pin backups by an authorized
  atomic replacement only when ownership and compatibility permit. Restore only
  settings explicitly approved in the same cutover; preserve all unrelated lanes.
- Re-read hashes, mechanism/policy bindings and existing demand path. Demonstrate
  same assignment, no new duplicates, preserved unknown effects and valid stop.
  Record before/after package identities and concrete evidence; rollback is not
  complete merely because an old controller file was copied.

## Handoff / remaining blockers

Core owner has acknowledged no overlap for the dedicated acceptance filename.
The installer, dedicated acceptance tests and this manifest are owned here; campaign, dispatcher, hold,
profile and execution-contract source remain with their existing owners. Installer repin and cached-module defects were reported to core owner and subsequently fixed under parent-authorized installer-only ownership. Fresh-admission ordering remains a source-owner handoff. No live install was attempted. Final activation is pending qualified exact
package, missing demand/overlap/atomic-admission evidence, and explicit deployment
authorization. The separately denied consumer response batch remains untouched.

## Installer source correction verification

Both original expected failures were first run as ordinary regressions against
the unchanged installer: 2 failed (conflicting pin did not raise; updated disk
pin returned old cached code). With the correction, 42 focused installer/hook
tests pass, including malformed/multiple bindings and markers, same-pin normalized
idempotency, all seven same-path cached dependencies preserving existing import
objects/path/bytecode setting, valid replacement Python, original loader tamper
cases and installer input-overwrite rejection. No expected failures remain in
the dedicated acceptance module. Ruff, Black and diff checks pass.

The cache policy is intentionally conservative: even unchanged cached pinned
modules require a fresh process. Imports of ordinary loader standard-library
helpers may occur; pinned cached objects are never replaced or deleted. The
installer recognizes only its current exact generated loader template for
marked-source idempotency, and rejects old or modified templates.
