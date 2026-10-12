import os
import csv
import json
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from iqoptionapi.stable_api import IQ_Option


# MOMENTUM 10 MULTI-PAIR PRACTICE SCANNER
BASE_PAIRS = ["EURUSD", "AUDUSD", "GBPUSD", "USDJPY"]
WATCHLIST = []

for pair in BASE_PAIRS:
    WATCHLIST.append(pair)
    WATCHLIST.append(pair + "-OTC")

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.07
MIN_TURN_DISTANCE = 0.06

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0

TARGET_TRADES = 50
DAILY_PROFIT_TARGET = 20.0
DAILY_LOSS_LIMIT = 5.0

SCAN_INTERVAL = 5
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
RESULT_RETRY_SECONDS = 15

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"

LAGOS = ZoneInfo("Africa/Lagos")

api = None
asset_ids = {}
unavailable_assets = set()

stop_event = threading.Event()
state_lock = threading.RLock()
trade_lock = threading.Lock()

last_heartbeat = 0
candle_worker = None

state = {
    "date": "",
    "net_profit": 0.0,
    "wins": 0,
    "losses": 0,
    "draws": 0,
    "unknown": 0,
    "resolved": 0,
    "last_signal_candle": {},
    "active_order": None
}


def today_string():
    return datetime.now(LAGOS).strftime("%Y-%m-%d")


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
            data={"chat_id": chat_id, "text": message},
            timeout=10
        )

        if response.status_code != 200:
            print("Telegram error:", response.status_code)

    except Exception as exc:
        print("Telegram error:", exc)


def save_state():
    with state_lock:
        temp_file = STATE_FILE + ".tmp"

        try:
            with open(temp_file, "w", encoding="utf-8") as file:
                json.dump(state, file, indent=2)

            os.replace(temp_file, STATE_FILE)

        except Exception as exc:
            print("State save error:", exc)


def load_state():
    global state

    if not os.path.exists(STATE_FILE):
        state["date"] = today_string()
        save_state()
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        if isinstance(saved, dict):
            for key in state:
                if key in saved:
                    state[key] = saved[key]

        if not isinstance(state.get("last_signal_candle"), dict):
            state["last_signal_candle"] = {}

    except Exception as exc:
        print("State load error:", exc)
        raise RuntimeError(
            "Cannot safely load saved state. Check the state file."
        ) from exc

    if state["date"] != today_string():
        state["date"] = today_string()
        state["net_profit"] = 0.0
        state["wins"] = 0
        state["losses"] = 0
        state["draws"] = 0
        state["unknown"] = 0
        state["resolved"] = 0
        save_state()


def log_trade(values):
    fields = [
        "time", "date", "asset", "direction", "momentum",
        "candle_time", "order_id", "result", "profit",
        "daily_net"
    ]

    exists = os.path.exists(LOG_FILE)

    try:
        with open(
            LOG_FILE, "a", newline="", encoding="utf-8"
        ) as file:
            writer = csv.DictWriter(file, fieldnames=fields)

            if not exists or os.path.getsize(LOG_FILE) == 0:
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
    wanted = normalize_name(target)

    id_keys = (
        "active_id", "activeid", "id",
        "instrument_id", "instrumentid"
    )

    name_keys = (
        "name", "active", "symbol",
        "ticker", "instrument"
    )

    if isinstance(data, dict):
        names = []

        for key in name_keys:
            value = data.get(key)

            if isinstance(value, str):
                names.append(value)

        matched = any(
            normalize_name(name) == wanted
            for name in names
        )

        if matched:
            for key in id_keys:
                value = data.get(key)

                if isinstance(value, (int, float)):
                    return int(value)

        for key, value in data.items():
            if normalize_name(key) == wanted:
                if isinstance(value, dict):
                    for id_key in id_keys:
                        found = value.get(id_key)

                        if isinstance(found, (int, float)):
                            return int(found)

        for value in data.values():
            found = find_id_in_data(value, target)

            if found is not None:
                return found

    elif isinstance(data, list):
        for item in data:
            found = find_id_in_data(item, target)

            if found is not None:
                return found

    return None


