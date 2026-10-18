"""Time-based pool pacing. Pure arithmetic with explicit clocks and no I/O.

Banked resets expand the pacing budget; actual exhaustion always sheds.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

RESERVE_FRACTION = 0.15
RESET_BOOST = 0.5
BURN_WINDOW_HOURS = 6
MAX_WEIGHT_CHANGE = 0.25
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
BURN_ANCHOR_MAX_AGE_SECONDS = BURN_WINDOW_HOURS * 3600 + STALE_AFTER_SECONDS


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


def burn_estimate(
    samples: list[dict],
    now: float,
    start: float,
    *,
    source=None,
    diagnostics: list[str] | None = None,
) -> tuple[float | None, float]:
    """Return EWMA du/day and observed span, retaining one clipped window anchor.

    Only distinct timestamps from this source/period count. A negative usage jump
    starts a new history, including for pools whose scheduled period stays fixed.
    Sparse history still reports its span, but cannot define a burn rate.
    """
    window = BURN_WINDOW_HOURS * 3600
    by_time = {
        r["ts"]: r
        for r in samples
        if start <= r["ts"] <= now
        and isinstance(r.get("used_fraction"), (int, float))
        and math.isfinite(r["used_fraction"])
        and (source is None or r.get("source") == source)
    }
    rows = [by_time[t] for t in sorted(by_time)]
    # Keep the latest sample at/before the cutoff. Real poll timestamps jitter;
    # dropping this anchor makes a full window depend on exact clock alignment.
    anchors = [i for i, row in enumerate(rows) if row["ts"] <= now - window]
    if anchors:
        anchor = anchors[-1]
        age = now - rows[anchor]["ts"]
        if age > BURN_ANCHOR_MAX_AGE_SECONDS:
            rows = rows[anchor + 1 :]
            if diagnostics is not None:
                diagnostics.append(
                    f"anchor ignored: age={age:g}s > bound={BURN_ANCHOR_MAX_AGE_SECONDS:g}s"
                )
        else:
            rows = rows[anchor:]
    restart = 0
    for i in range(1, len(rows)):
        if rows[i]["used_fraction"] < rows[i - 1]["used_fraction"]:
            restart = i
    rows = rows[restart:]
    span_h = min(window, rows[-1]["ts"] - rows[0]["ts"]) / 3600 if rows else 0.0
    if len(rows) < 3 or span_h < 1:
        return None, span_h
    weighted = mass = 0.0
    for left, right in zip(rows, rows[1:]):
        dt = right["ts"] - left["ts"]
        delta = right["used_fraction"] - left["used_fraction"]
        # The interval rate uses its actual endpoints; only its EWMA mass is
        # clipped. Usage before the window must not acquire extra influence.
        a = max(left["ts"], now - window)
        b = right["ts"]
        weight = math.exp(-(now - b) / window) - math.exp(-(now - a) / window)
        weighted += delta / dt * DAY_SECONDS * weight
        mass += weight
    return (weighted / mass if mass > 0 else None), span_h


def burn_rate(samples: list[dict], now: float, start: float, *, source=None) -> float | None:
    """Compatibility view of the minimum-sample-qualified EWMA estimate."""
    return burn_estimate(samples, now, start, source=source)[0]


def routing_weight(sustainable_rate: float, effective_burn: float) -> float:
    return 1.0 if effective_burn <= 0 else min(1.0, max(0.0, sustainable_rate / effective_burn))


def spec_routing_weight(projected: float, reserve: float) -> float:
    """Literal spec formula for comparison tests only; never used by routing."""
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
    previous_weight: float | None = None,
) -> dict:
    bank = active_resets(resets, now) if resets is not None else None
    n = len(bank) if bank is not None else None
    reserve = effective_reserve(n if n is not None else 0)
    result: dict = dict(
        state="unknown",
        used_fraction=used_fraction,
        allowed_fraction=None,
        projected_fraction=None,
        effective_projected_fraction=None,
        span_h=None,
        confidence=None,
        burn_rate_per_day=None,
        burn_per_day=None,
        routing_weight=None,
        capacity_budget=None,
        n_usable=None,
        sustainable_rate_per_hour=None,
        burn_short_per_hour=None,
        burn_period_per_hour=None,
        burn_effective_per_hour=None,
        target_routing_weight=None,
        previous_routing_weight=previous_weight,
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
    rate, span_h = burn_estimate(
        samples, now, period_start, source=source, diagnostics=result["fyi"]
    )
    confidence = min(1.0, max(0.0, span_h / BURN_WINDOW_HOURS))
    remaining_days = max(0, period_end - now) / DAY_SECONDS
    projected = u + rate * remaining_days if rate is not None else None
    effective_projected = u + confidence * rate * remaining_days if rate is not None else None
    if u >= 1 or latched:
        state = "shed"
    elif effective_projected is None or confidence < 1:
        state = "ok" if u <= allowed + EARLY_SPEND_BAND else "warn"
    elif effective_projected <= 1 - reserve:
        state = "ok"
    elif effective_projected <= 1 or (n is not None and n >= 1):
        state = "warn"
    else:
        state = "shed"
    n_usable = len(bank) if bank is not None else 0
    budget = (1 - reserve) + n_usable
    sustainable = max(0.0, budget - u) / max((period_end - now) / 3600, 1.0)
    period_burn = u / max((now - period_start) / 3600, 1.0)
    short_burn = rate / 24 if rate is not None else None
    effective_burn = (
        confidence * short_burn + (1 - confidence) * period_burn
        if short_burn is not None
        else period_burn
    )
    # Source changes preserve the same period's prior; reset events restart it.
    prior_weight = min(1.0, max(0.0, previous_weight)) if previous_weight is not None else 1.0
    target_weight = routing_weight(sustainable, effective_burn)
    weight = target_weight
    if u >= 1 or latched:
        weight = 0.0
    if u < 1 and not latched:
        weight = min(
            prior_weight + MAX_WEIGHT_CHANGE, max(prior_weight - MAX_WEIGHT_CHANGE, weight)
        )
    result.update(
        state=state,
        allowed_fraction=allowed,
        projected_fraction=projected,
        effective_projected_fraction=effective_projected,
        span_h=span_h,
        confidence=confidence,
        previous_routing_weight=prior_weight,
        burn_rate_per_day=rate,
        burn_per_day=rate,
        routing_weight=weight,
        capacity_budget=budget,
        n_usable=n_usable,
        sustainable_rate_per_hour=sustainable,
        burn_short_per_hour=short_burn,
        burn_period_per_hour=period_burn,
        burn_effective_per_hour=effective_burn,
        target_routing_weight=target_weight,
    )
    if u + effective_burn * max(0, period_end - now) / 3600 > 1 and bank and not latched and u < 1:
        reset = min(bank, key=lambda entry: timestamp(entry["expires"]) or 0.0)
        result["fyi"].append(
            f"pool {pool}: would consume oldest-expiring reset "
            f"expires={reset['expires']} (expiry source={reset.get('source', 'owner estimate UNKNOWN')})"
        )
    if (
        state == "ok"
        and previous_state in ("shed", "warn")
        and projected is not None
        and confidence == 1
    ):
        result["fyi"].append(f"pool {pool}: ũ={projected:.2f} ≤ 1−r_eff → ok")
    if previous_state == "shed" and state != "shed":
        if projected is None:
            result["fyi"].append(f"pool {pool}: shed → {state} (reset/rollover; ũ=unmeasured)")
        elif confidence < 1:
            result["fyi"].append(
                f"pool {pool}: shed → {state}; schedule decides state (c={confidence:.3f}); "
                f"ũ_eff={effective_projected:.2f}; clears on fresh samples or reset at {period_end:.0f}"
            )
        elif state != "ok":
            result["fyi"].append(
                f"pool {pool}: shed → {state}; ũ={projected:.2f} vs 1−r_eff={1 - reserve:.2f}; "
                f"u={u:.2f}; clears toward ok on fresh flat samples or reset at {period_end:.0f}"
            )
    if effective_projected is None or confidence < 1:
        projection_text = (
            "ũ=unmeasured"
            if effective_projected is None
            else f"ũ_eff={effective_projected:.2f}; c={confidence:.3f}; schedule decides state"
        )
        blocking = f"u={u:.2f} vs g(e)+δ={allowed + EARLY_SPEND_BAND:.2f}; {projection_text}"
        clear = "fresh samples or schedule advancing"
    else:
        blocking = f"ũ={effective_projected:.2f} vs 1-r_eff={1 - reserve:.2f}"
        hours = (
            max(0, (effective_projected - (1 - reserve)) / rate * 24)
            if rate is not None and rate > 0
            else None
        )
        clear = f"~{hours:.1f}h at current burn" if hours is not None else "fresh flat samples"
    if latched or u >= 1:
        clear = f"reset at {period_end:.0f} or banked reset event"
    result["reason"] = (
        f"{pool}: {blocking}; span_h={span_h:.3f}; {state}; "
        f"w_p_prev→w_p={prior_weight:.3f}→{weight:.3f}; clears in {clear} or on reset"
    )
    return result
