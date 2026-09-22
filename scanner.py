import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# PRECISION IQ OPTION OTC SCANNER — CONTINUOUS VERSION
# ============================================================
# READ-ONLY SCANNER
# - No automatic trading
# - Discovers real IQ Option OTC instruments
# - 5M trend + 1M entry
# - 5-minute reference expiry
# - Keeps running continuously
# - Scans every 5 minutes
# - Sends QUALIFIED SIGNAL or NO TRADE to Telegram
# ============================================================


# ============================================================
# ENVIRONMENT
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# STRATEGY SETTINGS
# ============================================================

MIN_SCORE = 80
MIN_ADX = 18.0
MIN_CANDLE_STRENGTH = 0.40

MAX_EXTENSION_ATR = 2.80
MIN_ROOM_ATR = 0.60

MIN_CONFIRMATIONS = 4
MIN_SCORE_GAP = 10


# ============================================================
# SCANNER SETTINGS
# ============================================================

CANDLE_COUNT_5M = 160
CANDLE_COUNT_1M = 160

MAX_OTC_ASSETS = 60

EXPIRY_MINUTES = 5

SCAN_INTERVAL_SECONDS = 300

BALANCE_MODE = "PRACTICE"


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
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

        print("Telegram error:", response.text)

    except Exception as e:
        print("Telegram exception:", e)

    return False


# ============================================================
# MATH HELPERS
# ============================================================

def safe_float(value, default=0.0):
    try:
        value = float(value)

        if math.isnan(value) or math.isinf(value):
            return default

        return value

    except Exception:
        return default


def mean(values):
    values = [safe_float(v) for v in values]

    if not values:
        return 0.0

    return sum(values) / len(values)


# ============================================================
# EMA
# ============================================================

def ema(values, period):
    values = [safe_float(v) for v in values]

    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1.0)

    result = [sum(values[:period]) / period]

    for price in values[period:]:
        result.append(
            (price - result[-1]) * multiplier + result[-1]
        )

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):
    values = [safe_float(v) for v in values]

    if len(values) <= period:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = []

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100.0 - (100.0 / (1.0 + rs)))

    for i in range(period, len(gains)):
        avg_gain = (
            ((avg_gain * (period - 1)) + gains[i])
            / period
        )

        avg_loss = (
            ((avg_loss * (period - 1)) + losses[i])
            / period
        )

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(
                100.0 - (100.0 / (1.0 + rs))
            )

    return result


# ============================================================
# MACD
# ============================================================

def macd(values, fast=12, slow=26, signal=9):
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    if not fast_ema or not slow_ema:
        return [], []

    offset = slow - fast

    if offset >= len(fast_ema):
        return [], []

    fast_aligned = fast_ema[offset:]

    length = min(
        len(fast_aligned),
        len(slow_ema)
    )

    macd_line = []

    for i in range(length):
        macd_line.append(
            fast_aligned[i] - slow_ema[i]
        )

    signal_line = ema(macd_line, signal)

    return macd_line, signal_line


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):
    if len(candles) <= period:
        return []

    trs = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        high = safe_float(current["high"])
        low = safe_float(current["low"])
        prev_close = safe_float(previous["close"])

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        trs.append(tr)

    if len(trs) < period:
        return []

    result = [
        sum(trs[:period]) / period
    ]

    for value in trs[period:]:
        result.append(
            ((result[-1] * (period - 1)) + value)
            / period
        )

    return result


# ============================================================
# ADX / DMI
# ============================================================

