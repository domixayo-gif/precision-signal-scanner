
# MOMENTUM 10 IQ OPTION SCANNER
# PRACTICE ONLY | Manual scan never places trades
# Momentum 20 has been completely removed.

import os
import re
import json
import time
import math
import base64
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from iqoptionapi.stable_api import IQ_Option

# ================ SETTINGS ================
BALANCE_MODE = "PRACTICE"
AUTO_TRADING_ALLOWED = True

STAKE = 1.0
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60
LOOKBACK_CANDLES = 50
MOMENTUM_PERIOD = 10
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06

DAILY_PROFIT_TARGET = 5.0
DAILY_MAX_LOSS = -3.0
MAX_RESOLVED_TRADES = 20
MAX_CONSECUTIVE_LOSSES = 5

SCAN_INTERVAL_SECONDS = 60
REQUEST_PAUSE_SECONDS = 0.12
RECONNECT_SECONDS = 30
HEARTBEAT_SECONDS = 300
RESULT_TIMEOUT_SECONDS = EXPIRY_MINUTES * 60 + 45
TOP_SIGNALS = 3
PROGRESS_EVERY = 20
STATE_FILE = "momentum_state.json"

LAGOS = ZoneInfo("Africa/Lagos")

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"

CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD",
    "SGD", "HKD", "NOK", "SEK", "DKK", "MXN", "ZAR", "TRY", "PLN",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

DEFAULT_STATE = {
    "date": "",
    "daily_pnl": 0.0,
    "resolved_trades": 0,
    "consecutive_losses": 0,
    "auto_enabled": False,
    "last_update_id": 0,
    "last_scan": "Never",
    "last_scan_count": 0,
    "last_scan_setups": 0,
    "last_scan_rejections": {},
    "last_auto_candles": {},
    "pending_trade": None,
}

session = requests.Session()
IQ = None
practice_verified = False
state = dict(DEFAULT_STATE)
scan_requested = False
busy = False
OPEN_TIME_CACHE = None
last_auto_scan = 0.0
last_heartbeat = 0.0
last_connect_attempt = 0.0

result_reader_thread = None
result_reader_event = None
result_reader_result = {}

state_lock = threading.RLock()
scan_lock = threading.Lock()
api_lock = threading.RLock()
save_lock = threading.Lock()


# ================ TIME AND STATE ================
def now_lagos():
    return datetime.now(LAGOS)


def day_key():
    return now_lagos().strftime("%Y-%m-%d")


def github_contents_url():
    if not GITHUB_REPOSITORY:
        return ""
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
    )


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state():
    loaded = {}
    url = github_contents_url()

    if url and GITHUB_TOKEN:
        try:
            response = session.get(
                url,
                headers=github_headers(),
                params={"ref": GITHUB_REF_NAME},
                timeout=15,
            )

            if response.ok:
                encoded = response.json().get("content", "")
                if encoded:
                    loaded = json.loads(
                        base64.b64decode(encoded).decode("utf-8")
                    )
            elif response.status_code != 404:
                logging.warning(
                    "GitHub state load failed: %s",
                    response.status_code,
                )

        except Exception as exc:
            logging.warning("GitHub state load error: %s", exc)

    if not isinstance(loaded, dict) or not loaded:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception:
            loaded = {}

    result = dict(DEFAULT_STATE)
    if isinstance(loaded, dict):
        result.update(loaded)

    if result.get("date") != day_key():
        result["date"] = day_key()
        result["daily_pnl"] = 0.0
        result["resolved_trades"] = 0
        result["consecutive_losses"] = 0

    # AUTO never resumes automatically after a restart.
    result["auto_enabled"] = False

    result["last_update_id"] = max(
        0, int(result.get("last_update_id", 0) or 0)
    )
    result["daily_pnl"] = float(result.get("daily_pnl", 0) or 0)
    result["resolved_trades"] = max(
        0, int(result.get("resolved_trades", 0) or 0)
    )
    result["consecutive_losses"] = max(
        0, int(result.get("consecutive_losses", 0) or 0)
    )

    for key in ("last_auto_candles", "last_scan_rejections"):
        if not isinstance(result.get(key), dict):
            result[key] = {}

    if not isinstance(result.get("pending_trade"), dict):
        result["pending_trade"] = None

    return result


def save_state(remote=True):
    with save_lock:
        with state_lock:
            state["date"] = day_key()
            snapshot = json.loads(json.dumps(state))

        try:
            temporary = STATE_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2)
            os.replace(temporary, STATE_FILE)
        except Exception as exc:
            logging.warning("Local state save failed: %s", exc)

        if not remote:
            return

        url = github_contents_url()
        if not url or not GITHUB_TOKEN:
            return

        content = base64.b64encode(
            json.dumps(snapshot, indent=2).encode("utf-8")
        ).decode("ascii")

        for attempt in range(3):
            try:
                current = session.get(
                    url,
                    headers=github_headers(),
                    params={"ref": GITHUB_REF_NAME},
                    timeout=15,
                )

                sha = current.json().get("sha") if current.ok else None

                if not current.ok and current.status_code != 404:
                    logging.warning(
                        "GitHub state lookup failed: %s",
                        current.status_code,
                    )
                    time.sleep(attempt + 1)
                    continue

                body = {
                    "message": "Update Momentum 10 scanner state",
                    "content": content,
                    "branch": GITHUB_REF_NAME,
                }
                if sha:
                    body["sha"] = sha

                response = session.put(
                    url,
                    headers=github_headers(),
                    json=body,
                    timeout=20,
                )

                if response.ok:
                    return

                logging.warning(
                    "GitHub state save failed: %s %s",
                    response.status_code,
                    response.text[:160],
                )

                if response.status_code not in (409, 422):
                    break

                time.sleep(attempt + 1)

            except Exception as exc:
                logging.warning("GitHub state save error: %s", exc)
                time.sleep(attempt + 1)


