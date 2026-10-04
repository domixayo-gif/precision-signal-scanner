import os
import csv
import time
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# MOMENTUM 10 EXTREME-REVERSAL
# IQ OPTION OTC - PRACTICE MODE
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

SCAN_INTERVAL = 3
ASSET_REFRESH_SECONDS = 1800
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 20

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
# GLOBALS
# ============================================================

total_trades = 0
wins = 0
losses = 0

last_signal_candle = {}
extreme_state = {}

last_heartbeat = 0
last_asset_refresh = 0


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are missing.")
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

        if response.ok:
            print("Telegram message sent.")
            return True

        print(
            "Telegram HTTP error:",
            response.status_code,
            response.text[:300]
        )

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
        print("CSV creation error:", exc)


def log_signal(
    asset,
    direction,
    candle_time,
    analysis,
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
        round(analysis["momentum"], 5),
        round(analysis["previous"], 5),
        round(analysis["previous_previous"], 5),
        round(analysis["recent_low"], 5),
        round(analysis["recent_high"], 5),
        round(analysis["low_threshold"], 5),
        round(analysis["high_threshold"], 5),
        analysis["extreme"],
        round(analysis["strength"], 2),
        analysis["price"],
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
        print("CSV log error:", exc)


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    print("Connecting to IQ Option...")

    if not IQ_EMAIL or not IQ_PASSWORD:
        print("IQ credentials are missing.")
        return None

    try:
        api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        connected = api.connect()

    except Exception as exc:
        print("IQ connection exception:", exc)
        return None

    if not connected:
        print("IQ Option connection failed.")
        return None

    print("IQ Option connection successful.")

    try:
        api.change_balance(BALANCE_MODE)
        print("Balance mode:", BALANCE_MODE)

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
                upper_key = key.upper()

                if "-OTC" in upper_key:

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
    print("Refreshing IQ Option OTC assets...")

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
        print("No OTC assets returned.")
        return []

    assets = assets[:MAX_ASSETS]

    print("OTC assets found:", len(assets))

    return assets


# ============================================================
# CANDLE DATA
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
            candle_time = int(candle["from"])
            close = float(candle["close"])

            if close <= 0:
                continue

            valid.append({
                "time": candle_time,
                "open": float(candle["open"]),
                "high": float(candle["max"]),
                "low": float(candle["min"]),
                "close": close
            })

        except Exception:
            continue

    valid.sort(key=lambda item: item["time"])

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

def calculate_momentum(closes):
    values = []

    if len(closes) <= MOMENTUM_PERIOD:
        return values

    for index in range(
        MOMENTUM_PERIOD,
        len(closes)
    ):
        old_close = closes[
            index - MOMENTUM_PERIOD
        ]

        if old_close <= 0:
            continue

        value = (
            closes[index] / old_close
        ) * 100.0

        if value == value:
            if value != float("inf"):
                values.append(value)

    return values


# ============================================================
# PERCENTILE
# ============================================================

def percentile(values, percent):
    if not values:
        return 0.0

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    position = (
        len(ordered) - 1
    ) * percent

    lower = int(position)
    upper = lower + 1

    if upper >= len(ordered):
        return ordered[-1]

    fraction = position - lower

    return (
        ordered[lower]
        + (
            ordered[upper]
            - ordered[lower]
        ) * fraction
    )


# ============================================================
# MOMENTUM ANALYSIS
# ============================================================

def analyze_momentum(candles):
    closes = []

    for candle in candles:
        closes.append(candle["close"])

    momentum = calculate_momentum(closes)

    required = (
        MOMENTUM_LOOKBACK
        + 3
    )

    if len(momentum) < required:
        return None

    recent = momentum[
        -MOMENTUM_LOOKBACK:
    ]

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

    rising_now = current > previous
    falling_now = current < previous

    previous_falling = (
        previous < previous_previous
    )

    previous_rising = (
        previous > previous_previous
    )

    upward_turn = (
        rising_now
        and previous_falling
    )

    downward_turn = (
        falling_now
        and previous_rising
    )

    turn_distance = abs(
        current - previous
    )

    strong_turn = (
        turn_distance >= MIN_TURN_DISTANCE
    )

    low_extreme = (
        current <= low_threshold
    )

    high_extreme = (
        current >= high_threshold
    )

    direction = None
    extreme = None

    if low_extreme and upward_turn:

        if REQUIRE_TURN:
            if not strong_turn:
                return None

        direction = "CALL"
        extreme = "LOW"

    elif high_extreme and downward_turn:

        if REQUIRE_TURN:
            if not strong_turn:
                return None

        direction = "PUT"
        extreme = "HIGH"

    else:
        return None

    threshold_range = (
        high_threshold
        - low_threshold
    )

    if threshold_range <= 0:
        strength = 0.0

    elif extreme == "LOW":
        strength = (
            (
                low_threshold
                - current
            )
            / threshold_range
        ) * 100.0

    else:
        strength = (
            (
                current
                - high_threshold
            )
            / threshold_range
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

def make_signal_id(
    asset,
    direction,
    candle_time
):
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
# SIGNAL DUPLICATE CHECK
# ============================================================

def is_duplicate(
    asset,
    candle_time
):
    previous = last_signal_candle.get(asset)

    if previous is None:
        return False

    return previous == candle_time


def mark_signal(
    asset,
    candle_time
):
    last_signal_candle[asset] = candle_time


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    analysis,
    signal_id
):
    message = (
        "🔔 MOMENTUM 10 SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: "
        + asset
        + "\n"
        "Direction: "
        + analysis["direction"]
        + "\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "Strategy: Momentum 10 Extreme-Reversal\n"
        "Extreme: "
        + analysis["extreme"]
        + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Momentum: "
        + str(round(
            analysis["momentum"],
            5
        ))
        + "\n"
        "Previous: "
        + str(round(
            analysis["previous"],
            5
        ))
        + "\n"
        "Previous 2: "
        + str(round(
            analysis["previous_previous"],
            5
        ))
        + "\n"
        "Recent Low: "
        + str(round(
            analysis["recent_low"],
            5
        ))
        + "\n"
        "Recent High: "
        + str(round(
            analysis["recent_high"],
            5
        ))
        + "\n"
        "Low Threshold: "
        + str(round(
            analysis["low_threshold"],
            5
        ))
        + "\n"
        "High Threshold: "
        + str(round(
            analysis["high_threshold"],
            5
        ))
        + "\n"
        "Reversal Strength: "
        + str(round(
            analysis["strength"],
            1
        ))
        + "%\n"
        "Price: "
        + str(analysis["price"])
        + "\n"
        "Signal ID: "
        + signal_id
    )

    return message


# ============================================================
# TRADE
# ============================================================

def execute_trade(
    api,
    asset,
    direction
):
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
            "Trade error:",
            asset,
            direction,
            exc
        )

        return None

    if not success:
        print(
            "Trade rejected:",
            asset,
            direction
        )

        send_telegram(
            "❌ TRADE REJECTED\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
        )

        return None

    total_trades += 1

    print(
        "TRADE ACCEPTED",
        asset,
        direction,
        "Trade ID:",
        trade_id,
        "Trade:",
        total_trades,
        "/",
        TARGET_TRADES
    )

    return str(trade_id)


