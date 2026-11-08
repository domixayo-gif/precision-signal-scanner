import os, time, math, logging, threading, traceback, requests, queue, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from iqoptionapi.stable_api import IQ_Option
from breakout_retest_filter import build_entry_signal
from trend_filter import classify_trend, is_strong_trend
from execution_guard import TradeExposureGuard, classify_profit
from broker_results import get_binary_option_net_profit
from ema_martingale import stake_for_step, next_step_after_result, required_balance_from_step, MAX_STEPS
from momentum_strategy import calculate_signal as calculate_momentum_signal
from ema_strategy import calculate_signal as calculate_ema_signal
from fusion_strategy import calculate_signal as calculate_fusion_signal
import base64, json

def select_breakout_expiry(direction, trend_5m, trend_15m, strong_15m=False):
    """Choose 5/10/15m from closed higher-timeframe trend alignment.

    The 5-minute trend remains mandatory. Sideways, unavailable, or opposing
    15-minute trend selects 5m; aligned 15m selects 10m; strong alignment 15m.
    """
    direction = str(direction or "").lower()
    required = "up" if direction == "call" else "down" if direction == "put" else ""
    if not required or str(trend_5m).lower() != required:
        return 5
    if str(trend_15m).lower() != required:
        return 5
    return 15 if bool(strong_15m) else 10


# PURE BREAKOUT & RETEST — PRACTICE ONLY.
BOT_NAME = "Breakout & Retest IQ Option Scanner"
AUTO_TRADING_ALLOWED = True
STAKE, EXPIRY_MINUTES, CANDLE_SECONDS, LOOKBACK_CANDLES = 1.0, 2, 60, 60
MIN_CLOSED_CANDLES = 28
SCAN_INTERVAL_SECONDS, REQUEST_PAUSE_SECONDS, RECONNECT_SECONDS = 30, 0.12, 30
HEARTBEAT_SECONDS, REQUEST_TIMEOUT = 300, 15
MAX_SYMBOLS_PER_SCAN, TOP_SIGNALS, PROGRESS_EVERY = 40, 3, 10
MAX_SIGNAL_AGE_SECONDS, ORDER_REQUEST_TIMEOUT = 90, 12
MAX_CONCURRENT_TRADES = 3
MAX_TRADES_PER_AUTO_SESSION = 20
MARKET_MODE = "BOTH"
STRATEGY_SETTINGS = {
    "B": {"name": "Breakout & Retest", "expiry_minutes": 2, "scan_interval_seconds": 30, "lookback": 60},
    "M": {"name": "Momentum 10", "expiry_minutes": 1, "scan_interval_seconds": 60, "lookback": 50},
    "E": {"name": "EMA Precision — 20/50 Pullback + ADX/DI + CHOP/ATR", "expiry_minutes": 2, "scan_interval_seconds": 30, "lookback": 300},
    "F": {"name": "Precision Fusion X — Strict Confluence", "expiry_minutes": 2, "scan_interval_seconds": 30, "lookback": 80},
}
ACTIVE_STRATEGY = "B"
STRATEGY_GENERATION = 0
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")
# Enabled: executable 1-minute setups must agree with the closed 5-minute trend.
FIVE_MINUTE_TREND_FILTER_ENABLED = True
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
EXECUTION_QUEUE = queue.Queue()
LAST_SIGNAL_BY_ASSET = {}
MANUAL_SIGNAL_SENT_BY_ASSET = {}
LAST_HEARTBEAT = LAST_SCAN_STARTED = LAST_SCAN_FINISHED = 0
LAST_ASSET_COUNT = LAST_SCAN_SCANNED = LAST_SCAN_TOTAL = LAST_SCAN_CANDIDATES = ASSET_CURSOR = 0
LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR = False, ""
LAST_REJECTIONS = {}
LAST_ASSET_DIAGNOSTIC = "Asset discovery has not run yet."
LAST_ASSET_DISCOVERY_MODE = "NOT RUN"
TRADE_EXPOSURE = TradeExposureGuard(max_open_trades=MAX_CONCURRENT_TRADES,
                                     expiry_seconds=2 * 60, expiry_grace_seconds=10)
ORDER_ATTEMPTS = ORDER_ACCEPTED = ORDER_REJECTED = 0
AUTO_SESSION_ACCEPTED = 0
LAST_ORDER_STATUS = "No order attempt yet"
EMA_MARTINGALE_STATUS = "READY — next EMA trade $1"


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
        payload["reply_markup"] = {"keyboard": [
            [{"text":"B — BREAKOUT"},{"text":"M — MOMENTUM"}],
            [{"text":"E — EMA"},{"text":"F — FUSION"}],
            [{"text":"START AUTO"}],
            [{"text":"STOP AUTO"},{"text":"SCAN MARKET"}],
            [{"text":"STATUS"}],
            [{"text":"REGULAR PAIRS"},{"text":"OTC PAIRS"},{"text":"BOTH MARKETS"}]
        ], "resize_keyboard": True}
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
    telegram_send(f"🟢 IQ OPTION CONNECTED\nAccount: PRACTICE ONLY\nBalance: {money(balance)}\nSelected strategy: {ACTIVE_STRATEGY} — {STRATEGY_SETTINGS[ACTIVE_STRATEGY]['name']}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nStake: $1\nManual scans never place trades.")

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


def binary_fx_asset_info(asset):
    """Return (canonical FX symbol, is_otc) for supported IQ Option asset names."""
    raw = str(asset or "").strip().upper().replace("/", "").replace(" ", "")
    if "." in raw:
        raw = raw.rsplit(".", 1)[1].strip()
    if not raw or "HC" in raw:
        return "", False
    is_otc = bool(re.search(r"(?:[-_ ]?OTC)$", raw))
    if is_otc:
        raw = re.sub(r"(?:[-_ ]?OTC)$", "", raw)
    # IQ Option's regular binary/turbo feed commonly labels FX as EURNZD-op.
    # -op is a platform suffix, not an OTC marker.
    if raw.endswith("-OP"):
        raw = raw[:-3]
    name = raw.replace("-", "").replace("_", "")
    if not re.fullmatch(r"[A-Z]{6}", name):
        return "", is_otc
    currencies = {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD",
        "HKD", "NOK", "SEK", "MXN", "TRY", "ZAR", "PLN", "CZK", "HUF",
        "DKK", "CNH", "THB", "INR", "BRL", "IDR", "MYR", "PHP", "NGN",
        "AED", "SAR"
    }
    if name[:3] not in currencies or name[3:] not in currencies or name[:3] == name[3:]:
        return "", is_otc
    return name, is_otc


