# Orchestrator architecture — rails vs. agent-roles

> **Keeping this current (CONTRACT).** This file and [`orchestrator-loop.svg`](orchestrator-loop.svg)
> are the source of truth for the rails/roles taxonomy and the loop. **Any change to the system's
> stages, components, the rail/role classification, the feedback surfaces, or the role registry
> (`roles.py`) MUST update BOTH this doc and the diagram in the same change.** `PLANNING.md` and
> `ORCHESTRATOR.md` point here — do not let them drift. If you touch `roles.py` / `route_role` / the
> registry, or move a component across the rail/role line, updating the diagram is not optional.

![Orchestrator loop: deterministic rails vs. callable agent-roles](orchestrator-loop.svg)

## Status-driven local closer

`closer_dispatcher.py` composes existing rails for the local closer lifecycle;
`closer_queue.py` persists the assigned items and operation receipts. It inventories
status, schedules bounded independent private work, collects results and continues
other items while checks, review or verification wait. It is a deterministic
coordinator, not a new agent role or a second demand scheduler. Existing claims
serialize target/source/repository mutations across ledgers. Workers use existing
`dispatcher.offload`; Luna extracts evidence and proposes findings, while the senior
closer integrates original Tasks/AC evidence. Exact-head merge authority remains in
`merge_guard`; native post-merge evidence remains in `verifier_evidence`.

Postrun validates assignment accounting and actual remaining budget before end-round
or relay. No pass quota or one-complex-repository limit can authorize early stop.
Unknown mutation outcomes reconcile before retry, source/review/head changes
invalidate prior evidence, and verifier requests are deduplicated. Source closure
composes the separately owned supervisor actor when deployed and explicitly enabled;
this coordinator does not enable it. Deployment uses the existing mirror sync and
canonical local prompt renderers, preserving demand scheduling and safety gates.
See [the operating contract](docs/CLOSER_DISPATCHER.md) for trusted disposition input,
resource bounds, recovery and separate operational acceptance.

## Where this sits in the larger pipeline (READ FIRST)

**This document describes ONE COMPONENT.** Everything below — rails, roles, the feedback loop — is
internal to the Orchestrator. The pipeline it participates in has its **system-of-record in the
`Workflows` repo**, and nothing in this file can tell you what that pipeline is doing. Reading only
this document has already produced confidently wrong fleet-level conclusions.

```
  repo review (Workflows)  ->  human-decision packet  ->  APPROVED-ISSUE QUEUE (steward repo)
                                                                    |
                                                     opener lane (local, outside this tree)
                                                     creates issues + ready PRs, own PR cap
                                                                    |
                                        KEEPALIVE (Workflows GitHub Actions) drives each PR:
                                        agent:* label + green Gate + unchecked tasks -> rounds,
                                        stands down when all acceptance criteria are checked
                                                                    |
                                                     closer lane (local) merge -> verify -> close
```

**The Orchestrator's four real interfaces to that pipeline:**

| Interface | Direction | What it is |
|---|---|---|
| `capacity.py` | pipeline → here | The lanes read it to choose which agent gets an advisory review |
| `orchestrator_review` fallback | pipeline → here | A review-fallback path routes an advisory review through this tool |
| `capability_propensity --deliverable` → `capability_outcome_bridge` | pipeline → here | Lane verdicts name a PR explicitly; the bridge joins positive versioned verdicts to keepalive runs and records capability influence edges |
| `tick.py --active` → `delegate_remote` | here → pipeline | Applies `agent:*` labels, driving keepalive on REMOTE capacity — **shadow by default since 2026-09-03** (`ORCH_DISPATCH_LANE=1` re-enables): 14 dispatches in 30 days, none shown to be the labelled agent's work — the 5 credited merges were other lanes' work or empty (a local run's PR merged before the label, an empty bootstrap, a bootstrap the closer finished, 2 codex PRs the labelled agent never ran on), the 2 PRs closed unmerged were bootstraps whose agent round never completed, and 7 labelled issues closed with no PR from the labelled agent, 4 of them delivered by codex keepalive or by hand (re-measured 2026-10-04 from keepalive's runner records) — while keepalive ran 1,239 rounds without it |

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

Observer reports and tick-cadence rails retain invocation, fingerprint and finding-delta evidence,
but cannot earn delivery-usefulness verdicts. The existing selection exemption supplies the
classification, with explicit declarations for the event-driven propensity and redirect rails.
Historical observer verdicts remain in the append-only ledger as report-time `observer_noise`,
excluded from raw and weighted delivery rates. Usage prints `verdict n/a` and the value-chain
monitor's accepted consumer actions in its existing window, or `unmeasured` when unreadable.


`router` (selection) · `claims` · `capacity` · `provision` · `dispatcher` (transport) · `adapters` ·
`feedback` (the learner/store) · gates (`testgen_gate`, `local_verify`, `merge_guard`,
`runtime_ac_gate`, `frontend_verify` (Gate 1), `ux_review.gate_decision` (Gate 2 pass-requirement)).

The runtime-AC rail also has a non-blocking observation edge: keepalive ingest authors one
issue-derived shadow spec per new linked PR, and switch review consumes its existing Brain
completion events. Authoring extracts named tests, retains uncertain obligations as manual,
and records per-check confidence. Execution still requires the existing runtime/command
switches and an exact PR checkout. Ingest shares the panel's exact-commit provisioning rail,
uses a private disposable checkout, and attempts at most three executions per ingest across
all repos (failed checkout attempts also consume this bound). Remaining observations are
`planned/shadow_execution_budget`; checkout/cleanup failures carry per-target reasons in
`runtime_ac_checkout_errors` and increment `runtime_ac_checkout_failures`. Every disposable
checkout is removed after observation, including failed verification. Shadow specs never
implicitly enable merge enforcement or patch delivery outcomes. Coverage-only pytest options
are removed for named acceptance checks so test success and coverage policy cannot disagree.

Durability sweep also rechecks existing issue specs on the exact merge commit, at most
three disposable executions per sweep. It reuses the runtime runner, compares deliberate
breaks against the merge parent, and records one `post_merge_recheck` completion event per
source issue and merge SHA, including UNRUNNABLE observations. Assertion failures comment
once with the transcript; tool/collection/manual-evidence failures remain UNRUNNABLE and
never reopen an issue. Transcript artifacts are hash-bound under the existing runtime-AC
spec directory; failed GitHub delivery retries from the event without executing again.
`ORCH_RUNTIME_AC_REOPEN=1` additionally permits FAIL to reopen and label the source issue
`runtime-ac:reopened`; it defaults OFF and expires after 2026-10-27. Unset it to roll back;
comments remain. Existing runtime/command switches still govern execution. Switch review
prints `runtime-AC post-merge: rechecked N, failed F, reopened R, unrunnable U`. This remains
post-merge observation and adds no merge authority or delivery-outcome verdict.

Codex model and reasoning selection is also a deterministic rail: `execution_profiles` maps
the task and lane to an immutable profile, `router` records the selected profile, and
`dispatcher` passes its model and effort to `adapters`. An explicit `--profile-id` on a
bounded offload or delegation chooses that exact profile; it does not create a new role or
change the feedback loop. Operator `ORCH_CODEX_MODEL_*` pins and `ORCH_CODEX_MAX_TIER` ceilings
continue through the tier adapter for automatic routes; the explicit profile stays exact.
Gemini catalog renames retain the pinned effort when the newest available family offers it.
Gemini tiers follow the other families' shape (2026-10-15): cheap/mid run 3.8 Flash low/high and only
`full` runs the frontier model, Argon High, which replaced the Pro slot (agy no longer serves Pro 3.1;
the retired 3.1 profile follows `PROFILE_RETIREMENTS` to Argon High). Argon low/medium stay registered
and explicitly requestable, because Argon has its own weekly-only quota and bills ~2.7x Flash per token.

