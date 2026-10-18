import os
import csv
import json
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from iqoptionapi.stable_api import IQ_Option

# ==================== SETTINGS ====================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1
CANDLE_COUNT = 80

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0
TARGET_TRADES = 50

DAILY_PROFIT_TARGET = 20.0
DAILY_LOSS_LIMIT = 10.0

SCAN_INTERVAL = 5
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
RESULT_RETRY_SECONDS = 5
CANDLE_TIMEOUT = 15

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"

LAGOS = ZoneInfo("Africa/Lagos")

EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
connected_asset = None
crypto_assets = []
last_heartbeat = 0
last_signal_candles = {}


# ==================== LOGGING ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s"
)


def log(message):
    logging.info(message)


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=10
        )
    except Exception as exc:
        log("Telegram error: " + str(exc))


# ==================== STATE ====================

def new_state():
    return {
        "date": datetime.now(LAGOS).date().isoformat(),
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "net_profit": 0.0
    }


def load_state():
    today = datetime.now(LAGOS).date().isoformat()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if state.get("date") != today:
            return new_state()

        return state

    except (OSError, ValueError, TypeError):
        return new_state()


def save_state(state):
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    os.replace(temp_file, STATE_FILE)


def write_csv(row):
    exists = os.path.exists(LOG_FILE)

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        if not exists:
            writer.writerow([
                "time",
                "asset",
                "direction",
                "order_id",
                "outcome",
                "profit"
            ])

        writer.writerow(row)


# ==================== CRYPTO DISCOVERY ====================

CRYPTO_SYMBOLS = {
    "BTC", "ETH", "LTC", "XRP", "BCH", "EOS", "ETC",
    "ADA", "DOT", "SOL", "DOGE", "TRX", "LINK", "XLM",
    "BNB", "DASH", "ZEC", "NEO", "QTUM", "IOTA", "ATOM",
    "AVAX", "SHIB", "UNI", "FIL", "APT", "ARB", "OP",
    "PEPE", "TON", "NEAR", "ICP", "SUI", "INJ", "AAVE",
    "ALGO", "VET", "FTM", "HBAR", "MANA", "SAND", "AXS",
    "CRV", "COMP", "MKR", "SNX", "YFI", "GRT", "EGLD",
    "RUNE", "THETA", "XTZ", "KAVA", "ZIL", "ENJ", "CHZ",
    "BAT", "1INCH", "IMX", "SEI", "TIA", "WIF", "BONK",
    "FLOKI", "ORDI", "JUP", "WLD"
}


def is_crypto_name(name):
    normalized = str(name).upper().replace("_", "-")
    base = normalized.split("-")[0]

    for symbol in CRYPTO_SYMBOLS:
        if base.startswith(symbol):
            return True

    return False


def discover_asset():
    global crypto_assets

    crypto_assets = []

    try:
        market_data = api.get_all_open_time()
    except Exception as exc:
        log("Market discovery failed: " + str(exc))
        return []

    if not isinstance(market_data, dict):
        log("Unexpected market discovery response.")
        return []

    found = set()

    # Only binary/turbo instruments are considered because the
    # existing strategy uses api.buy() with a minute expiry.
    for market in ("turbo", "binary"):
        assets = market_data.get(market, {})

        if not isinstance(assets, dict):
            continue

        for name, details in assets.items():
            if not isinstance(details, dict):
                continue

            if details.get("open") is not True:
                continue

            if is_crypto_name(name):
                found.add(name)

    crypto_assets = sorted(found)

    log("Open crypto option assets found: " + str(len(crypto_assets)))

    for name in crypto_assets:
        log("CRYPTO FOUND: " + name)

    if not crypto_assets:
        log("No eligible crypto options found. No forex fallback.")
        return []

    return crypto_assets


# ==================== CONNECTION ====================

def connect_iq():
    global api, connected_asset

    api = None
    connected_asset = None

    if not EMAIL or not PASSWORD:
        log("Missing IQ_EMAIL or IQ_PASSWORD secret.")
        return False

    try:
        new_api = IQ_Option(EMAIL, PASSWORD)
        result = new_api.connect()

        if isinstance(result, tuple):
            connected = result[0]
        else:
            connected = bool(result)

        if not connected or not new_api.check_connect():
            log("IQ Option connection failed.")
            return False

        new_api.change_balance(BALANCE_MODE)
        api = new_api

        log("Connected. Balance mode: " + BALANCE_MODE)

        assets = discover_asset()

        if not assets:
            log("No eligible crypto assets. Scanner will reconnect.")
            api = None
            return False

        connected_asset = assets[0]

        telegram(
            "Crypto Momentum scanner connected.\n"
            "Practice mode: " + BALANCE_MODE + "\n"
            "Crypto assets found: " + str(len(assets)) + "\n"
            "Stake: $" + format(STAKE, ".2f")
        )

        return True

    except Exception as exc:
        log("Connection error: " + str(exc))
        api = None
        return False