def is_supported_binary_asset(asset):
    return bool(binary_fx_asset_info(asset)[0])


def get_asset_names():
    """Return only binary/turbo FX assets that match the selected market mode."""
    global LAST_ASSET_DIAGNOSTIC, LAST_ASSET_DISCOVERY_MODE
    scan_mode = str(MARKET_MODE).upper()
    LAST_ASSET_DISCOVERY_MODE = scan_mode
    client = IQ
    if client is None:
        return []
    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            # Read the broker's binary/turbo tables. The broad opcode list can
            # contain stocks and instruments that binary buy() cannot execute.
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
    if not isinstance(data, dict):
        logging.warning("Binary/turbo asset discovery returned %s, not a dictionary.",
                        type(data).__name__)
        return []

    try:
        # IQ Option library versions may return the tables directly or wrap
        # them in "result" (and some responses use "data").
        result = data.get("result")
        if not isinstance(result, dict):
            result = data.get("data")
        if not isinstance(result, dict):
            result = data
        if not any(key in result for key in ("binary", "turbo")):
            logging.warning("No binary/turbo tables found in IQ Option init response; keys=%s",
                            list(result.keys())[:20])
            return []

        candidates = set()
        counts = {
            "binary_turbo_entries": 0,
            "enabled_entries": 0,
            "supported_fx": 0,
            "regular": 0,
            "otc": 0,
        }

        def flag_is_true(value):
            # Broker/library versions may encode flags as bool, 0/1 or text.
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return False

        for market in ("binary", "turbo"):
            market_data = result.get(market, {})
            if not isinstance(market_data, dict):
                continue
            actives = market_data.get("actives", {})
            if isinstance(actives, dict):
                active_items = actives.values()
            elif isinstance(actives, list):
                active_items = actives
            else:
                continue

            for active in active_items:
                if not isinstance(active, dict):
                    continue
                counts["binary_turbo_entries"] += 1

                # Require an explicit enabled flag, but treat numeric/string
                # representations consistently. Only an explicit suspended
                # flag blocks an otherwise enabled asset.
                if not flag_is_true(active.get("enabled")):
                    continue
                counts["enabled_entries"] += 1
                if flag_is_true(active.get("is_suspended")):
                    continue

                raw_name = str(active.get("name", "")).strip()
                symbol, is_otc = binary_fx_asset_info(raw_name)
                if not symbol:
                    continue

                counts["supported_fx"] += 1
                counts["otc" if is_otc else "regular"] += 1

                if scan_mode == "REGULAR" and is_otc:
                    continue
                if scan_mode == "OTC" and not is_otc:
                    continue
                # Regular "-op" broker names are converted to the canonical six-letter
                # symbol for candle requests and binary buy(); OTC names retain OTC suffix.
                candidates.add(raw_name.rsplit(".", 1)[-1].strip() if is_otc else symbol)

        supported = sorted(candidates)
        if supported:
            LAST_ASSET_DIAGNOSTIC = (
                f"Found {len(supported)} FX pairs in {scan_mode} mode "
                f"({counts['regular']} regular, {counts['otc']} OTC available)."
            )
        elif scan_mode == "REGULAR" and counts["otc"] > 0 and counts["regular"] == 0:
            LAST_ASSET_DIAGNOSTIC = (
                f"No regular FX pairs are currently enabled. The broker returned "
                f"{counts['otc']} OTC FX pairs; select BOTH MARKETS or OTC PAIRS."
            )
        elif scan_mode == "OTC" and counts["regular"] > 0 and counts["otc"] == 0:
            LAST_ASSET_DIAGNOSTIC = (
                f"No OTC FX pairs are currently enabled. The broker returned "
                f"{counts['regular']} regular FX pairs; select BOTH MARKETS or REGULAR PAIRS."
            )
        elif counts["supported_fx"] == 0:
            LAST_ASSET_DIAGNOSTIC = (
                f"The broker returned {counts['binary_turbo_entries']} binary/turbo entries, "
                f"but none matched the supported FX-pair filter."
            )
        else:
            LAST_ASSET_DIAGNOSTIC = (
                f"No FX pairs matched market mode {scan_mode}; "
                f"regular={counts['regular']}, OTC={counts['otc']}."
            )
        logging.info(
            "IQ Option asset discovery mode=%s: entries=%d enabled=%d supported_fx=%d "
            "regular=%d otc=%d selected=%d",
            scan_mode, counts["binary_turbo_entries"], counts["enabled_entries"],
            counts["supported_fx"], counts["regular"], counts["otc"], len(supported)
        )
        logging.info("Asset discovery detail: %s", LAST_ASSET_DIAGNOSTIC)
        return supported
    except Exception as exc:
        logging.exception("Could not parse IQ Option binary/turbo active list: %s", exc)
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
    zone = signal.get("zone") or (0.0, 0.0)
    telegram_send(
        f"📣 BREAKOUT & RETEST SIGNAL\n{display_asset(signal.get('asset'))} — {direction}\n"
        f"Signal ID: {sid}\nConfirmed breakout level: {signal.get('level', 0):.6f}\n"
        f"Retest zone: {zone[0]:.6f} – {zone[1]:.6f}\n"
        f"5-minute trend: {str(signal.get('trend_5m', 'not recorded')).upper()}\n"
        f"Entry: first valid closed retest candle; no extra confirmation ({lagos_time(stamp)})\n"
        f"Fixed expiry: {int(signal.get('expiry_minutes', 2))} minutes\n"
        f"{'MANUAL SCAN — no trade placed.' if manual else 'AUTO MODE — PRACTICE order only.'}"
    )

