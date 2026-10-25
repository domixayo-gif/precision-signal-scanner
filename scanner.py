# PRACTICE ONLY — this scanner refuses to run in REAL mode.
# Telegram inline buttons: START AUTO, STOP AUTO, SCAN MARKET, STATUS.
# Manual scans only report ranked setups and never place orders.
import os
import json
import time
import base64
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from iqoptionapi.stable_api import IQ_Option

# -------------------- SETTINGS --------------------
BALANCE_MODE = "PRACTICE"
AUTO_TRADE = True
STAKE = 1.0
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60
LOOKBACK_CANDLES = 50
MOMENTUM_PERIOD = 10
MOMENTUM_CONFIRM_PERIOD = 20
EXTREME_PERCENTILE = 0.08
MIN_TURN_DISTANCE = 0.06
DAILY_PROFIT_TARGET = 5.0
DAILY_MAX_LOSS = -3.0
MAX_RESOLVED_TRADES = 20
MAX_CONSECUTIVE_LOSSES = 5
SCAN_INTERVAL_SECONDS = 60
REQUEST_PAUSE_SECONDS = 0.12
TOP_SIGNALS = 3
STATE_FILE = "momentum_state.json"
LAGOS = ZoneInfo("Africa/Lagos")

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD",
    "HKD", "NOK", "SEK", "DKK", "MXN", "ZAR", "TRY", "PLN",
}

DEFAULT_STATE = {
    "date": "",
    "daily_pnl": 0.0,
    "resolved_trades": 0,
    "consecutive_losses": 0,
    "auto_enabled": False,
    "last_update_id": 0,
    "last_scan": "Never",
}

session = requests.Session()
IQ = None
state = DEFAULT_STATE.copy()
scan_requested = False
last_auto_scan = 0.0
busy = False
OPEN_TIME_CACHE = None
telegram_listener_thread = None
state_lock = threading.RLock()
scan_lock = threading.Lock()


def now_lagos():
    return datetime.now(LAGOS)


def day_key():
    return now_lagos().strftime("%Y-%m-%d")


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
        logging.info("Telegram not configured; message: %s", text)
        return

    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": str(text)}
    if keyboard:
        payload["reply_markup"] = json.dumps(control_keyboard(), ensure_ascii=False)

    try:
        response = session.post(
            telegram_api("sendMessage"),
            data=payload,
            timeout=20,
        )
        if not response.ok:
            logging.warning(
                "Telegram send failed (%s): %s",
                response.status_code,
                response.text[:300],
            )
    except Exception as exc:
        logging.warning("Telegram send error: %s", exc)


def telegram_answer_callback(callback_id, text="Done"):
    if not callback_id:
        return
    try:
        response = session.post(
            telegram_api("answerCallbackQuery"),
            data={"callback_query_id": callback_id, "text": str(text)[:190]},
            timeout=10,
        )
        if not response.ok:
            logging.warning("Callback answer failed: %s", response.text[:200])
    except Exception as exc:
        logging.warning("Callback answer error: %s", exc)


def github_contents_url():
    if not GITHUB_REPOSITORY:
        return ""
    return f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_FILE}"


def load_state():
    loaded = {}
    url = github_contents_url()

    if url and GITHUB_TOKEN:
        try:
            response = session.get(
                url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                params={"ref": GITHUB_REF_NAME},
                timeout=15,
            )
            if response.ok:
                content = response.json().get("content", "")
                if content:
                    loaded = json.loads(
                        base64.b64decode(content).decode("utf-8")
                    )
        except Exception as exc:
            logging.warning("Could not load GitHub state: %s", exc)

    if not loaded:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception:
            loaded = {}

    result = DEFAULT_STATE.copy()
    if isinstance(loaded, dict):
        result.update(loaded)

    if result.get("date") != day_key():
        result["date"] = day_key()
        result["daily_pnl"] = 0.0
        result["resolved_trades"] = 0
        result["consecutive_losses"] = 0

    result["auto_enabled"] = bool(result.get("auto_enabled", False)) and AUTO_TRADE
    result["last_update_id"] = int(result.get("last_update_id", 0) or 0)
    return result


