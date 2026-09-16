import os
import json
import html
import time
import subprocess
from datetime import datetime, timezone

import requests

# ============================================================
# PRECISION SIGNAL SCANNER V3.2
# 5M trend + 1M entry confirmation
# Reference expiry: 10 minutes
#
# V3.2 CHANGES:
# - Keeps the V3.1 core strategy
# - Removes the overly strict "everything must agree" filter
# - Uses critical safety filters + 80/100 quality score
# - Pullback, RSI, MACD, candle and ADX can now be partial
# - Adds rejection diagnostics
# - Adds borderline 75-79 setups to the report for monitoring
# - Keeps qualified Telegram signals at 80+
#
# IMPORTANT:
# - Coinbase spot data is used only as a market-data proxy.
# - This script does NOT connect to or execute Pocket Option trades.
# - Score = setup quality, NOT probability of winning.
# - Binary/options-style trading is high risk.
# - Use demo/forward testing before risking money.
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))

TRACKER_FILE = "tracker.json"
COINBASE_BASE = "https://api.exchange.coinbase.com"

ASSET_BASES = [
    "BTC", "ETH", "SOL", "BNB", "ADA", "TRX", "LINK",
    "TON", "AVAX", "DOGE", "DOT", "LTC", "POL"
]

MAIN_SECONDS = 300
ENTRY_SECONDS = 60

MIN_SCORE = 80
BORDERLINE_SCORE = 75

REQUEST_TIMEOUT = 20
MAX_TRACKER_ITEMS = 500


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def tg(method, data=None):
    if not TELEGRAM_TOKEN:
        return None

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/"
        + method
    )

    response = requests.post(
        url,
        data=data or {},
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    payload = response.json()

    if not payload.get("ok"):
        raise RuntimeError(str(payload))

    return payload.get("result")


def send_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; message not sent.")
        return False

    try:
        for start in range(0, len(text), 3900):
            tg(
                "sendMessage",
                {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text[start:start + 3900],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true"
                }
            )

        return True

    except Exception as exc:
        print("Telegram send error:", exc)
        return False


def api_get(url, params=None):
    response = requests.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": "precision-signal-scanner/3.2"
        }
    )

    response.raise_for_status()

    return response.json()


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


# ============================================================
# TRACKER
# ============================================================

def load_tracker():
    if not os.path.exists(TRACKER_FILE):
        return {
            "signals": [],
            "offset": 0
        }

    try:
        with open(
            TRACKER_FILE,
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        if isinstance(data, dict):
            data.setdefault("signals", [])
            data.setdefault("offset", 0)

            if not isinstance(data["signals"], list):
                data["signals"] = []

            return data

        if isinstance(data, list):
            return {
                "signals": data,
                "offset": 0
            }

    except Exception as exc:
        print("Tracker read error:", exc)

    return {
        "signals": [],
        "offset": 0
    }


def save_tracker(data):
    signals = data.get("signals", [])

    data["signals"] = signals[-MAX_TRACKER_ITEMS:]

    with open(
        TRACKER_FILE,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            data,
            file,
            indent=2
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def help_text():
    return (
        "🤖 <b>PRECISION SCANNER V3.2</b>\n\n"
        "5M trend + 1M entry confirmation\n"
        "Reference expiry: <b>10 MINUTES</b>\n\n"
        "<b>Commands</b>\n"
        "/start - show help\n"
        "/win SIGNAL-ID - record WIN\n"
        "/loss SIGNAL-ID - record LOSS\n"
        "/stats - performance report\n\n"
        "⚠️ DEMO/TESTING ONLY.\n"
        "Coinbase is a market-data proxy and may differ from OTC pricing."
    )


def process_commands(data):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    changed = False

    try:
        updates = tg(
            "getUpdates",
            {
                "offset": int(data.get("offset", 0)) + 1,
                "limit": 100,
                "timeout": 1
            }
        ) or []

    except Exception as exc:
        print("Telegram command error:", exc)
        return False

    for update in updates:
        data["offset"] = update.get(
            "update_id",
            data.get("offset", 0)
        )

        message = update.get("message", {})

        chat_id = str(
            message.get("chat", {}).get("id", "")
        )

        if chat_id != TELEGRAM_CHAT_ID:
            continue

        text = str(
            message.get("text", "")
        ).strip()

        parts = text.split()

        if not parts:
            continue

        command = parts[0].split("@")[0].lower()

        if command == "/start":
            send_message(help_text())
            continue

        if command == "/stats":
            send_message(stats_text(data))
            continue

        if command in ("/win", "/loss"):

            if len(parts) != 2:
                send_message(
                    "⚠️ Use <code>"
                    + command
                    + " SIGNAL-ID</code>"
                )
                continue

            wanted = parts[1]
            found = None

            for item in data["signals"]:
                if item.get("id") == wanted:
                    found = item
                    break

            if found is None:
                send_message(
                    "❌ Signal not found: <code>"
                    + html.escape(wanted)
                    + "</code>"
                )
                continue

            if found.get("result", "PENDING") != "PENDING":
                send_message(
                    "⚠️ <code>"
                    + html.escape(wanted)
                    + "</code> is already <b>"
                    + html.escape(
                        str(found.get("result"))
                    )
                    + "</b>."
                )
                continue

            result = (
                "WIN"
                if command == "/win"
                else "LOSS"
            )

            found["result"] = result
            found["result_time"] = iso_now()

            changed = True

            send_message(
                "✅ <b>RESULT RECORDED</b>\n"
                "Signal: <code>"
                + html.escape(wanted)
                + "</code>\n"
                "Result: <b>"
                + result
                + "</b>"
            )

            continue

        if command.startswith("/"):
            send_message(
                "❓ Unknown command.\n\n"
                + help_text()
            )

    if changed:
        save_tracker(data)

    return changed


# ============================================================
# MARKET DATA
# ============================================================

def get_markets():
    raw = api_get(
        COINBASE_BASE + "/products"
    )

    found = {}

    for item in raw:

        if item.get("status") != "online":
            continue

        base = item.get("base_currency")
        quote = item.get("quote_currency")

        if (
            base in ASSET_BASES
            and quote in ("USD", "USDC")
        ):
            if base not in found:
                found[base] = item.get("id")

    return found


def get_candles(
    product_id,
    granularity,
    limit=220
):
    raw = api_get(
        COINBASE_BASE
        + "/products/"
        + product_id
        + "/candles",
        {
            "granularity": granularity
        }
    )

    candles = []

    for row in raw:

        if not isinstance(row, list):
            continue

        if len(row) < 6:
            continue

        candles.append(
            {
                "time": int(row[0]),
                "low": float(row[1]),
                "high": float(row[2]),
                "open": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5])
            }
        )

    candles.sort(
        key=lambda x: x["time"]
    )

    current = int(time.time())

    candles = [
        candle
        for candle in candles
        if candle["time"] + granularity <= current
    ]

    return candles[-limit:]


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    multiplier = 2.0 / (
        period + 1.0
    )

    seed = sum(
        values[:period]
    ) / period

    result[period - 1] = seed

    previous = seed

    for i in range(
        period,
        len(values)
    ):
        previous = (
            values[i] - previous
        ) * multiplier + previous

        result[i] = previous

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = [0.0] * len(values)
    losses = [0.0] * len(values)

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        gains[i] = max(
            change,
            0.0
        )

        losses[i] = max(
            -change,
            0.0
        )

    avg_gain = (
        sum(gains[1:period + 1])
        / period
    )

    avg_loss = (
        sum(losses[1:period + 1])
        / period
    )

    def calc(gain, loss):

        if loss == 0:
            return 100.0

        rs = gain / loss

        return (
            100.0
            - 100.0 / (1.0 + rs)
        )

    result[period] = calc(
        avg_gain,
        avg_loss
    )

    for i in range(
        period + 1,
        len(values)
    ):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / period

        result[i] = calc(
            avg_gain,
            avg_loss
        )

    return result


