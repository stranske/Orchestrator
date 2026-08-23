# Adding a capability to the Orchestrator

Written 2026-08-21 after reviewing the project's own record: the 2026-07-03 and 2026-07-09 audits,
the 2026-07-08 close-out, the activation-snapshot history, the scheduled-task run log, and the
interactive sessions. This is not a style guide. Every rule below is a specific failure that
happened, cost real time, and — in most cases — happened **again after a documentation rule was
written to prevent it**.

The governing lesson is in that last clause. §0 of `CLAUDE.md` has said "dedup before develop" since
2026-07-08, and August still turned up `watch.py` with no caller, `features.py` with no caller, and
two capabilities with no heartbeat. **A rule that lives only in prose does not survive the next
session.** So the countermeasures here are tests. `test_capability_admission.py` fails the suite;
this document only explains why.

---

## Part 1 — Why these requirements exist

Each requirement below was derived from a real, recorded failure in this deployment: subsystems that
shipped complete and were never invoked; an audit that closed "all findings resolved" while a third
of the capability set could not fire at all; a bounded trial whose scheduled review ran, wrote
nothing, and let a flag revert by timeout for over a month; and a promotion gate that counted rows
in a table nothing wrote for it.

The dated evidence for all nine failure modes is deliberately **not** committed — it names this
instance's repositories, PRs, spend and working constraints. It lives in
`ADDING_CAPABILITIES.local.md` (see `LOCAL_POLICY.md`), and the item-by-item status history lives in
the machine-local improvement log, reached with `python3 improvement_log.py search <term>`.

The governing lesson survives the split, and it is the reason this file has a test file rather than
only prose: **a rule that lives only in a document does not survive the next session.** `CLAUDE.md`
carried a "dedup before develop" rule for six weeks and more dormancy appeared anyway. So the
countermeasures below are enforced by `test_capability_admission.py`; this document only explains
them.

## Part 2 — The approach: what a capability must bring with it

Enforced by `capability_admission.py` + `test_capability_admission.py`. Run **before** writing code:

```bash
python3 capability_admission.py --preflight '{"capability_id":"capability:my-thing", ...}'
```

`preflight` answers the six declarable requirements immediately — including findability, which is the
one most worth learning before the code exists — and returns the other three (caller, heartbeat,
fixture) as explicit **obligations** rather than silently skipping them, because silently skipping is
how they got skipped.

### The nine parts

| # | Requirement | The failure it prevents |
|---|---|---|
| 1 | **Dedup finding recorded in the ledger** | FM4 — six dormant subsystems, then more after a prose rule. The finding goes somewhere durable, not in a plan. |
| 2 | **A caller exists** | The single defect behind every dormant subsystem. |
| 3 | **A heartbeat on the executed path** | `issue-readiness` and `switch-review` both shipped working with no heartbeat, so neither could accrue evidence of its own usefulness. |
| 4 | **A recurrence fixture** | FM6/FM7 — makes "would it fire?" answerable without guessing, and makes the set countable. |
| 5 | **An outcome path** (declared consumer **and** learning sink) | FM2 — `reference-sync-hygiene` had a producer, consumer, kill switch, rollback and 367 events, and its gate still read an empty table. Without this, *"let evidence accumulate"* is an instruction that can never be satisfied. |
| 6 | **A kill switch** | An undeclared switch cannot be found in an emergency. |
| 7 | **A rollback path** | — |
| 8 | **An expiry or a cadence** | Nothing may sit unexamined forever; that is how dormancy survives two audits. |
| 9 | **A surface that can OFFER it** (`findable`) | FM10 — 22 of 43 capabilities were bound to no surface at all, so nothing could offer them and no amount of running could produce evidence for them. All 43 had passed admission. The rule existed — as prose, in this file. |

Enforcement binds on capabilities registered from 2026-08-21, and **each requirement carries its own
date**: findability binds from 2026-08-23 (`capability_admission.REQUIREMENT_ENFORCED_FROM`). The
pre-gate capabilities are reported as **legacy debt on every run** and do not fail the suite — a gate
that is red on arrival gets switched off, and then it protects nothing. The same reasoning applies to
a requirement added later, which is why the date is per-requirement rather than one global cutoff.
Legacy and pre-cutoff rows still print exactly what they are missing; the exemption lives on the row,
never inside the predicates, so debt can never read as compliance.

### Requirement 9 in detail: which surface can OFFER it (or why none can)

The first eight parts make a capability *invocable and observable*. None of them makes it *findable*.
A capability nothing binds is offered from a 40-plus catalogue queried generically, which is the
measured 13.62% selection condition — built, admitted, and still not chosen.

**This was prose in this file until 2026-08-23, and the measurement is what a prose rule is worth
here: 37 of 43 capabilities had no usefulness evidence, and 22 of those were bound to no surface at
all.** It is now the ninth predicate in `capability_admission.REQUIREMENTS`, and
`test_capability_admission.py` fails on it.

So when adding or reviving one, name its surfaces in `capability_advisor.SURFACE_BINDINGS` — the
skills or automations for which it should be in the small declared set — with a one-line reason each.
Keep a bound set to 3–7 entries; past ~10 it reintroduces the problem the binding removes, and a
selftest enforces the ceiling. Run `--preflight` first: findability is **declarable**, so the answer
arrives before the code is written rather than after.

