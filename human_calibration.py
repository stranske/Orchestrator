#!/usr/bin/env python3
"""human_calibration.py - data-gated proxy-score calibration.

The feedback store can hold sparse human ground-truth anchors. This module keeps
the calibration path explicit without applying bias correction before the data
supports it: parse structured human score anchors, join them to evaluator proxy
scores, and fit a small linear correction only after enough matched pairs exist.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import feedback

DEFAULT_WINDOW_DAYS = 365
DEFAULT_MIN_PAIRS = 5


class CalibrationInputError(ValueError):
    """Raised when a human calibration anchor is malformed."""


def _score(value) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0 or score > feedback.QUALITY_MAX:
        return None
    return score


def _clamp_score(value: float) -> float:
    return max(0.0, min(feedback.QUALITY_MAX, value))


def build_anchor_payload(
    *,
    experiment_id: str,
    implementer: str,
    score,
    note: str | None = None,
    arm_id: str | None = None,
    member_id: str | None = None,
    profile_id: str | None = None,
    agent: str | None = None,
) -> dict:
    """Return an exact anchor payload while retaining legacy agent identity."""
    exp = str(experiment_id or "").strip()
    impl = str(implementer or "").strip()
    parsed_score = _score(score)
    if not exp:
        raise CalibrationInputError("experiment_id is required")
    if not impl:
        raise CalibrationInputError("implementer is required")
    if parsed_score is None:
        raise CalibrationInputError(f"score must be between 0 and {feedback.QUALITY_MAX:g}")
    if member_id and str(member_id) != impl:
        raise CalibrationInputError("member_id must match exact implementer identity")
    ref = f"{exp}:{impl}"
    verdict_dict = {
        "experiment_id": exp,
        "implementer": impl,
        "score": parsed_score,
    }

    # Add arm-aware metadata when available
    if arm_id:
        verdict_dict["arm_id"] = arm_id
    if member_id:
        verdict_dict["member_id"] = member_id
    if profile_id:
        verdict_dict["profile_id"] = profile_id
    if agent:
        verdict_dict["agent"] = agent

    verdict = json.dumps(verdict_dict, sort_keys=True, separators=(",", ":"))

    result = {
        "ref": ref,
        "human_verdict": verdict,
        "note": note,
        "experiment_id": exp,
        "implementer": impl,
        "score": parsed_score,
    }

    # Add arm-aware metadata to result as well
    if arm_id:
        result["arm_id"] = arm_id
    if member_id:
        result["member_id"] = member_id
    if profile_id:
        result["profile_id"] = profile_id
    if agent:
        result["agent"] = agent

    return result


def record_anchor(
    *,
    experiment_id: str,
    implementer: str,
    score,
    note: str | None = None,
    apply: bool = False,
    confirm_anchor: str | None = None,
    arm_id: str | None = None,
    member_id: str | None = None,
    profile_id: str | None = None,
    agent: str | None = None,
    record_func=feedback.record_human_calibration,
) -> dict:
    """Preview or record one structured human calibration score anchor."""
    payload = build_anchor_payload(
        experiment_id=experiment_id,
        implementer=implementer,
        score=score,
        note=note,
        arm_id=arm_id,
        member_id=member_id,
        profile_id=profile_id,
        agent=agent,
    )
    expected_confirmation = payload["ref"]
    if apply and confirm_anchor != expected_confirmation:
        raise CalibrationInputError(f"--apply requires --confirm-anchor {expected_confirmation!r}")
    if apply:
        record_func(payload["ref"], payload["human_verdict"], payload["note"])
    return {
        "status": "recorded" if apply else "dry_run",
        "applied": bool(apply),
        "required_confirmation": expected_confirmation,
        "row": {
            "ref": payload["ref"],
            "human_verdict": payload["human_verdict"],
            "note": payload["note"],
        },
        "parsed_anchor": {
            "experiment_id": payload["experiment_id"],
            "implementer": payload["implementer"],
            "score": payload["score"],
            **({"arm_id": payload["arm_id"]} if payload.get("arm_id") else {}),
            **({"member_id": payload["member_id"]} if payload.get("member_id") else {}),
            **({"profile_id": payload["profile_id"]} if payload.get("profile_id") else {}),
            **({"agent": payload["agent"]} if payload.get("agent") else {}),
        },
    }


def parse_human_anchors(rows) -> list[dict]:
    """Return structured human score anchors from human_calibration rows.

    Supported compact forms match judge_reliability.py:
    - ref="exp_id:implementer", human_verdict="8.5"
    - human_verdict='{"experiment_id":"exp_id","implementer":"agent","score":8.5}'
    - ref="exp_id", human_verdict='{"scores":{"agent":8.5}}'
    """
    anchors: list[dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            ts = row.get("ts")
            ref = row.get("ref")
            verdict = row.get("human_verdict")
            note = row.get("note")
        else:
            ts = row[0] if len(row) > 0 else None
            ref = row[1] if len(row) > 1 else None
            verdict = row[2] if len(row) > 2 else None
            note = row[3] if len(row) > 3 else None
        if not ref or verdict is None:
            continue
        parsed = None
        if isinstance(verdict, str):
            try:
                parsed = json.loads(verdict)
            except json.JSONDecodeError:
                parsed = None
        elif isinstance(verdict, dict):
            parsed = verdict

        if isinstance(parsed, dict):
            exp = parsed.get("experiment_id")
            impl = parsed.get("implementer")
            score = _score(parsed.get("score"))
            if exp and impl and score is not None:
                anchor = {
                    "ts": ts,
                    "ref": ref,
                    "experiment_id": str(exp),
                    "implementer": str(impl),
                    "score": score,
                    "note": note,
                }
                for key in ("arm_id", "member_id", "profile_id", "agent"):
                    if parsed.get(key):
                        anchor[key] = str(parsed[key])
                anchors.append(anchor)
            scores = parsed.get("scores")
            if isinstance(scores, dict):
                for impl, raw_score in scores.items():
                    score = _score(raw_score)
                    if score is not None:
                        anchors.append(
                            {
                                "ts": ts,
                                "ref": ref,
                                "experiment_id": str(ref),
                                "implementer": str(impl),
                                "score": score,
                                "note": note,
                            }
                        )
            continue

        score = _score(verdict)
        if score is None or ":" not in str(ref):
            continue
        exp, impl = str(ref).rsplit(":", 1)
        if exp and impl:
            anchors.append(
                {
                    "ts": ts,
                    "ref": ref,
                    "experiment_id": exp,
                    "implementer": impl,
                    "score": score,
                    "note": note,
                }
            )
    return anchors


def _evaluation_scores(rows) -> dict[tuple[str, str], list[float]]:
    scores: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows or []:
        if isinstance(row, dict):
            exp = row.get("experiment_id")
            impl = row.get("implementer")
            score = _score(row.get("score"))
        else:
            exp = row[0] if len(row) > 0 else None
            impl = row[1] if len(row) > 1 else None
            score = _score(row[3] if len(row) > 3 else None)
        if exp and impl and score is not None:
            scores[(str(exp), str(impl))].append(score)
    return scores


def _fit_linear(pairs: list[dict]) -> dict:
    xs = [float(row["proxy_score"]) for row in pairs]
    ys = [float(row["human_score"]) for row in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    raw_errors = [abs(x - y) for x, y in zip(xs, ys)]
    calibrated = [_clamp_score(intercept + slope * x) for x in xs]
    calibrated_errors = [abs(y_hat - y) for y_hat, y in zip(calibrated, ys)]
    return {
        "intercept": round(intercept, 6),
        "slope": round(slope, 6),
        "raw_mean_abs_error": round(sum(raw_errors) / len(raw_errors), 6),
        "calibrated_mean_abs_error": round(sum(calibrated_errors) / len(calibrated_errors), 6),
        "mean_proxy_score": round(mean_x, 6),
        "mean_human_score": round(mean_y, 6),
    }


def calibrate_score(score: float, model: dict | None) -> float:
    if not model or not model.get("ready"):
        return score
    return _clamp_score(float(model["intercept"]) + float(model["slope"]) * score)


def compute(
    evaluation_rows,
    human_rows,
    *,
    min_pairs: int = DEFAULT_MIN_PAIRS,
    generated_at: int | None = None,
) -> dict:
    anchors = parse_human_anchors(human_rows)
    anchor_scores: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in anchors:
        anchor_scores[(row["experiment_id"], row["implementer"])].append(row["score"])

    eval_scores = _evaluation_scores(evaluation_rows)
    pairs = []
    unpaired = []
    for key, human_scores in sorted(anchor_scores.items()):
        proxy_scores = eval_scores.get(key) or []
        if not proxy_scores:
            unpaired.append({"experiment_id": key[0], "implementer": key[1]})
            continue
        pairs.append(
            {
                "experiment_id": key[0],
                "implementer": key[1],
                "human_score": round(sum(human_scores) / len(human_scores), 6),
                "proxy_score": round(sum(proxy_scores) / len(proxy_scores), 6),
                "human_anchor_count": len(human_scores),
                "proxy_score_count": len(proxy_scores),
            }
        )

    ready = False
    model = None
    if not anchors:
        status = "no_human_anchors"
        recommendation = (
            "Record structured human_calibration score anchors before fitting calibration."
        )
    elif not pairs:
        status = "no_matched_proxy_scores"
        recommendation = (
            "Human anchors exist, but none match evaluation experiment/implementer pairs."
        )
    elif len(pairs) < min_pairs:
        status = "insufficient_pairs"
        recommendation = (
            f"Need at least {min_pairs} matched human/proxy score pairs; "
            f"{len(pairs)} available."
        )
    elif len({row["proxy_score"] for row in pairs}) < 2:
        status = "insufficient_score_variance"
        recommendation = "Need at least two distinct proxy-score values to fit a regression."
    else:
        ready = True
        status = "ready"
        model = _fit_linear(pairs)
        model["ready"] = True
        recommendation = (
            "Calibration regression is ready for controlled downstream use; compare raw vs calibrated "
            "proxy decisions before applying it to learning."
        )

    raw_mae = None
    if pairs:
        raw_mae = sum(
            abs(float(row["proxy_score"]) - float(row["human_score"])) for row in pairs
        ) / len(pairs)
    return {
        "generated_at": generated_at or int(time.time()),
        "method": "linear_proxy_score_to_human_score_regression",
        "quality_max": feedback.QUALITY_MAX,
        "min_pairs": min_pairs,
        "status": status,
        "ready": ready,
        "human_anchor_rows": len(human_rows or []),
        "structured_anchor_count": len(anchors),
        "matched_pair_count": len(pairs),
        "unmatched_anchor_count": len(unpaired),
        "raw_mean_abs_error": round(raw_mae, 6) if raw_mae is not None else None,
        "model": model,
        "pairs": pairs[:25],
        "unmatched_anchors": unpaired[:25],
        "recommendation": recommendation,
    }


def summarize(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_pairs: int = DEFAULT_MIN_PAIRS,
) -> dict:
    since = int(time.time()) - window_days * 86400
    with feedback._conn() as c:
        eval_rows = _exact_evaluation_rows(c, since)
        human_rows = c.execute(
            "SELECT ts, ref, human_verdict, note FROM human_calibration WHERE ts>=?",
            (since,),
        ).fetchall()
    report = compute(eval_rows, human_rows, min_pairs=min_pairs)
    report["window_days"] = window_days
    return report


def _exact_evaluation_rows(c, since: int) -> list[tuple]:
    """Prefer exact v2 rows and suppress their agent-level dual-write projection."""
    v2_rows = c.execute(
        "SELECT experiment_id, implementer_member_id, evaluator_id, score, ts "
        "FROM evaluations_v2 WHERE ts>=?",
        (since,),
    ).fetchall()
    v2_experiments = sorted({str(row[0]) for row in v2_rows})
    if v2_experiments:
        placeholders = ",".join("?" for _ in v2_experiments)
        legacy_rows = c.execute(
            "SELECT experiment_id, implementer, evaluator, score, ts FROM evaluations "
            f"WHERE ts>=? AND experiment_id NOT IN ({placeholders})",
            (since, *v2_experiments),
        ).fetchall()
    else:
        legacy_rows = c.execute(
            "SELECT experiment_id, implementer, evaluator, score, ts FROM evaluations WHERE ts>=?",
            (since,),
        ).fetchall()
    return [*legacy_rows, *v2_rows]


def pending_queue(*, window_days: int = DEFAULT_WINDOW_DAYS, limit: int = 5) -> dict:
    """Report objective-anchor followup state without creating an owner scoring queue.

    Missing anchors are machine workflow state: ``exp_abcd followup`` and the
    objective/referee lane must produce them.  The guarded manual record CLI remains
    available for explicit forensic use, but reports never emit paste-ready commands.
    """
    now = int(time.time())
    since = now - window_days * 86400
    with feedback._conn() as c:
        eval_rows = _exact_evaluation_rows(c, since)
        anchor_rows = c.execute(
            "SELECT ts, ref, human_verdict, note FROM human_calibration",
        ).fetchall()
        target_rows = c.execute(
            "SELECT experiment_id, agent, target FROM runs "
            "WHERE experiment_id IS NOT NULL AND experiment_id != ''",
        ).fetchall()
    anchored = {(a["experiment_id"], a["implementer"]) for a in parse_human_anchors(anchor_rows)}
    targets = {(str(e), str(a)): str(t or "") for e, a, t in target_rows}
    pairs: dict[tuple[str, str], dict] = {}
    for exp, impl, evaluator, score, ts in eval_rows:
        parsed = _score(score)
        if not exp or not impl or evaluator is None or parsed is None:
            continue
        pair = pairs.setdefault((str(exp), str(impl)), {"scores": {}, "last_ts": 0})
        pair["scores"][str(evaluator)] = parsed
        pair["last_ts"] = max(pair["last_ts"], int(ts or 0))
    candidates = []
    for (exp, impl), pair in pairs.items():
        if (exp, impl) in anchored:
            continue
        vals = list(pair["scores"].values())
        spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        candidates.append(
            {
                "experiment_id": exp,
                "implementer": impl,
                "target": targets.get((exp, impl), ""),
                "judge_scores": pair["scores"],
                "judge_spread": round(spread, 2),
                "judges": len(vals),
                "last_eval_ts": pair["last_ts"],
            }
        )
    # Highest information first: multi-judge disagreement, then recency; single-judge pairs last.
    candidates.sort(
        key=lambda x: (-(1 if x["judges"] > 1 else 0), -x["judge_spread"], -x["last_eval_ts"])
    )
    items = []
    for cand in candidates[: max(0, int(limit))]:
        items.append(
            {
                **cand,
                "status": "objective_anchor_pending",
                "owner_action_required": False,
                "next_transition": (
                    "experiment followup collects objective/referee evidence or records an exact skip reason"
                ),
            }
        )
    return {
        "generated_at": now,
        "window_days": window_days,
        "pending_total": len(candidates),
        "anchored_pairs": len(anchored),
        "owner_action_required": False,
        "status": "objective_anchor_pending" if candidates else "complete_or_no_candidates",
        "next_transition": (
            "exp_abcd followup and objective_anchor produce anchors automatically"
            if candidates
            else "wait for evaluated experiment arms"
        ),
        "items": items,
    }


def _selftest() -> None:
    empty = compute([], [], generated_at=1)
    assert empty["status"] == "no_human_anchors" and empty["ready"] is False, empty

    eval_rows = []
    human_rows = []
    for idx, (proxy, human) in enumerate(
        [(9.0, 8.0), (7.0, 6.5), (5.0, 5.2), (3.0, 3.8), (1.0, 2.0)], start=1
    ):
        exp = f"exp{idx}"
        impl = "candidate"
        eval_rows.extend(
            [
                (exp, impl, "judge_a", proxy + 0.2, 1),
                (exp, impl, "judge_b", proxy - 0.2, 1),
            ]
        )
        human_rows.append((1, f"{exp}:{impl}", str(human), None))
    report = compute(eval_rows, human_rows, min_pairs=5, generated_at=1)
    assert report["ready"] is True and report["model"], report
    assert report["model"]["calibrated_mean_abs_error"] < report["raw_mean_abs_error"], report
    assert calibrate_score(9.0, report["model"]) < 9.0, report

    sparse = compute(eval_rows[:2], human_rows[:1], min_pairs=5, generated_at=1)
    assert sparse["status"] == "insufficient_pairs" and sparse["ready"] is False, sparse

    preview = record_anchor(
        experiment_id="exp-preview",
        implementer="codex",
        score=8.25,
        note="human accepted with small caveat",
    )
    assert preview["status"] == "dry_run" and preview["row"]["ref"] == "exp-preview:codex", preview
    parsed_preview = parse_human_anchors(
        [
            {
                "ts": 1,
                "ref": preview["row"]["ref"],
                "human_verdict": preview["row"]["human_verdict"],
                "note": preview["row"]["note"],
            }
        ]
    )
    assert parsed_preview[0]["score"] == 8.25, parsed_preview
    recorded = []
    applied = record_anchor(
        experiment_id="exp-preview",
        implementer="codex",
        score=8.25,
        apply=True,
        confirm_anchor="exp-preview:codex",
        record_func=lambda ref, verdict, note=None: recorded.append((ref, verdict, note)),
    )
    assert applied["status"] == "recorded" and len(recorded) == 1, (applied, recorded)
    try:
        record_anchor(
            experiment_id="exp-preview",
            implementer="codex",
            score=8.25,
            apply=True,
            confirm_anchor="wrong",
            record_func=lambda ref, verdict, note=None: recorded.append((ref, verdict, note)),
        )
    except CalibrationInputError:
        pass
    else:
        raise AssertionError("missing confirmation should block apply")
    # Queue (audit item 12): pending pairs ranked by disagreement; anchored pairs excluded;
    # single-judge pairs sort last; commands are paste-ready with the confirm token.
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="human-calibration-selftest-"))
    old_db = feedback.DB_PATH
    try:
        feedback.DB_PATH = tmp / "t.db"
        feedback.record_run("qr1", "o/r#1", "implement", "codex", experiment_id="QE1")
        feedback.record_run("qr2", "o/r#2", "implement", "cursor", experiment_id="QE2")
        feedback.record_run("qr3", "o/r#3", "implement", "vibe", experiment_id="QE3")
        # QE1/codex: big disagreement; QE2/cursor: small; QE3/vibe: single judge.
        feedback.record_evaluation("QE1", "codex", "judge_a", 9.0)
        feedback.record_evaluation("QE1", "codex", "judge_b", 3.0)
        feedback.record_evaluation("QE2", "cursor", "judge_a", 7.0)
        feedback.record_evaluation("QE2", "cursor", "judge_b", 6.0)
        feedback.record_evaluation("QE3", "vibe", "judge_a", 8.0)
        queue = pending_queue(limit=5)
        order = [(i["experiment_id"], i["implementer"]) for i in queue["items"]]
        assert order == [("QE1", "codex"), ("QE2", "cursor"), ("QE3", "vibe")], order
        assert queue["items"][0]["judge_spread"] == 6.0, queue["items"][0]
        assert queue["items"][0]["target"] == "o/r#1", queue["items"][0]
        assert queue["owner_action_required"] is False, queue
        assert all("command" not in item for item in queue["items"]), queue
        # Anchoring a pair removes it from the ask.
        record_anchor(
            experiment_id="QE1",
            implementer="codex",
            score=7.5,
            apply=True,
            confirm_anchor="QE1:codex",
        )
        queue2 = pending_queue(limit=5)
        order2 = [(i["experiment_id"], i["implementer"]) for i in queue2["items"]]
        assert ("QE1", "codex") not in order2 and queue2["anchored_pairs"] == 1, queue2
        assert queue2["pending_total"] == 2, queue2
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)

    print(
        "human_calibration.py selftest: OK "
        "(anchor parsing, readiness, regression, guarded record path, objective-anchor state)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize data-gated human calibration regression readiness."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--min-pairs", type=int, default=DEFAULT_MIN_PAIRS)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--record-anchor",
        action="store_true",
        help="Preview or record one structured human score anchor.",
    )
    parser.add_argument("--experiment-id", help="A/B experiment id for --record-anchor.")
    parser.add_argument("--implementer", help="Implementer/agent name for --record-anchor.")
    parser.add_argument("--score", type=float, help="Human score on the 0-10 quality scale.")
    parser.add_argument("--note", help="Optional human note to store with the anchor.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually insert the anchor. Default is dry-run preview.",
    )
    parser.add_argument(
        "--confirm-anchor",
        help="Required with --apply; must equal EXPERIMENT_ID:IMPLEMENTER.",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Show objective-anchor followup state; never emits owner scoring commands.",
    )
    parser.add_argument("--limit", type=int, default=5, help="Max queue items to show.")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.queue:
        queue = pending_queue(window_days=args.window_days, limit=args.limit)
        if args.as_json:
            print(json.dumps(queue, indent=2, default=str))
        else:
            print(
                f"human_calibration objective-anchor state: {len(queue['items'])} of {queue['pending_total']} pending "
                f"(window {queue['window_days']}d, anchored so far {queue['anchored_pairs']})"
            )
            for item in queue["items"]:
                scores = " ".join(f"{k}={v:g}" for k, v in sorted(item["judge_scores"].items()))
                print(
                    f"  {item['experiment_id']}:{item['implementer']} "
                    f"target={item['target'] or 'n/a'} judges[{scores}] spread={item['judge_spread']}"
                )
                print(f"    next={item['next_transition']}")
        return 0
    if args.record_anchor:
        try:
            result = record_anchor(
                experiment_id=args.experiment_id,
                implementer=args.implementer,
                score=args.score,
                note=args.note,
                apply=args.apply,
                confirm_anchor=args.confirm_anchor,
            )
        except CalibrationInputError as exc:
            print(f"human_calibration: error: {exc}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(
                f"human_calibration: {result['status']} ref={result['row']['ref']} "
                f"score={result['parsed_anchor']['score']}"
            )
            if not result["applied"]:
                print(
                    "  dry-run only; add "
                    f"--apply --confirm-anchor {result['required_confirmation']} to record it"
                )
        return 0
    report = summarize(window_days=args.window_days, min_pairs=args.min_pairs)
    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"human_calibration: status={report['status']} ready={report['ready']} "
            f"anchors={report['structured_anchor_count']} pairs={report['matched_pair_count']} "
            f"window_days={report['window_days']}"
        )
        print(f"  recommendation: {report['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
