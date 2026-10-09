import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# CONTROLLED MOMENTUM 10 TEST
WATCHLIST = ["EURUSD-OTC"]

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0
TARGET_TRADES = 50

SCAN_INTERVAL = 5
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10

LOG_FILE = "momentum_signal_log.csv"

api = None
asset_id = None
asset_name = None

stop_event = threading.Event()
state_lock = threading.Lock()

resolved_trades = 0
wins = 0
losses = 0
draws = 0
unknown = 0

last_signal_candle = {}
last_heartbeat = 0
candle_worker = None


def telegram(message):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("Telegram secrets are missing.")
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
            timeout=10
        )
        if response.status_code != 200:
            print("Telegram error:", response.status_code)
    except Exception as exc:
        print("Telegram connection error:", exc)


def log_trade(values):
    fields = [
        "time", "asset", "direction", "score",
        "candle_time", "order_id", "result", "profit"
    ]

    exists = os.path.exists(LOG_FILE)

    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)

            if not exists:
                writer.writeheader()

            writer.writerow(values)
    except Exception as exc:
        print("CSV error:", exc)


def normalize_name(value):
    return "".join(
        char for char in str(value).upper()
        if char.isalnum()
    )


def find_id_in_data(data, target):
    target_normalized = normalize_name(target)
    id_keys = (
        "active_id", "activeid", "id",
        "instrument_id", "instrumentid"
    )
    name_keys = (
        "name", "active", "symbol",
        "ticker", "instrument"
    )

    if isinstance(data, dict):
        current_name = ""

        for key in name_keys:
            value = data.get(key)
            if isinstance(value, str):
                if normalize_name(value) == target_normalized:
                    current_name = value
                    break

        if current_name:
            for key in id_keys:
                value = data.get(key)
                if isinstance(value, (int, float)):
                    return int(value)

        for key, value in data.items():
            if normalize_name(key) == target_normalized:
                if isinstance(value, dict):
                    for id_key in id_keys:
                        found_id = value.get(id_key)
                        if isinstance(found_id, (int, float)):
                            return int(found_id)

                    nested = find_id_in_data(value, target)
                    if nested is not None:
                        return nested

                elif isinstance(value, (int, float)):
                    return int(value)

        for value in data.values():
            found_id = find_id_in_data(value, target)
            if found_id is not None:
                return found_id

    elif isinstance(data, list):
        for item in data:
            found_id = find_id_in_data(item, target)
            if found_id is not None:
                return found_id

    return None


def discover_asset():
    global api

    print("Looking for EURUSD-OTC in IQ Option data...")

    sources = []

    try:
        sources.append(api.get_all_init_v2())
    except Exception as exc:
        print("Init V2 unavailable:", exc)

    try:
        sources.append(api.get_all_init())
    except Exception as exc:
        print("Init data unavailable:", exc)

    for source in sources:
        if not source:
            continue

        found_id = find_id_in_data(source, "EURUSD-OTC")

        if found_id is not None:
            print("Found EURUSD-OTC active ID:", found_id)
            return found_id

    print("EURUSD-OTC active ID was not found.")
    print("The broker may not be exposing this asset right now.")
    return None


def connect_iq():
    global api, asset_id, asset_name

    email = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    if not email or not password:
        print("Missing IQ_EMAIL or IQ_PASSWORD GitHub Secrets.")
        return False

    try:
        print("Connecting to IQ Option...")
        new_api = IQ_Option(email, password)
        connected, reason = new_api.connect()

        if not connected:
            print("IQ Option connection failed:", reason)
            return False

        new_api.change_balance(BALANCE_MODE)

        if not new_api.check_connect():
            print("Connection check failed.")
            return False

        api = new_api
        print("Connected. Balance mode:", BALANCE_MODE)

        asset_id = discover_asset()
        asset_name = "EURUSD-OTC"

        if asset_id is None:
            print("Cannot scan until the asset ID is available.")
            return False

        telegram(
            "Momentum 10 Scanner started\n"
            "Mode: PRACTICE\n"
            "Asset: EUR/USD OTC\n"
            "Stake: $1\n"
            "Expiry: 1 minute\n"
            "Target: 50 resolved trades"
        )

        return True

    except Exception as exc:
        print("Connection error:", exc)
        return False


