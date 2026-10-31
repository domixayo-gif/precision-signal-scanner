import os, time, math, logging, threading, traceback, requests, queue, re, json
from datetime import datetime, timezone
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

# ALMA + CHOP — PRACTICE ONLY. Manual scans never place trades.
BOT_NAME = "ALMA + Choppiness IQ Option Scanner"
AUTO_TRADING_ALLOWED = True
STAKE, EXPIRY_MINUTES, CANDLE_SECONDS, LOOKBACK_CANDLES = 1.0, 1, 60, 50
# TradingView-style ALMA settings; standard offset/sigma used because the video did not specify them.
ALMA_FAST, ALMA_SLOW, ALMA_OFFSET, ALMA_SIGMA = 7, 12, 0.85, 6.0
CHOP_PERIOD, CHOP_MAX = 2, 59.8
CHOP_LOWER_BAND, CHOP_MIDDLE_BAND, CHOP_UPPER_BAND = 38.2, 50.0, 61.8
SCAN_INTERVAL_SECONDS, REQUEST_PAUSE_SECONDS, RECONNECT_SECONDS = 60, 0.12, 30
HEARTBEAT_SECONDS, REQUEST_TIMEOUT, ASSET_DISCOVERY_TIMEOUT = 300, 15, 40
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
MARKET_MODE_OVERRIDE = "REGULAR"  # Manual Telegram market selection; defaults to regular FX.
ACTIVE_MARKET_MODE = ""
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
LAST_DISCOVERY_DIAGNOSTICS = "Not collected yet"
# Broker order names may include a suffix such as EURNZD-op; candles use the normalized FX base.
ORDER_ASSET_BY_BASE = {}


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
            [{"text":"USE REGULAR FX"},{"text":"USE OTC FX"}]], "resize_keyboard": True}
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
    telegram_send(f"🟢 ALMA + CHOP CONNECTED\nAccount: PRACTICE ONLY\nBalance: {money(balance)}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nExpiry: 1 minute | Stake: $1\nManual scans never place trades.")

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


def regular_fx_base_name(asset):
    """Return the six-letter FX base for known IQ Option naming variants, else empty."""
    raw = str(asset or "").strip().upper().replace("/", "").replace(" ", "")
    # Explicitly reject OTC and HC before removing any known platform prefixes/suffixes.
    if "OTC" in raw or re.search(r"(?:^|[-_])HC(?:$|[-_])", raw):
        return ""
    if "." in raw:
        raw = raw.rsplit(".", 1)[1]
    # IQ Option's binary/turbo init feed can label a regular FX active as EURNZD-op.
    # Keep that exact active name for candle requests, but validate its FX base.
    if raw.endswith("-OP"):
        raw = raw[:-3]
    name = raw.replace("-", "").replace("_", "")
    if not re.fullmatch(r"[A-Z]{6}", name):
        return ""
    currencies = {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD",
        "HKD", "NOK", "SEK", "MXN", "TRY", "ZAR", "PLN", "CZK", "HUF",
        "DKK", "CNH", "THB", "INR", "BRL", "IDR", "MYR", "PHP", "NGN",
        "AED", "SAR"
    }
    return name if name[:3] in currencies and name[3:] in currencies and name[:3] != name[3:] else ""


def otc_fx_asset_name(asset):
    """Return a normalized six-letter OTC FX symbol, e.g. CADCHF-OTC; reject stocks/HC."""
    raw = str(asset or "").strip().upper().replace("/", "").replace(" ", "")
    if re.search(r"(?:^|[-_])HC(?:$|[-_])", raw):
        return ""
    if "." in raw:
        raw = raw.rsplit(".", 1)[1]
    # Some feeds append -op after the market suffix; accept either ordering.
    if raw.endswith("-OP"):
        raw = raw[:-3]
    if not raw.endswith("-OTC"):
        return ""
    base = raw[:-4].replace("-", "").replace("_", "")
    if not re.fullmatch(r"[A-Z]{6}", base):
        return ""
    currencies = {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD",
        "HKD", "NOK", "SEK", "MXN", "TRY", "ZAR", "PLN", "CZK", "HUF",
        "DKK", "CNH", "THB", "INR", "BRL", "IDR", "MYR", "PHP", "NGN",
        "AED", "SAR"
    }
    if base[:3] not in currencies or base[3:] not in currencies or base[:3] == base[3:]:
        return ""
    return base + "-OTC"


