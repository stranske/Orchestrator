# Orchestrator

A personal-scale multi-agent fleet controller. It coordinates subscription CLI coding agents
(Claude Code, OpenAI codex, Cursor, Gemini via Antigravity `agy`, Mistral `vibe`, aider) across
~11 GitHub repos, routes work by **learned** per-agent×task-type weights under **expiring
subscription-quota windows**, runs continuous A/B/C experiments on real tasks, and feeds every
outcome back into a local SQLite "Brain." The 2026-07 field survey (six independent research
briefs under `Code/Audits/Orchestrator/2026-07-03-research/`) found no published equivalent of the
core combination: learned cross-vendor routing + drain-before-refresh quota economics + live
production duels.

> **Before changing anything, read [`CLAUDE.md`](CLAUDE.md).** This project's dominant failure mode
> is not bugs — it is **built-and-forgotten features**. Between 2026-07-03 and 2026-07-08, *six*
> fully-built subsystems were found dormant and activated (followup evaluation, adversarial gate,
> range-lane slot, ship-gate, redirect-corpus intake, and more). The dedup-before-develop check in
> CLAUDE.md exists to stop that recurring. The historical dormancy scan is
> `Code/Audits/Orchestrator/2026-07-08-dormancy-rescan.md`; current activation truth is generated
> from the local capability ledger with `python3 capabilities.py inventory`.

## How it runs (execution topology)

```
CANONICAL (edit here):  ~/Library/CloudStorage/Dropbox/Learning/Code/Orchestrator/
   └─ orch-sync-mirror.sh  (RUN AFTER EVERY EDIT) ──►  ~/.codex/orchestrator-mirror/  (exec mirror)
          why: launchd/cron get EPERM on CloudStorage paths; the schedule runs the mirror copy.
LAUNCHD:   com.stranske.orchestrator — hourly at :40 → mirror/orchestrate.sh --active
STATE:     ~/.codex/orchestrator/   (Brain DB feedback/orchestrator.db, repos/, worktrees/,
                                      capabilities.json, offloads/, agent-runtime/, experiments/,
                                      .last-*/.fail-* stamps)
HANDOFF:   ~/.codex/handoff/         (heartbeat orchestrator.json — legacy lanes yield to it;
                                      capacity.json, backlog.json, tick log orchestrator-cron.log)
```

- **Shadow vs active.** `./orchestrate.sh` (no args) = SHADOW: capacity + discovery + a dry-run
  plan, prints what it *would* do, writes no heartbeat and dispatches nothing — safe to run
  anytime alongside the live fleet. `--active` (launchd only) claims targets, writes the heartbeat,
  and dispatches.
- **Editing safely.** Edit the canonical Dropbox copy, run `orch-sync-mirror.sh`, and confirm the
  mirror matches. A concurrent fleet tick writes only to worktrees and state — never to this
  canonical tree — so canonical edits are yours alone, but always re-sync so the schedule sees them.
- **Every module has a `--selftest`.** Run it after editing that module; it is the project's test
  suite (there is no separate pytest tree). `python3 <module>.py --selftest`.
- **`python3 verify.py` is the whole verdict.** Real pytest plus every module selftest plus the five
  capability gates, judged on the COUNTS rather than exit codes, against a recorded floor in
  `.verify-floor.json`. It also bounds SKIPPING: a check needing something only a running instance
  has (the populated capability ledger, an installed agent CLI, `~/.codex/skills`) skips with the
  missing thing named — see `env_prereq.py` — and the floor file caps how many such skips are
  allowed, so quietly checking less is a red. Every skip and its reason is printed, so a green run
  always states what it did not check. On a machine with all prerequisites nothing skips at all.
- **The remote Gate checks three of four python-ci legs, and says so.** `pr-00-gate.yml` calls the
  fleet's shared Python CI. Ruff lint, Black format, coverage and the pytest matrix are ON and
  green; `typecheck-mypy` is OFF, annotated at the single place the toggles are computed with its
  BLOCKING and its DRAINABLE quantity — 604 mypy errors, drainable 0 per PR. `coverage` was the
  second OFF leg until 2026-08-23: its one startup error was not drainable from inside this repo at
  all, and it cleared when the named UPSTREAM drain landed (`stranske/Workflows#3202` made
  `--cov-config` conditional on the file existing). The annotation records how it drained rather
  than being deleted. `ruff.toml` pins the rule set so the Gate and Autofix apply the same one;
  before that they disagreed and Autofix rewrote the tree on every Gate failure. Recorded baseline, measurement commands and per-rule drains:
  `docs/CI_LINT_BASELINE.md`, regenerated with `python3 scripts/ci_lint_baseline.py`.
