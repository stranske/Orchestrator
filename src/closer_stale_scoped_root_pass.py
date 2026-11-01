"""#1050 owner-side pass number: grade closer coordinator receipts.

Kept outside ``closer_dispatcher`` so the sealed validate-stop tip does not need
another digest rebind for a read-only grader. The live check remains owner-side
(merge + 24 h against retained coordinator receipts); this module makes that
grade machine-checkable once a receipt JSON is in hand.

Pass: ``authority is True``, or ``reason`` is a non-empty string other than
``prior_run_has_no_valid_stop`` and the receipt is not still stuck on the
reservation-drain latch named by #1049.

Next break (not #1050 completion): ``reason`` has left
``prior_run_has_no_valid_stop`` but ``blocking_reservations > 0`` with
``drainable_reservations != blocking_reservations`` — the successor latch already
tracked as #1049. Partial drainability is still blocked by the runtime gate.

Multi-receipt grade (#1050 AC): after the check date, **at least one** receipt
that passes (or surfaces the next-break drain latch) clears the stop-latch
criterion; older still-latched receipts do not veto that.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PASS_CHECK_DELAY_SECONDS = 24 * 60 * 60

PRIOR_STOP_LATCH_REASON = "prior_run_has_no_valid_stop"
NEXT_BREAK_ISSUE = 1049

# Prefer cleared outcomes over still-latched ones when grading a set.
_STATUS_RANK = {
    "pass": 0,
    "next_break": 1,
    "fail": 2,
    "unmeasured": 3,
    "pending": 4,
}


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _reservation_drain_stuck(receipt: Mapping[str, Any]) -> bool:
    blocking = _nonneg_int(receipt.get("blocking_reservations"))
    drainable = _nonneg_int(receipt.get("drainable_reservations"))
    return blocking is not None and drainable is not None and blocking > 0 and drainable != blocking


def stale_scoped_root_pass_number(
    receipt: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    merged_at: float | None = None,
):
    """Grade a closer coordinator receipt for the #1050 stale-root stop latch.

    Does not certify that the receipt came from a post-merge live closer round;
    the owner supplies that provenance when checking merge + 24 h.
    Without the actual merge timestamp, the grade remains pending.
    """
    import time

    stamp = time.time() if now is None else float(now)
    if not math.isfinite(stamp):
        raise ValueError("now must be a finite Unix timestamp")
    check_at = None
    check_date = None
    if merged_at is not None:
        if isinstance(merged_at, bool) or not math.isfinite(merged_at) or merged_at < 0:
            raise ValueError("merged_at must be a finite nonnegative Unix timestamp")
        check_at = merged_at + PASS_CHECK_DELAY_SECONDS
        try:
            check_date = datetime.fromtimestamp(check_at, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("merged_at is outside the supported date range") from exc
    row = receipt if isinstance(receipt, Mapping) else {}
    authority = row.get("authority")
    reason = row.get("reason")
    result = {
        "check_date": check_date,
        "check_at": check_at,
        "merged_at": merged_at,
        "scope": "receipt_conditions_only",
        "issue": 1050,
        "authority": authority,
        "reason": reason if isinstance(reason, str) else None,
        "blocking_reservations": row.get("blocking_reservations"),
        "drainable_reservations": row.get("drainable_reservations"),
        "status": "pending",
        "grade_reason": "actual merge timestamp not supplied",
        "next_break_issue": None,
    }
    if check_at is None:
        return result
    if stamp < check_at:
        result["grade_reason"] = f"check date {check_date} not reached"
        return result

    if authority is True:
        result.update(status="pass", grade_reason="authority true")
        return result

    if not isinstance(reason, str) or not reason.strip():
        result.update(status="unmeasured", grade_reason="coordinator reason unread")
        return result

    if reason == PRIOR_STOP_LATCH_REASON:
        result.update(
            status="fail",
            grade_reason="prior_run_has_no_valid_stop still latched",
        )
        return result

    if _reservation_drain_stuck(row):
        result.update(
            status="next_break",
            grade_reason=(
                f"stop latch cleared to {reason!r}; reservation drain still "
                f"blocking {row.get('blocking_reservations')} with drainable "
                f"{row.get('drainable_reservations')}"
            ),
            next_break_issue=NEXT_BREAK_ISSUE,
        )
        return result

    result.update(
        status="pass",
        grade_reason=f"reason {reason!r} is not {PRIOR_STOP_LATCH_REASON}",
    )
    return result


def grade_receipts(
    receipts: Sequence[Mapping[str, Any] | None],
    *,
    now: float | None = None,
    merged_at: float | None = None,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Grade many coordinator receipts; #1050 passes when at least one clears.

    Ranking after the check date: ``pass`` > ``next_break`` > ``fail`` >
    ``unmeasured`` > ``pending``. A later cleared receipt wins over older
    still-latched ones so pre-merge failures do not veto the AC.
    """
    graded: list[dict[str, Any]] = []
    for index, receipt in enumerate(receipts):
        row = stale_scoped_root_pass_number(receipt, now=now, merged_at=merged_at)
        source = None
        if sources is not None and index < len(sources):
            source = sources[index]
        graded.append({"source": source, "grade": row})

    if not graded:
        empty = stale_scoped_root_pass_number(None, now=now, merged_at=merged_at)
        empty = dict(empty)
        empty.update(
            scope="receipt_set_at_least_one",
            grade_reason="no coordinator receipts supplied",
            status="unmeasured" if empty["status"] != "pending" else "pending",
            receipt_count=0,
            winning_source=None,
            grades=[],
        )
        return empty

    best = min(
        graded,
        key=lambda item: _STATUS_RANK.get(item["grade"]["status"], 99),
    )
    aggregate = dict(best["grade"])
    aggregate.update(
        scope="receipt_set_at_least_one",
        receipt_count=len(graded),
        winning_source=best["source"],
        grades=graded,
    )
    source_note = f" ({best['source']})" if best["source"] else ""
    if aggregate["status"] == "pass":
        aggregate["grade_reason"] = (
            f"at least one receipt passed{source_note}: {best['grade']['grade_reason']}"
        )
    elif aggregate["status"] == "next_break":
        aggregate["grade_reason"] = (
            "at least one receipt cleared stop latch into reservation drain"
            f"{source_note}: {best['grade']['grade_reason']}"
        )
    elif aggregate["status"] == "fail":
        aggregate["grade_reason"] = (
            f"all {len(graded)} receipt(s) still latched on {PRIOR_STOP_LATCH_REASON}"
        )
    return aggregate


