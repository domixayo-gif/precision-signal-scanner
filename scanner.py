import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# CONTROLLED MOMENTUM 10 TEST
# IQ OPTION - PRACTICE MODE
# ============================================================

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
# CONTROLLED WATCHLIST
# ============================================================

WATCHLIST = [
    "TAO-OTC",
    "ONDO-OTC",
    "PALLADIUM-OTC",
    "USD/BRL-OTC",
    "EURUSD-OTC",
    "EURUSD",
]


# ============================================================
# SECRETS
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# GLOBAL STATE
# ============================================================

api = None

active_assets = []
active_ids = {}

trade_count = 0
wins = 0
losses = 0
pending_results = 0

signal_count = 0

last_signal_candle = {}
extreme_state = {}

stop_event = threading.Event()

state_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        requests.post(
            url,
            data=data,
            timeout=15,
        )
    except Exception as exc:
        print(
            "Telegram error:",
            exc,
            flush=True,
        )


# ============================================================
# CSV LOG
# ============================================================

def init_log():
    if os.path.exists(LOG_FILE):
        return

    try:
        with open(
            LOG_FILE,
            "w",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                "time",
                "asset",
                "direction",
                "signal_id",
                "entry_price",
                "result",
                "profit",
            ])

    except Exception as exc:
        print(
            "Log init error:",
            exc,
            flush=True,
        )


def log_trade(
    asset,
    direction,
    signal_id,
    entry_price,
    result,
    profit,
):
    try:
        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                datetime.now(
                    timezone.utc
                ).isoformat(),
                asset,
                direction,
                signal_id,
                entry_price,
                result,
                profit,
            ])

    except Exception as exc:
        print(
            "Log error:",
            exc,
            flush=True,
        )


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global api

    print(
        "Connecting to IQ Option...",
        flush=True,
    )

    try:
        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        connected, reason = api.connect()

        if not connected:
            print(
                "IQ OPTION CONNECTION FAILED:",
                reason,
                flush=True,
            )

            api = None
            return False

        print(
            "🟢 IQ OPTION CONNECTED",
            flush=True,
        )

        try:
            api.change_balance(
                BALANCE_MODE
            )
        except Exception as exc:
            print(
                "Balance mode warning:",
                exc,
                flush=True,
            )

        print(
            "Practice mode active.",
            flush=True,
        )

        return True

    except Exception as exc:
        print(
            "Connection error:",
            exc,
            flush=True,
        )

        api = None
        return False


# ============================================================
# ASSET NAME NORMALIZATION
# ============================================================

def clean_asset_name(name):
    if not name:
        return ""

    value = str(name).strip()

    if "." in value:
        value = value.split(
            ".",
            1,
        )[1]

    value = value.replace(
        "_",
        "/",
    )

    return value.strip()


def asset_key(name):
    value = clean_asset_name(name)

    return (
        value.upper()
        .replace(" ", "")
        .replace("_", "")
        .replace("/", "")
        .replace("-", "")
    )


def is_allowed_asset(name):
    wanted = asset_key(name)

    for item in WATCHLIST:
        if wanted == asset_key(item):
            return True

    return False


# ============================================================
# CONTROLLED ASSET DISCOVERY
# ============================================================

def add_asset(
    name,
    active_id,
    found_assets,
    found_ids,
):
    if not name:
        return

    try:
        active_id = int(active_id)
    except Exception:
        return

    cleaned = clean_asset_name(name)

    if not is_allowed_asset(cleaned):
        return

    matched_name = None

    for item in WATCHLIST:
        if asset_key(item) == asset_key(cleaned):
            matched_name = item
            break

    if matched_name is None:
        return

    if matched_name in found_ids:
        return

    found_assets.append(matched_name)
    found_ids[matched_name] = active_id


