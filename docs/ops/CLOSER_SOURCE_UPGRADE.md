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

The output remains non-authoritative. A later, separate source-owned consumer
must validate the sealed transition and newly qualified source, then issue its
own one-use held campaign and typed business demand. It must not reuse old
authority or invoke old native entry.
