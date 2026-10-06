import os
import csv
import time
import threading
from datetime import datetime

import requests
from iqoptionapi.stable_api import IQ_Option


WATCHLIST = ["EURUSD-OTC"]

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
RECONNECT_SECONDS = 15
CANDLE_REQUEST_TIMEOUT = 8

LOG_FILE = "momentum_signal_log.csv"


api = None

active_assets = {}

wins = 0
losses = 0
draws = 0
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

    url = "https://api.telegram.org/bot" + token + "/sendMessage"

    parts = []
    while message:
        parts.append(message[:3900])
        message = message[3900:]

    for part in parts:
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
            print("Telegram error:", exc)


def clean_asset_name(name):
    if not name:
        return ""

    text = str(name)
    text = text.replace("_", "-")
    text = text.replace(" ", "")
    return text.upper()


def asset_key(name):
    text = clean_asset_name(name)

    chars = []
    for char in text:
        if char.isalnum():
            chars.append(char)

    return "".join(chars)


def is_allowed_asset(name):
    key = asset_key(name)

    for wanted in WATCHLIST:
        if key == asset_key(wanted):
            return True

    return False


def add_asset(found, name, active_id):
    if name is None or active_id is None:
        return

    if not is_allowed_asset(name):
        return

    key = asset_key(name)

    if not key:
        return

    found[key] = {
        "name": str(name),
        "id": active_id
    }


def extract_asset_name(info, fallback):
    if not isinstance(info, dict):
        return fallback

    fields = [
        "name",
        "symbol",
        "display_name",
        "displayName",
        "instrument",
        "active_name",
        "activeName"
    ]

    for field in fields:
        value = info.get(field)

        if value:
            return str(value)

    return fallback


def scan_active_container(container, found):
    if not isinstance(container, dict):
        return

    for raw_key, info in container.items():
        active_id = None
        fallback_name = None

        if isinstance(raw_key, int):
            active_id = raw_key
        elif isinstance(raw_key, str):
            if raw_key.isdigit():
                active_id = int(raw_key)
            else:
                fallback_name = raw_key

        if isinstance(info, dict):
            if info.get("id") is not None:
                active_id = info.get("id")

            if info.get("active_id") is not None:
                active_id = info.get("active_id")

            if info.get("activeId") is not None:
                active_id = info.get("activeId")

        name = extract_asset_name(info, fallback_name)

        if name and active_id is not None:
            add_asset(found, name, active_id)


def scan_active_section(section, found):
    if not isinstance(section, dict):
        return

    actives = section.get("actives")

    if isinstance(actives, dict):
        scan_active_container(actives, found)

    elif isinstance(actives, list):
        for item in actives:
            if not isinstance(item, dict):
                continue

            active_id = item.get("id")
            if active_id is None:
                active_id = item.get("active_id")

            name = extract_asset_name(item, None)

            if name and active_id is not None:
                add_asset(found, name, active_id)


