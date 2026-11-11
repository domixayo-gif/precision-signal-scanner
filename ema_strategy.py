"""EMA pullback strategy with warmed indicators and regime/extension guards.

Pure Python: accepts closed candles only and never connects to a broker.
"""
from math import isfinite, log10


EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14
CHOP_PERIOD = 25
ADX_MIN = 20.0
CHOP_MAX = 61.8
MAX_EXTENSION_ATR = 0.85
PULLBACK_TOLERANCE_ATR = 0.20
MIN_CANDLES = 260


def _valid_number(value):
    try:
        number = float(value)
        return isfinite(number)
    except (TypeError, ValueError, OverflowError):
        return False


def ema(values, period):
    """EMA seeded with the first-period SMA; needs a full warm-up window."""
    if period < 1 or len(values) < period:
        return None
    try:
        numbers = [float(value) for value in values]
        if not all(isfinite(value) for value in numbers):
            return None
        alpha = 2.0 / (period + 1.0)
        value = sum(numbers[:period]) / period
        for price in numbers[period:]:
            value = alpha * price + (1.0 - alpha) * value
        return value
    except (TypeError, ValueError, OverflowError):
        return None


def ema_series(values, period):
    """Return the seeded EMA at each index; None until the seed is available."""
    if period < 1:
        return []
    output = [None] * len(values)
    if len(values) < period:
        return output
    try:
        numbers = [float(value) for value in values]
        if not all(isfinite(value) for value in numbers):
            return output
        value = sum(numbers[:period]) / period
        output[period - 1] = value
        alpha = 2.0 / (period + 1.0)
        for index in range(period, len(numbers)):
            value = alpha * numbers[index] + (1.0 - alpha) * value
            output[index] = value
        return output
    except (TypeError, ValueError, OverflowError):
        return output


def rsi(values, period=RSI_PERIOD):
    """Wilder RSI with a defined 50 reading for a completely flat series."""
    if period < 1 or len(values) < period + 1:
        return None
    try:
        numbers = [float(value) for value in values]
        if not all(isfinite(value) for value in numbers):
            return None
        changes = [numbers[i] - numbers[i - 1] for i in range(1, len(numbers))]
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
        return 100.0 - 100.0 / (1.0 + relative_strength)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def _directional_series(candles, period=ADX_PERIOD):
    """Wilder +DI, -DI, ADX; returns the latest values or None."""
    if not isinstance(candles, list) or period < 2 or len(candles) < 2 * period + 1:
        return None
    try:
        tr, plus_dm, minus_dm = [], [], []
        for index in range(1, len(candles)):
            current, previous = candles[index], candles[index - 1]
            high, low = float(current["high"]), float(current["low"])
            previous_high = float(previous["high"])
            previous_low = float(previous["low"])
            previous_close = float(previous["close"])
            if not all(isfinite(v) for v in (high, low, previous_high, previous_low, previous_close)):
                return None
            if high < low:
                return None
            up_move = high - previous_high
            down_move = previous_low - low
            tr.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        smooth_tr = sum(tr[:period])
        smooth_plus = sum(plus_dm[:period])
        smooth_minus = sum(minus_dm[:period])
        dx_values = []
        plus_di = minus_di = 0.0
        for index in range(period - 1, len(tr)):
            if index >= period:
                smooth_tr += tr[index] - smooth_tr / period
                smooth_plus += plus_dm[index] - smooth_plus / period
                smooth_minus += minus_dm[index] - smooth_minus / period
            if smooth_tr <= 0:
                plus_di = minus_di = 0.0
            else:
                plus_di = 100.0 * smooth_plus / smooth_tr
                minus_di = 100.0 * smooth_minus / smooth_tr
            denominator = plus_di + minus_di
            dx_values.append(100.0 * abs(plus_di - minus_di) / denominator
                             if denominator > 0 else 0.0)

        if len(dx_values) < period:
            return None
        adx_value = sum(dx_values[:period]) / period
        for dx in dx_values[period:]:
            adx_value = (adx_value * (period - 1) + dx) / period
        return plus_di, minus_di, adx_value
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def adx(candles, period=ADX_PERIOD):
    """Compatibility helper returning only ADX."""
    values = _directional_series(candles, period)
    return values[2] if values is not None else None


