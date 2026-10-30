# Closer campaign runtime (`src/closer_campaign.py`)

Ownership: the Orchestrator owns this runtime (capacity routing, parallel workers, handoffs,
recovery, release management, desktop adapters) and its version distribution. Workflows owns the
portable closer core. Production loads `CoreBundle` from a closed package descriptor covering
Core source, native GitHub store/transport, helper dependencies and operation schema.
`PinnedPortable` single-file loading remains an offline compatibility/testing API. Thin desktop launchers pin a compatible runtime manifest; a runtime manifest
declares `contract = closer-operation/v1`, state versions, and machine compatibility receipts.

## Executing paths and validation boundaries

| Path | Entry | Notes |
|---|---|---|
| Durable record | `Campaign` | flock + atomic write; `generation` fences old owners; `takeover` only when every worker is terminal |
| Step | `step` / `reconcile` | intent journaled before the process starts; only `terminal` frees a slot |
| Worker | `closer_campaign.py worker` → `run_worker` | fences (owner, generation, worker row, claim) BEFORE any effect; runs the existing `closer_dispatcher.py run` through `run_dispatcher`; writes one durable result |
| Scheduled resume | `closer_campaign.py tick --root R` / `controller_entry` | discovers `*.campaign.json`, reconciles, launches under cap and fresh cached capacity (never runs `capacity.py`) |
| Release | `resume_release` / `release-worker` | exact CAS on candidate, revision, receipt digest, installed generation |
| Action fence | `submit_effect` + `CoreOperationAdapter` | Core-owned claim/intent/reconcile for preferred AND basic ports; no adapter => `held`, no effect; key = repo+PR+action (survives head/criteria), denial and unresolved states are barriers |
| Manifest/pin | `make_manifest`, `verify_modules`, `select_pin` | binds module digests, runtime, command, candidate, state versions |
| Runner producer | `produce_harness_receipt`, `produce_launcher_receipt` (`launcher-receipt`) | real subprocesses; the launcher producer starts each ENABLED configured launcher entrypoint (`closer_launcher.py compat-child`, scratch dir, no credentials, no repo) and the parent observes startup, handoff, SIGKILL of its own child, restart, durable result |
| Launcher | `src/closer_launcher.py` | thin entrypoint; pins contract, launcher version, module/package/state/runtime digests; exit 78 on any mismatch |
| Work unit | `worker_config.granularity` | `round` (default): ONE serialised unit = the whole closer assignment through `closer_dispatcher.py run` (its own `--max-workers` gives parallelism; bounded native wrapper described below). `target-delegate`: one real target through `dispatcher.delegate` on the router's agent/profile. Anything else is refused |
| Native barrier | `native_barrier` | real HANDOFF_DIR/.lane-closer.lock/meta and coordinator .pipeline.json.runlock flock block competing rounds; exact target claims and pending/unknown ledger operations remain item scoped; unknown owners persist until terminal cleanup, never TTL eviction |
| Capacity | `select_route` | existing router + execution_profiles over an existing capacity JSON; Aider excluded, Gemini kept; stale/partial => hold; the dispatched agent/profile is the selected one |
| Qualification | `qualify` | before ANY tick effect: valid manifest + modules, `promotion.json` accepted by `check_promotion`, state schema match, pinned Core loadable |

## Standing publisher closure (prospective v2 contract)

`closer-controller-demand/v2` may bind a separately captured publisher closure.
Its `publisher_closure` records a canonical immutable root, entry module/function,
per-module bytes and modes, plus a verified Source release receipt binding
(`source_snapshot_receipt`: receipt path/digest, candidate SHA, verified snapshot
path/digest, source identity) and capture-tool binding
(`source_capture_tool`: implementation member name and digests tied to the standing
`closer_publisher_closure.py` loader); the
mutable controller data root remains an explicit contract field.  The campaign
validates the closure before a tick or publisher call and rejects preloaded local
publisher modules.  A v2 hook reaches the campaign before importing the mutable
`demand_router`; the entrypoint is loaded only from the captured closure.

The code and data roots must be disjoint. Declared dynamic dependencies carry
exact module-name/source-path bindings and captured bytes, including the local
liveness guard, maintenance budget reader and dependency-owner lock helper.
Module `__file__` preserves the declared data origin for compatibility;
`__spec__.origin` and compiled filenames identify the captured code. Temporary
import hooks and publisher module-cache entries are restored after each call.

`scripts/closer_publisher_package.py create` requires the actual
`closer_publisher_closure.py` implementation as its capture tool and a verified
Source receipt containing that member. The pure, runtime-pinned
`source_snapshot_digest.py` checks the complete snapshot; it never imports code
from the supplied snapshot or a mutable checkout. Publication claims a new
directory exclusively and retains `.publisher-incomplete` until all files and
the final-root descriptor are flushed. Readers reject that marker, partial
inventories and raced foreign destinations. A failed claim is preserved for
operator reconciliation; an existing package is only verified and read.

After a successful publisher call, closure drift preserves that original result
but holds demand admission/acceptance.  It never retroactively asserts that the
already-observed call had no effects.  This is an opt-in compatibility path;
existing v1 controller-demand manifests retain their current behavior.

