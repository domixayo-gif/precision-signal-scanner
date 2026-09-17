import os
import time
import math
import requests
from datetime import datetime, timezone

from storage import read_tracker, update_tracker


TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://api.exchange.coinbase.com"
TELEGRAM = "https://api.telegram.org/bot" + TOKEN

ASSETS = [
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "ADA",
    "LINK",
    "TON",
    "AVAX",
    "DOGE",
    "DOT",
    "LTC",
    "POL",
]

EXPIRY_MINUTES = 5
MIN_SCORE = 80
COOLDOWN_MINUTES = 5


def send_message(text):
    response = requests.post(
        TELEGRAM + "/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
        },
        timeout=30,
    )
    response.raise_for_status()


def get_candles(symbol, seconds):
    url = BASE + "/products/" + symbol + "-USD/candles"

    try:
        response = requests.get(
            url,
            params={"granularity": seconds},
            headers={
                "User-Agent": "precision-scanner-v3.5"
            },
            timeout=20,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if not isinstance(data, list):
            return []

        rows = []

        for row in data:
            if isinstance(row, list) and len(row) >= 6:
                rows.append(row)

        rows.sort(key=lambda x: x[0])

        if len(rows) > 1:
            rows = rows[:-1]

        return rows

    except Exception:
        return []


def ema(values, period):
    if len(values) < period:
        return None

    total = 0.0

    for value in values[:period]:
        total += value

    result = total / period
    multiplier = 2.0 / (period + 1.0)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1.0 - multiplier)
        )

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0

    start = len(values) - period - 1

    for i in range(start, len(values) - 1):
        change = values[i + 1] - values[i]

        if change > 0:
            gains += change
        else:
            losses += abs(change)

    average_gain = gains / period
    average_loss = losses / period

    if average_loss == 0:
        return 100.0

    rs = average_gain / average_loss

    return 100.0 - (100.0 / (1.0 + rs))


def true_ranges(rows):
    ranges = []

    for i in range(len(rows)):
        high = float(rows[i][2])
        low = float(rows[i][1])

        if i == 0:
            previous_close = float(rows[i][4])
        else:
            previous_close = float(rows[i - 1][4])

        first = high - low
        second = abs(high - previous_close)
        third = abs(low - previous_close)

        value = max(first, second, third)
        ranges.append(value)

    return ranges


def atr(rows, period=14):
    ranges = true_ranges(rows)

    if len(ranges) < period:
        return None

    return sum(ranges[-period:]) / period


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None

    return fast - slow


def momentum(values):
    if len(values) < 5:
        return 0

    recent = values[-5:]
    up = 0
    down = 0

    for i in range(1, len(recent)):
        if recent[i] > recent[i - 1]:
            up += 1
        elif recent[i] < recent[i - 1]:
            down += 1

    if up > down:
        return 1

    if down > up:
        return -1

    return 0


def adx_dmi(rows, period=14):
    if len(rows) < period + 2:
        return None, None, None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(rows)):
        high = float(rows[i][2])
        low = float(rows[i][1])

        old_high = float(rows[i - 1][2])
        old_low = float(rows[i - 1][1])
        old_close = float(rows[i - 1][4])

        tr1 = high - low
        tr2 = abs(high - old_close)
        tr3 = abs(low - old_close)

        trs.append(max(tr1, tr2, tr3))

        move_up = high - old_high
        move_down = old_low - low

        if move_up > move_down and move_up > 0:
            plus_dm.append(move_up)
        else:
            plus_dm.append(0.0)

        if move_down > move_up and move_down > 0:
            minus_dm.append(move_down)
        else:
            minus_dm.append(0.0)

    if len(trs) < period:
        return None, None, None

    recent_tr = trs[-period:]
    recent_plus = plus_dm[-period:]
    recent_minus = minus_dm[-period:]

    average_tr = sum(recent_tr) / period

    if average_tr == 0:
        return 0.0, 0.0, 0.0

    plus_di = (
        sum(recent_plus)
        / average_tr
        * 100.0
    )

    minus_di = (
        sum(recent_minus)
        / average_tr
        * 100.0
    )

    denominator = plus_di + minus_di

    if denominator == 0:
        dx = 0.0
    else:
        dx = (
            abs(plus_di - minus_di)
            / denominator
            * 100.0
        )

    return dx, plus_di, minus_di