def save_state():
    with state_lock:
        state["date"] = day_key()
        snapshot = dict(state)

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2)
    except Exception as exc:
        logging.warning("Local state save failed: %s", exc)

    url = github_contents_url()
    if not url or not GITHUB_TOKEN:
        return

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        current = session.get(
            url,
            headers=headers,
            params={"ref": GITHUB_REF_NAME},
            timeout=15,
        )
        sha = current.json().get("sha") if current.ok else None
        content = base64.b64encode(
            json.dumps(snapshot, indent=2).encode("utf-8")
        ).decode("ascii")

        body = {
            "message": "Update practice momentum scanner state",
            "content": content,
            "branch": GITHUB_REF_NAME,
        }
        if sha:
            body["sha"] = sha

        saved = session.put(
            url,
            headers=headers,
            json=body,
            timeout=20,
        )
        if not saved.ok:
            logging.warning(
                "GitHub state save failed (%s): %s",
                saved.status_code,
                saved.text[:250],
            )
    except Exception as exc:
        logging.warning("GitHub state save error: %s", exc)


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
        daily_pnl = float(state.get("daily_pnl", 0.0))
        resolved = int(state.get("resolved_trades", 0))
        losses = int(state.get("consecutive_losses", 0))

    if daily_pnl >= DAILY_PROFIT_TARGET:
        return f"daily profit target reached (+${DAILY_PROFIT_TARGET:.2f})"
    if daily_pnl <= DAILY_MAX_LOSS:
        return f"daily loss limit reached (${DAILY_MAX_LOSS:.2f})"
    if resolved >= MAX_RESOLVED_TRADES:
        return f"maximum resolved trades reached ({MAX_RESOLVED_TRADES})"
    if losses >= MAX_CONSECUTIVE_LOSSES:
        return f"maximum consecutive losses reached ({MAX_CONSECUTIVE_LOSSES})"
    return ""


def valid_currency_asset(name):
    clean = str(name).upper().replace("_", "")
    if clean.endswith("-OTC"):
        clean = clean[:-4]
    return (
        len(clean) == 6
        and clean.isalpha()
        and clean[:3] in CURRENCY_CODES
        and clean[3:] in CURRENCY_CODES
    )


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD repository secret.")

    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = IQ.connect()
    if not connected:
        raise RuntimeError(f"IQ Option connection failed: {reason}")

    IQ.change_balance("PRACTICE")
    time.sleep(2)

    if hasattr(IQ, "check_connect") and not IQ.check_connect():
        raise RuntimeError("IQ Option disconnected immediately after connecting.")

    logging.info("Connected to IQ Option PRACTICE balance.")


def get_asset_names():
    found = set()

    try:
        mapping = IQ.get_all_ACTIVES_OPCODE()
        if isinstance(mapping, dict):
            found.update(str(key) for key in mapping.keys())
    except Exception as exc:
        logging.warning("Asset list query failed: %s", exc)

    try:
        opcodes = getattr(IQ, "OP_code", None)
        actives = getattr(opcodes, "ACTIVES", None) if opcodes else None
        if isinstance(actives, dict):
            found.update(str(key) for key in actives.keys())
    except Exception:
        pass

    return sorted(name for name in found if valid_currency_asset(name))


def asset_is_open(asset):
    global OPEN_TIME_CACHE
    try:
        if OPEN_TIME_CACHE is None:
            OPEN_TIME_CACHE = IQ.get_all_open_time()

        opening = OPEN_TIME_CACHE
        saw_market_table = False

        for market_type in ("turbo", "binary"):
            market = opening.get(market_type, {}) if isinstance(opening, dict) else {}
            if market:
                saw_market_table = True
            row = market.get(asset)
            if isinstance(row, dict) and row.get("open"):
                return True

        return not saw_market_table
    except Exception as exc:
        logging.warning("Could not check open status for %s: %s", asset, exc)
        return True


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * fraction))
    return ordered[max(0, min(len(ordered) - 1, index))]


