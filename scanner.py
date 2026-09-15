import os
import json
import time
from datetime import datetime, timezone

import requests


# ============================================================
# V3 PRECISION SIGNAL SCANNER
# 5M trend + 1M entry confirmation
# Reference expiry: 10 minutes
#
# IMPORTANT:
# - This scanner analyzes Coinbase spot data as a market-data proxy.
# - It does NOT connect to or execute trades on Pocket Option.
# - A score is setup quality, NOT a probability of winning.
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TRACKER_FILE = "tracker.json"

COINBASE_BASE = "https://api.exchange.coinbase.com"

# Keep the current asset universe from the existing scanner.
# These are Coinbase spot symbols, NOT Pocket Option OTC prices.
ASSETS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "BNB-USD",
    "ADA-USD",
    "TRX-USD",
    "LINK-USD",
    "TON-USD",
    "AVAX-USD",
    "DOGE-USD",
    "DOT-USD",
    "LTC-USD",
    "POL-USD",
]

TIMEFRAME_MAIN = 300   # 5 minutes
TIMEFRAME_ENTRY = 60   # 1 minute

MIN_SCORE = 80
MAX_SCORE = 100

REQUEST_TIMEOUT = 15
MAX_TRACKER_ITEMS = 500


# ============================================================
# BASIC HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are missing. Signal was not sent.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Telegram error: {exc}")
        return False


# ============================================================
# TRACKER
# ============================================================

def load_tracker():
    if not os.path.exists(TRACKER_FILE):
        return []

    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            return data

        return []
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read tracker.json: {exc}")
        return []


def save_tracker(data):
    # Keep the file from growing forever.
    data = data[-MAX_TRACKER_ITEMS:]

    with open(TRACKER_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def signal_already_recorded(tracker, asset, signal, candle_time):
    """
    Prevent duplicate alerts for the same asset/direction/candle.
    """
    for item in reversed(tracker):
        if (
            item.get("asset") == asset
            and item.get("signal") == signal
            and item.get("candle_time") == candle_time
        ):
            return True

    return False


def record_signal(tracker, signal_data):
    tracker.append(signal_data)
    save_tracker(tracker)


# ============================================================
# MARKET DATA
# ============================================================

def get_candles(product_id, granularity, limit=200):
    """
    Coinbase Exchange candles:
    [time, low, high, open, close, volume]
    """
    url = f"{COINBASE_BASE}/products/{product_id}/candles"

    try:
        response = requests.get(
            url,
            params={"granularity": granularity},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "precision-signal-scanner/3.0"},
        )
        response.raise_for_status()
        raw = response.json()

        candles = []

        for row in raw:
            if len(row) < 6:
                continue

            candles.append(
                {
                    "time": int(row[0]),
                    "low": safe_float(row[1]),
                    "high": safe_float(row[2]),
                    "open": safe_float(row[3]),
                    "close": safe_float(row[4]),
                    "volume": safe_float(row[5]),
                }
            )

        candles.sort(key=lambda x: x["time"])

        # Remove an incomplete final candle where possible.
        current_ts = int(time.time())
        candles = [
            candle
            for candle in candles
            if candle["time"] + granularity <= current_ts
        ]

        return candles[-limit:]

    except (requests.RequestException, ValueError) as exc:
        print(f"{product_id} data error ({granularity}s): {exc}")
        return []


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result[period - 1] = seed

    previous = seed

    for i in range(period, len(values)):
        current = (values[i] - previous) * multiplier + previous
        result[i] = current
        previous = current

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = [0.0] * len(values)
    losses = [0.0] * len(values)

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def calc_rsi(gain, loss):
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = calc_rsi(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        result[i] = calc_rsi(avg_gain, avg_loss)

    return result


def true_ranges(candles):
    tr = [0.0] * len(candles)

    for i, candle in enumerate(candles):
        if i == 0:
            tr[i] = candle["high"] - candle["low"]
            continue

        previous_close = candles[i - 1]["close"]

        tr[i] = max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close),
        )

    return tr


def atr(candles, period=14):
    trs = true_ranges(candles)
    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    value = sum(trs[1:period + 1]) / period
    result[period] = value

    for i in range(period + 1, len(candles)):
        value = ((value * (period - 1)) + trs[i]) / period
        result[i] = value

    return result


