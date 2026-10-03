import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# ZETA V3
# 3EMA + RSI + TREND PRICE ACTION
#
# COMBINED FROM:
#   1. 3(Three)EMA + RSI Strategy v3
#   2. Trend-based Price Action Strategy
#
# PRACTICE / DEMO ONLY
# REAL IQ OPTION OTC DISCOVERY
# CLOSED CANDLES ONLY
# ============================================================


# ============================================================
# ACCOUNT / EXECUTION
# ============================================================

BALANCE_MODE = "PRACTICE"

STAKE = 1.0

EXPIRY_MINUTES = 10

TARGET_TRADES = 50


# ============================================================
# TIMEFRAMES
# ============================================================

TF5 = 300
TF1 = 60


# ============================================================
# DATA
# ============================================================

# 5M data for:
# - 5M RSI(20)
# - 5M trend/slope context
# - ADX
CANDLE_COUNT_5M = 220

# 1M data for:
# - EMA 3/21/50/200
# - EMA 200/600/1000
# - RSI14
# - price action
# - strong candle
#
# 1100 gives enough history for EMA1000.
CANDLE_COUNT_1M = 1100


# ============================================================
# OTC
# ============================================================

MAX_OTC_ASSETS = 70


# ============================================================
# LOOP / CONNECTION
# ============================================================

SCAN_INTERVAL = 15

STATUS_INTERVAL = 300

RECONNECT_INTERVAL = 30

DISCOVERY_INTERVAL = 1800


# ============================================================
# TRADE LOCKS
# ============================================================

ASSET_LOCK_SECONDS = 600

SIGNAL_LOCK_SECONDS = 180


# ============================================================
# ZETA V3 — 3EMA STRATEGY
# ============================================================

EMA_FAST = 3

EMA_MEDIUM = 21

EMA_SLOW_MEDIUM = 50

EMA_SLOW = 200


# ============================================================
# ZETA V3 — MACRO PRICE ACTION EMAS
# ============================================================

MACRO_EMA_FAST = 200

MACRO_EMA_MEDIUM = 600

MACRO_EMA_SLOW = 1000


# ============================================================
# RSI
# ============================================================

# Pine #3
RSI_MTF_PERIOD = 20

RSI_MTF_LONG = 55

RSI_MTF_SHORT = 45

RSI_MTF_LOOKBACK = 3


# Pine #4
RSI_PRICE_ACTION_PERIOD = 14

RSI_OVERBOUGHT = 70

RSI_OVERSOLD = 30

RSI_PULLBACK_LOOKBACK = 3


# ============================================================
# EMA SLOPE
# ============================================================

SLOPE_LOOKBACK = 3

MIN_BULL_SLOPE_PERCENT = 0.05

MIN_BEAR_SLOPE_PERCENT = -0.05

FLAT_SLOPE_PERCENT = 0.02

RANGE_CONFIRM_BARS = 3


# ============================================================
# STRONG CANDLE
# ============================================================

STRONG_CANDLE_AVG_LOOKBACK = 20

STRONG_CANDLE_MULTIPLIER = 1.50

MIN_STRONG_BODY_RANGE = 0.50


# ============================================================
# ATR / ADX
# ============================================================

ATR_PERIOD = 14

ADX_PERIOD = 14

MIN_ADX = 18


# ============================================================
# ROOM
# ============================================================

MIN_ROOM_ATR = 0.80


# ============================================================
# PULLBACK
# ============================================================

MIN_PULLBACK_ATR = 0.10

MAX_PULLBACK_ATR = 1.80

MAX_EXTENSION_ATR = 2.20


# ============================================================
# ZONE
# ============================================================

ZONE_TOLERANCE_ATR = 0.35


# ============================================================
# SCORE
#
# Maximum = 100
#
# 20 primary EMA trend
# 15 macro trend
# 15 RSI
# 10 EMA slope
# 15 price action
# 10 strong candle
# 5 pullback
# 5 room
# 5 ADX
# ============================================================

MIN_SCORE = 80


# ============================================================
# ENVIRONMENT
# ============================================================

IQ_EMAIL = os.getenv(
    "IQ_EMAIL",
    ""
).strip()

IQ_PASSWORD = os.getenv(
    "IQ_PASSWORD",
    ""
).strip()

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()


# ============================================================
# GLOBAL STATE
# ============================================================

api = None

otc_assets = []

last_signal_time = {}

last_trade_time = {}

total_trades = 0

start_time = time.time()

last_status_time = 0

last_discovery_time = 0

last_connection_check = 0

scan_cycles = 0

signals_found = 0

no_trade_cycles = 0

rejection_counts = {}


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc():

    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def safe_float(
    value,
    default=0.0,
):

    try:

        value = float(value)

        if math.isfinite(value):

            return value

    except Exception:

        pass

    return default


def server_now():

    try:

        if api is not None:

            value = api.get_server_timestamp()

            if value:

                return int(value)

    except Exception:

        pass

    return int(time.time())


def runtime_string():

    seconds = int(
        time.time()
        - start_time
    )

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    return (
        f"{hours}h {minutes}m"
    )