def enforce_new_day():
    changed = False

    with state_lock:
        if state.get("date") != day_key():
            state["date"] = day_key()
            state["daily_pnl"] = 0.0
            state["resolved_trades"] = 0
            state["consecutive_losses"] = 0
            changed = True

    if changed:
        save_state()


def risk_stop_reason():
    enforce_new_day()

    with state_lock:
        pnl = float(state.get("daily_pnl", 0))
        resolved = int(state.get("resolved_trades", 0))
        losses = int(state.get("consecutive_losses", 0))
        pending = state.get("pending_trade")

    if pending:
        return "an earlier order is unresolved; reconcile it first"
    if pnl >= DAILY_PROFIT_TARGET:
        return f"daily profit target reached (+${DAILY_PROFIT_TARGET:.2f})"
    if pnl <= DAILY_MAX_LOSS:
        return f"daily loss limit reached (${DAILY_MAX_LOSS:.2f})"
    if resolved >= MAX_RESOLVED_TRADES:
        return f"maximum resolved trades reached ({MAX_RESOLVED_TRADES})"
    if losses >= MAX_CONSECUTIVE_LOSSES:
        return f"maximum consecutive losses reached ({MAX_CONSECUTIVE_LOSSES})"

    return ""


# ================ TELEGRAM ================
def telegram_api(method):
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def control_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "▶️ START AUTO", "callback_data": "START_AUTO"},
                {"text": "⏹ STOP AUTO", "callback_data": "STOP_AUTO"},
            ],
            [
                {"text": "🔎 SCAN MARKET", "callback_data": "SCAN_MARKET"},
                {"text": "📊 STATUS", "callback_data": "STATUS"},
            ],
        ]
    }


def telegram_send(text, keyboard=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram not configured: %s", text)
        return

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": str(text)[:4000],
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(control_keyboard())

    try:
        response = session.post(
            telegram_api("sendMessage"),
            data=payload,
            timeout=20,
        )

        if not response.ok:
            logging.warning(
                "Telegram send failed: %s %s",
                response.status_code,
                response.text[:180],
            )

    except Exception as exc:
        logging.warning("Telegram send error: %s", exc)


def telegram_answer_callback(callback_id, text="Done"):
    if not callback_id:
        return

    try:
        session.post(
            telegram_api("answerCallbackQuery"),
            data={
                "callback_query_id": callback_id,
                "text": str(text)[:190],
            },
            timeout=10,
        )
    except Exception as exc:
        logging.warning("Callback answer error: %s", exc)


def prepare_telegram():
    response = session.post(
        telegram_api("deleteWebhook"),
        data={"drop_pending_updates": "false"},
        timeout=15,
    )
    data = response.json()

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram deleteWebhook failed: {response.text[:180]}"
        )

    response = session.get(telegram_api("getMe"), timeout=15)
    data = response.json()

    if not response.ok or not data.get("ok"):
        raise RuntimeError(f"Telegram getMe failed: {response.text[:180]}")

    username = (data.get("result") or {}).get("username", "unknown")
    logging.info("Telegram bot verified: @%s", username)


# ================ IQ OPTION SAFETY ================
def connection_is_live(client=None):
    client = client if client is not None else IQ

    if client is None:
        return False

    try:
        return bool(client.check_connect())
    except Exception:
        return False


def verify_practice_mode(force_select=True):
    global practice_verified

    if IQ is None or not connection_is_live():
        practice_verified = False
        return False

    try:
        with api_lock:
            if force_select:
                IQ.change_balance("PRACTICE")
                time.sleep(0.5)

            getter = getattr(IQ, "get_balance_mode", None)

            if not callable(getter):
                practice_verified = False
                logging.error(
                    "get_balance_mode() unavailable; AUTO orders blocked."
                )
                return False

            mode = str(getter() or "").strip().upper()

        practice_verified = mode == "PRACTICE"

        if not practice_verified:
            logging.error(
                "PRACTICE verification failed. API reported mode: %r",
                mode,
            )

        return practice_verified

    except Exception as exc:
        practice_verified = False
        logging.error("PRACTICE verification error: %s", exc)
        return False


def connect_iq():
    global IQ, practice_verified

    if not IQ_EMAIL or not IQ_PASSWORD:
        logging.error("Missing IQ_EMAIL or IQ_PASSWORD secrets.")
        return False

    try:
        candidate = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
        response = candidate.connect()

        connected = (
            response[0]
            if isinstance(response, tuple)
            else bool(response)
        )

        if not connected or not connection_is_live(candidate):
            logging.error("IQ Option connection failed: %r", response)
            return False

        with api_lock:
            candidate.change_balance("PRACTICE")
            time.sleep(0.5)

        IQ = candidate
        practice_verified = verify_practice_mode(force_select=False)

        if practice_verified:
            telegram_send(
                "✅ IQ Option connected; PRACTICE mode verified. AUTO is OFF."
            )
        else:
            telegram_send(
                "⚠️ Connected, but PRACTICE mode could not be verified. "
                "AUTO orders are blocked."
            )

        return True

    except Exception as exc:
        logging.exception("IQ Option connection error: %s", exc)
        IQ = None
        practice_verified = False
        return False