def candle_is_bullish_engulfing(previous, current):
    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def candle_is_bearish_engulfing(previous, current):
    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def get_closed_candles(asset):
    raw = IQ.get_candles(
        asset,
        CANDLE_SECONDS,
        LOOKBACK_CANDLES + MOMENTUM_CONFIRM_PERIOD + 5,
        time.time(),
    )

    if not raw or len(raw) < MOMENTUM_CONFIRM_PERIOD + 15:
        return []

    raw = sorted(raw, key=lambda item: item.get("from", 0))
    now_ts = time.time()
    closed = []

    for item in raw:
        stamp = float(item.get("from", 0) or 0)
        if stamp and stamp + CANDLE_SECONDS <= now_ts - 2:
            try:
                closed.append({
                    "open": float(item.get("open", 0)),
                    "close": float(item.get("close", 0)),
                    "high": float(item.get("max", item.get("high", 0))),
                    "low": float(item.get("min", item.get("low", 0))),
                    "from": stamp,
                })
            except (TypeError, ValueError):
                continue

    return closed[-(LOOKBACK_CANDLES + MOMENTUM_CONFIRM_PERIOD + 2):]


def momentum_percent(closes, period, end_index):
    start_index = end_index - period
    if start_index < 0 or closes[start_index] == 0:
        return None
    return ((closes[end_index] / closes[start_index]) - 1.0) * 100.0


def analyze_asset(asset):
    if not asset_is_open(asset):
        return None

    candles = get_closed_candles(asset)
    if len(candles) < MOMENTUM_CONFIRM_PERIOD + 15:
        return None

    closes = [c["close"] for c in candles]
    n = len(candles)
    current_index, previous_index = n - 1, n - 2

    current_m10 = momentum_percent(closes, MOMENTUM_PERIOD, current_index)
    previous_m10 = momentum_percent(closes, MOMENTUM_PERIOD, previous_index)
    current_m20 = momentum_percent(closes, MOMENTUM_CONFIRM_PERIOD, current_index)
    previous_m20 = momentum_percent(closes, MOMENTUM_CONFIRM_PERIOD, previous_index)

    if None in (current_m10, previous_m10, current_m20, previous_m20):
        return None

    history = []
    start = max(MOMENTUM_PERIOD, n - LOOKBACK_CANDLES)
    for i in range(start, current_index):
        value = momentum_percent(closes, MOMENTUM_PERIOD, i)
        if value is not None:
            history.append(value)

    if len(history) < 15:
        return None

    low_edge = percentile(history, EXTREME_PERCENTILE)
    high_edge = percentile(history, 1.0 - EXTREME_PERCENTILE)
    turn = current_m10 - previous_m10

    previous_candle, current_candle = candles[-2], candles[-1]
    bullish_engulf = candle_is_bullish_engulfing(previous_candle, current_candle)
    bearish_engulf = candle_is_bearish_engulfing(previous_candle, current_candle)
    m20_up = current_m20 > previous_m20
    m20_down = current_m20 < previous_m20

    direction = None
    if previous_m10 <= low_edge and turn >= MIN_TURN_DISTANCE and (m20_up or bullish_engulf):
        direction = "call"
        extreme_depth = max(0.0, low_edge - previous_m10)
        turn_strength = max(0.0, turn)
        confirmation_points = 20 if m20_up else 0
        engulf_points = 20 if bullish_engulf else 0
    elif previous_m10 >= high_edge and turn <= -MIN_TURN_DISTANCE and (m20_down or bearish_engulf):
        direction = "put"
        extreme_depth = max(0.0, previous_m10 - high_edge)
        turn_strength = max(0.0, -turn)
        confirmation_points = 20 if m20_down else 0
        engulf_points = 20 if bearish_engulf else 0
    else:
        return None

    depth_points = min(35.0, 15.0 + extreme_depth * 4.0)
    turn_points = min(25.0, 10.0 + turn_strength * 8.0)
    score = int(round(min(
        100.0,
        depth_points + turn_points + confirmation_points + engulf_points,
    )))

    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "m10": current_m10,
        "m20": current_m20,
        "turn": turn,
        "engulfing": bullish_engulf if direction == "call" else bearish_engulf,
        "timestamp": now_lagos().strftime("%H:%M:%S"),
    }


