# Closer resource-scoped coordinator (opt-in, staged)

**Status: local patch, not integrated.** Default is `legacy`; nothing changes unless
`run_pipeline(..., coordinator_mode="resource_scoped")` or `ORCH_CLOSER_COORDINATOR_MODE=resource_scoped`
is set. Invalid values raise. `closer_dispatcher.py` does not pass the argument (other owner).

## Baseline
`run_pipeline` holds `<state>.runlock` for the whole round and rewrites one shared
`runs/carry/cache` JSON. A second coordinator waits to its deadline then defers
(`coordinator_lock_unavailable`), even for unrelated targets.

## Scoped design
- Storage: `src/closer_resources.py`. Root `<state>.scoped/` with `contract.json`
  (`closer-scoped-state/v1`, layout 1, pipeline schema) and one directory per target
  (`targets/<sha256[:24]>/`) holding `state.json` (the unchanged `closer-pipeline/v1` document for that
  target alone) and a lease (`lease.lock` + `lease.json`).
- Each target runs the **unchanged** `_run_pipeline_legacy` against its shard, so deadlines, receipts,
  resume IDs, carry/cache/unknown/recovery logic are the legacy code. No lock spans targets and none
  is held across callbacks (agent/CI/GitHub). Aggregate concurrency obeys the three pool bounds via shared semaphores.
- Exclusion: non-blocking `flock` held for the target's work; crash releases it, a live owner is never
  evicted. `lease.json` epoch + expiry (deadline + 120 s commit grace) is re-checked inside `_save`
  immediately before every atomic replace, so a superseded or expired holder fails closed.
- A contested target is deferred (`resource_contested`) in the receipt and writes nothing to the shard.
- Legacy ledger: read-only. Targets it leaves `running`/`uncertain` are fenced
  (`legacy_*_outcome`); an unreadable ledger fences every target (`legacy_state_unreadable`).
  The file is never rewritten (tested byte-for-byte).
- Shard errors fence only that target (`shard_error`). An interrupted `act` stays `running`, and is never re-acted.
- Version pin: `closer_pipeline.scoped_capability()` returns contract/layout/pipeline schema and
  `proven_runtime_promotion: False`; `coordinator_contract_pin={"layout":..,"pipeline_schema":..}`
  refuses a root that differs. A different contract is refused, never migrated.
- Scoped receipt has `mode`, `run_ids` (per target), `scoped.{fenced_targets, lease_wait_seconds,
  max_lease_wait_seconds, completed}`; no single `run_id`.

## Latched-gate check (lease)
1. Decrement: process exit/release drops the flock. 2. Clearing never needs the action the gate forbids
(release is the holder's own, or the OS's). 3. One window: lease expiry derives from the round deadline.
4. Drained prints: `fenced_targets: []`, and `max_lease_wait_seconds` is a measured 0.0 (not a sentinel).
Weakness: a hung live holder keeps its target fenced (deliberately, no force-clear); report only.

## Measured (actual processes, sleep-only act, 2 coordinators x 2 disjoint targets, one run)
legacy 0.74 s wall, scoped 0.40 s, 4/4 completed, max lease wait 0.8 ms. **Not throughput evidence:**
`act` is `sleep(0.3)`, and the test asserts completion only. Real comparison needs completed
eligible work / elapsed from live rounds against the legacy baseline, taken after integration.

## Integration seams added (follow-up 1)
1. **Rollback.** `run_pipeline` in default/legacy mode calls `closer_resources.rollback_fence` itself (no
   callback or global injected). Targets that a scoped shard leaves `running`/`uncertain`, or whose lease is
   live, get a fenced `deferred` row and legacy never acts on them; disjoint targets run as before; if every
   target is affected legacy is not run and no state is written (`mode: legacy_refused_by_scoped_state`).
   A shard that cannot be read or holds a malformed row maps to no target, so EVERY target is fenced
   (`scoped_shard_unreadable`). Drainable count is reported as 0 because only a scoped round can resolve a shard.
2. **Resource keys.** Optional `resource_keys(target, context)` returns `kind:value` keys (`pr`, `issue`,
   `branch:o/r@name`, `worktree`, `repo`); owner/repo is case-folded, branch names are not, unknown kinds raise.
   Only `act` reserves, so prepare/interpret/exception reasoning is never blocked by a reservation. The
   target's own `pr:` key and its `repo:` serialisation key are always added (conservative default). Keys are
   taken in lexical order, each a short `flock` around the one effect; waits are bounded by
   `RESOURCE_WAIT_SECONDS` (30 s) and the deadline. Durable kinds write an intent before the effect; an
   exception or crash leaves it `unknown`, blocking the next effect on that key only. A blocked effect is
   never started. The whole-target lease is still held across the agent run. `repo:` keys carry no durable
   intent, so an unknown never blocks the whole repository.
   Drain (latched-gate): the owning target reaching `completed` (e.g. via reconcile) removes its intents
   (`release_target`). Receipt prints `unknown_reservations` AND `unknown_reservations_drainable`.