**An offload run records which work it was for (2026-10-12).** Both capability readers key a run's
subject by a `subject_id` in its routing metadata, else by `runs.target`, and an offload's target is
`offload:<cwd>`. No offload run recorded a subject, so all 591 role-backend runs since 2026-09-01 were
one subject (`offload:/`, the tick's cwd) and each research-program directory another.
`dispatcher.offload` now records `subject_id` and `subject_source`: a registered research round's
subject first (the completion-identity path already keys a round-bound run that way), else the
caller's `subject_id` (`--subject` on the CLI), else nothing. A role passes its own target when it
names one work item (`feedback.work_item_subject`: `owner/repo#N`); triage ranks a batch and passes
none, and a lane agent's ad hoc offload stays unkeyed until its caller names the work. Prose never
supplies a subject. The data migration `runs.offload-subjects-2026-10-12` gives earlier runs the
same two keys from the same two structured links (the round, and the role run whose
`backend_run_id` is the offload); `feedback.py restore-offload-subjects` undoes it. On a copy of the
live Brain it keyed 1,214 of 4,461 offload runs (277 by round, 937 by role target, none ambiguous).
The loop diagram is unchanged: no stage, component or surface moved.

Complete collected evidence also crosses the existing dispatch transport rail. Gemini
offloads can carry the full prompt as one native NDJSON user message on stdin, using
a private temporary file rather than embedding evidence in shell or provider argv.
The adapter preserves the selected model, workspace isolation and timeout; transport
never truncates the packet or grants acceptance. The file is removed after each attempt,
including start failures and timeouts. Other agent transports retain their existing contract.

Determinism here is load-bearing: the claims/capacity/provision rails are what the "0 unsafe
delegations" guarantee rests on, and the gates guard terminal merges and must stay auditable. An
LLM verifier (a review panel) is a *supplement* to a gate, never a replacement for it.

**The guarantee's two ownership reads answer three ways, and unknown refuses (2026-10-04).** Before
`delegate_remote` labels a target, the tick asks `claims.holder` whether a local claim holds it and
`dispatcher._target_labels` whether GitHub shows it paused or carrying an `agent:*` label. Each read
can say "owned", "free", or "could not tell". Until this date "could not tell" came back as "free".
A failed `gh` read returned an empty label set, and the rail labelled two PRs that had carried
`agent:codex` for hours (Trend_Model_Project#5913, #5944), each in a tick that skipped a neighbour
for the same label. A held claim with unreadable meta returned None, and claim meta was rewritten
in place, so a read caught between truncation and write made an old live claim look stale. Now an
unknown answer refuses the target, with the reason in the plan, in shadow and live ticks alike, and
claim meta is replaced in one step (`claims._write_meta`). The tick headline prints
`label reads A answered, U unanswered (refused)`. Nothing is cached, so the next tick's read clears
a refusal as soon as GitHub answers. (No stage, component or surface moved; the diagram is
unchanged.)

**A refusal takes no delegation slot (2026-10-04).** The tick's per-tick cap
(`ORCH_MAX_REMOTE_PER_TICK`, default 3) used to count every row that reached `delegate_remote`,
refusals included. Discovery lists a closer item only when its PR already carries an `agent:*`
label, and lists closer items first, so the cap filled with refusals. Over the live period
(2026-06-15 to 09-02), 2,826 of its slots went to refusals and 25 to delegations, and 65 targets that
carried no agent label when first deferred were never examined. `tick.is_delegation` is now the one
predicate the cap, `production_reserve`, the rejected-role influence edge and the `TICK-PLAN`
headline consume. A second bound, `EXAMINED_PER_DELEGATION` (4) times the cap, limits the items a
tick examines, so a backlog of owned PRs cannot turn into unbounded label reads. Items the rail
refuses on their discovery labels are examined after the rest (`tick.examination_order`), so they
cannot hold a delegable item behind that bound. The plan and the headline print each bound beside its
count, and the deferred items' blocking quantity (`delegable`) beside the drainable one.
In an active tick the cap's deferrals are drainable only when at least one cap-filling label
applied; a failed POST still consumes the spend bound but leaves its target unowned to take its slot
again. (No stage,
component or surface moved; the diagram is unchanged.)

**The rejected-role edges written for refusals are deleted once (2026-10-05).** Before that change
the tick wrote its rejected-role edge for every row, and a refusal records no run. The Brain held 210
such edges. The tick logs place 208 of them in a tick that refused the target: 78 point at a run
nothing recorded, 25 were written before the run's first recording and linked by its back-fill, and
105 were written by later ticks that refused a target its delegation had left owned. Each says a role
was overruled by a run that never saw it. A one-time data migration in `feedback.py`
(`influence_edges.refusal-role-edges-2026-10-05`) deletes those rows and keeps an edge written within
300 s after a recording of its run. `runs.ts` holds only the latest recording, so the first is read
from the run's completion events. It selects only the tick's own rows: role, `remote:` target,
rejected, no capability, the tick's metadata hash. The 2 edges from delegating ticks stay. The
deleted rows are kept in the migration's marker, and `feedback.py restore-refusal-role-edges` puts
them back. `completion_event_health` orphans fall from 298 to 220. The rest are capability edges, so
`dry_seam_audit` still fails on them. (No stage, component or surface moved; the diagram is
unchanged.)

**The closer-PR review hooks run at the merge, not before a delegation (2026-10-05).** Discovery
lists a closer item only for a PR that already carries an `agent:*` label, and the dispatcher refuses
every such label, so the tick can never delegate one. It still ran three hooks on each: the
runtime-AC gate, the adversarial panel for a high-stakes one, and the adjudicator on their
disagreement. None could change an outcome, and nothing read the panel's result. In the 30 days to
2026-10-06 they wrote 1,243 runtime-AC gate events for fleet PRs, every one `skipped`; 851 were shadow
ticks re-examining four PRs that merged on 2026-09-18, from a backlog not refreshed since. The
adjudicator hook recorded 1,777 selector events and matched no case, and the panel would have
re-reviewed the same head on every hourly tick. The
tick now sets closer items aside unexamined: they are named in the plan (`closer_not_examined`), counted on the
TICK-PLAN line, and kept out of research. Their hooks run at the decision a closer PR faces, its
terminal merge. `merge_guard` already ran the runtime-AC gate. It now gives a high-stakes PR the panel,
with the risk label read from the source issue because no fleet PR carries `risk:*`. The panel is
judged once per (PR, head SHA) by `adversarial.review_at_head` and stays advisory, behind the same
flag. The adjudicator is consulted only on a conclusive disagreement. The memo key is a hash over
the head, so a moved head is never shown an old verdict. Only PASS and BLOCKED are reused: an
INCONCLUSIVE panel is a shortfall, and reusing it would forbid its own re-run. A worktree at any other
commit is refused, and nothing is recorded. The Brain keeps the verdict and hashes; the findings
live in a hash-checked artifact beside it. The closer lane, which merges fleet PRs itself, reaches the
same entry through the advisor's `HOW_TO_USE` (`adversarial.py review --target … --head …`).
The panel moved from the Delegate stage to the Monitor + verify gate, so the diagram draws it there.

**The panel is on by default for eligible high-stakes merges, after the merge, with one reviewer
and a consumer (2026-10-12).** `ORCH_RUN_ADVERSARIAL_REVIEW=0` prevents new panel runs.
Scheduling requires determinable high-stakes status, an exact head, passing merge gates, and a
successful merge. A retrospective concept test replayed the panel on the 33 high-stakes agent PRs merged
2026-07-07..10-05, each at its final head in a checkout holding only that head's ancestors. Both PRs
that later broke were BLOCKED with the defect named; BLOCKED PRs were later broken or repaired 6 of 16
times against 1 of 14 PASS. Three mechanism defects came with it. Vibe vetoed 30 of 33, so the
two-seat panel was gemini's verdict paid twice. Reviewers ran write-capable in the tree they judged:
one edited the code the next reviewer vetoed, another left HEAD on an older commit. And nothing read
a BLOCKED verdict. The owner decided: on by default, advisory, one reviewer, and a consumer. So
`merge_guard` defers the panel past the merge and starts it detached
(`adversarial.spawn_post_merge_panel`): the merged head is the head it judges, and a ~13-minute
panel inside the merge call would stall the lane. The seat goes to ONE reviewer the router chooses on
the `review` route (`adversarial.choose_reviewer` over `router.select_agent`: ability, then capacity
and load, with exploration re-testing the others), never the change's author agent, vibe only when
nothing else is selectable. A reviewer that returns nothing or changes the tree is replaced, up to
three. The tree is checked after the panel as well as before, and a changed tree is restored with its
verdict discarded. A BLOCKED verdict files one follow-up issue in the target repo
(`adversarial.file_followup`), idempotent per panel key, and closes the PR's still-open source issue so
it is not left hanging. The checkout is the actual squash merge commit; the original PR head remains
its memo and follow-up key. Explicit reviewer sets also exclude authors and retain INCONCLUSIVE
when coverage is lost. Unavailable confinement dispatches no reviewer; the existing write boundary
protects sibling checkouts, live home state and Git metadata. Dirty or untracked review trees are
refused, restoration is verified before retry, and failed source closure is retried independently
of the already-filed follow-up. Selection stays a rail; only the reviewer's judgment is a role.

The adapter also decides each codex run's writable boundary. A run whose job is to commit (a
dispatch from `plan_dispatch`, or an experiment arm) gets `--add-dir` for exactly its linked
worktree's git dir, plus the canonical clone's `objects/`, `refs/` and `logs/`
(`adapters.codex_worktree_git_roots`). Since codex 0.158 that git dir is read-only under
`workspace-write` even inside a broader writable root, and the 2026-10-04 batch could not commit
in place. Offloads, read-only runs and the outer-seat bypass get nothing, and the clone's `config`
and `hooks/` are never granted. No stage, component or surface moved, so the diagram is unchanged.

That grant leaves one residue: the private git dir's `commondir` and `gitdir` pointer files are
writable by the run, and the tool later runs git in that worktree itself, outside any sandbox
(the push record, the redirect sweep's `watch`, `exp_abcd.collect`, `provision`'s reuse check and
`dispatch`'s status). Those reads go through `worktree_git.run` (2026-10-11). For a worktree under
`provision.WORKTREES_DIR` it checks the `.git` file, `gitdir`, `commondir` and the absence of
`config.worktree` against the clone `provision` derives (`canonical_path(repo)`, or the clone the
worktree's name belongs to). A mismatch refuses without starting git and names the file and the
clear path, which is removing the worktree and re-provisioning it. A match runs git with
`GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE` pinned to those known paths and with
`core.fsmonitor` and `core.hooksPath` disabled; other paths (offload cwds, `local_verify`) get the
two `-c` flags only. `ORCH_WORKTREE_GIT_GUARD_DISABLED=1` restores the old commands. It is a rail
hardening inside existing stages, so the diagram is unchanged. Each refusal is also appended, best-effort and
without changing it, to `$ORCH_STATE_DIR/worktree-git-refusals.jsonl` (time, worktree, pointer file,
reason, caller), and the weekly `switch_review` prints the window's count and latest worktrees as an
FYI line, zero included, beside the `skip-git` lines the out-of-repo `lane-worktree-gc.sh` now writes
when a removed worktree's `.git` names a canonical outside the known homes (2026-10-11); that is a
feedback surface on an existing sweep, so the diagram is still unchanged.

**An offload's caller can bound where it writes, whichever agent runs it (2026-10-11).**
`dispatcher.offload(write_boundary={"protect": [...], "allow": [...]})` lets the run write nowhere
under `protect` except its own cwd and the `allow` roots, and never in a `.git` beneath them. codex
is held by its own sandbox, with one `--add-dir` per root. cursor, agy, vibe, aider and claude have
no sandbox, so they run under a `sandbox-exec` profile (`adapters.seatbelt_write_profile`); cursor
and agy also get the roots as `--add-dir`, because their file tools keep to a workspace. A boundary
that cannot hold (an empty `protect`, a broad root, codex's seat bypass, no `sandbox-exec`, or a
nested seatbelt) refuses the run with exit 2 (`adapters.write_boundary_unenforceable`). Without a
boundary every argv is unchanged. The research program's driver is its first caller: from
2026-09-06 to 10-04 its 232 workers (cursor 144, gemini 50, codex 36, vibe 2) all ran with the
program's own code directory writable. No stage, component or surface moved, so the diagram is
unchanged.

**Muse Code is a backup-only, subscription-login backend (2026-10-15, issue #612).** Meta's `muse`
CLI joins the delegated sub-agent band as a seat that routine selection NEVER picks: it is in
`router.BACKUP_AGENTS` with aider. It has explicit-only entries in the `implement` and `review` route
tables and in the corresponding role eligibility sets, but the backup guard filters those entries from
ordinary selection; only `select_agent(..., only={"muse"})` or an explicit dispatcher call can reach it.
This preserves deterministic selection and priors while making the declared exploration surfaces honest.
Its capacity seat is `metered` (`capacity.AGENTS["muse"]`) because the subscription is a constrained
allowance, while its immutable execution-profile pool remains `flat`; the latter is a pool classification,
not a price claim. There is no usage API, so observed local accounting never claims Meta's reset epoch and
429-shed is authoritative. **The credential is the CLI LOGIN, never an API key.** `META_API_KEY` always overrides that login, and a
Default-Project key metered pay-as-you-go: measured on the live console, two capped execs under it put 8
requests and 81.5k input tokens in the pay-as-you-go ledger (the key the login itself mints, in the
`Muse Code` project, is untested), while a 30-request keychain-login run moved only the
subscription meter (under 1% to 1%) and added nothing there. So `_agent_runtime_prelude("muse")` unsets the
variable by name, and `adapters.auth_health("muse")` reads that record so `capacity._classify` sheds the seat when the login is
missing or has no active subscription (an unreadable record is not a failure). Meta documents the credential
order as the env key, then a stored key, then the stored browser session; whether a stored key can coexist
with the login is unverified, so the gate admits only an OAuth login record and refuses any other mechanism.
`agent_auth_check` counts the seat usable only when the login records an active
subscription (`subs_active`), reading the file's shape without logging credential values. The child uses
an isolated `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, and temporary
runtime state. The pinned `MUSE_AUTH_PATH` aliases only the existing login record; on macOS, the private
HOME's `Library/Keychains` aliases only the existing OS credential-store directory. A genuine native
trial authenticated with this one directory alias after private HOME alone failed. Personal Muse
settings are not linked. This isolates normal HOME/XDG config discovery; it is not an OS denial of
arbitrary absolute-path reads. An outer read-deny experiment blocked private files but broke Muse's
inner shell sandbox, so that unsupported nested arrangement is not used.
The argv is headless with
the sandbox ON (`--disable-approval`, never `--yolo`), without the owner's personal Claude/Codex rules
(`--no-foreign-personal-context`) and without a session log. Synchronous offloads and direct adapter calls pass prompts through a private
`--prompt-file`, native Muse JSONL durable-bound terminal parsing is the only accepted result format (not
the MSP protocol schema), and `--max-model-steps` bounds one invocation. A write boundary is REFUSED for muse
(`adapters.write_boundary_unenforceable`): it already runs in its own sandbox and a seatbelt profile around
it is an unproven nested seatbelt. Muse emits no token or usage field (37 `--json` events inspected) and the
console lags up to an hour. The seven-column `costs` table therefore stays unchanged: its Muse row uses
non-null `0.0` only with `source=imputed:subscription`, while the keyed
`muse_subscription_usage` extension records the actual invocation count, raw exits, wall seconds, and a
locally observed rolling five-hour window whose provider reset remains unknown. This is neither measured
dollars nor a free seat. A TSV per-exec audit row remains available through `dispatcher._record_muse_usage`
(`~/.codex/bin/muse-usage.sh`). Remote (CI) Muse is NOT part of this: a runner can only authenticate with the key, which bills
pay-as-you-go, so Workflows#3918 and #3919 stay parked. The sub-agent band lists one more backend; no
stage, rail or feedback surface moved. Native execution evidence and the post-merge provider verdict remain separate acceptance gates.

**No agent run is given a workspace that is or holds `/`, the home dir or the control plane
(2026-10-05).** A run's workspace is where it may write. codex makes `--cd` a write entry of the
run's own permission profile, so `writable_roots` cannot narrow it. agy's `--add-dir` is its write
guard. cursor, claude and vibe have no sandbox and work in the process cwd. The tick runs from
launchd with cwd `/`, and a role offloads with `cwd="."` unless it has a worktree, so 1,521 offloads
ran in `/` from 2026-07-10 to 2026-10-02 (gemini 1,279, cursor 224, codex 18; 1,513 were redirect or
triage role runs), each able to write the whole disk. `adapters.broad_workspace_reason` is the one
predicate. It refuses `/`, the home dir, `~/.codex`, `~/.claude`, the state dir, the local runtime
and the exec mirror, and any directory containing one of them. It compares the spelling and, for
case variants, the directory identity. `dispatcher.offload` replaces a refused workspace before
isolation with a fresh scratch directory under `$ORCH_STATE_DIR/scratch-workspaces`
(`adapters.agent_workspace`). The run still starts. The relocation goes to stderr, the dispatch log,
the agent's prompt, the result's `workspace_relocated` and the ledger's `workspace_relocated_from`.
`plan_dispatch` skips a refused provisioned worktree like a provision failure, because a committing
run cannot work in scratch. `build_command` refuses to emit one for a writable codex run or for agy.
That backstop covers only the argv it builds, and two spawners never call it. The A/B evaluators
(`exp_abcd._eval_command`, shared by `evaluate` and `ux_review`) are started by `Popen` with no cwd,
so in the tick they ran in `/`; agy's `--add-dir` named the module directory, which in the mirror
is the dispatcher's own code. Each evaluator now `cd`s into a scratch workspace of its own, and agy
is granted only that. Experiment arms (`exp_abcd._spawn`) and `adapters.dispatch` refuse a broad
workspace for every agent, because cursor, claude and vibe have no argv workspace to check, and a
synthesis resume with a blank worktree is blocked rather than run in the caller's cwd. The
predicate also names every home's default state dir and mirror (a sandbox may move `$HOME`) and,
on macOS, each anchor's `/System/Volumes/Data` firmlink path. Empty scratch directories older than
a day are pruned (`os.rmdir`, so anything an agent wrote is kept). The in-process tests replace
every spawn and store write with a failure, because a regressed guard reached the real ones once.
(No stage, component or surface moved; the diagram is unchanged.)

The dispatcher's agent prelude moves `XDG_CONFIG_HOME` into each agent's runtime, and gh honours
it. Until 2026-10-04 an agent whose dispatcher exported neither `GH_CONFIG_DIR` nor `GH_TOKEN` (a
delegate or offload started from a session, a lane or the check-in runner) read an empty config
and stopped at "gh auth login" before asking the keyring for a token. Agents the tick starts were
never in that state: `orchestrate.sh` exports both and the dispatcher passes its environment
through, so they also carry the tick's `GH_TOKEN`. The prelude now pins `GH_CONFIG_DIR` to the
directory the dispatcher's own gh reads, by gh's own lookup order (`dispatcher.gh_config_dir`).
Relative paths are anchored to the dispatcher's cwd before the child changes directory. The agent
therefore uses the same config; authentication also requires access to that config's credential
store. The pin is a path and adds no token to the agent's argv or environment, and every other
tool keeps the redirect. It works in both directions: gh's own writes (`auth logout`, `auth
switch`, `config set`) from an unsandboxed agent reach that config and keyring entry too, as they
would from the dispatcher's shell, so a guard refuses them inside a dispatched agent (`dispatcher._gh_guard_exports`, owner decision 2026-10-05): `auth logout|login|switch|refresh`, `config set` and `alias set|delete|import` exit 77, every other gh command runs. A wrapper first on PATH is not enough, because a login shell (codex's `zsh -lc`, cursor's shell) re-applies the owner's PATH; the same check is also a `gh` shell function that bash reads from `BASH_ENV` and zsh from a proxy `ZDOTDIR` whose files first run the owner's own dotfiles. It is bypassable on purpose by gh's full path. No stage, component or surface moved, so the diagram is
unchanged.

**The detached wrappers name the interpreter of their own python steps (2026-10-05).** The dispatch
and experiment wrappers are `bash -lc` strings that end with python steps: the claim release and the
completion record. Those steps were spelled `python3`, so the login profile picked the interpreter,
and its pick depended on the launcher. Codex Desktop exports conda's variables (`CONDA_SHLVL=1`,
`CONDA_PREFIX`) into every command it runs. With those set, the profile's conda hook took base as
already active and left an older `~/anaconda/bin` first on PATH. That directory's `python3` is an
unsigned x86_64 interpreter from 2016, and macOS kills it at exec. All 31 `rc=137` dispatch runs
show this signature, both steps killed at exec. 27 of the 31 were launched from Codex Desktop
threads (the other 4, in July, left no launcher record), and 0 of the 35 launched from Claude
sessions (33) or `codex exec` (2) died. The 3 Codex Desktop launches that went through `launchctl
submit`, which starts from a clean environment, also survived. The steps now run under the
interpreter running the dispatcher (`adapters.wrapper_python`), and the marker's `release_rc` counts
any later kill. The marker still precedes the completion step, so a step that dies for any other
reason still leaves latency and exit status to backfill. (No stage, component or surface moved; the
diagram is unchanged.)

**An offload's verdict comes from what the agent said, not what it read (2026-10-05).** An offload
fails with exit 70 when the agent reports the `OFFLOAD_INCOMPLETE` marker, and the dispatcher looked
for it anywhere in stdout. A codex run's stdout is its `exec --json` stream in every mode but
`assess`, so it also holds every command the agent ran and every file it read, and the repo-audit
skill's references quote the marker. 30 of the 51 codex offloads recorded as exit 70 carried it
only in command output, all 30 ending `turn.completed`. 16 of them reached the learners as FAILs
and 6 as not-useful `offload` verdicts, and the research program retried their units. For a run
whose argv asked for the stream, the dispatcher now reads only the agent's last message
(`rate_incidents.codex_agent_messages`), where the 12 runs that said the marker said it. A stream
with no message fails as `agent returned no message`. Text output is read as before. Recorded rows
are not rewritten. (No stage, component or surface moved; the diagram is unchanged.)

**An offload's log and run_id are claimed together (2026-10-05).** `dispatcher.offload` read
`time.time_ns()` once for its log name and again for its run_id, and on macOS that clock moves in
whole microseconds, so two offloads started in the same microsecond in one process could share
either name. In the capacity ledger, 4 of the 4,517 offload logs were written by two runs each, and
3 run_ids were carried by 7 runs. A shared log holds both run headers before either run's output, so
the reconcile pass read nothing for one run and both outputs for the other. A shared run_id is one
Brain `runs` row, and the second run replaced the first. `dispatcher._claim_offload_log` now creates
the log with O_EXCL, moves one nanosecond on past a name already on disk, and builds the run_id from
the same number. The name keeps its `offload.<agent>.<ns>.log` shape, and each of the two names
gives the other. Recorded rows are not rewritten. (No stage, component or surface moved; the
diagram is unchanged.)

**A shed seat names what clears it (2026-10-05).** A seat is shed by a marker that
`rate_incidents.ensure_shed` writes with an expiry, an incident and a category, and until this date
`capacity` printed "observed 429 / rate-limit shed flag set" whatever the marker held. One pure
reader, `capacity.shed_marker`, now serves the gate, its reason and the weekly `switch_review`. The
reason names the expiry (UTC), the incident and its category, and says the first capacity read
after the expiry clears the seat. A marker with no readable expiry says it is manual and names the
path to remove, and an unreadable one still holds. A codex refusal's stated reset sets the expiry,
read only from codex's harness error events (`rate_incidents.provider_reset_at`, used by every
recorder of a run). Until then the offload recorded no reset, and two refusals of 2026-09-25 that
named Sep 28 shed for the 6 h cooldown. `switch_review` names every marker, FYI only, and flags one
placed by hand that has held its seat past 14 days, or an expired one still on disk. (No stage,
component or surface moved; the diagram is unchanged.)

**What an agent prints passes one mask before this tool writes or returns it (2026-10-06).** While
gh was broken for dispatched agents, agents fetched a GitHub token another way and printed it, and
the dispatcher kept it wherever it keeps what an agent says: a vibe offload's stdout went into a
world-readable dispatch log and back to the caller, whose driver saved a copy, and two experiment
arms printed it into logs the detached wrapper fills straight from stdout. `credential_redaction`
holds one shape list (GitHub, `sk-` keys, Slack, Google, AWS, LangSmith, JWT, bearer and basic
headers, URL passwords, upper-case `*_TOKEN`-style assignments, PEM keys) plus the exact value of
the gh token file. `dispatcher.offload` masks before it logs or returns. The completion step a
detached run's wrapper already runs (`ledger_reconcile complete`) masks that run's own log segment
in place, for runs started from 2026-10-06 only. The experiment and UX-review panels mask their
output file before parsing it into the Brain. A mask is as long as what it hides, so an in-place
edit moves no byte another writer is still appending after. The agent CLIs' own transcripts
(cursor, agy, vibe, codex rollouts) are written by the CLIs and stay out of reach. A weekly FYI
section of `switch_review` counts the files under the dispatch-log and agent-runtime directories
holding the token's exact value or a GitHub-minted token (its own checksum holds), and edits
nothing. (No stage, component or surface moved; the diagram is unchanged.)

**The tick watchdog is infrastructure around the loop, not a stage of it** (`tick_watchdog.py`,
2026-10-02). launchd starts no tick while one runs, so a tick that never ends is a gate with no
drain, and everything that could report it runs inside the tick: for 5d20h from 2026-09-26 the
symptom was silence. The tick's first lines arm it as a separate process in its own session. It
reports a command past 90 min or a tick past 3 h with the exact `kill` that frees it, and is
report-only by the owner's decision. It is deterministic code, so a rail by the definition above,
but it selects nothing, gates nothing and feeds no learner. The diagram draws it as the rail along
the right edge: beside every stage rather than inside one, because it runs outside the tick.

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
| **PromptAgent** | dispatcher generic templates | issue → dispatch prompt or issue body; batch routes once, per-item scored runs | **built, shadow** |
| **DecomposerAgent** | `epic_lane` planner prompts | vague/large goal → subtask DAG | **built, shadow (2026-06-20)** |
| **TriageAgent** | `backlog` worth-it filter | which items now, skip underspecified, batch | **built, shadow (2026-06-20); optional fresh ranking consumption behind `ORCH_TRIAGE_CONSUME` (default off)** |
| **AdjudicatorAgent** | `runtime_ac_panel` / `adversarial` dispute step | verify a lone reviewer veto vs. ground truth | **built, shadow (2026-06-20)** |

PromptAgent is offered at the `research-program` and `repo-audit:phase-4` batch-filing
surfaces. `run_prompt_batch` consumes one per-cycle prompt-role decision for a whole
batch; each body retains its own validated role run and outcome lineage. Explicit
batch authoring does not enable automatic dispatch roles: an unset `ORCH_ROLE_SHADOW`
still leaves that seam off, and the delegate CLI reports the unset gate once.
Single-body, one-prompt, and no-batch declines are `wrong_moment`, so they remain
visible in surface counts without demoting the binding or rewriting past events.

The weekly `switch_review` also consumes `value_chain_monitor`, a deterministic
read-only rail beside Monitor and verify. It compares independent situation populations
with ledger invocations and existing Brain influence edges, naming the earliest broken
step and input switches held off. Unknown demand never becomes zero. It changes no
selection, gate, dispatcher or lifecycle decision; the existing weekly artifact carries
the section rather than a second audit log or inventory.

**Its `outcome` step reads what became of a run from `outcomes`, and counts it with the one
classifier of these edges (2026-10-11).** It counted an acted-on edge's own `outcome_verdict` and
`durability`, the copy that `capability_effectiveness` stopped trusting the same day (see the
feedback loop below), against a private verdict-and-durability set, and never read `failure_class`.
`_brain` now replaces the edge's copied columns with its target's `outcomes` row, read when the
report is made, and adds the target's `runs.role_name`. Each acted-on edge is then classified by
`capability_effectiveness._classify`, which applies `feedback`'s predicates
(`LEARNING_EXCLUDED_FAILURE_CLASSES`, `role_target_kind`, `outcome_is_terminal`). So the monitor is
the third reader that agrees with the lifecycle on what an outcome says, and it has no predicate of
its own. Only a terminal outcome, good or bad, is the `outcome` step. Every other class is counted
in its own field and printed beside it with a legend:
- `excl`: learning-excluded;
- `assoc`: an edge onto a role run, whose outcome is a copy of the acting run's (set aside at
  `acted_on` since 2026-10-12, below);
- `sweep`: a merge the durability sweep has not judged;
- `nomerge` (`unres` until 2026-10-12): a run that never merges. Since the owner's decision that
  day, it is set aside here and scored by verdict in `capability_effectiveness`, so the FAIL
  among them no longer counts at this step (agy-runtime-isolation's 8);
- `open`: no outcome yet.

`no_outcome` now means something can still resolve, which is only `open` or `sweep`. When every
evidence edge is excluded, the first break is `outcome_excluded`. When nothing can ever be scored,
it is `outcome_unscoreable`. This is the split `capability_effectiveness` reports as
`excluded_only` and `unscoreable`. An unread Brain still leaves every count unmeasured.

Measured on a copy of the live Brain, with the same inputs and clock on both sides:
- role-triage's `outcome` fell from 2 to 0 (12 excluded, 281 associations), and its first break
  from `works` to `outcome_excluded`. Its two outcomes were edges still reading PASS/durable on
  Trend_Model_Project#5913 and #5944, whose outcomes the 2026-10-04 relabels had cleared to
  `unattributed_delegation`. They had been the report's only `works`.
- offload moved from 48 to 46 (15 excluded, 1,742 associations).
- deliberate-break-verifier moved from 64 to 106 (5 awaiting the sweep). The 42 new ones are merges
  the fleet verifier marked `NON_PASS` while their durability is pending, which
  `outcome_is_terminal` treats as final.
- agy-runtime-isolation moved from 4 to 12: 8 FAILs, plus 11 excluded and 38 unresolvable set aside.
- role-adjudicator, runtime-ac-checks and testgen-lane each moved from 0 to 1, a `NON_PASS` merge.
- role-redirect's 762, role-prompt's 8 and role-decomposer's 4 acted-on edges are all associations.

The other 37 rows are identical. No first break other than role-triage's changed. No stage,
component or surface moved, so the loop diagram is unchanged.

**The section reaches a reader, `works` needs a success, and a role run is not acted on
(2026-10-12, owner decisions).** Three changes followed:
- **The tick writes the readable report.** The tick ran `switch_review.py --json` alone, so
  `format_report`, the monitor's declared consumer, never ran in production. The `value_chain` key
  reached `switch-review.json`, where nothing reads it, and no owner question was ever raised from
  switch review. The tick now also passes `--text-out`, writing `$ORCH_STATE_DIR/switch-review.md`
  beside the JSON, in one step that is whole or fails the step loudly. CLAUDE.md §6 and the
  repo-audit and rail-exercise skills name that file as the measurement a capability verdict cites.
- **`works` needs a durable success.** A terminal outcome is not yet value. `outcome_success` counts
  the terminal outcomes that are a durable success (`feedback.outcome_is_durable_success`, the
  lifecycle's predicate), and the chain breaks at `no_success` when outcomes exist and none
  succeeded. role-adjudicator, runtime-ac-checks and testgen-lane each held one terminal outcome
  and no success.
- **A role run is not acted on.** An edge onto a role run is counted in `acted_on_associations`
  beside `acted_on`, not in it, because the run that took the role's advice carries the
  capability's own edge. On the same copy, offload's `acted_on` falls from 1,803 to 61,
  role-triage's from 293 to 12, role-adjudicator's from 40 to 1, and role-redirect's from 762 to
  0. That last is the defect #540 named: nothing records which run acted on a redirect proposal.

No first break changes on today's data: every row these touch breaks earlier or has unmeasured
demand. No stage, component or surface moved, so the loop diagram is unchanged.

**An experiment member run has no outcome path (2026-10-12, owner decision "trace and wire
winner").** `exp_abcd` records each member run with the `abcd-experiment` capability and never
writes its `outcomes` row, by design: a member commits on an isolated branch, never pushes, and is
judged in `evaluations_v2`. A winner reaches delivery only through `synthesis_promotion`, whose
outcome lands on the SYNTHESIS run. The trace, on a copy of the live Brain and the live mirror's
`experiments/`: 257 promotions; 254 expired in `evaluated` before any synthesis (about 24
experiments a day against a ship gate that launches one a day); 2 synthesized no commit; 1
(strategy434) reached `candidate_ready`; 0 were linked to a delivery, because `link-delivery` is a
CLI with no production caller. Over REST, no `exp/*` branch was ever a PR in the seven repos the
experiments ran on. So abcd-experiment's 12 edges read as "awaiting outcomes" for a wait nothing
could end. `feedback.experiment_member_target` names a member run (identity v2 with a member id,
and not a synthesis run). `capability_effectiveness` counts such an edge as `no_outcome_path`,
never scored, even when another writer put an outcome on it: that is not the arm's delivery. The
value-chain monitor sets it aside at `acted_on`, as it does an association. On the same copy,
abcd-experiment moves from `not_yet_measurable` to `unscoreable`, and its `acted_on` from 12 to 0.
Every other row is identical. No stage, component or surface moved, so the loop diagram is
unchanged.

**A verified winner can now be delivered and scored (2026-10-12, owner decisions).** Two of the
three breaks above are closed. `exp_abcd.followup` passes `default_delivery_discovery` to every
reconcile, so a `candidate_ready` promotion links itself when exactly one PR in its repo names the
candidate id in its body and the keepalive ingester has recorded exactly one run for that PR, the
row `default_outcome_lookup` reads. No PR, two PRs, an unrecorded PR or an unreadable GitHub link
nothing and are reported as `delivery_discovery:<status>`. The candidate body now tells its
deliverer to name the id. The root synthesis run, where `default_mirror_outcome` lands the
delivery, carries the `abcd-experiment` edge; a resume run does not, because it is not the outcome
anchor and an edge there would wait forever. Nothing here opens a PR: publication stays with the
repository's issue/PR workflow. The first real trial is strategy434, delivered to PAEM#2336. The
remaining break, intake, is unchanged. No stage, component or surface moved, so the loop diagram
is unchanged.

**The research lane has its own input (2026-10-12, owner decision: build intake, keep launching
off).** Its only input was `backlog.json`, written by dispatch discovery, which retired with the
dispatch lane: the file was last written 2026-09-17 with 0 issues, and every tick printed
`no_subject`. `backlog.py --research` now writes `research-backlog.json` each tick (orchestrate.sh,
skipped when gh is deferred): open issues that are prioritised or ready, minus trackers, automated
and sync issues, paused or in-progress ones, epic/cross-repo/mechanical ones, anything with an
open PR, and anything `dispatcher._remote_skip_reason` refuses (`agents:paused`, any `agent:*`
writer), with a count per reason; a repo it cannot read is named, never read as empty. The tick
plans research from it only while it is under two hours old, and the TICK-PLAN line prints the
research status beside its inputs and skip reasons. At launch the issue is re-read and refused if it
is no longer open or has since been paused or given a writer, and the subject cooldown is per issue whatever arm set is proposed (Workflows#2710 was
launched 123 times because a new arm mix passed it). Live read after review: 7 eligible of 72 open
issues in 16 repos, 6 of them owned or paused; a shadow tick planned 1. Launching stays off, and it is held by TWO switches, not
one: `ORCH_RESEARCH_ARM=1`, and a non-dry-run research tick, which `tick.main` grants only when
the dispatch lane is live. No stage moved, so the loop diagram is unchanged.

`issue_size_quality` extends that weekly caller with a read-only join: every closed issue in
the 90-day reviewed-fleet population contributes its checkbox-count band, and only PRs merged
by the issue's close can contribute canonical Brain outcomes. PASS uses the final adjudication;
verifier NON_PASS preserves the original verifier judgment. Fleet facts supply merge time for
the existing seven-day durability window; the broke-later column uses fleet-shapes' bad-durability
definition. Every rate carries its sample count; an answered zero is zero, and an absent sample
is unmeasured. Per-repo search pagination and errors remain explicit. The existing weekly command
writes `$ORCH_STATE_DIR/capability-program/size-quality.json`, with one observation per week.
After complete observations span two weeks, compare 16+ tasks to 1–4: at least ten percentage
points of degradation names `file_opener_bucket_wiring_issue`; otherwise it names
`retire_issue_level_claim`. These are evidence dispositions of the issue-level wiring claim,
not lifecycle changes or a new owner approval queue. Partial or stale observations cannot decide.

A redirect `decompose` verdict calls the existing router-chosen shadow DecomposerAgent and
attaches its plan only after the existing epic validator accepts it. `ORCH_ROLE_SHADOW` and the
existing per-cycle cap still govern model transport. No valid plan leaves the deterministic
2–3-slice prompt with a concrete fallback reason. The plan retains the role-run id when present;
this seam never dispatches subtasks or changes live-apply authorization.
The same weekly artifact reports adversarial high-stakes demand from `fleet_shapes`' exact
merged-agent population: workflow/metadata and auth/data paths, or the single 500-line
size threshold, beside the independent label count. Raw paths are counted before top-three
shape grouping, so a small auth change cannot vanish behind docs/tests/scripts. Missing
facts stay unmeasured; measured zero is printed as zero. The production weekly caller
records one match observation per UTC week in the existing capability ledger, never a
review invocation or usefulness verdict. The advisor's shape probe stays unknown until
two complete observations of the same rule are at least seven days apart and the latest
is at most eight days old. Label matching remains a second route after that shadow gate;
`adversarial.main`, the closer's existing label/title matcher and merge authority do not change.


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
rejected records the disagreement and inherits no PASS. The role inherits the acting run's exclusion as well:
an outcome in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` trains neither surface. That set holds an infra
death (`transient_infra`: a run whose done marker says the AGENT died by signal, or since 2026-10-04 one
the provider refused before any work, read only from codex's own `turn.failed` event, the one log that
can show nothing ran. Until 2026-10-04 the dispatch wrapper wrote its claim release's exit status into
that marker instead of the agent's, so a marker without `rc_of` is never read as a death; no stage,
component or surface moved, so the loop diagram is unchanged) and, since 2026-10-04,
`unattributed_closing_pr`: outcome ingest found no PR on any candidate branch of a delegate whose issue
a PR closed, so it records the run as over with no verdict rather than as a failure. A PR closed the issue only if it merged by the time the issue closed: GitHub lists every PR that links an issue, including ones created after it closed, and those are named and not counted (an unread merge time leaves the run unanswered). The same day it gained `unattributed_delegation`: a remote delegation is credited
with a PR only when the PR is its own (the labelled PR, or `{agent}/issue-N`), settled after the label, and
keepalive's own runner records (`runner-reservation`/`runner-completion`/`runner-dispatch` PR markers from
trusted writers) show a completed round of the labelled agent on it since the label; otherwise the settled
run is over with no verdict, and so is a delegation whose issue closed with no PR of its own and no closing PR,
since its agent never had a PR to run on (a local run, which did run, keeps that case's FAIL). Until then ingest credited the first PR on any agent's or lane's branch, and
none of the 9 merged delegation PASS rows was the labelled agent's work (no stage, component or surface
moved, so the loop diagram is unchanged). The merge guard's own outcome patch keeps to that rule: it
credits the latest remote run on the merged PR only if that run is not a delegation (a keepalive run is
its PR), and leaves a delegation with no outcome for ingest to decide, through the one predicate both read
(`outcomes.needs_delegation_guard`). Until 2026-10-04 it credited whichever remote run was latest, and
ingest never re-decides a row already merged and pending durability, so a delegation's merge PASS would
have skipped the guard for good (latent: its 2 rows were keepalive runs; again no stage moved). A local run's candidates start with the branches it pushed from its own worktree, which its completion step reads from git's reflogs (`pushed_branches.py`, the Brain table `run_pushes`): a PR there is credited only if the run opened it (head == branch, created at or after the run started), and a run with no usable record resolves exactly as before (again no stage, component or surface moved). Since 2026-10-05 a local run's own PR closed unmerged is not yet its FAIL: the opener lane rehomes a PR keepalive cannot drive into a replacement at the SAME head commit on a registry branch and closes the old one, so ingest first reads which PRs linked to the run's issue carry the closed PR's head (`outcomes.judge_replacement`, one GraphQL read). Exactly one, merged after the run started, for a PR the run opened (created at or after its start), is the run's merge, pending durability and named `replacement PR #M merged` for the sweep, and the PR's own keepalive run keeps its verifier evidence (`durability_sweep._verifier_claims`); a replacement with different commits is different work and the FAIL stands, and so does an issue GitHub says does not exist; an open carrier waits as the run's own open PR would; an answer that cannot single one out (several carriers, a list cut at the read's limit, a run or closed PR that cannot be placed in time, a PR older than the run) is `unattributed_replacement`, in the excluded set; an unanswered read is retried. Until then 5 of the 9 local closed-unmerged FAILs of the 90 days, all codex, were runs whose head had merged that day in such a replacement. The rows written before are re-judged once each by `outcomes.recheck_replacements` after the local ingest, only for the PR each FAIL was written about, behind a snapshot of the whole outcome row and its `record_outcome` completion events (`outcomes.py --undo-replacement-recheck` restores both), `ORCH_REPLACEMENT_RECHECK=0`, and an owner question whose newest answer stops it when it says "decline"; a row it cannot decide within 7 days keeps its FAIL with the reason. A remote delegation's row is unchanged. Since 2026-10-11 a KEEPALIVE run's own PR closed unmerged gets the same read before its FAIL (`outcomes.keepalive_replacement`): the closed PR's view, then the same query once per issue it links in its repository, judged by the same judge with the run's start at the PR's creation, since a keepalive run IS its PR. But the carrier is recorded as its own keepalive run with its own verdict, so a PR carrying the closed head (merged after it opened, merged before it opened, or still open) ends the run with NO verdict, `carried_by_other_pr`, in the excluded set: a PASS would count one delivery twice. A replacement with different commits, or a PR that links no issue, keeps the FAIL, marked. Both writers read: `keepalive_outcomes` before a row's first outcome, and remote ingest for a keepalive row recorded while its PR was open. A read GitHub did not answer writes nothing until `REPLACEMENT_RECHECK_HORIZON_DAYS` after the PR closed, then keeps the FAIL with the reason; each summary prints `keepalive_replacements` with `waiting` beside `drains_by`. Until then 16 of the 99 named-agent keepalive FAILs of the 90 days were such PRs (14 rehomes: codex 10, claude 3, cursor 1; 2 opened on work that had already merged), and every carrier with a Brain row had its own PASS. The rows written before are re-judged once each by `outcomes.recheck_keepalive_replacements` after the daily keepalive ingest, named-agent rows only, behind its own snapshot (`keepalive_outcomes.py --undo-replacement-recheck`), `ORCH_KEEPALIVE_REPLACEMENT_RECHECK=0`, and its own owner question (again no stage, component or surface moved; the diagram is unchanged). Also since
2026-10-04 the set holds `unjudgeable_merge`: the durability sweep (`durability_sweep.find_merge`) judges only
THE merge a row recorded, a direct PR target or the one merge on the run's own branch, and closes a row with
no such merge as durability `unjudgeable`. Until then such rows were re-skipped on every run while their
`pending` merge scored as a provisional PASS. Every row the sweep leaves pending names its drain (grace, retry,
fix search, acting run), and each run prints pending beside drainable. A role run's verdict still comes over its role edge,
so the sweep judges role runs after their acting runs (again no stage moved; the diagram is unchanged). The same
day the set gained `broke_later_unchecked`. The broke-later check now reads each repo's fix PRs ONCE per run, from
the oldest merge being judged, whole (one merged-date range qualifier, split past GitHub's 1,000-result cap), and
a merge that read did not reach is never `durable`: it waits under the `fix_search` drain and closes as
`unjudgeable` with that class seven days after the first run that missed it. Until then the read was the 200
best matches of a repo's whole history, so in the two repos with more fix PRs than that a merge's own weeks were
read by chance, and 436 rows were judged durable on an unread window; each run now prints how many merges the
read covered and how many it did not (again no stage moved; the diagram is unchanged). The rows already recorded `durable` on the cut-short read are read again by the same sweep, by the owner's decision: each one up to its own verdict's moment, so a fix named by then makes it `broke_later` and otherwise only the note changes. Each run prints `truncated N` until none remain; `durability_sweep.py --undo-truncated-recheck` restores the snapshot it took first, and `ORCH_DURABILITY_TRUNCATED_RECHECK=0` stops it. Since 2026-10-05 the `retry` drain has the same bound on the same per-row clock: a merge lookup or a revert check GitHub never answers (a renamed or deleted repository, lost access) is asked again by every run for `durability_sweep.RETRY_HORIZON_DAYS` from the first run that went unanswered, then closes as `unjudgeable` with `unjudgeable_merge`, the class an answered revert check that cannot decide already gets. Until then such a row stayed pending forever, scoring as a provisional PASS and counted drainable (latent: no row was in it). A clock lives only while its row stays unanswered, and each run prints the next retry close beside drainable (again no stage moved; the diagram is unchanged). Since 2026-10-12 a later fix PR makes a merge `broke_later` only where a sentence naming it STATES a repair (repairs, follows up, reverts, corrects; `durability_sweep.reference_relation`); one that names it without saying how closes it as `unjudgeable` / `broke_later_unstated`, never durable. Until then any `#N` in a fix-titled PR counted, and 87 of the 129 naming pairs behind the 134 live `broke_later` rows were siblings, rebase bases, coverage-round targets or "Related Issues/PRs" links. Those rows are judged again by the same sweep over their own verdict's window, print `bare-reference N` until none remain, share the truncated re-check's undo log, and stop on `ORCH_DURABILITY_STATED_RECHECK=0` (no stage moved; the diagram is unchanged). This keeps role learning separate from normal
implement/review weights while still using the same `relearn_quality()` machinery. Since 2026-09-21
that machinery reads the fleet's keepalive outcomes (`assignment` `assigned`/`none`) as well as the
tool's own `experimental` rows, under the 2026-08-29 broke-later detection floor the receiver rail
already applies, and imputes rather than reads an agent's near-empty cost telemetry; every rationale
records `population=`, `fleet_rows=`, `pre_detection_skipped=` and `telemetry=`
(`ORCH_RELEARN_FLEET_ROWS=0` restores the experimental-only population). Since 2026-09-22 cost has ONE
scale (`feedback.COST_SCALE`, list-price USD): only whole-run sources (`COMPLETE_COST_SOURCES`) with
coverage of at least `MIN_COST_COVERAGE` of an agent's telemetry-eligible runs are measured; partial
traces read as UNMEASURED and are imputed from the agent's own task-type row; a priced cost subsumes
the token term and is charged in row units so the tuned penalty survives the change of currency.

A third, coarser feedback surface re-keys pattern mining to the fleet rather than to this tool's own
completion events (which require a research-subject identity the fleet's work never carries, so the
miner accepted 0 of 5,770 events). `fleet_shapes.py` — a rail, daily — groups merged fleet PRs by
commit type, label family and the path classes they touched, and measures broke-later, hours to merge,
cost and commit count per shape and agent. It is what lets the advisor's `repeated_pattern`
precondition answer from data (codemod-campaign is offered where a shape recurs across repos), and it
is the population a shape-keyed router would learn from. It feeds no weight table directly.

Sandboxed rail-contract verdicts use `fixture_observed` with ranking weight zero. The existing
capability ledger retains these passes separately from production useful outcomes; selection
promotion/demotion ignores the fixture trial's offer and invocation. Historical contract verdicts
are corrected by `capability_propensity.py migrate-fixture-provenance` with idempotent append-only
`fixture_provenance_amendment` events. Original events remain available, and the usage report prints
`production useful N / fixture passes M` in the same 90-day window. This adds no dispatch authority.

Lane-invoked capabilities have a separate explicit bridge to fleet outcomes. `trigger` and `useful`
can record `--deliverable owner/repo#N`; the positive verdict captures the capability version then.
`capability_outcome_bridge` joins that structured key to a keepalive run's exact target and writes
a versioned influence edge only when the run has a completion event. It does not infer attribution
from prose or from a capability merely being offered. `capabilities.py usage` reports all-time
`fleet_edges`, which counts edges rather than independent durable successes.

**A capability inherited from a role takes the role edge's arm (2026-10-11).** A role run's
capability attribution moves onto the run its role edge points at. `record_run` does this when a
dispatch carries an accepted role, and `capability_outcome_bridge.backfill_role_capability_edges`
repairs the history daily. Until 2026-10-11 the bridge joined ANY role edge and wrote an ACCEPTED
capability edge, so a run that went ahead against its role credited the role's capability with its
outcome. On the owner's Brain that was 334 of the 361 edges the bridge had derived:
- 329 came from the rejected edges the tick wrote for refused delegations, which #491's migration
  deleted. No run acted on that advice.
- 5 came from the 3 rejected role edges that remain.

The 329 were 104 of role-triage's 128 accepted-arm terminal outcomes in `capability_effectiveness`,
and 130 of offload's 206.

A rejected role edge onto a RECORDED run now derives a COUNTERFACTUAL capability edge. That is the
control arm `capability_effectiveness` compares the accepted arm against, and until now no
capability had one. The edge carries exactly the observational grade its role edge was given
(`outcomes.backfill_triage_disagreements`), and nothing back-propagates over it. Both inheritance
sites (`feedback._capability_attribution_of` and the bridge) inherit accepted attribution only, and
the bridge's outcome heartbeats (`collect`) read accepted edges only.

A once-only migration in `feedback.py` (`influence_edges.rejected-role-capability-edges-2026-10-11`)
deletes the 334 rows and keeps them in its marker. The bridge's next pass derives the 5 from real
delegations as counterfactual edges. `feedback.py restore-rejected-role-capability-edges` puts the
rows back and removes those replacements. `completion_event_health` orphans fall from 220 to 125.
The rest are the `offload` association edges on advisory role runs kept on 2026-08-21, so
`dry_seam_audit` still fails on them.

The control arm starts at 2 subjects each for role-triage and offload, short of
`capability_effectiveness.MIN_SAMPLE` (5), and every one of them is learning-excluded, so none is
scored (next paragraph). It grows only when a delegation goes ahead against triage, and the dispatch
lane has been shadow since 2026-09-03. No stage, component or surface moved: the counterfactual arm
was already a column of the edge table and the comparison `capability_effectiveness` makes. The
loop diagram is unchanged.

**`capability_effectiveness` reads what became of a run from `outcomes`, and scores no outcome a
learner may not (2026-10-11).** An influence edge says which capability touched which run, and in
which arm. Its `outcome_verdict` and `durability` columns are only a copy of the target's `outcomes`
row, taken when the edge was last propagated, and nothing keeps that copy current:
- propagation runs only when a writer re-records the target, and only over accepted edges;
- the triage grader writes a counterfactual edge's grade once;
- `durability_sweep`'s truncated-recheck restore, `feedback.mark_transient_infra` and the data
  migrations write `outcomes` directly.

The three 2026-10-04 relabels moved 53 outcomes into excluded classes and refreshed none of the 54
accepted capability edges on them. So the reader takes the verdict (adjudicated, else the verifier's)
and the durability from `outcomes` when the report is made, as `capability_causal_evidence` already
does. A target whose `failure_class` is in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` is counted
in `excluded`, beside `terminal`, in either arm, and is never scored. An arm whose
outcomes are all excluded reads `excluded_only`; an arm with unresolved nonexcluded attempts remains
`not_yet_measurable` and visible in `awaiting_outcomes`. The report qualifies its summary as
accepted-arm-only, because the control arm can still have a measured rate. Waiting cannot change
the excluded subset; those outcomes need attribution.

Measured on a copy of the live Brain after #525:

| Arm | Before | After |
|---|---|---|
| role-triage, accepted | 22 terminal on 12 subjects, durable rate 0.167 | none scored; 24 excluded on 12 subjects |
| offload, accepted | 73 terminal on 60 subjects, 0.633 | 46 on 46 subjects, 0.783; 30 excluded on 14 |
| both control arms | 2 terminal each | none scored; 2 and 3 excluded |

Both durable subjects behind role-triage's 0.167 were edges still reading PASS/durable on outcomes
since relabelled `unattributed_delegation`. No stage, component or surface moved, so the loop
diagram is unchanged.

**What an outcome says is defined once, in `feedback`, for both readers of these edges
(2026-10-11).** `capability_causal_evidence` (the lifecycle's tally) and `capability_effectiveness`
read the same edges and the same `outcomes` rows. Until this change each wrote its own predicates,
and they disagreed. Both now call `feedback.outcome_is_terminal` and
`feedback.outcome_is_durable_success`, and the lifecycle's `advisory_self` class comes from
`feedback.role_target_kind`. Re-dumping all 3,778 lifecycle rows on a Brain copy gave byte-identical
output before and after the extraction. Measured on that copy, the disagreements were:
- **An edge onto a role run.** The outcome a role run holds is a copy of its acting run's
  (`_propagate_outcome_lineage_in_conn`), and the acting run carries the same capability's edge. So
  this reader counted one delivery twice, and `_target_of` pooled every `role:<name>:*` run into one
  subject. 2,711 of its 3,653 edges target role runs. Every one of the 27 that held an outcome was
  such a copy, and each had a same-capability edge on its acting run.

  Such an edge is now an ASSOCIATION, in both arms. `advisory_self` is the role's own capability.
  `role_association` is any other capability, such as the `offload` that produced the proposal (the
  2026-08-21 decision). Associations are counted in `associations` and never scored.
- **PASS with `pending` durability.** This reader counted it as terminal and non-durable; the
  lifecycle does not count it as terminal. The rows fall into two populations:
  - 55 merges are selected by `durability_sweep._pending_merged_runs` and sit in its grace window.
    The sweep's 2026-10-11 run printed `undrainable 0`. They are `awaiting_sweep` and resolve on
    their own.
  - 38 agy-runtime-isolation `offload:*` runs never open a PR, and nothing selects them. They are
    `unresolvable`: counted, never scored, never awaiting.
- **A verifier `NON_PASS` under an adjudicated PASS.** `NON_PASS` is the fleet verifier's verdict on
  the merged commit. The lifecycle counted it as a failure; this reader counted it as a durable
  success. Among judged merges, `NON_PASS` regressed 6 of 65 times, against 2 of 79 for a verifier
  PASS and 130 of 4,535 with no verifier verdict.

An accepted arm whose edges can never be scored reads `unscoreable (...)`, never
`not_yet_measurable`. role-redirect is one: all 762 of its edges are on its own role runs, and
nothing records which run acted on a redirect proposal (accepted role edges exist only for triage,
15 of them).

| Accepted arm | #529 | Now |
|---|---|---|
| deliberate-break-verifier | 60/111 subjects, 0.541 | 55/106, 0.519; 5 awaiting the sweep (42 of the 47 pending merges carry `NON_PASS`) |
| offload | 36/46, 0.783; 30 excluded | 31/46, 0.674; 15 excluded, 1,617 associations |
| agy-runtime-isolation | 4/5 on 50 attempts, 0.8 | 4/5 on 12 attempts, 0.8; 38 unresolvable |
| role-triage | 24 excluded, `not_yet_measurable` | 12 excluded on 11 subjects, 281 associations, `excluded_only` |
| role-redirect, -prompt, -decomposer | `not_yet_measurable` | `unscoreable` (762, 8, 4 associations) |

**The lifecycle's tally scores no excluded outcome and no role run (2026-10-12).**
`capability_causal_evidence` refused a class in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` only on
its adjudicated-FAIL branch. A verifier FAIL never read the class, so agy-runtime-isolation's 11
`transient_infra` runs trained its routing prior as failures (β 20, posterior 0.2, where 0.357 was
due). Such a row now tallies `learning_excluded`, ahead of success and failure, and is counted in
readiness. An excluded regression no longer sets `rollback_pending`.

The tally also classes `role_association` (any capability on a role run that is not the role's own)
beside `advisory_self`. Durable subjects are read from the `success` tally rather than the raw flag.
Readiness reads rework, evidence age and a gate's `min_linked_outcomes` from the SCORED rows only
(`success` or `failure`, counted in `scored_outcomes`), and only a `failure` regression sets
`rollback_pending`. On a Brain copy this moved offload's 30 `churn_unattributed` rows to 15
learning-excluded and 15 role associations, and role-triage's 12 to learning-excluded. One readiness
criterion changed: role-triage's `evidence_age`, True to False, because all of its terminal rows are
excluded or role-run copies and it has no scored evidence. Its gate had met `min_linked_outcomes` 5
with 130 such rows in the live ledger. Neither was ready before or after, since neither has a durable
subject.

**Both readers key a subject the same way (2026-10-12).** `feedback.run_subject_id` is the
lifecycle's rule, extracted unchanged: an explicit `causal_context.subject_id`, else a `subject_id`
in the routing metadata, else `runs.target`, else the run id. All 3,798 lifecycle rows on a fresh
Brain copy were byte-identical before and after the extraction.

`capability_effectiveness` used to parse the run id instead. That never handled a local run id
(`stranske__Deliverable-Render_20-gemini-<ns>`), so each attempt at one issue was its own subject.
It also keyed an `offload:<agent>:<ns>` run by its agent. On that copy the change moves one row:
agy-runtime-isolation's stated rate of 0.8 becomes `insufficient_evidence` (4 of 5 subjects). Its
five subjects had been three issues, one of them attempted twice, plus its pooled offload runs.

Every research-program offload run has the same `runs.target`, the automation's working directory.
So both readers now pool those runs into one subject. Neither the run id nor `runs.target` names the
task an offload run did; only its notes do, and notes are prose.

**A run that never merges is scored by its verdict, in a rate of its own (owner decision
2026-10-12).** An offload run opens no PR, so no sweep ever judges its durability. Until this
change its FAIL was a terminal non-durable result and its PASS could never score, so for such runs
only failures counted. On a Brain copy that was agy-runtime-isolation's 8 scored FAILs beside 38
PASSes that never counted. `capability_effectiveness` now classes a run that is not merged, whose
durability is unjudged and which has an outcome, as `no_merge`, whatever its verdict. Such a run
never enters the durable rate, which stays a rate of merges. A separate verdict rate scores it:
distinct subjects with a passing verdict over distinct subjects with any verdict, under the same
`MIN_SAMPLE`. A verifier failure under an adjudicated PASS is a failing verdict. The report adds a
"No-merge verdict" column and a `no_merge_only` list.

On the copy, agy-runtime-isolation's 46 no-merge attempts (38 passing) are all one subject (the
pooling above), so its verdict rate is refused as `insufficient_evidence (1/5 ...)`. None of 2,112
offload runs since 2026-09-01 records which task it was for, so the rate yields a figure only once
the offload writer records a subject.

The lifecycle (`capability_causal_evidence`) still counts a no-merge FAIL as a terminal failure.
This reader differs from it there, on purpose.

No stage, component or surface moved, so the loop diagram is unchanged.

The one place two LLMs touch the same task is an `agent:auto` switch, where the keepalive delegation
policy replaces a stalled agent. `agent_switches.py` (rail, daily) records each such pair in the Brain
table `agent_switches` — commits before and after the switch and the terminal outcome — from two
sources, distinguished by the row's `source`. `label` rows come from the PR's own label timeline.
`policy` rows (2026-09-23) come from the `delegation_log` in the latest keepalive state marker a
trusted writer left on the PR, read in the same GraphQL request as the timeline, because the policy
never relabels: every switch it makes is recorded only there, so a label timeline could never show
one. Each policy row carries that entry's `delegation_source` (`route_weights`, `static` or
`unknown`), which is how the Brain answers whether the exported route weights are consumed at all. A
PR whose state has not been read is reported as unread, never as zero switches. It applies no labels:
the hashed sample that once labelled an auto arm was retired on 2026-09-22, when the opener began
labelling every PR it creates `agent:auto`. The policy still picks the replacement; this surface only
makes the pair observable. (It is not drawn in the loop diagram, which is unchanged.)

## The capability layer — what the tool can do, and how a surface finds it

Consumer declarations in `capabilities.KNOWN_GATES` and `KNOWN_DECLARATIONS` name the
existing acting rail as `file.py:symbol`, including the local handover for external
workers. Resolution proves that a symbol exists, not that it acted. A declaration
with no local consumer carries `consumer_missing: true` and an explanation; this
diagnostic survives reconciliation and grants no admission waiver, lifecycle
promotion, or measured action. The admission gate retains its original grace and
enforcement rules. `test_declared_consumers_resolve_in_the_tree` guards the reviewed
bindings against prose and unresolved symbols.


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

**Rail exercise cadence** is a shadow capability that periodically executes committed read-only or dry-run contracts against disposable fixture copies. It does not add a loop stage or change a rail into a role: it observes the existing deterministic rails, names every skipped contract, and can write machine-observed usefulness evidence only when explicitly armed.

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

**The reported bound set is the OFFERED one, and the rest of the declaration is named (2026-09-23).**
`bound_count` and `bound_capabilities` describe what the answer offers. A declared binding whose row
is not live (`capabilities.NOT_LIVE_STATES`) or has no ledger row on this machine is never offered,
so it is not counted either: it is named beside the count, `bound_not_live` by status and
`bound_unregistered`, so the three always partition what `binding_for` returns. Until then the
classified path inserted live rows only while reporting the raw binding, so a retired bound row read
as bound and never appeared, and the classification-miss path filtered the same rows with its own copy
of the rule; one live map now serves insertion, annotation, the bound-first partition and the report
on every branch. Direct entry follows the same rule: the dispatcher-derived map knows nothing about
the ledger, so a retired target is not offered as `entered_directly`. Latent when fixed (the ledger's
one not-live row was bound nowhere), but a clean runner has 31 bound ids with no ledger row, which the
classified path had been counting as bound.

**A row the code declares exists from the first writing load (2026-10-06).** Every writing load seeds
each `KNOWN_GATES` entry and each `KNOWN_DECLARATIONS` entry that declares a status
(`capabilities.declared_row_ids`), at its declared status and never `active`, so a declared capability
lacks its row on a machine only until that machine's first tick of the tree. Until then such a row
was registered only by a caller its own module wrote, which ran only after deployment. For
`value-chain-monitor` that made the pre-sync verdict refuse the very sync that would deploy the
caller (`docs/MIRROR_SYNC_PATCH.md`). (No stage, component or surface moved; the diagram is
unchanged.)

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

**A projection grades FINDINGS, never the roster, and a changed projection is a first observation
(2026-09-22).** The activation audit's projection carried `reachable_ids` beside `by_defect`, and
`reachable_ids` is the list of HEALTHY capabilities: it moves only when a row is registered or
retired, which is the ledger changing rather than the audit finding anything. Three of its first four
"useful" verdicts were other sessions' registrations or its own false positive appearing, and every
"not useful" verdict counted the healthy rows as findings. Its graded set is now `by_defect` alone; a
capability moving blocked → reachable already leaves it. And because an edit to what a projection
declares, or a report that stops emitting a declared key, makes the next observation incomparable
with the last, `tick_evidence` re-baselines that observation and records no verdict — the same rule
as a first sighting — rather than letting the edit itself mint one.

**And a changed POPULATION is a first observation too (2026-09-22).** The firing monitor listed a row
retired on 2026-09-03 under `never_fired` on every run. Excluding `capabilities.NOT_LIVE_STATES` rows
fixed the report and would have minted a "useful" verdict for the fix, because the finding set's
CONTENT moved while its keys and its projection, the two things the rule above compares, did not. So
a report DECLARES the rule deciding which ledger rows its findings may name, under
`capabilities.FINDING_POPULATION_KEY` (the activation audit declares `live_finding_population()`
extended by its label evidence, and the firing monitor extends it by its held-off rule; both are
below), and `tick_evidence`
re-baselines whenever the declared population
differs from the one recorded with the previous observation, including "none recorded, one declared
now", which is the transition itself. The rule is read from the report being graded, so each
production is judged by the rule it was produced under, and a report that declares none never drifts.
A row retired LATER still leaves the monitor's findings and is still graded: that finding was
answered by a lifecycle action, where this one was answered by an edit to the monitor.

**A deliberately-off capability is held off, not overdue (2026-10-02).** The firing monitor listed
`issue-readiness` (its cadence step retired by default) and `range-lane-rollout` (its declared gate
blocks the delivering path) under `overdue` and `regressed` on every run. A silent live row that a
DECLARED hold keeps off is now named under `held_off`, with the hold, what lifts it, and the
findings it would otherwise be, and the report prints `overdue` and `held off` side by side. Two
declarations hold, and nothing is inferred: `gate_blocks_execution` on the declared branch of
`classify_liveness` (a `gate_reason` alone reaches the same label without saying the path that fires
is blocked), and the retirement of every cadence step that declares the row under the registry's
`capabilities` field, never a match on names. A declared-off row that is firing has no silence to
explain and is not listed. The list is reported, not graded: it moves when someone declares or lifts
a hold, which is the ledger changing (the `reachable_ids` lesson above), and its effect on the graded
lists already is graded. The monitor's declared population names the hold kinds and the three
silence findings they exclude a row from, so this edit re-baselined the grader, while a hold declared
or lifted later moves a graded finding exactly as a retirement does.

**A silence the row's own history contradicts is named as one (2026-10-02).** Only an `invocation`
event moves `last_invocation`, and two producers broke the reading "silent means it did not run": a
heartbeat that records a run's `success` or `failure` and no invocation, and a consult trial
(`capability_propensity.record_trigger`), which writes an invocation through `capabilities.heartbeat`
outside any tick. Four of the five rows the monitor called overdue that day had never recorded a
tick invocation: each one's "last invocation" was a 2026-09-03 trial. Three of the four ran at every
scheduled step with a success-only heartbeat. `stall-watcher` was credited per classified claim, so a
sweep that found no claims recorded nothing. Those heartbeats now record the invocation on the path
that runs, and `silence_evidence` puts the contradicting facts under each silent row: the runs
recorded later than the last invocation's tolerance window, and whether a trial set the field. It
annotates: every finding list is unchanged when the histories are emptied, and the grader projects
findings to their ids, so the edit mints no verdict. `redirect-apply-bootstrap` stays silent on
purpose. Its invocation is an authorised apply, and `switch_review` raises its ON-but-idle drain
from its invocations, so a pass that applies nothing must record nothing.

**Firing findings reach switch review with separate step evidence (2026-10-05).**
`switch_review.firing_regressions` consumes the persisted weekly `capability-firing-monitor.json`,
then reads the current ledger and the cadence registry's declared capability carriers. Each
regressed or overdue row carries the ledger's last invocation, success-stamp age and artifact mtime.
A fresh success stamp later than the heartbeat reads `heartbeat silent, step ran`; a stale stamp
reads `step last ran`. An artifact alone does not prove successful execution, and a cadence stamp
for redirect linking does not prove an authorised apply. Missing or unreadable evidence stays
UNKNOWN. The snapshot date remains visible; a newer heartbeat marks an old finding as history.
No alarm is cleared, no regression rule changes, and no owner question or automated action is added.
Route export (`route_weights_export.py`, schema `orchestrator.route-weights/v1` on branch
`exports/route-weights`) records invocation on every daily run and success only after an actual
publication; the published JSON preserves existing v1 routing values and adds additive
`capacity_schema` (`orchestrator.route-weights-capacity/v1`) plus a `capacity` block with per-agent
`state`, `window`, and `observed_at` from the capacity snapshot's own `generated_at`, plus
an additive `pacing` block for Codex/Claude (fractions, projection, EWMA burn, routing weight,
period bounds, reset inventory and source freshness; no credentials). `pacing.py` defines the
weekly window and constants once. The capacity tick appends source-labelled observations and
snapshots even while shed. Codex weekly percent/reset epochs come from timestamped rollout token-count events;
Claude uses desktop weekly history. Unknown/stale pacing preserves native routability; shared
ccusage totals never become per-agent percentages. Both burn-rate aliases are retained.
`router.select_agent` preserves load/late/state/policy/drain tiers and samples tied pools by
posterior × pacing weight within Codex/Claude only (legacy exploration runs first), then falls
back to zero-weight warn pools, then pacing-shed pools with
`fail-toward-motion`; auth/model/manual holds retain authority. The plan and persisted dispatch
metadata carry all candidate weights and projections. `capacity.py pick-agent` exposes that same
selection for the separate relay follow-up; it prints the agent on stdout and evidence on stderr.
`capacity.py pacing-report` queries completed-period usage, incidents with another pool ok, and
non-Codex dispatch share in constrained versus ok hours; missing populations stay unmeasured.
`switch_review` prints that weekly check. Reset inventory and calibration are local runtime data;
see `docs/capacity-pacing.md`. Semantic change detection includes capacity state, weekly usage and reset changes,
excluding observation clocks, reasons and burn/schedule/projection drift, so a shed or recovered seat republishes without touching posteriors or
`exploration.policy_version`. Rail exercise records invocation without
`--record`, which governs exercise verdicts only.
The learner and exporter share feedback.py's instance DB selection: explicit DB, DB environment,
runtime environment, state environment, then shared default (with private `DB_PATH` patches
supported). CLI `--state-dir` selects output/diagnostics only. Export reads never create or migrate
a DB, and log its absolute path and source version before later work. The publish guard rejects
version zero/invalid and all-false task evidence absolutely. Partial per-task evidence loss requires
explicit `--allow-evidence-regression` regardless of age, preserving source660's original contract.
`route-weights-regression-hold.json` records diagnostics only; recovery or explicit partial override
clears it, and changed regressing task sets restart its timestamp. Capacity-only changes with
identical task evidence publish immediately. Refusals print task count and infinite hours to drain,
plus the required flag for partial loss. Overrides log the reason. Commit provenance records the
resolved DB, source version and Git identity (or `orchestrator-export`). Shadow output remains
available for diagnosis.
Routing economics extends this existing export rail in shadow (`comparative.py`, #676).
Absolute advantage orders agents by durable-PASS posterior; comparative advantage orders
`q[a,k]*V[k] - (lambda[a] + p[a])*c[a,k]`, with V defaulting to one. Lambda is the
capacity dual of the exact continuous LP maximizing `sum x*(q*V-p*c)` subject to
`sum_k c*x <= B` and `sum_a x <= D`. A pure-Python dense two-phase simplex uses Bland's
entering/leaving rule; the nonnegative <= inputs provide an immediately feasible Phase I
slack basis. Phase II slack reduced costs give capacity lambda and demand mu. Slack pools
have exactly zero lambda. Assignment records fractional runs per class/agent, and unmet
demand is explicit. Tied comparative scores prefer the optimal allocation, then agent name.
No downstream consumer or learner order changes: the additive `comparative` block has schema
`orchestrator.route-weights-comparative/v1`, the enclosing export remains `/v1`, and every
ranking entry gains an integer `comparative_rank` or null with a reason. Reserve separation
and exploration policy identity stay intact. This adds no dispatch path or lifecycle.

The adapter prefers `agents[agent]["pacing"]` (#680), falling back to `pools[pool]["pacing"]`.
It accepts numeric `burn_rate_per_day` or `burn_per_day`, honors the shared pacing `stale`
flag, and reports the actual missing agent/key. Legacy inputs without `stale` use
`pacing.STALE_AFTER_SECONDS`; missing/future observation timestamps remain unmeasured.
Unknown `resets_available` does not block measurement: the LP consumes `effective_reserve`.
Budget is remaining-period capacity `B[a] = max(0, (1 - effective_reserve) - used_fraction)`;
negative balances are clamped to zero and labelled `over_reserve`. A missing reserve yields
an unmeasured B with reason `effective_reserve missing`; no reserve is guessed.
`allowed_fraction` is allowance-to-date, retained only as FYI, never used as the budget.
Cost is burn/day divided by observed runs/day, times the local class/agent
mean-token ratio (otherwise 1.0, explicitly imputed). Demand is trailing-window class run
count times seconds remaining divided by window seconds. The common horizon is the earliest
period_end of fully measured agents. Only latest-version quality cells with n_obs >= 20 count,
including classes outside the hosted export. Measured agents alone enter the LP; excluded
agents retain null lambda/rank and reasons, with status `partial` when some are measured.
Other statuses are `optimal`, `infeasible_inputs`, and `inputs_unmeasured`. Subscription price
is zero; Muse or explicitly metered pools require complete list-price observations. Reads
never create or migrate the Brain.

`route_weights_export.py --comparative-report --window-days 14` reads
`$ORCH_STATE_DIR/capacity-ledger.ndjson`, filtering event=pacing_snapshot and using ts.
Legacy pacing-history.ndjson is also read when present. It retains the latest snapshot per
UTC hour and compares Codex/Claude over each snapshot's trailing run window without future
runs. A missing third agent cannot block this pass number. The window verdict requires
`hours_measured/hours_observed >= MIN_MEASURED_HOUR_SHARE` (0.5); failed hours count only
against that share and their reasons print separately. Measured slack prints
`not_testable_slack`; otherwise the report prints classes_differing N. This is observed-hour
coverage, not a claim that absent hours were measured. The CLI returns before heartbeat,
export writes or publication. Comparative republishing compares status, entry ranks, lambda
rounded to three decimals, and cost-source/imputed provenance, excluding diagnostic drift.
Switch review reads the same report and prints one summary line.
The live pass number requires at least one differing class with measured Codex and Claude prices
over the 14-day pacing-history window, checked no earlier than 2026-11-07 and 14 days after
the first #668 state. Fixture success does not meet that live pass number or enable consumption.
Dedup: firing history and its ledger-only silence annotation already existed; the weekly reader
and independent cadence evidence were missing. Existing heartbeat repairs from main are retained.

**And a consult trial is not a switch doing anything (2026-10-02).** The switch review's idle rule
read `last_invocation`, which a trial moves from any session, so one rail-exercise round on a
switch-mapped capability made an idle ON switch read active for `REVIEW_DAYS` and hid the drain its
row carries. It now counts non-trial invocation EVENTS through `capabilities.split_invocations`, the
one split `silence_evidence` reads too, and a `last_invocation` no event recorded counts for
nothing: a causal reconciliation sets it from the influence edges the outcome bridge draws from lane
consult trials' verdicts, which is a trial through a second door. Each idle row names what it left
out beside `idle_days` (`trials_excluded`, `newest_trial_days`, `no_event_days`), and the review
declares the evidence it counts in its finding population, so the edit re-baselines the grader
rather than minting a verdict. Latent when fixed: four of the six switch-mapped capabilities had
only trial invocations, all older than the window.

**The activation audit's own history applies the same rule (2026-09-23).** `progress()` diffs
`reachable_ids` against the last snapshot, and a not-live row is kept out of that set, so a capability
that was reachable and was then retired read as REGRESSED in the scorecard. It is now named under
`retired_since`, by status, from the report's `not_audited`, and `regressed` keeps only the rest. That
reading holds only against a snapshot drawn from the same population, so each snapshot records the
population the report declares, and a snapshot that predates the record, or was drawn from another
population, is an UNKNOWN BASELINE: nothing is compared against it rather than guessing which of its
rows were live. An undeclared population matches nothing, not even another undeclared one. That is
the opposite of `tick_evidence`, where most graded reports declare no population and must stay
comparable; the audit always declares, so an absence here can only be a missing record. A report that
declares none says so and names the fix, because no snapshot of it could clear the state. Otherwise
the drain is the tick's daily `--snapshot`, which records unconditionally, so the state lasts one run.
It is deliberately not re-baselined on every retirement: that would blank the comparison on the day of
a lifecycle action and hide any real regression in the same window.

**The audit's advisor-reach sub-report applies the same rule (2026-09-24).** `advisor_reach` probes
live rows only but computed `regressed` as the baseline minus what it reached, so a RETIRED baseline
capability would have read as a reach regression in the artifact's `advisor_reach` block. Every
baseline id now lands in exactly one of `reachable`, `regressed` and `baseline_not_live` (by status),
on the unreadable-advisor return too; an id with no ledger row stays `regressed`, because silence must
never read as a pass. The direct-entry half still measures the dispatcher-derived MAP:
`direct_entry_targets` and `direct_entry_regressed` never read the machine-local ledger, because that
baseline exists to catch a dispatcher edit that narrows reach, and a retirement must neither mask the
edit nor read as one. A not-live map target is named beside the map as `direct_entry_not_live`, and
the two fields that claim reach, `direct_entry_only` and `total_reachable_count`, count only targets
with registered, live ledger rows. An unregistered target remains in the code-derived map but is not
offerable, so it is not reach. Four of the five declared-baseline ids are map targets as well, so a
retired one left `reachable` and came straight back through the map. None of it reaches the graded
projection:
`advisor_reach_regression` attaches only to live rows, whose membership in `regressed` is unchanged,
so `by_defect` is identical for every input and no verdict is minted. Latent when fixed: the ledger's
one not-live row was in neither baseline.

**A repo the audit could not read is unknown, and the audit declares it (2026-10-04).** Its fleet
label index was a 7-day cache, refreshed with one `gh label list` per fleet repo; a failed read was
skipped and the refresh was then written as fresh regardless. So a refresh during an outage cached
an EMPTY index for a week, a partial failure dropped the failed repos for a week, and a stale but
good index was wiped by the refresh meant to renew it. Against a fake `gh`, every read failing
removed `vocabulary_mismatch` for the week, and the four repos carrying `testing` failing asserted
`label_absent_from_fleet` for a label four of twelve repos carry. Both move `by_defect`, so the
outage itself would have been graded, in both directions. Each repo now keeps its last good labels
and when they were read. A failed read keeps them, is reported under `unread` with its reason, and
is retried by the next audit run, an hour later at the soonest. A repo with no labels at all is `unknown`: `label_coverage` reports it
beside its counts, and `label_absent_from_fleet` is asserted or ruled out only when the answer would
hold whatever that repo carries. The report declares the label rule and the repos never read in its
finding population, so an observation drawn while any repo was never read re-baselines
`tick_evidence` rather than being graded, and the declaration being new made the first report after
the change a first observation too. Last good labels are not declared: a finding drawn from them is
the one the last good read produced, and it is graded as usual. Latent when fixed: the live index
held all twelve repos and `by_defect` was empty.

### A DECLARED BINDING WITH NO CALLER IS THE SAME DEFECT AS NO BINDING

Layer 1 is offered to a surface *by that surface's own consult*. So a surface nothing consults is a
table entry that can never be selected, can never earn evidence, and can never be ranked — the gate
starving its own drain, one level down from the concealment rule above. Measured 2026-08-23: **22 of
43 capabilities were bound to NO surface at all**, and two whole surfaces (`ci`, and every phase of
the tick) had bindings with no caller.

Two callers close that, and the second is the same mechanism as the first — `advise()` plus the
`match` heartbeat, never a second one. (A third row named a `ci` caller, `verify.py`'s
`ci_consult_line()`; it was never committed, and `ci` is declared `NO_BINDING` because nothing
consults it. Corrected 2026-09-23.)

| Surface | Caller | What bounds it |
|---|---|---|
| `tick` (4 capabilities) | `capability_propensity.tick_evidence` (PR #37) | one verdict per capability per UTC day, gated on artifact regeneration → ~1.3/day |
| `tick:<phase>` (14 capabilities) | `capability_advisor.py --consult-tick-phases`, at `ORCH-ANCHOR: tick-phase-consult` | consult text stable per (surface, UTC day); the match heartbeat is idempotent on its digest → 34 events on the first tick of a day, 0 on the other 23. **No verdicts at all**, so #37's ceiling is untouched |

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
must not look alike.

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
critic, scoring five dimensions (`wired` / `usability` / `help_clarity` / `workflow_productivity` /
`truthfulness`) where every sub-8 score must cite screen + click-path + expected-vs-actual (no abstract
findings). `truthfulness` (2026-09-20) asks whether the figures and verdicts on a surface are DERIVED
FROM THE USER'S INPUTS, judged only from the bundle's `substance` block (per computed surface: two
materially different inputs, their outputs, the diff). It exists because the other four score
rendering and completion, and trip-planner rendered the same `$1,160` for every destination and party
size across ten reviews without any of them able to object. Two pieces of it are rails, not judgment:
`ux_review.substance_findings` turns every probe whose output did not move into a severity-4
`fabricated_output` finding regardless of panel scores, and `ux_review.substance_gaps` lists every
computed surface the bundle declares but did not probe, which `gate_decision` prints by name as
`substance_unprobed:N` and refuses to call done. (No new stage or role; the loop diagram is unchanged.)

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
- **Routing.** `route_as="review"` (prior only), `eligible_backends={gemini, codex, cursor, claude, muse}`,
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
Authorisation is a pure function of recorded state — prior lane shown not live, no foreign claim, lineage
stamp present, gate deficit still open, per-target and per-day bounds — never an owner review queue.
Liveness is three-valued: a keepalive lane has no pid, so it counts as not live only when the supervisor
itself reports it stalled or exited, and UNKNOWN is a refusal (until 2026-10-02 a missing pid read as
dead). The lane facts are one function, applied by a free screen before the role is paid and by
`authorize()` after, over the keepalive supervisor's latest stage-2 plan rather than every report file
it ever wrote; each run reports offloads spent beside the candidates that could still be authorised.
The supervisor's planner applies the same function before it counts a candidate as needing a Stage-2
recording. A refused lane is listed with the screen's reasons instead, so the dashboard's recording
warn counts only candidates the bootstrap can drain (until 2026-10-02 it also counted every lane the
screen refuses, which only a manual offload could clear).

Stage-2 disagreement credit compares the applied plan's action and named worker with the baseline;
the raw proposal-action disagreement stays available for historical analysis. Only accepted, synced
outcome links earn this credit. A bad outcome-link row appends its failure to the existing corpus and
does not hide later rows; the linker reports both linked and failed counts. Once the unchanged
20-proposal / 10-synced-outcome / 3-disagreement gate opens, the supervisor writes
`supervised-apply-plan.json` for the next eligible stalled candidate with worker capacity and records
a success heartbeat for producing that artifact. This is a baseline dry-run preview, not a new role
verdict or permission to execute; live apply remains off. With no safe candidate, the report says why
no plan was written. `redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls` exercises the
auth, exited and drift fixtures through offline proposal, named-worker plan and flag-off authorization,
without recording role outcomes or dispatching workers.

## PromptAgent — the second role (built 2026-06-20)

PromptAgent upgrades generic delegation templates without changing deterministic selection. It turns
`{target, goal, task_type, lane, context}` into a strict JSON prompt proposal containing a standalone
`scoped_prompt`, `definition_of_done`, acceptance criteria, validation, expected paths, out-of-scope
boundaries, risks, and confidence.

- **Routing.** `route_as="implement"` (prior only), `eligible_backends={gemini, codex, cursor, claude, vibe, muse}`,
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

- **Routing.** `route_as="review"` (prior only), `eligible_backends={cursor, vibe, gemini, codex, claude, muse}`,
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

### Opener candidate shadow (issue #433)

The existing TriageAgent is also called by `triage_shadow.py` once per active
cycle, independently of the worker dispatch flag. Its snapshot is read from the
opener's priority searches and filtered through current scoped holds and open
PR linkage. The deterministic priority/oldest pick is recorded beside the
advisory top three in `capability-program/triage-shadow.jsonl`. The existing
dispatch-loop role invocation is disabled for that process to prevent paying
for two snapshots. `ORCH_TRIAGE_SHADOW=0` restores the prior caller alone.

The comparison is an observational feedback surface, never an acceptance or
worker-selection edge. Switch review consumes it with separate known outcome
denominators; replay and provider failure cannot produce production evidence.
`outcomes.backfill_triage_disagreements` uses the existing attribution resolvers
and Brain tables, records `source=backfill`, and grades rejected edges without
changing their accepted bit or assigning a counterfactual verdict to the role.
Merge evidence retains pending durability until the usual sweep judges it.
No new role, routing algorithm, learning store or deployment path is introduced.

Issue #583 extends this same shadow: valid live cycles atomically publish
`triage-ranking.json`. `triage_shadow.py --consume --candidates <targets.json>`
can reorder the caller-filtered targets only with `ORCH_TRIAGE_CONSUME=1`, at
least 20 valid live cycles, and a ranking no older than 7200 seconds. Unknown,
expired and disabled input retains rule order; unranked current targets remain
visible. Selection by the deterministic router is unchanged. The flag defaults
off; this repository change does not wire or activate the external opener call.
`triage-consumed.jsonl` records the pick and capability version; `--backfill`
links the first subsequent non-role run only after its completion event exists,
using the existing version-exact Brain consumption API. That run is on the pick
itself or, because the opener's work is recorded on the pull request it opens, on
the first pull request that covers the pick (`outcomes.first_run_for_pick`, deciding
"covers" with `backlog._pr_issue_refs`; one REST read per pull request, at most 20
per call, answers remembered in `triage-pr-covers.jsonl`). No subsequent run after
48 hours records `not_followed` without an edge, but only when every candidate
pull request was read; one that could not be read, or that the bound left unread,
stays pending and then records `unresolved` (`pr_read_failed`, `reads_pending`).
Incomplete runs stay pending.
Switch review prints consumed/followed/not-followed/open and rule agreement.
After merge and manual sync, the program's 2026-10-20/2026-11-03 check owns the
bounded live-consumption experiment; fixture edges are not production outcomes.

## AdjudicatorAgent — the fifth role (built 2026-06-20)

AdjudicatorAgent upgrades disputed-reviewer handling into a callable role. It reviews one blocker/veto
against supplied ground-truth evidence and advises whether to uphold it, reject it, or gather more
evidence.

- **Routing.** `route_as="review"` (prior only), `eligible_backends={gemini, codex, claude, muse}`. Claude is
  reserved by default through `route_role()`, so routine shadow adjudication starts with Gemini/Codex.
- **Shadow only — never emits terminal verifier verdicts or mutates.** It does not produce `PASS`, `FAIL`,
  `BLOCKED`, or `verifier_verdict`, and it does not call merge/label/claim/delegate paths.
- **Rail preservation.** `runtime_ac_panel.adjudicate_panel()` and `adversarial.aggregate_veto()` remain the
  deterministic aggregation math. Automated gate failures still block through the gate/merge rails; an
  adjudicator recommendation is evidence for the orchestrator/human to inspect, not an override.
- **Validation.** The role rejects terminal/mutating keys, requires cited ground-truth refs when upholding
  or rejecting a blocker, requires evidence gaps for `needs_more_evidence`, and rejects next steps that ask
  for mutating execution.

The `adjudicator_retro` rail reads persisted verifier/merge-disposition disputes from the Brain,
requires the trusted merge-bound verifier comment plus diff summary and exact-head gate runs,
and calls the existing router-selected role in bounded, resumable shadow batches. Each role run
is tagged `source=retrospective`; no outcome is created or modified and no verdict is applied.
The report at `$ORCH_STATE_DIR/capability-program/adjudicator-retro.json` grades advice only
against post-detection durability evidence, compares it with the merged-PASS baseline, and names
unmeasured costs. The closer-lane advisor offers this role only when the caller's recorded
`verifier_verdict` and `merge_disposition` disagree; missing facts withhold the offer. Weekly switch
review consumes the saved report and prints its case, agreement, disagreement and cost counts, and
the number of saved rows still awaiting a retry (`retry pending`, 0 when drained, UNKNOWN for a report
that predates the field), then `advice graded N (agree A, disagree D)`: the model's committed decisions on
complete packets that have later truth, counted from the decision itself because the role replaces every
retrospective-source decision with `needs_more_evidence`, so the strict `graded` cannot move (UNKNOWN when
neither the summary nor the rows can give it). Under `--retry` the dispatch limit is spent on those rows before any newer
unsaved dispute (`attempt_order`). A linked issue's acceptance criteria enter the packet only if its
body was not edited after the merge, read from the issue's `lastEditedAt`; GitHub's `updatedAt` moves
on every comment, label and reopen, and refused criteria whose text never changed. A packet left
without criteria stays metadata-only and is never graded. A diff line over 4096 bytes is shortened to
its head and listed in `diff_patch.shortened_lines`, so one generated line (a bookkeeping file's growing
note) cannot clip the whole patch; a patch still over 256000 bytes after that is clipped and, like a
packet with no criteria, stays metadata-only.
This extends the existing role and reporting rails; it does not re-enable tick adjudication. Native CLI terminal token usage is retained
through the existing role record as ledger effort, never complete-source dollars.
Shadow reports label measured, imputed and unmeasured attempts separately on
`feedback.COST_SCALE`. Imputation uses eligible complete-source agent means or
the global median under the existing coverage/plausibility guards; without a
pool the report names UNMEASURED. Estimates never become measured Brain costs,
and late native session receipts replace report estimates without redispatch.

Its separate `--collect-case` rail reads only local evaluated-revision Git blobs. Explicit
`git-path:` acceptance locations use the same bounded literal-path reader as source files,
with lazy fetching and replacement objects disabled. Complete collection covers only the supplied
inventory; inventory exhaustiveness and semantic acceptance remain unverified. Collection never
promotes a shadow packet, writes the Brain, or dispatches adjudication.
The explicit `--prepare-collected-case` extension recollects those same immutable inputs and
places their complete typed contents inside the existing role case's ground-truth field.
It refuses incomplete inventories, existing outputs and oversized whole packets without truncation.
The existing retrospective evidence floor stays in force; this is transport to the existing role,
not semantic admission or a new role/feedback surface. Preparation never dispatches.

The explicit `--judge-collected-case` seam extends this same collection rail. It freshly
collects an exact first-parent squash diff, requires the declared and saved inventories to
match the entire Git diff, and includes before/after regular blobs for modifications. Full
saved criterion text, its body digest, target/head snapshot and ordered artifact-or-owned-UNKNOWN
mappings accompany the contents. Unsupported changes, incomplete collection and oversized
complete prompts fail closed. The collector issues an opaque, digest-bound in-process case to
the existing role; serialized JSON, copied flags and caller-created objects cannot issue one.
Qualified input permits shadow semantic judgment only. Retrospective isolation and source
tagging remain in force, while snapshot authenticity, external exhaustiveness, native cost and
later truth stay independently unverified. Persisted reports retain their evidence floor:
neither this input nor its serialized result authenticates a historical native execution or
changes historical NON_PASS. Default invocation is a dry run; only explicit `--dispatch` calls
the existing role backend and records its native role receipt. This creates no role or ledger.

Native original-case retry uses a separate private SQLite receipt journal, never the persisted
report JSON, as its trust boundary. The journal reserves one `(original case, substantive evidence
revision)` before provider dispatch and commits the raw response, backend ID, usage and error
before JSON parsing, Brain telemetry, cost refresh or a success event. A trusted dispatcher
start callback changes the pre-invocation `reserved` state to `in_flight` with its backend ID
immediately before process invocation. Only an unstarted `reserved` row can be released after
a routing/preflight refusal; missing raw evidence after the start boundary remains
`in_flight UNKNOWN` and blocks automatic redispatch. Validation requires exactly one persisted
raw receipt. This is deliberately not an exactly-once
claim across the provider boundary. The original report is immutable input and any native result
is a separately created projection bound to its source hash, original case ID and full retained
paid-attempt lineage. Restart admission requires both the journal's trusted record and a freshly
issued in-process collected case; copied JSON fields, packet hashes and `persisted_admission`
cannot authenticate it. The spend revision excludes administrative owner/action/order wording
while retaining collected bytes, coverage and UNKNOWN references. Native admission still does not
grade a case without independently established later truth.

The native caller supplies the receipt callback to `dispatcher.offload` itself. It runs after
the isolated provider invocation returns, before dispatcher completion, cost and Brain effects;
callback failure aborts completion and leaves the unresolved reservation UNKNOWN. Provider-owned
agy logs are scrubbed in callback cleanup even when persistence or interruption raises. Native calls
explicitly disable the dispatcher's automatic network retry, so one reservation covers one
invocation. Other offload callers retain their existing retry policy. A returned malformed
proposal is recorded as a consumed validation outcome, not an unresolved provider invocation:
the same evidence revision cannot spend again, while a genuinely new revision remains eligible.
The native projection is separate from canonical cohort publication; native summary values do
not satisfy the original canonical graded-count acceptance by themselves.

Canonical native publication rebuilds every admitted original from the immutable cohort, fresh
collector capabilities, and the producer journal while holding the existing publication lock.
It checks source, journal and canonical generations before atomic replacement. Ordinary
`summary.graded` then describes authenticated distinct original cases; the original summary,
primary records and paid histories remain separately retained. All old and new backend IDs
contribute once to cost coverage, with measured, imputed and UNKNOWN amounts kept distinct.
Generic retrospective run/persistence refuses to overwrite this native publication.

A reviewed exact-revision execution-replay producer may add fresh control evidence to a
collected case. It runs a fixed registered runner in a private immutable checkout, observes
all named baseline/assertion-failure/restoration phases and issues an opaque digest-checked
receipt. Serialized receipts cannot issue authority. Raw output and execution time remain
visible; the spending revision excludes timestamps, temporary paths and equivalent output
noise. Fresh replay does not establish historical native execution, and cannot select a
provider decision, alter later truth, or grant a grade on its own.


### CLI

```bash
python3 src/roles.py route --role adjudicator
python3 src/roles.py adjudicate --case-json case.json [--proposal-json adjudication.json]
python3 src/roles.py adjudicate --case-json case.json --dispatch
python3 src/adjudicator_retro.py --dispatch --limit 5 [--timeout SECONDS]  # resume shadow evidence; --retry retries failed cases; --timeout is the per-dispatch budget (default: the role's own)
```

### Prompt authoring output contracts

`run_prompt_agent(output="dispatch_prompt")` preserves the worker finish contract.
`output="issue_body"` instead validates Why, checkbox Tasks, Acceptance Criteria and Non-Goals.
`run_prompt_batch` chooses one backend for a batch, then records each item as a separate role run
with a shared `batch_id`; proposal validity and downstream outcomes remain per item.
`python3 src/roles.py prompt --batch items.json --output issue_body --dispatch --output-dir bodies`
exports valid Markdown files and a manifest containing role-run IDs and all validator verdicts.
Each manifest item also names `failed_body_file` and `raw_output_file` (or `null`):
failed items retain any authored body as `NNN.failed.md` and any raw backend output as
`NNN.raw.txt`, including bodies withheld after role-recording failure. These evidence files
preserve the text exactly and are never published as the valid-only `NNN.md`. A rejected
proposal survives validation in the item result as `rejected_proposal`; the validator and
role-run admission rules are unchanged. Valid items have `null` in both evidence-file fields.
This extends the existing PromptAgent role with explicit batch-authoring bindings at
`research-program` and `repo-audit:phase-4`. Automatic dispatch roles remain shadow-gated;
these bindings do not enable autonomous dispatch.

### Redirect routing and provenance (issue #424)

Unset/`auto` sweep backends let `route_role("redirect")` select the judgment backend.
Explicit backends remain overrides. A redirect/decompose proposal lacking a worker uses
`router.select_agent(task_type or "implement")`, excluding reserve and backup seats:
`implement` is a task prior, not a registered role. The plan records `agent_source=router`.
No available worker produces a named error and inspection-only commands; direct plan
construction refuses a missing or placeholder agent. Watch classification retains its
recommendation while withholding apply commands until a real worker is selected.
Role-run Brain metadata retains caller `source` and `report_state`, distinguishing
real sweep stalls, live dispatch and historical replay without changing apply authority.

Local `dispatcher.delegate` records a delivery-lane invocation only after its worker
starts: `testgen` → `testgen-lane`, `codemod` → `codemod-campaign`, and
`cross_repo` → `cross-repo-coordination`. The same lane tag travels through the
existing Brain run and versioned influence edge. Standalone delegates are observed
even outside an active tick; prompt construction and dry-run plans only match
work, and refused/unbuildable delegates never count as invocations. This credits
a started worker, not a successful outcome; outcomes keep their durability gates.

### Explicit codemod campaign input (issue #435)

The existing codemod rail can file validated per-repo add-only ignore work orders.
The range rollout rail accepts only that campaign's persisted, freshly revalidated targets,
with scoped/owned and linked-PR exclusions; foreign router assignments are refused.
Its three apply/window controls remain unchanged. Worker assignments carry the actual
work order and existing codemod/range capability tags. A capability-program projection
reports target PR heads/merges and attributed Brain durability/cost, preserving UNKNOWN.
No role, stage, learning store or recurring dispatch policy is added. The diagram's
backlog rail also represents this exact campaign input.

Research intake recovery (#569) reuses dispatcher ownership policy at discovery, scheduler selection and launch. Full issue/PR pages are incomplete and fail closed; feed timestamps must be finite, nonnegative and not future dated. Shadow intake and both existing launch gates remain unchanged.


Consumer admission deadlines use the first invocation's fixed 14-day interval. The value-chain
monitor supplies the same accepted, non-counterfactual edge classifier through
`acted_on_window_index`; its weekly 90-day rolling view remains separate. A late first action
cannot repair a missed admission deadline, and timely evidence remains creditable after it leaves
the weekly view. Missing Brain evidence remains unmeasured. Reads are cached per Brain path,
hour, and first-invocation binding; this change does not activate the installed mirror.
