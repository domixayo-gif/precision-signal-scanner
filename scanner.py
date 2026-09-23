import os
import time
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# BACK TO TREND — IQ OPTION OTC SCANNER
# ============================================================
# READ-ONLY SCANNER
# No automatic trading.
#
# STRATEGY:
#   1. 5M trend context
#   2. Price pulls back toward trend/level
#   3. 1M reaches a meaningful level
#   4. Confirmation candle triggers entry
#   5. Enough room before opposing structure
#
# Primary trigger:
#   CALL = Bullish Engulfing / Bullish Pin Bar
#   PUT  = Bearish Engulfing / Bearish Pin Bar
#
# Reference expiry: 5 minutes
#
# SCANNING:
#   - Rechecks every 60 seconds
#   - Uses fresh CLOSED 1M candles
#   - New setup candle = eligible for new signal
#   - Same candle cannot generate duplicate signal
#   - No artificial signal generation
# ============================================================


# ============================================================
# ENVIRONMENT
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# SETTINGS
# ============================================================

PRACTICE = True

MAX_OTC_ASSETS = 60

CANDLES_5M = 160
CANDLES_1M = 160

TF_5M = 300
TF_1M = 60

EXPIRY_MINUTES = 5

# IMPORTANT:
# Scan every 60 seconds so every new closed 1M candle
# can be evaluated.
SCAN_INTERVAL = 60

# Trend
EMA_FAST = 20
EMA_SLOW = 50

# RSI
RSI_PERIOD = 14

# ATR
ATR_PERIOD = 14

# Pullback tolerance
PULLBACK_ATR = 0.45

# Minimum room to opposing structure
MIN_ROOM_ATR = 0.80

# Recent structure
SWING_LOOKBACK = 25

# Trigger candle minimum body
MIN_TRIGGER_BODY = 0.35

# Do not signal if the candle is too extended
MAX_TRIGGER_RANGE_ATR = 1.80


# ============================================================
# RUNTIME
# ============================================================

iq = None

# Stores the last candle that produced a signal
# for each asset.
#
# This prevents the same setup from being sent again
# repeatedly during the same candle.
last_signal_candle = {}


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    try:
        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage"
        )

        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=15,
        )

    except Exception as e:
        print("Telegram error:", e)


# ============================================================
# BASIC MATH
# ============================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def mean(values):
    if not values:
        return 0.0

    return sum(values) / len(values)


# ============================================================
# EMA
# ============================================================