3. **Scoped `validate_stop`.** `closer_pipeline.validate_stop_scoped(state, round_id, targets)`. Each round
   writes an immutable create-only `<state>.scoped/rounds/<round_id>.json` (round id, per-target run ids,
   shard dirs, original run deadlines). The caller names its round; there is no "latest". Each target is
   judged by the unchanged `validate_stop` on its shard, bound to the recorded run id (history after that run
   is hidden from the check; a different run id is `run_binding_lost`). Fenced targets, empty or missing
   accounting and a changed assignment are invalid; resume keeps the shard run's original deadline.
4. **Pools.** Lane bounds (the existing `max_io/low/exception_workers`, no new budget) are now enforced
   across processes by flock slot files (`pool_slot`). `pool_lease(lane, target, context)` is an optional
   callback returning a context manager so the root can reserve its real profile pools; without it only the
   lane-level bound is enforced. **Seam for root:** exact profile pools (dispatcher/routing) are not
   reserved by this code. All coordinators must pass the same bounds.
5. **Strict state.** A prior lease that is unreadable, not an object, has an epoch that is not an int >= 1, or
   names another target raises `CorruptShard` (target fenced `corrupt_shard`, record untouched); a shard with
   `state.json` but no lease is refused. Epoch 1 only for a shard with neither. Targets must be canonical
   (no surrounding whitespace/control chars; `Owner/Repo#N` folds to lower case; case-variant duplicates in
   one assignment are refused). Atomic writes fsync the file and its directory. A contract pin is checked
   before anything is created.

## Proven locally (tests/test_closer_scoped.py, 25 tests; separate processes, real `run_pipeline`)
Original: default legacy untouched; invalid mode refused; disjoint concurrent progress; same-target
exclusion; stale/expired lease cannot commit; crash keeps the unknown; GitHub-outage act; legacy ledger
byte-identical; pin/contract refusal. Added: legacy refuses scoped unknown while a disjoint target runs, and
fails closed on a malformed shard; two target ids sharing a case-aliased branch key never overlap in act;
same-repo acts serialise by default while prepare is not blocked; a crash leaves a durable unknown that
blocks only the sharing target and drains on release; lexical order and key validation; scoped validate_stop
per named round, with fenced/empty/foreign round rejected; io-pool bound across two processes; corrupt-lease
variants; canonical identity; bad pin creates no root; threaded fence registration. Break demos: removing the
rollback guard and disabling the reservation lock each turned their tests red. Existing closer pipeline,
dispatcher, queue, continuation and recovery suites (551 tests with these) pass.

## Measured (sleep-only act, 2 coordinators x 2 disjoint targets, one run)
legacy ~0.7 s wall, scoped ~0.4 s, 4/4 completed. **Not throughput evidence:** `act` is `sleep(0.3)`. No
benchmark claim is made; a real comparison needs live eligible work / elapsed after integration.

## NOT proved / actual limits
- Runtime promotion during active workers, release install and `closer_dispatcher`/launcher wiring: not done
  (root owns them). Nothing passes `coordinator_mode`, `resource_keys` or `pool_lease` yet; the dispatcher must
  supply `resource_keys` that map source issue/branch/worktree, otherwise only the default PR+repo keys apply
  and two ids for one source issue are NOT excluded.
- A reservation block or timeout surfaces as an act exception, which the legacy pipeline records as its
  ordinary act-failure row, not a typed deferral (that contract needs validated evidence). The effect was not
  started, but the row's retry semantics are legacy's.
- `scoped_unresolved` / `rollback_fence` read shards without a lock; a coordinator starting between the read
  and legacy's start is not excluded (legacy still has no scoped lease). Only live-lease probing narrows it.
- Pool slots: exact profile pools are root's; a hung holder keeps its slot until the process exits.
- Same-target contested coordinators defer instead of waiting. Capacity sampler still runs per target.
- Validator history is read from the shard (hidden-later-runs copy); the aggregate receipt is not signed.
- No Windows/non-POSIX support (`fcntl`). `.verify-floor.json` untouched; full `verify.py` not run.