def remember_rejection(
    reason
):

    rejection_counts[
        reason
    ] = (
        rejection_counts.get(
            reason,
            0
        )
        + 1
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        print(
            "\n[TELEGRAM DISABLED]"
        )

        print(text)

        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
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

        print(
            "[TELEGRAM ERROR]",
            response.status_code,
            response.text[:500],
        )

    except Exception as e:

        print(
            "[TELEGRAM EXCEPTION]",
            repr(e),
        )

    return False


# ============================================================
# OTC NAME DETECTION
# ============================================================

def is_otc_name(name):

    if not isinstance(
        name,
        str,
    ):

        return False

    upper = (
        name.upper()
        .strip()
    )

    return (
        upper.endswith(
            "-OTC"
        )
        or upper.endswith(
            "_OTC"
        )
        or upper.endswith(
            " OTC"
        )
        or "-OTC." in upper
        or "_OTC." in upper
        or " OTC." in upper
    )


def clean_active_name(
    raw_name
):

    if raw_name is None:

        return None

    name = str(
        raw_name
    ).strip()

    if "." in name:

        parts = name.split(".")

        if len(parts) >= 2:

            name = parts[-1]

    return name.strip()


# ============================================================
# ACTIVE ID
# ============================================================

def register_active_id(
    name,
    active_id,
):

    if not name:

        return False

    try:

        active_id = int(
            active_id
        )

    except Exception:

        return False

    try:

        OP_code.ACTIVES[
            name
        ] = active_id

        return True

    except Exception as e:

        print(
            "[ACTIVE MAP ERROR]",
            name,
            active_id,
            repr(e),
        )

        return False


# ============================================================
# IQ OPTION INITIALIZATION
# ============================================================

def get_raw_initialization():

    if api is None:

        return None

    print(
        "\n"
        + "=" * 70
    )

    print(
        "IQ OPTION MARKET INITIALIZATION"
    )

    print(
        "=" * 70
    )

    try:

        print(
            "[RAW] Requesting "
            "get_all_init_v2()..."
        )

        data = (
            api.get_all_init_v2()
        )

        if data:

            print(
                "[RAW] "
                "get_all_init_v2 received."
            )

            print(
                "[RAW] Type:",
                type(data).__name__,
            )

            return data

    except Exception as e:

        print(
            "[RAW V2 ERROR]",
            repr(e),
        )

    try:

        print(
            "[RAW] Trying "
            "get_all_init()..."
        )

        data = (
            api.get_all_init()
        )

        if data:

            print(
                "[RAW] "
                "get_all_init received."
            )

            print(
                "[RAW] Type:",
                type(data).__name__,
            )

            return data

    except Exception as e:

        print(
            "[RAW LEGACY ERROR]",
            repr(e),
        )

    print(
        "[RAW] No initialization data."
    )

    return None


# ============================================================
# REAL OTC DISCOVERY
# ============================================================

def discover_otc_from_initialization(
    data
):

    print(
        "\n"
        + "=" * 70
    )

    print(
        "REAL IQ OPTION OTC DISCOVERY"
    )

    print(
        "=" * 70
    )

    if not isinstance(
        data,
        dict,
    ):

        print(
            "[DISCOVERY] "
            "Unexpected root type:",
            type(data).__name__,
        )

        return []

    found = []

    seen = set()

    def walk(
        node,
        market_type="unknown",
    ):

        if (
            len(found)
            >= MAX_OTC_ASSETS
        ):

            return

        if isinstance(
            node,
            dict,
        ):

            if "actives" in node:

                actives = node.get(
                    "actives"
                )

                if isinstance(
                    actives,
                    dict,
                ):

                    for (
                        active_id,
                        active,
                    ) in actives.items():

                        if (
                            len(found)
                            >= MAX_OTC_ASSETS
                        ):

                            return

                        if not isinstance(
                            active,
                            dict,
                        ):

                            continue

                        raw_name = (
                            active.get(
                                "name"
                            )
                        )

                        name = (
                            clean_active_name(
                                raw_name
                            )
                        )

                        if not is_otc_name(
                            name
                        ):

                            continue

                        enabled = bool(
                            active.get(
                                "enabled",
                                True,
                            )
                        )

                        suspended = bool(
                            active.get(
                                "is_suspended",
                                False,
                            )
                        )

                        if not enabled:

                            continue

                        if suspended:

                            continue

                        try:

                            numeric_id = int(
                                active_id
                            )

                        except Exception:

                            continue

                        register_active_id(
                            name,
                            numeric_id,
                        )

                        key = (
                            name,
                            numeric_id,
                        )

                        if key in seen:

                            continue

                        seen.add(key)

                        found.append(
                            {
                                "asset": name,
                                "market_type":
                                    market_type,
                                "active_id":
                                    numeric_id,
                            }
                        )

            for (
                key,
                value,
            ) in node.items():

                child_market = (
                    market_type
                )

                if key in (
                    "binary",
                    "turbo",
                    "digital",
                    "cfd",
                    "forex",
                    "crypto",
                    "stocks",
                    "commodities",
                ):

                    child_market = key

                if isinstance(
                    value,
                    (
                        dict,
                        list,
                    ),
                ):

                    walk(
                        value,
                        child_market,
                    )

        elif isinstance(
            node,
            list,
        ):

            for item in node:

                if (
                    len(found)
                    >= MAX_OTC_ASSETS
                ):

                    return

                walk(
                    item,
                    market_type,
                )

    root = data

    if isinstance(
        data.get("result"),
        dict,
    ):

        print(
            "[DISCOVERY] "
            "result wrapper detected."
        )

        root = data[
            "result"
        ]

    print(
        "[DISCOVERY] "
        "Top-level keys:"
    )

    for key in root.keys():

        print(
            " -",
            key,
        )

    walk(root)

    found.sort(
        key=lambda item: (
            item["asset"],
            item["market_type"],
            item["active_id"],
        )
    )

    print(
        "\nTOTAL REAL OTC DISCOVERED:",
        len(found),
    )

    if found:

        print(
            "\n[REAL IQ OPTION OTC ASSETS]"
        )

        for index, item in enumerate(
            found,
            start=1,
        ):

            print(
                f"{index:02d}. "
                f"{item['asset']:<20} "
                f"{item['market_type']:<12} "
                f"ID={item['active_id']}"
            )

    else:

        print(
            "[DISCOVERY] No enabled OTC "
            "instruments found."
        )

    return found[
        :MAX_OTC_ASSETS
    ]


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candles(raw):

    if not raw:

        return []

    result = []

    for candle in raw:

        try:

            item = {
                "from": safe_float(
                    candle.get(
                        "from"
                    )
                ),
                "open": safe_float(
                    candle.get(
                        "open"
                    )
                ),
                "close": safe_float(
                    candle.get(
                        "close"
                    )
                ),
                "low": safe_float(
                    candle.get(
                        "min",
                        candle.get(
                            "low"
                        ),
                    )
                ),
                "high": safe_float(
                    candle.get(
                        "max",
                        candle.get(
                            "high"
                        ),
                    )
                ),
                "volume": safe_float(
                    candle.get(
                        "volume"
                    )
                ),
            }

            if (
                item["open"] > 0
                and item["close"] > 0
                and item["low"] > 0
                and item["high"] > 0
            ):

                result.append(
                    item
                )

        except Exception:

            continue

    result.sort(
        key=lambda x: x["from"]
    )

    return result


# ============================================================
# CLOSED CANDLES
# ============================================================

def remove_open_candle(
    candles,
    timeframe_seconds,
):

    if len(candles) < 2:

        return candles

    current_time = (
        server_now()
    )

    completed = []

    for candle in candles:

        candle_start = (
            candle["from"]
        )

        if (
            candle_start
            + timeframe_seconds
            <= current_time
        ):

            completed.append(
                candle
            )

    return completed


# ============================================================
# CANDLE FETCH
# ============================================================

def get_candles_safe(
    asset,
    interval,
    count,
):

    if api is None:

        return []

    try:

        raw = api.get_candles(
            asset,
            interval,
            count + 10,
            server_now(),
        )

        candles = (
            normalize_candles(
                raw
            )
        )

        candles = (
            remove_open_candle(
                candles,
                interval,
            )
        )

        return candles[
            -count:
        ]

    except Exception as e:

        print(
            "[CANDLE ERROR]",
            asset,
            interval,
            repr(e),
        )

        return []


# ============================================================
# EMA
# ============================================================

def ema(
    values,
    period,
):

    if len(values) < period:

        return [
            None
        ] * len(values)

    multiplier = (
        2.0
        / (
            period
            + 1.0
        )
    )

    result = [
        None
    ] * len(values)

    seed = (
        sum(
            values[
                :period
            ]
        )
        / period
    )

    result[
        period - 1
    ] = seed

    previous = seed

    for i in range(
        period,
        len(values),
    ):

        previous = (
            (
                values[i]
                - previous
            )
            * multiplier
            + previous
        )

        result[i] = previous

    return result


# ============================================================
# ATR
# ============================================================

def atr_values(
    candles,
    period=14,
):

    if len(candles) < (
        period + 1
    ):

        return [
            None
        ] * len(candles)

    trs = [
        None
    ] * len(candles)

    for i in range(
        1,
        len(candles),
    ):

        high = candles[i][
            "high"
        ]

        low = candles[i][
            "low"
        ]

        previous_close = (
            candles[
                i - 1
            ]["close"]
        )

        trs[i] = max(
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

    result = [
        None
    ] * len(candles)

    seed = [
        x
        for x in trs
        if x is not None
    ]

    if len(seed) < period:

        return result

    current = (
        sum(
            seed[
                :period
            ]
        )
        / period
    )

    result[
        period
    ] = current

    for i in range(
        period + 1,
        len(candles),
    ):

        if trs[i] is None:

            continue

        current = (
            (
                current
                * (
                    period - 1
                )
            )
            + trs[i]
        ) / period

        result[i] = current

    return result


# ============================================================
# RSI
# ============================================================

def rsi_values(
    closes,
    period=14,
):

    result = [
        None
    ] * len(closes)

    if len(closes) <= period:

        return result

    gains = []

    losses_list = []

    for i in range(
        1,
        len(closes),
    ):

        change = (
            closes[i]
            - closes[
                i - 1
            ]
        )

        gains.append(
            max(
                change,
                0.0
            )
        )

        losses_list.append(
            max(
                -change,
                0.0
            )
        )

    avg_gain = (
        sum(
            gains[
                :period
            ]
        )
        / period
    )

    avg_loss = (
        sum(
            losses_list[
                :period
            ]
        )
        / period
    )

    index = period

    if avg_loss == 0:

        result[index] = 100.0

    else:

        rs = (
            avg_gain
            / avg_loss
        )

        result[index] = (
            100.0
            - (
                100.0
                / (
                    1.0 + rs
                )
            )
        )

    for j in range(
        period,
        len(gains),
    ):

        avg_gain = (
            (
                avg_gain
                * (
                    period - 1
                )
            )
            + gains[j]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (
                    period - 1
                )
            )
            + losses_list[j]
        ) / period

        index = j + 1

        if avg_loss == 0:

            result[index] = 100.0

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result[index] = (
                100.0
                - (
                    100.0
                    / (
                        1.0 + rs
                    )
                )
            )

    return result


# ============================================================
# ADX
# ============================================================

def adx_values(
    candles,
    period=14,
):

    length = len(candles)

    result = [
        None
    ] * length

    if length < (
        period * 2
        + 2
    ):

        return result

    tr = [
        0.0
    ] * length

    plus_dm = [
        0.0
    ] * length

    minus_dm = [
        0.0
    ] * length

    for i in range(
        1,
        length,
    ):

        high = candles[i][
            "high"
        ]

        low = candles[i][
            "low"
        ]

        prev_high = (
            candles[
                i - 1
            ]["high"]
        )

        prev_low = (
            candles[
                i - 1
            ]["low"]
        )

        prev_close = (
            candles[
                i - 1
            ]["close"]
        )

        tr[i] = max(
            high - low,
            abs(
                high
                - prev_close
            ),
            abs(
                low
                - prev_close
            ),
        )

        up_move = (
            high
            - prev_high
        )

        down_move = (
            prev_low
            - low
        )

        if (
            up_move
            > down_move
            and up_move > 0
        ):

            plus_dm[i] = (
                up_move
            )

        if (
            down_move
            > up_move
            and down_move > 0
        ):

            minus_dm[i] = (
                down_move
            )

    atr = (
        sum(
            tr[
                1:period + 1
            ]
        )
        / period
    )

    plus = (
        sum(
            plus_dm[
                1:period + 1
            ]
        )
        / period
    )

    minus = (
        sum(
            minus_dm[
                1:period + 1
            ]
        )
        / period
    )

    dx_values = []

    for i in range(
        period + 1,
        length,
    ):

        atr = (
            (
                atr
                * (
                    period - 1
                )
            )
            + tr[i]
        ) / period

        plus = (
            (
                plus
                * (
                    period - 1
                )
            )
            + plus_dm[i]
        ) / period

        minus = (
            (
                minus
                * (
                    period - 1
                )
            )
            + minus_dm[i]
        ) / period

        if atr <= 0:

            continue

        plus_di = (
            100.0
            * plus
            / atr
        )

        minus_di = (
            100.0
            * minus
            / atr
        )

        denominator = (
            plus_di
            + minus_di
        )

        if denominator <= 0:

            continue

        dx = (
            100.0
            * abs(
                plus_di
                - minus_di
            )
            / denominator
        )

        dx_values.append(
            dx
        )

        if len(
            dx_values
        ) >= period:

            if (
                result[
                    i - 1
                ]
                is None
            ):

                adx = (
                    sum(
                        dx_values[
                            :period
                        ]
                    )
                    / period
                )

            else:

                adx = (
                    (
                        result[
                            i - 1
                        ]
                        * (
                            period - 1
                        )
                    )
                    + dx
                ) / period

            result[i] = adx

    return result


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_body(
    candle
):

    return abs(
        candle["close"]
        - candle["open"]
    )


def candle_range(
    candle
):

    return (
        candle["high"]
        - candle["low"]
    )


def is_bullish(
    candle
):

    return (
        candle["close"]
        > candle["open"]
    )


def is_bearish(
    candle
):

    return (
        candle["close"]
        < candle["open"]
    )


def body_ratio(
    candle
):

    rng = candle_range(
        candle
    )

    if rng <= 0:

        return 0.0

    return (
        candle_body(
            candle
        )
        / rng
    )


# ============================================================
# STRONG CANDLE — PINE #3
# ============================================================

def average_body(
    candles,
    lookback=20,
):

    if not candles:

        return 0.0

    subset = candles[
        -lookback:
    ]

    bodies = [
        candle_body(c)
        for c in subset
    ]

    if not bodies:

        return 0.0

    return (
        sum(bodies)
        / len(bodies)
    )


def is_strong_bullish(
    candles,
):

    if not candles:

        return False

    current = candles[
        -1
    ]

    avg = average_body(
        candles[
            :-1
        ],
        STRONG_CANDLE_AVG_LOOKBACK,
    )

    if avg <= 0:

        return False

    return (
        is_bullish(current)
        and candle_body(
            current
        )
        > (
            avg
            * STRONG_CANDLE_MULTIPLIER
        )
        and body_ratio(
            current
        )
        >= MIN_STRONG_BODY_RANGE
    )


def is_strong_bearish(
    candles,
):

    if not candles:

        return False

    current = candles[
        -1
    ]

    avg = average_body(
        candles[
            :-1
        ],
        STRONG_CANDLE_AVG_LOOKBACK,
    )

    if avg <= 0:

        return False

    return (
        is_bearish(current)
        and candle_body(
            current
        )
        > (
            avg
            * STRONG_CANDLE_MULTIPLIER
        )
        and body_ratio(
            current
        )
        >= MIN_STRONG_BODY_RANGE
    )


# ============================================================
# ENGULFING — PINE #4
# ============================================================

def bullish_engulfing(
    previous,
    current,
):

    return (
        is_bearish(
            previous
        )
        and is_bullish(
            current
        )
        and current[
            "open"
        ] <= previous[
            "close"
        ]
        and current[
            "close"
        ] >= previous[
            "open"
        ]
    )


def bearish_engulfing(
    previous,
    current,
):

    return (
        is_bullish(
            previous
        )
        and is_bearish(
            current
        )
        and current[
            "open"
        ] >= previous[
            "close"
        ]
        and current[
            "close"
        ] <= previous[
            "open"
        ]
    )


# ============================================================
# MORNING / EVENING STAR — PINE #4
# ============================================================

def morning_star(
    candles
):

    if len(candles) < 3:

        return False

    c2 = candles[-3]

    c1 = candles[-2]

    c0 = candles[-1]

    return (
        c2["low"]
        > c1["low"]
        and c1["low"]
        < c0["low"]
        and c2["close"]
        < c2["open"]
        and c1["close"]
        > c1["open"]
        and c0["close"]
        > max(
            c0["open"],
            c0["close"],
        )
        if False
        else (
            c2["low"]
            > c1["low"]
            and c1["low"]
            < c0["low"]
            and c2["close"]
            < c2["open"]
            and c1["close"]
            > c1["open"]
            and c0["close"]
            > max(
                c1["open"],
                c1["close"],
            )
            and candle_body(
                c1
            )
            < candle_body(
                c2
            )
        )
    )


def evening_star(
    candles
):

    if len(candles) < 3:

        return False

    c2 = candles[-3]

    c1 = candles[-2]

    c0 = candles[-1]

    return (
        c2["high"]
        < c1["high"]
        and c1["high"]
        > c0["high"]
        and c2["close"]
        > c2["open"]
        and c1["close"]
        < c1["open"]
        and c0["close"]
        < max(
            c1["open"],
            c1["close"],
        )
        and candle_body(
            c1
        )
        < candle_body(
            c2
        )
    )


# ============================================================
# REJECTION
# ============================================================

def bullish_rejection(
    candle
):

    rng = candle_range(
        candle
    )

    if rng <= 0:

        return False

    lower_wick = (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )

    body = candle_body(
        candle
    )

    return (
        lower_wick
        >= body
        and candle[
            "close"
        ]
        >= (
            candle["low"]
            + rng * 0.55
        )
    )


def bearish_rejection(
    candle
):

    rng = candle_range(
        candle
    )

    if rng <= 0:

        return False

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )

    body = candle_body(
        candle
    )

    return (
        upper_wick
        >= body
        and candle[
            "close"
        ]
        <= (
            candle["high"]
            - rng * 0.55
        )
    )


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def recent_support(
    candles,
    lookback=30,
):

    subset = candles[
        -lookback:
    ]

    if not subset:

        return None

    return min(
        c["low"]
        for c in subset
    )


