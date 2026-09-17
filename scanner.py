"""
===========================================================
PRECISION SIGNAL SCANNER V3.8
===========================================================

PURPOSE
-------
Research/testing scanner for short-term market setups.

V3.8 CHANGES
------------
1. Tests official Bybit mainnet endpoint:
       https://api.bybit.com

2. Falls back to official alternate endpoint:
       https://api.bytick.com

3. Remembers the working endpoint during the run.

4. Gives detailed diagnostics when both endpoints fail.

5. Never creates a signal without real candle data.

6. Keeps 5M trend + 1M entry framework.

7. Keeps Telegram alerts and tracker.

8. Keeps 5-minute per-asset signal lock.

9. Deduplicates the same candle/setup.

10. Supports GitHub Actions one-shot mode:
       python scanner.py --once

11. Optional continuous mode:
       python scanner.py --loop

IMPORTANT
---------
This is a research/testing scanner.
It does NOT guarantee profitable trades or win rate.
A score is setup quality, NOT probability of winning.

OTC remains disabled until a legitimate OTC candle feed
is connected. Normal Bybit data is never substituted for OTC.
===========================================================
"""

import os
import sys
import json
import time
import math
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests


# =========================================================
# VERSION / CONFIG
# =========================================================

VERSION = "V3.8"

MAIN_TIMEFRAME = "5"
ENTRY_TIMEFRAME = "1"

REFERENCE_EXPIRY_MINUTES = 5

MIN_SCORE = 85
BORDERLINE_SCORE = 80

MIN_DOMINANCE = 3
MIN_ADX = 18
STRONG_ADX = 22

MIN_ROOM_SCORE = 3
MIN_EXTENSION_SCORE = 3

MIN_CANDLE_STRENGTH = 0.50

CALL_RSI_MIN = 43
CALL_RSI_MAX = 68

PUT_RSI_MIN = 32
PUT_RSI_MAX = 57

SIGNAL_LOCK_SECONDS = 300

REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 3

CANDLE_LIMIT = 220

SCAN_INTERVAL_SECONDS = 300

TELEGRAM_LIMIT = 3900

MAX_TRACKER_ITEMS = 1000

HEARTBEAT_EVERY_SCANS = 15


# =========================================================
# OFFICIAL BYBIT ENDPOINTS
# =========================================================

BYBIT_ENDPOINTS = [
    "https://api.bybit.com",
    "https://api.bytick.com",
]

BYBIT_KLINE_PATH = "/v5/market/kline"

# The first endpoint that successfully returns usable data
# is remembered for the rest of the current process.
working_bybit_endpoint: Optional[str] = None


# =========================================================
# MARKET SYMBOLS
# =========================================================

NORMAL_SYMBOLS = {
    "EURUSD": "EURUSDUSDT",
    "GBPUSD": "GBPUSDUSDT",
    "USDJPY": "USDJPYUSDT",
}

# OTC deliberately disabled.
OTC_ENABLED = False

OTC_SYMBOLS = {
    "EURUSD_OTC": None,
    "GBPUSD_OTC": None,
    "USDJPY_OTC": None,
}


# =========================================================
# ENVIRONMENT
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()

GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

TRACKER_FILE = "tracker.json"

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 "
            "(X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "Chrome/120 Safari/537.36"
        ),
        "Accept": "application/json",
    }
)


# =========================================================
# TIME HELPERS
# =========================================================