def scan_init_sections(
    data,
    found_assets,
    found_ids,
):
    if not isinstance(data, dict):
        return

    sections = [
        data.get("turbo", {}),
        data.get("binary", {}),
    ]

    for section in sections:

        if not isinstance(section, dict):
            continue

        actives = section.get(
            "actives",
            {},
        )

        if not isinstance(actives, dict):
            continue

        for key, info in actives.items():

            if not isinstance(info, dict):
                continue

            active_id = info.get(
                "active_id"
            )

            if active_id is None:
                active_id = key

            name = info.get(
                "name",
                "",
            )

            if not name:
                continue

            enabled = info.get(
                "enabled"
            )

            suspended = info.get(
                "is_suspended"
            )

            if enabled is False:
                continue

            if suspended is True:
                continue

            add_asset(
                name,
                active_id,
                found_assets,
                found_ids,
            )


def scan_legacy_sections(
    data,
    found_assets,
    found_ids,
):
    if not isinstance(data, dict):
        return

    result = data.get(
        "result",
        {},
    )

    if not isinstance(result, dict):
        return

    sections = [
        result.get("turbo", {}),
        result.get("binary", {}),
    ]

    for section in sections:

        if not isinstance(section, dict):
            continue

        actives = section.get(
            "actives",
            {},
        )

        if not isinstance(actives, dict):
            continue

        for key, info in actives.items():

            if not isinstance(info, dict):
                continue

            active_id = info.get(
                "active_id"
            )

            if active_id is None:
                active_id = key

            name = info.get(
                "name",
                "",
            )

            if not name:
                continue

            enabled = info.get(
                "enabled"
            )

            suspended = info.get(
                "is_suspended"
            )

            if enabled is False:
                continue

            if suspended is True:
                continue

            add_asset(
                name,
                active_id,
                found_assets,
                found_ids,
            )


def get_controlled_assets():
    global active_assets
    global active_ids

    print(
        "Preparing controlled watchlist...",
        flush=True,
    )

    found_assets = []
    found_ids = {}

    try:
        data = None

        try:
            data = api.get_all_init_v2()
        except Exception as exc:
            print(
                "get_all_init_v2 warning: "
                + str(exc),
                flush=True,
            )

        scan_init_sections(
            data,
            found_assets,
            found_ids,
        )

        if not found_assets:

            print(
                "Trying legacy asset discovery...",
                flush=True,
            )

            try:
                data = api.get_all_init()
            except Exception as exc:
                print(
                    "get_all_init warning: "
                    + str(exc),
                    flush=True,
                )
                data = None

            scan_legacy_sections(
                data,
                found_assets,
                found_ids,
            )

        if not found_assets:

            print(
                "❌ None of the six "
                "controlled assets were found.",
                flush=True,
            )

            return False

        active_assets = []

        for item in WATCHLIST:
            if item in found_ids:
                active_assets.append(item)

        active_ids = {}

        for item in active_assets:
            active_ids[item] = found_ids[item]

            OP_code.ACTIVES[item] = (
                found_ids[item]
            )

        print("", flush=True)

        print(
            "🎯 CONTROLLED WATCHLIST READY",
            flush=True,
        )

        print(
            "Allowed instruments: 6",
            flush=True,
        )

        print(
            "Currently available: "
            + str(len(active_assets)),
            flush=True,
        )

        for item in WATCHLIST:
            if item in active_ids:
                print(
                    "✅ "
                    + item
                    + " | ID "
                    + str(active_ids[item]),
                    flush=True,
                )
            else:
                print(
                    "⚠️ "
                    + item
                    + " | NOT AVAILABLE",
                    flush=True,
                )

        print("", flush=True)

        return True

    except Exception as exc:
        print(
            "Controlled asset discovery error:",
            exc,
            flush=True,
        )

        return False


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(
    asset,
    count,
):
    active_id = active_ids.get(asset)

    if active_id is None:
        print(
            "No active ID for "
            + asset,
            flush=True,
        )

        return []

    try:
        api.api.candles.candles_data = []

        server_time = time.time()

        try:
            server_time = (
                api.api.timesync.server_timestamp
            )
        except Exception:
            pass

        api.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time,
        )

        started = time.time()

        while (
            time.time() - started
            < CANDLE_REQUEST_TIMEOUT
        ):

            candles = (
                api.api.candles.candles_data
            )

            if candles:
                break

            time.sleep(0.1)

        candles = (
            api.api.candles.candles_data
        )

        if not candles:
            return []

        result = []

        for candle in candles:

            if not isinstance(
                candle,
                dict,
            ):
                continue

            try:
                result.append({
                    "from": float(
                        candle.get("from")
                    ),
                    "open": float(
                        candle.get("open")
                    ),
                    "close": float(
                        candle.get("close")
                    ),
                    "high": float(
                        candle.get("max")
                    ),
                    "low": float(
                        candle.get("min")
                    ),
                })

            except Exception:
                continue

        return result

    except Exception as exc:
        print(
            "Candle error "
            + asset
            + ": "
            + str(exc),
            flush=True,
        )

        return []


