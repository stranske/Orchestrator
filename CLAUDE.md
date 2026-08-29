# CLAUDE.md — Orchestrator agent rules

> **Local vs public.** This repository is the TOOL: generic capabilities, gates and tests. This
> instance's EVIDENCE is not committed — the failure history, spend calibration and attention-budget
> figures live in `LOCAL_POLICY.md`, `CAPABILITY_USEFULNESS.md`, `*.local.md` and
> `Code/Audits/Orchestrator/`, all gitignored. Where a rule below cites a figure, read it from
> `LOCAL_POLICY.md`. Runtime state and durable evidence live OUTSIDE the tree, behind two variables:
> `$ORCH_LOCAL_RUNTIME` (the Brain, the capability ledger, and the **improvement log** — reached with
> `improvement_log.py`, never by path) and `$ORCH_STATE_DIR` (the audit cache, cadence stamps,
> monitors, worktrees). Both default to `~/.codex/orchestrator`, so a second instance needs different
> values and no code change.
>
> **Gitignored is not the same as machine-local, and confusing the two hid a rule from its own
> workers.** A gitignored file does not exist in a git WORKTREE, and agents work in worktrees — so
> the improvement log, which §0 and §5 both require, was unreachable by every worker those rules
> bind. Evidence that agents must READ belongs outside the tree behind an accessor, not gitignored
> inside it. When you add such a thing, leave a tracked pointer naming the accessor.

Read `README.md` first for what this project is and its important functionality. This file is the
rules for anyone (human or agent) *changing* it.

## −2. REQUIRED READING BEFORE ANY WORK HERE: `ARCHITECTURE.md`

**Read `ARCHITECTURE.md` and look at `orchestrator-loop.svg` before starting work in this tree — not
only before proposing a design change.** §5 below already makes them a contract you must UPDATE; this
rule makes them something you must READ first, because the doc defines the vocabulary every other
rule in this file uses, and reasoning about this system without it produces confident, wrong answers.
That has happened: a full session's work was planned and executed here while treating "capability" as
the system's core noun, when `ARCHITECTURE.md`'s taxonomy is rails / roles / delegated sub-agents /
gates / feedback surfaces — and "capability" is a different axis entirely.

**The vocabulary, so it stays regular:**

- **Rail** — deterministic code. Same inputs, same behaviour, no model call. `router` (selection),
  `claims`, `capacity`, `provision`, `dispatcher`, `adapters`, `feedback`, and the gates. Determinism
  is the safety/auditability property; it is why *selection stays a rail* (agentifying it would blur
  the credit assignment the learner depends on).
- **Role** — a slot where an LLM makes a judgment, behind a typed contract
  (`Role(name, route_as, eligible_backends, mode, build_prompt, validate)`) whose **model is
  swappable and router-chosen**. Redirect, decompose, triage, prompt-authoring, adjudication.
- **Capability** — **what a tool in the Orchestrator DOES.** This is the unit of accounting, and it
  is orthogonal to rail/role: rail-vs-role says how a thing is IMPLEMENTED, capability says what it
  is FOR. One capability routinely spans both (`adversarial-review` is role judgment invoked by a
  rail gate and recorded over a rail acceptance edge).
- **The admission parts are not the definition of a capability.** A caller, heartbeat, outcome path,
  fixture, kill switch, rollback, expiry, dedup finding and a surface that can offer it are the
  components that must be present for a capability to WORK WITH THIS SYSTEM — to be invocable,
  observable, findable and improvable. Do not describe a capability by its admission parts; describe
  it by what it does, then check the parts.

**Two kinds of capability, and their measurement stories differ — do not average across them:**

1. **Workflow capabilities** run implementation code when invoked (`testgen_lane.py`,
   `local_verify.py`, `runtime_ac_gate.py`, `partitioned_review.py`). Success is a definable
   condition, so effectiveness is a pass/fail rate on a stated task.
