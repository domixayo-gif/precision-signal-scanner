import os
import json
import html
import time
import subprocess
from datetime import datetime, timezone

import requests


# ============================================================
# PRECISION SCANNER V3.4
# Continuous 5M Trend + 1M Entry Scanner
# Coinbase Spot Proxy
# ============================================================

VERSION = "V3.4"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))

TRACKER_FILE = "tracker.json"

COINBASE_BASE = "https://api.exchange.coinbase.com"

ASSET_BASES = [
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "ADA",
    "TRX",
    "LINK",
    "TON",
    "AVAX",
    "DOGE",
    "DOT",
    "LTC",
    "POL",
]

# ------------------------------------------------------------
# STRATEGY SETTINGS
# ------------------------------------------------------------

MAIN_SECONDS = 300
ENTRY_SECONDS = 60

REFERENCE_EXPIRY_MINUTES = 5

MIN_SCORE = 80
BORDERLINE_SCORE = 75

# ------------------------------------------------------------
# ENGINE SETTINGS
# ------------------------------------------------------------

REQUEST_TIMEOUT = 20
MAX_REQUEST_RETRIES = 3

SCAN_INTERVAL_SECONDS = 60

# Refresh Coinbase product list every 30 minutes.
MARKET_REFRESH_SECONDS = 1800

# Do not send another signal for the same asset within 5 minutes.
ALERT_COOLDOWN_SECONDS = 300

# Send a "scanner is alive" message every 15 minutes.
HEARTBEAT_EVERY_SCANS = 15

MAX_TRACKER_ITEMS = 500

MAX_ALERTED_KEYS = 1000


# ============================================================
# HTTP SESSION
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "PrecisionScanner/3.4",
    "Accept": "application/json",
})


# ============================================================
# TIME HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_timestamp():
    return int(time.time())


def format_utc(ts=None):
    if ts is None:
        dt = utc_now()
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)

    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_candle_time(ts):
    if not ts:
        return "N/A"

    try:
        return datetime.fromtimestamp(
            float(ts),
            tz=timezone.utc
        ).strftime("%H:%M:%S UTC")
    except Exception:
        return "N/A"


# ============================================================
# TELEGRAM
# ============================================================

def tg(method, data=None):
    if not TELEGRAM_TOKEN:
        print("Telegram token not configured.")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

    try:
        response = SESSION.post(
            url,
            data=data or {},
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        return response.json()

    except Exception as exc:
        print(f"Telegram error [{method}]: {exc}")
        return None


def send_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram is not configured.")
        return False

    # Telegram message limit is approximately 4096 characters.
    chunks = [
        text[i:i + 3900]
        for i in range(0, len(text), 3900)
    ]

    success = True

    for chunk in chunks:

        result = tg(
            "sendMessage",
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

        if not result or not result.get("ok"):
            success = False

    return success


# ============================================================
# TRACKER
# ============================================================

def load_tracker():
    if not os.path.exists(TRACKER_FILE):

        return {
            "signals": [],
            "offset": 0,
            "meta": {
                "last_scan": None,
                "last_heartbeat": None,
                "last_signal": None,
                "alerted_keys": [],
            },
        }

    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

    except Exception as exc:
        print("Tracker load error:", exc)

        return {
            "signals": [],
            "offset": 0,
            "meta": {
                "last_scan": None,
                "last_heartbeat": None,
                "last_signal": None,
                "alerted_keys": [],
            },
        }

    # Support older tracker format.
    if isinstance(data, list):

        data = {
            "signals": data,
            "offset": 0,
            "meta": {},
        }

    if "signals" not in data:
        data["signals"] = []

    if "offset" not in data:
        data["offset"] = 0

    if "meta" not in data:
        data["meta"] = {}

    meta = data["meta"]

    if "last_scan" not in meta:
        meta["last_scan"] = None

    if "last_heartbeat" not in meta:
        meta["last_heartbeat"] = None

    if "last_signal" not in meta:
        meta["last_signal"] = None

    if "alerted_keys" not in meta:
        meta["alerted_keys"] = []

    return data


def save_tracker(data):
    data["signals"] = data.get("signals", [])[-MAX_TRACKER_ITEMS:]

    meta = data.setdefault("meta", {})

    alerted = meta.get("alerted_keys", [])

    if len(alerted) > MAX_ALERTED_KEYS:
        meta["alerted_keys"] = alerted[-MAX_ALERTED_KEYS:]

    temp_file = TRACKER_FILE + ".tmp"

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=2,
                ensure_ascii=False,
            )

        os.replace(temp_file, TRACKER_FILE)

        return True

    except Exception as exc:
        print("Tracker save error:", exc)

        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass

        return False


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def help_text():

    return (
        "<b>PRECISION SCANNER V3.4</b>\n\n"
        "<b>Commands:</b>\n"
        "/start - Start scanner bot\n"
        "/help - Show commands\n"
        "/stats - Show signal statistics\n"
        "/win SIGNAL-ID - Record WIN\n"
        "/loss SIGNAL-ID - Record LOSS\n"
        "/scan - Run one immediate scan\n\n"
        "<b>Strategy:</b>\n"
        "5M trend + 1M entry confirmation\n"
        f"Minimum qualified score: {MIN_SCORE}/100\n"
        f"Reference expiry: {REFERENCE_EXPIRY_MINUTES} minutes\n"
        "Coinbase spot proxy\n\n"
        "<i>The scanner does not execute trades.</i>"
    )


def find_signal(data, signal_id):

    for signal in data.get("signals", []):

        if signal.get("id") == signal_id:
            return signal

    return None


def process_result_command(data, signal_id, result):

    signal = find_signal(data, signal_id)

    if not signal:
        return (
            False,
            f"❌ Signal <code>{html.escape(signal_id)}</code> "
            f"was not found."
        )

    old_result = signal.get("result", "PENDING")

    if old_result in ("WIN", "LOSS"):

        return (
            False,
            f"⚠️ <code>{html.escape(signal_id)}</code> "
            f"is already recorded as <b>{old_result}</b>."
        )

    signal["result"] = result
    signal["result_time"] = format_utc()

    return (
        True,
        f"✅ <b>{html.escape(signal_id)}</b> recorded as "
        f"<b>{result}</b>."
    )


def process_commands(data):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False, False, []

    offset = int(data.get("offset", 0))

    result = tg(
        "getUpdates",
        {
            "offset": offset + 1,
            "limit": 100,
            "timeout": 1,
            "allowed_updates": json.dumps(["message"]),
        },
    )

    if not result or not result.get("ok"):
        return False, False, []

    updates = result.get("result", [])

    changed = False
    offset_changed = False
    immediate_scan_requested = False
    replies = []

    for update in updates:

        update_id = update.get("update_id")

        if update_id is not None:
            data["offset"] = update_id
            offset_changed = True

        message = update.get("message") or {}

        chat = message.get("chat") or {}

        chat_id = str(chat.get("id", ""))

        if chat_id != TELEGRAM_CHAT_ID:
            continue

        text = str(message.get("text", "")).strip()

        if not text:
            continue

        # /start
        if text.startswith("/start"):

            replies.append(help_text())

        # /help
        elif text.startswith("/help"):

            replies.append(help_text())

        # /stats
        elif text.startswith("/stats"):

            replies.append(stats_text(data))

        # /scan
        elif text.startswith("/scan"):

            immediate_scan_requested = True

            replies.append(
                "🔎 <b>Manual scan requested.</b>\n"
                "The scanner will run a fresh market scan."
            )

        # /win
        elif text.lower().startswith("/win"):

            parts = text.split(maxsplit=1)

            if len(parts) < 2:

                replies.append(
                    "Usage:\n"
                    "<code>/win SIGNAL-ID</code>"
                )

            else:

                signal_id = parts[1].strip()

                was_changed, reply = process_result_command(
                    data,
                    signal_id,
                    "WIN",
                )

                changed = changed or was_changed

                replies.append(reply)

        # /loss
        elif text.lower().startswith("/loss"):

            parts = text.split(maxsplit=1)

            if len(parts) < 2:

                replies.append(
                    "Usage:\n"
                    "<code>/loss SIGNAL-ID</code>"
                )

            else:

                signal_id = parts[1].strip()

                was_changed, reply = process_result_command(
                    data,
                    signal_id,
                    "LOSS",
                )

                changed = changed or was_changed

                replies.append(reply)

        else:

            replies.append(
                "Unknown command.\n\n" + help_text()
            )

    for reply in replies:
        send_message(reply)

    return changed, offset_changed, immediate_scan_requested


# ============================================================
# COINBASE REQUESTS
# ============================================================

def coinbase_get(path, params=None):

    url = COINBASE_BASE + path

    last_error = None

    for attempt in range(MAX_REQUEST_RETRIES):

        try:

            response = SESSION.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:

                wait_time = 2 ** attempt

                print(
                    f"Coinbase rate limit. "
                    f"Waiting {wait_time}s..."
                )

                time.sleep(wait_time)
                continue

            response.raise_for_status()

            return response.json()

        except Exception as exc:

            last_error = exc

            if attempt < MAX_REQUEST_RETRIES - 1:

                wait_time = 1.5 * (attempt + 1)

                print(
                    f"Coinbase request failed. "
                    f"Retrying in {wait_time:.1f}s..."
                )

                time.sleep(wait_time)

    raise RuntimeError(
        f"Coinbase request failed: {last_error}"
    )


# ============================================================
# MARKET LIST
# ============================================================

def get_markets():

    products = coinbase_get("/products")

    markets = {}

    for product in products:

        try:

            status = str(
                product.get("status", "")
            ).lower()

            if status not in ("online", "active", ""):
                continue

            base = product.get("base_currency")

            quote = product.get("quote_currency")

            product_id = product.get("id")

            if not base or not quote or not product_id:
                continue

            if base not in ASSET_BASES:
                continue

            if quote not in ("USD", "USDC"):
                continue

            # Prefer USD when available.
            if base not in markets:
                markets[base] = product_id

            elif quote == "USD":
                markets[base] = product_id

        except Exception:
            continue

    return markets


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(product_id, granularity, limit=220):

    raw = coinbase_get(
        f"/products/{product_id}/candles",
        {
            "granularity": granularity,
        },
    )

    candles = []

    for row in raw:

        if len(row) < 6:
            continue

        try:

            candle = {
                "time": int(row[0]),
                "low": float(row[1]),
                "high": float(row[2]),
                "open": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }

            candles.append(candle)

        except Exception:
            continue

    candles.sort(key=lambda x: x["time"])

    # Remove currently incomplete candle.
    now = int(time.time())

    completed = []

    for candle in candles:

        candle_start = candle["time"]

        if candle_start + granularity <= now:
            completed.append(candle)

    return completed[-limit:]


# ============================================================
# BASIC INDICATORS
# ============================================================

def ema(values, period):

    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    seed = sum(values[:period]) / period

    result[period - 1] = seed

    multiplier = 2 / (period + 1)

    previous = seed

    for i in range(period, len(values)):

        current = (
            values[i] - previous
        ) * multiplier + previous

        result[i] = current

        previous = current

    return result


def rsi(values, period=14):

    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result[period] = 100
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(values)):

        gain = gains[i - 1]
        loss = losses[i - 1]

        avg_gain = (
            (avg_gain * (period - 1)) + gain
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + loss
        ) / period

        if avg_loss == 0:
            result[i] = 100
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


