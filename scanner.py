import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# EUR/USD OTC ONLY
# MOMENTUM 10 EXTREME-REVERSAL
# PRACTICE MODE
# ============================================================

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


# ============================================================
# ENVIRONMENT
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# STATE
# ============================================================

api = None
active_assets = {}

wins = 0
losses = 0
total_trades = 0

pending_results = 0

last_signal_candle = {}
extreme_state = {}

state_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(
            url,
            data=data,
            timeout=15
        )

        if response.status_code != 200:
            print("Telegram error:", response.text[:300])

    except Exception as error:
        print("Telegram exception:", error)


# ============================================================
# ASSET NAME NORMALIZATION
# ============================================================

def clean_asset_name(name):
    if not name:
        return ""

    value = str(name).strip()

    if "." in value:
        value = value.split(".", 1)[1]

    value = value.replace("_", "/")
    value = value.replace(" ", "")

    return value.strip()


def asset_key(name):
    value = clean_asset_name(name).upper()

    result = ""

    for char in value:
        if char.isalnum():
            result += char

    return result


def is_allowed_asset(name):
    key = asset_key(name)

    for allowed in WATCHLIST:
        if key == asset_key(allowed):
            return True

    return False


# ============================================================
# ASSET DISCOVERY
# ============================================================

def add_asset(found, name, active_id, info=None):
    if not name:
        return

    if active_id is None:
        return

    if not is_allowed_asset(name):
        return

    if info is None:
        info = {}

    if isinstance(info, dict):
        if info.get("enabled") is False:
            return

        if info.get("is_suspended") is True:
            return

        if info.get("disabled") is True:
            return

    found[asset_key(name)] = {
        "name": clean_asset_name(name),
        "id": active_id
    }


def scan_active_section(found, section):
    if not isinstance(section, dict):
        return

    actives = section.get("actives")

    if not isinstance(actives, dict):
        return

    for active_id, info in actives.items():

        if not isinstance(info, dict):
            continue

        name = (
            info.get("name")
            or info.get("symbol")
            or info.get("display_name")
            or info.get("instrument")
            or info.get("active_name")
        )

        if name:
            add_asset(
                found,
                name,
                active_id,
                info
            )


def scan_binary_sections(found, data):
    if not isinstance(data, dict):
        return

    for section_name in [
        "binary",
        "turbo"
    ]:
        section = data.get(section_name)

        if isinstance(section, dict):
            scan_active_section(
                found,
                section
            )


def scan_nested_binary(found, data):
    if not isinstance(data, dict):
        return

    for key, value in data.items():

        if not isinstance(value, dict):
            continue

        key_lower = str(key).lower()

        if key_lower in [
            "binary",
            "turbo"
        ]:
            scan_active_section(
                found,
                value
            )

            scan_nested_binary(
                found,
                value
            )

        else:
            scan_nested_binary(
                found,
                value
            )


def find_otc_candidates(data):
    candidates = []

    def walk(value, binary_context=False):

        if isinstance(value, dict):

            local_context = binary_context

            for key in value.keys():
                key_lower = str(key).lower()

                if key_lower in [
                    "binary",
                    "turbo"
                ]:
                    local_context = True

            if local_context:

                possible_name = (
                    value.get("name")
                    or value.get("symbol")
                    or value.get("display_name")
                    or value.get("instrument")
                    or value.get("active_name")
                )

                if possible_name:
                    text = str(possible_name).upper()

                    if "EUR" in text or "USD" in text:
                        if "OTC" in text:
                            if text not in candidates:
                                candidates.append(text)

            for child in value.values():
                walk(child, local_context)

        elif isinstance(value, list):

            for child in value:
                walk(child, binary_context)

    walk(data)

    return candidates