def recent_resistance(
    candles,
    lookback=30,
):

    subset = candles[
        -lookback:
    ]

    if not subset:

        return None

    return max(
        c["high"]
        for c in subset
    )


# ============================================================
# RSI MULTI-BAR CONFIRMATION
# ============================================================

def average_recent_values(
    values,
    lookback,
):

    valid = [
        x
        for x in values[
            -lookback:
        ]
        if x is not None
    ]

    if len(valid) < lookback:

        return None

    return (
        sum(valid)
        / len(valid)
    )


def recent_rsi_extreme(
    rsi,
    threshold,
    direction,
    lookback=3,
):

    values = [
        x
        for x in rsi[
            -lookback:
        ]
        if x is not None
    ]

    if not values:

        return False

    if direction == "LOW":

        return any(
            x <= threshold
            for x in values
        )

    return any(
        x >= threshold
        for x in values
    )


# ============================================================
# EMA SLOPE
# ============================================================

def sampled_slope_percent(
    values,
    lookback=3,
):

    valid = [
        x
        for x in values[
            -lookback:
        ]
        if x is not None
    ]

    if len(valid) < 2:

        return None

    start = valid[0]

    end = valid[-1]

    if start == 0:

        return 0.0

    return (
        (
            end
            - start
        )
        / abs(start)
    ) * 100.0