def adx_dmi(candles, period=14):
    """
    Wilder-style ADX / DMI calculation.
    Returns +DI, -DI and ADX arrays.
    """
    n = len(candles)

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = true_ranges(candles)

    for i in range(1, n):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

    plus_di = [None] * n
    minus_di = [None] * n
    adx = [None] * n

    if n <= period * 2:
        return plus_di, minus_di, adx

    # Initial Wilder sums.
    tr_sum = sum(tr[1:period + 1])
    plus_sum = sum(plus_dm[1:period + 1])
    minus_sum = sum(minus_dm[1:period + 1])

    dx_values = []

    for i in range(period, n):
        if i > period:
            tr_sum = tr_sum - (tr_sum / period) + tr[i]
            plus_sum = plus_sum - (plus_sum / period) + plus_dm[i]
            minus_sum = minus_sum - (minus_sum / period) + minus_dm[i]

        if tr_sum == 0:
            plus = 0.0
            minus = 0.0
        else:
            plus = 100.0 * plus_sum / tr_sum
            minus = 100.0 * minus_sum / tr_sum

        plus_di[i] = plus
        minus_di[i] = minus

        denominator = plus + minus

        if denominator == 0:
            dx = 0.0
        else:
            dx = 100.0 * abs(plus - minus) / denominator

        dx_values.append(dx)

        # Need 'period' DX values for the initial ADX.
        if len(dx_values) == period:
            adx[i] = sum(dx_values) / period

        elif len(dx_values) > period:
            previous_adx = adx[i - 1]

            if previous_adx is not None:
                adx[i] = (
                    (previous_adx * (period - 1)) + dx
                ) / period

    return plus_di, minus_di, adx


def macd(values, fast_period=12, slow_period=26, signal_period=9):
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)

    line = [None] * len(values)

    for i in range(len(values)):
        if fast[i] is not None and slow[i] is not None:
            line[i] = fast[i] - slow[i]

    valid_macd = [x for x in line if x is not None]
    signal_valid = ema(valid_macd, signal_period)

    signal = [None] * len(values)

    start = len(values) - len(valid_macd)

    for j, value in enumerate(signal_valid):
        if value is not None:
            signal[start + j] = value

    histogram = [None] * len(values)

    for i in range(len(values)):
        if line[i] is not None and signal[i] is not None:
            histogram[i] = line[i] - signal[i]

    return line, signal, histogram


# ============================================================
# PRICE ACTION / MARKET STRUCTURE
# ============================================================

def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "BULL"
    if candle["close"] < candle["open"]:
        return "BEAR"
    return "DOJI"


def candle_body_ratio(candle):
    full_range = candle["high"] - candle["low"]

    if full_range <= 0:
        return 0.0

    return abs(candle["close"] - candle["open"]) / full_range


def bullish_confirmation(candles):
    """
    Entry confirmation on the latest closed 1M candle.

    Strong bullish candle OR bullish engulfing OR a higher close
    after a short pullback.
    """
    if len(candles) < 5:
        return False

    current = candles[-1]
    previous = candles[-2]
    two_back = candles[-3]

    strong_body = (
        candle_direction(current) == "BULL"
        and candle_body_ratio(current) >= 0.55
        and current["close"] > previous["close"]
    )

    bullish_engulfing = (
        candle_direction(previous) == "BEAR"
        and candle_direction(current) == "BULL"
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )

    pullback_reclaim = (
        previous["low"] <= two_back["low"]
        and current["close"] > previous["high"]
    )

    return strong_body or bullish_engulfing or pullback_reclaim


def bearish_confirmation(candles):
    if len(candles) < 5:
        return False

    current = candles[-1]
    previous = candles[-2]
    two_back = candles[-3]

    strong_body = (
        candle_direction(current) == "BEAR"
        and candle_body_ratio(current) >= 0.55
        and current["close"] < previous["close"]
    )

    bearish_engulfing = (
        candle_direction(previous) == "BULL"
        and candle_direction(current) == "BEAR"
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )

    pullback_reclaim = (
        previous["high"] >= two_back["high"]
        and current["close"] < previous["low"]
    )

    return strong_body or bearish_engulfing or pullback_reclaim


