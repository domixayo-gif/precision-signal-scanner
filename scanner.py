import os
import time
import json
import math
import logging
import threading
import traceback
import requests

from datetime import datetime, timezone
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# ZETA MOMENTUM 10 SCANNER
# PRACTICE ONLY — MANUAL SCAN NEVER PLACES TRADES
# ============================================================

BOT_NAME = "Momentum 10 IQ Option Scanner"

BALANCE_MODE = "PRACTICE"
AUTO_TRADING_ALLOWED = True

STAKE = 1.0
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60
LOOKBACK_CANDLES = 50

# MOMENTUM 10 ONLY
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

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


# ============================================================
# SKIP THE BROKEN DIGITAL-OPEN DISCOVERY THREAD
# ============================================================

def skip_digital_open(self):
    return


try:
    IQ_Option._IQ_Option__get_digital_open = skip_digital_open
except Exception:
    logging.warning(
        "Could not disable digital-open discovery; "
        "continuing with normal API initialization."
    )


# ============================================================
# GLOBAL STATE
# ============================================================

IQ = None
RUNNING = True
AUTO_MODE = False

STATE_LOCK = threading.RLock()
TELEGRAM_OFFSET = 0

OPEN_TRADES = {}
LAST_SIGNAL_BY_ASSET = {}
LAST_HEARTBEAT = 0

MANUAL_SCAN_REQUESTED = False
SCAN_IN_PROGRESS = False

STATE = {
    "date": "",
    "daily_pl": 0.0,
    "resolved": 0,
    "wins": 0,
    "losses": 0,
    "consecutive_losses": 0,
    "signals": 0,
    "executed": 0,
    "blocked": 0,
    "history": []
}


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def today_string():
    return utc_now().strftime("%Y-%m-%d")


def money(value):
    return f"${float(value):.2f}"


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except Exception:
        pass
    return default


def telegram_send(message, keyboard=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram unavailable. Message: %s", message)
        return

    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
        "disable_web_page_preview": True
    }

    if keyboard:
        payload["reply_markup"] = {
            "keyboard": [
                [
                    {"text": "START AUTO"},
                    {"text": "STOP AUTO"}
                ],
                [
                    {"text": "SCAN MARKET"},
                    {"text": "STATUS"}
                ]
            ],
            "resize_keyboard": True
        }

    try:
        response = requests.post(
            url, json=payload, timeout=15
        )
        response.raise_for_status()
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)


def save_state():
    with STATE_LOCK:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(STATE, file, indent=2)
        except Exception as exc:
            logging.warning("Local state save failed: %s", exc)

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return

    try:
        api_url = (
            "https://api.github.com/repos/"
            f"{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
        )

        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        existing = requests.get(
            api_url,
            headers=headers,
            params={"ref": GITHUB_REF_NAME},
            timeout=10
        )

        payload = {
            "message": "Update Momentum 10 scanner state",
            "branch": GITHUB_REF_NAME,
            "content": requests.utils.quote("")
        }

        # GitHub Contents API requires base64 content.
        import base64

        with STATE_LOCK:
            content = json.dumps(
                STATE, indent=2
            ).encode("utf-8")

        payload["content"] = base64.b64encode(
            content
        ).decode("ascii")

        if existing.status_code == 200:
            payload["sha"] = existing.json().get("sha")
        elif existing.status_code != 404:
            logging.warning(
                "GitHub state lookup returned %s",
                existing.status_code
            )
            return

        result = requests.put(
            api_url,
            headers=headers,
            json=payload,
            timeout=15
        )

        if result.status_code not in (200, 201):
            logging.warning(
                "GitHub state update failed: %s %s",
                result.status_code,
                result.text[:300]
            )

    except Exception as exc:
        logging.warning("GitHub state save failed: %s", exc)


def load_state():
    global STATE

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            loaded = json.load(file)

        if isinstance(loaded, dict):
            STATE.update(loaded)

    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.warning("State load failed: %s", exc)

    if STATE.get("date") != today_string():
        STATE["date"] = today_string()
        STATE["daily_pl"] = 0.0
        STATE["resolved"] = 0
        STATE["wins"] = 0
        STATE["losses"] = 0
        STATE["consecutive_losses"] = 0
        STATE["history"] = []

    save_state()


