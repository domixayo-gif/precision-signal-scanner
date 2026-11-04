"""Closed-candle 5-minute trend confirmation for the breakout/retest scanner."""


def _ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = alpha * price + (1.0 - alpha) * value
    return value


def classify_trend(candles):
    """Return 'up', 'down', or 'sideways' using only closed 5-minute candles.

    A directional trend requires EMA20/EMA50 alignment, a rising/falling
    EMA20 over five bars, and the latest close on the matching side of EMA20.
    Anything mixed or insufficient is sideways and blocks entries.
    """
    if not isinstance(candles, list) or len(candles) < 55:
        return "sideways"
    try:
        closes = [float(c["close"]) for c in candles]
        if any(not (price > 0 and price < float("inf")) for price in closes):
            return "sideways"
        ema20_now = _ema(closes, 20)
        ema50_now = _ema(closes, 50)
        ema20_previous = _ema(closes[:-5], 20)
        if None in (ema20_now, ema50_now, ema20_previous):
            return "sideways"
        latest_close = closes[-1]
        if ema20_now > ema50_now and ema20_now > ema20_previous and latest_close > ema20_now:
            return "up"
        if ema20_now < ema50_now and ema20_now < ema20_previous and latest_close < ema20_now:
            return "down"
        return "sideways"
    except (KeyError, TypeError, ValueError, OverflowError):
        return "sideways"


def is_strong_trend(candles, trend=None):
    """Identify a strong directional trend from closed candles only."""
    if not isinstance(candles, list) or len(candles) < 55:
        return False
    try:
        closes = [float(c["close"]) for c in candles]
        ranges = [max(0.0, float(c["high"]) - float(c["low"])) for c in candles[-14:]]
        atr = sum(ranges) / len(ranges) if ranges else 0.0
        if atr <= 0:
            return False
        ema20_now = _ema(closes, 20)
        ema50_now = _ema(closes, 50)
        ema20_previous = _ema(closes[:-5], 20)
        if None in (ema20_now, ema50_now, ema20_previous):
            return False
        direction = trend or classify_trend(candles)
        aligned = ((direction == "up" and ema20_now > ema50_now and ema20_now > ema20_previous)
                   or (direction == "down" and ema20_now < ema50_now and ema20_now < ema20_previous))
        separation = abs(ema20_now - ema50_now) / atr
        slope = abs(ema20_now - ema20_previous) / atr
        return bool(aligned and separation >= 0.35 and slope >= 0.10)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return False
