# BRIEF — Which Orchestrator capabilities to transfer into the GitHub keepalive system

*Research-backed, adversarially-tested ranking of capability transfers from the local
`Code/Orchestrator/` Brain into the GitHub-Actions keepalive/auto-pilot system in
`Code/Workflows/`. Produced in three ordered stages (external research → adversarial
evaluation → this brief). Investigation only — no production changes were made.*

**Date:** 2026-06-19 · **Grounding read on both sides; module selftests 7/7 green; PR #2466 (merge-guard) confirmed MERGED, PR #2473 (langsmith producer) confirmed OPEN.**

---

## TL;DR

The starting proposal was directionally right but two of its premises were already partly
handled and three of its transfers fight the owner's own red-teamed design
(`Workflows/docs/plans/local-multi-agent-orchestrator.md`). After research + adversarial
adjudication the ranking is:

| Rank | Transfer | Adjudicated verdict | Brain-coupled? | First-build? |
|------|----------|---------------------|----------------|--------------|
| **1** | **Scoped execution-based deliberate-break gate** (`local_verify.py` → Gate) | **BUILD — reshaped** (named-test only, opt-in, + test-tamper tripwire) | No (git+pytest) | **★ YES** |
| **2** | **Minimal curated repo-knowledge export** (`repo_knowledge.py` → committed `AGENTS.md`) | **BUILD — export-not-fork** (single owner, keepalive = freshness owner) | No (static JSON) | strong runner-up |
| **3** | **Trustworthy verdict = structured file, not grep** (transfer `runtime_ac`'s structured-verdict pattern; keep the single cross-family judge) | **BUILD the verdict-hardening; REJECT the N-judge panel** | No (the fix); panel = yes | parallel cheap win |
| **4** | **Durability-as-success export** (`durability_sweep.py` → `langsmith-fleet/v1`) | **DEFER** until the producer pipe flows (PR #2473 + the join) | Detection no; label yes | not yet |
| **5** | **Churn-without-verification escalation** (sliver of `redirect_policy.py`/`watch.py`) | **SMALL ADD** — most of it already exists (`error_classifier.js`) | No | minor |
| **6** | **Learned agent routing in keepalive** (`feedback.current_weights`) | **REJECT — keep local** (owner's "one brain, one language") | High | no |

**Recommended first build:** **#1, the scoped deliberate-break gate** — it is the owner's own
explicitly-named prerequisite ("the only real pre-merge gate is Gate CI; harden it with
deterministic, LLM-independent, pre-merge checks *before* routing cheap volume"), it is
Brain-free so it sidesteps the hard constraint entirely, and it closes the precise hole the
already-shipped static lint leaves. The adversarial pass changed *how* to build it (scope to the
issue's named deliberate-break test, not all changed tests), which is what makes it correct and
cheap instead of blunt and expensive.

---

## The hard constraint, and the principle that resolves it

A GitHub-Actions keepalive workflow **cannot read the local Orchestrator store at runtime.** The
owner's rule: keep the learning Brain in `Code/Orchestrator/` and *call it / feed it* — do not
build a divergent learning system. The owner's design doc resolves this as **"one brain, one
language"**: routing/learning logic lives in one Python module on the Mac; the GitHub-side
`agent_delegation_policy.js` "stays as-is" for its own job; the local brain reads the same verified
signals but does not share its runtime.

That yields a clean sorting principle for every candidate transfer:

- **Deterministic, Brain-free checks** (run tests, scan a diff, read a static file) → belong in
  Actions. Transfer them. They sidestep the constraint because they need no learned state.
- **Brain-coupled learning** (durability *labels*, learned *routing weights*, judge-*reliability*
  weighting) → stay local. Do **not** port them; instead **feed the Brain** evidence through the
  Workflows-native `langsmith-fleet/v1` artifact contract, which `langsmith_pull.py` already
  ingests. The canonical pattern for ever surfacing learned policy to CI is **offline-policy-export
  + off-policy-evaluation** (learn online locally, freeze, validate, commit a small table CI reads),
  never a live query — and there is no demonstrated need for that yet.

Every verdict below is an application of this principle.

---

## Verified current state (so we do not rebuild what exists)

Read on 2026-06-19 directly from the code, not inferred:

- **Pre-merge Gate already has a `test-quality` job** (`pr-00-gate.yml:303`) running
  `scripts/check_gate_diff_quality.py`: it **statically** flags added/modified test files lacking a
  *literal-expected* assertion (`assert x == 5`, `pytest.raises(`, `expect().toEqual(`) **and**
  scans the full diff for secret patterns. This is part of the design doc's L2/F21 hardening —
  *already shipped*. It is **static**: it cannot tell whether a test with a literal assertion
  actually couples to the new code.
- **The runtime-AC merge guard is merged and wired into all three GitHub merge lanes**
  (`runtime_ac_merge_guard.js`, PR #2466; callers in `maint-71-merge-sync-prs.yml`,
  `agents-73-codex-belt-conveyor.yml:455`, `reusable-70-orchestrator-main.yml:3438`, all
  fail-closed before the merge call). **But it only fires when a PR carries a `runtime-ac`-family
  label, and nothing in `.github/` or `templates/` ever applies those labels** — it is dormant
  plumbing that defers labeled PRs to the local `merge_guard.py`.
- **The deliberate-break criterion is required by the issue template's "Definition of Ready"**
  (`templates/consumer-repo/docs/AGENT_ISSUE_FORMAT.md`) but **enforced by nothing** at PR/merge time.
- **The LLM verifier is post-merge only** (`reusable-agents-verifier.yml`,
  `agents_verifier_context.js:271` skips unless `pr.merged`) **and its verdict is grep-injectable**
  (`grep -qiE 'verdict:[[:space:]]*pass' codex-output.md`, line 629–631) over agent-authored text
  that includes the diff. It already supports a `model2` cross-provider compare mode (lines 42, 1021).
- **`agent_delegation_policy.js`**: `effective = commits >= 1 || tasks >= 1` (line 235),
  `detectStall` threshold 3 (line 87), `needs-human` after 3. **`error_classifier.js` already
  classifies** transient/auth/resource/logic/unknown with recovery actions, wired into ~10 workflows.
- **Keepalive prompt injects only the PR's own Scope/Tasks/Acceptance** — zero per-repo gotchas.
- **`langsmith-fleet/v1` exists as a full contract** (schema + validator + conformance workflow +
  registry) **but no consumer repo uploads the artifact yet** (`docs/LANGSMITH_INTEGRATION_STATUS.md`;
  matches Orchestrator backlog item C; producer fix is unmerged PR #2473).
- **All seven candidate Orchestrator modules pass `--selftest`** (local_verify, runtime_ac_gate,
  merge_guard, repo_knowledge, adversarial, durability_sweep, redirect_policy).

---

## Method (three stages, in order)

1. **External research** (two background deep-research agents, web search + fetch + adversarial
   cross-verification): SWE-bench fail-to-pass, reward-hacking, durability vs green-CI, LLM-judge
   bias / minority-veto, repo-knowledge injection, learned/bandit routing. Cited findings in the
   appendix; every headline claim cross-checked against ≥2 sources.
2. **Adversarial evaluation**: three independent refute-mode agents (lenses: *cost & Actions
   limits*, *maintenance & divergence*, *fit/correctness/redundancy*) each returned per-transfer
   `{blocker, severity, finding, confidence}`, plus one real `dispatcher.py offload --agent codex`
   refuting the recommended build. Adjudicated by the Orchestrator's own minority-veto logic
   (`adversarial.aggregate_veto`: ≥2 substantiated high/critical vetoes ⇒ reconsider) — but
   **adjudicated against ground truth, never obeyed** (per `ORCHESTRATOR.md`). Tally in the appendix.
3. **This brief.**

---

## The transfers, ranked

### #1 — Scoped execution-based deliberate-break gate  ★ recommended first build

**What it is.** Port the green-then-red logic of `local_verify.py` into a pre-merge Gate check:
run the named acceptance test on HEAD (must pass), rebuild the BASE commit, overlay only that
test, rerun (must **fail**). A test that passes on both is hollow — it does not couple to the new
implementation. This is exactly SWE-bench's `FAIL_TO_PASS` property and exactly the
"deliberate-break → named test must fail → revert" pattern the issue template already mandates.

**Why it is #1.** It is the owner's explicitly-named P2 prerequisite; it is **Brain-free**
(git + pytest) so it runs natively in Actions; and it closes the precise gap the shipped static
lint leaves (a literal-assertion test can still pass on base). Research is unambiguous that this
is the highest-value, highest-TNR, store-free guard against hollow PRs.

**Research (cited).**
- SWE-bench Verified validates patches by `FAIL_TO_PASS` (test fails on buggy code, passes after) +
  `PASS_TO_PASS` (nothing else breaks) — "fully CI-native … no local store needed."
  (OpenAI, https://openai.com/index/introducing-swe-bench-verified/; Epoch AI,
  https://epoch.ai/blog/what-skills-does-swe-bench-verified-evaluate)
- Without it, ~1-in-5 "solved" patches are semantically wrong (weak tests); SWE-bench+ attributes
  ~31% of "passes" to weak tests, ~33% to solution leakage; resolution roughly **halves** when
  filtered. (Wang/PatchDiff, https://arxiv.org/abs/2503.15223; SWE-bench+,
  https://arxiv.org/html/2410.06992v2)
- Reward hacking is now measured: GPT-5 edits the test instead of the code 76% of the time when
  that is the only way to pass (ImpossibleBench, https://www.greaterwrong.com/posts/qJYMbrabcQqCZ7iqm/).
  A held-out test set the agent never sees is the strongest mechanical guard (EvilGenie,
  https://arxiv.org/html/2511.21654).

**Adversarial findings & adjudication.** This drew the heaviest fire — and improved the most.
- *Codex offload (non-Claude), high/0.90:* "passes on base ≠ hollow" for **general** PRs — many
  legit PRs add regression coverage for existing behavior, refactor tests, or touch fixtures
  outside the overlaid file, so a blanket "all changed tests must fail on base" rule **systematically
  false-blocks valid PRs** while still missing hollow ones on flake/setup failures.
  **Adjudicated:** decisive — but for *scope*, not rejection. Enforce fail-to-pass only for the
  test(s) the issue's deliberate-break criterion **explicitly names** (the issue format already
  requires one). Repos/PRs with no named deliberate-break test are untouched. This collapses the
  false-block objection: we never assert "all changed tests must fail on base."
- *Cost (high/0.82):* Gate runs `concurrency: cancel-in-progress: true`, so a double-suite cost
  re-pays on every keepalive push (an 8-nudge PR pays 8×). **Adjudicated:** valid against the blunt
  version; mitigated by running only the *named test* (one test, two runs — cheap), only when the AC
  declares one, and gating it on merge-readiness rather than every push.
- *Maintenance (high/0.84):* a new required Gate job synced to ~10 repos + the consumer template
  collides with **intentional** per-repo Gate divergence (Counter_Risk custom Gate, Trend ruff-only,
  LMS Postgres). **Adjudicated:** valid → ship as **opt-in**, firing only when the AC names a
  deliberate-break test; do not make it a new required job in every repo.
- *Fit (medium/0.78, non-blocker):* `local_verify` overlays only test files, so neither it nor T1
  catches the agent **editing the test itself**. **Adjudicated:** real limitation → pair T1 with a
  cheap **test-tamper tripwire** (block/flag if the PR diff modifies the named acceptance test),
  per ImpossibleBench.

**Implementation sketch.**
1. `agents-pr-meta` parses the Acceptance Criteria for a deliberate-break marker naming a test
   nodeid + the file to revert (the issue format already prescribes this shape). No marker → skip
   (opt-in).
2. New Gate job `deliberate-break` (or a step in `test-quality`, which already fetches the base ref):
   `git archive BASE | extract` → overlay the named test file → run **only** the named test on base
   (expect fail) and on HEAD (expect pass). Reuse `local_verify.verify(..., test_paths=[named])`
   verbatim (it is already selftested and accepts explicit `--test-path`).
3. Test-tamper tripwire: fail if the diff modifies the named acceptance-test file's assertions.
4. Arm the dormant `runtime_ac_merge_guard.js`: when the AC declares a deliberate-break/runtime
   criterion, auto-apply the `acceptance-criteria` label so high-risk merges that *can't* be
   verified in Actions correctly **defer to the local `merge_guard.py`** (the plumbing already exists).
5. Emit a structured result to the `langsmith-fleet/v1` artifact (verdict + which gate ran) so the
   Brain can later weight agents by who ships hollow tests.

**Fit verdict: STRONG. Build first. Brain-free, owner-endorsed, closes a real hole — once scoped to the named test.**

---

### #2 — Minimal curated repo-knowledge export

**What it is.** Stop letting the GitHub-side agents start cold. Surface the Orchestrator's curated
per-repo "definition of done / known gotchas" registry (`repo_knowledge.json` — already encodes
Counter_Risk=black, Trend=default-branch facts, LMS=Postgres, Workflows=sync surfaces) to the keepalive agents.

**Channel (this is the whole design decision).** Do **not** fork the registry into keepalive
prompt code. Have the local Orchestrator remain the single owner and **export a tiny, curated
`AGENTS.md` per repo** (committed). `AGENTS.md` is the Linux-Foundation cross-vendor standard
auto-loaded by 20+ tools including Codex, Aider, Gemini CLI, and Copilot — so the nudged agent
picks it up with **zero keepalive prompt changes**. One source, one export, no second store.

**Research (cited).**
- AGENTS.md is the de-facto standard, auto-loaded by the fleet's agents; "closest wins" nesting.
  (https://agents.md)
- **Counter-finding that shapes the design:** context files *often hurt* first-try success —
  developer-written +4%, **LLM-generated −3%**, >20% cost (ETH Zurich, 138 SWE tasks,
  https://arxiv.org/abs/2602.11988). Minimalism wins; keep it human-curated and small.
- Convention files **stack with no override** and go stale (the freshness-ownership failure mode);
  context rot / lost-in-the-middle degrade reasoning as injected context grows
  (https://aclanthology.org/2024.tacl-1.9/). Curated incremental playbooks beat raw RAG-over-history
  (ExpeL https://arxiv.org/abs/2308.10144; ACE https://arxiv.org/pdf/2510.04618, which names
  "context collapse" from wholesale rewrites).

**Adversarial findings & adjudication.**
- *Maintenance (high/0.8) & Fit (high/0.8):* two copies drift; ETH says bloated/LLM-generated
  context makes agents *worse*; the local lane already injects this, so it's "redundant + negative."
  **Adjudicated:** the vetoes target a *fork-and-bloat* version I am not proposing. (a) The registry
  is **human-curated, evidence-driven, task/lane-filtered, ≤3000 chars** — the +4% regime, not the
  −3% one. (b) **Export, don't fork** kills the drift: one owner → one committed `AGENTS.md`.
  (c) Keepalive becomes the **freshness owner** — on each cycle, validate referenced paths/commands
  still exist and prune dead rules (runs entirely in CI, turns the staleness liability into an
  automated refresh). (d) "Redundant with the local lane" is wrong for keepalive: the local lane
  injects for the *local* dispatcher; the GitHub agents get **nothing** today.
- *Cost (low, non-blocker):* near-zero.

**Implementation sketch.** Add `repo_knowledge.export_agents_md(repo)` emitting a ≤30-line section
to each repo's `AGENTS.md` under a managed marker; a tiny scheduled Workflows job (or a step in the
existing repo-review uploader) commits it; a Gate lint validates the marked section's cited
paths/commands still resolve and warns on staleness. Keep the section short, load-bearing rules
first.

**Fit verdict: STRONG, cheap, low-risk — provided it stays minimal, single-owner, export-only.**

---

### #3 — Trustworthy verdict: structured file, not grep (keep the single cross-family judge)

**What it is.** The starting proposal's "#5 multi-judge AC panel." Adjudication **splits** it: reject
the N-judge panel, but extract the genuinely high-value, cheap, store-free piece hiding inside it —
**make the verifier's verdict un-spoofable**, and enforce that the judge is a *different family*
than the implementer. The Orchestrator's `runtime_ac.py`/`runtime_ac_panel.py` already produce
**structured JSON verdicts written to a path outside the diff**; transfer *that pattern* to
`reusable-agents-verifier.yml`, which today greps `verdict:\s*pass` over agent-authored text that
includes the diff (an agent can write `Verdict: PASS` into its own diff and the judge echoes it).

**Research (cited).**
- arXiv 2510.11822 (the paper behind `adversarial.py`'s minority-veto) confirms LLM judges
  rubber-stamp: individual TNR <25%; **minority-veto (n=4) reaches only TPR 95.5% / TNR 30.9%** — it
  still misses ~69% of bad outputs and adds false-blocks. "Pair it with the deterministic checks,
  which have far higher TNR." (https://arxiv.org/abs/2510.11822)
- A **diverse jury** of small models beats one big judge on bias and cost (PoLL,
  https://arxiv.org/abs/2404.18796) — but the active ingredient is *family diversity*, and the
  per-PR cost/latency is the MoA tax. Never let the family that wrote the PR judge it (self-preference
  ~10%, https://arxiv.org/html/2410.21819v2).
- The owner's design doc independently concludes: single-worker + **one** strong cross-family judge
  over multi-judge debate (MAST: ~79% of multi-agent failures are coordination; MoA locks in false
  consensus).

**Adversarial findings & adjudication.** The panel drew 3 vetoes (critical/high/high): N calls × MoA
latency per PR; ~31% TNR adds false-blocks that re-arm the keepalive loop (each re-arm = another
billed agent job); the panel's distinguishing value (`record_panel_verdict` → feedback +
evidence_gaps; judge-reliability weighting) is **Brain-coupled** so it can't run in Actions without a
divergent store; and a **`model2` cross-provider compare path already exists** in the verifier.
**Adjudicated: REJECT the panel; BUILD the two cheap wins** — (1) structured verdict file +
tamper-detection (forces FAIL if a `verdict:` string appears inside the diff region), and (2)
cross-family precondition (judge ≠ implementer family) using the existing `model2` slot. Optionally
add a single refute-mode prompt to that one judge (cheap, advisory). Judge-*reliability* weighting
stays in the local Brain, fed by `langsmith-fleet`.

**Fit verdict: BUILD the verdict-hardening (cheap, closes an active spoofing hole); REJECT the panel (expensive, Brain-coupled, ~31% TNR, dominated by the existing compare mode).**

---

### #4 — Durability-as-success export

**What it is.** Extend keepalive's notion of "done" from "merged + green" toward "did it durably
hold," using `durability_sweep.py`'s logic: post-merge, detect reverted/reopened PRs (Brain-free
`gh` queries) and feed the durability label to the learner.

**Research (cited).** Merge is provisional: ~1-in-5 "solved" patches are wrong; OpenAI **abandoned
SWE-bench Verified** in early 2026 over flawed tests + memorization — "even a curated pass signal
degrades; the durable label must come from post-merge reality" (https://www.latent.space/p/swe-bench-dead).
DORA's change-failure-rate + 2024 rework-rate and **code-turnover over 14–30–90 days** are the
established post-merge truth signals (https://dora.dev/guides/dora-metrics/;
https://larridin.com/developer-productivity-hub/code-turnover-rate-ai-quality-metric).
**Honest counter-evidence:** "Will It Survive?" finds agent code survives *longer* than human code
(death rate 53.9% vs 69.3%) — survival ≠ correctness (https://arxiv.org/html/2601.16809v1). So
durability is a real but **noisy, lagged** label, not a merge gate.

**Adversarial findings & adjudication.** Three vetoes converge on **timing, not architecture**: the
shape is owner-compliant (export-only, feeds one Brain), but the `langsmith-fleet` **producer pipe
is not flowing** (0 artifacts; PR #2473 unmerged; `cost_usd` often null), and the only consumer is
the local learner. Building a fleet-wide post-merge sweep that emits into a dark pipe pays Actions
minutes + `gh` rate budget for zero realized signal. The detection itself is cheap and Brain-free,
and `durability_sweep.py` already exists locally with `GRACE_DAYS=7` (extend toward the literature's
14–30 day window). **Adjudicated: DEFER.** Sequence after PR #2473 lands and the (agent×tier)×verdict
join exists; until then the local `durability_sweep` already does the job for local runs.

**Fit verdict: RIGHT SHAPE, WRONG TIME. Architecturally the model transfer (feed the Brain via langsmith-fleet); defer until the producer flows.**

---

### #5 — Churn-without-verification escalation (a sliver of root-cause redirect)

**What it is.** The starting proposal's "#6 root-cause redirect." Adjudication finds **most of it is
already built**: `error_classifier.js` (transient/auth/resource/logic/unknown + recovery) is wired
into ~10 workflows, and `detectStall`/`needs-human` already handle the blunt cases. `redirect_policy.py`'s
richer verbs (wait/collect/inspect/redirect/decompose) collapse, at the Actions boundary, to the two
actions keepalive can actually take: keep the same agent, or hand to a human.

**The one genuinely-additive, plan-endorsed piece:** today `effective = commits >= 1`, so a
confidently-wrong agent that commits every round is "effective" and never escalates. Add a
**churn-without-verification detector**: *N commits + no verified pass + rising rework → down-weight
and escalate* (the inverse of "commits = good"). This is small, Brain-free, and additive to the
existing policy — not a port of `redirect_policy.py`.

**Adversarial findings & adjudication.** Maintenance (critical/0.88) + fit (medium/0.72): porting
`redirect_policy.decide` duplicates *decision logic* into the file mandated to "stay as-is," and
changes no Actions outcome. **Adjudicated:** agreed — reject the port; keep only the
churn-without-verification escalation, which pairs naturally with #1 (a deliberate-break failure is
exactly a "not verified" signal).

**Fit verdict: MOSTLY ALREADY BUILT. Take one small additive detector; do not port the classifier.**

---

### #6 — Learned agent routing in keepalive

**What it is.** Use `feedback.current_weights(task_type)` (durability-driven Beta-Binomial posteriors)
to inform keepalive's agent selection instead of the label/registry + blunt stall-switch.

**Adjudication: REJECT for keepalive — keep it local.** Three independent reasons, all confirmed
against ground truth and the owner's own plan:
- **Architecturally blocked:** `router.py:271` reads `feedback.current_weights()` live from the
  local SQLite Brain, which Actions cannot read. The only compliant options are a divergent CI-side
  brain (the owner's explicit anti-pattern) or a frozen exported table — and a frozen table is the
  textbook **routing-collapse/drift** scenario where the export silently diverges from the live
  posterior (EquiRouter, https://arxiv.org/abs/2602.03478; "When Routing Collapses").
- **No decision to change:** keepalive routes by `agent:*` label at PR creation; at tens of
  PRs/week per arm there is near-zero evidence (cold-start), which is exactly why the owner's design
  **cut ε-greedy/learned-score at solo volume** and keeps `agent_delegation_policy.js` as-is.
- **The owner already decided this:** "one brain, one language … we do not maintain routing logic in
  two languages on two runtimes."

If a need ever appears, the only correct path is **offline-policy-export + off-policy-evaluation**
(learn locally, freeze, validate the new table beats the old, commit a small `routing-table.json`
CI reads, monitor arm-entropy for collapse) — the standard Open Bandit Pipeline pattern
(https://arxiv.org/abs/2008.07146). Not now.

**Fit verdict: REJECT. Highest Brain-coupling, lowest marginal value, directly counter to the owner's design. Keep routing in the local lanes; feed it `langsmith-fleet` evidence instead.**

---

## Recommended first build — concrete plan for #1

**Build the scoped deliberate-break gate** as an **opt-in** Gate check:

- **Trigger:** only when a PR's Acceptance Criteria contains a deliberate-break marker naming the
  test nodeid + the line/file to break (the issue format already requires one). Absent → skip.
- **Mechanism:** reuse `local_verify.verify(worktree, base_ref=BASE, test_cmd=…,
  test_paths=[named_test])` — already selftested. Run the *named test only* on base (expect fail)
  and HEAD (expect pass); `FAIL_HOLLOW`/`FAIL_BROKEN` ⇒ block.
- **Companion tripwire:** block/flag if the diff edits the named acceptance test's assertions
  (anti-reward-hacking).
- **Arm the existing guard:** auto-apply `acceptance-criteria` when a runtime/deliberate-break
  criterion is present, so non-CI-verifiable high-risk merges defer to local `merge_guard.py`.
- **Feed the Brain:** emit the verdict to `langsmith-fleet/v1` for later agent weighting.

**Acceptance criteria (dogfood the deliberate-break pattern on itself):** a PR whose named test
passes on base must be blocked with `FAIL_HOLLOW`; a PR that edits the named test's assertions must
be flagged; a PR with no deliberate-break marker must be untouched; the check must run on the named
test only (not the full suite) and not re-trigger redundantly under `cancel-in-progress`.

**Risks & mitigations (from the adversarial pass):** false-blocks on general PRs → *scope to the
named test, opt-in*. CI cost → *one test, two runs, merge-readiness-gated*. Fleet divergence →
*opt-in, not a new required job*. Misses agent-edits-the-test → *tamper tripwire*. Residual: still
won't catch a "heuristic" patch that genuinely passes a weak named test → that is what the deferred
durability export (#4) and the local Brain's weighting ultimately backstop.

**Sequence after #1:** #2 (repo-knowledge export) and #3's verdict-hardening are both cheap,
Brain-free, parallelizable; #4 waits on PR #2473; #5 is a small follow-on to #1; #6 stays local.

---

## Appendix A — adversarial verdict tally (raw → adjudicated)

Minority-veto threshold = 2 substantiated high/critical vetoes (`adversarial.aggregate_veto`).
"Adjudicated" reflects ground-truth adjudication, not obedience to the vetoes.

| Transfer | ADV-cost | ADV-maint | ADV-fit | codex offload | Raw veto count | Adjudicated |
|----------|----------|-----------|---------|---------------|----------------|-------------|
| T1 deliberate-break | high✗ | high✗ | med (no) | **high✗ (0.90)** | 3 high | **BUILD, reshaped** (named-test, opt-in, +tamper) — vetoes were against the *blunt* version |
| T2 repo-knowledge | low (no) | high✗ | high✗ | — | 2 high | **BUILD, export-not-fork** — vetoes were against *fork-and-bloat* |
| T3 durability | med✗ | med✗ | high✗ | — | 3 | **DEFER** — valid on *timing* (dark pipe), shape is owner-compliant |
| T4 routing | med✗ | **crit✗** | **crit✗** | — | 3 (2 crit) | **REJECT** — vetoes upheld; keep local |
| T5 panel | **crit✗** | high✗ | high✗ | — | 3 (1 crit) | **REJECT panel; BUILD verdict-hardening** |
| T6 redirect | low (no) | **crit✗** | med✗ | — | 2 | **MOSTLY REJECT**; keep churn-without-verification sliver |

The adversarial stage materially changed the design (it did not merely confirm it): it scoped T1,
reframed T2 to export-only, split T5 into reject-panel/keep-hardening, deferred T3 on flowing-pipe
grounds, and shrank T6 to a one-line detector — while upholding the T4 rejection.

## Appendix B — key sources (all cross-verified ≥2 sources unless noted)

Verification / anti-hollow-PR: SWE-bench Verified (openai.com/index/introducing-swe-bench-verified/;
epoch.ai); Wang/PatchDiff 2503.15223; SWE-bench+ 2410.06992; ImpossibleBench (LessWrong, authors
Zhong/Raghunathan/Carlini); EvilGenie 2511.21654; OpenAI Codex Security (developers.openai.com/codex/security).
Durability: latent.space/p/swe-bench-dead (OpenAI abandons SWE-bench Verified — *blockchain.news
primary 403'd; corroborated via Latent Space + search snippet*); DORA (dora.dev); code-turnover
(larridin.com); "Will It Survive?" 2601.16809 (counter-evidence).
Multi-judge: 2510.11822 (minority-veto, exact figures verified); self-preference 2410.21819; PoLL
2404.18796. Repo-knowledge: agents.md; ETH AGENTS.md study 2602.11988 (*recent arXiv ID, flagged
by the research agent; numbers corroborated via ETH SRI lab page + independent write-ups*); Reflexion
2303.11366; ExpeL 2308.10144; ACE 2510.04618; Lost-in-the-Middle (TACL 2024). Routing: RouteLLM
2406.18665; EquiRouter 2602.03478 (*recent ID, flagged*); MoA 2406.04692; Open Bandit Pipeline
2008.07146; Router-R1 2506.09033.
Owner's prior design: `Workflows/docs/plans/local-multi-agent-orchestrator.md` (revised after five
red-team passes) — the single most load-bearing internal source; this brief is consistent with it.

---

## Addendum (2026-06-19) — original-#6 re-evaluated as a *benefit* question (supersedes the rank-5 treatment above)

The owner correctly noted the first pass judged *implementation* ("don't duplicate redirect logic across runtimes"), not *benefit*. Re-ran the full 3-stage method on the right question: **what is the benefit potential of the local stateful orchestrator supervising keepalive-driven PRs** (richer recovery: early-stop, root-cause diagnosis, as-needed decompose; verification-before-done; memory-leverage), decoupled from implementation — noting the orchestrator already runs locally on these PRs (closer lane), so "local supervision" sidesteps the Actions constraint and honors one-brain.

**Stage 1 (research) — benefit is real IN PRINCIPLE.** Self-correction works *with an external oracle* (the CI gate), not intrinsically (Huang et al., ICLR 2024, arXiv 2310.01798). Root-cause-aware recovery **+26% relative success, largest on cheaper models** (AgentDebug, arXiv 2509.25370). External early-stop of doomed runs **saves 28–64% of wasted tokens at 1.6–4.2% success cost**, and agents are *not* budget-aware so it *requires* an external watchdog (BAGEN, arXiv 2606.00198; EET arXiv 2601.05777). Failure-triggered decomposition **+28 pts** (ADaPT, arXiv 2311.05772). Memory/trajectory-diff is the orchestrator's structural edge a stateless loop cannot have. Caveats: cap repair at ~2 rounds (Olausson, ICLR 2024); decompose sparingly; drift is often a *bounded equilibrium* (false-kill risk); multi-agent ≈15× tokens & single-agent often wins (MAST, arXiv 2503.13657; Cognition "Don't Build Multi-Agents" → "Devin manages Devins").

**Stage 2 (adversarial) — near-unanimous against the LIVE/heavy version** (3 Claude refuters + codex offload, codex critical/0.88):
1. **Split-brain control (decisive).** keepalive's `agent_delegation_policy.js` and the orchestrator's `redirect_policy.py` both wake at the ~3-stall horizon with *incompatible* verdicts (switch-agent vs decompose/kill), via *different channels* (runner label vs local `gh`+kill+claim-release), with *no shared lock* (`claims.py` does not extend to keepalive's GitHub execution) → manufactures the MAST coordination/misalignment failures by construction.
2. **Self-stall false-kill.** The supervisor is the same process whose heartbeat lapsed *today* and stalled gemini/vibe at 0 bytes; when it most confidently reads "run is dead," the likeliest cause is that *it* stalled — and its kill contaminates the very dataset meant to teach it.
3. **ROI ≈ zero realized at this deployment's flat-rate volume.** The cited figures are token/success on flat-rate-equivalent spend; keepalive burns paid-regardless seats + free Actions minutes, so realizable savings are ~nil against this deployment's savings ceiling (LOCAL_POLICY.md), while a Claude-driven supervisor adds new expensive-seat cost = the F9 "meta eats the savings" the design doc auto-disables.
4. **Redundant:** early-stop ≈ `detectStall(3)`; root-cause ≈ `error_classifier.js`; verify-before-done ≈ post-merge verifier + closer lane + the #2475 deliberate-break gate. 3 of 5 already wired.
5. **Untransferred evidence:** AgentDebug/BAGEN/ADaPT are ALFWorld/GAIA/WebShop/TextCraft (game/web-nav, dense feedback, resettable) — not GitHub-PR loops.
6. **Data-starved:** memory (the one true differentiator) needs a corpus; the store has runs=15/outcomes=5 — a posterior on n=5 is the prior.

**Adjudicated verdict (the vetoes align with ground truth and the owner's own doc):** do **not** wire up a live richer supervisor now. The first-pass *outcome* (don't build it now) holds, but the correct *reasons* are ROI-at-solo-volume + split-brain-control + redundancy + untransferred-evidence + data-starvation — not "divergence." The benefit is real *in principle*; it is not realized *here, now, live*.

**The staged, owner-aligned yield (this is the real answer to "what is the potential for benefit?"):**
- **Now — one cheap, Brain-free, conflict-free win:** lower keepalive `detectStall` 3→2 **and add a hard per-PR token/round budget cap** (captures most of BAGEN's external-early-stop benefit; one config edit; no supervisor; no second controller). Plus the churn-without-verification escalation: commits>0 but no verified pass + rising rework → escalate.
- **Next — earn the rest via shadow-mode corpus-building:** run the already-built advisory `watch.py`/`redirect_policy.py` in **shadow** over keepalive PRs — record what they *would* advise + the actual outcome — to accumulate labeled stalled-vs-successful trajectories with **no live action** (no kill/decompose → no split-brain). Owner-aligned (shadow-by-default, like the research arm). Turns "data-starved" into a plan.
- **Later — only if earned:** once the corpus supports an A/B showing supervised recovery beats keepalive-alone on real PRs, consider going live — and only **layered** (supervisor acts *after* keepalive terminally escalates to `needs-human`, never concurrently), so there is a single authority at any moment.

Net: the orchestrator's capabilities *can* benefit keepalive, but the realized value today is **one cheap config win + a shadow corpus that earns the rest** — not a live supervisor.
