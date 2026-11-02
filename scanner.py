import os, time, math, logging, threading, traceback, requests, queue, re, csv, subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from iqoptionapi.stable_api import IQ_Option
from breakout_retest_filter import evaluate as evaluate_breakout_retest
from broker_results import get_binary_option_net_profit
from execution_guard import TradeExposureGuard, classify_profit

# PURE BREAKOUT & RETEST — PRACTICE ONLY. Momentum and 5-minute trend filters are disabled.
BOT_NAME = "Breakout & Retest IQ Option Scanner"
AUTO_TRADING_ALLOWED = True
STAKE, EXPIRY_MINUTES, CANDLE_SECONDS, LOOKBACK_CANDLES = 1.0, 2, 60, 60
MIN_CLOSED_CANDLES = 28
SCAN_INTERVAL_SECONDS, REQUEST_PAUSE_SECONDS, RECONNECT_SECONDS = 60, 0.12, 30
HEARTBEAT_SECONDS, REQUEST_TIMEOUT = 300, 15
MAX_SYMBOLS_PER_SCAN, TOP_SIGNALS, PROGRESS_EVERY = 40, 3, 10
MAX_SIGNAL_AGE_SECONDS, ORDER_REQUEST_TIMEOUT = 180, 12
MAX_CONCURRENT_TRADES = 3
TRADE_HISTORY_FILE = "trade_history.csv"
OUTCOME_CHECK_DELAY_SECONDS = 10
OUTCOME_RETRY_SECONDS, OUTCOME_MAX_ATTEMPTS = 30, 8
TRADE_HISTORY_BATCH_SIZE, TRADE_HISTORY_FLUSH_SECONDS = 5, 60
RETEST_FILTER_MODE = "enforce"
MARKET_MODE = "BOTH"
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
TRADE_EXPOSURE = TradeExposureGuard(max_open_trades=MAX_CONCURRENT_TRADES,
                                     expiry_seconds=EXPIRY_MINUTES * 60, expiry_grace_seconds=10)
TRADE_RECORDS = {}
TRADE_HISTORY_ROWS = []
OUTCOME_RESULTS = queue.Queue()
TRADE_HISTORY_DIRTY = 0
LAST_TRADE_HISTORY_FLUSH = 0
TRADE_TOTAL_WINS = TRADE_TOTAL_LOSSES = TRADE_TOTAL_DRAWS = 0
TRADE_TOTAL_PNL = 0.0


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
            [{"text":"SCAN MARKET"},{"text":"STATUS"}],
            [{"text":"REGULAR PAIRS"},{"text":"OTC PAIRS"},{"text":"BOTH MARKETS"}]], "resize_keyboard": True}
    try:
        response = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                 json=payload, timeout=12)
        response.raise_for_status()
        return True
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)
        return False


def connect_iq(timeout=40):
    """Connect without allowing a stalled IQ Option handshake to freeze the scanner loop."""
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD secret.")

    result_queue = queue.Queue(maxsize=1)

    def connection_worker():
        client = None
        try:
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
            balance = safe_float(client.get_balance(), 0.0)
            result_queue.put_nowait(("ok", client, balance))
        except Exception as exc:
            try:
                result_queue.put_nowait(("error", str(exc), None))
            except queue.Full:
                pass

    threading.Thread(target=connection_worker, daemon=True, name="iq-connect").start()
    try:
        result = result_queue.get(timeout=timeout)
    except queue.Empty:
        raise RuntimeError(f"IQ Option connection attempt exceeded {timeout}s; scanner will retry without freezing.")
    if result[0] != "ok":
        raise RuntimeError(result[1])
    IQ = result[1]
    balance = result[2]
    logging.info("Connected in PRACTICE. Balance %s", money(balance))
    telegram_send(f"🟢 BREAKOUT & RETEST CONNECTED\nAccount: PRACTICE ONLY\nBalance: {money(balance)}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nExpiry: 2 minutes | Stake: $1\nManual scans never place trades.")

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