2. **Sub-agent capabilities** spin out a bounded, goal-scoped agent whose backend is router-chosen
   (the five `role-*` capabilities, `adversarial-review`, `ux_review`, `offload`). Effectiveness is
   **backend fit**, which needs arm + member identity — never collapse two same-agent arms back to
   the provider name, and never average a member into an ensemble verdict.

**Selection is offered, never mandated — so narrowing is the whole lever.** A capability is offered
to a calling agent that may legitimately have a better way to do the work. The design problem is
raising the odds the right one is chosen, and catalogue size is the dominant factor: published
measurements put selection accuracy at 84–95% for ~50 tools, 41–83% at 200, near zero at 740, with a
safe zone of 10–20 per reasoning context; RAG-MCP measured 13.62% with a full catalogue against
43.13% showing top-3-of-15. A 40-plus capability catalogue queried generically IS the 13.62% case.

Three layers, ordered by when each starts working — see `ARCHITECTURE.md` for the full treatment:

1. **`capability_advisor.SURFACE_BINDINGS`** — declared per surface, 3–7 entries, each carrying its
   reason. Works on day one with no classifier and no history. When adding or reviving a capability,
   say which surfaces bind it, or say why none does — **and this is now the ninth admission
   requirement, not advice**: `capability_admission.req_findable` fails a new capability that no
   surface can offer, distinguishing `bound_nowhere` from `bound_to_unconsulted_surface`, because the
   fixes differ. A binding is only half of it; `capability_advisor.CONSULT_SITES` declares which
   surfaces a caller actually NAMES, and a binding to a surface nobody consults is indistinguishable
   from no binding at all.
2. **`capability_propensity.rank`** — orders *within* the bound set by measured usefulness.
3. **`capability_advisor.learned_associations`** — corrects the table from observed use.

Two rules that keep layer 1 honest, and both are enforced by selftest:

- **Binding prioritises, never conceals.** Unbound capabilities are still returned, ranked after and
  flagged. A concealed capability can never be selected, so it can never earn the evidence that would
  bind it — the gate would starve its own drain.
- **The binding is DATA, not prose.** Never write a loop that edits an automation's or skill's prompt
  to increase selection. §1 makes the manual mirror sync the only circuit breaker between an agent's
  change and the dispatcher; a prompt-rewriting loop is a self-modifying dispatch path. Promote by
  writing a `binding_promotion` event instead, and gate promotion on an EXTERNAL signal (the surface
  did the work by hand; a post-hoc failure) never on the advisor's own naming — otherwise the loop
  ratchets toward invocation regardless of usefulness.

**The objective this work serves.** The point is not to maximise capability INVOCATIONS. It is to
turn a capability on, let it accomplish a stated task, and collect whether it accomplished it — so
the learner can rank capabilities by measured effectiveness and improve the ones that fail. Building
more measurement while producing zero completed, scored invocations is the failure mode this rule
exists to name; it has already consumed a session.

## −1. THIS IS A COMPONENT, NOT THE SYSTEM (read before any fleet-level claim)

**The Orchestrator is one part of a larger pipeline whose SYSTEM-OF-RECORD IS ELSEWHERE.** Every rule
below is about changing this tool. None of them tell you what the wider system is doing, and reading
only this file will make you confidently wrong about it — that has now happened.

**The pipeline, and where work actually comes from:**

1. Repo review (the `Workflows` repo) → human-decision packet → an **approved-issue queue** held by
   the steward repo. That queue, plus already-published open issues from prior cycles, is the
   origin of new work. **NOT** `status: ready` labels.
2. The **opener lane** (local automation, outside this tree) reads that queue and creates issues +
   draft PRs, under its own active-PR cap.
3. **Keepalive** (GitHub Actions, in `Workflows`) drives each PR: an `agent:*` label + green Gate +
   unchecked tasks → agent rounds until every acceptance criterion is checked, then it stands down.
4. The **closer lane** (local) drives merge → verify → close.

**What this tool's live role actually is.** The lanes consume it NARROWLY: `capacity.py` to choose
which agent gets an advisory review, and an `orchestrator_review` fallback path for reviews. Its own
`tick.py --active` additionally applies `agent:*` labels to drive keepalive on remote capacity. It is
a capacity advisor, a review router and a keepalive driver — **it is not the fleet's work-discovery
engine**, and `backlog._is_ready()` is this tool's own private discovery path, not the fleet's.