def true_ranges(candles):
    tr = [0.0] * len(candles)

    for i, candle in enumerate(candles):

        if i == 0:
            tr[i] = (
                candle["high"]
                - candle["low"]
            )
            continue

        previous_close = (
            candles[i - 1]["close"]
        )

        tr[i] = max(
            candle["high"]
            - candle["low"],

            abs(
                candle["high"]
                - previous_close
            ),

            abs(
                candle["low"]
                - previous_close
            )
        )

    return tr


def atr(candles, period=14):
    tr = true_ranges(candles)

    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    value = (
        sum(tr[1:period + 1])
        / period
    )

    result[period] = value

    for i in range(
        period + 1,
        len(candles)
    ):

        value = (
            value * (period - 1)
            + tr[i]
        ) / period

        result[i] = value

    return result


def adx_dmi(candles, period=14):
    n = len(candles)

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    tr = true_ranges(candles)

    for i in range(1, n):

        up = (
            candles[i]["high"]
            - candles[i - 1]["high"]
        )

        down = (
            candles[i - 1]["low"]
            - candles[i]["low"]
        )

        if up > down and up > 0:
            plus_dm[i] = up

        if down > up and down > 0:
            minus_dm[i] = down

    plus_di = [None] * n
    minus_di = [None] * n
    adx = [None] * n

    if n <= period * 2:
        return (
            plus_di,
            minus_di,
            adx
        )

    tr_sum = sum(
        tr[1:period + 1]
    )

    plus_sum = sum(
        plus_dm[1:period + 1]
    )

    minus_sum = sum(
        minus_dm[1:period + 1]
    )

    dx_values = []

    for i in range(
        period,
        n
    ):

        if i > period:

            tr_sum = (
                tr_sum
                - tr_sum / period
                + tr[i]
            )

            plus_sum = (
                plus_sum
                - plus_sum / period
                + plus_dm[i]
            )

            minus_sum = (
                minus_sum
                - minus_sum / period
                + minus_dm[i]
            )

        if tr_sum == 0:
            pdi = 0.0
            mdi = 0.0

        else:
            pdi = (
                100.0
                * plus_sum
                / tr_sum
            )

            mdi = (
                100.0
                * minus_sum
                / tr_sum
            )

        plus_di[i] = pdi
        minus_di[i] = mdi

        denominator = (
            pdi + mdi
        )

        dx = (
            0.0
            if denominator == 0
            else
            100.0
            * abs(pdi - mdi)
            / denominator
        )

        dx_values.append(dx)

        if len(dx_values) == period:

            adx[i] = (
                sum(dx_values)
                / period
            )

        elif (
            len(dx_values) > period
            and adx[i - 1] is not None
        ):

            adx[i] = (
                adx[i - 1]
                * (period - 1)
                + dx
            ) / period

    return (
        plus_di,
        minus_di,
        adx
    )


