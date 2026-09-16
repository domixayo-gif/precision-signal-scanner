"""
===========================================================
PRECISION SCANNER V3.6
STRICT 5M + 1M SIGNAL FRAMEWORK

Normal Forex + Separate OTC Framework
5M Trend + 1M Entry
5-Minute Reference Expiry
5-Minute Per-Asset Signal Lock
Telegram Alerts
Persistent Signal Tracking
Immediate Manual /scan

IMPORTANT:
- This scanner does NOT guarantee profitability.
- 75-80% is a testing target, not a guaranteed result.
- Weak/conflicting setups are rejected as NO TRADE.
- OTC remains disabled until a legitimate OTC candle feed
  is connected.
- This program DOES NOT execute Pocket Option trades.
- Score is a setup-quality score, NOT a win probability.
===========================================================
"""

import os
import json
import time
import html
from datetime import datetime, timezone

import requests


# =========================================================
# CONFIGURATION
# =========================================================

VERSION = "V3.6"

TELEGRAM_TOKEN = os.environ.get(
    "TELEGRAM_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = str(
    os.environ.get(
        "TELEGRAM_CHAT_ID",
        ""
    )
)

TRACKER_FILE = "tracker.json"


# =========================================================
# MARKET MODES
# =========================================================

NORMAL_MODE = "NORMAL"
OTC_MODE = "OTC"

ENABLED_MODES = [
    NORMAL_MODE,
    OTC_MODE,
]


# =========================================================
# NORMAL MARKET - BYBIT
# =========================================================

BYBIT_BASE = "https://api.bybit.com"

NORMAL_SYMBOLS = {
    "EURUSD": "EURUSDUSDT",
    "GBPUSD": "GBPUSDUSDT",
    "USDJPY": "USDJPYUSDT",
}

BYBIT_CATEGORY = "linear"


# =========================================================
# OTC
# =========================================================

OTC_ENABLED = False

OTC_SYMBOLS = {
    "EURUSD_OTC": "EUR/USD OTC",
    "GBPUSD_OTC": "GBP/USD OTC",
    "USDJPY_OTC": "USD/JPY OTC",
}


# =========================================================
# STRATEGY
# =========================================================

MAIN_TIMEFRAME = "5"
ENTRY_TIMEFRAME = "1"

REFERENCE_EXPIRY_MINUTES = 5


# =========================================================
# V3.6 STRICT FILTERS
# =========================================================

MIN_SCORE = 85
BORDERLINE_SCORE = 80

MIN_DOMINANCE = 3

MIN_ADX = 18
STRONG_ADX = 22

MIN_ROOM_SCORE = 3
MIN_EXTENSION_SCORE = 3

MIN_CANDLE_STRENGTH = 0.50

CALL_RSI_MIN = 43
CALL_RSI_MAX = 68

PUT_RSI_MIN = 32
PUT_RSI_MAX = 57

SIGNAL_LOCK_SECONDS = (
    REFERENCE_EXPIRY_MINUTES * 60
)

SCAN_INTERVAL_SECONDS = 60

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

CANDLE_LIMIT = 220

TELEGRAM_MESSAGE_LIMIT = 3900

HEARTBEAT_EVERY_SCANS = 15

MAX_TRACKER_ITEMS = 1000


# =========================================================
# HTTP
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "PrecisionScanner/3.6",
    "Accept": "application/json",
})


# =========================================================
# TIME
# =========================================================

def now_ts():
    return int(time.time())


def format_utc(ts):

    if not ts:
        return "N/A"

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def candle_id(ts):

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime(
        "%H%M%S"
    )


# =========================================================
# TELEGRAM
# =========================================================

def telegram_request(
    method,
    payload=None
):

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

        print(
            "Telegram error:",
            exc
        )

        return None


def send_telegram(message):

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        print(
            "\n--- TELEGRAM DISABLED ---"
        )

        print(message)

        print(
            "-------------------------\n"
        )

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

        if (
            not result
            or not result.get("ok")
        ):

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

            "signal_locks": {},

            "alerted_keys": [],

            "delivery_failed_keys": [],
        }
    }


def load_tracker():

    if not os.path.exists(
        TRACKER_FILE
    ):

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

            for key in result:

                if key in data:

                    result[key] = data[key]

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

    data["signals"] = (
        data.get(
            "signals",
            []
        )
        [-MAX_TRACKER_ITEMS:]
    )

    meta = data.setdefault(
        "meta",
        {}
    )

    meta["alerted_keys"] = (
        meta.get(
            "alerted_keys",
            []
        )
        [-MAX_TRACKER_ITEMS:]
    )

    meta["delivery_failed_keys"] = (
        meta.get(
            "delivery_failed_keys",
            []
        )
        [-MAX_TRACKER_ITEMS:]
    )

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
# HTTP
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

            if (
                attempt
                < MAX_RETRIES - 1
            ):

                time.sleep(
                    1.5 * (
                        attempt + 1
                    )
                )

    return None