def get_controlled_assets():

    global active_assets

    found = {}

    print("")
    print("Searching IQ Option for EUR/USD OTC...")
    print("Allowed instruments: 1")
    print("Target: EUR/USD OTC")
    print("")

    # --------------------------------------------------------
    # Method 1: get_all_init_v2
    # --------------------------------------------------------

    try:
        data = api.get_all_init_v2()

        if isinstance(data, dict):
            scan_binary_sections(
                found,
                data
            )

            scan_nested_binary(
                found,
                data
            )

    except Exception as error:
        print("V2 asset discovery error:", error)

    # --------------------------------------------------------
    # Method 2: legacy get_all_init
    # --------------------------------------------------------

    if not found:

        try:
            data = api.get_all_init()

            if isinstance(data, dict):
                scan_binary_sections(
                    found,
                    data
                )

                scan_nested_binary(
                    found,
                    data
                )

        except Exception as error:
            print("Legacy asset discovery error:", error)

    # --------------------------------------------------------
    # Result
    # --------------------------------------------------------

    if found:

        active_assets = {}

        for key, item in found.items():

            name = item["name"]
            active_id = item["id"]

            active_assets[key] = active_id

            try:
                OP_code.ACTIVES[item["name"]] = active_id
            except Exception:
                pass

            print(
                "FOUND:",
                name,
                "| ID",
                active_id
            )

        print("")
        print("EUR/USD OTC found successfully.")
        print("")

        return active_assets

    # --------------------------------------------------------
    # Diagnostics
    # --------------------------------------------------------

    print("")
    print("EUR/USD OTC was NOT found.")
    print("")

    print("Checking API data for EUR/USD OTC candidates...")

    try:
        data = api.get_all_init_v2()

        candidates = find_otc_candidates(data)

        if candidates:
            for candidate in candidates:
                print(
                    "Possible OTC candidate:",
                    candidate
                )
        else:
            print(
                "No EUR/USD OTC candidate name was exposed."
            )

    except Exception as error:
        print(
            "Diagnostic discovery error:",
            error
        )

    print("")

    active_assets = {}

    return {}


# ============================================================
# CANDLES
# ============================================================

def get_candles(active_id, count):
    try:
        api.api.candles.candles_data = []

        server_time = api.timesync.server_timestamp

        api.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time
        )

        deadline = time.time() + CANDLE_REQUEST_TIMEOUT

        while time.time() < deadline:

            candles = api.api.candles.candles_data

            if candles:
                return candles

            time.sleep(0.2)

    except Exception as error:
        print(
            "Candle error:",
            error
        )

    return []


# ============================================================
# MOMENTUM
# ============================================================

def calculate_momentum(candles):
    if len(candles) < MOMENTUM_PERIOD + 3:
        return None

    closes = []

    for candle in candles:
        try:
            closes.append(float(candle["close"]))
        except Exception:
            pass

    if len(closes) < MOMENTUM_PERIOD + 3:
        return None

    values = []

    for i in range(MOMENTUM_PERIOD, len(closes)):

        current = closes[i]
        previous = closes[i - MOMENTUM_PERIOD]

        if previous == 0:
            continue

        momentum = (
            (current - previous)
            / previous
        ) * 100

        values.append(momentum)

    if len(values) < 5:
        return None

    return values


# ============================================================
# EXTREME + REVERSAL
# ============================================================

