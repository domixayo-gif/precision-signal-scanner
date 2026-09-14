import os
import html
import requests
from datetime import datetime, timezone

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Pocket Option OTC assets we want to monitor
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

# Coinbase public market data
BASE = "https://api.exchange.coinbase.com"

TIMEOUT = 20

# Scanner settings
CANDLE_TIMEFRAME = "5 MINUTES"
RECOMMENDED_EXPIRY = "5 MINUTES"


def get(url, params=None):
    headers = {
        "Accept": "application/json",
        "User-Agent": "precision-signal-scanner/3.0",
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


def telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(
        url,
        data=data,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result)


def find_products():
    data = get(f"{BASE}/products")

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


def candles(pair):
    url = f"{BASE}/products/{pair}/candles"

    data = get(
        url,
        {
            "granularity": 300
        },
    )

    if len(data) < 60:
        raise RuntimeError(
            f"Only {len(data)} candles available"
        )

    data.sort(key=lambda x: x[0])

    # Coinbase format:
    # [timestamp, low, high, open, close, volume]

    return [
        float(x[4])
        for x in data[-200:]
    ]


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

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    gain = sum(
        gains[:period]
    ) / period

    loss = sum(
        losses[:period]
    ) / period

    for i in range(period, len(gains)):
        gain = (
            (gain * (period - 1))
            + gains[i]
        ) / period

        loss = (
            (loss * (period - 1))
            + losses[i]
        ) / period

    if loss == 0:
        return 100

    rs = gain / loss

    return 100 - (
        100 / (1 + rs)
    )


def macd(values):
    macd_series = []

    for i in range(26, len(values) + 1):
        fast = ema(
            values[:i],
            12
        )

        slow = ema(
            values[:i],
            26
        )

        macd_series.append(
            fast - slow
        )

    if len(macd_series) < 9:
        raise RuntimeError(
            "Not enough MACD data"
        )

    line = macd_series[-1]

    signal = ema(
        macd_series,
        9
    )

    histogram = line - signal

    return line, signal, histogram


def analyze(symbol, pair):
    prices = candles(pair)

    current = prices[-1]

    ema20 = ema(
        prices,
        20
    )

    ema50 = ema(
        prices,
        50
    )

    rsi_value = rsi(prices)

    macd_line, macd_signal, macd_hist = macd(
        prices
    )

    bull = 0
    bear = 0

    reasons = []

    # -------------------------
    # PRICE VS EMA20
    # -------------------------

    if current > ema20:
        bull += 2
        reasons.append(
            "Price above EMA20"
        )
    else:
        bear += 2
        reasons.append(
            "Price below EMA20"
        )

    # -------------------------
    # EMA20 VS EMA50
    # -------------------------

    if ema20 > ema50:
        bull += 3
        reasons.append(
            "EMA20 above EMA50"
        )
    else:
        bear += 3
        reasons.append(
            "EMA20 below EMA50"
        )

    # -------------------------
    # RSI
    # -------------------------

    if 50 <= rsi_value < 70:
        bull += 2
        reasons.append(
            "RSI bullish"
        )

    elif 30 < rsi_value < 50:
        bear += 2
        reasons.append(
            "RSI bearish"
        )

    elif rsi_value <= 30:
        bull += 1
        reasons.append(
            "RSI oversold"
        )

    else:
        bear += 1
        reasons.append(
            "RSI overbought"
        )

    # -------------------------
    # MACD
    # -------------------------

    if macd_hist > 0:
        bull += 3
        reasons.append(
            "MACD bullish"
        )
    else:
        bear += 3
        reasons.append(
            "MACD bearish"
        )

    # -------------------------
    # FINAL SIGNAL
    # -------------------------

    if bull > bear and bull >= 7:
        trade = "🟢 CALL"
        score = bull

    elif bear > bull and bear >= 7:
        trade = "🔴 PUT"
        score = bear

    else:
        trade = "⚪ NO TRADE"
        score = max(
            bull,
            bear
        )

    return {
        "symbol": symbol,
        "pair": pair,
        "price": current,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi_value,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "hist": macd_hist,
        "bull": bull,
        "bear": bear,
        "score": score,
        "trade": trade,
        "reasons": reasons,
    }


def fmt(value):
    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return f"{value:,.4f}"

    return f"{value:.6f}"


def main():
    print(
        "Starting Precision OTC Proxy Scanner..."
    )

    products = find_products()

    results = []
    errors = []

    # -------------------------
    # ANALYZE ALL ASSETS
    # -------------------------

    for symbol, otc_name in ASSETS.items():

        pair = products.get(symbol)

        if not pair:
            errors.append(
                f"{symbol}: no Coinbase USD/USDC market"
            )
            continue

        print(
            f"Analyzing {symbol} using {pair}..."
        )

        try:
            result = analyze(
                symbol,
                pair
            )

            result["otc_name"] = otc_name

            results.append(result)

            print(
                f"{symbol}: "
                f"{result['trade']} "
                f"{result['score']}/10"
            )

        except Exception as error:
            errors.append(
                f"{symbol}: {error}"
            )

    if not results:
        raise RuntimeError(
            "No assets could be analyzed"
        )

    # -------------------------
    # RANK SIGNALS
    # -------------------------

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    # -------------------------
    # TIME
    # -------------------------

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    # -------------------------
    # MESSAGE HEADER
    # -------------------------

    message = (
        "🚨 <b>POCKET OPTION OTC "
        "PROXY SCANNER</b>\n"
        f"⏰ {timestamp}\n"
        "📡 Market data: Coinbase\n"
        f"🕐 Timeframe: <b>{CANDLE_TIMEFRAME}</b>\n"
        f"⌛ Recommended Expiry: "
        f"<b>{RECOMMENDED_EXPIRY}</b>\n"
        "⚠️ Proxy signal — not Pocket Option's "
        "exact OTC feed\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    # -------------------------
    # TOP OPPORTUNITIES
    # -------------------------

    trades = [
        result
        for result in results
        if result["trade"] != "⚪ NO TRADE"
    ]

    if trades:

        message += (
            "🔥 <b>TOP OPPORTUNITIES</b>\n\n"
        )

        for index, result in enumerate(
            trades[:5],
            1
        ):

            message += (
                f"<b>#{index} "
                f"{result['symbol']} OTC</b>\n"
                f"🎯 {result['trade']}\n"
                f"🔥 Strength: "
                f"<b>{result['score']}/10</b>\n"
                f"💰 Price: "
                f"{fmt(result['price'])}\n"
                f"〽️ RSI: "
                f"{result['rsi']:.2f}\n"
                f"📈 EMA20: "
                f"{fmt(result['ema20'])}\n"
                f"📊 EMA50: "
                f"{fmt(result['ema50'])}\n"
                f"📉 MACD hist: "
                f"{result['hist']:.6f}\n"
                f"⌛ Expiry: "
                f"<b>{RECOMMENDED_EXPIRY}</b>\n\n"
            )

    else:

        message += (
            "⚪ <b>NO STRONG SETUPS</b>\n\n"
        )

    # -------------------------
    # FULL MARKET SCAN
    # -------------------------

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>FULL MARKET SCAN</b>\n\n"
    )

    for result in
