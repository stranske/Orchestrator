# Orchestrator — planning & remaining work

The local multi-agent orchestrator: Claude (rotatable) assesses each task and delegates the token-heavy
coding/reading to cheaper agents (cursor/vibe/gemini/codex/aider), then monitors, redirects, and **learns
from outcomes** which agent/strategy fits which work. Design docs: [`ORCHESTRATOR.md`](ORCHESTRATOR.md)
(operating manual), [`FEEDBACK_LOOP.md`](FEEDBACK_LOOP.md) (learning store), [`EVAL_AND_TESTING.md`](EVAL_AND_TESTING.md)
(research arm: when/who/intensity, dataset growth, testing system, feature recognition).

## Layout (read before moving anything)

- **`Code/Orchestrator/` (here, Dropbox)** — all CODE (`*.py`), DOCS (`*.md`), launchers (`*.sh`),
  `experiments/` (specs, eval outputs, diff patches, `hypotheses.json`, `features.json`), `data/`
  (`feedback-snapshot.json` — the reviewable copy of the dataset), this file.
- **`~/.codex/orchestrator/` (LOCAL disk, NOT Dropbox)** — `repos/` + `worktrees/` (git checkouts),
  `feedback/orchestrator.db` (live SQLite), `aider-venv/`. **These cannot live on Dropbox**: it rejects
  `git worktree add` ("Operation not permitted") and deadlocks `git push` ("mmap"), and can corrupt a
  live SQLite file. Code defaults (`provision.LOCAL_RUNTIME`, `feedback.LOCAL_RUNTIME`) point here;
  override with `ORCH_LOCAL_RUNTIME` / `ORCH_REPOS_DIR` / `ORCH_WORKTREE_BASE` / `ORCH_FEEDBACK_DB`.
- Run `feedback.snapshot_json()` to refresh `data/feedback-snapshot.json` after meaningful runs.
- **Architecture is a contract**: [`ARCHITECTURE.md`](ARCHITECTURE.md) + `orchestrator-loop.svg` are the
  source of truth for the rails-vs-agent-roles taxonomy and the loop. Any change to the stages,
  components, the rail/role classification, the feedback surfaces, or the role registry (`roles.py`)
  MUST update both in the same change. Do not agentify the Decide-stage `router` selection — it's the
  learner's signal.

## Built + selftested modules

capacity · backlog · claims · provision · adapters · router (reads learned weights) · dispatcher
(records every decision) · feedback (store + Beta-Binomial learner + quality-magnitude + growth/prune +
snapshot) · exp_abcd (A/B/C/D + per-judge-randomized cross-eval + judgment ship-gate + synthesize) ·
research_scheduler (spare-capacity gate + info/cost knapsack + Top-Two variable-N + hypothesis registry) ·
strategy_experiment (guarded H4/H5 strategy experiment planner/prepare metadata) ·
features (rule-of-three ladder + reflection CLI) · adversarial (refute + minority-veto) · watch (stall/drift watcher) ·
redirect_policy · redirect_plan · local_verify · durability_sweep · relearn_report · periodic_report ·
dry_seam_audit ·
frontend_verify · testgen_gate · testgen_lane · epic_lane · codemod_lane · cross_repo_lane ·
runtime_ac · runtime_ac_gate · runtime_ac_panel · merge_guard · langsmith_pull · keepalive_outcomes ·
repo_knowledge ·
roles (route_role + RedirectAgent + PromptAgent + DecomposerAgent + TriageAgent + AdjudicatorAgent
shadow roles).

---

## Remaining elements — task list

### A. Data-gated (need accumulated experiment/outcome data first — expected to be a while)
These are designed; implementing them earlier than the data supports would be premature. Accumulate by
running experiments opportunistically (the research scheduler) and refreshing the snapshot.

- [ ] **Tune `PRIOR_STRENGTH` (k).** Observed at n=1: a single experiment flipped codex above claude for
      `implement` — too eager. Raise k (or make it task-type-specific) once enough outcomes exist to
      calibrate how fast evidence *should* overtake the prior. (feedback.py)
- [x] **Data-driven judge-reliability weighting, first increment.** `judge_reliability.py` estimates judge
      weights from leave-one-out consensus over the retained evaluation matrix plus optional structured
      human score anchors. `exp_abcd._winner_and_harvest` now uses ready weights and keeps the old Gemini
      exclusion only as a not-ready fallback; `periodic_report.py` surfaces the summary. Full
      human-calibrated bias correction still waits on more human labels.
- [ ] **Human-calibration regression.** A small set of human spot-labels → regression bias-correction on
      the proxy scores. Needs human labels (none yet; the user reviews rarely by preference). (feedback.py)
      First wiring increment: `human_calibration.py` now parses structured human score anchors, joins them
      to evaluator proxy scores, fits a simple regression only after enough matched pairs exist, and is
      surfaced by `periodic_report.py` / `observability_dashboard.py`. The remaining work is to collect
      real human labels and compare raw vs calibrated proxy decisions before applying correction to
      learning.
- [ ] **Strategy-value learning (H4/H5).** Learn when multi-agent assignment (e.g. high+low pair + synthesis)
      beats a single agent despite the cost. Needs multi-agent experiments actually run. (research_scheduler +
      feedback: record strategy outcomes via `decomposition`)
      First runner increment: `strategy_experiment.py` now normalizes H4/H5 arms, expands strategy arms into
      the unique implementation-agent set for `exp_abcd`, writes durable `experiments/<exp_id>/strategy.json`
      metadata, and exposes a guarded active prepare command requiring `--prepare --confirm-strategy` plus
      `ORCH_STRATEGY_EXPERIMENT=1`. `research_scheduler.py` still refuses to auto-launch strategy arms from
      cron, but skipped H4/H5 diagnostics now include a concrete `strategy_experiment.py` planning command.
      Remaining work: run real guarded H4/H5 experiments, collect/evaluate/synthesize them, and record
      arm-level quality/cost outcomes before promoting/refuting the strategy hypotheses.
