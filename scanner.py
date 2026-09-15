import asyncio
import os
import time
from datetime import datetime, timezone

import requests

from storage import load_tracker, update_tracker

try:
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
except Exception:
    PocketOptionAsync = None


TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

COINBASE = "https://api.exchange.coinbase.com"
TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "ADA", "TRX", "LINK",
    "TON", "AVAX", "DOGE", "DOT", "LTC", "POL",
]

EXPIRY_MINUTES = int(os.getenv("EXPIRY_MINUTES", "5"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "20"))

PO_LOOKBACK_CANDLES = int(os.getenv("PO_LOOKBACK_CANDLES", "4"))

PO_SYMBOL_CANDIDATES = {
    "BTC": ["BTCUSD_otc"],
    "ETH": ["ETHUSD_otc"],
    "SOL": ["SOLUSD_otc"],
    "BNB": ["BNBUSD_otc"],
    "ADA": ["ADAUSD_otc"],
    "TRX": ["TRXUSD_otc"],
    "LINK": ["LINUSD_otc", "LINKUSD_otc"],
    "TON": ["TONUSD_otc"],
    "AVAX": ["AVAUSD_otc", "AVAXUSD_otc"],
    "DOGE": ["DOGUSD_otc", "DOGEUSD_otc"],
    "DOT": ["DOTUSD_otc"],
    "LTC": ["LTCUSD_otc"],
    "POL": ["POLUSD_otc", "MATICUSD_otc"],
}


def tg(text):
    response = requests.post(
        f"{TG}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
        },
        timeout=20,
    )
    response.raise_for_status()


def candles(symbol, seconds, start=None, end=None):
    params = {
        "granularity": seconds,
    }

    if start is not None:
        params["start"] = datetime.fromtimestamp(
            start,
            timezone.utc,
        ).isoformat()

    if end is not None:
        params["end"] = datetime.fromtimestamp(
            end,
            timezone.utc,
        ).isoformat()

    try:
        response = requests.get(
            f"{COINBASE}/products/{symbol}-USD/candles",
            params=params,
            headers={
                "User-Agent": "precision-signal-scanner/v3"
            },
            timeout=20,
        )
    except requests.RequestException:
        return []

    if response.status_code != 200:
        return []

    try:
        rows = response.json()
    except ValueError:
        return []

    rows.sort(key=lambda row: row[0])

    # Ignore the currently forming candle.
    return rows[:-1]


def closes(rows):
    return [float(row[4]) for row in rows]


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (
            price * multiplier
            + value * (1 - multiplier)
        )

    return value


def ema_series(values, period):
    if len(values) < period:
        return []

    output = [None] * (period - 1)

    multiplier = 2 / (period + 1)

    value = sum(values[:period]) / period

    output.append(value)

    for price in values[period:]:
        value = (
            price * multiplier
            + value * (1 - multiplier)
        )
        output.append(value)

    return output


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for old, new in zip(
        values[-period - 1:-1],
        values[-period:],
    ):
        change = new - old

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0

    return 100 - (
        100 / (
            1 + average_gain / average_loss
        )
    )


def macd_values(values):
    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)

    if not ema12 or not ema26:
        return None

    macd_line = []

    for fast, slow in zip(ema12, ema26):
        if fast is not None and slow is not None:
            macd_line.append(fast - slow)

    if len(macd_line) < 10:
        return None

    signal_line = ema_series(macd_line, 9)

    if not signal_line:
        return None

    if signal_line[-1] is None or signal_line[-2] is None:
        return None

    line = macd_line[-1]
    previous_line = macd_line[-2]

    signal = signal_line[-1]
    previous_signal = signal_line[-2]

    histogram = line - signal
    previous_histogram = previous_line - previous_signal

    return (
        line,
        signal,
        histogram,
        previous_histogram,
    )


def true_ranges(rows):
    if len(rows) < 2:
        return []

    output = []

    for index in range(1, len(rows)):
        high = float(rows[index][2])
        low = float(rows[index][1])
        previous_close = float(rows[index - 1][4])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        output.append(tr)

    return output


def atr(rows, period=14):
    values = true_ranges(rows)

    if len(values) < period:
        return None

    return sum(values[-period:]) / period


def atr_series(rows, period=14):
    values = true_ranges(rows)

    if len(values) < period:
        return []

    output = []

    for index in range(
        period,
        len(values) + 1,
    ):
        output.append(
            sum(
                values[index - period:index]
            ) / period
        )

    return output


