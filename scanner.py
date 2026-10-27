import os, time, math, logging, threading, traceback, requests, queue, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 — PRACTICE ONLY. Manual scans never place trades.
BOT_NAME = "Momentum 10 IQ Option Scanner"
AUTO_TRADING_ALLOWED = True
STAKE, EXPIRY_MINUTES, CANDLE_SECONDS, LOOKBACK_CANDLES = 1.0, 1, 60, 50
# Keep the existing strategy filters unchanged.
MOMENTUM_PERIOD, EXTREME_PERCENTILE, MIN_TURN_DISTANCE = 10, 0.08, 0.06
SCAN_INTERVAL_SECONDS, REQUEST_PAUSE_SECONDS, RECONNECT_SECONDS = 60, 0.12, 30
HEARTBEAT_SECONDS, REQUEST_TIMEOUT = 300, 15
MAX_SYMBOLS_PER_SCAN, TOP_SIGNALS, PROGRESS_EVERY = 40, 3, 10
MAX_SIGNAL_AGE_SECONDS, ORDER_REQUEST_TIMEOUT = 180, 12
IQ_EMAIL, IQ_PASSWORD = os.getenv("IQ_EMAIL", ""), os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
try:
    IQ_Option._IQ_Option__get_digital_open = lambda self: None
except Exception:
    logging.warning("Could not disable digital-open discovery.")

IQ, RUNNING, AUTO_MODE = None, True, False
TELEGRAM_OFFSET = None
STATE_LOCK = threading.RLock()
CLIENT_LOCKS_GUARD = threading.Lock()
CLIENT_LOCKS = {}
RECONNECT_LOCK = threading.RLock()
SCAN_IN_PROGRESS = False
MANUAL_SCAN_REQUESTED = False
SCAN_RESULTS_QUEUE = queue.Queue()
LAST_SIGNAL_BY_ASSET = {}
MANUAL_SIGNAL_SENT_BY_ASSET = {}
LAST_HEARTBEAT = LAST_SCAN_STARTED = LAST_SCAN_FINISHED = 0
LAST_ASSET_COUNT = LAST_SCAN_SCANNED = LAST_SCAN_TOTAL = LAST_SCAN_CANDIDATES = ASSET_CURSOR = 0
LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR = False, ""
LAST_REJECTIONS = {}


def utc_now():
    return datetime.now(timezone.utc)


def money(value):
    try:
        return f"${float(value):.2f}"
    except Exception:
        return "Unavailable"


def safe_float(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def client_api_lock(client):
    key = id(client)
    with CLIENT_LOCKS_GUARD:
        if key not in CLIENT_LOCKS:
            CLIENT_LOCKS[key] = threading.RLock()
        return CLIENT_LOCKS[key]


def telegram_send(message, keyboard=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram unavailable: %s", message)
        return False
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": str(message)[:4000], "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"keyboard": [[{"text":"START AUTO"},{"text":"STOP AUTO"}],
            [{"text":"SCAN MARKET"},{"text":"STATUS"}]], "resize_keyboard": True}
    try:
        response = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                 json=payload, timeout=12)
        response.raise_for_status()
        return True
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)
        return False


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD secret.")
    client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    result = client.connect()
    if result is False:
        raise RuntimeError("IQ Option connection failed.")
    for _ in range(15):
        try:
            if client.check_connect():
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("IQ Option connection timed out.")
    client.change_balance("PRACTICE")
    time.sleep(1)
    try:
        mode = str(client.get_balance_mode()).upper()
    except Exception:
        mode = ""
    if "PRACTICE" not in mode:
        raise RuntimeError("Safety stop: PRACTICE account mode could not be verified.")
    IQ = client
    balance = safe_float(client.get_balance(), 0.0)
    logging.info("Connected in PRACTICE. Balance %s", money(balance))
    telegram_send(f"🟢 MOMENTUM 10 CONNECTED\nAccount: PRACTICE ONLY\nBalance: {money(balance)}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nExpiry: 1 minute | Stake: $1\nManual scans never place trades.")


def practice_mode_verified(timeout=15):
    client = IQ
    if client is None:
        return False
    lock = client_api_lock(client)
    if not lock.acquire(timeout=timeout):
        logging.warning("PRACTICE verification deferred: IQ Option API is busy.")
        return False
    try:
        connected = bool(client.check_connect())
        mode = str(client.get_balance_mode()).upper() if connected else ""
        verified = connected and "PRACTICE" in mode
        if not verified:
            logging.warning("PRACTICE verification failed: connected=%s mode=%s", connected, mode or "unavailable")
        return verified
    except Exception as exc:
        logging.warning("PRACTICE verification error: %s", exc)
        return False
    finally:
        lock.release()


