# Source-upgrade isolation route

`closer_native_source_upgrade.py` is a bounded preparation/CAS boundary for a
terminal worker whose Source correction stopped before Native ACT. It accepts
only the exact `owned_unknown` owner-criterion disposition, an empty ACT queue,
null deferred operation accounting, and `completed=false` / `accepted=false`.
It does not reinterpret retirement evidence, retry the old campaign, or create
new business demand.

The supported predecessor is the reviewed `f858dc3857ad7a90461126abd945edc0d380d9b3`
release. Its 191-module map, public descriptor bytes and physical interpreter
are pinned before any old import. The current release has a different module
closure; its strict validator is used only for the replacement. Old code is
captured into a private temporary directory, rehashed before import, and runs
its own version-aware validation against the original manifest/proof paths.
Undeclared Python or cached bytecode is refused before execution, and original
closure/manifest bytes are checked again before accepting the diagnostic.
The parent captures exact campaign bytes into the private staging directory;
the old campaign reader takes its lock on that snapshot. Cohort and proof
validation retain the original campaign path. This avoids reacquiring the
parent's real campaign lock during locked plan validation. The returned
snapshot digest must match the captured reference and the original bytes
must remain unchanged. Validate this composition under the actual plan lock
closure before another release, as well as outside the locks.
Validate this read-only boundary against the actual predecessor before a new
full release; simulated transition fixtures alone do not establish compatibility.

Before changing the wrapper it validates immutable old state/result/cohort/round
references, runs a bounded `-I -B` hash diagnostic under the old physical
interpreter, seals the current disposition, and writes an immutable intent. The
seal, intent, and post-CAS known receipt are all signed with the current
observation-root mechanism key; for a clean current observation root,
`apply` provisions that existing mechanism-domain key only after its second
locked plan validation and before the first seal is written. Recovery never
creates or replaces it. Unsigned or substituted records are held. The seal
path is fixed beneath that current observation root. The
CAS is permitted only from the old manifest's canonical wrapper render to a
different current manifest's pinned render. After CAS, validation is against
the new contract only; invoking the old contract would be circular because its
wrapper pin intentionally no longer matches.

A complete matching pre-CAS seal can resume the same operation after a later
intent write fails, provided the old wrapper preimage, absent new state, all
source facts and the externally confirmed plan digest revalidate under the
original locks. Existing records are never overwritten. Partial or different
records remain held. After replacement, a missing known-CAS receipt remains
uncertain and cannot create a campaign or retry the CAS automatically.

Operator flow is deliberately explicit and uses the fully installed Runtime
API only: call `plan` with the old witnesses, replacement manifest, strict
rebound worker configuration/frontier, and installed release journal; call
`apply` with that exact plan confirmation; then call `prepare_held` with only
the sealed disposition and its plan digest. `prepare_held` creates one inert
generation-one HELD campaign through the readmission intent/known protocol.

That is still non-authoritative. A later, separate normal
`closer_native_readmission.admit_initialized` call must validate the current
source factory and typed demand before clearing the hold. It must not reuse old
authority or invoke old native entry.