def scan_market(manual=False, auto_eligible=False, strategy=None, generation=None):
    global SCAN_IN_PROGRESS, ASSET_CURSOR, LAST_ASSET_COUNT, LAST_SCAN_FINISHED
    global LAST_SCAN_CANDIDATES, LAST_SCAN_SCANNED, LAST_SCAN_TOTAL, LAST_SCAN_TIMED_OUT
    global LAST_SCAN_ERROR, LAST_REJECTIONS
    strategy = str(strategy or ACTIVE_STRATEGY).upper()
    generation = STRATEGY_GENERATION if generation is None else int(generation)
    if strategy in ("M", "E", "F"):
        return scan_indicator_market(manual, auto_eligible, strategy, generation)
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
            error_text = LAST_ASSET_DIAGNOSTIC
            return
        start = ASSET_CURSOR % total
        batch = [assets[(start+i) % total] for i in range(min(MAX_SYMBOLS_PER_SCAN, total))]
        logging.info("Breakout/retest scan: %d of %d assets; manual=%s auto_eligible=%s",
                     len(batch), total, manual, auto_eligible)
        for index, asset in enumerate(batch, 1):
            if not RUNNING or strategy != ACTIVE_STRATEGY or generation != STRATEGY_GENERATION:
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
            if strategy != ACTIVE_STRATEGY or generation != STRATEGY_GENERATION:
                break
            # Determine closed bars from their timestamps, rather than blindly
            # dropping the final item (some broker responses omit the forming bar).
            now = int(time.time())
            closed = [c for c in candles
                      if int(c.get("from") or 0) + CANDLE_SECONDS <= now]
            if len(closed) < MIN_CLOSED_CANDLES:
                # Some IQ Option responses contain only a short candle history
                # even when 60 bars were requested. Retry once with a wider
                # history window; keep the exact same strategy thresholds.
                initial_closed_count = len(closed)
                retry_candles = fetch_candles(asset, timeframe=CANDLE_SECONDS, count=120)
                if retry_candles is not None:
                    retry_closed = [c for c in retry_candles
                                    if int(c.get("from") or 0) + CANDLE_SECONDS <= now]
                    if len(retry_closed) > len(closed):
                        candles, closed = retry_candles, retry_closed
                if len(closed) < MIN_CLOSED_CANDLES:
                    logging.info(
                        "Insufficient candle history for %s: initial=%d retry=%d required=%d",
                        asset, initial_closed_count, len(closed), MIN_CLOSED_CANDLES
                    )
                    rejected["insufficient closed candles"] = rejected.get("insufficient closed candles", 0) + 1
            if len(closed) >= MIN_CLOSED_CANDLES:
                candidate, retest = build_entry_signal(asset, closed, CANDLE_SECONDS)
                if candidate is None:
                    if retest.state.value == "retested":
                        rejected["first valid retest candle already passed; no late entry"] = rejected.get("first valid retest candle already passed; no late entry", 0) + 1
                    else:
                        reason = "; ".join(retest.reasons) if retest.reasons else retest.state.value
                        rejected[reason] = rejected.get(reason, 0) + 1
                else:
                    candidate.update({"strategy": "B", "generation": generation,
                                      "expiry_minutes": 2,
                                      "max_signal_age_seconds": MAX_SIGNAL_AGE_SECONDS})
                    # A valid 1-minute retest is executable only when the latest
                    # CLOSED 5-minute candles confirm the same directional trend.
                    trend_candles = fetch_candles(asset, timeframe=300, count=60)
                    if trend_candles is None:
                        rejected["5-minute trend data unavailable"] = rejected.get("5-minute trend data unavailable", 0) + 1
                        trend_candles = []
                    now = int(time.time())
                    closed_trend = [c for c in trend_candles
                                    if int(c.get("from") or 0) + 300 <= now]
                    trend = classify_trend(closed_trend)
                    if strategy != ACTIVE_STRATEGY or generation != STRATEGY_GENERATION:
                        break
                    required_trend = "up" if candidate["direction"] == "call" else "down"
                    if trend != required_trend:
                        reason = "5-minute trend unclear/sideways" if trend == "sideways" else "1-minute setup disagrees with 5-minute trend"
                        rejected[reason] = rejected.get(reason, 0) + 1
                        logging.info("Trend filter blocked %s %s: 5m trend=%s required=%s",
                                     asset, candidate["direction"].upper(), trend, required_trend)
                    else:
                        candidate["trend_5m"] = trend
                        candidate["expiry_minutes"] = 2
                        # candle_time is the retest candle CLOSE time, not its open.
                        stamp = int(candidate["candle_time"])
                        age = time.time() - stamp
                        if age > MAX_SIGNAL_AGE_SECONDS or age < -5:
                            rejected["first retest signal stale"] = rejected.get("first retest signal stale", 0) + 1
                        elif manual:
                            candidate["entry_time"] = max(int(time.time()), stamp)
                            signal_key = f"{strategy}:{asset}"
                            if MANUAL_SIGNAL_SENT_BY_ASSET.get(signal_key) != str(stamp):
                                results.append(candidate)
                                MANUAL_SIGNAL_SENT_BY_ASSET[signal_key] = str(stamp)
                                if len(results) <= TOP_SIGNALS:
                                    send_signal(candidate, manual=True)
                            else:
                                rejected["setup already reported"] = rejected.get("setup already reported", 0) + 1
                        elif LAST_SIGNAL_BY_ASSET.get(f"{strategy}:{asset}") != str(stamp):
                            results.append(candidate)
                            if (auto_eligible and AUTO_MODE and strategy == ACTIVE_STRATEGY
                                    and generation == STRATEGY_GENERATION):
                                # Submit fresh setups through the main loop immediately,
                                # without waiting for the remaining symbols in this batch.
                                EXECUTION_QUEUE.put(candidate)
                                logging.info("Fresh first-retest setup queued for immediate execution: %s %s (5m trend=%s)",
                                             asset, candidate["direction"].upper(), trend)
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
            suffix = "\nConnection timed out; scanner will reconnect." if timed_out else (f"\n{error_text}" if error_text else "")
            telegram_send(f"NO TRADE — NO VALID FIRST RETEST\nScanned: {scanned}/{total}\nTop rejection reasons: {reasons}{suffix}")
        SCAN_RESULTS_QUEUE.put({"manual": manual, "auto_eligible": auto_eligible, "results": results,
                                "timed_out": timed_out, "error": error_text,
                                "strategy": strategy, "generation": generation})
        with STATE_LOCK:
            SCAN_IN_PROGRESS = False



