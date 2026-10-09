
import os
import csv
import time
import threading
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option


MOMENTUM_PERIOD = 10
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1

BALANCE_MODE = "PRACTICE"
STAKE = 1.0
AUTO_TRADE = True
TARGET_TRADES = 50

MAX_OPENED_TRADES = 100
MAX_PENDING_TRADES = 3
SCAN_INTERVAL = 1
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
CANDLE_TIMEOUT = 8

LOG_FILE = "momentum_signal_log.csv"

api = None
assets = {}
last_signal_candle = {}

wins = 0
losses = 0
draws = 0
completed = 0
opened = 0
pending = 0

lock = threading.Lock()


def telegram(message):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("Telegram credentials missing.")
        return

    url = "https://api.telegram.org/bot" + token + "/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message[:3900],
                "parse_mode": "HTML"
            },
            timeout=15
        )

        if not response.ok:
            print("Telegram HTTP error:", response.status_code)

    except Exception as exc:
        print("Telegram error:", exc)


def normalize_name(name):
    text = str(name).strip().upper()

    for prefix in ["FRONT.", "FRONT_", "FRONT-"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    return text.replace("_", "-")


def asset_id_map():
    found = {}

    try:
        data = api.get_all_ACTIVES_OPCODE()

        if isinstance(data, dict):
            for name, active_id in data.items():
                symbol = normalize_name(name)

                if not symbol or not symbol[0].isalnum():
                    continue

                try:
                    number = int(active_id)
                except (ValueError, TypeError):
                    continue

                if len(symbol) >= 5:
                    found[symbol] = number

    except Exception as exc:
        print("Active-ID discovery error:", exc)

    return found


def discover_assets():
    print("=" * 45)
    print("DISCOVERING IQ OPTION ASSETS")
    print("=" * 45)

    found = asset_id_map()

    if not found:
        print("Active-ID list unavailable. Trying initialization data.")

        sources = []

        try:
            data = api.get_all_init_v2()

            if data:
                sources.append(data)

        except Exception as exc:
            print("V2 discovery error:", exc)

        try:
            data = api.get_all_init()

            if data:
                sources.append(data)

        except Exception as exc:
            print("Legacy discovery error:", exc)

        def walk(obj, depth=0):
            if depth > 18:
                return

            if isinstance(obj, dict):
                for raw_key, value in obj.items():
                    name = None
                    active_id = None

                    if isinstance(value, dict):
                        for field in [
                            "name",
                            "symbol",
                            "active_name",
                            "activeName"
                        ]:
                            candidate = value.get(field)

                            if isinstance(candidate, str) and candidate:
                                name = candidate
                                break

                        for field in [
                            "id",
                            "active_id",
                            "activeId"
                        ]:
                            try:
                                active_id = int(value[field])
                                break
                            except (KeyError, TypeError, ValueError):
                                pass

                    if name is None:
                        name = str(raw_key)

                    if active_id is None and str(raw_key).isdigit():
                        active_id = int(raw_key)

                    symbol = normalize_name(name)

                    if active_id is not None and len(symbol) >= 5:
                        if symbol[0].isalnum():
                            found[symbol] = active_id

                    if isinstance(value, (dict, list)):
                        walk(value, depth + 1)

            elif isinstance(obj, list):
                for item in obj:
                    walk(item, depth + 1)

        for source in sources:
            walk(source)

    print("Assets discovered:", len(found))

    for name, active_id in list(found.items())[:25]:
        print("ASSET:", name, "| ID:", active_id)

    if len(found) > 25:
        print("Additional assets:", len(found) - 25)

    return found


def get_candles(symbol, count):
    try:
        candles = api.get_candles(
            symbol,
            CANDLE_SECONDS,
            count,
            time.time()
        )

        if not candles:
            return []

        candles = sorted(
            candles,
            key=lambda candle: candle.get("from", 0)
        )

        return candles

    except Exception as exc:
        print("Candle error:", symbol, str(exc)[:200])
        return []


def momentum_values(candles):
    closes = []

    for candle in candles:
        try:
            closes.append(float(candle["close"]))
        except (KeyError, TypeError, ValueError):
            pass

    if len(closes) < MOMENTUM_PERIOD + 3:
        return []

    values = []

    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]

        if old_price == 0:
            continue

        change = (
            (closes[index] - old_price) / old_price
        ) * 100.0

        values.append(change)

    return values