def scan_market():
    global busy, OPEN_TIME_CACHE

    if not scan_lock.acquire(blocking=False):
        telegram_send("⏳ A market scan is already running. Wait for that result.")
        return []

    busy = True
    signals = []
    OPEN_TIME_CACHE = None
    started = time.time()

    try:
        telegram_send("🔎 Reading IQ Option market and loading currency pairs…", keyboard=False)
        assets = get_asset_names()

        if not assets:
            logging.error("No valid currency assets found in IQ Option asset list.")
            telegram_send(
                "⚠️ Connected to IQ Option, but no valid currency-pair names "
                "were returned. Check the Actions log and API asset list."
            )
            return []

        logging.info("Scanning %d currency pairs.", len(assets))
        telegram_send(
            f"🔎 Market scan started: checking {len(assets)} currency pairs.",
            keyboard=False,
        )

        for index, asset in enumerate(assets, start=1):
            try:
                result = analyze_asset(asset)
                if result:
                    signals.append(result)
            except Exception as exc:
                logging.warning("Skip %s due to scan error: %s", asset, exc)

            if index % 10 == 0 or index == len(assets):
                logging.info(
                    "Market scan progress: %d/%d assets checked; %d valid setups.",
                    index, len(assets), len(signals),
                )
                telegram_send(
                    f"🔎 Scan progress: {index}/{len(assets)} pairs checked; "
                    f"{len(signals)} valid setups.",
                    keyboard=False,
                )

            time.sleep(REQUEST_PAUSE_SECONDS)

        signals.sort(
            key=lambda item: (item["score"], abs(item["turn"])),
            reverse=True,
        )

        with state_lock:
            state["last_scan"] = now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT")
        save_state()

        logging.info(
            "Market scan completed in %.1f seconds; %d valid setups.",
            time.time() - started, len(signals),
        )
        return signals

    except Exception as exc:
        logging.exception("Market scan failed")
        telegram_send(f"🚨 Market scan failed: {type(exc).__name__}: {exc}")
        return []

    finally:
        busy = False
        OPEN_TIME_CACHE = None
        scan_lock.release()


def format_signals(signals, title):
    if not signals:
        return (
            f"{title}\n\nNO TRADE — no valid setups passed the strategy filters. "
            "Weak signals will not be added just to fill the list."
        )

    lines = [
        title,
        "Ranked by setup score. Score is not win probability or a guarantee.",
        "",
    ]

    for position, signal in enumerate(signals[:TOP_SIGNALS], start=1):
        side = "CALL / UP" if signal["direction"] == "call" else "PUT / DOWN"
        candle_text = "Yes" if signal["engulfing"] else "No"
        lines.append(
            f"#{position} — {signal['asset']} — {side}\n"
            f"   SCORE: {signal['score']}/100 | M10: {signal['m10']:.3f}% | "
            f"M20: {signal['m20']:.3f}% | Engulfing: {candle_text}"
        )

    if len(signals) < TOP_SIGNALS:
        lines.append(
            f"\nOnly {len(signals)} qualifying setup(s) found; "
            "no fake scores were added."
        )

    return "\n".join(lines)


def status_text():
    enforce_new_day()
    reason = risk_stop_reason()

    with state_lock:
        mode = (
            "AUTO TRADING ON — PRACTICE"
            if state.get("auto_enabled")
            else "MANUAL / SCAN ONLY"
        )
        daily_pnl = float(state.get("daily_pnl", 0.0))
        resolved = int(state.get("resolved_trades", 0))
        losses = int(state.get("consecutive_losses", 0))
        last_scan = state.get("last_scan", "Never")

    lines = [
        "📊 PRACTICE SCANNER STATUS",
        f"Mode: {mode}",
        "Balance mode: PRACTICE (locked)",
        f"Today P/L: ${daily_pnl:+.2f}",
        f"Resolved trades: {resolved}/{MAX_RESOLVED_TRADES}",
        f"Consecutive losses: {losses}/{MAX_CONSECUTIVE_LOSSES}",
        f"Daily target/limit: +${DAILY_PROFIT_TARGET:.2f} / ${DAILY_MAX_LOSS:.2f}",
        f"Last scan: {last_scan}",
    ]

    if reason:
        lines.append(f"Risk stop: {reason}")

    return "\n".join(lines)