def atr(candles, period=ATR_PERIOD):
    """Wilder ATR using true range and a period-sized initial mean."""
    if not isinstance(candles, list) or period < 1 or len(candles) < period + 1:
        return None
    try:
        ranges = []
        for index in range(1, len(candles)):
            current, previous = candles[index], candles[index - 1]
            high, low = float(current["high"]), float(current["low"])
            previous_close = float(previous["close"])
            values = (high, low, previous_close)
            if not all(isfinite(value) for value in values) or high < low:
                return None
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        value = sum(ranges[:period]) / period
        for current_range in ranges[period:]:
            value = (value * (period - 1) + current_range) / period
        return value if isfinite(value) and value > 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def choppiness_index(candles, period=CHOP_PERIOD):
    """Standard Choppiness Index; high values indicate a range/chop regime."""
    if not isinstance(candles, list) or period < 2 or len(candles) < period + 1:
        return None
    try:
        window = candles[-period:]
        true_ranges = []
        for index in range(len(candles) - period, len(candles)):
            current = candles[index]
            previous_close = float(candles[index - 1]["close"])
            high, low = float(current["high"]), float(current["low"])
            if not all(isfinite(value) for value in (previous_close, high, low)) or high < low:
                return None
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        highest = max(float(candle["high"]) for candle in window)
        lowest = min(float(candle["low"]) for candle in window)
        total_range = highest - lowest
        total_tr = sum(true_ranges)
        if total_range <= 0 or total_tr <= 0:
            return None
        result = 100.0 * log10(total_tr / total_range) / log10(period)
        return result if isfinite(result) else None
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def _five_minute_direction(candles):
    """Confirm higher-timeframe EMA20/50 alignment and EMA20 slope."""
    if not isinstance(candles, list) or len(candles) < 80:
        return None
    try:
        closes = [float(c["close"]) for c in candles]
        if not all(isfinite(value) and value > 0 for value in closes):
            return None
        fast_now, slow_now = ema(closes, 20), ema(closes, 50)
        fast_previous = ema(closes[:-5], 20)
        if None in (fast_now, slow_now, fast_previous):
            return None
        if fast_now > slow_now and fast_now > fast_previous and closes[-1] > fast_now:
            return "call"
        if fast_now < slow_now and fast_now < fast_previous and closes[-1] < fast_now:
            return "put"
        return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None



def current_trend_direction(candles, candles_5m):
    """Return the currently confirmed EMA trend direction, without requiring a new pullback.

    Used as a last-moment execution guard, especially before same-direction
    martingale recovery orders. Returns None when the current regime is weak,
    choppy, malformed, or disagrees with the closed 5-minute trend.
    """
    if not isinstance(candles, list) or len(candles) < MIN_CANDLES:
        return None
    if not isinstance(candles_5m, list) or len(candles_5m) < 80:
        return None
    try:
        closes = [float(candle["close"]) for candle in candles]
        if not all(isfinite(value) and value > 0 for value in closes):
            return None
        fast_values = ema_series(closes, EMA_FAST)
        slow_values = ema_series(closes, EMA_SLOW)
        fast_now, slow_now = fast_values[-1], slow_values[-1]
        fast_previous, slow_previous = fast_values[-6], slow_values[-6]
        directional = _directional_series(candles, ADX_PERIOD)
        atr_value = atr(candles, ATR_PERIOD)
        chop_value = choppiness_index(candles, CHOP_PERIOD)
        if None in (fast_now, slow_now, fast_previous, slow_previous,
                    directional, atr_value, chop_value):
            return None
        plus_di, minus_di, adx_value = directional
        if adx_value < ADX_MIN or chop_value >= CHOP_MAX:
            return None

        bullish = (fast_now > slow_now and fast_now > fast_previous
                   and slow_now >= slow_previous and plus_di > minus_di)
        bearish = (fast_now < slow_now and fast_now < fast_previous
                   and slow_now <= slow_previous and minus_di > plus_di)
        if bullish and _five_minute_direction(candles_5m) == "call":
            return "call"
        if bearish and _five_minute_direction(candles_5m) == "put":
            return "put"
        return None
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return None