**"ORCHESTRATOR" IS AN OVERLOADED WORD HERE, AND THE COLLISION IS LOAD-BEARING.** The keepalive
contract doc in `Workflows` has a section titled *"Orchestrator Invariants"* — that means the GitHub
Actions concurrency/round orchestration (concurrency groups, run summaries, bail reasons). It is a
DIFFERENT SYSTEM from this repository. When a Workflows doc says "the orchestrator", assume it means
the Actions workflow until proven otherwise.

**Double-dispatch is prevented by a heartbeat, not by a lock.** When `orchestrate.sh --active` runs it
writes a freshness heartbeat to the handoff dir; the lanes' prerun reads it and halts that round
("legacy cron yields this tick"). Fail-open: absent, stale or malformed → the lanes proceed as
normal. So at any moment one side drives, not both — but the exclusion is COARSE (whole round,
~15-minute freshness), not per-target. Do not add a second dispatch path assuming per-issue locking.

### The rule

**Before making or acting on ANY claim about the fleet — how much work exists, why nothing is
happening, what the fleet is doing, whether a lane fired — read the owning `Workflows` doc and NAME
IT.** A local JSON artifact, a `backlog.json`, a readiness count or a capability inventory is
downstream of that pipeline and does not answer the question. The documented failure is exactly this:
an agent read an empty approved-issue queue and concluded "the opener has nothing to do", which was
wrong because the selection space also includes already-published issues.

**Metrics from this tree are SCOPED TO THIS TREE.** `backlog.json` item counts, `issue_readiness`
verdicts and `true_open` measure *this tool's own dispatch lane*. They are not fleet throughput.
Report them with that scope attached or the next reader will repeat the error.

## 0. Dedup-before-develop (MANDATORY — this project's #1 failure mode)

The Orchestrator's dominant defect class is **built-and-forgotten features**, not bugs. Six
fully-built subsystems were found dormant and (re)activated between 2026-07-03 and 2026-07-08 —
each had working code and selftests, but nothing invoked it, or an unexported `ORCH_*` flag held it
back. Adding a "new" feature that already exists is the easy mistake here.

**Before writing ANY new feature, run this check and record the result in your plan:**
1. Grep the tree for the capability by concept, not just name (e.g. searching "thompson", "resume",
   "calibrat", "adversar", "drain" before building routing/recovery/calibration/review/quota work).
2. Read the historical dormancy inventory:
   `Code/Audits/Orchestrator/2026-07-08-dormancy-rescan.md`, then generate current activation truth
   with `python3 src/capabilities.py inventory`. Feature maturity is not activation evidence.
3. Search the improvement log — `python3 src/improvement_log.py search <term>`. Items carry status
   notes and many "ideas" are already DONE. The log is machine-local evidence living outside the
   tree, so **use the accessor, never a path**: it resolves `$ORCH_LOCAL_RUNTIME` for you, prints
   each hit under the item that owns it, and — when the log is not on this machine — names what is
   missing and exits 2 instead of looking like "no matches". `IMPROVEMENT_BACKLOG.md` in the tree is
   a pointer, not the log.
4. If the capability EXISTS: the task is to **wire/activate/extend or un-gate it** (and say so),
   not rebuild it. If it exists but is deliberately gated, treat flipping the gate as the change and
   justify it. Only build new if the concept genuinely isn't present.

State the dedup finding explicitly before implementing. "Checked X/Y/Z; not present; building new"
or "exists at file:line, dormant behind FLAG; activating." **Record it in the capability's ledger
`notes`, not just in the plan** — plans are not durable, which is why this rule was re-broken twice
after it was written.