def current_market_mode():
    """Return the user-selected market; never switch automatically by time."""
    return MARKET_MODE_OVERRIDE or "REGULAR"


def set_market_mode(mode):
    """Manually select regular FX or OTC FX for subsequent scans."""
    global MARKET_MODE_OVERRIDE, ACTIVE_MARKET_MODE, ASSET_CURSOR
    if mode not in ("REGULAR", "OTC"):
        return False
    MARKET_MODE_OVERRIDE = mode
    ACTIVE_MARKET_MODE = mode
    ASSET_CURSOR = 0
    return True


def market_asset_name(asset, mode=None):
    selected = mode or current_market_mode()
    return regular_fx_base_name(asset) if selected == "REGULAR" else otc_fx_asset_name(asset)


def is_supported_binary_asset(asset):
    return bool(market_asset_name(asset))

def get_asset_names(market_mode=None):
    """Discover only the FX market manually selected by the user."""
    global IQ, LAST_DISCOVERY_DIAGNOSTICS, ORDER_ASSET_BY_BASE, ACTIVE_MARKET_MODE
    market_mode = market_mode or current_market_mode()
    ACTIVE_MARKET_MODE = market_mode
    client = IQ
    if client is None:
        return []
    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            with client_api_lock(client):
                # Prefer the v2 initialization feed; some IQ Option sessions populate
                # this feed while the legacy init-all response is empty or delayed.
                payload = None
                v2_method = getattr(client, "get_all_init_v2", None)
                if callable(v2_method):
                    try:
                        payload = v2_method()
                    except Exception as exc:
                        logging.warning("get_all_init_v2() discovery failed: %s", exc)
                if not payload:
                    payload = client.get_all_init()
                result_queue.put(("ok", payload))
        except Exception as exc:
            result_queue.put(("error", str(exc)))

    threading.Thread(target=worker, daemon=True, name="asset-discovery").start()
    try:
        status, data = result_queue.get(timeout=ASSET_DISCOVERY_TIMEOUT)
    except queue.Empty:
        LAST_DISCOVERY_DIAGNOSTICS = f"Init feed did not return within {ASSET_DISCOVERY_TIMEOUT}s (v2/legacy call stalled)."
        logging.error("Binary/turbo asset discovery exceeded %ss; IQ Option init feed may be stalled.", ASSET_DISCOVERY_TIMEOUT)
        if IQ is client:
            IQ = None
        return []
    if status != "ok":
        LAST_DISCOVERY_DIAGNOSTICS = f"Init feed raised: {str(data)[:180]}"
        logging.warning("IQ Option init-feed asset discovery failed: %s", data)
        return []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            LAST_DISCOVERY_DIAGNOSTICS = f"Unexpected payload type: {type(data).__name__}"
            logging.warning("Unexpected IQ Option init payload type: %s", type(data).__name__)
            return []

        # API/library versions differ in where the binary/turbo active tables live.
        # Search only keys explicitly named 'actives'; never treat arbitrary instruments as tradable.
        active_tables = []
        visited = set()

        def walk(node, path="root"):
            if isinstance(node, dict):
                identity = id(node)
                if identity in visited:
                    return
                visited.add(identity)
                for key, value in node.items():
                    if str(key).lower() == "actives":
                        if isinstance(value, dict):
                            # Keep active ID keys so orders use the live broker mapping.
                            active_tables.append((path + ".actives", list(value.items())))
                        elif isinstance(value, list):
                            active_tables.append((path + ".actives", [(None, item) for item in value]))
                    if isinstance(value, (dict, list)):
                        walk(value, path + "." + str(key))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    if isinstance(value, (dict, list)):
                        walk(value, f"{path}[{index}]")

        walk(data)
        total_actives = 0
        candidates = set()
        order_asset_by_base = {}
        order_asset_priority = {}
        active_id_priority = {}
        raw_fx_seen = 0
        disabled_fx_seen = 0
        suspended_fx_seen = 0
        raw_fx_samples = []
        for table_path, entries in active_tables:
            # Regular and OTC FX may appear in binary, turbo, or blitz market tables.
            # Search only these explicit market tables; no arbitrary instruments.
            market_path = table_path.lower()
            if not any(market in market_path for market in ("binary", "turbo", "blitz")):
                continue
            source_priority = 2 if any(market in market_path for market in ("binary", "turbo")) else 1
            for active_key, active in entries:
                if not isinstance(active, dict):
                    continue
                total_actives += 1
                enabled = active.get("enabled")
                suspended = active.get("is_suspended", active.get("suspended"))
                enabled_text = str(enabled).strip().lower()
                suspended_text = str(suspended).strip().lower()
                raw_name = str(active.get("name", "")).strip()
                # Count scheduled FX names before availability flags for diagnostics.
                raw_normalized_name = market_asset_name(raw_name, market_mode)
                if raw_normalized_name:
                    raw_fx_seen += 1
                    if enabled_text in ("false", "0", "no"):
                        disabled_fx_seen += 1
                    if suspended_text in ("true", "1", "yes"):
                        suspended_fx_seen += 1
                    if len(raw_fx_samples) < 8:
                        raw_fx_samples.append({"name": raw_name, "enabled": enabled, "suspended": suspended})
                # Do not submit orders for assets the broker explicitly marks disabled or suspended.
                if enabled_text in ("false", "0", "no") or suspended_text in ("true", "1", "yes"):
                    continue
                # Accept only the manually selected FX market; candles use canonical symbols
                # while orders retain the exact broker-provided active name.
                normalized_name = market_asset_name(raw_name, market_mode)
                if normalized_name:
                    candidates.add(normalized_name)
                    # stable_api.buy() resolves the exact active key in OP_code.ACTIVES.
                    # Keep the broker-provided OTC name for orders and candles.
                    broker_name = raw_name.rsplit(".", 1)[-1].strip()
                    previous = order_asset_by_base.get(normalized_name, "")
                    if broker_name and (not previous or source_priority >= order_asset_priority.get(normalized_name, 0)):
                        order_asset_by_base[normalized_name] = broker_name
                        order_asset_priority[normalized_name] = source_priority
                    # Refresh the order-name -> active-ID lookup from this live feed.
                    # The previous code discarded dictionary keys and could leave a stale ID,
                    # leading to repeated "asset is not available" order rejections.
                    active_id = active_key
                    if active_id is None:
                        active_id = active.get("id", active.get("active_id"))
                    try:
                        if broker_name and active_id is not None and source_priority >= active_id_priority.get(broker_name.upper(), 0):
                            OP_code.ACTIVES[broker_name] = int(active_id)
                            active_id_priority[broker_name.upper()] = source_priority
                    except (TypeError, ValueError):
                        logging.debug("Could not map active ID for %s: %r", broker_name, active_id)

        # If the expected tables are missing, record payload structure for the next diagnosis.
        if not active_tables or total_actives == 0:
            logging.warning(
                "No binary/turbo active entries found. init payload type=%s root_keys=%s active_table_paths=%s",
                type(data).__name__, list(data.keys())[:30],
                [path for path, _ in active_tables][:20]
            )
        supported = sorted(candidates)
        ORDER_ASSET_BY_BASE = dict(order_asset_by_base)
        table_paths = [path for path, _ in active_tables][:12]
        sample_names = []
        for table_path, entries in active_tables:
            if any(market in table_path.lower() for market in ("binary", "turbo", "blitz")):
                for active_key, active in entries:
                    if isinstance(active, dict):
                        sample_names.append({"name": active.get("name"), "active_id": active_key if active_key is not None else active.get("id", active.get("active_id")), "enabled": active.get("enabled"), "suspended": active.get("is_suspended", active.get("suspended"))})
                        if len(sample_names) >= 6:
                            break
            if len(sample_names) >= 6:
                break
        LAST_DISCOVERY_DIAGNOSTICS = (
            f"payload={type(data).__name__}; root_keys={list(data.keys())[:15]}; "
            f"active_paths={table_paths}; market_entries={total_actives}; "
            f"market_mode={market_mode}; FX_candidates={len(supported)}; raw_FX_names={raw_fx_seen}; "
            f"disabled_FX={disabled_fx_seen}; suspended_FX={suspended_fx_seen}; "
            f"raw_FX_samples={raw_fx_samples}; sample={sample_names}"
        )[:1200]
        logging.info("Asset discovery diagnostic: %s", LAST_DISCOVERY_DIAGNOSTICS)
        logging.info(
            "Asset discovery: active_tables=%d binary/turbo/blitz entries=%d FX_candidates=%d",
            len(active_tables), total_actives, len(supported)
        )
        return supported
    except Exception as exc:
        LAST_DISCOVERY_DIAGNOSTICS = f"Parser error: {type(data).__name__}: {str(exc)[:180]}"
        logging.warning("Could not parse IQ Option active list (%s): %s", type(data).__name__, exc)
        return []


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