def ema(values, period):
    if len(values) < period:
        return []

    result = []

    multiplier = 2.0 / (period + 1.0)

    current = mean(values[:period])

    result.append(current)

    for price in values[period:]:
        current = (
            (price - current) * multiplier
            + current
        )

        result.append(current)

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):
    if len(values) <= period:
        return []

    gains = []
    losses = []

    for i in range(1, period + 1):

        change = (
            values[i]
            - values[i - 1]
        )

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = mean(gains)
    avg_loss = mean(losses)

    result = []

    if avg_loss == 0:

        result.append(100.0)

    else:

        rs = avg_gain / avg_loss

        result.append(
            100.0
            - (
                100.0
                / (1.0 + rs)
            )
        )

    for i in range(
        period + 1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            (
                avg_gain * (period - 1)
                + gain
            )
            / period
        )

        avg_loss = (
            (
                avg_loss * (period - 1)
                + loss
            )
            / period
        )

        if avg_loss == 0:

            result.append(100.0)

        else:

            rs = avg_gain / avg_loss

            result.append(
                100.0
                - (
                    100.0
                    / (1.0 + rs)
                )
            )

    return result


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):

    if len(candles) <= period:
        return []

    trs = []

    for i in range(1, len(candles)):

        high = candles[i]["max"]
        low = candles[i]["min"]
        previous_close = (
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        trs.append(tr)

    if len(trs) < period:
        return []

    current = mean(trs[:period])

    result = [current]

    for tr in trs[period:]:

        current = (
            (
                current * (period - 1)
                + tr
            )
            / period
        )

        result.append(current)

    return result


# ============================================================
# MACD
# ============================================================

def macd(values):

    if len(values) < 35:
        return None

    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return None

    fast_aligned = fast[-len(slow):]

    macd_line = [
        a - b
        for a, b in zip(
            fast_aligned,
            slow
        )
    ]

    if len(macd_line) < 9:
        return None

    signal_line = ema(
        macd_line,
        9
    )

    if not signal_line:
        return None

    macd_value = macd_line[-1]
    signal_value = signal_line[-1]

    return {
        "macd": macd_value,
        "signal": signal_value,
        "histogram": (
            macd_value
            - signal_value
        ),
    }


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_range(c):
    return max(
        c["max"] - c["min"],
        1e-12
    )


def candle_body(c):
    return abs(
        c["close"]
        - c["open"]
    )


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


# ============================================================
# ENGULFING
# ============================================================

def bullish_engulfing(previous, current):

    if not bearish(previous):
        return False

    if not bullish(current):
        return False

    previous_body_high = max(
        previous["open"],
        previous["close"],
    )

    previous_body_low = min(
        previous["open"],
        previous["close"],
    )

    current_body_high = max(
        current["open"],
        current["close"],
    )

    current_body_low = min(
        current["open"],
        current["close"],
    )

    return (
        current_body_high
        >= previous_body_high
        and
        current_body_low
        <= previous_body_low
        and
        candle_body(current)
        >= candle_body(previous)
    )


def bearish_engulfing(previous, current):

    if not bullish(previous):
        return False

    if not bearish(current):
        return False

    previous_body_high = max(
        previous["open"],
        previous["close"],
    )

    previous_body_low = min(
        previous["open"],
        previous["close"],
    )

    current_body_high = max(
        current["open"],
        current["close"],
    )

    current_body_low = min(
        current["open"],
        current["close"],
    )

    return (
        current_body_high
        >= previous_body_high
        and
        current_body_low
        <= previous_body_low
        and
        candle_body(current)
        >= candle_body(previous)
    )


# ============================================================
# PIN BAR
# ============================================================

def bullish_pin_bar(c):

    rng = candle_range(c)
    body = candle_body(c)

    upper_wick = (
        c["max"]
        - max(
            c["open"],
            c["close"]
        )
    )

    lower_wick = (
        min(
            c["open"],
            c["close"]
        )
        - c["min"]
    )

    body_ratio = body / rng
    lower_ratio = lower_wick / rng

    return (
        bullish(c)
        and
        lower_ratio >= 0.50
        and
        lower_wick >= body * 1.5
        and
        body_ratio <= 0.45
    )


def bearish_pin_bar(c):

    rng = candle_range(c)
    body = candle_body(c)

    upper_wick = (
        c["max"]
        - max(
            c["open"],
            c["close"]
        )
    )

    lower_wick = (
        min(
            c["open"],
            c["close"]
        )
        - c["min"]
    )

    body_ratio = body / rng
    upper_ratio = upper_wick / rng

    return (
        bearish(c)
        and
        upper_ratio >= 0.50
        and
        upper_wick >= body * 1.5
        and
        body_ratio <= 0.45
    )


# ============================================================
# STRUCTURE LEVELS
# ============================================================

def recent_support(
    candles,
    lookback=25
):

    data = candles[-lookback:]

    if not data:
        return None

    lows = [
        c["min"]
        for c in data
    ]

    return min(lows)


def recent_resistance(
    candles,
    lookback=25
):

    data = candles[-lookback:]

    if not data:
        return None

    highs = [
        c["max"]
        for c in data
    ]

    return max(highs)


# ============================================================
# PULLBACK DETECTION
# ============================================================

def pullback_to_bullish_zone(
    candles_1m,
    ema20,
    ema50,
    current,
    atr_value,
):

    tolerance = (
        atr_value
        * PULLBACK_ATR
    )

    lower_zone = (
        min(ema20, ema50)
        - tolerance
    )

    upper_zone = (
        max(ema20, ema50)
        + tolerance
    )

    return (
        current["min"]
        <= upper_zone
        and
        current["max"]
        >= lower_zone
    )


def pullback_to_bearish_zone(
    candles_1m,
    ema20,
    ema50,
    current,
    atr_value,
):

    tolerance = (
        atr_value
        * PULLBACK_ATR
    )

    lower_zone = (
        min(ema20, ema50)
        - tolerance
    )

    upper_zone = (
        max(ema20, ema50)
        + tolerance
    )

    return (
        current["min"]
        <= upper_zone
        and
        current["max"]
        >= lower_zone
    )


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    assets = set()

    try:

        data = None

        # Newer method
        try:
            data = iq.get_all_init_v2()
        except Exception:
            data = None

        # Legacy fallback
        if not data:

            try:
                data = iq.get_all_open_time()
            except Exception:
                data = None

        if not data:
            return []

        def recursive_scan(obj):

            if isinstance(obj, dict):

                name_candidates = []

                for key in (
                    "name",
                    "active_name",
                    "symbol",
                    "instrument",
                    "pair",
                ):

                    value = obj.get(key)

                    if isinstance(
                        value,
                        str
                    ):
                        name_candidates.append(
                            value
                        )

                for name in name_candidates:

                    if "-OTC" in name.upper():
                        assets.add(name)

                for key, value in obj.items():

                    if isinstance(key, str):

                        if "-OTC" in key.upper():
                            assets.add(key)

                    recursive_scan(value)

            elif isinstance(obj, list):

                for item in obj:
                    recursive_scan(item)

        recursive_scan(data)

    except Exception as e:

        print(
            "OTC discovery error:",
            e
        )

    cleaned = []

    for asset in assets:

        if not isinstance(
            asset,
            str
        ):
            continue

        asset = asset.strip()

        if not asset:
            continue

        if "-OTC" not in asset.upper():
            continue

        cleaned.append(asset)

    cleaned = sorted(
        set(cleaned)
    )

    return cleaned[:MAX_OTC_ASSETS]


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candles(raw):

    result = []

    if not raw:
        return result

    for c in raw:

        try:

            result.append(
                {
                    "from": int(
                        c.get(
                            "from",
                            0
                        )
                    ),

                    "open": safe_float(
                        c.get("open")
                    ),

                    "close": safe_float(
                        c.get("close")
                    ),

                    "min": safe_float(
                        c.get(
                            "min",
                            c.get("low")
                        )
                    ),

                    "max": safe_float(
                        c.get(
                            "max",
                            c.get("high")
                        )
                    ),
                }
            )

        except Exception:
            continue

    result.sort(
        key=lambda x: x["from"]
    )

    return result


# ============================================================
# GET CLOSED CANDLES
# ============================================================

def get_closed_candles(
    asset,
    timeframe,
    count
):

    try:

        now = int(
            time.time()
        )

        raw = iq.get_candles(
            asset,
            timeframe,
            count,
            now,
        )

        candles = normalize_candles(
            raw
        )

        if not candles:
            return []

        current_bucket = (
            int(
                time.time()
                // timeframe
            )
            * timeframe
        )

        # Remove currently forming candle.
        candles = [
            c
            for c in candles
            if c["from"]
            < current_bucket
        ]

        return candles

    except Exception as e:

        print(
            f"Candle error "
            f"{asset} "
            f"{timeframe}s:",
            e
        )

        return []


# ============================================================
# TREND ANALYSIS
# ============================================================

def analyze_5m(candles):

    if len(candles) < (
        EMA_SLOW + 10
    ):
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    ema20_values = ema(
        closes,
        EMA_FAST
    )

    ema50_values = ema(
        closes,
        EMA_SLOW
    )

    if (
        len(ema20_values) < 3
        or
        len(ema50_values) < 3
    ):
        return None

    e20 = ema20_values[-1]
    e20_prev = ema20_values[-3]

    e50 = ema50_values[-1]
    e50_prev = ema50_values[-3]

    rsi_values = rsi(
        closes,
        RSI_PERIOD
    )

    rsi_value = (
        rsi_values[-1]
        if rsi_values
        else 50.0
    )

    macd_data = macd(
        closes
    )

    current = candles[-1]

    bullish_context = (
        e20 > e50
        and
        e20 >= e20_prev
        and
        current["close"] >= e20
    )

    bearish_context = (
        e20 < e50
        and
        e20 <= e20_prev
        and
        current["close"] <= e20
    )

    if bullish_context:
        trend = "BULLISH"

    elif bearish_context:
        trend = "BEARISH"

    else:
        trend = "NEUTRAL"

    return {
        "trend": trend,
        "ema20": e20,
        "ema50": e50,
        "rsi": rsi_value,
        "macd": macd_data,
        "price": current["close"],
    }


# ============================================================
# BACK TO TREND STRATEGY
# ============================================================

def evaluate_back_to_trend(
    candles_5m,
    candles_1m
):

    trend_data = analyze_5m(
        candles_5m
    )

    if not trend_data:
        return None

    trend = trend_data["trend"]

    if trend not in (
        "BULLISH",
        "BEARISH"
    ):
        return None

    closes_1m = [
        c["close"]
        for c in candles_1m
    ]

    ema20_1m_values = ema(
        closes_1m,
        EMA_FAST
    )

    ema50_1m_values = ema(
        closes_1m,
        EMA_SLOW
    )

    atr_values = atr(
        candles_1m,
        ATR_PERIOD
    )

    if not ema20_1m_values:
        return None

    if not ema50_1m_values:
        return None

    if not atr_values:
        return None

    ema20_1m = (
        ema20_1m_values[-1]
    )

    ema50_1m = (
        ema50_1m_values[-1]
    )

    atr_value = atr_values[-1]

    if atr_value <= 0:
        return None

    if len(candles_1m) < 2:
        return None

    current = candles_1m[-1]
    previous = candles_1m[-2]

    current_range = candle_range(
        current
    )

    # Reject abnormally large
    # trigger candles.
    if (
        current_range
        > atr_value
        * MAX_TRIGGER_RANGE_ATR
    ):
        return None

    # ========================================================
    # BULLISH SETUP
    # ========================================================

    if trend == "BULLISH":

        pullback = (
            pullback_to_bullish_zone(
                candles_1m,
                ema20_1m,
                ema50_1m,
                current,
                atr_value,
            )
        )

        if not pullback:
            return None

        support = recent_support(
            candles_1m[:-1],
            SWING_LOOKBACK
        )

        if support is None:
            return None

        level_tolerance = (
            atr_value * 0.60
        )

        level_ok = (
            current["min"]
            <= support
            + level_tolerance
        )

        if not level_ok:
            return None

        engulfing = (
            bullish_engulfing(
                previous,
                current
            )
        )

        pinbar = bullish_pin_bar(
            current
        )

        trigger = (
            "BULLISH ENGULFING"
            if engulfing
            else
            "BULLISH PIN BAR"
            if pinbar
            else None
        )

        if trigger is None:
            return None

        rsi_ok = (
            trend_data["rsi"] >= 50
            and
            trend_data["rsi"] <= 70
        )

        macd_ok = False

        if trend_data["macd"]:

            macd_ok = (
                trend_data["macd"][
                    "histogram"
                ] >= 0
            )

        resistance = recent_resistance(
            candles_1m[:-1],
            SWING_LOOKBACK
        )

        if resistance is None:
            return None

        room = (
            resistance
            - current["close"]
        )

        if room <= 0:
            return None

        room_atr = (
            room / atr_value
        )

        if room_atr < MIN_ROOM_ATR:
            return None

        score = 0

        score += 25
        score += 25
        score += 25

        if rsi_ok:
            score += 10

        if macd_ok:
            score += 5

        if room_atr >= 1.20:
            score += 10

        return {
            "direction": "CALL",
            "trend": "BULLISH",
            "trigger": trigger,
            "level": "SUPPORT RETEST",
            "score": min(
                score,
                100
            ),
            "rsi": trend_data["rsi"],
            "macd_hist": (
                trend_data["macd"][
                    "histogram"
                ]
                if trend_data["macd"]
                else 0.0
            ),
            "ema20": trend_data["ema20"],
            "ema50": trend_data["ema50"],
            "entry": current["close"],
            "support": support,
            "resistance": resistance,
            "room": room,
            "room_atr": room_atr,
            "candle_time": current["from"],
        }

    # ========================================================
    # BEARISH SETUP
    # ========================================================

    if trend == "BEARISH":

        pullback = (
            pullback_to_bearish_zone(
                candles_1m,
                ema20_1m,
                ema50_1m,
                current,
                atr_value,
            )
        )

        if not pullback:
            return None

        resistance = recent_resistance(
            candles_1m[:-1],
            SWING_LOOKBACK
        )

        if resistance is None:
            return None

        level_tolerance = (
            atr_value * 0.60
        )

        level_ok = (
            current["max"]
            >= resistance
            - level_tolerance
        )

        if not level_ok:
            return None

        engulfing = (
            bearish_engulfing(
                previous,
                current
            )
        )

        pinbar = bearish_pin_bar(
            current
        )

        trigger = (
            "BEARISH ENGULFING"
            if engulfing
            else
            "BEARISH PIN BAR"
            if pinbar
            else None
        )

        if trigger is None:
            return None

        rsi_ok = (
            trend_data["rsi"] <= 50
            and
            trend_data["rsi"] >= 30
        )

        macd_ok = False

        if trend_data["macd"]:

            macd_ok = (
                trend_data["macd"][
                    "histogram"
                ] <= 0
            )

        support = recent_support(
            candles_1m[:-1],
            SWING_LOOKBACK
        )

        if support is None:
            return None

        room = (
            current["close"]
            - support
        )

        if room <= 0:
            return None

        room_atr = (
            room / atr_value
        )

        if room_atr < MIN_ROOM_ATR:
            return None

        score = 0

        score += 25
        score += 25
        score += 25

        if rsi_ok:
            score += 10

        if macd_ok:
            score += 5

        if room_atr >= 1.20:
            score += 10

        return {
            "direction": "PUT",
            "trend": "BEARISH",
            "trigger": trigger,
            "level": "RESISTANCE RETEST",
            "score": min(
                score,
                100
            ),
            "rsi": trend_data["rsi"],
            "macd_hist": (
                trend_data["macd"][
                    "histogram"
                ]
                if trend_data["macd"]
                else 0.0
            ),
            "ema20": trend_data["ema20"],
            "ema50": trend_data["ema50"],
            "entry": current["close"],
            "support": support,
            "resistance": resistance,
            "room": room,
            "room_atr": room_atr,
            "candle_time": current["from"],
        }

    return None


# ============================================================
# SIGNAL ID
# ============================================================

def make_signal_id(
    asset,
    direction,
    candle_time
):

    timestamp = datetime.fromtimestamp(
        candle_time,
        tz=timezone.utc
    ).strftime("%H%M%S")

    clean_asset = (
        asset
        .replace("/", "")
        .replace("-", "")
        .replace(" ", "")
    )

    return (
        f"{clean_asset}-"
        f"{direction}-"
        f"{timestamp}"
    )


# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(
    asset,
    setup
):

    candle_time = (
        setup["candle_time"]
    )

    signal_id = make_signal_id(
        asset,
        setup["direction"],
        candle_time
    )

    entry_time = (
        datetime.fromtimestamp(
            candle_time,
            tz=timezone.utc
        )
        .strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    )

    direction_icon = (
        "🟢"
        if setup["direction"] == "CALL"
        else "🔴"
    )

    message = f"""
{direction_icon} <b>BACK TO TREND SIGNAL</b>
━━━━━━━━━━━━━━━━━━
<b>Asset:</b> {asset}
<b>Direction:</b> <b>{setup["direction"]}</b>
<b>Reference expiry:</b> {EXPIRY_MINUTES} minutes

<b>Signal ID:</b> <code>{signal_id}</code>

<b>5M CONTEXT</b>
Trend: {setup["trend"]}
EMA20: {setup["ema20"]:.6f}
EMA50: {setup["ema50"]:.6f}

<b>1M SETUP</b>
Level: {setup["level"]}
Trigger: <b>{setup["trigger"]}</b>

RSI 14: {setup["rsi"]:.1f}
MACD histogram: {setup["macd_hist"]:.6f}

Entry: {setup["entry"]:.6f}
Room: {setup["room_atr"]:.2f} ATR

<b>SETUP READY</b>
Context ✓
Pullback ✓
Level ✓
Trigger ✓
Room ✓

<b>Quality:</b> {setup["score"]}/100

<b>Signal candle:</b>
{entry_time}
━━━━━━━━━━━━━━━━━━
READ-ONLY SIGNAL
NO AUTOMATIC TRADE
""".strip()

    telegram(message)

    return signal_id