**`ADDING_CAPABILITIES.md` is the procedure, and it is ENFORCED.** Run
`python3 src/capability_admission.py --preflight '<spec json>'` before writing code: a capability must
arrive with a dedup finding, a caller, a heartbeat, a recurrence fixture, an outcome path, a kill
switch, a rollback, an expiry-or-cadence, **and a surface that can offer it**.
`test_capability_admission.py` fails the suite otherwise, and also fails on a citation to a dated
record that does not exist or a deadline that passed with no record. That doc lists the failure modes
behind those nine requirements; read it before adding or reviving a capability.

The ninth is the newest and the one `--preflight` exists for: findability is **declarable**, so a
capability no surface will ever offer is a design answer you get before writing code rather than a
second project afterwards. Each requirement carries its own enforcement date
(`capability_admission.REQUIREMENT_ENFORCED_FROM`), because a rule added later is red on arrival for
everything that predates it, and a gate red on arrival gets switched off.

For model/profile or compiler work, also inspect `execution_profiles.py`,
`completion_event_adapter.py`, `pattern_miner.py`, `capability_compiler.py`, `evidence_schema.py`,
`consumer_sync_shadow.py`, `runner_effect_bridge.py`, `cadence_registry.py`, `repo_knowledge.py`,
and `synthesis_promotion.py`; these are the canonical
extension points. Synthesis promotion may add only subordinate delivery phases mapped back to
`capabilities.CANONICAL_STATES`; it must not create a second lifecycle enum or delivery controller.
Do not create a second event log, model registry, or capability inventory.

## 1. Editing & sync

- **Git is canonical** (`stranske/Orchestrator`, public) as of 2026-08-21. The Dropbox tree is a
  working clone, not the source of truth — commit and push; do not leave work only on disk.
- **Three trees, and they are not interchangeable.** (1) the repo/clone you edit; (2)
  `~/.codex/orchestrator-mirror`, which launchd actually RUNS — run `orch-sync-mirror.sh` after
  every edit and confirm with `cmp`, because unsynced edits do nothing; (3) `$ORCH_STATE_DIR`
  (default `~/.codex/orchestrator`), the machine-local state, which is never committed.
  `cmp`-clean is not agreement: re-run the verdict FROM THE MIRROR, since a path resolved relative
  to a module's own directory is right in one tree and wrong in the other.
- **The modules live in `src/`, the tests in `tests/`, and the CHECKOUT ROOT IS NOT THE MODULE
  DIRECTORY.** Those were the same directory until 2026-08-23, and every path in the tree was
  derived from that accident. Two questions with two answers now: sibling modules resolve from
  `paths.MODULE_DIR`, while `orchestrate.sh`, `.verify-floor.json`, `pyproject.toml` and the docs
  resolve from `paths.REPO_ROOT`. **Never write `Path(__file__).resolve().parent` for a repo-root
  file, and never hardcode `parent.parent` for it either** — `paths.checkout_root(module_dir)`
  applies the rule, and the rule is DETECTED (module dir named `src` ⇒ checkout is its parent, else
  they coincide) because THE MIRROR IS FLAT. A hardcoded prefix is right in one tree and wrong in
  the other, which is the failure `capability_activation_audit._fleet_roots` already documents.
  `orchestrate.sh` does the same detection in shell for `$ORCH`. Verify with
  `python3 src/verify.py`.
- **A remote merge is inert until the mirror is synced.** Keep that gap manual. It is the only
  circuit breaker between an agent's change and the dispatcher that dispatches those agents.
  **The `src/` move needs a one-time patch to `orch-sync-mirror.sh`, which lives outside the repo:
  its `cp "$SRC"/*.py` now matches nothing.** The patch and how to confirm it are in
  `docs/MIRROR_SYNC_PATCH.md`. Until it is applied the mirror has no modules.
- Run the touched module's `--selftest` (the project's test suite). Add a selftest case for new
  behavior, including a deliberate-break→revert demonstration for correctness-critical logic.