- [ ] **Promote/refute the seeded hypotheses** (H1–H5 in `experiments/hypotheses.json`) as evidence
      accumulates; update `status` open→accumulating→supported→refuted.
- [ ] **Meta-eval schema growth in practice.** Capture `evidence_gaps` during *real* evals, let recurring
      gaps drive `propose_evidence_changes` → `record_evidence_type`; prune dead evidence. Needs eval volume.
      First wiring increment: `exp_abcd.py evaluate` now asks cross-evaluators for `evidence_gaps` and
      records non-empty responses into `feedback.evidence_gaps`; the remaining work is to run enough real
      evals for recurrence proposals and pruning decisions to become meaningful.
      Second wiring increment: `evidence_schema.py` now clusters recurring free-text gaps into evidence-type
      candidates when exact string recurrence is too sparse, surfaces them in periodic/dashboard reports,
      and offers guarded approval via `--apply NAME --confirm-type NAME`. The remaining work is explicit
      type approval plus later influence/pruning once active types are cited.
      Third increment: the first five concrete clustered types were approved, `exp_abcd.py evaluate` and
      `runtime_ac_panel.py` now ask for `cited_evidence_types`, and active citations increment
      `evidence_types.influence`. Remaining work is to let real evaluator traffic cite or ignore those
      types, then prune stale uncited types after the review window.
      Follow-up 2026-06-23: the two remaining clustered proposal classes were approved with the guarded
      path: `documentation_help_evidence` (9 matching gaps) and `browse_dialog_evidence` (5 matching
      gaps). Live status is now `active evidence_types=7`, `open_gap_rows=122`,
      `clustered_proposal_count=0`, and `prune_candidate_count=0`; the item is back to waiting on real
      evaluator traffic to cite or ignore active types before pruning.

### B. Infra-gated (need live integration / external access)
- [x] **LangSmith trace artifact pull, first increment.** `langsmith_pull.py` ingests local Workflows
      `langsmith-fleet/v1` NDJSON artifacts, joins by known Orchestrator `run_id`, records trace refs in
      `execution_traces`, and aggregates token/$/latency into `costs(source=langsmith)`.
- [x] **Remote trace run_id bridge.** `langsmith_pull.py` now falls back from exact `run_id` to
      `github_pr`/`github_issue` + `domain.agent` when LangSmith owns the trace run_id; remote delegation
      records `pr_number` for more robust target matching.
- [x] **Automated LangSmith source/cadence, first increment.** `langsmith_fetch.py` downloads the latest
      registered GitHub Actions `langsmith-fleet.ndjson` artifacts, combines them locally, and scheduled
      cadence calls it with `--ingest` before artifacts age out. `langsmith_direct.py` separately pulls
      `workflows-agents` directly from the LangSmith API, reuses the same join path, and falls back to
      stdlib HTTP when the SDK is not installed.
- [x] **Local-run ledger reconciliation, first increment.** Local CLI delegates now write start/complete
      rows with the same `run_id` used by `feedback.runs`; `ledger_reconcile.py` parses dispatch-log JSON
      usage into `costs(source=ledger)` while preserving richer `source=langsmith`/`source=ccusage` rows.
- [x] **ccusage per-run attribution, first increment.** `ccusage_reconcile.py` maps Codex/Claude ccusage
      sessions to completed same-agent Orchestrator run windows only when the session `lastActivity` has a
      unique match, writes `costs(source=ccusage)`, skips ambiguous/active windows, and runs in daily cadence.
- [x] **Durability sweep (cron).** Re-check merged PRs days later (reverted/reworked/reopened/broke) →
      patch `outcomes.durability`; `orchestrate.sh` runs it daily, fail-open.
- [x] **Autonomous scheduler loop.** `research_scheduler.build_research_plan()` now wires live-shaped
      `capacity.py` snapshots + `backlog.py` items + hypotheses + learned posteriors into a capacity-gated
      `exp_abcd` plan; `tick.py.research_tick()` consumes that plan, writes a frozen issue spec, and can
      launch one isolated A/B per active tick when `ORCH_RESEARCH_ARM=1`. Shadow mode remains default, and
      active launches are claim-gated. Strategy arms remain blocked from cron auto-launch, but skipped H4/H5
      diagnostics now point to `strategy_experiment.py`, the guarded strategy-aware runner surface.
- [x] **Stable local delegate `run_id` + task_type capture.** Local delegate rows now use one durable
      `run_id` across `runs`, ledger start/complete rows, and dispatch logs; dispatcher records the passed
      `task_type` instead of relying on PID-derived IDs. `outcomes.py --mode local|remote|both` now exposes
      the local outcome-ingest path from the CLI and the daily cadence runs local delegate outcome ingest
      fail-open, resolving local issue targets through the deterministic `orchestrator/issue-N` PR branch.
      Dry-run attribution now separates new recordable outcomes, `open_pr` waits, `no_pr_for_branch` join
      gaps, and already-recorded pending-durability rows.

### C. Buildable when prioritized (not data/infra-gated)
- [x] **Sustained ε-greedy exploration.** `router.select_agent` can route a small fraction of selections
      to a same-tier, least-observed eligible challenger (`ORCH_EXPLORATION_RATE`, default 0.05) so the
      learner does not only observe the current favorite.
- [x] **Thompson-hybrid exploration, first increment.** `router.select_agent` supports opt-in
      `ORCH_EXPLORATION_MODE=thompson-hybrid`, keeping the ε safety cap while choosing same-tier
      challengers with a reconstructed Beta posterior sample from learned `posterior`/`n_obs` plus the
      effort-adjusted `score`. Default routing remains ε-greedy until enough outcome volume supports a
      default switch.