# ============================================================
# SCAN ONE ASSET
# ============================================================

def scan_asset(asset):

    try:

        candles_5m = get_closed_candles(
            asset,
            TF_5M,
            CANDLES_5M
        )

        candles_1m = get_closed_candles(
            asset,
            TF_1M,
            CANDLES_1M
        )

        if len(candles_5m) < 70:
            return None

        if len(candles_1m) < 70:
            return None

        setup = evaluate_back_to_trend(
            candles_5m,
            candles_1m
        )

        if not setup:
            return None

        candle_time = (
            setup["candle_time"]
        )

        # ====================================================
        # IMPORTANT:
        # Only prevent duplicate signals for the EXACT SAME
        # CLOSED 1M CANDLE.
        #
        # There is NO 300-second asset lock anymore.
        #
        # Therefore:
        # Candle 10:05 -> signal
        # Candle 10:06 -> can signal again if qualified
        # Candle 10:07 -> can signal again if qualified
        # ====================================================

        if (
            last_signal_candle.get(asset)
            == candle_time
        ):
            return None

        last_signal_candle[
            asset
        ] = candle_time

        return send_signal(
            asset,
            setup
        )

    except Exception:

        print(
            f"Scan error: {asset}"
        )

        traceback.print_exc()

        return None


# ============================================================
# LOGIN
# ============================================================

