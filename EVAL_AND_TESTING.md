# The orchestrator's research arm — evaluation, testing, and a dataset that actually grows

This answers four things the feedback loop's first draft got wrong or left out (your critique, 2026-06-14):
**when/who** evaluates and at **what intensity**; how the dataset **genuinely grows** instead of
sitting in a fixed schema; how to run a **system of comparative-advantage testing**; and how to
**recognize reusable orchestrator features** as they appear. They are not four systems — they are one
**capacity-aware research engine** that runs the orchestrator's own science. Grounded in established
practice, not freelanced (citations inline).

The honest starting point: running the learner on the first real experiment (the Scorecard A/B/C/D)
exposed three flaws that prove the first draft could not grow:
1. **Binary PASS/FAIL discards magnitude** — cursor (8.33) and codex (7.83) both counted as "1 success."
2. **The cost term divides by zero for free agents** — `score = posterior / cost_per_success` silently
   drops cursor's decisive "free *and* best" advantage.
3. **At n=1 the prior dominates** — correct conservatism, but it means the system is starved for data
   unless something *produces* data systematically. That "something" is the testing system below.

---

## 0. The unifying frame: information-gain per unit capacity

Every evaluation, experiment, adversarial review, and feature-hardening task is a **job** that costs
capacity and yields **information**. The engine's single objective, borrowed straight from cost-aware
active learning, is to **maximize information gain per unit capacity under the currently-available
budget** ([cost-efficient AL](https://arxiv.org/pdf/2502.06209): "favoring samples that maximize
information gain per unit cost"). That one principle decides *when* to run (only when spare capacity
exists), *what* to run (highest info/cost first), and *how intensely* (fill the available budget).

---

## A1. When, by whom, and at what intensity — the capacity-aware scheduler

**Never always-on.** The engine wakes only when there is **spare capacity**, defined precisely against
`capacity.py`: (a) a free/flat seat idle (cursor composer, vibe), or (b) a metered reasoning seat
(codex/claude) with 5h-window headroom that will otherwise **reset unused** (use-it-or-lose-it), or
(c) a deliberate human "run the science now" trigger. No spare capacity → the engine does nothing and
production routing is untouched.

**Variable intensity = a budgeted acquisition problem.** Candidate jobs sit in a queue, each scored by
an **acquisition function** and tagged with a capacity cost:

```
priority(job) = info_value(job) / capacity_cost(job)
info_value   = uncertainty · stakes · staleness
```
- **uncertainty** — entropy of the posterior over "which agent is best for this task_type"
  (uncertainty-based acquisition, [AL survey](https://www.nature.com/articles/s41598-023-50598-z)).
  We test where we are *least sure*, not where we already know the answer.
- **stakes** — production importance of the underlying work (a release-blocking PR > a docstring).
- **staleness** — time since an agent/task_type was last measured (fleets drift; a stale arm is informative).

The scheduler then **greedily fills the available capacity budget** (a knapsack): abundant capacity →
the intense job (a 4-way experiment + an adversarial panel + a 3-judge ensemble); scarce capacity → the
cheap job (a single-judge spot-check of one production outcome). **Same queue, intensity scales to the
budget.** This is the "variable intensity as capacity exists" you asked for, made concrete.

**Who evaluates:** the cross-family pool, but **weighted by measured judge reliability** (see A2/§ on
judge quality). gemini, having failed as both implementer and judge in run 1, is down-weighted, not
trusted equally.

---

## A2. How the dataset actually grows — the self-evolving flywheel

A fixed schema cannot grow; you were right. Growth requires that **three artifacts evolve**, each
versioned with lineage ([data flywheel](https://www.nvidia.com/en-us/glossary/data-flywheel/): a
"versioned store tying raw logs, labeled examples, prompts, policies, evaluations, and lineage"):

1. **The labels** (outcomes) — already stored.
2. **The rubric / evaluation protocol** — *how* we judge. Versioned.
3. **The schema / evidence captured** — *what* we record. Versioned, and it **changes over time** via
   the meta-evaluation loop below.

**The meta-evaluation loop (the mechanism you specified).** Every evaluation emits, alongside its
verdict, an **`evidence_gap`**: "what would have let me judge this better?" — e.g. *"could not assess
correctness without test-execution output," "needed to see empty-config behavior," "the diff alone
didn't reveal runtime behavior."* These gaps are first-class rows. Then:

- **Identify** — aggregate gaps across evaluations; a gap that **recurs above a threshold** is a signal.
- **Report** — recurring gaps surface in the periodic report as **proposed data-structure changes**
  ("Add evidence type: `test_run_output`. Cited as missing in 7/12 recent evals.").
- **Add** — on approval, a **schema migration** registers the new evidence type; future runs capture it.
- **Remove** — every field/evidence type tracks **influence** (was it actually *cited* in a verdict?).
  Evidence never referenced across a window is flagged for **pruning**. The dataset stays lean, not
  hoarded. (Data-centric AI: curate the *informative* data, [Cleanlab](https://cleanlab.ai/blog/learn/guide-to-dcai/).)

This is the part that makes the evaluation *grow*: the **rubric and schema are themselves learning
targets**, driven by the evaluators telling us what they lacked. Without it, the schema is frozen and
"growth" is just more rows of the same shape.

**High-signal curation.** Not every outcome deserves expensive labeling. Prioritize the **surprising**
cases — judge disagreement, prior↔outcome mismatch (cursor beating claude), durability reversals,
low-confidence verdicts. These carry the most information (the flywheel "collects high-signal data:
incorrect predictions, low-confidence outputs").

**Fixing the three flaws this surfaced:**
- **Magnitude:** store the continuous quality score (0–10) and learn a **Beta posterior on a normalized
  reward** `q = score/10`, not a coin-flip — `posterior = (k·prior + Σqᵢ)/(k + n)`. cursor's 8.33 now
  outranks codex's 7.83 instead of tying.
- **Cost for free agents:** replace the divide with **value-per-marginal-dollar that rewards zero cost**:
  rank first by quality posterior, then break ties / weight by `quality · e^(−λ·cost)` so cost=0 is the
  *best* possible multiplier (1.0), not a divide-by-zero. Free-and-good wins, as it should.
- **n=1 starvation:** the testing system (B) manufactures data on purpose.

---

## B0. Two governing principles (user-corrected 2026-06-14)

**Evidence, not proof.** An experiment never *proves* a comparative-advantage claim — it produces one
observation that affirms or undercuts a hypothesis as data accumulates. This is why the learner is
Bayesian (n=1 barely moves the prior) and why hypotheses carry a `status`
(open→accumulating→supported→refuted) instead of a boolean. Report results as "in N trials, X" — never
"X is proven." A *capability* observation (an agent wrote zero files twice) is firmer than a *quality*
comparison, but still pending root-cause, not absolute.

**Experiments are never wasted capacity.** A review/eval extracts value even from approaches not judged
optimal: the panel names *specific strengths* in the losers. The experiment's deliverable is therefore
**the synthesized best** — start from the chosen base, graft the panel-identified strengths of the
others, fix the base's noted weaknesses, ship that. Implemented as `exp_abcd.synthesize` (winner +
harvested strengths → one shippable diff). The judge-panel "synthesize from the winner, graft the
runners-up" pattern.

**The ship/discard decision is JUDGMENT, not a score cutoff** (user-corrected: "point systems communicate
to humans — do they really help *you*?"). A 6/7-out-of-10 gate is theater; we have no idea how a number
correlates with "is this a productive starting point." So `usefulness_gate()` delegates the call to a
strong reasoning agent reading the actual code: *is the best approach mostly the right direction / a
productive base to finish efficiently, or are its weaknesses serious enough that building on it is
inefficient long-term (discard, restart)? And do the approaches diverge so much that the wrong base makes
implementation a digression?* The 0–10 panel scores remain, but only as human-facing context — never the gate.

**The test-cadence is itself a learned judgment** (user-corrected: "build judgment, but it has to be
exercised to be useful — rough heuristics with a strong bias to update"). `should_test()` returns a
*recommendation the orchestrator exercises and may override*, not a mandate; the system is biased to
**update** the heuristic from whether past tests of that kind proved informative. Early/rough priors →
high uncertainty → it tests more; as hypotheses resolve → it tests less. Cadence sits **between
"opportunistic-only" and "aggressive"**: capacity-gated, hypothesis-driven, leaning to explore while the
priors are thin.

**Multi-agent assignment is a first-class, learnable strategy** (user-corrected: "don't let the system
preclude that multi-agent, despite inefficiency, is sometimes the better outcome"). An experiment *arm*
is a single agent **or** a strategy (`{parallel: [high, low], synthesize: true}`). Seeded hypotheses H4
("a high+low-cost pair + synthesis beats a single high-cost agent on harder implements") and H5
("mixed-tier parallelism beats two-high — diversity > horsepower") are open questions the engine
accumulates evidence on. A strategy's value = synthesized-best quality vs its **total** cost (sum of arms)
vs the single-agent baseline. The synthesize phase is what can make paying for parallel agents worth it.
`strategy_experiment.py` is the guarded runner surface for these arms: it expands strategy arms into
`exp_abcd` implementation agents, writes strategy metadata, and keeps active prepare supervised rather
than cron-launched.

## B1. The testing system — comparative advantage by design

**A hypothesis registry.** Falsifiable comparative-advantage claims, each `{claim, task_type/conditions,
arms, evidence(n, effect_size, posterior), status: open|accumulating|supported|refuted}`. Seeded with
**established ideas to test** (your examples): *"cursor composer ≥ premium seats on well-specified
integration implements"* (run 1: supported, n=1 — keep accumulating); *"a cheap GPT/codex-mini model is
cost-efficient on mechanical codemods"*; *"gemini's huge context wins on large-file comprehension."*
Hypotheses are the queue of questions the engine is trying to answer.

**Random assignment with variable N (2–5), done right.** When the scheduler greenlights a task as a test
and capacity allows, it picks a **subset** of plausible agents — not always the same count:
- Composition uses **Top-Two Thompson Sampling**, not vanilla Thompson, because the goal is *best-arm
  identification*, not reward maximization ([why](https://towardsdatascience.com/why-not-to-use-thompson-sampling-for-best-arm-identification-8ed458428126/);
  Top-Two needs ~35% fewer trials to find the best arm). Practically: include the current **leader** +
  the highest-**uncertainty challenger**, then add random extras up to N as capacity permits.
- **N scales with capacity**: scarce → 2 arms (leader vs one challenger); abundant → 4–5 (a fuller
  comparison + adversarial panel). The *randomization* (which extras, sometimes a wildcard) prevents the
  self-confirming blind spot; the *Top-Two seeding* keeps it efficient.

**Two triggers, both capacity-gated:**
- **Hypothesis-driven** — an open hypothesis + an arriving task that matches its conditions → route the
  task as an experiment.
- **Opportunistic** — spare capacity + a task that happens to be a clean test subject → spend the idle
  capacity on a bonus arm or two. (Use-it-or-lose-it capacity is *free information*.)

**Guardrail:** experiments run on **isolated branches, nothing auto-merges** (the exp_abcd model). The
production lane still gets the leader's output; the extra arms are pure measurement.

## B2. Adversarial review — a first-class feature (you asked for this)

Distinct from the advisory review in `ORCHESTRATOR.md`. Reviewers are prompted to **refute** — find the
fatal flaw, default to "reject unless proven sound" — and a **minority-veto / debate ensemble** adjudicates:
- **Minority-veto** raises the true-negative rate: a small number of well-justified vetoes force a
  "blocked" label ([LLM-judge bias work](https://arxiv.org/pdf/2510.11822)). Good for "is this *actually*
  correct," where agreeableness bias otherwise rubber-stamps.
- **Debate / multi-persona** (MAJ-Eval style) improves alignment to ground truth for reasoning-heavy
  calls. Use when a single judge's verdict is high-stakes or contested.
- Adjudicate, don't obey: the orchestrator weighs vetoes against ground truth (tests, repo conventions),
  consistent with the lesson already in `ORCHESTRATOR.md`.

## B3. Evaluation-process quality (so the labels are trustworthy)

The LLM-judge literature names exactly the failure we measured and prescribes fixes — fold all in:
- **Randomize candidate order per judge** (run 1 used a fixed A/B/C order → positional bias). Cheap, do always.
- **Self-preference bias** is real and known; anonymization (already done) + measuring it (already done:
  cursor +1, codex +0.5) + **down-weighting a judge's score on its own work**.
- **Judge reliability is a measured property of the instrument**, not assumed equal (item-response-theory
  view, [2025–26 work](https://www.emergentmind.com/topics/llm-judge-evaluation)). Track each judge's
  agreement with the consensus and with rare human labels; **weight judges by reliability** (gemini → low).
- **Calibrate to a small human-annotated set**: periodic human spot-labels (your stated low-frequency
  preference) drive a **regression bias-correction** that "halves residual error"
  ([Deepchecks](https://deepchecks.com/llm-judge-calibration-automated-issues/)). The human anchors the proxy.

---

## C. Recognizing orchestrator features as they appear

You noticed the pattern: A/B/C/D harness, the stall-watcher, offload, adversarial review — these emerged
ad hoc. To capture them **consistently**:

- **The rule of three + a reflection step.** At the end of a task the orchestrator runs a short
  self-check: *"Did I build a structure that solves a problem a future task will hit? Is this the 2nd/3rd
  time?"* If yes, it's logged to a **features registry**: `{name, problem_solved, where_used[], maturity}`.
- **Maturity ladder:** `ad-hoc` (built inline once) → `reused` (invoked again) → `hardened` (promoted to a
  selftested module, like `exp_abcd.py`). The registry makes the ladder explicit and surfaces promotion
  candidates ("`stall-watcher` used 3×, still inline → promote to a module").
- **Partly automatable:** scan `~/.codex/orchestrator/` for repeated ad-hoc scripts and the session for
  recurring patterns; propose promotions. The promotion/hardening itself is a **capacity-aware job** in
  the §A1 scheduler — features get hardened when there's spare capacity, same as everything else.
- **Operational first increment:** `features.py record --name ... --where ... --problem ...` is the
  task-end reflection entry point; `summary`, `candidates`, and `harden` expose the ladder, and
  `periodic_report.py` includes maturity counts plus promotion candidates without creating or mutating the
  registry during report generation.

---

## D. Deterministic Research Usage Guard & Anomaly Controls

To prevent runaway evaluator dispatch, budget exhaustion, or repeated missing-spec execution loops:

1. **Opt-in Unattended Research (`ORCH_RESEARCH_ARM=0`):** Unattended research ticks default to disabled (`0`). Explicit opt-in (`ORCH_RESEARCH_ARM=1`) or manual/supervised overrides (`is_manual=True` or `ORCH_RESEARCH_USAGE_BYPASS=1`) enable research execution.
2. **Missing-Spec Zero LLM Dispatch:** Recovered experiments with missing specs (`spec_provenance: "missing_spec_stub"`, `missing_spec: True`) never invoke LLM evaluators or synthesis. Objective anchors (diff evidence) are preserved, terminal `followup-skip.json` artifacts are written, and subject lifecycle is marked `skipped`.
3. **Stable Signature Deduplication:** Candidate panel evaluation signatures cover repo, normalized spec hash, base SHA, and candidate diff hashes. Panel width and judge identity are deliberately excluded so changing evaluators cannot bypass immutable-input deduplication. Persisted `followup-decision.json` (the completed unattended decision, signature, evaluator result, and objective anchors), `eval-maps.json`, and the local opportunity ledger prevent re-spending across process restarts.
4. **Local Usage Guard & Anomaly Report (`src/research_usage_guard.py`):** Operates deterministically with zero network and zero LLM calls. Assesses rolling budget limits (24h evaluator calls, 24h prompt bytes) and anomaly spikes (1h evaluator call spikes, 1h prompt byte spikes, repeated subject share). Records opportunity decisions (`admitted`, `deferred`, `duplicate`, `missing-spec-objective-only`, `blocked_by_limit`, `blocked_by_anomaly`) before launch, reconciles admitted rows to a terminal outcome, and independently audits recorded evaluator runs so bypassed or pre-deployment missing-spec/wide/repeated panels remain visible. The daily cadence writes `$ORCH_STATE_DIR/research-usage-report.json` and fails visibly while a block remains active in the 24-hour alert window, an admitted dispatch is stale, or observed-run telemetry is unavailable.
5. **Evaluator Panel Sizes:** Direct/manual `evaluate` calls retain the 4-evaluator default, while unattended follow-up starts with one Vibe judge. A wider calibration panel requires an explicit `ORCH_FOLLOWUP_EVALUATORS` list.

---

## Build status (honest: designed vs. implemented)

- **Implemented + selftested:** the growth mechanism in `feedback.py` v2 — `evidence_gaps` +
  `evidence_types` registry (meta-eval/schema evolution + influence-based pruning), quality-magnitude
  learning (continuous reward), the free-agent cost fix, rubric/schema versioning. The
  `research_scheduler.py` scaffold — hypothesis registry, capacity-aware acquisition/knapsack, Top-Two
  variable-N arm selection; the feature registry reflection CLI + periodic-report surface.
  The deterministic research usage guard (`src/research_usage_guard.py`) — rolling limits, anomaly spike detection, missing-spec zero dispatch, stable signature deduplication, and telemetry isolation in `test_feedback_model_provenance.py`.
- **Designed, scaffolded next:** human calibration regression.
- **Integration seams (unchanged plan):** LangSmith cost pull; durability sweep; router reads learned weights.

The thesis: the orchestrator should **run its own science** — and experiments, evaluations, reviews, and
feature-hardening are all jobs competing for spare capacity, ranked by information-gain-per-capacity. The
dataset is the lab notebook, and it grows because the evaluators are required to say what they wished they'd had.