def adx_dmi(rows, period=14):
    if len(rows) < period * 2 + 2:
        return None

    tr_values = []
    plus_dm = []
    minus_dm = []

    for index in range(1, len(rows)):
        high = float(rows[index][2])
        low = float(rows[index][1])

        previous_high = float(rows[index - 1][2])
        previous_low = float(rows[index - 1][1])
        previous_close = float(rows[index - 1][4])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        up_move = high - previous_high
        down_move = previous_low - low

        plus = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        tr_values.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    dx_values = []

    for index in range(
        period - 1,
        len(tr_values),
    ):
        tr_sum = sum(
            tr_values[
                index - period + 1:index + 1
            ]
        )

        if tr_sum == 0:
            continue

        plus_di = (
            100
            * sum(
                plus_dm[
                    index - period + 1:index + 1
                ]
            )
            / tr_sum
        )

        minus_di = (
            100
            * sum(
                minus_dm[
                    index - period + 1:index + 1
                ]
            )
            / tr_sum
        )

        denominator = plus_di + minus_di

        if denominator == 0:
            dx = 0
        else:
            dx = (
                100
                * abs(plus_di - minus_di)
                / denominator
            )

        dx_values.append(
            (plus_di, minus_di, dx)
        )

    if len(dx_values) < period:
        return None

    adx = sum(
        value[2]
        for value in dx_values[-period:]
    ) / period

    plus_last = dx_values[-1][0]
    minus_last = dx_values[-1][1]

    return (
        adx,
        plus_last,
        minus_last,
    )


def candle_info(rows):
    current = rows[-1]

    open_price = float(current[3])
    high = float(current[2])
    low = float(current[1])
    close = float(current[4])

    body = abs(close - open_price)

    candle_range = max(
        high - low,
        1e-12,
    )

    upper_wick = (
        high - max(open_price, close)
    )

    lower_wick = (
        min(open_price, close) - low
    )

    bullish = close > open_price
    bearish = close < open_price

    previous = rows[-2]

    previous_open = float(previous[3])
    previous_close = float(previous[4])

    previous_bullish = (
        previous_close > previous_open
    )

    previous_bearish = (
        previous_close < previous_open
    )

    bullish_engulfing = (
        bullish
        and previous_bearish
        and open_price <= previous_close
        and close >= previous_open
    )

    bearish_engulfing = (
        bearish
        and previous_bullish
        and open_price >= previous_close
        and close <= previous_open
    )

    hammer = (
        lower_wick >= body * 1.8
        and upper_wick
        <= max(
            body * 0.8,
            candle_range * 0.15,
        )
        and close >= open_price
    )

    shooting_star = (
        upper_wick >= body * 1.8
        and lower_wick
        <= max(
            body * 0.8,
            candle_range * 0.15,
        )
        and close <= open_price
    )

    bullish_rejection = (
        bullish
        and lower_wick >= candle_range * 0.35
        and close >= low + candle_range * 0.65
    )

    bearish_rejection = (
        bearish
        and upper_wick >= candle_range * 0.35
        and close <= low + candle_range * 0.35
    )

    return {
        "bullish": bullish,
        "bearish": bearish,
        "range": candle_range,
        "body": body,
        "bullish_engulf": bullish_engulfing,
        "bearish_engulf": bearish_engulfing,
        "hammer": hammer,
        "shooting": shooting_star,
        "strong_bull_rejection": bullish_rejection,
        "strong_bear_rejection": bearish_rejection,
    }


def swing_levels(rows, lookback=80, window=2):
    recent = rows[-lookback:]

    resistance = []
    support = []

    for index in range(
        window,
        len(recent) - window,
    ):
        high = float(recent[index][2])
        low = float(recent[index][1])

        surrounding_highs = [
            float(row[2])
            for row in recent[
                index - window:index + window + 1
            ]
        ]

        surrounding_lows = [
            float(row[1])
            for row in recent[
                index - window:index + window + 1
            ]
        ]

        if high >= max(surrounding_highs):
            resistance.append(high)

        if low <= min(surrounding_lows):
            support.append(low)

    return (
        sorted(set(resistance)),
        sorted(set(support), reverse=True),
    )


def cluster_levels(levels, tolerance):
    if not levels:
        return []

    levels = sorted(levels)

    clusters = [
        [levels[0]]
    ]

    for level in levels[1:]:
        center = (
            sum(clusters[-1])
            / len(clusters[-1])
        )

        if abs(level - center) <= tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])

    return [
        (
            sum(cluster)
            / len(cluster),
            len(cluster),
        )
        for cluster in clusters
    ]


def support_resistance(
    rows,
    price,
    atr_value,
):
    resistance, support = swing_levels(rows)

    tolerance = max(
        atr_value * 0.25,
        price * 0.0005,
    )

    resistance_clusters = cluster_levels(
        resistance,
        tolerance,
    )

    support_clusters = cluster_levels(
        support,
        tolerance,
    )

    levels_above = [
        (level, touches)
        for level, touches
        in resistance_clusters
        if level > price
    ]

    levels_below = [
        (level, touches)
        for level, touches
        in support_clusters
        if level < price
    ]

    nearest_resistance = min(
        levels_above,
        default=None,
        key=lambda item: item[0],
    )

    nearest_support = max(
        levels_below,
        default=None,
        key=lambda item: item[0],
    )

    room_to_resistance = (
        nearest_resistance[0] - price
        if nearest_resistance
        else atr_value * 4
    )

    room_to_support = (
        price - nearest_support[0]
        if nearest_support
        else atr_value * 4
    )

    return (
        nearest_resistance,
        nearest_support,
        room_to_resistance,
        room_to_support,
    )