- [x] **Exploration-policy default review gate, first increment.** `exploration_review.py` reads current
      route weights, simulates ε-greedy vs `thompson-hybrid`, and refuses to recommend a default change
      until direct instrumented exploration evidence is ready. `dispatcher.py` records router assignment
      metadata in `feedback.runs.routing_metadata`, preserving `assignment` for causal-learning filters.
      Trigger for the next stage: at least 30 outcome-bearing exploration runs for each mode across at
      least 3 task types, plus route-weight coverage gates in the report.
- [x] **Exploration evidence acquisition planner, first increment.** `exploration_evidence_plan.py` reports
      remaining ε-greedy and `thompson-hybrid` outcome deficits, candidate low-risk opener task types,
      route-weight cells below coverage gates, and safe opt-in window settings. `periodic_report.py` and
      `observability_dashboard.py` expose the stage and remaining deficits. It is read-only and does not
      dispatch, mutate router policy, or count synthetic evidence.
- [x] **Exploration supervised collection windows, first increment.** `exploration_collection.py` builds
      bounded, opt-in windows that alternate or choose between `ORCH_EXPLORATION_MODE=epsilon-greedy` and
      `ORCH_EXPLORATION_MODE=thompson-hybrid` on low-risk opener work only. It is dry-run by default;
      active dispatch requires `--apply --confirm-window` and `ORCH_EXPLORATION_EVIDENCE=1`. The safety
      filter rejects closer/merge-critical work, late/paygo or backup agents, and any default-policy change.
- [x] **Exploration route-coverage backfill, first increment.** `exploration_backfill.py` implements
      Stage 3 from `IMPROVEMENT_BACKLOG.md` item 1: it detects missing `(task_type, agent)` route cells,
      plans guarded `exp_abcd` A/B backfill jobs for real unclaimed opener subjects, exposes status in
      `periodic_report.py` and `observability_dashboard.py`, and keeps active launch behind
      `--apply --confirm-backfill` plus `ORCH_EXPLORATION_BACKFILL=1`. It counts nothing itself; route
      evidence advances only after `exp_abcd evaluate` records real evaluations or production outcomes are
      ingested. Live 2026-06-22: `waiting_for_direct_mode_progress`, 36 missing cells, 0 planned jobs
      because the current backlog snapshot has no unclaimed opener subjects.
- [x] **Offload no-commit guard + isolation.** `dispatcher.py offload` injects non-git/no-commit rules and
      supports `--isolate` / `--worktree-isolation` to copy `cwd` into a persistent local offload workspace
      for parallel code-building proposals.
- [x] **Offload agent runtime hardening.** Cursor, Vibe, and Gemini offloads now get durable per-agent
      runtime state under `~/.codex/orchestrator/agent-runtime/` and pass live isolated-dispatch smokes in
      Codex without using Claude. Gemini/Antigravity specifically keeps real `HOME` for keychain auth while
      `--gemini_dir` redirects project metadata and app data into the writable runtime; this fixed the
      Codex-only `no active conversation` failure on unregistered repos. Gemini progress-only stdout
      ("waiting for pytest; I will inspect later", or task-handle wait text) now fails closed with a
      nonzero offload result instead of being accepted as a usable review. Gemini/AGY print mode now passes
      an explicit requested model (`ORCH_GEMINI_MODEL`, default `gemini-2.5-pro`) so Antigravity 1.0.10
      cannot silently fail with "neither PlanModel nor RequestedModel specified"; Gemini offload failures
      also return a bounded `agent_log_tail` from the AGY log when stdout/stderr hide the real cause. Gemini
      offloads now default to a shorter 600s outer timeout (`ORCH_GEMINI_OFFLOAD_TIMEOUT`) and align
      `agy --print-timeout` under that budget so silent streaming calls fail faster unless a caller
      explicitly passes a longer `--timeout`. Interrupted offloads also record `exit=130` completion rows so
      the capacity ledger does not retain start-only runs.
- [x] **Periodic report generator.** Human-facing review of the dataset: current learned weights vs prior,
      route-weights version diffs (did a change help?), proposed schema changes, hypothesis status. Reads
      the snapshot/DB (`python3 src/periodic_report.py [--json] [--snapshot-json PATH]`). Read-only by default.
      This is the human's window into the loop.
- [x] **Feature reflection CLI + report surface.** `features.py record` now records task-end reusable
      structures with optional module/maturity metadata; `summary`, `candidates`, and `harden` expose the
      rule-of-three ladder; `periodic_report.py` includes registry maturity and promotion candidates while
      staying read-only and not creating the registry as a side effect.
- [x] **Production-outcome blend in quality learner.** `feedback.relearn_quality` now records one reward per
      run: mean cross-eval score when evaluations exist, otherwise durability/verifier production outcome
      reward. `relearn_report` writes `route_weights` from both A/B evidence and real production outcomes
      without double-counting multi-reviewer evaluation runs.
- [x] **Test-generation lane prompt wiring.** `testgen_lane.py` builds a gate-backed delegation prompt from
      source/baseline/candidate pytest args; `backlog.py` classifies tests/coverage labels as
      `task_type=testgen`; `router.py` routes `testgen` without defaulting to Claude; `dispatcher.py`
      has a generic test-generation prompt template.
- [x] **Test-generation lane live exercise.** Isolated Gemini/AGY offload generated focused pytest coverage
      for Inv-Man-Intake `workflow_validation`; `testgen_gate.py` passed collect/import, baseline
      non-regression, candidate reliability 3/3, and covered-lines delta +20 (83 → 103). The gate now
      forces `coverage json --fail-under=0` so repo-level fail-under settings cannot mask its own verdict.
