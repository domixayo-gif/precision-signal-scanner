import os
import csv
import json
import time
import threading
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option


# MOMENTUM 10 CONTROLLED PRACTICE SCANNER
# Momentum 10 selects the entry; Momentum 20 confirms the turn.

# Discover up to 70 OTC assets from live IQ Option binary/turbo broker data.
# Normal market currency pairs are intentionally excluded.
WATCHLIST = []
MAX_OTC_ASSETS = 70
ASSET_MIN_TRADES_FOR_RANK = 5

MOMENTUM_PERIOD = 10
MOMENTUM_CONFIRM_PERIOD = 20
MOMENTUM_LOOKBACK = 50
EXTREME_PERCENTILE = 0.10
MIN_TURN_DISTANCE = 0.03

CANDLE_SECONDS = 60
EXPIRY_MINUTES = 1
AUTO_TRADE = True
BALANCE_MODE = "PRACTICE"
STAKE = 1.0

SESSION_PROFIT_TARGET = 15.0
SESSION_LOSS_LIMIT = 5.0
MAX_LOSING_TRADES = 5
MAX_RESOLVED_TRADES = 50

SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 30
CANDLE_REQUEST_TIMEOUT = 10
RESULT_RETRY_SECONDS = 15
RESULT_LOOKUP_TIMEOUT = 8
DIAGNOSTIC_INTERVAL = 60

MIN_CANDLES_FOR_SIGNAL = MOMENTUM_LOOKBACK + MOMENTUM_PERIOD + 2
CANDLE_REQUEST_COUNT = MIN_CANDLES_FOR_SIGNAL + 8

LOG_FILE = "momentum_signal_log.csv"
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

if os.getenv("GITHUB_RUN_ID"):
    SESSION_ID = (
        str(os.getenv("GITHUB_RUN_ID"))
        + "-"
        + str(os.getenv("GITHUB_RUN_ATTEMPT", "1"))
    )
else:
    SESSION_ID = str(int(time.time() * 1000))

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

api = None
asset_ids = {}
last_asset_refresh = 0.0
last_heartbeat = 0.0
candle_worker = None
last_pair_diagnostic = {}

stop_event = threading.Event()
state_lock = threading.RLock()
trade_lock = threading.Lock()
connection_lock = threading.Lock()
git_lock = threading.Lock()
result_lookup_lock = threading.Lock()
result_lookup_workers = {}

state = {
    "date": "",
    "session_id": "",
    "net_profit": 0.0,
    "wins": 0,
    "losses": 0,
    "draws": 0,
    "unknown": 0,
    "resolved": 0,
    "last_signal_candle": {},
    "active_order": None,
    "stop_reason": "",
    # Historical totals persist across separate manual workflow runs.
    "asset_stats": {},
}


def today_string():
    return datetime.now(LAGOS).strftime("%Y-%m-%d")


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        response = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        if response.status_code != 200:
            print("Telegram error:", response.status_code, flush=True)
    except Exception as exc:
        print("Telegram error:", exc, flush=True)


