import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# ZETA V3 — 3EMA + RSI + TREND-BASED PRICE ACTION
# REAL IQ OPTION OTC DISCOVERY
# PRACTICE / DEMO ONLY
#
# STRATEGY SOURCES:
#   1. 3EMA + RSI Strategy v3
#   2. Trend-based Price Action Strategy
#
# STRUCTURE:
#   5M = trend / momentum context
#   1M = price-action entry
#   EXPIRY = 10 MINUTES
#
# RESULT TRACKING:
#   MANUAL IN IQ OPTION
# ============================================================


# ============================================================
# ACCOUNT / TRADE SETTINGS
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

CANDLE_COUNT_5M = 220
CANDLE_COUNT_1M = 220


# ============================================================
# OTC DISCOVERY
# ============================================================

MAX_OTC_ASSETS = 70

DISCOVERY_INTERVAL = 1800
RECONNECT_INTERVAL = 30


# ============================================================
# SCANNING
# ============================================================

SCAN_INTERVAL = 5
STATUS_INTERVAL = 300

ASSET_LOCK_SECONDS = 300
SIGNAL_COOLDOWN_SECONDS = 180


# ============================================================
# ZETA V3 — PRIMARY 3EMA STRUCTURE
# ============================================================

EMA_FAST = 3
EMA_MEDIUM = 21
EMA_SLOW_MEDIUM = 50
EMA_SLOW = 200


# ============================================================
# ZETA V3 — 5M RSI
#
# Based on the Pine strategy:
# RSI period = 20
# Long threshold = 55
# Short threshold = 45
# Lookback = 3 HTF bars
# ============================================================

RSI_PERIOD_5M = 20

RSI_LONG_THRESHOLD = 55
RSI_SHORT_THRESHOLD = 45

RSI_HTF_LOOKBACK = 3


# ============================================================
# ZETA V3 — EMA SLOPE
#
# Based on Pine strategy:
# slope lookback = 3 HTF periods
# long >= +0.05%
# short <= -0.05%
# flat < 0.02%
# ============================================================

SLOPE_LOOKBACK_5M = 3

LONG_SLOPE_MIN = 0.05
SHORT_SLOPE_MAX = -0.05

FLAT_SLOPE_THRESHOLD = 0.02


# ============================================================
# ZETA V3 — STRONG CANDLE
#
# Based on Pine strategy:
# body > average body * 1.5
# body >= 50% of range
# average lookback = 20
# ============================================================

STRONG_CANDLE_LOOKBACK = 20
STRONG_CANDLE_MULTIPLIER = 1.50
STRONG_CANDLE_BODY_RATIO = 0.50


# ============================================================
# ZETA V3 — TREND PRICE ACTION
#
# Based on Strategy #4
# ============================================================

PRICE_ACTION_LOOKBACK = 3


# ============================================================
# ZETA V3 — MACRO TREND FILTER
#
# Strategy #4 uses:
# EMA 200 / 600 / 1000
#
# We calculate these on 5M candles.
# This means they represent approximately:
#   200 x 5m
#   600 x 5m
#   1000 x 5m
#
# They are used as a secondary macro filter,
# not as the primary entry engine.
# ============================================================

MACRO_EMA_FAST = 200
MACRO_EMA_MEDIUM = 600
MACRO_EMA_SLOW = 1000

USE_MACRO_TREND = True


# ============================================================
# ZETA V3 — PRICE ACTION RSI
#
# Strategy #4:
# RSI 14
# oversold 30
# overbought 70
# lookback 3 bars
#
# This is used as a recent pullback/reversal confirmation.
# It is NOT required simultaneously with RSI 5M >=55
# or <=45 because that would create a contradictory filter.
# ============================================================

PRICE_ACTION_RSI_PERIOD = 14

PRICE_ACTION_RSI_OVERBOUGHT = 70
PRICE_ACTION_RSI_OVERSOLD = 30

PRICE_ACTION_RSI_LOOKBACK = 3


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

            value = (
                api.get_server_timestamp()
            )

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
        upper.endswith("-OTC")
        or upper.endswith("_OTC")
        or upper.endswith(" OTC")
        or "-OTC." in upper
        or "_OTC." in upper
        or " OTC." in upper
    )