- **A WIRING PIN NAMES THE FRAGMENT, NEVER THE STATEMENT.** Several checks here assert that a
  helper is actually CALLED by searching the module's own source, because this repo's founding
  defect is code that exists and is never invoked. That technique is right and it stays — but three
  of those pins fired on 2026-08-23/24 against changes that were entirely correct, because each
  pinned a whole line: the pin file's baseline citation, `test_verify_coverage_mode`'s
  `verify(...)` call, and `verify.py`'s own `--update-floor` guard. Reformatting a call across
  lines, or adding an argument, is not a regression; a test that says it is gets waived, and a
  waived test protects nothing.
  **Pin the smallest fragment that would be ABSENT if the wiring were removed.** No newlines, no
  indentation, no trailing `:`, no full argument list — nothing a formatter owns. Keep splitting
  the literal (`"and not " + "_blocks_floor_update(problems)"`) so the needle cannot match its own
  line, and put the reason beside it. Where it is cheap, assert the BEHAVIOUR instead and delete
  the pin: `test_coverage_never_changes_the_exit_code` now asserts "coverage is not among the
  kwargs" rather than "the call reads exactly thus", which is strictly stronger — it catches a
  coverage input in any formatting.
- Register or update lifecycle state in `capabilities.py` for any new/wired capability. Run
  `python3 src/capabilities.py --selftest` and `python3 src/capabilities.py --json validate`. Never mark a
  capability active from code existence, a passing selftest, or a feature-registry maturity alone;
  activation requires executable producer, consumer, outcome, expiry, kill-switch, and rollback
  evidence.
- **Verify with `python3 src/verify.py`, never with `for t in test_*.py; do python3 "$t"; done`.**
  Most test files are pytest-only: run directly they define their tests, execute nothing, and exit
  0 — which is how 9 failures and a two-month-old broken selftest went unnoticed. `verify.py` runs
  real pytest, reads the COUNTS rather than the exit status, enforces a collection floor so tests
  silently ceasing to run cannot look like tests passing, treats a silent zero-exit selftest as a
  failure, and runs the five capability gates. CI runs the same command on a clean machine.
- **The collection floor is an EQUALITY, so adding tests means bumping it in the same PR.**
  `collected` in `.verify-floor.json` must EQUAL what pytest collects: too few fails (tests
  stopped running), and since 2026-08-23 too many fails as well. A floor BEHIND reality is
  permissive by exactly the gap, and that direction was silent for as long as it existed — four
  drifts (21 low at the worst, then 8, then 1, then 2) each caught only because somebody happened
  to look, because nothing required a test-adding PR to touch the file at all. CI prints the two
  integers to write. **If a branch merged under you, REBASE before re-measuring** — the number is
  a property of the merge result, not of your branch. You will rarely have to remember that: once
  every test-adding branch edits these same two lines, two concurrent branches conflict in git,
  and the second cannot merge without rebasing onto the first. `passed` stays a MINIMUM on
  `passed + skipped`, because only collection is machine-invariant — a skipped test is still a
  collected one, so a bare runner and the owner's machine collect the same number while their
  pass/skip split differs. `--update-floor` is not blocked by drift (that would be a gate
  forbidding its own drain) and now APPENDS to the note rather than replacing it.
- **A check whose PREREQUISITE is absent skips with the missing thing NAMED, and skipping is
  bounded.** Some checks need what only a running instance has: the populated capability ledger,
  an installed agent CLI, `~/.codex/skills`, the version-capable Codex binary. Those gates live in
  `env_prereq.py` — detect the prerequisite, never `$CI`, so the same code is right on any machine.
  Three rules, and they are enforced, not advisory: every skip carries a reason naming what is
  missing; `verify.py` prints all of them so a green run always states what it did not check; and
  `.verify-floor.json` caps the number of skipped tests, selftests and gates, so skipping one more
  thing than agreed is a RED, not a footnote. Raising a ceiling means agreeing that one more thing
  goes unchecked — do it deliberately and say why. When a check fails only because a stub leaked
  (a monkeypatched `Popen` catching a model-catalog probe, say), the fix is isolation, not a skip:
  that makes CI run MORE. **Never turn a real failure into a skip**, and never add a skip without
  a reason string — a reason-less skip is indistinguishable from a pass, which is this repo's
  founding defect wearing a different hat.
