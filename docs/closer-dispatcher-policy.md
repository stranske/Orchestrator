# Status-driven closer execution

This policy supersedes older one-complex-lane or at-most-one-repository selection
limits. It does not authorize changing schedules, configured models, budgets,
permissions, security boundaries or another writer's work. The Luna demand
controller retains its scheduling authority.

After mandatory preflight, start the repository-backed coordinator with
`CLOSER_ROUND_STATE="$HOME/.codex/handoff/closer-dispatcher.json" python3 ~/.codex/orchestrator-mirror/closer_dispatcher.py run --assignment ~/.codex/automations/reviewed-repo-automation-controller/closer-assignment.json --state "$HOME/.codex/handoff/closer-dispatcher.json" --budget-seconds 2700 --max-workers 2 --apply --workers --pipeline`
under existing authorization, resource bounds and ownership. The fixed ledger path
retains pending operations across rounds. Omitting `--apply` is a read-only plan;
it does not advance the queue. Known repository workspaces are resolved through
their canonical target remote; provide explicit exact-head worktree mapping when
a repair needs a prepared branch workspace. Do not substitute a different repository.

The pipeline uses a sibling `<state>.pipeline.json` coordinator receipt and
per-target action ledgers under the pipeline state directory. Existing closer
queue state is preserved during this migration. A read-only staged preview is
`run ... --state <path> --shadow`; it retains bounded evidence in a sibling
private evidence directory but does not dispatch workers or queue actions.

Shell exports do not persist between separate tool calls. Prefix ACTION C itself
with `CLOSER_ROUND_STATE="$HOME/.codex/handoff/closer-dispatcher.json"` so postrun
receives the same initialized ledger even when it runs in a new shell.
Do not invent a ledger or backfill worker completion from an acknowledgement.

Cover every assigned issue and PR, including merged implementations with pending
acceptance. Select the next action from current evidence: verification, evidence
retrieval, review resolution, bounded repair, guarded merge, postmerge verification
or source closure. Revalidate exact heads and ownership before mutations. Reuse
unchanged evidence; do not repeat general review or provider calls to create motion.
Keep one writer per conflicting source/target and honor holds and exclusions.

Use independent bounded workers for disjoint work only when existing capacity and
admission allow it. The coordinator owns integration, full required check topology,
review-thread resolution, review floors, the normal guarded merge, native verifier
and final source-acceptance decision. A merged patch, signed receipt, review badge
or routing record alone does not establish source completion. Preserve raw findings,
signatures and provenance. No worker may substitute a fabricated trusted disposition.
An exact-head pull-request CI run whose GitHub association array is empty is retained
only when its event, branch and repository match the linked PR and a bounded PR
lookup finds exactly one matching head. Failed runs remain in the evidence; retrieved
artifacts alone do not establish the required-check topology or acceptance.
Raw test and coverage receipts use the existing bounded archive expansion budget;
the compact packet section limit does not discard otherwise bounded input files.
Retain exact raw hashes, reject unsafe XML, and keep coverage.py statement and branch
counts separate. Unrecognized metadata and unknown required-check topology remain
partial proof. A failed stronger worker stays at the exception stage with its native
receipt; it cannot advance to an action, and a later round prepares fresh evidence.
Helper-validated NON_PASS waits retain a bounded disposition proof bound to the
current merged PR, native verifier, source/review identity and one-target receipt.
A reason string alone proves nothing and the wait grants no acceptance authority.
Deterministic verifier routes are freshly observed, never replayed as cached model
assessments. Confirmed native label waits do not launch another evidence worker.

