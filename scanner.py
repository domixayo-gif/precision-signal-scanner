import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# PRECISION IQ OPTION OTC SCANNER
# ============================================================
# READ-ONLY SCANNER
# - No automatic trading
# - Discovers real open OTC instruments from IQ Option
# - 5M trend + 1M entry
# - 5-minute reference expiry
# - CALL / PUT / NO TRADE
# - Telegram alerts
# - 5-minute per-asset cooldown
# ============================================================


# -----------------------------
# SETTINGS
# -----------------------------

MIN_SCORE = 80
BORDERLINE_SCORE = 75

MIN_ADX = 18.0

CANDLE_COUNT_5M = 120
CANDLE_COUNT_1M = 120

EXPIRY_MINUTES = 5

LOCK_SECONDS = 300

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")


# Maximum number of OTC assets to scan.
# The scanner discovers the actual instruments from IQ Option.
MAX_OTC_ASSETS = 40


# -----------------------------
# STATE
# -----------------------------

last_signal_time = {}


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        if response.ok:
            return True

        print("Telegram error:", response.text)
        return False

    except Exception as exc:
        print("Telegram exception:", exc)
        return False


# ============================================================
# BASIC MATH
# ============================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def mean(values):
    if not values:
        return 0.0

    return sum(values) / len(values)


def stddev(values):
    if len(values) < 2:
        return 0.0

    avg = mean(values)

    variance = sum(
        (x - avg) ** 2
        for x in values
    ) / len(values)

    return math.sqrt(variance)


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(candle):
    """
    IQ Option candle fields normally include:
    from, to, open, close, min, max, volume
    """

    return {
        "timestamp": int(
            safe_float(
                candle.get("from", candle.get("to", 0))
            )
        ),
        "open": safe_float(candle.get("open")),
        "high": safe_float(
            candle.get("max", candle.get("high"))
        ),
        "low": safe_float(
            candle.get("min", candle.get("low"))
        ),
        "close": safe_float(candle.get("close")),
        "volume": safe_float(candle.get("volume")),
    }


def clean_candles(raw):
    candles = []

    if not raw:
        return candles

    for item in raw:
        try:
            c = normalize_candle(item)

            if (
                c["open"] > 0
                and c["high"] > 0
                and c["low"] > 0
                and c["close"] > 0
            ):
                candles.append(c)

        except Exception:
            continue

    candles.sort(key=lambda x: x["timestamp"])

    return candles


# ============================================================
# EMA
# ============================================================

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1.0)

    result = []

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
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):
    if len(candles) <= period:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            ),
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    current_atr = mean(true_ranges[:period])

    for tr in true_ranges[period:]:
        current_atr = (
            (current_atr * (period - 1))
            + tr
        ) / period

    return current_atr


# ============================================================
# ADX / DMI
# ============================================================

def adx_dmi(candles, period=14):
    if len(candles) < period + 2:
        return None, None, None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        high_change = (
            current["high"]
            - previous["high"]
        )

        low_change = (
            previous["low"]
            - current["low"]
        )

        pdm = (
            high_change
            if high_change > low_change
            and high_change > 0
            else 0
        )

        mdm = (
            low_change
            if low_change > high_change
            and low_change > 0
            else 0
        )

        tr = max(
            current["high"] - current["low"],
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            ),
        )

        trs.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    if len(trs) < period:
        return None, None, None

    tr_avg = mean(trs[:period])
    plus_avg = mean(plus_dm[:period])
    minus_avg = mean(minus_dm[:period])

    dx_values = []
    plus_di_values = []
    minus_di_values = []

    for i in range(period, len(trs)):
        tr_avg = (
            (tr_avg * (period - 1))
            + trs[i]
        ) / period

        plus_avg = (
            (plus_avg * (period - 1))
            + plus_dm[i]
        ) / period

        minus_avg = (
            (minus_avg * (period - 1))
            + minus_dm[i]
        ) / period

        if tr_avg == 0:
            plus_di = 0
            minus_di = 0
        else:
            plus_di = 100 * plus_avg / tr_avg
            minus_di = 100 * minus_avg / tr_avg

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
        return None, None, None

    adx_value = mean(dx_values[:period])

    for dx in dx_values[period:]:
        adx_value = (
            (adx_value * (period - 1))
            + dx
        ) / period

    return (
        adx_value,
        plus_di_values[-1],
        minus_di_values[-1],
    )


# ============================================================
# MACD
# ============================================================

