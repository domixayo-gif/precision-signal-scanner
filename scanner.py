import os
import time
import math
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# BACK TO TREND — IQ OPTION OTC SCANNER
# 30M TREND -> 5M PULLBACK -> 1M ENTRY
# REFERENCE EXPIRY: 3 MINUTES
# READ-ONLY / NO AUTOMATIC TRADING
# ============================================================

PRACTICE = True

TF_30M = 1800
TF_5M = 300
TF_1M = 60

CANDLES_30M = 180
CANDLES_5M = 180
CANDLES_1M = 180

EXPIRY_MINUTES = 3

SCAN_INTERVAL = 60
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30

MAX_RUNTIME = 24 * 60 * 60

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

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

iq = None
scan_cycles = 0
assets_scanned = 0
signals_sent = 0
errors_recovered = 0
last_status = 0
sent_signal_keys = {}


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=15,
        )
        return r.ok
    except Exception:
        return False


def safe_float(value, default=None):
    try:
        x = float(value)

        if not math.isfinite(x):
            return default

        return x

    except Exception:
        return default


def ema(values, period):
    values = [safe_float(x) for x in values]
    values = [x for x in values if x is not None]

    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1.0)

    for value in values[period:]:
        result = ((value - result) * multiplier) + result

    return result


def rsi(values, period=14):
    values = [safe_float(x) for x in values]
    values = [x for x in values if x is not None]

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = safe_float(candles[i]["high"])
        low = safe_float(candles[i]["low"])
        previous_close = safe_float(
            candles[i - 1]["close"]
        )

        if None in (
            high,
            low,
            previous_close
        ):
            continue

        trs.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close)
            )
        )

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def adx(candles, period=14):
    if len(candles) < (period * 2) + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):

        high = safe_float(candles[i]["high"])
        low = safe_float(candles[i]["low"])

        previous_high = safe_float(
            candles[i - 1]["high"]
        )

        previous_low = safe_float(
            candles[i - 1]["low"]
        )

        previous_close = safe_float(
            candles[i - 1]["close"]
        )

        if None in (
            high,
            low,
            previous_high,
            previous_low,
            previous_close
        ):
            continue

        trs.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close)
            )
        )

        up_move = high - previous_high
        down_move = previous_low - low

        plus_dm.append(
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm.append(
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

    if len(trs) < period * 2:
        return None

    dx_values = []

    for i in range(period - 1, len(trs)):

        tr_sum = sum(
            trs[i - period + 1:i + 1]
        )

        plus_sum = sum(
            plus_dm[i - period + 1:i + 1]
        )

        minus_sum = sum(
            minus_dm[i - period + 1:i + 1]
        )

        if tr_sum <= 0:
            continue

        plus_di = 100.0 * plus_sum / tr_sum
        minus_di = 100.0 * minus_sum / tr_sum

        denominator = plus_di + minus_di

        if denominator <= 0:
            continue

        dx_values.append(
            100.0
            * abs(plus_di - minus_di)
            / denominator
        )

    if len(dx_values) < period:
        return None

    return sum(dx_values[-period:]) / period


def candle_range(candle):
    return (
        safe_float(candle["high"], 0.0)
        - safe_float(candle["low"], 0.0)
    )


def candle_body(candle):
    return abs(
        safe_float(candle["close"], 0.0)
        - safe_float(candle["open"], 0.0)
    )


def bullish(candle):
    return (
        safe_float(candle["close"], 0.0)
        > safe_float(candle["open"], 0.0)
    )


def bearish(candle):
    return (
        safe_float(candle["close"], 0.0)
        < safe_float(candle["open"], 0.0)
    )


def bullish_engulfing(previous, current):
    return (
        bearish(previous)
        and bullish(current)
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        bullish(previous)
        and bearish(current)
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bullish_pinbar(candle):
    high = safe_float(candle["high"], 0.0)
    low = safe_float(candle["low"], 0.0)
    open_price = safe_float(candle["open"], 0.0)
    close = safe_float(candle["close"], 0.0)

    body = abs(close - open_price)

    lower_wick = (
        min(open_price, close) - low
    )

    upper_wick = (
        high - max(open_price, close)
    )

    return (
        body > 0
        and lower_wick >= body * 2
        and lower_wick > upper_wick
        and close > low + (
            (high - low) * 0.50
        )
    )


def bearish_pinbar(candle):
    high = safe_float(candle["high"], 0.0)
    low = safe_float(candle["low"], 0.0)
    open_price = safe_float(candle["open"], 0.0)
    close = safe_float(candle["close"], 0.0)

    body = abs(close - open_price)

    lower_wick = (
        min(open_price, close) - low
    )

    upper_wick = (
        high - max(open_price, close)
    )

    return (
        body > 0
        and upper_wick >= body * 2
        and upper_wick > lower_wick
        and close < high - (
            (high - low) * 0.50
        )
    )


def server_now():
    try:
        if iq is not None:
            value = iq.get_server_timestamp()

            if value:
                return int(value)

    except Exception:
        pass

    return int(time.time())


def get_closed_candles(
    asset,
    timeframe,
    count
):
    if iq is None:
        return []

    try:
        candles = iq.get_candles(
            asset,
            timeframe,
            count + 5,
            server_now()
        )

        if not candles:
            return []

        candles = sorted(
            candles,
            key=lambda x: int(x["from"])
        )

        now = server_now()

        closed = [
            candle
            for candle in candles
            if int(candle["from"]) + timeframe <= now
        ]

        return closed[-count:]

    except Exception:
        return []


def analyze_trend(candles):
    if len(candles) < EMA_SLOW + 10:
        return None

    closes = [
        safe_float(candle["close"])
        for candle in candles
    ]

    closes = [
        x for x in closes
        if x is not None
    ]

    fast = ema(
        closes,
        EMA_FAST
    )

    slow = ema(
        closes,
        EMA_SLOW
    )

    atr_value = atr(
        candles,
        ATR_PERIOD
    )

    rsi_value = rsi(
        closes,
        RSI_PERIOD
    )

    adx_value = adx(
        candles,
        ADX_PERIOD
    )

    if None in (
        fast,
        slow,
        atr_value
    ):
        return None

    price = closes[-1]

    if fast > slow and price > fast:
        direction = "BULLISH"

    elif fast < slow and price < fast:
        direction = "BEARISH"

    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "price": price,
        "ema_fast": fast,
        "ema_slow": slow,
        "atr": atr_value,
        "rsi": rsi_value,
        "adx": adx_value
    }


def structure(candles):
    if len(candles) < SWING_LOOKBACK:
        return None

    recent = candles[-SWING_LOOKBACK:]

    highs = [
        safe_float(candle["high"])
        for candle in recent
    ]

    lows = [
        safe_float(candle["low"])
        for candle in recent
    ]

    highs = [
        x for x in highs
        if x is not None
    ]

    lows = [
        x for x in lows
        if x is not None
    ]

    if not highs or not lows:
        return None

    return {
        "resistance": max(highs),
        "support": min(lows)
    }


def pullback_quality(
    candles,
    direction,
    atr_value
):
    if len(candles) < 10 or not atr_value:
        return (
            False,
            0,
            "insufficient pullback data"
        )

    recent = candles[-8:]

    price = safe_float(
        candles[-1]["close"]
    )

    recent_high = max(
        safe_float(
            candle["high"],
            0
        )
        for candle in recent
    )

    recent_low = min(
        safe_float(
            candle["low"],
            0
        )
        for candle in recent
    )

    if direction == "BULLISH":
        distance = abs(
            price - recent_high
        )

    else:
        distance = abs(
            price - recent_low
        )

    if distance <= (
        atr_value
        * MAX_5M_DISTANCE_ATR
    ):
        return (
            True,
            15,
            "valid trend pullback"
        )

    return (
        False,
        0,
        "pullback too far"
    )


def trigger_quality(
    candles,
    direction,
    atr_value
):
    if len(candles) < 5 or not atr_value:
        return (
            False,
            0,
            "insufficient entry data"
        )

    previous = candles[-2]
    current = candles[-1]

    candle_rng = candle_range(current)
    body = candle_body(current)

    if candle_rng <= 0:
        return (
            False,
            0,
            "invalid trigger candle"
        )

    if candle_rng > (
        atr_value
        * MAX_TRIGGER_RANGE_ATR
    ):
        return (
            False,
            0,
            "trigger candle too large"
        )

    if body / candle_rng < 0.35:
        return (
            False,
            0,
            "weak trigger body"
        )

    if direction == "BULLISH":

        engulf = bullish_engulfing(
            previous,
            current
        )

        pin = bullish_pinbar(
            current
        )

        strong_close = (
            current["close"]
            >= current["high"]
            - (candle_rng * 0.25)
        )

        if engulf or pin or strong_close:
            return (
                True,
                20,
                "bullish trigger"
            )

    if direction == "BEARISH":

        engulf = bearish_engulfing(
            previous,
            current
        )

        pin = bearish_pinbar(
            current
        )

        strong_close = (
            current["close"]
            <= current["low"]
            + (candle_rng * 0.25)
        )

        if engulf or pin or strong_close:
            return (
                True,
                20,
                "bearish trigger"
            )

    return (
        False,
        0,
        "no valid trigger"
    )


def evaluate_asset(asset):
    try:

        # =========================
        # 30M PRIMARY TREND
        # =========================

        candles_30m = get_closed_candles(
            asset,
            TF_30M,
            CANDLES_30M
        )

        if len(candles_30m) < EMA_SLOW + 10:
            return None

        trend_30m = analyze_trend(
            candles_30m
        )

        if not trend_30m:
            return None

        if trend_30m["direction"] == "NEUTRAL":
            return None

        if (
            trend_30m["adx"] is None
            or trend_30m["adx"] < MIN_ADX
        ):
            return None

        direction = trend_30m["direction"]

        # =========================
        # 5M PULLBACK
        # =========================

        candles_5m = get_closed_candles(
            asset,
            TF_5M,
            CANDLES_5M
        )

        if len(candles_5m) < EMA_SLOW + 10:
            return None

        trend_5m = analyze_trend(
            candles_5m
        )

        if not trend_5m:
            return None

        if trend_5m["direction"] != direction:
            return None

        if (
            trend_5m["adx"] is None
            or trend_5m["adx"] < MIN_ADX
        ):
            return None

        pullback_ok, pullback_points, pullback_reason = (
            pullback_quality(
                candles_5m,
                direction,
                trend_5m["atr"]
            )
        )

        if not pullback_ok:
            return None

        # =========================
        # 1M ENTRY
        # =========================

        candles_1m = get_closed_candles(
            asset,
            TF_1M,
            CANDLES_1M
        )

        if len(candles_1m) < 20:
            return None

        trigger_ok, trigger_points, trigger_reason = (
            trigger_quality(
                candles_1m,
                direction,
                trend_5m["atr"]
            )
        )

        if not trigger_ok:
            return None

        # =========================
        # ROOM CHECK
        # =========================

        levels = structure(
            candles_5m
        )

        if not levels:
            return None

        price = trend_5m["price"]

        if direction == "BULLISH":
            room = (
                levels["resistance"]
                - price
            )

        else:
            room = (
                price
                - levels["support"]
            )

        room_atr = (
            room / trend_5m["atr"]
            if trend_5m["atr"]
            else 0
        )

        if room_atr < MIN_ROOM_ATR:
            return None

        # =========================
        # EXTENSION CHECK
        # =========================

        extension = (
            abs(
                price
                - trend_5m["ema_fast"]
            )
            / trend_5m["atr"]
        )

        if extension > MAX_EXTENSION_ATR:
            return None

        # =========================
        # RSI CHECK
        # =========================

        if trend_5m["rsi"] is None:
            return None

        if (
            direction == "BULLISH"
            and trend_5m["rsi"] >= 72
        ):
            return None

        if (
            direction == "BEARISH"
            and trend_5m["rsi"] <= 28
        ):
            return None

        # =========================
        # SCORE
        # =========================

        score = 25

        score += 20

        score += (
            10
            if trend_30m["adx"] >= 25
            else 5
        )

        score += (
            10
            if trend_5m["adx"] >= 25
            else 5
        )

        score += pullback_points
        score += trigger_points

        if room_atr >= 2.0:
            score += 10

        elif room_atr >= 1.5:
            score += 7

        else:
            score += 5

        if (
            direction == "BULLISH"
            and 45 <= trend_5m["rsi"] <= 65
        ):
            score += 5

        elif (
            direction == "BEARISH"
            and 35 <= trend_5m["rsi"] <= 55
        ):
            score += 5

        score = min(
            100,
            int(score)
        )

        if score < MIN_SCORE:
            return None

        return {
            "asset": asset,
            "direction": (
                "CALL"
                if direction == "BULLISH"
                else "PUT"
            ),
            "score": score,
            "price": price,
            "rsi": trend_5m["rsi"],
            "adx30": trend_30m["adx"],
            "adx5": trend_5m["adx"],
            "room_atr": room_atr,
            "extension_atr": extension,
            "trigger_time": int(
                candles_1m[-1]["from"]
            ),
            "pullback": pullback_reason,
            "trigger": trigger_reason
        }

    except Exception:
        return None


def discover_otc_assets():
    if iq is None:
        return []

    try:

        opened = iq.get_all_open_time()

        if not opened:
            return []

        assets = set()

        for market_type in (
            "binary",
            "turbo",
            "digital"
        ):

            market = opened.get(
                market_type,
                {}
            )

            if not isinstance(
                market,
                dict
            ):
                continue

            for asset, info in market.items():

                if not isinstance(
                    info,
                    dict
                ):
                    continue

                if not info.get(
                    "open",
                    False
                ):
                    continue

                if "-OTC" in str(
                    asset
                ).upper():
                    assets.add(asset)

        return sorted(assets)

    except Exception:
        return []


def connect():
    global iq

    try:

        iq = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        connected, reason = iq.connect()

        if not connected:

            telegram(
                "🔴 IQ OPTION CONNECTION FAILED\n"
                f"Reason: {reason}\n"
                "Retrying automatically."
            )

            return False

        try:

            iq.change_balance(
                "PRACTICE"
                if PRACTICE
                else "REAL"
            )

        except Exception:
            pass

        return True

    except Exception as exc:

        telegram(
            "🔴 IQ OPTION CONNECTION ERROR\n"
            f"{str(exc)[:300]}\n"
            "Retrying automatically."
        )

        return False


def cleanup_signal_keys():
    now = time.time()

    old_keys = [
        key
        for key, timestamp
        in sent_signal_keys.items()
        if now - timestamp > 600
    ]

    for key in old_keys:
        del sent_signal_keys[key]


def send_signal(signal):
    global signals_sent

    key = (
        signal["asset"],
        signal["direction"],
        signal["trigger_time"]
    )

    cleanup_signal_keys()

    if key in sent_signal_keys:
        return

    sent_signal_keys[key] = time.time()

    signal_id = (
        f'{signal["asset"]}-'
        f'{signal["direction"]}-'
        f'{datetime.now(timezone.utc).strftime("%H%M%S")}'
    )

    message = (
        "🔴 NEW QUALIFIED OTC SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f'Asset: {signal["asset"]}\n'
        f'Direction: {signal["direction"]}\n'
        f'Score: {signal["score"]}/100\n'
        f'Reference expiry: {EXPIRY_MINUTES} minutes\n'
        f'Price: {signal["price"]}\n'
        "\n"
        "30M: PRIMARY TREND\n"
        "5M: PULLBACK / STRUCTURE\n"
        "1M: ENTRY TRIGGER\n"
        f'5M RSI: {signal["rsi"]:.1f}\n'
        f'30M ADX: {signal["adx30"]:.1f}\n'
        f'5M ADX: {signal["adx5"]:.1f}\n'
        f'Room: {signal["room_atr"]:.2f} ATR\n'
        f'Extension: {signal["extension_atr"]:.2f} ATR\n'
        f'Pullback: {signal["pullback"]}\n'
        f'Trigger: {signal["trigger"]}\n'
        "\n"
        f"Signal ID: {signal_id}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "READ-ONLY — NO AUTOMATIC TRADE"
    )

    if telegram(message):
        signals_sent += 1


def send_status(
    assets,
    session_start
):
    uptime = int(
        time.time()
        - session_start
    )

    hours = uptime // 3600

    minutes = (
        uptime % 3600
    ) // 60

    telegram(
        "🟡 BACK TO TREND STATUS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"OTC assets: {len(assets)}\n"
        f"Scan cycles: {scan_cycles}\n"
        f"Assets scanned: {assets_scanned}\n"
        f"Qualified signals: {signals_sent}\n"
        f"Recovered errors: {errors_recovered}\n"
        f"Uptime: {hours}h {minutes}m\n"
        "\n"
        "30M: Primary trend\n"
        "5M: Pullback / structure\n"
        "1M: Entry trigger\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        "Scanner is still running.\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def run_session():
    global scan_cycles
    global assets_scanned
    global errors_recovered
    global last_status

    session_start = time.time()

    telegram(
        "🟡 BACK TO TREND SCANNER ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy: Back to Trend\n"
        "Context: 30M\n"
        "Pullback: 5M\n"
        "Entry: 1M\n"
        f"Expiry: {EXPIRY_MINUTES} minutes\n"
        f"Scan interval: {SCAN_INTERVAL} seconds\n"
        "Status interval: 5 minutes\n"
        "Maximum session: 24 hours\n"
        "Mode: READ-ONLY\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    while (
        time.time() - session_start
        < MAX_RUNTIME
    ):

        cycle_start = time.time()

        scan_cycles += 1

        if iq is None:

            if not connect():

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

        assets = discover_otc_assets()

        if not assets:

            errors_recovered += 1

            telegram(
                "🟠 NO OTC ASSETS FOUND\n"
                "Scanner remains active.\n"
                "Retrying automatically."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )

            continue

        for asset in assets:

            try:

                signal = evaluate_asset(
                    asset
                )

                assets_scanned += 1

                if signal:
                    send_signal(
                        signal
                    )

            except Exception:

                errors_recovered += 1

            time.sleep(0.15)

        now = time.time()

        if (
            now - last_status
            >= STATUS_INTERVAL
        ):

            send_status(
                assets,
                session_start
            )

            last_status = now

        elapsed = (
            time.time()
            - cycle_start
        )

        wait_time = max(
            5,
            SCAN_INTERVAL - elapsed
        )

        time.sleep(
            wait_time
        )

    telegram(
        "🟢 24-HOUR SCANNER SESSION FINISHED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Scan cycles: {scan_cycles}\n"
        f"Qualified signals: {signals_sent}\n"
        f"Recovered errors: {errors_recovered}\n"
        "Starting a fresh session automatically."
    )


def main():
    global iq

    if not IQ_EMAIL or not IQ_PASSWORD:

        telegram(
            "🔴 MISSING IQ OPTION CREDENTIALS\n"
            "Check IQ_EMAIL and IQ_PASSWORD secrets."
        )

        raise RuntimeError(
            "Missing IQ_EMAIL or IQ_PASSWORD"
        )

    while True:

        try:

            if (
                iq is None
                and not connect()
            ):

                time.sleep(
                    RECONNECT_INTERVAL
                )

                continue

            run_session()

            iq = None

            time.sleep(10)

        except KeyboardInterrupt:

            telegram(
                "⛔ SCANNER STOPPED MANUALLY"
            )

            break

        except Exception as exc:

            iq = None

            telegram(
                "🔴 SCANNER ERROR — RECOVERY MODE\n"
                f"{str(exc)[:400]}\n"
                "Reconnecting and continuing automatically."
            )

            time.sleep(
                RECONNECT_INTERVAL
            )


if __name__ == "__main__":
    main())
