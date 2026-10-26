import os, time, json, math, logging, threading, traceback, requests, queue
from datetime import datetime, timezone
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 — PRACTICE ONLY. Manual scans NEVER place trades.
BOT_NAME = "Momentum 10 IQ Option Scanner"
AUTO_TRADING_ALLOWED = True
STAKE = 1.0
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60
LOOKBACK_CANDLES = 50

# Original strategy filters — unchanged
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
MAX_SYMBOLS_PER_SCAN = 40
CANDLE_REQUEST_TIMEOUT = 8
TOP_SIGNALS = 3
PROGRESS_EVERY = 10
MAX_SIGNAL_AGE_SECONDS = 180
STATE_FILE = "momentum_state.json"

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def skip_digital_open(self):
    return

try:
    IQ_Option._IQ_Option__get_digital_open = skip_digital_open
except Exception:
    logging.warning("Could not disable digital-open discovery.")

IQ = None
RUNNING = True
AUTO_MODE = False
STATE_LOCK = threading.RLock()
STATE_SAVE_LOCK = threading.Lock()
TELEGRAM_OFFSET = 0
OPEN_TRADES = {}
LAST_SIGNAL_BY_ASSET = {}
MANUAL_SCAN_REQUESTED = False
SCAN_IN_PROGRESS = False
SCAN_THREAD = None
SCAN_RESULTS_QUEUE = queue.Queue()
LAST_HEARTBEAT = 0
LAST_SCAN_STARTED = 0
LAST_SCAN_FINISHED = 0
LAST_SCAN_CANDIDATES = 0
LAST_SCAN_SCANNED = 0
LAST_SCAN_TOTAL = 0
LAST_SCAN_TIMED_OUT = False
ASSET_CURSOR = 0
LAST_ASSET_COUNT = 0

DEFAULT_STATE = {
    "date": "", "daily_pl": 0.0, "resolved": 0, "wins": 0, "losses": 0,
    "consecutive_losses": 0, "signals": 0, "executed": 0, "blocked": 0,
    "history": [], "open_trades": {}, "last_signal_candles": {},
    "last_update_id": 0
}
STATE = dict(DEFAULT_STATE)

def utc_now():
    return datetime.now(timezone.utc)

def today_string():
    return utc_now().strftime("%Y-%m-%d")

def money(value):
    return f"${float(value):.2f}"

def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default

def telegram_send(message, keyboard=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram unavailable. Message: %s", message)
        return False
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": str(message)[:4000],
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"keyboard": [
            [{"text": "START AUTO"}, {"text": "STOP AUTO"}],
            [{"text": "SCAN MARKET"}, {"text": "STATUS"}]
        ], "resize_keyboard": True}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)
        return False

def save_state():
    with STATE_LOCK:
        STATE["open_trades"] = {str(k): dict(v) for k, v in OPEN_TRADES.items()}
        STATE["last_signal_candles"] = dict(LAST_SIGNAL_BY_ASSET)
        snapshot = dict(STATE)
        snapshot["history"] = list(STATE.get("history", []))
        snapshot["open_trades"] = dict(STATE["open_trades"])
        snapshot["last_signal_candles"] = dict(STATE["last_signal_candles"])
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)
            content = json.dumps(snapshot, indent=2).encode("utf-8")
        except Exception as exc:
            logging.warning("Local state save failed: %s", exc)
            return
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return
    with STATE_SAVE_LOCK:
        try:
            import base64
            url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
            headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
                       "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"}
            old = requests.get(url, headers=headers, params={"ref": GITHUB_REF_NAME}, timeout=10)
            payload = {"message": "Update Momentum 10 scanner state",
                       "branch": GITHUB_REF_NAME,
                       "content": base64.b64encode(content).decode("ascii")}
            if old.status_code == 200:
                payload["sha"] = old.json().get("sha")
            elif old.status_code != 404:
                logging.warning("GitHub state lookup returned %s", old.status_code)
                return
            result = requests.put(url, headers=headers, json=payload, timeout=15)
            if result.status_code not in (200, 201):
                logging.warning("GitHub state update failed: %s %s",
                                result.status_code, result.text[:250])
        except Exception as exc:
            logging.warning("GitHub state save failed: %s", exc)