def macd(values):
    if len(values) < 35:
        return None, None

    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return None, None

    fast_start = len(fast) - len(slow)

    if fast_start < 0:
        return None, None

    macd_values = []

    for i in range(len(slow)):
        macd_values.append(
            fast[fast_start + i]
            - slow[i]
        )

    if len(macd_values) < 9:
        return None, None

    signal_line = ema(macd_values, 9)

    if not signal_line:
        return None, None

    macd_now = macd_values[-1]
    signal_now = signal_line[-1]

    return macd_now, signal_now


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_direction(candles):
    if len(candles) < 10:
        return "NEUTRAL"

    recent = candles[-10:]

    highs = [
        c["high"]
        for c in recent
    ]

    lows = [
        c["low"]
        for c in recent
    ]

    first_half_high = mean(highs[:5])
    second_half_high = mean(highs[5:])

    first_half_low = mean(lows[:5])
    second_half_low = mean(lows[5:])

    if (
        second_half_high > first_half_high
        and second_half_low > first_half_low
    ):
        return "BULLISH"

    if (
        second_half_high < first_half_high
        and second_half_low < first_half_low
    ):
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# CANDLE STRENGTH
# ============================================================

def candle_strength(candle):
    total_range = candle["high"] - candle["low"]

    if total_range <= 0:
        return 0.0

    body = abs(
        candle["close"]
        - candle["open"]
    )

    return body / total_range


def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "BULLISH"

    if candle["close"] < candle["open"]:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# EXTENSION
# ============================================================

def extension_atr(candles, atr_value):
    if not atr_value or atr_value <= 0:
        return 0.0

    closes = [
        c["close"]
        for c in candles[-20:]
    ]

    if not closes:
        return 0.0

    average = mean(closes)

    return abs(
        closes[-1] - average
    ) / atr_value


# ============================================================
# ROOM
# ============================================================

def room_in_atr(candles, atr_value):
    if not atr_value or atr_value <= 0:
        return 0.0, 0.0

    current = candles[-1]["close"]

    recent_high = max(
        c["high"]
        for c in candles[-20:]
    )

    recent_low = min(
        c["low"]
        for c in candles[-20:]
    )

    room_up = (
        recent_high - current
    ) / atr_value

    room_down = (
        current - recent_low
    ) / atr_value

    return (
        max(room_up, 0),
        max(room_down, 0),
    )


# ============================================================
# ANALYZE ONE ASSET
# ============================================================