def true_range(candles):

    result = [None] * len(candles)

    for i, candle in enumerate(candles):

        if i == 0:

            result[i] = (
                candle["high"] - candle["low"]
            )

            continue

        previous_close = candles[i - 1]["close"]

        result[i] = max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close),
        )

    return result


def atr(candles, period=14):

    tr = true_range(candles)

    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    initial = [
        x for x in tr[1:period + 1]
        if x is not None
    ]

    if len(initial) < period:
        return result

    current = sum(initial) / period

    result[period] = current

    for i in range(period + 1, len(candles)):

        value = tr[i]

        if value is None:
            continue

        current = (
            (current * (period - 1)) + value
        ) / period

        result[i] = current

    return result


def adx_dmi(candles, period=14):

    n = len(candles)

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr_values = [0.0] * n

    for i in range(1, n):

        up_move = (
            candles[i]["high"]
            - candles[i - 1]["high"]
        )

        down_move = (
            candles[i - 1]["low"]
            - candles[i]["low"]
        )

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

        tr_values[i] = max(
            candles[i]["high"] - candles[i]["low"],
            abs(
                candles[i]["high"]
                - candles[i - 1]["close"]
            ),
            abs(
                candles[i]["low"]
                - candles[i - 1]["close"]
            ),
        )

    adx = [None] * n
    plus_di = [None] * n
    minus_di = [None] * n

    if n <= period * 2:
        return adx, plus_di, minus_di

    atr_value = sum(
        tr_values[1:period + 1]
    )

    plus_value = sum(
        plus_dm[1:period + 1]
    )

    minus_value = sum(
        minus_dm[1:period + 1]
    )

    dx_values = []

    for i in range(period + 1, n):

        atr_value = (
            atr_value
            - (atr_value / period)
            + tr_values[i]
        )

        plus_value = (
            plus_value
            - (plus_value / period)
            + plus_dm[i]
        )

        minus_value = (
            minus_value
            - (minus_value / period)
            + minus_dm[i]
        )

        if atr_value == 0:
            continue

        pdi = (
            100 * plus_value / atr_value
        )

        mdi = (
            100 * minus_value / atr_value
        )

        plus_di[i] = pdi
        minus_di[i] = mdi

        denominator = pdi + mdi

        if denominator == 0:
            dx = 0
        else:
            dx = (
                100
                * abs(pdi - mdi)
                / denominator
            )

        dx_values.append((i, dx))

    if len(dx_values) < period:
        return adx, plus_di, minus_di

    initial = dx_values[:period]

    current_adx = (
        sum(x[1] for x in initial)
        / period
    )

    adx[initial[-1][0]] = current_adx

    for i, dx in dx_values[period:]:

        current_adx = (
            (current_adx * (period - 1))
            + dx
        ) / period

        adx[i] = current_adx

    return adx, plus_di, minus_di