def adx_dmi(candles, period=14):
    if len(candles) <= period + 1:
        return None

    plus_dm = []
    minus_dm = []
    tr_values = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        high = safe_float(current["high"])
        low = safe_float(current["low"])

        prev_high = safe_float(previous["high"])
        prev_low = safe_float(previous["low"])
        prev_close = safe_float(previous["close"])

        up_move = high - prev_high
        down_move = prev_low - low

        if up_move > down_move and up_move > 0:
            pdm = up_move
        else:
            pdm = 0.0

        if down_move > up_move and down_move > 0:
            mdm = down_move
        else:
            mdm = 0.0

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        plus_dm.append(pdm)
        minus_dm.append(mdm)
        tr_values.append(tr)

    if len(tr_values) < period:
        return None

    atr_values = []

    current_atr = sum(
        tr_values[:period]
    ) / period

    atr_values.append(current_atr)

    for i in range(period, len(tr_values)):
        current_atr = (
            ((current_atr * (period - 1))
             + tr_values[i])
            / period
        )

        atr_values.append(current_atr)

    plus_values = []
    minus_values = []

    smooth_plus = sum(
        plus_dm[:period]
    ) / period

    smooth_minus = sum(
        minus_dm[:period]
    ) / period

    for i in range(len(atr_values)):

        if i > 0:
            idx = period + i - 1

            if idx < len(plus_dm):
                smooth_plus = (
                    ((smooth_plus * (period - 1))
                     + plus_dm[idx])
                    / period
                )

                smooth_minus = (
                    ((smooth_minus * (period - 1))
                     + minus_dm[idx])
                    / period
                )

        atr_value = atr_values[i]

        if atr_value <= 0:
            plus_di = 0.0
            minus_di = 0.0
        else:
            plus_di = (
                100.0 * smooth_plus / atr_value
            )

            minus_di = (
                100.0 * smooth_minus / atr_value
            )

        plus_values.append(plus_di)
        minus_values.append(minus_di)

    dx_values = []

    for plus_di, minus_di in zip(
        plus_values,
        minus_values
    ):

        denominator = plus_di + minus_di

        if denominator == 0:
            dx = 0.0
        else:
            dx = (
                100.0
                * abs(plus_di - minus_di)
                / denominator
            )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_value = sum(
        dx_values[:period]
    ) / period

    for value in dx_values[period:]:
        adx_value = (
            ((adx_value * (period - 1))
             + value)
            / period
        )

    return {
        "adx": adx_value,
        "plus_di": plus_values[-1],
        "minus_di": minus_values[-1],
    }


# ============================================================
# CANDLE STRENGTH
# ============================================================

def candle_strength(candles, count=20):
    if not candles:
        return 0.0

    selected = candles[-count:]

    strengths = []

    for candle in selected:

        high = safe_float(candle["high"])
        low = safe_float(candle["low"])
        open_price = safe_float(candle["open"])
        close = safe_float(candle["close"])

        rng = high - low

        if rng <= 0:
            continue

        body = abs(close - open_price)

        strengths.append(
            body / rng
        )

    return mean(strengths)


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles, lookback=20):
    if len(candles) < lookback:
        return "NEUTRAL"

    selected = candles[-lookback:]

    highs = [
        safe_float(c["high"])
        for c in selected
    ]

    lows = [
        safe_float(c["low"])
        for c in selected
    ]

    if len(highs) < 6:
        return "NEUTRAL"

    recent_high = max(highs[-5:])
    previous_high = max(highs[-10:-5])

    recent_low = min(lows[-5:])
    previous_low = min(lows[-10:-5])

    if recent_high < previous_high and recent_low < previous_low:
        return "BEARISH"

    if recent_high > previous_high and recent_low > previous_low:
        return "BULLISH"

    return "NEUTRAL"


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(candle):
    try:
        return {
            "open": safe_float(
                candle.get("open")
            ),
            "close": safe_float(
                candle.get("close")
            ),
            "high": safe_float(
                candle.get("max", candle.get("high"))
            ),
            "low": safe_float(
                candle.get("min", candle.get("low"))
            ),
            "from": safe_float(
                candle.get("from", 0)
            ),
            "to": safe_float(
                candle.get("to", 0)
            ),
        }

    except Exception:
        return None


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(api, asset, interval, count):

    try:
        candles = api.get_candles(
            asset,
            interval,
            count,
            time.time(),
        )

        if not candles:
            return []

        normalized = []

        for candle in candles:

            item = normalize_candle(candle)

            if item is not None:
                normalized.append(item)

        # Remove currently forming candle.
        if len(normalized) > 2:
            normalized = normalized[:-1]

        return normalized

    except Exception as e:

        print(
            f"Candle error {asset} "
            f"{interval}s: {e}"
        )

        return []


# ============================================================
# OTC NAME DETECTION
# ============================================================

def is_otc_name(name):

    if not name:
        return False

    name = str(name).upper()

    return (
        "-OTC" in name
        or "_OTC" in name
        or " OTC" in name
        or ".OTC" in name
    )


# ============================================================
# CLEAN ACTIVE NAME
# ============================================================