- **Activation is evidence-backed.** `features.py` describes reusable code maturity;
  `capabilities.py` is the activation authority. An `active` declaration must prove its matcher,
  invocation, artifact consumer, outcome sink, expiry, kill switch, and rollback. Each active tick
  validates the ledger and writes `capability-validation.json` plus `capability-inventory.md` under
  the local state directory.

## Important functionality (what actually runs, grouped by job)

Verdicts (ACTIVE / gated / CLI-only) reflect the 2026-07-08 dormancy re-scan; re-run that scan to
refresh. "Gated" = code is live but a default-OFF `ORCH_*` flag holds it back — an intentional
safety switch, not dead code.

### The tick (hourly `orchestrate.sh --active`)
1. **capacity.py** — per-seat budget/policy across 5h + weekly quota windows (steady/reserve/drain),
   plus two **dispatchability gates that run before any budget math**: a seat sheds if its CLI
   cannot authenticate (`unavailable_auth_failed`) or if a pinned model in any tier is not in the
   CLI's own catalog (`unavailable_model_unresolved`). Both fail SAFE — an unrunnable check is
   UNKNOWN and never sheds a working seat. Added 2026-08-08 after a rotted gemini model pin made
   every dispatch exit 1 while this file still reported `ok` with full headroom.
2. **backlog.py** — discovers actionable issues/PRs across the fleet from GitHub labels.
3. **router.py** — ranks agents per task: capacity tier → learned route weights → **continuous
   drain urgency** (unused expiring quota reads cheaper). ε-greedy exploration is default; a
   Thompson-sampling path exists, gated behind `ORCH_EXPLORATION_MODE`. That gate is no longer
   "pending a review": `exploration_review` was run on 2026-08-22 and returned
   `keep_epsilon_greedy` / `epsilon_still_preferred` — its evidence gates ARE met, and Thompson-hybrid
   improves simulated challenger quality and direct exploration outcomes not *both*. So ε-greedy is
   kept on merit, and `capability_recurrence_check._check_thompson` now runs the review and reports
   its recommendation, so the switch re-raises itself if that verdict ever flips.
4. **tick.py → dispatcher.py** — claims a target (claims.py: atomic, live-pid-guarded, reap-grace +
   reap-mutex), spawns the agent in an isolated worktree with a kill-proof done-marker, releases on
   exit. High-stakes closer items pass through the **adversarial.py** refute-mode veto panel.
5. **exp_abcd.py followup** — collects + cross-evaluates finished A/B/C experiments (nothing used to;
   this drained a 249-experiment backlog in July 2026), records objective anchors, and resumes the
   `synthesis_promotion.py` lifecycle. Useful syntheses must complete, pass scope/secret/local
   deliberate-break/runtime-AC/repo gates, and compile one local canonical delivery candidate before
   the daily ship-gate stamps. The harness never publishes or auto-merges. Arm/member/profile identity is exact, so the same provider can occupy
   multiple model or strategy arms without artifact collisions or evaluator ambiguity.
6. **range_lane_rollout.py** — daily slot that exercises the specialized lanes (testgen/epic/codemod/
   cross_repo/runtime_ac); PREVIEW by default, live dispatch behind `ORCH_RANGE_LANE_ROLLOUT`.
7. **runtime_ac_flow_monitor.py** — daily read-only monitor over structured gate events. Its
   denominator is required active closer gates, never generic closer traffic; the legacy cron log is
   archival-only. Runtime AC is a hard opt-in machine gate for labeled/spec-backed closer work:
   active progression requires a target-exact spec, `ORCH_RUN_RUNTIME_AC=1`, and `PASS`. The separate
   adversarial reviewer panel remains advisory.