def structure(rows, lookback=40):
    recent = rows[-lookback:]

    swing_highs = []
    swing_lows = []

    for index in range(
        2,
        len(recent) - 2,
    ):
        high = float(recent[index][2])
        low = float(recent[index][1])

        left_highs = [
            float(row[2])
            for row in recent[
                index - 2:index
            ]
        ]

        right_highs = [
            float(row[2])
            for row in recent[
                index + 1:index + 3
            ]
        ]

        left_lows = [
            float(row[1])
            for row in recent[
                index - 2:index
            ]
        ]

        right_lows = [
            float(row[1])
            for row in recent[
                index + 1:index + 3
            ]
        ]

        if (
            high > max(left_highs)
            and high >= max(right_highs)
        ):
            swing_highs.append(
                (index, high)
            )

        if (
            low < min(left_lows)
            and low <= min(right_lows)
        ):
            swing_lows.append(
                (index, low)
            )

    highs_only = [
        item[1]
        for item in swing_highs
    ]

    lows_only = [
        item[1]
        for item in swing_lows
    ]

    bullish_structure = (
        len(highs_only) >= 2
        and len(lows_only) >= 2
        and highs_only[-1] > highs_only[-2]
        and lows_only[-1] > lows_only[-2]
    )

    bearish_structure = (
        len(highs_only) >= 2
        and len(lows_only) >= 2
        and highs_only[-1] < highs_only[-2]
        and lows_only[-1] < lows_only[-2]
    )

    price = float(recent[-1][4])

    previous_high = (
        highs_only[-1]
        if highs_only
        else price
    )

    previous_low = (
        lows_only[-1]
        if lows_only
        else price
    )

    bullish_bos = (
        bool(highs_only)
        and price > previous_high
    )

    bearish_bos = (
        bool(lows_only)
        and price < previous_low
    )

    recent_high = max(
        float(row[2])
        for row in recent[:-1]
    )

    recent_low = min(
        float(row[1])
        for row in recent[:-1]
    )

    failed_bullish_breakout = (
        float(recent[-1][2]) > recent_high
        and price < recent_high
    )

    failed_bearish_breakout = (
        float(recent[-1][1]) < recent_low
        and price > recent_low
    )

    return {
        "bullish": bullish_structure,
        "bearish": bearish_structure,
        "bos_bull": bullish_bos,
        "bos_bear": bearish_bos,
        "failed_bull": failed_bullish_breakout,
        "failed_bear": failed_bearish_breakout,
    }


def pullback_state(
    rows,
    ema20_value,
    ema50_value,
    atr_value,
    direction,
):
    recent = rows[-6:]

    touched = False

    for row in recent[:-1]:
        high = float(row[2])
        low = float(row[1])

        if (
            low
            <= ema20_value + atr_value * 0.25
            and high
            >= ema20_value - atr_value * 0.25
        ):
            touched = True

        if (
            low
            <= ema50_value + atr_value * 0.25
            and high
            >= ema50_value - atr_value * 0.25
        ):
            touched = True

    candle = candle_info(rows)

    if direction == "CALL":
        rejection = (
            candle["bullish_engulf"]
            or candle["hammer"]
            or candle["strong_bull_rejection"]
        )
    else:
        rejection = (
            candle["bearish_engulf"]
            or candle["shooting"]
            or candle["strong_bear_rejection"]
        )

    return touched, rejection


def rsi_divergence(rows, direction):
    values = closes(rows)

    if len(values) < 35:
        return False

    recent = rows[-35:]

    rsi_points = []

    for index in range(
        15,
        len(values),
    ):
        rsi_value = rsi(
            values[:index + 1]
        )

        rsi_points.append(rsi_value)

    if len(rsi_points) < 10:
        return False

    price_a = float(recent[-15][4])
    price_b = float(recent[-1][4])

    rsi_a = rsi_points[-15]
    rsi_b = rsi_points[-1]

    if rsi_a is None or rsi_b is None:
        return False

    if direction == "CALL":
        return (
            price_b < price_a
            and rsi_b > rsi_a
        )

    return (
        price_b > price_a
        and rsi_b < rsi_a
    )