def macd(values):

    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    line = [None] * len(values)

    for i in range(len(values)):

        if ema12[i] is not None and ema26[i] is not None:

            line[i] = (
                ema12[i] - ema26[i]
            )

    usable = [
        x for x in line
        if x is not None
    ]

    signal_values = ema(
        usable,
        9,
    )

    signal = [None] * len(values)

    index = 0

    for i in range(len(values)):

        if line[i] is not None:

            if index < len(signal_values):
                signal[i] = signal_values[index]

            index += 1

    histogram = [None] * len(values)

    for i in range(len(values)):

        if line[i] is not None and signal[i] is not None:

            histogram[i] = (
                line[i] - signal[i]
            )

    return line, signal, histogram


# ============================================================
# PRICE ACTION
# ============================================================

def direction(candle):

    if candle["close"] > candle["open"]:
        return "BULL"

    if candle["close"] < candle["open"]:
        return "BEAR"

    return "NEUTRAL"


def body_ratio(candle):

    total_range = (
        candle["high"] - candle["low"]
    )

    if total_range <= 0:
        return 0

    body = abs(
        candle["close"] - candle["open"]
    )

    return body / total_range


def bullish_confirmation(candle):

    return (
        candle["close"] > candle["open"]
        and body_ratio(candle) >= 0.45
    )


def bearish_confirmation(candle):

    return (
        candle["close"] < candle["open"]
        and body_ratio(candle) >= 0.45
    )


