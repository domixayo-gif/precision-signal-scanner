import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


WATCHLIST = [
    "EURUSD-OTC",
    "EURUSD",
    "GBPUSD-OTC",
    "GBPUSD",
    "USDGBP",
    "AUDUSD-OTC",
    "AUDUSD",
    "CADUSD",
    "USDCAD",
    "USDCAD-OTC",
]

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
CANDLE_COUNT = 65
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0
TARGET_TRADES = 50

SCAN_INTERVAL = 2
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30

LOG_FILE = "momentum_signal_log.csv"

api = None
active_assets = {}
last_signal_candle = {}

wins = 0
losses = 0
draws = 0
unknown_results = 0
total_trades = 0

state_lock = threading.Lock()


def send_telegram(message):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("Telegram secrets are not configured.")
        return

    url = "https://api.telegram.org/bot" + token + "/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=15
        )
        if not response.ok:
            print("Telegram HTTP error:", response.status_code)
    except Exception as exc:
        print("Telegram error:", repr(exc))


def asset_key(name):
    text = str(name or "").strip().upper()

    if text.startswith("FRONT."):
        text = text[6:]
    elif text.startswith("FRONT_"):
        text = text[6:]

    return "".join(c for c in text if c.isalnum())


def clean_asset_name(name):
    text = str(name or "").strip()

    if text.lower().startswith("front."):
        text = text[6:]
    elif text.lower().startswith("front_"):
        text = text[6:]

    return text.split(".")[-1].strip()


def ensure_log():
    if os.path.exists(LOG_FILE):
        return

    with open(LOG_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            "time", "asset", "direction", "stake",
            "order_id", "status", "profit", "momentum"
        ])


def log_trade(asset, direction, order_id, status,
              profit="", momentum=""):
    ensure_log()

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            asset,
            direction,
            STAKE,
            order_id,
            status,
            profit,
            momentum
        ])


def connect():
    global api

    email = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    if not email or not password:
        print("ERROR: IQ_EMAIL or IQ_PASSWORD is missing.")
        return False

    try:
        api = IQ_Option(email, password)
        connected, reason = api.connect()

        if not connected:
            print("IQ Option connection failed:", reason)
            return False

        api.change_balance(BALANCE_MODE)
        print("Connected. Balance mode:", BALANCE_MODE)

        return True

    except Exception as exc:
        print("Connection error:", repr(exc))
        return False


def get_controlled_assets():
    found = {}

    try:
        data = api.get_all_init_v2()
    except Exception as exc:
        print("Asset discovery error:", repr(exc))
        return found

    if not isinstance(data, dict):
        print("Invalid asset data.")
        return found

    if isinstance(data.get("result"), dict):
        if "binary" not in data and "turbo" not in data:
            data = data["result"]

    for option_type in ("turbo", "binary"):
        section = data.get(option_type, {})

        if not isinstance(section, dict):
            continue

        actives = section.get("actives", {})

        if not isinstance(actives, dict):
            continue

        for raw_id, info in actives.items():
            if not isinstance(info, dict):
                continue

            name = clean_asset_name(info.get("name", ""))
            key = asset_key(name)

            wanted = None
            for item in WATCHLIST:
                if asset_key(item) == key:
                    wanted = item
                    break

            if wanted is None:
                continue

            enabled = info.get("enabled", False)
            suspended = info.get("is_suspended", False)

            if not enabled or suspended:
                continue

            try:
                int(raw_id)
            except (TypeError, ValueError):
                continue

            if key not in found:
                found[key] = {
                    "name": wanted,
                    "option_type": option_type
                }

    print("")
    print("ASSET DISCOVERY")
    print("-" * 40)

    for name in WATCHLIST:
        item = found.get(asset_key(name))

        if item:
            print(name, "AVAILABLE", item["option_type"])
        else:
            print(name, "NOT FOUND OR CLOSED")

    print("Available pairs:", len(found))
    return found


def get_candles(asset):
    try:
        candles = api.get_candles(
            asset,
            CANDLE_SECONDS,
            CANDLE_COUNT,
            int(time.time())
        )

        if not isinstance(candles, list):
            return []

        valid = []

        for candle in candles:
            if not isinstance(candle, dict):
                continue

            try:
                valid.append({
                    "from": int(candle["from"]),
                    "close": float(candle["close"])
                })
            except (KeyError, TypeError, ValueError):
                continue

        valid.sort(key=lambda item: item["from"])
        return valid

    except Exception as exc:
        print("Candle error:", asset, repr(exc))
        return []