def discover_assets():
    global asset_ids, unavailable_assets

    sources = []

    try:
        sources.append(api.get_all_init_v2())
    except Exception as exc:
        print("Init V2 unavailable:", exc)

    try:
        sources.append(api.get_all_init())
    except Exception as exc:
        print("Init data unavailable:", exc)

    found_assets = {}

    for name in WATCHLIST:
        found_id = None

        for source in sources:
            if not source:
                continue

            found_id = find_id_in_data(source, name)

            if found_id is not None:
                break

        if found_id is None:
            print("Asset unavailable:", name)
        else:
            found_assets[name] = found_id
            print("Verified asset ID:", name, found_id)

    asset_ids = found_assets
    unavailable_assets = set(WATCHLIST) - set(asset_ids.keys())

    return bool(asset_ids)


def connect_iq():
    global api

    email = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    if not email or not password:
        print("Missing IQ_EMAIL or IQ_PASSWORD secrets.")
        return False

    try:
        print("Connecting to IQ Option...")

        new_api = IQ_Option(email, password)
        connected, reason = new_api.connect()

        if not connected:
            print("Connection failed:", reason)
            return False

        new_api.change_balance(BALANCE_MODE)

        if not new_api.check_connect():
            print("Connection check failed.")
            return False

        api = new_api

        if not discover_assets():
            print("No configured pairs found in broker data.")
            return False

        available_text = ", ".join(sorted(asset_ids.keys()))
        missing_text = ", ".join(sorted(unavailable_assets))

        message = (
            "Momentum 10 PRACTICE scanner connected\n"
            "Available pairs: " + available_text + "\n"
            "Unavailable pairs: " + (missing_text or "None") + "\n"
            "Stake: $1\n"
            "Expiry: 1 minute\n"
            "Daily net target: +$20\n"
            "Daily net loss limit: -$5\n"
            "Trade limit: 50 resolved trades"
        )

        print(message)
        telegram(message)

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

        result["candles"] = list(candle_api.candles_data or [])

    except Exception as exc:
        result["error"] = str(exc)


def get_candles(active_id, count):
    global candle_worker

    if api is None:
        return []

    if candle_worker is not None and candle_worker.is_alive():
        print("Previous candle request still running.")
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
        print("Candle request timed out.")
        return []

    if result["error"]:
        print("Candle error:", result["error"])
        return []

    candles = result["candles"]

    if not candles:
        print("No candles returned.")
        return []

    try:
        candles = sorted(
            candles,
            key=lambda item: float(item.get("from", 0))
        )

        now = time.time()

        candles = [
            item for item in candles
            if float(item.get("from", 0))
            + CANDLE_SECONDS <= now - 2
        ]

    except Exception as exc:
        print("Candle validation error:", exc)
        return []

    return candles


def calculate_signal(candles):
    required = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2

    if len(candles) < required:
        return None

    try:
        closes = [float(item["close"]) for item in candles]
        opens = [float(item["open"]) for item in candles]
        candle_times = [int(item["from"]) for item in candles]

    except Exception:
        print("Invalid candle data.")
        return None

    momentums = []

    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]
        new_price = closes[index]

        if old_price <= 0:
            continue

        value = (new_price - old_price) / old_price * 100
        momentums.append(value)

    if len(momentums) < MOMENTUM_LOOKBACK + 1:
        return None

    recent = momentums[-(MOMENTUM_LOOKBACK + 1):-1]
    current = momentums[-1]
    previous = momentums[-2]

    ordered = sorted(recent)

    low_index = int(
        (len(ordered) - 1) * EXTREME_PERCENTILE
    )

    high_index = int(
        (len(ordered) - 1) * (1 - EXTREME_PERCENTILE)
    )

    low_limit = ordered[low_index]
    high_limit = ordered[high_index]

    if abs(current - previous) < MIN_TURN_DISTANCE:
        return None

    direction = None
    last_open = opens[-1]
    last_close = closes[-1]

    if current <= low_limit and current > previous:
        if last_close > last_open:
            direction = "call"

    elif current >= high_limit and current < previous:
        if last_close < last_open:
            direction = "put"

    if direction is None:
        return None

    return {
        "direction": direction,
        "momentum": round(current, 5),
        "previous": round(previous, 5),
        "candle_time": candle_times[-1]
    }


