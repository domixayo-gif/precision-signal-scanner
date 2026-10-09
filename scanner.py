import os
import time
import math
import csv
import traceback
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# ZETA V2.4 — CONTROLLED PRACTICE TEST
# ============================================================

BALANCE_MODE = "PRACTICE"
STAKE = 1.0
EXPIRY_MINUTES = 2

TF5 = 300
TF1 = 60
COUNT5 = 180
COUNT1 = 180

MAX_OTC_ASSETS = 70
TARGET_TRADES = 50

SCAN_INTERVAL = 5
STATUS_INTERVAL = 300
RECONNECT_INTERVAL = 30
DISCOVERY_INTERVAL = 1800
ASSET_LOCK_SECONDS = 300

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MIN_SCORE = 80
MIN_ADX = 18
MIN_ROOM_ATR = 0.80
MIN_PULLBACK_ATR = 0.20
MAX_PULLBACK_ATR = 1.50
MAX_EXTENSION_ATR = 1.80
ZONE_TOLERANCE_ATR = 0.35
MIN_BODY_RATIO = 0.40

RSI_BULL_MIN = 43
RSI_BULL_MAX = 68
RSI_BEAR_MIN = 32
RSI_BEAR_MAX = 57

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

api = None
otc_assets = []
last_signal = {}
last_trade = {}
total_trades = 0
start_time = time.time()
last_discovery = 0
last_status = 0


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def number(value, default=0.0):
    try:
        value = float(value)
        if math.isfinite(value):
            return value
    except Exception:
        pass
    return default


def asset_key(name):
    return "".join(
        c for c in str(name or "").upper()
        if c.isalnum()
    )


def clean_name(name):
    name = str(name or "").strip()
    if name.upper().startswith("FRONT."):
        name = name[6:]
    elif name.upper().startswith("FRONT_"):
        name = name[6:]
    return name.split(".")[-1].strip()


def is_otc(name):
    text = str(name or "").upper().strip()
    return (
        text.endswith("-OTC")
        or text.endswith("_OTC")
        or text.endswith(" OTC")
    )