def calculate_5m_sampled_ema50_slope(
    candles_1m
):

    if len(
        candles_1m
    ) < 30:

        return None

    closes = [
        c["close"]
        for c in candles_1m
    ]

    ema50 = ema(
        closes,
        EMA_SLOW_MEDIUM,
    )

    # Sample the 1M EMA50 at the
    # closes of the latest 5M candles.
    sampled = []

    current_bucket = None

    bucket_last_index = None

    for i, candle in enumerate(
        candles_1m
    ):

        bucket = int(
            candle["from"]
            // TF5
        )

        if (
            current_bucket
            is None
            or bucket
            != current_bucket
        ):

            if (
                bucket_last_index
                is not None
            ):

                value = ema50[
                    bucket_last_index
                ]

                if value is not None:

                    sampled.append(
                        value
                    )

            current_bucket = bucket

        bucket_last_index = i

    if (
        bucket_last_index
        is not None
    ):

        value = ema50[
            bucket_last_index
        ]

        if value is not None:

            sampled.append(
                value
            )

    return sampled_slope_percent(
        sampled,
        SLOPE_LOOKBACK,
    )


# ============================================================
# RANGE DETECTION
# ============================================================

def is_ranging_from_slope(
    candles_1m
):

    closes = [
        c["close"]
        for c in candles_1m
    ]

    ema50 = ema(
        closes,
        EMA_SLOW_MEDIUM,
    )

    valid = [
        x
        for x in ema50[
            -RANGE_CONFIRM_BARS:
        ]
        if x is not None
    ]

    if len(valid) < (
        RANGE_CONFIRM_BARS
    ):

        return False

    changes = []

    for i in range(
        1,
        len(valid),
    ):

        previous = valid[
            i - 1
        ]

        current = valid[i]

        if previous == 0:

            continue

        changes.append(
            (
                (
                    current
                    - previous
                )
                / abs(previous)
            )
            * 100.0
        )

    if not changes:

        return False

    return all(
        abs(x)
        < FLAT_SLOPE_PERCENT
        for x in changes
    )


# ============================================================
# MAIN ZETA V3 ENGINE
# ============================================================

