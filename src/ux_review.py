#!/usr/bin/env python3
"""ux_review.py — Gate 2: evidence-bound UX + wiring review of a running frontend.

WHY THIS EXISTS: Gate 1 (frontend_verify) proves named interactions wire correctly; this gate asks
whether a first-time user can actually finish the core workflow — scored by an anonymized panel of
LLM evaluators plus a hostile adversarial critic, recorded to feedback.py for cross-repo learning
and anchored by human calibration. It is the structural cure for "claimed done/usable but never
really evaluated."

Pairs with Gate 1. Consumes a pre-captured bundle (a11y trees, scenario transcripts with OBSERVED
outcomes, Gate-1 wired findings); evaluators judge from text evidence only.

Out of scope for v1: multimodal screenshot input to evaluators; automated bundle capture (operator-
supplied for now); auto-running on every PR (manual invocation first, wire to a lane later).

Pure helpers are selftested offline (`--selftest`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import TextIO

import adapters
import dispatcher
import feedback
import provision
import research_subjects
from exp_abcd import (
    AGENT_MODE,
    _ensure_min_evaluators,
    _eval_command,
    _extract_json,
)

ORCH = Path(__file__).resolve().parent
REVIEW_DIR = Path(os.environ.get("ORCH_UX_REVIEW_DIR", ORCH / "ux_reviews"))

DIMENSIONS = ("wired", "usability", "help_clarity", "workflow_productivity")
FAILURE_MODES = frozenset(
    {
        "false_success",
        "recovery_failure",
        "efficiency_trap",
        "confusion",
        "missing_help",
    }
)


def median(values: list[float]) -> float:
    """Robust central tendency for panel scores (pure; selftested)."""
    if not values:
        return 0.0
    s = sorted(float(v) for v in values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def consensus_flag(scores: list[float]) -> bool:
    """True when evaluator score spread is >= 3 → route to human calibration (pure; selftested).
    Uses >= (not >) so a 4-vs-7 split on a 0-10 dimension escalates: meaningful disagreement, not noise.
    """
    if len(scores) < 2:
        return False
    return max(scores) - min(scores) >= 3


def has_reproducible_click_path(finding: dict) -> bool:
    click_path = finding.get("click_path")
    return (
        isinstance(click_path, list)
        and len(click_path) > 0
        and all(str(s).strip() for s in click_path)
    )


def finding_key(finding: dict) -> tuple[str, str, str]:
    return (
        str(finding.get("screen") or ""),
        str(finding.get("element") or ""),
        str(finding.get("failure_mode") or ""),
    )


def dedupe_findings(findings: list[dict]) -> list[dict]:
    """Dedupe by (screen, element, failure_mode), keeping max severity + max confidence (pure; selftested)."""
    merged: dict[tuple[str, str, str], dict] = {}
    for f in findings:
        k = finding_key(f)
        if k not in merged:
            merged[k] = dict(f)
            continue
        cur = merged[k]
        cur["severity"] = max(int(cur.get("severity") or 0), int(f.get("severity") or 0))
        cur["confidence"] = max(float(cur.get("confidence") or 0), float(f.get("confidence") or 0))
        if float(f.get("stuck_probability") or 0) > float(cur.get("stuck_probability") or 0):
            cur["stuck_probability"] = f.get("stuck_probability")
    out = list(merged.values())
    out.sort(
        key=lambda x: (-int(x.get("severity") or 0), x.get("screen", ""), x.get("element", ""))
    )
    return out


def _merge_finding_group(group: list[dict]) -> dict:
    base = dict(group[0])
    for f in group[1:]:
        base["severity"] = max(int(base.get("severity") or 0), int(f.get("severity") or 0))
        base["confidence"] = max(
            float(base.get("confidence") or 0), float(f.get("confidence") or 0)
        )
        if float(f.get("stuck_probability") or 0) > float(base.get("stuck_probability") or 0):
            base["stuck_probability"] = f.get("stuck_probability")
    # Preserve EVERY distinct fix_hint across the cluster — the per-evaluator improvement
    # ideas are the productive payload, and a single-rep merge silently dropped all but one.
    fix_hints: list[str] = []
    seen_fh: set[str] = set()
    for f in group:
        fh = " ".join(str(f.get("fix_hint") or "").split())
        if fh and fh.lower() not in seen_fh:
            seen_fh.add(fh.lower())
            fix_hints.append(fh[:500])
    if fix_hints:
        base["fix_hints"] = fix_hints
    for k in ("_source", "_adversarial", "reject_reason"):
        base.pop(k, None)
    return base


def aggregate_accepted_findings(
    evaluator_findings: dict[str, list[dict]],
    adversarial_findings: list[dict],
    n_evaluators: int,
) -> tuple[list[dict], list[dict]]:
    """Accept findings by majority (screen, element, failure_mode) or adversarial stuck_probability>=0.5.

    Reject findings without a reproducible click_path as non_findings (audit trail only).
    Returns (accepted, non_findings). Pure; selftested.
    """
    raw: list[dict] = []
    for ev, findings in evaluator_findings.items():
        for f in findings or []:
            raw.append({**f, "_source": ev, "_adversarial": False})
    for f in adversarial_findings or []:
        raw.append({**f, "_source": "adversarial", "_adversarial": True})

    non_findings: list[dict] = []
    candidates: list[dict] = []
    for f in raw:
        if not has_reproducible_click_path(f):
            nf = dict(f)
            nf["reject_reason"] = "missing_click_path"
            non_findings.append(nf)
            continue
        candidates.append(f)

    # Cluster by FAILURE MODE (the stable semantic category), NOT the exact
    # (screen, element, failure_mode) string. Evaluators describe the same issue with different
    # screen/element wording, so an exact-key majority silently DROPPED real corroborated findings
    # (observed: panels emitted 4-9 findings each, all discarded -> report showed findings:[]).
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in candidates:
        groups[str(f.get("failure_mode") or "")].append(f)

    accepted: list[dict] = []
    majority_needed = (n_evaluators // 2) + 1
    for group in groups.values():
        ev_sources = {g["_source"] for g in group if not g.get("_adversarial")}
        adv_hits = [g for g in group if g.get("_adversarial")]
        adv_accept = any(float(g.get("stuck_probability") or 0) >= 0.5 for g in adv_hits)
        if len(ev_sources) >= majority_needed or adv_accept:
            rep = _merge_finding_group(group)
            rep["corroboration"] = len(ev_sources)
            accepted.append(rep)

    return dedupe_findings(accepted), non_findings


def finding_severity_spread(
    evaluator_findings: dict[str, list[dict]], key: tuple[str, str, str]
) -> int:
    severities: list[int] = []
    for findings in evaluator_findings.values():
        for f in findings or []:
            if finding_key(f) == key:
                severities.append(int(f.get("severity") or 0))
    if len(severities) < 2:
        return 0
    return max(severities) - min(severities)


def compute_overall_median(evaluator_overalls: list[float], accepted_findings: list[dict]) -> float:
    """Blocker-dominated overall: a confirmed severity-4 caps the median at <=3 (pure; selftested)."""
    raw = median(evaluator_overalls)
    if any(int(f.get("severity") or 0) >= 4 for f in accepted_findings):
        return min(raw, 3.0)
    return raw


DERIVE_THRESHOLD = 6.0
_DIM_FAILMODE = {
    "wired": "false_success",
    "usability": "confusion",
    "help_clarity": "missing_help",
    "workflow_productivity": "efficiency_trap",
}


def severity_from_median(m: float) -> int:
    """Map a low dimension median to a derived-finding severity (pure; selftested)."""
    if m <= 3:
        return 3
    if m < DERIVE_THRESHOLD:
        return 2
    return 0


def derive_findings(
    bundle: dict, dimension_medians: dict, threshold: float = DERIVE_THRESHOLD
) -> list[dict]:
    """Deterministically derive GROUNDED findings for low-scored dimensions from the bundle's OBSERVED
    failures. The panel reliably scores a failure low but won't restate a documented failure as a finding
    (observed across 3 real reviews), so the report otherwise carries a verdict but no findings. Each
    derived finding cites real bundle evidence and is marked source='derived'; a dimension with NO concrete
    bundle evidence yields NO finding (left as an evidence_gap — never fabricate). Pure; selftested.
    """
    out: list[dict] = []
    screens = bundle.get("screens") or [{}]
    first_screen = (screens[0] or {}).get("name") or "App"
    failed_scenarios = [
        s for s in (bundle.get("scenarios") or []) if s.get("goal_achieved") is False
    ]
    failed_wired = [
        f for f in ((bundle.get("wired") or {}).get("findings") or []) if f.get("passed") is False
    ]
    help_text = bundle.get("help_surfaces") or ""
    for dim in DIMENSIONS:
        m = dimension_medians.get(dim)
        if m is None or float(m) >= threshold:
            continue
        sev = severity_from_median(float(m))
        if sev == 0:
            continue
        screen, click_path, element, evidence = first_screen, ["open the app"], dim, None
        if dim == "wired" and failed_wired:
            wf = failed_wired[0]
            element = str(wf.get("interaction") or "control")
            evidence = str(wf.get("note") or "control does not behave as claimed")
            click_path = ["open the app", f"observe '{element}'"]
        elif dim in ("usability", "workflow_productivity") and failed_scenarios:
            sc = failed_scenarios[0]
            steps = sc.get("steps") or []
            element = str(sc.get("goal") or sc.get("name") or "core task")
            click_path = [
                str(step.get("action") if isinstance(step, dict) else step) for step in steps
            ] or ["open the app"]
            last = steps[-1] if steps else None
            evidence = str(
                (last.get("observed") if isinstance(last, dict) else None)
                or "the workflow did not reach its goal"
            )
        elif dim == "help_clarity" and help_text:
            element = "in-app help / field labels / error messages"
            evidence = str(help_text)[:300]
            click_path = ["open the app", "read the labels and any error/help text"]
        if evidence is None:
            continue  # no concrete bundle evidence -> leave as evidence_gap; do NOT fabricate
        out.append(
            {
                "dimension": dim,
                "severity": sev,
                "screen": screen,
                "element": element,
                "click_path": click_path,
                "expected": f"{dim.replace('_', ' ')} holds for a first-time user",
                "actual": evidence,
                "failure_mode": _DIM_FAILMODE.get(dim, "confusion"),
                "confidence": 0.6,
                "source": "derived",
                "dimension_median": float(m),
            }
        )
    return out


def build_rubric_prompt(bundle: dict) -> str:
    """Evidence-bound rubric prompt (verbatim rubric text; selftested)."""
    bundle_json = json.dumps(bundle, indent=2, default=str)
    return (
        "You are a UX evaluator reviewing a pre-captured frontend bundle (accessibility trees, "
        "scenario transcripts with OBSERVED outcomes, and Gate-1 wiring findings). Evaluators do "
        "NOT browse the live URL — judge ONLY from the supplied evidence.\n\n"
        "Score four dimensions 0-10:\n"
        "- wired: do controls do what they claim? cross-check the wired findings; flag claimed-but-dead\n"
        "- usability: can a first-time user finish the core task without confusion?\n"
        "- help_clarity: are labels/tooltips/empty-states/errors sufficient & clear?\n"
        "- workflow_productivity: is the core workflow efficient — steps/clicks/friction?\n\n"
        "Return STRICT JSON only, exactly this shape:\n"
        '{"scores":{"wired":0-10,"usability":0-10,"help_clarity":0-10,"workflow_productivity":0-10},\n'
        ' "findings":[{"dimension":"<wired|usability|help_clarity|workflow_productivity>",'
        '"severity":0-4,"screen":"<name>","element":"<what>",'
        '"click_path":["step1","step2"],"expected":"<...>","actual":"<...>","fix_hint":"<...>",'
        '"confidence":0-1,'
        '"failure_mode":"<false_success|recovery_failure|efficiency_trap|confusion|missing_help>"}],\n'
        ' "overall":0-10,\n'
        ' "evidence_gaps":["<bundle data that was missing to judge well>"]}\n\n'
        "HARD RULES:\n"
        "(a) For EVERY dimension you score below 8 you MUST emit at least one finding for that "
        'dimension, citing screen + click_path + expected + actual — no abstract findings ("feels '
        'clunky") allowed. A low score with no finding is invalid output.\n'
        "(b) severity scale 0=none, 1=cosmetic, 2=minor, 3=major, 4=blocker;\n"
        "(c) return STRICT JSON only, no prose.\n\n"
        'OBSERVED OUTCOMES RULE: Each scenario step includes an "observed" field documenting what '
        "actually happened (from Gate-1 click→assert). Never infer behavior not present in observed. "
        "But an OBSERVED failure — a dead-end, an error, a cryptic message, a missing affordance — IS "
        "finding evidence: write it as a finding, citing the observed outcome as `actual` and what the "
        "user expected as `expected`. `evidence_gaps` are ONLY for an aspect you genuinely could not "
        "judge for lack of data — they are NOT a substitute for a finding about a failure you DID observe.\n\n"
        "===== BUNDLE =====\n"
        f"{bundle_json}\n"
    )


def build_adversarial_prompt(bundle: dict) -> str:
    """Adversarial critic prompt (verbatim hostile-user framing; selftested)."""
    bundle_json = json.dumps(bundle, indent=2, default=str)
    return (
        "You are a hostile, novice first-time user who WANTS to fail. Using ONLY the supplied "
        "screens/scenarios, find the places a confused user gets stuck or does the wrong thing.\n\n"
        "Return STRICT JSON only, exactly this shape:\n"
        '{"findings":[{"dimension":"adversarial","severity":0-4,"screen":"<name>","element":"<what>",'
        '"click_path":["step1","step2"],"expected":"<...>","actual":"<...>","fix_hint":"<...>",'
        '"confidence":0-1,'
        '"failure_mode":"<false_success|recovery_failure|efficiency_trap|confusion|missing_help>",'
        '"stuck_probability":0-1}],\n'
        ' "worst_case":"<the single most likely point of failure>",\n'
        ' "evidence_gaps":["<bundle data that was missing>"]}\n\n'
        "HARD RULES: same severity scale; findings MUST cite screen + click_path + expected + actual; "
        "never infer beyond observed outcomes; STRICT JSON only. An observed dead-end, error, or "
        "confusing state IS where a novice gets stuck — you MUST emit it as a finding with a high "
        "stuck_probability; do NOT return empty findings when the scenario shows a failure.\n\n"
        "===== BUNDLE =====\n"
        f"{bundle_json}\n"
    )


def _normalize_evidence_gaps(parsed: dict | None) -> list[str]:
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("evidence_gaps")
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    gaps: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value.get("gap") if isinstance(value, dict) else value).strip()
        text = " ".join(text.split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        gaps.append(text[:500])
    return gaps


def panel_arm_outcome(
    parsed: dict | None,
    arm_findings: list[dict],
    accepted_keys: set,
    panel_found_anything: bool,
) -> tuple[str, str, str]:
    """Machine ground truth for ONE panel arm: (verdict, durability, note). Pure; selftested.

    Route weights learn from `outcomes`, so a UX-review arm needs an un-gameable label or its
    evidence cannot reach the router. Two objective signals, both already computed by the panel:

    * The arm either produced parseable rubric JSON with dimension scores, or it did not. That is
      not a judgement call, so it is the verdict.
    * Durability is CONTRIBUTION TO CORROBORATED CONSENSUS: did any of this arm's findings survive
      `aggregate_accepted_findings`? An arm whose every finding was rejected as a non-finding added
      noise, not evidence.

    The clean-app case is deliberately not penalised. When the panel corroborated nothing at all,
    every parsing arm is credited `held`. Marking an arm down for reporting no findings on a sound
    app would train arms to invent findings -- the exact gaming this label exists to prevent.
    """
    if not parsed or not (parsed.get("scores") or {}):
        return "FAIL", "reverted", "objective:unparseable-or-unscored"
    if not panel_found_anything:
        return "PASS", "held", "objective:clean-app-consensus"
    contributed = any(finding_key(f) in accepted_keys for f in arm_findings)
    if contributed:
        return "PASS", "durable", "objective:finding-corroborated"
    return "PASS", "reverted", "objective:no-finding-corroborated"


def aggregate_panel(
    evaluator_results: dict[str, dict | None],
    adversarial_result: dict | None,
    n_evaluators: int,
) -> dict:
    """Pure aggregation of parsed evaluator JSON into dimension medians, flags, and findings."""
    dimension_scores: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    evaluator_overalls: list[float] = []
    evaluator_findings: dict[str, list[dict]] = {}
    panel: dict[str, dict] = {}
    all_gaps: list[str] = []

    for ev, parsed in evaluator_results.items():
        if not parsed:
            evaluator_findings[ev] = []
            continue
        scores = parsed.get("scores") or {}
        panel[ev] = scores if isinstance(scores, dict) else {}
        for dim in DIMENSIONS:
            if dim in scores:
                try:
                    dimension_scores[dim].append(float(scores[dim]))
                except (TypeError, ValueError):
                    pass
        try:
            evaluator_overalls.append(float(parsed.get("overall", 0)))
        except (TypeError, ValueError):
            pass
        evaluator_findings[ev] = list(parsed.get("findings") or [])
        all_gaps.extend(_normalize_evidence_gaps(parsed))

    adv_findings = list((adversarial_result or {}).get("findings") or [])
    all_gaps.extend(_normalize_evidence_gaps(adversarial_result))

    dimension_medians = {d: median(dimension_scores[d]) for d in DIMENSIONS}
    consensus_flags: dict[str, bool] = {d: consensus_flag(dimension_scores[d]) for d in DIMENSIONS}

    accepted, non_findings = aggregate_accepted_findings(
        evaluator_findings,
        adv_findings,
        n_evaluators,
    )

    for f in accepted:
        sev = int(f.get("severity") or 0)
        if sev >= 3:
            spread = finding_severity_spread(evaluator_findings, finding_key(f))
            if spread >= 2:
                flag_key = f"finding:{f.get('screen')}:{f.get('element')}:{f.get('failure_mode')}"
                consensus_flags[flag_key] = True

    overall_median = compute_overall_median(evaluator_overalls, accepted)
    blockers = [f for f in accepted if int(f.get("severity") or 0) >= 4]

    return {
        "dimension_medians": dimension_medians,
        "consensus_flags": consensus_flags,
        "overall_median": overall_median,
        "findings": accepted,
        "non_findings": non_findings,
        "adversarial": {
            "worst_case": (adversarial_result or {}).get("worst_case"),
            "findings": adv_findings,
        },
        "evidence_gaps": all_gaps,
        "panel": panel,
        "blockers": blockers,
    }


def resolve_panel_base_sha(bundle: dict) -> str | None:
    """The commit the reviewed app was running, or None when it genuinely cannot be known.

    A UX review is of a running app at a specific commit, and that commit was never captured, so
    every panel subject registered with `base_sha=None` -- which is one of the two fields
    `identity_complete` requires. The bundle wins if the caller supplied it; otherwise the app's
    canonical checkout is asked, since the panel already knows which app it reviewed.

    Returns None rather than a placeholder when the app is not a repository (a local tool, a
    domain study). An invented base commit would make two different states of the same app look
    like one subject, which is worse than an honest gap.
    """
    supplied = str(bundle.get("base_sha") or "").strip()
    if supplied:
        return supplied
    # HISTORICAL PANELS MUST NOT BORROW TODAY'S HEAD. The git fallback below is right for a panel
    # running NOW -- the app under review is the checkout at HEAD -- and catastrophically wrong for
    # a backfill of a June panel, because it would stamp August's commit onto it and make two
    # different states of the same app look like one subject. That is precisely the failure this
    # function's docstring warns about, so a caller replaying history says so and gets None.
    if bundle.get("base_sha_unrecoverable"):
        return None
    app = str(bundle.get("app") or "").strip()
    if "/" not in app:
        return None
    try:
        canon = provision.canonical_path(app)
        if not canon or not Path(canon).is_dir():
            return None
        out = subprocess.run(
            ["git", "-C", str(canon), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        sha = (out.stdout or "").strip()
        return sha or None
    except Exception as exc:  # never fail a panel over provenance
        print(f"warn: ux_review base_sha unresolved for {app}: {exc}", file=sys.stderr)
        return None


def register_panel_subject(
    bundle: dict,
    evaluators: list[str],
    *,
    spec: str | None = None,
    conn=None,
) -> dict | None:
    """Register ONE research subject for a UX-review panel, and return its identity.

    This is the join key the completion-event exporter needs: it resolves identity through
    ``research_subject_experiments -> research_subjects`` keyed on ``experiment_id``. Without a
    subject row every run the panel records is identity-less, so the panel's evidence stays
    invisible to pattern mining no matter how many arms it scores.

    Nothing here is invented. The spec is the panel's real rubric prompt -- byte-identical for
    every arm, which is what makes one spec hash per panel correct -- and the arm set is the real
    evaluator list. Returns None when registration fails, having said so on stderr.
    """
    spec = build_rubric_prompt(bundle) if spec is None else spec
    review_id = bundle["review_id"]
    try:
        identity = research_subjects.subject_identity(
            bundle["app"], "ux_review", spec, resolve_panel_base_sha(bundle), evaluators
        )
        research_subjects.record_subject(
            identity,
            lifecycle="active",
            exp_id=review_id,
            reason="uxreview_panel",
            conn=conn,
        )
        return identity
    except Exception as exc:  # must not kill a panel that costs real agent time
        # Never silent: a swallowed failure here is indistinguishable from a panel that was
        # never meant to be mined, which is exactly how this evidence went missing before.
        print(
            f"warn: ux_review subject registration failed for {review_id}: {exc}",
            file=sys.stderr,
        )
        return None


def review(
    bundle: dict,
    evaluators: list[str] | None = None,
    adversary: str = "claude",
    timeout: int = 1500,
) -> dict:
    """Run the UX review panel concurrently (mirrors exp_abcd.evaluate launch pattern)."""
    evaluators = _ensure_min_evaluators(evaluators or ["claude", "codex", "cursor", "gemini"])
    review_id = bundle["review_id"]
    app = bundle["app"]
    rdir = REVIEW_DIR / review_id.replace(":", "_")
    rdir.mkdir(parents=True, exist_ok=True)

    # The rubric prompt is the panel's SPEC and is identical for every arm, so it is built
    # once here rather than per evaluator, and reused as the subject's spec below.
    rubric_prompt = build_rubric_prompt(bundle)

    register_panel_subject(bundle, evaluators, spec=rubric_prompt)

    # The middle element is the evaluator's open log file, not an opaque object — declaring it
    # `object` is what made every `out.write/flush/close` unreachable to the checker.
    procs: dict[str, tuple[subprocess.Popen, TextIO, Path]] = {}
    for ev in evaluators:
        pf = rdir / f"rubric-prompt-{ev}.txt"
        pf.write_text(rubric_prompt)
        out_path = rdir / f"rubric-out-{ev}.txt"
        out = out_path.open("w")
        mode = AGENT_MODE.get(ev, "full")
        run_id = f"{review_id}:eval:{ev}"
        target = f"{app} [ux_review]"
        feedback.record_run(
            run_id,
            target,
            "ux_review",
            ev,
            mode=mode,
            reasoning_level=mode,
            experiment_id=review_id,
            model=adapters.model_identity(ev, mode),
            rationale="Gate 2 UX review panel evaluator",
        )
        out.write(
            f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} UX-REVIEW "
            f"{ev}/{mode} run_id={run_id} ===\n"
        )
        out.flush()
        procs[ev] = (
            subprocess.Popen(
                ["bash", "-lc", dispatcher._net_hygiene_prelude() + _eval_command(ev, str(pf))],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            ),
            out,
            out_path,
        )

    adv_pf = rdir / f"adversarial-prompt-{adversary}.txt"
    adv_pf.write_text(build_adversarial_prompt(bundle))
    adv_out_path = rdir / f"adversarial-out-{adversary}.txt"
    adv_out = adv_out_path.open("w")
    adv_mode = AGENT_MODE.get(adversary, "full")
    adv_run_id = f"{review_id}:adversary:{adversary}"
    adv_target = f"{app} [ux_review adversary]"
    feedback.record_run(
        adv_run_id,
        adv_target,
        "ux_review",
        adversary,
        mode=adv_mode,
        reasoning_level=adv_mode,
        experiment_id=review_id,
        model=adapters.model_identity(adversary, adv_mode),
        rationale="Gate 2 UX review adversarial critic",
    )
    adv_out.write(
        f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} UX-ADVERSARY "
        f"{adversary}/{adv_mode} run_id={adv_run_id} ===\n"
    )
    adv_out.flush()
    adv_proc = subprocess.Popen(
        ["bash", "-lc", dispatcher._net_hygiene_prelude() + _eval_command(adversary, str(adv_pf))],
        stdout=adv_out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    evaluator_results: dict[str, dict | None] = {}
    for ev, (proc, out, out_path) in procs.items():
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        out.close()
        evaluator_results[ev] = _extract_json(out_path.read_text(errors="replace"))

    try:
        adv_proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        adv_proc.kill()
        try:
            adv_proc.wait(timeout=5)
        except Exception:
            pass
    adv_out.close()
    adversarial_result = _extract_json(adv_out_path.read_text(errors="replace"))

    agg = aggregate_panel(evaluator_results, adversarial_result, len(evaluators))
    # Corroborated-consensus set, computed once: the arm labels below are relative to what the
    # PANEL accepted, not to what any single arm claimed.
    accepted_keys = {finding_key(f) for f in (agg.get("findings") or [])}
    panel_found_anything = bool(accepted_keys)

    for ev in evaluators:
        parsed = evaluator_results.get(ev) or {}
        scores = parsed.get("scores") or {}
        findings = parsed.get("findings") or []
        try:
            overall = float(parsed.get("overall", 0))
        except (TypeError, ValueError):
            overall = 0.0
        feedback.record_evaluation(
            review_id,
            app,
            ev,
            overall,
            verdict={"scores": scores, "n_findings": len(findings), "findings": findings},
        )
        # An evaluation is a SCORE; only an outcome reaches the learner. Without this the panel's
        # arms are invisible to route weights no matter how many times it runs.
        verdict_label, durability, note = panel_arm_outcome(
            evaluator_results.get(ev), findings, accepted_keys, panel_found_anything
        )
        try:
            feedback.record_outcome(
                f"{review_id}:eval:{ev}",
                verifier_verdict=verdict_label,
                adjudicated_verdict=verdict_label,
                durability=durability,
                notes=note,
            )
        except Exception as exc:  # a learner write must never lose a completed panel
            print(f"warn: ux_review outcome failed for {ev}: {exc}", file=sys.stderr)
        for gap in _normalize_evidence_gaps(parsed):
            feedback.record_evidence_gap(review_id, ev, gap)

    for gap in _normalize_evidence_gaps(adversarial_result):
        feedback.record_evidence_gap(review_id, adversary, gap)

    # Fallback ONLY when the panel + aggregation surfaced no findings (e.g. a clean app, or all
    # findings uncorroborated). The primary path is the real evaluator findings via aggregate_panel.
    if not agg.get("findings"):
        derived = derive_findings(bundle, agg.get("dimension_medians") or {})
        if derived:
            agg["derived_findings"] = derived
            agg["findings"] = derived
            feedback.record_evaluation(
                review_id,
                app,
                "_panel_derived",
                float(agg.get("overall_median") or 0.0),
                verdict={"findings": derived, "source": "derived"},
            )
    return {
        "review_id": review_id,
        "app": app,
        **agg,
    }


def cross_repo_patterns(window_days: int = 120, min_recurrence: int = 2) -> list[dict]:
    """Read-only SQL over feedback evaluations to surface recurring high-severity UX finding categories."""
    since = int(time.time()) - window_days * 86400
    patterns: dict[str, dict] = defaultdict(
        lambda: {"apps": set(), "examples": [], "max_severity": 0, "count": 0},
    )
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT implementer, experiment_id, verdict FROM evaluations "
            "WHERE ts>=? AND experiment_id LIKE '%:uxreview:%'",
            (since,),
        ).fetchall()
    for implementer, _exp_id, verdict_json in rows:
        if not verdict_json:
            continue
        try:
            verdict = json.loads(verdict_json)
        except Exception:
            continue
        for f in verdict.get("findings") or []:
            sev = int(f.get("severity") or 0)
            if sev < 3:
                continue
            fm = str(f.get("failure_mode") or "unknown")
            entry = patterns[fm]
            entry["apps"].add(implementer)
            entry["count"] += 1
            entry["max_severity"] = max(entry["max_severity"], sev)
            if len(entry["examples"]) < 5:
                entry["examples"].append(
                    {
                        "app": implementer,
                        "screen": f.get("screen"),
                        "element": f.get("element"),
                        "severity": sev,
                    }
                )
    out: list[dict] = []
    for failure_mode, data in sorted(patterns.items(), key=lambda kv: (-len(kv[1]["apps"]), kv[0])):
        if len(data["apps"]) < min_recurrence:
            continue
        out.append(
            {
                "failure_mode": failure_mode,
                "category": failure_mode,
                "app_count": len(data["apps"]),
                "apps": sorted(data["apps"]),
                "occurrences": data["count"],
                "max_severity": data["max_severity"],
                "examples": data["examples"],
            }
        )
    return out


def calibrate(review_id: str, human_verdict: str, note: str | None = None) -> None:
    """DEPRECATED / DO NOT REWIRE (2026-07-08). This was a weekly human spot-check anchoring the
    panel to owner taste. No human code/UX-quality review gate is available in this deployment (see LOCAL_POLICY.md and the project
    CLAUDE.md human-involvement rule); calibration is now ZERO-OWNER via machine ground-truth
    (objective_anchor.py) + consensus (judge_reliability.py). This stub is kept ONLY to make the
    forbidden path explicit — do not wire it into any cadence or gate. Use objective_anchor."""
    raise NotImplementedError(
        "ux_review.calibrate is retired: owner-code-review calibration is forbidden; "
        "use objective_anchor.py (machine ground truth) — see Orchestrator/CLAUDE.md"
    )


def gate_decision(gate1_verdict: dict, gate2_report: dict, min_overall: float = 7.0) -> dict:
    """Pure gate: done only when Gate 1 passed, overall >= min, and no blockers (selftested)."""
    reasons: list[str] = []
    done = True

    gate1_ok = gate1_verdict.get("ok") if isinstance(gate1_verdict, dict) else False
    if not gate1_ok:
        done = False
        reasons.append("gate1_not_ok")

    overall = float(gate2_report.get("overall_median") or 0)
    if overall < min_overall:
        done = False
        reasons.append(f"overall_median_below_{min_overall}")

    blockers = [
        f
        for f in (gate2_report.get("findings") or gate2_report.get("blockers") or [])
        if int(f.get("severity") or 0) >= 4
    ]
    if blockers:
        done = False
        reasons.append("blockers_present")

    return {"done": done, "reasons": reasons}


def synthesize_improvements(report: dict) -> dict:
    """Mine the panel for actionable improvement ideas + coverage gaps.

    The score and top-line findings answer "is it good"; this answers "how do I make it
    better". It surfaces every distinct per-evaluator ``fix_hint`` behind each corroborated
    finding (preserved by ``_merge_finding_group``), ranked by severity x corroboration, plus
    the unioned ``evidence_gaps`` as the coverage to drive on the next pass. Mining the full
    per-evaluator output is thus a single call, not a manual re-read of rubric files. Pure.
    """
    improvements: list[dict] = []
    for f in report.get("findings") or []:
        hints = list(f.get("fix_hints") or [])
        if not hints and f.get("fix_hint"):
            hints = [str(f["fix_hint"])]
        improvements.append(
            {
                "dimension": f.get("dimension"),
                "failure_mode": f.get("failure_mode"),
                "severity": int(f.get("severity") or 0),
                "corroboration": int(f.get("corroboration") or 0),
                "element": f.get("element"),
                "fix_hints": hints,
            }
        )
    improvements.sort(
        key=lambda x: (x["severity"] * max(x["corroboration"], 1), x["severity"]),
        reverse=True,
    )
    return {
        "improvements": improvements,
        "coverage_gaps": list(report.get("evidence_gaps") or []),
        "n_findings": len(improvements),
        "n_with_hints": sum(1 for i in improvements if i["fix_hints"]),
    }


def _sample_bundle() -> dict:
    return {
        "app": "stranske/Trend_Model_Project",
        "review_id": "stranske/Trend_Model_Project:uxreview:2026-06-22",
        "url": "http://localhost:8600/",
        "wired": {"ok": True, "findings": [{"target": "Run Demo", "pass": True}]},
        "screens": [{"name": "Home", "a11y": "button Run Demo", "notes": ""}],
        "scenarios": [
            {
                "name": "Run the demo",
                "steps": [
                    {"action": "open Home", "observed": "Home screen visible"},
                    {"action": "click Run Demo", "observed": "results table appears"},
                ],
                "goal": "see results",
            }
        ],
    }


def _selftest_panel_backfill() -> None:
    """Historical panels register from what is on disk, and never from what is convenient."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # A real panel: one rubric shared by every arm, one retried seat, one failed attempt.
        good = root / "stranske" / "Demo-App_uxreview_2026-06-01"
        good.mkdir(parents=True)
        for agent in ("claude", "codex", "vibe", "vibe-retry1"):
            (good / f"rubric-prompt-{agent}.txt").write_text("IDENTICAL RUBRIC")
            (good / f"rubric-out-{agent}.txt").write_text("scores")
        (good / "rubric-out-vibe.FAILED-http520.txt").write_text("boom")
        # A smoke fixture, and a panel whose arms were asked DIFFERENT questions.
        smoke = root / "stranske" / "_gate2_smoke_uxreview_2026-06-01"
        smoke.mkdir(parents=True)
        (smoke / "rubric-prompt-codex.txt").write_text("SMOKE")
        (smoke / "rubric-out-codex.txt").write_text("ok")
        split = root / "stranske" / "Split-App_uxreview_2026-06-02"
        split.mkdir(parents=True)
        (split / "rubric-prompt-codex.txt").write_text("QUESTION A")
        (split / "rubric-prompt-claude.txt").write_text("QUESTION B - DIFFERENT")
        (split / "rubric-out-codex.txt").write_text("ok")
        (split / "rubric-out-claude.txt").write_text("ok")

        panels = discover_historical_panels(root)
        ids = {panel["review_id"] for panel in panels}
        assert "_gate2_smoke_uxreview_2026-06-01" not in ids, "a smoke fixture is not evidence"
        panel = next(p for p in panels if p["review_id"].startswith("Demo-App"))
        assert panel["app"] == "stranske/Demo-App", panel["app"]
        # ONE AGENT IS ONE ARM: retry and failure decoration collapse, the failure is dropped.
        assert panel["arms"] == ["claude", "codex", "vibe"], panel["arms"]
        assert panel["base_sha_unrecoverable"] is True, "must never borrow today's HEAD"

        result = backfill_panel_subjects(root, apply=False)
        assert result["applied"] is False
        skipped = {row["review_id"]: row["reason"] for row in result["skipped"]}
        assert "Split-App_uxreview_2026-06-02" in skipped, result["skipped"]
        assert "differs" in skipped["Split-App_uxreview_2026-06-02"], skipped
        # Blocking AND drainable quantity: a registered subject with no base commit is still
        # not minable, so both counts are reported or neither is meaningful.
        assert "with_base_sha" in result and "without_base_sha" in result, result

    # A historical bundle must not inherit the current checkout's commit.
    assert (
        resolve_panel_base_sha({"app": "stranske/Workflows", "base_sha_unrecoverable": True})
        is None
    )
    assert resolve_panel_base_sha({"app": "x/y", "base_sha": "abc123"}) == "abc123"