def load_state():
    global STATE, OPEN_TRADES, LAST_SIGNAL_BY_ASSET, TELEGRAM_OFFSET
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            STATE.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.warning("State load failed: %s", exc)
    for key, default in DEFAULT_STATE.items():
        if key not in STATE:
            STATE[key] = default.copy() if isinstance(default, (dict, list)) else default
    if not isinstance(STATE.get("history"), list):
        STATE["history"] = []
    if not isinstance(STATE.get("open_trades"), dict):
        STATE["open_trades"] = {}
    if not isinstance(STATE.get("last_signal_candles"), dict):
        STATE["last_signal_candles"] = {}

    # Migrate an older saved pending order, unless already marked settled.
    pending = STATE.get("pending_order_id")
    settled = {str(x) for x in STATE.get("settled_order_ids", [])}
    if pending and str(pending) not in settled and str(pending) not in STATE["open_trades"]:
        started = time.time()
        try:
            started = datetime.fromisoformat(
                str(STATE.get("pending_started")).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            pass
        STATE["open_trades"][str(pending)] = {
            "signal_id": f"RECOVERED-{pending}",
            "asset": str(STATE.get("pending_asset") or "Unknown"),
            "direction": str(STATE.get("pending_direction") or "unknown"),
            "stake": STAKE, "score": 0, "started": started, "recovered": True
        }

    OPEN_TRADES = {str(k): dict(v) for k, v in STATE["open_trades"].items()
                   if isinstance(v, dict)}
    LAST_SIGNAL_BY_ASSET = {str(k): str(v)
                            for k, v in STATE["last_signal_candles"].items()}
    try:
        TELEGRAM_OFFSET = int(STATE.get("last_update_id", 0)) + 1
    except Exception:
        TELEGRAM_OFFSET = 0

    if STATE.get("date") != today_string():
        STATE.update({"date": today_string(), "daily_pl": 0.0, "resolved": 0,
                      "wins": 0, "losses": 0, "consecutive_losses": 0,
                      "signals": 0, "executed": 0, "blocked": 0, "history": []})
    save_state()

def daily_limit_reached():
    with STATE_LOCK:
        return (STATE["daily_pl"] >= DAILY_PROFIT_TARGET
                or STATE["daily_pl"] <= DAILY_MAX_LOSS
                or STATE["resolved"] >= MAX_RESOLVED_TRADES
                or STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES)

def daily_limit_reason():
    with STATE_LOCK:
        if STATE["daily_pl"] >= DAILY_PROFIT_TARGET: return "Daily profit target reached."
        if STATE["daily_pl"] <= DAILY_MAX_LOSS: return "Daily loss limit reached."
        if STATE["resolved"] >= MAX_RESOLVED_TRADES: return "Maximum resolved trades reached."
        if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES: return "Maximum consecutive losses reached."
    return ""

# IQ connection always forces PRACTICE and refuses to continue if not verified.
def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD secret.")
    logging.info("Connecting to IQ Option...")
    candidate = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    result = candidate.connect()
    if result is False:
        raise RuntimeError("IQ Option connection failed.")
    for _ in range(15):
        try:
            if candidate.check_connect(): break
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
    if "PRACTICE" not in mode:
        raise RuntimeError("Safety stop: PRACTICE account mode could not be verified.")
    IQ = candidate
    try: balance = safe_float(candidate.get_balance())
    except Exception: balance = 0.0
    logging.info("Connected. Practice balance: %s", money(balance))
    telegram_send("🟢 MOMENTUM 10 CONNECTED\nAccount: PRACTICE ONLY\n"
                  f"Balance: {money(balance)}\nAUTO MODE: OFF\n"
                  "Expiry: 1 minute\nStake: $1\nManual scans never place trades.")

def ensure_connection():
    global IQ
    try:
        if IQ is not None and IQ.check_connect() and "PRACTICE" in str(IQ.get_balance_mode()).upper():
            return True
    except Exception:
        pass
    IQ = None
    try:
        connect_iq()
        return True
    except Exception as exc:
        logging.error("Reconnect failed: %s", exc)
        return False

def practice_mode_verified():
    try:
        return IQ is not None and IQ.check_connect() and "PRACTICE" in str(IQ.get_balance_mode()).upper()
    except Exception:
        return False

def get_asset_names():
    try:
        active_map = IQ.get_all_ACTIVES_OPCODE()
        if not isinstance(active_map, dict):
            logging.warning("Active-symbol mapping is not a dictionary.")
            return []
        return sorted({s.strip() for s in active_map if isinstance(s, str) and s.strip()})
    except Exception as exc:
        logging.warning("Asset discovery failed: %s", exc)
        return []

def _candle_request_worker(client, asset, output_queue):
    try:
        output_queue.put(("ok", client.get_candles(
            asset, CANDLE_SECONDS, LOOKBACK_CANDLES, int(time.time())))
        )
    except Exception as exc:
        output_queue.put(("error", str(exc)))

def fetch_candles(asset):
    global IQ
    client = IQ
    if client is None: return None
    q = queue.Queue(maxsize=1)
    worker = threading.Thread(target=_candle_request_worker, args=(client, asset, q), daemon=True)
    worker.start()
    worker.join(CANDLE_REQUEST_TIMEOUT)
    if worker.is_alive():
        logging.error("CANDLE TIMEOUT: %s exceeded %ss; reconnect required.", asset, CANDLE_REQUEST_TIMEOUT)
        if IQ is client: IQ = None
        return None
    try: status, candles = q.get_nowait()
    except queue.Empty: return []
    if status != "ok" or not isinstance(candles, list): return []
    if len(candles) < MOMENTUM_PERIOD + 3: return []
    candles = sorted(candles, key=lambda c: c.get("from", 0))
    cleaned = []
    for candle in candles:
        close = safe_float(candle.get("close"), float("nan"))
        op = safe_float(candle.get("open"), float("nan"))
        high = safe_float(candle.get("max"), float("nan"))
        low = safe_float(candle.get("min"), float("nan"))
        if not all(math.isfinite(v) for v in (close, op, high, low)): continue
        if close <= 0 or high < low: continue
        cleaned.append({"open": op, "close": close, "high": high,
                        "low": low, "from": candle.get("from", 0)})
    # Exclude the newest candle because it may still be forming.
    if len(cleaned) > 1: cleaned = cleaned[:-1]
    return cleaned

# Original Momentum 10 signal rules preserved.
def calculate_signal(asset, candles):
    if len(candles) < MOMENTUM_PERIOD + 3: return None, "insufficient candles"
    closes = [c["close"] for c in candles]
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    current_close, previous_close = closes[-1], closes[-2]
    old_close = closes[-1 - MOMENTUM_PERIOD]
    if old_close <= 0: return None, "invalid reference price"
    momentum = (current_close - old_close) / old_close
    ranges = [highs[i] - lows[i] for i in range(max(0, len(candles)-14), len(candles))]
    average_range = sum(ranges) / len(ranges) if ranges else 0.0
    if average_range <= 0: return None, "no usable candle range"
    last_range = highs[-1] - lows[-1]
    if last_range <= 0: return None, "flat latest candle"
    body_ratio = abs(current_close - opens[-1]) / last_range
    upper_turn = (highs[-1] - current_close) / last_range
    lower_turn = (current_close - lows[-1]) / last_range
    if momentum < 0:
        if current_close <= opens[-1]: return None, "no bullish reversal after downward momentum"
        turn_distance = lower_turn
        if turn_distance < MIN_TURN_DISTANCE: return None, "reversal distance too small"
        direction = "call"
    elif momentum > 0:
        if current_close >= opens[-1]: return None, "no bearish reversal after upward momentum"
        turn_distance = upper_turn
        if turn_distance < MIN_TURN_DISTANCE: return None, "reversal distance too small"
        direction = "put"
    else:
        return None, "zero momentum"
    normalized = abs(current_close - old_close) / average_range
    if normalized < EXTREME_PERCENTILE: return None, "momentum not extreme enough"
    if direction == "call" and current_close <= previous_close:
        return None, "bullish reversal lacks close confirmation"
    if direction == "put" and current_close >= previous_close:
        return None, "bearish reversal lacks close confirmation"
    score = 50
    if normalized >= 0.5: score += 10
    if normalized >= 1.0: score += 10
    if body_ratio >= 0.4: score += 10
    if body_ratio >= 0.65: score += 10
    if turn_distance >= 0.3: score += 5
    if turn_distance >= 0.5: score += 5
    try: candle_time = int(candles[-1].get("from") or time.time())
    except Exception: candle_time = int(time.time())
    return {"asset": asset, "direction": direction, "score": min(score, 100),
            "momentum": momentum, "normalized_momentum": normalized,
            "body_ratio": body_ratio, "turn_distance": turn_distance,
            "price": current_close, "candle_time": candle_time,
            "time": utc_now().strftime("%H:%M:%S UTC")}, ""

def send_manual_scan_results(results, scanned, total, rejected, timed_out=False):
    lines = ["🔎 MANUAL MARKET SCAN", "Mode: REPORT ONLY — NO TRADES",
             f"Symbols checked this batch: {scanned}/{total}",
             f"Batch size limit: {MAX_SYMBOLS_PER_SCAN}",
             f"Candidates: {len(results)}", ""]
    if timed_out: lines.append("⚠️ Candle request timed out. Reconnect required.")
    if total > scanned:
        lines.append("The scanner rotates through the discovered symbol list across successive scans.")
    if results:
        lines.append("TOP MOMENTUM 10 SETUPS:")
        for s in results[:TOP_SIGNALS]:
            lines.append(f"{s['asset']} | {s['direction'].upper()} | Score {s['score']}/100 | Momentum {s['normalized_momentum']:.2f}")
    else:
        lines.append("NO TRADE — no qualifying setups in this batch.")
    if rejected:
        lines.extend(["", "REJECTION SUMMARY:"])
        for reason, count in sorted(rejected.items(), key=lambda x: x[1], reverse=True)[:5]:
            lines.append(f"• {reason}: {count}")
    telegram_send("\n".join(lines))

def scan_market(manual=False, auto_eligible=False):
    global SCAN_IN_PROGRESS, ASSET_CURSOR, LAST_ASSET_COUNT
    global LAST_SCAN_FINISHED, LAST_SCAN_CANDIDATES, LAST_SCAN_SCANNED
    global LAST_SCAN_TOTAL, LAST_SCAN_TIMED_OUT
    with STATE_LOCK:
        if SCAN_IN_PROGRESS: return
        SCAN_IN_PROGRESS = True
    results, rejected = [], {}
    scanned = attempted = total = 0
    timed_out = False
    try:
        if not ensure_connection():
            if manual: telegram_send("❌ Scan failed: IQ Option is disconnected.")
            return
        assets = get_asset_names()
        total = len(assets)
        LAST_ASSET_COUNT = total
        if not assets:
            logging.warning("No symbols returned by IQ Option.")
            if manual: telegram_send("⚠️ No symbols returned by IQ Option.")
            return
        start = ASSET_CURSOR % total
        batch = [assets[(start+i) % total] for i in range(min(MAX_SYMBOLS_PER_SCAN, total))]
        logging.info("Scanning batch: %d symbols; total=%d; start=%d; auto_eligible=%s",
                     len(batch), total, start, auto_eligible)
        for index, asset in enumerate(batch, 1):
            if not RUNNING: break
            if IQ is None:
                timed_out = True
                rejected["connection reset after timeout"] = 1
                break
            attempted += 1
            candles = fetch_candles(asset)
            if candles is None:
                timed_out = True
                rejected["candle request timeout"] = rejected.get("candle request timeout", 0) + 1
                break
            scanned += 1
            if not candles:
                rejected["no candles/unavailable"] = rejected.get("no candles/unavailable", 0) + 1
            else:
                signal, reason = calculate_signal(asset, candles)
                if signal: results.append(signal)
                else: rejected[reason] = rejected.get(reason, 0) + 1
            if index % PROGRESS_EVERY == 0:
                logging.info("Batch progress: %d/%d; candidates=%d", index, len(batch), len(results))
            time.sleep(REQUEST_PAUSE_SECONDS)
        ASSET_CURSOR = (start + max(attempted, 1)) % total
        results.sort(key=lambda s: s["score"], reverse=True)
        LAST_SCAN_FINISHED = time.time()
        LAST_SCAN_CANDIDATES = len(results)
        LAST_SCAN_SCANNED = scanned
        LAST_SCAN_TOTAL = total
        LAST_SCAN_TIMED_OUT = timed_out
        logging.info("Batch finished: scanned=%d/%d attempted=%d candidates=%d timeout=%s",
                     scanned, total, attempted, len(results), timed_out)
        if manual: send_manual_scan_results(results, scanned, total, rejected, timed_out)
        SCAN_RESULTS_QUEUE.put({"manual": manual, "auto_eligible": auto_eligible,
                                "finished_at": time.time(), "results": results,
                                "scanned": scanned, "total": total, "timed_out": timed_out})
    except Exception as exc:
        logging.error("Market scan failed: %s", exc)
        logging.debug(traceback.format_exc())
        if manual: telegram_send(f"❌ Market scan error: {str(exc)[:250]}")
        SCAN_RESULTS_QUEUE.put({"manual": manual, "auto_eligible": auto_eligible,
                                "finished_at": time.time(), "results": [],
                                "scanned": scanned, "total": total,
                                "timed_out": timed_out, "error": str(exc)})
    finally:
        with STATE_LOCK: SCAN_IN_PROGRESS = False

def start_scan(manual=False):
    global SCAN_THREAD, LAST_SCAN_STARTED
    with STATE_LOCK:
        if SCAN_IN_PROGRESS: return False
        auto_eligible = bool(AUTO_MODE and not manual)
        SCAN_IN_PROGRESS = True
        LAST_SCAN_STARTED = time.time()
        def wrapper():
            global SCAN_IN_PROGRESS
            with STATE_LOCK: SCAN_IN_PROGRESS = False
            scan_market(manual=manual, auto_eligible=auto_eligible)
        SCAN_THREAD = threading.Thread(target=wrapper, daemon=True)
        SCAN_THREAD.start()
    return True

def execute_signal(signal):
    global AUTO_MODE
    if not AUTO_TRADING_ALLOWED or not AUTO_MODE: return False
    if daily_limit_reached():
        AUTO_MODE = False
        telegram_send("🛑 AUTO STOPPED\n" + daily_limit_reason())
        return False
    if not practice_mode_verified():
        AUTO_MODE = False
        telegram_send("🚨 SAFETY STOP: PRACTICE mode could not be verified. No order sent.")
        return False
    asset = str(signal["asset"])
    direction = str(signal["direction"]).lower()
    candle_time = int(signal.get("candle_time") or time.time())
    age = time.time() - candle_time
    if age > MAX_SIGNAL_AGE_SECONDS or age < -5:
        logging.info("Skipping stale signal: %s age=%.1fs", asset, age)
        return False
    if LAST_SIGNAL_BY_ASSET.get(asset) == str(candle_time):
        logging.info("Duplicate candle signal skipped: %s", asset)
        return False
    signal_id = f"M10-{asset}-{direction.upper()}-{candle_time}"
    try:
        if not practice_mode_verified():
            AUTO_MODE = False
            telegram_send("🚨 Practice-mode verification failed.")
            return False
        response = IQ.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        if not isinstance(response, tuple) or len(response) < 2:
            logging.error("Unexpected IQ.buy response for %s: %r", asset, response)
            with STATE_LOCK: STATE["blocked"] += 1
            save_state()
            return False
        success, order_id = bool(response[0]), response[1]
        if not success or order_id is None or str(order_id) == "":
            with STATE_LOCK: STATE["blocked"] += 1
            logging.warning("Order rejected: %s; response=%r", asset, response)
            save_state()
            return False
        order_key = str(order_id)
        with STATE_LOCK:
            OPEN_TRADES[order_key] = {"signal_id": signal_id, "asset": asset,
                "direction": direction, "stake": STAKE, "score": signal["score"],
                "started": time.time(), "candle_time": candle_time}
            LAST_SIGNAL_BY_ASSET[asset] = str(candle_time)
            STATE["executed"] += 1
            STATE["signals"] += 1
        save_state()
        telegram_send("🟡 PRACTICE TRADE ACCEPTED\n"
            f"Signal ID: {signal_id}\nOrder ID: {order_key}\nAsset: {asset}\n"
            f"Direction: {direction.upper()}\nStake: {money(STAKE)}\n"
            f"Expiry: {EXPIRY_MINUTES} minute\nScore: {signal['score']}/100\n"
            "Score is not win probability.")
        logging.info("Practice order accepted: %s id=%s", asset, order_key)
        return True
    except Exception as exc:
        logging.error("Order error for %s: %s", asset, exc)
        with STATE_LOCK: STATE["blocked"] += 1
        save_state()
        telegram_send(f"⚠️ AUTO ORDER FAILED\nAsset: {asset}\nReason: {str(exc)[:220]}")
        return False

def reconcile_trades():
    global AUTO_MODE
    if not OPEN_TRADES or not ensure_connection(): return
    for order_id in list(OPEN_TRADES):
        trade = OPEN_TRADES.get(order_id)
        if not trade: continue
        started = safe_float(trade.get("started"), time.time())
        if time.time() - started < EXPIRY_MINUTES * 60: continue
        try:
            result = IQ.check_win_v4(int(order_id))
        except Exception as exc:
            logging.warning("Result check failed for %s: %s", order_id, exc)
            continue
        if result is None:
            now = time.time()
            if now - started > RESULT_TIMEOUT_SECONDS and now - safe_float(trade.get("last_pending_log"), 0) > 60:
                logging.warning("Result still pending for order %s", order_id)
                trade["last_pending_log"] = now
            continue
        result = safe_float(result, float("nan"))
        if not math.isfinite(result): continue
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
            else: outcome = "BREAKEVEN"
            STATE["history"].append({"signal_id": trade.get("signal_id", order_id),
                "order_id": str(order_id), "asset": trade.get("asset", "Unknown"),
                "direction": trade.get("direction", "unknown"), "outcome": outcome,
                "net": result, "time": utc_now().isoformat()})
            STATE["history"] = STATE["history"][-200:]
            daily_pl, resolved = STATE["daily_pl"], STATE["resolved"]
        save_state()
        telegram_send(f"📊 TRADE RESULT: {outcome}\nSignal ID: {trade.get('signal_id', order_id)}\n"
            f"Order ID: {order_id}\nAsset: {trade.get('asset', 'Unknown')}\n"
            f"Direction: {str(trade.get('direction', 'unknown')).upper()}\n"
            f"Net result: {money(result)}\nDaily P/L: {money(daily_pl)}\n"
            f"Resolved: {resolved}/{MAX_RESOLVED_TRADES}")
        if daily_limit_reached():
            AUTO_MODE = False
            telegram_send("🛑 AUTO MODE OFF\n" + daily_limit_reason())

def status_message():
    connected = practice_ok = False
    balance_text = "Unavailable"
    try:
        connected = bool(IQ is not None and IQ.check_connect())
        if connected:
            practice_ok = "PRACTICE" in str(IQ.get_balance_mode()).upper()
            balance_text = money(IQ.get_balance()) if practice_ok else "Blocked: practice mode not verified"
    except Exception:
        connected = practice_ok = False
    with STATE_LOCK:
        scan_age = f"{int(time.time()-LAST_SCAN_FINISHED)}s ago" if LAST_SCAN_FINISHED else "Never completed"
        return ("📊 MOMENTUM 10 STATUS\n"
            f"Connection: {'CONNECTED' if connected else 'DISCONNECTED'}\n"
            f"Practice verification: {'OK' if practice_ok else 'NOT VERIFIED'}\n"
            f"Balance: {balance_text}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
            f"Daily P/L: {money(STATE['daily_pl'])}\nWins: {STATE['wins']} | Losses: {STATE['losses']}\n"
            f"Resolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}\n"
            f"Open trades tracked: {len(OPEN_TRADES)}\nSignals accepted: {STATE['signals']}\n"
            f"Orders accepted: {STATE['executed']}\nOrders blocked/rejected: {STATE['blocked']}\n"
            f"Consecutive losses: {STATE['consecutive_losses']}\nDiscovered symbols: {LAST_ASSET_COUNT}\n"
            f"Last scan: {scan_age}\nLast batch: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\n"
            f"Last candidates: {LAST_SCAN_CANDIDATES}\n"
            f"Last scan timeout: {'YES' if LAST_SCAN_TIMED_OUT else 'NO'}\n"
            f"Scan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
            f"Daily stop: {daily_limit_reason() or 'Not reached'}")

def process_telegram_update(update):
    global AUTO_MODE, MANUAL_SCAN_REQUESTED
    message = update.get("message")
    if not message:
        message = update.get("callback_query", {}).get("message")
    if not message: return
    chat_id = str(message.get("chat", {}).get("id", ""))
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID): return
    text = (message.get("text") or update.get("callback_query", {}).get("data") or "").strip()
    if not text: return
    upper = text.upper()
    logging.info("Telegram command received: %s", upper)
    if upper in ("/START", "START AUTO"):
        if not AUTO_TRADING_ALLOWED:
            telegram_send("AUTO refused: automatic trading is disabled.")
        elif daily_limit_reached():
            telegram_send("Cannot start AUTO.\n" + daily_limit_reason())
        elif not ensure_connection() or not practice_mode_verified():
            telegram_send("AUTO refused: connection or PRACTICE verification failed.")
        else:
            AUTO_MODE = True
            telegram_send("🟢 AUTO MODE ON\nPractice account verified.\nStake: $1 | Expiry: 1 minute\n"
                          "At most one accepted order per scan batch.\nSCAN MARKET remains report-only.")
    elif upper in ("/STOP", "STOP AUTO"):
        AUTO_MODE = False
        telegram_send("🔴 AUTO MODE OFF. No new automatic trades will be started.")
    elif upper in ("SCAN MARKET", "/SCAN"):
        MANUAL_SCAN_REQUESTED = True
        telegram_send("🔎 Manual scan queued. REPORT ONLY: it will not place trades.")
    elif upper in ("STATUS", "/STATUS"):
        telegram_send(status_message())
    elif upper.startswith("/STATS"):
        with STATE_LOCK:
            total = STATE["wins"] + STATE["losses"]
            rate = STATE["wins"] / total * 100 if total else 0.0
            telegram_send("📈 MOMENTUM 10 STATS\n"
                f"Wins: {STATE['wins']}\nLosses: {STATE['losses']}\nWin rate: {rate:.2f}%\n"
                f"Daily P/L: {money(STATE['daily_pl'])}\nResolved: {STATE['resolved']}\n"
                f"Open trades tracked: {len(OPEN_TRADES)}")
    elif upper.startswith("/RECONCILE"):
        reconcile_trades()
        telegram_send(f"Reconciliation check requested.\nOpen trades tracked: {len(OPEN_TRADES)}\nUse /stats for results.")