def ensure_connection():
    global IQ
    with RECONNECT_LOCK:
        if IQ is not None and practice_mode_verified():
            return True
        IQ = None
        try:
            connect_iq()
            return practice_mode_verified()
        except Exception as exc:
            logging.error("Reconnect failed: %s", exc)
            return False


def get_asset_names():
    client = IQ
    if client is None:
        return []
    result_queue = queue.Queue(maxsize=1)
    def worker():
        try:
            with client_api_lock(client):
                result_queue.put(("ok", client.get_all_ACTIVES_OPCODE()))
        except Exception as exc:
            result_queue.put(("error", str(exc)))
    threading.Thread(target=worker, daemon=True, name="asset-discovery").start()
    try:
        status, data = result_queue.get(timeout=REQUEST_TIMEOUT)
    except queue.Empty:
        logging.error("Asset discovery timed out.")
        if IQ is client:
            globals()["IQ"] = None
        return []
    if status != "ok":
        logging.warning("Asset discovery failed: %s", data)
        return []
    if not isinstance(data, dict):
        return []
    return sorted({name.strip() for name in data if isinstance(name, str) and name.strip()})


def fetch_candles(asset):
    global IQ
    client = IQ
    if client is None:
        return None
    result_queue = queue.Queue(maxsize=1)
    def worker():
        try:
            with client_api_lock(client):
                candles = client.get_candles(asset, CANDLE_SECONDS, LOOKBACK_CANDLES, int(time.time()))
            result_queue.put(("ok", candles))
        except Exception as exc:
            result_queue.put(("error", str(exc)))
    threading.Thread(target=worker, daemon=True, name="candle-request").start()
    try:
        status, candles = result_queue.get(timeout=REQUEST_TIMEOUT)
    except queue.Empty:
        logging.warning("Candle request timed out for %s.", asset)
        if IQ is client:
            IQ = None
        return None
    if status != "ok" or not isinstance(candles, list):
        return []
    cleaned = []
    for candle in sorted(candles, key=lambda item: item.get("from", 0)):
        close = safe_float(candle.get("close"), float("nan"))
        opening = safe_float(candle.get("open"), float("nan"))
        high = safe_float(candle.get("max"), float("nan"))
        low = safe_float(candle.get("min"), float("nan"))
        if not all(math.isfinite(v) for v in (close, opening, high, low)) or close <= 0 or high < low:
            continue
        cleaned.append({"open":opening, "close":close, "high":high, "low":low, "from":candle.get("from", 0)})
    return cleaned


def calculate_signal(asset, candles):
    if len(candles) < MOMENTUM_PERIOD + 3:
        return None, "insufficient candles"
    closes = [c["close"] for c in candles]
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    current, previous, old = closes[-1], closes[-2], closes[-1-MOMENTUM_PERIOD]
    if old <= 0:
        return None, "invalid reference price"
    momentum = (current - old) / old
    ranges = [highs[i] - lows[i] for i in range(max(0, len(candles)-14), len(candles))]
    average_range = sum(ranges) / len(ranges) if ranges else 0.0
    if average_range <= 0:
        return None, "no usable candle range"
    candle_range = highs[-1] - lows[-1]
    if candle_range <= 0:
        return None, "flat latest candle"
    body = abs(current - opens[-1]) / candle_range
    upper = (highs[-1] - current) / candle_range
    lower = (current - lows[-1]) / candle_range
    if momentum < 0:
        if current <= opens[-1]:
            return None, "no bullish reversal after downward momentum"
        turn, direction = lower, "call"
    elif momentum > 0:
        if current >= opens[-1]:
            return None, "no bearish reversal after upward momentum"
        turn, direction = upper, "put"
    else:
        return None, "zero momentum"
    if turn < MIN_TURN_DISTANCE:
        return None, "reversal distance too small"
    normalized = abs(current - old) / average_range
    if normalized < EXTREME_PERCENTILE:
        return None, "momentum not extreme enough"
    if direction == "call" and current <= previous:
        return None, "bullish reversal lacks close confirmation"
    if direction == "put" and current >= previous:
        return None, "bearish reversal lacks close confirmation"
    score = 50 + (10 if normalized >= 0.5 else 0) + (10 if normalized >= 1.0 else 0) + (10 if body >= 0.4 else 0) + (10 if body >= 0.65 else 0) + (5 if turn >= 0.3 else 0) + (5 if turn >= 0.5 else 0)
    try:
        candle_time = int(candles[-1].get("from") or time.time())
    except Exception:
        candle_time = int(time.time())
    return {"asset":asset, "direction":direction, "score":min(score,100),
            "momentum":momentum, "normalized_momentum":normalized, "body_ratio":body,
            "turn_distance":turn, "price":current, "candle_time":candle_time,
            "time":utc_now().strftime("%H:%M:%S UTC")}, ""