def daily_limit_reached():
    with STATE_LOCK:
        return (
            STATE["daily_pl"] >= DAILY_PROFIT_TARGET
            or STATE["daily_pl"] <= DAILY_MAX_LOSS
            or STATE["resolved"] >= MAX_RESOLVED_TRADES
            or STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES
        )


def daily_limit_reason():
    with STATE_LOCK:
        if STATE["daily_pl"] >= DAILY_PROFIT_TARGET:
            return "Daily profit target reached."
        if STATE["daily_pl"] <= DAILY_MAX_LOSS:
            return "Daily loss limit reached."
        if STATE["resolved"] >= MAX_RESOLVED_TRADES:
            return "Maximum resolved trades reached."
        if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            return "Maximum consecutive losses reached."
    return ""


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq():
    global IQ

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError(
            "Missing IQ_EMAIL or IQ_PASSWORD GitHub secret."
        )

    logging.info("Connecting to IQ Option...")

    candidate = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    result = candidate.connect()

    if result is False:
        raise RuntimeError("IQ Option connection failed.")

    for _ in range(15):
        try:
            if candidate.check_connect():
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("IQ Option connection timed out.")

    candidate.change_balance("PRACTICE")
    time.sleep(2)

    try:
        mode = str(candidate.get_balance_mode()).upper()
    except Exception:
        mode = ""

    if "REAL" in mode:
        raise RuntimeError(
            "Safety stop: IQ Option reports REAL account mode."
        )

    IQ = candidate

    try:
        balance = safe_float(IQ.get_balance())
    except Exception:
        balance = 0.0

    logging.info(
        "Connected. Practice balance: %s",
        money(balance)
    )

    telegram_send(
        "🟢 MOMENTUM 10 CONNECTED\n"
        "Account: PRACTICE ONLY\n"
        f"Balance: {money(balance)}\n"
        "AUTO MODE: OFF\n"
        "Expiry: 1 minute\n"
        "Stake: $1\n"
        "Manual scans never place trades."
    )


def ensure_connection():
    global IQ

    try:
        if IQ is not None and IQ.check_connect():
            return True
    except Exception:
        pass

    try:
        connect_iq()
        return True
    except Exception as exc:
        logging.error("Reconnect failed: %s", exc)
        return False


# ============================================================
# DISCOVER ASSETS
# ============================================================

def get_asset_names():
    """
    Discover symbols from IQ Option's active-symbol mapping.
    No currency-only filter: currency, crypto and index symbols
    can be considered if the account/API exposes them.
    """
    try:
        active_map = IQ.get_all_ACTIVES_OPCODE()

        if not isinstance(active_map, dict):
            logging.warning("Active-symbol mapping was not a dictionary.")
            return []

        names = []

        for symbol in active_map.keys():
            if not isinstance(symbol, str):
                continue

            symbol = symbol.strip()

            if not symbol:
                continue

            names.append(symbol)

        return sorted(set(names))

    except Exception as exc:
        logging.warning("Asset discovery failed: %s", exc)
        return []


# ============================================================
# CANDLE DATA
# ============================================================

def fetch_candles(asset):
    try:
        candles = IQ.get_candles(
            asset,
            CANDLE_SECONDS,
            LOOKBACK_CANDLES,
            int(time.time())
        )

        if not isinstance(candles, list):
            return []

        if len(candles) < MOMENTUM_PERIOD + 3:
            return []

        candles = sorted(
            candles,
            key=lambda candle: candle.get("from", 0)
        )

        cleaned = []

        for candle in candles:
            close = safe_float(candle.get("close"), float("nan"))
            open_price = safe_float(candle.get("open"), float("nan"))
            high = safe_float(candle.get("max"), float("nan"))
            low = safe_float(candle.get("min"), float("nan"))

            if not all(
                math.isfinite(value)
                for value in (close, open_price, high, low)
            ):
                continue

            if close <= 0 or high < low:
                continue

            cleaned.append({
                "open": open_price,
                "close": close,
                "high": high,
                "low": low,
                "from": candle.get("from", 0)
            })

        # Exclude the newest candle because it may still be forming.
        if len(cleaned) > 1:
            cleaned = cleaned[:-1]

        return cleaned

    except Exception as exc:
        logging.debug(
            "Candle request failed for %s: %s", asset, exc
        )
        return []


# ============================================================
# MOMENTUM 10 STRATEGY
# ============================================================