- [x] **Frontend verifier Trip Planner exercise.** `frontend_verify.py` was run against Trip Planner's
      local full stack for `/health`, `/login`, and a login→signup click flow; verifier failures now carry
      structured `diagnostic`/`hint` fields for setup, target, and browser-launch issues.
- [x] **Frontend verifier sandbox fallback.** `frontend_verify.py` and the local Playwright helper accept
      a Chrome/Chromium CDP browser endpoint via `--browser-endpoint` or
      `ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT`; `runtime_ac.py` carries the same endpoint through generated
      frontend-check commands for sandboxed automation contexts.
- [x] **Dry-seam audit and periodic cadence.** `dry_seam_audit.py` now flags empty/stale Brain sinks,
      orphan cost/trace joins, runs without outcomes, and prior-only routing cells. Outcome gaps are
      classified into actionable production-ingest candidates vs offload/experiment/role/advisory rows,
      and `periodic_report.py` / `observability_dashboard.py` surface those counts while
      `orchestrate.sh` writes weekly reports in local Orchestrator state.
- [x] **Repo-review liveness dimension.** Workflows repo review now includes `liveness_evidence`, requiring
      real sink/output evidence before accepting implemented/wired/scheduled/automated completion claims.
- [x] **Non-agent keepalive process ingest.** `keepalive_outcomes.py --include-non-agent` records terminal
      unlabeled/bot/human PRs as `source=keepalive`, `assignment=none`, `agent=none`, and classified
      `work_type`; the daily cadence passes the flag under the gh core-budget gate so renovate/sync/tooling
      rows can feed process signals without entering route-weight causal learning.
- [x] **Process-improvement report surface.** `periodic_report.py` now summarizes `work_type` outcomes,
      non-agent maintenance rows, failure-backed renovate/sync/tooling/docs process signals, and recent
      non-durable issue runs; `observability_dashboard.py` carries those signals into weekly JSON/Markdown
      with alerts tied to concrete maintenance-loop recommendations.
- [x] **Schema-migration approval flow** surfaced in that report (approve a proposed `evidence_type`).
- [x] **Operator observability dashboard, first increment.** `observability_dashboard.py` builds a
      read-only JSON/Markdown dashboard from `periodic_report.py` plus live `capacity.py`: outcome
      coverage, merged/durable-success/failure rates, pending durability, capacity state, learned
      top-agent-by-task, process-improvement signals, data-health/dry-seam alerts, and
      feature/evidence/judge readiness. The weekly cadence writes `$ORCH_STATE_DIR/observability-dashboard.json`
      and `.md`.
- [x] **Production-flow freshness metric.** `periodic_report.py` now distinguishes real production-flow
      freshness from aggregate run counts by excluding offload/role/experiment/advisory rows, reporting
      7-day production run/outcome counts plus latest-run age, and `observability_dashboard.py` surfaces
      the status in the scorecard with a stale/dry alert.
- [x] **Promote `stall-watcher` to a hardened module.** `watch.py` classifies detached local delegates
      (running/progress/stalled/exited + advisory hints + semantic drift signals + `recommended_action`);
      `exp_abcd.status` reuses it.
- [x] **History-aware redirect/decompose policy.** `redirect_policy.py` adds advisory
      wait/collect/inspect/redirect/decompose decisions over watch reports plus attempt history; `watch.py`
      exposes the result as `policy_decision`.
- [x] **Safe redirect/decompose execution planner.** `redirect_plan.py` converts a watch report and
      `policy_decision` into a dry-run recovery plan with inspection commands, retry/decomposition prompt
      text, and mutating kill/claim-release/delegate steps explicitly marked as confirmation-required;
      `watch.py` attaches this as `redirect_plan`.
- [x] **Guarded redirect/decompose apply mode.** `redirect_plan.py --apply --confirm-target <target>`
      writes the generated prompt before mutating state, refuses placeholder commands, skips stale PIDs,
      releases claims, and delegates the retry/decomposed slice.
- [x] **Automatic redirect watch sweep, shadow-only.** `redirect_sweep.py` scans active local claims,
      reconstructs watch inputs from dispatcher-stamped metadata, classifies them with `watch.py`, and
      writes `$ORCH_STATE_DIR/redirect-sweep.json` on every `orchestrate.sh` cadence tick. It is deliberately
      advisory-only: no kill, claim release, delegation, RedirectAgent dispatch, or `redirect_plan --apply`.
      Live automatic redirect/decompose remains gated on measurement evidence.
- [x] **Redirect sweep to RedirectAgent shadow corpus bridge.** `redirect_sweep.py --record-corpus
      --dispatch-redirect-agent` can now convert capped, deduped actionable sweep rows into RedirectAgent
      proposal evidence tagged `source=redirect-sweep-live`; the `orchestrate.sh` cadence exposes it only
      behind `ORCH_REDIRECT_SWEEP_RECORD_CORPUS=1`. This records measurement evidence, not live recovery:
      no kill, claim release, replacement delegation, or `redirect_plan --apply`.
- [x] **Local deliberate-break verifier, first increment.** `local_verify.py` runs a narrow green check,
      overlays candidate tests onto the base implementation in a temporary copy, and flags hollow tests
      whose checks still pass against base code.
- [x] **Verifier verdict feedback wiring.** `local_verify.py --record-run-id` patches
      `outcomes.verifier_verdict`; `feedback.relearn` treats `FAIL_HOLLOW`/`FAIL_BROKEN` as failures even
      when merge/durability signals would otherwise look successful.
- [x] **Wire adversarial review into the dispatch path** for high-stakes merges. `tick.py` now detects
      closer PRs with explicit high-risk metadata, reports the advisory panel in dry-run, and only runs it
      in active ticks when `ORCH_RUN_ADVERSARIAL_REVIEW=1`.