def analyze_momentum(values):

    if len(values) < 5:
        return None

    current = values[-1]
    previous = values[-2]
    previous2 = values[-3]

    lookback = values[
        -MOMENTUM_LOOKBACK:
    ]

    sorted_values = sorted(lookback)

    extreme_count = max(
        1,
        int(
            len(sorted_values)
            * EXTREME_PERCENTILE
        )
    )

    low_threshold = sorted_values[
        extreme_count - 1
    ]

    high_threshold = sorted_values[
        -extreme_count
    ]

    extreme = None
    action = None

    if current <= low_threshold:

        extreme = "LOW"

        if (
            previous < previous2
            and current > previous
        ):
            action = "CALL"

    elif current >= high_threshold:

        extreme = "HIGH"

        if (
            previous > previous2
            and current < previous
        ):
            action = "PUT"

    if not extreme:
        return None

    turn_distance = abs(
        current - previous
    )

    if turn_distance < MIN_TURN_DISTANCE:
        return {
            "action": None,
            "extreme": extreme,
            "current": current,
            "previous": previous,
            "previous2": previous2,
            "turn_distance": turn_distance
        }

    if not action:
        return {
            "action": None,
            "extreme": extreme,
            "current": current,
            "previous": previous,
            "previous2": previous2,
            "turn_distance": turn_distance
        }

    return {
        "action": action,
        "extreme": extreme,
        "current": current,
        "previous": previous,
        "previous2": previous2,
        "turn_distance": turn_distance
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    action,
    analysis,
    signal_id
):

    emoji = "🟢" if action == "CALL" else "🔴"

    message = (
        "🚨 <b>MOMENTUM 10 SIGNAL</b>\n"
        "\n"
        "Test: EUR/USD OTC Controlled Test\n"
        "\n"
        "Asset: <b>"
        + asset
        + "</b>\n"
        "Direction: <b>"
        + emoji
        + " "
        + action
        + "</b>\n"
        "Expiry: <b>1 MINUTE</b>\n"
        "\n"
        "Momentum: "
        + str(round(analysis["current"], 5))
        + "\n"
        "Previous: "
        + str(round(analysis["previous"], 5))
        + "\n"
        "Previous 2: "
        + str(round(analysis["previous2"], 5))
        + "\n"
        "\n"
        "Extreme: <b>"
        + analysis["extreme"]
        + "</b>\n"
        "Reversal Strength: "
        + str(
            round(
                analysis["turn_distance"],
                5
            )
        )
        + "%\n"
        "\n"
        "Signal ID: <code>"
        + signal_id
        + "</code>\n"
        "\n"
        "Practice Mode: ON"
    )

    return message


# ============================================================
# TRADE LOG
# ============================================================

def log_trade(
    asset,
    action,
    signal_id,
    result,
    profit
):

    file_exists = os.path.exists(
        LOG_FILE
    )

    try:

        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8"
        ) as file:

            writer = csv.writer(file)

            if not file_exists:
                writer.writerow([
                    "time",
                    "asset",
                    "action",
                    "signal_id",
                    "result",
                    "profit"
                ])

            writer.writerow([
                datetime.now(
                    timezone.utc
                ).isoformat(),
                asset,
                action,
                signal_id,
                result,
                profit
            ])

    except Exception as error:
        print(
            "Log error:",
            error
        )


# ============================================================
# TRADE RESULT
# ============================================================

def monitor_trade(
    asset,
    action,
    signal_id,
    order_id
):

    global wins
    global losses
    global pending_results

    try:

        print(
            "Monitoring:",
            signal_id
        )

        deadline = (
            time.time()
            + (EXPIRY_MINUTES * 60)
            + 30
        )

        result = None
        profit = 0

        while time.time() < deadline:

            try:

                result_data = (
                    api.check_win_v4(
                        order_id
                    )
                )

                if result_data is not None:

                    profit = float(
                        result_data
                    )

                    if profit > 0:
                        result = "WIN"
                    elif profit < 0:
                        result = "LOSS"
                    else:
                        result = "DRAW"

                    break

            except Exception:
                pass

            time.sleep(2)

        if result is None:
            result = "UNKNOWN"

        with state_lock:

            if result == "WIN":
                wins += 1

            elif result == "LOSS":
                losses += 1

        log_trade(
            asset,
            action,
            signal_id,
            result,
            profit
        )

        total = wins + losses

        if total > 0:
            win_rate = (
                wins / total
            ) * 100
        else:
            win_rate = 0

        message = (
            "📊 <b>EUR/USD OTC RESULT</b>\n"
            "\n"
            "Signal: <code>"
            + signal_id
            + "</code>\n"
            "Result: <b>"
            + result
            + "</b>\n"
            "Profit: "
            + str(round(profit, 2))
            + "\n"
            "\n"
            "Wins: "
            + str(wins)
            + "\n"
            "Losses: "
            + str(losses)
            + "\n"
            "Win Rate: "
            + str(round(win_rate, 1))
            + "%\n"
            "Completed: "
            + str(total)
            + "/"
            + str(TARGET_TRADES)
        )

        send_telegram(message)

    except Exception as error:

        print(
            "Trade monitor error:",
            error
        )

    finally:

        with state_lock:
            pending_results -= 1