def send_indicator_signal(signal, manual=False):
    strategy = signal.get("strategy", ACTIVE_STRATEGY)
    name = STRATEGY_SETTINGS.get(strategy, {}).get("name", strategy)
    direction = "CALL 🟢" if signal["direction"] == "call" else "PUT 🔴"
    expiry = int(signal.get("expiry_minutes", STRATEGY_SETTINGS[strategy]["expiry_minutes"]))
    stamp = int(signal.get("candle_time", time.time()))
    if strategy == "M":
        detail = (f"Momentum score: {signal.get('score', 0)}/100\n"
                  f"Normalized momentum: {signal.get('normalized_momentum', 0):.3f}")
    elif strategy == "F":
        detail = (f"Strict confluence: {signal.get('confluence', 'not recorded')}\n"
                  f"5-minute trend: {signal.get('trend_5m', 'unknown')} | ALMA: {signal.get('alma_direction', 'unknown')}\n"
                  f"CHOP25: {signal.get('choppiness', 'unknown')} | ADX14: {signal.get('adx14', 0):.2f} | RSI14: {signal.get('rsi14', 0):.2f}")
    else:
        detail = (f"EMA20: {signal.get('ema20', 0):.6f} | EMA50: {signal.get('ema50', 0):.6f}\n"
                  f"ADX14: {signal.get('adx14', 0):.2f} | +DI/-DI: {signal.get('plus_di14', 0):.2f}/{signal.get('minus_di14', 0):.2f}\n"
                  f"RSI14: {signal.get('rsi14', 0):.2f} | CHOP25: {signal.get('chop25', 0):.2f}\n"
                  f"ATR14: {signal.get('atr14', 0):.6f} | Extension: {signal.get('extension_atr', 0):.2f} ATR\n"
                  f"5-minute trend: {signal.get('trend_5m', 'unknown').upper()}")
    telegram_send(
        f"📣 {strategy} — {name} SIGNAL\n{display_asset(signal.get('asset'))} — {direction}\n"
        f"Entry: closed 1-minute candle ({lagos_time(stamp)})\n{detail}\n"
        f"Expiry: {expiry} minute(s)\n"
        f"{'MANUAL SCAN — no trade placed.' if manual else 'AUTO MODE — PRACTICE order only.'}"
    )


def scan_indicator_market(manual, auto_eligible, strategy, generation):
    global SCAN_IN_PROGRESS, ASSET_CURSOR, LAST_ASSET_COUNT, LAST_SCAN_FINISHED, LAST_SCAN_CANDIDATES
    global LAST_SCAN_SCANNED, LAST_SCAN_TOTAL, LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR, LAST_REJECTIONS
    settings = STRATEGY_SETTINGS[strategy]
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
            error_text = LAST_ASSET_DIAGNOSTIC
            return
        start = ASSET_CURSOR % total
        batch = [assets[(start + i) % total] for i in range(min(MAX_SYMBOLS_PER_SCAN, total))]
        logging.info("%s scan: %d of %d FX assets; manual=%s auto_eligible=%s",
                     strategy, len(batch), total, manual, auto_eligible)
        for index, asset in enumerate(batch, 1):
            if not RUNNING or strategy != ACTIVE_STRATEGY or generation != STRATEGY_GENERATION:
                break
            if IQ is None and not ensure_connection():
                timed_out = True
                rejected["connection/reconnect failed"] = rejected.get("connection/reconnect failed", 0) + 1
                break
            attempted += 1
            candles = fetch_candles(asset, timeframe=CANDLE_SECONDS, count=settings["lookback"])
            if candles is None:
                timed_out = True
                timeouts += 1
                rejected["candle timeout"] = rejected.get("candle timeout", 0) + 1
                if timeouts >= 2 or not ensure_connection():
                    break
                continue
            timeouts = 0
            scanned += 1
            if strategy != ACTIVE_STRATEGY or generation != STRATEGY_GENERATION:
                break
            now = int(time.time())
            closed = [c for c in candles
                      if int(c.get("from") or 0) + CANDLE_SECONDS <= now]
            if strategy == "M":
                signal, reason = calculate_momentum_signal(asset, closed, CANDLE_SECONDS)
            elif strategy == "E":
                trend_candles = fetch_candles(asset, timeframe=300, count=100)
                if trend_candles is None:
                    signal, reason = None, "5-minute candle request timed out"
                else:
                    closed_trend = [c for c in trend_candles
                                    if int(c.get("from") or 0) + 300 <= now]
                    signal, reason = calculate_ema_signal(
                        asset, closed, CANDLE_SECONDS, closed_trend
                    )
            else:
                trend_candles = fetch_candles(asset, timeframe=300, count=80)
                if trend_candles is None:
                    signal, reason = None, "5-minute candle request timed out"
                else:
                    closed_trend = [c for c in trend_candles
                                    if int(c.get("from") or 0) + 300 <= now]
                    signal, reason = calculate_fusion_signal(
                        asset, closed, closed_trend, CANDLE_SECONDS
                    )
            if signal is None:
                rejected[reason or "no valid setup"] = rejected.get(reason or "no valid setup", 0) + 1
            else:
                signal.update({
                    "strategy": strategy, "generation": generation,
                    "expiry_minutes": settings["expiry_minutes"],
                    "max_signal_age_seconds": MAX_SIGNAL_AGE_SECONDS,
                })
                stamp = int(signal["candle_time"])
                age = time.time() - stamp
                signal_key = f"{strategy}:{asset}"
                if age > MAX_SIGNAL_AGE_SECONDS or age < -5:
                    rejected["signal candle is stale"] = rejected.get("signal candle is stale", 0) + 1
                elif manual:
                    if MANUAL_SIGNAL_SENT_BY_ASSET.get(signal_key) != str(stamp):
                        results.append(signal)
                        MANUAL_SIGNAL_SENT_BY_ASSET[signal_key] = str(stamp)
                        if len(results) <= TOP_SIGNALS:
                            send_indicator_signal(signal, manual=True)
                    else:
                        rejected["setup already reported"] = rejected.get("setup already reported", 0) + 1
                elif LAST_SIGNAL_BY_ASSET.get(signal_key) != str(stamp):
                    results.append(signal)
                    if (auto_eligible and AUTO_MODE and strategy == ACTIVE_STRATEGY
                            and generation == STRATEGY_GENERATION):
                        EXECUTION_QUEUE.put(signal)
                        logging.info("%s signal queued for immediate PRACTICE execution: %s %s",
                                     strategy, asset, signal["direction"].upper())
                else:
                    rejected["signal already handled"] = rejected.get("signal already handled", 0) + 1
            if index % PROGRESS_EVERY == 0:
                logging.info("%s scan progress %d/%d; candidates=%d",
                             strategy, index, len(batch), len(results))
            time.sleep(REQUEST_PAUSE_SECONDS)
    except Exception as exc:
        error_text = str(exc)
        logging.exception("%s market scan failed", strategy)
    finally:
        if total:
            ASSET_CURSOR = (start + max(attempted, 1)) % total
        LAST_SCAN_FINISHED = time.time()
        LAST_SCAN_CANDIDATES = len(results)
        LAST_SCAN_SCANNED, LAST_SCAN_TOTAL = scanned, total
        LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR = timed_out, error_text
        LAST_REJECTIONS = dict(sorted(rejected.items(), key=lambda item: item[1], reverse=True)[:8])
        logging.info("%s scan finished: scanned=%d/%d candidates=%d timeout=%s error=%s",
                     strategy, scanned, total, len(results), timed_out, error_text or "none")
        if manual and not results and strategy == ACTIVE_STRATEGY and generation == STRATEGY_GENERATION:
            top = sorted(rejected.items(), key=lambda item: item[1], reverse=True)[:3]
            reasons = "; ".join(f"{name}: {count}" for name, count in top) or "No valid closed candles returned."
            suffix = f"\n{error_text}" if error_text else ""
            telegram_send(f"NO TRADE — {strategy}\nScanned: {scanned}/{total}\nTop rejection reasons: {reasons}{suffix}")
        SCAN_RESULTS_QUEUE.put({
            "manual": manual, "auto_eligible": auto_eligible, "results": results,
            "timed_out": timed_out, "error": error_text,
            "strategy": strategy, "generation": generation,
        })
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
        strategy = ACTIVE_STRATEGY
        generation = STRATEGY_GENERATION
    threading.Thread(target=scan_market,
                     kwargs={"manual": manual, "auto_eligible": eligible,
                             "strategy": strategy, "generation": generation},
                     daemon=True, name=f"market-scan-{strategy}").start()
    return True


