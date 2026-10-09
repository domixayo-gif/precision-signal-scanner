
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
REQUIRE_TURN = True
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0
TARGET_TRADES = 50

SCAN_INTERVAL = 2
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 8

LOG_FILE = "momentum_signal_log.csv"

api = None
active_assets = {}

wins = 0
losses = 0
draws = 0
unknown_results = 0
completed_trades = 0
total_trades = 0
pending_results = 0

last_signal_candle = {}
last_extreme_state = {}

state_lock = threading.Lock()


def send_telegram(message):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        return

    url = (
        "https://api.telegram.org/bot"
        + token
        + "/sendMessage"
    )

    while message:
        part = message[:3900]
        message = message[3900:]

        try:
            requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "HTML"
                },
                timeout=15
            )
        except Exception as exc:
            print("Telegram error:", repr(exc))


def asset_key(name):
    text = str(name or "").strip().upper()

    if text.startswith("FRONT."):
        text = text[6:]
    elif text.startswith("FRONT_"):
        text = text[6:]

    return "".join(
        char for char in text
        if char.isalnum()
    )


def wanted_asset(name):
    key = asset_key(name)

    for wanted in WATCHLIST:
        if key == asset_key(wanted):
            return wanted

    return None


def extract_name(info, fallback=None):
    if isinstance(info, dict):
        for field in (
            "name", "symbol", "display_name",
            "displayName", "active_name",
            "activeName", "instrument"
        ):
            value = info.get(field)
            if value:
                return str(value)

    return fallback


def extract_id(info, fallback=None):
    if isinstance(info, dict):
        for field in (
            "active_id", "activeId",
            "instrument_id", "instrumentId", "id"
        ):
            value = info.get(field)
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    return value

    if fallback is not None:
        try:
            return int(fallback)
        except Exception:
            return None

    return None


def get_open_time_assets():
    found = {}

    print("")
    print("=" * 55)
    print("CHECKING BINARY/TURBO AVAILABILITY")
    print("=" * 55)

    try:
        open_data = api.get_all_open_time()
    except Exception as exc:
        print("Open-time lookup failed:", repr(exc))
        return found

    if not isinstance(open_data, dict):
        print("IQ Option returned invalid availability data.")
        return found

    for option_type in ("turbo", "binary"):
        section = open_data.get(option_type, {})

        if not isinstance(section, dict):
            continue

        print("")
        print("Checking", option_type.upper())

        for raw_name, info in section.items():
            wanted = wanted_asset(raw_name)

            if wanted is None:
                continue

            is_open = (
                isinstance(info, dict)
                and info.get("open") is True
            )

            print(
                wanted,
                "OPEN" if is_open else "CLOSED",
                "| Type:",
                option_type
            )

            if not is_open:
                continue

            key = asset_key(wanted)

            if key not in found:
                found[key] = {
                    "name": str(raw_name),
                    "id": None,
                    "option_type": option_type,
                    "open": True
                }

    return found


def add_init_assets(obj, found, depth=0):
    if depth > 18:
        return

    if isinstance(obj, dict):
        for key, value in obj.items():

            if isinstance(value, dict):
                name = extract_name(value, None)
                wanted = wanted_asset(name)

                if wanted:
                    active_id = extract_id(value, key)

                    if active_id is not None:
                        asset_type = "unknown"

                        if "binary" in str(key).lower():
                            asset_type = "binary"

                        found_name = str(name)
                        found_key = asset_key(wanted)

                        if found_key in found:
                            found[found_key]["id"] = active_id
                            found[found_key]["name"] = found_name
                        else:
                            found[found_key] = {
                                "name": found_name,
                                "id": active_id,
                                "option_type": asset_type,
                                "open": False
                            }

                add_init_assets(
                    value,
                    found,
                    depth + 1
                )

            elif isinstance(value, list):
                add_init_assets(
                    value,
                    found,
                    depth + 1
                )

    elif isinstance(obj, list):
        for item in obj:
            add_init_assets(
                item,
                found,
                depth + 1
            )


