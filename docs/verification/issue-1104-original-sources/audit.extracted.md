# 2026-10-04 — Audit: rarely used or frequently ineffective capabilities, and the assessor's role

Owner's question (2026-10-04): do the same patterns of failure in the agent's work that produced the
wrong gate-renewal verdicts have a role in the bad outcomes of the OTHER capabilities? Population:
every live ledger row (47). Window: 90 days to 2026-10-04 14:15Z. Sources, all read-only: the
capability ledger, the Brain (influence edges, outcomes, runs), `capability-selection-detect.json`
(2026-10-01), `capability-firing-monitor.json` and its history, the advisor's declared bindings and
preconditions, the running mirror's `orchestrate.sh`, GitHub search, and the cadence stamps.
Companion record for the thirteen expiring rows: `2026-10-04-gate-renewal-decisions.md`.

## Answer

Yes. Five system-level patterns, each one an assessor habit written into the tooling, account for most
"rarely used" and "ineffective" readings. In each case the reading measures the measurement, not the
concept, and in two cases a working production path was invisible to the ledger that judged it.

### P1 — Offers made before the situation exists (register A1, A5 at scale)

The lanes consult the advisor once per round, at round start. Five capabilities whose situation is a
property of the ITEM (a repeated pattern, a multi-repo change, a high-stakes PR, a coverage gap, an
acceptance spec) were offered on every round regardless: testgen-lane 1,423 offers / 535 declines /
3 invocations; codemod-campaign 1,292 / 547 / 4; cross-repo-coordination 1,387 / 487 / 1;
adversarial-review 1,448 / 533 / 7; runtime-ac-checks 2,549 / 994 / 4. Every decline is unkinded in the
ledger; the reasons read "no multi-repository source change", "no repeated mechanical migration",
"exact source claims reproduced directly" — correct declines of a wrong-moment offer. The
precondition table exists for all five (`CAPABILITY_PRECONDITIONS`: repeated_pattern,
multi_repo_change, nontrivial_change) but its probes return "unknown" without PR facts ("PR size
unknown", "no title, labels or size to judge a pattern by"), and an unknown precondition still
offers. adversarial-review's declared situation, a `risk:*` label, occurred on 0 PRs in 90 days.
The role capabilities are the same pattern at the skill surfaces (see the companion record).

### P2 — Fixture verdicts recorded as production usefulness (register A3 at scale)

`rail_exercise.py` records every passing fixture contract as `useful=True, provenance=machine_observed`,
the same class as a production outcome. For about twenty live rows that is the ONLY usefulness
evidence (flag FIXTURE-ONLY below: completion-event-lineage, feedback-store, issue-readiness,
research-scheduler, stall-watcher, evidence-acquisition, coverage-testgen-trigger, docs-drift-fix-agent,
redirect-plan, redirect-policy, strategy-experiments, synthesis-promotion, local-model-profile-trial,
route-weights-export, capability-admission-gate, and others). The propensity ranking that orders every
offer is therefore trained, for half the catalogue, on fixtures passed on 2026-09-02/03. §2 of
CLAUDE.md (never let environment noise train as capability evidence) applies in the other direction
here: fixture passes are not evidence that the capability helped anyone.

### P3 — Observers judged by selection (measurement mismatch)

The selection detector proposed 54 demotions on 2026-10-01, `applied: []`. Most are against observers
(capability-activation-audit, capability-firing-monitor, capability-propensity, switch-review,
feedback-store, issue-readiness, stall-watcher, research-scheduler) offered at `tick:*` phases where
nothing can "trigger" them: they run by cadence, not by choice. The verdict axis already exempts
observers (`observer_output_change`); the selection-gap axis does not. The 54 rows bury the two
proposals that matter (role-prompt from `orchestrate`, role-decomposer from `repo-audit:phase-2`,
both produced by P1's wrong-moment declines).

### P4 — Monitors that report to nobody (broken left broken)

The firing monitor has flagged the same rows in five consecutive weekly snapshots (09-04, 09-11,
09-18, 09-26, 10-03): regressed redirect-apply-bootstrap, research-usage-guard, route-weights-export;
overdue rail-exercise-cadence. The tick writes the report to a file; `switch_review` and the periodic
report do not read it; nothing acted. Checked against reality: redirect-apply-bootstrap is a REAL
regression (0 applies since 2026-09-02; the chain trace in the companion record explains why);
route-weights-export is FALSE — the export branch moved on 2026-10-02 and its heartbeat sits on a
path the daily run takes only when the artifact changes; rail-exercise-cadence is FALSE — the weekly
run stamped 2026-10-01 and wrote a 167 KB report, but it runs without `--record`, so neither
verdicts nor heartbeat reach the ledger; research-usage-guard is silent because its input (the
research arm) went off with the dispatch lane. Two of three "regressions" are the ledger diverging
from the running step. A monitor with no consumer and a 1-in-3 precision rate is not a check.

### P5 — Inputs switched off without re-homing the dependents (register A1, A4)

The dispatch-lane shadow of 2026-09-03 (owner decision, PR #202) silently removed the input of at
least ten rows: role-triage, role-adjudicator (tick path), the runtime-AC tick path, the research arm
and everything downstream of it (abcd-experiment since 08-28, synthesis-promotion, strategy-experiments,
research-usage-guard), range-lane-rollout, redirect's tick path, the epic decomposition lane. None of
their cadence promises was suspended or re-homed, so each reads `never fired` / `no demand`, and the
first decision cards this week read them exactly that way. The decision was right; the bookkeeping
that would make its consequences legible was never done.

### Two production paths the ledger could not see

* **testgen-lane** reads 3 invocations in 90 days. The Brain holds 44 testgen dispatches in the same
  window (38 codex, 4 gemini, 2 cursor), made by `coverage-autopilot` through `dispatcher.py delegate`:
  32 PASS, 12 FAIL, 26 durable, 4 broke later. That is the fleet's one working range lane, and it
  never heartbeats the capability that names it. coverage-autopilot has been `paused_stalled` on
  every track since 2026-09-15 after a `gh issue view` failure (rc 1), with an owner question recorded
  that day; nothing resumed it. Last testgen dispatch: 2026-09-15.
* **synthesis-promotion**'s latch (companion record; fix in #409) was invisible for the same reason:
  the heartbeat fires only from the CLI.

## What the assessor's habits had to do with it

Each pattern is a habit of the sessions that built and judged this tool: binding a capability to a
surface without saying when the situation exists (P1); recording whatever the harness produced as
evidence (P2); reusing a selection metric on things that are not selected (P3); building a measurement
and never wiring its reader (P4); switching an input off and reading the resulting silence as absence of
demand (P5); and, in the testgen case, never asking whether the concept was already running under
another name before calling it rare. The rule written today (repo CLAUDE.md §6, ADDING_CAPABILITIES
Part 3) addresses the verdict habit; the fixes below address the tooling that made the verdicts look
supported.

## Fixes (zero-Tim unless marked)

1. **Preconditions decide offers.** An unknown precondition does not offer; it records `fact_missing`
   against the SURFACE (the lane did not pass PR facts), never a decline against the capability. The
   value-chain monitor's `situation_count` is the precondition's true count. adversarial-review gets a
   measurable situation derived from fleet shapes (workflow, auth, data-path classes, large diffs),
   counted in shadow before any offer.
2. **Fixture provenance.** A `fixture_observed` class, excluded from the ranking weight and reported
   separately in `capabilities usage` ("production useful N / fixture passes M"). The weekly exercise
   keeps recording, under that class (owner switch `ORCH_RAIL_EXERCISE_RECORD`; see decision 2).
3. **Observers out of selection-gap detection.** `is_observer` and cadence-run rails are excluded from
   demotion proposals; the detector's output drops from 54 rows to the ones that mean something.
4. **Monitor findings get a consumer and a second number.** The weekly switch review prints the firing
   monitor's regressions beside what the running step says (stamp age, artifact mtime), so a
   ledger-only silence prints as `heartbeat silent, step ran <date>`; heartbeats move onto the paths
   the cadences take (route-weights-export, rail-exercise-cadence).
5. **Dependency-aware silence.** A row whose cadence depends on a default-off flag prints
   `input off: ORCH_DISPATCH_LANE`, never `never fired`.
6. **testgen attribution.** `dispatcher delegate task_type=testgen` heartbeats testgen-lane, so its
   record shows the 44 / 32 / 26 above. **Owner decision 1:** resume coverage-autopilot's paused tracks
   with the gh failure classified transient (retry) rather than stall.
7. **Owner decision 2:** set `ORCH_RAIL_EXERCISE_RECORD=1` once fix 2 lands, so the weekly exercise's
   verdicts reach the ledger as fixture evidence instead of a report nobody opens.

Fixes 1–6 are the value-chain monitor PR (decided) plus two small PRs; nothing here adds a recurring
step for the owner.

## Per-capability table (90 days; live rows)

Columns: invocations, successes, offers (advice events), declines, useful verdicts (+/−), graded
influence edges (PASS/FAIL), and flags. FIXTURE-ONLY = every useful verdict is a rail-exercise contract.
DEMOTION = the 2026-10-01 detector proposes demoting a binding. GATE-OFF = matcher is a default-off
flag. My earlier "unconsulted" computation was wrong and is omitted; `consulting_surfaces()` reports
no bound-but-unconsulted surface today.

| capability | status | inv | succ | offers | declined | useful + / − | edges P/F | flags |
|---|---|---|---|---|---|---|---|---|
| abcd-experiment | generated | 273 | 269 | 55 | 10 | 2 / 0 | 0/0 | DEMOTION tick:experiments |
| adversarial-review | generated | 7 | 0 | 1448 | 533 | 6 / 0 | 0/0 | DECLINED 533/1448 |
| agy-runtime-isolation | generated | 49 | 0 | 8 | 0 | 2 / 1 | 42/19 |  |
| capability-activation-audit | wired | 73 | 0 | 176 | 2 | 4 / 28 | 0/0 | DEMOTION tick:capacity,tick:dispatch,tick:experiments,tick:learning,tick:redirect |
| capability-admission-gate | generated | 2 | 0 | 3 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY |
| capability-firing-monitor | generated | 15 | 0 | 178 | 2 | 5 / 0 | 0/0 | DEMOTION tick:capacity,tick:dispatch,tick:experiments,tick:learning,tick:redirect |
| capability-propensity | generated | 2608 | 0 | 178 | 0 | 3 / 0 | 0/0 | DEMOTION tick:capacity,tick:dispatch,tick:experiments,tick:learning,tick:redirect |
| capability:reference-sync-hygiene-test-gate | shadow | 4 | 2 | 3 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY |
| codemod-campaign | generated | 4 | 0 | 1292 | 547 | 3 / 1 | 0/0 | DEMOTION repo-audit:dimension-2,repo-audit:fix; DECLINED 547/1292 |
| completion-event-lineage | canary | 49 | 0 | 27 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:learning |
| coverage-testgen-trigger | wired | 7 | 0 | 23 | 2 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:learning |
| cross-repo-coordination | generated | 1 | 0 | 1387 | 487 | 2 / 0 | 0/0 | DEMOTION closer-lane; DECLINED 487/1387 |
| deliberate-break-verifier | generated | 379 | 0 | 2633 | 1005 | 375 / 19 | 0/0 | DECLINED 1005/2633 |
| docs-drift-fix-agent | wired | 10 | 0 | 5 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY |
| epic-decomposition | generated | 2 | 0 | 33 | 14 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION repo-audit:fix; DECLINED 14/33 |
| evidence-acquisition | shadow | 22 | 0 | 171 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:capacity,tick:dispatch,tick:experiments,tick:learning,tick:redirect |
| feature-reflection-cli | generated | 40 | 0 | 27 | 0 | 1 / 1 | 0/0 | FIXTURE-ONLY; DEMOTION tick:learning |
| feature-scan | wired | 36 | 0 | 8 | 3 | 0 / 1 | 0/0 |  |
| feedback-store | generated | 55 | 0 | 27 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:learning |
| frontend-verifier | generated | 3 | 0 | 22 | 9 | 3 / 0 | 0/0 | DEMOTION repo-audit:phase-2; DECLINED 9/22 |
| issue-readiness | wired | 2379 | 0 | 31 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:capacity |
| live-keepalive-supervisor | wired | 49 | 0 | 31 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:dispatch |
| local-model-profile-trial | shadow | 2 | 0 | 3 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY |
| offload | generated | 1055 | 0 | 1659 | 513 | 176 / 42 | 0/0 | DEMOTION implementation-verification,repo-audit:dimension-1,repo-audit:dimension-2,repo-audit:dimension-4,repo-audit:fix; DECLINED 513/1659 |
| partitioned-review | generated | 3 | 0 | 206 | 63 | 3 / 0 | 0/0 | DECLINED 63/206 |
| rail-exercise-cadence | shadow | 1 | 3 | 1 | 0 | 1 / 0 | 0/0 | FIXTURE-ONLY; FM overdue |
| range-lane-rollout | canary | 13 | 1 | 29 | 0 | 1 / 0 | 0/0 | DEMOTION tick:dispatch |
| redirect-apply-bootstrap | canary | 2 | 0 | 25 | 0 | 1 / 1 | 0/0 | FIXTURE-ONLY; DEMOTION tick:redirect; FM overdue,regressed |
| redirect-plan | generated | 831 | 0 | 1351 | 467 | 2 / 0 | 0/0 | FIXTURE-ONLY; DECLINED 467/1351 |
| redirect-policy | generated | 1162 | 0 | 1351 | 467 | 2 / 0 | 0/0 | FIXTURE-ONLY; DECLINED 467/1351 |
| repo-playbook | generated | 2 | 0 | 12 | 5 | 1 / 1 | 0/0 |  |
| research-scheduler | generated | 861 | 0 | 30 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:experiments |
| research-usage-guard | wired | 4 | 28 | 23 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:experiments; FM overdue,regressed |
| role-adjudicator | shadow | 1 | 0 | 11 | 4 | 1 / 0 | 0/0 |  |
| role-decomposer | shadow | 1 | 0 | 47 | 16 | 2 / 0 | 0/0 | DEMOTION repo-audit:phase-2; DECLINED 16/47 |
| role-prompt | wired | 2 | 0 | 62 | 23 | 1 / 0 | 0/0 | DEMOTION orchestrate; DECLINED 23/62 |
| role-redirect | shadow | 752 | 737 | 33 | 9 | 1 / 1 | 0/0 |  |
| role-triage | exercised | 781 | 644 | 44 | 13 | 2 / 1 | 3/7 | DEMOTION repo-audit:phase-4 |
| route-weights-export | shadow | 3 | 24 | 1 | 0 | 1 / 0 | 0/0 | FIXTURE-ONLY; FM overdue,regressed |
| runtime-ac-checks | canary | 4 | 0 | 2549 | 994 | 3 / 1 | 0/0 | DEMOTION closer-lane,file-agent-issue; DECLINED 994/2549 |
| stall-watcher | generated | 132 | 0 | 26 | 2 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:redirect |
| strategy-experiments | wired | 2 | 0 | 31 | 1 | 2 / 0 | 0/0 | GATE-OFF ORCH_STRATEGY_EXPERIMENT=1; FIXTURE-ONLY; DEMOTION tick:experiments |
| switch-review | wired | 16 | 0 | 197 | 7 | 2 / 6 | 0/0 | DEMOTION tick:capacity,tick:dispatch,tick:experiments,tick:learning,tick:redirect |
| synthesis-promotion | wired | 2 | 0 | 30 | 0 | 2 / 0 | 0/0 | FIXTURE-ONLY; DEMOTION tick:experiments |
| testgen-lane | generated | 3 | 0 | 1423 | 535 | 3 / 1 | 0/0 | DECLINED 535/1423 |
| thompson-hybrid-routing | shadow | 10 | 0 | 30 | 0 | 1 / 0 | 0/0 | GATE-OFF ORCH_EXPLORATION_MODE=thompson-hybrid |
| windowed-capacity-policy | generated | 869 | 0 | 33 | 8 | 2 / 1 | 0/0 |  |
