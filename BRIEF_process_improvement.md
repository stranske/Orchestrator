# BRIEF: process-improvement + completed-PR feedback layer

Method: 3 independent cross-family proposals (codex / gemini / cursor) + a refuter pass on the
front-runner. Anti-groupthink by construction (independent, anonymized, adversarial). 2026-06-19.

## Convergence (high confidence — all 3 independently)
- **One Brain-owned layer**, facts→recommendations, no per-repo forked loops.
- **The token/cost half of (a) is NOT buildable now.** 0/581 keepalive runs have cost data;
  `execution_traces`=0; `costs`=7 (local ledger). All three called it "decorative schema / schema
  theater" unprompted. **Prerequisite: make the `langsmith-fleet/v1` artifact flow** (its own item).
- The **GitHub-derived process signals + the 581 durability outcomes** are the data to use now.

## The split (codex+gemini vs cursor)
- **codex + gemini:** build the **(a) friction / loop-spin miner first** (weekly report:
  high-friction-durable, low-friction-reverted, high-friction-abandoned, work-type comparisons).
- **cursor (dissent):** build **(b) issue-quality first** — "optimizing throughput of work you'll
  revert is optimizing the wrong metric."

## The refutation flipped the front-runner
The refuter dismantled the friction-report-first plan, and it was right:
1. **"Friction" mostly measures task DIFFICULTY, not waste** → the report would reward shrinking toward
   easy work and penalize hard-but-valuable work.
2. **Statistical theater** — split by work_type × repo × assignment × durability × week, buckets are
   tiny, correlated, non-random; it ranks noise with a confident face.
3. **It reproduces the exact defect it's meant to fix** — a weekly report has *no forcing function*;
   it's more collected-but-unused telemetry nobody acts on.
4. **Bad issues are upstream.** Friction-mining is downstream cleanup — "improving the factory floor
   while bad orders keep entering." Deferring issue quality is backwards.
5. Several gh signals (review rounds, autofix iterations, CI attempts) are **fragile operational
   exhaust**, not clean features.

## RECOMMENDATION (changed by the A/B + refuter, against the majority)
**First increment — a durability-learned issue-quality GATE (a forcing function, not a report):**
- EXTEND the repo-review body-writer's existing `body_quality_errors` gate (it already checks ≥4 file
  paths, ≥1 named test, etc.) with a score *learned from the 581 outcomes*: which issue-content features
  (repro steps, acceptance criteria, test instructions, scope clarity, file/module hints, routing
  metadata) actually correlate with `durable` vs `reverted/abandoned` PRs.
- The gate runs **before** an approved-queue issue is materialized into a PR: low-scoring issues require
  remediation or an explicit human override. Forcing function = it blocks/flags, it doesn't just report.
- Measure durability of resulting PRs against the per-work_type baseline we now have (issue 92% / sync
  82% / docs 75%). Forward gate + baseline = a quasi-experiment, which also controls the difficulty
  confound better than post-hoc correlation.
- This is element (b), sharpened into the refuter's "single change," and it directly fixes the
  collected-but-unused defect Tim flagged.

**Keep a narrow (a) slice that DOES have a forcing function:** renovate/sync **loop-spin → a specific
config change** proposal (e.g. "renovate config too aggressive → group/pin/schedule"). This is the
maintenance-system signal Tim explicitly asked for, it isn't a difficulty proxy, and it ends in an
action. (NOT the general friction dashboard.)

**KILL:** the general friction/durability dashboard (difficulty-proxy, underpowered, no forcing
function — the refuter's strongest target).

**DEFER (data-blocked):** all token/$ analysis until LangSmith fleet artifacts flow.

**Other opportunity (gemini only):** export curated rules back to committed `AGENTS.md` per repo,
refreshed when the durability sweep detects a revert — a closed quality loop into the repos. Ties to the
existing repo-knowledge-export idea.

## Calibration note (not splitting the difference)
The refuter's critique is decisive against friction-as-quality-proxy and as a first build; it does NOT
kill the one (a) signal that has a forcing function and isn't a difficulty proxy (renovate loop-spin).
So: (b)-gate first, the renovate slice alongside, the general friction report killed. The same
difficulty-confound the refuter raised against (a) also applies to a naive (b) correlation — which is
why (b) must be a forward gate measured against baseline, not a retrospective correlation.

## Anti-groupthink record
The recommendation came from the MINORITY proposal (cursor) + the refuter, against the 2-of-3 majority
(codex+gemini). Worked example for [[feedback_no_sycophancy]]: independent cross-family proposals + a
real adversary changed the answer.

## LIVE RESULT (2026-06-19) — the data overrode the recommendation too
`issue_quality.py --lookback-days 90`: 483 runs, 433 issues analyzed, **baseline durable rate 95.2%
(412/433)**. Findings:
- **Almost no failure variance to learn from** (only ~21 non-durable of 433). A durability-learned
  scorer can't find much signal when 95% is durable.
- **Quality features are near-universal** — acceptance criteria 430/433, ≥4 file hints 425/433 — i.e.
  no contrast cohort, because the body-writer's existing `body_quality_errors` gate ALREADY enforces
  them. The gate is working; that's why there's nothing to learn.
- **Only one powered signal:** named test instructions (+0.135; 96.4% vs 82.9%, without-cohort n=41).
- **Selection bias:** requiring a linked issue dropped 50 PRs; failures disproportionately lack a clean
  linked issue, skewing the analyzed set toward durable.

**REVISED RECOMMENDATION (do NOT build increment 2 as scoped):** a durability-learned issue-quality gate
is largely redundant — the existing gate already produces 95% durability, and there is no variance to
learn from. Instead: (1) NARROW win — have the existing gate enforce/weight "named test instructions"
(the one real signal); (2) the durability question that's actually open is "what distinguishes the ~38
non-durable issue PRs?" — analyze the FAILURES directly (small-n, failure-focused), not feature
correlation across the 95%-durable mass; (3) the larger lever is the cost/efficiency gap (LangSmith,
the #1 spinoff), not issue quality.

Second worked example for [[feedback_no_sycophancy]]: the live data contradicted BOTH the majority and
the refuter's "build the gate" call — and the right move was to NOT build it. The scorer earned its keep
by telling us the gate already works.
