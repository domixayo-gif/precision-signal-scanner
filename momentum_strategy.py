"""Momentum 10 signal logic, isolated from the live Breakout & Retest rules."""

MOMENTUM_PERIOD = 10
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06


def calculate_signal(asset, candles, candle_seconds=60):
    """Return the original Momentum 10 reversal signal using CLOSED candles only."""
    if not isinstance(candles, list) or len(candles) < MOMENTUM_PERIOD + 4:
        return None, "insufficient candles"
    try:
        closes = [float(c["close"]) for c in candles]
        opens = [float(c["open"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        if any(not (x > 0 and x < float("inf")) for x in closes + opens + highs + lows):
            return None, "invalid candle values"
        current, previous, old = closes[-1], closes[-2], closes[-1 - MOMENTUM_PERIOD]
        if old <= 0:
            return None, "invalid reference price"
        momentum = (current - old) / old
        start = max(0, len(candles) - 14)
        ranges = [highs[i] - lows[i] for i in range(start, len(candles))]
        average_range = sum(ranges) / len(ranges) if ranges else 0.0
        if average_range <= 0:
            return None, "no usable candle range"
        candle_range = highs[-1] - lows[-1]
        if candle_range <= 0:
            return None, "flat latest candle"
        body = abs(current - opens[-1]) / candle_range
        upper = (highs[-1] - current) / candle_range
        lower = (current - lows[-1]) / candle_range
        if momentum < 0:
            if current <= opens[-1]:
                return None, "no bullish reversal after downward momentum"
            turn, direction = lower, "call"
        elif momentum > 0:
            if current >= opens[-1]:
                return None, "no bearish reversal after upward momentum"
            turn, direction = upper, "put"
        else:
            return None, "zero momentum"
        if turn < MIN_TURN_DISTANCE:
            return None, "reversal distance too small"
        normalized = abs(current - old) / average_range
        if normalized < EXTREME_PERCENTILE:
            return None, "momentum not extreme enough"
        if direction == "call" and current <= previous:
            return None, "bullish reversal lacks close confirmation"
        if direction == "put" and current >= previous:
            return None, "bearish reversal lacks close confirmation"
        score = (50 + (10 if normalized >= 0.5 else 0)
                 + (10 if normalized >= 1.0 else 0)
                 + (10 if body >= 0.4 else 0)
                 + (10 if body >= 0.65 else 0)
                 + (5 if turn >= 0.3 else 0)
                 + (5 if turn >= 0.5 else 0))
        candle_time = int(candles[-1].get("from") or 0) + int(candle_seconds)
        if candle_time <= 0:
            return None, "invalid candle timestamp"
        return {
            "asset": str(asset), "direction": direction, "score": min(score, 100),
            "momentum": momentum, "normalized_momentum": normalized,
            "body_ratio": body, "turn_distance": turn, "price": current,
            "candle_time": candle_time, "source": "Momentum 10"
        }, ""
    except (KeyError, TypeError, ValueError, OverflowError, IndexError, ZeroDivisionError):
        return None, "invalid candle data"
