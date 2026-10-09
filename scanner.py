
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
        print("Telegram credentials missing.", flush=True)
        return

    try:
        response = requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message[:3900],
                "parse_mode": "HTML"
            },
            timeout=15
        )

        if not response.ok:
            print("Telegram HTTP error:", response.status_code, flush=True)

    except Exception as exc:
        print("Telegram error:", str(exc)[:200], flush=True)


def normalize_name(name):
    text = str(name).strip().upper()

    for prefix in ["FRONT.", "FRONT_", "FRONT-"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    return text.replace("_", "-")


def discover_assets():
    print("Discovering assets...", flush=True)
    found = {}

    try:
        data = api.get_all_ACTIVES_OPCODE()

        if isinstance(data, dict):
            for name, active_id in data.items():
                symbol = normalize_name(name)

                try:
                    number = int(active_id)
                except (ValueError, TypeError):
                    continue

                if symbol and len(symbol) >= 5:
                    found[symbol] = number

    except Exception as exc:
        print("Asset discovery error:", str(exc)[:200], flush=True)

    if not found:
        print("No active-ID assets returned by the API.", flush=True)
        return {}

    print("Assets discovered:", len(found), flush=True)

    for name, active_id in list(found.items())[:10]:
        print("ASSET:", name, "| ID:", active_id, flush=True)

    return found


def get_candles(symbol, count):
    try:
        candles = api.get_candles(
            symbol,
            CANDLE_SECONDS,
            count,
            time.time()
        )

        if not isinstance(candles, list):
            return []

        candles = sorted(
            candles,
            key=lambda item: item.get("from", 0)
        )

        return candles

    except Exception as exc:
        print(
            "CANDLE ERROR:",
            symbol,
            str(exc)[:150],
            flush=True
        )
        return []


def momentum_values(candles):
    closes = []

    for candle in candles:
        try:
            closes.append(float(candle["close"]))
        except (KeyError, TypeError, ValueError):
            continue

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

    if current <= low:
        if (
            previous < previous_two
            and current > previous
            and abs(current - previous) >= MIN_TURN_DISTANCE
        ):
            action = "CALL"

    elif current >= high:
        if (
            previous > previous_two
            and current < previous
            and abs(current - previous) >= MIN_TURN_DISTANCE
        ):
            action = "PUT"

    return {
        "action": action,
        "current": current
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
        print("CSV ERROR:", str(exc)[:150], flush=True)


def monitor_trade(order_id, signal_id, symbol, action, analysis):
    global wins, losses, draws, completed, pending

    try:
        profit = float(api.check_win_v4(order_id))

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
            signal_id, symbol, action,
            result_name, profit, analysis
        )

        print(
            "RESULT:", result_name,
            "|", symbol,
            "| Profit:", profit,
            "| Resolved:", resolved,
            "/", TARGET_TRADES,
            flush=True
        )

        telegram(
            "<b>" + result_name + "</b>\n"
            "<b>Asset:</b> " + symbol + "\n"
            "<b>Direction:</b> " + action + "\n"
            "<b>Profit:</b> $" + str(round(profit, 2)) + "\n"
            "<b>Resolved:</b> " + str(resolved) + "/" +
            str(TARGET_TRADES) + "\n"
            "<b>W/L/D:</b> " + str(current_wins) + "/" +
            str(current_losses) + "/" + str(current_draws)
        )

    except Exception as exc:
        print(
            "RESULT CHECK ERROR:",
            signal_id,
            str(exc)[:200],
            flush=True
        )

        log_result(
            signal_id, symbol, action,
            "UNKNOWN", 0.0, analysis
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
        print("Missing IQ_OPTION GitHub Secrets.", flush=True)
        return False

    try:
        api = IQ_Option(email, password)
        connected, reason = api.connect()

        if not connected:
            print("Connection failed:", reason, flush=True)
            return False

        print("IQ Option connected.", flush=True)

        api.change_balance(BALANCE_MODE)
        print("Balance mode:", BALANCE_MODE, flush=True)

        return True

    except Exception as exc:
        print("Connection error:", str(exc)[:200], flush=True)
        return False


def run_scanner():
    global assets, opened, pending

    print("=" * 45, flush=True)
    print("MOMENTUM 10 ALL-ASSETS SCANNER", flush=True)
    print("PRACTICE | $1 | 1-MINUTE EXPIRY", flush=True)
    print("=" * 45, flush=True)

    if not connect():
        telegram("❌ Scanner stopped. Check connection and secrets.")
        return

    ensure_log()

    telegram(
        "<b>🤖 Momentum 10 Scanner Started</b>\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Stake:</b> $1\n"
        "<b>Expiry:</b> 1 minute\n"
        "<b>Target:</b> 50 resolved trades\n"
        "<b>Strategy:</b> Momentum 10 Extreme-Reversal"
    )

    assets = discover_assets()

    if not assets:
        print("STOP: No assets discovered.", flush=True)
        telegram("⚠️ No assets discovered. Scanner stopped.")
        return

    print("Starting candle scan now...", flush=True)

    telegram(
        "🟢 <b>Scan starting</b>\n"
        "<b>Discovered:</b> " + str(len(assets)) + "\n"
        "Checking candles and looking for valid signals."
    )

    last_refresh = time.time()
    last_heartbeat = time.time()
    candle_count = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 5
    pass_number = 0

    while True:
        with lock:
            resolved = completed
            opened_count = opened
            pending_count = pending

        if resolved >= TARGET_TRADES and pending_count == 0:
            break

        if opened_count >= MAX_OPENED_TRADES and pending_count == 0:
            print("Safety limit reached.", flush=True)
            break

        if time.time() - last_refresh >= ASSET_REFRESH_SECONDS:
            refreshed = discover_assets()

            if refreshed:
                assets = refreshed

            last_refresh = time.time()

        pass_number += 1
        total = len(assets)
        checked = 0
        usable = 0
        no_candles = 0
        signal_count = 0

        print(
            "SCAN PASS:", pass_number,
            "| Assets:", total,
            "| Opened:", opened_count,
            "| Resolved:", resolved,
            flush=True
        )

        for symbol, active_id in list(assets.items()):
            with lock:
                if completed >= TARGET_TRADES:
                    break
                if opened >= MAX_OPENED_TRADES:
                    break
                if pending >= MAX_PENDING_TRADES:
                    break

            checked += 1

            print(
                "CHECKING:", checked, "/", total,
                "|", symbol,
                flush=True
            )

            candles = get_candles(symbol, candle_count)

            if len(candles) < candle_count - 2:
                no_candles += 1
                continue

            closed = candles[:-1]

            if len(closed) < candle_count - 3:
                no_candles += 1
                continue

            usable += 1

            candle_time = closed[-1].get("from", 0)

            if last_signal_candle.get(symbol) == candle_time:
                continue

            result = analyze(closed)

            if not result or not result["action"]:
                continue

            action = result["action"]
            signal_id = (
                "M10-" + str(int(time.time())) +
                "-" + str(active_id) + "-" + action
            )

            with lock:
                if completed + pending >= TARGET_TRADES:
                    break
                if pending >= MAX_PENDING_TRADES:
                    continue

                last_signal_candle[symbol] = candle_time
                pending += 1

            signal_count += 1

            print(
                "SIGNAL:", action,
                "| Asset:", symbol,
                "| Momentum:", round(result["current"], 5),
                "| ID:", signal_id,
                flush=True
            )

            telegram(
                "<b>📊 MOMENTUM 10 SIGNAL</b>\n"
                "<b>Asset:</b> " + symbol + "\n"
                "<b>Direction:</b> " + action + "\n"
                "<b>Stake:</b> $1 PRACTICE\n"
                "<b>Expiry:</b> 1 minute\n"
                "<b>Momentum:</b> " +
                str(round(result["current"], 5)) + "\n"
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
                    print(
                        "ORDER REJECTED:", symbol,
                        "| Check whether this asset supports binary/turbo trading.",
                        flush=True
                    )

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
                    "TRADE OPENED:", opened_number,
                    "|", symbol, "|", action,
                    "| Order:", order_id,
                    flush=True
                )

                telegram(
                    "🚀 <b>PRACTICE TRADE OPENED</b>\n"
                    "<b>Asset:</b> " + symbol + "\n"
                    "<b>Direction:</b> " + action + "\n"
                    "<b>Stake:</b> $1\n"
                    "<b>Opened:</b> " + str(opened_number) + "\n"
                    "<b>Signal ID:</b> " + signal_id
                )

                worker = threading.Thread(
                    target=monitor_trade,
                    args=(
                        order_id, signal_id,
                        symbol, action, result
                    ),
                    daemon=True
                )
                worker.start()

            except Exception as exc:
                print(
                    "ORDER ERROR:", symbol,
                    str(exc)[:200],
                    flush=True
                )

                with lock:
                    pending = max(0, pending - 1)

                telegram(
                    "❌ <b>Order error</b>\n"
                    "<b>Asset:</b> " + symbol + "\n"
                    "<b>Error:</b> " + str(exc)[:250]
                )

            if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                with lock:
                    current_opened = opened
                    current_completed = completed
                    current_pending = pending

                print(
                    "HEARTBEAT:",
                    "| Checked:", checked, "/", total,
                    "| Usable candles:", usable,
                    "| No candles:", no_candles,
                    "| Opened:", current_opened,
                    "| Resolved:", current_completed,
                    "| Pending:", current_pending,
                    flush=True
                )

                last_heartbeat = time.time()

        print(
            "PASS SUMMARY:",
            "| Checked:", checked, "/", total,
            "| Usable candles:", usable,
            "| Insufficient candles:", no_candles,
            "| Signals:", signal_count,
            flush=True
        )

        with lock:
            resolved = completed
            opened_count = opened
            pending_count = pending

        if resolved >= TARGET_TRADES and pending_count == 0:
            break

        if opened_count >= MAX_OPENED_TRADES and pending_count == 0:
            break

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

    print(
        "FINAL:",
        "| Opened:", final_opened,
        "| Resolved:", final_completed,
        "| W/L/D:", final_wins, final_losses, final_draws,
        "| Win rate:", round(rate, 2), "%",
        flush=True
    )

    telegram(
        "<b>🏁 SCANNER FINISHED</b>\n"
        "<b>Mode:</b> PRACTICE\n"
        "<b>Opened:</b> " + str(final_opened) + "\n"
        "<b>Resolved:</b> " + str(final_completed) + "\n"
        "<b>Wins:</b> " + str(final_wins) + "\n"
        "<b>Losses:</b> " + str(final_losses) + "\n"
        "<b>Draws:</b> " + str(final_draws) + "\n"
        "<b>Win rate excluding draws:</b> " +
        str(round(rate, 2)) + "%"
    )


if __name__ == "__main__":
    run_scanner()
