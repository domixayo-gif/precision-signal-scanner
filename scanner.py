"""
=============================================================
PRECISION SIGNAL SCANNER V3.7
=============================================================

PURPOSE
-------
One-shot scanner designed for GitHub Actions.

GitHub Actions should run this file every 5 minutes:

    python scanner.py --once

The scanner:
    1. Downloads market candles
    2. Analyzes the 5-minute trend
    3. Analyzes the 1-minute entry
    4. Checks structure
    5. Checks ADX/DMI
    6. Checks MACD
    7. Checks RSI
    8. Checks pullback
    9. Checks candle strength
   10. Checks available room
   11. Checks price extension
   12. Applies strict blockers
   13. Sends only qualified signals
   14. Records signals
   15. Saves tracker data
   16. Exits

IMPORTANT
---------
This is a TESTING/RESEARCH scanner.

A score is a setup-quality score, NOT a guaranteed probability
of winning.

The scanner does NOT execute trades.

OTC is intentionally disabled because this script does not have
a verified OTC candle feed.

=============================================================
"""

import os
import sys
import json
import time
import math
import base64
import argparse
from datetime import datetime, timezone

import requests


# =============================================================
# VERSION
# =============================================================

VERSION = "V3.7"


# =============================================================
# CONFIGURATION
# =============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()

TRACKER_FILE = "tracker.json"

# GitHub Actions runs the scanner every 5 minutes.
# scanner.py itself performs ONE scan and exits when --once is used.
SCAN_INTERVAL_SECONDS = 300

# Market
BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_CATEGORY = "linear"

# Timeframes
MAIN_TIMEFRAME = "5"
ENTRY_TIMEFRAME = "1"

# Reference expiry
REFERENCE_EXPIRY_MINUTES = 5

# Signal thresholds
MIN_SCORE = 85
BORDERLINE_SCORE = 80

MIN_DOMINANCE = 3

MIN_ADX = 18
STRONG_ADX = 22

MIN_ROOM_SCORE = 3
MIN_EXTENSION_SCORE = 3

MIN_CANDLE_STRENGTH = 0.50

# RSI zones
CALL_RSI_MIN = 43
CALL_RSI_MAX = 68

PUT_RSI_MIN = 32
PUT_RSI_MAX = 57

# Signal lock
SIGNAL_LOCK_SECONDS = 300

# Data
CANDLE_LIMIT = 220

# HTTP
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 3

# Telegram
TELEGRAM_MESSAGE_LIMIT = 3900

# Tracker
MAX_TRACKER_ITEMS = 1000

# Heartbeat
HEARTBEAT_EVERY_SCANS = 15

# OTC deliberately disabled.
OTC_ENABLED = False


# =============================================================
# SYMBOL CONFIGURATION
# =============================================================
#
# IMPORTANT:
# These symbols must actually exist on the configured Bybit
# market. If your data provider uses different symbols, change
# these mappings.
#
# The scanner does not substitute crypto data for OTC data.
#

NORMAL_SYMBOLS = {
    "EURUSD": "EURUSDUSDT",
    "GBPUSD": "GBPUSDUSDT",
    "USDJPY": "USDJPYUSDT",
}

OTC_SYMBOLS = {
    "EURUSD_OTC": None,
    "GBPUSD_OTC": None,
    "USDJPY_OTC": None,
}


# =============================================================
# HTTP SESSION
# =============================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": f"PrecisionSignalScanner/{VERSION}",
        "Accept": "application/json",
    }
)


# =============================================================
# GENERAL HELPERS
# =============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def unix_now():
    return int(time.time())


def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def fmt_price(value):
    if value is None:
        return "N/A"

    value = float(value)

    if abs(value) >= 100:
        return f"{value:.3f}"

    if abs(value) >= 1:
        return f"{value:.5f}"

    return f"{value:.8f}"


def fmt_number(value, digits=2):
    if value is None:
        return "N/A"

    return f"{float(value):.{digits}f}"


# =============================================================
# TRACKER
# =============================================================

def default_tracker():
    return {
        "version": VERSION,
        "signals": [],
        "offset": 0,
        "metadata": {
            "last_scan": None,
            "last_signal": None,
            "last_heartbeat": None,
            "scan_count": 0,
            "signal_locks": {},
            "alerted_keys": [],
            "delivery_failed_keys": [],
        },
    }


def load_tracker():
    if not os.path.exists(TRACKER_FILE):
        return default_tracker()

    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_tracker()

        base = default_tracker()

        for key, value in data.items():
            base[key] = value

        if not isinstance(base.get("signals"), list):
            base["signals"] = []

        if not isinstance(base.get("metadata"), dict):
            base["metadata"] = default_tracker()["metadata"]

        for key, value in default_tracker()["metadata"].items():
            base["metadata"].setdefault(key, value)

        return base

    except Exception as e:
        print(f"[TRACKER] Failed to load tracker: {e}")
        return default_tracker()


def save_tracker_local(tracker):
    try:
        temp_file = TRACKER_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(tracker, f, indent=2, ensure_ascii=False)

        os.replace(temp_file, TRACKER_FILE)

        return True

    except Exception as e:
        print(f"[TRACKER] Local save failed: {e}")
        return False


def trim_tracker(tracker):
    signals = tracker.get("signals", [])

    if len(signals) > MAX_TRACKER_ITEMS:
        tracker["signals"] = signals[-MAX_TRACKER_ITEMS:]

    metadata = tracker.setdefault("metadata", {})

    for key in ("alerted_keys", "delivery_failed_keys"):
        values = metadata.get(key, [])

        if len(values) > MAX_TRACKER_ITEMS:
            metadata[key] = values[-MAX_TRACKER_ITEMS:]


