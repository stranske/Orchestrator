#!/usr/bin/env python3
"""judge_reliability.py - data-gated evaluator reliability weights.

The A/B/C/D harness records an evaluation matrix, but winner synthesis previously
handled known judge drift with a static exclusion. This module turns the matrix
itself into a cautious reliability signal: compare each judge's score to the
leave-one-out consensus for the same experiment/candidate, optionally add simple
human score anchors, and only emit non-neutral weights after enough comparisons.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict

import feedback

DEFAULT_WINDOW_DAYS = 365
DEFAULT_MIN_COMPARISONS = 6
DEFAULT_MIN_EXPERIMENTS = 2
MIN_WEIGHT = 0.25
MAX_WEIGHT = 1.25
# MAE at which a judge is considered uninformative and pinned to MIN_WEIGHT. The old mapping
# (clamp(0.5 + (1 - mae/QUALITY_MAX))) saturated: every mae <= 2.5 hit MAX_WEIGHT, and ALL live
# judges sat in the 0.9-2.3 band, so a mae-2.23 judge weighed the same 1.25 as a mae-0.90 one
# (2026-07-08 audit follow-up, item 12a). On a 0-10 rubric a judge guessing near the mean lands
# around mae 3.5-4.5 against real score dispersion, so 4.0 is the "no better than guessing" line;
# the weight now falls linearly from MAX_WEIGHT at mae=0 to MIN_WEIGHT at mae>=USELESS_JUDGE_MAE.
USELESS_JUDGE_MAE = 4.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _score(value) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0 or score > feedback.QUALITY_MAX:
        return None
    return score


def _human_anchor_rows(rows) -> dict[tuple[str, str], list[float]]:
    """Parse optional human score anchors from human_calibration.

    Supported compact forms:
    - ref="exp_id:implementer", human_verdict="8.5"
    - human_verdict='{"experiment_id":"exp_id","implementer":"agent","score":8.5}'
    - ref="exp_id", human_verdict='{"scores":{"agent":8.5}}'

    Free-form human notes remain ignored; they are still useful elsewhere, but not
    structured enough to weight judges.
    """
    anchors: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows or []:
        if isinstance(row, dict):
            ref = row.get("ref")
            verdict = row.get("human_verdict")
        else:
            # (ts, ref, human_verdict, note)
            ref = row[1] if len(row) > 1 else None
            verdict = row[2] if len(row) > 2 else None
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
                anchors[(str(exp), str(impl))].append(score)
            scores = parsed.get("scores")
            if ref and isinstance(scores, dict):
                for impl, raw_score in scores.items():
                    score = _score(raw_score)
                    if score is not None:
                        anchors[(str(ref), str(impl))].append(score)
            continue
        score = _score(verdict)
        if score is None or ":" not in str(ref):
            continue
        exp, impl = str(ref).rsplit(":", 1)
        if exp and impl:
            anchors[(exp, impl)].append(score)
    return anchors


def compute(
    evaluation_rows,
    human_rows=None,
    *,
    min_comparisons: int = DEFAULT_MIN_COMPARISONS,
    min_experiments: int = DEFAULT_MIN_EXPERIMENTS,
    generated_at: int | None = None,
) -> dict:
    """Return reliability weights from evaluation rows.

    `evaluation_rows` accepts either dicts with experiment_id/implementer/evaluator/score
    or tuples shaped like feedback.evaluations. Not-ready judges get neutral weight 1.0.
    """
    by_target: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    raw_counts: dict[str, int] = defaultdict(int)
    for row in evaluation_rows or []:
        if isinstance(row, dict):
            exp = row.get("experiment_id")
            impl = row.get("implementer")
            evaluator = row.get("evaluator")
            score = _score(row.get("score"))
        else:
            # (experiment_id, implementer, evaluator, score, ...)
            exp = row[0] if len(row) > 0 else None
            impl = row[1] if len(row) > 1 else None
            evaluator = row[2] if len(row) > 2 else None
            score = _score(row[3] if len(row) > 3 else None)
        if not exp or not impl or not evaluator or score is None:
            continue
        exp, impl, evaluator = str(exp), str(impl), str(evaluator)
        by_target[(exp, impl)].append((evaluator, score))
        raw_counts[evaluator] += 1

    anchors = _human_anchor_rows(human_rows)
    stats = {
        evaluator: {
            "consensus_errors": [],
            "human_errors": [],
            "experiments": set(),
        }
        for evaluator in raw_counts
    }
    for (exp, impl), ratings in by_target.items():
        for evaluator, score in ratings:
            others = [other_score for other_eval, other_score in ratings if other_eval != evaluator]
            if others:
                consensus = sum(others) / len(others)
                stats[evaluator]["consensus_errors"].append(abs(score - consensus))
                stats[evaluator]["experiments"].add(exp)
            human_scores = anchors.get((exp, impl)) or []
            if human_scores:
                human_score = sum(human_scores) / len(human_scores)
                stats[evaluator]["human_errors"].append(abs(score - human_score))
                stats[evaluator]["experiments"].add(exp)

    judges = {}
    for evaluator in sorted(raw_counts):
        consensus_errors = stats[evaluator]["consensus_errors"]
        human_errors = stats[evaluator]["human_errors"]
        errors = consensus_errors + human_errors
        comparisons = len(errors)
        experiments = len(stats[evaluator]["experiments"])
        ready = comparisons >= min_comparisons and experiments >= min_experiments
        mae = (sum(errors) / comparisons) if comparisons else None
        mae_for_agreement = mae if mae is not None else feedback.QUALITY_MAX
        agreement = _clamp(1.0 - (mae_for_agreement / feedback.QUALITY_MAX), 0.0, 1.0)
        weight = (
            _clamp(
                MAX_WEIGHT
                - (MAX_WEIGHT - MIN_WEIGHT) * (mae_for_agreement / USELESS_JUDGE_MAE),
                MIN_WEIGHT,
                MAX_WEIGHT,
            )
            if ready
            else 1.0
        )
        judges[evaluator] = {
            "weight": round(weight, 4),
            "ready": ready,
            "raw_score_count": raw_counts[evaluator],
            "comparisons": comparisons,
            "experiments": experiments,
            "mean_abs_error": round(mae, 4) if mae is not None else None,
            "consensus_comparisons": len(consensus_errors),
            "consensus_mean_abs_error": (
                round(sum(consensus_errors) / len(consensus_errors), 4)
                if consensus_errors else None
            ),
            "human_comparisons": len(human_errors),
            "human_mean_abs_error": (
                round(sum(human_errors) / len(human_errors), 4)
                if human_errors else None
            ),
            "agreement": round(agreement, 4) if comparisons else None,
        }
    ready_judges = sum(1 for row in judges.values() if row["ready"])
    return {
        "generated_at": generated_at or int(time.time()),
        "method": "leave_one_out_consensus_plus_optional_human_score_anchors",
        "quality_max": feedback.QUALITY_MAX,
        "min_comparisons": min_comparisons,
        "min_experiments": min_experiments,
        "min_weight": MIN_WEIGHT,
        "max_weight": MAX_WEIGHT,
        "useless_judge_mae": USELESS_JUDGE_MAE,
        "human_anchor_count": sum(len(v) for v in anchors.values()),
        "judge_count": len(judges),
        "ready_judge_count": ready_judges,
        "ready": ready_judges > 0,
        "judges": judges,
    }


def summarize(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_comparisons: int = DEFAULT_MIN_COMPARISONS,
    min_experiments: int = DEFAULT_MIN_EXPERIMENTS,
) -> dict:
    since = int(time.time()) - window_days * 86400
    with feedback._conn() as c:
        legacy_rows = c.execute(
            "SELECT experiment_id, implementer, evaluator, score, ts FROM evaluations WHERE ts>=?",
            (since,),
        ).fetchall()
        v2_rows = c.execute(
            "SELECT experiment_id, implementer_member_id, evaluator_id, evaluator_agent, score, ts "
            "FROM evaluations_v2 WHERE ts>=?",
            (since,),
        ).fetchall()
        human_rows = c.execute(
            "SELECT ts, ref, human_verdict, note FROM human_calibration WHERE ts>=?",
            (since,),
        ).fetchall()
    v2_experiments = {str(row[0]) for row in v2_rows}
    legacy_exact = [row for row in legacy_rows if str(row[0]) not in v2_experiments]
    exact_rows = [
        *legacy_exact,
        *[
            {
                "experiment_id": exp,
                "implementer": member,
                "evaluator": evaluator_id,
                "score": score,
                "ts": ts,
            }
            for exp, member, evaluator_id, _agent, score, ts in v2_rows
        ],
    ]
    report = compute(
        exact_rows,
        human_rows,
        min_comparisons=min_comparisons,
        min_experiments=min_experiments,
    )
    fallback_rows = [
        *legacy_exact,
        *[
            {
                "experiment_id": exp,
                "implementer": member,
                "evaluator": evaluator_agent,
                "score": score,
                "ts": ts,
            }
            for exp, member, _evaluator_id, evaluator_agent, score, ts in v2_rows
        ],
    ]
    fallback_report = compute(
        fallback_rows,
        human_rows,
        min_comparisons=min_comparisons,
        min_experiments=min_experiments,
    )
    report["agent_fallback_judges"] = fallback_report["judges"]
    report["evaluator_fallbacks"] = {
        str(evaluator_id): str(evaluator_agent)
        for _exp, _member, evaluator_id, evaluator_agent, _score, _ts in v2_rows
    }
    report["window_days"] = window_days
    return report


def weights_from_summary(
    report: dict | None,
    *,
    evaluators=None,
    fallback_exclude_judges=(),
) -> dict[str, float]:
    """Return synthesis weights.

    Ready judges use learned reliability. Not-ready judges stay neutral, except
    callers may preserve a legacy exclusion as a fallback until enough evidence
    exists to override it.
    """
    judges = (report or {}).get("judges") or {}
    fallback_judges = (report or {}).get("agent_fallback_judges") or {}
    identities = (report or {}).get("evaluator_fallbacks") or {}
    names = list(evaluators or judges)
    out: dict[str, float] = {}
    fallback = set(fallback_exclude_judges or ())
    for name in names:
        row = judges.get(name) or {}
        if row.get("ready"):
            out[name] = float(row.get("weight", 1.0))
        else:
            fallback_name = identities.get(name, name)
            fallback_row = fallback_judges.get(fallback_name) or {}
            if fallback_row.get("ready"):
                out[name] = float(fallback_row.get("weight", 1.0))
            else:
                out[name] = 0.0 if fallback_name in fallback else 1.0
    return out


def _selftest() -> None:
    eval_rows = []
    for exp in ("exp1", "exp2"):
        eval_rows.extend([
            (exp, "impl_a", "good_1", 9.0),
            (exp, "impl_a", "good_2", 8.5),
            (exp, "impl_a", "good_3", 9.0),
            (exp, "impl_a", "noisy", 1.0),
            (exp, "impl_b", "good_1", 4.0),
            (exp, "impl_b", "good_2", 4.5),
            (exp, "impl_b", "good_3", 4.0),
            (exp, "impl_b", "noisy", 10.0),
        ])
    human_rows = [
        (1, "exp1:impl_a", "9.0", None),
        (1, "exp1:impl_b", "4.0", None),
        (1, "exp2", json.dumps({"scores": {"impl_a": 8.8, "impl_b": 4.2}}), None),
    ]
    report = compute(
        eval_rows,
        human_rows,
        min_comparisons=4,
        min_experiments=2,
        generated_at=1,
    )
    assert report["ready"] is True and report["human_anchor_count"] == 4, report
    assert report["judges"]["good_1"]["ready"] is True, report["judges"]["good_1"]
    assert report["judges"]["noisy"]["weight"] < report["judges"]["good_1"]["weight"], report["judges"]
    perfect = compute(
        [
            ("exp1", "impl_a", "judge_a", 8.0),
            ("exp1", "impl_a", "judge_b", 8.0),
            ("exp1", "impl_a", "judge_c", 8.0),
            ("exp1", "impl_b", "judge_a", 4.0),
            ("exp1", "impl_b", "judge_b", 4.0),
            ("exp1", "impl_b", "judge_c", 4.0),
        ],
        min_comparisons=2,
        min_experiments=1,
        generated_at=1,
    )
    assert perfect["judges"]["judge_a"]["agreement"] == 1.0, perfect
    assert perfect["judges"]["judge_a"]["weight"] == MAX_WEIGHT, perfect
    # 12a regression: a REALISTIC mae spread (both judges under 2.5) must NOT saturate at
    # MAX_WEIGHT — the old clamp(0.5+agreement) map gave a mae-2.2 judge the same 1.25 as a
    # mae-0.9 judge (observed live: all 5 judges capped despite mae 0.90-2.23).
    band = compute(
        [
            ("exp1", "impl_a", "sharp", 8.9),
            ("exp1", "impl_a", "blunt", 5.8),
            ("exp2", "impl_b", "sharp", 4.9),
            ("exp2", "impl_b", "blunt", 6.2),
        ],
        [
            (1, "exp1:impl_a", "8.0", None),
            (1, "exp2:impl_b", "4.0", None),
        ],
        min_comparisons=2,
        min_experiments=2,
        generated_at=1,
    )
    sharp_w = band["judges"]["sharp"]["weight"]
    blunt_w = band["judges"]["blunt"]["weight"]
    assert band["judges"]["sharp"]["ready"] and band["judges"]["blunt"]["ready"], band
    assert sharp_w < MAX_WEIGHT and blunt_w < MAX_WEIGHT, (sharp_w, blunt_w)
    assert sharp_w - blunt_w > 0.1, (sharp_w, blunt_w)
    weights = weights_from_summary(report, evaluators=["good_1", "missing", "noisy"], fallback_exclude_judges=["missing"])
    assert weights["good_1"] > weights["noisy"] and weights["missing"] == 0.0, weights
    sparse = compute([("exp1", "impl_a", "new_judge", 8.0)], min_comparisons=4, generated_at=1)
    assert sparse["judges"]["new_judge"]["ready"] is False
    assert weights_from_summary(sparse, evaluators=["new_judge"])["new_judge"] == 1.0
    print("judge_reliability.py selftest: OK (consensus + human anchors, data-gated weights)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize data-gated evaluator reliability weights.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--min-comparisons", type=int, default=DEFAULT_MIN_COMPARISONS)
    parser.add_argument("--min-experiments", type=int, default=DEFAULT_MIN_EXPERIMENTS)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    report = summarize(
        window_days=args.window_days,
        min_comparisons=args.min_comparisons,
        min_experiments=args.min_experiments,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"judge_reliability: judges={report['judge_count']} ready={report['ready_judge_count']} "
            f"window_days={report['window_days']}"
        )
        for name, row in report["judges"].items():
            status = "ready" if row["ready"] else "not-ready"
            print(
                f"  {name}: {status} weight={row['weight']:.3f} "
                f"comparisons={row['comparisons']} mae={row['mean_abs_error']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