def _selftest() -> None:
    _selftest_panel_backfill()
    bundle = _sample_bundle()

    rubric = build_rubric_prompt(bundle)
    for dim in DIMENSIONS:
        assert dim in rubric, dim
    assert "workflow_productivity" in rubric
    assert '"scores":{"wired":0-10' in rubric or '"scores":{"wired":0-10,' in rubric.replace(
        "\n", ""
    )
    assert "HARD RULES" in rubric and "feels clunky" in rubric
    assert "severity scale 0=none" in rubric
    assert "STRICT JSON only" in rubric
    assert "confidence" in rubric and "failure_mode" in rubric
    assert "Never infer behavior not present in observed" in rubric
    assert "stranske/Trend_Model_Project" in rubric
    assert '"observed"' in rubric

    adv = build_adversarial_prompt(bundle)
    assert "hostile, novice first-time user who WANTS to fail" in adv
    assert "stuck_probability" in adv
    assert "worst_case" in adv
    assert (
        '"dimension":"adversarial"' in adv.replace("\n", "") or '"dimension":"adversarial"' in adv
    )

    assert median([1, 2, 3, 4, 100]) == 3.0
    assert median([1, 2, 3, 4]) == 2.5
    assert median([]) == 0.0
    assert consensus_flag([5, 6, 7]) is False
    assert consensus_flag([5, 6, 10]) is True

    f1 = {
        "screen": "Home",
        "element": "Run",
        "failure_mode": "confusion",
        "severity": 2,
        "confidence": 0.6,
        "click_path": ["a"],
    }
    f2 = {
        "screen": "Home",
        "element": "Run",
        "failure_mode": "confusion",
        "severity": 4,
        "confidence": 0.8,
        "click_path": ["a"],
    }
    f3 = {
        "screen": "Home",
        "element": "Help",
        "failure_mode": "missing_help",
        "severity": 3,
        "confidence": 0.5,
        "click_path": ["b"],
    }
    deduped = dedupe_findings([f1, f2, f3])
    assert deduped[0]["severity"] == 4 and deduped[0]["confidence"] == 0.8
    assert len(deduped) == 2

    ev_findings = {
        "claude": [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 2,
                "click_path": ["x"],
                "confidence": 0.5,
            }
        ],
        "codex": [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 3,
                "click_path": ["x"],
                "confidence": 0.6,
            }
        ],
        "cursor": [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 2,
                "click_path": ["x"],
                "confidence": 0.4,
            }
        ],
        "gemini": [],
    }
    accepted, non = aggregate_accepted_findings(ev_findings, [], n_evaluators=4)
    assert len(accepted) == 1 and accepted[0]["severity"] == 3

    # fix_hints are collected across the whole cluster (not just the first member) and
    # surfaced by synthesize_improvements, with evidence_gaps carried as coverage_gaps.
    fh_findings = {
        "claude": [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 3,
                "click_path": ["x"],
                "fix_hint": "hide empty state",
            }
        ],
        "codex": [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 2,
                "click_path": ["x"],
                "fix_hint": "show completed-state line",
            }
        ],
        "cursor": [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 2,
                "click_path": ["x"],
                "fix_hint": "hide empty state",
            }
        ],
        "gemini": [],
    }
    fh_acc, _ = aggregate_accepted_findings(fh_findings, [], n_evaluators=4)
    assert fh_acc and set(fh_acc[0]["fix_hints"]) == {
        "hide empty state",
        "show completed-state line",
    }
    syn = synthesize_improvements({"findings": fh_acc, "evidence_gaps": ["tabs not driven"]})
    assert syn["n_findings"] == 1 and syn["n_with_hints"] == 1
    assert syn["coverage_gaps"] == ["tabs not driven"]

    adv_only, _ = aggregate_accepted_findings(
        {"claude": [], "codex": [], "cursor": [], "gemini": []},
        [
            {
                "screen": "H",
                "element": "E",
                "failure_mode": "confusion",
                "severity": 3,
                "click_path": ["x"],
                "stuck_probability": 0.7,
                "dimension": "adversarial",
            }
        ],
        n_evaluators=4,
    )
    assert len(adv_only) == 1

    rejected, non2 = aggregate_accepted_findings(
        {"claude": [{"screen": "H", "element": "E", "failure_mode": "confusion", "severity": 2}]},
        [],
        n_evaluators=4,
    )
    assert rejected == [] and len(non2) == 1 and non2[0]["reject_reason"] == "missing_click_path"

    assert compute_overall_median([8.0, 8.5, 9.0], []) == 8.5
    assert compute_overall_median([8.0, 8.5, 9.0], [{"severity": 4}]) == 3.0

    agg = aggregate_panel(
        {
            "claude": {
                "scores": {
                    "wired": 9,
                    "usability": 7,
                    "help_clarity": 8,
                    "workflow_productivity": 8,
                },
                "overall": 8,
                "findings": [],
                "evidence_gaps": ["missing tooltip text"],
            },
            "codex": {
                "scores": {
                    "wired": 9,
                    "usability": 4,
                    "help_clarity": 8,
                    "workflow_productivity": 8,
                },
                "overall": 7,
                "findings": [],
                "evidence_gaps": [],
            },
            "cursor": {
                "scores": {
                    "wired": 9,
                    "usability": 7,
                    "help_clarity": 8,
                    "workflow_productivity": 8,
                },
                "overall": 8,
                "findings": [],
                "evidence_gaps": [],
            },
            "gemini": {
                "scores": {
                    "wired": 9,
                    "usability": 7,
                    "help_clarity": 8,
                    "workflow_productivity": 8,
                },
                "overall": 8,
                "findings": [],
                "evidence_gaps": [],
            },
        },
        {"worst_case": "Run Demo", "findings": []},
        n_evaluators=4,
    )
    assert agg["dimension_medians"]["usability"] == 7.0
    assert agg["consensus_flags"]["usability"] is True
    assert "missing tooltip text" in agg["evidence_gaps"]

    g1_ok = {"ok": True}
    g1_bad = {"ok": False}
    rep_pass = {"overall_median": 8.0, "findings": [{"severity": 2}]}
    rep_low = {"overall_median": 6.0, "findings": []}
    rep_block = {"overall_median": 8.0, "findings": [{"severity": 4}]}
    assert gate_decision(g1_ok, rep_pass)["done"] is True
    assert gate_decision(g1_bad, rep_pass)["done"] is False
    assert gate_decision(g1_ok, rep_low)["done"] is False
    assert gate_decision(g1_ok, rep_block)["done"] is False
    assert "blockers_present" in gate_decision(g1_ok, rep_block)["reasons"]

    # derive_findings: grounded synthesis for low dimensions; no evidence -> no finding (never fabricate)
    assert (
        severity_from_median(2) == 3
        and severity_from_median(5) == 2
        and severity_from_median(8) == 0
    )
    _db = {
        "screens": [{"name": "Home"}],
        "wired": {"findings": [{"interaction": "Run", "passed": False, "note": "dead-ends"}]},
        "scenarios": [
            {
                "name": "Run it",
                "goal": "see results",
                "goal_achieved": False,
                "steps": [{"action": "click Run", "observed": "error, no results"}],
            }
        ],
        "help_surfaces": "no tooltips; cryptic error",
    }
    _der = derive_findings(
        _db, {"wired": 5, "usability": 2, "help_clarity": 1, "workflow_productivity": 3}
    )
    assert {"wired", "usability", "help_clarity", "workflow_productivity"} <= {
        f["dimension"] for f in _der
    }
    assert all(f["source"] == "derived" and f.get("click_path") and f.get("actual") for f in _der)
    assert (
        derive_findings({"screens": [{"name": "X"}]}, {"usability": 2}) == []
    )  # no evidence -> no finding
    assert derive_findings(_db, {"usability": 9}) == []  # above threshold -> none

    print(
        "ux_review.py selftest: OK (rubric+adversarial prompts, median, consensus_flag, finding "
        "dedupe+acceptance+non_findings, blocker-capped overall, gate_decision, evidence-gap passthrough, "
        "fix_hint collection + improvement synthesis, derive_findings grounded synthesis)"
    )