def persist_state_to_git():
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return

    with git_lock:
        try:
            added = subprocess.run(
                ["git", "add", "-f", STATE_FILE],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if added.returncode != 0:
                print("State git-add warning:", added.stderr.strip(), flush=True)
                return

            changed = subprocess.run(
                ["git", "diff", "--cached", "--quiet", "--", STATE_FILE],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if changed.returncode == 0:
                return
            if changed.returncode != 1:
                print("State git-diff warning:", changed.stderr.strip(), flush=True)
                return

            subprocess.run(
                ["git", "config", "user.name", "github-actions[bot]"],
                capture_output=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "git", "config", "user.email",
                    "41898282+github-actions[bot]@users.noreply.github.com",
                ],
                capture_output=True,
                timeout=10,
            )

            committed = subprocess.run(
                ["git", "commit", "-m", "Save Momentum scanner state"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if committed.returncode != 0:
                print("State commit warning:", committed.stderr.strip(), flush=True)
                return

            branch = os.getenv("GITHUB_REF_NAME", "main")
            pushed = subprocess.run(
                ["git", "push", "origin", "HEAD:" + branch],
                capture_output=True,
                text=True,
                timeout=25,
            )
            if pushed.returncode != 0:
                print("STATE PUSH FAILED:", pushed.stderr.strip(), flush=True)
        except Exception as exc:
            print("State persistence warning:", exc, flush=True)


def save_state():
    try:
        with state_lock:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as file:
                json.dump(state, file, indent=2)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
        persist_state_to_git()
    except Exception as exc:
        print("State save error:", exc, flush=True)


def reset_session_counters():
    state["date"] = today_string()
    state["session_id"] = SESSION_ID
    state["net_profit"] = 0.0
    state["wins"] = 0
    state["losses"] = 0
    state["draws"] = 0
    state["unknown"] = 0
    state["resolved"] = 0
    state["stop_reason"] = ""


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                for key in state:
                    if key in saved:
                        state[key] = saved[key]
        except Exception as exc:
            raise RuntimeError(
                "Cannot safely read momentum_state.json. Check the file."
            ) from exc

    if not isinstance(state.get("last_signal_candle"), dict):
        state["last_signal_candle"] = {}

    if not isinstance(state.get("active_order"), (dict, type(None))):
        state["active_order"] = None
    if not isinstance(state.get("asset_stats"), dict):
        state["asset_stats"] = {}

    # Risk totals reset for each manual workflow run; saved orders and asset
    # performance history are retained across runs.
    if (
        state.get("session_id") != SESSION_ID
        or state.get("date") != today_string()
    ):
        reset_session_counters()

    save_state()


def apply_verified_order_recovery():
    """Apply the user-verified result for one exact saved broker order once."""
    verified = {
        14361222265: {
            "asset": "ARBUSD-OTC",
            "direction": "put",
            "result": "WIN",
            "net_profit": 0.82,
            "opening_price": 0.145885,
            "closing_price": 0.145755,
            "trade_date": "2026-10-16",
            "trade_session_id": "38909478425-1",
        }
    }

    with state_lock:
        order = state.get("active_order")
        if not isinstance(order, dict):
            return False

        try:
            order_id = int(order.get("order_id"))
        except (TypeError, ValueError, OverflowError):
            return False

        result = verified.get(order_id)
        if result is None:
            return False

        if str(order.get("asset", "")) != result["asset"]:
            print("Verified recovery refused: asset does not match order ID.", flush=True)
            return False
        if str(order.get("direction", "")).lower() != result["direction"]:
            print("Verified recovery refused: direction does not match order ID.", flush=True)
            return False

        # The order belongs to an earlier run. Update persistent asset history,
        # but do not add it to this run's profit target or trade counters.
        stats_all = state.get("asset_stats", {})
        if not isinstance(stats_all, dict):
            stats_all = {}
            state["asset_stats"] = stats_all
        asset_stats = stats_all.get(result["asset"])
        if not isinstance(asset_stats, dict):
            asset_stats = {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "net_profit": 0.0,
                "last_result": "",
                "last_updated": "",
            }
            stats_all[result["asset"]] = asset_stats

        asset_stats["trades"] = int(asset_stats.get("trades", 0)) + 1
        asset_stats["wins"] = int(asset_stats.get("wins", 0)) + 1
        asset_stats["net_profit"] = round(
            float(asset_stats.get("net_profit", 0.0)) + result["net_profit"], 2
        )
        asset_stats["last_result"] = "WIN"
        asset_stats["last_updated"] = datetime.now(LAGOS).isoformat()
        state["active_order"] = None
        session_net = round(float(state.get("net_profit", 0.0)), 2)

    # Save the cleared order first. If the workflow restarts, this exact result
    # cannot be applied twice because active_order will already be null.
    save_state()
    log_trade({
        "time": datetime.now(LAGOS).isoformat(),
        "date": result["trade_date"],
        "session_id": result["trade_session_id"],
        "asset": result["asset"],
        "direction": "PUT",
        "momentum": order.get("momentum", ""),
        "momentum20": order.get("momentum20", ""),
        "candle_time": order.get("candle_time", ""),
        "order_id": order_id,
        "result": "WIN",
        "profit": result["net_profit"],
        "session_net": session_net,
    })
    print(
        "VERIFIED RECOVERY: order " + str(order_id)
        + " recorded as WIN, net +$0.82. Active order cleared."
        + " Prior-run result excluded from this run's risk totals.",
        flush=True,
    )
    telegram(
        "VERIFIED PRIOR-RUN RESULT RECOVERED\n"
        "Asset: ARBUSD-OTC\nDirection: PUT\nResult: WIN\n"
        "Net profit: +$0.82\nOrder ID: " + str(order_id)
        + "\nPrior-run result excluded from this run's risk totals."
    )
    return True


def log_trade(values):
    fields = [
        "time", "date", "session_id", "asset", "direction",
        "momentum", "momentum20", "candle_time", "order_id",
        "result", "profit", "session_net",
    ]
    try:
        exists = os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            if not exists or os.path.getsize(LOG_FILE) == 0:
                writer.writeheader()
            writer.writerow(values)
    except Exception as exc:
        print("CSV error:", exc, flush=True)


def get_asset_stats_snapshot(asset):
    with state_lock:
        stats_all = state.get("asset_stats", {})
        stats = stats_all.get(asset, {}) if isinstance(stats_all, dict) else {}
        trades = int(stats.get("trades", 0))
        wins = int(stats.get("wins", 0))
        losses = int(stats.get("losses", 0))
        net = round(float(stats.get("net_profit", 0.0)), 2)
    win_rate = round((wins / trades) * 100.0, 1) if trades else 0.0
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "net_profit": net,
        "win_rate": win_rate,
    }


def asset_performance_summary(limit=10):
    with state_lock:
        stats_all = state.get("asset_stats", {})
        if not isinstance(stats_all, dict):
            stats_all = {}
        rows = []
        for asset, stats in stats_all.items():
            if not isinstance(stats, dict):
                continue
            trades = int(stats.get("trades", 0))
            if trades <= 0:
                continue
            wins = int(stats.get("wins", 0))
            losses = int(stats.get("losses", 0))
            net = round(float(stats.get("net_profit", 0.0)), 2)
            win_rate = (wins / trades) * 100.0
            rows.append({
                "asset": asset,
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "net": net,
                "win_rate": win_rate,
            })

    if not rows:
        return "No resolved asset results have been recorded yet."

    qualified = [row for row in rows if row["trades"] >= ASSET_MIN_TRADES_FOR_RANK]
    if qualified:
        rows = sorted(
            qualified,
            key=lambda row: (row["net"], row["net"] / row["trades"], row["win_rate"]),
            reverse=True,
        )[:limit]
        heading = "ASSET RANKING (minimum " + str(ASSET_MIN_TRADES_FOR_RANK) + " trades):"
    else:
        rows = sorted(
            rows,
            key=lambda row: (row["trades"], row["net"], row["win_rate"]),
            reverse=True,
        )[:limit]
        heading = "PROVISIONAL ASSET RESULTS (not enough assets have " + str(
            ASSET_MIN_TRADES_FOR_RANK
        ) + " trades yet):"

    lines = [heading]
    for row in rows:
        lines.append(
            row["asset"] + " | trades=" + str(row["trades"])
            + " | W/L=" + str(row["wins"]) + "/" + str(row["losses"])
            + " | win rate=" + str(round(row["win_rate"], 1)) + "%"
            + " | net=$" + str(row["net"])
        )
    return "\n".join(lines)


def diagnostic(asset, message, force=False):
    now = time.time()
    previous = last_pair_diagnostic.get(asset, 0.0)
    if force or now - previous >= DIAGNOSTIC_INTERVAL:
        print("SCAN DIAGNOSTIC |", asset, "|", message, flush=True)
        last_pair_diagnostic[asset] = now


def normalize_name(value):
    return "".join(char for char in str(value).upper() if char.isalnum())


def as_active_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def find_asset_id(data, target):
    wanted = normalize_name(target)
    id_keys = (
        "active_id", "activeid", "id", "instrument_id",
        "instrumentid", "actives_id",
    )
    name_keys = ("name", "active", "symbol", "ticker", "instrument")

    if isinstance(data, list):
        for item in data:
            found = find_asset_id(item, target)
            if found is not None:
                return found
        return None

    if not isinstance(data, dict):
        return None

    names = []
    for key in name_keys:
        value = data.get(key)
        if isinstance(value, str):
            names.append(value)

    if any(normalize_name(name) == wanted for name in names):
        for key in id_keys:
            found = as_active_id(data.get(key))
            if found is not None:
                return found

    for key, value in data.items():
        if normalize_name(key) == "ACTIVES" and isinstance(value, dict):
            for active_key, info in value.items():
                if not isinstance(info, dict):
                    continue
                found_names = []
                for name_key in name_keys:
                    name_value = info.get(name_key)
                    if isinstance(name_value, str):
                        found_names.append(name_value)
                if any(normalize_name(name) == wanted for name in found_names):
                    for id_key in id_keys:
                        found = as_active_id(info.get(id_key))
                        if found is not None:
                            return found
                    found = as_active_id(active_key)
                    if found is not None:
                        return found

        if normalize_name(key) == wanted and isinstance(value, dict):
            for id_key in id_keys:
                found = as_active_id(value.get(id_key))
                if found is not None:
                    return found

    for value in data.values():
        found = find_asset_id(value, target)
        if found is not None:
            return found

    return None


def otc_name_from_info(info):
    if not isinstance(info, dict):
        return None

    name_keys = ("name", "active", "symbol", "ticker", "instrument")
    for key in name_keys:
        value = info.get(key)
        if not isinstance(value, str):
            continue

        # IQ Option payloads may name assets like "turbo.EURUSD-OTC".
        # Use the final dotted segment and remove separators used in display names.
        parts = value.upper().split(".")
        for part in reversed(parts):
            candidate = part.strip().replace("/", "").replace(" ", "")
            if candidate.endswith("-OTC") and len(candidate) > 4:
                return candidate
    return None


def flag_is_false(value):
    if value is False or value == 0:
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "disabled"):
        return True
    return False


def flag_is_true(value):
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def collect_otc_from_option_data(source, found):
    """Collect only OTC entries exposed under IQ Option binary/turbo options."""
    if isinstance(source, list):
        for item in source:
            collect_otc_from_option_data(item, found)
        return

    if not isinstance(source, dict):
        return

    for key, value in source.items():
        key_name = normalize_name(key)
        if key_name in ("BINARY", "TURBO") and isinstance(value, dict):
            actives = value.get("actives")
            if isinstance(actives, dict):
                for id_key, info in actives.items():
                    if not isinstance(info, dict):
                        continue
                    if "enabled" in info and flag_is_false(info.get("enabled")):
                        continue
                    if flag_is_true(info.get("is_suspended")):
                        continue
                    if flag_is_true(info.get("closed")):
                        continue

                    asset = otc_name_from_info(info)
                    active_id = as_active_id(info.get("active_id"))
                    if active_id is None:
                        active_id = as_active_id(info.get("actives_id"))
                    if active_id is None:
                        active_id = as_active_id(id_key)

                    if asset and active_id is not None:
                        found.setdefault(asset, active_id)

        # Covers both v2 payloads and legacy payloads wrapped in "result".
        collect_otc_from_option_data(value, found)


def refresh_assets(reset=False):
    global asset_ids, last_asset_refresh, WATCHLIST

    if api is None:
        return False

    sources = []
    try:
        sources.append(api.get_all_init_v2())
    except Exception as exc:
        print("Init V2 refresh warning:", exc, flush=True)

    try:
        sources.append(api.get_all_init())
    except Exception as exc:
        print("Legacy init refresh warning:", exc, flush=True)

    candidates = {}
    for source in sources:
        collect_otc_from_option_data(source, candidates)

    # Stable ordering makes the selected set reproducible when the broker
    # exposes more than 70 OTC assets. Only currently listed, enabled and
    # non-suspended binary/turbo assets are considered.
    selected_names = sorted(candidates.keys())[:MAX_OTC_ASSETS]
    discovered = {name: candidates[name] for name in selected_names}

    if discovered:
        asset_ids = discovered
        WATCHLIST = list(discovered.keys())
        for asset, active_id in asset_ids.items():
            try:
                OP_code.ACTIVES[asset] = active_id
            except Exception:
                pass
    elif reset:
        asset_ids = {}
        WATCHLIST = []

    last_asset_refresh = time.time()
    print(
        "OTC asset refresh:", len(asset_ids), "available OTC assets found; limit:",
        MAX_OTC_ASSETS, flush=True,
    )
    if not asset_ids:
        print("No available OTC binary/turbo assets found; will retry.", flush=True)
    else:
        print("OTC assets:", ", ".join(WATCHLIST), flush=True)

    return bool(asset_ids)


def connect_iq(notify=True):
    global api

    with connection_lock:
        if api is not None:
            try:
                if api.check_connect() and (asset_ids or refresh_assets()):
                    return True
            except Exception:
                pass

        if not IQ_EMAIL or not IQ_PASSWORD:
            print("Missing IQ_EMAIL or IQ_PASSWORD GitHub secrets.", flush=True)
            api = None
            return False

        try:
            print("Connecting to IQ Option...", flush=True)
            new_api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            connected, reason = new_api.connect()

            if not connected:
                print("IQ Option connection failed:", reason, flush=True)
                api = None
                return False

            # Safety: this scanner is fixed to Practice mode only.
            new_api.change_balance("PRACTICE")
            if not new_api.check_connect():
                print("Connection check failed.", flush=True)
                api = None
                return False

            api = new_api
            if not refresh_assets(True):
                print("Connected, but no watchlist IDs found. Will retry.", flush=True)
                api = None
                return False

            pairs = ", ".join(pair for pair in WATCHLIST if pair in asset_ids)
            print("Connected in PRACTICE mode. Pairs:", pairs, flush=True)

            if notify:
                telegram(
                    "Momentum scanner connected in PRACTICE mode\n"
                    "Pairs: " + pairs + "\n"
                    "Stake: $1 | Expiry: 1 minute\n"
                    "Run target: +$15 | Net loss stop: -$5 | Stop after 5 losses\n"
                    "Max resolved trades: 50"
                )
            return True

        except Exception as exc:
            print("IQ Option connection error:", exc, flush=True)
            api = None
            return False


def candle_request_worker(api_instance, active_id, count, result):
    try:
        candle_api = api_instance.api.candles
        candle_api.candles_data = []

        try:
            server_time = api_instance.get_server_timestamp()
        except Exception:
            server_time = time.time()

        api_instance.api.getcandles(
            active_id, CANDLE_SECONDS, count, server_time
        )

        started = time.time()
        while time.time() - started < CANDLE_REQUEST_TIMEOUT:
            candles = candle_api.candles_data
            if candles and len(candles) >= MIN_CANDLES_FOR_SIGNAL:
                result["candles"] = list(candles)
                return
            time.sleep(0.2)

        result["candles"] = list(candle_api.candles_data or [])

    except Exception as exc:
        result["error"] = str(exc)


def get_candles(asset, count):
    global api, asset_ids, candle_worker, last_asset_refresh

    if api is None:
        diagnostic(asset, "NO DATA: IQ Option connection is not ready")
        return []

    active_id = asset_ids.get(asset)
    if active_id is None:
        diagnostic(asset, "NO DATA: asset ID is missing")
        return []

    if candle_worker is not None and candle_worker.is_alive():
        diagnostic(asset, "CANDLE REQUEST BUSY: previous request has not finished")
        return []

    result = {"candles": [], "error": ""}
    api_instance = api
    worker = threading.Thread(
        target=candle_request_worker,
        args=(api_instance, active_id, count, result),
        daemon=True,
    )
    candle_worker = worker
    worker.start()
    worker.join(CANDLE_REQUEST_TIMEOUT + 2)

    if worker.is_alive():
        print(
            "CANDLE REQUEST STUCK for", asset,
            "; reconnecting instead of silently skipping all pairs.",
            flush=True,
        )
        # The stuck worker holds the previous API object. Drop that connection
        # and let the main loop create a fresh one for the next scan cycle.
        api = None
        asset_ids = {}
        last_asset_refresh = 0.0
        candle_worker = None
        return []

    if result["error"]:
        diagnostic(asset, "CANDLE ERROR: " + result["error"], force=True)
        return []

    try:
        candles = sorted(
            result["candles"],
            key=lambda item: float(item.get("from", 0)),
        )
        now = time.time()
        closed = [
            candle for candle in candles
            if float(candle.get("from", 0)) + CANDLE_SECONDS <= now - 2
        ]
        if not closed:
            diagnostic(
                asset,
                "NO CLOSED CANDLES: broker returned " + str(len(candles)) + " candles",
            )
        return closed
    except Exception as exc:
        diagnostic(asset, "CANDLE VALIDATION ERROR: " + str(exc), force=True)
        return []


def evaluate_signal(candles):
    details = {"count": len(candles)}
    if len(candles) < MIN_CANDLES_FOR_SIGNAL:
        details["status"] = (
            "TOO FEW CLOSED CANDLES: have " + str(len(candles))
            + ", need at least " + str(MIN_CANDLES_FOR_SIGNAL)
        )
        return None, details

    try:
        closes = [float(item["close"]) for item in candles]
        opens = [float(item["open"]) for item in candles]
        candle_times = [int(item["from"]) for item in candles]
    except Exception as exc:
        details["status"] = "INVALID CANDLE VALUES: " + str(exc)
        return None, details

    momentums = []
    for index in range(MOMENTUM_PERIOD, len(closes)):
        old_price = closes[index - MOMENTUM_PERIOD]
        if old_price <= 0:
            continue
        value = (closes[index] - old_price) / old_price * 100.0
        momentums.append(value)

    if len(momentums) < MOMENTUM_LOOKBACK + 1:
        details["status"] = "TOO FEW MOMENTUM VALUES: " + str(len(momentums))
        return None, details

    recent = momentums[-(MOMENTUM_LOOKBACK + 1):-1]
    current = momentums[-1]
    previous = momentums[-2]
    ordered = sorted(recent)

    low_index = int((len(ordered) - 1) * EXTREME_PERCENTILE)
    high_index = int((len(ordered) - 1) * (1.0 - EXTREME_PERCENTILE))
    low_limit = ordered[low_index]
    high_limit = ordered[high_index]

    turn_distance = abs(current - previous)
    old20_now = closes[-1 - MOMENTUM_CONFIRM_PERIOD]
    old20_previous = closes[-2 - MOMENTUM_CONFIRM_PERIOD]

    if old20_now <= 0 or old20_previous <= 0:
        details["status"] = "INVALID PRICE FOR MOMENTUM 20"
        return None, details

    momentum20_current = (
        (closes[-1] - old20_now) / old20_now * 100.0
    )
    momentum20_previous = (
        (closes[-2] - old20_previous) / old20_previous * 100.0
    )

    m20_rising = momentum20_current > momentum20_previous
    m20_falling = momentum20_current < momentum20_previous
    bullish = closes[-1] > opens[-1]
    bearish = closes[-1] < opens[-1]

    details.update({
        "m10": round(current, 5),
        "m10_prev": round(previous, 5),
        "low_limit": round(low_limit, 5),
        "high_limit": round(high_limit, 5),
        "turn_distance": round(turn_distance, 5),
        "m20": round(momentum20_current, 5),
        "m20_prev": round(momentum20_previous, 5),
        "m20_rising": m20_rising,
        "m20_falling": m20_falling,
        "last_candle": "BULLISH" if bullish else "BEARISH" if bearish else "DOJI",
        "candle_time": candle_times[-1],
    })

    if turn_distance < MIN_TURN_DISTANCE:
        details["status"] = "NO SETUP: Momentum 10 turn is below minimum distance"
        return None, details

    call_extreme = current <= low_limit
    put_extreme = current >= high_limit

    if call_extreme and current > previous and m20_rising and bullish:
        details["status"] = "CALL SETUP"
        return {
            "direction": "call",
            "momentum": round(current, 5),
            "previous": round(previous, 5),
            "momentum20": round(momentum20_current, 5),
            "momentum20_previous": round(momentum20_previous, 5),
            "candle_time": candle_times[-1],
        }, details

    if put_extreme and current < previous and m20_falling and bearish:
        details["status"] = "PUT SETUP"
        return {
            "direction": "put",
            "momentum": round(current, 5),
            "previous": round(previous, 5),
            "momentum20": round(momentum20_current, 5),
            "momentum20_previous": round(momentum20_previous, 5),
            "candle_time": candle_times[-1],
        }, details

    reasons = []
    if not call_extreme and not put_extreme:
        reasons.append("Momentum 10 is not at an extreme")
    if call_extreme and current <= previous:
        reasons.append("low extreme has not turned upward")
    if put_extreme and current >= previous:
        reasons.append("high extreme has not turned downward")
    if call_extreme and current > previous and not m20_rising:
        reasons.append("Momentum 20 does not confirm CALL turn")
    if put_extreme and current < previous and not m20_falling:
        reasons.append("Momentum 20 does not confirm PUT turn")
    if call_extreme and current > previous and m20_rising and not bullish:
        reasons.append("last candle is not bullish")
    if put_extreme and current < previous and m20_falling and not bearish:
        reasons.append("last candle is not bearish")

    details["status"] = "NO SETUP: " + "; ".join(reasons or ["conditions not aligned"])
    return None, details


def calculate_signal(candles):
    signal, details = evaluate_signal(candles)
    return signal


def risk_status():
    with state_lock:
        net = float(state.get("net_profit", 0.0))
        losses = int(state.get("losses", 0))
        resolved = int(state.get("resolved", 0))

    if net >= SESSION_PROFIT_TARGET:
        return False, "Run profit target reached (+$15)."
    if losses >= MAX_LOSING_TRADES:
        return False, "Five losing trades reached; stopping this run."
    if net <= -SESSION_LOSS_LIMIT:
        return False, "Run net loss limit reached (-$5)."
    if resolved >= MAX_RESOLVED_TRADES:
        return False, "Maximum 50 resolved trades reached."
    return True, ""


def stop_for_risk(reason):
    if stop_event.is_set():
        return
    stop_event.set()
    with state_lock:
        state["stop_reason"] = reason
    save_state()
    print("RISK STOP:", reason, flush=True)
    telegram("RISK STOP\n" + reason + "\nNo further trades will be opened in this run.")


def get_cached_closed_result(order_id):
    """Read a closure event already received on the current WebSocket."""
    current_api = api
    if current_api is None:
        return None

    try:
        closed_items = current_api.api.socket_option_closed
    except Exception:
        return None

    keys = [order_id, str(order_id)]
    try:
        keys.append(int(order_id))
    except (TypeError, ValueError, OverflowError):
        pass

    item = None
    for key in keys:
        try:
            item = closed_items.get(key)
        except Exception:
            item = None
        if isinstance(item, dict):
            break

    if not isinstance(item, dict):
        return None

    message = item.get("msg", item)
    if not isinstance(message, dict):
        return None

    status = str(message.get("win", "")).strip().lower()
    if status in ("equal", "draw", "tie"):
        return status, 0.0

    if status in ("loose", "lose", "loss"):
        try:
            amount = abs(float(message.get("sum", STAKE)))
        except (TypeError, ValueError, OverflowError):
            amount = STAKE
        return status, -amount

    if status in ("win", "won"):
        try:
            payout = float(message["win_amount"])
            amount = float(message["sum"])
            return status, round(payout - amount, 2)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    return None


def find_order_record(data, order_id):
    """Find a bet-info record matching the exact broker order ID."""
    wanted = str(order_id)

    if isinstance(data, dict):
        candidate = data.get(wanted)
        if isinstance(candidate, dict) and any(
            key in candidate for key in ("win", "profit", "deposit", "win_amount")
        ):
            return candidate

        for value in data.values():
            found = find_order_record(value, order_id)
            if found is not None:
                return found

    elif isinstance(data, list):
        for value in data:
            found = find_order_record(value, order_id)
            if found is not None:
                return found

    return None


def parse_betinfo_response(response, order_id):
    """Return (broker status, net profit) only when the broker confirms closure."""
    if not isinstance(response, (tuple, list)) or len(response) < 2:
        return None
    if not response[0] or not isinstance(response[1], dict):
        return None

    record = find_order_record(response[1], order_id)
    if not isinstance(record, dict):
        return None

    status = str(record.get("win", "")).strip().lower()
    if status in ("equal", "draw", "tie"):
        return status, 0.0
    if status in ("loose", "lose", "loss"):
        return status, -STAKE
    if status not in ("win", "won"):
        return None

    # In iqoptionapi bet-info responses, profit minus deposit is the net win.
    try:
        net = float(record["profit"]) - float(record["deposit"])
    except (KeyError, TypeError, ValueError, OverflowError):
        try:
            net = float(record["win_amount"]) - float(
                record.get("amount", record.get("sum", STAKE))
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    if net <= 0:
        return None
    return status, round(net, 2)


def id_value_matches(value, order_id):
    wanted = str(order_id)
    if isinstance(value, dict):
        return any(id_value_matches(item, order_id) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(id_value_matches(item, order_id) for item in value)
    if value is None:
        return False
    return str(value) == wanted


def find_closed_option_record(data, order_id):
    """Find an exact order ID in the broker's closed-options history."""
    if isinstance(data, dict):
        id_keys = ("id", "option_id", "order_id", "orderId")
        has_id = any(
            key in data and id_value_matches(data.get(key), order_id)
            for key in id_keys
        )
        has_result = any(
            key in data for key in ("win", "status", "win_amount", "profit")
        )
        if has_id and has_result:
            return data
        for value in data.values():
            found = find_closed_option_record(value, order_id)
            if found is not None:
                return found
    elif isinstance(data, (list, tuple)):
        for value in data:
            found = find_closed_option_record(value, order_id)
            if found is not None:
                return found
    return None


def parse_closed_options_response(response, order_id):
    """Parse a matching closed turbo/binary option without guessing."""
    record = find_closed_option_record(response, order_id)
    if not isinstance(record, dict):
        return None

    status = str(record.get("win", record.get("status", ""))).strip().lower()
    if status in ("equal", "draw", "tie"):
        return status, 0.0
    if status in ("loose", "lose", "loss", "lost"):
        return status, -STAKE
    if status not in ("win", "won"):
        return None

    amount_pairs = [
        ("win_amount", "amount"),
        ("win_amount", "sum"),
        ("profit", "deposit"),
    ]
    for gross_key, stake_key in amount_pairs:
        try:
            net = float(record[gross_key]) - float(record[stake_key])
            if net > 0:
                return status, round(net, 2)
            return "equal", 0.0
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return None


def direct_betinfo_result(api_instance, order_id, timeout_seconds):
    """Use the low-level request with a timeout, not stable_api's endless loop."""
    raw_api = api_instance.api
    raw_api.game_betinfo.isSuccessful = None
    raw_api.game_betinfo.dict = {}
    raw_api.get_betinfo(order_id)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = raw_api.game_betinfo.isSuccessful
        if status is not None:
            response = (status, raw_api.game_betinfo.dict)
            return parse_betinfo_response(response, order_id)
        if not api_instance.check_connect():
            break
        time.sleep(0.1)
    return None


def direct_closed_history_result(api_instance, order_id, timeout_seconds):
    """Request recent closed binary/turbo options and wait for a bounded time."""
    raw_api = api_instance.api
    raw_api.get_options_v2_data = None
    raw_api.get_options_v2(100, "binary,turbo")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = raw_api.get_options_v2_data
        if response is not None:
            return parse_closed_options_response(response, order_id)
        if not api_instance.check_connect():
            break
        time.sleep(0.1)
    return None


def get_trade_result_bounded(order_id):
    """Try cached event, bounded bet-info, then recent closed-options history."""
    cached = get_cached_closed_result(order_id)
    if cached is not None:
        return cached

    current_api = api
    if current_api is None:
        raise RuntimeError("IQ Option connection is not ready for result lookup.")

    errors = []
    try:
        result = direct_betinfo_result(current_api, order_id, 5)
        if result is not None:
            return result
        errors.append("direct bet-info did not confirm the result")
    except Exception as exc:
        errors.append("direct bet-info error: " + str(exc))

    # Some broker sessions fail to answer get-bet-info for a saved order.
    # Recent closed-options history is a separate retrieval route.
    try:
        result = direct_closed_history_result(current_api, order_id, 6)
        if result is not None:
            return result
        errors.append("closed-options history did not contain a final result")
    except Exception as exc:
        errors.append("closed-options history error: " + str(exc))

    raise RuntimeError(
        "Broker has not confirmed order " + str(order_id)
        + "; saved order remains unresolved. " + "; ".join(errors)
    )

def monitor_trade(order):
    order_id = order["order_id"]
    asset = order.get("asset", "Unknown")
    direction = order.get("direction", "call")
    opened_at = float(order.get("opened_at", time.time()))
    trade_session = str(order.get("session_id", ""))
    trade_date = order.get("trade_date", today_string())
    wait_until = opened_at + EXPIRY_MINUTES * 60 + 5

    while not stop_event.is_set():
        delay = wait_until - time.time()
        if delay > 0:
            stop_event.wait(min(delay, 5))
            continue

        try:
            if api is None or not api.check_connect():
                if not connect_iq(False):
                    stop_event.wait(RECONNECT_SECONDS)
                    continue

            # Do not call check_win_v4 here: in the installed iqoptionapi it
            # loops forever until a close event is present in the current socket.
            # After a workflow restart, that old event may not be in memory.
            broker_result = get_trade_result_bounded(order_id)
            broker_status = str(broker_result[0]).strip().lower()
            broker_profit = float(broker_result[1])

            if broker_status in ("win", "won") and broker_profit > 0:
                outcome = "WIN"
                net_change = round(broker_profit, 2)
            elif broker_status in (
                "loose", "lose", "loss", "equal", "draw", "tie"
            ):
                # Project rule: losses and zero/equal results count as a full stake loss.
                outcome = "LOSS"
                net_change = -STAKE
            else:
                raise RuntimeError(
                    "Unrecognized final broker result: " + repr(broker_result)
                )

            same_session = trade_session == SESSION_ID
            with state_lock:
                if same_session:
                    state["net_profit"] = round(
                        float(state["net_profit"]) + net_change, 2
                    )
                    if outcome == "WIN":
                        state["wins"] = int(state["wins"]) + 1
                    else:
                        state["losses"] = int(state["losses"]) + 1
                    state["resolved"] = int(state["resolved"]) + 1

                # Per-asset history deliberately persists between manual runs.
                stats_all = state.get("asset_stats", {})
                if not isinstance(stats_all, dict):
                    stats_all = {}
                    state["asset_stats"] = stats_all
                asset_stats = stats_all.get(asset)
                if not isinstance(asset_stats, dict):
                    asset_stats = {
                        "trades": 0, "wins": 0, "losses": 0,
                        "net_profit": 0.0, "last_result": "",
                        "last_updated": "",
                    }
                    stats_all[asset] = asset_stats
                asset_stats["trades"] = int(asset_stats.get("trades", 0)) + 1
                if outcome == "WIN":
                    asset_stats["wins"] = int(asset_stats.get("wins", 0)) + 1
                else:
                    asset_stats["losses"] = int(asset_stats.get("losses", 0)) + 1
                asset_stats["net_profit"] = round(
                    float(asset_stats.get("net_profit", 0.0)) + net_change, 2
                )
                asset_stats["last_result"] = outcome
                asset_stats["last_updated"] = datetime.now(LAGOS).isoformat()

                state["active_order"] = None
                session_net = round(float(state["net_profit"]), 2)
                resolved = int(state["resolved"])
                wins_now = int(state["wins"])
                losses_now = int(state["losses"])
                asset_stat_snapshot = dict(asset_stats)

            save_state()
            log_trade({
                "time": datetime.now(LAGOS).isoformat(),
                "date": trade_date,
                "session_id": trade_session,
                "asset": asset,
                "direction": direction.upper(),
                "momentum": order.get("momentum", ""),
                "momentum20": order.get("momentum20", ""),
                "candle_time": order.get("candle_time", ""),
                "order_id": order_id,
                "result": outcome,
                "profit": net_change,
                "session_net": session_net,
            })

            message = (
                "MOMENTUM SCANNER RESULT\n"
                "Asset: " + asset + "\n"
                "Direction: " + direction.upper() + "\n"
                "Result: " + outcome + "\n"
                "Net trade result: $" + str(round(net_change, 2)) + "\n"
                "Run net: $" + str(session_net) + "\n"
                "Resolved: " + str(resolved) + "/" + str(MAX_RESOLVED_TRADES) + "\n"
                "Wins: " + str(wins_now) + " | Losses: " + str(losses_now) + "\n"
                "Asset history: " + str(asset_stat_snapshot.get("trades", 0)) + " trades"
                + " | W/L=" + str(asset_stat_snapshot.get("wins", 0)) + "/"
                + str(asset_stat_snapshot.get("losses", 0))
                + " | Net=$" + str(asset_stat_snapshot.get("net_profit", 0.0))
            )
            if not same_session:
                message += (
                    "\nPrior-run order: result logged but excluded from this run's risk totals."
                )
            print(message, flush=True)
            telegram(message)
            print(asset_performance_summary(), flush=True)

            if same_session:
                allowed, reason = risk_status()
                if not allowed:
                    stop_for_risk(reason)
            return

        except Exception as exc:
            print("Trade result unresolved; retrying:", exc, flush=True)
            stop_event.wait(RESULT_RETRY_SECONDS)


def place_trade(asset, direction, signal):
    if not AUTO_TRADE or stop_event.is_set():
        return False
    if not trade_lock.acquire(blocking=False):
        print("Trade placement already in progress.", flush=True)
        return False

    try:
        allowed, reason = risk_status()
        if not allowed:
            stop_for_risk(reason)
            return False

        with state_lock:
            if state.get("active_order") is not None:
                print("Trade blocked: an order is unresolved.", flush=True)
                return False

        if api is None or not api.check_connect():
            print("Disconnected; trade skipped.", flush=True)
            return False
        if asset not in asset_ids:
            print("No active ID for", asset, "- trade skipped.", flush=True)
            return False

        allowed, reason = risk_status()
        if not allowed or stop_event.is_set():
            if not allowed:
                stop_for_risk(reason)
            return False

        print("PRACTICE balance:", api.get_balance(), flush=True)
        success, order_id = api.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        if not success:
            print("Order rejected for", asset, ":", order_id, flush=True)
            telegram("Order rejected for " + asset + ". No trade recorded.")
            return False

        order = {
            "order_id": order_id,
            "asset": asset,
            "direction": direction,
            "momentum": signal["momentum"],
            "momentum20": signal.get("momentum20", ""),
            "candle_time": signal["candle_time"],
            "opened_at": time.time(),
            "trade_date": today_string(),
            "session_id": SESSION_ID,
        }
        with state_lock:
            state["active_order"] = order
        save_state()

        print(
            "PRACTICE order accepted:", asset,
            direction.upper(), order_id, flush=True,
        )
        telegram(
            "PRACTICE TRADE OPENED\n"
            "Asset: " + asset + "\n"
            "Direction: " + direction.upper() + "\n"
            "Stake: $1 | Expiry: 1 minute\n"
            "Run target: +$15 | Net loss stop: -$5 | Stop after 5 losses\n"
            "Order ID: " + str(order_id)
        )
        thread = threading.Thread(target=monitor_trade, args=(order,), daemon=True)
        thread.start()
        return True

    except Exception as exc:
        print("Trade placement error:", exc, flush=True)
        telegram("Trade placement error: " + str(exc))
        return False
    finally:
        trade_lock.release()


def heartbeat():
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return
    last_heartbeat = now

    with state_lock:
        active = state.get("active_order")
        active_asset = active.get("asset") if isinstance(active, dict) else "None"
        print(
            "HEARTBEAT | Run:", state.get("session_id"),
            "| OTC assets:", len(asset_ids), "/", MAX_OTC_ASSETS,
            "| Resolved:", state.get("resolved"),
            "| Wins:", state.get("wins"),
            "| Losses:", state.get("losses"),
            "| Net: $", state.get("net_profit"),
            "| Active order:", active_asset,
            flush=True,
        )


def run_scanner():
    global last_asset_refresh
    load_state()
    apply_verified_order_recovery()

    print("Momentum 10/20 scanner starting in PRACTICE mode.", flush=True)
    print("Run ID:", SESSION_ID, flush=True)
    print("OTC discovery limit:", MAX_OTC_ASSETS, "assets", flush=True)
    print("Only broker-listed, enabled, non-suspended OTC binary/turbo assets are scanned.", flush=True)
    print("Per-asset results are saved across manual runs for later ranking.", flush=True)
    print("Run profit target: +$15 | Net loss stop: -$5 | Stop after 5 losing trades", flush=True)
    print("Maximum resolved trades:", MAX_RESOLVED_TRADES, flush=True)
    print(
        "Diagnostics enabled: each pair reports candle/data status at least once per minute.",
        flush=True,
    )

    if not connect_iq(True):
        print("Initial connection not ready. Scanner will retry.", flush=True)

    with state_lock:
        saved_order = state.get("active_order")
    if isinstance(saved_order, dict):
        print(
            "Saved unresolved order found; checking it before new entries.",
            flush=True,
        )
        thread = threading.Thread(target=monitor_trade, args=(saved_order,), daemon=True)
        thread.start()

    while not stop_event.is_set():
        try:
            if api is None or not api.check_connect():
                if not connect_iq(False):
                    stop_event.wait(RECONNECT_SECONDS)
                    continue
        except Exception:
            if not connect_iq(False):
                stop_event.wait(RECONNECT_SECONDS)
                continue

        if time.time() - last_asset_refresh >= ASSET_REFRESH_SECONDS:
            refresh_assets()

        allowed, reason = risk_status()
        if not allowed:
            stop_for_risk(reason)
            break

        with state_lock:
            active = state.get("active_order")
        if active is not None:
            heartbeat()
            stop_event.wait(SCAN_INTERVAL)
            continue

        for asset in list(WATCHLIST):
            if stop_event.is_set():
                break
            with state_lock:
                if state.get("active_order") is not None:
                    break

            allowed, reason = risk_status()
            if not allowed:
                stop_for_risk(reason)
                break

            if asset not in asset_ids:
                diagnostic(asset, "SKIPPED: asset ID is not available")
                continue

            candles = get_candles(asset, CANDLE_REQUEST_COUNT)
            if not candles:
                diagnostic(asset, "NO CLOSED CANDLE DATA returned by candle request")
                continue

            signal, details = evaluate_signal(candles)
            if signal is None:
                diagnostic(
                    asset,
                    "candles=" + str(len(candles))
                    + " | " + str(details.get("status", "No qualifying setup"))
                    + " | M10=" + str(details.get("m10", "n/a"))
                    + " prev=" + str(details.get("m10_prev", "n/a"))
                    + " extremes=" + str(details.get("low_limit", "n/a"))
                    + "/" + str(details.get("high_limit", "n/a"))
                    + " | M20=" + str(details.get("m20", "n/a"))
                    + " prev=" + str(details.get("m20_prev", "n/a"))
                    + " | candle=" + str(details.get("last_candle", "n/a")),
                )
                continue

            signal_time = int(signal["candle_time"])
            with state_lock:
                previous_signal = state["last_signal_candle"].get(asset)
                duplicate = previous_signal == signal_time
                if not duplicate:
                    state["last_signal_candle"][asset] = signal_time
            if duplicate:
                diagnostic(asset, "SETUP found but already handled for this candle")
                continue

            save_state()
            message = (
                "MOMENTUM 10 SIGNAL (MOMENTUM 20 CONFIRMED)\n"
                "Asset: " + asset + "\n"
                "Direction: " + signal["direction"].upper() + "\n"
                "Timeframe: 1 minute | Expiry: 1 minute\n"
                "Momentum 10: " + str(signal["momentum"]) + "\n"
                "Momentum 20: " + str(signal["momentum20"]) + "\n"
                "Mode: PRACTICE"
            )
            print(message, flush=True)
            telegram(message)
            place_trade(asset, signal["direction"], signal)

            with state_lock:
                if state.get("active_order") is not None:
                    break
            if stop_event.is_set():
                break

        heartbeat()
        stop_event.wait(SCAN_INTERVAL)

    with state_lock:
        active = state.get("active_order")
        summary = (
            "MOMENTUM SCANNER STOPPED\n"
            "Run ID: " + str(state.get("session_id")) + "\n"
            "Resolved: " + str(state.get("resolved"))
            + "/" + str(MAX_RESOLVED_TRADES) + "\n"
            "Wins: " + str(state.get("wins"))
            + " | Losses: " + str(state.get("losses")) + "\n"
            "Run net: $" + str(state.get("net_profit")) + "\n"
            "Target: +$15 | Net loss stop: -$5 | Max losing trades: 5\n"
            "Stop reason: " + str(state.get("stop_reason") or "Manual/workflow stop")
            + "\nUnresolved order: " + str(active is not None)
        )
    save_state()
    print(summary, flush=True)
    print(asset_performance_summary(), flush=True)
    telegram(summary + "\n\n" + asset_performance_summary())


if __name__ == "__main__":
    try:
        run_scanner()
    except KeyboardInterrupt:
        print("Scanner stopped by user.", flush=True)
        stop_event.set()
        save_state()
    except Exception as exc:
        print("Fatal scanner error:", exc, flush=True)
        telegram("Fatal scanner error: " + str(exc))
        stop_event.set()
        save_state()
        raise
