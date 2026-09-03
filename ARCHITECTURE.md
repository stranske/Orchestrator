# Orchestrator architecture — rails vs. agent-roles

> **Keeping this current (CONTRACT).** This file and [`orchestrator-loop.svg`](orchestrator-loop.svg)
> are the source of truth for the rails/roles taxonomy and the loop. **Any change to the system's
> stages, components, the rail/role classification, the feedback surfaces, or the role registry
> (`roles.py`) MUST update BOTH this doc and the diagram in the same change.** `PLANNING.md` and
> `ORCHESTRATOR.md` point here — do not let them drift. If you touch `roles.py` / `route_role` / the
> registry, or move a component across the rail/role line, updating the diagram is not optional.

![Orchestrator loop: deterministic rails vs. callable agent-roles](orchestrator-loop.svg)

## Where this sits in the larger pipeline (READ FIRST)

**This document describes ONE COMPONENT.** Everything below — rails, roles, the feedback loop — is
internal to the Orchestrator. The pipeline it participates in has its **system-of-record in the
`Workflows` repo**, and nothing in this file can tell you what that pipeline is doing. Reading only
this document has already produced confidently wrong fleet-level conclusions.

```
  repo review (Workflows)  ->  human-decision packet  ->  APPROVED-ISSUE QUEUE (steward repo)
                                                                    |
                                                     opener lane (local, outside this tree)
                                                     creates issues + draft PRs, own PR cap
                                                                    |
                                        KEEPALIVE (Workflows GitHub Actions) drives each PR:
                                        agent:* label + green Gate + unchecked tasks -> rounds,
                                        stands down when all acceptance criteria are checked
                                                                    |
                                                     closer lane (local) merge -> verify -> close
```

**The Orchestrator's three real interfaces to that pipeline:**

| Interface | Direction | What it is |
|---|---|---|
| `capacity.py` | pipeline → here | The lanes read it to choose which agent gets an advisory review |
| `orchestrator_review` fallback | pipeline → here | A review-fallback path routes an advisory review through this tool |
| `tick.py --active` → `delegate_remote` | here → pipeline | Applies `agent:*` labels, driving keepalive on REMOTE capacity — **shadow by default since 2026-09-03** (`ORCH_DISPATCH_LANE=1` re-enables): 14 dispatches in 30 days, 9 abandoned, none durable, while keepalive ran 1,239 rounds without it |

So this tool is a **capacity advisor, a review router, and (in shadow unless deliberately enabled) a keepalive driver**. It is **not** the
fleet's work-discovery engine: `backlog._is_ready()` is this tool's own private discovery path, and
the fleet's work originates from the approved-issue queue, not from `status: ready` labels.

### Two things that will mislead you if unstated

**The word "orchestrator" is overloaded.** The keepalive contract in `Workflows` has a section headed
*"Orchestrator Invariants"* which means the **GitHub Actions concurrency and round orchestration** —
a different system from this repository. When a Workflows doc says "the orchestrator", assume it means
the Actions workflow until proven otherwise.

**Double-dispatch is prevented by a coarse heartbeat, not a per-target lock.** `orchestrate.sh
--active` writes a freshness heartbeat; the lanes' prerun reads it and yields that round. Fail-open —
absent, stale or malformed means the lanes proceed normally. One side drives at a time, but the
exclusion covers a whole ROUND (~15-minute freshness), not an individual issue. `claims.py` is local
and does **not** span the lanes' GitHub execution, so do not add a dispatch path that assumes
per-issue locking.

**Metrics from this tree are scoped to this tree.** `backlog.json` counts, `issue_readiness` verdicts
and `true_open` describe this tool's own dispatch lane, never fleet throughput.

## The one rule: selection stays deterministic; judgment becomes agent-roles

The discriminator for "should this be an agent?" is **who decides the next step — code or the model.**

- **Do NOT agentify the Decide stage's selection step.** Choosing *which agent/LLM* does a piece of
  work (`router.select_agent`) looks like judgment, but it is a **learned policy**, not an LLM call —
  and it is the signal `feedback.py` estimates. Replacing it with an LLM would (a) blur the
  credit-assignment the learner depends on, (b) add per-item token cost, and (c) make routing
  non-reproducible and un-A/B-able. Selection is the place that **stays code**. The agentic layer may
  *override* the router (the seat already does), but the router itself is a rail.
- **Agentify judgment under an open/ambiguous action space** — redirect, decompose, triage,
  prompt-authoring, adjudication. Each becomes a **typed agent-role** whose LLM backend is **swappable
  and router-chosen**.

## Reading the diagram

The five boxes are the orchestrator's cycle (`ORCHESTRATOR.md` → "Your loop, each cycle"). Inside each
stage, every component is tagged:

- **blue = deterministic rail** — code. Keep it predictable; determinism is the safety/auditability property.
- **amber, dashed = agent-role** — LLM judgment behind a typed contract; the model is swappable.
- **teal = delegated sub-agent** — the worker CLIs spawned in isolated worktrees.

The single left arc is the loop closing. What it carries back is the point: `feedback.py` relearns
**both** surfaces — `selection weights` (blue) and `role ↔ backend fit` (amber) — into the next cycle.

## Rails — deterministic, keep as code

`router` (selection) · `claims` · `capacity` · `provision` · `dispatcher` (transport) · `adapters` ·
`feedback` (the learner/store) · gates (`testgen_gate`, `local_verify`, `merge_guard`,
`runtime_ac_gate`, `frontend_verify` (Gate 1), `ux_review.gate_decision` (Gate 2 pass-requirement)).

Determinism here is load-bearing: the claims/capacity/provision rails are what the "0 unsafe
delegations" guarantee rests on, and the gates guard terminal merges and must stay auditable. An
LLM verifier (a review panel) is a *supplement* to a gate, never a replacement for it.

## Agent-roles — judgment, typed contract, swappable backend

A role is defined in `roles.py` as `Role(name, route_as, eligible_backends, mode, build_prompt,
validate)`. `route_role(role, cap)` reuses `router.select_agent` restricted to the role's
`eligible_backends` (RESERVE seats — claude — excluded by default, allowed only as last resort or
`high_leverage=True`). So **role-backend choice obeys the same capacity + learned weights as worker
selection**, and every role becomes a **new learnable surface**: `exp_abcd` / `feedback` can learn the
best backend *per role* the same way they learn the best implementer per task_type.

`route_as` is an **existing `ROUTE_TABLE` task_type used only as the routing prior**. Once a role has
accepted downstream outcomes, `route_role()` prefers learned weights for `role:<name>` and falls back to
`route_as` only while that role-specific surface is cold.

| role | replaces / upgrades | judgment it adds | status |
|---|---|---|---|
| **RedirectAgent** | `redirect_policy` heuristics | read log+diff+AC → action + corrected prompt | **built, shadow (2026-06-19)** |
| **PromptAgent** | dispatcher generic templates | issue → scoped prompt + definition-of-done | **built, shadow (2026-06-20)** |
| **DecomposerAgent** | `epic_lane` planner prompts | vague/large goal → subtask DAG | **built, shadow (2026-06-20)** |
| **TriageAgent** | `backlog` worth-it filter | which items now, skip underspecified, batch | **built, shadow (2026-06-20)** |
| **AdjudicatorAgent** | `runtime_ac_panel` / `adversarial` dispute step | verify a lone reviewer veto vs. ground truth | **built, shadow (2026-06-20)** |

## The feedback loop closes over both surfaces

`feedback.py` learns (1) **router weights** — which agent per task_type — and (2) **role ↔ backend
fit** — which LLM per role. Both return to the next cycle. Surface (2) is wired through role runs:
`feedback.record_role_run()` records a `task_type='role:<name>'` decision, and the downstream run's
outcome comes back to that role run over an **accepted influence edge**. That link forms automatically at
the dispatch seam — the accepted `role_run_id` is stamped onto the dispatch
(`dispatcher.delegate --influenced-by-role-run-id`, emitted into the plan by
`redirect_plan.attach_role_lineage`), `feedback.record_run()` writes the `influence_type='role'` edge, and
`feedback._propagate_outcome_lineage_in_conn()` back-propagates the acting run's terminal verdict when it
lands. `feedback.join_role_to_outcome()` is the manual equivalent for links made after the fact.
Attribution is to the ACTING run: only an `accepted=1` edge back-propagates, so a role whose proposal was
rejected records the disagreement and inherits no PASS. This keeps role learning separate from normal
implement/review weights while still using the same `relearn_quality()` machinery.