ARM_RETRY_RE = re.compile(
    r"^(?P<agent>[a-z0-9]+)(?:[-.](?:retry\d*|attempt\d*|FAILED.*))?$", re.IGNORECASE
)


def _arm_agent(raw: str) -> str:
    """The agent behind an on-disk arm filename, with retry/failure decoration removed."""
    match = ARM_RETRY_RE.match(raw.strip())
    return (match.group("agent") if match else raw.strip()).lower()


PANEL_DIR_RE = re.compile(r"^(?P<app>.+)_uxreview_(?P<date>\d{4}-\d{2}-\d{2})(?P<suffix>.*)$")


def discover_historical_panels(root: Path | None = None) -> list[dict]:
    """Every panel already on disk, as a bundle the live registrar can consume.

    Reads only what the panel actually recorded. The rubric prompt IS the spec -- byte-identical
    across a panel's arms, which is what makes one spec hash per panel correct -- and the arm set is
    the agents that actually produced output, never the agents that were asked.

    `base_sha_unrecoverable` is set on every panel, because none of them captured the commit under
    review and inferring one from today's checkout would fuse two states of the same app into one
    subject. A panel whose evidence DOES name a commit gets it; see `--base-sha`.
    """
    root = Path(root or REVIEW_DIR)
    if not root.is_dir():
        return []
    panels: list[dict] = []
    for prompt in sorted(root.rglob("rubric-prompt-*.txt")):
        directory = prompt.parent
        match = PANEL_DIR_RE.match(directory.name)
        if not match:
            continue
        # The app is owner/name when the panel sits under an owner directory, and a bare local
        # name otherwise -- `local-Reader` is a tool, not a repo, and must not be given a slash.
        owner = directory.parent.name if directory.parent != root else ""
        app_part = match.group("app")
        app = f"{owner}/{app_part}" if owner else app_part
        review_id = directory.name
        if any(existing["review_id"] == review_id for existing in panels):
            continue
        # A SMOKE FIXTURE IS NOT EVIDENCE. `_gate2_smoke` is an underscore-prefixed harness run
        # (2.5KB rubric against a real panel's 8.7KB) that exists to prove the pipeline executes.
        # Registering it as a research subject would put a self-test into the population the miner
        # learns from, which is worse than leaving it out -- the learner cannot tell a rehearsal
        # from a review.
        if app_part.startswith("_"):
            continue
        specs = {
            candidate.read_bytes() for candidate in sorted(directory.glob("rubric-prompt-*.txt"))
        }
        # ONE AGENT IS ONE ARM, however many times it was asked. On disk a retried seat leaves
        # `rubric-out-vibe.txt`, `rubric-out-vibe-retry1.txt` AND
        # `rubric-out-vibe.FAILED-http520.txt`; taken literally that is three arms, which would
        # manufacture independence the evidence does not have and treble that seat's weight in
        # every comparison drawn from the subject. A `.FAILED-*` attempt produced no usable output
        # and is not an arm at all.
        arms = sorted(
            {
                _arm_agent(out.name[len("rubric-out-") : -len(".txt")])
                for out in directory.glob("rubric-out-*.txt")
                if out.stat().st_size > 0 and ".FAILED-" not in out.name
            }
        )
        panels.append(
            {
                "app": app,
                "review_id": review_id,
                "date": match.group("date"),
                "spec": prompt.read_text(errors="ignore"),
                "spec_variants": len(specs),
                "arms": arms,
                "path": str(directory),
                "base_sha_unrecoverable": True,
            }
        )
    return panels


