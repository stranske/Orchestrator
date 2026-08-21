# BRIEF — Expanding the RANGE of work the agent fleet can do (backlog #2)

Grounded in the 2026-06-15 deep-research scan (24 adversarially-verified findings; sources inline).
Today the fleet's range is **issue→PR→merge**. SOTA agents expand range on two axes: richer
task-decomposition/orchestration, and a wider tool surface beyond code-editing. Below are 5 candidate
improvements — each with the research-backed pattern, tools + tradeoffs, a fit verdict, and an
implementation sketch — then the recommended top pick.

## Cross-cutting finding — a ready-made "range taxonomy"
"Range" is now measured by a suite beyond SWE-bench Verified: **Commit0** (greenfield/from-scratch),
**SWE-Bench Multimodal** (visual/frontend), **SWT-Bench** (test generation), **GAIA** (general agentic
tool-use) — OpenHands V1 reports 56–80% across them [arxiv 2511.03690, high]. These enumerate the
work-types to expand into *and* double as eval categories for new lanes (ties to backlog #6 observability).

**How to ADD range cheaply (architecture):** OpenHands' SDK [2511.03690, high] makes delegation a *tool
primitive* (sub-agents = independent conversations) and adds new reach via **MCP** (tool JSON-schemas
become first-class actions) over **pluggable sandboxes** — i.e. browsers/runtime/static-analysis plug in
without touching the agent core. Our analog: expose each new capability as an **orchestrator-offered
tool/CLI any lane agent can call**, rather than per-agent wiring — more robust given heterogeneous CLI
agents (Claude/Codex/Cursor/Gemini/vibe) with uneven MCP support.

---

## 1. Frontend / visual verification capability — **RECOMMENDED TOP PICK**
**Range gap:** the fleet can edit TS/React but cannot *run + observe* a UI, so it can't verify frontend
behavior — the trip-planner timeline (#2) and LMS issues need exactly this; such PRs ship unverified today.
**Pattern + tools:** **Playwright MCP** drives the browser via the **accessibility tree** — "no vision
models needed, operates purely on structured data… deterministic," ~200–400 tokens/snapshot vs ~3000–5000
for screenshots [microsoft/playwright-mcp + playwright.dev, high]. Opt-in vision/coordinate mode only for
canvas/SVG. Lets text-only members (Codex, vibe) do real DOM assertions / form-fills / navigation without
a multimodal model.
**Fit (research):** *Strong fit.*
**Sketch:** an orchestrator capability `verify_frontend` — launch a page (served URL or a repo's
dev-server cmd), capture the a11y snapshot via Playwright (tree mode), run declarative assertions
(element/text present, navigation/flow works), return `{ok, snapshot, findings}` a lane can act on.
Agent-agnostic (a tool the orchestrator offers, not per-agent MCP); vision fallback flagged for canvas.
First increment: the helper + selftest + a demo on a served page; then point it at trip-planner.
**Why top pick:** biggest genuine *range* expansion (a sense the fleet wholly lacks), unblocks real
pending frontend work, strong + cheap evidence, and the most exploratory/fun.

## 2. Test-generation lane with an assured-acceptance gate
**Range gap:** the fleet implements given issues but doesn't proactively *raise coverage* — yet the
repo-review queue keeps emitting "baseline coverage" issues (Inv-Man, Counter_Risk #2, trip-planner #1).
**Pattern + tools (strongest, most transferable evidence):** never accept raw LLM tests — gate them.
Meta **TestGen-LLM** "Assured Offline LLMSE": build → 5-run reliability → coverage-delta → non-regression;
Instagram funnel 75%/57%/25%, 73% of recommendations accepted [2402.09171, high]. **CoverUp** coverage-
guided run-measure-feedback loop (SlipCover), 80% vs CodaMosa 47% median/module [2403.16218, high].
**TestART** generate-then-repair — repair is the dominant correctness driver (+28% ablation) [2408.03095,
high]. Tools are Java-centric; transfer the *pattern* on the Python-native stack (pytest + coverage +
Hypothesis). Caveat: coverage ≠ correctness — keep the reliability + non-regression filters.
**Fit:** *Strong fit* (Python repos).
**Sketch:** a `testgen` lane: delegate test generation for module X → **acceptance gate** (`testgen_gate.py`:
import/build-ok → run 5× for flakiness → coverage-delta vs baseline → non-regression of existing tests) →
open a PR only on pass. Self-contained (pytest+coverage already in the fleet).
**Built first lane increment (2026-06-16):** `testgen_lane.py` now emits the gate-backed delegation prompt,
`backlog.py` maps tests/coverage labels to `task_type=testgen`, `router.py` routes that task type through
non-Claude lanes by default, and `dispatcher.py` carries a generic test-generation prompt template.

## 3. Epic decomposition lane (goal → subtask plan → dispatch → re-decompose)
**Range gap:** the opener works pre-formed issues; the fleet can't take a *large/vague goal* and run it.
**Pattern + tools:** **Plan-and-Solve** zero-shot plan-then-execute as the baseline planner pass
[2305.04091, high]; **ADaPT** as-needed *recursive* decomposition triggered on executor failure — beat
ReAct + plan-and-execute by 28–33% [2311.05772, high]; **MetaGPT** structured-document handoffs
(PRD→file-list/interface→impl→tests) + executable-feedback loop, but NOT its rigid 5-role waterfall
[2308.00352, high]. Caveat: decomposition evidence is GPT-3.5-era on non-SWE tasks — pattern, not guarantee.
**Fit:** *Partial→good* — map "executor failure" to a lane that stalls/fails CI (matches the existing
delegation-on-stall), then re-decompose the *stuck* sub-task; adopt structured-doc handoffs (echoes the
lane sentinel / handoff-relay).
**Sketch:** an `epic` entrypoint: planner pass emits a subtask-issue list → opener dispatches per subtask →
on a stuck subtask, ADaPT-style split + re-dispatch. Higher complexity; sequence after #1/#2.
**Built first lane increment (2026-06-17):** `epic_lane.py` now emits strict planner prompts and validates
agent-produced plans with epic metadata, dispatchable subtasks, dependencies, integration order, final
verification, and re-decomposition triggers. Backlog labels map to `task_type=epic`; router defaults avoid
Claude and prefer Gemini/AGY for this substantial planning lane; dispatcher has a planning prompt template.
Automatic issue creation / subtask dispatch remains a later increment.

## 4. Codemod / refactor-campaign lane
**Range gap:** single-issue edits only; no cross-cutting structural campaigns.
**Pattern + tools:** type-attributed **OpenRewrite** LSTs enable cross-file/cross-project-accurate
transforms regex/plain-AST can't [docs.openrewrite.org, high] — but JVM-centric. For Python/TS:
**ast-grep / Comby / jscodeshift** (structural, no cross-project type attribution). Model: a recipe
catalog applied fleet-wide, agent-driven for the non-mechanical cases.
**Fit:** *Partial* — use ast-grep/Comby for the fleet; treat OpenRewrite's recipe-catalog as the campaign
template; reserve OpenRewrite for any JVM repos.
**Sketch:** a `codemod` lane: author/select an ast-grep rule → dry-run across target files/repos →
delegate review+fix of non-mechanical cases → batched PRs. Sequence after #1/#2.
**Built first lane increment (2026-06-17):** `codemod_lane.py` emits strict campaign-authoring prompts,
validates campaign JSON (metadata, scope, recipe, rollout, acceptance/validation/manual_review/
delegate_prompt), and produces dry-run plans with review-before-run commands for
ast-grep/Comby/jscodeshift/OpenRewrite/custom when enough fields are present. Backlog labels map to
`task_type=codemod`; router defaults avoid Claude and prefer cursor/vibe/codex; dispatcher has a campaign
prompt template. Automatic codemod apply and batched PR rollout remain later increments.

## 5. Cross-repo coordinated-change capability (fleet-specific)
**Range gap (real + recurring):** a contract change in `stranske/Workflows` must land *coordinated* PRs in
consumer repos (the sync-manifest already models the dependency); the fleet does these one repo at a time.
**Research status:** *open question* — the verified evidence surfaced no strong external pattern for
dependency-aware multi-repo PR coordination (OpenRewrite is cross-project *within* JVM; SWE-bench is
single-repo). Fleet-specific design, informed by the sync-manifest + the existing opener/closer.
**Sketch:** a `cross-repo` campaign: a source change + the sync-manifest/dep graph → fan out coordinated
draft PRs to affected consumers → a barrier that merges them together (or sequences source→consumers).
Design-heavy; lowest evidence — sequence last.
**Built first lane increment (2026-06-17):** `cross_repo_lane.py` emits strict coordination-authoring
prompts, validates source/consumer coordination JSON, and produces dry-run rollout plans with planned
work items, dependency/barrier ordering, and dispatch-ready prompts. Backlog labels map to
`task_type=cross_repo`; router defaults avoid Claude and prioritize Gemini/AGY; dispatcher has a
cross-repo planning template. Automatic PR rollout and live barrier enforcement remain later increments.

---

## Recommendation
**Top pick to build now: #1 (frontend / visual verification).** It's the largest true *range* expansion
(a capability the fleet entirely lacks), unblocks real pending frontend work, has strong + token-cheap
evidence, and best fits the "fun exploration" intent for this step. **#2 (test-gen gate)** is the
highest-evidence, most-recurring-demand runner-up and the safest pure-ROI build — the natural "next."
#3–#5 are higher-complexity / lower-evidence; sequence after.

*Honest build note:* #2 is the more robustly-shippable-this-session option (no external deps); #1 carries
some Playwright/browser-install setup risk. I'll verify Playwright is installable before committing the #1
build and **pivot to #2 if the environment fights it** — stating which.

*Open questions carried from the research (for later):* a benchmark-validated standalone "vague-goal →
refined spec/issue" agent (Theme 3); dependency-aware multi-repo PR coordination (Theme 2 / #5 above);
concrete sandbox-vendor tradeoffs (E2B/Modal/Daytona) if remote execution is ever needed; and whether
as-needed recursive decomposition still beats upfront plan-and-execute with modern CLI executors.
