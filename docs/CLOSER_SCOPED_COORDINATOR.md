# Resource-scoped closer coordination

The dispatcher now integrates the staged coordinator. Default mode remains `legacy`.
Use `closer_dispatcher.py run --pipeline --coordinator-mode resource_scoped` with the
complete `--coordinator-contract-pin` advertised by this version. The CLI rejects missing
or incompatible pins. Scheduled launchers retain their default mode until supported
promotion and qualification for their explicit execution mechanism; source changes do not
enable a live lane.

## Ownership and state

Both cooperating modes acquire common per-target gates before reading admission state
and retain them through the receipt. The CLI passes a live, path/target-bound admission
capability to lower layers, avoiding a second lock of its own gate. Unknown or old writers
that do not speak this protocol must be observed terminal before promotion; age is not proof.

Scoped state lives in `<state>.scoped`, with a versioned contract and one unchanged pipeline
state document per canonical target. A target lease remains held across its work. Each
atomic state replacement rechecks the live lease, epoch, and original deadline plus commit
grace immediately before replacement. Files and containing directories are fsynced.
Disjoint targets do not share a whole-round JSON rewrite lock.

Historical quarantine uses the narrowest verified conflict closure. Content-addressed
snapshot and packet references bind the original PR, sources, branch and authorized
execution workspace. A fully bound remote GitHub operation needs no synthetic filesystem
scope; missing operation binding or an unverified local workspace retains the conservative
repository fallback. Exact supported observation-cache records are reported separately only
when their immutable packet/snapshot provenance and the preserving cache-writer contract
prove that they are observations. Native operation fields or history cannot borrow that
classification. Every original row and reservation remains unchanged.

An audited `closer-unverified-read-barrier/v1` may also carry a terminal, one-target
queue wait with no operation or operation history. The census verifies the retained
snapshot, audit chain, exact current receipt, revision and head with the existing
validators. Only native remote-effect admission excludes that row's inferred remote
aliases. Explicit original intent keys and every local/workspace key remain fenced;
local effects, live independent reservations, missing/mismatched proof and genuinely
uncertain operations retain their existing guards. This projection grants no retry,
acceptance or historical-state mutation authority.

## Effects and provider capacity

The dispatcher derives mutation aliases from prepared evidence: PR, original source issues,
head repository/branch, and authorized worktree. Fresh execution evidence must remain within
those reserved aliases and the prepared head. Common reservations are stored outside the
scoped root so staged and raw legacy writers cooperate even before the first scoped run.
Preparation and reasoning can proceed while another target holds conflicting effect aliases.

PR, issue, branch, and worktree intents survive callback return, exceptions, and process death.
Only the exact reservation owner can release them after the durable completed receipt.
An existing unknown intent blocks an overlapping effect, including a new owner of the same
target. Repository serialization carries no durable intent. Raw legacy execution also uses
fresh observed aliases; unrelated work can progress while prior unknowns remain retained.

Low and exception execution reserve the exact registered profile capacity pools across
processes for all six approved providers. Capacity is refreshed after reservation. A provider
exception after execution begins is not retried on another provider. Unknown cost stays unknown.

## Restart, rollback, and stop

Rollback retains scoped and historical reservations. Target-specific unknowns and live leases
fence affected targets; unreadable global contracts fail closed. Completed native effects use
the pinned Core's exact-effect reconciliation, preserving the original operation binding and
deadline. Verification-request delivery is distinct from verification acceptance.

Scoped ACTION C names an immutable round receipt, bound to its per-target run IDs and original
deadlines. There is no latest-round fallback. Empty, missing, foreign, or fenced accounting is
invalid. Raw and staged legacy stop checks include retained common reservations. These receipts
validate accounting; they do not perform native helper cleanup.

A terminal native round can separately prove containment without proving completion.
Every unsettled admitted row must exactly match its immutable aggregate receipt and one
independently regenerated quarantine item, including the original run, row hash and conflict
keys. Every unadmitted target needs a positive pre-callback refusal. Unbounded census damage,
missing rows or changed receipts refuse continuation. The native boundary also requires
terminal B, the exact reviewed C65 cleanup result and no live lane. Future rounds recheck
resource admission: the unknown target remains fenced while disjoint work may proceed.
Global stop and acceptance remain false; no original unknown, deadline or advertisement is
cleared.

## Evidence and remaining rollout limits

Tests exercise the actual CLI through subprocesses, mixed-mode first use, disjoint progress,
same-resource aliases, stale/expired writes, crash unknowns, capacity/auth failures, exact profile
pools, original-budget recovery, and incompatible-contract refusal. Collection is governed by
`.verify-floor.json` and reconciled on main; the pass floor is an expectation, and every
skip/exemption ceiling is unchanged.

The sleep-only timing fixture is not eligible-work throughput evidence. Active-worker promotion,
supported desktop/controller qualification, installed helper migration, and a real approved
low-effort response reaching a guarded queue action remain required. The installed generation,
explicit pauses, existing claims, quotas, and uncertain ledgers have not been replaced or cleared.


### Current verifier outcome and incomplete reads

An older terminal-incomplete chain with no semantic verdict must not veto a complete, admitted canonical review of the exact same repository, PR, head and merge. Existing applicability rules may supersede that older chain when every old node matches the complete anchor's request digest or material digest and predates it. Material binds code/diff, CI hard gate and required selectors; equal material does not mean unchanged requirements. The new full review assesses the revised authoritative request. Both digests changing, actual verdict conflicts, advisory or unadmitted results, newer observations and ambiguous causal chains retain their guards. All input markers remain in history; source-issue outcome ownership is unchanged.