def get_controlled_assets():
    print("")
    print("=" * 55)
    print("DISCOVERING REQUESTED CURRENCY PAIRS")
    print("=" * 55)

    open_assets = get_open_time_assets()
    init_assets = {}

    try:
        data = api.get_all_init_v2()

        if isinstance(data, dict):
            add_init_assets(data, init_assets)

    except Exception as exc:
        print("Initialization lookup error:", repr(exc))

    # Keep only assets confirmed open for binary/turbo.
    result = {}

    for key, item in open_assets.items():
        init_item = init_assets.get(key)

        if init_item and init_item.get("id") is not None:
            item["id"] = init_item["id"]

        if item.get("id") is None:
            print(
                "Skipping",
                item["name"],
                "- active ID was not found"
            )
            continue

        result[key] = item

    print("")
    print("=" * 55)
    print("ASSET DISCOVERY REPORT")
    print("=" * 55)

    for wanted in WATCHLIST:
        key = asset_key(wanted)

        if key in result:
            item = result[key]
            print(
                wanted,
                "AVAILABLE",
                "| ID:",
                item["id"],
                "| Type:",
                item["option_type"]
            )
        else:
            print(wanted, "NOT AVAILABLE FOR BINARY/TURBO")

    print("")
    print("Total available assets:", len(result))

    return result


def get_candles(active_id, count):
    if api is None:
        return []

    try:
        api.api.candles.candles_data = []

        server_time = api.get_server_timestamp()

        if not server_time:
            server_time = time.time()

        api.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time
        )

        started = time.time()

        while time.time() - started < CANDLE_REQUEST_TIMEOUT:
            candles = api.api.candles.candles_data

            if candles and len(candles) >= count:
                return candles

            time.sleep(0.2)

        candles = api.api.candles.candles_data

        return candles if candles else []

    except Exception as exc:
        print("Candle error:", repr(exc))
        return []


def calculate_momentum(candles):
    closes = []

    for candle in candles:
        try:
            closes.append(float(candle["close"]))
        except Exception:
            continue

    if len(closes) < MOMENTUM_PERIOD + 3:
        return []

    momentum = []

    for i in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[i - MOMENTUM_PERIOD]

        if old_price == 0:
            continue

        value = (
            (closes[i] - old_price) / old_price
        ) * 100.0

        momentum.append(value)

    return momentum


def percentile(values, percent):
    if not values:
        return None

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = lower + 1

    if upper >= len(ordered):
        return ordered[lower]

    weight = position - lower

    return (
        ordered[lower]
        + (ordered[upper] - ordered[lower]) * weight
    )


def analyze_momentum(candles):
    momentum = calculate_momentum(candles)

    if len(momentum) < 4:
        return None

    lookback = momentum[-MOMENTUM_LOOKBACK:]

    if len(lookback) < 4:
        return None

    current = lookback[-1]
    previous = lookback[-2]
    previous_two = lookback[-3]

    low_level = percentile(
        lookback,
        EXTREME_PERCENTILE
    )

    high_level = percentile(
        lookback,
        1.0 - EXTREME_PERCENTILE
    )

    if low_level is None or high_level is None:
        return None

    extreme = "NONE"
    action = None

    if current <= low_level:
        extreme = "LOW"
    elif current >= high_level:
        extreme = "HIGH"

    if extreme == "LOW":
        turned_up = (
            previous < previous_two
            and current > previous
        )

        if turned_up:
            turn_distance = abs(current - previous)

            if turn_distance >= MIN_TURN_DISTANCE:
                action = "CALL"

    elif extreme == "HIGH":
        turned_down = (
            previous > previous_two
            and current < previous
        )

        if turned_down:
            turn_distance = abs(current - previous)

            if turn_distance >= MIN_TURN_DISTANCE:
                action = "PUT"

    strength = 0.0

    if previous != 0:
        strength = (
            abs(current - previous)
            / max(abs(previous), 0.000001)
        ) * 100.0

    return {
        "action": action,
        "current": current,
        "previous": previous,
        "previous_two": previous_two,
        "low_level": low_level,
        "high_level": high_level,
        "extreme": extreme,
        "reversal_strength": strength
    }