def calculate_momentum(candles):
    closes = [item["close"] for item in candles]

    if len(closes) < MOMENTUM_PERIOD + 3:
        return []

    values = []

    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]

        if old_price == 0:
            continue

        value = (
            (closes[index] - old_price) / old_price
        ) * 100.0

        values.append(value)

    return values


def percentile(values, percent):
    if not values:
        return None

    ordered = sorted(values)

    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered))
    weight = position - lower

    return (
        ordered[lower]
        + (ordered[upper] - ordered[lower]) * weight
    )


def analyze_momentum(candles):
    momentum = calculate_momentum(candles)

    if len(momentum) < 4:
        return None

    values = momentum[-MOMENTUM_LOOKBACK:]

    if len(values) < 4:
        return None

    current = values[-1]
    previous = values[-2]
    previous_two = values[-3]

    low = percentile(values, EXTREME_PERCENTILE)
    high = percentile(values, 1.0 - EXTREME_PERCENTILE)

    if low is None or high is None:
        return None

    action = None

    if current <= low:
        turned_up = (
            previous < previous_two
            and current > previous
        )

        if turned_up:
            distance = abs(current - previous)

            if distance >= MIN_TURN_DISTANCE:
                action = "call"

    elif current >= high:
        turned_down = (
            previous > previous_two
            and current < previous
        )

        if turned_down:
            distance = abs(current - previous)

            if distance >= MIN_TURN_DISTANCE:
                action = "put"

    return {
        "action": action,
        "momentum": current,
        "candle_time": candles[-1]["from"]
    }


def get_trade_count():
    with state_lock:
        return total_trades


def get_stats():
    with state_lock:
        return wins, losses, draws, unknown_results, total_trades


def place_trade(asset, signal):
    global total_trades
    global wins, losses, draws, unknown_results

    direction = signal["action"]

    if direction not in ("call", "put"):
        return

    with state_lock:
        if total_trades >= TARGET_TRADES:
            return

    print(
        "SIGNAL:", asset,
        direction.upper(),
        "| Momentum:", round(signal["momentum"], 6)
    )

    send_telegram(
        "📊 <b>MOMENTUM 10 SIGNAL</b>\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction.upper() + "\n"
        "Expiry: 1 minute\n"
        "Stake: $" + str(STAKE) + "\n"
        "Mode: PRACTICE\n"
        "Status: Sending order"
    )

    if not AUTO_TRADE:
        log_trade(
            asset, direction, "",
            "SIGNAL_ONLY", "", signal["momentum"]
        )
        return

    try:
        result = api.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES
        )

        if not isinstance(result, (tuple, list)):
            print("Unexpected order response:", repr(result))
            log_trade(
                asset, direction, "",
                "ORDER_UNCONFIRMED", "", signal["momentum"]
            )
            return

        if len(result) < 2 or not result[0]:
            print("Order rejected:", asset, repr(result))
            log_trade(
                asset, direction, "",
                "REJECTED", "", signal["momentum"]
            )
            send_telegram(
                "⚠️ Order not accepted: " + asset
                + "\nThis signal will not count as a trade."
            )
            return

        order_id = result[1]

        with state_lock:
            total_trades += 1
            trade_number = total_trades

        log_trade(
            asset, direction, order_id,
            "ACCEPTED_PENDING", "", signal["momentum"]
        )

        send_telegram(
            "✅ <b>PRACTICE ORDER ACCEPTED</b>\n"
            "Trade: " + str(trade_number)
            + "/" + str(TARGET_TRADES) + "\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction.upper() + "\n"
            "Order ID: " + str(order_id)
        )

        print(
            "Accepted trade",
            trade_number,
            "of",
            TARGET_TRADES,
            "| Order ID:",
            order_id
        )

        profit = None

        try:
            profit = api.check_win_v4(order_id)
        except Exception as exc:
            print("Result lookup error:", repr(exc))

        if not isinstance(profit, (int, float)):
            with state_lock:
                unknown_results += 1

            log_trade(
                asset, direction, order_id,
                "UNKNOWN_RESULT", "", signal["momentum"]
            )

            send_telegram(
                "❔ Result not confirmed.\n"
                "Order: " + str(order_id)
                + "\nCheck the practice account before classifying it."
            )
            return

        if profit > 0:
            status = "WIN"
            with state_lock:
                wins += 1
        elif profit < 0:
            status = "LOSS"
            with state_lock:
                losses += 1
        else:
            status = "DRAW"
            with state_lock:
                draws += 1

        log_trade(
            asset, direction, order_id,
            status, profit, signal["momentum"]
        )

        w, l, d, u, count = get_stats()

        message = (
            "📈 <b>TRADE RESULT: " + status + "</b>\n"
            "Asset: " + asset + "\n"
            "Profit: " + str(profit) + "\n"
            "Wins: " + str(w) + "\n"
            "Losses: " + str(l) + "\n"
            "Draws: " + str(d) + "\n"
            "Unknown: " + str(u) + "\n"
            "Accepted trades: " + str(count)
            + "/" + str(TARGET_TRADES)
        )

        send_telegram(message)
        print(message)

    except Exception as exc:
        print("Trade error:", asset, repr(exc))
        send_telegram(
            "⚠️ Trade error for " + asset
            + ". Check the GitHub Actions log."
        )


