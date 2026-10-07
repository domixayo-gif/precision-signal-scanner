import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


WATCHLIST = [
    "EURUSD-OTC",
    "EURUSD"
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
    token = os.getenv(
        "TELEGRAM_TOKEN",
        ""
    )

    chat_id = os.getenv(
        "TELEGRAM_CHAT_ID",
        ""
    )

    if not token or not chat_id:
        return

    url = (
        "https://api.telegram.org/bot"
        + token
        + "/sendMessage"
    )

    parts = []

    while message:
        parts.append(
            message[:3900]
        )

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

            print(
                "Telegram error:",
                exc
            )


def clean_asset_name(name):
    if not name:
        return ""

    text = str(name).strip()

    if text.lower().startswith(
        "front."
    ):
        text = text[6:]

    if text.lower().startswith(
        "front_"
    ):
        text = text[6:]

    text = text.replace(
        "_",
        "-"
    )

    text = text.replace(
        " ",
        ""
    )

    return text.upper()


def asset_key(name):
    text = clean_asset_name(
        name
    )

    chars = []

    for char in text:

        if char.isalnum():
            chars.append(char)

    return "".join(chars)


def is_allowed_asset(name):
    key = asset_key(name)

    for wanted in WATCHLIST:

        if key == asset_key(
            wanted
        ):
            return True

    return False


def add_asset(
    found,
    name,
    active_id,
    option_type="unknown",
    open_state=False
):
    if name is None:
        return

    if active_id is None:
        return

    if not is_allowed_asset(name):
        return

    key = asset_key(name)

    if not key:
        return

    found[key] = {
        "name": str(name),
        "id": active_id,
        "option_type": option_type,
        "open": bool(open_state)
    }


def extract_asset_name(
    info,
    fallback
):
    if not isinstance(
        info,
        dict
    ):
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

        value = info.get(
            field
        )

        if value:
            return str(value)

    return fallback


def extract_active_id(info):
    if not isinstance(
        info,
        dict
    ):
        return None

    fields = [
        "id",
        "active_id",
        "activeId",
        "instrument_id",
        "instrumentId"
    ]

    for field in fields:

        value = info.get(
            field
        )

        if value is not None:

            try:

                return int(value)

            except Exception:

                return value

    return None


def get_open_state_value(info):
    if not isinstance(
        info,
        dict
    ):
        return None

    if "open" in info:
        return bool(
            info["open"]
        )

    if "enabled" in info:

        enabled = bool(
            info["enabled"]
        )

        suspended = bool(
            info.get(
                "is_suspended",
                False
            )
        )

        return (
            enabled
            and not suspended
        )

    return None


def scan_active_container(
    container,
    found,
    option_type
):
    if not isinstance(
        container,
        dict
    ):
        return

    for raw_key, info in (
        container.items()
    ):

        fallback_name = None
        active_id = None

        if isinstance(
            raw_key,
            int
        ):

            active_id = raw_key

        elif isinstance(
            raw_key,
            str
        ):

            if raw_key.isdigit():

                active_id = int(
                    raw_key
                )

            else:

                fallback_name = raw_key

        if isinstance(
            info,
            dict
        ):

            detected_id = (
                extract_active_id(
                    info
                )
            )

            if detected_id is not None:
                active_id = detected_id

        name = extract_asset_name(
            info,
            fallback_name
        )

        if not name:
            continue

        if active_id is None:
            continue

        open_state = (
            get_open_state_value(
                info
            )
        )

        if open_state is None:
            open_state = False

        add_asset(
            found,
            name,
            active_id,
            option_type,
            open_state
        )


def scan_open_time_section(
    section,
    found,
    option_type
):
    if not isinstance(
        section,
        dict
    ):
        return

    actives = section.get(
        "actives"
    )

    if isinstance(
        actives,
        dict
    ):

        scan_active_container(
            actives,
            found,
            option_type
        )

    elif isinstance(
        actives,
        list
    ):

        for item in actives:

            if not isinstance(
                item,
                dict
            ):
                continue

            name = extract_asset_name(
                item,
                None
            )

            active_id = (
                extract_active_id(
                    item
                )
            )

            if name and active_id is not None:

                open_state = (
                    get_open_state_value(
                        item
                    )
                )

                if open_state is None:
                    open_state = False

                add_asset(
                    found,
                    name,
                    active_id,
                    option_type,
                    open_state
                )


def robust_asset_scan(
    obj,
    found,
    depth=0
):
    if depth > 15:
        return

    if isinstance(
        obj,
        dict
    ):

        for key, value in obj.items():

            key_text = str(
                key
            ).lower()

            option_type = (
                key_text
                if key_text in [
                    "binary",
                    "turbo"
                ]
                else "unknown"
            )

            if option_type != "unknown":

                if isinstance(
                    value,
                    dict
                ):

                    scan_active_section(
                        value,
                        found,
                        option_type
                    )

            if isinstance(
                value,
                dict
            ):

                robust_asset_scan(
                    value,
                    found,
                    depth + 1
                )

            elif isinstance(
                value,
                list
            ):

                robust_asset_scan(
                    value,
                    found,
                    depth + 1
                )

    elif isinstance(
        obj,
        list
    ):

        for item in obj:

            robust_asset_scan(
                item,
                found,
                depth + 1
            )


def get_asset_match(text):
    if not text:
        return None

    value = str(
        text
    ).strip()

    if value.lower().startswith(
        "front."
    ):
        value = value[6:]

    if value.lower().startswith(
        "front_"
    ):
        value = value[6:]

    value = value.upper()

    value = value.replace(
        "_",
        ""
    )

    value = value.replace(
        "-",
        ""
    )

    value = value.replace(
        "/",
        ""
    )

    value = value.replace(
        " ",
        ""
    )

    if value == "EURUSDOTC":
        return "EURUSDOTC"

    if value == "EURUSD":
        return "EURUSD"

    return None


def inspect_asset_object(
    raw_key,
    info,
    found,
    diagnostics
):
    key_text = ""

    if raw_key is not None:
        key_text = str(
            raw_key
        )

    name = ""

    if isinstance(
        info,
        dict
    ):

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

            value = info.get(
                field
            )

            if value:

                name = str(
                    value
                )

                break

    possible_names = []

    if key_text:
        possible_names.append(
            key_text
        )

    if name:
        possible_names.append(
            name
        )

    matched_key = None
    matched_name = None

    for possible in possible_names:

        match = get_asset_match(
            possible
        )

        if match:

            matched_key = match
            matched_name = possible

            break

    if matched_key is None:
        return

    active_id = (
        extract_active_id(
            info
        )
    )

    if active_id is None:

        if key_text.isdigit():

            try:

                active_id = int(
                    key_text
                )

            except Exception:
                pass

    open_state = (
        get_open_state_value(
            info
        )
    )

    diagnostics.append(
        (
            key_text,
            name,
            active_id,
            matched_key,
            open_state
        )
    )

    if active_id is None:
        return

    found[matched_key] = {
        "name": matched_name,
        "id": active_id,
        "option_type": "raw",
        "open": bool(
            open_state
        )
    }


def find_eurusd_candidates(
    obj,
    results,
    depth=0
):
    if depth > 12:
        return

    if isinstance(
        obj,
        dict
    ):

        for key, value in obj.items():

            key_text = str(
                key
            )

            if (
                "eur" in key_text.lower()
                or "usd" in key_text.lower()
            ):

                results.append(
                    key_text
                )

            if isinstance(
                value,
                dict
            ):

                name = extract_asset_name(
                    value,
                    ""
                )

                if (
                    "eur" in name.lower()
                    or "usd" in name.lower()
                ):

                    results.append(
                        name
                    )

            find_eurusd_candidates(
                value,
                results,
                depth + 1
            )

    elif isinstance(
        obj,
        list
    ):

        for item in obj:

            find_eurusd_candidates(
                item,
                results,
                depth + 1
            )


def get_explicit_open_time_assets():
    found = {}

    print("")
    print(
        "=" * 60
    )
    print(
        "CHECKING IQ OPTION OPEN-TIME TABLES"
    )
    print(
        "=" * 60
    )

    try:

        open_data = (
            api.get_all_open_time()
        )

    except Exception as exc:

        print(
            "Open-time discovery error:",
            repr(exc)
        )

        return found

    if not isinstance(
        open_data,
        dict
    ):

        print(
            "IQ Option returned no open-time data."
        )

        return found

    sections = [
        (
            "turbo",
            "TURBO"
        ),
        (
            "binary",
            "BINARY"
        )
    ]

    for section_key, label in sections:

        section = open_data.get(
            section_key
        )

        print("")
        print(
            label,
            "SECTION"
        )

        if not isinstance(
            section,
            dict
        ):

            print(
                "Section unavailable."
            )

            continue

        for wanted in WATCHLIST:

            wanted_key = asset_key(
                wanted
            )

            matched = None

            for raw_name, info in (
                section.items()
            ):

                current_key = asset_key(
                    raw_name
                )

                if current_key == wanted_key:

                    matched = (
                        raw_name,
                        info
                    )

                    break

            if matched is None:

                print(
                    wanted,
                    ": NOT FOUND"
                )

                continue

            raw_name, info = matched

            active_id = (
                extract_active_id(
                    info
                )
            )

            open_state = (
                get_open_state_value(
                    info
                )
            )

            if open_state is None:
                open_state = False

            print(
                wanted,
                ":",
                "OPEN"
                if open_state
                else "CLOSED",
                "| Name:",
                raw_name,
                "| ID:",
                active_id
            )

            if (
                active_id is not None
                and open_state
            ):

                add_asset(
                    found,
                    raw_name,
                    active_id,
                    section_key,
                    True
                )

    print("")
    print(
        "NORMAL EUR/USD BINARY/TURBO CHECK"
    )

    normal_key = asset_key(
        "EURUSD"
    )

    if normal_key in found:

        item = found[
            normal_key
        ]

        print(
            "NORMAL EURUSD IS AVAILABLE."
        )

        print(
            "Name:",
            item["name"]
        )

        print(
            "Active ID:",
            item["id"]
        )

        print(
            "Option type:",
            item["option_type"]
        )

    else:

        print(
            "NORMAL EURUSD IS NOT CURRENTLY "
            "AVAILABLE FOR BINARY/TURBO."
        )

    return found


def check_forex_availability():
    print("")
    print(
        "=" * 60
    )
    print(
        "CHECKING NORMAL FOREX EUR/USD"
    )
    print(
        "=" * 60
    )

    try:

        open_data = (
            api.get_all_open_time()
        )

    except Exception as exc:

        print(
            "Forex availability error:",
            repr(exc)
        )

        return None

    if not isinstance(
        open_data,
        dict
    ):
        return None

    forex = open_data.get(
        "forex"
    )

    if not isinstance(
        forex,
        dict
    ):

        print(
            "Forex section unavailable."
        )

        return None

    info = forex.get(
        "EURUSD"
    )

    if info is None:

        print(
            "EURUSD was not found in Forex section."
        )

        return False

    open_state = (
        get_open_state_value(
            info
        )
    )

    print(
        "Forex EURUSD:",
        "OPEN"
        if open_state
        else "CLOSED"
    )

    if isinstance(
        info,
        dict
    ):

        print(
            "Forex details:",
            {
                "name": info.get(
                    "name"
                ),
                "open": info.get(
                    "open"
                ),
                "enabled": info.get(
                    "enabled"
                ),
                "is_suspended": info.get(
                    "is_suspended"
                )
            }
        )

    return bool(
        open_state
    )


def get_controlled_assets():
    found = {}

    print("")
    print(
        "=" * 60
    )
    print(
        "SEARCHING FOR EUR/USD AND EUR/USD OTC"
    )
    print(
        "=" * 60
    )

    explicit_assets = (
        get_explicit_open_time_assets()
    )

    for key, item in (
        explicit_assets.items()
    ):

        found[key] = item

    forex_open = (
        check_forex_availability()
    )

    data_sources = []

    try:

        data = api.get_all_init_v2()

        if data:

            data_sources.append(
                (
                    "V2",
                    data
                )
            )

            print(
                "V2 initialization data received."
            )

    except Exception as exc:

        print(
            "V2 initialization error:",
            repr(exc)
        )

    try:

        data = api.get_all_init()

        if data:

            data_sources.append(
                (
                    "LEGACY",
                    data
                )
            )

            print(
                "Legacy initialization data received."
            )

    except Exception as exc:

        print(
            "Legacy initialization error:",
            repr(exc)
        )

    diagnostics = []

    for source_name, data in (
        data_sources
    ):

        before = len(
            found
        )

        robust_asset_scan(
            data,
            found
        )

        after = len(
            found
        )

        print(
            source_name,
            "matches added:",
            after - before
        )

        extra = []

        find_eurusd_candidates(
            data,
            extra
        )

        diagnostics.extend(
            extra
        )

    print("")
    print(
        "=" * 60
    )
    print(
        "FINAL CONTROLLED ASSET LIST"
    )
    print(
        "=" * 60
    )

    if found:

        for key, item in (
            found.items()
        ):

            print(
                "Asset:",
                item["name"],
                "| ID:",
                item["id"],
                "| Type:",
                item.get(
                    "option_type",
                    "unknown"
                ),
                "| Open:",
                item.get(
                    "open",
                    False
                )
            )

    else:

        print(
            "No EUR/USD binary/turbo assets "
            "are currently available."
        )

    normal_key = asset_key(
        "EURUSD"
    )

    otc_key = asset_key(
        "EURUSD-OTC"
    )

    print("")
    print(
        "=" * 60
    )
    print(
        "EUR/USD AVAILABILITY RESULT"
    )
    print(
        "=" * 60
    )

    if normal_key in found:

        normal_item = found[
            normal_key
        ]

        print(
            "NORMAL EURUSD: AVAILABLE"
        )

        print(
            "Trading name:",
            normal_item["name"]
        )

        print(
            "Active ID:",
            normal_item["id"]
        )

        print(
            "Option type:",
            normal_item["option_type"]
        )

    else:

        print(
            "NORMAL EURUSD: NOT AVAILABLE "
            "FOR CURRENT BINARY/TURBO TEST"
        )

        if forex_open:

            print(
                "IMPORTANT: Normal EURUSD exists "
                "as Forex, but not as an available "
                "binary/turbo asset right now."
            )

        else:

            print(
                "Normal EURUSD is not currently "
                "open as Forex either."
            )

    if otc_key in found:

        otc_item = found[
            otc_key
        ]

        print(
            "EURUSD OTC: AVAILABLE"
        )

        print(
            "Trading name:",
            otc_item["name"]
        )

        print(
            "Active ID:",
            otc_item["id"]
        )

        print(
            "Option type:",
            otc_item["option_type"]
        )

    else:

        print(
            "EURUSD OTC: NOT AVAILABLE "
            "FOR BINARY/TURBO"
        )

    if not found:

        print("")
        print(
            "EUR/USD diagnostic candidates:"
        )

        seen = set()

        for item in diagnostics:

            if item in seen:
                continue

            seen.add(item)

            print(
                "-",
                item
            )

    return found


def get_candles(
    active_id,
    count
):
    global api

    if api is None:
        return []

    try:

        api.api.candles.candles_data = []

        server_time = (
            api.get_server_timestamp()
        )

        if not server_time:
            server_time = time.time()

        api.api.getcandles(
            active_id,
            CANDLE_SECONDS,
            count,
            server_time
        )

        started = time.time()

        while (
            time.time() - started
            < CANDLE_REQUEST_TIMEOUT
        ):

            candles = (
                api.api.candles.candles_data
            )

            if (
                candles
                and len(candles) >= count
            ):

                return candles

            time.sleep(
                0.2
            )

        candles = (
            api.api.candles.candles_data
        )

        if candles:
            return candles

    except Exception as exc:

        print(
            "Candle error:",
            repr(exc)
        )

    return []


def calculate_momentum(candles):
    closes = []

    for candle in candles:

        try:

            close = float(
                candle["close"]
            )

            closes.append(
                close
            )

        except Exception:
            continue

    if len(closes) < (
        MOMENTUM_PERIOD + 3
    ):

        return []

    momentum = []

    start = MOMENTUM_PERIOD

    for i in range(
        start,
        len(closes)
    ):

        old_price = closes[
            i - MOMENTUM_PERIOD
        ]

        if old_price == 0:
            continue

        value = (
            (
                closes[i]
                - old_price
            )
            / old_price
        ) * 100.0

        momentum.append(
            value
        )

    return momentum


def percentile(
    values,
    percent
):
    if not values:
        return None

    ordered = sorted(
        values
    )

    if len(ordered) == 1:
        return ordered[0]

    position = (
        len(ordered) - 1
    ) * percent

    lower = int(
        position
    )

    upper = lower + 1

    if upper >= len(ordered):
        return ordered[lower]

    weight = (
        position - lower
    )

    return (
        ordered[lower]
        + (
            ordered[upper]
            - ordered[lower]
        ) * weight
    )


def analyze_momentum(candles):
    momentum = calculate_momentum(
        candles
    )

    if len(momentum) < 4:
        return None

    lookback = momentum[
        -MOMENTUM_LOOKBACK:
    ]

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

    if (
        low_level is None
        or high_level is None
    ):
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

            turn_distance = abs(
                current - previous
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

                action = "CALL"

    elif extreme == "HIGH":

        turned_down = (
            previous > previous_two
            and current < previous
        )

        if turned_down:

            turn_distance = abs(
                current - previous
            )

            if (
                turn_distance
                >= MIN_TURN_DISTANCE
            ):

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

    reversal_strength = 0.0

    if previous != 0:

        reversal_strength = (
            abs(
                current - previous
            )
            / max(
                abs(previous),
                0.000001
            )
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


def build_signal_message(
    asset_name,
    analysis,
    signal_id
):
    action = analysis["action"]

    if action == "CALL":
        direction = "🟢 CALL"

    else:
        direction = "🔴 PUT"

    current = analysis["current"]
    previous = analysis["previous"]
    previous_two = analysis["previous_two"]
    extreme = analysis["extreme"]

    strength = analysis.get(
        "reversal_strength",
        0.0
    )

    message = (
        "<b>🤖 Crypto Signal Bot</b>\n\n"
        "<b>Strategy:</b> Momentum 10\n"
        "<b>Asset:</b> "
        + asset_name
        + "\n"
        "<b>Signal:</b> "
        + direction
        + "\n"
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
    if os.path.exists(
        LOG_FILE
    ):
        return

    try:

        with open(
            LOG_FILE,
            "w",
            newline=""
        ) as file:

            writer = csv.writer(
                file
            )

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

        print(
            "Log setup error:",
            repr(exc)
        )


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

        with open(
            LOG_FILE,
            "a",
            newline=""
        ) as file:

            writer = csv.writer(
                file
            )

            writer.writerow([
                datetime.now(
                    timezone.utc
                ).isoformat(),
                signal_id,
                asset,
                action,
                STAKE,
                EXPIRY_MINUTES,
                result,
                profit,
                analysis.get(
                    "current",
                    ""
                ),
                analysis.get(
                    "previous",
                    ""
                ),
                analysis.get(
                    "previous_two",
                    ""
                ),
                analysis.get(
                    "extreme",
                    ""
                )
            ])

    except Exception as exc:

        print(
            "Log error:",
            repr(exc)
        )


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
    global unknown_results
    global completed_trades
    global pending_results

    result_name = "UNKNOWN"
    profit = 0.0

    try:

        print("")
        print(
            "Monitoring:",
            signal_id
        )

        print(
            "Order ID:",
            order_id
        )

        time.sleep(
            EXPIRY_MINUTES * 60
        )

        result = api.check_win_v4(
            order_id
        )

        print(
            "Raw trade result:",
            repr(result)
        )

        try:

            profit = float(
                result
            )

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
            current_completed = (
                completed_trades
            )

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

        send_telegram(
            message
        )

        print("")
        print(
            "RESULT:",
            result_name
        )

        print(
            "Profit:",
            profit
        )

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

        print(
            "Trade monitoring error:",
            repr(exc)
        )

        with state_lock:

            unknown_results += 1
            completed_trades += 1

            current_completed = (
                completed_trades
            )

            current_unknown = (
                unknown_results
            )

        log_trade(
            signal_id,
            asset,
            action,
            result_name,
            profit,
            analysis
        )

        send_telegram(
            "⚠️ <b>TRADE RESULT UNKNOWN</b>\n\n"
            "<b>Asset:</b> "
            + asset
            + "\n"
            + "<b>Action:</b> "
            + action
            + "\n"
            + "<b>Signal ID:</b> "
            + signal_id
            + "\n"
            + "<b>Reason:</b> "
            + str(exc)
            + "\n\n"
            + "<b>Completed:</b> "
            + str(current_completed)
            + "/"
            + str(TARGET_TRADES)
            + "\n"
            + "<b>Unknown:</b> "
            + str(current_unknown)
        )

    finally:

        with state_lock:
            pending_results -= 1


def connect_iq():
    global api

    email = os.getenv(
        "IQ_EMAIL",
        ""
    )

    password = os.getenv(
        "IQ_PASSWORD",
        ""
    )

    if not email or not password:

        print(
            "Missing IQ_EMAIL or IQ_PASSWORD."
        )

        return False

    print("")
    print(
        "Connecting to IQ Option..."
    )

    try:

        api = IQ_Option(
            email,
            password
        )

        check, reason = (
            api.connect()
        )

        if check:

            print(
                "Connected successfully."
            )

            return True

        print(
            "Connection failed:",
            reason
        )

    except Exception as exc:

        print(
            "Connection error:",
            repr(exc)
        )

    return False


def send_startup():
    message = (
        "<b>🤖 Crypto Signal Bot</b>\n\n"
        "<b>Strategy:</b> Momentum 10\n"
        "<b>Assets:</b> "
        "<b>EUR/USD OTC + EUR/USD</b>\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Auto Trading:</b> ON\n"
        "<b>Expiry:</b> 1 MINUTE\n"
        "<b>Target:</b> 50 bot trades\n\n"
        "Controlled EUR/USD test started.\n\n"
        "🟢 <b>IQ Option connected</b>\n\n"
        "Checking normal EUR/USD and EUR/USD OTC..."
    )

    send_telegram(
        message
    )


def send_completion():
    with state_lock:

        final_wins = wins
        final_losses = losses
        final_draws = draws
        final_unknown = unknown_results
        final_completed = completed_trades
        final_opened = total_trades

    decided = (
        final_wins
        + final_losses
    )

    if decided > 0:

        win_rate = (
            final_wins
            / decided
        ) * 100.0

    else:

        win_rate = 0.0

    message = (
        "<b>🏁 EUR/USD TEST COMPLETE</b>\n\n"
        "<b>Strategy:</b> Momentum 10\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Assets:</b> EUR/USD OTC + EUR/USD\n"
        "<b>Bot Trades Opened:</b> "
        + str(final_opened)
        + "/"
        + str(TARGET_TRADES)
        + "\n"
        + "<b>Completed:</b> "
        + str(final_completed)
        + "\n"
        + "<b>Wins:</b> "
        + str(final_wins)
        + "\n"
        + "<b>Losses:</b> "
        + str(final_losses)
        + "\n"
        + "<b>Draws:</b> "
        + str(final_draws)
        + "\n"
        + "<b>Unknown:</b> "
        + str(final_unknown)
        + "\n"
        + "<b>Win Rate:</b> "
        + str(round(win_rate, 2))
        + "%\n\n"
        "Controlled test finished."
    )

    send_telegram(
        message
    )


def get_trade_asset_name(
    asset_name
):
    name = str(
        asset_name
    ).strip()

    if name.lower().startswith(
        "front."
    ):
        name = name[6:]

    if name.lower().startswith(
        "front_"
    ):
        name = name[6:]

    return name


def open_practice_trade(
    asset_name,
    active_id,
    action
):
    trade_asset = (
        get_trade_asset_name(
            asset_name
        )
    )

    print("")
    print(
        "TRADE OPEN ATTEMPT"
    )

    print(
        "Original asset:",
        repr(asset_name)
    )

    print(
        "Trading asset:",
        repr(trade_asset)
    )

    print(
        "Active ID:",
        repr(active_id)
    )

    print(
        "Action:",
        repr(action.lower())
    )

    print(
        "Stake:",
        repr(STAKE)
    )

    print(
        "Expiry:",
        repr(EXPIRY_MINUTES)
    )

    print(
        "Balance mode:",
        repr(BALANCE_MODE)
    )

    try:

        result = api.buy(
            STAKE,
            trade_asset,
            action.lower(),
            EXPIRY_MINUTES
        )

        print(
            "RAW api.buy() RESULT:",
            repr(result)
        )

        if isinstance(
            result,
            tuple
        ):

            if len(result) >= 2:

                success = result[0]
                order_id = result[1]

            elif len(result) == 1:

                success = result[0]
                order_id = None

            else:

                success = False
                order_id = None

        else:

            success = bool(
                result
            )

            order_id = None

        print(
            "api.buy success:",
            repr(success)
        )

        print(
            "api.buy order/error:",
            repr(order_id)
        )

        if success:

            return (
                True,
                order_id,
                trade_asset,
                ""
            )

        reason = (
            "IQ Option returned "
            "success=False. API response: "
            + repr(order_id)
        )

        return (
            False,
            None,
            trade_asset,
            reason
        )

    except Exception as exc:

        reason = (
            "Exception during api.buy(): "
            + repr(exc)
        )

        print(
            reason
        )

        return (
            False,
            None,
            trade_asset,
            reason
        )


def run_scanner():
    global active_assets
    global total_trades
    global pending_results

    print("")
    print(
        "=" * 60
    )
    print(
        "MOMENTUM 10 EXTREME-REVERSAL"
    )
    print(
        "IQ OPTION EUR/USD + EUR/USD OTC"
    )
    print(
        "PRACTICE MODE"
    )
    print(
        "=" * 60
    )

    print("")
    print(
        "EUR/USD OTC + EUR/USD"
    )

    print(
        "Target trades:",
        TARGET_TRADES
    )

    print(
        "Stake:",
        STAKE
    )

    print(
        "Expiry:",
        EXPIRY_MINUTES,
        "minute"
    )

    print("")

    if not connect_iq():

        send_telegram(
            "❌ <b>IQ Option connection failed.</b>\n"
            "Controlled EUR/USD test stopped."
        )

        return

    send_startup()

    try:

        api.change_balance(
            BALANCE_MODE
        )

        print(
            "Balance mode set to:",
            BALANCE_MODE
        )

    except Exception as exc:

        print(
            "Balance mode error:",
            repr(exc)
        )

    ensure_log_file()

    active_assets = {}

    last_refresh = 0
    last_heartbeat = time.time()

    while True:

        with state_lock:

            current_pending = (
                pending_results
            )

            current_opened = (
                total_trades
            )

        if (
            current_opened >= TARGET_TRADES
            and current_pending == 0
        ):

            print("")
            print(
                "Target of 50 opened bot "
                "trades reached and all "
                "results resolved."
            )

            send_completion()

            break

        now = time.time()

        if (
            not active_assets
            or now - last_refresh
            >= ASSET_REFRESH_SECONDS
        ):

            active_assets = (
                get_controlled_assets()
            )

            last_refresh = now

            if active_assets:

                for key, item in (
                    active_assets.items()
                ):

                    print(
                        "Controlled asset:",
                        item["name"],
                        "| ID:",
                        item["id"],
                        "| Type:",
                        item.get(
                            "option_type",
                            "unknown"
                        )
                    )

                found_names = []

                for item in (
                    active_assets.values()
                ):

                    found_names.append(
                        item["name"]
                    )

                send_telegram(
                    "🟢 <b>EUR/USD asset scan complete</b>\n\n"
                    "<b>Available:</b> "
                    + ", ".join(
                        found_names
                    )
                    + "\n\n"
                    "<b>Starting controlled scan.</b>"
                )

            else:

                print(
                    "Controlled EUR/USD binary/"
                    "turbo assets unavailable."
                )

                send_telegram(
                    "🟡 <b>EUR/USD binary scan waiting</b>\n\n"
                    "No EUR/USD binary/turbo "
                    "asset is currently available.\n\n"
                    "The scanner will retry."
                )

                time.sleep(
                    RECONNECT_SECONDS
                )

                continue

        for key, item in list(
            active_assets.items()
        ):

            with state_lock:

                if (
                    total_trades
                    >= TARGET_TRADES
                ):
                    break

            asset_name = item["name"]
            active_id = item["id"]

            try:

                candles = get_candles(
                    active_id,
                    MOMENTUM_LOOKBACK
                    + MOMENTUM_PERIOD
                    + 5
                )

                if not candles:
                    continue

                if len(candles) < 20:
                    continue

                closed_candles = (
                    candles[:-1]
                )

                if len(
                    closed_candles
                ) < 20:
                    continue

                signal_candle = (
                    closed_candles[-1]
                )

                candle_time = (
                    signal_candle.get(
                        "from",
                        signal_candle.get(
                            "to",
                            0
                        )
                    )
                )

                analysis = (
                    analyze_momentum(
                        closed_candles
                    )
                )

                if not analysis:
                    continue

                extreme = (
                    analysis["extreme"]
                )

                if (
                    analysis["action"]
                    is None
                ):
                    continue

                with state_lock:

                    previous_candle = (
                        last_signal_candle.get(
                            key
                        )
                    )

                    previous_extreme = (
                        last_extreme_state.get(
                            key
                        )
                    )

                    if (
                        previous_candle
                        == candle_time
                    ):
                        continue

                    if (
                        previous_extreme
                        == extreme
                    ):
                        continue

                    last_signal_candle[
                        key
                    ] = candle_time

                    last_extreme_state[
                        key
                    ] = extreme

                action = (
                    analysis["action"]
                )

                asset_id_text = (
                    asset_key(
                        asset_name
                    )
                )

                signal_id = (
                    "M10-"
                    + asset_id_text
                    + "-"
                    + action
                    + "-"
                    + str(
                        int(
                            time.time()
                        )
                    )
                )

                print("")
                print(
                    "=" * 50
                )

                print(
                    "SIGNAL"
                )

                print(
                    "Asset:",
                    asset_name
                )

                print(
                    "Action:",
                    action
                )

                print(
                    "Option type:",
                    item.get(
                        "option_type",
                        "unknown"
                    )
                )

                print(
                    "Active ID:",
                    active_id
                )

                print(
                    "Signal ID:",
                    signal_id
                )

                print(
                    "Momentum:",
                    analysis["current"]
                )

                print(
                    "Extreme:",
                    analysis["extreme"]
                )

                print(
                    "=" * 50
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

                    if (
                        total_trades
                        >= TARGET_TRADES
                    ):
                        continue

                (
                    success,
                    order_id,
                    trade_asset,
                    trade_error
                ) = open_practice_trade(
                    asset_name,
                    active_id,
                    action
                )

                if not success:

                    print("")
                    print(
                        "TRADE WAS NOT OPENED"
                    )

                    print(
                        "Asset:",
                        repr(
                            trade_asset
                        )
                    )

                    print(
                        "Active ID:",
                        repr(
                            active_id
                        )
                    )

                    print(
                        "Action:",
                        repr(
                            action
                        )
                    )

                    print(
                        "Reason:",
                        trade_error
                    )

                    send_telegram(
                        "⚠️ <b>TRADE NOT OPENED</b>\n\n"
                        "<b>Asset:</b> "
                        + trade_asset
                        + "\n"
                        + "<b>Active ID:</b> "
                        + str(
                            active_id
                        )
                        + "\n"
                        + "<b>Action:</b> "
                        + action
                        + "\n"
                        + "<b>Signal ID:</b> "
                        + signal_id
                        + "\n\n"
                        + "<b>API response:</b>\n"
                        + trade_error
                    )

                    continue

                with state_lock:

                    total_trades += 1
                    pending_results += 1

                    opened_number = (
                        total_trades
                    )

                print("")
                print(
                    "TRADE OPENED SUCCESSFULLY"
                )

                print(
                    "Order ID:",
                    repr(
                        order_id
                    )
                )

                print(
                    "Bot trade:",
                    opened_number,
                    "/",
                    TARGET_TRADES
                )

                send_telegram(
                    "🚀 <b>TRADE OPENED</b>\n\n"
                    "<b>Asset:</b> "
                    + trade_asset
                    + "\n"
                    + "<b>Action:</b> "
                    + action
                    + "\n"
                    + "<b>Stake:</b> $"
                    + str(
                        STAKE
                    )
                    + "\n"
                    + "<b>Expiry:</b> 1 minute\n"
                    + "<b>Bot Trade #:</b> "
                    + str(
                        opened_number
                    )
                    + "/"
                    + str(
                        TARGET_TRADES
                    )
                    + "\n"
                    + "<b>Signal ID:</b> "
                    + signal_id
                )

                worker = threading.Thread(
                    target=monitor_trade,
                    args=(
                        order_id,
                        signal_id,
                        trade_asset,
                        action,
                        analysis
                    )
                )

                worker.daemon = True
                worker.start()

            except Exception as exc:

                print(
                    "Scan error for",
                    asset_name,
                    ":",
                    repr(exc)
                )

        if (
            time.time()
            - last_heartbeat
            >= HEARTBEAT_SECONDS
        ):

            with state_lock:

                heartbeat_opened = (
                    total_trades
                )

                heartbeat_completed = (
                    completed_trades
                )

                heartbeat_pending = (
                    pending_results
                )

                heartbeat_wins = wins
                heartbeat_losses = losses
                heartbeat_draws = draws

                heartbeat_unknown = (
                    unknown_results
                )

            print("")
            print(
                "HEARTBEAT"
            )

            print(
                "Opened:",
                heartbeat_opened,
                "/",
                TARGET_TRADES
            )

            print(
                "Completed:",
                heartbeat_completed
            )

            print(
                "Pending:",
                heartbeat_pending
            )

            print(
                "W/L/D/U:",
                heartbeat_wins,
                "/",
                heartbeat_losses,
                "/",
                heartbeat_draws,
                "/",
                heartbeat_unknown
            )

            send_telegram(
                "💓 <b>Scanner heartbeat</b>\n\n"
                "<b>EUR/USD + EUR/USD OTC</b>\n"
                "<b>Bot trades opened:</b> "
                + str(
                    heartbeat_opened
                )
                + "/"
                + str(
                    TARGET_TRADES
                )
                + "\n"
                + "<b>Completed:</b> "
                + str(
                    heartbeat_completed
                )
                + "\n"
                + "<b>Pending:</b> "
                + str(
                    heartbeat_pending
                )
                + "\n"
                + "<b>W/L/D/U:</b> "
                + str(
                    heartbeat_wins
                )
                + "/"
                + str(
                    heartbeat_losses
                )
                + "/"
                + str(
                    heartbeat_draws
                )
                + "/"
                + str(
                    heartbeat_unknown
                )
            )

            last_heartbeat = (
                time.time()
            )

        time.sleep(
            SCAN_INTERVAL
        )


if __name__ == "__main__":
    run_scanner()
