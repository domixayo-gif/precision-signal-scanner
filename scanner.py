"""
===========================================================
PRECISION SCANNER V3.5
Normal Forex + Separate OTC Framework
5M Trend + 1M Entry
5-Minute Expiry
5-Minute Per-Asset Signal Lock
Telegram Alerts
Signal Tracking
===========================================================

IMPORTANT:
- NORMAL market data: Bybit public market-data API.
- OTC market data: separate adapter. No fake OTC data is generated.
- This program DOES NOT execute Pocket Option trades.
- It only produces/records analysis signals.
"""

import os
import json
import time
import math
import html
import hashlib
from datetime import datetime, timezone

import requests


# =========================================================
# CONFIGURATION
# =========================================================

VERSION = "V3.5"

# Telegram
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))

# Tracking
TRACKER_FILE = "tracker.json"

# ---------------------------------------------------------
# MARKET MODES
# ---------------------------------------------------------

NORMAL_MODE = "NORMAL"
OTC_MODE = "OTC"

ENABLED_MODES = [
    NORMAL_MODE,
    OTC_MODE,
]

# ---------------------------------------------------------
# NORMAL MARKET - BYBIT
# ---------------------------------------------------------

BYBIT_BASE = "https://api.bybit.com"

# Current Bybit FX perpetual symbols
NORMAL_SYMBOLS = {
    "EURUSD": "EURUSDUSDT",
    "GBPUSD": "GBPUSDUSDT",
    "USDJPY": "USDJPYUSDT",
}

# Optional additional instruments if supported by your account
# Keep disabled until verified.
# "XAUUSD": "XAUUSDUSDT",

BYBIT_CATEGORY = "linear"

# ---------------------------------------------------------
# OTC
# ---------------------------------------------------------

# IMPORTANT:
# Do NOT put a random Pocket Option endpoint here.
#
# OTC data must come from a legitimate candle source that
# actually represents the OTC instrument.
#
# When an OTC feed is available, implement:
#
#     get_otc_candles(symbol, interval)
#
# and return the same candle structure used by
# get_bybit_candles().
#
OTC_ENABLED = False

OTC_SYMBOLS = {
    "EURUSD_OTC": "EUR/USD OTC",
    "GBPUSD_OTC": "GBP/USD OTC",
    "USDJPY_OTC": "USD/JPY OTC",
}

# ---------------------------------------------------------
# STRATEGY
# ---------------------------------------------------------

MAIN_TIMEFRAME = "5"
ENTRY_TIMEFRAME = "1"

REFERENCE_EXPIRY_MINUTES = 5

MIN_SCORE = 80
BORDERLINE_SCORE = 75

# The important new V3.5 rule:
SIGNAL_LOCK_SECONDS = REFERENCE_EXPIRY_MINUTES * 60

# Scanner cycle
SCAN_INTERVAL_SECONDS = 60

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

# Data
CANDLE_LIMIT = 220

# Telegram
TELEGRAM_MESSAGE_LIMIT = 3900

# Heartbeat
HEARTBEAT_EVERY_SCANS = 15

# Tracker limits
MAX_TRACKER_ITEMS = 1000


# =========================================================
# HTTP SESSION
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "PrecisionScanner/3.5",
    "Accept": "application/json",
})


# =========================================================
# TIME HELPERS
# =========================================================

def now_ts():
    return int(time.time())


def utc_now():
    return datetime.now(timezone.utc)


def format_utc(ts):
    if not ts:
        return "N/A"

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def candle_id(ts):
    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime("%H%M%S")


# =========================================================
# TELEGRAM
# =========================================================

