# ============================================================
# BACK TO TREND — IQ OPTION OTC DEMO AUTO-TESTER
# ============================================================
#
# 30M TREND -> 5M PULLBACK -> 1M ENTRY
# REFERENCE EXPIRY: 3 MINUTES
#
# PRACTICE / DEMO ONLY
# AUTOMATIC TRADING ENABLED ON PRACTICE ACCOUNT
# REAL ACCOUNT IS HARD-BLOCKED
#
# ============================================================

import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# IQ OPTION / TELEGRAM
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# DEMO SAFETY
# ============================================================

PRACTICE = True

AUTO_TRADE = True

# HARD SAFETY LOCK
DEMO_ONLY_LOCK = True

# Fixed demo stake
STAKE = 1.0

# Maximum demo trades in one session
MAX_AUTO_TRADES = 30

# One trade at a time
MAX_OPEN_TRADES = 1

# Minimum time between entries
TRADE_COOLDOWN = 180

# Stop automatic trading after consecutive losses
MAX_CONSECUTIVE_LOSSES = 4


# ============================================================
# TIMEFRAMES
# ============================================================

TF_30M = 1800
TF_5M = 300
TF_1M = 60

EXPIRY_MINUTES = 3

CANDLES_30M = 180
CANDLES_5M = 180
CANDLES_1M = 180


# ============================================================
# SCANNER SETTINGS
# ============================================================

SCAN_INTERVAL = 60

STATUS_INTERVAL = 300

OTC_DIAGNOSTIC_INTERVAL = 300

RECONNECT_INTERVAL = 30

MAX_RUNTIME = 24 * 60 * 60


# ============================================================
# STRATEGY SETTINGS
# ============================================================

EMA_FAST = 20
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

SWING_LOOKBACK = 30

MIN_SCORE = 90

MIN_ADX = 20

MIN_ROOM_ATR = 1.20

MAX_TRIGGER_RANGE_ATR = 1.50

MAX_EXTENSION_ATR = 2.20

MAX_5M_DISTANCE_ATR = 0.65

MIN_TRIGGER_BODY = 0.35

PULLBACK_ATR = 0.45


# ============================================================
# GLOBAL STATE
# ============================================================

iq = None

seen_signal_keys = set()

last_otc_diagnostic = 0

last_status_time = 0

last_trade_time = 0

trade_count = 0

wins = 0

losses = 0

draws = 0

total_profit = 0.0

consecutive_losses = 0

active_trade = None

session_started = time.time()

telegram_offset = None


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_float(value, default=0.0):

    try:

        if value is None:
            return default

        number = float(value)

        if not math.isfinite(number):
            return default

        return number

    except Exception:

        return default


def now_utc():

    return datetime.now(timezone.utc)


def utc_text():

    return now_utc().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        print(message)

        return False

    try:

        url = (
            "https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }

        response = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        return response.ok

    except Exception as exc:

        print(
            "Telegram error:",
            exc,
        )

        return False


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return []

    result = []

    multiplier = 2.0 / (period + 1)

    seed = (
        sum(values[:period])
        / period
    )

    result.append(seed)

    previous = seed

    for price in values[period:]:

        current = (
            (price - previous)
            * multiplier
            + previous
        )

        result.append(current)

        previous = current

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):

    if len(values) < period + 1:
        return []

    gains = []

    losses_ = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses_.append(0.0)

        else:

            gains.append(0.0)
            losses_.append(
                abs(change)
            )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses_[:period])
        / period
    )

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
        period,
        len(gains),
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses_[i]
        ) / period

        if avg_loss == 0:

            result.append(100.0)

        else:

            rs = (
                avg_gain
                / avg_loss
            )

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

    if len(candles) < period + 1:
        return []

    trs = []

    for i in range(
        1,
        len(candles),
    ):

        high = safe_float(
            candles[i]["max"]
        )

        low = safe_float(
            candles[i]["min"]
        )

        previous_close = safe_float(
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(
                high
                - previous_close
            ),
            abs(
                low
                - previous_close
            ),
        )

        trs.append(tr)

    if len(trs) < period:
        return []

    first = (
        sum(trs[:period])
        / period
    )

    result = [first]

    previous = first

    for tr in trs[period:]:

        current = (
            (
                previous
                * (period - 1)
            )
            + tr
        ) / period

        result.append(current)

        previous = current

    return result


# ============================================================
# ADX
# ============================================================