def build_signal_message(asset, analysis, signal_id):
    action = analysis["action"]
    direction = "🟢 CALL" if action == "CALL" else "🔴 PUT"

    return (
        "<b>🤖 Momentum 10 Scanner</b>\n\n"
        "<b>Asset:</b> " + asset + "\n"
        "<b>Signal:</b> " + direction + "\n"
        "<b>Expiry:</b> 1 MINUTE\n"
        "<b>Mode:</b> PRACTICE\n\n"
        "<b>Momentum:</b> "
        + str(round(analysis["current"], 5)) + "\n"
        "<b>Previous:</b> "
        + str(round(analysis["previous"], 5)) + "\n"
        "<b>Previous 2:</b> "
        + str(round(analysis["previous_two"], 5)) + "\n"
        "<b>Extreme:</b> " + analysis["extreme"] + "\n"
        "<b>Reversal Strength:</b> "
        + str(round(analysis["reversal_strength"], 2)) + "%\n\n"
        "<b>Signal ID:</b> " + signal_id
    )


def ensure_log_file():
    if os.path.exists(LOG_FILE):
        return

    with open(LOG_FILE, "w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            "timestamp", "signal_id", "asset", "action",
            "stake", "expiry", "result", "profit",
            "momentum", "previous", "previous_two", "extreme"
        ])


def log_trade(signal_id, asset, action, result, profit, analysis):
    ensure_log_file()

    try:
        with open(LOG_FILE, "a", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                signal_id,
                asset,
                action,
                STAKE,
                EXPIRY_MINUTES,
                result,
                profit,
                analysis.get("current", ""),
                analysis.get("previous", ""),
                analysis.get("previous_two", ""),
                analysis.get("extreme", "")
            ])

    except Exception as exc:
        print("Log error:", repr(exc))


def monitor_trade(order_id, signal_id, asset, action, analysis):
    global wins, losses, draws
    global unknown_results, completed_trades, pending_results

    result_name = "UNKNOWN"
    profit = 0.0

    try:
        time.sleep(EXPIRY_MINUTES * 60)

        result = api.check_win_v4(order_id)

        print("Raw trade result:", repr(result))

        if result is None:
            raise RuntimeError("IQ Option returned no trade result")

        profit = float(result)

        if profit > 0:
            result_name = "WIN"
        elif profit < 0:
            result_name = "LOSS"
        else:
            result_name = "DRAW"

        with state_lock:
            if result_name == "WIN":
                wins += 1
            elif result_name == "LOSS":
                losses += 1
            else:
                draws += 1

            completed_trades += 1

            current_wins = wins
            current_losses = losses
            current_draws = draws
            current_completed = completed_trades

        log_trade(
            signal_id, asset, action,
            result_name, profit, analysis
        )

        icon = "✅" if result_name == "WIN" else (
            "❌" if result_name == "LOSS" else "⚪"
        )

        send_telegram(
            icon + " <b>" + result_name + "</b>\n\n"
            "<b>Asset:</b> " + asset + "\n"
            "<b>Action:</b> " + action + "\n"
            "<b>Profit:</b> $" + str(round(profit, 2)) + "\n"
            "<b>Signal ID:</b> " + signal_id + "\n\n"
            "<b>Completed:</b> " + str(current_completed)
            + "/" + str(TARGET_TRADES) + "\n"
            "<b>Wins:</b> " + str(current_wins) + "\n"
            "<b>Losses:</b> " + str(current_losses) + "\n"
            "<b>Draws:</b> " + str(current_draws)
        )

        print(
            "RESULT:", result_name,
            "| Profit:", profit,
            "| Completed:", current_completed,
            "/", TARGET_TRADES
        )

    except Exception as exc:
        print("Trade monitoring error:", repr(exc))

        with state_lock:
            unknown_results += 1
            completed_trades += 1
            current_completed = completed_trades
            current_unknown = unknown_results

        log_trade(
            signal_id, asset, action,
            "UNKNOWN", profit, analysis
        )

        send_telegram(
            "⚠️ <b>TRADE RESULT UNKNOWN</b>\n\n"
            "<b>Asset:</b> " + asset + "\n"
            "<b>Signal ID:</b> " + signal_id + "\n"
            "<b>Reason:</b> " + str(exc) + "\n\n"
            "<b>Completed:</b> " + str(current_completed)
            + "/" + str(TARGET_TRADES) + "\n"
            "<b>Unknown:</b> " + str(current_unknown)
        )

    finally:
        with state_lock:
            pending_results -= 1