A completed private report is an internal handoff to this senior closer, not an
external blocker. Read the retained result, integrate or reject its findings and
continue within the original budget. The ledger marks `requires_coordinator`;
postrun rejects stopping on that flag while usable time remains. A trusted
integration acknowledgment is bound to operation ID, source digest, current head
and review revision; it grants no acceptance, merge or closure authority.
The low worker may return a single complete JSON document inside one JSON code
fence. The same schema, target, head and evidence checks apply after unwrapping;
prose, multiple fences and incomplete JSON do not become decisions.
Missing evidence is a completed uncertain assessment: name the gaps and leave
decision criteria empty instead of inventing PASS rows. A normally exited worker
that reports `OFFLOAD_INCOMPLETE` retains its failed result for senior integration;
without authoritative provider-incident evidence it does not suppress a healthy
profile for other targets. Real quota/transport failures and uncertain native
outcomes retain their existing barriers. Inspect retained files with read-only
commands such as `python3 -c`; do not create temporary files or widen permissions.

An asynchronous CI/review wait parks that exact target and leaves other eligible
work running. Refresh material transitions and continue while a concrete authorized
action fits the real remaining budget. Do not stop merely after one complex item.
Stop only for exhausted actual resources, an empty eligible queue, or specific
current blockers for all remaining work. Record owners, evidence revisions, next
actions and resumption conditions. A generic needs-human label is not a decision;
identify the actual unresolved choice when one exists.

A malformed ordinary worker result or an item-scoped hold can leave an exact target
in the inner queue's terminal `wait` state. The outer pipeline records this only as
`validated_inner_wait` when the finished, stop-valid inner receipt is bound to the
same target, revision, head, durable action ledger and specific source trigger, with
no uncertain operation. It is neither acceptance nor a fabricated asynchronous
event. A later cadence prepares fresh source evidence and re-evaluates the target;
an unverified or changed receipt fails closed. If the inner queue observes a newer
revision on the same head during an action,
its validated wait only proves progress to fresh preparation; the earlier outer
revision cannot directly inherit that wait. A pre-fix failed action may re-enter
preparation only when its retained blocked receipt matches this same durable proof;
the historical invalid stop remains in the audit. Reusing an unchanged wait still
requires fresh campaign admission and matching evidence producer and semantic
binding. A changed producer or head returns to normal interpretation.

An expired interrupted round may admit a fresh round without making its invalid
historical stop valid. Recovery retains the original failure evidence and permits
fresh preparation for assigned targets with no action or an exact durable
non-mutating wait proof, with current ownership, capacity, evidence and budget
checks. A failed action without that proof remains quarantined. An uncertain
operation stays behind reconciliation before any retry. Removed unsafe targets
retain their barriers; removed read-only failures remain in the audit. They do
not become new work. These barriers and the invalid-stop audit must survive
later cadences and history trimming. Missing or malformed recovery proof fails
closed; never clear the ledger or invent a resource boundary to admit work.
An unknown reconciliation outcome retains an explicit reconciliation-only trigger.
Recovery recognizes the legacy missing-trigger form only with its recorded unknown
legacy-operation result; it does not establish that the operation never started.
A compact excluded wait may recover its proof view only from a matching immutable
audit and the unchanged current native wait validator. Its original compact-row
hash remains recorded, the prior stop stays invalid, and the item stays excluded.
Full validated wait proof takes precedence over compact carry on later cadences,
including cadences whose prior stop is valid. Reassignment requires fresh preparation.
If an action lacks a derived inner queue operation, reconciliation still checks
the original legacy queue for an unresolved operation before declaring the
action not started. Original uncertainty stays quarantined for its queue owner.
When the bounded in-ledger invalid-stop history fills, the pipeline fsyncs exact
older audit entries into immutable, hash-linked chunks beside its state before
recording the archive head. Recovery verifies the complete chain; missing or
tampered chunks block fresh work while the original invalid audit remains intact.

Before ACTION C finalizes the round, run
`python3 ~/.codex/orchestrator-mirror/closer_dispatcher.py validate-stop --state "$CLOSER_ROUND_STATE" --assignment ~/.codex/automations/reviewed-repo-automation-controller/closer-assignment.json`.
The postrun hook independently performs this check before end-round and relay;
the normal EXIT cleanup still releases its mutex even on rejection. A rejected stop requires preserving the ledger and resuming or recording
the concrete resource/blocker evidence; never delete state to escape validation.