8. **issue_readiness.py** — daily cadence step that decides which open issues the fleet may work,
   removing the owner from the ready-label queue. Four verdicts (auto_ready / owner_review /
   needs_specification / not_opener_work); only risk-labelled AND actionable issues reach the owner,
   via a non-blocking `feedback.owner_questions` entry that auto-ratifies to *proceed* at expiry so
   nothing can latch. Read-only assessment by default; label writes are gated behind
   `ORCH_ISSUE_AUTOREADY`. The report states its own attention cost each run (measured 2026-08-18:
   well under the weekly attention budget (LOCAL_POLICY.md)), so a drift in the issue mix surfaces instead of
   quietly growing.
9. **redirect_apply.py** — the consumer `redirect_plan.apply_plan` never had. Daily and local (no
   gh). `--link-outcomes` is ALWAYS ON and mutates nothing: for every redirect role run whose
   stamped dispatch reached a terminal outcome it appends the corpus outcome link, which is what
   makes `synced_role_outcomes` climb with no owner in the loop (the manual `link-outcome` design
   produced 5 links in ~2 months). `--apply` is DEFAULT OFF behind `ORCH_REDIRECT_APPLY_BOOTSTRAP`;
   armed, it applies at most one *authorised* plan per day, only on an ALREADY-DEAD lane (so no kill
   ever runs and the apply reduces to release-claim + delegate, which the rails already do to a dead
   stalled lane), never over a foreign claim, never on an un-stamped plan, and it disarms itself the
   moment the Stage-2 deficits close. It exists because that gate is a structural deadlock:
   `synced_role_outcomes` counts only applied advice, so the gate authorising apply required ten
   applied outcomes. The machine-checkable arming condition lives in
   `capability_recurrence_check.SWITCH_ON_CRITERIA`, not in anyone's judgement.
10. **research_scheduler.py + research_subjects.py** — opportunistic acquisition uses only capacity
   left after production/range reservation. A durable target/task/spec/base/arm fingerprint blocks
   active, cooldown, per-subject, and global unevaluated duplicates; repeated runs on one subject
   sum to one effective learner observation.

### The Brain (feedback.py, SQLite at ~/.codex/orchestrator/feedback/orchestrator.db)
- **Capability attribution at dispatch** — a run is tagged with the infrastructure capabilities it
  actually exercises (`dispatcher._exercised_capability_ids`: the gemini adapter path for
  `agy-runtime-isolation`, an actual Thompson challenger choice for `thompson-hybrid-routing`;
  range-lane assignments tag themselves before dispatch; a role run tags `offload` when a
  `backend_run_id` shows one produced its proposal). Each condition is the same one the capability's
  own heartbeat fires on — never an entrypoint-string guess, which
  `capability_outcome_bridge` refuses by design. `capability_outcome_bridge` then turns those edges
  into ledger outcome heartbeats, so a capability records not just that it RAN but how the work
  turned out. Its `run_tagged` resolver was dead code until 2026-08-21 (it read a column that does
  not exist), and its edge repairs now run before the heartbeat pass rather than a cycle behind it.
- **The tick consults the front door, and records whether a capability helped**
  (`capability_propensity.py tick-evidence`, every tick, below `ORCH-ANCHOR: heartbeat-export` and
  below the four steps it grades). `capability_advisor.advise()` and the `invocation`/`outcome`
  recording edges both existed and had no production caller, which is why the propensity report
  printed PRIOR-ONLY on every run: a measurement with no producer. For the four capabilities bound
  to the `tick` surface it now consults the advisor and, for the ones
  `capabilities.is_observer()` confirms, records an **output-change verdict**: an observer HELPED
  when its report's finding set changed since its own previous run (a defect newly reported, a
  regression flagged, a switch verdict that moved, a finding resolved) and did NOT help when it
  re-emitted an identical set — silence is not usefulness. Never a delivery verdict, which a report
  can never earn. Two independent bounds stop 24 runs/day becoming 96 unearned data points: the
  experiment id is scoped to the UTC day (so the ledger idempotency keys admit at most one verdict
  per capability per day), and a verdict additionally requires that capability's own cadence
  artifact to have been regenerated, which bounds the graded rate to ~1.3/day. The finding
  projection keeps identity and verdict fields only — `overdue`'s `silent_days` rises daily on its
  own, and hashing a row whole would score the monitor "useful" on every run it will ever make.
  Kill switch: `ORCH_TICK_EVIDENCE_DISABLED=1`, or `ORCH_DISABLE_STEPS=tick-capability-evidence`.
