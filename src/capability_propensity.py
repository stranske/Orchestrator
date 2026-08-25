#!/usr/bin/env python3
"""capability_propensity.py — does triggering a capability actually help, and should we do it more?

THE OPEN LOOP THIS CLOSES. `capability_advisor` names candidate capabilities for a task and records
a `match` against each. Nothing recorded what happened NEXT: whether the candidate was actually
triggered, and whether triggering it helped. So the front door could learn WHICH capabilities match
WHICH work, but never which ones were WORTH matching — and "recommend the useful ones more often"
had no signal to stand on.

DEDUP FINDING (2026-08-22, before writing this). Checked `feedback.py` (outcomes, route_weights,
influence_edges, record_capability_consumption, capability_causal_evidence), `exp_abcd.py`,
`exploration_evidence_plan.py`, `exploration_backfill.py`, `range_lane_rollout.py`,
`capability_activation_audit.py`, `capability_firing_monitor.py`, `CAPABILITY_USEFULNESS.md`.
Findings, and why this is new but small:

* `feedback.route_weights` learns AGENT choice per task_type. There is no analogue for capability
  choice. Different decision, same shape.
* `feedback.record_capability_consumption` IS the rigorous producer->consumer edge, but it demands an
  immutable `capability_version_id` and completion events on both runs. Per the advisor's own
  docstring 0 of the capabilities carry version lineage, so that path cannot carry this signal today
  without fabricating identity — which the learning-loop rules forbid outright.
* `CAPABILITY_USEFULNESS.md` already answers "is this capability useful" — by HAND, once, dated
  2026-08-19, over six named corpora. It is a static judgment, not a loop. This module is the
  continuous version of that same question, and does not replace the analysis.
* NO NEW EVENT TYPES AND NO NEW STORE. `capabilities.EVENT_FIELDS` already has `match`,
  `invocation` and `outcome`; the advisor already writes `match` with `ref="advice:<digest>"`. This
  module writes the other two against the SAME ref and reads all three back. So the natural
  experiment is assembled from the store that already exists.

THE NATURAL EXPERIMENT, and why it is natural. Every advisory call produces a candidate SET under
one `advice:<digest>`. In the ordinary course of work some candidates get triggered and some do not,
for reasons that have nothing to do with this module. That is the experiment: same task, same
context, same candidate set, divergent treatment. Comparing outcomes within a digest needs no
randomisation and no extra work from anyone — it only needs the two missing edges to be recorded.

LATCHED-GATE DISCIPLINE (the failure mode this repo commits most). A propensity that rises with
measured usefulness is a gate whose clear path could trivially be blocked by the thing it measures:
never triggered -> no usefulness evidence -> low propensity -> never triggered. The three answers,
in writing, because a gate that cannot answer them is not ready:

  1. WHAT DECREMENTS IT? `EXPLORATION_FLOOR`. Every capability keeps a non-zero recommendation
     probability regardless of evidence, so evidence can always be acquired. Not "time passes".
  2. CAN THE DRAIN RUN WHILE THE GATE IS CLOSED? Yes, and that is the whole point: the floor applies
     hardest to capabilities with the LEAST evidence, so the population that most needs sampling is
     the population most likely to be sampled.
  3. DOES THE MEASURING WINDOW EQUAL THE DRAINING WINDOW? Yes, by construction: `WINDOW_DAYS` is
     defined once here and consumed by both `usefulness()` and `propensity()`. One constant, so it
     cannot drift into permanent debt.

And the runtime rule: `propensity()` reports its blocking quantity (`evidence_count`) and its
drainable quantity (`explorable`) in the SAME dict, so "0.05" can never read as "be patient" when it
should read "nothing can ever clear this".

WHAT THIS DELIBERATELY DOES NOT DO. It does not dispatch, does not trigger anything itself, and does
not write to the Brain. It ranks advice. Triggering stays with the caller that already had the
authority to trigger, so a bad propensity can misorder a recommendation list and nothing else.

DECLINES ARE A THIRD STATE, NOT A NEGATIVE OUTCOME (2026-08-23). Two independent audit rounds the
same day reached the same conclusion: propensity carried information exactly once because 11 of 13
candidates sat at the uninformative 0.5 floor, and the missing input was not more consults but
DECLINES. Four of nine decisions in one round and 20 of 22 candidate-offers in the other were
reasoned rejections, and none was learnable, because a capability declined on repo-specific grounds
looked IDENTICAL in the ledger to one nobody ever considered (`trig 0, use 0, no 0`).

THE TRAP, and it is the whole difficulty. A decline means the capability was NOT TRIGGERED. Recording
it as an `outcome` would land it in `not_useful` — a false statement that we tried it and it did not
help, about something that never ran, corrupting the exact signal this module exists to sharpen. So a
decline is carried on a `match` event (it WAS offered; that part is true) tagged
`source=capability_decline`, and `experiments()` buckets useful/not_useful from `outcome` events
ONLY. The separation is structural, not conventional: there is no code path by which a decline can
reach the posterior. `propensity()` reports the decline count beside the posterior precisely so the
two can be read together without being mixed.

WHAT A DECLINE FEEDS, exhaustively:
  1. THE DISTINCTION between `declined` (offered, rejected, reason stated) and
     `not_triggered_silently` (offered, ignored) — the reported defect above. The three states
     partition `candidates`, and the selftest asserts that partition.
  2. `propose_demotions` — a binding declined at a surface across `DEMOTION_MIN_DECLINES` runs is a
     demotion candidate. This is far better evidence than silent non-use, so its floor is much lower
     than `DEMOTION_MIN_TRIALS`, and the reasons travel with the proposal.
  3. NOTHING ELSE. It must not, and structurally cannot, move `propensity`.

AND A DECLINE HAS A KIND, because one undifferentiated count licenses the wrong correction. A third
audit round the same day declined 25 offers across six reason classes, and the classes imply OPPOSITE
fixes:

  * `testgen-lane` matched CORRECTLY three times and was structurally impossible each time -- a
    read-only audit has no commit target. Fix: NOTHING.
  * `offload` was declined at nine surfaces, always structurally, because it is declared
    surface-wide and a one-subsystem audit has nothing big enough to hand off. Fix: a precondition,
    or a narrower declaration.
  * `frontend-verifier` was declined on two frontend-less repos and then, on a repo that DOES have a
    display surface, produced the second-strongest finding of that audit -- one the code-reading path
    had missed. Fix: EVALUATE THE CONDITION, do not weaken the binding. Down-weighting it on the two
    negatives alone would have cost that finding.

So `demotable` is a property of the KIND (`DECLINE_KINDS`), declared once and read nowhere else.
Exactly two kinds indict a binding; the rest are counted, reported, and cannot clear the demotion
floor. An unknown kind is refused rather than coerced, because a typo silently becoming
`unspecified` would discard the classification the caller believed it had made.

NO NEW STORE, AGAIN. `capabilities.EVENT_FIELDS` already has `match`; `record_promotion` already
carries a non-match fact on it distinguished by `metadata.source`. A decline follows that precedent
rather than adding an eighth event type or a second table.

AND A VERDICT HAS A PROVENANCE, because "11 of 12 useful" was not a measurement of usefulness
(2026-08-23). The entire corpus was 12 verdicts from three audits, every one SELF-ASSESSED by the
same agent that chose to use the capability, and all three audits were the same model under
near-identical instructions -- selection bias on top of correlated arms, which §2 forbids treating
as independent evidence. So `VERDICT_PROVENANCE` classifies where a verdict came from,
`propensity()` weights BY it (an outcome-corroborated verdict 1.0, a self-report 0.25), correlated
verdicts from one judge arm total 1.0 however many there are (the same reciprocal
`relearn_quality` already applies to research arms, via
`research_subjects.reciprocal_evidence_weights`), and every report states the MIX. Down-weighted,
not banned: self-assessment is the only signal most capabilities have, so excluding it would empty
the dataset. The reporting requirement is as important as the arithmetic -- "0.800" must never again
be readable as independent outcome evidence when it rests on three correlated self-reports.

WHAT THE COUNTERFACTUAL IS, and where it already lives (checked before adding anything).
`influence_edges.counterfactual` is populated on every edge and is the DELIVERY counterfactual --
the capability's contribution was considered and rejected on a run -- and `capability_effectiveness`
already computes `durable_rate(accepted)` against `durable_rate(counterfactual)` from it. It is
keyed on `(capability, run)` in the Brain, and an advisory consult is not a run, so it cannot carry a
per-verdict comparison here. THIS module's counterfactual already exists too: `experiments()`
records, per trial, the candidates named for that exact task and NOT triggered
(`not_triggered` / `not_triggered_silently`), which is the same task, same context, divergent
treatment. `propensity()` now reports that arm beside the posterior. Nothing new was added for it.

AND THERE IS A THIRD ACTION, because "worth having AND broken" was unrepresentable (2026-08-23).
The loop had exactly promote and demote, so the only response to a broken capability was to stop
offering it -- which silences the thing that should be fixed. The live case: `repo-playbook` sits at
one useful and one not-useful verdict, and the Fine-Art-Archive audit documented WHY -- its useful
content is gated behind `task_type: implement/testgen/mechanical`, so a `review` consult receives 308
characters, one clause of which is factually wrong (it tells auditors a repo's default branch is
something it is not). `propose_repair` reads `not_useful` verdicts WITH THEIR EVIDENCE CARRIED
FORWARD, plus the declines whose KIND indicates a DEFECT (`decline_kind_repairable`: `wrong_match`
and `precondition_unmet`, explicitly NOT `no_landing_zone`, which is nobody's fault and whose
capability is working correctly). `repairable` is a SECOND PROPERTY OF THE KIND, declared once beside
`demotable` and read by one lookup -- and the pair that proves they must be separate is
`precondition_unmet`, which is NOT demotable and IS repairable, so before this it was recorded and
inert forever. Report-only, never applied, and it queues nothing for anyone. The drain is
`record_repair`, an ACTION, not the calendar.

AND A DEFECT FOUND IS THE STRONGEST SIGNAL THERE IS, so it must be recordable (2026-08-23).
Instrumented work found SEVEN defects in this system's own code that its author had not. Two were
attributable to a capability and were recorded; the other FIVE were found by the PROCESS -- an audit
noticing that a suppressed surface still offered capabilities, an agent reading this module and
finding a branch that recorded nothing -- so they had no capability to attribute to, became PRs and
prose, and taught the loop nothing. `record_find` accepts a finder that is EITHER a capability (which
feeds that capability's usefulness at `defect_found` provenance -- outcome evidence, not an opinion)
OR a surface (which feeds BINDING QUALITY, the thing that had nowhere to go: "consulting at
repo-audit:phase-1 surfaced a defect in the advisor itself" is evidence about the surface). `defect`
and `artifact` are both REQUIRED and refused when blank -- a CLAIMED find with no artifact is worth
nothing -- and the correlated-arm discount caps it, so ten artifact-backed finds from one arm are
still one observation and only an independent arm moves the number.

    python3 capability_propensity.py report
    python3 capability_propensity.py experiments
    python3 capability_propensity.py decline --capability X --experiment advice:abc --reason "..."
    python3 capability_propensity.py useful --capability X --experiment advice:abc \
        --evidence "..." --provenance outcome_corroborated --judge codex --corroboration "..."
    python3 capability_propensity.py find --defect "..." --artifact "issue #77" \
        --surface repo-audit:phase-1 [--capability X --experiment advice:abc]
    python3 capability_propensity.py binding-quality --surface repo-audit:phase-1
    python3 capability_propensity.py repair
    python3 capability_propensity.py record-repair --capability X --fix "..." --artifact "PR #1"
    python3 capability_propensity.py --json report
    python3 capability_propensity.py --selftest
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import time
from typing import Any

import capabilities
import paths

# KILL SWITCH. Off means the advisor stops ranking by propensity and falls back to its previous
# order; the recording edges still work, so turning this off never destroys evidence -- it only
# stops the evidence from steering anything.
DISABLED = os.environ.get("ORCH_CAPABILITY_PROPENSITY_DISABLED", "").strip() == "1"

# ONE constant, consumed by both the measurement and the drain. A matching pair of literals would
# drift; a shared name cannot.
WINDOW_DAYS = 90
# No capability's recommendation probability may reach zero, or it can never earn evidence again.
EXPLORATION_FLOOR = 0.05
# Beta(1,1) prior: never-triggered sits at 0.5 rather than 0, so an untried capability is optimistic
# under uncertainty instead of buried by it.
PRIOR_USEFUL, PRIOR_TOTAL = 1.0, 2.0
# An outcome heartbeat carries the verdict in metadata under this key.
USEFUL_KEY = "useful"
ADVICE_REF_PREFIX = "advice:"
# A DECLINE rides on a `match` event, tagged by source. `match` is the honest carrier: the capability
# genuinely WAS offered, which is the only claim the event type itself makes. Everything that
# distinguishes a decline lives in metadata, and no reader of `outcome` events can see it -- which is
# why a decline cannot reach the usefulness posterior even by accident.
DECLINE_SOURCE = "capability_decline"
DECLINE_REASON_KEY = "reason"
DECLINE_KIND_KEY = "decline_kind"

# --------------------------------------------------------------------------- verdict provenance
# WHERE A VERDICT CAME FROM, because "11 of 12 useful" was not a measurement of usefulness.
#
# MEASURED, NOT THEORISED (2026-08-23). The whole corpus was 12 verdicts, 11 useful, from three
# audits. EVERY ONE was self-assessed by the same agent that chose to use the capability, and all
# three audits were the same model under near-identical instructions. That is selection bias on top
# of correlated arms, and `CLAUDE.md` §2 forbids treating correlated arms as independent evidence.
# The 11/12 rate is almost certainly optimistic; printing it as though it rested on independent
# outcome evidence is the failure this axis exists to prevent.
#
# THE RULE THIS INHERITS. §2 already mandates an UN-GAMEABLE label for route weights — durability,
# verified success, never green-CI-alone. Capability usefulness inherits it: a verdict corroborated
# by an outcome (the finding survived adversarial review, the issue was filed, the defect was real)
# OUTRANKS an opinion. So provenance is recorded on the row and `propensity()` weights BY it instead
# of counting every verdict equally.
#
# WEIGHT, NOT EXCLUSION. Self-assessment is the only signal that exists for most capabilities today,
# so banning it would empty the dataset — the gate would starve its own drain. It is discounted and
# still counted.
#
# TWO AXES, NEVER COLLAPSED. `verdict_provenance` is WHERE the verdict came from; `verdict_kind`
# (e.g. `observer_output_change`) is WHICH QUESTION was answered. §2 forbids averaging across the
# two KINDS, so the kinds are reported per capability and a mixed-kind posterior is FLAGGED rather
# than silently blended. This axis does not fix that; it must not hide it either.
VERDICT_PROVENANCE_KEY = "verdict_provenance"
VERDICT_JUDGE_KEY = "verdict_judge"
VERDICT_CORROBORATION_KEY = "corroboration"
VERDICT_KIND_KEY = "verdict_kind"

# A LATE-ARRIVING OUTCOME. The verdict write is idempotent on (capability, experiment), so the tier
# chosen at trigger time is permanent — see the block above `unstated_provenance_refusal`. That is
# right for OPINIONS and wrong for OUTCOMES, because `outcome_corroborated` is by construction
# knowable only AFTER the outcome. Measured on the live ledger 2026-08-25: `deliberate-break-verifier`
# reaches 1.0 eight times because a break->revert demonstration finishes inside the same run, while
# `adversarial-review`'s findings sat at `self_reported` 0.25 with their fixes merged that morning
# and no way to say so. Ranking on that mixture measures HOW FAST AN OUTCOME ARRIVES, not how useful
# the capability is — the measuring window (verdict time) and the draining window (outcome time) are
# different windows, which is the latched-gate shape CLAUDE.md names.
#
# So outcomes get their own append-only channel onto an EXISTING trial. Four properties make it
# evidence rather than a dial:
#   * SYMMETRIC. It carries `refutes` as well as `corroborates`. An upgrade-only channel would be a
#     monotonic inflation ratchet — the same hazard CLAUDE.md flags for binding promotion — so the
#     direction that can LOWER a capability's measured usefulness ships in the same commit as the
#     one that raises it, or neither ships.
#   * NEVER SELF-ASSESSED. `late_outcome_provenances()` is derived from VERDICT_PROVENANCE by
#     excluding the self-assessed tiers, so the tiers offered are the tiers accepted and an opinion
#     cannot be re-asserted later to overwrite the first one. Re-stating a self-report is exactly
#     what must stay impossible.
#   * NO STRONGER THAN THE DIRECT PATH. `corroboration` naming the outcome is required for every
#     direction and tier, not only the 1.0 ones. If `outcome_corroborated` with a named outcome is
#     acceptable at trigger time, the same evidence must be equally acceptable an hour later; making
#     the late path harder would re-introduce the latency bias by the back door.
#   * ONE PER TRIAL, AND IT SAYS SO. Idempotent on (capability, experiment) like the verdict itself,
#     so a caller cannot re-roll until the number is agreeable — and a second attempt is REFUSED with
#     the existing attachment named, never silently dropped, which is the defect (#127) that this
#     whole axis was found by.
LATE_OUTCOME_SOURCE = "capability_late_outcome"
# The ledger event type. Distinct from "outcome" so a reader that predates this channel IGNORES the
# amendment rather than misreading it — see the note in `capabilities.EVENT_FIELDS`. This is the
# forward-compatibility property that makes it safe to write amendments while the exec mirror is
# still running pre-change code, which it always is until the owner syncs it by hand (CLAUDE.md §1).
LATE_OUTCOME_EVENT_TYPE = "outcome_amendment"
LATE_OUTCOME_DIRECTION_KEY = "late_outcome_direction"
LATE_OUTCOME_SUPERSEDED_KEY = "late_outcome_superseded_provenance"
LATE_OUTCOME_CORROBORATES = "corroborates"
LATE_OUTCOME_REFUTES = "refutes"
LATE_OUTCOME_DIRECTIONS = {
    LATE_OUTCOME_CORROBORATES: {
        "means": "the outcome CONFIRMS the verdict already recorded for this trial",
        "keeps_verdict": True,
    },
    LATE_OUTCOME_REFUTES: {
        "means": "the outcome CONTRADICTS the verdict already recorded for this trial",
        "keeps_verdict": False,
    },
}

VERDICT_PROVENANCE: dict[str, dict] = {
    # An OUTCOME corroborates the verdict: the finding survived adversarial review, the issue was
    # filed, the fix landed and held. The caller must NAME that outcome (`corroboration`) or the
    # class is refused — otherwise the strongest weight in the table would be self-certifying,
    # which is the green-CI-alone label wearing a new name.
    "outcome_corroborated": {
        "weight": 1.0,
        "requires_corroboration": True,
        "self_assessed": False,
        "means": "a named outcome (survived review, issue filed, fix landed and held) "
        "corroborates the verdict",
    },
    # A DEFECT WAS FOUND, with an artifact naming what was defective. A defect found is an outcome,
    # not an opinion: the artifact is checkable by someone who was not there. Written only by
    # `record_find`, which refuses an unevidenced claim.
    "defect_found": {
        "weight": 1.0,
        "requires_corroboration": True,
        "self_assessed": False,
        "means": "the capability surfaced a defect and the record names the artifact proving it",
    },
    # COMPUTED BY CODE from the capability's own artifacts — the tick's finding-set diff. Nobody's
    # opinion, but the capability's output compared against itself rather than against a delivery,
    # so it is worth less than a corroborated outcome and more than a self-report.
    "machine_observed": {
        "weight": 0.6,
        "requires_corroboration": False,
        "self_assessed": False,
        "means": "computed by code from the capability's own artifacts (a finding-set diff), "
        "not asserted by the agent that used it",
    },
    # THE DEFAULT, and the honest reading of every pre-provenance row: the agent that chose to use
    # the capability also graded it. Kept, discounted, and never presented as measurement.
    "self_reported": {
        "weight": 0.25,
        "requires_corroboration": False,
        "self_assessed": True,
        "means": "self-assessed by the agent that chose to use the capability — selection bias, "
        "and the only signal most capabilities have today",
    },
}
PROVENANCE_DEFAULT = "self_reported"
# THE READ DEFAULT AND THE WRITE DEFAULT ARE DIFFERENT QUESTIONS, AND SHARING ONE CONSTANT MADE THEM
# LOOK LIKE ONE (2026-08-25).
#
# READING a pre-provenance row with no recorded provenance, `PROVENANCE_DEFAULT` is the honest
# answer: that row IS a self-report, nobody said otherwise, and assuming the strongest would recreate
# the 11/12 reading this axis exists to correct. That use stays exactly as it is.
#
# WRITING a new verdict, silence is not an answer — it is an unasked question, and answering it with
# the weakest tier is IRREVERSIBLE. The mechanism is IDEMPOTENCY, not appending, and the difference
# decides what a fixer should build: `record_usefulness` writes through `capabilities.heartbeat`
# under `idempotency_key="useful:<capability>:<experiment>"`, so re-recording the same trial with
# better provenance does not add a second observation and does not double-count — it is DROPPED.
# The call returns False and the CLI prints `recorded: false`, but at exit 0, so a shell caller that
# only tests the exit status sees success. A weak verdict therefore cannot be upgraded AND cannot be
# diluted; there is no partial remedy, and every minute the silent default stands costs provenance
# that cannot be recovered. Pinned by `_selftest_second_verdict_is_dropped_not_appended`.
#
# MEASURED, NOT THEORISED: two of three independent implementation runs on 2026-08-25 recorded real
# `outcome_corroborated` evidence as `self_reported` at weight 0.25 by omitting the flag — and this
# capability's OWN `how_to_use` entry had warned about the default in prose since 2026-08-24. A rule
# that lives only in prose does not survive the next session, so the countermeasure is a refusal.
# `self_reported` is still recordable and still the only signal most capabilities have; it just has
# to be CHOSEN (`--provenance self_reported`) rather than fallen into.
#
# LATCHED-GATE ANSWERS (it is a refusal, so it owes all three):
#   1. WHAT DECREMENTS IT? The caller naming a provenance. A concrete mechanism, available on the
#      very next invocation, and named in the refusal text itself.
#   2. CAN THE DRAIN RUN WHILE IT IS CLOSED? Yes, and this is the whole point: the refusal writes
#      NOTHING, so the retry still records the FIRST observation of that trial. A gate that consumed
#      the experiment id would be the deadlock — refusing the write is what keeps the drain open.
#   3. SAME WINDOW BOTH WAYS? One constant: `VERDICT_PROVENANCE`'s keys are the set the refusal
#      demands and the set `record_usefulness` accepts, so the remedy cannot drift from the check.
PROVENANCE_UNSTATED = "__unstated__"
# THE UNATTRIBUTED ARM. A verdict with no judge identity is not "some unknown independent judge" —
# treating it that way is exactly the assumption that turned three runs of one model into 11
# independent successes. All unattributed verdicts of one provenance about one capability are ONE
# arm, so they total 1.0 no matter how many there are.
UNATTRIBUTED_JUDGE = "unattributed"


def provenance_weight(provenance: str) -> float:
    """How much a verdict of this provenance may weigh. One lookup, so it cannot drift."""
    row = VERDICT_PROVENANCE.get(str(provenance)) or VERDICT_PROVENANCE[PROVENANCE_DEFAULT]
    return float(row["weight"])


def provenance_self_assessed(provenance: str) -> bool:
    """Whether this provenance is the capability's user grading their own choice."""
    row = VERDICT_PROVENANCE.get(str(provenance)) or VERDICT_PROVENANCE[PROVENANCE_DEFAULT]
    return bool(row["self_assessed"])


def unstated_provenance_refusal() -> str:
    """Why an unstated provenance is refused, and what to pass instead. ONE text, two callers.

    Derived from `VERDICT_PROVENANCE` rather than typed out, so the tiers a caller is told about are
    by construction the tiers the write path accepts — a hand-written list here would be free to name
    a value `record_usefulness` rejects, or omit one it takes.
    """
    tiers = ", ".join(
        f"{name} ({row['weight']})" for name, row in sorted(VERDICT_PROVENANCE.items())
    )
    return (
        "a usefulness verdict must STATE its provenance: pass one of "
        f"{tiers}. "
        "Nothing was recorded, so the retry still lands as this trial's FIRST observation — which "
        "is the point: the write is IDEMPOTENT on (capability, experiment), so a verdict filed at "
        "the wrong tier cannot be upgraded later and cannot be diluted either — a second record is "
        "DROPPED, returning False and printing `recorded: false` at exit 0. The first verdict on a "
        "trial is the only one. "
        f"To record an opinion deliberately, say so: --provenance {PROVENANCE_DEFAULT}. "
        "Name --judge in the same breath; verdicts with no judge identity are all treated as ONE "
        "correlated arm, and that is not recoverable either."
    )


def late_outcome_provenances() -> list[str]:
    """Which provenance tiers a LATE outcome may claim: every tier that is not self-assessed.

    DERIVED, not typed out, for the same reason `unstated_provenance_refusal` is: a hand-written
    list here would be free to drift from `VERDICT_PROVENANCE` and offer a tier the write path
    rejects. Excluding the self-assessed tiers is the whole anti-gaming property of this channel —
    an outcome may correct a verdict, an opinion may not.
    """
    return sorted(p for p in VERDICT_PROVENANCE if not provenance_self_assessed(p))


def late_outcome_refusal(reason: str, *, remedy: str) -> str:
    """One text shape for every late-outcome refusal: what was wrong, and what to do instead.

    Every branch that declines to write MUST come through here. A refusal that does not name a
    remedy is the silence this channel exists to remove.
    """
    return f"{reason} {remedy}"


def verdict_provenance(metadata: dict | None) -> str:
    """The provenance of one outcome event. DERIVED, so pre-provenance rows classify honestly.

    Precedence: an explicit `verdict_provenance`, then the machine-computed tick verdict (which
    already stamped `verdict_kind=observer_output_change` before this axis existed), then
    `self_reported`. Defaulting to the WEAKEST class is deliberate: an unlabelled verdict is one
    whose provenance nobody recorded, and assuming the strongest would recreate the 11/12 reading
    this axis exists to correct.
    """
    meta = metadata or {}
    explicit = str(meta.get(VERDICT_PROVENANCE_KEY) or "").strip()
    if explicit in VERDICT_PROVENANCE:
        return explicit
    if str(meta.get(VERDICT_KIND_KEY) or "").strip() == TICK_VERDICT_KIND:
        return "machine_observed"
    return PROVENANCE_DEFAULT


def verdict_judge(metadata: dict | None) -> str:
    """Which arm judged. Unknown is ONE arm, never many — see `UNATTRIBUTED_JUDGE`."""
    return str((metadata or {}).get(VERDICT_JUDGE_KEY) or "").strip() or UNATTRIBUTED_JUDGE


# THE KIND OF DECLINE, because the kinds imply OPPOSITE corrections and one undifferentiated
# "declined" column would license the wrong one. Measured, not theorised: a third audit round on
# 2026-08-23 declined 22 offers across six reason classes, and only some are the binding's fault.
#
# TWO PROPERTIES OF THE KIND, each declared here once and read by exactly one lookup:
#
#   `demotable`  — may this decline weaken the BINDING? (the drain on the binding table)
#   `repairable` — does this decline indicate a DEFECT IN THE CAPABILITY? (the repair channel)
#
# They are deliberately NOT the same question, and neither implies the other. The loop had exactly
# two actions, promote and demote, so it could not represent "this capability is worth having and is
# BROKEN" — and demoting such a capability silences the thing that should be fixed.
# `precondition_unmet` is the case that proves the pair must be separate: it is NOT demotable (the
# fix is to evaluate the condition, not weaken the binding) and IS repairable (an undeclared or
# unevaluated precondition is a defect in the capability), so before `repairable` existed it was
# recorded and INERT FOREVER — 11 of them on the live ledger with no channel that could act on any.
DECLINE_KINDS: dict[str, dict] = {
    # It does not fit this work. The binding or the matcher is wrong -- and "the matcher is wrong" is
    # a defect in the capability, so this is the one kind that is BOTH.
    "wrong_match": {
        "demotable": True,
        "repairable": True,
        "fix": "the matcher or the binding",
    },
    # A CORRECT match declared too broadly. `offload` was offered at 9 of 12 surfaces in one run and
    # declined at all 9, always structurally, because a one-subsystem audit has no read big enough
    # to pay for a dispatch. Narrowing the declaration IS a demotion, so this counts -- and the
    # proposal carries the fix text, because "add a precondition" is the other valid answer.
    # NOT REPAIRABLE, and the reason is arithmetic rather than taste: the fix is to narrow the
    # DECLARATION, which IS the demotion path above. Routing one decline into two opposite actions
    # would double-count it and let the same evidence argue for unbinding and for rebuilding at once.
    "scope_too_small": {
        "demotable": True,
        "repairable": False,
        "fix": "a precondition or a narrower declaration, not a lower rank",
    },
    # A CORRECT match whose declared PRECONDITION does not hold here: the instrument is aimed at
    # another system (`switch-review` audits THIS repo's switches; the gate under audit was in
    # another), or at a surface this repo does not have (`frontend-verifier` on a repo with no UI).
    # NOT DEMOTABLE, and this is the most important row in the table. `frontend-verifier` was
    # declined on two frontend-less repos and then, on a repo that DOES have a display surface,
    # produced the second-strongest finding of that audit -- one the code-reading path had missed.
    # Down-weighting it on the two negatives alone would have cost that finding. THE FIX IS TO
    # EVALUATE THE CONDITION, NOT TO WEAKEN THE BINDING.
    # REPAIRABLE, and this is the load-bearing pair in the table: NOT demotable, so before the
    # repair channel existed there was no action this kind could ever produce. An undeclared or
    # unevaluated precondition is a defect IN THE CAPABILITY -- fixable, and worth fixing precisely
    # because the match itself was correct.
    "precondition_unmet": {
        "demotable": False,
        "repairable": True,
        "fix": "declare and EVALUATE the capability's precondition (applies_to, "
        "an observable surface); the binding is right where it holds",
    },
    # A CORRECT match the deliverable shape made impossible: `testgen-lane` matched correctly three
    # times in a read-only audit with no commit target. THE FIX IS NOTHING, so this must never
    # demote -- down-weighting here would punish a capability for being right.
    # NEITHER demotable NOR repairable. NOBODY'S FAULT: the match was correct and the capability
    # is working correctly; there was simply nowhere to put the result. Proposing a repair here
    # would be as wrong as demoting -- it asserts a defect that does not exist.
    "no_landing_zone": {
        "demotable": False,
        "repairable": False,
        "fix": "nothing — the match was correct and the deliverable had nowhere to "
        "put the result",
    },
    # Correct match held behind a deliberate default-OFF switch or a shadow status. The gate is the
    # subject, and it moves on its own evidence, not on this.
    "gated_off": {
        "demotable": False,
        # Not a defect: the gate is the subject and it moves on its own evidence.
        "repairable": False,
        "fix": "the capability's own gate, on its own evidence",
    },
    # Wanted and not affordable this run ("the one I most regret declining"). Nothing is broken.
    "deferred": {
        "demotable": False,
        "repairable": False,
        "fix": "nothing — wanted, not affordable this run",
    },
    # The caller did not classify it. Recorded, so offered-vs-never-considered still works, and NOT
    # demotable: an unclassified decline that could demote is precisely the wrong correction arriving
    # by default, which is the failure this vocabulary exists to prevent.
    # ...and NOT repairable either, for the identical reason: a repair proposed by DEFAULT, from a
    # decline nobody classified, is the wrong correction arriving unasked.
    "unspecified": {
        "demotable": False,
        "repairable": False,
        "fix": "unknown — the caller did not classify it",
    },
}
DECLINE_KIND_DEFAULT = "unspecified"


def decline_kind_demotable(kind: str) -> bool:
    """Whether a decline of this kind may drive a demotion. One lookup, so it cannot drift."""
    return bool((DECLINE_KINDS.get(str(kind)) or DECLINE_KINDS[DECLINE_KIND_DEFAULT])["demotable"])


def decline_kind_repairable(kind: str) -> bool:
    """Whether a decline of this kind indicates a DEFECT IN THE CAPABILITY. One lookup, one place.

    A second property of the kind, declared once in `DECLINE_KINDS` and read only here — the same
    discipline `demotable` follows, for the same reason: a predicate spelled out at each call site
    drifts, and the two answers here are opposite corrections.
    """
    return bool((DECLINE_KINDS.get(str(kind)) or DECLINE_KINDS[DECLINE_KIND_DEFAULT])["repairable"])


# The surface a decline (or a match) was recorded for. Attribution has to be on the EVENT: the
# advisor's own consults recorded `skill=None` for every `--surface` call, so the control arm of the
# two 2026-08-23 audit rounds was unattributable to `repo-audit:*` at all.
SURFACE_KEY = "surface"


def _events(cap: dict) -> list[dict]:
    return list(cap.get("event_history") or [])


def _experiment_id(event: dict) -> str | None:
    """The advisory digest this event belongs to, or None if it is not experiment-linked."""
    ref = str(event.get("ref") or "")
    return ref if ref.startswith(ADVICE_REF_PREFIX) else None


def _within_window(event: dict, *, now: int, window_days: int) -> bool:
    ts = event.get("timestamp")
    if not isinstance(ts, (int, float)):
        return False
    return (now - float(ts)) <= window_days * 86400


