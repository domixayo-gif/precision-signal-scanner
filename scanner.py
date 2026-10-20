import os
import re
import csv
import json
import time
import math
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 + TWO CONFIRMATIONS (M20 TREND AND ENGULFING CANDLE)
# PRACTICE ONLY. Do not change BALANCE_MODE to REAL.

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BALANCE_MODE = "PRACTICE"
AUTO_TRADE = True
STAKE = 1.0
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60

MOMENTUM_PERIOD = 10
MOMENTUM_CONFIRM_PERIOD = 20
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06

SESSION_PROFIT_TARGET = 5.0
SESSION_LOSS_LIMIT = 3.0

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

if os.getenv("GITHUB_RUN_ID"):
    SESSION_ID = str(os.getenv("GITHUB_RUN_ID")) + "-" + str(
        os.getenv("GITHUB_RUN_ATTEMPT", "1")
    )
else:
    SESSION_ID = str(int(time.time() * 1000))

IQ = None
ASSETS = []
ASSET_REFRESH_AT = 0.0
REJECTED_ASSETS = {}
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
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
    saved = {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            saved = loaded
    except (OSError, ValueError, TypeError):
        saved = {}

    if saved.get("session_id") == SESSION_ID and saved.get("date") == current["date"]:
        current.update(saved)
    else:
        # New run: reset only this run's risk totals, but preserve unresolved
        # order details so the bot never opens a second trade while one is pending.
        current["pending_order_id"] = saved.get("pending_order_id")
        current["pending_asset"] = saved.get("pending_asset")
        current["pending_direction"] = saved.get("pending_direction")
        current["pending_started"] = saved.get("pending_started")
        old_signals = saved.get("last_signals", {})
        current["last_signals"] = old_signals if isinstance(old_signals, dict) else {}
    if not isinstance(current.get("last_signals"), dict):
        current["last_signals"] = {}
    return current


STATE = load_state()


def save_state():
    try:
        with STATE_LOCK:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(STATE, handle, indent=2)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError as exc:
        logging.warning("Could not save state: %s", exc)


def log_trade(asset, direction, m10, status, profit="", order_id=""):
    fields = [
        "timestamp_lagos", "session_id", "asset", "direction",
        "momentum10", "status", "profit", "order_id", "session_profit"
    ]
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
    return (
        "Session P/L: ${:+.2f}\nWins: {}\nLosses: {}\nResolved trades: {}"
    ).format(
        float(STATE.get("profit", 0.0)),
        int(STATE.get("wins", 0)),
        int(STATE.get("losses", 0)),
        int(STATE.get("resolved", 0)),
    )


def risk_stop_reason():
    """Return a stop reason whenever either monetary session limit is reached."""
    profit = round(float(STATE.get("profit", 0.0)), 2)
    if profit >= SESSION_PROFIT_TARGET:
        return "Session profit target reached (+${:.2f})".format(SESSION_PROFIT_TARGET)
    if profit <= -SESSION_LOSS_LIMIT:
        return "Session loss limit reached (-${:.2f})".format(SESSION_LOSS_LIMIT)
    return None


def stop_if_limit_reached():
    reason = risk_stop_reason()
    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set()
        STATE["stop_reason"] = reason
        save_state()
        message = "<b>SCANNER STOPPED</b>\n{}\n{}".format(reason, format_stats())
        logging.warning(message.replace("<b>", "").replace("</b>", ""))
        send_telegram(message)
    return bool(reason)


def normalize_name(value):
    return "".join(char for char in str(value).upper() if char.isalnum())


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
    if not pair:
        return
    clean_name = pair + ("-OTC" if is_otc else "")
    resolved_id = None
    for candidate in (active_id, getattr(OP_code, "ACTIVES", {}).get(clean_name)):
        try:
            if candidate is not None:
                resolved_id = int(candidate)
                break
        except (TypeError, ValueError, OverflowError):
            continue
    found[clean_name] = {
        "name": clean_name,
        "pair": pair,
        "otc": is_otc,
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
    assets = list(found.values())
    assets.sort(key=lambda item: (item["otc"], item["name"]))
    if assets:
        ASSETS = assets
        ASSET_REFRESH_AT = time.time()
        normal_count = sum(1 for item in assets if not item["otc"])
        otc_count = sum(1 for item in assets if item["otc"])
        logging.info("Forex assets: %s total; %s normal; %s OTC", len(assets), normal_count, otc_count)
        send_telegram(
            "<b>Forex assets refreshed</b>\nTotal: {}\nNormal: {}\nOTC: {}".format(
                len(assets), normal_count, otc_count
            )
        )
    else:
        logging.warning("No forex assets discovered from IQ Option data.")
    return ASSETS


def disable_digital_open_worker(self):
    logging.info("Disabled unused IQ Option digital-market watcher")


def safe_get_candles(self, active, interval, count, endtime):
    try:
        active_code = getattr(OP_code, "ACTIVES", {}).get(active)
        if active_code is None:
            logging.debug("No IQ Option active code for %s", active)
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
        logging.warning("Candle request timed out for %s", active)
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
    last_error = "unknown error"
    client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
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
            send_telegram(
                "<b>Scanner connected</b>\nMode: PRACTICE\nStake: ${:.2f}\nExpiry: {} minute\n"
                "Target: +${:.2f}\nStop loss: -${:.2f}\n"
                "Confirmations: M20 trend + engulfing candle".format(
                    STAKE, EXPIRY_MINUTES, SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT
                )
            )
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
        if old <= 0:
            values.append(0.0)
        else:
            values.append(((current / old) - 1.0) * 100.0)
    return values


def percentile_value(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int((len(ordered) - 1) * fraction)
    index = max(0, min(len(ordered) - 1, index))
    return ordered[index]


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
    closed = []
    for candle in raw:
        try:
            start = int(candle.get("from", 0))
            if start > 0 and start + CANDLE_SECONDS <= current_time:
                closed.append(candle)
        except (TypeError, ValueError):
            continue
    return closed


def bullish_engulfing(previous, current):
    try:
        po = float(previous["open"])
        pc = float(previous["close"])
        co = float(current["open"])
        cc = float(current["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return pc < po and cc > co and co <= pc and cc >= po


def bearish_engulfing(previous, current):
    try:
        po = float(previous["open"])
        pc = float(previous["close"])
        co = float(current["open"])
        cc = float(current["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return pc > po and cc < co and co >= pc and cc <= po


def evaluate_signal(candles):
    """M10 extreme + turn, confirmed by M20 direction and a closed engulfing candle."""
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

    current10 = values10[-1]
    previous10 = values10[-2]
    before_previous10 = values10[-3]
    history10 = values10[-(MOMENTUM_LOOKBACK + 1):-1]
    low_extreme = percentile_value(history10, EXTREME_PERCENTILE)
    high_extreme = percentile_value(history10, 1.0 - EXTREME_PERCENTILE)
    turn_size = abs(current10 - previous10)
    if turn_size < MIN_TURN_DISTANCE:
        return None

    current20 = values20[-1]
    previous20 = values20[-2]
    m20_rising = current20 > previous20
    m20_falling = current20 < previous20
    previous_candle = candles[-2]
    current_candle = candles[-1]

    turning_up = current10 > previous10 and previous10 <= before_previous10
    turning_down = current10 < previous10 and previous10 >= before_previous10

    if current10 <= low_extreme and turning_up and m20_rising:
        if bullish_engulfing(previous_candle, current_candle):
            return {
                "direction": "call",
                "momentum": round(current10, 5),
                "previous": round(previous10, 5),
                "momentum20": round(current20, 5),
                "previous20": round(previous20, 5),
                "extreme": round(low_extreme, 5),
                "candle_time": candle_time,
                "confirmation": "M20 rising + bullish engulfing",
            }

    if current10 >= high_extreme and turning_down and m20_falling:
        if bearish_engulfing(previous_candle, current_candle):
            return {
                "direction": "put",
                "momentum": round(current10, 5),
                "previous": round(previous10, 5),
                "momentum20": round(current20, 5),
                "previous20": round(previous20, 5),
                "extreme": round(high_extreme, 5),
                "candle_time": candle_time,
                "confirmation": "M20 falling + bearish engulfing",
            }
    return None


def extract_numeric_profit(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {
            "false", "none", "null", "pending", "open", "win", "won",
            "loss", "lost", "loose", "draw", "unknown"
        }:
            return None
        try:
            number = float(text)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, dict):
        for key in (
            "profit", "profit_amount", "profitAmount", "close_profit",
            "closeProfit", "net_profit", "netProfit", "win_amount", "winAmount"
        ):
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
        if isinstance(value.get("result"), (int, float, str)):
            return extract_numeric_profit(value.get("result"))
    if isinstance(value, (list, tuple)):
        for item in value:
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

    # User's accounting rule: a $0 result on a $1 binary trade counts as -$1.
    if broker_profit == 0:
        net_change = -STAKE
    else:
        net_change = float(broker_profit)
    is_win = net_change > 0
    label = "WIN" if is_win else "LOSS"

    with STATE_LOCK:
        STATE["profit"] = round(float(STATE.get("profit", 0.0)) + net_change, 2)
        STATE["resolved"] = int(STATE.get("resolved", 0)) + 1
        if is_win:
            STATE["wins"] = int(STATE.get("wins", 0)) + 1
        else:
            STATE["losses"] = int(STATE.get("losses", 0)) + 1
        STATE["pending_order_id"] = None
        STATE["pending_asset"] = None
        STATE["pending_direction"] = None
        STATE["pending_started"] = None
    save_state()
    log_trade(asset, direction, m10, label, round(net_change, 2), order_id)
    logging.info("%s | %s %s | %+0.2f | %s", label, asset, direction, net_change, format_stats())
    send_telegram(
        "<b>Trade result: {}</b>\nAsset: {}\nDirection: {}\nBroker result: ${:+.2f}\n"
        "Risk-accounted result: ${:+.2f}\n{}".format(
            label, asset, direction.upper(), broker_profit, net_change, format_stats()
        )
    )
    stop_if_limit_reached()


def wait_for_order_result(order_id, asset, direction, m10):
    last_notice = 0.0
    while not STOP_EVENT.is_set():
        value = query_order_profit(order_id)
        if value is not None:
            settle_result(asset, direction, value, m10, order_id)
            return
        now = time.time()
        if now - last_notice >= 60:
            logging.warning(
                "Broker result for order %s is unreadable. Keeping order pending; no new trade.",
                order_id,
            )
            last_notice = now
        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def recover_pending_order():
    order_id = STATE.get("pending_order_id")
    if not order_id:
        return
    asset = STATE.get("pending_asset") or "UNKNOWN"
    direction = STATE.get("pending_direction") or "unknown"
    logging.warning("Recovering unresolved order %s (%s %s)", order_id, asset, direction)
    send_telegram(
        "<b>Recovering unresolved order</b>\nOrder: {}\nAsset: {}\n"
        "The scanner will not place another trade until this order is resolved.".format(
            order_id, asset
        )
    )
    wait_for_order_result(order_id, asset, direction, None)


def place_trade(asset_info, signal):
    asset = asset_info["name"]
    direction = signal["direction"]
    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached():
            return
        if STATE.get("pending_order_id"):
            logging.warning("A pending order exists; refusing to place a second order")
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
            success = bool(result)
            order_id = None
        if not success or order_id is None:
            REJECTED_ASSETS[asset] = time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            log_trade(asset, direction, signal.get("momentum"), "ORDER_REJECTED", "")
            logging.warning(
                "Broker rejected order on %s; cooling this asset down for %s seconds: %r",
                asset, REJECTED_ASSET_COOLDOWN_SECONDS, result
            )
            send_telegram(
                "<b>Order rejected</b>\nAsset: {}\nBroker response: {}\n"
                "Skipping this asset for {} minutes. No trade loss recorded.".format(
                    asset, result, int(REJECTED_ASSET_COOLDOWN_SECONDS / 60)
                )
            )
            return
        STATE["pending_order_id"] = order_id
        STATE["pending_asset"] = asset
        STATE["pending_direction"] = direction
        STATE["pending_started"] = now_lagos().isoformat(timespec="seconds")
        save_state()
        log_trade(asset, direction, signal.get("momentum"), "OPENED", "", order_id)
        send_telegram(
            "<b>Trade opened in PRACTICE</b>\nAsset: {}\nDirection: {}\nStake: ${:.2f}\n"
            "Expiry: {} minute\nM10: {:.5f}\nM20: {:.5f}\nConfirmation: {}\n{}".format(
                asset, direction.upper(), STAKE, EXPIRY_MINUTES,
                signal["momentum"], signal["momentum20"], signal["confirmation"], format_stats()
            )
        )
        logging.info("Opened order %s on %s %s", order_id, asset, direction.upper())
    wait_for_order_result(order_id, asset, direction, signal.get("momentum"))


def heartbeat(last_time):
    if time.time() - last_time < HEARTBEAT_SECONDS:
        return last_time
    message = "<b>Momentum 10 scanner heartbeat</b>\n{}\nForex assets: {}\n{}".format(
        now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT"), len(ASSETS), format_stats()
    )
    send_telegram(message)
    logging.info(message.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
    return time.time()


def main():
    connect_iq()
    refresh_assets(force=True)
    if not ASSETS:
        raise RuntimeError("No forex assets found in IQ Option active data")

    if stop_if_limit_reached():
        return
    if STATE.get("pending_order_id"):
        recover_pending_order()
    if STOP_EVENT.is_set() or stop_if_limit_reached():
        return

    send_telegram(
        "<b>Momentum 10 scanner started</b>\nMode: PRACTICE\n"
        "Entry: M10 extreme + confirmed turn\n"
        "Confirmation 1: M20 momentum trend\n"
        "Confirmation 2: fully closed engulfing candle\n"
        "Target: +${:.2f} | Stop loss: -${:.2f}\n{}".format(
            SESSION_PROFIT_TARGET, SESSION_LOSS_LIMIT, format_stats()
        )
    )
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
                logging.warning("IQ Option connection lost; attempting reconnection")
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
                asset_name = item["name"]
                cooldown_until = REJECTED_ASSETS.get(asset_name, 0.0)
                if time.time() < cooldown_until:
                    continue
                if cooldown_until:
                    REJECTED_ASSETS.pop(asset_name, None)
                candles = get_closed_candles(asset_name)
                signal = evaluate_signal(candles)
                if signal is None:
                    continue
                last_key = str(signal.get("candle_time", ""))
                last_signals = STATE.setdefault("last_signals", {})
                if last_signals.get(item["name"]) == last_key:
                    continue
                last_signals[item["name"]] = last_key
                save_state()
                log_trade(item["name"], signal["direction"], signal["momentum"], "SIGNAL", "")
                logging.info(
                    "CONFIRMED SIGNAL %s %s | M10=%.5f | M20=%.5f | %s",
                    item["name"], signal["direction"].upper(), signal["momentum"],
                    signal["momentum20"], signal["confirmation"]
                )
                send_telegram(
                    "<b>Confirmed setup</b>\nAsset: {}\nDirection: {}\n"
                    "M10: {:.5f}\nM20: {:.5f}\nConfirmations: {}\n"
                    "Engulfing candle: confirmed".format(
                        item["name"], signal["direction"].upper(), signal["momentum"],
                        signal["momentum20"], signal["confirmation"]
                    )
                )
                if AUTO_TRADE:
                    place_trade(item, signal)
            except Exception as exc:
                logging.warning("Scan error for %s: %s", item.get("name", "UNKNOWN"), exc)
            time.sleep(0.15)
        time.sleep(SCAN_INTERVAL)

    reason = STATE.get("stop_reason") or risk_stop_reason() or "Scanner stopped"
    logging.info("Scanner finished: %s | %s", reason, format_stats())
    send_telegram("<b>Scanner finished</b>\n{}\n{}".format(reason, format_stats()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state()
        logging.info("Scanner stopped by keyboard interrupt")
    except Exception as exc:
        logging.exception("Fatal scanner error")
        send_telegram("<b>Scanner error</b>\n{}".format(exc))
        save_state()
        raise