# ================ ASSET DISCOVERY ================
def normalize_asset_name(name):
    name = str(name or "").strip().upper().replace("_", "-")
    return re.sub(r"-+", "-", name)


def valid_currency_asset(name):
    name = normalize_asset_name(name)

    if name.endswith("-OTC"):
        name = name[:-4]

    if len(name) != 6 or not name.isalpha():
        return False

    return name[:3] in CURRENCY_CODES and name[3:] in CURRENCY_CODES


def refresh_open_time_cache():
    global OPEN_TIME_CACHE

    try:
        with api_lock:
            result = IQ.get_all_open_time()

        OPEN_TIME_CACHE = result if isinstance(result, dict) else None

    except Exception as exc:
        OPEN_TIME_CACHE = None
        logging.warning("Open-market table error: %s", exc)

    return isinstance(OPEN_TIME_CACHE, dict)


def get_asset_names():
    found = set()

    if IQ is None or not connection_is_live():
        return []

    try:
        with api_lock:
            mapping = IQ.get_all_ACTIVES_OPCODE()

        if isinstance(mapping, dict):
            found.update(str(key) for key in mapping.keys())

    except Exception as exc:
        logging.warning("Active assets query failed: %s", exc)

    if isinstance(OPEN_TIME_CACHE, dict):
        for market_type in ("turbo", "binary"):
            market = OPEN_TIME_CACHE.get(market_type, {})
            if isinstance(market, dict):
                found.update(str(key) for key in market.keys())

    return sorted({
        normalize_asset_name(name)
        for name in found
        if valid_currency_asset(name)
    })


def asset_is_open(asset):
    if not isinstance(OPEN_TIME_CACHE, dict):
        return False, "market_table_unavailable"

    wanted = normalize_asset_name(asset)
    saw_market = False
    saw_asset = False

    for market_type in ("turbo", "binary"):
        market = OPEN_TIME_CACHE.get(market_type, {})

        if not isinstance(market, dict) or not market:
            continue

        saw_market = True

        for name, details in market.items():
            if normalize_asset_name(name) != wanted:
                continue

            saw_asset = True

            if isinstance(details, dict) and details.get("open") is True:
                return True, ""

    if not saw_market:
        return False, "market_table_unavailable"
    if saw_asset:
        return False, "market_closed"

    return False, "market_closed_or_not_listed"


def get_closed_candles(asset):
    if IQ is None or not connection_is_live():
        return []

    with api_lock:
        raw = IQ.get_candles(
            asset,
            CANDLE_SECONDS,
            LOOKBACK_CANDLES + MOMENTUM_PERIOD + 8,
            time.time(),
        )

    if not isinstance(raw, list):
        return []

    current_time = time.time()
    candles = []

    for row in raw:
        try:
            stamp = float(row.get("from", 0) or 0)

            # Only closed candles are allowed.
            if not stamp or stamp + CANDLE_SECONDS > current_time - 2:
                continue

            candle = {
                "open": float(row.get("open", 0)),
                "close": float(row.get("close", 0)),
                "high": float(row.get("max", row.get("high", 0))),
                "low": float(row.get("min", row.get("low", 0))),
                "from": stamp,
            }

            if candle["open"] > 0 and candle["close"] > 0:
                candles.append(candle)

        except (TypeError, ValueError, AttributeError):
            continue

    candles.sort(key=lambda item: item["from"])
    return candles[-(LOOKBACK_CANDLES + MOMENTUM_PERIOD + 3):]


# ================ MOMENTUM 10 STRATEGY ONLY ================
def percentile(values, fraction):
    if not values:
        return 0.0

    values = sorted(values)
    index = int(round((len(values) - 1) * fraction))
    index = max(0, min(len(values) - 1, index))
    return float(values[index])


def momentum_10(closes, end_index):
    """Ten-candle percentage momentum. No Momentum 20 calculation exists."""
    start_index = end_index - MOMENTUM_PERIOD

    if start_index < 0 or closes[start_index] == 0:
        return None

    return (
        closes[end_index] / closes[start_index] - 1.0
    ) * 100.0


