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
``drainable_reservations == 0`` — the successor latch already tracked as #1049.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

# Provisional calendar date for merge+24h once #1051 lands on 2026-11-01/02.
# Revise the label if merge slips past that window.
STALE_SCOPED_ROOT_PASS_CHECK_TS = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())
STALE_SCOPED_ROOT_PASS_CHECK_LABEL = "2026-11-03"

PRIOR_STOP_LATCH_REASON = "prior_run_has_no_valid_stop"
NEXT_BREAK_ISSUE = 1049


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _reservation_drain_stuck(receipt: Mapping[str, Any]) -> bool:
    blocking = _nonneg_int(receipt.get("blocking_reservations"))
    drainable = _nonneg_int(receipt.get("drainable_reservations"))
    return blocking is not None and drainable is not None and blocking > 0 and drainable == 0


def stale_scoped_root_pass_number(receipt: Mapping[str, Any] | None, *, now: float | None = None):
    """Grade a closer coordinator receipt for the #1050 stale-root stop latch.

    Does not certify that the receipt came from a post-merge live closer round;
    the owner supplies that provenance when checking merge + 24 h.
    """
    import time

    stamp = time.time() if now is None else float(now)
    row = receipt if isinstance(receipt, Mapping) else {}
    authority = row.get("authority")
    reason = row.get("reason")
    result = {
        "check_date": STALE_SCOPED_ROOT_PASS_CHECK_LABEL,
        "scope": "receipt_conditions_only",
        "issue": 1050,
        "authority": authority,
        "reason": reason if isinstance(reason, str) else None,
        "blocking_reservations": row.get("blocking_reservations"),
        "drainable_reservations": row.get("drainable_reservations"),
        "status": "pending",
        "grade_reason": f"check date {STALE_SCOPED_ROOT_PASS_CHECK_LABEL} not reached",
        "next_break_issue": None,
    }
    if stamp < STALE_SCOPED_ROOT_PASS_CHECK_TS:
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
