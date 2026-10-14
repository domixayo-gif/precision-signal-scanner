
import os
import csv
import json
import time
import threading
import requests

from datetime import datetime
from zoneinfo import ZoneInfo
from queue import Queue, Empty
from iqoptionapi.stable_api import IQ_Option


# ==================== SETTINGS ====================

ASSET = "EURUSD-OTC"
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
DAILY_LOSS_LIMIT = 5.0

SCAN_INTERVAL = 5
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_TIMEOUT = 15
RESULT_RETRY_SECONDS = 5

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"

LAGOS = ZoneInfo("Africa/Lagos")

EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
connected_asset = None
last_heartbeat = 0
last_signal_candle = 0


# ==================== LOGGING ====================

def log(message):
    now = datetime.now(LAGOS).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


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
        log(f"Telegram error: {exc}")


def write_csv(row):
    exists = os.path.exists(LOG_FILE)

    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)

            if not exists:
                writer.writerow([
                    "time",
                    "asset",
                    "signal",
                    "entry",
                    "result",
                    "profit"
                ])

            writer.writerow(row)

    except Exception as exc:
        log(f"CSV error: {exc}")


# ==================== STATE ====================

def new_state():
    return {
        "date": datetime.now(LAGOS).date().isoformat(),
        "net_profit": 0.0,
        "wins": 0,
        "losses": 0,
        "trades": 0
    }


def load_state():
    state = new_state()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                saved = json.load(file)

            if saved.get("date") == state["date"]:
                state.update(saved)

        except Exception as exc:
            log(f"State file could not be read: {exc}")

    save_state(state)
    return state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
    except Exception as exc:
        log(f"State save error: {exc}")


# ==================== BROKER CONNECTION ====================

def asset_exists(data, target):
    target = target.upper().replace("_", "-")

    try:
        text = json.dumps(data, default=str).upper()
        text = text.replace("_", "-")
        return target in text
    except Exception:
        return False


def discover_asset():
    global api

    if api is None:
        return False

    methods = [
        "get_all_init_v2",
        "get_all_init"
    ]

    for method_name in methods:
        try:
            method = getattr(api, method_name, None)

            if method is None:
                continue

            data = method()

            if asset_exists(data, ASSET):
                log(f"Asset confirmed in broker data: {ASSET}")
                return True

        except Exception as exc:
            log(f"{method_name} failed: {exc}")

    log(f"Asset not confirmed in broker data: {ASSET}")
    return False


def connect_iq():
    global api, connected_asset

    api = None
    connected_asset = None

    if not EMAIL or not PASSWORD:
        log("Missing IQ_EMAIL or IQ_PASSWORD GitHub secret.")
        return False

    try:
        log("Connecting to IQ Option...")

        new_api = IQ_Option(EMAIL, PASSWORD)
        result = new_api.connect()

        if isinstance(result, tuple):
            connected = result[0]
        else:
            connected = bool(result)

        if not connected or not new_api.check_connect():
            log(f"Connection failed: {result}")
            return False

        new_api.change_balance(BALANCE_MODE)

        api = new_api
        log(f"Connected. Balance mode: {BALANCE_MODE}")

        if not discover_asset():
            log("Asset discovery failed. Will retry connection.")
            api = None
            return False

        connected_asset = ASSET

        try:
            log(f"Available balance: {api.get_balance()}")
        except Exception:
            log("Connected; balance could not be read.")

        telegram(
            "Momentum scanner connected.\n"
            f"Asset: {ASSET}\n"
            f"Mode: {BALANCE_MODE}\n"
            f"Stake: ${STAKE:.2f}"
        )

        return True

    except Exception as exc:
        log(f"Connection error: {exc}")
        api = None
        connected_asset = None
        return False


# ==================== CANDLE DATA ====================

def candle_worker(result_queue):
    try:
        candles = api.get_candles(
            ASSET,
            CANDLE_SECONDS,
            CANDLE_COUNT,
            time.time()
        )
        result_queue.put(("ok", candles))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def get_candles():
    if api is None or not api.check_connect():
        log("Not connected; candles unavailable.")
        return []

    result_queue = Queue()

    worker = threading.Thread(
        target=candle_worker,
        args=(result_queue,),
        daemon=True
    )
    worker.start()

    try:
        status, result = result_queue.get(timeout=CANDLE_TIMEOUT)
    except Empty:
        log("Candle request timed out.")
        return []

    if status == "error":
        log(f"Candle request failed: {result}")
        return []

    if not isinstance(result, list):
        log("Broker returned an unexpected candle format.")
        return []

    now = int(time.time())
    candles = []

    for candle in result:
        try:
            candle_end = int(candle.get("to", 0))

            if candle_end > 0 and candle_end < now:
                candles.append(candle)

        except Exception:
            continue

    candles.sort(key=lambda item: item.get("from", 0))

    required = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2

    if len(candles) < required:
        log(
            f"Not enough completed candles: {len(candles)}; "
            f"need at least {required}."
        )
        return []

    log(f"Completed candles received: {len(candles)}")
    return candles


# ==================== MOMENTUM SIGNAL ====================

def percentile(values, fraction):
    ordered = sorted(values)

    if not ordered:
        return 0.0

    index = int((len(ordered) - 1) * fraction)
    return ordered[index]


