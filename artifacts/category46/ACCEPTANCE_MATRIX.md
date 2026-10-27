# Closer categories 4 and 6: acceptance and measurement checkpoint

Approved baseline: **closer-implementation-work-plan.pdf**, Library
`libfile_bd7dafc02b1c81919f9a70ee0e0c8a4e`, **version 5**, six pages,
modified 2026-10-25 21:43:15.893380 UTC. This matrix follows the plan's
implementation directions and preserves original task/criterion inventories;
it does not certify those original inventories complete.

Exact source base: **234d097bae302fe41c7c1e302d6871c8cf43a77e**.
Isolated branch: `codex/category46-acceptance`, worktree
`/tmp/orchestrator-category46-acceptance`. Only the expressly handed-off
`src/closer_stage_feedback.py` is changed in production source. Other writers'
modules and existing test files are untouched. New acceptance test, offline
measurement adapter, fixture, and evidence files are additive. See
`source-bindings.json` for file hashes and test result bindings.

## Six-category readiness

| Category | Owner | Concrete evidence / remaining gate |
|---|---|---|
| 1. Decision contracts and confined corrective routing | Core / execution-contract writer | Existing deterministic and refusal cases pass. New named-criterion/mechanical contracts need integrated composition and their actual owner/executor consumers. |
| 2. Model qualification | Candidate qualification owner | Role filters and shared pools tested; exact production profile/transport qualification remains unmet. |
| 3. Durable recovery | Core / hold writer | Selected release/rearm/expiry/unknown-fence tests pass; aggregate-history correction and escalation delivery/ack consumer remain unmet. |
| 4. Combined acceptance | This branch, core owns final broad run | 73 selected causal cases and 70 metrics/plumbing cases pass on the recorded base plus scoped correction; installed end-to-end proof remains unmet. |
| 5. Existing hook deployment | Deployment-preparation owner | Installer tests are preparation; current demand admission, qualified package and actual authorized installed receipt remain unmet. |
| 6. Efficiency measurement | This branch | Existing metrics reused; POST undercount corrected; representative fixture and adapter tested. Matched live baseline, repeat-classification join, missed-wake and escalation-ack data remain unmeasured. |

| Plan requirement | Evidence at this version | Assessment / next owner |
|---|---|---|
| pp1–2: retain coordinator responsibilities, task/AC identity and valid stopping | Source campaign/hold consumer and actual CI→merge→POST receipt inspected; selected restart test passes. | Partial source evidence. Core owns complete named decision/corrective handoff integration. Original task completion is not inferred. |
| p2: ready bound evidence goes directly to ACT without semantic model | `test_closer_role_controller.py` actual tick/admission tests; actual authenticated descendants produce merge and POST with provider count zero. | Source fixture PASS; installed entry unproved. |
| p2: collection/watch/reconcile make no semantic calls | Missing-delivery and evidence-pending restart tests; original CI→merge→POST restart; unknown-action reconcile-only tests. | Selected causal source tests PASS. Full installed collection/capacity/provider recovery composition awaits qualified release. |
| p2: bounded classifier and one named unresolved strong decision, coverage/stale bindings | Versioned semantic cache test; explicit unknown-need exception cannot self-certify; role eligibility/shared-account tests. | Existing slice PASS. Typed named-criterion/finding contract is in active execution-contract worktree, absent from frozen base: integration UNMET, core owns. |
| pp2–3: mechanical-only confined writes and mandatory tests; substantive repair handoff | Actual substantive reply is refused at exception repair. New execution-contract source inspected read-only: required-test registry empty and executor explicitly not enabled. | Refusal slice PASS; complete registered recipe/executor and actionable source-owner corrective consumer UNMET, core owns. |
| p3: exact binary/config/model/effort/role qualification and confined transports | Existing role selection recheck and shared-capacity tests PASS; current role source head ffa747fce4914e8cd4f0b0fdf23525e120fad645. | Production qualification and runtime provider proof UNMET, category2 owner. No new provider call made here. |
| p4: hold release/rearm, original bounds, expiry and no lost hold | Hold safety module exercises source drift, original attempt/deadline preservation, release/rearm, process death, expiry and exhaustion. | Selected tests PASS; history aggregate-budget hardening remains active under core owner and must join final composition. |
| p4: provider/action outcome unknown stays fenced; no duplicate operation | Unknown action cannot adopt another operation; unknown-effect probe cannot release retry; interrupted probe keeps original unknown; authenticated descendant restart avoids replay. | Selected source tests PASS; remote/live proof absent. |
| p4: escalation named recipient/destination, acknowledgment state/window and consumer | Hold source has durable escalation status/owner; core confirms delivery/ack consumer absent. | **UNMET**. Core owns narrow delivery/ack contract. Status or owner is not a delivery receipt. |
| p5 Phase4: qualified installed pin/hook, due/no-demand, overlapping old/new trigger, restart | Category5 owns installer/wiring tests; existing installer alone does not supply current demand admission. | **UNMET installed evidence**. Category5/core must supply exact qualified package, current demand admission and actual hook/receipt readback under deployment authorization. |
| p5 Phase5: same-candidate producer/caller/consumer receipts through terminal reconciliation | This branch joins actual local authenticated root+two descendant receipts, with synthetic external services. CI wait excluded; completed merge and POST counted. | Source composition PASS only. Installed end-to-end gate UNMET. Source-closure actor remains opt-off. |
| p5: quality, calls, cost/quota, wall time, rework, repeat classification, hold age, missed wake, escalation ack | Existing efficiency summary reused; new offline CLI and representative fixture verify plumbing; measured coverage preserved. | Plumbing PASS. Live baseline/savings, repeat-classification ledger join, scheduled wake delivery and acknowledgment data UNMEASURED. |
| p6: do not replace shadow trial or expand held nine-PR batch | No candidate dispatch, benchmark replacement, batch operation, live setting, schedule, credential or deployment mutation. | Boundary preserved. Existing shadow owner retains seven-day/ten-pair/human promotion protocol. |