## The capability layer — what the tool can do, and how a surface finds it

This doc described rails and roles and never once said "capability", which let a whole session treat
the two axes as one. They are orthogonal:

- **rail vs. role** is *how a thing is implemented* — deterministic code, or LLM judgment behind a
  typed contract with a swappable backend.
- **capability** is *what a tool in the Orchestrator does*. It is the unit of accounting, and one
  capability routinely spans both (`adversarial-review` is role judgment, invoked by a rail gate,
  recorded over a deterministic acceptance edge).

The nine admission parts (`ADDING_CAPABILITIES.md`) are **not** the definition of a capability. They
are what must be present for one to work with this system — invocable, observable, findable,
improvable.

**Two kinds, and their measurement stories differ.** *Workflow* capabilities run implementation code
and have a definable success condition, so effectiveness is a pass/fail rate. *Sub-agent* capabilities
spin out a bounded, goal-scoped agent whose backend is router-chosen, so effectiveness is **backend
fit** and needs arm + member identity. Never average across the two kinds.

### Selection: three layers, because offering is all you can do

A capability is *offered*, never mandated — the calling agent may have a better way to do the work,
and constraining it to use a tool because we built it would be worse than it choosing otherwise. So
the design problem is not compulsion, it is **raising the probability the right capability is chosen**.

The published measurements say catalogue size is the dominant factor. Selection accuracy runs
84–95% at ~50 tools, 41–83% at 200, and near zero at 740, with a practical safe zone of **10–20 per
reasoning context** and a "lost in the middle" effect dropping mid-list selection to 22–52%. RAG-MCP
measured the fix: full catalogue exposed gave **13.62%**, top-3-of-15 gave **43.13%**. Anthropic's own
subagent guidance names the same failure — auto-selection is unreliable, and a session often does the
work itself even when a subagent's description matches cleanly.

So a 40-plus capability catalogue queried generically is the 13.62% condition, and the three layers
are ordered by when each starts working:

| Layer | Mechanism | Works from |
|---|---|---|
| 1 | `capability_advisor.SURFACE_BINDINGS` — declared, per surface and per PHASE of a long surface, 3–7 entries each with its reason; `CONSULT_SITES`, which declares who actually ASKS at each surface; plus `CAPABILITY_PRECONDITIONS`, which explains an offer without changing it | day one; no classifier, no history |
| 2 | `capability_propensity.rank` — orders *within* the bound set by measured usefulness | first resolved trials |
| 3 | `capability_advisor.learned_associations` — corrects the table from what a surface actually reaches for | once observations accumulate |

Layer 1 is a **rail**: a declared table plus a deterministic keyword classifier, no model call. The
committed table is the seed (tool); instance promotions live in the ledger (evidence).

**Binding prioritises, it never conceals.** Unbound capabilities are still returned, ranked after the
bound set and flagged `bound: false`. A concealed capability could never be selected, so it could
never earn the evidence that would bind it — the gate would starve its own drain.

**And a binding is only half of layer 1: `CONSULT_SITES` is the other half, and nothing declared it
until 2026-08-23.** `SURFACE_BINDINGS` says which capabilities a surface should be offered; nothing
said which surfaces are ever ASKED, and the two are independent — from a capability's point of view,
a binding to a surface no caller consults is indistinguishable from no binding at all. Measured over
the 43-row ledger: `ci` bound two capabilities and no caller anywhere consults a `ci` surface;
`opener-lane` and `closer-lane` bind ten between them and both lane prompts consult the advisor with
**no `--surface`**, so `binding_for("")` returns `{}` and the declared set never reaches the caller it
was written for. `repo-audit` is the control case — never consulted under its bare name, and
correctly so, because every consult happens at a phase key whose resolution merges the parent's
entries. So "not consulted" is a defect only for a key that is not a PREFIX of a consulted key.
A consult site is a **falsifiable claim about a file**: the selftest opens it. Present-and-no-longer-
naming-its-surface is DRIFT and fails; absent on this machine is *unverified*, never refuted — the
same "no ledger, no verdict" rule `capability_admission.commitments()` uses, because treating absence
as refutation would strand every skill-bound capability on a fresh clone.

**A surface therefore has exactly three declared states, and a fourth is a selftest failure.** It is
consulted (`CONSULT_SITES`), it deliberately binds nothing (`NO_BINDING` with the reason), or it holds
bindings nothing can reach and that is recorded with the reason and the fix (`KNOWN_UNCONSULTED` —
currently `opener-lane` and `closer-lane`, whose fix is a `--surface` flag in a lane TOML outside this
repository). Bindings nothing can reach that nobody wrote down is the fourth state, it is what `ci`
was, and it is invisible until a capability is stranded on it — so it now fails naming the SURFACE,
not just the capability. `KNOWN_UNCONSULTED` is a record and not a waiver: a capability bound only
there still fails requirement 9. A fixed entry may not linger either; a stale one fails, because a
cached reason outliving its evidence is the prose-cache defect under a different hat.

**Findability is the ninth admission requirement (2026-08-23), because the eight before it make a
capability invocable and observable and none of them makes it findable.** 37 of 43 capabilities had
no usefulness evidence and 22 of those were bound to no surface, so nothing could offer them and no
amount of running could produce evidence for them — every one had passed admission, and the rule
against it existed as prose in the document that argues prose does not survive the next session.
`capability_admission.req_findable` consumes `capability_advisor.surfaces_binding` (the inverse of
`binding_for`, so ONE resolver) and `consulting_surfaces()`, and it distinguishes `bound_nowhere` from
`bound_to_unconsulted_surface` because the fixes differ: declare a surface, versus bind a consulted
one or make the surface consult. A capability a rail invokes UNCONDITIONALLY rather than offers is
NOT exempt (it was, as `findability_category: no_surface`, until 2026-09-02): it declares
`exercise_bound` in `capabilities.KNOWN_DECLARATIONS` / `KNOWN_GATES` and is bound on exactly one
`rail-exercise:<phase>` surface, where the binding reason is a read-only or dry-run EXERCISE of the
rail's own code against a fixture or its own artifact, scored by a pre-committed check. The live path
stays with the rail; what becomes possible is a consult that can trigger it and a verdict that can
land — the exemption had made both impossible for fifteen rows.

Two things it deliberately does **not** decide, named rather than omitted. A surface that invokes the
entrypoint DIRECTLY without surface attribution: the `orchestrate` skill already runs `capacity.py`
while `windowed-capacity-policy`'s heartbeat sits behind `ORCH_CAPABILITY_HEARTBEATS`, which only a
live tick sets, so the capability is used and entirely uncredited.
`capability_activation_audit.heartbeat_reachable` answers a different question — it calls that row
`reachable` via `orchestrate.sh (CLI)`, because it asks whether *some* driver reaches the heartbeat,
not whether *this surface's* invocation is attributed to the surface — and deciding it needs the
surface's own prompt, which lives outside this repository. And a surface that is NAMED but never
ENTERED: only trial records can show that, never a table of files. `repo-audit:fix` was the worked
example — listed by the skill and reached by no audit run, since an audit ends at phase 5 and hands
implementation to the lanes — and on 2026-08-25 three independent implementation runs entered it nine
times with filed issues and commit targets, which is exactly the evidence a table of files could
never have produced. Its bound set grew by the two instruments those runs actually used
(`deliberate-break-verifier`, `frontend-verifier`); the classifier's vocabulary was deliberately not
widened, because widening it to raise a hit rate corrupts the learned associations. Enforcement is per-requirement dated (`REQUIREMENT_ENFORCED_FROM`) so the 43 pre-existing rows
are reported as drainable debt instead of failing the suite; the report prints the debt, its causes,
the surfaces that strand a binding, and the drainable count beside it.

**And a fourth input, orthogonal to all three: the per-repo contraindication.** The three layers above
rank a capability by how well it fits the SURFACE. None of them can say *this tool does not work
against this particular repository* — a fact that lives in the repo's own record, not in the ledger.
A real audit run was offered `frontend-verifier` and `repo-playbook` in the same response for a repo
whose audit history says `frontend_verify.py` snapshots its Streamlit SPA before the websocket render
completes; the two bound capabilities contradicted each other and the reconciliation existed only in
the auditor's head. `repo_knowledge`'s `contraindications` section now carries `{capability, reason,
instead, evidence}` per repo, and `capability_advisor.advise(repository=…)` annotates matching
candidates on both answer paths — the classified one and the classification-miss one a free-text
consult actually lands on. It follows the same two rules as binding: it **annotates, never removes**
(a concealed candidate can never earn the evidence that would clear it), and it is **data, not prose**.
It is deliberately **repo-scoped rather than surface-scoped**: a demotion learned here would unbind
the capability for every other repo, which is the wrong granularity for "broken against this one app".