def structure(rows):
    if len(rows) < 12:
        return 0

    recent = rows[-6:]
    older = rows[-12:-6]

    recent_high = max(
        float(row[2]) for row in recent
    )

    recent_low = min(
        float(row[1]) for row in recent
    )

    older_high = max(
        float(row[2]) for row in older
    )

    older_low = min(
        float(row[1]) for row in older
    )

    if (
        recent_high > older_high
        and recent_low > older_low
    ):
        return 1

    if (
        recent_high < older_high
        and recent_low < older_low
    ):
        return -1

    return 0


def candle_direction(row):
    open_price = float(row[3])
    close_price = float(row[4])

    if close_price > open_price:
        return 1

    if close_price < open_price:
        return -1

    return 0


def analyze(symbol):
    candles_5m = get_candles(symbol, 300)
    candles_1m = get_candles(symbol, 60)

    if len(candles_5m) < 70:
        return None, "5M data unavailable"

    if len(candles_1m) < 50:
        return None, "1M data unavailable"

    prices5 = []
    prices1 = []

    for row in candles_5m:
        prices5.append(float(row[4]))

    for row in candles_1m:
        prices1.append(float(row[4]))

    price5 = prices5[-1]
    price1 = prices1[-1]

    ema20_5 = ema(prices5, 20)
    ema50_5 = ema(prices5, 50)
    ema20_old = ema(prices5[:-5], 20)

    ema9_1 = ema(prices1, 9)
    ema21_1 = ema(prices1, 21)

    rsi5 = rsi(prices5, 14)
    rsi1 = rsi(prices1, 14)

    macd5 = macd(prices5)
    macd5_old = macd(prices5[:-3])

    macd1 = macd(prices1)
    macd1_old = macd(prices1[:-3])

    atr5 = atr(candles_5m, 14)
    atr1 = atr(candles_1m, 14)

    adx5, plus_di, minus_di = adx_dmi(
        candles_5m,
        14,
    )

    struct = structure(candles_5m)

    values = [
        ema20_5,
        ema50_5,
        ema20_old,
        ema9_1,
        ema21_1,
        rsi5,
        rsi1,
        macd5,
        macd5_old,
        macd1,
        macd1_old,
        atr5,
        atr1,
        adx5,
        plus_di,
        minus_di,
    ]

    for value in values:
        if value is None:
            return None, "indicator error"

    if atr5 <= 0 or atr1 <= 0:
        return None, "invalid volatility"

    call = 0
    put = 0

    trend_score = 0
    structure_score = 0
    adx_score = 0
    macd_score = 0
    rsi_score = 0
    entry_score = 0
    pullback_score = 0
    candle_score = 0
    room_score = 0
    extension_score = 0

    if price5 > ema20_5:
        trend_score += 10

    if ema20_5 > ema50_5:
        trend_score += 10

    if price5 < ema20_5:
        trend_score -= 10

    if ema20_5 < ema50_5:
        trend_score -= 10

    if trend_score > 0:
        call += 20
    elif trend_score < 0:
        put += 20

    if struct == 1:
        call += 10
        structure_score = 10

    elif struct == -1:
        put += 10
        structure_score = 10

    if adx5 >= 15:
        if plus_di > minus_di:
            call += 10
            adx_score = 10
        elif minus_di > plus_di:
            put += 10
            adx_score = 10
        else:
            adx_score = 5
    elif adx5 >= 12:
        if plus_di > minus_di:
            call += 5
            adx_score = 5
        elif minus_di > plus_di:
            put += 5
            adx_score = 5

    if macd5 > 0 and macd5 >= macd5_old:
        call += 5
        macd_score += 5

    if macd5 < 0 and macd5 <= macd5_old:
        put += 5
        macd_score += 5

    if macd1 > 0 and macd1 >= macd1_old:
        call += 5
        macd_score += 5

    if macd1 < 0 and macd1 <= macd1_old:
        put += 5
        macd_score += 5

    if rsi5 >= 50 and rsi5 < 70:
        call += 5
        rsi_score += 5

    if rsi1 >= 48 and rsi1 < 70:
        call += 5
        rsi_score += 5

    if rsi5 <= 50 and rsi5 > 30:
        put += 5
        rsi_score += 5

    if rsi1 <= 52 and rsi1 > 30:
        put += 5
        rsi_score += 5

    if price1 > ema9_1:
        call += 7
        entry_score += 7

    if price1 < ema9_1:
        put += 7
        entry_score += 7

    if ema9_1 > ema21_1:
        call += 8
        entry_score += 8

    if ema9_1 < ema21_1:
        put += 8
        entry_score += 8

    distance1 = abs(price1 - ema20_5)

    if atr5 > 0:
        extension_ratio = distance1 / atr5
    else:
        extension_ratio = 99.0

    if extension_ratio <= 1.8:
        if call > put:
            call += 5
            extension_score = 5
        elif put > call:
            put += 5
            extension_score = 5

    last = candles_1m[-1]

    last_open = float(last[3])
    last_high = float(last[2])
    last_low = float(last[1])
    last_close = float(last[4])

    last_range = last_high - last_low

    body = abs(last_close - last_open)

    if last_range > 0:
        body_ratio = body / last_range
    else:
        body_ratio = 0

    if last_range <= atr1 * 1.8:
        if body_ratio >= 0.45:
            if last_close > last_open:
                call += 5
                candle_score = 5
            elif last_close < last_open:
                put += 5
                candle_score = 5

    recent_low = min(
        float(row[1])
        for row in candles_5m[-12:]
    )

    recent_high = max(
        float(row[2])
        for row in candles_5m[-12:]
    )

    if call > put:
        room = recent_high - price1

        if room >= atr5 * 0.8:
            call += 5
            room_score = 5

    elif put > call:
        room = price1 - recent_low

        if room >= atr5 * 0.8:
            put += 5
            room_score = 5

    recent_momentum = momentum(prices1)

    if recent_momentum == 1:
        call += 5
        pullback_score = 5

    elif recent_momentum == -1:
        put += 5
        pullback_score = 5

    if call > put:
        direction = "CALL"
        score = call
    elif put > call:
        direction = "PUT"
        score = put
    else:
        return None, "score tied"

    if direction == "CALL":
        if price5 <= ema20_5:
            return None, "bullish trend not confirmed"

        if ema20_5 <= ema50_5:
            return None, "5M bearish structure"

        if plus_di <= minus_di and adx5 >= 15:
            return None, "DMI conflict"

        if rsi5 >= 72 or rsi1 >= 75:
            return None, "CALL RSI too high"

        if recent_momentum == -1:
            return None, "1M momentum conflict"

    if direction == "PUT":
        if price5 >= ema20_5:
            return None, "bearish trend not confirmed"

        if ema20_5 >= ema50_5:
            return None, "5M bullish structure"

        if minus_di <= plus_di and adx5 >= 15:
            return None, "DMI conflict"

        if rsi5 <= 28 or rsi1 <= 25:
            return None, "PUT RSI too low"

        if recent_momentum == 1:
            return None, "1M momentum conflict"

    volatility_ratio = atr1 / atr5

    if volatility_ratio > 0.55:
        return None, "extreme volatility"

    if score < MIN_SCORE:
        return None, "score " + str(score) + "/100"

    entry_ts = int(candles_1m[-1][0])

    signal = {
        "symbol": symbol,
        "asset": symbol + " OTC",
        "direction": direction,
        "score": score,
        "price": round(price1, 8),
        "rsi5": round(rsi5, 1),
        "rsi1": round(rsi1, 1),
        "adx": round(adx5, 1),
        "entry_ts": entry_ts,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "expiry_minutes": EXPIRY_MINUTES,
        "expiry_at": datetime.fromtimestamp(
            entry_ts + 300,
            timezone.utc,
        ).isoformat(),
        "result": "PENDING",
    }

    return signal, None


