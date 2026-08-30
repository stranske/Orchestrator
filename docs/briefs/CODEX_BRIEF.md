# Codex brief — the Orchestrator's feedback, data & experiment systems

**Audience:** Codex, when holding the orchestrator seat (`/orchestrate`) or working on the Orchestrator
code itself. This is the **map**; the territory is `FEEDBACK_LOOP.md`, `EVAL_AND_TESTING.md`, and the
modules. Read this first, then those for depth.

## The thesis (why any of this exists)
The orchestrator routes coding to a fleet of cheaper agents and must **learn which agent has comparative
advantage for which kind of work** — measured as **capacity-per-verified-success**, not dollars (all
coding seats are flat-rate; the objective is throughput / capacity-resilience). It "runs its own
science": experiments, evaluations, reviews, and feature-hardening are all **jobs competing for spare
capacity, ranked by information-gain per unit capacity**.

Two governing principles you must respect:
- **Evidence, not proof.** No experiment *proves* a claim; it moves a Bayesian posterior. Report "in N
  trials, X," never "X is proven." Hypotheses carry a status: open → accumulating → supported → refuted.
- **Experiments are never wasted capacity.** Losing approaches still yield value — the panel names their
  *specific strengths*, and the deliverable is the **synthesized best** (winner + grafted strengths +
  fixed weaknesses), not the raw winner.

---

## 1. Feedback system (`FEEDBACK_LOOP.md`)
Three data planes, **joined by `run_id`/PR into one durable row** — that join IS the architecture:

| plane | source | knows | cannot know |
|---|---|---|---|
| **Decision** | orchestrator @ dispatch | agent/mode/reasoning-level, decomposition, **rationale** | if it was a good call |
| **Execution** | LangSmith fleet artifacts + ledger | trace refs, provider/model/status, tokens / $ / latency | if the work met the goal |
| **Outcome** | verifier + durability sweep + human | passed AC, merged, **durably held** | — it IS the label |

LangSmith is ephemeral + goal-blind, so trace refs and cost are **copied out** into the durable store with
the label it can't see.

- **Success label = durability, NOT green CI.** A PASS that durably held. A merge later
  reverted/reworked/reopened is a failure the verdict missed. Un-gameable: an agent can fake a green
  diff, not "surviving contact with the codebase for weeks." Durability starts `pending`; a daily-ish
  sweep resolves it. `record_outcome()` **patches** (late-arriving durability never clobbers the row).
- **Learner = Beta-Binomial prior→posterior** (`relearn()` / `relearn_quality()`): the hand-set route table
  (`ORCHESTRATOR.md`) is the **prior** (strength k=8 pseudo-obs). `posterior = (k·prior + Σq)/(k+n)`, where
  `relearn_quality()` records **one reward per run**: mean cross-eval score/10 when evaluator evidence
  exists, otherwise production outcome/durability success as 1.0/0.0. This preserves score magnitude while
  allowing real merged/failed work to shape routing, without double-counting multi-reviewer panels. Rank by
  quality posterior, tie-break/weight by conservative effort multipliers over mean cost, tokens, and latency,
  so **zero/unknown effort is the best multiplier**, never a divide-by-zero. Cold start (n=0) → posterior = prior (behaves as hand-tuned; no
  wild swings on thin data). 90-day rolling window. `route_weights` is **versioned** — every relearn writes
  a new version (prior/posterior/n_obs/success_rate/score/window/rationale) so a change *to the
  orchestrator* can be attributed by comparing before/after a version bump.
- **Exploration (anti-selection-bias):** a loop that only sees outcomes for the agent it *chose* confirms
  its own habits ("cursor always implements"). Fixes: (a) the A/B/C/D experiment = one-time full
  exploration seeding unbiased cross-agent evidence; (b) sustained ε exploration in `router.select_agent`
  (built 2026-06-16; default ε=0.05, `ORCH_EXPLORATION_RATE` override) to keep deprioritized agents fresh;
  (c) Thompson-hybrid remains available as an explicit same-tier challenger selector using a reconstructed
  Beta posterior sample, but the 2026-06-28 live review keeps epsilon-greedy as the default because direct
  exploration outcomes still favor it.
- **Human calibration:** low-frequency spot-check (`record_human_calibration()`), not per-PR; if human
  verdicts systematically disagree with the proxy, that's the signal to re-tune the proxy.

