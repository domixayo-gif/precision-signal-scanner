import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# PRECISION IQ OPTION OTC SCANNER V3
# ============================================================
# READ-ONLY SCANNER
# - NO automatic trading
# - Discovers OTC instruments dynamically
# - Does NOT hard-code fake OTC pairs
# - 5M trend + 1M entry
# - 5-minute reference expiry
# - Telegram alerts
# - 5-minute per-asset lock
# ============================================================


# ============================================================
# CONFIG
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# Strategy
MIN_SCORE = 80
MIN_ADX = 18.0

MIN_CANDLE_STRENGTH = 0.40
MAX_EXTENSION_ATR = 2.80
MIN_ROOM_ATR = 0.60

MIN_CONFIRMATIONS = 4
MIN_SCORE_GAP = 10

# Candle counts
CANDLE_COUNT_5M = 160
CANDLE_COUNT_1M = 160

# Lock
LOCK_SECONDS = 300

# Maximum assets to test
MAX_OTC_ASSETS = 60

# Reference expiry
EXPIRY_MINUTES = 5

# Practice only
BALANCE_MODE = "PRACTICE"


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=0.0):
    try:
        value = float(value)
        if math.isfinite(value):
            return value
    except Exception:
        pass
    return default


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n[TELEGRAM] Token/chat ID missing.")
        print(text)
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if response.ok:
            return True

        print(
            "[TELEGRAM ERROR]",
            response.status_code,
            response.text[:500],
        )

    except Exception as e:
        print("[TELEGRAM EXCEPTION]", e)

    return False


# ============================================================
# DATA CLEANING
# ============================================================

def normalize_candles(raw):
    """
    Converts IQ Option candle dictionaries into a clean list.

    Keeps:
        from, open, close, min, max, volume
    """

    if not raw:
        return []

    result = []

    for c in raw:
        try:
            item = {
                "from": safe_float(c.get("from")),
                "open": safe_float(c.get("open")),
                "close": safe_float(c.get("close")),
                "low": safe_float(
                    c.get("min", c.get("low"))
                ),
                "high": safe_float(
                    c.get("max", c.get("high"))
                ),
                "volume": safe_float(c.get("volume")),
            }

            if (
                item["open"] > 0
                and item["close"] > 0
                and item["low"] > 0
                and item["high"] > 0
            ):
                result.append(item)

        except Exception:
            continue

    result.sort(key=lambda x: x["from"])

    return result


def remove_open_candle(candles, timeframe_seconds):
    """
    Remove the currently forming candle.

    IQ Option's get_candles() can be delayed, so we use
    completed candles only for the signal calculation.
    """

    if len(candles) < 3:
        return candles

    current_time = time.time()

    result = []

    for candle in candles:
        candle_start = candle["from"]

        if candle_start + timeframe_seconds <= current_time:
            result.append(candle)

    return result


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1.0)

    result = [None] * len(values)

    seed = sum(values[:period]) / period
    result[period - 1] = seed

    previous = seed

    for i in range(period, len(values)):
        previous = (
            (values[i] - previous) * multiplier
            + previous
        )
        result[i] = previous

    return result