def evaluate_zeta_v3(
    asset,
    candles_5m,
    candles_1m,
):

    global signals_found

    if len(
        candles_5m
    ) < 80:

        remember_rejection(
            "insufficient_5m_data"
        )

        return None

    if len(
        candles_1m
    ) < 1000:

        remember_rejection(
            "insufficient_1m_macro_data"
        )

        return None

    # ========================================================
    # CURRENT CLOSED CANDLES
    # ========================================================

    current = candles_1m[-1]

    previous = candles_1m[-2]

    candle_time = (
        current["from"]
    )

    price = current[
        "close"
    ]


    # ========================================================
    # 1M EMA SYSTEM — PINE #3
    # ========================================================

    closes_1m = [
        c["close"]
        for c in candles_1m
    ]

    ema3 = ema(
        closes_1m,
        EMA_FAST,
    )

    ema21 = ema(
        closes_1m,
        EMA_MEDIUM,
    )

    ema50 = ema(
        closes_1m,
        EMA_SLOW_MEDIUM,
    )

    ema200 = ema(
        closes_1m,
        EMA_SLOW,
    )

    i = len(
        candles_1m
    ) - 1

    p = i - 1

    values = (
        ema3[i],
        ema21[i],
        ema50[i],
        ema200[i],
        ema3[p],
        ema21[p],
        ema50[p],
        ema200[p],
    )

    if any(
        x is None
        for x in values
    ):

        remember_rejection(
            "ema_not_ready"
        )

        return None

    e3 = ema3[i]

    e21 = ema21[i]

    e50 = ema50[i]

    e200 = ema200[i]

    p_e3 = ema3[p]

    p_e21 = ema21[p]

    p_e50 = ema50[p]

    p_e200 = ema200[p]


    # ========================================================
    # PRIMARY TREND — 3/21/50/200
    # ========================================================

    bullish_primary = (
        e3 > e21
        and e21 > e50
        and e50 > e200
        and price > e21
        and price > e50
    )

    bearish_primary = (
        e3 < e21
        and e21 < e50
        and e50 < e200
        and price < e21
        and price < e50
    )


    # ========================================================
    # PRIMARY REGIME CROSS
    # ========================================================

    bullish_regime = (
        e21 >= e50
        and p_e21 >= p_e50
    )

    bearish_regime = (
        e21 <= e50
        and p_e21 <= p_e50
    )

    if not (
        bullish_primary
        or bearish_primary
    ):

        remember_rejection(
            "primary_ema_trend"
        )

        return None


    # ========================================================
    # MACRO EMA — PINE #4
    # ========================================================

    macro200 = ema(
        closes_1m,
        MACRO_EMA_FAST,
    )

    macro600 = ema(
        closes_1m,
        MACRO_EMA_MEDIUM,
    )

    macro1000 = ema(
        closes_1m,
        MACRO_EMA_SLOW,
    )

    macro_values = (
        macro200[i],
        macro600[i],
        macro1000[i],
    )

    if any(
        x is None
        for x in macro_values
    ):

        remember_rejection(
            "macro_ema_not_ready"
        )

        return None

    m200 = macro200[i]

    m600 = macro600[i]

    m1000 = macro1000[i]

    macro_bullish = (
        m200 > m600
        and m600 > m1000
        and price > m200
        and price > m600
        and price > m1000
    )

    macro_bearish = (
        m1000 > m600
        and m600 > m200
        and price < m200
        and price < m600
        and price < m1000
    )


    # ========================================================
    # 5M RSI(20) — PINE #3
    # ========================================================

    closes_5m = [
        c["close"]
        for c in candles_5m
    ]

    rsi5 = rsi_values(
        closes_5m,
        RSI_MTF_PERIOD,
    )

    current_rsi5 = rsi5[-1]

    average_rsi5 = (
        average_recent_values(
            rsi5,
            RSI_MTF_LOOKBACK,
        )
    )

    if (
        current_rsi5 is None
        or average_rsi5 is None
    ):

        remember_rejection(
            "rsi5_not_ready"
        )

        return None

    rsi_bullish = (
        current_rsi5
        >= RSI_MTF_LONG
        and average_rsi5
        >= RSI_MTF_LONG
    )

    rsi_bearish = (
        current_rsi5
        <= RSI_MTF_SHORT
        and average_rsi5
        <= RSI_MTF_SHORT
    )


    # ========================================================
    # 5M EMA50 SLOPE
    # ========================================================

    slope = (
        calculate_5m_sampled_ema50_slope(
            candles_1m
        )
    )

    if slope is None:

        remember_rejection(
            "slope_not_ready"
        )

        return None

    slope_bullish = (
        slope
        >= MIN_BULL_SLOPE_PERCENT
    )

    slope_bearish = (
        slope
        <= MIN_BEAR_SLOPE_PERCENT
    )

    ranging = (
        abs(slope)
        < FLAT_SLOPE_PERCENT
        and is_ranging_from_slope(
            candles_1m
        )
    )

    if ranging:

        remember_rejection(
            "ranging_market"
        )

        return None


    # ========================================================
    # 1M RSI14 — PRICE ACTION PULLBACK
    # ========================================================

    rsi14 = rsi_values(
        closes_1m,
        RSI_PRICE_ACTION_PERIOD,
    )

    current_rsi14 = rsi14[-1]

    if current_rsi14 is None:

        remember_rejection(
            "rsi14_not_ready"
        )

        return None

    recent_oversold = (
        recent_rsi_extreme(
            rsi14,
            RSI_OVERSOLD,
            "LOW",
            RSI_PULLBACK_LOOKBACK,
        )
    )

    recent_overbought = (
        recent_rsi_extreme(
            rsi14,
            RSI_OVERBOUGHT,
            "HIGH",
            RSI_PULLBACK_LOOKBACK,
        )
    )

    bullish_rsi_pullback = (
        recent_oversold
        and current_rsi14 > 30
        and current_rsi14 < 55
    )

    bearish_rsi_pullback = (
        recent_overbought
        and current_rsi14 < 70
        and current_rsi14 > 45
    )


    # ========================================================
    # ATR / ADX
    # ========================================================

    atr1 = atr_values(
        candles_1m,
        ATR_PERIOD,
    )

    atr5 = atr_values(
        candles_5m,
        ATR_PERIOD,
    )

    adx5 = adx_values(
        candles_5m,
        ADX_PERIOD,
    )

    current_atr1 = atr1[-1]

    current_atr5 = atr5[-1]

    current_adx = adx5[-1]

    if (
        current_atr1 is None
        or current_atr5 is None
        or current_adx is None
    ):

        remember_rejection(
            "atr_adx_not_ready"
        )

        return None

    if current_atr1 <= 0:

        remember_rejection(
            "invalid_atr"
        )

        return None

    if current_adx < MIN_ADX:

        remember_rejection(
            "adx_below_minimum"
        )

        return None


    # ========================================================
    # PULLBACK / EXTENSION
    # ========================================================

    distance_from_ema21 = abs(
        price - e21
    )

    pullback_atr = (
        distance_from_ema21
        / current_atr1
    )

    if (
        pullback_atr
        < MIN_PULLBACK_ATR
    ):

        remember_rejection(
            "pullback_too_small"
        )

        return None

    if (
        pullback_atr
        > MAX_PULLBACK_ATR
    ):

        remember_rejection(
            "pullback_too_deep"
        )

        return None

    if (
        pullback_atr
        > MAX_EXTENSION_ATR
    ):

        remember_rejection(
            "extension_too_large"
        )

        return None


    # ========================================================
    # SUPPORT / RESISTANCE
    # ========================================================

    support = recent_support(
        candles_1m,
        30,
    )

    resistance = recent_resistance(
        candles_1m,
        30,
    )

    if (
        support is None
        or resistance is None
    ):

        remember_rejection(
            "no_support_resistance"
        )

        return None

    zone_tolerance = (
        current_atr1
        * ZONE_TOLERANCE_ATR
    )

    near_support = (
        abs(
            price
            - support
        )
        <= zone_tolerance
    )

    near_resistance = (
        abs(
            price
            - resistance
        )
        <= zone_tolerance
    )

    near_ema21 = (
        abs(
            price
            - e21
        )
        <= zone_tolerance
    )


    # ========================================================
    # PRICE ACTION — PINE #4
    # ========================================================

    bull_engulf = (
        bullish_engulfing(
            previous,
            current,
        )
    )

    bear_engulf = (
        bearish_engulfing(
            previous,
            current,
        )
    )

    bull_morning = (
        morning_star(
            candles_1m
        )
    )

    bear_evening = (
        evening_star(
            candles_1m
        )
    )

    bullish_pattern = (
        bull_engulf
        or bull_morning
    )

    bearish_pattern = (
        bear_engulf
        or bear_evening
    )


    # ========================================================
    # STRONG CANDLE — PINE #3
    # ========================================================

    strong_bull = (
        is_strong_bullish(
            candles_1m
        )
    )

    strong_bear = (
        is_strong_bearish(
            candles_1m
        )
    )


    # ========================================================
    # CANDLE CONFIRMATION
    # ========================================================

    bull_rejection = (
        bullish_rejection(
            current
        )
    )

    bear_rejection = (
        bearish_rejection(
            current
        )
    )

    bull_candle_confirm = (
        is_bullish(current)
        and (
            strong_bull
            or bullish_pattern
            or bull_rejection
        )
    )

    bear_candle_confirm = (
        is_bearish(current)
        and (
            strong_bear
            or bearish_pattern
            or bear_rejection
        )
    )


    # ========================================================
    # ROOM
    # ========================================================

    room_up = (
        resistance
        - price
    )

    room_down = (
        price
        - support
    )

    room_up_atr = (
        room_up
        / current_atr1
    )

    room_down_atr = (
        room_down
        / current_atr1
    )


    # ========================================================
    # DIRECTION
    # ========================================================

    direction = None

    reasons = []

    score = 0


    # ========================================================
    # CALL
    # ========================================================

    if bullish_primary:

        # 20 — primary EMA trend
        score += 20

        reasons.append(
            "3/21/50/200 bullish"
        )

        # 15 — macro trend
        if macro_bullish:

            score += 15

            reasons.append(
                "200/600/1000 bullish"
            )

        else:

            remember_rejection(
                "macro_not_bullish"
            )

            return None

        # 15 — RSI
        if rsi_bullish:

            score += 15

            reasons.append(
                "5M RSI bullish"
            )

        else:

            remember_rejection(
                "5m_rsi_not_bullish"
            )

            return None

        # 10 — slope
        if slope_bullish:

            score += 10

            reasons.append(
                "EMA50 positive slope"
            )

        else:

            remember_rejection(
                "bullish_slope_failed"
            )

            return None

        # 15 — price action
        if bullish_pattern:

            score += 15

            if bull_engulf:

                reasons.append(
                    "bullish engulfing"
                )

            if bull_morning:

                reasons.append(
                    "morning star"
                )

        elif bull_candle_confirm:

            score += 10

            reasons.append(
                "bullish candle confirmation"
            )

        else:

            remember_rejection(
                "no_bullish_trigger"
            )

            return None

        # 10 — strong candle
        if strong_bull:

            score += 10

            reasons.append(
                "strong bullish candle"
            )

        # 5 — pullback
        if (
            bullish_rsi_pullback
            or near_ema21
            or near_support
        ):

            score += 5

            reasons.append(
                "bullish pullback"
            )

        # 5 — room
        if (
            room_up_atr
            >= MIN_ROOM_ATR
        ):

            score += 5

            reasons.append(
                "room to resistance"
            )

        else:

            remember_rejection(
                "insufficient_call_room"
            )

            return None

        # 5 — ADX
        if current_adx >= MIN_ADX:

            score += 5

            reasons.append(
                "ADX trend strength"
            )

        # final confirmation
        if not (
            bull_candle_confirm
        ):

            remember_rejection(
                "bullish_candle_failed"
            )

            return None

        direction = "CALL"


    # ========================================================
    # PUT
    # ========================================================

    elif bearish_primary:

        # 20 — primary EMA trend
        score += 20

        reasons.append(
            "3/21/50/200 bearish"
        )

        # 15 — macro trend
        if macro_bearish:

            score += 15

            reasons.append(
                "200/600/1000 bearish"
            )

        else:

            remember_rejection(
                "macro_not_bearish"
            )

            return None

        # 15 — RSI
        if rsi_bearish:

            score += 15

            reasons.append(
                "5M RSI bearish"
            )

        else:

            remember_rejection(
                "5m_rsi_not_bearish"
            )

            return None

        # 10 — slope
        if slope_bearish:

            score += 10

            reasons.append(
                "EMA50 negative slope"
            )

        else:

            remember_rejection(
                "bearish_slope_failed"
            )

            return None

        # 15 — price action
        if bearish_pattern:

            score += 15

            if bear_engulf:

                reasons.append(
                    "bearish engulfing"
                )

            if bear_evening:

                reasons.append(
                    "evening star"
                )

        elif bear_candle_confirm:

            score += 10

            reasons.append(
                "bearish candle confirmation"
            )

        else:

            remember_rejection(
                "no_bearish_trigger"
            )

            return None

        # 10 — strong candle
        if strong_bear:

            score += 10

            reasons.append(
                "strong bearish candle"
            )

        # 5 — pullback
        if (
            bearish_rsi_pullback
            or near_ema21
            or near_resistance
        ):

            score += 5

            reasons.append(
                "bearish pullback"
            )

        # 5 — room
        if (
            room_down_atr
            >= MIN_ROOM_ATR
        ):

            score += 5

            reasons.append(
                "room to support"
            )

        else:

            remember_rejection(
                "insufficient_put_room"
            )

            return None

        # 5 — ADX
        if current_adx >= MIN_ADX:

            score += 5

            reasons.append(
                "ADX trend strength"
            )

        if not (
            bear_candle_confirm
        ):

            remember_rejection(
                "bearish_candle_failed"
            )

            return None

        direction = "PUT"


    # ========================================================
    # NO DIRECTION
    # ========================================================

    if direction is None:

        remember_rejection(
            "no_direction"
        )

        return None


    # ========================================================
    # SCORE FILTER
    # ========================================================

    if score < MIN_SCORE:

        remember_rejection(
            "score_below_minimum"
        )

        return None


    # ========================================================
    # SIGNAL COOLDOWN
    # ========================================================

    previous_signal_time = (
        last_signal_time.get(
            asset,
            0,
        )
    )

    if previous_signal_time:

        if (
            candle_time
            - previous_signal_time
            < SIGNAL_LOCK_SECONDS
        ):

            remember_rejection(
                "signal_cooldown"
            )

            return None


    # ========================================================
    # TRADE LOCK
    # ========================================================

    previous_trade_time = (
        last_trade_time.get(
            asset,
            0,
        )
    )

    if previous_trade_time:

        if (
            time.time()
            - previous_trade_time
            < ASSET_LOCK_SECONDS
        ):

            remember_rejection(
                "asset_trade_lock"
            )

            return None


    # ========================================================
    # SIGNAL ID
    # ========================================================

    signal_id = (
        "ZETA3-"
        + asset.replace(
            "-",
            "",
        )
        + "-"
        + direction
        + "-"
        + datetime.now(
            timezone.utc
        ).strftime(
            "%H%M%S"
        )
    )

    last_signal_time[
        asset
    ] = candle_time

    signals_found += 1


    # ========================================================
    # RETURN SIGNAL
    # ========================================================

    return {
        "signal_id":
            signal_id,

        "asset":
            asset,

        "direction":
            direction,

        "score":
            score,

        "price":
            price,

        "ema3":
            e3,

        "ema21":
            e21,

        "ema50":
            e50,

        "ema200":
            e200,

        "macro200":
            m200,

        "macro600":
            m600,

        "macro1000":
            m1000,

        "rsi5":
            current_rsi5,

        "rsi5_average":
            average_rsi5,

        "rsi14":
            current_rsi14,

        "ema50_slope":
            slope,

        "adx5":
            current_adx,

        "atr1":
            current_atr1,

        "pullback_atr":
            pullback_atr,

        "room_up_atr":
            room_up_atr,

        "room_down_atr":
            room_down_atr,

        "bullish_pattern":
            bullish_pattern,

        "bearish_pattern":
            bearish_pattern,

        "strong_bull":
            strong_bull,

        "strong_bear":
            strong_bear,

        "timestamp":
            now_utc(),

        "reasons":
            reasons,
    }


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(
    signal
):

    direction = (
        signal["direction"]
    )

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    reasons = ", ".join(
        signal["reasons"]
    )

    return (
        f"{emoji} *ZETA V3 SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {signal['asset']}\n"
        f"*Direction:* *{direction}*\n"
        f"*Score:* *{signal['score']}/100*\n"
        "*Strategy:* 3EMA + RSI + Price Action\n"
        "*Trend:* 3/21/50/200 EMA\n"
        "*Macro:* 200/600/1000 EMA\n"
        "*Context:* 5M\n"
        "*Entry:* 1M\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*5M RSI:* {signal['rsi5']:.2f}\n"
        f"*5M RSI Avg:* {signal['rsi5_average']:.2f}\n"
        f"*1M RSI:* {signal['rsi14']:.2f}\n"
        f"*EMA50 slope:* {signal['ema50_slope']:.4f}%\n"
        f"*ADX 5M:* {signal['adx5']:.2f}\n"
        f"*Pullback:* {signal['pullback_atr']:.2f} ATR\n"
        f"*Room UP:* {signal['room_up_atr']:.2f} ATR\n"
        f"*Room DOWN:* {signal['room_down_atr']:.2f} ATR\n"
        f"*Price:* {signal['price']:.8f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Confirmations:* {reasons}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        f"*Time:* {signal['timestamp']}\n"
        "🤖 *ZETA V3 — PRACTICE AUTO TRADE*"
    )