def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        print("[TELEGRAM DISABLED]", message)
        return

    try:
        response = requests.post(
            "https://api.telegram.org/bot"
            + TG_TOKEN + "/sendMessage",
            data={
                "chat_id": TG_CHAT,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=15
        )
        if not response.ok:
            print("Telegram error:", response.status_code)
    except Exception as exc:
        print("Telegram error:", repr(exc))


def log_event(asset, direction, order_id, status, score=""):
    file_name = "zeta_trade_log.csv"
    exists = os.path.exists(file_name)

    with open(
        file_name, "a", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file)

        if not exists:
            writer.writerow([
                "time_utc", "asset", "direction",
                "order_id", "status", "score"
            ])

        writer.writerow([
            now_utc(), asset, direction,
            order_id, status, score
        ])


# ============================================================
# CONNECTION
# ============================================================

def connect_iq():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        print("Missing IQ_EMAIL or IQ_PASSWORD secret.")
        return False

    try:
        api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
        connected, reason = api.connect()

        print("Connection:", connected, reason)

        if not connected:
            return False

        api.change_balance(BALANCE_MODE)
        print("Balance mode:", BALANCE_MODE)
        return True

    except Exception as exc:
        print("Connection error:", repr(exc))
        return False


def reconnect():
    global api

    try:
        if api is not None:
            api.close()
    except Exception:
        pass

    time.sleep(1)
    return connect_iq()


# ============================================================
# MARKET DISCOVERY
# Only binary/turbo assets are candidates for api.buy().
# ============================================================

def discover_otc():
    global last_discovery

    found = {}
    data = None

    for method_name in ("get_all_init_v2", "get_all_init"):
        try:
            method = getattr(api, method_name)
            data = method()
            if isinstance(data, dict) and data:
                print("Initialization method:", method_name)
                break
        except Exception as exc:
            print(method_name, "error:", repr(exc))
            data = None

    if not isinstance(data, dict):
        print("Initialization data unavailable.")
        return []

    if isinstance(data.get("result"), dict):
        data = data["result"]

    for market in ("turbo", "binary"):
        section = data.get(market, {})

        if not isinstance(section, dict):
            continue

        actives = section.get("actives", {})

        if not isinstance(actives, dict):
            continue

        for raw_id, info in actives.items():
            if not isinstance(info, dict):
                continue

            name = clean_name(info.get("name", ""))

            if not is_otc(name):
                continue

            if not info.get("enabled", False):
                continue

            if info.get("is_suspended", False):
                continue

            try:
                active_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            # Register the ID for API methods using OP_code.ACTIVES.
            try:
                OP_code.ACTIVES[name] = active_id
            except Exception:
                pass

            key = asset_key(name)

            if key not in found:
                found[key] = {
                    "asset": name,
                    "active_id": active_id,
                    "market": market
                }

            if len(found) >= MAX_OTC_ASSETS:
                break

        if len(found) >= MAX_OTC_ASSETS:
            break

    result = list(found.values())
    result.sort(key=lambda item: item["asset"])

    print("OTC binary/turbo candidates:", len(result))

    for item in result:
        print(
            item["asset"],
            "|", item["market"],
            "| ID:", item["active_id"]
        )

    last_discovery = time.time()
    return result


# ============================================================
# CANDLES
# ============================================================

def get_candles(asset, interval, count):
    try:
        raw = api.get_candles(
            asset,
            interval,
            count,
            int(time.time())
        )
    except Exception as exc:
        print("Candle error:", asset, repr(exc))
        return []

    if not isinstance(raw, list):
        return []

    result = []

    try:
        server_time = api.get_server_timestamp()
        if not server_time:
            server_time = time.time()
    except Exception:
        server_time = time.time()

    for item in raw:
        if not isinstance(item, dict):
            continue

        candle = {
            "from": number(item.get("from")),
            "open": number(item.get("open")),
            "close": number(item.get("close")),
            "low": number(item.get("min", item.get("low"))),
            "high": number(item.get("max", item.get("high")))
        }

        if candle["from"] <= 0:
            continue

        if min(
            candle["open"],
            candle["close"],
            candle["low"],
            candle["high"]
        ) <= 0:
            continue

        # Do not evaluate a candle that is still forming.
        if candle["from"] + interval > server_time:
            continue

        result.append(candle)

    result.sort(key=lambda item: item["from"])
    return result[-count:]


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2.0 / (period + 1.0)

    for i in range(period, len(values)):
        current = (
            values[i] - current
        ) * multiplier + current
        result[i] = current

    return result


def atr(candles, period=14):
    result = [None] * len(candles)
    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        previous = candles[i - 1]["close"]

        trs.append(max(
            high - low,
            abs(high - previous),
            abs(low - previous)
        ))

    if len(trs) < period:
        return result

    current = sum(trs[:period]) / period
    result[period] = current

    for i in range(period + 1, len(candles)):
        current = (
            current * (period - 1) + trs[i - 1]
        ) / period
        result[i] = current

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0.0) for x in changes]
    losses_list = [max(-x, 0.0) for x in changes]

    gain = sum(gains[:period]) / period
    loss = sum(losses_list[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    result[period] = calc(gain, loss)

    for j in range(period, len(gains)):
        gain = (gain * (period - 1) + gains[j]) / period
        loss = (
            loss * (period - 1) + losses_list[j]
        ) / period
        result[j + 1] = calc(gain, loss)

    return result


def adx(candles, period=14):
    size = len(candles)
    result = [None] * size

    if size < period * 2 + 2:
        return result

    tr = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size

    for i in range(1, size):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_high = candles[i - 1]["high"]
        prev_low = candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]

        tr[i] = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        up = high - prev_high
        down = prev_low - low

        if up > down and up > 0:
            plus_dm[i] = up

        if down > up and down > 0:
            minus_dm[i] = down

    atr_value = sum(tr[1:period + 1]) / period
    plus = sum(plus_dm[1:period + 1]) / period
    minus = sum(minus_dm[1:period + 1]) / period

    dx_values = []
    current_adx = None

    for i in range(period + 1, size):
        atr_value = (
            atr_value * (period - 1) + tr[i]
        ) / period

        plus = (
            plus * (period - 1) + plus_dm[i]
        ) / period

        minus = (
            minus * (period - 1) + minus_dm[i]
        ) / period

        if atr_value <= 0:
            continue

        plus_di = 100.0 * plus / atr_value
        minus_di = 100.0 * minus / atr_value
        total = plus_di + minus_di

        if total <= 0:
            continue

        dx = 100.0 * abs(plus_di - minus_di) / total
        dx_values.append(dx)

        if len(dx_values) == period:
            current_adx = sum(dx_values) / period
        elif len(dx_values) > period:
            current_adx = (
                current_adx * (period - 1) + dx
            ) / period

        if current_adx is not None:
            result[i] = current_adx

    return result


# ============================================================
# CANDLE PATTERNS
# ============================================================

def body_ratio(candle):
    size = candle["high"] - candle["low"]
    if size <= 0:
        return 0.0

    return abs(
        candle["close"] - candle["open"]
    ) / size


def bull_rejection(c):
    body = abs(c["close"] - c["open"])
    lower = min(c["open"], c["close"]) - c["low"]
    size = c["high"] - c["low"]

    return (
        size > 0
        and lower >= body
        and c["close"] >= c["low"] + size * 0.55
    )


def bear_rejection(c):
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    size = c["high"] - c["low"]

    return (
        size > 0
        and upper >= body
        and c["close"] <= c["high"] - size * 0.55
    )


def bull_engulf(previous, current):
    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bear_engulf(previous, current):
    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


# ============================================================
# ZETA TREND-PULLBACK STRATEGY
# ============================================================

def evaluate(asset, candles5, candles1):
    if len(candles5) < 80 or len(candles1) < 80:
        return None

    close5 = [c["close"] for c in candles5]
    close1 = [c["close"] for c in candles1]

    fast5 = ema(close5, EMA_FAST)
    slow5 = ema(close5, EMA_SLOW)
    atr5 = atr(candles5, ATR_PERIOD)
    adx5 = adx(candles5, ADX_PERIOD)

    i5 = len(candles5) - 1
    p5 = i5 - 1

    if any(x is None for x in (
        fast5[i5], slow5[i5],
        fast5[p5], slow5[p5],
        atr5[i5], adx5[i5]
    )):
        return None

    bull_trend = (
        fast5[i5] > slow5[i5]
        and fast5[p5] >= slow5[p5]
        and fast5[i5] > fast5[p5]
    )

    bear_trend = (
        fast5[i5] < slow5[i5]
        and fast5[p5] <= slow5[p5]
        and fast5[i5] < fast5[p5]
    )

    if not (bull_trend or bear_trend):
        return None

    if adx5[i5] < MIN_ADX or atr5[i5] <= 0:
        return None

    fast1 = ema(close1, EMA_FAST)
    slow1 = ema(close1, EMA_SLOW)
    atr1 = atr(candles1, ATR_PERIOD)
    rsi1 = rsi(close1, RSI_PERIOD)

    i1 = len(candles1) - 1
    p1 = i1 - 1

    if any(x is None for x in (
        fast1[i1], slow1[i1],
        fast1[p1], slow1[p1],
        atr1[i1], rsi1[i1]
    )):
        return None

    current = candles1[i1]
    previous = candles1[p1]
    price = current["close"]
    volatility = atr1[i1]

    if volatility <= 0:
        return None

    distance = abs(price - fast1[i1])
    pullback = distance / volatility

    if pullback < MIN_PULLBACK_ATR:
        return None

    if pullback > MAX_PULLBACK_ATR:
        return None

    if pullback > MAX_EXTENSION_ATR:
        return None

    recent = candles1[-30:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)
    tolerance = volatility * ZONE_TOLERANCE_ATR

    near_support = abs(price - support) <= tolerance
    near_resistance = abs(price - resistance) <= tolerance
    near_ema = distance <= tolerance

    room_up = (resistance - price) / volatility
    room_down = (price - support) / volatility

    bullish = current["close"] > current["open"]
    bearish = current["close"] < current["open"]

    bull_confirm = (
        bullish
        and body_ratio(current) >= MIN_BODY_RATIO
        and (
            current["close"] > previous["high"]
            or bull_engulf(previous, current)
            or bull_rejection(current)
        )
    )

    bear_confirm = (
        bearish
        and body_ratio(current) >= MIN_BODY_RATIO
        and (
            current["close"] < previous["low"]
            or bear_engulf(previous, current)
            or bear_rejection(current)
        )
    )

    score = 20
    reasons = []

    if bull_trend:
        direction = "CALL"
        reasons.append("5M bullish trend")

        if fast1[i1] >= slow1[i1] and fast1[i1] >= fast1[p1]:
            score += 10
            reasons.append("1M EMA structure")

        if near_support or near_ema:
            score += 15
            reasons.append("pullback zone")

        if bull_rejection(current):
            score += 20
            reasons.append("bullish rejection")

        if bull_confirm:
            score += 10
            reasons.append("1M confirmation")

        if price > previous["close"]:
            score += 5
            reasons.append("bullish momentum")

        if RSI_BULL_MIN <= rsi1[i1] <= RSI_BULL_MAX:
            score += 5
            reasons.append("RSI valid")

        if room_up >= MIN_ROOM_ATR:
            score += 5
            reasons.append("room available")

        if bull_engulf(previous, current):
            score += 5

        valid = (
            (near_support or near_ema)
            and bull_rejection(current)
            and bull_confirm
            and price > previous["close"]
            and room_up >= MIN_ROOM_ATR
        )

    else:
        direction = "PUT"
        reasons.append("5M bearish trend")

        if fast1[i1] <= slow1[i1] and fast1[i1] <= fast1[p1]:
            score += 10
            reasons.append("1M EMA structure")

        if near_resistance or near_ema:
            score += 15
            reasons.append("pullback zone")

        if bear_rejection(current):
            score += 20
            reasons.append("bearish rejection")

        if bear_confirm:
            score += 10
            reasons.append("1M confirmation")

        if price < previous["close"]:
            score += 5
            reasons.append("bearish momentum")

        if RSI_BEAR_MIN <= rsi1[i1] <= RSI_BEAR_MAX:
            score += 5
            reasons.append("RSI valid")

        if room_down >= MIN_ROOM_ATR:
            score += 5
            reasons.append("room available")

        if bear_engulf(previous, current):
            score += 5

        valid = (
            (near_resistance or near_ema)
            and bear_rejection(current)
            and bear_confirm
            and price < previous["close"]
            and room_down >= MIN_ROOM_ATR
        )

    if not valid or score < MIN_SCORE:
        return None

    candle_time = current["from"]

    if candle_time == last_signal.get(asset):
        return None

    if time.time() - last_trade.get(asset, 0) < ASSET_LOCK_SECONDS:
        return None

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "price": price,
        "adx": adx5[i5],
        "rsi": rsi1[i1],
        "pullback": pullback,
        "room_up": room_up,
        "room_down": room_down,
        "candle_time": candle_time,
        "reasons": reasons
    }