def analyze_asset(asset, candles_5m, candles_1m):

    if (
        len(candles_5m) < 50
        or len(candles_1m) < 50
    ):
        return None

    close5 = [
        c["close"]
        for c in candles_5m
    ]

    close1 = [
        c["close"]
        for c in candles_1m
    ]

    # -------------------------
    # 5M TREND
    # -------------------------

    ema20_5 = ema(close5, 20)
    ema50_5 = ema(close5, 50)

    if not ema20_5 or not ema50_5:
        return None

    ema20_now = ema20_5[-1]
    ema50_now = ema50_5[-1]

    price5 = close5[-1]

    if (
        price5 > ema20_now
        and ema20_now > ema50_now
    ):
        trend5 = "BULLISH"

    elif (
        price5 < ema20_now
        and ema20_now < ema50_now
    ):
        trend5 = "BEARISH"

    else:
        trend5 = "NEUTRAL"

    # -------------------------
    # 1M TREND
    # -------------------------

    ema9_1 = ema(close1, 9)
    ema21_1 = ema(close1, 21)

    if not ema9_1 or not ema21_1:
        return None

    price1 = close1[-1]

    if (
        price1 > ema9_1[-1]
        and ema9_1[-1] > ema21_1[-1]
    ):
        entry = "BULLISH"

    elif (
        price1 < ema9_1[-1]
        and ema9_1[-1] < ema21_1[-1]
    ):
        entry = "BEARISH"

    else:
        entry = "NEUTRAL"

    # -------------------------
    # RSI
    # -------------------------

    rsi5 = rsi(close5, 14)
    rsi1 = rsi(close1, 14)

    if rsi5 is None or rsi1 is None:
        return None

    # -------------------------
    # MACD
    # -------------------------

    macd_value, macd_signal = macd(close1)

    if (
        macd_value is None
        or macd_signal is None
    ):
        return None

    # -------------------------
    # ADX
    # -------------------------

    adx_value, plus_di, minus_di = adx_dmi(
        candles_5m,
        14,
    )

    if (
        adx_value is None
        or plus_di is None
        or minus_di is None
    ):
        return None

    # -------------------------
    # ATR
    # -------------------------

    atr_value = atr(
        candles_5m,
        14,
    )

    if not atr_value or atr_value <= 0:
        return None

    # -------------------------
    # STRUCTURE
    # -------------------------

    structure = structure_direction(
        candles_5m
    )

    # -------------------------
    # LAST CANDLE
    # -------------------------

    last1 = candles_1m[-1]

    candle_dir = candle_direction(last1)

    strength = candle_strength(last1)

    # -------------------------
    # EXTENSION
    # -------------------------

    extension = extension_atr(
        candles_5m,
        atr_value,
    )

    room_up, room_down = room_in_atr(
        candles_5m,
        atr_value,
    )

    # ========================================================
    # HARD FILTERS
    # ========================================================

    if adx_value < MIN_ADX:
        return {
            "signal": "NO TRADE",
            "reason": "ADX too weak",
            "score": 0,
            "asset": asset,
            "trend5": trend5,
            "entry": entry,
            "structure": structure,
            "adx": adx_value,
            "rsi5": rsi5,
            "rsi1": rsi1,
        }

    # Avoid extremely extended markets.
    if extension >= 3.0:
        return {
            "signal": "NO TRADE",
            "reason": "Extreme extension",
            "score": 0,
            "asset": asset,
            "trend5": trend5,
            "entry": entry,
            "structure": structure,
            "adx": adx_value,
            "rsi5": rsi5,
            "rsi1": rsi1,
        }

    # Weak candle filter.
    if strength < 0.35:
        return {
            "signal": "NO TRADE",
            "reason": "Weak entry candle",
            "score": 0,
            "asset": asset,
            "trend5": trend5,
            "entry": entry,
            "structure": structure,
            "adx": adx_value,
            "rsi5": rsi5,
            "rsi1": rsi1,
        }

    # ========================================================
    # SCORE
    # ========================================================

    call_score = 0
    put_score = 0

    # 5M trend
    if trend5 == "BULLISH":
        call_score += 20

    elif trend5 == "BEARISH":
        put_score += 20

    # 1M entry
    if entry == "BULLISH":
        call_score += 15

    elif entry == "BEARISH":
        put_score += 15

    # Structure
    if structure == "BULLISH":
        call_score += 15

    elif structure == "BEARISH":
        put_score += 15

    # DMI
    if plus_di > minus_di:
        call_score += 10

    elif minus_di > plus_di:
        put_score += 10

    # MACD
    if macd_value > macd_signal:
        call_score += 10

    elif macd_value < macd_signal:
        put_score += 10

    # RSI
    if 43 <= rsi1 <= 68:
        call_score += 10

    if 32 <= rsi1 <= 57:
        put_score += 10

    # Candle
    if candle_dir == "BULLISH":
        call_score += 10

    elif candle_dir == "BEARISH":
        put_score += 10

    # ADX quality
    if adx_value >= 25:
        if call_score > put_score:
            call_score += 5
        elif put_score > call_score:
            put_score += 5

    # Extension penalty
    if extension > 2.0:
        if call_score > put_score:
            call_score -= 8
        elif put_score > call_score:
            put_score -= 8

    # ========================================================
    # ROOM FILTER
    # ========================================================

    if call_score > put_score:
        direction = "CALL"
        score = call_score

        if room_up < 0.50:
            return {
                "signal": "NO TRADE",
                "reason": "Insufficient upside room",
                "score": score,
                "asset": asset,
                "trend5": trend5,
                "entry": entry,
                "structure": structure,
                "adx": adx_value,
                "rsi5": rsi5,
                "rsi1": rsi1,
            }

    elif put_score > call_score:
        direction = "PUT"
        score = put_score

        if room_down < 0.50:
            return {
                "signal": "NO TRADE",
                "reason": "Insufficient downside room",
                "score": score,
                "asset": asset,
                "trend5": trend5,
                "entry": entry,
                "structure": structure,
                "adx": adx_value,
                "rsi5": rsi5,
                "rsi1": rsi1,
            }

    else:
        return {
            "signal": "NO TRADE",
            "reason": "Conflicting setup",
            "score": 0,
            "asset": asset,
            "trend5": trend5,
            "entry": entry,
            "structure": structure,
            "adx": adx_value,
            "rsi5": rsi5,
            "rsi1": rsi1,
        }

    # ========================================================
    # FINAL CONFIRMATION
    # ========================================================

    if score < MIN_SCORE:
        return {
            "signal": "NO TRADE",
            "reason": f"Score below {MIN_SCORE}",
            "score": score,
            "asset": asset,
            "trend5": trend5,
            "entry": entry,
            "structure": structure,
            "adx": adx_value,
            "rsi5": rsi5,
            "rsi1": rsi1,
        }

    return {
        "signal": direction,
        "score": int(clamp(score, 0, 100)),
        "asset": asset,
        "trend5": trend5,
        "entry": entry,
        "structure": structure,
        "adx": adx_value,
        "rsi5": rsi5,
        "rsi1": rsi1,
        "macd": macd_value,
        "macd_signal": macd_signal,
        "atr": atr_value,
        "extension": extension,
        "room_up": room_up,
        "room_down": room_down,
        "candle_strength": strength,
        "price": price1,
    }