def structure(candles, lookback=8):

    if len(candles) < lookback:
        return "NEUTRAL"

    recent = candles[-lookback:]

    highs = [
        x["high"]
        for x in recent
    ]

    lows = [
        x["low"]
        for x in recent
    ]

    midpoint = (
        recent[0]["close"]
        + recent[-1]["close"]
    ) / 2

    early_high = max(
        x["high"]
        for x in recent[:lookback // 2]
    )

    late_high = max(
        x["high"]
        for x in recent[lookback // 2:]
    )

    early_low = min(
        x["low"]
        for x in recent[:lookback // 2]
    )

    late_low = min(
        x["low"]
        for x in recent[lookback // 2:]
    )

    if (
        late_high > early_high
        and late_low > early_low
        and midpoint > sum(lows) / len(lows)
    ):
        return "BULLISH"

    if (
        late_high < early_high
        and late_low < early_low
        and midpoint < sum(highs) / len(highs)
    ):
        return "BEARISH"

    return "RANGE"


def levels(candles, lookback=30):

    recent = candles[-lookback:]

    support = min(
        x["low"]
        for x in recent
    )

    resistance = max(
        x["high"]
        for x in recent
    )

    return support, resistance


# ============================================================
# VOLATILITY / MARKET CONDITION
# ============================================================

def recent_range(candles, lookback=20):

    if len(candles) < lookback:
        return 0

    recent = candles[-lookback:]

    high = max(
        x["high"]
        for x in recent
    )

    low = min(
        x["low"]
        for x in recent
    )

    return high - low


def volatility_state(candles, atr_values):

    if not candles or not atr_values:
        return "UNKNOWN"

    current_atr = atr_values[-1]

    if current_atr is None:
        return "UNKNOWN"

    current_price = candles[-1]["close"]

    if current_price == 0:
        return "UNKNOWN"

    atr_pct = (
        current_atr
        / current_price
        * 100
    )

    if atr_pct < 0.03:
        return "LOW"

    if atr_pct < 0.15:
        return "NORMAL"

    if atr_pct < 0.35:
        return "HIGH"

    return "EXTREME"


def detect_chop(candles, ema_fast, ema_slow):

    if len(candles) < 20:
        return False

    recent_fast = ema_fast[-20:]
    recent_slow = ema_slow[-20:]

    crosses = 0

    previous_diff = None

    for fast_value, slow_value in zip(
        recent_fast,
        recent_slow,
    ):

        if fast_value is None or slow_value is None:
            continue

        diff = fast_value - slow_value

        if previous_diff is not None:

            if (
                diff > 0 and previous_diff < 0
            ) or (
                diff < 0 and previous_diff > 0
            ):
                crosses += 1

        previous_diff = diff

    return crosses >= 5


def proximity_to_level(
    price,
    support,
    resistance,
):

    if resistance > price:
        distance_up = resistance - price
    else:
        distance_up = float("inf")

    if price > support:
        distance_down = price - support
    else:
        distance_down = float("inf")

    return distance_up, distance_down


def candle_strength(candle, signal):

    ratio = body_ratio(candle)

    if signal == "CALL":

        if candle["close"] <= candle["open"]:
            return 0

        if ratio >= 0.75:
            return 5

        if ratio >= 0.60:
            return 4

        if ratio >= 0.45:
            return 3

        return 1

    if signal == "PUT":

        if candle["close"] >= candle["open"]:
            return 0

        if ratio >= 0.75:
            return 5

        if ratio >= 0.60:
            return 4

        if ratio >= 0.45:
            return 3

        return 1

    return 0


# ============================================================
# ANALYSIS HELPERS
# ============================================================

def extension_score(
    price,
    ema20,
    atr_value,
):

    if atr_value is None or atr_value <= 0:
        return 0, 999

    extension = abs(
        price - ema20
    ) / atr_value

    if extension <= 1.2:
        return 5, extension

    if extension <= 1.6:
        return 4, extension

    if extension <= 2.0:
        return 2, extension

    if extension <= 2.2:
        return 1, extension

    return 0, extension


def room_score(
    price,
    signal,
    support,
    resistance,
    atr_value,
):

    if atr_value is None or atr_value <= 0:
        return 0, 0

    if signal == "CALL":

        room = resistance - price

    else:

        room = price - support

    room_atr = room / atr_value

    if room_atr >= 0.9:
        return 5, room_atr

    if room_atr >= 0.7:
        return 4, room_atr

    if room_atr >= 0.5:
        return 3, room_atr

    if room_atr >= 0.35:
        return 2, room_atr

    if room_atr >= 0.25:
        return 1, room_atr

    return 0, room_atr


def pullback_quality(
    candles,
    signal,
    ema20,
    ema50,
):

    if len(candles) < 6:
        return 0

    recent = candles[-6:]

    score = 0

    if signal == "CALL":

        touched = any(
            x["low"] <= ema20[-1]
            for x in recent
            if ema20[-1] is not None
        )

        recovered = (
            recent[-1]["close"]
            > recent[-2]["close"]
        )

        if touched:
            score += 5

        if recovered:
            score += 3

        if (
            ema20[-1] is not None
            and ema50[-1] is not None
            and ema20[-1] > ema50[-1]
        ):
            score += 2

    elif signal == "PUT":

        touched = any(
            x["high"] >= ema20[-1]
            for x in recent
            if ema20[-1] is not None
        )

        recovered = (
            recent[-1]["close"]
            < recent[-2]["close"]
        )

        if touched:
            score += 5

        if recovered:
            score += 3

        if (
            ema20[-1] is not None
            and ema50[-1] is not None
            and ema20[-1] < ema50[-1]
        ):
            score += 2

    return min(score, 10)


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_asset(symbol, product_id):

    candles5 = get_candles(
        product_id,
        MAIN_SECONDS,
        220,
    )

    candles1 = get_candles(
        product_id,
        ENTRY_SECONDS,
        220,
    )

    if len(candles5) < 100:
        raise RuntimeError(
            f"Insufficient 5M candles: {len(candles5)}"
        )

    if len(candles1) < 100:
        raise RuntimeError(
            f"Insufficient 1M candles: {len(candles1)}"
        )

    closes5 = [
        x["close"]
        for x in candles5
    ]

    closes1 = [
        x["close"]
        for x in candles1
    ]

    # --------------------------------------------------------
    # 5M INDICATORS
    # --------------------------------------------------------

    ema20_5 = ema(closes5, 20)
    ema50_5 = ema(closes5, 50)

    rsi5 = rsi(closes5, 14)

    atr5 = atr(candles5, 14)

    adx5, plus_di5, minus_di5 = adx_dmi(
        candles5,
        14,
    )

    macd5, macd_signal5, macd_hist5 = macd(
        closes5
    )

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    ema9_1 = ema(closes1, 9)
    ema21_1 = ema(closes1, 21)

    # --------------------------------------------------------
    # LATEST DATA
    # --------------------------------------------------------

    i5 = len(candles5) - 1
    i1 = len(candles1) - 1

    price = candles1[i1]["close"]

    candle1 = candles1[i1]

    candle5 = candles5[i5]

    e20 = ema20_5[i5]
    e50 = ema50_5[i5]

    rsi_value = rsi5[i5]

    atr_value = atr5[i5]

    adx_value = adx5[i5]

    plus_di = plus_di5[i5]
    minus_di = minus_di5[i5]

    macd_value = macd5[i5]
    macd_signal_value = macd_signal5[i5]
    macd_hist_value = macd_hist5[i5]

    entry_fast = ema9_1[i1]
    entry_slow = ema21_1[i1]

    if any(
        x is None
        for x in [
            e20,
            e50,
            rsi_value,
            atr_value,
            adx_value,
            plus_di,
            minus_di,
            macd_value,
            macd_signal_value,
            macd_hist_value,
            entry_fast,
            entry_slow,
        ]
    ):
        raise RuntimeError(
            "Indicator calculation incomplete."
        )

    # --------------------------------------------------------
    # 5M MAJOR TREND
    # --------------------------------------------------------

    bullish_major = (
        price > e50
        and e20 > e50
    )

    bearish_major = (
        price < e50
        and e20 < e50
    )

    if bullish_major:
        major_trend = "BULLISH"

    elif bearish_major:
        major_trend = "BEARISH"

    else:
        major_trend = "NEUTRAL"

    # --------------------------------------------------------
    # 1M ENTRY TREND
    # --------------------------------------------------------

    if entry_fast > entry_slow:
        entry_trend = "BULLISH"

    elif entry_fast < entry_slow:
        entry_trend = "BEARISH"

    else:
        entry_trend = "NEUTRAL"

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    structure5 = structure(candles5)

    # --------------------------------------------------------
    # DIRECTION
    # --------------------------------------------------------

    bullish_votes = 0
    bearish_votes = 0

    if bullish_major:
        bullish_votes += 3

    if bearish_major:
        bearish_votes += 3

    if entry_trend == "BULLISH":
        bullish_votes += 2

    if entry_trend == "BEARISH":
        bearish_votes += 2

    if structure5 == "BULLISH":
        bullish_votes += 2

    if structure5 == "BEARISH":
        bearish_votes += 2

    if plus_di > minus_di:
        bullish_votes += 1

    if minus_di > plus_di:
        bearish_votes += 1

    if macd_hist_value > 0:
        bullish_votes += 1

    if macd_hist_value < 0:
        bearish_votes += 1

    if candle1["close"] > candle1["open"]:
        bullish_votes += 1

    if candle1["close"] < candle1["open"]:
        bearish_votes += 1

    if bullish_votes > bearish_votes:
        signal = "CALL"

    elif bearish_votes > bullish_votes:
        signal = "PUT"

    else:
        signal = "NO TRADE"

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE
    # --------------------------------------------------------

    support, resistance = levels(
        candles5,
        30,
    )

    room_points, room_atr = room_score(
        price,
        signal,
        support,
        resistance,
        atr_value,
    )

    extension_points, extension = extension_score(
        price,
        e20,
        atr_value,
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 0

    score_components = {}

    # Trend - 20
    trend_points = 0

    if signal == "CALL" and bullish_major:
        trend_points = 20

    elif signal == "PUT" and bearish_major:
        trend_points = 20

    elif signal in ("CALL", "PUT"):
        trend_points = 8

    score += trend_points

    score_components["trend"] = trend_points

    # Structure - 10
    structure_points = 0

    if (
        signal == "CALL"
        and structure5 == "BULLISH"
    ):
        structure_points = 10

    elif (
        signal == "PUT"
        and structure5 == "BEARISH"
    ):
        structure_points = 10

    elif structure5 != "RANGE":
        structure_points = 4

    score += structure_points

    score_components["structure"] = structure_points

    # DMI / ADX - 10
    dmi_points = 0

    if adx_value >= 25:
        dmi_points = 5

    elif adx_value >= 20:
        dmi_points = 4

    elif adx_value >= 17:
        dmi_points = 3

    elif adx_value >= 15:
        dmi_points = 2

    if (
        signal == "CALL"
        and plus_di > minus_di
    ):
        dmi_points += 5

    elif (
        signal == "PUT"
        and minus_di > plus_di
    ):
        dmi_points += 5

    score += min(dmi_points, 10)

    score_components["dmi_adx"] = min(
        dmi_points,
        10,
    )

    # 5M MACD - 10
    macd_points = 0

    if signal == "CALL":

        if macd_value > macd_signal_value:
            macd_points += 6

        if macd_hist_value > 0:
            macd_points += 4

    elif signal == "PUT":

        if macd_value < macd_signal_value:
            macd_points += 6

        if macd_hist_value < 0:
            macd_points += 4

    score += macd_points

    score_components["macd"] = macd_points

    # RSI - 10
    rsi_points = 0

    if signal == "CALL":

        if 50 <= rsi_value <= 65:
            rsi_points = 10

        elif 45 <= rsi_value < 50:
            rsi_points = 7

        elif 65 < rsi_value <= 70:
            rsi_points = 6

        elif 40 <= rsi_value < 45:
            rsi_points = 3

    elif signal == "PUT":

        if 35 <= rsi_value <= 50:
            rsi_points = 10

        elif 50 < rsi_value <= 55:
            rsi_points = 7

        elif 30 <= rsi_value < 35:
            rsi_points = 6

        elif 55 < rsi_value <= 60:
            rsi_points = 3

    score += rsi_points

    score_components["rsi"] = rsi_points

    # 1M Entry - 15
    entry_points = 0

    if signal == "CALL":

        if entry_fast > entry_slow:
            entry_points += 8

        if bullish_confirmation(candle1):
            entry_points += 7

    elif signal == "PUT":

        if entry_fast < entry_slow:
            entry_points += 8

        if bearish_confirmation(candle1):
            entry_points += 7

    score += entry_points

    score_components["entry"] = entry_points

    # Pullback - 10
    pullback_points = pullback_quality(
        candles1,
        signal,
        ema9_1,
        ema21_1,
    )

    score += pullback_points

    score_components["pullback"] = pullback_points

    # Candle - 5
    candle_points = candle_strength(
        candle1,
        signal,
    )

    score += candle_points

    score_components["candle"] = candle_points

    # Room - 5
    score += room_points

    score_components["room"] = room_points

    # Extension - 5
    score += extension_points

    score_components["extension"] = extension_points

    # --------------------------------------------------------
    # MARKET CONDITIONS
    # --------------------------------------------------------

    chop = detect_chop(
        candles5,
        ema20_5,
        ema50_5,
    )

    volatility = volatility_state(
        candles5,
        atr5,
    )

    distance_up, distance_down = proximity_to_level(
        price,
        support,
        resistance,
    )

    diagnostics = []

    blockers = []

    # --------------------------------------------------------
    # CRITICAL BLOCKERS
    # --------------------------------------------------------

    if signal == "CALL" and not bullish_major:

        blockers.append(
            "5M bullish trend not confirmed"
        )

    if signal == "PUT" and not bearish_major:

        blockers.append(
            "5M bearish trend not confirmed"
        )

    if chop:

        blockers.append(
            "market appears choppy"
        )

    if volatility == "EXTREME":

        blockers.append(
            "extreme volatility"
        )

    if room_points == 0:

        blockers.append(
            "insufficient room"
        )

    if extension > 2.2:

        blockers.append(
            "price too extended from EMA20"
        )

    # Too close to opposing level.
    if signal == "CALL":

        if (
            atr_value > 0
            and distance_up < atr_value * 0.25
        ):
            blockers.append(
                "too close to resistance"
            )

    elif signal == "PUT":

        if (
            atr_value > 0
            and distance_down < atr_value * 0.25
        ):
            blockers.append(
                "too close to support"
            )

    # Opposite strong structure.
    if (
        signal == "CALL"
        and structure5 == "BEARISH"
    ):
        blockers.append(
            "opposite strong structure"
        )

    if (
        signal == "PUT"
        and structure5 == "BULLISH"
    ):
        blockers.append(
            "opposite strong structure"
        )

    # ADX filter.
    if adx_value < 15:

        blockers.append(
            "ADX below 15"
        )

    ema_gap_pct = (
        abs(e20 - e50)
        / price
        * 100
    ) if price else 0

    if (
        adx_value < 17
        and ema_gap_pct < 0.10
    ):
        blockers.append(
            "weak ADX + compressed EMA structure"
        )

    # --------------------------------------------------------
    # ADD DIAGNOSTICS
    # --------------------------------------------------------

    if major_trend == "NEUTRAL":

        diagnostics.append(
            "5M trend neutral"
        )

    if entry_trend == "NEUTRAL":

        diagnostics.append(
            "1M entry neutral"
        )

    if structure5 == "RANGE":

        diagnostics.append(
            "5M structure ranging"
        )

    if adx_value < 20:

        diagnostics.append(
            f"ADX only {adx_value:.1f}"
        )

    if extension > 1.6:

        diagnostics.append(
            f"extension {extension:.2f} ATR"
        )

    if room_atr < 0.5:

        diagnostics.append(
            f"room only {room_atr:.2f} ATR"
        )

    if rsi_value > 70:

        diagnostics.append(
            "RSI overbought"
        )

    if rsi_value < 30:

        diagnostics.append(
            "RSI oversold"
        )

    if volatility == "LOW":

        diagnostics.append(
            "low volatility"
        )

    # --------------------------------------------------------
    # FINAL QUALIFICATION
    # --------------------------------------------------------

    qualified = False
    borderline = False

    if not blockers:

        if (
            signal in ("CALL", "PUT")
            and score >= MIN_SCORE
        ):

            # Direction dominance.
            if signal == "CALL":

                dominance = (
                    bullish_votes
                    - bearish_votes
                )

            else:

                dominance = (
                    bearish_votes
                    - bullish_votes
                )

            if dominance >= 2:

                qualified = True

            else:

                diagnostics.append(
                    "directional dominance too weak"
                )

        elif (
            signal in ("CALL", "PUT")
            and score >= BORDERLINE_SCORE
        ):

            borderline = True

    # If not qualified, preserve signal internally
    # only as diagnostic information.
    final_signal = signal if qualified else "NO TRADE"

    status = "QUALIFIED"

    if qualified:
        status = "QUALIFIED"

    elif borderline:
        status = "BORDERLINE"

    else:
        status = "REJECTED"

    # --------------------------------------------------------
    # SIGNAL ID BASED ON COMPLETED 1M CANDLE
    # --------------------------------------------------------

    entry_candle_time = candles1[-1]["time"]

    if qualified:

        signal_dt = datetime.fromtimestamp(
            entry_candle_time,
            tz=timezone.utc,
        )

        signal_id = (
            f"{symbol}-{signal}-"
            f"{signal_dt.strftime('%H%M%S')}"
        )

    else:

        signal_id = None

    return {
        "symbol": symbol,
        "product": product_id,

        "signal": final_signal,
        "raw_direction": signal,

        "status": status,

        "score": int(score),

        "major_trend": major_trend,
        "entry_trend": entry_trend,
        "structure": structure5,

        "price": price,

        "rsi": rsi_value,
        "adx": adx_value,

        "plus_di": plus_di,
        "minus_di": minus_di,

        "macd": macd_value,
        "macd_signal": macd_signal_value,
        "macd_histogram": macd_hist_value,

        "atr": atr_value,

        "extension_atr": extension,
        "room_atr": room_atr,

        "support": support,
        "resistance": resistance,

        "volatility": volatility,
        "chop": chop,

        "ema_gap_pct": ema_gap_pct,

        "bullish_votes": bullish_votes,
        "bearish_votes": bearish_votes,

        "score_components": score_components,

        "blockers": blockers,
        "diagnostics": diagnostics,

        "signal_id": signal_id,

        "entry_candle_time": entry_candle_time,
        "main_candle_time": candles5[-1]["time"],

        "scan_time": utc_timestamp(),
    }


# ============================================================
# SIGNAL TRACKING
# ============================================================

def signal_dedupe_key(result):

    return (
        f"{result['symbol']}|"
        f"{result['signal']}|"
        f"{result['entry_candle_time']}"
    )


def add_new_signal(data, result):

    if result.get("signal") == "NO TRADE":
        return False

    signal_id = result.get("signal_id")

    if not signal_id:
        return False

    dedupe_key = signal_dedupe_key(result)

    # Existing signal?
    for existing in data.get("signals", []):

        if existing.get("dedupe_key") == dedupe_key:
            return False

        if existing.get("id") == signal_id:
            return False

    record = {
        "id": signal_id,

        "dedupe_key": dedupe_key,

        "symbol": result["symbol"],
        "product": result["product"],

        "signal": result["signal"],

        "score": result["score"],

        "status": result["status"],

        "result": "PENDING",

        "created_at": format_utc(
            result["scan_time"]
        ),

        "entry_candle_time": result[
            "entry_candle_time"
        ],

        "main_candle_time": result[
            "main_candle_time"
        ],

        "price": result["price"],

        "rsi": result["rsi"],
        "adx": result["adx"],

        "plus_di": result["plus_di"],
        "minus_di": result["minus_di"],

        "macd": result["macd"],
        "macd_signal": result["macd_signal"],
        "macd_histogram": result[
            "macd_histogram"
        ],

        "atr": result["atr"],

        "extension_atr": result[
            "extension_atr"
        ],

        "room_atr": result["room_atr"],

        "support": result["support"],
        "resistance": result["resistance"],

        "major_trend": result[
            "major_trend"
        ],

        "entry_trend": result[
            "entry_trend"
        ],

        "structure": result[
            "structure"
        ],

        "volatility": result[
            "volatility"
        ],

        "bullish_votes": result[
            "bullish_votes"
        ],

        "bearish_votes": result[
            "bearish_votes"
        ],

        "score_components": result[
            "score_components"
        ],

        "alert_sent": False,
    }

    data.setdefault(
        "signals",
        []
    ).append(record)

    return True


def mark_alert_sent(data, signal_id):

    for signal in data.get("signals", []):

        if signal.get("id") == signal_id:

            signal["alert_sent"] = True
            signal["alert_sent_at"] = format_utc()

            return True

    return False


def was_alerted(data, dedupe_key):

    meta = data.setdefault(
        "meta",
        {}
    )

    return dedupe_key in meta.get(
        "alerted_keys",
        []
    )


def mark_alerted(data, dedupe_key):

    meta = data.setdefault(
        "meta",
        {}
    )

    alerted = meta.setdefault(
        "alerted_keys",
        []
    )

    if dedupe_key not in alerted:

        alerted.append(dedupe_key)

    if len(alerted) > MAX_ALERTED_KEYS:

        meta["alerted_keys"] = alerted[
            -MAX_ALERTED_KEYS:
        ]


# ============================================================
# ALERT COOLDOWN
# ============================================================

def get_last_asset_alert(data, symbol):

    latest = None

    for signal in data.get("signals", []):

        if signal.get("symbol") != symbol:
            continue

        if not signal.get("alert_sent"):
            continue

        created = signal.get("entry_candle_time")

        if created is None:
            continue

        if latest is None or created > latest:
            latest = created

    return latest


def is_asset_on_cooldown(
    data,
    symbol,
    current_candle_time,
):

    last_alert = get_last_asset_alert(
        data,
        symbol,
    )

    if last_alert is None:
        return False

    elapsed = (
        current_candle_time
        - last_alert
    )

    return elapsed < ALERT_COOLDOWN_SECONDS


# ============================================================
# TELEGRAM SIGNAL FORMAT
# ============================================================

def build_signal_alert(result):

    signal = result["signal"]

    emoji = "🟢" if signal == "CALL" else "🔴"

    direction_text = (
        "CALL / UP"
        if signal == "CALL"
        else "PUT / DOWN"
    )

    components = result[
        "score_components"
    ]

    return (
        f"{emoji} <b>NEW QUALIFIED SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(result['symbol'])}</b> "
        f"<b>{direction_text}</b>\n\n"

        f"🎯 <b>Score:</b> "
        f"{result['score']}/100\n"
        f"⏱ <b>Reference expiry:</b> "
        f"{REFERENCE_EXPIRY_MINUTES} minutes\n"
        f"💰 <b>Price:</b> "
        f"{result['price']:.8f}\n\n"

        f"📊 <b>5M Trend:</b> "
        f"{result['major_trend']}\n"
        f"📈 <b>1M Entry:</b> "
        f"{result['entry_trend']}\n"
        f"🏗 <b>Structure:</b> "
        f"{result['structure']}\n"
        f"📐 <b>ADX:</b> "
        f"{result['adx']:.1f}\n"
        f"📉 <b>RSI:</b> "
        f"{result['rsi']:.1f}\n\n"

        f"<b>Score Breakdown</b>\n"
        f"Trend: {components.get('trend', 0)}/20\n"
        f"Structure: {components.get('structure', 0)}/10\n"
        f"ADX/DMI: {components.get('dmi_adx', 0)}/10\n"
        f"MACD: {components.get('macd', 0)}/10\n"
        f"RSI: {components.get('rsi', 0)}/10\n"
        f"1M Entry: {components.get('entry', 0)}/15\n"
        f"Pullback: {components.get('pullback', 0)}/10\n"
        f"Candle: {components.get('candle', 0)}/5\n"
        f"Room: {components.get('room', 0)}/5\n"
        f"Extension: {components.get('extension', 0)}/5\n\n"

        f"🆔 <code>{html.escape(result['signal_id'])}</code>\n"
        f"🕐 Candle: "
        f"{format_candle_time(result['entry_candle_time'])}\n\n"

        f"⚠️ <i>Coinbase spot proxy. "
        f"Signal is not a guaranteed outcome.</i>"
    )


# ============================================================
# HEARTBEAT
# ============================================================

def build_heartbeat(
    results,
    errors,
    data,
    scan_number,
):

    qualified = [
        x for x in results
        if x.get("signal") != "NO TRADE"
    ]

    borderline = [
        x for x in results
        if x.get("status") == "BORDERLINE"
    ]

    rejected = [
        x for x in results
        if x.get("signal") == "NO TRADE"
    ]

    completed = completed_signals(data)

    if completed:

        wins = sum(
            1
            for x in completed
            if x.get("result") == "WIN"
        )

        losses = sum(
            1
            for x in completed
            if x.get("result") == "LOSS"
        )

        total = wins + losses

        if total:
            wr = (
                wins / total
                * 100
            )
        else:
            wr = 0

        stats_line = (
            f"Wins: {wins} | "
            f"Losses: {losses} | "
            f"Win rate: {wr:.1f}%"
        )

    else:

        stats_line = (
            "No completed tracked results yet."
        )

    top = []

    reason_counts = {}

    for result in results:

        for reason in result.get(
            "blockers",
            []
        ):

            reason_counts[reason] = (
                reason_counts.get(reason, 0)
                + 1
            )

    sorted_reasons = sorted(
        reason_counts.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    for reason, count in sorted_reasons[:3]:

        top.append(
            f"• {reason}: {count}"
        )

    top_text = "\n".join(top)

    if not top_text:
        top_text = "• No major blockers."

    return (
        f"🟦 <b>PRECISION SCANNER {VERSION}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💓 <b>Scanner is running</b>\n\n"

        f"🕐 {format_utc()}\n"
        f"🔄 Scan #{scan_number}\n"
        f"📊 Assets analyzed: {len(results)}\n"
        f"🟢 Qualified: {len(qualified)}\n"
        f"🟡 Borderline: {len(borderline)}\n"
        f"⚪ Rejected: {len(rejected)}\n"
        f"⚠️ Data errors: {len(errors)}\n\n"

        f"<b>Tracked Results</b>\n"
        f"{stats_line}\n\n"

        f"<b>Common Rejections</b>\n"
        f"{top_text}\n\n"

        f"Next scan: approximately "
        f"{SCAN_INTERVAL_SECONDS} seconds."
    )


# ============================================================
# STATISTICS
# ============================================================

def completed_signals(data):

    return [
        x
        for x in data.get("signals", [])
        if x.get("result") in ("WIN", "LOSS")
    ]


def winrate(data):

    completed = completed_signals(data)

    if not completed:
        return 0

    wins = sum(
        1
        for x in completed
        if x.get("result") == "WIN"
    )

    return (
        wins
        / len(completed)
        * 100
    )


def stats_text(data):

    signals = data.get(
        "signals",
        []
    )

    completed = completed_signals(data)

    wins = sum(
        1
        for x in completed
        if x.get("result") == "WIN"
    )

    losses = sum(
        1
        for x in completed
        if x.get("result") == "LOSS"
    )

    pending = sum(
        1
        for x in signals
        if x.get("result") == "PENDING"
    )

    rate = winrate(data)

    qualified = sum(
        1
        for x in signals
        if x.get("status") == "QUALIFIED"
    )

    return (
        f"📊 <b>PRECISION SCANNER STATS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"Total tracked: <b>{len(signals)}</b>\n"
        f"Qualified signals: <b>{qualified}</b>\n"
        f"Pending: <b>{pending}</b>\n"
        f"Completed: <b>{len(completed)}</b>\n\n"

        f"🟢 Wins: <b>{wins}</b>\n"
        f"🔴 Losses: <b>{losses}</b>\n"
        f"📈 Recorded win rate: <b>{rate:.1f}%</b>\n\n"

        f"<i>Win rate is historical tracker data, "
        f"not a prediction of future results.</i>"
    )


# ============================================================
# REJECTION SUMMARY
# ============================================================

def rejection_summary(results):

    counts = {}

    for result in results:

        reasons = result.get(
            "blockers",
            []
        )

        if not reasons:

            reasons = result.get(
                "diagnostics",
                []
            )

        for reason in reasons:

            counts[reason] = (
                counts.get(reason, 0)
                + 1
            )

    return sorted(
        counts.items(),
        key=lambda x: x[1],
        reverse=True,
    )


# ============================================================
# RUN ONE SCAN
# ============================================================

def run_scan_cycle(
    data,
    markets,
    scan_number,
):

    results = []
    errors = []

    print(
        "\n========================================"
    )

    print(
        f"SCAN #{scan_number} "
        f"{format_utc()}"
    )

    print(
        "========================================"
    )

    for symbol in ASSET_BASES:

        product = markets.get(symbol)

        if not product:

            error = (
                f"{symbol}: Coinbase market unavailable"
            )

            errors.append(error)

            print(error)

            continue

        print(
            f"Analyzing {symbol} ({product})..."
        )

        try:

            result = analyze_asset(
                symbol,
                product,
            )

            results.append(result)

            print(
                f"{symbol}: "
                f"{result['signal']} "
                f"{result['score']}/100 "
                f"{result['status']}"
            )

            if result.get("diagnostics"):

                print(
                    f"  Diagnostics: "
                    f"{' | '.join(result['diagnostics'][:4])}"
                )

            if result.get("blockers"):

                print(
                    f"  Blockers: "
                    f"{' | '.join(result['blockers'][:4])}"
                )

        except Exception as exc:

            error = (
                f"{symbol}: {str(exc)}"
            )

            errors.append(error)

            print(
                f"{symbol}: ERROR {exc}"
            )

        # Small delay helps avoid hammering the API.
        time.sleep(0.15)

    # --------------------------------------------------------
    # TRACKER / ALERTS
    # --------------------------------------------------------

    tracker_changed = False

    new_alerts = []

    for result in results:

        if result.get("signal") == "NO TRADE":
            continue

        dedupe_key = signal_dedupe_key(
            result
        )

        # Already alerted exact candle/setup.
        if was_alerted(
            data,
            dedupe_key,
        ):
            continue

        # Five-minute per-asset cooldown.
        if is_asset_on_cooldown(
            data,
            result["symbol"],
            result["entry_candle_time"],
        ):

            print(
                f"{result['symbol']}: "
                f"qualified but on alert cooldown."
            )

            continue

        was_added = add_new_signal(
            data,
            result,
        )

        if was_added:

            tracker_changed = True

            new_alerts.append(result)

    # --------------------------------------------------------
    # SEND NEW SIGNALS
    # --------------------------------------------------------

    for result in new_alerts:

        message = build_signal_alert(
            result
        )

        sent = send_message(
            message
        )

        if sent:

            mark_alerted(
                data,
                signal_dedupe_key(result),
            )

            mark_alert_sent(
                data,
                result["signal_id"],
            )

            data["meta"][
                "last_signal"
            ] = result["signal_id"]

            tracker_changed = True

            print(
                f"Telegram alert sent: "
                f"{result['signal_id']}"
            )

        else:

            print(
                f"Telegram alert FAILED: "
                f"{result['signal_id']}"
            )

    # --------------------------------------------------------
    # UPDATE LAST SCAN
    # --------------------------------------------------------

    data["meta"][
        "last_scan"
    ] = format_utc()

    # Do not mark this as tracker changed solely because
    # the timestamp changed. Otherwise GitHub would receive
    # a commit every minute.
    #
    # --------------------------------------------------------

    return (
        results,
        errors,
        tracker_changed,
        len(new_alerts),
    )


# ============================================================
# GITHUB TRACKER COMMIT
# ============================================================

def git_command(args):

    try:

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=60,
        )

        print(
            "Git:",
            " ".join(args),
            "=>",
            result.returncode,
        )

        if result.stdout:
            print(
                result.stdout[-1000:]
            )

        if result.stderr:
            print(
                result.stderr[-1000:]
            )

        return result.returncode == 0

    except Exception as exc:

        print(
            "Git command error:",
            exc,
        )

        return False


def commit_tracker():

    if not os.path.exists(TRACKER_FILE):

        print(
            "No tracker file to commit."
        )

        return False

    # Configure identity.
    git_command(
        [
            "git",
            "config",
            "user.email",
            "precision-scanner@users.noreply.github.com",
        ]
    )

    git_command(
        [
            "git",
            "config",
            "user.name",
            "Precision Scanner",
        ]
    )

    # Add tracker.
    if not git_command(
        [
            "git",
            "add",
            TRACKER_FILE,
        ]
    ):
        return False

    # Check if anything is staged.
    try:

        result = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # 0 means no staged changes.
        if result.returncode == 0:

            print(
                "No tracker changes to commit."
            )

            return True

    except Exception as exc:

        print(
            "Git diff check failed:",
            exc,
        )

    # Commit.
    if not git_command(
        [
            "git",
            "commit",
            "-m",
            "Update scanner tracker",
        ]
    ):
        return False

    # Push.
    return git_command(
        [
            "git",
            "push",
        ]
    )


# ============================================================
# MARKET CACHE
# ============================================================

def refresh_markets_if_needed(
    markets,
    last_refresh,
):

    now = time.time()

    if (
        markets
        and now - last_refresh
        < MARKET_REFRESH_SECONDS
    ):
        return markets, last_refresh

    print(
        "Refreshing Coinbase markets..."
    )

    try:

        new_markets = get_markets()

        print(
            f"Markets loaded: "
            f"{len(new_markets)}"
        )

        return (
            new_markets,
            now,
        )

    except Exception as exc:

        print(
            "Market refresh failed:",
            exc,
        )

        # Keep previous cache if available.
        if markets:
            return markets, last_refresh

        raise


# ============================================================
# SLEEP / MINUTE ALIGNMENT
# ============================================================

def sleep_until_next_cycle(
    cycle_start,
):

    elapsed = time.time() - cycle_start

    remaining = (
        SCAN_INTERVAL_SECONDS
        - elapsed
    )

    if remaining < 1:
        remaining = 1

    print(
        f"Sleeping {remaining:.1f}s "
        f"until next scan..."
    )

    time.sleep(remaining)


# ============================================================
# MAIN CONTINUOUS ENGINE
# ============================================================

def main():

    print(
        "========================================"
    )

    print(
        f"PRECISION SCANNER {VERSION}"
    )

    print(
        "Continuous scanner starting..."
    )

    print(
        f"Reference expiry: "
        f"{REFERENCE_EXPIRY_MINUTES} minutes"
    )

    print(
        f"Minimum score: {MIN_SCORE}/100"
    )

    print(
        f"Scan interval: "
        f"{SCAN_INTERVAL_SECONDS} seconds"
    )

    print(
        "========================================"
    )

    data = load_tracker()

    markets = {}

    last_market_refresh = 0

    scan_number = 0

    scans_since_heartbeat = 0

    # --------------------------------------------------------
    # STARTUP TELEGRAM
    # --------------------------------------------------------

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:

        send_message(
            f"🟦 <b>PRECISION SCANNER {VERSION}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Scanner engine started.\n\n"
            f"🔄 Continuous scanning: ON\n"
            f"⏱ Scan interval: "
            f"{SCAN_INTERVAL_SECONDS}s\n"
            f"🎯 Minimum score: "
            f"{MIN_SCORE}/100\n"
            f"⏳ Reference expiry: "
            f"{REFERENCE_EXPIRY_MINUTES}m\n\n"
            f"Waiting for qualified setups..."
        )

    while True:

        cycle_start = time.time()

        scan_number += 1

        scans_since_heartbeat += 1

        tracker_changed = False

        # ----------------------------------------------------
        # TELEGRAM COMMANDS
        # ----------------------------------------------------

        try:

            command_changed, offset_changed, immediate_scan = (
                process_commands(data)
            )

            tracker_changed = (
                tracker_changed
                or command_changed
            )

            # Offset needs to be persisted locally.
            if offset_changed:

                save_tracker(data)

        except Exception as exc:

            print(
                "Command processing error:",
                exc,
            )

            immediate_scan = False

        # ----------------------------------------------------
        # MARKET CACHE
        # ----------------------------------------------------

        try:

            markets, last_market_refresh = (
                refresh_markets_if_needed(
                    markets,
                    last_market_refresh,
                )
            )

        except Exception as exc:

            print(
                "Unable to load markets:",
                exc,
            )

            time.sleep(10)

            continue

        # ----------------------------------------------------
        # RUN SCAN
        # ----------------------------------------------------

        try:

            (
                results,
                errors,
                cycle_tracker_changed,
                new_alert_count,
            ) = run_scan_cycle(
                data,
                markets,
                scan_number,
            )

            tracker_changed = (
                tracker_changed
                or cycle_tracker_changed
            )

        except Exception as exc:

            print(
                "SCAN CYCLE ERROR:",
                exc,
            )

            results = []
            errors = [
                str(exc)
            ]

            new_alert_count = 0

        # ----------------------------------------------------
        # HEARTBEAT
        # ----------------------------------------------------

        if (
            scans_since_heartbeat
            >= HEARTBEAT_EVERY_SCANS
        ):

            try:

                heartbeat = build_heartbeat(
                    results,
                    errors,
                    data,
                    scan_number,
                )

                send_message(
                    heartbeat
                )

                data["meta"][
                    "last_heartbeat"
                ] = format_utc()

                scans_since_heartbeat = 0

            except Exception as exc:

                print(
                    "Heartbeat error:",
                    exc,
                )

        # ----------------------------------------------------
        # SAVE TRACKER
        # ----------------------------------------------------

        if tracker_changed:

            if save_tracker(data):

                print(
                    "Tracker saved."
                )

                # Push only when actual signal/result
                # information changed.
                try:

                    commit_tracker()

                except Exception as exc:

                    print(
                        "GitHub update error:",
                        exc,
                    )

        # ----------------------------------------------------
        # CONSOLE SUMMARY
        # ----------------------------------------------------

        qualified = [
            x for x in results
            if x.get("signal") != "NO TRADE"
        ]

        print(
            "\n--- SCAN SUMMARY ---"
        )

        print(
            f"Assets analyzed: {len(results)}"
        )

        print(
            f"Qualified: {len(qualified)}"
        )

        print(
            f"New Telegram alerts: "
            f"{new_alert_count}"
        )

        if qualified:

            for result in qualified:

                print(
                    f"  {result['symbol']} "
                    f"{result['signal']} "
                    f"{result['score']}/100 "
                    f"{result['signal_id']}"
                )

        else:

            print(
                "No new qualified signal."
            )

            reasons = rejection_summary(
                results
            )

            if reasons:

                print(
                    "Top rejection reasons:"
                )

                for reason, count in reasons[:5]:

                    print(
                        f"  {count}x {reason}"
                    )

        if errors:

            print(
                f"Data warnings: {len(errors)}"
            )

            for error in errors[:5]:

                print(
                    f"  {error}"
                )

        # ----------------------------------------------------
        # WAIT
        # ----------------------------------------------------

        sleep_until_next_cycle(
            cycle_start
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nScanner stopped manually."
        )

    except Exception as exc:

        print(
            "\nFATAL SCANNER ERROR:",
            exc,
        )

        # Try to notify Telegram.
        try:

            send_message(
                f"🚨 <b>SCANNER STOPPED</b>\n\n"
                f"<code>{html.escape(str(exc))}</code>\n\n"
                f"Restart the scanner process."
            )

        except Exception:
            pass

        raise
