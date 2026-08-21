# The orchestrator's feedback loop — long-term learning architecture

**Problem this fixes (the honest version):** earlier passes processed an outcome once, at the
moment it was generated, and folded a sentence into `ORCHESTRATOR.md`. That is not a loop — it's a
diary. Nothing was *retained* in a form the next decision could query, nothing tracked *what a change
to the orchestrator did*, and the orchestrator only ever saw outcomes for the agents it already chose
(selection bias → it ossifies). An elite loop needs four things this design supplies: a **durable
store**, a **goal-success label LangSmith cannot give**, a **learning rule that respects evidence
accumulation**, and an **exploration mechanism** so the data isn't self-confirming.

## The three data planes (and why none alone is enough)

| plane | source | what it knows | what it CANNOT know |
|---|---|---|---|
| **Decision** | the orchestrator, at dispatch | which agent/mode/reasoning-level, the decomposition, the *rationale* | whether it was a good call |
| **Execution** | LangSmith (+ ledger) | tokens, $, latency per run | whether the work met the user's goal |
| **Outcome** | verifier + durability sweep + human spot-check | did it PASS the AC, merge, and **durably hold** | nothing — this is the label |

The loop's whole job is to **join all three by `run_id`/PR into one durable row**, then learn from the
joined history. LangSmith is the execution plane only — it is **ephemeral** (retention windows) and
**goal-blind**. So we *copy* cost out of it into a store that retains indefinitely and carries the
goal-label it can't see. That join is the architecture.

## 1. Storage & retention — `feedback.py` (SQLite, built + selftested)

`~/.codex/orchestrator/feedback/orchestrator.db`. Core tables:

- `runs` — one row per decision: agent, mode, reasoning_level, decomposition, **rationale**, pr, experiment_id.
- `outcomes` — `verifier_verdict`, `adjudicated_verdict`, `merged`, `ci_status`, and **`durability`**
  (`pending|durable|reverted|reworked|reopened|broke_later`). Durability is **updated days later** by a
  sweep — `record_outcome()` patches an existing row instead of clobbering it, because the un-gameable
  signal (did the merge survive?) arrives long after the merge.
- `costs` — tokens/$/latency, `source` = `langsmith|ccusage|ledger`. Retained even after LangSmith
  expires the trace.
- `execution_traces` — trace_id/URL, provider/model, operation/status, and per-record execution metrics
  copied out of LangSmith fleet artifacts so the durable dataset keeps trace evidence, not just aggregate
  cost.
- `route_weights` — **versioned**. Every relearn writes a new version with prior, posterior, n_obs,
  success_rate, cost_per_success, score, the data window, and a rationale. This is how a change *to the
  orchestrator* is catalogued so its effect can be attributed (compare outcomes before/after a version bump).
- `evaluations` — the A/B/C/D cross-eval matrix (implementer × evaluator → score/rank/verdict).
- `human_calibration` — periodic human ground-truth that re-anchors the proxy.

**Retention:** rows are tiny JSON/scalars; keep them indefinitely. The dataset *growing* is the asset —
the learning improves as it develops, which is exactly the property requested.

## 2. The success label — durability, not green CI

`_is_success()` = a PASS verdict **that durably held**. A merge that is later reverted/reworked/reopened
is scored as a **failure the verdict missed** — even though CI was green and it merged. This is what makes
the label hard to game: an agent can produce plausible-looking green diffs, but it cannot fake *surviving
contact with the codebase over the next weeks*. `pending` durability provisionally credits a PASS but is
not yet confirmed; the sweep resolves it.

## 3. LangSmith interface

- **Join key:** preferred path is one `run_id` emitted into both the local `runs` row and the agent's
  LangSmith trace metadata. While that is incomplete for remote keepalive traces, `langsmith_pull.py`
  bridges LangSmith-owned `run_id`s through `github_pr`/`github_issue` + `domain.agent` to the recorded
  Orchestrator target, skipping ambiguous matches.
- **Pull:** `langsmith_fetch.py` reads the Workflows fleet registry, downloads each repo's latest GitHub
  Actions `langsmith-fleet.ndjson` artifact, combines the NDJSON on local disk, then calls
  `langsmith_pull.py`. If no direct per-repo artifacts exist, it falls back to the Workflows dashboard's
  distinct `langsmith-fleet-rollup-*` artifact. The puller joins records to known Orchestrator `run_id`s,
  writes `execution_traces`, aggregates tokens/$/latency per `run_id`, and calls
  `record_cost(run_id, ..., source="langsmith")`. `langsmith_direct.py` also pulls `workflows-agents`
  directly from the LangSmith API into the same join path.
- **Local CLI fallback + upgrade:** local CLI delegates that do not emit LangSmith traces get per-run
  latency and JSON log usage from dispatcher start/complete ledger rows via `ledger_reconcile.py`,
  `source="ledger"`. `ccusage_reconcile.py` then upgrades Codex/Claude rows to `source="ccusage"` only
  when a ccusage session's `metadata.lastActivity` maps to exactly one completed same-agent run window.