def is_supported_binary_asset(asset):
    # Restrict AUTO orders to recognizable FX pairs (including OTC). IQ Option's
    # active list also contains stocks and other instruments that binary buy()
    # rejects, such as FIS:US, Ferrari, GS, MMM:US, and MA:US.
    name = str(asset or "").strip().upper().replace("/", "").replace(" ", "")
    name = re.sub(r"[-_]?OTC$", "", name)
    if not re.fullmatch(r"[A-Z]{6}", name):
        return False
    currencies = {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD",
        "HKD", "NOK", "SEK", "MXN", "TRY", "ZAR", "PLN", "CZK", "HUF",
        "DKK", "CNH", "THB", "INR", "BRL", "IDR", "MYR", "PHP", "NGN",
        "AED", "SAR"
    }
    return name[:3] in currencies and name[3:] in currencies and name[:3] != name[3:]


def get_asset_names():
    client = IQ
    if client is None:
        return []
    result_queue = queue.Queue(maxsize=1)
    def worker():
        try:
            # Do not use get_all_ACTIVES_OPCODE here: it can include stocks and
            # other instruments that binary-option buy() cannot trade. Build the
            # universe only from IQ Option's binary/turbo active tables.
            with client_api_lock(client):
                init_info = client.get_all_init()
            result_queue.put(("ok", init_info))
        except Exception as exc:
            result_queue.put(("error", str(exc)))
    threading.Thread(target=worker, daemon=True, name="asset-discovery").start()
    try:
        status, data = result_queue.get(timeout=REQUEST_TIMEOUT)
    except queue.Empty:
        logging.error("Binary/turbo asset discovery timed out.")
        if IQ is client:
            globals()["IQ"] = None
        return []
    if status != "ok":
        logging.warning("Binary/turbo asset discovery failed: %s", data)
        return []
    try:
        result = data.get("result", {})
        candidates = set()
        total_binary_turbo = 0
        for market in ("binary", "turbo"):
            market_data = result.get(market, {})
            actives = market_data.get("actives", {})
            if not isinstance(actives, dict):
                continue
            for active in actives.values():
                if not isinstance(active, dict):
                    continue
                total_binary_turbo += 1
                # Only take enabled, unsuspended binary/turbo instruments.
                if active.get("enabled") is not True or active.get("is_suspended") is not False:
                    continue
                raw_name = str(active.get("name", "")).strip()
                if "." in raw_name:
                    raw_name = raw_name.split(".", 1)[1].strip()
                if raw_name and is_supported_binary_asset(raw_name):
                    is_otc = bool(re.search(r"(?:[-_ ]?OTC)$", raw_name, re.IGNORECASE))
                    if MARKET_MODE == "REGULAR" and is_otc:
                        continue
                    if MARKET_MODE == "OTC" and not is_otc:
                        continue
                    candidates.add(raw_name)
        supported = sorted(candidates)
        logging.info("Read %d binary/turbo actives; %d enabled, unsuspended FX candidates.", total_binary_turbo, len(supported))
        return supported
    except Exception as exc:
        logging.warning("Could not parse IQ Option binary/turbo active list: %s", exc)
        return []