def market_structure(candles, lookback=20):
    """
    Simple structure test using halves of the recent window.
    It avoids pretending to identify every swing point perfectly.
    """
    if len(candles) < lookback:
        return "NEUTRAL"

    recent = candles[-lookback:]

    midpoint = lookback // 2
    first = recent[:midpoint]
    second = recent[midpoint:]

    first_high = max(c["high"] for c in first)
    second_high = max(c["high"] for c in second)

    first_low = min(c["low"] for c in first)
    second_low = min(c["low"] for c in second)

    if second_high > first_high and second_low > first_low:
        return "BULL"

    if second_high < first_high and second_low < first_low:
        return "BEAR"

    return "NEUTRAL"


def recent_levels(candles, lookback=30):
    if len(candles) < lookback + 1:
        return None, None

    # Exclude the latest candle so the level represents prior structure.
    window = candles[-(lookback + 1):-1]

    resistance = max(c["high"] for c in window)
    support = min(c["low"] for c in window)

    return support, resistance


# ============================================================
# SCORING
# ============================================================

def analyze_asset(asset):
    candles_5m = get_candles(asset, TIMEFRAME_MAIN, 220)
    candles_1m = get_candles(asset, TIMEFRAME_ENTRY, 220)

    if len(candles_5m) < 100 or len(candles_1m) < 100:
        return {
            "signal": "NO TRADE",
            "score": 0,
            "reason": "Insufficient market data",
        }

    close_5 = [c["close"] for c in candles_5m]
    close_1 = [c["close"] for c in candles_1m]

    ema20_5 = ema(close_5, 20)
    ema50_5 = ema(close_5, 50)
    rsi_5 = rsi(close_5, 14)
    macd_5, macd_signal_5, macd_hist_5 = macd(close_5)
    atr_5 = atr(candles_5m, 14)
    plus_di_5, minus_di_5, adx_5 = adx_dmi(candles_5m, 14)

    ema9_1 = ema(close_1, 9)
    ema21_1 = ema(close_1, 21)
    rsi_1 = rsi(close_1, 14)
    macd_1, macd_signal_1, macd_hist_1 = macd(close_1)

    i5 = len(candles_5m) - 1
    i1 = len(candles_1m) - 1

    required = [
        ema20_5[i5],
        ema50_5[i5],
        rsi_5[i5],
        macd_hist_5[i5],
        atr_5[i5],
        plus_di_5[i5],
        minus_di_5[i5],
        adx_5[i5],
        ema9_1[i1],
        ema21_1[i1],
        rsi_1[i1],
        macd_hist_1[i1],
    ]

    if any(value is None for value in required):
        return {
            "signal": "NO TRADE",
            "score": 0,
            "reason": "Indicators not ready",
        }

    price = close_5[i5]
    price_1m = close_1[i1]

    e20 = ema20_5[i5]
    e50 = ema50_5[i5]
    rsi5 = rsi_5[i5]
    hist5 = macd_hist_5[i5]
    atr5 = atr_5[i5]
    pdi = plus_di_5[i5]
    mdi = minus_di_5[i5]
    adx = adx_5[i5]

    e9 = ema9_1[i1]
    e21 = ema21_1[i1]
    rsi1 = rsi_1[i1]
    hist1 = macd_hist_1[i1]

    previous_e20 = ema20_5[i5 - 3]
    previous_e50 = ema50_5[i5 - 3]
    previous_rsi5 = rsi_5[i5 - 1]
    previous_adx = adx_5[i5 - 1] if adx_5[i5 - 1] is not None else adx

    structure = market_structure(candles_5m, 20)
    support, resistance = recent_levels(candles_5m, 30)

    if support is None or resistance is None:
        return {
            "signal": "NO TRADE",
            "score": 0,
            "reason": "Support/resistance unavailable",
        }

    # --------------------------------------------------------
    # TREND CONDITIONS
    # --------------------------------------------------------

    bull_trend = price > e20 > e50
    bear_trend = price < e20 < e50

    ema20_rising = e20 > previous_e20
    ema20_falling = e20 < previous_e20

    ema50_rising = e50 > previous_e50
    ema50_falling = e50 < previous_e50

    bull_momentum = hist5 > 0
    bear_momentum = hist5 < 0

    bull_dmi = pdi > mdi
    bear_dmi = mdi > pdi

    strong_trend = adx >= 20
    very_strong_trend = adx >= 25

    bull_structure = structure == "BULL"
    bear_structure = structure == "BEAR"

    # --------------------------------------------------------
    # ENTRY CONDITIONS
    # --------------------------------------------------------

    bull_1m_trend = price_1m > e9 > e21
    bear_1m_trend = price_1m < e9 < e21

    bull_1m_momentum = hist1 > 0
    bear_1m_momentum = hist1 < 0

    bull_rsi = 50 < rsi5 < 70 and rsi5 >= previous_rsi5
    bear_rsi = 30 < rsi5 < 50 and rsi5 <= previous_rsi5

    bull_entry_rsi = 50 <= rsi1 < 75
    bear_entry_rsi = 25 < rsi1 <= 50

    bull_candle = bullish_confirmation(candles_1m)
    bear_candle = bearish_confirmation(candles_1m)

    # --------------------------------------------------------
    # PULLBACK / ENTRY TIMING
    # --------------------------------------------------------

    recent_1m = candles_1m[-6:]

    pulled_back_to_ema_bull = any(
        c["low"] <= e9 * 1.0015
        for c in recent_1m[:-1]
    )

    pulled_back_to_ema_bear = any(
        c["high"] >= e9 * 0.9985
        for c in recent_1m[:-1]
    )

    # A pullback is preferred, but a clean continuation candle
    # is still allowed if price is not extended.
    pullback_bull = pulled_back_to_ema_bull or bull_candle
    pullback_bear = pulled_back_to_ema_bear or bear_candle

    # --------------------------------------------------------
    # ANTI-CHASE / EXTENSION FILTER
    # --------------------------------------------------------

    extension_atr = abs(price - e20) / atr5 if atr5 > 0 else 999

    not_overextended = extension_atr <= 1.8

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE "ROOM TO MOVE" FILTER
    # --------------------------------------------------------

    range_size = max(resistance - support, atr5)

    room_to_resistance = (
        resistance - price
        if resistance > price
        else 0
    )

    room_to_support = (
        price - support
        if support < price
        else 0
    )

    # Require at least roughly 0.35 ATR of room in the direction
    # of the expected move, unless the structure level is very far.
    bull_room = room_to_resistance >= atr5 * 0.35
    bear_room = room_to_support >= atr5 * 0.35

    # If the market range is tiny relative to ATR, treat it as poor.
    healthy_range = range_size >= atr5 * 1.5

    # --------------------------------------------------------
    # CHOP FILTER
    # --------------------------------------------------------

    ema_gap = abs(e20 - e50) / atr5 if atr5 > 0 else 0

    not_too_flat = ema_gap >= 0.15

    # Avoid the RSI middle zone where direction is often unclear.
    rsi_not_choppy_bull = rsi5 >= 53
    rsi_not_choppy_bear = rsi5 <= 47

    # --------------------------------------------------------
    # WEIGHTED QUALITY SCORE
    # --------------------------------------------------------

    bull_score = 0
    bear_score = 0

    bull_reasons = []
    bear_reasons = []

    # 25 points: main trend
    if bull_trend:
        bull_score += 15
        bull_reasons.append("5M EMA trend bullish")

    if ema20_rising and ema50_rising:
        bull_score += 10
        bull_reasons.append("5M EMAs rising")

    if bear_trend:
        bear_score += 15
        bear_reasons.append("5M EMA trend bearish")

    if ema20_falling and ema50_falling:
        bear_score += 10
        bear_reasons.append("5M EMAs falling")

    # 15 points: market structure
    if bull_structure:
        bull_score += 15
        bull_reasons.append("Higher-high/higher-low structure")

    if bear_structure:
        bear_score += 15
        bear_reasons.append("Lower-high/lower-low structure")

    # 15 points: DMI/ADX
    if bull_dmi:
        bull_score += 8
        bull_reasons.append("+DI above -DI")

    if bear_dmi:
        bear_score += 8
        bear_reasons.append("-DI above +DI")

    if strong_trend:
 
