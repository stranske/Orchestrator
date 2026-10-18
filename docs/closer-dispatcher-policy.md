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