def place_and_monitor(signal):
    with state_lock:
        if not state.get("auto_enabled"):
            logging.info("Auto mode was stopped before order submission; skipping trade.")
            telegram_send("⏹ AUTO is OFF. Scan result received, but no order was placed.")
            return

    reason = risk_stop_reason()
    if reason:
        with state_lock:
            state["auto_enabled"] = False
        save_state()
        telegram_send(f"🛑 AUTO STOPPED: {reason}.\n\n{status_text()}")
        return

    with state_lock:
        if not state.get("auto_enabled"):
            telegram_send("⏹ AUTO is OFF. No new order was submitted.")
            return

        try:
            accepted, order_id = IQ.buy(
                STAKE, signal["asset"], signal["direction"], EXPIRY_MINUTES
            )
        except Exception as exc:
            telegram_send(f"⚠️ Practice order rejected for {signal['asset']}: {exc}")
            return

    if not accepted:
        telegram_send(
            f"⚠️ IQ Option rejected the PRACTICE order for {signal['asset']}. "
            "No trade was counted."
        )
        return

    telegram_send(
        f"🟡 PRACTICE ORDER ACCEPTED\n"
        f"{signal['asset']} — {signal['direction'].upper()}\n"
        f"Stake: ${STAKE:.2f} | Expiry: {EXPIRY_MINUTES} minute(s)\n"
        "Waiting for the result. STOP AUTO prevents new orders but cannot cancel "
        "this accepted trade."
    )

    profit = None
    deadline = time.time() + (EXPIRY_MINUTES * 60) + 35

    while time.time() < deadline:
        if hasattr(IQ, "check_win_v4"):
            try:
                profit = IQ.check_win_v4(order_id)
                if profit is not None:
                    break
            except Exception:
                pass
        time.sleep(2)

    if profit is None:
        telegram_send(
            "⚠️ Order was accepted, but IQ Option did not return its result. "
            "P/L was not guessed; check account history."
        )
        return

    try:
        profit = float(profit)
    except (TypeError, ValueError):
        telegram_send("⚠️ IQ Option returned an unreadable result. P/L was not guessed.")
        return

    enforce_new_day()

    with state_lock:
        state["daily_pnl"] = round(float(state.get("daily_pnl", 0.0)) + profit, 2)
        state["resolved_trades"] = int(state.get("resolved_trades", 0)) + 1

        if profit > 0:
            state["consecutive_losses"] = 0
            outcome = "WIN ✅"
        else:
            state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
            outcome = "LOSS ❌" if profit < 0 else "ZERO RESULT — COUNTED AS LOSS ❌"

        pnl = state["daily_pnl"]
        resolved = state["resolved_trades"]

    save_state()

    telegram_send(
        f"{outcome}\n{signal['asset']} — {signal['direction'].upper()}\n"
        f"Trade result: ${profit:+.2f}\nToday P/L: ${pnl:+.2f}\n"
        f"Resolved: {resolved}/{MAX_RESOLVED_TRADES}"
    )

    reason = risk_stop_reason()
    if reason:
        with state_lock:
            state["auto_enabled"] = False
        save_state()
        telegram_send(f"🛑 AUTO STOPPED: {reason}.\nManual scanning remains available.")