def lagos_time(timestamp):
    try:
        value = datetime.fromtimestamp(float(timestamp), ZoneInfo("Africa/Lagos")).strftime("%I:%M:%S %p")
    except Exception:
        value = datetime.fromtimestamp(float(timestamp)).strftime("%I:%M:%S %p")
    return value.lstrip("0") + " WAT"


def display_asset(asset):
    name = str(asset or "").strip()
    otc = bool(re.search(r"(?:[-_\s]?OTC)$", name, re.IGNORECASE))
    base = re.sub(r"(?:[-_\s]?OTC)$", "", name, flags=re.IGNORECASE).strip()
    if len(base) == 6 and base.isalpha():
        base = base[:3].upper() + "/" + base[3:].upper()
    else:
        base = base.replace("_", " ").replace("-", " ").strip()
    return base + (" OTC" if otc else "")


def send_signal(signal, manual=False):
    direction = "CALL 🟢" if signal["direction"] == "call" else "PUT 🔴"
    entry = int(signal.get("entry_time", signal.get("candle_time", time.time())))
    sid = f"M10-{signal.get('asset','UNKNOWN')}-{signal['direction'].upper()}-{int(signal.get('candle_time',time.time()))}"
    telegram_send(f"📣 MOMENTUM 10 SIGNAL\n{display_asset(signal.get('asset'))} — {direction}\nSignal ID: {sid}\nEntry: {lagos_time(entry)}\nExpiry: 1 minute\nScore: {signal.get('score',0)}/100\n{'MANUAL SCAN — no trade placed.' if manual else 'AUTO MODE — order will be checked before placement.'}\nScore is not win probability.")