def telegram_request(method, payload=None):
    if not TELEGRAM_TOKEN:
        return None

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )

    try:
        response = session.post(
            url,
            json=payload or {},
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        return response.json()

    except Exception as exc:
        print("Telegram error:", exc)
        return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n--- TELEGRAM DISABLED ---")
        print(message)
        print("-------------------------\n")
        return False

    chunks = [
        message[i:i + TELEGRAM_MESSAGE_LIMIT]
        for i in range(
            0,
            len(message),
            TELEGRAM_MESSAGE_LIMIT
        )
    ]

    success = True

    for chunk in chunks:

        result = telegram_request(
            "sendMessage",
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        )

        if not result or not result.get("ok"):
            success = False

    return success


# =========================================================
# TRACKER
# =========================================================

def default_tracker():
    return {
        "signals": [],
        "offset": 0,

        "meta": {
            "last_scan": None,
            "last_signal": None,
            "last_heartbeat": None,

            # New V3.5 persistent locks
            #
            # Example:
            # {
            #     "NORMAL:EURUSD": 1760000000,
            #     "OTC:EURUSD_OTC": 1760000300
            # }
            "signal_locks": {},

            "alerted_keys": [],
        }
    }


def load_tracker():

    if not os.path.exists(TRACKER_FILE):
        return default_tracker()

    try:

        with open(
            TRACKER_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(data, list):
            result = default_tracker()
            result["signals"] = data
            return result

        result = default_tracker()

        if isinstance(data, dict):

            result.update({
                k: v
                for k, v in data.items()
                if k in result
            })

            if isinstance(
                data.get("meta"),
                dict
            ):

                result["meta"].update(
                    data["meta"]
                )

        return result

    except Exception as exc:

        print(
            "Tracker load error:",
            exc
        )

        return default_tracker()


def save_tracker(data):

    data["signals"] = data.get(
        "signals",
        []
    )[-MAX_TRACKER_ITEMS:]

    meta = data.setdefault(
        "meta",
        {}
    )

    meta["alerted_keys"] = meta.get(
        "alerted_keys",
        []
    )[-MAX_TRACKER_ITEMS:]

    tmp = TRACKER_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(
        tmp,
        TRACKER_FILE
    )


# =========================================================
# GENERIC HTTP GET
# =========================================================

def http_get_json(
    url,
    params=None
):

    for attempt in range(
        MAX_RETRIES
    ):

        try:

            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 429:

                wait = 2 ** attempt

                print(
                    f"Rate limited. "
                    f"Waiting {wait}s..."
                )

                time.sleep(wait)
                continue

            response.raise_for_status()

            return response.json()

        except Exception as exc:

            print(
                f"HTTP attempt "
                f"{attempt + 1}: {exc}"
            )

            if attempt < MAX_RETRIES - 1:
                time.sleep(
                    1.5 * (attempt + 1)
                )

    return None


# =========================================================
# BYBIT NORMAL MARKET
# =========================================================

def get_bybit_candles(
    symbol,
    interval
):

    params = {
        "category": BYBIT_CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": CANDLE_LIMIT,
    }

    data = http_get_json(
        f"{BYBIT_BASE}/v5/market/kline",
        params
    )

    if not data:
        return []

    if data.get("retCode") != 0:
        print(
            "Bybit error:",
            data
        )
        return []

    rows = (
        data
        .get("result", {})
        .get("list", [])
    )

    candles = []

    for row in rows:

        try:

            # Bybit format:
            # [startTime, open, high, low,
            #  close, volume, turnover]

            ts = int(
                int(row[0]) / 1000
            )

            candles.append({
                "time": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        except Exception:
            continue

    candles.sort(
        key=lambda x: x["time"]
    )

    # Remove currently forming candle.
    current = now_ts()

    seconds = (
        60
        if interval == "1"
        else 300
    )

    candles = [
        c
        for c in candles
        if c["time"] + seconds <= current
    ]

    return candles


# =========================================================
# OTC ADAPTER
# =========================================================

def get_otc_candles(
    symbol,
    interval
):
    """
    OTC DATA ADAPTER

    This intentionally returns [].

    Why?

    Pocket Option's OTC prices should not be
    substituted with ordinary EUR/USD prices.

    When you have a legitimate OTC candle feed,
    replace this function with an adapter that
    returns:

    [
        {
            "time": 1234567890,
            "open": 1.1234,
            "high": 1.1240,
            "low": 1.1228,
            "close": 1.1238,
            "volume": 0
        }
    ]

    The rest of V3.5 can then use it automatically.
    """

    if not OTC_ENABLED:
        return []

    # -----------------------------------------------------
    # PLACEHOLDER
    # -----------------------------------------------------
    #
    # DO NOT invent an endpoint here.
    #
    # Example structure once a legitimate provider
    # is selected:
    #
    # response = http_get_json(
    #     OTC_PROVIDER_URL,
    #     {
    #         "symbol": symbol,
    #         "interval": interval
    #     }
    # )
    #
    # Parse response into the candle format above.
    #

    return []


# =========================================================
# UNIFIED DATA ROUTER
# =========================================================

def get_candles(
    mode,
    symbol,
    interval
):

    if mode == NORMAL_MODE:

        bybit_symbol = NORMAL_SYMBOLS.get(
            symbol
        )

        if not bybit_symbol:
            return []

        return get_bybit_candles(
            bybit_symbol,
            interval
        )

    if mode == OTC_MODE:

        return get_otc_candles(
            symbol,
            interval
        )

    return []


# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):

    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    multiplier = 2 / (
        period + 1
    )

    initial = sum(
        values[:period]
    ) / period

    result[period - 1] = initial

    previous = initial

    for i in range(
        period,
        len(values)
    ):

        current = (
            values[i] * multiplier
            + previous * (1 - multiplier)
        )

        result[i] = current
        previous = current

    return result


def rsi(values, period=14):

    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i] - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period]) / period
    )

    avg_loss = (
        sum(losses[:period]) / period
    )

    if avg_loss == 0:
        result[period] = 100
    else:
        rs = avg_gain / avg_loss
        result[period] = (
            100 - 100 / (1 + rs)
        )

    for i in range(
        period + 1,
        len(values)
    ):

        gain = gains[i - 1]
        loss = losses[i - 1]

        avg_gain = (
            (avg_gain * (period - 1) + gain)
            / period
        )

        avg_loss = (
            (avg_loss * (period - 1) + loss)
            / period
        )

        if avg_loss == 0:
            result[i] = 100
        else:

            rs = (
                avg_gain / avg_loss
            )

            result[i] = (
                100 - 100 / (1 + rs)
            )

    return result


def true_range(candles):

    result = []

    for i, c in enumerate(candles):

        if i == 0:

            tr = (
                c["high"] - c["low"]
            )

        else:

            previous_close = (
                candles[i - 1]["close"]
            )

            tr = max(
                c["high"] - c["low"],
                abs(
                    c["high"]
                    - previous_close
                ),
                abs(
                    c["low"]
                    - previous_close
                )
            )

        result.append(tr)

    return result


