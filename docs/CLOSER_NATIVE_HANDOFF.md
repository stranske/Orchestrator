# Native wrapper handoff

`native-handoff/v1` observes a reviewed native wrapper invoking the pinned Runtime. It is independent of `cli-offload/v1` and `desktop-scheduler/v1`. The supported initial origin is the canonical `~/.codex/bin/cursor-lane.sh closer` wrapper. Its actor identifies the wrapper origin, not the provider selected by current capacity policy. Other wrapper origins require their own reviewed adapter.

The Runtime does not infer that launchd or the Codex Desktop scheduler started a process. Native receipts say `source=owned-native-wrapper-process`, `scheduler_origin=unobserved`, and `desktop_qualified=false`. The stricter Desktop event contract remains unchanged; this adapter cannot issue Desktop promotion evidence.

## Export and activation

`scripts/render_closer_native_entry.py --source SOURCE --manifest MANIFEST --campaign-root ROOT --out NEW_PREVIEW` exports an opt-in wrapper preview and hashes. It refuses to overwrite the source, canonical wrapper, or existing output. The inserted block precedes the legacy wrapper's capacity/auth/provider branch. An enabled native invocation returns the pinned Runtime result without falling through to that branch. Opener behavior is unchanged.

Host activation is a separate reviewed deployment operation: retain original wrapper bytes; review/install the preview; pin its exact resulting hash in the host manifest; provision a private mode-0700 observation directory and canonical campaign directory; bind the physical interpreter and closed Runtime modules; then create the explicit `~/.codex/handoff/closer-native-handoff.enabled` opt-in file. The launchd plist need not change. This source change neither installs a wrapper nor creates that opt-in file.

The manifest mechanism contract binds `actor=cursor`, wrapper path/hash, interpreter path/hash, observation root and campaign root. `native-configured-policy/v1` pins only the canonical ACTIVE supervising controller and imi-closer TOMLs, using the same owner mapping as configured CLI policy. Fresh pause/hash reads precede entry, worker and target delegation. These reads are configured-file policy, not app policy readback.

## Qualification and continuation

The pinned launcher provides `qualify-native --manifest MANIFEST --budget-seconds N`, `native-status --manifest MANIFEST`, and the wrapper-only `native-entry`. Qualification launches the actual opted-in wrapper with a fixed no-effect request. Independent owned-process metadata verifies the wrapper and entry PID/birth/byte-exact argv/lineage. Two fixed no-effect children demonstrate durable handoff, controlled crash, restart and result recording. Children share the original deadline, with cleanup inside that budget. No provider, queue or scheduler-control API is called. An unknown submission or actual denial remains held; qualification never resets it or relaunches it.

The resulting signed record binds the package, policy, wrapper, interpreter, nonce and immutable phase hashes. Signatures detect substituted JSON within the trusted same-UID code model; they are not an OS privilege boundary against malicious code running as that user. Arguments are hashed, not persisted. Kernel argv parsing does not inspect environment or private provider/session storage.

Normal entry observes its actual wrapper parent, acquires its own entry lock, writes durable intent, and calls the existing campaign tick. Direct native tick without that in-process observed-entry capability is held. Each detached worker has a signed exact pre-launch handoff bound to campaign state, target, owner, generation, result identity and manifest; the worker still passes existing campaign claims, current policy, Core and native A/B/C fences.

A terminal scoped native round can return `scoped_continuation` only after independently validating its exact named immutable receipt against the pinned original assignment, all admitted-domain run outcomes, and absent native lease. This distinguishes a reviewed ordinary global-stop refusal from timeout, denial, arbitrary C failure or unresolved admitted effects. Global stop remains false, held targets remain held, and no acceptance is claimed. Campaign consumption revalidates the proof before making that round pending again. Older Runtime versions lacking admitted-domain validation remain held.

The host configuration selects scoped coordination explicitly with `coordinator_mode=resource_scoped` and the exact installed `coordinator_contract_pin`. An incompatible pin refuses dispatch. When every assigned target is fenced before acquiring a shard, a separate validator requires a positive hash-bound, round- and target-specific no-callback admission refusal for each target and empty run IDs. Only that proven no-start boundary returns `unavailable` for ordinary pending cadence. Missing proof, a callback start, or an uncertain admitted outcome remains held; historical rows and global-stop failure are unchanged.

Actual installed-wrapper qualification and scheduled-entry deployment remain release operations. Isolated fixture success proves the implementation path, not that a host scheduler ran it or that any issue was accepted.

## Optional standing native admission

A host may explicitly configure `mechanism_contract.capacity_publication`:

```json
{"schema":"native-scoped-capacity/v1","owner":"native-closer-host","profile_ids":["claude-sonnet-5-5-low"],"bounded_trial_agents":["claude"],"admission_seconds":600,"continue_on_assignment_change":true}
```