## 4. The learning rule — Beta-Binomial prior→posterior (`relearn()`)

The hand-set route table (`ORCHESTRATOR.md`) is the **prior**. For each `(task_type, agent)`:

```
posterior_success = (k·prior + successes) / (k + n)      # k = PRIOR_STRENGTH (8 pseudo-obs)
score             = posterior_success / cost_per_success  # capacity-per-VERIFIED-success
```

- **Cold start (n=0):** posterior = prior. The orchestrator behaves exactly as hand-tuned. No wild swings
  on thin data.
- **As data develops:** observed durable-success moves the posterior; past ~k real outcomes, evidence
  dominates the prior. (Selftest proves the order *flips* against the prior once evidence warrants.)
- **Rolling window (90d):** old outcomes age out, so an agent that improved last month isn't judged on
  six-month-old failures. The loop tracks the *current* fleet, not its history forever.
- **Cost-aware:** the score is success **per unit capacity**, so a cheap agent that's slightly-less-reliable
  can rightly outrank an expensive one — which is the entire economic premise of this orchestrator.
- The router reads `current_weights(task_type)` as its order; the hand-set table remains the floor.

## 5. Exploration — the fix for the blind spot (this is the part that was missing)

A loop that only records outcomes for the agent it *chose* never learns whether a deprioritized agent
would have done better. It confirms its own habits — exactly the "Cursor always implements" trap. Two fixes:

- **Bootstrap:** the A/B/C/D experiment is a *one-time full exploration* — all four plausible agents do the
  same task, all four cross-evaluate. That seeds the priors with **unbiased** cross-agent evidence before
  any habit forms. (Recorded in `runs` + `evaluations`.)
- **Sustained:** ε-greedy exploration in the router — occasionally route to a same-tier non-top agent,
  preferring the least-observed eligible challenger, to keep the evidence fresh. Without this the posteriors
  go stale for unused agents and the loop calcifies. `ORCH_EXPLORATION_MODE=thompson-hybrid` is an opt-in
  refinement that keeps the ε cap but chooses the challenger with a reconstructed Beta posterior sample.
  Router-dispatched runs persist `exploration` / `exploration_mode` in `runs.routing_metadata`, while
  leaving `runs.assignment` reserved for causal-learning filters.

## 6. Human calibration — guard against proxy drift

The proxy (AC-verifier + durability + adjudicated review) approximates the user's goal but can drift. The
user reviews rarely (by their own preference), so calibration is a **low-frequency spot-check**, not
per-PR: when the human does weigh in, `record_human_calibration()` stores it; if human verdicts and the
proxy systematically disagree, that's the signal to re-tune the proxy. The proxy carries the load; the human
is the occasional ground-truth anchor.

## Cadence (who runs what, when)

1. **At dispatch** — `record_run(...)` (decision + rationale). *Now wired into the dispatch path.*
2. **At completion** — `record_outcome(verifier/adjudicated/merged/ci)`. Durability stays `pending`.
3. **Daily-ish sweep** — re-check merged PRs: reverted? reworked? reopened? broke later? → patch durability.
4. **Daily-ish execution pull** — LangSmith artifact/direct fetch, local ledger reconciliation, and
   ccusage attribution → `record_execution_trace` + `record_cost`.
5. **Weekly/monthly** — `relearn()` → new `route_weights` version; the periodic report shows the diff
   (what the orchestrator now believes vs last version, and whether the last change helped).

## What's built vs. what's next

- **Built + selftested:** the store, trace retention, the success/durability label, the prior→posterior
  learner, versioned weights, the eval matrix, human-calibration, late-arriving updates, local LangSmith
  fleet artifact ingestion, registry-driven GitHub artifact fetch/cadence, direct LangSmith API
  fetch/cadence, local delegate ledger/log reconciliation, ccusage unique-window per-run attribution, and
  opt-in Thompson-hybrid router exploration. `exploration_review.py` reports the ε-greedy vs
  Thompson-hybrid default-review gate; `exploration_evidence_plan.py`, `exploration_collection.py`, and
  `exploration_backfill.py` provide the staged acquisition path for direct mode evidence and missing route
  cells.
- **Next:** run the staged acquisition path on real low-risk opener subjects. Keep the default ε-greedy
  until the report sees at least 30 outcome-bearing exploration runs for each mode across at least 3 task
  types, plus enough route-weight coverage for a non-cold comparison.

## Note (2026-07-03 audit F8): `runs.agent = 'none'` is keepalive OBSERVATION, not dispatch

1,220 of 3,366 `runs` rows (36%) are keepalive-observed remote work recorded with `agent='none'`
(`source=keepalive`) — PR evolution the fleet merely watched, joined to outcomes so the learner sees
live data. Any consumer of `runs` that does not filter `agent='none'` overstates fleet activity
~1.5x and mixes observation FAILs (the "bootstrap PR died" class) into dispatch statistics.
relearn()/route weights are safe (keyed by real agents); dashboards, reports, and ad-hoc queries
must filter explicitly.