def trading_is_allowed():
    with state_lock:
        if state["date"] != today_string():
            state["date"] = today_string()
            state["net_profit"] = 0.0
            state["wins"] = 0
            state["losses"] = 0
            state["draws"] = 0
            state["unknown"] = 0
            state["resolved"] = 0
            save_state()

        if state["net_profit"] >= DAILY_PROFIT_TARGET:
            return False, "Daily profit target reached."

        if state["net_profit"] <= -DAILY_LOSS_LIMIT:
            return False, "Daily loss limit reached."

        if state["resolved"] >= TARGET_TRADES:
            return False, "Resolved trade limit reached."

    return True, ""


def monitor_trade(order):
    order_id = order["order_id"]
    opened_at = float(order["opened_at"])
    trade_date = order["trade_date"]

    wait_until = opened_at + EXPIRY_MINUTES * 60 + 5

    while not stop_event.is_set():
        delay = wait_until - time.time()

        if delay > 0:
            stop_event.wait(min(delay, 5))
            continue

        try:
            if api is None or not api.check_connect():
                if not connect_iq():
                    stop_event.wait(RECONNECT_SECONDS)
                    continue

            result = api.check_win_v4(order_id)

            if result is None:
                raise RuntimeError("Trade result is not available.")

            profit = float(result)

            if profit > 0:
                outcome = "WIN"
                net_change = profit
            elif profit < 0:
                outcome = "LOSS"
                net_change = profit
            else:
                outcome = "DRAW"
                net_change = 0.0

            with state_lock:
                if trade_date == today_string():
                    state["net_profit"] = round(
                        state["net_profit"] + net_change, 2
                    )

                    if outcome == "WIN":
                        state["wins"] += 1
                    elif outcome == "LOSS":
                        state["losses"] += 1
                    else:
                        state["draws"] += 1

                    state["resolved"] += 1

                state["active_order"] = None

                daily_net = state["net_profit"]
                resolved = state["resolved"]
                wins_now = state["wins"]
                losses_now = state["losses"]
                draws_now = state["draws"]

                save_state()

            log_trade({
                "time": datetime.now(LAGOS).isoformat(),
                "date": trade_date,
                "asset": order["asset"],
                "direction": order["direction"].upper(),
                "momentum": order["momentum"],
                "candle_time": order["candle_time"],
                "order_id": order_id,
                "result": outcome,
                "profit": net_change,
                "daily_net": daily_net
            })

            message = (
                "MOMENTUM 10 RESULT\n"
                "Asset: " + order["asset"] + "\n"
                "Direction: " + order["direction"].upper() + "\n"
                "Result: " + outcome + "\n"
                "Net trade result: $" + str(round(net_change, 2)) + "\n"
                "Today's net: $" + str(round(daily_net, 2)) + "\n"
                "Resolved today: " + str(resolved) + "/" +
                str(TARGET_TRADES) + "\n"
                "Wins: " + str(wins_now) + "\n"
                "Losses: " + str(losses_now) + "\n"
                "Draws: " + str(draws_now)
            )

            print(message)
            telegram(message)

            allowed, reason = trading_is_allowed()

            if not allowed:
                print("Trading stopped:", reason)
                telegram("SCANNER STOPPED\n" + reason)

            return

        except Exception as exc:
            print("Result unresolved; will retry:", exc)
            stop_event.wait(RESULT_RETRY_SECONDS)

    print("Result monitor ended because stop_event was set.")


