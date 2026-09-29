# ============================================================
# BACK TO TREND — IQ OPTION OTC DEMO AUTO-TESTER V2
# ============================================================
#
# 30M TREND -> 5M PULLBACK -> 1M ENTRY
# EXPIRY: 3 MINUTES
#
# REAL IQ OPTION OTC ASSETS ONLY
#
# PRACTICE ACCOUNT ONLY
# AUTOMATIC DEMO TRADING ENABLED
#
# IMPORTANT:
# This version does NOT depend on get_all_open_time()
# for OTC discovery.
#
# ============================================================

import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# CREDENTIALS
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# DEMO SAFETY
# ============================================================

PRACTICE = True
AUTO_TRADE = True
DEMO_ONLY_LOCK = True

STAKE = 1.0

MAX_AUTO_TRADES = 30

MAX_CONSECUTIVE_LOSSES = 4

TRADE_COOLDOWN = 180


# ============================================================
# TIMEFRAMES
# ============================================================

TF_30M = 1800
TF_5M = 300
TF_1M = 60

EXPIRY_MINUTES = 3


# ============================================================
# CANDLE SETTINGS
# ============================================================

CANDLES_30M = 180
CANDLES_5M = 180
CANDLES_1M = 180


# ============================================================
# SCANNER SETTINGS
# ============================================================

SCAN_INTERVAL = 60
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30

MAX_RUNTIME = 24 * 60 * 60


# ============================================================
# STRATEGY
# ============================================================

EMA_FAST = 20
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

SWING_LOOKBACK = 30

MIN_SCORE = 90

MIN_ADX = 20

MIN_ROOM_ATR = 1.20

MAX_TRIGGER_RANGE_ATR = 1.50

MAX_EXTENSION_ATR = 2.20

MAX_5M_DISTANCE_ATR = 0.65

MIN_TRIGGER_BODY = 0.35


# ============================================================
# GLOBALS
# ============================================================

iq = None

active_trade = None

seen_signal_keys = set()

last_trade_time = 0

last_status_time = 0

session_started = 0

trade_count = 0

wins = 0

losses = 0

draws = 0

total_profit = 0.0

consecutive_losses = 0

telegram_offset = None


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_float(value, default=0.0):

    try:

        if value is None:
            return default

        value = float(value)

        if not math.isfinite(value):
            return default

        return value

    except Exception:

        return default