- [x] **Runtime AC checks, first increment.** `runtime_ac.py` emits strict AC-bound spec-authoring prompts,
      validates runtime verification JSON for metadata/context, per-AC evidence requirements,
      frontend/command/deliberate-break/manual checks, non-regression checks, and verdict policies, and
      produces advisory dry-run verification plans with review-before-run commands. `task_type=runtime_ac`
      is wired through backlog label classification, Gemini/AGY-first non-Claude router defaults, and
      dispatcher templates.
- [x] **Runtime AC active gate, first increment.** `runtime_ac.py --run spec.json --confirm-run` executes
      selected verifier/tool checks and maps results back to acceptance criteria; command/non-regression
      execution requires `--allow-command-checks`, shell-control commands are refused, manual checks yield
      `NEEDS_REVIEW`, `--results ... --result-json ...` gates externally collected evidence, and
      `--record-run-id` can patch `outcomes.verifier_verdict`. `feedback.relearn` treats
      `FAIL_RUNTIME_AC` as a verifier failure.
- [x] **Runtime AC closer tick invocation.** `tick.py` detects closer PRs requiring runtime AC by label or
      spec file, reports planned gates in dry-run, and blocks active closer progression before remote
      delegation when the spec is missing, the required gate is disabled, or the verdict is not `PASS`.
      Passing gates can patch the latest remote run's verifier verdict.
- [x] **Runtime AC terminal merge guard.** `runtime_ac_gate.py` now centralizes closer-gate logic, and
      `merge_guard.py` wraps explicit terminal `gh pr merge` actions with the same runtime-AC policy. It
      dry-runs by default, requires `--confirm-merge` before invoking GitHub, fails closed on unreadable PR
      metadata/draft/non-open PRs, blocks missing/disabled/non-PASS runtime gates, and records a pending
      merge outcome when it can join the target to a remote run.
- [x] **Runtime AC multi-judge panel, first increment.** `runtime_ac_panel.py` builds strict judge prompts
      from a runtime AC spec plus gate evidence, adjudicates reviewer JSON with corroborated-veto logic,
      treats automated gate failure as failure, returns `NEEDS_REVIEW` for lone vetoes/disagreement, and can
      patch `outcomes.verifier_verdict` while recording reviewer evidence gaps for dataset growth.
- [x] **Runtime AC live reviewer dispatch convenience.** `runtime_ac_panel.py --dispatch` sends inline
      offload prompts to comma-separated reviewer agents, parses plain/fenced reviewer JSON, synthesizes
      `NEEDS_REVIEW` records for failed or unparseable reviewers, and adjudicates the panel in one command.
- [x] **AGY/Gemini windowed-prepaid capacity policy.** `capacity.py` models Gemini as estimated compute
      units across 5h + weekly soft budgets, exposes `steady`/`reserve`/`drain` metadata, and `router.py`
      promotes AGY during drain for good-fit reasoning work while conserving it during reserve.
- [x] **Repo playbook prompt injection.** `repo_knowledge.py` stores per-repo gotchas/definition-of-done
      rules in `experiments/repo_knowledge.json`; dispatcher appends the bounded `REPO PLAYBOOK` block to
      generated and manually supplied delegation prompts.
- [x] **Repo playbook suggestion queue.** `repo_knowledge.py --suggest-from-snapshot` mines retained outcome
      notes for candidate gotchas/validation rules without auto-injecting unreviewed text.
- [x] **Repo playbook suggestion approval.** `repo_knowledge.py --approve-suggestion ... --apply` promotes
      a reviewed suggestion into the registry with duplicate protection.
- [x] **Repo playbook doc/comment mining.** `repo_knowledge.py` can now mine the same review queue from
      conservative repo docs (`--suggest-from-docs`; broad root-doc scans require `--include-root-docs`),
      exported review payloads (`--suggest-from-review-json`), and live GitHub PR comments
      (`--suggest-from-pr`) without auto-injecting unreviewed text.
- [x] **Repo playbook suggestion clustering.** Candidate suggestions now get deterministic `cluster_key`
      metadata; near-duplicates merge into one review entry with `occurrence_count` and retained evidence.
- [x] **Repo playbook AGENTS.md export.** `repo_knowledge.py --export-agents-md` emits a small managed
      Orchestrator section from approved registry entries only; `--repo-path --apply` updates committed
      `AGENTS.md` files without overwriting human content, and `--validate-agents-md` checks marker
      currentness plus missing backticked path references for keepalive freshness ownership.
- [x] **Repo memory search, first increment.** `repo_knowledge.py --search` / `search_repo_memory()`
      retrieves approved playbook entries plus retained run/outcome notes for a repo, scoped by task/lane.
      It is read-only and never auto-injects unapproved feedback text.
- [x] **Repo memory retrieval quality, first increment.** Search now expands compound/path tokens such as
      `sync-manifest`, `consumer-facing`, and `docs/ci/WORKFLOWS.md`, keeps scoped playbook rules visible
      when filters are omitted, ranks with local per-query TF-IDF plus section boosts, and returns
      `matched_terms`/`coverage` for explainable prompt-authoring retrieval. External embedding/vector RAG
      remains deferred until local retrieval proves insufficient.
