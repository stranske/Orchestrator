# CLAUDE.md — Orchestrator agent rules

> **Local vs public.** This repository is the TOOL: generic capabilities, gates and tests. This
> instance's EVIDENCE is not committed — the failure history, spend calibration and attention-budget
> figures live in `LOCAL_POLICY.md`, `IMPROVEMENT_BACKLOG.md`, `CAPABILITY_USEFULNESS.md`,
> `*.local.md` and `Code/Audits/Orchestrator/`, all gitignored. Where a rule below cites a figure,
> read it from `LOCAL_POLICY.md`. Runtime state (Brain, ledger, stamps, worktrees) lives outside the
> tree at `$ORCH_STATE_DIR`, default `~/.codex/orchestrator`, so a second instance needs a different
> `ORCH_STATE_DIR` and no code change.

Read `README.md` first for what this project is and its important functionality. This file is the
rules for anyone (human or agent) *changing* it.

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
   with `python3 capabilities.py inventory`. Feature maturity is not activation evidence.
3. Check `IMPROVEMENT_BACKLOG.md` — items carry status notes; many "ideas" are already DONE.
4. If the capability EXISTS: the task is to **wire/activate/extend or un-gate it** (and say so),
   not rebuild it. If it exists but is deliberately gated, treat flipping the gate as the change and
   justify it. Only build new if the concept genuinely isn't present.

State the dedup finding explicitly before implementing. "Checked X/Y/Z; not present; building new"
or "exists at file:line, dormant behind FLAG; activating." **Record it in the capability's ledger
`notes`, not just in the plan** — plans are not durable, which is why this rule was re-broken twice
after it was written.

**`ADDING_CAPABILITIES.md` is the procedure, and it is ENFORCED.** Run
`python3 capability_admission.py --preflight '<spec json>'` before writing code: a capability must
arrive with a dedup finding, a caller, a heartbeat, a recurrence fixture, an outcome path, a kill
switch, a rollback and an expiry-or-cadence. `test_capability_admission.py` fails the suite
otherwise, and also fails on a citation to a dated record that does not exist or a deadline that
passed with no record. That doc lists the nine failure modes behind those eight requirements; read
it before adding or reviving a capability.

For model/profile or compiler work, also inspect `execution_profiles.py`,
`completion_event_adapter.py`, `pattern_miner.py`, `capability_compiler.py`, `evidence_schema.py`,
`consumer_sync_shadow.py`, `runner_effect_bridge.py`, `cadence_registry.py`, `repo_knowledge.py`,
and `synthesis_promotion.py`; these are the canonical
extension points. Synthesis promotion may add only subordinate delivery phases mapped back to
`capabilities.CANONICAL_STATES`; it must not create a second lifecycle enum or delivery controller.
Do not create a second event log, model registry, or capability inventory.

## 1. Editing & sync

- Edit the CANONICAL Dropbox copy. Run `orch-sync-mirror.sh` after every edit and verify the mirror
  matches (launchd runs the mirror; unsynced edits do nothing). Confirm with `cmp`.
- Run the touched module's `--selftest` (the project's test suite). Add a selftest case for new
  behavior, including a deliberate-break→revert demonstration for correctness-critical logic.
- Register or update lifecycle state in `capabilities.py` for any new/wired capability. Run
  `python3 capabilities.py --selftest` and `python3 capabilities.py --json validate`. Never mark a
  capability active from code existence, a passing selftest, or a feature-registry maturity alone;
  activation requires executable producer, consumer, outcome, expiry, kill-switch, and rollback
  evidence.
- Not a git repo. There is no CI. Selftests + the dormancy scan + the audit ledger are the safety
  net. Snapshot before large edits if you want a rollback point.
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
topology changed, and append a status note to the relevant IMPROVEMENT_BACKLOG.md item. Do not
duplicate lifecycle verdicts in prose; stale parallel inventories are how features get forgotten.