# ============================================================
# SIGNAL AND ORDER
# ============================================================

def execute(signal):
    global total_trades

    asset = signal["asset"]
    direction = signal["direction"]

    message = (
        "📊 <b>ZETA V2.4 SIGNAL</b>\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Score: " + str(signal["score"]) + "/100\n"
        "5M trend / 1M entry\n"
        "Expiry: 2 minutes\n"
        "Practice stake: $1\n"
        "Reasons: " + ", ".join(signal["reasons"])
    )

    print(message)
    telegram(message)

    active_id = OP_code.ACTIVES.get(asset)

    if active_id is None:
        print("No registered active ID:", asset)
        log_event(asset, direction, "", "NO_ACTIVE_ID", signal["score"])
        return

    if total_trades >= TARGET_TRADES:
        return

    try:
        result = api.buy(
            STAKE,
            asset,
            direction.lower(),
            EXPIRY_MINUTES
        )

        print("Order response:", repr(result))

    except Exception as exc:
        print("Order error:", repr(exc))
        log_event(asset, direction, "", "ORDER_ERROR", signal["score"])
        return

    if not isinstance(result, (tuple, list)) or len(result) < 2:
        log_event(asset, direction, "", "UNCONFIRMED", signal["score"])
        return

    accepted = bool(result[0])
    order_id = result[1]

    if not accepted:
        log_event(asset, direction, order_id, "REJECTED", signal["score"])
        telegram("⚠️ Order rejected for " + asset)
        return

    total_trades += 1
    last_trade[asset] = time.time()

    log_event(asset, direction, order_id, "ACCEPTED_MANUAL_RESULT", signal["score"])

    telegram(
        "✅ <b>PRACTICE ORDER ACCEPTED</b>\n"
        "Trade: " + str(total_trades) + "/" + str(TARGET_TRADES) + "\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Order ID: " + str(order_id) + "\n"
        "Result tracking: manual"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    global otc_assets, last_discovery, last_status

    print("=" * 55)
    print("ZETA V2.4 PRACTICE SCANNER")
    print("Strategy: 5M trend + 1M entry")
    print("Stake: $1")
    print("Expiry: 2 minutes")
    print("Target: 50 accepted orders")
    print("=" * 55)

    while not connect_iq():
        print("Retrying connection in 30 seconds.")
        time.sleep(RECONNECT_INTERVAL)

    telegram(
        "🟢 <b>ZETA V2.4 STARTED</b>\n"
        "Account: PRACTICE\n"
        "Stake: $1\n"
        "Expiry: 2 minutes\n"
        "Target: 50 accepted orders\n"
        "Results: manual tracking"
    )

    while total_trades < TARGET_TRADES:
        try:
            try:
                connected = api.check_connect()
            except Exception:
                connected = False

            if not connected:
                print("Connection lost. Reconnecting.")
                if not reconnect():
                    time.sleep(RECONNECT_INTERVAL)
                    continue
                otc_assets = []
                last_discovery = 0

            if (
                not otc_assets
                or time.time() - last_discovery >= DISCOVERY_INTERVAL
            ):
                otc_assets = discover_otc()

                if not otc_assets:
                    print("No enabled binary/turbo OTC assets found.")
                    print("Retrying in 30 seconds.")
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            for item in list(otc_assets):
                if total_trades >= TARGET_TRADES:
                    break

                asset = item["asset"]

                candles5 = get_candles(asset, TF5, COUNT5)
                if len(candles5) < 80:
                    continue

                candles1 = get_candles(asset, TF1, COUNT1)
                if len(candles1) < 80:
                    continue

                signal = evaluate(asset, candles5, candles1)

                if signal is None:
                    continue

                last_signal[asset] = signal["candle_time"]
                execute(signal)
                time.sleep(1)

            if time.time() - last_status >= STATUS_INTERVAL:
                last_status = time.time()
                print(
                    "HEARTBEAT",
                    now_utc(),
                    "| OTC assets:", len(otc_assets),
                    "| Accepted orders:", total_trades,
                    "/", TARGET_TRADES
                )

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("Stopped by user.")
            break

        except Exception as exc:
            print("Main loop error:", repr(exc))
            traceback.print_exc()
            time.sleep(5)

    telegram(
        "🏁 <b>ZETA V2.4 TEST STOPPED</b>\n"
        "Accepted orders: " + str(total_trades) + "\n"
        "Results must be reviewed manually in IQ Option.\n"
        "CSV: zeta_trade_log.csv"
    )

    try:
        api.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
