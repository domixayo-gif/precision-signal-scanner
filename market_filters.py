from math import log10


def _ema(values, period):
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    out = [value]
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
        out.append(value)
    return out


def _true_range(candles, index):
    high = float(candles[index]["high"])
    low = float(candles[index]["low"])
    previous = float(candles[index - 1]["close"]) if index else float(candles[index]["close"])
    return max(high - low, abs(high - previous), abs(low - previous))


def choppiness_index(candles, period=14):
    if len(candles) < period + 1:
        return None
    end = len(candles)
    window = candles[end - period:end]
    tr_sum = sum(_true_range(candles, i) for i in range(end - period, end))
    span = max(float(c["high"]) for c in window) - min(float(c["low"]) for c in window)
    if span <= 0 or tr_sum <= 0:
        return 100.0
    return 100.0 * log10(tr_sum / span) / log10(period)


def evaluate_market_filters(one_minute, five_minute):
    try:
        if len(one_minute) < 30 or len(five_minute) < 52:
            return {"ready": False, "reason": "insufficient closed candles for market filters"}
        closes = [float(c["close"]) for c in five_minute]
        fast, slow = _ema(closes, 20), _ema(closes, 50)
        if len(fast) < 4 or not slow:
            return {"ready": False, "reason": "5-minute trend calculation unavailable"}
        if fast[-1] > slow[-1] and fast[-1] > fast[-4]:
            trend = "up"
        elif fast[-1] < slow[-1] and fast[-1] < fast[-4]:
            trend = "down"
        else:
            trend = "unclear"
        chop = choppiness_index(one_minute, 14)
        if chop is None:
            return {"ready": False, "reason": "consolidation calculation unavailable"}
        recent = [float(c["close"]) for c in one_minute[-15:]]
        path = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))
        efficiency = abs(recent[-1] - recent[0]) / path if path > 0 else 0.0
        return {"ready": True, "trend": trend, "choppiness": round(chop, 2),
                "efficiency": round(efficiency, 3),
                "consolidating": chop >= 61.8 or efficiency <= 0.15, "reason": ""}
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return {"ready": False, "reason": "market filter data invalid"}