def calculate_signal(asset, candles):
    """
    Momentum 10:
    - Uses the last 10 completed candles.
    - Looks for an extreme directional move.
    - Requires a reversal candle and a minimum turn distance.
    - Does not treat the score as a probability of winning.
    """

    if len(candles) < MOMENTUM_PERIOD + 3:
        return None, "insufficient candles"

    closes = [c["close"] for c in candles]
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    current_close = closes[-1]
    previous_close = closes[-2]
    old_close = closes[-1 - MOMENTUM_PERIOD]

    if old_close <= 0:
        return None, "invalid reference price"

    momentum = (current_close - old_close) / old_close

    recent_ranges = [
        highs[i] - lows[i]
        for i in range(max(0, len(candles) - 14), len(candles))
    ]

    average_range = (
        sum(recent_ranges) / len(recent_ranges)
        if recent_ranges else 0.0
    )

    if average_range <= 0:
        return None, "no usable candle range"

    last_range = highs[-1] - lows[-1]

    if last_range <= 0:
        return None, "flat latest candle"

    body = abs(closes[-1] - opens[-1])
    body_ratio = body / last_range

    # Measure the latest candle's reversal from its extreme.
    upper_turn = (highs[-1] - current_close) / last_range
    lower_turn = (current_close - lows[-1]) / last_range

    # Keep the minimum-turn requirement explicit.
    if momentum < 0:
        # A strong downward move needs a bullish reversal candle.
        bullish = current_close > opens[-1]
        turn_distance = lower_turn

        if not bullish:
            return None, "no bullish reversal after downward momentum"

        if turn_distance < MIN_TURN_DISTANCE:
            return None, "reversal distance too small"

        direction = "call"

    elif momentum > 0:
        # A strong upward move needs a bearish reversal candle.
        bearish = current_close < opens[-1]
        turn_distance = upper_turn

        if not bearish:
            return None, "no bearish reversal after upward momentum"

        if turn_distance < MIN_TURN_DISTANCE:
            return None, "reversal distance too small"

        direction = "put"

    else:
        return None, "zero momentum"

    # Momentum must be meaningfully larger than recent candle noise.
    normalized_momentum = (
        abs(current_close - old_close) / average_range
    )

    if normalized_momentum < EXTREME_PERCENTILE:
        return None, "momentum not extreme enough"

    # Do not accept a reversal if the latest close has not moved
    # in the expected direction relative to the preceding close.
    if direction == "call" and current_close <= previous_close:
        return None, "bullish reversal lacks close confirmation"

    if direction == "put" and current_close >= previous_close:
        return None, "bearish reversal lacks close confirmation"

    score = 50

    if normalized_momentum >= 0.5:
        score += 10
    if normalized_momentum >= 1.0:
        score += 10
    if body_ratio >= 0.4:
        score += 10
    if body_ratio >= 0.65:
        score += 10
    if turn_distance >= 0.3:
        score += 5
    if turn_distance >= 0.5:
        score += 5

    signal = {
        "asset": asset,
        "direction": direction,
        "score": min(score, 100),
        "momentum": momentum,
        "normalized_momentum": normalized_momentum,
        "body_ratio": body_ratio,
        "turn_distance": turn_distance,
        "price": current_close,
        "time": utc_now().strftime("%H:%M:%S UTC")
    }

    return signal, ""


# ============================================================
# MARKET SCAN
# ============================================================

def scan_market(manual=False):
    global SCAN_IN_PROGRESS

    with STATE_LOCK:
        if SCAN_IN_PROGRESS:
            return []
        SCAN_IN_PROGRESS = True

    try:
        if not ensure_connection():
            if manual:
                telegram_send("❌ Scan failed: IQ Option is disconnected.")
            return []

        assets = get_asset_names()

        if not assets:
            message = (
                "⚠️ No assets returned by IQ Option.\n"
                "The API may not have initialized its active-symbol list."
            )
            logging.warning(message)

            if manual:
                telegram_send(message)

            return []

        logging.info(
            "Scanning %d symbols with Momentum 10.", len(assets)
        )

        results = []
        rejected = {}
        scanned = 0

        for index, asset in enumerate(assets, start=1):
            if not RUNNING:
                break

            candles = fetch_candles(asset)
            scanned += 1

            if not candles:
                rejected["no candles/unavailable"] = (
                    rejected.get("no candles/unavailable", 0) + 1
                )
                time.sleep(REQUEST_PAUSE_SECONDS)
                continue

            signal, reason = calculate_signal(asset, candles)

            if signal:
                results.append(signal)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1

            if index % PROGRESS_EVERY == 0:
                logging.info(
                    "Scan progress: %d/%d; candidates=%d",
                    index, len(assets), len(results)
                )

            time.sleep(REQUEST_PAUSE_SECONDS)

        results.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        logging.info(
            "Scan finished: scanned=%d candidates=%d",
            scanned, len(results)
        )

        if manual:
            send_manual_scan_results(results, scanned, rejected)

        return results

    except Exception as exc:
        logging.error("Market scan failed: %s", exc)
        logging.debug(traceback.format_exc())

        if manual:
            telegram_send(
                f"❌ Market scan error: {str(exc)[:250]}"
            )

        return []

    finally:
        with STATE_LOCK:
            SCAN_IN_PROGRESS = False