def fetch_candles(asset, timeframe=CANDLE_SECONDS, count=LOOKBACK_CANDLES):
    global IQ
    client = IQ
    if client is None:
        return None
    result_queue = queue.Queue(maxsize=1)
    def worker():
        try:
            with client_api_lock(client):
                candles = client.get_candles(asset, timeframe, count, int(time.time()))
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
    stamp = int(signal.get("candle_time", time.time()))
    sid = f"BR-{signal.get('asset','UNKNOWN')}-{signal['direction'].upper()}-{stamp}"
    telegram_send(
        f"📣 BREAKOUT & RETEST SIGNAL\\n{display_asset(signal.get('asset'))} — {direction}\\n"
        f"Signal ID: {sid}\\nSetup: confirmed level breakout + first valid retest\\n"
        f"Entry: first retest close; no extra confirmation candle\\nExpiry: {EXPIRY_MINUTES} minutes\\n"
        f"{'MANUAL SCAN — no trade placed.' if manual else 'AUTO MODE — PRACTICE order only.'}"
    )

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
        logging.info("Breakout/retest scan: %d of %d assets; manual=%s auto_eligible=%s",
                     len(batch), total, manual, auto_eligible)
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
            # Exclude the forming candle. A setup must be confirmed by closed bars.
            closed = candles[:-1]
            if len(closed) < MIN_CLOSED_CANDLES:
                rejected["insufficient closed candles"] = rejected.get("insufficient closed candles", 0) + 1
            else:
                retest = evaluate_breakout_retest(closed)
                if retest.state.value != "retested":
                    reason = "; ".join(retest.reasons) if retest.reasons else retest.state.value
                    rejected[reason] = rejected.get(reason, 0) + 1
                elif retest.retest_index != len(closed) - 1:
                    # Never chase a retest after its first completed candle.
                    rejected["first retest candle already passed"] = rejected.get("first retest candle already passed", 0) + 1
                else:
                    retest_candle = closed[retest.retest_index]
                    stamp = int(retest_candle.get("from") or time.time())
                    candidate = {
                        "asset": asset,
                        "direction": "call" if retest.direction == "up" else "put",
                        "score": 100,
                        "candle_time": stamp,
                        "price": retest_candle["close"],
                        "time": utc_now().strftime("%H:%M:%S UTC"),
                        "source": "Pure breakout + first valid retest",
                        "level": retest.level,
                    }
                    age = time.time() - stamp
                    if age > MAX_SIGNAL_AGE_SECONDS or age < -5:
                        rejected["first retest signal stale"] = rejected.get("first retest signal stale", 0) + 1
                    elif manual:
                        candidate["entry_time"] = max(int(time.time()), stamp + CANDLE_SECONDS)
                        if MANUAL_SIGNAL_SENT_BY_ASSET.get(asset) != str(stamp):
                            results.append(candidate)
                            MANUAL_SIGNAL_SENT_BY_ASSET[asset] = str(stamp)
                            if len(results) <= TOP_SIGNALS:
                                send_signal(candidate, manual=True)
                        else:
                            rejected["setup already reported"] = rejected.get("setup already reported", 0) + 1
                    elif LAST_SIGNAL_BY_ASSET.get(asset) != str(stamp):
                        results.append(candidate)
            if index % PROGRESS_EVERY == 0:
                logging.info("Scan progress %d/%d; valid first retests=%d", index, len(batch), len(results))
            time.sleep(REQUEST_PAUSE_SECONDS)
    except Exception as exc:
        error_text = str(exc)
        logging.exception("Breakout/retest market scan failed")
    finally:
        if total:
            ASSET_CURSOR = (start + max(attempted, 1)) % total
        LAST_SCAN_FINISHED = time.time()
        LAST_SCAN_CANDIDATES = len(results)
        LAST_SCAN_SCANNED, LAST_SCAN_TOTAL = scanned, total
        LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR = timed_out, error_text
        LAST_REJECTIONS = dict(sorted(rejected.items(), key=lambda item: item[1], reverse=True)[:8])
        logging.info("Breakout/retest scan finished: scanned=%d/%d valid_retests=%d timeout=%s error=%s",
                     scanned, total, len(results), timed_out, error_text or "none")
        if manual and not results:
            top = sorted(rejected.items(), key=lambda item: item[1], reverse=True)[:3]
            reasons = "; ".join(f"{name}: {count}" for name, count in top) or "No valid closed candles returned."
            suffix = "\\nConnection timed out; scanner will reconnect." if timed_out else (f"\\n{error_text}" if error_text else "")
            telegram_send(f"NO TRADE — NO VALID FIRST RETEST\\nScanned: {scanned}/{total}\\nTop rejection reasons: {reasons}{suffix}")
        SCAN_RESULTS_QUEUE.put({"manual": manual, "auto_eligible": auto_eligible, "results": results,
                                "timed_out": timed_out, "error": error_text})
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


def current_open_trade_count():
    with STATE_LOCK:
        return TRADE_EXPOSURE.count(time.time())