def clean_active_name(name):

    name = str(name).strip()

    if "." in name:
        name = name.split(".")[-1]

    return name


# ============================================================
# GET IQ OPTION INITIALIZATION
# ============================================================

def get_raw_init(api):

    try:
        data = api.get_all_init_v2()

        if data:
            return data

    except Exception as e:
        print("get_all_init_v2 error:", e)

    try:
        data = api.get_all_init()

        if data:
            return data

    except Exception as e:
        print("get_all_init error:", e)

    return {}


# ============================================================
# DISCOVER OTC ASSETS
# ============================================================

def discover_otc_assets(api):

    raw = get_raw_init(api)

    if not raw:
        print("No initialization data.")
        return []

    assets = []

    for section_name in (
        "binary",
        "turbo",
    ):

        section = raw.get(section_name, {})

        if not isinstance(section, dict):
            continue

        for name, info in section.items():

            if not is_otc_name(name):
                continue

            if not isinstance(info, dict):
                continue

            enabled = info.get("enabled", True)
            suspended = info.get("is_suspended", False)

            if enabled is False:
                continue

            if suspended:
                continue

            clean_name = clean_active_name(name)

            active_id = (
                info.get("active_id")
                or info.get("id")
                or info.get("activeId")
            )

            if active_id is not None:

                try:
                    OP_code.ACTIVES[clean_name] = int(
                        active_id
                    )

                except Exception:
                    pass

            if clean_name not in assets:
                assets.append(clean_name)

    assets = assets[:MAX_OTC_ASSETS]

    print(
        f"Discovered {len(assets)} OTC assets."
    )

    return assets


# ============================================================
# TEST CANDLE FEED
# ============================================================

def test_candle_feed(api, assets):

    working = []

    for asset in assets:

        candles = get_candles(
            api,
            asset,
            60,
            10,
        )

        if len(candles) >= 5:

            working.append(asset)

    print(
        f"Working OTC candle feeds: "
        f"{len(working)}"
    )

    return working


# ============================================================
# ANALYZE ASSET
# ============================================================