def save_tracker_to_github(tracker):
    """
    Persist tracker.json into the repository using the GitHub Contents API.

    This is useful because GitHub Actions runners are temporary.
    """

    save_tracker_local(tracker)

    if not GITHUB_TOKEN:
        print("[GITHUB] GITHUB_TOKEN not available; local tracker saved only.")
        return False

    if not GITHUB_REPOSITORY:
        print("[GITHUB] GITHUB_REPOSITORY not available; local tracker saved only.")
        return False

    try:
        with open(TRACKER_FILE, "rb") as f:
            content = f.read()

        encoded = base64.b64encode(content).decode("utf-8")

        api_url = (
            f"https://api.github.com/repos/"
            f"{GITHUB_REPOSITORY}/contents/{TRACKER_FILE}"
        )

        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # Find current SHA if file already exists.
        sha = None

        response = SESSION.get(
            api_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            existing = response.json()
            sha = existing.get("sha")

        payload = {
            "message": f"Update scanner tracker [{VERSION}]",
            "content": encoded,
            "branch": os.getenv("GITHUB_REF_NAME", "main"),
        }

        if sha:
            payload["sha"] = sha

        put_response = SESSION.put(
            api_url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        if put_response.status_code in (200, 201):
            print("[GITHUB] tracker.json saved.")
            return True

        print(
            "[GITHUB] tracker save failed:",
            put_response.status_code,
            put_response.text[:500],
        )

        return False

    except Exception as e:
        print(f"[GITHUB] tracker save exception: {e}")
        return False


# =============================================================
# TELEGRAM
# =============================================================

def telegram_api_url(method):
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        print(message)
        return False

    # Telegram has a message size limit.
    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        message = message[:TELEGRAM_MESSAGE_LIMIT - 50] + "\n...[truncated]"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = SESSION.post(
            telegram_api_url("sendMessage"),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            return True

        print(
            "[TELEGRAM] Send failed:",
            response.status_code,
            response.text[:500],
        )

        return False

    except Exception as e:
        print(f"[TELEGRAM] Send exception: {e}")
        return False


def telegram_get_updates(offset):
    if not TELEGRAM_TOKEN:
        return []

    params = {
        "offset": offset,
        "limit": 100,
        "timeout": 1,
    }

    try:
        response = SESSION.get(
            telegram_api_url("getUpdates"),
            params=params,
            timeout=REQUEST_TIMEOUT + 5,
        )

        if response.status_code != 200:
            print(
                "[TELEGRAM] getUpdates failed:",
                response.status_code,
                response.text[:300],
            )
            return []

        data = response.json()

        if not data.get("ok"):
            return []

        return data.get("result", [])

    except Exception as e:
        print(f"[TELEGRAM] getUpdates exception: {e}")
        return []


# =============================================================
# COMMANDS
# =============================================================

def command_text(update):
    try:
        return update["message"]["text"].strip()
    except Exception:
        return ""


def command_chat_id(update):
    try:
        return str(update["message"]["chat"]["id"])
    except Exception:
        return ""


def command_name(text):
    if not text:
        return ""

    first = text.split()[0]

    if first.startswith("/"):
        first = first.split("@")[0]

    return first.lower()


def command_argument(text):
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return ""

    return parts[1].strip()


def help_message():
    return (
        f"🧠 PRECISION SIGNAL SCANNER {VERSION}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"5M TREND + 1M ENTRY\n"
        f"Reference expiry: {REFERENCE_EXPIRY_MINUTES} minutes\n"
        f"OTC: DISABLED\n\n"
        f"/scan — run a scan during the current scanner run\n"
        f"/stats — show recorded statistics\n"
        f"/win SIGNAL_ID — record WIN\n"
        f"/loss SIGNAL_ID — record LOSS\n"
        f"/start — scanner information\n"
        f"/help — show commands\n\n"
        f"⚠️ The score measures setup quality.\n"
        f"It is NOT a guaranteed win probability."
    )


def start_message():
    return (
        f"🧠 PRECISION SIGNAL SCANNER {VERSION}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: ACTIVE\n"
        f"Framework: 5M trend + 1M entry\n"
        f"Reference expiry: {REFERENCE_EXPIRY_MINUTES} minutes\n"
        f"OTC: DISABLED\n\n"
        f"The GitHub workflow performs one scan every 5 minutes.\n\n"
        f"Use /help for commands."
    )


def find_signal(tracker, signal_id):
    for signal in tracker.get("signals", []):
        if signal.get("signal_id") == signal_id:
            return signal

    return None


def record_outcome(tracker, signal_id, outcome):
    signal = find_signal(tracker, signal_id)

    if signal is None:
        return False, "Signal ID not found."

    old_outcome = signal.get("outcome")

    if old_outcome in ("WIN", "LOSS"):
        return False, f"Signal already recorded as {old_outcome}."

    signal["outcome"] = outcome
    signal["outcome_time"] = iso_now()

    return True, f"{signal_id} recorded as {outcome}."


def stats_message(tracker):
    signals = tracker.get("signals", [])

    total = len(signals)
    wins = sum(1 for x in signals if x.get("outcome") == "WIN")
    losses = sum(1 for x in signals if x.get("outcome") == "LOSS")
    pending = sum(
        1
        for x in signals
        if x.get("outcome") not in ("WIN", "LOSS")
    )

    completed = wins + losses

    if completed:
        win_rate = wins / completed * 100
        win_rate_text = f"{win_rate:.1f}%"
    else:
        win_rate_text = "N/A"

    calls = sum(1 for x in signals if x.get("signal") == "CALL")
    puts = sum(1 for x in signals if x.get("signal") == "PUT")

    return (
        f"📊 SCANNER STATISTICS\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Recorded signals: {total}\n"
        f"CALL: {calls}\n"
        f"PUT: {puts}\n"
        f"WINS: {wins}\n"
        f"LOSSES: {losses}\n"
        f"Pending: {pending}\n"
        f"Completed: {completed}\n"
        f"Recorded win rate: {win_rate_text}\n\n"
        f"⚠️ Historical results do not guarantee future results."
    )


# =============================================================
# MARKET DATA
# =============================================================

def bybit_get_klines(symbol, interval, limit=CANDLE_LIMIT):
    """
    Returns candles in chronological order.

    Each candle:
    {
        "time": unix milliseconds,
        "open": float,
        "high": float,
        "low": float,
        "close": float,
        "volume": float
    }
    """

    params = {
        "category": BYBIT_CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = SESSION.get(
                f"{BYBIT_BASE_URL}/v5/market/kline",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            if data.get("retCode") != 0:
                last_error = (
                    f"Bybit retCode={data.get('retCode')} "
                    f"retMsg={data.get('retMsg')}"
                )

                print(
                    f"[BYBIT] {symbol} {interval} attempt "
                    f"{attempt}/{REQUEST_RETRIES}: {last_error}"
                )

                time.sleep(1)
                continue

            rows = data.get("result", {}).get("list", [])

            if not rows:
                last_error = "No candles returned."
                time.sleep(1)
                continue

            candles = []

            for row in rows:
                if len(row) < 6:
                    continue

                candle = {
                    "time": int(row[0]),
                    "open": safe_float(row[1]),
                    "high": safe_float(row[2]),
                    "low": safe_float(row[3]),
                    "close": safe_float(row[4]),
                    "volume": safe_float(row[5], 0.0),
                }

                if None in (
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"],
                ):
                    continue

                candles.append(candle)

            candles.sort(key=lambda x: x["time"])

            if len(candles) < 50:
                last_error = (
                    f"Insufficient candles: {len(candles)}"
                )
                time.sleep(1)
                continue

            return candles

        except Exception as e:
            last_error = str(e)

            print(
                f"[BYBIT] {symbol} {interval} exception "
                f"attempt {attempt}/{REQUEST_RETRIES}: {e}"
            )

            time.sleep(1)

    raise RuntimeError(
        f"Unable to fetch {symbol} {interval}: {last_error}"
    )


# =============================================================
# OTC ADAPTER
# =============================================================

def get_otc_candles(symbol, timeframe):
    """
    OTC is deliberately disabled.

    Do NOT substitute normal-market candles for OTC.
    """

    if not OTC_ENABLED:
        return []

    return []


# =============================================================
# INDICATORS
# =============================================================

def closes(candles):
    return [float(x["close"]) for x in candles]


def highs(candles):
    return [float(x["high"]) for x in candles]


def lows(candles):
    return [float(x["low"]) for x in candles]


def ema(values, period):
    if not values or len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    result = [float(values[0])]

    for value in values[1:]:
        result.append(
            (float(value) - result[-1]) * multiplier + result[-1]
        )

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = []

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100 - (100 / (1 + rs)))

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - (100 / (1 + rs)))

    return result


def true_ranges(candles):
    if not candles:
        return []

    result = []

    for i, candle in enumerate(candles):
        high = candle["high"]
        low = candle["low"]

        if i == 0:
            tr = high - low
        else:
            previous_close = candles[i - 1]["close"]

            tr = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )

        result.append(tr)

    return result


def atr(candles, period=14):
    tr = true_ranges(candles)

    if len(tr) < period:
        return []

    first = sum(tr[:period]) / period
    result = [first]

    previous = first

    for value in tr[period:]:
        previous = (
            (previous * (period - 1)) + value
        ) / period

        result.append(previous)

    return result


def macd(values, fast_period=12, slow_period=26, signal_period=9):
    if len(values) < slow_period + signal_period:
        return None

    fast = ema(values, fast_period)
    slow = ema(values, slow_period)

    # Align fast EMA with slow EMA.
    offset = slow_period - fast_period

    fast_aligned = fast[offset:]

    if len(fast_aligned) != len(slow):
        minimum = min(len(fast_aligned), len(slow))
        fast_aligned = fast_aligned[-minimum:]
        slow = slow[-minimum:]

    macd_line = [
        f - s
        for f, s in zip(fast_aligned, slow)
    ]

    signal_line = ema(macd_line, signal_period)

    if not signal_line:
        return None

    return {
        "line": macd_line,
        "signal": signal_line,
        "histogram": (
            macd_line[-len(signal_line):][-1]
            - signal_line[-1]
        ),
    }


def adx(candles, period=14):
    if len(candles) < period * 2 + 5:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        if up_move > down_move and up_move > 0:
            p_dm = up_move
        else:
            p_dm = 0

        if down_move > up_move and down_move > 0:
            m_dm = down_move
        else:
            m_dm = 0

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

        trs.append(tr)
        plus_dm.append(p_dm)
        minus_dm.append(m_dm)

    if len(trs) < period:
        return None

    smoothed_tr = sum(trs[:period])
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])

    dx_values = []
    plus_di_values = []
    minus_di_values = []

    for i in range(period, len(trs)):
        smoothed_tr = (
            smoothed_tr
            - (smoothed_tr / period)
            + trs[i]
        )

        smoothed_plus = (
            smoothed_plus
            - (smoothed_plus / period)
            + plus_dm[i]
        )

        smoothed_minus = (
            smoothed_minus
            - (smoothed_minus / period)
            + minus_dm[i]
        )

        if smoothed_tr == 0:
            plus_di = 0
            minus_di = 0
        else:
            plus_di = (
                100 * smoothed_plus / smoothed_tr
            )

            minus_di = (
                100 * smoothed_minus / smoothed_tr
            )

        denominator = plus_di + minus_di

        if denominator == 0:
            dx = 0
        else:
            dx = (
                100
                * abs(plus_di - minus_di)
                / denominator
            )

        plus_di_values.append(plus_di)
        minus_di_values.append(minus_di)
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_value = sum(dx_values[:period]) / period

    for value in dx_values[period:]:
        adx_value = (
            (adx_value * (period - 1)) + value
        ) / period

    return {
        "adx": adx_value,
        "plus_di": plus_di_values[-1],
        "minus_di": minus_di_values[-1],
    }


