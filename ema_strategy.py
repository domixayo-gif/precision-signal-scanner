"""EMA 20/50 + ADX 14 + RSI 14 signal logic for the isolated E strategy."""


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = alpha * price + (1.0 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def adx(candles, period=14):
    """Wilder ADX calculated from high/low/close; returns None if not warmed up."""
    if len(candles) < (2 * period + 1):
        return None
    try:
        true_ranges, plus_dm, minus_dm = [], [], []
        for index in range(1, len(candles)):
            current, previous = candles[index], candles[index - 1]
            high, low = float(current["high"]), float(current["low"])
            previous_high, previous_low = float(previous["high"]), float(previous["low"])
            previous_close = float(previous["close"])
            up_move = high - previous_high
            down_move = previous_low - low
            true_ranges.append(max(high - low, abs(high - previous_close),
                                   abs(low - previous_close)))
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        smooth_tr = sum(true_ranges[:period])
        smooth_plus = sum(plus_dm[:period])
        smooth_minus = sum(minus_dm[:period])
        dx_values = []
        for index in range(period - 1, len(true_ranges)):
            if index >= period:
                smooth_tr = smooth_tr - smooth_tr / period + true_ranges[index]
                smooth_plus = smooth_plus - smooth_plus / period + plus_dm[index]
                smooth_minus = smooth_minus - smooth_minus / period + minus_dm[index]
            if smooth_tr <= 0:
                dx_values.append(0.0)
                continue
            plus_di = 100.0 * smooth_plus / smooth_tr
            minus_di = 100.0 * smooth_minus / smooth_tr
            denominator = plus_di + minus_di
            dx_values.append(100.0 * abs(plus_di - minus_di) / denominator
                             if denominator > 0 else 0.0)
        if len(dx_values) < period:
            return None
        value = sum(dx_values[:period]) / period
        for dx in dx_values[period:]:
            value = (value * (period - 1) + dx) / period
        return value
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def calculate_signal(asset, candles, candle_seconds=60):
    """Return a CALL/PUT only when EMA alignment, ADX strength and RSI agree."""
    if not isinstance(candles, list) or len(candles) < 55:
        return None, "insufficient closed candles"
    try:
        closes = [float(c["close"]) for c in candles]
        if any(not (value > 0 and value < float("inf")) for value in closes):
            return None, "invalid close prices"
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        rsi14 = rsi(closes, 14)
        adx14 = adx(candles, 14)
        if None in (ema20, ema50, rsi14, adx14):
            return None, "indicators not warmed up"
        if adx14 <= 20:
            return None, "ADX below or equal to 20"
        if ema20 > ema50 and 50 < rsi14 < 70:
            direction = "call"
        elif ema20 < ema50 and 30 < rsi14 < 50:
            direction = "put"
        elif ema20 > ema50:
            return None, "bullish trend but RSI outside CALL range"
        elif ema20 < ema50:
            return None, "bearish trend but RSI outside PUT range"
        else:
            return None, "EMA 20 and EMA 50 are equal"
        candle_time = int(candles[-1].get("from") or 0) + int(candle_seconds)
        if candle_time <= 0:
            return None, "invalid candle timestamp"
        return {
            "asset": str(asset), "direction": direction, "score": 0,
            "ema20": ema20, "ema50": ema50, "adx14": adx14, "rsi14": rsi14,
            "price": closes[-1], "candle_time": candle_time,
            "source": "EMA 20/50 + ADX 14 + RSI 14"
        }, ""
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return None, "invalid candle data"