# ============================================================
# EXECUTE PRACTICE TRADE
# ============================================================

def execute_demo_trade(
    signal
):

    global total_trades

    if api is None:

        return False

    asset = signal[
        "asset"
    ]

    direction = signal[
        "direction"
    ]

    option_type = (
        "call"
        if direction == "CALL"
        else "put"
    )

    active_id = (
        OP_code.ACTIVES.get(
            asset
        )
    )

    if active_id is None:

        print(
            "[TRADE ERROR] "
            "No active ID:",
            asset,
        )

        send_telegram(
            "🔴 *ZETA V3 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            "*Reason:* No IQ Option "
            "active ID is mapped."
        )

        return False

    print(
        "\n"
        + "=" * 70
    )

    print(
        "[ZETA V3 AUTO EXECUTION]"
    )

    print(
        "Asset:",
        asset
    )

    print(
        "Active ID:",
        active_id
    )

    print(
        "Direction:",
        direction
    )

    print(
        "Score:",
        signal["score"]
    )

    print(
        "Stake:",
        STAKE
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES
    )

    print(
        "Account:",
        BALANCE_MODE
    )

    print(
        "=" * 70
    )

    send_telegram(
        "🟡 *ZETA V3 SENDING DEMO ORDER*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {asset}\n"
        f"*Active ID:* {active_id}\n"
        f"*Direction:* {direction}\n"
        f"*Score:* {signal['score']}/100\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Sending order to IQ Option..."
    )

    try:

        result = api.buy(
            STAKE,
            asset,
            option_type,
            EXPIRY_MINUTES,
        )

        print(
            "[BUY RAW RESULT]:",
            repr(result),
        )

    except Exception as e:

        print(
            "[BUY EXCEPTION]",
            repr(e),
        )

        traceback.print_exc()

        send_telegram(
            "🔴 *ZETA V3 TRADE ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Error:* {str(e)[:700]}"
        )

        return False

    success = False

    trade_id = None

    if isinstance(
        result,
        (tuple, list),
    ):

        if len(result) >= 2:

            success = bool(
                result[0]
            )

            trade_id = result[1]

        elif len(result) == 1:

            success = bool(
                result[0]
            )

    else:

        success = bool(
            result
        )

    if not success:

        print(
            "[BUY FAILED]",
            repr(result),
        )

        send_telegram(
            "🔴 *ZETA V3 TRADE REJECTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Score:* {signal['score']}/100\n"
            f"*Signal ID:* {signal['signal_id']}\n"
            f"*IQ Option response:* "
            f"{str(result)[:700]}"
        )

        return False

    total_trades += 1

    last_trade_time[
        asset
    ] = time.time()

    if trade_id is None:

        print(
            "[BUY ACCEPTED] "
            "No trade ID returned."
        )

        send_telegram(
            "🟡 *ZETA V3 ORDER ACCEPTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Trade #:* "
            f"{total_trades}/{TARGET_TRADES}\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Score:* {signal['score']}/100\n"
            f"*Stake:* ${STAKE:.2f}\n"
            f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
            f"*Signal ID:* {signal['signal_id']}\n"
            "*Trade ID:* NOT RETURNED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📋 *Track result manually in IQ Option.*"
        )

        return True

    trade_id = str(
        trade_id
    )

    print(
        "[BUY SUCCESS]",
        trade_id,
    )

    send_telegram(
        "🚀 *ZETA V3 DEMO TRADE OPENED*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Trade #:* "
        f"{total_trades}/{TARGET_TRADES}\n"
        f"*Asset:* {asset}\n"
        f"*Direction:* *{direction}*\n"
        f"*Score:* *{signal['score']}/100*\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* *{EXPIRY_MINUTES} minutes*\n"
        f"*Trade ID:* {trade_id}\n"
        f"*Signal ID:* {signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 *PRACTICE / DEMO ONLY*\n"
        "📋 *Track result manually in IQ Option.*"
    )

    return True