def execute_signal(signal):
    global ORDER_ATTEMPTS, ORDER_ACCEPTED, ORDER_REJECTED, LAST_ORDER_STATUS, AUTO_SESSION_ACCEPTED, EMA_MARTINGALE_STATUS
    if not AUTO_TRADING_ALLOWED or not AUTO_MODE:
        return False
    if AUTO_SESSION_ACCEPTED >= MAX_TRADES_PER_AUTO_SESSION:
        set_auto(False, "20 accepted trades reached for this START AUTO session")
        telegram_send("🛑 AUTO MODE OFF — session limit reached: 20 accepted PRACTICE trades. Send START AUTO to begin a new session.")
        return False
    if not practice_mode_verified():
        set_auto(False, "PRACTICE mode could not be verified")
        telegram_send("🛑 AUTO STOPPED: PRACTICE mode could not be verified.")
        return False
    strategy = str(signal.get("strategy", "B")).upper()
    generation = int(signal.get("generation", STRATEGY_GENERATION))
    if strategy != ACTIVE_STRATEGY or generation != STRATEGY_GENERATION:
        logging.info("Skipping signal from inactive strategy/generation: %s/%s", strategy, generation)
        return False
    expiry_minutes = int(signal.get("expiry_minutes", STRATEGY_SETTINGS[strategy]["expiry_minutes"]))
    asset = str(signal.get("asset", ""))
    direction = str(signal.get("direction", "")).lower()
    candle_time = int(signal.get("candle_time") or time.time())
    age = time.time() - candle_time
    max_age = int(signal.get("max_signal_age_seconds", MAX_SIGNAL_AGE_SECONDS))
    signal_key = f"{strategy}:{asset}"
    martingale_followup = bool(signal.get("_martingale_followup")) and strategy == "E"
    martingale_step = int(signal.get("_martingale_step", 1)) if strategy == "E" else 1
    order_stake = stake_for_step(martingale_step) if strategy == "E" else STAKE
    if (age > max_age or age < -5) and not martingale_followup:
        logging.info("Skipping stale %s signal %s (age %.1fs)", strategy, asset, age)
        return False
    if not asset or direction not in ("call", "put") or (LAST_SIGNAL_BY_ASSET.get(signal_key) == str(candle_time) and not martingale_followup):
        return False
    with STATE_LOCK:
        allowed, exposure_reason = TRADE_EXPOSURE.can_open(asset, time.time())
        if not allowed:
            logging.info("Skipping %s %s: %s (open trades %d/%d).",
                         asset, direction.upper(), exposure_reason,
                         TRADE_EXPOSURE.count(time.time()), MAX_CONCURRENT_TRADES)
            if martingale_followup:
                EMA_MARTINGALE_STATUS = f"PAUSED — follow-up blocked by exposure guard: {exposure_reason}"
                set_auto(False, "EMA martingale follow-up blocked by exposure guard")
                telegram_send("🛑 EMA MARTINGALE PAUSED — the next same-direction step was blocked by the open-trade safety limit. AUTO is OFF; no new $1 sequence will start.")
            return False
    client = IQ
    if strategy == "E":
        try:
            available_balance = safe_float(client.get_balance(), 0.0)
        except Exception:
            available_balance = 0.0
        if available_balance < required_balance_from_step(martingale_step):
            EMA_MARTINGALE_STATUS = "PAUSED — insufficient balance for remaining sequence"
            set_auto(False, "Insufficient PRACTICE balance for EMA martingale")
            telegram_send("EMA martingale paused: insufficient PRACTICE balance for remaining steps; no order placed.")
            return False
    # Mark this candle before sending the request, so a timed-out request is never retried for the same candle.
    LAST_SIGNAL_BY_ASSET[signal_key] = str(candle_time)
    result_queue = queue.Queue(maxsize=1)
    def worker():
        try:
            if not practice_mode_verified():
                result_queue.put(("safety", "PRACTICE verification failed"))
                return
            with STATE_LOCK:
                if (not AUTO_MODE or strategy != ACTIVE_STRATEGY
                        or generation != STRATEGY_GENERATION):
                    result_queue.put(("cancelled", "strategy changed before order submission"))
                    return
                with client_api_lock(client):
                    response = client.buy(order_stake, asset, direction, expiry_minutes)
            result_queue.put(("result", response))
        except Exception as exc:
            try:
                result_queue.put(("error", str(exc)))
            except queue.Full:
                pass
    ORDER_ATTEMPTS += 1
    LAST_ORDER_STATUS = f"REQUEST SENT: {display_asset(asset)} {direction.upper()}; awaiting broker confirmation"
    logging.info("ORDER REQUEST attempt=%d strategy=%s asset=%s direction=%s stake=%s expiry=%sm",
                 ORDER_ATTEMPTS, strategy, asset, direction.upper(), money(order_stake), expiry_minutes)
    threading.Thread(target=worker, daemon=True, name="place-practice-order").start()
    try:
        status, response = result_queue.get(timeout=ORDER_REQUEST_TIMEOUT)
    except queue.Empty:
        ORDER_REJECTED += 1
        LAST_ORDER_STATUS = f"TIMEOUT: {display_asset(asset)} {direction.upper()}; broker acceptance unknown"
        set_auto(False, "Order request timed out; no retry made")
        telegram_send(f"⚠️ AUTO STOPPED — ORDER RESPONSE TIMED OUT\n{display_asset(asset)} {direction.upper()}\nNo retry was made for this candle. Check the practice account before restarting.")
        return False
    if status == "cancelled":
        logging.info("Order safely cancelled before broker submission: %s", response)
        return False
    if status == "safety":
        ORDER_REJECTED += 1
        LAST_ORDER_STATUS = f"BLOCKED: {display_asset(asset)} {direction.upper()} — PRACTICE verification failed"
        set_auto(False, response)
        telegram_send("🛑 AUTO STOPPED: " + response)
        return False
    if status != "result" or not isinstance(response, (tuple, list)) or len(response) < 2:
        detail = str(response)
        ORDER_REJECTED += 1
        LAST_ORDER_STATUS = f"REQUEST ERROR: {display_asset(asset)} {direction.upper()} — {detail[:140]}"
        logging.warning("Order request failed for %s: %r", asset, response)
        telegram_send(f"⚠️ ORDER NOT CONFIRMED\n{display_asset(asset)} {direction.upper()}\nNo trade was confirmed.\nExecution error: {detail[:220]}")
        if martingale_followup:
            EMA_MARTINGALE_STATUS = "PAUSED — martingale follow-up order was not accepted"
            set_auto(False, "EMA martingale follow-up was not accepted")
            telegram_send("🛑 EMA MARTINGALE PAUSED — follow-up order was not accepted. AUTO is OFF; no fresh sequence will be started automatically.")
        if "insufficient funds" in detail.lower():
            set_auto(False, "Broker rejected order: insufficient funds")
            telegram_send("🛑 AUTO MODE OFF — IQ Option reported insufficient funds. No more orders will be attempted. Check the PRACTICE balance before manually restarting.")
        return False
    success, order_id = bool(response[0]), response[1]
    if not success or order_id is None or str(order_id) == "":
        ORDER_REJECTED += 1
        LAST_ORDER_STATUS = f"BROKER REJECTED: {display_asset(asset)} {direction.upper()} — {str(order_id)[:120]}"
        logging.info("Practice order rejected for %s: %r", asset, response)
        detail = str(response[1] if len(response) > 1 else "broker returned no order ID")
        detail = re.sub(r"[\r\n]+", " ", detail)[:220]
        telegram_send(f"⚪ ORDER REJECTED\n{display_asset(asset)} {direction.upper()}\nNo trade was confirmed.\nBroker response: {detail}")
        if martingale_followup:
            EMA_MARTINGALE_STATUS = "PAUSED — martingale follow-up was rejected by broker"
            set_auto(False, "EMA martingale follow-up rejected by broker")
            telegram_send("🛑 EMA MARTINGALE PAUSED — broker rejected the next step. AUTO is OFF; no fresh sequence will be started automatically.")
        if "insufficient funds" in detail.lower():
            set_auto(False, "Broker rejected order: insufficient funds")
            telegram_send("🛑 AUTO MODE OFF — insufficient PRACTICE funds. Resolve the balance issue, then restart AUTO manually.")
        return False
    accepted_at = time.time()
    ORDER_ACCEPTED += 1
    AUTO_SESSION_ACCEPTED += 1
    LAST_ORDER_STATUS = f"ACCEPTED: {display_asset(asset)} {direction.upper()} — order {order_id}"
    with STATE_LOCK:
        TRADE_EXPOSURE.register(order_id, asset, direction, accepted_at,
                                expiry_seconds=expiry_minutes * 60)
    step_note = f" | EMA martingale step {martingale_step}/{MAX_STEPS}" if strategy == "E" else ""
    telegram_send(f"🟡 PRACTICE ORDER ACCEPTED\n{display_asset(asset)} — {direction.upper()}\nOrder ID: {order_id}\n"
                  f"Strategy: {strategy} | Stake: {money(order_stake)}{step_note} | Expiry: {expiry_minutes} minute(s)\n"
                  f"Open exposure: {TRADE_EXPOSURE.count(time.time())}/{MAX_CONCURRENT_TRADES}\n"
                  f"Session trades: {AUTO_SESSION_ACCEPTED}/{MAX_TRADES_PER_AUTO_SESSION}\n"
                  f"EMA outcomes are checked only to manage the requested $1/$2/$4 sequence.")
    if AUTO_SESSION_ACCEPTED >= MAX_TRADES_PER_AUTO_SESSION and strategy != "E":
        set_auto(False, "20 accepted trades reached for this START AUTO session")
        telegram_send("🛑 AUTO MODE OFF — 20 accepted PRACTICE trades completed in this session.")
    logging.info("Practice order accepted for %s; order id=%s; exposure=%d/%d",
                 asset, order_id, TRADE_EXPOSURE.count(time.time()), MAX_CONCURRENT_TRADES)
    if strategy == "E":
        EMA_MARTINGALE_STATUS = f"WAITING — step {martingale_step}/{MAX_STEPS}, stake {money(order_stake)}"
        time.sleep(max(0.0, accepted_at + expiry_minutes * 60 + 2 - time.time()))
        try:
            if not practice_mode_verified():
                raise RuntimeError("PRACTICE account verification failed before EMA result lookup")
            net_profit = get_binary_option_net_profit(client, order_id, lock=client_api_lock(client), timeout=35)
        except Exception as exc:
            EMA_MARTINGALE_STATUS = f"PAUSED — result unverified for order {order_id}"
            LAST_ORDER_STATUS = f"RESULT UNVERIFIED: order {order_id}; no martingale retry"
            set_auto(False, "EMA outcome could not be verified; martingale stopped safely")
            telegram_send(f"🛑 EMA MARTINGALE PAUSED\nOrder {order_id} result could not be verified.\nNo $2/$4 follow-up will be placed. AUTO is OFF.\nReason: {str(exc)[:180]}")
            return True
        outcome = classify_profit(net_profit)
        with STATE_LOCK:
            TRADE_EXPOSURE.complete(order_id)
        LAST_ORDER_STATUS = f"RESULT CONFIRMED: {outcome} on order {order_id} (net {money(net_profit)})"
        next_step = next_step_after_result(outcome, martingale_step)
        if outcome == "LOSS" and next_step is not None:
            if AUTO_SESSION_ACCEPTED >= MAX_TRADES_PER_AUTO_SESSION:
                set_auto(False, "AUTO session trade limit reached before martingale continuation")
            if AUTO_MODE and strategy == ACTIVE_STRATEGY and generation == STRATEGY_GENERATION and practice_mode_verified():
                next_stake = stake_for_step(next_step)
                EMA_MARTINGALE_STATUS = f"LOSS CONFIRMED — placing step {next_step}/{MAX_STEPS} at {money(next_stake)} in the same direction"
                telegram_send(f"🔴 EMA LOSS CONFIRMED\n{display_asset(asset)} — {direction.upper()}\nNext trade: {money(next_stake)} in the SAME direction, without waiting for another signal.\nMartingale step {next_step}/{MAX_STEPS}.")
                followup = dict(signal)
                followup.update({"_martingale_followup": True, "_martingale_step": next_step, "_stake": next_stake})
                return execute_signal(followup)
            EMA_MARTINGALE_STATUS = "STOPPED — AUTO off or safety verification failed; next stake remains $1"
            telegram_send("⚠️ EMA martingale continuation was not placed because AUTO is OFF or PRACTICE verification failed. No trade was forced.")
        elif outcome == "LOSS":
            EMA_MARTINGALE_STATUS = "3/3 LOSSES — sequence ended; next fresh EMA setup starts at $1"
            telegram_send("🛑 EMA MARTINGALE SEQUENCE ENDED\nThe $4 third trade also lost. No $8 trade will be placed. The next fresh EMA signal starts a new $1 sequence.")
        else:
            EMA_MARTINGALE_STATUS = f"{outcome} — sequence reset; next fresh EMA setup starts at $1"
            telegram_send(f"✅ EMA {outcome}\nOrder {order_id} closed with net {money(net_profit)}. Martingale sequence reset. Next fresh EMA setup starts at $1.")
        if AUTO_SESSION_ACCEPTED >= MAX_TRADES_PER_AUTO_SESSION:
            set_auto(False, "20 accepted trades reached for this START AUTO session")
            telegram_send("🛑 AUTO MODE OFF — session limit reached: 20 accepted PRACTICE trades.")
    return True