def load_trade_history():
    """Load the CSV ledger and restore any still-open or unresolved practice orders."""
    global TRADE_HISTORY_ROWS, TRADE_TOTAL_WINS, TRADE_TOTAL_LOSSES
    global TRADE_TOTAL_DRAWS, TRADE_TOTAL_PNL
    try:
        with open(TRADE_HISTORY_FILE, newline="", encoding="utf-8") as handle:
            TRADE_HISTORY_ROWS = list(csv.DictReader(handle))
        now = time.time()
        restored_open = 0
        for row in TRADE_HISTORY_ROWS:
            outcome = str(row.get("outcome", "")).upper()
            pnl = safe_float(row.get("net_profit"), 0.0)
            if outcome == "WIN":
                TRADE_TOTAL_WINS += 1
                TRADE_TOTAL_PNL += pnl
            elif outcome == "LOSS":
                TRADE_TOTAL_LOSSES += 1
                TRADE_TOTAL_PNL += pnl
            elif outcome == "DRAW":
                TRADE_TOTAL_DRAWS += 1
            order_id = str(row.get("order_id", "")).strip()
            if not order_id or outcome != "PENDING" or order_id in TRADE_RECORDS:
                continue
            try:
                accepted_at = datetime.fromisoformat(row["accepted_at_utc"]).timestamp()
                expiry_minutes = int(row.get("expiry_minutes") or EXPIRY_MINUTES)
                expires_at = accepted_at + expiry_minutes * 60 + 10
                record = dict(row)
                record.update({
                    "expires_at": expires_at, "outcome_attempts": 0,
                    "next_outcome_check": 0, "outcome_thread": None,
                    "recovered": True,
                })
                TRADE_RECORDS[order_id] = record
                if expires_at > now:
                    TRADE_EXPOSURE.register(order_id, row.get("asset", ""),
                                             row.get("direction", ""), accepted_at)
                    restored_open += 1
            except Exception as exc:
                logging.warning("Could not restore pending order %s: %s", order_id, exc)
        logging.info("Loaded %d trade-history rows; restored %d still-open orders.",
                     len(TRADE_HISTORY_ROWS), restored_open)
    except FileNotFoundError:
        TRADE_HISTORY_ROWS = []
    except Exception:
        logging.exception("Could not load trade history CSV.")
        TRADE_HISTORY_ROWS = []


def persist_trade_history(force=False):
    """Batch-save verified order outcomes to the repository CSV on Actions."""
    global TRADE_HISTORY_DIRTY, LAST_TRADE_HISTORY_FLUSH
    now = time.time()
    if not TRADE_HISTORY_ROWS:
        return
    if not force and TRADE_HISTORY_DIRTY < TRADE_HISTORY_BATCH_SIZE and now - LAST_TRADE_HISTORY_FLUSH < TRADE_HISTORY_FLUSH_SECONDS:
        return
    fields = ["accepted_at_utc", "order_id", "asset", "direction", "stake",
              "expiry_minutes", "signal_source", "outcome", "net_profit",
              "result_checked_at_utc", "result_error"]
    try:
        with open(TRADE_HISTORY_FILE, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows([{key: row.get(key, "") for key in fields} for row in TRADE_HISTORY_ROWS])
        if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                           check=True, capture_output=True, text=True, timeout=15)
            subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
                           check=True, capture_output=True, text=True, timeout=15)
            subprocess.run(["git", "add", TRADE_HISTORY_FILE],
                           check=True, capture_output=True, text=True, timeout=15)
            staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                    capture_output=True, text=True, timeout=15)
            if staged.returncode != 0:
                subprocess.run(["git", "commit", "-m", "Update verified practice trade history"],
                               check=True, capture_output=True, text=True, timeout=30)
            branch = os.getenv("GITHUB_REF_NAME", "main")
            subprocess.run(["git", "push", "origin", f"HEAD:{branch}"],
                           check=True, capture_output=True, text=True, timeout=45)
        TRADE_HISTORY_DIRTY = 0
        LAST_TRADE_HISTORY_FLUSH = now
        logging.info("Trade history saved: %d rows.", len(TRADE_HISTORY_ROWS))
    except Exception as exc:
        logging.exception("Trade history persistence failed: %s", exc)