---

## 2. Data management (`feedback.py` — SQLite)
**Location: LOCAL disk `~/.codex/orchestrator/feedback/orchestrator.db`** — NEVER the Dropbox copy
(Dropbox can corrupt a live DB). Defaults point local via `feedback.LOCAL_RUNTIME` (env
`ORCH_FEEDBACK_DB`). Rows are tiny scalars/JSON, **kept indefinitely — the dataset *growing* is the asset.**

**9 tables:** `runs` (decision) · `outcomes` (label + durability) · `costs`
(tokens/$/latency, `source=langsmith|ccusage|ledger`) · `execution_traces` (trace refs/provider/model/status copied
from LangSmith fleet artifacts) · `route_weights` (versioned learned order) ·
`evaluations` (cross-eval matrix: `experiment_id × implementer × evaluator → score/rank/verdict`) ·
`human_calibration` (ground-truth anchor) · plus the v2 self-evolution pair `evidence_gaps` +
`evidence_types`.

**The self-evolving dataset (what makes it GROW, not just accumulate rows):** three artifacts version
with lineage — the **labels**, the **rubric/protocol**, and the **schema itself**. Every evaluation emits
an `evidence_gap` ("what would have let me judge this better?" — e.g. "needed test-execution output").
Recurring gaps above a threshold surface in the periodic report as **proposed schema migrations**; on
approval a new `evidence_type` is registered and future runs capture it. Every evidence type tracks
**`influence`** (was it actually cited in a verdict?); never-cited types are flagged for **pruning**. So
the rubric/schema are themselves learning targets, driven by evaluators saying what they lacked.

- **High-signal curation:** prioritize *surprising* cases — judge disagreement, prior↔outcome mismatch
  (cursor beating claude), durability reversals, low-confidence verdicts. They carry the most information.
- **Review copy:** `feedback.snapshot_json()` → `data/feedback-snapshot.json` (readable export; live DB
  stays local).

---

## 3. A/B testing harness (`exp_abcd.py`)
Give the **same frozen spec** to N agents in **isolated git worktrees**; every agent then **anonymously
cross-evaluates every output** → unbiased comparative evidence recorded to `feedback` (tagged
`experiment_id`). It deliberately does NOT use `dispatcher.delegate` (no claims, no PR) — nothing merges;
the deliverable is the comparison + the synthesized best.

**Phases (CLI subcommands; implements run detached):**
- `prepare <repo> <spec_file> <exp_id> <a,b,c,…>` — worktree per agent off `origin/base`, spawn each
  detached on the frozen spec, record runs.
- `status <exp_id>` · `collect <repo> <exp_id>` — monitor via `watch.py` (state +
  `recommended_action` per agent); write each agent's diff (`diff-<agent>.patch`).
- `evaluate <repo> <spec_file> <exp_id> [evaluators]` — anonymized N×M cross-eval, **per-judge randomized
  candidate order** (defeats positional bias), strict-JSON `{scores,best,worst,notes}`, recorded
  agent-keyed. **≥4-evaluator policy (shipped 2026-06-15):** `_ensure_min_evaluators` tops up to ≥4 with
  *neutral non-implementers* (limits self-favoring + more signal); a 2-agent A/B auto-recruits 2 neutrals.
- `synthesize <repo> <exp_id>` — `usefulness_gate()` (a strong agent reads the winner's actual code and
  decides **use/discard by JUDGMENT, not a score cutoff**) → if `use`, spawn the base agent to graft the
  runners-up's panel-named strengths + fix the winner's weaknesses → commit (no push/PR).

**Strategy experiments (H4/H5):** use `strategy_experiment.py` when the arm is a strategy rather than a
single agent (for example, `single(claude)` vs `parallel(claude+cursor+synth)`). It is read-only by
default: `python3 src/strategy_experiment.py --hypothesis H4 --repo owner/repo --spec-file spec.md --exp-id
id --json` normalizes arms, expands the unique implementation agents for `exp_abcd`, and points to the
`strategy.json` metadata path. Active prepare is guarded by both `--prepare --confirm-strategy` and
`ORCH_STRATEGY_EXPERIMENT=1`. The cron research tick still auto-launches only simple single-agent A/B/C/D
jobs; strategy arms need deliberate supervised launch and later arm-level quality/cost attribution.

