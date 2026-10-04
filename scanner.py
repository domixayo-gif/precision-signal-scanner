import os
import csv
import time
import threading
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

SCAN_INTERVAL = 2
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 15

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

pending_trades = {}
pending_lock = threading.Lock()

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
            timeout=15
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

        result = api.connect()

    except Exception as exc:
        print("IQ connection exception:", exc)
        return None

    connected = False

    if isinstance(result, tuple):
        if len(result) > 0:
            connected = bool(result[0])
    else:
        connected = bool(result)

    if not connected:
        print("IQ Option connection failed.")
        return None

    print("IQ Option connection successful.")

    try:
        api.change_balance(BALANCE_MODE)
        print("Balance mode:", BALANCE_MODE)

    except Exception as exc:
        print("Balance mode warning:", exc)

    time.sleep(2)

    try:
        if not api.check_connect():
            print("Connection check failed after login.")
            return None
    except Exception as exc:
        print("Connection verification error:", exc)
        return None

    return api


# ============================================================
# OTC ASSET DISCOVERY
# ============================================================

def get_otc_assets(api):
    print("Refreshing IQ Option OTC assets...")

    try:
        all_assets = api.get_all_open_time()

    except Exception as exc:
        print(
            "get_all_open_time error:",
            exc
        )
        return []

    if not isinstance(all_assets, dict):
        print("IQ Option returned invalid asset data.")
        return []

    assets = []

    # 1-minute expiry uses TURBO/BINARY.
    # We prioritize TURBO because 1-minute orders belong here.
    turbo = all_assets.get("turbo", {})

    if isinstance(turbo, dict):

        for asset, info in turbo.items():

            if not isinstance(asset, str):
                continue

            if "-OTC" not in asset.upper():
                continue

            if not isinstance(info, dict):
                continue

            if info.get("open") is True:
                if asset not in assets:
                    assets.append(asset)

    # Some installations may expose OTC assets in binary too.
    if not assets:

        binary = all_assets.get("binary", {})

        if isinstance(binary, dict):

            for asset, info in binary.items():

                if not isinstance(asset, str):
                    continue

                if "-OTC" not in asset.upper():
                    continue

                if not isinstance(info, dict):
                    continue

                if info.get("open") is True:
                    if asset not in assets:
                        assets.append(asset)

    assets = sorted(assets)

    if not assets:
        print("No OPEN OTC turbo/binary assets found.")
        return []

    if len(assets) > MAX_ASSETS:
        assets = assets[:MAX_ASSETS]

    print(
        "OPEN OTC assets found:",
        len(assets)
    )

    for asset in assets[:20]:
        print("OTC:", asset)

    if len(assets) > 20:
        print(
            "...and",
            len(assets) - 20,
            "more."
        )

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
        print(
            asset,
            "candle error:",
            exc
        )
        return []

    if not candles:
        return []

    valid = []

    for candle in candles:

        try:
            candle_time = int(
                candle["from"]
            )

            open_price = float(
                candle["open"]
            )

            high_price = float(
                candle["max"]
            )

            low_price = float(
                candle["min"]
            )

            close_price = float(
                candle["close"]
            )

            if close_price <= 0:
                continue

            valid.append({
                "time": candle_time,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price
            })

        except Exception:
            continue

    valid.sort(
        key=lambda item: item["time"]
    )

    return valid


def remove_open_candle(candles):
    if len(candles) < 2:
        return candles

    now = int(time.time())

    latest = candles[-1]

    candle_end = (
        latest["time"]
        + CANDLE_SECONDS
    )

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
            closes[index]
            / old_close
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
        closes.append(
            candle["close"]
        )

    momentum = calculate_momentum(
        closes
    )

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

    rising_now = (
        current > previous
    )

    falling_now = (
        current < previous
    )

    previous_falling = (
        previous
        < previous_previous
    )

    previous_rising = (
        previous
        > previous_previous
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
        turn_distance
        >= MIN_TURN_DISTANCE
    )

    low_extreme = (
        current <= low_threshold
    )

    high_extreme = (
        current >= high_threshold
    )

    direction = None
    extreme = None

    if (
        low_extreme
        and upward_turn
    ):

        if REQUIRE_TURN:
            if not strong_turn:
                return None

        direction = "CALL"
        extreme = "LOW"

    elif (
        high_extreme
        and downward_turn
    ):

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
    clean_asset = asset.replace(
        "/",
        ""
    )

    clean_asset = clean_asset.replace(
        "-",
        ""
    )

    return (
        "M10-"
        + clean_asset
        + "-"
        + direction
        + "-"
        + str(candle_time)
    )


# ============================================================
# DUPLICATE CHECK
# ============================================================

def is_duplicate(
    asset,
    candle_time
):
    previous = last_signal_candle.get(
        asset
    )

    if previous is None:
        return False

    return previous == candle_time


