import os
import requests
import pandas as pd

# Telegram settings
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    requests.post(url, data=data)


def get_market_data(symbol):
    url = "https://api.binance.com/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": "5m",
        "limit": 100
    }

    response = requests.get(url, params=params)
    response.raise_for_status()

    data = response.json()

    df = pd.DataFrame(data, columns=[
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "trades",
        "buy_volume",
        "buy_quote_volume",
        "ignore"
    ])

    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)

    return df


def analyze(symbol):
    df = get_market_data(symbol)

    df["ema50"] = df["close"].ewm(span=50).mean()
    df["ema200"] = df["close"].ewm(span=200).mean()

    price = df["close"].iloc[-1]
    ema50 = df["ema50"].iloc[-1]
    ema200 = df["ema200"].iloc[-1]

    if price > ema200 and ema50 > ema200:
        signal = "BUY"
    elif price < ema200 and ema50 < ema200:
        signal = "SELL"
    else:
        signal = "NO TRADE"

    return signal, price


def main():
    symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT"
    ]

    for symbol in symbols:
        try:
            signal, price = analyze(symbol)

            if signal != "NO TRADE":
                message = (
                    f"📊 SIGNAL\n\n"
                    f"Asset: {symbol}\n"
                    f"Signal: {signal}\n"
                    f"Timeframe: 5M\n"
                    f"Expiry reference: 10M\n"
                    f"Price: {price}"
                )

                send_telegram(message)

        except Exception as e:
            print(f"{symbol}: {e}")


if __name__ == "__main__":
    main()