def analyze_asset(api, asset):

    candles_5m = get_candles(
        api,
        asset,
        300,
        CANDLE_COUNT_5M,
    )

    candles_1m = get_candles(
        api,
        asset,
        60,
        CANDLE_COUNT_1M,
    )

    if (
        len(candles_5m) < 60
        or len(candles_1m) < 60
    ):
        return None

    close_5m = [
        c["close"]
        for c in candles_5m
    ]

    close_1m = [
        c["close"]
        for c in candles_1m
    ]

    # --------------------------------------------------------
    # 5M INDICATORS
    # --------------------------------------------------------

    ema20_5m = ema(close_5m, 20)
    ema50_5m = ema(close_5m, 50)

    rsi_5m_values = rsi(
        close_5m,
        14,
    )

    macd_5m, signal_5m = macd(
        close_5m
    )

    atr_5m = atr(
        candles_5m,
        14,
    )

    dmi = adx_dmi(
        candles_5m,
        14,
    )

    if not ema20_5m or not ema50_5m:
        return None

    if not rsi_5m_values:
        return None

    if not atr_5m:
        return None

    if not dmi:
        return None

    # --------------------------------------------------------
    # 1M INDICATORS
    # --------------------------------------------------------

    ema9_1m = ema(
        close_1m,
        9,
    )

    ema21_1m = ema(
        close_1m,
        21,
    )

    rsi_1m_values = rsi(
        close_1m,
        14,
    )

    macd_1m, signal_1m = macd(
        close_1m
    )

    if not ema9_1m or not ema21_1m:
        return None

    if not rsi_1m_values:
        return None

    price = close_1m[-1]

    # --------------------------------------------------------
    # CURRENT VALUES
    # --------------------------------------------------------

    ema20 = ema20_5m[-1]
    ema50 = ema50_5m[-1]

    rsi5 = rsi_5m_values[-1]
    rsi1 = rsi_1m_values[-1]

    atr_value = atr_5m[-1]

    adx_value = dmi["adx"]
    plus_di = dmi["plus_di"]
    minus_di = dmi["minus_di"]

    ema9 = ema9_1m[-1]
    ema21 = ema21_1m[-1]

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    if (
        ema20 > ema50
        and price > ema20
    ):
        trend = "BULLISH"

    elif (
        ema20 < ema50
        and price < ema20
    ):
        trend = "BEARISH"

    else:
        trend = "NEUTRAL"

    # --------------------------------------------------------
    # 1M ENTRY
    # --------------------------------------------------------

    if (
        ema9 > ema21
        and price > ema9
    ):
        entry = "BULLISH"

    elif (
        ema9 < ema21
        and price < ema9
    ):
        entry = "BEARISH"

    else:
        entry = "NEUTRAL"

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    structure = market_structure(
        candles_5m
    )

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    macd_direction = "NEUTRAL"

    if macd_5m and signal_5m:

        if (
            macd_5m[-1]
            > signal_5m[-1]
        ):
            macd_direction = "BULLISH"

        elif (
            macd_5m[-1]
            < signal_5m[-1]
        ):
            macd_direction = "BEARISH"

    # --------------------------------------------------------
    # 1M MACD
    # --------------------------------------------------------

    macd1_direction = "NEUTRAL"

    if macd_1m and signal_1m:

        if (
            macd_1m[-1]
            > signal_1m[-1]
        ):
            macd1_direction = "BULLISH"

        elif (
            macd_1m[-1]
            < signal_1m[-1]
        ):
            macd1_direction = "BEARISH"

    # --------------------------------------------------------
    # CANDLE STRENGTH
    # --------------------------------------------------------

    strength = candle_strength(
        candles_1m,
        20,
    )

    # --------------------------------------------------------
    # EXTENSION
    # --------------------------------------------------------

    extension = (
        abs(price - ema20)
        / atr_value
        if atr_value > 0
        else 999
    )

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE ROOM
    # --------------------------------------------------------

    recent_high = max(
        c["high"]
        for c in candles_5m[-20:]
    )

    recent_low = min(
        c["low"]
        for c in candles_5m[-20:]
    )

    room_up = (
        (recent_high - price)
        / atr_value
        if atr_value > 0
        else 0
    )

    room_down = (
        (price - recent_low)
        / atr_value
        if atr_value > 0
        else 0
    )

    # --------------------------------------------------------
    # HARD FILTERS
    # --------------------------------------------------------

    if adx_value < MIN_ADX:
        return None

    if extension >= MAX_EXTENSION_ATR:
        return None

    if strength < MIN_CANDLE_STRENGTH:
        return None

    # --------------------------------------------------------
    # SCORES
    # --------------------------------------------------------

    call_score = 0
    put_score = 0

    call_confirmations = 0
    put_confirmations = 0

    # 5M trend
    if trend == "BULLISH":
        call_score += 20
        call_confirmations += 1

    elif trend == "BEARISH":
        put_score += 20
        put_confirmations += 1

    # 1M entry
    if entry == "BULLISH":
        call_score += 15
        call_confirmations += 1

    elif entry == "BEARISH":
        put_score += 15
        put_confirmations += 1

    # Structure
    if structure == "BULLISH":
        call_score += 15
        call_confirmations += 1

    elif structure == "BEARISH":
        put_score += 15
        put_confirmations += 1

    # DMI
    if plus_di > minus_di:
        call_score += 10
        call_confirmations += 1

    elif minus_di > plus_di:
        put_score += 10
        put_confirmations += 1

    # MACD
    if macd_direction == "BULLISH":
        call_score += 10
        call_confirmations += 1

    elif macd_direction == "BEARISH":
        put_score += 10
        put_confirmations += 1

    # 1M MACD
    if macd1_direction == "BULLISH":
        call_score += 5

    elif macd1_direction == "BEARISH":
        put_score += 5

    # RSI
    if 43 <= rsi5 <= 68:
        call_score += 10
        call_confirmations += 1

    elif 32 <= rsi5 <= 57:
        put_score += 10
        put_confirmations += 1

    # Candle strength
    if strength >= 0.40:

        if trend == "BULLISH":
            call_score += 10

        elif trend == "BEARISH":
            put_score += 10

    # ADX quality
    if adx_value >= 25:

        if call_score > put_score:
            call_score += 5

        elif put_score > call_score:
            put_score += 5

    # Extension penalty
    if extension >= 2.0:

        if call_score > put_score:
            call_score -= 8

        elif put_score > call_score:
            put_score -= 8

    # --------------------------------------------------------
    # SELECT DIRECTION
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
        return None

    score_gap = abs(
        call_score - put_score
    )

    # --------------------------------------------------------
    # FINAL QUALIFICATION
    # --------------------------------------------------------

    if score < MIN_SCORE:
        return None

    if confirmations < MIN_CONFIRMATIONS:
        return None

    if score_gap < MIN_SCORE_GAP:
        return None

    if room < MIN_ROOM_ATR:
        return None

    timestamp = datetime.now(
        timezone.utc
    )

    signal_id = (
        f"{asset}-"
        f"{direction}-"
        f"{timestamp.strftime('%H%M%S')}"
    )

    return {
        "signal_id": signal_id,
        "asset": asset,
        "direction": direction,
        "score": score,
        "price": price,
        "trend": trend,
        "entry": entry,
        "structure": structure,
        "adx": adx_value,
        "rsi5": rsi5,
        "rsi1": rsi1,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "extension": extension,
        "room": room,
        "confirmations": confirmations,
        "timestamp": timestamp,
    }


