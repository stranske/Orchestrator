"""Time-based pool pacing. Pure arithmetic with explicit clocks and no I/O.

A reset substitutes for reserve, not actual quota: exhaustion always sheds.
The literal revised reserve/weight formulas govern (one reset => 0.075 reserve).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

RESERVE_FRACTION = 0.15
RESET_BOOST = 0.5
BURN_WINDOW_HOURS = 6
EARLY_SPEND_BAND = 0.10
PASS_LOWER_MARGIN = 0.10
PASS_UPPER_MARGIN = 0.05
PASS_SHARE_MULTIPLIER = 2
DAY_SECONDS = 86400
PERIOD_SECONDS = 7 * DAY_SECONDS
TICK_SECONDS = 900  # Capacity recording cadence; distinct from scheduled usage polls.
POLL_TICK_SECONDS = 600
POLL_STALE_TICKS = 3
STALE_AFTER_SECONDS = POLL_STALE_TICKS * POLL_TICK_SECONDS


def timestamp(value) -> float | None:
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def active_resets(entries: list[dict], now: float) -> list[dict]:
    return [
        entry
        for entry in entries
        if entry.get("consumed_at") is None
        and (expiry := timestamp(entry.get("expires"))) is not None
        and expiry > now
    ]


def effective_reserve(resets_available: int) -> float:
    return RESERVE_FRACTION * max(0.0, 1 - RESET_BOOST * resets_available)


def period(
    pool: str, now: float, *, start=None, reset_at=None, end=None
) -> tuple[float, float] | None:
    if pool == "claude":
        finish = timestamp(end)
        # Only a provider-observed weekly boundary is authoritative. No guessed anchor.
        return (finish - PERIOD_SECONDS, finish) if finish is not None else None
    origin = timestamp(reset_at if reset_at is not None else start)
    if origin is None or origin > now:
        return None
    origin += math.floor((now - origin) / PERIOD_SECONDS) * PERIOD_SECONDS
    return origin, origin + PERIOD_SECONDS


def elapsed_fraction(now: float, start: float, end: float) -> float:
    if end <= start:
        raise ValueError("period_end must follow period_start")
    return min(1.0, max(0.0, (now - start) / (end - start)))


def burn_rate(samples: list[dict], now: float, start: float, *, source=None) -> float | None:
    """EWMA of du/day on the last W hours; spanning intervals are clipped to W.

    A repeated fresh flat sample measures zero. A stale observation does not invent
    a flat sample. Negative jumps mark a reset and cannot debit the new period.
    """
    rows = sorted(
        (
            r
            for r in samples
            if start <= r["ts"] <= now
            and r.get("used_fraction") is not None
            and (source is None or r.get("source") == source)
        ),
        key=lambda r: r["ts"],
    )
    if len(rows) < 2:
        return None
    window = BURN_WINDOW_HOURS * 3600
    weighted = mass = 0.0
    for left, right in zip(rows, rows[1:]):
        dt = right["ts"] - left["ts"]
        if dt <= 0 or right["ts"] <= now - window:
            continue
        delta = right["used_fraction"] - left["used_fraction"]
        if delta < 0:
            weighted = mass = 0.0
            continue
        a = max(left["ts"], now - window)
        b = right["ts"]
        weight = math.exp(-(now - b) / window) - math.exp(-(now - a) / window)
        weighted += delta / dt * DAY_SECONDS * weight
        mass += weight
    return weighted / mass if mass > 0 else None


def routing_weight(projected: float, reserve: float) -> float:
    target = 1 - reserve
    return min(1.0, max(0.0, (target - projected) / target))


def pass_bounds(reserve: float) -> tuple[float, float]:
    return 1 - reserve - PASS_LOWER_MARGIN, min(1, 1 - reserve + PASS_UPPER_MARGIN)


def calibration_miss(cap: float, observed_fraction: float) -> float:
    """At quota while predicted ok, reduce the allowance to observed spend."""
    return cap * max(0.0, min(1.0, observed_fraction))


def evaluate(
    pool: str,
    *,
    now: float,
    used_fraction: float | None,
    period_start: float | None,
    period_end: float | None,
    resets: list[dict] | None,
    samples: list[dict],
    source: str,
    source_ts: float | None,
    exhausted_at: float | None = None,
    previous_state: str | None = None,
) -> dict:
    bank = active_resets(resets, now) if resets is not None else None
    n = len(bank) if bank is not None else None
    reserve = effective_reserve(n if n is not None else 0)
    result: dict = dict(
        state="unknown",
        used_fraction=used_fraction,
        allowed_fraction=None,
        projected_fraction=None,
        burn_rate_per_day=None,
        burn_per_day=None,
        routing_weight=None,
        period_start=period_start,
        period_end=period_end,
        resets_available=n,
        source=source,
        ts=source_ts,
        effective_reserve=reserve,
        stale=source_ts is not None and now - source_ts > STALE_AFTER_SECONDS,
        fyi=[],
        reason="usage or period unmeasured; clears on a fresh usage/period sample",
    )
    if bank is None:
        result["fyi"].append(f"pool {pool}: resets=unknown; conservative reserve={reserve:.2f}")
    if period_start is None or period_end is None:
        return result
    latched = exhausted_at is not None and period_start <= exhausted_at < period_end
    if used_fraction is None or result["stale"] or source_ts is None:
        if latched:
            result["state"] = "shed"
            result["routing_weight"] = 0.0
        result["reason"] = (
            f"pool {pool}: usage {'stale' if result['stale'] else 'unmeasured'}; "
            f"{result['state']}; clears on fresh usage or reset at {period_end:.0f}"
        )
        return result
    # A newer measured observation is stronger evidence than a window-ambiguous 429.
    if latched and exhausted_at is not None and source_ts > exhausted_at and used_fraction < 1:
        latched = False
    u = max(0.0, used_fraction)
    allowed = (1 - reserve) * elapsed_fraction(now, period_start, period_end)
    rate = burn_rate(samples, now, period_start, source=source)
    projected = u + rate * max(0, period_end - now) / DAY_SECONDS if rate is not None else None
    if u >= 1 or latched:
        state = "shed"
    elif projected is None:
        state = "ok" if u <= allowed + EARLY_SPEND_BAND else "warn"
    elif projected <= 1 - reserve:
        state = "ok"
    elif projected <= 1 or (n is not None and n >= 1):
        state = "warn"
    else:
        state = "shed"
    # Cold-start schedule test governs state; the literal weight uses observed u
    # until a projection is measurable, visibly labelled in the result.
    weight = routing_weight(projected if projected is not None else u, reserve)
    if state == "shed":
        weight = 0.0
    result.update(
        state=state,
        allowed_fraction=allowed,
        projected_fraction=projected,
        burn_rate_per_day=rate,
        burn_per_day=rate,
        routing_weight=weight,
    )
    if projected is not None and projected > 1 and bank and not latched and u < 1:
        reset = bank[0]
        result["fyi"].append(
            f"pool {pool}: would consume oldest-expiring reset "
            f"expires={reset['expires']} (expiry source={reset.get('source', 'owner estimate UNKNOWN')})"
        )
    if state == "ok" and previous_state == "shed" and projected is not None:
        result["fyi"].append(f"pool {pool}: ũ={projected:.2f} ≤ 1−r_eff → ok")
    if previous_state == "shed" and state != "shed":
        if projected is None:
            result["fyi"].append(f"pool {pool}: shed → {state} (reset/rollover; ũ=unmeasured)")
        elif state != "ok":
            result["fyi"].append(
                f"pool {pool}: shed → {state}; ũ={projected:.2f} vs 1−r_eff={1 - reserve:.2f}; "
                f"u={u:.2f}; clears toward ok on fresh flat samples or reset at {period_end:.0f}"
            )
    if projected is None:
        blocking = f"u={u:.2f} vs g(e)+δ={allowed + EARLY_SPEND_BAND:.2f}; ũ=unmeasured"
        clear = "fresh samples or schedule advancing"
    else:
        blocking = f"ũ={projected:.2f} vs 1-r_eff={1 - reserve:.2f}"
        hours = (
            max(0, (projected - (1 - reserve)) / rate * 24)
            if rate is not None and rate > 0
            else None
        )
        clear = f"~{hours:.1f}h at current burn" if hours is not None else "fresh flat samples"
    if latched or u >= 1:
        clear = f"reset at {period_end:.0f} or banked reset event"
    result["reason"] = (
        f"{pool}: {blocking}; {state}; w_p={weight:.3f}; clears in {clear} or on reset"
    )
    return result