def connect_iq():
    global api

    email = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    if not email or not password:
        print("Missing IQ_EMAIL or IQ_PASSWORD.")
        return False

    try:
        print("Connecting to IQ Option...")

        api = IQ_Option(email, password)
        connected, reason = api.connect()

        if connected:
            print("Connected successfully.")
            return True

        print("Connection failed:", repr(reason))

    except Exception as exc:
        print("Connection error:", repr(exc))

    return False


def open_practice_trade(asset_name, action):
    trade_asset = str(asset_name).strip()

    if trade_asset.lower().startswith("front."):
        trade_asset = trade_asset[6:]
    elif trade_asset.lower().startswith("front_"):
        trade_asset = trade_asset[6:]

    try:
        result = api.buy(
            STAKE,
            trade_asset,
            action.lower(),
            EXPIRY_MINUTES
        )

        print("Raw api.buy result:", repr(result))

        if not isinstance(result, tuple) or len(result) < 2:
            return False, None, trade_asset, "Unexpected API response"

        success, order_id = result

        if success and order_id is not None:
            return True, order_id, trade_asset, ""

        return (
            False,
            None,
            trade_asset,
            "IQ Option rejected the trade: " + repr(order_id)
        )

    except Exception as exc:
        return False, None, trade_asset, repr(exc)


def send_completion():
    with state_lock:
        final_wins = wins
        final_losses = losses
        final_draws = draws
        final_unknown = unknown_results
        final_completed = completed_trades
        final_opened = total_trades

    decided = final_wins + final_losses

    win_rate = (
        final_wins / decided * 100.0
        if decided else 0.0
    )

    send_telegram(
        "<b>🏁 MOMENTUM 10 TEST COMPLETE</b>\n\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Trades opened:</b> " + str(final_opened)
        + "/" + str(TARGET_TRADES) + "\n"
        "<b>Completed:</b> " + str(final_completed) + "\n"
        "<b>Wins:</b> " + str(final_wins) + "\n"
        "<b>Losses:</b> " + str(final_losses) + "\n"
        "<b>Draws:</b> " + str(final_draws) + "\n"
        "<b>Unknown:</b> " + str(final_unknown) + "\n"
        "<b>Win rate excluding draws:</b> "
        + str(round(win_rate, 2)) + "%\n\n"
        "Controlled practice test finished."
    )