- **A usefulness verdict carries its PROVENANCE, and the posterior is weighted by it**
  (`capability_propensity.VERDICT_PROVENANCE`, 2026-08-23). The first real corpus was 12 verdicts,
  11 useful — every one **self-assessed by the agent that chose to use the capability**, from three
  audits by the same model under near-identical instructions. Selection bias on top of correlated
  arms, which `CLAUDE.md` §2 forbids counting as independent evidence. So a verdict is classified
  `outcome_corroborated` / `defect_found` (1.0, and **refused** without `corroboration` naming the
  outcome), `machine_observed` (0.6 — the tick's code-computed finding-set diff) or `self_reported`
  (0.25, the honest default for any unlabelled row); verdicts are grouped by
  `(judge arm, provenance)` and each group totals 1.0 however many it holds, reusing the same
  reciprocal `relearn_quality` applies to research arms via
  `research_subjects.reciprocal_evidence_weights`. Down-weighted, never banned — self-assessment is
  the only signal most capabilities have. Every surface states the mix: `propensity()` and `rank()`
  hand the caller the provenance mix, independent-arm count and self-reported share beside the
  number, and the report headline now reads *12 verdicts, 12 self_reported, 0 outcome-derived*, with
  the three capabilities that had shown 0.800 showing 0.556.
- **The repair channel — the loop's third action** (`capability_propensity.propose_repair`,
  `record_repair`, 2026-08-23). Promote and demote were the only two actions, so the loop could not
  represent *"this capability is worth having and is broken"* — and demoting such a capability
  silences the thing that should be fixed. Live case: `repo-playbook` at one useful and one
  not-useful verdict, where the audit documented that its useful content is gated behind
  `task_type: implement/testgen/mechanical`, so a `review` consult gets 308 characters with one
  factually wrong clause. Fed by `not_useful` verdicts **with their evidence carried forward** (a
  proposal without the words is a flag) and by the declines whose kind indicates a defect —
  `repairable` is a second property of `DECLINE_KINDS`, declared once beside `demotable`:
  `wrong_match` and `precondition_unmet` yes, `no_landing_zone` explicitly no (nobody's fault, the
  capability is working), `scope_too_small` no (its fix *is* the demotion path). `precondition_unmet`
  is the pair that proves the two properties are independent: not demotable, so before this it had no
  action at all. **Report-only, never applied, and it queues nothing for anyone** — 13 rows in a
  report the cadence step already writes, 0 minutes/week. The drain is `record_repair` (an action,
  not the calendar), and every proposal prints its measuring, blocking and drainable counts together.
- **A defect found is recordable, and the finder may be a capability OR a surface**
  (`capability_propensity.record_find`, `binding_quality`, 2026-08-23). Instrumented work found seven
  defects in this system's own code; two were attributable to a capability and recorded, and the
  other five were found by the **process** — an audit noticing that a suppressed surface still
  offered capabilities, an agent finding a branch of this module that recorded nothing — so they had
  no capability to attribute to and became PRs and prose. A capability-attributed find now feeds that
  capability's usefulness at `defect_found` provenance (an outcome, not an opinion, with the artifact
  as its corroboration); a surface-attributed find feeds **binding quality**, which had nowhere to
  live. No new store and no new event type: a find rides a `match` event tagged
  `source=capability_find` with a `find:` ref rather than an `advice:` one, so `experiments()` /
  `usefulness()` / `propensity()` cannot see it — structurally, not by convention. `defect` and
  `artifact` are both required (a claimed find with no artifact is worth nothing), and the
  correlated-arm discount is the real guard: ten artifact-backed finds from one judge arm are still
  one observation, so volume cannot inflate a capability and only an independent arm moves it.