def send_manual_scan_results(results, scanned, rejected):
    lines = [
        "🔎 MANUAL MARKET SCAN",
        "Mode: REPORT ONLY — NO TRADES",
        f"Symbols checked: {scanned}",
        f"Candidates: {len(results)}",
        ""
    ]

    if results:
        lines.append("TOP MOMENTUM 10 SETUPS:")

        for signal in results[:TOP_SIGNALS]:
            lines.append(
                f"{signal['asset']} | "
                f"{signal['direction'].upper()} | "
                f"Score {signal['score']}/100 | "
                f"Momentum {signal['normalized_momentum']:.2f}"
            )
    else:
        lines.append("NO TRADE — no qualifying setups.")

    if rejected:
        lines.append("")
        lines.append("REJECTION SUMMARY:")

        for reason, count in sorted(
            rejected.items(),
            key=lambda item: item[1],
            reverse=True
        )[:5]:
            lines.append(f"• {reason}: {count}")

    telegram_send("\n".join(lines))


# ============================================================
# PRACTICE-ONLY ORDER PROTECTION
# ============================================================

def practice_mode_verified():
    try:
        mode = str(IQ.get_balance_mode()).upper()
        return "PRACTICE" in mode
    except Exception:
        return False


def execute_signal(signal):
    global AUTO_MODE

    if not AUTO_TRADING_ALLOWED:
        return False

    if not AUTO_MODE:
        return False

    if daily_limit_reached():
        AUTO_MODE = False
        telegram_send(
            "🛑 AUTO STOPPED\n" + daily_limit_reason()
        )
        return False

    if not practice_mode_verified():
        AUTO_MODE = False
        telegram_send(
            "🚨 SAFETY STOP\n"
            "Could not verify PRACTICE mode. "
            "No order was sent."
        )
        return False

    asset = signal["asset"]
    direction = signal["direction"]

    # Avoid repeated orders on the same asset within one candle.
    candle_key = (
        asset,
        int(time.time() // CANDLE_SECONDS)
    )

    if candle_key in LAST_SIGNAL_BY_ASSET:
        return False

    signal_id = (
        f"M10-{asset}-{direction.upper()}-"
        f"{int(time.time())}"
    )

    try:
        # Re-check the mode immediately before sending an order.
        if not practice_mode_verified():
            AUTO_MODE = False
            telegram_send("🚨 Practice-mode verification failed.")
            return False

        success, order_id = IQ.buy(
            STAKE,
            asset,
            direction,
            EXPIRY_MINUTES
        )

        if not success:
            with STATE_LOCK:
                STATE["blocked"] += 1
            logging.warning("Order rejected: %s", asset)
            return False

        LAST_SIGNAL_BY_ASSET[candle_key] = True

        with STATE_LOCK:
            OPEN_TRADES[str(order_id)] = {
                "signal_id": signal_id,
                "asset": asset,
                "direction": direction,
                "stake": STAKE,
                "score": signal["score"],
                "started": time.time()
            }

            STATE["executed"] += 1
            STATE["signals"] += 1

        save_state()

        telegram_send(
            "🟡 PRACTICE TRADE OPENED\n"
            f"Signal ID: {signal_id}\n"
            f"Asset: {asset}\n"
            f"Direction: {direction.upper()}\n"
            f"Stake: {money(STAKE)}\n"
            f"Expiry: {EXPIRY_MINUTES} minute\n"
            f"Score: {signal['score']}/100\n"
            "Reminder: score is not win probability."
        )

        logging.info(
            "Practice order accepted: %s id=%s",
            asset, order_id
        )

        return True

    except Exception as exc:
        logging.error(
            "Order error for %s: %s", asset, exc
        )
        return False


# ============================================================
# TRADE RESULT TRACKING
# ============================================================

def reconcile_trades():
    if not OPEN_TRADES or not ensure_connection():
        return

    for order_id in list(OPEN_TRADES.keys()):
        trade = OPEN_TRADES.get(order_id)

        if not trade:
            continue

        if time.time() - trade["started"] < EXPIRY_MINUTES * 60:
            continue

        try:
            result = IQ.check_win_v4(int(order_id))
        except Exception as exc:
            logging.warning(
                "Result check failed for %s: %s",
                order_id, exc
            )
            continue

        if result is None:
            if time.time() - trade["started"] > RESULT_TIMEOUT_SECONDS:
                logging.warning(
                    "Result still pending for order %s",
                    order_id
                )
            continue

        result = safe_float(result, float("nan"))

        if not math.isfinite(result):
            continue

        with STATE_LOCK:
            OPEN_TRADES.pop(order_id, None)

            STATE["resolved"] += 1
            STATE["daily_pl"] += result

            if result > 0:
                STATE["wins"] += 1
                STATE["consecutive_losses"] = 0
                outcome = "WIN"
            elif result < 0:
                STATE["losses"] += 1
                STATE["consecutive_losses"] += 1
                outcome = "LOSS"
            else:
                outcome = "BREAKEVEN"

            STATE["history"].append({
                "signal_id": trade["signal_id"],
                "asset": trade["asset"],
                "direction": trade["direction"],
                "outcome": outcome,
                "net": result,
                "time": utc_now().isoformat()
            })

            STATE["history"] = STATE["history"][-200:]

        save_state()

        telegram_send(
            f"📊 TRADE RESULT: {outcome}\n"
            f"Signal ID: {trade['signal_id']}\n"
            f"Asset: {trade['asset']}\n"
            f"Direction: {trade['direction'].upper()}\n"
            f"Net result: {money(result)}\n"
            f"Daily P/L: {money(STATE['daily_pl'])}\n"
            f"Resolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}"
        )

        if daily_limit_reached():
            global AUTO_MODE
            AUTO_MODE = False
            telegram_send(
                "🛑 AUTO MODE OFF\n" + daily_limit_reason()
            )


# ============================================================
# STATUS AND TELEGRAM COMMANDS
# ============================================================

def status_message():
    try:
        balance = safe_float(IQ.get_balance()) if IQ else 0.0
    except Exception:
        balance = 0.0

    with STATE_LOCK:
        return (
            "📊 MOMENTUM 10 STATUS\n"
            "Account: PRACTICE ONLY\n"
            f"Balance: {money(balance)}\n"
            f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
            f"Daily P/L: {money(STATE['daily_pl'])}\n"
            f"Wins: {STATE['wins']}\n"
            f"Losses: {STATE['losses']}\n"
            f"Resolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}\n"
            f"Open trades: {len(OPEN_TRADES)}\n"
            f"Signals: {STATE['signals']}\n"
            f"Executed: {STATE['executed']}\n"
            f"Blocked: {STATE['blocked']}\n"
            f"Consecutive losses: {STATE['consecutive_losses']}\n"
            f"Daily stop: {daily_limit_reason() or 'Not reached'}"
        )


def process_telegram_update(update):
    global AUTO_MODE, MANUAL_SCAN_REQUESTED

    message = update.get("message") or update.get("callback_query", {}).get("message")

    if not message:
        return

    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))

    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return

    text = (
        message.get("text")
        or update.get("callback_query", {}).get("data")
        or ""
    ).strip()

    if not text:
        return

    upper = text.upper()

    logging.info("Telegram command received: %s", upper)

    if upper in ("/START", "START AUTO"):
        if daily_limit_reached():
            telegram_send(
                "Cannot start AUTO.\n" + daily_limit_reason()
            )
            return

        if not practice_mode_verified():
            telegram_send(
                "AUTO refused: PRACTICE mode could not be verified."
            )
            return

        AUTO_MODE = True
        telegram_send(
            "🟢 AUTO MODE ON\n"
            "Practice account only.\n"
            "Stake: $1 | Expiry: 1 minute\n"
            "Momentum 10 only."
        )

    elif upper in ("/STOP", "STOP AUTO"):
        AUTO_MODE = False
        telegram_send("🔴 AUTO MODE OFF. Scanning can continue.")

    elif upper in ("SCAN MARKET", "/SCAN"):
        MANUAL_SCAN_REQUESTED = True
        telegram_send(
            "🔎 Manual scan queued. It will report setups only; "
            "no trades will be placed."
        )

    elif upper in ("STATUS", "/STATUS"):
        telegram_send(status_message())

    elif upper.startswith("/STATS"):
        with STATE_LOCK:
            total = STATE["wins"] + STATE["losses"]
            win_rate = (
                STATE["wins"] / total * 100
                if total else 0.0
            )

            telegram_send(
                "📈 MOMENTUM 10 STATS\n"
                f"Wins: {STATE['wins']}\n"
                f"Losses: {STATE['losses']}\n"
                f"Win rate: {win_rate:.2f}%\n"
                f"Daily P/L: {money(STATE['daily_pl'])}\n"
                f"Resolved: {STATE['resolved']}"
            )

    elif upper.startswith("/RECONCILE"):
        telegram_send(
            "Automatic reconciliation is active for trades "
            "opened during this workflow run. Use /stats for results."
        )