def percentile(values, fraction):
    ordered = sorted(values)

    if not ordered:
        return None

    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower

    return (
        ordered[lower]
        + (ordered[upper] - ordered[lower]) * weight
    )


def analyze(candles):
    values = momentum_values(candles)

    if len(values) < MOMENTUM_LOOKBACK:
        return None

    recent = values[-MOMENTUM_LOOKBACK:]

    current = recent[-1]
    previous = recent[-2]
    previous_two = recent[-3]

    low = percentile(recent, EXTREME_PERCENTILE)
    high = percentile(recent, 1.0 - EXTREME_PERCENTILE)

    action = None
    extreme = "NONE"

    if current <= low:
        extreme = "LOW"

        if (
            previous < previous_two
            and current > previous
            and abs(current - previous) >= MIN_TURN_DISTANCE
        ):
            action = "CALL"

    elif current >= high:
        extreme = "HIGH"

        if (
            previous > previous_two
            and current < previous
            and abs(current - previous) >= MIN_TURN_DISTANCE
        ):
            action = "PUT"

    return {
        "action": action,
        "current": current,
        "previous": previous,
        "previous_two": previous_two,
        "extreme": extreme
    }


def ensure_log():
    if os.path.exists(LOG_FILE):
        return

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
            "momentum"
        ])


def log_result(signal_id, symbol, action, result, profit, analysis):
    try:
        ensure_log()

        with open(LOG_FILE, "a", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                signal_id,
                symbol,
                action,
                STAKE,
                EXPIRY_MINUTES,
                result,
                profit,
                analysis["current"]
            ])

    except Exception as exc:
        print("CSV logging error:", exc)


def monitor_trade(order_id, signal_id, symbol, action, analysis):
    global wins, losses, draws, completed, pending

    result_name = "UNKNOWN"
    profit = 0.0

    try:
        result = api.check_win_v4(order_id)
        profit = float(result)

        if profit > 0:
            result_name = "WIN"
        elif profit < 0:
            result_name = "LOSS"
        else:
            result_name = "DRAW"

        with lock:
            if result_name == "WIN":
                wins += 1
            elif result_name == "LOSS":
                losses += 1
            else:
                draws += 1

            completed += 1

            resolved = completed
            current_wins = wins
            current_losses = losses
            current_draws = draws

        log_result(
            signal_id,
            symbol,
            action,
            result_name,
            profit,
            analysis
        )

        print(
            "RESULT:",
            result_name,
            "|",
            symbol,
            "| Profit:",
            profit,
            "| Resolved:",
            resolved
        )

        telegram(
            "<b>" + result_name + "</b>\n\n"
            "<b>Asset:</b> " + symbol + "\n"
            "<b>Action:</b> " + action + "\n"
            "<b>Profit:</b> $" + str(round(profit, 2)) + "\n"
            "<b>Signal ID:</b> " + signal_id + "\n"
            "<b>Resolved:</b> " + str(resolved)
            + "/" + str(TARGET_TRADES) + "\n"
            "<b>W/L/D:</b> " + str(current_wins) + "/"
            + str(current_losses) + "/" + str(current_draws)
        )

    except Exception as exc:
        print("Result check failed:", signal_id, str(exc)[:200])

        log_result(
            signal_id,
            symbol,
            action,
            result_name,
            profit,
            analysis
        )

        telegram(
            "⚠️ <b>Result unknown</b>\n"
            "<b>Asset:</b> " + symbol + "\n"
            "<b>Signal ID:</b> " + signal_id + "\n"
            "Not counted as resolved."
        )

    finally:
        with lock:
            pending = max(0, pending - 1)