# ==================== CANDLES ====================

def get_candles(asset):
    if api is None or not api.check_connect():
        return []

    try:
        candles = api.get_candles(
            asset,
            CANDLE_SECONDS,
            CANDLE_COUNT,
            time.time()
        )

        if not isinstance(candles, list):
            return []

        now = time.time()
        completed = []

        for candle in candles:
            candle_end = float(candle.get("to", 0))

            if candle_end and candle_end < now:
                completed.append(candle)

        completed.sort(key=lambda item: float(item.get("from", 0)))

        required = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2

        if len(completed) < required:
            return []

        return completed

    except Exception as exc:
        log("Candle error for " + asset + ": " + str(exc))
        return []


# ==================== MOMENTUM STRATEGY ====================

def percentile(values, fraction):
    if not values:
        return 0.0

    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower

    return (
        ordered[lower] * (1.0 - weight)
        + ordered[upper] * weight
    )


def calculate_signal(candles, asset):
    if len(candles) < MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2:
        return None

    closes = []

    try:
        for candle in candles:
            closes.append(float(candle["close"]))
    except (KeyError, TypeError, ValueError):
        return None

    momentums = []

    for index in range(MOMENTUM_PERIOD, len(closes)):
        previous_close = closes[index - MOMENTUM_PERIOD]

        if previous_close == 0:
            return None

        momentum = (
            (closes[index] - previous_close)
            / previous_close
        ) * 100.0

        momentums.append(momentum)

    if len(momentums) < MOMENTUM_LOOKBACK + 2:
        return None

    current_candle = candles[-1]
    candle_id = int(current_candle.get("from", 0))

    if last_signal_candles.get(asset) == candle_id:
        return None

    history = momentums[-(MOMENTUM_LOOKBACK + 2):-2]

    if len(history) < MOMENTUM_LOOKBACK:
        return None

    low_limit = percentile(history, EXTREME_PERCENTILE)
    high_limit = percentile(history, 1.0 - EXTREME_PERCENTILE)

    previous_momentum = momentums[-2]
    current_momentum = momentums[-1]

    candle_open = float(current_candle["open"])
    candle_close = float(current_candle["close"])
    turn = current_momentum - previous_momentum

    if (
        previous_momentum <= low_limit
        and turn >= MIN_TURN_DISTANCE
        and candle_close > candle_open
    ):
        last_signal_candles[asset] = candle_id
        return "call"

    if (
        previous_momentum >= high_limit
        and turn <= -MIN_TURN_DISTANCE
        and candle_close < candle_open
    ):
        last_signal_candles[asset] = candle_id
        return "put"

    return None


# ==================== RISK CONTROLS ====================

def trading_allowed(state):
    if state["trades"] >= TARGET_TRADES:
        log("Trade limit reached.")
        return False

    if state["net_profit"] >= DAILY_PROFIT_TARGET:
        log("Daily profit target reached.")
        return False

    if state["net_profit"] <= -DAILY_LOSS_LIMIT:
        log("Daily loss limit reached.")
        return False

    return True


# ==================== TRADE MANAGEMENT ====================