def macd(
    values,
    fast=12,
    slow=26,
    signal=9
):
    fast_ema = ema(
        values,
        fast
    )

    slow_ema = ema(
        values,
        slow
    )

    line = [None] * len(values)

    for i in range(len(values)):

        if (
            fast_ema[i] is not None
            and slow_ema[i] is not None
        ):
            line[i] = (
                fast_ema[i]
                - slow_ema[i]
            )

    valid = [
        x for x in line
        if x is not None
    ]

    signal_valid = ema(
        valid,
        signal
    )

    signal_line = [None] * len(values)

    start = (
        len(values)
        - len(valid)
    )

    for j, value in enumerate(
        signal_valid
    ):

        if value is not None:
            signal_line[
                start + j
            ] = value

    histogram = [None] * len(values)

    for i in range(len(values)):

        if (
            line[i] is not None
            and signal_line[i] is not None
        ):
            histogram[i] = (
                line[i]
                - signal_line[i]
            )

    return (
        line,
        signal_line,
        histogram
    )


# ============================================================
# PRICE ACTION / STRUCTURE
# ============================================================

def direction(candle):

    if candle["close"] > candle["open"]:
        return "BULL"

    if candle["close"] < candle["open"]:
        return "BEAR"

    return "DOJI"


def body_ratio(candle):

    full = (
        candle["high"]
        - candle["low"]
    )

    if full <= 0:
        return 0.0

    return (
        abs(
            candle["close"]
            - candle["open"]
        )
        / full
    )


def bullish_confirmation(candles):

    if len(candles) < 3:
        return False

    current = candles[-1]
    previous = candles[-2]

    strong = (
        direction(current) == "BULL"
        and body_ratio(current) >= 0.55
        and current["close"]
        > previous["close"]
    )

    engulfing = (
        direction(previous) == "BEAR"
        and direction(current) == "BULL"
        and current["open"]
        <= previous["close"]
        and current["close"]
        >= previous["open"]
    )

    reclaim = (
        current["close"]
        > previous["high"]
        and previous["low"]
        <= candles[-3]["low"]
    )

    return (
        strong
        or engulfing
        or reclaim
    )


def bearish_confirmation(candles):

    if len(candles) < 3:
        return False

    current = candles[-1]
    previous = candles[-2]

    strong = (
        direction(current) == "BEAR"
        and body_ratio(current) >= 0.55
        and current["close"]
        < previous["close"]
    )

    engulfing = (
        direction(previous) == "BULL"
        and direction(current) == "BEAR"
        and current["open"]
        >= previous["close"]
        and current["close"]
        <= previous["open"]
    )

    reclaim = (
        current["close"]
        < previous["low"]
        and previous["high"]
        >= candles[-3]["high"]
    )

    return (
        strong
        or engulfing
        or reclaim
    )


def structure(candles, lookback=20):

    if len(candles) < lookback:
        return "NEUTRAL"

    recent = candles[-lookback:]

    half = lookback // 2

    first = recent[:half]
    second = recent[half:]

    first_high = max(
        x["high"]
        for x in first
    )

    second_high = max(
        x["high"]
        for x in second
    )

    first_low = min(
        x["low"]
        for x in first
    )

    second_low = min(
        x["low"]
        for x in second
    )

    if (
        second_high > first_high
        and second_low > first_low
    ):
        return "BULL"

    if (
        second_high < first_high
        and second_low < first_low
    ):
        return "BEAR"

    return "NEUTRAL"


def levels(candles, lookback=30):

    if len(candles) < lookback + 1:
        return None, None

    window = candles[
        -(lookback + 1):-1
    ]

    support = min(
        x["low"]
        for x in window
    )

    resistance = max(
        x["high"]
        for x in window
    )

    return support, resistance


# ============================================================
# ADDITIONAL MARKET CONDITIONS
# ============================================================

def volatility_state(
    candles,
    atr_values,
    index
):
    if index < 10:
        return "NORMAL"

    current_atr = atr_values[index]

    if current_atr is None:
        return "NORMAL"

    previous_values = [
        atr_values[i]
        for i in range(
            max(0, index - 10),
            index
        )
        if atr_values[i] is not None
    ]

    if not previous_values:
        return "NORMAL"

    average_atr = (
        sum(previous_values)
        / len(previous_values)
    )

    if average_atr <= 0:
        return "NORMAL"

    ratio = (
        current_atr
        / average_atr
    )

    if ratio >= 1.8:
        return "EXPANSION"

    if ratio <= 0.65:
        return "LOW"

    return "NORMAL"


def recent_range(candles, count=12):

    recent = candles[-count:]

    if not recent:
        return 0.0

    return (
        max(
            c["high"]
            for c in recent
        )
        -
        min(
            c["low"]
            for c in recent
        )
    )


def detect_chop(
    candles,
    ema20,
    ema50,
    atr_value
):
    if (
        atr_value is None
        or atr_value <= 0
    ):
        return True

    price_range = recent_range(
        candles,
        12
    )

    if price_range < atr_value * 1.8:
        return True

    gap = abs(
        ema20 - ema50
    )

    gap_atr = gap / atr_value

    if gap_atr < 0.10:
        return True

    return False


def proximity_to_level(
    price,
    level,
    atr_value
):
    if (
        level is None
        or atr_value is None
        or atr_value <= 0
    ):
        return 999.0

    return abs(
        price - level
    ) / atr_value


def candle_strength(candles):

    if len(candles) < 2:
        return 0.0

    candle = candles[-1]

    return body_ratio(candle)


# ============================================================
# SCORING / ANALYSIS
# ============================================================