def scan_market(manual=False, auto_eligible=False):
    global SCAN_IN_PROGRESS, ASSET_CURSOR, LAST_ASSET_COUNT, LAST_SCAN_FINISHED
    global LAST_SCAN_CANDIDATES, LAST_SCAN_SCANNED, LAST_SCAN_TOTAL, LAST_SCAN_TIMED_OUT
    global LAST_SCAN_ERROR, LAST_REJECTIONS
    results, rejected = [], {}
    scanned = attempted = total = 0
    timed_out, error_text = False, ""
    start = ASSET_CURSOR
    timeouts = 0
    try:
        if not ensure_connection():
            error_text = "IQ Option disconnected; reconnect failed."
            return
        assets = get_asset_names()
        total = len(assets)
        LAST_ASSET_COUNT = total
        if not assets:
            timed_out = IQ is None
            error_text = "Asset discovery timed out or IQ Option returned zero symbols."
            return
        start = ASSET_CURSOR % total
        batch = [assets[(start+i) % total] for i in range(min(MAX_SYMBOLS_PER_SCAN, total))]
        logging.info("Scanning %d of %d discovered assets; manual=%s auto_eligible=%s", len(batch), total, manual, auto_eligible)
        for index, asset in enumerate(batch, 1):
            if not RUNNING:
                break
            if IQ is None and not ensure_connection():
                timed_out = True
                rejected["connection/reconnect failed"] = rejected.get("connection/reconnect failed", 0) + 1
                break
            attempted += 1
            candles = fetch_candles(asset)
            if candles is None:
                timed_out = True
                timeouts += 1
                rejected["candle timeout"] = rejected.get("candle timeout", 0) + 1
                if timeouts >= 2 or not ensure_connection():
                    break
                continue
            timeouts = 0
            scanned += 1
            if len(candles) < MOMENTUM_PERIOD + 4:
                rejected["no candles/unavailable"] = rejected.get("no candles/unavailable", 0) + 1
            elif manual:
                # Evaluate the forming candle for an advance warning; report only.
                signal, reason = calculate_signal(asset, candles)
                if signal:
                    signal["entry_time"] = int(signal.get("candle_time", time.time())) + CANDLE_SECONDS
                    if signal["entry_time"] - time.time() >= 8:
                        if LAST_SIGNAL_BY_ASSET.get(asset) != str(signal["candle_time"]):
                            results.append(signal)
                            LAST_SIGNAL_BY_ASSET[asset] = str(signal["candle_time"])
                            # Send promptly; waiting for the whole batch can make a warning stale.
                            if len(results) <= TOP_SIGNALS:
                                send_signal(signal, manual=True)
                    else:
                        rejected["setup too close to candle close"] = rejected.get("setup too close to candle close", 0) + 1
                else:
                    rejected[reason] = rejected.get(reason, 0) + 1
            else:
                # Automatic entries use only fully closed candles.
                signal, reason = calculate_signal(asset, candles[:-1])
                if signal:
                    if LAST_SIGNAL_BY_ASSET.get(asset) != str(signal["candle_time"]):
                        results.append(signal)
                else:
                    rejected[reason] = rejected.get(reason, 0) + 1
            if index % PROGRESS_EVERY == 0:
                logging.info("Scan progress %d/%d; candidates=%d", index, len(batch), len(results))
            time.sleep(REQUEST_PAUSE_SECONDS)
    except Exception as exc:
        error_text = str(exc)
        logging.exception("Market scan failed")
    finally:
        if total:
            ASSET_CURSOR = (start + max(attempted, 1)) % total
        results.sort(key=lambda item: item["score"], reverse=True)
        LAST_SCAN_FINISHED = time.time()
        LAST_SCAN_CANDIDATES = len(results)
        LAST_SCAN_SCANNED, LAST_SCAN_TOTAL = scanned, total
        LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR = timed_out, error_text
        LAST_REJECTIONS = dict(sorted(rejected.items(), key=lambda item:item[1], reverse=True)[:8])
        logging.info("Scan finished: scanned=%d/%d candidates=%d timeout=%s error=%s", scanned, total, len(results), timed_out, error_text or "none")
        if manual and not results:
            top = sorted(rejected.items(), key=lambda item:item[1], reverse=True)[:3]
            reasons = "; ".join(f"{name}: {count}" for name,count in top) or "No valid candles returned."
            suffix = "\nConnection timed out; scanner will reconnect." if timed_out else (f"\n{error_text}" if error_text else "")
            telegram_send(f"NO TRADE\nScanned: {scanned}/{total}\nTop rejection reasons: {reasons}{suffix}")
        SCAN_RESULTS_QUEUE.put({"manual":manual, "auto_eligible":auto_eligible, "results":results,
                                "timed_out":timed_out, "error":error_text})
        with STATE_LOCK:
            SCAN_IN_PROGRESS = False


def start_scan(manual=False):
    global SCAN_IN_PROGRESS, LAST_SCAN_STARTED
    with STATE_LOCK:
        if SCAN_IN_PROGRESS:
            return False
        SCAN_IN_PROGRESS = True
        LAST_SCAN_STARTED = time.time()
        eligible = bool(AUTO_MODE and not manual)
    threading.Thread(target=scan_market, kwargs={"manual":manual, "auto_eligible":eligible},
                     daemon=True, name="market-scan").start()
    return True


