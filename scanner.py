import os
import json
import html
import time
import subprocess
from datetime import datetime, timezone

import requests

# ============================================================
# PRECISION SIGNAL SCANNER V3.1
# 5M trend + 1M entry confirmation
# Reference expiry: 10 minutes
#
# IMPORTANT:
# - Coinbase spot data is used only as a market-data proxy.
# - This script does NOT connect to or execute Pocket Option trades.
# - Score = setup quality, NOT probability of winning.
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

    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/" + method
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
            tg("sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[start:start + 3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true"
            })
        return True
    except Exception as exc:
        print("Telegram send error:", exc)
        return False


def api_get(url, params=None):
    response = requests.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "precision-signal-scanner/3.1"}
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# TRACKER
# Keeps compatibility with the existing:
# {"signals": [...], "offset": ...}
# ============================================================

def load_tracker():
    if not os.path.exists(TRACKER_FILE):
        return {"signals": [], "offset": 0}

    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        # Existing scanner format.
        if isinstance(data, dict):
            data.setdefault("signals", [])
            data.setdefault("offset", 0)

            if not isinstance(data["signals"], list):
                data["signals"] = []

            return data

        # Also accept a plain list if an older V3 file created one.
        if isinstance(data, list):
            return {"signals": data, "offset": 0}

    except Exception as exc:
        print("Tracker read error:", exc)

    return {"signals": [], "offset": 0}