def _outcome_worker(order_id, client, recovered=False):
    try:
        pnl = get_binary_option_net_profit(
            client, order_id, lock=client_api_lock(client), timeout=12
        )
        OUTCOME_RESULTS.put((str(order_id), "ok", float(pnl)))
    except Exception as exc:
        OUTCOME_RESULTS.put((str(order_id), "error", str(exc)))

def process_trade_outcomes():
    """Start background broker-result checks and apply completed results."""
    global TRADE_HISTORY_DIRTY, TRADE_TOTAL_WINS, TRADE_TOTAL_LOSSES
    global TRADE_TOTAL_DRAWS, TRADE_TOTAL_PNL
    now = time.time()
    while True:
        try:
            order_id, status, value = OUTCOME_RESULTS.get_nowait()
        except queue.Empty:
            break
        record = TRADE_RECORDS.get(order_id)
        if not record or record.get("outcome") != "PENDING":
            continue
        if status == "ok":
            pnl = round(float(value), 2)
            outcome = classify_profit(pnl)
            record["outcome"] = outcome
            record["net_profit"] = f"{pnl:.2f}"
            record["result_checked_at_utc"] = utc_now().isoformat()
            record["result_error"] = ""
            TRADE_TOTAL_PNL += pnl
            if outcome == "WIN":
                TRADE_TOTAL_WINS += 1
            elif outcome == "LOSS":
                TRADE_TOTAL_LOSSES += 1
            else:
                TRADE_TOTAL_DRAWS += 1
            fields = ("accepted_at_utc", "order_id", "asset", "direction", "stake",
                      "expiry_minutes", "signal_source", "outcome", "net_profit",
                      "result_checked_at_utc", "result_error")
            for row in TRADE_HISTORY_ROWS:
                if str(row.get("order_id")) == order_id:
                    row.update({key: record.get(key, "") for key in fields})
                    break
            logging.info("TRADE RESULT order_id=%s asset=%s outcome=%s net_pnl=%s",
                         order_id, record["asset"], outcome, money(pnl))
            telegram_send(f"📊 PRACTICE TRADE RESULT\n{display_asset(record['asset'])} — {record['direction'].upper()}\n"
                          f"Order ID: {order_id}\nResult: {outcome}\nNet P/L: {money(pnl)}")
            TRADE_HISTORY_DIRTY += 1
            persist_trade_history()
        else:
            record["result_error"] = str(value)[:180]
            record["next_outcome_check"] = now + OUTCOME_RETRY_SECONDS
            logging.warning("Outcome check failed for order %s (attempt %s): %s",
                            order_id, record["outcome_attempts"], value)
            if int(record.get("outcome_attempts", 0))  >= OUTCOME_MAX_ATTEMPTS:
                record["outcome"] = "UNVERIFIED"
                record["result_checked_at_utc"] = utc_now().isoformat()
                fields = ("accepted_at_utc", "order_id", "asset", "direction", "stake",
                          "expiry_minutes", "signal_source", "outcome", "net_profit",
                          "result_checked_at_utc", "result_error")
                for row in TRADE_HISTORY_ROWS:
                    if str(row.get("order_id")) == order_id:
                        row.update({key: record.get(key, "") for key in fields})
                        break
                telegram_send(f"⚠️ RESULT UNVERIFIED\nOrder ID: {order_id}\nBroker result unavailable after 8 bounded checks. Check IQ Option history.")
                TRADE_HISTORY_DIRTY += 1
                persist_trade_history()

    for order_id, record in list(TRADE_RECORDS.items()):
        if record.get("outcome") != "PENDING":
            continue
        if now < float(record.get("expires_at", now)) + OUTCOME_CHECK_DELAY_SECONDS:
            continue
        worker = record.get("outcome_thread")
        if worker is not None and worker.is_alive():
            continue
        if now < float(record.get("next_outcome_check", 0)):
            continue
        if int(record.get("outcome_attempts", 0))  >= OUTCOME_MAX_ATTEMPTS:
            continue
        if IQ is None or not practice_mode_verified():
            record["next_outcome_check"] = now + OUTCOME_RETRY_SECONDS
            continue
        record["outcome_attempts"] = int(record.get("outcome_attempts", 0)) + 1
        worker = threading.Thread(target=_outcome_worker,
                                  args=(order_id, IQ, bool(record.get("recovered", False))),
                                  daemon=True, name=f"trade-result-{order_id}")
        record["outcome_thread"] = worker
        worker.start()


