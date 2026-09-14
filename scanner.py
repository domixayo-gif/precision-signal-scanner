import os
import requests
from datetime import datetime, timezone

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

BINANCE_URL = "https://api.binance.com/api/v3/klines"

TIMEFRAME = "5m"
CANDLE_LIMIT = 100


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(url, data=data, timeout=20)
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")

    return result


# =========================
# MARKET DATA
# =========================

def get_klines(symbol):
    params = {
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": CANDLE_LIMIT,
    }

    response = requests.get(BINANCE_URL, params=params, timeout=20)
    response.raise_for_status()

    return response.json()


# =========================
# INDICATORS
# =========================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (price - value) * multiplier + value

    return value


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def macd(values):
    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    if ema12 is None or ema26 is None:
        return None, None

    macd_line = ema12 - ema26

    # Approximate signal line using recent MACD calculations
    macd_values = []

    for i in range(26, len(values) + 1):
        short = ema(values[:i], 12)
        long = ema(values[:i], 26)

        if short is not None and long is not None:
            macd_values.append(short - long)

    signal_line = ema(macd_values, 9)

    return macd_line, signal_line


# =========================
# ANALYSIS
# =========================

def analyze(symbol):
    raw = get_klines(symbol)

    closes = [float(candle[4]) for candle in raw]

    current_price = closes[-1]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    current_rsi = rsi(closes, 14)

    macd_line, signal_line = macd(closes)

    bullish_points = 0
    bearish_points = 0

    reasons = []

    # EMA trend
    if ema20 and ema50:

        if current_price > ema20:
            bullish_points += 1
            reasons.append("Price above EMA20")
        else:
            bearish_points += 1
            reasons.append("Price below EMA20")

        if ema20 > ema50:
            bullish_points += 1
            reasons.append("EMA20 above EMA50")
        else:
            bearish_points += 1
            reasons.append("EMA20 below EMA50")

    # RSI
    if current_rsi is not None:

        if current_rsi < 30:
            bullish_points += 2
            reasons.append("RSI oversold")

        elif current_rsi > 70:
            bearish_points += 2
            reasons.append("RSI overbought")

        elif current_rsi >= 50:
            bullish_points += 1
            reasons.append("RSI bullish")

        else:
            bearish_points += 1
            reasons.append("RSI bearish")

    # MACD
    if macd_line is not None and signal_line is not None:

        if macd_line > signal_line:
            bullish_points += 1
            reasons.append("MACD bullish")

        else:
            bearish_points += 1
            reasons.append("MACD bearish")

    # Final signal
    if bullish_points >= 4 and bullish_points > bearish_points:
        signal = "🟢 BUY"

    elif bearish_points >= 4 and bearish_points > bullish_points:
        signal = "🔴 SELL"

    else:
        signal = "⚪ NO TRADE"

    return {
        "symbol": symbol,
        "price": current_price,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": current_rsi,
        "macd": macd_line,
        "macd_signal": signal_line,
        "bullish": bullish_points,
        "bearish": bearish_points,
        "signal": signal,
        "reasons": reasons,
    }


# =========================
# TELEGRAM REPORT
# =========================

def build_report(results):

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    message = (
        "📊 <b>PRECISION SIGNAL SCANNER</b>\n"
        f"⏰ {now}\n"
        f"🕐 Timeframe: <b>{TIMEFRAME}</b>\n\n"
    )

    for result in results:

        symbol = result["symbol"].replace("USDT", "")

        message += (
            f"<b>━━ {symbol}/USDT ━━</b>\n"
            f"💰 Price: <b>{result['price']:,.4f}</b>\n"
            f"📈 EMA20: {result['ema20']:,.4f}\n"
            f"📊 EMA50: {result['ema50']:,.4f}\n"
            f"〽️ RSI: <b>{result['rsi']:.2f}</b>\n"
            f"MACD: {result['macd']:.6f}\n"
            f"Signal: {result['macd_signal']:.6f}\n\n"
            f"🟢 Bullish score: {result['bullish']}\n"
            f"🔴 Bearish score: {result['bearish']}\n"
            f"🎯 <b>{result['signal']}</b>\n"
            f"📝 {', '.join(result['reasons'])}\n\n"
        )

    message += (
        "━━━━━━━━━━━━━━\n"
        "⚠️ <i>Technical analysis only. "
        "This is not financial advice.</i>"
    )

    return message


# =========================
# MAIN
# =========================

def main():

    print("Starting Precision Signal Scanner...")

    results = []

    for symbol in SYMBOLS:

        try:
            print(f"Analyzing {symbol}...")

            result = analyze(symbol)

            results.append(result)

            print(
                f"{symbol}: "
                f"{result['signal']} "
                f"Price={result['price']}"
            )

        except Exception as error:

            print(f"Error analyzing {symbol}: {error}")

            # Continue analyzing the other coins
            continue

    if not results:
        raise RuntimeError("No market analysis was successfully completed.")

    report = build_report(results)

    print("\nSending Telegram report...")

    send_telegram(report)

    print("Telegram report sent successfully.")


if __name__ == "__main__":
    main()