def is_bullish_engulfing(previous, current):
    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def is_bearish_engulfing(previous, current):
    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def analyze_asset(asset):
    opened, reason = asset_is_open(asset)

    if not opened:
        return None, reason

    try:
        candles = get_closed_candles(asset)
    except Exception as exc:
        logging.warning("Candle error for %s: %s", asset, exc)
        return None, "candle_request_error"

    required = LOOKBACK_CANDLES + MOMENTUM_PERIOD

    if len(candles) < required:
        return None, "insufficient_closed_candles"

    closes = [candle["close"] for candle in candles]
    current_index = len(candles) - 1
    previous_index = current_index - 1

    current_momentum = momentum_10(closes, current_index)
    previous_momentum = momentum_10(closes, previous_index)

    if current_momentum is None or previous_momentum is None:
        return None, "insufficient_momentum_history"

    history = []
    start_index = max(MOMENTUM_PERIOD, len(candles) - LOOKBACK_CANDLES)

    for index in range(start_index, current_index):
        value = momentum_10(closes, index)
        if value is not None:
            history.append(value)

    if len(history) < 15:
        return None, "insufficient_momentum_history"

    lower_extreme = percentile(history, EXTREME_PERCENTILE)
    upper_extreme = percentile(history, 1.0 - EXTREME_PERCENTILE)
    turn = current_momentum - previous_momentum

    previous_candle = candles[-2]
    current_candle = candles[-1]

    bullish_candle = current_candle["close"] > current_candle["open"]
    bearish_candle = current_candle["close"] < current_candle["open"]

    bullish_engulfing = is_bullish_engulfing(
        previous_candle, current_candle
    )
    bearish_engulfing = is_bearish_engulfing(
        previous_candle, current_candle
    )

    # CALL: prior Momentum 10 reached the lower extreme, then turned upward.
    if (
        previous_momentum <= lower_extreme
        and turn >= MIN_TURN_DISTANCE
        and (bullish_candle or bullish_engulfing)
    ):
        direction = "call"
        depth = max(0.0, lower_extreme - previous_momentum)
        turn_strength = max(0.0, turn)
        candle_points = 20 if bullish_candle else 0
        engulf_points = 15 if bullish_engulfing else 0

    # PUT: prior Momentum 10 reached the upper extreme, then turned downward.
    elif (
        previous_momentum >= upper_extreme
        and turn <= -MIN_TURN_DISTANCE
        and (bearish_candle or bearish_engulfing)
    ):
        direction = "put"
        depth = max(0.0, previous_momentum - upper_extreme)
        turn_strength = max(0.0, -turn)
        candle_points = 20 if bearish_candle else 0
        engulf_points = 15 if bearish_engulfing else 0

    else:
        return None, "no_momentum10_reversal_confirmation"

    depth_points = min(35.0, 15.0 + depth * 4.0)
    turn_points = min(30.0, 10.0 + turn_strength * 8.0)

    score = int(round(min(
        100.0,
        depth_points + turn_points + candle_points + engulf_points,
    )))

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "momentum10": current_momentum,
        "turn": turn,
        "engulfing": (
            bullish_engulfing if direction == "call"
            else bearish_engulfing
        ),
        "candle_id": int(current_candle["from"]),
        "timestamp": now_lagos().strftime("%H:%M:%S WAT"),
    }, ""


