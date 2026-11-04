"""Precision Fusion X: strict, closed-candle confluence engine.

This module is deliberately broker-independent. It combines the repository's
existing Breakout/Retest, Momentum 10, and EMA/ADX/RSI strategies with the
archived ALMA 7/12 + Choppiness regime filter and the existing 5-minute trend
filter. Every required component must agree on direction; otherwise NO TRADE.

CRT/PO3/OTE is intentionally not represented here: the repository currently
has no tested, reproducible implementation of those rules. Do not treat a
strategy description as a validated executable signal.
"""

from math import exp, isfinite, log10

from breakout_retest_filter import RetestState, build_entry_signal
from momentum_strategy import calculate_signal as calculate_momentum_signal
from ema_strategy import calculate_signal as calculate_ema_signal
from trend_filter import classify_trend

ALMA_FAST = 7
ALMA_SLOW = 12
ALMA_OFFSET = 0.85
ALMA_SIGMA = 6.0
CHOP_PERIOD = 25
CHOP_MIN = 36.2
CHOP_MAX = 59.8
CHOP_RECENT_READINGS = 3
MIN_FIVE_MINUTE_CANDLES = 55


def alma(values, period, offset=ALMA_OFFSET, sigma=ALMA_SIGMA):
    """Arnaud Legoux Moving Average, using the standard Gaussian weighting."""
    if len(values) < period or period < 2 or sigma <= 0:
        return None
    m = offset * (period - 1)
    s = period / sigma
    weights = [exp(-((i - m) ** 2) / (2 * s * s)) for i in range(period)]
    denominator = sum(weights)
    if denominator <= 0:
        return None
    window = values[-period:]
    if any(not isfinite(float(value)) for value in window):
        return None
    return sum(float(value) * weight for value, weight in zip(window, weights)) / denominator


def choppiness_index(candles, period=CHOP_PERIOD):
    """Standard Choppiness Index (0..100) for the latest closed-candle window."""
    if not isinstance(candles, list) or len(candles) < period + 1:
        return None
    try:
        window = candles[-period:]
        true_ranges = []
        for index in range(len(candles) - period, len(candles)):
            current = candles[index]
            previous_close = float(candles[index - 1]["close"])
            high = float(current["high"])
            low = float(current["low"])
            true_ranges.append(max(high - low, abs(high - previous_close),
                                   abs(low - previous_close)))
        highest = max(float(c["high"]) for c in window)
        lowest = min(float(c["low"]) for c in window)
        price_range = highest - lowest
        total_tr = sum(true_ranges)
        if price_range <= 0 or total_tr <= 0:
            return None
        value = 100.0 * log10(total_tr / price_range) / log10(period)
        return value if isfinite(value) else None
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def _alma_direction(candles):
    closes = [float(c["close"]) for c in candles]
    if len(closes) < ALMA_SLOW + 1:
        return None
    fast_now = alma(closes, ALMA_FAST)
    slow_now = alma(closes, ALMA_SLOW)
    fast_prev = alma(closes[:-1], ALMA_FAST)
    slow_prev = alma(closes[:-1], ALMA_SLOW)
    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return None
    if fast_now > slow_now and fast_now > fast_prev and slow_now >= slow_prev:
        return "call"
    if fast_now < slow_now and fast_now < fast_prev and slow_now <= slow_prev:
        return "put"
    return None


def _chop_is_acceptable(candles):
    if len(candles) < CHOP_PERIOD + CHOP_RECENT_READINGS:
        return False, None, "insufficient candles for CHOP25"
    readings = []
    for end in range(len(candles) - CHOP_RECENT_READINGS + 1, len(candles) + 1):
        value = choppiness_index(candles[:end], CHOP_PERIOD)
        if value is None:
            return False, None, "CHOP25 unavailable"
        readings.append(value)
    if any(value < CHOP_MIN or value > CHOP_MAX for value in readings):
        return False, readings[-1], "CHOP25 outside the configured 36.2–59.8 band"
    return True, readings[-1], ""


def calculate_signal(asset, candles_1m, candles_5m, candle_seconds=60):
    """Return a signal only when B, M10, EMA, ALMA, CHOP and 5m trend all agree.

    Inputs must contain closed candles, oldest first. The returned candle_time
    is the common close timestamp. This function never connects to a broker or
    places an order.
    """
    if not isinstance(candles_1m, list) or not isinstance(candles_5m, list):
        return None, "invalid candle collections"
    if len(candles_1m) < 60:
        return None, "insufficient 1-minute closed candles for all strategies"
    if len(candles_5m) < MIN_FIVE_MINUTE_CANDLES:
        return None, "insufficient 5-minute closed candles"

    try:
        breakout, retest = build_entry_signal(asset, candles_1m, candle_seconds)
        if breakout is None:
            reason = "; ".join(retest.reasons) if retest.reasons else retest.state.value
            if retest.state == RetestState.RETESTED:
                reason = "first valid retest candle has already passed"
            return None, "Breakout/Retest: " + reason

        momentum, momentum_reason = calculate_momentum_signal(asset, candles_1m, candle_seconds)
        if momentum is None:
            return None, "Momentum 10: " + (momentum_reason or "no signal")

        ema_signal, ema_reason = calculate_ema_signal(asset, candles_1m, candle_seconds)
        if ema_signal is None:
            return None, "EMA/ADX/RSI: " + (ema_reason or "no signal")

        directions = {
            "Breakout/Retest": breakout["direction"],
            "Momentum 10": momentum["direction"],
            "EMA/ADX/RSI": ema_signal["direction"],
        }
        if len(set(directions.values())) != 1:
            return None, "strategy direction disagreement: " + ", ".join(
                f"{name}={direction}" for name, direction in directions.items()
            )
        direction = breakout["direction"]

        alma_direction = _alma_direction(candles_1m)
        if alma_direction != direction:
            return None, f"ALMA 7/12 disagrees or is unavailable (expected {direction})"

        chop_ok, chop_value, chop_reason = _chop_is_acceptable(candles_1m)
        if not chop_ok:
            return None, chop_reason

        trend = classify_trend(candles_5m)
        expected_trend = "up" if direction == "call" else "down"
        if trend != expected_trend:
            return None, f"5-minute trend disagreement: {trend}; expected {expected_trend}"

        stamps = {
            int(breakout["candle_time"]),
            int(momentum["candle_time"]),
            int(ema_signal["candle_time"]),
        }
        if len(stamps) != 1:
            return None, "strategy signals do not share the same closed-candle timestamp"

        signal = dict(breakout)
        signal.update({
            "direction": direction,
            "candle_time": stamps.pop(),
            "source": "Precision Fusion X — strict confluence",
            "strategy_votes": directions,
            "alma_direction": alma_direction,
            "choppiness": round(chop_value, 2),
            "trend_5m": trend,
            "ema20": ema_signal.get("ema20"),
            "ema50": ema_signal.get("ema50"),
            "adx14": ema_signal.get("adx14"),
            "rsi14": ema_signal.get("rsi14"),
            "momentum_score": momentum.get("score"),
            "confluence": "B+M+E+ALMA+CHOP+5M",
        })
        # This is a confluence count, not a win-probability estimate.
        signal["score"] = 6
        return signal, ""
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return None, "invalid candle data; fusion engine safely skipped"


__all__ = [
    "ALMA_FAST", "ALMA_SLOW", "ALMA_OFFSET", "ALMA_SIGMA",
    "CHOP_PERIOD", "CHOP_MIN", "CHOP_MAX", "alma",
    "choppiness_index", "calculate_signal",
]