def execute_signal(signal):
    global TRADE_HISTORY_DIRTY
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
    with STATE_LOCK:
        allowed, exposure_reason = TRADE_EXPOSURE.can_open(asset, time.time())
        if not allowed:
            logging.info("Skipping %s %s: %s (open trades %d/%d).",
                         asset, direction.upper(), exposure_reason,
                         TRADE_EXPOSURE.count(time.time()), MAX_CONCURRENT_TRADES)
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
        detail = str(response)
        logging.warning("Order request failed for %s: %r", asset, response)
        if "insufficient funds" in detail.lower():
            set_auto(False, "Broker rejected order: insufficient funds")
            telegram_send("🛑 AUTO MODE OFF — IQ Option reported insufficient funds. No more orders will be attempted. Check the PRACTICE balance before manually restarting.")
        return False
    success, order_id = bool(response[0]), response[1]
    if not success or order_id is None or str(order_id) == "":
        logging.info("Practice order rejected for %s: %r", asset, response)
        detail = str(response[1] if len(response) > 1 else "broker returned no order ID")
        detail = re.sub(r"[\\r\\n]+", " ", detail)[:220]
        telegram_send(f"⚪ ORDER REJECTED\n{display_asset(asset)} {direction.upper()}\nNo trade was confirmed.\nBroker response: {detail}")
        if "insufficient funds" in detail.lower():
            set_auto(False, "Broker rejected order: insufficient funds")
            telegram_send("🛑 AUTO MODE OFF — insufficient PRACTICE funds. Resolve the balance issue, then restart AUTO manually.")
        return False
    accepted_at = time.time()
    expires_at = accepted_at + EXPIRY_MINUTES * 60 + 10
    with STATE_LOCK:
        TRADE_EXPOSURE.register(order_id, asset, direction, accepted_at)
        record = {
            "accepted_at_utc": utc_now().isoformat(), "order_id": str(order_id),
            "asset": asset, "direction": direction, "stake": f"{STAKE:.2f}",
            "expiry_minutes": str(EXPIRY_MINUTES),
            "signal_source": str(signal.get("source", "unknown")),
            "outcome": "PENDING", "net_profit": "",
            "result_checked_at_utc": "", "result_error": "",
            "expires_at": expires_at, "outcome_attempts": 0,
            "next_outcome_check": 0, "outcome_thread": None,
        }
        TRADE_RECORDS[str(order_id)] = record
        TRADE_HISTORY_ROWS.append({key: record.get(key, "") for key in
            ("accepted_at_utc", "order_id", "asset", "direction", "stake",
             "expiry_minutes", "signal_source", "outcome", "net_profit",
             "result_checked_at_utc", "result_error")})
        TRADE_HISTORY_DIRTY += 1
        persist_trade_history()
    telegram_send(f"🟡 PRACTICE ORDER ACCEPTED\n{display_asset(asset)} — {direction.upper()}\nOrder ID: {order_id}\n"
                  f"Stake: {money(STAKE)} | Expiry: {EXPIRY_MINUTES} minutes\n"
                  f"Open exposure: {TRADE_EXPOSURE.count(time.time())}/{MAX_CONCURRENT_TRADES}\n"
                  f"Broker result will be checked after expiry.")
    logging.info("Practice order accepted for %s; order id=%s; exposure=%d/%d",
                 asset, order_id, TRADE_EXPOSURE.count(time.time()), MAX_CONCURRENT_TRADES)
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
    return (f"📊 BREAKOUT & RETEST STATUS\nConnection: {'CONNECTED' if connected else 'DISCONNECTED'}\n"
            f"Practice verification: {'OK' if practice else 'NOT VERIFIED'}\nBalance: {balance}\n"
            f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nStake: $1 | Expiry: 2 minutes\n"
            f"Market selection: {MARKET_MODE}\n5-minute trend: ON | Consolidation protection: ON\nBreakout/retest: first valid retest within setup window (no confirmation)\n"
            f"Open trades: {TRADE_EXPOSURE.count(time.time())}/{MAX_CONCURRENT_TRADES}\n"
            f"Verified results this run: W{TRADE_TOTAL_WINS}/L{TRADE_TOTAL_LOSSES}/D{TRADE_TOTAL_DRAWS} | Net P/L: {money(TRADE_TOTAL_PNL)}\n"
            f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {age}\n"
            f"Last batch: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\nCandidates: {LAST_SCAN_CANDIDATES}\n"
            f"Scan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
            f"Last timeout: {'YES' if LAST_SCAN_TIMED_OUT else 'NO'}\n"
            f"Last error: {LAST_SCAN_ERROR or 'None'}\n"
            f"Top rejection reasons: {'; '.join(f'{k}: {v}' for k,v in LAST_REJECTIONS.items()) or 'No scan data yet'}")