# ============================================================
# SEND QUALIFIED SIGNAL
# ============================================================

def send_signal(signal):

    direction = signal["direction"]

    if direction == "CALL":
        emoji = "🟢"
        direction_text = "CALL / UP"
    else:
        emoji = "🔴"
        direction_text = "PUT / DOWN"

    message = (
        f"{emoji} *NEW QUALIFIED OTC SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* `{signal['asset']}`\n"
        f"*Direction:* *{direction_text}*\n"
        f"*Score:* *{signal['score']}/100*\n"
        f"*Reference expiry:* "
        f"*{EXPIRY_MINUTES} minutes*\n"
        f"*Price:* `{signal['price']:.8f}`\n"
        f"*5M Trend:* {signal['trend']}\n"
        f"*1M Entry:* {signal['entry']}\n"
        f"*Structure:* {signal['structure']}\n"
        f"*ADX:* {signal['adx']:.1f}\n"
        f"*RSI 5M:* {signal['rsi5']:.1f}\n"
        f"*RSI 1M:* {signal['rsi1']:.1f}\n"
        f"*+DI:* {signal['plus_di']:.1f}\n"
        f"*-DI:* {signal['minus_di']:.1f}\n"
        f"*Extension:* {signal['extension']:.2f} ATR\n"
        f"*Room:* {signal['room']:.2f} ATR\n"
        f"*Confirmations:* "
        f"{signal['confirmations']}\n"
        f"*Signal ID:* `{signal['signal_id']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"READ-ONLY — NO AUTO TRADE"
    )

    telegram_send(message)


# ============================================================
# NO TRADE MESSAGE
# ============================================================