# ============================================================
# DISCOVER REAL OPEN OTC ASSETS
# ============================================================

def discover_otc_assets(api):
    print("Discovering currently open IQ Option OTC assets...")

    try:
        all_open = api.get_all_open_time()
    except Exception as exc:
        print("Asset discovery failed:", exc)
        return []

    found = set()

    # IQ Option community API exposes OTC instruments
    # through binary/turbo/digital markets.
    for market_type in (
        "digital",
        "turbo",
        "binary",
    ):
        market = all_open.get(
            market_type,
            {},
        )

        if not isinstance(market, dict):
            continue

        for asset, info in market.items():

            if not isinstance(asset, str):
                continue

            if "-OTC" not in asset.upper():
                continue

            if not isinstance(info, dict):
                continue

            if info.get("open") is not True:
                continue

            found.add(asset)

    assets = sorted(found)

    print(
        f"Found {len(assets)} open OTC assets."
    )

    for asset in assets:
        print("OTC:", asset)

    return assets[:MAX_OTC_ASSETS]


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(api, asset, seconds, count):
    try:
        raw = api.get_candles(
            asset,
            seconds,
            count,
            time.time(),
        )

        candles = clean_candles(raw)

        if len(candles) < 50:
            return []

        return candles

    except Exception as exc:
        print(
            f"{asset} {seconds}s candle error:",
            exc,
        )

        return []


# ============================================================
# SIGNAL ID
# ============================================================

def signal_id(asset, direction):
    now = datetime.now(
        timezone.utc
    ).strftime("%H%M%S")

    clean_asset = (
        asset
        .replace("-OTC", "")
        .replace("/", "")
        .replace("-", "")
    )

    return (
        f"{clean_asset}-"
        f"{direction}-"
        f"{now}"
    )


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(result):
    sid = signal_id(
        result["asset"],
        result["signal"],
    )

    message = (
        "🟢 <b>NEW QUALIFIED IQ OPTION OTC SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💱 <b>{result['asset']}</b>\n"
        f"🎯 <b>{result['signal']}</b>\n"
        f"⭐ <b>Score:</b> {result['score']}/100\n"
        f"⏱ <b>Reference expiry:</b> {EXPIRY_MINUTES} minutes\n"
        f"💰 <b>Price:</b> {result['price']:.6f}\n"
        "\n"
        f"📊 <b>5M Trend:</b> {result['trend5']}\n"
        f"📈 <b>1M Entry:</b> {result['entry']}\n"
        f"🏗 <b>Structure:</b> {result['structure']}\n"
        f"📐 <b>ADX:</b> {result['adx']:.1f}\n"
        f"📉 <b>5M RSI:</b> {result['rsi5']:.1f}\n"
        f"📉 <b>1M RSI:</b> {result['rsi1']:.1f}\n"
        f"📏 <b>Extension:</b> {result['extension']:.2f} ATR\n"
        f"⬆️ <b>Room Up:</b> {result['room_up']:.2f} ATR\n"
        f"⬇️ <b>Room Down:</b> {result['room_down']:.2f} ATR\n"
        f"🕯 <b>Candle Strength:</b> "
        f"{result['candle_strength']:.2f}\n"
        "\n"
        f"🆔 <b>Signal ID:</b> <code>{sid}</code>\n"
        "\n"
        "⚠️ Read-only scanner. "
        "No trade was placed automatically."
    )

    return message, sid


# ============================================================
# OPTIONAL TRACKER
# ============================================================

def save_tracker_signal(result, sid):
    """
    Tries to preserve compatibility with an existing storage.py.

    If storage.py exposes update_tracker(), we attempt to use it.
    Failure here will NOT stop Telegram scanning.
    """

    try:
        from storage import update_tracker

        try:
            update_tracker(
                sid,
                {
                    "signal_id": sid,
                    "asset": result["asset"],
                    "direction": result["signal"],
                    "score": result["score"],
                    "timestamp": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "outcome": "PENDING",
                },
            )

            print(
                "Tracker updated:",
                sid,
            )

        except TypeError:
            # Compatibility with different existing
            # storage.py signatures.
            print(
                "storage.py exists but uses a "
                "different update_tracker signature."
            )

    except ImportError:
        print(
            "storage.py not available; "
            "continuing without tracker."
        )

    except Exception as exc:
        print(
            "Tracker warning:",
            exc,
        )


