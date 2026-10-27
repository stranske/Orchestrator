# Closer hold follow-up

The existing campaign `tick` owns follow-up. It consumes a bounded number of due
holds before model-capacity routing; it does not install a scheduler or submit a
model request to wake the coordinator. Native round admission remains signed and
fresh even when no model role is qualified.

Each durable hold is bound to the campaign owner, release/configuration pins,
original generation, exact target/frontier/revision, source row and run or
operation. It retains its release predicate, next check/event, deadline,
escalation owner, attempt budget and check identity across restart. Recapture
cannot reset the deadline or budget. Expiration records escalation and grants no
action authority.

## Execution knowledge

- **Confirmed not started:** qualified role/capacity recovery can use the existing
  finite-reentry planner and audit. Re-entry begins at fresh PREPARE, retains the
  original source requirements and does not copy a previous acceptance result.
- **Provider outcome unknown:** keep the original provider run owned. Absence of a
  terminal API is a wait/escalation condition, never permission to resubmit.
- **Action outcome unknown:** reconcile the exact original operation. The
  reconciler may CAS-settle its original Core fence after remote readback; it
  cannot execute another action or invent an outer completion receipt.

Same-assignment native rearm requires the actual original worker's authenticated
terminal boundary, either the exact no-start reentry audit or a correlated
terminal sidecar, fresh admission and final owner/deadline checks. It advances the generation once. Live/unknown workers,
changed source rows, denied campaigns and expired holds cannot open another
cohort.

## Accepted verifier requests

An accepted POST is distinct from a terminal verifier result. Closer's request
basis is distinct from the verifier's source-request/material event digests.
Future run identifiers cannot be inserted into the original immutable action
binding.

The coordinated interface is an optional `verification_request_basis` dispatch
input, retained in authenticated Workflows manifest/markers. It is correlation
only: it changes neither event identity, verdict selection, deduplication nor
retry authority. An original accepted request must retain proof that this input
was sent. Missing legacy correlation does not justify a guessed match or a new
request.

New normally admitted initial requests use the existing workflow-dispatch route
with this input, after the existing preflight, freshness and Core guards. The
acknowledgment is joined to the exact succeeded original Core intent and retained
in the native queue. HTTP 422 remains confirmed not started; there is no fallback
POST. Deployment of this route requires the matching qualified Workflows input
and collector/marker interface. Existing accepted label requests and unknown
outcomes are never migrated or resent.

A current authenticated terminal marker must match the original sent basis,
head/merge and original authored source requirements. A deduplicated request can
instead use a trusted `verifier-request-correlation/v1` alias referencing the
normally selected exact terminal event, assessment, run/attempt, source comment
and canonical full decision digest. The alias supplies no verdict. Malformed or
ambiguous correlation disables follow-up without changing ordinary semantic
verdict selection or lifecycle conflicts.

The durable sidecar preserves the exact terminal proof and original native
worker boundary. Rearm opens only the original full cohort through fresh normal
native admission. The existing dispatcher successor creates a new frontier and
runs fresh PREPARE; the original completed ACT and request queue stay unchanged.
Unrelated comments and update timestamps do not change the semantic follow-up
proof, while source requirements, checks, holds and unresolved review facts do.

## Original CI waits

A pending CI callback can advance only after the exact retained check run reports
success on the original head, all fresh checks/readiness pass, and the original
source requirements still match. The initial queue proof is checked under
its ledger lock and must show no operation, effect history, or uncertainty. It is
retained with immutable queue bytes and the causal successor link. Missing proof
never establishes no-start. Failed checks retain their exact check/test reference
for the existing corrective lane; pending or mismatched checks remain owned.

The existing CLI driver follows at most eight exact linked stage descendants per
target, within its original cohort and normal admission. Each child runs fresh
PREPARE and the existing guarded action. Restart reuses the same linked child;
UNKNOWN descendants cannot allocate a new child or retry an uncertain effect.
The root receipt and full assignment remain the native TOP boundary. Actual
public CLI fixtures cover CI success through merge and verification wait, then
restart with one merge and one request total and zero Closer model calls.

The pinned native hook independently reads the actual descendant receipts and
causal journal before signing a bounded immutable bundle. It retains the root
full cohort, original admission, worker handoff and pre-run watermark. Historical
receipt reuse requires an actual prior known worker result and finalized cleanup;
UNKNOWN descendants remain owned UNKNOWN and cannot promote root completion.
Campaign capture selects only the exact active leaf authenticated by that bundle.
Raw child JSON and uncovered historical receipts supply no release authority.

Offline composition covers signed native admission and cleanup, the actual CI to
merge/request path, original Core correlation, terminal sidecar, same-full-cohort
rearm and restart without another POST. The read-only collector evaluates normal
successor applicability with workers enabled locally and apply disabled; executing
through that reader still returns inventory-only. Fresh native PREPARE retains
all model qualification, capacity and guarded-action gates. All-completed child
lineage retains the existing root scoped-continuation boundary; this slice adds
no global-stop authority. These source and offline test results do not claim
deployment.

## Measurements

The summary counts observed deterministic direct ACT branches without retained
Closer classification, adjudication or repair runs. Downstream verifier
evaluation is a separate operation and can consume model capacity. The summary
does not estimate hypothetical saved calls. A bounded, read-only Brain lookup joins only the exact retained model runs to
persisted stage events. Those measurements provide latency/cost with explicit
coverage and unknown values;
independent false-acceptance/rework outcomes require separately bound evidence.
The native consumer does not infer independent outcomes from stage records.
A PASS or accepted stage alone never establishes either outcome. Hold reports
include ownership, due checks and age. Telemetry cannot authorize an action, and telemetry failure cannot block a
normally admitted controller tick.

## Staging

Implementation and synthetic test evidence are separate from package deployment,
host qualification and real model-role qualification. No live controller/security
settings, provider entitlements or production qualification records are changed
by these source changes.
