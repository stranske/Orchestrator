# 2026-10-04 — Gate renewal decisions for the 13 KNOWN_GATES rows expiring 2026-10-08

Owner decisions recorded 2026-10-04 (form submitted ~13:30Z), applied the same day with
`capabilities.py renew` from the running mirror. Every renewal extends the row 90 days, to
2027-01-02, and carries the measure that is checked on that date. Value-chain numbers below were
re-measured read-only against the live ledger, the redirect shadow corpus, the Brain and the
experiments directory at 12:35Z on 2026-10-04.

## Owner rule recorded with these decisions (concept-first evaluation)

A capability's record under the conditions it happened to run is evidence about its WIRING, not
about its CONCEPT. Demand is measured from the population of situations the capability exists
for, independently of whether it ran. "Hand use" is not a recommendation (nothing triggers it).
A test that cannot produce a real verdict is redirected, not scored. When an assessor is about to
conclude that a capability lacks potential, the first question is whether the evaluation itself
was deficient. Written into the repo CLAUDE.md, ADDING_CAPABILITIES.md, the repo-audit skill and
the global decision rule the same day.

## Decided

| row | decision | chain (right situation → usable → acted on → outcome) | measure on 2027-01-02 |
|---|---|---|---|
| role-redirect | FIX | 12 of 782 calls were real stalls → 2 usable → 0 applied → none. Bootstrap asked about ~760 healthy lanes; sweep defaulted to cursor, which failed 10 of 12 real stalls. | one usable proposal on a real stall acted on by the apply path, else retire |
| synthesis-promotion | FIX | 256 evaluated → 2 launched (no commit) → 0 → none. Latched gate: `exp_abcd.followup` re-touches `.last-ship-gate` for already-finished promotions on every run, so `launch_available` has been False since 2026-07-09; 254 expired unlaunched after 14 days. | fixture proves launches resume a day after a finish; next evaluated experiment launches within a day |
| completion-event-lineage | KEEP | 13,852 runs → 56,935 of 56,937 events accepted → 3,922 capability edges → 459 graded (204 PASS / 255 FAIL) | >99% accepted, graded links still accruing |
| value-chain monitor (new) | BUILD | — | PR through the admission gate; distinguishes situation count from invocation count |

## Held under the owner-question default (renewed; one click reverses to a decided retirement)

The owner rejected the expire/hold/hand-use recommendations on these and asked for a real
concept evaluation. Letting them time out on 2026-10-08 on the rejected evidence would be the
passive failure named in the feedback, so each is renewed now with the evaluation as its reason;
if the owner answers "let it expire" the row is retired as a DECISION (not a timeout) that day.

| row | owner's question | evaluation due |
|---|---|---|
| runtime-ac-checks | systematize AC verification across the lanes; is "completed with unmet ACs" measured anywhere? | this week |
| live-keepalive-supervisor | Hold is correct, but prove it works once role-redirect works (trace the downstream chain for obvious breaks) | this week |
| role-decomposer | "hand use" rejected: measure epic-sized demand across the fleet and wire a real trigger | this week |
| role-prompt | batches are filed all the time: why do batch sites never offer it; is role offering broken? | this week |
| role-adjudicator | redo the evaluation: measure contested-blocker demand and wire a consumer | this week |
| thompson-hybrid-routing | needs an A/B/C/D-style test to learn if/when it helps | this week |
| role-triage | explain the dispatch-lane retirement; find where ranking is still needed | this week |
| strategy-experiments | full concept test, never a verdict from the situation it happened to be tried in | this week |
| range-lane-rollout | same | this week |
| local-model-profile-trial | same | this week |

## Corrections to the first decision cards (same day)

The first card set carried a wrong or unchecked claim on 8 of 14 cards (triage "0 acted on",
905 declines, "2 dispatches", synthesis "throughput", Thompson "9 picks", a "recorded trigger"
that did not exist, and others). Cause: numbers reconstructed after a context summary instead of
re-measured. Rule saved: re-verify every number against a tool result before rendering.

## Owner decisions on the ten held rows (form submitted 2026-10-04, second sitting)

| row | decision | note |
|---|---|---|
| role-redirect + live-keepalive-supervisor | **Fix the whole chain** | three ordered PRs: router-chosen sweep backend + no placeholder agent; escalated PRs eligible + sweep stalls reach apply + recording on with a named consumer; disagreement credit for any differing applied action + tolerant link step + an open gate triggers the plan |
| runtime-ac-checks | **Build the shadow spec and gate measurement** | auto-authored specs on new keepalive PRs; gate in shadow; weekly "merged with unmet ACs" beside the verifier's 57; closer consumes only after zero false FAILs |
| role-prompt | **Run the batch test** | issue-body output mode + batch call; bind to `repo-audit:phase-4` and the declared `research-program` surface; lift the per-cycle cap for a batch; reclassify the 23 declines as wrong-moment |
| role-adjudicator | **Run the shadow test on the 71 disputes** | retrospective first, then live-shadow at the closer's disposition step |
| role-decomposer | **Measure quality by size, then decide** | the size → delivery-quality join decides whether step 2 (wire at the opener's bucket and epic filing) runs |
| thompson-hybrid-routing | **Need more info** | owner: "the research program doesn't run regularly. Where else can this be tested? And can't you use the previous live data more effectively?" — answered in the capability program (offline replay on 90 days of graded runs now; the keepalive delegation population through the route-weights export as the regular live bed) |
| role-triage | **Shadow-rank the opener's candidates** | plus grade the 28 ungraded disagreements |
| strategy-experiments | **Run one strategy experiment on a recurring shape** | |
| range-lane-rollout | **Run one codemod campaign through the lane** | |
| local-model-profile-trial | **Build the ingestion and run the two-profile trial** | |

Owner's standing instruction with these decisions: do not wait for Nov 15 — find productive repo work
that tests each capability now, and keep an artifact that works through every unused capability
(`2026-10-04-capability-program.md`, and `~/.codex/automations/capability-program/`).

## Owner decisions from the capability audit (form submitted 2026-10-04, third sitting)

| item | decision |
|---|---|
| coverage-autopilot | **Resume with the transient fix** — classify the `gh issue view` rc 1 failure as transient (retry), then resume the paused tracks; testgen-lane heartbeats from `delegate task_type=testgen` |
| rail exercise recording | **Yes, after the fixture class lands** — set `ORCH_RAIL_EXERCISE_RECORD=1` only once `fixture_observed` provenance exists (program unit U-PROV) |

Owner, same sitting: the program's issues must be filed soon; nothing may delay them.