def alma_series(values, length, offset=ALMA_OFFSET, sigma=ALMA_SIGMA):
    """Arnaud Legoux Moving Average with TradingView's common defaults."""
    if len(values) < length:
        return None
    m = offset * (length - 1)
    s = length / sigma
    weights = [math.exp(-((i - m) ** 2) / (2 * s * s)) for i in range(length)]
    total = sum(weights)
    if total <= 0:
        return None
    return sum(values[-length + i] * weights[i] for i in range(length)) / total


def choppiness_index(candles, period=CHOP_PERIOD):
    """Standard Choppiness Index; returns a 0..100 value or None if undefined."""
    if len(candles) < period + 1:
        return None
    recent = candles[-period:]
    true_ranges = []
    for i, candle in enumerate(recent):
        if len(candles) - period + i == 0:
            previous_close = candle["close"]
        else:
            previous_close = candles[len(candles) - period + i - 1]["close"]
        true_ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close)
        ))
    total_tr = sum(true_ranges)
    highest = max(c["high"] for c in recent)
    lowest = min(c["low"] for c in recent)
    span = highest - lowest
    if total_tr <= 0 or span <= 0:
        return None
    value = 100.0 * math.log10(total_tr / span) / math.log10(period)
    return max(0.0, min(100.0, value))