def sma(values, period):
    if len(values) < period:
        return []

    result = [None] * len(values)

    running = sum(values[:period])
    result[period - 1] = running / period

    for i in range(period, len(values)):
        running += values[i]
        running -= values[i - period]
        result[i] = running / period

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [None] * len(values)

    def calc(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = calc(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

        result[i + 1] = calc(avg_gain, avg_loss)

    return result


def macd(values, fast_period=12, slow_period=26, signal_period=9):
    fast_line = ema(values, fast_period)
    slow_line = ema(values, slow_period)

    macd_line = [None] * len(values)

    valid = []

    for i in range(len(values)):
        if (
            fast_line[i] is not None
            and slow_line[i] is not None
        ):
            macd_line[i] = (
                fast_line[i] - slow_line[i]
            )
            valid.append(macd_line[i])

    signal_values = ema(valid, signal_period)

    signal_line = [None] * len(values)

    valid_index = 0

    for i in range(len(values)):
        if macd_line[i] is not None:
            if valid_index < len(signal_values):
                signal_line[i] = signal_values[valid_index]
            valid_index += 1

    histogram = [None] * len(values)

    for i in range(len(values)):
        if (
            macd_line[i] is not None
            and signal_line[i] is not None
        ):
            histogram[i] = (
                macd_line[i] - signal_line[i]
            )

    return macd_line, signal_line, histogram


def atr(candles, period=14):
    if len(candles) < period + 1:
        return []

    true_ranges = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        true_ranges.append(tr)

    result = [None] * len(candles)

    current = sum(true_ranges[:period]) / period

    result[period] = current

    for i in range(period, len(true_ranges)):
        current = (
            ((current * (period - 1)) + true_ranges[i])
            / period
        )

        result[i + 1] = current

    return result


def adx_dmi(candles, period=14):
    if len(candles) < period * 2 + 5:
        return [], [], []

    tr_values = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_high = candles[i - 1]["high"]
        previous_low = candles[i - 1]["low"]
        previous_close = candles[i - 1]["close"]

        up_move = high - previous_high
        down_move = previous_low - low

        plus = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        tr_values.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(tr_values) < period:
        return [], [], []

    atr_smoothed = sum(tr_values[:period])
    plus_smoothed = sum(plus_dm[:period])
    minus_smoothed = sum(minus_dm[:period])

    dx_values = []
    plus_di_values = []
    minus_di_values = []

    for i in range(period, len(tr_values)):

        if i > period:
            atr_smoothed = (
                atr_smoothed
                - (atr_smoothed / period)
                + tr_values[i]
            )

            plus_smoothed = (
                plus_smoothed
                - (plus_smoothed / period)
                + plus_dm[i]
            )

            minus_smoothed = (
                minus_smoothed
                - (minus_smoothed / period)
                + minus_dm[i]
            )

        if atr_smoothed == 0:
            plus_di = 0
            minus_di = 0
        else:
            plus_di = (
                100.0 * plus_smoothed / atr_smoothed
            )

            minus_di = (
                100.0 * minus_smoothed / atr_smoothed
            )

        denominator = plus_di + minus_di

        if denominator == 0:
            dx = 0
        else:
            dx = (
                100.0
                * abs(plus_di - minus_di)
                / denominator
            )

        plus_di_values.append(plus_di)
        minus_di_values.append(minus_di)
        dx_values.append(dx)

    if len(dx_values) < period:
        return [], [], []

    adx_values = [None] * len(candles)
    plus_result = [None] * len(candles)
    minus_result = [None] * len(candles)

    first_adx = sum(dx_values[:period]) / period

    base_index = period + period - 1

    if base_index < len(candles):
        adx_values[base_index] = first_adx

        plus_result[base_index] = plus_di_values[period - 1]
        minus_result[base_index] = minus_di_values[period - 1]

    current_adx = first_adx

    for i in range(period, len(dx_values)):

        current_adx = (
            ((current_adx * (period - 1)) + dx_values[i])
            / period
        )

        candle_index = (
            period + i
        )

        if candle_index < len(candles):
            adx_values[candle_index] = current_adx

            plus_result[candle_index] = (
                plus_di_values[i]
            )

            minus_result[candle_index] = (
                minus_di_values[i]
            )

    return (
        adx_values,
        plus_result,
        minus_result,
    )


# ============================================================
# PRICE / STRUCTURE
# ============================================================

def candle_strength(candle):
    high = candle["high"]
    low = candle["low"]
    open_price = candle["open"]
    close = candle["close"]

    rng = high - low

    if rng <= 0:
        return 0.0

    body = abs(close - open_price)

    return body / rng


def market_structure(candles, lookback=20):
    if len(candles) < lookback:
        return "NEUTRAL"

    recent = candles[-lookback:]

    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    half = len(recent) // 2

    first_high = max(highs[:half])
    second_high = max(highs[half:])

    first_low = min(lows[:half])
    second_low = min(lows[half:])

    if (
        second_high > first_high
        and second_low > first_low
    ):
        return "BULLISH"

    if (
        second_high < first_high
        and second_low < first_low
    ):
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# ROBUST OTC DISCOVERY
# ============================================================

def is_otc_name(name):
    if not isinstance(name, str):
        return False

    upper = name.upper()

    return (
        "-OTC" in upper
        or "_OTC" in upper
        or " OTC" in upper
    )


def is_open_entry(value):
    """
    Handles different structures returned by
    different iqoptionapi versions.
    """

    if not isinstance(value, dict):
        return False

    # Normal documented format
    if value.get("open") is True:
        return True

    # Some versions/structures may expose enabled/open
    if value.get("enabled") is True:
        if value.get("is_suspended") is True:
            return False
        return True

    # Nested open information
    for key in (
        "status",
        "state",
        "market",
        "active",
    ):
        nested = value.get(key)

        if isinstance(nested, dict):
            if is_open_entry(nested):
                return True

    return False


def recursively_find_otc(obj, path="ROOT"):
    """
    Walk the entire get_all_open_time() response.

    This deliberately does not assume:
        digital/turbo/binary
    are the only possible locations.
    """

    found = []

    if isinstance(obj, dict):

        for key, value in obj.items():

            key_string = str(key)

            if is_otc_name(key_string):

                if is_open_entry(value):
                    found.append(
                        {
                            "asset": key_string,
                            "path": path,
                            "data": value,
                        }
                    )

            found.extend(
                recursively_find_otc(
                    value,
                    f"{path}/{key_string}",
                )
            )

    elif isinstance(obj, list):

        for index, value in enumerate(obj):

            found.extend(
                recursively_find_otc(
                    value,
                    f"{path}[{index}]",
                )
            )

    return found


def collect_market_summary(obj):
    """
    Creates a safe summary without dumping
    the entire API response.
    """

    summary = {}

    if not isinstance(obj, dict):
        return summary

    for key, value in obj.items():

        if isinstance(value, dict):
            summary[str(key)] = {
                "entries": len(value),
                "otc_names": sum(
                    1
                    for item_key in value.keys()
                    if is_otc_name(str(item_key))
                ),
                "open_otc": sum(
                    1
                    for item_key, item_value in value.items()
                    if (
                        is_otc_name(str(item_key))
                        and is_open_entry(item_value)
                    )
                ),
            }

        elif isinstance(value, list):
            summary[str(key)] = {
                "entries": len(value),
                "otc_names": 0,
                "open_otc": 0,
            }

        else:
            summary[str(key)] = {
                "entries": 1,
                "otc_names": 0,
                "open_otc": 0,
            }

    return summary


def discover_otc_assets(api):
    print("\n" + "=" * 70)
    print("IQ OPTION OTC DISCOVERY")
    print("=" * 70)

    try:
        all_open = api.get_all_open_time()
    except Exception as e:
        print("[DISCOVERY ERROR]", repr(e))
        return []

    if all_open is None:
        print("[DISCOVERY] get_all_open_time() returned None.")
        return []

    print(
        "[DISCOVERY] Response type:",
        type(all_open).__name__,
    )

    if isinstance(all_open, dict):
        print(
            "[DISCOVERY] Top-level categories:",
            ", ".join(str(k) for k in all_open.keys()),
        )

    summary = collect_market_summary(all_open)

    print("\n[MARKET SUMMARY]")

    for category, data in summary.items():
        print(
            f"  {category}: "
            f"{data['entries']} entries | "
            f"{data['otc_names']} OTC names | "
            f"{data['open_otc']} OPEN OTC"
        )

    raw_found = recursively_find_otc(all_open)

    # Deduplicate by asset name while preserving discovery order
    assets = []
    seen = set()

    print("\n[OTC CANDIDATES]")

    for item in raw_found:

        asset = item["asset"]

        if asset in seen:
            continue

        seen.add(asset)

        assets.append(asset)

        print(
            f"  {len(assets):02d}. "
            f"{asset} "
            f"| {item['path']}"
        )

    print(
        f"\n[DISCOVERY RESULT] "
        f"{len(assets)} unique OPEN OTC instruments."
    )

    if not assets:
        print(
            "\n[IMPORTANT] IQ Option returned no OTC instrument "
            "with an open=True/open-like status."
        )

        print(
            "[IMPORTANT] The strategy has NOT rejected these assets."
        )

        print(
            "[IMPORTANT] There is simply no discovered OTC "
            "instrument to scan."
        )

    return assets[:MAX_OTC_ASSETS]


# ============================================================
# CANDLE FETCH
# ============================================================

def get_candles_safe(api, asset, interval, count):
    try:

        end_time = time.time()

        raw = api.get_candles(
            asset,
            interval,
            count,
            end_time,
        )

        candles = normalize_candles(raw)

        candles = remove_open_candle(
            candles,
            interval,
        )

        return candles

    except Exception as e:

        print(
            f"[CANDLE ERROR] {asset} "
            f"{interval}s -> {repr(e)}"
        )

        return []


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_asset(api, asset):

    candles_5m = get_candles_safe(
        api,
        asset,
        300,
        CANDLE_COUNT_5M,
    )

    candles_1m = get_candles_safe(
        api,
        asset,
        60,
        CANDLE_COUNT_1M,
    )

    if len(candles_5m) < 80:
        return None, (
            f"{asset}: insufficient 5M candles "
            f"({len(candles_5m)})"
        )

    if len(candles_1m) < 80:
        return None, (
            f"{asset}: insufficient 1M candles "
            f"({len(candles_1m)})"
        )

    close_5m = [
        c["close"]
        for c in candles_5m
    ]

    close_1m = [
        c["close"]
        for c in candles_1m
    ]

    # --------------------------------------------------------
    # 5M
    # --------------------------------------------------------

    ema20_5 = ema(close_5m, 20)
    ema50_5 = ema(close_5m, 50)

    rsi_5 = rsi(close_5m, 14)

    macd_5, signal_5, hist_5 = macd(close_5m)

    adx_5, plus_di_5, minus_di_5 = adx_dmi(
        candles_5m,
        14,
    )

    atr_5 = atr(candles_5m, 14)

    # --------------------------------------------------------
    # 1M
    # --------------------------------------------------------

    ema9_1 = ema(close_1m, 9)
    ema21_1 = ema(close_1m, 21)

    rsi_1 = rsi(close_1m, 14)

    macd_1, signal_1, hist_1 = macd(close_1m)

    # --------------------------------------------------------
    # Latest valid values
    # --------------------------------------------------------

    def last_valid(values):
        for value in reversed(values):
            if value is not None:
                return value
        return None

    e20 = last_valid(ema20_5)
    e50 = last_valid(ema50_5)

    e9_1 = last_valid(ema9_1)
    e21_1 = last_valid(ema21_1)

    r5 = last_valid(rsi_5)
    r1 = last_valid(rsi_1)

    m5 = last_valid(macd_5)
    s5 = last_valid(signal_5)
    h5 = last_valid(hist_5)

    m1 = last_valid(macd_1)
    s1 = last_valid(signal_1)
    h1 = last_valid(hist_1)

    adx = last_valid(adx_5)
    plus_di = last_valid(plus_di_5)
    minus_di = last_valid(minus_di_5)

    current_atr = last_valid(atr_5)

    if any(
        x is None
        for x in (
            e20,
            e50,
            e9_1,
            e21_1,
            r5,
            r1,
            m5,
            s5,
            h5,
            m1,
            s1,
            h1,
            adx,
            plus_di,
            minus_di,
            current_atr,
        )
    ):
        return None, f"{asset}: indicator calculation incomplete"

    current_price = candles_1m[-1]["close"]

    latest_5m = candles_5m[-1]
    latest_1m = candles_1m[-1]

    strength_5 = candle_strength(latest_5m)
    strength_1 = candle_strength(latest_1m)

    candle_strength_avg = (
        strength_5 + strength_1
    ) / 2.0

    # --------------------------------------------------------
    # Trend
    # --------------------------------------------------------

    trend_5 = (
        "BULLISH"
        if e20 > e50
        else "BEARISH"
        if e20 < e50
        else "NEUTRAL"
    )

    entry_1 = (
        "BULLISH"
        if e9_1 > e21_1
        else "BEARISH"
        if e9_1 < e21_1
        else "NEUTRAL"
    )

    structure = market_structure(
        candles_5m,
        20,
    )

    # --------------------------------------------------------
    # Extension
    # --------------------------------------------------------

    extension_atr = 0.0

    if current_atr > 0:

        extension_atr = abs(
            current_price - e20
        ) / current_atr

    # --------------------------------------------------------
    # Room
    # --------------------------------------------------------

    lookback = candles_5m[-20:]

    resistance = max(
        c["high"]
        for c in lookback
    )

    support = min(
        c["low"]
        for c in lookback
    )

    if current_atr > 0:

        room_up = (
            resistance - current_price
        ) / current_atr

        room_down = (
            current_price - support
        ) / current_atr

    else:

        room_up = 0.0
        room_down = 0.0

    # --------------------------------------------------------
    # Hard filters
    # --------------------------------------------------------

    if adx < MIN_ADX:
        return None, (
            f"{asset}: NO TRADE | "
            f"ADX {adx:.1f} < {MIN_ADX:.1f}"
        )

    if extension_atr >= MAX_EXTENSION_ATR:
        return None, (
            f"{asset}: NO TRADE | "
            f"extension {extension_atr:.2f} ATR"
        )

    if candle_strength_avg < MIN_CANDLE_STRENGTH:
        return None, (
            f"{asset}: NO TRADE | "
            f"candle strength "
            f"{candle_strength_avg:.2f}"
        )

    # --------------------------------------------------------
    # Direction scores
    # --------------------------------------------------------

    call_score = 0
    put_score = 0

    call_confirmations = 0
    put_confirmations = 0

    # 5M trend — 20
    if trend_5 == "BULLISH":
        call_score += 20
        call_confirmations += 1

    elif trend_5 == "BEARISH":
        put_score += 20
        put_confirmations += 1

    # 1M entry — 15
    if entry_1 == "BULLISH":
        call_score += 15
        call_confirmations += 1

    elif entry_1 == "BEARISH":
        put_score += 15
        put_confirmations += 1

    # Structure — 15
    if structure == "BULLISH":
        call_score += 15
        call_confirmations += 1

    elif structure == "BEARISH":
        put_score += 15
        put_confirmations += 1

    # DMI — 10
    if plus_di > minus_di:
        call_score += 10
        call_confirmations += 1

    elif minus_di > plus_di:
        put_score += 10
        put_confirmations += 1

    # MACD — 10
    if (
        m5 > s5
        and h5 > 0
        and m1 > s1
        and h1 > 0
    ):
        call_score += 10
        call_confirmations += 1

    elif (
        m5 < s5
        and h5 < 0
        and m1 < s1
        and h1 < 0
    ):
        put_score += 10
        put_confirmations += 1

    # RSI — 10
    if (
        45 <= r5 <= 67
        and 45 <= r1 <= 67
    ):
        call_score += 10
        call_confirmations += 1

    elif (
        33 <= r5 <= 55
        and 33 <= r1 <= 55
    ):
        put_score += 10
        put_confirmations += 1

    # Candle — 10
    if (
        latest_5m["close"] > latest_5m["open"]
        and latest_1m["close"] > latest_1m["open"]
    ):
        call_score += 10
        call_confirmations += 1

    elif (
        latest_5m["close"] < latest_5m["open"]
        and latest_1m["close"] < latest_1m["open"]
    ):
        put_score += 10
        put_confirmations += 1

    # ADX quality — 5
    if adx >= 25:

        if call_score > put_score:
            call_score += 5

        elif put_score > call_score:
            put_score += 5

    # Extension penalty
    if extension_atr >= 2.0:

        if call_score > put_score:
            call_score -= 8

        elif put_score > call_score:
            put_score -= 8

    # Prevent negative
    call_score = max(0, call_score)
    put_score = max(0, put_score)

    # --------------------------------------------------------
    # Direction
    # --------------------------------------------------------

    if call_score > put_score:

        direction = "CALL"
        score = call_score
        confirmations = call_confirmations
        room = room_up

    elif put_score > call_score:

        direction = "PUT"
        score = put_score
        confirmations = put_confirmations
        room = room_down

    else:

        return None, (
            f"{asset}: NO TRADE | "
            f"CALL {call_score} / PUT {put_score}"
        )

    # --------------------------------------------------------
    # Final filters
    # --------------------------------------------------------

    if score < MIN_SCORE:
        return None, (
            f"{asset}: NO TRADE | "
            f"{direction} score {score} < {MIN_SCORE}"
        )

    if confirmations < MIN_CONFIRMATIONS:
        return None, (
            f"{asset}: NO TRADE | "
            f"confirmations "
            f"{confirmations} < {MIN_CONFIRMATIONS}"
        )

    opposite_score = (
        put_score
        if direction == "CALL"
        else call_score
    )

    if score - opposite_score < MIN_SCORE_GAP:
        return None, (
            f"{asset}: NO TRADE | "
            f"score gap {score - opposite_score} "
            f"< {MIN_SCORE_GAP}"
        )

    if room < MIN_ROOM_ATR:
        return None, (
            f"{asset}: NO TRADE | "
            f"room {room:.2f} ATR "
            f"< {MIN_ROOM_ATR:.2f}"
        )

    # --------------------------------------------------------
    # Qualified
    # --------------------------------------------------------

    signal_id = (
        f"{asset.replace('-', '')}-"
        f"{direction}-"
        f"{datetime.now(timezone.utc).strftime('%H%M%S')}"
    )

    result = {
        "signal_id": signal_id,
        "asset": asset,
        "direction": direction,
        "score": score,
        "price": current_price,
        "trend_5m": trend_5,
        "entry_1m": entry_1,
        "structure": structure,
        "adx": adx,
        "rsi_5m": r5,
        "rsi_1m": r1,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "extension_atr": extension_atr,
        "room_up": room_up,
        "room_down": room_down,
        "confirmations": confirmations,
        "timestamp": now_utc(),
    }

    return result, None


# ============================================================
# TELEGRAM SIGNAL
# ============================================================

def format_signal(signal):

    direction_emoji = (
        "🟢"
        if signal["direction"] == "CALL"
        else "🔴"
    )

    return (
        f"{direction_emoji} *NEW QUALIFIED OTC SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* `{signal['asset']}`\n"
        f"*Direction:* *{signal['direction']} / "
        f"{'UP' if signal['direction'] == 'CALL' else 'DOWN'}*\n"
        f"*Score:* *{signal['score']}/100*\n"
        f"*Reference expiry:* *{EXPIRY_MINUTES} minutes*\n"
        f"*Price:* `{signal['price']:.8f}`\n"
        f"*5M Trend:* `{signal['trend_5m']}`\n"
        f"*1M Entry:* `{signal['entry_1m']}`\n"
        f"*Structure:* `{signal['structure']}`\n"
        f"*ADX:* `{signal['adx']:.1f}`\n"
        f"*RSI 5M:* `{signal['rsi_5m']:.1f}`\n"
        f"*RSI 1M:* `{signal['rsi_1m']:.1f}`\n"
        f"*DI+:* `{signal['plus_di']:.1f}`\n"
        f"*DI-:* `{signal['minus_di']:.1f}`\n"
        f"*Extension:* `{signal['extension_atr']:.2f} ATR`\n"
        f"*Room Up:* `{signal['room_up']:.2f} ATR`\n"
        f"*Room Down:* `{signal['room_down']:.2f} ATR`\n"
        f"*Confirmations:* "
        f"`{signal['confirmations']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Signal ID:* `{signal['signal_id']}`\n"
        f"*Time:* `{signal['timestamp']}`\n"
        f"⚠️ *READ-ONLY SIGNAL — NO AUTO TRADE*"
    )


# ============================================================
# DIAGNOSTIC TELEGRAM
# ============================================================

def send_discovery_status(asset_count):

    if asset_count > 0:
        text = (
            "🟢 *IQ OPTION OTC SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"OTC instruments discovered: *{asset_count}*\n"
            "Starting precision scan.\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "READ-ONLY — NO AUTO TRADE"
        )

    else:
        text = (
            "🟡 *IQ OPTION OTC SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No currently open OTC instruments "
            "were discovered from IQ Option.\n\n"
            "The scanner inspected the complete "
            "market response instead of using a "
            "hard-coded OTC list.\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No strategy signals were generated."
        )

    send_telegram(text)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("PRECISION IQ OPTION OTC SCANNER V3")
    print("=" * 70)
    print("Started:", now_utc())
    print("Mode:", BALANCE_MODE)
    print("Automatic trading: DISABLED")
    print("=" * 70)

    if not IQ_EMAIL or not IQ_PASSWORD:

        message = (
            "🔴 *IQ OPTION SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )

        print(message)
        send_telegram(message)
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        print(
            "[WARNING] Telegram environment variables "
            "are missing."
        )

    api = None

    try:

        print("\n[1/5] Connecting to IQ Option...")

        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        check, reason = api.connect()

        if not check:

            print(
                "[LOGIN FAILED]",
                reason,
            )

            send_telegram(
                "🔴 *IQ OPTION SCANNER*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Connection/login failed.\n\n"
                f"`{reason}`"
            )

            return

        print("[LOGIN] Connection successful.")

        if hasattr(api, "check_connect"):

            if not api.check_connect():

                print(
                    "[LOGIN] API reports connection "
                    "is not active."
                )

                send_telegram(
                    "🔴 *IQ OPTION SCANNER*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "IQ Option connection is not active."
                )

                return

        print("\n[2/5] Switching to PRACTICE...")

        try:
            api.change_balance(BALANCE_MODE)
            print(
                "[BALANCE]",
                BALANCE_MODE,
            )
        except Exception as e:
            print(
                "[BALANCE WARNING]",
                repr(e),
            )

        print("\n[3/5] Discovering OTC instruments...")

        assets = discover_otc_assets(api)

        send_discovery_status(len(assets))

        if not assets:

            print(
                "\n[STOP] Zero OTC instruments discovered."
            )

            print(
                "[STOP] No strategy scan will run."
            )

            print(
                "[STOP] This is an OTC availability/"
                "API discovery issue, not a score issue."
            )

            return

        print("\n[4/5] Testing candle access...")

        working_assets = []

        # Test only a few first so discovery doesn't
        # hammer the API.
        test_assets = assets[:10]

        for asset in test_assets:

            print(
                f"[CANDLE TEST] {asset}"
            )

            candles = get_candles_safe(
                api,
                asset,
                60,
                20,
            )

            if len(candles) >= 5:

                print(
                    f"  OK — {len(candles)} candles"
                )

                working_assets.append(asset)

            else:

                print(
                    f"  FAILED — only "
                    f"{len(candles)} candles"
                )

        # If some assets worked, scan those first.
        # Otherwise try all discovered assets.
        if working_assets:

            scan_assets = working_assets + [
                a
                for a in assets
                if a not in working_assets
            ]

        else:

            scan_assets = assets

        scan_assets = scan_assets[:MAX_OTC_ASSETS]

        print(
            "\n[5/5] Starting precision scan..."
        )

        print(
            f"[SCAN] Assets selected: "
            f"{len(scan_assets)}"
        )

        print(
            f"[SCAN] Minimum score: "
            f"{MIN_SCORE}"
        )

        print(
            f"[SCAN] Minimum ADX: "
            f"{MIN_ADX}"
        )

        print(
            f"[SCAN] Expiry reference: "
            f"{EXPIRY_MINUTES} minutes"
        )

        print("-" * 70)

        locks = {}

        qualified_count = 0
        rejected_count = 0
        error_count = 0

        for asset in scan_assets:

            # Lock check
            last_scan = locks.get(asset, 0)

            if (
                time.time() - last_scan
                < LOCK_SECONDS
            ):
                continue

            locks[asset] = time.time()

            print(
                f"\n[SCAN] {asset}"
            )

            try:

                signal, reason = analyze_asset(
                    api,
                    asset,
                )

                if signal:

                    qualified_count += 1

                    print(
                        f"[QUALIFIED] "
                        f"{asset} "
                        f"{signal['direction']} "
                        f"score={signal['score']}"
                    )

                    message = format_signal(
                        signal
                    )

                    send_telegram(message)

                else:

                    rejected_count += 1

                    print(
                        f"[REJECTED] "
                        f"{reason}"
                    )

            except Exception as e:

                error_count += 1

                print(
                    f"[SCAN ERROR] "
                    f"{asset}: {repr(e)}"
                )

                traceback.print_exc()

        print("\n" + "=" * 70)
        print("SCAN COMPLETE")
        print("=" * 70)

        print(
            "Assets:",
            len(scan_assets),
        )

        print(
            "Qualified:",
            qualified_count,
        )

        print(
            "Rejected:",
            rejected_count,
        )

        print(
            "Errors:",
            error_count,
        )

        print(
            "Finished:",
            now_utc(),
        )

        print("=" * 70)

    except Exception as e:

        print(
            "\n[FATAL ERROR]",
            repr(e),
        )

        traceback.print_exc()

        send_telegram(
            "🔴 *IQ OPTION SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Fatal scanner error:\n"
            f"`{str(e)[:800]}`"
        )

    finally:

        if api is not None:

            try:
                api.close()
            except Exception:
                pass

        print(
            "\nScanner stopped safely."
        )


if __name__ == "__main__":
    main()