def run_scanner():
    global active_assets, total_trades, pending_results

    print("=" * 55)
    print("MOMENTUM 10 - EXPANDED FOREX WATCHLIST")
    print("PRACTICE MODE | $1 | 1-MINUTE EXPIRY")
    print("Target:", TARGET_TRADES, "successfully opened trades")
    print("=" * 55)

    if not connect_iq():
        send_telegram(
            "❌ <b>IQ Option connection failed.</b>"
        )
        return

    try:
        api.change_balance(BALANCE_MODE)
        print("Balance mode:", BALANCE_MODE)
    except Exception as exc:
        print("Balance mode error:", repr(exc))

    ensure_log_file()

    send_telegram(
        "<b>🤖 Momentum 10 Scanner</b>\n\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Stake:</b> $1\n"
        "<b>Expiry:</b> 1 minute\n"
        "<b>Target:</b> 50 opened trades\n\n"
        "Checking expanded currency watchlist..."
    )

    last_refresh = 0
    last_heartbeat = time.time()

    while True:
        with state_lock:
            opened = total_trades
            pending = pending_results

        if opened >= TARGET_TRADES and pending == 0:
            print("All 50 trade results resolved.")
            send_completion()
            break

        now = time.time()

        if (
            not active_assets
            or now - last_refresh >= ASSET_REFRESH_SECONDS
        ):
            active_assets = get_controlled_assets()
            last_refresh = now

            if active_assets:
                names = [
                    item["name"]
                    for item in active_assets.values()
                ]

                print("Trading assets:", ", ".join(names))

                send_telegram(
                    "🟢 <b>Asset discovery complete</b>\n\n"
                    "<b>Available for binary/turbo:</b>\n"
                    + ", ".join(names)
                    + "\n\nStarting controlled scan."
                )

            else:
                print("No matching binary/turbo assets available.")
                send_telegram(
                    "🟡 <b>Scanner waiting</b>\n\n"
                    "No requested pairs are confirmed open "
                    "for binary/turbo trading. Retrying."
                )

                time.sleep(RECONNECT_SECONDS)
                continue

        for key, item in list(active_assets.items()):
            with state_lock:
                if total_trades >= TARGET_TRADES:
                    break

            asset_name = item["name"]
            active_id = item["id"]

            try:
                candles = get_candles(
                    active_id,
                    MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5
                )

                if len(candles) < 20:
                    continue

                closed_candles = candles[:-1]

                if len(closed_candles) < 20:
                    continue

                signal_candle = closed_candles[-1]
                candle_time = signal_candle.get(
                    "from",
                    signal_candle.get("to", 0)
                )

                analysis = analyze_momentum(closed_candles)

                if not analysis or analysis["action"] is None:
                    continue

                extreme = analysis["extreme"]

                with state_lock:
                    if last_signal_candle.get(key) == candle_time:
                        continue

                    if last_extreme_state.get(key) == extreme:
                        continue

                    last_signal_candle[key] = candle_time
                    last_extreme_state[key] = extreme

                action = analysis["action"]

                signal_id = (
                    "M10-"
                    + asset_key(asset_name)
                    + "-"
                    + action
                    + "-"
                    + str(int(time.time()))
                )

                print(
                    "SIGNAL:", asset_name,
                    action,
                    "| Momentum:", analysis["current"],
                    "| ID:", signal_id
                )

                send_telegram(
                    build_signal_message(
                        asset_name,
                        analysis,
                        signal_id
                    )
                )

                if not AUTO_TRADE:
                    continue

                with state_lock:
                    if total_trades >= TARGET_TRADES:
                        continue

                success, order_id, trade_asset, error = (
                    open_practice_trade(
                        asset_name,
                        action
                    )
                )

                if not success:
                    print(
                        "TRADE NOT OPENED:",
                        trade_asset,
                        repr(error)
                    )

                    send_telegram(
                        "⚠️ <b>TRADE NOT OPENED</b>\n\n"
                        "<b>Asset:</b> " + trade_asset + "\n"
                        "<b>Action:</b> " + action + "\n"
                        "<b>Signal ID:</b> " + signal_id + "\n"
                        "<b>Reason:</b> " + str(error)
                    )

                    continue

                with state_lock:
                    total_trades += 1
                    pending_results += 1
                    trade_number = total_trades

                print(
                    "TRADE OPENED:",
                    trade_number,
                    "/",
                    TARGET_TRADES,
                    "| Order:",
                    order_id
                )

                send_telegram(
                    "🚀 <b>TRADE OPENED</b>\n\n"
                    "<b>Asset:</b> " + trade_asset + "\n"
                    "<b>Action:</b> " + action + "\n"
                    "<b>Stake:</b> $1\n"
                    "<b>Expiry:</b> 1 minute\n"
                    "<b>Trade:</b> " + str(trade_number)
                    + "/" + str(TARGET_TRADES) + "\n"
                    "<b>Signal ID:</b> " + signal_id
                )

                worker = threading.Thread(
                    target=monitor_trade,
                    args=(
                        order_id,
                        signal_id,
                        trade_asset,
                        action,
                        analysis
                    ),
                    daemon=True
                )

                worker.start()

            except Exception as exc:
                print(
                    "Scan error for",
                    asset_name,
                    repr(exc)
                )

        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            with state_lock:
                opened = total_trades
                completed = completed_trades
                pending = pending_results
                current_wins = wins
                current_losses = losses
                current_draws = draws
                current_unknown = unknown_results

            print(
                "HEARTBEAT:",
                opened,
                "/",
                TARGET_TRADES,
                "| W/L/D/U:",
                current_wins,
                current_losses,
                current_draws,
                current_unknown
            )

            send_telegram(
                "💓 <b>Scanner heartbeat</b>\n\n"
                "<b>Opened:</b> " + str(opened)
                + "/" + str(TARGET_TRADES) + "\n"
                "<b>Completed:</b> " + str(completed) + "\n"
                "<b>Pending:</b> " + str(pending) + "\n"
                "<b>W/L/D/U:</b> "
                + str(current_wins) + "/"
                + str(current_losses) + "/"
                + str(current_draws) + "/"
                + str(current_unknown)
            )

            last_heartbeat = time.time()

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run_scanner()