- **A CEILING BOUNDS ONE DEPRIVED SHAPE, AND THERE ARE TWO.** `skipped_max` was measured on a bare
  GitHub runner — no agent CLIs, no `~/.codex/skills`, no app bundle, no populated ledger — and was
  then also applied to the EXEC MIRROR, which is the opposite deprivation: every local prerequisite
  present, but a flat file copy with no `.github/` and no `.git`, so it skips 31 tests a runner
  skips none of. One number over two populations is the latched-gate defect in its purest form, and
  the symptom was total: `python3 verify.py` from `~/.codex/orchestrator-mirror` was RED on every
  input including a correct tree, for as long as it existed — while §1 above makes that run the
  verdict. Raising the number would have been the wrong repair; it would have handed the RUNNER
  five units of slack, where 26 is the measured bound. **Each shape carries its own agreed number,
  measured where it is enforced**: `env_prereq.exec_mirror_shape()` detects the tree (both marks
  required, `$CI` never consulted) and `verify.mirror_key` derives the floor key, so any ceiling may
  carry a `mirror_` variant and an unset one falls back to the base — the strict direction. The
  summary always prints which tree it decided it was in. When you add a ceiling, ask which shape you
  measured it in before writing the number down.
- **State lives behind TWO variables and they are not the same.** `ORCH_STATE_DIR` holds the audit
  cache, firing-monitor and redirect-sweep state; `ORCH_LOCAL_RUNTIME` holds the capability LEDGER
  and the Brain. Pointing only the first at an empty directory and concluding "the suite is
  state-independent" is exactly the mistake that made the first CI run red — the ledger never
  moved. Set both when testing a fresh-machine claim.
- **The LEDGER is shared per MACHINE; CODE is branch-isolated per WORKTREE — so a row can outrun
  its module.** A capability registered by another session sits in `$ORCH_LOCAL_RUNTIME` for every
  worktree, while its module exists only on that session's branch. `verify.py` then goes red HERE
  with three checks naming only the missing admission parts (`caller_exists`, `heartbeat`,
  `fixture`), which reads as "registered with no implementation — retire it". Retiring the row or
  waiving it DISCARDS finished work, and CI cannot catch the confusion because `ci.yml` bootstraps
  an empty ledger. **Never retire or waive a row before checking whether its module is simply
  elsewhere.** The three checks and the `verify.py` summary now say so themselves, from
  `capability_activation_audit.entrypoint_presence` / `absent_entrypoint_note`, which name the
  sibling checkout the code was found in — but read the message rather than the missing-parts list.
  **And FETCH BEFORE you conclude the code is nowhere.** An empty
  `git log --all --oneline -- <file>` proves nothing until the refs exist locally: `--all` searches
  the refs this checkout HAS, so an unfetched sibling branch reads as "no such file was ever
  committed anywhere". That false negative is what produced the wrong verdict on 2026-08-22 and cost
  a full session. `git fetch origin` first, then search. The honest verdict on a branch carrying
  someone else's ledger row comes from a fresh-state run with BOTH `ORCH_STATE_DIR` and
  `ORCH_LOCAL_RUNTIME` pointed at empty directories, which is what CI does.
- **The split is TOOL vs EVIDENCE.** Generic capabilities, gates and tests are committed. This
  instance's evidence is not: `CAPABILITY_USEFULNESS.md`, `LOCAL_POLICY.md`, `*.local.md`,
  `experiments/`, `ux_reviews/`, `data/`, `Audits/` — gitignored in the tree — plus the ledger, the
  Brain and the **improvement log**, which live outside it under `$ORCH_LOCAL_RUNTIME`. When adding a
  personal figure — spend, availability, a habit — it goes in `LOCAL_POLICY.md`; the code refers to
  it. The boundary is file location, not recall. Published VENDOR list prices are fine and are kept
  deliberately: they are public and they are the model-tier rationale.
  **Evidence an agent is REQUIRED to read must go outside the tree behind an accessor, not
  gitignored inside it** — a gitignored path is absent from every worktree, which is how §0 step 3
  became unfollowable. `improvement_log.py` is the pattern: a tracked pointer of the same name, an
  accessor that resolves the path, and a named absence when the file is not on this machine.