Deploy initially in migration mode: an already-running old-prompt round without
state may finish with an actionable migration warning. Once it is terminal and
the next coordinator-started round is ready, explicitly reinstall with
`--enforce-stop` so missing state rejects closer finalization. Do not enable this
flag over an active old-prompt round. Installation is dry-run by default, preserves
all non-prompt TOML fields, and runs the normal Claude prompt renderer plus each
installed Cursor/Muse renderer. All resulting caches are checked against source
and restored on failure when their bytes still belong to the installer. No install
or renderer starts workers.

## Staged resource-scoped dispatcher integration

The production CLI defaults explicitly to `--coordinator-mode legacy`. Opt in only
with `--pipeline --apply --workers --coordinator-mode resource_scoped` (or use
`--shadow` for evidence-only testing), and supply the complete contract pin:

```json
{"schema":"closer-scoped-state/v1","layout":1,"pipeline_schema":"closer-pipeline/v1","coordination_gates":1}
```

Pass that JSON as `--coordinator-contract-pin`. The staging capability is mandatory:
an older scoped root without `coordination_gates: 1` is incompatible and is never
migrated in place. Existing uncooperative installed writers must be proven terminal
before promotion. Their elapsed runtime is not terminal evidence. This change does
not install or promote a runtime, change controller scheduling, or establish a live
canary result. Non-staged legacy callers and old installed code are not evidence of
this staging capability; require their terminality before enabling a mixed deployment.
The compatibility guarantee here covers the cooperating staged dispatcher paths.

Both cooperating modes acquire stable per-target gates beside the pipeline ledger
before inspecting state. Legacy retains its JSON round lock; scoped writers use
separate item shards. Mutation intents use `<pipeline>.coordination/reservations`,
which exists independently of the scoped root, including for cooperating legacy
writers before first scoped use. Historical intents in the scoped root are still
read and fenced. Legacy stop validation checks these common unknowns too.
A cooperating legacy executor advertises its aliases in `legacy-intent.json`
under the same key locks. It may advance its existing guarded JSON queue, and its
advertisement is removed only after that durable ledger validates a known native
continuation or completion boundary. A crash or invalid stop retains it. This
separate compatibility protocol never adopts or clears a Core `intent.json`:
every Core intent, including one for the same target, still blocks a new reservation
until its current owner has written the required durable COMPLETE receipt.
Target gates are held through item work, with no fleet-wide
exclusive lock across provider calls. Before mutation, both modes reserve the same
PR, source issue, branch and authorized workspace keys derived from retained,
hash-checked prepared snapshots. Issue-to-PR aliases reserve the delivery PR and
all source issues. The native operation seam also rejects a changed head or resource
binding; native queue claims, authorization checks and exact-head guards still run.
An unknown branch/source reservation conservatively holds legacy mutations when
prepared aliases are unavailable. Read/preparation work remains possible. Non-staged
legacy apply cannot resume through an existing scoped root.

Profile execution holds one cross-process slot for every exact registry
`capacity_pool_ids` entry beneath the common `HANDOFF_DIR/closer-profile-execution`.
This preserves the existing router's exclusive pool admission: different low/strong
profiles and all six approved agents share the same pool identity. All coordinators
must use the same common handoff directory. The existing cached `capacity.json` is
read again after reservation. A held pool excludes every profile using it; fallback
retains the lane's qualified reasoning effort. Failed pools are suppressed for the
round, authentication errors are not retried, and uncertain executions retain their
native receipt and unknown cost fields. No global capacity refresh is performed.

Every scoped receipt names its exact `round_id` and returns an `action_c` object
with the arguments for `validate-stop --state ... --round-id ...` (plus `--shadow`
when appropriate). The native ACTION C hook must pass this ID explicitly; there is
no global latest-round selection. If a scoped root exists, missing IDs refuse legacy
validator fallback. Empty assignments, fenced targets and unresolved resource intents
cannot produce a valid stop. `native_cleanup_performed` remains false: the receipt
is a handoff seam, not evidence of installed cleanup. Historical uncertain intents
remain held until a separately verified, resource/head-bound reconciliation exists;
this integration exposes no clearing shortcut.