def calculate_signal(candles):
    global last_signal_candle

    required = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2

    if len(candles) < required:
        return None

    closes = []

    try:
        for candle in candles:
            closes.append(float(candle["close"]))
    except Exception:
        log("Invalid candle close values.")
        return None

    momentums = []

    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]
        new_price = closes[index]

        if old_price == 0:
            continue

        value = ((new_price - old_price) / old_price) * 100.0
        momentums.append(value)

    if len(momentums) < MOMENTUM_LOOKBACK + 2:
        log("Not enough momentum values.")
        return None

    current_candle = candles[-1]
    candle_id = int(current_candle.get("from", 0))

    if candle_id == last_signal_candle:
        return None

    # Use prior momentum values to calculate the extremes.
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

    # CALL: momentum was extremely weak, then turned upward.
    if (
        previous_momentum <= low_limit
        and turn >= MIN_TURN_DISTANCE
        and candle_close > candle_open
    ):
        last_signal_candle = candle_id
        return "call"

    # PUT: momentum was extremely strong, then turned downward.
    if (
        previous_momentum >= high_limit
        and turn <= -MIN_TURN_DISTANCE
        and candle_close < candle_open
    ):
        last_signal_candle = candle_id
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

def place_trade(direction, state):
    global api

    if not AUTO_TRADE:
        log(f"Signal only: {direction.upper()}")
        telegram(f"Momentum signal: {ASSET} {direction.upper()}")
        return

    if api is None or not api.check_connect():
        log("Trade skipped: broker is disconnected.")
        return

    if not trading_allowed(state):
        return

    log(
        f"Signal confirmed: {direction.upper()} | "
        f"Asset: {ASSET} | Stake: ${STAKE:.2f}"
    )

    telegram(
        f"Signal confirmed: {ASSET}\n"
        f"Direction: {direction.upper()}\n"
        f"Stake: ${STAKE:.2f}\n"
        f"Expiry: {EXPIRY_MINUTES} minute"
    )

    try:
        accepted, order_id = api.buy(
            STAKE,
            ASSET,
            direction,
            EXPIRY_MINUTES
        )
    except Exception as exc:
        log(f"Order submission error: {exc}")
        return

    if not accepted or not order_id:
        log("Broker did not accept the order.")
        telegram("Order was not accepted by IQ Option.")
        return

    state["trades"] += 1
    save_state(state)

    log(f"Order accepted. ID: {order_id}")
    telegram(f"Order accepted. ID: {order_id}")

    # Do not submit another trade until this result is checked.
    while True:
        try:
            result = api.check_win_v4(order_id)

            if result is None:
                log("Trade result pending; waiting.")
                time.sleep(RESULT_RETRY_SECONDS)
                continue

            result = float(result)
            break

        except Exception as exc:
            log(f"Result check failed: {exc}")
            time.sleep(RESULT_RETRY_SECONDS)

            if api is None or not api.check_connect():
                log("Connection lost while waiting for trade result.")
                if not connect_iq():
                    continue

    # Per the user's accounting rule, a zero result counts as a loss.
    if result > 0:
        profit = result
        state["wins"] += 1
        outcome = "WIN"
    else:
        profit = -STAKE if result == 0 else result
        state["losses"] += 1
        outcome = "LOSS"

    state["net_profit"] = round(
        state["net_profit"] + profit, 2
    )

    save_state(state)

    log(
        f"{outcome} | Result: {result:.2f} | "
        f"Counted P/L: {profit:.2f} | "
        f"Daily P/L: {state['net_profit']:.2f} | "
        f"W/L: {state['wins']}/{state['losses']}"
    )

    write_csv([
        datetime.now(LAGOS).isoformat(),
        ASSET,
        direction,
        order_id,
        outcome,
        round(profit, 2)
    ])

    telegram(
        f"Trade {outcome}\n"
        f"Asset: {ASSET}\n"
        f"Counted P/L: ${profit:.2f}\n"
        f"Daily P/L: ${state['net_profit']:.2f}\n"
        f"Wins/Losses: {state['wins']}/{state['losses']}"
    )


# ==================== MAIN SCANNER ====================

def run_scanner():
    global last_heartbeat

    state = load_state()

    log("======================================")
    log("MOMENTUM 10 SCANNER STARTING")
    log(f"Asset: {ASSET}")
    log(f"Balance mode: {BALANCE_MODE}")
    log(f"Automatic trading: {AUTO_TRADE}")
    log(f"Stake: ${STAKE:.2f}")
    log(f"Daily profit target: ${DAILY_PROFIT_TARGET:.2f}")
    log(f"Daily loss limit: ${DAILY_LOSS_LIMIT:.2f}")
    log("======================================")

    telegram(
        "Momentum 10 scanner starting.\n"
        f"Asset: {ASSET}\n"
        f"Mode: {BALANCE_MODE}"
    )

    while True:
        try:
            today = datetime.now(LAGOS).date().isoformat()

            if state.get("date") != today:
                state = new_state()
                save_state(state)
                log("New Lagos trading day. Daily counters reset.")

            if api is None or not api.check_connect():
                log("Connecting or reconnecting...")
                if not connect_iq():
                    time.sleep(RECONNECT_SECONDS)
                    continue

            if not trading_allowed(state):
                log("Trading paused by risk controls.")
                time.sleep(60)
                continue

            candles = get_candles()

            if not candles:
                time.sleep(SCAN_INTERVAL)
                continue

            signal = calculate_signal(candles)

            if signal:
                log(f"Confirmed signal: {signal.upper()}")
                place_trade(signal, state)
            else:
                log("No confirmed reversal signal.")

            now = time.time()

            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                log(
                    f"HEARTBEAT | Trades: {state['trades']} | "
                    f"Wins: {state['wins']} | "
                    f"Losses: {state['losses']} | "
                    f"Daily P/L: ${state['net_profit']:.2f}"
                )
                last_heartbeat = now

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("Scanner stopped manually.")
            break

        except Exception as exc:
            log(f"Scanner loop error: {exc}")
            time.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    run_scanner()