**Eval-quality rules baked in:** anonymization (letters, not names) + measure self-favoring *after* +
down-weight a judge on its own work; **judge reliability is a measured property** —
`judge_reliability.py` estimates evaluator weights from leave-one-out consensus plus optional human score
anchors, and `_winner_and_harvest` uses those weights once a judge is evidence-ready. The old Gemini
exclusion is now only a not-ready fallback; once the dataset supports it, Gemini counts at its measured
weight. Scores (0–10) are human-facing context only, never the ship gate.

---

## 4. Research arm & usage guard controls (`EVAL_AND_TESTING.md`, `src/research_usage_guard.py`)
- **Opt-in Default (`ORCH_RESEARCH_ARM=0`):** Unattended research tick execution is opt-in by default (`ORCH_RESEARCH_ARM=0`), while explicit owner overrides (`ORCH_RESEARCH_ARM=1` or `ORCH_RESEARCH_USAGE_BYPASS=1` for manual/supervised runs) remain fully supported.
- **Capacity-aware scheduler (`research_scheduler.py`):** never always-on — wakes only on **spare
  capacity** (an idle free/flat seat, or use-it-or-lose-it 5h headroom, or a human "run science" trigger).
  Jobs scored `priority = info_value / capacity_cost`, where `info_value = uncertainty · stakes ·
  staleness`; it **greedily knapsacks the budget** — abundant capacity → an intense job (4–5-way + an
  adversarial panel); scarce → a cheap single-judge spot-check. **Same queue, intensity scales to budget.**
- **Deterministic Research Usage Guard (`src/research_usage_guard.py`):** local, zero-network, zero-LLM usage guard. Every followup opportunity evaluates rolling limits and anomaly spike detectors (evaluator call count, prompt byte count, repeated subject share). One opportunity decision is recorded for every eligible followup (`admitted`, `deferred`, `duplicate`, `missing-spec-objective-only`, `blocked_by_limit`, `blocked_by_anomaly`). A separate read-side pass audits recorded evaluator runs for legacy or bypassed missing-spec, wide-panel, and repeated-subject traffic. Detected active anomalies block optional followup and surface in `$ORCH_STATE_DIR/research-usage-report.json`.
- **Missing-Spec Zero LLM Dispatch:** Recovered experiments with missing specs (`missing_spec: True`, `spec_provenance: "missing_spec_stub"`) never launch LLM judges or synthesis. Objective anchors are preserved, an idempotent terminal artifact (`followup-skip.json`) is written, and subject lifecycle is marked as `skipped` (`reason="missing_spec_recovered"`).
- **Stable Signature Deduplication:** Prevents repeated unchanged followup panel evaluations by a stable signature covering repository, normalized spec hash, base SHA, and candidate diff hashes. Judge identity and panel width are excluded so configuration changes cannot bypass immutable-input deduplication. Decisions are persisted on disk (`followup-decision.json`, `eval-maps.json`) and in the opportunity ledger so restarts cannot re-spend.
- **Evaluator Panel Size Semantics:** Direct/manual `evaluate` calls retain the 4-evaluator default (neutral judge top-up), while unattended followup starts with one Vibe judge. Wider calibration panels require an explicit `ORCH_FOLLOWUP_EVALUATORS` list.
- **Arm selection = Top-Two Thompson with variable N (2–5):** best-arm *identification* (not reward
  maximization) — include the current leader + the highest-uncertainty challenger, add random extras up to
  N as capacity permits (randomization prevents the self-confirming blind spot; Top-Two seeding keeps it
  efficient). N scales with capacity.