- **`gate_blocks_execution`** — an opt-in capability declaration for the case where a switch blocks
  the code path that would produce an outcome (Thompson never chooses while the mode is
  epsilon-greedy; range-lane's heartbeats sit on the live-apply branch; issue-readiness's label
  write is gated). Those read `deliberately_gated` instead of accruing a permanent "fix outcome
  linkage" instruction they cannot act on. Deliberately opt-in: 16 of 39 capabilities carry a
  `gate_reason`, so reordering the checks for everyone would have hidden real gaps.
- **runs / execution_attempts / outcomes / costs / route_weights / evaluations_v2** — decisions,
  causally scoped worker/evaluator/verifier attempts, exact experiment identities, and results.
  Generic trace models and legacy `runs.model` tags never resolve worker identity; unresolved rows
  remain usable for agent-level learning without contaminating exact-model evidence.
- **Structured runtime-AC gate events** — `runtime_ac_gate.py` records required/planned/missing,
  skipped/error, executed verdict, exact target/spec/hash, closer/verifier joins, and downstream
  outcome linkage in the existing `completion_events` plane. Range-lane specs are installed only by
  `runtime_ac_gate.py --materialize-range-spec <artifact> --target owner/repo#N`; cross-target or
  invalid artifacts terminate as non-installed evidence instead of becoming gate inputs.
- **Verified synthesis delivery lifecycle** — each evaluated experiment can persist
  `synthesis-promotion.json` through `evaluated → synth_running → synth_complete → synth_verified →
  candidate_ready → delegated_or_pr → merged → durable`, with discarded/reverted work retired.
  These subordinate phases derive canonical capability states; they do not create a second lifecycle
  enum. `exp_abcd.py followup` reconciles/resumes them exactly once. Candidate bodies use the canonical
  agent issue sections and preserve experiment/arm/member/evaluator/profile/synthesis/capacity and
  accepted influence lineage. An explicit external delivery link hands authority to Workflows
  auto-pilot/Keepalive; no `gh`, push, publication, or merge path exists in the promotion module.
- **relearn_report.py** (weekly) re-estimates versioned route weights: Beta-Binomial posteriors
  with recency decay, cost/effort imputation (missing telemetry never reads as free — the cost
  plane is repaired), and **Bradley-Terry warm-starts** blended from the A/B/C duel data.
- **judge_reliability.py** — leave-one-out consensus weights per judge (de-saturated so real error
  spread maps to real weight spread); **human_calibration.py** + **objective_anchor.py** supply
  machine ground-truth anchors (no owner code-review required — see CLAUDE.md).
- **Two-tier outcomes**: signal-killed/infra failures are classified `transient_infra` and excluded
  from learning, so environment noise never trains as agent incapability.
- **Independent-subject weighting**: explicitly linked research repetitions are down-weighted by
  `(agent, subject_family)` before posterior updates; legacy rows keep agent-level value without
  receiving invented subject provenance.
- **Execution profiles**: `execution_profiles.py` keeps provider capacity pools separate from
  model/profile identity (`codex-5.6-sol`, `codex-5.6-terra`, `codex-5.6-luna`). Profile routing
  is fail-closed, capacity-aware, and shadow-learns per-agent/provider priors before any live
  promotion. `feedback.py completion-events --jsonl` exports the canonical phase envelope used by
  downstream learning; it never fabricates historical observations.
- **Sol/Terra/Luna trial bridge**: `model_profile_trial_bridge.py preflight|prepare|collect-remote|ingest|qualify`
  binds the frozen trial packet to replayable per-profile request IDs, one live shared-capacity
  snapshot, external quarantine artifacts, and strict requested/selected/reported identity while
  preserving provider-resolved identity as null. It never dispatches a remote workflow or writes
  the Brain. The dedicated Workflows runner is pinned and read-only; normal workspace-write Keepalive
  rejects trial profiles and is not a substitute. `collect-remote` binds authenticated GitHub run,
  workflow, source, artifact, archive-digest, and embedded JSON identity before ingest. The 2026-07-10
  serial Terra/Luna/Sol canary passed those checks and was correctly quarantined because subscription
  execution did not expose independent provider-resolved identity. `qualify --manifest ... --envelope
  ... --results ... --quarantine ...` replays the sealed source manifests, request/envelope hashes,
  identity artifacts, and exact quarantine result. Its default output is
  `transport-qualification.json` beside the supplied quarantine; use `--output` for staging. A passing
  qualification releases only the pinned transport and CLI-reported profile contract for future
  no-learning instrumentation. Provider-resolved identity remains null and unclaimed; the canary remains
  ineligible for Brain ingestion, quality-weight updates, and promotion. Provider-attested finalization
  is unchanged. Instrumentation completion events are excluded from Pattern Miner input. Run
  `python3 model_profile_trial_bridge.py selftest` before preparing a canary.