def calculate_signal(asset, candles):
    # A crossover requires the fast/slow ALMA relationship on two closed candles.
    if len(candles) < ALMA_SLOW + 2:
        return None, "insufficient candles for ALMA 12 / crossover"
    closes = [c["close"] for c in candles]
    fast_now = alma_series(closes, ALMA_FAST)
    slow_now = alma_series(closes, ALMA_SLOW)
    fast_prev = alma_series(closes[:-1], ALMA_FAST)
    slow_prev = alma_series(closes[:-1], ALMA_SLOW)
    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return None, "ALMA unavailable"
    chop = choppiness_index(candles, CHOP_PERIOD)
    if chop is None:
        return None, "CHOP unavailable (flat range)"
    if chop >= CHOP_MAX:
        return None, f"CHOP too high ({chop:.1f} >= {CHOP_MAX:.1f}); ranging market"
    crossed_up = fast_prev <= slow_prev and fast_now > slow_now
    crossed_down = fast_prev >= slow_prev and fast_now < slow_now
    if crossed_up:
        direction = "call"
    elif crossed_down:
        direction = "put"
    else:
        return None, "no fresh ALMA 7/12 crossover"
    candle = candles[-1]
    try:
        candle_time = int(candle.get("from") or time.time())
    except Exception:
        candle_time = int(time.time())
    # This is a setup-quality ranking, not a probability of winning.
    score = int(round(100.0 - chop))
    return {
        "asset": asset,
        "direction": direction,
        "score": score,
        "alma_fast": fast_now,
        "alma_slow": slow_now,
        "chop": chop,
        "price": closes[-1],
        "candle_time": candle_time,
        "time": utc_now().strftime("%H:%M:%S UTC")
    }, ""

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
    base = re.sub(r"-op$", "", base, flags=re.IGNORECASE)
    base = base.rsplit(".", 1)[-1]
    if len(base) == 6 and base.isalpha():
        base = base[:3].upper() + "/" + base[3:].upper()
    else:
        base = base.replace("_", " ").replace("-", " ").strip()
    return base + (" OTC" if otc else "")


def send_signal(signal, manual=False):
    direction = "CALL 🟢" if signal["direction"] == "call" else "PUT 🔴"
    entry = int(signal.get("entry_time", signal.get("candle_time", time.time())))
    sid = f"AC-{signal.get('asset','UNKNOWN')}-{signal['direction'].upper()}-{int(signal.get('candle_time',time.time()))}"
    telegram_send(
        f"📣 ALMA + CHOP SIGNAL\n{display_asset(signal.get('asset'))} — {direction}\n"
        f"Signal ID: {sid}\nEntry: {lagos_time(entry)}\nExpiry: 1 minute | Stake: $1\n"
        f"ALMA 7: {signal.get('alma_fast', 0):.6f} | ALMA 12: {signal.get('alma_slow', 0):.6f}\n"
        f"CHOP(2): {signal.get('chop', 0):.1f} | filter: below {CHOP_MAX:.1f}\n"
        f"Setup ranking: {signal.get('score', 0)}/100 (not win probability)\n"
        f"{'MANUAL SCAN — no trade placed.' if manual else 'AUTO MODE — practice order checked before placement.'}"
    )