def experiments(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> list[dict]:
    """Assemble every natural experiment: one candidate set, who was triggered, what came of it.

    Reads the ledger with `load_declared` — a WRITING load from verification code is how this repo
    once mutated the live ledger while claiming to inspect it.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    now = capabilities._now() if now is None else now
    trials: dict[str, dict] = {}
    for cap_id, cap in sorted(caps.items()):
        for event in _events(cap):
            exp = _experiment_id(event)
            if not exp or not _within_window(event, now=now, window_days=window_days):
                continue
            trial = trials.setdefault(
                exp,
                {
                    "experiment_id": exp,
                    "candidates": [],
                    "triggered": [],
                    "useful": [],
                    "not_useful": [],
                    "declined": [],
                    "decline_reasons": {},
                    "decline_kinds": {},
                    # PER-VERDICT PROVENANCE, carried the same way the decline metadata already is.
                    # Without it a reader can only count verdicts, and counting 3 correlated
                    # self-reports as 3 independent observations is the defect this fixes.
                    "verdict_provenance": {},
                    "verdict_judges": {},
                    "verdict_kinds": {},
                    # LATE OUTCOMES, collected here and applied AFTER the pass. Applying them
                    # inline would make the result depend on whether the attachment happened to be
                    # walked before or after the verdict it corrects, and event order is not a
                    # contract this assembly should rest on.
                    "late_outcome_events": {},
                    "skills": set(),
                },
            )
            meta = event.get("metadata") or {}
            # SURFACE and SKILL are the same attribution axis read from two keys. `--surface` calls
            # recorded no `skill` at all, so a surface-attributed run was invisible to
            # `propose_demotions` and `missed_selection` -- the control arm existed and could not be
            # located. Reading both keys fixes that without a second attribution field.
            for key in ("skill", SURFACE_KEY):
                if meta.get(key):
                    trial["skills"].add(str(meta[key]))
            etype = event.get("type") or event.get("event_type")
            if etype == "match":
                if cap_id not in trial["candidates"]:
                    trial["candidates"].append(cap_id)
                # A DECLINE. It is a candidate (it was offered) and it is NOT an outcome. This branch
                # is the only place a decline is read, and it sits inside `match` on purpose: the
                # `outcome` branch below cannot see it, so `useful`/`not_useful` cannot absorb it.
                if meta.get("source") == DECLINE_SOURCE:
                    if cap_id not in trial["declined"]:
                        trial["declined"].append(cap_id)
                    reason = str(meta.get(DECLINE_REASON_KEY) or "").strip()
                    if reason:
                        trial["decline_reasons"].setdefault(cap_id, reason)
                    trial["decline_kinds"].setdefault(
                        cap_id, str(meta.get(DECLINE_KIND_KEY) or DECLINE_KIND_DEFAULT)
                    )
            elif etype == "invocation" and cap_id not in trial["triggered"]:
                trial["triggered"].append(cap_id)
            elif etype == LATE_OUTCOME_EVENT_TYPE:
                # NOT a verdict: an outcome that arrived after one. Held aside; applied below.
                trial["late_outcome_events"].setdefault(
                    cap_id,
                    {
                        "direction": str(meta.get(LATE_OUTCOME_DIRECTION_KEY) or ""),
                        "provenance": verdict_provenance(meta),
                        "judge": verdict_judge(meta),
                        "corroboration": str(meta.get(VERDICT_CORROBORATION_KEY) or ""),
                        "evidence": str(meta.get("evidence") or ""),
                    },
                )
            elif etype == "outcome":
                bucket = "useful" if meta.get(USEFUL_KEY) is True else "not_useful"
                if cap_id not in trial[bucket]:
                    trial[bucket].append(cap_id)
                # PROVENANCE travels with the verdict, or the weighting has nothing to read. The
                # first verdict for a capability in a trial wins, matching the idempotency key that
                # already admits at most one.
                trial["verdict_provenance"].setdefault(cap_id, verdict_provenance(meta))
                trial["verdict_judges"].setdefault(cap_id, verdict_judge(meta))
                kind = str(meta.get(VERDICT_KIND_KEY) or "").strip()
                if kind:
                    trial["verdict_kinds"].setdefault(cap_id, kind)
    out = []
    for trial in trials.values():
        trial["skills"] = sorted(trial["skills"])
        # APPLY THE LATE OUTCOMES. Order-independent by construction: every event has been walked
        # before this runs. An attachment with no verdict to correct is an ORPHAN — reported, never
        # promoted into a verdict, because inventing one would credit a capability with an outcome
        # for a trial whose trigger the window can no longer see.
        trial["late_outcomes"] = {}
        trial["late_outcome_orphans"] = {}
        for cap_id, late in sorted(trial.pop("late_outcome_events").items()):
            bucket = next(
                (b for b in ("useful", "not_useful") if cap_id in trial[b]),
                None,
            )
            if bucket is None or late["direction"] not in LATE_OUTCOME_DIRECTIONS:
                trial["late_outcome_orphans"][cap_id] = {
                    **late,
                    "why": (
                        "no verdict in window to correct"
                        if bucket is None
                        else f"unknown direction {late['direction']!r}"
                    ),
                }
                continue
            keeps = LATE_OUTCOME_DIRECTIONS[late["direction"]]["keeps_verdict"]
            after = bucket if keeps else ("not_useful" if bucket == "useful" else "useful")
            if after != bucket:
                trial[bucket].remove(cap_id)
                if cap_id not in trial[after]:
                    trial[after].append(cap_id)
            # OVERWRITE, not setdefault: this is the one place a later observation is ALLOWED to
            # replace the trigger-time tier, and it is why the channel exists. The original event
            # keeps its own provenance in the log, so nothing is lost.
            trial["verdict_provenance"][cap_id] = late["provenance"]
            if late["judge"] and late["judge"] != UNATTRIBUTED_JUDGE:
                trial["verdict_judges"][cap_id] = late["judge"]
            trial["late_outcomes"][cap_id] = {
                **late,
                "superseded_bucket": bucket,
                "verdict_after": after,
            }
        # The CONTROL ARM is what makes this an experiment rather than a tally: candidates that were
        # named for this exact task and NOT triggered. Reporting it is not optional -- an experiment
        # with an unreported control arm is a testimonial.
        trial["not_triggered"] = sorted(set(trial["candidates"]) - set(trial["triggered"]))
        # A capability that was declined and LATER triggered in the same trial ran; the trigger
        # wins. Otherwise a change of mind would be counted as a rejection forever.
        trial["declined"] = sorted(set(trial["declined"]) - set(trial["triggered"]))
        trial["decline_reasons"] = {
            c: r for c, r in sorted(trial["decline_reasons"].items()) if c in trial["declined"]
        }
        trial["decline_kinds"] = {
            c: k for c, k in sorted(trial["decline_kinds"].items()) if c in trial["declined"]
        }
        # THE DEMOTABLE SUBSET, separated here so no downstream reader has to remember which kinds
        # are the binding's fault. `no_landing_zone` was a CORRECT match; it belongs in `declined`
        # and must never appear here.
        trial["declined_demotable"] = sorted(
            c for c in trial["declined"] if decline_kind_demotable(trial["decline_kinds"].get(c))
        )
        # THE THIRD STATE, named. `triggered` + `declined` + `not_triggered_silently` partition
        # `candidates` exactly, which is the property that makes "rejected on stated grounds"
        # distinguishable from "offered and ignored" from "never considered" (not a candidate).
        trial["not_triggered_silently"] = sorted(
            set(trial["not_triggered"]) - set(trial["declined"])
        )
        # RESOLVED means an OUTCOME landed. A decline resolves nothing -- the capability never ran,
        # so there is nothing to have been useful or useless about.
        trial["resolved"] = bool(trial["useful"] or trial["not_useful"])
        out.append(trial)
    return sorted(out, key=lambda t: t["experiment_id"])


def usefulness(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> dict:
    """Per capability: how often named, how often triggered, how often it helped.

    Every rate travels with its denominator. A bare "80% useful" over 5 trials has burned this
    project before under a different name.

    AND EVERY VERDICT TRAVELS WITH ITS PROVENANCE. `usefulness_rate` is the RAW share — kept,
    because it is what the events say — while `effective_useful` / `n_eff` are the same evidence
    after two discounts that `CLAUDE.md` §2 requires: a provenance weight (self-assessment outranked
    by a corroborated outcome) and the correlated-arm reciprocal from
    `research_subjects.reciprocal_evidence_weights` (n verdicts from one judge answering one
    question about one capability are ONE observation, not n). `propensity()` reads the weighted
    numbers; the raw ones stay visible beside them so the discount is inspectable rather than
    implied.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    rows: dict[str, dict] = {
        cap_id: {
            "capability_id": cap_id,
            "candidates": 0,
            "triggered": 0,
            "useful": 0,
            "not_useful": 0,
            "declined": 0,
            "declined_demotable": 0,
            "declines_by_kind": {},
            # THE CONTROL ARM, per capability. Already recorded per trial by `experiments()`; it is
            # counted here so `propensity()` can report the counterfactual beside the posterior
            # without a second pass. Same task, same context, divergent treatment.
            "named_not_triggered": 0,
            "named_not_triggered_silently": 0,
            "status": cap.get("status"),
        }
        for cap_id, cap in sorted(caps.items())
    }
    # One entry per resolved verdict: (capability, useful?, provenance, judge arm, verdict kind).
    verdicts: dict[str, list[tuple[bool, str, str, str]]] = {cap_id: [] for cap_id in rows}
    for trial in experiments(path=path, window_days=window_days, now=now):
        for cap_id in trial["candidates"]:
            if cap_id in rows:
                rows[cap_id]["candidates"] += 1
        for cap_id in trial["triggered"]:
            if cap_id in rows:
                rows[cap_id]["triggered"] += 1
        # DECLINES ARE COUNTED AND KEPT OUT OF EVERY RATE BELOW. `resolved` is deliberately
        # `useful + not_useful` and nothing else, so this column can never leak into the posterior.
        for cap_id in trial["declined"]:
            if cap_id in rows:
                rows[cap_id]["declined"] += 1
                kind = trial["decline_kinds"].get(cap_id, DECLINE_KIND_DEFAULT)
                rows[cap_id]["declines_by_kind"][kind] = (
                    rows[cap_id]["declines_by_kind"].get(kind, 0) + 1
                )
        for cap_id in trial["declined_demotable"]:
            if cap_id in rows:
                rows[cap_id]["declined_demotable"] += 1
        for cap_id in trial["not_triggered"]:
            if cap_id in rows:
                rows[cap_id]["named_not_triggered"] += 1
        for cap_id in trial["not_triggered_silently"]:
            if cap_id in rows:
                rows[cap_id]["named_not_triggered_silently"] += 1
        for key in ("useful", "not_useful"):
            for cap_id in trial[key]:
                if cap_id in rows:
                    rows[cap_id][key] += 1
                    verdicts[cap_id].append(
                        (
                            key == "useful",
                            trial["verdict_provenance"].get(cap_id, PROVENANCE_DEFAULT),
                            trial["verdict_judges"].get(cap_id, UNATTRIBUTED_JUDGE),
                            trial["verdict_kinds"].get(cap_id, ""),
                        )
                    )
    for cap_id, row in rows.items():
        row["declines_by_kind"] = dict(sorted(row["declines_by_kind"].items()))
        resolved = row["useful"] + row["not_useful"]
        row["resolved"] = resolved
        row["trigger_rate"] = (row["triggered"] / row["candidates"]) if row["candidates"] else None
        row["usefulness_rate"] = (row["useful"] / resolved) if resolved else None
        row.update(_weigh_verdicts(verdicts[cap_id]))
    return {
        "window_days": window_days,
        "capability_count": len(rows),
        "rows": {k: v for k, v in sorted(rows.items())},
    }


def _weigh_verdicts(verdicts: list[tuple[bool, str, str, str]]) -> dict:
    """Provenance weight x correlated-arm discount, for one capability's resolved verdicts.

    TWO DISCOUNTS, in this order, and neither is a second scheme:

      1. PROVENANCE — `VERDICT_PROVENANCE[...]["weight"]`. An outcome-corroborated verdict weighs
         1.0; a self-report weighs 0.25. §2's un-gameable-label rule, applied to capability
         usefulness the way it already applies to route weights.
      2. CORRELATION — `research_subjects.reciprocal_evidence_weights`, the SAME function
         `relearn_quality` uses for research arms, keyed here on `(judge arm, provenance)`. Three
         verdicts from one model answering one question about one capability total 1.0, not 3.
         Keyed on the pair rather than the judge alone because an outcome-corroborated verdict's
         independence rests on the NAMED OUTCOME, not on who noticed it; sharing an arm with three
         self-reports must not discount it.

    Falls back to the local reciprocal if `research_subjects` cannot be imported (it reaches the
    Brain at module scope), because the weighting must hold on a machine with no database — but the
    formula is the same one, not a second one.
    """
    if not verdicts:
        return {
            "effective_useful": 0.0,
            "n_eff": 0.0,
            "weighted_usefulness_rate": None,
            "provenance_mix": {},
            "judge_arms": [],
            "independent_arms": 0,
            "outcome_derived": 0,
            "self_reported": 0,
            "self_reported_share": None,
            "verdict_kinds": {},
            "mixed_verdict_kinds": False,
        }
    groups: dict[tuple[str, str], list[int]] = {}
    for index, (_useful, prov, judge, _kind) in enumerate(verdicts):
        groups.setdefault((judge, prov), []).append(index)
    try:
        import research_subjects

        corr = research_subjects.reciprocal_evidence_weights(groups)
    except Exception:  # noqa: BLE001
        corr = {i: 1.0 / len(ix) for ix in groups.values() for i in ix}
    eff_useful = 0.0
    n_eff = 0.0
    mix: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for index, (useful, prov, _judge, kind) in enumerate(verdicts):
        weight = provenance_weight(prov) * float(corr.get(index, 1.0))
        n_eff += weight
        if useful:
            eff_useful += weight
        mix[prov] = mix.get(prov, 0) + 1
        label = kind or "unstated"
        kinds[label] = kinds.get(label, 0) + 1
    self_n = sum(n for p, n in mix.items() if provenance_self_assessed(p))
    return {
        "effective_useful": round(eff_useful, 4),
        "n_eff": round(n_eff, 4),
        "weighted_usefulness_rate": round(eff_useful / n_eff, 4) if n_eff else None,
        "provenance_mix": dict(sorted(mix.items())),
        # BOTH quantities: which arms spoke, and how many of them were actually distinct. "3
        # verdicts" and "3 verdicts from 1 arm" are opposite readings.
        "judge_arms": sorted({judge for _u, _p, judge, _k in verdicts}),
        "independent_arms": len(groups),
        "outcome_derived": len(verdicts) - self_n,
        "self_reported": self_n,
        "self_reported_share": round(self_n / len(verdicts), 4),
        # §2 forbids AVERAGING across verdict kinds. This axis cannot unmix them, so it reports
        # them and flags the mixture rather than letting it pass as one rate.
        "verdict_kinds": dict(sorted(kinds.items())),
        "mixed_verdict_kinds": len(kinds) > 1,
    }


def propensity(
    capability_id: str, *, path=None, window_days: int = WINDOW_DAYS, now: int | None = None
) -> dict:
    """How strongly should this capability be recommended when it matches? With BOTH quantities.

    Posterior mean of a Beta(1,1)-Bernoulli over resolved outcomes, floored so evidence can always
    be acquired. `evidence_count` is the blocking quantity and `explorable` the drainable one, in
    one dict, because "0.05" alone reads as patience when it may mean deadlock.

    THE POSTERIOR IS PROVENANCE-WEIGHTED, and saying so is half the point. It reads `n_eff` /
    `effective_useful` from `usefulness()`, not the raw counts: a self-report weighs 0.25 and
    correlated verdicts from one judge arm total 1.0 however many there are. So a capability with
    three same-model self-reports lands NEAR THE PRIOR rather than at 1.0, and the returned
    `provenance_mix`, `independent_arms` and `self_reported_share` say why. "0.800" must never again
    be readable as independent outcome evidence when it rests on three correlated self-reports.

    LATCHED-GATE ANSWERS for the weighting (it discounts evidence, so it owes all three in writing):
      1. WHAT DECREMENTS IT? Recording ONE verdict from a different judge arm, or one verdict of a
         non-self-assessed provenance — `record_usefulness(..., judge=..., provenance=...)` and
         `record_find`. Named mechanisms, not "time passes" and not "someone notices".
      2. CAN THE DRAIN RUN WHILE IT IS CLOSED? Yes, and the direction is favourable: the discount
         compresses a posterior TOWARDS the 0.5 prior, never below `EXPLORATION_FLOOR`, and never
         changes the candidate SET. A capability whose only evidence is self-reported therefore
         stays offered — if anything more often than its raw rate would justify — so it can keep
         earning the independent verdict that clears the discount.
      3. SAME WINDOW BOTH WAYS? Yes: `WINDOW_DAYS`, the one constant, bounds the verdicts counted
         and the verdicts that can drain the mix. There is no second literal to drift.
    """
    stats = usefulness(path=path, window_days=window_days, now=now)["rows"]
    row = stats.get(capability_id)
    if row is None:
        raise ValueError(f"unknown capability: {capability_id}")
    resolved = row["resolved"]
    n_eff = float(row["n_eff"])
    posterior = (row["effective_useful"] + PRIOR_USEFUL) / (n_eff + PRIOR_TOTAL)
    value = max(EXPLORATION_FLOOR, posterior)
    return {
        "capability_id": capability_id,
        "propensity": round(value, 4),
        "posterior_mean": round(posterior, 4),
        "floored": value > posterior,
        # BLOCKING quantity and DRAINABLE quantity, together, always.
        "evidence_count": resolved,
        # THE SAME EVIDENCE AFTER THE DISCOUNTS, beside the raw count. `evidence_count` 3 with
        # `evidence_weight` 0.25 is the honest shape of "three correlated self-reports"; printing
        # only the first is how 11/12 came to look like a measurement.
        "evidence_weight": row["n_eff"],
        "effective_useful": row["effective_useful"],
        "raw_usefulness_rate": row["usefulness_rate"],
        "weighted_usefulness_rate": row["weighted_usefulness_rate"],
        "provenance_mix": dict(row["provenance_mix"]),
        "independent_arms": row["independent_arms"],
        "judge_arms": list(row["judge_arms"]),
        "outcome_derived_verdicts": row["outcome_derived"],
        "self_reported_verdicts": row["self_reported"],
        "self_reported_share": row["self_reported_share"],
        # §2: never average across verdict KINDS. Flagged, not blended away.
        "verdict_kinds": dict(row["verdict_kinds"]),
        "mixed_verdict_kinds": row["mixed_verdict_kinds"],
        # THE COUNTERFACTUAL ARM, reported beside the posterior and never mixed into it. These are
        # trials where this capability was named for the exact same task and NOT triggered. It is
        # the natural comparison the module was built on; `influence_edges.counterfactual` is the
        # DELIVERY counterfactual and is already consumed by `capability_effectiveness`, so it is
        # not duplicated here (see the module docstring).
        "counterfactual_named_not_triggered": row["named_not_triggered"],
        "counterfactual_silent": row["named_not_triggered_silently"],
        # REPORTED, NEVER MIXED. "0.5 with 0 evidence" and "0.5 with 0 evidence and 4 reasoned
        # rejections" are opposite findings that were indistinguishable until declines existed.
        # Printing them side by side is the point; the posterior above is computed from `resolved`,
        # which is `useful + not_useful` and cannot include this number.
        "declines": row["declined"],
        # THE KIND SPLIT, not just the count. "9 declines" invites narrowing a binding; "9 declines,
        # 0 of them the binding's fault" forbids it. A third audit round found two of its three
        # decline classes were CORRECT matches, so a bare number licenses the wrong fix two times
        # in three.
        "declines_by_kind": dict(row["declines_by_kind"]),
        "declines_demotable": row["declined_demotable"],
        "declines_excluded_from_posterior": True,
        # DERIVED, never asserted. This field was a hardcoded True until a break-test removed the
        # floor and it still claimed the gate was drainable -- a predicate that cannot fail is
        # decoration, and decoration is exactly what this repo's prose rules turned out to be.
        "explorable": value >= EXPLORATION_FLOOR,
        "exploration_floor": EXPLORATION_FLOOR,
        "basis": (
            "no resolved outcomes yet — optimistic prior plus an unconditional floor, so this "
            "can still be sampled and can therefore still earn evidence"
            if not resolved
            else (
                f"{row['useful']} of {resolved} resolved trials were useful; "
                f"provenance {row['provenance_mix']} across {row['independent_arms']} independent "
                f"judge arm(s) discounts that to {row['effective_useful']:.2f} of "
                f"{row['n_eff']:.2f} effective observations"
                + (
                    " — SELF-REPORTED ONLY, so this is an opinion mix, not outcome evidence"
                    if not row["outcome_derived"]
                    else ""
                )
                + (
                    f" — MIXED VERDICT KINDS {row['verdict_kinds']}, which §2 forbids averaging: "
                    "read them separately"
                    if row["mixed_verdict_kinds"]
                    else ""
                )
            )
        )
        + (
            f"; declined with a stated reason {row['declined']} time(s) "
            f"({row['declined_demotable']} of them attributable to the binding), which is "
            f"recorded but never scored — it never ran"
            if row["declined"]
            else ""
        ),
        "window_days": window_days,
    }


def rank(entries: list[dict], *, path=None, window_days: int = WINDOW_DAYS) -> list[dict]:
    """Annotate advisory candidates with propensity and order them by it. THE PRODUCTION PATH.

    One call per advisory question rather than one per candidate, so the heartbeat credits the
    decision that was actually made and the ledger does not accrue N events for one question.

    ORDER ONLY. The candidate SET is never changed, so the worst a wrong propensity can do is put a
    good suggestion second. That containment is deliberate: this module ranks advice, it does not
    decide what runs.

    AND THE CALLER RECEIVES THE PROVENANCE, not just the number. The reporting requirement is as
    important as the arithmetic: an entry carrying `propensity: 0.8` and nothing else invites being
    read as measured, so every ranked entry also carries the provenance mix, the independent-arm
    count and the self-reported share. Asserted through what a CALLER receives, because a helper
    that computes the right thing while the caller sees the old thing is a bug this repo has shipped.
    """
    if DISABLED or not entries:
        return entries
    scored = []
    for entry in entries:
        prop = propensity(entry["capability_id"], path=path, window_days=window_days)
        entry["propensity"] = prop["propensity"]
        entry["propensity_basis"] = prop["basis"]
        entry["usefulness_evidence_count"] = prop["evidence_count"]
        entry["propensity_floored"] = prop["floored"]
        # PROVENANCE, on the entry the caller actually reads.
        entry["usefulness_evidence_weight"] = prop["evidence_weight"]
        entry["usefulness_provenance_mix"] = prop["provenance_mix"]
        entry["usefulness_independent_arms"] = prop["independent_arms"]
        entry["usefulness_self_reported_share"] = prop["self_reported_share"]
        entry["usefulness_outcome_derived"] = prop["outcome_derived_verdicts"]
        scored.append(entry)
    scored.sort(key=lambda e: (-e["propensity"], e["capability_id"]))
    # Credited on the executed path, not only from the CLI: a capability whose heartbeat sits behind
    # a manual command reads as dormant no matter how often production uses it.
    _capability_heartbeat("invocation", f"rank:{len(scored)}")
    with_evidence = sum(1 for e in scored if e["usefulness_evidence_count"])
    outcome_derived = sum(1 for e in scored if e["usefulness_outcome_derived"])
    # BOTH quantities in one place: how many of these rankings rest on measurement, and how many on
    # the prior. A ranked list that does not say which is which invites being trusted too early.
    # And a THIRD: how many rest on anything other than the user's own opinion of their own choice.
    _capability_heartbeat(
        "output", f"rank:evidence:{with_evidence}/{len(scored)}:outcome_derived:{outcome_derived}"
    )
    return scored


def _capability_heartbeat(event_type: str, ref: str) -> None:
    """This capability's own production heartbeat. Absent one, it cannot accrue evidence of its own
    usefulness -- the exact defect `issue-readiness` and `switch-review` both shipped with."""
    try:
        capabilities.production_heartbeat("capability-propensity", event_type, ref=ref)
    except Exception:  # noqa: BLE001
        pass


def report(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> dict:
    """The whole denominator, ranked, with the unresolved population named rather than dropped."""
    stats = usefulness(path=path, window_days=window_days, now=now)
    trials = experiments(path=path, window_days=window_days, now=now)
    ranked = []
    for cap_id, row in stats["rows"].items():
        prop = propensity(cap_id, path=path, window_days=window_days, now=now)
        ranked.append(
            {
                **row,
                "propensity": prop["propensity"],
                "floored": prop["floored"],
                "basis": prop["basis"],
            }
        )
    ranked.sort(key=lambda r: (-r["propensity"], -r["resolved"], r["capability_id"]))
    resolved_caps = [r["capability_id"] for r in ranked if r["resolved"]]
    # THE PROVENANCE MIX OF THE WHOLE CORPUS, in the headline. This is the number that makes the
    # arithmetic honest: on 2026-08-23 it was 12 verdicts, 12 of them self_reported, 0 outcome-
    # derived, from 1 judge arm per capability — a fact the old headline could not express, so
    # "11 of 12 useful" read as a measurement of usefulness rather than of one model's opinion.
    corpus_mix: dict[str, int] = {}
    for row in ranked:
        for prov, n in (row["provenance_mix"] or {}).items():
            corpus_mix[prov] = corpus_mix.get(prov, 0) + n
    verdict_total = sum(corpus_mix.values())
    self_total = sum(n for p, n in corpus_mix.items() if provenance_self_assessed(p))
    recorded_finds = finds(path=path, window_days=window_days, now=now)
    repairs = propose_repair(path=path, window_days=window_days, now=now)
    markers = repair_markers(path=path, window_days=window_days, now=now)
    return {
        "window_days": window_days,
        "capability_count": stats["capability_count"],
        "experiment_count": len(trials),
        "resolved_experiment_count": sum(1 for t in trials if t["resolved"]),
        # THE HONEST HEADLINE. If this is 0 the loop is not learning yet, and every propensity below
        # is the prior rather than a measurement. Saying so is the difference between this and a
        # dashboard that looks informative while reporting nothing.
        "capabilities_with_evidence": len(resolved_caps),
        "capabilities_without_evidence": stats["capability_count"] - len(resolved_caps),
        # THE PROVENANCE MIX. Never omit this beside a usefulness rate: the two together are the
        # only honest reading, and the first without the second is what this axis exists to stop.
        "verdict_count": verdict_total,
        "verdicts_by_provenance": dict(sorted(corpus_mix.items())),
        # THE LATE-OUTCOME CHANNEL, both directions and its failure mode, in the same place. A
        # channel that can only be seen by reading individual trials is a channel nobody audits —
        # and the two numbers must be reported TOGETHER, because `corroborated` alone climbing while
        # `refuted` stays at zero is the signature of a ratchet rather than a measurement.
        "late_outcomes_corroborating": sum(
            1
            for t in trials
            for row in t["late_outcomes"].values()
            if row["direction"] == LATE_OUTCOME_CORROBORATES
        ),
        "late_outcomes_refuting": sum(
            1
            for t in trials
            for row in t["late_outcomes"].values()
            if row["direction"] == LATE_OUTCOME_REFUTES
        ),
        # ORPHANS: attachments whose verdict the window can no longer see. Not an error and not
        # silence — a non-zero count here means outcomes are arriving later than the window is wide,
        # which is a statement about the WINDOW, not about the capabilities.
        "late_outcomes_orphaned": sum(len(t["late_outcome_orphans"]) for t in trials),
        "verdicts_self_reported": self_total,
        "verdicts_outcome_derived": verdict_total - self_total,
        "verdicts_self_reported_share": (
            round(self_total / verdict_total, 4) if verdict_total else None
        ),
        # THE DRAINABLE QUANTITY for the provenance discount: how many capabilities have any
        # evidence that is not their user's own opinion. 0 means every number below is opinion.
        "capabilities_with_outcome_derived_evidence": sum(
            1 for r in ranked if r["outcome_derived"]
        ),
        "capabilities_with_multiple_judge_arms": sum(
            1 for r in ranked if r["independent_arms"] > 1
        ),
        # THE POPULATION THE 0.5 FLOOR USED TO HIDE. A capability with no outcome evidence but
        # several reasoned rejections is not "unmeasured"; it is measured on a different axis. This
        # count says how much of the un-evidenced population is actually of that kind.
        "capabilities_declined_with_reason": sum(1 for r in ranked if r["declined"]),
        "decline_count": sum(len(t["declined"]) for t in trials),
        # Both quantities again: how many declines exist, and how many of them are actually a
        # statement about the binding rather than about the work's shape.
        "decline_demotable_count": sum(len(t["declined_demotable"]) for t in trials),
        # DEFECT FINDS. Counted here so a run that surfaced defects cannot read as a quiet one.
        # The capability-attributed half is ALSO in `verdicts_by_provenance` as `defect_found`; the
        # surface-attributed half is in no rate at all, by construction, and is the binding-quality
        # evidence that previously had nowhere to go.
        "find_count": len(recorded_finds),
        "finds_by_finder_kind": {
            k: sum(1 for f in recorded_finds if f["finder_kind"] == k)
            for k in sorted({f["finder_kind"] for f in recorded_finds})
        },
        "find_subjects": sorted({f["subject"] for f in recorded_finds if f["subject"]}),
        # THE THIRD ACTION. Report-only, never applied, never queued for anyone.
        "repair_proposals": repairs,
        "repair_proposal_count": len(repairs),
        "repair_proposals_worth_having": sum(1 for r in repairs if r["worth_having"]),
        # THE DRAINABLE QUANTITY, carried even when the proposal list is EMPTY -- an empty list
        # cannot say whether anything is accumulating, and "0 proposals, 0 repairs ever recorded"
        # reads completely differently from "0 proposals, 6 repairs recorded".
        "repairs_recorded": sum(m["count"] for m in markers.values()),
        "declines_by_kind": {
            k: sum(v for r in ranked for kk, v in r["declines_by_kind"].items() if kk == k)
            for k in sorted(DECLINE_KINDS)
            if any(k in r["declines_by_kind"] for r in ranked)
        },
        "ranked": ranked,
        "experiments": trials,
    }


# --------------------------------------------------------------------------- recording the two
# missing edges. Thin on purpose: the advisor already writes `match`.


def record_trigger(
    capability_id: str, experiment_id: str, *, path=None, metadata: dict | None = None
) -> bool:
    """This candidate was actually triggered. Idempotent per (capability, experiment)."""
    if not experiment_id.startswith(ADVICE_REF_PREFIX):
        raise ValueError(f"experiment_id must start with {ADVICE_REF_PREFIX!r}: {experiment_id!r}")
    return capabilities.heartbeat(
        capability_id,
        "invocation",
        ref=experiment_id,
        path=path or capabilities.REG,
        idempotency_key=f"trigger:{capability_id}:{experiment_id}",
        metadata={"source": "capability_propensity", **(metadata or {})},
    )


def record_usefulness(
    capability_id: str,
    experiment_id: str,
    *,
    useful: bool,
    evidence: str,
    provenance: str = PROVENANCE_UNSTATED,
    judge: str = "",
    corroboration: str = "",
    path=None,
    timestamp: int | None = None,
    metadata: dict | None = None,
) -> bool:
    """Did triggering it help? `evidence` is required: an unevidenced verdict is an opinion.

    The verdict must describe what the capability CHANGED, not that it ran. "It fired" is the
    un-gameable-label failure this project's learning rules exist to prevent.

    `metadata` carries the verdict's VERDICT KIND — which question was answered. Added for the tick
    wiring: an observer graded on "did your report change" and a lane capability graded on "did the
    delivery survive" answer different questions, and CLAUDE.md's learning rules forbid averaging
    across the two kinds. Without a durable `verdict_kind` on the row, a later reader could only
    average them.

    `provenance` is the ORTHOGONAL axis: where the verdict CAME FROM, and it is REQUIRED. It used to
    default to `self_reported` — the weakest tier at weight 0.25 — so an omitted argument silently
    filed outcome-backed evidence as an opinion, and because the write is IDEMPOTENT on
    (capability, experiment), that choice could never be corrected afterwards — nor even diluted: a
    second record is dropped, returning False. Silence is now refused with the tiers named
    (`unstated_provenance_refusal`), which writes nothing and leaves the trial's first observation
    still available. Claiming `outcome_corroborated` or `defect_found` REQUIRES `corroboration`
    naming the outcome — an unnamed corroboration would make the strongest weight in the table
    self-certifying, which is green-CI-alone under a new name. An unknown provenance is refused
    rather than coerced, for the same reason an unknown decline kind is: a typo silently becoming the
    default would discard the classification the caller believed it had made.

    `judge` is the ARM that judged. Optional, and load-bearing: verdicts with no judge identity are
    ALL treated as one correlated arm, so recording it is how a capability escapes the correlated-arm
    discount. It cannot be inferred, and inferring independence is exactly the error that turned
    three runs of one model into eleven independent successes.
    """
    if not str(evidence).strip():
        raise ValueError("a usefulness verdict requires evidence naming what changed")
    if not experiment_id.startswith(ADVICE_REF_PREFIX):
        raise ValueError(f"experiment_id must start with {ADVICE_REF_PREFIX!r}: {experiment_id!r}")
    if str(provenance) == PROVENANCE_UNSTATED:
        raise ValueError(unstated_provenance_refusal())
    if str(provenance) not in VERDICT_PROVENANCE:
        raise ValueError(
            f"unknown verdict provenance {provenance!r}; expected one of "
            f"{sorted(VERDICT_PROVENANCE)}"
        )
    if (
        VERDICT_PROVENANCE[str(provenance)]["requires_corroboration"]
        and not str(corroboration).strip()
    ):
        raise ValueError(
            f"provenance {provenance!r} claims outcome-strength evidence, so it requires "
            "`corroboration` naming the outcome that corroborates it (the review that confirmed "
            "it, the issue filed, the fix that landed); an unnamed corroboration is self-certifying"
        )
    extra: dict = {VERDICT_PROVENANCE_KEY: str(provenance)}
    if str(judge).strip():
        extra[VERDICT_JUDGE_KEY] = str(judge).strip()
    if str(corroboration).strip():
        extra[VERDICT_CORROBORATION_KEY] = str(corroboration)[:400]
    return capabilities.heartbeat(
        capability_id,
        "outcome",
        ref=experiment_id,
        path=path or capabilities.REG,
        idempotency_key=f"useful:{capability_id}:{experiment_id}",
        # A TEST SEAM, not a caller-facing field: ledger timestamps are second-granular, and a
        # selftest that records a defect, its repair and the re-opening evidence inside one second
        # cannot distinguish "the action drained it" from "the tie-break happened to go this way".
        # Deliberately NOT added to `record_decline`: `mcp_server`'s contract selftest asserts that
        # every keyword-only parameter of that function is advertised by the `capability_decline`
        # MCP tool, and an MCP caller must never be able to backdate a decline. CI caught exactly
        # that when the seam was put there first.
        timestamp=timestamp,
        metadata={
            "source": "capability_propensity",
            USEFUL_KEY: bool(useful),
            "evidence": str(evidence)[:400],
            **extra,
            **(metadata or {}),
        },
    )


def existing_late_outcome(
    capability_id: str,
    experiment_id: str,
    *,
    path=None,
    window_days: int = WINDOW_DAYS,
    now: int | None = None,
) -> dict | None:
    """The late outcome already attached to this trial for this capability, or None.

    Read through `experiments()` rather than by re-walking the events, so "what is attached" is
    answered by the same assembly the WEIGHTING reads. A second reader here could disagree with it,
    and then a refusal would cite an attachment the measurement never applied.
    """
    for trial in experiments(path=path, window_days=window_days, now=now):
        if trial["experiment_id"] != experiment_id:
            continue
        applied = (trial.get("late_outcomes") or {}).get(capability_id)
        if applied:
            return applied
        return (trial.get("late_outcome_orphans") or {}).get(capability_id)
    return None


def verdict_in_window(
    capability_id: str,
    experiment_id: str,
    *,
    path=None,
    window_days: int = WINDOW_DAYS,
    now: int | None = None,
) -> str | None:
    """The bucket ("useful"/"not_useful") this capability's in-window verdict sits in, or None."""
    for trial in experiments(path=path, window_days=window_days, now=now):
        if trial["experiment_id"] != experiment_id:
            continue
        for bucket in ("useful", "not_useful"):
            if capability_id in trial[bucket]:
                return bucket
    return None


def record_late_outcome(
    capability_id: str,
    experiment_id: str,
    *,
    direction: str,
    evidence: str,
    provenance: str,
    corroboration: str,
    judge: str = "",
    path=None,
    window_days: int = WINDOW_DAYS,
    timestamp: int | None = None,
    now: int | None = None,
) -> dict:
    """Attach an outcome that arrived AFTER the verdict, in either direction. Append-only.

    Returns a dict rather than a bool because every refusal here has a different remedy, and a bare
    False is what made the idempotent verdict drop look like a success for two days (#127). The
    caller gets `attached`, `reason` and `remedy`; the CLI turns a False into a NON-ZERO exit, which
    the older `useful` verb deliberately does not do — it has two live automation callers whose retry
    logic would break, so that one gained a `remedy` field instead.

    THE ORIGINAL VERDICT IS NOT MUTATED. It stays in the event log with its original provenance and
    timestamp, and this attaches beside it; `experiments()` applies the attachment when it assembles
    the trial. So the record always shows both what was believed at trigger time and what the outcome
    later established, which is what makes this auditable rather than a rewrite.

    Refuses, each naming its remedy:
      * an unknown direction, rather than defaulting to the flattering one;
      * a self-assessed provenance — this channel is for OUTCOMES, and re-asserting an opinion to
        overwrite an earlier opinion is the gaming path;
      * no `corroboration`, for every tier, because the direct path requires it for the strong tiers
        and a late path that required less would be the weaker door into the same weight;
      * NO IN-WINDOW VERDICT to attach to. This is the window edge, and it is a refusal rather than
        a write because attaching to a verdict the measurement can no longer see would store an event
        that changes nothing — the silent no-op class again. The remedy is a NEW trial, since a
        capability used again deserves its own experiment rather than a retro-fit onto a dead one;
      * AN ATTACHMENT ALREADY PRESENT, with the existing one named, so one trial cannot be re-rolled.
    """
    if not str(evidence).strip():
        raise ValueError("a late outcome requires evidence naming what the outcome established")
    if not experiment_id.startswith(ADVICE_REF_PREFIX):
        raise ValueError(f"experiment_id must start with {ADVICE_REF_PREFIX!r}: {experiment_id!r}")
    if str(direction) not in LATE_OUTCOME_DIRECTIONS:
        raise ValueError(
            late_outcome_refusal(
                f"unknown late-outcome direction {direction!r}.",
                remedy=(
                    "pass one of "
                    + ", ".join(
                        f"{name} ({row['means']})"
                        for name, row in sorted(LATE_OUTCOME_DIRECTIONS.items())
                    )
                    + " — there is no default, because defaulting would silently pick the direction "
                    "that flatters the capability"
                ),
            )
        )
    allowed = late_outcome_provenances()
    if str(provenance) not in allowed:
        raise ValueError(
            late_outcome_refusal(
                f"provenance {provenance!r} may not attach as a late outcome.",
                remedy=(
                    f"pass one of {allowed}. A late attachment must be an OUTCOME observation; "
                    "re-asserting a self-assessed verdict later is how an opinion would overwrite "
                    "the first opinion, and the first one is the honest one"
                ),
            )
        )
    if not str(corroboration).strip():
        raise ValueError(
            late_outcome_refusal(
                "a late outcome requires `corroboration` naming the outcome itself.",
                remedy=(
                    "name the merged fix, the filed issue, the review that confirmed it or the "
                    "re-measurement that contradicted it. Required for EVERY tier here, not only "
                    "the 1.0 ones: the direct path requires it for outcome-strength evidence, and a "
                    "late path that asked for less would be the weaker door into the same weight"
                ),
            )
        )
    bucket = verdict_in_window(
        capability_id, experiment_id, path=path, window_days=window_days, now=now
    )
    if bucket is None:
        return {
            "attached": False,
            "capability": capability_id,
            "experiment": experiment_id,
            "reason": (
                f"no in-window verdict for {capability_id!r} on {experiment_id!r} to attach to "
                f"(window {window_days}d)"
            ),
            "remedy": late_outcome_refusal(
                "Nothing was written.",
                remedy=(
                    "record the trial itself — `trigger` then `useful` with the tier the evidence "
                    "actually supports — because a capability used again earns its own experiment; "
                    "a late outcome corrects an existing verdict, it "
                    "does not create one, and attaching to a verdict the window can no longer see "
                    "would store an event that changes no measurement"
                ),
            ),
        }
    already = existing_late_outcome(
        capability_id, experiment_id, path=path, window_days=window_days, now=now
    )
    if already:
        return {
            "attached": False,
            "capability": capability_id,
            "experiment": experiment_id,
            "reason": (
                f"a late outcome is already attached: direction "
                f"{already.get('direction')!r} at provenance {already.get('provenance')!r}, "
                f"corroborated by {str(already.get('corroboration'))[:120]!r}"
            ),
            "remedy": late_outcome_refusal(
                "Nothing was written, and the existing attachment stands.",
                remedy=(
                    "one attachment per trial is the point — it stops a trial being re-rolled until "
                    "the number is agreeable. If the NEW outcome genuinely supersedes the old one, "
                    "record a fresh trial for the fresh use rather than overwriting this record"
                ),
            ),
            "existing": already,
        }
    ok = capabilities.heartbeat(
        capability_id,
        LATE_OUTCOME_EVENT_TYPE,
        ref=experiment_id,
        path=path or capabilities.REG,
        idempotency_key=f"late:{capability_id}:{experiment_id}",
        timestamp=timestamp,
        metadata={
            "source": LATE_OUTCOME_SOURCE,
            LATE_OUTCOME_DIRECTION_KEY: str(direction),
            VERDICT_PROVENANCE_KEY: str(provenance),
            VERDICT_CORROBORATION_KEY: str(corroboration)[:400],
            "evidence": str(evidence)[:400],
            **({VERDICT_JUDGE_KEY: str(judge).strip()} if str(judge).strip() else {}),
        },
    )
    return {
        "attached": bool(ok),
        "capability": capability_id,
        "experiment": experiment_id,
        "direction": str(direction),
        "provenance": str(provenance),
        "provenance_weight": provenance_weight(str(provenance)),
        "superseded_bucket": bucket,
        "verdict_after": (
            bucket
            if LATE_OUTCOME_DIRECTIONS[str(direction)]["keeps_verdict"]
            else ("not_useful" if bucket == "useful" else "useful")
        ),
        "judge": str(judge).strip() or UNATTRIBUTED_JUDGE,
    }


# ---------------------------------------------------------------------------
# TICK EVIDENCE — the tick consults the front door, and records whether a capability HELPED.
#
# DEDUP FINDING (2026-08-22, before writing this; recorded in the ledger `notes` of
# `capability-propensity` as well as here, because a plan is not durable).
#   * `capability_advisor.advise()` EXISTS, and `SURFACE_BINDINGS["tick"]` has bound four
#     capabilities to the tick since the binding table was written — but a tree-wide grep for
#     `capability_advisor` finds NO caller outside the module's own selftests and this module's.
#     Nothing in `orchestrate.sh` or `tick.py` has ever consulted it.
#   * `record_trigger` / `record_usefulness` EXIST (above) and likewise have no production caller:
#     only the two CLI subcommands and the selftests. So the natural experiment has never had a
#     producer for its `invocation` and `outcome` edges, which is exactly why the propensity step
#     prints PRIOR-ONLY on every run.
#   * `capability_firing_monitor` stores per-capability firing HISTORY — did it RUN. It does not and
#     should not ask whether the run said anything new.
#   * `capability_effectiveness` measures delivery (`influence_edges` accepted vs counterfactual),
#     which no observer can ever populate.
#   So: the capability exists and is dormant for want of a caller. This is the caller. The one
#   genuinely new part is the observer verdict below, because nothing compared an observer's own
#   output across runs.
#
# WHAT "IT HELPED" MEANS FOR AN OBSERVER, and why a delivery verdict would be a category error.
# Three of the four tick-bound capabilities carry `{"kind": "tick_phase"}` matchers, so
# `capabilities.is_observer()` is True for them: they emit a report and can never merge a PR. Asking
# them for a delivery outcome is the mistake that parked eight capabilities in a measurement gap they
# could not leave. Their deliverable is INFORMATION, so the verdict is:
#
#     USEFUL      — its report's FINDING SET changed since its previous run: a defect newly
#                   reported, a regression flagged, a switch verdict that moved, or a finding that
#                   resolved. The run told the system something it did not already know.
#     NOT USEFUL  — it ran and re-emitted an identical finding set. SILENCE IS NOT USEFULNESS, and
#                   an empty finding set that stays empty is explicitly not useful, not "fine".
#
# It is deliberately un-gameable in the one direction that matters: a capability cannot improve its
# score by running MORE, because a verdict is tied to one artifact PRODUCTION (below) and is measured
# against that capability's own previous output.
#
# THE INFLATION BOUND — the single biggest risk here, treated as a correctness requirement. The tick
# runs 24x/day and binds four capabilities, so an unconditional verdict per run would write 96
# unearned data points a day and the ranking would then measure the cadence, not usefulness. Two
# INDEPENDENT bounds, both enforced in code below:
#
#   1. STRUCTURAL (cannot be exceeded even if bound 2 is buggy): the experiment id is scoped to the
#      UTC DAY, so `advise()`'s `advice:<cap>:<digest>` and `record_trigger`/`record_usefulness`'s
#      `trigger:`/`useful:` idempotency keys are all day-unique. Ceiling: 4 match + 4 trigger + 4
#      outcome events per day, whatever happens. Ticks 2..24 of a day write nothing.
#   2. SUBSTANTIVE (what actually happens): a verdict requires the capability's own cadence ARTIFACT
#      to have been regenerated since the last evaluation. The four steps' declared cadences are
#      daily (`capability-activation-audit`) and 6-day (`switch-review`, `capability-firing-monitor`,
#      `capability-propensity`, the last of which is not graded at all — see below). So the graded
#      ceiling is 1/day + 2 x 1/7 per day = 1.29 verdicts/day against the naive 4 x 24 = 96. A 74x
#      reduction, and the realised rate is lower still because an unchanged projection is only
#      recorded once per production, not once per tick.
#
# LATCHED-GATE ANSWERS for the freshness gate (it is a gate, so it owes all three in writing):
#   1. WHAT DECREMENTS IT? The capability's own cadence step regenerating its artifact — a mechanism
#      that already runs on `_cadence_due`, not "time passes" and not "someone notices".
#   2. CAN THE DRAIN RUN WHILE CLOSED? Yes. This gate suppresses only RECORDING; it never gates the
#      cadence step itself, which runs on its own schedule whether or not anything is recorded.
#   3. SAME WINDOW BOTH WAYS? Yes, by construction: the measuring quantity and the draining quantity
#      are the SAME artifact mtime, produced by the SAME step the cadence registry declares. There is
#      no second literal to drift.
#   And the runtime rule: every run reports `verdicts_recorded` (the blocking quantity) beside
#   `gradable` and `awaiting_regeneration` (the drainable quantity), so "0 verdicts" can never read as
#   patience when it should read as deadlock.
#
# THE TICK MUST NOT BE ABLE TO STALL. Everything here is advisory and read-only apart from the
# capability ledger events it exists to write: no gh, no network, no subprocess, no dispatch. Every
# per-capability step is individually guarded, the whole run is wrapped, and the CLI arms a SIGALRM
# budget so a blocked ledger flock cannot hold the tick. Any failure returns a report and exit 0.

# NOT A SECOND STORE, and the distinction matters because `CLAUDE.md` forbids one. All EVIDENCE goes
# to the existing capability ledger through `record_trigger`/`record_usefulness` and the advisor's
# `match` heartbeat -- no new event log, no second inventory, no parallel lifecycle. The state file
# below holds one thing the ledger cannot: the artifact mtime and finding fingerprint LAST SEEN, so
# "did the output change" has something to compare against. Same shape and same reason as
# `capability_firing_monitor`'s `capability-firing-history.json`, which is a comparison baseline
# rather than a store of record.
TICK_SURFACE = "tick"
TICK_EVIDENCE_STATE = "tick-capability-evidence-state.json"
TICK_EVIDENCE_REPORT = "tick-capability-evidence.json"
# Wall-clock budget for the whole run. The only unbounded wait in here is `capabilities._locked`'s
# blocking flock; SIGALRM interrupts it, and ledger writes are tmp+os.replace so an interrupt cannot
# leave a torn ledger.
TICK_EVIDENCE_BUDGET_S = 30
TICK_VERDICT_KIND = "observer_output_change"

# What counts as a FINDING in each capability's own report, and what IDENTIFIES one.
#
# THIS FIELD LIST IS THE WHOLE SAFETY MECHANISM. `capability-firing-monitor`'s `overdue` rows carry
# `silent_days`, which rises every day on its own; hashing a row whole would score the monitor
# "useful" on every run it will ever make -- a 100% usefulness rate that measures the calendar. So a
# projection keeps IDENTITY and VERDICT fields and drops everything else, and
# `_selftest_tick_evidence` re-feeds identical findings with moved counters and timestamps and
# asserts the verdict is NOT useful.
#
# An empty tuple means the value needs no field selection: a list of ids, or a map of id lists, is
# already identity-only.
TICK_FINDING_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "switch-review": {
        # (flag, state), so a switch that MOVED reads as a change while the long stored `criterion`
        # prose -- which is documentation, not a finding -- does not.
        "held_off": ("flag", "state"),
        "on_but_idle": ("flag", "state"),
        "unconditioned": ("flag", "state"),
    },
    "capability-firing-monitor": {
        "regressed": ("capability_id",),
        "overdue": ("capability_id",),  # NOT silent_days / tolerance_days: counters, not findings
        "never_fired": (),
        "no_cadence_declared": (),
    },
    "capability-activation-audit": {
        "by_defect": (),  # {defect class: [capability ids]}
        "reachable_ids": (),
    },
    # DELIBERATELY EMPTY, and a verdict rather than an omission: this module IS the grader, so
    # grading it on its own report is the circular-measurement failure mode (FM7). Its usefulness
    # report and its selection-detect output both move BECAUSE of the rows written here, so a
    # self-verdict would ratchet. It stays a bound candidate and its production is still recorded as
    # a trigger -- what it does not get is a verdict from itself.
    "capability-propensity": {},
}

# Why a bound capability is not graded, stated once so "not graded" can never look like "nothing
# happened". Reasons are computed, not stored; these are just the words.
TICK_SKIP_REASONS = {
    "no_cadence_artifact": "no cadence step declares an artifact for it, so it produces no report "
    "this can read",
    "artifact_missing": "its declared cadence artifact does not exist yet; the step has not run here",
    "not_an_observer": "capabilities.is_observer() is False, so its deliverable is not a report and "
    "an output-change verdict would mix two different questions in one rate",
    "no_finding_projection": "no finding projection is declared for it, so 'did the output change' "
    "has no defined answer",
    "unprojectable": "its artifact carried none of the declared finding keys -- a SHAPE CHANGE, "
    "reported rather than scored, because a broken parse must not read as 'nothing "
    "new'",
    "not_in_ledger": "it has no row in this machine's capability ledger, so there is nothing to "
    "record against",
    "budget_exhausted": "the run hit its wall-clock budget before reaching it; nothing is recorded "
    "rather than recorded late",
}


def tick_evidence_disabled() -> bool:
    """THE KILL SWITCH, read at call time so the switch itself is testable as a switch.

    `ORCH_TICK_EVIDENCE_DISABLED=1` makes the tick behave EXACTLY as it did before this wiring
    existed: no consult, no ledger event, no state file, no report. Read here rather than captured in
    a module constant at import so a selftest exercises the real environment round-trip -- an
    untested kill switch is theatre, and this repo has said so in `orchestrate.sh` since 2026-08-21.
    `ORCH_DISABLE_STEPS=tick-capability-evidence` is the second, shell-side lever; either alone is
    sufficient.
    """
    return os.environ.get("ORCH_TICK_EVIDENCE_DISABLED", "").strip() == "1"


def _tick_state_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("ORCH_STATE_DIR", str(pathlib.Path.home() / ".codex/orchestrator"))
    )