def place_trade(direction, state, asset):
    if not AUTO_TRADE:
        log("Signal only: " + asset + " " + direction.upper())
        telegram("Crypto signal: " + asset + " " + direction.upper())
        return

    if api is None or not api.check_connect():
        log("Trade skipped. Broker disconnected.")
        return

    if not trading_allowed(state):
        return

    log(
        "Signal confirmed: " + direction.upper()
        + " | Asset: " + asset
        + " | Stake: $" + format(STAKE, ".2f")
    )

    telegram(
        "Crypto signal confirmed: " + asset + "\n"
        + "Direction: " + direction.upper() + "\n"
        + "Stake: $" + format(STAKE, ".2f") + "\n"
        + "Expiry: " + str(EXPIRY_MINUTES) + " minute"
    )

    try:
        accepted, order_id = api.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES
        )
    except Exception as exc:
        log("Order submission error: " + str(exc))
        return

    if not accepted or not order_id:
        log("Broker did not accept order for " + asset)
        return

    state["trades"] += 1
    save_state(state)

    log("Order accepted. ID: " + str(order_id))

    while True:
        try:
            result = api.check_win_v4(order_id)

            if result is None:
                time.sleep(RESULT_RETRY_SECONDS)
                continue

            result = float(result)
            break

        except Exception as exc:
            log("Result check failed: " + str(exc))
            time.sleep(RESULT_RETRY_SECONDS)

            if api is None or not api.check_connect():
                log("Connection lost while waiting for result.")
                if not connect_iq():
                    continue

    if result > 0:
        profit = result
        state["wins"] += 1
        outcome = "WIN"
    else:
        profit = -STAKE if result == 0 else result
        state["losses"] += 1
        outcome = "LOSS"

    state["net_profit"] = round(state["net_profit"] + profit, 2)
    save_state(state)

    log(
        outcome + " | Asset: " + asset
        + " | Result: " + format(result, ".2f")
        + " | Counted P/L: " + format(profit, ".2f")
        + " | Daily P/L: " + format(state["net_profit"], ".2f")
    )

    write_csv([
        datetime.now(LAGOS).isoformat(),
        asset,
        direction,
        order_id,
        outcome,
        round(profit, 2)
    ])

    telegram(
        "Crypto trade " + outcome + "\n"
        + "Asset: " + asset + "\n"
        + "Counted P/L: $" + format(profit, ".2f") + "\n"
        + "Daily P/L: $" + format(state["net_profit"], ".2f")
    )


# ==================== MAIN SCANNER ====================

def run_scanner():
    global last_heartbeat

    state = load_state()

    log("CRYPTO MOMENTUM 10 SCANNER STARTING")
    log("Balance mode: " + BALANCE_MODE)
    log("Automatic trading: " + str(AUTO_TRADE))
    log("Stake: $" + format(STAKE, ".2f"))
    log("Trade cap: " + str(TARGET_TRADES))
    log("Profit target: $" + format(DAILY_PROFIT_TARGET, ".2f"))
    log("Loss limit: $" + format(DAILY_LOSS_LIMIT, ".2f"))

    telegram(
        "Crypto Momentum 10 scanner starting.\n"
        + "Mode: " + BALANCE_MODE + "\n"
        + "Stake: $" + format(STAKE, ".2f")
    )

    while True:
        try:
            today = datetime.now(LAGOS).date().isoformat()

            if state.get("date") != today:
                state = new_state()
                save_state(state)
                log("New Lagos trading day. Counters reset.")

            if api is None or not api.check_connect():
                log("Connecting or reconnecting...")
                if not connect_iq():
                    time.sleep(RECONNECT_SECONDS)
                    continue

            if not trading_allowed(state):
                log("Trading paused by risk controls.")
                time.sleep(60)
                continue

            assets = list(crypto_assets)

            if not assets:
                log("No crypto assets available. Refreshing discovery.")
                assets = discover_asset()

            if not assets:
                time.sleep(RECONNECT_SECONDS)
                continue

            for asset in assets:
                if not trading_allowed(state):
                    break

                if api is None or not api.check_connect():
                    log("Connection lost. Reconnecting.")
                    if not connect_iq():
                        break
                    assets = list(crypto_assets)
                    continue

                candles = get_candles(asset)

                if not candles:
                    continue

                signal = calculate_signal(candles, asset)

                if signal:
                    log(
                        "Confirmed crypto signal: "
                        + asset + " " + signal.upper()
                    )
                    place_trade(signal, state, asset)

                    # Wait for the current trade to resolve before
                    # checking another asset.
                    if AUTO_TRADE:
                        break

            now = time.time()

            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                log(
                    "HEARTBEAT | Trades: " + str(state["trades"])
                    + " | Wins: " + str(state["wins"])
                    + " | Losses: " + str(state["losses"])
                    + " | Daily P/L: $"
                    + format(state["net_profit"], ".2f")
                    + " | Crypto assets: " + str(len(crypto_assets))
                )
                last_heartbeat = now

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("Scanner stopped by user.")
            break

        except Exception as exc:
            log("Scanner loop error: " + str(exc))
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run_scanner()