def candle_request_worker(api_instance, active_id, count, result):
    try:
        candle_api = api_instance.api.candles
        candle_api.candles_data = []

        try:
            server_time = api_instance.get_server_timestamp()
        except Exception:
            server_time = None

        if not server_time:
            server_time = time.time()

        api_instance.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time
        )

        started = time.time()

        while time.time() - started < CANDLE_REQUEST_TIMEOUT:
            candles = candle_api.candles_data

            if candles and len(candles) >= count:
                result["candles"] = list(candles)
                return

            time.sleep(0.2)

        candles = candle_api.candles_data

        if candles:
            result["candles"] = list(candles)

    except Exception as exc:
        result["error"] = str(exc)


def get_candles(active_id, count):
    global candle_worker, api

    if api is None:
        return []

    if candle_worker is not None and candle_worker.is_alive():
        print("Previous candle request is still running; skipping this pass.")
        return []

    result = {"candles": [], "error": ""}

    worker = threading.Thread(
        target=candle_request_worker,
        args=(api, active_id, count, result),
        daemon=True
    )

    candle_worker = worker
    worker.start()
    worker.join(CANDLE_REQUEST_TIMEOUT + 2)

    if worker.is_alive():
        print("Candle request timed out. Skipping this scan pass.")
        return []

    if result["error"]:
        print("Candle error:", result["error"])

    candles = result["candles"]

    if not candles:
        print("No candles returned.")
        return []

    try:
        candles = sorted(
            candles,
            key=lambda item: float(item.get("from", 0))
        )
    except Exception:
        pass

    return candles


def calculate_signal(candles):
    if len(candles) < MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2:
        return None

    try:
        closes = [float(item["close"]) for item in candles]
        candle_times = [int(item["from"]) for item in candles]
    except Exception:
        print("Invalid candle data.")
        return None

    momentums = []

    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]
        new_price = closes[index]

        if old_price == 0:
            continue

        value = ((new_price - old_price) / old_price) * 100
        momentums.append(value)

    if len(momentums) < MOMENTUM_LOOKBACK + 1:
        return None

    recent = momentums[-(MOMENTUM_LOOKBACK + 1):-1]
    current = momentums[-1]
    previous = momentums[-2]

    ordered = sorted(recent)
    low_index = int((len(ordered) - 1) * EXTREME_PERCENTILE)
    high_index = int(
        (len(ordered) - 1) * (1 - EXTREME_PERCENTILE)
    )

    low_limit = ordered[low_index]
    high_limit = ordered[high_index]

    turn_distance = abs(current - previous)

    if turn_distance < MIN_TURN_DISTANCE:
        return None

    direction = None

    if current <= low_limit and current > previous:
        direction = "call"

    elif current >= high_limit and current < previous:
        direction = "put"

    if direction is None:
        return None

    candle_time = candle_times[-1]

    return {
        "direction": direction,
        "momentum": round(current, 5),
        "previous": round(previous, 5),
        "candle_time": candle_time,
        "price": closes[-1]
    }


def monitor_trade(order_id, direction, signal_info):
    global resolved_trades, wins, losses, draws, unknown

    result_name = "UNKNOWN"
    profit = 0.0

    try:
        time.sleep(EXPIRY_MINUTES * 60 + 5)

        current_api = api

        if current_api is None:
            raise RuntimeError("IQ Option connection unavailable")

        profit = current_api.check_win_v4(order_id)

        if profit is None:
            result_name = "UNKNOWN"
        elif profit > 0:
            result_name = "WIN"
        elif profit < 0:
            result_name = "LOSS"
        else:
            result_name = "DRAW"

    except Exception as exc:
        print("Trade result error:", exc)
        result_name = "UNKNOWN"

    with state_lock:
        if result_name == "WIN":
            wins += 1
            resolved_trades += 1
        elif result_name == "LOSS":
            losses += 1
            resolved_trades += 1
        elif result_name == "DRAW":
            draws += 1
            resolved_trades += 1
        else:
            unknown += 1

        total = resolved_trades
        current_wins = wins
        current_losses = losses
        current_draws = draws

    log_trade({
        "time": datetime.now(timezone.utc).isoformat(),
        "asset": asset_name,
        "direction": direction.upper(),
        "score": signal_info["momentum"],
        "candle_time": signal_info["candle_time"],
        "order_id": order_id,
        "result": result_name,
        "profit": profit
    })

    message = (
        "Momentum 10 Trade Result\n"
        "Asset: EUR/USD OTC\n"
        "Direction: " + direction.upper() + "\n"
        "Result: " + result_name + "\n"
        "Profit: " + str(profit) + "\n"
        "Resolved: " + str(total) + "/" + str(TARGET_TRADES) + "\n"
        "Wins: " + str(current_wins) + "\n"
        "Losses: " + str(current_losses) + "\n"
        "Draws: " + str(current_draws)
    )

    print(message)
    telegram(message)

    if total >= TARGET_TRADES:
        stop_event.set()
        telegram(
            "50-trade test target reached. "
            "Please review the recorded results before another run."
        )