def analyze_asset(
    symbol,
    product_id
):
    candles5 = get_candles(
        product_id,
        MAIN_SECONDS
    )

    candles1 = get_candles(
        product_id,
        ENTRY_SECONDS
    )

    if (
        len(candles5) < 100
        or len(candles1) < 100
    ):
        return {
            "symbol": symbol,
            "signal": "NO TRADE",
            "score": 0,
            "status": "DATA",
            "reason": "Insufficient data",
            "diagnostics": [
                "Not enough completed candles"
            ]
        }

    close5 = [
        x["close"]
        for x in candles5
    ]

    close1 = [
        x["close"]
        for x in candles1
    ]

    e20 = ema(
        close5,
        20
    )

    e50 = ema(
        close5,
        50
    )

    rsi5 = rsi(
        close5
    )

    _, _, hist5 = macd(
        close5
    )

    atr5 = atr(
        candles5
    )

    pdi, mdi, adx = adx_dmi(
        candles5
    )

    e9 = ema(
        close1,
        9
    )

    e21 = ema(
        close1,
        21
    )

    rsi1 = rsi(
        close1
    )

    _, _, hist1 = macd(
        close1
    )

    i5 = len(candles5) - 1
    i1 = len(candles1) - 1

    required = [
        e20[i5],
        e50[i5],
        rsi5[i5],
        hist5[i5],
        atr5[i5],
        pdi[i5],
        mdi[i5],
        adx[i5],
        e9[i1],
        e21[i1],
        rsi1[i1],
        hist1[i1]
    ]

    if any(
        x is None
        for x in required
    ):
        return {
            "symbol": symbol,
            "signal": "NO TRADE",
            "score": 0,
            "status": "DATA",
            "reason": "Indicators not ready",
            "diagnostics": [
                "Indicator calculation incomplete"
            ]
        }

    price = close5[i5]
    price1 = close1[i1]

    ema20 = e20[i5]
    ema50 = e50[i5]

    r5 = rsi5[i5]
    h5 = hist5[i5]
    a5 = atr5[i5]

    plus = pdi[i5]
    minus = mdi[i5]
    adx_now = adx[i5]

    ema9 = e9[i1]
    ema21 = e21[i1]

    r1 = rsi1[i1]
    h1 = hist1[i1]

    prev_ema20 = e20[i5 - 3]
    prev_ema50 = e50[i5 - 3]
    prev_rsi5 = rsi5[i5 - 1]

    market_structure = structure(
        candles5
    )

    support, resistance = levels(
        candles5
    )

    if (
        support is None
        or resistance is None
    ):
        return {
            "symbol": symbol,
            "signal": "NO TRADE",
            "score": 0,
            "status": "DATA",
            "reason": "Levels unavailable",
            "diagnostics": [
                "Support/resistance unavailable"
            ]
        }

    # ========================================================
    # 5M TREND
    # ========================================================

    bull_trend = (
        price > ema20 > ema50
    )

    bear_trend = (
        price < ema20 < ema50
    )

    bull_ema_slope = (
        ema20 > prev_ema20
        and ema50 > prev_ema50
    )

    bear_ema_slope = (
        ema20 < prev_ema20
        and ema50 < prev_ema50
    )

    bull_structure = (
        market_structure == "BULL"
    )

    bear_structure = (
        market_structure == "BEAR"
    )

    bull_dmi = plus > minus
    bear_dmi = minus > plus

    strong_trend = (
        adx_now >= 20
    )

    very_strong_trend = (
        adx_now >= 25
    )

    bull_momentum = h5 > 0
    bear_momentum = h5 < 0

    bull_rsi = (
        53 <= r5 < 70
        and r5 >= prev_rsi5
    )

    bear_rsi = (
        30 < r5 <= 47
        and r5 <= prev_rsi5
    )

    # ========================================================
    # 1M ENTRY
    # ========================================================

    bull_entry_trend = (
        price1 > ema9 > ema21
    )

    bear_entry_trend = (
        price1 < ema9 < ema21
    )

    bull_entry_momentum = (
        h1 > 0
    )

    bear_entry_momentum = (
        h1 < 0
    )

    bull_entry_rsi = (
        50 <= r1 < 75
    )

    bear_entry_rsi = (
        25 < r1 <= 50
    )

    bull_candle = (
        bullish_confirmation(
            candles1
        )
    )

    bear_candle = (
        bearish_confirmation(
            candles1
        )
    )

    # ========================================================
    # PULLBACK
    # ========================================================

    recent = candles1[-8:]

    bull_pullback = any(
        c["low"]
        <= ema9 * 1.0025
        for c in recent[:-1]
    )

    bear_pullback = any(
        c["high"]
        >= ema9 * 0.9975
        for c in recent[:-1]
    )

    # ========================================================
    # ANTI-CHASE / ROOM
    # ========================================================

    extension = (
        abs(price - ema20)
        / a5
        if a5 > 0
        else 999
    )

    not_overextended = (
        extension <= 2.0
    )

    room_up = max(
        resistance - price,
        0
    )

    room_down = max(
        price - support,
        0
    )

    bull_room = (
        room_up >= a5 * 0.35
    )

    bear_room = (
        room_down >= a5 * 0.35
    )

    healthy_range = (
        resistance - support
    ) >= a5 * 1.25

    ema_gap = (
        abs(ema20 - ema50)
        / a5
        if a5 > 0
        else 0
    )

    not_flat = (
        ema_gap >= 0.10
    )

    severe_chop = detect_chop(
        candles5,
        ema20,
        ema50,
        a5
    )

    vol_state = volatility_state(
        candles5,
        atr5,
        i5
    )

    # ========================================================
    # SUPPORT / RESISTANCE ZONE FILTER
    # ========================================================

    resistance_distance = (
        proximity_to_level(
            price,
            resistance,
            a5
        )
    )

    support_distance = (
        proximity_to_level(
            price,
            support,
            a5
        )
    )

    # Don't enter directly into a major opposing level.
    major_resistance_block = (
        resistance_distance <= 0.20
    )

    major_support_block = (
        support_distance <= 0.20
    )

    # ========================================================
    # SCORE
    #
    # Maximum = 100
    #
    # Trend        25
    # Structure    15
    # DMI/ADX      15
    # Momentum     10
    # RSI          10
    # Entry        10
    # Timing        5
    # ========================================================

    bull_score = 0
    bear_score = 0

    bull_reasons = []
    bear_reasons = []

    # --------------------------------------------------------
    # 25: TREND
    # --------------------------------------------------------

    if bull_trend:
        bull_score += 15
        bull_reasons.append(
            "5M trend bullish"
        )

    if bull_ema_slope:
        bull_score += 10
        bull_reasons.append(
            "5M EMAs rising"
        )

    if bear_trend:
        bear_score += 15
        bear_reasons.append(
            "5M trend bearish"
        )

    if bear_ema_slope:
        bear_score += 10
        bear_reasons.append(
            "5M EMAs falling"
        )

    # --------------------------------------------------------
    # 15: STRUCTURE
    # --------------------------------------------------------

    if bull_structure:
        bull_score += 15
        bull_reasons.append(
            "Bullish structure"
        )

    elif market_structure == "NEUTRAL":
        # Neutral structure is not automatically fatal.
        # It simply receives no structure points.
        pass

    if bear_structure:
        bear_score += 15
        bear_reasons.append(
            "Bearish structure"
        )

    # --------------------------------------------------------
    # 15: DMI / ADX
    # --------------------------------------------------------

    if bull_dmi:
        bull_score += 8
        bull_reasons.append(
            "+DI > -DI"
        )

    if bear_dmi:
        bear_score += 8
        bear_reasons.append(
            "-DI > +DI"
        )

    if strong_trend:

        if bull_dmi:
            bull_score += 7

        if bear_dmi:
            bear_score += 7

    # --------------------------------------------------------
    # 10: 5M MOMENTUM
    # --------------------------------------------------------

    if bull_momentum:
        bull_score += 10
        bull_reasons.append(
            "5M MACD positive"
        )

    if bear_momentum:
        bear_score += 10
        bear_reasons.append(
            "5M MACD negative"
        )

    # --------------------------------------------------------
    # 10: 5M RSI
    # --------------------------------------------------------

    # Full points for ideal RSI.
    #
    # Partial points are awarded for directional RSI
    # that is not yet in the perfect range.
    #
    # This is one of the important changes from V3.1.

    if bull_rsi:
        bull_score += 10
        bull_reasons.append(
            "5M RSI bullish"
        )

    elif (
        r5 >= 50
        and r5 < 73
        and r5 >= prev_rsi5
    ):
        bull_score += 6
        bull_reasons.append(
            "5M RSI supportive"
        )

    if bear_rsi:
        bear_score += 10
        bear_reasons.append(
            "5M RSI bearish"
        )

    elif (
        r5 > 27
        and r5 <= 50
        and r5 <= prev_rsi5
    ):
        bear_score += 6
        bear_reasons.append(
            "5M RSI supportive"
        )

    # --------------------------------------------------------
    # 10: 1M ENTRY
    # --------------------------------------------------------

    if bull_entry_trend:
        bull_score += 6
        bull_reasons.append(
            "1M EMA alignment"
        )

    elif (
        price1 > ema21
        and ema9 > ema21
    ):
        bull_score += 4
        bull_reasons.append(
            "1M trend supportive"
        )

    if bear_entry_trend:
        bear_score += 6
        bear_reasons.append(
            "1M EMA alignment"
        )

    elif (
        price1 < ema21
        and ema9 < ema21
    ):
        bear_score += 4
        bear_reasons.append(
            "1M trend supportive"
        )

    if bull_entry_momentum:
        bull_score += 4

    if bear_entry_momentum:
        bear_score += 4

    # --------------------------------------------------------
    # 10: ENTRY CONFIRMATION
    # --------------------------------------------------------

    if bull_candle:
        bull_score += 6
        bull_reasons.append(
            "Bullish candle confirmation"
        )

    elif (
        direction(candles1[-1])
        == "BULL"
        and body_ratio(candles1[-1])
        >= 0.40
    ):
        bull_score += 3
        bull_reasons.append(
            "Bullish candle"
        )

    if bear_candle:
        bear_score += 6
        bear_reasons.append(
            "Bearish candle confirmation"
        )

    elif (
        direction(candles1[-1])
        == "BEAR"
        and body_ratio(candles1[-1])
        >= 0.40
    ):
        bear_score += 3
        bear_reasons.append(
            "Bearish candle"
        )

    if bull_entry_rsi:
        bull_score += 4

    elif (
        r1 >= 47
        and r1 < 78
    ):
        bull_score += 2

    if bear_entry_rsi:
        bear_score += 4

    elif (
        r1 > 22
        and r1 <= 53
    ):
        bear_score += 2

    # --------------------------------------------------------
    # 5: TIMING
    # --------------------------------------------------------

    if (
        bull_pullback
        and bull_room
        and not_overextended
    ):
        bull_score += 5
        bull_reasons.append(
            "Pullback + room + no chase"
        )

    else:

        if bull_pullback:
            bull_score += 2
            bull_reasons.append(
                "Pullback detected"
            )

        if (
            bull_room
            and not_overextended
        ):
            bull_score += 2
            bull_reasons.append(
                "Room + no chase"
            )

    if (
        bear_pullback
        and bear_room
        and not_overextended
    ):
        bear_score += 5
        bear_reasons.append(
            "Pullback + room + no chase"
        )

    else:

        if bear_pullback:
            bear_score += 2
            bear_reasons.append(
                "Pullback detected"
            )

        if (
            bear_room
            and not_overextended
        ):
            bear_score += 2
            bear_reasons.append(
                "Room + no chase"
            )

    bull_score = min(
        100,
        bull_score
    )

    bear_score = min(
        100,
        bear_score
    )

    # ========================================================
    # CRITICAL SAFETY FILTERS
    #
    # These replace the old V3.1 "all conditions must be true"
    # filter.
    #
    # Soft confirmations can be imperfect.
    # Structural danger remains a hard rejection.
    # ========================================================

    bull_critical_failures = []
    bear_critical_failures = []

    # ---------------- BULLISH BLOCKERS ----------------

    if not bull_trend:
        bull_critical_failures.append(
            "5M trend not bullish"
        )

    if severe_chop:
        bull_critical_failures.append(
            "Severe 5M chop"
        )

    if not healthy_range:
        bull_critical_failures.append(
            "Range too compressed"
        )

    if not not_flat:
        bull_critical_failures.append(
            "5M EMA spread too flat"
        )

    if not bull_room:
        bull_critical_failures.append(
            "Insufficient room to resistance"
        )

    if major_resistance_block:
        bull_critical_failures.append(
            "Too close to resistance"
        )

    if extension > 2.0:
        bull_critical_failures.append(
            "Price too extended"
        )

    # ---------------- BEARISH BLOCKERS ----------------

    if not bear_trend:
        bear_critical_failures.append(
            "5M trend not bearish"
        )

    if severe_chop:
        bear_critical_failures.append(
            "Severe 5M chop"
        )

    if not healthy_range:
        bear_critical_failures.append(
            "Range too compressed"
        )

    if not not_flat:
        bear_critical_failures.append(
            "5M EMA spread too flat"
        )

    if not bear_room:
        bear_critical_failures.append(
            "Insufficient room to support"
        )

    if major_support_block:
        bear_critical_failures.append(
            "Too close to support"
        )

    if extension > 2.0:
        bear_critical_failures.append(
            "Price too extended"
        )

    # ========================================================
    # SOFT CONFLICT CHECKS
    # ========================================================

    # We don't require perfect structure anymore, but a strong
    # opposite structure is still treated as a contradiction.

    bull_structure_conflict = (
        market_structure == "BEAR"
    )

    bear_structure_conflict = (
        market_structure == "BULL"
    )

    if bull_structure_conflict:
        bull_critical_failures.append(
            "Structure strongly bearish"
        )

    if bear_structure_conflict:
        bear_critical_failures.append(
            "Structure strongly bullish"
        )

    # Very low ADX means there is little directional pressure.
    # It is a blocker only when extremely weak.
    if adx_now < 15:

        bull_critical_failures.append(
            "ADX too weak"
        )

        bear_critical_failures.append(
            "ADX too weak"
        )

    # ========================================================
    # FINAL DECISION
    # ========================================================

    bull_critical_ok = (
        len(bull_critical_failures) == 0
    )

    bear_critical_ok = (
        len(bear_critical_failures) == 0
    )

    signal = "NO TRADE"

    score = max(
        bull_score,
        bear_score
    )

    reasons = []
    diagnostics = []

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if (
        bull_critical_ok
        and bull_score >= MIN_SCORE
        and bull_score > bear_score
    ):
        signal = "CALL"
        score = bull_score
        reasons = bull_reasons

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    elif (
        bear_critical_ok
        and bear_score >= MIN_SCORE
        and bear_score > bull_score
    ):
        signal = "PUT"
        score = bear_score
        reasons = bear_reasons

    # --------------------------------------------------------
    # BORDERLINE
    #
    # These are NOT sent as trade signals.
    # They are visible for diagnostics only.
    # --------------------------------------------------------

    elif (
        bull_critical_ok
        and bull_score >= BORDERLINE_SCORE
        and bull_score > bear_score
    ):
        diagnostics.append(
            "CALL borderline: "
            + str(bull_score)
            + "/100"
        )

        diagnostics.extend(
            bull_reasons[:4]
        )

    elif (
        bear_critical_ok
        and bear_score >= BORDERLINE_SCORE
        and bear_score > bull_score
    ):
        diagnostics.append(
            "PUT borderline: "
            + str(bear_score)
            + "/100"
        )

        diagnostics.extend(
            bear_reasons[:4]
        )

    # --------------------------------------------------------
    # NO TRADE DIAGNOSTICS
    # --------------------------------------------------------

    if signal == "NO TRADE":

        if bull_score >= bear_score:

            if bull_critical_failures:
                diagnostics.extend(
                    [
                        "CALL blocked: "
                        + x
                        for x in
                        bull_critical_failures[:4]
                    ]
                )

            if bull_score < BORDERLINE_SCORE:
                diagnostics.append(
                    "CALL score only "
                    + str(bull_score)
                    + "/100"
                )

        else:

            if bear_critical_failures:
                diagnostics.extend(
                    [
                        "PUT blocked: "
                        + x
                        for x in
                        bear_critical_failures[:4]
                    ]
                )

            if bear_score < BORDERLINE_SCORE:
                diagnostics.append(
                    "PUT score only "
                    + str(bear_score)
                    + "/100"
                )

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if signal != "NO TRADE":

        status = "QUALIFIED"
        final_reason = "Qualified setup"

    elif score >= BORDERLINE_SCORE:

        status = "BORDERLINE"
        final_reason = (
            "Setup close to qualification"
        )

    else:

        status = "NO TRADE"
        final_reason = (
            "Critical filters or score not sufficient"
        )

    return {
        "symbol": symbol,
        "signal": signal,
        "score": int(score),
        "status": status,

        "price": price,

        "rsi5": r5,
        "rsi1": r1,

        "adx": adx_now,

        "structure": market_structure,

        "extension_atr": extension,

        "volatility": vol_state,

        "ema_gap_atr": ema_gap,

        "room_up_atr": (
            room_up / a5
            if a5 > 0
            else 0
        ),

        "room_down_atr": (
            room_down / a5
            if a5 > 0
            else 0
        ),

        "bull_score": int(
            bull_score
        ),

        "bear_score": int(
            bear_score
        ),

        "reasons": reasons,

        "diagnostics": diagnostics,

        "critical_failures": (
            bull_critical_failures
            if bull_score >= bear_score
            else bear_critical_failures
        ),

        "reason": final_reason
    }