def prepare_telegram():
    """Verify Telegram and clear any webhook before using long polling."""
    try:
        response = session.post(
            telegram_api("deleteWebhook"),
            data={"drop_pending_updates": "false"},
            timeout=15,
        )
        data = response.json()

        if not response.ok or not data.get("ok"):
            raise RuntimeError(
                f"Telegram deleteWebhook failed: {response.text[:250]}"
            )

        me_response = session.get(telegram_api("getMe"), timeout=15)
        me_data = me_response.json()

        if not me_response.ok or not me_data.get("ok"):
            raise RuntimeError(f"Telegram getMe failed: {me_response.text[:250]}")

        bot_name = (me_data.get("result") or {}).get("username", "unknown")
        logging.info(
            "Telegram bot verified: @%s; webhook cleared for polling.",
            bot_name,
        )

    except Exception as exc:
        raise RuntimeError(f"Telegram setup failed: {exc}") from exc


def turn_auto_off(reason=None):
    with state_lock:
        was_on = bool(state.get("auto_enabled"))
        state["auto_enabled"] = False

    save_state()

    if reason:
        telegram_send(
            f"⏹ AUTO MODE STOPPED: {reason}. Manual scans remain available."
        )
    elif was_on:
        telegram_send(
            "⏹ AUTO MODE STOPPED. No new practice orders will be submitted. "
            "Any already accepted trade will still be monitored."
        )
    else:
        telegram_send(
            "⏹ AUTO MODE IS ALREADY OFF. Manual scans remain available."
        )


def handle_command(command, callback_id=None):
    global scan_requested
    command = str(command or "").upper()

    if command == "START_AUTO":
        reason = risk_stop_reason()

        if not AUTO_TRADE:
            with state_lock:
                state["auto_enabled"] = False
            save_state()
            telegram_send("AUTO_TRADE is disabled in the code. Manual scans are available.")
            answer = "Auto trading disabled"

        elif reason:
            with state_lock:
                state["auto_enabled"] = False
            save_state()
            telegram_send(f"🛑 Cannot start AUTO: {reason}.\nUse STATUS to check limits.")
            answer = "Risk limit reached"

        else:
            with state_lock:
                state["auto_enabled"] = True
            save_state()
            telegram_send(
                "▶️ AUTO MODE STARTED — PRACTICE ONLY. I will scan every 60 seconds "
                "and submit at most one top-ranked setup per scan, within the risk limits."
            )
            answer = "Auto started — practice only"

        if callback_id:
            telegram_answer_callback(callback_id, answer)

    elif command == "STOP_AUTO":
        turn_auto_off()
        if callback_id:
            telegram_answer_callback(callback_id, "Auto stopped")

    elif command == "SCAN_MARKET":
        scan_requested = True
        telegram_send(
            "🔎 MANUAL SCAN QUEUED. It will report up to three ranked setups "
            "and will not place any trades."
        )
        if callback_id:
            telegram_answer_callback(callback_id, "Scan queued")

    elif command == "STATUS":
        telegram_send(status_text())
        if callback_id:
            telegram_answer_callback(callback_id, "Status sent")

    else:
        if callback_id:
            telegram_answer_callback(callback_id, "Unknown button")