- **Hypothesis registry:** falsifiable comparative-advantage claims with `status`; seeded with ideas to
  test (e.g. "cursor composer ≥ premium seats on well-specified integration implements"; "gemini's huge
  context wins on large-file comprehension"; H4/H5 multi-agent-strategy claims).
- **Multi-agent strategy is a first-class arm:** an arm can be a single agent **or**
  `{parallel:[high,low], synthesize:true}`; value = synthesized-best quality vs **total** cost (sum of
  arms) vs the single-agent baseline. The system must not preclude "multi-agent sometimes wins."
- **Adversarial review mode:** reviewers prompted to **refute** (reject-unless-proven-sound); minority-veto
  / debate ensemble; *adjudicate vs ground truth, don't obey* a lone veto.
- **Features registry (`features.py`, rule-of-three):** an end-of-task reflection logs reusable structures
  `{name, problem_solved, where_used[], maturity}` on a ladder **ad-hoc → reused → hardened** (e.g.
  `exp_abcd` was hardened from an ad-hoc pattern); `features.py record/summary/candidates/harden` is the
  operational reflection CLI, and `periodic_report.py` surfaces registry maturity + promotion candidates
  read-only.

---

## Salient invariants & gotchas (read before you touch it)
- **Local vs Dropbox:** code lives on Dropbox (`Code/Orchestrator/`); the DB, canonical clones, and
  worktrees are **LOCAL** (`~/.codex/orchestrator/`). Never run git worktrees against the Dropbox checkout
  (`git worktree add`/`push` fail — mmap deadlock). Headless/launchd can't read Dropbox → use the mirror
  `~/.codex/orchestrator-mirror/` (refresh via `~/.codex/bin/orch-sync-mirror.sh`) + export `GH_TOKEN`.
- **Prompt handling / approval hygiene:** `dispatcher.py delegate` and `dispatcher.py offload` both accept
  inline `--prompt` and `--prompt-file`. Use inline `--prompt` for compact one-off prompts, reviews, and
  offloads. Reserve `--prompt-file` for large, reusable, or audit-worthy briefs. Do not create disposable
  prompt documents solely to satisfy an offload command, and do not ask for per-file approval for routine
  Orchestrator code/doc edits inside the writable workspace; escalate only for sandbox-required,
  destructive, credential, or irreversible actions.
- **Local agent runtime isolation (fixed 2026-06-16):** child CLIs write mutable state under
  `~/.codex/orchestrator/agent-runtime/`, not real-home dotdirs that Codex may not be allowed to write.
  Cursor keeps real `HOME` for shell/keychain semantics but uses `AGENT_CLI_CREDENTIAL_STORE=memory`,
  runtime `CURSOR_DATA_DIR`/`CURSOR_CONFIG_DIR`/`NODE_COMPILE_CACHE`, and `--trust --workspace .`.
  Vibe uses runtime `VIBE_HOME` plus a copied config whose `session_logging.save_dir` points at the
  runtime log dir; `VIBE_HOME` alone is not enough when the real config contains an absolute
  `~/.vibe/logs/session` path. Gemini keeps real `HOME` so Antigravity keychain auth still works, but
  passes the hidden `--gemini_dir ~/.codex/orchestrator/agent-runtime/gemini/.gemini` flag so project
  metadata, conversations, and app data are writable under the Orchestrator runtime; isolated offloads
  still point `--add-dir` at the isolated copy. Gemini/AGY print mode also passes an explicit requested
  model (`ORCH_GEMINI_MODEL`, default `gemini-2.5-pro`) and Gemini offload failures return a bounded
  `agent_log_tail` from the AGY log when stdout/stderr are empty. Gemini offloads default to a 600s outer
  timeout and align `agy --print-timeout` below it unless a caller explicitly passes a longer `--timeout`.
  cursor=composer (free), codex/claude/vibe=full.
- **Decision capture is wired:** `record_run` fires in both `dispatcher.delegate` (mode=`local`) and
  `delegate_remote` (mode=`remote`); `outcomes.py --mode local|remote|both` closes the loop — local
  resolves the PR by the deterministic `orchestrator/issue-N` branch, remote by PR number. Local delegates
  now use one stable `run_id` across `runs`, ledger start/complete rows, dispatch-log usage reconciliation,
  and the daily cadence runs the local outcome-ingest path fail-open. Dry-runs include `skipped_details`
  and `pending_durability_details` so open PRs, missing branch/PR joins, and already-recorded pending
  durability rows are distinguishable.
- **Process evidence is not causal agent evidence:** `keepalive_outcomes.py --include-non-agent` also
  records terminal bot/human/unlabeled PRs as `source=keepalive`, `assignment=none`, `agent=none`, and
  classified `work_type`. These rows feed repo/process fragility signals surfaced by `periodic_report.py`
  and `observability_dashboard.py`, but `relearn_quality()` excludes them from `route_weights` because only
  `assignment='experimental'` is causal routing evidence.
- **Transport qualification is not model identity or quality evidence:** a sealed, pinned,
  no-fallback Sol/Terra/Luna canary may qualify the Workflows transport and CLI-reported profile
  contract for future instrumentation. It must keep provider-resolved provider/model null and
  unclaimed, and it cannot write the Brain, update quality weights, or promote profiles. Use
  `model_profile_trial_bridge.py qualify`; provider-attested finalization remains a separate,
  stronger boundary.
- **Every module has `--selftest`** — run it (and `orch-sync-mirror.sh`) after edits.

## Build status (honest — designed vs implemented)
- **Implemented + selftested:** store + 9-table schema; durability label; prior→posterior learner
  (quality-magnitude + production-outcome blend + free-agent-cost fixes); versioned weights; eval matrix;
  the `evidence_gaps` /
  `evidence_types` growth mechanism; `features.py` reflection CLI + periodic-report surface;
  `exp_abcd` (prepare→evaluate→synthesize→usefulness_gate) incl. the
  ≥4-evaluator policy; `research_scheduler.py` (hypothesis registry + acquisition/knapsack + Top-Two
  variable-N) plus `strategy_experiment.py` for guarded H4/H5 strategy-arm plan/prepare metadata;
  `features.py`; `langsmith_pull.py` first increment for `langsmith-fleet/v1` artifact
  ingestion with exact-run-id plus GitHub-ref/agent bridging; sustained ε-greedy router exploration;
  offload no-commit guard + isolated workspaces; Gemini/Antigravity `--gemini_dir` runtime isolation for
  Codex-run AGY offloads; `testgen_lane.py` gate-backed generated-test lane prompts
  plus `task_type=testgen` routing/classification, with `testgen_gate.py` live-exercised on
  Inv-Man-Intake `workflow_validation` via isolated Gemini offload (+20 covered lines, 3/3 reliability);
  `langsmith_fetch.py` registry-driven GitHub artifact
  source/cadence with Workflows rollup fallback, transitional `gate-langsmith-fleet` artifact-name
  recognition, expected-vs-exempted repo artifact diagnostics, missing expected repo Actions-run
  diagnostics with producer-CI run attribution, Workflows reusable-CI fallback rows for implemented repos
  that produced no fleet artifact, and artifact-distribution diagnostics;
  `langsmith_direct.py`
  direct LangSmith API source/cadence with SDK-free stdlib fallback; `ledger_reconcile.py` local delegate
  ledger/log cost reconciliation;
  `ccusage_reconcile.py` unique-window per-run attribution for Codex/Claude ccusage sessions;
  Thompson-hybrid router exploration plus the `exploration_review.py` default-review gate,
  `exploration_evidence_plan.py` acquisition planner, guarded `exploration_collection.py` supervised
  collection windows, `exploration_backfill.py` guarded route-coverage backfill plans, and dedicated
  `runs.routing_metadata` capture for future per-mode outcome comparison;
  `range_lane_rollout.py` guarded specialized-lane rollout surface for opener-only
  `testgen`/`epic`/`codemod`/`cross_repo`/`runtime_ac` backlog work with dry-run previews and
  `--apply --confirm-rollout` + `ORCH_RANGE_LANE_ROLLOUT=1` active dispatch;
  Gemini progress-only offload stdout fails closed and interrupted offloads write completion
  rows; `watch.py` conservative stall/drift watcher (running/progress/stalled/exited + advisory root-cause
  and changed-path drift hints) plus `redirect_policy.py` history-aware
  wait/collect/inspect/redirect/decompose advice, `redirect_plan.py` safe recovery plans with guarded
  `--apply --confirm-target` execution for redirect/decompose, and cadence-wired `redirect_sweep.py`
  active-claim shadow reports with an opt-in sweep-to-RedirectAgent shadow corpus bridge and `--doctor`
  preflight for cadence/corpus/readiness state;
  `frontend_verify.py` live-exercised against Trip Planner (`/health`, `/login`, login→signup click flow)
  with structured diagnostics for browser/setup/target failures and a CDP browser-endpoint fallback for
  sandboxed automation, plus JSON-compatible `frontend_verify.py --doctor` preflight for helper/node/CDP
  endpoint readiness;
  `partitioned_review.py` bounded review/reconciliation plans routed through `dispatcher.py review-corpus`,
  with strict six-category results, raw-name-scan rejection, per-partition offload/source provenance,
  resumable timeout/failure envelopes, and synthesis that fails closed on missing or stale partitions;
  `local_verify.py` local deliberate-break verifier whose
  `FAIL_HOLLOW`/`FAIL_BROKEN` verdicts can patch `outcomes.verifier_verdict` and count against relearn;
  adversarial high-stakes merge review hook in `tick.py` (advisory, opt-in active execution);
  AGY/Gemini windowed-prepaid capacity policy (5h+weekly soft-unit estimates, reserve/drain router hints);
  repo playbook injection plus snapshot-derived suggestion queue and explicit approval/apply path
  plus conservative repo-doc/review-comment/PR-comment suggestion mining (`repo_knowledge.py` + dispatcher
  prompt augmentation). Suggestions are deterministically clustered/deduped with extra evidence retained.
  Approved playbook entries can also be exported into committed `AGENTS.md` files as a small managed block
  via `repo_knowledge.py --export-agents-md` / `--repo-path --apply`, with `--validate-agents-md` for
  freshness checks.
  `repo_knowledge.py --search` retrieves approved playbook entries plus retained outcome notes with
  compound/path token expansion, local TF-IDF weighting, section boosts, and matched-term explanations.
  Mining is strictly a review queue: unreviewed text is never auto-injected into delegated prompts and only
  becomes prompt context after `--approve-suggestion ... --apply`.
- `periodic_report.py` read-only dataset review: DB/snapshot health, learned weights vs prior/previous
  version, recent outcomes/costs/traces, judge-reliability weights, human-calibration regression
  readiness, exact and clustered evidence-gap proposals, feature-registry maturity/promotion candidates,
  dry-seam/liveness findings from `dry_seam_audit.py` including outcome-gap classification into actionable
  production-ingest candidates vs offload/experiment/role/advisory rows, production-flow freshness for real
  non-advisory runs, process-improvement rollups/signals for non-causal keepalive maintenance rows, the
  deferred live keepalive-supervisor trigger gate, LangSmith artifact-distribution vs durable telemetry-sink
  health, hypothesis status, and `--approve-evidence-type` preview/apply for recurrence-gated schema growth.
  `orchestrate.sh` writes the weekly JSON report to local
  Orchestrator state.
- `observability_dashboard.py` compact read-only operator dashboard: folds `periodic_report.py` plus a live
  capacity snapshot into productivity/quality scorecards (outcome coverage, merged rate, durable-success
  rate, durability failures), learned top-agent-by-task, process-improvement signals/non-durable issue
  failures, keepalive-supervisor gate status, capacity state, production-flow freshness, LangSmith telemetry
  vs artifact-distribution health, data-health/dry-seam alerts plus outcome-gap category counts, durability
  sweep cadence-stamp freshness, Stage 2 keepalive live-candidate/link-flow alerts, and JSON/Markdown output. The weekly cadence writes both
  `observability-dashboard.json` and `.md`.
- `epic_lane.py` epic plan builder and schema validator: emits strict planner prompts for vague/large goals,
  validates subtask/dependency/integration/re-decomposition structure, and exposes dispatch-ready prompt
  records; `task_type=epic` is wired through backlog labels, router defaults, and dispatcher templates.
- `codemod_lane.py` refactor-campaign helper: emits strict campaign-authoring prompts, validates campaign
  JSON for metadata/scope/recipe/rollout/checks, and produces safe dry-run plans with review-before-run
  commands; `task_type=codemod` is wired through backlog labels, non-Claude router defaults, and dispatcher
  templates. Automatic codemod apply and batched PR rollout remain later increments.
- `cross_repo_lane.py` cross-repo coordination helper: emits strict source+consumer authoring prompts,
  validates coordination JSON for contract changes, consumers, rollout barriers, and prompts, and produces
  safe dry-run rollout plans with dispatch-ready source/consumer/review prompts; `task_type=cross_repo` is
  wired through backlog labels, Gemini-first non-Claude router defaults, and dispatcher templates.
- `runtime_ac.py` runtime acceptance-criteria verifier helper: emits strict AC-bound evidence-authoring
  prompts, validates runtime verification specs for metadata, runtime context, AC evidence requirements,
  frontend/command/deliberate-break/manual checks, non-regression checks, and verdict policy, and produces
  advisory dry-run verification plans with review-before-run commands. It can also actively run selected
  checks only with `--confirm-run`, separately gates command/non-regression execution behind
  `--allow-command-checks`, refuses shell-control commands, evaluates external result JSON, and can patch
  `outcomes.verifier_verdict` with `PASS_RUNTIME_AC`/`FAIL_RUNTIME_AC`/`NEEDS_REVIEW_RUNTIME_AC`.
  `runtime_ac_gate.py` centralizes closer-gate requirement detection/execution and provides a read-only
  backlog scanner for required closer gates, missing specs, and active-run blockers, plus a local history
  scanner that finds archived runtime-AC gate events in the cron log and retained runtime-AC rows in the
  feedback DB. `tick.py` invokes required
  runtime AC gates for closer PRs marked by label or spec file before remote delegation, reports planned
  gates in dry-run, and blocks active closer progression on missing specs, disabled required gates, or
  non-PASS verdicts. `runtime_ac_gate.py --exercise` runs a non-mutating active-gate command smoke when no
  current closer requires the gate. `merge_guard.py` applies the same gate before explicit terminal
  `gh pr merge` actions and records a pending merge outcome when it can join to a remote run.
  `runtime_ac_panel.py` adds the first
  multi-judge AC adjudication layer: it builds strict judge prompts from a spec + gate, aggregates returned
  reviewer JSON with corroborated-veto logic, ignores bare unsubstantiated `FAIL` labels when a strong
  passing panel is otherwise present, can dispatch reviewer offloads directly via `--dispatch`, can patch
  `outcomes.verifier_verdict`, and records reviewer evidence gaps for dataset growth. `task_type=runtime_ac`
  is wired through backlog labels, Gemini-first non-Claude router defaults, and dispatcher templates.
- `roles.py` agent-role learning surface: role invocations can now be recorded as
  `task_type='role:<name>'` via `feedback.record_role_run()`, linked to the downstream run outcome they
  influenced via `feedback.join_role_to_outcome()` / `roles.py link-outcome`, and relearned as role-specific
  backend-fit weights. `route_role()` prefers learned `role:<name>` weights before falling back to the
  generic `route_as` prior, keeping role learning separate from normal implement/review routing.
- `redirect_shadow.py` RedirectAgent measurement ramp: real `watch.py` reports can be dispatched through
  RedirectAgent in shadow and appended to a local JSONL corpus with baseline-vs-proposal disagreement,
  validity, backend, role_run_id, and dry-run plan metadata. Accepted/applied role advice can be linked to
  downstream outcomes through `redirect_shadow.py link-outcome`, and summaries expose
  `ready_for_supervised_apply` while keeping autonomous redirect disabled. Historical keepalive-shadow rows
  can be surfaced with `redirect_shadow.py historical-candidates`, but they remain replay candidates only;
  they do not count as RedirectAgent evidence until replayed through a fresh/blinded RedirectAgent proposal
  and linked to an outcome. `redirect_sweep.py --record-corpus --dispatch-redirect-agent` can now feed
  capped, deduped sweep-origin proposal rows tagged `source=redirect-sweep-live`; the cadence enables that
  only when `ORCH_REDIRECT_SWEEP_RECORD_CORPUS=1`. Invalid dispatch rows may include bounded
  `backend_error_detail_*` diagnostics when a backend such as Gemini fails before producing JSON.
- `keepalive_supervisor.py` Stage 1 supervised-candidate planner: read-only, post-escalation only
  (`needs-human` / `agent:needs-attention`), writes RedirectAgent report JSON, review-only proposal
  commands, Stage 2 corpus-record commands, and outcome-link templates, and never labels, delegates,
  releases claims, or applies redirect plans. `keepalive_supervisor.py --stage2-plan` now turns the
  proposal-corpus state into the next acquisition command: valid-proposal-de-duped live Stage 2 record
  commands, strict historical replay, or calibration replay when strict candidates are exhausted and
  disagreement evidence is still thin. Use `--stage2-backend <agent>` to pin future live Stage 2 evidence
  commands when deterministic collection is needed or a backend is unhealthy. `periodic_report.py` and
  `observability_dashboard.py` expose the Stage 2
  RedirectAgent proposal-corpus counts/readiness, but later stages still require synced live role-outcome
  evidence before any supervised apply path exists.
- `roles.py` PromptAgent: `roles.py prompt` turns a target/goal/context into a shadow-only, dispatch-ready
  prompt with definition-of-done, acceptance criteria, validation, scope boundaries, and risk flags. It uses
  `route_role("prompt")`, records live role runs as `role:prompt`, rejects task-type changes/persona leakage/
  duplicated repo-playbook text, and never calls `dispatcher.delegate`.
- `roles.py` DecomposerAgent: `roles.py decompose` turns a large/vague goal into a shadow-only,
  `epic_lane.py`-validated subtask DAG with dispatch prompts, dependencies, integration order, final
  verification, and re-decomposition triggers. It uses `route_role("decomposer")`, records live role runs
  as `role:decomposer`, reuses `epic_lane.validate_plan()`/`build_dispatch_prompts()`, and never delegates.
- `roles.py` TriageAgent: `roles.py triage` turns a backlog snapshot into shadow-only
  work-now/defer/needs-scope/skip/monitor recommendations plus advisory batches. It uses
  `route_role("triage")`, records live role runs as `role:triage`, requires exact visible-target coverage,
  rejects unknown or duplicate targets/batch IDs, falls back to deterministic backlog order, and never
  selects worker agents, claims, labels, delegates, or mutates state. `backlog.py` now retains issue/PR
  bodies so triage can judge underspecification from more than titles and labels.
- `roles.py` AdjudicatorAgent: `roles.py adjudicate` turns a single disputed blocker/veto case plus
  supplied ground-truth evidence into shadow-only `uphold_blocker` / `reject_blocker` /
  `needs_more_evidence` guidance. It uses `route_role("adjudicator")`, records live role runs as
  `role:adjudicator`, rejects terminal/mutating output keys, requires ground-truth refs for uphold/reject
  decisions, and never replaces `runtime_ac_panel.adjudicate_panel()` or `adversarial.aggregate_veto()`.
- `judge_reliability.py` first increment: summarizes evaluator reliability from the retained
  cross-evaluation matrix, includes simple human score anchors when available, gates weights behind minimum
  comparison/experiment thresholds, feeds `exp_abcd` synthesis, and is surfaced by `periodic_report.py`.
- `human_calibration.py` first increment: parses structured human score anchors, joins them to proxy
  evaluator scores, fits a simple calibration regression only when enough matched pairs exist, and is
  surfaced by `periodic_report.py` / `observability_dashboard.py`. It remains data-gated and does not alter
  learner weights while human anchors are absent.
- `evidence_schema.py` first increment: clusters recurring free-text `evidence_gaps` into approval-ready
  evidence-type candidates, surfaces them in `periodic_report.py` / `observability_dashboard.py`, keeps
  active approval guarded by `--apply NAME --confirm-type NAME`, and now reviews active-type influence/prune
  readiness. `exp_abcd.py evaluate` and `runtime_ac_panel.py` prompt reviewers to return
  `cited_evidence_types`; known active citations increment `evidence_types.influence`, while unknown names
  are ignored.
- `consumer_sync_artifact_ingest.py` closes the consumer-sync evidence seam: it validates a successful
  Workflows artifact and run-bound handoff, hashes downloaded remote default-branch snapshots, and records
  idempotent local outcomes for a capped registered cohort. The cadence is active-only and read-only with
  respect to GitHub and consumers; its human-on-exception phase is explicitly dated and self-reverting.
- **Integration seams (foundation exists, wiring pending):** richer task_type taxonomy; evidence-backed
  decision on whether Thompson-hybrid should become the default once outcome volume supports comparison.

## Pointers (depth)
- `FEEDBACK_LOOP.md` — feedback architecture (the four things an elite loop needs).
- `EVAL_AND_TESTING.md` — the research arm (scheduler, dataset growth, A/B, adversarial, features); has citations.
- `ORCHESTRATOR.md` — the seat's operating manual (think → delegate → monitor → redirect; route-table prior).
- `PLANNING.md` — remaining-work task list.
- Modules: `feedback.py` (store + learner) · `outcomes.py` (loop closure) · `exp_abcd.py` (A/B harness) ·
  `judge_reliability.py` · `human_calibration.py` · `evidence_schema.py` · `research_scheduler.py` ·
  `strategy_experiment.py` · `features.py` · `watch.py` · `testgen_lane.py` · `epic_lane.py` ·
  `codemod_lane.py` · `cross_repo_lane.py` · `exploration_backfill.py` ·
  `runtime_ac.py` · `runtime_ac_gate.py` ·
  `runtime_ac_panel.py` · `merge_guard.py`.
  Each has `--selftest`.
