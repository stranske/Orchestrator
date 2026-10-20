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

Actual installed-wrapper qualification and scheduled-entry deployment remain release operations. Isolated fixture success proves the implementation path, not that a host scheduler ran it or that any issue was accepted.