def execute_signal(signal):
    if not AUTO_TRADING_ALLOWED or not AUTO_MODE:
        return False
    if not practice_mode_verified():
        set_auto(False, "PRACTICE mode could not be verified")
        telegram_send("🛑 AUTO STOPPED: PRACTICE mode could not be verified.")
        return False
    asset = str(signal.get("asset", ""))
    direction = str(signal.get("direction", "")).lower()
    candle_time = int(signal.get("candle_time") or time.time())
    age = time.time() - candle_time
    if age > MAX_SIGNAL_AGE_SECONDS or age < -5:
        logging.info("Skipping stale signal %s (age %.1fs)", asset, age)
        return False
    if not asset or direction not in ("call", "put") or LAST_SIGNAL_BY_ASSET.get(asset) == str(candle_time):
        return False
    # Mark this candle before sending the request, so a timed-out request is never retried for the same candle.
    LAST_SIGNAL_BY_ASSET[asset] = str(candle_time)
    result_queue = queue.Queue(maxsize=1)
    client = IQ
    def worker():
        try:
            if not practice_mode_verified():
                result_queue.put(("safety", "PRACTICE verification failed"))
                return
            with client_api_lock(client):
                response = client.buy(STAKE, asset, direction, EXPIRY_MINUTES)
            result_queue.put(("result", response))
        except Exception as exc:
            try:
                result_queue.put(("error", str(exc)))
            except queue.Full:
                pass
    threading.Thread(target=worker, daemon=True, name="place-practice-order").start()
    try:
        status, response = result_queue.get(timeout=ORDER_REQUEST_TIMEOUT)
    except queue.Empty:
        set_auto(False, "Order request timed out; no retry made")
        telegram_send(f"⚠️ AUTO STOPPED — ORDER RESPONSE TIMED OUT\n{display_asset(asset)} {direction.upper()}\nNo retry was made for this candle. Check the practice account before restarting.")
        return False
    if status == "safety":
        set_auto(False, response)
        telegram_send("🛑 AUTO STOPPED: " + response)
        return False
    if status != "result" or not isinstance(response, tuple) or len(response) < 2:
        logging.warning("Order request failed for %s: %r", asset, response)
        return False
    success, order_id = bool(response[0]), response[1]
    if not success or order_id is None or str(order_id) == "":
        logging.info("Practice order rejected for %s: %r", asset, response)
        telegram_send(f"⚪ ORDER REJECTED\n{display_asset(asset)} {direction.upper()}\nNo trade was confirmed.")
        return False
    telegram_send(f"🟡 PRACTICE ORDER ACCEPTED\n{display_asset(asset)} — {direction.upper()}\nOrder ID: {order_id}\nStake: $1 | Expiry: 1 minute\nThis bot does not record wins or losses.")
    logging.info("Practice order accepted for %s; order id=%s", asset, order_id)
    return True


def set_auto(enabled, reason=""):
    global AUTO_MODE
    AUTO_MODE = bool(enabled)
    logging.info("AUTO mode %s: %s", "ON" if enabled else "OFF", reason or "manual")
    return True


def status_message():
    connected = practice = False
    balance = "Unavailable"
    try:
        connected = bool(IQ is not None and IQ.check_connect())
        if connected:
            practice = "PRACTICE" in str(IQ.get_balance_mode()).upper()
            if practice:
                balance = money(IQ.get_balance())
    except Exception:
        connected = practice = False
    age = f"{int(time.time()-LAST_SCAN_FINISHED)} seconds ago" if LAST_SCAN_FINISHED else "Not completed yet"
    return (f"📊 MOMENTUM 10 STATUS\nConnection: {'CONNECTED' if connected else 'DISCONNECTED'}\n"
            f"Practice verification: {'OK' if practice else 'NOT VERIFIED'}\nBalance: {balance}\n"
            f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nStake: $1 | Expiry: 1 minute\n"
            f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {age}\n"
            f"Last batch: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\nCandidates: {LAST_SCAN_CANDIDATES}\n"
            f"Scan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
            f"Last timeout: {'YES' if LAST_SCAN_TIMED_OUT else 'NO'}\n"
            f"Last error: {LAST_SCAN_ERROR or 'None'}\n"
            f"Top rejection reasons: {'; '.join(f'{k}: {v}' for k,v in LAST_REJECTIONS.items()) or 'No scan data yet'}")


def process_telegram_update(update):
    global MANUAL_SCAN_REQUESTED
    message = update.get("message") or update.get("callback_query", {}).get("message")
    if not message:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return
    raw = (message.get("text") or update.get("callback_query", {}).get("data") or "").strip()
    if not raw:
        return
    command = raw.upper()
    logging.info("Telegram command: %s", command)
    if command in ("/START", "START AUTO"):
        if not AUTO_TRADING_ALLOWED:
            telegram_send("AUTO is disabled in this scanner.")
        elif not ensure_connection():
            set_auto(False, "Connection or PRACTICE verification failed")
            telegram_send("AUTO refused: connection or PRACTICE verification failed. Check STATUS; AUTO remains OFF.")
        else:
            set_auto(True)
            telegram_send("🟢 AUTO MODE ON\nPRACTICE verified. $1 stake / 1-minute expiry.\nSCAN MARKET remains report-only.\nWins/losses and trade history are not recorded.")
    elif command in ("/STOP", "STOP AUTO"):
        set_auto(False, "Stopped manually")
        telegram_send("🔴 AUTO MODE OFF. No new automatic orders will be started.")
    elif command in ("SCAN MARKET", "/SCAN"):
        if not MANUAL_SCAN_REQUESTED and not SCAN_IN_PROGRESS:
            MANUAL_SCAN_REQUESTED = True
            telegram_send("🔎 Manual market scan queued. It will send qualifying signals only; manual scans never place trades.")
        elif SCAN_IN_PROGRESS:
            telegram_send("🔎 A scan is already running. Wait for its signal or NO TRADE result.")
        else:
            telegram_send("🔎 Manual scan is already queued.")
    elif command in ("STATUS", "/STATUS"):
        telegram_send(status_message())
    elif command in ("/HELP", "HELP"):
        telegram_send("Commands:\nSTART AUTO — enable $1 PRACTICE trades\nSTOP AUTO — stop new automatic trades\nSCAN MARKET — report-only signal scan\nSTATUS — connection and scan status\nNo win/loss tracker or trade-history recorder is included.")
    else:
        telegram_send("Unknown command. Use START AUTO, STOP AUTO, SCAN MARKET, STATUS, or /HELP.")