def analyze(symbol):
    c5 = candles(
        symbol,
        300,
    )

    c1 = candles(
        symbol,
        60,
    )

    if len(c5) < 230 or len(c1) < 80:
        return None, "unavailable"

    x5 = closes(c5)
    x1 = closes(c1)

    p5 = x5[-1]
    p1 = x1[-1]

    entry_ts = int(c1[-1][0])

    # =========================
    # 5M INDICATORS
    # =========================

    ema200_5 = ema(x5, 200)
    ema50_5 = ema(x5, 50)
    ema20_5 = ema(x5, 20)

    ema200_prev = ema(
        x5[:-3],
        200,
    )

    ema50_prev = ema(
        x5[:-3],
        50,
    )

    ema20_prev = ema(
        x5[:-3],
        20,
    )

    # =========================
    # 1M INDICATORS
    # =========================

    ema20_1 = ema(x1, 20)
    ema50_1 = ema(x1, 50)

    rsi5 = rsi(x5)
    rsi1 = rsi(x1)

    macd5 = macd_values(x5)
    macd1 = macd_values(x1)

    atr5 = atr(c5)
    atr1 = atr(c1)

    atr5_history = atr_series(c5)

    dmi5 = adx_dmi(c5)
    dmi1 = adx_dmi(c1)

    if any(
        value is None
        for value in (
            ema200_5,
            ema50_5,
            ema20_5,
            ema200_prev,
            ema50_prev,
            ema20_prev,
            ema20_1,
            ema50_1,
            rsi5,
            rsi1,
            macd5,
            macd1,
            atr5,
            atr1,
            dmi5,
            dmi1,
        )
    ):
        return None, "indicator error"

    adx5, plus5, minus5 = dmi5
    adx1, plus1, minus1 = dmi1

    # =========================
    # MAJOR 5M TREND
    # =========================

    bullish_trend = (
        p5 > ema200_5
        and ema20_5 > ema50_5 > ema200_5
        and ema20_5 > ema20_prev
        and ema50_5 > ema50_prev
        and ema200_5 > ema200_prev
    )

    bearish_trend = (
        p5 < ema200_5
        and ema20_5 < ema50_5 < ema200_5
        and ema20_5 < ema20_prev
        and ema50_5 < ema50_prev
        and ema200_5 < ema200_prev
    )

    if bullish_trend:
        direction = "CALL"
    elif bearish_trend:
        direction = "PUT"
    else:
        return None, "no strong 5M trend"

    # =========================
    # MARKET STRUCTURE
    # =========================

    structure_data = structure(c5)

    if (
        structure_data["failed_bull"]
        or structure_data["failed_bear"]
    ):
        return None, "failed structure breakout"

    if direction == "CALL":
        if not (
            structure_data["bullish"]
            or structure_data["bos_bull"]
        ):
            return None, "structure not bullish"
    else:
        if not (
            structure_data["bearish"]
            or structure_data["bos_bear"]
        ):
            return None, "structure not bearish"

    # =========================
    # SUPPORT / RESISTANCE
    # =========================

    (
        nearest_resistance,
        nearest_support,
        room_resistance,
        room_support,
    ) = support_resistance(
        c5,
        p1,
        atr5,
    )

    if direction == "CALL":
        room = room_resistance
    else:
        room = room_support

    if room < atr5 * 0.90:
        return None, "insufficient room to S/R"

    if (
        direction == "CALL"
        and nearest_resistance
        and nearest_resistance[1] >= 2
        and room_resistance < atr5 * 1.25
    ):
        return None, "CALL too close to resistance"

    if (
        direction == "PUT"
        and nearest_support
        and nearest_support[1] >= 2
        and room_support < atr5 * 1.25
    ):
        return None, "PUT too close to support"

    # =========================
    # CHOP DETECTION
    # =========================

    ema_span = (
        max(
            ema20_5,
            ema50_5,
            ema200_5,
        )
        - min(
            ema20_5,
            ema50_5,
            ema200_5,
        )
    )

    tangled = ema_span < atr5 * 0.70

    weak_adx = (
        adx5 < 20
        or adx1 < 18
    )

    rsi_near_50 = (
        abs(rsi5 - 50) < 2
        and abs(rsi1 - 50) < 3
    )

    weak_dmi = (
        abs(plus5 - minus5) < 5
    )

    if (
        tangled
        or weak_adx
        or (rsi_near_50 and weak_dmi)
    ):
        return None, "choppy market"

    # =========================
    # ANTI-CHASING
    # =========================

    distance_from_ema20 = abs(
        p1 - ema20_1
    )

    distance_from_ema50 = abs(
        p1 - ema50_1
    )

    if (
        distance_from_ema20 > atr1 * 1.50
        or distance_from_ema50 > atr1 * 2.20
    ):
        return None, "entry extended"

    last_range = (
        float(c1[-1][2])
        - float(c1[-1][1])
    )

    if len(atr5_history) >= 30:
        recent_atrs = atr5_history[-30:]
        recent_atrs = sorted(recent_atrs)

        median_atr = recent_atrs[
            len(recent_atrs) // 2
        ]
    else:
        median_atr = atr5

    atr_ratio = (
        atr5
        / max(median_atr, 1e-12)
    )

    if atr_ratio > 1.80:
        return None, "ATR expansion too high"

    if atr_ratio < 0.55:
        return None, "volatility too low"

    if last_range > atr1 * 1.80:
        return None, "signal candle too large"

    large_candle_count = 0

    for row in c1[-5:]:
        candle_range = (
            float(row[2])
            - float(row[1])
        )

        if candle_range > atr1 * 1.25:
            large_candle_count += 1

    if large_candle_count >= 3:
        return None, "recent move already extended"

    # =========================
    # PULLBACK
    # =========================

    slope1_up = (
        ema20_1 > ema50_1
    )

    slope1_down = (
        ema20_1 < ema50_1
    )

    pullback_touched, rejection = pullback_state(
        c1,
        ema20_1,
        ema50_1,
        atr1,
        direction,
    )

    if not pullback_touched or not rejection:
        return None, "pullback confirmation missing"

    # =========================
    # RSI / MACD / DMI / CANDLE
    # =========================

    current_candle = candle_info(c1)

    previous_rsi1 = rsi(
        x1[:-1]
    )

    if direction == "CALL":

        rsi_ok = (
            rsi5 > 50
            and rsi1 > 50
            and previous_rsi1 is not None
            and rsi1 >= previous_rsi1
        )

        dmi_ok = (
            plus5 > minus5
            and plus1 > minus1
        )

        macd_ok = (
            macd5[0] > macd5[1]
            and macd5[2] > macd5[3]
            and macd1[0] > macd1[1]
            and macd1[2] > macd1[3]
        )

        entry_ok = (
            p1 >= ema20_1
            and slope1_up
        )

        candle_ok = (
            current_candle["bullish_engulf"]
            or current_candle["hammer"]
            or current_candle[
                "strong_bull_rejection"
            ]
        )

        structure_ok = (
            structure_data["bullish"]
            or structure_data["bos_bull"]
        )

    else:

        rsi_ok = (
            rsi5 < 50
            and rsi1 < 50
            and previous_rsi1 is not None
            and rsi1 <= previous_rsi1
        )

        dmi_ok = (
            minus5 > plus5
            and minus1 > plus1
        )

        macd_ok = (
            macd5[0] < macd5[1]
            and macd5[2] < macd5[3]
            and macd1[0] < macd1[1]
            and macd1[2] < macd1[3]
        )

        entry_ok = (
            p1 <= ema20_1
            and slope1_down
        )

        candle_ok = (
            current_candle["bearish_engulf"]
            or current_candle["shooting"]
            or current_candle[
                "strong_bear_rejection"
            ]
        )

        structure_ok = (
            structure_data["bearish"]
            or structure_data["bos_bear"]
        )

    # Critical conditions cannot be rescued by score.

    if not dmi_ok:
        return None, "DMI conflicts with direction"

    if not entry_ok:
        return None, "1M entry confirmation missing"

    if not candle_ok:
        return None, "candle confirmation missing"

    # =========================
    # QUALITY SCORE 0-100
    # =========================

    trend_score = 15

    structure_score = (
        15
        if structure_ok
        else 8
    )

    if room >= atr5 * 1.75:
        sr_score = 15
    elif room >= atr5 * 1.25:
        sr_score = 12
    else:
        sr_score = 9

    pullback_score = (
        10
        if pullback_touched and rejection
        else 0
    )

    rsi_score = (
        10
        if rsi_ok
        else 5
    )

    macd_score = (
        10
        if macd_ok
        else 5
    )

    adx_score = (
        10
        if (
            adx5 >= 25
            and (
                (
                    direction == "CALL"
                    and plus5 > minus5
                )
                or
                (
                    direction == "PUT"
                    and minus5 > plus5
                )
            )
        )
        else 7
    )

    candle_score = (
        10
        if candle_ok
        else 5
    )

    room_score = (
        5
        if room >= atr5 * 2.0
        else 4
        if room >= atr5 * 1.5
        else 3
    )

    score = (
        trend_score
        + structure_score
        + sr_score
        + pullback_score
        + rsi_score
        + macd_score
        + adx_score
        + candle_score
        + room_score
    )

    if score < MIN_SCORE:
        return None, (
            f"quality below {MIN_SCORE}"
        )

    divergence = rsi_divergence(
        c1,
        direction,
    )

    return {
        "direction": direction,
        "score": score,
        "price": round(p1, 8),
        "entry_ts": entry_ts,

        "rsi5": round(rsi5, 1),
        "rsi1": round(rsi1, 1),

        "adx5": round(adx5, 1),
        "adx1": round(adx1, 1),

        "room_atr": round(
            room / max(atr5, 1e-12),
            2,
        ),

        "trend": (
            "Bullish"
            if direction == "CALL"
            else "Bearish"
        ),

        "structure": (
            "Higher highs/lows"
            if (
                direction == "CALL"
                and structure_data["bullish"]
            )
            else
            "Lower highs/lows"
            if (
                direction == "PUT"
                and structure_data["bearish"]
            )
            else
            "BOS"
        ),

        "pullback": "Confirmed",

        "support_resistance": "Confirmed",

        "rsi_state": (
            "Bullish"
            if direction == "CALL"
            else "Bearish"
        ),

        "macd_state": (
            "Bullish"
            if direction == "CALL"
            else "Bearish"
        ),

        "adx_state": (
            "Strong"
            if adx5 >= 25
            else "Trend"
        ),

        "room": (
            "Good"
            if room >= atr5 * 1.75
            else "Acceptable"
        ),

        "entry_state": "Not extended",

        "rsi_divergence": (
            "Yes"
            if divergence
            else "No"
        ),

        "candle": "Engulfing/Rejection",

        "atr_ratio": round(
            atr_ratio,
            2,
        ),

        "expiry_minutes": EXPIRY_MINUTES,
    }, None