def backfill_panel_subjects(root: Path | None = None, *, apply: bool = False, conn=None) -> dict:
    """Register the panels already on disk as research subjects. Idempotent; dry-run by default.

    WHY THIS IS NOT A FABRICATION. Every field comes off disk: the target is the app directory, the
    spec is the panel's own rubric bytes, the arm set is the agents that actually returned output.
    Nothing is imputed. The one field that CANNOT be recovered -- the commit the app was running --
    is left null rather than borrowed from today's checkout, so these subjects are honestly
    identified but most of them stay non-minable, and this reports exactly how many.

    A panel whose rubric prompts are not byte-identical across arms is SKIPPED, not merged: a
    differing spec means the arms were not asked the same question, so one spec hash would claim a
    comparison the evidence does not support.
    """
    panels = discover_historical_panels(root)
    registered: list[dict] = []
    skipped: list[dict] = []
    for panel in panels:
        if not panel["arms"]:
            skipped.append({"review_id": panel["review_id"], "reason": "no arm produced output"})
            continue
        if panel["spec_variants"] != 1:
            skipped.append(
                {
                    "review_id": panel["review_id"],
                    "reason": f"rubric differs across arms ({panel['spec_variants']} variants)",
                }
            )
            continue
        if not apply:
            registered.append(
                {
                    "review_id": panel["review_id"],
                    "app": panel["app"],
                    "arms": panel["arms"],
                    "would_register": True,
                }
            )
            continue
        identity = register_panel_subject(panel, panel["arms"], spec=panel["spec"], conn=conn)
        if identity is None:
            skipped.append({"review_id": panel["review_id"], "reason": "registration failed"})
            continue
        registered.append(
            {
                "review_id": panel["review_id"],
                "app": panel["app"],
                "arms": panel["arms"],
                "subject_id": identity["subject_id"],
                "base_sha": identity.get("base_sha"),
            }
        )
    minable = [row for row in registered if row.get("base_sha")]
    return {
        "applied": apply,
        "panels_found": len(panels),
        "registered": len(registered),
        "skipped": skipped,
        # BLOCKING AND DRAINABLE QUANTITY IN ONE PLACE. "26 subjects registered" would read as
        # "mining unblocked"; it is not, because a repo-scoped subject with no base commit still
        # cannot produce an acceptable completion event. Say both numbers or say neither.
        "with_base_sha": len(minable),
        "without_base_sha": len(registered) - len(minable),
        "detail": registered,
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="Run offline selftest")
    parser.add_argument("--bundle", help="Path to UX candidate bundle JSON")
    parser.add_argument("--evaluators", help="Comma-separated evaluator agents")
    parser.add_argument("--adversary", default="claude", help="Adversarial critic agent")
    parser.add_argument("--timeout", type=int, default=1500, help="Per-agent timeout seconds")
    parser.add_argument(
        "--backfill-panels",
        action="store_true",
        help="register panels already on disk as research subjects (dry-run)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="with --backfill-panels, actually write the subjects"
    )
    args = parser.parse_args(argv)
    if args.backfill_panels:
        print(json.dumps(backfill_panel_subjects(apply=args.apply), indent=2, default=str))
        return 0
    if args.selftest:
        _selftest()
        return 0
    if not args.bundle:
        print(__doc__)
        return 2
    bundle = json.loads(Path(args.bundle).read_text())
    evs = args.evaluators.split(",") if args.evaluators else None
    print(
        json.dumps(
            review(bundle, evaluators=evs, adversary=args.adversary, timeout=args.timeout),
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