def cooldown_allowed(
    data,
    symbol,
    direction,
    entry_ts,
):
    cutoff = entry_ts - (
        COOLDOWN_MINUTES * 60
    )

    signals = data.get("signals", [])

    for old in reversed(signals):
        if old.get("symbol") != symbol:
            continue

        old_ts = int(old.get("entry_ts", 0))

        if old_ts < cutoff:
            break

        if old.get("direction") == direction:
            return False

    return True


def save_signals(signals, scan_id):
    if not signals:
        return

    def mutate(data):
        existing = set()

        for old in data.get("signals", []):
            existing.add(old.get("id"))

        for signal in signals:
            if signal["id"] not in existing:
                data["signals"].append(signal)

        data["signals"] = data["signals"][-1000:]

        return data

    update_tracker(
        mutate,
        "Add V3.5 signals " + scan_id,
    )


def main():
    scan_id = os.getenv("GITHUB_RUN_ID")

    if not scan_id:
        scan_id = str(int(time.time()))

    data, _ = read_tracker()

    found = []
    rejected = []
    unavailable = []

    for symbol in ASSETS:
        try:
            signal, reason = analyze(symbol)

            if signal:
                signal["id"] = (
                    "V35-"
                    + symbol
                    + "-"
                    + signal["direction"]
                    + "-"
                    + str(signal["entry_ts"])
                )

                if cooldown_allowed(
                    data,
                    symbol,
                    signal["direction"],
                    signal["entry_ts"],
                ):
                    found.append(signal)
                else:
                    rejected.append(
                        symbol + ": cooldown"
                    )

            elif reason in (
                "5M data unavailable",
                "1M data unavailable",
                "indicator error",
                "invalid volatility",
            ):
                unavailable.append(
                    symbol + "(" + reason + ")"
                )

            else:
                rejected.append(
                    symbol + ": " + str(reason)
                )

        except Exception as exc:
            unavailable.append(
                symbol
                + "("
                + type(exc).__name__
                + ": "
                + str(exc)
                + ")"
            )

    save_signals(found, scan_id)

    now = datetime.now(timezone.utc)

    timestamp = (
        now.isoformat()
        .replace("T", " ")
        .replace("+00:00", " UTC")
    )

    lines = [
        "🟦 PRECISION SCANNER V3.5",
        "Scan: " + timestamp,
        "Data: Coinbase spot proxy",
        "Trend: 5M",
        "Entry: 1M",
        "Expiry: 5 MINUTES",
        "Minimum score: 80/100",
        "Assets analyzed: " + str(len(ASSETS)),
        "",
    ]

    if found:
        lines.append(
            "🚨 QUALIFIED SIGNALS"
        )
        lines.append("")

        for signal in found:
            if signal["direction"] == "CALL":
                icon = "🟢 CALL"
            else:
                icon = "🔴 PUT"

            lines.append(
                icon + " • " + signal["asset"]
            )
            lines.append(
                "Score: "
                + str(signal["score"])
                + "/100"
            )
            lines.append(
                "Price: "
                + str(signal["price"])
            )
            lines.append(
                "ADX: "
                + str(signal["adx"])
            )
            lines.append(
                "5M RSI: "
                + str(signal["rsi5"])
                + " | 1M RSI: "
                + str(signal["rsi1"])
            )
            lines.append(
                "Signal ID: "
                + signal["id"]
            )
            lines.append(
                "Expiry: 5 minutes"
            )
            lines.append("")

    else:
        lines.append("⚪ NO TRADE")
        lines.append(
            "No asset passed the V3.5 qualification."
        )

        if rejected:
            lines.append("")
            lines.append(
                "Top rejection reasons:"
            )

            for item in rejected[:8]:
                lines.append("• " + item)

    if unavailable:
        lines.append("")
        lines.append(
            "⚠️ MARKET/DATA WARNINGS"
        )

        for item in unavailable[:8]:
            lines.append("• " + item)

    lines.append("")
    lines.append(
        "Commands: /win ID | /loss ID | /stats"
    )
    lines.append("")
    lines.append(
        "⚠️ DEMO/TESTING ONLY."
    )
    lines.append(
        "Coinbase proxy may differ from "
        "Pocket Option OTC pricing."
    )

    send_message("\n".join(lines))


if __name__ == "__main__":
    main()