def clean_active_name(raw_name):

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
# ACTIVE ID REGISTRATION
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
                "[RAW] get_all_init_v2 "
                "received."
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
                "[RAW] get_all_init "
                "received."
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
            "[DISCOVERY] Unexpected "
            "root type:",
            type(data).__name__,
        )

        return []

    found = []
    seen = set()

    def walk(
        node,
        market_type="unknown",
    ):

        if len(found) >= (
            MAX_OTC_ASSETS
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

                        if len(found) >= (
                            MAX_OTC_ASSETS
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
                    (dict, list),
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

                if len(found) >= (
                    MAX_OTC_ASSETS
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
            "[DISCOVERY] result "
            "wrapper detected."
        )

        root = data[
            "result"
        ]

    print(
        "[DISCOVERY] Top-level keys:"
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
            "\n[DISCOVERY] No enabled "
            "OTC instruments found."
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
                    candle.get("from")
                ),
                "open": safe_float(
                    candle.get("open")
                ),
                "close": safe_float(
                    candle.get("close")
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
# CLOSED CANDLES ONLY
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
# SAFE CANDLE FETCH
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
            count + 5,
            server_now(),
        )

        candles = normalize_candles(
            raw
        )

        candles = remove_open_candle(
            candles,
            interval,
        )

        return candles[-count:]

    except Exception as e:

        print(
            f"[CANDLE ERROR] "
            f"{asset} "
            f"{interval}s -> "
            f"{repr(e)}"
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
            for _ in values
        ]

    multiplier = (
        2.0
        / (
            period
            + 1.0
        )
    )

    result = [
        None
        for _ in values
    ]

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
            for _ in candles
        ]

    trs = [
        None
        for _ in candles
    ]

    for i in range(
        1,
        len(candles),
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

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
        for _ in candles
    ]

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

    result[period] = current

    for i in range(
        period + 1,
        len(candles),
    ):

        if trs[i] is None:
            continue

        current = (
            (
                current
                * (period - 1)
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
        for _ in closes
    ]

    if len(closes) <= period:
        return result

    gains = []
    losses = []

    for i in range(
        1,
        len(closes),
    ):

        change = (
            closes[i]
            - closes[i - 1]
        )

        gains.append(
            max(
                change,
                0.0,
            )
        )

        losses.append(
            max(
                -change,
                0.0,
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
            losses[
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
                * (period - 1)
            )
            + gains[j]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[j]
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
# CANDLE HELPERS
# ============================================================

def candle_body(candle):

    return abs(
        candle["close"]
        - candle["open"]
    )


def candle_range(candle):

    return (
        candle["high"]
        - candle["low"]
    )


def is_bullish(candle):

    return (
        candle["close"]
        > candle["open"]
    )


def is_bearish(candle):

    return (
        candle["close"]
        < candle["open"]
    )


def body_ratio(candle):

    rng = candle_range(
        candle
    )

    if rng <= 0:
        return 0.0

    return (
        candle_body(candle)
        / rng
    )


# ============================================================
# STRONG CANDLE
# ============================================================

def is_strong_bullish_candle(
    candles,
    index,
):

    if index < (
        STRONG_CANDLE_LOOKBACK
        + 1
    ):

        return False

    current = candles[
        index
    ]

    if not is_bullish(
        current
    ):

        return False

    current_body = (
        candle_body(current)
    )

    current_ratio = (
        body_ratio(current)
    )

    if (
        current_ratio
        < STRONG_CANDLE_BODY_RATIO
    ):

        return False

    previous_bodies = []

    start = max(
        0,
        index
        - STRONG_CANDLE_LOOKBACK,
    )

    for i in range(
        start,
        index,
    ):

        previous_bodies.append(
            candle_body(
                candles[i]
            )
        )

    if not previous_bodies:
        return False

    average_body = (
        sum(previous_bodies)
        / len(previous_bodies)
    )

    return (
        current_body
        > (
            average_body
            * STRONG_CANDLE_MULTIPLIER
        )
    )


def is_strong_bearish_candle(
    candles,
    index,
):

    if index < (
        STRONG_CANDLE_LOOKBACK
        + 1
    ):

        return False

    current = candles[
        index
    ]

    if not is_bearish(
        current
    ):

        return False

    current_body = (
        candle_body(current)
    )

    current_ratio = (
        body_ratio(current)
    )

    if (
        current_ratio
        < STRONG_CANDLE_BODY_RATIO
    ):

        return False

    previous_bodies = []

    start = max(
        0,
        index
        - STRONG_CANDLE_LOOKBACK,
    )

    for i in range(
        start,
        index,
    ):

        previous_bodies.append(
            candle_body(
                candles[i]
            )
        )

    if not previous_bodies:
        return False

    average_body = (
        sum(previous_bodies)
        / len(previous_bodies)
    )

    return (
        current_body
        > (
            average_body
            * STRONG_CANDLE_MULTIPLIER
        )
    )


# ============================================================
# ENGULFING
#
# Based on Strategy #4, with full engulfing structure.
# ============================================================

def bullish_engulfing(
    previous,
    current,
):

    return (
        is_bearish(previous)
        and is_bullish(current)
        and current["open"]
        <= previous["close"]
        and current["close"]
        >= previous["open"]
    )


def bearish_engulfing(
    previous,
    current,
):

    return (
        is_bullish(previous)
        and is_bearish(current)
        and current["open"]
        >= previous["close"]
        and current["close"]
        <= previous["open"]
    )


# ============================================================
# MORNING STAR
#
# Translation of Strategy #4.
# ============================================================

def morning_star(
    candles,
    index,
):

    if index < 2:
        return False

    first = candles[
        index - 2
    ]

    middle = candles[
        index - 1
    ]

    current = candles[
        index
    ]

    first_body = (
        candle_body(first)
    )

    middle_body = (
        candle_body(middle)
    )

    return (
        first["low"]
        > middle["low"]
        and middle["low"]
        < current["low"]
        and is_bearish(first)
        and is_bullish(middle)
        and current["close"]
        > max(
            current["open"],
            current["close"],
        )
        and middle_body
        < first_body
    )


def evening_star(
    candles,
    index,
):

    if index < 2:
        return False

    first = candles[
        index - 2
    ]

    middle = candles[
        index - 1
    ]

    current = candles[
        index
    ]

    first_body = (
        candle_body(first)
    )

    middle_body = (
        candle_body(middle)
    )

    return (
        first["high"]
        < middle["high"]
        and middle["high"]
        > current["high"]
        and is_bullish(first)
        and is_bearish(middle)
        and current["close"]
        < min(
            current["open"],
            current["close"],
        )
        and middle_body
        < first_body
    )


# ============================================================
# REJECTION CANDLES
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
        and candle["close"]
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
        and candle["close"]
        <= (
            candle["high"]
            - rng * 0.55
        )
    )


# ============================================================
# EMA SLOPE
# ============================================================

def percentage_slope(
    current,
    previous,
):

    if (
        current is None
        or previous is None
        or previous == 0
    ):

        return None

    return (
        (
            current
            - previous
        )
        / abs(previous)
    ) * 100.0


# ============================================================
# 5M RSI MULTI-BAR CONFIRMATION
# ============================================================

def average_recent_values(
    values,
    end_index,
    lookback,
):

    start = (
        end_index
        - lookback
        + 1
    )

    if start < 0:
        return None

    selected = []

    for i in range(
        start,
        end_index + 1,
    ):

        if values[i] is None:
            return None

        selected.append(
            values[i]
        )

    if len(selected) != lookback:
        return None

    return (
        sum(selected)
        / len(selected)
    )


# ============================================================
# PRICE-ACTION RSI PULLBACK
# ============================================================

def recent_oversold(
    rsi,
    index,
):

    start = max(
        0,
        index
        - PRICE_ACTION_RSI_LOOKBACK
        + 1,
    )

    for i in range(
        start,
        index + 1,
    ):

        if (
            rsi[i] is not None
            and rsi[i]
            <= PRICE_ACTION_RSI_OVERSOLD
        ):

            return True

    return False


def recent_overbought(
    rsi,
    index,
):

    start = max(
        0,
        index
        - PRICE_ACTION_RSI_LOOKBACK
        + 1,
    )

    for i in range(
        start,
        index + 1,
    ):

        if (
            rsi[i] is not None
            and rsi[i]
            >= PRICE_ACTION_RSI_OVERBOUGHT
        ):

            return True

    return False


# ============================================================
# ZETA V3 STRATEGY ENGINE
# ============================================================

def evaluate_zeta_v3(
    asset,
    candles_5m,
    candles_1m,
):

    # --------------------------------------------------------
    # BASIC DATA REQUIREMENTS
    # --------------------------------------------------------

    if len(candles_5m) < 210:
        return None

    if len(candles_1m) < 80:
        return None

    # --------------------------------------------------------
    # 5M CLOSES
    # --------------------------------------------------------

    closes_5m = [
        c["close"]
        for c in candles_5m
    ]

    # --------------------------------------------------------
    # PRIMARY EMAs
    # --------------------------------------------------------

    ema3_5m = ema(
        closes_5m,
        EMA_FAST,
    )

    ema21_5m = ema(
        closes_5m,
        EMA_MEDIUM,
    )

    ema50_5m = ema(
        closes_5m,
        EMA_SLOW_MEDIUM,
    )

    ema200_5m = ema(
        closes_5m,
        EMA_SLOW,
    )

    # --------------------------------------------------------
    # 5M RSI
    # --------------------------------------------------------

    rsi5 = rsi_values(
        closes_5m,
        RSI_PERIOD_5M,
    )

    i5 = (
        len(candles_5m)
        - 1
    )

    if i5 < (
        RSI_HTF_LOOKBACK
        + SLOPE_LOOKBACK_5M
        + 5
    ):

        return None

    # --------------------------------------------------------
    # PRIMARY VALUES
    # --------------------------------------------------------

    required_5m = (
        ema3_5m[i5],
        ema21_5m[i5],
        ema50_5m[i5],
        ema200_5m[i5],
        rsi5[i5],
    )

    if any(
        x is None
        for x in required_5m
    ):

        return None

    fast5 = ema3_5m[i5]
    medium5 = ema21_5m[i5]
    slow_medium5 = ema50_5m[i5]
    slow5 = ema200_5m[i5]

    current_rsi5 = rsi5[i5]

    # --------------------------------------------------------
    # 3-BAR RSI AVERAGE
    # --------------------------------------------------------

    rsi_average_3 = (
        average_recent_values(
            rsi5,
            i5,
            RSI_HTF_LOOKBACK,
        )
    )

    if rsi_average_3 is None:
        return None

    rsi_long_ok = (
        current_rsi5
        >= RSI_LONG_THRESHOLD
        and rsi_average_3
        >= RSI_LONG_THRESHOLD
    )

    rsi_short_ok = (
        current_rsi5
        <= RSI_SHORT_THRESHOLD
        and rsi_average_3
        <= RSI_SHORT_THRESHOLD
    )

    # --------------------------------------------------------
    # EMA SLOPE
    # --------------------------------------------------------

    slope_index = (
        i5
        - SLOPE_LOOKBACK_5M
    )

    if slope_index < 0:
        return None

    slope50 = (
        percentage_slope(
            ema50_5m[i5],
            ema50_5m[
                slope_index
            ],
        )
    )

    if slope50 is None:
        return None

    bullish_slope = (
        slope50
        >= LONG_SLOPE_MIN
    )

    bearish_slope = (
        slope50
        <= SHORT_SLOPE_MAX
    )

    ranging = (
        abs(slope50)
        < FLAT_SLOPE_THRESHOLD
    )

    # --------------------------------------------------------
    # PRIMARY EMA ALIGNMENT
    # --------------------------------------------------------

    bullish_alignment = (
        fast5
        > medium5
        > slow_medium5
        > slow5
    )

    bearish_alignment = (
        fast5
        < medium5
        < slow_medium5
        < slow5
    )

    # --------------------------------------------------------
    # PRIMARY TREND REGIME
    #
    # EMA 21 x EMA 200 regime
    # --------------------------------------------------------

    previous_medium5 = (
        ema21_5m[i5 - 1]
    )

    previous_slow5 = (
        ema200_5m[i5 - 1]
    )

    bullish_regime = (
        medium5
        > slow5
    )

    bearish_regime = (
        medium5
        < slow5
    )

    bullish_cross_recent = (
        medium5 > slow5
        and previous_medium5
        <= previous_slow5
    )

    bearish_cross_recent = (
        medium5 < slow5
        and previous_medium5
        >= previous_slow5
    )

    # --------------------------------------------------------
    # MACRO TREND
    #
    # Strategy #4:
    # 200 > 600 > 1000 for long
    # 1000 > 600 > 200 for short
    # --------------------------------------------------------

    macro_bullish = True
    macro_bearish = True

    macro_ema_200 = None
    macro_ema_600 = None
    macro_ema_1000 = None

    if USE_MACRO_TREND:

        macro_ema_200 = ema(
            closes_5m,
            MACRO_EMA_FAST,
        )

        macro_ema_600 = ema(
            closes_5m,
            MACRO_EMA_MEDIUM,
        )

        macro_ema_1000 = ema(
            closes_5m,
            MACRO_EMA_SLOW,
        )

        if (
            macro_ema_1000[i5]
            is None
            or macro_ema_600[i5]
            is None
            or macro_ema_200[i5]
            is None
        ):

            return None

        macro_200 = (
            macro_ema_200[i5]
        )

        macro_600 = (
            macro_ema_600[i5]
        )

        macro_1000 = (
            macro_ema_1000[i5]
        )

        macro_bullish = (
            macro_200
            > macro_600
            > macro_1000
        )

        macro_bearish = (
            macro_1000
            > macro_600
            > macro_200
        )

    # --------------------------------------------------------
    # 1M DATA
    # --------------------------------------------------------

    closes_1m = [
        c["close"]
        for c in candles_1m
    ]

    ema3_1m = ema(
        closes_1m,
        EMA_FAST,
    )

    ema21_1m = ema(
        closes_1m,
        EMA_MEDIUM,
    )

    ema50_1m = ema(
        closes_1m,
        EMA_SLOW_MEDIUM,
    )

    ema200_1m = ema(
        closes_1m,
        EMA_SLOW,
    )

    rsi14_1m = rsi_values(
        closes_1m,
        PRICE_ACTION_RSI_PERIOD,
    )

    atr1 = atr_values(
        candles_1m,
        14,
    )

    i1 = (
        len(candles_1m)
        - 1
    )

    if i1 < 5:
        return None

    required_1m = (
        ema3_1m[i1],
        ema21_1m[i1],
        ema50_1m[i1],
        ema200_1m[i1],
        rsi14_1m[i1],
        atr1[i1],
    )

    if any(
        x is None
        for x in required_1m
    ):

        return None

    current = candles_1m[i1]

    previous = candles_1m[
        i1 - 1
    ]

    two_back = candles_1m[
        i1 - 2
    ]

    price = current[
        "close"
    ]

    current_atr1 = atr1[
        i1
    ]

    current_rsi14 = (
        rsi14_1m[i1]
    )

    if current_atr1 <= 0:
        return None

    # --------------------------------------------------------
    # 1M EMA VALUES
    # --------------------------------------------------------

    fast1 = ema3_1m[i1]
    medium1 = ema21_1m[i1]
    slow_medium1 = ema50_1m[i1]
    slow1 = ema200_1m[i1]

    # --------------------------------------------------------
    # 1M PRICE-ACTION TREND
    # --------------------------------------------------------

    bullish_1m_structure = (
        fast1
        > medium1
        and medium1
        > slow_medium1
    )

    bearish_1m_structure = (
        fast1
        < medium1
        and medium1
        < slow_medium1
    )

    price_above_primary = (
        price
        > medium1
        and price
        > slow_medium1
    )

    price_below_primary = (
        price
        < medium1
        and price
        < slow_medium1
    )

    # --------------------------------------------------------
    # 1M MACRO PRICE POSITION
    # --------------------------------------------------------

    price_above_200 = (
        price > slow1
    )

    price_below_200 = (
        price < slow1
    )

    # --------------------------------------------------------
    # PRICE ACTION PATTERNS
    # --------------------------------------------------------

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
            candles_1m,
            i1,
        )
    )

    bear_evening = (
        evening_star(
            candles_1m,
            i1,
        )
    )

    strong_bull = (
        is_strong_bullish_candle(
            candles_1m,
            i1,
        )
    )

    strong_bear = (
        is_strong_bearish_candle(
            candles_1m,
            i1,
        )
    )

    bull_reject = (
        bullish_rejection(
            current
        )
    )

    bear_reject = (
        bearish_rejection(
            current
        )
    )

    # --------------------------------------------------------
    # FINAL PRICE-ACTION TRIGGERS
    # --------------------------------------------------------

    bullish_price_action = (
        bull_engulf
        or bull_morning
        or strong_bull
        or (
            bull_reject
            and is_bullish(
                current
            )
        )
    )

    bearish_price_action = (
        bear_engulf
        or bear_evening
        or strong_bear
        or (
            bear_reject
            and is_bearish(
                current
            )
        )
    )

    # --------------------------------------------------------
    # PRICE-ACTION RSI PULLBACK
    # --------------------------------------------------------

    recent_oversold_flag = (
        recent_oversold(
            rsi14_1m,
            i1,
        )
    )

    recent_overbought_flag = (
        recent_overbought(
            rsi14_1m,
            i1,
        )
    )

    # --------------------------------------------------------
    # 1M MOMENTUM
    # --------------------------------------------------------

    bullish_momentum = (
        current["close"]
        > previous["close"]
        and previous["close"]
        >= two_back["close"]
    )

    bearish_momentum = (
        current["close"]
        < previous["close"]
        and previous["close"]
        <= two_back["close"]
    )

    # --------------------------------------------------------
    # CANDLE BODY QUALITY
    # --------------------------------------------------------

    current_body_ratio = (
        body_ratio(current)
    )

    # --------------------------------------------------------
    # DISTANCE FROM EMA21
    # --------------------------------------------------------

    distance_from_ema21 = abs(
        price
        - medium1
    )

    distance_ema21_atr = (
        distance_from_ema21
        / current_atr1
    )

    # Avoid chasing candles excessively far from EMA21.
    max_entry_distance_atr = 1.80

    if (
        distance_ema21_atr
        > max_entry_distance_atr
    ):

        return None

    # --------------------------------------------------------
    # FINAL DIRECTION
    # --------------------------------------------------------

    direction = None

    score = 0

    reasons = []

    rejection_reasons = []

    # ========================================================
    # CALL ENGINE
    # ========================================================

    call_core = (
        bullish_regime
        and bullish_slope
        and not ranging
        and rsi_long_ok
        and price_above_primary
        and price_above_200
    )

    if call_core:

        # ----------------------------------------------------
        # Trend
        # ----------------------------------------------------

        score += 20

        reasons.append(
            "5M bullish EMA regime"
        )

        # ----------------------------------------------------
        # EMA alignment
        # ----------------------------------------------------

        if bullish_alignment:

            score += 15

            reasons.append(
                "3/21/50/200 bullish alignment"
            )

        else:

            rejection_reasons.append(
                "5M EMA alignment incomplete"
            )

        # ----------------------------------------------------
        # Slope
        # ----------------------------------------------------

        score += 10

        reasons.append(
            f"EMA50 slope +{slope50:.3f}%"
        )

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        score += 15

        reasons.append(
            f"5M RSI {current_rsi5:.1f}"
        )

        reasons.append(
            f"3-bar RSI avg {rsi_average_3:.1f}"
        )

        # ----------------------------------------------------
        # Macro trend
        # ----------------------------------------------------

        if USE_MACRO_TREND:

            if macro_bullish:

                score += 10

                reasons.append(
                    "macro EMA bullish"
                )

            else:

                rejection_reasons.append(
                    "macro EMA not bullish"
                )

        # ----------------------------------------------------
        # 1M structure
        # ----------------------------------------------------

        if bullish_1m_structure:

            score += 10

            reasons.append(
                "1M EMA structure bullish"
            )

        # ----------------------------------------------------
        # Price action
        # ----------------------------------------------------

        if bullish_price_action:

            score += 15

            if bull_engulf:

                reasons.append(
                    "bullish engulfing"
                )

            if bull_morning:

                reasons.append(
                    "morning star"
                )

            if strong_bull:

                reasons.append(
                    "strong bullish candle"
                )

            if bull_reject:

                reasons.append(
                    "bullish rejection"
                )

        else:

            rejection_reasons.append(
                "no bullish price-action trigger"
            )

        # ----------------------------------------------------
        # Momentum
        # ----------------------------------------------------

        if bullish_momentum:

            score += 5

            reasons.append(
                "1M bullish momentum"
            )

        # ----------------------------------------------------
        # RSI pullback bonus
        # ----------------------------------------------------

        if recent_oversold_flag:

            score += 5

            reasons.append(
                "recent 1M RSI oversold pullback"
            )

        # ----------------------------------------------------
        # Candle quality
        # ----------------------------------------------------

        if (
            current_body_ratio
            >= 0.50
        ):

            score += 5

            reasons.append(
                "strong candle body"
            )

        # ----------------------------------------------------
        # Final CALL
        # ----------------------------------------------------

        if (
            bullish_price_action
            and score >= 80
        ):

            direction = "CALL"

    # ========================================================
    # PUT ENGINE
    # ========================================================

    put_core = (
        bearish_regime
        and bearish_slope
        and not ranging
        and rsi_short_ok
        and price_below_primary
        and price_below_200
    )

    if (
        direction is None
        and put_core
    ):

        # ----------------------------------------------------
        # Trend
        # ----------------------------------------------------

        score += 20

        reasons.append(
            "5M bearish EMA regime"
        )

        # ----------------------------------------------------
        # EMA alignment
        # ----------------------------------------------------

        if bearish_alignment:

            score += 15

            reasons.append(
                "3/21/50/200 bearish alignment"
            )

        else:

            rejection_reasons.append(
                "5M EMA alignment incomplete"
            )

        # ----------------------------------------------------
        # Slope
        # ----------------------------------------------------

        score += 10

        reasons.append(
            f"EMA50 slope {slope50:.3f}%"
        )

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        score += 15

        reasons.append(
            f"5M RSI {current_rsi5:.1f}"
        )

        reasons.append(
            f"3-bar RSI avg {rsi_average_3:.1f}"
        )

        # ----------------------------------------------------
        # Macro trend
        # ----------------------------------------------------

        if USE_MACRO_TREND:

            if macro_bearish:

                score += 10

                reasons.append(
                    "macro EMA bearish"
                )

            else:

                rejection_reasons.append(
                    "macro EMA not bearish"
                )

        # ----------------------------------------------------
        # 1M structure
        # ----------------------------------------------------

        if bearish_1m_structure:

            score += 10

            reasons.append(
                "1M EMA structure bearish"
            )

        # ----------------------------------------------------
        # Price action
        # ----------------------------------------------------

        if bearish_price_action:

            score += 15

            if bear_engulf:

                reasons.append(
                    "bearish engulfing"
                )

            if bear_evening:

                reasons.append(
                    "evening star"
                )

            if strong_bear:

                reasons.append(
                    "strong bearish candle"
                )

            if bear_reject:

                reasons.append(
                    "bearish rejection"
                )

        else:

            rejection_reasons.append(
                "no bearish price-action trigger"
            )

        # ----------------------------------------------------
        # Momentum
        # ----------------------------------------------------

        if bearish_momentum:

            score += 5

            reasons.append(
                "1M bearish momentum"
            )

        # ----------------------------------------------------
        # RSI pullback bonus
        # ----------------------------------------------------

        if recent_overbought_flag:

            score += 5

            reasons.append(
                "recent 1M RSI overbought pullback"
            )

        # ----------------------------------------------------
        # Candle quality
        # ----------------------------------------------------

        if (
            current_body_ratio
            >= 0.50
        ):

            score += 5

            reasons.append(
                "strong candle body"
            )

        # ----------------------------------------------------
        # Final PUT
        # ----------------------------------------------------

        if (
            bearish_price_action
            and score >= 80
        ):

            direction = "PUT"

    # ========================================================
    # NO TRADE
    # ========================================================

    if direction is None:

        return None

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    if score < 80:
        return None

    # --------------------------------------------------------
    # SIGNAL COOLDOWN
    # --------------------------------------------------------

    candle_time = current[
        "from"
    ]

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
            < SIGNAL_COOLDOWN_SECONDS
        ):

            return None

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

            return None

    # --------------------------------------------------------
    # SIGNAL ID
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # RETURN SIGNAL
    # --------------------------------------------------------

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

        "ema3_5m":
            fast5,

        "ema21_5m":
            medium5,

        "ema50_5m":
            slow_medium5,

        "ema200_5m":
            slow5,

        "ema3_1m":
            fast1,

        "ema21_1m":
            medium1,

        "ema50_1m":
            slow_medium1,

        "ema200_1m":
            slow1,

        "rsi_5m":
            current_rsi5,

        "rsi_avg_3":
            rsi_average_3,

        "rsi_14_1m":
            current_rsi14,

        "ema50_slope":
            slope50,

        "atr_1m":
            current_atr1,

        "body_ratio":
            current_body_ratio,

        "distance_ema21_atr":
            distance_ema21_atr,

        "bullish_engulfing":
            bull_engulf,

        "bearish_engulfing":
            bear_engulf,

        "morning_star":
            bull_morning,

        "evening_star":
            bear_evening,

        "strong_bull":
            strong_bull,

        "strong_bear":
            strong_bear,

        "macro_bullish":
            macro_bullish,

        "macro_bearish":
            macro_bearish,

        "reasons":
            reasons,

        "timestamp":
            now_utc(),
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
        "*Context:* 5M\n"
        "*Entry:* 1M\n"
        f"*Expiry:* {EXPIRY_MINUTES} minutes\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*EMA 3 / 21 / 50 / 200:* "
        f"{signal['ema3_5m']:.8f} / "
        f"{signal['ema21_5m']:.8f} / "
        f"{signal['ema50_5m']:.8f} / "
        f"{signal['ema200_5m']:.8f}\n"
        f"*EMA50 slope:* "
        f"{signal['ema50_slope']:.3f}%\n"
        f"*RSI 5M:* "
        f"{signal['rsi_5m']:.2f}\n"
        f"*RSI 3-bar avg:* "
        f"{signal['rsi_avg_3']:.2f}\n"
        f"*RSI 1M:* "
        f"{signal['rsi_14_1m']:.2f}\n"
        f"*1M body ratio:* "
        f"{signal['body_ratio']:.2f}\n"
        f"*EMA21 distance:* "
        f"{signal['distance_ema21_atr']:.2f} ATR\n"
        f"*Price:* "
        f"{signal['price']:.8f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Price Action:*\n"
        f"• Bull engulf: "
        f"{signal['bullish_engulfing']}\n"
        f"• Bear engulf: "
        f"{signal['bearish_engulfing']}\n"
        f"• Morning star: "
        f"{signal['morning_star']}\n"
        f"• Evening star: "
        f"{signal['evening_star']}\n"
        f"• Strong bull: "
        f"{signal['strong_bull']}\n"
        f"• Strong bear: "
        f"{signal['strong_bear']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Confirmations:* {reasons}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Signal ID:* "
        f"{signal['signal_id']}\n"
        f"*Time:* "
        f"{signal['timestamp']}\n"
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
            "*Reason:* No real IQ Option "
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
        asset,
    )

    print(
        "Active ID:",
        active_id,
    )

    print(
        "Direction:",
        direction,
    )

    print(
        "Score:",
        signal["score"],
    )

    print(
        "Stake:",
        STAKE,
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
    )

    print(
        "Account:",
        BALANCE_MODE,
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
            f"*Trade number:* "
            f"{total_trades}/{TARGET_TRADES}\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Stake:* ${STAKE:.2f}\n"
            f"*Expiry:* "
            f"{EXPIRY_MINUTES} minutes\n"
            f"*Signal ID:* "
            f"{signal['signal_id']}\n"
            "*Trade ID:* NOT RETURNED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📋 *Track result manually "
            "in IQ Option.*"
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
        f"*Expiry:* "
        f"*{EXPIRY_MINUTES} minutes*\n"
        f"*Trade ID:* {trade_id}\n"
        f"*Signal ID:* "
        f"{signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 *PRACTICE / DEMO ONLY*\n"
        "📋 *Track the result manually "
        "in IQ Option.*"
    )

    return True