# ============================================================
# RESULT
# ============================================================

def check_result(
    api,
    trade_id
):
    try:
        result = api.check_win_v4(
            trade_id
        )

    except Exception as exc:
        print(
            "Result check error:",
            exc
        )

        return None

    if result is None:
        return None

    if result > 0:
        return "WIN"

    if result < 0:
        return "LOSS"

    return "DRAW"


def wait_for_result(
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

    print(
        "Waiting",
        wait_seconds,
        "seconds for result:",
        trade_id
    )

    time.sleep(wait_seconds)

    result = check_result(
        api,
        trade_id
    )

    if result is None:
        print(
            "Result unavailable:",
            trade_id
        )
        return

    if result == "WIN":
        wins += 1

    elif result == "LOSS":
        losses += 1

    closed = wins + losses

    if closed > 0:
        win_rate = (
            wins / closed
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
        "RATE:",
        round(win_rate, 1),
        "%"
    )

    log_signal(
        asset,
        direction,
        analysis["candle_time"],
        analysis,
        signal_id,
        trade_id,
        result
    )

    message = (
        "📊 TRADE RESULT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: "
        + asset
        + "\n"
        "Direction: "
        + direction
        + "\n"
        "Result: "
        + result
        + "\n"
        "Trade ID: "
        + trade_id
        + "\n"
        "Signal ID: "
        + signal_id
        + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Wins: "
        + str(wins)
        + "\n"
        "Losses: "
        + str(losses)
        + "\n"
        "Win Rate: "
        + str(round(win_rate, 1))
        + "%"
    )

    send_telegram(message)


# ============================================================
# PROCESS ASSET
# ============================================================

def process_asset(
    api,
    asset
):
    candles = get_candles(
        api,
        asset,
        MOMENTUM_LOOKBACK
        + MOMENTUM_PERIOD
        + 10
    )

    if not candles:
        return

    candles = remove_open_candle(
        candles
    )

    minimum = (
        MOMENTUM_LOOKBACK
        + MOMENTUM_PERIOD
        + 5
    )

    if len(candles) < minimum:
        return

    analysis = analyze_momentum(
        candles
    )

    if analysis is None:
        return

    candle_time = analysis[
        "candle_time"
    ]

    if is_duplicate(
        asset,
        candle_time
    ):
        return

    current_extreme = analysis[
        "extreme"
    ]

    previous_state = extreme_state.get(
        asset,
        "CENTER"
    )

    # --------------------------------------------------------
    # One signal per extreme episode.
    # A new signal requires the previous state to be different.
    # --------------------------------------------------------

    if previous_state == current_extreme:
        return

    extreme_state[asset] = current_extreme

    signal_id = make_signal_id(
        asset,
        analysis["direction"],
        candle_time
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

    print("")
    print(message)
    print("")

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
        analysis,
        signal_id,
        trade_id or "",
        ""
    )

    if trade_id:
        trade_message = (
            "🟢 PRACTICE TRADE ACCEPTED\n"
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
            "Momentum: "
            + str(round(
                analysis["momentum"],
                5
            ))
            + "\n"
            "Extreme: "
            + analysis["extreme"]
            + "\n"
            "Trade ID: "
            + trade_id
            + "\n"
            "Signal ID: "
            + signal_id
        )

        send_telegram(
            trade_message
        )

        wait_for_result(
            api,
            asset,
            analysis["direction"],
            trade_id,
            signal_id,
            analysis
        )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat(
    api,
    assets
):
    global last_heartbeat

    now = time.time()

    if (
        now - last_heartbeat
        < HEARTBEAT_SECONDS
    ):
        return

    last_heartbeat = now

    closed = wins + losses

    if closed > 0:
        rate = (
            wins / closed
        ) * 100.0
    else:
        rate = 0.0

    message = (
        "💚 MOMENTUM 10 BOT ALIVE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Connection: OK\n"
        "Balance: PRACTICE\n"
        "Auto Trading: ON\n"
        "Strategy: Momentum 10\n"
        "Timeframe: 1M\n"
        "Expiry: 1 minute\n"
        "OTC Assets: "
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

    print(message)
    send_telegram(message)


# ============================================================
# MAIN CONTINUOUS LOOP
# ============================================================

def main():
    global last_asset_refresh

    create_log_file()

    send_telegram(
        "🚀 MOMENTUM 10 BOT STARTING\n"
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
        + "\n"
        "Continuous scanning: ON"
    )

    api = None
    assets = []

    while True:

        # ----------------------------------------------------
        # CONNECTION
        # ----------------------------------------------------

        if api is None:

            api = connect_iq()

            if api is None:
                send_telegram(
                    "⚠️ IQ OPTION CONNECTION FAILED\n"
                    "Retrying automatically..."
                )

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

            send_telegram(
                "🟢 IQ OPTION CONNECTED\n"
                "Practice mode active.\n"
                "Continuous scanning started."
            )

            assets = []
            last_asset_refresh = 0

        # ----------------------------------------------------
        # ASSET REFRESH
        # ----------------------------------------------------

        now = time.time()

        if (
            not assets
            or now - last_asset_refresh
            >= ASSET_REFRESH_SECONDS
        ):

            try:
                new_assets = get_otc_assets(
                    api
                )

                if new_assets:
                    assets = new_assets

                    send_telegram(
                        "🔎 OTC ASSETS READY\n"
                        "Found "
                        + str(len(assets))
                        + " OTC assets.\n"
                        "Scanning continuously."
                    )

                else:
                    print(
                        "No OTC assets yet."
                    )

            except Exception as exc:
                print(
                    "Asset refresh error:",
                    exc
                )

            last_asset_refresh = now

        # ----------------------------------------------------
        # NO ASSETS
        # ----------------------------------------------------

        if not assets:
            print(
                "Waiting for OTC assets..."
            )

            time.sleep(
                RECONNECT_SECONDS
            )

            continue

        # ----------------------------------------------------
        # HEARTBEAT
        # ----------------------------------------------------

        heartbeat(
            api,
            assets
        )

        # ----------------------------------------------------
        # TARGET CHECK
        # ----------------------------------------------------

        if total_trades >= TARGET_TRADES:

            send_telegram(
                "🏁 TARGET REACHED\n"
                "Trades: "
                + str(total_trades)
                + "\n"
                "Wins: "
                + str(wins)
                + "\n"
                "Losses: "
                + str(losses)
            )

            print(
                "Target reached."
            )

            # Keep the process alive rather than exiting.
            # This prevents the workflow from looking like
            # a scanner crash or silent stop.
            while True:
                send_telegram(
                    "⏸️ TARGET REACHED\n"
                    "Scanner is staying online.\n"
                    "No new trades will be opened."
                )

                time.sleep(
                    HEARTBEAT_SECONDS
                )

        # ----------------------------------------------------
        # SCAN ALL OTC ASSETS
        # ----------------------------------------------------

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
                    "Asset error:",
                    asset,
                    exc
                )

            time.sleep(
                SCAN_INTERVAL
            )

        # ----------------------------------------------------
        # CONNECTION TEST
        # ----------------------------------------------------

        try:
            connected = api.check_connect()

            if not connected:
                print(
                    "IQ Option disconnected."
                )

                send_telegram(
                    "⚠️ IQ OPTION DISCONNECTED\n"
                    "Attempting automatic reconnect..."
                )

                api = None

                time.sleep(
                    RECONNECT_SECONDS
                )

        except Exception as exc:
            print(
                "Connection check error:",
                exc
            )

            api = None

            time.sleep(
                RECONNECT_SECONDS
            )


if __name__ == "__main__":
    main()