# ============================================================
# TEST REAL OTC FEEDS
# ============================================================

def test_candle_access(
    assets
):

    print(
        "\n"
        + "=" * 70
    )

    print(
        "TESTING REAL OTC CANDLE FEEDS"
    )

    print(
        "=" * 70
    )

    working = []

    for item in assets:

        asset = item[
            "asset"
        ]

        mapped_id = (
            OP_code.ACTIVES.get(
                asset
            )
        )

        if mapped_id is None:

            continue

        candles = (
            get_candles_safe(
                asset,
                TF1,
                20,
            )
        )

        if len(
            candles
        ) >= 5:

            working.append(
                item
            )

            print(
                "[FEED OK]",
                asset,
                "| candles=",
                len(candles),
            )

        else:

            print(
                "[FEED FAILED]",
                asset,
                "| candles=",
                len(candles),
            )

    print(
        "\nWORKING OTC CANDLE FEEDS:",
        len(working),
    )

    return working


# ============================================================
# DISCOVER + TEST OTC
# ============================================================

def refresh_otc_assets():

    global otc_assets

    global last_discovery_time

    raw_data = (
        get_raw_initialization()
    )

    if not raw_data:

        print(
            "[OTC] Initialization "
            "returned no data."
        )

        return False

    discovered = (
        discover_otc_from_initialization(
            raw_data
        )
    )

    if not discovered:

        print(
            "[OTC] 0 real OTC assets."
        )

        return False

    working = (
        test_candle_access(
            discovered
        )
    )

    if working:

        otc_assets = working

        last_discovery_time = (
            time.time()
        )

        print(
            "[OTC] READY:",
            len(otc_assets),
        )

        return True

    print(
        "[OTC] Assets discovered, "
        "but candle feeds failed."
    )

    return False


# ============================================================
# CONNECTION
# ============================================================

def connection_is_alive():

    global last_connection_check

    if api is None:

        return False

    if (
        time.time()
        - last_connection_check
        < RECONNECT_INTERVAL
    ):

        return True

    last_connection_check = (
        time.time()
    )

    try:

        if hasattr(
            api,
            "check_connect",
        ):

            return bool(
                api.check_connect()
            )

    except Exception:

        pass

    return True


# ============================================================
# REJECTION SUMMARY
# ============================================================