# ============================================================
# TEST REAL OTC CANDLE FEEDS
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
                10,
            )
        )

        if len(candles) >= 5:

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
        "[OTC] Assets were discovered, "
        "but candle feeds did not respond."
    )

    return False


# ============================================================
# CONNECTION CHECK
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
        f"*OTC feeds:* "
        f"{len(otc_assets)}\n"
        f"*Demo orders opened:* "
        f"{total_trades}/{TARGET_TRADES}\n"
        f"*Runtime:* "
        f"{runtime_string()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Auto-trading:* ON\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* "
        f"{EXPIRY_MINUTES} minutes\n"
        "*Strategy:* "
        "3EMA + RSI + Price Action\n"
        "*Context:* 5M\n"
        "*Entry:* 1M\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if balance is not None:

        message += (
            f"*Demo balance:* "
            f"${safe_float(balance):.2f}\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 *Results are tracked manually "
        "in IQ Option.*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *ZETA V3 SCANNING "
        "REAL OTC MARKETS*"
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
# TARGET CHECK
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

        print(message)

        send_telegram(
            message
        )

        return

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

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
        "*Strategy:* "
        "3EMA + RSI + Trend Price Action\n"
        "*Trend:* 5M\n"
        "*Entry:* 1M\n"
        f"*Expiry:* "
        f"{EXPIRY_MINUTES} minutes\n"
        "*EMA:* 3 / 21 / 50 / 200\n"
        "*RSI:* 5M RSI-20\n"
        "*Price Action:* "
        "Engulfing + Morning/Evening Star + Strong Candle\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Target:* "
        f"{TARGET_TRADES} demo orders\n"
        "*Result tracking:* MANUAL\n"
        "*Auto-trading:* ON\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Using real IQ Option "
        "OTC initialization..."
    )

    # --------------------------------------------------------
    # INITIAL OTC DISCOVERY
    # --------------------------------------------------------

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
                "*Trend:* 5M\n"
                "*Entry:* 1M\n"
                f"*Expiry:* "
                f"{EXPIRY_MINUTES} minutes\n"
                "*Strategy:* "
                "ZETA V3\n"
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

    # --------------------------------------------------------
    # CONTINUOUS LOOP
    # --------------------------------------------------------

    while True:

        try:

            if target_reached():

                send_telegram(
                    "🏁 *ZETA V3 "
                    "50-TRADE TEST REACHED*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"*Demo orders opened:* "
                    f"{total_trades}\n"
                    "*Results:* "
                    "Tracked manually in IQ Option\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "The 50-order Practice "
                    "test is complete."
                )

                break

            current_time = (
                time.time()
            )

            # ------------------------------------------------
            # CONNECTION
            # ------------------------------------------------

            if not connection_is_alive():

                print(
                    "[CONNECTION] Lost."
                )

                send_telegram(
                    "🔴 *ZETA V3 "
                    "CONNECTION LOST*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Attempting reconnect..."
                )

                try:

                    api.connect()

                    api.change_balance(
                        BALANCE_MODE
                    )

                    print(
                        "[CONNECTION] "
                        "Reconnected."
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

            # ------------------------------------------------
            # OTC REFRESH
            # ------------------------------------------------

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

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            if (
                current_time
                - last_scan_cycle
                >= SCAN_INTERVAL
            ):

                last_scan_cycle = (
                    current_time
                )

                if not otc_assets:

                    print(
                        "[SCAN] No working "
                        "OTC feeds."
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
                        "Demo orders opened:",
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

                            # --------------------------------
                            # 5M
                            # --------------------------------

                            candles_5m = (
                                get_candles_safe(
                                    asset,
                                    TF5,
                                    CANDLE_COUNT_5M,
                                )
                            )

                            if len(
                                candles_5m
                            ) < 210:

                                continue

                            # --------------------------------
                            # 1M
                            # --------------------------------

                            candles_1m = (
                                get_candles_safe(
                                    asset,
                                    TF1,
                                    CANDLE_COUNT_1M,
                                )
                            )

                            if len(
                                candles_1m
                            ) < 80:

                                continue

                            # --------------------------------
                            # STRATEGY
                            # --------------------------------

                            signal = (
                                evaluate_zeta_v3(
                                    asset,
                                    candles_5m,
                                    candles_1m,
                                )
                            )

                            if signal is None:
                                continue

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

                            print(
                                "REASONS:",
                                ", ".join(
                                    signal[
                                        "reasons"
                                    ]
                                ),
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

                    print(
                        "[ZETA V3 "
                        "SCAN COMPLETE]",
                        now_utc(),
                    )

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

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
        "ZETA V3 — "
        "3EMA + RSI + PRICE ACTION"
    )

    print(
        "=" * 70
    )

    print(
        "Started:",
        now_utc(),
    )

    print(
        "Strategy:",
        "3EMA + RSI + Trend Price Action",
    )

    print(
        "Trend Context:",
        "5M",
    )

    print(
        "Entry:",
        "1M",
    )

    print(
        "EMA:",
        "3 / 21 / 50 / 200",
    )

    print(
        "RSI:",
        "5M RSI-20",
    )

    print(
        "Price Action:",
        "Engulfing / Star / Strong Candle",
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
# IMPORTANT — DO NOT CHANGE THIS
# ============================================================

if __name__ == "__main__":
    main()