- [x] **Agent-role registry + first role (RedirectAgent).** `roles.py` adds `route_role` (reuses the
      learned router, restricted to a role's eligible backends, claude reserved) and the RedirectAgent
      contract; it PROPOSES into `redirect_plan.py` in shadow (new `prompt_override` carries the
      agent-authored corrected prompt) and never mutates. Selftested. Design: `ARCHITECTURE.md`.
- [x] **Role-outcome feedback wiring.** Close the loop's second surface (role ↔ backend fit): role
      invocations are recorded as `task_type='role:<name>'`, accepted/applied role decisions can be linked
      to the downstream run outcome with `feedback.join_role_to_outcome()` / `roles.py link-outcome`, and
      `route_role()` now prefers learned `role:<name>` weights before falling back to the generic
      `route_as` prior.
- [x] **RedirectAgent advisor measurement foundation.** `redirect_shadow.py` records RedirectAgent
      proposals on real `watch.py` reports into a local JSONL corpus, summarizes proposal validity,
      baseline disagreement, linked/synced outcomes, and exposes `link-outcome` for accepted/applied
      advice. `historical-candidates` surfaces old keepalive-shadow PRs worth fresh/blinded replay without
      counting them as proposal evidence. `roles.py redirect --record-corpus` and `redirect_sweep.py
      --record-corpus --dispatch-redirect-agent` are available as opt-ins, but no autonomous redirect is
      enabled.
- [x] **Keepalive supervisor trigger audit.** `periodic_report.py` now includes a read-only
      `keepalive_supervisor` gate over `keepalive_shadow.summarize()` and `observability_dashboard.py`
      surfaces the status in weekly JSON/Markdown. The gate distinguishes labeled-count readiness from
      failure-signal readiness and keeps live supervision disabled.
- [x] **Keepalive supervisor Stage 1 candidate planner.** `keepalive_supervisor.py` identifies only
      post-escalation keepalive PRs (`needs-human` / `agent:needs-attention`) as single-authority
      candidates, writes RedirectAgent report JSON when requested, and emits review-only proposal commands,
      Stage 2 corpus-record commands, and outcome-link templates without applying labels, claims,
      delegation, or redirect plans.
- [ ] **Keepalive supervisor Stage 2 proposal corpus.** Dispatch RedirectAgent proposals for Stage 1
      candidates via the emitted `stage2_record_command`, record/link outcomes via the emitted
      `outcome_link_command_template`, and require enough linked positive evidence before any supervised
      apply path exists. `periodic_report.py` and `observability_dashboard.py` now expose Stage 2
      readiness counts, but live candidates/outcomes still need to accumulate. 2026-06-22: live
      `keepalive_supervisor.py --list-live` found zero eligible post-escalation targets; seeded the
      historical replay side with three cursor-backed blinded RedirectAgent proposals and withheld outcome
      links (`valid_proposals=3`, `historical_linked_proposals=3`, still not ready for supervised apply).
      2026-06-23: `keepalive_supervisor.py --stage2-plan` now turns the corpus state into the next
      acquisition command: live Stage 2 record commands for unrecorded eligible targets, strict historical
      replay when no unrecorded live targets remain, and calibration replay when strict candidates are
      exhausted but disagreement evidence is thin. It de-dupes already-recorded live targets and reports
      exact deficits. Live `stranske/Pension-Data#596` was recorded as one valid Cursor-backed `wait`
      proposal; strict historical replay was exhausted (40 candidates) and one calibration page sampled.
      Current corpus: `valid_proposals=50`, `live_dispatches=1`, `historical_linked_proposals=49`,
      `historical_linked_disagreements=2`; still not ready because supervised apply requires synced live
      role-outcome links and the historical analysis gate still lacks one linked disagreement.
      Follow-up 2026-06-23: Stage 2 live dispatch surfaced a Gemini/Antigravity failure where API
      connection resets left AGY without a valid requested/plan model and no JSON stdout. Role dispatches
      now preserve bounded backend log-tail diagnostics, RedirectAgent corpus rows store diagnostic
      hash/previews, invalid live Stage 2 proposals no longer consume a live target, and
      `keepalive_supervisor.py --stage2-plan --stage2-backend <agent>` can emit pinned live evidence
      commands. Two Cursor-pinned live Stage 2 records were added afterward
      (`stranske/Manager-Database#1238`, `stranske/Trend_Model_Project#5659`), both valid `wait`
      proposals with no disagreement or mutation. Current corpus: `valid_proposals=95`,
      `live_dispatches=4`, `historical_linked_disagreements=2`; current planner status is
      `waiting_for_candidates` because the remaining eligible live target is already valid-recorded and
      no strict/calibration historical candidates remain.
      Follow-up 2026-06-23: a later `--stage2-plan` found one new live post-escalation candidate,
      `stranske/Pension-Data#608`. The emitted Cursor-backed RedirectAgent command recorded a valid
      `wait` proposal (`role:redirect:cursor:1782241367618087000`) with no disagreement and no mutation.
      Refreshed corpus: `n=110`, `live_dispatches=5`, `valid_proposals=96`,
      `historical_linked_disagreements=2`, `synced_role_outcomes=0`. The planner is back to
      `waiting_for_candidates`: the live target is now already valid-recorded, historical/calibration
      replay remains exhausted, and supervised apply still needs 10 synced live role-outcome links plus
      3 live linked disagreements.
- [ ] **RedirectAgent supervised-apply ramp.** Only after the shadow corpus has enough synced outcome
      evidence (`ready_for_supervised_apply`) should RedirectAgent drive `redirect_plan --apply`, still
      under the existing `--confirm-target` gate.
- [x] **PromptAgent role.** `roles.py prompt` now turns a target/goal/context into a shadow-only,
      dispatch-ready delegation prompt with DoD, AC, validation, expected paths, non-goals, risks, strict
      validation, and `role:prompt` feedback-learning surface. It never delegates or changes router
      selection.
- [x] **DecomposerAgent role.** `roles.py decompose` now turns a large/vague goal into a shadow-only,
      validated `epic_lane.py` plan with dispatchable subtasks, dependencies, integration order, final
      verification, and re-decomposition triggers. It emits dispatch-prompt records but never delegates.
- [x] **TriageAgent role.** `roles.py triage` now turns a backlog snapshot into shadow-only
      work-now/defer/needs-scope/skip/monitor recommendations plus advisory batches. It requires exact
      visible-target coverage, rejects hallucinated targets and invalid batches, preserves deterministic
      router/claim/dispatcher rails, and never selects worker agents or mutates state. `backlog.py` carries
      issue/PR bodies so underspecification judgments are grounded.
- [x] **AdjudicatorAgent role.** `roles.py adjudicate` now turns a single disputed blocker/veto case plus
      supplied ground-truth evidence into shadow-only uphold/reject/needs-more-evidence guidance. It rejects
      terminal verifier verdicts and mutating next steps, preserves `runtime_ac_panel.py` and
      `adversarial.py` aggregation math as code, and never merges, labels, claims, delegates, or mutates
      state.

---

## Orchestrator ↔ keepalive integration (opener/closer) — DESIGN GATE before cron-live

Verified 2026-06-14: the Orchestrator **is designed for cron** (`orchestrate.sh --active` = reap→router→
dispatcher+heartbeat; `handoff-prerun.sh` yield-guard stands the legacy lanes down when the heartbeat is
fresh). It is **not turned on**. The dispatcher today only spawns LOCAL CLI agents — **no remote/label
delegation** (confirmed: grep empty).

**Target model (owner's framing, adopted):** on cron for opener/closer the Orchestrator MOSTLY drives the
remote repo system, with some local coding:
- Remote (bulk): assess → choose agent → apply `agent:X` label → GitHub keepalive runs `reusable-X-run.yml`
  on a runner (remote capacity, not local). Local (minority): bounded fixes + offloaded reading.
- Orchestrator's own work: discover → assess (route-table + learned weights + capacity) → label → drive
  `merge/verify/close` (gh) → monitor → redirect (`agent:auto` stall-switch) → learn.
- Payoff: this is what feeds the feedback loop LIVE data (keepalive run + PR outcome joined by PR/run_id).

**Build before cron-live (the gate) — ALL DONE + selftested 2026-06-14:**
1. ✅ `dispatcher.delegate_remote(agent, target)` — applies `agent:X` to drive the remote keepalive (mode=remote, records decision).
2. ✅ `router.select_remote_agent(task_type, cap, learned)` — route-table + learned-weights + capacity, restricted to KEEPALIVE_AGENTS.
3. ✅ `outcomes.ingest_outcomes()` + `feedback.runs_needing_outcome()` — reads keepalive PR state → records merged/abandoned outcomes (durability pending) → live data for the learner.
4. ✅ rails: `dispatcher._remote_skip_reason()` skips `agents:paused` / already-owned PRs (cooperates with the delegation policy).

5. ✅ **tick-wiring** (`tick.py` + `orchestrate.sh --active`): the autonomous tick now uses the REMOTE
   model — for each backlog item, `select_remote_agent` (reserve-aware) → `delegate_remote` (apply
   `agent:X`) → keepalive runs it → `ingest_outcomes`. Heartbeat written so legacy lanes yield. SHADOW
   (default) is dry-run. Selftested; 14/14 suite green.

**Sequencing (owner directive 2026-06-14): build gates ✅ → test phase ✅ (Renovate PR #2384: orchestrator
chose cursor → keepalive ran it → cursor implemented the preset; validated end-to-end, cleaned up) →
tick wired ✅ → CRON ACTIVATED ✅ (2026-06-15, owner go).**

### Cron activation (LIVE 2026-06-15)
- launchd `com.stranske.orchestrator`, hourly at :40, runs `orchestrate.sh --active`.
- **Runs a LOCAL EXEC-MIRROR** `~/.codex/orchestrator-mirror/` (code + small registry JSONs), NOT the Dropbox canonical —
  launchd can't read CloudStorage (TCC/EPERM; a symlink doesn't bypass it). Refresh after any code edit
  with `~/.codex/bin/orch-sync-mirror.sh` (run from a Dropbox-capable context). State (feedback DB/repos/
  worktrees) is shared at `~/.codex/orchestrator` regardless of which copy runs — no divergence.
- **Two launchd bugs caught + fixed during supervised activation:** (1) gh token is in the macOS keyring
  (unreadable by launchd) → `orchestrate.sh` now exports `GH_TOKEN` from `~/.codex/credentials/gh_cli_token`
  + a preflight aborts `--active` if gh is unauthenticated (no acting on stale/blind state); (2)
  dispatcher returned `"skipped"` while tick read `"skip"` → unified to `"skip"`.
- Verified: kickstart tick = 0 net delegations (rails correctly skip already-assigned `agent:*` items),
  no feedback pollution, preflight guards. Kill switch: `launchctl unload ~/Library/LaunchAgents/com.stranske.orchestrator.plist`.
- **Follow-up (not blocking):** the heartbeat yields BOTH legacy lanes for 15m/tick, but the orchestrator
  tick only does opener-delegation — so the closer loses ~15m/hr. Hourly cadence keeps that modest
  (~45m/hr closer time). A lane-aware yield (orchestrator heartbeat yields only the opener) would remove
  the closer-starvation; revisit when the orchestrator gains closer-terminal-action capability.

## Orchestrator test issues (use these to exercise label-delegation + feedback before cron-live)
Real-but-low-stakes work to drive through the orchestrator→keepalive→feedback path:
- [x] **Renovate (Workflows-self)** — `renovate.json` merged (#2384); Dependabot removed from Workflows.
      Phase 1 of the FLEET migration shipped as **PR #2386**: extracted the inline rules into a shared
      `renovate-presets/fleet.json` (single source of truth) + Workflows extends it. Remaining fleet phases:
      - [x] **P2 fleet-distribution** (the activation step — adding `renovate.json` to `sync-manifest.yml`
            auto-pushes to all 13 consumers on the 05:00 `maint-68` cron): thin consumer-template
            `renovate.json` (extends the preset) + manifest entry (`create_only`, satisfies health-70) +
            retarget `sync_dependabot_campaign.js` to also watch `renovate[bot]` + re-seed campaign sync-hash
            (the `maint-82` contract test). Implemented earlier via Workflows PR #2394 with bot-agnostic
            cleanup in #2412; PR #2494 adds explicit contract-test coverage for the consumer Renovate
            `create_only` and maint-82 Renovate-tracking invariants.
      - [ ] **P3 per-consumer cutover** — remove each consumer's `dependabot.yml` (manifest can't delete it)
            in an atomic swap, canary → fleet, monitored across one dependency cycle.
      - [ ] **P4 cleanup** — retire `maint-dependabot-{auto-label,auto-lock,weekly-sweep}.yml` once dual-run proven.
- [x] **release-please** — DONE via PR #2408 (2026-06-15), the first genuine end-to-end orchestrator-driven
      test (Renovate was hand-driven; this one wasn't). orchestrate-skill flow: I assessed capacity + designed
      the spec + authored issue #2405, then delegated to **codex** (local worktree, reserve-aware) which
      implemented manifest-mode release-please (maint-61-release-please.yml + config + manifest seeded 1.1.2 +
      WORKFLOWS app-token + all 4 convention surfaces) and opened the PR; I integration-checked vs spec →
      gate-green → merged. No redirect needed.
- [x] **docs-drift fix-agent, first increment.** Workflows PR #2493 adds
      `scripts/docs_drift_fix_agent.py`, a read-only-by-default CLI that composes deterministic docs-drift
      checks plus optional `docs-drift-scan.json` into bounded repair prompts, agent-ready issue bodies,
      and PR plans. It also tightens scanner false-positive handling for consumer `.github/workflows/...`
      destination paths and fixes stale workflow-naming test path references. The monthly audit still seeds
      work; this first increment supplies the standalone fix-agent surface rather than auto-editing docs.
- [ ] (plus the repo-review queue across the consumer repos; 2026-06-22 processed the
      `stranske/Travel-Plan-Permission` high-priority deeper-review hold as `skip` for new product issues
      after focused evidence review and 49 passing contract/orchestration tests; a post-fix Gemini rerun
      returned strict JSON and surfaced four separate script-maintenance candidates; evidence recorded in
      `experiments/repo_review_deeper_reviews/travel-plan-permission-2026-06-22.md`. Also processed the
      `stranske/trip-planner` high-priority deeper-review hold as `skip` for new product/readiness issues
      after planner-turn, TPP cross-repo, workspace/runtime, and frontend map verification; evidence
      recorded in `experiments/repo_review_deeper_reviews/trip-planner-2026-06-22.md`. Also processed the
      `stranske/Counter_Risk` normal-priority deeper-review hold as `skip` for new product/data-integrity
      issues after audit-fix implementation review, Runner.xlsm package inspection, and 236 focused passing
      tests across GUI/Runner, writers, compute, MOSERS output structure, and non-release bundle checks;
      evidence recorded in
      `experiments/repo_review_deeper_reviews/counter-risk-2026-06-22.md`. Also processed the
      `stranske/Inv-Man-Intake` normal-priority `revise|deeper-review` hold as `skip` for new
      product/workflow-readiness issues after comparing the empty product issue set against the v1 design,
      closed repo-review work, contract docs, readiness caveats, and 301 focused passing tests plus the
      synthetic throughput readiness command; evidence recorded in
      `experiments/repo_review_deeper_reviews/inv-man-intake-2026-06-22.md`. Also processed the
      `stranske/Portable-Alpha-Extension-Model` normal-priority deeper-review hold: LLM/reference-pack
      implementation evidence is present and 170 focused offline tests passed, but an order-dependent
      LLM import/config-patch test isolation regression was uploaded as
      `stranske/Portable-Alpha-Extension-Model#2016`; evidence recorded in
      `experiments/repo_review_deeper_reviews/portable-alpha-extension-model-2026-06-22.md`. Also processed
      the `stranske/Trend_Model_Project` normal-priority deeper-review hold: Monte Carlo mixture-mode
      determinism, CASH gating, canonical cost schema loading, and CLI surfaces have executable evidence and
      95 focused tests passed, but a lognormal transaction-cost basis-point mean semantics mismatch was
      uploaded as `stranske/Trend_Model_Project#5634`; evidence recorded in
      `experiments/repo_review_deeper_reviews/trend-model-project-2026-06-22.md`)

## CodeRabbit (replacing Copilot Code Review)
App install is owner-side (OAuth). Repo-side config (`.coderabbit.yaml`) prepared; PR it into Workflows +
the consumer template once the app is installed org-wide. Drop Bugbot at trial end; keep Codex as the free
cross-family second opinion.

## Working cadence
The data-gated items (A) wait on accumulation — don't force them. Near-term: run experiments
opportunistically to grow the dataset, keep `data/feedback-snapshot.json` fresh, and revisit (A) when the
numbers can actually support a decision. (B) unblocks as LangSmith/cron access is wired. (C) can proceed
anytime it's worth the tokens. Live session task IDs #16/#17 map onto B and A/C respectively.

## Consumer-Sync Artifact Ingestion Bridge (2026-07-18)
- [x] Ingest the exact successful Workflows artifact idempotently with run-attempt, handoff, archive,
      registered-repository, default-branch, content-hash, and no-side-effect validation.
- [x] Record a bounded human-on-exception evidence phase locally (one artifact, five consumers,
      expiry 2026-07-25) while retaining `write_authority=false` and `promotion_allowed=false`.
- [x] Wire the active-only daily cadence behind the GitHub core gate, with atomic report/state artifacts,
      a concurrency lock, exception retry/backoff, and automatic return to shadow evidence after expiry.
