import os
import html
import requests
from datetime import datetime, timezone

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PAIRS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}

BASE = "https://api.exchange.coinbase.com/products"
TIMEOUT = 20


def telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    r = requests.post(url, data=data, timeout=TIMEOUT)
    r.raise_for_status()

    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(result)


def candles(pair):
    url = f"{BASE}/{pair}/candles"
    params = {"granularity": 300}

    headers = {
        "Accept": "application/json",
        "User-Agent": "precision-signal-scanner/1.0",
    }

    r = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    data = r.json()

    if len(data) < 60:
        raise RuntimeError(
            f"Only {len(data)} candles returned"
        )

    data.sort(key=lambda x: x[0])

    return [float(x[4]) for x in data[-200:]]


def ema(values, period):
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = price * k + value * (1 - k)

    return value


def rsi(values, period=14):
    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        gain = ((gain * 13) + gains[i]) / 14
        loss = ((loss * 13) + losses[i]) / 14

    if loss == 0:
        return 100

    return 100 - (100 / (1 + gain / loss))


def macd(values):
    series = []

    for i in range(26, len(values) + 1):
        fast = ema(values[:i], 12)
        slow = ema(values[:i], 26)
        series.append(fast - slow)

    line = series[-1]
    signal = ema(series, 9)
    return line, signal, line - signal


def analyze(name, pair):
    prices = candles(pair)

    price = prices[-1]
    e20 = ema(prices, 20)
    e50 = ema(prices, 50)
    r = rsi(prices)
    m, s, hist = macd(prices)

    bull = 0
    bear = 0
    reasons = []

    if price > e20:
        bull += 1
        reasons.append("Price above EMA20")
    else:
        bear += 1
        reasons.append("Price below EMA20")

    if e20 > e50:
        bull += 2
        reasons.append("EMA20 above EMA50")
    else:
        bear += 2
        reasons.append("EMA20 below EMA50")

    if r >= 70:
        bear += 2
        reasons.append("RSI overbought")
    elif r <= 30:
        bull += 2
        reasons.append("RSI oversold")
    elif r >= 50:
        bull += 1
        reasons.append("RSI bullish")
    else:
        bear += 1
        reasons.append("RSI bearish")

    if hist > 0:
        bull += 2
        reasons.append("MACD bullish")
    else:
        bear += 2
        reasons.append("MACD bearish")

    if bull >= 5 and bull > bear:
        signal = "🟢 CALL"
    elif bear >= 5 and bear > bull:
        signal = "🔴 PUT"
    else:
        signal = "⚪ NO TRADE"

    return {
        "name": name,
        "price": price,
        "e20": e20,
        "e50": e50,
        "rsi": r,
        "macd": m,
        "signal": s,
        "hist": hist,
        "bull": bull,
        "bear": bear,
        "trade": signal,
        "reasons": reasons,
    }


def price(value):
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:.6f}"


def main():
    print("Starting Precision Signal Scanner...")

    results = []
    errors = []

    for name, pair in PAIRS.items():
        print(f"Analyzing {pair}...")

        try:
            result = analyze(name, pair)
            results.append(result)

            print(
                f"{pair}: {result['trade']} "
                f"Price={result['price']}"
            )

        except Exception as e:
            errors.append(f"{pair}: {e}")
            print(f"{pair}: ERROR - {e}")

    if not results:
        raise RuntimeError("No market analysis completed")

    now = datetime.now(timezone.utc)
    now = now.strftime("%Y-%m-%d %H:%M:%S UTC")

    message = (
        "📊 <b>PRECISION SIGNAL SCANNER</b>\n"
        f"⏰ {now}\n"
        "📡 Data: Coinbase\n"
        "🕐 Timeframe: <b>5 MINUTES</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    for x in results:
        reasons = html.escape(", ".join(x["reasons"]))

        message += (
            f"<b>━━ {x['name']}/USD ━━</b>\n"
            f"💰 Price: <b>{price(x['price'])}</b>\n"
            f"📈 EMA20: {price(x['e20'])}\n"
            f"📊 EMA50: {price(x['e50'])}\n"
            f"〽️ RSI14: <b>{x['rsi']:.2f}</b>\n"
            f"📉 MACD: {x['macd']:.6f}\n"
            f"📊 Histogram: {x['hist']:.6f}\n"
            f"🟢 Bullish: {x['bull']}\n"
            f"🔴 Bearish: {x['bear']}\n"
            f"🎯 <b>{x['trade']}</b>\n"
            f"📝 {reasons}\n\n"
        )

    if errors:
        message += "⚠️ <b>ERRORS</b>\n"
        for error in errors:
            message += f"• {html.escape(error)}\n"

    message += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Technical analysis only. "
        "Not financial advice.</i>"
    )

    print("Sending Telegram report...")
    telegram(message)
    print("Telegram report sent successfully.")


if __name__ == "__main__":
    main()