def rejection_summary():

    if not rejection_counts:

        return "No rejections recorded."

    ordered = sorted(
        rejection_counts.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    lines = []

    for reason, count in ordered[
        :6
    ]:

        lines.append(
            f"• {reason}: {count}"
        )

    return "\n".join(
        lines
    )


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat():

    balance = None

    try:

        balance = (
            api.get_balance()
        )

    except Exception:

        pass

    message = (
        "🟡 *ZETA V3 HEARTBEAT*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Status:* ONLINE\n"
        f"*OTC feeds:* {len(otc_assets)}\n"
        f"*Demo orders:* "
        f"{total_trades}/{TARGET_TRADES}\n"
        f"*Scan cycles:* {scan_cycles}\n"
        f"*Signals found:* {signals_found}\n"
        f"*Runtime:* {runtime_string()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Auto-trading:* ON\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*Minimum score:* {MIN_SCORE}/100\n"
    )

    if balance is not None:

        message += (
            f"*Demo balance:* "
            f"${safe_float(balance):.2f}\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "*Top rejection reasons:*\n"
        f"{rejection_summary()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *ZETA V3 SCANNING REAL OTC MARKETS*"
    )

    send_telegram(
        message
    )


# ============================================================
# NO TRADE STATUS
# ============================================================

def send_no_trade_summary():

    message = (
        "⚪ *ZETA V3 — NO TRADE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*OTC feeds:* {len(otc_assets)}\n"
        f"*Orders:* {total_trades}/{TARGET_TRADES}\n"
        f"*Signals found:* {signals_found}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Reason summary:*\n"
        f"{rejection_summary()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "No setup passed the full combined strategy."
    )

    send_telegram(
        message
    )


# ============================================================
# CONNECT IQ OPTION
# ============================================================

def connect_iq():

    global api

    print(
        "\n"
        + "=" * 70
    )

    print(
        "CONNECTING TO IQ OPTION"
    )

    print(
        "=" * 70
    )

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    connected, reason = (
        api.connect()
    )

    print(
        "[LOGIN]",
        connected,
        reason,
    )

    if not connected:

        return False

    try:

        api.change_balance(
            BALANCE_MODE
        )

    except Exception as e:

        print(
            "[BALANCE WARNING]",
            repr(e),
        )

    try:

        balance = (
            api.get_balance()
        )

        print(
            "[BALANCE]",
            balance,
        )

    except Exception:

        pass

    return True


# ============================================================
# TARGET
# ============================================================

def target_reached():

    return (
        total_trades
        >= TARGET_TRADES
    )


# ============================================================
# MAIN TRADER
# ============================================================

def run_trader():

    global last_status_time

    global last_discovery_time

    global scan_cycles

    global no_trade_cycles

    if (
        not IQ_EMAIL
        or not IQ_PASSWORD
    ):

        message = (
            "🔴 *ZETA V3*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD "
            "is missing from GitHub Secrets."
        )

        print(
            message
        )

        send_telegram(
            message
        )

        return

    # ========================================================
    # CONNECT
    # ========================================================

    while not connect_iq():

        send_telegram(
            "🔴 *ZETA V3 CONNECTION FAILED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Retrying IQ Option connection "
            "in 30 seconds..."
        )

        time.sleep(
            RECONNECT_INTERVAL
        )

    print(
        "\n"
        + "=" * 70
    )

    print(
        "🟢 ZETA V3 DEMO TRADER ONLINE"
    )

    print(
        "=" * 70
    )

    send_telegram(
        "🟢 *ZETA V3 ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Connection:* OK\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Strategy:* 3EMA + RSI + Trend Price Action\n"
        "*Primary:* EMA 3 / 21 / 50 / 200\n"
        "*Macro:* EMA 200 / 600 / 1000\n"
        "*Momentum:* 5M RSI(20)\n"
        "*Price Action:* Engulfing / Morning-Star / Evening-Star\n"
        "*Entry:* 1M closed candle\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        f"*Minimum score:* {MIN_SCORE}/100\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Target:* {TARGET_TRADES} demo orders\n"
        "*Result tracking:* MANUAL\n"
        "*Auto-trading:* ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Using real IQ Option OTC initialization..."
    )

    # ========================================================
    # INITIAL DISCOVERY
    # ========================================================

    while not otc_assets:

        refresh_success = (
            refresh_otc_assets()
        )

        if refresh_success:

            send_telegram(
                "🟢 *REAL OTC FEEDS READY*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"*Working OTC feeds:* "
                f"{len(otc_assets)}\n"
                "*Data:* REAL IQ OPTION CANDLES\n"
                "*Strategy:* ZETA V3\n"
                "*Context:* 5M\n"
                "*Entry:* 1M\n"
                f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "ZETA V3 scanning started."
            )

            break

        send_telegram(
            "🟡 *WAITING FOR OTC MARKETS*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ Option connection is active,\n"
            "but no working OTC candle feeds "
            "were confirmed yet.\n"
            "Retrying real IQ Option initialization..."
        )

        time.sleep(
            RECONNECT_INTERVAL
        )

    last_status_time = (
        time.time()
    )

    last_scan_cycle = 0

    # ========================================================
    # CONTINUOUS LOOP
    # ========================================================

    while True:

        try:

            if target_reached():

                send_telegram(
                    "🏁 *ZETA V3 50-TRADE TEST REACHED*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"*Demo orders opened:* "
                    f"{total_trades}\n"
                    "*Results:* Tracked manually in IQ Option\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "The 50-order Practice test is complete."
                )

                break

            current_time = (
                time.time()
            )

            # =================================================
            # CONNECTION
            # =================================================

            if not connection_is_alive():

                print(
                    "[CONNECTION] Lost."
                )

                send_telegram(
                    "🔴 *ZETA V3 CONNECTION LOST*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Attempting reconnect..."
                )

                try:

                    api.connect()

                    api.change_balance(
                        BALANCE_MODE
                    )

                    print(
                        "[CONNECTION] Reconnected."
                    )

                except Exception as e:

                    print(
                        "[RECONNECT ERROR]",
                        repr(e),
                    )

                    time.sleep(
                        RECONNECT_INTERVAL
                    )

                    continue

            # =================================================
            # OTC REFRESH
            # =================================================

            if (
                not otc_assets
                or (
                    current_time
                    - last_discovery_time
                    >= DISCOVERY_INTERVAL
                )
            ):

                print(
                    "\n[OTC] Refreshing "
                    "real IQ Option OTC markets..."
                )

                refresh_otc_assets()

            # =================================================
            # SCAN
            # =================================================

            if (
                current_time
                - last_scan_cycle
                >= SCAN_INTERVAL
            ):

                last_scan_cycle = (
                    current_time
                )

                scan_cycles += 1

                cycle_signals = 0

                if not otc_assets:

                    print(
                        "[SCAN] "
                        "No working OTC feeds."
                    )

                else:

                    print(
                        "\n"
                        + "-" * 70
                    )

                    print(
                        "[ZETA V3 SCAN]",
                        now_utc(),
                    )

                    print(
                        "OTC feeds:",
                        len(otc_assets),
                    )

                    print(
                        "Demo orders:",
                        total_trades,
                    )

                    for item in list(
                        otc_assets
                    ):

                        if target_reached():

                            break

                        asset = item[
                            "asset"
                        ]

                        try:

                            # ---------------------------------
                            # 5M
                            # ---------------------------------

                            candles_5m = (
                                get_candles_safe(
                                    asset,
                                    TF5,
                                    CANDLE_COUNT_5M,
                                )
                            )

                            if len(
                                candles_5m
                            ) < 80:

                                remember_rejection(
                                    "5m_feed_short"
                                )

                                continue

                            # ---------------------------------
                            # 1M
                            # ---------------------------------

                            candles_1m = (
                                get_candles_safe(
                                    asset,
                                    TF1,
                                    CANDLE_COUNT_1M,
                                )
                            )

                            if len(
                                candles_1m
                            ) < 1000:

                                remember_rejection(
                                    "1m_feed_short"
                                )

                                continue

                            # ---------------------------------
                            # STRATEGY
                            # ---------------------------------

                            signal = (
                                evaluate_zeta_v3(
                                    asset,
                                    candles_5m,
                                    candles_1m,
                                )
                            )

                            if signal is None:

                                continue

                            cycle_signals += 1

                            print(
                                "\n[ZETA V3 SIGNAL]",
                                asset,
                                signal[
                                    "direction"
                                ],
                                "SCORE=",
                                signal[
                                    "score"
                                ],
                            )

                            send_telegram(
                                format_signal(
                                    signal
                                )
                            )

                            execute_demo_trade(
                                signal
                            )

                        except Exception as e:

                            print(
                                "[ASSET ERROR]",
                                asset,
                                repr(e),
                            )

                            traceback.print_exc()

                            remember_rejection(
                                "asset_exception"
                            )

                    if cycle_signals == 0:

                        no_trade_cycles += 1

                        print(
                            "[ZETA V3] "
                            "NO TRADE — "
                            "no asset passed."
                        )

                    else:

                        print(
                            "[ZETA V3] "
                            f"{cycle_signals} "
                            "signal(s) found."
                        )

                    print(
                        "[ZETA V3 SCAN COMPLETE]",
                        now_utc(),
                    )

            # =================================================
            # HEARTBEAT
            # =================================================

            if (
                time.time()
                - last_status_time
                >= STATUS_INTERVAL
            ):

                last_status_time = (
                    time.time()
                )

                send_heartbeat()

            time.sleep(1)

        except KeyboardInterrupt:

            print(
                "\n[STOP] "
                "Keyboard interrupt."
            )

            break

        except Exception as e:

            print(
                "\n[MAIN LOOP ERROR]",
                repr(e),
            )

            traceback.print_exc()

            time.sleep(5)


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 70
    )

    print(
        "ZETA V3 — IQ OPTION OTC DEMO TRADER"
    )

    print(
        "3EMA + RSI + TREND PRICE ACTION"
    )

    print(
        "=" * 70
    )

    print(
        "Started:",
        now_utc(),
    )

    print(
        "Primary EMA:",
        "3 / 21 / 50 / 200",
    )

    print(
        "Macro EMA:",
        "200 / 600 / 1000",
    )

    print(
        "5M RSI:",
        RSI_MTF_PERIOD,
    )

    print(
        "1M RSI:",
        RSI_PRICE_ACTION_PERIOD,
    )

    print(
        "Context:",
        "5M",
    )

    print(
        "Entry:",
        "1M",
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
        "minutes",
    )

    print(
        "Account:",
        BALANCE_MODE,
    )

    print(
        "Stake:",
        STAKE,
    )

    print(
        "Minimum score:",
        MIN_SCORE,
    )

    print(
        "Target:",
        TARGET_TRADES,
        "demo orders",
    )

    print(
        "Result tracking:",
        "MANUAL",
    )

    print(
        "Automatic trading:",
        "ENABLED",
    )

    print(
        "=" * 70
    )

    try:

        run_trader()

    except Exception as e:

        print(
            "\n[FATAL ERROR]",
            repr(e),
        )

        traceback.print_exc()

        send_telegram(
            "🔴 *ZETA V3 FATAL ERROR*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{str(e)[:800]}"
        )

    finally:

        if api is not None:

            try:

                api.close()

            except Exception:

                pass

        print(
            "\nZETA V3 stopped."
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
