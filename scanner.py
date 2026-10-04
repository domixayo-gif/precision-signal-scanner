import os
import csv
import time
import uuid
import statistics
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL
# IQ OPTION OTC - PRACTICE MODE
# ============================================================

MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50

# Stronger extremes
EXTREME_PERCENTILE = 0.10

# Require a clear turn
REQUIRE_TURN = True

# Require the current Momentum to move a minimum amount
MIN_TURN_DISTANCE = 0.03

# 1-minute strategy
CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

# Trading
AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0

# Target
TARGET_TRADES = 50

# Scanner
SCAN_INTERVAL = 3
ASSET_REFRESH_SECONDS = 1800
HEARTBEAT_SECONDS = 300

MAX_ASSETS = 70

LOG_FILE = "momentum_signal_log.csv"


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

total_trades = 0
wins = 0
losses = 0

last_signal_candle = {}
extreme_state = {}

trade_records = {}


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(
            url,
            data=data,
            timeout=20
        )

        return response.ok

    except Exception as exc:
        print("Telegram error:", exc)
        return False


# ============================================================
# CSV LOG
# ============================================================

def create_log_file():
    if os.path.exists(LOG_FILE):
        return

    headers = [
        "timestamp",
        "asset",
        "direction",
        "candle_time",
        "expiry_minutes",
        "momentum",
        "previous_momentum",
        "previous_previous_momentum",
        "recent_low",
        "recent_high",
        "low_threshold",
        "high_threshold",
        "extreme",
        "strength",
        "price",
        "signal_id",
        "trade_id",
        "result"
    ]

    try:
        with open(
            LOG_FILE,
            "w",
            newline="",
            encoding="utf-8"
        ) as file:
            writer = csv.writer(file)
            writer.writerow(headers)

    except Exception as exc:
        print("Log creation error:", exc)


def log_signal(
    asset,
    direction,
    candle_time,
    momentum,
    previous,
    previous_previous,
    recent_low,
    recent_high,
    low_threshold,
    high_threshold,
    extreme,
    strength,
    price,
    signal_id,
    trade_id="",
    result=""
):
    row = [
        datetime.now(timezone.utc).isoformat(),
        asset,
        direction,
        candle_time,
        EXPIRY_MINUTES,
        round(momentum, 5),
        round(previous, 5),
        round(previous_previous, 5),
        round(recent_low, 5),
        round(recent_high, 5),
        round(low_threshold, 5),
        round(high_threshold, 5),
        extreme,
        round(strength, 2),
        price,
        signal_id,
        trade_id,
        result
    ]

    try:
        with open(
            LOG_FILE,
            "a",
            newline="",
            encoding="utf-8"
        ) as file:
            writer = csv.writer(file)
            writer.writerow(row)

    except Exception as exc:
        print("Log error:", exc)


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    print("Connecting to IQ Option...")

    api = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    try:
        connected = api.connect()

    except Exception as exc:
        print("Connection error:", exc)
        return None

    if not connected:
        print("IQ Option connection failed.")
        return None

    print("IQ Option connected.")

    try:
        api.change_balance(BALANCE_MODE)
        print("Balance:", BALANCE_MODE)

    except Exception as exc:
        print("Balance mode warning:", exc)

    return api


# ============================================================
# OTC ASSET DISCOVERY
# ============================================================

def extract_otc_assets(data):
    assets = []

    if not isinstance(data, dict):
        return assets

    def walk(obj):
        if not isinstance(obj, dict):
            return

        for key, value in obj.items():

            if isinstance(key, str):
                name = key.upper()

                if "-OTC" in name:
                    if isinstance(value, dict):
                        opened = value.get("open")

                        if opened is True:
                            if key not in assets:
                                assets.append(key)

                    elif value is True:
                        if key not in assets:
                            assets.append(key)

            if isinstance(value, dict):
                walk(value)

    walk(data)

    return sorted(assets)


