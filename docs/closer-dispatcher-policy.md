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

A completed private report is an internal handoff to this senior closer, not an
external blocker. Read the retained result, integrate or reject its findings and
continue within the original budget. The ledger marks `requires_coordinator`;
postrun rejects stopping on that flag while usable time remains. A trusted
integration acknowledgment is bound to operation ID, source digest, current head
and review revision; it grants no acceptance, merge or closure authority.

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
an unverified or changed receipt fails closed. A pre-fix failed action is carried
forward only when its retained blocked receipt matches this same durable proof;
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