## Explicit finite frontiers

New work may opt into `finite-frontier/v2` with an immutable selected target set.
This policy removes aggregate elapsed-work admission and round cutoffs only for
that new frontier. Existing rounds retain their original deadlines, stop verdicts,
claims and uncertainty barriers; selecting the same target again does not authorize
replaying an interrupted operation.

Build the descriptor with `closer_frontier_policy.make_frontier_from_observations`
from the already-produced, eligible normalized source observations. It binds an
explicit owner and frontier ID, ordered target identities, and each target's exact
source revision and action/operation fingerprint. Canonical duplicate targets,
assignment growth, changed bindings and unselected work are rejected. Do not invent
replacement action fingerprints to make old work eligible.

Pass the serialized descriptor as `--finite-frontier-json` and an individual
request timeout as `--request-timeout-seconds` to the staged dispatcher. This mode
requires `--pipeline --coordinator-mode resource_scoped`, the complete coordinator
contract pin above, and the usual authorization and worker options. It rejects
`--budget-seconds`; legacy callers retain their existing aggregate budget behavior.
The campaign forwards the exact descriptor and request timeout to its dispatcher.
Native admission additionally requires the matching versioned capacity and
invocation proofs; proof freshness is separate from the frontier's work duration.

Each request retains its original finite timeout, at most 120 seconds. Quota,
cost, rate, profile capacity, concurrency, pause, cancellation, exact ownership and
resource-conflict checks still gate admission. The supported production callback
factory is required; this policy does not promise cancellation of arbitrary Python
callbacks. Each selected operation is attempted once and reaches completed, held,
paused, cancelled or capacity-blocked state. A changed source binding returns to a
hold rather than growing the frontier. Interrupted or uncertain provider/action
work remains durably fenced across restart and cannot become a fresh pending
attempt merely because time has passed.

Finite inner-queue workers inherit the original scoped request window and the
same held pool records and handles. Crossing a thread boundary cannot renew a
request, acquire another lease, or reuse a slot for another target. Finished
finite frontiers return their retained receipt without invoking callbacks again.

Default Core HTTP operations use one owned child per request, bounded by Core's
existing 30-second limit and the original request's remaining time. The child
also enforces that same absolute deadline if its supervisor exits. A timeout or
unproved outcome retains the original operation's uncertainty fence; it does
not authorize another attempt. HTTP status and rate-limit classification remain
bound to the pinned Core implementation.

A recovered prelaunch failure can re-enter the **same frozen frontier** through
`closer_dispatcher.py finite-reentry`. First pass `--state` (the exact target
shard `state.json`), `--frontier` (the original descriptor file), `--target` and
an explicit `--event` describing the observed recovery. Inspect the returned
plan, then repeat those arguments with `--apply --expected-plan-sha256 <digest>`.
Finally rerun the original public `run` command with its unchanged frontier and
canonical queue. This records scheduling progress; it does not evaluate a PR.

Re-entry accepts only an unstarted preparation pool refusal, a qualified
assessment transport replacing the named transport refusal, or an exact retained
scope-admission proof that the callback never started. The plan binds the row,
lease, original descriptor, retained proof and absence of any contradictory
inner-queue operation. Apply takes the existing target/run/inventory/state locks,
checks those bindings again, and retains a recoverable before/after audit. Active
owners, uncertain providers/effects, changed inputs and repeated unchanged failure
boundaries remain blocked. No history, sibling state or global fence is removed.

A proven pre-callback action refusal becomes a no-effect scope hold; it does not
become an action-ready row. The historical scope still requires its own supported
reconciliation. Paused repositories remain paused. The existing `--max-workers`
now also bounds concurrent scoped targets, preventing a nominally serial run from
starting several callbacks against the same canonical workspace while retaining
all lane ceilings and request timeouts.