def compare_result(
    direction,
    entry,
    expiry,
):
    if entry is None or expiry is None:
        return None

    if expiry == entry:
        return "DRAW"

    if direction == "CALL":
        return (
            "WIN"
            if expiry > entry
            else "LOSS"
        )

    return (
        "WIN"
        if expiry < entry
        else "LOSS"
    )


def coinbase_price_at(
    symbol,
    target_ts,
):
    rows = candles(
        symbol,
        60,
        target_ts - 180,
        target_ts + 180,
    )

    if not rows:
        return None

    eligible = [
        row
        for row in rows
        if row[0] <= target_ts
    ]

    if not eligible:
        return None

    latest = max(
        eligible,
        key=lambda row: row[0],
    )

    return float(latest[4])


def get_pocket_client():
    ssid = os.getenv(
        "PO_SSID",
        "",
    ).strip()

    if not ssid:
        return None, "PO_SSID not configured"

    if PocketOptionAsync is None:
        return None, (
            "PocketOption library unavailable"
        )

    return ssid, None


async def resolve_pocket_symbol(
    client,
    symbol,
):
    candidates = PO_SYMBOL_CANDIDATES.get(
        symbol,
        [],
    )

    try:
        active = await client.active_assets()

        names = set()

        if isinstance(active, dict):

            names.update(
                active.keys()
            )

            for value in active.values():

                if isinstance(value, dict):

                    for key in (
                        "symbol",
                        "name",
                        "asset",
                    ):
                        if value.get(key):
                            names.add(
                                str(value[key])
                            )

        elif isinstance(active, list):

            for value in active:

                if isinstance(value, str):
                    names.add(value)

                elif isinstance(value, dict):

                    for key in (
                        "symbol",
                        "name",
                        "asset",
                    ):
                        if value.get(key):
                            names.add(
                                str(value[key])
                            )

        for candidate in candidates:

            if candidate in names:
                return candidate

    except Exception:
        pass

    return (
        candidates[0]
        if candidates
        else None
    )