def connect():

    global iq

    if (
        not IQ_EMAIL
        or
        not IQ_PASSWORD
    ):

        telegram(
            "🔴 <b>BACK TO TREND SCANNER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )

        return False

    try:

        iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        iq.connect()

        time.sleep(3)

        if not iq.check_connect():

            telegram(
                "🔴 <b>BACK TO TREND SCANNER</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "IQ Option connection failed."
            )

            return False

        try:

            iq.change_balance(
                "PRACTICE"
                if PRACTICE
                else "REAL"
            )

        except Exception:
            pass

        return True

    except Exception as e:

        telegram(
            "🔴 <b>BACK TO TREND SCANNER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Connection/login failed.\n\n"
            f"<code>{str(e)}</code>"
        )

        return False


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    print(
        "=========================================="
    )

    print(
        "BACK TO TREND — IQ OPTION OTC SCANNER"
    )

    print(
        "=========================================="
    )

    if not connect():
        return

    telegram(
        "🟡 <b>BACK TO TREND SCANNER ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "IQ Option OTC connection: <b>OK</b>\n"
        "Strategy: <b>Back to Trend</b>\n"
        "Context: 5M\n"
        "Entry: 1M\n"
        "Expiry: 5 minutes\n"
        "Scan cycle: <b>1 minute</b>\n"
        "Mode: READ-ONLY\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Scanning continuously for fresh "
        "trend pullback setups..."
    )

    while True:

        cycle_start = time.time()

        try:

            # Re-discover OTC instruments on
            # every scan cycle.
            assets = discover_otc_assets()

            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] "
                f"OTC assets discovered: "
                f"{len(assets)}"
            )

            if not assets:

                telegram(
                    "🟡 <b>BACK TO TREND SCANNER</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "No currently open OTC instruments "
                    "were discovered.\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Will check again in 1 minute."
                )

            else:

                signals = 0

                for asset in assets:

                    result = scan_asset(
                        asset
                    )

                    if result:
                        signals += 1

                    time.sleep(0.20)

                print(
                    f"Qualified Back to Trend "
                    f"signals this cycle: "
                    f"{signals}"
                )

                if signals == 0:

                    telegram(
                        "🟡 <b>BACK TO TREND SCANNER</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"OTC candle feeds working: "
                        f"<b>{len(assets)}</b>\n"
                        "Qualified setups: <b>0</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "NO TRADE\n\n"
                        "Checking again in 1 minute."
                    )

        except Exception:

            traceback.print_exc()

            try:

                if (
                    iq
                    and
                    not iq.check_connect()
                ):

                    telegram(
                        "🟠 <b>BACK TO TREND SCANNER</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "Connection lost.\n"
                        "Attempting to reconnect..."
                    )

                    connect()

            except Exception:

                pass

        # ====================================================
        # KEEP A 60-SECOND SCAN CYCLE
        #
        # If scanning itself takes 20 seconds,
        # only wait about 40 more seconds.
        # ====================================================

        elapsed = (
            time.time()
            - cycle_start
        )

        wait_time = max(
            1,
            SCAN_INTERVAL - elapsed
        )

        print(
            f"Next scan in "
            f"{wait_time:.1f} seconds."
        )

        time.sleep(
            wait_time
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