# ============================================================
# MAIN SCANNER
# ============================================================

def run_scanner():

    global api
    global total_trades
    global pending_results

    if not IQ_EMAIL or not IQ_PASSWORD:
        print(
            "IQ_EMAIL or IQ_PASSWORD is missing."
        )
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "Telegram credentials are missing."
        )
        return

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

    send_telegram(
        "🤖 <b>Crypto Signal Bot</b>\n"
        "\n"
        "Strategy: Momentum 10\n"
        "Asset: <b>EUR/USD OTC ONLY</b>\n"
        "Mode: <b>PRACTICE</b>\n"
        "Auto Trading: <b>ON</b>\n"
        "Expiry: <b>1 MINUTE</b>\n"
        "Target: <b>50 bot trades</b>\n"
        "\n"
        "Controlled EUR/USD OTC test started."
    )

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    print("Connecting to IQ Option...")

    connected, reason = api.connect()

    if not connected:

        print(
            "Connection failed:",
            reason
        )

        send_telegram(
            "❌ <b>IQ Option connection failed</b>\n"
            "\n"
            + str(reason)
        )

        return

    print("Connected successfully.")

    try:
        api.change_balance(
            BALANCE_MODE
        )
    except Exception as error:
        print(
            "Balance mode error:",
            error
        )

    send_telegram(
        "🟢 <b>IQ Option connected</b>\n"
        "\n"
        "Searching for EUR/USD OTC..."
    )

    last_refresh = 0
    last_heartbeat = 0

    while True:

        now = time.time()

        # ----------------------------------------------------
        # Refresh asset
        # ----------------------------------------------------

        if (
            not active_assets
            or now - last_refresh
            >= ASSET_REFRESH_SECONDS
        ):

            active_assets = (
                get_controlled_assets()
            )

            last_refresh = now

            if not active_assets:

                print(
                    "EUR/USD OTC unavailable. "
                    "Retrying..."
                )

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

            send_telegram(
                "✅ <b>EUR/USD OTC FOUND</b>\n"
                "\n"
                "The scanner is now watching "
                "EUR/USD OTC only."
            )

        # ----------------------------------------------------
        # Target reached
        # ----------------------------------------------------

        with state_lock:

            completed = wins + losses

        if (
            completed >= TARGET_TRADES
            and pending_results == 0
        ):

            if completed > 0:
                win_rate = (
                    wins / completed
                ) * 100
            else:
                win_rate = 0

            print("")
            print("=" * 60)
            print("50-TRADE TEST COMPLETE")
            print(
                "Wins:",
                wins
            )
            print(
                "Losses:",
                losses
            )
            print(
                "Win rate:",
                round(win_rate, 2),
                "%"
            )
            print("=" * 60)

            send_telegram(
                "🏁 <b>EUR/USD OTC TEST COMPLETE</b>\n"
                "\n"
                "Completed: "
                + str(completed)
                + "\n"
                "Wins: "
                + str(wins)
                + "\n"
                "Losses: "
                + str(losses)
                + "\n"
                "Win Rate: "
                + str(round(win_rate, 2))
                + "%"
            )

            break

        # ----------------------------------------------------
        # Scan EUR/USD OTC
        # ----------------------------------------------------

        for key, active_id in list(
            active_assets.items()
        ):

            asset_name = "EUR/USD-OTC"

            candles = get_candles(
                active_id,
                MOMENTUM_LOOKBACK
                + MOMENTUM_PERIOD
                + 5
            )

            if not candles:
                continue

            # Remove open candle
            if len(candles) > 1:
                candles = candles[:-1]

            values = calculate_momentum(
                candles
            )

            if not values:
                continue

            analysis = analyze_momentum(
                values
            )

            if not analysis:
                continue

            action = analysis["action"]

            if not action:
                continue

            candle_time = candles[-1].get(
                "from",
                int(time.time())
            )

            last_time = (
                last_signal_candle.get(
                    key
                )
            )

            if last_time == candle_time:
                continue

            last_signal_candle[
                key
            ] = candle_time

            current_extreme = (
                analysis["extreme"]
            )

            previous_extreme = (
                extreme_state.get(key)
            )

            if (
                previous_extreme
                == current_extreme
            ):
                continue

            extreme_state[
                key
            ] = current_extreme

            signal_id = (
                "M10-EURUSDOTC-"
                + action
                + "-"
                + str(int(time.time()))
            )

            message = build_signal_message(
                asset_name,
                action,
                analysis,
                signal_id
            )

            print("")
            print("=" * 60)
            print("SIGNAL")
            print("Asset:", asset_name)
            print("Action:", action)
            print("Signal ID:", signal_id)
            print("=" * 60)

            send_telegram(
                message
            )

            # ------------------------------------------------
            # Execute practice trade
            # ------------------------------------------------

            if AUTO_TRADE:

                try:

                    with state_lock:
                        pending_results += 1

                    success, order_id = api.buy(
                        STAKE,
                        active_id,
                        action.lower(),
                        EXPIRY_MINUTES
                    )

                    if success:

                        with state_lock:
                            total_trades += 1

                        print(
                            "Trade opened:",
                            order_id
                        )

                        completed = (
                            wins + losses
                        )

                        send_telegram(
                            "🚀 <b>EUR/USD OTC TRADE OPENED</b>\n"
                            "\n"
                            "Direction: <b>"
                            + action
                            + "</b>\n"
                            "Expiry: <b>1 MINUTE</b>\n"
                            "Stake: $"
                            + str(STAKE)
                            + "\n"
                            "Signal ID: <code>"
                            + signal_id
                            + "</code>\n"
                            "\n"
                            "Progress: "
                            + str(completed + pending_results)
                            + "/"
                            + str(TARGET_TRADES)
                        )

                        thread = threading.Thread(
                            target=monitor_trade,
                            args=(
                                asset_name,
                                action,
                                signal_id,
                                order_id
                            ),
                            daemon=True
                        )

                        thread.start()

                    else:

                        with state_lock:
                            pending_results -= 1

                        print(
                            "Trade was not opened."
                        )

                        send_telegram(
                            "⚠️ <b>EUR/USD OTC TRADE FAILED</b>\n"
                            "\n"
                            "Direction: "
                            + action
                            + "\n"
                            "Signal ID: <code>"
                            + signal_id
                            + "</code>"
                        )

                except Exception as error:

                    with state_lock:
                        pending_results -= 1

                    print(
                        "Trade error:",
                        error
                    )

            time.sleep(
                SCAN_INTERVAL
            )

        # ----------------------------------------------------
        # Heartbeat
        # ----------------------------------------------------

        if (
            time.time()
            - last_heartbeat
            >= HEARTBEAT_SECONDS
        ):

            with state_lock:

                completed = (
                    wins + losses
                )

                pending = (
                    pending_results
                )

            if completed > 0:
                win_rate = (
                    wins / completed
                ) * 100
            else:
                win_rate = 0

            heartbeat = (
                "💓 <b>EUR/USD OTC SCANNER</b>\n"
                "\n"
                "Status: Running\n"
                "Asset: EUR/USD OTC only\n"
                "Wins: "
                + str(wins)
                + "\n"
                "Losses: "
                + str(losses)
                + "\n"
                "Win Rate: "
                + str(round(win_rate, 1))
                + "%\n"
                "Completed: "
                + str(completed)
                + "/"
                + str(TARGET_TRADES)
                + "\n"
                "Pending: "
                + str(pending)
            )

            send_telegram(
                heartbeat
            )

            last_heartbeat = time.time()

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    run_scanner()()