def telegram_listener():
    global TELEGRAM_OFFSET
    if not TELEGRAM_TOKEN:
        logging.warning("TELEGRAM_TOKEN missing; listener disabled.")
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    while RUNNING:
        try:
            r = requests.get(f"{base}/getUpdates",
                params={"offset": TELEGRAM_OFFSET, "timeout": 20}, timeout=25)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                update_id = int(update.get("update_id", 0))
                TELEGRAM_OFFSET = update_id + 1
                process_telegram_update(update)
                with STATE_LOCK: STATE["last_update_id"] = update_id
                save_state()
        except Exception as exc:
            logging.warning("Telegram listener error: %s", exc)
            time.sleep(5)

def heartbeat():
    global LAST_HEARTBEAT
    if time.time() - LAST_HEARTBEAT < HEARTBEAT_SECONDS: return
    LAST_HEARTBEAT = time.time()
    with STATE_LOCK:
        message = ("💓 MOMENTUM 10 HEARTBEAT\nAccount: PRACTICE ONLY\n"
            f"Connection: {'CONNECTED' if practice_mode_verified() else 'NOT VERIFIED'}\n"
            f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nDaily P/L: {money(STATE['daily_pl'])}\n"
            f"Resolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}\nW/L: {STATE['wins']}/{STATE['losses']}\n"
            f"Open trades tracked: {len(OPEN_TRADES)}\nOrders accepted: {STATE['executed']}\n"
            f"Orders blocked/rejected: {STATE['blocked']}\n"
            f"Scan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\nNo qualifying setup means NO TRADE.")
    telegram_send(message)