# =============================================================
# PRICE ACTION
# =============================================================

def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "BULLISH"

    if candle["close"] < candle["open"]:
        return "BEARISH"

    return "NEUTRAL"


def candle_strength(candle):
    total_range = candle["high"] - candle["low"]

    if total_range <= 0:
        return 0

    body = abs(candle["close"] - candle["open"])

    return body / total_range


def structure_direction(candles, lookback=8):
    if len(candles) < lookback + 2:
        return "NEUTRAL"

    recent = candles[-lookback:]

    highs_list = [x["high"] for x in recent]
    lows_list = [x["low"] for x in recent]

    first_half = recent[:lookback // 2]
    second_half = recent[lookback // 2:]

    first_high = max(x["high"] for x in first_half)
    second_high = max(x["high"] for x in second_half)

    first_low = min(x["low"] for x in first_half)
    second_low = min(x["low"] for x in second_half)

    higher_high = second_high > first_high
    higher_low = second_low > first_low

    lower_high = second_high < first_high
    lower_low = second_low < first_low

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    # Secondary check using recent close movement.
    if recent[-1]["close"] > recent[0]["close"]:
        return "BULLISH"

    if recent[-1]["close"] < recent[0]["close"]:
        return "BEARISH"

    return "NEUTRAL"


def trend_direction(candles):
    values = closes(candles)

    ema9 = ema(values, 9)
    ema21 = ema(values, 21)
    ema50 = ema(values, 50)

    if not ema9 or not ema21 or not ema50:
        return "NEUTRAL", None

    e9 = ema9[-1]
    e21 = ema21[-1]
    e50 = ema50[-1]
    price = values[-1]

    if price > e9 > e21 > e50:
        return "BULLISH", {
            "ema9": e9,
            "ema21": e21,
            "ema50": e50,
        }

    if price < e9 < e21 < e50:
        return "BEARISH", {
            "ema9": e9,
            "ema21": e21,
            "ema50": e50,
        }

    # Less strict directional state.
    if e9 > e21 and e21 > e50:
        return "BULLISH", {
            "ema9": e9,
            "ema21": e21,
            "ema50": e50,
        }

    if e9 < e21 and e21 < e50:
        return "BEARISH", {
            "ema9": e9,
            "ema21": e21,
            "ema50": e50,
        }

    return "NEUTRAL", {
        "ema9": e9,
        "ema21": e21,
        "ema50": e50,
    }


# =============================================================
# PULLBACK
# =============================================================

def clean_pullback(candles, direction):
    if len(candles) < 10:
        return False

    recent = candles[-6:]

    if direction == "BULLISH":
        bullish_count = sum(
            1 for x in recent[:-1]
            if candle_direction(x) == "BULLISH"
        )

        bearish_pullback = any(
            candle_direction(x) == "BEARISH"
            for x in recent[:-2]
        )

        return bullish_count >= 2 and bearish_pullback

    if direction == "BEARISH":
        bearish_count = sum(
            1 for x in recent[:-1]
            if candle_direction(x) == "BEARISH"
        )

        bullish_pullback = any(
            candle_direction(x) == "BULLISH"
            for x in recent[:-2]
        )

        return bearish_count >= 2 and bullish_pullback

    return False


# =============================================================
# ROOM / EXTENSION
# =============================================================

def room_score(candles, direction):
    if len(candles) < 25:
        return 0

    recent = candles[-25:]
    current = recent[-1]["close"]

    atr_values = atr(recent, 14)

    if not atr_values:
        return 0

    current_atr = atr_values[-1]

    if current_atr <= 0:
        return 0

    if direction == "BULLISH":
        resistance = max(x["high"] for x in recent[:-2])
        room = resistance - current

    elif direction == "BEARISH":
        support = min(x["low"] for x in recent[:-2])
        room = current - support

    else:
        return 0

    ratio = room / current_atr

    if ratio >= 2.5:
        return 5

    if ratio >= 1.8:
        return 4

    if ratio >= 1.2:
        return 3

    if ratio >= 0.8:
        return 2

    if ratio >= 0.4:
        return 1

    return 0


def extension_score(candles, direction):
    if len(candles) < 30:
        return 0

    values = closes(candles)
    current = values[-1]

    ema21_values = ema(values, 21)

    if not ema21_values:
        return 0

    e21 = ema21_values[-1]

    atr_values = atr(candles, 14)

    if not atr_values:
        return 0

    current_atr = atr_values[-1]

    if current_atr <= 0:
        return 0

    distance = abs(current - e21)
    ratio = distance / current_atr

    # Score represents how acceptable the extension is.
    # Higher = less extended.
    if ratio <= 0.5:
        return 5

    if ratio <= 0.9:
        return 4

    if ratio <= 1.3:
        return 3

    if ratio <= 1.8:
        return 2

    if ratio <= 2.3:
        return 1

    return 0


# =============================================================
# INDICATOR ANALYSIS
# =============================================================

def analyze_setup(main_candles, entry_candles):
    """
    Returns a complete setup analysis dictionary.
    """

    if len(main_candles) < 60:
        return {
            "direction": "NO TRADE",
            "score": 0,
            "blockers": ["insufficient 5M candles"],
        }

    if len(entry_candles) < 60:
        return {
            "direction": "NO TRADE",
            "score": 0,
            "blockers": ["insufficient 1M candles"],
        }

    # ---------------------------------------------------------
    # 5M TREND
    # ---------------------------------------------------------

    main_trend, main_emas = trend_direction(main_candles)

    # ---------------------------------------------------------
    # 5M STRUCTURE
    # ---------------------------------------------------------

    main_structure = structure_direction(main_candles)

    # ---------------------------------------------------------
    # 1M ENTRY
    # ---------------------------------------------------------

    entry_trend, entry_emas = trend_direction(entry_candles)

    entry_structure = structure_direction(entry_candles)

    # ---------------------------------------------------------
    # ADX / DMI
    # ---------------------------------------------------------

    main_adx = adx(main_candles)

    # ---------------------------------------------------------
    # MACD
    # ---------------------------------------------------------

    main_closes = closes(main_candles)
    main_macd = macd(main_closes)

    # ---------------------------------------------------------
    # RSI
    # ---------------------------------------------------------

    entry_closes = closes(entry_candles)
    entry_rsi_values = rsi(entry_closes)

    entry_rsi = (
        entry_rsi_values[-1]
        if entry_rsi_values
        else None
    )

    # ---------------------------------------------------------
    # CANDLE
    # ---------------------------------------------------------

    confirmation_candle = entry_candles[-1]

    confirmation_direction = candle_direction(
        confirmation_candle
    )

    confirmation_strength = candle_strength(
        confirmation_candle
    )

    # ---------------------------------------------------------
    # DOMINANCE
    # ---------------------------------------------------------

    bullish_points = 0
    bearish_points = 0

    directional_items = [
        main_trend,
        main_structure,
        entry_trend,
        entry_structure,
        confirmation_direction,
    ]

    for item in directional_items:
        if item == "BULLISH":
            bullish_points += 1

        elif item == "BEARISH":
            bearish_points += 1

    dominance = abs(
        bullish_points - bearish_points
    )

    # ---------------------------------------------------------
    # DETERMINE DIRECTION
    # ---------------------------------------------------------

    if bullish_points > bearish_points:
        direction = "CALL"

    elif bearish_points > bullish_points:
        direction = "PUT"

    else:
        direction = "NO TRADE"

    blockers = []

    # ---------------------------------------------------------
    # HARD BLOCKERS
    # ---------------------------------------------------------

    if direction == "CALL":

        if main_trend != "BULLISH":
            blockers.append("5M trend mismatch")

        if entry_trend != "BULLISH":
            blockers.append("1M entry trend mismatch")

        if main_structure != "BULLISH":
            blockers.append("5M structure not bullish")

        if entry_structure != "BULLISH":
            blockers.append("1M structure not bullish")

        if main_adx is None:
            blockers.append("ADX unavailable")

        else:
            if main_adx["adx"] < MIN_ADX:
                blockers.append(
                    f"ADX too low ({main_adx['adx']:.1f})"
                )

            if main_adx["plus_di"] <= main_adx["minus_di"]:
                blockers.append("DMI mismatch")

        if main_macd is None:
            blockers.append("MACD unavailable")

        else:
            macd_line = main_macd["line"][-1]
            signal_line = main_macd["signal"][-1]

            if macd_line <= signal_line:
                blockers.append("MACD mismatch")

        if entry_rsi is None:
            blockers.append("RSI unavailable")

        elif not (
            CALL_RSI_MIN
            <= entry_rsi
            <= CALL_RSI_MAX
        ):
            blockers.append(
                f"RSI outside CALL zone ({entry_rsi:.1f})"
            )

        if confirmation_direction != "BULLISH":
            blockers.append("confirmation candle mismatch")

        if confirmation_strength < MIN_CANDLE_STRENGTH:
            blockers.append(
                f"weak candle ({confirmation_strength:.2f})"
            )

        if not clean_pullback(
            entry_candles,
            "BULLISH",
        ):
            blockers.append("no clean bullish pullback")

    elif direction == "PUT":

        if main_trend != "BEARISH":
            blockers.append("5M trend mismatch")

        if entry_trend != "BEARISH":
            blockers.append("1M entry trend mismatch")

        if main_structure != "BEARISH":
            blockers.append("5M structure not bearish")

        if entry_structure != "BEARISH":
            blockers.append("1M structure not bearish")

        if main_adx is None:
            blockers.append("ADX unavailable")

        else:
            if main_adx["adx"] < MIN_ADX:
                blockers.append(
                    f"ADX too low ({main_adx['adx']:.1f})"
                )

            if main_adx["minus_di"] <= main_adx["plus_di"]:
                blockers.append("DMI mismatch")

        if main_macd is None:
            blockers.append("MACD unavailable")

        else:
            macd_line = main_macd["line"][-1]
            signal_line = main_macd["signal"][-1]

            if macd_line >= signal_line:
                blockers.append("MACD mismatch")

        if entry_rsi is None:
            blockers.append("RSI unavailable")

        elif not (
            PUT_RSI_MIN
            <= entry_rsi
            <= PUT_RSI_MAX
        ):
            blockers.append(
                f"RSI outside PUT zone ({entry_rsi:.1f})"
            )

        if confirmation_direction != "BEARISH":
            blockers.append("confirmation candle mismatch")

        if confirmation_strength < MIN_CANDLE_STRENGTH:
            blockers.append(
                f"weak candle ({confirmation_strength:.2f})"
            )

        if not clean_pullback(
            entry_candles,
            "BEARISH",
        ):
            blockers.append("no clean bearish pullback")

    else:
        blockers.append("no directional agreement")

    # ---------------------------------------------------------
    # ROOM
    # ---------------------------------------------------------

    room = room_score(
        main_candles,
        "BULLISH" if direction == "CALL"
        else "BEARISH" if direction == "PUT"
        else "NEUTRAL",
    )

    if room < MIN_ROOM_SCORE:
        blockers.append(
            f"insufficient room ({room}/5)"
        )

    # ---------------------------------------------------------
    # EXTENSION
    # ---------------------------------------------------------

    extension = extension_score(
        main_candles,
        "BULLISH" if direction == "CALL"
        else "BEARISH" if direction == "PUT"
        else "NEUTRAL",
    )

    if extension < MIN_EXTENSION_SCORE:
        blockers.append(
            f"price too extended ({extension}/5)"
        )

    # ---------------------------------------------------------
    # SCORE
    # ---------------------------------------------------------

    score = 0

    # Trend /20
    if direction == "CALL":
        if main_trend == "BULLISH":
            score += 15

        if entry_trend == "BULLISH":
            score += 5

    elif direction == "PUT":
        if main_trend == "BEARISH":
            score += 15

        if entry_trend == "BEARISH":
            score += 5

    # Structure /10
    if direction == "CALL":
        if main_structure == "BULLISH":
            score += 6

        if entry_structure == "BULLISH":
            score += 4

    elif direction == "PUT":
        if main_structure == "BEARISH":
            score += 6

        if entry_structure == "BEARISH":
            score += 4

    # ADX /10
    if main_adx:
        adx_value = main_adx["adx"]

        if adx_value >= STRONG_ADX:
            score += 7
        elif adx_value >= MIN_ADX:
            score += 5

        if direction == "CALL":
            if main_adx["plus_di"] > main_adx["minus_di"]:
                score += 3

        elif direction == "PUT":
            if main_adx["minus_di"] > main_adx["plus_di"]:
                score += 3

    # MACD /10
    if main_macd:
        macd_line = main_macd["line"][-1]
        signal_line = main_macd["signal"][-1]
        histogram = main_macd["histogram"]

        if direction == "CALL":
            if macd_line > signal_line:
                score += 7

            if histogram > 0:
                score += 3

        elif direction == "PUT":
            if macd_line < signal_line:
                score += 7

            if histogram < 0:
                score += 3

    # RSI /10
    if entry_rsi is not None:
        if direction == "CALL":
            if 48 <= entry_rsi <= 62:
                score += 10
            elif CALL_RSI_MIN <= entry_rsi <= CALL_RSI_MAX:
                score += 7

        elif direction == "PUT":
            if 38 <= entry_rsi <= 52:
                score += 10
            elif PUT_RSI_MIN <= entry_rsi <= PUT_RSI_MAX:
                score += 7

    # Entry /15
    if direction == "CALL":
        if entry_trend == "BULLISH":
            score += 8

        if entry_structure == "BULLISH":
            score += 4

        if confirmation_direction == "BULLISH":
            score += 3

    elif direction == "PUT":
        if entry_trend == "BEARISH":
            score += 8

        if entry_structure == "BEARISH":
            score += 4

        if confirmation_direction == "BEARISH":
            score += 3

    # Pullback /10
    if direction == "CALL":
        if clean_pullback(entry_candles, "BULLISH"):
            score += 10

    elif direction == "PUT":
        if clean_pullback(entry_candles, "BEARISH"):
            score += 10

    # Candle /5
    if confirmation_strength >= 0.75:
        score += 5
    elif confirmation_strength >= MIN_CANDLE_STRENGTH:
        score += 3

    # Room /5
    score += room

    # Extension /5
    score += extension

    score = int(clamp(score, 0, 100))

    return {
        "direction": direction,
        "score": score,
        "blockers": blockers,
        "main_trend": main_trend,
        "main_structure": main_structure,
        "entry_trend": entry_trend,
        "entry_structure": entry_structure,
        "adx": main_adx["adx"] if main_adx else None,
        "plus_di": main_adx["plus_di"] if main_adx else None,
        "minus_di": main_adx["minus_di"] if main_adx else None,
        "macd": (
            main_macd["line"][-1]
            if main_macd
            else None
        ),
        "macd_signal": (
            main_macd["signal"][-1]
            if main_macd
            else None
        ),
        "macd_histogram": (
            main_macd["histogram"]
            if main_macd
            else None
        ),
        "rsi": entry_rsi,
        "candle_direction": confirmation_direction,
        "candle_strength": confirmation_strength,
        "room_score": room,
        "extension_score": extension,
        "dominance": dominance,
        "bullish_points": bullish_points,
        "bearish_points": bearish_points,
        "price": entry_candles[-1]["close"],
        "entry_time": entry_candles[-1]["time"],
    }


# =============================================================
# SIGNAL CLASSIFICATION
# =============================================================

def classify_setup(analysis):
    direction = analysis.get("direction")
    score = analysis.get("score", 0)
    blockers = analysis.get("blockers", [])

    if direction not in ("CALL", "PUT"):
        return "NO TRADE"

    if blockers:
        return "NO TRADE"

    if analysis.get("dominance", 0) < MIN_DOMINANCE:
        return "NO TRADE"

    if score >= MIN_SCORE:
        return "QUALIFIED"

    if score >= BORDERLINE_SCORE:
        return "BORDERLINE"

    return "NO TRADE"


# =============================================================
# SIGNAL ID
# =============================================================

def make_signal_id(
    asset,
    direction,
    entry_time,
    mode="NORMAL",
):
    timestamp = int(entry_time // 1000)

    if mode == "OTC":
        return f"{asset}-OTC-{direction}-{timestamp}"

    return f"{asset}-{direction}-{timestamp}"


# =============================================================
# DUPLICATE / LOCK MANAGEMENT
# =============================================================

def signal_already_recorded(tracker, signal_id):
    return find_signal(tracker, signal_id) is not None


def signal_key(asset, direction, entry_time):
    return f"{asset}|{direction}|{entry_time}"


def is_locked(tracker, asset):
    locks = tracker.setdefault("metadata", {}).setdefault(
        "signal_locks",
        {},
    )

    lock_until = locks.get(asset)

    if not lock_until:
        return False

    try:
        return time.time() < float(lock_until)
    except Exception:
        return False


def set_signal_lock(tracker, asset):
    locks = tracker.setdefault("metadata", {}).setdefault(
        "signal_locks",
        {},
    )

    # IMPORTANT:
    # Lock starts from actual signal creation/processing time,
    # not from the candle timestamp.
    locks[asset] = time.time() + SIGNAL_LOCK_SECONDS


def mark_processed(tracker, key):
    metadata = tracker.setdefault("metadata", {})

    keys = metadata.setdefault("alerted_keys", [])

    if key not in keys:
        keys.append(key)

    trim_tracker(tracker)


def was_processed(tracker, key):
    metadata = tracker.setdefault("metadata", {})

    return key in metadata.setdefault(
        "alerted_keys",
        [],
    )


# =============================================================
# SIGNAL MESSAGE
# =============================================================

def build_signal_message(
    asset,
    analysis,
    signal_id,
    mode="NORMAL",
):
    direction = analysis["direction"]

    if direction == "CALL":
        emoji = "🟢"
        label = "CALL / UP"
    else:
        emoji = "🔴"
        label = "PUT / DOWN"

    adx_text = fmt_number(analysis.get("adx"), 1)
    rsi_text = fmt_number(analysis.get("rsi"), 1)

    return (
        f"{emoji} NEW QUALIFIED SIGNAL\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧠 PRECISION SCANNER {VERSION}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💱 {asset} {label}\n"
        f"🎯 Score: {analysis['score']}/100\n"
        f"⏱ Reference expiry: "
        f"{REFERENCE_EXPIRY_MINUTES} minutes\n"
        f"💰 Price: {fmt_price(analysis['price'])}\n"
        f"📊 5M Trend: {analysis['main_trend']}\n"
        f"📈 1M Entry: {analysis['entry_trend']}\n"
        f"🏗 Structure: {analysis['main_structure']}\n"
        f"📐 ADX: {adx_text}\n"
        f"📉 RSI: {rsi_text}\n"
        f"📊 MACD: "
        f"{fmt_number(analysis.get('macd'), 6)}\n"
        f"🕯 Candle: "
        f"{analysis['candle_direction']} "
        f"({analysis['candle_strength']:.2f})\n"
        f"↩️ Pullback: CLEAN\n"
        f"🚪 Room: "
        f"{analysis['room_score']}/5\n"
        f"📏 Extension: "
        f"{analysis['extension_score']}/5\n"
        f"🧭 Dominance: "
        f"{analysis['dominance']}\n"
        f"🆔 Signal ID: {signal_id}\n"
        f"🌐 Mode: {mode}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Setup-quality score only.\n"
        f"Not a guaranteed win probability.\n"
        f"Demo/testing recommended."
    )


# =============================================================
# PROCESS SIGNAL
# =============================================================

def process_result(
    tracker,
    asset,
    analysis,
    mode="NORMAL",
):
    classification = classify_setup(analysis)

    if classification == "NO TRADE":
        return {
            "classification": "NO TRADE",
            "new_record": False,
            "alert_sent": False,
            "delivery_failed": False,
        }

    if classification == "BORDERLINE":
        return {
            "classification": "BORDERLINE",
            "new_record": False,
            "alert_sent": False,
            "delivery_failed": False,
        }

    direction = analysis["direction"]
    entry_time = analysis["entry_time"]

    signal_id = make_signal_id(
        asset,
        direction,
        entry_time,
        mode,
    )

    key = signal_key(
        asset,
        direction,
        entry_time,
    )

    # ---------------------------------------------------------
    # SAME SIGNAL ALREADY PROCESSED
    # ---------------------------------------------------------

    if was_processed(tracker, key):
        return {
            "classification": "QUALIFIED",
            "new_record": False,
            "alert_sent": False,
            "delivery_failed": False,
            "duplicate": True,
            "signal_id": signal_id,
        }

    # ---------------------------------------------------------
    # SAME SIGNAL ALREADY IN TRACKER
    # ---------------------------------------------------------

    if signal_already_recorded(tracker, signal_id):
        mark_processed(tracker, key)

        return {
            "classification": "QUALIFIED",
            "new_record": False,
            "alert_sent": False,
            "delivery_failed": False,
            "duplicate": True,
            "signal_id": signal_id,
        }

    # ---------------------------------------------------------
    # 5-MINUTE ASSET LOCK
    # ---------------------------------------------------------

    if is_locked(tracker, asset):
        print(
            f"[LOCK] {asset} is locked. "
            f"Qualified signal suppressed."
        )

        return {
            "classification": "QUALIFIED",
            "new_record": False,
            "alert_sent": False,
            "delivery_failed": False,
            "locked": True,
            "signal_id": signal_id,
        }

    # ---------------------------------------------------------
    # CREATE RECORD
    # ---------------------------------------------------------

    created_time = unix_now()

    record = {
        "signal_id": signal_id,
        "asset": asset,
        "mode": mode,
        "signal": direction,
        "score": analysis["score"],
        "price": analysis["price"],
        "entry_time": entry_time,
        "created_at": created_time,
        "created_at_iso": iso_now(),
        "reference_expiry_minutes": REFERENCE_EXPIRY_MINUTES,
        "outcome": None,
        "outcome_time": None,
        "metadata": {
            "main_trend": analysis["main_trend"],
            "main_structure": analysis["main_structure"],
            "entry_trend": analysis["entry_trend"],
            "entry_structure": analysis["entry_structure"],
            "adx": analysis["adx"],
            "plus_di": analysis["plus_di"],
            "minus_di": analysis["minus_di"],
            "rsi": analysis["rsi"],
            "macd": analysis["macd"],
            "macd_signal": analysis["macd_signal"],
            "macd_histogram": analysis["macd_histogram"],
            "candle_direction": analysis["candle_direction"],
            "candle_strength": analysis["candle_strength"],
            "room_score": analysis["room_score"],
            "extension_score": analysis["extension_score"],
            "dominance": analysis["dominance"],
        },
    }

    tracker.setdefault("signals", []).append(record)

    tracker.setdefault("metadata", {})[
        "last_signal"
    ] = signal_id

    # ---------------------------------------------------------
    # START LOCK FROM ACTUAL CREATION TIME
    # ---------------------------------------------------------

    set_signal_lock(tracker, asset)

    # ---------------------------------------------------------
    # MARK AS PROCESSED BEFORE DELIVERY
    # ---------------------------------------------------------
    #
    # This prevents duplicate records/alerts if Telegram
    # delivery fails and the scanner is run again.
    #

    mark_processed(tracker, key)

    # ---------------------------------------------------------
    # SEND TELEGRAM ALERT
    # ---------------------------------------------------------

    message = build_signal_message(
        asset,
        analysis,
        signal_id,
        mode,
    )

    alert_sent = telegram_send(message)

    delivery_failed = not alert_sent

    if delivery_failed:
        failed_keys = tracker.setdefault(
            "metadata",
            {},
        ).setdefault(
            "delivery_failed_keys",
            [],
        )

        if key not in failed_keys:
            failed_keys.append(key)

        print(
            f"[ALERT] Delivery failed for {signal_id}, "
            f"but signal remains recorded and deduplicated."
        )

    else:
        print(
            f"[ALERT] Telegram signal sent: {signal_id}"
        )

    trim_tracker(tracker)

    return {
        "classification": "QUALIFIED",
        "new_record": True,
        "alert_sent": alert_sent,
        "delivery_failed": delivery_failed,
        "signal_id": signal_id,
    }


# =============================================================
# SINGLE ASSET SCAN
# =============================================================

def scan_asset(asset, provider_symbol, mode="NORMAL"):
    print(
        f"[SCAN] {asset} "
        f"({provider_symbol}) "
        f"mode={mode}"
    )

    if mode == "OTC":
        main_candles = get_otc_candles(
            provider_symbol,
            MAIN_TIMEFRAME,
        )

        entry_candles = get_otc_candles(
            provider_symbol,
            ENTRY_TIMEFRAME,
        )

    else:
        main_candles = bybit_get_klines(
            provider_symbol,
            MAIN_TIMEFRAME,
        )

        entry_candles = bybit_get_klines(
            provider_symbol,
            ENTRY_TIMEFRAME,
        )

    if not main_candles:
        raise RuntimeError(
            f"No {MAIN_TIMEFRAME} candles for {asset}"
        )

    if not entry_candles:
        raise RuntimeError(
            f"No {ENTRY_TIMEFRAME} candles for {asset}"
        )

    analysis = analyze_setup(
        main_candles,
        entry_candles,
    )

    return analysis


# =============================================================
# FULL SCAN CYCLE
# =============================================================

def run_scan_cycle(tracker):
    started = time.time()

    tracker.setdefault("metadata", {})[
        "last_scan"
    ] = iso_now()

    tracker["metadata"]["scan_count"] = (
        tracker["metadata"].get("scan_count", 0) + 1
    )

    cycle_number = tracker["metadata"]["scan_count"]

    qualified = []
    borderline = []
    rejected = []
    errors = []
    new_records = []
    alerts_sent = []

    print()
    print("=" * 60)
    print(
        f"PRECISION SIGNAL SCANNER {VERSION}"
    )
    print(
        f"SCAN #{cycle_number}"
    )
    print(
        f"Time: {iso_now()}"
    )
    print("=" * 60)

    # ---------------------------------------------------------
    # NORMAL MARKET
    # ---------------------------------------------------------

    for asset, provider_symbol in NORMAL_SYMBOLS.items():

        try:
            analysis = scan_asset(
                asset,
                provider_symbol,
                "NORMAL",
            )

            classification = classify_setup(
                analysis
            )

            if classification == "QUALIFIED":
                qualified.append(
                    (asset, analysis)
                )

                result = process_result(
                    tracker,
                    asset,
                    analysis,
                    "NORMAL",
                )

                if result.get("new_record"):
                    new_records.append(
                        result["signal_id"]
                    )

                if result.get("alert_sent"):
                    alerts_sent.append(
                        result["signal_id"]
                    )

            elif classification == "BORDERLINE":
                borderline.append(
                    (asset, analysis)
                )

            else:
                rejected.append(
                    (asset, analysis)
                )

            print(
                f"[RESULT] {asset}: "
                f"{analysis['direction']} "
                f"score={analysis['score']} "
                f"blockers={len(analysis['blockers'])}"
            )

        except Exception as e:
            errors.append(
                (asset, str(e))
            )

            print(
                f"[ERROR] {asset}: {e}"
            )

    # ---------------------------------------------------------
    # OTC
    # ---------------------------------------------------------

    if OTC_ENABLED:
        for asset, provider_symbol in OTC_SYMBOLS.items():

            if not provider_symbol:
                continue

            try:
                analysis = scan_asset(
                    asset,
                    provider_symbol,
                    "OTC",
                )

                classification = classify_setup(
                    analysis
                )

                if classification == "QUALIFIED":
                    qualified.append(
                        (asset, analysis)
                    )

                    result = process_result(
                        tracker,
                        asset,
                        analysis,
                        "OTC",
                    )

                    if result.get("new_record"):
                        new_records.append(
                            result["signal_id"]
                        )

                    if result.get("alert_sent"):
                        alerts_sent.append(
                            result["signal_id"]
                        )

                elif classification == "BORDERLINE":
                    borderline.append(
                        (asset, analysis)
                    )

                else:
                    rejected.append(
                        (asset, analysis)
                    )

            except Exception as e:
                errors.append(
                    (asset, str(e))
                )

    else:
        print(
            "[OTC] Disabled. "
            "No normal-market substitution is used."
        )

    # ---------------------------------------------------------
    # HEARTBEAT
    # ---------------------------------------------------------

    if (
        cycle_number % HEARTBEAT_EVERY_SCANS == 0
    ):
        tracker.setdefault(
            "metadata",
            {},
        )["last_heartbeat"] = iso_now()

        telegram_send(
            f"💓 PRECISION SCANNER HEARTBEAT\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Version: {VERSION}\n"
            f"Scan: #{cycle_number}\n"
            f"Time: {iso_now()}\n"
            f"Markets checked: "
            f"{len(NORMAL_SYMBOLS)}\n"
            f"OTC: DISABLED\n"
        )

    # ---------------------------------------------------------
    # SAVE
    # ---------------------------------------------------------

    trim_tracker(tracker)

    save_tracker_local(tracker)

    elapsed = time.time() - started

    print()
    print("=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)
    print(f"Qualified setups: {len(qualified)}")
    print(f"Borderline setups: {len(borderline)}")
    print(f"Rejected setups: {len(rejected)}")
    print(f"Errors: {len(errors)}")
    print(f"New signals recorded: {len(new_records)}")
    print(f"Telegram alerts sent: {len(alerts_sent)}")
    print(f"Duration: {elapsed:.2f}s")
    print("=" * 60)

    return {
        "qualified": qualified,
        "borderline": borderline,
        "rejected": rejected,
        "errors": errors,
        "new_records": new_records,
        "alerts_sent": alerts_sent,
        "duration": elapsed,
    }


# =============================================================
# MANUAL SCAN SUMMARY
# =============================================================

def build_scan_summary(results):
    qualified = results.get("qualified", [])
    borderline = results.get("borderline", [])
    rejected = results.get("rejected", [])
    errors = results.get("errors", [])
    new_records = results.get("new_records", [])
    alerts_sent = results.get("alerts_sent", [])

    lines = [
        "🔎 MANUAL SCAN COMPLETE",
        "━━━━━━━━━━━━━━━━━━",
        f"Qualified setups: {len(qualified)}",
        f"New signals recorded: {len(new_records)}",
        f"Telegram alerts sent: {len(alerts_sent)}",
        f"Borderline setups: {len(borderline)}",
        f"Rejected: {len(rejected)}",
        f"Errors: {len(errors)}",
        f"Duration: {results.get('duration', 0):.1f}s",
    ]

    if qualified:
        lines.append("")
        lines.append("Qualified:")

        for asset, analysis in qualified:
            lines.append(
                f"• {asset} "
                f"{analysis['direction']} "
                f"{analysis['score']}/100"
            )

    if errors:
        lines.append("")
        lines.append("Errors:")

        for asset, error in errors:
            lines.append(
                f"• {asset}: {error[:120]}"
            )

    lines.append("")
    lines.append(
        "⚠️ No-trade results are normal. "
        "The scanner does not force signals."
    )

    return "\n".join(lines)


# =============================================================
# COMMAND PROCESSING
# =============================================================

def process_commands(tracker):
    """
    Process Telegram commands available during this workflow run.

    Returns:
        changed, manual_scan_requested
    """

    offset = int(tracker.get("offset", 0))

    updates = telegram_get_updates(offset)

    changed = False
    manual_scan_requested = False

    for update in updates:

        update_id = update.get("update_id")

        if update_id is not None:
            tracker["offset"] = update_id + 1
            changed = True

        text = command_text(update)

        if not text.startswith("/"):
            continue

        command = command_name(text)
        argument = command_argument(text)

        chat_id = command_chat_id(update)

        # Only respond to configured chat.
        if (
            TELEGRAM_CHAT_ID
            and chat_id
            and chat_id != str(TELEGRAM_CHAT_ID)
        ):
            continue

        if command == "/start":
            telegram_send(start_message())

        elif command == "/help":
            telegram_send(help_message())

        elif command == "/stats":
            telegram_send(
                stats_message(tracker)
            )

        elif command == "/scan":
            manual_scan_requested = True

            telegram_send(
                "🔎 Manual scan requested.\n"
                "The scanner will perform the scan "
                "during this workflow run."
            )

        elif command == "/win":
            if not argument:
                telegram_send(
                    "Usage:\n"
                    "/win SIGNAL_ID"
                )
                continue

            ok, message = record_outcome(
                tracker,
                argument,
                "WIN",
            )

            telegram_send(
                ("✅ " if ok else "❌ ") + message
            )

            if ok:
                changed = True

        elif command == "/loss":
            if not argument:
                telegram_send(
                    "Usage:\n"
                    "/loss SIGNAL_ID"
                )
                continue

            ok, message = record_outcome(
                tracker,
                argument,
                "LOSS",
            )

            telegram_send(
                ("🔴 " if ok else "❌ ") + message
            )

            if ok:
                changed = True

    return changed, manual_scan_requested


# =============================================================
# MAIN
# =============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            f"Precision Signal Scanner {VERSION}"
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run exactly one scan and exit. "
            "Recommended for GitHub Actions."
        ),
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Run continuously every 60 seconds. "
            "Not recommended for GitHub Actions."
        ),
    )

    args = parser.parse_args()

    print()
    print("==============================================")
    print(
        f"PRECISION SIGNAL SCANNER {VERSION}"
    )
    print("==============================================")
    print(f"Started: {iso_now()}")
    print(
        f"Mode: "
        f"{'ONE-SHOT' if args.once else 'LOOP' if args.loop else 'ONE-SHOT'}"
    )
    print(
        f"Normal markets: "
        f"{', '.join(NORMAL_SYMBOLS.keys())}"
    )
    print(
        f"5M timeframe: {MAIN_TIMEFRAME}"
    )
    print(
        f"1M timeframe: {ENTRY_TIMEFRAME}"
    )
    print(
        f"Reference expiry: "
        f"{REFERENCE_EXPIRY_MINUTES} minutes"
    )
    print(
        f"Minimum score: {MIN_SCORE}/100"
    )
    print(
        f"OTC enabled: {OTC_ENABLED}"
    )
    print("==============================================")
    print()

    tracker = load_tracker()

    # ---------------------------------------------------------
    # PROCESS TELEGRAM COMMANDS
    # ---------------------------------------------------------

    changed, manual_scan_requested = process_commands(
        tracker
    )

    if changed:
        save_tracker_local(tracker)

    # ---------------------------------------------------------
    # ONE-SHOT MODE
    # ---------------------------------------------------------

    if args.once or not args.loop:
        results = run_scan_cycle(tracker)

        # Send manual summary only when /scan was requested.
        if manual_scan_requested:
            telegram_send(
                build_scan_summary(results)
            )

        save_tracker_to_github(tracker)

        print()
        print(
            f"[EXIT] {VERSION} one-shot scan complete."
        )

        return

    # ---------------------------------------------------------
    # CONTINUOUS MODE
    # ---------------------------------------------------------
    #
    # This exists for an always-on server/VPS.
    # Do NOT use this mode in GitHub Actions.
    #

    while True:

        try:
            results = run_scan_cycle(tracker)

            if manual_scan_requested:
                telegram_send(
                    build_scan_summary(results)
                )

            manual_scan_requested = False

            save_tracker_to_github(tracker)

        except KeyboardInterrupt:
            print(
                "[EXIT] Scanner stopped."
            )
            save_tracker_to_github(tracker)
            return

        except Exception as e:
            print(
                f"[FATAL] Scan cycle failed: {e}"
            )

            save_tracker_to_github(tracker)

        print(
            f"[SLEEP] Waiting "
            f"{SCAN_INTERVAL_SECONDS} seconds..."
        )

        time.sleep(SCAN_INTERVAL_SECONDS)


# =============================================================
# ENTRY POINT
# =============================================================

if __name__ == "__main__":
    main()