def get_otc_assets(api):
    print("Refreshing OTC assets...")

    data = None

    try:
        data = api.get_all_init_v2()

    except Exception as exc:
        print("get_all_init_v2 error:", exc)

    if data is None:
        try:
            data = api.get_all_init()

        except Exception as exc:
            print("get_all_init error:", exc)

    assets = extract_otc_assets(data)

    if not assets:
        print("No OTC assets found.")
        return []

    assets = assets[:MAX_ASSETS]

    print("OTC assets:", len(assets))

    return assets


# ============================================================
# CANDLE VALIDATION
# ============================================================

def get_candles(api, asset, count):
    try:
        now = int(time.time())

        candles = api.get_candles(
            asset,
            CANDLE_SECONDS,
            count,
            now
        )

    except Exception as exc:
        print(asset, "candle error:", exc)
        return []

    if not candles:
        return []

    valid = []

    for candle in candles:
        try:
            timestamp = int(candle["from"])
            close = float(candle["close"])

            if close <= 0:
                continue

            valid.append({
                "time": timestamp,
                "open": float(candle["open"]),
                "high": float(candle["max"]),
                "low": float(candle["min"]),
                "close": close
            })

        except Exception:
            continue

    valid.sort(key=lambda x: x["time"])

    return valid


def remove_open_candle(candles):
    if len(candles) < 2:
        return candles

    now = int(time.time())

    latest = candles[-1]

    candle_end = latest["time"] + CANDLE_SECONDS

    if candle_end > now:
        return candles[:-1]

    return candles


# ============================================================
# MOMENTUM 10
# ============================================================

def calculate_momentum(closes, period):
    values = []

    if len(closes) <= period:
        return values

    for i in range(period, len(closes)):
        previous_close = closes[i - period]

        if previous_close <= 0:
            continue

        value = (
            closes[i] / previous_close
        ) * 100.0

        if math_is_valid(value):
            values.append(value)

    return values


def math_is_valid(value):
    try:
        return value == value and value != float("inf")
    except Exception:
        return False


# ============================================================
# PERCENTILE
# ============================================================

def percentile(values, percent):
    if not values:
        return 0.0

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percent

    lower = int(position)
    upper = lower + 1

    if upper >= len(ordered):
        return ordered[-1]

    fraction = position - lower

    return (
        ordered[lower]
        + (
            ordered[upper] - ordered[lower]
        ) * fraction
    )


# ============================================================
# MOMENTUM ANALYSIS
# ============================================================