# =========================================================
# BYBIT
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

    current = now_ts()

    seconds = (
        60
        if interval == "1"
        else 300
    )

    candles = [
        c
        for c in candles
        if (
            c["time"]
            + seconds
            <= current
        )
    ]

    return candles


# =========================================================
# OTC ADAPTER
# =========================================================

def get_otc_candles(
    symbol,
    interval
):

    if not OTC_ENABLED:

        return []

    # -----------------------------------------------------
    # IMPORTANT:
    # A legitimate OTC candle source must be connected here.
    #
    # Do NOT substitute normal forex/Bybit candles for OTC.
    # -----------------------------------------------------

    return []


# =========================================================
# DATA ROUTER
# =========================================================

def get_candles(
    mode,
    symbol,
    interval
):

    if mode == NORMAL_MODE:

        bybit_symbol = (
            NORMAL_SYMBOLS.get(
                symbol
            )
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

def ema(
    values,
    period
):

    if len(values) < period:

        return [
            None
            for _ in values
        ]

    result = [
        None
        for _ in values
    ]

    multiplier = (
        2 / (period + 1)
    )

    initial = (
        sum(values[:period])
        / period
    )

    result[
        period - 1
    ] = initial

    previous = initial

    for i in range(
        period,
        len(values)
    ):

        current = (
            values[i]
            * multiplier
            + previous
            * (1 - multiplier)
        )

        result[i] = current

        previous = current

    return result


def rsi(
    values,
    period=14
):

    result = [
        None
        for _ in values
    ]

    if len(values) <= period:

        return result

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    if avg_loss == 0:

        result[period] = 100

    else:

        rs = (
            avg_gain
            / avg_loss
        )

        result[period] = (
            100
            - 100 / (1 + rs)
        )

    for i in range(
        period + 1,
        len(values)
    ):

        gain = gains[i - 1]
        loss = losses[i - 1]

        avg_gain = (
            (
                avg_gain
                * (period - 1)
                + gain
            )
            / period
        )

        avg_loss = (
            (
                avg_loss
                * (period - 1)
                + loss
            )
            / period
        )

        if avg_loss == 0:

            result[i] = 100

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result[i] = (
                100
                - 100 / (1 + rs)
            )

    return result


def true_range(candles):

    result = []

    for i, candle in enumerate(
        candles
    ):

        if i == 0:

            value = (
                candle["high"]
                - candle["low"]
            )

        else:

            previous_close = (
                candles[i - 1]["close"]
            )

            value = max(
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

        result.append(value)

    return result


def atr(
    candles,
    period=14
):

    tr = true_range(candles)

    result = [
        None
        for _ in candles
    ]

    if len(tr) < period:

        return result

    value = (
        sum(tr[:period])
        / period
    )

    result[
        period - 1
    ] = value

    for i in range(
        period,
        len(tr)
    ):

        value = (
            (
                value
                * (period - 1)
            )
            + tr[i]
        ) / period

        result[i] = value

    return result


def macd(values):

    ema12 = ema(
        values,
        12
    )

    ema26 = ema(
        values,
        26
    )

    line = [
        None
        for _ in values
    ]

    for i in range(
        len(values)
    ):

        if (
            ema12[i] is not None
            and ema26[i] is not None
        ):

            line[i] = (
                ema12[i]
                - ema26[i]
            )

    clean = [
        x
        for x in line
        if x is not None
    ]

    signal_clean = ema(
        clean,
        9
    )

    signal = [
        None
        for _ in values
    ]

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

    histogram = [
        None
        for _ in values
    ]

    for i in range(
        len(values)
    ):

        if (
            line[i] is not None
            and signal[i] is not None
        ):

            histogram[i] = (
                line[i]
                - signal[i]
            )

    return (
        line,
        signal,
        histogram
    )


def adx(
    candles,
    period=14
):

    if len(candles) < (
        period + 2
    ):

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
            if (
                up_move > down_move
                and up_move > 0
            )
            else 0
        )

        minus = (
            down_move
            if (
                down_move > up_move
                and down_move > 0
            )
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

        plus_dm.append(
            plus
        )

        minus_dm.append(
            minus
        )

    adx_values = [
        None
        for _ in candles
    ]

    plus_di = [
        None
        for _ in candles
    ]

    minus_di = [
        None
        for _ in candles
    ]

    tr_avg = (
        sum(
            tr[1:period + 1]
        )
        / period
    )

    plus_avg = (
        sum(
            plus_dm[
                1:period + 1
            ]
        )
        / period
    )

    minus_avg = (
        sum(
            minus_dm[
                1:period + 1
            ]
        )
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

        denominator = (
            pdi + mdi
        )

        if denominator == 0:

            dx = 0

        else:

            dx = (
                100
                * abs(
                    pdi - mdi
                )
                / denominator
            )

        dx_values.append(dx)

        if len(dx_values) == period:

            adx_values[i] = (
                sum(dx_values)
                / period
            )

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
        minus_di
    )


# =========================================================
# PRICE ACTION
# =========================================================

def candle_direction(
    candle
):

    if (
        candle["close"]
        > candle["open"]
    ):

        return "BULLISH"

    if (
        candle["close"]
        < candle["open"]
    ):

        return "BEARISH"

    return "NEUTRAL"


def candle_strength(
    candle
):

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


def structure_direction(
    candles
):

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
# SCORING HELPERS
# =========================================================

def extension_score(
    price,
    atr_value,
    ema20
):

    if (
        atr_value is None
        or atr_value <= 0
        or ema20 is None
    ):

        return 0

    distance = abs(
        price - ema20
    )

    multiple = (
        distance
        / atr_value
    )

    if multiple <= 0.8:

        return 5

    if multiple <= 1.2:

        return 4

    if multiple <= 1.6:

        return 3

    if multiple <= 2.0:

        return 2

    if multiple <= 2.2:

        return 1

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
            resistance
            - price
        )

    elif direction == "PUT":

        support = min(
            c["low"]
            for c in lookback
        )

        room = (
            price
            - support
        )

    else:

        return 0

    multiple = (
        room
        / atr_value
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
# STRICT V3.6 ANALYSIS
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
            "version": VERSION,
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
    ) = macd(
        main_close
    )

    i = len(main) - 1

    e = len(entry) - 1

    price = entry_close[e]

    # =====================================================
    # 1. 5M MAJOR TREND
    # =====================================================

    if (
        main_ema20[i] is not None
        and main_ema50[i] is not None
        and price > main_ema50[i]
        and main_ema20[i]
        > main_ema50[i]
    ):

        major_trend = "BULLISH"

    elif (
        main_ema20[i] is not None
        and main_ema50[i] is not None
        and price < main_ema50[i]
        and main_ema20[i]
        < main_ema50[i]
    ):

        major_trend = "BEARISH"

    else:

        major_trend = "NEUTRAL"

    # =====================================================
    # 2. 1M ENTRY TREND
    # =====================================================

    if (
        entry_ema9[e] is not None
        and entry_ema21[e] is not None
    ):

        if (
            entry_ema9[e]
            > entry_ema21[e]
        ):

            entry_trend = "BULLISH"

        elif (
            entry_ema9[e]
            < entry_ema21[e]
        ):

            entry_trend = "BEARISH"

        else:

            entry_trend = "NEUTRAL"

    else:

        entry_trend = "NEUTRAL"

    # =====================================================
    # 3. STRUCTURE
    # =====================================================

    structure = structure_direction(
        main
    )

    # =====================================================
    # 4. DIRECTION VOTES
    # =====================================================

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

        if (
            plus_di[i]
            > minus_di[i]
        ):

            bullish_votes += 1

        elif (
            minus_di[i]
            > plus_di[i]
        ):

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

        raw_signal = "CALL"

    elif bearish_votes > bullish_votes:

        raw_signal = "PUT"

    else:

        raw_signal = "NO TRADE"

    # =====================================================
    # 5. SCORE
    # =====================================================

    components = {}

    # -----------------------------------------------------
    # TREND / 20
    # -----------------------------------------------------

    if (
        (
            raw_signal == "CALL"
            and major_trend == "BULLISH"
        )
        or
        (
            raw_signal == "PUT"
            and major_trend == "BEARISH"
        )
    ):

        components["Trend"] = 20

    elif major_trend != "NEUTRAL":

        components["Trend"] = 8

    else:

        components["Trend"] = 0

    # -----------------------------------------------------
    # STRUCTURE / 10
    # -----------------------------------------------------

    if (
        (
            raw_signal == "CALL"
            and structure == "BULLISH"
        )
        or
        (
            raw_signal == "PUT"
            and structure == "BEARISH"
        )
    ):

        components["Structure"] = 10

    elif structure != "NEUTRAL":

        components["Structure"] = 4

    else:

        components["Structure"] = 0

    # -----------------------------------------------------
    # ADX + DMI / 10
    # -----------------------------------------------------

    adx_value = adx_values[i]

    if adx_value is None:

        components["ADX/DMI"] = 0

    else:

        dmi_aligned = (
            (
                raw_signal == "CALL"
                and plus_di[i]
                > minus_di[i]
            )
            or
            (
                raw_signal == "PUT"
                and minus_di[i]
                > plus_di[i]
            )
        )

        if adx_value >= STRONG_ADX:

            components["ADX/DMI"] = (
                10
                if dmi_aligned
                else 0
            )

        elif adx_value >= MIN_ADX:

            components["ADX/DMI"] = (
                8
                if dmi_aligned
                else 2
            )

        else:

            components["ADX/DMI"] = 0

    # -----------------------------------------------------
    # MACD / 10
    # -----------------------------------------------------

    if macd_hist[i] is not None:

        macd_good = (
            (
                raw_signal == "CALL"
                and macd_hist[i] > 0
            )
            or
            (
                raw_signal == "PUT"
                and macd_hist[i] < 0
            )
        )

        components["MACD"] = (
            10
            if macd_good
            else 0
        )

    else:

        components["MACD"] = 0

    # -----------------------------------------------------
    # RSI / 10
    # -----------------------------------------------------

    rsi_value = main_rsi[i]

    if rsi_value is None:

        components["RSI"] = 0

    elif raw_signal == "CALL":

        if (
            CALL_RSI_MIN
            <= rsi_value
            <= CALL_RSI_MAX
        ):

            components["RSI"] = 10

        elif (
            38
            <= rsi_value
            <= 72
        ):

            components["RSI"] = 5

        else:

            components["RSI"] = 0

    elif raw_signal == "PUT":

        if (
            PUT_RSI_MIN
            <= rsi_value
            <= PUT_RSI_MAX
        ):

            components["RSI"] = 10

        elif (
            28
            <= rsi_value
            <= 62
        ):

            components["RSI"] = 5

        else:

            components["RSI"] = 0

    else:

        components["RSI"] = 0

    # -----------------------------------------------------
    # ENTRY / 15
    # -----------------------------------------------------

    if (
        (
            raw_signal == "CALL"
            and entry_trend == "BULLISH"
        )
        or
        (
            raw_signal == "PUT"
            and entry_trend == "BEARISH"
        )
    ):

        components["Entry"] = 15

    else:

        components["Entry"] = 0

    # -----------------------------------------------------
    # PULLBACK / 10
    # -----------------------------------------------------

    recent_entry = entry[-7:]

    pullback_score = 0

    if raw_signal == "CALL":

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

            pullback_score = 4

    elif raw_signal == "PUT":

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

            pullback_score = 4

    components["Pullback"] = (
        pullback_score
    )

    # -----------------------------------------------------
    # CONFIRMATION CANDLE / 5
    # -----------------------------------------------------

    strength = candle_strength(
        latest_entry_candle
    )

    candle_aligned = (
        (
            raw_signal == "CALL"
            and candle_dir == "BULLISH"
        )
        or
        (
            raw_signal == "PUT"
            and candle_dir == "BEARISH"
        )
    )

    if candle_aligned:

        if strength >= 0.65:

            components["Candle"] = 5

        elif strength >= 0.50:

            components["Candle"] = 4

        else:

            components["Candle"] = 1

    else:

        components["Candle"] = 0

    # -----------------------------------------------------
    # ROOM / 5
    # -----------------------------------------------------

    room = room_score(
        price,
        main,
        raw_signal,
        main_atr[i]
    )

    components["Room"] = room

    # -----------------------------------------------------
    # EXTENSION / 5
    # -----------------------------------------------------

    ext = extension_score(
        price,
        main_atr[i],
        main_ema20[i]
    )

    components["Extension"] = ext

    score = sum(
        components.values()
    )

    # =====================================================
    # HARD BLOCKERS
    # =====================================================

    blockers = []

    # -----------------------------------------------------
    # 5M TREND
    # -----------------------------------------------------

    if (
        raw_signal == "CALL"
        and major_trend != "BULLISH"
    ):

        blockers.append(
            "5M TREND MISMATCH"
        )

    if (
        raw_signal == "PUT"
        and major_trend != "BEARISH"
    ):

        blockers.append(
            "5M TREND MISMATCH"
        )

    # -----------------------------------------------------
    # 1M ENTRY
    # -----------------------------------------------------

    if (
        raw_signal == "CALL"
        and entry_trend != "BULLISH"
    ):

        blockers.append(
            "1M ENTRY MISMATCH"
        )

    if (
        raw_signal == "PUT"
        and entry_trend != "BEARISH"
    ):

        blockers.append(
            "1M ENTRY MISMATCH"
        )

    # -----------------------------------------------------
    # STRUCTURE
    # -----------------------------------------------------

    if (
        raw_signal == "CALL"
        and structure != "BULLISH"
    ):

        blockers.append(
            "STRUCTURE NOT BULLISH"
        )

    if (
        raw_signal == "PUT"
        and structure != "BEARISH"
    ):

        blockers.append(
            "STRUCTURE NOT BEARISH"
        )

    # -----------------------------------------------------
    # ADX
    # -----------------------------------------------------

    if (
        adx_value is None
        or adx_value < MIN_ADX
    ):

        blockers.append(
            "ADX TOO LOW"
        )

    # -----------------------------------------------------
    # DMI
    # -----------------------------------------------------

    if (
        plus_di[i] is None
        or minus_di[i] is None
    ):

        blockers.append(
            "DMI UNAVAILABLE"
        )

    else:

        if (
            raw_signal == "CALL"
            and plus_di[i]
            <= minus_di[i]
        ):

            blockers.append(
                "DMI NOT BULLISH"
            )

        elif (
            raw_signal == "PUT"
            and minus_di[i]
            <= plus_di[i]
        ):

            blockers.append(
                "DMI NOT BEARISH"
            )

    # -----------------------------------------------------
    # MACD
    # -----------------------------------------------------

    if macd_hist[i] is None:

        blockers.append(
            "MACD UNAVAILABLE"
        )

    else:

        if (
            raw_signal == "CALL"
            and macd_hist[i] <= 0
        ):

            blockers.append(
                "MACD NOT BULLISH"
            )

        elif (
            raw_signal == "PUT"
            and macd_hist[i] >= 0
        ):

            blockers.append(
                "MACD NOT BEARISH"
            )

    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

    if rsi_value is None:

        blockers.append(
            "RSI UNAVAILABLE"
        )

    elif raw_signal == "CALL":

        if not (
            CALL_RSI_MIN
            <= rsi_value
            <= CALL_RSI_MAX
        ):

            blockers.append(
                "RSI OUTSIDE CALL ZONE"
            )

    elif raw_signal == "PUT":

        if not (
            PUT_RSI_MIN
            <= rsi_value
            <= PUT_RSI_MAX
        ):

            blockers.append(
                "RSI OUTSIDE PUT ZONE"
            )

    # -----------------------------------------------------
    # CONFIRMATION CANDLE
    # -----------------------------------------------------

    if not candle_aligned:

        blockers.append(
            "CONFIRMATION CANDLE MISMATCH"
        )

    elif strength < MIN_CANDLE_STRENGTH:

        blockers.append(
            "WEAK CONFIRMATION CANDLE"
        )

    # -----------------------------------------------------
    # PULLBACK
    # -----------------------------------------------------

    if pullback_score < 10:

        blockers.append(
            "NO CLEAN PULLBACK"
        )

    # -----------------------------------------------------
    # ROOM
    # -----------------------------------------------------

    if room < MIN_ROOM_SCORE:

        blockers.append(
            "INSUFFICIENT ROOM"
        )

    # -----------------------------------------------------
    # EXTENSION
    # -----------------------------------------------------

    if ext < MIN_EXTENSION_SCORE:

        blockers.append(
            "PRICE TOO EXTENDED"
        )

    # -----------------------------------------------------
    # DOMINANCE
    # -----------------------------------------------------

    dominance = abs(
        bullish_votes
        - bearish_votes
    )

    if dominance < MIN_DOMINANCE:

        blockers.append(
            "WEAK DIRECTIONAL DOMINANCE"
        )

    # =====================================================
    # QUALIFICATION
    # =====================================================

    qualified = (
        raw_signal in (
            "CALL",
            "PUT"
        )
        and score >= MIN_SCORE
        and dominance >= MIN_DOMINANCE
        and len(blockers) == 0
    )

    borderline = (
        raw_signal in (
            "CALL",
            "PUT"
        )
        and score >= BORDERLINE_SCORE
        and score < MIN_SCORE
        and len(blockers) == 0
    )

    final_signal = (
        raw_signal
        if qualified
        else "NO TRADE"
    )

    entry_time = entry[-1]["time"]

    signal_id = (
        f"{mode}-"
        f"{symbol}-"
        f"{final_signal}-"
        f"{candle_id(entry_time)}"
    )

    return {
        "version": VERSION,

        "mode": mode,

        "symbol": symbol,

        "signal": final_signal,

        "raw_direction": raw_signal,

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

        "plus_di": plus_di[i],

        "minus_di": minus_di[i],

        "macd_hist": macd_hist[i],

        "candle_strength": strength,

        "room_score": room,

        "extension_score": ext,

        "components": components,

        "blockers": blockers,

        "entry_candle_time": entry_time,

        "main_candle_time": main[-1]["time"],

        "signal_id": signal_id,

        "scanned_at": now_ts(),
    }


# =========================================================
# DEDUPLICATION
# =========================================================

def dedupe_key(result):

    return "|".join([
        result["mode"],
        result["symbol"],
        result["signal"],
        str(
            result[
                "entry_candle_time"
            ]
        )
    ])


def was_alerted(
    tracker,
    key
):

    return key in (
        tracker[
            "meta"
        ].get(
            "alerted_keys",
            []
        )
    )


def mark_alerted(
    tracker,
    key
):

    arr = (
        tracker[
            "meta"
        ].setdefault(
            "alerted_keys",
            []
        )
    )

    if key not in arr:

        arr.append(key)

    tracker[
        "meta"
    ]["alerted_keys"] = (
        arr[-MAX_TRACKER_ITEMS:]
    )


def mark_delivery_failed(
    tracker,
    key
):

    arr = (
        tracker[
            "meta"
        ].setdefault(
            "delivery_failed_keys",
            []
        )
    )

    if key not in arr:

        arr.append(key)

    tracker[
        "meta"
    ]["delivery_failed_keys"] = (
        arr[-MAX_TRACKER_ITEMS:]
    )


# =========================================================
# SIGNAL LOCK
# =========================================================

def asset_lock_key(
    mode,
    symbol
):

    return (
        f"{mode}:{symbol}"
    )


def get_lock_until(
    tracker,
    mode,
    symbol
):

    locks = (
        tracker[
            "meta"
        ].setdefault(
            "signal_locks",
            {}
        )
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
        <
        get_lock_until(
            tracker,
            mode,
            symbol
        )
    )


def set_signal_lock(
    tracker,
    mode,
    symbol,
    start_time=None
):

    locks = (
        tracker[
            "meta"
        ].setdefault(
            "signal_locks",
            {}
        )
    )

    if start_time is None:

        start_time = now_ts()

    lock_until = (
        int(start_time)
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
# SIGNAL TRACKING
# =========================================================

def signal_already_recorded(
    tracker,
    signal_id
):

    for signal in tracker.get(
        "signals",
        []
    ):

        if (
            signal.get(
                "signal_id"
            )
            == signal_id
        ):

            return True

    return False


def add_signal(
    tracker,
    result
):

    created_at = now_ts()

    record = {
        **result,

        "result": "PENDING",

        "alert_sent": False,

        "created_at": created_at,

        "expiry_minutes":
            REFERENCE_EXPIRY_MINUTES,

        "expiry_at":
            created_at
            + SIGNAL_LOCK_SECONDS,
    }

    tracker[
        "signals"
    ].append(record)

    return record


# =========================================================
# ALERT
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

    components = (
        result["components"]
    )

    reasons = []

    for name, value in (
        components.items()
    ):

        if value > 0:

            reasons.append(
                f"{name}: {value}"
            )

    reasons_text = (
        "\n".join(reasons)
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
<b>+DI:</b> {result["plus_di"]:.2f}
<b>-DI:</b> {result["minus_di"]:.2f}

<b>RSI:</b> {result["rsi"]:.2f}

<b>Candle strength:</b>
{result["candle_strength"]:.2f}

<b>Room:</b>
{result["room_score"]}/5

<b>Extension:</b>
{result["extension_score"]}/5

<b>Direction votes:</b>
🟢 {result["bullish_votes"]}
🔴 {result["bearish_votes"]}

<b>Score breakdown:</b>
{reasons_text}

<b>Signal ID:</b>
<code>{html.escape(result["signal_id"])}</code>

<b>Signal candle:</b>
{format_utc(result["entry_candle_time"])}

<b>🔒 Asset lock:</b>
{format_utc(lock_until)}

Analysis signal only.
No automatic trade execution.
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
        if s.get("result")
        == "PENDING"
    )

    wins = sum(
        1
        for s in signals
        if s.get("result")
        == "WIN"
    )

    losses = sum(
        1
        for s in signals
        if s.get("result")
        == "LOSS"
    )

    total = wins + losses

    rate = (
        wins
        / total
        * 100
        if total
        else 0
    )

    return f"""
<b>💓 PRECISION SCANNER {VERSION}</b>

<b>Status:</b> RUNNING

<b>Scan:</b>
{scan_number}

<b>Minimum score:</b>
{MIN_SCORE}/100

<b>Testing target:</b>
75-80% recorded win rate

<b>Normal assets:</b>
{len(NORMAL_SYMBOLS)}

<b>OTC:</b>
{"ENABLED" if OTC_ENABLED else "WAITING FOR LEGITIMATE FEED"}

<b>Tracked:</b>
{len(signals)}

<b>Pending:</b>
{pending}

<b>Wins:</b>
{wins}

<b>Losses:</b>
{losses}

<b>Recorded win rate:</b>
{rate:.1f}%

<b>Asset lock:</b>
{SIGNAL_LOCK_SECONDS} seconds

Weak setups are rejected.
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

        return False, False

    updates = response.get(
        "result",
        []
    )

    changed = False

    manual_scan_requested = False

    for update in updates:

        update_id = update.get(
            "update_id"
        )

        if update_id is not None:

            tracker[
                "offset"
            ] = update_id

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
            and chat_id
            != TELEGRAM_CHAT_ID
        ):

            continue

        if text == "/start":

            send_telegram(
                f"""
<b>PRECISION SCANNER {VERSION}</b>

Scanner is online.

<b>Strategy:</b>
Strict 5M + 1M confirmation

<b>Minimum score:</b>
{MIN_SCORE}/100

Commands:

/scan
/stats
/help

Results:

/win SIGNAL_ID

/loss SIGNAL_ID
"""
            )

        elif text == "/help":

            send_telegram(
                """
<b>Commands</b>

/scan
Run an immediate market scan.

/stats
Show recorded statistics.

/win SIGNAL-ID
Record a WIN.

/loss SIGNAL-ID
Record a LOSS.
"""
            )

        elif text == "/scan":

            manual_scan_requested = True

            changed = True

            send_telegram(
                "🔎 <b>Manual scan started.</b>\n\n"
                "Analyzing all enabled markets now..."
            )

        elif text == "/stats":

            send_telegram(
                stats_text(
                    tracker
                )
            )

        elif text.startswith(
            "/win "
        ):

            signal_id = (
                text[5:].strip()
            )

            if mark_result(
                tracker,
                signal_id,
                "WIN"
            ):

                send_telegram(
                    "✅ Recorded WIN: "
                    f"<code>"
                    f"{html.escape(signal_id)}"
                    f"</code>"
                )

                changed = True

            else:

                send_telegram(
                    "Signal ID not found "
                    "or already settled."
                )

        elif text.startswith(
            "/loss "
        ):

            signal_id = (
                text[6:].strip()
            )

            if mark_result(
                tracker,
                signal_id,
                "LOSS"
            ):

                send_telegram(
                    "❌ Recorded LOSS: "
                    f"<code>"
                    f"{html.escape(signal_id)}"
                    f"</code>"
                )

                changed = True

            else:

                send_telegram(
                    "Signal ID not found "
                    "or already settled."
                )

    return (
        changed,
        manual_scan_requested
    )


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
            signal.get(
                "signal_id"
            )
            == signal_id
        ):

            # Prevent accidental overwriting
            # of an already settled result.
            if signal.get(
                "result"
            ) in (
                "WIN",
                "LOSS"
            ):

                return False

            signal[
                "result"
            ] = result

            signal[
                "result_time"
            ] = now_ts()

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
        in (
            "WIN",
            "LOSS"
        )
    ]


def stats_text(
    tracker
):

    completed = (
        completed_signals(
            tracker
        )
    )

    wins = sum(
        s["result"] == "WIN"
        for s in completed
    )

    losses = sum(
        s["result"] == "LOSS"
        for s in completed
    )

    total = (
        wins
        + losses
    )

    if total:

        rate = (
            wins
            / total
            * 100
        )

    else:

        rate = 0

    # -----------------------------------------------------
    # CALL / PUT STATS
    # -----------------------------------------------------

    call_completed = [
        s
        for s in completed
        if s.get(
            "signal"
        ) == "CALL"
    ]

    put_completed = [
        s
        for s in completed
        if s.get(
            "signal"
        ) == "PUT"
    ]

    call_wins = sum(
        s["result"] == "WIN"
        for s in call_completed
    )

    put_wins = sum(
        s["result"] == "WIN"
        for s in put_completed
    )

    call_rate = (
        call_wins
        / len(call_completed)
        * 100
        if call_completed
        else 0
    )

    put_rate = (
        put_wins
        / len(put_completed)
        * 100
        if put_completed
        else 0
    )

    pending = sum(
        s.get("result")
        == "PENDING"
        for s in tracker.get(
            "signals",
            []
        )
    )

    return f"""
<b>📊 PRECISION SCANNER {VERSION}</b>

<b>Completed:</b>
{total}

<b>Wins:</b>
{wins}

<b>Losses:</b>
{losses}

<b>Recorded win rate:</b>
{rate:.1f}%

<b>CALL:</b>
{len(call_completed)} completed
{call_rate:.1f}% win rate

<b>PUT:</b>
{len(put_completed)} completed
{put_rate:.1f}% win rate

<b>Pending:</b>
{pending}

<b>Testing target:</b>
75-80%

The recorded percentage is calculated
only from results actually entered.

It does not predict future performance.
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
    # NORMAL
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
    # OTC
    # -----------------------------------------------------

    if OTC_ENABLED:

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
# PROCESS RESULT
# =========================================================

def process_result(
    tracker,
    result,
    qualified,
    borderline,
    rejected
):

    if result.get(
        "borderline"
    ):

        borderline.append(
            result
        )

        return

    if not result.get(
        "qualified"
    ):

        rejected.append(
            result
        )

        return

    qualified.append(
        result
    )

    mode = result[
        "mode"
    ]

    symbol = result[
        "symbol"
    ]

    key = dedupe_key(
        result
    )

    # -----------------------------------------------------
    # SAME SIGNAL ALREADY RECORDED
    # -----------------------------------------------------

    if signal_already_recorded(
        tracker,
        result["signal_id"]
    ):

        return

    # -----------------------------------------------------
    # SAME CANDLE ALREADY ALERTED
    # -----------------------------------------------------

    if was_alerted(
        tracker,
        key
    ):

        return

    # -----------------------------------------------------
    # ASSET LOCK
    # -----------------------------------------------------

    if is_locked(
        tracker,
        mode,
        symbol
    ):

        lock_until = (
            get_lock_until(
                tracker,
                mode,
                symbol
            )
        )

        print(
            f"LOCKED: "
            f"{mode} "
            f"{symbol} "
            f"until "
            f"{format_utc(lock_until)}"
        )

        return

    # -----------------------------------------------------
    # RECORD SIGNAL
    # -----------------------------------------------------

    record = add_signal(
        tracker,
        result
    )

    # -----------------------------------------------------
    # IMPORTANT:
    # Lock begins when the signal is actually
    # recorded, NOT at the beginning of the
    # already completed candle.
    # -----------------------------------------------------

    signal_created_at = (
        record["created_at"]
    )

    lock_until = set_signal_lock(
        tracker,
        mode,
        symbol,
        signal_created_at
    )

    record[
        "lock_until"
    ] = lock_until

    # -----------------------------------------------------
    # BUILD ALERT
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

    record[
        "alert_attempt_time"
    ] = now_ts()

    # -----------------------------------------------------
    # Mark this exact setup as handled regardless
    # of Telegram delivery result.
    # -----------------------------------------------------

    mark_alerted(
        tracker,
        key
    )

    if sent:

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

        mark_delivery_failed(
            tracker,
            key
        )

        print(
            "Signal recorded but "
            "Telegram delivery failed: "
            f"{result['signal_id']}"
        )


# =========================================================
# MANUAL SCAN SUMMARY
# =========================================================

def build_manual_scan_summary(
    results
):

    qualified = results.get(
        "qualified",
        []
    )

    borderline = results.get(
        "borderline",
        []
    )

    rejected = results.get(
        "rejected",
        []
    )

    errors = results.get(
        "errors",
        []
    )

    if qualified:

        qualified_lines = []

        for result in qualified:

            qualified_lines.append(
                f"• {html.escape(result['symbol'])} "
                f"<b>{result['signal']}</b> "
                f"({result['score']}/100)"
            )

        qualified_text = "\n".join(
            qualified_lines
        )

        return (
            "⚡ <b>MANUAL SCAN COMPLETE</b>\n\n"
            f"<b>Qualified:</b> {len(qualified)}\n"
            f"{qualified_text}\n\n"
            f"<b>Borderline:</b> {len(borderline)}\n"
            f"<b>Rejected:</b> {len(rejected)}\n"
            f"<b>Errors:</b> {len(errors)}\n\n"
            "Qualified signals were sent separately.\n\n"
            "Automatic scanning will continue."
        )

    return (
        "🔎 <b>MANUAL SCAN COMPLETE</b>\n\n"
        "❌ <b>No qualified signal right now.</b>\n\n"
        f"<b>Borderline:</b> {len(borderline)}\n"
        f"<b>Rejected:</b> {len(rejected)}\n"
        f"<b>Errors:</b> {len(errors)}\n\n"
        "The scanner will continue "
        "automatic scanning."
    )


# =========================================================
# STARTUP MESSAGE
# =========================================================

def startup_message():

    return f"""
<b>🚀 PRECISION SCANNER {VERSION}</b>

Scanner engine started.

<b>Strategy:</b>
Strict 5M Trend + 1M Entry

<b>Minimum score:</b>
{MIN_SCORE}/100

<b>Minimum dominance:</b>
{MIN_DOMINANCE}

<b>Minimum ADX:</b>
{MIN_ADX}

<b>Minimum room:</b>
{MIN_ROOM_SCORE}/5

<b>Minimum extension:</b>
{MIN_EXTENSION_SCORE}/5

<b>Reference expiry:</b>
{REFERENCE_EXPIRY_MINUTES} minutes

<b>Asset lock:</b>
{SIGNAL_LOCK_SECONDS} seconds

<b>Testing target:</b>
75-80% recorded win rate

Weak/conflicting setups are rejected.

OTC:
{"ENABLED" if OTC_ENABLED else "DISABLED"}

<b>/scan</b>
Runs an immediate market scan.

No automatic Pocket Option
trades are executed.

Score is a setup-quality score,
not a win probability.
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

            changed, manual_scan = (
                process_commands(
                    tracker
                )
            )

            if changed:

                save_tracker(
                    tracker
                )

            # -------------------------------------------------
            # IMMEDIATE MANUAL SCAN
            # -------------------------------------------------

            if manual_scan:

                scan_number += 1

                print(
                    "\n"
                    f"========== MANUAL SCAN "
                    f"{scan_number} =========="
                )

                results = run_scan_cycle(
                    tracker
                )

                save_tracker(
                    tracker
                )

                send_telegram(
                    build_manual_scan_summary(
                        results
                    )
                )

                print(
                    "Manual scan complete."
                )

                print(
                    "Qualified:",
                    len(
                        results[
                            "qualified"
                        ]
                    )
                )

                print(
                    "Borderline:",
                    len(
                        results[
                            "borderline"
                        ]
                    )
                )

                print(
                    "Rejected:",
                    len(
                        results[
                            "rejected"
                        ]
                    )
                )

                print(
                    "Errors:",
                    len(
                        results[
                            "errors"
                        ]
                    )
                )

                # -------------------------------------------------
                # IMPORTANT:
                # The manual scan counts as this scan cycle.
                # Do not immediately run another automatic scan.
                # Go back to command polling.
                # -------------------------------------------------

                continue

            # -------------------------------------------------
            # AUTOMATIC SCAN
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
                ]["last_heartbeat"] = (
                    now_ts()
                )

                save_tracker(
                    tracker
                )

            # -------------------------------------------------
            # CONSOLE
            # -------------------------------------------------

            print(
                "Qualified:",
                len(
                    results[
                        "qualified"
                    ]
                )
            )

            print(
                "Borderline:",
                len(
                    results[
                        "borderline"
                    ]
                )
            )

            print(
                "Rejected:",
                len(
                    results[
                        "rejected"
                    ]
                )
            )

            print(
                "Errors:",
                len(
                    results[
                        "errors"
                    ]
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
                f"<code>"
                f"{html.escape(str(exc))}"
                f"</code>"
            )

            time.sleep(10)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