def tick_artifact(
    capability_id: str, *, state_dir: pathlib.Path, steps=None
) -> pathlib.Path | None:
    """The report artifact this capability's cadence step publishes. ONE source: the registry.

    Read from `cadence_registry.STEP_BY_KEY` rather than re-listed here, because the tick-bound
    capability ids ARE cadence step keys and a second table of filenames would be free to drift from
    the step that writes them. `_selftest_tick_evidence` pins the agreement as code-vs-code, so it
    holds on a clean runner with no ledger and no state directory.
    """
    if steps is None:
        try:
            import cadence_registry

            steps = cadence_registry.STEP_BY_KEY
        except Exception:  # noqa: BLE001
            return None
    name = ((steps or {}).get(capability_id) or {}).get("artifact")
    return (state_dir / str(name)) if name else None


def _finding_identity(value, fields: tuple[str, ...]) -> str:
    """One finding reduced to identity. Anything not declared is DROPPED -- that is the point."""
    if isinstance(value, dict):
        if fields:
            return "|".join(f"{f}={value.get(f)!r}" for f in fields)
        return "|".join(
            f"{k}={sorted(map(str, v))!r}" if isinstance(v, list) else f"{k}={v!r}"
            for k, v in sorted(value.items())
        )
    return str(value)


def project_findings(capability_id: str, report) -> dict[str, list[str]] | None:
    """This report's findings, identity-only. None when nothing declared could be read.

    None is a distinct answer from `{}` on purpose. `{}` cannot happen here (a key that is present
    but empty yields `{key: []}`), so None means the artifact carried NONE of the declared keys --
    a shape change. Scoring that as "unchanged" would let a broken parse read as a clean answer,
    which is this repo's founding defect.
    """
    spec = TICK_FINDING_FIELDS.get(capability_id)
    if not spec or not isinstance(report, dict):
        return None
    out: dict[str, list[str]] = {}
    for key, fields in spec.items():
        if key not in report:
            continue
        value = report[key]
        if isinstance(value, list):
            out[key] = sorted(_finding_identity(v, fields) for v in value)
        elif isinstance(value, dict):
            out[key] = sorted(
                f"{k}={sorted(map(str, v))!r}" if isinstance(v, list) else f"{k}={v!r}"
                for k, v in value.items()
            )
        else:
            out[key] = [str(value)]
    return out or None


def finding_fingerprint(findings: dict[str, list[str]]) -> str:
    return hashlib.sha1(json.dumps(findings, sort_keys=True).encode()).hexdigest()[:16]


def _finding_delta(now: dict[str, list[str]], before: dict[str, list[str]]) -> list[str]:
    """Which finding keys moved, and by how much. The evidence string for a USEFUL verdict."""
    out = []
    for key in sorted(set(now) | set(before)):
        cur, prev = set(now.get(key) or []), set(before.get(key) or [])
        if cur != prev:
            out.append(f"{key} +{len(cur - prev)}/-{len(prev - cur)}")
    return out


def tick_task(day: str) -> str:
    """The advisory question the tick asks, stable per UTC day.

    Stable per day is what coalesces the consult: `advise()`'s match heartbeat is idempotent per
    (capability, task digest), so 24 ticks a day produce at most one match event per bound
    capability. Deliberately carries NO word from `capability_advisor.TASK_SIGNALS`: the tick is a
    cadence, not one free-text task, so the DECLARED binding is the right answer and the keyword
    classifier must not add to it. `_selftest_tick_evidence` asserts what the CALLER receives is
    exactly the bound set, so a stray keyword fails a test instead of quietly widening the consult.
    """
    return f"orchestrator tick cadence pass {day}"


