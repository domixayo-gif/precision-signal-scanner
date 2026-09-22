import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests

from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# PRECISION IQ OPTION OTC SCANNER V4
# ============================================================
#
# READ-ONLY
# NO automatic trading
#
# IMPORTANT:
# This version does NOT depend on:
#
#     api.get_all_open_time()
#
# for OTC discovery.
#
# It directly requests IQ Option initialization data and
# extracts real OTC instruments + active IDs.
#
# Then it:
#
# 1. Updates the local active-ID map
# 2. Tests real candle access
# 3. Runs the precision strategy
# 4. Sends qualified signals to Telegram
#
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
# DATA SETTINGS
# ============================================================

CANDLE_COUNT_5M = 160
CANDLE_COUNT_1M = 160

MAX_OTC_ASSETS = 60

EXPIRY_MINUTES = 5

LOCK_SECONDS = 300

BALANCE_MODE = "PRACTICE"


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def safe_float(value, default=0.0):

    try:

        value = float(value)

        if math.isfinite(value):
            return value

    except Exception:
        pass

    return default


def send_telegram(text):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        print("\n[TELEGRAM DISABLED]")
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

    if not isinstance(name, str):
        return False

    upper = name.upper().strip()

    return (
        upper.endswith("-OTC")
        or "-OTC." in upper
        or "_OTC" in upper
        or " OTC" in upper
    )


def clean_active_name(raw_name):

    if raw_name is None:
        return None

    name = str(raw_name).strip()

    # IQ Option initialization names are often like:
    #
    # binary.EURUSD-OTC
    #
    # turbo.EURUSD-OTC
    #
    # We want:
    #
    # EURUSD-OTC

    if "." in name:

        parts = name.split(".")

        name = parts[-1]

    return name.strip()


# ============================================================
# RAW INITIALIZATION DATA
# ============================================================