**And a fifth input, which is NOT that one: the capability's own declared PRECONDITION**
(`applies_to: self | audited_repo | both`, plus named one-time repo facts —
`capability_advisor.CAPABILITY_PRECONDITIONS`). The distinction matters and the two must not merge. A
*contraindication* is a **recorded, per-(repo, capability) human judgement** — "broken against THIS
app" — and it ranks last within its partition because a recorded judgement is high-confidence. A
*precondition* is an **intrinsic, per-capability declaration evaluated per consult** — "acts on the
Orchestrator's own runtime", "needs an observable surface at all" — and it only annotates.
`switch-review` is Orchestrator-scoped for *every* audited repo, so expressing it as a
contraindication would mean a hand-written note in all thirteen repo records: an N×M table nobody
maintains. And the `frontend-verifier`-on-`Workflows` false positive happened *because* no note
existed, so a mechanism that requires someone to have written one cannot catch the case where nobody
did.
Three audit rounds on 2026-08-23 hit the same defect from both sides. `frontend-verifier` was offered
to two repositories with no application UI, its binding reason conditional — "when observable surfaces
exist" — in prose nothing read; and `capability:reference-sync-hygiene-test-gate` was filtered out as
not-applicable during an audit *of sync hygiene*, because it is scoped to this tool's runtime while
the audit target was another repo. `repo-audit:dimension-8` was the clearest case: four well-chosen
capabilities whose concepts transferred and whose instruments did not — eleven declines across the
three rounds, all of that one shape.

**But the axis ANNOTATES and changes neither the set nor the order, and that restraint is the
finding.** On a third repository — one that does have a display surface — `frontend-verifier` was
ready on its first `--doctor` call and produced that audit's highest evidence-to-effort finding, one
the code-reading path had missed, moving its propensity off the floor onto real positive evidence.
Down-weighting the binding on the two negatives alone would have cost that finding. So the axis turns
"investigate this offer to discover it cannot apply" into "dismiss it in one line", and nothing else:
a selftest asserts the returned list is identical, in membership and order, with the axis populated
and emptied. `evaluate_precondition` also hands back `suggested_decline_kind: precondition_unmet` —
the kind `capability_propensity` marks NON-demotable — so the two halves cannot disagree.

Verdicts are three-valued. Undeclared, an unnamed repository, and a repo fact whose checkout was not
supplied are all **not evaluated**, never failures: collapsing them into False would silently
reclassify the catalogue, and collapsing them into True would restore the original defect. An
unevaluated precondition NAMES its missing input (`repo_path`), because a condition nothing can even
attempt to check is what this replaces. The repo-fact probes return the markers they matched, so a
verdict is evidence that can be argued with rather than a heuristic's bare boolean.

**But naming the missing input is a diagnosis, and a diagnosis is not an instruction.** The axis went
in with `unevaluated_because` saying *"'observable_surface' is a one-time repo fact and needs
`repo_path`, a checkout to look at"* — and the sole caller kept consulting with neither `repository`
nor `repo_path`, so `frontend-verifier` accumulated four decline records all reading *"the binding's
own precondition is never evaluated"* while the declaration, the probe and both parameters existed and
worked. That is this workspace's runtime rule one level down: a gate must report its **drainable**
quantity beside its blocking one. So `advise()` now returns `precondition.missing_inputs` — the consult
inputs that would turn UNEVALUATED into a verdict, derived from the declarations through one
`PRECONDITION_INPUT_FOR` table so the remedy cannot drift from what is actually read — and
`precondition.how_to_evaluate`, the re-ask in words, printed loudly rather than left under `--json`.
It goes **empty** once the inputs are supplied, because a remedy that prints when nothing is missing
is noise a reader learns to skip.

**The same defect had a second instance, and it was pure delivery: `HOW_TO_USE` was read by
`format_advice` alone.** Every real consult arrives through the `capability_advice` MCP tool and
receives the result **dict**, which carried `entrypoint`, `blocker` and `next_step` and never this — so
a caller was offered `adversarial-review` with `blocker: "matched but a gate blocked invocation"` and
no gate NAMED, went and read the ledger row, found `{kind: closer_gate, name: high_stakes_review}` and
declined it as *"a lane gate, not an audit dimension"* — while the table held the direct call that
answers exactly that. `_attach_how_to_use` stamps it onto every entry on both answer branches, and
`format_advice` now reads it **from the entry**, one lookup, because reading the table twice is how the
render and the answer came apart. The field is always present and `None` when unknown: "no guidance
recorded" and "this answer does not carry the field" must not look alike.

**And the SURFACE itself has a state, because an invented name answered the wrong question
(2026-08-25).** A run opened with `--surface 'audit-implementation-run'`, a name nothing declares.
`binding_for` returned `{}`, the free text did not classify, and the answer came back
`bound_count: 0`, `useful: false`, `capabilities: []` — which reads as *"the advisor has nothing for
issue-filing work"*. It has three capabilities for exactly that, at `file-agent-issue`. The caller
acted on the wrong sentence and recorded nothing at all. That is silent absence in the advisor
itself, the same class as a binding with no caller. `capability_advisor.surface_status` answers it
in four values — `unspecified` / `declared` / `inherited` / `unknown` — from `known_surfaces()`,
derived from the same tables `binding_for` resolves against so a list of valid names cannot drift
from the names that actually resolve. `inherited` exists so a legitimate phase of a known surface
(`repo-audit:phase-9`) is not called invented, and `unspecified` so a caller who passed no surface is
not told they made one up. Like every other axis here it **annotates and changes neither the set nor
the order** — a selftest pins the two candidate lists identical — and, being a diagnosis, it carries
its remedy: the closest declared surface names.

**And a `null` that is a table gap must not read as a rule.** The Counter_Risk audit (2026-08-24)
saw `how_to_use: null` on every capability whose precondition had failed and concluded the answer
*suppresses* guidance on a failed precondition. It does not — the stamping above is unconditional on
both branches. The correlation was a coincidence of populations: the five `applies_to='self'` rows
were among the **29 of 39 bound capabilities the table had no entry for at all**. Per-entry `null`
cannot distinguish those two readings, and the wrong one was the reasonable inference, so `advise()`
now returns `guidance = {offered, documented, undocumented}` and the render states the cause in
words. *`2 of 5 documented`* is a gap in a table; nothing about it suggests a mechanism to go
looking for.

**The note and the guidance answer different questions, so they are declared in different places.**
`precondition_note` ends *"the concept may transfer; the instrument does not"* — an invitation, and
until 2026-08-24 an invitation with nothing behind it. The same audit accepted it for `feature-scan`,
transferred the concept by hand, produced two dimension-6 findings with it, and had to **reconstruct
what the capability's question even was from its name**. So `CAPABILITY_PRECONDITIONS` carries a
third key, `concept`: the question the capability asks, in words that name no repository.
`evaluate_precondition` returns it as `transferable_concept` and `format_advice` prints it as
`ASK IT BY HAND:` directly under the note it completes.

Two disciplines keep the pair honest, both enforced by `_selftest_how_to_use`. It rides the **scope**
mismatch only — a `requires` failure means the repository has no observable surface at all, so there
is no question left to transfer and offering one would rebuild the empty-invitation defect facing the
other way. And every `applies_to: self` row must declare **both** a `concept` and a `HOW_TO_USE`
entry: the first is for the audit that must ask the question by hand, the second for the consult
where the instrument does apply, and neither substitutes for the other.

**And the boundary belongs in that field as much as the call does.** Six `offload` declines in one
window were one sentence repeated — the work had to be first-person (run the code and read exit codes,
re-run a guard with the break in place, hold a whole grep trace, drive a browser). That is neither a
scope judgement nor a defect in the dispatcher; it is offload's intrinsic boundary, and it was written
down nowhere a caller could see. A capability that cannot say what it **cannot** take gets
investigated and declined once per surface, forever. The counter-rule holds here too: the boundary is
stated in the offer, and the binding is not narrowed — narrowing on structural declines is the
demotion path, and demoting the fleet's most-used capability would silence what should be explained.