def calculate_signal(asset, candles, candle_seconds=60, candles_5m=None):
    """Signal only on a fresh EMA20 pullback bounce in a directional market.

    Candles must be closed and ordered oldest -> newest. No signal is emitted
    for insufficient warm-up, malformed data, weak/choppy trend, wrong DI
    direction, failed pullback, or an overextended entry.
    """
    if not isinstance(candles, list) or len(candles) < MIN_CANDLES:
        return None, f"insufficient closed candles (need {MIN_CANDLES})"
    if not isinstance(candles_5m, list) or len(candles_5m) < 80:
        return None, "insufficient closed 5-minute trend candles (need 80)"
    try:
        required = ("open", "high", "low", "close", "from")
        previous_stamp = None
        for candle in candles:
            if not isinstance(candle, dict) or any(key not in candle for key in required):
                return None, "invalid candle data"
            if not all(_valid_number(candle[key]) for key in required):
                return None, "invalid candle data"
            opening, high, low, close = (float(candle[k]) for k in ("open", "high", "low", "close"))
            if min(opening, high, low, close) <= 0 or high < max(opening, close) or low > min(opening, close) or high < low:
                return None, "invalid candle OHLC"
            stamp = int(float(candle["from"]))
            if stamp <= 0 or (previous_stamp is not None and stamp <= previous_stamp):
                return None, "invalid or unordered candle timestamps"
            previous_stamp = stamp

        closes = [float(candle["close"]) for candle in candles]
        ema20_values = ema_series(closes, EMA_FAST)
        ema50_values = ema_series(closes, EMA_SLOW)
        ema20_now, ema50_now = ema20_values[-1], ema50_values[-1]
        ema20_prev, ema50_prev = ema20_values[-6], ema50_values[-6]
        rsi_value = rsi(closes, RSI_PERIOD)
        directional = _directional_series(candles, ADX_PERIOD)
        atr_value = atr(candles, ATR_PERIOD)
        chop_value = choppiness_index(candles, CHOP_PERIOD)
        if None in (ema20_now, ema50_now, ema20_prev, ema50_prev,
                    rsi_value, directional, atr_value, chop_value):
            return None, "indicators not warmed up"

        plus_di, minus_di, adx_value = directional
        if adx_value < ADX_MIN:
            return None, "ADX below 20: trend strength too weak"
        if chop_value >= CHOP_MAX:
            return None, "CHOP25 indicates a sideways market"
        latest = candles[-1]
        opening, high, low, close = (float(latest[k]) for k in ("open", "high", "low", "close"))
        extension = abs(close - ema20_now) / atr_value
        if extension > MAX_EXTENSION_ATR:
            return None, "price is overextended from EMA20"

        bullish_trend = (ema20_now > ema50_now and ema20_now > ema20_prev
                         and ema50_now >= ema50_prev and plus_di > minus_di)
        bearish_trend = (ema20_now < ema50_now and ema20_now < ema20_prev
                         and ema50_now <= ema50_prev and minus_di > plus_di)
        tolerance = atr_value * PULLBACK_TOLERANCE_ATR

        if bullish_trend:
            touched = low <= ema20_now + tolerance
            bounced = close > ema20_now and close > opening
            if not (touched and bounced):
                return None, "no bullish EMA20 pullback-and-bounce candle"
            if not 50.0 < rsi_value < 70.0:
                return None, "RSI outside bullish continuation range"
            direction = "call"
        elif bearish_trend:
            touched = high >= ema20_now - tolerance
            bounced = close < ema20_now and close < opening
            if not (touched and bounced):
                return None, "no bearish EMA20 pullback-and-rejection candle"
            if not 30.0 < rsi_value < 50.0:
                return None, "RSI outside bearish continuation range"
            direction = "put"
        else:
            return None, "EMA alignment, slope, or +DI/-DI direction disagrees"

        higher_timeframe = _five_minute_direction(candles_5m)
        if higher_timeframe != direction:
            return None, f"5-minute trend mismatch or sideways (expected {direction})"

        candle_time = int(float(latest["from"])) + int(candle_seconds)
        if candle_time <= 0 or int(candle_seconds) <= 0:
            return None, "invalid candle timestamp"
        return {
            "asset": str(asset), "direction": direction, "score": 0,
            "ema20": ema20_now, "ema50": ema50_now,
            "adx14": adx_value, "plus_di14": plus_di, "minus_di14": minus_di,
            "rsi14": rsi_value, "atr14": atr_value, "chop25": chop_value,
            "extension_atr": extension, "trend_5m": higher_timeframe, "price": close, "candle_time": candle_time,
            "source": "EMA20/50 pullback + ADX/DI + CHOP25/ATR + 5-minute EMA alignment",
        }, ""
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return None, "invalid candle data"