async def pocket_candle_close(
    client,
    asset,
    target_ts,
):
    try:

        rows = await client.get_candles_advanced(
            asset,
            60,
            PO_LOOKBACK_CANDLES * 60,
            int(target_ts),
        )

    except Exception:
        return None

    normalized = []

    for row in rows or []:

        try:

            timestamp = int(
                float(
                    row.get(
                        "time",
                        row.get("timestamp"),
                    )
                )
            )

            if timestamp > 10_000_000_000:
                timestamp //= 1000

            normalized.append(
                (
                    timestamp,
                    float(row["close"]),
                )
            )

        except (
            TypeError,
            ValueError,
            KeyError,
        ):
            continue

    if not normalized:
        return None

    exact = [
        item
        for item in normalized
        if item[0] == target_ts
    ]

    if exact:
        return exact[0][1]

    before = [
        item
        for item in normalized
        if item[0] <= target_ts
    ]

    if not before:
        return None

    timestamp, close = max(
        before,
        key=lambda item: item[0],
    )

    if target_ts - timestamp <= 120:
        return close

    return None


async def resolve_pocket_async(
    pending,
):
    ssid, error = get_pocket_client()

    if error:
        return [], error

    resolved = []

    try:

        async with PocketOptionAsync(
            ssid
        ) as client:

            if not client.is_demo():
                return [], (
                    "PO_SSID is not a demo session"
                )

            for signal in pending:

                if (
                    signal.get(
                        "pocket_result"
                    )
                    != "PENDING"
                ):
                    continue

                po_asset = await resolve_pocket_symbol(
                    client,
                    signal["symbol"],
                )

                if not po_asset:
                    continue

                entry_ts = int(
                    signal["entry_ts"]
                )

                expiry_ts = int(
                    datetime.fromisoformat(
                        signal[
                            "expiry_at"
                        ].replace(
                            "Z",
                            "+00:00",
                        )
                    ).timestamp()
                )

                pocket_entry = (
                    await pocket_candle_close(
                        client,
                        po_asset,
                        entry_ts,
                    )
                )

                pocket_expiry = (
                    await pocket_candle_close(
                        client,
                        po_asset,
                        expiry_ts,
                    )
                )

                if (
                    pocket_entry is None
                    or pocket_expiry is None
                ):
                    continue

                result = compare_result(
                    signal["direction"],
                    pocket_entry,
                    pocket_expiry,
                )

                signal.update(
                    {
                        "pocket_symbol": po_asset,

                        "pocket_entry_price":
                            round(
                                pocket_entry,
                                8,
                            ),

                        "pocket_expiry_price":
                            round(
                                pocket_expiry,
                                8,
                            ),

                        "pocket_result": result,

                        "pocket_resolved_at":
                            datetime.now(
                                timezone.utc
                            ).isoformat(),

                        "pocket_result_source":
                            "automatic_demo_feed",
                    }
                )

                resolved.append(
                    (
                        signal["id"],
                        "Pocket",
                        result,
                    )
                )

    except Exception as exc:

        return (
            resolved,
            f"Pocket feed error: {type(exc).__name__}",
        )

    return resolved, None