def set_auto(enabled, reason=""):
    global AUTO_MODE
    AUTO_MODE = bool(enabled)
    logging.info("AUTO mode %s: %s", "ON" if enabled else "OFF", reason or "manual")
    return True


def strategy_label(strategy=None):
    key = str(strategy or ACTIVE_STRATEGY).upper()
    return f"{key} — {STRATEGY_SETTINGS.get(key, {}).get('name', 'Unknown strategy')}"


def strategy_expiry(strategy=None):
    key = str(strategy or ACTIVE_STRATEGY).upper()
    return int(STRATEGY_SETTINGS.get(key, STRATEGY_SETTINGS["B"])["expiry_minutes"])


def persist_active_strategy(strategy):
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/active_strategy.json"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        current = requests.get(url, headers=headers, params={"ref": GITHUB_REF_NAME}, timeout=8)
        payload = {
            "message": f"Select {strategy} strategy",
            "content": base64.b64encode((json.dumps({"strategy": strategy}) + "\n").encode()).decode(),
            "branch": GITHUB_REF_NAME,
        }
        if current.status_code == 200:
            payload["sha"] = current.json().get("sha")
        elif current.status_code != 404:
            current.raise_for_status()
        response = requests.put(url, headers=headers, json=payload, timeout=8)
        response.raise_for_status()
        return True
    except Exception as exc:
        logging.warning("Could not persist active strategy selection: %s", exc)
        return False