def get_raw_initialization(api):

    print("\n" + "=" * 70)
    print("RAW IQ OPTION INITIALIZATION")
    print("=" * 70)

    # Preferred current method
    try:

        print(
            "[RAW] Requesting "
            "get-initialization-data..."
        )

        data = api.get_all_init_v2()

        if data:

            print(
                "[RAW] Initialization received."
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

    # Fallback
    try:

        print(
            "[RAW] Trying legacy "
            "api_option_init_all..."
        )

        data = api.get_all_init()

        if data:

            print(
                "[RAW] Legacy initialization received."
            )

            return data

    except Exception as e:

        print(
            "[RAW LEGACY ERROR]",
            repr(e),
        )

    print(
        "[RAW] IQ Option returned no initialization data."
    )

    return None


# ============================================================
# ACTIVE ID REGISTRATION
# ============================================================

def register_active_id(name, active_id):

    if not name:
        return False

    try:

        active_id = int(active_id)

    except Exception:

        return False

    try:

        OP_code.ACTIVES[name] = active_id

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
# RAW OTC DISCOVERY
# ============================================================

def discover_otc_from_initialization(data):

    print("\n" + "=" * 70)
    print("DIRECT OTC DISCOVERY")
    print("=" * 70)

    if not isinstance(data, dict):

        print(
            "[DISCOVERY] Unexpected initialization type:",
            type(data).__name__,
        )

        return []


    # --------------------------------------------------------
    # Handle both possible structures:
    #
    # V2:
    #
    # {
    #     "binary": {...},
    #     "turbo": {...}
    # }
    #
    # Legacy:
    #
    # {
    #     "result": {
    #         "binary": {...},
    #         "turbo": {...}
    #     }
    # }
    # --------------------------------------------------------

    root = data

    if isinstance(data.get("result"), dict):

        print(
            "[DISCOVERY] Legacy 'result' wrapper detected."
        )

        root = data["result"]


    print(
        "[DISCOVERY] Top-level keys:"
    )

    for key in root.keys():

        print(
            "  -",
            key
        )


    found = []

    seen = set()


    # --------------------------------------------------------
    # Binary + Turbo
    # --------------------------------------------------------

    for market_type in (
        "binary",
        "turbo",
    ):

        section = root.get(market_type)

        if not isinstance(section, dict):

            print(
                f"\n[{market_type.upper()}] "
                "section not found."
            )

            continue


        actives = section.get("actives")

        if not isinstance(actives, dict):

            print(
                f"\n[{market_type.upper()}] "
                "No actives dictionary."
            )

            continue


        print(
            f"\n[{market_type.upper()}] "
            f"Total raw actives: {len(actives)}"
        )


        otc_count = 0

        open_count = 0


        for active_id, active in actives.items():

            if not isinstance(active, dict):
                continue


            raw_name = active.get("name")

            name = clean_active_name(
                raw_name
            )


            if not is_otc_name(name):
                continue


            otc_count += 1


            enabled = (
                active.get("enabled")
                is True
            )

            suspended = (
                active.get("is_suspended")
                is True
            )


            is_open = (
                enabled
                and not suspended
            )


            print(
                f"  OTC: {name} "
                f"| ID={active_id} "
                f"| enabled={enabled} "
                f"| suspended={suspended} "
                f"| open={is_open}"
            )


            # We only want instruments that the API
            # itself reports as enabled and not suspended.

            if not is_open:
                continue


            open_count += 1


            # Register the REAL IQ Option active ID.
            register_active_id(
                name,
                active_id,
            )


            unique_key = (
                market_type,
                name,
            )


            if unique_key in seen:
                continue


            seen.add(unique_key)


            found.append(
                {
                    "asset": name,
                    "market_type": market_type,
                    "active_id": int(active_id),
                    "enabled": enabled,
                    "suspended": suspended,
                }
            )


        print(
            f"[{market_type.upper()}] "
            f"OTC names={otc_count} "
            f"| OPEN={open_count}"
        )


    # --------------------------------------------------------
    # Digital underlying
    # --------------------------------------------------------

    digital_section = root.get(
        "digital"
    )

    if isinstance(digital_section, dict):

        print(
            "\n[DIGITAL] Initialization section found."
        )

    else:

        print(
            "\n[DIGITAL] No direct initialization "
            "section available."
        )


    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    found.sort(
        key=lambda x: (
            x["asset"],
            x["market_type"],
        )
    )


    print("\n" + "-" * 70)

    print(
        f"TOTAL OPEN OTC INSTRUMENTS: {len(found)}"
    )


    if found:

        print(
            "\n[REAL OTC ASSETS]"
        )

        for index, item in enumerate(
            found,
            start=1,
        ):

            print(
                f"{index:02d}. "
                f"{item['asset']:<20} "
                f"{item['market_type']:<8} "
                f"ID={item['active_id']}"
            )

    else:

        print(
            "\n[NO OPEN OTC ASSETS]"
        )


    return found[:MAX_OTC_ASSETS]


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
                        candle.get("low"),
                    )
                ),

                "high": safe_float(
                    candle.get(
                        "max",
                        candle.get("high"),
                    )
                ),

                "volume": safe_float(
                    candle.get("volume")
                ),
            }


            if (
                item["open"] > 0
                and item["close"] > 0
                and item["low"] > 0
                and item["high"] > 0
            ):

                result.append(item)


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

    if len(candles) < 3:
        return candles


    current_time = time.time()

    completed = []


    for candle in candles:

        candle_start = candle["from"]


        if (
            candle_start
            + timeframe_seconds
            <= current_time
        ):

            completed.append(candle)


    return completed


# ============================================================
# CANDLE FETCH
# ============================================================

def get_candles_safe(
    api,
    asset,
    interval,
    count,
):

    try:

        end_time = time.time()


        raw = api.get_candles(
            asset,
            interval,
            count,
            end_time,
        )


        candles = normalize_candles(raw)


        candles = remove_open_candle(
            candles,
            interval,
        )


        return candles


    except Exception as e:

        print(
            f"[CANDLE ERROR] "
            f"{asset} "
            f"{interval}s -> "
            f"{repr(e)}"
        )

        return []


# ============================================================
# CANDLE ACCESS TEST
# ============================================================