def unix_now() -> int:
    return int(time.time())


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_time(ts: Optional[int]) -> str:
    if not ts:
        return "N/A"

    try:
        return datetime.fromtimestamp(
            int(ts),
            tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "N/A"


# =========================================================
# GENERIC HELPERS
# =========================================================

def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def direction(value: float) -> str:
    if value > 0:
        return "BULLISH"

    if value < 0:
        return "BEARISH"

    return "NEUTRAL"


def opposite(signal: str) -> str:
    if signal == "CALL":
        return "PUT"

    if signal == "PUT":
        return "CALL"

    return "NO TRADE"


# =========================================================
# TRACKER
# =========================================================

DEFAULT_TRACKER = {
    "version": VERSION,
    "signals": [],
    "offset": 0,
    "meta": {
        "last_scan": None,
        "last_signal": None,
        "last_heartbeat": None,
        "signal_locks": {},
        "alerted_keys": [],
        "delivery_failed_keys": [],
        "bybit_endpoint": None,
        "bybit_endpoint_tested": [],
    },
}


def load_local_tracker() -> Dict[str, Any]:
    if not os.path.exists(TRACKER_FILE):
        return json.loads(json.dumps(DEFAULT_TRACKER))

    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("Tracker is not a JSON object")

        data.setdefault("version", VERSION)
        data.setdefault("signals", [])
        data.setdefault("offset", 0)
        data.setdefault("meta", {})

        meta = data["meta"]

        for key, value in DEFAULT_TRACKER["meta"].items():
            meta.setdefault(key, value)

        return data

    except Exception as exc:
        print(f"[TRACKER] Local load error: {exc}")

        return json.loads(json.dumps(DEFAULT_TRACKER))


tracker = load_local_tracker()


def save_local_tracker() -> None:
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(
                tracker,
                f,
                indent=2,
                ensure_ascii=False,
            )
    except Exception as exc:
        print(f"[TRACKER] Local save error: {exc}")


def save_tracker_to_github() -> bool:
    """
    Persists tracker.json using the GitHub Contents API.

    Requires:
      GITHUB_TOKEN
      GITHUB_REPOSITORY

    The workflow must have:
      permissions:
        contents: write
    """

    if not GITHUB_ACTIONS:
        return True

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[GITHUB] Missing GITHUB_TOKEN or GITHUB_REPOSITORY")
        return False

    try:
        import base64

        with open(TRACKER_FILE, "rb") as f:
            raw = f.read()

        encoded = base64.b64encode(raw).decode("utf-8")

        url = (
            "https://api.github.com/repos/"
            f"{GITHUB_REPOSITORY}/contents/{TRACKER_FILE}"
        )

        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        sha = None

        get_response = SESSION.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        if get_response.status_code == 200:
            sha = get_response.json().get("sha")

        payload = {
            "message": f"Update {TRACKER_FILE} - {VERSION}",
            "content": encoded,
        }

        if sha:
            payload["sha"] = sha

        response = SESSION.put(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code in (200, 201):
            print("[GITHUB] tracker.json saved")
            return True

        print(
            "[GITHUB] tracker save failed:",
            response.status_code,
            response.text[:500],
        )

        return False

    except Exception as exc:
        print(f"[GITHUB] Tracker save exception: {exc}")
        return False


def persist_tracker() -> None:
    save_local_tracker()
    save_tracker_to_github()


def trim_tracker() -> None:
    signals = tracker.get("signals", [])

    if len(signals) > MAX_TRACKER_ITEMS:
        tracker["signals"] = signals[-MAX_TRACKER_ITEMS:]

    for key in (
        "alerted_keys",
        "delivery_failed_keys",
    ):
        values = tracker["meta"].get(key, [])

        if len(values) > MAX_TRACKER_ITEMS:
            tracker["meta"][key] = values[-MAX_TRACKER_ITEMS:]


# =========================================================
# TELEGRAM
# =========================================================

def telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def telegram_configured() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def telegram_send(text: str) -> bool:
    if not telegram_configured():
        print("[TELEGRAM] Not configured")
        return False

    chunks = [
        text[i:i + TELEGRAM_LIMIT]
        for i in range(0, len(text), TELEGRAM_LIMIT)
    ]

    success = True

    for chunk in chunks:
        try:
            response = SESSION.post(
                telegram_url("sendMessage"),
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                print(
                    "[TELEGRAM] Send failed:",
                    response.status_code,
                    response.text[:500],
                )
                success = False
            else:
                print("[TELEGRAM] Message sent")

        except Exception as exc:
            print(f"[TELEGRAM] Send exception: {exc}")
            success = False

    return success


def telegram_get_updates(offset: int) -> List[Dict[str, Any]]:
    if not telegram_configured():
        return []

    try:
        response = SESSION.get(
            telegram_url("getUpdates"),
            params={
                "offset": offset,
                "timeout": 2,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=5,
        )

        if response.status_code != 200:
            print(
                "[TELEGRAM] getUpdates failed:",
                response.status_code,
            )
            return []

        data = response.json()

        if not data.get("ok"):
            return []

        return data.get("result", [])

    except Exception as exc:
        print(f"[TELEGRAM] getUpdates exception: {exc}")
        return []


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def stats_text() -> str:
    signals = tracker.get("signals", [])

    wins = sum(
        1
        for s in signals
        if str(s.get("outcome", "")).upper() == "WIN"
    )

    losses = sum(
        1
        for s in signals
        if str(s.get("outcome", "")).upper() == "LOSS"
    )

    pending = sum(
        1
        for s in signals
        if str(s.get("outcome", "PENDING")).upper()
        not in ("WIN", "LOSS")
    )

    completed = wins + losses

    win_rate = (
        wins / completed * 100
        if completed
        else 0
    )

    endpoint = tracker["meta"].get(
        "bybit_endpoint",
        "Not established",
    )

    return (
        f"📊 PRECISION SCANNER {VERSION}\n\n"
        f"Signals recorded: {len(signals)}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Pending: {pending}\n"
        f"Completed: {completed}\n"
        f"Recorded win rate: {win_rate:.1f}%\n\n"
        f"Bybit endpoint:\n{endpoint}\n\n"
        f"⚠️ Recorded results are historical test data, "
        f"not a guarantee of future performance."
    )


def help_text() -> str:
    return (
        f"🧠 PRECISION SIGNAL SCANNER {VERSION}\n\n"
        f"/start - Start scanner information\n"
        f"/help - Show commands\n"
        f"/scan - Request an immediate scan\n"
        f"/stats - Show recorded results\n"
        f"/win SIGNAL_ID - Record WIN\n"
        f"/loss SIGNAL_ID - Record LOSS\n\n"
        f"Markets: EURUSD, GBPUSD, USDJPY\n"
        f"Framework: 5M trend + 1M entry\n"
        f"Reference expiry: 5 minutes\n"
        f"OTC: Disabled\n"
    )


def find_signal(signal_id: str) -> Optional[Dict[str, Any]]:
    for signal in tracker.get("signals", []):
        if signal.get("signal_id") == signal_id:
            return signal

    return None


def update_outcome(signal_id: str, outcome: str) -> bool:
    signal = find_signal(signal_id)

    if not signal:
        return False

    signal["outcome"] = outcome
    signal["outcome_time"] = iso_now()

    persist_tracker()

    return True


def process_commands() -> bool:
    """
    Returns True if a /scan command was received.
    """

    manual_scan_requested = False

    offset = int(tracker.get("offset", 0))

    updates = telegram_get_updates(offset)

    for update in updates:
        update_id = update.get("update_id")

        if update_id is not None:
            tracker["offset"] = int(update_id) + 1

        message = update.get("message") or {}
        text = str(message.get("text", "")).strip()

        if not text:
            continue

        parts = text.split()
        command = parts[0].split("@")[0].lower()

        if command == "/start":
            telegram_send(help_text())

        elif command == "/help":
            telegram_send(help_text())

        elif command == "/stats":
            telegram_send(stats_text())

        elif command == "/scan":
            manual_scan_requested = True

            telegram_send(
                "🔎 Immediate scan requested.\n"
                "The scanner will analyze the markets now."
            )

        elif command in ("/win", "/loss"):
            if len(parts) < 2:
                telegram_send(
                    f"Usage: {command} SIGNAL_ID"
                )
                continue

            signal_id = parts[1]

            outcome = (
                "WIN"
                if command == "/win"
                else "LOSS"
            )

            if update_outcome(signal_id, outcome):
                telegram_send(
                    f"✅ {outcome} recorded\n\n"
                    f"Signal: {signal_id}"
                )
            else:
                telegram_send(
                    f"❌ Signal not found:\n{signal_id}"
                )

    persist_tracker()

    return manual_scan_requested


# =========================================================
# BYBIT HTTP
# =========================================================

def classify_http_error(response: requests.Response) -> str:
    code = response.status_code

    if code == 403:
        return (
            "403 FORBIDDEN - Bybit rejected this runner/IP. "
            "Possible causes include regional/IP restrictions "
            "or access restrictions."
        )

    if code == 429:
        return (
            "429 RATE LIMIT - Bybit is rate limiting requests."
        )

    if code == 404:
        return "404 NOT FOUND - endpoint/path issue."

    if code == 400:
        return "400 BAD REQUEST - request parameters rejected."

    if code == 401:
        return "401 UNAUTHORIZED - authentication issue."

    return f"HTTP {code}"


def request_bybit_kline(
    symbol: str,
    interval: str,
    limit: int = CANDLE_LIMIT,
) -> Tuple[List[List[Any]], str]:
    """
    Returns:
        candles, endpoint_used

    Raises:
        RuntimeError with detailed diagnostics.
    """

    global working_bybit_endpoint

    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    endpoint_order = []

    if working_bybit_endpoint:
        endpoint_order.append(working_bybit_endpoint)

    for endpoint in BYBIT_ENDPOINTS:
        if endpoint not in endpoint_order:
            endpoint_order.append(endpoint)

    errors = []

    for endpoint in endpoint_order:
        url = endpoint + BYBIT_KLINE_PATH

        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                print(
                    f"[BYBIT] {symbol} {interval} "
                    f"trying {endpoint} "
                    f"attempt {attempt}/{REQUEST_RETRIES}"
                )

                response = SESSION.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )

                if response.status_code != 200:
                    detail = classify_http_error(response)

                    error_text = (
                        f"{endpoint} -> {detail}"
                    )

                    print(
                        f"[BYBIT] {symbol} {interval}: "
                        f"{error_text}"
                    )

                    errors.append(error_text)

                    # 403 is unlikely to improve by retrying
                    # the same endpoint repeatedly.
                    if response.status_code == 403:
                        break

                    if response.status_code == 429:
                        time.sleep(3)

                    continue

                try:
                    data = response.json()
                except Exception as exc:
                    error_text = (
                        f"{endpoint} -> invalid JSON: {exc}"
                    )

                    print(f"[BYBIT] {error_text}")
                    errors.append(error_text)
                    break

                ret_code = data.get("retCode")

                if ret_code != 0:
                    ret_msg = data.get(
                        "retMsg",
                        "Unknown Bybit error",
                    )

                    error_text = (
                        f"{endpoint} -> "
                        f"retCode={ret_code}, "
                        f"retMsg={ret_msg}"
                    )

                    print(
                        f"[BYBIT] {symbol} {interval}: "
                        f"{error_text}"
                    )

                    errors.append(error_text)
                    break

                result = data.get("result", {})
                candles = result.get("list", [])

                if not candles:
                    error_text = (
                        f"{endpoint} -> successful response "
                        f"but candle list is empty"
                    )

                    print(
                        f"[BYBIT] {symbol} {interval}: "
                        f"{error_text}"
                    )

                    errors.append(error_text)
                    break

                working_bybit_endpoint = endpoint

                tracker["meta"]["bybit_endpoint"] = endpoint

                tested = tracker["meta"].setdefault(
                    "bybit_endpoint_tested",
                    [],
                )

                if endpoint not in tested:
                    tested.append(endpoint)

                print(
                    f"[BYBIT] SUCCESS {symbol} {interval} "
                    f"via {endpoint} "
                    f"candles={len(candles)}"
                )

                return candles, endpoint

            except requests.exceptions.Timeout:
                error_text = (
                    f"{endpoint} -> timeout"
                )

                print(
                    f"[BYBIT] {symbol} {interval}: "
                    f"{error_text}"
                )

                errors.append(error_text)

            except requests.exceptions.RequestException as exc:
                error_text = (
                    f"{endpoint} -> request exception: {exc}"
                )

                print(
                    f"[BYBIT] {symbol} {interval}: "
                    f"{error_text}"
                )

                errors.append(error_text)

            except Exception as exc:
                error_text = (
                    f"{endpoint} -> exception: {exc}"
                )

                print(
                    f"[BYBIT] {symbol} {interval}: "
                    f"{error_text}"
                )

                errors.append(error_text)

    raise RuntimeError(
        f"Unable to fetch {symbol} {interval}. "
        f"Tested official endpoints:\n"
        + "\n".join(errors)
    )


# =========================================================
# CANDLE PARSING
# =========================================================

def parse_candles(raw: List[List[Any]]) -> List[Dict[str, float]]:
    """
    Bybit returns:
    [startTime, open, high, low, close, volume, turnover]

    Returned oldest -> newest.
    """

    candles = []

    for row in raw:
        try:
            if len(row) < 6:
                continue

            candles.append(
                {
                    "time": float(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )

        except Exception:
            continue

    candles.sort(key=lambda x: x["time"])

    return candles


# =========================================================
# INDICATORS
# =========================================================

def ema(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * len(values)

    seed = sum(values[:period]) / period
    result[period - 1] = seed

    multiplier = 2 / (period + 1)

    previous = seed

    for i in range(period, len(values)):
        previous = (
            (values[i] - previous) * multiplier
            + previous
        )

        result[i] = previous

    return result


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    if len(values) <= period:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * len(values)

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(values)):
        gain = gains[i - 1]
        loss = losses[i - 1]

        avg_gain = (
            (avg_gain * (period - 1)) + gain
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + loss
        ) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


def true_ranges(candles: List[Dict[str, float]]) -> List[float]:
    tr = []

    for i, candle in enumerate(candles):
        high = candle["high"]
        low = candle["low"]

        if i == 0:
            tr.append(high - low)
            continue

        previous_close = candles[i - 1]["close"]

        tr.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )

    return tr


def atr(
    candles: List[Dict[str, float]],
    period: int = 14,
) -> List[Optional[float]]:

    tr = true_ranges(candles)

    if len(tr) < period:
        return [None] * len(tr)

    result: List[Optional[float]] = [None] * len(tr)

    current = sum(tr[:period]) / period
    result[period - 1] = current

    for i in range(period, len(tr)):
        current = (
            (current * (period - 1)) + tr[i]
        ) / period

        result[i] = current

    return result


def macd(
    values: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
):
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)

    macd_line: List[Optional[float]] = [None] * len(values)

    for i in range(len(values)):
        if fast[i] is not None and slow[i] is not None:
            macd_line[i] = fast[i] - slow[i]

    usable = [
        x for x in macd_line
        if x is not None
    ]

    signal_values = ema(
        [float(x) for x in usable],
        signal_period,
    )

    signal_line: List[Optional[float]] = [None] * len(values)

    start_index = len(values) - len(signal_values)

    for j, value in enumerate(signal_values):
        if start_index + j < len(values):
            signal_line[start_index + j] = value

    histogram: List[Optional[float]] = [None] * len(values)

    for i in range(len(values)):
        if (
            macd_line[i] is not None
            and signal_line[i] is not None
        ):
            histogram[i] = (
                macd_line[i] - signal_line[i]
            )

    return macd_line, signal_line, histogram


def adx(
    candles: List[Dict[str, float]],
    period: int = 14,
):
    n = len(candles)

    if n <= period + 1:
        return (
            [None] * n,
            [None] * n,
            [None] * n,
        )

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    for i in range(1, n):
        high = candles[i]["high"]
        low = candles[i]["low"]

        prev_high = candles[i - 1]["high"]
        prev_low = candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]

        tr[i] = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        up_move = high - prev_high
        down_move = prev_low - low

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

    atr_values = [None] * n
    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n
    adx_values = [None] * n

    tr_sum = sum(tr[1:period + 1])
    plus_sum = sum(plus_dm[1:period + 1])
    minus_sum = sum(minus_dm[1:period + 1])

    if tr_sum != 0:
        plus_di[period] = (
            100 * plus_sum / tr_sum
        )

        minus_di[period] = (
            100 * minus_sum / tr_sum
        )

        denominator = (
            plus_di[period] + minus_di[period]
        )

        if denominator != 0:
            dx[period] = (
                100
                * abs(
                    plus_di[period]
                    - minus_di[period]
                )
                / denominator
            )

    atr_values[period] = tr_sum / period

    for i in range(period + 1, n):
        tr_sum = (
            tr_sum
            - (tr_sum / period)
            + tr[i]
        )

        plus_sum = (
            plus_sum
            - (plus_sum / period)
            + plus_dm[i]
        )

        minus_sum = (
            minus_sum
            - (minus_sum / period)
            + minus_dm[i]
        )

        if tr_sum != 0:
            plus_di[i] = (
                100 * plus_sum / tr_sum
            )

            minus_di[i] = (
                100 * minus_sum / tr_sum
            )

            denominator = (
                plus_di[i] + minus_di[i]
            )

            if denominator != 0:
                dx[i] = (
                    100
                    * abs(
                        plus_di[i]
                        - minus_di[i]
                    )
                    / denominator
                )

        atr_values[i] = tr_sum / period

    valid_dx = [
        x for x in dx
        if x is not None
    ]

    if len(valid_dx) >= period:
        first_adx = (
            sum(valid_dx[:period]) / period
        )

        first_index = next(
            i
            for i, x in enumerate(dx)
            if x is not None
        ) + period - 1

        if first_index < n:
            adx_values[first_index] = first_adx

        for i in range(first_index + 1, n):
            if dx[i] is not None:
                previous = adx_values[i - 1]

                if previous is not None:
                    adx_values[i] = (
                        (
                            previous * (period - 1)
                        )
                        + dx[i]
                    ) / period

    return (
        adx_values,
        plus_di,
        minus_di,
    )


# =========================================================
# PRICE ACTION
# =========================================================

def candle_strength(candle: Dict[str, float]) -> float:
    high = candle["high"]
    low = candle["low"]
    open_price = candle["open"]
    close = candle["close"]

    rng = high - low

    if rng <= 0:
        return 0.0

    body = abs(close - open_price)

    return body / rng


def candle_direction(
    candle: Dict[str, float],
) -> str:

    if candle["close"] > candle["open"]:
        return "BULLISH"

    if candle["close"] < candle["open"]:
        return "BEARISH"

    return "NEUTRAL"


def structure_direction(
    candles: List[Dict[str, float]],
    lookback: int = 8,
) -> str:

    if len(candles) < lookback + 2:
        return "NEUTRAL"

    recent = candles[-lookback:]

    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    first_half_high = max(
        highs[: len(highs) // 2]
    )

    second_half_high = max(
        highs[len(highs) // 2 :]
    )

    first_half_low = min(
        lows[: len(lows) // 2]
    )

    second_half_low = min(
        lows[len(lows) // 2 :]
    )

    if (
        second_half_high > first_half_high
        and second_half_low > first_half_low
    ):
        return "BULLISH"

    if (
        second_half_high < first_half_high
        and second_half_low < first_half_low
    ):
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# ANALYSIS
# =========================================================

def analyze_timeframe(
    candles: List[Dict[str, float]],
) -> Dict[str, Any]:

    if len(candles) < 60:
        raise ValueError(
            f"Not enough candles: {len(candles)}"
        )

    closes = [
        c["close"]
        for c in candles
    ]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    rsi_values = rsi(closes, 14)

    atr_values = atr(candles, 14)

    macd_line, macd_signal, macd_hist = macd(
        closes
    )

    adx_values, plus_di, minus_di = adx(
        candles,
        14,
    )

    i = len(candles) - 1

    close = closes[i]

    values = {
        "price": close,
        "ema9": ema9[i],
        "ema21": ema21[i],
        "ema50": ema50[i],
        "rsi": rsi_values[i],
        "atr": atr_values[i],
        "macd": macd_line[i],
        "macd_signal": macd_signal[i],
        "macd_hist": macd_hist[i],
        "adx": adx_values[i],
        "plus_di": plus_di[i],
        "minus_di": minus_di[i],
        "structure": structure_direction(candles),
        "candle_direction": candle_direction(
            candles[i]
        ),
        "candle_strength": candle_strength(
            candles[i]
        ),
        "candle_time": int(
            candles[i]["time"] / 1000
        ),
    }

    if (
        values["ema9"] is not None
        and values["ema21"] is not None
        and values["ema50"] is not None
    ):
        if (
            close > values["ema9"]
            and values["ema9"] > values["ema21"]
            and values["ema21"] > values["ema50"]
        ):
            values["trend"] = "BULLISH"

        elif (
            close < values["ema9"]
            and values["ema9"] < values["ema21"]
            and values["ema21"] < values["ema50"]
        ):
            values["trend"] = "BEARISH"

        else:
            values["trend"] = "NEUTRAL"

    else:
        values["trend"] = "NEUTRAL"

    return values


def calculate_pullback_score(
    main: Dict[str, Any],
    entry: Dict[str, Any],
    setup: str,
) -> int:

    score = 0

    if setup == "CALL":
        if (
            main["ema9"] is not None
            and entry["price"] >= main["ema9"]
        ):
            score += 3

        if (
            entry["rsi"] is not None
            and 45 <= entry["rsi"] <= 60
        ):
            score += 3

        if entry["structure"] == "BULLISH":
            score += 4

    elif setup == "PUT":
        if (
            main["ema9"] is not None
            and entry["price"] <= main["ema9"]
        ):
            score += 3

        if (
            entry["rsi"] is not None
            and 40 <= entry["rsi"] <= 55
        ):
            score += 3

        if entry["structure"] == "BEARISH":
            score += 4

    return min(score, 10)


def calculate_room_score(
    candles: List[Dict[str, float]],
    setup: str,
) -> int:

    if len(candles) < 20:
        return 0

    price = candles[-1]["close"]

    recent = candles[-20:]

    if setup == "CALL":
        resistance = max(
            c["high"]
            for c in recent[:-1]
        )

        room = resistance - price

    else:
        support = min(
            c["low"]
            for c in recent[:-1]
        )

        room = price - support

    atr_values = atr(candles, 14)
    current_atr = atr_values[-1]

    if not current_atr or current_atr <= 0:
        return 0

    ratio = room / current_atr

    if ratio >= 2:
        return 5

    if ratio >= 1.5:
        return 4

    if ratio >= 1.0:
        return 3

    if ratio >= 0.5:
        return 2

    if ratio > 0:
        return 1

    return 0


def calculate_extension_score(
    candles: List[Dict[str, float]],
    setup: str,
) -> int:

    if len(candles) < 30:
        return 0

    closes = [
        c["close"]
        for c in candles
    ]

    ema21_values = ema(
        closes,
        21,
    )

    atr_values = atr(
        candles,
        14,
    )

    price = closes[-1]
    ema21_value = ema21_values[-1]
    atr_value = atr_values[-1]

    if (
        ema21_value is None
        or atr_value is None
        or atr_value <= 0
    ):
        return 0

    distance = abs(
        price - ema21_value
    )

    ratio = distance / atr_value

    # Higher score = less extended.
    if ratio <= 0.5:
        return 5

    if ratio <= 0.8:
        return 4

    if ratio <= 1.1:
        return 3

    if ratio <= 1.5:
        return 2

    return 0


def analyze_setup(
    main_candles: List[Dict[str, float]],
    entry_candles: List[Dict[str, float]],
) -> Dict[str, Any]:

    main = analyze_timeframe(
        main_candles
    )

    entry = analyze_timeframe(
        entry_candles
    )

    setup = "NO TRADE"

    if (
        main["trend"] == "BULLISH"
        and entry["trend"] == "BULLISH"
    ):
        setup = "CALL"

    elif (
        main["trend"] == "BEARISH"
        and entry["trend"] == "BEARISH"
    ):
        setup = "PUT"

    # -----------------------------------------------------
    # SCORE COMPONENTS
    # -----------------------------------------------------

    trend_score = 0

    if setup == "CALL":
        if main["trend"] == "BULLISH":
            trend_score += 12

        if entry["trend"] == "BULLISH":
            trend_score += 8

    elif setup == "PUT":
        if main["trend"] == "BEARISH":
            trend_score += 12

        if entry["trend"] == "BEARISH":
            trend_score += 8

    structure_score = 0

    if setup != "NO TRADE":
        if main["structure"] == (
            "BULLISH"
            if setup == "CALL"
            else "BEARISH"
        ):
            structure_score += 6

        if entry["structure"] == (
            "BULLISH"
            if setup == "CALL"
            else "BEARISH"
        ):
            structure_score += 4

    adx_score = 0

    adx_value = entry["adx"]

    if adx_value is not None:
        if adx_value >= STRONG_ADX:
            adx_score = 10

        elif adx_value >= MIN_ADX:
            adx_score = 7

        else:
            adx_score = 0

    dmi_direction = "NEUTRAL"

    if (
        entry["plus_di"] is not None
        and entry["minus_di"] is not None
    ):
        if entry["plus_di"] > entry["minus_di"]:
            dmi_direction = "BULLISH"

        elif entry["minus_di"] > entry["plus_di"]:
            dmi_direction = "BEARISH"

    if setup == "CALL":
        if dmi_direction == "BULLISH":
            adx_score = min(10, adx_score + 0)

    elif setup == "PUT":
        if dmi_direction == "BEARISH":
            adx_score = min(10, adx_score + 0)

    macd_score = 0

    hist = entry["macd_hist"]

    if hist is not None:
        if setup == "CALL" and hist > 0:
            macd_score = 10

        elif setup == "PUT" and hist < 0:
            macd_score = 10

    rsi_score = 0

    rsi_value = entry["rsi"]

    if rsi_value is not None:
        if setup == "CALL":
            if CALL_RSI_MIN <= rsi_value <= CALL_RSI_MAX:
                rsi_score = 10

            elif 40 <= rsi_value <= 70:
                rsi_score = 5

        elif setup == "PUT":
            if PUT_RSI_MIN <= rsi_value <= PUT_RSI_MAX:
                rsi_score = 10

            elif 30 <= rsi_value <= 60:
                rsi_score = 5

    entry_score = 0

    if setup == "CALL":
        if entry["trend"] == "BULLISH":
            entry_score += 8

        if entry["candle_direction"] == "BULLISH":
            entry_score += 4

        if (
            entry["ema9"] is not None
            and entry["price"] >= entry["ema9"]
        ):
            entry_score += 3

    elif setup == "PUT":
        if entry["trend"] == "BEARISH":
            entry_score += 8

        if entry["candle_direction"] == "BEARISH":
            entry_score += 4

        if (
            entry["ema9"] is not None
            and entry["price"] <= entry["ema9"]
        ):
            entry_score += 3

    pullback_score = calculate_pullback_score(
        main,
        entry,
        setup,
    )

    candle_score = 0

    if entry["candle_strength"] >= 0.70:
        candle_score = 5

    elif entry["candle_strength"] >= MIN_CANDLE_STRENGTH:
        candle_score = 3

    room_score = calculate_room_score(
        entry_candles,
        setup,
    )

    extension_score = calculate_extension_score(
        entry_candles,
        setup,
    )

    total_score = (
        trend_score
        + structure_score
        + adx_score
        + macd_score
        + rsi_score
        + entry_score
        + pullback_score
        + candle_score
        + room_score
        + extension_score
    )

    # -----------------------------------------------------
    # DIRECTIONAL DOMINANCE
    # -----------------------------------------------------

    bullish_points = 0
    bearish_points = 0

    if main["trend"] == "BULLISH":
        bullish_points += 2

    elif main["trend"] == "BEARISH":
        bearish_points += 2

    if entry["trend"] == "BULLISH":
        bullish_points += 2

    elif entry["trend"] == "BEARISH":
        bearish_points += 2

    if main["structure"] == "BULLISH":
        bullish_points += 1

    elif main["structure"] == "BEARISH":
        bearish_points += 1

    if entry["structure"] == "BULLISH":
        bullish_points += 1

    elif entry["structure"] == "BEARISH":
        bearish_points += 1

    if dmi_direction == "BULLISH":
        bullish_points += 1

    elif dmi_direction == "BEARISH":
        bearish_points += 1

    if hist is not None:
        if hist > 0:
            bullish_points += 1

        elif hist < 0:
            bearish_points += 1

    dominance = abs(
        bullish_points - bearish_points
    )

    # -----------------------------------------------------
    # BLOCKERS
    # -----------------------------------------------------

    blockers = []

    if setup == "NO TRADE":
        blockers.append(
            "5M/1M trend mismatch"
        )

    if setup == "CALL":
        if main["trend"] != "BULLISH":
            blockers.append(
                "5M trend not bullish"
            )

        if entry["trend"] != "BULLISH":
            blockers.append(
                "1M entry trend not bullish"
            )

        if main["structure"] != "BULLISH":
            blockers.append(
                "5M structure not bullish"
            )

        if entry["structure"] != "BULLISH":
            blockers.append(
                "1M structure not bullish"
            )

        if dmi_direction != "BULLISH":
            blockers.append(
                "DMI not bullish"
            )

        if hist is None or hist <= 0:
            blockers.append(
                "MACD not bullish"
            )

        if (
            rsi_value is None
            or not (
                CALL_RSI_MIN
                <= rsi_value
                <= CALL_RSI_MAX
            )
        ):
            blockers.append(
                "RSI outside CALL zone"
            )

        if (
            entry["candle_direction"]
            != "BULLISH"
        ):
            blockers.append(
                "confirmation candle not bullish"
            )

    elif setup == "PUT":
        if main["trend"] != "BEARISH":
            blockers.append(
                "5M trend not bearish"
            )

        if entry["trend"] != "BEARISH":
            blockers.append(
                "1M entry trend not bearish"
            )

        if main["structure"] != "BEARISH":
            blockers.append(
                "5M structure not bearish"
            )

        if entry["structure"] != "BEARISH":
            blockers.append(
                "1M structure not bearish"
            )

        if dmi_direction != "BEARISH":
            blockers.append(
                "DMI not bearish"
            )

        if hist is None or hist >= 0:
            blockers.append(
                "MACD not bearish"
            )

        if (
            rsi_value is None
            or not (
                PUT_RSI_MIN
                <= rsi_value
                <= PUT_RSI_MAX
            )
        ):
            blockers.append(
                "RSI outside PUT zone"
            )

        if (
            entry["candle_direction"]
            != "BEARISH"
        ):
            blockers.append(
                "confirmation candle not bearish"
            )

    if adx_value is None or adx_value < MIN_ADX:
        blockers.append(
            f"ADX below {MIN_ADX}"
        )

    if pullback_score < 3:
        blockers.append(
            "no clean pullback"
        )

    if room_score < MIN_ROOM_SCORE:
        blockers.append(
            "insufficient room"
        )

    if extension_score < MIN_EXTENSION_SCORE:
        blockers.append(
            "price too extended"
        )

    if dominance < MIN_DOMINANCE:
        blockers.append(
            "weak directional dominance"
        )

    qualified = (
        setup in ("CALL", "PUT")
        and total_score >= MIN_SCORE
        and dominance >= MIN_DOMINANCE
        and len(blockers) == 0
    )

    borderline = (
        setup in ("CALL", "PUT")
        and BORDERLINE_SCORE
        <= total_score
        < MIN_SCORE
        and len(blockers) == 0
    )

    final_signal = (
        setup
        if qualified
        else "NO TRADE"
    )

    return {
        "setup": setup,
        "final_signal": final_signal,
        "qualified": qualified,
        "borderline": borderline,
        "score": total_score,
        "dominance": dominance,
        "bullish_points": bullish_points,
        "bearish_points": bearish_points,
        "blockers": blockers,
        "trend_score": trend_score,
        "structure_score": structure_score,
        "adx_score": adx_score,
        "macd_score": macd_score,
        "rsi_score": rsi_score,
        "entry_score": entry_score,
        "pullback_score": pullback_score,
        "candle_score": candle_score,
        "room_score": room_score,
        "extension_score": extension_score,
        "main": main,
        "entry": entry,
    }


# =========================================================
# SIGNAL IDs / LOCKS / DEDUPLICATION
# =========================================================

def signal_id(
    symbol: str,
    final_signal: str,
    candle_time: int,
) -> str:

    return (
        f"{symbol}-"
        f"{final_signal}-"
        f"{candle_time}"
    )


def processed_key(
    symbol: str,
    signal: str,
    candle_time: int,
) -> str:

    return (
        f"{symbol}|"
        f"{signal}|"
        f"{candle_time}"
    )


def asset_locked(symbol: str) -> bool:
    locks = tracker["meta"].setdefault(
        "signal_locks",
        {},
    )

    lock_until = int(
        locks.get(symbol, 0)
    )

    return unix_now() < lock_until


def set_asset_lock(symbol: str) -> None:
    locks = tracker["meta"].setdefault(
        "signal_locks",
        {},
    )

    # IMPORTANT:
    # Lock begins NOW, not at candle timestamp.
    locks[symbol] = (
        unix_now()
        + SIGNAL_LOCK_SECONDS
    )


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def build_signal_message(
    symbol: str,
    broker_symbol: str,
    analysis: Dict[str, Any],
    endpoint: str,
) -> str:

    signal = analysis["final_signal"]

    main = analysis["main"]
    entry = analysis["entry"]

    emoji = (
        "🟢"
        if signal == "CALL"
        else "🔴"
    )

    return (
        f"{emoji} NEW QUALIFIED SIGNAL\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{symbol} {signal}\n\n"
        f"🎯 Score: "
        f"{analysis['score']}/100\n"
        f"⏱ Reference expiry: "
        f"{REFERENCE_EXPIRY_MINUTES} minutes\n"
        f"💰 Price: "
        f"{entry['price']:.8f}\n\n"
        f"📊 5M Trend: "
        f"{main['trend']}\n"
        f"📈 1M Entry: "
        f"{entry['trend']}\n"
        f"🏗 Structure: "
        f"{entry['structure']}\n"
        f"📐 ADX: "
        f"{entry['adx']:.1f}\n"
        f"📉 RSI: "
        f"{entry['rsi']:.1f}\n"
        f"📊 MACD Hist: "
        f"{entry['macd_hist']:.8f}\n"
        f"🕯 Candle: "
        f"{entry['candle_strength']:.2f}\n"
        f"↕ Dominance: "
        f"{analysis['dominance']}\n\n"
        f"📡 Data: Bybit\n"
        f"🔌 Endpoint: "
        f"{endpoint}\n"
        f"📦 Symbol: "
        f"{broker_symbol}\n\n"
        f"⚠️ TESTING ONLY\n"
        f"Score = setup quality, "
        f"not guaranteed win probability.\n"
        f"No profitability guarantee."
    )


# =========================================================
# PROCESS ONE SYMBOL
# =========================================================

def scan_symbol(
    symbol: str,
    broker_symbol: str,
) -> Dict[str, Any]:

    print(
        f"\n[SCAN] {symbol} "
        f"({broker_symbol}) mode=NORMAL"
    )

    main_raw, main_endpoint = request_bybit_kline(
        broker_symbol,
        MAIN_TIMEFRAME,
    )

    entry_raw, entry_endpoint = request_bybit_kline(
        broker_symbol,
        ENTRY_TIMEFRAME,
    )

    main_candles = parse_candles(
        main_raw
    )

    entry_candles = parse_candles(
        entry_raw
    )

    if len(main_candles) < 60:
        raise RuntimeError(
            f"{symbol}: only "
            f"{len(main_candles)} "
            f"5M candles available"
        )

    if len(entry_candles) < 60:
        raise RuntimeError(
            f"{symbol}: only "
            f"{len(entry_candles)} "
            f"1M candles available"
        )

    analysis = analyze_setup(
        main_candles,
        entry_candles,
    )

    endpoint = entry_endpoint or main_endpoint

    result = {
        "symbol": symbol,
        "broker_symbol": broker_symbol,
        "endpoint": endpoint,
        **analysis,
    }

    print(
        f"[RESULT] {symbol} "
        f"setup={analysis['setup']} "
        f"signal={analysis['final_signal']} "
        f"score={analysis['score']}/100 "
        f"dominance={analysis['dominance']}"
    )

    if analysis["blockers"]:
        print(
            f"[BLOCKERS] {symbol}: "
            + ", ".join(
                analysis["blockers"]
            )
        )

    return result


# =========================================================
# PROCESS RESULT
# =========================================================

def process_result(
    result: Dict[str, Any],
) -> Dict[str, int]:

    symbol = result["symbol"]

    final_signal = result["final_signal"]

    entry = result["entry"]

    candle_time = int(
        entry["candle_time"]
    )

    outcome = {
        "qualified": 0,
        "borderline": 0,
        "rejected": 0,
        "new_record": 0,
        "alert_sent": 0,
        "alert_failed": 0,
        "locked": 0,
        "duplicate": 0,
    }

    if result["borderline"]:
        outcome["borderline"] = 1

    if final_signal == "NO TRADE":
        outcome["rejected"] = 1
        return outcome

    if not result["qualified"]:
        outcome["rejected"] = 1
        return outcome

    outcome["qualified"] = 1

    key = processed_key(
        symbol,
        final_signal,
        candle_time,
    )

    signal_key_list = tracker["meta"].setdefault(
        "alerted_keys",
        [],
    )

    if key in signal_key_list:
        print(
            f"[DEDUP] Already processed: {key}"
        )
        outcome["duplicate"] = 1
        return outcome

    signal = signal_id(
        symbol,
        final_signal,
        candle_time,
    )

    if find_signal(signal):
        print(
            f"[DEDUP] Signal ID already recorded: "
            f"{signal}"
        )

        signal_key_list.append(key)

        outcome["duplicate"] = 1

        persist_tracker()

        return outcome

    if asset_locked(symbol):
        print(
            f"[LOCK] {symbol} locked. "
            f"Qualified setup suppressed."
        )

        outcome["locked"] = 1

        # Mark the setup as processed so the same
        # candle does not repeatedly alert.
        signal_key_list.append(key)

        persist_tracker()

        return outcome

    record_time = unix_now()

    record = {
        "signal_id": signal,
        "symbol": symbol,
        "broker_symbol": result["broker_symbol"],
        "signal": final_signal,
        "score": result["score"],
        "dominance": result["dominance"],
        "price": entry["price"],
        "created_at": record_time,
        "created_at_iso": iso_now(),
        "candle_time": candle_time,
        "candle_time_iso": format_time(
            candle_time
        ),
        "reference_expiry_minutes":
            REFERENCE_EXPIRY_MINUTES,
        "outcome": "PENDING",
        "endpoint": result["endpoint"],
        "setup": result["setup"],
        "main_trend": result["main"]["trend"],
        "entry_trend": result["entry"]["trend"],
        "main_structure":
            result["main"]["structure"],
        "entry_structure":
            result["entry"]["structure"],
        "adx": result["entry"]["adx"],
        "rsi": result["entry"]["rsi"],
        "macd_hist":
            result["entry"]["macd_hist"],
        "pullback_score":
            result["pullback_score"],
        "room_score":
            result["room_score"],
        "extension_score":
            result["extension_score"],
    }

    tracker["signals"].append(record)

    # Mark as processed BEFORE Telegram delivery.
    #
    # This prevents duplicate signals when Telegram
    # fails but the workflow retries the same setup.
    signal_key_list.append(key)

    tracker["meta"]["last_signal"] = record_time

    set_asset_lock(symbol)

    trim_tracker()

    outcome["new_record"] = 1

    persist_tracker()

    message = build_signal_message(
        symbol,
        result["broker_symbol"],
        result,
        result["endpoint"],
    )

    delivered = telegram_send(message)

    if delivered:
        outcome["alert_sent"] = 1
        print(
            f"[ALERT] Telegram delivered: "
            f"{signal}"
        )

    else:
        outcome["alert_failed"] = 1

        failed = tracker["meta"].setdefault(
            "delivery_failed_keys",
            [],
        )

        if key not in failed:
            failed.append(key)

        persist_tracker()

        print(
            f"[ALERT] Telegram delivery failed: "
            f"{signal}"
        )

    return outcome


# =========================================================
# SCAN CYCLE
# =========================================================

def run_scan_cycle() -> Dict[str, Any]:

    scan_number = (
        tracker["meta"].get(
            "scan_number",
            0,
        )
        + 1
    )

    tracker["meta"]["scan_number"] = scan_number
    tracker["meta"]["last_scan"] = unix_now()

    print("\n")
    print("=" * 60)
    print(f"PRECISION SIGNAL SCANNER {VERSION}")
    print(f"SCAN #{scan_number}")
    print(f"Time: {iso_now()}")
    print("=" * 60)

    summary = {
        "qualified": 0,
        "borderline": 0,
        "rejected": 0,
        "errors": 0,
        "new_records": 0,
        "alerts_sent": 0,
        "alerts_failed": 0,
        "locked": 0,
        "duplicates": 0,
    }

    for symbol, broker_symbol in NORMAL_SYMBOLS.items():

        try:
            result = scan_symbol(
                symbol,
                broker_symbol,
            )

            processed = process_result(
                result
            )

            for key in summary:
                mapping = {
                    "qualified": "qualified",
                    "borderline": "borderline",
                    "rejected": "rejected",
                    "new_records": "new_record",
                    "alerts_sent": "alert_sent",
                    "alerts_failed": "alert_failed",
                    "locked": "locked",
                    "duplicates": "duplicate",
                }

                if key in mapping:
                    summary[key] += processed[
                        mapping[key]
                    ]

        except Exception as exc:

            summary["errors"] += 1

            print(
                f"[ERROR] {symbol}: {exc}"
            )

    trim_tracker()

    persist_tracker()

    print("\n" + "=" * 60)
    print("SCAN SUMMARY")
    print("=" * 60)

    print(
        f"Qualified: {summary['qualified']}"
    )

    print(
        f"Borderline: {summary['borderline']}"
    )

    print(
        f"Rejected: {summary['rejected']}"
    )

    print(
        f"Errors: {summary['errors']}"
    )

    print(
        f"New records: {summary['new_records']}"
    )

    print(
        f"Telegram sent: {summary['alerts_sent']}"
    )

    print(
        f"Telegram failed: {summary['alerts_failed']}"
    )

    print(
        f"Locked: {summary['locked']}"
    )

    print(
        f"Duplicates: {summary['duplicates']}"
    )

    endpoint = tracker["meta"].get(
        "bybit_endpoint"
    )

    print(
        f"Working Bybit endpoint: "
        f"{endpoint or 'NONE'}"
    )

    print("=" * 60)

    return summary


# =========================================================
# MANUAL SUMMARY
# =========================================================

def send_scan_summary(summary: Dict[str, Any]) -> None:

    endpoint = tracker["meta"].get(
        "bybit_endpoint",
        "NONE",
    )

    text = (
        f"🔎 SCAN COMPLETE — {VERSION}\n\n"
        f"Qualified: {summary['qualified']}\n"
        f"Borderline: {summary['borderline']}\n"
        f"Rejected: {summary['rejected']}\n"
        f"Errors: {summary['errors']}\n"
        f"New records: {summary['new_records']}\n"
        f"Telegram alerts sent: "
        f"{summary['alerts_sent']}\n"
        f"Telegram failures: "
        f"{summary['alerts_failed']}\n\n"
        f"Bybit endpoint:\n{endpoint}\n\n"
        f"OTC: Disabled"
    )

    telegram_send(text)


# =========================================================
# STARTUP DIAGNOSTIC
# =========================================================

def print_configuration() -> None:

    print("=" * 60)
    print(f"PRECISION SIGNAL SCANNER {VERSION}")
    print("=" * 60)

    print(f"Started: {iso_now()}")

    print(
        "Mode: "
        + (
            "GITHUB ONE-SHOT"
            if GITHUB_ACTIONS
            else "LOCAL"
        )
    )

    print(
        "Normal markets: "
        + ", ".join(
            NORMAL_SYMBOLS.keys()
        )
    )

    print(
        f"5M timeframe: "
        f"{MAIN_TIMEFRAME}"
    )

    print(
        f"1M timeframe: "
        f"{ENTRY_TIMEFRAME}"
    )

    print(
        f"Reference expiry: "
        f"{REFERENCE_EXPIRY_MINUTES} minutes"
    )

    print(
        f"Minimum score: "
        f"{MIN_SCORE}/100"
    )

    print(
        f"OTC enabled: "
        f"{OTC_ENABLED}"
    )

    print("\nOfficial Bybit endpoints:")
    for endpoint in BYBIT_ENDPOINTS:
        print(f"  - {endpoint}")

    print(
        "\nTelegram configured: "
        f"{telegram_configured()}"
    )

    print("=" * 60)


# =========================================================
# LOOP MODE
# =========================================================

def run_loop() -> None:

    print_configuration()

    while True:

        try:
            process_commands()

            run_scan_cycle()

        except KeyboardInterrupt:
            print("\n[STOP] Scanner stopped.")
            break

        except Exception as exc:
            print(
                f"[LOOP ERROR] {exc}"
            )

            traceback.print_exc()

        print(
            f"\n[WAIT] "
            f"{SCAN_INTERVAL_SECONDS} seconds..."
        )

        time.sleep(
            SCAN_INTERVAL_SECONDS
        )


# =========================================================
# ONE-SHOT MODE
# =========================================================

def run_once() -> None:

    print_configuration()

    # Process commands first.
    #
    # If /scan was waiting in Telegram, it is consumed
    # and this same one-shot execution performs the scan.
    manual_scan_requested = (
        process_commands()
    )

    if manual_scan_requested:
        print(
            "[COMMAND] /scan detected. "
            "Running immediately."
        )

    summary = run_scan_cycle()

    if manual_scan_requested:
        send_scan_summary(
            summary
        )

    print(
        "\n[COMPLETE] "
        "One scanner cycle finished."
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    mode = (
        sys.argv[1].lower()
        if len(sys.argv) > 1
        else "--once"
    )

    if mode == "--loop":
        run_loop()

    elif mode == "--once":
        run_once()

    else:
        print(
            "Usage:\n"
            "  python scanner.py --once\n"
            "  python scanner.py --loop"
        )

        sys.exit(1)exit(1)
