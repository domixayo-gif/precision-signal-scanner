import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# BACK TO TREND — IQ OPTION OTC SCANNER
# ============================================================
# READ-ONLY SCANNER
#
# 30M = PRIMARY TREND / CONTEXT
# 5M  = PULLBACK / STRUCTURE
# 1M  = ENTRY TRIGGER
# EXPIRY = 3 MINUTES
#
# Continuous scanning
# No automatic trading
# No forced signals
# ============================================================


# =========================
# CONFIGURATION
# =========================

PRACTICE = True

TF_30M = 1800
TF_5M = 300
TF_1M = 60

CANDLES_30M = 180
CANDLES_5M = 180
CANDLES_1M = 180

EXPIRY_MINUTES = 3

SCAN_INTERVAL = 60
STATUS_INTERVAL = 300

# Run continuously for 24 hours per session.
# The scanner automatically keeps going through scan errors.
MAX_RUNTIME = 24 * 60 * 60

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

SWING_LOOKBACK = 30

# High-precision filters
MIN_SCORE = 90
MIN_ADX = 20

MIN_ROOM_ATR = 1.20
MAX_TRIGGER_RANGE_ATR = 1.50
MAX_EXTENSION_ATR = 2.20
MAX_5M_DISTANCE_ATR = 0.65


# =========================
# ENVIRONMENT
# =========================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# =========================
# GLOBAL STATE
# =========================

iq = None

signals_sent = 0
scan_cycles = 0
assets_scanned = 0
errors_count = 0
last_signal_time = None
last_status_time = 0

started_at = time.time()


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        return response.ok

    except Exception:
        return False


# =========================
# SAFE HELPERS
# =========================

def safe_float(value, default=None):
    try:
        x = float(value)

        if math.isnan(x) or math.isinf(x):
            return default

        return x

    except Exception:
        return default


def mean(values):
    values = [
        safe_float(x)
        for x in values
        if safe_float(x) is not None
    ]

    if not values:
        return None

    return sum(values) / len(values)


def clamp(value, low, high):
    return max(low, min(high, value))


# =========================
# INDICATORS
# =========================

def ema(values, period):
    values = [
        safe_float(x)
        for x in values
        if safe_float(x) is not None
    ]

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (
            price - result
        ) * multiplier + result

    return result