def main():
    global active_assets

    ensure_log()

    print("=" * 50)
    print("MOMENTUM 10 IQ OPTION SCANNER")
    print("MODE:", BALANCE_MODE)
    print("AUTO TRADE:", AUTO_TRADE)
    print("STAKE: $", STAKE)
    print("TARGET:", TARGET_TRADES, "accepted orders")
    print("EXPIRY:", EXPIRY_MINUTES, "minute")
    print("=" * 50)

    send_telegram(
        "🤖 <b>Momentum 10 Scanner Started</b>\n"
        "Mode: PRACTICE\n"
        "Stake: $1\n"
        "Expiry: 1 minute\n"
        "Target: 50 accepted orders\n"
        "Results will be tracked separately."
    )

    last_refresh = 0
    last_heartbeat = time.time()

    while get_trade_count() < TARGET_TRADES:
        if api is None or not api.check_connect():
            print("Disconnected. Reconnecting...")

            if not connect():
                time.sleep(RECONNECT_SECONDS)
                continue

            last_refresh = 0

        now = time.time()

        if now - last_refresh >= ASSET_REFRESH_SECONDS:
            active_assets = get_controlled_assets()
            last_refresh = now

            if not active_assets:
                print("No requested assets available. Retrying later.")
                time.sleep(RECONNECT_SECONDS)
                continue

        for name in WATCHLIST:
            if get_trade_count() >= TARGET_TRADES:
                break

            key = asset_key(name)

            if key not in active_assets:
                continue

            candles = get_candles(name)

            if len(candles) < CANDLE_COUNT - 5:
                print("Insufficient candles:", name, len(candles))
                time.sleep(0.2)
                continue

            signal = analyze_momentum(candles)

            if not signal or not signal["action"]:
                time.sleep(0.2)
                continue

            candle_time = signal["candle_time"]

            if last_signal_candle.get(key) == candle_time:
                time.sleep(0.2)
                continue

            last_signal_candle[key] = candle_time
            place_trade(name, signal)

            time.sleep(1)

        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            w, l, d, u, count = get_stats()

            print(
                "HEARTBEAT | Trades:", count,
                "| Wins:", w,
                "| Losses:", l,
                "| Draws:", d,
                "| Unknown:", u
            )

            last_heartbeat = time.time()

        time.sleep(SCAN_INTERVAL)

    w, l, d, u, count = get_stats()

    summary = (
        "🏁 <b>50-TRADE TEST STOPPED</b>\n"
        "Accepted orders: " + str(count) + "\n"
        "Wins: " + str(w) + "\n"
        "Losses: " + str(l) + "\n"
        "Draws: " + str(d) + "\n"
        "Unknown results: " + str(u) + "\n"
        "CSV: " + LOG_FILE
    )

    print(summary)
    send_telegram(summary)


if __name__ == "__main__":
    main()