def place_trade(asset, direction, signal):
    if not AUTO_TRADE:
        print("Auto trading disabled.")
        return

    allowed, reason = trading_is_allowed()

    if not allowed:
        print("Trade skipped:", reason)
        return

    if not trade_lock.acquire(blocking=False):
        print("Trade placement already in progress.")
        return

    try:
        allowed, reason = trading_is_allowed()

        if not allowed:
            print("Trade skipped:", reason)
            return

        with state_lock:
            if state["active_order"] is not None:
                print("Previous order is unresolved.")
                return

        if asset not in asset_ids:
            print("Unverified asset. Trade skipped:", asset)
            return

        if api is None or not api.check_connect():
            print("Disconnected. Trade skipped.")
            return

        try:
            api.change_balance(BALANCE_MODE)
            balance = api.get_balance()
        except Exception as exc:
            print("Balance check failed:", exc)
            return

        print(BALANCE_MODE, "balance:", balance)

        success, order_id = api.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES
        )

        if not success:
            print("Order rejected:", asset, order_id)
            telegram("Order rejected for " + asset + ".")
            return

        order = {
            "order_id": order_id,
            "asset": asset,
            "direction": direction,
            "momentum": signal["momentum"],
            "candle_time": signal["candle_time"],
            "opened_at": time.time(),
            "trade_date": today_string()
        }

        with state_lock:
            state["active_order"] = order
            save_state()

        print("PRACTICE order accepted:", asset, order_id)

        telegram(
            "PRACTICE TRADE OPENED\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction.upper() + "\n"
            "Stake: $1\n"
            "Expiry: 1 minute\n"
            "Order ID: " + str(order_id)
        )

        thread = threading.Thread(
            target=monitor_trade,
            args=(order,),
            daemon=False
        )

        thread.start()

    except Exception as exc:
        print("Trade placement error:", exc)
        telegram("Trade placement error: " + str(exc))

    finally:
        trade_lock.release()


def heartbeat():
    global last_heartbeat

    now = time.time()

    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return

    last_heartbeat = now

    with state_lock:
        print(
            "HEARTBEAT | Date:", state["date"],
            "| Resolved:", state["resolved"],
            "| Wins:", state["wins"],
            "| Losses:", state["losses"],
            "| Draws:", state["draws"],
            "| Net:", state["net_profit"],
            "| Active order:", bool(state["active_order"]),
            "| Available assets:", len(asset_ids)
        )


def run_scanner():
    load_state()

    saved_order = None

    with state_lock:
        if state["active_order"] is not None:
            saved_order = dict(state["active_order"])

    while not stop_event.is_set():
        if api is None or not api.check_connect():
            if not connect_iq():
                print("Connection unavailable; retrying.")
                stop_event.wait(RECONNECT_SECONDS)
                continue

        if saved_order is not None:
            print(
                "Resuming saved order monitoring:",
                saved_order["order_id"]
            )

            thread = threading.Thread(
                target=monitor_trade,
                args=(saved_order,),
                daemon=False
            )

            thread.start()
            saved_order = None

        allowed, reason = trading_is_allowed()

        
        if not asset_ids:
            print("No verified pairs. Retrying.")
            stop_event.wait(RECONNECT_SECONDS)
            continue

        for asset in list(asset_ids.keys()):
            if stop_event.is_set():
                break

            allowed, reason = trading_is_allowed()

            if not allowed:
                print("Trading stopped:", reason)
                telegram("SCANNER STOPPED\n" + reason)
                return

            with state_lock:
                if state["active_order"] is not None:
                    break

            active_id = asset_ids.get(asset)

            if active_id is None:
                continue

            candles = get_candles(
                active_id,
                MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5
            )

            if not candles:
                print(asset, "| no usable candles.")
                continue

            signal = calculate_signal(candles)

            if signal is None:
                print(asset, "| no confirmed reversal signal.")
                continue

            candle_time = signal["candle_time"]

            with state_lock:
                last_seen = state["last_signal_candle"].get(asset, 0)
                duplicate = last_seen == candle_time

                if not duplicate:
                    state["last_signal_candle"][asset] = candle_time
                    save_state()

            if duplicate:
                print(asset, "| duplicate signal skipped.")
                continue

            print(
                "SIGNAL:", asset,
                signal["direction"].upper(),
                "| Momentum:", signal["momentum"]
            )

            telegram(
                "Momentum 10 Signal\n"
                "Asset: " + asset + "\n"
                "Direction: " + signal["direction"].upper() + "\n"
                "Momentum: " + str(signal["momentum"])
            )

            place_trade(asset, signal["direction"], signal)

            with state_lock:
                if state["active_order"] is not None:
                    break

        heartbeat()
        stop_event.wait(SCAN_INTERVAL)