**And the binding is DATA, not prose, deliberately.** The recursive loop below must be able to change
what a surface reaches for without rewriting that surface's prompt. `CLAUDE.md` §1 makes the manual
mirror sync "the only circuit breaker between an agent's change and the dispatcher that dispatches
those agents"; a loop that edits lane prompts is a self-modifying dispatch path. A surface's prompt
says *consult your bound set*; the bound set is a table.

### The recursive loop (both halves built)

Selection should improve where it should have been chosen and was not. Three detectable signals,
strongest first:

1. **The surface did the capability's work by hand.** Measured, not hypothetical: the opener performed
   `deliberate-break-verifier`'s exact break-then-revert contract in 271 of 2,445 rounds while never
   invoking it — and that practice appears nowhere in its instructions, only in its rolling memory.
2. **Named but not triggered, and the round went badly.** The control arm of every propensity
   experiment is exactly this candidate set; `influence_edges.counterfactual` already carries the column.
3. **Post-hoc failure attribution.** A verifier follow-up exists because merged work missed its own
   criteria → `runtime-ac-checks` should have run.

Implemented in `capability_propensity`: `hand_work()` scores signal 1 against a surface's own
records, `missed_selection()` reports all three, `propose_bindings()` / `propose_demotions()` emit
the actions, and `record_promotion()` writes a `binding_promotion` event that `binding_for()` reads —
so the loop closes as a **data change**, with no prompt rewritten. `detect` runs it across every
surface whose records resolve on this machine; the tick calls it REPORT-ONLY (`--apply` exists and is
deliberately not passed, matching how `feature_scan` is wired).

Signal 3 is consumed, not recomputed: `capability_matcher_proposals.evaluate()` already scores
"should work have been ROUTED here" against the Brain's run history and reports 6 capabilities across
379 runs of matching work never invoked. It was itself a built-and-forgotten module — working report,
no caller, no ledger row. Note its limit: `runs` has no surface column (`runs.source` holds only
keepalive / orchestrator_local / orchestrator_remote), so run history says a capability is under-used
OVERALL and cannot say which surface passed it over. That is why signal 1 exists and why a promotion
is never derived from run history alone.

### A VERDICT HAS A PROVENANCE, and the number is meaningless without it

Layer 2's first real corpus was **12 verdicts, 11 useful, from three audits** — and every one was
**self-assessed by the agent that chose to use the capability**, with all three audits run by the
same model under near-identical instructions. That is selection bias on top of correlated arms, which
`CLAUDE.md` §2 forbids treating as independent evidence, so 11/12 is almost certainly optimistic and
must never be presented as though it were not.

So `capability_propensity.VERDICT_PROVENANCE` declares, once, where a verdict came from and what it
may weigh, and `propensity()` **weights by it** instead of counting every verdict equally:

| provenance | weight | what it is |
|---|---|---|
| `outcome_corroborated` | 1.0 | a **named** outcome corroborates it (survived review, issue filed, fix landed and held) |
| `defect_found` | 1.0 | it surfaced a defect and the record names the artifact proving it |
| `machine_observed` | 0.6 | computed by code from the capability's own artifacts (the tick's finding-set diff) — nobody's opinion |
| `self_reported` | 0.25 | the agent that chose the capability also graded it |

Four disciplines make that honest rather than decorative:

1. **Outcome-derived outranks self-reported**, inheriting §2's un-gameable-label rule from route
   weights. The two strong classes **require** `corroboration` naming the outcome and are refused
   without it — an unnamed corroboration would make the top weight self-certifying, which is
   green-CI-alone under a new name.
2. **Correlated arms are represented, not assumed away.** Verdicts are grouped by
   `(judge arm, provenance)` and each group totals **1.0** however many verdicts it holds — the same
   reciprocal `relearn_quality` already applies to research arms, now consumed from
   `research_subjects.reciprocal_evidence_weights` by both, so there is one scheme and not two. Three
   same-model self-reports are worth 0.25 effective observations, not three; a verdict with **no**
   judge identity joins the one `unattributed` arm rather than being assumed independent.
3. **Down-weighted, never banned — but never DEFAULTED to either (2026-08-25).** Self-assessment is
   the only signal most capabilities have, so excluding it would empty the dataset; the gate would
   starve its own drain. It must still be *chosen*. `provenance` was optional and defaulted to
   `self_reported`, so an omitted flag filed outcome-backed evidence at 0.25 — and because the write
   is **idempotent** on `(capability, experiment)`, that choice was **irreversible**: a second,
   better-labelled record does not upgrade the first and does not double-count it either, it is
   simply dropped (`recorded: false`, exit 0). There is no partial remedy — which is why the
   countermeasure had to be a refusal at write time rather than a correction afterwards, and why a
   late-arriving outcome cannot strengthen a verdict already filed. Two of three independent
   implementation runs on 2026-08-25 hit
   it, with this capability's own `HOW_TO_USE` entry warning about the default in prose the whole
   time. `record_usefulness` and the `useful` CLI now REFUSE an unstated provenance
   (`unstated_provenance_refusal`, derived from `VERDICT_PROVENANCE` so the tiers offered are the
   tiers accepted). The refusal writes nothing, which is what keeps the retry the trial's *first*
   observation — a gate that consumed the experiment id would be the deadlock rather than the fix.
4. **An outcome that arrives LATE can still correct the verdict (2026-08-25).** The refusal above
   fixed the *silent* half of the problem and left the structural half: because the write is
   idempotent, the tier chosen at trigger time was permanent, and `outcome_corroborated` is by
   construction knowable only *after* the outcome. So the 1.0 tier was reachable only by
   capabilities whose outcome is immediate — `deliberate-break-verifier` earned it eight times
   because a break→revert finishes inside the same run, while `adversarial-review`'s findings sat at
   0.25 with their fixes merged the same morning and no way to say so. Ranking on that mixture
   measures **how fast an outcome arrives**, not how useful the capability is: the measuring window
   (verdict time) and the draining window (outcome time) were different windows, which is the
   latched-gate shape §CLAUDE.md names. `record_late_outcome` gives outcomes their own append-only
   channel onto an existing trial. Four properties make it evidence rather than a dial, and the
   first is the one that matters most: it is **symmetric** — `refutes` lowers a capability's measured
   usefulness on exactly the terms `corroborates` raises it, because an upgrade-only channel is a
   monotonic inflation ratchet, the same hazard this document flags for binding promotion. It is
   never self-assessed (`late_outcome_provenances()` excludes the self-assessed tiers, derived from
   the table so the offer cannot drift from the acceptance), never cheaper than the direct path
   (`corroboration` naming the outcome is required for every tier and direction), and one per trial
   with a **named refusal** on a second attempt rather than a silent drop. The original verdict is
   never mutated: it keeps its provenance and timestamp in the event log and the attachment sits
   beside it, so the record always shows both what was believed at trigger time and what the outcome
   established. `report()` prints `late_outcomes_corroborating`, `late_outcomes_refuting` and
   `late_outcomes_orphaned` together, because a corroborating count climbing while the refuting
   count stays at zero is the signature of a ratchet rather than a measurement.

5. **A decline has THREE possible subjects, not two (2026-08-25).** `DECLINE_KINDS` carried
   `demotable` (the binding is wrong) and `repairable` (the capability is wrong). There was no way
   to say *the binding is right, the capability is right, and the offer was too thin to judge* — so
   that case landed in `wrong_match`, which is **demotable**, making a bad offer into evidence
   against a good binding. The measured scale: **21 of 39 bound capabilities declare nothing at
   all** — no `HOW_TO_USE` entry and no precondition — so their offer is only their own name.
   `offer_too_thin` is the new kind and `offer_improvable` the new axis, declared on every row so it
   can never be merely absent. `propose_offer_improvements()` reports both populations: the
   structural one (declares nothing) and the observed one (a caller said so).