def adx(candles, period=14):

    if len(candles) < (
        period * 2
    ) + 2:

        return []

    trs = []

    plus_dm = []

    minus_dm = []

    for i in range(
        1,
        len(candles),
    ):

        high = safe_float(
            candles[i]["max"]
        )

        low = safe_float(
            candles[i]["min"]
        )

        previous_high = safe_float(
            candles[i - 1]["max"]
        )

        previous_low = safe_float(
            candles[i - 1]["min"]
        )

        previous_close = safe_float(
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(
                high
                - previous_close
            ),
            abs(
                low
                - previous_close
            ),
        )

        up_move = (
            high
            - previous_high
        )

        down_move = (
            previous_low
            - low
        )

        if (
            up_move > down_move
            and up_move > 0
        ):

            plus = up_move

        else:

            plus = 0.0

        if (
            down_move > up_move
            and down_move > 0
        ):

            minus = down_move

        else:

            minus = 0.0

        trs.append(tr)

        plus_dm.append(plus)

        minus_dm.append(minus)

    if len(trs) < period:
        return []

    tr_sum = sum(
        trs[:period]
    )

    plus_sum = sum(
        plus_dm[:period]
    )

    minus_sum = sum(
        minus_dm[:period]
    )

    atr_values = [
        tr_sum / period
    ]

    plus_values = [
        plus_sum / period
    ]

    minus_values = [
        minus_sum / period
    ]

    for i in range(
        period,
        len(trs),
    ):

        tr_sum = (
            tr_sum
            - (
                tr_sum
                / period
            )
            + trs[i]
        )

        plus_sum = (
            plus_sum
            - (
                plus_sum
                / period
            )
            + plus_dm[i]
        )

        minus_sum = (
            minus_sum
            - (
                minus_sum
                / period
            )
            + minus_dm[i]
        )

        atr_values.append(
            tr_sum / period
        )

        plus_values.append(
            plus_sum / period
        )

        minus_values.append(
            minus_sum / period
        )

    dx = []

    for i in range(
        len(atr_values)
    ):

        if atr_values[i] <= 0:

            dx.append(0.0)

            continue

        plus_di = (
            100.0
            * plus_values[i]
            / atr_values[i]
        )

        minus_di = (
            100.0
            * minus_values[i]
            / atr_values[i]
        )

        denominator = (
            plus_di
            + minus_di
        )

        if denominator <= 0:

            dx.append(0.0)

        else:

            dx.append(
                100.0
                * abs(
                    plus_di
                    - minus_di
                )
                / denominator
            )

    if len(dx) < period:
        return []

    first_adx = (
        sum(dx[:period])
        / period
    )

    result = [first_adx]

    previous = first_adx

    for value in dx[period:]:

        current = (
            (
                previous
                * (period - 1)
            )
            + value
        ) / period

        result.append(current)

        previous = current

    return result


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_body(candle):

    return abs(
        safe_float(
            candle["close"]
        )
        - safe_float(
            candle["open"]
        )
    )


def candle_range(candle):

    return (
        safe_float(
            candle["max"]
        )
        - safe_float(
            candle["min"]
        )
    )


def candle_direction(candle):

    opening = safe_float(
        candle["open"]
    )

    closing = safe_float(
        candle["close"]
    )

    if closing > opening:
        return "BULLISH"

    if closing < opening:
        return "BEARISH"

    return "NEUTRAL"


def is_bullish_engulfing(
    previous,
    current,
):

    prev_open = safe_float(
        previous["open"]
    )

    prev_close = safe_float(
        previous["close"]
    )

    cur_open = safe_float(
        current["open"]
    )

    cur_close = safe_float(
        current["close"]
    )

    return (
        prev_close < prev_open
        and cur_close > cur_open
        and cur_open <= prev_close
        and cur_close >= prev_open
    )


def is_bearish_engulfing(
    previous,
    current,
):

    prev_open = safe_float(
        previous["open"]
    )

    prev_close = safe_float(
        previous["close"]
    )

    cur_open = safe_float(
        current["open"]
    )

    cur_close = safe_float(
        current["close"]
    )

    return (
        prev_close > prev_open
        and cur_close < cur_open
        and cur_open >= prev_close
        and cur_close <= prev_open
    )


def is_bullish_pin(candle):

    opening = safe_float(
        candle["open"]
    )

    closing = safe_float(
        candle["close"]
    )

    high = safe_float(
        candle["max"]
    )

    low = safe_float(
        candle["min"]
    )

    body = abs(
        closing - opening
    )

    lower_wick = (
        min(opening, closing)
        - low
    )

    upper_wick = (
        high
        - max(opening, closing)
    )

    rng = high - low

    if rng <= 0:
        return False

    return (
        lower_wick >= body * 1.5
        and lower_wick > upper_wick
        and closing
        >= low + rng * 0.55
    )


def is_bearish_pin(candle):

    opening = safe_float(
        candle["open"]
    )

    closing = safe_float(
        candle["close"]
    )

    high = safe_float(
        candle["max"]
    )

    low = safe_float(
        candle["min"]
    )

    body = abs(
        closing - opening
    )

    lower_wick = (
        min(opening, closing)
        - low
    )

    upper_wick = (
        high
        - max(opening, closing)
    )

    rng = high - low

    if rng <= 0:
        return False

    return (
        upper_wick >= body * 1.5
        and upper_wick > lower_wick
        and closing
        <= low + rng * 0.45
    )


# ============================================================
# SERVER TIME
# ============================================================

def server_now():

    try:

        if iq is not None:

            timestamp = (
                iq.get_server_timestamp()
            )

            if timestamp:

                return int(timestamp)

    except Exception:
        pass

    return int(time.time())


# ============================================================
# GET CLOSED CANDLES
# ============================================================

def get_closed_candles(
    asset,
    interval,
    count,
):

    try:

        end_time = server_now()

        candles = iq.get_candles(
            asset,
            interval,
            count + 5,
            end_time,
        )

        if not candles:
            return []

        candles = sorted(
            candles,
            key=lambda x: safe_float(
                x.get("from", 0)
            ),
        )

        current_bucket = (
            int(end_time)
            // interval
        ) * interval

        closed = []

        for candle in candles:

            candle_time = int(
                safe_float(
                    candle.get(
                        "from",
                        0,
                    )
                )
            )

            if candle_time < current_bucket:

                closed.append(candle)

        return closed[-count:]

    except Exception as exc:

        print(
            f"Candle error "
            f"{asset} "
            f"{interval}: {exc}"
        )

        return []


# ============================================================
# TREND ANALYSIS
# ============================================================