def load_active_strategy():
    global ACTIVE_STRATEGY
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/active_strategy.json"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        response = requests.get(url, headers=headers, params={"ref": GITHUB_REF_NAME}, timeout=8)
        if response.status_code == 404:
            return
        response.raise_for_status()
        raw = base64.b64decode(response.json().get("content", "")).decode()
        selected = str(json.loads(raw).get("strategy", "B")).upper()
        if selected in STRATEGY_SETTINGS:
            ACTIVE_STRATEGY = selected
    except Exception as exc:
        logging.warning("Could not load saved strategy selection; defaulting to B: %s", exc)


def select_strategy(strategy):
    global ACTIVE_STRATEGY, STRATEGY_GENERATION, MANUAL_SCAN_REQUESTED
    strategy = str(strategy).upper()
    if strategy not in STRATEGY_SETTINGS:
        return False
    with STATE_LOCK:
        set_auto(False, "Strategy changed; AUTO must be restarted manually")
        ACTIVE_STRATEGY = strategy
        STRATEGY_GENERATION += 1
        MANUAL_SCAN_REQUESTED = False
        while True:
            try:
                EXECUTION_QUEUE.get_nowait()
            except queue.Empty:
                break
    persisted = persist_active_strategy(strategy)
    status = "Saved for future workflow restarts." if persisted else "Selected for this running session."
    telegram_send(f"✅ STRATEGY SELECTED\n{strategy_label(strategy)}\nAUTO MODE: OFF\n{status}\nSend START AUTO when ready. Switching never starts orders automatically.")
    logging.info("Selected strategy %s; generation=%d; persisted=%s",
                 strategy, STRATEGY_GENERATION, persisted)
    return True