def rsi(values, period=14):
    values = [
        safe_float(x)
        for x in values
        if safe_float(x) is not None
    ]

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = values[i] - values[i - 1]

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]

        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            (avg_gain * (period - 1)) + gain
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + loss
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        high = safe_float(current["high"])
        low = safe_float(current["low"])
        prev_close = safe_float(previous["close"])

        if None in (high, low, prev_close):
            continue

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        high = safe_float(current["high"])
        low = safe_float(current["low"])
        prev_high = safe_float(previous["high"])
        prev_low = safe_float(previous["low"])
        prev_close = safe_float(previous["close"])

        if None in (
            high,
            low,
            prev_high,
            prev_low,
            prev_close
        ):
            continue

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        up_move = high - prev_high
        down_move = prev_low - low

        pdm = (
            up_move
            if up_move > down_move and up_move > 0
            else 0
        )

        mdm = (
            down_move
            if down_move > up_move and down_move > 0
            else 0
        )

        trs.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    if len(trs) < period * 2:
        return None

    dx_values = []

    for i in range(period, len(trs)):
        tr_sum = sum(trs[i - period + 1:i + 1])
        plus_sum = sum(plus_dm[i - period + 1:i + 1])
        minus_sum = sum(minus_dm[i - period + 1:i + 1])

        if tr_sum == 0:
            continue

        plus_di = 100 * plus_sum / tr_sum
        minus_di = 100 * minus_sum / tr_sum

        denominator = plus_di + minus_di

        if denominator == 0:
            continue

        dx = (
            100
            * abs(plus_di - minus_di)
            / denominator
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    return sum(dx_values[-period:]) / period


# =========================
# CANDLE HELPERS
# =========================

def candle_range(candle):
    return (
        safe_float(candle["high"], 0)
        - safe_float(candle["low"], 0)
    )


def candle_body(candle):
    return abs(
        safe_float(candle["close"], 0)
        - safe_float(candle["open"], 0)
    )


def bullish(candle):
    return (
        safe_float(candle["close"], 0)
        > safe_float(candle["open"], 0)
    )


def bearish(candle):
    return (
        safe_float(candle["close"], 0)
        < safe_float(candle["open"], 0)
    )


def bullish_engulfing(previous, current):
    return (
        bearish(previous)
        and bullish(current)
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        bullish(previous)
        and bearish(current)
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bullish_pinbar(candle):
    high = safe_float(candle["high"], 0)
    low = safe_float(candle["low"], 0)
    open_price = safe_float(candle["open"], 0)
    close = safe_float(candle["close"], 0)

    body = abs(close - open_price)
    lower_wick = min(open_price, close) - low
    upper_wick = high - max(open_price, close)

    if body <= 0:
        return False

    return (
        lower_wick >= body * 2
        and lower_wick > upper_wick
        and close > low + (
            (high - low) * 0.50
        )
    )


def bearish_pinbar(candle):
    high = safe_float(candle["high"], 0)
    low = safe_float(candle["low"], 0)
    open_price = safe_float(candle["open"], 0)
    close = safe_float(candle["close"], 0)

    body = abs(close - open_price)
    lower_wick = min(open_price, close) - low
    upper_wick = high - max(open_price, close)

    if body <= 0:
        return False

    return (
        upper_wick >= body * 2
        and upper_wick > lower_wick
        and close < high - (
            (high - low) * 0.50
        )
    )


# =========================
# CLOSED CANDLES
# =========================

def get_closed_candles(asset, timeframe, count):
    """
    Retrieve candles and remove the currently forming candle.
    """

    global iq

    if iq is None:
        return []

    now = int(time.time())

    try:
        server_time = iq.get_server_timestamp()

        if server_time:
            now = int(server_time)

    except Exception:
        pass

    try:
        candles = iq.get_candles(
            asset,
            timeframe,
            count + 5,
            now
        )

        if not candles:
            return []

        candles = sorted(
            candles,
            key=lambda x: x["from"]
        )

        closed = []

        for candle in candles:
            candle_time = int(candle["from"])

            if candle_time + timeframe <= now:
                closed.append(candle)

        return closed[-count:]

    except Exception:
        return []


# =========================
# TREND ANALYSIS
# =========================

def analyze_trend(candles):
    if len(candles) < EMA_SLOW + 10:
        return None

    closes = [
        safe_float(c["close"])
        for c in candles
    ]

    closes = [
        x for x in closes
        if x is not None
    ]

    if len(closes) < EMA_SLOW + 5:
        return None

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    current = closes[-1]

    rsi_value = rsi(closes, RSI_PERIOD)
    atr_value = atr(candles, ATR_PERIOD)
    adx_value = adx(candles, ADX_PERIOD)

    if None in (
        fast,
        slow,
        current,
        atr_value
    ):
        return None

    if fast > slow and current > fast:
        direction = "BULLISH"

    elif fast < slow and current < fast:
        direction = "BEARISH"

    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "ema_fast": fast,
        "ema_slow": slow,
        "price": current,
        "rsi": rsi_value,
        "atr": atr_value,
        "adx": adx_value,
    }


# =========================
# STRUCTURE
# =========================

def get_structure(candles):
    if len(candles) < SWING_LOOKBACK:
        return None

    recent = candles[-SWING_LOOKBACK:]

    highs = [
        safe_float(c["high"])
        for c in recent
    ]

    lows = [
        safe_float(c["low"])
        for c in recent
    ]

    highs = [x for x in highs if x is not None]
    lows = [x for x in lows if x is not None]

    if not highs or not lows:
        return None

    return {
        "resistance": max(highs),
        "support": min(lows),
    }


# =========================
# 5M PULLBACK
# =========================

def pullback_quality(candles, direction, atr_value):
    if len(candles) < 10 or not atr_value:
        return False, 0, "insufficient structure"

    current = candles[-1]

    current_price = safe_float(current["close"])

    if current_price is None:
        return False, 0, "invalid price"

    recent = candles[-8:]

    recent_high = max(
        safe_float(c["high"], 0)
        for c in recent
    )

    recent_low = min(
        safe_float(c["low"], 0)
        for c in recent
    )

    if direction == "BULLISH":
        distance = abs(
            current_price - recent_high
        )

        if distance <= atr_value * MAX_5M_DISTANCE_ATR:
            return True, 15, "bullish pullback"

    if direction == "BEARISH":
        distance = abs(
            current_price - recent_low
        )

        if distance <= atr_value * MAX_5M_DISTANCE_ATR:
            return True, 15, "bearish pullback"

    return False, 0, "pullback too far from trend"


# =========================
# 1M TRIGGER
# =========================

def trigger_quality(candles, direction, atr_value):
    if len(candles) < 5 or not atr_value:
        return False, 0, "insufficient entry candles"

    current = candles[-1]
    previous = candles[-2]

    rng = candle_range(current)
    body = candle_body(current)

    if rng <= 0:
        return False, 0, "invalid trigger candle"

    if rng > atr_value * MAX_TRIGGER_RANGE_ATR:
        return False, 0, "trigger candle too large"

    body_ratio = body / rng

    if body_ratio < 0.35:
        return False, 0, "weak trigger body"

    if direction == "BULLISH":

        engulf = bullish_engulfing(
            previous,
            current
        )

        pin = bullish_pinbar(current)

        strong_close = (
            safe_float(current["close"], 0)
            >= safe_float(current["high"], 0)
            - rng * 0.25
        )

        if engulf or pin or strong_close:
            return True, 20, "bullish entry trigger"

    elif direction == "BEARISH":

        engulf = bearish_engulfing(
            previous,
            current
        )

        pin = bearish_pinbar(current)

        strong_close = (
            safe_float(current["close"], 0)
            <= safe_float(current["low"], 0)
            + rng * 0.25
        )

        if engulf or pin or strong_close:
            return True, 20, "bearish entry trigger"

    return False, 0, "no valid entry trigger"


# =========================
# ASSET EVALUATION
# =========================

def evaluate_asset(asset):
    try:

        # --------------------------------
        # 30M PRIMARY TREND
        # --------------------------------

        candles_30m = get_closed_candles(
            asset,
            TF_30M,
            CANDLES_30M
        )

        if len(candles_30m) < EMA_SLOW + 10:
            return None

        trend_30m = analyze_trend(
            candles_30m
        )

        if not trend_30m:
            return None

        if trend_30m["direction"] == "NEUTRAL":
            return None

        if (
            trend_30m["adx"] is None
            or trend_30m["adx"] < MIN_ADX
        ):
            return None

        direction = trend_30m["direction"]

        # --------------------------------
        # 5M STRUCTURE
        # --------------------------------

        candles_5m = get_closed_candles(
            asset,
            TF_5M,
            CANDLES_5M
        )

        if len(candles_5m) < EMA_SLOW + 10:
            return None

        trend_5m = analyze_trend(
            candles_5m
        )

        if not trend_5m:
            return None

        # 30M and 5M must agree.
        if trend_5m["direction"] != direction:
            return None

        if (
            trend_5m["adx"] is None
            or trend_5m["adx"] < MIN_ADX
        ):
            return None

        atr_5m = trend_5m["atr"]

        # --------------------------------
        # 5M PULLBACK
        # --------------------------------

        pullback_ok, pullback_points, pullback_reason = (
            pullback_quality(
                candles_5m,
                direction,
                atr_5m
            )
        )

        if not pullback_ok:
            return None

        # --------------------------------
        # 1M ENTRY
        # --------------------------------

        candles_1m = get_closed_candles(
            asset,
            TF_1M,
            CANDLES_1M
        )

        if len(candles_1m) < 20:
            return None

        trigger_ok, trigger_points, trigger_reason = (
            trigger_quality(
                candles_1m,
                direction,
                atr_5m
            )
        )

        if not trigger_ok:
            return None

        # --------------------------------
        # ROOM CHECK
        # --------------------------------

        structure = get_structure(
            candles_5m
        )

        if not structure:
            return None

        price = trend_5m["price"]

        if direction == "BULLISH":
            room = structure["resistance"] - price

        else:
            room = price - structure["support"]

        room_atr = room / atr_5m if atr_5m else 0

        if room_atr < MIN_ROOM_ATR:
            return None

        # --------------------------------
        # EXTENSION CHECK
        # --------------------------------

        extension = abs(
            price - trend_5m["ema_fast"]
        ) / atr_5m

        if extension > MAX_EXTENSION_ATR:
            return None

        # --------------------------------
        # RSI CHECK
        # --------------------------------

        rsi_value = trend_5m["rsi"]

        if rsi_value is None:
            return None

        if direction == "BULLISH":
            if rsi_value >= 72:
                return None

        else:
            if rsi_value <= 28:
                return None

        # --------------------------------
        # SCORE
        # --------------------------------

        score = 0

        # 30M trend
        score += 25

        # 5M trend agreement
        score += 20

        # ADX quality
        if trend_30m["adx"] >= 25:
            score += 10
        else:
            score += 5

        if trend_5m["adx"] >= 25:
            score += 10
        else:
            score += 5

        # Pullback
        score += pullback_points

        # Trigger
        score += trigger_points

        # Room
        if room_atr >= 2.0:
            score += 10
        elif room_atr >= 1.5:
            score += 7
        else:
            score += 5

        # RSI alignment
        if direction == "BULLISH":
            if 45 <= rsi_value <= 65:
                score += 5

        else:
            if 35 <= rsi_value <= 55:
                score += 5

        score = int(clamp(score, 0, 100))

        if score < MIN_SCORE:
            return None

        return {
            "asset": asset,
            "direction": (
                "CALL"
                if direction == "BULLISH"
                else "PUT"
            ),
            "score": score,
            "price": price,
            "trend_30m": trend_30m["direction"],
            "trend_5m": trend_5m["direction"],
            "rsi_5m": rsi_value,
            "adx_30m": trend_30m["adx"],
            "adx_5m": trend_5m["adx"],
            "room_atr": room_atr,
            "extension_atr": extension,
            "pullback": pullback_reason,
            "trigger": trigger_reason,
        }

    except Exception:
        return None


# =========================
# SIGNAL ID
# =========================

def create_signal_id(asset, direction):
    now = datetime.now(
        timezone.utc
    ).strftime("%H%M%S")

    return f"{asset}-{direction}-{now}"


# =========================
# SIGNAL MESSAGE
# =========================

def send_signal(signal):
    global signals_sent
    global last_signal_time

    signal_id = create_signal_id(
        signal["asset"],
        signal["direction"]
    )

    message = (
        "🔴 <b>NEW QUALIFIED OTC SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset:</b> {signal['asset']}\n"
        f"<b>Direction:</b> "
        f"<b>{signal['direction']}</b>\n"
        f"<b>Score:</b> {signal['score']}/100\n"
        f"<b>Reference expiry:</b> "
        f"{EXPIRY_MINUTES} minutes\n"
        f"<b>Price:</b> {signal['price']}\n"
        "\n"
        f"<b>30M Trend:</b> {signal['trend_30m']}\n"
        f"<b>5M Trend:</b> {signal['trend_5m']}\n"
        f"<b>5M RSI:</b> {signal['rsi_5m']:.1f}\n"
        f"<b>30M ADX:</b> {signal['adx_30m']:.1f}\n"
        f"<b>5M ADX:</b> {signal['adx_5m']:.1f}\n"
        f"<b>Room:</b> {signal['room_atr']:.2f} ATR\n"
        f"<b>Extension:</b> "
        f"{signal['extension_atr']:.2f} ATR\n"
        "\n"
        f"<b>Pullback:</b> {signal['pullback']}\n"
        f"<b>Trigger:</b> {signal['trigger']}\n"
        "\n"
        f"<b>Signal ID:</b> <code>{signal_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "READ-ONLY SCANNER"
    )

    if send_telegram(message):
        signals_sent += 1
        last_signal_time = time.time()


# =========================
# OTC DISCOVERY
# =========================

def discover_otc_assets():
    global iq

    if iq is None:
        return []

    assets = set()

    try:
        open_time = iq.get_all_open_time()

        if not open_time:
            return []

        for market_type in (
            "binary",
            "turbo",
            "digital"
        ):

            market = open_time.get(
                market_type,
                {}
            )

            if not isinstance(market, dict):
                continue

            for asset, info in market.items():

                if not isinstance(info, dict):
                    continue

                if not info.get("open", False):
                    continue

                name = str(asset).upper()

                if "-OTC" in name:
                    assets.add(asset)

    except Exception:
        return []

    return sorted(assets)


# =========================
# CONNECTION
# =========================

def connect():
    global iq

    try:

        iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        check, reason = iq.connect()

        if not check:
            send_telegram(
                "🔴 <b>IQ OPTION CONNECTION FAILED</b>\n"
                f"<code>{reason}</code>"
            )

            return False

        try:
            if PRACTICE:
                iq.change_balance("PRACTICE")
            else:
                iq.change_balance("REAL")
        except Exception:
            pass

        return True

    except Exception as e:

        send_telegram(
            "🔴 <b>CONNECTION ERROR</b>\n"
            f"<code>{str(e)[:300]}</code>"
        )

        return False


# =========================
# STATUS
# =========================

def send_status(assets):
    uptime = int(time.time() - started_at)

    hours = uptime // 3600
    minutes = (uptime % 3600) // 60

    message = (
        "🟡 <b>BACK TO TREND STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>OTC assets:</b> {len(assets)}\n"
        f"<b>Scan cycles:</b> {scan_cycles}\n"
        f"<b>Assets scanned:</b> {assets_scanned}\n"
        f"<b>Qualified signals:</b> {signals_sent}\n"
        f"<b>Errors recovered:</b> {errors_count}\n"
        f"<b>Uptime:</b> {hours}h {minutes}m\n"
        "\n"
        "<b>30M:</b> Trend\n"
        "<b>5M:</b> Pullback\n"
        "<b>1M:</b> Entry\n"
        f"<b>Expiry:</b> {EXPIRY_MINUTES} minutes\n"
        "\n"
        "Scanner is still running.\n"
        "No forced signals.\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    send_telegram(message)


# =========================
# SCAN ONE ASSET
# =========================

def scan_asset(asset):
    global assets_scanned
    global errors_count

    try:

        signal = evaluate_asset(asset)

        assets_scanned += 1

        if signal:
            send_signal(signal)

    except Exception:

        errors_count += 1

        return


# =========================
# MAIN CONTINUOUS LOOP
# =========================

def main():
    global iq
    global scan_cycles
    global errors_count
    global last_status_time

    send_telegram(
        "🟡 <b>BACK TO TREND SCANNER ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "IQ Option OTC connection: "
        "<b>CONNECTING...</b>\n"
        "Strategy: <b>Back to Trend</b>\n"
        "Context: <b>30M</b>\n"
        "Pullback: <b>5M</b>\n"
        "Entry: <b>1M</b>\n"
        f"Expiry: <b>{EXPIRY_MINUTES} minutes</b>\n"
        f"Scan interval: <b>{SCAN_INTERVAL} seconds</b>\n"
        f"Status interval: <b>{STATUS_INTERVAL // 60} minutes</b>\n"
        "Maximum runtime: <b>24 hours</b>\n"
        "Mode: <b>READ-ONLY</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    # Initial connection
    while True:

        if connect():
            break

        time.sleep(30)

    send_telegram(
        "🟢 <b>IQ OPTION OTC CONNECTION: OK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: <b>Back to Trend</b>\n"
        "30M trend → 5M pullback → 1M trigger\n"
        f"Reference expiry: <b>{EXPIRY_MINUTES} minutes</b>\n"
        "Scanner will continue running."
    )

    session_start = time.time()

    while True:

        # --------------------------------
        # 24-HOUR SESSION CHECK
        # --------------------------------

        if time.time() - session_start >= MAX_RUNTIME:

            send_telegram(
                "🟢 <b>24-HOUR SCANNER SESSION COMPLETE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Scan cycles: {scan_cycles}\n"
                f"Qualified signals: {signals_sent}\n"
                f"Recovered errors: {errors_count}\n"
                "The current session has ended safely."
            )

            break

        cycle_start = time.time()

        scan_cycles += 1

        # --------------------------------
        # DISCOVER OTC
        # --------------------------------

        try:
            assets = discover_otc_assets()

        except Exception:
            assets = []
            errors_count += 1

        # --------------------------------
        # NO ASSETS
        # --------------------------------

        if not assets:

            errors_count += 1

            send_telegram(
                "🟠 <b>NO OTC ASSETS FOUND</b>\n"
                "The scanner is still running.\n"
                "Retrying automatically..."
            )

            time.sleep(30)

            continue

        # --------------------------------
        # SCAN ASSETS
        # --------------------------------

        for asset in assets:

            try:

                scan_asset(asset)

            except Exception:

                errors_count += 1

            # Keep individual assets from
            # monopolizing the scanner.
            time.sleep(0.20)

        # --------------------------------
        # PERIODIC STATUS
        # --------------------------------

        if (
            time.time() - last_status_time
            >= STATUS_INTERVAL
        ):

            send_status(assets)

            last_status_time = time.time()

        # --------------------------------
        # WAIT UNTIL NEXT CYCLE
        # --------------------------------

        elapsed = time.time() - cycle_start

        wait_time = max(
            5,
            SCAN_INTERVAL - elapsed
        )

        time.sleep(wait_time)


# =========================
# CRASH RECOVERY
# =========================

if __name__ == "__main__":

    while True:

        try:

            main()

            # Main normally exits after 24 hours.
            # Give the process a short pause before
            # starting a fresh continuous session.

            send_telegram(
                "🔄 <b>STARTING NEW SCANNER SESSION</b>\n"
                "The scanner will continue automatically."
            )

            time.sleep(10)

        except KeyboardInterrupt:

            send_telegram(
                "⛔ <b>SCANNER STOPPED</b>\n"
                "Manual interruption received."
            )

            break

        except Exception as e:

            errors_count += 1

            send_telegram(
                "🔴 <b>SCANNER ERROR — RECOVERY MODE</b>\n"
                f"<code>{str(e)[:500]}</code>\n"
                "\n"
                "The scanner will reconnect and continue."
            )

            time.sleep(30)

            continue

Important: this version is deliberately continuous. A failed candle request, empty OTC list, or individual asset error does not terminate the scanner. It retries and continues scanning.

The reference expiry is 3 minutes, not 30 minutes.nutes.