def test_candle_access(
    api,
    otc_assets,
):

    print("\n" + "=" * 70)
    print("REAL OTC CANDLE ACCESS TEST")
    print("=" * 70)


    working = []


    for item in otc_assets:

        asset = item["asset"]

        active_id = item["active_id"]

        market_type = item["market_type"]


        print(
            f"\n[TEST] {asset}"
        )

        print(
            f"       market={market_type}"
        )

        print(
            f"       active_id={active_id}"
        )


        # Verify mapping
        mapped_id = OP_code.ACTIVES.get(
            asset
        )


        print(
            f"       mapped_id={mapped_id}"
        )


        if mapped_id is None:

            print(
                "       RESULT: NO ACTIVE ID MAPPING"
            )

            continue


        candles = get_candles_safe(
            api,
            asset,
            60,
            10,
        )


        if len(candles) >= 5:

            latest = candles[-1]


            print(
                f"       RESULT: OK"
            )

            print(
                f"       candles={len(candles)}"
            )

            print(
                f"       last_close="
                f"{latest['close']}"
            )


            working.append(item)


        else:

            print(
                f"       RESULT: FAILED"
            )

            print(
                f"       candles={len(candles)}"
            )


    print("\n" + "-" * 70)

    print(
        f"WORKING OTC CANDLE FEEDS: "
        f"{len(working)}"
    )


    if working:

        print(
            "\n[WORKING OTC ASSETS]"
        )

        for item in working:

            print(
                f"  {item['asset']} "
                f"| {item['market_type']} "
                f"| ID={item['active_id']}"
            )


    return working


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return []


    multiplier = (
        2.0 / (period + 1.0)
    )


    result = [None] * len(values)


    seed = (
        sum(values[:period])
        / period
    )


    result[period - 1] = seed


    previous = seed


    for i in range(
        period,
        len(values),
    ):

        previous = (
            (values[i] - previous)
            * multiplier
            + previous
        )


        result[i] = previous


    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):

    if len(values) < period + 1:
        return []


    gains = []
    losses = []


    for i in range(
        1,
        len(values),
    ):

        change = (
            values[i]
            - values[i - 1]
        )


        if change > 0:

            gains.append(change)
            losses.append(0.0)

        else:

            gains.append(0.0)
            losses.append(
                abs(change)
            )


    avg_gain = (
        sum(gains[:period])
        / period
    )


    avg_loss = (
        sum(losses[:period])
        / period
    )


    result = [None] * len(values)


    def calculate(
        gain,
        loss,
    ):

        if loss == 0:
            return 100.0

        rs = gain / loss

        return (
            100.0
            - (
                100.0
                / (1.0 + rs)
            )
        )


    result[period] = calculate(
        avg_gain,
        avg_loss,
    )


    for i in range(
        period,
        len(gains),
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period


        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period


        result[i + 1] = calculate(
            avg_gain,
            avg_loss,
        )


    return result


# ============================================================
# MACD
# ============================================================

def macd(
    values,
    fast_period=12,
    slow_period=26,
    signal_period=9,
):

    fast_line = ema(
        values,
        fast_period,
    )

    slow_line = ema(
        values,
        slow_period,
    )


    macd_line = [None] * len(values)

    valid_values = []


    for i in range(
        len(values)
    ):

        if (
            fast_line[i] is not None
            and slow_line[i] is not None
        ):

            value = (
                fast_line[i]
                - slow_line[i]
            )

            macd_line[i] = value

            valid_values.append(value)


    signal_values = ema(
        valid_values,
        signal_period,
    )


    signal_line = [None] * len(values)


    valid_index = 0


    for i in range(
        len(values)
    ):

        if macd_line[i] is not None:

            if (
                valid_index
                < len(signal_values)
            ):

                signal_line[i] = (
                    signal_values[
                        valid_index
                    ]
                )

            valid_index += 1


    histogram = [None] * len(values)


    for i in range(
        len(values)
    ):

        if (
            macd_line[i] is not None
            and signal_line[i] is not None
        ):

            histogram[i] = (
                macd_line[i]
                - signal_line[i]
            )


    return (
        macd_line,
        signal_line,
        histogram,
    )


# ============================================================
# ATR
# ============================================================

def atr(
    candles,
    period=14,
):

    if len(candles) < period + 1:
        return []


    true_ranges = []


    for i in range(
        1,
        len(candles),
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_close = (
            candles[i - 1]["close"]
        )


        tr = max(
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


        true_ranges.append(tr)


    result = [None] * len(candles)


    current = (
        sum(true_ranges[:period])
        / period
    )


    result[period] = current


    for i in range(
        period,
        len(true_ranges),
    ):

        current = (
            (
                current
                * (period - 1)
            )
            + true_ranges[i]
        ) / period


        result[i + 1] = current


    return result


# ============================================================
# ADX / DMI
# ============================================================

def adx_dmi(
    candles,
    period=14,
):

    if len(candles) < (
        period * 2 + 5
    ):

        return [], [], []


    tr_values = []
    plus_dm = []
    minus_dm = []


    for i in range(
        1,
        len(candles),
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_high = (
            candles[i - 1]["high"]
        )

        previous_low = (
            candles[i - 1]["low"]
        )

        previous_close = (
            candles[i - 1]["close"]
        )


        up_move = (
            high - previous_high
        )

        down_move = (
            previous_low - low
        )


        plus = (
            up_move
            if (
                up_move > down_move
                and up_move > 0
            )
            else 0.0
        )


        minus = (
            down_move
            if (
                down_move > up_move
                and down_move > 0
            )
            else 0.0
        )


        tr = max(
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


        tr_values.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)


    if len(tr_values) < period:

        return [], [], []


    atr_smoothed = sum(
        tr_values[:period]
    )

    plus_smoothed = sum(
        plus_dm[:period]
    )

    minus_smoothed = sum(
        minus_dm[:period]
    )


    dx_values = []

    plus_di_values = []

    minus_di_values = []


    for i in range(
        period,
        len(tr_values),
    ):

        if i > period:

            atr_smoothed = (
                atr_smoothed
                - (
                    atr_smoothed
                    / period
                )
                + tr_values[i]
            )


            plus_smoothed = (
                plus_smoothed
                - (
                    plus_smoothed
                    / period
                )
                + plus_dm[i]
            )


            minus_smoothed = (
                minus_smoothed
                - (
                    minus_smoothed
                    / period
                )
                + minus_dm[i]
            )


        if atr_smoothed == 0:

            plus_di = 0.0
            minus_di = 0.0

        else:

            plus_di = (
                100.0
                * plus_smoothed
                / atr_smoothed
            )

            minus_di = (
                100.0
                * minus_smoothed
                / atr_smoothed
            )


        denominator = (
            plus_di
            + minus_di
        )


        if denominator == 0:

            dx = 0.0

        else:

            dx = (
                100.0
                * abs(
                    plus_di
                    - minus_di
                )
                / denominator
            )


        plus_di_values.append(
            plus_di
        )

        minus_di_values.append(
            minus_di
        )

        dx_values.append(dx)


    if len(dx_values) < period:

        return [], [], []


    adx_values = [
        None
    ] * len(candles)

    plus_result = [
        None
    ] * len(candles)

    minus_result = [
        None
    ] * len(candles)


    first_adx = (
        sum(dx_values[:period])
        / period
    )


    base_index = (
        period
        + period
        - 1
    )


    if base_index < len(candles):

        adx_values[
            base_index
        ] = first_adx

        plus_result[
            base_index
        ] = plus_di_values[
            period - 1
        ]

        minus_result[
            base_index
        ] = minus_di_values[
            period - 1
        ]


    current_adx = first_adx


    for i in range(
        period,
        len(dx_values),
    ):

        current_adx = (
            (
                current_adx
                * (period - 1)
            )
            + dx_values[i]
        ) / period


        candle_index = (
            period + i
        )


        if candle_index < len(candles):

            adx_values[
                candle_index
            ] = current_adx

            plus_result[
                candle_index
            ] = plus_di_values[i]

            minus_result[
                candle_index
            ] = minus_di_values[i]


    return (
        adx_values,
        plus_result,
        minus_result,
    )


# ============================================================
# CANDLE STRENGTH
# ============================================================

def candle_strength(candle):

    high = candle["high"]
    low = candle["low"]

    open_price = candle["open"]
    close = candle["close"]


    candle_range = (
        high - low
    )


    if candle_range <= 0:
        return 0.0


    body = abs(
        close - open_price
    )


    return (
        body / candle_range
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(
    candles,
    lookback=20,
):

    if len(candles) < lookback:

        return "NEUTRAL"


    recent = candles[
        -lookback:
    ]


    highs = [
        c["high"]
        for c in recent
    ]


    lows = [
        c["low"]
        for c in recent
    ]


    half = (
        len(recent) // 2
    )


    first_high = max(
        highs[:half]
    )

    second_high = max(
        highs[half:]
    )


    first_low = min(
        lows[:half]
    )

    second_low = min(
        lows[half:]
    )


    if (
        second_high > first_high
        and second_low > first_low
    ):

        return "BULLISH"


    if (
        second_high < first_high
        and second_low < first_low
    ):

        return "BEARISH"


    return "NEUTRAL"


# ============================================================
# LAST VALID VALUE
# ============================================================

def last_valid(values):

    for value in reversed(values):

        if value is not None:

            return value


    return None


# ============================================================
# ANALYZE ONE OTC ASSET
# ============================================================

def analyze_asset(
    api,
    asset,
):

    candles_5m = get_candles_safe(
        api,
        asset,
        300,
        CANDLE_COUNT_5M,
    )


    candles_1m = get_candles_safe(
        api,
        asset,
        60,
        CANDLE_COUNT_1M,
    )


    if len(candles_5m) < 80:

        return None, (
            f"{asset}: insufficient 5M candles "
            f"({len(candles_5m)})"
        )


    if len(candles_1m) < 80:

        return None, (
            f"{asset}: insufficient 1M candles "
            f"({len(candles_1m)})"
        )


    close_5m = [
        c["close"]
        for c in candles_5m
    ]


    close_1m = [
        c["close"]
        for c in candles_1m
    ]


    # --------------------------------------------------------
    # 5M indicators
    # --------------------------------------------------------

    ema20_5 = ema(
        close_5m,
        20,
    )

    ema50_5 = ema(
        close_5m,
        50,
    )

    rsi_5 = rsi(
        close_5m,
        14,
    )

    macd_5, signal_5, hist_5 = macd(
        close_5m
    )

    adx_5, plus_di_5, minus_di_5 = adx_dmi(
        candles_5m,
        14,
    )

    atr_5 = atr(
        candles_5m,
        14,
    )


    # --------------------------------------------------------
    # 1M indicators
    # --------------------------------------------------------

    ema9_1 = ema(
        close_1m,
        9,
    )

    ema21_1 = ema(
        close_1m,
        21,
    )

    rsi_1 = rsi(
        close_1m,
        14,
    )

    macd_1, signal_1, hist_1 = macd(
        close_1m
    )


    # --------------------------------------------------------
    # Latest values
    # --------------------------------------------------------

    e20 = last_valid(
        ema20_5
    )

    e50 = last_valid(
        ema50_5
    )

    e9_1 = last_valid(
        ema9_1
    )

    e21_1 = last_valid(
        ema21_1
    )

    r5 = last_valid(
        rsi_5
    )

    r1 = last_valid(
        rsi_1
    )

    m5 = last_valid(
        macd_5
    )

    s5 = last_valid(
        signal_5
    )

    h5 = last_valid(
        hist_5
    )

    m1 = last_valid(
        macd_1
    )

    s1 = last_valid(
        signal_1
    )

    h1 = last_valid(
        hist_1
    )

    adx = last_valid(
        adx_5
    )

    plus_di = last_valid(
        plus_di_5
    )

    minus_di = last_valid(
        minus_di_5
    )

    current_atr = last_valid(
        atr_5
    )


    required = (
        e20,
        e50,
        e9_1,
        e21_1,
        r5,
        r1,
        m5,
        s5,
        h5,
        m1,
        s1,
        h1,
        adx,
        plus_di,
        minus_di,
        current_atr,
    )


    if any(
        x is None
        for x in required
    ):

        return None, (
            f"{asset}: indicator calculation incomplete"
        )


    current_price = (
        candles_1m[-1]["close"]
    )


    latest_5m = (
        candles_5m[-1]
    )

    latest_1m = (
        candles_1m[-1]
    )


    strength_5 = candle_strength(
        latest_5m
    )

    strength_1 = candle_strength(
        latest_1m
    )


    average_strength = (
        strength_5
        + strength_1
    ) / 2.0


    # --------------------------------------------------------
    # Trend
    # --------------------------------------------------------

    trend_5 = (
        "BULLISH"
        if e20 > e50
        else
        "BEARISH"
        if e20 < e50
        else
        "NEUTRAL"
    )


    entry_1 = (
        "BULLISH"
        if e9_1 > e21_1
        else
        "BEARISH"
        if e9_1 < e21_1
        else
        "NEUTRAL"
    )


    structure = market_structure(
        candles_5m,
        20,
    )


    # --------------------------------------------------------
    # Extension
    # --------------------------------------------------------

    extension_atr = 0.0


    if current_atr > 0:

        extension_atr = (
            abs(
                current_price
                - e20
            )
            / current_atr
        )


    # --------------------------------------------------------
    # Room
    # --------------------------------------------------------

    lookback = (
        candles_5m[-20:]
    )


    resistance = max(
        c["high"]
        for c in lookback
    )


    support = min(
        c["low"]
        for c in lookback
    )


    if current_atr > 0:

        room_up = (
            resistance
            - current_price
        ) / current_atr


        room_down = (
            current_price
            - support
        ) / current_atr

    else:

        room_up = 0.0
        room_down = 0.0


    # --------------------------------------------------------
    # HARD FILTERS
    # --------------------------------------------------------

    if adx < MIN_ADX:

        return None, (
            f"{asset}: NO TRADE | "
            f"ADX {adx:.1f} < {MIN_ADX:.1f}"
        )


    if extension_atr >= MAX_EXTENSION_ATR:

        return None, (
            f"{asset}: NO TRADE | "
            f"extension "
            f"{extension_atr:.2f} ATR"
        )


    if average_strength < MIN_CANDLE_STRENGTH:

        return None, (
            f"{asset}: NO TRADE | "
            f"candle strength "
            f"{average_strength:.2f}"
        )


    # --------------------------------------------------------
    # SCORING
    # --------------------------------------------------------

    call_score = 0
    put_score = 0

    call_confirmations = 0
    put_confirmations = 0


    # 5M trend — 20
    if trend_5 == "BULLISH":

        call_score += 20
        call_confirmations += 1

    elif trend_5 == "BEARISH":

        put_score += 20
        put_confirmations += 1


    # 1M entry — 15
    if entry_1 == "BULLISH":

        call_score += 15
        call_confirmations += 1

    elif entry_1 == "BEARISH":

        put_score += 15
        put_confirmations += 1


    # Structure — 15
    if structure == "BULLISH":

        call_score += 15
        call_confirmations += 1

    elif structure == "BEARISH":

        put_score += 15
        put_confirmations += 1


    # DMI — 10
    if plus_di > minus_di:

        call_score += 10
        call_confirmations += 1

    elif minus_di > plus_di:

        put_score += 10
        put_confirmations += 1


    # MACD — 10
    if (
        m5 > s5
        and h5 > 0
        and m1 > s1
        and h1 > 0
    ):

        call_score += 10
        call_confirmations += 1


    elif (
        m5 < s5
        and h5 < 0
        and m1 < s1
        and h1 < 0
    ):

        put_score += 10
        put_confirmations += 1


    # RSI — 10
    if (
        45 <= r5 <= 67
        and 45 <= r1 <= 67
    ):

        call_score += 10
        call_confirmations += 1


    elif (
        33 <= r5 <= 55
        and 33 <= r1 <= 55
    ):

        put_score += 10
        put_confirmations += 1


    # Candle — 10
    if (
        latest_5m["close"]
        > latest_5m["open"]
        and
        latest_1m["close"]
        > latest_1m["open"]
    ):

        call_score += 10
        call_confirmations += 1


    elif (
        latest_5m["close"]
        < latest_5m["open"]
        and
        latest_1m["close"]
        < latest_1m["open"]
    ):

        put_score += 10
        put_confirmations += 1


    # ADX quality — 5
    if adx >= 25:

        if call_score > put_score:

            call_score += 5

        elif put_score > call_score:

            put_score += 5


    # Extension penalty
    if extension_atr >= 2.0:

        if call_score > put_score:

            call_score -= 8

        elif put_score > call_score:

            put_score -= 8


    call_score = max(
        0,
        call_score
    )

    put_score = max(
        0,
        put_score
    )


    # --------------------------------------------------------
    # Direction
    # --------------------------------------------------------

    if call_score > put_score:

        direction = "CALL"

        score = call_score

        confirmations = (
            call_confirmations
        )

        room = room_up

        opposite_score = put_score


    elif put_score > call_score:

        direction = "PUT"

        score = put_score

        confirmations = (
            put_confirmations
        )

        room = room_down

        opposite_score = call_score


    else:

        return None, (
            f"{asset}: NO TRADE | "
            f"CALL {call_score} / "
            f"PUT {put_score}"
        )


    # --------------------------------------------------------
    # FINAL FILTERS
    # --------------------------------------------------------

    if score < MIN_SCORE:

        return None, (
            f"{asset}: NO TRADE | "
            f"{direction} score {score} "
            f"< {MIN_SCORE}"
        )


    if confirmations < MIN_CONFIRMATIONS:

        return None, (
            f"{asset}: NO TRADE | "
            f"confirmations "
            f"{confirmations} "
            f"< {MIN_CONFIRMATIONS}"
        )


    if (
        score - opposite_score
        < MIN_SCORE_GAP
    ):

        return None, (
            f"{asset}: NO TRADE | "
            f"score gap "
            f"{score - opposite_score} "
            f"< {MIN_SCORE_GAP}"
        )


    if room < MIN_ROOM_ATR:

        return None, (
            f"{asset}: NO TRADE | "
            f"room {room:.2f} ATR "
            f"< {MIN_ROOM_ATR:.2f}"
        )


    # --------------------------------------------------------
    # QUALIFIED SIGNAL
    # --------------------------------------------------------

    signal_id = (
        f"{asset.replace('-', '')}-"
        f"{direction}-"
        f"{datetime.now(timezone.utc).strftime('%H%M%S')}"
    )


    signal = {

        "signal_id": signal_id,

        "asset": asset,

        "direction": direction,

        "score": score,

        "price": current_price,

        "trend_5m": trend_5,

        "entry_1m": entry_1,

        "structure": structure,

        "adx": adx,

        "rsi_5m": r5,

        "rsi_1m": r1,

        "plus_di": plus_di,

        "minus_di": minus_di,

        "extension_atr": extension_atr,

        "room_up": room_up,

        "room_down": room_down,

        "confirmations": confirmations,

        "timestamp": now_utc(),
    }


    return signal, None


# ============================================================
# TELEGRAM SIGNAL FORMAT
# ============================================================

def format_signal(signal):

    emoji = (
        "🟢"
        if signal["direction"] == "CALL"
        else
        "🔴"
    )


    return (
        f"{emoji} *NEW QUALIFIED OTC SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* `{signal['asset']}`\n"
        f"*Direction:* *"
        f"{signal['direction']}"
        f" / "
        f"{'UP' if signal['direction'] == 'CALL' else 'DOWN'}"
        f"*\n"
        f"*Score:* *{signal['score']}/100*\n"
        f"*Reference expiry:* "
        f"*{EXPIRY_MINUTES} minutes*\n"
        f"*Price:* `{signal['price']:.8f}`\n"
        f"*5M Trend:* `{signal['trend_5m']}`\n"
        f"*1M Entry:* `{signal['entry_1m']}`\n"
        f"*Structure:* `{signal['structure']}`\n"
        f"*ADX:* `{signal['adx']:.1f}`\n"
        f"*RSI 5M:* `{signal['rsi_5m']:.1f}`\n"
        f"*RSI 1M:* `{signal['rsi_1m']:.1f}`\n"
        f"*DI+:* `{signal['plus_di']:.1f}`\n"
        f"*DI-:* `{signal['minus_di']:.1f}`\n"
        f"*Extension:* "
        f"`{signal['extension_atr']:.2f} ATR`\n"
        f"*Room Up:* "
        f"`{signal['room_up']:.2f} ATR`\n"
        f"*Room Down:* "
        f"`{signal['room_down']:.2f} ATR`\n"
        f"*Confirmations:* "
        f"`{signal['confirmations']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Signal ID:* "
        f"`{signal['signal_id']}`\n"
        f"*Time:* "
        f"`{signal['timestamp']}`\n"
        f"⚠️ *READ-ONLY — NO AUTO TRADE*"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("PRECISION IQ OPTION OTC SCANNER V4")
    print("=" * 70)

    print(
        "Started:",
        now_utc()
    )

    print(
        "Mode:",
        BALANCE_MODE
    )

    print(
        "Automatic trading:",
        "DISABLED"
    )

    print("=" * 70)


    # --------------------------------------------------------
    # Credentials
    # --------------------------------------------------------

    if not IQ_EMAIL or not IQ_PASSWORD:

        message = (
            "🔴 *IQ OPTION SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )

        print(message)

        send_telegram(message)

        return


    api = None


    try:

        # ----------------------------------------------------
        # LOGIN
        # ----------------------------------------------------

        print(
            "\n[1/6] Connecting to IQ Option..."
        )


        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )


        check, reason = api.connect()


        print(
            "[LOGIN]",
            check,
            reason
        )


        if not check:

            send_telegram(
                "🔴 *IQ OPTION SCANNER*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Connection/login failed.\n\n"
                f"`{reason}`"
            )

            return


        print(
            "[LOGIN] Successful."
        )


        if hasattr(
            api,
            "check_connect",
        ):

            if not api.check_connect():

                send_telegram(
                    "🔴 *IQ OPTION SCANNER*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "IQ Option connection is not active."
                )

                return


        # ----------------------------------------------------
        # PRACTICE
        # ----------------------------------------------------

        print(
            "\n[2/6] Switching to PRACTICE..."
        )


        try:

            api.change_balance(
                BALANCE_MODE
            )

            print(
                "[BALANCE]",
                BALANCE_MODE
            )

        except Exception as e:

            print(
                "[BALANCE WARNING]",
                repr(e)
            )


        # ----------------------------------------------------
        # RAW INITIALIZATION
        # ----------------------------------------------------

        print(
            "\n[3/6] Requesting raw "
            "IQ Option market initialization..."
        )


        raw_data = get_raw_initialization(
            api
        )


        if not raw_data:

            send_telegram(
                "🔴 *IQ OPTION SCANNER*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "IQ Option connected, but "
                "raw market initialization "
                "returned no data."
            )

            return


        # ----------------------------------------------------
        # DIRECT OTC DISCOVERY
        # ----------------------------------------------------

        print(
            "\n[4/6] Extracting real OTC "
            "instruments and IDs..."
        )


        otc_assets = (
            discover_otc_from_initialization(
                raw_data
            )
        )


        if not otc_assets:

            print(
                "\n[STOP]"
            )

            print(
                "Raw IQ Option initialization "
                "contained no OPEN OTC "
                "binary/turbo instruments."
            )


            send_telegram(
                "🟡 *IQ OPTION OTC SCANNER*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Raw IQ Option initialization "
                "was received successfully, "
                "but it contained *0 OPEN OTC "
                "binary/turbo instruments*.\n\n"
                "The scanner did not invent "
                "any symbols and did not "
                "generate a signal.\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "READ-ONLY"
            )

            return


        # ----------------------------------------------------
        # CANDLE TEST
        # ----------------------------------------------------

        print(
            "\n[5/6] Testing real OTC "
            "candle feeds..."
        )


        working_assets = (
            test_candle_access(
                api,
                otc_assets
            )
        )


        if not working_assets:

            print(
                "\n[STOP]"
            )

            print(
                "OTC instruments were discovered "
                "but none returned usable candles."
            )


            send_telegram(
                "🟡 *IQ OPTION OTC SCANNER*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Found *{len(otc_assets)}* "
                "open OTC instruments.\n\n"
                "However, none returned enough "
                "candle data for scanning.\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "No strategy signals generated."
            )

            return


        # ----------------------------------------------------
        # PRECISION SCAN
        # ----------------------------------------------------

        print(
            "\n[6/6] Running precision strategy..."
        )


        locks = {}

        qualified = 0

        rejected = 0

        errors = 0


        for item in working_assets:

            asset = item["asset"]


            # -----------------------------------------------
            # 5-minute asset lock
            # -----------------------------------------------

            last_scan = locks.get(
                asset,
                0
            )


            if (
                time.time()
                - last_scan
                < LOCK_SECONDS
            ):

                continue


            locks[asset] = (
                time.time()
            )


            print(
                "\n" + "-" * 70
            )

            print(
                "[SCAN]",
                asset
            )

            print(
                "Market type:",
                item["market_type"]
            )

            print(
                "Active ID:",
                item["active_id"]
            )


            try:

                signal, reason = (
                    analyze_asset(
                        api,
                        asset
                    )
                )


                if signal:

                    qualified += 1


                    print(
                        "[QUALIFIED]",
                        signal["direction"],
                        "score=",
                        signal["score"]
                    )


                    send_telegram(
                        format_signal(
                            signal
                        )
                    )


                else:

                    rejected += 1


                    print(
                        "[NO TRADE]",
                        reason
                    )


            except Exception as e:

                errors += 1


                print(
                    "[SCAN ERROR]",
                    asset,
                    repr(e)
                )


                traceback.print_exc()


        # ----------------------------------------------------
        # SUMMARY
        # ----------------------------------------------------

        print(
            "\n" + "=" * 70
        )

        print(
            "SCAN COMPLETE"
        )

        print(
            "=" * 70
        )

        print(
            "Working OTC assets:",
            len(working_assets)
        )

        print(
            "Qualified signals:",
            qualified
        )

        print(
            "NO TRADE:",
            rejected
        )

        print(
            "Errors:",
            errors
        )

        print(
            "Finished:",
            now_utc()
        )

        print(
            "=" * 70
        )


        if qualified == 0:

            send_telegram(
                "🟡 *IQ OPTION OTC SCANNER*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"OTC candle feeds working: "
                f"*{len(working_assets)}*\n"
                f"Qualified signals: *0*\n\n"
                "The market data is available, "
                "but no setup reached the "
                f"*{MIN_SCORE}/100* threshold.\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "NO TRADE"
            )


    except Exception as e:

        print(
            "\n[FATAL ERROR]",
            repr(e)
        )

        traceback.print_exc()


        send_telegram(
            "🔴 *IQ OPTION SCANNER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Fatal scanner error:\n"
            f"`{str(e)[:800]}`"
        )


    finally:

        if api is not None:

            try:
                api.close()
            except Exception:
                pass


        print(
            "\nScanner stopped safely."
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