def place_trade(direction, signal_info):
    global api

    if not AUTO_TRADE:
        print("Signal found, but auto trading is disabled.")
        return

    if api is None or not api.check_connect():
        print("Not connected. Trade skipped.")
        return

    try:
        print("PRACTICE ORDER:", direction.upper())

        check_balance = api.get_balance()
        print("Practice balance:", check_balance)

        success, order_id = api.buy(
            STAKE,
            asset_name,
            direction,
            EXPIRY_MINUTES
        )

        if not success:
            print("Order was rejected:", order_id)
            telegram(
                "Signal found but order was not accepted.\n"
                "Asset: EUR/USD OTC\n"
                "Direction: " + direction.upper()
            )
            return

        print("Practice order accepted. ID:", order_id)

        telegram(
            "PRACTICE TRADE OPENED\n"
            "Asset: EUR/USD OTC\n"
            "Direction: " + direction.upper().upper() + "\n"
            "Stake: $1\n"
            "Expiry: 1 minute\n"
            "Order ID: " + str(order_id)
        )

        thread = threading.Thread(
            target=monitor_trade,
            args=(order_id, direction, signal_info),
            daemon=True
        )
        thread.start()

    except Exception as exc:
        print("Trade placement error:", exc)
        telegram("Trade placement error: " + str(exc))


def heartbeat():
    global last_heartbeat

    now = time.time()

    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return

    last_heartbeat = now

    with state_lock:
        total = resolved_trades
        current_wins = wins
        current_losses = losses
        current_draws = draws

    print(
        "HEARTBEAT | Resolved:", total,
        "| Wins:", current_wins,
        "| Losses:", current_losses,
        "| Draws:", current_draws
    )


def run_scanner():
    global api, asset_id

    while not stop_event.is_set():
        if api is None or not api.check_connect():
            print("Connection unavailable. Reconnecting...")
            if not connect_iq():
                time.sleep(RECONNECT_SECONDS)
                continue

        with state_lock:
            if resolved_trades >= TARGET_TRADES:
                break

        print("Starting EURUSD-OTC candle scan...")

        candles = get_candles(
            asset_id,
            MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5
        )

        if not candles:
            heartbeat()
            time.sleep(SCAN_INTERVAL)
            continue

        signal = calculate_signal(candles)

        if signal is None:
            print("No valid Momentum 10 extreme-reversal signal.")
        else:
            direction = signal["direction"]
            candle_time = signal["candle_time"]
            key = (asset_name, direction)

            if last_signal_candle.get(key) == candle_time:
                print("Duplicate signal candle skipped.")
            else:
                last_signal_candle[key] = candle_time

                print(
                    "SIGNAL:", direction.upper(),
                    "| Momentum:", signal["momentum"],
                    "| Candle:", candle_time
                )

                telegram(
                    "Momentum 10 Signal\n"
                    "Asset: EUR/USD OTC\n"
                    "Direction: " + direction.upper() + "\n"
                    "Momentum: " + str(signal["momentum"]) + "\n"
                    "Mode: PRACTICE"
                )

                place_trade(direction, signal)

        heartbeat()
        time.sleep(SCAN_INTERVAL)

    with state_lock:
        total = resolved_trades
        current_wins = wins
        current_losses = losses
        current_draws = draws
        current_unknown = unknown

    summary = (
        "MOMENTUM 10 TEST STOPPED\n"
        "Resolved trades: " + str(total) + "/" + str(TARGET_TRADES) + "\n"
        "Wins: " + str(current_wins) + "\n"
        "Losses: " + str(current_losses) + "\n"
        "Draws: " + str(current_draws) + "\n"
        "Unknown results: " + str(current_unknown)
    )

    print(summary)
    telegram(summary)


if __name__ == "__main__":
    try:
        run_scanner()
    except KeyboardInterrupt:
        print("Scanner stopped.")
    except Exception as exc:
        print("Fatal scanner error:", exc)
        telegram("Fatal scanner error: " + str(exc))
        raise