def _load_tick_state(state_dir: pathlib.Path) -> dict:
    try:
        data = json.loads((state_dir / TICK_EVIDENCE_STATE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"schema": 1, "capabilities": {}, "last_consult_day": None}
    if not isinstance(data, dict):
        return {"schema": 1, "capabilities": {}, "last_consult_day": None}
    data.setdefault("capabilities", {})
    data.setdefault("last_consult_day", None)
    return data


def _write_json_atomic(path: pathlib.Path, payload: dict) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def tick_evidence(
    *,
    now: int | None = None,
    state_dir: pathlib.Path | None = None,
    path=None,
    steps=None,
    record: bool = True,
    budget_s: float | None = None,
) -> dict:
    """Consult the advisor for the `tick` surface and record earned usefulness verdicts.

    Called every tick from `orchestrate.sh`, BELOW `ORCH-ANCHOR: heartbeat-export` (a producer above
    it records nothing, and `capability_activation_audit.heartbeat_env_gate` fails the suite if one
    ever moves there). Cheap by construction: on the 23 ticks a day with no freshly-regenerated
    cadence artifact and the consult already recorded, it reads one small state file and returns.
    """
    now = int(time.time()) if now is None else int(now)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    started = time.monotonic()
    budget = TICK_EVIDENCE_BUDGET_S if budget_s is None else float(budget_s)
    base = {"generated_at": now, "day": day, "surface": TICK_SURFACE}
    if tick_evidence_disabled():
        # EXACTLY as before this wiring existed: nothing read, nothing written, nothing recorded.
        return {
            **base,
            "disabled": True,
            "reason": "ORCH_TICK_EVIDENCE_DISABLED=1",
            "bound": [],
            "evaluated": [],
            "verdicts_recorded": 0,
            "triggers_recorded": 0,
            "matches_recorded": 0,
            "gradable": [],
            "awaiting_regeneration": [],
            "skipped": [],
        }

    state_dir = _tick_state_dir() if state_dir is None else pathlib.Path(state_dir)
    ledger = path or capabilities.REG

    import capability_advisor

    bound = sorted(capability_advisor.binding_for(TICK_SURFACE, path=path))
    caps = capabilities.load_declared(ledger)
    state = _load_tick_state(state_dir)
    per_cap = state["capabilities"]

    # ---- classify every bound capability BEFORE writing anything. Three outcomes only:
    #      evaluate (its artifact was regenerated since we last looked), baseline (we have never
    #      looked), stale (unchanged -> nothing at all, not a negative verdict).
    plans: list[dict] = []
    skipped: list[dict] = []
    for cap_id in bound:
        artifact = tick_artifact(cap_id, state_dir=state_dir, steps=steps)
        if artifact is None:
            skipped.append(
                {
                    "capability_id": cap_id,
                    "reason": "no_cadence_artifact",
                    "detail": TICK_SKIP_REASONS["no_cadence_artifact"],
                }
            )
            continue
        try:
            mtime = int(artifact.stat().st_mtime)
        except OSError:
            skipped.append(
                {
                    "capability_id": cap_id,
                    "reason": "artifact_missing",
                    "detail": TICK_SKIP_REASONS["artifact_missing"],
                    "artifact": str(artifact),
                }
            )
            continue
        prior = per_cap.get(cap_id) or {}
        seen = int(prior.get("artifact_mtime") or 0)
        if not prior:
            plans.append(
                {
                    "capability_id": cap_id,
                    "action": "baseline",
                    "artifact": artifact,
                    "mtime": mtime,
                }
            )
        elif mtime > seen:
            plans.append(
                {
                    "capability_id": cap_id,
                    "action": "evaluate",
                    "artifact": artifact,
                    "mtime": mtime,
                    "prior": prior,
                }
            )
        # else: stale. No fresh production, so there is no concrete reason to record anything.

    consult_due = state.get("last_consult_day") != day
    if not plans and not consult_due:
        # THE HOT PATH: 23 of 24 ticks. One small file read, zero ledger touches, zero writes.
        return {
            **base,
            "bound": bound,
            "evaluated": [],
            "baselined": [],
            "stale": [c for c in bound if c not in {s["capability_id"] for s in skipped}],
            "skipped": skipped,
            "consulted": False,
            "verdicts_recorded": 0,
            "triggers_recorded": 0,
            "matches_recorded": 0,
            "gradable": sorted(_tick_gradable(bound, caps)),
            "awaiting_regeneration": sorted(_tick_gradable(bound, caps)),
            "reason": "no cadence artifact was regenerated since the last evaluation, and "
            "today's consult is already recorded — nothing to add",
        }

    # ---- the CONSULT. This is the thing the tick never did. `record=True` writes the `match` edge
    #      each verdict below is attached to; it is idempotent per (capability, day).
    advice = {}
    matches = 0
    if record:
        try:
            advice = capability_advisor.advise(
                tick_task(day),
                surface=TICK_SURFACE,
                skill=TICK_SURFACE,
                lane="tick",
                path=path,
                record=True,
            )
            matches = int(advice.get("recorded_matches") or 0)
        except Exception as exc:  # noqa: BLE001
            advice = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        try:
            advice = capability_advisor.advise(
                tick_task(day),
                surface=TICK_SURFACE,
                skill=TICK_SURFACE,
                lane="tick",
                path=path,
                record=False,
            )
        except Exception as exc:  # noqa: BLE001
            advice = {"error": f"{type(exc).__name__}: {exc}"}
    experiment = advice.get("experiment_id") or capability_advisor.experiment_id(tick_task(day))

    evaluated: list[dict] = []
    baselined: list[dict] = []
    triggers = 0
    verdicts = 0
    for plan in plans:
        cap_id = plan["capability_id"]
        if time.monotonic() - started > budget:
            skipped.append(
                {
                    "capability_id": cap_id,
                    "reason": "budget_exhausted",
                    "detail": TICK_SKIP_REASONS["budget_exhausted"],
                }
            )
            continue
        cap_row = caps.get(cap_id)
        if cap_row is None:
            skipped.append(
                {
                    "capability_id": cap_id,
                    "reason": "not_in_ledger",
                    "detail": TICK_SKIP_REASONS["not_in_ledger"],
                }
            )
            continue
        try:
            report = json.loads(plan["artifact"].read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            skipped.append(
                {
                    "capability_id": cap_id,
                    "reason": "unreadable_artifact",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        findings = project_findings(cap_id, report)
        fingerprint = finding_fingerprint(findings) if findings else None

        # IT RAN. Recorded for every fresh production, gradable or not, so the control arm stays
        # honest: a bound capability that really did produce output must not read as "offered and
        # skipped" just because this module declines to grade it.
        if record:
            try:
                if record_trigger(
                    cap_id,
                    experiment,
                    path=path,
                    metadata={"surface": TICK_SURFACE, "artifact": plan["artifact"].name},
                ):
                    triggers += 1
            except Exception as exc:  # noqa: BLE001
                skipped.append(
                    {
                        "capability_id": cap_id,
                        "reason": "trigger_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

        entry = {
            "capability_id": cap_id,
            "artifact": plan["artifact"].name,
            "fingerprint": fingerprint,
            "finding_count": sum(len(v) for v in (findings or {}).values()),
        }
        reason = _tick_ungradable_reason(cap_id, cap_row, findings)
        if plan["action"] == "baseline":
            # FIRST SIGHT ESTABLISHES THE BASELINE AND RECORDS NO VERDICT. There is nothing to
            # compare against, and inventing a verdict from a single observation is the manufactured
            # evidence this whole design exists to prevent. Same discipline as
            # `capability_firing_monitor`: the first run only establishes the baseline.
            baselined.append({**entry, "reason": "first observation — baseline only, no verdict"})
        elif reason:
            evaluated.append(
                {
                    **entry,
                    "graded": False,
                    "reason": reason,
                    "detail": TICK_SKIP_REASONS.get(reason, reason),
                }
            )
        else:
            previous = plan["prior"].get("findings") or {}
            prev_fp = plan["prior"].get("fingerprint")
            changed = fingerprint != prev_fp
            delta = _finding_delta(findings or {}, previous)
            if changed:
                evidence = (
                    f"{TICK_VERDICT_KIND}: report changed since "
                    f"{plan['prior'].get('day', 'the previous run')} — "
                    + ("; ".join(delta) if delta else "finding set differs")
                )
            else:
                evidence = (
                    f"{TICK_VERDICT_KIND}: ran and re-emitted an IDENTICAL finding set "
                    f"({entry['finding_count']} finding(s) across {len(findings or {})} "
                    f"key(s)) last seen {plan['prior'].get('day', 'previously')}; nothing "
                    f"new was reported, and silence is not usefulness"
                )
            recorded = False
            if record:
                try:
                    recorded = record_usefulness(
                        cap_id,
                        experiment,
                        useful=changed,
                        evidence=evidence,
                        # MACHINE-OBSERVED, stated rather than inferred. The tick computes this
                        # verdict from a fingerprint diff of the capability's own artifact; nobody
                        # asserts it, so it is not a self-report. The derivation in
                        # `verdict_provenance()` would reach the same class from `verdict_kind`
                        # alone, but a derivation is a fallback for rows written before this axis
                        # existed, not a substitute for the producer saying what it produced.
                        provenance="machine_observed",
                        judge=TICK_SURFACE,
                        path=path,
                        metadata={
                            "verdict_kind": TICK_VERDICT_KIND,
                            "surface": TICK_SURFACE,
                            "fingerprint": fingerprint,
                            "previous_fingerprint": prev_fp,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    evaluated.append(
                        {
                            **entry,
                            "graded": False,
                            "reason": "verdict_failed",
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                verdicts += 1 if recorded else 0
            evaluated.append(
                {
                    **entry,
                    "graded": True,
                    "useful": changed,
                    "previous_fingerprint": prev_fp,
                    "changed_keys": delta,
                    "recorded": recorded,
                    "evidence": evidence,
                }
            )

        per_cap[cap_id] = {
            "artifact_mtime": plan["mtime"],
            "fingerprint": fingerprint,
            "findings": findings or {},
            "evaluated_at": now,
            "day": day,
            "experiment_id": experiment,
        }

    state["last_consult_day"] = day
    state["schema"] = 1
    gradable = sorted(_tick_gradable(bound, caps))
    acted = {e["capability_id"] for e in evaluated} | {b["capability_id"] for b in baselined}
    report_out = {
        **base,
        "experiment_id": experiment,
        "bound": bound,
        "consulted": bool(advice) and "error" not in advice,
        "advice_confidence": advice.get("confidence"),
        "advice_error": advice.get("error"),
        "advice_capabilities": [c["capability_id"] for c in (advice.get("capabilities") or [])],
        "evaluated": evaluated,
        "baselined": baselined,
        "stale": [
            c for c in bound if c not in acted and c not in {s["capability_id"] for s in skipped}
        ],
        "skipped": skipped,
        "matches_recorded": matches,
        "triggers_recorded": triggers,
        # BOTH QUANTITIES, ALWAYS, IN ONE PLACE. `verdicts_recorded` is what landed;
        # `gradable`/`awaiting_regeneration` is what CAN still land. "0 verdicts" beside "3 gradable,
        # awaiting regeneration" reads as cadence; "0 verdicts, 0 gradable" reads as a deadlock, and
        # that difference is the whole point of printing them together.
        "verdicts_recorded": verdicts,
        "gradable": gradable,
        "awaiting_regeneration": [c for c in gradable if c not in acted],
        "not_gradable": {s["capability_id"]: s["reason"] for s in skipped},
        "ceiling": {
            "ticks_per_day": 24,
            "structural_verdicts_per_day": len(bound),
            "structural_basis": "the experiment id is scoped to the UTC day, so the "
            "record_usefulness idempotency key admits at most one verdict per "
            "bound capability per day however many ticks run",
            "graded_verdicts_per_day_ceiling": round(
                sum(
                    1.0
                    / max(
                        1.0,
                        float(((steps or _tick_steps()).get(c) or {}).get("cadence_days") or 0)
                        + 1.0,
                    )
                    for c in gradable
                ),
                2,
            ),
            "graded_basis": "a verdict additionally requires the capability's own cadence artifact "
            "to have been regenerated, so the rate is bounded by each step's "
            "declared cadence, not by the tick",
            "naive_unconditional_per_day": 24 * len(bound),
        },
    }
    if record:
        try:
            _write_json_atomic(state_dir / TICK_EVIDENCE_STATE, state)
            _write_json_atomic(state_dir / TICK_EVIDENCE_REPORT, report_out)
        except Exception as exc:  # noqa: BLE001
            report_out["state_write_error"] = f"{type(exc).__name__}: {exc}"
    return report_out


def _tick_steps() -> dict:
    try:
        import cadence_registry

        return cadence_registry.STEP_BY_KEY
    except Exception:  # noqa: BLE001
        return {}


def _tick_ungradable_reason(capability_id: str, cap_row: dict, findings: dict | None) -> str | None:
    """Why this capability gets no output-change verdict, or None when it gets one."""
    if not TICK_FINDING_FIELDS.get(capability_id):
        return "no_finding_projection"
    if not capabilities.is_observer(cap_row):
        # DERIVED from the one existing source of the observer/deliverer distinction, never
        # re-declared here. Widening `OBSERVER_MATCHER_KINDS` to sweep a capability in would hide a
        # real linkage gap for the other capabilities sharing that kind; `capabilities.py` says so
        # at the set itself.
        return "not_an_observer"
    if findings is None:
        return "unprojectable"
    return None


def _tick_gradable(bound: list[str], caps: dict) -> set[str]:
    """Bound capabilities that COULD earn a verdict. The drainable quantity."""
    out = set()
    for cap_id in bound:
        row = caps.get(cap_id)
        if row is None:
            continue
        if not TICK_FINDING_FIELDS.get(cap_id):
            continue
        if not capabilities.is_observer(row):
            continue
        out.add(cap_id)
    return out


def tick_evidence_guarded(**kwargs) -> dict:
    """`tick_evidence` that cannot take the tick down. THE ONLY entry point the shell calls.

    A capability-evaluation feature that can break the hourly tick is unacceptable: the tick drives
    real dispatch on real repositories. So the SIGALRM budget bounds the one blocking wait in the
    path (the ledger flock), and any exception at all becomes a reported field rather than a
    non-zero exit.
    """
    import signal

    # `is None`, not falsy: `--budget-seconds 0` means "do nothing", and silently promoting it to 30
    # would make a control lie about what it does.
    budget = kwargs.pop("budget_s", None)
    budget = TICK_EVIDENCE_BUDGET_S if budget is None else int(budget)
    kwargs["budget_s"] = budget

    def _expired(_signum, _frame):
        raise TimeoutError(f"tick capability evidence exceeded {budget}s")

    armed = False
    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, _expired)
        # The in-loop budget check stops between capabilities; the alarm is the backstop for the one
        # syscall that can block indefinitely (the ledger flock). Ledger writes are tmp+os.replace,
        # so an interrupt cannot leave a torn ledger.
        signal.alarm(max(1, budget) + 5)
        armed = True
    except (ValueError, AttributeError, OSError):
        armed = False  # not the main thread, or no SIGALRM: run unbounded
    try:
        return tick_evidence(**kwargs)
    except BaseException as exc:  # noqa: BLE001
        return {
            "generated_at": int(time.time()),
            "surface": TICK_SURFACE,
            "error": f"{type(exc).__name__}: {exc}",
            "verdicts_recorded": 0,
            "triggers_recorded": 0,
            "matches_recorded": 0,
            "bound": [],
            "evaluated": [],
            "gradable": [],
            "awaiting_regeneration": [],
            "skipped": [],
            "reason": "tick capability evidence failed; the tick is unaffected",
        }
    finally:
        if armed:
            try:
                signal.alarm(0)
                if previous is not None:
                    signal.signal(signal.SIGALRM, previous)
            except (ValueError, OSError):
                pass


def format_tick_evidence(rep: dict) -> str:
    """One or two lines, in the tick log's own style. Always states both quantities."""
    if rep.get("disabled"):
        return "  [tick-evidence] DISABLED by ORCH_TICK_EVIDENCE_DISABLED=1 (no consult, no record)"
    if rep.get("error"):
        return f"  [tick-evidence] error: {rep['error']} (tick unaffected)"
    if not rep.get("consulted") and not rep.get("evaluated") and not rep.get("baselined"):
        return (
            "  [tick-evidence] nothing regenerated since the last evaluation; "
            f"{len(rep.get('gradable') or [])} gradable, awaiting their cadence"
        )
    useful = [
        e["capability_id"]
        for e in rep.get("evaluated") or []
        if e.get("graded") and e.get("useful")
    ]
    quiet = [
        e["capability_id"]
        for e in rep.get("evaluated") or []
        if e.get("graded") and not e.get("useful")
    ]
    lines = [
        f"  [tick-evidence] advisor consulted for surface 'tick': "
        f"{len(rep.get('advice_capabilities') or [])} bound capability(ies); "
        f"verdicts {rep.get('verdicts_recorded', 0)}, "
        f"gradable {len(rep.get('gradable') or [])}, "
        f"awaiting regeneration {len(rep.get('awaiting_regeneration') or [])}"
    ]
    if useful or quiet or rep.get("baselined"):
        detail = []
        if useful:
            detail.append("USEFUL(output changed): " + ", ".join(useful))
        if quiet:
            detail.append("not useful(identical output): " + ", ".join(quiet))
        if rep.get("baselined"):
            detail.append("baselined: " + ", ".join(b["capability_id"] for b in rep["baselined"]))
        lines.append("    " + " | ".join(detail))
    return "\n".join(lines)


def _selftest_tick_evidence() -> None:
    """The tick wiring: an earned verdict, a bounded one, and no manufactured evidence.

    SYNTHETIC LEDGER AND SYNTHETIC STATE DIR THROUGHOUT. This machine's ledger holds 43 rows and a
    clean runner holds 14; an assertion against the live one passes here and fails in CI, which has
    already happened twice in this subsystem. Every number below comes from fixtures.
    """
    import tempfile
    from pathlib import Path

    import capability_advisor

    now = 1_800_000_000
    day = time.strftime("%Y-%m-%d", time.gmtime(now))

    # ---- PART 1: THE REAL TABLE, code vs code. Runs on any machine: no ledger, no state dir.
    real_bound = capability_advisor.binding_for(TICK_SURFACE)
    assert (
        real_bound
    ), "the tick surface must have a declared bound set, or there is nothing to wire"
    steps = _tick_steps()
    assert steps, "cadence registry unreadable; the artifact resolver would silently find nothing"
    # THE CONSULT TEXT MUST STAY UNCLASSIFIABLE, so the tick's answer is exactly its DECLARED bound
    # set. The tick is a cadence, not one free-text task; a stray keyword would silently widen both
    # the consult and the recorded matches to whatever the classifier happened to hit. This fails a
    # test instead of drifting.
    assert capability_advisor.classify_task(tick_task(day)) == [], (
        f"the tick's consult text now hits the keyword classifier "
        f"({capability_advisor.classify_task(tick_task(day))}); pick a phrase that does not, or the "
        f"declared binding stops being the whole answer"
    )
    assert tick_artifact("no-such-capability", state_dir=Path("/nonexistent")) is None
    for cap_id in TICK_FINDING_FIELDS:
        assert cap_id in real_bound, (
            f"{cap_id} has a finding projection but is not bound to the tick surface, so nothing "
            f"will ever read it: {sorted(real_bound)}"
        )
        if not TICK_FINDING_FIELDS[cap_id]:
            continue  # deliberately not graded; see the table's own comment
        art = tick_artifact(cap_id, state_dir=Path("/nonexistent"))
        assert art is not None, (
            f"{cap_id} is graded on an artifact, but no cadence step declares one for it — the "
            f"registry and this projection have drifted"
        )
    # The projection must never name a field that moves on its own. Enumerated, because this is the
    # one mistake that would turn every run into a "useful" verdict.
    moving = {
        "silent_days",
        "tolerance_days",
        "generated_at",
        "timestamp",
        "last_invocation",
        "unchanged_for_days",
        "snapshots_stored",
        "propensity",
        "raise_count",
    }
    for cap_id, spec in TICK_FINDING_FIELDS.items():
        for key, fields in spec.items():
            assert not (set(fields) & moving), (cap_id, key, fields)

    # ---- A CALLER MUST EXIST, and it must sit in the right place. This project's #1 defect class is
    # built-and-forgotten, and the two ways this wiring could become inert are both checkable from
    # source with no ledger and no state directory:
    #   (a) the tick stops invoking it at all — every dormant subsystem here had exactly that defect;
    #   (b) the invocation drifts ABOVE `ORCH-ANCHOR: heartbeat-export`, where it would run and
    #       record nothing, which is the defect that had `frontend-verifier` reading `never fired`
    #       while working. `heartbeat_env_gate` catches (b) for heartbeat-emitting modules; this
    #       catches it for THIS subcommand specifically, and catches (a), which nothing else does.
    # Resolved relative to THIS module's own directory on purpose: the check must verify the driver
    # in the same tree as the code, which is right in both the repo and the exec mirror.
    driver = paths.orchestrate_sh()
    if driver.exists():
        text = driver.read_text(errors="ignore")
        call = text.find('capability_propensity.py" tick-evidence')
        assert call > 0, (
            "orchestrate.sh no longer invokes `capability_propensity.py "
            "tick-evidence`; the tick has stopped consulting the advisor and stopped "
            "recording usefulness, which is this project's #1 defect class"
        )
        export = text.find("export ORCH_CAPABILITY_HEARTBEATS=1")
        assert 0 < export < call, (
            "the tick-evidence step is invoked ABOVE the ORCH_CAPABILITY_HEARTBEATS export, so "
            f"every heartbeat it emits is discarded (export at {export}, call at {call})"
        )
        # ...and below every step it grades, or a step that ran THIS tick is graded a tick late.
        for graded in sorted(TICK_FINDING_FIELDS):
            step = text.find(f"_cadence_due {graded} ")
            assert step == -1 or step < call, (
                f"the tick-evidence step runs BEFORE the `{graded}` cadence step, so it can only "
                f"ever grade the previous tick's artifact"
            )

    with tempfile.TemporaryDirectory(prefix="tick-evidence-") as td:
        root = Path(td)
        ledger = root / "capabilities.json"
        state_dir = root / "state"
        state_dir.mkdir()

        # ---- PART 2: THE MECHANISM, on a wholly synthetic surface + registry + ledger.
        rows = {}
        for cid, kind in (
            ("obs-daily", "tick_phase"),
            ("obs-weekly", "tick_phase"),
            ("deliverer", "closer_gate"),
            ("no-projection", "tick_phase"),
        ):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "wired"
            cap["matcher"] = {"kind": kind, "name": cid}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        t_steps = {
            "obs-daily": {"key": "obs-daily", "artifact": "obs-daily.json", "cadence_days": 0},
            "obs-weekly": {"key": "obs-weekly", "artifact": "obs-weekly.json", "cadence_days": 6},
            "deliverer": {"key": "deliverer", "artifact": "deliverer.json", "cadence_days": 0},
            "no-projection": {"key": "no-projection", "artifact": "np.json", "cadence_days": 0},
        }
        real_fields = dict(TICK_FINDING_FIELDS)
        real_binding = capability_advisor.SURFACE_BINDINGS.get(TICK_SURFACE)
        TICK_FINDING_FIELDS.clear()
        TICK_FINDING_FIELDS.update(
            {
                "obs-daily": {"overdue": ("capability_id",), "regressed": ("capability_id",)},
                "obs-weekly": {"held_off": ("flag", "state")},
                "deliverer": {"findings": ("id",)},
                # `no-projection` deliberately absent: the "declared empty" case.
            }
        )
        capability_advisor.SURFACE_BINDINGS[TICK_SURFACE] = {
            "obs-daily": "synthetic observer, daily",
            "obs-weekly": "synthetic observer, weekly",
            "deliverer": "synthetic non-observer",
            "no-projection": "synthetic observer with no projection declared",
        }
        try:

            def write(name: str, payload: dict, mtime: int) -> None:
                p = state_dir / name
                p.write_text(json.dumps(payload), encoding="utf-8")
                os.utime(p, (mtime, mtime))

            # `silent_days` and `generated_at` MOVE between the two writes below; the finding
            # identities do not. That pairing is the trap the projection exists to survive.
            def daily(overdue, regressed, tick):
                return {
                    "generated_at": now + tick,
                    "overdue": [
                        {"capability_id": c, "silent_days": 1.0 + tick, "tolerance_days": 2.0}
                        for c in overdue
                    ],
                    "regressed": [
                        {"capability_id": c, "unchanged_for_days": tick} for c in regressed
                    ],
                }

            write("obs-daily.json", daily(["range-lane-rollout"], [], 1), now - 100)
            write(
                "obs-weekly.json",
                {
                    "generated_at": now,
                    "held_off": [
                        {
                            "flag": "ORCH_X",
                            "state": "off",
                            "criterion": "long prose that never changes",
                        }
                    ],
                },
                now - 100,
            )
            write("deliverer.json", {"findings": [{"id": "d1"}]}, now - 100)
            write("np.json", {"anything": 1}, now - 100)

            # 1. FIRST RUN IS A BASELINE AND RECORDS NO VERDICT. Inventing one from a single
            #    observation is exactly the manufactured evidence this design forbids.
            r1 = tick_evidence(now=now, state_dir=state_dir, path=ledger, steps=t_steps)
            assert r1["consulted"] is True, r1
            assert (
                r1["verdicts_recorded"] == 0
            ), f"a FIRST observation produced a verdict; there was nothing to compare against: {r1}"
            assert sorted(b["capability_id"] for b in r1["baselined"]) == [
                "deliverer",
                "no-projection",
                "obs-daily",
                "obs-weekly",
            ], r1["baselined"]
            assert r1["triggers_recorded"] == 4, r1
            # THE CONSULT MUST RETURN THE BOUND SET, asserted on what the CALLER receives rather
            # than on the binding table -- a suppression bug hid behind exactly that shortcut once.
            assert sorted(r1["advice_capabilities"]) == [
                "deliverer",
                "no-projection",
                "obs-daily",
                "obs-weekly",
            ], r1["advice_capabilities"]
            assert r1["gradable"] == ["obs-daily", "obs-weekly"], r1["gradable"]

            # 2. NO FRESH ARTIFACT -> NOTHING RECORDED AT ALL. This is the bound that keeps 24
            #    ticks a day from becoming 96 data points.
            for tick in range(2, 25):
                rn = tick_evidence(now=now + tick, state_dir=state_dir, path=ledger, steps=t_steps)
                assert rn["verdicts_recorded"] == 0, (tick, rn)
                assert rn["triggers_recorded"] == 0, (tick, rn)
                assert rn["matches_recorded"] == 0, (tick, rn)

            # 3. AN IDENTICAL REPORT IS NOT USEFUL, even though every counter and timestamp in it
            #    moved. THE central assertion: without the field projection this is `useful=True`
            #    and the ranking measures the calendar.
            write("obs-daily.json", daily(["range-lane-rollout"], [], 99), now + 1000)
            r3 = tick_evidence(now=now + 86400, state_dir=state_dir, path=ledger, steps=t_steps)
            got = {e["capability_id"]: e for e in r3["evaluated"]}
            assert got["obs-daily"]["graded"] is True, got
            assert got["obs-daily"]["useful"] is False, got["obs-daily"]
            assert "IDENTICAL" in got["obs-daily"]["evidence"], got["obs-daily"]["evidence"]
            assert r3["verdicts_recorded"] == 1, r3

            # 4. A NEW FINDING IS USEFUL, and the evidence names what moved.
            write("obs-daily.json", daily(["range-lane-rollout", "new-defect"], [], 5), now + 2000)
            r4 = tick_evidence(now=now + 2 * 86400, state_dir=state_dir, path=ledger, steps=t_steps)
            got = {e["capability_id"]: e for e in r4["evaluated"]}
            assert got["obs-daily"]["useful"] is True, got["obs-daily"]
            assert any(d.startswith("overdue +1") for d in got["obs-daily"]["changed_keys"]), got[
                "obs-daily"
            ]["changed_keys"]
            # ...and a finding that RESOLVED is also a change worth reporting.
            write("obs-daily.json", daily([], [], 7), now + 3000)
            r4b = tick_evidence(
                now=now + 3 * 86400, state_dir=state_dir, path=ledger, steps=t_steps
            )
            got = {e["capability_id"]: e for e in r4b["evaluated"]}
            assert got["obs-daily"]["useful"] is True, got["obs-daily"]
            # SILENCE IS NOT USEFULNESS: an empty finding set that STAYS empty is not useful.
            write("obs-daily.json", daily([], [], 8), now + 4000)
            r4c = tick_evidence(
                now=now + 4 * 86400, state_dir=state_dir, path=ledger, steps=t_steps
            )
            got = {e["capability_id"]: e for e in r4c["evaluated"]}
            assert got["obs-daily"]["useful"] is False, got["obs-daily"]

            # 5. A NON-OBSERVER GETS NO OUTPUT-CHANGE VERDICT, but its production IS recorded, so it
            #    never reads as "offered and skipped" when it really ran.
            write("deliverer.json", {"findings": [{"id": "d2"}]}, now + 5000)
            r5 = tick_evidence(now=now + 5 * 86400, state_dir=state_dir, path=ledger, steps=t_steps)
            got = {e["capability_id"]: e for e in r5["evaluated"]}
            assert got["deliverer"]["graded"] is False, got["deliverer"]
            assert got["deliverer"].get("reason") == "not_an_observer", got["deliverer"]
            assert "deliverer" not in r5["gradable"], r5["gradable"]
            # ...and a bound observer with NO declared projection is a stated verdict, not silence.
            write("np.json", {"anything": 2}, now + 5000)
            r5b = tick_evidence(
                now=now + 5 * 86400 + 1, state_dir=state_dir, path=ledger, steps=t_steps
            )
            got = {e["capability_id"]: e for e in r5b["evaluated"]}
            assert got["no-projection"].get("reason") == "no_finding_projection", got[
                "no-projection"
            ]

            # 6. A SHAPE CHANGE IS REPORTED, NEVER SCORED. A broken parse must not read as
            #    "nothing new" -- that is this repo's founding defect wearing a different hat.
            write("obs-weekly.json", {"generated_at": now, "renamed_bucket": []}, now + 6000)
            r6 = tick_evidence(now=now + 6 * 86400, state_dir=state_dir, path=ledger, steps=t_steps)
            got = {e["capability_id"]: e for e in r6["evaluated"]}
            assert got["obs-weekly"].get("reason") == "unprojectable", got["obs-weekly"]
            assert got["obs-weekly"]["graded"] is False, got["obs-weekly"]

            # 7. THE VERDICT LANDS AS REAL PROPENSITY EVIDENCE — assert through the PUBLIC surface
            #    the ranking actually reads, not through the state file this module wrote.
            # REAL clock, deliberately: `capabilities.heartbeat` stamps events with `_now()`, so
            # the 90-day window must be evaluated against the same clock that wrote them. Passing
            # the fixture clock here reads every event as out-of-window and the assertions below
            # would pass vacuously against zeroes.
            u = usefulness(path=ledger)["rows"]
            assert u["obs-daily"]["resolved"] >= 3, u["obs-daily"]
            assert u["obs-daily"]["useful"] >= 2, u["obs-daily"]
            assert u["obs-daily"]["usefulness_rate"] is not None, u["obs-daily"]
            assert u["deliverer"]["resolved"] == 0, "a non-observer must earn no output verdict"
            # The trial carries a real control arm: a bound candidate that did not run that day.
            trials = {t["experiment_id"]: t for t in experiments(path=ledger)}
            assert trials, "the tick produced no natural experiment at all"
            assert any(
                t["not_triggered"] for t in trials.values()
            ), "every trial triggered everything, so there is no control arm"
            assert any(
                TICK_SURFACE in (t["skills"] or []) for t in trials.values()
            ), "trials are not attributable to the tick surface, so demotion could never drain it"

            # 8. THE DAY CEILING IS STRUCTURAL. Force a second same-day evaluation and prove the
            #    idempotency key refuses it, so a bug in the freshness gate still cannot inflate.
            write("obs-daily.json", daily(["seed"], [], 0), now + 7000)
            tick_evidence(
                now=now + 6 * 86400, state_dir=state_dir, path=ledger, steps=t_steps
            )  # consumes day 6's single allowance
            before = usefulness(path=ledger)["rows"]["obs-daily"]["resolved"]
            assert (
                before >= 1
            ), f"the fixture produced no verdicts, so 'no further verdicts' is vacuous: {before}"
            for bump in range(1, 6):
                # Each write moves the mtime forward AND changes the findings, so the freshness gate
                # and the change test both say "record a verdict". Only the day-scoped idempotency
                # key stands between that and five more rows.
                write("obs-daily.json", daily([f"x{bump}"], [], bump), now + 7000 + bump)
                tick_evidence(
                    now=now + 6 * 86400 + bump, state_dir=state_dir, path=ledger, steps=t_steps
                )
            after = usefulness(path=ledger)["rows"]["obs-daily"]["resolved"]
            assert after == before, (
                f"five more same-day evaluations added {after - before} verdict(s); the day-scoped "
                f"experiment id must admit at most one per capability per day"
            )

            # 9. THE KILL SWITCH. Off means the tick behaves exactly as before this existed: no
            #    consult, no ledger event, no state write.
            write("obs-daily.json", daily(["kill-switch-probe"], [], 3), now + 9000)
            state_before = (state_dir / TICK_EVIDENCE_STATE).read_text(encoding="utf-8")
            resolved_before = usefulness(path=ledger)["rows"]
            os.environ["ORCH_TICK_EVIDENCE_DISABLED"] = "1"
            try:
                off = tick_evidence(
                    now=now + 9 * 86400, state_dir=state_dir, path=ledger, steps=t_steps
                )
                assert off.get("disabled") is True, f"a disabled run still did work: {off}"
                assert off["verdicts_recorded"] == 0, off
                assert off["evaluated"] == [] and off["bound"] == [], off
            finally:
                os.environ.pop("ORCH_TICK_EVIDENCE_DISABLED", None)
            assert (state_dir / TICK_EVIDENCE_STATE).read_text(
                encoding="utf-8"
            ) == state_before, "a disabled run wrote state"
            assert (
                usefulness(path=ledger)["rows"] == resolved_before
            ), "a disabled run wrote ledger evidence"
            # ...and with the switch back off, the same fresh artifact IS evaluated, so the
            # assertion above discriminates rather than describing an inert path.
            on = tick_evidence(now=now + 9 * 86400, state_dir=state_dir, path=ledger, steps=t_steps)
            assert on["verdicts_recorded"] == 1, on

            # 10. IT CANNOT TAKE THE TICK DOWN. A guarded run over a corrupt artifact and a broken
            #     registry must still return a report.
            (state_dir / "obs-daily.json").write_text("{not json", encoding="utf-8")
            os.utime(state_dir / "obs-daily.json", (now + 10_000, now + 10_000))
            r10 = tick_evidence_guarded(
                now=now + 10 * 86400, state_dir=state_dir, path=ledger, steps=t_steps
            )
            assert "unreadable_artifact" in {s["reason"] for s in r10["skipped"]}, r10["skipped"]
            broken = tick_evidence_guarded(
                now=now + 11 * 86400,
                state_dir=state_dir,
                path=ledger,
                steps={"obs-daily": {"artifact": None}},
            )
            assert "no_cadence_artifact" in {s["reason"] for s in broken["skipped"]}, broken
            assert format_tick_evidence(r10), "the tick log line must never be empty"
            assert "DISABLED" in format_tick_evidence({"disabled": True})
            # A ZERO BUDGET MUST MEAN ZERO WORK, not a silent promotion to the default. A control
            # that quietly does something other than what it says is worse than no control.
            write(
                "obs-weekly.json", {"held_off": [{"flag": "ORCH_Y", "state": "off"}]}, now + 12_000
            )
            starved = tick_evidence_guarded(
                now=now + 12 * 86400, state_dir=state_dir, path=ledger, steps=t_steps, budget_s=0
            )
            assert "budget_exhausted" in {s["reason"] for s in starved["skipped"]}, starved
            assert starved["verdicts_recorded"] == 0, starved
        finally:
            TICK_FINDING_FIELDS.clear()
            TICK_FINDING_FIELDS.update(real_fields)
            if real_binding is None:
                capability_advisor.SURFACE_BINDINGS.pop(TICK_SURFACE, None)
            else:
                capability_advisor.SURFACE_BINDINGS[TICK_SURFACE] = real_binding

    print(
        "capability_propensity tick-evidence selftest: OK (baseline first, identical output is "
        "NOT useful, moving counters are projected away, non-observers get no output verdict, "
        "shape change reported not scored, one verdict per capability per day, kill switch inert)"
    )


def record_decline(
    capability_id: str,
    experiment_id: str,
    *,
    reason: str,
    surface: str = "",
    kind: str = DECLINE_KIND_DEFAULT,
    path=None,
    metadata: dict | None = None,
) -> bool:
    """This candidate was OFFERED and deliberately NOT used, for a stated reason.

    THE THIRD STATE. `record_trigger` says it ran; `record_usefulness` says whether running helped.
    Neither can express "it was the wrong tool here, and here is why" — and until this existed a
    reasoned rejection was byte-identical in the ledger to a capability nobody ever considered.

    WHAT THIS MUST NOT DO, and structurally cannot. It writes a `match`, never an `outcome`, so it
    can never enter the `useful`/`not_useful` buckets `propensity()` is computed from. Recording a
    decline as a negative outcome would assert that we tried it and it did not help — a false
    statement about something that never ran, and it would corrupt the one signal declines exist to
    sharpen.

    `reason` is REQUIRED and refused when blank, exactly as `record_usefulness` refuses an
    unevidenced verdict: an unexplained decline is indistinguishable from inattention, which is the
    state this replaces. `surface` is optional but load-bearing — without it the decline is recorded
    and readable but cannot be attributed to a surface, so it cannot feed `propose_demotions`.

    `kind` says WHAT KIND of decline, from `DECLINE_KINDS`, because the kinds imply opposite fixes:
    `wrong_match` indicts the binding, while `no_landing_zone` says the match was correct and the
    deliverable had nowhere to put the result. An unknown kind is refused rather than coerced — a
    typo silently becoming `unspecified` would hide the classification the caller thought it made.
    Omitting it yields `unspecified`, which is recorded and can never demote.

    Idempotent per (capability, experiment), so replaying a backfill cannot inflate the count.
    """
    if not str(reason).strip():
        raise ValueError(
            "a decline requires a reason naming why this capability was not the right "
            "tool here; an unexplained decline is indistinguishable from inattention"
        )
    if str(kind) not in DECLINE_KINDS:
        raise ValueError(f"unknown decline kind {kind!r}; expected one of {sorted(DECLINE_KINDS)}")
    if not experiment_id.startswith(ADVICE_REF_PREFIX):
        raise ValueError(f"experiment_id must start with {ADVICE_REF_PREFIX!r}: {experiment_id!r}")
    return capabilities.heartbeat(
        capability_id,
        "match",
        ref=experiment_id,
        path=path or capabilities.REG,
        idempotency_key=f"decline:{capability_id}:{experiment_id}",
        metadata={
            "source": DECLINE_SOURCE,
            DECLINE_REASON_KEY: str(reason)[:400],
            DECLINE_KIND_KEY: str(kind),
            SURFACE_KEY: surface or None,
            **(metadata or {}),
        },
    )


# ---------------------------------------------------------------------------
# DEFECT FINDS — what was found, and by whom. The strongest signal the loop was throwing away.
#
# MEASURED (2026-08-23). Instrumented work found SEVEN defects in this system's own code that its
# author had not found. TWO were attributable to a capability and were recorded:
# `adversarial-review` supplied citations that became the strongest facts in two issue bodies, and
# `deliberate-break-verifier` caught an auditor's own methodological error. The other FIVE were
# found by the PROCESS -- an audit noticing that a suppressed surface still offered capabilities; an
# agent reading this module and finding a branch that recorded nothing. Those had no capability to
# attribute to, so they became PRs and prose and taught the loop nothing at all.
#
# SO THE FINDER MAY BE A CAPABILITY *OR* A SURFACE, and the two feed different things:
#
#   * CAPABILITY-attributed -> that capability's USEFULNESS, at `defect_found` provenance. A defect
#     found is an OUTCOME, not an opinion: the artifact naming it is checkable by someone who was
#     not there. It reaches the posterior through the provenance weighting and by NO other route.
#   * SURFACE-attributed -> BINDING QUALITY. "Consulting at `repo-audit:phase-1` surfaced a defect
#     in the advisor itself" is evidence about the SURFACE, not about any capability, and there was
#     nowhere at all to put it. `binding_quality()` is that place.
#
# NO NEW STORE AND NO NEW EVENT TYPE (`CLAUDE.md` forbids a second one). A find rides on a `match`
# event tagged `source=capability_find`, exactly as `record_decline` and `record_promotion` already
# carry non-match facts there. Its ref is `find:<digest>`, NOT `advice:<digest>` -- so
# `_experiment_id()` returns None for it and `experiments()`, `usefulness()` and `propensity()`
# cannot see it AT ALL. That is structural, not conventional: no metadata a caller could set would
# make a find record reach a posterior.
#
# IT MUST NOT BECOME A WAY TO INFLATE A CAPABILITY'S STANDING. Three guards, and the third is the
# one that actually binds:
#   1. `artifact` is REQUIRED and refused when blank, exactly as `record_usefulness` refuses an
#      unevidenced verdict. A CLAIMED find with no artifact is worth nothing.
#   2. `defect` is REQUIRED and must name WHAT was defective, not that something was found.
#   3. The correlated-arm discount caps it. N finds from one judge arm total ONE observation, so ten
#      artifact-backed finds from one agent cannot lift a capability past 0.667; only an independent
#      arm can. The incentive is corroboration, not volume.
FIND_SOURCE = "capability_find"
FIND_REF_PREFIX = "find:"
# The carrier row for a SURFACE-attributed find. The ledger is keyed by capability, so a fact about
# a surface needs a row to sit on; this module owns binding quality (`detect`, `propose_bindings`,
# `propose_demotions`), so its own row is the honest carrier. The record is `find:`-reffed, so it
# sits outside every rate on every row INCLUDING this one -- it cannot flatter its own carrier.
FIND_CARRIER = "capability-propensity"
FIND_DEFECT_KEY = "defect"
FIND_ARTIFACT_KEY = "artifact"
FIND_SUBJECT_KEY = "subject"
FIND_FINDER_KEY = "finder"
FIND_FINDER_KIND_KEY = "finder_kind"


def find_id(defect: str, *, surface: str = "", capability_id: str = "") -> str:
    """A stable id for one find, so replaying a backfill cannot inflate the count.

    Keyed on the DEFECT plus its finder, not on a timestamp: the same defect found again by the same
    finder is the same find, and a second record of it is not a second piece of evidence.
    """
    payload = "|".join((str(defect).strip().lower(), str(surface or ""), str(capability_id or "")))
    return f"{FIND_REF_PREFIX}{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def record_find(
    *,
    defect: str,
    artifact: str,
    surface: str = "",
    capability_id: str = "",
    experiment_id: str = "",
    subject: str = "",
    judge: str = "",
    path=None,
) -> dict:
    """A defect was found. Record WHO found it and WHAT proves it.

    `defect` names what was defective. `artifact` is the thing a stranger could check — the PR, the
    issue, the file:line, the failing test. Both are REQUIRED and refused when blank, for the same
    reason `record_usefulness` refuses an unevidenced verdict and `record_decline` an unexplained
    one: a claimed find with no artifact is worth nothing, and letting one through would make this
    the cheapest way to inflate a capability's standing.

    THE FINDER IS EITHER A CAPABILITY OR A SURFACE, and at least one must be named:

      * `capability_id` (with the `experiment_id` it was offered under) -> the find ALSO writes a
        usefulness verdict at `defect_found` provenance, whose `corroboration` is the artifact. That
        is the ONLY path from a find to a posterior, and it is the provenance weighting -- so N finds
        from one judge arm still total one observation.
      * `surface` alone -> the find feeds BINDING QUALITY only. "Consulting here surfaced a defect"
        is evidence about the surface, and `binding_quality()` reads it. It touches no posterior.

    `subject` optionally names what the defect was IN (a module, a capability, a doc). Recorded for
    the audit trail; it is never scored, because a capability must not be debited for having had a
    bug found in it.

    Idempotent on `find_id`, so a replay records nothing twice. Returns what happened, including
    `affects_propensity`, because a caller that thinks it just scored a capability has been misled.
    """
    if not str(defect).strip():
        raise ValueError("a find requires `defect` naming WHAT was defective")
    if not str(artifact).strip():
        raise ValueError(
            "a find requires `artifact` — the PR, issue, file:line or failing test a stranger "
            "could check; a claimed find with no artifact is worth nothing"
        )
    if not str(surface).strip() and not str(capability_id).strip():
        raise ValueError(
            "a find needs a FINDER: either `capability_id` (the capability that surfaced it) or "
            "`surface` (the surface whose consult surfaced it). An unattributed find teaches "
            "nothing, which is the state this replaces"
        )
    if str(capability_id).strip() and not str(experiment_id).startswith(ADVICE_REF_PREFIX):
        raise ValueError(
            "a capability-attributed find must carry the `experiment_id` "
            f"({ADVICE_REF_PREFIX}<digest>) it was offered under, or its usefulness verdict "
            "belongs to no trial"
        )
    cap_id = str(capability_id).strip()
    finder_kind = "capability" if cap_id else "surface"
    finder = cap_id or str(surface).strip()
    ref = find_id(defect, surface=surface, capability_id=cap_id)
    carrier = cap_id or FIND_CARRIER
    recorded = capabilities.heartbeat(
        carrier,
        "match",
        ref=ref,
        path=path or capabilities.REG,
        idempotency_key=f"find:{finder}:{ref}",
        metadata={
            "source": FIND_SOURCE,
            FIND_DEFECT_KEY: str(defect)[:400],
            FIND_ARTIFACT_KEY: str(artifact)[:400],
            FIND_SUBJECT_KEY: str(subject)[:200] or None,
            FIND_FINDER_KEY: finder,
            FIND_FINDER_KIND_KEY: finder_kind,
            SURFACE_KEY: str(surface).strip() or None,
        },
    )
    verdict = False
    if cap_id:
        verdict = record_usefulness(
            cap_id,
            experiment_id,
            useful=True,
            evidence=f"found a defect: {str(defect)[:280]}",
            provenance="defect_found",
            judge=judge,
            corroboration=str(artifact),
            path=path,
            metadata={SURFACE_KEY: str(surface).strip() or None},
        )
    return {
        "find_id": ref,
        "recorded": bool(recorded),
        "finder": finder,
        "finder_kind": finder_kind,
        "carrier": carrier,
        "surface": str(surface).strip() or None,
        "subject": str(subject).strip() or None,
        "feeds": "capability_usefulness" if cap_id else "binding_quality",
        "usefulness_recorded": bool(verdict),
        # SAY WHAT THIS DID. A surface find scores nothing; a capability find scores one
        # PROVENANCE-WEIGHTED observation, shared with every other verdict from the same arm.
        "affects_propensity": bool(cap_id),
        "provenance": "defect_found" if cap_id else None,
    }


def finds(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> list[dict]:
    """Every recorded find in the window, from the ledger. No second store to read."""
    caps = capabilities.load_declared(path or capabilities.REG)
    now = capabilities._now() if now is None else now
    out = []
    for cap_id, cap in sorted(caps.items()):
        for event in _events(cap):
            meta = event.get("metadata") or {}
            if meta.get("source") != FIND_SOURCE:
                continue
            if not _within_window(event, now=now, window_days=window_days):
                continue
            out.append(
                {
                    "find_id": str(event.get("ref") or ""),
                    "carrier": cap_id,
                    "finder": str(meta.get(FIND_FINDER_KEY) or ""),
                    "finder_kind": str(meta.get(FIND_FINDER_KIND_KEY) or ""),
                    "surface": meta.get(SURFACE_KEY),
                    "defect": str(meta.get(FIND_DEFECT_KEY) or ""),
                    "artifact": str(meta.get(FIND_ARTIFACT_KEY) or ""),
                    "subject": meta.get(FIND_SUBJECT_KEY),
                    "timestamp": int(event.get("timestamp") or 0),
                }
            )
    return sorted(out, key=lambda f: (f["timestamp"], f["find_id"]))


def binding_quality(surface: str, *, path=None, window_days: int = WINDOW_DAYS) -> dict:
    """Is consulting at this surface producing anything? THE PLACE A SURFACE FIND HAD TO GO.

    The three layers rank a capability by fit to a surface. Nothing measured the surface itself, so
    "consulting at `repo-audit:phase-1` surfaced a defect in the advisor" was evidence with no home.
    This is the home: the bound set, what it was offered and what came of it, and the FINDS the
    consults at this surface produced -- capability-attributed and surface-attributed separately,
    because only the first is also a verdict on a capability.

    REPORT ONLY. Nothing here promotes, demotes or scores; `propose_bindings` and
    `propose_demotions` keep their existing, external, evidence rules unchanged. A surface find is a
    number about a surface, and a number about a surface must not become selection pressure on a
    capability -- that is the ratchet the detection loop already refuses.
    """
    import capability_advisor

    counts = surface_decline_counts(surface, path=path, window_days=window_days)
    here = [
        f
        for f in finds(path=path, window_days=window_days)
        if f["surface"] == surface or f["finder"] == surface
    ]
    by_kind: dict[str, int] = {}
    for f in here:
        by_kind[f["finder_kind"]] = by_kind.get(f["finder_kind"], 0) + 1
    return {
        "surface": surface,
        "bound": sorted(capability_advisor.binding_for(surface, path=path)),
        "offers": sum(counts["offered"].values()),
        "triggers": sum(counts["triggered"].values()),
        "declines": sum(counts["declined"].values()),
        # BOTH quantities: how many defects the consults here surfaced, and how many of those are
        # ALSO a verdict on a capability. A surface that produces finds while triggering nothing is
        # not an idle surface, and those were indistinguishable before this existed.
        "finds": len(here),
        "finds_by_finder_kind": dict(sorted(by_kind.items())),
        "find_subjects": sorted({f["subject"] for f in here if f["subject"]}),
        "find_defects": [f["defect"] for f in here][:10],
        "window_days": window_days,
    }


# ---------------------------------------------------------------------------
# THE REPAIR CHANNEL — because "worth having AND broken" was unrepresentable.
#
# The loop had exactly two actions: PROMOTE (widen a binding) and DEMOTE (narrow one). Neither can
# say "this capability should exist and does not work", so the only available response to a broken
# capability was to stop offering it — which silences the thing that should be fixed and loses a
# capability that was worth having.
#
# THE LIVE CASE, and it is why this exists. `repo-playbook` sits at one useful verdict and one
# not-useful verdict, and the Fine-Art-Archive audit documented exactly WHY: its useful content is
# gated behind `task_type: implement/testgen/mechanical`, so a `review` consult receives 308
# characters, one clause of which is factually wrong — it tells auditors a repository's default
# branch is something it is not. Demotion silences that. A repair proposal names it.
#
# TWO INPUTS, and the second is the one the taxonomy was missing an action for:
#   1. `not_useful` verdicts, WITH THEIR EVIDENCE CARRIED FORWARD, so the proposal is actionable
#      rather than a flag. "0.5, one bad verdict" is a number; "308 characters, one clause factually
#      wrong about the default branch" is a repair.
#   2. Declines whose KIND indicates a DEFECT — `decline_kind_repairable`, i.e. `wrong_match` (the
#      matcher may be wrong) and `precondition_unmet` (an undeclared or unevaluated precondition is
#      a defect in the capability, and it is NOT demotable, so this channel is the only one that can
#      act on it at all). Explicitly NOT `no_landing_zone`: nobody's fault, the match was correct,
#      the capability is working. Proposing a repair there asserts a defect that does not exist.
#
# REPORT-ONLY, NEVER AUTO-APPLIED, and it never queues anything for the owner (`CLAUDE.md` §3). It
# is a field in a report the cadence step already writes; nothing waits on a human, nothing expires
# against a human, and no human action can be behind on it. ATTENTION COST: the live ledger produces
# 13 proposal rows inside an existing hourly/6-daily report, requiring zero actions and expiring on
# their own with `WINDOW_DAYS`. 0 minutes/week.
#
# LATCHED-GATE ANSWERS (a proposal set is a gate, so it owes all three in writing):
#
#   1. WHAT DECREMENTS IT? `record_repair` — a named mechanism that writes a durable marker with the
#      fix and its artifact, after which a proposal counts only defect evidence NEWER than that
#      marker. Not "time passes" and not "someone notices". Window expiry is a SECOND drain and uses
#      the same `WINDOW_DAYS` constant, so it cannot drift from the measurement.
#      The first draft of this had NO marker: defect evidence stayed in the 90-day window, so fixing
#      the capability did not clear its proposal for three months. That is the latch, and it was
#      caught by asking question 1 rather than by testing.
#   2. CAN THE DRAIN RUN WHILE THE GATE IS NON-EMPTY? Yes, unconditionally. `record_repair` requires
#      nothing a standing proposal forbids, and a proposal is report-only on both sides: it never
#      withholds the capability from `rank()`, never lowers its propensity, and never blocks a
#      consult. So the capability keeps being offered, keeps being able to earn useful verdicts, and
#      the repair can be recorded at any moment — including while the proposal stands.
#   3. DOES THE MEASURING WINDOW EQUAL THE DRAINING WINDOW? Yes, by construction: `WINDOW_DAYS`, the
#      one constant `usefulness()`, `propensity()` and `surface_decline_counts()` already share,
#      bounds the defect evidence counted AND the repair markers that clear it. One name, consumed by
#      both — a matching pair of literals would drift.
#
#   And the runtime rule: every proposal reports `defect_evidence_total` (measuring), and
#   `defect_evidence_since_repair` (blocking) beside `repairs_recorded` (drainable), so
#   "13 proposals" can never read as patience when it should read as a repair that was never
#   recorded. `report()` carries `repairs_recorded` even when the proposal list is EMPTY, because an
#   empty list cannot say whether anything is accumulating.
#
# NO NEW STORE. A repair marker rides a `match` event tagged `source=capability_repair` with a
# `repair:<digest>` ref — same carrier and same structural exclusion as a find, so a marker can
# never reach a posterior either.
REPAIR_SOURCE = "capability_repair"
REPAIR_REF_PREFIX = "repair:"
REPAIR_FIX_KEY = "fix"
REPAIR_ARTIFACT_KEY = "artifact"
# ONE piece of evidenced defect evidence is enough to PROPOSE, because a proposal costs nothing and
# is never applied. The floor exists to be stated, not to hold anything shut: raising it would make
# the channel silent about exactly the single-verdict case (`repo-playbook`) it was built for.
REPAIR_MIN_DEFECT_EVIDENCE = 1


def record_repair(
    capability_id: str, *, fix: str, artifact: str, path=None, timestamp: int | None = None
) -> bool:
    """THE DRAIN. Record that a proposed repair was actually MADE.

    `fix` says what was changed; `artifact` is the PR, commit or file:line a stranger could check.
    Both are REQUIRED and refused when blank, for the same reason a find and a verdict are: a
    claimed repair with nothing to check would clear a proposal without fixing anything, which is
    worse than no drain at all.

    After this, `propose_repair` counts only defect evidence recorded AFTER the marker. That is what
    makes the gate drainable by an ACTION rather than by the calendar — the first draft had no
    marker, so a repaired capability kept its proposal for the whole 90-day window.
    """
    if not str(fix).strip():
        raise ValueError("a repair record must say what was FIXED")
    if not str(artifact).strip():
        raise ValueError(
            "a repair record requires an `artifact` — the PR, commit or file:line a stranger "
            "could check; a claimed repair with nothing to check would clear a proposal without "
            "fixing anything"
        )
    digest = hashlib.sha256(f"{capability_id}|{str(fix).strip().lower()}".encode()).hexdigest()[:12]
    return capabilities.heartbeat(
        capability_id,
        "match",
        ref=f"{REPAIR_REF_PREFIX}{digest}",
        path=path or capabilities.REG,
        idempotency_key=f"repair:{capability_id}:{digest}",
        # `timestamp` exists so a selftest can lay events out in TIME. Ledger timestamps are
        # second-granular, and a test that records the defect, the repair and the re-opening
        # evidence inside one second cannot distinguish "the action drained it" from "the tie-break
        # happened to go this way" -- which is the whole property under test.
        timestamp=timestamp,
        metadata={
            "source": REPAIR_SOURCE,
            REPAIR_FIX_KEY: str(fix)[:400],
            REPAIR_ARTIFACT_KEY: str(artifact)[:400],
        },
    )


def repair_markers(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> dict:
    """Per capability: how many repairs were recorded, and when the latest one was.

    Read with the SAME window as the defect evidence it clears -- `WINDOW_DAYS`, the one constant.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    now = capabilities._now() if now is None else now
    out: dict[str, dict] = {}
    for cap_id, cap in sorted(caps.items()):
        for event in _events(cap):
            meta = event.get("metadata") or {}
            if meta.get("source") != REPAIR_SOURCE:
                continue
            if not _within_window(event, now=now, window_days=window_days):
                continue
            ts = int(event.get("timestamp") or 0)
            row = out.setdefault(cap_id, {"count": 0, "latest_ts": 0, "records": []})
            row["count"] += 1
            row["latest_ts"] = max(row["latest_ts"], ts)
            row["records"].append(
                {
                    "timestamp": ts,
                    "fix": str(meta.get(REPAIR_FIX_KEY) or ""),
                    "artifact": str(meta.get(REPAIR_ARTIFACT_KEY) or ""),
                }
            )
    return out


def defect_evidence(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> dict:
    """Per capability: every in-window record saying the capability itself is BROKEN.

    Derived from `experiments()` rather than from a fresh ledger scan, so it inherits every
    refinement that already exists there — a decline the capability was later TRIGGERED for does not
    count, and the trial-level window is the same one. Timestamps and evidence text then come from
    the events those trials were built from, because a proposal without the words is a flag.
    """
    trials = {
        t["experiment_id"]: t for t in experiments(path=path, window_days=window_days, now=now)
    }
    caps = capabilities.load_declared(path or capabilities.REG)
    now = capabilities._now() if now is None else now
    out: dict[str, list[dict]] = {}
    for cap_id, cap in sorted(caps.items()):
        for event in _events(cap):
            exp = _experiment_id(event)
            trial = trials.get(exp) if exp else None
            if trial is None or not _within_window(event, now=now, window_days=window_days):
                continue
            meta = event.get("metadata") or {}
            etype = event.get("type") or event.get("event_type")
            ts = int(event.get("timestamp") or 0)
            if etype == "outcome" and cap_id in trial["not_useful"]:
                out.setdefault(cap_id, []).append(
                    {
                        "basis": "not_useful_verdict",
                        "experiment_id": exp,
                        "timestamp": ts,
                        # CARRIED FORWARD, deliberately. The whole difference between a flag and an
                        # actionable proposal is that the words travel with it.
                        "evidence": str(meta.get("evidence") or ""),
                        "provenance": verdict_provenance(meta),
                        "surface": meta.get(SURFACE_KEY) or meta.get("skill"),
                        "implied_fix": "the capability's own behaviour on this task",
                    }
                )
            elif etype == "match" and meta.get("source") == DECLINE_SOURCE:
                if cap_id not in trial["declined"]:
                    continue
                kind = str(meta.get(DECLINE_KIND_KEY) or DECLINE_KIND_DEFAULT)
                if not decline_kind_repairable(kind):
                    continue
                out.setdefault(cap_id, []).append(
                    {
                        "basis": "declined_repairable",
                        "decline_kind": kind,
                        "experiment_id": exp,
                        "timestamp": ts,
                        "evidence": str(meta.get(DECLINE_REASON_KEY) or ""),
                        "surface": meta.get(SURFACE_KEY),
                        "implied_fix": DECLINE_KINDS[kind]["fix"],
                    }
                )
    return {c: sorted(v, key=lambda r: r["timestamp"]) for c, v in out.items()}


def propose_repair(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> list:
    """The third action: this capability is worth having and something about it is BROKEN.

    REPORT-ONLY. Nothing here is applied, nothing is queued for anyone, and a proposal changes
    neither the candidate set nor any propensity — so a wrong proposal costs a line in a report.

    `worth_having` is REPORTED, never a filter. Filtering on it would hide a capability whose only
    evidence is negative, and that population is the demotion path's business; both readings must
    stay visible, because "broken and wanted" and "broken and unwanted" imply different work.
    """
    evidence = defect_evidence(path=path, window_days=window_days, now=now)
    markers = repair_markers(path=path, window_days=window_days, now=now)
    stats = usefulness(path=path, window_days=window_days, now=now)["rows"]
    out = []
    for cap_id, records in sorted(evidence.items()):
        marker = markers.get(cap_id) or {"count": 0, "latest_ts": 0, "records": []}
        # `>=`, NOT `>`, and the tie-break direction is the point. Ledger timestamps are
        # second-granular, so a defect recorded in the same second as a repair is UNORDERABLE — and
        # with a strict `>` it would be excluded forever, which is silence. A gate must fail toward
        # MOTION: an unorderable defect re-opens the proposal, which costs one report line, rather
        # than vanishing, which costs the finding. The drain is unaffected, because the evidence a
        # repair is answering is strictly older than the repair that answers it.
        fresh = [r for r in records if r["timestamp"] >= marker["latest_ts"]]
        if len(fresh) < REPAIR_MIN_DEFECT_EVIDENCE:
            continue
        row = stats.get(cap_id) or {}
        bases = sorted({r["basis"] for r in fresh})
        kinds: dict[str, int] = {}
        for r in fresh:
            if r.get("decline_kind"):
                kinds[r["decline_kind"]] = kinds.get(r["decline_kind"], 0) + 1
        useful_n = int(row.get("useful") or 0)
        out.append(
            {
                "capability_id": cap_id,
                "action": "repair",
                "basis": bases,
                # WORTH HAVING, reported beside the defect rather than gating it.
                "worth_having": useful_n > 0,
                "worth_having_basis": (
                    f"{useful_n} useful verdict(s) in the window"
                    if useful_n
                    else "no useful verdict yet — the defect evidence stands alone, so read this "
                    "beside the demotion proposals rather than instead of them"
                ),
                "useful": useful_n,
                "not_useful": int(row.get("not_useful") or 0),
                "propensity": propensity(cap_id, path=path, window_days=window_days, now=now)[
                    "propensity"
                ],
                # THE WORDS, carried forward. This is what makes it a repair and not a flag.
                "evidence": [r["evidence"] for r in fresh if r["evidence"]][:5],
                "implied_fixes": sorted({r["implied_fix"] for r in fresh if r["implied_fix"]}),
                "declines_repairable_by_kind": dict(sorted(kinds.items())),
                "surfaces": sorted({str(r["surface"]) for r in fresh if r.get("surface")}),
                # MEASURING quantity, BLOCKING quantity, DRAINABLE quantity — all three, always.
                "defect_evidence_total": len(records),
                "defect_evidence_since_repair": len(fresh),
                "repairs_recorded": marker["count"],
                "last_repair_at": marker["latest_ts"] or None,
                "defect_evidence_floor": REPAIR_MIN_DEFECT_EVIDENCE,
                "window_days": window_days,
                "auto_applied": False,
                "queued_for_owner": False,
            }
        )
    # Worth-having first, then most defect evidence: a capability that is wanted AND broken is the
    # case this channel exists for, and it must not sit below one that is merely broken.
    return sorted(
        out,
        key=lambda r: (
            not r["worth_having"],
            -r["defect_evidence_since_repair"],
            r["capability_id"],
        ),
    )


def _selftest_repair() -> None:
    """ "WORTH HAVING AND BROKEN" MUST BE EXPRESSIBLE, and the channel must be drainable BY ACTION.

    Every assertion was written by breaking it first:

      * making `no_landing_zone` repairable -> a CORRECT match is proposed for repair, caught here;
      * making `precondition_unmet` NON-repairable -> the one kind that has no other channel goes
        silent again, caught here;
      * dropping the repair marker from the freshness comparison -> a repaired capability keeps its
        proposal for the whole 90-day window, which is the latch; caught here;
      * dropping the artifact guard on `record_repair` -> a claimed repair with nothing to check
        clears a proposal, caught here;
      * dropping the carried-forward evidence -> the proposal becomes a flag, caught here;
      * letting a repair proposal touch the posterior -> caught here.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="repair-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("worth-fixing", "correct-match", "precondition-only", "healthy"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["review"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        # THE LIVE SHAPE: one useful verdict, one evidenced not-useful verdict, one wrong_match
        # decline. Worth having AND broken -- the state the two-action loop could not express.
        capabilities.heartbeat(
            "worth-fixing",
            "match",
            ref="advice:wf00000001",
            path=ledger,
            idempotency_key="m:wf1",
            metadata={"surface": "repo-audit:phase-4"},
        )
        record_trigger("worth-fixing", "advice:wf00000001", path=ledger)
        record_usefulness(
            "worth-fixing",
            "advice:wf00000001",
            useful=True,
            evidence="returned the repo-specific rule that changed the scope boundary",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        )
        capabilities.heartbeat(
            "worth-fixing",
            "match",
            ref="advice:wf00000002",
            path=ledger,
            idempotency_key="m:wf2",
            metadata={"surface": "repo-audit:phase-2"},
        )
        record_trigger("worth-fixing", "advice:wf00000002", path=ledger)
        record_usefulness(
            "worth-fixing",
            "advice:wf00000002",
            useful=False,
            evidence="308 chars that changed no finding, and one clause is factually wrong: it "
            "names a default branch this repo does not have",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        )
        record_decline(
            "worth-fixing",
            "advice:wf00000003",
            reason="the review path receives only the gated summary",
            kind="wrong_match",
            surface="repo-audit:phase-2",
            path=ledger,
        )
        # A CORRECT MATCH the deliverable made impossible. Never a repair candidate.
        for i in range(4):
            record_decline(
                "correct-match",
                f"advice:cm0000000{i}",
                reason=f"read-only audit, no commit target ({i})",
                kind="no_landing_zone",
                surface="repo-audit:phase-4",
                path=ledger,
            )
        # THE KIND WITH NO OTHER CHANNEL: not demotable, so before this it was inert forever.
        for i in range(3):
            record_decline(
                "precondition-only",
                f"advice:po0000000{i}",
                reason=f"aimed at another system's runtime ({i})",
                kind="precondition_unmet",
                surface="repo-audit:dimension-8",
                path=ledger,
            )
        # ...and one that is simply fine.
        capabilities.heartbeat(
            "healthy",
            "match",
            ref="advice:hh00000001",
            path=ledger,
            idempotency_key="m:hh1",
            metadata={"surface": "repo-audit:phase-1"},
        )
        record_trigger("healthy", "advice:hh00000001", path=ledger)
        record_usefulness(
            "healthy",
            "advice:hh00000001",
            useful=True,
            evidence="found two defects",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        )

        props = {p["capability_id"]: p for p in propose_repair(path=ledger)}
        # ---- 1. THE THIRD ACTION EXISTS, and it is not promote or demote.
        assert "worth-fixing" in props, props
        wf = props["worth-fixing"]
        assert wf["action"] == "repair", wf
        assert wf["worth_having"] is True and wf["useful"] == 1, wf
        assert sorted(wf["basis"]) == ["declined_repairable", "not_useful_verdict"], wf
        # THE WORDS TRAVEL WITH IT. A proposal without the evidence is a flag.
        assert any("factually wrong" in e for e in wf["evidence"]), wf["evidence"]
        assert any("gated summary" in e for e in wf["evidence"]), wf["evidence"]
        assert wf["implied_fixes"], wf
        assert wf["surfaces"] == ["repo-audit:phase-2"], wf
        # ---- 2. A CORRECT MATCH IS NEVER PROPOSED FOR REPAIR. `no_landing_zone` is nobody's fault
        #         and the capability is working; four of them must produce nothing.
        assert "correct-match" not in props, props
        # ---- 3. THE KIND WITH NO OTHER CHANNEL IS REACHED. `precondition_unmet` cannot demote, so
        #         without this it accumulated forever with no action available.
        assert "precondition-only" in props, props
        po = props["precondition-only"]
        assert po["declines_repairable_by_kind"] == {"precondition_unmet": 3}, po
        assert po["worth_having"] is False, po
        assert "no useful verdict yet" in po["worth_having_basis"], po
        # ...and worth-having sorts FIRST, because wanted-and-broken is the case this is for.
        assert (
            list(props)[0] == "worth-fixing"
            or [p["capability_id"] for p in propose_repair(path=ledger)][0] == "worth-fixing"
        ), list(props)
        # ---- 4. A HEALTHY CAPABILITY IS NOT PROPOSED.
        assert "healthy" not in props, props
        # ---- 5. NEVER APPLIED, NEVER QUEUED. §3 forbids a human touchpoint that can accumulate.
        for p in propose_repair(path=ledger):
            assert p["auto_applied"] is False and p["queued_for_owner"] is False, p
        # ---- 6. BOTH QUANTITIES ON EVERY ROW, per the runtime rule.
        assert wf["defect_evidence_total"] == 2, wf
        assert wf["defect_evidence_since_repair"] == 2, wf
        assert wf["repairs_recorded"] == 0 and wf["last_repair_at"] is None, wf
        # ---- 7. IT DOES NOT TOUCH THE POSTERIOR. A repair proposal is not a verdict.
        before = propensity("worth-fixing", path=ledger)
        propose_repair(path=ledger)
        after = propensity("worth-fixing", path=ledger)
        assert after["propensity"] == before["propensity"], (before, after)
        assert after["evidence_weight"] == before["evidence_weight"], (before, after)

        # ---- 8. THE DRAIN RUNS BY ACTION, NOT BY THE CALENDAR. This is the latched-gate answer,
        #         asserted: recording the repair clears the proposal immediately, and the same
        #         defect evidence is still inside the window. Explicit timestamps, because every
        #         event above landed in the SAME SECOND and a same-second comparison cannot
        #         distinguish "the action drained it" from "the tie-break went this way".
        base = capabilities._now()
        assert record_repair(
            "worth-fixing",
            fix="ungate the review path so a review consult receives the repo facts",
            artifact="PR #999, repo_knowledge.py:812",
            path=ledger,
            timestamp=base + 3600,
        )
        drained = {p["capability_id"]: p for p in propose_repair(path=ledger)}
        assert "worth-fixing" not in drained, drained
        # ...and the evidence really is still there, so this proves an ACTION cleared it and not
        # the window. A break that dropped the marker comparison would leave the proposal standing.
        assert len(defect_evidence(path=ledger)["worth-fixing"]) == 2, defect_evidence(path=ledger)
        # ...and the repair is VISIBLE, so a cleared proposal is not an unexplained silence.
        assert repair_markers(path=ledger)["worth-fixing"]["count"] == 1
        # ...and a REPEAT is idempotent: the same fix twice is one repair.
        assert (
            record_repair(
                "worth-fixing",
                fix="Ungate The Review Path So A Review Consult Receives The Repo Facts",
                artifact="same PR",
                path=ledger,
            )
            is False
        )
        # ---- 9. AND NEW defect evidence after the repair RE-OPENS it, so a recorded repair is not
        #         a permanent silencer -- the gate fails toward motion in both directions.
        record_usefulness(
            "worth-fixing",
            "advice:wf00000009",
            useful=False,
            evidence="still gated for review consults after the fix",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
            timestamp=base + 7200,
        )
        reopened = {p["capability_id"]: p for p in propose_repair(path=ledger)}
        assert "worth-fixing" in reopened, reopened
        assert reopened["worth-fixing"]["repairs_recorded"] == 1, reopened["worth-fixing"]
        assert reopened["worth-fixing"]["defect_evidence_since_repair"] == 1, reopened[
            "worth-fixing"
        ]
        assert reopened["worth-fixing"]["defect_evidence_total"] == 3, reopened["worth-fixing"]

        # ---- 9b. THE TIE-BREAK FAILS TOWARD MOTION. A defect recorded in the SAME SECOND as a
        #          repair is unorderable; it must re-open the proposal (one report line) rather
        #          than vanish (the finding). Asserted on a fresh capability so the surrounding
        #          state cannot make it pass for another reason.
        capabilities.save(
            {
                **capabilities.load_declared(ledger),
                "tie-break": {
                    **capabilities._blank_capability("tie-break"),
                    "status": "generated",
                    "matcher": {"field": "task_type", "operator": "in", "value": ["review"]},
                },
            },
            ledger,
        )
        record_usefulness(
            "tie-break",
            "advice:tb00000001",
            useful=False,
            evidence="the matcher does not fit this work",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
            timestamp=base + 100,
        )
        assert record_repair(
            "tie-break",
            fix="fixed the matcher",
            artifact="PR #1000",
            path=ledger,
            timestamp=base + 200,
        )
        cleared = {p["capability_id"] for p in propose_repair(path=ledger)}
        assert "tie-break" not in cleared, cleared
        record_usefulness(
            "tie-break",
            "advice:tb00000002",
            useful=False,
            evidence="still does not fit",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
            timestamp=base + 200,  # EXACTLY the repair's second
        )
        tied = {p["capability_id"] for p in propose_repair(path=ledger)}
        assert "tie-break" in tied, (
            "a defect recorded in the same second as a repair must RE-OPEN the proposal, not "
            "vanish -- a gate must fail toward motion, not silence"
        )

        # ---- 10. A CLAIMED REPAIR WITH NOTHING TO CHECK MUST NOT CLEAR ANYTHING.
        repair_cases: tuple[dict[str, Any], ...] = (
            {"fix": "fixed it", "artifact": ""},
            {"fix": "", "artifact": "PR #1"},
        )
        for kwargs in repair_cases:
            try:
                record_repair("worth-fixing", path=ledger, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f"an unevidenced repair must be refused: {kwargs}")

    # ---- 11. THE TWO PROPERTIES OF A KIND ARE INDEPENDENT, and the pair that proves it is
    #          `precondition_unmet`: NOT demotable, IS repairable. If those ever coincide the
    #          repair channel is redundant with the drain.
    assert decline_kind_demotable("precondition_unmet") is False
    assert decline_kind_repairable("precondition_unmet") is True
    assert decline_kind_repairable("no_landing_zone") is False
    assert decline_kind_repairable("unspecified") is False, "a default must not propose a repair"
    for kind, row in DECLINE_KINDS.items():
        assert "repairable" in row, f"{kind} does not declare `repairable`"
    print(
        "capability_propensity repair selftest: OK (worth-having-and-broken is expressible, a "
        "correct match is never proposed, precondition_unmet finally has an action, the evidence "
        "travels with the proposal, recording the repair drains it while the evidence is still in "
        "window, new evidence re-opens it, and nothing is applied or queued)"
    )


def _selftest_finds() -> None:
    """A FIND IS AN ATTRIBUTION RECORD, and the finder may be a capability OR a surface.

    Every assertion was written by breaking it first:

      * dropping the `artifact` guard -> a claimed find with no artifact is accepted, caught here;
      * dropping the `defect` guard -> likewise;
      * letting a surface find write a usefulness verdict -> the posterior moves for a capability
        nobody credited, caught here;
      * giving the find record an `advice:` ref -> `experiments()` sees it, the candidate count
        moves, and the structural separation becomes conventional; caught here;
      * dropping the idempotency key -> re-recording one find inflates the count, caught here;
      * dropping `defect_found` from the provenance table's corroboration requirement -> covered by
        `_selftest_provenance`, and the interlock below proves the discount still caps volume.
    """
    import tempfile
    from pathlib import Path

    import capability_advisor

    with tempfile.TemporaryDirectory(prefix="finds-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("finder-cap", FIND_CARRIER):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["review"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        real = capability_advisor.SURFACE_BINDINGS.get("f-surf")
        capability_advisor.SURFACE_BINDINGS["f-surf"] = {"finder-cap": "bound for the test"}
        try:
            # ---- 1. A CLAIM WITHOUT AN ARTIFACT IS WORTH NOTHING, and is refused.
            for kwargs in (
                {"defect": "the advisor still offered a suppressed surface", "artifact": ""},
                {"defect": "", "artifact": "PR #123"},
                {"defect": "   ", "artifact": "   "},
            ):
                try:
                    record_find(surface="f-surf", path=ledger, **kwargs)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"an unevidenced find must be refused: {kwargs}")
            # ...and a find with NO finder at all teaches nothing, so it is refused too.
            try:
                record_find(defect="d", artifact="PR #1", path=ledger)
            except ValueError:
                pass
            else:
                raise AssertionError("a find with no finder must be refused")
            # ...and a capability-attributed find without its experiment id belongs to no trial.
            try:
                record_find(defect="d", artifact="PR #1", capability_id="finder-cap", path=ledger)
            except ValueError:
                pass
            else:
                raise AssertionError("a capability find with no experiment id must be refused")

            # ---- 2. A SURFACE-ATTRIBUTED FIND FEEDS BINDING QUALITY AND SCORES NOTHING.
            before = propensity("finder-cap", path=ledger)
            res = record_find(
                defect="the advisor offered capabilities at a suppressed surface",
                # A SYNTHETIC artifact string, deliberately not a dated filename:
                # `capability_admission.commitments()` treats `<date>-<name>.md` in committed code
                # as a CITATION to a decision record and fails the suite when the record does not
                # exist. A fixture must not look like a promise.
                artifact="finding A2 of the audit run, recorded in the audit's own ledger",
                surface="f-surf",
                subject="capability-advisor",
                path=ledger,
            )
            assert res["recorded"] and res["finder_kind"] == "surface", res
            assert res["feeds"] == "binding_quality", res
            assert res["affects_propensity"] is False and not res["usefulness_recorded"], res
            assert res["carrier"] == FIND_CARRIER, res
            after = propensity("finder-cap", path=ledger)
            assert after["propensity"] == before["propensity"], (before, after)
            assert after["evidence_weight"] == before["evidence_weight"], (before, after)
            # ...and it did not touch the CARRIER's numbers either, because `find:` is not `advice:`.
            carrier = usefulness(path=ledger)["rows"][FIND_CARRIER]
            assert carrier["candidates"] == 0 and carrier["resolved"] == 0, carrier
            assert experiments(path=ledger) == [], "a find must be invisible to experiments()"

            # ...and it IS visible where it belongs.
            bq = binding_quality("f-surf", path=ledger)
            assert bq["finds"] == 1, bq
            assert bq["finds_by_finder_kind"] == {"surface": 1}, bq
            assert bq["find_subjects"] == ["capability-advisor"], bq

            # ---- 3. IDEMPOTENT: the same defect from the same finder is ONE find.
            again = record_find(
                defect="The Advisor Offered Capabilities At A Suppressed Surface",
                artifact="same finding, recorded twice",
                surface="f-surf",
                path=ledger,
            )
            assert again["recorded"] is False, again
            assert binding_quality("f-surf", path=ledger)["finds"] == 1, "a replay must not inflate"

            # ---- 4. A CAPABILITY-ATTRIBUTED FIND IS OUTCOME EVIDENCE, at `defect_found`.
            capabilities.heartbeat(
                "finder-cap",
                "match",
                ref="advice:findtrial01",
                path=ledger,
                idempotency_key="m:fc:1",
                metadata={"surface": "f-surf"},
            )
            got = record_find(
                defect="gui/app.py:884 offers an exporter that export.EXPORTERS never registers",
                artifact="issue #77, verified at gui/app.py:884-888",
                surface="f-surf",
                capability_id="finder-cap",
                experiment_id="advice:findtrial01",
                judge="codex",
                path=ledger,
            )
            assert got["recorded"] and got["usefulness_recorded"], got
            assert got["finder_kind"] == "capability" and got["affects_propensity"], got
            scored = propensity("finder-cap", path=ledger)
            assert scored["provenance_mix"] == {"defect_found": 1}, scored
            assert scored["evidence_weight"] == 1.0, scored
            assert scored["outcome_derived_verdicts"] == 1, scored
            # A defect found outweighs a self-report: 0.6667 against 0.5556, both literals.
            assert scored["propensity"] == 0.6667, scored
            assert binding_quality("f-surf", path=ledger)["finds_by_finder_kind"] == {
                "capability": 1,
                "surface": 1,
            }, binding_quality("f-surf", path=ledger)

            # ---- 5. THE INFLATION INTERLOCK. Nine more artifact-backed finds from the SAME arm
            #        must not move the number, because one arm is one observation. This is the
            #        guard that makes `defect_found` safe to weigh at 1.0.
            for i in range(9):
                exp = f"advice:spamfind{i:03d}"
                capabilities.heartbeat(
                    "finder-cap",
                    "match",
                    ref=exp,
                    path=ledger,
                    idempotency_key=f"m:spam{i}",
                    metadata={"surface": "f-surf"},
                )
                record_find(
                    defect=f"another distinct defect {i}",
                    artifact=f"issue #{100 + i}",
                    surface="f-surf",
                    capability_id="finder-cap",
                    experiment_id=exp,
                    judge="codex",
                    path=ledger,
                )
            spammed = propensity("finder-cap", path=ledger)
            assert spammed["evidence_count"] == 10, spammed
            # TEN finds, ONE arm, still ONE observation's worth -- and the number has not budged.
            assert spammed["evidence_weight"] == 1.0, spammed
            assert spammed["propensity"] == 0.6667, spammed
            assert spammed["independent_arms"] == 1, spammed
            # ...while ONE find from a SECOND arm does move it, which is the incentive we want.
            capabilities.heartbeat(
                "finder-cap",
                "match",
                ref="advice:secondarm01",
                path=ledger,
                idempotency_key="m:arm2",
                metadata={"surface": "f-surf"},
            )
            record_find(
                defect="a defect the other reviewer found",
                artifact="issue #200",
                surface="f-surf",
                capability_id="finder-cap",
                experiment_id="advice:secondarm01",
                judge="gemini",
                path=ledger,
            )
            corroborated = propensity("finder-cap", path=ledger)
            assert corroborated["independent_arms"] == 2, corroborated
            assert corroborated["evidence_weight"] == 2.0, corroborated
            assert corroborated["propensity"] > spammed["propensity"], (
                corroborated,
                spammed,
            )
        finally:
            if real is None:
                capability_advisor.SURFACE_BINDINGS.pop("f-surf", None)
            else:
                capability_advisor.SURFACE_BINDINGS["f-surf"] = real

    print(
        "capability_propensity finds selftest: OK (a claimed find with no artifact is refused, a "
        "surface find feeds binding quality and scores nothing, a capability find is defect_found "
        "outcome evidence, replays do not inflate, and ten finds from one arm are still one "
        "observation)"
    )


def _selftest_declines() -> None:
    """A DECLINE IS A THIRD STATE. It must be visible, attributable, and inert on the posterior.

    The last property is the whole difficulty and the reason this function exists. Bucketing a
    decline as a negative outcome would assert "we tried it and it did not help" about a capability
    that never ran — a false statement, and it would corrupt the exact signal declines were added to
    sharpen. Every assertion below was written by breaking it first:

      * routing `record_decline` through an `outcome` heartbeat moves the posterior -> caught here;
      * dropping the blank-reason guard -> caught here;
      * dropping the decline rule from `propose_demotions` -> caught here;
      * dropping the `triggered` guard so a used capability is still demoted -> caught here;
      * dropping the surface key from `experiments()` -> the demotion loses its attribution and is
        caught here.
    """
    import tempfile
    from pathlib import Path

    import capability_advisor

    with tempfile.TemporaryDirectory(prefix="decline-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("helper", "wrong-tool", "used-here"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        exp = "advice:decline000001"
        for cid in ("helper", "wrong-tool"):
            capabilities.heartbeat(
                cid,
                "match",
                ref=exp,
                path=ledger,
                idempotency_key=f"m:{cid}",
                metadata={"skill": "t-dec"},
            )
        record_trigger("helper", exp, path=ledger)
        record_usefulness(
            "helper",
            exp,
            useful=True,
            evidence="found a real defect",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        )

        # ---- 1. THE POSTERIOR MUST NOT MOVE. Measured on a capability that HAS evidence, so a
        # decline leaking in as `not_useful` would visibly drag a real number down rather than
        # merely appearing beside a prior.
        before = propensity("helper", path=ledger)
        # 0.5556, not 0.6667: one UNATTRIBUTED SELF-REPORT weighs 0.25, so (1+0.25)/(2+0.25). The
        # provenance discount landed here on 2026-08-23; the point of the assertion is unchanged
        # (a real, non-prior number the decline below must leave alone).
        assert before["evidence_count"] == 1 and before["propensity"] == 0.5556, before
        assert before["evidence_weight"] == 0.25, before
        for i in range(3):
            e = f"advice:helperdecl{i:03d}"
            assert record_decline(
                "helper",
                e,
                reason=f"wrong phase for this work ({i})",
                surface="t-dec",
                kind="wrong_match",
                path=ledger,
            )
        after = propensity("helper", path=ledger)
        assert after["propensity"] == before["propensity"], (before, after)
        assert after["posterior_mean"] == before["posterior_mean"], (before, after)
        assert after["evidence_count"] == before["evidence_count"], (before, after)
        # ...and it is nonetheless VISIBLE. Inert must not mean invisible: "0.5, no evidence" and
        # "0.5, no evidence, three reasoned rejections" are the two readings the audits could not
        # tell apart.
        assert after["declines"] == 3, after
        assert after["declines_excluded_from_posterior"] is True, after
        assert "never scored" in after["basis"], after["basis"]
        u = usefulness(path=ledger)["rows"]["helper"]
        assert u["declined"] == 3 and u["useful"] == 1 and u["not_useful"] == 0, u
        assert u["usefulness_rate"] == 1.0, u  # not 0.25 — declines are not failures

        # ---- 2. THE THREE STATES PARTITION THE CANDIDATE SET. This is what makes "declined with a
        # reason", "offered and ignored" and "never considered" three different findings.
        d_exp = "advice:decline000002"
        for cid in ("helper", "wrong-tool", "used-here"):
            capabilities.heartbeat(
                cid,
                "match",
                ref=d_exp,
                path=ledger,
                idempotency_key=f"m2:{cid}",
                metadata={"surface": "t-dec"},
            )
        record_trigger("used-here", d_exp, path=ledger)
        record_decline(
            "wrong-tool",
            d_exp,
            reason="this repo has no front end",
            surface="t-dec",
            kind="wrong_match",
            path=ledger,
        )
        trial = next(t for t in experiments(path=ledger) if t["experiment_id"] == d_exp)
        assert trial["declined"] == ["wrong-tool"], trial
        assert trial["decline_reasons"]["wrong-tool"] == "this repo has no front end", trial
        assert trial["decline_kinds"]["wrong-tool"] == "wrong_match", trial
        assert trial["declined_demotable"] == ["wrong-tool"], trial
        assert trial["triggered"] == ["used-here"], trial
        assert trial["not_triggered_silently"] == ["helper"], trial
        assert (
            set(trial["triggered"]) | set(trial["declined"]) | set(trial["not_triggered_silently"])
        ) == set(trial["candidates"]), trial
        assert not (set(trial["triggered"]) & set(trial["declined"])), trial
        assert not (set(trial["declined"]) & set(trial["not_triggered_silently"])), trial
        # A DECLINE RESOLVES NOTHING. `resolved` gates the usefulness population, so a decline that
        # resolved a trial would make the denominator lie in the other direction.
        assert trial["resolved"] is False, trial
        assert trial["useful"] == [] and trial["not_useful"] == [], trial
        # It IS a candidate: a decline is evidence the capability was offered.
        assert "wrong-tool" in trial["candidates"], trial
        # ...and it is NOT an invocation.
        assert "wrong-tool" not in trial["triggered"], trial

        # ---- 3. THE TRIGGER WINS. Declining and then using it is a change of mind, not a rejection.
        both = "advice:decline000003"
        capabilities.heartbeat(
            "helper",
            "match",
            ref=both,
            path=ledger,
            idempotency_key="m3:helper",
            metadata={"surface": "t-dec"},
        )
        record_decline(
            "helper",
            both,
            reason="looked wrong at first",
            surface="t-dec",
            kind="wrong_match",
            path=ledger,
        )
        record_trigger("helper", both, path=ledger)
        t3 = next(t for t in experiments(path=ledger) if t["experiment_id"] == both)
        assert t3["declined"] == [] and t3["triggered"] == ["helper"], t3
        assert t3["decline_reasons"] == {} and t3["decline_kinds"] == {}, t3
        assert t3["declined_demotable"] == [], t3

        # ---- 4. A REASON IS MANDATORY, exactly as an evidenced verdict is.
        for bad in ("", "   ", "\n"):
            try:
                record_decline("helper", "advice:decline000004", reason=bad, path=ledger)
            except ValueError:
                pass
            else:
                raise AssertionError("an unexplained decline must be refused")
        # And the experiment must be a real advisory digest, or declines accrue against no trial.
        try:
            record_decline("helper", "not-an-advice-ref", reason="x", path=ledger)
        except ValueError:
            pass
        else:
            raise AssertionError("a non-advisory experiment id must be refused")
        # AN UNKNOWN KIND IS REFUSED, never coerced. A typo silently becoming `unspecified` would
        # hide the classification the caller believed it had made, and `unspecified` cannot demote —
        # so the coercion would quietly discard the one signal the taxonomy exists to carry.
        try:
            record_decline(
                "helper", "advice:decline0000ff", reason="x", kind="wrong-match", path=ledger
            )
        except ValueError:
            pass
        else:
            raise AssertionError("an unknown decline kind must be refused, not coerced")
        # Omitting the kind is allowed and yields the non-demotable default: no silence, no wrong
        # correction. Failing toward motion, not toward a demotion nobody classified.
        assert record_decline(
            "helper",
            "advice:decline0000aa",
            reason="did not classify it",
            surface="t-dec",
            path=ledger,
        )
        assert decline_kind_demotable(DECLINE_KIND_DEFAULT) is False
        # IDEMPOTENT per (capability, experiment): replaying a backfill cannot inflate the count.
        assert (
            record_decline("wrong-tool", d_exp, reason="repeat", surface="t-dec", path=ledger)
            is False
        )
        assert usefulness(path=ledger)["rows"]["wrong-tool"]["declined"] == 1

        # ---- 5. DEMOTION CONSUMES DECLINES. A binding rejected repeatedly at one surface is the
        # drain the binding table needs, and it must carry the caller's own words.
        real = capability_advisor.SURFACE_BINDINGS.get("t-dec")
        capability_advisor.SURFACE_BINDINGS["t-dec"] = {
            "wrong-tool": "bound for now",
            "used-here": "bound and used",
            "helper": "bound and used",
        }
        try:
            # LITERAL boundary, deliberately not `DEMOTION_MIN_DECLINES - 1`: an assertion written
            # in terms of the constant it guards moves with the constant and can never fail.
            assert DEMOTION_MIN_DECLINES == 2, "boundary cases below assume the floor is 2"
            # One decline so far for wrong-tool -> below the floor, no proposal, and the accumulating
            # count must still be REPORTED. "no proposal" beside "1/2 accumulating" reads completely
            # differently from "no proposal" beside nothing.
            assert propose_demotions("t-dec", path=ledger) == [], "1 decline must not demote"
            counts = surface_decline_counts("t-dec", path=ledger)
            assert counts["declined"]["wrong-tool"] == 1, counts
            record_decline(
                "wrong-tool",
                "advice:decline000005",
                reason="code-mutating tool offered inside a read-only audit",
                surface="t-dec",
                kind="wrong_match",
                path=ledger,
            )
            dem = propose_demotions("t-dec", path=ledger)
            assert [d["capability_id"] for d in dem] == ["wrong-tool"], dem
            assert dem[0]["basis"] == "declined_with_reason", dem[0]
            assert dem[0]["declined"] == 2 and dem[0]["triggered"] == 0, dem[0]
            assert dem[0]["declined_demotable"] == 2, dem[0]
            assert dem[0]["declines_by_kind"] == {"wrong_match": 2}, dem[0]
            assert dem[0]["implied_fixes"] == ["the matcher or the binding"], dem[0]
            assert len(dem[0]["decline_reasons"]) == 2, dem[0]
            assert "read-only audit" in dem[0]["reason"], dem[0]
            # BOTH QUANTITIES on the proposal, so the floor it cleared is legible.
            assert dem[0]["declines_floor"] == DEMOTION_MIN_DECLINES, dem[0]

            # A CAPABILITY THAT IS ACTUALLY USED HERE IS NEVER DEMOTED, however often it is passed
            # over. Enough declines to clear the floor on its own, or removing the trigger guard
            # would leave this below the floor and the assertion could not discriminate.
            for i in range(DEMOTION_MIN_DECLINES + 1):
                record_decline(
                    "used-here",
                    f"advice:usedheredec{i:02d}",
                    reason="not this time",
                    surface="t-dec",
                    kind="wrong_match",
                    path=ledger,
                )
            assert (
                surface_decline_counts("t-dec", path=ledger)["declined"]["used-here"]
                > DEMOTION_MIN_DECLINES
            )
            assert "used-here" not in [
                d["capability_id"] for d in propose_demotions("t-dec", path=ledger)
            ], "a capability triggered at this surface is not a demotion candidate"

            # ATTRIBUTION IS ON THE EVENT. A decline with no surface is still recorded and still
            # readable, and it must not feed a demotion for a surface it never named.
            record_decline(
                "helper",
                "advice:decline000006",
                reason="no surface given",
                kind="wrong_match",
                path=ledger,
            )
            assert usefulness(path=ledger)["rows"]["helper"]["declined"] >= 4
            assert "helper" not in [
                d["capability_id"] for d in propose_demotions("t-dec", path=ledger)
            ], "an unattributed decline must not demote a surface it never named"

            # ---- THE TAXONOMY'S WHOLE POINT: A CORRECT MATCH MUST NOT BE PUNISHED FOR BEING
            # RIGHT. `testgen-lane` matched correctly three times in one read-only audit and was
            # structurally impossible every time (no commit target). Its fix is NOTHING, so however
            # many times it is declined that way it can never clear the demotion floor.
            #
            # DELIBERATELY MANY TIMES OVER THE FLOOR, and asserted against `wrong_match` in the same
            # ledger: if `demotable` were ignored, this capability would demote and the assertion
            # would fire. A count merely equal to the floor could not tell "the kind was honoured"
            # apart from "the floor was not reached".
            right_but_impossible = capabilities._blank_capability("right-but-impossible")
            right_but_impossible["status"] = "generated"
            right_but_impossible["matcher"] = {
                "field": "task_type",
                "operator": "in",
                "value": ["testgen"],
            }
            all_rows = capabilities.load_declared(ledger)
            all_rows["right-but-impossible"] = right_but_impossible
            capabilities.save(all_rows, ledger)
            capability_advisor.SURFACE_BINDINGS["t-dec"]["right-but-impossible"] = "bound, correct"
            for i in range(DEMOTION_MIN_DECLINES * 4):
                record_decline(
                    "right-but-impossible",
                    f"advice:nolanding{i:04d}",
                    reason="correct match, read-only run has no commit target",
                    surface="t-dec",
                    kind="no_landing_zone",
                    path=ledger,
                )
            counts = surface_decline_counts("t-dec", path=ledger)
            # The decline IS recorded and IS visible -- inert must not mean invisible.
            assert counts["declined"]["right-but-impossible"] == DEMOTION_MIN_DECLINES * 4, counts
            assert counts["declined_demotable"].get("right-but-impossible", 0) == 0, counts
            # THE TWO RULES READ DISJOINT POPULATIONS. This probe deliberately exceeds the SILENT
            # floor as well, so it proves the never-triggered rule cannot be reached through
            # declines. Without that, eight honest declines demote a correct match via the other
            # rule -- which is what the first draft of this function actually did.
            assert (
                DEMOTION_MIN_DECLINES * 4 >= DEMOTION_MIN_TRIALS
            ), "this probe must exceed the silent-offer floor too, or it cannot discriminate"
            assert counts["silent"].get("right-but-impossible", 0) == 0, counts
            assert counts["declines_by_kind"]["right-but-impossible"] == {
                "no_landing_zone": DEMOTION_MIN_DECLINES * 4
            }, counts
            assert "right-but-impossible" not in [
                d["capability_id"] for d in propose_demotions("t-dec", path=ledger)
            ], (
                "a CORRECT match blocked by the deliverable's shape must never be demoted — the "
                "fix for no_landing_zone is nothing"
            )
            # ...and it must not reach the posterior either, on any kind.
            prop = propensity("right-but-impossible", path=ledger)
            assert prop["evidence_count"] == 0 and prop["declines"] == DEMOTION_MIN_DECLINES * 4
            assert prop["declines_demotable"] == 0, prop
            assert prop["propensity"] >= EXPLORATION_FLOOR and prop["explorable"] is True, prop

            # THE frontend-verifier STORY, asserted. Declined at two surfaces because its
            # precondition did not hold, then USEFUL at a third on a repo that has the surface. The
            # two negatives must not demote it anywhere -- "evaluate the condition, do not weaken
            # the binding". Exactly at the floor, so a demotable `precondition_unmet` would fire.
            precond = capabilities._blank_capability("surface-gated")
            precond["status"] = "generated"
            precond["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows2 = capabilities.load_declared(ledger)
            rows2["surface-gated"] = precond
            capabilities.save(rows2, ledger)
            capability_advisor.SURFACE_BINDINGS["t-dec"]["surface-gated"] = "bound, conditional"
            for i in range(DEMOTION_MIN_DECLINES):
                record_decline(
                    "surface-gated",
                    f"advice:precond{i:05d}",
                    reason="this repository has no observable surface at all",
                    surface="t-dec",
                    kind="precondition_unmet",
                    path=ledger,
                )
            pc = surface_decline_counts("t-dec", path=ledger)
            assert pc["declined"]["surface-gated"] == DEMOTION_MIN_DECLINES, pc
            assert pc["declined_demotable"].get("surface-gated", 0) == 0, pc
            dem_ids = [d["capability_id"] for d in propose_demotions("t-dec", path=ledger)]
            assert "surface-gated" not in dem_ids, (
                "an unmet PRECONDITION must never demote the binding — the fix is to evaluate the "
                "condition, and two negatives are not a verdict on a binding that fires elsewhere"
            )
            assert DECLINE_KINDS["precondition_unmet"]["demotable"] is False

            # EVERY non-demotable kind behaves the same way, so the guarantee is a property of the
            # table rather than of one branch. Iterating the table also means a NEW kind cannot be
            # added as demotable-by-accident without this failing.
            for kind, spec in sorted(DECLINE_KINDS.items()):
                if spec["demotable"]:
                    continue
                cid = f"nd-{kind}"
                rows_now = capabilities.load_declared(ledger)
                blank = capabilities._blank_capability(cid)
                blank["status"] = "generated"
                blank["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
                rows_now[cid] = blank
                capabilities.save(rows_now, ledger)
                capability_advisor.SURFACE_BINDINGS["t-dec"][cid] = f"bound to probe {kind}"
                for i in range(DEMOTION_MIN_DECLINES * 3):
                    record_decline(
                        cid,
                        f"advice:{kind[:6]}nd{i:04d}",
                        reason=f"declined as {kind}",
                        surface="t-dec",
                        kind=kind,
                        path=ledger,
                    )
                assert cid not in [
                    d["capability_id"] for d in propose_demotions("t-dec", path=ledger)
                ], f"a non-demotable kind ({kind}) demoted a binding"

            # `detect()` prints the drainable quantity for the surface even when nothing fires --
            # and it must find a surface that has EVIDENCE BUT NO TABLE ENTRY, because the surfaces
            # most likely to be over-bound are the ones that only inherit a binding. `t-dec` has a
            # stubbed entry, so assert the derived path on a surface that has none.
            assert "t-dec" in observed_surfaces(path=ledger), sorted(observed_surfaces(path=ledger))
            record_decline(
                "helper",
                "advice:inherited0001",
                reason="inherited-surface probe",
                surface="t-inherited-only",
                kind="wrong_match",
                path=ledger,
            )
            assert "t-inherited-only" in observed_surfaces(
                path=ledger
            ), "a surface with evidence and no table entry must still be enumerated"
            assert "t-inherited-only" not in capability_advisor.SURFACE_BINDINGS
            rep = detect(path=ledger)
            assert "t-dec" in rep["surfaces"], sorted(rep["surfaces"])
            # ASSERT ON WHAT THE CALLER RECEIVES, not on the helper. The first version of this
            # checked `observed_surfaces()` alone and stayed GREEN when `detect()` was reverted to
            # enumerating only declared keys -- a test of the table instead of the answer, which is
            # this project's most-repeated testing mistake.
            assert "t-inherited-only" in rep["surfaces"], (
                "detect() must REPORT a surface that has decline evidence and no table entry; "
                f"it reported {sorted(rep['surfaces'])}"
            )
            assert rep["surfaces"]["t-inherited-only"]["declines"] == {"helper": 1}, rep[
                "surfaces"
            ]["t-inherited-only"]
            assert rep["surfaces"]["t-dec"]["declines"]["wrong-tool"] == 2, rep["surfaces"]["t-dec"]
            assert rep["surfaces"]["t-dec"]["declines_floor"] == DEMOTION_MIN_DECLINES
            assert "wrong-tool" in [d["capability_id"] for d in rep["demotions"]], rep["demotions"]
        finally:
            if real is None:
                capability_advisor.SURFACE_BINDINGS.pop("t-dec", None)
            else:
                capability_advisor.SURFACE_BINDINGS["t-dec"] = real

        # ---- 6. ONE WINDOW. Declines age out with the trials they belong to, so the measuring and
        # the draining window cannot drift apart into permanent debt.
        old = capabilities._now() + (WINDOW_DAYS + 2) * 86400
        assert usefulness(path=ledger, now=old)["rows"]["wrong-tool"]["declined"] == 0
        rep_old = report(path=ledger, now=old)
        assert rep_old["decline_count"] == 0, rep_old
        rep_now = report(path=ledger)
        assert rep_now["decline_count"] >= 6, rep_now
        assert rep_now["capabilities_declined_with_reason"] >= 3, rep_now

    print(
        "capability_propensity decline selftest: OK (a decline is a candidate, never an outcome, "
        "never moves the posterior, partitions the third state, and drains a binding)"
    )


def _selftest_provenance() -> None:
    """A VERDICT IS ONLY AS GOOD AS WHERE IT CAME FROM, and the report must say where.

    Every assertion below was written by breaking it first, and each break was checked to
    DISCRIMINATE — several earlier attempts in this repo asserted a property using the constant that
    guarded it, so the test moved with the bug and could never fail:

      * dropping the provenance weight (treat every verdict as 1.0) -> three correlated
        self-reports reach 0.6667, exactly where one corroborated outcome sits, and the
        `corroborated > solo` assertion fails;
      * dropping the correlated-arm reciprocal -> three same-arm self-reports reach 0.6364,
        exactly where three DIFFERENT arms sit, and both the `n_eff == 0.25` and
        `many_arms > solo` assertions fail;
      * defaulting an unlabelled row to the strongest class -> the legacy-row mix assertion fails;
      * dropping the corroboration requirement -> the refusal assertion fails;
      * keying the correlation on the judge alone -> a corroborated verdict sharing the
        unattributed arm with three self-reports drops from n_eff 1.25 to 0.4375 and the
        `mixed_arm` assertion fails;
      * annotating the entry in `rank()` but not with the mix -> the CALLER assertion fails, which
        is the one that matters: a helper computing the right thing while the caller receives the
        old thing is a bug this repo has shipped and an audit, not its author, caught.

    LITERAL EXPECTED VALUES throughout, deliberately not expressed in terms of
    `VERDICT_PROVENANCE[...]["weight"]`: an assertion written in terms of the table it guards moves
    with the table and can never fail.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="provenance-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("solo", "many-arms", "corroborated", "legacy", "ticky", "mixed-arm"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        def _offer(cap_id: str, exp: str) -> None:
            capabilities.heartbeat(
                cap_id,
                "match",
                ref=exp,
                path=ledger,
                idempotency_key=f"m:{cap_id}:{exp}",
                metadata={"surface": "repo-audit:phase-1"},
            )
            record_trigger(cap_id, exp, path=ledger)

        # THE MEASURED CASE: three verdicts, one model, near-identical instructions. The raw rate
        # says 100%; the honest reading is one observation's worth of one opinion.
        for i in range(3):
            exp = f"advice:solo{i:08d}"
            _offer("solo", exp)
            record_usefulness(
                "solo",
                exp,
                useful=True,
                evidence=f"found defect {i}",
                provenance=PROVENANCE_DEFAULT,
                path=ledger,
            )
        # ...the same three verdicts from three DIFFERENT arms.
        for i, judge in enumerate(("codex", "cursor", "gemini")):
            exp = f"advice:arms{i:08d}"
            _offer("many-arms", exp)
            record_usefulness(
                "many-arms",
                exp,
                useful=True,
                evidence="found a defect",
                judge=judge,
                provenance=PROVENANCE_DEFAULT,
                path=ledger,
            )
        # ...and ONE verdict corroborated by a named outcome.
        _offer("corroborated", "advice:corrob00000")
        record_usefulness(
            "corroborated",
            "advice:corrob00000",
            useful=True,
            evidence="supplied the citation that became the strongest fact in the issue body",
            provenance="outcome_corroborated",
            corroboration="issue #123 filed and the fix merged in PR #124, still green 14d later",
            path=ledger,
        )
        # A PRE-PROVENANCE ROW, written the way the live ledger's 12 verdicts were: an outcome
        # event with `useful` and `evidence` and nothing else.
        capabilities.heartbeat(
            "legacy",
            "outcome",
            ref="advice:legacy000000",
            path=ledger,
            idempotency_key="useful:legacy:advice:legacy000000",
            metadata={"source": "capability_propensity", USEFUL_KEY: True, "evidence": "helped"},
        )
        # A TICK ROW, which stamped `verdict_kind` before this axis existed.
        capabilities.heartbeat(
            "ticky",
            "outcome",
            ref="advice:ticky0000000",
            path=ledger,
            idempotency_key="useful:ticky:advice:ticky0000000",
            metadata={
                "source": "capability_propensity",
                USEFUL_KEY: True,
                "evidence": "finding set changed",
                VERDICT_KIND_KEY: TICK_VERDICT_KIND,
            },
        )
        # THREE self-reports AND one corroborated outcome, all from the unattributed arm. The
        # corroborated one's independence rests on the NAMED OUTCOME, not on who noticed it, so
        # sharing an arm with three opinions must not discount it.
        for i in range(3):
            exp = f"advice:mixed{i:07d}"
            _offer("mixed-arm", exp)
            record_usefulness(
                "mixed-arm",
                exp,
                useful=True,
                evidence=f"helped {i}",
                provenance=PROVENANCE_DEFAULT,
                path=ledger,
            )
        _offer("mixed-arm", "advice:mixedcorrob")
        record_usefulness(
            "mixed-arm",
            "advice:mixedcorrob",
            useful=True,
            evidence="the finding survived adversarial review",
            provenance="outcome_corroborated",
            corroboration="two independent reviewers refuted none of the four findings",
            path=ledger,
        )

        u = usefulness(path=ledger)["rows"]
        # THE RAW RATE IS UNCHANGED and still reported -- the discount must be inspectable, not
        # applied silently in place of the number the events actually say.
        assert u["solo"]["useful"] == 3 and u["solo"]["usefulness_rate"] == 1.0, u["solo"]
        # ...and the discounted evidence is a QUARTER of ONE observation, not three.
        assert u["solo"]["n_eff"] == 0.25, u["solo"]
        assert u["solo"]["independent_arms"] == 1, u["solo"]
        assert u["solo"]["provenance_mix"] == {"self_reported": 3}, u["solo"]
        assert u["solo"]["self_reported_share"] == 1.0, u["solo"]
        assert u["solo"]["outcome_derived"] == 0, u["solo"]
        # THREE ARMS ARE THREE OBSERVATIONS, each still discounted for being an opinion.
        assert u["many-arms"]["n_eff"] == 0.75, u["many-arms"]
        assert u["many-arms"]["independent_arms"] == 3, u["many-arms"]
        # A CORROBORATED OUTCOME IS A WHOLE OBSERVATION.
        assert u["corroborated"]["n_eff"] == 1.0, u["corroborated"]
        assert u["corroborated"]["outcome_derived"] == 1, u["corroborated"]
        assert u["corroborated"]["self_reported_share"] == 0.0, u["corroborated"]
        # AN UNLABELLED ROW IS A SELF-REPORT, because that is what it is.
        assert u["legacy"]["provenance_mix"] == {"self_reported": 1}, u["legacy"]
        assert u["legacy"]["n_eff"] == 0.25, u["legacy"]
        # A TICK ROW IS MACHINE-OBSERVED even though it predates the explicit field.
        assert u["ticky"]["provenance_mix"] == {"machine_observed": 1}, u["ticky"]
        assert u["ticky"]["n_eff"] == 0.6, u["ticky"]
        assert u["ticky"]["outcome_derived"] == 1, u["ticky"]
        # THE PAIR KEY: 3 correlated opinions (0.25 total) + 1 corroborated outcome (1.0).
        assert u["mixed-arm"]["n_eff"] == 1.25, u["mixed-arm"]
        assert u["mixed-arm"]["independent_arms"] == 2, u["mixed-arm"]
        assert u["mixed-arm"]["provenance_mix"] == {
            "outcome_corroborated": 1,
            "self_reported": 3,
        }, u["mixed-arm"]

        p_solo = propensity("solo", path=ledger)
        p_arms = propensity("many-arms", path=ledger)
        p_corr = propensity("corroborated", path=ledger)
        p_mixed = propensity("mixed-arm", path=ledger)
        # 3 correlated self-reports at a RAW 100% must land near the 0.5 prior, not near 1.0.
        assert p_solo["propensity"] == 0.5556, p_solo
        assert p_solo["raw_usefulness_rate"] == 1.0, p_solo
        # ONE corroborated outcome OUTWEIGHS three correlated opinions.
        assert p_corr["propensity"] > p_solo["propensity"], (p_corr, p_solo)
        # THREE INDEPENDENT ARMS outweigh three correlated ones at the same raw rate.
        assert p_arms["propensity"] > p_solo["propensity"], (p_arms, p_solo)
        assert p_mixed["propensity"] > p_arms["propensity"], (p_mixed, p_arms)
        # THE REPORTING REQUIREMENT: the mix travels with the number, always.
        for prop in (p_solo, p_arms, p_corr, p_mixed):
            assert prop["provenance_mix"], prop
            assert prop["evidence_count"] >= 1 and prop["evidence_weight"] > 0, prop
            assert prop["independent_arms"] >= 1, prop
            assert prop["self_reported_share"] is not None, prop
        assert "SELF-REPORTED ONLY" in p_solo["basis"], p_solo["basis"]
        assert "SELF-REPORTED ONLY" not in p_corr["basis"], p_corr["basis"]
        # BOTH quantities, per the runtime rule: the raw count AND the effective weight.
        assert f"{p_solo['evidence_count']}" == "3" and p_solo["evidence_weight"] == 0.25, p_solo
        # THE LATCHED-GATE PROPERTY SURVIVES THE DISCOUNT: discounting compresses towards the
        # prior, never below the floor, so a self-reported-only capability stays samplable.
        assert p_solo["propensity"] >= EXPLORATION_FLOOR and p_solo["explorable"], p_solo

        # THE COUNTERFACTUAL ARM is reported beside the posterior, from the trials themselves.
        capabilities.heartbeat(
            "solo",
            "match",
            ref="advice:controlarm00",
            path=ledger,
            idempotency_key="m:solo:control",
            metadata={"surface": "repo-audit:phase-1"},
        )
        after = propensity("solo", path=ledger)
        assert after["counterfactual_named_not_triggered"] == 1, after
        assert after["counterfactual_silent"] == 1, after
        # ...and it did NOT move the posterior. A counterfactual is context, never a verdict.
        assert after["propensity"] == p_solo["propensity"], (after, p_solo)

        # WHAT A CALLER RECEIVES. `rank()` is the production path; a mix computed and not handed
        # over is a mix nobody reads.
        ranked = rank(
            [{"capability_id": c} for c in ("solo", "corroborated", "many-arms")], path=ledger
        )
        assert [e["capability_id"] for e in ranked][0] == "corroborated", ranked
        for entry in ranked:
            assert entry["usefulness_provenance_mix"], entry
            assert entry["usefulness_independent_arms"] >= 1, entry
            assert entry["usefulness_self_reported_share"] is not None, entry
            assert entry["usefulness_evidence_weight"] > 0, entry
        solo_entry = next(e for e in ranked if e["capability_id"] == "solo")
        assert solo_entry["usefulness_provenance_mix"] == {"self_reported": 3}, solo_entry
        assert solo_entry["usefulness_evidence_weight"] == 0.25, solo_entry
        assert solo_entry["usefulness_outcome_derived"] == 0, solo_entry
        # ORDER ONLY: the discount reorders, it never drops a candidate.
        assert len(ranked) == 3, ranked

        # THE HEADLINE STATES THE MIX. "11 of 12 useful" with no mix is the reading this replaces.
        rep = report(path=ledger)
        assert rep["verdict_count"] == 13, rep["verdict_count"]
        assert rep["verdicts_by_provenance"] == {
            "machine_observed": 1,
            "outcome_corroborated": 2,
            "self_reported": 10,
        }, rep["verdicts_by_provenance"]
        assert rep["verdicts_self_reported"] == 10, rep
        assert rep["verdicts_outcome_derived"] == 3, rep
        assert rep["verdicts_self_reported_share"] == round(10 / 13, 4), rep
        assert rep["capabilities_with_outcome_derived_evidence"] == 3, rep
        assert rep["capabilities_with_multiple_judge_arms"] == 2, rep
        assert "self-reported" in _fmt(rep), _fmt(rep)

        # AN UNKNOWN PROVENANCE IS REFUSED, never coerced to the default: a typo must not silently
        # discard the classification the caller believed it had made.
        try:
            record_usefulness(
                "solo", "advice:badprov0000", useful=True, evidence="x", provenance="great"
            )
        except ValueError:
            pass
        else:
            raise AssertionError("an unknown verdict provenance must be refused")
        # THE STRONGEST CLASSES ARE NOT SELF-CERTIFYING: no named outcome, no claim.
        for claim in ("outcome_corroborated", "defect_found"):
            for blank in ("", "   "):
                try:
                    record_usefulness(
                        "solo",
                        "advice:nocorrob000",
                        useful=True,
                        evidence="it helped",
                        provenance=claim,
                        corroboration=blank,
                        path=ledger,
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"{claim} without a named corroboration must be refused")
        # ...and a DECLINE still cannot reach the posterior, even carrying provenance metadata.
        # There is no code path from a decline to a verdict, and this is where that is proven for
        # the weighted posterior specifically.
        before = propensity("many-arms", path=ledger)
        record_decline(
            "many-arms",
            "advice:declprov000",
            reason="wrong tool for a read-only audit",
            kind="wrong_match",
            surface="repo-audit:phase-1",
            path=ledger,
            metadata={VERDICT_PROVENANCE_KEY: "outcome_corroborated", VERDICT_JUDGE_KEY: "codex"},
        )
        post = propensity("many-arms", path=ledger)
        assert post["propensity"] == before["propensity"], (before, post)
        assert post["evidence_weight"] == before["evidence_weight"], (before, post)
        assert post["provenance_mix"] == before["provenance_mix"], (before, post)
        assert post["declines"] == 1, post

        # ---- SILENCE IS REFUSED, AND THE REFUSAL WRITES NOTHING (2026-08-25).
        #
        # Asserted on BEHAVIOUR rather than on the default's value, because a test that reads back
        # the constant it guards passes with the constant restored. Break the fix by putting
        # `provenance: str = PROVENANCE_DEFAULT` back on `record_usefulness` and this whole block
        # goes red: the call returns True instead of raising, and the verdict count moves.
        quiet = propensity("solo", path=ledger)
        try:
            record_usefulness(
                "solo",
                "advice:noprovenance",
                useful=True,
                evidence="found the defect the PR fixes",
                path=ledger,
            )
        except ValueError as exc:
            refusal = str(exc)
        else:
            raise AssertionError(
                "a usefulness verdict with no stated provenance must be REFUSED, not filed at the "
                "weakest tier — recording appends, so that choice cannot be corrected afterwards"
            )
        # THE REFUSAL IS A REMEDY, not a complaint: every tier it accepts is named, so the caller
        # can retry without reading the source.
        for tier in VERDICT_PROVENANCE:
            assert tier in refusal, (tier, refusal)
        assert "--provenance" in refusal and "--judge" in refusal, refusal
        # NOTHING WAS WRITTEN, which is what keeps the retry the trial's FIRST observation. A
        # refusal that consumed the experiment id would be the deadlock, not the fix.
        assert propensity("solo", path=ledger) == quiet, (quiet, propensity("solo", path=ledger))
        # ...AND THE WEAKEST TIER IS STILL RECORDABLE. Self-assessment is the only signal most
        # capabilities have; the fix makes it a CHOICE, it does not ban it.
        assert record_usefulness(
            "solo",
            "advice:noprovenance",
            useful=True,
            evidence="found the defect the PR fixes",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        ), "an explicit self_reported verdict must still record"
        assert (
            propensity("solo", path=ledger)["evidence_count"] == quiet["evidence_count"] + 1
        ), quiet

        # AND THE SAME REFUSAL REACHES THE SHELL, which is the surface that actually hit this.
        argv = [
            "useful",
            "--capability",
            "solo",
            "--experiment",
            "advice:clinoprov00",
            "--evidence",
            "it found the defect",
            "--ledger",
            str(ledger),
        ]
        before_cli = propensity("solo", path=ledger)
        # The refusal and the success both print; captured so a passing selftest stays readable and
        # an argparse usage block cannot be mistaken for a failure in `verify.py`'s output.
        noise, quiet_out = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stderr(noise), contextlib.redirect_stdout(quiet_out):
                main(argv)
        except SystemExit as exc:
            assert exc.code != 0, exc.code
        else:
            raise AssertionError("the `useful` CLI must refuse a verdict with no --provenance")
        assert "--provenance" in noise.getvalue(), noise.getvalue()
        assert propensity("solo", path=ledger) == before_cli, before_cli
        with contextlib.redirect_stdout(quiet_out):
            assert main(argv + ["--provenance", PROVENANCE_DEFAULT]) == 0
        assert (
            propensity("solo", path=ledger)["evidence_count"] == before_cli["evidence_count"] + 1
        ), before_cli

    print(
        "capability_propensity provenance selftest: OK (self-reports weigh a quarter, one judge "
        "arm totals one observation, a corroborated outcome outweighs three correlated opinions, "
        "unlabelled rows classify as self-reported, the strongest classes are not self-certifying, "
        "an unstated provenance is refused without writing anything, declines stay inert, and the "
        "caller receives the mix)"
    )


def _selftest_late_outcome() -> None:
    """AN OUTCOME THAT ARRIVES AFTER THE VERDICT CAN STILL CORRECT IT — in both directions.

    The gap this closes: the verdict write is idempotent on (capability, experiment), so the tier
    chosen at trigger time was permanent, and `outcome_corroborated` is knowable only AFTER the
    outcome. That made the 1.0 tier reachable only by capabilities whose outcome is immediate, so
    the ranking partly measured how fast an outcome arrives.

    THE TWO ASSERTIONS THAT CARRY THE DESIGN, and either one failing means the feature is worse
    than not shipping it:

      * SYMMETRY (`refutes` lowers). An upgrade-only channel is a monotonic inflation ratchet. The
        refuting arm must move a capability DOWN on the same terms the corroborating arm moves it
        up, or this is a dial for making numbers larger.
      * NO SELF-ASSESSED ATTACHMENT. If an opinion could attach later, the channel becomes "record
        weakly, upgrade once you like the answer". Only non-self-assessed tiers may attach, derived
        from `VERDICT_PROVENANCE` so the offer cannot drift from the acceptance.

    BREAK -> REVERT, each confirmed to discriminate:
      * make the channel upgrade-only (treat `refutes` as `corroborates`) -> the symmetry assertion
        fails: `dud` stays at 0.25 useful instead of moving to not_useful at 0.0;
      * let a self-assessed tier attach (drop the `late_outcome_provenances()` check) -> the gaming
        assertion fails, because the second self-report is accepted;
      * apply with `setdefault` instead of assignment -> the corroborating assertion fails at 0.25,
        which is the whole point of the channel;
      * drop the already-attached guard -> the re-roll assertion fails, and that guard is what stops
        a trial being re-rolled until its number is agreeable;
      * promote an orphan into a verdict -> the orphan assertion fails, and this is the one that
        would silently credit a capability for a trial whose trigger the window cannot see;
      * set `LATE_OUTCOME_EVENT_TYPE = "outcome"` (the unsafe first draft) -> fails immediately at
        "the trigger-time verdict must read at the self-reported weight before any amendment".
        Worth reading carefully, because it fails for a STRUCTURAL reason rather than the
        forward-compatibility one it was written to probe: the amendment branch precedes the verdict
        branch, so identical types make the reader route its own verdicts aside and no verdict exists
        at all. The two types cannot be merged even locally, which is stronger than a convention.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="late-outcome-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("auditor", "dud", "guarded"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            rows[cid] = cap
        capabilities.save(rows, ledger)

        def _trial(cap_id: str, exp: str, *, useful: bool = True) -> None:
            record_trigger(cap_id, exp, path=ledger)
            record_usefulness(
                cap_id,
                exp,
                useful=useful,
                evidence="what it changed at the time",
                provenance="self_reported",
                path=ledger,
            )

        # ---- CORROBORATES: the outcome lands later and the verdict earns its real tier ---------
        aud = "advice:late0000aud0"
        _trial("auditor", aud)
        assert (
            propensity("auditor", path=ledger)["effective_useful"] == 0.25
        ), "the trigger-time verdict must read at the self-reported weight before any amendment"
        res = record_late_outcome(
            "auditor",
            aud,
            direction=LATE_OUTCOME_CORROBORATES,
            evidence="the fix merged on main",
            provenance="outcome_corroborated",
            corroboration="stranske/Fine-Art-Archive#599 merged",
            judge="codex",
            path=ledger,
        )
        assert res["attached"] is True, res
        after = propensity("auditor", path=ledger)
        assert after["effective_useful"] == 1.0, after["effective_useful"]
        assert after["provenance_mix"] == {"outcome_corroborated": 1}, after["provenance_mix"]
        # The named arm travels with the outcome, so a corroborated verdict escapes the
        # `unattributed` correlated arm it was filed under.
        assert after["judge_arms"] == ["codex"], after["judge_arms"]
        aud_row = usefulness(path=ledger)["rows"]["auditor"]
        assert aud_row["useful"] == 1 and aud_row["not_useful"] == 0, aud_row

        # THE ORIGINAL VERDICT IS STILL IN THE LOG. This channel attaches, it does not rewrite, so
        # the record shows both what was believed at trigger time and what the outcome established.
        by_type: dict[str, list[str]] = {}
        for e in _events(capabilities.load_declared(ledger)["auditor"]):
            if _experiment_id(e) != aud:
                continue
            et = str(e.get("type") or e.get("event_type"))
            if et in {"outcome", LATE_OUTCOME_EVENT_TYPE}:
                by_type.setdefault(et, []).append(
                    str((e.get("metadata") or {}).get(VERDICT_PROVENANCE_KEY))
                )
        assert by_type == {
            "outcome": ["self_reported"],
            LATE_OUTCOME_EVENT_TYPE: ["outcome_corroborated"],
        }, by_type

        # FORWARD COMPATIBILITY, and it is not cosmetic. The amendment is a DISTINCT event type
        # because `experiments()` dispatches on match / invocation / outcome; a reader that predates
        # this channel has no branch for `outcome_amendment` and ignores it, so an unsynced exec
        # mirror reads the pre-amendment truth. The first draft tagged it `outcome` and told the two
        # apart by a metadata `source`, which was unsafe in one direction only: an older reader
        # computes `useful if metadata["useful"] is True else not_useful`, and an amendment carries
        # no `useful` key — so every CORROBORATION would have been read as a REFUTATION. This
        # asserts the property directly against a stand-in for the old three-branch dispatch, rather
        # than against git, so it holds on a machine with no `.git` (the mirror has none).
        def _pre_change_reader(events: list[dict]) -> list[str]:
            seen = []
            for e in events:
                et = str(e.get("type") or e.get("event_type"))
                if et == "match":
                    seen.append("candidate")
                elif et == "invocation":
                    seen.append("triggered")
                elif et == "outcome":
                    meta = e.get("metadata") or {}
                    seen.append("useful" if meta.get(USEFUL_KEY) is True else "not_useful")
            return seen

        aud_events = [
            e
            for e in _events(capabilities.load_declared(ledger)["auditor"])
            if _experiment_id(e) == aud
        ]
        assert _pre_change_reader(aud_events) == ["triggered", "useful"], (
            "a pre-change reader must see exactly the original trial — one trigger and one useful "
            "verdict — and must not see the amendment at all"
        )
        assert (
            LATE_OUTCOME_EVENT_TYPE in capabilities.EVENT_FIELDS
        ), "the amendment type must be registered, or `heartbeat` refuses it"
        assert LATE_OUTCOME_EVENT_TYPE != "outcome", "the whole forward-compat property is the type"

        # ---- REFUTES: THE ANTI-RATCHET ARM. Same terms, opposite direction. -------------------
        dud = "advice:late0000dud0"
        _trial("dud", dud)
        assert propensity("dud", path=ledger)["effective_useful"] == 0.25
        res = record_late_outcome(
            "dud",
            dud,
            direction=LATE_OUTCOME_REFUTES,
            evidence="re-measured; the output was identical",
            provenance="machine_observed",
            corroboration="re-run of the same task produced no change",
            path=ledger,
        )
        assert res["attached"] is True and res["verdict_after"] == "not_useful", res
        refuted = propensity("dud", path=ledger)
        assert refuted["effective_useful"] == 0.0, refuted["effective_useful"]
        dud_row = usefulness(path=ledger)["rows"]["dud"]
        assert dud_row["useful"] == 0 and dud_row["not_useful"] == 1, dud_row
        assert dud_row["usefulness_rate"] == 0.0, dud_row["usefulness_rate"]

        # ---- NO SELF-ASSESSED ATTACHMENT: the gaming path stays shut ---------------------------
        assert "self_reported" not in late_outcome_provenances(), late_outcome_provenances()
        gd = "advice:late0000grd0"
        _trial("guarded", gd)
        for bad, why in (
            (
                dict(provenance="self_reported"),
                "a self-assessed tier must not attach",
            ),
            (dict(corroboration=""), "an unnamed outcome must not attach"),
            (dict(direction="helps"), "an unknown direction must not attach"),
            (dict(evidence="  "), "an unevidenced attachment must not attach"),
        ):
            kwargs = dict(
                direction=LATE_OUTCOME_CORROBORATES,
                evidence="ev",
                provenance="outcome_corroborated",
                corroboration="PR #1 merged",
                path=ledger,
            )
            kwargs.update(bad)
            try:
                record_late_outcome("guarded", gd, **kwargs)
                raise AssertionError(why)
            except ValueError:
                pass
        # None of the refusals wrote: the tier is still the trigger-time one.
        assert propensity("guarded", path=ledger)["effective_useful"] == 0.25

        # ---- THE WINDOW EDGE: an orphan is reported, never promoted into a verdict -------------
        orphan = record_late_outcome(
            "guarded",
            "advice:late0000none",
            direction=LATE_OUTCOME_CORROBORATES,
            evidence="an outcome for a trial the window cannot see",
            provenance="outcome_corroborated",
            corroboration="PR #2 merged",
            path=ledger,
        )
        assert orphan["attached"] is False, orphan
        assert "no in-window verdict" in orphan["reason"], orphan["reason"]
        assert orphan["remedy"].strip(), "a refusal with no remedy is the silence this replaces"
        # NOTHING WRITTEN, so the orphan did not invent a trial either.
        assert not [
            t for t in experiments(path=ledger) if t["experiment_id"] == "advice:late0000none"
        ], "an orphan attachment must not create a trial"

        # ---- THE ASSEMBLY'S ORPHAN BRANCH, reached the only way it can be ---------------------
        # `record_late_outcome` refuses to write when no in-window verdict exists, so that guard
        # keeps the reader's orphan branch unreachable through the front door. The branch is still
        # REAL: a verdict can age out of the window between the attachment and a later read, which
        # is exactly the state built here — an aged-out verdict plus a fresh attachment, written
        # through `capabilities.heartbeat` directly because the point is to test the READER.
        # Without this the branch would be defensive code no test ever executes.
        aged = "advice:late0000aged"
        day = 86400
        old_ts = capabilities._now() - (WINDOW_DAYS + 10) * day
        # No `trigger` here: `record_trigger` has no timestamp seam, and the backdated VERDICT is
        # the whole point — an aged-out verdict is what makes the fresh attachment an orphan.
        record_usefulness(
            "guarded",
            aged,
            useful=True,
            evidence="a verdict that will age out",
            provenance="self_reported",
            path=ledger,
            timestamp=old_ts,
        )
        capabilities.heartbeat(
            "guarded",
            LATE_OUTCOME_EVENT_TYPE,
            ref=aged,
            path=ledger,
            idempotency_key=f"late:guarded:{aged}",
            metadata={
                "source": LATE_OUTCOME_SOURCE,
                LATE_OUTCOME_DIRECTION_KEY: LATE_OUTCOME_CORROBORATES,
                VERDICT_PROVENANCE_KEY: "outcome_corroborated",
                VERDICT_CORROBORATION_KEY: "PR #3 merged",
                "evidence": "the outcome arrived after the window closed",
            },
        )
        aged_trial = [t for t in experiments(path=ledger) if t["experiment_id"] == aged]
        assert len(aged_trial) == 1, aged_trial
        orphaned = aged_trial[0]
        assert "guarded" in orphaned["late_outcome_orphans"], orphaned
        assert orphaned["late_outcome_orphans"]["guarded"]["why"] == (
            "no verdict in window to correct"
        ), orphaned["late_outcome_orphans"]
        # AND IT INVENTED NOTHING. Promoting an orphan would credit the capability with an outcome
        # for a trial whose trigger the window can no longer see.
        assert orphaned["useful"] == [] and orphaned["not_useful"] == [], orphaned
        assert "guarded" not in orphaned["late_outcomes"], orphaned["late_outcomes"]
        assert report(path=ledger)["late_outcomes_orphaned"] == 1

        # ---- ONE PER TRIAL: a re-roll is refused with the standing attachment named ------------
        reroll = record_late_outcome(
            "auditor",
            aud,
            direction=LATE_OUTCOME_REFUTES,
            evidence="on reflection I preferred the other answer",
            provenance="machine_observed",
            corroboration="nothing new, just a different mood",
            path=ledger,
        )
        assert reroll["attached"] is False, reroll
        # The idempotency key alone would ALSO return attached=False here — with no reason and no
        # remedy, which is the silent-drop defect wearing this feature's clothes. So the guard's
        # real job is the EXPLANATION, and that is what this asserts.
        assert "reason" in reroll, (
            "a refused re-roll must SAY it was already attached; attached=False with no reason is "
            "the silent drop this channel exists to avoid"
        )
        assert "already attached" in reroll["reason"], reroll["reason"]
        assert reroll["existing"]["direction"] == LATE_OUTCOME_CORROBORATES, reroll["existing"]
        assert (
            propensity("auditor", path=ledger)["effective_useful"] == 1.0
        ), "the re-roll changed it"

        # ---- BOTH COUNTS IN THE REPORT, together, so a ratchet would be visible ---------------
        rep = report(path=ledger)
        assert rep["late_outcomes_corroborating"] == 1, rep["late_outcomes_corroborating"]
        assert rep["late_outcomes_refuting"] == 1, rep["late_outcomes_refuting"]
        assert rep["late_outcomes_orphaned"] == 1, rep["late_outcomes_orphaned"]

    print(
        "capability_propensity late-outcome selftest: OK (a later outcome earns the real tier and "
        "carries its judge, refuting lowers on the same terms so the channel is not a ratchet, a "
        "self-assessed tier cannot attach, an orphan is reported and invents no trial, one "
        "attachment per trial, and the report shows both directions)"
    )


def _selftest_second_verdict_is_dropped_not_appended() -> None:
    """THE FIRST VERDICT ON A TRIAL IS THE ONLY ONE — and the mechanism is idempotency.

    Written because the prose shipped with #123 said the opposite mechanism in five places: that
    `record_usefulness` APPENDS, so a weak verdict "can only be diluted, never upgraded". The
    conclusion was right and the mechanism was wrong, which matters twice over. It implies a partial
    remedy that does not exist — someone holding a `self_reported` verdict and a fresh merged fix
    would record again, get `recorded: false` at exit 0, and reasonably believe the corroboration
    landed. And it points a future fixer at the wrong design problem: guarding against
    double-counting, when the real one is an idempotency key of `(capability, experiment)` with no
    supersede path, so a late-arriving outcome can never strengthen a verdict already filed.

    The control arm is the load-bearing part. Asserting only "the second record changed nothing"
    passes trivially if the write path is broken and NOTHING ever records, so a fresh trial must be
    shown to record normally in the same ledger.

    BREAK -> REVERT, each confirmed to discriminate:
      * drop `idempotency_key` from `record_usefulness`'s heartbeat call -> the second record lands,
        `returned_second is False`, the byte-identical-ledger and the unchanged-provenance
        assertions all fail together;
      * scope the key to the capability alone (`f"useful:{capability_id}"`) -> the CONTROL fails
        with "a verdict on a DIFFERENT trial must still record": the fresh trial's verdict is
        swallowed too, which is the failure the control exists to catch and which every other
        assertion here would have called success. Run this break against THIS function alone —
        in a whole-module run `_selftest_provenance` reaches the same broken key first and fails
        earlier, which masks what the control is demonstrating.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="idempotent-verdict-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        cap = capabilities._blank_capability("dropper")
        cap["status"] = "generated"
        capabilities.save({"dropper": cap}, ledger)

        first, second = "advice:11110000aaaa", "advice:22220000bbbb"
        record_trigger("dropper", first, path=ledger)
        returned_first = record_usefulness(
            "dropper",
            first,
            useful=True,
            evidence="the weak verdict filed at trigger time",
            provenance="self_reported",
            path=ledger,
        )
        assert returned_first is True, "the trial's first verdict must record"
        before = ledger.read_bytes()
        weak = propensity("dropper", path=ledger)
        assert weak["effective_useful"] == 0.25, weak["effective_useful"]

        # The outcome lands later and is STRONGER. This is the case the five prose sites described
        # as dilution; it is a drop.
        returned_second = record_usefulness(
            "dropper",
            first,
            useful=True,
            evidence="the fix merged, corroborating the finding",
            provenance="outcome_corroborated",
            corroboration="PR #999 merged",
            judge="codex",
            path=ledger,
        )
        assert returned_second is False, "a second verdict on the same trial must report the drop"
        assert ledger.read_bytes() == before, "a dropped verdict must not touch the ledger at all"

        # The strongest form: a CONTRADICTING machine-observed verdict is dropped just the same, so
        # a later refutation cannot correct an earlier self-reported success.
        returned_flip = record_usefulness(
            "dropper",
            first,
            useful=False,
            evidence="re-measured; it changed nothing after all",
            provenance="machine_observed",
            path=ledger,
        )
        assert returned_flip is False, "a contradicting verdict on the same trial is dropped too"
        assert ledger.read_bytes() == before, "a dropped refutation must not touch the ledger"

        after = propensity("dropper", path=ledger)
        assert after["provenance_mix"] == {"self_reported": 1}, after["provenance_mix"]
        assert after["effective_useful"] == 0.25, after["effective_useful"]
        assert after["judge_arms"] == ["unattributed"], after["judge_arms"]

        # THE CONTROL: idempotency is scoped to the TRIAL, not the capability. Without this, a
        # write path that recorded nothing at all would pass every assertion above.
        record_trigger("dropper", second, path=ledger)
        returned_fresh = record_usefulness(
            "dropper",
            second,
            useful=True,
            evidence="a different trial, corroborated by its own outcome",
            provenance="outcome_corroborated",
            corroboration="PR #1000 merged",
            judge="codex",
            path=ledger,
        )
        assert returned_fresh is True, "a verdict on a DIFFERENT trial must still record"
        fresh = propensity("dropper", path=ledger)
        assert fresh["provenance_mix"] == {
            "self_reported": 1,
            "outcome_corroborated": 1,
        }, fresh["provenance_mix"]

    print(
        "capability_propensity idempotent-verdict selftest: OK (the first verdict on a trial is "
        "the only one, a stronger or contradicting second is dropped without touching the ledger "
        "and says so, and a different trial still records)"
    )


def _selftest_detection() -> None:
    """The recursive loop: detect a pass-over, propose, promote — and never ratchet.

    The anti-ratchet assertion is the important one. "Should have been chosen" derived from the
    advisor's own naming, fed back into more naming, drives selection pressure up regardless of
    usefulness. That is the trap the learning rules forbid ("loosening a matcher TO MOVE THIS NUMBER
    is forbidden"), so the control arm reports and may never promote.
    """
    import tempfile
    from pathlib import Path

    import capability_advisor

    with tempfile.TemporaryDirectory(prefix="detect-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in (
            "deliberate-break-verifier",
            "adversarial-review",
            "bound-idle",
            # Signal 4's two fixtures: identical except for how many evidenced uses they carry, so
            # the floor is the only thing that can separate them.
            "used-here",
            "used-once",
        ):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        real = capability_advisor.SURFACE_BINDINGS.get("t-surf")
        capability_advisor.SURFACE_BINDINGS["t-surf"] = {"bound-idle": "already bound"}
        try:
            # HAND WORK is counted per record, from the declared signature.
            recs = ["we performed a deliberate break and reverted it"] * 5 + ["nothing here"] * 3
            hw = hand_work("t-surf", recs)
            assert hw.get("deliberate-break-verifier") == 5, hw

            # PROMOTION on hand work above the floor, and not below it.
            props = propose_bindings("t-surf", recs, path=ledger)
            assert [p["capability_id"] for p in props] == ["deliberate-break-verifier"], props
            assert props[0]["action"] == "promote" and props[0]["reason"], props
            # LITERAL boundary, deliberately not `PROMOTION_MIN_HAND_WORK - 1`: an assertion
            # written in terms of the constant it guards moves with the constant and can never fail.
            assert PROMOTION_MIN_HAND_WORK == 3, "boundary cases below assume the floor is 3"
            assert (
                propose_bindings("t-surf", recs[:2], path=ledger) == []
            ), "2 records must not promote"
            assert propose_bindings("t-surf", recs[:3], path=ledger), "3 records must promote"

            # ALREADY BOUND is never re-proposed.
            capability_advisor.SURFACE_BINDINGS["t-surf"]["deliberate-break-verifier"] = "bound now"
            assert propose_bindings("t-surf", recs, path=ledger) == [], "no re-promotion"
            del capability_advisor.SURFACE_BINDINGS["t-surf"]["deliberate-break-verifier"]

            # THE ANTI-RATCHET. A capability named by the advisor and skipped, with NO hand-work
            # evidence, must be REPORTED and must NOT be promoted.
            # ENOUGH of them to clear the promotion floor on their own, or a break that counted the
            # control arm would stay under the floor and the assertion could not discriminate.
            for i in range(PROMOTION_MIN_HAND_WORK + 2):
                capabilities.heartbeat(
                    "adversarial-review",
                    "match",
                    ref=f"advice:ratchet{i:07d}",
                    path=ledger,
                    idempotency_key=f"m:ar{i}",
                    metadata={"skill": "t-surf"},
                )
            ms = missed_selection("t-surf", ["nothing here"] * 9, path=ledger)
            ar = next(r for r in ms["rows"] if r["capability_id"] == "adversarial-review")
            assert ar["named_not_triggered"] >= PROMOTION_MIN_HAND_WORK, ar
            assert ar["hand_work"] == 0, ar
            promoted = [
                p["capability_id"]
                for p in propose_bindings("t-surf", ["nothing here"] * 9, path=ledger)
            ]
            assert (
                "adversarial-review" not in promoted
            ), "the control arm must never promote on its own — that is the ratchet"

            # DEMOTION is the drain: bindings that could only grow end at 43 per surface.
            for i in range(DEMOTION_MIN_TRIALS):
                capabilities.heartbeat(
                    "bound-idle",
                    "match",
                    ref=f"advice:idle{i:08d}",
                    path=ledger,
                    idempotency_key=f"m:idle{i}",
                    metadata={"skill": "t-surf"},
                )
                capabilities.heartbeat(
                    "bound-idle",
                    "outcome",
                    ref=f"advice:idle{i:08d}",
                    path=ledger,
                    idempotency_key=f"o:idle{i}",
                    metadata={
                        "skill": "t-surf",
                        USEFUL_KEY: False,
                        "evidence": "resolved so the trial counts",
                    },
                )
            dem = propose_demotions("t-surf", path=ledger)
            assert [d["capability_id"] for d in dem] == ["bound-idle"], dem
            assert dem[0]["triggered"] == 0 and dem[0]["offered"] >= DEMOTION_MIN_TRIALS, dem[0]

            # SIGNAL 4 (2026-08-25): TRIGGERED HERE, AND IT HELPED, while nothing binds it here.
            # The measured case is `deliberate-break-verifier` at `repo-audit:fix` — reached only
            # through the keyword classifier, so the one consult whose free text missed the
            # vocabulary got a fix arc without it. `used-here` carries ZERO hand-work records, so
            # this cannot pass on the old signal.
            no_records: list[str] = []
            for i in range(PROMOTION_MIN_USEFUL_UNBOUND):
                capabilities.heartbeat(
                    "used-here",
                    "match",
                    ref=f"advice:usedhere{i:05d}",
                    path=ledger,
                    idempotency_key=f"m:uh{i}",
                    metadata={SURFACE_KEY: "t-surf"},
                )
                record_trigger("used-here", f"advice:usedhere{i:05d}", path=ledger)
                record_usefulness(
                    "used-here",
                    f"advice:usedhere{i:05d}",
                    useful=True,
                    evidence=f"produced the red/green transcript the PR body needed ({i})",
                    provenance=PROVENANCE_DEFAULT,
                    path=ledger,
                )
            rows = {
                r["capability_id"]: r
                for r in missed_selection("t-surf", no_records, path=ledger)["rows"]
            }
            assert rows["used-here"]["hand_work"] == 0, rows["used-here"]
            assert rows["used-here"]["useful_here"] == PROMOTION_MIN_USEFUL_UNBOUND, rows[
                "used-here"
            ]
            proposals = {
                p["capability_id"]: p for p in propose_bindings("t-surf", no_records, path=ledger)
            }
            assert "used-here" in proposals, sorted(proposals)
            assert "classifier" in proposals["used-here"]["reason"], proposals["used-here"]
            # ONE evidenced use is an anecdote and must NOT clear the floor. Asserted through a
            # SECOND capability rather than by rewinding the first, so the two populations cannot
            # interfere: `used-once` differs from `used-here` only in the count.
            capabilities.heartbeat(
                "used-once",
                "match",
                ref="advice:usedonce0000",
                path=ledger,
                idempotency_key="m:uo0",
                metadata={SURFACE_KEY: "t-surf"},
            )
            record_trigger("used-once", "advice:usedonce0000", path=ledger)
            record_usefulness(
                "used-once",
                "advice:usedonce0000",
                useful=True,
                evidence="helped once",
                provenance=PROVENANCE_DEFAULT,
                path=ledger,
            )
            assert "used-once" not in {
                p["capability_id"] for p in propose_bindings("t-surf", no_records, path=ledger)
            }, "one evidenced use is an anecdote, not a binding"
            # ...and the count is REPORTED below the floor, so "no proposal" cannot read as
            # "nothing is accumulating".
            below = {
                r["capability_id"]: r
                for r in missed_selection("t-surf", no_records, path=ledger)["rows"]
            }
            assert below["used-once"]["useful_here"] == 1, below["used-once"]
            # A capability already bound here is never re-proposed on this signal either.
            capability_advisor.SURFACE_BINDINGS["t-surf"]["used-here"] = "bound now"
            try:
                assert "used-here" not in {
                    p["capability_id"] for p in propose_bindings("t-surf", no_records, path=ledger)
                }, "binding it is the drain, so the proposal must clear"
            finally:
                del capability_advisor.SURFACE_BINDINGS["t-surf"]["used-here"]

            # A promotion must record WHY.
            for bad in ("", "  "):
                try:
                    record_promotion("adversarial-review", "t-surf", bad, path=ledger)
                except ValueError:
                    pass
                else:
                    raise AssertionError("an unexplained binding promotion must be refused")
        finally:
            if real is None:
                capability_advisor.SURFACE_BINDINGS.pop("t-surf", None)
            else:
                capability_advisor.SURFACE_BINDINGS["t-surf"] = real
    print(
        "capability_propensity detection selftest: OK (hand work promotes above a floor, evidenced "
        "use at an unbound surface promotes and one use does not, the control arm never promotes, "
        "demotion drains, promotions must state why)"
    )


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    # Synthetic ledger throughout: this must assert the MECHANISM on any machine, not this
    # instance's ledger. A reach test asserted against the live ledger earlier today and passed
    # locally while failing CI, which is the same mistake one module over.
    with tempfile.TemporaryDirectory(prefix="propensity-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("helper", "dud", "never-tried"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        exp = "advice:deadbeef1234"
        for cid in ("helper", "dud", "never-tried"):
            capabilities.heartbeat(
                cid,
                "match",
                ref=exp,
                path=ledger,
                idempotency_key=f"m:{cid}",
                metadata={"skill": "repo-audit"},
            )
        record_trigger("helper", exp, path=ledger)
        record_trigger("dud", exp, path=ledger)
        record_usefulness(
            "helper",
            exp,
            useful=True,
            evidence="found 3 real defects",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        )
        record_usefulness(
            "dud",
            exp,
            useful=False,
            evidence="no findings, cost a round",
            provenance=PROVENANCE_DEFAULT,
            path=ledger,
        )

        trials = experiments(path=ledger)
        assert len(trials) == 1, trials
        t = trials[0]
        assert sorted(t["candidates"]) == ["dud", "helper", "never-tried"], t
        assert sorted(t["triggered"]) == ["dud", "helper"], t
        # THE CONTROL ARM must be reported, or this is a tally and not an experiment.
        assert t["not_triggered"] == ["never-tried"], t
        assert t["useful"] == ["helper"] and t["not_useful"] == ["dud"], t
        assert t["skills"] == ["repo-audit"], t

        u = usefulness(path=ledger)["rows"]
        assert u["helper"]["usefulness_rate"] == 1.0, u["helper"]
        assert u["dud"]["usefulness_rate"] == 0.0, u["dud"]
        # Rates must be None, never 0.0, when there is no denominator -- "0% useful" and "never
        # measured" are opposite findings that look identical once one is written as a zero.
        assert u["never-tried"]["usefulness_rate"] is None, u["never-tried"]
        assert u["never-tried"]["trigger_rate"] == 0.0, u["never-tried"]

        # USEFULNESS ORDERS THE RECOMMENDATION. This is the property the whole module exists for.
        p_helper = propensity("helper", path=ledger)["propensity"]
        p_dud = propensity("dud", path=ledger)["propensity"]
        assert p_helper > p_dud, (p_helper, p_dud)

        # THE LATCHED-GATE PROPERTY: no evidence must NOT mean no chance of being tried.
        p_new = propensity("never-tried", path=ledger)
        assert p_new["propensity"] >= EXPLORATION_FLOOR, p_new
        assert p_new["evidence_count"] == 0 and p_new["explorable"] is True, p_new
        assert "can therefore still earn evidence" in p_new["basis"], p_new
        # ...and the useless one must ALSO stay drainable, or one bad trial is a life sentence.
        assert propensity("dud", path=ledger)["propensity"] >= EXPLORATION_FLOOR
        assert propensity("dud", path=ledger)["explorable"] is True

        # Evidence is mandatory for a verdict.
        for bad in ("", "   "):
            try:
                record_usefulness(
                    "helper",
                    exp,
                    useful=True,
                    evidence=bad,
                    provenance=PROVENANCE_DEFAULT,
                    path=ledger,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("an unevidenced usefulness verdict must be refused")
        # An experiment id that is not an advisory digest must be refused, or the experiment
        # population silently fills with rows that belong to no trial.
        try:
            record_trigger("helper", "not-an-advice-ref", path=ledger)
        except ValueError:
            pass
        else:
            raise AssertionError("a non-advisory experiment id must be refused")

        # WINDOW: one constant drives measurement and drain, so a stale trial leaves both together.
        old = capabilities._now() + (WINDOW_DAYS + 2) * 86400
        assert experiments(path=ledger, now=old) == [], "the window must expire trials"
        r_old = report(path=ledger, now=old)
        assert r_old["capabilities_with_evidence"] == 0, r_old
        assert r_old["capability_count"] == 3, "the denominator must survive the window"

        rep = report(path=ledger)
        assert rep["experiment_count"] == 1 and rep["resolved_experiment_count"] == 1, rep
        assert rep["capabilities_with_evidence"] == 2, rep
        assert rep["capabilities_without_evidence"] == 1, rep
        assert [r["capability_id"] for r in rep["ranked"]][0] == "helper", rep["ranked"]

    # THE LANE-FACING CONTRACT. Both lane automations now call this from bash, so the shapes they
    # depend on are pinned here. A loop that can only be closed from Python cannot be closed by an
    # automation, and an experiment id the caller never receives cannot be passed back.
    import capability_advisor

    task = "resolve the unresolved review threads on this PR"
    eid = capability_advisor.experiment_id(task)
    assert eid.startswith(ADVICE_REF_PREFIX), eid
    assert eid == capability_advisor.experiment_id(task), "experiment id must be stable per task"
    assert eid != capability_advisor.experiment_id("something else"), "and task-specific"
    # advise() must HAND BACK the id it recorded under, in both the classified and unclassified
    # branches -- the caller cannot close a loop whose key it was never told.
    got = capability_advisor.advise(task, lane="closer", record=False)
    assert got["experiment_id"] == eid, (got.get("experiment_id"), eid)
    blank = capability_advisor.advise("xyzzy plugh frobnicate", record=False)
    assert blank["experiment_id"] == capability_advisor.experiment_id(
        "xyzzy plugh frobnicate"
    ), blank
    print(
        "capability_propensity.py selftest: OK (natural experiments with a reported control arm, "
        "usefulness orders recommendation, no-evidence stays drainable, window shared)"
    )


def _fmt(rep: dict) -> str:
    lines = [
        f"capability propensity — {rep['window_days']}d window",
        f"  experiments: {rep['experiment_count']} "
        f"({rep['resolved_experiment_count']} resolved)",
        f"  capabilities with usefulness evidence: {rep['capabilities_with_evidence']} "
        f"of {rep['capability_count']}",
        # THE PROVENANCE MIX, never printed apart from the rate it qualifies.
        f"  verdicts: {rep['verdict_count']} — {rep['verdicts_by_provenance'] or '(none)'}; "
        f"{rep['verdicts_outcome_derived']} outcome-derived, {rep['verdicts_self_reported']} "
        f"self-reported"
        + (
            f" ({rep['verdicts_self_reported_share']:.0%})"
            if rep["verdicts_self_reported_share"] is not None
            else ""
        ),
        f"  capabilities with non-self-reported evidence: "
        f"{rep['capabilities_with_outcome_derived_evidence']}; with >1 judge arm: "
        f"{rep['capabilities_with_multiple_judge_arms']}",
        f"  repair proposals: {rep['repair_proposal_count']} "
        f"({rep['repair_proposals_worth_having']} worth having and broken); "
        f"repairs recorded: {rep['repairs_recorded']} — report only, never applied, never queued",
        f"  defect finds: {rep['find_count']} — {rep['finds_by_finder_kind'] or '(none)'} "
        f"(capability-attributed finds also score; surface-attributed ones feed binding quality "
        f"and score nothing)",
        f"  reasoned declines recorded: {rep['decline_count']} across "
        f"{rep['capabilities_declined_with_reason']} capability(ies) — counted, never scored; "
        f"{rep['decline_demotable_count']} attributable to a binding",
        f"  decline kinds: {rep['declines_by_kind'] or '(none)'}",
    ]
    if not rep["capabilities_with_evidence"]:
        lines.append(
            "  NOTE: no resolved outcomes yet — every propensity below is the PRIOR, "
            "not a measurement"
        )
    lines.append("")
    lines.append(
        f"  {'capability':34s} {'prop':>6s} {'cand':>5s} {'trig':>5s} {'use':>4s} {'no':>3s}"
        f" {'decl':>5s}"
    )
    for row in rep["ranked"][:60]:
        lines.append(
            f"  {row['capability_id']:34s} {row['propensity']:6.3f} "
            f"{row['candidates']:5d} {row['triggered']:5d} {row['useful']:4d} "
            f"{row['not_useful']:3d} {row['declined']:5d}"
            + ("  (floored)" if row["floored"] else "")
        )
    if rep["repair_proposals"]:
        lines.append("")
        lines.append(
            "  REPAIR PROPOSALS (report only; the loop's third action — neither promote nor demote)"
        )
        for prop in rep["repair_proposals"][:10]:
            lines.append(
                f"    ~ {prop['capability_id']:34s} "
                f"{'WORTH HAVING' if prop['worth_having'] else 'no useful verdict yet':22s} "
                f"defect evidence {prop['defect_evidence_since_repair']}"
                f"/{prop['defect_evidence_total']}, repairs recorded {prop['repairs_recorded']}"
            )
            if prop["evidence"]:
                lines.append(f"        {prop['evidence'][0][:110]}")
        if len(rep["repair_proposals"]) > 10:
            lines.append(f"    ... and {len(rep['repair_proposals']) - 10} more")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "command",
        nargs="?",
        default="report",
        choices=[
            "report",
            "experiments",
            "trigger",
            "useful",
            "late-outcome",
            "decline",
            "find",
            "binding-quality",
            "repair",
            "record-repair",
            "detect",
            "tick-evidence",
        ],
    )
    # A loop that can only be closed from Python cannot be closed by a lane, which runs bash. These
    # two subcommands are the whole reason the recording edges are reachable from an automation.
    ap.add_argument("--capability", default="", help="capability id, for trigger/useful")
    ap.add_argument("--experiment", default="", help="advice:<digest> from capability_advice")
    ap.add_argument(
        "--evidence", default="", help="what the capability CHANGED (required by useful)"
    )
    ap.add_argument(
        "--not-useful", action="store_true", help="record that triggering it did NOT help"
    )
    # PROVENANCE FROM BASH, because the surfaces that judge are shells. It has NO DEFAULT: an
    # omitted flag used to file the verdict at the weakest tier (0.25) and, because recording
    # appends, that could never be corrected afterwards. `useful` now refuses without it.
    ap.add_argument(
        "--provenance",
        default=PROVENANCE_UNSTATED,
        choices=sorted(VERDICT_PROVENANCE),
        help="useful: WHERE the verdict came from. REQUIRED — there is no default, because the "
        f"old one ({PROVENANCE_DEFAULT}, weight "
        f"{VERDICT_PROVENANCE[PROVENANCE_DEFAULT]['weight']}) silently filed outcome-backed "
        "evidence as an opinion. outcome_corroborated/defect_found weigh 1.0 and require "
        "--corroboration",
    )
    ap.add_argument(
        "--judge",
        default="",
        help="useful: which arm judged (model/backend/surface). Verdicts with no judge are "
        "treated as ONE correlated arm, so naming it is how a capability escapes that discount",
    )
    ap.add_argument(
        "--direction",
        choices=sorted(LATE_OUTCOME_DIRECTIONS),
        default="",
        help=(
            "late-outcome: does the outcome CONFIRM the verdict already recorded, or CONTRADICT "
            "it? REQUIRED — no default, because defaulting would silently pick the direction that "
            "flatters the capability, and an upgrade-only channel is an inflation ratchet"
        ),
    )
    ap.add_argument(
        "--corroboration",
        default="",
        help="useful: the outcome corroborating the verdict (review that confirmed it, issue "
        "filed, fix that landed). Required by outcome_corroborated/defect_found",
    )
    # THE CALLERS ARE BASH. Both lane automations and every skill reach this module from a shell, so
    # a verb that exists only in Python is a verb the surfaces that make these decisions cannot use.
    ap.add_argument(
        "--reason",
        default="",
        help="decline: why this capability was NOT the right tool here (required)",
    )
    ap.add_argument(
        "--surface",
        default="",
        help="decline: the surface that declined (e.g. repo-audit:phase-2). Optional, "
        "but a decline without it cannot feed propose_demotions",
    )
    ap.add_argument(
        "--kind",
        default=DECLINE_KIND_DEFAULT,
        choices=sorted(DECLINE_KINDS),
        help="decline: WHICH KIND of decline. The kinds imply opposite fixes, and only "
        "wrong_match/scope_too_small can ever demote a binding",
    )
    # ISOLATION FOR PROOFS. Wiring this up, I recorded a trial into the LIVE ledger whose evidence
    # described the wiring rather than the capability's review value -- a mislabeled trial, and the
    # system's first data point. A proof belongs on a throwaway ledger; without this flag the only
    # way to demonstrate the path was to pollute the thing being demonstrated.
    ap.add_argument(
        "--apply",
        action="store_true",
        help="detect: write the proposed promotions (default is report-only)",
    )
    ap.add_argument(
        "--ledger",
        default="",
        help="write to this ledger instead of the live one (use for demos and proofs)",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="tick-evidence: consult and classify but record nothing",
    )
    ap.add_argument(
        "--budget-seconds",
        type=int,
        default=TICK_EVIDENCE_BUDGET_S,
        help="tick-evidence: wall-clock ceiling; the tick must never wait longer",
    )
    # A FIND, FROM BASH. The surfaces that find defects are skills and lanes, which run shells, so
    # a verb reachable only from Python is a verb the finders cannot use -- the same reason `decline`
    # has a subcommand.
    ap.add_argument(
        "--defect",
        default="",
        help="find: WHAT was defective (required). Not 'something was found'",
    )
    ap.add_argument(
        "--artifact",
        default="",
        help="find: the PR, issue, file:line or failing test a stranger could check (required). "
        "A claimed find with no artifact is worth nothing",
    )
    ap.add_argument(
        "--subject",
        default="",
        help="find: what the defect was IN (module, capability, doc). Recorded, never scored",
    )
    ap.add_argument(
        "--fix",
        default="",
        help="record-repair: what was CHANGED (required)",
    )
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        _selftest_provenance()
        _selftest_second_verdict_is_dropped_not_appended()
        _selftest_late_outcome()
        _selftest_finds()
        _selftest_repair()
        _selftest_declines()
        _selftest_detection()
        _selftest_tick_evidence()
        return 0
    if args.command == "tick-evidence":
        # ALWAYS EXIT 0 on a handled failure. The tick calls this every hour and drives real
        # dispatch; an advisory evidence step is not permitted to change the tick's control flow.
        rep = tick_evidence_guarded(
            record=not args.dry_run,
            budget_s=args.budget_seconds,
            path=pathlib.Path(args.ledger) if args.ledger else None,
        )
        print(json.dumps(rep, indent=2) if args.json else format_tick_evidence(rep))
        return 0
    if args.command == "find":
        ledger = pathlib.Path(args.ledger) if args.ledger else None
        try:
            res = record_find(
                defect=args.defect,
                artifact=args.artifact,
                surface=args.surface,
                capability_id=args.capability,
                experiment_id=args.experiment,
                subject=args.subject,
                judge=args.judge,
                path=ledger,
            )
        except ValueError as exc:
            ap.error(str(exc))
        res["ledger"] = str(ledger) if ledger else "live"
        print(json.dumps(res, indent=2 if args.json else None))
        return 0
    if args.command == "repair":
        props = propose_repair(window_days=args.window_days)
        _capability_heartbeat("invocation", f"repair-proposals:{len(props)}")
        if args.json:
            print(json.dumps(props, indent=2))
        else:
            worth = sum(1 for p in props if p["worth_having"])
            print(
                f"repair proposals — {len(props)} ({worth} worth having and broken). "
                "REPORT ONLY: never applied, never queued for anyone."
            )
            for prop in props:
                print(
                    f"  ~ {prop['capability_id']:34s} "
                    f"{'WORTH HAVING' if prop['worth_having'] else 'no useful verdict yet':22s} "
                    f"basis={','.join(prop['basis'])} "
                    f"defect={prop['defect_evidence_since_repair']}"
                    f"/{prop['defect_evidence_total']} repairs={prop['repairs_recorded']}"
                )
                for line in prop["evidence"][:2]:
                    print(f"      {line[:118]}")
                for fix in prop["implied_fixes"]:
                    print(f"      fix: {fix[:118]}")
        return 0
    if args.command == "record-repair":
        if not args.capability:
            ap.error("--capability is required")
        ledger = pathlib.Path(args.ledger) if args.ledger else None
        try:
            ok = record_repair(args.capability, fix=args.fix, artifact=args.artifact, path=ledger)
        except ValueError as exc:
            ap.error(str(exc))
        print(
            json.dumps(
                {
                    "recorded": bool(ok),
                    "command": "record-repair",
                    "ledger": str(ledger) if ledger else "live",
                    "capability": args.capability,
                    # SAY WHAT THIS DID. It drains the proposal; it is NOT a verdict and moves no
                    # posterior. New defect evidence after it re-opens the proposal.
                    "drains_repair_proposal": True,
                    "affects_propensity": False,
                }
            )
        )
        return 0
    if args.command == "binding-quality":
        if not args.surface:
            ap.error("--surface is required: binding quality is a property OF a surface")
        rep = binding_quality(args.surface, window_days=args.window_days)
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            print(
                f"binding quality — {rep['surface']} ({rep['window_days']}d)\n"
                f"  bound: {len(rep['bound'])}  offers: {rep['offers']}  "
                f"triggers: {rep['triggers']}  declines: {rep['declines']}\n"
                f"  finds: {rep['finds']} {rep['finds_by_finder_kind'] or ''}  "
                f"subjects: {rep['find_subjects'] or '(none)'}"
            )
        return 0
    if args.command == "detect":
        rep = detect(apply_promotions=args.apply)
        _capability_heartbeat("invocation", f"detect:{len(rep['promotions'])}")
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            print(
                f"capability selection detection — {len(rep['surfaces'])} surface(s) with records"
            )
            for s_, info in rep["surfaces"].items():
                print(
                    f"  {s_:26s} records={info['records']:5d} bound={len(info['bound'])} "
                    f"finds={info['finds']}"
                )
            print(
                f"\n  PROMOTIONS proposed: {len(rep['promotions'])}"
                + (
                    "  (report-only; pass --apply to write them)"
                    if rep["promotions"] and not args.apply
                    else ""
                )
            )
            for pr in rep["promotions"]:
                print(f"    + {pr['surface']} -> {pr['capability_id']}: {pr['reason'][:88]}")
            print(f"  DEMOTIONS proposed: {len(rep['demotions'])} (never auto-applied)")
            for de in rep["demotions"]:
                print(f"    - {de['surface']} -> {de['capability_id']}: {de['reason'][:88]}")
            if rep["applied"]:
                print(f"  APPLIED: {rep['applied']}")
        return 0
    if args.command in {"trigger", "useful", "late-outcome", "decline"}:
        if not args.capability or not args.experiment:
            ap.error("--capability and --experiment are required")
        ledger = pathlib.Path(args.ledger) if args.ledger else None
        if args.command == "late-outcome":
            if not args.direction:
                ap.error(
                    "--direction is required: "
                    + ", ".join(
                        f"{name} ({row['means']})"
                        for name, row in sorted(LATE_OUTCOME_DIRECTIONS.items())
                    )
                    + ". There is no default — defaulting would pick the direction that flatters "
                    "the capability, and a channel that only ever raises usefulness is an inflation "
                    "ratchet, not a measurement"
                )
            try:
                res = record_late_outcome(
                    args.capability,
                    args.experiment,
                    direction=args.direction,
                    evidence=args.evidence,
                    provenance=args.provenance,
                    corroboration=args.corroboration,
                    judge=args.judge,
                    path=ledger,
                    window_days=args.window_days,
                )
            except ValueError as exc:
                ap.error(str(exc))
            res["command"] = "late-outcome"
            res["ledger"] = str(ledger) if ledger else "live"
            print(json.dumps(res))
            # NON-ZERO WHEN IT DID NOT ATTACH. The older `useful` verb returns 0 on its idempotent
            # drop and keeps doing so — it has two live automation callers whose retries would break
            # — but this contract is new, so it gets the honest exit from the start. `recorded: false`
            # at exit 0 is precisely how the drop stayed invisible for two days (#127).
            return 0 if res.get("attached") else 3
        if args.command == "decline":
            if not args.reason.strip():
                ap.error(
                    "--reason is required: an unexplained decline is indistinguishable from "
                    "inattention, which is the state this verb exists to replace"
                )
            ok = record_decline(
                args.capability,
                args.experiment,
                reason=args.reason,
                surface=args.surface,
                kind=args.kind,
                path=ledger,
            )
            print(
                json.dumps(
                    {
                        "recorded": bool(ok),
                        "command": "decline",
                        "ledger": str(ledger) if ledger else "live",
                        "capability": args.capability,
                        "experiment": args.experiment,
                        "surface": args.surface or None,
                        "kind": args.kind,
                        "kind_implies_fix": DECLINE_KINDS[args.kind]["fix"],
                        "can_demote_the_binding": decline_kind_demotable(args.kind),
                        # SAY WHAT THIS DID NOT DO. A decline is not a verdict, and a caller
                        # that thinks it scored the capability has been misled.
                        "affects_propensity": False,
                        "attributable_to_surface": bool(args.surface),
                    }
                )
            )
            return 0
        if args.command == "trigger":
            ok = record_trigger(args.capability, args.experiment, path=ledger)
        else:
            if not args.evidence.strip():
                ap.error(
                    "--evidence is required: an unevidenced verdict is an opinion, and "
                    "'it ran' is not usefulness"
                )
            # REFUSE, DON'T ASSUME. Same text `record_usefulness` raises, rendered as an argparse
            # error so the shell caller that hit this gets the tiers, the remedy and the reason it
            # cannot be fixed after the fact — instead of a traceback or, as before, a silent 0.25.
            try:
                ok = record_usefulness(
                    args.capability,
                    args.experiment,
                    useful=not args.not_useful,
                    evidence=args.evidence,
                    provenance=args.provenance,
                    judge=args.judge,
                    corroboration=args.corroboration,
                    path=ledger,
                )
            except ValueError as exc:
                ap.error(str(exc))
        out = {
            "recorded": bool(ok),
            "command": args.command,
            "ledger": str(ledger) if ledger else "live",
            "capability": args.capability,
            "experiment": args.experiment,
        }
        if args.command == "useful":
            # SAY WHAT THIS VERDICT IS WORTH, at the moment it is recorded. A caller that thinks
            # it just added a full observation has been misled -- and a self-report from an
            # unnamed arm is worth a quarter of one, shared with every other verdict from that arm.
            out.update(
                {
                    "provenance": args.provenance,
                    "provenance_weight": provenance_weight(args.provenance),
                    "self_assessed": provenance_self_assessed(args.provenance),
                    "judge": args.judge or UNATTRIBUTED_JUDGE,
                    "judge_attributed": bool(args.judge.strip()),
                    "correlated_with_same_arm_verdicts": not args.judge.strip(),
                }
            )
            if not ok:
                # THE DROP IS NOW RECOVERABLE, so say how. Exit stays 0 for the two live automation
                # callers, but a caller reading the JSON is no longer told only that nothing
                # happened — before the late-outcome channel existed there was genuinely nothing to
                # suggest here, which is why this field could not have been written earlier.
                out["remedy"] = (
                    "this trial already holds a verdict for this capability and the write is "
                    "idempotent on (capability, experiment), so nothing changed. If the new "
                    "evidence is an OUTCOME rather than another opinion, attach it: `late-outcome "
                    "--direction corroborates|refutes --provenance "
                    f"{'|'.join(late_outcome_provenances())} --corroboration <the outcome>`"
                )
        print(json.dumps(out))
        return 0
    if args.command == "experiments":
        data = experiments(window_days=args.window_days)
        print(
            json.dumps(data, indent=2)
            if args.json
            else "\n".join(
                f"{t['experiment_id']}  candidates={len(t['candidates'])} "
                f"triggered={len(t['triggered'])} not_triggered={len(t['not_triggered'])} "
                f"useful={t['useful']} skills={t['skills']}"
                for t in data
            )
            + "\n"
        )
        return 0
    rep = report(window_days=args.window_days)
    _capability_heartbeat("invocation", f"cli:report:{rep['experiment_count']}")
    print(json.dumps(rep, indent=2) if args.json else _fmt(rep), end="")
    return 0


# ---------------------------------------------------------------------------
# DETECTION — was a capability passed over when it should have been chosen?
#
# DEDUP FINDING (2026-08-23, before writing this). `capability_matcher_proposals.py` ALREADY answers
# the adjacent question — "should work have been ROUTED here", scored against the Brain's run
# history — and it reports 6 capabilities across 379 runs of matching work that were never invoked
# (deliberate-break-verifier 111, frontend-verifier 131, testgen-lane 86, codemod-campaign 38,
# cross-repo-coordination 7, epic-decomposition 6). It is itself a built-and-forgotten feature: it
# runs, it produces that report, and it has NO caller and NO ledger registration. This module
# CONSUMES it rather than recomputing, because a second implementation of the same evidence is the
# parallel-inventory defect.
#
# WHAT IT CANNOT TELL US, and why signal 1 exists. `runs` has no surface column — `runs.source` holds
# only keepalive / orchestrator_local / orchestrator_remote / None. So run history says a capability
# is under-used OVERALL; it cannot say WHICH SURFACE passed it over. Attribution has to come from the
# surface's own records, so a binding proposal is never derived from run history alone.
#
# THREE SIGNALS, and only two may promote:
#   1. HAND-WORK (external, surface-attributed) — the surface's own records show it doing the
#      capability's work manually. This is the strongest signal and the measured one: the opener
#      performed deliberate-break-verifier's contract in 271 of 2,445 rounds while never invoking it.
#   2. CONTROL ARM (internal, surface-attributed) — the advisor named it, the surface did not trigger
#      it. MAY NOT PROMOTE ALONE. "Should have been chosen" derived from the advisor's own naming,
#      feeding back into more naming, is a ratchet: it would drive selection pressure up regardless
#      of usefulness, which is the trap the learning rules forbid.
#   3. UNDER-USE (external, unattributed) — the matcher-proposals evidence above. Corroborates a
#      promotion; cannot locate it.
#
# So: promote on 1, corroborated by 3. Report 2 as context, never as cause.
#
# THE DEMOTION PATH IS THE DRAIN. Bindings that could only grow would end at every surface holding
# 43 capabilities — the exact condition binding exists to prevent. A capability bound to a surface
# that never triggers it across DEMOTION_MIN_TRIALS resolved experiments is proposed for removal.
# ---------------------------------------------------------------------------

# What a surface's own record looks like when it did the capability's work BY HAND. Declared, not
# inferred: the mapping from a capability to the trace it leaves is a judgement that belongs in one
# reviewable place. Patterns are deliberately narrow — a loose pattern manufactures false positives,
# and a false promotion costs more than a missed one because it widens the very set we are narrowing.
HAND_WORK_SIGNATURES: dict[str, str] = {
    "deliberate-break-verifier": r"deliberate(ly)?[ -](break|widen|forc)|deliberate break",
    "adversarial-review": r"adversarial(ly)? (verif|review|critic)",
    "runtime-ac-checks": r"acceptance criteri\w* (unmet|not met|unverified)|stale checkbox",
    "partitioned-review": r"partition(ed)? (the )?review|split the review",
    "offload": r"offload(ed)? (to|the)|hand(ed)? off the read",
}
PROMOTION_MIN_HAND_WORK = 3  # below this, one anecdote could widen a bound set
# THE SIGNAL THIS LOOP COULD NOT SEE (2026-08-25): the surface USED the capability, it HELPED, and
# the capability is not bound there. Stronger than hand-work by construction — hand-work is a regex
# guessing from a surface's prose that it did the work manually, while this is the capability itself
# running, at that surface, with an evidenced `useful` verdict attached. It is also NOT the control
# arm: promotion from "named and not triggered" is the ratchet the rules forbid, because the
# advisor's own naming would feed back into more naming. "Named, TRIGGERED, and it helped" is an
# outcome, and outcomes are what the learning rules permit.
#
# WHY IT MATTERS EVEN THOUGH THE CAPABILITY WAS ALREADY REACHED. It was reached by the KEYWORD
# CLASSIFIER, which depends on the caller already using the capability's vocabulary. Measured:
# `deliberate-break-verifier` was triggered and scored useful six times at `repo-audit:fix` across
# three runs, and the one consult whose free text said "verbatim console record / red / green"
# instead of "pytest" was offered a fix arc without it — then used it successfully on that very
# issue. Binding is the layer that does not depend on classification, so a capability repeatedly
# useful at a surface belongs in that surface's declared set. Widening `TASK_SIGNALS` instead is
# explicitly forbidden: it corrupts the learned associations.
#
# LATCHED-GATE ANSWERS (it is a threshold, so it owes all three):
#   1. WHAT DECREMENTS IT? Binding the capability at that surface — `already_bound` drops the row
#      outright. That is the very action the proposal asks for, not "someone notices".
#   2. CAN THE DRAIN RUN WHILE IT IS CLOSED? Yes, unconditionally. The proposal is report-only: it
#      never withholds the capability, never lowers its propensity and never blocks a consult, so
#      the surface keeps triggering it — which is how the count rose in the first place.
#   3. SAME WINDOW BOTH WAYS? Yes: `WINDOW_DAYS`, the one constant `experiments()` already uses for
#      both the trials counted here and the declines counted opposite. And `missed_selection` reports
#      the count even BELOW the floor, so "no proposal" can never read as "nothing is accumulating".
PROMOTION_MIN_USEFUL_UNBOUND = 2
DEMOTION_MIN_TRIALS = 8  # resolved experiments a binding gets before non-use counts
# A REASONED DECLINE IS MUCH STRONGER EVIDENCE THAN SILENT NON-USE, so its floor is much lower. A
# phase surface is consulted at most ONCE per run, so two declines are two independent runs by
# construction, where `DEMOTION_MIN_TRIALS` trials can all come from one high-volume lane.
#
# LATCHED-GATE ANSWERS (it is a threshold, so it needs all three in writing):
#   1. WHAT DECREMENTS IT? A single TRIGGER at that surface removes the proposal outright, and the
#      binding keeps offering the capability while the count sits below the floor. So the gate fails
#      toward motion: the capability stays selectable either way.
#   2. CAN THE DRAIN RUN WHILE CLOSED? Yes. Demotion is itself the drain on the binding table, and
#      recording a decline requires nothing the proposal forbids -- the capability is still offered
#      on every consult, so it can always be either declined again or used.
#   3. SAME WINDOW BOTH WAYS? Yes: `WINDOW_DAYS`, the one constant, drives the decline count and the
#      trial count alike. And `detect()` reports each bound capability's decline count even when it
#      is BELOW the floor, so "no proposal" can never read as "nothing is accumulating".
DEMOTION_MIN_DECLINES = 2


# Where a surface's own records live. INSTANCE paths, so they are resolved at runtime and
# overridable — the tool must not hardcode one machine's layout. A surface with no resolvable
# records simply yields no hand-work evidence, which is the safe direction: no evidence, no promotion.
SURFACE_RECORD_GLOBS: dict[str, str] = {
    "opener-lane": "~/.codex/automations/pd-workloop-resume/memory-*.md",
    "closer-lane": "~/.codex/automations/imi-merge-verify-closer/memory-*.md",
}
RECORD_SPLIT = r"^## \d{4}-\d{2}-\d{2}T[\d:]+Z?\s*$"


def surface_records(surface: str) -> list[str]:
    """This surface's own records, split into rounds. Empty when the layout is not present here."""
    import os
    import re
    from pathlib import Path

    pattern = os.environ.get(
        f"ORCH_RECORDS_{surface.replace('-', '_').upper()}"
    ) or SURFACE_RECORD_GLOBS.get(surface)
    if not pattern:
        return []
    root = Path(pattern).expanduser()
    text = ""
    for f in sorted(root.parent.glob(root.name)):
        try:
            text += f.read_text(errors="ignore")
        except OSError:
            continue
    if not text:
        return []
    return [r for r in re.split(RECORD_SPLIT, text, flags=re.MULTILINE) if r.strip()]


def observed_surfaces(*, path=None, window_days: int = WINDOW_DAYS) -> set[str]:
    """Surfaces that actually consulted, read from the trials themselves.

    Derived, never a second list: a declared table of surfaces would drift from the surfaces that
    exist, and the ones that only INHERIT a binding would never appear in it at all.
    """
    return {
        surface
        for trial in experiments(path=path, window_days=window_days)
        for surface in (trial.get("skills") or [])
    }


def detect(*, path=None, apply_promotions: bool = False) -> dict:
    """Run detection across every surface whose records are resolvable here.

    REPORT-ONLY BY DEFAULT, matching how `feature_scan` is wired into the tick: `--apply` exists and
    is deliberately not passed by the cadence step. A promotion widens a bound set, and widening it
    without a diff anyone saw is how a narrowing mechanism quietly stops narrowing.
    """
    import capability_advisor

    surfaces: dict[str, dict] = {}
    promotions: list[dict] = []
    demotions: list[dict] = []
    applied: list[dict] = []
    all_finds = finds(path=path)
    # EVERY SURFACE THAT HAS EITHER A DECLARATION OR EVIDENCE. Enumerating only the declared keys
    # missed the inherited ones entirely: `repo-audit:dimension-1` has no table entry of its own --
    # it inherits `offload` surface-wide -- so three independent audits declining `offload` there
    # were recorded and never read. A drain that cannot see a surface cannot drain it, and the
    # surfaces most likely to be over-bound are exactly the ones that only inherit.
    for surface in sorted(
        set(SURFACE_RECORD_GLOBS)
        | set(capability_advisor.SURFACE_BINDINGS)
        | observed_surfaces(path=path)
    ):
        recs = surface_records(surface)
        proms = propose_bindings(surface, recs, path=path) if recs else []
        dems = propose_demotions(surface, path=path)
        counts = surface_decline_counts(surface, path=path)
        # BINDING QUALITY, reported per surface. A surface that triggers nothing while its consults
        # keep surfacing defects is not an idle surface, and the two were indistinguishable before
        # finds existed. Read here, never acted on: a number about a surface must not become
        # selection pressure on a capability.
        here = [f for f in all_finds if f["surface"] == surface or f["finder"] == surface]
        if recs or proms or dems or counts["declined"] or here:
            surfaces[surface] = {
                "records": len(recs),
                "bound": sorted(capability_advisor.binding_for(surface, path=path)),
                "finds": len(here),
                "finds_by_finder_kind": {
                    k: sum(1 for f in here if f["finder_kind"] == k)
                    for k in sorted({f["finder_kind"] for f in here})
                },
                "find_subjects": sorted({f["subject"] for f in here if f["subject"]}),
                # THE DRAINABLE QUANTITY, printed whether or not the floor was reached. "0 proposals"
                # beside "3 declines accumulating, floor 2" reads completely differently from "0
                # proposals" beside nothing at all, and only one of those is a healthy silence.
                "declines": dict(sorted(counts["declined"].items())),
                "declines_demotable": dict(sorted(counts["declined_demotable"].items())),
                "declines_by_kind": {
                    c: dict(sorted(k.items()))
                    for c, k in sorted(counts["declines_by_kind"].items())
                },
                "declines_floor": DEMOTION_MIN_DECLINES,
            }
        promotions.extend(proms)
        demotions.extend(dems)
    finds_count = len(all_finds)
    finds_by_finder_kind = {
        k: sum(1 for f in all_finds if f["finder_kind"] == k)
        for k in sorted({f["finder_kind"] for f in all_finds})
    }
    if apply_promotions:
        for prom in promotions:
            # Respect the ceiling the binding exists to enforce; a promotion that pushes a context
            # past the safe zone defeats the purpose of promoting into it.
            current = capability_advisor.binding_for(prom["surface"], path=path)
            if len(current) >= 10:
                prom["skipped"] = "surface already at the safe-zone ceiling"
                continue
            if record_promotion(prom["capability_id"], prom["surface"], prom["reason"], path=path):
                applied.append({"capability_id": prom["capability_id"], "surface": prom["surface"]})
    return {
        "surfaces": surfaces,
        "promotions": promotions,
        "demotions": demotions,
        "applied": applied,
        "finds": finds_count,
        "finds_by_finder_kind": finds_by_finder_kind,
    }


def _under_use() -> dict[str, int]:
    """Capabilities the Brain says work existed for and which never ran. Consumed, not recomputed."""
    try:
        import capability_matcher_proposals as proposals

        rep = proposals.evaluate()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, int] = {}
    for row in rep.get("rows") or []:
        if row.get("should_have_been_used"):
            out[str(row["capability_id"])] = int(row.get("historical_matches") or 0)
    return out


def hand_work(surface: str, records: list, *, capability_ids=None) -> dict[str, int]:
    """How many of this surface's own records show it doing a capability's work by hand.

    `records` are text blobs (a lane's memory rounds, an audit's documents). Passed in rather than
    discovered, because their locations are instance evidence and this module is tool.
    """
    import re

    wanted = set(capability_ids or HAND_WORK_SIGNATURES)
    out: dict[str, int] = {}
    for cap_id in sorted(wanted & set(HAND_WORK_SIGNATURES)):
        rx = re.compile(HAND_WORK_SIGNATURES[cap_id], re.IGNORECASE)
        n = sum(1 for text in records if rx.search(str(text)))
        if n:
            out[cap_id] = n
    return out


def missed_selection(
    surface: str, records: list, *, path=None, window_days: int = WINDOW_DAYS
) -> dict:
    """Evidence that this surface passed over a capability it should have chosen."""
    import capability_advisor

    bound = set(capability_advisor.binding_for(surface, path=path))
    hands = hand_work(surface, records)
    under = _under_use()

    # Signal 2, reported and never promoting: candidates this surface was offered and skipped.
    control: dict[str, int] = {}
    # Signal 4, and it MAY promote: the surface triggered it here and recorded that it HELPED. An
    # outcome, not the advisor's own advocacy — see PROMOTION_MIN_USEFUL_UNBOUND for why this is not
    # the ratchet, and why "already reached by the classifier" is not the same as "bound".
    used_usefully: dict[str, int] = {}
    for trial in experiments(path=path, window_days=window_days):
        if surface not in (trial.get("skills") or []):
            continue
        for cap_id in trial.get("not_triggered") or []:
            control[cap_id] = control.get(cap_id, 0) + 1
        for cap_id in trial.get("useful") or []:
            used_usefully[cap_id] = used_usefully.get(cap_id, 0) + 1

    rows = []
    for cap_id in sorted(set(hands) | set(control) | set(used_usefully)):
        rows.append(
            {
                "capability_id": cap_id,
                "surface": surface,
                "hand_work": hands.get(cap_id, 0),
                "named_not_triggered": control.get(cap_id, 0),
                # REPORTED EVEN BELOW THE FLOOR, so "no proposal" cannot read as "nothing is
                # accumulating" — the runtime rule every threshold here owes.
                "useful_here": used_usefully.get(cap_id, 0),
                "under_use_runs": under.get(cap_id, 0),
                "already_bound": cap_id in bound,
            }
        )
    return {"surface": surface, "record_count": len(records), "bound": sorted(bound), "rows": rows}


def propose_bindings(surface: str, records: list, *, path=None) -> list[dict]:
    """Promotions warranted for this surface. External signal only — never the control arm alone.

    TWO RULES NOW, and they read the same population from opposite sides: hand-work says the surface
    did the capability's job WITHOUT it, evidenced usefulness says the surface did the job WITH it
    while nothing bound it there. Either promotes; the control arm still never does.
    """
    out = []
    for row in missed_selection(surface, records, path=path)["rows"]:
        if row["already_bound"]:
            continue
        by_hand = row["hand_work"] >= PROMOTION_MIN_HAND_WORK
        by_use = row["useful_here"] >= PROMOTION_MIN_USEFUL_UNBOUND
        if not (by_hand or by_use):
            continue
        why: list[str] = []
        if by_use:
            why.append(
                f"{row['useful_here']} evidenced `useful` verdict(s) at this surface while nothing "
                f"binds the capability here, so it reaches a caller only when the keyword "
                f"classifier happens to match — the binding is the layer that does not"
            )
        if by_hand:
            why.append(
                f"{row['hand_work']} of {len(records)} records show this surface doing the "
                f"capability's work by hand"
            )
        if row["under_use_runs"]:
            why.append(
                f"the Brain shows {row['under_use_runs']} runs of matching work that never "
                f"invoked it"
            )
        out.append({**row, "action": "promote", "reason": "; ".join(why)})
    # Evidenced use outranks a prose signature, which is why it sorts first.
    return sorted(out, key=lambda r: (-r["useful_here"], -r["hand_work"]))


def surface_decline_counts(surface: str, *, path=None, window_days: int = WINDOW_DAYS) -> dict:
    """Per capability at this surface: offered / triggered / declined, plus the stated reasons.

    Split out of `propose_demotions` so the DRAINABLE quantity is reportable on its own. A threshold
    that only speaks when it fires cannot be told apart from one that will never fire.
    """
    seen: dict[str, int] = {}
    used: dict[str, int] = {}
    silent: dict[str, int] = {}
    declined: dict[str, int] = {}
    demotable: dict[str, int] = {}
    kinds: dict[str, dict[str, int]] = {}
    reasons: dict[str, list[str]] = {}
    for trial in experiments(path=path, window_days=window_days):
        if surface not in (trial.get("skills") or []):
            continue
        for cap_id in trial.get("candidates") or []:
            seen[cap_id] = seen.get(cap_id, 0) + 1
        for cap_id in trial.get("triggered") or []:
            used[cap_id] = used.get(cap_id, 0) + 1
        # SILENT is the population the never-triggered rule is ABOUT: offered, and nothing said. It
        # must exclude declines, or a capability declined honestly enough times trips a rule meant
        # for capabilities nobody spoke about -- which is how a `no_landing_zone` decline would have
        # demoted a correct match through the back door. (It did, in the first draft of this.)
        for cap_id in trial.get("not_triggered_silently") or []:
            silent[cap_id] = silent.get(cap_id, 0) + 1
        for cap_id in trial.get("declined") or []:
            declined[cap_id] = declined.get(cap_id, 0) + 1
            kind = (trial.get("decline_kinds") or {}).get(cap_id, DECLINE_KIND_DEFAULT)
            bucket = kinds.setdefault(cap_id, {})
            bucket[kind] = bucket.get(kind, 0) + 1
            why = (trial.get("decline_reasons") or {}).get(cap_id)
            if why and why not in reasons.setdefault(cap_id, []):
                reasons[cap_id].append(why)
        for cap_id in trial.get("declined_demotable") or []:
            demotable[cap_id] = demotable.get(cap_id, 0) + 1
    return {
        "surface": surface,
        "offered": seen,
        "triggered": used,
        "silent": silent,
        "declined": declined,
        # BOTH QUANTITIES. `declined` is how often it was turned down; `demotable` is how much of
        # that is a statement about the BINDING. Reporting only the first is what would license
        # unbinding `testgen-lane` for matching correctly three times.
        "declined_demotable": demotable,
        "declines_by_kind": kinds,
        "decline_reasons": reasons,
    }


def propose_demotions(surface: str, *, path=None, window_days: int = WINDOW_DAYS) -> list[dict]:
    """Bound capabilities this surface rejects or never triggers. The drain on the binding table.

    TWO RULES, and the decline rule is the sharper one. Silent non-use across `DEMOTION_MIN_TRIALS`
    offers says only that nobody reached for it, which has two causes with opposite fixes. A
    REASONED DECLINE says which one it is, in the caller's own words, so it clears at
    `DEMOTION_MIN_DECLINES` and carries its evidence into the proposal.

    THE TWO RULES READ DISJOINT POPULATIONS, and that is load-bearing. `never_triggered` counts
    only offers where NOTHING WAS SAID (`not_triggered_silently`), never declines. The first draft
    counted every offer, so eight honest `no_landing_zone` declines tripped the silent-non-use rule
    and demoted a correct match through the back door — the exact wrong correction the taxonomy
    exists to prevent, arriving via the other rule.

    ONLY DEMOTABLE KINDS COUNT, and that qualifier is the whole point of the taxonomy.
    `testgen-lane` matched CORRECTLY three times in one audit and was structurally impossible
    (`no_landing_zone`, read-only run, no commit target); demoting it would punish a capability for
    being right. So a non-demotable decline is counted, reported on the row, and cannot clear the
    floor. `frontend-verifier`, declined on two frontend-less repos and then producing the
    second-strongest finding of a third audit on a repo that has a display surface, is the same
    lesson from the other side: two negatives are not a verdict on a binding.

    A single trigger at this surface disqualifies the capability from both rules: something that
    actually gets used here is not a demotion candidate however often it is passed over.
    """
    import capability_advisor

    bound = capability_advisor.binding_for(surface, path=path)
    counts = surface_decline_counts(surface, path=path, window_days=window_days)
    seen, used, silent = counts["offered"], counts["triggered"], counts["silent"]
    declined, reasons = counts["declined"], counts["decline_reasons"]
    demotable, by_kind = counts["declined_demotable"], counts["declines_by_kind"]
    out = []
    for c in bound:
        if used.get(c):
            continue
        n_dec, n_seen = declined.get(c, 0), seen.get(c, 0)
        n_dem, n_silent = demotable.get(c, 0), silent.get(c, 0)
        kinds = dict(sorted((by_kind.get(c) or {}).items()))
        fixes = sorted({DECLINE_KINDS[k]["fix"] for k in kinds if k in DECLINE_KINDS})
        if n_dem >= DEMOTION_MIN_DECLINES:
            basis = "declined_with_reason"
            why = (
                f"declined with a stated reason in {n_dec} of {n_seen} offers at this surface, "
                f"{n_dem} of them attributable to the binding (floor "
                f"{DEMOTION_MIN_DECLINES}), never triggered: " + " | ".join(reasons.get(c, [])[:3])
            )
        elif n_silent >= DEMOTION_MIN_TRIALS:
            basis = "never_triggered"
            why = (
                f"bound and offered in {n_seen} experiments for this surface, "
                f"{n_silent} of them passed over with nothing said, triggered "
                f"{used.get(c, 0)} times"
            )
        else:
            continue
        out.append(
            {
                "capability_id": c,
                "surface": surface,
                "offered": n_seen,
                "triggered": used.get(c, 0),
                "silent": n_silent,
                "declined": n_dec,
                # THE QUALIFIED COUNT, beside the raw one, plus the fix each kind implies -- so
                # a reader can choose "add a precondition" over "unbind" where that is the
                # correct answer, instead of inferring one action from one number.
                "declined_demotable": n_dem,
                "declines_by_kind": kinds,
                "implied_fixes": fixes,
                "decline_reasons": reasons.get(c, []),
                "basis": basis,
                # BLOCKING quantity and the floor it is measured against, together.
                "declines_floor": DEMOTION_MIN_DECLINES,
                "silent_offers_floor": DEMOTION_MIN_TRIALS,
                "action": "demote",
                "reason": why,
            }
        )
    return sorted(out, key=lambda r: (-r["declined_demotable"], -r["offered"], r["capability_id"]))


def record_promotion(capability_id: str, surface: str, reason: str, *, path=None) -> bool:
    """Write the binding promotion `capability_advisor.binding_for()` reads. DATA, never a prompt.

    This is the whole reason the binding is a table: the loop changes what a surface reaches for
    without rewriting that surface's instructions. A loop that edited an automation's prompt would
    be a self-modifying dispatch path, and the manual mirror sync is the only circuit breaker there.
    """
    if not reason.strip():
        raise ValueError("a binding promotion must record why")
    return capabilities.heartbeat(
        capability_id,
        "match",
        ref=f"{ADVICE_REF_PREFIX}promotion",
        path=path or capabilities.REG,
        idempotency_key=f"binding_promotion:{surface}:{capability_id}",
        metadata={"source": "binding_promotion", "surface": surface, "reason": reason},
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
