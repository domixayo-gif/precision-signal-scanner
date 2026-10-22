import os
import re
import csv
import json
import time
import math
import logging
import threading
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 + MOMENTUM 20 + ENGULFING CANDLE
# PRACTICE ACCOUNT ONLY.
IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main").strip()
GITHUB_WORKFLOW_FILE = "scanner.yml"

BALANCE_MODE = "PRACTICE"
AUTO_TRADE = True
STAKE = 1.50
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60
MOMENTUM_PERIOD = 10
MOMENTUM_CONFIRM_PERIOD = 20
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06

SESSION_PROFIT_TARGET = 10.0
SESSION_LOSS_LIMIT = 4.0

SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
REJECTED_ASSET_COOLDOWN_SECONDS = 600
CANDLE_REQUEST_COUNT = MOMENTUM_LOOKBACK + MOMENTUM_CONFIRM_PERIOD + 12
LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "CNY", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN", "CZK",
    "HUF", "TRY", "ZAR", "MXN", "BRL", "INR", "THB", "AED",
    "SAR", "ILS", "KRW", "TWD", "RUB", "RON", "BGN", "ISK",
    "PHP", "IDR", "MYR"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
SESSION_ID = "{}-{}".format(
    os.getenv("GITHUB_RUN_ID", str(int(time.time()))),
    os.getenv("GITHUB_RUN_ATTEMPT", "1")
)
IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0.0
REJECTED_ASSETS = {}
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
NEXT_WORKFLOW_REQUESTED = False


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        response = requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN),
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)


def fresh_state():
    return {
        "date": now_lagos().date().isoformat(),
        "session_id": SESSION_ID,
        "profit": 0.0,
        "wins": 0,
        "losses": 0,
        "resolved": 0,
        "pending_order_id": None,
        "pending_asset": None,
        "pending_direction": None,
        "pending_started": None,
        "last_signals": {},
        "stop_reason": "",
    }


def load_state():
    current = fresh_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if not isinstance(saved, dict):
            return current
    except (OSError, ValueError, TypeError):
        return current

    # Each workflow gets fresh session P/L. Preserve only signal de-duplication
    # and unresolved-order details across workflow runs.
    # Always preserve unresolved orders, even if the next workflow starts
    # after midnight. Only signal de-duplication resets on a new Lagos day.
    for key in ("pending_order_id", "pending_asset", "pending_direction", "pending_started"):
        current[key] = saved.get(key)
    if saved.get("date") == current["date"]:
        current["last_signals"] = saved.get("last_signals", {})
    if not isinstance(current.get("last_signals"), dict):
        current["last_signals"] = {}
    return current


STATE = load_state()


def save_state(persist=False):
    try:
        with STATE_LOCK:
            STATE["date"] = now_lagos().date().isoformat()
            STATE["session_id"] = SESSION_ID
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(STATE, handle, indent=2)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
        if persist:
            persist_state_to_github()
    except Exception as exc:
        logging.warning("State save failed: %s", exc)