- **Pattern-to-capability compiler**: `pattern_miner.py` consumes those seven-phase completion
  envelopes and emits candidate-only capability IR. It is intentionally non-dispatching: inspect
  `~/.codex/orchestrator/pattern-miner-status.json`, `pattern-miner-inventory.json`, and
  `pattern-miner-state.json` after the daily cadence (or run `python3 pattern_miner.py status`
  and `inventory`). A useful first check-in is after 7 daily runs or 20 accepted episodes, whichever
  comes first; review candidate evidence, counterexamples, and expiry before promoting anything.
  Deterministic candidates can then be dry-compiled by `capability_compiler.py`; its reference rail
  proves lifecycle consumption without granting an apply or arbitrary-shell path.
  The same existing `capability:reference-sync-hygiene-test-gate` now accepts typed
  `workflows.consumer-sync-plan/v1` evidence through `consumer_sync_shadow.py`. The classifier
  emits only read-only create/update/remove/skip/no-change proposals, and
  `runner_effect_bridge.py` validates provider-neutral runner effect evidence before recording
  idempotent outcomes or counterexamples in the existing capability ledger. Run
  `python3 consumer_sync_shadow.py dashboard` to see distinct effects, harms, reduced-supervision
  evidence, expiry/kill-switch state, and explicit promotion blockers. The rail remains shadow-only;
  no consumer writes, dispatch, merge, or promotion authority is exposed.
  `consumer_sync_artifact_ingest.py preview` validates the latest successful producer artifact and
  the consumers' default-branch content without writing state. It reads each consumer through ONE
  recursive git-tree call plus per-blob fetches memoised on the content-addressed blob id (cached
  in the ingest state file across runs) — never a whole-repo archive, which made a 122MB consumer
  unreadable and cost ~171MB/day of transfer for ~4MB of signal. The same tree yields a read-only
  `hygiene` section per repo in the ingest report, naming committed dependency/cache directories
  with evidence-based dispositions (`untrack` only where no ecosystem-matched manifest vouches for
  the directory; `review_vendored`/`review_owner` otherwise). Findings escalate through existing
  surfaces only: machine-decidable ones become a digest line in `periodic_report.py`
  (`consumer_sync_hygiene`), and material judgment calls become a single auto-expiring
  `feedback.record_owner_question` whose default changes nothing — measured at ~0.17 questions per
  month, so no backlog is possible. The active-only daily cadence uses
  `ingest` for at most one artifact and five registered consumers, records only local evidence, and
  runs a self-expiring human-on-exception phase through 2026-07-25. It has no consumer or GitHub
  write path; exceptions fail the cadence and remain visible in the local report/state.
  For a concise check-in, run `python3 periodic_report.py --json --window-days 7` and inspect the
  `model_profile_trial`, `model_profile_transport_qualification`, `role_activation`, `pattern_miner`,
  and `dataset` sections alongside the
  generated status/inventory artifacts.
- Candidate extensions are also available through `capability_compiler.py`: skill packages remain
  under a shadow candidate directory until separately promoted, while evidence contracts report
  independent-subject counts, named capture hooks, influence measures, expiry, and rollback.
  Judgment-only candidates can compile into strict, provider/profile-agnostic roles with bounded
  selectors, capacity policy, prompt hashes, expiry, kill switches, and predecessor rollback.
  Generated roles register only in the process-local shadow registry in `roles.py`; they cannot
  alter the static production roster, dispatch work, or mutate baseline behavior. Accepted and
  rejected proposals both join the existing role influence/outcome lineage.
  Repo-specific durable candidates can compile into low-risk managed playbook canaries only after
  exact current path/symbol checks, independent durable evidence, negative examples, dedupe, and
  expiry/rollback validation. `repo_knowledge.py` preserves user-authored `AGENTS.md` content,
  reports managed blocks as `absent`, `stale`, `mismatched`, or `current`, and can merge the hashed
  rule into an optional Workflows capability bundle without replacing unrelated bundle content.
  Matches, injections, acceptances, counterfactual rejections, and downstream outcomes reuse the
  existing completion-event and capability-influence planes.

