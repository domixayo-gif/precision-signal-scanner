import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL V1
#
# REPLACEMENT FOR ZETA V3
#
# STRATEGY:
#   1M candles
#   Momentum period = 10
#   Strong LOW extreme -> CALL
#   Strong HIGH extreme -> PUT
#   Central / choppy -> NO TRADE
#   1-minute expiry
#
# PRACTICE / DEMO ONLY
#
# IMPORTANT:
#   No EMA
#   No RSI
#   No MACD
#   No ADX
#   No Bollinger Bands
#   No price action filters
#   No macro trend filters
#   No Martingale
# ============================================================


# ============================================================
# ACCOUNT / TRADE SETTINGS
# ============================================================

BALANCE_MODE = "PRACTICE"

STAKE = 1.0

EXPIRY_MINUTES = 1

TARGET_TRADES = 50


# ============================================================
# TIMEFRAME
# ============================================================

TF1 = 60

CANDLE_COUNT_1M = 220


# ============================================================
# REAL IQ OPTION OTC DISCOVERY
# ============================================================

MAX_OTC_ASSETS = 70

DISCOVERY_INTERVAL = 1800

RECONNECT_INTERVAL = 30


# ============================================================
# SCANNING
# ============================================================

SCAN_INTERVAL = 3

STATUS_INTERVAL = 300

SIGNAL_COOLDOWN_SECONDS = 60


# ============================================================
# MOMENTUM SETTINGS
# ============================================================

MOMENTUM_PERIOD = 10

# Number of recent Momentum readings used to determine
# the relative upper and lower extremes.
MOMENTUM_LOOKBACK = 50

# Extreme percentile.
#
# Lower 10% of recent Momentum values:
# CALL candidate
#
# Upper 10% of recent Momentum values:
# PUT candidate
#
# This is a relative threshold because the video describes
# the signal visually as reaching the top/bottom extreme
# rather than providing a fixed numerical Momentum value.
EXTREME_PERCENTILE = 0.10

# Minimum separation from the recent range.
#
# 0.0 means percentile alone determines the extreme.
MIN_EXTREME_DISTANCE = 0.0

# Require Momentum to show a turn away from the extreme.
#
# This prevents blindly entering while Momentum is still
# accelerating strongly in the same direction.
REQUIRE_TURN = True


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


# Keeps track of whether an asset is currently inside
# an extreme event.
#
# Once a signal is generated at an extreme, another signal
# from the same extreme is not generated until Momentum
# returns toward the central region.
extreme_state = {}


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
# MOMENTUM
#
# Momentum is calculated as:
#
# current close / close N periods ago * 100
#
# A value around 100 is the central reference.
#
# The strategy does NOT use a fixed 100/0 threshold
# to decide extremes. It compares the current Momentum
# with its recent distribution.
# ============================================================

def momentum_values(
    closes,
    period,
):

    result = [
        None
        for _ in closes
    ]

    if len(closes) <= period:
        return result

    for i in range(
        period,
        len(closes),
    ):

        previous = (
            closes[
                i - period
            ]
        )

        current = (
            closes[i]
        )

        if previous <= 0:
            continue

        result[i] = (
            current
            / previous
        ) * 100.0

    return result


# ============================================================
# SIMPLE PERCENTILE
# ============================================================

def percentile(
    values,
    fraction,
):

    if not values:
        return None

    ordered = sorted(
        values
    )

    if len(ordered) == 1:
        return ordered[0]

    position = (
        fraction
        * (
            len(ordered) - 1
        )
    )

    lower = int(
        math.floor(position)
    )

    upper = int(
        math.ceil(position)
    )

    if lower == upper:
        return ordered[lower]

    weight = (
        position
        - lower
    )

    return (
        ordered[lower]
        * (1.0 - weight)
        + ordered[upper]
        * weight
    )


# ============================================================
# MOMENTUM EXTREME ANALYSIS
# ============================================================