def resolve_pending():
    tracker = load_tracker()

    now = datetime.now(
        timezone.utc
    )

    pending = []

    for signal in tracker.get(
        "signals",
        [],
    ):

        if (
            signal.get(
                "coinbase_result"
            ) == "PENDING"
            or
            signal.get(
                "pocket_result"
            ) == "PENDING"
        ):

            expiry = signal.get(
                "expiry_at"
            )

            if not expiry:
                continue

            try:

                expiry_dt = datetime.fromisoformat(
                    expiry.replace(
                        "Z",
                        "+00:00",
                    )
                )

            except ValueError:
                continue

            if now >= expiry_dt:
                pending.append(signal)

    if not pending:
        return [], [], None

    resolved = []

    # =========================
    # COINBASE RESULT
    # =========================

    for signal in pending:

        if (
            signal.get(
                "coinbase_result"
            )
            != "PENDING"
        ):
            continue

        expiry_ts = int(
            datetime.fromisoformat(
                signal[
                    "expiry_at"
                ].replace(
                    "Z",
                    "+00:00",
                )
            ).timestamp()
        )

        expiry_price = coinbase_price_at(
            signal["symbol"],
            expiry_ts,
        )

        if expiry_price is None:
            continue

        result = compare_result(
            signal["direction"],
            float(
                signal["price"]
            ),
            expiry_price,
        )

        signal.update(
            {
                "coinbase_entry_price":
                    float(
                        signal["price"]
                    ),

                "coinbase_expiry_price":
                    round(
                        expiry_price,
                        8,
                    ),

                "coinbase_result":
                    result,

                "coinbase_resolved_at":
                    now.isoformat(),
            }
        )

        resolved.append(
            (
                signal["id"],
                "Coinbase",
                result,
            )
        )

    # =========================
    # POCKET OPTION RESULT
    # =========================

    pocket_resolved, pocket_error = (
        asyncio.run(
            resolve_pocket_async(
                pending
            )
        )
    )

    resolved.extend(
        pocket_resolved
    )

    # =========================
    # SAVE RESULTS
    # =========================

    if resolved:

        changed = {
            signal["id"]: signal
            for signal in pending
        }

        def mutate(data):

            keys = (
                "coinbase_entry_price",
                "coinbase_expiry_price",
                "coinbase_result",
                "coinbase_resolved_at",

                "pocket_symbol",
                "pocket_entry_price",
                "pocket_expiry_price",
                "pocket_result",
                "pocket_resolved_at",
                "pocket_result_source",
            )

            for item in data.get(
                "signals",
                [],
            ):

                source = changed.get(
                    item.get("id")
                )

                if source:

                    for key in keys:

                        if key in source:
                            item[key] = source[key]

            return data

        update_tracker(
            mutate,
            "Resolve V3 dual experiment results",
        )

    return (
        resolved,
        pending,
        pocket_error,
    )