## Outcomes
`ok | unavailable | denied | unknown`. Offline (`ConnectionRefusedError`) is `unavailable`;
`PermissionError` / exit 77 is `denied` and holds with the exact reason; a timeout, signal death, any
other failure of a possibly-writing run, or a worker that vanished without a result is `unknown`:
the target moves to `reconcile` and is never relaunched. Core records intent first, so a crash mid-submit stays a barrier; Core will not mark a port's `ok`
terminal without typed reconciliation evidence, so the target/action stays `unknown` until `reconcile`.

## Scheduled integration (default, no new cadence)
`scripts/install_closer_campaign_hook.py --controller C --manifest M --out O` renders a controller
whose `main` loads `closer_campaign`, `closer_release` and `claims` from the manifest's paths after
re-hashing every pinned module (a plain `import closer_campaign` from the controller directory is not
used). Any mismatch falls back to the unchanged `demand_router.main`. On each existing scheduled run
it calls `tick_safe()` (never raises; inert without `<ORCH_STATE_DIR>/closer-campaigns/pin.manifest.json`;
`ORCH_CLOSER_CAMPAIGN_TICK=0` disables) and then the original path. Not deployed by this change.

## Release resume
`acceptance_pending` (#794, revision 6) plans only `await_acceptance` with `reverify=false,
reinstall=false` and launches nothing. `verified`/`installing` reuse the sealed receipt. The same
attempt still running → `wait`; dead attempt without result → `reconcile`; same verifier fingerprint
already ran → `skip_unchanged`. Newer state schema → `IncompatibleState` before any write; package
rollback is refused when state versions differ.

## Gate math (latched-gate check)
Cap `max_workers` (1..4) is decremented by `reconcile`, which runs while the cap is full; cap and drain
count the same set; `status` prints `workers=N/cap drainable=M` and `complete` when drained. Core
barriers drain only via Core reconciliation (read-back), which runs while the fence is closed to
that key. Native-owner holds drain only by the native owner's terminal cleanup; the tick reports
the holder it is waiting on. An unqualified pin is inert and says which receipt/state/core check failed.

## Production action binding

`Closer.execute` retains live snapshot/revision/action checks and runs its original native
mutation body through `RuntimeBinding.execute` → `submit_effect`. The production adapter
constructs Core's GitHub Contents CAS store and native transport from an already supplied
GH_TOKEN/GITHUB_TOKEN only. It never reads credentials elsewhere or substitutes a local
store. Original merge guard and Tasks/AC evidence remain mandatory. `initiate_verify` and
`post_merge_verify` normalize to Core's `verify_request` action. `respond_review` and `close_source`
are explicit unsupported capabilities; `reconcile` is never a mutation alias. Submitted
success/unknown operations remain Core barriers until independently reconciled.

`--runtime-manifest` opts the dispatcher into this path and propagates to child action
backends. Missing/invalid binding blocks remote actions; local preparation remains possible.
An old invocation without that option retains native deployed behavior. New campaign
launchers always pass the option and use `--apply --workers --pipeline` together.

## Pause and qualification boundaries

A configured worker TOML must say ACTIVE, and a registered policy reader must supply a
fresh app readback bound to the hashes of those TOMLs. Missing, PAUSED, stale or unsupported
app state holds scheduled writes even if the controller is ACTIVE. A caller-written JSON
policy success cannot register a reader. There is no provider or cadence change here.

`qualification_request` gives the desktop controller a package/state-bound request for
all enabled runners. A registered observer must independently read native desktop events
(including authorized read_thread tool results), app/run/thread/producer identities and
hash-bound phase artifacts. Merely changing a receipt's kind to desktop-runner fails.
No desktop observer is installed by this local patch; default qualification remains held.

## Native round and packaging boundaries

`run_native_round` reserves the original deadline plus receipt reserve, calls A once,
executes B once and attempts C once after a started round, including timeout. Its native
hook must validate the original exact lease token inside cleanup; a caller-side check
before spawning an unconditional cleanup script cannot protect a successor. The current
installed postrun has an unconditional rm -rf trap and cannot satisfy that contract.
The production default therefore holds with an explicit unsupported adapter reason.
A failed C retains C65/no_matching_pipeline_run and all uncertain ledgers. Unit tests use
an injected token-aware native hook, never a native queue run.

`closer_distribution_package.py` integrates the terminal Runtime packager API with the
actual closed production module closure. Public descriptors contain own source hashes,
state versions and compatible Core pins. Host paths, interpreter, policy and Core install
root belong only to the private host manifest. Core packaging stays Workflows-owned.
Relocation tests invoke the real Core CLI and Runtime/Launcher, preserve the missing
policy hold, and reject source/schema drift. No release upload, install, or desktop
acceptance is claimed. Declared dependency installation and live scheduled successor,
replacement and guarded response completion remain unverified.


## Staged ownership rollout

The resource-scoped pipeline mode is opt-in. It retains durable resource intents
across callback returns and releases only the current executor's reservations after
a durable completed receipt. Historical unknowns need independent, resource-bound
reconciliation. Exact-target leases and explicit named-round stop receipts preserve
original deadlines. A legacy/scoped transition must also preserve resource exclusions;
the installed lane protections remain in place until actual entry-path, shared-pool,
rollback and runtime-promotion validation completes.

Native helper previews use `HANDOFF_EXACT_TOKEN_V1`. Runtime pins both scripts and
the lease module; acquisition refuses every existing owner, and cleanup compares the
complete original meta credential. Temporary stub-shell tests exercise the actual
Runtime adapter and real lease module. They do not qualify an installed desktop runner.