def telegram_listener():
    global TELEGRAM_OFFSET

    if not TELEGRAM_TOKEN:
        logging.warning("TELEGRAM_TOKEN missing; listener disabled.")
        return

    base_url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    )

    while RUNNING:
        try:
            response = requests.get(
                f"{base_url}/getUpdates",
                params={
                    "offset": TELEGRAM_OFFSET,
                    "timeout": 20
                },
                timeout=25
            )

            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                TELEGRAM_OFFSET = update["update_id"] + 1
                process_telegram_update(update)

        except Exception as exc:
            logging.warning("Telegram listener error: %s", exc)
            time.sleep(5)


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():
    global LAST_HEARTBEAT

    if time.time() - LAST_HEARTBEAT < HEARTBEAT_SECONDS:
        return

    LAST_HEARTBEAT = time.time()

    with STATE_LOCK:
        message = (
            "💓 MOMENTUM 10 HEARTBEAT\n"
            "Account: PRACTICE ONLY\n"
            f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
            f"Daily P/L: {money(STATE['daily_pl'])}\n"
            f"Resolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}\n"
            f"W/L: {STATE['wins']}/{STATE['losses']}\n"
            f"Open trades: {len(OPEN_TRADES)}\n"
            f"Signals: {STATE['signals']}\n"
            f"Executed: {STATE['executed']}\n"
            f"Blocked: {STATE['blocked']}\n"
            "No qualifying setup means NO TRADE."
        )

    telegram_send(message)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    global AUTO_MODE, MANUAL_SCAN_REQUESTED

    # Required: every workflow restart begins with AUTO OFF.
    AUTO_MODE = False

    load_state()

    telegram_send(
        "🚀 MOMENTUM 10 STARTING\n"
        "Automatic trading starts OFF.\n"
        "Practice account only.\n"
        "Manual SCAN MARKET never places trades."
    )

    listener = threading.Thread(
        target=telegram_listener,
        daemon=True
    )
    listener.start()

    while RUNNING:
        try:
            if not ensure_connection():
                time.sleep(RECONNECT_SECONDS)
                continue

            reconcile_trades()

            manual_scan = MANUAL_SCAN_REQUESTED

            if manual_scan:
                MANUAL_SCAN_REQUESTED = False

            # One scan services either the manual request or
            # the normal automatic scan cycle.
            results = scan_market(manual=manual_scan)

            if AUTO_MODE and not manual_scan and not daily_limit_reached():
                if results:
                    # Only the highest-ranked qualifying setup is
                    # considered in each automatic scan cycle.
                    execute_signal(results[0])
                else:
                    logging.info("NO TRADE: no qualifying setup.")

            heartbeat()
            save_state()

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            break

        except Exception as exc:
            logging.error("Main loop error: %s", exc)
            logging.debug(traceback.format_exc())
            time.sleep(RECONNECT_SECONDS)

    AUTO_MODE = False
    telegram_send("Scanner stopped. AUTO MODE OFF.")


if __name__ == "__main__":
    main()