def analyze_momentum(
    candles
):

    minimum_required = (
        MOMENTUM_PERIOD
        + MOMENTUM_LOOKBACK
        + 5
    )

    if len(candles) < (
        minimum_required
    ):

        return None

    closes = [
        candle["close"]
        for candle in candles
    ]

    momentum = momentum_values(
        closes,
        MOMENTUM_PERIOD,
    )

    current_index = (
        len(momentum) - 1
    )

    previous_index = (
        current_index - 1
    )

    previous_previous_index = (
        current_index - 2
    )

    current = momentum[
        current_index
    ]

    previous = momentum[
        previous_index
    ]

    previous_previous = momentum[
        previous_previous_index
    ]

    if (
        current is None
        or previous is None
        or previous_previous is None
    ):

        return None

    recent_start = max(
        MOMENTUM_PERIOD,
        current_index
        - MOMENTUM_LOOKBACK
        + 1,
    )

    recent = []

    for i in range(
        recent_start,
        current_index + 1,
    ):

        value = momentum[i]

        if value is not None:

            recent.append(
                value
            )

    if len(recent) < 20:
        return None

    low_threshold = percentile(
        recent,
        EXTREME_PERCENTILE,
    )

    high_threshold = percentile(
        recent,
        1.0
        - EXTREME_PERCENTILE,
    )

    recent_low = min(
        recent
    )

    recent_high = max(
        recent
    )

    recent_range = (
        recent_high
        - recent_low
    )

    if recent_range <= 0:
        return None

    low_distance = (
        low_threshold
        - current
    )

    high_distance = (
        current
        - high_threshold
    )

    low_extreme = (
        current
        <= low_threshold
    )

    high_extreme = (
        current
        >= high_threshold
    )

    # --------------------------------------------------------
    # Turn detection
    #
    # CALL:
    # Momentum was falling and has started turning upward.
    #
    # PUT:
    # Momentum was rising and has started turning downward.
    # --------------------------------------------------------

    momentum_rising = (
        current > previous
    )

    momentum_falling = (
        current < previous
    )

    previous_was_falling = (
        previous
        <= previous_previous
    )

    previous_was_rising = (
        previous
        >= previous_previous
    )

    bullish_turn = (
        momentum_rising
        and previous_was_falling
    )

    bearish_turn = (
        momentum_falling
        and previous_was_rising
    )

    # --------------------------------------------------------
    # Strength
    # --------------------------------------------------------

    low_strength = (
        (
            low_threshold
            - current
        )
        / recent_range
    ) * 100.0

    high_strength = (
        (
            current
            - high_threshold
        )
        / recent_range
    ) * 100.0

    if low_strength < 0:
        low_strength = 0.0

    if high_strength < 0:
        high_strength = 0.0

    low_strength = min(
        low_strength,
        100.0,
    )

    high_strength = min(
        high_strength,
        100.0,
    )

    direction = None

    strength = 0.0

    extreme_type = "NONE"

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if low_extreme:

        if (
            not REQUIRE_TURN
            or bullish_turn
        ):

            direction = "CALL"

            strength = max(
                0.0,
                100.0
                - (
                    (
                        current
                        - recent_low
                    )
                    / recent_range
                )
                * 100.0,
            )

            extreme_type = (
                "LOW EXTREME"
            )

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    elif high_extreme:

        if (
            not REQUIRE_TURN
            or bearish_turn
        ):

            direction = "PUT"

            strength = max(
                0.0,
                100.0
                - (
                    (
                        recent_high
                        - current
                    )
                    / recent_range
                )
                * 100.0,
            )

            extreme_type = (
                "HIGH EXTREME"
            )

    # --------------------------------------------------------
    # CENTRAL / CHOPPY
    # --------------------------------------------------------

    central_distance = (
        abs(
            current
            - 100.0
        )
    )

    # A Momentum reading close to the middle
    # is considered central.
    #
    # This does NOT generate a trade.
    central_region = (
        current >= low_threshold
        and current <= high_threshold
    )

    return {
        "current":
            current,

        "previous":
            previous,

        "previous_previous":
            previous_previous,

        "low_threshold":
            low_threshold,

        "high_threshold":
            high_threshold,

        "recent_low":
            recent_low,

        "recent_high":
            recent_high,

        "recent_range":
            recent_range,

        "low_strength":
            low_strength,

        "high_strength":
            high_strength,

        "strength":
            strength,

        "direction":
            direction,

        "extreme_type":
            extreme_type,

        "central_region":
            central_region,

        "central_distance":
            central_distance,

        "momentum_rising":
            momentum_rising,

        "momentum_falling":
            momentum_falling,

        "bullish_turn":
            bullish_turn,

        "bearish_turn":
            bearish_turn,

        "timestamp":
            candles[
                current_index
            ]["from"],
    }