def analyze_trend(candles):

    if len(candles) < (
        EMA_SLOW + 10
    ):

        return None

    closes = [
        safe_float(
            c["close"]
        )
        for c in candles
    ]

    ema_fast_values = ema(
        closes,
        EMA_FAST,
    )

    ema_slow_values = ema(
        closes,
        EMA_SLOW,
    )

    rsi_values = rsi(
        closes,
        RSI_PERIOD,
    )

    adx_values = adx(
        candles,
        ADX_PERIOD,
    )

    atr_values = atr(
        candles,
        ATR_PERIOD,
    )

    if not ema_fast_values:
        return None

    if not ema_slow_values:
        return None

    if not rsi_values:
        return None

    if not adx_values:
        return None

    if not atr_values:
        return None

    fast = ema_fast_values[-1]

    slow = ema_slow_values[-1]

    price = closes[-1]

    trend = "NEUTRAL"

    if (
        fast > slow
        and price > fast
    ):

        trend = "BULLISH"

    elif (
        fast < slow
        and price < fast
    ):

        trend = "BEARISH"

    return {
        "trend": trend,
        "ema_fast": fast,
        "ema_slow": slow,
        "price": price,
        "rsi": rsi_values[-1],
        "adx": adx_values[-1],
        "atr": atr_values[-1],
    }


# ============================================================
# STRUCTURE
# ============================================================

def structure(candles):

    if len(candles) < (
        SWING_LOOKBACK + 5
    ):

        return None

    recent = candles[
        -SWING_LOOKBACK:
    ]

    highs = [
        safe_float(
            c["max"]
        )
        for c in recent
    ]

    lows = [
        safe_float(
            c["min"]
        )
        for c in recent
    ]

    price = safe_float(
        candles[-1]["close"]
    )

    return {
        "price": price,
        "swing_high": max(highs),
        "swing_low": min(lows),
    }


# ============================================================
# PULLBACK QUALITY
# ============================================================

def pullback_quality(
    trend,
    candles_5m,
    atr_5m,
):

    if len(candles_5m) < 6:
        return None

    last = candles_5m[-1]

    previous = candles_5m[-2]

    price = safe_float(
        last["close"]
    )

    distance = abs(
        price
        - trend["ema_fast"]
    )

    if atr_5m <= 0:
        return None

    distance_atr = (
        distance
        / atr_5m
    )

    recent = candles_5m[-5:]

    bullish_count = sum(
        1
        for candle in recent
        if candle_direction(candle)
        == "BULLISH"
    )

    bearish_count = sum(
        1
        for candle in recent
        if candle_direction(candle)
        == "BEARISH"
    )

    if trend["trend"] == "BULLISH":

        pullback_seen = (
            bearish_count >= 1
            or safe_float(
                previous["min"]
            )
            <= trend["ema_fast"]
        )

        return {
            "valid": pullback_seen,
            "distance_atr": distance_atr,
            "direction": "CALL",
        }

    if trend["trend"] == "BEARISH":

        pullback_seen = (
            bullish_count >= 1
            or safe_float(
                previous["max"]
            )
            >= trend["ema_fast"]
        )

        return {
            "valid": pullback_seen,
            "distance_atr": distance_atr,
            "direction": "PUT",
        }

    return None


# ============================================================
# 1M TRIGGER
# ============================================================

def trigger_quality(
    candles_1m,
    direction,
    atr_1m,
):

    if len(candles_1m) < 4:
        return None

    previous = candles_1m[-2]

    current = candles_1m[-1]

    rng = candle_range(
        current
    )

    body = candle_body(
        current
    )

    if rng <= 0:
        return None

    body_ratio = (
        body / rng
    )

    if atr_1m <= 0:
        return None

    range_atr = (
        rng / atr_1m
    )

    if (
        range_atr
        > MAX_TRIGGER_RANGE_ATR
    ):

        return None

    bullish_engulf = (
        is_bullish_engulfing(
            previous,
            current,
        )
    )

    bearish_engulf = (
        is_bearish_engulfing(
            previous,
            current,
        )
    )

    bullish_pin = (
        is_bullish_pin(
            current
        )
    )

    bearish_pin = (
        is_bearish_pin(
            current
        )
    )

    if direction == "CALL":

        if (
            (
                body_ratio
                >= MIN_TRIGGER_BODY
                and candle_direction(
                    current
                ) == "BULLISH"
            )
            or bullish_engulf
            or bullish_pin
        ):

            if bullish_engulf:

                trigger = (
                    "BULLISH ENGULFING"
                )

            elif bullish_pin:

                trigger = "BULLISH PIN"

            else:

                trigger = "BULLISH BODY"

            return {
                "valid": True,
                "trigger": trigger,
                "range_atr": range_atr,
                "body_ratio": body_ratio,
            }

    if direction == "PUT":

        if (
            (
                body_ratio
                >= MIN_TRIGGER_BODY
                and candle_direction(
                    current
                ) == "BEARISH"
            )
            or bearish_engulf
            or bearish_pin
        ):

            if bearish_engulf:

                trigger = (
                    "BEARISH ENGULFING"
                )

            elif bearish_pin:

                trigger = "BEARISH PIN"

            else:

                trigger = "BEARISH BODY"

            return {
                "valid": True,
                "trigger": trigger,
                "range_atr": range_atr,
                "body_ratio": body_ratio,
            }

    return None


# ============================================================
# ASSET EVALUATION
# ============================================================