def load_receipt_json(path: Path) -> Mapping[str, Any]:
    """Load one coordinator receipt object from a JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: receipt JSON must be an object")
    return raw


def load_receipts_from_paths(paths: Sequence[Path]) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Load receipt objects; directories contribute ``*.json`` files (sorted)."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.glob("*.json") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(str(path))
    receipts = [load_receipt_json(path) for path in files]
    return receipts, [str(path) for path in files]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Grade closer coordinator receipt(s) for #1050 stale-scoped-root "
            "stop-latch clearance (owner-side pass number)."
        )
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="receipt JSON file(s) and/or directories of *.json receipts",
    )
    parser.add_argument(
        "--merged-at",
        type=float,
        default=None,
        help="actual merge Unix timestamp; grading stays pending until merge + 24 hours",
    )
    parser.add_argument(
        "--now",
        type=float,
        default=None,
        help="override unix timestamp used for the check-date gate",
    )
    args = parser.parse_args(argv)
    try:
        receipts, sources = load_receipts_from_paths(args.paths)
        if len(receipts) == 1:
            result = stale_scoped_root_pass_number(
                receipts[0], now=args.now, merged_at=args.merged_at
            )
            result = dict(result)
            result["source"] = sources[0]
        else:
            result = grade_receipts(
                receipts, now=args.now, sources=sources, merged_at=args.merged_at
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, sort_keys=True))
        return 78
    print(json.dumps(result, sort_keys=True))
    status = result.get("status")
    if status == "pass":
        return 0
    if status == "pending":
        return 0
    if status == "next_break":
        return 2
    if status == "fail":
        return 1
    return 78


if __name__ == "__main__":
    sys.exit(main())