# ================ MARKET SCANNING ================
def scan_market():
    global busy, OPEN_TIME_CACHE

    if not scan_lock.acquire(blocking=False):
        telegram_send("⏳ A market scan is already running.")
        return []

    busy = True
    signals = []
    rejections = {}
    started = time.time()

    try:
        if IQ is None or not connection_is_live():
            telegram_send("⚠️ IQ Option is disconnected. Scan unavailable.")
            return []

        refresh_open_time_cache()
        assets = get_asset_names()

        if not assets:
            rejections["no_currency_assets_returned"] = 1

            with state_lock:
                state["last_scan"] = now_lagos().strftime(
                    "%Y-%m-%d %H:%M:%S WAT"
                )
                state["last_scan_count"] = 0
                state["last_scan_setups"] = 0
                state["last_scan_rejections"] = rejections

            save_state(remote=False)
            telegram_send(
                "⚠️ No valid currency pairs returned by IQ Option. "
                "Check the GitHub Actions log."
            )
            return []

        logging.info("Scanning %d currency pairs.", len(assets))
        telegram_send(
            f"🔎 Momentum 10 scan started: {len(assets)} currency pairs.",
            keyboard=False,
        )

        for index, asset in enumerate(assets, start=1):
            try:
                signal, reason = analyze_asset(asset)

                if signal:
                    signals.append(signal)
                elif reason:
                    rejections[reason] = rejections.get(reason, 0) + 1

            except Exception as exc:
                logging.warning("Skipping %s: %s", asset, exc)
                rejections["unexpected_asset_error"] = (
                    rejections.get("unexpected_asset_error", 0) + 1
                )

            if index % PROGRESS_EVERY == 0 or index == len(assets):
                logging.info(
                    "Scan progress %d/%d; %d setup(s).",
                    index, len(assets), len(signals),
                )
                telegram_send(
                    f"🔎 Progress: {index}/{len(assets)} pairs; "
                    f"{len(signals)} qualifying setup(s).",
                    keyboard=False,
                )

            time.sleep(REQUEST_PAUSE_SECONDS)

        signals.sort(
            key=lambda item: (item["score"], abs(item["turn"])),
            reverse=True,
        )

        top_rejections = dict(
            sorted(
                rejections.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
        )

        with state_lock:
            state["last_scan"] = now_lagos().strftime(
                "%Y-%m-%d %H:%M:%S WAT"
            )
            state["last_scan_count"] = len(assets)
            state["last_scan_setups"] = len(signals)
            state["last_scan_rejections"] = top_rejections

        save_state(remote=False)

        logging.info(
            "Scan finished in %.1fs: %d pairs, %d setups, rejections=%s",
            time.time() - started,
            len(assets),
            len(signals),
            top_rejections,
        )

        return signals

    except Exception as exc:
        logging.exception("Market scan failed")
        telegram_send(f"🚨 Scan failed: {type(exc).__name__}: {exc}")
        return []

    finally:
        busy = False
        OPEN_TIME_CACHE = None
        scan_lock.release()


def format_rejections():
    with state_lock:
        reasons = dict(state.get("last_scan_rejections", {}))
        pairs = int(state.get("last_scan_count", 0) or 0)
        setups = int(state.get("last_scan_setups", 0) or 0)

    if pairs == 0 and not reasons:
        return "No completed market scan yet."

    labels = {
        "market_closed": "market closed",
        "market_closed_or_not_listed": "closed/not listed",
        "market_table_unavailable": "market table unavailable",
        "insufficient_closed_candles": "insufficient closed candles",
        "insufficient_momentum_history": "insufficient Momentum 10 history",
        "no_momentum10_reversal_confirmation": "no Momentum 10 reversal confirmation",
        "candle_request_error": "candle/API errors",
        "unexpected_asset_error": "unexpected pair errors",
        "no_currency_assets_returned": "no currency pairs returned",
    }

    if not reasons:
        return f"Pairs checked: {pairs}; qualifying setups: {setups}."

    details = ", ".join(
        f"{labels.get(key, key)}: {value}"
        for key, value in reasons.items()
    )

    return (
        f"Pairs checked: {pairs}; qualifying setups: {setups}. "
        f"Rejections: {details}."
    )


def format_signals(signals, title):
    if not signals:
        return (
            f"{title}\n\n"
            "NO TRADE — no setup passed the Momentum 10 filters.\n"
            f"{format_rejections()}\n"
            "Filters have not been loosened to force a signal."
        )

    lines = [
        title,
        "Momentum 10 only. Score ranks setups; it is not win probability.",
        "",
    ]

    for rank, signal in enumerate(signals[:TOP_SIGNALS], start=1):
        direction = (
            "CALL / UP" if signal["direction"] == "call"
            else "PUT / DOWN"
        )

        lines.append(
            f"#{rank} — {signal['asset']} — {direction}\n"
            f"   SCORE: {signal['score']}/100 | "
            f"M10: {signal['momentum10']:+.3f}% | "
            f"Turn: {signal['turn']:+.3f}% | "
            f"Engulfing: {'Yes' if signal['engulfing'] else 'No'} | "
            f"{signal['timestamp']}"
        )

    if len(signals) < TOP_SIGNALS:
        lines.append(
            f"\nOnly {len(signals)} qualifying setup(s) found."
        )

    return "\n".join(lines)


# ================ TRADE RESULT TRACKING ================
def parse_profit(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (tuple, list)):
        numbers = [
            item for item in value
            if isinstance(item, (int, float))
            and not isinstance(item, bool)
        ]
        if not numbers:
            return None
        value = numbers[-1]

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return number if math.isfinite(number) else None


def turn_auto_off(reason=None):
    with state_lock:
        was_on = bool(state.get("auto_enabled"))
        state["auto_enabled"] = False

    save_state()

    if reason:
        telegram_send(f"⏹ AUTO STOPPED: {reason}.")
    elif was_on:
        telegram_send(
            "⏹ AUTO is OFF. No new orders will be submitted. "
            "An already accepted order cannot be cancelled here."
        )


def complete_pending_trade(profit, source="broker result"):
    profit = parse_profit(profit)

    if profit is None:
        return False

    enforce_new_day()

    with state_lock:
        pending = state.get("pending_trade")

        if not isinstance(pending, dict):
            return False

        asset = str(pending.get("asset", "unknown"))
        direction = str(pending.get("direction", "unknown")).upper()

        state["daily_pnl"] = round(
            float(state.get("daily_pnl", 0)) + profit, 2
        )
        state["resolved_trades"] = int(
            state.get("resolved_trades", 0)
        ) + 1

        if profit > 0:
            state["consecutive_losses"] = 0
            outcome = "WIN ✅"
        elif profit < 0:
            state["consecutive_losses"] = int(
                state.get("consecutive_losses", 0)
            ) + 1
            outcome = "LOSS ❌"
        else:
            outcome = "DRAW / ZERO P&L ➖"

        state["pending_trade"] = None
        pnl = state["daily_pnl"]
        resolved = state["resolved_trades"]
        losses = state["consecutive_losses"]

    save_state()

    telegram_send(
        f"{outcome}\n{asset} — {direction}\n"
        f"Net result ({source}): ${profit:+.2f}\n"
        f"Today P/L: ${pnl:+.2f}\n"
        f"Resolved: {resolved}/{MAX_RESOLVED_TRADES} | "
        f"Consecutive losses: {losses}/{MAX_CONSECUTIVE_LOSSES}"
    )

    reason = risk_stop_reason()
    if reason:
        turn_auto_off(reason)

    return True


def result_reader(order_id):
    try:
        if IQ is None:
            result_reader_result["error"] = "IQ connection unavailable"
        else:
            result_reader_result["value"] = IQ.check_win_v4(order_id)

    except Exception as exc:
        result_reader_result["error"] = str(exc)

    finally:
        event = result_reader_event
        if event is not None:
            event.set()


def poll_late_result():
    global result_reader_thread, result_reader_event

    event = result_reader_event
    if event is None or not event.is_set():
        return

    if "value" in result_reader_result:
        profit = parse_profit(result_reader_result.get("value"))

        if profit is not None:
            complete_pending_trade(profit, source="broker result")
        else:
            with state_lock:
                pending = state.get("pending_trade")

            if pending:
                turn_auto_off("unreadable broker result; reconcile the trade")
                telegram_send(
                    "⚠️ Broker result unreadable. Check account history, "
                    "then use /reconcile NET-P/L."
                )

    elif result_reader_result.get("error"):
        with state_lock:
            pending = state.get("pending_trade")

        if pending:
            turn_auto_off("broker result request failed; reconcile the trade")
            telegram_send(
                "⚠️ Broker result request failed. Check account history, "
                "then use /reconcile NET-P/L."
            )

    result_reader_result.clear()
    result_reader_thread = None
    result_reader_event = None


def place_and_monitor(signal):
    global result_reader_thread, result_reader_event

    with state_lock:
        if not state.get("auto_enabled"):
            telegram_send("AUTO is OFF. No order was placed.")
            return

        if state.get("pending_trade"):
            telegram_send("🛑 Order blocked: an earlier trade is unresolved.")
            return

    reason = risk_stop_reason()
    if reason:
        turn_auto_off(reason)
        return

    if result_reader_thread is not None and result_reader_thread.is_alive():
        turn_auto_off("previous broker result request still running")
        return

    if not practice_verified or not verify_practice_mode(force_select=True):
        turn_auto_off("PRACTICE mode verification failed before order")
        telegram_send("🛑 ORDER BLOCKED: PRACTICE mode not verified.")
        return

    try:
        with api_lock:
            if not verify_practice_mode(force_select=True):
                turn_auto_off("PRACTICE verification failed before order")
                return

            with state_lock:
                if (
                    not state.get("auto_enabled")
                    or state.get("pending_trade")
                ):
                    telegram_send("AUTO is OFF or a trade is pending; no order.")
                    return

            accepted, order_id = IQ.buy(
                STAKE,
                signal["asset"],
                signal["direction"],
                EXPIRY_MINUTES,
            )

    except Exception as exc:
        logging.exception("Order request failed")
        telegram_send(f"⚠️ PRACTICE order request failed: {exc}")
        return

    if not accepted or order_id is None:
        with state_lock:
            state.setdefault("last_auto_candles", {})[
                signal["asset"]
            ] = signal["candle_id"]

        save_state()
        telegram_send(
            f"⚠️ IQ Option rejected the order for {signal['asset']}. "
            "No trade was counted."
        )
        return

    with state_lock:
        state.setdefault("last_auto_candles", {})[
            signal["asset"]
        ] = signal["candle_id"]

        state["pending_trade"] = {
            "asset": signal["asset"],
            "direction": signal["direction"],
            "order_id": str(order_id),
            "stake": STAKE,
            "expiry_minutes": EXPIRY_MINUTES,
            "candle_id": signal["candle_id"],
            "accepted_at": now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT"),
        }

    save_state()

    telegram_send(
        f"🟡 PRACTICE ORDER ACCEPTED\n"
        f"{signal['asset']} — {signal['direction'].upper()}\n"
        f"Stake: ${STAKE:.2f} | Expiry: {EXPIRY_MINUTES} minute(s)\n"
        f"Momentum 10 score: {signal['score']}/100\n"
        "Waiting for broker result. STOP AUTO cannot cancel this accepted order."
    )

    result_reader_result.clear()
    result_reader_event = threading.Event()

    result_reader_thread = threading.Thread(
        target=result_reader,
        args=(order_id,),
        name="broker-result-reader",
        daemon=True,
    )
    result_reader_thread.start()

    if result_reader_event.wait(RESULT_TIMEOUT_SECONDS):
        poll_late_result()
    else:
        turn_auto_off("broker result timeout; trade must be reconciled")
        telegram_send(
            "⚠️ RESULT PENDING. No outcome was guessed and no new order "
            "will be placed. Check IQ Option history, then send "
            "/reconcile -1.00 or /reconcile 0.82 using the exact NET P/L."
        )


# ================ STATUS AND TELEGRAM COMMANDS ================
def status_text():
    enforce_new_day()

    with state_lock:
        auto_on = bool(state.get("auto_enabled"))
        pnl = float(state.get("daily_pnl", 0))
        resolved = int(state.get("resolved_trades", 0))
        losses = int(state.get("consecutive_losses", 0))
        last_scan = state.get("last_scan", "Never")
        pending = state.get("pending_trade")

    lines = [
        "📊 MOMENTUM 10 SCANNER STATUS",
        f"Strategy: Momentum 10 ONLY",
        f"Mode: {'AUTO ON — PRACTICE' if auto_on else 'MANUAL / SCAN ONLY'}",
        f"PRACTICE check: {'VERIFIED' if practice_verified else 'NOT VERIFIED — ORDERS BLOCKED'}",
        f"Today P/L: ${pnl:+.2f}",
        f"Resolved trades: {resolved}/{MAX_RESOLVED_TRADES}",
        f"Consecutive losses: {losses}/{MAX_CONSECUTIVE_LOSSES}",
        f"Last scan: {last_scan}",
        format_rejections(),
    ]

    if pending:
        lines.append(
            f"⚠️ UNRESOLVED: {pending.get('asset', '?')} "
            f"{str(pending.get('direction', '?')).upper()}, "
            f"ID {pending.get('order_id', '?')}. AUTO is blocked."
        )

    reason = risk_stop_reason()
    if reason:
        lines.append(f"Risk stop: {reason}")

    return "\n".join(lines)


def handle_command(command, callback_id=None, message_text=""):
    global scan_requested, last_auto_scan

    command = str(command or "").upper().strip()

    if command == "START_AUTO":
        if not AUTO_TRADING_ALLOWED:
            turn_auto_off("AUTO trading disabled in code")
            answer = "Auto disabled in code"

        elif not connection_is_live():
            telegram_send("⚠️ IQ Option is disconnected; AUTO not started.")
            answer = "IQ Option disconnected"

        elif not verify_practice_mode(force_select=True):
            turn_auto_off("PRACTICE mode verification failed")
            telegram_send("🛑 START AUTO refused: PRACTICE not verified.")
            answer = "PRACTICE check failed"

        elif (
            result_reader_thread is not None
            and result_reader_thread.is_alive()
        ):
            telegram_send("🛑 Previous broker result request is still running.")
            answer = "Previous result still running"

        else:
            reason = risk_stop_reason()

            if reason:
                turn_auto_off(reason)
                answer = "Risk stop active"
            else:
                with state_lock:
                    state["auto_enabled"] = True

                save_state()
                last_auto_scan = 0.0

                telegram_send(
                    "▶️ AUTO STARTED — PRACTICE ONLY.\n"
                    "Momentum 10 only | $1 stake | 1-minute expiry | "
                    "scan interval 60 seconds | daily target +$5 | "
                    "daily loss limit -$3 | max 20 resolved trades | "
                    "max 5 consecutive losses.\n"
                    "AUTO stops if PRACTICE or a trade result cannot be verified."
                )
                answer = "AUTO started — PRACTICE only"

    elif command == "STOP_AUTO":
        turn_auto_off()
        answer = "AUTO stopped"

    elif command == "SCAN_MARKET":
        with state_lock:
            scan_requested = True

        telegram_send(
            "🔎 Manual scan queued. Momentum 10 only. "
            "This button never places orders."
        )
        answer = "Scan queued"

    elif command == "STATUS":
        telegram_send(status_text())
        answer = "Status sent"

    elif command == "RECONCILE":
        parts = str(message_text or "").strip().split()

        if len(parts) != 2:
            telegram_send(
                "Use /reconcile followed by the exact NET P/L from account "
                "history, e.g. /reconcile -1.00 or /reconcile 0.82."
            )
            answer = "Use /reconcile NET-P/L"

        else:
            profit = parse_profit(parts[1])

            with state_lock:
                pending = state.get("pending_trade")

            if profit is None or not isinstance(pending, dict):
                telegram_send(
                    "No unresolved trade to reconcile, or invalid P/L. Check STATUS."
                )
                answer = "Nothing to reconcile"

            elif complete_pending_trade(
                profit,
                source="manual reconciliation",
            ):
                telegram_send(
                    "✅ Trade reconciled using the net P/L you supplied. "
                    "AUTO remains OFF."
                )
                answer = "Trade reconciled"

            else:
                telegram_send("Reconciliation unchanged; check STATUS.")
                answer = "Reconciliation unchanged"

    else:
        answer = "Unknown command"

    if callback_id:
        telegram_answer_callback(callback_id, answer)


def process_telegram_updates():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram secrets are missing.")

    with state_lock:
        offset = int(state.get("last_update_id", 0)) + 1

    try:
        response = session.get(
            telegram_api("getUpdates"),
            params={
                "timeout": 10,
                "offset": offset,
                "allowed_updates": json.dumps(
                    ["message", "callback_query"]
                ),
            },
            timeout=15,
        )

        if not response.ok:
            logging.warning("Telegram polling HTTP error: %s", response.status_code)
            time.sleep(2)
            return

        data = response.json()

        if not data.get("ok"):
            logging.warning("Telegram polling API error: %s", str(data)[:180])
            time.sleep(2)
            return

        updates = data.get("result", [])

    except Exception as exc:
        logging.warning("Telegram polling error: %s", exc)
        time.sleep(2)
        return

    highest_processed = None

    for update in updates:
        callback = update.get("callback_query")

        if callback:
            message = callback.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            callback_id = callback.get("id", "")
            command = str(callback.get("data", "")).upper()

            logging.info("Telegram button received: %s", command)

            if chat_id == str(TELEGRAM_CHAT_ID):
                handle_command(command, callback_id=callback_id)
            else:
                telegram_answer_callback(callback_id, "Not authorized")

        else:
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = str(message.get("text", "")).strip()

            if chat_id == str(TELEGRAM_CHAT_ID) and text:
                lowered = text.lower()

                if lowered in ("/start", "/menu", "/help"):
                    telegram_send(
                        "MOMENTUM 10 SCANNER\n"
                        "Buttons: SCAN MARKET, START AUTO, STOP AUTO, STATUS.\n"
                        "Commands: /scan, /auto, /stop, /status, /stats.\n"
                        "For unresolved orders: /reconcile NET-P/L."
                    )
                elif lowered in ("/scan", "scan"):
                    handle_command("SCAN_MARKET")
                elif lowered in ("/status", "status", "/stats", "stats"):
                    handle_command("STATUS")
                elif lowered in ("/stop", "stop"):
                    handle_command("STOP_AUTO")
                elif lowered in ("/auto", "auto"):
                    handle_command("START_AUTO")
                elif lowered.startswith("/reconcile"):
                    handle_command("RECONCILE", message_text=text)

        highest_processed = max(
            int(update.get("update_id", 0)),
            highest_processed or 0,
        )

    if highest_processed is not None:
        with state_lock:
            state["last_update_id"] = max(
                int(state.get("last_update_id", 0)),
                highest_processed,
            )

        save_state()


def telegram_listener_loop():
    logging.info("Telegram listener started.")

    while True:
        try:
            process_telegram_updates()
        except Exception:
            logging.exception("Telegram listener error; retrying.")
            time.sleep(3)


# ================ MAIN LOOP ================
def main():
    global state, scan_requested
    global last_auto_scan, last_heartbeat, last_connect_attempt

    if BALANCE_MODE != "PRACTICE":
        raise RuntimeError("Safety stop: this scanner only permits PRACTICE.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets."
        )

    state = load_state()

    with state_lock:
        state["auto_enabled"] = False

    save_state()
    prepare_telegram()

    threading.Thread(
        target=telegram_listener_loop,
        name="telegram-listener",
        daemon=True,
    ).start()

    telegram_send(
        "✅ MOMENTUM 10 SCANNER ONLINE\n"
        "Momentum 20 is removed. AUTO is OFF after startup.\n"
        "SCAN MARKET only reports setups. START AUTO is a separate action "
        "and is restricted to verified PRACTICE mode."
    )

    last_connect_attempt = 0.0
    last_heartbeat = time.monotonic()

    while True:
        try:
            enforce_new_day()
            poll_late_result()

            if (
                not connection_is_live()
                and time.monotonic() - last_connect_attempt >= RECONNECT_SECONDS
            ):
                last_connect_attempt = time.monotonic()

                if not connect_iq():
                    logging.warning("IQ Option unavailable; retrying later.")

            if connection_is_live():
                with state_lock:
                    do_manual_scan = bool(scan_requested) and not busy

                    if do_manual_scan:
                        scan_requested = False

                if do_manual_scan:
                    logging.info("Manual market scan started.")
                    results = scan_market()

                    telegram_send(
                        format_signals(
                            results,
                            "🔎 MOMENTUM 10 — MANUAL SCAN",
                        )
                    )

                    logging.info(
                        "Manual scan completed: %d setups.",
                        len(results),
                    )

                    # Manual scans never submit orders.
                    last_auto_scan = time.monotonic()

                with state_lock:
                    auto_on = bool(state.get("auto_enabled"))

                if (
                    auto_on
                    and not busy
                    and time.monotonic() - last_auto_scan >= SCAN_INTERVAL_SECONDS
                ):
                    reason = risk_stop_reason()

                    if reason:
                        turn_auto_off(reason)

                    elif not practice_verified or not verify_practice_mode(
                        force_select=True
                    ):
                        turn_auto_off(
                            "PRACTICE mode could not be verified before AUTO scan"
                        )

                    else:
                        last_auto_scan = time.monotonic()
                        telegram_send(
                            "⏳ AUTO Momentum 10 scan started.",
                            keyboard=False,
                        )

                        results = scan_market()

                        with state_lock:
                            still_enabled = bool(state.get("auto_enabled"))
                            used_candles = dict(
                                state.get("last_auto_candles", {})
                            )

                        if not still_enabled:
                            telegram_send(
                                "⏹ AUTO was stopped during the scan. No order placed."
                            )

                        else:
                            fresh_results = [
                                item for item in results
                                if int(
                                    used_candles.get(item["asset"], -1)
                                ) != int(item["candle_id"])
                            ]

                            if fresh_results:
                                telegram_send(
                                    format_signals(
                                        fresh_results,
                                        "🤖 AUTO — MOMENTUM 10 SETUPS",
                                    )
                                )

                                place_and_monitor(fresh_results[0])

                            elif results:
                                telegram_send(
                                    "NO TRADE — setups were already attempted on "
                                    "these closed candles. Waiting for fresh candles."
                                )

                            else:
                                telegram_send(
                                    "NO TRADE — no setup passed Momentum 10 filters.\n"
                                    + format_rejections()
                                )

                        last_auto_scan = time.monotonic()

            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = time.monotonic()

                with state_lock:
                    auto_on = bool(state.get("auto_enabled"))
                    pending = state.get("pending_trade")
                    no_setups = (
                        int(state.get("last_scan_setups", 0) or 0) == 0
                    )

                telegram_send(
                    "💓 SCANNER HEARTBEAT\n"
                    f"{'NO TRADE — ' if no_setups else ''}"
                    f"Connection: {'connected' if connection_is_live() else 'disconnected'} | "
                    f"PRACTICE verified: {'yes' if practice_verified else 'no'} | "
                    f"AUTO: {'ON' if auto_on else 'OFF'}\n"
                    + (
                        "⚠️ Unresolved order: AUTO remains blocked.\n"
                        if pending else ""
                    )
                    + format_rejections()
                )

        except Exception:
            logging.exception("Main loop error")
            turn_auto_off("internal main-loop error")
            telegram_send(
                "⚠️ Internal scanner error. AUTO disabled; check Actions logs."
            )
            time.sleep(3)

        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        turn_auto_off("scanner interrupted")
        telegram_send("Scanner stopped. AUTO is OFF.")

    except Exception as exc:
        logging.exception("Fatal scanner error")

        try:
            turn_auto_off("fatal scanner error")
            telegram_send(
                f"🚨 FATAL SCANNER ERROR: {exc}\nAUTO has been disabled."
            )
        except Exception:
            pass

        raise