def connect():
    global api

    email = os.getenv("IQ_EMAIL", "")
    password = os.getenv("IQ_PASSWORD", "")

    if not email or not password:
        print("IQ_EMAIL or IQ_PASSWORD is missing.")
        return False

    try:
        api = IQ_Option(email, password)
        connected, reason = api.connect()

        if not connected:
            print("Connection failed:", reason)
            return False

        print("IQ Option connected.")

        api.change_balance(BALANCE_MODE)

        print("Balance mode selected:", BALANCE_MODE)
        return True

    except Exception as exc:
        print("Connection error:", exc)
        return False


def run_scanner():
    global assets, opened, pending

    print("=" * 45)
    print("MOMENTUM 10 ALL-ASSETS SCANNER")
    print("PRACTICE MODE | $1 | 1-MINUTE EXPIRY")
    print("=" * 45)

    if not connect():
        telegram(
            "❌ <b>Scanner stopped</b>\n"
            "IQ Option connection or practice setup failed."
        )
        return

    telegram(
        "<b>🤖 Momentum 10 Scanner Started</b>\n\n"
        "<b>Assets:</b> All discovered assets\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Stake:</b> $1\n"
        "<b>Expiry:</b> 1 minute\n"
        "<b>Target:</b> 50 resolved trades\n"
        "<b>Strategy:</b> Momentum 10 Extreme-Reversal"
    )

    ensure_log()
    assets = discover_assets()

    if not assets:
        telegram(
            "⚠️ No assets discovered. "
            "Check the GitHub Actions logs."
        )
        return

    telegram(
        "🟢 <b>Asset scan ready</b>\n"
        "<b>Discovered:</b> " + str(len(assets))
    )

    last_refresh = time.time()
    last_heartbeat = time.time()
    candle_count = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5

    while True:
        with lock:
            resolved = completed
            opened_count = opened
            pending_count = pending

        if resolved >= TARGET_TRADES and pending_count == 0:
            break

        if opened_count >= MAX_OPENED_TRADES and pending_count == 0:
            print("Maximum opened-trade safety limit reached.")
            break

        if time.time() - last_refresh >= ASSET_REFRESH_SECONDS:
            refreshed = discover_assets()

            if refreshed:
                assets = refreshed

            last_refresh = time.time()

        for symbol, active_id in list(assets.items()):
            with lock:
                if completed >= TARGET_TRADES:
                    break

                if opened >= MAX_OPENED_TRADES:
                    break

                if pending >= MAX_PENDING_TRADES:
                    break

            try:
                candles = get_candles(symbol, candle_count)

                if len(candles) < candle_count - 2:
                    continue

                closed = candles[:-1]

                if len(closed) < candle_count - 3:
                    continue

                candle_time = closed[-1].get(
                    "from",
                    closed[-1].get("to", 0)
                )

                if last_signal_candle.get(symbol) == candle_time:
                    continue

                result = analyze(closed)

                if not result or not result["action"]:
                    continue

                action = result["action"]
                signal_id = (
                    "M10-" + str(int(time.time()))
                    + "-" + str(active_id)
                    + "-" + action
                )

                with lock:
                    if completed + pending >= TARGET_TRADES:
                        break

                    if pending >= MAX_PENDING_TRADES:
                        continue

                    last_signal_candle[symbol] = candle_time
                    pending += 1

                print(
                    "SIGNAL:",
                    action,
                    "| Asset:",
                    symbol,
                    "| ID:",
                    signal_id,
                    "| Momentum:",
                    round(result["current"], 5)
                )

                telegram(
                    "<b>📊 MOMENTUM 10 SIGNAL</b>\n\n"
                    "<b>Asset:</b> " + symbol + "\n"
                    "<b>Direction:</b> " + action + "\n"
                    "<b>Stake:</b> $1 PRACTICE\n"
                    "<b>Expiry:</b> 1 minute\n"
                    "<b>Momentum:</b> "
                    + str(round(result["current"], 5)) + "\n"
                    "<b>Signal ID:</b> " + signal_id
                )

                if not AUTO_TRADE:
                    with lock:
                        pending = max(0, pending - 1)
                    continue

                try:
                    success, order_id = api.buy(
                        STAKE,
                        symbol,
                        action.lower(),
                        EXPIRY_MINUTES
                    )

                    if not success:
                        print("ORDER REJECTED:", symbol)

                        with lock:
                            pending = max(0, pending - 1)

                        telegram(
                            "⚠️ <b>Order rejected</b>\n"
                            "<b>Asset:</b> " + symbol + "\n"
                            "<b>Signal ID:</b> " + signal_id
                        )
                        continue

                    with lock:
                        opened += 1
                        opened_number = opened

                    print(
                        "TRADE OPENED:",
                        opened_number,
                        "|",
                        symbol,
                        "|",
                        action,
                        "|",
                        order_id
                    )

                    telegram(
                        "🚀 <b>PRACTICE TRADE OPENED</b>\n\n"
                        "<b>Asset:</b> " + symbol + "\n"
                        "<b>Direction:</b> " + action + "\n"
                        "<b>Stake:</b> $1\n"
                        "<b>Opened:</b> " + str(opened_number) + "\n"
                        "<b>Signal ID:</b> " + signal_id
                    )

                    worker = threading.Thread(
                        target=monitor_trade,
                        args=(
                            order_id,
                            signal_id,
                            symbol,
                            action,
                            result
                        ),
                        daemon=True
                    )

                    worker.start()

                except Exception as exc:
                    print("ORDER ERROR:", symbol, str(exc)[:200])

                    with lock:
                        pending = max(0, pending - 1)

                    telegram(
                        "❌ <b>Order error</b>\n"
                        "<b>Asset:</b> " + symbol + "\n"
                        "<b>Error:</b> " + str(exc)[:300]
                    )

            except Exception as exc:
                print("SCAN ERROR:", symbol, str(exc)[:200])

        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            with lock:
                resolved = completed
                opened_count = opened
                pending_count = pending
                current_wins = wins
                current_losses = losses
                current_draws = draws

            print(
                "HEARTBEAT:",
                "Assets", len(assets),
                "| Opened", opened_count,
                "| Resolved", resolved,
                "| Pending", pending_count
            )

            telegram(
                "💓 <b>Scanner heartbeat</b>\n\n"
                "<b>Assets:</b> " + str(len(assets)) + "\n"
                "<b>Opened:</b> " + str(opened_count) + "\n"
                "<b>Resolved:</b> " + str(resolved)
                + "/" + str(TARGET_TRADES) + "\n"
                "<b>Pending:</b> " + str(pending_count) + "\n"
                "<b>W/L/D:</b> " + str(current_wins) + "/"
                + str(current_losses) + "/" + str(current_draws)
            )

            last_heartbeat = time.time()

        time.sleep(SCAN_INTERVAL)

    with lock:
        final_wins = wins
        final_losses = losses
        final_draws = draws
        final_completed = completed
        final_opened = opened

    decided = final_wins + final_losses
    rate = 0.0

    if decided:
        rate = final_wins * 100.0 / decided

    telegram(
        "<b>🏁 SCANNER FINISHED</b>\n\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Opened:</b> " + str(final_opened) + "\n"
        "<b>Resolved:</b> " + str(final_completed) + "\n"
        "<b>Wins:</b> " + str(final_wins) + "\n"
        "<b>Losses:</b> " + str(final_losses) + "\n"
        "<b>Draws:</b> " + str(final_draws) + "\n"
        "<b>Win rate excluding draws:</b> "
        + str(round(rate, 2)) + "%"
    )

    print("Scanner finished.")


if __name__ == "__main__":
    run_scanner()
