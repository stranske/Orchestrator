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

`preflight` answers the five declarable requirements immediately and returns the other three as
explicit **obligations** rather than silently skipping them — because silently skipping is how they
got skipped.

### The eight parts

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

Enforcement binds on capabilities registered from 2026-08-21. The 36 pre-gate capabilities are
reported as **legacy debt on every run** and do not fail the suite — a gate that is red on arrival
gets switched off, and then it protects nothing. Legacy rows still print exactly what they are
missing; the exemption lives on the row, never inside the predicates, so debt can never read as
compliance.

### Say which surfaces bind it (or why none does)

The eight parts make a capability *invocable and observable*. They do not make it *findable*. A
capability nothing binds is offered from a 40-plus catalogue queried generically, which is the
measured 13.62% selection condition — built, admitted, and still not chosen.

So when adding or reviving one, name its surfaces in `capability_advisor.SURFACE_BINDINGS` — the
skills or automations for which it should be in the small declared set — with a one-line reason each,
or state that no surface binds it yet and what would change that. Keep a bound set to 3–7 entries;
past ~10 it reintroduces the problem the binding removes, and a selftest enforces the ceiling.

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