This is a private host capability pin. Only approved active low-effort offload profiles are admitted; trial permission remains separate from measured headroom. Known quota markers and ordinary-usage denials always win. Publication uses only the existing allowlisted operational polls and read-only shed markers, never the global scanner or provider session stores. Missing/invalid policy, unreadable inputs, changed input hashes or unsupported profile scope hold the invocation.

The observed entry freezes one original window of at most 900 seconds. After reconciling actual terminal worker results, only a genuinely pending `resource_scoped` whole-round campaign may publish. Live, unknown, denied and reconciliation-only campaign workers receive no new report, assignment or deadline. The source copies current canonical controller assignment bytes into `<observation_root>/<nonce>/assignment.json`, publishes an exclusively created `scoped-capacity.json`, and signs `admission.json`. Old files and the original campaign worker configuration are preserved. A second normal invocation uses a separate nonce and current inputs; it does not refresh old worker or shard deadlines.

The signed pre-launch handoff binds its worker to that exact context. Its effective configuration uses the immutable snapshot for both B and C, the scoped report, and the original deadline. A rechecks that snapshot's hash against the current canonical controller assignment. The worker's B budget subtracts C reserve, the pipeline's 300-second inner receipt padding and a 15-second margin; it is clamped again after A. Insufficient remaining budget starts no B. Worker results retain the original signed context, so later native invocations can revalidate the exact historical snapshot even after the active nonce changes or that old capacity report expires. This historical readback grants no new provider permission.

Default finite campaigns remain finite. With explicit `continue_on_assignment_change=true`, a known successful standing round may become pending only when the current canonical assignment hash differs. The actual B0/C0 boundary must have a signed exact named-round proof recording global stop valid at that boundary. Rearming revalidates that immutable receipt, exact original run IDs and row hashes, and the signed worker/context binding. Unrelated later quarantine does not revoke this old known completion; changes to its own rows or proof hold it. The transition preserves old workers and phase, appends a durable assignment transition and advances the generation. A human hold, completed phase, release operation, live/unknown/denied worker or missing completion proof cannot be resurrected. It never claims issue acceptance. Each newly admitted callback still passes the current native, Core, pause and quarantine fences.

Before each native B submission, the pinned hook retains an immutable, signed census of existing round receipt identities in that invocation's observation directory; C accepts only a newly produced receipt bound to that submission, never a prior successful same-assignment round, and an unknown submission cannot refresh this watermark.

## Exact cleanup and pre-dispatch no-start evidence

The rendered postrun adapter requests a structured receipt from the pinned lease
module. A receipt attests an actual removal of the matching original lease under
its coordinator lock. It records the credential and metadata hashes, canonical
lane path, lease inode and removal time; it does not expose the credential.
An already-absent lane is still an idempotent success for the legacy release API,
but cannot produce an exact cleanup receipt. A successor token, malformed lane
or concurrent unrecognized files are retained.

Native postrun emits nonce-bound machine-readable markers for exact cleanup and
for a missing terminal named round. Its EXIT trap preserves the body status and
propagates cleanup refusal. The worker records separate A/B/C timestamps and
whether B was submitted, so setup, request execution and cleanup are distinguishable.

A new positive pre-dispatch refusal can remain pending only when the pinned worker
observed no B submission, an ordinary admitted A, the expected typed C refusal,
and exact cleanup within that invocation. The Runtime writes a create-only signed
`native-pre-dispatch-no-start/v1` boundary tied to the original admission, worker,
result identity, campaign generation, assignment and package. Campaign consumption
revalidates that boundary and the complete execution report. A missing, changed,
replayed or contradictory report remains unknown. A possible B call, timeout,
denial or arbitrary C result cannot borrow this proof. It records no accepted work.

Older unknown results have no new proof and retain their original meaning. Neither
lane absence nor a plaintext “not started” message supplies the missing historical
cleanup identity.

## Pinned execution workspaces

Native campaign configuration may provide `workspaces` as an exact canonical
`path`/`sha256` file pin. Admission freezes the bytes in the invocation directory;
worker execution and result readback use that immutable copy. The dispatcher
validates the versioned mapping and repository identity before selecting a checkout.
An exact-target execution selection takes precedence over repository fallback;
unused fallback paths are not silently added to that new candidate's resource set.
The full native coordinator capability is validated before projecting its storage
fields for the scoped storage implementation.

`closer-workspace-bindings/v1` separates `historical` scope hints from prospective
exact-target `execution` choices. Neither mapping is evidence of an old callback's
working directory. An old local prepared snapshot without an immutable original
execution binding retains conservative repository fallback. Explicit original
worktree, intent, PR, source and branch keys remain fences. Physical aliases and
parent/child writable checkout overlaps are checked as shared resources. New
configuration never moves an old unknown onto the newly selected checkout.
Native admission requires the versioned envelope and canonical physical paths for
every selected checkout; a pinned flat map or a path alias is refused before effects.
Shared ancestor and exclusive checkout locks cover admission through intent
publication and effects, excluding parent/child writers while allowing sibling
checkouts to run concurrently; retained unresolved intents continue to fence overlaps.