def utc_text():

    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    print(message)

    if not TELEGRAM_TOKEN:
        return False

    if not TELEGRAM_CHAT_ID:
        return False

    try:

        url = (
            "https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }

        response = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        return response.ok

    except Exception as exc:

        print(
            "Telegram error:",
            exc,
        )

        return False


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return []

    multiplier = (
        2.0 / (period + 1)
    )

    seed = (
        sum(values[:period])
        / period
    )

    result = [seed]

    previous = seed

    for value in values[period:]:

        current = (
            (
                value - previous
            )
            * multiplier
            + previous
        )

        result.append(current)

        previous = current

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):

    if len(values) < period + 1:
        return []

    gains = []
    losses_ = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses_.append(0.0)

        else:

            gains.append(0.0)
            losses_.append(
                abs(change)
            )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses_[:period])
        / period
    )

    result = []

    for i in range(
        period - 1,
        len(gains),
    ):

        if i >= period:

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
                + losses_[i]
            ) / period

        if avg_loss == 0:

            result.append(100.0)

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result.append(
                100.0
                - (
                    100.0
                    / (1.0 + rs)
                )
            )

    return result


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):

    if len(candles) < period + 1:
        return []

    trs = []

    for i in range(1, len(candles)):

        high = safe_float(
            candles[i]["max"]
        )

        low = safe_float(
            candles[i]["min"]
        )

        previous_close = safe_float(
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

        trs.append(tr)

    if len(trs) < period:
        return []

    first = (
        sum(trs[:period])
        / period
    )

    result = [first]

    previous = first

    for value in trs[period:]:

        current = (
            (
                previous
                * (period - 1)
            )
            + value
        ) / period

        result.append(current)

        previous = current

    return result


# ============================================================
# ADX
# ============================================================

def adx(candles, period=14):

    if len(candles) < (
        period * 2
    ) + 2:

        return []

    trs = []

    plus_dm = []

    minus_dm = []

    for i in range(1, len(candles)):

        high = safe_float(
            candles[i]["max"]
        )

        low = safe_float(
            candles[i]["min"]
        )

        previous_high = safe_float(
            candles[i - 1]["max"]
        )

        previous_low = safe_float(
            candles[i - 1]["min"]
        )

        previous_close = safe_float(
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

        up = (
            high
            - previous_high
        )

        down = (
            previous_low
            - low
        )

        plus = (
            up
            if up > down and up > 0
            else 0.0
        )

        minus = (
            down
            if down > up and down > 0
            else 0.0
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    tr_sum = sum(
        trs[:period]
    )

    plus_sum = sum(
        plus_dm[:period]
    )

    minus_sum = sum(
        minus_dm[:period]
    )

    atr_values = [
        tr_sum / period
    ]

    plus_values = [
        plus_sum / period
    ]

    minus_values = [
        minus_sum / period
    ]

    for i in range(
        period,
        len(trs),
    ):

        tr_sum = (
            tr_sum
            - (
                tr_sum / period
            )
            + trs[i]
        )

        plus_sum = (
            plus_sum
            - (
                plus_sum / period
            )
            + plus_dm[i]
        )

        minus_sum = (
            minus_sum
            - (
                minus_sum / period
            )
            + minus_dm[i]
        )

        atr_values.append(
            tr_sum / period
        )

        plus_values.append(
            plus_sum / period
        )

        minus_values.append(
            minus_sum / period
        )

    dx = []

    for i in range(
        len(atr_values)
    ):

        if atr_values[i] <= 0:

            dx.append(0.0)

            continue

        plus_di = (
            100.0
            * plus_values[i]
            / atr_values[i]
        )

        minus_di = (
            100.0
            * minus_values[i]
            / atr_values[i]
        )

        total = (
            plus_di
            + minus_di
        )

        if total <= 0:

            dx.append(0.0)

        else:

            dx.append(
                100.0
                * abs(
                    plus_di
                    - minus_di
                )
                / total
            )

    if len(dx) < period:
        return []

    first = (
        sum(dx[:period])
        / period
    )

    result = [first]

    previous = first

    for value in dx[period:]:

        current = (
            (
                previous
                * (period - 1)
            )
            + value
        ) / period

        result.append(current)

        previous = current

    return result


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_body(candle):

    return abs(
        safe_float(
            candle["close"]
        )
        - safe_float(
            candle["open"]
        )
    )


def candle_range(candle):

    return (
        safe_float(
            candle["max"]
        )
        - safe_float(
            candle["min"]
        )
    )


def candle_direction(candle):

    opening = safe_float(
        candle["open"]
    )

    closing = safe_float(
        candle["close"]
    )

    if closing > opening:
        return "BULLISH"

    if closing < opening:
        return "BEARISH"

    return "NEUTRAL"


def bullish_engulfing(
    previous,
    current,
):

    po = safe_float(
        previous["open"]
    )

    pc = safe_float(
        previous["close"]
    )

    co = safe_float(
        current["open"]
    )

    cc = safe_float(
        current["close"]
    )

    return (
        pc < po
        and cc > co
        and co <= pc
        and cc >= po
    )


def bearish_engulfing(
    previous,
    current,
):

    po = safe_float(
        previous["open"]
    )

    pc = safe_float(
        previous["close"]
    )

    co = safe_float(
        current["open"]
    )

    cc = safe_float(
        current["close"]
    )

    return (
        pc > po
        and cc < co
        and co >= pc
        and cc <= po
    )


def bullish_pin(candle):

    opening = safe_float(
        candle["open"]
    )

    closing = safe_float(
        candle["close"]
    )

    high = safe_float(
        candle["max"]
    )

    low = safe_float(
        candle["min"]
    )

    body = abs(
        closing - opening
    )

    lower = (
        min(opening, closing)
        - low
    )

    upper = (
        high
        - max(opening, closing)
    )

    rng = high - low

    if rng <= 0:
        return False

    return (
        lower >= body * 1.5
        and lower > upper
        and closing
        >= low + rng * 0.55
    )


def bearish_pin(candle):

    opening = safe_float(
        candle["open"]
    )

    closing = safe_float(
        candle["close"]
    )

    high = safe_float(
        candle["max"]
    )

    low = safe_float(
        candle["min"]
    )

    body = abs(
        closing - opening
    )

    lower = (
        min(opening, closing)
        - low
    )

    upper = (
        high
        - max(opening, closing)
    )

    rng = high - low

    if rng <= 0:
        return False

    return (
        upper >= body * 1.5
        and upper > lower
        and closing
        <= low + rng * 0.45
    )


# ============================================================
# SERVER TIME
# ============================================================

def server_now():

    try:

        if iq is not None:

            timestamp = (
                iq.get_server_timestamp()
            )

            if timestamp:

                return int(timestamp)

    except Exception:
        pass

    return int(
        time.time()
    )


# ============================================================
# CLOSED CANDLES
# ============================================================

def get_closed_candles(
    asset,
    interval,
    count,
):

    try:

        end_time = server_now()

        candles = iq.get_candles(
            asset,
            interval,
            count + 5,
            end_time,
        )

        if not candles:
            return []

        candles = sorted(
            candles,
            key=lambda x: safe_float(
                x.get(
                    "from",
                    0,
                )
            ),
        )

        current_bucket = (
            int(end_time)
            // interval
        ) * interval

        closed = []

        for candle in candles:

            candle_time = int(
                safe_float(
                    candle.get(
                        "from",
                        0,
                    )
                )
            )

            if (
                candle_time
                < current_bucket
            ):

                closed.append(
                    candle
                )

        return closed[-count:]

    except Exception as exc:

        print(
            f"Candle error "
            f"{asset} "
            f"{interval}: {exc}"
        )

        return []


# ============================================================
# DIRECT OTC DISCOVERY
# ============================================================
#
# We intentionally do NOT use get_all_open_time().
#
# This avoids the NoneType/polling problem seen in the
# installed API build.
#
# ============================================================

def discover_otc_assets():

    binary_assets = set()

    turbo_assets = set()

    digital_assets = set()

    try:

        # ----------------------------------------------------
        # PRIMARY SOURCE
        # ----------------------------------------------------

        data = None

        if hasattr(
            iq,
            "get_all_init_v2",
        ):

            data = (
                iq.get_all_init_v2()
            )

        if not data:

            telegram(
                "🔴 OTC INIT DATA EMPTY\n"
                "IQ Option connected, but "
                "get_all_init_v2() returned "
                "no market data."
            )

            return []

        # ----------------------------------------------------
        # BINARY / TURBO
        # ----------------------------------------------------

        for market_type, target in (
            (
                "binary",
                binary_assets,
            ),
            (
                "turbo",
                turbo_assets,
            ),
        ):

            market = data.get(
                market_type,
                {},
            )

            if not isinstance(
                market,
                dict,
            ):

                continue

            actives = market.get(
                "actives",
                {},
            )

            if not isinstance(
                actives,
                dict,
            ):

                continue

            for active_id, info in (
                actives.items()
            ):

                if not isinstance(
                    info,
                    dict,
                ):

                    continue

                name = (
                    info.get("name")
                    or info.get(
                        "underlying"
                    )
                    or ""
                )

                name = str(
                    name
                )

                if not name:
                    continue

                # Some versions return names
                # like "1.EURUSD-OTC".
                if "." in name:

                    name = (
                        name.split(
                            ".",
                            1,
                        )[1]
                    )

                if (
                    "OTC"
                    not in name.upper()
                ):

                    continue

                enabled = info.get(
                    "enabled",
                    True,
                )

                suspended = info.get(
                    "is_suspended",
                    False,
                )

                if (
                    enabled
                    and not suspended
                ):

                    target.add(
                        name
                    )

        # ----------------------------------------------------
        # DIGITAL
        # ----------------------------------------------------

        try:

            if hasattr(
                iq,
                "get_digital_underlying_list_data",
            ):

                digital_data = (
                    iq.get_digital_underlying_list_data()
                )

                if isinstance(
                    digital_data,
                    dict,
                ):

                    underlying = (
                        digital_data.get(
                            "underlying",
                            [],
                        )
                    )

                    if isinstance(
                        underlying,
                        list,
                    ):

                        current = time.time()

                        for item in underlying:

                            if not isinstance(
                                item,
                                dict,
                            ):

                                continue

                            name = str(
                                item.get(
                                    "underlying",
                                    "",
                                )
                            )

                            if (
                                "OTC"
                                not in name.upper()
                            ):

                                continue

                            schedule = (
                                item.get(
                                    "schedule",
                                    [],
                                )
                            )

                            open_now = False

                            if isinstance(
                                schedule,
                                list,
                            ):

                                for row in schedule:

                                    if not isinstance(
                                        row,
                                        dict,
                                    ):

                                        continue

                                    start = safe_float(
                                        row.get(
                                            "open",
                                            0,
                                        )
                                    )

                                    close = safe_float(
                                        row.get(
                                            "close",
                                            0,
                                        )
                                    )

                                    if (
                                        start
                                        < current
                                        < close
                                    ):

                                        open_now = True

                                        break

                            if open_now:

                                digital_assets.add(
                                    name
                                )

        except Exception as exc:

            print(
                "Digital discovery warning:",
                exc,
            )

        # ----------------------------------------------------
        # BUILD REAL TRADING LIST
        # ----------------------------------------------------
        #
        # Automatic trading uses binary/turbo only.
        # Digital is reported but not automatically
        # traded by this version because its order API
        # is different.
        #
        trading_assets = sorted(
            binary_assets
            | turbo_assets
        )

        all_otc = sorted(
            set(trading_assets)
            | digital_assets
        )

        telegram(
            "🟢 OTC DISCOVERY SUCCESS\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Binary OTC: "
            f"{len(binary_assets)}\n"
            f"Turbo OTC: "
            f"{len(turbo_assets)}\n"
            f"Digital OTC: "
            f"{len(digital_assets)}\n"
            f"Tradable by this bot: "
            f"{len(trading_assets)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            + (
                "Sample: "
                + ", ".join(
                    trading_assets[:12]
                )
                if trading_assets
                else "No binary/turbo OTC assets."
            )
        )

        if not trading_assets:

            telegram(
                "🟠 NO BINARY/TURBO OTC ASSETS\n"
                "Digital OTC may exist, but this "
                "demo auto-trader is waiting for "
                "a binary/turbo OTC instrument."
            )

        return trading_assets

    except Exception as exc:

        print(
            traceback.format_exc()
        )

        telegram(
            "🔴 OTC DISCOVERY ERROR\n"
            f"{type(exc).__name__}: {exc}\n"
            "\n"
            "Direct OTC discovery failed.\n"
            "Scanner will retry automatically."
        )

        return []


# ============================================================
# TREND
# ============================================================

def analyze_trend(candles):

    if len(candles) < 70:
        return None

    closes = [
        safe_float(
            c["close"]
        )
        for c in candles
    ]

    fast_values = ema(
        closes,
        EMA_FAST,
    )

    slow_values = ema(
        closes,
        EMA_SLOW,
    )

    rsi_values = rsi(
        closes,
        RSI_PERIOD,
    )

    atr_values = atr(
        candles,
        ATR_PERIOD,
    )

    adx_values = adx(
        candles,
        ADX_PERIOD,
    )

    if not fast_values:
        return None

    if not slow_values:
        return None

    if not rsi_values:
        return None

    if not atr_values:
        return None

    if not adx_values:
        return None

    fast = fast_values[-1]

    slow = slow_values[-1]

    price = closes[-1]

    trend = "NEUTRAL"

    if (
        fast > slow
        and price > fast
    ):

        trend = "BULLISH"

    elif (
        fast < slow
        and price < fast
    ):

        trend = "BEARISH"

    return {
        "trend": trend,
        "price": price,
        "ema_fast": fast,
        "ema_slow": slow,
        "rsi": rsi_values[-1],
        "atr": atr_values[-1],
        "adx": adx_values[-1],
    }


# ============================================================
# STRUCTURE
# ============================================================

def get_structure(candles):

    if len(candles) < 35:
        return None

    recent = candles[
        -SWING_LOOKBACK:
    ]

    highs = [
        safe_float(
            c["max"]
        )
        for c in recent
    ]

    lows = [
        safe_float(
            c["min"]
        )
        for c in recent
    ]

    price = safe_float(
        candles[-1]["close"]
    )

    return {
        "price": price,
        "high": max(highs),
        "low": min(lows),
    }


# ============================================================
# PULLBACK
# ============================================================

def check_pullback(
    trend,
    candles,
):

    if len(candles) < 6:
        return False

    atr_value = trend["atr"]

    if atr_value <= 0:
        return False

    recent = candles[-5:]

    bullish = 0
    bearish = 0

    for candle in recent:

        direction = (
            candle_direction(
                candle
            )
        )

        if direction == "BULLISH":
            bullish += 1

        elif direction == "BEARISH":
            bearish += 1

    last = candles[-1]

    previous = candles[-2]

    if trend["trend"] == "BULLISH":

        touched = (
            safe_float(
                previous["min"]
            )
            <= trend["ema_fast"]
            + (
                atr_value
                * 0.45
            )
        )

        had_pullback = (
            bearish >= 1
        )

        return (
            touched
            or had_pullback
        )

    if trend["trend"] == "BEARISH":

        touched = (
            safe_float(
                previous["max"]
            )
            >= trend["ema_fast"]
            - (
                atr_value
                * 0.45
            )
        )

        had_pullback = (
            bullish >= 1
        )

        return (
            touched
            or had_pullback
        )

    return False


# ============================================================
# TRIGGER
# ============================================================

def get_trigger(
    candles,
    direction,
):

    if len(candles) < 4:
        return None

    previous = candles[-2]

    current = candles[-1]

    rng = candle_range(
        current
    )

    body = candle_body(
        current
    )

    if rng <= 0:
        return None

    body_ratio = (
        body / rng
    )

    atr_values = atr(
        candles,
        ATR_PERIOD,
    )

    if not atr_values:
        return None

    atr_value = atr_values[-1]

    if atr_value <= 0:
        return None

    range_atr = (
        rng / atr_value
    )

    if (
        range_atr
        > MAX_TRIGGER_RANGE_ATR
    ):

        return None

    if direction == "CALL":

        if bullish_engulfing(
            previous,
            current,
        ):

            return {
                "name":
                    "BULLISH ENGULFING",
                "range_atr":
                    range_atr,
                "body_ratio":
                    body_ratio,
            }

        if bullish_pin(
            current
        ):

            return {
                "name":
                    "BULLISH PIN",
                "range_atr":
                    range_atr,
                "body_ratio":
                    body_ratio,
            }

        if (
            candle_direction(
                current
            )
            == "BULLISH"
            and body_ratio
            >= MIN_TRIGGER_BODY
        ):

            return {
                "name":
                    "BULLISH BODY",
                "range_atr":
                    range_atr,
                "body_ratio":
                    body_ratio,
            }

    if direction == "PUT":

        if bearish_engulfing(
            previous,
            current,
        ):

            return {
                "name":
                    "BEARISH ENGULFING",
                "range_atr":
                    range_atr,
                "body_ratio":
                    body_ratio,
            }

        if bearish_pin(
            current
        ):

            return {
                "name":
                    "BEARISH PIN",
                "range_atr":
                    range_atr,
                "body_ratio":
                    body_ratio,
            }

        if (
            candle_direction(
                current
            )
            == "BEARISH"
            and body_ratio
            >= MIN_TRIGGER_BODY
        ):

            return {
                "name":
                    "BEARISH BODY",
                "range_atr":
                    range_atr,
                "body_ratio":
                    body_ratio,
            }

    return None


# ============================================================
# EVALUATE ASSET
# ============================================================

def evaluate_asset(asset):

    try:

        candles_30m = (
            get_closed_candles(
                asset,
                TF_30M,
                CANDLES_30M,
            )
        )

        candles_5m = (
            get_closed_candles(
                asset,
                TF_5M,
                CANDLES_5M,
            )
        )

        candles_1m = (
            get_closed_candles(
                asset,
                TF_1M,
                CANDLES_1M,
            )
        )

        if len(
            candles_30m
        ) < 70:

            return None

        if len(
            candles_5m
        ) < 70:

            return None

        if len(
            candles_1m
        ) < 40:

            return None

        trend_30m = (
            analyze_trend(
                candles_30m
            )
        )

        trend_5m = (
            analyze_trend(
                candles_5m
            )
        )

        if not trend_30m:
            return None

        if not trend_5m:
            return None

        # ----------------------------------------------------
        # 30M DIRECTION
        # ----------------------------------------------------

        if trend_30m["trend"] == "BULLISH":

            direction = "CALL"

        elif trend_30m["trend"] == "BEARISH":

            direction = "PUT"

        else:

            return None

        # ----------------------------------------------------
        # 5M MUST AGREE
        # ----------------------------------------------------

        if (
            trend_5m["trend"]
            != trend_30m["trend"]
        ):

            return None

        score = 25

        # ----------------------------------------------------
        # ADX
        # ----------------------------------------------------

        if (
            trend_30m["adx"]
            < MIN_ADX
        ):

            return None

        score += 15

        # ----------------------------------------------------
        # 5M EMA DISTANCE
        # ----------------------------------------------------

        atr_5m = trend_5m["atr"]

        if atr_5m <= 0:
            return None

        distance = abs(
            trend_5m["price"]
            - trend_5m["ema_fast"]
        )

        distance_atr = (
            distance
            / atr_5m
        )

        if (
            distance_atr
            > MAX_5M_DISTANCE_ATR
        ):

            return None

        score += 15

        # ----------------------------------------------------
        # PULLBACK
        # ----------------------------------------------------

        if not check_pullback(
            trend_5m,
            candles_5m,
        ):

            return None

        score += 15

        # ----------------------------------------------------
        # 1M TRIGGER
        # ----------------------------------------------------

        trigger = get_trigger(
            candles_1m,
            direction,
        )

        if not trigger:
            return None

        score += 20

        # ----------------------------------------------------
        # 1M RSI
        # ----------------------------------------------------

        closes_1m = [
            safe_float(
                c["close"]
            )
            for c in candles_1m
        ]

        rsi_values = rsi(
            closes_1m,
            RSI_PERIOD,
        )

        if not rsi_values:
            return None

        rsi_1m = rsi_values[-1]

        if direction == "CALL":

            if not (
                45
                <= rsi_1m
                <= 70
            ):

                return None

        else:

            if not (
                30
                <= rsi_1m
                <= 55
            ):

                return None

        score += 5

        # ----------------------------------------------------
        # ROOM
        # ----------------------------------------------------

        structure = get_structure(
            candles_5m
        )

        if not structure:
            return None

        price = structure["price"]

        if direction == "CALL":

            room = (
                structure["high"]
                - price
            )

        else:

            room = (
                price
                - structure["low"]
            )

        room_atr = (
            room
            / atr_5m
        )

        if (
            room_atr
            < MIN_ROOM_ATR
        ):

            return None

        score += 5

        # ----------------------------------------------------
        # EXTENSION
        # ----------------------------------------------------

        extension_atr = (
            distance
            / atr_5m
        )

        if (
            extension_atr
            > MAX_EXTENSION_ATR
        ):

            return None

        return {
            "asset": asset,
            "direction": direction,
            "score": score,
            "price": price,
            "trend_30m":
                trend_30m["trend"],
            "trend_5m":
                trend_5m["trend"],
            "rsi_30m":
                trend_30m["rsi"],
            "rsi_5m":
                trend_5m["rsi"],
            "rsi_1m":
                rsi_1m,
            "adx":
                trend_30m["adx"],
            "room_atr":
                room_atr,
            "extension_atr":
                extension_atr,
            "trigger":
                trigger["name"],
        }

    except Exception as exc:

        print(
            f"Evaluation error "
            f"{asset}: {exc}"
        )

        return None


# ============================================================
# SIGNAL ID
# ============================================================

def make_signal_id(
    signal
):

    asset = (
        signal["asset"]
        .replace(
            "/",
            "",
        )
        .replace(
            "-",
            "",
        )
    )

    timestamp = (
        datetime.now(
            timezone.utc
        ).strftime(
            "%H%M%S"
        )
    )

    return (
        f"{asset.upper()}-"
        f"{signal['direction']}-"
        f"{timestamp}"
    )


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def send_signal(
    signal,
    sid,
):

    telegram(
        "🟢 QUALIFIED BACK TO TREND SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {signal['asset']}\n"
        f"Direction: "
        f"{signal['direction']}\n"
        f"Score: "
        f"{signal['score']}/100\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES} minutes\n"
        f"Price: "
        f"{signal['price']}\n"
        "\n"
        f"30M Trend: "
        f"{signal['trend_30m']}\n"
        f"5M Trend: "
        f"{signal['trend_5m']}\n"
        f"1M Trigger: "
        f"{signal['trigger']}\n"
        f"30M RSI: "
        f"{signal['rsi_30m']:.1f}\n"
        f"5M RSI: "
        f"{signal['rsi_5m']:.1f}\n"
        f"1M RSI: "
        f"{signal['rsi_1m']:.1f}\n"
        f"ADX: "
        f"{signal['adx']:.1f}\n"
        f"Room: "
        f"{signal['room_atr']:.2f} ATR\n"
        f"Extension: "
        f"{signal['extension_atr']:.2f} ATR\n"
        "\n"
        f"Signal ID: {sid}\n"
        "\n"
        "🤖 DEMO AUTO-TRADE"
    )


# ============================================================
# PLACE DEMO TRADE
# ============================================================

def execute_demo_trade(
    signal,
    sid,
):

    global active_trade
    global trade_count
    global last_trade_time

    if not AUTO_TRADE:
        return False

    if PRACTICE is not True:

        telegram(
            "🛑 TRADE BLOCKED\n"
            "PRACTICE safety lock failed."
        )

        return False

    if DEMO_ONLY_LOCK is not True:

        telegram(
            "🛑 TRADE BLOCKED\n"
            "DEMO_ONLY_LOCK failed."
        )

        return False

    if active_trade is not None:
        return False

    if (
        trade_count
        >= MAX_AUTO_TRADES
    ):

        return False

    if (
        time.time()
        - last_trade_time
        < TRADE_COOLDOWN
    ):

        return False

    asset = signal["asset"]

    direction = (
        signal["direction"]
        .lower()
    )

    try:

        # ----------------------------------------------------
        # VERIFY BINARY/TURBO ACTIVE
        # ----------------------------------------------------

        market_data = (
            iq.get_all_init_v2()
        )

        if not market_data:

            telegram(
                "🟠 DEMO TRADE SKIPPED\n"
                "Market initialization data "
                "is unavailable."
            )

            return False

        found = False

        for market_type in (
            "binary",
            "turbo",
        ):

            market = (
                market_data.get(
                    market_type,
                    {},
                )
            )

            if not isinstance(
                market,
                dict,
            ):

                continue

            actives = (
                market.get(
                    "actives",
                    {},
                )
            )

            if not isinstance(
                actives,
                dict,
            ):

                continue

            for active_id, info in (
                actives.items()
            ):

                if not isinstance(
                    info,
                    dict,
                ):

                    continue

                name = str(
                    info.get(
                        "name",
                        "",
                    )
                )

                if "." in name:

                    name = (
                        name.split(
                            ".",
                            1,
                        )[1]
                    )

                if (
                    name
                    == asset
                ):

                    enabled = info.get(
                        "enabled",
                        True,
                    )

                    suspended = info.get(
                        "is_suspended",
                        False,
                    )

                    if (
                        enabled
                        and not suspended
                    ):

                        found = True

                    break

        if not found:

            telegram(
                "⏭ DEMO TRADE SKIPPED\n"
                f"Asset: {asset}\n"
                "Asset is no longer confirmed "
                "as an open binary/turbo OTC."
            )

            return False

        # ----------------------------------------------------
        # PLACE ORDER
        # ----------------------------------------------------

        telegram(
            "🟡 PLACING DEMO TRADE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{direction.upper()}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: "
            f"{EXPIRY_MINUTES} minutes\n"
            f"Score: "
            f"{signal['score']}/100\n"
            f"Signal ID: {sid}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "PRACTICE ACCOUNT ONLY"
        )

        result = iq.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES,
        )

        status = False

        order_id = None

        if isinstance(
            result,
            tuple,
        ):

            if len(result) >= 2:

                status = bool(
                    result[0]
                )

                order_id = result[1]

        elif result:

            status = True

            order_id = result

        if not status:

            telegram(
                "🔴 DEMO ORDER FAILED\n"
                f"Asset: {asset}\n"
                f"Direction: "
                f"{direction.upper()}\n"
                f"Signal ID: {sid}\n"
                f"API response: {result}"
            )

            return False

        active_trade = {
            "order_id": order_id,
            "asset": asset,
            "direction":
                direction.upper(),
            "signal_id": sid,
            "opened_at":
                time.time(),
        }

        trade_count += 1

        last_trade_time = (
            time.time()
        )

        telegram(
            "🟢 DEMO TRADE OPENED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Trade #: "
            f"{trade_count}\n"
            f"Asset: {asset}\n"
            f"Direction: "
            f"{direction.upper()}\n"
            f"Stake: ${STAKE:.2f}\n"
            f"Expiry: "
            f"{EXPIRY_MINUTES} minutes\n"
            f"Signal ID: {sid}\n"
            f"Order ID: {order_id}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return True

    except Exception as exc:

        telegram(
            "🔴 DEMO TRADE ERROR\n"
            f"Asset: {asset}\n"
            f"Signal ID: {sid}\n"
            f"{type(exc).__name__}: {exc}"
        )

        return False


# ============================================================
# CHECK DEMO TRADE RESULT
# ============================================================

def monitor_trade():

    global active_trade
    global wins
    global losses
    global draws
    global total_profit
    global consecutive_losses

    if active_trade is None:
        return

    order_id = (
        active_trade[
            "order_id"
        ]
    )

    try:

        # ----------------------------------------------------
        # Preferred v3
        # ----------------------------------------------------

        if hasattr(
            iq,
            "check_win_v3",
        ):

            profit = (
                iq.check_win_v3(
                    order_id
                )
            )

        # ----------------------------------------------------
        # Fallback v2
        # ----------------------------------------------------

        elif hasattr(
            iq,
            "check_win_v2",
        ):

            profit = (
                iq.check_win_v2(
                    order_id,
                    3,
                )
            )

        # ----------------------------------------------------
        # Fallback
        # ----------------------------------------------------

        elif hasattr(
            iq,
            "check_win",
        ):

            profit = (
                iq.check_win(
                    order_id
                )
            )

        else:

            raise RuntimeError(
                "No supported result "
                "function found."
            )

        profit = safe_float(
            profit,
            0.0,
        )

        total_profit += profit

        if profit > 0:

            wins += 1

            consecutive_losses = 0

            result = "WIN 🟢"

        elif profit < 0:

            losses += 1

            consecutive_losses += 1

            result = "LOSS 🔴"

        else:

            draws += 1

            result = "DRAW 🟡"

        completed = (
            wins
            + losses
            + draws
        )

        if (
            wins + losses
            > 0
        ):

            win_rate = (
                wins
                / (
                    wins
                    + losses
                )
                * 100
            )

        else:

            win_rate = 0.0

        telegram(
            "📊 DEMO TRADE RESULT\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Result: {result}\n"
            f"Asset: "
            f"{active_trade['asset']}\n"
            f"Direction: "
            f"{active_trade['direction']}\n"
            f"Profit: "
            f"${profit:.2f}\n"
            f"Signal ID: "
            f"{active_trade['signal_id']}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Completed: "
            f"{completed}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Draws: {draws}\n"
            f"Win rate: "
            f"{win_rate:.2f}%\n"
            f"Net demo P/L: "
            f"${total_profit:.2f}"
        )

        active_trade = None

    except Exception as exc:

        print(
            traceback.format_exc()
        )

        telegram(
            "⚠️ TRADE RESULT ERROR\n"
            f"{type(exc).__name__}: {exc}"
        )

        # Do not immediately place another
        # trade while result state is unknown.
        time.sleep(10)


# ============================================================
# STATUS
# ============================================================

def send_status(
    assets
):

    global last_status_time

    if (
        time.time()
        - last_status_time
        < STATUS_INTERVAL
    ):

        return

    last_status_time = (
        time.time()
    )

    if (
        wins + losses
        > 0
    ):

        win_rate = (
            wins
            / (
                wins
                + losses
            )
            * 100
        )

    else:

        win_rate = 0.0

    runtime = int(
        time.time()
        - session_started
    )

    hours = (
        runtime // 3600
    )

    minutes = (
        runtime % 3600
    ) // 60

    telegram(
        "🟡 BACK TO TREND STATUS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC assets: "
        f"{len(assets)}\n"
        f"Trades: "
        f"{trade_count}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Win rate: "
        f"{win_rate:.2f}%\n"
        f"Net demo P/L: "
        f"${total_profit:.2f}\n"
        f"Consecutive losses: "
        f"{consecutive_losses}\n"
        f"Active trade: "
        f"{'YES' if active_trade else 'NO'}\n"
        f"Runtime: "
        f"{hours}h {minutes}m\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Account: PRACTICE\n"
        "Auto-trading: ON\n"
        "Stake: $1\n"
        "Expiry: 3 minutes"
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def poll_telegram():

    global telegram_offset

    if not TELEGRAM_TOKEN:
        return

    try:

        params = {
            "timeout": 1,
        }

        if telegram_offset is not None:

            params["offset"] = (
                telegram_offset
            )

        response = requests.get(
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_TOKEN}/getUpdates"
            ),
            params=params,
            timeout=5,
        )

        data = response.json()

        if not data.get(
            "ok",
            False,
        ):

            return

        for update in data.get(
            "result",
            [],
        ):

            telegram_offset = (
                update["update_id"]
                + 1
            )

            message = update.get(
                "message",
                {},
            )

            text = message.get(
                "text",
                "",
            )

            if text == "/stats":

                if (
                    wins + losses
                    > 0
                ):

                    rate = (
                        wins
                        / (
                            wins
                            + losses
                        )
                        * 100
                    )

                else:

                    rate = 0.0

                telegram(
                    "📊 DEMO STATISTICS\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Trades: "
                    f"{trade_count}\n"
                    f"Wins: {wins}\n"
                    f"Losses: {losses}\n"
                    f"Draws: {draws}\n"
                    f"Win rate: "
                    f"{rate:.2f}%\n"
                    f"Net P/L: "
                    f"${total_profit:.2f}\n"
                    f"Active trade: "
                    f"{'YES' if active_trade else 'NO'}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "PRACTICE ACCOUNT"
                )

            elif text == "/scan":

                telegram(
                    "🔎 SCANNER ACTIVE\n"
                    "30M Trend → 5M Pullback "
                    "→ 1M Entry\n"
                    "Expiry: 3 minutes\n"
                    "Auto-trade: PRACTICE only"
                )

            elif text == "/help":

                telegram(
                    "🤖 COMMANDS\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "/stats\n"
                    "/scan\n"
                    "/help\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Automatic trading is locked "
                    "to PRACTICE."
                )

    except Exception:
        pass


# ============================================================
# SESSION
# ============================================================

def run_session():

    global session_started
    global last_status_time

    session_started = (
        time.time()
    )

    last_status_time = 0

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    if not connect():

        time.sleep(
            RECONNECT_INTERVAL
        )

        return

    telegram(
        "🟡 BACK TO TREND SCANNER ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: Back to Trend\n"
        "Context: 30M\n"
        "Pullback: 5M\n"
        "Entry: 1M\n"
        "Expiry: 3 minutes\n"
        "Scan interval: 60 seconds\n"
        "Automatic demo trading: ON\n"
        "Stake: $1.00\n"
        "Max trades/session: 30\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "PRACTICE ACCOUNT ONLY"
    )

    while (
        time.time()
        - session_started
        < MAX_RUNTIME
    ):

        try:

            poll_telegram()

            # ------------------------------------------------
            # ACTIVE TRADE
            # ------------------------------------------------

            if active_trade is not None:

                monitor_trade()

                time.sleep(5)

                continue

            # ------------------------------------------------
            # STOP AFTER 4 CONSECUTIVE LOSSES
            # ------------------------------------------------

            if (
                consecutive_losses
                >= MAX_CONSECUTIVE_LOSSES
            ):

                telegram(
                    "🛑 DEMO AUTO-TRADING STOPPED\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Consecutive losses: "
                    f"{consecutive_losses}\n"
                    f"Limit: "
                    f"{MAX_CONSECUTIVE_LOSSES}\n"
                    "\n"
                    "No more automatic trades "
                    "will be placed this session."
                )

                while True:

                    poll_telegram()

                    time.sleep(30)

            # ------------------------------------------------
            # STOP AFTER 30 TRADES
            # ------------------------------------------------

            if (
                trade_count
                >= MAX_AUTO_TRADES
            ):

                if (
                    wins + losses
                    > 0
                ):

                    final_rate = (
                        wins
                        / (
                            wins
                            + losses
                        )
                        * 100
                    )

                else:

                    final_rate = 0.0

                telegram(
                    "🏁 30-TRADE DEMO TEST COMPLETE\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Trades: "
                    f"{trade_count}\n"
                    f"Wins: {wins}\n"
                    f"Losses: {losses}\n"
                    f"Draws: {draws}\n"
                    f"Win rate: "
                    f"{final_rate:.2f}%\n"
                    f"Net demo P/L: "
                    f"${total_profit:.2f}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "PRACTICE ACCOUNT ONLY"
                )

                while True:

                    poll_telegram()

                    time.sleep(60)

            # ------------------------------------------------
            # DISCOVER OTC
            # ------------------------------------------------

            assets = (
                discover_otc_assets()
            )

            if not assets:

                telegram(
                    "🟠 NO TRADABLE OTC ASSETS FOUND\n"
                    "Scanner remains active.\n"
                    "Retrying automatically."
                )

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            # ------------------------------------------------
            # STATUS
            # ------------------------------------------------

            send_status(
                assets
            )

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            for asset in assets:

                try:

                    signal = (
                        evaluate_asset(
                            asset
                        )
                    )

                    if not signal:
                        continue

                    if (
                        signal["score"]
                        < MIN_SCORE
                    ):

                        continue

                    # ------------------------------------------------
                    # PREVENT DUPLICATE SIGNALS
                    # ------------------------------------------------

                    candle_slot = int(
                        server_now()
                        / TF_1M
                    )

                    key = (
                        signal["asset"],
                        signal["direction"],
                        candle_slot,
                    )

                    if (
                        key
                        in seen_signal_keys
                    ):

                        continue

                    seen_signal_keys.add(
                        key
                    )

                    sid = (
                        make_signal_id(
                            signal
                        )
                    )

                    send_signal(
                        signal,
                        sid,
                    )

                    # ------------------------------------------------
                    # AUTOMATIC DEMO TRADE
                    # ------------------------------------------------

                    placed = (
                        execute_demo_trade(
                            signal,
                            sid,
                        )
                    )

                    if placed:

                        break

                except Exception as exc:

                    print(
                        f"Asset error "
                        f"{asset}: {exc}"
                    )

            if len(
                seen_signal_keys
            ) > 500:

                seen_signal_keys.clear()

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            telegram(
                "🛑 BACK TO TREND STOPPED"
            )

            return

        except Exception as exc:

            print(
                traceback.format_exc()
            )

            telegram(
                "⚠️ SCANNER ERROR\n"
                f"{type(exc).__name__}: {exc}\n"
                "Recovering automatically..."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# CONNECT
# ============================================================

def connect():

    global iq

    if not IQ_EMAIL:

        telegram(
            "🔴 IQ_EMAIL is missing."
        )

        return False

    if not IQ_PASSWORD:

        telegram(
            "🔴 IQ_PASSWORD is missing."
        )

        return False

    # --------------------------------------------------------
    # HARD DEMO SAFETY
    # --------------------------------------------------------

    if PRACTICE is not True:

        telegram(
            "🛑 START BLOCKED\n"
            "PRACTICE must be True."
        )

        return False

    if DEMO_ONLY_LOCK is not True:

        telegram(
            "🛑 START BLOCKED\n"
            "DEMO_ONLY_LOCK must be True."
        )

        return False

    try:

        iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        connected = (
            iq.connect()
        )

        if not connected:

            telegram(
                "🔴 IQ OPTION CONNECTION FAILED"
            )

            return False

        if not iq.check_connect():

            telegram(
                "🔴 IQ OPTION LOGIN FAILED"
            )

            return False

        # ----------------------------------------------------
        # FORCE PRACTICE
        # ----------------------------------------------------

        iq.change_balance(
            "PRACTICE"
        )

        time.sleep(2)

        telegram(
            "🟢 IQ OPTION CONNECTED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Account: PRACTICE\n"
            "Automatic trading: ENABLED\n"
            "Stake: $1.00\n"
            "Expiry: 3 minutes\n"
            "Mode: DEMO TEST ONLY\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        return True

    except Exception as exc:

        telegram(
            "🔴 CONNECTION ERROR\n"
            f"{type(exc).__name__}: {exc}"
        )

        return False


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # ABSOLUTE SAFETY CHECK
    # --------------------------------------------------------

    if PRACTICE is not True:

        print(
            "FATAL: PRACTICE must be True."
        )

        return

    if DEMO_ONLY_LOCK is not True:

        print(
            "FATAL: DEMO_ONLY_LOCK must be True."
        )

        return

    telegram(
        "🚀 BACK TO TREND V2 STARTING\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "30M Trend → 5M Pullback → 1M Entry\n"
        "Expiry: 3 minutes\n"
        "Stake: $1\n"
        "Automatic demo trading: ON\n"
        "Account: PRACTICE\n"
        "Real account: HARD BLOCKED\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    while True:

        try:

            run_session()

        except KeyboardInterrupt:

            telegram(
                "🛑 BOT STOPPED"
            )

            break

        except Exception as exc:

            print(
                traceback.format_exc()
            )

            telegram(
                "🔴 SESSION CRASH\n"
                f"{type(exc).__name__}: {exc}\n"
                "Restarting automatically..."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
