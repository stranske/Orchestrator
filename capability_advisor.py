#!/usr/bin/env python3
"""capability_advisor.py — "Is the Orchestrator useful for this task, and which capabilities apply?"

THE FRONT DOOR. Everything else in the capability stack makes the answer CORRECT (matchers,
attribution, readiness); nothing asked the question. This does, from free text, at the moment a task
arrives — so a session handed "please use Orchestrator if appropriate" has something concrete to
consult instead of guessing.

DESIGN COMMITMENTS, each learned from a failure in this subsystem:

1. IT MUST BE ABLE TO SAY NO. An advisor that always finds something to recommend is worthless —
   worse, it trains you to ignore it. `useful: false` is a first-class, common answer.

2. ADVICE IS NOT DISPATCH. `capabilities.capability_routing_decision` additionally requires
   status=='active' AND immutable version lineage, and today 0 of 33 capabilities have either — so a
   dispatch-strict query returns an empty set for every task, permanently. Advice asks the weaker,
   useful question ("would this capability's declared trigger match this work, and is it usable?")
   and labels each answer with what would have to be true to actually run it. The two are never
   conflated: `dispatch_ready` is reported per capability and is presently false everywhere.

3. LOW CONFIDENCE IS REPORTED, NOT HIDDEN. Task classification is keyword-based and deterministic
   (no model call — this must be fast, offline, and reproducible). When nothing classifies well the
   answer says so rather than guessing a task_type and confidently matching against it.

    python3 capability_advisor.py "add tests for the retry helper"
    python3 capability_advisor.py --json "audit the whole repo for dead code"
    python3 capability_advisor.py --selftest

Also exposed as the `capability_advice` MCP tool, so any session can ask at task initiation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import capabilities
import env_prereq

# Free text -> the task_type vocabulary the fleet actually records. Deterministic and inspectable;
# a model call here would make the same task classify differently on different days, which would
# make the historical match counts meaningless.
TASK_SIGNALS: dict[str, tuple[str, ...]] = {
    "testgen": ("test", "tests", "unit test", "coverage", "pytest", "test case"),
    # `offload` was ABSENT until 2026-08-19 while being the fleet's most-used capability by 20x
    # (196 runs/week vs 2 for the next). So the front door could never recommend the one thing the
    # Orchestrator is used for most: spending a cheap agent's context instead of this seat's.
    "offload": ("offload", "summarise", "summarize", "read these", "large context", "200 pages",
                "delegate this", "conserve capacity", "cheap agent", "token-heavy", "big read"),
    # Real campaign language, taken from the Trend_Model_Project legacy-removal issues that this
    # advisor previously failed to classify at all ("remove the legacy config shapes ... phase 6").
    # The old signals ("rename across", "mass change", "sweep") matched none of the actual work.
    "codemod": ("codemod", "refactor", "rename across", "mass change", "sweep", "migrate all",
                "legacy removal", "remove legacy", "remove remaining", "remove retired",
                "remove duplicate", "consolidate", "deduplicate", "dedupe", "extract shared",
                "across the whole repo", "phase 2", "phase 3", "phase 4", "phase 5", "phase 6",
                "phase 7", "phase 8"),
    "review": ("review", "critique", "assess", "evaluate", "audit", "check quality"),
    "ux_review": ("ux", "usability", "frontend", "ui", "screen", "user interface"),
    "epic": ("epic", "break down", "decompose", "roadmap", "plan out", "vague goal"),
    "cross_repo": ("cross-repo", "across repos", "consumer repos", "fleet-wide", "both repos"),
    "runtime_ac": ("acceptance criteria", "runtime check", "verify behaviour", "verify behavior"),
    "docs": ("docs", "documentation", "readme", "changelog", "docstring"),
    "mechanical": ("lint", "format", "dependency bump", "bump version", "typo"),
    "implement": ("implement", "add feature", "build", "fix bug", "fix the", "write code"),
}
# A task must clear this to be treated as classified at all.
MIN_SIGNAL_HITS = 1


def classify_task(text: str) -> list[dict]:
    """Candidate task types with the phrases that triggered them. Deterministic, order-stable."""
    low = f" {(text or '').lower()} "
    found = []
    for task_type, signals in TASK_SIGNALS.items():
        hits = [s for s in signals if re.search(rf"(?<![a-z]){re.escape(s)}", low)]
        if len(hits) >= MIN_SIGNAL_HITS:
            found.append({"task_type": task_type, "hits": hits, "score": len(hits)})
    return sorted(found, key=lambda c: (-c["score"], c["task_type"]))


def _usability(cap: dict) -> dict:
    """What it would take to actually use this capability right now."""
    unblock = capabilities.unblock(cap)
    gate = capabilities.gate_readiness(cap)
    dispatch_ready = bool(
        cap.get("status") == "active"
        and cap.get("capability_version_id")
        and cap.get("artifact_hash")
        and cap.get("lifecycle_policy_hash")
        and not cap.get("rollback_pending")
    )
    return {
        "status": cap.get("status"),
        "dispatch_ready": dispatch_ready,
        "blocker": unblock["blocker"],
        "next_step": unblock["action"],
        "gate_ready": gate.get("ready") if gate.get("gated") else None,
    }


def advise(text: str, *, repository: str = "", lane: str = "opener", skill: str = "",
           record: bool = True, path=None) -> dict:
    """Should the Orchestrator be used for this task, and which capabilities apply?

    `skill` names the skill that surfaced this work, if any; it is recorded with each match so the
    skill -> capability association can be LEARNED rather than declared. `record=False` makes the
    call a pure query (used by tests and by dry inspection).
    """
    caps = capabilities.load(path or capabilities.REG)
    candidates = classify_task(text)
    if not candidates:
        return {
            "task": text, "useful": False, "confidence": "none", "skill": skill or None,
            "repository": repository, "task_types": [], "capabilities": [],
            "dispatch_ready_count": 0,
            "reason": ("could not classify this task into any work type the fleet records; "
                       "no capability can be matched to it"),
        }

    # Infrastructure capabilities are ENTERED DIRECTLY, never routed by task_type, so their
    # kind-based matchers ({"kind": "transport"}) can never match a {task_type} trigger — it fails
    # closed by design. Without this map the advisor is structurally unable to name them, which is
    # how `offload` stayed invisible. Mirrors dispatcher.TASK_TYPE_CAPABILITY.
    DIRECT_ENTRY = {"offload": "offload"}

    matched: list[dict] = []
    for candidate in candidates:
        direct = DIRECT_ENTRY.get(candidate["task_type"])
        if direct and direct in caps and not any(m["capability_id"] == direct for m in matched):
            cap = caps[direct]
            matched.append({"capability_id": direct,
                            "matched_task_type": candidate["task_type"],
                            "entrypoint": cap.get("entrypoint"),
                            "entered_directly": True, **_usability(cap)})
        trigger = {"repository": repository, "task_type": candidate["task_type"], "lane": lane}
        for cap_id, cap in sorted(caps.items()):
            if cap.get("status") in {"retired", "superseded"}:
                continue
            ok, reasons = capabilities._matches_trigger(cap, trigger)
            if not ok:
                continue
            entry = {"capability_id": cap_id, "matched_task_type": candidate["task_type"],
                     "entrypoint": cap.get("entrypoint"), **_usability(cap)}
            if not any(m["capability_id"] == cap_id for m in matched):
                matched.append(entry)

    # Confidence describes the CLASSIFICATION, not the recommendation — a strong keyword match on a
    # capability that cannot run yet is still high-confidence advice to not bother.
    top = candidates[0]["score"]
    confidence = "high" if top >= 2 else "low"
    usable = [m for m in matched if m["dispatch_ready"]]
    result = {
        "task": text,
        "useful": bool(matched),
        "confidence": confidence,
        "skill": skill or None,
        "repository": repository,
        "task_types": [c["task_type"] for c in candidates],
        "classification_evidence": {c["task_type"]: c["hits"] for c in candidates},
        "capabilities": matched,
        "dispatch_ready_count": len(usable),
        "reason": (
            f"{len(matched)} capability(ies) declare a trigger matching "
            f"{', '.join(c['task_type'] for c in candidates)}"
            + ("; none is dispatch-ready yet, so treat these as advisory"
               if matched and not usable else "")
            if matched else
            f"classified as {', '.join(c['task_type'] for c in candidates)}, but no capability "
            f"declares a trigger for that work"
        ),
    }
    if record and matched:
        # Asking the question is itself the observation that improves the answer.
        result["recorded_matches"] = _record_matches(result, skill=skill, path=path)
    return result


# ---------------------------------------------------------------------------
# RE-ASK TRIGGERS. The question is "is the Orchestrator still the right tool", and it must be asked
# again when the WORK CHANGES, never on a clock. A timer-driven advisory fires during stretches
# where nothing has changed, which is how a surface trains you to ignore it. Every condition below
# is derived from observable state, so it fires on change and is silent otherwise.
# ---------------------------------------------------------------------------

def should_reask(previous: dict | None, current_context: dict) -> dict:
    """Should the advisor be consulted again? Returns {reask, reasons}.

    Stateless: the caller holds the last advice and the current context, so this stays testable and
    imposes no storage. `current_context` carries {task, repository, skill, capabilities_ready}.
    """
    reasons: list[str] = []
    if not previous:
        return {"reask": True, "reasons": ["no_advice_yet"]}

    # The work reclassified — e.g. an "implement" task that has moved on to writing tests. This is
    # the highest-value trigger: it is exactly when a different capability becomes relevant.
    now_types = set(classify_task(str(current_context.get("task") or "")) and
                    [c["task_type"] for c in classify_task(str(current_context.get("task") or ""))])
    was_types = set(previous.get("task_types") or [])
    if now_types and now_types != was_types:
        reasons.append(f"task_reclassified:{','.join(sorted(now_types - was_types)) or 'narrowed'}")

    # A skill starting is a strong, explicit statement about the kind of work now underway.
    if current_context.get("skill") and current_context.get("skill") != previous.get("skill"):
        reasons.append(f"skill_invoked:{current_context['skill']}")

    # Scope moved to a different repository.
    if (current_context.get("repository") or "") != (previous.get("repository") or ""):
        reasons.append("scope_changed")

    # Something previously advisory can now actually run — worth surfacing once.
    was_ready = int(previous.get("dispatch_ready_count") or 0)
    now_ready = int(current_context.get("capabilities_ready") or 0)
    if now_ready > was_ready:
        reasons.append("capability_became_dispatch_ready")

    return {"reask": bool(reasons), "reasons": reasons}


def _record_matches(advice: dict, *, skill: str = "", path=None) -> int:
    """Record that these capabilities matched REAL work, with the skill that surfaced it.

    Uses the existing `match` heartbeat rather than a new store: a capability whose declared trigger
    matched an actual task genuinely experienced a match, and recording it moves the capability out
    of `no_matching_work` into `matched_not_invoked` — which is the honest classification for
    "work of your kind occurred and you still did not run".

    The skill is carried in the event metadata, so `learned_associations()` can later aggregate
    skill -> capability from accumulated observations. Idempotent per (capability, exact task), so
    repeating the same query does not inflate frequency while distinct tasks still accumulate.
    """
    import hashlib
    digest = hashlib.sha1(str(advice.get("task") or "").encode()).hexdigest()[:12]
    written = 0
    for entry in advice.get("capabilities") or []:
        ok = capabilities.heartbeat(
            entry["capability_id"], "match",
            ref=f"advice:{digest}",
            path=path or capabilities.REG,
            idempotency_key=f"advice:{entry['capability_id']}:{digest}",
            metadata={"source": "capability_advisor", "skill": skill or None,
                      "task_type": entry.get("matched_task_type")},
        )
        written += 1 if ok else 0
    return written


def learned_associations(*, path=None) -> dict:
    """skill -> capabilities, learned from accumulated advisory matches.

    This is the emergent half of the design: declared matchers tie capabilities to WORK TYPES, while
    repeated observation ties them to the SKILLS that surface that work. Nothing is hand-declared
    here — if invoking the audit skill keeps surfacing adversarial-review, that association shows up
    on its own once the observations exist.

    EVERY COUNT NAMES ITS OWN POPULATION (FM5, fixed 2026-08-22). `observations` used to be the sum
    over `by_skill` ALONE, so an advisory match recorded WITHOUT a skill was invisible in the count
    while still landing in `by_task_type` — the live ledger read "observations: 3" beside 12
    task-type observations, and the difference looked like a bug in the ledger rather than in the
    denominator. A convenient denominator in the reporting path of the very tool meant to measure
    skill wiring would make "the skills are wired now" unfalsifiable. The three counts below
    reconcile by construction: with_skill + without_skill == observations.
    """
    caps = capabilities.load(path or capabilities.REG)
    by_skill: dict[str, dict[str, int]] = {}
    by_task_type: dict[str, dict[str, int]] = {}
    total = 0
    with_skill = 0
    for cap_id, cap in caps.items():
        for event in cap.get("event_history") or []:
            meta = event.get("metadata") or {}
            if meta.get("source") != "capability_advisor":
                continue
            total += 1
            skill = meta.get("skill")
            if skill:
                with_skill += 1
                by_skill.setdefault(str(skill), {}).setdefault(cap_id, 0)
                by_skill[str(skill)][cap_id] += 1
            tt = meta.get("task_type")
            if tt:
                by_task_type.setdefault(str(tt), {}).setdefault(cap_id, 0)
                by_task_type[str(tt)][cap_id] += 1
    return {
        "by_skill": {s: dict(sorted(v.items(), key=lambda kv: -kv[1])) for s, v in sorted(by_skill.items())},
        "by_task_type": {t: dict(sorted(v.items(), key=lambda kv: -kv[1])) for t, v in sorted(by_task_type.items())},
        "observations": total,
        "observations_with_skill": with_skill,
        "observations_without_skill": total - with_skill,
        "populations": {
            "observations": "every capability_advisor match event in the ledger",
            "observations_with_skill":
                "the subset naming a skill — the ONLY population by_skill can cover",
            "observations_without_skill":
                "matches with no skill attributed: counted in by_task_type, absent from by_skill",
            "by_task_type": "every match event carrying a task_type, skill-attributed or not",
        },
    }


# How to actually USE each capability from a chat session. The ledger's `next_step` answers a
# lifecycle question ("lift the gate, or accept it is deliberately off"), which is the wrong answer
# for someone who just wants to route the task in front of them. This maps capability -> the concrete
# thing to run or do. Absent entries fall back to the lifecycle text rather than inventing a command.
HOW_TO_USE = {
    "offload": ("dispatcher.offload('gemini', prompt, cwd=repo) — spends the cheap agent's context "
                "instead of this seat's; returns the result, opens no PR"),
    "codemod-campaign": ("label the issue `refactor` (or let the daily issue_readiness task-label "
                         "step do it) so classify() routes it to the codemod lane; the lane hands "
                         "an agent the codemod_lane.py plan schema"),
    "testgen-lane": ("label the issue `testing`; the lane adds the testgen_gate.py acceptance gate "
                     "to the prompt and requires it to pass before the PR body is accepted"),
    "adversarial-review": ("adversarial.review(worktree, reviewers=['vibe','gemini']) — refute-mode "
                           "minority-veto panel; use when being wrong is expensive, not for routine "
                           "review"),
    "ux-review": "run the /ux-review skill; drives every primary surface, not the happy path",
    "epic-decomposition": ("only for a PARENT epic ([Epic] with no #NNN parent ref); produces a "
                           "subtask plan, does not implement"),
    "cross-repo-coordination": ("label `consumer-sync`/`cross-repo`; produces a dry-run rollout plan "
                                "with barrier ordering, creates nothing"),
    "deliberate-break-verifier": ("local_verify.py — proves a test gate actually fails when the "
                                  "behaviour is broken, so a vacuous gate cannot pass"),
    "docs-drift-fix-agent": ("bounded docs-drift repair BATCHES from an existing drift scan; it does "
                             "not do a semantic docs review and edits nothing itself"),
    "runtime-ac-checks": ("runtime_ac.py — turns acceptance criteria into a structured evidence plan; "
                          "execution is opt-in via --confirm-run and mutates nothing"),
}


def format_advice(a: dict) -> str:
    verdict = "USE THE ORCHESTRATOR" if a["useful"] else "NO ORCHESTRATOR CAPABILITY APPLIES"
    lines = [f"{verdict} — {a['reason']}", ""]
    if a["task_types"]:
        lines.append(f"classified as: {', '.join(a['task_types'])} (confidence: {a['confidence']})")
        for tt, hits in (a.get("classification_evidence") or {}).items():
            lines.append(f"  {tt}: matched on {', '.join(repr(h) for h in hits)}")
        lines.append("")
    for cap in a["capabilities"]:
        flag = "READY" if cap["dispatch_ready"] else "advisory"
        lines.append(f"- {cap['capability_id']}  [{flag}] via {cap['matched_task_type']}")
        lines.append(f"    entrypoint: {cap['entrypoint']}")
        lines.append(f"    blocker:    {cap['blocker']}")
        how = HOW_TO_USE.get(cap["capability_id"])
        lines.append(f"    how to use: {how}" if how
                     else f"    next step:  {cap['next_step']}")
        if cap.get("entered_directly"):
            lines.append("    note:       entered directly in code — not routed by task type")
    return "\n".join(lines) + "\n"


def _selftest_front_door() -> None:
    """The front door must name the RIGHT capability for the tasks the fleet actually does.

    Each case here is a real failure observed on 2026-08-19: `offload` (196 runs/week, the most-used
    capability) was unreachable because TASK_SIGNALS had no entry for it AND its kind-based matcher
    can never match a task_type trigger; and the legacy-removal campaign — proven-valuable codemod
    work — classified as nothing at all.

    Every case names a capability the advisor must FIND, so the whole function needs those rows
    present. They are registered by running the system, not by checking out the tree.
    """
    gaps: list[str] = []
    if not env_prereq.runnable(gaps, env_prereq.ledger_rows_absent(
            "offload", "codemod-campaign", "testgen-lane")):
        env_prereq.report_gaps("capability_advisor.py front-door", gaps)
        return
    cases = [
        ("summarise these 200 pages of docs", "offload"),
        ("offload this big read to a cheap agent", "offload"),
        ("remove the legacy config shapes across the whole repo, phase 6", "codemod-campaign"),
        ("Legacy removal Phase 8: enforce zero-reference gates", "codemod-campaign"),
        ("add pytest coverage for the retry helper", "testgen-lane"),
    ]
    for text, expected in cases:
        r = advise(text, record=False)
        ids = [c["capability_id"] for c in r["capabilities"]]
        assert r["useful"], f"front door said NO to real work: {text!r}"
        assert expected in ids, f"{text!r} -> {ids}, expected {expected}"
    # It must still be able to say NO.
    for text in ("what did I eat for lunch", "book a flight to Lisbon"):
        assert not advise(text, record=False)["useful"], text
    # And the advice must be actionable, not a lifecycle instruction.
    text = format_advice(advise("summarise these 200 pages", record=False))
    assert "how to use:" in text and "dispatcher.offload" in text, text
    assert "entered directly" in text, text
    print("capability_advisor front-door selftest: OK (offload + campaign reachable, says no, "
          "advice is actionable)")


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    # Classification is deterministic and evidence-bearing.
    c = classify_task("please add unit tests for the retry helper")
    assert c and c[0]["task_type"] == "testgen", c
    assert c[0]["hits"], "classification must report WHY it classified"
    assert classify_task("please add unit tests for the retry helper") == c, "must be deterministic"

    # IT MUST BE ABLE TO SAY NO — both when unclassifiable and when nothing declares a trigger.
    with tempfile.TemporaryDirectory(prefix="advisor-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        lane = capabilities._blank_capability("testgen-lane")
        lane["status"] = "generated"
        lane["entrypoint"] = "testgen_lane.py"
        lane["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
        unrelated = capabilities._blank_capability("some-other")
        unrelated["status"] = "generated"
        unrelated["matcher"] = {"field": "task_type", "operator": "in", "value": ["codemod"]}
        retired = capabilities._blank_capability("gone")
        retired["status"] = "retired"
        retired["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
        capabilities.save({"testgen-lane": lane, "some-other": unrelated, "gone": retired}, ledger)

        hit = advise("add unit tests for the retry helper", path=ledger)
        assert hit["useful"] is True, hit
        ids = [m["capability_id"] for m in hit["capabilities"]]
        assert ids == ["testgen-lane"], ids                 # only the matching, non-retired one
        assert hit["capabilities"][0]["dispatch_ready"] is False, hit
        assert "advisory" in hit["reason"], hit             # never implies it will actually run

        # Unclassifiable free text => a clean NO, with the reason.
        none = advise("xyzzy plugh frobnicate", path=ledger)
        assert none["useful"] is False and none["confidence"] == "none", none
        assert "could not classify" in none["reason"], none

        # Classified but nothing declares that trigger => also a clean NO.
        gap = advise("rewrite the documentation and the readme", path=ledger)
        assert gap["useful"] is False and "docs" in gap["task_types"], gap
        assert "no capability declares a trigger" in gap["reason"], gap

        # A capability with NO matcher must never be advised (fail-closed matching). Kept in the
        # SAME ledger — saving a single-capability dict would replace the whole file.
        bare = capabilities._blank_capability("bare")
        bare["status"] = "generated"
        capabilities.save({"testgen-lane": lane, "some-other": unrelated, "gone": retired,
                           "bare": bare}, ledger)
        advised = [m["capability_id"] for m in advise("add unit tests", path=ledger,
                                                      record=False)["capabilities"]]
        assert "bare" not in advised, advised

        text = format_advice(hit)
        assert "USE THE ORCHESTRATOR" in text and "testgen-lane" in text
        assert "NO ORCHESTRATOR CAPABILITY APPLIES" in format_advice(none)

        # --- RE-ASK TRIGGERS: fire on CHANGE, never on a clock -----------------------------
        first = advise("add unit tests", path=ledger, record=False)
        # Same work, same scope => silent. This is the case a timer would get wrong.
        quiet = should_reask(first, {"task": "add unit tests", "repository": "",
                                     "capabilities_ready": 0})
        assert quiet["reask"] is False and quiet["reasons"] == [], quiet
        # The work reclassified — the highest-value trigger.
        moved = should_reask(first, {"task": "now refactor every call site", "repository": "",
                                     "capabilities_ready": 0})
        assert moved["reask"] and any(r.startswith("task_reclassified") for r in moved["reasons"]), moved
        # A skill starting is an explicit statement about the kind of work now underway.
        sk = should_reask(first, {"task": "add unit tests", "skill": "repo-audit",
                                  "repository": "", "capabilities_ready": 0})
        assert "skill_invoked:repo-audit" in sk["reasons"], sk
        # Scope moved, and something became runnable.
        assert "scope_changed" in should_reask(
            first, {"task": "add unit tests", "repository": "o/other", "capabilities_ready": 0})["reasons"]
        assert "capability_became_dispatch_ready" in should_reask(
            first, {"task": "add unit tests", "repository": "", "capabilities_ready": 1})["reasons"]
        # No prior advice always asks.
        assert should_reask(None, {"task": "anything"})["reasons"] == ["no_advice_yet"]

        # --- LEARNED skill -> capability association ---------------------------------------
        assert learned_associations(path=ledger)["observations"] == 0, "nothing learned yet"
        advise("add unit tests for the parser", skill="repo-audit", path=ledger)
        advise("write tests for the loader", skill="repo-audit", path=ledger)
        assoc = learned_associations(path=ledger)
        assert assoc["by_skill"]["repo-audit"]["testgen-lane"] == 2, assoc
        assert assoc["by_task_type"]["testgen"]["testgen-lane"] == 2, assoc
        # Repeating the SAME task must not inflate frequency; a distinct task must count.
        advise("write tests for the loader", skill="repo-audit", path=ledger)
        assert learned_associations(path=ledger)["by_skill"]["repo-audit"]["testgen-lane"] == 2
        # record=False stays a pure query.
        before = learned_associations(path=ledger)["observations"]
        advise("add tests for the writer", skill="repo-audit", path=ledger, record=False)
        assert learned_associations(path=ledger)["observations"] == before, "record=False wrote"
        # Recording a match moves the capability out of no_matching_work — the honest reading of
        # "work of your kind occurred and you still did not run".
        after = capabilities.load(ledger)["testgen-lane"]
        assert after["last_match"], after
        assert capabilities.classify_liveness(after) == "matched_not_invoked", after

        # --- DENOMINATORS: a skill-less match must not be invisible (FM5) -------------------
        # THE BUG THIS CATCHES: `observations` was the sum over by_skill alone, so an advisory
        # match with no skill attributed vanished from the count while still being counted in
        # by_task_type. Every skill-wiring claim measured with that number was unfalsifiable.
        # An advisory call with NO skill — exactly what a session that forgets `skill=` produces.
        advise("add unit tests for the anonymous caller", path=ledger)
        d = learned_associations(path=ledger)
        # by_skill is blind to it, by design; the totals are not, and they reconcile.
        assert "" not in d["by_skill"] and None not in d["by_skill"], d["by_skill"]
        assert d["observations_with_skill"] == sum(
            sum(v.values()) for v in d["by_skill"].values()), d
        assert d["observations_without_skill"] >= 1, d      # the skill-less match IS visible
        assert d["observations"] == (
            d["observations_with_skill"] + d["observations_without_skill"]), d
        # The old definition would have made these two equal; they must differ here, or the
        # convenient denominator is back.
        assert d["observations"] > d["observations_with_skill"], d
        # Every count states which population it covers, so a subset cannot pass as the set.
        assert set(d["populations"]) >= {"observations", "observations_with_skill",
                                         "observations_without_skill", "by_task_type"}, d

    print("capability_advisor.py selftest: OK (deterministic classification, says NO, "
          "advice never implies dispatch, retired/unmatched excluded, denominators named)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", nargs="*", help="the task, in plain words")
    ap.add_argument("--repository", default="")
    ap.add_argument("--lane", default="opener")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        _selftest_front_door()
        return 0
    if not args.task:
        ap.error("give the task in plain words, or use --selftest")
    result = advise(" ".join(args.task), repository=args.repository, lane=args.lane)
    print(json.dumps(result, indent=2) if args.json else format_advice(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