def atr(candles, period=14):

    tr = true_range(candles)

    result = [None] * len(
        candles
    )

    if len(tr) < period:
        return result

    value = (
        sum(tr[:period]) / period
    )

    result[period - 1] = value

    for i in range(
        period,
        len(tr)
    ):

        value = (
            (value * (period - 1))
            + tr[i]
        ) / period

        result[i] = value

    return result


def macd(values):

    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    line = [None] * len(values)

    for i in range(
        len(values)
    ):

        if (
            ema12[i] is not None
            and ema26[i] is not None
        ):

            line[i] = (
                ema12[i] - ema26[i]
            )

    clean = [
        x for x in line
        if x is not None
    ]

    signal_clean = ema(
        clean,
        9
    )

    signal = [None] * len(values)

    start = (
        len(values)
        - len(clean)
    )

    for i, value in enumerate(
        signal_clean
    ):

        signal[
            start + i
        ] = value

    histogram = [None] * len(values)

    for i in range(
        len(values)
    ):

        if (
            line[i] is not None
            and signal[i] is not None
        ):

            histogram[i] = (
                line[i] - signal[i]
            )

    return line, signal, histogram


def adx(candles, period=14):

    if len(candles) < period + 2:
        return (
            [None] * len(candles),
            [None] * len(candles),
            [None] * len(candles),
        )

    tr = [0]
    plus_dm = [0]
    minus_dm = [0]

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        up_move = (
            current["high"]
            - previous["high"]
        )

        down_move = (
            previous["low"]
            - current["low"]
        )

        plus = (
            up_move
            if up_move > down_move
            and up_move > 0
            else 0
        )

        minus = (
            down_move
            if down_move > up_move
            and down_move > 0
            else 0
        )

        tr.append(
            max(
                current["high"]
                - current["low"],
                abs(
                    current["high"]
                    - previous["close"]
                ),
                abs(
                    current["low"]
                    - previous["close"]
                )
            )
        )

        plus_dm.append(plus)
        minus_dm.append(minus)

    adx_values = [None] * len(candles)
    plus_di = [None] * len(candles)
    minus_di = [None] * len(candles)

    tr_avg = (
        sum(tr[1:period + 1])
        / period
    )

    plus_avg = (
        sum(plus_dm[1:period + 1])
        / period
    )

    minus_avg = (
        sum(minus_dm[1:period + 1])
        / period
    )

    dx_values = []

    for i in range(
        period,
        len(candles)
    ):

        if i > period:

            tr_avg = (
                (
                    tr_avg
                    * (period - 1)
                )
                + tr[i]
            ) / period

            plus_avg = (
                (
                    plus_avg
                    * (period - 1)
                )
                + plus_dm[i]
            ) / period

            minus_avg = (
                (
                    minus_avg
                    * (period - 1)
                )
                + minus_dm[i]
            ) / period

        if tr_avg == 0:
            pdi = 0
            mdi = 0
        else:

            pdi = (
                100
                * plus_avg
                / tr_avg
            )

            mdi = (
                100
                * minus_avg
                / tr_avg
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

        dx_values.append(dx)

        if len(dx_values) == period:

            adx_value = (
                sum(dx_values)
                / period
            )

            adx_values[i] = adx_value

        elif len(dx_values) > period:

            previous = (
                adx_values[i - 1]
            )

            if previous is not None:

                adx_values[i] = (
                    (
                        previous
                        * (period - 1)
                    )
                    + dx
                ) / period

    return (
        adx_values,
        plus_di,
        minus_di,
    )


# =========================================================
# PRICE ACTION
# =========================================================

def candle_direction(candle):

    if candle["close"] > candle["open"]:
        return "BULLISH"

    if candle["close"] < candle["open"]:
        return "BEARISH"

    return "NEUTRAL"


def candle_strength(candle):

    rng = (
        candle["high"]
        - candle["low"]
    )

    if rng <= 0:
        return 0

    body = abs(
        candle["close"]
        - candle["open"]
    )

    return body / rng


def structure_direction(candles):

    if len(candles) < 10:
        return "NEUTRAL"

    recent = candles[-5:]
    previous = candles[-10:-5]

    recent_high = max(
        c["high"]
        for c in recent
    )

    previous_high = max(
        c["high"]
        for c in previous
    )

    recent_low = min(
        c["low"]
        for c in recent
    )

    previous_low = min(
        c["low"]
        for c in previous
    )

    if (
        recent_high > previous_high
        and recent_low > previous_low
    ):

        return "BULLISH"

    if (
        recent_high < previous_high
        and recent_low < previous_low
    ):

        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# SCORING
# =========================================================

def extension_score(
    price,
    atr_value,
    ema20
):

    if (
        atr_value is None
        or atr_value <= 0
    ):
        return 0

    distance = abs(
        price - ema20
    )

    multiple = (
        distance / atr_value
    )

    if multiple <= 0.8:
        return 5

    if multiple <= 1.2:
        return 4

    if multiple <= 1.6:
        return 3

    if multiple <= 2.2:
        return 2

    return 0


def room_score(
    price,
    candles,
    direction,
    atr_value
):

    if (
        atr_value is None
        or atr_value <= 0
    ):
        return 0

    lookback = candles[-30:]

    if direction == "CALL":

        resistance = max(
            c["high"]
            for c in lookback
        )

        room = (
            resistance - price
        )

    else:

        support = min(
            c["low"]
            for c in lookback
        )

        room = (
            price - support
        )

    multiple = (
        room / atr_value
    )

    if multiple >= 2:
        return 5

    if multiple >= 1.5:
        return 4

    if multiple >= 1:
        return 3

    if multiple >= 0.5:
        return 1

    return 0


# =========================================================
# ANALYSIS
# =========================================================

def analyze_asset(
    mode,
    symbol
):

    main = get_candles(
        mode,
        symbol,
        MAIN_TIMEFRAME
    )

    entry = get_candles(
        mode,
        symbol,
        ENTRY_TIMEFRAME
    )

    if (
        len(main) < 100
        or len(entry) < 100
    ):

        return {
            "mode": mode,
            "symbol": symbol,
            "qualified": False,
            "borderline": False,
            "signal": "NO TRADE",
            "reason": "INSUFFICIENT DATA",
        }

    main_close = [
        c["close"]
        for c in main
    ]

    entry_close = [
        c["close"]
        for c in entry
    ]

    main_ema20 = ema(
        main_close,
        20
    )

    main_ema50 = ema(
        main_close,
        50
    )

    entry_ema9 = ema(
        entry_close,
        9
    )

    entry_ema21 = ema(
        entry_close,
        21
    )

    main_rsi = rsi(
        main_close
    )

    entry_rsi = rsi(
        entry_close
    )

    main_atr = atr(
        main
    )

    (
        adx_values,
        plus_di,
        minus_di
    ) = adx(main)

    (
        macd_line,
        macd_signal,
        macd_hist
    ) = macd(main_close)

    i = len(main) - 1
    e = len(entry) - 1

    price = entry_close[e]

    # -----------------------------------------------------
    # MAIN TREND
    # -----------------------------------------------------

    if (
        price > main_ema50[i]
        and main_ema20[i] > main_ema50[i]
    ):

        major_trend = "BULLISH"

    elif (
        price < main_ema50[i]
        and main_ema20[i] < main_ema50[i]
    ):

        major_trend = "BEARISH"

    else:

        major_trend = "NEUTRAL"

    # -----------------------------------------------------
    # ENTRY TREND
    # -----------------------------------------------------

    if (
        entry_ema9[e] is not None
        and entry_ema21[e] is not None
    ):

        if entry_ema9[e] > entry_ema21[e]:
            entry_trend = "BULLISH"

        elif entry_ema9[e] < entry_ema21[e]:
            entry_trend = "BEARISH"

        else:
            entry_trend = "NEUTRAL"

    else:

        entry_trend = "NEUTRAL"

    # -----------------------------------------------------
    # STRUCTURE
    # -----------------------------------------------------

    structure = structure_direction(
        main
    )

    # -----------------------------------------------------
    # DIRECTION VOTES
    # -----------------------------------------------------

    bullish_votes = 0
    bearish_votes = 0

    if major_trend == "BULLISH":
        bullish_votes += 1

    elif major_trend == "BEARISH":
        bearish_votes += 1

    if entry_trend == "BULLISH":
        bullish_votes += 1

    elif entry_trend == "BEARISH":
        bearish_votes += 1

    if structure == "BULLISH":
        bullish_votes += 1

    elif structure == "BEARISH":
        bearish_votes += 1

    if (
        plus_di[i] is not None
        and minus_di[i] is not None
    ):

        if plus_di[i] > minus_di[i]:
            bullish_votes += 1

        elif minus_di[i] > plus_di[i]:
            bearish_votes += 1

    if macd_hist[i] is not None:

        if macd_hist[i] > 0:
            bullish_votes += 1

        elif macd_hist[i] < 0:
            bearish_votes += 1

    latest_entry_candle = entry[-1]

    candle_dir = candle_direction(
        latest_entry_candle
    )

    if candle_dir == "BULLISH":
        bullish_votes += 1

    elif candle_dir == "BEARISH":
        bearish_votes += 1

    if bullish_votes > bearish_votes:
        signal = "CALL"
    elif bearish_votes > bullish_votes:
        signal = "PUT"
    else:
        signal = "NO TRADE"

    # -----------------------------------------------------
    # SCORE
    # -----------------------------------------------------

    components = {}

    # Trend / 20
    if (
        signal == "CALL"
        and major_trend == "BULLISH"
    ) or (
        signal == "PUT"
        and major_trend == "BEARISH"
    ):

        components["Trend"] = 20

    elif major_trend != "NEUTRAL":

        components["Trend"] = 10

    else:

        components["Trend"] = 0

    # Structure / 10
    if (
        signal == "CALL"
        and structure == "BULLISH"
    ) or (
        signal == "PUT"
        and structure == "BEARISH"
    ):

        components["Structure"] = 10

    elif structure != "NEUTRAL":

        components["Structure"] = 5

    else:

        components["Structure"] = 0

    # ADX / DMI / 10
    adx_value = adx_values[i]

    if adx_value is None:

        components["ADX/DMI"] = 0

    elif adx_value >= 25:

        if (
            signal == "CALL"
            and plus_di[i] > minus_di[i]
        ) or (
            signal == "PUT"
            and minus_di[i] > plus_di[i]
        ):

            components["ADX/DMI"] = 10

        else:

            components["ADX/DMI"] = 4

    elif adx_value >= 20:

        components["ADX/DMI"] = 7

    elif adx_value >= 17:

        components["ADX/DMI"] = 4

    else:

        components["ADX/DMI"] = 0

    # MACD / 10
    if macd_hist[i] is not None:

        macd_good = (
            signal == "CALL"
            and macd_hist[i] > 0
        ) or (
            signal == "PUT"
            and macd_hist[i] < 0
        )

        components["MACD"] = (
            10 if macd_good else 0
        )

    else:

        components["MACD"] = 0

    # RSI / 10
    rsi_value = main_rsi[i]

    if rsi_value is None:

        components["RSI"] = 0

    elif signal == "CALL":

        if 45 <= rsi_value <= 65:
            components["RSI"] = 10
        elif 35 <= rsi_value <= 70:
            components["RSI"] = 6
        else:
            components["RSI"] = 2

    else:

        if 35 <= rsi_value <= 55:
            components["RSI"] = 10
        elif 30 <= rsi_value <= 65:
            components["RSI"] = 6
        else:
            components["RSI"] = 2

    # Entry / 15
    if (
        signal == "CALL"
        and entry_trend == "BULLISH"
    ) or (
        signal == "PUT"
        and entry_trend == "BEARISH"
    ):

        components["Entry"] = 15

    else:

        components["Entry"] = 0

    # Pullback / 10
    recent_entry = entry[-6:]

    pullback_score = 0

    if signal == "CALL":

        had_pullback = any(
            c["close"] < c["open"]
            for c in recent_entry[:-1]
        )

        if (
            had_pullback
            and candle_dir == "BULLISH"
        ):

            pullback_score = 10

        elif candle_dir == "BULLISH":

            pullback_score = 5

    elif signal == "PUT":

        had_pullback = any(
            c["close"] > c["open"]
            for c in recent_entry[:-1]
        )

        if (
            had_pullback
            and candle_dir == "BEARISH"
        ):

            pullback_score = 10

        elif candle_dir == "BEARISH":

            pullback_score = 5

    components["Pullback"] = pullback_score

    # Candle / 5
    strength = candle_strength(
        latest_entry_candle
    )

    if (
        (
            signal == "CALL"
            and candle_dir == "BULLISH"
        )
        or
        (
            signal == "PUT"
            and candle_dir == "BEARISH"
        )
    ):

        if strength >= 0.65:
            components["Candle"] = 5
        elif strength >= 0.45:
            components["Candle"] = 4
        else:
            components["Candle"] = 2

    else:

        components["Candle"] = 0

    # Room / 5
    room = room_score(
        price,
        main,
        signal,
        main_atr[i]
    )

    components["Room"] = room

    # Extension / 5
    ext = extension_score(
        price,
        main_atr[i],
        main_ema20[i]
    )

    components["Extension"] = ext

    score = sum(
        components.values()
    )

    # -----------------------------------------------------
    # BLOCKERS
    # -----------------------------------------------------

    blockers = []

    if (
        major_trend != "NEUTRAL"
        and (
            (
                signal == "CALL"
                and major_trend != "BULLISH"
            )
            or
            (
                signal == "PUT"
                and major_trend != "BEARISH"
            )
        )
    ):

        blockers.append(
            "MAJOR TREND MISMATCH"
        )

    if adx_value is not None:

        if adx_value < 15:

            blockers.append(
                "ADX TOO LOW"
            )

        elif (
            adx_value < 17
            and main_ema50[i]
            and main_ema20[i]
        ):

            gap = abs(
                main_ema20[i]
                - main_ema50[i]
            ) / price * 100

            if gap < 0.10:

                blockers.append(
                    "WEAK TREND"
                )

    if room <= 1:

        blockers.append(
            "INSUFFICIENT ROOM"
        )

    # Extension blocker
    if (
        main_atr[i] is not None
        and main_atr[i] > 0
        and main_ema20[i] is not None
    ):

        extension_multiple = (
            abs(price - main_ema20[i])
            / main_atr[i]
        )

        if extension_multiple > 2.2:

            blockers.append(
                "EXTENDED > 2.2 ATR"
            )

    # Opposing structure
    if (
        structure != "NEUTRAL"
        and (
            (
                signal == "CALL"
                and structure == "BEARISH"
            )
            or
            (
                signal == "PUT"
                and structure == "BULLISH"
            )
        )
    ):

        blockers.append(
            "OPPOSING STRUCTURE"
        )

    # -----------------------------------------------------
    # QUALIFICATION
    # -----------------------------------------------------

    dominance = abs(
        bullish_votes
        - bearish_votes
    )

    qualified = (
        signal in ("CALL", "PUT")
        and score >= MIN_SCORE
        and dominance >= 2
        and len(blockers) == 0
    )

    borderline = (
        signal in ("CALL", "PUT")
        and BORDERLINE_SCORE <= score < MIN_SCORE
        and len(blockers) == 0
    )

    # If not qualified, don't call it a trade signal.
    final_signal = (
        signal
        if qualified
        else "NO TRADE"
    )

    entry_time = entry[-1]["time"]

    signal_id = (
        f"{symbol}-"
        f"{final_signal}-"
        f"{candle_id(entry_time)}"
    )

    return {
        "version": VERSION,
        "mode": mode,
        "symbol": symbol,

        "signal": final_signal,

        "raw_direction": signal,

        "qualified": qualified,
        "borderline": borderline,

        "score": score,
        "minimum_score": MIN_SCORE,

        "bullish_votes": bullish_votes,
        "bearish_votes": bearish_votes,
        "dominance": dominance,

        "price": price,

        "major_trend": major_trend,
        "entry_trend": entry_trend,
        "structure": structure,

        "adx": adx_value,
        "rsi": rsi_value,

        "components": components,
        "blockers": blockers,

        "entry_candle_time": entry_time,

        "main_candle_time": main[-1]["time"],

        "signal_id": signal_id,

        "scanned_at": now_ts(),
    }


# =========================================================
# SIGNAL DEDUPLICATION
# =========================================================

def dedupe_key(result):

    return "|".join([
        result["mode"],
        result["symbol"],
        result["signal"],
        str(
            result["entry_candle_time"]
        )
    ])


def was_alerted(
    tracker,
    key
):

    return key in tracker[
        "meta"
    ].get(
        "alerted_keys",
        []
    )


def mark_alerted(
    tracker,
    key
):

    arr = tracker[
        "meta"
    ].setdefault(
        "alerted_keys",
        []
    )

    if key not in arr:

        arr.append(key)

        tracker[
            "meta"
        ]["alerted_keys"] = arr[
            -MAX_TRACKER_ITEMS:
        ]


# =========================================================
# V3.5 SIGNAL LOCK
# =========================================================

def asset_lock_key(
    mode,
    symbol
):

    return f"{mode}:{symbol}"


def get_lock_until(
    tracker,
    mode,
    symbol
):

    locks = tracker[
        "meta"
    ].setdefault(
        "signal_locks",
        {}
    )

    return int(
        locks.get(
            asset_lock_key(
                mode,
                symbol
            ),
            0
        )
    )


def is_locked(
    tracker,
    mode,
    symbol
):

    return (
        now_ts()
        < get_lock_until(
            tracker,
            mode,
            symbol
        )
    )


def set_signal_lock(
    tracker,
    mode,
    symbol,
    signal_time
):

    locks = tracker[
        "meta"
    ].setdefault(
        "signal_locks",
        {}
    )

    # Lock begins from the signal candle time.
    # It lasts exactly 5 minutes.
    lock_until = (
        int(signal_time)
        + SIGNAL_LOCK_SECONDS
    )

    locks[
        asset_lock_key(
            mode,
            symbol
        )
    ] = lock_until

    return lock_until


# =========================================================
# TRACK SIGNAL
# =========================================================

def add_signal(
    tracker,
    result
):

    record = {
        **result,

        "result": "PENDING",

        "alert_sent": False,

        "created_at": now_ts(),

        "expiry_minutes":
            REFERENCE_EXPIRY_MINUTES,
    }

    tracker[
        "signals"
    ].append(record)

    return record


# =========================================================
# ALERT MESSAGE
# =========================================================

def build_signal_alert(
    result,
    lock_until
):

    direction = result["signal"]

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    components = result[
        "components"
    ]

    reasons = []

    for name, value in components.items():

        if value > 0:

            reasons.append(
                f"{name}: {value}"
            )

    reasons_text = "\n".join(
        reasons
    )

    return f"""
<b>⚡ PRECISION SCANNER {VERSION}</b>

<b>{emoji} {direction}</b>

<b>Market:</b> {html.escape(result["mode"])}
<b>Asset:</b> {html.escape(result["symbol"])}

<b>Score:</b> {result["score"]}/100
<b>Reference expiry:</b> {REFERENCE_EXPIRY_MINUTES} minutes

<b>Price:</b> {result["price"]}

<b>5M Trend:</b> {result["major_trend"]}
<b>1M Entry:</b> {result["entry_trend"]}
<b>Structure:</b> {result["structure"]}

<b>ADX:</b> {result["adx"]:.2f}
<b>RSI:</b> {result["rsi"]:.2f}

<b>Score breakdown:</b>
{reasons_text}

<b>Signal ID:</b>
<code>{result["signal_id"]}</code>

<b>Signal candle:</b>
{format_utc(result["entry_candle_time"])}

<b>🔒 New V3.5 lock:</b>
No new alert for this asset until:
{format_utc(lock_until)}

This is an analysis signal, not an automatic trade.
"""


# =========================================================
# HEARTBEAT
# =========================================================

def build_heartbeat(
    tracker,
    scan_number
):

    signals = tracker.get(
        "signals",
        []
    )

    pending = sum(
        1
        for s in signals
        if s.get("result") == "PENDING"
    )

    wins = sum(
        1
        for s in signals
        if s.get("result") == "WIN"
    )

    losses = sum(
        1
        for s in signals
        if s.get("result") == "LOSS"
    )

    return f"""
<b>💓 PRECISION SCANNER {VERSION}</b>

<b>Status:</b> RUNNING

<b>Scan:</b> {scan_number}

<b>Normal market:</b>
{len(NORMAL_SYMBOLS)} assets configured

<b>OTC framework:</b>
{"ENABLED" if OTC_ENABLED else "WAITING FOR OTC FEED"}

<b>Tracked:</b> {len(signals)}

<b>Pending:</b> {pending}
<b>Wins:</b> {wins}
<b>Losses:</b> {losses}

<b>Expiry:</b> {REFERENCE_EXPIRY_MINUTES} minutes

<b>Asset lock:</b>
{SIGNAL_LOCK_SECONDS} seconds

The scanner does not force signals.
"""


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def process_commands(
    tracker
):

    offset = tracker.get(
        "offset",
        0
    )

    response = telegram_request(
        "getUpdates",
        {
            "offset": offset + 1,
            "timeout": 1,
            "allowed_updates": [
                "message"
            ],
        }
    )

    if not response:
        return False

    updates = response.get(
        "result",
        []
    )

    changed = False

    for update in updates:

        update_id = update.get(
            "update_id"
        )

        if update_id is not None:

            tracker["offset"] = update_id
            changed = True

        message = update.get(
            "message"
        )

        if not message:
            continue

        text = (
            message.get(
                "text",
                ""
            )
            .strip()
        )

        chat_id = str(
            message.get(
                "chat",
                {}
            ).get(
                "id",
                ""
            )
        )

        if (
            TELEGRAM_CHAT_ID
            and chat_id != TELEGRAM_CHAT_ID
        ):
            continue

        if text == "/start":

            send_telegram(
                f"""
<b>PRECISION SCANNER {VERSION}</b>

Scanner is online.

Commands:

/scan
/stats
/help

Signals are tracked automatically.
"""
            )

        elif text == "/help":

            send_telegram(
                """
<b>Commands</b>

/scan — request a scan
/stats — tracker statistics
/win SIGNAL-ID — record win
/loss SIGNAL-ID — record loss
"""
            )

        elif text == "/scan":

            send_telegram(
                "🔎 Manual scan requested. "
                "The next scan cycle will analyze the markets."
            )

        elif text == "/stats":

            send_telegram(
                stats_text(tracker)
            )

        elif text.startswith("/win "):

            signal_id = text[5:].strip()

            if mark_result(
                tracker,
                signal_id,
                "WIN"
            ):

                send_telegram(
                    f"✅ Recorded WIN: "
                    f"<code>{html.escape(signal_id)}</code>"
                )

                changed = True

            else:

                send_telegram(
                    "Signal ID not found."
                )

        elif text.startswith("/loss "):

            signal_id = text[6:].strip()

            if mark_result(
                tracker,
                signal_id,
                "LOSS"
            ):

                send_telegram(
                    f"❌ Recorded LOSS: "
                    f"<code>{html.escape(signal_id)}</code>"
                )

                changed = True

            else:

                send_telegram(
                    "Signal ID not found."
                )

    return changed


# =========================================================
# RESULT TRACKING
# =========================================================

def mark_result(
    tracker,
    signal_id,
    result
):

    for signal in reversed(
        tracker.get(
            "signals",
            []
        )
    ):

        if (
            signal.get("signal_id")
            == signal_id
        ):

            signal["result"] = result
            signal["result_time"] = now_ts()

            return True

    return False


def completed_signals(
    tracker
):

    return [
        s
        for s in tracker.get(
            "signals",
            []
        )
        if s.get("result")
        in ("WIN", "LOSS")
    ]


def stats_text(
    tracker
):

    completed = completed_signals(
        tracker
    )

    wins = sum(
        s["result"] == "WIN"
        for s in completed
    )

    losses = sum(
        s["result"] == "LOSS"
        for s in completed
    )

    total = wins + losses

    if total:
        rate = (
            wins / total
        ) * 100
    else:
        rate = 0

    return f"""
<b>📊 PRECISION SCANNER STATS</b>

Completed: {total}

Wins: {wins}
Losses: {losses}

Recorded win rate:
{rate:.1f}%

Pending:
{len(tracker.get("signals", [])) - total}

<b>Important:</b>
This percentage is based only on
recorded results. It is not a prediction
of future performance.
"""


# =========================================================
# SCAN CYCLE
# =========================================================

def run_scan_cycle(
    tracker
):

    qualified = []
    borderline = []
    rejected = []

    errors = []

    # -----------------------------------------------------
    # NORMAL MARKET
    # -----------------------------------------------------

    for symbol in NORMAL_SYMBOLS:

        try:

            result = analyze_asset(
                NORMAL_MODE,
                symbol
            )

            process_result(
                tracker,
                result,
                qualified,
                borderline,
                rejected
            )

        except Exception as exc:

            print(
                f"Normal {symbol} error:",
                exc
            )

            errors.append(
                f"NORMAL {symbol}"
            )

    # -----------------------------------------------------
    # OTC MARKET
    # -----------------------------------------------------

    for symbol in OTC_SYMBOLS:

        try:

            result = analyze_asset(
                OTC_MODE,
                symbol
            )

            process_result(
                tracker,
                result,
                qualified,
                borderline,
                rejected
            )

        except Exception as exc:

            print(
                f"OTC {symbol} error:",
                exc
            )

            errors.append(
                f"OTC {symbol}"
            )

    tracker[
        "meta"
    ]["last_scan"] = now_ts()

    return {
        "qualified": qualified,
        "borderline": borderline,
        "rejected": rejected,
        "errors": errors,
    }


# =========================================================
# PROCESS ANALYSIS RESULT
# =========================================================

def process_result(
    tracker,
    result,
    qualified,
    borderline,
    rejected
):

    if result.get("borderline"):

        borderline.append(result)

    elif not result.get("qualified"):

        rejected.append(result)

        return

    # Only qualified signals reach this point.
    qualified.append(result)

    mode = result["mode"]
    symbol = result["symbol"]

    key = dedupe_key(
        result
    )

    # -----------------------------------------------------
    # NEVER SEND THE SAME CANDLE TWICE
    # -----------------------------------------------------

    if was_alerted(
        tracker,
        key
    ):

        return

    # -----------------------------------------------------
    # V3.5 FIVE-MINUTE ASSET LOCK
    # -----------------------------------------------------

    if is_locked(
        tracker,
        mode,
        symbol
    ):

        lock_until = get_lock_until(
            tracker,
            mode,
            symbol
        )

        print(
            f"LOCKED: "
            f"{mode} {symbol} "
            f"until {format_utc(lock_until)}"
        )

        return

    # -----------------------------------------------------
    # RECORD NEW SIGNAL
    # -----------------------------------------------------

    record = add_signal(
        tracker,
        result
    )

    # -----------------------------------------------------
    # SET FIVE-MINUTE LOCK
    # -----------------------------------------------------

    lock_until = set_signal_lock(
        tracker,
        mode,
        symbol,
        result["entry_candle_time"]
    )

    # -----------------------------------------------------
    # SEND ALERT
    # -----------------------------------------------------

    message = build_signal_alert(
        result,
        lock_until
    )

    sent = send_telegram(
        message
    )

    record[
        "alert_sent"
    ] = sent

    if sent:

        mark_alerted(
            tracker,
            key
        )

        tracker[
            "meta"
        ]["last_signal"] = (
            result["signal_id"]
        )

        print(
            f"ALERT SENT: "
            f"{mode} "
            f"{symbol} "
            f"{result['signal']} "
            f"{result['score']}/100"
        )

    else:

        print(
            f"Signal recorded but "
            f"Telegram delivery failed: "
            f"{result['signal_id']}"
        )


# =========================================================
# STARTUP
# =========================================================

def startup_message():

    return f"""
<b>🚀 PRECISION SCANNER {VERSION}</b>

Scanner engine started.

<b>Normal:</b> Bybit FX market data

<b>OTC:</b>
Separate OTC data framework
{"ACTIVE" if OTC_ENABLED else "NOT CONNECTED"}

<b>Strategy:</b>
5M trend + 1M confirmation

<b>Minimum score:</b>
{MIN_SCORE}/100

<b>Reference expiry:</b>
{REFERENCE_EXPIRY_MINUTES} minutes

<b>Signal lock:</b>
{SIGNAL_LOCK_SECONDS} seconds

No automatic Pocket Option trades are executed.
"""


# =========================================================
# MAIN LOOP
# =========================================================

def main():

    print(
        f"Precision Scanner {VERSION}"
    )

    tracker = load_tracker()

    send_telegram(
        startup_message()
    )

    scan_number = 0

    while True:

        try:

            # -------------------------------------------------
            # TELEGRAM COMMANDS
            # -------------------------------------------------

            changed = process_commands(
                tracker
            )

            if changed:
                save_tracker(
                    tracker
                )

            # -------------------------------------------------
            # SCAN
            # -------------------------------------------------

            scan_number += 1

            print(
                "\n"
                f"========== SCAN "
                f"{scan_number} =========="
            )

            results = run_scan_cycle(
                tracker
            )

            # -------------------------------------------------
            # SAVE
            # -------------------------------------------------

            save_tracker(
                tracker
            )

            # -------------------------------------------------
            # HEARTBEAT
            # -------------------------------------------------

            if (
                scan_number
                % HEARTBEAT_EVERY_SCANS
                == 0
            ):

                heartbeat = (
                    build_heartbeat(
                        tracker,
                        scan_number
                    )
                )

                send_telegram(
                    heartbeat
                )

                tracker[
                    "meta"
                ]["last_heartbeat"] = now_ts()

                save_tracker(
                    tracker
                )

            # -------------------------------------------------
            # CONSOLE SUMMARY
            # -------------------------------------------------

            print(
                "Qualified:",
                len(
                    results["qualified"]
                )
            )

            print(
                "Borderline:",
                len(
                    results["borderline"]
                )
            )

            print(
                "Rejected:",
                len(
                    results["rejected"]
                )
            )

            print(
                "Errors:",
                len(
                    results["errors"]
                )
            )

            print(
                "Next scan in",
                SCAN_INTERVAL_SECONDS,
                "seconds."
            )

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            print(
                "Scanner stopped."
            )

            send_telegram(
                f"🛑 Precision Scanner "
                f"{VERSION} stopped."
            )

            break

        except Exception as exc:

            print(
                "MAIN LOOP ERROR:",
                exc
            )

            send_telegram(
                "⚠️ Scanner engine error:\n"
                f"<code>{html.escape(str(exc))}</code>"
            )

            time.sleep(10)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