def recursive_asset_scan(obj, found, depth=0):
    if depth > 12:
        return

    if isinstance(obj, dict):
        if "actives" in obj:
            scan_active_section(obj, found)

        for key, value in obj.items():
            key_text = str(key).lower()

            if key_text in [
                "binary",
                "turbo",
                "digital",
                "otc",
                "forex"
            ]:
                if isinstance(value, dict):
                    scan_active_section(value, found)

            recursive_asset_scan(value, found, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            recursive_asset_scan(item, found, depth + 1)


def find_otc_candidates(obj, results, depth=0):
    if depth > 10:
        return

    if isinstance(obj, dict):
        for key, value in obj.items():
            key_text = str(key)

            if "eur" in key_text.lower() or "usd" in key_text.lower():
                results.append(key_text)

            if isinstance(value, dict):
                name = extract_asset_name(value, "")
                if "eur" in name.lower() or "usd" in name.lower():
                    results.append(name)

            find_otc_candidates(value, results, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            find_otc_candidates(item, results, depth + 1)


def get_controlled_assets():
    found = {}

    print("")
    print("Searching for EUR/USD OTC...")

    try:
        data = api.get_all_init_v2()

        if data:
            recursive_asset_scan(data, found)
    except Exception as exc:
        print("V2 asset discovery error:", exc)

    if not found:
        try:
            data = api.get_all_init()

            if data:
                recursive_asset_scan(data, found)
        except Exception as exc:
            print("Legacy asset discovery error:", exc)

    if found:
        print("")
        print("EUR/USD OTC found.")

        for key, item in found.items():
            print("Asset:", item["name"])
            print("Active ID:", item["id"])

        return found

    diagnostics = []

    try:
        data = api.get_all_init_v2()

        if data:
            find_otc_candidates(data, diagnostics)
    except Exception:
        pass

    if diagnostics:
        print("")
        print("EUR/USD diagnostic candidates:")

        seen = set()

        for item in diagnostics:
            if item in seen:
                continue

            seen.add(item)
            print("-", item)

    print("")
    print("EUR/USD OTC was not found.")

    return {}


def get_candles(active_id, count):
    global api

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

        if candles:
            return candles

    except Exception as exc:
        print("Candle error:", exc)

    return []


def calculate_momentum(candles):
    closes = []

    for candle in candles:
        try:
            close = float(candle["close"])
            closes.append(close)
        except Exception:
            continue

    if len(closes) < MOMENTUM_PERIOD + 3:
        return []

    momentum = []

    start = MOMENTUM_PERIOD

    for i in range(start, len(closes)):
        old_price = closes[i - MOMENTUM_PERIOD]

        if old_price == 0:
            continue

        value = ((closes[i] - old_price) / old_price) * 100.0
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

    if action is None:
        return {
            "action": None,
            "current": current,
            "previous": previous,
            "previous_two": previous_two,
            "low_level": low_level,
            "high_level": high_level,
            "extreme": extreme
        }

    if extreme == "LOW":
        reversal_strength = 0.0

        if previous != 0:
            reversal_strength = (
                abs(current - previous)
                / max(abs(previous), 0.000001)
            ) * 100.0

    else:
        reversal_strength = 0.0

        if previous != 0:
            reversal_strength = (
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
        "reversal_strength": reversal_strength
    }


def build_signal_message(asset_name, analysis, signal_id):
    action = analysis["action"]

    if action == "CALL":
        direction = "🟢 CALL"
    else:
        direction = "🔴 PUT"

    current = analysis["current"]
    previous = analysis["previous"]
    previous_two = analysis["previous_two"]
    extreme = analysis["extreme"]
    strength = analysis.get("reversal_strength", 0.0)

    message = (
        "<b>🤖 Crypto Signal Bot</b>\n\n"
        "<b>Strategy:</b> Momentum 10\n"
        "<b>Asset:</b> " + asset_name + "\n"
        "<b>Signal:</b> " + direction + "\n"
        "<b>Expiry:</b> 1 MINUTE\n"
        "<b>Mode:</b> PRACTICE\n\n"
        "<b>Momentum:</b> "
        + str(round(current, 5))
        + "\n"
        "<b>Previous:</b> "
        + str(round(previous, 5))
        + "\n"
        "<b>Previous 2:</b> "
        + str(round(previous_two, 5))
        + "\n"
        "<b>Extreme:</b> "
        + extreme
        + "\n"
        "<b>Reversal Strength:</b> "
        + str(round(strength, 2))
        + "%\n\n"
        "<b>Signal ID:</b> "
        + signal_id
    )

    return message


def ensure_log_file():
    if os.path.exists(LOG_FILE):
        return

    try:
        with open(LOG_FILE, "w", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                "timestamp",
                "signal_id",
                "asset",
                "action",
                "stake",
                "expiry",
                "result",
                "profit",
                "momentum",
                "previous",
                "previous_two",
                "extreme"
            ])

    except Exception as exc:
        print("Log setup error:", exc)


def log_trade(
    signal_id,
    asset,
    action,
    result,
    profit,
    analysis
):
    ensure_log_file()

    try:
        with open(LOG_FILE, "a", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                datetime.utcnow().isoformat(),
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
        print("Log error:", exc)


def monitor_trade(
    order_id,
    signal_id,
    asset,
    action,
    analysis
):
    global wins
    global losses
    global draws
    global completed_trades
    global pending_results

    result_name = "UNKNOWN"
    profit = 0.0

    try:
        print("")
        print("Monitoring:", signal_id)
        print("Order ID:", order_id)

        result = api.check_win_v4(order_id)

        try:
            profit = float(result)
        except Exception:
            profit = 0.0

        if profit > 0:
            result_name = "WIN"

            with state_lock:
                wins += 1

        elif profit < 0:
            result_name = "LOSS"

            with state_lock:
                losses += 1

        else:
            result_name = "DRAW"

            with state_lock:
                draws += 1

        with state_lock:
            completed_trades += 1

            current_wins = wins
            current_losses = losses
            current_draws = draws
            current_completed = completed_trades

        log_trade(
            signal_id,
            asset,
            action,
            result_name,
            profit,
            analysis
        )

        if result_name == "WIN":
            icon = "✅"
        elif result_name == "LOSS":
            icon = "❌"
        else:
            icon = "⚪"

        message = (
            icon
            + " <b>"
            + result_name
            + "</b>\n\n"
            + "<b>Asset:</b> "
            + asset
            + "\n"
            + "<b>Action:</b> "
            + action
            + "\n"
            + "<b>Profit:</b> $"
            + str(round(profit, 2))
            + "\n"
            + "<b>Signal ID:</b> "
            + signal_id
            + "\n\n"
            + "<b>Completed:</b> "
            + str(current_completed)
            + "/"
            + str(TARGET_TRADES)
            + "\n"
            + "<b>Wins:</b> "
            + str(current_wins)
            + "\n"
            + "<b>Losses:</b> "
            + str(current_losses)
            + "\n"
            + "<b>Draws:</b> "
            + str(current_draws)
        )

        send_telegram(message)

        print("")
        print("RESULT:", result_name)
        print("Profit:", profit)
        print(
            "Completed:",
            current_completed,
            "/",
            TARGET_TRADES
        )
        print(
            "W/L/D:",
            current_wins,
            "/",
            current_losses,
            "/",
            current_draws
        )

    except Exception as exc:
        print("Trade monitoring error:", exc)

        with state_lock:
            completed_trades += 1

        log_trade(
            signal_id,
            asset,
            action,
            result_name,
            profit,
            analysis
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

    print("")
    print("Connecting to IQ Option...")

    try:
        api = IQ_Option(email, password)

        check, reason = api.connect()

        if check:
            print("Connected successfully.")
            return True

        print("Connection failed:", reason)

    except Exception as exc:
        print("Connection error:", exc)

    return False


def send_startup():
    message = (
        "<b>🤖 Crypto Signal Bot</b>\n\n"
        "<b>Strategy:</b> Momentum 10\n"
        "<b>Asset:</b> <b>EUR/USD OTC ONLY</b>\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Auto Trading:</b> ON\n"
        "<b>Expiry:</b> 1 MINUTE\n"
        "<b>Target:</b> 50 bot trades\n\n"
        "Controlled EUR/USD OTC test started.\n\n"
        "🟢 <b>IQ Option connected</b>\n\n"
        "Searching for EUR/USD OTC..."
    )

    send_telegram(message)


def send_completion():
    with state_lock:
        final_wins = wins
        final_losses = losses
        final_draws = draws
        final_completed = completed_trades

    decided = final_wins + final_losses

    if decided > 0:
        win_rate = (
            final_wins / decided
        ) * 100.0
    else:
        win_rate = 0.0

    message = (
        "<b>🏁 EUR/USD OTC TEST COMPLETE</b>\n\n"
        "<b>Strategy:</b> Momentum 10\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Completed Trades:</b> "
        + str(final_completed)
        + "/"
        + str(TARGET_TRADES)
        + "\n"
        "<b>Wins:</b> "
        + str(final_wins)
        + "\n"
        "<b>Losses:</b> "
        + str(final_losses)
        + "\n"
        "<b>Draws:</b> "
        + str(final_draws)
        + "\n"
        "<b>Win Rate:</b> "
        + str(round(win_rate, 2))
        + "%\n\n"
        "Controlled test finished."
    )

    send_telegram(message)


def run_scanner():
    global active_assets
    global total_trades
    global pending_results

    print("")
    print("=" * 60)
    print("MOMENTUM 10 EXTREME-REVERSAL")
    print("IQ OPTION OTC")
    print("PRACTICE MODE")
    print("=" * 60)
    print("")
    print("EUR/USD OTC ONLY")
    print("Target trades:", TARGET_TRADES)
    print("Stake:", STAKE)
    print("Expiry:", EXPIRY_MINUTES, "minute")
    print("")

    if not connect_iq():
        send_telegram(
            "❌ <b>IQ Option connection failed.</b>\n"
            "Controlled EUR/USD OTC test stopped."
        )
        return

    send_startup()

    try:
        api.change_balance(BALANCE_MODE)
    except Exception as exc:
        print("Balance mode error:", exc)

    ensure_log_file()

    active_assets = {}
    last_refresh = 0
    last_heartbeat = time.time()

    while True:
        with state_lock:
            current_completed = completed_trades
            current_pending = pending_results

        if (
            current_completed >= TARGET_TRADES
            and current_pending == 0
        ):
            print("")
            print("Target of 50 resolved bot trades reached.")
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
                for key, item in active_assets.items():
                    print(
                        "Controlled asset:",
                        item["name"],
                        "| ID:",
                        item["id"]
                    )

                send_telegram(
                    "🟢 <b>EUR/USD OTC found</b>\n\n"
                    "<b>Asset:</b> "
                    + list(active_assets.values())[0]["name"]
                    + "\n"
                    "<b>Starting controlled scan.</b>"
                )

            else:
                print(
                    "Controlled EUR/USD OTC unavailable. "
                    "Retrying..."
                )

                time.sleep(RECONNECT_SECONDS)
                continue

        for key, item in list(active_assets.items()):
            asset_name = item["name"]
            active_id = item["id"]

            try:
                candles = get_candles(
                    active_id,
                    MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5
                )

                if not candles:
                    continue

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

                analysis = analyze_momentum(
                    closed_candles
                )

                if not analysis:
                    continue

                extreme = analysis["extreme"]

                if analysis["action"] is None:
                    continue

                with state_lock:
                    previous_candle = last_signal_candle.get(
                        key
                    )

                    previous_extreme = last_extreme_state.get(
                        key
                    )

                    if previous_candle == candle_time:
                        continue

                    if previous_extreme == extreme:
                        continue

                    last_signal_candle[key] = candle_time
                    last_extreme_state[key] = extreme

                action = analysis["action"]

                signal_id = (
                    "M10-EURUSDOTC-"
                    + action
                    + "-"
                    + str(int(time.time()))
                )

                print("")
                print("=" * 50)
                print("SIGNAL")
                print("Asset:", asset_name)
                print("Action:", action)
                print("Signal ID:", signal_id)
                print("Momentum:", analysis["current"])
                print("Extreme:", analysis["extreme"])
                print("=" * 50)

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
                    if (
                        completed_trades
                        + pending_results
                        >= TARGET_TRADES
                    ):
                        continue

                    pending_results += 1

                try:
                    success, order_id = api.buy(
                        STAKE,
                        asset_name,
                        action.lower(),
                        EXPIRY_MINUTES
                    )

                    if not success:
                        print("Trade was not opened.")

                        with state_lock:
                            pending_results -= 1

                        send_telegram(
                            "⚠️ <b>Trade not opened</b>\n\n"
                            "<b>Asset:</b> "
                            + asset_name
                            + "\n"
                            "<b>Action:</b> "
                            + action
                            + "\n"
                            "<b>Signal ID:</b> "
                            + signal_id
                        )

                        continue

                    with state_lock:
                        total_trades += 1
                        opened_number = total_trades

                    print("")
                    print("Trade opened successfully.")
                    print("Order ID:", order_id)
                    print(
                        "Bot trade:",
                        opened_number
                    )

                    send_telegram(
                        "🚀 <b>TRADE OPENED</b>\n\n"
                        "<b>Asset:</b> "
                        + asset_name
                        + "\n"
                        "<b>Action:</b> "
                        + action
                        + "\n"
                        "<b>Stake:</b> $"
                        + str(STAKE)
                        + "\n"
                        "<b>Expiry:</b> 1 minute\n"
                        "<b>Bot Trade #:</b> "
                        + str(opened_number)
                        + "\n"
                        "<b>Signal ID:</b> "
                        + signal_id
                    )

                    worker = threading.Thread(
                        target=monitor_trade,
                        args=(
                            order_id,
                            signal_id,
                            asset_name,
                            action,
                            analysis
                        )
                    )

                    worker.daemon = True
                    worker.start()

                except Exception as exc:
                    print(
                        "Trade opening error:",
                        exc
                    )

                    with state_lock:
                        pending_results -= 1

                    send_telegram(
                        "❌ <b>Trade opening error</b>\n\n"
                        + str(exc)
                    )

            except Exception as exc:
                print(
                    "Scan error for",
                    asset_name,
                    ":",
                    exc
                )

        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            with state_lock:
                heartbeat_completed = completed_trades
                heartbeat_pending = pending_results
                heartbeat_wins = wins
                heartbeat_losses = losses
                heartbeat_draws = draws

            print("")
            print("HEARTBEAT")
            print(
                "Completed:",
                heartbeat_completed,
                "/",
                TARGET_TRADES
            )
            print("Pending:", heartbeat_pending)
            print(
                "W/L/D:",
                heartbeat_wins,
                "/",
                heartbeat_losses,
                "/",
                heartbeat_draws
            )

            send_telegram(
                "💓 <b>Scanner heartbeat</b>\n\n"
                "<b>EUR/USD OTC</b>\n"
                "<b>Completed:</b> "
                + str(heartbeat_completed)
                + "/"
                + str(TARGET_TRADES)
                + "\n"
                "<b>Pending:</b> "
                + str(heartbeat_pending)
                + "\n"
                "<b>W/L/D:</b> "
                + str(heartbeat_wins)
                + "/"
                + str(heartbeat_losses)
                + "/"
                + str(heartbeat_draws)
            )

            last_heartbeat = time.time()

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run_scanner())