def scan_market(manual=False, auto_eligible=False):
    global SCAN_IN_PROGRESS, ASSET_CURSOR, LAST_ASSET_COUNT, LAST_SCAN_FINISHED
    global LAST_SCAN_CANDIDATES, LAST_SCAN_SCANNED, LAST_SCAN_TOTAL, LAST_SCAN_TIMED_OUT
    global LAST_SCAN_ERROR, LAST_REJECTIONS
    scan_market_mode = current_market_mode()
    results, rejected = [], {}
    scanned = attempted = total = 0
    timed_out, error_text = False, ""
    start = ASSET_CURSOR
    timeouts = 0
    try:
        if not ensure_connection():
            error_text = "IQ Option disconnected; reconnect failed."
            return
        assets = get_asset_names(scan_market_mode)
        total = len(assets)
        LAST_ASSET_COUNT = total
        if not assets:
            timed_out = IQ is None
            error_text = "Asset discovery timed out or IQ Option returned zero symbols."
            logging.error("Zero supported %s FX assets. %s", ACTIVE_MARKET_MODE, LAST_DISCOVERY_DIAGNOSTICS)
            telegram_send(f"⚠️ {ACTIVE_MARKET_MODE} FX ASSET DISCOVERY DIAGNOSTIC\nNo available assets found for the manually selected market.\n{LAST_DISCOVERY_DIAGNOSTICS}\nNo trade placed.", keyboard=False)
            return
        start = ASSET_CURSOR % total
        batch = [assets[(start+i) % total] for i in range(min(MAX_SYMBOLS_PER_SCAN, total))]
        logging.info("Scanning %s FX: %d of %d discovered assets; manual=%s auto_eligible=%s", ACTIVE_MARKET_MODE, len(batch), total, manual, auto_eligible)
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
            if len(candles) < ALMA_SLOW + 3:
                rejected["no candles/unavailable"] = rejected.get("no candles/unavailable", 0) + 1
            else:
                # Use closed candles for both manual alerts and AUTO to avoid repainting.
                closed_candles = candles[:-1]
                signal, reason = calculate_signal(asset, closed_candles)
                if signal:
                    if LAST_SIGNAL_BY_ASSET.get(asset) != str(signal["candle_time"]):
                        signal["entry_time"] = int(signal.get("candle_time", time.time())) + CANDLE_SECONDS
                        seconds_to_entry = signal["entry_time"] - time.time()
                        if -10 <= seconds_to_entry <= 5:
                            results.append(signal)
                            if manual:
                                LAST_SIGNAL_BY_ASSET[asset] = str(signal["candle_time"])
                                if len(results) <= TOP_SIGNALS:
                                    send_signal(signal, manual=True)
                        else:
                            rejected["closed-candle setup outside entry window"] = rejected.get("closed-candle setup outside entry window", 0) + 1
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
        SCAN_RESULTS_QUEUE.put({"manual":manual, "auto_eligible":auto_eligible, "market_mode":scan_market_mode, "results":results,
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
    if ACTIVE_MARKET_MODE and ACTIVE_MARKET_MODE != current_market_mode():
        logging.info("Skipping signal because the Lagos market session changed.")
        return False
    if not practice_mode_verified():
        set_auto(False, "PRACTICE mode could not be verified")
        telegram_send("🛑 AUTO STOPPED: PRACTICE mode could not be verified.")
        return False
    asset = str(signal.get("asset", ""))
    direction = str(signal.get("direction", "")).lower()
    # Candles use canonical FX names; the IQ Option order API needs its exact active key.
    order_asset = ORDER_ASSET_BY_BASE.get(asset)
    if not order_asset:
        logging.warning("Skipping %s: no broker active identifier was captured for this FX pair.", asset)
        telegram_send(f"⚠️ ORDER SKIPPED\n{display_asset(asset)} {direction.upper()}\nNo exact broker active identifier was found; no order was sent.")
        return False
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
                response = client.buy(STAKE, order_asset, direction, EXPIRY_MINUTES)
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
        detail = str(response[1] if len(response) > 1 else "broker returned no order ID")
        detail = re.sub(r"[\\r\n]+", " ", detail)[:220]
        telegram_send(f"⚪ ORDER REJECTED\n{display_asset(asset)} {direction.upper()}\nNo trade was confirmed.\nBroker response: {detail}")
        return False
    telegram_send(f"🟡 PRACTICE ORDER ACCEPTED\n{display_asset(asset)} — {direction.upper()}\nOrder ID: {order_id}\nStake: $1 | Expiry: 1 minute\nThis bot does not record wins or losses.")
    logging.info("Practice order accepted for %s (broker active %s); order id=%s", asset, order_asset, order_id)
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
    return (f"📊 ALMA + CHOP STATUS\nMarket: {current_market_mode()} FX (manual switch)\nConnection: {'CONNECTED' if connected else 'DISCONNECTED'}\n"
            f"Practice verification: {'OK' if practice else 'NOT VERIFIED'}\nBalance: {balance}\n"
            f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nStake: $1 | Expiry: 1 minute\n"
            f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {age}\n"
            f"Last batch: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\nCandidates: {LAST_SCAN_CANDIDATES}\n"
            f"Scan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
            f"Last timeout: {'YES' if LAST_SCAN_TIMED_OUT else 'NO'}\n"
            f"Last error: {LAST_SCAN_ERROR or 'None'}\n"
            f"Asset discovery details: {LAST_DISCOVERY_DIAGNOSTICS}\n"
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
    if command in ("USE REGULAR FX", "/REGULAR", "REGULAR FX"):
        set_market_mode("REGULAR")
        telegram_send("🔁 MARKET SWITCHED: REGULAR FX\nNext scan uses normal currency pairs only. Strategy and filters unchanged.")
    elif command in ("USE OTC FX", "/OTC", "OTC FX"):
        set_market_mode("OTC")
        telegram_send("🔁 MARKET SWITCHED: OTC FX\nNext scan uses OTC currency pairs only. Strategy and filters unchanged.")
    elif command in ("/START", "START AUTO"):
        if not AUTO_TRADING_ALLOWED:
            telegram_send("AUTO is disabled in this scanner.")
        elif not ensure_connection():
            set_auto(False, "Connection or PRACTICE verification failed")
            telegram_send("AUTO refused: connection or PRACTICE verification failed. Check STATUS; AUTO remains OFF.")
        else:
            set_auto(True)
            telegram_send(f"🟢 AUTO MODE ON\nSelected market: {current_market_mode()} FX (manual switch).\nPRACTICE verified. $1 stake / 1-minute expiry.\nSCAN MARKET remains report-only.\nWins/losses and trade history are not recorded.")
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
        telegram_send("Commands:\nUSE REGULAR FX — normal currency pairs\nUSE OTC FX — OTC currency pairs\nSTART AUTO — enable $1 PRACTICE trades\nSTOP AUTO — stop new automatic trades\nSCAN MARKET — report-only signal scan\nSTATUS — connection and scan status\nNo win/loss tracker or trade-history recorder is included.")
    else:
        telegram_send("Unknown command. Use USE REGULAR FX, USE OTC FX, START AUTO, STOP AUTO, SCAN MARKET, STATUS, or /HELP.")


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
    telegram_send(f"💓 ALMA + CHOP HEARTBEAT\nMarket: {current_market_mode()} FX (manual switch)\nAccount: PRACTICE ONLY\n"
                  f"Connection: {'OK' if practice_mode_verified() else 'NOT VERIFIED'}\n"
                  f"AUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\n"
                  f"Discovered assets: {LAST_ASSET_COUNT}\nLast scan: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\n"
                  f"Candidates: {LAST_SCAN_CANDIDATES}\nScan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\n"
                  f"No qualifying setup means NO TRADE.")


def main():
    global RUNNING, MANUAL_SCAN_REQUESTED
    telegram_send(f"🚀 ALMA + CHOP STARTING\nPRACTICE-only scanner is connecting.\nSelected market: {current_market_mode()} FX (manual switch).\nUse USE REGULAR FX or USE OTC FX to switch markets. Manual SCAN MARKET sends signals only and never places trades.\nNo win/loss tracking or trade-history recording.")
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
                    if completed.get("market_mode") != current_market_mode():
                        logging.info("Discarding scan results from the previous market session.")
                    elif completed.get("timed_out"):
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