- A live fleet tick runs hourly and writes only to worktrees/state, never to this canonical tree —
  but if another interactive/headless session is editing here too, coordinate (the fleet-checkout
  hazard is real; see the user's memory). Look before overwriting a file you didn't create.

## 2. Learning-loop integrity (don't corrupt the Brain)

- Route weights learn from `outcomes`; keep the un-gameable label (durability, verified success),
  never green-CI-alone. Infra/killed failures must be classified `transient_infra` (excluded from
  learning) — don't let environment noise train as agent incapability.
- Missing cost/effort telemetry must never read as "free" — impute it (see feedback.relearn_quality).
- New evidence sources go through feedback.py's tables + a migration; don't fork a parallel store.
- Execution provenance is causal: only a successful `operation_role=worker` attempt with an explicit
  provider-resolved model can support exact-model claims. Evaluator/verifier/replay traces and
  synthetic adapter tags such as `codex:full:default` must remain non-worker or unresolved.
- Model-profile trial transport is quarantine-only. Use `model_profile_trial_bridge.py` to validate
  the immutable packet/profile/registry/capacity contract, create deterministic request envelopes,
  and collect authenticated Workflows run/artifact provenance.
  Never copy `--model` into `reported_model`, treat a generic trace model as provider resolution, or
  send the trial through a workspace-write commit/push runner. A local Codex session rollout may
  establish CLI-reported identity, but subscription execution has no independent provider-resolved
  immutable identity; keep that field null and quarantine before learning/promotion. The dedicated
  pinned read-only Workflows runner is the only allowed remote trial path; normal Keepalive must reject
  trial profiles. Brain ingestion stays off until its multi-row write is atomic.
- Experiment artifacts and evaluations use arm + member + profile identity. Never collapse two
  same-agent arms back to the provider name, and never use the averaged legacy projection when an
  exact member identity exists.
- Research acquisition yields to production/range reservations and must pass the durable subject
  fingerprint/cooldown/backlog gates. Repeated research on one subject must retain its subject
  weight in `relearn_quality`; do not treat correlated arms as independent evidence.
- Completion observations are seven-phase, canonical, and append-only. A compiler may emit only
  candidate IR with provenance, counterexamples, TTL, and a rollback/tombstone path. It must not
  dispatch, activate, or infer a capability from raw prompts or synthetic artifacts.

## 3. Human involvement (hard constraint)

The owner's weekly attention budget is small and shared across every system; the figure is in `LOCAL_POLICY.md` (machine-local, not committed). **Never add an approval,
review, label, or check-in step that can accumulate a backlog.** Any human touchpoint must be
non-blocking with an auto-expiring default (see the owner-question protocol: agents proceed on a
default; unanswered questions ratify at expiry). There is NO human code-quality review gate in this deployment — use
machine ground truth / consensus / referees for judgment, never an owner code-review gate. No
publishing suggestions. Before proposing any recurring human step, do the attention-cost math (the
global `~/.claude/human-involvement-check` skill).

## 4. Safety switches

Default-OFF `ORCH_*` flags are deliberate (live keepalive apply, Thompson routing, runtime-AC
execution, strategy campaigns, range-lane live dispatch). Flipping one is a real change: justify it,
prefer the system's own evidence gate where one exists (e.g. exploration_review for routing mode),
and update the gated-features list in README.md + the dormancy inventory.

## 5. Keep the docs true

When you activate a dormant feature, un-gate a flag, or add a subsystem: update its lifecycle
record, regenerate the capability inventory, update README.md's functionality section if the
topology changed, and record a status note on the relevant improvement-log item with
`python3 src/improvement_log.py append <item-ref> "<note>"`. Use the accessor rather than editing a
file: the log is machine-local (outside the tree), the accessor finds the item and places the dated
note inside it, and it REFUSES on an ambiguous or unknown ref rather than guessing — a note filed
against the wrong item corrupts the record it exists to improve. Do not duplicate lifecycle verdicts
in prose; stale parallel inventories are how features get forgotten.
