import os
import html
import requests
from datetime import datetime, timezone

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_URL = "https://api.exchange.coinbase.com"
TIMEOUT = 20

# Pocket Option OTC assets
ASSETS = {
    "BTC": "Bitcoin OTC",
    "ETH": "Ethereum OTC",
    "SOL": "Solana OTC",
    "BNB": "BNB OTC",
    "ADA": "Cardano OTC",
    "TRX": "TRON OTC",
    "LINK": "Chainlink OTC",
    "TON": "Toncoin OTC",
    "AVAX": "Avalanche OTC",
    "DOGE": "Dogecoin OTC",
    "DOT": "Polkadot OTC",
    "LTC": "Litecoin OTC",
    "POL": "Polygon OTC",
}


def request_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers={
            "Accept": "application/json",
            "User-Agent": "precision-signal-scanner",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(str(result))


def ema(values, period):
    if len(values) < period:
        return None

    value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

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

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        average_gain = (
            (average_gain * (period - 1))
            + gains[i]
        ) / period

        average_loss = (
            (average_loss * (period - 1))
            + losses[i]
        ) / period

    if average_loss == 0:
        return 100.0

    rs = average_gain / average_loss

    return 100 - (100 / (1 + rs))


def macd(values):
    if len(values) < 50:
        raise RuntimeError("Not enough candles for MACD")

    macd_values = []

    for i in range(26, len(values) + 1):
        fast = ema(values[:i], 12)
        slow = ema(values[:i], 26)
        macd_values.append(fast - slow)

    line = macd_values[-1]
    signal = ema(macd_values, 9)

    if signal is None:
        raise RuntimeError("Not enough MACD data")

    histogram = line - signal

    return line, signal, histogram


def get_products():
    data = request_json(f"{BASE_URL}/products")

    products = {}

    for product in data:
        if product.get("status") != "online":
            continue

        base = product.get("base_currency")
        quote = product.get("quote_currency")

        if base in ASSETS and quote in ("USD", "USDC"):
            if base not in products:
                products[base] = product["id"]

    return products


def get_prices(product_id):
    data = request_json(
        f"{BASE_URL}/products/{product_id}/candles",
        {"granularity": 300},
    )

    if not isinstance(data, list):
        raise RuntimeError("Invalid candle response")

    if len(data) < 60:
        raise RuntimeError(
            f"Only {len(data)} candles received"
        )

    data.sort(key=lambda candle: candle[0])

    # Coinbase candle format:
    # timestamp, low, high, open, close, volume

    prices = [
        float(candle[4])
        for candle in data
    ]

    # Ignore the newest candle because it may
    # still be forming.
    if len(prices) > 60:
        prices = prices[:-1]

    return prices[-200:]


def analyze(symbol, product_id):
    prices = get_prices(product_id)

    current = prices[-1]
    previous = prices[-2]

    ema20 = ema(prices, 20)
    ema50 = ema(prices, 50)
    rsi_value = rsi(prices)

    macd_line, macd_signal, macd_hist = macd(prices)

    bull = 0
    bear = 0
    reasons = []

    # Price trend
    if current > ema20:
        bull += 2
        reasons.append("Price above EMA20")
    else:
        bear += 2
        reasons.append("Price below EMA20")

    # Main trend
    if ema20 > ema50:
        bull += 3
        reasons.append("EMA20 above EMA50")
    else:
        bear += 3
        reasons.append("EMA20 below EMA50")

    # RSI
    if 50 <= rsi_value < 70:
        bull += 2
        reasons.append("RSI bullish")
    elif 30 < rsi_value < 50:
        bear += 2
        reasons.append("RSI bearish")
    elif rsi_value <= 30:
        bull += 1
        reasons.append("RSI oversold")
    else:
        bear += 1
        reasons.append("RSI overbought")

    # MACD
    if macd_hist > 0:
        bull += 3
        reasons.append("MACD bullish")
    else:
        bear += 3
        reasons.append("MACD bearish")

    # Short-term momentum
    if current > previous:
        bull += 1
        reasons.append("Latest candle momentum up")
    else:
        bear += 1
        reasons.append("Latest candle momentum down")

    if bull > bear and bull >= 8:
        signal = "🟢 CALL"
        score = bull
    elif bear > bull and bear >= 8:
        signal = "🔴 PUT"
        score = bear
    else:
        signal = "⚪ NO TRADE"
        score = max(bull, bear)

    return {
        "symbol": symbol,
        "price": current,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi_value,
        "macd_hist": macd_hist,
        "bull": bull,
        "bear": bear,
        "score": score,
        "signal": signal,
        "reasons": reasons,
    }


def price_format(value):
    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return f"{value:,.4f}"

    return f"{value:.6f}"


def build_report(results, errors):
    results.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    now = datetime.now(timezone.utc)

    timestamp = now.strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    message = (
        "🚨 <b>POCKET OPTION OTC "
        "PROXY SCANNER</b>\n\n"
        f"⏰ Scan time: {timestamp}\n"
        "📡 Data source: Coinbase\n"
        "🕐 Analysis timeframe: <b>5 MINUTES</b>\n"
        "⌛ Recommended expiry: <b>5 MINUTES</b>\n"
        "⚠️ Coinbase is a proxy for Pocket Option OTC.\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    trades = [
        item
        for item in results
        if item["signal"] != "⚪ NO TRADE"
    ]

    if trades:
        message += "🔥 <b>TOP SIGNALS</b>\n\n"

        for rank, item in enumerate(
            trades[:5],
            start=1,
        ):
            message += (
                f"<b>#{rank} {item['symbol']} OTC</b>\n"
                f"🎯 Signal: {item['signal']}\n"
                f"🔥 Strength: <b>{item['score']}/11</b>\n"
                f"💰 Price: {price_format(item['price'])}\n"
                f"〽️ RSI: {item['rsi']:.2f}\n"
                f"📈 EMA20: {price_format(item['ema20'])}\n"
                f"📊 EMA50: {price_format(item['ema50'])}\n"
                f"📉 MACD: {item['macd_hist']:.6f}\n"
                "⌛ Expiry: <b>5 MINUTES</b>\n\n"
            )
    else:
        message += (
            "⚪ <b>NO STRONG SIGNALS</b>\n\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>FULL MARKET RANKING</b>\n\n"
    )

    for rank, item in enumerate(results, start=1):
        reasons = html.escape(
            ", ".join(item["reasons"])
        )

        message += (
            f"<b>#{rank} {item['symbol']} OTC</b> "
            f"{item['signal']} "
            f"<b>{item['score']}/11</b>\n"
            f"💰 {price_format(item['price'])} | "
            f"RSI {item['rsi']:.1f}\n"
            f"📝 {reasons}\n\n"
        )

    if errors:
        message += (
            "⚠️ <b>UNAVAILABLE MARKETS</b>\n"
        )

        for error in errors:
            message += (
                f"• {html.escape(error)}\n"
            )

        message += "\n"

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>TRADE SETTINGS</b>\n"
        "📊 Candle timeframe: 5 minutes\n"
        "⌛ Recommended expiry: 5 minutes\n\n"
        "⚠️ <i>Technical-analysis proxy only. "
        "Signals are not guaranteed.</i>"
    )

    return message


