import os
import time
import traceback
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
    "TRX",
    "LINK",
    "TON",
    "AVAX",
    "DOGE",
    "DOT",
    "LTC",
    "POL",
]

MIN_SCORE = 9
EXPIRY_MINUTES = 5
COOLDOWN_MINUTES = 15


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
    product = symbol + "-USD"
    url = BASE + "/products/" + product + "/candles"

    try:
        response = requests.get(
            url,
            params={"granularity": seconds},
            headers={"User-Agent": "precision-signal-scanner-v2.2"},
            timeout=20,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if not isinstance(data, list):
            return []

        clean = []

        for row in data:
            if isinstance(row, list) and len(row) >= 6:
                clean.append(row)

        clean.sort(key=lambda row: row[0])

        if len(clean) > 1:
            clean = clean[:-1]

        return clean

    except Exception:
        return []


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (
            price * multiplier
            + value * (1.0 - multiplier)
        )

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    start = len(values) - period - 1

    for index in range(start, len(values) - 1):
        change = values[index + 1] - values[index]

        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss

    return 100.0 - (
        100.0 / (1.0 + relative_strength)
    )


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None

    return fast - slow


def average_range(rows, period=14):
    if len(rows) < period:
        return None

    ranges = []

    for row in rows[-period:]:
        high = float(row[2])
        low = float(row[1])
        ranges.append(high - low)

    return sum(ranges) / len(ranges)


def momentum(values):
    if len(values) < 4:
        return 0

    up = 0
    down = 0
    recent = values[-4:]

    for index in range(1, len(recent)):
        if recent[index] > recent[index - 1]:
            up += 1
        elif recent[index] < recent[index - 1]:
            down += 1

    if up > down:
        return 1

    if down > up:
        return -1

    return 0


def analyze(symbol):
    candles_5m = get_candles(symbol, 300)
    candles_1m = get_candles(symbol, 60)

    if len(candles_5m) < 60:
        return None, "5M data unavailable"

    if len(candles_1m) < 40:
        return None, "1M data unavailable"

    prices_5m = []
    prices_1m = []

    for row in candles_5m:
        prices_5m.append(float(row[4]))

    for row in candles_1m:
        prices_1m.append(float(row[4]))

    price_5m = prices_5m[-1]
    price_1m = prices_1m[-1]

    ema20_5m = ema(prices_5m, 20)
    ema50_5m = ema(prices_5m, 50)
    ema20_old = ema(prices_5m[:-2], 20)

    ema9_1m = ema(prices_1m, 9)
    ema21_1m = ema(prices_1m, 21)

    rsi_5m = rsi(prices_5m, 14)
    rsi_1m = rsi(prices_1m, 14)

    macd_5m = macd(prices_5m)
    macd_5m_old = macd(prices_5m[:-1])

    macd_1m = macd(prices_1m)
    macd_1m_old = macd(prices_1m[:-1])

    atr_5m = average_range(candles_5m, 14)
    atr_1m = average_range(candles_1m, 14)

    values = [
        ema20_5m,
        ema50_5m,
        ema20_old,
        ema9_1m,
        ema21_1m,
        rsi_5m,
        rsi_1m,
        macd_5m,
        macd_5m_old,
        macd_1m,
        macd_1m_old,
        atr_5m,
        atr_1m,
    ]

    for value in values:
        if value is None:
            return None, "indicator error"

    call_score = 0
    put_score = 0

    if price_5m > ema20_5m:
        call_score += 2
    elif price_5m < ema20_5m:
        put_score += 2

    if ema20_5m > ema50_5m:
        call_score += 2
    elif ema20_5m < ema50_5m:
        put_score += 2

    if ema20_5m > ema20_old:
        call_score += 1
    elif ema20_5m < ema20_old:
        put_score += 1

    if macd_5m > 0 and macd_5m >= macd_5m_old:
        call_score += 1
    elif macd_5m < 0 and macd_5m <= macd_5m_old:
        put_score += 1

    if rsi_5m > 50 and rsi_1m > 50:
        call_score += 1
    elif rsi_5m < 50 and rsi_1m < 50:
        put_score += 1

    if price_1m > ema9_1m:
        call_score += 1
    elif price_1m < ema9_1m:
        put_score += 1

    if ema9_1m > ema21_1m:
        call_score += 1
    elif ema9_1m < ema21_1m:
        put_score += 1

    if macd_1m > 0 and macd_1m >= macd_1m_old:
        call_score += 1
    elif macd_1m < 0 and macd_1m <= macd_1m_old:
        put_score += 1

    current_momentum = momentum(prices_1m)

    if current_momentum == 1:
        call_score += 1
    elif current_momentum == -1:
        put_score += 1

    if call_score > put_score:
        direction = "CALL"
        score = call_score
    elif put_score > call_score:
        direction = "PUT"
        score = put_score
    else:
        return None, "score tied"

    if direction == "CALL":
        if price_5m <= ema20_5m:
            return None, "5M trend conflict"

        if ema20_5m <= ema50_5m:
            return None, "5M trend conflict"

    if direction == "PUT":
        if price_5m >= ema20_5m:
            return None, "5M trend conflict"

        if ema20_5m >= ema50_5m:
            return None, "5M trend conflict"

    distance = abs(price_1m - ema9_1m)

    if distance > atr_1m * 2.2:
        return None, "entry extended"

    if direction == "CALL":
        if rsi_5m >= 75 or rsi_1m >= 75:
            return None, "CALL RSI too high"

    if direction == "PUT":
        if rsi_5m <= 25 or rsi_1m <= 25:
            return None, "PUT RSI too low"

    last_range = (
        float(candles_1m[-1][2])
        - float(candles_1m[-1][1])
    )

    if last_range > atr_1m * 2.8:
        return None, "entry candle too large"

    slope = abs(ema20_5m - ema20_old)

    if slope < atr_5m * 0.02 and score < 10:
        return None, "5M trend too flat"

    if score < MIN_SCORE:
        return None, "score " + str(score) + "/11"

    entry_ts = int(candles_1m[-1][0])

    signal = {
        "symbol": symbol,
        "asset": symbol + " OTC",
        "direction": direction,
        "score": score,
        "price": round(price_1m, 8),
        "rsi5": round(rsi_5m, 1),
        "rsi1": round(rsi_1m, 1),
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
        existing_ids = set()

        for old in data.get("signals", []):
            existing_ids.add(old.get("id"))

        for signal in signals:
            if signal["id"] not in existing_ids:
                data["signals"].append(signal)

        data["signals"] = data["signals"][-1000:]

        return data

    update_tracker(
        mutate,
        "Add V2.2 signals " + scan_id,
    )


def main():
    scan_id = os.getenv("GITHUB_RUN_ID")

    if not scan_id:
        scan_id = str(int(time.time()))

    data, _ = read_tracker()

    found = []
    unavailable = []
    rejected = []

    for symbol in ASSETS:
        try:
            signal, reason = analyze(symbol)

            if signal:
                signal["id"] = (
                    "V22-"
                    + symbol
                    + "-"
                    + signal["direction"]
                    + "-"
                    + str(signal["entry_ts"])
                )

                allowed = cooldown_allowed(
                    data,
                    symbol,
                    signal["direction"],
                    signal["entry_ts"],
                )

                if allowed:
                    found.append(signal)
                else:
                    rejected.append(
                        symbol + ": cooldown"
                    )

            elif reason in (
                "5M data unavailable",
                "1M data unavailable",
                "indicator error",
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
        "📡 PRECISION SCANNER V2.2",
        "Scan: " + timestamp,
        "Data: Coinbase spot proxy",
        "Trend: 5M",
        "Entry: 1M",
        "Expiry: 5 MINUTES",
        "Minimum setup score: 9/11",
        "",
    ]

    if found:
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
                + "/11"
            )
            lines.append(
                "Price: "
                + str(signal["price"])
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
            lines.append("Expiry: 5 minutes")
            lines.append("")

    else:
        lines.append("⚪ NO TRADE")
        lines.append(
            "No asset passed the V2.2 filters."
        )

        if rejected:
            lines.append("")
            lines.append(
                "Top rejection reasons:"
            )

            for reason in rejected[:8]:
                lines.append("• " + reason)

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