def evaluate_asset(asset):

    try:

        candles_30m = (
            get_closed_candles(
                asset,
                TF_30M,
                CANDLES_30M,
            )
        )

        candles_5m = (
            get_closed_candles(
                asset,
                TF_5M,
                CANDLES_5M,
            )
        )

        candles_1m = (
            get_closed_candles(
                asset,
                TF_1M,
                CANDLES_1M,
            )
        )

        if (
            len(candles_30m) < 70
            or len(candles_5m) < 70
            or len(candles_1m) < 40
        ):

            return None

        trend = analyze_trend(
            candles_30m
        )

        trend_5m = analyze_trend(
            candles_5m
        )

        if not trend or not trend_5m:
            return None

        if trend["trend"] not in (
            "BULLISH",
            "BEARISH",
        ):

            return None

        if trend_5m["trend"] not in (
            "BULLISH",
            "BEARISH",
        ):

            return None

        direction = (
            "CALL"
            if trend["trend"]
            == "BULLISH"
            else "PUT"
        )

        score = 0

        reasons = []

        # ----------------------------------------------------
        # 30M + 5M ALIGNMENT
        # ----------------------------------------------------

        if (
            trend["trend"]
            == trend_5m["trend"]
        ):

            score += 25

            reasons.append(
                "30M/5M trend aligned"
            )

        else:

            return None

        # ----------------------------------------------------
        # ADX
        # ----------------------------------------------------

        if (
            trend["adx"]
            >= MIN_ADX
        ):

            score += 15

            reasons.append(
                "ADX strong"
            )

        else:

            return None

        # ----------------------------------------------------
        # 5M EMA
        # ----------------------------------------------------

        price_5m = (
            trend_5m["price"]
        )

        atr_5m = (
            trend_5m["atr"]
        )

        if atr_5m <= 0:
            return None

        distance_5m = abs(
            price_5m
            - trend_5m["ema_fast"]
        )

        distance_5m_atr = (
            distance_5m
            / atr_5m
        )

        if (
            distance_5m_atr
            <= MAX_5M_DISTANCE_ATR
        ):

            score += 15

            reasons.append(
                "5M near EMA"
            )

        else:

            return None

        # ----------------------------------------------------
        # PULLBACK
        # ----------------------------------------------------

        pullback = (
            pullback_quality(
                trend_5m,
                candles_5m,
                atr_5m,
            )
        )

        if not pullback:
            return None

        if not pullback["valid"]:
            return None

        score += 15

        reasons.append(
            "5M pullback"
        )

        # ----------------------------------------------------
        # 1M TRIGGER
        # ----------------------------------------------------

        atr_1m_values = atr(
            candles_1m,
            ATR_PERIOD,
        )

        if not atr_1m_values:
            return None

        atr_1m = (
            atr_1m_values[-1]
        )

        trigger = (
            trigger_quality(
                candles_1m,
                direction,
                atr_1m,
            )
        )

        if not trigger:
            return None

        score += 20

        reasons.append(
            trigger["trigger"]
        )

        # ----------------------------------------------------
        # 1M RSI
        # ----------------------------------------------------

        closes_1m = [
            safe_float(
                c["close"]
            )
            for c in candles_1m
        ]

        rsi_1m_values = rsi(
            closes_1m,
            RSI_PERIOD,
        )

        if not rsi_1m_values:
            return None

        rsi_1m = (
            rsi_1m_values[-1]
        )

        if direction == "CALL":

            if 45 <= rsi_1m <= 70:

                score += 5

                reasons.append(
                    "1M RSI supportive"
                )

            else:

                return None

        else:

            if 30 <= rsi_1m <= 55:

                score += 5

                reasons.append(
                    "1M RSI supportive"
                )

            else:

                return None

        # ----------------------------------------------------
        # STRUCTURE / ROOM
        # ----------------------------------------------------

        struct = structure(
            candles_5m
        )

        if not struct:
            return None

        current_price = (
            struct["price"]
        )

        if direction == "CALL":

            room = (
                struct["swing_high"]
                - current_price
            )

        else:

            room = (
                current_price
                - struct["swing_low"]
            )

        room_atr = (
            room / atr_5m
            if atr_5m > 0
            else 0
        )

        if (
            room_atr
            < MIN_ROOM_ATR
        ):

            return None

        score += 5

        reasons.append(
            "Room available"
        )

        # ----------------------------------------------------
        # EXTENSION
        # ----------------------------------------------------

        extension = (
            distance_5m
            / atr_5m
            if atr_5m > 0
            else 0
        )

        if (
            extension
            > MAX_EXTENSION_ATR
        ):

            return None

        return {
            "asset": asset,
            "direction": direction,
            "score": score,
            "price": current_price,
            "trend_30m": trend["trend"],
            "trend_5m": trend_5m["trend"],
            "rsi_30m": trend["rsi"],
            "rsi_5m": trend_5m["rsi"],
            "rsi_1m": rsi_1m,
            "adx_30m": trend["adx"],
            "adx_5m": trend_5m["adx"],
            "room_atr": room_atr,
            "extension_atr": extension,
            "pullback_atr": pullback[
                "distance_atr"
            ],
            "trigger": trigger[
                "trigger"
            ],
            "trigger_range_atr": trigger[
                "range_atr"
            ],
            "reasons": reasons,
        }

    except Exception as exc:

        print(
            f"Evaluation error "
            f"{asset}: {exc}"
        )

        return None


# ============================================================
# OTC DISCOVERY DIAGNOSTIC
# ============================================================