def remove_open_candle(candles):
    if len(candles) < 2:
        return candles

    now = time.time()

    last = candles[-1]

    candle_from = last.get(
        "from",
        0,
    )

    if (
        now - candle_from
        < CANDLE_SECONDS
    ):
        return candles[:-1]

    return candles


# ============================================================
# MOMENTUM 10
# ============================================================

def calculate_momentum(candles):
    values = []

    if len(candles) <= MOMENTUM_PERIOD:
        return values

    for index in range(
        MOMENTUM_PERIOD,
        len(candles),
    ):

        current = candles[index]["close"]

        previous = candles[
            index - MOMENTUM_PERIOD
        ]["close"]

        if previous == 0:
            continue

        momentum = (
            current / previous
        ) * 100.0

        values.append(momentum)

    return values


def analyze_momentum(candles):
    if len(candles) < (
        MOMENTUM_LOOKBACK + 3
    ):
        return None

    momentum_values = calculate_momentum(
        candles
    )

    if len(momentum_values) < (
        MOMENTUM_LOOKBACK + 3
    ):
        return None

    recent = momentum_values[
        -MOMENTUM_LOOKBACK:
    ]

    current = momentum_values[-1]
    previous = momentum_values[-2]
    previous_previous = momentum_values[-3]

    sorted_values = sorted(recent)

    low_index = int(
        len(sorted_values)
        * EXTREME_PERCENTILE
    )

    high_index = int(
        len(sorted_values)
        * (1.0 - EXTREME_PERCENTILE)
    )

    if low_index >= len(sorted_values):
        low_index = (
            len(sorted_values) - 1
        )

    if high_index >= len(sorted_values):
        high_index = (
            len(sorted_values) - 1
        )

    low_threshold = (
        sorted_values[low_index]
    )

    high_threshold = (
        sorted_values[high_index]
    )

    recent_low = min(recent)
    recent_high = max(recent)

    signal = None
    extreme = None
    reversal_strength = 0.0

    if current <= low_threshold:

        extreme = "LOW"

        if (
            current > previous
            and previous < previous_previous
        ):

            turn_distance = (
                current - previous
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                signal = "CALL"

                if recent_low != 0:

                    reversal_strength = (
                        turn_distance
                        / abs(recent_low)
                    ) * 100.0

    elif current >= high_threshold:

        extreme = "HIGH"

        if (
            current < previous
            and previous > previous_previous
        ):

            turn_distance = (
                previous - current
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                signal = "PUT"

                if recent_high != 0:

                    reversal_strength = (
                        turn_distance
                        / abs(recent_high)
                    ) * 100.0

    return {
        "signal": signal,
        "extreme": extreme,
        "momentum": current,
        "previous": previous,
        "previous_previous": previous_previous,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "low_threshold": low_threshold,
        "high_threshold": high_threshold,
        "reversal_strength": reversal_strength,
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    direction,
    analysis,
    price,
    signal_id,
):
    return (
        "🔔 <b>CONTROLLED TEST SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: "
        + asset
        + "\n"
        "Direction: "
        + direction
        + "\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Test: 6-Instrument Controlled Test\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Momentum: "
        + str(
            round(
                analysis["momentum"],
                5,
            )
        )
        + "\n"
        "Previous: "
        + str(
            round(
                analysis["previous"],
                5,
            )
        )
        + "\n"
        "Previous 2: "
        + str(
            round(
                analysis["previous_previous"],
                5,
            )
        )
        + "\n"
        "Extreme: "
        + str(
            analysis["extreme"]
        )
        + "\n"
        "Recent Low: "
        + str(
            round(
                analysis["recent_low"],
                5,
            )
        )
        + "\n"
        "Recent High: "
        + str(
            round(
                analysis["recent_high"],
                5,
            )
        )
        + "\n"
        "Low Threshold: "
        + str(
            round(
                analysis["low_threshold"],
                5,
            )
        )
        + "\n"
        "High Threshold: "
        + str(
            round(
                analysis["high_threshold"],
                5,
            )
        )
        + "\n"
        "Reversal Strength: "
        + str(
            round(
                analysis["reversal_strength"],
                1,
            )
        )
        + "%\n"
        "Price: "
        + str(price)
        + "\n"
        "Signal ID: "
        + signal_id
    )


# ============================================================
# TRADE RESULT MONITOR
# ============================================================

def monitor_trade(
    order_id,
    asset,
    direction,
    signal_id,
    entry_price,
):
    global pending_results
    global wins
    global losses

    try:
        time.sleep(
            (EXPIRY_MINUTES * 60)
            + 5
        )

        result = None

        for _ in range(20):

            try:
                result = api.check_win_v4(
                    order_id
                )
            except Exception:
                result = None

            if result is not None:
                break

            time.sleep(1)

        if result is None:

            print(
                "Could not get result for "
                + asset,
                flush=True,
            )

            return

        try:
            profit = float(result)
        except Exception:
            profit = 0.0

        if profit > 0:

            wins += 1
            result_text = "WIN"

        else:

            losses += 1
            result_text = "LOSS"

        log_trade(
            asset,
            direction,
            signal_id,
            entry_price,
            result_text,
            profit,
        )

        print(
            "📊 RESULT "
            + asset
            + " "
            + result_text
            + " "
            + str(profit),
            flush=True,
        )

        send_telegram(
            "📊 <b>CONTROLLED TEST RESULT</b>\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
            + "\n"
            "Result: "
            + result_text
            + "\n"
            "Profit: "
            + str(profit)
            + "\n"
            "Signal ID: "
            + signal_id
        )

    except Exception as exc:

        print(
            "Result monitor error: "
            + str(exc),
            flush=True,
        )

    finally:

        pending_results -= 1


# ============================================================
# EXECUTE TRADE
# ============================================================

def execute_trade(
    asset,
    direction,
    signal_id,
    entry_price,
):
    global trade_count
    global pending_results

    try:

        action = direction.lower()

        print(
            "Attempting trade: "
            + asset
            + " "
            + action,
            flush=True,
        )

        order_result = api.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES,
        )

        if isinstance(
            order_result,
            tuple,
        ):

            success = order_result[0]
            order_id = order_result[1]

        else:

            success = bool(
                order_result
            )

            order_id = order_result

        if not success:

            print(
                "❌ TRADE FAILED "
                + asset
                + " "
                + direction,
                flush=True,
            )

            return False

        trade_count += 1
        pending_results += 1

        print(
            "✅ TRADE OPENED "
            + asset
            + " "
            + direction
            + " Order: "
            + str(order_id),
            flush=True,
        )

        send_telegram(
            "✅ <b>TRADE OPENED</b>\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
            + "\n"
            "Stake: $"
            + str(STAKE)
            + "\n"
            "Expiry: 1 minute\n"
            "Progress: "
            + str(trade_count)
            + "/"
            + str(TARGET_TRADES)
            + "\n"
            "Signal ID: "
            + signal_id
        )

        thread = threading.Thread(
            target=monitor_trade,
            args=(
                order_id,
                asset,
                direction,
                signal_id,
                entry_price,
            ),
            daemon=True,
        )

        thread.start()

        return True

    except Exception as exc:

        print(
            "❌ TRADE EXCEPTION",
            flush=True,
        )

        print(
            "Asset: "
            + asset,
            flush=True,
        )

        print(
            "Direction: "
            + direction,
            flush=True,
        )

        print(
            "Error: "
            + str(exc),
            flush=True,
        )

        return False


# ============================================================
# PROCESS ASSET
# ============================================================

def process_asset(asset):
    global signal_count

    try:

        print(
            "Scanning: "
            + asset,
            flush=True,
        )

        candles = get_candles(
            asset,
            MOMENTUM_LOOKBACK
            + MOMENTUM_PERIOD
            + 10,
        )

        if not candles:
            return

        candles = remove_open_candle(
            candles
        )

        if len(candles) < (
            MOMENTUM_LOOKBACK + 3
        ):
            return

        analysis = analyze_momentum(
            candles
        )

        if not analysis:
            return

        signal = analysis["signal"]

        if signal is None:
            return

        current_candle = candles[-1]

        candle_id = current_candle["from"]

        if (
            last_signal_candle.get(asset)
            == candle_id
        ):
            return

        extreme = analysis["extreme"]

        if (
            extreme_state.get(asset)
            == extreme
        ):
            return

        last_signal_candle[asset] = (
            candle_id
        )

        extreme_state[asset] = extreme

        price = current_candle["close"]

        signal_count += 1

        signal_id = (
            "M10-"
            + asset.replace(
                "/",
                "",
            )
            .replace(
                "-",
                "",
            )
            + "-"
            + signal
            + "-"
            + str(
                int(
                    time.time()
                )
            )
        )

        message = build_signal_message(
            asset,
            signal,
            analysis,
            price,
            signal_id,
        )

        print("", flush=True)

        print(
            "🔔 CONTROLLED TEST SIGNAL",
            flush=True,
        )

        print(
            "Asset: "
            + asset,
            flush=True,
        )

        print(
            "Direction: "
            + signal,
            flush=True,
        )

        print(
            "Signal #: "
            + str(signal_count),
            flush=True,
        )

        print(
            "Trade progress: "
            + str(trade_count)
            + "/"
            + str(TARGET_TRADES),
            flush=True,
        )

        print(
            "Momentum: "
            + str(
                round(
                    analysis["momentum"],
                    5,
                )
            ),
            flush=True,
        )

        print(
            "Extreme: "
            + str(extreme),
            flush=True,
        )

        print(
            "Reversal Strength: "
            + str(
                round(
                    analysis[
                        "reversal_strength"
                    ],
                    1,
                )
            )
            + "%",
            flush=True,
        )

        print(
            "Price: "
            + str(price),
            flush=True,
        )

        print(
            "Signal ID: "
            + signal_id,
            flush=True,
        )

        send_telegram(message)

        if AUTO_TRADE:

            execute_trade(
                asset,
                signal,
                signal_id,
                price,
            )

    except Exception as exc:

        print(
            "Process asset error "
            + asset
            + ": "
            + str(exc),
            flush=True,
        )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():
    total_results = (
        wins + losses
    )

    win_rate = 0.0

    if total_results > 0:

        win_rate = (
            wins
            / total_results
        ) * 100.0

    print("", flush=True)

    print(
        "💚 CONTROLLED TEST BOT ALIVE",
        flush=True,
    )

    print(
        "━━━━━━━━━━━━━━━━━━",
        flush=True,
    )

    print(
        "Connection: OK",
        flush=True,
    )

    print(
        "Balance: "
        + BALANCE_MODE,
        flush=True,
    )

    print(
        "Auto Trading: "
        + str(AUTO_TRADE),
        flush=True,
    )

    print(
        "Strategy: Momentum 10",
        flush=True,
    )

    print(
        "Timeframe: 1M",
        flush=True,
    )

    print(
        "Expiry: 1 minute",
        flush=True,
    )

    print(
        "Watchlist: 6 instruments",
        flush=True,
    )

    print(
        "Available now: "
        + str(
            len(active_assets)
        ),
        flush=True,
    )

    print(
        "Signals: "
        + str(signal_count),
        flush=True,
    )

    print(
        "Trades: "
        + str(trade_count)
        + "/"
        + str(TARGET_TRADES),
        flush=True,
    )

    print(
        "Pending Results: "
        + str(pending_results),
        flush=True,
    )

    print(
        "Wins: "
        + str(wins),
        flush=True,
    )

    print(
        "Losses: "
        + str(losses),
        flush=True,
    )

    print(
        "Completed Results: "
        + str(total_results),
        flush=True,
    )

    print(
        "Win Rate: "
        + str(
            round(
                win_rate,
                1,
            )
        )
        + "%",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    global api

    init_log()

    print(
        "🚀 CONTROLLED MOMENTUM 10 BOT",
        flush=True,
    )

    print(
        "━━━━━━━━━━━━━━━━━━",
        flush=True,
    )

    print(
        "Mode: "
        + BALANCE_MODE,
        flush=True,
    )

    print(
        "Auto Trading: "
        + str(AUTO_TRADE),
        flush=True,
    )

    print(
        "Strategy: Momentum 10 Extreme-Reversal",
        flush=True,
    )

    print(
        "Timeframe: 1M",
        flush=True,
    )

    print(
        "Expiry: 1 minute",
        flush=True,
    )

    print(
        "Stake: $"
        + str(STAKE),
        flush=True,
    )

    print(
        "Target: "
        + str(TARGET_TRADES)
        + " executed trades",
        flush=True,
    )

    print(
        "Controlled watchlist:",
        flush=True,
    )

    for item in WATCHLIST:
        print(
            " - "
            + item,
            flush=True,
        )

    print("", flush=True)

    last_asset_refresh = 0
    last_heartbeat = 0

    while not stop_event.is_set():

        if api is None:

            if not connect_iq():

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

        try:

            if not api.check_connect():

                print(
                    "IQ Option disconnected.",
                    flush=True,
                )

                api = None

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

        except Exception:

            api = None

            time.sleep(
                RECONNECT_SECONDS
            )

            continue

        if not active_assets:

            if not get_controlled_assets():

                print(
                    "Controlled watchlist "
                    "unavailable. Retrying...",
                    flush=True,
                )

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

            last_asset_refresh = (
                time.time()
            )

            last_heartbeat = (
                time.time()
            )

        now = time.time()

        if (
            now - last_asset_refresh
            >= ASSET_REFRESH_SECONDS
        ):

            if get_controlled_assets():

                last_asset_refresh = now

        if (
            now - last_heartbeat
            >= HEARTBEAT_SECONDS
        ):

            heartbeat()

            last_heartbeat = now

        if trade_count >= TARGET_TRADES:

            print(
                "🎯 50-TRADE TARGET REACHED.",
                flush=True,
            )

            heartbeat()

            send_telegram(
                "🎯 <b>CONTROLLED TEST COMPLETE</b>\n"
                "Target: "
                + str(TARGET_TRADES)
                + " trades\n"
                "Wins: "
                + str(wins)
                + "\n"
                "Losses: "
                + str(losses)
                + "\n"
                "Signals: "
                + str(signal_count)
            )

            break

        for asset in list(
            active_assets
        ):

            if stop_event.is_set():
                break

            if trade_count >= TARGET_TRADES:
                break

            if not is_allowed_asset(asset):
                continue

            process_asset(asset)

            time.sleep(
                SCAN_INTERVAL
            )

        time.sleep(
            SCAN_INTERVAL
        )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "Bot stopped.",
            flush=True,
        )

    except Exception as exc:

        print(
            "FATAL ERROR: "
            + str(exc),
            flush=True,
        )
