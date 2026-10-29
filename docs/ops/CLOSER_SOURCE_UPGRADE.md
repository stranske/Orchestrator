# Source-upgrade isolation route

`closer_native_source_upgrade.py` is a bounded preparation/CAS boundary for a
terminal worker whose Source correction stopped before Native ACT. It accepts
only the exact `owned_unknown` owner-criterion disposition, an empty ACT queue,
null deferred operation accounting, and `completed=false` / `accepted=false`.
It does not reinterpret retirement evidence, retry the old campaign, or create
new business demand.

Before changing the wrapper it validates immutable old state/result/cohort/round
references, runs a bounded `-I -B` hash diagnostic under the old physical
interpreter, seals the current disposition, and writes an immutable intent. The
CAS is permitted only from the old manifest's canonical wrapper render to a
different current manifest's pinned render. After CAS, validation is against
the new contract only; invoking the old contract would be circular because its
wrapper pin intentionally no longer matches.

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