def save_tracker(data):
    signals = data.get("signals", [])
    data["signals"] = signals[-MAX_TRACKER_ITEMS:]

    with open(TRACKER_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def help_text():
    return (
        "🤖 <b>PRECISION SCANNER V3.1</b>\n\n"
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
        updates = tg("getUpdates", {
            "offset": int(data.get("offset", 0)) + 1,
            "limit": 100,
            "timeout": 1
        }) or []
    except Exception as exc:
        print("Telegram command error:", exc)
        return False

    for update in updates:
        data["offset"] = update.get("update_id", data.get("offset", 0))

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != TELEGRAM_CHAT_ID:
            continue

        text = str(message.get("text", "")).strip()
        parts = text.split()

        if not parts:
            continue

        command = parts[0].split("@")[0].lower()

        if command == "/start":
            send_message(help_text())
            continue        if command == "/stats":
            send_message(stats_text(data))
            continue

        if command in ("/win", "/loss"):
            if len(parts) != 2:
                send_message(
                    "⚠️ Use <code>" +
                    command +
                    " SIGNAL-ID</code>"
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
                    "❌ Signal not found: <code>" +
                    html.escape(wanted) +
                    "</code>"
                )
                continue

            if found.get("result", "PENDING") != "PENDING":
                send_message(
                    "⚠️ <code>" +
                    html.escape(wanted) +
                    "</code> is already <b>" +
                    html.escape(str(found.get("result"))) +
                    "</b>."
                )
                continue

            result = "WIN" if command == "/win" else "LOSS"
            found["result"] = result
            found["result_time"] = iso_now()
            changed = True

            send_message(
                "✅ <b>RESULT RECORDED</b>\n"
                "Signal: <code>" + html.escape(wanted) + "</code>\n"
                "Result: <b>" + result + "</b>"
            )
            continue

        if command.startswith("/"):
            send_message("❓ Unknown command.\n\n" + help_text())

    if changed:
        save_tracker(data)

    return changed


# ============================================================
# MARKET DATA
# ============================================================

def get_markets():
    raw = api_get(COINBASE_BASE + "/products")
    found = {}

    for item in raw:
        if item.get("status") != "online":
            continue

        base = item.get("base_currency")
        quote = item.get("quote_currency")

        if base in ASSET_BASES and quote in ("USD", "USDC"):
            if base not in found:
                found[base] = item.get("id")

    return found


def get_candles(product_id, granularity, limit=220):
    raw = api_get(
        COINBASE_BASE + "/products/" + product_id + "/candles",
        {"granularity": granularity}
    )

    candles = []

    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            continue

        candles.append({
            "time": int(row[0]),
            "low": float(row[1]),
            "high": float(row[2]),
            "open": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        })

    candles.sort(key=lambda x: x["time"])

    # Use completed candles only.
    current = int(time.time())
    candles = [
        c for c in candles
        if c["time"] + granularity <= current
    ]

    return candles[-limit:]


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)
    multiplier = 2.0 / (period + 1.0)

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    previous = seed

    for i in range(period, len(values)):        previous = (
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
        change = values[i] - values[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def calc(gain, loss):
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    result[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period
        result[i] = calc(avg_gain, avg_loss)

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
            abs(candle["low"] - previous_close)
        )

    return tr


def atr(candles, period=14):
    tr = true_ranges(candles)
    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    value = sum(tr[1:period + 1]) / period
    result[period] = value

    for i in range(period + 1, len(candles)):
        value = (
            value * (period - 1) + tr[i]
        ) / period
        result[i] = value

    return result


def adx_dmi(candles, period=14):
    n = len(candles)

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = true_ranges(candles)

    for i in range(1, n):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]

        if up > down and up > 0:
            plus_dm[i] = up

        if down > up and down > 0:
            minus_dm[i] = down

    plus_di = [None] * n
    minus_di = [None] * n
    adx = [None] * n

    if n <= period * 2:
        return plus_di, minus_di, adx

    tr_sum = sum(tr[1:period + 1])
    plus_sum = sum(plus_dm[1:period + 1])
    minus_sum = sum(minus_dm[1:period + 1])

    dx_values = []

    for i in range(period, n):
        if i > period:
            tr_sum = tr_sum - tr_sum / period + tr[i]
            plus_sum = (
                plus_sum - plus_sum / period + plus_dm[i]
            )
            minus_sum = (
                minus_sum - minus_sum / period + minus_dm[i]
            )

        if tr_sum == 0:
            pdi = 0.0
            mdi = 0.0
        else:
            pdi = 100.0 * plus_sum / tr_sum
            mdi = 100.0 * minus_sum / tr_sum        plus_di[i] = pdi
        minus_di[i] = mdi

        denominator = pdi + mdi
        dx = (
            0.0
            if denominator == 0
            else 100.0 * abs(pdi - mdi) / denominator
        )

        dx_values.append(dx)

        if len(dx_values) == period:
            adx[i] = sum(dx_values) / period
        elif len(dx_values) > period and adx[i - 1] is not None:
            adx[i] = (
                adx[i - 1] * (period - 1) + dx
            ) / period

    return plus_di, minus_di, adx


def macd(values, fast=12, slow=26, signal=9):
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    line = [None] * len(values)

    for i in range(len(values)):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            line[i] = fast_ema[i] - slow_ema[i]

    valid = [x for x in line if x is not None]
    signal_valid = ema(valid, signal)

    signal_line = [None] * len(values)
    start = len(values) - len(valid)

    for j, value in enumerate(signal_valid):
        if value is not None:
            signal_line[start + j] = value

    histogram = [None] * len(values)

    for i in range(len(values)):
        if line[i] is not None and signal_line[i] is not None:
            histogram[i] = line[i] - signal_line[i]

    return line, signal_line, histogram


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
    full = candle["high"] - candle["low"]
    if full <= 0:
        return 0.0
    return abs(candle["close"] - candle["open"]) / full


def bullish_confirmation(candles):
    if len(candles) < 3:
        return False

    current = candles[-1]
    previous = candles[-2]

    strong = (
        direction(current) == "BULL"
        and body_ratio(current) >= 0.55
        and current["close"] > previous["close"]
    )

    engulfing = (
        direction(previous) == "BEAR"
        and direction(current) == "BULL"
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )

    reclaim = (
        current["close"] > previous["high"]
        and previous["low"] <= candles[-3]["low"]
    )

    return strong or engulfing or reclaim


def bearish_confirmation(candles):
    if len(candles) < 3:
        return False

    current = candles[-1]
    previous = candles[-2]

    strong = (
        direction(current) == "BEAR"
        and body_ratio(current) >= 0.55
        and current["close"] < previous["close"]
    )

    engulfing = (
        direction(previous) == "BULL"
        and direction(current) == "BEAR"
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )

    reclaim = (
        current["close"] < previous["low"]
        and previous["high"] >= candles[-3]["high"]
    )

    return strong or engulfing or reclaim


def structure(candles    recent = candles[-lookback:]
    half = lookback // 2

    first = recent[:half]
    second = recent[half:]

    first_high = max(x["high"] for x in first)
    second_high = max(x["high"] for x in second)
    first_low = min(x["low"] for x in first)
    second_low = min(x["low"] for x in second)

    if second_high > first_high and second_low > first_low:
        return "BULL"

    if second_high < first_high and second_low < first_low:
        return "BEAR"

    return "NEUTRAL"


def levels(candles, lookback=30):
    if len(candles) < lookback + 1:
        return None, None

    window = candles[-(lookback + 1):-1]
    support = min(x["low"] for x in window)
    resistance = max(x["high"] for x in window)

    return support, resistance


# ============================================================
# SCORING
# Maximum = 100 points
# ============================================================

def analyze_asset(symbol, product_id):
    candles5 = get_candles(product_id, MAIN_SECONDS)
    candles1 = get_candles(product_id, ENTRY_SECONDS)

    if len(candles5) < 100 or len(candles1) < 100:
        return {
            "signal": "NO TRADE",
            "score": 0,
            "reason": "Insufficient data"
        }

    close5 = [x["close"] for x in candles5]
    close1 = [x["close"] for x in candles1]

    e20 = ema(close5, 20)
    e50 = ema(close5, 50)
    rsi5 = rsi(close5)
    _, _, hist5 = macd(close5)
    atr5 = atr(candles5)
    pdi, mdi, adx = adx_dmi(candles5)

    e9 = ema(close1, 9)
    e21 = ema(close1, 21)
    rsi1 = rsi(close1)
    _, _, hist1 = macd(close1)

    i5 = len(candles5) - 1
    i1 = len(candles1) - 1

    required = [
        e20[i5], e50[i5], rsi5[i5], hist5[i5],
        atr5[i5], pdi[i5], mdi[i5], adx[i5],
        e9[i1], e21[i1], rsi1[i1], hist1[i1]
    ]

    if any(x is None for x in required):
        return {
            "signal": "NO TRADE",
            "score": 0,
            "reason": "Indicators not ready"
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

    market_structure = structure(candles5)
    support, resistance = levels(candles5)

    if support is None or resistance is None:
        return {
            "signal": "NO TRADE",
            "score": 0,
            "reason": "Levels unavailable"
        }

    # ---------------- TREND ----------------
    bull_trend = price > ema20 > ema50
    bear_trend = price < ema20 < ema50

    bull_ema_slope = (
        ema20 > prev_ema20 and ema50 > prev_ema50
    )
    bear_ema_slope = (
        ema20 < prev_ema20 and ema50 < prev_ema50
    )

    bull_structure = market_structure == "BULL"
    bear_structure = market_structure == "BEAR"

    bull_dmi = plus > minus
    bear_dmi = minus > plus
    strong_trend = adx_now >= 20

    bull_momentum = h5 > 0
    bear_momentum = h5 < 0    bull_rsi = 53 <= r5 < 70 and r5 >= prev_rsi5
    bear_rsi = 30 < r5 <= 47 and r5 <= prev_rsi5

    # ---------------- 1M ENTRY ----------------
    bull_entry_trend = price1 > ema9 > ema21
    bear_entry_trend = price1 < ema9 < ema21

    bull_entry_momentum = h1 > 0
    bear_entry_momentum = h1 < 0

    bull_entry_rsi = 50 <= r1 < 75
    bear_entry_rsi = 25 < r1 <= 50

    bull_candle = bullish_confirmation(candles1)
    bear_candle = bearish_confirmation(candles1)

    recent = candles1[-6:]

    bull_pullback = any(
        c["low"] <= ema9 * 1.0015
        for c in recent[:-1]
    )

    bear_pullback = any(
        c["high"] >= ema9 * 0.9985
        for c in recent[:-1]
    )

    # ---------------- ANTI-CHASE ----------------
    extension = abs(price - ema20) / a5 if a5 > 0 else 999
    not_overextended = extension <= 1.8

    room_up = max(resistance - price, 0)
    room_down = max(price - support, 0)

    bull_room = room_up >= a5 * 0.35
    bear_room = room_down >= a5 * 0.35

    healthy_range = (resistance - support) >= a5 * 1.5
    if bull_pullback and bull_room and not_overextended:
        bull_score += 5
        bull_reasons.append("Pullback + room + no chase")

    if bear_pullback and bear_room and not_overextended:
        bear_score += 5
        bear_reasons.append("Pullback + room + no chase")

    # ---------------- FINAL FILTERS ----------------
    bull_ready = all([
        bull_trend,
        bull_ema_slope,
        bull_structure,
        bull_dmi,
        strong_trend,
        bull_momentum,
        bull_rsi,
        bull_entry_trend,
        bull_entry_momentum,
        bull_entry_rsi,
        bull_candle,
        bull_pullback,
        bull_room,
        healthy_range,
        not_flat,
        not_overextended
    ])

    bear_ready = all([
        bear_trend,
        bear_ema_slope,
        bear_structure,
        bear_dmi,
        strong_trend,
        bear_momentum,
        bear_rsi,
        bear_entry_trend,
        bear_entry_momentum,
        bear_entry_rsi,
        bear_candle,
        bear_pullback,
        bear_room,
        healthy_range,
        not_flat,
        not_overextended
    ])

    signal = "NO TRADE"
    score = max(bull_score, bear_score)
    reasons = []

    if (
        bull_ready
        and bull_score >= MIN_SCORE
        and bull_score > bear_score
    ):
        signal = "CALL"
        score = bull_score
        reasons = bull_reasons

    elif (
        bear_ready
        and bear_score >= MIN_SCORE
        and bear_score > bull_score
    ):
        signal = "PUT"
        score = bear_score
        reasons = bear_reasons

    if signal == "NO TRADE":
        if bull_score >= bear
    ema_gap = abs(ema20 - ema50) / a5 if adef build_report(results, errors, data):
    stamp = now_utc()

    qualified = [
        x for x in results
        if x["signal"] != "NO TRADE"
    ]

    qualified.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    lines = [
        "🧠 <b>PRECISION SCANNER V3.1</b>",
        "",
        "Scan: " + stamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Data: Coinbase spot proxy",
        "Trend: <b>5M</b>",
        "Entry: <b>1M</b>",
        "Reference expiry: <b>10 MINUTES</b>",
        "Minimum setup score: <b>80/100</b>",
        ""
    ]

    if not qualified:
        lines += [
            "⚪ <b>NO TRADE</b>",
            "No asset passed all precision filters."
        ]
    else:
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

            side = "🟢 CALL" if result["signal"] == "CALL" else "🔴 PUT"

            lines += [
                "",
                "<b>" + html.escape(result["symbol"]) + " OTC proxy</b>",
                "Signal: <b>" + side + "</b>",
                "Setup quality: <b>" +
                str(result["score"]) + "/100</b>",
                "5M RSI: %.1f" % result["rsi5"],
                "1M RSI: %.1f" % result["rsi1"],
                "ADX: %.1f" % result["adx"],
                "Price: %.8f" % result        data
    )

    save_tracker(data)

    print("Sending Telegram report...")
    send_message(message)

    print("Updating GitHub tracker...")
    commit_tracker()

    print("Scanner complete.")


if __name__ == "__main__":
    main()