def process_telegram_update(update):
    global MANUAL_SCAN_REQUESTED, MARKET_MODE
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
            telegram_send(f"🟢 AUTO MODE ON\nPRACTICE verified. {money(STAKE)} stake / {EXPIRY_MINUTES}-minute expiry.\nMax concurrent trades: {MAX_CONCURRENT_TRADES}; one open trade per asset.\nBroker outcomes are checked and saved to trade_history.csv.")
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
    elif command in ("REGULAR PAIRS", "/MARKET REGULAR"):
        MARKET_MODE = "REGULAR"
        telegram_send("Market selection: REGULAR FX pairs only. AUTO setting unchanged.")
    elif command in ("OTC PAIRS", "/MARKET OTC"):
        MARKET_MODE = "OTC"
        telegram_send("Market selection: OTC FX pairs only. AUTO setting unchanged.")
    elif command in ("BOTH MARKETS", "/MARKET BOTH"):
        MARKET_MODE = "BOTH"
        telegram_send("Market selection: BOTH regular and OTC FX pairs. AUTO setting unchanged.")
    elif command in ("STATUS", "/STATUS"):
        telegram_send(status_message())
    elif command in ("/HELP", "HELP"):
        telegram_send("Commands:\nSTART AUTO — enable PRACTICE trades\nSTOP AUTO — stop new automatic trades\nSCAN MARKET — report-only signal scan\nREGULAR PAIRS / OTC PAIRS / BOTH MARKETS — choose market\nSTATUS — connection and scan status\nOpen exposure is capped at 3 trades total and 1 per pair. Broker outcomes are checked and saved to trade_history.csv.")
    else:
        telegram_send("Unknown command. Use START AUTO, STOP AUTO, SCAN MARKET, REGULAR PAIRS, OTC PAIRS, BOTH MARKETS, STATUS, or /HELP.")


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
    process_trade_outcomes()
    persist_trade_history()
    telegram_send(f"💓 BREAKOUT & RETEST HEARTBEAT\nAccount: PRACTICE ONLY\n"
                  f"Connection: {'OK' if practice_mode_verified() else 'NOT VERIFIED'}\n"
                  f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
                  f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\n"
                  f"Candidates: {LAST_SCAN_CANDIDATES}\nScan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
                  f"No qualifying setup means NO TRADE.")


def main():
    global RUNNING, MANUAL_SCAN_REQUESTED
    load_trade_history()
    telegram_send("🚀 BREAKOUT & RETEST STARTING\nPRACTICE-only scanner is connecting.\nManual SCAN MARKET sends signals only and never places trades.\nMax open exposure: 3 trades total, 1 per pair. Broker results are checked and saved to trade_history.csv.")
    threading.Thread(target=telegram_listener, daemon=True, name="telegram-listener").start()
    while RUNNING:
        try:
            process_trade_outcomes()
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
                            if TRADE_EXPOSURE.count(time.time()) >= MAX_CONCURRENT_TRADES:
                                logging.info("Global exposure cap reached; remaining candidates skipped for this scan.")
                                break
                            execute_signal(signal)
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
    process_trade_outcomes()
    persist_trade_history(force=True)
    telegram_send("Scanner stopped. AUTO MODE OFF.")


if __name__ == "__main__":
    main()
