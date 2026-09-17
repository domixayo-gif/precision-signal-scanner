import os
import time
import requests
from datetime import datetime, timezone

from storage import read_tracker, update_tracker


TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://api.exchange.coinbase.com"
TG = f"https://api.telegram.org/bot{TOKEN}"

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
        f"{TG}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
        },
        timeout=30,
    )

    response.raise_for_status()


def get_candles(symbol, seconds):
    try:
        response = requests.get(
            f"{BASE}/products/{symbol}-USD/candles",
            params={
                "granularity": seconds,
            },
            headers={
                "User-Agent": "precision-signal-scanner-v2.2",
            },
            timeout=20,
        )

        if response.status_code != 200:
            return []

        rows = response.json()

        rows.sort(
            key=lambda row: row[0]
        )

        if len(rows) > 1:
            rows = rows[:-1]

        return rows

    except Exception:
        return []


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(
        values[:period]
    ) / period

    for price in values[period:]:
        value = (
            price * multiplier
            + value * (1 - multiplier)
        )

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    start = len(values) - period - 1
    end = len(values)

    for index in range(start, end - 1):
        change = (
            values[index + 1]
            - values[index]
        )

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0

    relative_strength = (
        average_gain / average_loss
    )

    return 100 - (
        100 / (1 + relative_strength)
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

        ranges.append(
            high - low
        )

    return sum(ranges) / len(ranges)


def recent_momentum(values):
    if len(values) < 4:
        return 0

    up = 0
    down = 0

    recent = values[-4:]

    for index in range(1, len(recent)):
        if recent[index] > recent[index - 1]:
            up += 1

        if recent[index] < recent[index - 1]:
            down += 1

    if up > down:
        return 1

    if down > up:
        return -1

    return 0


def analyze(symbol):
    candles_5m = get_candles(
        symbol,
        300,
    )

    candles_1m = get_candles(
        symbol,
        60,
    )

    if (
        len(candles_5m) < 60
        or len(candles_1m) < 40
    ):
        return None, "unavailable"

    prices_5m = [
        float(row[4])
        for row in candlesmain()  ]

    prices_1m = [
        float(row[4])
        for row in candles_1m
    ]

    price_5m = prices_5m[-1]
    price_1m = prices_1m[-1]

    ema20_5m = ema(
        prices_5m,
        20,
    )

    ema50_5m = ema(
        prices_5m,
        50,
    )

    ema20_previous = ema(
        prices_5m[:-2],
        20,
    )

    ema9_1m = ema(
        prices_1m,
        9,
    )

    ema21_1m = ema(
        prices_1m,
        21,
    )

    rsi_5m = rsi(
        prices_5m
    )

    rsi_1m = rsi(
        prices_1m
    )

    macd_5m = macd(
        prices_5m
    )

    macd_5m_previous = macd(
        prices_5m[:-1]
    )

    macd_1m = macd(
        prices_1m
    )

    macd_1m_previous = macd(
        prices_1m[:-1]
    )

    atr_5m = average_range(
        candles_5m,
        14,
    )

    atr_1m = average_range(
        candles_1m,
        14,
    )

    if None in (
        ema20_5m,
        ema50_5m,
        ema20_previous,
        ema9_1m,
        ema21_1m,
        rsi_5m,
        rsi_1m,
        macd_5m,
        macd_5m_previous,
        macd_1m,
        macd_1m_previous,
        atr_5m,
        atr_1m,
    ):
        return None, "indicator error"

    call_score = 0
    put_score = 0

    # 1. 5M price vs EMA20 = 2 points

    if price_5m > ema20_5m:
        call_score += 2

    if price_5m < ema20_5m:
        put_score += 2

    # 2. 5M EMA20 vs EMA50 = 2 points

    if ema20_5m > ema50_5m:
        call_score += 2

    if ema20_5m < ema50_5m:
        put_score += 2

    # 3. 5M EMA20 slope = 1 point

    if ema20_5m > ema20_previous:
        call_score += 1

    if ema20_5m < ema20_previous:
        put_score += 1

    # 4. 5M MACD = 1 point

    if (
        macd_5m > 0
        and macd_5m >= macd_5m_previous
    ):
        call_score += 1

    if (
        macd_5m < 0
        and macd_5m <= macd_5m_previous
    ):
        put_score += 1

    # 5. RSI agreement = 1 point

    if (
        rsi_5m > 50
        and rsi_1m > 50
    ):
        call_score += 1

    if (
        rsi_5m < 50
        and rsi_1m < 50
    ):
        put_score += 1

    # 6. 1M price vs EMA9 = 1 point

    if price_1m > ema9_1m:
        call_score += 1

    if price_1m < ema9_1m:
        put_score += 1

    # 7. 1M EMA9 vs EMA21 = 1 point

    if ema9_1m > ema21_1m:
        call_score += 1

    if ema9_1m < ema21_1m:
        put_score += 1

    # 8. 1M MACD = 1 point

    if (
        macd_1m > 0
        and macd_1m >= macd_1m_previous
    ):
        call_score += 1

    if (
        macd_1m < 0
        and macd_1m <= macd_1m_previous
    ):
        put_score += 1

    # 9. 1M recent momentum = 1 point

    momentum = recent_momentum(
        prices_1m
    )

    if momentum == 1:
        call_score += 1

    if momentum == -1:
        put_score += 1

    # Choose direction

    if call_score > put_score:
        direction = "CALL"
        score = call_score

    elif put_score > call_score:
        direction = "PUT"
        score = put_score

    else:
        return None, "score tied"

    # 5M trend confirmation

    if direction == "CALL":
        if not (
            price_5m > ema20_5m
            and ema20_5m > ema50_5m
        ):
            return None, "5M trend conflict"

    if direction == "PUT":
        if not (
            price_5m < ema20_5m
            and ema20_5m < ema50_5m
        ):
            return None, "5M trend conflict"

    # V2.2 allows slightly more room
    # before rejecting an extended entry.

    distance = abs(
        price_1m - ema9_1m
    )

    if distance > atr_1m * 2.2:
        return None, "entry extended"

    # Avoid extreme RSI,
    # but allow slightly more range than V2.1.

    if direction == "CALL":
        if (
            rsi_5m >= 75
            or rsi_1m >= 75
        ):
            return None, "CALL RSI too high"

    if direction == "PUT":
        if (
            rsi_5m <= 25
            or rsi_1m <= 25
        ):
            return None, "PUT RSI too low"

    # Avoid extremely large entry candles.

    last_candle_range = (
        float(candles_1m[-1][2])
        - float(candles_1m[-1][1])
    )

    if last_candle_range > atr_1m * 2.8:
        return None, "entry candle too large"

    # V2.2 allows slightly flatter trends
    # when the overall score is strong.

    slope_size = abs(
        ema20_5m - ema20_previous
    )

    if (
        slope_size < atr_5m * 0.02
        and score < 10
    ):
        return None, "5M trend too flat"

    # Main V2.2 qualification.

    if score < MIN_SCORE:
        return None, f"score {score}/11"

    entry_ts = int(
        candles_1m[-1][0]
    )

    signal = {
        "symbol": symbol,
        "asset": f"{symbol} OTC",
        "direction": direction,
        "score": score,
        "price": round(
            price_1m,
            8,
        ),
        "rsi5": round(
            rsi_5m,
            1,
        ),
        "rsi1": round(
            rsi_1m,
            1,
        ),
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
    cutoff = (
        entry_ts
        - COOLDOWN_MINUTES * 60
    )

    signals = data.get(
        "signals",
        [],
    )

    for old in reversed(signals):
        if old.get("symbol") != symbol:
            continue

        old_ts = int(
            old.get(
                "entry_ts",
                0,
            )
        )

        if old_ts < cutoff:
            break

        if (
            old.get("direction")
            == direction
        ):
            return False

    return True


def save_signals(found, scan_id):
    if not found:
        return

    def mutate(data):
        existing = {
            s.get("id")
            for s in data.get(
                "signals",
                [],
            )
        }

        for signal in found:
            if signal["id"] not in existing:
                data["signals"].append(
                    signal
                )

        data["signals"] = data[
            "signals"
        ][-1000:]

        return data

    update_tracker(
        mutate,
        f"Add V2.2 signals {scan_id}",
    )


def main():
    scan_id = (
        os.getenv("GITHUB_RUN_ID")
        or str(int(time.time()))
    )

    data, _ = read_tracker()

    found = []
    unavailable = []
    rejected = []

    for symbol in ASSETS:
        try:
            signal, reason = analyze(
                symbol
            )

            if signal:
                signal["id"] = (
                    f"V22-"
                    f"{symbol}-"
                    f"{signal['direction']}-"
                    f"{signal['entry_ts']}"
                )

                if cooldown_allowed(
                    data,
                    symbol,
                    signal["direction"],
                    signal["entry_ts"],
                ):
                    found.append(
                        signal
                    )
                else:
                    rejected.append(
                        f"{symbol}: cooldown"
                    )

            elif reason == "unavailable":
                unavailable.append(
                    symbol
                )

            else:
                rejected.append(
                    f"{symbol}: {reason}"
                )

        except Exception as exc:
            unavailable.append(
                f"{symbol}({type(exc).__name__})"
            )

    save_signals(
        found,
        scan_id,
    )

    now = datetime.now(
        timezone.utc
    )

    lines = [
        "📡 PRECISION SCANNER V2.2",
        (
            "Scan: "
            + now.isoformat()
            .replace("T", " ")
            .replace("+00:00", " UTC")
        ),
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

            lines.extend([
                (
                    f"{icon} • "
                    f"{signal['asset']}"
                ),
                (
                    f"Score: "
                    f"{signal['score']}/11"
                ),
                (
                    f"Price: "
                    f"{signal['price']}"
                ),
                (
                    f"5M RSI: "
                    f"{signal['rsi5']} | "
                    f"1M RSI: "
                    f"{signal['rsi1']}"
                ),
                (
                    f"Signal ID: "
                    f"{signal['id']}"
                ),
                "Expiry: 5 minutes",
                "",
            ])

    else:
        lines.append(
            "⚪ NO TRADE"
        )

        lines.append(
            "No asset passed the V2.2 filters."
        )

        if rejected:
            lines.append("")
            lines.append(
                "Top rejection reasons:"
            )

            for reason in rejected[:6]:
                lines.append(
                    "• " + reason
                )

    if unavailable:
        lines.extend([
            "",
            "⚠️ MARKET/DATA WARNINGS",
            "• "
            + ", ".join(unavailable),
        ])

    lines.extend([
        "",
        "Commands: /win ID | /loss ID | /stats",
        "",
        "⚠️ DEMO/TESTING ONLY.",
        "Coinbase proxy may differ from Pocket Option OTC pricing.",
    ])

    send_message(
        "\n".join(lines)
    )


if __name__ == "__main__":
    main()