# ============================================================
# REPORT + TRACKING
# ============================================================

def make_signal_id(
    symbol,
    signal,
    stamp
):
    return (
        symbol
        + "-"
        + signal
        + "-"
        + stamp.strftime("%H%M%S")
    )


def add_new_signal(
    data,
    result,
    stamp
):
    if result["signal"] == "NO TRADE":
        return None

    signal_id = make_signal_id(
        result["symbol"],
        result["signal"],
        stamp
    )

    for item in data["signals"]:

        if item.get("id") == signal_id:
            return None

    record = {
        "id": signal_id,

        "symbol": result["symbol"],
        "asset": result["symbol"],

        "signal": result["signal"],

        "score": result["score"],

        "time": stamp.isoformat(),
        "candle_time": stamp.isoformat(),

        "price": result["price"],

        "rsi5": result["rsi5"],
        "rsi1": result["rsi1"],

        "adx": result["adx"],

        "structure": result["structure"],

        "extension_atr": result[
            "extension_atr"
        ],

        "volatility": result[
            "volatility"
        ],

        "result": "PENDING",

        "result_time": None
    }

    data["signals"].append(
        record
    )

    data["signals"] = (
        data["signals"]
        [-MAX_TRACKER_ITEMS:]
    )

    return signal_id


def build_report(
    results,
    errors,
    data
):
    stamp = now_utc()

    qualified = [
        x for x in results
        if x["signal"] != "NO TRADE"
    ]

    borderline = [
        x for x in results
        if (
            x["signal"] == "NO TRADE"
            and x["status"] == "BORDERLINE"
        )
    ]

    qualified.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    borderline.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    lines = [
        "🧠 <b>PRECISION SCANNER V3.2</b>",
        "",
        "Scan: "
        + stamp.strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),

        "Data: Coinbase spot proxy",

        "Trend: <b>5M</b>",

        "Entry: <b>1M</b>",

        "Reference expiry: "
        "<b>10 MINUTES</b>",

        "Minimum setup score: "
        "<b>80/100</b>",

        ""
    ]

    # ========================================================
    # QUALIFIED
    # ========================================================

    if qualified:

        lines.append(
            "🔥 <b>QUALIFIED SIGNALS</b>"
        )

        for result in qualified:

            signal_id = add_new_signal(
                data,
                result,
                stamp
            )

            if not signal_id:
                continue

            side = (
                "🟢 CALL"
                if result["signal"] == "CALL"
                else "🔴 PUT"
            )

            lines += [
                "",
                "<b>"
                + html.escape(
                    result["symbol"]
                )
                + " OTC proxy</b>",

                "Signal: <b>"
                + side
                + "</b>",

                "Setup quality: <b>"
                + str(result["score"])
                + "/100</b>",

                "5M RSI: %.1f"
                % result["rsi5"],

                "1M RSI: %.1f"
                % result["rsi1"],

                "ADX: %.1f"
                % result["adx"],

                "Structure: <b>"
                + html.escape(
                    result["structure"]
                )
                + "</b>",

                "Price: %.8f"
                % result["price"],

                "Extension: %.2f ATR"
                % result["extension_atr"],

                "Signal ID: <code>"
                + html.escape(
                    signal_id
                )
                + "</code>",

                "Reference expiry: "
                "<b>10 MINUTES</b>"
            ]

            if result["reasons"]:

                lines.append(
                    "Why: "
                    + html.escape(
                        ", ".join(
                            result[
                                "reasons"
                            ][:6]
                        )
                    )
                )

    else:

        lines += [
            "⚪ <b>NO QUALIFIED SIGNAL</b>",
            "No setup reached 80/100 with all critical"
            " safety conditions satisfied."
        ]

    # ========================================================
    # BORDERLINE
    # ========================================================

    if borderline:

        lines += [
            "",
            "🟡 <b>BORDERLINE / WATCH</b>"
        ]

        for result in borderline[:5]:

            direction_text = (
                "CALL"
                if result["bull_score"]
                >= result["bear_score"]
                else "PUT"
            )

            lines.append(
                "• "
                + html.escape(
                    result["symbol"]
                )
                + " "
                + direction_text
                + " "
                + str(result["score"])
                + "/100"
            )

            if result["diagnostics"]:

                lines.append(
                    "  "
                    + html.escape(
                        result[
                            "diagnostics"
                        ][0]
                    )
                )

    # ========================================================
    # DIAGNOSTICS
    # ========================================================

    diagnostic_candidates = [
        x for x in results
        if x["signal"] == "NO TRADE"
        and x["status"] != "BORDERLINE"
        and x.get("diagnostics")
    ]

    if diagnostic_candidates:

        lines += [
            "",
            "🔎 <b>TOP REJECTION REASONS</b>"
        ]

        shown = 0

        for result in sorted(
            diagnostic_candidates,
            key=lambda x: x["score"],
            reverse=True
        ):

            if shown >= 6:
                break

            reason = result[
                "diagnostics"
            ][0]

            lines.append(
                "• "
                + html.escape(
                    result["symbol"]
                )
                + ": "
                + html.escape(reason)
                + " ("
                + str(result["score"])
                + "/100)"
            )

            shown += 1

    # ========================================================
    # ERRORS
    # ========================================================

    if errors:

        lines += [
            "",
            "⚠️ <b>MARKET/DATA WARNINGS</b>"
        ]

        for error in errors[:8]:

            lines.append(
                "• "
                + html.escape(error)
            )

    # ========================================================
    # FOOTER
    # ========================================================

    lines += [
        "",
        "Commands: /win ID | /loss ID | /stats",
        "",
        "⚠️ DEMO/TESTING ONLY.",
        "Score = setup quality, not win probability.",
        "Coinbase proxy may differ from Pocket Option OTC pricing."
    ]

    return "\n".join(lines)