def mark_signal(
    asset,
    candle_time
):
    last_signal_candle[
        asset
    ] = candle_time


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset,
    analysis,
    signal_id
):
    return (
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


# ============================================================
# RESULT MONITOR
# ============================================================

def monitor_trade(
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
        "Trade monitor started:",
        trade_id,
        asset
    )

    time.sleep(wait_seconds)

    result = None

    try:
        result = api.check_win_v4(
            trade_id
        )

    except Exception as exc:
        print(
            "Result check error:",
            trade_id,
            exc
        )

    if result is None:
        print(
            "Result unavailable:",
            trade_id
        )

        with pending_lock:
            pending_trades.pop(
                str(trade_id),
                None
            )

        send_telegram(
            "⚠️ TRADE RESULT UNAVAILABLE\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
            + "\n"
            "Trade ID: "
            + str(trade_id)
        )

        return

    if result > 0:
        result_text = "WIN"
        wins += 1

    elif result < 0:
        result_text = "LOSS"
        losses += 1

    else:
        result_text = "DRAW"

    closed = (
        wins
        + losses
    )

    if closed > 0:
        win_rate = (
            wins
            / closed
        ) * 100.0
    else:
        win_rate = 0.0

    print(
        "RESULT:",
        asset,
        direction,
        result_text,
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
        str(trade_id),
        result_text
    )

    send_telegram(
        "📊 TRADE RESULT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Asset: "
        + asset
        + "\n"
        "Direction: "
        + direction
        + "\n"
        "Result: "
        + result_text
        + "\n"
        "Trade ID: "
        + str(trade_id)
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
        + str(round(
            win_rate,
            1
        ))
        + "%"
    )

    with pending_lock:
        pending_trades.pop(
            str(trade_id),
            None
        )


# ============================================================
# TRADE EXECUTION
# ============================================================

def execute_trade(
    api,
    asset,
    direction,
    signal_id,
    analysis
):
    global total_trades

    if not AUTO_TRADE:
        print("Auto trading disabled.")
        return None

    if total_trades >= TARGET_TRADES:
        return None

    action = direction.lower()

    print(
        "Attempting trade:",
        asset,
        direction
    )

    try:
        success, trade_id = api.buy(
            STAKE,
            asset,
            action,
            EXPIRY_MINUTES
        )

    except Exception as exc:
        print(
            "Trade exception:",
            asset,
            direction,
            exc
        )

        send_telegram(
            "❌ TRADE EXCEPTION\n"
            "Asset: "
            + asset
            + "\n"
            "Direction: "
            + direction
            + "\n"
            "Error: "
            + str(exc)[:300]
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
            + "\n"
            "Stake: $"
            + str(STAKE)
        )

        return None

    total_trades += 1

    trade_id = str(trade_id)

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

    with pending_lock:
        pending_trades[trade_id] = {
            "asset": asset,
            "direction": direction
        }

    send_telegram(
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
        + direction
        + "\n"
        "Stake: $"
        + str(STAKE)
        + "\n"
        "Expiry: 1 minute\n"
        "Trade ID: "
        + trade_id
        + "\n"
        "Signal ID: "
        + signal_id
    )

    worker = threading.Thread(
        target=monitor_trade,
        args=(
            api,
            asset,
            direction,
            trade_id,
            signal_id,
            analysis
        ),
        daemon=True
    )

    worker.start()

    return trade_id


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
        + 15
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

    # Reset the extreme episode when there
    # is no valid extreme reversal.
    if analysis is None:
        extreme_state[asset] = "CENTER"
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

    if previous_state == current_extreme:
        return

    extreme_state[
        asset
    ] = current_extreme

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
        analysis["direction"],
        signal_id,
        analysis
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

    closed = (
        wins
        + losses
    )

    if closed > 0:
        rate = (
            wins
            / closed
        ) * 100.0
    else:
        rate = 0.0

    with pending_lock:
        pending_count = len(
            pending_trades
        )

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
        "Pending Results: "
        + str(pending_count)
        + "\n"
        "Wins: "
        + str(wins)
        + "\n"
        "Losses: "
        + str(losses)
        + "\n"
        "Win Rate: "
        + str(round(
            rate,
            1
        ))
        + "%"
    )

    print(message)
    send_telegram(message)


# ============================================================
# MAIN
# ============================================================

def main():
    global last_asset_refresh
    global last_heartbeat

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
                "Preparing OTC assets..."
            )

            assets = []
            last_asset_refresh = 0
            last_heartbeat = 0

        # ----------------------------------------------------
        # CONNECTION CHECK
        # ----------------------------------------------------

        try:
            if not api.check_connect():

                print(
                    "IQ Option disconnected."
                )

                send_telegram(
                    "⚠️ IQ OPTION DISCONNECTED\n"
                    "Reconnecting..."
                )

                api = None

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

        except Exception as exc:

            print(
                "Connection check error:",
                exc
            )

            api = None

            time.sleep(
                RECONNECT_SECONDS
            )

            continue

        # ----------------------------------------------------
        # ASSET REFRESH
        # ----------------------------------------------------

        now = time.time()

        if (
            not assets
            or (
                now
                - last_asset_refresh
                >= ASSET_REFRESH_SECONDS
            )
        ):

            new_assets = get_otc_assets(
                api
            )

            if new_assets:

                assets = new_assets

                send_telegram(
                    "🔎 OTC ASSETS READY\n"
                    "Found "
                    + str(len(assets))
                    + " OPEN OTC assets.\n"
                    "1M scanner is now active."
                )

            else:

                print(
                    "No open OTC assets."
                )

                if assets:
                    print(
                        "Keeping previous asset list."
                    )
                else:
                    send_telegram(
                        "⚠️ NO OTC ASSETS FOUND\n"
                        "Retrying OTC discovery..."
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

            while True:

                heartbeat(
                    api,
                    assets
                )

                time.sleep(
                    HEARTBEAT_SECONDS
                )

        # ----------------------------------------------------
        # SCAN ALL OTC ASSETS
        # ----------------------------------------------------

        for asset in list(assets):

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
        # SHORT LOOP DELAY
        # ----------------------------------------------------

        time.sleep(1)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()ain()