def process_telegram_updates():
    global scan_requested

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID repository secret."
        )

    try:
        # FIX: explicitly request callback_query updates for inline buttons.
        response = session.get(
            telegram_api("getUpdates"),
            params={
                "timeout": 10,
                "offset": int(state.get("last_update_id", 0)) + 1,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=15,
        )

        if not response.ok:
            logging.warning(
                "Telegram polling failed (%s): %s",
                response.status_code,
                response.text[:250],
            )
            time.sleep(3)
            return

        data = response.json()
        if not data.get("ok"):
            logging.warning("Telegram API returned an error: %s", str(data)[:250])
            time.sleep(3)
            return

        updates = data.get("result", [])

    except Exception as exc:
        logging.warning("Telegram polling error: %s", exc)
        time.sleep(3)
        return

    changed = False

    for update in updates:
        with state_lock:
            state["last_update_id"] = max(
                int(state.get("last_update_id", 0)),
                int(update.get("update_id", 0)),
            )
        changed = True

        callback = update.get("callback_query")

        if callback:
            message = callback.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            callback_id = callback.get("id", "")
            command = str(callback.get("data", "")).upper()

            logging.info("Telegram button callback received: %s", command)

            if chat_id != str(TELEGRAM_CHAT_ID):
                telegram_answer_callback(callback_id, "Not authorized")
                continue

            handle_command(command, callback_id)
            continue

        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text", "")).strip().lower()

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        logging.info("Telegram message received: %s", text[:100])

        if text in ("/start", "/menu"):
            telegram_send(
                "PRACTICE MOMENTUM SCANNER\n"
                "Use the four buttons below to control it."
            )
        elif text in ("/scan", "scan"):
            handle_command("SCAN_MARKET")
        elif text in ("/status", "status"):
            handle_command("STATUS")
        elif text in ("/stop", "stop"):
            handle_command("STOP_AUTO")
        elif text in ("/auto", "auto"):
            handle_command("START_AUTO")

    if changed:
        save_state()


def telegram_listener_loop():
    logging.info("Telegram listener thread started.")
    while True:
        try:
            process_telegram_updates()
        except Exception:
            logging.exception("Telegram listener iteration failed; retrying.")
            time.sleep(3)


def main():
    global scan_requested, last_auto_scan, telegram_listener_thread, state

    if BALANCE_MODE != "PRACTICE":
        raise RuntimeError("Safety stop: this file only permits PRACTICE mode.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in GitHub repository Secrets."
        )

    state = load_state()
    prepare_telegram()
    connect_iq()

    telegram_send(
        "✅ PRACTICE MOMENTUM SCANNER ONLINE\n"
        "Use the four clickable buttons below.\n"
        "Manual SCAN MARKET reports ranked setups only; it never places orders.\n"
        "START AUTO enables practice trading; STOP AUTO prevents new orders.\n"
        "Setup scores are not win probabilities. REAL trading is not enabled."
    )

    telegram_listener_thread = threading.Thread(
        target=telegram_listener_loop,
        name="telegram-listener",
        daemon=True,
    )
    telegram_listener_thread.start()
    logging.info("Telegram polling enabled; waiting for button commands.")

    while True:
        enforce_new_day()

        if scan_requested and not busy:
            scan_requested = False
            logging.info("Manual market scan started.")
            results = scan_market()
            telegram_send(
                format_signals(results, "🔎 TOP MARKET SETUPS — MANUAL MODE")
            )
            logging.info(
                "Manual market scan finished; %d qualified setups.",
                len(results),
            )

            if state.get("auto_enabled"):
                last_auto_scan = time.time()

        with state_lock:
            auto_is_enabled = bool(state.get("auto_enabled"))

        if auto_is_enabled and not busy:
            reason = risk_stop_reason()

            if reason:
                turn_auto_off(reason)

            elif time.time() - last_auto_scan >= SCAN_INTERVAL_SECONDS:
                last_auto_scan = time.time()
                telegram_send(
                    "⏳ AUTO scan started. Checking available currency pairs.",
                    keyboard=False,
                )
                logging.info("Automatic market scan started.")

                results = scan_market()

                with state_lock:
                    still_enabled = bool(state.get("auto_enabled"))

                if not still_enabled:
                    telegram_send(
                        "⏹ AUTO was stopped during the scan. No new order was placed."
                    )
                elif results:
                    telegram_send(format_signals(results, "🤖 AUTO SCAN — TOP SETUPS"))
                    place_and_monitor(results[0])
                else:
                    telegram_send(
                        "NO TRADE — no setup passed the filters in this auto scan. "
                        "No order placed."
                    )
                    logging.info("No valid auto setup this cycle.")

        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        try:
            with state_lock:
                state["auto_enabled"] = False
            save_state()
            telegram_send("Scanner stopped safely. Auto mode is off.")
        except Exception:
            pass

    except Exception as exc:
        logging.exception("Fatal scanner error")
        try:
            with state_lock:
                state["auto_enabled"] = False
            save_state()
            telegram_send(f"🚨 SCANNER ERROR: {exc}\nAuto mode has been disabled.")
        except Exception:
            pass
        raise