def parse_strategy_command(command):
    normalized = command.strip().upper().replace("—", "-")
    aliases = {
        "B": "B", "/B": "B", "STRATEGY B": "B", "/STRATEGY B": "B",
        "B - BREAKOUT": "B", "BREAKOUT": "B", "BREAKOUT & RETEST": "B",
        "M": "M", "/M": "M", "STRATEGY M": "M", "/STRATEGY M": "M",
        "M - MOMENTUM": "M", "MOMENTUM": "M", "MOMENTUM 10": "M",
        "E": "E", "/E": "E", "STRATEGY E": "E", "/STRATEGY E": "E",
        "E - EMA": "E", "EMA": "E", "EMA STRATEGY": "E",
        "F": "F", "/F": "F", "STRATEGY F": "F", "/STRATEGY F": "F",
        "F - FUSION": "F", "FUSION": "F", "PRECISION FUSION X": "F",
    }
    return aliases.get(normalized)


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
    return (f"📊 IQ OPTION SCANNER STATUS\nConnection: {'CONNECTED' if connected else 'DISCONNECTED'}\n"
            f"Practice verification: {'OK' if practice else 'NOT VERIFIED'}\nBalance: {balance}\n"
            f"Selected strategy: {strategy_label()}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
            f"Stake: $1 base | EMA martingale: $1 → $2 → $4 | Expiry: {strategy_expiry()} minute(s)\n"
            f"EMA martingale status: {EMA_MARTINGALE_STATUS}\n"
            f"Market selection: {MARKET_MODE}\n"
            f"Last discovery mode: {LAST_ASSET_DISCOVERY_MODE}\n"
            f"Asset discovery: {LAST_ASSET_DIAGNOSTIC}\n"
            f"Breakout 5-minute trend filter: {'ON' if FIVE_MINUTE_TREND_FILTER_ENABLED else 'OFF'}\n"
            f"Order requests: {ORDER_ATTEMPTS} | Accepted: {ORDER_ACCEPTED} | Rejected/timeouts: {ORDER_REJECTED}\n"
            f"Last order: {LAST_ORDER_STATUS}\n"
            f"Open trades: {TRADE_EXPOSURE.count(time.time())}/{MAX_CONCURRENT_TRADES}\n"
            f"Trades this AUTO session: {AUTO_SESSION_ACCEPTED}/{MAX_TRADES_PER_AUTO_SESSION}\n"

            f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {age}\n"
            f"Last batch: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\nCandidates: {LAST_SCAN_CANDIDATES}\n"
            f"Scan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
            f"Last timeout: {'YES' if LAST_SCAN_TIMED_OUT else 'NO'}\n"
            f"Last error: {LAST_SCAN_ERROR or 'None'}\n"
            f"Top rejection reasons: {'; '.join(f'{k}: {v}' for k,v in LAST_REJECTIONS.items()) or 'No scan data yet'}")


def process_telegram_update(update):
    global MANUAL_SCAN_REQUESTED, MARKET_MODE, AUTO_SESSION_ACCEPTED
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
    selected = parse_strategy_command(command)
    if selected:
        select_strategy(selected)
        return
    if command in ("/START", "START AUTO"):
        if not AUTO_TRADING_ALLOWED:
            telegram_send("AUTO is disabled in this scanner.")
        elif not ensure_connection():
            set_auto(False, "Connection or PRACTICE verification failed")
            telegram_send("AUTO refused: connection or PRACTICE verification failed. Check STATUS; AUTO remains OFF.")
        else:
            if not AUTO_MODE:
                AUTO_SESSION_ACCEPTED = 0
            set_auto(True)
            telegram_send(f"🟢 AUTO MODE ON\nSelected strategy: {strategy_label()}\nPRACTICE verified. {money(STAKE)} stake / {strategy_expiry()}-minute expiry.\nSession limit: {MAX_TRADES_PER_AUTO_SESSION} accepted trades. Max concurrent open trades: {MAX_CONCURRENT_TRADES}; one open trade per asset.\nOnly the selected strategy can submit orders. No trade outcomes are recorded.")
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
        telegram_send("Strategy buttons: B — Breakout & Retest; M — Momentum 10; E — EMA 20/50 + ADX 14 + RSI 14; F — Precision Fusion X strict confluence\nSTART AUTO — enable PRACTICE trades for the selected strategy\nSTOP AUTO — stop new orders\nSCAN MARKET — report-only scan\nREGULAR PAIRS / OTC PAIRS / BOTH MARKETS — choose market\nSTATUS — scanner status\nSwitching strategy always turns AUTO OFF. One strategy can trade at a time. No win/loss history is recorded.")
    else:
        telegram_send("Unknown command. Use B, M, E, F, START AUTO, STOP AUTO, SCAN MARKET, REGULAR PAIRS, OTC PAIRS, BOTH MARKETS, STATUS, or /HELP.")


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
    telegram_send(f"💓 SCANNER HEARTBEAT — {strategy_label()}\nAccount: PRACTICE ONLY\n"
                  f"Connection: {'OK' if practice_mode_verified() else 'NOT VERIFIED'}\n"
                  f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
                  f"Market selection: {MARKET_MODE}\n"
                  f"Discovered assets: {LAST_ASSET_COUNT}\nAsset discovery: {LAST_ASSET_DIAGNOSTIC}\n"
                  f"Last scan: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\nCandidates: {LAST_SCAN_CANDIDATES}\nScan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
                  f"No valid first retest means NO TRADE.")


def main():
    global RUNNING, MANUAL_SCAN_REQUESTED
    load_active_strategy()
    telegram_send(f"🚀 IQ OPTION SCANNER STARTING\nSelected strategy: {strategy_label()}\nPRACTICE-only scanner is connecting. AUTO MODE starts OFF after every GitHub Actions restart; send START AUTO to enable PRACTICE execution.\nChoose B, M, E, or F at any time; switching turns AUTO OFF.\nManual SCAN MARKET sends signals only and never places trades.\nMax open exposure: 3 trades total, 1 per pair. No win/loss history is recorded.")
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
                    fresh_signal = EXECUTION_QUEUE.get_nowait()
                except queue.Empty:
                    break
                if AUTO_MODE:
                    execute_signal(fresh_signal)
            while True:
                try:
                    completed = SCAN_RESULTS_QUEUE.get_nowait()
                except queue.Empty:
                    break
                if (completed.get("strategy", ACTIVE_STRATEGY) != ACTIVE_STRATEGY
                        or completed.get("generation", STRATEGY_GENERATION) != STRATEGY_GENERATION):
                    continue
                completed_results = completed.get("results", [])
                if completed_results and not completed.get("manual") and not completed.get("auto_eligible"):
                    strategy = completed.get("strategy", ACTIVE_STRATEGY)
                    logging.warning("%d valid %s setup(s) found but AUTO_MODE was OFF when this scan started; no order submitted.",
                                    len(completed_results), strategy)
                    telegram_send(f"🔔 {len(completed_results)} VALID {strategy} SETUP(S) FOUND\nAUTO MODE is OFF, so no order was submitted.\nSend START AUTO to enable PRACTICE execution.")
                elif completed.get("auto_eligible") and completed_results:
                    logging.info("%d fresh %s setup(s) were handed to the execution queue during scanning.",
                                 len(completed_results), completed.get("strategy", ACTIVE_STRATEGY))
            if MANUAL_SCAN_REQUESTED and not SCAN_IN_PROGRESS:
                MANUAL_SCAN_REQUESTED = False
                if not start_scan(manual=True):
                    MANUAL_SCAN_REQUESTED = True
            elif (not SCAN_IN_PROGRESS
                  and time.time() - LAST_SCAN_STARTED >= STRATEGY_SETTINGS[ACTIVE_STRATEGY]["scan_interval_seconds"]):
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