def persist_state_to_github():
    # Persist state only when an order opens/resolves or a limit is reached.
    # This avoids making a repository commit for every scanned candle.
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.warning("State not committed: GITHUB_TOKEN/repository unavailable")
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email",
                        "41898282+github-actions[bot]@users.noreply.github.com"],
                       check=True, capture_output=True, text=True)
        files_to_add = [STATE_FILE]
        if os.path.exists(LOG_FILE):
            files_to_add.append(LOG_FILE)
        subprocess.run(["git", "add"] + files_to_add, check=True,
                       capture_output=True, text=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                 capture_output=True).returncode != 0
        if not changed:
            return
        subprocess.run(["git", "commit", "-m", "Persist scanner order state"],
                       check=True, capture_output=True, text=True)
        result = subprocess.run(["git", "push", "origin", "HEAD"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            logging.warning("State push failed: %s", result.stderr.strip())
    except Exception as exc:
        logging.warning("Git state persistence failed: %s", exc)


def log_trade(asset, direction, m10, status, profit="", order_id=""):
    fields = ["timestamp_lagos", "session_id", "asset", "direction",
              "momentum10", "status", "profit", "order_id", "session_profit"]
    try:
        exists = os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if not exists or os.path.getsize(LOG_FILE) == 0:
                writer.writeheader()
            writer.writerow({
                "timestamp_lagos": now_lagos().isoformat(timespec="seconds"),
                "session_id": SESSION_ID,
                "asset": asset,
                "direction": direction,
                "momentum10": "" if m10 is None else round(float(m10), 5),
                "status": status,
                "profit": profit,
                "order_id": order_id,
                "session_profit": round(float(STATE.get("profit", 0.0)), 2),
            })
    except Exception as exc:
        logging.warning("CSV logging error: %s", exc)


def format_stats():
    return "Session P/L: ${:+.2f}\nWins: {}\nLosses: {}\nResolved trades: {}".format(
        float(STATE.get("profit", 0.0)), int(STATE.get("wins", 0)),
        int(STATE.get("losses", 0)), int(STATE.get("resolved", 0)))


def risk_stop_reason():
    profit = round(float(STATE.get("profit", 0.0)), 2)
    if profit >= SESSION_PROFIT_TARGET:
        return "Session take-profit reached (+${:.2f})".format(SESSION_PROFIT_TARGET)
    if profit <= -SESSION_LOSS_LIMIT:
        return "Session loss limit reached (-${:.2f})".format(SESSION_LOSS_LIMIT)
    return None


def request_next_workflow():
    global NEXT_WORKFLOW_REQUESTED
    if NEXT_WORKFLOW_REQUESTED:
        return
    NEXT_WORKFLOW_REQUESTED = True
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.error("Cannot dispatch next workflow: GITHUB_TOKEN/repository missing")
        send_telegram("<b>Next workflow was not triggered.</b>\nCheck GITHUB_TOKEN permissions.")
        return
    url = "https://api.github.com/repos/{}/actions/workflows/{}/dispatches".format(
        GITHUB_REPOSITORY, GITHUB_WORKFLOW_FILE)
    headers = {"Authorization": "Bearer " + GITHUB_TOKEN,
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    for attempt in range(1, 4):
        try:
            response = requests.post(
                url, headers=headers,
                json={"ref": GITHUB_REF_NAME},
                timeout=15,
            )
            if response.status_code in (201, 204):
                logging.info("Next workflow dispatch accepted.")
                send_telegram("<b>Next workflow queued</b>\nThe scanner will continue in a fresh session.")
                return
            logging.warning("Workflow dispatch attempt %s failed: %s %s",
                            attempt, response.status_code, response.text[:300])
        except Exception as exc:
            logging.warning("Workflow dispatch attempt %s error: %s", attempt, exc)
        time.sleep(3)
    send_telegram("<b>WARNING:</b> automatic next workflow dispatch failed. Check GitHub Actions permissions.")


def stop_if_limit_reached():
    reason = risk_stop_reason()
    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set()
        STATE["stop_reason"] = reason
        save_state(persist=True)
        message = "<b>SCANNER SESSION FINISHED</b>\n{}\n{}".format(reason, format_stats())
        logging.warning(message.replace("<b>", "").replace("</b>", ""))
        send_telegram(message)
    return bool(reason)


def normalize_forex_name(value):
    if not isinstance(value, str):
        return None, False
    raw = value.strip().upper().split(".")[-1]
    if not raw:
        return None, False
    is_otc = "OTC" in raw
    letters = re.sub(r"[^A-Z]", "", raw)
    for suffix in ("OTC", "BINARY", "TURBO", "DIGITAL"):
        if letters.endswith(suffix):
            letters = letters[:-len(suffix)]
    if len(letters) < 6:
        return None, False
    pair = letters[-6:]
    if pair[:3] not in CURRENCIES or pair[3:] not in CURRENCIES:
        return None, False
    return pair, is_otc


def add_asset(found, name, active_id=None):
    pair, is_otc = normalize_forex_name(name)

    # NORMAL FOREX ONLY: never add OTC markets to the scanner.
    if not pair or is_otc:
        return

    clean_name = pair
    resolved_id = None
    for candidate in (
        active_id,
        getattr(OP_code, "ACTIVES", {}).get(clean_name),
    ):
        try:
            if candidate is not None:
                resolved_id = int(candidate)
                break
        except (TypeError, ValueError, OverflowError):
            pass

    found[clean_name] = {
        "name": clean_name,
        "pair": pair,
        "otc": False,
        "id": resolved_id,
    }
    if resolved_id is not None:
        try:
            OP_code.ACTIVES[clean_name] = resolved_id
        except Exception:
            pass


def walk_asset_tree(node, found, parent_key=None):
    if isinstance(node, dict):
        name = node.get("name") or node.get("active_name") or node.get("symbol")
        active_id = node.get("id") or node.get("active_id") or node.get("activeId")
        if name:
            add_asset(found, str(name), active_id if active_id is not None else parent_key)
        elif isinstance(parent_key, str):
            add_asset(found, parent_key, active_id)
        for key, value in node.items():
            walk_asset_tree(value, found, key)
    elif isinstance(node, list):
        for item in node:
            walk_asset_tree(item, found, parent_key)


def refresh_assets(force=False):
    global ASSETS, ASSET_REFRESH_AT
    if ASSETS and not force and time.time() - ASSET_REFRESH_AT < ASSET_REFRESH_SECONDS:
        return ASSETS

    found = {}
    try:
        active_map = IQ.get_all_ACTIVES_OPCODE()
        if isinstance(active_map, dict):
            for name, active_id in active_map.items():
                add_asset(found, str(name), active_id)
    except Exception as exc:
        logging.warning("Active-code asset discovery failed: %s", exc)

    for method_name in ("get_all_init_v2", "get_all_init"):
        try:
            method = getattr(IQ, method_name, None)
            if callable(method):
                walk_asset_tree(method(), found)
        except Exception as exc:
            logging.warning("%s asset discovery failed: %s", method_name, exc)

    # Final safety filter: only standard forex pairs may enter ASSETS.
    assets = [
        item for item in found.values()
        if not item.get("otc", True)
        and "OTC" not in item.get("name", "").upper()
        and len(item.get("pair", "")) == 6
    ]
    assets.sort(key=lambda item: item["name"])

    if assets:
        ASSETS = assets
        ASSET_REFRESH_AT = time.time()
        logging.info("NORMAL FOREX ONLY: %s pairs loaded; OTC excluded", len(ASSETS))
        send_telegram(
            "<b>Normal forex assets refreshed</b>\n"
            "Pairs: {}\nOTC: disabled".format(len(ASSETS))
        )
    else:
        ASSETS = []
        logging.warning("No normal forex assets discovered from IQ Option data.")
    return ASSETS



def disable_digital_open_worker(self):
    logging.info("Disabled unused IQ Option digital-market watcher")


def safe_get_candles(self, active, interval, count, endtime):
    try:
        active_code = getattr(OP_code, "ACTIVES", {}).get(active)
        if active_code is None:
            return None
        self.api.candles.candles_data = None
        self.api.getcandles(active_code, interval, count, endtime)
        deadline = time.time() + CANDLE_REQUEST_TIMEOUT
        while time.time() < deadline:
            data = getattr(self.api.candles, "candles_data", None)
            if data is not None:
                return data
            try:
                if not self.check_connect():
                    return None
            except Exception:
                return None
            time.sleep(0.1)
    except Exception as exc:
        logging.warning("Candle request error for %s: %s", active, exc)
    return None


try:
    if hasattr(IQ_Option, "_IQ_Option__get_digital_open"):
        IQ_Option._IQ_Option__get_digital_open = disable_digital_open_worker
    IQ_Option.get_candles = safe_get_candles
except Exception as exc:
    logging.warning("IQ Option candle safety patch failed: %s", exc)


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD GitHub secret")
    client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    last_error = "unknown error"
    for attempt in range(1, 6):
        try:
            result = client.connect()
            ok = result[0] if isinstance(result, tuple) else bool(result)
            if not ok:
                last_error = str(result[1] if isinstance(result, tuple) and len(result) > 1 else result)
                raise ConnectionError(last_error)
            client.change_balance(BALANCE_MODE)
            time.sleep(1)
            if not client.check_connect():
                raise ConnectionError("Connection dropped after login")
            IQ = client
            logging.info("Connected to IQ Option in PRACTICE mode")
            send_telegram("<b>Scanner connected</b>\nMode: PRACTICE\nStake: ${:.2f}\nExpiry: {} minute\nTarget: +${:.2f}\nLoss limit: -${:.2f}".format(
                STAKE, EXPIRY_MINUTES, SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT))
            return
        except Exception as exc:
            last_error = str(exc)
            logging.warning("Connection attempt %s/5 failed: %s", attempt, last_error)
            if attempt < 5:
                time.sleep(RECONNECT_SECONDS)
    raise RuntimeError("Could not connect to IQ Option: " + last_error)


def momentum_series(closes, period):
    values = []
    for index in range(period, len(closes)):
        old = closes[index - period]
        current = closes[index]
        values.append(0.0 if old <= 0 else ((current / old) - 1.0) * 100.0)
    return values


def percentile_value(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int((len(ordered) - 1) * fraction)
    return ordered[max(0, min(len(ordered) - 1, index))]


def get_closed_candles(asset):
    try:
        raw = IQ.get_candles(asset, CANDLE_SECONDS, CANDLE_REQUEST_COUNT, int(time.time()))
    except Exception as exc:
        logging.debug("Candle fetch failed for %s: %s", asset, exc)
        return []
    if not isinstance(raw, list):
        return []
    try:
        raw.sort(key=lambda row: int(row.get("from", 0)))
    except Exception:
        return []
    current_time = int(time.time())
    return [c for c in raw if int(c.get("from", 0)) > 0
            and int(c.get("from", 0)) + CANDLE_SECONDS <= current_time]


def bullish_engulfing(previous, current):
    try:
        po, pc = float(previous["open"]), float(previous["close"])
        co, cc = float(current["open"]), float(current["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return pc < po and cc > co and co <= pc and cc >= po


def bearish_engulfing(previous, current):
    try:
        po, pc = float(previous["open"]), float(previous["close"])
        co, cc = float(current["open"]), float(current["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return pc > po and cc < co and co >= pc and cc <= po


def evaluate_signal(candles):
    minimum = MOMENTUM_LOOKBACK + MOMENTUM_CONFIRM_PERIOD + 2
    if len(candles) < minimum:
        return None
    try:
        closes = [float(row["close"]) for row in candles]
        candle_time = int(candles[-1].get("from", 0))
    except (KeyError, TypeError, ValueError):
        return None
    values10 = momentum_series(closes, MOMENTUM_PERIOD)
    values20 = momentum_series(closes, MOMENTUM_CONFIRM_PERIOD)
    if len(values10) < MOMENTUM_LOOKBACK + 3 or len(values20) < 3:
        return None
    current10, previous10, before10 = values10[-1], values10[-2], values10[-3]
    history10 = values10[-(MOMENTUM_LOOKBACK + 1):-1]
    low_extreme = percentile_value(history10, EXTREME_PERCENTILE)
    high_extreme = percentile_value(history10, 1.0 - EXTREME_PERCENTILE)
    if abs(current10 - previous10) < MIN_TURN_DISTANCE:
        return None
    current20, previous20 = values20[-1], values20[-2]
    rising, falling = current20 > previous20, current20 < previous20
    previous_candle, current_candle = candles[-2], candles[-1]
    turning_up = current10 > previous10 and previous10 <= before10
    turning_down = current10 < previous10 and previous10 >= before10
    if current10 <= low_extreme and turning_up and rising and bullish_engulfing(previous_candle, current_candle):
        return {"direction": "call", "momentum": round(current10, 5),
                "momentum20": round(current20, 5), "candle_time": candle_time,
                "confirmation": "M20 rising + bullish engulfing"}
    if current10 >= high_extreme and turning_down and falling and bearish_engulfing(previous_candle, current_candle):
        return {"direction": "put", "momentum": round(current10, 5),
                "momentum20": round(current20, 5), "candle_time": candle_time,
                "confirmation": "M20 falling + bearish engulfing"}
    return None


def extract_numeric_profit(value):
    # Only parse numeric values from known profit fields. Do not treat order IDs,
    # stake amounts, or arbitrary numbers in a response as profit.
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "false", "none", "null", "pending", "open",
                    "win", "won", "loss", "lost", "loose", "draw", "unknown"):
            return None
        try:
            number = float(text)
            return number if math.isfinite(number) else None
        except (ValueError, OverflowError):
            return None
    if isinstance(value, dict):
        for key in ("profit", "profit_amount", "profitAmount", "close_profit",
                    "closeProfit", "net_profit", "netProfit", "win_amount", "winAmount"):
            if key in value:
                result = extract_numeric_profit(value.get(key))
                if result is not None:
                    return result
        for key in ("result", "data", "betinfo", "bet_info", "option"):
            nested = value.get(key)
            if isinstance(nested, (dict, list, tuple)):
                result = extract_numeric_profit(nested)
                if result is not None:
                    return result
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                result = extract_numeric_profit(item)
                if result is not None:
                    return result
    return None


def query_order_profit(order_id):
    for name in ("check_win_v4", "check_win", "get_betinfo", "get_betinfo_v2"):
        method = getattr(IQ, name, None)
        if not callable(method):
            continue
        try:
            raw = method(order_id)
        except Exception as exc:
            logging.debug("%s failed for order %s: %s", name, order_id, exc)
            continue
        value = extract_numeric_profit(raw)
        if value is not None:
            logging.info("Order %s resolved through %s: %+.2f", order_id, name, value)
            return value
    return None


def settle_result(asset, direction, broker_profit, m10=None, order_id=""):
    if broker_profit is None:
        raise ValueError("Cannot settle an order without a numeric broker result")
    # Existing user accounting rule: a numeric $0 broker result counts as a full stake loss.
    net_change = -STAKE if float(broker_profit) == 0 else float(broker_profit)
    is_win = net_change > 0
    label = "WIN" if is_win else "LOSS"
    with STATE_LOCK:
        STATE["profit"] = round(float(STATE.get("profit", 0.0)) + net_change, 2)
        STATE["resolved"] = int(STATE.get("resolved", 0)) + 1
        STATE["wins" if is_win else "losses"] = int(STATE.get("wins" if is_win else "losses", 0)) + 1
        STATE["pending_order_id"] = None
        STATE["pending_asset"] = None
        STATE["pending_direction"] = None
        STATE["pending_started"] = None
    save_state(persist=True)
    log_trade(asset, direction, m10, label, round(net_change, 2), order_id)
    logging.info("%s | %s %s | %+.2f | %s", label, asset, direction, net_change, format_stats())
    send_telegram("<b>Trade result: {}</b>\nAsset: {}\nDirection: {}\nBroker result: ${:+.2f}\nRisk-accounted result: ${:+.2f}\n{}".format(
        label, asset, direction.upper(), broker_profit, net_change, format_stats()))
    stop_if_limit_reached()


def wait_for_order_result(order_id, asset, direction, m10):
    last_notice = 0.0
    while not STOP_EVENT.is_set():
        value = query_order_profit(order_id)
        if value is not None:
            settle_result(asset, direction, value, m10, order_id)
            return
        if time.time() - last_notice >= 60:
            logging.warning("Broker result unreadable for order %s; keeping it pending.", order_id)
            last_notice = time.time()
        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def recover_pending_order():
    order_id = STATE.get("pending_order_id")
    if not order_id:
        return
    asset = STATE.get("pending_asset") or "UNKNOWN"
    direction = STATE.get("pending_direction") or "unknown"
    logging.warning("Recovering unresolved order %s (%s %s)", order_id, asset, direction)
    send_telegram("<b>Recovering unresolved order</b>\nOrder: {}\nAsset: {}".format(order_id, asset))
    wait_for_order_result(order_id, asset, direction, None)


def place_trade(asset_info, signal):
    asset = asset_info["name"]
    direction = signal["direction"]
    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached():
            return
        if STATE.get("pending_order_id"):
            logging.warning("Pending order exists; refusing a second trade")
            return
        active_id = asset_info.get("id")
        if active_id is not None:
            try:
                OP_code.ACTIVES[asset] = int(active_id)
            except Exception:
                pass
        try:
            result = IQ.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        except Exception as exc:
            log_trade(asset, direction, signal.get("momentum"), "ORDER_ERROR", "")
            logging.warning("Order placement failed for %s: %s", asset, exc)
            return
        if isinstance(result, tuple):
            success = bool(result[0])
            order_id = result[1] if len(result) > 1 else None
        else:
            success, order_id = bool(result), None
        if not success or order_id is None:
            REJECTED_ASSETS[asset] = time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            log_trade(asset, direction, signal.get("momentum"), "ORDER_REJECTED", "")
            logging.warning("Broker rejected %s order: %r", asset, result)
            return
        STATE["pending_order_id"] = order_id
        STATE["pending_asset"] = asset
        STATE["pending_direction"] = direction
        STATE["pending_started"] = now_lagos().isoformat(timespec="seconds")
        save_state(persist=True)
        log_trade(asset, direction, signal.get("momentum"), "OPENED", "", order_id)
        send_telegram("<b>Trade opened in PRACTICE</b>\nAsset: {}\nDirection: {}\nStake: ${:.2f}\nExpiry: {} minute\nM10: {:.5f}\nM20: {:.5f}\n{}\n{}".format(
            asset, direction.upper(), STAKE, EXPIRY_MINUTES, signal["momentum"],
            signal["momentum20"], signal["confirmation"], format_stats()))
        logging.info("Opened order %s on %s %s", order_id, asset, direction.upper())
    wait_for_order_result(order_id, asset, direction, signal.get("momentum"))


def heartbeat(last_time):
    if time.time() - last_time < HEARTBEAT_SECONDS:
        return last_time
    message = "<b>Momentum scanner heartbeat</b>\n{}\nForex assets: {}\n{}".format(
        now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT"), len(ASSETS), format_stats())
    send_telegram(message)
    logging.info(message.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
    return time.time()


def main():
    connect_iq()
    refresh_assets(force=True)
    if not ASSETS:
        raise RuntimeError("No forex assets found from IQ Option data")
    if stop_if_limit_reached():
        request_next_workflow()
        return
    if STATE.get("pending_order_id"):
        recover_pending_order()
    if STOP_EVENT.is_set() or stop_if_limit_reached():
        if risk_stop_reason():
            request_next_workflow()
        return
    send_telegram("<b>Momentum scanner started</b>\nPractice only\nEntry: M10 extreme + turn\nConfirmations: M20 trend + closed engulfing candle\nTarget: +${:.2f} | Loss limit: -${:.2f}\nStake: ${:.2f}\n{}".format(
        SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT, STAKE, format_stats()))
    last_heartbeat = time.time()
    last_reconnect_notice = 0.0

    while not STOP_EVENT.is_set():
        if stop_if_limit_reached():
            break
        try:
            if not IQ.check_connect():
                raise ConnectionError("IQ Option connection lost")
        except Exception:
            if time.time() - last_reconnect_notice >= RECONNECT_SECONDS:
                logging.warning("IQ Option connection lost; reconnecting")
                last_reconnect_notice = time.time()
            try:
                response = IQ.connect()
                connected = response[0] if isinstance(response, tuple) else bool(response)
                if not connected:
                    raise ConnectionError("reconnection rejected")
                IQ.change_balance(BALANCE_MODE)
                time.sleep(2)
                if not IQ.check_connect():
                    raise ConnectionError("reconnected session not ready")
                refresh_assets(force=True)
            except Exception as exc:
                logging.warning("Reconnect failed: %s", exc)
                time.sleep(RECONNECT_SECONDS)
                continue

        if time.time() - ASSET_REFRESH_AT >= ASSET_REFRESH_SECONDS:
            refresh_assets(force=True)
        last_heartbeat = heartbeat(last_heartbeat)
        for item in list(ASSETS):
            if STOP_EVENT.is_set() or stop_if_limit_reached():
                break
            try:
                asset = item["name"]
                cooldown_until = REJECTED_ASSETS.get(asset, 0.0)
                if time.time() < cooldown_until:
                    continue
                if cooldown_until:
                    REJECTED_ASSETS.pop(asset, None)
                signal = evaluate_signal(get_closed_candles(asset))
                if signal is None:
                    continue
                key = str(signal.get("candle_time", ""))
                last_signals = STATE.setdefault("last_signals", {})
                if last_signals.get(asset) == key:
                    continue
                last_signals[asset] = key
                save_state()
                log_trade(asset, signal["direction"], signal["momentum"], "SIGNAL", "")
                logging.info("CONFIRMED SIGNAL %s %s M10=%.5f M20=%.5f %s",
                             asset, signal["direction"].upper(), signal["momentum"],
                             signal["momentum20"], signal["confirmation"])
                send_telegram("<b>Confirmed setup</b>\nAsset: {}\nDirection: {}\nM10: {:.5f}\nM20: {:.5f}\n{}".format(
                    asset, signal["direction"].upper(), signal["momentum"],
                    signal["momentum20"], signal["confirmation"]))
                if AUTO_TRADE:
                    place_trade(item, signal)
            except Exception as exc:
                logging.warning("Scan error for %s: %s", item.get("name", "UNKNOWN"), exc)
            time.sleep(0.15)
        time.sleep(SCAN_INTERVAL)

    reason = STATE.get("stop_reason") or risk_stop_reason() or "Scanner stopped"
    logging.info("Scanner finished: %s | %s", reason, format_stats())
    send_telegram("<b>Scanner session finished</b>\n{}\n{}".format(reason, format_stats()))
    if risk_stop_reason():
        request_next_workflow()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state(persist=True)
        logging.info("Scanner stopped by keyboard interrupt")
    except Exception as exc:
        logging.exception("Fatal scanner error")
        send_telegram("<b>Scanner error</b>\n{}".format(exc))
        save_state(persist=True)
        raise