def send_no_trade(
    discovered,
    working,
    qualified,
):

    message = (
        "🟡 *IQ OPTION OTC SCANNER*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*OTC assets discovered:* {discovered}\n"
        f"*OTC candle feeds working:* {working}\n"
        f"*Qualified signals:* {qualified}\n"
        "\n"
        "The market data is available, "
        "but no setup reached the "
        f"*{MIN_SCORE}/100* threshold.\n"
        "\n"
        "*NO TRADE*\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    telegram_send(message)


# ============================================================
# CONNECTION
# ============================================================

def connect_iq():

    print("Connecting to IQ Option...")

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    check, reason = api.connect()

    if not check:

        print(
            "IQ Option connection failed:",
            reason,
        )

        telegram_send(
            "🔴 *IQ OPTION SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Connection/login failed.\n"
            f"`{reason}`"
        )

        return None

    print("IQ Option connected.")

    try:
        api.change_balance(
            BALANCE_MODE
        )

        print(
            f"Balance mode: "
            f"{BALANCE_MODE}"
        )

    except Exception as e:

        print(
            "Balance mode error:",
            e,
        )

    return api


# ============================================================
# RUN ONE SCAN CYCLE
# ============================================================

def run_scan_cycle(api):

    print(
        "\n"
        "=================================================="
    )

    print(
        "NEW SCAN CYCLE:",
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
    )

    print(
        "=================================================="
    )

    # --------------------------------------------------------
    # CHECK CONNECTION
    # --------------------------------------------------------

    try:

        if not api.check_connect():

            print(
                "IQ Option disconnected."
            )

            try:
                api.connect()
            except Exception as e:
                print(
                    "Reconnect error:",
                    e,
                )

            if not api.check_connect():

                telegram_send(
                    "🔴 *IQ OPTION SCANNER*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "IQ Option connection lost.\n"
                    "Waiting for the next cycle."
                )

                return

    except Exception as e:

        print(
            "Connection check error:",
            e,
        )

        return

    # --------------------------------------------------------
    # DISCOVER OTC
    # --------------------------------------------------------

    assets = discover_otc_assets(api)

    if not assets:

        telegram_send(
            "🟡 *IQ OPTION OTC SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No currently open OTC "
            "instruments were discovered.\n"
            "*NO TRADE*"
        )

        return

    # --------------------------------------------------------
    # TEST CANDLE FEEDS
    # --------------------------------------------------------

    working_assets = test_candle_feed(
        api,
        assets,
    )

    if not working_assets:

        telegram_send(
            "🟡 *IQ OPTION OTC SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*OTC assets discovered:* "
            f"{len(assets)}\n"
            "*OTC candle feeds working:* 0\n"
            "*Qualified signals:* 0\n"
            "\n"
            "No usable OTC candle feed "
            "was available.\n"
            "\n"
            "*NO TRADE*"
        )

        return

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    qualified = []

    for asset in working_assets:

        try:

            result = analyze_asset(
                api,
                asset,
            )

            if result is not None:

                print(
                    f"QUALIFIED: "
                    f"{asset} "
                    f"{result['direction']} "
                    f"{result['score']}/100"
                )

                qualified.append(result)

            else:

                print(
                    f"No qualified setup: "
                    f"{asset}"
                )

        except Exception as e:

            print(
                f"Analysis error "
                f"{asset}: {e}"
            )

    # --------------------------------------------------------
    # SEND SIGNALS
    # --------------------------------------------------------

    for signal in qualified:

        send_signal(signal)

        # Small delay between Telegram messages.
        time.sleep(1)

    # --------------------------------------------------------
    # NO TRADE
    # --------------------------------------------------------

    if not qualified:

        send_no_trade(
            len(assets),
            len(working_assets),
            0,
        )

    print(
        f"Cycle complete. "
        f"Qualified signals: {len(qualified)}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=================================================="
    )

    print(
        "PRECISION IQ OPTION OTC SCANNER"
    )

    print(
        "CONTINUOUS 5-MINUTE MODE"
    )

    print(
        "=================================================="
    )

    # --------------------------------------------------------
    # CREDENTIAL CHECK
    # --------------------------------------------------------

    missing = []

    if not IQ_EMAIL:
        missing.append("IQ_EMAIL")

    if not IQ_PASSWORD:
        missing.append("IQ_PASSWORD")

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:

        print(
            "Missing environment variables:",
            ", ".join(missing),
        )

        telegram_send(
            "🔴 *IQ OPTION SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Missing required credentials:\n"
            + "\n".join(
                f"• `{x}`"
                for x in missing
            )
        )

        return

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    api = connect_iq()

    if api is None:
        return

    # --------------------------------------------------------
    # CONTINUOUS LOOP
    # --------------------------------------------------------

    try:

        while True:

            cycle_started = time.time()

            try:

                run_scan_cycle(api)

            except Exception as e:

                print(
                    "Scan cycle error:",
                    e,
                )

                traceback.print_exc()

                telegram_send(
                    "🟠 *IQ OPTION SCANNER*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Scan cycle encountered "
                    "an error.\n"
                    "The scanner will continue "
                    "and retry in 5 minutes."
                )

            # ------------------------------------------------
            # WAIT EXACTLY UNTIL NEXT 5-MINUTE CYCLE
            # ------------------------------------------------

            next_scan = (
                cycle_started
                + SCAN_INTERVAL_SECONDS
            )

            wait_seconds = max(
                1,
                int(
                    next_scan - time.time()
                ),
            )

            print(
                f"Next scan in "
                f"{wait_seconds} seconds."
            )

            time.sleep(
                wait_seconds
            )

    except KeyboardInterrupt:

        print(
            "Scanner stopped manually."
        )

    except Exception as e:

        print(
            "Fatal scanner error:",
            e,
        )

        traceback.print_exc()

    finally:

        try:
            api.close()
        except Exception:
            pass

        print(
            "IQ Option connection closed."
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