## Local acceptance evidence

`acceptance-selected.xml`: **73 PASS**, 9.79 seconds, selected existing causal
cases. `focused-metrics.xml`: **70 PASS**, exact result duration retained in
JUnit. These are two disjoint invocations, not a repository-wide suite.
The core owner explicitly owns the single final broad composition after its
history fix. Counts from its prior broad runs are not added to these counts.

The new composition test first reproduced the handed-off defect on the frozen
base: three authenticated receipts yielded denominator **1**, direct **1**,
excluded waits **2**. Correction requires the producer's authenticated resource
proof, the exact queue receipt hash, original operation/event/head/merge/frontier
bindings, completed queue operation and submitted/advanced events. It yields
denominator **2**, direct **2**, excluded waits **1**, model runs **0**.
Twenty-one altered proof/binding/completion/causal-event cases remain excluded.
The summary is a read-side join over already authenticated receipts, not a new
authentication boundary or action authority. Pending metadata alone is excluded.

`authenticated-descendant-summary.json` contains measured local stage durations
for the actual fixture producers. The downstream verifier outcome, its model
usage/cost, false acceptance and rework remain unknown. Completion of the POST
does not mean completion of verification or source acceptance.

## Measurement baseline and use

Run:

```sh
python3 scripts/measure_closer_acceptance.py \
  tests/fixtures/closer-category46/representative-workload.json \
  --output artifacts/category46/representative-measurement.json
```

This reuses `closer_stage_feedback.summarize_efficiency`; it does not create a
second metrics system or touch the live Brain. The representative input has nine
branches: merge, POST, evidence collection, CI watch, capacity watch,
reconciliation, classifier, named strong decision and mechanical worker, plus a
duplicate receipt. Fixture role outcomes, costs and timings are deliberately
synthetic. Its result is five observed branches, two direct zero-reasoning
branches, three retained model identities and four excluded waits. Cost coverage
is 2/3; the missing strong-role cost remains unknown. Those numbers exercise
accounting, not execution of the modeled routes or live savings.

The adapter measures its own wall/CPU time and process peak RSS separately.
Provider latency/cost comes only from retained events, with coverage. Summed
stage durations are not end-to-end wall time; process RSS is not per-role cost.
Due hold count is not missed-wake count. Stage acceptance is not an independent
false-acceptance label. Duplicate receipt deduplication is not proof that repeat
classification was avoided. No live saving is claimed.

Before comparison, retain matched baseline/candidate installed pins, same original
tasks/criteria and evidence identities, observation windows, per-role run IDs,
actual usage/quota/cost coverage, independent false-acceptance and rework labels,
scheduled-due/actual invocation receipts, and exact escalation delivery/ack IDs.
Retain unknowns explicitly. Compare quality and rework with resource consumption;
do not replace the existing verifier shadow trial.

## Exact unmet acknowledgment acceptance case

On deadline or exhausted budget, persist one escalation bound to the exact hold,
target/revision and responsible recipient/destination. Restart and reordered or
duplicate wakeups must reuse that escalation. A durable acknowledgment must join
the exact escalation/recipient identity; wrong or stale acknowledgments cannot
settle it. Beyond the configured acknowledgment window, the consumer must retain
and surface the overdue unacknowledged state without granting action authority.
Measure delivery and acknowledgment timestamps only from actual receipts.
Owner, destination, recheck/deadline policy and acknowledgment window need the
existing responsible owner's configuration agreement before activation.

## Milestone for maintained plan/diagram

Implemented: handed-off POST accounting correction and offline measurement
adapter. Source-tested: selected causal safety slices and authenticated local
descendant accounting. Qualified: no new production profile. Installed/enabled:
none. Remaining gates: integrated named contracts/recipe and owner consumers,
history hardening, escalation delivery/ack, exact transport/profile qualification,
current demand admission, qualified package plus authorized installed end-to-end
receipt and measured matched baseline.

Checkpoint message to parent thread `01a171a0-b25a-729e-8a45-595d3653a0c6`
failed with **thread not found**, known not delivered. Core owner messages
succeeded. No denied or uncertain external action was retried.

## Independent review correction

Review of the original accounting patch found that matching a frontier ID and
target list alone accepted a descriptor missing its policy and bindings. The
correction calls the existing `validate_frontier(frontier, targets=[target])`
fail-closed and joins its bound revision to the retained branch revision. Seven
additional joins reject missing policy, wrong schema, missing owner, target digest
drift, binding digest drift, malformed action binding with a recomputed digest,
and a validly rehashed but wrong branch revision. The focused metrics/plumbing
run remains **70 PASS**, now with **21 negative joins**, in **6.11 seconds**.
The 73-case selected acceptance result predates this narrow metrics correction;
its original source hashes remain attached to that result in `source-bindings.json`.
No broad rerun, live baseline, escalation acknowledgment or deployment claim is added.
