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

    url = "https://api.telegram.org/bot" + token + "/sendMessage"

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

    return "".join(char for char in text if char.isalnum())


def wanted_asset(name):
    key = asset_key(name)

    for wanted in WATCHLIST:
        if key == asset_key(wanted):
            return wanted

    return None


def clean_asset_name(name):
    text = str(name or "").strip()

    if "." in text:
        text = text.split(".")[-1]

    if text.lower().startswith("front."):
        text = text[6:]
    elif text.lower().startswith("front_"):
        text = text[6:]

    return text.strip()


def get_controlled_assets():
    """Read binary/turbo data directly without requesting digital data."""
    found = {}

    print("")
    print("=" * 55)
    print("DISCOVERING REQUESTED CURRENCY PAIRS")
    print("=" * 55)

    data = None

    for attempt in range(1, 4):
        try:
            print("Requesting asset data. Attempt", attempt, "of 3")
            data = api.get_all_init_v2()

            if isinstance(data, dict):
                break

        except Exception as exc:
            print("Asset data error:", repr(exc))

        data = None
        time.sleep(5)

    if not isinstance(data, dict):
        print("ERROR: Binary/turbo initialization data unavailable.")
        print("The scanner will retry later.")
        return found

    # Support the direct structure and a wrapped result structure.
    if isinstance(data.get("result"), dict):
        if "binary" not in data and "turbo" not in data:
            data = data["result"]

    if not isinstance(data, dict):
        print("ERROR: Unexpected initialization data.")
        return found

    print("")
    print("=" * 55)
    print("CHECKING BINARY/TURBO AVAILABILITY")
    print("=" * 55)

    for option_type in ("turbo", "binary"):
        section = data.get(option_type, {})

        if not isinstance(section, dict):
            print("No valid", option_type.upper(), "data returned.")
            continue

        actives = section.get("actives", {})

        if not isinstance(actives, dict):
            print("No active list for", option_type.upper())
            continue

        print("")
        print("Checking", option_type.upper())

        for raw_id, info in actives.items():
            if not isinstance(info, dict):
                continue

            raw_name = info.get("name", "")
            name = clean_asset_name(raw_name)
            wanted = wanted_asset(name)

            if wanted is None:
                continue

            enabled = info.get("enabled") is True
            suspended = info.get("is_suspended") is True
            is_open = enabled and not suspended

            print(
                wanted,
                "OPEN" if is_open else "CLOSED",
                "| Type:",
                option_type
            )

            if not is_open:
                continue

            try:
                active_id = int(raw_id)
            except (TypeError, ValueError):
                print("Skipping", wanted, "- invalid active ID:", raw_id)
                continue

            key = asset_key(wanted)

            # Keep the first confirmed-open option type for this asset.
            if key not in found:
                found[key] = {
                    "name": name,
                    "id": active_id,
                    "option_type": option_type,
                    "open": True
                }

    print("")
    print("=" * 55)
    print("ASSET DISCOVERY REPORT")
    print("=" * 55)

    for wanted in WATCHLIST:
        key = asset_key(wanted)

        if key in found:
            item = found[key]
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
    print("Total available assets:", len(found))

    return found


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

    low_level = percentile(lookback, EXTREME_PERCENTILE)
    high_level = percentile(lookback, 1.0 - EXTREME_PERCENTILE)

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
        "ext
