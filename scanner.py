# PRACTICE ONLY — never switch this bot to REAL.
# Telegram buttons: START AUTO, STOP AUTO, SCAN MARKET, STATUS
# Manual scans only report ranked setups. They never place orders.

import os
import re
import json
import time
import base64
import logging
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
MIN_TURN_DISTANCE = 0.06  # percentage-point momentum turn, not a win probability
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD",
    "SGD", "HKD", "NOK", "SEK", "DKK", "MXN", "ZAR", "TRY", "PLN",
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


def now_lagos():
    return datetime.now(LAGOS)


def day_key():
    return now_lagos().strftime("%Y-%m-%d")


def telegram_api(method):
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def telegram_send(text, keyboard=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram not configured; message: %s", text)
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps({
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
        })
    try:
        response = session.post(telegram_api("sendMessage"), data=payload, timeout=20)
        if not response.ok:
            logging.warning("Telegram send failed: %s", response.text[:300])
    except Exception as exc:
        logging.warning("Telegram send error: %s", exc)


def telegram_answer_callback(callback_id, text="Done"):
    try:
        session.post(
            telegram_api("answerCallbackQuery"),
            data={"callback_query_id": callback_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def github_contents_url():
    if not GITHUB_REPOSITORY:
        return ""
    return f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_FILE}"


def load_state():
    loaded = {}
    # Prefer the saved GitHub file, so daily limits survive workflow restarts.
    url = github_contents_url()
    if url and GITHUB_TOKEN:
        try:
            response = session.get(
                url,
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
                params={"ref": GITHUB_REF_NAME},
                timeout=15,
            )
            if response.ok:
                content = response.json().get("content", "")
                decoded = base64.b64decode(content).decode("utf-8")
                loaded = json.loads(decoded)
        except Exception as exc:
            logging.warning("Could not load GitHub state: %s", exc)
    if not loaded:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception:
            loaded = {}
    result = DEFAULT_STATE.copy()
    result.update(loaded if isinstance(loaded, dict) else {})
    if result.get("date") != day_key():
        result["date"] = day_key()
        result["daily_pnl"] = 0.0
        result["resolved_trades"] = 0
        result["consecutive_losses"] = 0
    result["auto_enabled"] = bool(result.get("auto_enabled", False)) and AUTO_TRADE
    result["last_update_id"] = int(result.get("last_update_id", 0) or 0)
    return result


def save_state():
    state["date"] = day_key()
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
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
        current = session.get(url, headers=headers, params={"ref": GITHUB_REF_NAME}, timeout=15)
        sha = current.json().get("sha") if current.ok else None
        content = base64.b64encode(json.dumps(state, indent=2).encode("utf-8")).decode("ascii")
        body = {
            "message": "Update practice momentum scanner state",
            "content": content,
            "branch": GITHUB_REF_NAME,
        }
        if sha:
            body["sha"] = sha
        saved = session.put(url, headers=headers, json=body, timeout=20)
        if not saved.ok:
            logging.warning("GitHub state save failed (%s): %s", saved.status_code, saved.text[:250])
    except Exception as exc:
        logging.warning("GitHub state save error: %s", exc)


def enforce_new_day():
    if state.get("date") != day_key():
        state["date"] = day_key()
        state["daily_pnl"] = 0.0
        state["resolved_trades"] = 0
        state["consecutive_losses"] = 0
        save_state()


def risk_stop_reason():
    enforce_new_day()
    if state["daily_pnl"] >= DAILY_PROFIT_TARGET:
        return f"daily profit target reached (+${DAILY_PROFIT_TARGET:.2f})"
    if state["daily_pnl"] <= DAILY_MAX_LOSS:
        return f"daily loss limit reached (${DAILY_MAX_LOSS:.2f})"
    if state["resolved_trades"] >= MAX_RESOLVED_TRADES:
        return f"maximum resolved trades reached ({MAX_RESOLVED_TRADES})"
    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        return f"maximum consecutive losses reached ({MAX_CONSECUTIVE_LOSSES})"
    return ""


def valid_currency_asset(name):
    clean = str(name).upper().replace("_", "")
    if clean.endswith("-OTC"):
        clean = clean[:-4]
    if len(clean) != 6 or not clean.isalpha():
        return False
    return clean[:3] in CURRENCY_CODES and clean[3:] in CURRENCY_CODES


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD repository secret.")
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = IQ.connect()
    if not connected:
        raise RuntimeError(f"IQ Option connection failed: {reason}")
    # PRACTICE is hard-coded intentionally. No REAL option is present.
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
    # Load the market-open table once per scan, not once per asset.
    # Repeated calls here can make the scanner appear frozen for minutes.
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
        # If the API didn't provide a usable market table, let candle query decide.
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
    raw = IQ.get_candles(asset, CANDLE_SECONDS, LOOKBACK_CANDLES + MOMENTUM_CONFIRM_PERIOD + 5, time.time())
    if not raw or len(raw) < MOMENTUM_CONFIRM_PERIOD + 15:
        return []
    raw = sorted(raw, key=lambda item: item.get("from", 0))
    now_ts = time.time()
    closed = []
    for item in raw:
        stamp = float(item.get("from", 0) or 0)
        # Don't use a candle that is still forming.
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
    current_index = n - 1
    previous_index = n - 2
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
    previous_candle = candles[-2]
    current_candle = candles[-1]
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

    # This is a relative setup-strength score, NOT a probability of winning.
    depth_points = min(35.0, 15.0 + extreme_depth * 4.0)
    turn_points = min(25.0, 10.0 + turn_strength * 8.0)
    score = int(round(min(100.0, depth_points + turn_points + confirmation_points + engulf_points)))
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
    if busy:
        telegram_send("⏳ A market scan is already running. Please wait for its result.")
        return []
    busy = True
    signals = []
    OPEN_TIME_CACHE = None
    started = time.time()
    try:
        telegram_send("🔎 Reading IQ Option market and loading currency pairs…")
        assets = get_asset_names()
        if not assets:
            telegram_send("⚠️ IQ Option connected, but no currency-pair names were returned. Check the API asset list in the Actions log.")
            logging.error("No valid currency assets found in IQ Option asset list.")
            return []
        logging.info("Scanning %d currency pairs.", len(assets))
        telegram_send(f"🔎 Market scan started: checking {len(assets)} currency pairs. I'll send progress while scanning.")
        for index, asset in enumerate(assets, start=1):
            try:
                result = analyze_asset(asset)
                if result:
                    signals.append(result)
            except Exception as exc:
                logging.warning("Skip %s due to scan error: %s", asset, exc)
            if index % 10 == 0 or index == len(assets):
                logging.info("Market scan progress: %d/%d assets checked; %d valid setups.", index, len(assets), len(signals))
                telegram_send(f"🔎 Market scan progress: {index}/{len(assets)} pairs checked. Valid setups found: {len(signals)}.", keyboard=False)
            time.sleep(REQUEST_PAUSE_SECONDS)
        signals.sort(key=lambda item: (item["score"], abs(item["turn"])), reverse=True)
        state["last_scan"] = now_lagos().strftime("%Y-%m-%d %H:%M:%S WAT")
        save_state()
        logging.info("Market scan completed in %.1f seconds; %d valid setups.", time.time() - started, len(signals))
        return signals
    except Exception as exc:
        logging.exception("Market scan failed")
        telegram_send(f"🚨 Market scan failed: {type(exc).__name__}: {exc}")
        return []
    finally:
        busy = False
        OPEN_TIME_CACHE = None


def format_signals(signals, title):
    if not signals:
        return f"{title}\n\nNo valid setups passed the strategy filters. I won't fill the list with weak signals."
    lines = [
        title,
        "Setup score = strategy strength, NOT win probability or a guaranteed result.",
        "",
    ]
    for position, signal in enumerate(signals[:TOP_SIGNALS], start=1):
        side = "CALL / UP" if signal["direction"] == "call" else "PUT / DOWN"
        candle_text = "Yes" if signal["engulfing"] else "No"
        lines.append(
            f"{position}. {signal['asset']} — {side}\n"
            f"   Setup score: {signal['score']}/100 | M10: {signal['m10']:.3f}% | "
            f"M20: {signal['m20']:.3f}% | Engulfing: {candle_text}"
        )
    return "\n".join(lines)


def status_text():
    enforce_new_day()
    mode = "AUTO TRADING ON" if state.get("auto_enabled") else "MANUAL / SCAN ONLY"
    reason = risk_stop_reason()
    lines = [
        "📊 PRACTICE SCANNER STATUS",
        f"Mode: {mode}",
        f"Balance mode: {BALANCE_MODE}",
        f"Today P/L: ${float(state['daily_pnl']):+.2f}",
        f"Resolved trades: {state['resolved_trades']}/{MAX_RESOLVED_TRADES}",
        f"Consecutive losses: {state['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}",
        f"Daily target/limit: +${DAILY_PROFIT_TARGET:.2f} / ${DAILY_MAX_LOSS:.2f}",
        f"Last scan: {state.get('last_scan', 'Never')}",
    ]
    if reason:
        lines.append(f"Risk stop: {reason}")
    return "\n".join(lines)


def place_and_monitor(signal):
    # Risk is checked immediately before every order. Only one new order is sent per scan.
    reason = risk_stop_reason()
    if reason:
        state["auto_enabled"] = False
        save_state()
        telegram_send(f"🛑 AUTO STOPPED: {reason}.\n\n{status_text()}")
        return
    try:
        # PRACTICE mode was selected during connect_iq(); there is no REAL switch.
        accepted, order_id = IQ.buy(STAKE, signal["asset"], signal["direction"], EXPIRY_MINUTES)
    except Exception as exc:
        telegram_send(f"⚠️ Order rejected for {signal['asset']}: {exc}")
        return
    if not accepted:
        telegram_send(
            f"⚠️ IQ Option rejected the practice order for {signal['asset']}.\n"
            "The asset may not be tradable right now. No trade was counted."
        )
        return

    telegram_send(
        f"🟡 PRACTICE ORDER ACCEPTED\n{signal['asset']} — {signal['direction'].upper()}\n"
        f"Stake: ${STAKE:.2f} | Expiry: {EXPIRY_MINUTES} minute(s)\n"
        "Waiting for the result."
    )
    profit = None
    # Keep trying the common iqoptionapi result methods; the bot remains in practice mode.
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
        telegram_send("⚠️ Order was accepted, but its result was not returned. P/L was not guessed; check IQ Option history.")
        return

    try:
        profit = float(profit)
    except (TypeError, ValueError):
        telegram_send("⚠️ IQ Option returned an unreadable result. P/L was not guessed.")
        return

    enforce_new_day()
    state["daily_pnl"] = round(float(state["daily_pnl"]) + profit, 2)
    state["resolved_trades"] = int(state["resolved_trades"]) + 1
    # As requested, a $0 result counts as a loss.
    if profit > 0:
        state["consecutive_losses"] = 0
        outcome = "WIN ✅"
    else:
        state["consecutive_losses"] = int(state["consecutive_losses"]) + 1
        outcome = "LOSS ❌" if profit < 0 else "ZERO RESULT — COUNTED AS LOSS ❌"
    save_state()
    telegram_send(
        f"{outcome}\n{signal['asset']} — {signal['direction'].upper()}\n"
        f"Trade result: ${profit:+.2f}\nToday P/L: ${state['daily_pnl']:+.2f}\n"
        f"Resolved: {state['resolved_trades']}/{MAX_RESOLVED_TRADES}"
    )
    reason = risk_stop_reason()
    if reason:
        state["auto_enabled"] = False
        save_state()
        telegram_send(f"🛑 AUTO STOPPED: {reason}.\nManual scanning remains available.")


def process_telegram_updates():
    global scan_requested
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID repository secret.")
    try:
        response = session.get(
            telegram_api("getUpdates"),
            params={"timeout": 10, "offset": int(state["last_update_id"]) + 1},
            timeout=15,
        )
        if not response.ok:
            logging.warning("Telegram polling failed: %s", response.text[:250])
            time.sleep(3)
            return
        updates = response.json().get("result", [])
    except Exception as exc:
        logging.warning("Telegram polling error: %s", exc)
        time.sleep(3)
        return

    for update in updates:
        state["last_update_id"] = max(int(state["last_update_id"]), int(update.get("update_id", 0)))
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message", {})
            chat_id = str((message.get("chat") or {}).get("id", ""))
            callback_id = callback.get("id", "")
            command = str(callback.get("data", "")).upper()
            if chat_id != str(TELEGRAM_CHAT_ID):
                telegram_answer_callback(callback_id, "Not authorized")
                continue
            if command == "START_AUTO":
                reason = risk_stop_reason()
                if not AUTO_TRADE:
                    state["auto_enabled"] = False
                    telegram_send("AUTO_TRADE is disabled in code. Manual scans are available.")
                elif reason:
                    state["auto_enabled"] = False
                    telegram_send(f"🛑 Cannot start auto: {reason}.\nUse STATUS to check limits.")
                else:
                    state["auto_enabled"] = True
                    telegram_send("▶️ AUTO MODE STARTED — PRACTICE ONLY. I will trade at most one top-ranked setup per scan.")
                save_state()
            elif command == "STOP_AUTO":
                state["auto_enabled"] = False
                save_state()
                telegram_send("⏹ AUTO MODE STOPPED. Manual SCAN MARKET still works; it will not place trades.")
            elif command == "SCAN_MARKET":
                scan_requested = True
                telegram_answer_callback(callback_id, "Market scan queued")
                telegram_send("🔎 Manual market scan queued. No trades will be placed by this scan.")
            elif command == "STATUS":
                telegram_send(status_text())
                telegram_answer_callback(callback_id, "Status sent")
            else:
                telegram_answer_callback(callback_id, "Unknown button")
        else:
            # Also let an authorized user type /start, /scan, /status, or /stop.
            message = update.get("message", {})
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = str(message.get("text", "")).strip().lower()
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue
            if text in ("/start", "/menu"):
                telegram_send("PRACTICE MOMENTUM SCANNER\nUse the buttons below to control it.")
            elif text in ("/scan", "scan"):
                scan_requested = True
                telegram_send("🔎 Manual market scan queued. No trades will be placed by this scan.")
            elif text in ("/status", "status"):
                telegram_send(status_text())
            elif text in ("/stop", "stop"):
                state["auto_enabled"] = False
                save_state()
                telegram_send("⏹ AUTO MODE STOPPED.")
            elif text in ("/auto", "auto"):
                reason = risk_stop_reason()
                if AUTO_TRADE and not reason:
                    state["auto_enabled"] = True
                    save_state()
                    telegram_send("▶️ AUTO MODE STARTED — PRACTICE ONLY.")
                else:
                    telegram_send(f"Cannot start auto. {reason or 'AUTO_TRADE disabled in code.'}")
    if updates:
        save_state()


def main():
    global scan_requested, last_auto_scan
    if BALANCE_MODE != "PRACTICE":
        raise RuntimeError("Safety stop: this file only permits PRACTICE mode.")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in GitHub repository Secrets.")

    global state
    state = load_state()
    connect_iq()
    logging.info("Telegram polling enabled; waiting for button commands.")
    telegram_send(
        "✅ PRACTICE MOMENTUM SCANNER ONLINE\n"
        "Buttons: START AUTO, STOP AUTO, SCAN MARKET, STATUS.\n"
        "Manual scans never place trades. Auto mode sends at most one ranked trade per scan.\n"
        "Setup scores are not win probabilities."
    )

    while True:
        enforce_new_day()
        process_telegram_updates()

        if scan_requested and not busy:
            scan_requested = False
            results = scan_market()
            telegram_send(format_signals(results, "🔎 TOP MARKET SETUPS — MANUAL MODE"))
            # Do not start a second, trading scan in the same loop after a manual request.
            if state.get("auto_enabled"):
                last_auto_scan = time.time()

        if state.get("auto_enabled") and not busy:
            reason = risk_stop_reason()
            if reason:
                state["auto_enabled"] = False
                save_state()
                telegram_send(f"🛑 AUTO STOPPED: {reason}.")
            elif time.time() - last_auto_scan >= SCAN_INTERVAL_SECONDS:
                last_auto_scan = time.time()
                results = scan_market()
                if results:
                    telegram_send(format_signals(results, "🤖 AUTO SCAN — TOP SETUPS"))
                    # One trade only per scan keeps the risk cap meaningful.
                    place_and_monitor(results[0])
                else:
                    logging.info("No valid auto setup this cycle.")
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        state["auto_enabled"] = False
        save_state()
        telegram_send("Scanner stopped safely. Auto mode is off.")
    except Exception as exc:
        logging.exception("Fatal scanner error")
        try:
            telegram_send(f"🚨 SCANNER ERROR: {exc}")
        except Exception:
            pass
        raise