# ============================================================
# EXTREME STATE MANAGEMENT
# ============================================================

def update_extreme_state(
    asset,
    analysis,
):

    state = extreme_state.get(
        asset,
        "CENTER",
    )

    if analysis is None:

        return state

    current = analysis[
        "current"
    ]

    low_threshold = analysis[
        "low_threshold"
    ]

    high_threshold = analysis[
        "high_threshold"
    ]

    # --------------------------------------------------------
    # Once Momentum returns inside the recent central range,
    # unlock the next extreme.
    # --------------------------------------------------------

    if (
        current > low_threshold
        and current < high_threshold
    ):

        extreme_state[
            asset
        ] = "CENTER"

        return "CENTER"

    if current <= low_threshold:

        if state != "LOW":

            extreme_state[
                asset
            ] = "LOW"

        return "LOW"

    if current >= high_threshold:

        if state != "HIGH":

            extreme_state[
                asset
            ] = "HIGH"

        return "HIGH"

    return state


# ============================================================
# STRATEGY ENGINE
# ============================================================

def evaluate_momentum_strategy(
    asset,
    candles_1m,
):

    analysis = analyze_momentum(
        candles_1m
    )

    if analysis is None:
        return None

    previous_state = (
        extreme_state.get(
            asset,
            "CENTER",
        )
    )

    current_state = (
        update_extreme_state(
            asset,
            analysis,
        )
    )

    direction = analysis[
        "direction"
    ]

    # --------------------------------------------------------
    # No signal.
    # --------------------------------------------------------

    if direction is None:
        return None

    # --------------------------------------------------------
    # Prevent repeated trades from the same extreme.
    #
    # The Momentum must first return toward the center
    # before another signal from the same side can happen.
    # --------------------------------------------------------

    if (
        previous_state
        == current_state
    ):

        return None

    # --------------------------------------------------------
    # Cooldown.
    # --------------------------------------------------------

    candle_time = analysis[
        "timestamp"
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

    # --------------------------------------------------------
    # Signal ID.
    # --------------------------------------------------------

    signal_id = (
        "MOM10-"
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
    # Signal.
    # --------------------------------------------------------

    return {
        "signal_id":
            signal_id,

        "asset":
            asset,

        "direction":
            direction,

        "price":
            candles_1m[-1][
                "close"
            ],

        "momentum":
            analysis[
                "current"
            ],

        "momentum_previous":
            analysis[
                "previous"
            ],

        "momentum_previous_2":
            analysis[
                "previous_previous"
            ],

        "low_threshold":
            analysis[
                "low_threshold"
            ],

        "high_threshold":
            analysis[
                "high_threshold"
            ],

        "recent_low":
            analysis[
                "recent_low"
            ],

        "recent_high":
            analysis[
                "recent_high"
            ],

        "strength":
            analysis[
                "strength"
            ],

        "low_strength":
            analysis[
                "low_strength"
            ],

        "high_strength":
            analysis[
                "high_strength"
            ],

        "extreme_type":
            analysis[
                "extreme_type"
            ],

        "bullish_turn":
            analysis[
                "bullish_turn"
            ],

        "bearish_turn":
            analysis[
                "bearish_turn"
            ],

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

    if direction == "CALL":

        emoji = "🟢"

        interpretation = (
            "Momentum reached a strong "
            "LOW extreme and started "
            "turning upward."
        )

    else:

        emoji = "🔴"

        interpretation = (
            "Momentum reached a strong "
            "HIGH extreme and started "
            "turning downward."
        )

    return (
        f"{emoji} *MOMENTUM 10 SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {signal['asset']}\n"
        f"*Direction:* *{direction}*\n"
        "*Timeframe:* 1M\n"
        "*Expiry:* 1 minute\n"
        "*Indicator:* Momentum\n"
        "*Period:* 10\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Momentum:* "
        f"{signal['momentum']:.5f}\n"
        f"*Previous:* "
        f"{signal['momentum_previous']:.5f}\n"
        f"*Previous 2:* "
        f"{signal['momentum_previous_2']:.5f}\n"
        f"*Recent Low:* "
        f"{signal['recent_low']:.5f}\n"
        f"*Recent High:* "
        f"{signal['recent_high']:.5f}\n"
        f"*Low Extreme:* "
        f"{signal['low_threshold']:.5f}\n"
        f"*High Extreme:* "
        f"{signal['high_threshold']:.5f}\n"
        f"*Extreme:* "
        f"{signal['extreme_type']}\n"
        f"*Strength:* "
        f"{signal['strength']:.1f}%\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Interpretation:* "
        f"{interpretation}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Price:* "
        f"{signal['price']:.8f}\n"
        f"*Signal ID:* "
        f"{signal['signal_id']}\n"
        f"*Time:* "
        f"{signal['timestamp']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 *MOMENTUM 10 — PRACTICE ONLY*"
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
            "🔴 *MOMENTUM 10 TRADE ERROR*\n"
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
        "[MOMENTUM 10 PRACTICE EXECUTION]"
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
        "Momentum:",
        signal["momentum"],
    )

    print(
        "Strength:",
        signal["strength"],
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
        "🟡 *MOMENTUM 10 SENDING DEMO ORDER*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Asset:* {asset}\n"
        f"*Active ID:* {active_id}\n"
        f"*Direction:* {direction}\n"
        f"*Momentum:* "
        f"{signal['momentum']:.5f}\n"
        f"*Extreme:* "
        f"{signal['extreme_type']}\n"
        f"*Strength:* "
        f"{signal['strength']:.1f}%\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Expiry:* "
        f"{EXPIRY_MINUTES} minute\n"
        f"*Signal ID:* "
        f"{signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Sending PRACTICE order..."
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
            "🔴 *MOMENTUM 10 TRADE ERROR*\n"
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
            "🔴 *MOMENTUM 10 TRADE REJECTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Momentum:* "
            f"{signal['momentum']:.5f}\n"
            f"*Signal ID:* "
            f"{signal['signal_id']}\n"
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
            "🟡 *MOMENTUM 10 ORDER ACCEPTED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"*Trade number:* "
            f"{total_trades}/{TARGET_TRADES}\n"
            f"*Asset:* {asset}\n"
            f"*Direction:* {direction}\n"
            f"*Stake:* ${STAKE:.2f}\n"
            "*Expiry:* 1 minute\n"
            f"*Signal ID:* "
            f"{signal['signal_id']}\n"
            "*Trade ID:* NOT RETURNED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📋 Track the result manually "
            "in IQ Option."
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
        "🚀 *MOMENTUM 10 DEMO TRADE OPENED*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"*Trade #:* "
        f"{total_trades}/{TARGET_TRADES}\n"
        f"*Asset:* {asset}\n"
        f"*Direction:* *{direction}*\n"
        f"*Momentum:* "
        f"{signal['momentum']:.5f}\n"
        f"*Extreme:* "
        f"{signal['extreme_type']}\n"
        f"*Strength:* "
        f"{signal['strength']:.1f}%\n"
        f"*Stake:* ${STAKE:.2f}\n"
        "*Expiry:* *1 minute*\n"
        f"*Trade ID:* {trade_id}\n"
        f"*Signal ID:* "
        f"{signal['signal_id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 *PRACTICE / DEMO ONLY*\n"
        "📋 Track the result manually "
        "in IQ Option."
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
                20,
            )
        )

        if len(candles) >= 15:

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
        "🟡 *MOMENTUM 10 HEARTBEAT*\n"
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
        "*Automatic trading:* ON\n"
        f"*Stake:* ${STAKE:.2f}\n"
        "*Timeframe:* 1M\n"
        "*Expiry:* 1 minute\n"
        "*Indicator:* Momentum 10\n"
        "*Strategy:* Extreme Reversal\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if balance is not None:

        message += (
            f"*Practice balance:* "
            f"${safe_float(balance):.2f}\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 Results are tracked manually "
        "in IQ Option.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *MOMENTUM 10 SCANNER ONLINE*"
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
            "🔴 *MOMENTUM 10*\n"
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
            "🔴 *MOMENTUM 10 CONNECTION FAILED*\n"
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
        "🟢 MOMENTUM 10 DEMO TRADER ONLINE"
    )

    print(
        "=" * 70
    )

    send_telegram(
        "🟢 *MOMENTUM 10 ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Connection:* OK\n"
        f"*Account:* {BALANCE_MODE}\n"
        "*Strategy:* Momentum 10 "
        "Extreme Reversal\n"
        "*Timeframe:* 1M\n"
        "*Expiry:* 1 minute\n"
        "*Extreme detection:* Relative\n"
        f"*Lookback:* "
        f"{MOMENTUM_LOOKBACK} candles\n"
        f"*Extreme percentile:* "
        f"{EXTREME_PERCENTILE:.0%}\n"
        f"*Stake:* ${STAKE:.2f}\n"
        f"*Target:* "
        f"{TARGET_TRADES} demo orders\n"
        "*Martingale:* OFF\n"
        "*Account mode:* PRACTICE\n"
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
                "*Timeframe:* 1M\n"
                "*Expiry:* 1 minute\n"
                "*Strategy:* Momentum 10\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Momentum 10 scanning started."
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
                    "🏁 *MOMENTUM 10 "
                    "50-TRADE TEST REACHED*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"*Demo orders opened:* "
                    f"{total_trades}\n"
                    "*Results:* "
                    "Track manually in IQ Option\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "The Practice test is complete."
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
                    "🔴 *MOMENTUM 10 "
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
                        "[MOMENTUM 10 SCAN]",
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
                            # 1M CANDLES
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
                            ) < (
                                MOMENTUM_PERIOD
                                + MOMENTUM_LOOKBACK
                                + 5
                            ):

                                continue

                            # --------------------------------
                            # MOMENTUM STRATEGY
                            # --------------------------------

                            signal = (
                                evaluate_momentum_strategy(
                                    asset,
                                    candles_1m,
                                )
                            )

                            if signal is None:
                                continue

                            print(
                                "\n"
                                "[MOMENTUM 10 SIGNAL]",
                                asset,
                            )

                            print(
                                "DIRECTION:",
                                signal[
                                    "direction"
                                ],
                            )

                            print(
                                "MOMENTUM:",
                                signal[
                                    "momentum"
                                ],
                            )

                            print(
                                "EXTREME:",
                                signal[
                                    "extreme_type"
                                ],
                            )

                            print(
                                "STRENGTH:",
                                signal[
                                    "strength"
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

                    print(
                        "[MOMENTUM 10 "
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
        "MOMENTUM 10 "
        "EXTREME-REVERSAL V1"
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
        "Momentum 10 Extreme Reversal",
    )

    print(
        "Timeframe:",
        "1M",
    )

    print(
        "Momentum period:",
        MOMENTUM_PERIOD,
    )

    print(
        "Extreme lookback:",
        MOMENTUM_LOOKBACK,
    )

    print(
        "Extreme percentile:",
        f"{EXTREME_PERCENTILE:.0%}",
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
        "minute",
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
        "Martingale:",
        "OFF",
    )

    print(
        "Target:",
        TARGET_TRADES,
        "demo orders",
    )

    print(
        "Automatic trading:",
        "PRACTICE ONLY",
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
            "🔴 *MOMENTUM 10 FATAL ERROR*\n"
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
            "\nMOMENTUM 10 stopped."
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    main()