# ============================================================
# MAIN SCANNER
# ============================================================

def run_scanner():

    print("=" * 60)
    print("PRECISION IQ OPTION OTC SCANNER")
    print("=" * 60)

    if not IQ_EMAIL:
        print("ERROR: IQ_EMAIL secret is missing.")
        return

    if not IQ_PASSWORD:
        print("ERROR: IQ_PASSWORD secret is missing.")
        return

    print("Connecting to IQ Option...")

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    try:
        connected, reason = api.connect()
    except Exception as exc:
        print(
            "IQ Option connection exception:",
            exc,
        )
        return

    if not connected:
        print(
            "IQ Option login failed:",
            reason,
        )

        telegram(
            "🔴 <b>IQ OPTION SCANNER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Connection/login failed.\n"
            f"<code>{reason}</code>"
        )

        return

    print("IQ Option connection successful.")

    try:
        # Practice/demo balance only.
        api.change_balance("PRACTICE")
        print("Using IQ Option PRACTICE account.")

    except Exception as exc:
        print(
            "Could not switch balance:",
            exc,
        )

    assets = discover_otc_assets(api)

    if not assets:
        print(
            "No open OTC instruments were discovered."
        )

        telegram(
            "🟡 <b>IQ OPTION OTC SCANNER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No currently open OTC instruments "
            "were discovered."
        )

        try:
            api.close()
        except Exception:
            pass

        return

    qualified = []

    for index, asset in enumerate(
        assets,
        start=1,
    ):

        print(
            f"\n[{index}/{len(assets)}] "
            f"Scanning {asset}"
        )

        # -----------------------------------------
        # COOLDOWN
        # -----------------------------------------

        previous = last_signal_time.get(
            asset,
            0,
        )

        if (
            time.time() - previous
            < LOCK_SECONDS
        ):
            print(
                "Skipped: cooldown active."
            )
            continue

        # -----------------------------------------
        # 5M CANDLES
        # -----------------------------------------

        candles_5m = get_candles(
            api,
            asset,
            300,
            CANDLE_COUNT_5M,
        )

        if not candles_5m:
            print(
                "No usable 5M candles."
            )
            continue

        # -----------------------------------------
        # 1M CANDLES
        # -----------------------------------------

        candles_1m = get_candles(
            api,
            asset,
            60,
            CANDLE_COUNT_1M,
        )

        if not candles_1m:
            print(
                "No usable 1M candles."
            )
            continue

        # -----------------------------------------
        # ANALYSIS
        # -----------------------------------------

        result = analyze_asset(
            asset,
            candles_5m,
            candles_1m,
        )

        if not result:
            print(
                "Analysis unavailable."
            )
            continue

        print(
            f"Result: {result['signal']} "
            f"| Score: {result['score']} "
            f"| Reason: "
            f"{result.get('reason', 'qualified')}"
        )

        # -----------------------------------------
        # ONLY QUALIFIED SIGNALS
        # -----------------------------------------

        if (
            result["signal"]
            not in ("CALL", "PUT")
        ):
            continue

        if result["score"] < MIN_SCORE:
            continue

        qualified.append(result)

    # ========================================================
    # SEND QUALIFIED SIGNALS
    # ========================================================

    print("\n" + "=" * 60)
    print(
        f"QUALIFIED SIGNALS: {len(qualified)}"
    )
    print("=" * 60)

    # Highest score first.
    qualified.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    for result in qualified:

        asset = result["asset"]

        previous = last_signal_time.get(
            asset,
            0,
        )

        if (
            time.time() - previous
            < LOCK_SECONDS
        ):
            continue

        message, sid = format_signal(
            result
        )

        sent = telegram(message)

        if sent:
            last_signal_time[
                asset
            ] = time.time()

            save_tracker_signal(
                result,
                sid,
            )

            print(
                "Telegram signal sent:",
                sid,
            )

        else:
            print(
                "Telegram signal failed:",
                asset,
            )

    # ========================================================
    # FINISH
    # ========================================================

    try:
        api.close()
    except Exception:
        pass

    print("\nScanner finished.")


# ============================================================
# ERROR HANDLING
# ============================================================

if __name__ == "__main__":

    try:
        run_scanner()

    except KeyboardInterrupt:
        print(
            "\nScanner stopped."
        )

    except Exception as exc:
        print(
            "\nFATAL SCANNER ERROR:",
            exc,
        )

        traceback.print_exc()

        try:
            telegram(
                "🔴 <b>SCANNER ERROR</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"<code>{str(exc)}</code>"
            )
        except Exception:
            pass