def telegram_listener():
    global TELEGRAM_OFFSET
    if not TELEGRAM_TOKEN:
        logging.warning("TELEGRAM_TOKEN missing; Telegram listener disabled.")
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    # Skip old queued Telegram commands on startup; only process new messages.
    try:
        response = requests.get(base + "/getUpdates", params={"timeout":0}, timeout=5)
        data = response.json()
        if data.get("ok") and data.get("result"):
            TELEGRAM_OFFSET = max(int(item.get("update_id", 0)) for item in data["result"]) + 1
        elif TELEGRAM_OFFSET is None:
            TELEGRAM_OFFSET = 0
    except Exception:
        if TELEGRAM_OFFSET is None:
            TELEGRAM_OFFSET = 0
    while RUNNING:
        try:
            response = requests.get(base + "/getUpdates", params={"offset":TELEGRAM_OFFSET, "timeout":20},
                                    timeout=25)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                TELEGRAM_OFFSET = int(update.get("update_id", 0)) + 1
                try:
                    process_telegram_update(update)
                except Exception:
                    logging.exception("Telegram command handler failed")
                    telegram_send("⚠️ Command error. Check the GitHub Actions log.")
        except Exception as exc:
            logging.warning("Telegram listener error: %s", exc)
            time.sleep(5)


def heartbeat():
    global LAST_HEARTBEAT
    if time.time() - LAST_HEARTBEAT < HEARTBEAT_SECONDS:
        return
    LAST_HEARTBEAT = time.time()
    telegram_send(f"💓 MOMENTUM 10 HEARTBEAT\nAccount: PRACTICE ONLY\n"
                  f"Connection: {'OK' if practice_mode_verified() else 'NOT VERIFIED'}\n"
                  f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
                  f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\n"
                  f"Candidates: {LAST_SCAN_CANDIDATES}\nScan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
                  f"No qualifying setup means NO TRADE.")


def main():
    global RUNNING, MANUAL_SCAN_REQUESTED
    telegram_send("🚀 MOMENTUM 10 STARTING\nPRACTICE-only scanner is connecting.\nManual SCAN MARKET sends signals only and never places trades.\nNo win/loss tracking or trade-history recording.")
    threading.Thread(target=telegram_listener, daemon=True, name="telegram-listener").start()
    while RUNNING:
        try:
            if IQ is None and not ensure_connection():
                if AUTO_MODE:
                    set_auto(False, "Connection lost")
                    telegram_send("⚠️ AUTO MODE OFF: connection lost; reconnecting.")
                heartbeat()
                time.sleep(RECONNECT_SECONDS)
                continue
            while True:
                try:
                    completed = SCAN_RESULTS_QUEUE.get_nowait()
                except queue.Empty:
                    break
                if completed.get("auto_eligible") and AUTO_MODE and not completed.get("error"):
                    if completed.get("timed_out"):
                        logging.warning("Skipping automatic entries because this scan timed out.")
                    else:
                        for signal in completed.get("results", []):
                            if not AUTO_MODE:
                                break
                            if execute_signal(signal):
                                break
            if MANUAL_SCAN_REQUESTED and not SCAN_IN_PROGRESS:
                MANUAL_SCAN_REQUESTED = False
                if not start_scan(manual=True):
                    MANUAL_SCAN_REQUESTED = True
            elif not SCAN_IN_PROGRESS and time.time() - LAST_SCAN_STARTED >= SCAN_INTERVAL_SECONDS:
                start_scan(manual=False)
            heartbeat()
            time.sleep(0.5)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logging.exception("Main loop error: %s", exc)
            time.sleep(2)
    set_auto(False, "Scanner stopped")
    telegram_send("Scanner stopped. AUTO MODE OFF.")


if __name__ == "__main__":
    main()