def analyze_momentum(candles):
    closes = []

    for candle in candles:
        closes.append(candle["close"])

    momentum = calculate_momentum(
        closes,
        MOMENTUM_PERIOD
    )

    required = MOMENTUM_LOOKBACK + 3

    if len(momentum) < required:
        return None

    recent = momentum[-MOMENTUM_LOOKBACK:]

    current = momentum[-1]
    previous = momentum[-2]
    previous_previous = momentum[-3]

    low_threshold = percentile(
        recent,
        EXTREME_PERCENTILE
    )

    high_threshold = percentile(
        recent,
        1.0 - EXTREME_PERCENTILE
    )

    recent_low = min(recent)
    recent_high = max(recent)

    # --------------------------------------------------------
    # TURN CONFIRMATION
    # --------------------------------------------------------

    rising_now = current > previous
    falling_now = current < previous

    previous_falling = previous < previous_previous
    previous_rising = previous > previous_previous

    upward_turn = (
        rising_now
        and previous_falling
    )

    downward_turn = (
        falling_now
        and previous_rising
    )

    # --------------------------------------------------------
    # DISTANCE FROM PREVIOUS MOMENTUM
    # --------------------------------------------------------

    turn_distance = abs(
        current - previous
    )

    strong_turn = (
        turn_distance >= MIN_TURN_DISTANCE
    )

    # --------------------------------------------------------
    # EXTREME DETECTION
    # --------------------------------------------------------

    low_extreme = current <= low_threshold
    high_extreme = current >= high_threshold

    direction = None
    extreme = None

    if low_extreme and upward_turn:
        if REQUIRE_TURN and not strong_turn:
            return None

        direction = "CALL"
        extreme = "LOW"

    elif high_extreme and downward_turn:
        if REQUIRE_TURN and not strong_turn:
            return None

        direction = "PUT"
        extreme = "HIGH"

    else:
        return None

    # --------------------------------------------------------
    # STRENGTH
    # --------------------------------------------------------

    if extreme == "LOW":
        distance = high_threshold - low_threshold

        if distance <= 0:
            strength = 0.0
        else:
            strength = (
                (low_threshold - current)
                / distance
            ) * 100.0

            strength = abs(strength)

    else:
        distance = high_threshold - low_threshold

        if distance <= 0:
            strength = 0.0
        else:
            strength = (
                (current - high_threshold)
                / distance
            ) * 100.0

            strength = abs(strength)

    if strength < 50:
        strength = 50.0

    if strength > 100:
        strength = 100.0

    return {
        "direction": direction,
        "extreme": extreme,
        "momentum": current,
        "previous": previous,
        "previous_previous": previous_previous,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "low_threshold": low_threshold,
        "high_threshold": high_threshold,
        "strength": strength,
        "candle_time": candles[-1]["time"],
        "price": candles[-1]["close"]
    }


# ============================================================
# SIGNAL ID
# ============================================================

def make_signal_id(asset, direction, candle_time):
    clean_asset = asset.replace("/", "")
    clean_asset = clean_asset.replace("-", "")

    return (
        "M10-"
        + clean_asset
        + "-"
        + direction
        + "-"
        + str(candle_time)
    )


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def is_duplicate_signal(asset, candle_time):
    previous_time = last_signal_candle.get(asset)

    if previous_time is None:
        return False

    return previous_time == candle_time


def mark_signal(asset, candle_time):
    last_signal_candle[asset] = candle_time


# ============================================================
# EXTREME STATE
# ============================================================

def update_state(asset, current_extreme):
    previous = extreme_state.get(asset)

    if current_extreme is None:
        extreme_state[asset] = "CENTER"

    else:
        extreme_state[asset] = current_extreme

    return previous


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    analysis,
    signal_id
):
    direction = analysis["direction"]
    extreme = analysis["extreme"]

    message = (
        "🔔 MOMENTUM 10 SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Extreme: " + extreme + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Momentum: "
        + str(round(analysis["momentum"], 5))
        + "\n"
        "Previous: "
        + str(round(analysis["previous"], 5))
        + "\n"
        "Previous 2: "
        + str(round(analysis["previous_previous"], 5))
        + "\n"
        "Recent Low: "
        + str(round(analysis["recent_low"], 5))
        + "\n"
        "Recent High: "
        + str(round(analysis["recent_high"], 5))
        + "\n"
        "Low Threshold: "
        + str(round(analysis["low_threshold"], 5))
        + "\n"
        "High Threshold: "
        + str(round(analysis["high_threshold"], 5))
        + "\n"
        "Reversal Strength: "
        + str(round(analysis["strength"], 1))
        + "%\n"
        "Price: "
        + str(analysis["price"])
        + "\n"
        "Signal ID: "
        + signal_id
    )

    return message


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_trade(api, asset, direction):
    global total_trades

    if not AUTO_TRADE:
        return None

    if total_trades >= TARGET_TRADES:
        return None

    if direction == "CALL":
        action = "call"
    else:
        action = "put"

    try:
        success, trade_id = api.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES
        )

    except Exception as exc:
        print(
            asset,
            direction,
            "trade error:",
            exc
        )

        return None

    if not success:
        print(
            asset,
            direction,
            "trade rejected"
        )

        send_telegram(
            "❌ TRADE REJECTED\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction
        )

        return None

    total_trades += 1

    print(
        "TRADE ACCEPTED:",
        asset,
        direction,
        "ID:",
        trade_id
    )

    return str(trade_id)