def completed_signals(data):
    return [
        x
        for x in data["signals"]
        if x.get("result")
        in ("WIN", "LOSS")
    ]


def winrate(items):

    if not items:
        return 0.0

    wins = sum(
        x.get("result") == "WIN"
        for x in items
    )

    return (
        wins
        * 100.0
        / len(items)
    )


def stats_text(data):

    done = completed_signals(
        data
    )

    pending = sum(
        x.get("result") == "PENDING"
        for x in data["signals"]
    )

    wins = sum(
        x.get("result") == "WIN"
        for x in done
    )

    losses = sum(
        x.get("result") == "LOSS"
        for x in done
    )

    lines = [
        "📊 <b>PRECISION SCANNER V3.2</b>",
        "",
        "Completed: "
        + str(len(done)),

        "Wins: "
        + str(wins),

        "Losses: "
        + str(losses),

        "Win rate: %.1f%%"
        % winrate(done),

        "Pending: "
        + str(pending),

        ""
    ]

    for side in (
        "CALL",
        "PUT"
    ):

        group = [
            x
            for x in done
            if x.get("signal") == side
        ]

        lines.append(
            side
            + ": "
            + str(
                sum(
                    x.get("result")
                    == "WIN"
                    for x in group
                )
            )
            + "W / "
            + str(
                sum(
                    x.get("result")
                    == "LOSS"
                    for x in group
                )
            )
            + "L = %.1f%%"
            % winrate(group)
        )

    lines += [
        "",
        "⚠️ Score is setup quality, not win probability.",
        "Use forward testing before scaling."
    ]

    return "\n".join(lines)