def main():
    global AUTO_MODE, MANUAL_SCAN_REQUESTED
    AUTO_MODE = False  # Deliberately OFF after every workflow restart.
    load_state()
    telegram_send("🚀 MOMENTUM 10 STARTING\nAutomatic trading starts OFF.\n"
                  "Practice account only.\nManual SCAN MARKET never places trades.")
    threading.Thread(target=telegram_listener, daemon=True).start()
    while RUNNING:
        try:
            if IQ is None and not ensure_connection():
                heartbeat()
                time.sleep(RECONNECT_SECONDS)
                continue
            reconcile_trades()
            while True:
                try: completed = SCAN_RESULTS_QUEUE.get_nowait()
                except queue.Empty: break
                if (not completed.get("manual") and completed.get("auto_eligible")
                    and AUTO_MODE and not completed.get("timed_out") and not daily_limit_reached()):
                    results = completed.get("results", [])
                    if not results:
                        logging.info("NO TRADE: no qualifying setup.")
                    else:
                        # Try ranked candidates until one order is accepted.
                        # Rejected/unavailable symbols don't block the next candidate.
                        for signal in results:
                            if not AUTO_MODE or daily_limit_reached(): break
                            if execute_signal(signal): break
            if MANUAL_SCAN_REQUESTED and not SCAN_IN_PROGRESS:
                MANUAL_SCAN_REQUESTED = False
                start_scan(manual=True)
            elif not SCAN_IN_PROGRESS and time.time() - LAST_SCAN_STARTED >= SCAN_INTERVAL_SECONDS:
                start_scan(manual=False)
            heartbeat()
            time.sleep(1)
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
