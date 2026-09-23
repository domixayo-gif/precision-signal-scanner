import os
import time
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# BACK TO TREND — IQ OPTION OTC SCANNER
# ============================================================
# READ-ONLY
# IQ OPTION OTC
# 5M TREND + 1M ENTRY
# 5-MINUTE REFERENCE EXPIRY
#
# RUNS CONTINUOUSLY FOR UP TO 24 HOURS
#
# IMPORTANT:
# - No automatic trading
# - Signals only
# - Scans every 1 minute
# - Status message every 5 minutes
# ============================================================


# ============================================================
# ENVIRONMENT
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# SETTINGS
# ============================================================

PRACTICE = True

MAX_OTC_ASSETS = 60

CANDLES_5M = 160
CANDLES_1M = 160

TF_5M = 300
TF_1M = 60

EXPIRY_MINUTES = 5

# Scan every minute.
SCAN_INTERVAL = 60

# Send scanner heartbeat every 5 minutes.
STATUS_INTERVAL = 300

# Maximum runtime: 24 hours.
MAX_RUNTIME = 24 * 60 * 60

EMA_FAST = 20
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14

PULLBACK_ATR = 0.45
MIN_ROOM_ATR = 0.80

SWING_LOOKBACK = 25

MAX_TRIGGER_RANGE_ATR = 1.80


# ============================================================
# RUNTIME
# ============================================================

iq = None

last_signal_candle = {}

start_time = time.time()

last_status_time = 0


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    if not TELEGRAM_TOKEN:
        return False

    if not TELEGRAM_CHAT_ID:
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        return response.ok

    except Exception:

        return False


# ============================================================
# MATH
# ============================================================

def safe_float(value, default=0.0):

    try:
        return float(value)

    except Exception:
        return default


def mean(values):

    if not values:
        return 0.0

    return sum(values) / len(values)


def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1)

    result = mean(
        values[:period]
    )

    for price in values[period:]:

        result = (
            (price - result)
            * multiplier
            + result
        )

    return result


def rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(
        1,
        period + 1
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

    avg_gain = mean(gains)
    avg_loss = mean(losses)

    for i in range(
        period + 1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gain
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + loss
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (
        100.0 / (1.0 + rs)
    )


def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        high = safe_float(
            current["max"]
        )

        low = safe_float(
            current["min"]
        )

        previous_close = safe_float(
            previous["close"]
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
            )
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    return mean(
        true_ranges[-period:]
    )


def macd(values):

    if len(values) < 35:
        return None, None, None

    ema12 = ema(
        values,
        12
    )

    ema26 = ema(
        values,
        26
    )

    if (
        ema12 is None
        or
        ema26 is None
    ):
        return None, None, None

    macd_line = (
        ema12 - ema26
    )

    signal_line = macd_line

    histogram = (
        macd_line
        - signal_line
    )

    return (
        macd_line,
        signal_line,
        histogram
    )


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_range(c):

    return (
        safe_float(c["max"])
        -
        safe_float(c["min"])
    )


def candle_body(c):

    return abs(
        safe_float(c["close"])
        -
        safe_float(c["open"])
    )


def bullish(c):

    return (
        safe_float(c["close"])
        >
        safe_float(c["open"])
    )


def bearish(c):

    return (
        safe_float(c["close"])
        <
        safe_float(c["open"])
    )


# ============================================================
# PATTERNS
# ============================================================

def bullish_engulfing(
    previous,
    current
):

    if not bearish(previous):
        return False

    if not bullish(current):
        return False

    prev_open = safe_float(
        previous["open"]
    )

    prev_close = safe_float(
        previous["close"]
    )

    curr_open = safe_float(
        current["open"]
    )

    curr_close = safe_float(
        current["close"]
    )

    return (
        curr_open <= prev_close
        and
        curr_close >= prev_open
        and
        candle_body(current)
        >= candle_body(previous)
    )


def bearish_engulfing(
    previous,
    current
):

    if not bullish(previous):
        return False

    if not bearish(current):
        return False

    prev_open = safe_float(
        previous["open"]
    )

    prev_close = safe_float(
        previous["close"]
    )

    curr_open = safe_float(
        current["open"]
    )

    curr_close = safe_float(
        current["close"]
    )

    return (
        curr_open >= prev_close
        and
        curr_close <= prev_open
        and
        candle_body(current)
        >= candle_body(previous)
    )


def bullish_pinbar(c):

    high = safe_float(
        c["max"]
    )

    low = safe_float(
        c["min"]
    )

    open_price = safe_float(
        c["open"]
    )

    close = safe_float(
        c["close"]
    )

    body = abs(
        close - open_price
    )

    if body <= 0:
        return False

    lower_wick = (
        min(
            open_price,
            close
        )
        - low
    )

    upper_wick = (
        high
        -
        max(
            open_price,
            close
        )
    )

    return (
        lower_wick >= body * 2
        and
        lower_wick > upper_wick
        and
        close >= open_price
    )


def bearish_pinbar(c):

    high = safe_float(
        c["max"]
    )

    low = safe_float(
        c["min"]
    )

    open_price = safe_float(
        c["open"]
    )

    close = safe_float(
        c["close"]
    )

    body = abs(
        close - open_price
    )

    if body <= 0:
        return False

    lower_wick = (
        min(
            open_price,
            close
        )
        - low
    )

    upper_wick = (
        high
        -
        max(
            open_price,
            close
        )
    )

    return (
        upper_wick >= body * 2
        and
        upper_wick > lower_wick
        and
        close <= open_price
    )


# ============================================================
# STRUCTURE
# ============================================================

def structure_levels(candles):

    recent = candles[
        -SWING_LOOKBACK:
    ]

    support = min(
        safe_float(c["min"])
        for c in recent
    )

    resistance = max(
        safe_float(c["max"])
        for c in recent
    )

    return support, resistance


# ============================================================
# PULLBACK
# ============================================================

def bullish_pullback(
    candle,
    ema20,
    ema50,
    atr_value
):

    if (
        atr_value is None
        or
        atr_value <= 0
    ):
        return False

    low = safe_float(
        candle["min"]
    )

    high = safe_float(
        candle["max"]
    )

    zone_low = (
        min(
            ema20,
            ema50
        )
        -
        atr_value * PULLBACK_ATR
    )

    zone_high = (
        max(
            ema20,
            ema50
        )
        +
        atr_value * PULLBACK_ATR
    )

    return (
        high >= zone_low
        and
        low <= zone_high
    )


def bearish_pullback(
    candle,
    ema20,
    ema50,
    atr_value
):

    if (
        atr_value is None
        or
        atr_value <= 0
    ):
        return False

    low = safe_float(
        candle["min"]
    )

    high = safe_float(
        candle["max"]
    )

    zone_low = (
        min(
            ema20,
            ema50
        )
        -
        atr_value * PULLBACK_ATR
    )

    zone_high = (
        max(
            ema20,
            ema50
        )
        +
        atr_value * PULLBACK_ATR
    )

    return (
        high >= zone_low
        and
        low <= zone_high
    )


# ============================================================
# OTC DISCOVERY
# ============================================================

def discover_otc_assets():

    found = set()

    def walk(obj):

        if len(found) >= MAX_OTC_ASSETS:
            return

        if isinstance(
            obj,
            dict
        ):

            for key, value in (
                obj.items()
            ):

                key_string = str(key)

                if (
                    "-OTC"
                    in
                    key_string.upper()
                ):

                    found.add(
                        key_string
                    )

                    if (
                        len(found)
                        >= MAX_OTC_ASSETS
                    ):
                        return

                walk(value)

        elif isinstance(
            obj,
            list
        ):

            for item in obj:

                walk(item)

                if (
                    len(found)
                    >= MAX_OTC_ASSETS
                ):
                    return

        elif isinstance(
            obj,
            str
        ):

            if (
                "-OTC"
                in
                obj.upper()
            ):

                found.add(obj)

    try:

        if hasattr(
            iq,
            "get_all_init_v2"
        ):

            data = (
                iq.get_all_init_v2()
            )

            walk(data)

    except Exception:
        pass

    if not found:

        try:

            data = (
                iq.get_all_open_time()
            )

            walk(data)

        except Exception:
            pass

    return sorted(found)[
        :MAX_OTC_ASSETS
    ]


# ============================================================
# CLOSED CANDLES
# ============================================================

def get_closed_candles(
    asset,
    timeframe,
    count
):

    now = int(time.time())

    candles = iq.get_candles(
        asset,
        timeframe,
        count,
        now
    )

    if not candles:
        return []

    current_bucket = (
        now // timeframe
    ) * timeframe

    normalized = []

    for candle in candles:

        try:

            candle_time = int(
                candle.get(
                    "from",
                    0
                )
            )

            if (
                candle_time
                >=
                current_bucket
            ):
                continue

            normalized.append({

                "from": candle_time,

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

                "min": safe_float(
                    candle.get(
                        "min"
                    )
                ),

                "max": safe_float(
                    candle.get(
                        "max"
                    )
                ),
            })

        except Exception:
            continue

    normalized.sort(
        key=lambda x: x["from"]
    )

    return normalized


# ============================================================
# 5M TREND
# ============================================================

def analyze_5m(candles):

    if len(candles) < (
        EMA_SLOW + 10
    ):
        return None

    closes = [
        safe_float(
            c["close"]
        )
        for c in candles
    ]

    current_close = closes[-1]

    ema20 = ema(
        closes,
        EMA_FAST
    )

    ema50 = ema(
        closes,
        EMA_SLOW
    )

    if (
        ema20 is None
        or
        ema50 is None
    ):
        return None

    previous_ema20 = ema(
        closes[:-3],
        EMA_FAST
    )

    if previous_ema20 is None:
        return None

    rsi_value = rsi(
        closes,
        RSI_PERIOD
    )

    _, _, macd_hist = macd(
        closes
    )

    if (
        ema20 > ema50
        and
        ema20 > previous_ema20
        and
        current_close >= ema20
    ):

        trend = "BULLISH"

    elif (
        ema20 < ema50
        and
        ema20 < previous_ema20
        and
        current_close <= ema20
    ):

        trend = "BEARISH"

    else:

        trend = "NEUTRAL"

    return {

        "trend": trend,

        "ema20": ema20,

        "ema50": ema50,

        "rsi": rsi_value,

        "macd_hist": macd_hist,
    }


# ============================================================
# BACK TO TREND
# ============================================================

def evaluate_back_to_trend(
    candles_1m,
    context_5m
):

    if not context_5m:
        return None

    if (
        context_5m["trend"]
        == "NEUTRAL"
    ):
        return None

    if len(candles_1m) < (
        EMA_SLOW + 10
    ):
        return None

    closes = [
        safe_float(
            c["close"]
        )
        for c in candles_1m
    ]

    ema20 = ema(
        closes,
        EMA_FAST
    )

    ema50 = ema(
        closes,
        EMA_SLOW
    )

    atr_value = atr(
        candles_1m,
        ATR_PERIOD
    )

    if (
        ema20 is None
        or
        ema50 is None
        or
        atr_value is None
        or
        atr_value <= 0
    ):
        return None

    current = candles_1m[-1]
    previous = candles_1m[-2]

    current_close = safe_float(
        current["close"]
    )

    current_low = safe_float(
        current["min"]
    )

    current_high = safe_float(
        current["max"]
    )

    support, resistance = (
        structure_levels(
            candles_1m
        )
    )

    current_range = (
        candle_range(current)
    )

    if current_range > (
        atr_value
        * MAX_TRIGGER_RANGE_ATR
    ):
        return None

    trend = context_5m["trend"]

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if trend == "BULLISH":

        pullback = (
            bullish_pullback(
                current,
                ema20,
                ema50,
                atr_value
            )
        )

        near_support = (
            abs(
                current_low
                - support
            )
            <=
            atr_value * 0.60
        )

        trigger = (
            bullish_engulfing(
                previous,
                current
            )
            or
            bullish_pinbar(
                current
            )
        )

        room = (
            resistance
            - current_close
        )

        room_atr = (
            room / atr_value
        )

        if not (
            pullback
            and
            near_support
            and
            trigger
            and
            room_atr >= MIN_ROOM_ATR
        ):
            return None

        score = 75

        rsi_value = (
            context_5m["rsi"]
        )

        macd_hist = (
            context_5m["macd_hist"]
        )

        if (
            rsi_value is not None
            and
            50 <= rsi_value <= 70
        ):
            score += 10

        if (
            macd_hist is not None
            and
            macd_hist >= 0
        ):
            score += 5

        if room_atr >= 1.20:
            score += 10

        return {

            "direction": "CALL",

            "entry": current_close,

            "room_atr": room_atr,

            "score": min(
                score,
                100
            ),

            "candle_time": (
                current["from"]
            ),
        }


    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    if trend == "BEARISH":

        pullback = (
            bearish_pullback(
                current,
                ema20,
                ema50,
                atr_value
            )
        )

        near_resistance = (
            abs(
                current_high
                - resistance
            )
            <=
            atr_value * 0.60
        )

        trigger = (
            bearish_engulfing(
                previous,
                current
            )
            or
            bearish_pinbar(
                current
            )
        )

        room = (
            current_close
            - support
        )

        room_atr = (
            room / atr_value
        )

        if not (
            pullback
            and
            near_resistance
            and
            trigger
            and
            room_atr >= MIN_ROOM_ATR
        ):
            return None

        score = 75

        rsi_value = (
            context_5m["rsi"]
        )

        macd_hist = (
            context_5m["macd_hist"]
        )

        if (
            rsi_value is not None
            and
            30 <= rsi_value <= 50
        ):
            score += 10

        if (
            macd_hist is not None
            and
            macd_hist <= 0
        ):
            score += 5

        if room_atr >= 1.20:
            score += 10

        return {

            "direction": "PUT",

            "entry": current_close,

            "room_atr": room_atr,

            "score": min(
                score,
                100
            ),

            "candle_time": (
                current["from"]
            ),
        }

    return None


# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(
    asset,
    setup,
    context
):

    direction = setup[
        "direction"
    ]

    candle_time = (
        datetime.fromtimestamp(
            setup["candle_time"],
            timezone.utc
        )
    )

    clean_asset = (
        asset
        .replace("/", "")
        .replace("-", "")
        .replace(" ", "")
    )

    signal_id = (
        clean_asset
        + "-"
        + direction
        + "-"
        + candle_time.strftime(
            "%H%M%S"
        )
    )

    rsi_value = context["rsi"]

    rsi_text = (
        f"{rsi_value:.1f}"
        if rsi_value is not None
        else "N/A"
    )

    macd_value = (
        context["macd_hist"]
    )

    macd_text = (
        f"{macd_value:.6f}"
        if macd_value is not None
        else "N/A"
    )

    message = (
        "🔴 <b>NEW BACK TO TREND SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset:</b> {asset}\n"
        f"<b>Direction:</b> "
        f"<b>{direction}</b>\n"
        f"<b>Score:</b> "
        f"<b>{setup['score']}/100</b>\n"
        f"<b>Reference expiry:</b> "
        f"{EXPIRY_MINUTES} minutes\n"
        f"<b>5M Trend:</b> "
        f"{context['trend']}\n"
        "<b>1M Setup:</b> "
        "Trend Pullback + Trigger\n"
        f"<b>5M RSI:</b> "
        f"{rsi_text}\n"
        f"<b>MACD Hist:</b> "
        f"{macd_text}\n"
        f"<b>Entry:</b> "
        f"{setup['entry']}\n"
        f"<b>Room:</b> "
        f"{setup['room_atr']:.2f} ATR\n"
        f"<b>Signal candle:</b> "
        f"{candle_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"<b>Signal ID:</b> "
        f"{signal_id}\n"
        "<b>Mode:</b> READ-ONLY\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    telegram(message)

    return signal_id


# ============================================================
# SCAN ONE ASSET
# ============================================================

def scan_asset(asset):

    try:

        candles_5m = (
            get_closed_candles(
                asset,
                TF_5M,
                CANDLES_5M
            )
        )

        if len(candles_5m) < (
            EMA_SLOW + 10
        ):
            return None

        context = analyze_5m(
            candles_5m
        )

        if not context:
            return None

        if (
            context["trend"]
            == "NEUTRAL"
        ):
            return None

        candles_1m = (
            get_closed_candles(
                asset,
                TF_1M,
                CANDLES_1M
            )
        )

        if len(candles_1m) < (
            EMA_SLOW + 10
        ):
            return None

        setup = (
            evaluate_back_to_trend(
                candles_1m,
                context
            )
        )

        if not setup:
            return None

        candle_time = (
            setup["candle_time"]
        )

        if (
            last_signal_candle.get(
                asset
            )
            ==
            candle_time
        ):
            return None

        last_signal_candle[
            asset
        ] = candle_time

        return send_signal(
            asset,
            setup,
            context
        )

    except Exception:

        return None


# ============================================================
# CONNECT
# ============================================================

def connect():

    global iq

    iq = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    connected, reason = (
        iq.connect()
    )

    if not connected:

        raise RuntimeError(
            f"Connection failed: {reason}"
        )

    if PRACTICE:

        iq.change_balance(
            "PRACTICE"
        )

    else:

        iq.change_balance(
            "REAL"
        )

    return True


# ============================================================
# STATUS
# ============================================================

def send_status(
    cycle,
    assets_count,
    signals
):

    elapsed = (
        time.time()
        - start_time
    )

    hours = int(
        elapsed // 3600
    )

    minutes = int(
        (elapsed % 3600)
        // 60
    )

    telegram(
        "🟢 <b>BACK TO TREND SCANNER ALIVE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Cycle:</b> {cycle}\n"
        f"<b>OTC assets:</b> {assets_count}\n"
        f"<b>Signals this cycle:</b> {signals}\n"
        f"<b>Runtime:</b> "
        f"{hours}h {minutes}m\n"
        "<b>Context:</b> 5M\n"
        "<b>Entry:</b> 1M\n"
        "<b>Expiry:</b> 5 minutes\n"
        "<b>Mode:</b> READ-ONLY\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Next status:</b> 5 minutes"
    )


# ============================================================
# MAIN 24-HOUR LOOP
# ============================================================

def main():

    global iq
    global last_status_time

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    try:

        connect()

    except Exception as e:

        telegram(
            "🔴 <b>IQ OPTION CONNECTION FAILED</b>\n"
            f"<code>{str(e)[:500]}</code>"
        )

        raise


    # --------------------------------------------------------
    # ONLINE MESSAGE
    # --------------------------------------------------------

    telegram(
        "🟡 <b>BACK TO TREND SCANNER ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "IQ Option OTC connection: <b>OK</b>\n"
        "Strategy: <b>Back to Trend</b>\n"
        "Context: <b>5M</b>\n"
        "Entry: <b>1M</b>\n"
        "Expiry: <b>5 minutes</b>\n"
        "Scan interval: <b>1 minute</b>\n"
        "Status interval: <b>5 minutes</b>\n"
        "Maximum runtime: <b>24 hours</b>\n"
        "Mode: <b>READ-ONLY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Scanner will run continuously."
    )


    # --------------------------------------------------------
    # START TIMER
    # --------------------------------------------------------

    run_start = time.time()

    cycle = 0

    last_status_time = run_start


    # --------------------------------------------------------
    # CONTINUOUS 24-HOUR LOOP
    # --------------------------------------------------------

    while (
        time.time()
        - run_start
        <
        MAX_RUNTIME
    ):

        cycle += 1

        cycle_start = time.time()

        signals = 0

        # ----------------------------------------------------
        # CONNECTION CHECK
        # ----------------------------------------------------

        try:

            if not iq.check_connect():

                telegram(
                    "🟠 <b>IQ OPTION DISCONNECTED</b>\n"
                    "Reconnecting..."
                )

                connect()

        except Exception:

            try:

                connect()

            except Exception:

                time.sleep(30)

                continue


        # ----------------------------------------------------
        # DISCOVER OTC
        # ----------------------------------------------------

        try:

            assets = (
                discover_otc_assets()
            )

        except Exception:

            assets = []


        # ----------------------------------------------------
        # 5-MINUTE STATUS
        # ----------------------------------------------------

        if (
            time.time()
            - last_status_time
            >= STATUS_INTERVAL
        ):

            send_status(
                cycle,
                len(assets),
                0
            )

            last_status_time = (
                time.time()
            )


        # ----------------------------------------------------
        # SCAN OTC ASSETS
        # ----------------------------------------------------

        for asset in assets:

            # Stop scanning if 24 hours
            # have been reached.

            if (
                time.time()
                - run_start
                >= MAX_RUNTIME
            ):
                break

            try:

                result = scan_asset(
                    asset
                )

                if result:

                    signals += 1

            except Exception:

                pass

            # Prevent hammering the API.

            time.sleep(0.10)


        # ----------------------------------------------------
        # NO TRADE
        # ----------------------------------------------------

        if assets and signals == 0:

            telegram(
                "⚪ <b>NO TRADE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"<b>Cycle:</b> {cycle}\n"
                f"<b>OTC checked:</b> "
                f"{len(assets)}\n"
                "<b>Qualified signals:</b> 0\n"
                "No Back to Trend setup "
                "qualified on the latest "
                "closed 1M candles.\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Scanner remains active."
            )


        # ----------------------------------------------------
        # KEEP THE 1-MINUTE SCAN CYCLE
        # ----------------------------------------------------

        elapsed = (
            time.time()
            - cycle_start
        )

        remaining = (
            SCAN_INTERVAL
            - elapsed
        )

        if remaining > 0:

            # Sleep in small chunks so the
            # 24-hour timer is always checked.

            end_sleep = (
                time.time()
                + remaining
            )

            while (
                time.time()
                < end_sleep
            ):

                if (
                    time.time()
                    - run_start
                    >= MAX_RUNTIME
                ):
                    break

                time.sleep(
                    min(
                        5,
                        end_sleep
                        - time.time()
                    )
                )


    # --------------------------------------------------------
    # 24 HOURS COMPLETED
    # --------------------------------------------------------

    telegram(
        "🏁 <b>24-HOUR SCANNER SESSION COMPLETE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Back to Trend scanner has completed "
        "its 24-hour runtime.\n"
        "<b>Mode:</b> READ-ONLY\n"
        "The GitHub Actions job will finish "
        "normally.\n"
        "━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# AUTO RESTART INSIDE SAME JOB
# ============================================================

if __name__ == "__main__":

    while True:

        try:

            main()

            # main() completed its 24-hour session.
            # Exit normally so GitHub Actions can
            # finish the job.

            break

        except KeyboardInterrupt:

            break

        except Exception as e:

            try:

                telegram(
                    "🔴 <b>SCANNER ERROR</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{str(e)[:500]}</code>\n"
                    "Restarting scanner in 30 seconds."
                )

            except Exception:
                pass

            time.sleep(30)