# ============================================================
# RESULT CHECK
# ============================================================

def check_trade_result(api, trade_id):
    try:
        result = api.check_win_v4(trade_id)

        if result is None:
            return None

        if result > 0:
            return "WIN"

        if result < 0:
            return "LOSS"

        return "DRAW"

    except Exception as exc:
        print(
            "Result error:",
            trade_id,
            exc
        )

        return None


# ============================================================
# TRADE RESULT WAIT
# ============================================================

def process_trade_result(
    api,
    asset,
    direction,
    trade_id,
    signal_id,
    analysis
):
    global wins
    global losses

    wait_seconds = (
        EXPIRY_MINUTES * 60
    ) + 5

    time.sleep(wait_seconds)

    result = check_trade_result(
        api,
        trade_id
    )

    if result is None:
        print(
            "Could not determine result:",
            trade_id
        )

        return

    if result == "WIN":
        wins += 1

    elif result == "LOSS":
        losses += 1

    total_closed = wins + losses

    if total_closed > 0:
        win_rate = (
            wins / total_closed
        ) * 100.0
    else:
        win_rate = 0.0

    print(
        "RESULT:",
        asset,
        direction,
        result,
        "W:",
        wins,
        "L:",
        losses,
        "Rate:",
        round(win_rate, 1),
        "%"
    )

    message = (
        "📊 TRADE RESULT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: " + asset + "\n"
        "Direction: " + direction + "\n"
        "Result: " + result + "\n"
        "Trade ID: " + trade_id + "\n"
        "Signal ID: " + signal_id + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Wins: " + str(wins) + "\n"
        "Losses: " + str(losses) + "\n"
        "Win Rate: "
        + str(round(win_rate, 1))
        + "%"
    )

    send_telegram(message)

    log_signal(
        asset,
        direction,
        analysis["candle_time"],
        analysis["momentum"],
        analysis["previous"],
        analysis["previous_previous"],
        analysis["recent_low"],
        analysis["recent_high"],
        analysis["low_threshold"],
        analysis["high_threshold"],
        analysis["extreme"],
        analysis["strength"],
        analysis["price"],
        signal_id,
        trade_id,
        result
    )


# ============================================================
# PROCESS ONE ASSET
# ============================================================

def process_asset(api, asset):
    candles = get_candles(
        api,
        asset,
        MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 10
    )

    if not candles:
        return None

    # Never analyze an unfinished candle.
    candles = remove_open_candle(candles)

    minimum_candles = (
        MOMENTUM_LOOKBACK
        + MOMENTUM_PERIOD
        + 5
    )

    if len(candles) < minimum_candles:
        return None

    analysis = analyze_momentum(candles)

    if analysis is None:
        update_state(asset, None)
        return None

    candle_time = analysis["candle_time"]

    if is_duplicate_signal(
        asset,
        candle_time
    ):
        return None

    signal_id = make_signal_id(
        asset,
        analysis["direction"],
        candle_time
    )

    previous_state = extreme_state.get(
        asset,
        "CENTER"
    )

    current_state = analysis["extreme"]

    # Only allow a new signal when entering
    # a new extreme state.
    if previous_state == current_state:
        return None

    update_state(
        asset,
        current_state
    )

    mark_signal(
        asset,
        candle_time
    )

    message = build_signal_message(
        asset,
        analysis,
        signal_id
    )

    print("\n" + message)

    send_telegram(message)

    trade_id = execute_trade(
        api,
        asset,
        analysis["direction"]
    )

    log_signal(
        asset,
        analysis["direction"],
        candle_time,
        analysis["momentum"],
        analysis["previous"],
        analysis["previous_previous"],
        analysis["recent_low"],
        analysis["recent_high"],
        analysis["low_threshold"],
        analysis["high_threshold"],
        analysis["extreme"],
        analysis["strength"],
        analysis["price"],
        signal_id,
        trade_id or "",
        ""
    )

    if trade_id:
        trade_message = (
            "🟢 DEMO TRADE ACCEPTED\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Trade: "
            + str(total_trades)
            + "/"
            + str(TARGET_TRADES)
            + "\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + analysis["direction"]
            + "\n"
            "Stake: $"
            + str(STAKE)
            + "\n"
            "Expiry: 1 minute\n"
            "Extreme: "
            + analysis["extreme"]
            + "\n"
            "Momentum: "
            + str(round(
                analysis["momentum"],
                5
            ))
            + "\n"
            "Trade ID: "
            + trade_id
            + "\n"
            "Signal ID: "
            + signal_id
        )

        send_telegram(trade_message)

        trade_records[trade_id] = {
            "asset": asset,
            "direction": analysis["direction"],
            "signal_id": signal_id,
            "analysis": analysis
        }

    return True