def cooldown_allows(
    tracker,
    signal,
):
    cutoff = (
        signal["entry_ts"]
        - COOLDOWN_MINUTES * 60
    )

    for old in reversed(
        tracker.get(
            "signals",
            [],
        )
    ):

        if (
            old.get("symbol")
            != signal["symbol"]
        ):
            continue

        try:
            old_ts = int(
                old.get(
                    "entry_ts",
                    0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if old_ts < cutoff:
            break

        if (
            old.get("direction")
            == signal["direction"]
        ):
            return False

    return True


def add_signals(
    found,
    scan_id,
):
    if not found:
        return

    def add(data):

        ids = {
            signal.get("id")
            for signal in data.get(
                "signals",
                [],
            )
        }

        for signal in found:

            if signal["id"] not in ids:
                data["signals"].append(
                    signal
                )

        data["signals"] = data[
            "signals"
        ][-1000:]

        return data

    update_tracker(
        add,
        f"Add V3 dual-experiment signals {scan_id}",
    )


def main():

    # First resolve previous experiments.
    resolved, pending, pocket_error = (
        resolve_pending()
    )

    tracker = load_tracker()

    scan_id = (
        os.getenv(
            "GITHUB_RUN_ID"
        )
        or str(int(time.time()))
    )

    created_dt = datetime.now(
        timezone.utc
    )

    created = created_dt.isoformat()

    found = []
    unavailable = []
    rejected = {}

    for symbol in ASSETS:

        try:

            signal, reason = analyze(
                symbol
            )

            if signal:

                signal.update(
                    {
                        "id":
                            f"V3-{symbol}-{signal['direction']}-{signal['entry_ts']}",

                        "symbol":
                            symbol,

                        "asset":
                            f"{symbol} OTC",

                        "result":
                            "PENDING",

                        "coinbase_result":
                            "PENDING",

                        "pocket_result":
                            "PENDING",

                        "created_at":
                            created,

                        "entry_at":
                            datetime.fromtimestamp(
                                signal["entry_ts"],
                                timezone.utc,
                            ).isoformat(),

                        "expiry_at":
                            datetime.fromtimestamp(
                                signal["entry_ts"]
                                + EXPIRY_MINUTES * 60,
                                timezone.utc,
                            ).isoformat(),
                    }
                )

                if cooldown_allows(
                    tracker,
                    signal,
                ):
                    found.append(
                        signal
                    )
                else:
                    rejected[symbol] = (
                        "cooldown"
                    )

            elif reason == "unavailable":

                unavailable.append(
                    symbol
                )

            else:

                rejected[symbol] = reason

        except Exception as exc:

            unavailable.append(
                f"{symbol}({type(exc).__name__})"
            )

    add_signals(
        found,
        scan_id,
    )

    # =========================
    # TELEGRAM MESSAGE
    # =========================

    lines = [
        (
            "📡 V3 DUAL EXPERIMENT • "
            + created.replace(
                "T",
                " ",
            ).replace(
                "+00:00",
                " UTC",
            )
        ),

        "5M trend + 1M entry confirmation",

        (
            f"Quality threshold: "
            f"{MIN_SCORE}/100 | "
            f"Expiry: "
            f"{EXPIRY_MINUTES}M"
        ),

        (
            "Hard filters: trend • "
            "structure • S/R • "
            "chop • extension • "
            "pullback"
        ),

        (
            "Coinbase = exchange feed | "
            "Pocket Option = demo OTC feed"
        ),

        "",
    ]

    if resolved:

        lines.append(
            "RESULTS RESOLVED:"
        )

        for signal_id, market, result in resolved:

            lines.append(
                f"• {signal_id} | "
                f"{market}: {result}"
            )

        lines.append("")

    if (
        pocket_error
        and any(
            signal.get(
                "pocket_result"
            ) == "PENDING"
            for signal in pending
        )
    ):

        lines.extend(
            [
                f"Pocket feed: {pocket_error}",
                "",
            ]
        )

    if found:

        for signal in found:

            if (
                signal["direction"]
                == "CALL"
            ):
                arrow = "🟢 CALL"
            else:
                arrow = "🔴 PUT"

            lines.extend(
                [
                    (
                        f"{arrow} • "
                        f"{signal['asset']}"
                    ),

                    (
                        f"Quality: "
                        f"{signal['score']}/100"
                    ),

                    (
                        f"Trend: "
                        f"{signal['trend']} | "
                        f"Structure: "
                        f"{signal['structure']}"
                    ),

                    (
                        f"Pullback: "
                        f"{signal['pullback']} | "
                        f"S/R: "
                        f"{signal['support_resistance']}"
                    ),

                    (
                        f"RSI: "
                        f"{signal['rsi_state']} "
                        f"({signal['rsi5']}/"
                        f"{signal['rsi1']}) | "
                        f"MACD: "
                        f"{signal['macd_state']}"
                    ),

                    (
                        f"ADX: "
                        f"{signal['adx5']} | "
                        f"Room: "
                        f"{signal['room']} "
                        f"({signal['room_atr']} ATR)"
                    ),

                    (
                        f"Entry: "
                        f"{signal['entry_state']} | "
                        f"Candle: "
                        f"{signal['candle']}"
                    ),

                    (
                        f"Entry: "
                        f"{signal['price']} | "
                        f"Expiry reference: "
                        f"{EXPIRY_MINUTES}M"
                    ),

                    (
                        f"Signal ID: "
                        f"{signal['id']}"
                    ),

                    "",
                ]
            )

    else:

        lines.append(
            "⚪ NO TRADE — "
            "no setup passed all "
            "V3 hard filters"
        )

        if rejected:

            top_reasons = list(
                rejected.items()
            )[:6]

            lines.append(
                "Reasons: "
                + "; ".join(
                    f"{asset}: {reason}"
                    for asset, reason
                    in top_reasons
                )
            )

    if unavailable:

        lines.append(
            "Unavailable: "
            + ", ".join(
                unavailable
            )
        )

    lines.append(
        "Demo/testing only • "
        "no trades are placed by this scanner."
    )

    tg(
        "\n".join(lines)
    )


if __name__ == "__main__":
    main()