### Telemetry & recovery (ledger_reconcile.py, daily)
- Harvests real cost (agent-reported `total_cost_usd`), latency, tokens, done-markers, **resume
  tokens** (CLI session IDs → `feedback.py resume-hint <run_id>`), and **owner questions** from run
  logs. Backfills killed completions from markers.

### Human touchpoints (all non-blocking — see CLAUDE.md attention budget)
- **Owner questions** (interrupt-as-data): agents record a product-level question + the default they
  proceed on; unanswered questions auto-ratify their default at expiry (no possible backlog); answers
  steer future prompts. `feedback.py questions` / `answer <id> "<text>"`, or via the MCP server.
- **periodic_report.py / observability_dashboard.py** (weekly) — FYI dashboards, not action queues.

### Interfaces
- **agent_auth_check.py** (CLI-only) — "can every seat actually authenticate and dispatch, right
  now?" Checks the way the FLEET does: it sources each agent's credential file and disables the
  interactive credential store, because a bare `cursor-agent status` in your shell answers about a
  stored session the headless lane never reads (it reports "logged in" with a dead API key and
  "Not logged in" with a live one). Prints the per-seat refresh hint; never prints secret values.
  `--json` for machines; exit 1 only when a seat is *definitively* broken, never on UNKNOWN.
- **mcp_server.py** — exposes the fleet to any MCP client (registered user-scope as `orchestrator`):
  capacity, fleet summary, route weights, capability advice, owner-question list/answer, resume
  hints. Read-only plus three bounded actions — the two owner-question ones and
  `capability_decline`, which records that an OFFERED capability was rejected, why, and of which
  `kind`. No dispatch through this door. A decline is append-only evidence and never a verdict: it
  cannot reach the usefulness posterior, because the capability did not run. Only the kinds that
  indict a binding (`wrong_match`, `scope_too_small`) can propose a demotion — a correct match with
  nowhere to land is recorded and never counted against the capability. `capability_advice` also
  takes `repo_path`, a checkout of the repository under discussion, which lets a capability's
  declared precondition (`applies_to: self | audited_repo | both`, or a named repo fact such as
  "does this repo have an observable surface at all") actually be EVALUATED. A failed precondition
  is explained and never enforced: the offer keeps its place in the list.
- **Cadence resilience** — failing daily/weekly steps back off (`.fail-<step>` stamps,
  `ORCH_CADENCE_RETRY_HOURS`) and ALERT after N consecutive failures instead of retrying hourly.
- **Per-step kill switch** — `ORCH_DISABLE_STEPS="feature-scan,redirect-sweep"` (comma or space
  separated) skips named steps. One mechanism instead of a flag per capability. It ANNOUNCES every
  skip (a silent disable is the latched-gate pattern), touches NO stamp (re-enabling makes the step
  immediately due — it defers work, it never fakes completion), and WARNS on an unknown key so a
  typo cannot leave a step running while you believe it is off. Unset/empty disables nothing.
- **Other kill switches** — `ORCH_OFFLOAD_DISABLED=1` refuses at the top of `dispatcher.offload`
  before any spend; `ORCH_REPO_PLAYBOOK=0` stops playbook injection into delegation prompts on the
  next dispatch without editing the registry; `ORCH_TICK_EVIDENCE_DISABLED=1` makes the tick's
  capability consult/verdict step inert from any caller (no consult, no ledger event, no state
  file), which is the module-side twin of `ORCH_DISABLE_STEPS=tick-capability-evidence`.
- **Daily compiler cadence** — the active tick atomically publishes completion-event JSONL plus
  pattern-miner status/inventory artifacts. Empty output is a healthy “no eligible history yet”
  result, not a reason to seed synthetic data.

