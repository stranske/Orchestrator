# Verification storage lifecycle

## Persistent verifier attribution evidence

`closer_attribution_store.py` bounds the shared
`<state>.evidence/verifier-attribution` store at 256 MiB. This is a retained-byte
ceiling, not a provider/runtime budget. Content-addressed identical observations
deduplicate. New objects and reference updates use one store lock and the existing
`storage_lifecycle.Reservations` admission ledger (`ORCH_STORAGE_STATE_DIR` when
supplied, otherwise the existing operational runtime's `.storage-lifecycle`). The forecast includes atomic
object/index staging; all resident bytes, including recovery material, count.

References are registered before dependent bytes/receipts are published. Current,
pending, uncertain and imported legacy references remain protected. The append
contract has no release, eviction or cleanup entry point. Exhaustion yields the
typed per-item `storage_capacity` prewrite wait with measured accounting for a
meaningful re-screen trigger; previously retained identical evidence stays usable.
It never silently omits a required archive or deletes the last expired remote copy.

An interrupted write retains its reservation. A synchronous failed file write,
after all owned handles have unwound, releases its prospective charge while its
partial bytes remain measured; it is not an uncertain remote effect. An exact digest-verified pending
object can resume under the same exclusive store lock; unknown partial staging
requires explicit reconciliation. This patch does not claim automatic reclamation:
future reference-aware retirement needs authoritative terminal ownership, verified
recoverability and a separately reviewed policy. Moving recovery material alone
would not free this ceiling. No live cache is cleaned or migrated by installation.

### Activation and over-capacity procedure

Before activation, identify the exact `--state` used by the installed invocation
and measure its `verifier-attribution` root with the read-only metadata operation
`storage_lifecycle.footprint(root)`; compare the larger of logical/allocated bytes
against `closer_attribution_store.MAX_BYTES`. Include control, staging and recovery
material. This is a point-in-time observation, not admission or permission to write;
the existing ledger must also admit actual staging alongside concurrent reservations.
Repeat for separately scoped state roots; one legacy-root measurement is not a
fleet total. Do not instantiate `Store` just to measure: its constructor initializes
control/ledger directories.

If at or above the ceiling, preserve the store and record its exact state/root,
measured bytes, requested growth and the affected target's `storage_capacity` wait.
An unchanged indexed reference can still return existing bytes without new admission;
do not declare all preflight reads or the whole lane blocked. Continue independent
eligible work. Do not unlink, rotate/copy the store, release unknown references or
lower storage admission to make the refusal disappear. A reviewed capacity plan or
future proven-terminal, reference-aware recoverable retirement is needed before
growth resumes; this append-only patch grants neither cleanup nor unlimited quota.

Before retry, obtain fresh measured accounting and normal per-target re-screening.
Expired required archives and uncertain operations retain their evidence. A move to
recovery alone is not reclaimed capacity, and successful collection is not acceptance.

`scripts/verify_before_sync.sh` admits and finishes its own storage through
`src/storage_lifecycle.py`. This is part of the existing verification command;
there is no scheduled cleaner, new native automation, or engineering time lease.
It does not install a runtime or retire unrelated worktrees.

## Prepare before staging

Strict local verification preserves the **27 GiB** launch floor. Before copying
anything substantial, one filesystem-locked ledger admits this demand:

```
required available bytes = max(27 GiB, peak growth + observed operating margin)
                         + this job's staging forecast
                         + outstanding bytes for other admitted jobs
```

The source estimate covers simultaneous mirror/copy-contract staging, including
Git object sizes for committed docs/tests/scripts even when working-tree files
are absent or online-only. State
directories over the existing copy limit are excluded, and admission metadata is
never copied into another run. Cross-filesystem snapshot publication refuses
until a separate destination forecast is supplied; a source-filesystem plan is
not authority to spend another volume's space.

The initial strict-verification forecast is 23,860,408,320 bytes (22.22 GiB),
grounded in the October 20 strict-run window: available space fell from
29,490,778,112 bytes at 20:40 UTC to 5,630,369,792 bytes at 21:02 UTC.
This is larger than October 21's partial 9.86 GiB observation. These shared-volume
observations are not exclusive attribution or proof of a final upper bound.
`VERIFY_BEFORE_SYNC_PEAK_BYTES` can supply a larger producer forecast. The largest
recorded peak also wins; a failed or unusually short run never lowers it. The
observed 5,630,369,792-byte residual is the initial operating margin. The 27 GiB
launch minimum already budgets working space, so adding a second full peak to it
would count that space twice. A comparable next run prepares above roughly
**27.47 GiB plus staging and other reservations**; a larger measured or planned
peak raises that threshold. This observation is a conservative starting plan,
not a guarantee against unregistered external writers or an unseen larger peak.

Admission refusal reports current free, required, deficit, staging, peak, floor,
and other reserved bytes. It starts no copier or verifier. Once the staged copy
is complete, the exact authenticated owner releases that prospective copy charge;
already resident bytes remain accounted through actual free space. The peak charge
stays conservative until terminal completion. Each supervisor samples available
space during its run and reports pressure without killing active work.

Reservations have **no time expiry**. A vanished parent or elapsed time does not
prove its descendants stopped. Uncertain writers retain their charges. Producers
that do not use this ledger and unrelated applications can still consume space;
this is measured admission for cooperating jobs, not an APFS block reservation.

## Own disposable output

Each run gets a private `storage-verify-*` root, an exclusive owner lock, inode and
token binding, a separate staging directory, and its own child `TMPDIR`. Pytest
fixtures and other descendant temporary output therefore remain within the
completion scope. A PID-bound entry marker prevents a nested invocation from
mistaking an inherited environment variable for its own admission.

An atomic source/profile/runtime claim also prevents two supervisors from
verifying identical input simultaneously, even with ample space. Release
coordinators set `VERIFY_BEFORE_SYNC_OWNER_KEY` to their stable
publication target key: this additionally excludes a superseded candidate for
that same publication scope, including different runtime inputs or tree profiles.
Independent keys still run concurrently when their
aggregate byte plan fits. Claims survive uncertain termination and never expire.
This is not a global lane hold, nor does it prove that a legacy launcher which
does not participate stopped. Coordinators must still reconcile those writers.

Verified snapshots/digest outputs retain their existing publication behavior and
remain outside retirement. The live runtime, source worktree, ledger, Brain,
user files and arbitrary caches are not cleanup targets.

## Finish and preserve evidence

After the original child is reaped, the supervisor requires its process group to
be absent and independently checks process arguments and open files, including
self-visibility. Inspection denial, blindness, an active descendant, or changed
ownership preserves the data; there is no alternate signal or deletion fallback.

Before retirement it persists source identity, result, paths, byte measurements,
and up to 64 KiB of diagnostic output in a private receipt. If the source identity
changed, the staging tree is retained because it might hold unique old source.
`VERIFY_BEFORE_SYNC_KEEP=1` continues to retain the run explicitly.

For unchanged source and proven terminality, the private directory is moved to
its named `.retiring` boundary, checked again, and removed with descriptor-relative
symlink-safe deletion. Nested filesystems refuse retirement. The original source
and published artifacts are retained, so disposable copies/fixtures can be
recreated; no full cleanup archive duplicates them.

Intentional read-only fixtures do not stop independent writable siblings from
being retired. Permission-denied entries and their nonempty ancestors remain in
the named `.retiring` container with an owner marker and exact receipt paths.
No denied entry is retried and no permissions are changed. Unexpected I/O errors
still stop retirement and preserve the remnant for inspection.

A failed verifier retires only its owned child `tmp` fixture scope. Its staging
tree, full verification log and state copy remain: the live input may have changed
and a failure may need those exact bytes. These retained failure records require
source-owned triage before later retirement; they are not recursively archived or
copied into new runs. The receipt keeps the original wrapper exit code. A cleanup failure/interruption records
the exact remnant and keeps it for inspection, without blind replay. After
positive no-writer proof, failed deletion creates no fictitious future allocation:
the remnant is already counted by actual free bytes. An interrupted supervisor or
uncertain descendant instead retains its reservation and directory.

## Accounting and recovery

Default control state is `<runtime>/.storage-lifecycle`, overridable through
`ORCH_STORAGE_STATE_DIR`. Receipts are small metadata records, not nested source,
state or fixture archives. The copy path explicitly excludes this control tree.

File-size forecasts deduplicate hardlinks and record allocated and logical bytes.
They use the larger value for a prospective copy because shared/sparse extents may
materialize. Neither allocation totals, clone counts nor unlink counts are treated
as reclaimed space: every completion records a fresh `statvfs` measurement.
Shared-volume growth measurements can include concurrent activity and are labeled
accordingly. APFS sharing can make a large directory yield little physical space.

On an uncertain receipt, inspect the named original/retiring path, owner identity,
child group and open-file evidence before a separately authorized recovery action.
Do not erase a reservation merely because its owner is old, force a lock, retry a
denied operation automatically, or classify an unresolved result as success.

Focused tests cover independent concurrent admission processes, staging ownership,
source changes, failed runs, inspection denial, interrupted supervisors/cleanup,
successor directories, aliases, sparse/shared allocation, peak forecasts and actual
free-byte reporting. They use tiny private fixtures, not the release suite or live
cleanup.

## Integration and deployment acceptance

Repository entry points are wired, not merely given a helper:

- `ReleaseRecord.verify_and_install` passes the shared `ORCH_STORAGE_STATE_DIR`
  and derives `publication:<logical mirror path>` from the publication target.
  The final generation symlink is not resolved, so the key stays stable across
  installed generations, candidates, journal files and lane owners.
- The executable sync-wrapper source in `docs/MIRROR_SYNC_PATCH.md` derives the
  same key and shared ledger. Its extracted-wrapper integration tests assert
  those environment values. The source is not the installed native wrapper.
- Direct nonpublication `verify_before_sync.sh` calls share the default ledger
  and source claim. An explicit ledger override must be agreed by all launchers;
  separate ledgers cannot coordinate physical demand or publication ownership.

Activation requires the release coordinator to inventory the actual cooperating
launchers, install this reviewed source through the normal verified release, and
confirm that each invokes this wrapper with the agreed ledger and publication
key. Ad hoc project release drivers and older installed wrappers are legacy
until updated and witnessed. Helper availability alone is not fleet adoption.

Before activation, reconcile every known legacy invocation using its actual
source/candidate, journal, PID/group terminal receipt and fresh handle evidence.
Keep uncertain writers and their data; elapsed time and a new ledger claim are
not terminal proof. This is an explicit ownership handoff for the publication
target, not a new global lane hold. The frozen active release is outside this PR.

Deployment acceptance must witness two changed candidates for one publication
key refusing overlap before the copier starts, while an independent target can
be admitted when its byte plan fits. Then witness terminal success/failure,
retained failure state, permission remnants and fresh available-byte receipts.
The focused tiny integration/concurrency tests cover those contracts; no live
large verifier or runtime installation is performed by this implementation task.
Keep tested, integrated-in-source, merged and installed states separate.

## Native agent temporary output

A child carrying the supervisor's `ORCH_STORAGE_RUN_ROOT`, `ORCH_STORAGE_RUN_ID`,
`ORCH_STORAGE_OWNER_TOKEN` and explicit shared `ORCH_STORAGE_STATE_DIR` routes native
`TMPDIR` to `<owned-root>/tmp/native-tmp/<agent>`. The shared ownership validator checks
private physical directories, the owner marker, active reservation/token/inode/device
binding and per-agent directory identity before launch. The explicit ledger remains
the same when strict verification redirects its ordinary runtime/state into staging.

Only TMPDIR moves. Existing XDG caches, configuration, authentication, generated GH
guards, canonical packets, logs and retained offload worktrees keep their current
paths. An absent job context preserves the previous behavior. A partial, changed,
uncertain or aliased context produces a named no-start refusal; Closer keeps that
as recoverable pending work rather than inventing an uncertain provider execution.
The existing supervisor's terminal group/open-file checks and retirement own this
scratch. This does not add orphan adoption, expiry, permission repair or cleanup of
shared native runtime directories. Direct launchers must pass their actual admitted
job markers; setting TMPDIR alone does not establish native storage ownership.

Terminal receipts also record `terminal_owned_footprint`: logical bytes, allocated
bytes, entry count and unique inode count measured during retirement's existing
mandatory filesystem scan, with hardlinks counted once. This is the owned tree at
terminal inspection, not its peak, an exclusive growth attribution or a physical
reclamation claim. Fresh `available_after` remains the physical-space observation;
permission remnants and failed retirement remain explicitly recorded.