# ============================================================
# HEARTBEAT
# ============================================================

def send_heartbeat(assets):
    closed = wins + losses

    if closed > 0:
        rate = (
            wins / closed
        ) * 100.0
    else:
        rate = 0.0

    message = (
        "🟢 MOMENTUM 10 SCANNER ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Connection: OK\n"
        "Balance: PRACTICE\n"
        "Auto Trading: ON\n"
        "Strategy: Momentum 10\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Assets: "
        + str(len(assets))
        + "\n"
        "Trades: "
        + str(total_trades)
        + "/"
        + str(TARGET_TRADES)
        + "\n"
        "Wins: "
        + str(wins)
        + "\n"
        "Losses: "
        + str(losses)
        + "\n"
        "Win Rate: "
        + str(round(rate, 1))
        + "%"
    )

    send_telegram(message)

    print(message)


# ============================================================
# MAIN
# ============================================================

def main():
    if not IQ_EMAIL or not IQ_PASSWORD:
        print("Missing IQ_EMAIL or IQ_PASSWORD.")
        return

    create_log_file()

    api = connect_iq()

    if api is None:
        return

    assets = get_otc_assets(api)

    if not assets:
        print("No OTC assets available.")
        return

    send_telegram(
        "🚀 MOMENTUM 10 SCANNER STARTED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Mode: PRACTICE\n"
        "Auto Trading: ON\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Stake: $"
        + str(STAKE)
        + "\n"
        "Target: "
        + str(TARGET_TRADES)
        + " trades"
    )

    last_refresh = time.time()
    last_heartbeat = 0

    while total_trades < TARGET_TRADES:

        now = time.time()

        if (
            now - last_refresh
            >= ASSET_REFRESH_SECONDS
        ):
            new_assets = get_otc_assets(api)

            if new_assets:
                assets = new_assets

            last_refresh = now

        if (
            now - last_heartbeat
            >= HEARTBEAT_SECONDS
        ):
            send_heartbeat(assets)
            last_heartbeat = now

        for asset in assets:

            if total_trades >= TARGET_TRADES:
                break

            try:
                process_asset(
                    api,
                    asset
                )

            except Exception as exc:
                print(
                    "Asset processing error:",
                    asset,
                    exc
                )

            time.sleep(SCAN_INTERVAL)

    final_message = (
        "🏁 MOMENTUM 10 TARGET REACHED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Trades: "
        + str(total_trades)
        + "\n"
        "Wins: "
        + str(wins)
        + "\n"
        "Losses: "
        + str(losses)
    )

    closed = wins + losses

    if closed > 0:
        final_rate = (
            wins / closed
        ) * 100.0

        final_message += (
            "\nWin Rate: "
            + str(round(final_rate, 1))
            + "%"
        )

    send_telegram(final_message)

    print(final_message)


if __name__ == "__main__":
    main()