# ============================================================
# GITHUB TRACKER COMMIT
# ============================================================

def commit_tracker():

    try:

        subprocess.run(
            [
                "git",
                "config",
                "user.name",
                "github-actions[bot]"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]"
                "@users.noreply.github.com"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "add",
                TRACKER_FILE
            ],
            check=True
        )

        check = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--quiet"
            ]
        )

        if check.returncode == 0:

            print(
                "No tracker changes to commit."
            )

            return

        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "Update scanner tracker"
            ],
            check=True
        )

        subprocess.run(
            [
                "git",
                "push"
            ],
            check=True
        )

        print(
            "Tracker pushed."
        )

    except Exception as exc:

        print(
            "Tracker git update failed:",
            exc
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "Starting Precision Signal Scanner V3.2"
    )

    data = load_tracker()

    print(
        "Processing Telegram commands..."
    )

    process_commands(
        data
    )

    print(
        "Loading Coinbase markets..."
    )

    markets = get_markets()

    results = []
    errors = []

    for symbol in ASSET_BASES:

        product = markets.get(
            symbol
        )

        if not product:

            errors.append(
                symbol
                + ": Coinbase market unavailable"
            )

            continue

        print(
            "Analyzing "
            + symbol
            + " ("
            + product
            + ")"
        )

        try:

            result = analyze_asset(
                symbol,
                product
            )

            results.append(
                result
            )

            print(
                symbol
                + ": "
                + result["signal"]
                + " "
                + str(result["score"])
                + "/100"
            )

            if result.get("diagnostics"):

                print(
                    symbol
                    + " diagnostics: "
                    + " | ".join(
                        result[
                            "diagnostics"
                        ][:4]
                    )
                )

        except Exception as exc:

            errors.append(
                symbol
                + ": "
                + str(exc)
            )

            print(
                symbol
                + ": ERROR "
                + str(exc)
            )

    if not results:

        raise RuntimeError(
            "All Coinbase markets failed."
        )

    message = build_report(
        results,
        errors,
        data
    )

    save_tracker(
        data
    )

    print(
        "Sending Telegram report..."
    )

    send_message(
        message
    )

    print(
        "Updating GitHub tracker..."
    )

    commit_tracker()

    print(
        "Scanner complete."
    )


if __name__ == "__main__":
    main()