6. **One re-offer, and it may echo only DECLARED FACTS.** A decline can be caused by an offer that
   omitted something the tables already hold. `record_reoffer` supplies exactly those facts and
   nothing else — it cannot compose, re-rank or re-argue, because "offer it again, harder" is a
   persuasion loop and this document forbids that shape for binding promotion for the same reason.
   With no undelivered fact it **refuses**, and that refusal is the productive one: it means the
   offer is as good as the tables allow, so the fix is the tables. Only `offer_too_thin`,
   `wrong_match` and `precondition_unmet` are re-offerable; the rest state a structural reason the
   caller was entitled to give, and their answer is a different **task**, which
   `capability_task_proposals` derives from the same table. **Conversion is DERIVED** from the
   ledger (an invocation at or after the re-offer converted it) rather than reported, so a caller
   cannot flatter the mechanism by omitting its failures, and `report()` prints
   `reoffers_converted` beside `reoffers_declined_again` — a conversion count rising alone is a
   ratchet, not a measurement.

7. **The two-round rule must not latch.** A demotable decline of a re-offerable kind does not count
   toward demotion until its round has happened and the caller declined again. Nobody is *obliged*
   to re-offer, so waiting forever would hold every `wrong_match` decline shut: `REOFFER_GRACE_DAYS`
   is the drain, and `surface_decline_counts` reports `held_for_reoffer` so the hold is a number on
   a page rather than a silence. Measured on arrival: 19 declines held at `repo-audit:fix` alone.

8. **Witness overlap turns adjudication on (2026-08-29).** A five-repo partitioned review carried a
   fabricated finding and `synthesize`'s adjudication returned `not_needed` — the partitioner gave
   every assertion exactly one witness, so the cross-partition machinery (which joins findings on
   `assertion_key`) had nothing to compare. `prepare --overlap N --witness-agents a,b` now emits N
   witness copies per partition: `item_id` suffixed (global uniqueness), `assertion_key` kept (the
   join), each witness carrying its own **distinct** agent — same-agent witnesses are refused
   because two runs of one model are one correlated arm wearing two ids. Agreeing witnesses
   corroborate; disagreeing ones conflict, which is the only mechanism that could have caught the
   fabrication.

9. **`status_shadow` is a decline kind of its own (2026-08-29).** `gated_off` was absorbing
   "status: shadow", a lifecycle *permission* — and no environment flag gates any `role-*`
   capability, so the task proposer prescribed gate-satisfying work for capabilities with no gate.
   Shadow gates *acting* on output, not producing it, so the kind's task shape is actionable: run
   it advisorily and score the advice. Not demotable, offer-improvable, and re-offerable — the
   echoable fact is the boundary line saying shadow permits advisory invocation, whose absence is
   what produced the measured declines.

   The read path is unchanged and still classifies an unlabelled pre-provenance row as
   `self_reported`, because that is what such a row honestly is: `PROVENANCE_DEFAULT` answers "how do
   I read silence", `PROVENANCE_UNSTATED` answers "may I write it", and sharing one constant is what
   made them look like one question.
4. **The reporting requirement is as load-bearing as the arithmetic.** `propensity()` returns the
   provenance mix, the independent-arm count, the self-reported share and the raw count beside the
   weighted one; `rank()` hands all of it to the **caller**; `report()` states the corpus mix in the
   headline. On the live ledger that headline reads *12 verdicts, 12 self_reported, 0
   outcome-derived, 0 capabilities with >1 judge arm* — and the three capabilities that had shown
   0.800 now show 0.556, which is what three correlated opinions are worth.

Two axes, never collapsed: `verdict_provenance` is *where it came from*; `verdict_kind` (e.g.
`observer_output_change`) is *which question was answered*. §2 forbids averaging across the kinds, so
a mixed-kind posterior is **flagged** on the row rather than silently blended.

**The counterfactual was already there, and nothing was added for it.**
`influence_edges.counterfactual` is the **delivery** counterfactual, keyed on `(capability, run)`,
and `capability_effectiveness` already computes `durable_rate(accepted)` against
`durable_rate(counterfactual)` from it — an advisory consult is not a run, so it cannot carry a
per-verdict comparison here. This module's counterfactual is `experiments()`'s control arm: the
candidates named for the exact same task and not triggered. `propensity()` now reports that arm
beside the posterior and never mixes it in.

### The REPAIR channel — the loop's third action

Promote and demote were the only two actions, so the loop **could not represent "this capability is
worth having and is broken."** The only available response to a broken capability was to stop
offering it, which silences the thing that should be fixed and loses a capability worth keeping.

**The live case.** `repo-playbook` sits at one useful and one not-useful verdict, and the
Fine-Art-Archive audit documented *why*: its useful content is gated behind
`task_type: implement/testgen/mechanical`, so a `review` consult receives 308 characters, one clause
of which is factually wrong — it tells auditors a repository's default branch is something it is not.
Demotion silences that. A repair proposal names it, with the words attached.

`capability_propensity.propose_repair` reads two inputs:

1. **`not_useful` verdicts, with their evidence carried forward.** That is the whole difference
   between a flag and a repair: *"0.5, one bad verdict"* is a number, *"308 characters, one clause
   factually wrong about the default branch"* is an action.
2. **Declines whose KIND indicates a defect** — `decline_kind_repairable`: `wrong_match` (the matcher
   may be wrong) and `precondition_unmet`. Explicitly **not** `no_landing_zone`: nobody's fault, the
   match was correct, the capability is working; proposing a repair there asserts a defect that does
   not exist. `scope_too_small` is also excluded, and for an arithmetic reason rather than a
   judgement — its fix is narrowing the *declaration*, which **is** the demotion path, and one
   decline must not argue for unbinding and rebuilding at once.

**`repairable` is a second property of the kind**, declared once beside `demotable` and read by one
lookup. They are independent questions, and the pair that proves it is `precondition_unmet`: **not
demotable, is repairable.** Before this channel existed it therefore had *no action at all* — 11 of
them on the live ledger, recorded and inert forever.

**Report-only, and it queues nothing for anyone** (`CLAUDE.md` §3). Proposals are a field in a report
the cadence step already writes: nothing waits on a human, nothing expires against a human, and no
human action can fall behind. Attention cost: 13 rows in an existing report, zero actions required,
expiring on their own with `WINDOW_DAYS` — **0 minutes/week**.

#### Latched-gate answers (a proposal set is a gate, so it owes all three)

1. **What decrements it?** `record_repair` — a named mechanism writing a durable marker with the fix
   and its artifact, after which a proposal counts only defect evidence **newer than that marker**.
   Not "time passes", not "someone notices". Window expiry is a *second* drain on the same
   `WINDOW_DAYS` constant. *The first draft had no marker at all: defect evidence stayed in the
   90-day window, so fixing the capability did not clear its proposal for three months. That is the
   latch, and asking question 1 — not testing — is what caught it.*
2. **Can that mechanism run while the gate is non-empty?** Yes, unconditionally. `record_repair`
   requires nothing a standing proposal forbids, and a proposal is report-only on both sides: it
   never withholds the capability from `rank()`, never lowers its propensity, never blocks a consult.
   The capability keeps being offered and keeps earning verdicts while the proposal stands.
3. **Does the measuring window equal the draining window?** Yes, by construction — `WINDOW_DAYS`,
   the one constant `usefulness()`, `propensity()` and `surface_decline_counts()` already share,
   bounds both the defect evidence counted and the repair markers that clear it.

Runtime rule: every proposal carries `defect_evidence_total` (measuring),
`defect_evidence_since_repair` (blocking) and `repairs_recorded` (drainable), and `report()` carries
`repairs_recorded` **even when the proposal list is empty** — an empty list cannot say whether
anything is accumulating, and "0 proposals, 0 repairs ever recorded" reads nothing like "0 proposals,
6 repairs recorded".

And the tie-break **fails toward motion**: ledger timestamps are second-granular, so the freshness
test is `>=`, not `>`. A defect recorded in the same second as a repair is unorderable and must
*re-open* the proposal (one report line) rather than vanish (the finding).

### A FIND has a finder, and the finder may be a capability OR a surface

The strongest signal this loop produced on 2026-08-23 was not in the dataset. Instrumented work
found **seven defects in the system's own code** that its author had not found. Two were attributable
to a capability and *were* recorded — `adversarial-review` supplying citations that became the
strongest facts in two issue bodies, `deliberate-break-verifier` catching an auditor's own
methodological error. The other **five were found by the process**: an audit noticing that a
suppressed surface still offered capabilities; an agent reading `capability_propensity` and finding a
branch that recorded nothing. Those had no capability to attribute to, so they became PRs and prose
and taught the loop nothing at all.

`capability_propensity.record_find` closes that, with the finder as a first-class field:

| finder | feeds | how |
|---|---|---|
| a **capability** (with the `experiment_id` it was offered under) | that capability's **usefulness** | a verdict at `defect_found` provenance, weight 1.0, whose `corroboration` is the artifact — a defect found is an outcome, not an opinion |
| a **surface** | **binding quality** | `binding_quality(surface)` — offers, triggers, declines *and finds* for that surface. There was nowhere to put this before |

**No new store and no new event type.** A find rides on a `match` event tagged
`source=capability_find`, exactly as a decline and a binding promotion already do. Its ref is
`find:<digest>` and **not** `advice:<digest>`, so `_experiment_id()` returns None for it and
`experiments()` / `usefulness()` / `propensity()` cannot see it at all. That separation is
**structural, not conventional**: no metadata a caller could set would make a find record reach a
posterior. The only path from a find to the posterior is `record_usefulness` at `defect_found`
provenance.

**And it must not become a way to inflate a capability's standing.** `defect` and `artifact` are both
required and refused when blank — a *claimed* find with no artifact is worth nothing, the same rule
that refuses an unevidenced verdict and an unexplained decline. The binding guard, though, is the
correlated-arm discount from the section above: **ten artifact-backed finds from one judge arm total
one observation**, so the number does not move past 0.667 however many are recorded; only an
independent arm moves it. Measured on the break: removing the discount takes ten same-arm finds from
0.667 to 0.917, which is exactly the inflation this is built not to allow.

`binding_quality()` is **report-only**. `propose_bindings` and `propose_demotions` keep their
existing external evidence rules untouched — a number about a *surface* must not become selection
pressure on a *capability*, which is the ratchet the detection loop already refuses.

### Where layer 2's evidence comes from

Layer 2 needs resolved trials, and until 2026-08-22 nothing produced any: `advise()` recorded the
`match` edge, and the `invocation`/`outcome` edges had no production caller at all, so every
propensity was the prior and the cadence step said so on every run. **The tick is now that
producer** (`capability_propensity.py tick-evidence`, every tick, below the heartbeat export and
below the four steps it grades) — chosen because it is the highest-volume unattended surface, so
coverage accrues hourly with no further attention.

An observer's verdict is **not** a delivery verdict — a cadence report can never merge a PR, and
demanding one is the category error that parked eight capabilities in a measurement gap they could
not leave. For the tick-bound capabilities that `capabilities.is_observer()` confirms, *helped* means
**its report's finding set changed since its own previous run**: a defect newly reported, a
regression flagged, a switch verdict that moved, a finding resolved. Re-emitting an identical finding
set is *not* useful, and an empty set that stays empty is explicitly not useful — silence is not
usefulness. Capabilities the observer test does not confirm record that they ran and get no verdict,
because averaging an output-change question with a delivery question would violate the never-average
rule two paragraphs up.

The bounding is a correctness requirement, not a nicety: 24 ticks a day over four bound capabilities
is 96 potential data points, and a verdict written on every run would make the ranking measure the
cadence. Two independent bounds — the experiment id is scoped to the UTC day, so the ledger's
idempotency keys admit at most one verdict per capability per day whatever happens; and a verdict
additionally requires that capability's own cadence artifact to have been regenerated since the last
evaluation, which ties one verdict to one production and bounds the graded rate to ~1.3/day. The
finding projection keeps identity and verdict fields only, because `overdue`'s `silent_days` rises
daily on its own and hashing a row whole would score the monitor useful on every run it will ever
make.

### A DECLARED BINDING WITH NO CALLER IS THE SAME DEFECT AS NO BINDING

Layer 1 is offered to a surface *by that surface's own consult*. So a surface nothing consults is a
table entry that can never be selected, can never earn evidence, and can never be ranked — the gate
starving its own drain, one level down from the concealment rule above. Measured 2026-08-23: **22 of
43 capabilities were bound to NO surface at all**, and two whole surfaces (`ci`, and every phase of
the tick) had bindings with no caller.

Three callers close that, and the last two are the same mechanism as the first — `advise()` plus the
`match` heartbeat, never a second one:

| Surface | Caller | What bounds it |
|---|---|---|
| `tick` (4 capabilities) | `capability_propensity.tick_evidence` (PR #37) | one verdict per capability per UTC day, gated on artifact regeneration → ~1.3/day |
| `tick:<phase>` (14 capabilities) | `capability_advisor.py --consult-tick-phases`, at `ORCH-ANCHOR: tick-phase-consult` | consult text stable per (surface, UTC day); the match heartbeat is idempotent on its digest → 34 events on the first tick of a day, 0 on the other 23. **No verdicts at all**, so #37's ceiling is untouched |
| `ci` (3 capabilities) | `verify.py`'s `ci_consult_line()` — it runs on every PR and already executes the admission gate | `record=False`: a verifier must not write to the ledger its own gates read |

**The tick is sub-surfaced for exactly the reason `repo-audit` is.** 18 of the 43 capabilities live
on the tick; binding all 18 to `tick` would rebuild the too-many-tools condition inside the tick.
The five phases — `tick:capacity`, `tick:dispatch`, `tick:experiments`, `tick:redirect`,
`tick:learning` — are the tick's OWN names, taken from `orchestrate.sh`'s first line ("capacity ->
discover -> plan -> dispatch"), its `--- Learning cadence ---` heading and its `[cadence] redirect
…` / `[cadence] experiment follow-up` blocks; most of the capabilities bound below carry a
`{"kind": "tick_phase", "name": …}` matcher naming the very phase they land in. Each phase resolves
to 6–8 rather than 18.

**The bare `tick` set does not move, and that is a constraint rather than a preference.**
`capability_propensity.TICK_SURFACE` is `"tick"`, `tick_evidence()` grades exactly
`binding_for("tick")`, and its selftest requires every capability with a `TICK_FINDING_FIELDS`
projection to be in that set. Moving those four into a phase would silently zero the only producer
of layer-2 evidence in the system — so the phases ADD, and the phase contexts inherit the four
surface-wide observers for the same reason `repo-audit` declares `offload` surface-wide.

**And a capability no surface may offer says so.** `local-model-profile-trial` is the one ledger row
that is deliberately unbound — the quarantine-only trial transport — and it is declared with
`NO_BINDING` and its reason rather than left absent, because silent absence and deliberate emptiness
must not look alike. The `ci` consult line reports the pair on every PR: rows bound to some surface,
beside rows bound to none.

**Demotion is the drain.** Bindings that could only grow end with every surface holding all 43 —
the exact condition binding prevents. Two rules propose removal, and they read **disjoint
populations**: `never_triggered` counts offers where nothing was said (`not_triggered_silently`)
across `DEMOTION_MIN_TRIALS`; `declined_with_reason` counts *demotable* declines across
`DEMOTION_MIN_DECLINES`, a much lower floor because a stated reason is much better evidence. A single
trigger at that surface disqualifies both — something actually used there is not a demotion candidate
however often it is passed over. The disjointness is not tidiness: counting declines as silent offers
lets an honest decline of a *correct* match trip the rule meant for capabilities nobody spoke about.

**A DECLINE IS A THIRD STATE, NOT A NEGATIVE OUTCOME** (`capability_propensity.record_decline`, CLI
`decline`, MCP `capability_decline`). Two independent audit rounds on 2026-08-23 reached the same
finding: propensity carried information exactly once, because most candidates sat at the
uninformative prior, and the missing input was not more consults but reasoned rejections — a
capability declined on repo-specific grounds looked identical in the ledger to one nobody ever
considered. So `triggered` / `declined` / `not_triggered_silently` now partition the candidate set,
and *never considered* is the fourth case of not being a candidate at all.

The discipline that makes this safe is a separation, not a convention. A decline means the capability
did NOT run, so recording it as an `outcome` would bucket it into `not_useful` — asserting we tried
it and it did not help, about something that never executed, corrupting the one signal declines
exist to sharpen. A decline is therefore carried on a `match` event (it genuinely *was* offered)
tagged `source=capability_decline`, and `usefulness()` reads `outcome` events only. There is no code
path from a decline to the posterior. `propensity()` reports the decline count **beside** the
posterior for exactly this reason: "prior, no evidence" and "prior, no evidence, four reasoned
rejections" are opposite readings that were previously identical. A decline requires a reason and is
refused without one, the same way `record_usefulness` refuses an unevidenced verdict.

`detect` enumerates every surface that has either a declaration or evidence, not only the declared
keys: `repo-audit:dimension-1` has no table entry of its own — it inherits `offload` surface-wide —
so three independent audits declining `offload` there were recorded and never read. A drain that
cannot see a surface cannot drain it, and the surfaces most likely to be over-bound are exactly the
ones that only inherit.

Attribution is on the event: the advisor records the `surface` on each `match`, because it recorded
only `skill` before and the CLI has no `--skill` flag — so every `--surface` consult wrote
`skill: null` and its whole control arm was unattributable to the surface that produced it.

**And a decline has a KIND, because the kinds imply opposite corrections.** One undifferentiated
"declined" column licenses the wrong fix. A third audit round on 2026-08-23 separated them and the
separation is the finding: `testgen-lane` matched **correctly** three times in a read-only audit and
was structurally impossible every time (no commit target), while `offload` was declined at 9 of 12
surfaces because it is declared surface-wide and a one-subsystem audit has nothing big enough to hand
off. The first calls for no change at all; the second calls for a precondition or a narrower
declaration. And `frontend-verifier` — declined on two frontend-less repos, then the
second-strongest finding of an audit on a repo that *does* have a display surface — is the same
lesson from the other side: two negatives are not a verdict on a binding.

So `demotable` is a property of the **kind**, declared once in `DECLINE_KINDS` and read by exactly
one lookup — as is `repairable`, its independent twin (see the repair channel below).
`wrong_match` and `scope_too_small` may demote; `precondition_unmet`, `no_landing_zone`,
`gated_off`, `deferred` and the `unspecified` default may not — they are counted and reported, and
cannot clear the floor. `precondition_unmet` is the load-bearing one: the correct response to a
capability whose condition does not hold here is to **evaluate the condition, not to weaken the
binding**, which is why it is recorded and inert. An unknown kind is refused rather than coerced,
because a typo silently becoming `unspecified` would discard the classification the caller believed
it had made.

First live run found a real gap: `deliberate-break-verifier` showed 69 hand-done instances in 1,765
closer rounds while bound only to the opener, and the loop promoted it. **It must not ratchet:** raising selection pressure whenever a capability was not chosen,
while "should have been chosen" is partly derived from that capability's own advocacy, optimises the
measured number rather than usefulness. Promotion is therefore gated on an *external* signal (1 or 3
above), never on the advisor's own naming.

### A capability can be one output FORMAT away from being the right tool

The same audit reached for `deliberate-break-verifier` at `repo-audit:phase-4`, called it *"a
genuinely close match to what I did by hand"*, and ran the break-then-revert itself anyway. The
reason was not a capability mismatch: `AGENT_ISSUE_FORMAT` requires a named test gate with the raw
before/after console output **quoted verbatim** into the issue body, and `local_verify.verify()`
returns a structured verdict whose console output is JSON-escaped inside it. Every audit on record
has re-run the same proof by hand for that reason — a **packaging** mismatch, and the cheapest kind
of finding to act on.

`local_verify.break_transcript()` renders the two halves the result already holds (`red`, `green`)
as the quotable block, and `--transcript` prints it. It captures nothing new and changes no verdict,
no exit code and no consumer: the exit code is computed above the rendering choice, because a
rendering flag that could move a gate would make the artifact and the gate two different answers to
the same question.

**A quotable artifact must not overstate, because its caveats do not travel with it.** So every
caveat is stated *inside* the block: the hollow nodes PR #114 named, an `INDETERMINATE` per-node
pass, and one new guard. `--test-path` takes files and directories, so a pytest **node id** is
silently not copied into the base tree; the base then runs without that test, the command fails with
*"file or directory not found"*, and the rolled-up verdict is `PASS` — red because the test was
**absent**, not because it **failed**. `uncopied_test_paths()` detects exactly that and the
transcript leads with `THIS IS NOT A VALID DEMONSTRATION`. Reported, never gated: the verdict is
deliberately unchanged, and a selftest pins that it is.

**And a second cause of the same banner, from the opposite direction (2026-08-25): the overlay can
carry the FIX.** The overlay is meant to add the candidate tests and nothing else — but when the fix
itself lives *in test files*, the default `--test-path` scope ("every changed test file") is every
changed file, so the base tree after the overlay is identical to the worktree in every file that
differs. RED and GREEN then run the same code. Measured on Counter_Risk #964, where scoping
`--test-path` to only the new module was load-bearing and had to be known in advance; the run
otherwise reports `FAIL_HOLLOW` with every candidate node named as a tautology — a confident
statement about the TESTS, and a false one, whose fix is the opposite of the one it implies.
`overlay_covers_every_change()` is the exact condition rather than a heuristic about which files look
like tests, and it is conservative: it fires only when *nothing* is left uncovered, so the correct
usage can never trip it. Both causes now come from ONE predicate,
`local_verify.invalid_demonstration()`, consumed by the transcript **and** by the result dict — the
JSON consumer is the one being misled, and until this the finding reached only the rendered text,
which is the delivery defect `how_to_use` already paid for once.

## Gate 2 — the usability review panel (`ux_review.py`, built 2026-06-22)

Frontend work has two gates. **Gate 1** is `frontend_verify` (a deterministic rail: assert→click→assert
on the accessibility tree — *does the control do what it claims*). **Gate 2** is `ux_review` — an
evidence-bound *usability* review by an anonymized panel of ≥4 evaluator backends plus an adversarial
critic, scoring four dimensions (`wired` / `usability` / `help_clarity` / `workflow_productivity`) where
every sub-8 score must cite screen + click-path + expected-vs-actual (no abstract findings).

Per the rule above — *an LLM review panel is a **supplement** to a gate, never a replacement* — the
**panel is LLM judgment, not a rail.** The **rail is the deterministic `ux_review.gate_decision`**, which
marks a frontend "done" only when Gate 1 passed AND the panel's `overall_median ≥ threshold` AND there is
no severity-4 blocker. The panel reuses `exp_abcd`'s anonymized-evaluator machinery (`_eval_command`,
`_ensure_min_evaluators`, `_extract_json`) and launches with `dispatcher._net_hygiene_prelude()`
(proxy-scrubbed to match the fleet's clean env).

It is a **new feedback *source*, not a new surface**: it writes the existing `feedback.evaluations`
(per-evaluator UX scores), `evidence_gaps` (the self-evolving "what evidence did I lack?" growth layer),
and `human_calibration` (the weekly owner spot-check that anchors the panel). `ux_review.cross_repo_patterns()`
queries `evaluations` to surface a flaw recurring across apps as a prior for the next review. Evaluator
disagreement (score spread ≥3, or a contested severity-3/4 finding) is **flagged → routed to human
calibration**, never averaged away — so "the panel agreed" can't masquerade as "the panel was right."

**The score is not the deliverable — the improvements are.** `ux_review.synthesize_improvements(report)`
mines the panel for *how to make it better*: it preserves every distinct per-evaluator `fix_hint`
behind each corroborated finding (the merge used to keep only one) and ranks them by
severity×corroboration, and it surfaces the unioned `evidence_gaps` as the **coverage to drive next
pass**. Two operational disciplines (in the `/ux-review` skill, not the orchestrator rails) keep this
honest: (1) a **full-coverage pass** — drive *every* primary surface, recording a `coverage` ledger in
the bundle so a happy-path-only review can't post a falsely clean score; (2) a **diff-anchored in-repo
`docs/ux-review/REVIEW_LOG.md`** — each run records the reviewed commit SHA + coverage + finding
dispositions, so the next run `git diff`s that SHA→HEAD and concentrates on new + likely-affected
functionality. These extend the existing component; they add no new rail, role, or `feedback.py` surface.

## RedirectAgent — the first role (built 2026-06-19)

The highest-value autonomy gap is closed-loop monitor→redirect: `ORCHESTRATOR.md` names redirect "your
defining skill / the thing a deterministic dispatcher cannot do," its absence compounds (a drifting
unattended agent isn't caught until a bad outcome hours later), and the scaffolding already existed
(`watch.py` → `policy_decision` + `redirect_plan.py` dry-run/apply). So redirect is the first amber box.

- **Contract.** Input `{report (a watch.py report), acceptance_criteria[, attempt_history]}` →
  `{action ∈ wait|collect|inspect|redirect|decompose, reason, confidence, corrected_prompt, switch_agent}`.
- **Routing.** `route_as="review"` (prior only), `eligible_backends={gemini, codex, cursor, claude}`,
  claude reserved.
- **Shadow only — never mutates.** It proposes into `redirect_plan.plan()` by injecting its decision as
  `report["policy_decision"]` and passing its authored prompt via the new `prompt_override` param. The
  existing `redirect_plan.py --apply --confirm-target` remains the **single human/seat-gated mutation
  path**. An invalid proposal is rejected and falls back to the deterministic `redirect_policy.decide`
  baseline.
- **Rollout discipline (do not skip).** advisor → measure proposal quality vs. outcomes → *only then*
  autonomous action. `redirect_shadow.py` is the measurement layer: it records real RedirectAgent
  proposals against the deterministic baseline, links accepted/applied advice to downstream outcomes, and
  reports `ready_for_supervised_apply`. Historical keepalive-shadow rows may identify replay candidates,
  but are not proposal evidence until rerun as fresh/blinded RedirectAgent proposals and outcome-linked.
  This is the same shadow → supervised → live ramp used for cron activation. The diagram shows the target
  architecture; the amber boxes light up one at a time, redirect first.

### CLI

```bash
python3 src/roles.py --selftest                       # offline contract checks
python3 src/roles.py route --role redirect            # show the router-chosen backend
python3 src/roles.py redirect --report-json r.json --ac "<acceptance criteria>" \
    [--proposal-json p.json]   # replay a captured proposal (offline)
python3 src/roles.py redirect --report-json r.json --ac "..." --dispatch   # live offload to the backend
python3 src/redirect_shadow.py record --report-json r.json --ac "..." --dispatch
python3 src/redirect_shadow.py summarize
python3 src/redirect_shadow.py historical-candidates
python3 src/redirect_shadow.py link-outcome --role-run-id RID --influenced-run-id DOWNSTREAM_RID
python3 src/roles.py link-outcome --role-run-id RID --influenced-run-id DOWNSTREAM_RID
```

All `redirect` invocations print a dry-run plan and a SHADOW banner; none mutate state. Live dispatches
also return `role_run_id` plus the backend offload `run_id`, and an accepted proposal's plan carries that
id on its `delegate-retry` argv (`--influenced-by-role-run-id`) so the downstream run stamps itself.

**Applying is machine-authorised, not reviewed** (`redirect_apply.py`, 2026-08-21). `redirect_plan.apply_plan`
had no caller at all, and the Stage-2 gate that would authorise one (`ready_for_supervised_apply`) counts
only *applied* advice — `join_role_to_outcome` returns `synced=False` for unaccepted links and historical
replay links are deliberately `not_role_learning=True` — so the gate required ten applied outcomes before
anything could apply. `redirect_apply.py` breaks that deadlock at both ends: `link_applied_outcomes()`
turns each applied redirect's own influence edge into the corpus link automatically, and a default-OFF,
self-disabling bootstrap (`ORCH_REDIRECT_APPLY_BOOTSTRAP`) applies at most one authorised plan per day.
Authorisation is a pure function of recorded state — dead prior process, no foreign claim, lineage stamp
present, gate deficit still open, per-target and per-day bounds — never an owner review queue.

## PromptAgent — the second role (built 2026-06-20)

PromptAgent upgrades generic delegation templates without changing deterministic selection. It turns
`{target, goal, task_type, lane, context}` into a strict JSON prompt proposal containing a standalone
`scoped_prompt`, `definition_of_done`, acceptance criteria, validation, expected paths, out-of-scope
boundaries, risks, and confidence.

- **Routing.** `route_as="implement"` (prior only), `eligible_backends={gemini, codex, cursor, claude, vibe}`,
  claude reserved by default through `route_role()`.
- **Shadow only — never delegates.** It returns a dispatch-ready prompt string for the orchestrator to
  inspect. It does not call `dispatcher.delegate`, write claims, label PRs, create branches, or open PRs.
- **Rail preservation.** The output `task_type` must match the deterministic rail-selected input
  `task_type`; PromptAgent may not reclassify work or replace `router.select_agent`.
- **Validation.** The role rejects missing DoD/AC/validation, task-type mismatch, agent persona leakage, and
  duplicated repo-playbook text because dispatcher injects persona and approved repo context.

### CLI

```bash
python3 src/roles.py route --role prompt
python3 src/roles.py prompt --target owner/repo#N --goal "..." --task-type implement \
  --target-detail "issue body or PR context" [--proposal-json p.json]
python3 src/roles.py prompt --target owner/repo#N --goal "..." --task-type implement --dispatch
```

## DecomposerAgent — the third role (built 2026-06-20)

DecomposerAgent upgrades the epic planning lane into a callable role. It turns a large/vague goal into an
`epic_lane.py` plan: epic metadata, dispatchable subtasks, dependencies, integration order, final
verification, and re-decomposition triggers.

- **Routing.** `route_as="epic"` (prior only), `eligible_backends={gemini, codex, cursor, vibe}`,
  matching the existing epic-lane prior while learning `role:decomposer` separately.
- **Shadow only — never dispatches.** It returns validated `dispatch_prompts` for the orchestrator to
  inspect. It does not call `dispatcher.delegate`, write claims, label PRs, create branches, or open PRs.
- **Validation.** The role reuses `epic_lane.validate_plan()` and `epic_lane.build_dispatch_prompts()` so
  the CLI lane and role lane cannot drift. Invalid proposals fall back to the deterministic planner prompt
  only; no dummy dispatchable plan is emitted.

### CLI

```bash
python3 src/roles.py route --role decomposer
python3 src/roles.py decompose --goal "..." --repo owner/repo --target owner/repo#N \
  [--subtask-count 3] [--proposal-json plan.json]
python3 src/roles.py decompose --goal "..." --repo owner/repo --dispatch
```

## TriageAgent — the fourth role (built 2026-06-20)

TriageAgent upgrades the backlog worth-it pass into a callable role. It turns a discovered backlog snapshot
into advisory recommendations: work now, defer, needs scope, skip, monitor, and optional logical batches.

- **Routing.** `route_as="review"` (prior only), `eligible_backends={cursor, vibe, gemini, codex, claude}`,
  with claude reserved by default through `route_role()`.
- **Shadow only — never selects workers or mutates backlog state.** It does not call `router.select_agent`,
  `dispatcher.delegate`, claims, label mutation, branch creation, or PR actions. Router capacity, claims,
  task type, lane, and worker selection remain deterministic rails.
- **Validation.** The role requires exactly one recommendation for each visible target, rejects unknown or
  duplicate targets, rejects unregistered batch IDs, forbids extra recommendation/batch keys that could
  smuggle worker selection or task reclassification, and falls back to deterministic backlog order when a
  proposal is invalid.
- **Input quality.** `backlog.py` now retains issue/PR `body` text so triage can judge underspecification
  from more than titles and labels.

### CLI

```bash
python3 src/roles.py route --role triage
python3 src/roles.py triage --backlog-json ~/.codex/handoff/backlog.json [--proposal-json triage.json]
python3 src/roles.py triage --backlog-json ~/.codex/handoff/backlog.json --dispatch
```

## AdjudicatorAgent — the fifth role (built 2026-06-20)

AdjudicatorAgent upgrades disputed-reviewer handling into a callable role. It reviews one blocker/veto
against supplied ground-truth evidence and advises whether to uphold it, reject it, or gather more
evidence.

- **Routing.** `route_as="review"` (prior only), `eligible_backends={gemini, codex, claude}`. Claude is
  reserved by default through `route_role()`, so routine shadow adjudication starts with Gemini/Codex.
- **Shadow only — never emits terminal verifier verdicts or mutates.** It does not produce `PASS`, `FAIL`,
  `BLOCKED`, or `verifier_verdict`, and it does not call merge/label/claim/delegate paths.
- **Rail preservation.** `runtime_ac_panel.adjudicate_panel()` and `adversarial.aggregate_veto()` remain the
  deterministic aggregation math. Automated gate failures still block through the gate/merge rails; an
  adjudicator recommendation is evidence for the orchestrator/human to inspect, not an override.
- **Validation.** The role rejects terminal/mutating keys, requires cited ground-truth refs when upholding
  or rejecting a blocker, requires evidence gaps for `needs_more_evidence`, and rejects next steps that ask
  for mutating execution.

### CLI

```bash
python3 src/roles.py route --role adjudicator
python3 src/roles.py adjudicate --case-json case.json [--proposal-json adjudication.json]
python3 src/roles.py adjudicate --case-json case.json --dispatch
```