## Governing docs
`ORCHESTRATOR.md` (role/philosophy) · `ARCHITECTURE.md` (data flow) · `FEEDBACK_LOOP.md` (learning
loop) · `EVAL_AND_TESTING.md` (selftest/gate regime) · `PLANNING.md` (roadmap) ·
`improvement_log.py` (the machine-local numbered items + status log; `IMPROVEMENT_BACKLOG.md` in the
tree is a pointer to it) · `CLAUDE.md` (agent rules) ·
**`ADDING_CAPABILITIES.md`** (the enforced procedure for adding or reviving a capability, and the
nine failure modes it exists to stop).
Durable audit history: `Code/Audits/Orchestrator/`.
Remote CI: `docs/CI_LINT_BASELINE.md` (what the Gate checks, what is deferred, and what would clear each deferral).

## Adding a capability (enforced)

`capability_admission.py` is the admission gate, and unlike the other capability tooling it is
PROSPECTIVE: everything else checks the capabilities that already exist. A capability must arrive
with nine parts — a recorded dedup finding, a caller, a heartbeat on the executed path, a
recurrence fixture, an outcome path (declared consumer **and** learning sink), a kill switch, a
rollback, an expiry-or-cadence, and **a surface that can offer it**. `--preflight '<spec json>'`
answers six of them before the code is written, returning caller/heartbeat/fixture as explicit
obligations rather than skipping them.

The ninth (`findable`, 2026-08-23) exists because the first eight make a capability invocable and
observable and none of them makes it findable: 22 of 43 capabilities were bound to no surface at all,
so nothing could offer them and no amount of running could produce evidence for them. It distinguishes
`bound_nowhere` from `bound_to_unconsulted_surface` — `capability_advisor.CONSULT_SITES` declares
which surfaces a caller actually names — because the fixes differ, and it states what it does not
check (a surface invoking the entrypoint with no surface attribution needs that surface's own prompt,
which is outside this repo).

It also tracks **commitments**: a citation to a dated record that does not exist, or a deadline that
passed with no record naming its subject, fails `test_capability_admission.py`. That check exists
because a scheduled trial review fired on time, wrote nothing, and let a flag revert by timeout for
36 days while live code cited the record nobody wrote.

Enforcement binds on capabilities registered from 2026-08-21, and each requirement carries its own
date (findability from 2026-08-23); the pre-cutoff set is reported as drainable debt on every run,
with its causes and its drainable count, and does not fail the suite. Rationale and the failure modes:
`ADDING_CAPABILITIES.md`.

## Capability activation inventory

`python3 capabilities.py usage` answers the question the inventory cannot: **why** a capability is
not being used, and what would change that. It reports invocations/week, `evidence_debt` (how many
further independent durable reuses the promotion policy still wants), and one next action per
capability, rolled up into READY TO LIFT / PROMOTABLE / MEASUREMENT GAPS / WORTH FEEDING / RETIRE
CANDIDATES. The distinction that matters: a gate is usually **starved of evidence**, not awaiting a
decision — `_causal_readiness` needs ≥3 independent durable reuses, so a capability nothing invokes
can never lift no matter how often it is reviewed. `unblock()["feed"]` marks the only class worth
spending real capacity on; measurement gaps and retirement candidates are explicitly NOT fed.

`gate_readiness()` evaluates a capability's OWN `evidence_threshold` for any status (the causal
reconciler consults readiness only for `canary`). Encode countable bounds in the optional
`gate_criteria` field; name anything the causal record cannot supply under `requires`. **A gate is
never reported ready while any criterion is unevaluated** — un-encoded prose, missing observations,
and unrecognised criteria all block readiness, so silence cannot read as a pass. Layer 2 reports
readiness; lifting a gate stays a deliberate act (see the safety-switch policy in CLAUDE.md).

Do not maintain a second static list of supposedly active or gated features here. Generate the
current inventory with `python3 capabilities.py inventory` (or inspect
`~/.codex/orchestrator/capability-inventory.md` after an active tick). It distinguishes deliberate
gates, canaries, no matching work, matched-but-not-invoked seams, missing outcomes, and stale active
capabilities from ordinary code maturity.