def otc_diagnostic(
    opened,
    reason="",
):

    global last_otc_diagnostic

    current = time.time()

    if (
        current
        - last_otc_diagnostic
        < OTC_DIAGNOSTIC_INTERVAL
    ):

        return

    last_otc_diagnostic = current

    lines = []

    lines.append(
        "🧪 IQ OPTION OTC DISCOVERY"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    if reason:

        lines.append(
            f"Reason: {reason}"
        )

    if not isinstance(
        opened,
        dict,
    ):

        lines.append(
            "IQ Option returned invalid "
            "market data."
        )

        telegram(
            "\n".join(lines)
        )

        return

    keys = list(
        opened.keys()
    )

    lines.append(
        "Market types: "
        + ", ".join(
            map(str, keys)
        )
    )

    for market_type in (
        "binary",
        "turbo",
        "digital",
    ):

        market = opened.get(
            market_type,
            {},
        )

        if not isinstance(
            market,
            dict,
        ):

            lines.append(
                f"\n{market_type.upper()}: "
                "invalid data"
            )

            continue

        total = len(market)

        open_assets = []

        otc_assets = []

        for asset, info in market.items():

            if not isinstance(
                info,
                dict,
            ):

                continue

            if info.get(
                "open",
                False,
            ):

                name = str(asset)

                open_assets.append(
                    name
                )

                if "OTC" in name.upper():

                    otc_assets.append(
                        name
                    )

        lines.append(
            f"\n{market_type.upper()}"
        )

        lines.append(
            f"Total: {total}"
        )

        lines.append(
            f"Open: {len(open_assets)}"
        )

        lines.append(
            f"OTC: {len(otc_assets)}"
        )

        if otc_assets:

            lines.append(
                "OTC sample: "
                + ", ".join(
                    otc_assets[:15]
                )
            )

        elif open_assets:

            lines.append(
                "Open sample: "
                + ", ".join(
                    open_assets[:15]
                )
            )

        else:

            lines.append(
                "No open assets returned."
            )

    telegram(
        "\n".join(lines)
    )


# ============================================================
# REAL OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    try:

        # IMPORTANT:
        # This API version requires the
        # polling argument.
        opened = (
            iq.get_all_open_time(300)
        )

        if not opened:

            otc_diagnostic(
                {},
                "get_all_open_time returned empty",
            )

            return []

        assets = set()

        for market_type in (
            "binary",
            "turbo",
            "digital",
        ):

            market = opened.get(
                market_type,
                {},
            )

            if not isinstance(
                market,
                dict,
            ):

                continue

            for asset, info in market.items():

                if not isinstance(
                    info,
                    dict,
                ):

                    continue

                if not info.get(
                    "open",
                    False,
                ):

                    continue

                name = str(asset)

                # Only accept actual OTC names
                # returned by IQ Option.
                if "OTC" in name.upper():

                    assets.add(name)

        if not assets:

            otc_diagnostic(
                opened,
                "No open OTC symbols matched",
            )

        return sorted(
            assets
        )

    except Exception as exc:

        print(
            "OTC discovery error:",
            exc,
        )

        telegram(
            "🔴 OTC DISCOVERY ERROR\n"
            f"{type(exc).__name__}: {exc}"
        )

        return []


# ============================================================
# CONNECT
# ============================================================

def connect():

    global iq

    if not IQ_EMAIL or not IQ_PASSWORD:

        telegram(
            "🔴 IQ OPTION CREDENTIALS MISSING\n"
            "Set IQ_EMAIL and IQ_PASSWORD."
        )

        return False

    # --------------------------------------------------------
    # HARD DEMO SAFETY
    # --------------------------------------------------------

    if AUTO_TRADE:

        if PRACTICE is not True:

            telegram(
                "🛑 AUTO-TRADE BLOCKED\n"
                "PRACTICE must be True."
            )

            return False

        if DEMO_ONLY_LOCK is not True:

            telegram(
                "🛑 AUTO-TRADE BLOCKED\n"
                "DEMO_ONLY_LOCK must remain True."
            )

            return False

    try:

        iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        ok = iq.connect()

        if not ok:

            telegram(
                "🔴 IQ OPTION CONNECTION FAILED"
            )

            return False

        if not iq.check_connect():

            telegram(
                "🔴 IQ OPTION LOGIN FAILED"
            )

            return False

        # ----------------------------------------------------
        # FORCE PRACTICE ACCOUNT
        # ----------------------------------------------------

        iq.change_balance(
            "PRACTICE"
        )

        time.sleep(2)

        telegram(
            "🟢 IQ OPTION CONNECTED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Account: PRACTICE\n"
            "Automatic trading: ENABLED\n"
            "Stake: $1.00\n"
            "Expiry: 3 minutes\n"
            "Mode: DEMO TEST ONLY\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return True

    except Exception as exc:

        telegram(
            "🔴 CONNECTION ERROR\n"
            f"{type(exc).__name__}: {exc}"
        )

        return False


# ============================================================
# SIGNAL ID
# ============================================================

def signal_id(signal):

    timestamp = (
        datetime.now(
            timezone.utc
        ).strftime("%H%M%S")
    )

    asset = (
        signal["asset"]
        .replace("/", "")
        .replace("-", "")
        .upper()
    )

    return (
        f"{asset}-"
        f"{signal['direction']}-"
        f"{timestamp}"
    )


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def send_signal(
    signal,
    sid,
):

    message = (
        "🟢 BACK TO TREND SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {signal['asset']}\n"
        f"Direction: {signal['direction']}\n"
        f"Score: {signal['score']}/100\n"
        f"Reference expiry: "
        f"{EXPIRY_MINUTES} minutes\n"
        f"Price: {signal['price']}\n"
        "\n"
        f"30M Trend: "
        f"{signal['trend_30m']}\n"
        f"5M Trend: "
        f"{signal['trend_5m']}\n"
        f"1M Trigger: "
        f"{signal['trigger']}\n"
        f"30M RSI: "
        f"{signal['rsi_30m']:.1f}\n"
        f"5M RSI: "
        f"{signal['rsi_5m']:.1f}\n"
        f"1M RSI: "
        f"{signal['rsi_1m']:.1f}\n"
        f"ADX: "
        f"{signal['adx_30m']:.1f}\n"
        f"Room: "
        f"{signal['room_atr']:.2f} ATR\n"
        f"Extension: "
        f"{signal['extension_atr']:.2f} ATR\n"
        "\n"
        f"Signal ID: {sid}\n"
        "\n"
        "🤖 DEMO AUTO-TRADE"
    )

    telegram(
        message
    )


# ============================================================
# EXECUTE DEMO TRADE
# ============================================================

def execute_demo_trade(
    signal,
    sid,
):

    global last_trade_time
    global trade_count
    global active_trade

    if not AUTO_TRADE:
        return False

    # --------------------------------------------------------
    # HARD DEMO LOCK
    # --------------------------------------------------------

    if not PRACTICE:

        telegram(
            "🛑 TRADE BLOCKED\n"
            "PRACTICE is not True."
        )

        return False

    if not DEMO_ONLY_LOCK:

        telegram(
            "🛑 TRADE BLOCKED\n"
            "DEMO_ONLY_LOCK is disabled."
        )

        return False

    if active_trade is not None:

        return False

    if (
        trade_count
        >= MAX_AUTO_TRADES
    ):

        return False

    if (
        time.time()
        - last_trade_time
        < TRADE_COOLDOWN
    ):

        return False

    asset = signal["asset"]

    direction = (
        signal["direction"]
        .lower()
    )

    try:

        # ----------------------------------------------------
        # VERIFY ASSET IS STILL OPEN
        # ----------------------------------------------------

        opened = (
            iq.get_all_open_time(
                300
            )
        )

        is_open = False

        for market_type in (
            "binary",
            "turbo",
            "digital",
        ):

            market = opened.get(
                market_type,
                {},
            )

            if not isinstance(
                market,
                dict,
            ):

                continue

            info = market.get(
                asset
            )

            if (
                isinstance(
                    info,
                    dict,
                )
                and info.get(
                    "open",
                    False,
                )
            ):

                is_open = True

                break

        if not is_open:

            telegram(
                "⏭ DEMO TRADE SKIPPED\n"
                f"{asset} is no longer open."
            )

            return False

        # ----------------------------------------------------
        # TRADE MESSAGE
        # ----------------------------------------------------

        telegram(
            "🟡 PLACING DEMO TRADE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{direction.upper()}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: "
            f"{EXPIRY_MINUTES} minutes\n"
            f"Score: "
            f"{signal['score']}/100\n"
            f"Signal ID: {sid}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "PRACTICE ACCOUNT ONLY"
        )

        result = iq.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES,
        )

        order_id = None

        status = True

        if isinstance(
            result,
            tuple,
        ):

            if len(result) >= 2:

                status = bool(
                    result[0]
                )

                order_id = result[1]

            elif len(result) == 1:

                order_id = result[0]

        else:

            order_id = result

        if (
            not status
            or not order_id
        ):

            telegram(
                "🔴 DEMO ORDER FAILED\n"
                f"Asset: {asset}\n"
                f"Direction: "
                f"{direction.upper()}\n"
                f"Signal ID: {sid}\n"
                f"API response: {result}"
            )

            return False

        active_trade = {
            "id": order_id,
            "asset": asset,
            "direction": (
                direction.upper()
            ),
            "signal_id": sid,
            "stake": STAKE,
            "score": signal["score"],
            "opened_at": time.time(),
        }

        last_trade_time = (
            time.time()
        )

        trade_count += 1

        telegram(
            "🟢 DEMO TRADE OPENED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{direction.upper()}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: "
            f"{EXPIRY_MINUTES} minutes\n"
            f"Trade #: {trade_count}\n"
            f"Signal ID: {sid}\n"
            f"Order ID: {order_id}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return True

    except Exception as exc:

        telegram(
            "🔴 DEMO TRADE ERROR\n"
            f"Asset: {asset}\n"
            f"Signal ID: {sid}\n"
            f"{type(exc).__name__}: {exc}"
        )

        return False


# ============================================================
# MONITOR ACTIVE TRADE
# ============================================================

def monitor_active_trade():

    global active_trade
    global wins
    global losses
    global draws
    global total_profit
    global consecutive_losses

    if active_trade is None:
        return

    order_id = active_trade["id"]

    try:

        telegram(
            "⏳ DEMO TRADE WAITING\n"
            f"{active_trade['asset']} "
            f"{active_trade['direction']}\n"
            f"Signal: "
            f"{active_trade['signal_id']}\n"
            "Waiting for expiry/result..."
        )

        profit = None

        if hasattr(
            iq,
            "check_win_v3",
        ):

            profit = iq.check_win_v3(
                order_id
            )

        elif hasattr(
            iq,
            "check_win_v2",
        ):

            profit = iq.check_win_v2(
                order_id,
                2,
            )

        elif hasattr(
            iq,
            "check_win",
        ):

            profit = iq.check_win(
                order_id
            )

        else:

            raise RuntimeError(
                "No supported result-check "
                "method found."
            )

        profit = safe_float(
            profit,
            0.0,
        )

        total_profit += profit

        if profit > 0:

            wins += 1

            consecutive_losses = 0

            result_text = "WIN 🟢"

        elif profit < 0:

            losses += 1

            consecutive_losses += 1

            result_text = "LOSS 🔴"

        else:

            draws += 1

            result_text = "DRAW 🟡"

        total = (
            wins
            + losses
            + draws
        )

        if (
            wins + losses
            > 0
        ):

            win_rate = (
                wins
                / (
                    wins
                    + losses
                )
                * 100
            )

        else:

            win_rate = 0.0

        telegram(
            "📊 DEMO TRADE RESULT\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Result: {result_text}\n"
            f"Asset: "
            f"{active_trade['asset']}\n"
            f"Direction: "
            f"{active_trade['direction']}\n"
            f"Profit: ${profit:.2f}\n"
            f"Signal ID: "
            f"{active_trade['signal_id']}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Trades: {total}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Win rate: "
            f"{win_rate:.2f}%\n"
            f"Net demo P/L: "
            f"${total_profit:.2f}\n"
            f"Consecutive losses: "
            f"{consecutive_losses}"
        )

        active_trade = None

    except Exception as exc:

        print(
            "Result monitoring error:",
            exc,
        )

        telegram(
            "⚠️ RESULT CHECK ERROR\n"
            f"Signal: "
            f"{active_trade['signal_id']}\n"
            f"{type(exc).__name__}: {exc}"
        )


# ============================================================
# STATUS
# ============================================================

def send_status(
    assets,
):

    global last_status_time

    if (
        time.time()
        - last_status_time
        < STATUS_INTERVAL
    ):

        return

    last_status_time = (
        time.time()
    )

    total = (
        wins
        + losses
        + draws
    )

    if (
        wins + losses
        > 0
    ):

        win_rate = (
            wins
            / (
                wins
                + losses
            )
            * 100
        )

    else:

        win_rate = 0.0

    runtime = int(
        time.time()
        - session_started
    )

    hours = (
        runtime // 3600
    )

    minutes = (
        runtime % 3600
    ) // 60

    telegram(
        "🟡 BACK TO TREND STATUS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC assets: "
        f"{len(assets)}\n"
        f"Trades: "
        f"{trade_count}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: "
        f"{win_rate:.2f}%\n"
        f"Net demo P/L: "
        f"${total_profit:.2f}\n"
        f"Consecutive losses: "
        f"{consecutive_losses}\n"
        f"Active trade: "
        f"{'YES' if active_trade else 'NO'}\n"
        f"Runtime: "
        f"{hours}h {minutes}m\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Account: PRACTICE\n"
        "Auto-trading: ON\n"
        "Stake: $1\n"
        "Expiry: 3 minutes"
    )


# ============================================================
# TELEGRAM UPDATES
# ============================================================

def telegram_updates(
    offset=None,
):

    if not TELEGRAM_TOKEN:
        return []

    try:

        url = (
            "https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/getUpdates"
        )

        params = {
            "timeout": 1,
        }

        if offset is not None:

            params["offset"] = offset

        response = requests.get(
            url,
            params=params,
            timeout=5,
        )

        data = response.json()

        if not data.get(
            "ok"
        ):

            return []

        return data.get(
            "result",
            [],
        )

    except Exception:

        return []


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def handle_command(
    text,
):

    text = text.strip()

    if text == "/stats":

        total = (
            wins
            + losses
            + draws
        )

        if (
            wins + losses
            > 0
        ):

            rate = (
                wins
                / (
                    wins
                    + losses
                )
                * 100
            )

        else:

            rate = 0.0

        telegram(
            "📊 DEMO STATISTICS\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Total: {total}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Win rate: "
            f"{rate:.2f}%\n"
            f"Net P/L: "
            f"${total_profit:.2f}\n"
            f"Trades this session: "
            f"{trade_count}\n"
            f"Consecutive losses: "
            f"{consecutive_losses}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "PRACTICE ACCOUNT"
        )

    elif text == "/scan":

        telegram(
            "🔎 SCANNER STATUS\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Back to Trend is running.\n"
            "30M trend → 5M pullback → "
            "1M entry\n"
            "Expiry: 3 minutes\n"
            "Auto-trade: PRACTICE only"
        )

    elif text == "/help":

        telegram(
            "🤖 BACK TO TREND COMMANDS\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "/stats — statistics\n"
            "/scan — scanner status\n"
            "/help — commands\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Automatic trading is locked "
            "to PRACTICE."
        )


def poll_telegram_commands():

    global telegram_offset

    updates = telegram_updates(
        telegram_offset
    )

    for update in updates:

        telegram_offset = (
            update["update_id"]
            + 1
        )

        message = update.get(
            "message",
            {},
        )

        text = message.get(
            "text",
            "",
        )

        if text:

            handle_command(
                text
            )


# ============================================================
# CLEAN SIGNAL MEMORY
# ============================================================

def cleanup_signal_keys():

    if len(
        seen_signal_keys
    ) > 500:

        items = list(
            seen_signal_keys
        )

        seen_signal_keys.clear()

        seen_signal_keys.update(
            items[-250:]
        )


# ============================================================
# RUN SESSION
# ============================================================

def run_session():

    global session_started
    global trade_count
    global last_status_time

    session_started = (
        time.time()
    )

    trade_count = 0

    last_status_time = 0

    if not connect():

        time.sleep(
            RECONNECT_INTERVAL
        )

        return

    telegram(
        "🟡 BACK TO TREND SCANNER ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: Back to Trend\n"
        "Context: 30M\n"
        "Pullback: 5M\n"
        "Entry: 1M\n"
        "Expiry: 3 minutes\n"
        "Scan interval: 60 seconds\n"
        "Status interval: 5 minutes\n"
        "Automatic demo trading: ON\n"
        "Stake: $1.00\n"
        f"Max trades/session: "
        f"{MAX_AUTO_TRADES}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "PRACTICE ACCOUNT ONLY"
    )

    while (
        time.time()
        - session_started
        < MAX_RUNTIME
    ):

        try:

            poll_telegram_commands()

            # ------------------------------------------------
            # EXISTING TRADE
            # ------------------------------------------------

            if active_trade is not None:

                monitor_active_trade()

                time.sleep(5)

                continue

            # ------------------------------------------------
            # CONSECUTIVE LOSS STOP
            # ------------------------------------------------

            if (
                consecutive_losses
                >= MAX_CONSECUTIVE_LOSSES
            ):

                telegram(
                    "🛑 DEMO AUTO-TRADING PAUSED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Consecutive losses: "
                    f"{consecutive_losses}\n"
                    f"Limit: "
                    f"{MAX_CONSECUTIVE_LOSSES}\n"
                    "\n"
                    "Scanner remains connected "
                    "but will not place more "
                    "automatic trades this session."
                )

                while True:

                    poll_telegram_commands()

                    send_status([])

                    time.sleep(30)

            # ------------------------------------------------
            # TRADE LIMIT
            # ------------------------------------------------

            if (
                trade_count
                >= MAX_AUTO_TRADES
            ):

                telegram(
                    "🏁 DEMO TEST SESSION COMPLETE\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Trades: "
                    f"{trade_count}\n"
                    f"Wins: {wins}\n"
                    f"Losses: {losses}\n"
                    f"Draws: {draws}\n"
                    f"Net P/L: "
                    f"${total_profit:.2f}\n"
                    "\n"
                    "No more automatic trades "
                    "will be placed this session."
                )

                while True:

                    poll_telegram_commands()

                    send_status([])

                    time.sleep(60)

            # ------------------------------------------------
            # REAL OTC DISCOVERY
            # ------------------------------------------------

            assets = (
                discover_otc_assets()
            )

            if not assets:

                telegram(
                    "🟠 NO OTC ASSETS FOUND\n"
                    "Scanner remains active.\n"
                    "Retrying automatically."
                )

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            # ------------------------------------------------
            # STATUS
            # ------------------------------------------------

            send_status(
                assets
            )

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            found_signal = False

            for asset in assets:

                try:

                    poll_telegram_commands()

                    signal = (
                        evaluate_asset(
                            asset
                        )
                    )

                    if not signal:
                        continue

                    if (
                        signal["score"]
                        < MIN_SCORE
                    ):

                        continue

                    key = (
                        signal["asset"],
                        signal["direction"],
                        int(
                            server_now()
                            / TF_1M
                        ),
                    )

                    if (
                        key
                        in seen_signal_keys
                    ):

                        continue

                    seen_signal_keys.add(
                        key
                    )

                    cleanup_signal_keys()

                    sid = signal_id(
                        signal
                    )

                    send_signal(
                        signal,
                        sid,
                    )

                    found_signal = True

                    # ------------------------------------------------
                    # AUTOMATIC DEMO TRADE
                    # ------------------------------------------------

                    execute_demo_trade(
                        signal,
                        sid,
                    )

                    if (
                        active_trade
                        is not None
                    ):

                        break

                except Exception as exc:

                    print(
                        f"Asset scan error "
                        f"{asset}: {exc}"
                    )

            if not found_signal:

                print(
                    f"[{utc_text()}] "
                    f"No qualified signal. "
                    f"OTC assets: "
                    f"{len(assets)}"
                )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            telegram(
                "🛑 SCANNER STOPPED"
            )

            return

        except Exception as exc:

            print(
                traceback.format_exc()
            )

            telegram(
                "⚠️ SCANNER ERROR\n"
                f"{type(exc).__name__}: {exc}\n"
                "Recovering automatically..."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # FINAL SAFETY CHECK
    # --------------------------------------------------------

    if AUTO_TRADE:

        if PRACTICE is not True:

            print(
                "FATAL: PRACTICE must be True."
            )

            return

        if (
            DEMO_ONLY_LOCK
            is not True
        ):

            print(
                "FATAL: DEMO_ONLY_LOCK "
                "must be True."
            )

            return

    telegram(
        "🚀 BACK TO TREND DEMO TESTER STARTING\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "30M Trend → 5M Pullback → 1M Entry\n"
        "Expiry: 3 minutes\n"
        "Stake: $1\n"
        "Automatic trading: ON\n"
        "Account: PRACTICE\n"
        "Real account: HARD BLOCKED\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    while True:

        try:

            run_session()

        except KeyboardInterrupt:

            telegram(
                "🛑 BACK TO TREND STOPPED"
            )

            break

        except Exception as exc:

            print(
                traceback.format_exc()
            )

            telegram(
                "🔴 FATAL SESSION ERROR\n"
                f"{type(exc).__name__}: {exc}\n"
                "Restarting..."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