**Three sub-causes, because the fixes differ.** The predicate names which one applies:

| cause | what it means | the fix |
|---|---|---|
| `bound_nowhere` | no surface declares it | add one entry to a surface's 3–7, with its reason |
| `bound_to_unconsulted_surface` | every binding names a surface no caller ever consults | bind a surface listed in `capability_advisor.CONSULT_SITES`, or make that surface consult |
| *(invoked without attribution)* | a surface runs the entrypoint directly and the invocation is credited to nobody | **not checked** — see below |

`CONSULT_SITES` is the other half of a binding, and until 2026-08-23 nothing declared it: `ci` bound
two capabilities and no caller anywhere consults a `ci` surface, while `opener-lane` and `closer-lane`
bind ten between them and both lane prompts consult with **no `--surface` at all**, so the declared
set never reaches the caller it was written for. A declared consult site is a falsifiable claim about
a file — the selftest opens it. Absent on this machine means *unverified*, never refuted; present and
no longer naming its surface is DRIFT and fails.

**A surface has exactly three honest states, and the selftest enforces it**: a caller consults it
(`CONSULT_SITES`), it deliberately binds nothing (`NO_BINDING` with the reason), or it holds bindings
nothing can reach and that is *recorded* (`KNOWN_UNCONSULTED`, with the reason AND the fix). A fourth
state — bindings nothing can reach and nobody wrote down — is what `ci` was, and it is invisible until
a capability is stranded on it, so it now fails `capability_advisor._selftest_findability` naming the
SURFACE rather than only the capability. `KNOWN_UNCONSULTED` is a record, not a waiver: capabilities
bound only there still fail requirement 9. And a fixed entry may not linger — the same selftest fails
on a stale one, because a cached reason that outlives its evidence is this workspace's named defect.

**What requirement 9 deliberately does NOT check**, stated here because a gate that cannot say what
it skipped is the same defect as one that cannot say what would clear it: a surface that invokes the
entrypoint *directly* without surface attribution. The `orchestrate` skill already runs `capacity.py`
while `windowed-capacity-policy`'s heartbeat sits behind `ORCH_CAPABILITY_HEARTBEATS`, which only a
live tick sets — so it is used and entirely uncredited.
`capability_activation_audit.heartbeat_reachable` was checked first and answers a different question:
it reports that row `reachable` via `orchestrate.sh (CLI)`, because it asks whether *some* driver
reaches the heartbeat, not whether *this surface's* invocation is attributed to the surface. Deciding
that needs the surface's own prompt, which lives outside this repository. Likewise
`repo-audit:fix` is *named* by its skill and never *entered* by a run; only trial records can show
that, never a table of files.

Binding is prioritisation, not concealment: an unbound capability is still returned, ranked after the
bound ones. That is deliberate — a capability that could never be selected could never earn the
evidence that would bind it.

### Commitments: a dated promise must leave an artifact

`capability_admission.commitments()` fails on:

* a **citation** to a dated record that does not exist (caught `orchestrate.sh:95`);
* a **deadline** that has passed with no audit record naming its subject (caught both expired
  windows — and through the second one, the dead ingest step).

Bounded trials end by **decision**, not by timeout. If the decision is "hold", that is a decision —
write it down with the criterion that would change it.

### Standing rules that are not yet tests

1. **Ask "can it fire?", never "was it used?"** — `capability_activation_audit` for reachability,
   `capability_recurrence_check` for historical replay. Non-use is a symptom with two causes and
   only one of them is the capability's fault.
2. **Test the mechanism, not the bug.** When fixing a defect a test asserts, rewrite the test around
   a synthetic fixture so it survives the fix. Fixtures may never error: an exception reads exactly
   like a real miss.
3. **Break→revert every correctness-critical assertion.** Prove the test fails without the fix.
   Three real holes in this work were found that way, including a waiver expiry of `9999999999`
   satisfying a "waivers must expire" rule.
4. **Re-run verdicts from the mirror.** `cmp`-clean is not agreement.
5. **Report the whole denominator.** Print the full roster with the ledger count by construction, so
   a subset cannot masquerade as the set.
6. **Never propose retiring a capability whose non-use you have not explained.** Distinguish "no
   demand" from "could not fire" first; they look identical in the data and have opposite fixes.
7. **No human touchpoint that can accumulate.** Every gate here fails to an agent-performable fix.
   Nothing in this document queues anything for the owner.

### The check that would have caught each mode

| Mode | Caught by |
|---|---|
| FM1 completion-as-paperwork | `capability_activation_audit` (reachability), `capability_recurrence_check` (replay) |
| FM2 latched state | break→revert on every blocked verdict; `switch_review` weekly re-raise |
| FM3 dated promises | `capability_admission.commitments()` |
| FM4 prose countermeasures | this file having a test file |
| FM5 convenient denominators | `test_capability_set_coverage.roster()`; `tick_env()` for env-independence |
| FM6 subset answers | `test_capability_set_coverage` — every capability needs a fixture |
| FM7 circular measurement | the activation/recurrence split |
| FM8 tests asserting bugs | fixture-must-not-error assertion; synthetic-fixture rule |
| FM9 wrong tree | mirror re-run after every sync |
| FM10 admitted but unofferable | `capability_admission.req_findable` (requirement 9), plus `capability_advisor.consulting_surfaces()` for the surfaces that strand a binding |