def main():
    print("Starting Precision Signal Scanner...")

    products = get_products()

    results = []
    errors = []

    for symbol in ASSETS:
        product_id = products.get(symbol)

        if not product_id:
            errors.append(
                f"{symbol}: Coinbase USD/USDC market unavailable"
            )
            continue

        print(
            f"Analyzing {symbol} ({product_id})..."
        )

        try:
            result = analyze(
                symbol,
                product_id,
            )

            results.append(result)

            print(
                f"{symbol}: "
                f"{result['signal']} "
                f"{result['score']}/11"
            )

        except Exception as error:
            errors.append(
                f"{symbol}: {error}"
            )

            print(
                f"{symbol}: ERROR - {error}"
            )

    if not results:
        raise RuntimeError(
            "No markets could be analyzed."
        )

    report = build_report(
        results,
        errors,
    )

    print("Sending Telegram report...")

    # Telegram messages have a size limit.
    # Send the complete report in chunks if necessary.
    max_length = 3900

    if len(report) <= max_length:
        send_telegram(report)
    else:
        parts = [
            report[i:i + max_length]
            for i in range(
                0,
                len(report),
                max_length,
            )
        ]

        for part in parts:
            send_telegram(part)

    print(
        "Telegram report sent successfully."
    )


if __name__ == "__main__":
    main()
