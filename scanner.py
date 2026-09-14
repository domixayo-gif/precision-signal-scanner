import os
import html
import requests
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}

COINBASE_URL = "https://api.exchange.coinbase.com/products"

GRANULARITY = 300       # 5 minutes
CANDLE_LIMIT = 200

TIMEOUT = 20


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(
        url,
        data=payload,
        timeout=TIMEOUT,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result


# ============================================================
# COINBASE MARKET DATA
# ============================================================

def get_candles(product_id):
    url = f"{COINBASE_URL}/{product_id}/candles"

    params = {
        "granularity": GRANULARITY,
    }

    headers = {
        "Accept": "application/json",
        "User-Agent": "precision-signal-scanner/1.0",
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=TIMEOUT,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Coinbase API returned HTTP "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected Coinbase response: {data}"
        )

    if len(data) < 60:
        raise RuntimeError(
            f"Not enough candles returned: {len(data)}"
        )

    # Coinbase normally returns newest first.
    # Convert to oldest -> newest.
    data = sorted(
        data,
        key=lambda candle: candle[0]
    )

    candles = []

    for candle in data[-CANDLE_LIMIT:]:
        candles.append({
            "timestamp": int(candle[0]),
            "low": float(candle[1]),
            "high": float(candle[2]),
            "open": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        })

    return candles


# ============================================================
# EMA
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (
            (price - value) * multiplier
            + value
        )

    return value


# ============================================================
# RSI
# ============================================================

def calculate_rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

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

    return 100 - (100 / (1 + rs))


# ============================================================
# MACD
# ============================================================

def calculate_macd(values):
    """
    Standard MACD:
    Fast EMA  = 12
    Slow EMA  = 26
    Signal    = 9
    """

    if len(values) < 35:
        return None, None, None

    macd_series = []

    for i in range(26, len(values) + 1):
        subset = values[:i]

        fast = ema(subset, 12)
        slow = ema(subset, 26)

        if fast is not None and slow is not None:
            macd_series.append(
                fast - slow
            )

    if len(macd_series) < 9:
        return None, None, None

    macd_line = macd_series[-1]

    signal_line = ema(
        macd_series,
        9
    )

    if signal_line is None:
        return None, None, None

    histogram = (
        macd_line - signal_line
    )

    return (
        macd_line,
        signal_line,
        histogram
    )


# ============================================================
# ANALYSIS
# ============================================================

def analyze(name, product_id):

    candles = get_candles(product_id)

    closes = [
        candle["close"]
        for candle in candles
    ]

    price = closes[-1]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    rsi = calculate_rsi(
        closes,
        14
    )

    (
        macd_line,
        macd_signal,
        macd_hist
    ) = calculate_macd(closes)

    if (
        ema20 is None
        or ema50 is None
        or rsi is None
        or macd_line is None
        or macd_signal is None
        or macd_hist is None
    ):
        raise RuntimeError(
            "Indicators could not be calculated"
        )

    bullish = 0
    bearish = 0

    reasons = []

    # --------------------------------------------------------
    # PRICE VS EMA20
    # --------------------------------------------------------

    if price > ema20:
        bullish += 1
        reasons.append(
            "Price above EMA20"
        )
    else:
        bearish += 1
        reasons.append(
            "Price below EMA20"
        )

    # --------------------------------------------------------
    # EMA20 VS EMA50
    # --------------------------------------------------------

    if ema20 > ema50:
        bullish += 2
        reasons.append(
            "EMA20 above EMA50"
        )
    else:
        bearish += 2
        reasons.append(
            "EMA20 below EMA50"
        )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if rsi >= 70:
        bearish += 2
        reasons.append(
            "RSI overbought"
        )

    elif rsi <= 30:
        bullish += 2
        reasons.append(
            "RSI oversold"
        )

    elif rsi >= 50:
        bullish += 1
        reasons.append(
            "RSI bullish"
        )

    else:
        bearish += 1
        reasons.append(
            "RSI bearish"
        )

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    if macd_hist > 0:
        bullish += 2
        reasons.append(
            "MACD bullish"
        )
    else:
        bearish += 2
        reasons.append(
            "MACD bearish"
        )

    # --------------------------------------------------------
    # FINAL SIGNAL
    # --------------------------------------------------------

    if bullish >= 5 and bullish > bearish:
        signal = "🟢 CALL"

    elif bearish >= 5 and bearish > bullish:
        signal = "🔴 PUT"

    else:
        signal = "⚪ NO TRADE"

    return {
        "name": name,
        "product": product_id,
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "bullish": bullish,
        "bearish": bearish,
        "signal": signal,
        "reasons": reasons,
        "candles": len(candles),
    }


# ============================================================
# PRICE FORMAT
# ============================================================

def format_price(price):

    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:,.4f}"

    return f"{price:.6f}"


# ============================================================
# TELEGRAM REPORT
# ============================================================

def build_report(results, errors):

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    message = (
        "📊 <b>PRECISION SIGNAL SCANNER</b>\n"
        f"⏰ {now}\n"
        "📡 Data: Coinbase\n"
        "🕐 Timeframe: <b>5 MINUTES</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    for result in results:

       
